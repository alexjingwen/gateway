#!/usr/bin/env python3
"""回归：/v1/models 与 /api/tags 改为读缓存 + 后台刷新。

背景（2026-11-08）：
  列举接口每次都现场向上游发 1+N 个同步请求（各 timeout=3s），于是
  ① 客户端轮询列表时可能阻塞数秒；
  ② 任一上游抖动，`except: pass` 会让列表**静默少几个模型**（最差只剩 auto），
     客户端据此缓存下来的结果也会跟着错；
  ③ 缓存只被"请求顺带刷新"，而且旧实现把"上游答复空清单"当成失败丢弃，
     于是**删掉的模型能长期留在列表里**。

期望：
  ① 缓存有效期内重复列举不再触达上游（TG_MODEL_LIST_TTL）
  ② ?refresh=1 立即把上游的真实清单拿过来（删掉的模型当场消失）
  ③ 上游探测失败（未知）→ 沿用手里的清单，不得塌成只剩 auto
  ④ 上游明确答复空清单（模型全删）→ 列表只剩 auto，这是"删掉还在"的最终验收
  ⑤ /api/tags 与 /v1/models 同源，且保留上游原始字段（details / size）
  ⑥ 上游变慢时列举不被拖住（缓存命中即返回）

用法：
  python3 tests/test_model_list_cache.py
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
KEY = "sk-ollama-TEST-LIST"

GOOD = "good-model:8b"
BAD = "bad-model:3b"
LIST_TTL = 2          # TG_MODEL_LIST_TTL
SLOW_S = 2.0          # 慢上游延迟

RESULTS = []
STATE = {"models": [GOOD, BAD], "tags": "ok", "slow": 0.0, "hits": 0}
HITS_LOCK = threading.Lock()


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _entry(name):
    big = name == GOOD
    return {"name": name, "size": 8_000_000_000 if big else 3_000_000_000,
            "modified_at": "2026-11-08T00:00:00Z",
            "details": {"parameter_size": "8B" if big else "3B",
                        "quantization_level": "Q4_K_M", "family": "llama"}}


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
            with HITS_LOCK:
                STATE["hits"] += 1
            if STATE["slow"]:
                time.sleep(STATE["slow"])
            if STATE["tags"] == "fail":
                self._json(500, {"error": "boom"})
                return
            self._json(200, {"models": [_entry(m) for m in STATE["models"]]})
        elif self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": []})
        else:
            self._json(200, {"data": []})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        self._json(200, {"capabilities": ["completion", "tools"], "details": {}})


def start_stub():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def start_gateway(stub_port):
    d = tempfile.mkdtemp(prefix="gw-list-")
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
        "TG_MIN_MAX_TOKENS_TEXT": "0",
        "TG_CONNECT_TIMEOUT": "3",
        # 关掉巡检线程，让"谁刷新了缓存"这件事可归因到列举接口本身
        "TG_MODEL_REFRESH_INTERVAL": "0",
        "TG_MODEL_LIST_TTL": str(LIST_TTL),
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


def get(port, path, timeout=30):
    t0 = time.time()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8", "replace"))
    return body, time.time() - t0


def ids(body):
    return [m["id"] for m in body.get("data", [])]


def names(body):
    return [m.get("name") for m in body.get("models", [])]


def hits():
    with HITS_LOCK:
        return STATE["hits"]


def read_log(d):
    return open(os.path.join(d, "srv.log"), errors="replace").read()


def main():
    stub, stub_port = start_stub()
    proc, d, port = start_gateway(stub_port)
    try:
        print("\n-- 0. 冷启动：一次同步探测拿到真实清单 --")
        b, _ = get(port, "/v1/models")
        check("列表含上游模型", GOOD in ids(b) and BAD in ids(b), ids(b))
        check("auto 仍排在最后", ids(b)[-1] == "auto", ids(b))
        check("冷启动确实探测了上游", hits() >= 1, f"hits={hits()}")

        print("\n-- 1. 缓存有效期内：重复列举不再触达上游 --")
        h0 = hits()
        for _ in range(3):
            get(port, "/v1/models")
            get(port, "/api/tags")
        time.sleep(0.3)
        check("6 次列举 0 次上游探测", hits() == h0, f"{h0} -> {hits()}")

        print("\n-- 2. 删除模型 + ?refresh=1：当场从列表消失 --")
        STATE["models"] = [GOOD]
        b, _ = get(port, "/v1/models?refresh=1")
        check("v1/models 不再含已删模型", BAD not in ids(b), ids(b))
        check("仍在的模型没被误删", GOOD in ids(b), ids(b))
        t, _ = get(port, "/api/tags?refresh=1")
        check("api/tags 同样剔除已删模型", BAD not in names(t), names(t))

        print("\n-- 3. 上游探测失败（未知）：沿用手里的清单，不得塌成只剩 auto --")
        STATE["tags"] = "fail"
        time.sleep(LIST_TTL + 0.4)
        b, _ = get(port, "/v1/models")
        check("上游 500 时列表仍含 good-model", GOOD in ids(b), ids(b))
        time.sleep(1.2)          # 等后台刷新跑完（失败 → 保留旧值）
        b, _ = get(port, "/v1/models")
        check("后台刷新失败后仍保留", GOOD in ids(b), ids(b))
        STATE["tags"] = "ok"

        print("\n-- 4. 上游明确答复空清单：模型全删 → 列表只剩 auto --")
        STATE["models"] = []
        b, _ = get(port, "/v1/models?refresh=1")
        check("空清单被当真（不再保留旧模型）", ids(b) == ["auto"], ids(b))
        t, _ = get(port, "/api/tags?refresh=1")
        check("api/tags 也清空", names(t) == [], names(t))

        print("\n-- 5. /api/tags 保留上游原始字段 --")
        STATE["models"] = [GOOD]
        t, _ = get(port, "/api/tags?refresh=1")
        e = next((m for m in t.get("models", []) if m.get("name") == GOOD), {})
        check("details.parameter_size 透传", (e.get("details") or {}).get("parameter_size") == "8B", e)
        check("size 透传非零", (e.get("size") or 0) > 0, e.get("size"))
        check("modified_at 透传", bool(e.get("modified_at")), e.get("modified_at"))

        print("\n-- 6. 上游变慢：缓存在手时列举不被拖住 --")
        STATE["slow"] = SLOW_S
        b, dt = get(port, "/v1/models")
        check(f"上游延迟 {SLOW_S}s，列举仍 <0.5s", dt < 0.5, f"{dt:.2f}s")
        check("内容仍是缓存里的清单", GOOD in ids(b), ids(b))
        # 等后台刷新（慢）跑完，避免污染后续判定
        time.sleep(SLOW_S + 1.0)
        STATE["slow"] = 0.0

        print("\n-- 7. 缓存过期后由后台刷新接手（不阻塞响应）--")
        STATE["models"] = [GOOD, BAD]
        time.sleep(LIST_TTL + 0.4)
        b, dt = get(port, "/v1/models")
        check("过期瞬间照旧秒回", dt < 0.5, f"{dt:.2f}s")
        time.sleep(1.5)
        b, _ = get(port, "/v1/models")
        check("后台刷新后新模型出现", BAD in ids(b), ids(b))

        log = read_log(d)
        check("未打印旧式同步探测异常", "获取模型列表失败" not in log,
              [l for l in log.splitlines() if "获取模型列表失败" in l][:2])
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
