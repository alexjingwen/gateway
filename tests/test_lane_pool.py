#!/usr/bin/env python3
"""回归：多模型泳道池（LanePool）—— 一个模型一个上下文，多的排队。

设计前提（见网关里「多模型泳道」那段注释）：
  · 125B 带 MTP，同模型两并发 100% 双双 500 → 一个模型同一时刻只能跑一个请求；
  · 27B/9B 不带投机可以并发，但同模型共享 BatchGenerator，n=4 只有 1.65x / 1.99x；
  · 不同模型各有自己的 BatchGenerator → "模型之间并行"才是真并行。
三条不变量：
  ① 粘性：同一上下文固定同一个模型（否则 APC 前缀缓存全落空，25s vs 0.2s）；
  ② 独占：一个模型同时只服务一个上下文（cap 默认 1）；
  ③ 排队：全忙时等待，不是 429；只有超时/队满才拒。

用法：
  python3 tests/test_lane_pool.py
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
LaneQueueFull = NS["LaneQueueFull"]
parse_lanes = NS["_parse_lanes"]
ctx_key = NS["_lane_context_key"]


class Hdrs(dict):
    """最小 headers 桩：大小写不敏感，和 http.client 的 HTTPMessage 一致。"""

    def get(self, k, default=None):
        return super().get(k.lower(), default)

    def __setitem__(self, k, v):
        super().__setitem__(k.lower(), v)


def lanes(*specs):
    return [dict(name=n, model=m, host=h, rank=r, cap=c) for n, m, h, r, c in specs]


def pool(*specs, **kw):
    kw.setdefault("wait_s", 5)
    return LanePool(lanes(*specs), **kw)


L1 = ("flash", "qwen3.8-flash-next", "http://127.0.0.1:8090", 100, 1)
L2 = ("mid", "qwen3.8-27b-mlx", "http://127.0.0.1:8090", 50, 1)
L3 = ("light", "gemma4:latest", "http://127.0.0.1:8090", 10, 1)

print("1. TG_LANES 解析：紧凑写法")
p = parse_lanes("flash=qwen3.8-flash-next|100|1,mid=qwen3.8-27b-mlx|50|1")
check("两条泳道", len(p) == 2, p)
check("名字/模型/优先级/容量都对",
      (p[0]["name"], p[0]["model"], p[0]["rank"], p[0]["cap"]) == ("flash", "qwen3.8-flash-next", 100, 1), p[0])
check("模型名里的冒号不会被当分隔符（gemma4:latest）",
      parse_lanes("light=gemma4:latest|10")[0]["model"] == "gemma4:latest")
check("默认容量 1", parse_lanes("x=m1")[0]["cap"] == 1)
check("省略名字时名字=模型", parse_lanes("m1|7")[0]["name"] == "m1")
check("@host 可指定上游", parse_lanes("a=m1@http://127.0.0.1:8091")[0]["host"] == "http://127.0.0.1:8091")
check("空串 → 不启用", parse_lanes("") == [])
check("JSON 写法同样支持",
      parse_lanes('[{"name":"a","model":"m1","rank":9,"cap":2}]')[0] ==
      {"name": "a", "model": "m1", "host": "", "rank": 9, "cap": 2})

print("\n2. 上下文标识：显式头 > 请求体字段 > 内容指纹")
h = Hdrs({"x-session-id": "sess-1"})
check("优先用 x-session-id", ctx_key(h, {"messages": []}) == "h:sess-1")
check("x-context-id 次之", ctx_key(Hdrs({"x-context-id": "c9"}), {}) == "h:c9")
check("请求体 conversation_id", ctx_key(Hdrs(), {"conversation_id": "conv-7"}) == "b:conversation_id:conv-7")
check("请求体 user", ctx_key(Hdrs(), {"user": "alice"}) == "b:user:alice")
base = {"messages": [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "第一条"}]}
k1 = ctx_key(Hdrs(), json.loads(json.dumps(base)))
k2 = ctx_key(Hdrs(), {"messages": base["messages"] + [
    {"role": "assistant", "content": "回一句"}, {"role": "user", "content": "第二条"}]})
check("同一对话多轮 → 指纹不变（只取前两条）", k1 == k2 and k1.startswith("fp-"), (k1, k2))
k3 = ctx_key(Hdrs(), {"messages": [base["messages"][0], {"role": "user", "content": "别的问题"}]})
check("换了首次提问 → 指纹不同", k1 != k3)
k4 = ctx_key(Hdrs(), {"messages": [{"role": "system", "content": "你是助手"},
                                   {"role": "user", "content": [{"type": "text", "text": "第一条"}]}]})
check("多模态 content 数组按文本折算，与纯文本同指纹", k4 == k1, (k4, k1))

print("\n3. 优先级：空闲时先给优先级高的模型")
P = pool(L1, L2, L3)
la, w0 = P.acquire("ctx-a")
check("第一个上下文进最高优先级泳道", la["name"] == "flash", la["name"])
check("无需等待", w0 < 0.05, w0)
lb, _ = P.acquire("ctx-b")
check("第二个进次高优先级", lb["name"] == "mid", lb["name"])
lc, _ = P.acquire("ctx-c")
check("第三个进最低优先级", lc["name"] == "light", lc["name"])
for x in (la, lb, lc):
    P.release(x)

print("\n4. 粘性：同一上下文固定同一个模型，即使有空出来更好的")
P = pool(L1, L2)
a1, _ = P.acquire("ctx-a")          # → flash
P.release(a1)
b1, _ = P.acquire("ctx-b")          # flash 空着，b 也进来
check("ctx-b 此刻拿到 flash", b1["name"] == "flash")
P.release(b1)
a2, _ = P.acquire("ctx-a")          # a 记得自己用过 flash
check("ctx-a 仍回 flash（粘性）", a2["name"] == "flash", a2["name"])
P.release(a2)
P2 = pool(L1, L2, sticky=False)
x1, _ = P2.acquire("ctx-x")
P2.release(x1)
y1, _ = P2.acquire("ctx-y")
P2.release(y1)
check("sticky=False 时不做绑定（快照里上下文数为 0）", P2.snapshot()["contexts"] == 0)

print("\n5. 独占 + 排队：全忙时等待，释放后立刻接手")
P = pool(L1, L2)
a, _ = P.acquire("A")
b, _ = P.acquire("B")
check("两条泳道都占满", P.snapshot()["lanes"][0]["busy"] == 1 and P.snapshot()["lanes"][1]["busy"] == 1)
result = {}


def waiter():
    t0 = time.time()
    try:
        lane, w = P.acquire("C", timeout=5)
        result["lane"] = lane["name"]
        result["waited"] = time.time() - t0
        P.release(lane)
    except Exception as e:                       # noqa: BLE001
        result["err"] = repr(e)


th = threading.Thread(target=waiter)
th.start()
time.sleep(0.25)
check("第三个上下文确实在排队（线程未完成）", th.is_alive())
check("快照里能看见 1 个等待者", P.snapshot()["waiting"] == 1, P.snapshot()["waiting"])
P.release(b)
th.join(5)
check("释放一个模型后排队者被唤醒", result.get("lane") in ("flash", "mid"), result)
check("等待时间被如实记录（>0.2s）", result.get("waited", 0) > 0.2, result)
P.release(a)

print("\n6. 同一上下文不会被并发塞进两个模型")
P = pool(L1, L2)
a, _ = P.acquire("A")
res = {}


def same_ctx():
    try:
        lane, _w = P.acquire("A", timeout=1)
        res["lane"] = lane["name"]
        P.release(lane)
    except LaneTimeout:
        res["timeout"] = True


th = threading.Thread(target=same_ctx)
th.start()
th.join(3)
check("同上下文的第二个请求排队到超时，而不是换模型", res.get("timeout") is True, res)
P.release(a)
th2 = threading.Thread(target=same_ctx)
th2.start()
th2.join(3)
check("原模型空出来后，同一上下文又回到它", res.get("lane") == "flash", res)

print("\n7. 指定泳道、未知泳道、队满、超时")
P = pool(L1, L2)
check("不存在泳道 → 选择返回 None，调用方据此报 400", "nope" not in P._by_name)
P.release(P.acquire("A", lane="light")[0]) if False else None
P = pool(L1, L2)
a, _ = P.acquire("A", lane="mid")
check("按名字强制指定泳道", a["name"] == "mid", a["name"])
P.release(a)
P = LanePool(lanes(*[L1, L2]), wait_s=0.2, queue_max=1)
a, _ = P.acquire("A")
b, _ = P.acquire("B")
try:
    P.acquire("C", timeout=0.2)
    check("超时应抛 LaneTimeout", False)
except LaneTimeout:
    check("超时抛 LaneTimeout（调用方回 429 lane_wait_timeout）", True)
P.release(a)
P.release(b)
P = LanePool(lanes(*[L1, L2]), wait_s=5, queue_max=1)
a, _ = P.acquire("A")
b, _ = P.acquire("B")
t = threading.Thread(target=lambda: P.acquire("C", timeout=3))
t.start()
time.sleep(0.15)
try:
    P.acquire("D", timeout=3)
    check("队满应抛 LaneQueueFull", False)
except LaneQueueFull:
    check("队满抛 LaneQueueFull（调用方回 429 lane_queue_full）", True)
P.release(a)
P.release(b)
t.join(3)

print("\n8. 故障隔离：连续失败 → 隔离该模型，上下文改派")
P = pool(L1, L2, fail_limit=2, cooldown_s=0.3)
a, _ = P.acquire("A")
P.release(a, error=True)
a, _ = P.acquire("A")
check("失败一次还不隔离", P.snapshot()["lanes"][0]["down_for_s"] == 0)
P.release(a, error=True)
snap = P.snapshot()
check("连续两次失败 → 该泳道被隔离", snap["lanes"][0]["down_for_s"] > 0, snap["lanes"][0])
check("被隔离模型的上下文绑定被清掉（可改派）", snap["contexts"] == 0, snap["contexts"])
c, _ = P.acquire("A")
check("隔离期间新请求落到另一个模型", c["name"] == "mid", c["name"])
P.release(c)
time.sleep(0.35)
P2 = pool(L1, L2, fail_limit=2, cooldown_s=0.1)
for _ in range(2):
    P2.release(P2.acquire("Z")[0], error=True)
time.sleep(0.15)
check("冷却结束后重新可用", P2.snapshot()["lanes"][0]["down_for_s"] == 0)

print("\n9. 容量 > 1：同一模型可以同时服务两个上下文（cap=2）")
P = pool(("big", "m1", "", 10, 2))
a, _ = P.acquire("A")
b, _ = P.acquire("B")
check("cap=2 时第二也能进", b["name"] == "big" and P.snapshot()["lanes"][0]["busy"] == 2)
try:
    P.acquire("C", timeout=0.2)
    check("超过 cap 应排队", False)
except LaneTimeout:
    check("超过 cap 才排队", True)
P.release(a)
P.release(b)

print("\n10. 快照字段（/api/admin/lanes 的契约）")
P = pool(L1, L2)
P.release(P.acquire("A")[0])
snap = P.snapshot()
need = {"pool_model", "enabled", "wait_s", "queue_max", "sticky", "sticky_ttl_s",
        "waiting", "contexts", "lanes", "metrics"}
check("顶层字段齐", need <= set(snap), need - set(snap))
lane_need = {"name", "model", "host", "rank", "cap", "busy", "served", "failed",
             "current_context", "avg_wait_s", "max_wait_s", "down_for_s", "idle_for_s"}
check("泳道字段齐", lane_need <= set(snap["lanes"][0]), lane_need - set(snap["lanes"][0]))
check("served 计数正确", snap["lanes"][0]["served"] == 1, snap["lanes"][0]["served"])
check("metrics 计数器存在", {"assigns", "queued", "timeouts", "queue_full"} <= set(snap["metrics"]))

print("\n11. 新上下文的「看一眼」预算：好模型正在收尾时不要立刻降级")
P = lane_pool_with_prefer = LanePool(lanes(*[L1, L2]), wait_s=3, prefer_wait_s=0.3,
                                     default_spb_s=0.05)
a, _ = P.acquire("A", est_tokens=6)      # flash 被占住（预计 0.3s 做完）
res = {}


def late_new_ctx():
    t0 = time.time()
    lane, w = P.acquire("B", est_tokens=6)
    res["lane"] = lane["name"]
    res["took"] = time.time() - t0
    P.release(lane)


th = threading.Thread(target=late_new_ctx)
th.start()
time.sleep(0.12)
P.release(a)                             # 0.12s 后 flash 空出来（模拟服务端 finally 收尾）
th.join(3)
check("B 等到了 flash 而不是立刻降级到 mid", res.get("lane") == "flash", res)
check("等待时间在预算内（<0.3s 多一点）", res.get("took", 9) < 0.6, res)

P = LanePool(lanes(*[L1, L2]), wait_s=3, prefer_wait_s=0.2, default_spb_s=0.05)
a, _ = P.acquire("A", est_tokens=6)
t0 = time.time()
b, _ = P.acquire("B", est_tokens=6)       # flash 一直忙 → 预算用完后落到 mid
took = time.time() - t0
check("预算用完就落到次优模型（不死等）", b["name"] == "mid", b["name"])
check("降级耗时约等于预算（<0.5s）", took < 0.5, took)
P.release(a)
P.release(b)

print("\n12. 粘性改派预算：默认关闭（严格粘性），开了才有队头绕行")
P = LanePool(lanes(*[L1, L2]), wait_s=0.3, sticky_wait_s=0)
a, _ = P.acquire("A")
P.release(a)
z, _ = P.acquire("Z")                     # 占住 flash
try:
    P.acquire("A", timeout=0.3)
    check("严格粘性下 A 宁可超时也不换模型", False)
except LaneTimeout:
    check("严格粘性下 A 宁可超时也不换模型", True)
P.release(z)
P = LanePool(lanes(*[L1, L2]), wait_s=5, sticky_wait_s=0.2)
a, _ = P.acquire("A")
P.release(a)
z, _ = P.acquire("Z")
t0 = time.time()
a2, _ = P.acquire("A")
took = time.time() - t0
check("开了预算后 A 改派到空闲模型", a2["name"] == "mid", a2["name"])
check("改派耗时约等于预算", 0.15 < took < 0.9, took)
check("改派被计数", P.snapshot()["metrics"].get("migrations") == 1)
P.release(a2)
P.release(z)

print("\n13. 速度画像：慢模型只在「它那儿先做完」时才被用")
P = LanePool(lanes(*[L1, L2]), wait_s=3, prefer_wait_s=0.0, default_spb_s=0.05)
P._by_name["flash"]["spb"] = 0.02        # 125B：50 tok/s
P._by_name["mid"]["spb"] = 0.10          # 27B：10 tok/s（故意放大差距）
res = {}
a, _ = P.acquire("A", est_tokens=100)     # flash 预计 2s
check("A 进快模型", a["name"] == "flash", a["name"])


def queued_b():
    t0 = time.time()
    lane, _w = P.acquire("B", est_tokens=100)
    res["lane"] = lane["name"]
    res["took"] = time.time() - t0
    P.release(lane)


th = threading.Thread(target=queued_b)
th.start()
time.sleep(0.3)
check("B 没有因为 mid 闲着就跑去慢模型（还在等 flash）", th.is_alive(), res)
P.release(a)
th.join(5)
check("flash 空出来后 B 拿到它", res.get("lane") == "flash", res)

# flash 积压很久（预计 12s 后才轮到）→ 这时用 mid（10s）反而更快
a, _ = P.acquire("A", est_tokens=100)
P.release(a)
a, _ = P.acquire("A", est_tokens=100)          # 占住 flash（粘性：A 一直用 flash）
P._by_name["flash"]["job_ends"] = [time.time() + 12.0]   # 模拟前面排了 6 个请求
# 用**新上下文 C**：粘性上下文不会为了速度换模型，只有新来的才做这个权衡
b, w = P.acquire("C", est_tokens=100)
check("flash 积压很深时新上下文改派到 mid", b["name"] == "mid", b["name"])
check("改派没有等待", w < 0.2, w)
P.release(a)
P.release(b)

print("\n14. 速度画像会从实测里学（observe）")
P = LanePool(lanes(*[L1, L2]), wait_s=3, default_spb_s=0.05)
a, _ = P.acquire("A", est_tokens=100)
P.release(a, tokens=100, elapsed=5.0)
check("100 token / 5s → spb 学到 0.05", abs(P.spb_of(a) - 0.05) < 1e-6, P.spb_of(a))
check("样本被计数", a["spb_n"] == 1, a["spb_n"])
a, _ = P.acquire("B", est_tokens=100)
P.release(a, tokens=100, elapsed=1.0)
check("第二单把 spb 往下拉（EWMA 0.7/0.3）",
      0.01 < P.spb_of(a) < 0.05, P.spb_of(a))
a, _ = P.acquire("C", est_tokens=100)
P.release(a, tokens=2, elapsed=9.0)
check("样本太短（<32 token）不参与学习", a["spb_n"] == 2, a["spb_n"])

# 真实踩到的一例：工具调用请求（长 schema 的 prefill + 很短正文）曾把 125B 的
# spb 学成 0.0948（≈10 tok/s，真值 98），单样本偏差 10 倍，要 5 个正常样本才纠回来。
a, _ = P.acquire("D", est_tokens=100)
P.release(a, tokens=20, elapsed=1.0)
check("工具调用式的短样本（20 token / 1s = 0.05 s/tok 的假信号）也不学",
      a["spb_n"] == 2, a["spb_n"])
_before = P.spb_of(a)
a, _ = P.acquire("E", est_tokens=100)
P.release(a, tokens=64, elapsed=200.0)       # 冷 prefill 的 20k 上下文：3.1 s/tok
check("离谱样本被夹到 0.2 s/tok（而不是 3.1）", P.spb_of(a) <= 0.2 + 1e-9, P.spb_of(a))
check("夹断后仍然往上偏（说明真的学到了「这条慢」）", P.spb_of(a) > _before, (_before, P.spb_of(a)))

print("\n15. 粘性 TTL：过期后允许换模型")
P = LanePool(lanes(*[L1, L2]), wait_s=5, sticky=True, sticky_ttl_s=0.2)
P.sticky_ttl_s = 0.2
a, _ = P.acquire("A")
P.release(a)
check("刚用完还绑着", P._ctx.get("A")[0] == "flash")
time.sleep(0.3)
b, _ = P.acquire("B")     # 占掉 flash
P.release(b)
a2, _ = P.acquire("A")    # 绑定过期 → 可以换到 mid
check("TTL 过期后上下文可改派（不再死等旧模型）", a2["name"] in ("flash", "mid"), a2["name"])
P.release(a2)

print("\n16. Ollama 泳道保活：只给「用过的、空闲的、非 MLX」的续命")
OLL = ("gptoss", "gpt-oss:20b", "http://127.0.0.1:11434", 95, 1)
P = pool(*[L1, OLL])
t0 = time.time()
check("从没接过单的泳道不保活（不为一条可能不来的请求预占 13GB）",
      P.keepalive_targets(t0, 240) == [], P.keepalive_targets(t0, 240))
a, _ = P.acquire("A")                 # 走 flash（rank 高，速度未知时按 rank）
P.release(a)
b, _ = P.acquire("B", lane="gptoss")
P.release(b)
tg = P.keepalive_targets(t0, 240)
check("只拍 Ollama 那条（MLX 侧靠 pin 常驻，不该被 /api/generate 打扰）",
      [l["name"] for l in tg] == ["gptoss"], [l["name"] for l in tg])
check("间隔未到不重复拍", P.keepalive_targets(t0 + 10, 240) == [])
check("间隔到了会再拍",
      [l["name"] for l in P.keepalive_targets(t0 + 300, 240)] == ["gptoss"])
check("间隔 0 = 关闭保活", P.keepalive_targets(t0 + 9999, 0) == [])

P2 = pool(*[L1, OLL])
c, _ = P2.acquire("C", lane="gptoss")     # 不 release：一直忙
check("正在跑的泳道不拍（本来就热）",
      P2.keepalive_targets(time.time() + 300, 240) == [])
P2.release(c)

P3 = pool(*[L1, OLL])
d, _ = P3.acquire("D", lane="gptoss")
P3.release(d)
P3._by_name["gptoss"]["down_until"] = time.time() + 60
check("隔离中的泳道不拍（别往正在报错的模型上加压）",
      P3.keepalive_targets(time.time(), 240) == [])
P3.note_ping("gptoss", True)
P3.note_ping("gptoss", False)
P3.note_ping("no-such-lane", True)        # 不该炸
check("击活记账（供 /api/admin/lanes 观测）",
      (P3._by_name["gptoss"]["ping_ok"], P3._by_name["gptoss"]["ping_fail"]) == (1, 1),
      P3._by_name["gptoss"])

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
