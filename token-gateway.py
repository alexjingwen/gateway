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
import struct
from pathlib import Path
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple


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
    "qwen3.5-122b=mlx-community/Qwen3.5-122B-A10B-4bit"
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

# ==========================

# 全局运行时状态
state_lock = threading.Lock()
key_qps_bucket: Dict[str, List[float]] = defaultdict(list)  # 每个 key 的请求时间戳
active_concurrency = 0

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
    "nightmedia/Qwen3.5-122B-A10B-Text-mxfp4-mlx": 262144,
    "qwen3.5-122b-mxfp4": 262144,
    "mlx-community/Qwen3.5-122B-A10B-4bit": 262144,
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
<thead><tr><th>Key</th><th>名称</th><th>状态</th><th>今日用量</th><th>本月用量</th><th>QPS</th><th>创建</th><th>操作</th></tr></thead>
<tbody id="keysBody"><tr><td colspan="8" class="empty">加载中...</td></tr></tbody>
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
load7dChart();
}catch(e){console.error(e);}
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
if(!keys.length){document.getElementById('keysBody').innerHTML='<tr><td colspan="8" class="empty">暂无 Key，点击右上角新建</td></tr>';return;}
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
<td style="font-size:11px;color:var(--dim)">${k.created||'-'}</td>
<td><button class="icon-btn" onclick="editKey('${k.key}')">编辑</button> <button class="icon-btn danger" onclick="delKey('${k.key}','${k.name}')">删除</button></td>
</tr>`;}).join('');
}catch(e){console.error(e);}
}

// Key 弹窗
let editingKey=null;
function openKeyModal(){editingKey=null;document.getElementById('keyModalTitle').textContent='新建 API Key';document.getElementById('kName').value='';document.getElementById('kDaily').value=1000000;document.getElementById('kMonthly').value=30000000;document.getElementById('kQps').value=5;document.getElementById('kLimited').value='1';document.getElementById('kActive').value='1';document.getElementById('keyModal').classList.add('show');}
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
document.getElementById('kLimited').value=k.rate_limited?'1':'0';
document.getElementById('kActive').value=k.active?'1':'0';
document.getElementById('keyModal').classList.add('show');
}catch(e){console.error(e);}
}
function closeKeyModal(){document.getElementById('keyModal').classList.remove('show');}
async function saveKey(){
const data={name:document.getElementById('kName').value.trim()||'未命名',daily_tokens:parseInt(document.getElementById('kDaily').value)||0,monthly_tokens:parseInt(document.getElementById('kMonthly').value)||0,qps:parseInt(document.getElementById('kQps').value)||5,rate_limited:document.getElementById('kLimited').value==='1',active:document.getElementById('kActive').value==='1'};
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
                         "X-Token-Usage,X-Quota-Remaining,X-RateLimit-Limit")

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

    def _fetch_ollama_models(self) -> set:
        """拉取 ollama 模型名集合；顺带缓存 details（参数量/量化）供 auto 质量排序使用。"""
        try:
            r = urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3)
            if r.status == 200:
                data = json.loads(r.read())
                models = set()
                meta = {}
                for m in data.get("models", []):
                    name = m.get("name")
                    if not name:
                        continue
                    models.add(name)
                    det = m.get("details") or {}
                    meta[name] = {
                        "param": det.get("parameter_size") or "",
                        "quant": det.get("quantization_level") or "",
                        "family": det.get("family") or "",
                        "size_gb": round((m.get("size") or 0) / 1e9, 2),
                    }
                if meta:
                    self._ollama_meta_cache["meta"] = meta
                    self._ollama_meta_cache["ts"] = time.time()
                return models
        except Exception:
            pass
        return set()

    def _fetch_rapid_mlx_models(self) -> dict:
        result = {}
        for host in RAPID_MLX_HOSTS:
            try:
                _headers = {}
                if host in RAPID_MLX_VLM_HOSTS:
                    _headers["Authorization"] = f"Bearer {MLX_VLM_API_KEY}"
                _req = urllib.request.Request(f"{host}/v1/models", headers=_headers)
                r = urllib.request.urlopen(_req, timeout=3)
                if r.status == 200:
                    data = json.loads(r.read())
                    for m in data.get("data", []):
                        mid = m["id"]
                        if "/" not in mid or "/" not in result:
                            result[mid] = host
            except Exception:
                continue
        return result

    def _refresh_model_caches(self) -> Tuple[set, dict]:
        """刷新并返回 (ollama 模型集合, {rapid-mlx 模型: host})。

        30 秒缓存；上游整体不可达时保留上一次的非空结果，避免候选池被瞬时清空。
        """
        with self._models_cache_lock:
            now = time.time()
            if now - self._ollama_models_cache["ts"] > 30:
                models = self._fetch_ollama_models()
                if models or not self._ollama_models_cache["models"]:
                    self._ollama_models_cache["models"] = models
                self._ollama_models_cache["ts"] = now
            if now - self._rapid_mlx_models_cache["ts"] > 30:
                models = self._fetch_rapid_mlx_models()
                if models or not self._rapid_mlx_models_cache["models"]:
                    self._rapid_mlx_models_cache["models"] = models
                self._rapid_mlx_models_cache["ts"] = now
            return (set(self._ollama_models_cache["models"]),
                    dict(self._rapid_mlx_models_cache["models"]))

    def _ollama_model_exists(self, model: str) -> bool:
        """检查模型是否在任一上游存在。"""
        if not model:
            return True
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
        """模型参数量（B）：优先用上游元数据，其次从名字推断。"""
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
        for m, h in pool:
            if m in deny or m in AUTO_EXCLUDE or m in auto_excl:
                continue
            if not wide_allow and m not in allow:
                continue
            allowed.append((m, h))

        if path.endswith("/embeddings"):
            embed = [(m, h) for m, h in allowed if self._auto_is_embed(m)]
            if embed:
                return embed, "嵌入端点，仅限嵌入模型"
            return [], "嵌入端点但候选池内没有嵌入模型"

        chat = [(m, h) for m, h in allowed if not self._auto_is_embed(m)]
        if self._auto_req_has_image(req):
            vlm = [(m, h) for m, h in chat if self._auto_is_vlm(m, h)]
            if vlm:
                return vlm, "带图请求，优先多模态模型"
            return chat, "带图请求但无多模态候选，回退文本模型（图片将被过滤）"
        return chat, "文本请求"

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
            self._send_json({"status": "ok", "version": "1.0.0", "uptime": int(time.time() - _start_ts)})
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
                all_models = []
                seen = set()
                try:
                    r = urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3)
                    if r.status == 200:
                        for m in json.loads(r.read()).get("models", []):
                            mid = m["name"]
                            if mid not in seen:
                                seen.add(mid)
                                all_models.append({"id": mid, "object": "model", "created": 0, "owned_by": "ollama"})
                except Exception:
                    pass
                for host in RAPID_MLX_HOSTS:
                    try:
                        _h = {}
                        if host in RAPID_MLX_VLM_HOSTS:
                            _h["Authorization"] = f"Bearer {MLX_VLM_API_KEY}"
                        _req = urllib.request.Request(f"{host}/v1/models", headers=_h)
                        r = urllib.request.urlopen(_req, timeout=3)
                        if r.status == 200:
                            for m in json.loads(r.read()).get("data", []):
                                mid = m.get("id", "")
                                if not mid or mid in seen:
                                    continue
                                mid = _MODEL_REVERSE_ALIAS.get(mid, mid)
                                if mid in seen:
                                    continue
                                seen.add(mid)
                                all_models.append({"id": mid, "object": "model", "created": 0, "owned_by": "rapid-mlx"})
                    except Exception:
                        continue
                # auto 虚拟模型放在列表末尾：避免改变"客户端默认取第一个模型"的既有行为
                if AUTO_ENABLED and AUTO_MODEL_NAME not in seen:
                    all_models.append({"id": AUTO_MODEL_NAME, "object": "model",
                                       "created": 0, "owned_by": "gateway"})
                    seen.add(AUTO_MODEL_NAME)
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
                ollama_models = []
                vlm_models = []
                try:
                    r = urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3)
                    if r.status == 200:
                        ollama_models = json.loads(r.read()).get("models", [])
                except Exception:
                    pass
                seen = {m.get("name") for m in ollama_models}
                for host in RAPID_MLX_HOSTS:
                    try:
                        _h = {}
                        if host in RAPID_MLX_VLM_HOSTS:
                            _h["Authorization"] = f"Bearer {MLX_VLM_API_KEY}"
                        _req = urllib.request.Request(f"{host}/v1/models", headers=_h)
                        r = urllib.request.urlopen(_req, timeout=3)
                        if r.status == 200:
                            for m in json.loads(r.read()).get("data", []):
                                mid = m.get("id", "")
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
                    except Exception:
                        continue
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
            with state_lock:
                if active_concurrency >= cfg["global"]["max_concurrency"]:
                    self._send_error(429, "too_many_requests", "服务器忙")
                    return
                active_concurrency += 1
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
                with state_lock:
                    _e = _active_reqs.pop(_rid, None)
                    if _e is None or not _e.get("freed"):
                        active_concurrency = max(0, active_concurrency - 1)
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
        if model and not self._auto_resolved and not self._check_model_allowed(model, cfg):
            self._send_error(403, "model_not_allowed", f"模型 '{model}' 不在白名单中")
            return

        # 模型存在性预检查（避免 Ollama 返回不明确的 404）
        if model and not self._ollama_model_exists(model):
            available = ", ".join(sorted(self._ollama_models_cache["models"])) or "无"
            self._send_error(404, "model_not_found",
                             f"模型 '{model}' 不存在。可用模型: {available}")
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

        # 并发检查
        with state_lock:
            if active_concurrency >= cfg["global"]["max_concurrency"]:
                self._send_error(429, "too_many_requests", "服务器忙，请稍后再试")
                return
            active_concurrency += 1
            _rid = next(_req_seq)
            _active_reqs[_rid] = {"ts": time.time(), "sock": self.connection, "freed": False}

        client_ip = self._real_ip()
        # 记录该模型在飞请求数：auto 的 least-busy 策略据此判断哪个模型繁忙
        _inflight_model = model
        model_inflight_inc(_inflight_model)
        self._inflight_model = _inflight_model
        self._inflight_released = False
        try:
            self._proxy("POST", client_ip, api_key, key_cfg, cfg,
                        body_bytes=body_bytes, original_req=req, model=model)
        except Exception as e:
            # 兜底：所有未预期异常都不应让并发计数泄漏
            # 记录完整堆栈：仅打 type+message 时（如 KeyError: 'max_tokens'）无法定位到行号，
            # 曾因此多花一轮排查。堆栈只写本地日志，不返回给客户端。
            log(f"❌ do_POST 未捕获异常: {type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}")
            try:
                self._send_error(500, "internal_error", "服务器内部错误")
            except Exception:
                pass
        finally:
            # _proxy 通常已经在上游读完时释放过；这里只兜底真正漏掉的情况
            if not getattr(self, "_inflight_released", False):
                model_inflight_dec(_inflight_model)
            # 释放槽位；如果 reaper 已经因为"客户端走了"提前回收过，这里就不要再减
            with state_lock:
                _e = _active_reqs.pop(_rid, None)
                if _e is None or not _e.get("freed"):
                    active_concurrency = max(0, active_concurrency - 1)

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

    def _proxy(self, method: str, client_ip: str, api_key: str, key_cfg: dict, cfg: dict,
               body_bytes: bytes = b"", original_req: dict = None, model: str = ""):

        upstream_host = self._resolve_upstream(model, self.path)
        url = f"{upstream_host}{self.path}"
        stream = bool(original_req and original_req.get("stream", False)) if original_req else False

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

        # ========= 输出预算下限（Ollama 等非 Rapid-MLX 上游） =========
        # 下方"Context Window 自动截断降级"整块嵌在 `upstream_host in RAPID_MLX_HOSTS`
        # 里，所以 Ollama 侧原本**完全没有** max_tokens 下限：gemma4 这类会"思考"的模型
        # 先把预算烧在思维链上，预算耗尽时 content 为空、finish_reason=length，
        # 客户端只看到一条空回复。这不是理论风险，已实测复现：
        #   max_tokens=64  → content 0 字符 / reasoning 233 字符 / completion_tokens=64
        #   max_tokens=192 → 同样为空；4096 时 reasoning 可到 1960 字符才出正文
        # 且 gemma4 是否触发思维链是概率性的（同一句提示另一次直接 34 token 出结果），
        # 所以下限只能降低概率，真正兜底的是后面的"推理模型空正文修复"。
        # 注意：Ollama 的 OpenAI 兼容端点会**忽略** think / reasoning_effort（已实测），
        # 无法从根上关掉思维链，只能给预算。
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

        # 上游重试：对连接类错误重试 1 次（Ollama 偶发抖动）
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, data=body_bytes or None, method=method, headers=upstream_headers)
                resp = urllib.request.urlopen(req, timeout=read_timeout)
                break
            except urllib.error.HTTPError as e:
                # HTTP 错误（如 404/500）不重试，直接处理
                last_exc = e
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

        # 处理连接失败（重试后仍失败）
        if resp is None:
            elapsed = time.time() - t0
            if isinstance(last_exc, urllib.error.HTTPError):
                e = last_exc
                err_body = b""
                try:
                    err_body = e.read() or b""
                except Exception:
                    pass
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
            # auto 命中时回传实际选中的模型，便于客户端确认与问题定位
            _auto_hit = getattr(self, "_auto_resolved", None)
            if _auto_hit:
                self.send_header("X-Gateway-Model", _auto_hit)
                _tier = getattr(self, "_auto_tier_label", "")
                if _tier:
                    self.send_header("X-Gateway-Auto-Tier", _tier)
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
                    except Exception:
                        pass
                # 如果流式没有 usage，估算一下（简单方法）
                if total == 0:
                    total_completion = len(raw_resp.decode("utf-8", errors="replace")) // 3
                    # 从请求估算 prompt
                    if original_req:
                        msgs = original_req.get("messages", [])
                        total_prompt = sum(len(m.get("content", "")) // 4 + 1 for m in msgs)
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
            })
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 收到停止信号，退出中...")
        server.shutdown()


if __name__ == "__main__":
    main()
