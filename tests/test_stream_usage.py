#!/usr/bin/env python3
"""回归：流式请求的 token 记账不得把 SSE 框架算成 token。

背景（2026-11-07 线上日志）：
  流式请求的 SSE 默认不带 usage，网关退回「原始字节 // 3」估算。SSE 每帧
  `data: {...}\n\n` 约 230 字节，于是"估算出来的 token"里九成是 JSON 与换行：
  实测一个真实 64 token 的流式回复被记成 C:27593 / 4,389 token（约 68 倍），
  直接按 27k 扣配额。

期望：
  ① 无 usage 时按**增量文本字符**折算（TOKENS_PER_CHAR，默认 0.5），误差 < 2×
  ② 非流式有 usage 时仍用上游精确值
  ③ 默认不向上游索取 stream_options.include_usage（不加 `choices: []` 帧给客户端）
  ④ TG_STREAM_INCLUDE_USAGE=1 时才注入，客户端可见的仍是原始字节流

用法：
  python3 tests/test_stream_usage.py
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
KEY = "sk-ollama-TEST-STREAMUSAGE"
MODEL = "test/stream-model"
CHUNKS = 20                      # 每帧 10 个汉字 → 共 200 字符（≈100 token @0.5）
TEXT = "一二三四五六七八九十"
RESULTS = []
SEEN = []                        # stub 收到的请求（含是否带 stream_options）


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
        req = json.loads(self.rfile.read(n).decode() or "{}")
        SEEN.append({
            "stream": bool(req.get("stream")),
            "stream_options": req.get("stream_options"),
        })
        if req.get("stream"):
            parts = []
            for _ in range(CHUNKS):
                chunk = {
                    "id": "c", "object": "chat.completion.chunk", "model": MODEL,
                    "choices": [{"index": 0, "delta": {"content": TEXT},
                                 "finish_reason": None}],
                }
                parts.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
            last = {"id": "c", "object": "chat.completion.chunk", "model": MODEL,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            parts.append("data: " + json.dumps(last, ensure_ascii=False) + "\n\n")
            parts.append("data: [DONE]\n\n")
            body = "".join(parts).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = json.dumps({
            "id": "c", "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def mk_cfg(port):
    d = tempfile.mkdtemp(prefix="gw-streamusage-")
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


def start_gw(port, env_extra):
    d = mk_cfg(port)
    env = dict(os.environ)
    env.update({"TG_DISABLE_HTTPS": "1", "NO_PROXY": "*",
                "TG_CONFIG_DIR": d, "TG_LOG_DIR": d + "/logs"})
    env.update(env_extra)
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


def chat(port, extra):
    body = {"model": MODEL, "messages": [{"role": "user", "content": "你好"}]}
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


def usage_lines(d):
    path = os.path.join(d, "logs", "usage.jsonl")
    try:
        return [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    except Exception:
        return []


def wait_new_usage(d, prev, tries=60):
    """等 usage.jsonl 行数超过 prev，返回新追加的最后一条（避免读到上一条）。"""
    for _ in range(tries):
        lines = usage_lines(d)
        if len(lines) > prev:
            return json.loads(lines[-1])
        time.sleep(0.1)
    return {}


def main():
    if not os.path.exists(SRC):
        print(f"找不到被测文件: {SRC}")
        return 2
    print(f"== 被测: {SRC} ==")

    stub_port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ---------- 默认配置 ----------
    port = free_port()
    p, d = start_gw(port, {"TG_OLLAMA_HOST": f"http://127.0.0.1:{stub_port}",
                           "TG_RAPID_MLX_HOSTS": f"http://127.0.0.1:{free_port()}",
                           "TG_RAPID_MLX_VLM_HOSTS": ""})
    try:
        print("\n-- 1. 流式无 usage：按增量文本折算，不按原始字节 --")
        SEEN.clear()
        n0 = len(usage_lines(d))
        st, txt = chat(port, {"stream": True, "max_tokens": 256})
        check("状态 200", st == 200, str(st))
        check("返回 SSE", "data:" in txt, txt[:40])
        u = wait_new_usage(d, n0)
        comp = u.get("completion_tokens", -1)
        total_chars = CHUNKS * len(TEXT)                    # 200 字符
        want = int(total_chars * 0.5)                       # ≈100
        sse_bytes = len(txt.encode("utf-8"))
        old_est = sse_bytes // 3
        check(f"completion_tokens≈{want}（字符折算）",
              0 < comp <= want * 2, f"实际 {comp}，want {want}")
        check(f"远小于旧估算 bytes//3={old_est}", comp < old_est // 5,
              f"{comp} vs {old_est}")
        check("prompt_tokens 未被算出天文数字", 0 < u.get("prompt_tokens", 0) < 500,
              str(u.get("prompt_tokens")))
        check("默认不注入 stream_options",
              SEEN and SEEN[-1]["stream_options"] is None, str(SEEN[-1]))

        print("\n-- 2. 非流式有 usage：仍用上游精确值 --")
        n1 = len(usage_lines(d))
        st, _ = chat(port, {"stream": False, "max_tokens": 256})
        check("状态 200", st == 200, str(st))
        u = wait_new_usage(d, n1)
        check("completion_tokens=11（上游原值）", u.get("completion_tokens") == 11,
              str(u.get("completion_tokens")))
        check("prompt_tokens=7（上游原值）", u.get("prompt_tokens") == 7,
              str(u.get("prompt_tokens")))
    finally:
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
        shutil.rmtree(d, ignore_errors=True)

    # ---------- TG_STREAM_INCLUDE_USAGE=1 ----------
    port2 = free_port()
    p2, d2 = start_gw(port2, {"TG_OLLAMA_HOST": f"http://127.0.0.1:{stub_port}",
                              "TG_RAPID_MLX_HOSTS": f"http://127.0.0.1:{free_port()}",
                              "TG_RAPID_MLX_VLM_HOSTS": "",
                              "TG_STREAM_INCLUDE_USAGE": "1"})
    try:
        print("\n-- 3. TG_STREAM_INCLUDE_USAGE=1 → 向上游索取精确 usage --")
        SEEN.clear()
        n2 = len(usage_lines(d2))
        st, txt = chat(port2, {"stream": True, "max_tokens": 256})
        check("状态 200", st == 200, str(st))
        check("上游收到 stream_options.include_usage",
              SEEN and (SEEN[-1]["stream_options"] or {}).get("include_usage") is True,
              str(SEEN[-1]))
        check("客户端仍收到原始字节流（含 [DONE]）", "[DONE]" in txt, txt[-40:])
        # stub 不返回 usage，所以这里仍走估算——断言它没有退化成 bytes//3
        u = wait_new_usage(d2, n2)
        check("估算仍然合理", 0 < u.get("completion_tokens", 0) <= CHUNKS * len(TEXT),
              str(u.get("completion_tokens")))
    finally:
        p2.terminate()
        try:
            p2.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p2.kill()
        shutil.rmtree(d2, ignore_errors=True)
        srv.shutdown()

    print("\n== 汇总 ==")
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
