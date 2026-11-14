#!/usr/bin/env python3
"""
Ollama Token Gateway - 多设备 Token 服务网关
功能：
  ✅ API Key 鉴权（多 Key 管理）
  ✅ 每个 Key 独立 Token 额度限制（日/月限额）
  ✅ 全请求 Token 用量记录（JSONL 日志）
  ✅ 速率限流（QPS/并发限制）
  ✅ OpenAI 兼容格式，支持流式 SSE
  ✅ CORS + 局域网访问
  ✅ 健康检查接口
"""

import http.server
import http.client
import socketserver
import subprocess
import urllib.parse
import urllib.request
import urllib.error
import json
import math
import os
import re
import itertools
import sys
import time
import traceback
import threading
import socket
import select
import ssl
import base64
import hashlib
import struct
import ctypes
import ctypes.util
from pathlib import Path
from collections import defaultdict, deque, OrderedDict
from typing import Dict, List, Optional, Tuple


# ========= 上游一律直连，永不经过 HTTP_PROXY =========
# 网关的所有上游（Ollama / llama-server / mlx_vlm）都在本机。urllib 默认会读
# HTTP_PROXY / HTTPS_PROXY / ALL_PROXY，于是连 `http://127.0.0.1:11434` 也会被
# 交给代理。2026-11-08 现网故障就是这么来的：进程从带
# `HTTP_PROXY=http://127.0.0.1:65388` 的环境里被拉起（沙箱给子进程注入的出口代理），
# 代理只在工具调用期间存活，停掉之后网关所有上游请求都变成
# `[Errno 61] Connection refused`：
#   · `/api/tags` 与上游 `/v1/models` 失败 → 模型缓存被清空
#   · `/v1/models` 只剩 `auto` → 用户看到的现象是「122B 不见了」
#   · 所有推理请求 502，而同一台机器上 curl 一切正常（curl 常带 --noproxy）
# 极难自查：/health 仍 200、进程还在跑，日志只有一句 Connection refused。
# 这里装一个空 ProxyHandler 的 opener，把这条隐式依赖从代码层断掉；
# plist 里的 NO_PROXY=* 只是双保险，不能作为唯一防线。
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)


def _env_num(name: str, default, cast=int, minimum=None, maximum=None):
    """读取数值型环境变量；非法值退回默认值，**绝不在请求路径抛异常**。

    为什么必须兜底：`do_POST` 里会实时读取 TG_READ_TIMEOUT / TG_MIN_MAX_TOKENS
    等变量。若部署脚本写成 `TG_READ_TIMEOUT=600s`，或在 plist 里留成空字符串，
    裸 `int()` 会抛 ValueError，被最外层兜底捕获后变成**每个请求 500**
    ——服务看着是 up 的（/health 正常），但所有对话全挂，且日志只有一句
    internal_error，极难定位。启动期的同名错误更糟：进程直接起不来，launchd 反复重启。

    这里用直接 print 而不是 log()：本函数在模块导入期就会被调用，
    而 log() 定义在文件更后面的位置。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = cast(str(raw).strip())
    except (TypeError, ValueError):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] ⚠️ 环境变量 {name}={raw!r} 非法，已回退默认值 {default}", flush=True)
        return default
    if minimum is not None and v < minimum:
        return minimum
    if maximum is not None and v > maximum:
        return maximum
    return v


# ========== 配置 ==========
CONFIG_DIR = Path(os.environ.get("TG_CONFIG_DIR", Path.home() / ".ollama-token-gateway"))
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_DIR = Path(os.environ.get("TG_LOG_DIR", CONFIG_DIR / "logs"))
USAGE_LOG = LOG_DIR / "usage.jsonl"

OLLAMA_HOST = os.environ.get("TG_OLLAMA_HOST", "http://127.0.0.1:11434")
RAPID_MLX_HOSTS = [h.strip() for h in os.environ.get(
    "TG_RAPID_MLX_HOSTS",
    os.environ.get("TG_RAPID_MLX_HOST", "http://127.0.0.1:8002,http://127.0.0.1:8088")
).split(",") if h.strip()]

# VLM hosts — 多模态模型，不过滤图片
RAPID_MLX_VLM_HOSTS = set(h.strip() for h in os.environ.get(
    "TG_RAPID_MLX_VLM_HOSTS",
    "http://127.0.0.1:8088"
).split(",") if h.strip())

# mlx-vlm server API Key
MLX_VLM_API_KEY = os.environ.get("TG_MLX_VLM_API_KEY", "sk-test")
def _parse_model_aliases(env_val: str) -> dict:
    out = {}
    for pair in env_val.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


MODEL_ALIASES = _parse_model_aliases(
    os.environ.get("TG_MODEL_ALIASES", "") or
    "qwen3.8-flash=qwen3.8-flash-125b,qwen3.8-27b=qwen3.8:27b"
)
_MODEL_REVERSE_ALIAS = {v: k for k, v in MODEL_ALIASES.items()}

# ========== auto 模型（自动分配） ==========
# 虚拟模型名 "auto"：请求到达后由网关从"本地可用模型池"里自动挑一个真实模型，
# 客户端不必为试不同模型而反复改 model 名。选中结果通过响应头 X-Gateway-Model 回传。
AUTO_MODEL_NAME = "auto"
AUTO_ENABLED = os.environ.get("TG_AUTO_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
# 分配策略：
#   balanced（默认）：先给请求分档（轻量/常规/深度），再把"分档适配"与"绝对质量"按**等权**
#                    和其他因子（显式偏好/量化保真）加权求和选模型。短问答给小模型
#                    （快、省），长上下文/工具调用上大模型。负载是硬约束（先排除繁忙者）
#   quality：纯质量降序（参数量 → 量化精度），忽略请求体量，一律用最强模型
#   least-busy：优先选网关侧 in-flight 最少的模型；全部繁忙时退化为"选负载最低者"，
#               全部空闲时行为与 round-robin 完全一致
#   round-robin：纯轮流，忽略负载与质量
#   random：随机
AUTO_STRATEGY = (os.environ.get("TG_AUTO_STRATEGY", "balanced") or "balanced").strip().lower()
# 显式质量优先顺序（从高到低，逗号分隔）。列出的模型优先于任何启发式，未列出的按参数量排在后面。
# 也可在 config.json 的 global.auto_quality（数组）里配置，两者会合并（env 在前）。允许写别名。
AUTO_QUALITY = [m.strip() for m in os.environ.get("TG_AUTO_QUALITY", "").split(",") if m.strip()]
# 量化精度档位（同参数量时的次级排序，值越大越精确）
AUTO_QUANT_RANK = (
    ("F32", 6), ("BF16", 5), ("FP16", 5), ("F16", 5),
    ("Q8", 4), ("Q6", 3), ("Q5", 2), ("Q4", 1), ("Q3", 0), ("Q2", 0), ("IQ", 0),
)
# 繁忙阈值：某模型在飞请求数 >= 此值即视为繁忙，least-busy 会跳过它（默认 1 = 已经有人用就换）
AUTO_BUSY_THRESHOLD = _env_num("TG_AUTO_BUSY_THRESHOLD", 1, minimum=1)
# 额外排除的模型（逗号分隔），与 config.json 的 global.deny_models / global.auto_exclude 叠加生效
AUTO_EXCLUDE = {m.strip() for m in os.environ.get("TG_AUTO_EXCLUDE", "").split(",") if m.strip()}

# ---- 分档优先（balanced 策略） ----
# 把请求按"体量/复杂度"归为三档，每档有理想参数量：轻量档用 8B 级模型够用且快，
# 常规档 30B 级均衡，深度档（长上下文/工具调用/带图）才上 122B 级。
AUTO_TIER_ENABLED = os.environ.get("TG_AUTO_TIER", "1").strip().lower() not in ("0", "false", "no", "off")
AUTO_TIER_NAMES = ("light", "standard", "deep")
AUTO_TIER_LABELS = {"light": "轻量", "standard": "常规", "deep": "深度"}

# 仅深度档可用的大模型（逗号分隔，允许写别名）。轻量/常规档把它们从候选池里摘掉。
#
# 为什么需要它：balanced 打分的 quality 因子按参数量对数归一，同一候选池里 125B
# 恒得 1.00。当大模型比小模型快很多时（例如 122B 对 8B），让 quality 说话是对的；
# 但反过来——本机 125B 解码约 40 t/s、27B 约 90 t/s——一句"1+1"也会被判给 125B，
# 把 111 GB 的权重和 3 倍延迟花在不需要的地方。速度因子是实测的，会部分抵消，
# 但分数差往往只有 0.03~0.05，扛不住负载波动。
# 这里用显式声明把"贵模型只服务难请求"从概率事件变成硬约束：默认空集（不改变
# 既有行为），由部署方按机器实际情况配置。
AUTO_DEEP_ONLY = {
    m.strip() for m in os.environ.get("TG_AUTO_DEEP_ONLY", "").split(",") if m.strip()
}

# 参数量覆写（逗号分隔的 model=params_b，例如 "qwen3.8-flash-next=125"）。
#
# 为什么需要：_auto_param_b 对非 Ollama 上游只能从**模型名**推断参数量。换到
# 不提供 /api/show 元数据、名字里又没数字的运行时（gmlx 的 `qwen3.8-flash-next`）
# 后，推断结果是 None，于是 tier_fit 与 quality 双双落到 0.35 的中性低分，
# 排在有参数量的模型（如 qwen3.8:27b）之后——实测表现为 auto 的深度档不再选
# 125B，而退回 27B。此表让部署方直接声明真实参数量，不再依赖名字约定。
MODEL_PARAM_OVERRIDES: Dict[str, float] = {}
for _item in os.environ.get("TG_MODEL_PARAMS", "").split(","):
    _k, _sep, _v = _item.partition("=")
    _k, _v = _k.strip(), _v.strip()
    if not _k or not _sep:
        continue
    try:
        MODEL_PARAM_OVERRIDES[_k] = float(_v)
    except ValueError:
        continue


def _parse_model_extra(env_val: str) -> Dict[str, dict]:
    """解析 TG_MODEL_EXTRA：按模型注入**缺省**请求体字段（JSON）。

    用途：有些上游能力只能靠请求体字段控制，而客户端不会传。最典型的是
    gpt-oss 的 `reasoning_effort` —— 同一个问题，默认（medium）300 个 token、
    3.35s；`low` 只要 119 个 token、1.51s（实测，见 README）。
    它既影响时延也影响额度（网关按上游 usage 记账），所以做成部署方可调的开关。

    语义刻意保守：**只补客户端没给的字段**。客户端显式设了就不动，
    免得"服务端偷偷改采样参数"这种最难排查的问题。非法 JSON 整体忽略。
    """
    if not env_val or not env_val.strip():
        return {}
    try:
        raw = json.loads(env_val)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, dict] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, dict) and k.strip():
            out[k.strip()] = {kk: vv for kk, vv in v.items()
                              if isinstance(kk, str) and kk.strip()}
    return out


MODEL_EXTRA: Dict[str, dict] = _parse_model_extra(os.environ.get("TG_MODEL_EXTRA", ""))


def _apply_model_extra(req: dict, model: str) -> dict:
    """把 MODEL_EXTRA[model] 中**客户端没给**的字段补进 req，返回实际补上的部分。

    拆成独立函数只为可测：这条语义（"只补缺省、绝不覆盖"）是它唯一的价值，
    放在 handler 中间就没人能验证了。
    """
    extra = MODEL_EXTRA.get(model) if model else None
    if not extra or not isinstance(req, dict):
        return {}
    added = {k: v for k, v in extra.items() if k not in req}
    if added:
        req.update(added)
    return added


def _parse_float_list(raw, default: List[float]) -> List[float]:
    """解析 '8,30,122' / [8, 30, 122] 为浮点列表；非法项忽略，空结果返回 default。"""
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = str(raw or "").split(",")
    out: List[float] = []
    for x in items:
        try:
            v = float(str(x).strip())
        except (TypeError, ValueError):
            continue
        if v > 0:
            out.append(v)
    return out or list(default)


def _parse_weight_map(raw, default: Dict[str, float]) -> Dict[str, float]:
    """解析权重：支持 dict 或 'tier=0.3,quality=0.3' 字符串；只覆盖已知键。"""
    out = dict(default)
    if not raw:
        return out
    if isinstance(raw, dict):
        pairs = raw.items()
    else:
        pairs = []
        for part in str(raw).split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                pairs.append((k, v))
    for k, v in pairs:
        key = str(k).strip().lower()
        if key not in out:
            continue
        try:
            w = float(v)
        except (TypeError, ValueError):
            continue
        if w >= 0:
            out[key] = w
    return out


# 各档理想参数量（B），可用 TG_AUTO_TIER_IDEALS / config global.auto_tier_ideals 覆盖
AUTO_TIER_IDEALS = _parse_float_list(os.environ.get("TG_AUTO_TIER_IDEALS", ""), [8.0, 30.0, 122.0])
# 分档阈值：短于 light 字符数视为轻量；长于 deep 字符数视为深度
AUTO_TIER_LIGHT_CHARS = _env_num("TG_AUTO_TIER_LIGHT_CHARS", 400, minimum=1)
AUTO_TIER_DEEP_CHARS = _env_num("TG_AUTO_TIER_DEEP_CHARS", 3000, minimum=1)
# 综合打分权重（balanced）：
#   tier    分档适配（prompt 规模 → 合适参数量）  默认 0.30
#   quality 绝对质量（参数量/量化）                默认 0.30
#   pref    显式优先表内排序                       默认 0.15
#   quant   量化保真                               默认 0.10
#   speed   速度收益（实测耗时越短越高）           默认 0.30
# **分档、质量、速度三者等权**：没有任何一项是隐含前提，最终按加权求和决定。
# speed 用实测耗时而非参数量——实测 122B 在 3k~20k 上下文比 8B 快，参数量反推会给出反向结论。
# 未启用的因子（权重 0 或未配置偏好表）从分母中剔除，不会拉低分数。
AUTO_WEIGHTS = _parse_weight_map(
    os.environ.get("TG_AUTO_WEIGHTS", ""),
    {"tier": 0.30, "quality": 0.30, "speed": 0.30, "pref": 0.15, "quant": 0.10},
)


# 网关侧「每模型在飞请求数」：只在经过本网关的请求上计数，用于 least-busy 选择。
# 注意：外部客户端直连上游（如直接打 :11434）造成的占用，网关无法感知。
_model_inflight: Dict[str, int] = {}
_model_inflight_lock = threading.Lock()


def model_inflight_inc(model: str) -> None:
    if not model:
        return
    with _model_inflight_lock:
        _model_inflight[model] = _model_inflight.get(model, 0) + 1


def model_inflight_dec(model: str) -> None:
    if not model:
        return
    with _model_inflight_lock:
        left = _model_inflight.get(model, 0) - 1
        if left <= 0:
            _model_inflight.pop(model, None)
        else:
            _model_inflight[model] = left


def model_inflight_get(model: str) -> int:
    with _model_inflight_lock:
        return _model_inflight.get(model, 0)
# 非对话模型（嵌入/重排）名字特征：不参与 chat 分配，只服务 /v1/embeddings
AUTO_EMBED_HINTS = ("embed", "bge", "mxbai", "gte-", "e5-", "rerank")
# 视觉模型名字特征：带图请求优先选它们（其余候选会被网关按既有逻辑过滤图片）
AUTO_VLM_HINTS = ("vl", "vision", "llava", "minicpm-v")


LISTEN_HOST = os.environ.get("TG_HOST", "0.0.0.0")
LISTEN_PORT = _env_num("TG_PORT", 3000, minimum=1, maximum=65535)

# HTTPS 配置
CERT_DIR = CONFIG_DIR / "certs"
CERT_FILE = CERT_DIR / "server.crt"
KEY_FILE = CERT_DIR / "server.key"
HTTP_REDIRECT_PORT = _env_num("TG_HTTP_PORT", 3001, minimum=1, maximum=65535)  # HTTP 端口（重定向到 HTTPS）
# 强制禁用 HTTPS（设为任意非空值即禁用，回退到 HTTP 单端口，方便不支持自签证书的客户端）
DISABLE_HTTPS = bool(os.environ.get("TG_DISABLE_HTTPS"))


def _https_enabled() -> bool:
    """是否启用 HTTPS（受环境变量 TG_DISABLE_HTTPS 控制）"""
    return (not DISABLE_HTTPS) and CERT_FILE.exists() and KEY_FILE.exists()

# 限流配置
DEFAULT_QPS = _env_num("TG_QPS", 5, minimum=1)     # 每个 Key 每秒请求数
DEFAULT_MAX_CONCURRENCY = _env_num("TG_CONCURRENCY", 10, minimum=1)  # 全局最大并发

# ── Rapid-MLX 上游串行化 ─────────────────────────────────────────────────────
# 踩过的坑（真实事故，2026-11-11）：gmlx 的 mid-flight 注入路径有形状缺陷 —— 当一个
# 请求正在 decode、另一个请求被塞进同一个 batch 时，qwen4_exp/vlm_model.py 的 mrope
# 位置张量算出 (1,B,L) 而目标要 (3,B,L) 且末维不等，整批 ValueError：
#     Cannot broadcast array of shape (1,2,2) into shape (3,2,1)
# 实测：两个并发请求 100% 双双 500（2 并发 3 轮 ×2 全中）；串行 6/6 全 200。
# 先试过 --speculative-width-cap 1（限制投机行数），**无效** —— 错误与投机宽度无关，
# 是注入本身。修在 gmlx 内部要改 site-packages（升级即丢），因此在网关这一层排队：
# 同一时刻只放一个生成请求进 8090。代价是并发吞吐退化为串行，收益是 0 个 500。
# /v1/models 之类的元数据 GET 不排队（不触发 batch 注入，排队只会让客户端卡住）。
MLX_SERIAL = os.environ.get("TG_MLX_SERIAL", "1") not in ("0", "false", "no", "off")
MLX_SERIAL_WAIT = _env_num("TG_MLX_SERIAL_WAIT", 300, minimum=1)

# 串行的**范围**（2026-11-11 晚补测后收窄）：
#   spec  —— 只串行带投机解码（MTP）的上游模型（默认）
#   model —— 每个上游模型各一把锁（上一版行为，一键回退用）
#   host  —— 整个 host 一把锁（最早的行为）
# 为什么可以这么收：缺陷发生在 speculative 的 mid-flight 注入路径（draft 张量宽度与
# 目标 batch 不符），**不带投机的模型走不到那段代码**。实测（gmlx 单进程驻留
# gemma4-9B + qwen3.8-27B，cache 关闭，200 token/请求）：
#   模型      n=1      n=2      n=4      同模型并发 4×4 轮输出
#   gemma4   109 t/s  156 t/s  216 t/s   与串行基准**逐字节一致**，0 错误
#   qwen27B   32 t/s   42 t/s   53 t/s   同上
# 而带 MTP 的 125B 两并发仍 100% 500。所以锁跟着 speculative 走，而不是跟着 host 走。
MLX_SERIAL_MODE = (os.environ.get("TG_MLX_SERIAL_MODE", "spec") or "spec").strip().lower()
if MLX_SERIAL_MODE not in ("spec", "model", "host"):
    MLX_SERIAL_MODE = "spec"

# 锁键的两层退化：
#   · 不同 host 是不同进程，各有自己的 BatchGenerator，互不干扰 —— 绝不能串在一起。
#     （初版用一把全局锁，多 host 时会互相阻塞，是真实的粒度错误。）
#   · 同一 host 内不同**模型**各有自己的 BatchGenerator，本来就并行；真正的串行需求来自
#     "某一个模型自己不能混批"，也就是带投机的那个。所以键按模型分，而不是按 host 分。
#   · 但不能按**请求里的模型名**分：网关看到的是别名（qwen3.8-flash →
#     qwen3.8-flash-125b → 上游 id qwen3.8-flash-next），按名字分会让同一个上游
#     模型拿到两把锁、照样混批。所以键必须用**上游 /v1/models 返回的真实 id**。
#   · 上游拿不到清单 / 名字对不上 → 退回 host 级锁（保守：不知道是谁就不放行）。
#     单模型 host 下 host 锁与旧行为完全等价，所以这条退化路径是 no-op。
_MLX_UPSTREAM_IDS_TTL = 60.0                       # 上游模型清单缓存秒数
_mlx_upstream_ids: Dict[str, tuple] = {}           # host -> (ts, {id: speculative} | None)
_mlx_upstream_ids_lock = threading.Lock()
_mlx_probe_lock = threading.Lock()                 # 防惊群：同一时刻只让一个线程去探测
_mlx_gates: Dict[str, "_PrioSerialGate"] = {}
_mlx_gates_lock = threading.Lock()


def _mlx_upstream_models(host: str):
    """上游真实模型 → 是否带投机解码（`{id: bool}`）；拿不到返回 None。

    带 TTL；**失败结果也缓存**，否则上游挂掉时每个请求都要吃一次 5s 超时。
    """
    now = time.time()
    with _mlx_upstream_ids_lock:
        hit = _mlx_upstream_ids.get(host)
        if hit and now - hit[0] < _MLX_UPSTREAM_IDS_TTL:
            return hit[1]
    with _mlx_probe_lock:
        # 拿到探测权后先复查：别的线程可能刚探完
        with _mlx_upstream_ids_lock:
            hit = _mlx_upstream_ids.get(host)
            if hit and time.time() - hit[0] < _MLX_UPSTREAM_IDS_TTL:
                return hit[1]
        models = None
        try:
            status, resp = _rapid_mlx_request(host, "GET", "/v1/models", timeout=5)
            if status == 200:
                data = json.loads(resp)
                got = {}
                for m in (data.get("data") or []):
                    mid = str(m.get("id") or "")
                    if not mid:
                        continue
                    # 字段缺失按 True 处理（保守）：宁可多排一次队，也不放进来混批
                    got[mid] = bool(m.get("speculative", True))
                models = got or None
        except Exception:
            models = None
        with _mlx_upstream_ids_lock:
            _mlx_upstream_ids[host] = (time.time(), models)
        return models


def _mlx_lock_key(host: str, model: str = "") -> str:
    """串行锁的键。返回**空串**表示该上游模型不需要排队（可安全批处理）。

    分档见 MLX_SERIAL_MODE；默认 `spec`：
      · 探测失败 → `host`（保守，等价于最早的行为）
      · 命中真实 id 且 speculative=False → `""`（不排队）
      · 命中真实 id 且 speculative=True  → `host|model`
      · 名字对不上（别名 / 未知名）→ `host`（保守：不知道是谁，就不敢放行）
    """
    if MLX_SERIAL_MODE == "host":
        return host
    models = _mlx_upstream_models(host)
    if not models:
        return host                      # 探测失败 → host 级锁（= 最早的行为）
    if model in models:
        if MLX_SERIAL_MODE == "model" or models[model]:
            return f"{host}|{model}"
        return ""                        # 不带投机 → 放行并发
    # 名字对不上（别名 / 未知名）→ 保守退 host 锁：不知道是谁就不放行
    return host


class _PrioSerialGate:
    """按 lock_key 串行，但**等待顺序按 API key 优先级**（替代 threading.Semaphore）。

    为什么换掉 Semaphore：它的唤醒顺序由解释器/内核决定，没有"谁更重要"的概念。
    于是 VIP 在 8090 的串行锁前与普通请求完全同权 —— 实测这就是 dsh 主路径
    （auto → qwen3.8-flash-next @ 8090）插不了队的真正原因：auto 解析出真实模型后
    不走 LanePool，`_acquire_conc_slot` 又因为槽位（默认 10）通常不满而不排队，
    真正那 74s / 300s 的等待全落在这把锁上。

    实现刻意与 `_acquire_conc_slot` **同构**（ticket + Condition + `_prio_turn`），
    因为"排队纪律只能有一个判定点"这条不变式比少写几行更重要：插队语义只在
    `_prio_turn` 定义一次，泳道队列、并发槽、串行锁三处共用，不会各自漂移。

    与 Semaphore 的语义差异（都是刻意的）：
      · 高优先级可以越过先到的低优先级（这是本次要的行为）；
      · 让路有窗口（LANE_PRIO_HOLD_S），过后恢复先来后到 → 不会饿死低优先级；
      · release() 用 notify_all 而不是 notify(1)：醒来后由 `_prio_turn` 判定谁该上，
        叫醒一个注定要让路的线程只是白转一圈（还容易漏掉真正该上的那个）。
    """

    __slots__ = ("_cv", "_waiters", "_seq", "_held")

    def __init__(self):
        self._cv = threading.Condition()
        self._waiters: List[dict] = []
        self._seq = itertools.count(1)
        self._held = False

    def acquire_ex(self, priority: int = 0, timeout: Optional[float] = None,
                   blocking: bool = True):
        """取锁。返回 (ok, how, waited_s, skipped)。

        how ∈ direct|queued|timeout；skipped = 我到达时排在队里、优先级比我低的
        等待者个数（>0 说明这次是插队拿到的，仅用于日志与观测）。
        """
        prio = _norm_prio(priority)
        t0 = time.monotonic()
        with self._cv:
            if not self._held:
                self._held = True
                return True, "direct", 0.0, 0
            if not blocking:
                return False, "timeout", 0.0, 0
            budget = float("inf") if timeout is None else max(0.0, float(timeout))
            now = time.time()
            skipped = sum(1 for t in self._waiters if t.get("prio", 0) < prio)
            ticket = {"prio": prio, "seq": next(self._seq), "since": now,
                      "expire_at": (now + budget) if budget != float("inf") else float("inf")}
            self._waiters.append(ticket)
            try:
                while True:
                    # strict_tie：串行锁资源唯一，同级也按到场顺序（见 _prio_turn）
                    if not self._held and _prio_turn(self._waiters, ticket,
                                                     LANE_PRIO_HOLD_S,
                                                     strict_tie=True):
                        self._held = True
                        waited = time.monotonic() - t0
                        return True, ("queued" if waited > 0.05 else "direct"), waited, skipped
                    remain = budget - (time.monotonic() - t0)
                    if remain <= 0:
                        return False, "timeout", time.monotonic() - t0, skipped
                    # 0.1s 只是兜底：让路窗口过期不会被 notify 触发，必须自己能醒
                    self._cv.wait(min(remain, 0.1))
            finally:
                try:
                    self._waiters.remove(ticket)
                except ValueError:
                    pass

    def acquire(self, priority: int = 0, timeout: Optional[float] = None,
                blocking: bool = True) -> bool:
        """Semaphore 风格的窄接口（只要成败时用它，测试也按这个语义断言）。"""
        return self.acquire_ex(priority=priority, timeout=timeout, blocking=blocking)[0]

    def release(self):
        with self._cv:
            self._held = False
            self._cv.notify_all()

    def snapshot(self) -> dict:
        now = time.time()
        with self._cv:
            return {"held": self._held, "waiting": len(self._waiters),
                    "top_prio": max((t.get("prio", 0) for t in self._waiters), default=0),
                    "max_wait_s": round(max((now - t.get("since", now)
                                             for t in self._waiters), default=0.0), 1)}


def _mlx_gate_for(key: str) -> "_PrioSerialGate":
    """取该键专属的串行闸门（懒创建）。"""
    with _mlx_gates_lock:
        gate = _mlx_gates.get(key)
        if gate is None:
            gate = _PrioSerialGate()
            _mlx_gates[key] = gate
        return gate


def _serial_gates_snapshot() -> dict:
    """串行闸门的实时视图：谁占着、几个在等、最高优先级多少、等最久的等了多久。

    只列"有动静"的键：闸门是懒创建的、永不回收，把空闲的全列出来会让这个接口
    随运行时间越长越吵（每个 host|model 组合一条）。
    """
    with _mlx_gates_lock:
        items = list(_mlx_gates.items())
    active = []
    for key, gate in items:
        s = gate.snapshot()
        if s["held"] or s["waiting"]:
            active.append({"key": key, **s})
    return {"total": len(items), "active": active}



# ── 多模型泳道（lane pool）───────────────────────────────────────────────────
# 需求（2026-11-11 晚，用户表述）：让**不同模型同时干活**，每个模型专门负责一个
# 上下文，新上下文排队等空闲模型 —— 也就是把并发做在「模型之间」，而不是「模型之内」。
#
# 为什么这样才是对的方向（前面三步实测的结论）：
#   · 125B 带 MTP：同一模型两个并发 100% 双双 500（mid-flight 注入缺陷），只能串行；
#   · 27B / 9B 不带投机：同模型 4 并发**安全**，但吞吐只有 1.65x / 1.99x（同模型
#     共享一个 BatchGenerator，解码步长被最慢的那条拖着走）；
#   · 不同模型各有自己的 BatchGenerator —— 一条在 decode、另一条在 prefill 时是真并行。
# 所以"一个模型一个上下文"能拿到的并行，是任何单模型并发都拿不到的。
#
# 三条不变量（实现和测试都围着它们转）：
#   1) **粘性**：同一上下文固定用同一个模型。否则多轮对话每次换模型，APC 前缀缓存
#      全部落空（冷 25s vs 热 0.2s，差两个数量级），而且不同模型的口吻/能力会跳变。
#   2) **独占**：一个模型同一时刻只服务一个上下文（cap 默认 1）。带投机的模型靠这条
#      避开上面那个 500；不带投机的模型也保持独占，语义才和"专门负责一个上下文"一致。
#   3) **排队而非拒绝**：模型全忙时新上下文进队列等，不直接 429。排队只在超时
#      (TG_LANE_WAIT) 或队列满 (TG_LANE_QUEUE_MAX) 时才拒绝。
#
# ── 第二步（2026-11-12 晚）：cap>1 之后，"并发"到底赚在哪里 ────────────────────
# 实测把并发收益拆成三个来源，机制必须按这三条分开算，不能笼统地"都并行就好"：
#
#   A. **跨后端并行（免费）**：不同端口/不同进程/不同模型各有自己的批处理循环。
#      gpt-oss:20b 单跑 104 tok/s，与 125B 同跑 83 / 71 —— 两边合计 154 tok/s，
#      是单跑 1.5 倍。这条只要**选不同 host** 就能拿到，代价为 0。
#   B. **同后端合批（有折扣）**：同一 Ollama 实例开 OLLAMA_NUM_PARALLEL=2 后，两个请求
#      合成一批，吞吐 1.44x，但**单个请求变慢到 1.44x 时长**（83/71 vs 单跑 104）。
#      净赚，只是赚得比 A 少。
#   C. **超过上游并行度的排队（净亏）**：NUM_PARALLEL=1 却把 cap 设成 2，两个请求在
#      Ollama 内部串行 —— 网关以为在并行，实际单请求时长直接翻倍（2.0x），吞吐一点没涨。
#      这是最危险的配置错误：**cap 是网关的意愿，NUM_PARALLEL 才是上游的能力**。
#
# 于是分派不再用"单请求速度"一把尺子，而是**并发感知的速度画像**：
#   · 每条泳道按并发档位（conc=1,2,3…）分别学 spb（每生成 token 秒数）；
#   · 预测"我要加入的那一档"（busy+1），所以往忙泳道里塞第二单会被如实定价成 1.44x；
#   · 同一时刻优先选**所在 host 最闲**的那条（把 A 类收益先吃满，再谈 B 类）；
#   · 自动降档：某档实测膨胀到 1.75x 以上（= 落在 C 类），把该泳道的有效容量压回去，
#     冷却后自动试探一次恢复。判据就是 B/C 的分界线（1.44 vs 2.0），不是拍脑袋。
#
# 配置（TG_LANES），两种写法都支持：
#   紧凑：flash=qwen3.8-flash-next|100|1,mid=qwen3.8-27b-mlx|50|1
#         名字=模型[|优先级[|容量]][@http://host:port]
#   JSON：[{"name":"flash","model":"qwen3.8-flash-next","rank":100,"cap":1}]
# 模型名允许带 `:`（Ollama tag 如 gemma4:latest），所以分隔符用 `|` 而不是 `:`。
# 这里的 `cap` 是**网关允许的并发**；上游真能跑几路由自动降档（TG_LANE_AUTOCAP）校准。
LANE_POOL_MODEL = (os.environ.get("TG_POOL_MODEL", "pool") or "pool").strip()
LANE_WAIT_S = _env_num("TG_LANE_WAIT", 600, minimum=1)
LANE_QUEUE_MAX = _env_num("TG_LANE_QUEUE_MAX", 64, minimum=1)
LANE_STICKY = os.environ.get("TG_LANE_STICKY", "1") not in ("0", "false", "no", "off")
LANE_STICKY_TTL_S = _env_num("TG_LANE_STICKY_TTL", 1800, minimum=0)
LANE_CTX_MAX = _env_num("TG_LANE_CTX_MAX", 512, minimum=1)
LANE_FAIL_LIMIT = _env_num("TG_LANE_FAIL_LIMIT", 3, minimum=1)
LANE_DOWN_COOLDOWN_S = _env_num("TG_LANE_DOWN_COOLDOWN", 60, minimum=1)
# 新上下文挑泳道时的"看一眼"预算（秒）：最高优先级的模型正在收尾时，新上下文宁可等
# 半秒也要落在好模型上。为什么需要：客户端收到最后一个字节时，服务端线程的 finally
# 可能还没跑完（还泳道），紧接着串行发出的下一个请求就会看到"最好的模型忙"而降级到
# 慢模型 —— 而这个上下文会被粘住半小时。只在**存在空闲泳道**时才等，绝不无谓阻塞。
LANE_PREFER_WAIT_S = _env_num("TG_LANE_PREFER_WAIT", 400, minimum=0) / 1000.0
# 粘性上下文的"改派预算"（秒）。默认 0 = 严格粘性：认了哪个模型就一直等它，哪怕别的
# 模型闲着 —— 这是"一个模型专门负责一个上下文"的字面语义，也是 APC 命中率最高的做法。
# 代价是队头阻塞：某上下文排在 flash 后面，mid 却空转。把它设成 >0（例如 5）就在
# 等满这个时长后**改派**到空闲模型（放弃缓存换时延）。默认关，因为改派的代价
# （一次冷 prefill，20k 上下文实测 25s）通常比多等几秒更贵。
LANE_STICKY_WAIT_S = _env_num("TG_LANE_STICKY_WAIT", 0, minimum=0)
# 冷启动时的"每 token 秒数"种子：还没测过任何一单时，所有泳道按同一个速度假设，
# 选道退化为按优先级（rank）。默认 30 tok/s（实测 125B 热缓存 ~55、27B ~15）。
LANE_DEFAULT_SPB_S = 1.0 / max(1.0, _env_num("TG_LANE_SEED_TPS", 30, minimum=1))
# 上下文标识的请求头优先级：客户端能显式给就给，给不了就靠指纹
LANE_CTX_HEADERS = ("x-tg-context", "x-context-id", "x-session-id", "x-conversation-id")
LANE_CTX_BODY_KEYS = ("conversation_id", "session_id", "context_id", "user")

# Ollama 类上游的保活间隔（秒，0=关闭）。Ollama 默认 5 分钟无请求就卸载模型，而它的
# OpenAI 兼容接口**不认**请求体里的 keep_alive（只有 /api/generate 与 /api/chat 认），
# 所以由网关自己定时发一个空 prompt 的 /api/generate 续命（实测 0.28s、不生成 token）。
# 只管"用过的泳道"：没用过的模型不该为了一条可能永远不来的请求占 13GB 内存。
LANE_KEEPALIVE_S = _env_num("TG_LANE_KEEPALIVE", 240, minimum=0)
LANE_KEEPALIVE_TTL = (os.environ.get("TG_LANE_KEEPALIVE_TTL", "15m") or "15m").strip()

# ── 自动降档：把"cap 意愿"校准到"上游并行能力" ────────────────────────────────
# 判断依据来自实测的两个数：上游开 NUM_PARALLEL=2 时，同批两个请求各自变成 1.44x 时长
# （合批，净赚）；NUM_PARALLEL=1 时第二个请求排在第一个后面，各自 2.0x 时长（净亏）。
# 所以阈值取 1.75 —— 卡在 1.44 与 2.0 之间，且要求连续多个样本（EWMA 后）才动手，
# 避免被一次冷 prefill 噪声触发。降档后冷却期内不再降；冷却到期自动试探恢复一次，
# 因此"上游后来开了 NUM_PARALLEL"这种变化不需要重启网关。
LANE_AUTOCAP = os.environ.get("TG_LANE_AUTOCAP", "1") not in ("0", "false", "no", "off")
LANE_AUTOCAP_RATIO = _env_num("TG_LANE_AUTOCAP_RATIO", 175, minimum=120) / 100.0
LANE_AUTOCAP_SAMPLES = _env_num("TG_LANE_AUTOCAP_SAMPLES", 3, minimum=1)
LANE_AUTOCAP_COOLDOWN_S = _env_num("TG_LANE_AUTOCAP_COOLDOWN", 120, minimum=1)
# 单请求样本门槛（token 数）：与速度画像同一条底线，短样本的 prefill 占比太高，
# 用它算"并发膨胀倍数"会把 20 token 的工具调用读成 3 倍慢。见 release() 的注释。
LANE_SAMPLE_MIN_TOKENS = _env_num("TG_LANE_SAMPLE_MIN", 32, minimum=1)

# ── API key 优先级（插队）────────────────────────────────────────────────────
# 优先级只有一个来源：**key 配置里的 `priority` 字段**（整数，缺省 0）。刻意不支持
# 请求头自报 —— 能自报就等于人人 VIP，优先级本身就没有意义了。改它只有两条路：
# 改 config.json，或走 /api/admin/keys（要 admin token）。
#
# 三个语义，各自对应一个真实场景：
#   · priority > 0：可以插队。泳道全忙时排在队首；全局并发槽满时有界等待而不是 429。
#   · priority = 0：默认值，行为与没有这个功能时**一致**：泳道按先来后到，槽满立即 429。
#   · priority < 0：主动让路。排队排到后面，用于"后台批处理不要挡住交互流量"。
KEY_PRIORITY_MAX = _env_num("TG_KEY_PRIORITY_MAX", 100, minimum=1)
# 高优先级请求在全局并发槽满时最多等多久（秒）。注意这不是"排队时长"而是"插队窗口"：
# 槽数（默认 10）通常远大于泳道容量（3），正常根本用不到；它只在有人把 TG_CONCURRENCY
# 压得很低时才有意义。普通 key 仍是立刻 429 —— 把所有 429 都改成排队，等于把"快速
# 失败让客户端退避"换成"一片连接挂在那里等"，那是雪崩的写法。
KEY_PRIO_SLOT_WAIT_S = _env_num("TG_KEY_PRIO_WAIT", 6, cast=float, minimum=0)
# 让路窗口（秒）：低优先级最多为更高优先级让这么久，过后恢复先来后到。没有它，持续的
# 高优先级流量会把低优先级饿死（长尾请求永远排不上），这在共享网关上比慢更糟。
LANE_PRIO_HOLD_S = _env_num("TG_LANE_PRIO_HOLD", 10, cast=float, minimum=0)
# 普通 key 遇到"有 VIP 在等空位"时最多让路这么久。它比 VIP 的等待窗口短得多：
# 让路是礼貌，不该让普通请求也变成排队。
KEY_PRIO_YIELD_S = 0.25


def _norm_prio(value) -> int:
    """把配置里的 priority 归一成整数：非法/缺失 → 0，越界夹到 ±KEY_PRIORITY_MAX。

    为什么不抛：这几行在请求路径上跑。一个写成 `"priority": "high"` 的配置不该让整个
    key 变成 500，安静地降级成"普通优先级"是唯一合理的降级方向。
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    return max(-KEY_PRIORITY_MAX, min(KEY_PRIORITY_MAX, v))


