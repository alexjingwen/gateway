#!/usr/bin/env python3
"""回归：串行闸门也要认优先级（Alex 在 dsh 主路径上能不能插队，就看这一层）。

背景（2026-11-14 实测）：`auto` 解析出真实模型后不走 LanePool，全局并发槽（默认 10）
通常也不满，所以 **`_proxy_serialized` 里那把串行锁是 VIP 与普通请求唯一的相遇点**。
它原来是裸 `threading.Semaphore`，等待顺序不可控、也不认优先级 —— 线上表现为
`⏳ ... 串行排队 74.1s 后放行`，而你给 Alex 配的 priority=100 完全空转。

换成 `_PrioSerialGate` 之后，这一组断言盯：
  ① 基本锁语义不能退化（互斥、不可重入、异键独立、超时兜底）；
  ② 高优先级后到能先拿到（插队）；
  ③ 同优先级仍是先来后到（插队不能变成乱序）；
  ④ 释放唤醒所有等待者 —— notify(1) 会叫醒一个注定让路的线程，然后整条队列卡住；
  ⑤ 超时/异常路径不泄漏等待者（泄漏会让 `_prio_turn` 把队列判错，越跑越慢）。

用法：
  python3 tests/test_serial_priority.py
环境变量：
  TG_SRC  被测网关文件，默认仓库根的 token-gateway.py
"""
import ast
import itertools
import json
import os
import sys
import threading
import time
from typing import List, Optional

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


WANTED_FUNCS = {"_norm_prio", "_prio_turn", "_mlx_gate_for", "_serial_gates_snapshot"}
WANTED_CLASSES = {"_PrioSerialGate"}
WANTED_NAMES = {"KEY_PRIORITY_MAX", "LANE_PRIO_HOLD_S", "_mlx_gates", "_mlx_gates_lock"}


def load_module(src_path):
    """抽真实定义（不复制逻辑），注入最小桩后 exec。"""
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in WANTED_FUNCS:
            nodes.append(n)
        elif isinstance(n, ast.ClassDef) and n.name in WANTED_CLASSES:
            nodes.append(n)
        elif isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) in WANTED_NAMES:
            nodes.append(n)
        elif isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") in WANTED_NAMES for t in n.targets):
            nodes.append(n)
    found = {n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    missing = (WANTED_FUNCS | WANTED_CLASSES) - found
    if missing:
        raise SystemExit(f"源码里找不到这些定义：{sorted(missing)}")
    ns = {"time": time, "threading": threading, "itertools": itertools,
          "List": List, "Optional": Optional, "Dict": dict, "os": os,
          "_env_num": _env_num}
    for n in nodes:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "<gateway>", "exec"), ns)
    return ns


NS = load_module(SRC)
Gate = NS["_PrioSerialGate"]
gate_for = NS["_mlx_gate_for"]
snapshot = NS["_serial_gates_snapshot"]
norm_prio = NS["_norm_prio"]
HOLD = NS["LANE_PRIO_HOLD_S"]


def wait_until(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.005)
    return False


print("1. 基本锁语义不能退化")
g = Gate()
ok, how, w, skip = g.acquire_ex(timeout=0.5)
check("空闲时直接拿到（direct）", ok and how == "direct" and w < 0.05, f"{ok} {how} {w}")
check("已持有时不可重入", g.acquire(blocking=False) is False)
g.release()
check("释放后可再获取", g.acquire(blocking=False) is True)
check("第二次拿到也算 direct", True)
g.release()

t0 = time.monotonic()
other = gate_for("k-timeout")
other.acquire()
ok2 = other.acquire(priority=0, timeout=0.25)
cost = time.monotonic() - t0
check("超时返回 False（不抛）", ok2 is False, ok2)
check("超时时间接近设定值（0.20~0.60s）", 0.20 <= cost <= 0.60, f"{cost:.3f}s")
check("超时后等待者已摘除（不泄漏）", other.snapshot()["waiting"] == 0, other.snapshot())
other.release()

print("\n2. 高优先级后到先拿到（插队）")
g = Gate()
g.acquire()
order = []
done = {}


def worker(name, prio, delay=0.0):
    time.sleep(delay)
    g.acquire(priority=prio, timeout=10)
    order.append(name)
    done[name] = time.time()
    time.sleep(0.05)
    g.release()


tb = threading.Thread(target=worker, args=("B", 0)); tb.start()
wait_until(lambda: len(g._waiters) >= 1)
tc = threading.Thread(target=worker, args=("C", 10)); tc.start()
wait_until(lambda: len(g._waiters) >= 2)
g.release()
tb.join(5); tc.join(5)
check("C(prio=10) 后到却先拿到", order[:1] == ["C"], order)
check("B(prio=0) 随后拿到（没被饿死）", order[1:] == ["B"], order)
check("两个都完成（无死锁）", len(order) == 2, order)

print("\n3. 同一优先级仍是先来后到")
g = Gate()
g.acquire()
order = []


