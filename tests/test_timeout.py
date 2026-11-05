#!/usr/bin/env python3
"""回归：超时 / 转发语义（静默预算、缓冲发送、Content-Length、流式错误事件、错误码）。

用一个可控的假上游（stub）模拟五类上游行为：
  normal        立即返回完整 JSON
  slow          静默 20s 后返回（模拟长上下文预填充，远超预算）
  broken        声明 Content-Length: 500 只发 50 字节就断开
  slowstream    分多段发送 SSE，段间有 6s 空档（< 预算，不得误杀）
  slowstreamdie 只发 1 个 SSE chunk 后彻底静默（应补错误事件且不伪造 [DONE]）

预算被特意压到 8s 以便快速跑完（生产为 600s），语义不变。

用法：
  python3 tests/test_timeout.py
环境变量：
  TG_SRC  被测网关文件，默认仓库根的 token-gateway.py
  TG_PY   启动网关用的解释器，默认当前解释器
"""
import json
import os
import shutil
import signal
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

KEY = "sk-e2e-timeout"
MODEL = "stub-a:latest"
MODEL_PORTS = {}
STUB_PORT = 0
GW_PORT = 0
BASE = ""
READ_TIMEOUT = 8

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------- 假上游 ----------------
def completion(content="ok"):
    return {
        "id": "chatcmpl-stub", "object": "chat.completion", "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


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
            self._json(200, {"models": [{
                "name": MODEL, "model": MODEL, "size": 4194304000,
                "details": {"family": "stub", "parameter_size": "8B",
                            "quantization_level": "Q4_K_M"},
            }]})
        elif self.path.startswith("/v1/models"):
            self._json(200, {"object": "list",
                             "data": [{"id": MODEL, "object": "model", "created": 0}]})
        else:
            self._json(404, {"error": "nf"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw.decode() or "{}")
        except Exception:
            req = {}
        text = json.dumps(req, ensure_ascii=False)
        mode = "normal"
        for m in ("slowstreamdie", "slowstream", "slow", "broken"):
            if m in text:
                mode = m
                break

        if mode == "broken":
            # 声明 500 字节，只发 50 字节就断开
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "500")
            self.end_headers()
            self.wfile.write(b'{"id":"x","choices":[{"message":{"cont')
            self.wfile.flush()
            self.close_connection = True
            return

        if mode in ("slowstream", "slowstreamdie"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.close_connection = True
            pieces = [
                b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"B"}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"C"}}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n',
                b'data: [DONE]\n\n',
            ]
            try:
                self.wfile.write(pieces[0])
                self.wfile.flush()
                if mode == "slowstreamdie":
                    time.sleep(30)          # 静默不动，逼网关超时
                    return
                for p in pieces[1:]:
                    time.sleep(6)           # 段间空档 > 心跳间隔(5s)
                    self.wfile.write(p)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if mode == "slow":
            time.sleep(READ_TIMEOUT * 2.5)  # 远超静默预算
            try:
                self._json(200, completion())
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # normal：6 秒后返回，用于验证「静默但未超预算」不会被误杀
        if "wait6" in text:
            time.sleep(6)
        try:
            self._json(200, completion())
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------- 网关启动 ----------------
def start_gateway(workdir):
    cfg = {
        "keys": {KEY: {"name": "e2e", "daily_tokens": 0, "monthly_tokens": 0,
                       "qps": 100, "rate_limited": False, "active": True,
                       "created": "2026-11-05"}},
        "global": {"max_concurrency": 8, "allow_models": ["*"], "deny_models": [],
                   "listen_host": "127.0.0.1", "listen_port": GW_PORT},
        "admin": {"token": ""},
    }
    json.dump(cfg, open(os.path.join(workdir, "config.json"), "w"),
              ensure_ascii=False, indent=2)

    env = dict(os.environ)
    env.update({
        "TG_CONFIG_DIR": workdir, "TG_DISABLE_HTTPS": "1", "NO_PROXY": "*",
        "TG_HOST": "127.0.0.1", "TG_PORT": str(GW_PORT),
        "TG_OLLAMA_HOST": f"http://127.0.0.1:{STUB_PORT}",
        "TG_RAPID_MLX_HOSTS": "", "TG_RAPID_MLX_VLM_HOSTS": "",
        "TG_READ_TIMEOUT": str(READ_TIMEOUT), "TG_READ_POLL": "2", "TG_SSE_KEEPALIVE": "5",
    })
    log = open(os.path.join(workdir, "gw.log"), "w")
    p = subprocess.Popen([PY, "-u", SRC], env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return p
        except Exception:
            time.sleep(0.3)
    raise SystemExit("网关未能启动，见 " + os.path.join(workdir, "gw.log"))


# ---------------- 请求 ----------------
def call(body, timeout=120):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=data,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY},
                                 method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace"), time.time() - t0