def _key_priority(key_cfg: Optional[dict]) -> int:
    """这条 API key 的调度优先级（只读配置，不看请求头）。"""
    return _norm_prio((key_cfg or {}).get("priority", 0))


def _prio_turn(waiters: List[dict], ticket: dict, hold_s: float,
               now: Optional[float] = None, strict_tie: bool = False) -> bool:
    """排队纪律：轮到我了吗？高优先级先上，同级按到场顺序，让路窗口过后恢复先来后到。

    这是整套"插队"唯一的判定点（泳道队列、全局并发槽、串行闸门共用），语义因此只有一处：
      · 我是排头 → 可以；
      · 排头优先级比我高、且它到场还没超过 hold_s → 我让一轮（这就是插队）；
      · 否则（排头优先级不高于我，或它已经等太久）→ 我也可以，避免饿死。

    `strict_tie`（2026-11-14 新增）：同级也要求"我是排头"。默认关闭，因为泳道池里
    排头可能用不上那条刚空出来的 lane（不同上下文绑定不同模型），强行让它会把可用
    资源空转。串行闸门则相反 —— 那把锁对谁都一样，资源唯一 ⇒ 排头一定能用上，于是
    可以也**应该**严格 FIFO。这一层差异源自资源性质，而不是两套纪律。
    """
    if not waiters or ticket not in waiters:
        return True
    now = time.time() if now is None else now
    alive = [t for t in waiters if t.get("expire_at", float("inf")) > now]
    head = min(alive or list(waiters), key=lambda t: (-t.get("prio", 0), t.get("seq", 0)))
    if head is ticket:
        return True
    hp, tp = head.get("prio", 0), ticket.get("prio", 0)
    if hp > tp and (now - head.get("since", now)) <= hold_s:
        return False
    # 同级也让路，但只让给"还没过期"的排头：过期的排头已经不会再来拿了，
    # 还让给它就等于把队列停在一张废票上。
    if strict_tie and hp == tp and head.get("expire_at", float("inf")) > now:
        return False
    return True


def _parse_lanes(raw: str) -> List[dict]:
    """解析 TG_LANES → [{name, model, host, rank, cap}]。非法条目跳过（不抛）。"""
    raw = (raw or "").strip()
    if not raw:
        return []
    lanes: List[dict] = []
    if raw[:1] in "[{":
        try:
            data = json.loads(raw)
        except Exception as e:
            # 这里在模块导入期执行，log() 还没定义 → 只能直接写 stderr
            print(f"⚠️ TG_LANES JSON 解析失败（{e}），泳道池不启用", file=sys.stderr)
            return []
        if isinstance(data, dict):
            data = data.get("lanes") or []
        for i, item in enumerate(data if isinstance(data, list) else []):
            if not isinstance(item, dict) or not item.get("model"):
                continue
            lanes.append({
                "name": str(item.get("name") or item["model"]),
                "model": str(item["model"]),
                "host": str(item.get("host") or "").rstrip("/"),
                "rank": int(item.get("rank") or 100 - i),
                "cap": max(1, int(item.get("cap") or 1)),
            })
        return lanes
    for i, item in enumerate(re.split(r"[,;]", raw)):
        item = item.strip()
        if not item:
            continue
        named = "=" in item
        name, spec = item.split("=", 1) if named else ("", item)
        host = ""
        if "@" in spec:
            spec, host = spec.rsplit("@", 1)
            host = host.strip().rstrip("/")
        parts = [p.strip() for p in spec.split("|")]
        model = parts[0]
        rank = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 100 - i
        cap = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        if not model:
            continue
        lanes.append({"name": (name.strip() or model), "model": model,
                      "host": host, "rank": rank, "cap": max(1, cap)})
    return lanes


def _lane_context_key(headers, req: dict) -> str:
    """一个上下文的稳定标识：显式头 > 请求体字段 > 内容指纹。

    指纹取**前两条消息**（system + 首条用户消息）：多轮对话里这两条不变，
    而后续每轮追加的历史会变，所以不能拿整段 messages 去算。
    """
    for h in LANE_CTX_HEADERS:
        v = (headers.get(h) or "").strip() if headers else ""
        if v:
            return "h:" + v[:96]
    for k in LANE_CTX_BODY_KEYS:
        v = req.get(k)
        if isinstance(v, str) and v.strip():
            return f"b:{k}:{v.strip()[:96]}"
    msgs = req.get("messages") or []
    parts = []
    for m in msgs[:2]:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = "".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
        parts.append(f"{m.get('role', '')}\x1f{str(c or '')[:4000]}")
    if not parts:
        parts = [str(req.get("prompt", ""))[:4000]]
    return "fp-" + hashlib.sha1("\x1e".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


class LaneTimeout(Exception):
    """排队等空闲模型超时。"""


class LaneQueueFull(Exception):
    """等待中的上下文已达队列上限。"""


class LanePool:
    """把上下文分派到互不干扰的模型上（一个模型 = 一条泳道）。

    线程安全：所有状态在 `self._cv` 里。典型的"资源池 + 粘性亲和 + FIFO 唤醒"。
    刻意不用每泳道一把 Semaphore：粘性 + 队列上限 + 优先级挑空闲这三件事需要
    在同一把锁里原子地决定，拆成多把锁会造出"两边都以为对方会接"的死角。
    """

    def __init__(self, lanes: List[dict], *, name: str = "", wait_s: int = 0,
                 queue_max: int = 0, sticky: bool = True, sticky_ttl_s: int = 0,
                 ctx_max: int = 0, fail_limit: int = 0, cooldown_s: int = 0,
                 prefer_wait_s: Optional[float] = None, sticky_wait_s: Optional[float] = None,
                 default_spb_s: Optional[float] = None, prio_hold_s: Optional[float] = None):
        self.name = name or LANE_POOL_MODEL
        self.wait_s = float(wait_s) if wait_s else float(LANE_WAIT_S)
        self.queue_max = int(queue_max or LANE_QUEUE_MAX)
        self.sticky = sticky
        self.sticky_ttl_s = int(sticky_ttl_s if sticky_ttl_s is not None else LANE_STICKY_TTL_S)
        self.ctx_max = int(ctx_max or LANE_CTX_MAX)
        self.fail_limit = int(fail_limit or LANE_FAIL_LIMIT)
        self.cooldown_s = float(cooldown_s or LANE_DOWN_COOLDOWN_S)
        self.prefer_wait = (LANE_PREFER_WAIT_S if prefer_wait_s is None
                            else max(0.0, float(prefer_wait_s)))
        self.sticky_wait = (LANE_STICKY_WAIT_S if sticky_wait_s is None
                            else max(0.0, float(sticky_wait_s)))
        self.default_spb = (LANE_DEFAULT_SPB_S if default_spb_s is None
                            else max(1e-5, float(default_spb_s)))
        # 让路窗口：低优先级最多为高优先级让这么久（秒，见 _prio_turn）
        self.prio_hold_s = (LANE_PRIO_HOLD_S if prio_hold_s is None
                            else max(0.0, float(prio_hold_s)))
        self._cv = threading.Condition(threading.Lock())
        self._lanes: List[dict] = []
        self._by_name: Dict[str, dict] = {}
        for spec in lanes or []:
            cap = max(1, int(spec.get("cap", 1)))
            lane = {
                "name": spec["name"], "model": spec["model"], "host": spec.get("host", ""),
                "rank": int(spec.get("rank", 0)), "cap": cap,
                "busy": 0, "served": 0, "failed": 0, "fail_streak": 0,
                "down_until": 0.0, "ctx": None, "wait_sum": 0.0, "wait_max": 0.0,
                "last_used": 0.0,
                # 速度画像：spb = 每生成 token 的实测秒数（EWMA）；未知时用默认值，
                # 于是所有泳道"看起来一样快"，选道退化成按 rank —— 首单的代价。
                # spb_by_conc 是**按并发档位分开**的画像：同一条泳道上，1 个请求和
                # 2 个合批请求的每 token 成本不是一回事（1.44x），混在一起学出来的
                # 是一个谁都不准的平均值。
                "spb": spec.get("spb"), "spb_n": 0,
                "spb_by_conc": {}, "spb_conc_n": {},
                # 并发膨胀倍数（同档实测 spb / 单档 spb）的 EWMA，自动降档的依据
                "ratio_by_conc": {}, "ratio_n": {},
                # eff_cap = 当前**有效**容量（自动降档会把它压到 cap 以下，冷却后试探恢复）
                "eff_cap": cap, "cap_down_until": 0.0, "cap_down_strikes": 0,
                "cap_reason": "",
                "job_ends": [],
                # Ollama 类上游的保活记账（见 keepalive_targets）
                "last_ping": 0.0, "ping_ok": 0, "ping_fail": 0,
            }
            self._lanes.append(lane)
            self._by_name[lane["name"]] = lane
        self._ctx: "OrderedDict[str, tuple]" = OrderedDict()   # ctx -> (lane_name, ts)
        self._waiting = 0
        self._tickets = itertools.count(1)
        # 排队登记簿：{prio, seq, since, expire_at, held}。与 _waiting 的分工 ——
        # _waiting 是"此刻卡在 wait() 里的线程数"（队列上限用它），这里记的是"谁在等、
        # 优先级多高、什么时候来的"，插队判定要用全局视野，所以不等到真阻塞就要登记。
        self._waiters: List[dict] = []
        # 取道时的并发档位（thread-local）：acquire 在请求线程里决定 conc，release 也在
        # 同一个线程里归还，所以不需要把它塞进返回值（返回值是给调用方的 2 元组契约）。
        self._tls = threading.local()
        self._metrics = {"assigns": 0, "queued": 0, "timeouts": 0,
                         "queue_full": 0, "failures": 0, "quarantines": 0,
                         "migrations": 0, "autocap_down": 0, "autocap_trials": 0,
                         # 插队：prio_inserts=高优先级拿到本来该给别人的位置；
                         # prio_holds=有人为更高优先级让路过（含被让的那个自己）
                         "prio_inserts": 0, "prio_holds": 0}

    # ---- 内部：选泳道 ----
    @property
    def enabled(self) -> bool:
        return bool(self._lanes)

    def _free(self, lane: dict, now: float) -> bool:
        return lane["busy"] < self._cap_of(lane, now) and lane["down_until"] <= now

    # ---- 有效容量：配置容量 → 被实测校准后的容量 ----
    def _cap_of(self, lane: dict, now: float) -> int:
        """当前这条泳道真正能接几路（≤ 配置 cap）。**必须在 self._cv 里调用**。

        冷却到期就试探性恢复：把 eff_cap 还给 cap，记住 reason="trial"。如果上游其实
        一直在排队，接下来的样本会重新把它压下去（strikes 翻倍，冷却变长）；如果上游
        真开了并行，就自然留在高档。这样"上游能力变了"不需要重启网关。
        """
        if lane["cap_down_until"] and now >= lane["cap_down_until"]:
            if lane["eff_cap"] < lane["cap"]:
                lane["eff_cap"] = lane["cap"]
                lane["cap_reason"] = "trial"
                self._metrics["autocap_trials"] += 1
            lane["cap_down_until"] = 0.0
        return lane["eff_cap"]

    def _host_load(self) -> Dict[str, int]:
        """每个 host 上正在跑的请求总数。同一 host = 同一个批处理循环 = 会互相拖慢；
        不同 host 才是 A 类"免费并行"。**必须在 self._cv 里调用**。"""
        load: Dict[str, int] = {}
        for l in self._lanes:
            h = l["host"] or "local"
            load[h] = load.get(h, 0) + l["busy"]
        return load

    def _pick_free(self, now: float):
        """空闲泳道里挑一条：并发档位预测最快者优先，其次 host 最闲的，再其次优先级。"""
        free = [l for l in self._lanes if self._free(l, now)]
        if not free:
            return None
        load = self._host_load()
        return min(free, key=lambda l: (self.spb_at(l, l["busy"] + 1),
                                        load.get(l["host"] or "local", 0), -l["rank"]))

    # ---- 速度画像：一条泳道"每个生成 token 要多少秒"（含 prefill，实测 EWMA）----
    # 为什么需要它：不同模型的速度差可以到 4 倍（125B+MTP 热缓存 ~0.018 s/tok，
    # 27B ~0.065 s/tok）。如果只按优先级分派，把一个请求丢给慢模型，它自己就要多花
    # 3~4 倍时间 —— 实测 3 个上下文并行反而比串行慢（0.96x）。正确的判据是
    # **"在谁那儿先做完"**：等快模型 t 秒 vs 立刻上慢模型，比的是预计完成时间。
    def spb_of(self, lane: dict) -> float:
        """单请求（conc=1）的每 token 秒数 —— 兼容旧语义与外部观测。"""
        return self.spb_at(lane, 1)

    def spb_at(self, lane: dict, conc: int) -> float:
        """**并发档位 conc 下**的每 token 秒数。

        为什么不直接复用单请求画像：往一条已经在跑 1 单的泳道塞第 2 单，那一路的真实
        成本是 1.44x（合批）或 2.0x（排队），不是 1.0x。用 1.0x 去比"等它 vs 上别的
        模型"，会系统性地高估这条泳道 —— 表现就是"两个请求都堆到最快的那条上，把它的
        批处理塞满，而另一台后端在空转"。
        """
        conc = max(1, int(conc or 1))
        table = lane["spb_by_conc"]
        if conc in table:
            return table[conc]
        if conc == 1 or not table:
            base = lane.get("spb")
            return base if base else self.default_spb
        # 没测过这一档：用已知最高档兜底（并发只会更慢，用更低的档会低估成本）。
        known = max(table)
        return table[known]

    def _wait_of(self, lane: dict, now: float) -> float:
        """这条泳道最早什么时候能接下一单（秒）。

        忙着的泳道永远给一个下限（0.05s），不返回 0：预测已经过期（实际比估算更久）
        时 ends[0]-now 会是负数，若就此返回 0，会和"真的空闲"混淆，导致排队逻辑
        一直挑这条其实拿不到的泳道。
        """
        if lane["busy"] < self._cap_of(lane, now):
            return 0.0
        ends = lane["job_ends"]
        if not ends:
            return 0.05
        return max(0.05, ends[0] - now)

    def _pred(self, lane: dict, now: float, est_tokens: int) -> float:
        """预计这条泳道把我的请求做完要多久（秒）。

        速度按"我要加入的那一档并发"取：空泳道 → conc=1（全速）；已经在跑 1 单的
        cap=2 泳道 → conc=2（合批/排队后的真实速率）。这是整套分派的核心等式。
        """
        return (self._wait_of(lane, now)
                + self.spb_at(lane, lane["busy"] + 1) * max(1, est_tokens))

    def _overdue(self, lane: dict, now: float) -> bool:
        """忙着的泳道是否已经超过预计完成时间（= 估算不准，别再为它等）。"""
        if lane["busy"] < self._cap_of(lane, now):
            return False
        ends = lane["job_ends"]
        return (not ends) or ends[0] <= now

    def _select(self, ctx: str, forced: str, now: float, est_tokens: int,
                allow_top_wait: bool = True):
        """选一条泳道，返回 (lane, reason)。lane=None 表示当前没有任何可用泳道。"""
        if forced:
            return self._by_name.get(forced), "forced"
        if self.sticky:
            rec = self._ctx.get(ctx)
            if rec and (self.sticky_ttl_s <= 0 or now - rec[1] <= self.sticky_ttl_s):
                lane = self._by_name.get(rec[0])
                if lane is not None and lane["down_until"] <= now:
                    return lane, "sticky"      # 粘性优先：忙也等它
            elif rec:
                self._ctx.pop(ctx, None)        # 粘性过期 → 当新上下文
        usable = [l for l in self._lanes if l["down_until"] <= now]
        if not usable:
            return None, "down"
        # 优先级最高的那条马上（prefer_wait 内）就空出来 → 等它，别急着降级。
        # 注意这里要求它**确实满了**（_wait_of > 0）：如果它本来就还能接（cap>1 且
        # busy<cap），就不该走"等它"的捷径 —— 那样会绕开并发档位成本，把第二个请求
        # 又塞进同一条泳道，而另一台后端在空转。空闲的情况交给下面的预测比较。
        top = max(usable, key=lambda l: (l["rank"], -l["busy"]))
        if (allow_top_wait and self._wait_of(top, now) > 0
                and not self._overdue(top, now)
                and self._wait_of(top, now) <= self.prefer_wait):
            return top, "top"
        load = self._host_load()
        return min(usable, key=lambda l: (self._pred(l, now, est_tokens),
                                          load.get(l["host"] or "local", 0),
                                          -l["rank"])), "pred"

    def _bind(self, ctx: str, lane_name: str):
        if not self.sticky:
            return
        self._ctx[ctx] = (lane_name, time.time())
        self._ctx.move_to_end(ctx)
        while len(self._ctx) > self.ctx_max:
            self._ctx.popitem(last=False)

    # ---- Ollama 类上游保活 ----
    def keepalive_targets(self, now: float, interval_s: float):
        """挑出这次该"拍一下"的泳道：只含用过、当前空闲、非 MLX 上游的。

        MLX 侧（gmlx）靠 `pin: true` 常驻，不需要也不该被 /api/generate 打扰。
        返回的是副本，调用方在锁外发 HTTP，不会拖住取道。
        """
        if interval_s <= 0:
            return []
        out = []
        with self._cv:
            for lane in self._lanes:
                if lane["busy"] >= self._cap_of(lane, now) or lane["down_until"] > now:
                    continue                       # 正在跑/隔离中：本来就热，或别添乱
                if lane["last_used"] <= 0:
                    continue                       # 从没接过单：不值得为它预占内存
                host = (lane["host"] or "").rstrip("/")
                if host and host in RAPID_MLX_HOSTS:
                    continue                       # MLX 上游：pin 常驻，跳过
                if now - lane["last_ping"] < interval_s:
                    continue
                lane["last_ping"] = now
                out.append(dict(lane))
        return out

    def note_ping(self, lane_name: str, ok: bool):
        with self._cv:
            lane = self._by_name.get(lane_name)
            if lane is None:
                return
            if ok:
                lane["ping_ok"] += 1
            else:
                lane["ping_fail"] += 1

    # ---- 对外 ----
    def admit_conc(self) -> int:
        """本线程刚取到那一单"加入时的并发档位"（acquire 写在 thread-local 里）。

        为什么不是让 acquire 返回三元组：调用方和 76 个既有断言都按 2 元组解包，
        为了一个只给画像用的标签去破坏这个契约不值得。
        """
        return max(1, int(getattr(self._tls, "conc", 1) or 1))

    def acquire(self, ctx: str, *, lane: str = "", timeout: Optional[float] = None,
                est_tokens: int = 0, priority: int = 0):
        """取一条泳道。返回 (lane_dict, 等待秒数)；超时/队满抛异常。

        `priority > 0` 时**可以插队**：允许越过先到的低优先级等待者，去拿刚空出来的
        泳道。这个判定只发生在 `_prio_turn` 一处，所以不存在"某条支路绕过优先级"。
        普通（0）与低优先级（<0）的语义不变：先来后到，超时/队满照旧。
        """
        limit = self.wait_s if timeout is None else float(timeout)
        deadline = time.monotonic() + limit
        t0 = time.monotonic()
        t_pref = None
        t_sticky = None
        with self._cv:
            # 一进来就登记，而不是等真要 wait() 时才登记：插队判定需要"谁在等"的全局
            # 视野，晚登记等于两只手同时摸最后一块，优先级来不及生效。
            # expire_at 用 wall clock，和 _prio_turn 的比较基准保持一致。
            ticket = {"prio": _norm_prio(priority), "seq": next(self._tickets),
                      "since": time.time(), "expire_at": time.time() + limit,
                      "held": False}
            self._waiters.append(ticket)
            try:
                while True:
                    now = time.time()
                    # 插队：有更高优先级的请求在等，且它还没等过头 → 我先让一轮。
                    # 只让"抢"这一步，不停表：deadline 照走，该超时还是超时。
                    if not _prio_turn(self._waiters, ticket, self.prio_hold_s, now):
                        if not ticket["held"]:
                            ticket["held"] = True
                            self._metrics["prio_holds"] += 1
                        if time.monotonic() >= deadline:
                            self._metrics["timeouts"] += 1
                            raise LaneTimeout(
                                f"等待空闲模型超过 {limit:g}s（{len(self._lanes)} 条泳道全忙，"
                                f"队列 {self._waiting}，优先级 {ticket['prio']}）")
                        # 20ms：足够让被让路的那个线程在同一轮 notify 里拿到资源，
                        # 又不至于把让路变成"空转吃 CPU"。
                        self._cv.wait(min(max(deadline - time.monotonic(), 0.01), 0.02))
                        continue
                    # 粘性上下文等太久 + 别的模型空着 → 允许改派（默认关，见 LANE_STICKY_WAIT_S）
                    if self.sticky_wait > 0 and not lane:
                        _rec = self._ctx.get(ctx)
                        _bl = self._by_name.get(_rec[0]) if _rec else None
                        if _bl is not None and _bl["busy"] >= self._cap_of(_bl, now) and self._pick_free(now) is not None:
                            if t_sticky is None:
                                t_sticky = now + self.sticky_wait
                            if now >= t_sticky:
                                self._ctx.pop(ctx, None)
                                self._metrics["migrations"] += 1
                                log(f"↔️ 上下文 {ctx} 等 {_bl['name']} 超过 {self.sticky_wait:g}s，改派")
                            else:
                                self._cv.wait(min(0.2, max(t_sticky - now, 0.01)))
                                continue
                    chosen, why = self._select(ctx, lane, now, est_tokens,
                                               allow_top_wait=(t_pref is None or now < t_pref))
                    if why == "top" and chosen is not None and chosen["busy"] >= self._cap_of(chosen, now) and t_pref is None:
                        # 开始等"最好的那条"，等满 prefer_wait 还没空出来就改看预测
                        t_pref = now + self.prefer_wait
                    if chosen is not None and chosen["busy"] < self._cap_of(chosen, now):
                        chosen["busy"] += 1
                        chosen["last_used"] = now
                        chosen["ctx"] = ctx
                        # 记下预计占用时长：队列里的后来者据此算"在谁那儿先做完"。
                        # 用**加入后这一档**的速率（合批后每 token 更慢），否则一条 cap=2 的
                        # 泳道会被算成两单都能全速跑完，排队者会误以为它马上就空。
                        chosen["job_ends"].append(
                            now + max(0.05, self.spb_at(chosen, chosen["busy"]) * max(1, est_tokens)))
                        chosen["job_ends"].sort()
                        self._bind(ctx, chosen["name"])
                        self._tls.conc = chosen["busy"]      # release 里学"这一档"的画像
                        waited = time.monotonic() - t0
                        chosen["wait_sum"] += waited
                        chosen["wait_max"] = max(chosen["wait_max"], waited)
                        self._metrics["assigns"] += 1
                        if waited > 0.05:
                            self._metrics["queued"] += 1
                        if ticket["prio"] > 0 and waited > 0.05:
                            # 只统计"等了才拿到"的高优先级：没等就拿到不算插队，
                            # 否则这个计数会随流量线性涨，看不出插队是否真的发生。
                            self._metrics["prio_inserts"] += 1
                            log(f"⏩ 优先级 {ticket['prio']} 插队拿到泳道 {chosen['name']}"
                                f"（等 {waited:.2f}s，队列 {len(self._waiters)}）")
                        return chosen, waited
                    if time.monotonic() >= deadline:
                        self._metrics["timeouts"] += 1
                        raise LaneTimeout(
                            f"等待空闲模型超过 {limit:g}s（{len(self._lanes)} 条泳道全忙，"
                            f"队列 {self._waiting}）")
                    # 队满只挡普通请求：VIP 的等待窗口本来就短，被"队满"拒掉等于插队
                    # 功能在最需要的时候失效。代价是队列上界对 VIP 不严格，可接受。
                    if self._waiting >= self.queue_max and ticket["prio"] <= 0:
                        self._metrics["queue_full"] += 1
                        raise LaneQueueFull(f"等待队列已满（{self.queue_max}），请稍后重试")
                    self._waiting += 1
                    try:
                        # 短轮询：任何一次 release 都会 notify，这里只是防丢通知的兜底
                        self._cv.wait(min(max(deadline - time.monotonic(), 0.01), 0.05))
                    finally:
                        self._waiting -= 1
            finally:
                try:
                    self._waiters.remove(ticket)
                except ValueError:
                    pass

    def release(self, lane: dict, *, error: bool = False, tokens: int = 0,
                elapsed: float = 0.0, conc: int = 0):
        _conc_hint = int(conc or getattr(self._tls, "conc", 0) or 0)
        self._tls.conc = 0          # 用掉即清：同一线程连着归还两单时，第二单不能沿用第一单的档位
        with self._cv:
            lane["busy"] = max(0, lane["busy"] - 1)
            lane["ctx"] = None
            lane["served"] += 1
            if lane["job_ends"]:
                lane["job_ends"].pop(0)
            # 更新速度画像。为什么门槛是 32 token 而不是 8：
            # elapsed 里含 prefill，工具调用那种"长 schema + 20 token 正文"的请求，
            # 实测给 125B 学出 0.0948 s/tok（≈10 tok/s，真值 98）—— 单样本把这条
            # 泳道的画像拉垮 10 倍，要 5 个正常样本才纠回来。样本长一些，prefill 占比
            # 就摊薄了。上下限再兜一道：冷 prefill 的 20k 上下文会算出 0.5 s/tok。
            if tokens >= LANE_SAMPLE_MIN_TOKENS and elapsed > 0:
                k = max(1, _conc_hint or 1)
                s = min(0.2, max(0.002, elapsed / tokens))
                prev = lane["spb_by_conc"].get(k)
                lane["spb_by_conc"][k] = s if prev is None else (0.7 * prev + 0.3 * s)
                lane["spb_conc_n"][k] = lane["spb_conc_n"].get(k, 0) + 1
                if k == 1:
                    # 单请求画像仍是"最快能多快"的基准，兼容旧的 spb/spb_n 观测
                    lane["spb"] = lane["spb_by_conc"][1]
                    lane["spb_n"] += 1
                else:
                    self._maybe_downshift(lane, k, s)
            if error:
                lane["failed"] += 1
                lane["fail_streak"] += 1
                self._metrics["failures"] += 1
                if lane["fail_streak"] >= self.fail_limit:
                    lane["down_until"] = time.time() + self.cooldown_s
                    lane["fail_streak"] = 0
                    self._metrics["quarantines"] += 1
                    dropped = [c for c, (n, _t) in self._ctx.items() if n == lane["name"]]
                    for c in dropped:
                        self._ctx.pop(c, None)
                    log(f"🚧 泳道 {lane['name']} 连续失败，隔离 {self.cooldown_s}s"
                        f"（{len(dropped)} 个上下文改派）")
            else:
                lane["fail_streak"] = 0
            self._cv.notify_all()

    def _maybe_downshift(self, lane: dict, conc: int, s: float):
        """并发档位实测比单档慢太多 → 判定"上游其实在排队"，把有效容量压回去。

        判据：ratio = 本档 spb / 单档 spb。实测边界很干净 ——
          合批（NUM_PARALLEL>=conc）：1.44x；排队（NUM_PARALLEL=1）：2.0x。
        所以默认阈值 1.75 两边都留了余量，还要 EWMA + 连续 N 个样本才动手。
        **必须在 self._cv 里调用**（由 release 持有）。
        """
        if not LANE_AUTOCAP:
            return
        base = lane["spb_by_conc"].get(1)
        if not base or lane["spb_conc_n"].get(1, 0) < 1:
            return                     # 单档还没测过，比不出倍数，先别动容量
        ratio = min(4.0, max(0.2, s / base))
        prev = lane["ratio_by_conc"].get(conc)
        lane["ratio_by_conc"][conc] = ratio if prev is None else (0.6 * prev + 0.4 * ratio)
        lane["ratio_n"][conc] = lane["ratio_n"].get(conc, 0) + 1
        if lane["ratio_n"][conc] < LANE_AUTOCAP_SAMPLES:
            return
        if lane["ratio_by_conc"][conc] < LANE_AUTOCAP_RATIO:
            return
        if lane["eff_cap"] < conc:
            return                     # 已经压到更低档了，别再重复降
        lane["eff_cap"] = max(1, conc - 1)
        lane["cap_down_strikes"] += 1
        cool = LANE_AUTOCAP_COOLDOWN_S * (2 ** min(3, lane["cap_down_strikes"] - 1))
        lane["cap_down_until"] = time.time() + cool
        lane["cap_reason"] = f"conc{conc} x{lane['ratio_by_conc'][conc]:.2f}"
        lane["ratio_n"][conc] = 0       # 降档后重新计数，避免同一批证据连着触发
        self._metrics["autocap_down"] += 1
        log(f"🔽 泳道 {lane['name']} 并发 {conc} 档实测慢 {lane['ratio_by_conc'][conc]:.2f}x"
            f"（上游并行度可能只有 {lane['eff_cap']}），有效容量 {lane['cap']}→{lane['eff_cap']}，"
            f"{cool:.0f}s 后试探恢复")

    def snapshot(self) -> dict:
        now = time.time()
        with self._cv:
            lanes = []
            for lane in self._lanes:
                lanes.append({
                    "name": lane["name"], "model": lane["model"], "host": lane["host"],
                    "rank": lane["rank"], "cap": lane["cap"], "busy": lane["busy"],
                    # eff_cap < cap 说明自动降档在起作用（上游并行度不够）；cap_reason
                    # 直接写着证据（例如 "conc2 x2.04"），排障时不用翻日志。
                    "eff_cap": self._cap_of(lane, now), "cap_reason": lane["cap_reason"],
                    "cap_down_for_s": round(max(0.0, lane["cap_down_until"] - now), 1),
                    "served": lane["served"], "failed": lane["failed"],
                    "current_context": lane["ctx"],
                    "avg_wait_s": round(lane["wait_sum"] / lane["served"], 3) if lane["served"] else 0,
                    "max_wait_s": round(lane["wait_max"], 3),
                    "spb_s_per_tok": round(self.spb_of(lane), 4),
                    "spb_by_conc": {str(k): round(v, 4) for k, v in sorted(lane["spb_by_conc"].items())},
                    "conc_ratio": {str(k): round(v, 2) for k, v in sorted(lane["ratio_by_conc"].items())},
                    "spb_samples": lane["spb_n"],
                    "est_wait_s": round(self._wait_of(lane, now), 2),
                    "down_for_s": round(max(0.0, lane["down_until"] - now), 1),
                    "idle_for_s": round(now - lane["last_used"], 1) if lane["last_used"] else None,
                    "ping_ok": lane["ping_ok"], "ping_fail": lane["ping_fail"],
                    "last_ping_for_s": (round(now - lane["last_ping"], 1)
                                        if lane["last_ping"] else None),
                })
            return {
                "pool_model": self.name, "enabled": self.enabled,
                "wait_s": self.wait_s, "queue_max": self.queue_max,
                "sticky": self.sticky, "sticky_ttl_s": self.sticky_ttl_s,
                "prefer_wait_s": round(self.prefer_wait, 3),
                "keepalive_s": LANE_KEEPALIVE_S,
                "autocap": {"on": bool(LANE_AUTOCAP), "ratio": LANE_AUTOCAP_RATIO,
                            "samples": LANE_AUTOCAP_SAMPLES,
                            "cooldown_s": LANE_AUTOCAP_COOLDOWN_S},
                "waiting": self._waiting, "contexts": len(self._ctx),
                # 优先级（插队）观测：让路窗口 + 当前队列里各优先级的人数。
                # 只有"高优先级是否真的插到了队"这个问题需要看 metrics.prio_inserts，
                # 其余（谁在等、多高、等多久）看这里。
                "priority": {
                    "hold_s": self.prio_hold_s,
                    "waiters": [{"prio": t["prio"], "seq": t["seq"],
                                 "age_s": round(now - t["since"], 2)}
                                for t in sorted(self._waiters,
                                                key=lambda t: (-t["prio"], t["seq"]))][:8],
                },
                "lanes": lanes, "metrics": dict(self._metrics),
            }


LANE_POOL = LanePool(_parse_lanes(os.environ.get("TG_LANES", "")),
                     name=LANE_POOL_MODEL, sticky=LANE_STICKY,
                     sticky_ttl_s=LANE_STICKY_TTL_S)


# ==========================

# 全局运行时状态
# state_lock 同时是 Condition：并发槽被占满时，高优先级请求要在这里**等**一个空位，
# 释放侧必须能唤醒它。`with state_lock:` 的写法与原来的 Lock 完全兼容。
state_lock = threading.Condition(threading.Lock())
key_qps_bucket: Dict[str, List[float]] = defaultdict(list)  # 每个 key 的请求时间戳
active_concurrency = 0
# 并发槽的等位登记簿（结构与泳道队列一致，插队判定共用 _prio_turn）
_conc_waiters: List[dict] = []
_conc_seq = itertools.count(1)

# ── 并发槽位回收 ─────────────────────────────────────────────────────────────
# 踩过的坑（真实事故）：客户端（挂机 bot）自己有步进预算，超时就断开重来；而网关这边
# 一旦上游还没回完，处理线程会一直阻塞到 read_timeout 才走 finally 释放槽位。
# 结果：一批"客户端已经走了"的请求把 active_concurrency 长期顶在 max_concurrency 上，
# 之后所有请求（包括健康的客户端）都拿到瞬时 429 —— 全局假死，且重启前无法自愈。
# 这里的做法：登记每个在飞请求的客户端 socket，后台每分钟巡检一次，发现对端已读关闭
# （select 可读 + MSG_PEEK 得到 b""）就立刻回收槽位，不再等上游。
# 这与"改小 read_timeout"是互补的两件事：reaper 处理"人早走了"，read_timeout 兜底"真卡住"。
_active_reqs: Dict[int, dict] = {}
_req_seq = itertools.count(1)
REAPER_INTERVAL_S = 5

# ── 上游模型清单巡检 ─────────────────────────────────────────────────────────
# 踩过的坑：模型被删掉（或被 `ollama rm` / 磁盘清理带走）之后，网关的缓存只在
# 有请求进来时才顺便刷新；而刷新失败时又"保留上一次非空结果"，于是**删掉的模型
# 会长期赖在候选池里**，auto 一直选中它、每次都被上游 404，再靠熔断挡 120s，
# 120s 后又重来一遍 —— 用户侧看到的就是"每两分钟白等一轮"。
# 这里起一个独立的巡检线程：按固定间隔无条件下拉一次清单，
#   · 上游**明确答复**（哪怕空集）→ 直接覆盖缓存，删掉的模型立刻消失；
#   · 网络失败（未知）→ 保留旧缓存，不能因为一次抖动就把候选池清空；
#   · 清单变化时顺带清理熔断表 / 能力缓存里对已经不存在的模型的记录。
MODEL_REFRESH_INTERVAL_S = _env_num("TG_MODEL_REFRESH_INTERVAL", 60, minimum=0)

# `/v1/models` 与 `/api/tags` 直接读缓存的陈旧容忍度（秒）。
# 旧实现每次列举都现场向上游发 1 + len(RAPID_MLX_HOSTS) 个同步请求（各 timeout=3s），
# 有两个后果：① 客户端轮询列表时每次可能阻塞数秒；② 任一上游抖动，`except: pass`
# 就会让列表**静默少几个模型**（甚至只剩 auto）——客户端据此缓存的结果也会跟着错。
# 现在列举只读缓存：超过这个陈旧度就**后台**触发一次刷新（不阻塞本次响应），
# 因此删掉的模型最多在一个 TTL + 一次探测耗时内从列表消失，而在的模型不会因抖动请假。
MODEL_LIST_TTL_S = _env_num("TG_MODEL_LIST_TTL", 5, minimum=0)


def _conc_limit() -> int:
    """当前全局并发上限（配置文件热改后立刻生效，不必算缓存）。"""
    try:
        return max(1, int(_cfg["global"]["max_concurrency"]))
    except Exception:
        return max(1, int(DEFAULT_MAX_CONCURRENCY))


def _acquire_conc_slot(priority: int = 0, wait_s: float = 0.0):
    """取一个全局并发槽，返回 (ok, how, waited_s)；how ∈ direct|queued|busy|timeout。

    与泳道队列的分工：泳道队列等的是"某个模型空出来"（可能几十秒），这里等的是网关
    自己的并发上限（默认 10，远大于泳道容量 3）。所以这个等待窗口很短 —— 它存在的
    意义只是别让高优先级请求被一个瞬时打满的槽位直接 429 掉。

    普通请求维持原语义：槽满就 busy，让客户端快速失败并退避。唯一例外是"槽里其实有
    空位、但队列里有 VIP 在等"时让 0.25s —— 否则刚空出来的槽会被恰好路过的普通请求
    抢走，插队就成了摆设。
    """
    global active_concurrency
    prio = _norm_prio(priority)
    t0 = time.monotonic()
    with state_lock:
        if prio <= 0 and active_concurrency >= _conc_limit():
            return False, "busy", 0.0
        budget = max(0.0, wait_s) if prio > 0 else min(KEY_PRIO_YIELD_S, max(0.0, wait_s))
        ticket = {"prio": prio, "seq": next(_conc_seq), "since": time.time(),
                  "expire_at": time.time() + budget}
        _conc_waiters.append(ticket)
        try:
            while True:
                if (active_concurrency < _conc_limit()
                        and _prio_turn(_conc_waiters, ticket, LANE_PRIO_HOLD_S)):
                    active_concurrency += 1
                    waited = time.monotonic() - t0
                    return True, ("queued" if waited > 0.05 else "direct"), waited
                remain = budget - (time.monotonic() - t0)
                if remain <= 0:
                    return False, ("timeout" if prio > 0 else "busy"), time.monotonic() - t0
                state_lock.wait(min(remain, 0.1))
        finally:
            try:
                _conc_waiters.remove(ticket)
            except ValueError:
                pass


def _drop_active_req(rid: int):
    """归还一个并发槽并唤醒等位者（请求 finally 与 reaper 共用）。

    用 notify_all 而不是 notify：等位者里普通请求和 VIP 混在一起，谁该先上由
    `_prio_turn` 在醒来之后判定；notify(1) 可能刚好叫醒一个注定要让路的线程，
    白白多转一圈（还顺手把真正该上的那个漏掉）。
    """
    global active_concurrency
    with state_lock:
        e = _active_reqs.pop(rid, None)
        if e is None or not e.get("freed"):
            active_concurrency = max(0, active_concurrency - 1)
        state_lock.notify_all()


def _slots_free_by_client_gone() -> int:
    """回收所有"客户端已断开"的在飞请求槽位，返回回收个数。"""
    global active_concurrency
    freed = 0
    with state_lock:
        for rid, e in list(_active_reqs.items()):
            sock = e.get("sock")
            gone = False
            if sock is None:
                gone = True
            else:
                try:
                    if sock.fileno() < 0:
                        gone = True
                    else:
                        r, _, _ = select.select([sock], [], [], 0)
                        if r:
                            try:
                                if sock.recv(1, socket.MSG_PEEK) == b"":
                                    gone = True
                            except (BlockingIOError, InterruptedError):
                                pass
                            except Exception:
                                gone = True
                except Exception:
                    gone = True
            if gone:
                e = _active_reqs.pop(rid, None)
                if e and not e.get("freed"):
                    e["freed"] = True
                    active_concurrency = max(0, active_concurrency - 1)
                    freed += 1
        if freed:
            # 回收出来的槽位必须唤醒等位者，否则"客户端走了"这件事对 VIP 仍然不可见，
            # 它要等到下一个请求自己结束才可能拿到位置。
            state_lock.notify_all()
    return freed


def _reaper_loop():
    while True:
        time.sleep(REAPER_INTERVAL_S)
        try:
            n = _slots_free_by_client_gone()
            if n:
                log(f"♻️  回收 {n} 个「客户端已断开」的并发槽位（客户端早走了，上游还在跑）")
        except Exception as e:
            log(f"⚠️ 槽位回收巡检异常: {type(e).__name__}: {e}")

# Rapid-MLX 熔断状态：连续 OOM 错误计数 + 最后错误时间
# 连续 >= 3 次 OOM → 下一次 Rapid-MLX 请求自动延迟 500-1500ms 给服务喘息
_mlx_circuit_lock = threading.Lock()
_mlx_oom_count: Dict[str, int] = defaultdict(int)       # {host: 连续 OOM 次数}
_mlx_last_oom_ts: Dict[str, float] = {}                  # {host: 最后 OOM 时间戳}
MLX_CIRCUIT_THRESHOLD = 3                                 # 连续 OOM 多少次触发半熔断
MLX_CIRCUIT_DELAY_BASE = 0.5                              # 半熔断基础延迟秒数
MLX_CIRCUIT_COOLDOWN = 30                                 # OOM 后 30s 内持续计数，超过则归零

_MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "*": 262144,
    # Qwen3.8 两档（当前架构）。值必须与上游实际开的窗口一致：给大了网关不会提前截断，
    # 客户端发超长上下文时上游才报 400；给小了则每次长上下文都被无谓截掉。
    "qwen3.8:27b": 262144,          # Ollama，原生 262144
    "qwen3.8-flash-125b": 131072,   # llama-server --ctx-size 131072
}

