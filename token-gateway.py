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
import socketserver
import urllib.request
import urllib.error
import json
import os
import itertools
import sys
import time
import threading
import socket
import select
import ssl
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

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


LISTEN_HOST = os.environ.get("TG_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("TG_PORT", "3000"))

# HTTPS 配置
CERT_DIR = CONFIG_DIR / "certs"
CERT_FILE = CERT_DIR / "server.crt"
KEY_FILE = CERT_DIR / "server.key"
HTTP_REDIRECT_PORT = int(os.environ.get("TG_HTTP_PORT", "3001"))  # HTTP 端口（重定向到 HTTPS）
# 强制禁用 HTTPS（设为任意非空值即禁用，回退到 HTTP 单端口，方便不支持自签证书的客户端）
DISABLE_HTTPS = bool(os.environ.get("TG_DISABLE_HTTPS"))


def _https_enabled() -> bool:
    """是否启用 HTTPS（受环境变量 TG_DISABLE_HTTPS 控制）"""
    return (not DISABLE_HTTPS) and CERT_FILE.exists() and KEY_FILE.exists()

# 限流配置
DEFAULT_QPS = int(os.environ.get("TG_QPS", "5"))     # 每个 Key 每秒请求数
DEFAULT_MAX_CONCURRENCY = int(os.environ.get("TG_CONCURRENCY", "10"))  # 全局最大并发

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
const srcPill=isMLX
  ?`<span class="pill" style="background:rgba(34,197,94,0.15);color:#22c55e;border:1px solid rgba(34,197,94,0.3)">🟢 MLX</span>`
  :`<span class="pill" style="background:rgba(148,163,184,0.15);color:#94a3b8;border:1px solid rgba(148,163,184,0.25)">⚪ Ollama</span>`;
return `<tr>
<td style="font-family:'SF Mono',monospace;color:var(--accent);font-size:12px">${m.name} ${srcPill}</td>
<td>${m.size_gb} GB</td>
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
        "sk-ollama-b254b42b799eac68d1026b97e21da64c92d57c9edb4eb13e": {
            "name": "默认主Key",
            "daily_tokens": 1_000_000,     # 每日 100万 tokens
            "monthly_tokens": 30_000_000,   # 每月 3000万 tokens
            "qps": DEFAULT_QPS,
            "rate_limited": True,
            "active": True,
            "created": time.strftime("%Y-%m-%d"),
        }
    },
    "global": {
        "max_concurrency": DEFAULT_MAX_CONCURRENCY,
        "allow_models": ["*"],          # 允许的模型，*=全部
        "deny_models": [],
        "listen_host": LISTEN_HOST,
        "listen_port": LISTEN_PORT,
    },
    "admin": {
        "token": "admin-change-me-please",  # /api/admin/* 管理接口密码
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
    _models_cache_lock = threading.Lock()

    def _fetch_ollama_models(self) -> set:
        try:
            r = urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3)
            if r.status == 200:
                data = json.loads(r.read())
                return {m["name"] for m in data.get("models", [])}
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

    def _ollama_model_exists(self, model: str) -> bool:
        """检查模型是否在任一上游存在。"""
        if not model:
            return True
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
        all_models = self._ollama_models_cache["models"] | set(self._rapid_mlx_models_cache["models"].keys())
        if all_models:
            return model in all_models
        return True

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

        # 模型白名单
        if model and not self._check_model_allowed(model, cfg):
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
        try:
            self._proxy("POST", client_ip, api_key, key_cfg, cfg,
                        body_bytes=body_bytes, original_req=req, model=model)
        except Exception as e:
            # 兜底：所有未预期异常都不应让并发计数泄漏
            log(f"❌ do_POST 未捕获异常: {type(e).__name__}: {e}")
            try:
                self._send_error(500, "internal_error", "服务器内部错误")
            except Exception:
                pass
        finally:
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

        # ========= Context Window 自动截断降级 =========
        if upstream_host in RAPID_MLX_HOSTS and original_req and self.path.startswith("/v1/chat"):
            _cw = _MODEL_CONTEXT_WINDOWS.get(model, _MODEL_CONTEXT_WINDOWS.get("*", 262144))
            _msgs = original_req.get("messages", [])
            _max_tokens = original_req.get("max_tokens", 4096)
            # 强制最小 max_tokens，防止 dsh 等客户端发送过小的 max_tokens 导致回复被截断。
            # 可用 TG_MIN_MAX_TOKENS 覆盖；**VLM 上游（多模态）一律不强制** ——
            # 视觉调用要的是"短结构化输出 + 低延迟"，把 200 抬到 32768 会让模型长篇胡写，
            # 单次请求从 ~30s 涨到 10 分钟，期间一直占着 global 并发额度。
            # 实测：10 个槽位被这类挂死请求占满 → 所有客户端（含挂机 bot）收到瞬时 429。
            _MIN_MAX_TOKENS = int(os.environ.get("TG_MIN_MAX_TOKENS", "32768"))
            if upstream_host in RAPID_MLX_VLM_HOSTS:
                _MIN_MAX_TOKENS = 0
            if _MIN_MAX_TOKENS and _max_tokens < _MIN_MAX_TOKENS:
                log(f"⬆️  提升 max_tokens: {_max_tokens} → {_MIN_MAX_TOKENS} (强制最小值) → {client_ip}")
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
                original_req["max_tokens"] = int(_hard_max)
                _truncated = True
                log(f"✂️  max_tokens 硬限制: {_max_tokens} → {original_req['max_tokens']} (context={_cw}, 硬上限={_hard_max}) → {client_ip}")
                _max_tokens = original_req["max_tokens"]

            _total_est = _prompt_tokens + _max_tokens
            if _total_est > _cw - _safety:
                _room = _cw - _prompt_tokens - _safety
                if _room > 1024:
                    if _max_tokens > _room:
                        original_req["max_tokens"] = max(512, int(_room))
                        _truncated = True
                        log(f"✂️  自动降 max_tokens: {_max_tokens} → {original_req['max_tokens']} (prompt~{int(_prompt_tokens)}, context={_cw}) → {client_ip}")
                else:
                    while len(_msgs) > 2 and _prompt_tokens + _safety > _cw:
                        _rm = _msgs.pop(1)
                        _prompt_tokens -= _est_msg_tokens(_rm)
                        _truncated = True
                    if _prompt_tokens + _max_tokens > _cw - _safety:
                        original_req["max_tokens"] = max(256, int(_cw - _prompt_tokens - _safety))
                    if _truncated:
                        log(f"✂️  截短历史消息 + 降 max_tokens → prompt~{int(_prompt_tokens)}, max={original_req['max_tokens']} → {client_ip}")
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

        # 分级超时：连接超时短（快速失败），整体超时可配（大模型加载慢）。
        # read_timeout 是"一个并发槽位最多被占多久"的上界：客户端提前断开时，网关线程
        # 仍会一直阻塞读到这个超时才会走 finally 释放额度，所以它是全局 429 的兜底闸门。
        # **多模态（带图）请求单独用更短的 TG_VLM_READ_TIMEOUT**：视觉决策是"看一眼答一小段
        # JSON"，正常 10~30s 就回；而挂机客户端有自己的步进预算，超时就走人 —— 留一个
        # 900s 的槽位给它，等于把全局并发额度和一个已经没人要的请求绑死。文本客户端
        # （dsh 等）仍用 TG_READ_TIMEOUT，行为不变。
        connect_timeout = int(os.environ.get("TG_CONNECT_TIMEOUT", "10"))
        read_timeout = int(os.environ.get("TG_READ_TIMEOUT", "900"))
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
            read_timeout = int(os.environ.get("TG_VLM_READ_TIMEOUT", "120"))

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
                log(f"❌ Ollama 连接失败 ({elapsed:.1f}s): {last_exc}")
                try:
                    self._send_error(502, "bad_gateway",
                                     f"无法连接 Ollama ({OLLAMA_HOST}): {str(last_exc)}")
                except self._CLIENT_GONE_ERRORS:
                    pass
            return

        # 成功连接，处理响应
        client_gone = False
        response_data = bytearray()
        try:
            with resp:
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

                self.end_headers()

                # 读取响应（同时处理流式和非流式）
                while True:
                    try:
                        chunk = resp.read(8192)
                    except (TimeoutError, ConnectionError, OSError) as e:
                        log(f"⚠️ 读取上游响应中断: {e}")
                        break
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except self._CLIENT_GONE_ERRORS:
                        # 客户端已断开，停止写入但仍记录已收到的用量
                        client_gone = True
                        break
                    response_data.extend(chunk)

        except self._CLIENT_GONE_ERRORS:
            client_gone = True
        except Exception as e:
            log(f"⚠️ 转发过程异常: {type(e).__name__}: {e}")

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
    print(f"  Admin Token: {_cfg['admin']['token']}")
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
