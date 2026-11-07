#!/usr/bin/env python3
"""prompt 规模估算回归：p_hat 必须把工具清单与图片算进去。

背景：速度因子按 prompt token 分桶取实测中位数，而 `_auto_req_text_len` 只数 messages
里的正文。工具清单与图片在真实 prompt 里同样占 token，漏算会把带工具/带图的请求
估进偏小的桶（那里全是纯文本样本，明显更快），speed 因子随之系统性偏高。

覆盖：
  1. 正文长度口径不变：`_auto_req_text_len` 不因 tools 而变（分档阈值依赖它）
  2. `_auto_req_prompt_chars` 计入 tools / functions / response_format 的序列化长度
  3. 边界穿越：正文 5000 字符 + 工具清单，p_hat 从桶 0 抬到桶 1
  4. 图片按面积折算：384² / 768² / 1536² 与实测 146 / 578 / 2306 token 相差 < 5%
  5. JPEG（SOF0）宽高解析正确
  6. 远程 URL 与坏数据回退名义值，且绝不抛异常
  7. 多种图片字段形态（content 内 image_url、ollama images、请求级 images）都能收集到

用法：
  TG_SRC=/path/to/token-gateway.py tests/test_prompt_estimate.py
"""
import base64
import importlib.util
import os
import pathlib
import struct
import sys
import tempfile
import zlib

SRC = pathlib.Path(os.environ.get(
    "TG_SRC",
    pathlib.Path(__file__).resolve().parent.parent / "token-gateway.py",
))

# 导入期会读 TG_CONFIG_DIR / TG_LOG_DIR，指到临时目录以免碰到真实配置与日志
_TMP = tempfile.mkdtemp(prefix="tg-est-")
os.environ.setdefault("TG_CONFIG_DIR", _TMP)
os.environ.setdefault("TG_LOG_DIR", _TMP)

spec = importlib.util.spec_from_file_location("tg_under_test", SRC)
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)

H = tg.GatewayHandler
FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        FAILS.append(name)


def make_png(w, h):
    raw = b"".join(b"\x00" + bytes([(x * 7 + y * 3) % 256 for x in range(w * 3)])
                   for y in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def png_uri(w, h):
    return "data:image/png;base64," + base64.b64encode(make_png(w, h)).decode()


def make_jpeg(w, h):
    # 只为验证解析：SOI + APP0 + SOF0 + EOI，不做真实编码
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
            + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
            + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9 + b"\xff\xd9")


TOOLS = [{"type": "function", "function": {
    "name": f"tool_{i}",
    "description": "读取文件的工具，支持分段读取",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}} for i in range(12)]

print("== 1. 正文口径不变（分档阈值依赖 text_len） ==")
plain = {"messages": [{"role": "user", "content": "字" * 1000}]}
with_tools = dict(plain)
with_tools["tools"] = TOOLS
check("text_len 不因 tools 变化",
      H._auto_req_text_len(plain) == H._auto_req_text_len(with_tools) == 1000,
      f"{H._auto_req_text_len(plain)} vs {H._auto_req_text_len(with_tools)}")

print("\n== 2. prompt 规模计入结构化字段 ==")
pc_plain = H._auto_req_prompt_chars(plain)
pc_tools = H._auto_req_prompt_chars(with_tools)
tools_chars = len(tg.json.dumps(TOOLS, ensure_ascii=False))
check("无 tools 时与 text_len 相等", pc_plain == 1000, pc_plain)
check("带 tools 时增加工具序列化长度",
      pc_tools == 1000 + tools_chars, f"{pc_tools} vs {1000 + tools_chars}")
rt = dict(plain)
rt["response_format"] = {"type": "json_object"}
check("response_format 一并计入",
      H._auto_req_prompt_chars(rt) == 1000 + len(tg.json.dumps(rt["response_format"])),
      H._auto_req_prompt_chars(rt))

print("\n== 3. 边界穿越：带工具时桶号被纠正 ==")
cross = {"messages": [{"role": "user", "content": "字" * 5000}]}
p_before = int(H._auto_req_text_len(cross) * tg.TOKENS_PER_CHAR)
cross["tools"] = TOOLS
p_after = int(H._auto_req_prompt_chars(cross) * tg.TOKENS_PER_CHAR)
b_before = tg.MODEL_LATENCY.bucket_of(p_before)
b_after = tg.MODEL_LATENCY.bucket_of(p_after)
check("正文口径落桶 0", b_before == 0, f"p_hat={p_before}")
check("修正后落桶 1", b_after == 1, f"p_hat={p_after}")

print("\n== 4. 图片按面积折算（实测 146 / 578 / 2306） ==")
for size, measured in ((384, 146), (768, 578), (1536, 2306)):
    req = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": png_uri(size, size)}}]}]}
    got = H._auto_req_image_tokens(req)
    ok = abs(got - measured) <= measured * 0.05
    check(f"{size}² → {got} tok（实测 {measured}，误差 <5%）", ok, f"got={got}")

print("\n== 5. JPEG 宽高解析 ==")
mp = tg._data_uri_megapixels(
    "data:image/jpeg;base64," + base64.b64encode(make_jpeg(1920, 1080)).decode())
check("1920x1080 → 2.07 MP", mp is not None and abs(mp - 2.0736) < 0.01, mp)

print("\n== 6. 无法解析时回退且不抛异常 ==")
remote = {"messages": [{"role": "user", "content": [
    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]}
check("远程 URL → 名义值", H._auto_req_image_tokens(remote) == tg.IMG_TOKENS_NOMINAL,
      H._auto_req_image_tokens(remote))
check("坏 base64 → None", tg._data_uri_megapixels("data:image/png;base64,!!!!") is None)
check("非 data URI → None", tg._data_uri_megapixels("https://x/y.png") is None)
check("空请求 → 0", H._auto_req_image_tokens({}) == 0)

print("\n== 7. 多种图片字段形态 ==")
forms = {
    "content 内 image_url": {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]},
    "ollama images": {"messages": [{"role": "user", "content": "x",
                                    "images": ["data:image/png;base64,AAAA"]}]},
    "请求级 images": {"messages": [{"role": "user", "content": "x"}],
                      "images": ["data:image/png;base64,AAAA"]},
}
for label, req in forms.items():
    check(label, len(H._auto_req_image_urls(req)) == 1,
          H._auto_req_image_urls(req))

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败: {FAILS}")
    sys.exit(1)
print("✅ 全部通过")