# 模型拉取任务状态
pull_tasks: Dict[str, dict] = {}  # {task_id: {name, status, progress, completed, total, error, started, finished}}
pull_lock = threading.Lock()


def _ollama_request(method: str, api_path: str, body: bytes = None, timeout: int = 30):
    """向本地 Ollama 发送请求，返回 (status_code, response_bytes)。OLLAMA_HOST 为空则返回 (0, b'')"""
    if not OLLAMA_HOST:
        return 0, b""
    url = OLLAMA_HOST.rstrip("/") + api_path
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def _rapid_mlx_request(host: str, method: str, api_path: str, body: bytes = None, timeout: int = 30):
    """向指定 Rapid-MLX host 发送 OpenAI 兼容请求，返回 (status_code, response_bytes)"""
    url = host.rstrip("/") + api_path
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    if host in RAPID_MLX_VLM_HOSTS:
        _k = os.environ.get("TG_MLX_VLM_API_KEY", "").strip()
        if _k:
            req.add_header("Authorization", f"Bearer {_k}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def _pull_worker(task_id: str, model_name: str):
    """后台线程：流式拉取 Ollama 模型"""
    url = OLLAMA_HOST.rstrip("/") + "/api/pull"
    body = json.dumps({"name": model_name, "stream": True}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=None) as resp:
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    with pull_lock:
                        if task_id not in pull_tasks:
                            return  # 任务被取消
                        t = pull_tasks[task_id]
                        if data.get("status") == "success":
                            t["status"] = "done"
                            t["progress"] = 100
                            t["finished"] = time.time()
                        else:
                            t["status"] = data.get("status", "running")
                            if data.get("total"):
                                t["total"] = data["total"]
                                t["completed"] = data.get("completed", 0)
                                t["progress"] = round(data["completed"] / data["total"] * 100, 1) if data["total"] else 0
                            elif data.get("completed"):
                                t["completed"] = data["completed"]
                except Exception:
                    continue
    except Exception as e:
        with pull_lock:
            if task_id in pull_tasks:
                pull_tasks[task_id]["status"] = "failed"
                pull_tasks[task_id]["error"] = str(e)
                pull_tasks[task_id]["finished"] = time.time()

# ========== 系统资源采集（面板实时内存 / 显存） ==========
# Apple Silicon 是统一内存架构，"显存"与"内存"同池，没有独立的 VRAM。因此这里报告三组数据：
#   1) 整机内存：vm_stat + hw.memsize，口径 = active + wired + compressed（对齐活动监视器"已用内存"）
#   2) GPU 侧：ioreg IOAccelerator 上报的 Metal 占用（In use system memory）与利用率
#   3) 进程常驻内存 Top N：libproc proc_pidinfo（等价 ps 的 RSS），不依赖 ps 命令
# 之所以不按监听端口归因：Ollama 的权重在 runner 子进程里、gmlx 的缓冲由 Metal 驱动持有，
# 监听进程自身 RSS 只有 0.2–1.1 GB，按端口归因会严重失真（实测 8181 的权重在一个独立
# llama-server 进程里占 80 GB）。枚举全部进程按 RSS 排序才反映真实占用。
SYS_CACHE_TTL = 2.0
_SYS_CACHE = {"ts": 0.0, "data": None}
_SYS_CACHE_LOCK = threading.Lock()


def _sh_text(argv, timeout=4.0):
    """执行命令并返回 stdout；失败返回空串（面板不得因采集失败而报错）。"""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


class _ProcTaskInfo(ctypes.Structure):
    """对应 macOS <libproc.h> 的 struct proc_taskinfo（96 字节）。"""
    _fields_ = [
        ("pti_virtual_size", ctypes.c_uint64),
        ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64),
        ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64),
        ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32),
        ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32),
        ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32),
        ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32),
        ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32),
        ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32),
        ("pti_priority", ctypes.c_int32),
    ]


def _libproc_init():
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        lib.proc_listpids.restype = ctypes.c_int
        lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidinfo.restype = ctypes.c_int
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                     ctypes.c_void_p, ctypes.c_int]
        lib.proc_name.restype = ctypes.c_int
        lib.proc_name.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        return lib
    except Exception:
        return None


_LIBPROC = _libproc_init()
PROC_ALL_PIDS = 1
PROC_PIDTASKINFO = 4


