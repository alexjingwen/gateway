#!/usr/bin/env python3
"""回归：串行锁的粒度 —— 同模型串行、异模型并行、探测失败退回 host 锁。

背景：
  gmlx 的 mid-flight 注入缺陷只在**同一个模型的 batch 内**触发
  （mrope 位置张量 (1,B,L) vs 目标 (3,B,L)，整批 ValueError）。实测：
    · 同一模型 2 并发  → 3 轮 6/6 全部 500（2.6~2.9s 快速失败）
    · 同一 gmlx 上两个不同模型并发 → 4/4 全 200，墙钟 5.72s < 串行 7.78s
  所以锁的粒度应当是「上游**真实驻留模型**」，但绝不能用请求里的名字：
  网关看到的是别名（qwen3.8-flash → qwen3.8-flash-125b → 上游 id
  qwen3.8-flash-next），按名字分会给同一个上游模型两把锁，照样混批。

期望：
  ① 探测失败 / 该 host 只有一个模型 → host 级锁（与串行化首版行为等价，保守）
  ② 多模型 host + 名字命中真实 id → host|model
  ③ 多模型 host + 名字对不上（别名/未知名）→ 退回 host 锁
  ④ 探测结果有 TTL 缓存，失败结果同样缓存（否则每个请求都吃一次 5s 超时）
  ⑤ 不同 model 的锁互不阻塞：一个被占用，另一个仍可立即获取
  ⑥ 同一 model 复用同一把锁，且不可重入

用法：
  python3 tests/test_serial_lock.py
环境变量：
  TG_SRC  被测网关文件，默认仓库根的 token-gateway.py
"""
import ast
import json
import os
import sys
import threading

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


def load_module(src_path):
    """从源码里抽真实定义（不是复制逻辑），注入桩函数后 exec。"""
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    wanted_funcs = {"_mlx_upstream_model_ids", "_mlx_lock_key", "_mlx_sem_for"}
    wanted_names = {"_MLX_UPSTREAM_IDS_TTL", "_mlx_upstream_ids", "_mlx_upstream_ids_lock",
                    "_mlx_probe_lock", "_mlx_serial_sems", "_mlx_serial_sems_lock"}
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in wanted_funcs:
            nodes.append(n)
        elif isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) in wanted_names:
            nodes.append(n)
        elif isinstance(n, ast.Assign) and any(getattr(t, "id", "") in wanted_names for t in n.targets):
            nodes.append(n)
    found = {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    missing = wanted_funcs - found
    if missing:
        raise SystemExit(f"源码里找不到这些定义：{sorted(missing)}")
    ns = {"json": json, "time": __import__("time"), "threading": threading, "Dict": dict}
    for n in nodes:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "<gateway>", "exec"), ns)
    return ns


NS = load_module(SRC)
CALLS = []
STATE = {"ids": [], "fail": False}


def fake_request(host, method, api_path, body=None, timeout=30):
    CALLS.append((host, method, api_path))
    if STATE["fail"]:
        return 0, b"unreachable"
    return 200, json.dumps({"data": [{"id": i} for i in STATE["ids"]]}).encode()


NS["_rapid_mlx_request"] = fake_request
lock_key = NS["_mlx_lock_key"]
sem_for = NS["_mlx_sem_for"]

H1 = "http://127.0.0.1:8090"
H2 = "http://127.0.0.1:8091"


def reset(ids, fail=False):
    NS["_mlx_upstream_ids"].clear()
    NS["_mlx_serial_sems"].clear()
    CALLS.clear()
    STATE["ids"] = list(ids)
    STATE["fail"] = fail


print("1. 探测失败 → host 级锁（保守）")
reset([], fail=True)
check("上游不可达时 lock_key == host", lock_key(H1, "anything") == H1, lock_key(H1, "anything"))
check("失败后再问仍返回 host（不抛异常）", lock_key(H1, "") == H1)
check("失败结果被缓存，TTL 内只探测一次", len(CALLS) == 1, CALLS)

print("\n2. 单模型 host → host 级锁（与首版行为等价）")
reset(["qwen3.8-flash-next"])
check("唯一模型请求 → host", lock_key(H1, "qwen3.8-flash-next") == H1, lock_key(H1, "qwen3.8-flash-next"))
check("别名请求 → 仍是 host", lock_key(H1, "qwen3.8-flash-125b") == H1)
check("空模型名 → 仍是 host", lock_key(H1, "") == H1)

print("\n3. 多模型 host → 按真实 id 分锁")
reset(["qwen3.8-flash-next", "qwen3.8-27b"])
k_a = lock_key(H1, "qwen3.8-flash-next")
k_b = lock_key(H1, "qwen3.8-27b")
check("模型 A → host|A", k_a == f"{H1}|qwen3.8-flash-next", k_a)
check("模型 B → host|B", k_b == f"{H1}|qwen3.8-27b", k_b)
check("A 与 B 是不同键", k_a != k_b)

print("\n4. 多模型 host + 名字对不上 → 退 host 锁（不吃两把锁）")
k_alias = lock_key(H1, "qwen3.8-flash-125b")
check("别名未命中 id 列表 → host", k_alias == H1, k_alias)
check("未知模型名 → host", lock_key(H1, "no-such-model") == H1)

print("\n5. TTL 缓存与刷新")
reset(["m1", "m2"])
lock_key(H1, "m1")
n1 = len(CALLS)
lock_key(H1, "m2")
check("TTL 内不重复探测", len(CALLS) == n1 == 1, f"calls={len(CALLS)}")
# 手动把缓存时间戳改老，模拟 TTL 过期
ts, ids = NS["_mlx_upstream_ids"][H1]
NS["_mlx_upstream_ids"][H1] = (ts - NS["_MLX_UPSTREAM_IDS_TTL"] - 1, ids)
lock_key(H1, "m1")
check("TTL 过期后重新探测", len(CALLS) == 2, f"calls={len(CALLS)}")

print("\n6. 跨 host 隔离")
reset(["m1", "m2"])
check("H1 与 H2 拿到不同键", lock_key(H1, "m1") != lock_key(H2, "m1"))

print("\n7. 信号量语义：同键复用、异键并行、不可重入")
reset(["m1", "m2"])
s_a1 = sem_for(lock_key(H1, "m1"))
s_a2 = sem_for(lock_key(H1, "m1"))
s_b = sem_for(lock_key(H1, "m2"))
check("同一键复用同一把锁", s_a1 is s_a2)
check("不同键拿到不同锁", s_a1 is not s_b)
s_a1.acquire()
check("A 占住后 A 不可重入", s_a1.acquire(blocking=False) is False)
check("A 占住时 B 仍可立即获取（异模型并行）", s_b.acquire(blocking=False) is True)
s_b.release()
s_a1.release()
check("释放后 A 可再次获取", s_a1.acquire(blocking=False) is True)
s_a1.release()

print("\n8. host 锁场景下的独占性（单模型 host 的等价性）")
reset(["only"])
s_h = sem_for(lock_key(H1, "only"))
s_h.acquire()
check("host 锁被占 → 同 host 任何请求都拿不到", lock_key(H1, "other") == H1 and s_h.acquire(blocking=False) is False)
s_h.release()

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
