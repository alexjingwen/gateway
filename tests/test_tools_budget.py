#!/usr/bin/env python3
"""回归：工具调用请求的输出预算下限（修 "Empty stream ... chunks=1"）。

背景（2026-11-07 线上）：
  IDE 客户端固定 max_tokens=2048，122B 同时在 RAPID_MLX_HOSTS 与 RAPID_MLX_VLM_HOSTS
  里，于是命中了「VLM 上游不强制 max_tokens」的豁免。工具调用是整块下发的：
  预算耗尽时上游只返回一个 finish_reason=length 的半截 tool_call（chunks=1），
  客户端解析不出完整调用 → "Empty stream: ... before emitting any model token"。

期望（按请求类型而非按主机豁免）：
  纯文本 + tools  → 抬到 TG_MIN_MAX_TOKENS_TOOLS（默认 8192）
  纯文本 + 无 tools → 保持客户端原值
  带图（多模态）    → 保持客户端原值（视觉调用要短输出、低延迟）
  已高于下限        → 不动

用法：
  python3 tests/test_tools_budget.py
环境变量：
  TG_SRC  被测网关文件，默认仓库根的 token-gateway.py
  TG_PY   启动网关用的解释器，默认当前解释器
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

urllib.request.getproxies = lambda: {}

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.environ.get("TG_SRC") or os.path.join(ROOT, "token-gateway.py")
PY = os.environ.get("TG_PY") or sys.executable
KEY = "sk-ollama-TEST-TOOLBUDGET"
MODEL = "test/tool-model"
TOOLS_FLOOR = 8192
RESULTS = []
SEEN = []          # stub 收到的 max_tokens


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": MODEL}], "models": [{"name": MODEL}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n)
        try:
            SEEN.append(json.loads(raw.decode() or "{}").get("max_tokens"))
        except Exception:
            SEEN.append(None)
        body = json.dumps({
            "id": "chatcmpl-stub", "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def mk_cfg(port):
    d = tempfile.mkdtemp(prefix="gw-toolbudget-")
    os.makedirs(d + "/logs", exist_ok=True)
    cfg = {
        "keys": {KEY: {"name": "t", "daily_tokens": 0, "monthly_tokens": 0,
                       "qps": 100, "rate_limited": False, "active": True,
                       "created": "2026-11-07"}},
        "global": {"max_concurrency": 8, "allow_models": ["*"], "deny_models": [],
                   "listen_host": "127.0.0.1", "listen_port": port},
        "admin": {"token": ""},
    }
    json.dump(cfg, open(d + "/config.json", "w", encoding="utf-8"), ensure_ascii=False)
    return d


def start_gw(port, stub_port):
    d = mk_cfg(port)
    env = dict(os.environ)
    env.update({
        "TG_DISABLE_HTTPS": "1", "NO_PROXY": "*",
        "TG_CONFIG_DIR": d, "TG_LOG_DIR": d + "/logs",
        "TG_RAPID_MLX_HOSTS": f"http://127.0.0.1:{stub_port}",
        "TG_RAPID_MLX_VLM_HOSTS": f"http://127.0.0.1:{stub_port}",
    })
    logf = open(d + "/srv.log", "wb")
    p = subprocess.Popen([PY, "-u", SRC], env=env, stdout=logf, stderr=subprocess.STDOUT)
    for _ in range(80):
        time.sleep(0.25)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return p, d
        except OSError:
            if p.poll() is not None:
                break
    raise RuntimeError("网关未启动:\n" + open(d + "/srv.log", errors="replace").read()[-800:])


def chat(port, msgs, extra=None):
    body = {"model": MODEL, "messages": msgs, "stream": False}
    if extra:
        body.update(extra)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


TEXT = [{"role": "user", "content": "读取 /etc/hosts 并总结。"}]
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]
IMAGE = [{"role": "user", "content": [
    {"type": "text", "text": "这张图里是什么？"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
]}]


def main():
    if not os.path.exists(SRC):
        print(f"找不到被测文件: {SRC}")
        return 2

    stub_port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    port = free_port()
    p, d = start_gw(port, stub_port)
    try:
        print(f"== 被测: {SRC}  |  网关 :{port}  stub :{stub_port} ==")

        print("\n-- 1. 纯文本 + tools + max_tokens=2048 → 抬到下限 --")
        SEEN.clear()
        st, _ = chat(port, TEXT, {"max_tokens": 2048, "tools": TOOLS})
        check("状态 200", st == 200, str(st))
        check(f"上游收到 max_tokens={TOOLS_FLOOR}", SEEN and SEEN[-1] == TOOLS_FLOOR, str(SEEN))
        log = open(d + "/srv.log", errors="replace").read()
        check("日志说明原因（工具调用预算）", "工具调用预算" in log,
              [l.strip() for l in log.splitlines() if "提升 max_tokens" in l][:1])

        print("\n-- 2. 纯文本无 tools + max_tokens=2048 → 不动 --")
        SEEN.clear()
        st, _ = chat(port, TEXT, {"max_tokens": 2048})
        check("状态 200", st == 200, str(st))
        check("上游仍收到 2048", SEEN and SEEN[-1] == 2048, str(SEEN))

        print("\n-- 3. 带图 + tools + max_tokens=2048 → 维持豁免 --")
        SEEN.clear()
        st, _ = chat(port, IMAGE, {"max_tokens": 2048, "tools": TOOLS})
        check("状态 200", st == 200, str(st))
        check("上游仍收到 2048", SEEN and SEEN[-1] == 2048, str(SEEN))

        print("\n-- 4. 纯文本 + tools + max_tokens=16384 → 已高于下限，不动 --")
        SEEN.clear()
        st, _ = chat(port, TEXT, {"max_tokens": 16384, "tools": TOOLS})
        check("状态 200", st == 200, str(st))
        check("上游仍收到 16384", SEEN and SEEN[-1] == 16384, str(SEEN))

    finally:
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
        srv.shutdown()
        shutil.rmtree(d, ignore_errors=True)

    print("\n== 汇总 ==")
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