def _top_process_rss(limit=8):
    """枚举全部进程按 RSS 倒序返回 [{'pid','name','rss'}]。非 macOS 或库不可用时返回空。"""
    if _LIBPROC is None:
        return []
    try:
        n = _LIBPROC.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
        if n <= 0:
            return []
        cnt = n // ctypes.sizeof(ctypes.c_int)
        buf = (ctypes.c_int * (cnt * 2))()
        got = _LIBPROC.proc_listpids(PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
        if got <= 0:
            return []
        pids = [buf[i] for i in range(got // ctypes.sizeof(ctypes.c_int)) if buf[i] > 0]
        rows = []
        for pid in pids:
            info = _ProcTaskInfo()
            if _LIBPROC.proc_pidinfo(pid, PROC_PIDTASKINFO, 0,
                                     ctypes.byref(info), ctypes.sizeof(info)) > 0:
                if info.pti_resident_size > 0:
                    rows.append([info.pti_resident_size, pid])
        rows.sort(reverse=True)
        out = []
        for rss, pid in rows[:limit]:
            namebuf = ctypes.create_string_buffer(64)
            nm = "?"
            if _LIBPROC.proc_name(pid, namebuf, 64) > 0:
                nm = namebuf.value.decode("utf-8", "replace")
            out.append({"pid": pid, "name": nm, "rss": rss})
        return out
    except Exception:
        return []


def _mem_stats():
    """整机内存（字节）。解析失败时返回 None。"""
    out = _sh_text(["/usr/bin/vm_stat"])
    if not out:
        return None
    m = re.search(r"page size of (\d+) bytes", out)
    if not m:
        return None
    page = int(m.group(1))
    pages = {}
    for line in out.splitlines():
        mm = re.match(r'"?([A-Za-z][^:"]*)"?:\s+(\d+)\.?\s*$', line.strip())
        if mm:
            pages[mm.group(1).strip().lower()] = int(mm.group(2))
    if "pages active" not in pages:
        return None
    total = 0
    raw = _sh_text(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip()
    if raw.isdigit():
        total = int(raw)
    if total <= 0:
        total = int(_sh_text(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip() or 0)
    active = pages.get("pages active", 0) * page
    wired = pages.get("pages wired down", 0) * page
    compressed = pages.get("pages occupied by compressor", 0) * page
    free = pages.get("pages free", 0) * page
    inactive = pages.get("pages inactive", 0) * page
    used = active + wired + compressed
    return {
        "total": total, "used": used, "active": active, "wired": wired,
        "compressed": compressed, "free": free, "inactive": inactive,
        "used_pct": round(used / total * 100, 1) if total else 0,
    }


def _gpu_stats():
    """Metal/GPU 侧占用（统一内存中由 GPU 驱动持有的部分）。"""
    out = _sh_text(["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], timeout=5.0)
    if not out:
        return None

    def grab(key):
        mm = re.search(r'"%s"=(\d+)' % re.escape(key), out)
        return int(mm.group(1)) if mm else None

    in_use = grab("In use system memory")
    pool = grab("Alloc system memory")
    if in_use is None and pool is None:
        return None
    return {
        "in_use": in_use or 0,
        "pool": pool or 0,
        "device_util": grab("Device Utilization %"),
        "renderer_util": grab("Renderer Utilization %"),
        "tiler_util": grab("Tiler Utilization %"),
    }


def _swap_stats():
    out = _sh_text(["/usr/sbin/sysctl", "-n", "vm.swapusage"]).strip()
    if not out:
        return None

    def parse(tag):
        mm = re.search(r"%s = ([\d.]+)([MG])" % tag, out)
        if not mm:
            return 0
        val = float(mm.group(1))
        return int(val * (1073741824 if mm.group(2) == "G" else 1048576))

    total, used = parse("total"), parse("used")
    if total <= 0:
        return None
    return {"total": total, "used": used}


def collect_system_metrics():
    """面板用的系统资源快照（带 2s 缓存，避免多标签页反复触发子进程）。"""
    now = time.time()
    with _SYS_CACHE_LOCK:
        cached = _SYS_CACHE["data"]
        if cached is not None and now - _SYS_CACHE["ts"] < SYS_CACHE_TTL:
            return cached
    mem = _mem_stats()
    if mem is None:
        data = {"ok": False, "error": "unavailable", "ts": now}
    else:
        data = {
            "ok": True,
            "ts": now,
            "platform": sys.platform,
            "mem": mem,
            "gpu": _gpu_stats(),
            "swap": _swap_stats(),
            "procs": _top_process_rss(8),
        }
    with _SYS_CACHE_LOCK:
        _SYS_CACHE["data"] = data
        _SYS_CACHE["ts"] = now
    return data


# ========== 管理 UI HTML ==========
ADMIN_UI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ollama Token Gateway 管理</title>
<style>
:root{--bg:#0b0e14;--surface:#141824;--surface2:#1c2030;--border:#262b3d;--text:#e2e8f0;--dim:#64748b;--accent:#6366f1;--accent2:#8b5cf6;--green:#22c55e;--red:#ef4444;--yellow:#eab308;--blue:#3b82f6}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent);text-decoration:none}
button{cursor:pointer;font-family:inherit}
::-webkit-scrollbar{width:7px;height:7px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--dim)}
/* 登录 */
.login-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.login-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:40px;width:100%;max-width:420px}
.login-card h1{font-size:22px;margin-bottom:6px;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.login-card p{color:var(--dim);font-size:14px;margin-bottom:24px}
.login-card input{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:10px;color:var(--text);padding:12px 16px;font-size:14px;margin-bottom:12px}
.login-card input:focus{outline:none;border-color:var(--accent)}
.login-card button{width:100%;background:var(--accent);color:#fff;border:none;padding:12px;border-radius:10px;font-size:15px;font-weight:600;transition:background .2s}
.login-card button:hover{background:var(--accent2)}
.login-error{color:var(--red);font-size:13px;margin-top:8px;display:none}
/* 布局 */
.app{display:none}
.app.show{display:block}
header{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px;height:56px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:10}
header .logo{font-size:17px;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
header .live{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--dim)}
header .live .dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
header nav{display:flex;gap:4px;margin-left:auto}
header nav button{background:none;border:none;color:var(--dim);padding:8px 14px;border-radius:8px;font-size:13px;transition:all .15s}
header nav button:hover{background:var(--surface2);color:var(--text)}
header nav button.active{background:var(--accent);color:#fff}
header .logout{background:none;border:1px solid var(--border);color:var(--dim);padding:6px 12px;border-radius:8px;font-size:12px}
header .logout:hover{border-color:var(--red);color:var(--red)}
.view{display:none;padding:24px;max-width:1200px;margin:0 auto}
.view.active{display:block}
/* 卡片 */
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px}
.card h2{font-size:15px;margin-bottom:16px;display:flex;align-items:center;gap:8px}
/* 统计卡 */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;margin-bottom:16px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px}
.stat .label{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.stat .value{font-size:22px;font-weight:700}
.stat .sub{font-size:12px;color:var(--dim);margin-top:3px}
/* 进度条 */
.bar-wrap{height:6px;background:var(--bg);border-radius:3px;overflow:hidden;margin-top:8px}
.bar-fill{height:100%;border-radius:3px;transition:width .6s}
/* 系统资源（内存/显存） */
.res-head{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px}
.res-big{font-size:21px;font-weight:700;letter-spacing:-.3px}
.res-big small{font-size:13px;font-weight:500;color:var(--dim);margin-left:4px}
.res-stamp{font-size:11px;color:var(--dim)}
.res-bar{display:flex;height:11px;background:var(--bg);border-radius:6px;overflow:hidden;margin:10px 0}
.res-seg{height:100%;transition:width .6s ease}
.res-legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--dim);margin-bottom:14px}
.res-legend span{display:flex;align-items:center;gap:5px}
.res-legend i{width:8px;height:8px;border-radius:2px;display:inline-block;border:1px solid rgba(255,255,255,.14)}
.res-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:760px){.res-grid{grid-template-columns:1fr}}
.res-box{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.res-box h3{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;font-weight:600}
.res-row{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:12px;color:var(--dim);padding:3px 0}
.res-row b{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums}
.res-mini{height:4px;background:var(--bg);border-radius:2px;overflow:hidden;margin-top:5px}
.res-mini div{height:100%;border-radius:2px;transition:width .6s}
/* Key 表格 */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);padding:10px 12px;border-bottom:1px solid var(--border);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:hover td{background:var(--surface2)}
.pill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600}
.pill.on{background:rgba(34,197,94,.15);color:var(--green)}
.pill.off{background:rgba(239,68,68,.15);color:var(--red)}
.key-cell{font-family:'SF Mono',monospace;font-size:12px;color:var(--accent);cursor:pointer;white-space:nowrap}
.key-cell:hover{text-decoration:underline}
.icon-btn{background:var(--surface2);border:1px solid var(--border);color:var(--dim);padding:5px 10px;border-radius:6px;font-size:12px;transition:all .15s}
.icon-btn:hover{border-color:var(--accent);color:var(--text)}
.icon-btn.danger:hover{border-color:var(--red);color:var(--red)}
/* 按钮 */
.btn{background:var(--accent);color:#fff;border:none;padding:8px 18px;border-radius:8px;font-size:13px;font-weight:600;transition:background .2s}
.btn:hover{background:var(--accent2)}
.btn.ghost{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
.btn.ghost:hover{border-color:var(--accent)}
/* 弹窗 */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center;padding:20px}
.modal-bg.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:28px;width:100%;max-width:500px}
.modal h3{font-size:17px;margin-bottom:20px}
.modal .field{margin-bottom:14px}
.modal .field label{display:block;font-size:12px;color:var(--dim);margin-bottom:5px}
.modal .field input,.modal .field select{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:9px 12px;font-size:13px}
.modal .field input:focus{outline:none;border-color:var(--accent)}
.modal .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.modal .actions{display:flex;gap:10px;margin-top:20px;justify-content:flex-end}
/* Toast */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;padding:10px 24px;border-radius:10px;font-size:14px;z-index:200;animation:fadeUp .3s}
.toast.err{background:var(--red)}
@keyframes fadeUp{from{opacity:0;transform:translate(-50%,20px)}to{opacity:1;transform:translate(-50%,0)}}
/* 日志 */
.log-row{font-size:12px;font-family:'SF Mono',monospace;padding:8px 12px;border-bottom:1px solid var(--border);display:grid;grid-template-columns:80px 1fr 80px 70px 60px;gap:8px;align-items:center}
.log-row:hover{background:var(--surface2)}
.log-time{color:var(--dim)}
.log-key{color:var(--accent);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.log-tokens{color:var(--green);text-align:right}
.log-model{color:var(--yellow);font-size:11px}
.log-ip{color:var(--dim);font-size:11px}
/* 图表 */
.chart-wrap{position:relative;height:200px;margin-top:12px}
.chart-bar{display:flex;align-items:flex-end;gap:3px;height:180px}
.chart-col{flex:1;background:linear-gradient(180deg,var(--accent),var(--accent2));border-radius:3px 3px 0 0;min-height:2px;position:relative;transition:height .5s}
.chart-col:hover{opacity:.8}
.chart-col::after{content:attr(data-val);position:absolute;bottom:-20px;left:50%;transform:translateX(-50%);font-size:10px;color:var(--dim);white-space:nowrap}
.chart-x{display:flex;gap:3px;margin-top:24px}
.chart-x span{flex:1;text-align:center;font-size:10px;color:var(--dim)}
.empty{text-align:center;color:var(--dim);padding:40px;font-size:14px}
</style>
</head>
<body>
<!-- 登录 -->
<div class="login-wrap" id="loginWrap" style="display:none">
<div class="login-card">
<h1>Ollama Token Gateway</h1>
<p>输入管理 Token 登录管理面板</p>
<input type="password" id="adminTokenInput" placeholder="Admin Token" autofocus>
<button onclick="doLogin()">登录</button>
<div class="login-error" id="loginErr">Token 错误，请重试</div>
<div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--border);font-size:11px;color:var(--dim);line-height:1.6">
💡 提示：若已通过 Tailscale 等网络层鉴权，可将 config.json 中 <code style="color:var(--accent)">admin.token</code> 设为空字符串以跳过此登录页
</div>
</div>
</div>

<!-- 应用 -->
<div class="app" id="app">
<header>
<div class="logo">Token Gateway</div>
<div class="live"><span class="dot"></span><span id="liveStatus">在线</span></div>
<nav>
<button class="active" onclick="showView('dashboard',this)">仪表盘</button>
<button onclick="showView('keys',this)">API Keys</button>
<button onclick="showView('models',this)">模型管理</button>
<button onclick="showView('usage',this)">用量统计</button>
<button onclick="showView('logs',this)">请求日志</button>
<button onclick="showView('research',this)">深度研究</button>
</nav>
<button class="logout" onclick="logout()">退出</button>
</header>

<!-- 仪表盘 -->
<div class="view active" id="v-dashboard">
<div class="stats-grid" id="dashStats"></div>
<div class="card" id="sysCard">
<h2>💾 系统资源 <span class="res-stamp" id="sysStamp"></span></h2>
<div id="sysRes"><div class="empty">读取中…</div></div>
</div>
<div class="card">
<h2>📈 近 7 天 Token 用量</h2>
<div class="chart-wrap"><div class="chart-bar" id="chart7d"></div><div class="chart-x" id="chart7x"></div></div>
</div>
<div class="card">
<h2>🔌 连接信息（给其他设备使用）</h2>
<div style="font-size:13px;line-height:2.2;color:var(--dim)">
<div id="tsUrlRow" style="display:none">Base URL (Tailscale): <b style="color:var(--green)" id="tsUrl">-</b> <button class="icon-btn" onclick="copyText(document.getElementById('tsUrl').textContent)">复制</button></div>
<div>Base URL (局域网): <b style="color:var(--text)" id="lanUrl">-</b> <button class="icon-btn" onclick="copyText(document.getElementById('lanUrl').textContent)">复制</button></div>
<div>Base URL (本机): <b style="color:var(--text)" id="localUrl">https://localhost:3000/v1</b></div>
<div>管理面板: <b style="color:var(--text)" id="adminUrl">-</b> <button class="icon-btn" onclick="copyText(document.getElementById('adminUrl').textContent)">复制</button></div>
<div>健康检查: <b style="color:var(--text)">GET /health</b></div>
<div>兼容协议: <b style="color:var(--text)">OpenAI API (chat/completions, models, stream)</b></div>
</div>
</div>
</div>

<!-- API Keys -->
<div class="view" id="v-keys">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
<h2 style="font-size:18px">🔐 API Key 管理</h2>
<button class="btn" onclick="openKeyModal()">+ 新建 Key</button>
</div>
<div class="card" style="padding:0">
<div class="table-wrap">
<table id="keysTable">
<thead><tr><th>Key</th><th>名称</th><th>状态</th><th>今日用量</th><th>本月用量</th><th>QPS</th><th>优先级</th><th>创建</th><th>操作</th></tr></thead>
<tbody id="keysBody"><tr><td colspan="9" class="empty">加载中...</td></tr></tbody>
</table>
</div>
</div>
</div>

<!-- 用量统计 -->
<div class="view" id="v-usage">
<div class="stats-grid" id="usageStats"></div>
<div class="card">
<h2>📊 每日 Token 用量明细</h2>
<div class="table-wrap">
<table>
<thead><tr><th>日期</th><th>请求数</th><th>Prompt Tokens</th><th>Completion Tokens</th><th>总 Tokens</th></tr></thead>
<tbody id="usageBody"><tr><td colspan="5" class="empty">加载中...</td></tr></tbody>
</table>
</div>
</div>
</div>

<!-- 请求日志 -->
<div class="view" id="v-logs">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
<h2 style="font-size:18px">📜 最近请求日志</h2>
<div style="display:flex;gap:8px">
<select id="logLimit" onchange="loadLogs()" style="background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:6px 10px;font-size:13px">
<option value="20">最近 20 条</option>
<option value="50" selected>最近 50 条</option>
<option value="100">最近 100 条</option>
<option value="200">最近 200 条</option>
</select>
<button class="icon-btn" onclick="loadLogs()">🔄 刷新</button>
</div>
</div>
<div class="card" style="padding:0">
<div id="logsList"><div class="empty">加载中...</div></div>
</div>
</div>

<!-- 深度研究 (DeerFlow) -->
<div class="view" id="v-research">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
<h2 style="font-size:18px">🔬 深度研究 (DeerFlow)</h2>
<div style="display:flex;gap:8px;align-items:center">
<span id="deerflowStatus" style="font-size:12px;color:var(--dim)">检查中...</span>
<button class="icon-btn" onclick="checkDeerFlow()">🔄 检查状态</button>
<button class="icon-btn" onclick="document.getElementById('deerflowFrame').src='http://'+location.hostname+':2026'">↗ 新窗口</button>
</div>
</div>
<div class="card" style="padding:0;overflow:hidden;position:relative">
<iframe id="deerflowFrame" src="about:blank" style="width:100%;height:calc(100vh - 200px);border:0;background:var(--bg)" allow="clipboard-read;clipboard-write"></iframe>
</div>
<div style="margin-top:10px;font-size:12px;color:var(--dim);line-height:1.6">
💡 DeerFlow 是字节跳动的深度研究智能体框架，使用本机 Ollama 模型作为 LLM 后端。在左侧聊天框输入问题即可发起深度研究。
<br>⚠️ 首次加载较慢（Next.js dev 编译），请耐心等待。如 iframe 内显示空白，点击「新窗口」直接访问 <code style="color:var(--accent)">http://localhost:2026</code>
</div>
</div>
</div>

<!-- 模型管理 -->
<div class="view" id="v-models">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
<h2 style="font-size:18px">🧠 模型管理</h2>
<div style="display:flex;gap:8px">
<button class="icon-btn" onclick="loadModels()">🔄 刷新</button>
<button class="btn" onclick="openPullModal()">📥 拉取模型</button>
</div>
</div>
<div id="pullTasks" style="margin-bottom:16px"></div>
<div class="card" style="padding:0">
<div class="table-wrap">
<table>
<thead><tr><th>模型名称</th><th>大小</th><th>量化</th><th>参数量</th><th>修改时间</th><th>操作</th></tr></thead>
<tbody id="modelsBody"><tr><td colspan="6" class="empty">加载中...</td></tr></tbody>
</table>
</div>
</div>
</div>

<!-- 新建/编辑 Key 弹窗 -->
<div class="modal-bg" id="keyModal">
<div class="modal">
<h3 id="keyModalTitle">新建 API Key</h3>
<div class="field"><label>名称（标注用途，如"我的iPhone"）</label><input id="kName" placeholder="设备/用户名称"></div>
<div class="row2">
<div class="field"><label>每日 Token 上限 (0=无限)</label><input id="kDaily" type="number" value="1000000"></div>
<div class="field"><label>每月 Token 上限 (0=无限)</label><input id="kMonthly" type="number" value="30000000"></div>
</div>
<div class="row2">
<div class="field"><label>QPS 限制 (次/秒)</label><input id="kQps" type="number" value="5"></div>
<div class="field"><label>启用限流</label><select id="kLimited" style="width:100%"><option value="1">是</option><option value="0">否</option></select></div>
</div>
<div class="field"><label>调度优先级（&gt;0 可插队，0 普通，&lt;0 让路）</label><input id="kPriority" type="number" value="0" min="-100" max="100"></div>
<div class="field"><label>状态</label><select id="kActive" style="width:100%"><option value="1">启用</option><option value="0">停用</option></select></div>
<div class="actions">
<button class="btn ghost" onclick="closeKeyModal()">取消</button>
<button class="btn" onclick="saveKey()">保存</button>
</div>
</div>
</div>

<!-- 拉取模型弹窗 -->
<div class="modal-bg" id="pullModal">
<div class="modal">
<h3>📥 拉取模型</h3>
<div class="field"><label>模型名称（Ollama 库标签，如 qwen3.5:122b）</label><input id="pullName" placeholder="qwen3.5:122b" list="modelSuggestions"></div>
<datalist id="modelSuggestions">
<option value="qwen3.5:latest"><option value="qwen3.5:0.8b"><option value="qwen3.5:2b"><option value="qwen3.5:4b"><option value="qwen3.5:9b"><option value="qwen3.5:27b"><option value="qwen3.5:35b"><option value="qwen3.5:122b">
<option value="gemma4:latest"><option value="gemma4:9b"><option value="gemma4:27b"><option value="gemma4:26b">
<option value="llama3.3:70b"><option value="deepseek-r1:70b"><option value="qwen2.5-coder:32b">
</datalist>
<div style="font-size:11px;color:var(--dim);margin-top:4px;line-height:1.6">
💡 浏览 <a href="https://ollama.com/library" target="_blank">ollama.com/library</a> 查看可用模型<br>
大型模型拉取需要较长时间，可在后台运行，关闭页面不影响
</div>
<div class="actions">
<button class="btn ghost" onclick="closePullModal()">取消</button>
<button class="btn" onclick="startPull()">开始拉取</button>
</div>
</div>
</div>

<script>
let ADMIN_TOKEN=localStorage.getItem('tg_admin_token')||'';
let refreshTimer=null;

// 登录
async function doLogin(){
const t=document.getElementById('adminTokenInput').value.trim();
if(!t)return;
try{
const r=await fetch('/api/admin/status',{headers:{'X-Admin-Token':t}});
if(r.ok){ADMIN_TOKEN=t;localStorage.setItem('tg_admin_token',t);showApp();}
else{document.getElementById('loginErr').style.display='block';}
}catch{document.getElementById('loginErr').style.display='block';}
}
document.getElementById('adminTokenInput').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});
function logout(){localStorage.removeItem('tg_admin_token');ADMIN_TOKEN='';location.reload();}
function showApp(){document.getElementById('loginWrap').style.display='none';document.getElementById('app').classList.add('show');refreshAll();startRefresh();}
function ah(){return{'X-Admin-Token':ADMIN_TOKEN,'Content-Type':'application/json'}}

// 视图切换
function showView(name,btn){
document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
document.querySelectorAll('header nav button').forEach(b=>b.classList.remove('active'));
document.getElementById('v-'+name).classList.add('active');
btn.classList.add('active');
if(name==='dashboard')loadDashboard();
if(name==='keys')loadKeys();
if(name==='models')loadModels();
if(name==='usage')loadUsage();
if(name==='logs')loadLogs();
if(name==='research'){checkDeerFlow();loadDeerFlow();}
}

// DeerFlow 状态检查和加载
function checkDeerFlow(){
const st=document.getElementById('deerflowStatus');
fetch('http://'+location.hostname+':2026',{method:'HEAD',mode:'no-cors',cache:'no-cache'})
.then(()=>{st.textContent='● 在线';st.style.color='var(--green)';})
.catch(()=>{st.textContent='● 离线';st.style.color='var(--red)';});
}
function loadDeerFlow(){
const f=document.getElementById('deerflowFrame');
if(f.src==='about:blank'||f.src===''){
f.src='http://'+location.hostname+':2026';
}
}

// 自动刷新
function startRefresh(){clearInterval(refreshTimer);refreshTimer=setInterval(()=>{const a=document.querySelector('.view.active');if(a){const id=a.id;if(id==='v-dashboard')loadDashboard();if(id==='v-keys')loadKeys();if(id==='v-models')loadPullStatus();}},5000);}
function refreshAll(){loadDashboard();}

// 工具
function fmt(n){return n?n.toLocaleString():'0';}
function fmtDur(s){if(s<60)return s+'秒';if(s<3600)return Math.floor(s/60)+'分';return Math.floor(s/3600)+'时'+Math.floor((s%3600)/60)+'分';}
function pct(used,limit){if(!limit)return 0;return Math.min(100,(used/limit*100)).toFixed(1);}
function barColor(p){if(p>90)return 'var(--red)';if(p>70)return 'var(--yellow)';return 'var(--green)';}
function copyText(t){
if(!t)return;
// 优先使用 Clipboard API（需安全上下文：HTTPS 或 localhost）
if(navigator.clipboard&&window.isSecureContext){
navigator.clipboard.writeText(t).then(()=>toast('已复制')).catch(()=>fallbackCopy(t));
}else{
fallbackCopy(t);
}
}
// 降级方案：临时 textarea + execCommand（兼容 HTTP 局域网/Tailscale 访问）
function fallbackCopy(t){
const ta=document.createElement('textarea');
ta.value=t;
ta.style.position='fixed';ta.style.top='-9999px';ta.style.left='-9999px';
document.body.appendChild(ta);
ta.focus();ta.select();
try{document.execCommand('copy');toast('已复制');}catch(e){toast('复制失败，请手动复制',true);}
document.body.removeChild(ta);
}
function toast(msg,err){const d=document.createElement('div');d.className='toast'+(err?' err':'');d.textContent=msg;document.body.appendChild(d);setTimeout(()=>d.remove(),2500);}

// 仪表盘
async function loadDashboard(){
try{
const r=await fetch('/api/admin/status',{headers:ah()});
if(!r.ok){if(r.status===401){logout();return;}return;}
const d=await r.json();
const ks=Object.values(d.keys);
const totalDaily=ks.reduce((s,k)=>s+k.daily_used,0);
const totalReq=ks.reduce((s,k)=>s+k.daily_requests,0);
const totalKeys=ks.length;
const activeKeys=ks.filter(k=>k.active).length;
let lanUrl=location.hostname==='localhost'?'http://'+location.hostname+':3000/v1':location.origin+'/v1';
document.getElementById('lanUrl').textContent=lanUrl;
// Tailscale 地址（后端返回，跨网络设备用此地址）
const tsIp=d.tailscale_ip||'', tsDns=d.tailscale_dns||'';
if(tsIp||tsDns){
  const host=tsDns||tsIp;
  const tsBase='http://'+host+':3000/v1';
  document.getElementById('tsUrl').textContent=tsBase;
  document.getElementById('tsUrlRow').style.display='block';
  document.getElementById('adminUrl').textContent='http://'+host+':3000/admin';
}else{
  document.getElementById('adminUrl').textContent=location.origin+'/admin';
}
document.getElementById('liveStatus').textContent='在线 '+fmtDur(d.uptime);
document.getElementById('dashStats').innerHTML=`
<div class="stat"><div class="label">服务器状态</div><div class="value" style="color:var(--green)">${d.status}</div><div class="sub">运行 ${fmtDur(d.uptime)}</div></div>
<div class="stat"><div class="label">今日总 Token</div><div class="value" style="color:var(--accent)">${fmt(totalDaily)}</div><div class="sub">${totalReq} 次请求</div></div>
<div class="stat"><div class="label">API Keys</div><div class="value">${activeKeys}/${totalKeys}</div><div class="sub">启用/总数</div></div>
<div class="stat"><div class="label">并发</div><div class="value">${d.active_concurrency}/${d.max_concurrency}</div><div class="sub">${d.ollama}</div></div>
<div class="stat"><div class="label">日志大小</div><div class="value">${d.log_size_mb}MB</div><div class="sub">${d.listen}</div></div>
`;
renderSys(d.system);
load7dChart();
}catch(e){console.error(e);}
}

// 系统资源（内存 / 显存）——数据来自后端 collect_system_metrics()，随仪表盘 5s 轮询刷新
function gb(b){return ((b||0)/1073741824).toFixed(1);}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function resSeg(v,total,color){
const w=total?Math.min(100,Math.max(0,v/total*100)):0;
return `<div class="res-seg" style="width:${w}%;background:${color}"></div>`;
}
function renderSys(s){
const el=document.getElementById('sysRes');
if(!el)return;
if(!s||!s.ok||!s.mem){
el.innerHTML='<div class="empty">系统资源采集不可用（该能力依赖 macOS 的 vm_stat / ioreg）</div>';
return;
}
const m=s.mem,g=s.gpu||{},sw=s.swap||{},procs=s.procs||[];
const cActive='var(--accent)',cWired='var(--accent2)',cComp='var(--yellow)',cFree='#0d1220';
const avail=Math.max(0,m.total-m.used);
const pressure=m.used_pct>90?'var(--red)':(m.used_pct>75?'var(--yellow)':'var(--text)');
const bar=resSeg(m.active,m.total,cActive)+resSeg(m.wired,m.total,cWired)
+resSeg(m.compressed,m.total,cComp)+resSeg(avail,m.total,cFree);
let gpuBox='';
if(g&&(g.in_use||g.pool)){
const util=g.device_util;
// GPU 利用率高是 MLX 满载的常态，不是告警项，用主色而非红色
gpuBox=`<div class="res-box">
<h3>GPU · Metal（统一内存）</h3>
<div class="res-row"><span>驱动占用</span><b>${gb(g.in_use)} GB</b></div>
<div class="res-mini"><div style="width:${g.pool?Math.min(100,g.in_use/g.pool*100).toFixed(1):0}%;background:var(--accent)"></div></div>
<div class="res-row" style="margin-top:8px"><span>可分配池</span><b>${gb(g.pool)} GB</b></div>
<div class="res-row"><span>GPU 利用率</span><b style="color:var(--accent)">${util==null?'n/a':util+'%'}</b></div>
<div class="res-row" style="margin-top:8px;font-size:11px;opacity:.75"><span>Apple Silicon 显存与内存同池，此处为其中被 GPU 驱动持有的部分</span></div>
</div>`;
}else{
gpuBox=`<div class="res-box"><h3>GPU · Metal（统一内存）</h3><div class="res-row"><span>未采集到 GPU 数据</span></div></div>`;
}
const maxRss=procs.length?procs[0].rss:1;
const procRows=procs.map(p=>`<div class="res-row"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.name)} <span style="opacity:.6">· ${p.pid}</span></span><b>${gb(p.rss)} GB</b></div>
<div class="res-mini"><div style="width:${Math.max(1.5,p.rss/maxRss*100)}%;background:var(--accent)"></div></div>`).join('');
el.innerHTML=`
<div class="res-head">
<div class="res-big" style="color:${pressure}">${gb(m.used)}<small>/ ${gb(m.total)} GB · ${m.used_pct}% 已用</small></div>
<div class="res-stamp">可用 ${gb(avail)} GB</div>
</div>
<div class="res-bar">${bar}</div>
<div class="res-legend">
<span><i style="background:${cActive}"></i>活跃 ${gb(m.active)}G</span>
<span><i style="background:${cWired}"></i>固定 ${gb(m.wired)}G</span>
<span><i style="background:${cComp}"></i>压缩 ${gb(m.compressed)}G</span>
<span><i style="background:${cFree}"></i>空闲 ${gb(avail)}G</span>
</div>
<div class="res-grid">
${gpuBox}
<div class="res-box">
<h3>交换 / 余量</h3>
<div class="res-row"><span>Swap 已用</span><b>${sw.total?gb(sw.used)+' / '+gb(sw.total)+' GB':'未启用'}</b></div>
<div class="res-row"><span>进程常驻合计</span><b>${gb(procs.reduce((s,p)=>s+p.rss,0))} GB</b></div>
<div class="res-row"><span>压缩内存</span><b>${gb(m.compressed)} GB</b></div>
<div class="res-row"><span>缓存(inactive)</span><b>${gb(m.inactive)} GB</b></div>
</div>
</div>
<div class="res-box" style="margin-top:12px">
<h3>常驻内存 Top ${procs.length} 进程（RSS）</h3>
${procRows||'<div class="res-row"><span>无数据</span></div>'}
</div>`;
const t=new Date((s.ts||Date.now()/1000)*1000);
document.getElementById('sysStamp').textContent='更新于 '+t.toLocaleTimeString('zh-CN',{hour12:false});
}

// 7 天图表
async function load7dChart(){
try{
const r=await fetch('/api/admin/usage?days=7',{headers:ah()});
if(!r.ok)return;
const d=await r.json();
const records=d.records||[];
const max=Math.max(...records.map(r=>r.total||0),1);
document.getElementById('chart7d').innerHTML=records.map(r=>{
const h=(r.total/max*100).toFixed(0);
return `<div class="chart-col" style="height:${h}%" data-val="${fmt(r.total)}" title="${r.date}: ${fmt(r.total)} tokens, ${r.reqs}次"></div>`;
}).join('')||'<div class="empty">暂无数据</div>';
document.getElementById('chart7x').innerHTML=records.map(r=>`<span>${r.date.slice(5)}</span>`).join('');
}catch(e){console.error(e);}
}

// Keys 列表
async function loadKeys(){
try{
const r=await fetch('/api/admin/keys/list',{headers:ah()});
if(!r.ok){if(r.status===401){logout();return;}return;}
const d=await r.json();
const keys=d.keys||[];
if(!keys.length){document.getElementById('keysBody').innerHTML='<tr><td colspan="9" class="empty">暂无 Key，点击右上角新建</td></tr>';return;}
document.getElementById('keysBody').innerHTML=keys.map(k=>{
const dp=pct(k.daily_used,k.daily_tokens);
const mp=pct(k.monthly_used,k.monthly_tokens);
return `<tr>
<td class="key-cell" onclick="copyText('${k.key}')" title="点击复制完整Key">${k.key}</td>
<td>${k.name||'-'}</td>
<td><span class="pill ${k.active?'on':'off'}">${k.active?'启用':'停用'}</span></td>
<td>${fmt(k.daily_used)}<div class="bar-wrap"><div class="bar-fill" style="width:${dp}%;background:${barColor(dp)}"></div></div><div style="font-size:10px;color:var(--dim)">${dp}% / ${fmt(k.daily_tokens)}</div></td>
<td>${fmt(k.monthly_used)}<div class="bar-wrap"><div class="bar-fill" style="width:${mp}%;background:${barColor(mp)}"></div></div><div style="font-size:10px;color:var(--dim)">${mp}% / ${fmt(k.monthly_tokens)}</div></td>
<td>${k.qps}/s</td>
<td>${k.priority>0?'<span class="pill on">'+k.priority+'</span>':(k.priority<0?k.priority:0)}</td>
<td style="font-size:11px;color:var(--dim)">${k.created||'-'}</td>
<td><button class="icon-btn" onclick="editKey('${k.key}')">编辑</button> <button class="icon-btn danger" onclick="delKey('${k.key}','${k.name}')">删除</button></td>
</tr>`;}).join('');
}catch(e){console.error(e);}
}

// Key 弹窗
let editingKey=null;
function openKeyModal(){editingKey=null;document.getElementById('keyModalTitle').textContent='新建 API Key';document.getElementById('kName').value='';document.getElementById('kDaily').value=1000000;document.getElementById('kMonthly').value=30000000;document.getElementById('kQps').value=5;document.getElementById('kPriority').value=0;document.getElementById('kLimited').value='1';document.getElementById('kActive').value='1';document.getElementById('keyModal').classList.add('show');}
async function editKey(key){
try{
const r=await fetch('/api/admin/keys/list',{headers:ah()});
const d=await r.json();
const k=d.keys.find(x=>x.key===key);
if(!k)return;
editingKey=key;
document.getElementById('keyModalTitle').textContent='编辑 Key: '+(k.name||key);
document.getElementById('kName').value=k.name||'';
document.getElementById('kDaily').value=k.daily_tokens;
document.getElementById('kMonthly').value=k.monthly_tokens;
document.getElementById('kQps').value=k.qps;
document.getElementById('kPriority').value=k.priority||0;
document.getElementById('kLimited').value=k.rate_limited?'1':'0';
document.getElementById('kActive').value=k.active?'1':'0';
document.getElementById('keyModal').classList.add('show');
}catch(e){console.error(e);}
}
function closeKeyModal(){document.getElementById('keyModal').classList.remove('show');}
async function saveKey(){
const data={name:document.getElementById('kName').value.trim()||'未命名',daily_tokens:parseInt(document.getElementById('kDaily').value)||0,monthly_tokens:parseInt(document.getElementById('kMonthly').value)||0,qps:parseInt(document.getElementById('kQps').value)||5,priority:parseInt(document.getElementById('kPriority').value)||0,rate_limited:document.getElementById('kLimited').value==='1',active:document.getElementById('kActive').value==='1'};
if(editingKey)data.key=editingKey;
try{
const r=await fetch('/api/admin/keys',{method:'POST',headers:ah(),body:JSON.stringify(data)});
const d=await r.json();
if(r.ok){closeKeyModal();toast(editingKey?'已更新':'已创建: '+d.key);loadKeys();if(!editingKey)copyText(d.key);}
else{toast(d.error?.message||'保存失败',true);}
}catch(e){toast('网络错误',true);}
}
async function delKey(prefix,name){
if(!confirm('确定删除 Key "'+name+'"？此操作不可恢复。'))return;
try{
const r=await fetch('/api/admin/keys/'+prefix,{method:'DELETE',headers:ah()});
if(r.ok){toast('已删除');loadKeys();}else{toast('删除失败',true);}
}catch(e){toast('网络错误',true);}
}

// ===== 模型管理 =====
let modelRefreshTimer=null;
async function loadModels(){
try{
const r=await fetch('/api/admin/models',{headers:ah()});
if(!r.ok){if(r.status===401){logout();return;}document.getElementById('modelsBody').innerHTML='<tr><td colspan="5" class="empty">加载失败</td></tr>';return;}
const d=await r.json();
const models=d.models||[];
if(!models.length){document.getElementById('modelsBody').innerHTML='<tr><td colspan="5" class="empty">暂无模型，点击右上角拉取</td></tr>';return;}
document.getElementById('modelsBody').innerHTML=models.map(m=>{
const det=m.details||{};
const q=det.quantization_level||'-';
const fam=(det.family||det.families||'-');
const param=det.parameter_size||'-';
const ts=!m.modified?null:(typeof m.modified==='number'?(m.modified<1e12?m.modified*1000:m.modified):new Date(m.modified).getTime());
const mod=ts?new Date(ts).toLocaleDateString('zh-CN'):'-';
const isMLX=m.upstream==='rapid-mlx';
const isVirtual=m.upstream==='gateway';
const srcPill=isVirtual
  ?`<span class="pill" style="background:rgba(168,85,247,0.15);color:#a855f7;border:1px solid rgba(168,85,247,0.35)">🎲 auto</span>`
  :(isMLX
  ?`<span class="pill" style="background:rgba(34,197,94,0.15);color:#22c55e;border:1px solid rgba(34,197,94,0.3)">🟢 MLX</span>`
  :`<span class="pill" style="background:rgba(148,163,184,0.15);color:#94a3b8;border:1px solid rgba(148,163,184,0.25)">⚪ Ollama</span>`);
const sizeCell=isVirtual?'—':(m.size_gb+' GB');
return `<tr>
<td style="font-family:'SF Mono',monospace;color:var(--accent);font-size:12px">${m.name} ${srcPill}</td>
<td>${sizeCell}</td>
<td><span class="pill on">${q}</span></td>
<td>${param} <span style="color:var(--dim);font-size:11px">${fam}</span></td>
<td style="font-size:11px;color:var(--dim)">${mod}</td>
</tr>`;}).join('');
loadPullStatus();
}catch(e){console.error(e);document.getElementById('modelsBody').innerHTML='<tr><td colspan="6" class="empty">加载失败</td></tr>';}
}

async function loadPullStatus(){
try{
const r=await fetch('/api/admin/models/pull/status',{headers:ah()});
if(!r.ok)return;
const d=await r.json();
const tasks=d.tasks||[];
const active=tasks.filter(t=>t.status!=='done'&&t.status!=='cancelled'&&t.status!=='failed');
if(!tasks.length){document.getElementById('pullTasks').innerHTML='';return;}
document.getElementById('pullTasks').innerHTML=tasks.map(t=>{
const isDone=t.status==='done';
const isFailed=t.status==='failed';
const isCancelled=t.status==='cancelled';
const isRunning=!isDone&&!isFailed&&!isCancelled;
const color=isDone?'var(--green)':(isFailed?'var(--red)':(isCancelled?'var(--dim)':'var(--accent)'));
const pctVal=t.progress||0;
const doneMB=t.completed?(t.completed/1e6).toFixed(0):0;
const totalMB=t.total?(t.total/1e6).toFixed(0):0;
const sizeInfo=t.total?`${doneMB}/${totalMB} MB`:'';
const statusText=isDone?'✅ 完成':(isFailed?'❌ 失败':(isCancelled?'⊘ 已取消':'⏳ '+t.status));
return `<div class="card" style="padding:14px;margin-bottom:8px">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
<div style="font-size:13px;font-weight:600">${t.name} <span style="color:${color};font-size:12px;font-weight:400">${statusText}</span></div>
${isRunning?`<button class="icon-btn danger" onclick="cancelPull('${t.id}')">取消</button>`:''}
</div>
${isRunning?`<div class="bar-wrap"><div class="bar-fill" style="width:${pctVal}%;background:${color}"></div></div><div style="font-size:11px;color:var(--dim);margin-top:4px">${pctVal}% ${sizeInfo}</div>`:''}
${isFailed&&t.error?`<div style="font-size:11px;color:var(--red);margin-top:4px">${t.error}</div>`:''}
</div>`;}).join('');
// 如果有运行中的任务，刷新模型列表
if(active.length&&modelRefreshTimer===null){modelRefreshTimer=setTimeout(()=>{modelRefreshTimer=null;loadModels();},3000);}
}catch(e){console.error(e);}
}

function openPullModal(){document.getElementById('pullName').value='';document.getElementById('pullModal').classList.add('show');}
function closePullModal(){document.getElementById('pullModal').classList.remove('show');}
async function startPull(){
const name=document.getElementById('pullName').value.trim();
if(!name){toast('请输入模型名称',true);return;}
try{
const r=await fetch('/api/admin/models/pull',{method:'POST',headers:ah(),body:JSON.stringify({name})});
const d=await r.json();
if(r.ok){closePullModal();toast('开始拉取: '+name);loadPullStatus();}
else{toast(d.error?.message||'拉取失败',true);}
}catch(e){toast('网络错误',true);}
}
async function cancelPull(taskId){
if(!confirm('确定取消拉取任务？'))return;
try{
const r=await fetch('/api/admin/models/pull/'+taskId,{method:'DELETE',headers:ah()});
if(r.ok){toast('已取消');loadPullStatus();}else{toast('取消失败',true);}
}catch(e){toast('网络错误',true);}
}
async function delModel(name){
if(!confirm('确定删除模型 "'+name+'"？此操作不可恢复，模型文件将被永久删除。'))return;
try{
const r=await fetch('/api/admin/models/'+encodeURIComponent(name),{method:'DELETE',headers:ah()});
if(r.ok){toast('已删除');loadModels();}else{const d=await r.json().catch(()=>{});toast(d?.error?.message||'删除失败',true);}
}catch(e){toast('网络错误',true);}
}

// 用量统计
async function loadUsage(){
try{
const r=await fetch('/api/admin/usage?days=30',{headers:ah()});
if(!r.ok)return;
const d=await r.json();
const records=d.records||[];
const totalT=records.reduce((s,r)=>s+r.total,0);
const totalP=records.reduce((s,r)=>s+r.prompt,0);
const totalC=records.reduce((s,r)=>s+r.completion,0);
const totalR=records.reduce((s,r)=>s+r.reqs,0);
document.getElementById('usageStats').innerHTML=`
<div class="stat"><div class="label">30天总 Token</div><div class="value" style="color:var(--accent)">${fmt(totalT)}</div></div>
<div class="stat"><div class="label">30天 Prompt</div><div class="value" style="color:var(--blue)">${fmt(totalP)}</div></div>
<div class="stat"><div class="label">30天 Completion</div><div class="value" style="color:var(--green)">${fmt(totalC)}</div></div>
<div class="stat"><div class="label">30天请求数</div><div class="value">${fmt(totalR)}</div></div>`;
document.getElementById('usageBody').innerHTML=records.length?records.map(r=>`<tr><td>${r.date}</td><td>${r.reqs}</td><td>${fmt(r.prompt)}</td><td>${fmt(r.completion)}</td><td><b>${fmt(r.total)}</b></td></tr>`).reverse().join(''):'<tr><td colspan="5" class="empty">暂无数据</td></tr>';
}catch(e){console.error(e);}
}

// 请求日志
async function loadLogs(){
try{
const limit=document.getElementById('logLimit').value;
const r=await fetch('/api/admin/recent-logs?limit='+limit,{headers:ah()});
if(!r.ok)return;
const d=await r.json();
const logs=d.logs||[];
document.getElementById('logsList').innerHTML=logs.length?logs.map(l=>`<div class="log-row"><span class="log-time">${l.time||''}</span><span class="log-key">${l.api_key||'-'}</span><span class="log-model">${l.model||''}</span><span class="log-tokens">${fmt(l.total_tokens)}</span><span class="log-ip">${l.client_ip||''}</span></div>`).join(''):'<div class="empty">暂无日志记录</div>';
}catch(e){console.error(e);}
}

// 启动 - 自动尝试登录（admin token 为空时也可直接进入）
async function init(){
  // 先尝试已保存的 token
  if(ADMIN_TOKEN){
    try{
      const r=await fetch('/api/admin/status',{headers:{'X-Admin-Token':ADMIN_TOKEN}});
      if(r.ok){showApp();return;}
    }catch(e){}
  }
  // 尝试空 token（后端禁用 token 时直接通过）
  try{
    const r=await fetch('/api/admin/status',{headers:{'X-Admin-Token':''}});
    if(r.ok){
      ADMIN_TOKEN='';
      localStorage.setItem('tg_admin_token','');
      showApp();
      return;
    }
  }catch(e){}
  // 都失败则显示登录页
  document.getElementById('loginWrap').style.display='flex';
  document.getElementById('adminTokenInput').focus();
}
init();
</script>
</body>
</html>"""


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------- Tailscale 信息（带 60s 缓存） ----------
_ts_cache = {"ts": 0.0, "ip": "", "dns": ""}
_ts_cache_lock = threading.Lock()


def _find_tailscale_bin() -> str:
    """查找 tailscale 可执行文件绝对路径（launchd 环境 PATH 通常不含 /usr/local/bin）。"""
    for p in (
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def get_tailscale_info() -> Tuple[str, str]:
    """返回 (tailscale_ip, tailscale_dns)，失败返回 ("", "")。带 60 秒缓存。"""
    with _ts_cache_lock:
        now = time.time()
        if now - _ts_cache["ts"] < 60 and (_ts_cache["ip"] or _ts_cache["dns"]):
            return _ts_cache["ip"], _ts_cache["dns"]
        ip, dns = "", ""
        import subprocess
        bin_path = _find_tailscale_bin()
        if bin_path:
            try:
                r = subprocess.run([bin_path, "ip", "-4"], capture_output=True, text=True, timeout=3)
                if r.returncode == 0:
                    ip = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
            except Exception:
                pass
            try:
                r = subprocess.run(
                    [bin_path, "status", "--json"],
                    capture_output=True, text=True, timeout=3,
                )
                if r.returncode == 0:
                    d = json.loads(r.stdout)
                    dns = (d.get("Self", {}) or {}).get("DNSName", "").rstrip(".")
            except Exception:
                pass
        _ts_cache["ts"] = now
        _ts_cache["ip"] = ip
        _ts_cache["dns"] = dns
        return ip, dns


# ---------- 配置加载与默认值 ----------
DEFAULT_CONFIG = {
    "keys": {
        # 安全：默认不内置任何真实 key。正式 key 由 config.json（gitignored，不入库）提供。
        # 首次运行若缺失 config.json 会写出本默认配置，故此处必须为空，避免把密钥写进磁盘。
    },
    "global": {
        "max_concurrency": DEFAULT_MAX_CONCURRENCY,
        "allow_models": ["*"],          # 允许的模型，*=全部
        "deny_models": [],
        "listen_host": LISTEN_HOST,
        "listen_port": LISTEN_PORT,
    },
    "admin": {
        "token": "",  # 留空 = 依赖 Tailscale 等网络层鉴权（admin 接口开放）；暴露于不可信网络时必须设强口令
    }
}


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False))
        log(f"✅ 生成默认配置文件: {CONFIG_FILE}")
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        # 合并默认值
        for k, v in DEFAULT_CONFIG["global"].items():
            cfg.setdefault("global", {})
            cfg["global"].setdefault(k, v)
        for k, v in DEFAULT_CONFIG["admin"].items():
            cfg.setdefault("admin", {})
            cfg["admin"].setdefault(k, v)
        return cfg
    except Exception as e:
        log(f"⚠️ 配置文件解析失败，使用默认配置: {e}")
        return DEFAULT_CONFIG


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


# ---------- 用量统计 ----------
def get_today() -> str:
    return time.strftime("%Y-%m-%d")

def get_month() -> str:
    return time.strftime("%Y-%m")


# ---------- 实测耗时统计（balanced 的 speed 因子数据源） ----------
# 速度必须用**实测耗时**度量，不能用参数量反推：实测 122B（MLX + APC）在 3k~20k
# 上下文区间比 gemma4 8B（Ollama，无前缀缓存）快约 2.7 倍，"小模型一定更快"在这里不成立。
# 按 prompt 规模分桶记录真实耗时取中位数；样本不足时返回 None，由调用方给中性分，
# 避免用两三条样本决定路由。
SPEED_BUCKET_EDGES = (3000, 20000)          # prompt token 分桶边界
SPEED_BUCKET_N = len(SPEED_BUCKET_EDGES) + 1
SPEED_WINDOW = _env_num("TG_SPEED_WINDOW", 200, minimum=10)
SPEED_MIN_SAMPLES = _env_num("TG_SPEED_MIN_SAMPLES", 12, minimum=2)
# 字符→token 的粗略换算（中英混排实测约 0.5），只用于选桶，不参与计费。
# 下限定 0.01：设成 0 会让所有请求都映射到 0 token（全都落进最小桶），速度因子退化为单一桶。
TOKENS_PER_CHAR = _env_num("TG_TOKENS_PER_CHAR", 0.5, cast=float, minimum=0.01)
# 速度软映射的参考耗时：speed = tau/(tau+t)。用绝对值而非"相对最快者"，
# 否则池内一个极快的微模型（如 0.2s 的视觉微调用）会把其余候选统一压到 0.05 下限。
SPEED_TAU = _env_num("TG_SPEED_TAU", 10.0, cast=float, minimum=0.5)


# 图片占用的 prompt token：实测本机 VLM 上游按面积线性计费，约 980 tok / 百万像素
# （384²=146、768²=578、1536²=2306，三点一致）。拿不到宽高时按一张 768² 的名义值计。
IMG_TOKENS_PER_MPIX = _env_num("TG_IMG_TOKENS_PER_MPIX", 980, cast=float, minimum=1)
IMG_TOKENS_NOMINAL = _env_num("TG_IMG_TOKENS_NOMINAL", 600, minimum=1)
# 只解前 256KB base64 就够读到图头（PNG 的 IHDR / JPEG 的 SOF 都在文件前部）
_IMG_HEADER_B64 = 262144


def _min_max_tokens_for(is_rapid: bool, is_vlm: bool, has_image: bool, has_tools: bool) -> int:
    """本次请求应强制的最小 max_tokens；返回 0 表示不强制（保持客户端原值）。

    按**请求类型**而非主机判定：122B 同时挂在 RAPID_MLX_HOSTS 与 RAPID_MLX_VLM_HOSTS
    上，纯文本的 agent 请求同样会命中 VLM 豁免；而视觉调用要的是"短结构化输出 +
    低延迟"，把 200 抬到 32768 会让模型长篇胡写，单次请求从 ~30s 涨到 10 分钟，
    期间一直占着 global 并发额度（实测 10 个槽位被占满 → 所有客户端瞬时 429）。

    非 Rapid-MLX（Ollama）侧必须有**纯文本下限**：gemma4 这类会"思考"的模型先把预算
    烧在思维链上，预算耗尽时 content 为空、finish_reason=length，客户端只看到一条空
    回复（实测 max_tokens=64 → content 0 字符 / reasoning 233 字符 / completion=64）。
    """
    if is_rapid:
        if not is_vlm:
            return _env_num("TG_MIN_MAX_TOKENS", 32768, minimum=0)
        if has_image:
            return 0
        if has_tools:
            return _env_num("TG_MIN_MAX_TOKENS_TOOLS", 8192, minimum=0)
        return 0
    # Ollama 等非 Rapid-MLX 上游
    if has_image:
        return 0                      # 视觉调用要短输出，维持原语义
    if has_tools:
        return _env_num("TG_MIN_MAX_TOKENS_TOOLS", 8192, minimum=0)
    return _env_num("TG_MIN_MAX_TOKENS_TEXT", 2048, minimum=0)


def _data_uri_megapixels(url) -> Optional[float]:
    """从 base64 data URI 读出图像面积（百万像素）；非 data URI 或解析失败返回 None。

    只解 PNG 与 JPEG 的图头，不解码像素：目的是估算 prompt token，不需要图像内容。
    不支持远程 http(s) URL —— 那里取宽高要额外发一次请求，不值得放在请求路径上。
    """
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return None
    head, _, b64 = url.partition(",")
    if "base64" not in head or not b64:
        return None
    try:
        # 截断后长度必须是 4 的倍数，否则 b64decode 会因填充错误抛异常
        raw = base64.b64decode(b64[:_IMG_HEADER_B64][: (_IMG_HEADER_B64 // 4) * 4])
    except Exception:
        return None
    w = h = 0
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
        try:
            w, h = struct.unpack(">II", raw[16:24])
        except struct.error:
            return None
    elif raw[:2] == b"\xff\xd8":
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        i = 2
        while i + 9 <= len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker == 0xFF:          # 填充字节
                i += 1
                continue
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2                  # 无载荷段
                continue
            seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
            if seg < 2:
                return None
            if marker in sof:
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                break
            i += 2 + seg
    if not w or not h or w * h > 50_000_000:   # 超过 50MP 视为异常数据，不采信
        return None
    return w * h / 1e6


def _latency_names(model: str) -> Tuple[str, ...]:
    """把模型名展开成「上游 id + 别名」两种记账键。

    必须两个方向都覆盖：auto 候选池里的模型名可能是上游 id（快照内取到的名字），
    而用量日志里记的可能是别名（客户端调用时写的那一侧）。只记一边就会出现
    「实时跑过的请求能查到速度样本，重启后同一模型却查不到」的错位。
    """
    if not model:
        return ()
    names = [model]
    alt = _MODEL_REVERSE_ALIAS.get(model) or MODEL_ALIASES.get(model)
    if alt and alt != model:
        names.append(alt)
    return tuple(names)


class LatencyStats:
    """按 (模型, prompt 桶) 记录真实耗时，供 speed 因子取中位数。"""

    def __init__(self):
        self._data: Dict[str, List[deque]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def bucket_of(prompt_tokens: int) -> int:
        pt = max(0, int(prompt_tokens or 0))
        for i, edge in enumerate(SPEED_BUCKET_EDGES):
            if pt < edge:
                return i
        return SPEED_BUCKET_N - 1

    def record(self, model: str, prompt_tokens: int, elapsed_s: float) -> bool:
        """写入一条实测耗时；模型名会按「id + 别名」双记。返回是否真的记了。"""
        if not model or elapsed_s is None or elapsed_s <= 0:
            return False
        names = _latency_names(model)
        if not names:
            return False
        b = self.bucket_of(prompt_tokens)
        val = float(elapsed_s)
        with self._lock:
            for name in names:
                buckets = self._data.get(name)
                if buckets is None:
                    buckets = [deque(maxlen=SPEED_WINDOW) for _ in range(SPEED_BUCKET_N)]
                    self._data[name] = buckets
                buckets[b].append(val)
        return True

    def estimate(self, model: str, prompt_tokens: int) -> Optional[float]:
        """该模型在给定 prompt 规模下的典型耗时（秒）；该桶样本不足返回 None。

        不做跨桶回退：不同规模量级的耗时不可互换，用别的桶顶替等于编造一个数字
        （实测 8B 在 3k~20k 桶无样本时曾被 0.5s 的短请求样本"冒充"成 1.24s）。
        宁可返回 None 让上游给中性分，也不拿不可比的样本充数。
        """
        if not model:
            return None
        b = self.bucket_of(prompt_tokens)
        with self._lock:
            buckets = self._data.get(model)
            if not buckets:
                return None
            samples = list(buckets[b])
        if len(samples) < SPEED_MIN_SAMPLES:
            return None
        samples.sort()
        return samples[len(samples) // 2]

    def seed_from_usage(self, path: Path, max_rows: int = 20000) -> int:
        """启动时用最近的用量日志预热，避免重启后 speed 因子失明。"""
        if not path.exists():
            return 0
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                block = min(size, 8 * 1024 * 1024)
                f.seek(max(0, size - block))
                tail = f.read().decode("utf-8", "replace").splitlines()[-max_rows:]
        except Exception:
            return 0
        n = 0
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if self.record(r.get("model", ""), r.get("prompt_tokens", 0), r.get("elapsed_s") or 0):
                n += 1
        return n

    def snapshot(self) -> Dict[str, List[int]]:
        """每个模型的样本数，供 /stats 与排查用。"""
        out: Dict[str, List[int]] = {}
        with self._lock:
            for m, buckets in self._data.items():
                out[m] = [len(q) for q in buckets]
        return out


MODEL_LATENCY = LatencyStats()


class UsageTracker:
    """跟踪每个 Key 的用量，内存统计 + JSONL 持久化"""

    def __init__(self):
        self.daily: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))    # {key: {date: tokens}}
        self.monthly: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))  # {key: {month: tokens}}
        self.req_count: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._load_from_log()

    def _load_from_log(self):
        if not USAGE_LOG.exists():
            return
        loaded = 0
        try:
            with open(USAGE_LOG) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        k = r.get("api_key", "unknown")
                        d = r.get("date", get_today())
                        m = d[:7]
                        t = r.get("total_tokens", 0)
                        self.daily[k][d] += t
                        self.monthly[k][m] += t
                        self.req_count[k][d] += 1
                        loaded += 1
                    except Exception:
                        continue
            if loaded:
                log(f"📊 已从历史日志恢复 {loaded} 条用量记录")
        except Exception as e:
            log(f"⚠️ 读取历史日志失败: {e}")

    def record(self, api_key: str, model: str, prompt_tokens: int,
               completion_tokens: int, total_tokens: int, elapsed_s: float,
               client_ip: str, stream: bool):
        today = get_today()
        month = today[:7]

        # 速度因子数据源：record 内部会按「上游 id + 别名」双记，这里只需调一次
        MODEL_LATENCY.record(model, prompt_tokens, elapsed_s)

        with state_lock:
            self.daily[api_key][today] += total_tokens
            self.monthly[api_key][month] += total_tokens
            self.req_count[api_key][today] += 1

        # JSONL 持久化
        rec = {
            "ts": int(time.time()),
            "date": today,
            "time": time.strftime("%H:%M:%S"),
            "api_key": api_key,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "elapsed_s": round(elapsed_s, 2),
            "client_ip": client_ip,
            "stream": stream,
        }
        try:
            with open(USAGE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            log(f"⚠️ 写入用量日志失败: {e}")

        log(f"📊 [{client_ip}] Key:{api_key} Model:{model} Tokens:{total_tokens} (P:{prompt_tokens}+C:{completion_tokens}) 用时:{elapsed_s:.1f}s")

    def check_quota(self, api_key: str, key_cfg: dict, est_tokens: int) -> Tuple[bool, str]:
        """检查额度是否超限，返回(是否允许, 原因)"""
        today = get_today()
        month = today[:7]
        with state_lock:
            used_d = self.daily[api_key].get(today, 0)
            used_m = self.monthly[api_key].get(month, 0)

        limit_d = key_cfg.get("daily_tokens", 0)
        limit_m = key_cfg.get("monthly_tokens", 0)

        if limit_d > 0 and used_d + est_tokens > limit_d:
            return False, f"日额度超限: 已用 {used_d}/{limit_d} tokens"
        if limit_m > 0 and used_m + est_tokens > limit_m:
            return False, f"月额度超限: 已用 {used_m}/{limit_m} tokens"
        return True, "ok"

    def usage_summary(self, api_key: Optional[str] = None) -> dict:
        today = get_today()
        month = today[:7]
        result = {"today": today, "month": month, "keys": {}}

        keys = [api_key] if api_key else list(tracker.daily.keys())
        with state_lock:
            for k in keys:
                result["keys"][k] = {
                    "daily_tokens": self.daily[k].get(today, 0),
                    "monthly_tokens": self.monthly[k].get(month, 0),
                    "daily_requests": self.req_count[k].get(today, 0),
                }
        return result


tracker = UsageTracker()


# ---------- 限流 ----------
def check_qps(api_key: str, limit: int) -> bool:
    """简单滑动窗口 QPS 限制。返回 True=允许"""
    if limit <= 0:
        return True
    now = time.time()
    with state_lock:
        bucket = [t for t in key_qps_bucket[api_key] if now - t < 1.0]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        key_qps_bucket[api_key] = bucket
        return True


# ---------- HTTP 请求处理器 ----------
class GatewayHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        return  # 禁用默认 HTTP 日志

    # ---- 工具方法 ----
    def _real_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def handle_one_request(self):
        """全局兜底：捕获所有未处理异常，防止单个请求异常导致线程崩溃。"""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端断开，静默处理
        except Exception as e:
            log(f"❌ handle_one_request 未捕获异常: {type(e).__name__}: {e}")
            try:
                self._send_error(500, "internal_error", "服务器内部错误")
            except Exception:
                pass

    def _send_json(self, data, status: int = 200, extra_headers: Optional[dict] = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端已断开，静默处理

    def _send_error(self, status: int, code: str, message: str):
        self._resp_status = status      # 供泳道归还时判断这条是不是上游故障
        self._send_json({
            "error": {
                "message": message,
                "type": code,
                "code": code,
            }
        }, status=status)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization,Content-Type,Accept,X-Admin-Token")
        self.send_header("Access-Control-Expose-Headers",
                         "X-Token-Usage,X-Quota-Remaining,X-RateLimit-Limit,"
                         "X-TG-Priority,X-Gateway-Lane,X-Gateway-Model")

    # ---- 鉴权提取 ----
    def _extract_api_key(self) -> Optional[str]:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        # 兼容 query 参数 ?api_key=xxx
        if "api_key=" in self.path:
            try:
                q = self.path.split("?", 1)[1]
                for p in q.split("&"):
                    if p.startswith("api_key="):
                        from urllib.parse import unquote
                        return unquote(p[8:])
            except Exception:
                pass
        return None

    def _check_model_allowed(self, model: str, cfg: dict) -> bool:
        allow = cfg["global"].get("allow_models", ["*"])
        deny = cfg["global"].get("deny_models", [])
        if model in deny:
            return False
        if "*" in allow or model in allow:
            return True
        return False

    # 多上游模型列表缓存（30 秒）
    _ollama_models_cache = {"ts": 0.0, "models": set()}
    _rapid_mlx_models_cache = {"ts": 0.0, "models": {}}  # {model_id: host}
    # ollama 模型元数据（parameter_size / quantization_level），供 auto 质量排序使用
    _ollama_meta_cache = {"ts": 0.0, "meta": {}}
    _models_cache_lock = threading.Lock()

    # 已失效模型熔断表 {model: 到期时间戳}。
    # 背景：`/api/tags` 只报告"manifest 在不在",不保证权重可加载。当模型被删除、
    # 权重损坏、或拉取中途中断时，它仍可能留在列表里，直到上游自己判定失败。
    # 此时 auto 会反复选中它，把上游的 404 原样打回客户端——用户侧表现为
    # "明明有别的模型可用，却每次都报错"。命中一次不可用就熔断一段时间，
    # 让候选池自动跳过它，而不是靠人工删配置。
    _bad_models: dict = {}
    _bad_models_lock = threading.Lock()
    # 连续失败计数 {model: n}：熔断时长按指数退避递增（120s → 240s → 480s …），
    # 上限 TG_BAD_MODEL_TTL_MAX。这样"真坏掉"的模型不会每 2 分钟就骗走一次上游往返，
    # 而偶然抖动一次的模型仍能很快回到候选池（第 1 次只有 120s）。
    _fail_counts: dict = {}
    _fail_counts_lock = threading.Lock()
    # 上一次"上游确实答复过"的模型清单，用于识别新增/消失（见 _sync_model_registry）
    _known_models: set = set()
    # 单飞标记：同一时刻只让一个线程去探测上游模型列表，其余请求直接用现有缓存。
    # 否则缓存一过期，N 个并发请求会各自等满 timeout（每主机 3s）——表现为整体卡顿。
    _ollama_refreshing = False
    _rapid_refreshing = False
    # 上游**原始**条目，供 /v1/models 与 /api/tags 原样透出（保留 details / size 等字段，
    # 避免客户端因字段缺失而认不出模型）。与上面的 set/dict 缓存同源同寿命。
    _ollama_raw_models: list = []
    _rapid_raw_models: list = []   # [{"id": 原始 id, "host": host}]
    # 列举接口的后台刷新单飞标记（与巡检线程的标记分开，互不干扰）
    _list_refreshing = False

    @classmethod
    def _mark_model_bad(cls, model: str, reason: str = "") -> None:
        """把模型记为不可用并按**连续失败次数**指数退避熔断。

        404（模型加载失败 / 权重缺失）与拥堵走同一套"从候选池里摘掉"的逻辑，
        区别只在恢复条件：拥堵看实时的在飞计数，这里看退避计时器 + 清单巡检。
        """
        if not model:
            return
        base = _env_num("TG_BAD_MODEL_TTL", 120, minimum=0)
        if base <= 0:
            return
        cap = _env_num("TG_BAD_MODEL_TTL_MAX", 3600, minimum=0)
        with cls._fail_counts_lock:
            n = cls._fail_counts.get(model, 0) + 1
            cls._fail_counts[model] = n
        ttl = base * (2 ** (n - 1))
        if cap > 0:
            ttl = min(ttl, cap)
        with cls._bad_models_lock:
            cls._bad_models[model] = time.time() + ttl
        log(f"🚫 模型熔断 {ttl:.0f}s（连续第 {n} 次失败）：{model}"
            + (f"（{reason}）" if reason else ""))

    @classmethod
    def _is_model_bad(cls, model: str) -> bool:
        if not model:
            return False
        with cls._bad_models_lock:
            exp = cls._bad_models.get(model)
            if exp is None:
                return False
            if exp <= time.time():
                cls._bad_models.pop(model, None)
                return False
            return True

    @classmethod
    def _mark_model_good(cls, model: str) -> None:
        if not model:
            return
        with cls._fail_counts_lock:
            n = cls._fail_counts.pop(model, 0)
        with cls._bad_models_lock:
            if cls._bad_models.pop(model, None) is not None:
                log(f"✅ 模型恢复：{model}" + (f"（此前连续失败 {n} 次）" if n else ""))

    # ---- 工具能力缓存 {model: (supports: bool, 到期时间戳)} ----
    # 背景（2026-11-08）：Ollama 的视觉/嵌入模型（如 qwen2.5vl:3b）对话完全正常，
    # 但**不接受 tools**，上游会回
    #   400 {"message":"registry.ollama.ai/library/qwen2.5vl:3b does not support tools"}
    # auto 的分档只看请求体量（工具清单不计入 chars），一条带 tools 的短请求会被判成
    # 「短问答」轻量档，于是正好选中这类小 VLM —— 表现就是"开了工具调用，每个请求都 400"。
    # 这里把能力探测结果缓存起来，候选池在带 tools 时按需过滤；同时把上游 400 当作
    # 能力信息（而不是模型损坏）记下来，**不能进熔断表**——它对普通对话仍然可用。
    _tool_caps: dict = {}
    _tool_caps_lock = threading.Lock()

    @classmethod
    def _tool_cap_get(cls, model: str) -> Optional[bool]:
        with cls._tool_caps_lock:
            entry = cls._tool_caps.get(model)
            if entry is None:
                return None
            supports, exp = entry
            if exp <= time.time():
                cls._tool_caps.pop(model, None)
                return None
            return supports

    @classmethod
    def _tool_cap_set(cls, model: str, supports: bool, ttl: Optional[float] = None) -> None:
        if not model:
            return
        if ttl is None:
            ttl = _env_num("TG_TOOL_CAP_TTL", 600, minimum=0)
        if ttl <= 0:
            return
        with cls._tool_caps_lock:
            cls._tool_caps[model] = (bool(supports), time.time() + ttl)

    @classmethod
    def _mark_model_no_tools(cls, model: str) -> None:
        """上游明确回「不支持 tools」：这是能力边界，不是模型坏了，别写进熔断表。"""
        if not model:
            return
        cls._tool_cap_set(model, False)
        log(f"🧰 记下能力边界：{model} 不支持工具调用（后续带 tools 的请求会跳过它）")

    @classmethod
    def _fetch_ollama_models(cls) -> Optional[set]:
        """拉取 ollama 模型名集合；顺带缓存 details（参数量/量化）供 auto 质量排序使用。

        返回值语义（**关键**，曾因混用而出过问题）：
          · `set`（可以是空集）—— 上游确实答复了，这就是它当前的清单。
            空集是合法状态（模型被全部删除），调用方应当用它覆盖缓存。
          · `None` —— 网络/解析失败，**未知**。调用方必须保留上一次缓存，
            否则一次 3 秒抖动就会把候选池清空，所有 auto 请求变 503。

        旧实现两种情况都返回 `set()`，调用方用 `if models or not 旧缓存` 兜底，
        结果"上游答复空清单"被当成失败丢弃 → 已删除的模型长期留在列表里。
        """
        try:
            r = urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3)
            if r.status != 200:
                return None
            data = json.loads(r.read())
            models = set()
            meta = {}
            raw = []
            for m in data.get("models", []) or []:
                name = m.get("name")
                if not name:
                    continue
                models.add(name)
                raw.append(m)
                det = m.get("details") or {}
                meta[name] = {
                    "param": det.get("parameter_size") or "",
                    "quant": det.get("quantization_level") or "",
                    "family": det.get("family") or "",
                    "size_gb": round((m.get("size") or 0) / 1e9, 2),
                }
            # 成功答复即覆盖元数据（空清单也要覆盖，否则被删模型的参数量会一直留着）
            cls._ollama_meta_cache["meta"] = meta
            cls._ollama_meta_cache["ts"] = time.time()
            cls._ollama_raw_models = raw
            return models
        except Exception:
            return None

    @classmethod
    def _fetch_rapid_mlx_models(cls) -> Optional[dict]:
        """返回 {model: host}；全部主机都探测失败时返回 None（未知），否则返回字典。"""
        result = {}
        raw = []
        answered = 0
        for host in RAPID_MLX_HOSTS:
            try:
                _headers = {}
                if host in RAPID_MLX_VLM_HOSTS:
                    _headers["Authorization"] = f"Bearer {MLX_VLM_API_KEY}"
                _req = urllib.request.Request(f"{host}/v1/models", headers=_headers)
                r = urllib.request.urlopen(_req, timeout=3)
                if r.status == 200:
                    answered += 1
                    data = json.loads(r.read())
                    for m in data.get("data", []):
                        mid = m["id"]
                        if "/" not in mid or "/" not in result:
                            result[mid] = host
                        raw.append({"id": mid, "host": host})
            except Exception:
                continue
        if answered:
            cls._rapid_raw_models = raw
            return result
        return None

    @classmethod
    def _wait_inflight_refresh(cls) -> None:
        """force 路径专用：若有刷新正在飞，等它落地（有界），别让"强刷"拿到旧值。

        为什么需要：单飞标记会让后来者直接跳过探测。对后台刷新这是对的（本来就
        不想排队），但对 `?refresh=1` 是错的 —— 用户明确要最新，却可能正好撞上
        巡检线程在飞，于是拿回一份旧清单，表现为"刚删掉的模型还在"。
        """
        wait = _env_num("TG_LIST_REFRESH_WAIT", 5, minimum=0)
        if wait <= 0:
            return
        deadline = time.time() + wait
        while time.time() < deadline:
            with cls._models_cache_lock:
                busy = cls._ollama_refreshing or cls._rapid_refreshing
            if not busy:
                return
            time.sleep(0.02)

    @classmethod
    def _refresh_model_caches(cls, force: bool = False) -> Tuple[set, dict]:
        """刷新并返回 (ollama 模型集合, {rapid-mlx 模型: host})。

        30 秒缓存（`force=True` 立即探测，供巡检线程用）。

        「探测」与「写回」严格分离：探测是同步网络调用（每主机 timeout=3s），
        必须在锁**外**做，否则缓存过期瞬间的并发请求会排成一队等同一个探测。
        锁内只做三件事：判定要不要刷、置单飞标记、写回结果。

        写回规则：拿到 `None`（未知）时保留旧值；拿到集合（含空集）时无条件覆盖，
        并在清单变化后清理熔断表 / 能力缓存里已经不存在的模型。
        """
        now = time.time()
        if force:
            # 先等掉在飞的刷新：否则单飞标记会让本次"强刷"直接跳过探测、返回旧值
            cls._wait_inflight_refresh()
            now = time.time()
        ttl = 0 if force else 30
        with cls._models_cache_lock:
            need_ollama = now - cls._ollama_models_cache["ts"] > ttl
            need_rapid = now - cls._rapid_mlx_models_cache["ts"] > ttl
            if need_ollama and cls._ollama_refreshing:
                need_ollama = False
            if need_rapid and cls._rapid_refreshing:
                need_rapid = False
            if need_ollama:
                cls._ollama_refreshing = True
            if need_rapid:
                cls._rapid_refreshing = True

        changed = False
        if need_ollama:
            try:
                models = cls._fetch_ollama_models()
                if models is not None:
                    with cls._models_cache_lock:
                        old = set(cls._ollama_models_cache["models"])
                        cls._ollama_models_cache["models"] = models
                        cls._ollama_models_cache["ts"] = time.time()
                    changed = changed or (old != models)
            finally:
                with cls._models_cache_lock:
                    cls._ollama_refreshing = False

        if need_rapid:
            try:
                models = cls._fetch_rapid_mlx_models()
                if models is not None:
                    with cls._models_cache_lock:
                        old = dict(cls._rapid_mlx_models_cache["models"])
                        cls._rapid_mlx_models_cache["models"] = models
                        cls._rapid_mlx_models_cache["ts"] = time.time()
                    changed = changed or (old != models)
            finally:
                with cls._models_cache_lock:
                    cls._rapid_refreshing = False

        with cls._models_cache_lock:
            ollama_now = set(cls._ollama_models_cache["models"])
            rapid_now = dict(cls._rapid_mlx_models_cache["models"])
        if changed:
            cls._sync_model_registry(ollama_now, rapid_now)
        return ollama_now, rapid_now

    @classmethod
    def _ensure_list_fresh(cls, force: bool = False) -> None:
        """列举接口（/v1/models、/api/tags）用：**不阻塞**地保证清单大致新鲜。

        三种情形：
          · `force=True` 或 `TG_MODEL_LIST_TTL=0` → 同步刷新（调用方明确要最新，且
            这是唯一会等网络的情形）；
          · 缓存年龄 ≤ TTL → 直接返回，本次用缓存；
          · 缓存已过期 → 起一个**单飞**后台线程去刷，本次仍返回旧缓存。
            这样客户端轮询列表不会被上游超时拖住，也不会因某台上游抖动而少几个模型。

        与巡检线程（`_model_refresh_loop`）的分工：巡检只负责闲时保活与对账；
        这里负责"有人在看列表"时尽快让删除/新增可见。两者都走同一把
        `_refresh_model_caches`，锁与单飞标记一致，不会重复探测。
        """
        if force or MODEL_LIST_TTL_S <= 0:
            cls._refresh_model_caches(force=True)
            return
        with cls._models_cache_lock:
            cold = not cls._known_models
            age = time.time() - max(cls._ollama_models_cache["ts"],
                                   cls._rapid_mlx_models_cache["ts"])
            busy = cls._list_refreshing
        if cold:
            # 冷启动没有旧数据可给，宁可等一次也别返回空列表（客户端会把空列表当真）
            cls._refresh_model_caches(force=True)
            return
        if age <= MODEL_LIST_TTL_S or busy:
            return
        with cls._models_cache_lock:
            if cls._list_refreshing:
                return
            cls._list_refreshing = True

        def _bg() -> None:
            try:
                cls._refresh_model_caches(force=True)
            except Exception as e:
                log(f"⚠️  后台刷新模型清单失败：{type(e).__name__}: {e}")
            finally:
                with cls._models_cache_lock:
                    cls._list_refreshing = False

        threading.Thread(target=_bg, daemon=True, name="model-list-refresh").start()

    @classmethod
    def _sync_model_registry(cls, ollama: set, rapid: dict) -> None:
        """按最新模型清单清理失效记录，并处理"重新出现"的模型。

        为什么需要：熔断表与能力缓存都以模型名为键，模型被删掉后这些条目不会自己消失。
        留着它们有两个后果：① 名字复用（删了又 pull 同名/同 tag）时，新模型一上来就带着
        旧的熔断/「不支持 tools」判定；② 长期累积、无从回收。清单变化时按名字做一次
        对账，最省事也最不容易错。
        """
        live = set(ollama) | set(rapid.keys())
        prev = set(cls._known_models)
        added = live - prev
        removed = prev - live
        cls._known_models = set(live)
        if not prev:
            # 首次观测（进程刚起）：把当前清单记下来，别把它当成"变化"刷一条误导日志
            log(f"📋 已同步上游模型清单：{len(live)} 个"
                + (f"（{', '.join(sorted(live)[:6])}{' …' if len(live) > 6 else ''}）" if live else ""))
            return
        if not (added or removed):
            return

        # 1) 已经不存在的模型：连同熔断 / 失败计数 / 能力缓存一起清掉
        gone_bad = 0
        with cls._bad_models_lock:
            for m in list(cls._bad_models):
                if m not in live:
                    cls._bad_models.pop(m, None)
                    gone_bad += 1
        with cls._fail_counts_lock:
            for m in list(cls._fail_counts):
                if m not in live:
                    cls._fail_counts.pop(m, None)
        with cls._tool_caps_lock:
            for m in list(cls._tool_caps):
                if m not in live:
                    cls._tool_caps.pop(m, None)

        # 2) 重新出现的模型：很可能是重新 pull 过，清掉旧的熔断与能力判定
        revived = []
        with cls._bad_models_lock:
            for m in added:
                if cls._bad_models.pop(m, None) is not None:
                    revived.append(m)
        with cls._fail_counts_lock:
            for m in added:
                cls._fail_counts.pop(m, None)
        with cls._tool_caps_lock:
            for m in added:
                cls._tool_caps.pop(m, None)

        def _brief(xs):
            xs = sorted(xs)
            head = ", ".join(xs[:4])
            return head + (f" 等 {len(xs)} 个" if len(xs) > 4 else "")

        parts = []
        if added:
            parts.append(f"+{_brief(added)}")
        if removed:
            parts.append(f"-{_brief(removed)}")
        log(f"📋 模型清单变化：{' '.join(parts)}"
            + (f"；清理 {gone_bad} 条失效熔断" if gone_bad else "")
            + (f"；解除 {len(revived)} 个重新出现的模型的熔断" if revived else ""))

    def _ollama_model_exists(self, model: str) -> bool:
        """检查模型是否在任一上游存在。"""
        if not model:
            return True
        # 熔断中的模型视为不存在：让客户端立刻拿到 404 + 可用列表，
        # 而不是把请求发出去、等上游自己拒绝。
        if self._is_model_bad(model):
            return False
        ollama_models, rapid_models = self._refresh_model_caches()
        all_models = ollama_models | set(rapid_models.keys())
        if all_models:
            return model in all_models
        return True

    # ---------- auto 虚拟模型：自动分配 ----------
    # 轮询游标（类级，跨请求共享）；用 itertools.count() 取模保证线程安全递增
    _auto_cursor = itertools.count()

    @staticmethod
    def _auto_is_embed(model: str) -> bool:
        """是否为非对话模型（嵌入/重排），这类模型不能用于 chat。"""
        m = model.lower()
        return any(h in m for h in AUTO_EMBED_HINTS)

    @staticmethod
    def _auto_is_vlm(model: str, host: str) -> bool:
        """是否具备多模态能力：上游被标为 VLM，或模型名带视觉特征。"""
        if host in RAPID_MLX_VLM_HOSTS:
            return True
        m = model.lower()
        return any(h in m for h in AUTO_VLM_HINTS)

    @staticmethod
    def _auto_req_has_tools(req: dict) -> bool:
        """请求是否携带工具定义（OpenAI 的 tools / 旧版 functions）。"""
        if not isinstance(req, dict):
            return False
        return bool(req.get("tools") or req.get("functions"))

    def _probe_ollama_tool_cap(self, model: str) -> Optional[bool]:
        """向 Ollama `/api/show` 询问模型能力，返回 True/False/None（未知）。

        只用于"带 tools 的 auto 请求"，按模型懒探测并缓存 `TG_TOOL_CAP_TTL` 秒。
        老版本 Ollama 不返回 `capabilities` 字段，此时一律返回 None —— **未知即放行**，
        绝不能因为探测不到就把模型从候选池里踢掉（那会把可用模型越筛越少）。
        """
        try:
            payload = json.dumps({"name": model}).encode("utf-8")
            req = urllib.request.Request(
                f"{OLLAMA_HOST}/api/show", data=payload, method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status != 200:
                    return None
                data = json.loads(r.read() or b"{}")
        except Exception:
            return None
        caps = data.get("capabilities")
        if not isinstance(caps, list) or not caps:
            return None
        return "tools" in [str(c).lower() for c in caps]

    def _auto_supports_tools(self, model: str, host: str) -> Optional[bool]:
        """模型是否支持工具调用；None 表示未知（按支持处理）。

        只有 Ollama 有可查的能力接口；Rapid-MLX / vLLM 这类上游没有，返回 None
        由上游 400 反向学习（见 `_mark_model_no_tools`）。
        """
        cap = self._tool_cap_get(model)
        if cap is not None:
            return cap
        if host == OLLAMA_HOST:
            cap = self._probe_ollama_tool_cap(model)
            if cap is not None:
                self._tool_cap_set(model, cap)
            return cap
        return None

    @staticmethod
    def _auto_req_has_image(req: dict) -> bool:
        """请求是否携带图片（与 _proxy 中的判定口径保持一致）。"""
        if not isinstance(req, dict):
            return False
        for msg in req.get("messages", []) or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list) and any(
                isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image")
                for p in content
            ):
                return True
        return bool(req.get("images") or req.get("image"))

    # ---- 请求分档：轻量 / 常规 / 深度 ----
    @staticmethod
    def _auto_req_text_len(req: dict) -> int:
        """请求文本总字符数（含 system/user/assistant 与多段 content 的 text 部分）。"""
        if not isinstance(req, dict):
            return 0
        total = 0
        for msg in req.get("messages", []) or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and isinstance(p.get("text"), str):
                        total += len(p["text"])
        if isinstance(req.get("prompt"), str):
            total += len(req["prompt"])
        return total

    @staticmethod
    def _auto_req_prompt_chars(req: dict) -> int:
        """估算真实 prompt 规模：正文 + 工具清单等结构化字段的序列化长度。

        与 `_auto_req_text_len` 的区别是用途不同：后者回答"用户输入有多长"（分档阈值用），
        这个回答"上游会看到多少 token"（速度分桶用）。

        工具清单在真实 prompt 里占的 token 不可忽略：实测 12 个工具、5,066 字符 JSON
        ≈ 1,967 prompt token（0.39 tok/char）。漏掉它会让带工具的长会话被估进偏小的桶，
        取到偏小的耗时样本，speed 因子随之失真。
        """
        total = GatewayHandler._auto_req_text_len(req)
        if not isinstance(req, dict):
            return total
        for key in ("tools", "functions", "response_format"):
            val = req.get(key)
            if not val:
                continue
            try:
                total += len(json.dumps(val, ensure_ascii=False))
            except (TypeError, ValueError):
                # 极端情况下（含不可序列化对象）给保守估计，避免整个估算失效
                total += 2048
        return total

    @staticmethod
    def _auto_req_image_urls(req: dict) -> List[str]:
        """收集请求里所有图片地址（data URI 或 http URL）。"""
        if not isinstance(req, dict):
            return []
        urls: List[str] = []
        for msg in req.get("messages", []) or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") not in ("image_url", "image", "input_image"):
                        continue
                    iu = p.get("image_url")
                    url = iu.get("url") if isinstance(iu, dict) else iu
                    if isinstance(url, str) and url:
                        urls.append(url)
            if isinstance(msg.get("images"), list):
                urls.extend(u for u in msg["images"] if isinstance(u, str))
        for key in ("images", "image"):
            val = req.get(key)
            if isinstance(val, str):
                urls.append(val)
            elif isinstance(val, list):
                urls.extend(u for u in val if isinstance(u, str))
        return urls

    @staticmethod
    def _auto_req_image_tokens(req: dict) -> int:
        """图片占用的 prompt token 估算。

        图片在真实 prompt 里按面积计费（实测约 980 tok / 百万像素），与正文无关。
        漏算它会让带图请求的 p_hat 偏小、取到偏小的速度桶（那里全是纯文本样本，
        明显更快），speed 因子因此系统性偏高。拿不到宽高（远程 URL）时按名义值计。
        """
        total = 0
        for url in GatewayHandler._auto_req_image_urls(req):
            mp = _data_uri_megapixels(url)
            total += IMG_TOKENS_NOMINAL if mp is None else int(mp * IMG_TOKENS_PER_MPIX)
        return total

    def _auto_policy(self, cfg: dict) -> dict:
        """读取分档/权重策略：env 默认值 + config.json global 覆盖（可运行时调整）。"""
        g = cfg.get("global") or {}

        tier_on = g.get("auto_tier", AUTO_TIER_ENABLED)
        if isinstance(tier_on, str):
            tier_on = tier_on.strip().lower() not in ("0", "false", "no", "off")
        else:
            tier_on = bool(tier_on)

        ideals = _parse_float_list(g.get("auto_tier_ideals"), AUTO_TIER_IDEALS)
        while len(ideals) < 3:  # 补足三档，避免下标越界
            ideals.append(AUTO_TIER_IDEALS[len(ideals) % len(AUTO_TIER_IDEALS)])

        def _int_opt(key: str, default: int) -> int:
            try:
                v = int(g.get(key) or default)
            except (TypeError, ValueError):
                return default
            return v if v > 0 else default

        return {
            "tier": tier_on,
            "ideals": ideals[:3],
            "weights": _parse_weight_map(g.get("auto_weights"), AUTO_WEIGHTS),
            "light_chars": _int_opt("auto_tier_light_chars", AUTO_TIER_LIGHT_CHARS),
            "deep_chars": _int_opt("auto_tier_deep_chars", AUTO_TIER_DEEP_CHARS),
        }

    def _auto_req_tier(self, path: str, req: dict, policy: dict) -> Tuple[int, str]:
        """给请求分档，返回 (0=轻量 | 1=常规 | 2=深度, 判定依据)。

        深度信号（任一命中即深度）：带图、工具调用、长输入、要求长输出、多轮长会话。
        轻量信号（全部满足）：短输入 + 短输出 + 轮次少。
        两者都不满足归为常规档。
        """
        if not policy.get("tier", True):
            return 1, "分档已关闭"
        if path.endswith("/embeddings"):
            return 1, "嵌入请求，不参与分档"
        if not isinstance(req, dict):
            return 1, "无请求体"

        n_msgs = len(req.get("messages", []) or [])
        chars = self._auto_req_text_len(req)
        try:
            max_tokens = int(req.get("max_tokens") or req.get("max_completion_tokens") or 0)
        except (TypeError, ValueError):
            max_tokens = 0

        if self._auto_req_has_image(req):
            return 2, "带图请求"
        if req.get("tools") or req.get("functions"):
            return 2, "工具调用"
        if chars >= policy["deep_chars"]:
            return 2, f"长输入 {chars} 字符"
        if max_tokens >= 2048:
            return 2, f"长输出 max_tokens={max_tokens}"
        if n_msgs >= 8:
            return 2, f"多轮会话 {n_msgs} 条消息"
        if chars <= policy["light_chars"] and n_msgs <= 3 and (max_tokens == 0 or max_tokens <= 1024):
            return 0, f"短问答 {chars} 字符"
        return 1, f"{chars} 字符 / {n_msgs} 条消息"

    # ---- 质量排序：给候选算一个"精度分"，越大越优质 ----
    @staticmethod
    def _parse_size_to_b(s: str) -> Optional[float]:
        """把 '8.0B' / '137M' / '1.2T' 解析成以 B 为单位的浮点数。"""
        if not s:
            return None
        m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMBT])?$", str(s).strip().upper())
        if not m:
            return None
        unit = m.group(2) or "B"
        return float(m.group(1)) * {"K": 1e-6, "M": 1e-3, "B": 1.0, "T": 1e3}[unit]

    @staticmethod
    def _param_b_from_name(model: str) -> Optional[float]:
        """从模型名解析参数量（B），取最大的一处。

        取最大值可把 'Qwen3.5-122B-A10B' 定位到总参 122B，而 'A10B'（激活参数）
        因前面紧邻字母被负向前瞻排除，不会误判成 10B。
        """
        best = None
        for m in re.finditer(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*[Bb](?![A-Za-z])", model):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if best is None or v > best:
                best = v
        return best

    def _auto_param_b(self, model: str, host: str) -> Optional[float]:
        """模型参数量（B）：显式覆写 > 上游元数据 > 从名字推断。"""
        override = MODEL_PARAM_OVERRIDES.get(model)
        if override is not None:
            return override
        if host == OLLAMA_HOST:
            info = (self._ollama_meta_cache.get("meta") or {}).get(model)
            if info:
                v = self._parse_size_to_b(info.get("param", ""))
                if v is not None:
                    return v
        return self._param_b_from_name(model)

    def _auto_quant_rank(self, model: str, host: str) -> int:
        """量化精度档位（越大越精确），参数量相同时用它做次级排序。"""
        q = ""
        if host == OLLAMA_HOST:
            info = (self._ollama_meta_cache.get("meta") or {}).get(model)
            if info:
                q = (info.get("quant") or "").upper()
        if not q:
            q = model.upper()
        for token, rank in AUTO_QUANT_RANK:
            if token in q:
                return rank
        return 1  # 未知按 Q4 档处理

    def _auto_priority_list(self, cfg: dict) -> List[str]:
        """显式质量优先顺序（高→低）：env TG_AUTO_QUALITY 在前，config global.auto_quality 在后。"""
        raw: List[str] = list(AUTO_QUALITY)
        cfg_list = (cfg.get("global") or {}).get("auto_quality") or []
        if isinstance(cfg_list, str):
            cfg_list = [x.strip() for x in cfg_list.split(",")]
        for x in cfg_list:
            if isinstance(x, str) and x.strip():
                raw.append(x.strip())
        out: List[str] = []
        for m in raw:
            real = MODEL_ALIASES.get(m, m)  # 允许用别名书写
            if real not in out:
                out.append(real)
        return out

    def _auto_quality_key(self, model: str, host: str, prio: List[str]) -> Tuple[int, float, int]:
        """(tier, rank, quant)，降序排序即"越优质越靠前"。

        tier 2 = 在显式优先表内（rank 越大越靠前）
        tier 1 = 有参数量（rank = 参数量 B）
        tier 0 = 参数量未知，排最后（同 tier 再比量化）
        """
        quant = self._auto_quant_rank(model, host)
        if model in prio:
            return (2, float(len(prio) - prio.index(model)), quant)
        pb = self._auto_param_b(model, host)
        if pb is not None:
            return (1, pb, quant)
        return (0, 0.0, quant)

    def _auto_quality_desc(self, model: str, host: str, prio: List[str]) -> str:
        """人类可读的选择理由，写进日志。"""
        if model in prio:
            return f"显式优先 #{prio.index(model) + 1}"
        pb = self._auto_param_b(model, host)
        if pb is None:
            return "参数量未知"
        info = (self._ollama_meta_cache.get("meta") or {}).get(model) or {}
        quant = info.get("quant") or ""
        return f"{pb:g}B" + (f" {quant}" if quant else "")

    # ---- 分档适配与综合打分 ----
    @staticmethod
    def _auto_tier_fit(pb: Optional[float], tier: int, ideals: List[float]) -> float:
        """模型参数量与该档"理想参数量"的贴合度，1.0 = 正好，0 = 差一个数量级以上。

        在对数尺度上用高斯衰减：8B vs 122B 相差约 1.18 个数量级，单档 sigma 取
        0.35（轻量，贴合更严格）/ 0.55（常规）/ 0.70（深度），保证跨档能拉开差距。
        深度档只惩罚"偏小"，比理想值更大的模型同样算贴合（1.0）。
        """
        if not pb or pb <= 0:
            return 0.35  # 参数量未知：给中性偏低分，避免它靠别的因子冒头
        tier = max(0, min(2, int(tier)))
        ideal = ideals[tier] if tier < len(ideals) else ideals[-1]
        x, target = math.log10(pb), math.log10(max(1e-3, ideal))
        d = (target - x) if tier == 2 else abs(x - target)
        d = max(0.0, d)
        sigma = (0.35, 0.55, 0.70)[tier]
        return math.exp(-(d * d) / (2.0 * sigma * sigma))

    def _auto_balanced_score(self, model: str, host: str, pb: Optional[float], tier: int,
                             policy: dict, prio: List[str],
                             ctx: dict, speed: Optional[float] = None) -> Tuple[float, str]:
        """多维加权综合打分（0~1，越大越好），返回 (分数, 各因子明细)。

        因子（默认权重见 AUTO_WEIGHTS）：
          tier    分档适配：模型大小是否符合本档理想值（轻量档偏好小模型）   0.30
          quality 绝对质量：候选池内参数量的对数位置（越大越强）             0.30
          speed   速度收益：**实测耗时**归一化（越快越高，见 LatencyStats）  0.30
          pref    显式偏好：config global.auto_quality 表内位置（越靠前越高）0.15
          quant   量化保真：F32/BF16 > Q8 > Q6 > Q5 > Q4 > Q3/Q2            0.10

        **三项主因子等权**：分档、质量、速度各自独立打分、按权重求和，不存在"先按档位
        筛选再按质量选"的硬顺序。权重可由 config global.auto_weights / TG_AUTO_WEIGHTS 调整。
        speed 由调用方按实测耗时做绝对软映射（tau/(tau+t)）后传入；缺测时给中性分，
        不用参数量猜——实测 122B 在 3k~20k 上下文比 8B 快，"小模型更快"并不成立。
        未启用的因子（权重 0 或未配置偏好表）从分母中剔除，不会拉低分数。
        """
        fit = self._auto_tier_fit(pb, tier, policy["ideals"])

        lo, hi = ctx.get("lo"), ctx.get("hi")
        if not pb or pb <= 0 or lo is None or hi is None:
            qual = 0.35
        elif hi - lo < 1e-9:
            qual = 0.75  # 候选只有一个尺寸：中性值
        else:
            qual = max(0.0, min(1.0, (math.log10(pb) - lo) / (hi - lo)))

        # 速度因子由实测耗时得出；无样本时 0.40（略低于中位，避免无数据的模型白占便宜）
        speed_v = 0.40 if speed is None else max(0.0, min(1.0, speed))

        quant = min(1.0, max(0.0, self._auto_quant_rank(model, host)) / 6.0)

        factors = {"tier": fit, "quality": qual, "speed": speed_v, "quant": quant}
        if prio:
            factors["pref"] = (0.85 ** prio.index(model)) if model in prio else 0.0
        weights = policy.get("weights") or AUTO_WEIGHTS
        total_w = sum(max(0.0, weights.get(k, 0.0)) for k in factors)
        if total_w <= 0:
            total_w = float(len(factors)) or 1.0
            weights = {k: 1.0 for k in factors}
        score = sum(max(0.0, weights.get(k, 0.0)) * v for k, v in factors.items()) / total_w
        detail = " ".join(f"{k}={v:.2f}" for k, v in factors.items() if weights.get(k, 0.0) > 0)
        return max(0.0, min(1.0, score)), detail

    def _auto_candidates(self, path: str, req: dict, cfg: dict) -> Tuple[List[Tuple[str, str]], str]:
        """构造 auto 候选池，返回 ([(model, upstream_host)], 说明)。

        过滤链：上游可用 → deny/auto 排除 → allow 白名单 → 能力匹配（chat / embeddings / 带图）。
        候选池按模型名排序，保证轮询顺序稳定、可复现。
        """
        ollama_models, rapid_models = self._refresh_model_caches()
        pool: List[Tuple[str, str]] = [(m, OLLAMA_HOST) for m in sorted(ollama_models)]
        pool += [(m, h) for m, h in sorted(rapid_models.items())]

        allow = cfg["global"].get("allow_models", ["*"]) or []
        deny = set(cfg["global"].get("deny_models", []) or [])
        # auto 专用排除：config.json 的 global.auto_exclude（可运行时调整，不影响直接调用该模型）
        auto_excl = set(cfg["global"].get("auto_exclude", []) or [])
        # allow 里出现 auto 本身即视为"放行 auto 入口"，候选池不受 allow 明细约束（仍受 deny 约束）
        wide_allow = ("*" in allow) or (AUTO_MODEL_NAME in allow)

        allowed: List[Tuple[str, str]] = []
        skipped_bad = 0
        skipped_bad_names: List[str] = []
        for m, h in pool:
            if m in deny or m in AUTO_EXCLUDE or m in auto_excl:
                continue
            if not wide_allow and m not in allow:
                continue
            # 熔断中的模型不进候选池：它可能仍在 /api/tags 里，但实际加载不了。
            if self._is_model_bad(m):
                skipped_bad += 1
                skipped_bad_names.append(m)
                continue
            allowed.append((m, h))
        note_bad = ""
        if skipped_bad:
            note_bad = f"；跳过 {skipped_bad} 个失效模型"
            if skipped_bad <= 3:
                note_bad += ": " + ", ".join(skipped_bad_names)

        if path.endswith("/embeddings"):
            embed = [(m, h) for m, h in allowed if self._auto_is_embed(m)]
            if embed:
                return embed, "嵌入端点，仅限嵌入模型" + note_bad
            return [], "嵌入端点但候选池内没有嵌入模型" + note_bad

        chat = [(m, h) for m, h in allowed if not self._auto_is_embed(m)]
        if self._auto_req_has_image(req):
            vlm = [(m, h) for m, h in chat if self._auto_is_vlm(m, h)]
            if vlm:
                base, note = vlm, "带图请求，优先多模态模型"
            else:
                base, note = chat, "带图请求但无多模态候选，回退文本模型（图片将被过滤）"
        else:
            base, note = chat, "文本请求"

        # 工具调用是硬能力要求：视觉模型常常只能对话、不接受 tools，
        # 选中它们的下场是上游 400（而且原文带 registry 路径，客户端看不懂）。
        if self._auto_req_has_tools(req):
            tooled = [(m, h) for m, h in base if self._auto_supports_tools(m, h) is not False]
            dropped = len(base) - len(tooled)
            if dropped:
                note += f"；带工具请求，已排除 {dropped} 个不支持 tools 的模型"
            if tooled:
                return tooled, note + note_bad
            # 全部未知/不支持：仍把原池交出去，让上游给出确切原因，别在这里谎报"无候选"
            return base, note + "；带工具请求但候选均未声明支持 tools" + note_bad
        return base, note + note_bad

    def _auto_pick_model(self, path: str, req: dict, cfg: dict) -> Tuple[str, str, str]:
        """从候选池挑一个模型，返回 (model, upstream_host, 说明)；无候选时 model 为空串。

        balanced（默认）：先给请求分档（轻量/常规/深度），再按多维综合打分择优。
          - 短问答给 8B 级模型（快、省），长上下文/工具调用/带图才上大模型
          - 显式优先表权重最高，其次分档适配与绝对质量，速度/量化用于细分
          - 最贴合的模型被占用：让位给次优的空闲模型，而不是降级到弱模型
          - 全部繁忙：在负载最低的候选里取综合分最高者，避免直接 503
        quality：纯质量降序（tier → 参数量/表内位次 → 量化），忽略请求体量。
        least-busy：按网关侧 in-flight 过滤出空闲候选，在空闲集合内轮询。
        """
        cands, note = self._auto_candidates(path, req, cfg)
        if not cands:
            return "", "", note

        if AUTO_STRATEGY == "random":
            import random
            idx = random.randrange(len(cands))
            model, host = cands[idx]
            return model, host, note

        if AUTO_STRATEGY in ("balanced", "quality"):
            prio = self._auto_priority_list(cfg)
            # 显式优先表是"用户指定的强偏好"：表内模型只要在候选池里，就只在表内挑，
            # 避免被分档/质量分反超；表内模型全部不可用时才回落到全量候选。
            if prio:
                in_prio = [(m, h) for m, h in cands if m in prio]
                if in_prio:
                    cands = in_prio
                    note += f"；命中显式优先表（{len(in_prio)} 个候选）"
            policy = self._auto_policy(cfg)
            tier, tier_why = self._auto_req_tier(path, req, policy)
            self._auto_tier_label = AUTO_TIER_NAMES[tier]

            # 仅深度档可用的大模型（env TG_AUTO_DEEP_ONLY + config global.auto_deep_only）
            deep_only = {MODEL_ALIASES.get(m, m) for m in AUTO_DEEP_ONLY}
            _cfg_deep = (cfg.get("global") or {}).get("auto_deep_only") or []
            if isinstance(_cfg_deep, str):
                _cfg_deep = _cfg_deep.split(",")
            for _m in _cfg_deep:
                if isinstance(_m, str) and _m.strip():
                    deep_only.add(MODEL_ALIASES.get(_m.strip(), _m.strip()))
            if deep_only and tier != len(AUTO_TIER_NAMES) - 1:
                gated = [(m, h) for m, h in cands if m not in deep_only]
                # 全被摘掉时不生效：宁可给贵模型，也不能因为策略配置而 503
                if gated and len(gated) != len(cands):
                    note += (f"；{len(cands) - len(gated)} 个模型仅限深度档，"
                             f"本档（{AUTO_TIER_LABELS[AUTO_TIER_NAMES[tier]]}）不参与")
                    cands = gated
            loads = {m: model_inflight_get(m) for m, _ in cands}

            entries = []
            if AUTO_STRATEGY == "balanced":
                sizes = [self._auto_param_b(m, h) for m, h in cands]
                logs = [math.log10(s) for s in sizes if s and s > 0]
                ctx = {"lo": min(logs) if logs else None, "hi": max(logs) if logs else None}
                # 速度因子：按本请求预估的 prompt 规模取各候选的实测中位耗时，
                # 再用软映射 tau/(tau+t) 转成 0~1（越快越高），与候选池构成无关。
                # 用 _auto_req_prompt_chars 而非 _auto_req_text_len：工具清单同样占 token，
                # 只数正文会把带工具的长会话估进偏小的桶；图片另按面积折算 token 再加。
                p_hat = (int(self._auto_req_prompt_chars(req) * TOKENS_PER_CHAR)
                         + self._auto_req_image_tokens(req))
                t_est = {}
                for (m, _h) in cands:
                    t = MODEL_LATENCY.estimate(m, p_hat)
                    if t is not None:
                        t_est[m] = t
                for (m, h), pb in zip(cands, sizes):
                    speed_v = None
                    if m in t_est:
                        speed_v = SPEED_TAU / (SPEED_TAU + t_est[m])
                    score, brk = self._auto_balanced_score(m, h, pb, tier, policy, prio, ctx, speed_v)
                    entries.append({"m": m, "h": h, "score": score, "desc": brk,
                                    "t_est": t_est.get(m)})
                entries.sort(key=lambda e: (-e["score"], e["m"]))
            else:
                for m, h in cands:
                    entries.append({"m": m, "h": h,
                                    "rank": self._auto_quality_key(m, h, prio),
                                    "desc": self._auto_quality_desc(m, h, prio)})
                entries.sort(key=lambda e: (-e["rank"][0], -e["rank"][1], -e["rank"][2], e["m"]))

            idle = [e for e in entries if loads[e["m"]] < AUTO_BUSY_THRESHOLD]
            busy = [e for e in entries if loads[e["m"]] >= AUTO_BUSY_THRESHOLD]
            if busy:
                busy_desc = ", ".join(f"{e['m']}({loads[e['m']]})" for e in busy)
                note += f"；跳过 {len(busy)} 个繁忙候选: {busy_desc}"

            if policy.get("tier"):
                tag = f"分档 {AUTO_TIER_LABELS.get(AUTO_TIER_NAMES[tier], tier_why)}（{tier_why}）"
            else:
                tag = tier_why

            if idle:
                pick = idle[0]
                if AUTO_STRATEGY == "balanced":
                    note += (f"；{tag} 综合 {pick['score']:.3f} 择优 "
                             f"{self._auto_quality_desc(pick['m'], pick['h'], prio)}"
                             f"（{pick['desc']}）")
                else:
                    note += f"；{tag}；质量优先 {pick['desc']}"
                return pick["m"], pick["h"], note

            lowest = min(loads[e["m"]] for e in entries)
            pick = [e for e in entries if loads[e["m"]] == lowest][0]
            if AUTO_STRATEGY == "balanced":
                note += (f"；全部候选繁忙，在最低负载 {lowest} 中取综合分最高者 "
                         f"{self._auto_quality_desc(pick['m'], pick['h'], prio)}"
                         f"（{pick['score']:.3f}）")
            else:
                note += f"；全部候选繁忙，在最低负载 {lowest} 中取质量最高者 {pick['desc']}"
            return pick["m"], pick["h"], note

        pool = cands
        if AUTO_STRATEGY == "least-busy":
            loads = {m: model_inflight_get(m) for m, _ in cands}
            idle = [(m, h) for m, h in cands if loads[m] < AUTO_BUSY_THRESHOLD]
            if idle:
                skipped = len(cands) - len(idle)
                if skipped:
                    busy_names = ", ".join(f"{m}({loads[m]})" for m, _ in cands if loads[m] >= AUTO_BUSY_THRESHOLD)
                    note += f"；跳过 {skipped} 个繁忙候选: {busy_names}"
                pool = idle
            else:
                lowest = min(loads[m] for m, _ in cands)
                pool = [(m, h) for m, h in cands if loads[m] == lowest]
                note += f"；全部候选繁忙，选最低负载 {lowest}"

        idx = next(self._auto_cursor) % len(pool)
        model, host = pool[idx]
        return model, host, note

    def _resolve_upstream(self, model: str, path: str = "") -> str:
        """根据模型名决定请求发往哪个上游。"""
        if path.startswith("/api/tags") or path.startswith("/api/pull") or path.startswith("/api/delete"):
            return OLLAMA_HOST
        # 先查 Rapid-MLX 缓存（带 host）
        rapid_cache = self._rapid_mlx_models_cache.get("models", {})
        if model and model in rapid_cache:
            return rapid_cache[model]
        # 也查 Ollama 缓存
        ollama_cache = self._ollama_models_cache.get("models", set())
        if model and model in ollama_cache:
            return OLLAMA_HOST
        # Ollama 专有路径或 fallback
        return OLLAMA_HOST

    # ---- 路由分发 ----
    # 无 /v1 前缀的 OpenAI 兼容路径（客户端 Base URL 未带 /v1 时兼容）
    _PATH_REWRITE = {
        "/models": "/v1/models",
        "/chat/completions": "/v1/chat/completions",
        "/completions": "/v1/completions",
        "/embeddings": "/v1/embeddings",
    }

    def _normalize_path(self):
        """规范化路径：URL 解码、去除空格、兼容无 /v1 前缀、去除末尾斜杠。

        修复客户端 Base URL 末尾带空格导致路径变成 /v1%20/v1/... 的 404 问题。
        """
        import urllib.parse as _up
        qs = ""
        if "?" in self.path:
            self.path, qs = self.path.split("?", 1)
            qs = "?" + qs
        # URL 解码后去除所有空白字符（修复 Base URL 末尾带空格的问题）
        self.path = _up.unquote(self.path).replace(" ", "").replace("\t", "")
        # 末尾斜杠去除（除了根 /）
        if len(self.path) > 1 and self.path.endswith("/"):
            self.path = self.path.rstrip("/")
        # 处理重复的 /v1 前缀（如 /v1/v1/chat/completions → /v1/chat/completions）
        while "/v1/v1/" in self.path:
            self.path = self.path.replace("/v1/v1/", "/v1/", 1)
        # 无 /v1 前缀的 OpenAI 路径重写
        if self.path in self._PATH_REWRITE:
            self.path = self._PATH_REWRITE[self.path]
        self.path = self.path + qs

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        global active_concurrency
        self._normalize_path()
        path = self.path.split("?", 1)[0]

        # 健康检查（无需鉴权）
        if path == "/health" or path == "/healthz":
            # watchdog 只暴露状态摘要（状态/次数/最近错误），不含权重路径等敏感信息
            self._send_json({"status": "ok", "version": "1.0.0",
                             "uptime": int(time.time() - _start_ts),
                             "watchdog": _wd_snapshot()})
            return

        # 管理面板 UI（无需鉴权，页面内输入 Admin Token）
        if path == "/" or path == "/admin" or path == "/admin/":
            html = ADMIN_UI_HTML
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        # /admin/chat — DeerFlow 深度研究聊天界面
        if path == "/admin/chat":
            import urllib.parse as _up2
            qs_dict = {}
            if "?" in self.path:
                try:
                    qs_dict = _up2.parse_qs(_up2.unquote(self.path.split("?", 1)[1]))
                except Exception:
                    pass
            session = qs_dict.get("session", [None])[0]
            deer_host = self.headers.get("Host", "localhost:3000").split(":")[0]
            deer_scheme = "https" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else "http"
            if session:
                deer_url = f"{deer_scheme}://{deer_host}:2026/workspace/chats/{_up2.quote(session)}"
            else:
                deer_url = f"{deer_scheme}://{deer_host}:2026/workspace/chats/new"
            html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeerFlow 深度研究</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;background:#0b0e14;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
iframe{{width:100%;height:100vh;border:0;display:block;background:#0b0e14}}
.topbar{{position:fixed;top:0;left:0;right:0;height:44px;background:#141824;border-bottom:1px solid #262b3d;display:flex;align-items:center;padding:0 16px;gap:12px;z-index:10;opacity:0.95;backdrop-filter:blur(8px)}}
.topbar a{{color:#6366f1;text-decoration:none;font-size:13px;font-weight:600}}
.topbar .title{{font-size:14px;color:#e2e8f0;font-weight:600}}
.topbar .spacer{{flex:1}}
.topbar .ext{{color:#64748b;font-size:12px;margin-right:8px}}
iframe{{padding-top:0}}
</style>
</head>
<body>
<div class="topbar">
<a href="/admin">← 返回管理面板</a>
<span class="title">🔬 DeerFlow 深度研究</span>
<span class="spacer"></span>
<span class="ext">DeerFlow 端口 2026</span>
<a href="{deer_url}" target="_blank">↗ 新窗口</a>
</div>
<iframe src="{deer_url}" allow="clipboard-read;clipboard-write"></iframe>
</body></html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        # 管理接口
        if path.startswith("/api/admin/"):
            self._handle_admin("GET", path)
            return

        # ---- 免鉴权的只读模型发现接口 ----

        # /v1/models — 合并 Ollama + Rapid-MLX
        if path == "/v1/models":
            try:
                # 列表一律读缓存：旧实现每次现场探测 1+N 个上游（各 timeout=3s），
                # 客户端轮询时会阻塞数秒，且任一上游抖动就静默少几个模型。
                # 缓存过期则后台刷新，本次照常返回旧值；?refresh=1 才同步等更新。
                _force = "_refresh=1" in self.path or "refresh=1" in self.path
                self._ensure_list_fresh(force=_force)
                with self._models_cache_lock:
                    ollama_raw = list(self._ollama_raw_models)
                    ollama_names = set(self._ollama_models_cache["models"])
                    rapid_raw = list(self._rapid_raw_models)
                all_models = []
                seen = set()
                for m in ollama_raw:
                    mid = m.get("name")
                    if mid and mid not in seen:
                        seen.add(mid)
                        all_models.append({"id": mid, "object": "model", "created": 0, "owned_by": "ollama"})
                # 兜底：原始条目为空但集合非空（例如从旧缓存恢复）时至少把名字列出来
                for mid in sorted(ollama_names - seen):
                    seen.add(mid)
                    all_models.append({"id": mid, "object": "model", "created": 0, "owned_by": "ollama"})
                for e in rapid_raw:
                    raw_id = e.get("id", "")
                    if not raw_id:
                        continue
                    mid = _MODEL_REVERSE_ALIAS.get(raw_id, raw_id)
                    if mid in seen:
                        continue
                    seen.add(mid)
                    all_models.append({"id": mid, "object": "model", "created": 0, "owned_by": "rapid-mlx"})
                # auto 虚拟模型放在列表末尾：避免改变"客户端默认取第一个模型"的既有行为
                if AUTO_ENABLED and AUTO_MODEL_NAME not in seen:
                    all_models.append({"id": AUTO_MODEL_NAME, "object": "model",
                                       "created": 0, "owned_by": "gateway"})
                    seen.add(AUTO_MODEL_NAME)
                # 泳道池虚拟模型：客户端把 model 填成它，网关按上下文分派到空闲模型
                if LANE_POOL.enabled and LANE_POOL.name not in seen:
                    all_models.append({"id": LANE_POOL.name, "object": "model",
                                       "created": 0, "owned_by": "gateway"})
                    seen.add(LANE_POOL.name)
                body = json.dumps({"object": "list", "data": all_models}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._send_error(500, "internal_error", f"获取模型列表失败: {e}")
            return

        # /api/tags — 合并 Ollama + Rapid-MLX（含 VLM）
        if path == "/api/tags":
            try:
                # 与 /v1/models 同源：读同一份缓存，保留上游原始字段（details / size / modified_at）
                _force = "_refresh=1" in self.path or "refresh=1" in self.path
                self._ensure_list_fresh(force=_force)
                with self._models_cache_lock:
                    ollama_models = list(self._ollama_raw_models)
                    ollama_names = set(self._ollama_models_cache["models"])
                    rapid_raw = list(self._rapid_raw_models)
                # 兜底：集合里有但原始条目缺失的名字，合成最小可用条目
                _have = {m.get("name") for m in ollama_models}
                for mid in sorted(ollama_names - _have):
                    ollama_models.append({"name": mid, "model": mid, "size": 0,
                                          "modified_at": "", "details": {}})
                vlm_models = []
                seen = {m.get("name") for m in ollama_models}
                for e in rapid_raw:
                    mid = e.get("id", "")
                    host = e.get("host", "")
                    if not mid:
                        continue
                    mid = _MODEL_REVERSE_ALIAS.get(mid, mid)
                    if mid in seen:
                        continue
                    seen.add(mid)
                    vlm_models.append({
                        "name": mid, "model": mid, "size": 0, "modified_at": "",
                        "details": {
                            "family": "rapid-mlx", "families": None,
                            "parameter_size": "unknown", "quantization_level": "",
                            "input": ["text", "image"] if host in RAPID_MLX_VLM_HOSTS else ["text"],
                        },
                    })
                merged = ollama_models + vlm_models
                body = json.dumps({"models": merged}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._send_error(500, "internal_error", f"获取模型列表失败: {e}")
            return

        # 鉴权
        auth_result = self._authenticate()
        if auth_result:
            return
        api_key, key_cfg = auth_result_ok
        cfg = _cfg

        

        

        # OpenAI 兼容 /v1/* GET
        if path.startswith("/v1/") or path.startswith("/api/"):
            client_ip = self._real_ip()
            _ok, _how, _ = _acquire_conc_slot(_key_priority(key_cfg), KEY_PRIO_SLOT_WAIT_S)
            if not _ok:
                self._send_error(429, "too_many_requests", "服务器忙")
                return
            with state_lock:
                _rid = next(_req_seq)
                _active_reqs[_rid] = {"ts": time.time(), "sock": self.connection, "freed": False}
            try:
                self._proxy("GET", client_ip, api_key, key_cfg, cfg)
            except Exception as e:
                log(f"❌ do_GET 未捕获异常: {type(e).__name__}: {e}")
                try:
                    self._send_error(500, "internal_error", "服务器内部错误")
                except Exception:
                    pass
            finally:
                _drop_active_req(_rid)
            return

        self._send_error(404, "not_found", "Not Found")

    def do_POST(self):
        global active_concurrency
        self._auto_resolved = None  # 每个请求重置，避免 keep-alive 复用 handler 时残留上次的 auto 结果
        self._auto_tier_label = ""  # 同上：本次 auto 选择使用的请求分档（light/standard/deep）
        self._normalize_path()
        path = self.path.split("?", 1)[0]

        if path.startswith("/api/admin/"):
            self._handle_admin("POST", path)
            return

        # 读取 body（带大小限制，防止超大请求导致 OOM）
        MAX_BODY = 50 * 1024 * 1024  # 50MB 上限
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        if content_length > MAX_BODY:
            self._send_error(413, "payload_too_large",
                             f"请求体过大 ({content_length} bytes)，上限 {MAX_BODY // 1024 // 1024}MB")
            return
        try:
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
        except (ConnectionError, TimeoutError) as e:
            log(f"⚠️ 读取请求体失败: {e}")
            return

        # 鉴权
        auth_err = self._authenticate()
        if auth_err:
            return
        api_key, key_cfg = auth_result_ok
        cfg = _cfg
        # 这条 key 的调度优先级（只来自配置）。后面两处排队都用它：
        #   ① 全局并发槽（短窗口：槽满时 VIP 可以等一个空位，而不是立刻 429）；
        #   ② 泳道队列（长窗口：VIP 排到队首，资源一空出来先给它）。
        _prio = _key_priority(key_cfg)

        # 解析请求体（容错 BOM 和空白）
        try:
            req = json.loads(body_bytes.decode("utf-8").lstrip("\ufeff").strip()) if body_bytes else {}
        except Exception:
            self._send_error(400, "bad_request", "无效的 JSON 请求体")
            return

        model = req.get("model", "")

        # 模型别名解析：短名 → 真实 ID（必须在存在性检查和白名单之前）
        if model and model in MODEL_ALIASES:
            model = MODEL_ALIASES[model]
            req["model"] = model
            body_bytes = json.dumps(req, ensure_ascii=False).encode("utf-8")

        # 兼容"完整 URL"模式：POST 到 /v1 或 / 且请求体像聊天/完成请求时自动路由
        if path in ("/", "/v1"):
            if "messages" in req:
                path = "/v1/chat/completions"
                self.path = path + ("?" + self.path.split("?", 1)[1] if "?" in self.path else "")
            elif "prompt" in req:
                path = "/v1/completions"
                self.path = path + ("?" + self.path.split("?", 1)[1] if "?" in self.path else "")

        # ---- 泳道池虚拟模型：把上下文分派到空闲的模型上 ----
        # 解析必须在白名单/存在性检查之前：`pool` 不是真模型名。
        # 这里只**定型**（是不是泳道请求、上下文是谁），真正的取泳道放在额度检查之后，
        # 因为排队可能要等几十秒，不该在排队前就把额度/并发账算掉。
        _lane_hint = (self.headers.get("X-TG-Lane") or "").strip()
        _pool_req = bool(model == LANE_POOL_MODEL or _lane_hint) and LANE_POOL.enabled
        _lane_ctx = ""
        if model == LANE_POOL_MODEL and not LANE_POOL.enabled:
            self._send_error(503, "lane_pool_disabled",
                             f"模型 '{LANE_POOL_MODEL}' 是泳道池虚拟模型，但网关没配 TG_LANES。"
                             f"示例：TG_LANES='flash=qwen3.8-flash-next|100|1,light=gemma4-9b|10|1'")
            return
        if _pool_req:
            _lane_ctx = _lane_context_key(self.headers, req)

        # ---- auto 虚拟模型：从本地可用模型池自动分配 ----
        # 必须在白名单/存在性检查之前解析成真实模型名：后续上下文窗口、图片过滤、
        # 上游路由、用量记录全部按真实模型处理，行为与直接指定该模型完全一致。
        # deny_models 中显式写入 auto 视为禁用：此时不解析，落到后面的白名单检查返回 403。
        _auto_denied = AUTO_MODEL_NAME in (cfg["global"].get("deny_models", []) or [])
        if AUTO_ENABLED and model == AUTO_MODEL_NAME and not _auto_denied:
            picked, picked_host, why = self._auto_pick_model(path, req, cfg)
            if not picked:
                self._send_error(503, "auto_no_candidate",
                                 f"auto 暂无可分配模型（{why}）。请确认上游已加载模型，"
                                 f"或检查 allow_models / deny_models / TG_AUTO_EXCLUDE。")
                return
            self._auto_resolved = picked
            model = picked
            req["model"] = picked
            body_bytes = json.dumps(req, ensure_ascii=False).encode("utf-8")
            log(f"🎲 auto → {picked} @ {picked_host}（{why}）→ {self._real_ip()}")

        # 模型白名单（auto 的候选池已在 _auto_candidates 内按 allow/deny 过滤，此处跳过重复校验）
        # 泳道池请求同样跳过：真实模型由泳道配置决定，白名单在配置侧把关。
        if model and not self._auto_resolved and not _pool_req and not self._check_model_allowed(model, cfg):
            self._send_error(403, "model_not_allowed", f"模型 '{model}' 不在白名单中")
            return

        # 模型存在性预检查（避免 Ollama 返回不明确的 404）
        if model and not _pool_req and not self._ollama_model_exists(model):
            # 可用列表要排除熔断中的模型：否则会出现"模型 X 不存在，可用模型: X, Y"
            # 这种自相矛盾的话（X 仍在 /api/tags 里，但已判定加载不了）。
            ollama_models, rapid_models = self._refresh_model_caches()
            avail = sorted(set(ollama_models) | set(rapid_models.keys()))
            avail = [m for m in avail if not self._is_model_bad(m)]
            available = ", ".join(avail) or "无"
            if self._is_model_bad(model):
                ttl = _env_num("TG_BAD_MODEL_TTL", 120, minimum=0)
                self._send_error(404, "model_unavailable",
                                 f"模型 '{model}' 暂不可用（上游加载失败，已熔断 {ttl}s 后自动重试）。"
                                 f"可用模型: {available}")
            else:
                self._send_error(404, "model_not_found",
                                 f"模型 '{model}' 不存在。可用模型: {available}")
            return

        # 工具能力预检查：已知不支持 tools 的模型（如 qwen2.5vl:3b）不必再浪费一次上游往返，
        # 也不该让客户端收到带 registry 路径的原始 400。auto 已在上面的候选池里过滤过。
        if (model and not self._auto_resolved and not _pool_req and self._auto_req_has_tools(req)
                and self._auto_supports_tools(model, self._resolve_upstream(model, path)) is False):
            self._send_error(400, "model_no_tools",
                             f"模型 '{model}' 不支持工具调用（tools）。"
                             f"请改用支持 tools 的模型（选 auto 时网关会自动挑），"
                             f"或把该模型加入 deny_models / TG_AUTO_EXCLUDE。")
            return

        # 估算 tokens (prompt_tokens + max_tokens) 用于额度预检查
        est = 1000
        try:
            est = req.get("max_tokens", 1024)
            msgs = req.get("messages", [])
            prompt_est = sum(len(m.get("content", "")) // 4 + 1 for m in msgs)
            est += prompt_est
        except Exception:
            pass

        # QPS 检查
        if key_cfg.get("rate_limited", True):
            if not check_qps(api_key, key_cfg.get("qps", DEFAULT_QPS)):
                self._send_error(429, "rate_limit_exceeded",
                                 f"请求过于频繁，QPS 限制: {key_cfg.get('qps', DEFAULT_QPS)}/秒")
                return

        # 额度检查
        ok, reason = tracker.check_quota(api_key, key_cfg, est)
        if not ok:
            self._send_error(429, "quota_exceeded", reason)
            return

        # ---- 取泳道（一个模型一个上下文；全忙则在此排队）----
        # 位置很关键：在**额度检查之后、并发槽位之前**。放在前面会让排队中的请求先吃掉
        # 额度（还没跑就记账），放在后面会让排队中的请求长期占着 max_concurrency 槽，
        # 第 4 个上下文就被 429 挡掉 —— 而需求正是"排队等空闲模型"，不是拒绝。
        _pool_lane = None
        if _pool_req:
            if _lane_hint and _lane_hint not in LANE_POOL._by_name:
                self._send_error(400, "lane_unknown",
                                 f"没有名为 '{_lane_hint}' 的泳道。现有："
                                 f"{', '.join(l['name'] for l in LANE_POOL._lanes)}")
                return
            try:
                # est_tokens 用**生成长度**而不是 prompt+max：spb 是"每个生成 token 的
                # 秒数"，把 prompt tokens 也算进去会把一单的预计时长放大十几倍，
                # 队列预测随之失真（实测表现为"明明该等快模型，却跑去慢模型"）。
                _gen_est = int(req.get("max_tokens") or req.get("max_completion_tokens") or 512)
                _pool_lane, _lane_wait = LANE_POOL.acquire(_lane_ctx, lane=_lane_hint,
                                                           est_tokens=_gen_est,
                                                           priority=_prio)
            except LaneQueueFull as e:
                self._send_error(429, "lane_queue_full", str(e))
                return
            except LaneTimeout as e:
                self._send_error(429, "lane_wait_timeout", str(e))
                return
            self._lane = _pool_lane
            self._lane_ctx_key = _lane_ctx
            self._lane_wait_s = _lane_wait
            self._lane_t0 = time.time()
            # 加入时的并发档位（1=独占独占档，2=与另一单共享上游）：归还时要按这一档
            # 记速度画像，否则合批/排队带来的单请求变慢会被算进"单请求速度"里。
            self._lane_conc = LANE_POOL.admit_conc()
            model = _pool_lane["model"]
            req["model"] = model
            body_bytes = json.dumps(req, ensure_ascii=False).encode("utf-8")
            log(f"🚦 泳道 {_pool_lane['name']} ← {_lane_ctx}"
                f"{' ×%d' % self._lane_conc if self._lane_conc > 1 else ''}"
                f"{' p%d' % _prio if _prio else ''}"
                f"{'（等 %.2fs）' % _lane_wait if _lane_wait > 0.05 else ''} → {model}")

        # 并发检查（优先级在这里起作用：VIP 槽满时有界等待，普通请求仍快速 429）
        _slot_ok, _slot_how, _slot_wait = _acquire_conc_slot(_prio, KEY_PRIO_SLOT_WAIT_S)
        if not _slot_ok:
            # 先把泳道还回去：等槽位的这段时间它一直被占着，超时后必须释放
            self._release_lane()
            if _slot_how == "timeout":
                self._send_error(429, "priority_wait_timeout",
                                 f"高优先级请求等待并发槽超过 {KEY_PRIO_SLOT_WAIT_S:g}s"
                                 f"（并发上限 {_conc_limit()}）")
            else:
                self._send_error(429, "too_many_requests", "服务器忙，请稍后再试")
            return
        with state_lock:
            _rid = next(_req_seq)
            _active_reqs[_rid] = {"ts": time.time(), "sock": self.connection, "freed": False}
        if _slot_wait > 0.05:
            log(f"⏳ 优先级 {_prio} 等并发槽 {_slot_wait:.2f}s 后拿到"
                f"（{active_concurrency}/{_conc_limit()}）")

        client_ip = self._real_ip()
        # 记录该模型在飞请求数：auto 的 least-busy 策略据此判断哪个模型繁忙
        _inflight_model = model
        model_inflight_inc(_inflight_model)
        self._inflight_model = _inflight_model
        self._inflight_released = False
        _lane_err = False
        try:
            self._proxy_serialized("POST", client_ip, api_key, key_cfg, cfg,
                                   body_bytes=body_bytes, original_req=req, model=model,
                                   upstream_host=(_pool_lane or {}).get("host", ""))
        except Exception as e:
            # 兜底：所有未预期异常都不应让并发计数泄漏
            # 记录完整堆栈：仅打 type+message 时（如 KeyError: 'max_tokens'）无法定位到行号，
            # 曾因此多花一轮排查。堆栈只写本地日志，不返回给客户端。
            log(f"❌ do_POST 未捕获异常: {type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}")
            _lane_err = True
            try:
                self._send_error(500, "internal_error", "服务器内部错误")
            except Exception:
                pass
        finally:
            # 泳道必须先还回去：漏还等于这个模型永久消失，比丢一个请求严重得多。
            self._release_lane(error=_lane_err or (getattr(self, "_resp_status", 0) or 0) >= 500)
            # _proxy 通常已经在上游读完时释放过；这里只兜底真正漏掉的情况
            if not getattr(self, "_inflight_released", False):
                model_inflight_dec(_inflight_model)
            # 释放槽位；reaper 若已因"客户端走了"提前回收过，这里就不要再减
            _drop_active_req(_rid)

    def _release_lane(self, error: bool = False):
        """归还泳道（幂等：多次调用只生效一次）。"""
        lane = getattr(self, "_lane", None)
        if lane is None:
            return
        self._lane = None
        try:
            # tokens/elapsed 用于更新该泳道的速度画像（决定以后"在谁那儿先做完"）；
            # conc 让画像分档记（1 档 vs 合批档），也是自动降档的输入。
            LANE_POOL.release(lane, error=bool(error),
                              tokens=int(getattr(self, "_lane_ct", 0) or 0),
                              elapsed=max(0.0, time.time() - getattr(self, "_lane_t0", time.time())),
                              conc=int(getattr(self, "_lane_conc", 0) or 0))
        except Exception as e:      # 归还路径绝不抛：抛了会盖住真正的错误
            log(f"⚠️ 泳道归还失败 {lane.get('name')}: {type(e).__name__}: {e}")

    def do_PUT(self):
        self._normalize_path()
        if self.path.startswith("/api/admin/"):
            self._handle_admin("PUT", self.path.split("?", 1)[0])
        else:
            self._send_error(404, "not_found", "Not Found")

    def do_DELETE(self):
        self._normalize_path()
        if self.path.startswith("/api/admin/"):
            self._handle_admin("DELETE", self.path.split("?", 1)[0])
        else:
            self._send_error(404, "not_found", "Not Found")

    # ---- 鉴权 ----
    def _authenticate(self) -> Optional[str]:
        """设置 auth_result_ok 或返回错误响应"""
        global auth_result_ok
        api_key = self._extract_api_key()
        if not api_key:
            self._send_error(401, "invalid_api_key",
                "缺少 API Key。请在 Authorization: Bearer YOUR_KEY 请求头中提供。")
            return "err"
        cfg = _cfg
        key_cfg = cfg["keys"].get(api_key)
        if not key_cfg:
            self._send_error(401, "invalid_api_key", "无效的 API Key")
            return "err"
        if not key_cfg.get("active", True):
            self._send_error(403, "api_key_disabled", "该 API Key 已被停用")
            return "err"
        auth_result_ok = (api_key, key_cfg)
        return None

    # ---- 代理转发 ----
    # 客户端断开类异常：捕获后静默退出，避免污染日志和堆栈
    _CLIENT_GONE_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def _proxy_serialized(self, method: str, client_ip: str, api_key: str,
                          key_cfg: dict, cfg: dict, upstream_host: str = "", **kw):
        """把发往 Rapid-MLX(gmlx) 的**生成**请求按需串行化。

        粒度：**只串行带投机解码（MTP）的上游模型**。缺陷在 speculative 的 mid-flight
        注入路径上：当一个请求正在 decode、另一个被塞进同一 batch 时，draft 张量宽度与
        目标 batch 不符，整批 ValueError（两个并发请求 100% 双双 500）。
        **不带投机的模型走不到那段代码** —— 实测 gemma4-9B / qwen3.8-27B 同模型 4 并发
        ×4 轮，输出与串行基准逐字节一致、0 错误，且 n=4 时吞吐 216 / 53 tok/s（1.99x / 1.65x）。
        不同上游模型各有自己的 BatchGenerator，本来就互不干扰。
        锁键的取法见 _mlx_lock_key；`TG_MLX_SERIAL_MODE=model` 可回退到"每模型都串行"，
        `=host` 回退到最早的全 host 串行。

        排队纪律：等待者是 `_PrioSerialGate`（见其文档），按 API key 的 priority 插队，
        判定点仍只有 `_prio_turn` 一处，让路窗口 LANE_PRIO_HOLD_S 过后恢复先来后到。
        这条很重要 —— `auto` 解析出真实模型后不走 LanePool，并发槽（默认 10）通常也
        不满，所以**这条锁是 VIP 与普通请求唯一的相遇点**：它是裸 Semaphore 的时候，
        Alex 的 priority=100 在 dsh 主路径上完完全全是空转。
        """
        path = self.path.split("?", 1)[0]
        is_gen = path.startswith("/v1/chat") or path.startswith("/v1/completions")
        model = kw.get("model", "")
        # 泳道池会把上游 host 显式传进来（同 host 多模型时也能指定），否则按模型名解析
        host = upstream_host or self._resolve_upstream(model, self.path)
        if not (is_gen and MLX_SERIAL and host in RAPID_MLX_HOSTS):
            return self._proxy(method, client_ip, api_key, key_cfg, cfg,
                               upstream_host=host, **kw)
        # 键的粒度见 _mlx_lock_key；空串 = 该上游模型不带投机解码，可以安全并发
        lock_key = _mlx_lock_key(host, model)
        if not lock_key:
            return self._proxy(method, client_ip, api_key, key_cfg, cfg,
                               upstream_host=host, **kw)
        # 优先级：与泳道队列/并发槽同一个来源（key 配置的 priority），也同一个判定点
        # （_prio_turn）。这条锁是 auto → gmlx 带投机模型的必经之路，之前不给优先级，
        # 于是 VIP 的插队在 dsh 主路径上是空转的。
        prio = _key_priority(key_cfg)
        gate = _mlx_gate_for(lock_key)
        t0 = time.time()
        got, _how, _wait, _skipped = gate.acquire_ex(priority=prio, timeout=MLX_SERIAL_WAIT)
        _wait = time.time() - t0          # 只记"等锁"的时长，不含生成耗时
        if not got:
            log(f"⏳ {lock_key} 串行队列等待 {MLX_SERIAL_WAIT}s 仍未空出，拒绝本次请求")
            self._send_error(
                429, "too_many_requests",
                f"上游模型正在处理其他请求，排队超过 {MLX_SERIAL_WAIT:g}s，请重试")
            return
        if _skipped:
            log(f"⏩ 优先级 {prio} 插队拿到串行锁 {lock_key}"
                f"（等 {_wait:.2f}s，跳过 {_skipped} 个等待者）")
        try:
            return self._proxy(method, client_ip, api_key, key_cfg, cfg,
                               upstream_host=host, **kw)
        finally:
            gate.release()
            if _wait > 1.0:
                log(f"⏳ {lock_key} 串行排队 {_wait:.1f}s 后放行")

    def _proxy(self, method: str, client_ip: str, api_key: str, key_cfg: dict, cfg: dict,
               body_bytes: bytes = b"", original_req: dict = None, model: str = "",
               upstream_host: str = ""):

        upstream_host = upstream_host or self._resolve_upstream(model, self.path)
        url = f"{upstream_host}{self.path}"
        stream = bool(original_req and original_req.get("stream", False)) if original_req else False

        # ========= 流式请求索取 usage（配额记账精度，默认关闭） =========
        # Ollama / llama-server / mlx_vlm 的 SSE 默认**不带 usage**，网关只能估算；而旧的
        # 估算是「原始字节 // 3」，把 SSE 每帧约 230 字节的 `data: {...}` 框架也算进去：
        # 实测一个真实 64 token 的流式回复被记成 4,389 token（约 68 倍），配额被吃光。
        # 估算本身已改为按增量文本字符折算（见下方解析段），这条注入是"要精确值"的开关。
        #
        # 为什么默认关闭：索取 usage 后上游会在 [DONE] 前多发一个 `choices: []` 的帧，
        # 这是 OpenAI 的标准行为，但**手写** SSE 解析器常写成 `chunk["choices"][0]`，
        # 遇到空数组会崩。网关自己转发的是字节流、不重写分片，无法只对该帧做过滤，
        # 所以不把这个行为默认强加给客户端。确认客户端能耐受时再开：TG_STREAM_INCLUDE_USAGE=1。
        _want_usage = os.environ.get("TG_STREAM_INCLUDE_USAGE", "0") not in ("0", "false", "no", "off")
        if (_want_usage and stream and original_req is not None
                and self.path.startswith("/v1/chat") and "stream_options" not in original_req):
            original_req["stream_options"] = {"include_usage": True}
            body_bytes = json.dumps(original_req, ensure_ascii=False).encode("utf-8")

        # ========= Rapid-MLX 图片过滤（纯文本模型不支持多模态） =========
        if upstream_host in RAPID_MLX_HOSTS and upstream_host not in RAPID_MLX_VLM_HOSTS and original_req and self.path.startswith("/v1/chat"):
            _msgs = original_req.get("messages", [])
            _stripped = False
            for _msg in _msgs:
                _content = _msg.get("content")
                if isinstance(_content, list):
                    _filtered = []
                    for _part in _content:
                        if isinstance(_part, dict) and _part.get("type") in ("image_url", "image", "input_image"):
                            _stripped = True
                            continue
                        _filtered.append(_part)
                    _msg["content"] = _filtered
            if _stripped:
                original_req.pop("images", None)
                original_req.pop("image", None)
                body_bytes = json.dumps(original_req, ensure_ascii=False).encode("utf-8")
                log(f"🖼️  已过滤图片请求（{model} 不支持多模态）→ 来自:{client_ip}")

        # ========= 按模型的缺省请求体字段（TG_MODEL_EXTRA） =========
        # 只补客户端没给的字段；客户端显式设了就一律不动。
        if original_req is not None and self.path.startswith("/v1/chat"):
            _added = _apply_model_extra(original_req, model)
            if _added:
                body_bytes = json.dumps(original_req, ensure_ascii=False).encode("utf-8")
                log(f"🎛️  补默认请求字段 {_added} → {model}")

        # ========= 输出预算下限（Ollama 等非 Rapid-MLX 上游） =========
        # 下方"Context Window 自动截断降级"整块嵌在 `upstream_host in RAPID_MLX_HOSTS`
        # 里，所以 Ollama 侧原本**完全没有** max_tokens 下限：gemma4 这类会"思考"的模型
        # 先把预算烧在思维链上，预算耗尽时 content 为空、finish_reason=length，
        # 客户端只看到一条空回复。这不是理论风险，已实测复现：
        #   max_tokens=64  → content 0 字符 / reasoning 233 字符 / completion_tokens=64
        #   max_tokens=192 → 同样为空；4096 时 reasoning 可到 1960 字符才出正文
        # 且 gemma4 是否触发思维链是概率性的（同一句提示另一次直接 34 token 出结果），
        # 所以下限只能降低概率，真正兜底的是后面的"推理模型空正文修复"。
        # 注：gemma4 确实忽略 think / reasoning_effort（已实测），但 gpt-oss **认**
        # reasoning_effort（low 让同题从 300 token/3.35s 降到 119 token/1.51s），
        # 想压低它的思考量请用 TG_MODEL_EXTRA 显式配置，不要指望全局开关。
        if (original_req and self.path.startswith("/v1/chat")
                and upstream_host not in RAPID_MLX_HOSTS):
            try:
                _o_max_tokens = int(original_req.get("max_tokens") or 4096)
            except (TypeError, ValueError):
                _o_max_tokens = 4096
            _o_tools = bool(original_req.get("tools") or original_req.get("functions"))
            _o_img = self._auto_req_has_image(original_req)
            _o_floor = _min_max_tokens_for(False, False, _o_img, _o_tools)
            if _o_floor and _o_max_tokens < _o_floor:
                _o_why = "工具调用预算" if _o_tools else "文本输出下限"
                log(f"⬆️  提升 max_tokens: {_o_max_tokens} → {_o_floor} ({_o_why}) → {client_ip}")
                original_req["max_tokens"] = _o_floor
                body_bytes = json.dumps(original_req, ensure_ascii=False).encode("utf-8")

        # ========= Context Window 自动截断降级 =========
        if upstream_host in RAPID_MLX_HOSTS and original_req and self.path.startswith("/v1/chat"):
            _cw = _MODEL_CONTEXT_WINDOWS.get(model, _MODEL_CONTEXT_WINDOWS.get("*", 262144))
            _msgs = original_req.get("messages", [])
            # 统一用局部变量 `_max_tokens` 表示"本次实际生效的输出上限"。
            # 早期版本在下述几处日志里直接读 original_req['max_tokens']，一旦客户端未传该
            # 字段（IDE 类客户端常见）且本轮又没有写过它，就会抛 KeyError: 'max_tokens'
            # ——表现为 500 internal_error。此处同时做类型兜底，避免字符串型值参与比较。
            try:
                _max_tokens = int(original_req.get("max_tokens") or 4096)
            except (TypeError, ValueError):
                _max_tokens = 4096
            # 强制最小 max_tokens，防止 dsh 等客户端发送过小的 max_tokens 导致回复被截断。
            # 决策统一收口在 _min_max_tokens_for()，两个上游分支共用同一套规则。
            # 本分支（Rapid-MLX）的结论是：带图 → 0（维持原语义）；
            # 纯文本 + 工具 → TG_MIN_MAX_TOKENS_TOOLS；纯文本无工具 → 0。
            _has_tools = bool(original_req.get("tools") or original_req.get("functions"))
            _MIN_MAX_TOKENS = _min_max_tokens_for(
                True,
                upstream_host in RAPID_MLX_VLM_HOSTS,
                self._auto_req_has_image(original_req),
                _has_tools,
            )
            if _MIN_MAX_TOKENS and _max_tokens < _MIN_MAX_TOKENS:
                _why = "工具调用预算" if _has_tools else "强制最小值"
                log(f"⬆️  提升 max_tokens: {_max_tokens} → {_MIN_MAX_TOKENS} ({_why}) → {client_ip}")
                original_req["max_tokens"] = _MIN_MAX_TOKENS
                _max_tokens = _MIN_MAX_TOKENS
                body_bytes = json.dumps(original_req, ensure_ascii=False).encode("utf-8")
            # 保守 token 估算: 按 chars/1 算（中文接近 1:1），再加 10% 安全系数
            def _est_msg_tokens(m):
                c = m.get("content", "")
                if isinstance(c, list):
                    c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                if isinstance(c, str):
                    return max(1, int(len(c) * 1.1))
                return 4
            _prompt_tokens = sum(_est_msg_tokens(m) for m in _msgs)
            _safety = 4096
            _hard_max = _cw - _safety
            _truncated = False

            # 硬限制: max_tokens 绝不能超过 _hard_max
            if _max_tokens > _hard_max:
                _max_tokens = int(_hard_max)
                original_req["max_tokens"] = _max_tokens
                _truncated = True
                log(f"✂️  max_tokens 硬限制 → {_max_tokens} (context={_cw}, 硬上限={_hard_max}) → {client_ip}")

            _total_est = _prompt_tokens + _max_tokens
            if _total_est > _cw - _safety:
                _room = _cw - _prompt_tokens - _safety
                if _room > 1024:
                    if _max_tokens > _room:
                        _max_tokens = max(512, int(_room))
                        original_req["max_tokens"] = _max_tokens
                        _truncated = True
                        log(f"✂️  自动降 max_tokens → {_max_tokens} (prompt~{int(_prompt_tokens)}, context={_cw}) → {client_ip}")
                else:
                    while len(_msgs) > 2 and _prompt_tokens + _safety > _cw:
                        _rm = _msgs.pop(1)
                        _prompt_tokens -= _est_msg_tokens(_rm)
                        _truncated = True
                    if _prompt_tokens + _max_tokens > _cw - _safety:
                        _max_tokens = max(256, int(_cw - _prompt_tokens - _safety))
                        original_req["max_tokens"] = _max_tokens
                    if _truncated:
                        log(f"✂️  截短历史消息 + 降 max_tokens → prompt~{int(_prompt_tokens)}, max={_max_tokens} → {client_ip}")
            if _truncated:
                body_bytes = json.dumps(original_req, ensure_ascii=False).encode("utf-8")

        # ========= Rapid-MLX 半熔断延迟 =========
        # 如果上游是 Rapid-MLX 且连续 OOM 达到阈值，先延迟几秒给服务喘息
        _circuit_delay = 0.0
        if upstream_host in RAPID_MLX_HOSTS:
            with _mlx_circuit_lock:
                count = _mlx_oom_count.get(upstream_host, 0)
                last_ts = _mlx_last_oom_ts.get(upstream_host, 0)
                if count >= MLX_CIRCUIT_THRESHOLD and (time.time() - last_ts) < 120:
                    # 半熔断状态：延迟随错误次数递增（最多 1.5s）
                    _circuit_delay = min(MLX_CIRCUIT_DELAY_BASE * (count - MLX_CIRCUIT_THRESHOLD + 1), 1.5)
                    log(f"⚡ Rapid-MLX 半熔断（连续 {count} 次 OOM），延迟 {_circuit_delay:.1f}s")
                    time.sleep(_circuit_delay)

        # 构造上游请求（去掉可能的 Authorization header）
        upstream_headers = {}
        for h in ("Content-Type", "Accept", "Accept-Encoding"):
            v = self.headers.get(h)
            if v:
                upstream_headers[h] = v
        upstream_headers["X-Forwarded-For"] = client_ip
        upstream_headers["X-Forwarded-Proto"] = "http"

        # VLM 上游需要 Authorization
        if upstream_host in RAPID_MLX_VLM_HOSTS:
            upstream_headers["Authorization"] = f"Bearer {MLX_VLM_API_KEY}"

        # 超时语义：TG_READ_TIMEOUT 是**单次静默上限**，不是整段耗时上限。
        # 5 万 token 上下文的预填充本身就要 100~150s，旧版把 240s 当整段上限，
        # 时间一到就砍连接，客户端只看到"用户已取消"（连接被截断）。
        # 现在它同时作用于两处：等上游响应头、以及每次读取响应体（阻塞读的内部计时）。
        # 注意：http.client 的响应对象在**一次 socket 超时后不可复用**
        # （报 "cannot read from timed out object"），所以不能把读切成小段轮询，
        # 一旦超时只能整条放弃——这也正是这里必须给足预算的原因。
        # 多模态（带图）请求单独用更短的 TG_VLM_READ_TIMEOUT：视觉决策正常 10~30s 就回。
        connect_timeout = _env_num("TG_CONNECT_TIMEOUT", 10, minimum=1)
        read_timeout = _env_num("TG_READ_TIMEOUT", 900, minimum=1)
        _req_has_image = False
        if isinstance(original_req, dict):
            for _m in original_req.get("messages", []) or []:
                _c = _m.get("content") if isinstance(_m, dict) else None
                if isinstance(_c, list) and any(
                    isinstance(_p, dict) and _p.get("type") in ("image_url", "image", "input_image")
                    for _p in _c
                ):
                    _req_has_image = True
                    break
            if not _req_has_image and (original_req.get("images") or original_req.get("image")):
                _req_has_image = True
        if _req_has_image:
            read_timeout = _env_num("TG_VLM_READ_TIMEOUT", 120, minimum=1)

        t0 = time.time()
        resp = None
        last_exc = None
        _err_body = b""

        # ---- auto 模型可用性降级 ----
        # `/api/tags` 只说明 manifest 在不在，不代表权重能加载（被删/损坏/拉取中断都算）。
        # auto 选中的模型一旦在上游被判定不可用，这里自动换下一个候选重试，
        # 而不是把上游的 404 原样打回客户端 —— 那会让"池子里还有别的可用模型"
        # 这件事完全失效，用户侧看起来就是"模型没了，网关也跟着废了"。
        # 显式点名某个模型的请求**不降级**：用户要的就是它，拿不到应当明确报错。
        _max_switch = _env_num("TG_AUTO_MODEL_FALLBACK", 2, minimum=0)
        _switched = 0

        def _looks_model_unavailable(code: int, text: str) -> bool:
            """上游错误是否属于"模型本身不可用"（而非参数错/内容错）。"""
            if code not in (400, 404, 422, 500):
                return False
            t = (text or "").lower()
            if not t:
                return code == 404
            for kw in ("not found", "no such model", "not exist", "unknown model",
                       "failed to load", "unable to load", "model not loaded",
                       "does not exist", "error loading model"):
                if kw in t:
                    return True
            return False

        _req_has_tools = bool(isinstance(original_req, dict)
                              and (original_req.get("tools") or original_req.get("functions")))

        def _looks_tools_unsupported(code: int, text: str) -> bool:
            """上游是否在说"这个模型不支持 tools"。

            与"模型不可用"必须分开处理：模型本身是好的，只是没有工具调用能力，
            把它写进熔断表会让普通对话也跟着被拒 120 秒。
            """
            if code not in (400, 422):
                return False
            t = (text or "").lower()
            if "tool" not in t:
                return False
            return ("not support" in t) or ("unsupported" in t) or ("cannot use tool" in t)

        while True:
            resp = None
            last_exc = None
            _err_body = b""
            # 上游重试：对连接类错误重试 1 次（Ollama 偶发抖动）
            for attempt in range(2):
                try:
                    req = urllib.request.Request(url, data=body_bytes or None, method=method, headers=upstream_headers)
                    resp = urllib.request.urlopen(req, timeout=read_timeout)
                    break
                except urllib.error.HTTPError as e:
                    # HTTP 错误（如 404/500）不重试，但要留住响应体：
                    # 判断"模型不可用"和最终透传都要用它，而 HTTPError 的流只能读一次。
                    last_exc = e
                    try:
                        _err_body = e.read() or b""
                    except Exception:
                        _err_body = b""
                    break
                except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                    last_exc = e
                    # 超时 ≠ 连接抖动：重试只会把已经等过的预算再翻一倍，
                    # 5 万 token 的生成也没必要跑两遍。直接判定超时。
                    if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                        break
                    if attempt == 0 and resp is None:
                        # 首次连接失败，等 1 秒后重试一次
                        log(f"⚠️ 上游连接失败，1秒后重试: {e}")
                        time.sleep(1)
                        continue
                    break

            if resp is not None:
                # 成功一次即解除熔断（同一模型下次还能被选）
                self._mark_model_good(model)
                break

            _is_http = isinstance(last_exc, urllib.error.HTTPError)
            _err_text = _err_body.decode("utf-8", errors="replace")
            _tools_unsup = _is_http and _req_has_tools and _looks_tools_unsupported(last_exc.code, _err_text)
            _unavail = _is_http and _looks_model_unavailable(last_exc.code, _err_text)
            if ((_unavail or _tools_unsup) and self._auto_resolved and _switched < _max_switch):
                # 两条路径的记账方式不同：不可用→熔断；不支持 tools→只记能力边界
                if _tools_unsup:
                    self._mark_model_no_tools(model)
                else:
                    self._mark_model_bad(model, f"HTTP {last_exc.code}")
                _nm, _nh, _nwhy = self._auto_pick_model(self.path, original_req or {}, cfg)
                if _nm and _nm != model:
                    _why_switch = "不支持 tools" if _tools_unsup else "不可用"
                    log(f"🔁 auto 降级：{model} {_why_switch} → 换 {_nm} @ {_nh}（{_nwhy}）")
                    model = _nm
                    self._auto_resolved = _nm
                    if isinstance(original_req, dict):
                        original_req["model"] = _nm
                        body_bytes = json.dumps(original_req, ensure_ascii=False).encode("utf-8")
                    upstream_host = _nh
                    url = f"{upstream_host}{self.path}"
                    upstream_headers = {}
                    for h in ("Content-Type", "Accept", "Accept-Encoding"):
                        v = self.headers.get(h)
                        if v:
                            upstream_headers[h] = v
                    upstream_headers["X-Forwarded-For"] = client_ip
                    upstream_headers["X-Forwarded-Proto"] = "http"
                    if upstream_host in RAPID_MLX_VLM_HOSTS:
                        upstream_headers["Authorization"] = f"Bearer {MLX_VLM_API_KEY}"
                    _switched += 1
                    continue
            break

        # 处理连接失败（重试后仍失败）
        if resp is None:
            elapsed = time.time() - t0
            if isinstance(last_exc, urllib.error.HTTPError):
                e = last_exc
                # 响应体已在上面读取并缓存（HTTPError 的流只能读一次）
                err_body = _err_body
                if _req_has_tools and _looks_tools_unsupported(
                        e.code, err_body.decode("utf-8", errors="replace")):
                    # 上游原话带 `registry.ollama.ai/library/...` 前缀，客户端只会把它当成
                    # 莫名其妙的 400 弹出来。换成人能看懂的原因 + 可执行的处置建议。
                    self._mark_model_no_tools(model)
                    log(f"🧰 上游拒绝工具调用：{model} 不支持 tools | 来自:{client_ip}")
                    try:
                        self._send_error(
                            400, "model_no_tools",
                            f"模型 '{model}' 不支持工具调用（tools）。"
                            f"请改用支持 tools 的模型（选 auto 时网关会自动挑），"
                            f"或把该模型加入 deny_models / TG_AUTO_EXCLUDE。")
                    except self._CLIENT_GONE_ERRORS:
                        pass
                    return
                try:
                    self.send_response(e.code)
                    for k, v in e.headers.items():
                        kl = k.lower()
                        if kl in ("content-length", "transfer-encoding", "connection"):
                            continue
                        self.send_header(k, v)
                    self._cors()
                    self.end_headers()
                    if err_body:
                        self.wfile.write(err_body)
                except self._CLIENT_GONE_ERRORS:
                    pass  # 客户端已断开
                err_preview = err_body.decode("utf-8", errors="replace")[:300] if err_body else ""
                log(f"⚠️ 上游 Ollama 错误 {e.code}: {e.reason} | 路径:{self.path} | 来自:{client_ip} | 响应:{err_preview}")
            else:
                _to = (isinstance(last_exc, TimeoutError)
                       or isinstance(getattr(last_exc, "reason", None), TimeoutError))
                if _to:
                    # 上游连响应头都没给：明确 504，别让客户端当成"自己取消的"
                    log(f"⏱️ 上游超时 {elapsed:.1f}s（静默预算 {read_timeout}s）"
                        f" | 路径:{self.path} | 来自:{client_ip}")
                    try:
                        self._send_error(
                            504, "upstream_timeout",
                            f"上游 {elapsed:.0f}s 未返回任何响应（静默预算 {read_timeout}s）；"
                            f"可重试，或改用更小的模型 / 更短的上下文")
                    except self._CLIENT_GONE_ERRORS:
                        pass
                    return
                log(f"❌ Ollama 连接失败 ({elapsed:.1f}s): {last_exc}")
                try:
                    self._send_error(502, "bad_gateway",
                                     f"无法连接 Ollama ({OLLAMA_HOST}): {str(last_exc)}")
                except self._CLIENT_GONE_ERRORS:
                    pass
            return

        # socket 轮询：把大块阻塞读切成 poll_s 小段，用于实现"静默预算"和流式心跳。
        # 注意：这里刻意**不**改动已建立连接的 socket 超时。http.client 的响应对象
        # 一旦发生 socket 超时就会被标记为不可读，改造超时会让我们失去重试/继续读取的可能，
        # 因此读取策略是"一次阻塞读到数据或超时"，超时即整条放弃。
        _up_clen = resp.headers.get("Content-Length")

        # 成功连接，处理响应
        client_gone = False
        upstream_fault = ""          # 非空 = 上游读取失败，用于对外返回明确错误
        _timed_out = False           # True = 静默预算耗尽（504），否则按连接故障（502）
        response_data = bytearray()
        last_data_at = time.time()

        def _send_upstream_headers(content_length=None):
            self._resp_status = resp.status
            self.send_response(resp.status)
            # 透传响应头
            for k, v in resp.headers.items():
                kl = k.lower()
                if kl in ("content-length", "transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self._cors()

            # 用量头
            today = get_today()
            month = today[:7]
            used_d = tracker.daily[api_key].get(today, 0)
            used_m = tracker.monthly[api_key].get(month, 0)
            limit_d = key_cfg.get("daily_tokens", 0)
            limit_m = key_cfg.get("monthly_tokens", 0)
            self.send_header("X-Quota-Remaining-Daily",
                           str(max(0, limit_d - used_d) if limit_d else "unlimited"))
            self.send_header("X-Quota-Remaining-Monthly",
                           str(max(0, limit_m - used_m) if limit_m else "unlimited"))
            self.send_header("X-RateLimit-Limit", str(key_cfg.get("qps", DEFAULT_QPS)))
            # 回显这条 key 的调度优先级（0 也发，客户端据此确认自己没配错）
            self.send_header("X-TG-Priority", str(_key_priority(key_cfg)))
            # auto 命中时回传实际选中的模型，便于客户端确认与问题定位
            _auto_hit = getattr(self, "_auto_resolved", None)
            if _auto_hit:
                self.send_header("X-Gateway-Model", _auto_hit)
                _tier = getattr(self, "_auto_tier_label", "")
                if _tier:
                    self.send_header("X-Gateway-Auto-Tier", _tier)
            # 泳道池命中：告诉客户端这次落在哪条泳道、等了多久。客户端据此可以自己
            # 观测"上下文是否粘在同一模型上"，不必翻服务端日志。
            _lane_hit = getattr(self, "_lane", None)
            if _lane_hit:
                self.send_header("X-Gateway-Lane", str(_lane_hit.get("name", "")))
                self.send_header("X-Gateway-Model", str(_lane_hit.get("model", "")))
                self.send_header("X-Gateway-Lane-Wait",
                                 f"{getattr(self, '_lane_wait_s', 0.0):.2f}")
                self.send_header("X-Gateway-Context", str(getattr(self, "_lane_ctx_key", "")))
            # 推理空正文修复命中时回传实际生效的预算，便于客户端/日志侧对齐
            _rrm = getattr(self, "_reasoning_retry_mt", 0)
            if _rrm:
                self.send_header("X-Gateway-Reasoning-Retry", str(_rrm))
            # 非流式补 Content-Length：客户端可据此判断响应是否收全（截断会直接报错，
            # 而不是悄悄当成一次"被取消"的请求）
            if content_length is not None:
                self.send_header("Content-Length", str(content_length))
            self.end_headers()

        try:
            with resp:
                # 流式：头必须先发，客户端才能尽早拿到首个 token；
                # 非流式默认（TG_BUFFER_NONSTREAM=1）等读完再发：上游失败时才能给出
                # 真实的 5xx 和 Content-Length，而不是"截断的 200"。
                # 若某客户端对首字节延迟极敏感，可设 TG_BUFFER_NONSTREAM=0 退回旧行为。
                _buffer_ns = os.environ.get("TG_BUFFER_NONSTREAM", "1") not in ("0", "false", "no", "off")
                _early = stream or not _buffer_ns
                if _early:
                    _send_upstream_headers()
                    try:
                        self.wfile.flush()
                    except self._CLIENT_GONE_ERRORS:
                        client_gone = True

                # 读取响应（流式/早发头：边读边转发；缓冲模式：只累积）
                while not client_gone:
                    try:
                        chunk = resp.read(8192)
                    except (TimeoutError, ConnectionError, OSError,
                            http.client.HTTPException) as e:
                        silent = time.time() - last_data_at
                        if isinstance(e, TimeoutError):
                            _timed_out = True
                            upstream_fault = (f"上游 {silent:.0f}s 无新数据"
                                              f"（静默预算 {read_timeout}s）")
                        else:
                            upstream_fault = f"{type(e).__name__}: {e}"
                        log(f"⚠️ 读取上游响应中断: {upstream_fault} | 已收 "
                            f"{len(response_data)} bytes | 用时 {time.time() - t0:.1f}s")
                        break
                    if not chunk:
                        break
                    last_data_at = time.time()
                    response_data.extend(chunk)
                    if _early:
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except self._CLIENT_GONE_ERRORS:
                            client_gone = True
                            break

                # 完整性校验：上游声明了 Content-Length 却没收全 → 连接被截断，按故障处理
                if not upstream_fault and not _early and _up_clen:
                    try:
                        if int(_up_clen) != len(response_data):
                            upstream_fault = (f"上游响应不完整（收到 {len(response_data)} / "
                                              f"声明 {_up_clen} bytes）")
                            log(f"⚠️ {upstream_fault}")
                    except ValueError:
                        pass

                # ========= 推理模型空正文修复（仅缓冲的非流式） =========
                # 已知现象：gemma4 / r1 这类"会思考"的模型把预算先花在 reasoning 上，
                # 预算耗尽时 message.content 为空、finish_reason=length —— 客户端看到的是
                # 一条"成功但空"的回复（IDE 里表现为模型没说话）。首次实测：
                #   max_tokens=64 → content 0 字符 / reasoning 233 字符 / completion=64
                # 预算下限只能降低概率（是否触发思维链本身是概率性的），这里做确定性兜底：
                # 已读完、还没往客户端写任何字节时，用更大的预算重发**一次**。
                # 流式响应无法回滚已发出的 SSE 分片，故只对缓冲的非流式请求生效。
                if (not _early and not client_gone and not upstream_fault
                        and response_data and original_req):
                    try:
                        _pj = json.loads(bytes(response_data).decode("utf-8", errors="replace"))
                        _pc = (_pj.get("choices") or [{}])[0] if isinstance(_pj, dict) else {}
                        _pmsg = (_pc.get("message") or {}) if isinstance(_pc, dict) else {}
                        _ptext = _pmsg.get("content")
                        _preason = _pmsg.get("reasoning") or _pmsg.get("reasoning_content") or ""
                        _pempty = (not _ptext) or (isinstance(_ptext, str) and not _ptext.strip())
                        if (_pempty and _preason
                                and _pc.get("finish_reason") == "length"):
                            _old_mt = int(original_req.get("max_tokens") or 0)
                            _new_mt = max(
                                _env_num("TG_REASONING_RETRY_TOKENS", 4096, minimum=1),
                                _old_mt * 4,
                            )
                            log(f"🔁 推理模型空正文（reasoning {len(_preason)} 字符，"
                                f"预算 {_old_mt} 已耗尽）→ 以 max_tokens={_new_mt} 重发一次"
                                f" → {client_ip}")
                            _rreq = dict(original_req)
                            _rreq["max_tokens"] = _new_mt
                            _rbody = json.dumps(_rreq, ensure_ascii=False).encode("utf-8")
                            try:
                                _r = urllib.request.Request(
                                    url, data=_rbody, method=method, headers=upstream_headers)
                                with urllib.request.urlopen(_r, timeout=read_timeout) as _rr:
                                    _rdata = _rr.read()
                                _rj = json.loads(_rdata.decode("utf-8", errors="replace"))
                                _rc = ((_rj.get("choices") or [{}])[0].get("message") or {})
                                _rtext = _rc.get("content")
                                if _rtext and str(_rtext).strip():
                                    response_data = bytearray(_rdata)
                                    original_req["max_tokens"] = _new_mt
                                    self._reasoning_retry_mt = _new_mt
                                    log(f"✅ 重发成功：正文 {len(str(_rtext))} 字符"
                                        f"（原为空） → {client_ip}")
                                else:
                                    log(f"⚠️ 重发仍未产出正文，透传原响应 → {client_ip}")
                            except Exception as _re:
                                log(f"⚠️ 重发失败（{type(_re).__name__}: {_re}），透传原响应")
                    except Exception:
                        pass          # 响应不是可解析的 JSON：按原样透传

                if not _early and not client_gone:
                    if upstream_fault:
                        # 明确告知是网关侧上游故障，避免客户端把它显示成"用户已取消"
                        _status = 504 if _timed_out else 502
                        _code = "upstream_timeout" if _timed_out else "upstream_broken"
                        try:
                            self._send_error(
                                _status, _code,
                                upstream_fault + "；可重试，或改用更小的模型 / 更短的上下文")
                        except self._CLIENT_GONE_ERRORS:
                            client_gone = True
                    else:
                        _send_upstream_headers(content_length=len(response_data))
                        self.wfile.write(bytes(response_data))
                elif _early and upstream_fault and not client_gone:
                    if stream:
                        # 已发出部分内容：补一个错误事件（不发 [DONE]，避免被当成正常结束）
                        try:
                            evt = {"error": {"message": upstream_fault,
                                             "type": "upstream_timeout",
                                             "code": "upstream_timeout"}}
                            self.wfile.write(("data: " + json.dumps(evt, ensure_ascii=False)
                                              + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                        except self._CLIENT_GONE_ERRORS:
                            client_gone = True

        except self._CLIENT_GONE_ERRORS:
            client_gone = True
        except Exception as e:
            log(f"⚠️ 转发过程异常: {type(e).__name__}: {e}")

        # 上游已经读完（或已放弃），模型不再被占用 —— 在这里就释放 auto 的繁忙计数，
        # 而不是等客户端把响应体收完。否则"客户端刚收到响应、立刻发下一条"的瞬间会被
        # 误判为该模型繁忙，auto 会跳到更弱的模型（同名同输入却不稳定）。
        _im = getattr(self, "_inflight_model", None)
        if _im and not getattr(self, "_inflight_released", False):
            model_inflight_dec(_im)
            self._inflight_released = True

        # ========= Rapid-MLX 熔断状态更新 =========
        if upstream_host in RAPID_MLX_HOSTS and not client_gone:
            raw_check = bytes(response_data)
            resp_text = raw_check.decode("utf-8", errors="replace")
            # 检测 Rapid-MLX OOM 错误模式
            # 注意：排除 Backpressure / D-METAL-CAP — 那是临时 admission 拒绝，不是真 OOM
            _is_backpressure = (
                "BackpressureError" in resp_text
                or "D-METAL-CAP" in resp_text
                or "would exceed gpu_memory_utilization" in resp_text
            )
            _is_oom = (
                not _is_backpressure
                and (
                    "Internal error during streaming" in resp_text
                    or "metal::malloc" in resp_text
                    or "Resource limit" in resp_text
                    or "Inference aborted" in resp_text
                )
            )
            # 检测成功模式：流式有 finish_reason 或 DONE，非流式有 completion 字段
            _is_success = (
                ("finish_reason" in resp_text or "\"[DONE]\"" in resp_text or "[DONE]" in resp_text)
                or (not stream and "\"choices\"" in resp_text and "\"content\"" in resp_text and "error" not in resp_text[:200])
            )
            with _mlx_circuit_lock:
                if _is_oom:
                    _mlx_oom_count[upstream_host] += 1
                    _mlx_last_oom_ts[upstream_host] = time.time()
                    cnt = _mlx_oom_count[upstream_host]
                    if cnt == MLX_CIRCUIT_THRESHOLD:
                        log(f"⚡ Rapid-MLX 触发半熔断（连续 {cnt} 次 OOM），后续请求将自动延迟")
                    elif cnt > MLX_CIRCUIT_THRESHOLD:
                        log(f"⚡ Rapid-MLX 半熔断中（连续 {cnt} 次 OOM）")
                elif _is_success and _mlx_oom_count.get(upstream_host, 0) > 0:
                    _mlx_oom_count[upstream_host] = 0
                    _mlx_last_oom_ts.pop(upstream_host, None)
                    log(f"✅ Rapid-MLX 恢复正常，熔断计数已重置")

        elapsed = time.time() - t0
        raw_resp = bytes(response_data)

        if client_gone:
            log(f"⚠️ 客户端提前断开: {client_ip} | 已传 {len(raw_resp)} bytes | {elapsed:.1f}s")

        # 解析 token 用量
        total_prompt = 0
        total_completion = 0
        total = 0
        try:
            if stream:
                # SSE: 每行 data: {...}，最后一条带 usage
                lines = raw_resp.decode("utf-8", errors="replace").split("\n")
                _sse_chars = 0        # 增量文本字符数（正文 + 思维链），供无 usage 时估算
                for line in lines:
                    if not line.startswith("data: "):
                        continue
                    d = line[6:].strip()
                    if d == "[DONE]":
                        continue
                    try:
                        chunk_obj = json.loads(d)
                        if chunk_obj.get("usage"):
                            u = chunk_obj["usage"]
                            total_prompt = u.get("prompt_tokens", total_prompt)
                            total_completion = u.get("completion_tokens", total_completion)
                            total = u.get("total_tokens", total)
                        for _ch in (chunk_obj.get("choices") or []):
                            _dl = (_ch or {}).get("delta") or {}
                            for _k in ("content", "reasoning", "reasoning_content"):
                                _v = _dl.get(_k)
                                if isinstance(_v, str):
                                    _sse_chars += len(_v)
                    except Exception:
                        pass
                # 无 usage 时才估算。**不要用原始字节**：SSE 每帧的 `data: {...}` 框架
                # 约 230 字节，按 bytes//3 算出来的"token"里九成是 JSON 与换行——实测
                # 一个真实 64 token 的流式回复被记成 4,389 token（约 68 倍），配额被吃光。
                # 改为按**增量文本字符**折算（TOKENS_PER_CHAR 与速度分桶共用同一系数）。
                if total == 0:
                    if _sse_chars > 0:
                        total_completion = max(1, int(_sse_chars * TOKENS_PER_CHAR))
                    else:
                        total_completion = len(raw_resp) // 3   # 非 SSE 的流：退回旧估算
                    # 从请求估算 prompt（content 可能是 str，也可能是分段 list）
                    if original_req:
                        _pchars = 0
                        for _m in (original_req.get("messages") or []):
                            _c = _m.get("content") if isinstance(_m, dict) else None
                            if isinstance(_c, str):
                                _pchars += len(_c)
                            elif isinstance(_c, list):
                                _pchars += sum(len(_p.get("text", ""))
                                               for _p in _c if isinstance(_p, dict))
                        total_prompt = max(1, int(_pchars * TOKENS_PER_CHAR))
                    total = total_prompt + total_completion
            else:
                # 非流式，整个 JSON
                resp_obj = json.loads(raw_resp.decode("utf-8", errors="replace"))
                u = resp_obj.get("usage", {})
                total_prompt = u.get("prompt_tokens", 0)
                total_completion = u.get("completion_tokens", 0)
                total = u.get("total_tokens", total_prompt + total_completion)
                # 若 response 有 model，覆盖
                if resp_obj.get("model") and not model:
                    model = resp_obj["model"]
        except Exception:
            # 解析失败，粗略估算
            total_completion = len(raw_resp) // 3
            if original_req:
                msgs = original_req.get("messages", [])
                total_prompt = sum(len(m.get("content", "")) // 4 + 1 for m in msgs)
            total = total_prompt + total_completion

        # 记录用量
        model_name = model or (original_req or {}).get("model", "unknown")
        self._lane_ct = total_completion      # 供泳道归还时更新速度画像
        tracker.record(api_key, model_name, total_prompt, total_completion, total,
                       elapsed, client_ip, stream)

    # ---- 管理接口 ----
    def _handle_admin(self, method: str, path: str):
        global _cfg
        token = self.headers.get("X-Admin-Token", "")
        cfg = _cfg
        # admin token 为空时跳过校验（依赖 Tailscale 网络层鉴权）
        configured_token = cfg["admin"].get("token", "")
        if configured_token and token != configured_token:
            self._send_error(401, "admin_auth_failed", "管理接口需要正确的 X-Admin-Token")
            return

        # GET /api/admin/status
        if path == "/api/admin/status" and method == "GET":
            keys_summary = {}
            for k, kc in cfg["keys"].items():
                today = get_today()
                month = today[:7]
                with state_lock:
                    ud = tracker.daily[k].get(today, 0)
                    um = tracker.monthly[k].get(month, 0)
                    reqs = tracker.req_count[k].get(today, 0)
                keys_summary[k] = {
                    "name": kc.get("name", ""),
                    "active": kc.get("active", True),
                    "daily_used": ud,
                    "daily_limit": kc.get("daily_tokens", 0),
                    "monthly_used": um,
                    "monthly_limit": kc.get("monthly_tokens", 0),
                    "daily_requests": reqs,
                }
            ts_ip, ts_dns = get_tailscale_info()
            self._send_json({
                "status": "ok",
                "uptime": int(time.time() - _start_ts),
                "ollama": OLLAMA_HOST,
                "rapid_mlx_hosts": RAPID_MLX_HOSTS,
                "active_concurrency": active_concurrency,
                "max_concurrency": cfg["global"]["max_concurrency"],
                "listen": f"{cfg['global']['listen_host']}:{cfg['global']['listen_port']}",
                "keys": keys_summary,
                "log_file": str(USAGE_LOG),
                "log_size_mb": round(USAGE_LOG.stat().st_size / 1048576, 2) if USAGE_LOG.exists() else 0,
                "tailscale_ip": ts_ip,
                "tailscale_dns": ts_dns,
                "lane_pool": LANE_POOL.snapshot(),
                "system": collect_system_metrics(),
            })
            return

        # GET /api/admin/lanes — 泳道池实时视图（谁在跑、谁在等、各模型服务了几个上下文）
        if path == "/api/admin/lanes" and method == "GET":
            snap = LANE_POOL.snapshot()
            # auto 路径的等待不经过泳道池，而在串行闸门上 —— 这一块不带上就等于
            # "最忙的时候看不到到底谁在排队、排了多久"。
            snap["serial_gates"] = _serial_gates_snapshot()
            self._send_json(snap)
            return

        # GET /api/admin/usage?key=xxx&days=7
        if path == "/api/admin/usage" and method == "GET":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            days = int(qs.get("days", ["7"])[0])
            key_filter = qs.get("key", [None])[0]
            result = []
            # 从 JSONL 日志里聚合
            if USAGE_LOG.exists():
                cutoff = time.time() - days * 86400
                agg = defaultdict(lambda: {"total": 0, "prompt": 0, "completion": 0, "reqs": 0})
                with open(USAGE_LOG) as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                            if r.get("ts", 0) < cutoff:
                                continue
                            if key_filter and not r.get("api_key", "").startswith(key_filter[:8]):
                                continue
                            d = r.get("date")
                            agg[d]["total"] += r.get("total_tokens", 0)
                            agg[d]["prompt"] += r.get("prompt_tokens", 0)
                            agg[d]["completion"] += r.get("completion_tokens", 0)
                            agg[d]["reqs"] += 1
                        except Exception:
                            continue
                for d in sorted(agg.keys()):
                    row = {"date": d}
                    row.update(agg[d])
                    result.append(row)
            self._send_json({"days": days, "records": result})
            return

        # POST /api/admin/keys (创建或更新 key)
        if path == "/api/admin/keys" and method == "POST":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            try:
                data = json.loads(body)
            except Exception:
                self._send_error(400, "bad_request", "无效 JSON")
                return
            key = data.get("key")
            if not key:
                import secrets
                key = "sk-ollama-" + secrets.token_hex(24)
            cfg["keys"][key] = {
                "name": data.get("name", f"Key {key[:8]}.."),
                "daily_tokens": int(data.get("daily_tokens", 1_000_000)),
                "monthly_tokens": int(data.get("monthly_tokens", 30_000_000)),
                "qps": int(data.get("qps", DEFAULT_QPS)),
                "rate_limited": bool(data.get("rate_limited", True)),
                # 调度优先级：高的能插队（见 KEY_PRIORITY_MAX 那段）。缺省 0 = 老行为；
                # 存的是夹紧后的整数，避免配置里出现 "high"/9999 这类让调度器失智的值。
                "priority": _norm_prio(data.get("priority", 0)),
                "active": bool(data.get("active", True)),
                "created": time.strftime("%Y-%m-%d"),
            }
            save_config(cfg)
            _cfg = load_config()
            self._send_json({"message": "Key 已保存", "key": key, **cfg["keys"][key]})
            return

        # DELETE /api/admin/keys/{prefix} (按前缀删除)
        if path.startswith("/api/admin/keys/") and method == "DELETE":
            prefix = path.rsplit("/", 1)[-1]
            removed = []
            for k in list(cfg["keys"].keys()):
                if k == prefix or k.startswith(prefix):
                    del cfg["keys"][k]
                    removed.append(k)
            if removed:
                save_config(cfg)
                _cfg = load_config()
                self._send_json({"message": f"已删除 {len(removed)} 个 Key", "removed": removed})
            else:
                self._send_error(404, "key_not_found", "没有匹配的 Key")
            return

        # GET /api/admin/keys/list - 完整 Key 列表（含完整 Key 值，用于管理 UI）
        if path == "/api/admin/keys/list" and method == "GET":
            keys_list = []
            for k, kc in cfg["keys"].items():
                today = get_today()
                month = today[:7]
                with state_lock:
                    ud = tracker.daily[k].get(today, 0)
                    um = tracker.monthly[k].get(month, 0)
                    reqs = tracker.req_count[k].get(today, 0)
                keys_list.append({
                    "key": k,
                    "name": kc.get("name", ""),
                    "active": kc.get("active", True),
                    "daily_tokens": kc.get("daily_tokens", 0),
                    "monthly_tokens": kc.get("monthly_tokens", 0),
                    "qps": kc.get("qps", DEFAULT_QPS),
                    "rate_limited": kc.get("rate_limited", True),
                    "priority": _key_priority(kc),
                    "created": kc.get("created", ""),
                    "daily_used": ud,
                    "monthly_used": um,
                    "daily_requests": reqs,
                })
            self._send_json({"keys": keys_list})
            return

        # GET /api/admin/recent-logs?limit=50 - 最近 N 条请求日志
        if path == "/api/admin/recent-logs" and method == "GET":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            limit = int(qs.get("limit", ["50"])[0])
            logs = []
            if USAGE_LOG.exists():
                # 读取最后 limit 行
                try:
                    with open(USAGE_LOG, "rb") as f:
                        f.seek(0, 2)
                        size = f.tell()
                        block = 4096
                        data = b""
                        while size > 0 and data.count(b"\n") <= limit + 1:
                            step = min(block, size)
                            size -= step
                            f.seek(size)
                            data = f.read(step) + data
                        lines = data.decode("utf-8", errors="replace").strip().split("\n")[-limit:]
                        for line in reversed(lines):
                            if not line.strip():
                                continue
                            try:
                                logs.append(json.loads(line))
                            except Exception:
                                continue
                except Exception as e:
                    log(f"⚠️ 读取最近日志失败: {e}")
            self._send_json({"count": len(logs), "logs": logs})
            return

        # ===== 模型管理 =====

        # GET /api/admin/models - 列出所有已安装模型（合并 Ollama + 多 Rapid-MLX）
        if path == "/api/admin/models" and method == "GET":
            models = []
            seen_names = set()
            # Ollama 模型
            if OLLAMA_HOST:
                status, resp = _ollama_request("GET", "/api/tags")
                if status == 200:
                    try:
                        data = json.loads(resp)
                        for m in data.get("models", []):
                            name = m.get("name", "")
                            seen_names.add(name)
                            models.append({
                                "name": name,
                                "size": m.get("size", 0),
                                "size_gb": round(m.get("size", 0) / 1e9, 2),
                                "modified": m.get("modified_at", ""),
                                "digest": m.get("digest", "")[:12],
                                "details": m.get("details", {}),
                                "upstream": "ollama",
                            })
                    except Exception:
                        pass
            # Rapid-MLX 模型（遍历所有 host，短名优先，去掉长名别名）
            for host in RAPID_MLX_HOSTS:
                status2, resp2 = _rapid_mlx_request(host, "GET", "/v1/models")
                if status2 != 200:
                    continue
                try:
                    data2 = json.loads(resp2)
                    for m in data2.get("data", []):
                        mid = m.get("id", "")
                        if not mid:
                            continue
                        mid = _MODEL_REVERSE_ALIAS.get(mid, mid)
                        if mid in seen_names:
                            continue
                        seen_names.add(mid)
                        # 推断家族和量化
                        mid_lower = mid.lower()
                        family = "-"
                        if "qwen" in mid_lower: family = "qwen3.5"
                        elif "gpt-oss" in mid_lower: family = "gpt-oss"
                        elif "gemma" in mid_lower: family = "gemma4"
                        elif "deepseek" in mid_lower: family = "deepseek"
                        quantization = "-"
                        if "mxfp4" in mid_lower: quantization = "MXFP4"
                        elif "mxfp6" in mid_lower: quantization = "MXFP6"
                        elif "int4" in mid_lower or "q4" in mid_lower: quantization = "INT4"
                        elif "int8" in mid_lower or "q8" in mid_lower: quantization = "INT8"
                        # 参数规模（从名字推断）
                        parameter_size = "-"
                        import re as _re
                        mm = _re.search(r"(\d+b)", mid_lower)
                        if mm: parameter_size = mm.group(1).upper()
                        # 从 HF 缓存目录算文件大小（如果有）
                        size_gb = 0
                        try:
                            import subprocess as _sp
                            hf_name = mid  # 短名不行就跳过长名
                            # 找 HF cache 里对应的目录
                            result = _sp.run(
                                ["du", "-sb", f"{os.path.expanduser('~')}/.cache/huggingface/hub/models--*/blobs"],
                                capture_output=True, text=True, timeout=5
                            )
                        except Exception:
                            pass
                        # 直接算：根据模型 ID 映射已知大小
                        known_sizes = {
                            "qwen3.5-122b-mxfp4": 60.0,
                            "gpt-oss-120b-mxfp4-q4": 96.0,
                        }
                        size_gb = known_sizes.get(mid, 0)

                        models.append({
                            "name": mid,
                            "size": int(size_gb * 1e9),
                            "size_gb": size_gb,
                            "modified": m.get("created", ""),
                            "digest": "",
                            "details": {
                                "provider": "rapid-mlx",
                                "family": family,
                                "parameter_size": parameter_size,
                                "quantization_level": quantization,
                                "is_moe": m.get("is_moe"),
                                "is_hybrid": m.get("is_hybrid"),
                                "context_window": m.get("context_window"),
                                "tool_call_parser": m.get("tool_call_parser"),
                                "reasoning_parser": m.get("reasoning_parser"),
                                "capabilities": m.get("capabilities", []),
                            },
                            "upstream": "rapid-mlx",
                        })
                except Exception:
                    continue
            # 虚拟模型 auto：不属于任何上游，不占磁盘，大小/量化无意义。
            # 追加在末尾，仅用于管理台可见性，不参与拉取/删除。
            if AUTO_ENABLED:
                models.append({
                    "name": AUTO_MODEL_NAME,
                    "size": 0,
                    "size_gb": 0,
                    "modified": "",
                    "digest": "",
                    "details": {
                        "family": "虚拟模型（自动分配）",
                        "parameter_size": "—",
                        "quantization_level": "—",
                        "virtual": True,
                    },
                    "upstream": "gateway",
                })
            self._send_json({"models": models, "count": len(models)})
            return

        # POST /api/admin/models/pull - 拉取模型（后台流式）
        if path == "/api/admin/models/pull" and method == "POST":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            try:
                data = json.loads(body)
            except Exception:
                self._send_error(400, "bad_request", "无效 JSON")
                return
            model_name = data.get("name", "").strip()
            if not model_name:
                self._send_error(400, "bad_request", "缺少模型名称")
                return
            import secrets as _sec
            task_id = _sec.token_hex(8)
            with pull_lock:
                # 检查是否已有同名拉取任务在运行
                for tid, t in pull_tasks.items():
                    if t["name"] == model_name and t["status"] not in ("done", "failed", "cancelled"):
                        self._send_error(409, "already_running", f"模型 {model_name} 正在拉取中 (task: {tid})")
                        return
                pull_tasks[task_id] = {
                    "id": task_id,
                    "name": model_name,
                    "status": "starting",
                    "progress": 0,
                    "completed": 0,
                    "total": 0,
                    "error": "",
                    "started": time.time(),
                    "finished": 0,
                }
            th = threading.Thread(target=_pull_worker, args=(task_id, model_name), daemon=True)
            th.start()
            log(f"📥 开始拉取模型: {model_name} (task: {task_id})")
            self._send_json({"message": "拉取已开始", "task_id": task_id, "name": model_name})
            return

        # GET /api/admin/models/pull/status - 查询拉取任务状态
        if path == "/api/admin/models/pull/status" and method == "GET":
            with pull_lock:
                tasks = list(pull_tasks.values())
            # 清理超过 1 小时的已完成任务
            now = time.time()
            keep = []
            for t in tasks:
                if t["status"] in ("done", "failed", "cancelled") and t.get("finished", 0) and now - t["finished"] > 3600:
                    with pull_lock:
                        pull_tasks.pop(t["id"], None)
                    continue
                keep.append(t)
            self._send_json({"tasks": keep})
            return

        # DELETE /api/admin/models/pull/{task_id} - 取消拉取任务
        if path.startswith("/api/admin/models/pull/") and method == "DELETE":
            task_id = path.rsplit("/", 1)[-1]
            with pull_lock:
                if task_id in pull_tasks:
                    pull_tasks[task_id]["status"] = "cancelled"
                    pull_tasks[task_id]["finished"] = time.time()
                    self._send_json({"message": "已取消", "task_id": task_id})
                else:
                    self._send_error(404, "task_not_found", "任务不存在")
            return

        # DELETE /api/admin/models/{name} - 已禁用（防 OOM 误删）
        if path.startswith("/api/admin/models/") and method == "DELETE" and "/pull/" not in path:
            self._send_error(403, "disabled", "模型删除功能已禁用，避免误删导致 OOM")
            return

        self._send_error(404, "not_found", f"未知管理接口: {method} {path}")


# ---------- 服务器 ----------
class DualStackServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """支持 IPv4 + IPv6 双栈监听的服务器"""
    address_family = socket.AF_INET6  # 使用 IPv6 socket
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def server_bind(self):
        # 允许 IPv4 客户端通过 IPv4-mapped IPv6 地址连接
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, socket.error):
            pass
        super().server_bind()


class HTTPSServer(DualStackServer):
    """HTTPS 服务器：自动包装 SSL 上下文"""
    def __init__(self, server_address, RequestHandlerClass, certfile, keyfile):
        super().__init__(server_address, RequestHandlerClass)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
        self.socket = ctx.wrap_socket(self.socket, server_side=True)


class HTTPRedirectHandler(http.server.BaseHTTPRequestHandler):
    """HTTP 处理器：将所有请求 301 重定向到 HTTPS"""
    def do_GET(self):
        self._redirect()
    def do_POST(self):
        self._redirect()
    def do_PUT(self):
        self._redirect()
    def do_DELETE(self):
        self._redirect()
    def do_HEAD(self):
        self._redirect()

    def _redirect(self):
        host = self.headers.get("Host", "")
        # 去掉原端口，使用 HTTPS 端口
        if ":" in host:
            host = host.rsplit(":", 1)[0]
        new_url = f"https://{host}:{LISTEN_PORT}{self.path}"
        self.send_response(301)
        self.send_header("Location", new_url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # 静默重定向日志


# 兼容旧名称
ThreadedServer = DualStackServer


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


def print_banner():
    lan_ip = get_lan_ip()
    port = _cfg["global"]["listen_port"]
    host = _cfg["global"]["listen_host"]
    key_count = len(_cfg["keys"])
    use_https = _https_enabled()
    scheme = "https" if use_https else "http"
    print("")
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║           Ollama Token Gateway - Token 服务网关                 ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print("")
    if use_https:
        print(f"  🔒 HTTPS 主服务: {scheme}://localhost:{port}")
        print(f"  📡 局域网:      {scheme}://{lan_ip}:{port}")
        print(f"  ↩️  HTTP 重定向: http://localhost:{HTTP_REDIRECT_PORT} → {scheme}://...:{port}")
    else:
        print(f"  📡 本地访问:    http://localhost:{port}")
        print(f"  🌐 局域网:      http://{lan_ip}:{port}")
    if host == "0.0.0.0":
        print(f"  ☁️  外网映射:    {YOUR_DOMAIN_HINT}")
    print(f"  🎯 Ollama 上游: {OLLAMA_HOST}")
    print(f"  🔐 API Key 数:  {key_count}")
    print(f"  📊 日志文件:    {USAGE_LOG}")
    print(f"  ⚙️  配置文件:    {CONFIG_FILE}")
    # 把实际生效的超时/转发模式打出来：这些值来自 launchd 注入的 env，
    # 改 plist 后若只做 kickstart，launchd 会沿用**加载时**的旧值，
    # 打印出来才能一眼看出"配置改没生效"。
    # 横幅必须显示**实际生效值**而不是原始环境变量：写错（如 "600s"）时若照原样打印，
    # 会让人以为预算是 600s 而实际回退成了 900s，排查方向直接跑偏。
    _rt = _env_num("TG_READ_TIMEOUT", 900, minimum=1)
    _vt = _env_num("TG_VLM_READ_TIMEOUT", 120, minimum=1)
    _bn = "缓冲" if os.environ.get("TG_BUFFER_NONSTREAM", "1") not in ("0", "false", "no", "off") else "直通"
    print(f"  ⏱️  静默预算:    文本 {_rt}s / 带图 {_vt}s   非流式转发: {_bn}")
    _lat = MODEL_LATENCY.snapshot()
    print(f"  🚀 速度因子:    窗口内样本 {sum(sum(v) for v in _lat.values())} 条 / {len(_lat)} 个模型"
          f"（window={SPEED_WINDOW}, min={SPEED_MIN_SAMPLES}, 权重 {AUTO_WEIGHTS.get('speed', 0):.2f}, tau={SPEED_TAU:g}s）")
    if use_https:
        print(f"  📜 证书文件:    {CERT_FILE}")
    print("")
    print("  ── 可用 API Key（从 config.json 管理）──")
    for k, v in _cfg["keys"].items():
        mask = k[:12] + "..." + k[-4:] if len(k) > 16 else k
        status = "✅" if v.get("active", True) else "🚫"
        daily = f"{v.get('daily_tokens', 0) / 1e6:.1f}M/天" if v.get('daily_tokens', 0) else "无限制/天"
        monthly = f"{v.get('monthly_tokens', 0) / 1e6:.1f}M/月" if v.get('monthly_tokens', 0) else "无限制/月"
        print(f"  {status} {mask}  [{v.get('name','')}] {daily} {monthly}")
    print("")
    print("  ── 管理接口（X-Admin-Token）──")
    print(f"  GET  /api/admin/status     服务器状态 + 各 Key 用量")
    print(f"  GET  /api/admin/usage      历史用量统计 (days=7)")
    print(f"  POST /api/admin/keys       新增/更新 API Key")
    print(f"  DEL  /api/admin/keys/<pfx> 删除 Key")
    _at = _cfg['admin'].get('token', '')
    print(f"  Admin Token: {'已设置' if _at else '未设置（admin 接口开放，须置于受信任网络/Tailscale）'}")
    if not _at:
        print("  \u26a0\ufe0f  安全警告：admin.token 为空，/api/admin/* 管理接口无鉴权。")
        print("       仅当网关位于受信任内网 / Tailscale 网络层鉴权之后才安全；")
        print("       若暴露到不可信网络，请在 config.json 设置强 admin.token。")

    print("")
    print("  ── OpenAI 兼容端点 ──")
    print(f"  GET  /v1/models            列出模型")
    print(f"  POST /v1/chat/completions  聊天（支持 stream=true 流式）")
    print(f"  POST /v1/completions       完成")
    print(f"  GET  /health               健康检查")
    print("")
    print("=" * 68)
    print("  提示：编辑 config.json 修改 Key/额度，重启服务生效")
    print("       或使用 /api/admin/keys 接口动态管理（无需重启）")
    print("=" * 68)
    print("")


YOUR_DOMAIN_HINT = "路由器端口转发此机 IP 的 3000 端口 → 公网即可访问"
_start_ts = time.time()
_cfg = load_config()
auth_result_ok = None


def _lane_keepalive_loop():
    """给 Ollama 类泳道续命（间隔 TG_LANE_KEEPALIVE，0 关闭）。

    不这么做的话：Ollama 空闲 5 分钟就把模型卸了，下一次请求要重新读 13GB
    （gpt-oss:20b 实测冷加载 1.8s）。而 /v1/chat/completions 不认 keep_alive，
    所以只能走 /api/generate 的空 prompt —— 实测 0.28s、done_reason=load、不生成 token。
    """
    sleep_s = max(5.0, LANE_KEEPALIVE_S / 2.0)
    while True:
        time.sleep(sleep_s)
        try:
            for lane in LANE_POOL.keepalive_targets(time.time(), LANE_KEEPALIVE_S):
                host = (lane.get("host") or OLLAMA_HOST).rstrip("/")
                body = json.dumps({"model": lane["model"],
                                   "keep_alive": LANE_KEEPALIVE_TTL}).encode()
                req = urllib.request.Request(host + "/api/generate", data=body,
                                             headers={"Content-Type": "application/json"})
                t0 = time.time()
                ok = False
                try:
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        resp.read()
                    ok = True
                    log(f"💓 泳道保活 {lane['name']}（{lane['model']}，"
                        f"{time.time()-t0:.2f}s）")
                except Exception as e:      # noqa: BLE001
                    log(f"⚠️ 泳道保活失败 {lane['name']}（{lane['model']}）: "
                        f"{type(e).__name__}: {e}")
                finally:
                    LANE_POOL.note_ping(lane["name"], ok)
        except Exception as e:              # noqa: BLE001
            log(f"⚠️ 保活巡检异常: {type(e).__name__}: {e}")


def _model_refresh_loop():
    """定期刷新上游模型清单（间隔 TG_MODEL_REFRESH_INTERVAL，0 关闭）。

    解决两个只在长跑里才暴露的问题：
      ① 被删掉的模型长期留在候选池里 —— 请求驱动的 30s 缓存只有在"有请求进来"时
         才刷新，而刷新失败又保留旧值，于是删掉的模型可以一直赖着；
      ② 熔断时长只有 120s，坏掉的模型每 2 分钟就骗走一次上游往返。
         巡检把清单变化（新增/消失）交给 `_sync_model_registry` 对账：
         消失的清掉熔断记录，新出现的解除旧熔断，配合指数退避即可收敛。
    """
    while True:
        time.sleep(MODEL_REFRESH_INTERVAL_S)
        try:
            GatewayHandler._refresh_model_caches(force=True)
        except Exception as e:
            log(f"⚠️ 上游模型清单巡检异常: {type(e).__name__}: {e}")


# ========== Flash 运行时看门狗（实现抽到 tg_watchdog.py） ==========
# 为什么抽出去：这套逻辑的难点不在"探测 + 重启"，而在**不把自己弄死**——
# 冷加载、长 prefill、抖动重试各有一条反直觉的判断，正好是别人最想直接复用的部分。
# 埋在 4200 行网关里，想抄的人得先读 200 行无关代码。
#
# 单文件运行的兼容性：本模块可选。tg_watchdog.py 不在同目录时网关照常工作，
# 只是失去自愈能力——/health 会明确报 reason，不静默降级。
try:
    import tg_watchdog as wd
except Exception as _wd_import_err:  # noqa: BLE001
    wd = None
    _WD_IMPORT_ERR = f"{type(_wd_import_err).__name__}: {_wd_import_err}"


def _wd_gw_inflight() -> int:
    """把网关自己的在飞计数接给看门狗（决定阈值要不要翻倍）。"""
    with _model_inflight_lock:
        return sum(_model_inflight.values())


if wd is not None:
    wd.configure(config_dir=CONFIG_DIR, log_fn=log, inflight_fn=_wd_gw_inflight)
    WD_ENABLED = wd.WD_ENABLED
else:
    WD_ENABLED = False


def _wd_targets() -> List[str]:
    return wd.targets() if wd is not None else []


def _wd_snapshot() -> dict:
    """给 /health 用的巡检快照。"""
    if wd is None:
        return {"enabled": False,
                "reason": f"tg_watchdog.py 未加载（{_WD_IMPORT_ERR}），自愈能力不可用"}
    return wd.snapshot()


def _watchdog_loop(targets: List[str]) -> None:
    wd.loop(targets)


def main():
    # 速度因子预热：用最近的用量日志回填实测耗时，避免重启后 speed 因子失明
    _seeded = MODEL_LATENCY.seed_from_usage(USAGE_LOG)
    if _seeded:
        log(f"🚀 速度因子已预热 {_seeded} 条实测耗时（{len(MODEL_LATENCY.snapshot())} 个模型）")
    port = _cfg["global"]["listen_port"]
    host = _cfg["global"]["listen_host"]
    # 双栈监听：IPv4 的 0.0.0.0 转成 IPv6 的 :: （配合 IPV6_V6ONLY=0 可同时收 IPv4/IPv6）
    if host in ("0.0.0.0", "::", "::0"):
        bind_host = "::"
    else:
        bind_host = host

    # 检查证书是否存在
    use_https = _https_enabled()

    if use_https:
        # HTTPS 服务器（主端口）
        try:
            server = HTTPSServer((bind_host, port), GatewayHandler, str(CERT_FILE), str(KEY_FILE))
        except OSError as e:
            print(f"❌ HTTPS 端口 {port} 被占用: {e}")
            sys.exit(1)

        # HTTP 重定向服务器（辅助端口）
        try:
            redirect_server = DualStackServer((bind_host, HTTP_REDIRECT_PORT), HTTPRedirectHandler)
            redirect_thread = threading.Thread(target=redirect_server.serve_forever, daemon=True)
            redirect_thread.start()
        except OSError as e:
            print(f"⚠️  HTTP 重定向端口 {HTTP_REDIRECT_PORT} 启动失败（不影响 HTTPS）: {e}")
            redirect_server = None
    else:
        try:
            server = ThreadedServer((bind_host, port), GatewayHandler)
        except OSError as e:
            print(f"❌ 端口 {port} 被占用: {e}")
            print("   修改 TG_PORT 环境变量或 config.json 换一个端口")
            sys.exit(1)

    print_banner()
    # 并发槽位回收巡检：把"客户端已经走了但上游还在跑"的槽位及时收回，
    # 避免 active_concurrency 被卡死在 max_concurrency（全局 429 假死）
    threading.Thread(target=_reaper_loop, daemon=True).start()
    print(f"♻️  槽位回收巡检已启动（每 {REAPER_INTERVAL_S}s 一次）")
    # 上游模型清单巡检：删掉的模型要及时消失，坏掉的模型不要每 2 分钟骗一次往返
    if MODEL_REFRESH_INTERVAL_S > 0:
        threading.Thread(target=_model_refresh_loop, daemon=True).start()
        print(f"🔍 上游模型清单巡检已启动（每 {MODEL_REFRESH_INTERVAL_S}s 一次）")
    # 泳道保活：Ollama 类上游空闲 5 分钟就卸载模型，而它的 OpenAI 兼容接口不认
    # keep_alive，只能由网关定时拍 /api/generate 续命（MLX 侧靠 pin，自动跳过）
    if LANE_POOL.enabled and LANE_KEEPALIVE_S > 0:
        threading.Thread(target=_lane_keepalive_loop, daemon=True,
                         name="lane-keepalive").start()
        print(f"💓 泳道保活已启动（每 {LANE_KEEPALIVE_S}s 一次，keep_alive={LANE_KEEPALIVE_TTL}）")
    # Flash 运行时看门狗：上游是我们自己拉起的进程，崩了没有别的托管方管；
    # 而"有没有请求在飞"只有网关知道，所以巡检挂在这里（实现见 tg_watchdog.py）
    if WD_ENABLED:
        _wd_targets_resolved = _wd_targets()
        if _wd_targets_resolved:
            threading.Thread(target=_watchdog_loop, args=(_wd_targets_resolved,),
                             daemon=True, name="flash-watchdog").start()
        else:
            print("🐕 看门狗已启用但没有巡检目标（TG_WD_TARGETS 为空且无 flash-runtime.json），跳过")
    elif wd is None:
        print(f"⚠️ 看门狗不可用：tg_watchdog.py 未加载（{_WD_IMPORT_ERR}），上游崩了不会自动恢复")
    else:
        print("🐕 看门狗已关闭（TG_WD_ENABLED=0）")
    # 启动即拉一次清单：别等第一个请求来才发现模型已经变了
    try:
        GatewayHandler._refresh_model_caches(force=True)
    except Exception as e:
        log(f"⚠️ 启动时刷新模型清单失败: {type(e).__name__}: {e}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 收到停止信号，退出中...")
        server.shutdown()


if __name__ == "__main__":
    main()
