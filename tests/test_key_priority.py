#!/usr/bin/env python3
"""回归：API key 优先级（插队）。

背景：泳道池只有 2 条泳道、容量 3（flash cap=1 + gptoss cap=2）。第 4 个上下文开始排队。
排队本身是设计（不是 429），但**排队顺序**之前是纯先来后到：一个后台批处理任务先到，
就能让交互请求排在它后面。这一组断言盯三件事：

  ① 优先级的唯一来源是 key 配置，非法值必须安静降级（不能 500，也不能变成 VIP）；
  ② 插队只发生在"抢资源"这一步，且只有一个判定点（_prio_turn）——
     高优先级越过先到的低优先级拿刚空出来的位置；
  ③ 不许饿死：让路有窗口（LANE_PRIO_HOLD_S），窗口过后低优先级恢复先来后到。

用法：
  python3 tests/test_key_priority.py
环境变量：
  TG_SRC  被测网关文件，默认仓库根的 token-gateway.py
"""
import ast
import hashlib
import itertools
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("TG_SRC") or os.path.join(os.path.dirname(HERE), "token-gateway.py")

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}  {detail}")


def _env_num(name, default, cast=int, minimum=None, maximum=None):
    """与网关同名的桩：读环境变量，非法值退回默认值。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = cast(raw)
    except Exception:
        return default
    if minimum is not None:
        val = max(minimum, val)
    if maximum is not None:
        val = min(maximum, val)
    return val


WANTED_FUNCS = {
    "_parse_lanes", "_lane_context_key", "_norm_prio", "_key_priority", "_prio_turn",
    "_acquire_conc_slot", "_drop_active_req", "_conc_limit",
}
WANTED_CLASSES = {"LanePool", "LaneTimeout", "LaneQueueFull"}
KEY_CONSTS = {"KEY_PRIORITY_MAX", "KEY_PRIO_SLOT_WAIT_S", "KEY_PRIO_YIELD_S"}


def load_module(src_path):
    """从源码里抽真实定义（不是复制逻辑），注入桩后 exec。"""
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in WANTED_FUNCS:
            nodes.append(n)
        elif isinstance(n, ast.ClassDef) and n.name in WANTED_CLASSES:
            nodes.append(n)
        elif isinstance(n, ast.Assign) and any(
                getattr(t, "id", "").startswith("LANE_")
                or getattr(t, "id", "") in KEY_CONSTS for t in n.targets):
            nodes.append(n)
    found = {n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    missing = (WANTED_FUNCS | WANTED_CLASSES) - found
    if missing:
        raise SystemExit(f"源码里找不到这些定义：{sorted(missing)}")
    ns = {
        "json": json, "re": re, "sys": sys, "time": time, "threading": threading,
        "itertools": itertools, "hashlib": hashlib, "OrderedDict": OrderedDict,
        "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple,
        "os": os, "_env_num": _env_num,
        "RAPID_MLX_HOSTS": ["http://127.0.0.1:8090"],
        "log": lambda *a, **k: None,
        # 并发槽相关的模块级状态（网关里在别处定义，这里按原样搭好）
        "state_lock": threading.Condition(threading.Lock()),
        "active_concurrency": 0,
        "_conc_waiters": [],
        "_conc_seq": itertools.count(1),
        "_active_reqs": {},
        "DEFAULT_MAX_CONCURRENCY": 10,
        "_cfg": {"global": {"max_concurrency": 2}},
    }
    for n in nodes:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "<gateway>", "exec"), ns)
    return ns


NS = load_module(SRC)
norm_prio = NS["_norm_prio"]
key_priority = NS["_key_priority"]
prio_turn = NS["_prio_turn"]
LanePool = NS["LanePool"]
LaneTimeout = NS["LaneTimeout"]
acquire_slot = NS["_acquire_conc_slot"]
drop_req = NS["_drop_active_req"]
KEY_PRIORITY_MAX = NS["KEY_PRIORITY_MAX"]
LANE_PRIO_HOLD_S = NS["LANE_PRIO_HOLD_S"]
KEY_PRIO_YIELD_S = NS["KEY_PRIO_YIELD_S"]


def lanes(*specs):
    return [dict(name=n, model=m, host=h, rank=r, cap=c) for n, m, h, r, c in specs]


L1 = ("flash", "qwen3.8-flash-next", "http://127.0.0.1:8090", 100, 1)


print("1. 优先级来自配置：非法值必须安静降级，越界必须夹紧")
check("缺省 → 0", norm_prio(None) == 0 and norm_prio("") == 0)
check("整数照用", norm_prio(7) == 7)
check("字符串数字照用（config.json 手写常见）", norm_prio("12") == 12)
check("非数字 → 0，不是异常", norm_prio("high") == 0)
check("浮点字符串 → 0（不猜 3.7 该进到哪一档）", norm_prio("3.7") == 0)
check(f"上限夹到 +{KEY_PRIORITY_MAX}", norm_prio(99999) == KEY_PRIORITY_MAX, norm_prio(99999))
check(f"下限夹到 -{KEY_PRIORITY_MAX}", norm_prio(-99999) == -KEY_PRIORITY_MAX, norm_prio(-99999))
check("key_cfg 没有 priority → 0（老配置零改动）", key_priority({"name": "x"}) == 0)
check("key_cfg 为 None → 0（不抛）", key_priority(None) == 0)
check("key_cfg 带 priority 直接透出", key_priority({"priority": 5}) == 5)


print("\n2. 排队纪律（_prio_turn）：唯一的插队判定点")
now = time.time()
low = {"prio": 0, "seq": 1, "since": now, "expire_at": now + 60}
vip = {"prio": 9, "seq": 2, "since": now, "expire_at": now + 60}
peer = {"prio": 9, "seq": 3, "since": now, "expire_at": now + 60}

check("空队列 → 谁都可以", prio_turn([], low, 10.0, now) is True)
check("我是排头 → 可以", prio_turn([vip, low], vip, 10.0, now) is True)
check("排头比我优先级高、且在让路窗口内 → 我让（这就是插队）",
      prio_turn([vip, low], low, 10.0, now) is False)
check("让路窗口过后 → 我也可以抢（防饿死）",
      prio_turn([vip, low], low, 10.0, now + 11) is True)
check("排头优先级不高于我 → 我不被拦住（同级可竞争，避免队头阻塞）",
      prio_turn([peer, low], vip, 10.0, now) is True)
_exp = {"prio": 9, "seq": 0, "since": now - 100, "expire_at": now - 1}
check("已过期的登记被跳过：卡死的等待者不能堵住整条队列",
      prio_turn([_exp, low], low, 10.0, now) is True)

print("\n3. 泳道队列：高优先级越过先到的低优先级，拿走刚空出来的位置")
P = LanePool(lanes(L1), wait_s=5, sticky=False, prio_hold_s=5.0)
holder, _ = P.acquire("holder")
order = []
got = threading.Event()


def waiter(name, prio):
    try:
        lane, waited = P.acquire(f"ctx-{name}", timeout=5, priority=prio)
        order.append((name, round(waited, 3)))
        if name == "vip":
            got.set()
        # 拿住 0.3s 再还，让"谁先拿到"这件事可观测
        time.sleep(0.3)
        P.release(lane)
    except Exception as e:                                    # noqa: BLE001
        order.append((name, type(e).__name__))


tl = threading.Thread(target=waiter, args=("low", 0))
tl.start()
time.sleep(0.2)                     # 让低优先级先入队（先来）
tv = threading.Thread(target=waiter, args=("vip", 10))
tv.start()
time.sleep(0.2)                     # 让 VIP 完成登记
P.release(holder)                   # 空出一个位置：按旧逻辑是 low 先拿
tv.join(6)
tl.join(6)

check("VIP 先拿到（低优先级先到但让路）", order and order[0][0] == "vip", order)
check("低优先级没有失败，只是晚了", all(x[0] in ("vip", "low") for x in order) and len(order) == 2, order)
check("metrics.prio_inserts 记到了这次插队", P.snapshot()["metrics"]["prio_inserts"] >= 1,
      P.snapshot()["metrics"])
check("metrics.prio_holds 记到了有人让路", P.snapshot()["metrics"]["prio_holds"] >= 1,
      P.snapshot()["metrics"])

print("\n4. 低优先级/普通优先级的行为不变：先来后到，不因功能存在而变慢")
P2 = LanePool(lanes(L1), wait_s=5, sticky=False)
h2, _ = P2.acquire("h")
res = []


def plain(n):
    p, w = P2.acquire(f"p{n}", timeout=5)
    res.append(n)
    time.sleep(0.05)
    P2.release(p)


ts = [threading.Thread(target=plain, args=(i,)) for i in range(2)]
for t in ts:
    t.start()
    time.sleep(0.05)
P2.release(h2)
for t in ts:
    t.join(6)
check("两个普通请求都拿到（没有因为插队逻辑被误拒）", sorted(res) == [0, 1], res)
check("普通请求排队不记插队", P2.snapshot()["metrics"]["prio_inserts"] == 0,
      P2.snapshot()["metrics"])

print("\n5. 全局并发槽：VIP 有界等待，普通请求仍快速失败")
SLOT = NS["_acquire_conc_slot"]
DROP = NS["_drop_active_req"]
ns = NS


def reset_slots(limit=2):
    ns["_cfg"]["global"]["max_concurrency"] = limit
    ns["_active_reqs"].clear()
    ns["_conc_waiters"].clear()
    ns["active_concurrency"] = 0


def take(prio=0, wait=0.0):
    return SLOT(prio, wait)


def release_all():
    for rid in list(ns["_active_reqs"]):
        DROP(rid)
    ns["active_concurrency"] = 0


reset_slots(2)
ok1, how1, w1 = take(0)
ok2, how2, w2 = take(0)
check("空槽：两个普通请求都直接拿到", ok1 and ok2 and how1 == how2 == "direct")
ok3, how3, _ = take(0)
check("槽满：普通请求立刻 busy（不排队、不等待）", ok3 is False and how3 == "busy", how3)

res_vip = {}


def vip_thread():
    t0 = time.time()
    ok, how, waited = take(10, 3.0)
    res_vip.update(ok=ok, how=how, waited=waited, wall=time.time() - t0)


tvip = threading.Thread(target=vip_thread)
tvip.start()
time.sleep(0.15)
check("槽满时 VIP 不立刻失败（它在等）", tvip.is_alive())
release_all()                       # 腾出位置 → 应该唤醒 VIP
tvip.join(4)
check("VIP 等到空位并拿到槽", res_vip.get("ok") is True, res_vip)
check("拿到的方式是 queued（确实等过，不是恰好路过）", res_vip.get("how") == "queued", res_vip)
check("等待时长与真实墙钟一致（没有被轮询放大）",
      abs(res_vip.get("waited", 0) - res_vip.get("wall", 0)) < 0.3, res_vip)
release_all()

reset_slots(1)
ok, _, _ = take(0)
check("占用唯一槽位", ok is True)
t0 = time.time()
ok_to, how_to, waited_to = take(10, 0.3)
el = time.time() - t0
check("没人释放时 VIP 有界超时（不会无限挂住）", ok_to is False and how_to == "timeout", how_to)
check("超时接近预算（0.3s）", 0.25 <= el <= 0.8, el)
release_all()

print("\n6. 两个 VIP 之间：优先级高的先拿（同级才看先来后到）")
reset_slots(1)
take(0)
got_order = []


def vip2(prio, tag):
    ok, how, _ = take(prio, 3.0)
    if ok:
        got_order.append(tag)
        time.sleep(0.2)
        release_all()


a = threading.Thread(target=vip2, args=(5, "p5"))
a.start()
time.sleep(0.2)
b = threading.Thread(target=vip2, args=(20, "p20"))
b.start()
time.sleep(0.2)
release_all()
a.join(6)
b.join(6)
check("后到但优先级更高的 p20 先拿到（5 < 20）", got_order and got_order[0] == "p20", got_order)
release_all()

print("\n7. 让路/等待的参数有界，不会把短请求拖成长请求")
check("普通请求的让路预算 ≤ 0.25s（远小于 VIP 的 6s 等待窗口）", KEY_PRIO_YIELD_S <= 0.25, KEY_PRIO_YIELD_S)
check("让路窗口有限（默认 10s），不是永远让", 0 < LANE_PRIO_HOLD_S < 120, LANE_PRIO_HOLD_S)

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(0 if FAIL == 0 else 1)
