#!/usr/bin/env python3
"""回归：上游一律直连，不允许被 HTTP_PROXY 劫持。

背景（2026-11-08 现网故障）：
  网关从 IDE 的沙箱 shell 里被手工拉起时，`env` 里带着
  `HTTP_PROXY=http://127.0.0.1:65388`（沙箱给子进程注入的出口代理）。
  urllib 默认读这个变量，于是连 `http://127.0.0.1:11434` 也被送去代理。
  代理只在工具调用期间存活，停了之后网关的所有上游请求都变成
  `[Errno 61] Connection refused`：

    · `/api/tags` 与上游 `/v1/models` 失败 → 模型缓存被清空
    · `/v1/models` 只剩 `auto` → 用户看到的现象是「122B 不见了」
    · 所有推理请求 502，而同一台机器上 curl 一切正常（curl 常带 --noproxy）

  这类故障极难自查：网关 /health 仍 200，进程也在跑，日志只有一句
  Connection refused，看起来像上游挂了。

期望：
  ① 上游拉模型列表不受 HTTP_PROXY 影响（代理指向死端口也必须成功）
  ② 推理请求同样直连，能正常返回
  ③ 该修复不依赖 NO_PROXY 环境变量（plist 里写 NO_PROXY 只是双保险）

用法：
  python3 tests/test_upstream_proxy.py
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

# 测试进程自己也要绕过环境代理，否则连不上被测网关
urllib.request.getproxies = lambda: {}

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.environ.get("TG_SRC") or os.path.join(ROOT, "token-gateway.py")
PY = os.environ.get("TG_PY") or sys.executable
KEY = "sk-ollama-TEST-PROXY"
MODEL = "upstream-ok:latest"

RESULTS = []
SEEN = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def dead_port():
    """拿一个曾经监听、现在已经关闭的端口——模拟"代理进程已退出"。"""
    p = free_port()
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=0.3):
            pass
    except OSError:
        return p
    return p


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._json(200, {"models": [
                {"name": MODEL, "size": 3_000_000_000,
                 "details": {"parameter_size": "3B", "quantization_level": "Q4_K_M",
                             "family": "llama"}},
            ]})
        elif self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": []})
        else:
            self._json(200, {"data": []})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw)
        except Exception:
            req = {}
        SEEN.append(req.get("model", ""))
        self._json(200, {
            "id": "chatcmpl-proxy", "object": "chat.completion", "created": 0,
            "model": req.get("model", ""),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        })


def start_stub():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def start_gateway(stub_port, poison):
    """poison=(http_proxy, no_proxy)：no_proxy=None 表示彻底移除 NO_PROXY。"""
    d = tempfile.mkdtemp(prefix="gw-proxy-")
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    port = free_port()
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump({
            "keys": {KEY: {"name": "t", "daily_tokens": 0, "monthly_tokens": 0, "qps": 100,
                           "rate_limited": False, "active": True, "created": "2026-11-08"}},
            "global": {"max_concurrency": 8, "allow_models": ["*"], "deny_models": [],
                       "listen_host": "127.0.0.1", "listen_port": port},
            "admin": {"token": ""},
        }, f)
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env.update({
        "TG_DISABLE_HTTPS": "1",
        "TG_CONFIG_DIR": d, "TG_LOG_DIR": os.path.join(d, "logs"),
        "TG_PORT": str(port), "TG_HOST": "127.0.0.1",
        "TG_OLLAMA_HOST": f"http://127.0.0.1:{stub_port}",
        "TG_RAPID_MLX_HOSTS": "", "TG_RAPID_MLX_VLM_HOSTS": "",
        "TG_AUTO_STRATEGY": "quality",
        "TG_MIN_MAX_TOKENS_TEXT": "0",
        "TG_CONNECT_TIMEOUT": "3",
    })
    http_proxy, no_proxy = poison
    if http_proxy:
        env["HTTP_PROXY"] = http_proxy
        env["http_proxy"] = http_proxy
        env["ALL_PROXY"] = http_proxy
    if no_proxy:
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    proc = subprocess.Popen([PY, "-u", SRC], env=env,
                            stdout=open(os.path.join(d, "srv.log"), "wb"),
                            stderr=subprocess.STDOUT)
    for _ in range(100):
        if proc.poll() is not None:
            logtxt = open(os.path.join(d, "srv.log"), errors="replace").read()
            raise RuntimeError(f"网关启动失败（exit={proc.returncode}）:\n{logtxt[-1500:]}")
        time.sleep(0.2)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            pass
    return proc, d, port


def get_models(port):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                 headers={"Authorization": "Bearer " + KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def chat(port):
    body = json.dumps({"model": "auto",
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 32}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def main():
    stub, stub_port = start_stub()
    procs = []
    try:
        dp = dead_port()
        print(f"死代理端口 = {dp}（模拟沙箱代理已退出）\n")

        print("-- 1. 代理指向死端口 + 没有 NO_PROXY：模型列表仍要能拉到 --")
        proc, d, port = start_gateway(stub_port, (f"http://127.0.0.1:{dp}", None))
        procs.append(proc)
        st, body = get_models(port)
        ids = [m.get("id") for m in body.get("data", [])]
        check("状态 200", st == 200, str(st))
        check("列出上游真实模型", MODEL in ids, str(ids))
        check("auto 仍在列表中", "auto" in ids, str(ids))

        print("\n-- 2. 同一进程的推理请求也要直连 --")
        SEEN.clear()
        st2, body2 = chat(port)
        check("状态 200", st2 == 200, str(st2))
        check("上游收到请求", SEEN == ["auto"] or SEEN == [MODEL], str(SEEN))
        got = (body2.get("choices") or [{}])[0].get("message", {}).get("content")
        check("拿到正文", got == "ok", repr(got))

        print("\n-- 3. NO_PROXY='*' 只是双保险：没它也照样直连（已由 1/2 覆盖）--")
        proc2, d2, port2 = start_gateway(stub_port, (f"http://127.0.0.1:{dp}", "*"))
        procs.append(proc2)
        st3, body3 = get_models(port2)
        ids3 = [m.get("id") for m in body3.get("data", [])]
        check("带 NO_PROXY 时同样正常", st3 == 200 and MODEL in ids3, f"{st3} {ids3}")
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        stub.shutdown()

    total = len(RESULTS)
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"\n{'='*54}\n{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
