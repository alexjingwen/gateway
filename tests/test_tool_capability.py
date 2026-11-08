#!/usr/bin/env python3
"""回归：带 tools 的请求不得落到不支持工具调用的模型上。

背景（2026-11-08）：
  qwen2.5vl:3b 这类视觉模型对话完全正常，但不接受 tools，上游直接回
    400 {"message":"registry.ollama.ai/library/qwen2.5vl:3b does not support tools"}
  auto 的分档只看请求体量（工具清单不计入 chars），一条带 tools 的短请求被
  判成「短问答」轻量档 —— 于是正好选中这个小 VLM，表现就是"一开工具调用，
  每个请求都 400"，而且错误原文带着 registry 路径，客户端只会把它当成
  莫名其妙的拒绝。

期望：
  ① auto + tools        → 能力探测阶段就过滤掉不支持 tools 的模型，压根不发上游
  ② 显式点名 + tools    → 400 且错误码为 model_no_tools，文案不含 registry 路径
  ③ 显式点名 + 无 tools → 仍然 200：能力边界不等于模型损坏，不能熔断
  ④ 能力接口不可用      → 靠上游 400 反向学习，auto 换下一个候选并 200
  ⑤ 学到之后            → 后续 auto + tools 直接跳过它，不再浪费一次往返
  ⑥ 无 tools 的请求     → 不受能力缓存影响，照常选中该模型

用法：
  python3 tests/test_tool_capability.py
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
KEY = "sk-ollama-TEST-TOOLCAP"

SMALL = "qwen2.5vl:3b"        # 有视觉、没 tools
BIG = "big-tool:8b"           # 有 tools

RESULTS = []
SEEN = []          # 上游收到的 (model, path)
SHOW_SEEN = []     # /api/show 探测过的模型
SHOW_MODE = "normal"   # normal | 404（模拟老版本 Ollama 没有 capabilities 字段）

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查天气",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    },
}]


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
                {"name": SMALL, "size": 3_200_000_000,
                 "details": {"parameter_size": "3B", "quantization_level": "Q4_K_M",
                             "family": "qwen2vl"}},
                {"name": BIG, "size": 8_000_000_000,
                 "details": {"parameter_size": "8B", "quantization_level": "Q4_K_M",
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
        if self.path.startswith("/api/show"):
            name = req.get("name", "")
            SHOW_SEEN.append(name)
            if SHOW_MODE == "404":
                # 老版本 Ollama：没有这个端点 / 没有 capabilities 字段
                self._json(404, {"error": "not found"})
                return
            caps = ["completion", "vision"] if name == SMALL else ["completion", "tools"]
            self._json(200, {"capabilities": caps, "details": {}})
            return

        model = req.get("model", "")
        SEEN.append((model, self.path))
        if model == SMALL and (req.get("tools") or req.get("functions")):
            self._json(400, {"message": f"registry.ollama.ai/library/{SMALL} does not support tools",
                             "type": "invalid_request_error", "param": None, "code": None})
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


def start_gateway(stub_port, extra_env=None, auto_quality=None):
    d = tempfile.mkdtemp(prefix="gw-toolcap-")
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    port = free_port()
    g = {"max_concurrency": 8, "allow_models": ["*"], "deny_models": [],
         "listen_host": "127.0.0.1", "listen_port": port}
    if auto_quality:
        g["auto_quality"] = list(auto_quality)
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump({
            "keys": {KEY: {"name": "t", "daily_tokens": 0, "monthly_tokens": 0, "qps": 100,
                           "rate_limited": False, "active": True, "created": "2026-11-08"}},
            "global": g,
            "admin": {"token": ""},
        }, f)
    env = dict(os.environ)
    env.update({
        "TG_DISABLE_HTTPS": "1", "NO_PROXY": "*",
        "TG_CONFIG_DIR": d, "TG_LOG_DIR": os.path.join(d, "logs"),
        "TG_PORT": str(port), "TG_HOST": "127.0.0.1",
        "TG_OLLAMA_HOST": f"http://127.0.0.1:{stub_port}",
        "TG_RAPID_MLX_HOSTS": "", "TG_RAPID_MLX_VLM_HOSTS": "",
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


def stop(proc, d):
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(d, ignore_errors=True)


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
        return json.loads(txt).get("model", "")
    except Exception:
        return ""


def read_log(d):
    return open(os.path.join(d, "srv.log"), errors="replace").read()


def main():
    global SHOW_MODE
    stub, stub_port = start_stub()
    base = {"messages": [{"role": "user", "content": "帮我查一下北京天气"}], "max_tokens": 64}
    with_tools = dict(base, tools=TOOLS)

    print("\n-- 1. auto + tools：能力探测阶段就排除不支持 tools 的模型 --")
    proc, d, port = start_gateway(stub_port)
    try:
        SEEN.clear()
        SHOW_SEEN.clear()
        st, txt = chat(port, dict(with_tools, model="auto"))
        check("状态 200", st == 200, str(st))
        check("选中 big-tool", body_model(txt) == BIG, body_model(txt))
        check("Qwen2.5-VL 未收到任何请求",
              all(m != SMALL for m, _ in SEEN), str(SEEN))
        log = read_log(d)
        check("日志说明已排除该模型", "已排除 1 个不支持 tools 的模型" in log, log[-200:].replace("\n", " "))

        print("\n-- 2. 显式点名 + tools：400 model_no_tools，不泄露 registry 路径 --")
        SEEN.clear()
        st, txt = chat(port, dict(with_tools, model=SMALL))
        check("状态 400", st == 400, str(st))
        check("错误码 model_no_tools", "model_no_tools" in txt, txt[:160])
        check("未把上游请求发出去（本地即可判定）", len(SEEN) == 0, str(SEEN))
        check("文案不含 registry.ollama.ai", "registry.ollama.ai" not in txt, txt[:160])
        check("文案给出可执行建议", "deny_models" in txt or "auto" in txt, txt[:160])

        print("\n-- 3. 显式点名 + 无 tools：仍 200（能力边界 ≠ 模型损坏）--")
        SEEN.clear()
        st, txt = chat(port, dict(base, model=SMALL))
        check("状态 200", st == 200, str(st))
        check("上游确实收到该模型", any(m == SMALL for m, _ in SEEN), str(SEEN))
    finally:
        stop(proc, d)

    print("\n-- 4. 能力接口不可用（老版本 Ollama）：靠上游 400 反馈学习并换模型 --")
    SHOW_MODE = "404"
    stub2, stub_port2 = start_stub()
    proc2, d2, port2 = start_gateway(stub_port2, auto_quality=[SMALL])
    try:
        SEEN.clear()
        st, txt = chat(port2, dict(with_tools, model="auto"))
        check("状态 200", st == 200, str(st))
        check("先试 small、再换 big",
              [m for m, _ in SEEN] == [SMALL, BIG], str(SEEN))
        check("最终返回 big-tool", body_model(txt) == BIG, body_model(txt))
        log = read_log(d2)
        check("日志记录能力边界", "不支持工具调用" in log, log[-300:].replace("\n", " "))
        check("日志记录降级原因", "不支持 tools" in log, log[-300:].replace("\n", " "))
        check("未误记熔断", "模型熔断" not in log, "模型熔断" in log)

        print("\n-- 5. 学到之后：后续 auto + tools 直接跳过它 --")
        SEEN.clear()
        st, txt = chat(port2, dict(with_tools, model="auto"))
        check("状态 200", st == 200, str(st))
        check("只调用一次（big-tool）", [m for m, _ in SEEN] == [BIG], str(SEEN))

        print("\n-- 6. 无 tools 的请求不受能力缓存影响 --")
        SEEN.clear()
        st, txt = chat(port2, dict(base, model="auto"))
        check("状态 200", st == 200, str(st))
        check("优先表仍选中 small", body_model(txt) == SMALL, body_model(txt))
    finally:
        stop(proc2, d2)
        stub2.shutdown()
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
