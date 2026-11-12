#!/usr/bin/env python3
"""回归：按模型的缺省请求体字段（TG_MODEL_EXTRA）。

为什么有这个开关：gpt-oss 的思考量只能从**请求体**控制（`reasoning_effort`）。
同一道题实测：
    默认（medium）  300 completion tokens / 3.35s
    reasoning_effort=low  119 tokens / 1.51s
而 gemma4 对 think / reasoning_effort 完全不认（Ollama 侧直接忽略），
所以不能做成全局开关 —— 必须按模型配。

不变量：
  ① 只补客户端**没给**的字段（绝不覆盖客户端显式设置，否则最难排查）；
  ② 非法配置整体忽略（坏 JSON 不该让网关起不来）；
  ③ 未配置时行为完全不变（向后兼容）。

用法：
  TG_SRC=/path/to/token-gateway.py python3 tests/test_model_extra.py
"""
import ast
import importlib.util
import json
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
        FAILS.append(name)
        print(f"  ❌ {name}  {detail}")


def load(env_val=None):
    """按真实源码加载解析器与应用函数；MODEL_EXTRA 用给定环境值重建。"""
    # 隔离日志目录：本用例会把网关模块完整 import 一次（为了拿真实函数而不是抄逻辑），
    # 而模块在 import 时就会重放 usage.jsonl。不隔离的话动辄读 40MB 生产日志，
    # 既慢又有"测试脚本碰生产数据"的隐患。
    logdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_logs")
    os.makedirs(logdir, exist_ok=True)
    os.environ["TG_LOG_DIR"] = logdir
    if env_val is None:
        os.environ.pop("TG_MODEL_EXTRA", None)
    else:
        os.environ["TG_MODEL_EXTRA"] = env_val
    spec = importlib.util.spec_from_file_location("tg_extra_%d" % len(FAILS), SRC)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        raise
    return m


print("1. 未配置：向后兼容（空表）")
m = load(None)
check("MODEL_EXTRA 为空", m.MODEL_EXTRA == {}, m.MODEL_EXTRA)
req = {"model": "gpt-oss:20b", "messages": []}
check("没有配置时不改动请求体", m._apply_model_extra(req, "gpt-oss:20b") == {}, req)
check("请求体保持原样", req == {"model": "gpt-oss:20b", "messages": []}, req)

print("\n2. 正常配置：补缺省字段")
m = load('{"gpt-oss:20b": {"reasoning_effort": "low"}}')
check("解析出一项", list(m.MODEL_EXTRA) == ["gpt-oss:20b"], m.MODEL_EXTRA)
req = {"model": "gpt-oss:20b", "messages": [{"role": "user", "content": "hi"}]}
added = m._apply_model_extra(req, "gpt-oss:20b")
check("补上了 reasoning_effort", added == {"reasoning_effort": "low"}, added)
check("请求体里生效", req.get("reasoning_effort") == "low", req)

print("\n3. 绝不覆盖客户端显式设置")
req = {"model": "gpt-oss:20b", "reasoning_effort": "high"}
added = m._apply_model_extra(req, "gpt-oss:20b")
check("客户端设了就不动", added == {} and req["reasoning_effort"] == "high", req)

print("\n4. 只对配置里的模型生效")
req = {"model": "qwen3.8-flash-next"}
check("别的模型不受影响",
      m._apply_model_extra(req, "qwen3.8-flash-next") == {} and "reasoning_effort" not in req, req)
check("unknown 模型也不炸", m._apply_model_extra({"a": 1}, "no-such-model") == {})

print("\n5. 非法配置被忽略（不让坏配置拖垮启动）")
for bad in ('', '   ', 'not json', '[]', '"str"', '{"a": 1}'):
    mb = load(bad)
    ok = mb.MODEL_EXTRA == {}
    check(f"非法配置 {bad!r} → 空表", ok, mb.MODEL_EXTRA)
mb = load('{"gpt-oss:20b": "low"}')       # 值不是 dict
check("值为非 dict 的项被丢弃", mb.MODEL_EXTRA == {}, mb.MODEL_EXTRA)
mb = load('{"gpt-oss:20b": {"reasoning_effort": "low", "": "x"}}')
check("空键被丢弃",
      mb.MODEL_EXTRA == {"gpt-oss:20b": {"reasoning_effort": "low"}}, mb.MODEL_EXTRA)

print("\n6. 多模型配置")
m = load('{"gpt-oss:20b": {"reasoning_effort": "low"}, "m2": {"top_k": 5}}')
check("两项都在", set(m.MODEL_EXTRA) == {"gpt-oss:20b", "m2"}, m.MODEL_EXTRA)
check("各补各的",
      m._apply_model_extra({}, "m2") == {"top_k": 5}
      and m._apply_model_extra({}, "gpt-oss:20b") == {"reasoning_effort": "low"})

if FAILS:
    print(f"\n{len(FAILS)} 项失败")
    sys.exit(1)
print("\n✅ 全部通过")
