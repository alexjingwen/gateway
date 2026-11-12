#!/usr/bin/env python3
"""回归：泳道池的「并发感知分派」—— 并行到底赚在哪里，机制就按那三条算。

背景（实测，见网关里「第二步」那段注释）：
  A. 跨后端并行免费：不同 host 各有批处理循环，两边合计 1.5x；
  B. 同后端合批有折扣：NUM_PARALLEL=2 时吞吐 1.44x，但每个请求变成 1.44x 时长；
  C. 超过上游并行度 = 排队，净亏：NUM_PARALLEL=1 却 cap=2，单请求 2.0x 时长、吞吐不涨。

于是这一组断言盯着四件事：
  ① 速度画像按**并发档位**分开学（spb_by_conc），预测按"我要加入的那一档"算；
  ② 同等速度下优先落到**没有别人在跑的 host**（先把 A 类收益吃满）；
  ③ 自动降档：某档实测膨胀 ≥1.75x（B/C 的分界线）就把有效容量压回去，冷却后试探恢复；
  ④ 观测字段（/api/admin/lanes）能直接看出"压到几路、为什么压"。

用法：
  python3 tests/test_lane_capacity.py
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


def load_module(src_path):
    """从源码里抽真实定义（不是复制逻辑），注入桩函数后 exec。"""
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    wanted_funcs = {"_parse_lanes", "_lane_context_key"}
    wanted_classes = {"LanePool", "LaneTimeout", "LaneQueueFull"}
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in wanted_funcs:
            nodes.append(n)
        elif isinstance(n, ast.ClassDef) and n.name in wanted_classes:
            nodes.append(n)
        elif isinstance(n, ast.Assign) and any(
                getattr(t, "id", "").startswith("LANE_") for t in n.targets):
            nodes.append(n)
    found = {n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    missing = (wanted_funcs | wanted_classes) - found
    if missing:
        raise SystemExit(f"源码里找不到这些定义：{sorted(missing)}")
    ns = {
        "json": json, "re": re, "sys": sys, "time": time, "threading": threading,
        "itertools": itertools, "hashlib": hashlib, "OrderedDict": OrderedDict,
        "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple,
        "os": os, "_env_num": _env_num,
        "RAPID_MLX_HOSTS": ["http://127.0.0.1:8090"],
        "log": lambda *a, **k: None,
    }
    for n in nodes:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "<gateway>", "exec"), ns)
    return ns


NS = load_module(SRC)
LanePool = NS["LanePool"]
LaneTimeout = NS["LaneTimeout"]


def lanes(*specs):
    return [dict(name=n, model=m, host=h, rank=r, cap=c) for n, m, h, r, c in specs]


def pool(*specs, **kw):
    kw.setdefault("wait_s", 2)
    kw.setdefault("prefer_wait_s", 0.0)     # 关掉"等最好那条"的预算，让预测说话
    kw.setdefault("default_spb_s", 0.05)
    return LanePool(lanes(*specs), **kw)


def teach(p, lane_name, elapsed, tokens=100, conc=1, times=1):
    """给某条泳道喂 N 个样本（acquire→release），用于把画像/膨胀倍数教出来。"""
    for _ in range(times):
        lane, _w = p.acquire(f"teach-{lane_name}-{time.time()}-{conc}", lane=lane_name)
        p.release(lane, tokens=tokens, elapsed=elapsed, conc=conc)


print("1. 速度画像按并发档位分开学（spb_by_conc）")
P = pool(("a", "m1", "http://h1", 100, 2))
A = P._by_name["a"]
c1, _ = P.acquire("ctx-1", est_tokens=100)
check("第一路加入时档位=1", P.admit_conc() == 1, P.admit_conc())
c2, _ = P.acquire("ctx-2", est_tokens=100)
check("第二路加入时档位=2（与另一单共享上游）", P.admit_conc() == 2, P.admit_conc())
P.release(c2, tokens=100, elapsed=4.0, conc=2)     # 0.04 s/tok（合批后变慢）
P.release(c1, tokens=100, elapsed=2.0, conc=1)     # 0.02 s/tok（独占全速）
check("1 档学成 0.02", abs(P.spb_at(A, 1) - 0.02) < 1e-9, P.spb_at(A, 1))
check("2 档学成 0.04", abs(P.spb_at(A, 2) - 0.04) < 1e-9, P.spb_at(A, 2))
check("spb_of 仍等于 1 档（对外语义不变）", abs(P.spb_of(A) - 0.02) < 1e-9, P.spb_of(A))
check("没测过的更高档用已知最高档兜底（不会乐观到用 1 档）",
      abs(P.spb_at(A, 3) - 0.04) < 1e-9, P.spb_at(A, 3))
check("两档样本分别计数", A["spb_conc_n"] == {1: 1, 2: 1}, A["spb_conc_n"])
check("release 用掉即清档位，同一线程第二单不会沿用",
      P.admit_conc() == 1, P.admit_conc())

P2 = pool(("b", "m2", "http://h1", 100, 2))
B = P2._by_name["b"]
check("还没有任何样本时退回种子速度", abs(P2.spb_at(B, 1) - 0.05) < 1e-9, P2.spb_at(B, 1))

print("\n2. 预测用「我要加入的那一档」，不是单请求速度")
P = pool(("a", "m1", "http://h1", 100, 2))
A = P._by_name["a"]
teach(P, "a", 2.0, conc=1, times=2)
teach(P, "a", 4.0, conc=2, times=1)
now = time.time()
check("空泳道 → 按 1 档算（2.0s / 100 tok）",
      abs(P._pred(A, now, 100) - 2.0) < 0.05, P._pred(A, now, 100))
c1, _ = P.acquire("ctx-1", est_tokens=100)
check("已经跑 1 单时 → 按 2 档算（4.0s / 100 tok，而不是 2.0s）",
      abs(P._pred(A, time.time(), 100) - 4.0) < 0.05, P._pred(A, time.time(), 100))
c2, _ = P.acquire("ctx-2", est_tokens=100)
check("第一单按 1 档记占用（它是独占开跑的那一路）",
      abs(min(A["job_ends"]) - (time.time() + 2.0)) < 0.2, A["job_ends"])
check("第二单按 2 档记占用（否则排队者以为它马上空）",
      abs(max(A["job_ends"]) - (time.time() + 4.0)) < 0.2, A["job_ends"])
P.release(c2, conc=2)
P.release(c1, conc=1)

print("\n3. 同等速度下先落到「没有别人在跑的 host」（A 类免费并行）")
P = pool(("h1a", "m1", "http://h1", 100, 2), ("h2", "m1", "http://h2", 10, 1))
x, _ = P.acquire("ctx-1", est_tokens=10)          # rank 最高 → h1a
check("第一单进高优先级的 h1a", x["name"] == "h1a", x["name"])
check("host 负载统计：h1 有 1 路、h2 空着",
      P._host_load() == {"http://h1": 1, "http://h2": 0}, P._host_load())
y, _ = P.acquire("ctx-2", est_tokens=10)
check("第二单没有挤进 h1 的第二槽，而是落到空闲的 h2",
      y["name"] == "h2", y["name"])
P.release(x)
P.release(y)

print("\n4. 同一条 host 上：宁可去独占泳道，也不挤进合批档")
P = pool(("wide", "m1", "http://h1", 100, 2), ("narrow", "m1", "http://h1", 50, 1))
W, N = P._by_name["wide"], P._by_name["narrow"]
teach(P, "wide", 2.0, conc=1, times=2)     # wide 1 档 0.02
teach(P, "wide", 4.0, conc=2, times=1)     # wide 2 档 0.04
teach(P, "narrow", 3.0, conc=1, times=2)   # narrow 0.03
w, _ = P.acquire("ctx-1", est_tokens=100)
check("第一单进 wide（1 档最快）", w["name"] == "wide", w["name"])
now = time.time()
check("挤进 wide 要 4.0s，去 narrow 只要 3.0s",
      P._pred(W, now, 100) > P._pred(N, now, 100), (P._pred(W, now, 100), P._pred(N, now, 100)))
check("若只看单请求速度会误判（2.0 < 3.0，正是老实现的选择）",
      P.spb_at(W, 1) * 100 < P.spb_at(N, 1) * 100)
n, _ = P.acquire("ctx-2", est_tokens=100)
check("第二单去了 narrow（真并行），而不是 wide 的第二槽", n["name"] == "narrow", n["name"])
P.release(w)
P.release(n)

print("\n5. 自动降档：并发档实测慢 ≥1.75x → 判定上游在排队，压回有效容量")
_COOL = NS["LANE_AUTOCAP_COOLDOWN_S"]
NS["LANE_AUTOCAP_COOLDOWN_S"] = 60


def conc_round(p, elapsed_conc2, ctx=""):
    """一次"两路并发"的往返：占满 2 路 → 按各自档位归还样本。"""
    a1, _ = p.acquire(f"x{ctx}{time.time()}", est_tokens=100)
    a2, _ = p.acquire(f"y{ctx}{time.time()}", est_tokens=100)
    p.release(a2, tokens=100, elapsed=elapsed_conc2, conc=2)
    p.release(a1, tokens=100, elapsed=2.0, conc=1)


P = pool(("a", "m1", "http://h1", 100, 2))
teach(P, "a", 2.0, conc=1, times=2)
conc_round(P, 4.0, "r1")
check("只有 1 个并发样本时不动手（要 EWMA + 连续样本数）",
      P._by_name["a"]["eff_cap"] == 2, P._by_name["a"]["eff_cap"])
conc_round(P, 4.0, "r2")
check("2 个样本也还不够（默认要 3 个）", P._by_name["a"]["eff_cap"] == 2)
conc_round(P, 4.0, "r3")
A = P._by_name["a"]
check("2.0x 膨胀 → 有效容量 2→1", A["eff_cap"] == 1, A["eff_cap"])
check("配置容量保持不变（降的是有效容量，随时可试探恢复）", A["cap"] == 2, A["cap"])
check("原因写清了证据", "conc2" in A["cap_reason"], A["cap_reason"])
check("降档被计数", P.snapshot()["metrics"]["autocap_down"] == 1, P.snapshot()["metrics"])
check("冷却期已设", A["cap_down_until"] > time.time(), A["cap_down_until"])
k1, _ = P.acquire("k1", est_tokens=100)
try:
    P.acquire("k2", est_tokens=100, timeout=0.2)
    check("降到 1 路后第二个请求应排队", False)
except LaneTimeout:
    check("降到 1 路后第二个请求排队（不再假并行）", True)
P.release(k1)

print("\n6. 合批（1.44x）不该被降档")
P = pool(("a", "m1", "http://h1", 100, 2))
teach(P, "a", 2.0, conc=1, times=2)
for i in range(4):
    conc_round(P, 2.88, f"b{i}")                      # 1.44x：合批，净赚
check("1.44x 在阈值(1.75x)以下 → 保持 2 路", P._by_name["a"]["eff_cap"] == 2,
      P._by_name["a"]["eff_cap"])
check("没有误触发降档", P.snapshot()["metrics"]["autocap_down"] == 0)

print("\n7. 比不出倍数时不动容量")
P = pool(("a", "m1", "http://h1", 100, 2))
for _ in range(3):
    P.release(P.acquire(f"z{time.time()}", est_tokens=100)[0],
              tokens=100, elapsed=4.0, conc=2)     # 只有 2 档样本，没有 1 档基线
check("没有单档基线 → 不降档（2.0x 只是猜测，不是证据）",
      P._by_name["a"]["eff_cap"] == 2, P._by_name["a"]["eff_cap"])
check("也不记膨胀倍数", P._by_name["a"]["ratio_by_conc"] == {}, P._by_name["a"]["ratio_by_conc"])

print("\n8. 开关：TG_LANE_AUTOCAP=0 时只学画像、不动容量")
NS["LANE_AUTOCAP"] = False
P = pool(("a", "m1", "http://h1", 100, 2))
teach(P, "a", 2.0, conc=1, times=2)
for i in range(3):
    conc_round(P, 4.0, f"o{i}")
check("关掉后仍保留 2 路", P._by_name["a"]["eff_cap"] == 2, P._by_name["a"]["eff_cap"])
NS["LANE_AUTOCAP"] = True

print("\n9. 冷却到期自动试探恢复；再挤就再降（冷却翻倍）")
NS["LANE_AUTOCAP_COOLDOWN_S"] = 0.2
P = pool(("a", "m1", "http://h1", 100, 2))
teach(P, "a", 2.0, conc=1, times=2)
for i in range(3):
    conc_round(P, 4.0, f"t{i}")
A = P._by_name["a"]
check("先压到 1 路", A["eff_cap"] == 1, A["eff_cap"])
time.sleep(0.25)
snap = P.snapshot()
la = snap["lanes"][0]
check("冷却到期 → 试探恢复 2 路", la["eff_cap"] == 2, la["eff_cap"])
check("reason 标成 trial（一看就知道这是试探，不是结论）", la["cap_reason"] == "trial", la["cap_reason"])
check("试探被计数", snap["metrics"]["autocap_trials"] == 1, snap["metrics"])
for i in range(3):
    conc_round(P, 4.0, f"u{i}")
check("还在排队 → 再压回 1 路", A["eff_cap"] == 1, A["eff_cap"])
check("strikes 递增（冷却按 2x 翻倍，避免对一条烂泳道反复试探）",
      A["cap_down_strikes"] == 2, A["cap_down_strikes"])
check("第二次冷却 ≈0.4s", 0.2 < A["cap_down_until"] - time.time() <= 0.45,
      A["cap_down_until"] - time.time())
NS["LANE_AUTOCAP_COOLDOWN_S"] = _COOL

print("\n10. 观测契约（/api/admin/lanes）")
P = pool(("a", "m1", "http://h1", 100, 2))
teach(P, "a", 2.0, conc=1, times=2)
teach(P, "a", 4.0, conc=2, times=1)
snap = P.snapshot()
lane_need = {"eff_cap", "cap_reason", "cap_down_for_s", "spb_by_conc", "conc_ratio"}
check("泳道新增字段齐", lane_need <= set(snap["lanes"][0]), lane_need - set(snap["lanes"][0]))
check("spb_by_conc 以字符串键输出（JSON 友好）",
      snap["lanes"][0]["spb_by_conc"].get("2") == 0.04, snap["lanes"][0]["spb_by_conc"])
check("conc_ratio 有 2 档的膨胀倍数", snap["lanes"][0]["conc_ratio"].get("2") == 2.0,
      snap["lanes"][0]["conc_ratio"])
check("顶层带 autocap 配置块（调参不用翻源码）",
      {"on", "ratio", "samples", "cooldown_s"} <= set(snap["autocap"]), snap.get("autocap"))
check("eff_cap 未降时等于 cap", snap["lanes"][0]["eff_cap"] == snap["lanes"][0]["cap"])

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
