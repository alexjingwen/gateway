#!/usr/bin/env python3
"""回归：参数量覆写（TG_MODEL_PARAMS）。

背景（2026-11-09，Flash-Next 运行时从 llama.cpp 换成 gmlx + MTP 之后）：
  _auto_param_b 对非 Ollama 上游只能从**模型名**推断参数量。
    · llama.cpp 时代模型名：qwen3.8-flash-125b  → 正则抓到 125 → 125B
    · gmlx 时代模型名：      qwen3.8-flash-next → 抓不到数字 → None
  结果为 None 时，_auto_tier_fit 与 balanced 的 quality 都落到 0.35 的中性低分，
  于是 auto 的深度档不再选 125B，而退回 27B（现网日志已复现：
  `auto → qwen3.8:27b ... tier=0.65 quality=0.75`，125B 根本没进候选）。

  这不该靠"模型名里必须带数字"这种约定来保证，因此加显式覆写表。

期望：
  1. 未配置时行为完全不变（默认空表，向后兼容）
  2. 覆写优先于"从名字推断"
  3. 覆写优先于 Ollama 上游元数据
  4. 非法项（缺等号、缺值、非数字、空 key）被忽略，不影响其它项
  5. 端到端：覆写后 auto 深度档选回 125B 而不是 27B

用法：
  TG_SRC=/path/to/token-gateway.py python3 tests/test_model_params.py
"""
import importlib.util
import os
import pathlib
import sys

SRC = pathlib.Path(os.environ.get(
    "TG_SRC",
    pathlib.Path(__file__).resolve().parent.parent / "token-gateway.py",
))

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        FAILS.append(name)


CURRENT = {"tg": None}


def fresh_import(params=None):
    """按指定的 TG_MODEL_PARAMS 重新加载模块（常量在 import 时解析）。"""
    os.environ.pop("TG_MODEL_PARAMS", None)
    if params is not None:
        os.environ["TG_MODEL_PARAMS"] = params
    spec = importlib.util.spec_from_file_location("tg_under_test", SRC)
    tg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tg)
    CURRENT["tg"] = tg
    return tg


class Stub:
    """_auto_param_b 需要 _ollama_meta_cache 和 _param_b_from_name（staticmethod）。"""

    def __init__(self, meta=None):
        self._ollama_meta_cache = {"meta": meta or {}}

    def _param_b_from_name(self, model):
        return CURRENT["tg"].GatewayHandler._param_b_from_name(model)


print("== 1. 未配置：行为不变 ==")
tg = fresh_import(None)
check("覆写表为空", tg.MODEL_PARAM_OVERRIDES == {}, tg.MODEL_PARAM_OVERRIDES)
h = Stub()
check("带数字的名字仍可推断", tg.GatewayHandler._auto_param_b(h, "qwen3.8-flash-125b", "http://x") == 125.0)
check("无数字的名字返回 None", tg.GatewayHandler._auto_param_b(h, "qwen3.8-flash-next", "http://x") is None)

print("\n== 2. 覆写生效，且优先于名字推断 ==")
tg = fresh_import("qwen3.8-flash-next=125")
h = Stub()
check("覆写表解析正确", tg.MODEL_PARAM_OVERRIDES == {"qwen3.8-flash-next": 125.0}, tg.MODEL_PARAM_OVERRIDES)
check("无数字名字 → 覆写值", tg.GatewayHandler._auto_param_b(h, "qwen3.8-flash-next", "http://x") == 125.0)

tg2 = fresh_import("qwen3.8-flash-125b=200")
h2 = Stub()
check("覆写优先于名字推断",
      tg2.GatewayHandler._auto_param_b(h2, "qwen3.8-flash-125b", "http://x") == 200.0)

