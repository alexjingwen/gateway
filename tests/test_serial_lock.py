#!/usr/bin/env python3
"""回归：串行锁的**范围** —— 只串行带投机的模型，其余放行并发。

背景（2026-11-11 两次实测）：
  gmlx 的 mid-flight 注入缺陷只在**带投机解码（MTP）**的模型上触发
  （mrope 位置张量 (1,B,L) vs 目标 (3,B,L)，整批 ValueError）：
    · qwen3.8-flash-next（speculative=true）两并发 → 3 轮 6/6 全部 500
  **不带投机**的模型走不到那段代码，实测可安全批处理（同一 gmlx 单进程，cache 关闭）：
    · gemma4-9B  同模型 4 并发 ×4 轮：输出与串行基准**逐字节一致**，0 错误；
      agg 109 → 216 tok/s（n=4），单请求 decode 112 → 57 tok/s
    · qwen3.8-27B 同模型 4 并发 ×4 轮：同上；agg 32 → 53 tok/s
所以锁跟着 `speculative` 走，而不是跟着 host 走 —— 否则小模型白白损失 2 倍吞吐。

期望：
  ① 探测失败 → host 级锁（保守，等价于最早的行为）
  ② speculative=true  → host|model
  ③ speculative=false → 空串（调用方据此跳过排队）
  ④ 名字对不上（别名/未知名）→ 退回 host 锁（不知道是谁就不放行）
  ⑤ 字段缺失按 True（保守）
  ⑥ 探测结果 TTL 缓存，失败结果同样缓存
  ⑦ TG_MLX_SERIAL_MODE=model / host 可回退旧行为
  ⑧ 不同键的锁互不阻塞；同键复用且不可重入

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
    wanted_funcs = {"_mlx_upstream_models", "_mlx_lock_key", "_mlx_sem_for"}
    wanted_names = {"_MLX_UPSTREAM_IDS_TTL", "_mlx_upstream_ids", "_mlx_upstream_ids_lock",
                    "_mlx_probe_lock", "_mlx_serial_sems", "_mlx_serial_sems_lock",
                    "MLX_SERIAL_MODE"}
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
    ns = {"json": json, "time": __import__("time"), "threading": threading, "Dict": dict,
          "os": os}
    for n in nodes:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "<gateway>", "exec"), ns)
    if "MLX_SERIAL_MODE" not in ns:
        ns["MLX_SERIAL_MODE"] = "spec"
    return ns


NS = load_module(SRC)
with open(SRC, encoding="utf-8") as fh:
    SRC_TEXT = fh.read()
CALLS = []
STATE = {"models": {}, "fail": False}


def fake_request(host, method, api_path, body=None, timeout=30):
    CALLS.append((host, method, api_path))
    if STATE["fail"]:
        return 0, b"unreachable"
    data = []
    for mid, item in STATE["models"].items():
        row = {"id": mid}
        if item is not None:                  # None = 字段缺失，测「保守默认」
            row["speculative"] = item
        data.append(row)
    return 200, json.dumps({"data": data}).encode()


NS["_rapid_mlx_request"] = fake_request
lock_key = NS["_mlx_lock_key"]
sem_for = NS["_mlx_sem_for"]

H1 = "http://127.0.0.1:8090"
H2 = "http://127.0.0.1:8091"


def reset(models, fail=False, mode="spec"):
    NS["_mlx_upstream_ids"].clear()
    NS["_mlx_serial_sems"].clear()
    NS["MLX_SERIAL_MODE"] = mode
    CALLS.clear()
    STATE["models"] = dict(models)
    STATE["fail"] = fail


print("1. 探测失败 → host 级锁（保守）")
reset({}, fail=True)
check("上游不可达时 lock_key == host", lock_key(H1, "anything") == H1, lock_key(H1, "anything"))
check("失败后再问仍返回 host（不抛异常）", lock_key(H1, "") == H1)
check("失败结果被缓存，TTL 内只探测一次", len(CALLS) == 1, CALLS)

print("\n2. 带投机（speculative=true）→ 同模型排队")
reset({"qwen3.8-flash-next": True})
check("命中且带投机 → host|model",
      lock_key(H1, "qwen3.8-flash-next") == f"{H1}|qwen3.8-flash-next",
      lock_key(H1, "qwen3.8-flash-next"))
check("别名对不上 → 退回 host（不吃两把锁）", lock_key(H1, "qwen3.8-flash-125b") == H1)
check("空模型名 → host", lock_key(H1, "") == H1)

print("\n3. 不带投机（speculative=false）→ 放行并发")
reset({"gemma4-9b": False})
check("命中且不带投机 → 空串（不排队）", lock_key(H1, "gemma4-9b") == "",
      repr(lock_key(H1, "gemma4-9b")))

print("\n4. 多模型混合：各按各的 spec 走")
reset({"qwen3.8-flash-next": True, "gemma4-9b": False, "qwen3.8-27b-mlx": False})
k_spec = lock_key(H1, "qwen3.8-flash-next")
k_free = lock_key(H1, "gemma4-9b")
check("带投机的拿 host|model", k_spec == f"{H1}|qwen3.8-flash-next", k_spec)
check("不带投机的拿空串", k_free == "", repr(k_free))
check("27B 也不排队", lock_key(H1, "qwen3.8-27b-mlx") == "")
check("未知模型名 → host（保守）", lock_key(H1, "no-such-model") == H1)

print("\n5. 字段缺失按 True（保守）")
reset({"mystery-model": None})
check("无 speculative 字段 → 仍需排队",
      lock_key(H1, "mystery-model") == f"{H1}|mystery-model",
      lock_key(H1, "mystery-model"))

print("\n6. TTL 缓存与刷新")
reset({"m1": False, "m2": True})
lock_key(H1, "m1")
n1 = len(CALLS)
lock_key(H1, "m2")
check("TTL 内不重复探测", len(CALLS) == n1 == 1, f"calls={len(CALLS)}")
ts, models = NS["_mlx_upstream_ids"][H1]
NS["_mlx_upstream_ids"][H1] = (ts - NS["_MLX_UPSTREAM_IDS_TTL"] - 1, models)
lock_key(H1, "m1")
check("TTL 过期后重新探测", len(CALLS) == 2, f"calls={len(CALLS)}")

print("\n7. 跨 host 隔离")
reset({"m1": True, "m2": True})
check("H1 与 H2 拿到不同键", lock_key(H1, "m1") != lock_key(H2, "m1"))

print("\n8. 回退开关：TG_MLX_SERIAL_MODE")
reset({"gemma4-9b": False, "other": True}, mode="model")
check("mode=model 时非投机模型也要排队",
      lock_key(H1, "gemma4-9b") == f"{H1}|gemma4-9b", lock_key(H1, "gemma4-9b"))
reset({"gemma4-9b": False, "other": True}, mode="host")
check("mode=host 时一律 host 锁", lock_key(H1, "gemma4-9b") == H1, lock_key(H1, "gemma4-9b"))
check("mode=host 时带投机的也走 host", lock_key(H1, "other") == H1)
check("默认值确实是 spec", "TG_MLX_SERIAL_MODE\", \"spec\"" in SRC_TEXT or '"spec"' in SRC_TEXT)

print("\n9. 信号量语义：同键复用、异键并行、不可重入")
reset({"m1": True, "m2": True})
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

print("\n10. 空串键不会建信号量（放行路径不该留垃圾）")
reset({"gemma4-9b": False})
before = len(NS["_mlx_serial_sems"])
key = lock_key(H1, "gemma4-9b")
if not key:
    pass                                  # 调用方直接跳过 _mlx_sem_for
check("不带投机的模型不产出锁键", key == "", repr(key))
check("（不会因为探测而创建信号量）", len(NS["_mlx_serial_sems"]) == before)

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
