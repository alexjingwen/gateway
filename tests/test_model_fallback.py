#!/usr/bin/env python3
"""回归：上游模型失效时的自动检测与降级。

背景（2026-11-08）：
  `/api/tags` 只报告"manifest 在不在",不保证权重能加载。实测中一个模型的
  config blob 缺失后，它仍然出现在 /api/tags 里，但任何请求都会被上游判定
  `model 'x' not found`。此时 auto 策略按分档反复选中它（短问答→8B 级），
  把 404 原样打回客户端 —— 尽管候选池里还有别的模型可用。用户侧表现为
  "模型没了，网关也跟着废了"。

期望：
  ① 显式点名不可用模型   → 404 透传（用户要的就是它，不擅自换模型）
  ② auto 选中不可用模型  → 自动换下一个候选重试并返回 200
  ③ 失败过的模型进入熔断 → 后续 auto 直接跳过，不再浪费一次上游往返
  ④ 熔断中的显式请求     → 立即 404，且**不发出**上游请求
  ⑤ 熔断到期             → 模型重新进入候选池
  ⑥ 全部候选失效         → 明确 503 no_candidate，而不是挂住

用法：
  python3 tests/test_model_fallback.py
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
KEY = "sk-ollama-TEST-FALLBACK"
BIG = "big-a:latest"      # 上游判定为不可用
SMALL = "small-b:latest"  # 唯一可用
BAD_TTL = 2

RESULTS = []
SEEN = []  # 上游收到的 (model, path)


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
                {"name": BIG, "size": 120_000_000_000,
                 "details": {"parameter_size": "120B", "quantization_level": "Q4_K_M", "family": "qwen"}},
                {"name": SMALL, "size": 3_000_000_000,
                 "details": {"parameter_size": "3B", "quantization_level": "Q4_K_M", "family": "llama"}},
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
        model = req.get("model", "")
        SEEN.append((model, self.path))
        if model == BIG:
            self._json(404, {"error": {
                "message": f"model '{model}' not found",
                "type": "not_found_error", "param": None, "code": None}})
            return
        self._json(200, {
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        })


def start_stub():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def start_gateway(stub_port, extra_env=None):
    d = tempfile.mkdtemp(prefix="gw-fallback-")
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
    env = dict(os.environ)
    env.update({
        "TG_DISABLE_HTTPS": "1", "NO_PROXY": "*",
        "TG_CONFIG_DIR": d, "TG_LOG_DIR": os.path.join(d, "logs"),
        "TG_PORT": str(port), "TG_HOST": "127.0.0.1",
        "TG_OLLAMA_HOST": f"http://127.0.0.1:{stub_port}",
        "TG_RAPID_MLX_HOSTS": "", "TG_RAPID_MLX_VLM_HOSTS": "",
        "TG_AUTO_STRATEGY": "quality",
        "TG_BAD_MODEL_TTL": str(BAD_TTL),
        "TG_MIN_MAX_TOKENS_TEXT": "0",
        "TG_CONNECT_TIMEOUT": "3",
    })
    if extra_env:
        env.update(extra_env)
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


def chat(port, body, timeout=60):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                 data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def body_model(txt):
    try:
        return (json.loads(txt).get("choices") or [{}])[0].get("message", {}).get("model") \
            or json.loads(txt).get("model", "")
    except Exception:
        return ""


def main():
    stub, stub_port = start_stub()
    proc, d, port = start_gateway(stub_port)
    try:
        msg = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}

        print("\n-- 1. 显式点名不可用模型：404 透传，不擅自换模型 --")
        SEEN.clear()
        st, txt = chat(port, dict(msg, model=BIG))
        check("状态 404", st == 404, str(st))
        check("上游被调用 1 次（big-a）", len(SEEN) == 1 and SEEN[0][0] == BIG, str(SEEN))
        check("错误信息透传上游原文", "not found" in txt.lower(), txt[:100])

        print("\n-- 2. auto 选中不可用模型：自动降级到可用模型 --")
        SEEN.clear()
        st, txt = chat(port, dict(msg, model="auto"))
        check("状态 200", st == 200, str(st))
        check("返回体 model 为 small-b", body_model(txt) == SMALL, body_model(txt))
        check("上游收到 2 次：先 big-a 再 small-b",
              [m for m, _ in SEEN] == [BIG, SMALL], str(SEEN))

        log = open(os.path.join(d, "srv.log"), errors="replace").read()
        check("日志记录熔断", "模型熔断" in log)
        check("日志记录降级", "auto 降级" in log)

        print("\n-- 3. 熔断生效：后续 auto 直接跳过坏模型 --")
        SEEN.clear()
        st, txt = chat(port, dict(msg, model="auto"))
        check("状态 200", st == 200, str(st))
        check("上游只被调用 1 次（small-b）", len(SEEN) == 1 and SEEN[0][0] == SMALL, str(SEEN))

        print("\n-- 4. 熔断中的显式请求：立即 404，不发上游 --")
        SEEN.clear()
        t0 = time.time()
        st, txt = chat(port, dict(msg, model=BIG))
        el = time.time() - t0
        check("状态 404", st == 404, str(st))
        check("未发出上游请求", len(SEEN) == 0, str(SEEN))
        check("快速返回（<1s）", el < 1.0, f"{el:.2f}s")

        print(f"\n-- 5. 熔断到期（{BAD_TTL}s）：模型重回候选池 --")
        time.sleep(BAD_TTL + 0.6)
        SEEN.clear()
        st, txt = chat(port, dict(msg, model="auto"))
        check("状态 200", st == 200, str(st))
        check("重新尝试过 big-a", any(m == BIG for m, _ in SEEN), str(SEEN))
        check("最终仍成功", body_model(txt) == SMALL, body_model(txt))

        print("\n-- 6. 全部候选被排除：明确 503，不挂住 --")
        # 两个模型都被排除 → 候选池为空，应立刻 503 而不是反复转发
        proc2, d2, port2 = start_gateway(stub_port, {
            "TG_AUTO_EXCLUDE": f"{SMALL},{BIG}",
        })
        try:
            SEEN.clear()
            t0 = time.time()
            st, txt = chat(port2, dict(msg, model="auto"))
            el = time.time() - t0
            check("状态 503", st == 503, str(st))
            check("错误码 auto_no_candidate", "auto_no_candidate" in txt, txt[:120])
            check("快速返回（<3s）", el < 3.0, f"{el:.2f}s")
        finally:
            proc2.terminate()
            try:
                proc2.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc2.kill()
            shutil.rmtree(d2, ignore_errors=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(d, ignore_errors=True)
        stub.shutdown()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'=' * 56}\n{passed}/{len(RESULTS)} 通过")
    if passed != len(RESULTS):
        for n, ok, det in RESULTS:
            if not ok:
                print(f"  FAIL: {n}  {det}")
        sys.exit(1)


if __name__ == "__main__":
    main()
