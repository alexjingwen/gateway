#!/usr/bin/env python3
"""回归：客户端不传 max_tokens 且请求超出上下文窗口时，网关不得 500。

历史故障（2026-11-05 21:26 线上）：
  上游属于 RAPID_MLX_VLM_HOSTS → `_MIN_MAX_TOKENS` 置 0 → 网关不会补写
  original_req["max_tokens"]；客户端本身也没传该字段；请求超过上下文窗口后
  走「截短历史消息」分支，日志语句却无条件读 original_req['max_tokens']
  → KeyError → 500 internal_error。

本用例用一个固定返回的 stub 上游复现该条件：stub 同时充当 MLX 与 MLX-VLM。

用法：
  python3 tests/test_ctx_trunc.py
      只验当前代码，期望 4/4。
  TG_SRC_OLD=/path/to/old/token-gateway.py python3 tests/test_ctx_trunc.py
      额外对照旧版本，期望 500 + 日志出现 KeyError（共 6 项）。

环境变量：
  TG_SRC      被测网关文件，默认仓库根的 token-gateway.py
  TG_SRC_OLD  旧版本文件，可选；提供时追加「旧代码复现故障」两组断言
  TG_PY       启动网关用的解释器，默认当前解释器
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
SRC_OLD = os.environ.get("TG_SRC_OLD", "")
PY = os.environ.get("TG_PY") or sys.executable
KEY = "sk-ollama-TEST-CTX-0001"
MODEL = "test/ctx-model"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------- stub 上游 ----------
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
        self.rfile.read(n)
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


def start_stub(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------- 网关实例 ----------
def mk_cfg(port):
    d = tempfile.mkdtemp(prefix=f"gw-ctxtest-{port}-")
    os.makedirs(d + "/logs", exist_ok=True)
    cfg = {
        "keys": {KEY: {"name": "t", "daily_tokens": 0, "monthly_tokens": 0,
                       "qps": 100, "rate_limited": False, "active": True,
                       "created": "2026-11-05"}},
        "global": {"max_concurrency": 8, "allow_models": ["*"], "deny_models": [],
                   "listen_host": "127.0.0.1", "listen_port": port},
        "admin": {"token": ""},
    }
    json.dump(cfg, open(d + "/config.json", "w", encoding="utf-8"), ensure_ascii=False)
    return d


def start_gw(src, port, stub_port):
    d = mk_cfg(port)
    env = dict(os.environ)
    env.update({
        "TG_DISABLE_HTTPS": "1",
        "NO_PROXY": "*",
        "TG_CONFIG_DIR": d,
        "TG_LOG_DIR": d + "/logs",
        # stub 同时充当 MLX 与 MLX-VLM：后者会让 _MIN_MAX_TOKENS=0，正是线上条件
        "TG_RAPID_MLX_HOSTS": f"http://127.0.0.1:{stub_port}",
        "TG_RAPID_MLX_VLM_HOSTS": f"http://127.0.0.1:{stub_port}",
    })
    logf = open(d + "/srv.log", "wb")
    p = subprocess.Popen([PY, "-u", src], env=env, stdout=logf, stderr=subprocess.STDOUT)
    for _ in range(80):
        time.sleep(0.25)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return p, d
        except OSError:
            if p.poll() is not None:
                break
    raise RuntimeError(f"网关 {port} 未启动:\n" + open(d + "/srv.log", errors="replace").read()[-800:])


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=8)
    except subprocess.TimeoutExpired:
        p.kill()


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


# 3 条消息共约 33 万字符：超过 262144 上下文，且 room<0 → 走「截短历史」分支
BLOCK = "上下文占位文本。" * 10000          # 约 8 万字符
MSGS = [
    {"role": "system", "content": BLOCK},                 # ~80k
    {"role": "assistant", "content": BLOCK},              # ~80k
    {"role": "user", "content": "请概括上面的内容。"},
]
BIG = [
    {"role": "system", "content": BLOCK},
    {"role": "assistant", "content": BLOCK},
    {"role": "user", "content": BLOCK},
    {"role": "user", "content": "请概括上面的内容。"},
]


def main():
    if not os.path.exists(SRC):
        print(f"找不到被测文件: {SRC}")
        return 2

    stub_port = free_port()
    start_stub(stub_port)

    print("== 场景：客户端不传 max_tokens，请求超过上下文窗口 ==")
    print(f"   被测: {SRC}")
    print(f"   消息字符总量 ≈ {sum(len(m['content']) for m in BIG)}")

    if SRC_OLD and os.path.exists(SRC_OLD):
        print("\n-- A. 旧代码（线上故障版本）--")
        port = free_port()
        p, d = start_gw(SRC_OLD, port, stub_port)
        try:
            st, body = chat(port, BIG)
            check("旧代码返回 500", st == 500, f"status={st} body={body[:120]}")
            log = open(d + "/srv.log", errors="replace").read()
            check("旧代码日志含 KeyError: 'max_tokens'", "KeyError" in log and "max_tokens" in log,
                  [l for l in log.splitlines() if "KeyError" in l][:1])
        finally:
            stop(p)
            shutil.rmtree(d, ignore_errors=True)

    print("\n-- B. 当前代码（期望全部通过）--")
    port = free_port()
    p, d = start_gw(SRC, port, stub_port)
    try:
        st, body = chat(port, BIG)
        check("超窗请求返回 200", st == 200, f"status={st} body={body[:120]}")
        log = open(d + "/srv.log", errors="replace").read()
        check("无 KeyError", "KeyError" not in log,
              [l for l in log.splitlines() if "KeyError" in l][:1])
        check("日志记录截短行为", "截短历史消息" in log,
              [l.strip() for l in log.splitlines() if "截短历史消息" in l][:1])

        # 正常短请求（同样不传 max_tokens）不应受影响
        st2, _ = chat(port, MSGS[:2] + [{"role": "user", "content": "你好"}])
        check("短请求仍 200", st2 == 200, f"status={st2}")
    finally:
        stop(p)
        shutil.rmtree(d, ignore_errors=True)

    print("\n== 汇总 ==")
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