def msg(text):
    return {"model": MODEL, "messages": [{"role": "user", "content": text}], "stream": False}


def main():
    global STUB_PORT, GW_PORT, BASE
    if not os.path.exists(SRC):
        print(f"找不到被测文件: {SRC}")
        return 2

    STUB_PORT, GW_PORT = free_port(), free_port()
    BASE = f"http://127.0.0.1:{GW_PORT}"
    workdir = tempfile.mkdtemp(prefix="gw-timeout-test-")

    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
    stub.daemon_threads = True
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    p = start_gateway(workdir)
    print(f"== 被测: {SRC}  |  网关 :{GW_PORT}  stub :{STUB_PORT}  静默预算 {READ_TIMEOUT}s ==")

    try:
        print("== 1. 正常非流式：缓冲 + Content-Length ==")
        st, h, b, el = call(msg("hello"))
        check("状态 200", st == 200, str(st))
        cl = h.get("Content-Length")
        check("带 Content-Length", cl is not None, str(cl))
        check("Content-Length 与实际一致", cl is not None and int(cl) == len(b.encode()),
              f"{cl} vs {len(b.encode())}")
        check("响应体是完整 JSON", json.loads(b).get("model") == MODEL, b[:80])
        check("耗时短（<2s）", el < 2, f"{el:.2f}s")

        print("== 2. 静默 6s / 预算 8s：不得误杀 ==")
        st, h, b, el = call(msg("wait6 please"))
        check("状态 200", st == 200, str(st))
        check("耗时 ≥6s 且 <8s", 5.5 <= el < 8.5, f"{el:.2f}s")
        check("响应完整", json.loads(b).get("choices")[0]["message"]["content"] == "ok", b[:60])

        print("== 3. 静默 20s / 预算 8s：返回 504 而不是断流 ==")
        st, h, b, el = call(msg("slow down"))
        check("状态 504", st == 504, str(st))
        check("在预算附近放弃（8~12s）", 7 <= el <= 13, f"{el:.2f}s")
        try:
            err = json.loads(b)["error"]
            check("错误类型 upstream_timeout", err.get("type") == "upstream_timeout", str(err.get("type")))
            check("错误信息含静默秒数与预算", "静默预算" in err.get("message", ""), err.get("message", "")[:90])
        except Exception as e:
            check("返回结构化错误 JSON", False, f"{type(e).__name__}: {b[:120]}")

        print("== 4. 上游截断（声明 500 只发 50）：502 ==")
        st, h, b, el = call(msg("broken pipe"))
        check("状态 502", st == 502, str(st))
        try:
            err = json.loads(b)["error"]
            check("错误类型 upstream_broken", err.get("type") == "upstream_broken", str(err.get("type")))
            check("指出响应不完整", "不完整" in err.get("message", ""), err.get("message", "")[:90])
        except Exception as e:
            check("返回结构化错误 JSON", False, f"{type(e).__name__}: {b[:120]}")

        print("== 5. 流式：段间空档 6s / 预算 8s，应能读完整 ==")
        st, h, b, el = call({"model": MODEL, "stream": True,
                             "messages": [{"role": "user", "content": "slowstream"}]})
        check("状态 200", st == 200, str(st))
        check("三个内容块都在", all(x in b for x in ('"A"', '"B"', '"C"')), b[:120])
        check("以 [DONE] 结束", b.rstrip().endswith("data: [DONE]"), repr(b[-40:]))
        check("总耗时 ≥12s（未被单次读超时误杀）", el >= 11.5, f"{el:.2f}s")

        print("== 6. 流式中途静默：补错误事件，不发 [DONE] ==")
        st, h, b, el = call({"model": MODEL, "stream": True,
                             "messages": [{"role": "user", "content": "slowstreamdie"}]})
        check("状态 200（头已发）", st == 200, str(st))
        check("含 upstream_timeout 事件", "upstream_timeout" in b, b[-160:])
        check("不伪造 [DONE]", "[DONE]" not in b, b[-80:])
        check("在预算附近结束（8~13s）", 7 <= el <= 14, f"{el:.2f}s")

        print("== 7. 日志可诊断性 ==")
        gwlog = open(os.path.join(workdir, "gw.log")).read()
        check("有上游超时记录", "上游超时" in gwlog, gwlog.count("上游超时"))
        check("有中断记录", "读取上游响应中断" in gwlog, gwlog.count("读取上游响应中断"))
        check("超时不重试（无「1秒后重试」）", "1秒后重试" not in gwlog)

        print("== 8. 数据完整性：并发 3 个正常请求 ==")
        res = []
        ths = [threading.Thread(target=lambda i=i: res.append(call(msg(f"hi{i}"))[0])) for i in range(3)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        check("全部 200", res.count(200) == 3, str(res))

    finally:
        try:
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=10)
        except Exception:
            p.kill()
        stub.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
