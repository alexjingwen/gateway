#!/usr/bin/env python3
"""speed 因子回归测试：速度必须来自实测耗时，且分档/质量/速度三权分立。

覆盖：
  1. 权重结构：tier / quality / speed 三项等权（0.30 各），speed 不为 0
  2. 轻量短请求 → 小模型（速度与分档同向）
  3. 常规请求（3k~20k 上下文）→ 大模型（实测它更快，反直觉但正确）
  4. 深度请求（带 tools）→ 大模型
  5. 无实测样本的模型拿中性分，不得因"没数据"胜出
  6. 速度因子确实随实测耗时变化（同一请求下快的候选得分更高）

用法：
  TG_SRC=/path/to/token-gateway.py python3 tests/test_speed_weight.py
"""
import importlib.util
import os
import pathlib
import sys

SRC = pathlib.Path(os.environ.get(
    "TG_SRC",
    pathlib.Path(__file__).resolve().parent.parent / "token-gateway.py",
))

spec = importlib.util.spec_from_file_location("tg_under_test", SRC)
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        FAILS.append(name)


# ---- 构造与现网一致的实测画像：8B 短请求快，122B 中长请求快 ----
# 数值取自 usage.jsonl 的桶内 p50（<3k / 3k~20k / >=20k）
PROFILE = {
    "small8b":   {"p": 8.0,   "quant": 4, "t": {0: 3.4, 1: 32.6, 2: 40.4}},
    "big122b":   {"p": 122.0, "quant": 4, "t": {0: 4.8, 1: 12.1, 2: 165.9}},
    "mid30b":    {"p": 30.0,  "quant": 4, "t": {0: 4.0, 1: 16.0, 2: 60.0}},
    "nodata3b":  {"p": 3.8,   "quant": 4, "t": None},
}
BUCKET_P = {0: 800, 1: 8000, 2: 60000}

tg.SPEED_MIN_SAMPLES = 2          # 测试里每桶只放 2 条样本
tg.MODEL_LATENCY = tg.LatencyStats()
for name, prof in PROFILE.items():
    if prof["t"] is None:
        continue
    for bucket, secs in prof["t"].items():
        for delta in (0.0, 0.4):
            tg.MODEL_LATENCY.record(name, BUCKET_P[bucket], secs + delta)

CANDIDATES = [(n, "http://127.0.0.1:8088") for n in PROFILE]

# ---- 构造一个不接 socket 的 handler ----
handler = object.__new__(tg.GatewayHandler)
handler._ollama_meta_cache = {"meta": {}}
handler._auto_tier_label = ""
handler._auto_candidates = lambda path, req, cfg: (list(CANDIDATES), "文本请求")
handler._auto_param_b = lambda m, h: PROFILE[m]["p"]
handler._auto_quant_rank = lambda m, h: PROFILE[m]["quant"]

CFG = {"global": {}}


def decide(tag, chars, max_tokens=1024, tools=False, n_msgs=1):
    content = "字" * chars
    req = {"messages": [{"role": "user", "content": content}] * n_msgs,
           "max_tokens": max_tokens}
    if tools:
        req["tools"] = [{"type": "function",
                         "function": {"name": "noop", "parameters": {"type": "object"}}}]
    model, host, why = handler._auto_pick_model("/v1/chat/completions", req, CFG)
    tier = handler._auto_tier_label
    print(f"  · {tag}: → {model}  [tier={tier}]")
    print(f"      {why}")
    return model, tier


print("== 1. 权重结构 ==")
w = tg.AUTO_WEIGHTS
check("speed 权重已启用", w.get("speed", 0) > 0, f"got {w.get('speed')}")
check("tier 与 quality 等权", abs(w.get("tier", 0) - w.get("quality", 0)) < 1e-9, str(w))
check("speed 与 quality 等权", abs(w.get("speed", 0) - w.get("quality", 0)) < 1e-9, str(w))

print("\n== 2. 轻量短请求 ==")
m, tier = decide("200 字符短问答", 200, max_tokens=256)
check("轻量档", tier == "light", tier)
check("短请求走 8B 级小模型", m == "small8b", m)

print("\n== 3. 常规请求（2000 字符，位于轻量与深度之间） ==")
m, tier = decide("2000 字符常规", 2000, max_tokens=1024)
check("常规档", tier == "standard", tier)
check("常规请求走中档模型（30B 级）", m == "mid30b", m)

print("\n== 3b. 长输入（8000 字符，超过 deep_chars=3000） ==")
m, tier = decide("8000 字符长输入", 8000, max_tokens=1024)
check("深度档", tier == "deep", tier)
check("长输入走 122B（实测它更快）", m == "big122b", m)

print("\n== 4. 深度请求（带 tools） ==")
m, tier = decide("8000 字符 + tools", 8000, max_tokens=2048, tools=True)
check("深度档", tier == "deep", tier)
check("工具调用走 122B", m == "big122b", m)

print("\n== 5. 长上下文深度请求 ==")
m, tier = decide("长上下文 60000 token", 120000, max_tokens=2048)
check("深度档", tier == "deep", tier)
check("长上下文走 122B", m == "big122b", m)

print("\n== 6. 无实测数据的模型不占便宜 ==")
t_nodata = tg.MODEL_LATENCY.estimate("nodata3b", 8000)
check("无样本返回 None", t_nodata is None, str(t_nodata))
speed_v = 0.40
score_nodata, desc = handler._auto_balanced_score(
    "nodata3b", "http://127.0.0.1:8088", 3.8, 1, handler._auto_policy(CFG), [], 
    {"lo": 0.58, "hi": 2.09}, None)
score_known, _ = handler._auto_balanced_score(
    "small8b", "http://127.0.0.1:8088", 8.0, 1, handler._auto_policy(CFG), [],
    {"lo": 0.58, "hi": 2.09}, 12.1 / 32.6)
check("无数据中性分低于实测较慢者", score_nodata < score_known + 1e-9,
      f"nodata={score_nodata:.3f} known={score_known:.3f}")
print(f"      nodata3b 得分 {score_nodata:.3f}（{desc}）")
print(f"      small8b  得分 {score_known:.3f}")

print("\n== 7. speed 因子随实测变化 ==")
policy = handler._auto_policy(CFG)
ctx = {"lo": 0.58, "hi": 2.09}
fast, desc_fast = handler._auto_balanced_score("big122b", "h", 122.0, 1, policy, [], ctx, 1.0)
slow, desc_slow = handler._auto_balanced_score("big122b", "h", 122.0, 1, policy, [], ctx, 0.3)
check("同一模型：越快得分越高", fast > slow, f"fast={fast:.3f} slow={slow:.3f}")
print(f"      speed=1.0 → {fast:.3f}（{desc_fast}）")
print(f"      speed=0.3 → {slow:.3f}（{desc_slow}）")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败: {', '.join(FAILS)}")
    sys.exit(1)
print("✅ 全部通过")
