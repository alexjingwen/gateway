#!/usr/bin/env python3
"""回归：大模型仅服务深度档（TG_AUTO_DEEP_ONLY / config global.auto_deep_only）。

背景（2026-11-09，架构迁移 27B + Flash-Next 125B 之后）：
  balanced 打分的 quality 因子是"候选池内参数量对数归一"，同池里 125B 恒得 1.00。
  当大模型**比小模型快**时（旧 122B 对 8B 就是如此）这条规则是对的；但反过来——
  本机 125B 解码约 40 t/s、27B 约 90 t/s——一句"1+1"也会被判给 125B，白烧 111 GB
  权重和 3 倍延迟。速度因子是实测的，会部分抵消，但实测分差只有 0.03~0.05，
  扛不住负载波动（首轮实测：light 档 tier=0.00 仍然选中 125B）。

期望：
  1. 不配置时行为完全不变（默认空集，向后兼容）
  2. 配置后：light / standard 档把该模型摘出候选池
  3. deep 档（长输入 / 带工具 / 带图）仍然选得到它
  4. config global.auto_deep_only 与 env 等效，且支持写别名
  5. 摘完候选池会空时**不生效**（宁可给贵模型，也不能 503）

用法：
  TG_SRC=/path/to/token-gateway.py python3 tests/test_auto_deep_only.py
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


# ---- 与现网同构的画像：125B 参数大但在本机更慢 ----
PROFILE = {
    "big125b": {"p": 125.0, "quant": 4},
    "mid27b":  {"p": 27.0,  "quant": 4},
}
tg.SPEED_MIN_SAMPLES = 2
tg.MODEL_LATENCY = tg.LatencyStats()
# 短请求：27B 2.0s，125B 12.6s（实测放大 3.5 倍）
for delta in (0.0, 0.3):
    tg.MODEL_LATENCY.record("mid27b", 800, 2.0 + delta)
    tg.MODEL_LATENCY.record("big125b", 800, 12.6 + delta)
    tg.MODEL_LATENCY.record("mid27b", 60000, 6.0 + delta)
    tg.MODEL_LATENCY.record("big125b", 60000, 21.0 + delta)

CANDIDATES = [(n, "http://127.0.0.1:8088") for n in PROFILE]

handler = object.__new__(tg.GatewayHandler)
handler._ollama_meta_cache = {"meta": {}}
handler._auto_tier_label = ""
handler._pool = list(CANDIDATES)
handler._auto_candidates = lambda path, req, cfg: (list(handler._pool), "文本请求")
handler._auto_param_b = lambda m, h: PROFILE[m]["p"]
handler._auto_quant_rank = lambda m, h: PROFILE[m]["quant"]

CFG_EMPTY = {"global": {}}


def decide(chars=6, tools=False, cfg=None):
    content = "字" * chars
    req = {"messages": [{"role": "user", "content": content}], "max_tokens": 256}
    if tools:
        req["tools"] = [{"type": "function",
                         "function": {"name": "noop", "parameters": {"type": "object"}}}]
    model, host, why = handler._auto_pick_model("/v1/chat/completions", req, cfg or CFG_EMPTY)
    return model, handler._auto_tier_label, why


print("== 1. 未配置：行为不变 ==")
tg.AUTO_DEEP_ONLY = set()
handler._pool = list(CANDIDATES)
m, tier, why = decide()
print(f"  · 短问答 → {m} [tier={tier}]")
check("默认不摘掉任何候选", tier == "light" and m == "big125b", f"{tier}/{m}")
check("说明里没有深度档过滤痕迹", "仅限深度档" not in why, why)

print("\n== 2. env 配置后：轻量档摘掉大模型 ==")
tg.AUTO_DEEP_ONLY = {"big125b"}
handler._pool = list(CANDIDATES)
m, tier, why = decide()
print(f"  · 短问答 → {m} [tier={tier}]")
check("轻量档改用 27B", m == "mid27b", m)
check("说明里标注了过滤", "仅限深度档" in why, why)

print("\n== 3. 常规档同样摘掉 ==")
m, tier, why = decide(chars=2000)
print(f"  · 2000 字符 → {m} [tier={tier}]")
check("常规档改用 27B", tier == "standard" and m == "mid27b", f"{tier}/{m}")

print("\n== 4. 深度档仍然选得到大模型 ==")
m, tier, why = decide(chars=8000)
print(f"  · 8000 字符 → {m} [tier={tier}]")
check("长输入走 125B", tier == "deep" and m == "big125b", f"{tier}/{m}")

m, tier, why = decide(chars=20, tools=True)
print(f"  · 带工具 → {m} [tier={tier}]")
check("工具调用走 125B", tier == "deep" and m == "big125b", f"{tier}/{m}")

print("\n== 5. config global.auto_deep_only（支持别名） ==")
tg.AUTO_DEEP_ONLY = set()
tg.MODEL_ALIASES["flash-alias"] = "big125b"
cfg = {"global": {"auto_deep_only": ["flash-alias"]}}
m, tier, why = decide(cfg=cfg)
print(f"  · 短问答 → {m} [tier={tier}]")
check("config 别名同样生效", m == "mid27b", m)

print("\n== 6. 摘完会空池时不生效 ==")
handler._pool = [("big125b", "http://127.0.0.1:8088")]
tg.AUTO_DEEP_ONLY = {"big125b"}
m, tier, why = decide()
print(f"  · 唯一候选=125B，短问答 → {m} [tier={tier}]")
check("不因策略配置而空池", m == "big125b", m)

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败: {', '.join(FAILS)}")
    sys.exit(1)
print("✅ 全部通过")