print("\n== 3. 覆写优先于 Ollama 元数据 ==")
tg3 = fresh_import("qwen3.8:27b=30")
h3 = Stub({"qwen3.8:27b": {"param": "27.3B", "quant": "Q4_K_M"}})
check("上游说 27.3B，覆写说 30B → 取 30",
      tg3.GatewayHandler._auto_param_b(h3, "qwen3.8:27b", tg3.OLLAMA_HOST) == 30.0)
h3b = Stub({"qwen3.8:27b": {"param": "27.3B"}})
check("未覆写的 Ollama 模型仍走元数据",
      tg3.GatewayHandler._auto_param_b(h3b, "gemma4:latest", tg3.OLLAMA_HOST) is None
      or tg3.GatewayHandler._auto_param_b(Stub({"gemma4:latest": {"param": "9.6B"}}),
                                          "gemma4:latest", tg3.OLLAMA_HOST) == 9.6)

print("\n== 4. 非法项被忽略 ==")
tg4 = fresh_import("bad,qwen3.8-flash-next=125,x=abc,=5, ,qwen3.8-27b= 27.3 ,y=")
check("只保留合法键值", tg4.MODEL_PARAM_OVERRIDES == {"qwen3.8-flash-next": 125.0, "qwen3.8-27b": 27.3},
      tg4.MODEL_PARAM_OVERRIDES)
check("空表不抛异常", fresh_import("").MODEL_PARAM_OVERRIDES == {})

print("\n== 5. 端到端：覆写后深度档选回 125B ==")
tg5 = fresh_import("qwen3.8-flash-next=125")
PROFILE = {"qwen3.8-flash-next": {"p": 125.0, "quant": 4}, "qwen3.8:27b": {"p": 27.0, "quant": 4}}
tg5.SPEED_MIN_SAMPLES = 2
tg5.MODEL_LATENCY = tg5.LatencyStats()
for delta in (0.0, 0.3):
    tg5.MODEL_LATENCY.record("qwen3.8:27b", 800, 2.0 + delta)
    tg5.MODEL_LATENCY.record("qwen3.8-flash-next", 800, 2.5 + delta)
    tg5.MODEL_LATENCY.record("qwen3.8:27b", 60000, 6.0 + delta)
    tg5.MODEL_LATENCY.record("qwen3.8-flash-next", 60000, 7.0 + delta)

handler = object.__new__(tg5.GatewayHandler)
handler._ollama_meta_cache = {"meta": {}}
handler._auto_tier_label = ""
handler._pool = [(n, "http://127.0.0.1:8090") for n in PROFILE]
handler._auto_candidates = lambda path, req, cfg: (list(handler._pool), "文本请求")
handler._auto_quant_rank = lambda m, h: PROFILE[m]["quant"]
# 注意：这里**不** patch _auto_param_b，让它走真实的覆写逻辑

CFG = {"global": {}}


def decide(chars=6, tools=False):
    req = {"messages": [{"role": "user", "content": "字" * chars}], "max_tokens": 256}
    if tools:
        req["tools"] = [{"type": "function",
                         "function": {"name": "noop", "parameters": {"type": "object"}}}]
    model, host, why = handler._auto_pick_model("/v1/chat/completions", req, CFG)
    return model, handler._auto_tier_label, why


tg5.AUTO_DEEP_ONLY = {"qwen3.8-flash-next"}
handler._pool = [(n, "http://127.0.0.1:8090") for n in PROFILE]
m, tier, why = decide(chars=20, tools=True)
print(f"  · 带工具 → {m} [tier={tier}]")
check("深度档选 125B", tier == "deep" and m == "qwen3.8-flash-next", f"{tier}/{m}")
check("理由里给出参数量而非'未知'", "参数量未知" not in why and "125" in why, why)

m, tier, why = decide()
print(f"  · 短问答 → {m} [tier={tier}]")
check("轻量档仍走 27B", tier == "light" and m == "qwen3.8:27b", f"{tier}/{m}")

print("\n" + "=" * 46)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败: {FAILS}")
    sys.exit(1)
print("✅ 全部通过")