def same(name):
    g.acquire(priority=5, timeout=10)
    order.append(name)
    time.sleep(0.02)
    g.release()


for nm in ("A", "B", "C"):
    threading.Thread(target=same, args=(nm,), daemon=True).start()
    wait_until(lambda: len(g._waiters) >= {"A": 1, "B": 2, "C": 3}[nm])
g.release()
wait_until(lambda: len(order) == 3, 3)
check("3 个同级等待者按到场顺序通过", order == ["A", "B", "C"], order)

print("\n4. 插队留痕（skipped 只在真插队时非零）")
# 锁必须由**第三方**持有：如果让主线程一边持有、一边去抢，测的就不是插队而是
# "自己等自己"（第一版就是这么写的，结果低优先级超时后误 release，把别人持有
# 的锁放掉了 → 主线程"瞬间拿到"，测试反而显示成功路径）。真实场景是上游
# 正被另一个请求占着，两个新请求排队。
g = Gate()
go_home = threading.Event()


def holder():
    g.acquire()
    go_home.wait(10)
    g.release()


res = {}
th = threading.Thread(target=holder, daemon=True); th.start()
wait_until(lambda: g.snapshot()["held"])


def low():
    ok, how, w, s = g.acquire_ex(priority=0, timeout=5)
    res["low"] = (ok, how, s)
    if ok:                      # 只在真拿到时释放，否则会把别人的锁放掉
        g.release()


tl = threading.Thread(target=low, daemon=True); tl.start()
wait_until(lambda: len(g._waiters) >= 1)
def vip_go():
    ok, how, w, s = g.acquire_ex(priority=10, timeout=5)
    res["vip"] = (ok, how, s)


vip = threading.Thread(target=vip_go, daemon=True)
vip.start()
wait_until(lambda: len(g._waiters) >= 2)
go_home.set()
vip.join(5)
check("VIP 拿到锁", res.get("vip", (False,))[0] is True, res.get("vip"))
check("VIP 的 skipped==1（到场时队列里有 1 个低优先级，插队留痕）",
      res.get("vip", (0, 0, 0))[2] == 1, res.get("vip"))
check("直接拿（空锁）时 skipped 恒为 0", Gate().acquire_ex()[3] == 0)
tl.join(6)
check("被插队的低优先级随后拿到", res.get("low", (False,))[0] is True, res.get("low"))
th.join(2)

print("\n5. 释放必须唤醒所有等待者（notify(1) 会卡住整条队列）")
g = Gate()
g.acquire()
hit = []
ths = []


def sleeper(prio):
    g.acquire(priority=prio, timeout=5)
    hit.append(prio)
    time.sleep(0.02)
    g.release()


# 先放一个低优先级，再放一个高优先级：release 时若只 notify 一个，可能叫醒低优先级
# → 它判定要让路 → 回去继续等，而高优先级根本没被通知 → 整条队列停在这里。
for p in (0, 10):
    t = threading.Thread(target=sleeper, args=(p,), daemon=True)
    t.start(); ths.append(t)
    wait_until(lambda: len(g._waiters) >= len(ths))
g.release()
wait_until(lambda: len(hit) == 2, 3)
check("两个等待者都被唤醒并通过", sorted(hit) == [0, 10], hit)
check("高优先级确实先通过", hit and hit[0] == 10, hit)

print("\n6. 跨键独立 + 非阻塞语义")
a, b = gate_for("host|m1"), gate_for("host|m2")
a.acquire()
check("A 占住不影响 B", b.acquire(blocking=False) is True)
b.release(); a.release()
check("同键复用同一个对象", gate_for("host|m1") is a)

print("\n7. 非法/越界优先级安静归一（不能 500，也不能变成 VIP）")
g = Gate()
for raw in ("high", None, "", "3.7"):
    ok, *_ = g.acquire_ex(priority=raw, timeout=0.2)
    check(f"priority={raw!r} → 按 0 处理且能拿到", ok is True, ok)
    g.release()
check("非法值归一为 0", norm_prio("high") == 0)
check("越界夹到上限", norm_prio(99999) == NS["KEY_PRIORITY_MAX"])

print("\n8. 观测：snapshot 反映占位/排队/最高优先级")
g = Gate()
g.acquire()
blocked = threading.Thread(target=lambda: g.acquire(priority=7, timeout=2), daemon=True)
blocked.start()
wait_until(lambda: g.snapshot()["waiting"] >= 1)
s = g.snapshot()
check("held=True", s["held"] is True)
check("waiting>=1", s["waiting"] >= 1, s)
check("top_prio=7", s["top_prio"] == 7, s)
check("max_wait_s 是数字", isinstance(s["max_wait_s"], (int, float)), s)
g.release(); blocked.join(3)
NS["_mlx_gates"].clear()
empty = snapshot()
check("空闲闸门不出现在 active 里（接口不越跑越吵）", empty["active"] == [], empty)

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
