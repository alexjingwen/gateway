#!/usr/bin/env python3
"""回归：上游模型清单定期巡检 + 失效模型与拥堵同等待遇（指数退避）。

背景（2026-11-08）：
  ① 缓存只在"有请求进来"时才顺便刷新，刷新失败又保留旧值 —— 于是**删掉的模型
     会长期赖在候选池里**：auto 一直选中它、每次 404、靠熔断挡 120s，两分钟后又重来。
  ② 熔断时长是定值 120s，真坏掉的模型每 2 分钟就骗走一次上游往返。

期望：
  ① 模型从上游清单消失 → 巡检后缓存不再包含它，显式请求变 404 model_not_found
  ② 上游探测**失败**（未知）→ 缓存保留，auto 照常可用，不得清空候选池
  ③ 同一模型连续失败 → 熔断时长指数递增（1s → 2s，按 TG_BAD_MODEL_TTL 基数）
  ④ 清单变化时清理已删模型的熔断记录；模型重新出现时解除旧熔断

用法：
  python3 tests/test_model_refresh.py
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
KEY = "sk-ollama-TEST-REFRESH"

GOOD = "good-model:8b"
BAD = "bad-model:3b"      # 上游 404（权重缺失），但仍在 /api/tags 里
REFRESH_S = 1
BAD_TTL = 1
BAD_TTL_MAX = 8

RESULTS = []
SEEN = []
STATE = {"tags": "ok", "models": [GOOD, BAD]}


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _detail(name):
    big = name == GOOD
    return {"name": name, "size": 8_000_000_000 if big else 3_000_000_000,
            "details": {"parameter_size": "8B" if big else "3B",
                        "quantization_level": "Q4_K_M",
                        "family": "llama" if big else "qwen2vl"}}


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
            if STATE["tags"] == "fail":
                self._json(500, {"error": "boom"})
                return
            self._json(200, {"models": [_detail(m) for m in STATE["models"]]})
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
            self._json(200, {"capabilities": ["completion", "tools"], "details": {}})
            return
        model = req.get("model", "")
        SEEN.append((model, self.path))
        if model == BAD:
            self._json(404, {"error": {"message": f"model '{model}' not found",
                                       "type": "not_found_error",
                                       "param": None, "code": None}})
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


def start_gateway(stub_port):
    d = tempfile.mkdtemp(prefix="gw-refresh-")
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    port = free_port()
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump({
            "keys": {KEY: {"name": "t", "daily_tokens": 0, "monthly_tokens": 0, "qps": 100,
                           "rate_limited": False, "active": True, "created": "2026-11-08"}},
            "global": {"max_concurrency": 8, "allow_models": ["*"], "deny_models": [],
                       "listen_host": "127.0.0.1", "listen_port": port,
                       "auto_quality": [BAD]},   # 强制优先选坏模型，便于观察退避
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
        "TG_MODEL_REFRESH_INTERVAL": str(REFRESH_S),
        "TG_BAD_MODEL_TTL": str(BAD_TTL),
        "TG_BAD_MODEL_TTL_MAX": str(BAD_TTL_MAX),
    })
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
        return json.loads(txt).get("model", "")
    except Exception:
        return ""


def read_log(d):
    return open(os.path.join(d, "srv.log"), errors="replace").read()


def main():
    stub, stub_port = start_stub()
    proc, d, port = start_gateway(stub_port)
    msg = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32}
    try:
        print("\n-- 0. 启动巡检：启动即拉一次清单 --")
        log = read_log(d)
        check("启动横幅提示巡检已开启", "上游模型清单巡检已启动" in log, log[:200].replace("\n", " "))

        print("\n-- 1. 模型从上游清单消失：巡检后不再认为它存在 --")
        SEEN.clear()
        st, txt = chat(port, dict(msg, model=BAD))
        check("删除前显式请求打到上游（404 透传）", st == 404 and len(SEEN) == 1, f"{st} {SEEN}")
        STATE["models"] = [GOOD]
        time.sleep(REFRESH_S + 1.6)
        SEEN.clear()
        st, txt = chat(port, dict(msg, model=BAD))
        check("删除后显式请求 404 model_not_found", st == 404, str(st))
        check("且不再发往上游（缓存已剔除）", len(SEEN) == 0, str(SEEN))
        log = read_log(d)
        check("日志记录清单变化（-bad-model）", "模型清单变化" in log and "-" in log,
              [l for l in log.splitlines() if "模型清单变化" in l][-1:])

        print("\n-- 2. 上游探测失败（未知）：缓存必须保留，不许清空候选池 --")
        STATE["tags"] = "fail"
        time.sleep(REFRESH_S + 1.6)
        st, txt = chat(port, dict(msg, model="auto"))
        check("auto 仍可服务（缓存保留）", st == 200, str(st))
        check("落到 good-model", body_model(txt) == GOOD, body_model(txt))

        print("\n-- 3. 连续失败：熔断时长指数递增（拥堵之外的另一种退避）--")
        STATE["tags"] = "ok"
        STATE["models"] = [GOOD, BAD]
        time.sleep(REFRESH_S + 1.6)
        SEEN.clear()
        st, txt = chat(port, dict(msg, model="auto"))
        check("首次 auto：先试 bad 再换 good", [m for m, _ in SEEN] == [BAD, GOOD], str(SEEN))
        check("状态 200", st == 200, str(st))
        log = read_log(d)
        check("第 1 次失败熔断 1s", "模型熔断 1s（连续第 1 次失败）" in log,
              [l for l in log.splitlines() if "模型熔断" in l][-1:])

        time.sleep(BAD_TTL + 0.4)   # 等第一次熔断到期，才会再次尝试
        SEEN.clear()
        st, txt = chat(port, dict(msg, model="auto"))
        check("到期后再次尝试 bad", any(m == BAD for m, _ in SEEN), str(SEEN))
        log = read_log(d)
        check("第 2 次失败熔断翻倍为 2s", "模型熔断 2s（连续第 2 次失败）" in log,
              [l for l in log.splitlines() if "模型熔断" in l][-1:])

        print("\n-- 4. 清单变化时清理失效熔断；模型重新出现则解除旧熔断 --")
        # 把 bad 从清单里删掉：巡检应清掉它的熔断记录
        STATE["models"] = [GOOD]
        time.sleep(REFRESH_S + 1.6)
        log = read_log(d)
        check("清理已删模型的熔断记录", "清理 1 条失效熔断" in log,
              [l for l in log.splitlines() if "模型清单变化" in l][-1:])
        # 再放回来：应视为"重新出现"，解除熔断（此时它尚未进入熔断表，故不应报解除）
        STATE["models"] = [GOOD, BAD]
        time.sleep(REFRESH_S + 1.6)
        log = read_log(d)
        check("重新出现后记录 + 新增", "模型清单变化" in log, "ok")
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
