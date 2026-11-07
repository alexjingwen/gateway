#!/usr/bin/env python3
"""回归：Ollama 侧输出预算下限 + 推理模型"空正文"修复。

背景（2026-11-07 线上实测）：
  gemma4:latest 是"会思考"的模型，先把预算花在 reasoning 上，预算耗尽时
  message.content 为空、finish_reason=length —— 客户端看到的是一条"成功但空"的回复。
  直连与经网关均已复现：
      max_tokens=64  → content 0 字符 / reasoning 233 字符 / completion_tokens=64
      max_tokens=192 → 同样为空
      max_tokens=4096 → reasoning 最多测到 1960 字符才出正文
  且是否触发思维链是**概率性**的（同一句提示另一次仅 34 token 直接出结果），
  所以单靠预算下限挡不住，必须有确定性兜底。

  根因之一：预算下限整块嵌在 `upstream_host in RAPID_MLX_HOSTS` 分支里，
  Ollama 侧完全不受约束。Ollama 的 OpenAI 兼容端点还会**忽略**
  think / reasoning_effort（已实测），无法从根上关掉思维链。

期望：
  ① Ollama 纯文本        → 抬到 TG_MIN_MAX_TOKENS_TEXT（默认 2048）
  ② Ollama 纯文本 + tools → 抬到 TG_MIN_MAX_TOKENS_TOOLS（默认 8192）
  ③ Ollama 带图          → 不抬（视觉要短输出、低延迟）
  ④ 已高于下限           → 不动
  ⑤ 非流式空正文 + reasoning + finish=length → 以更大预算重发**一次**并返回正文
  ⑥ 重发仍为空           → 只重发一次，不循环
  ⑦ 流式响应             → 不重发（已发出的分片无法回滚）
  ⑧ Rapid-MLX 既有语义   → 不变（纯文本不抬 / 带 tools 抬）

用法：
  python3 tests/test_reasoning_retry.py
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
KEY = "sk-ollama-TEST-REASONING"
MODEL = "test/reason-model"
TEXT_FLOOR = 2048
TOOLS_FLOOR = 8192
RETRY_TOKENS = 4096

RESULTS = []
SEEN = []      # 上游收到的请求摘要
PLAN = []      # 每次 POST 弹出一项，决定 stub 这次怎么回


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

    def _reply(self, body: bytes, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n)
        req = json.loads(raw.decode() or "{}")
        SEEN.append({
            "max_tokens": req.get("max_tokens"),
            "tools": bool(req.get("tools") or req.get("functions")),
            "stream": bool(req.get("stream")),
        })
        spec = PLAN.pop(0) if PLAN else {"content": "ok"}

        # SSE：模拟"预算烧在 reasoning 上、正文为空"的流式响应
        if req.get("stream"):
            chunk = {
                "id": "chatcmpl-stub", "object": "chat.completion.chunk", "model": MODEL,
                "choices": [{"index": 0, "delta": {"role": "assistant",
                                                   "content": spec.get("content", "")},
                             "finish_reason": spec.get("finish", "stop")}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            }
            body = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                    "data: [DONE]\n\n").encode("utf-8")
            self._reply(body, "text/event-stream")
            return

        msg = {"role": "assistant", "content": spec.get("content", "")}
        if spec.get("reasoning"):
            msg["reasoning"] = spec["reasoning"]
        body = json.dumps({
            "id": "chatcmpl-stub", "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": spec.get("finish", "stop")}],
            "usage": {"prompt_tokens": 10,
                      "completion_tokens": spec.get("completion", 1),
                      "total_tokens": 11},
        }).encode()
        self._reply(body)


def mk_cfg(port):
    d = tempfile.mkdtemp(prefix="gw-reasoning-")
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
    env.update({
        "TG_DISABLE_HTTPS": "1", "NO_PROXY": "*",
        "TG_CONFIG_DIR": d, "TG_LOG_DIR": d + "/logs",
    })
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
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", {}


TEXT = [{"role": "user", "content": "用一句话说明什么是TCP握手"}]
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]
IMAGE = [{"role": "user", "content": [
    {"type": "text", "text": "这张图里是什么？"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
]}]
EMPTY_REASONING = {"content": "", "reasoning": "思" * 300, "finish": "length", "completion": 64}


def body_of(resp_text):
    try:
        return json.loads(resp_text)
    except Exception:
        return {}


def main():
    if not os.path.exists(SRC):
        print(f"找不到被测文件: {SRC}")
        return 2

    print(f"== 被测: {SRC} ==")

    # ---------- A. Ollama 侧（含预算下限与空正文修复） ----------
    stub_port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dead = free_port()
    port = free_port()
    p, d = start_gw(port, {
        "TG_OLLAMA_HOST": f"http://127.0.0.1:{stub_port}",
        "TG_RAPID_MLX_HOSTS": f"http://127.0.0.1:{dead}",
        "TG_RAPID_MLX_VLM_HOSTS": "",
    })
    try:
        print("\n-- 1. Ollama 纯文本 max_tokens=64 → 抬到文本下限 --")
        SEEN.clear(); PLAN.clear()
        st, txt, _ = chat(port, TEXT, {"max_tokens": 64})
        check("状态 200", st == 200, str(st))
        check(f"上游收到 max_tokens={TEXT_FLOOR}", SEEN and SEEN[-1]["max_tokens"] == TEXT_FLOOR,
              str([s["max_tokens"] for s in SEEN]))
        log = open(d + "/srv.log", errors="replace").read()
        check("日志说明原因（文本输出下限）", "文本输出下限" in log,
              [l.strip() for l in log.splitlines() if "提升 max_tokens" in l][:1])

        print("\n-- 2. Ollama 纯文本 + tools max_tokens=64 → 抬到工具下限 --")
        SEEN.clear(); PLAN.clear()
        st, _, _ = chat(port, TEXT, {"max_tokens": 64, "tools": TOOLS})
        check("状态 200", st == 200, str(st))
        check(f"上游收到 max_tokens={TOOLS_FLOOR}", SEEN and SEEN[-1]["max_tokens"] == TOOLS_FLOOR,
              str([s["max_tokens"] for s in SEEN]))

        print("\n-- 3. Ollama 带图 max_tokens=64 → 维持豁免 --")
        SEEN.clear(); PLAN.clear()
        st, _, _ = chat(port, IMAGE, {"max_tokens": 64})
        check("状态 200", st == 200, str(st))
        check("上游仍收到 64", SEEN and SEEN[-1]["max_tokens"] == 64,
              str([s["max_tokens"] for s in SEEN]))

        print("\n-- 4. Ollama 纯文本 max_tokens=16384 → 已高于下限，不动 --")
        SEEN.clear(); PLAN.clear()
        st, _, _ = chat(port, TEXT, {"max_tokens": 16384})
        check("状态 200", st == 200, str(st))
        check("上游仍收到 16384", SEEN and SEEN[-1]["max_tokens"] == 16384,
              str([s["max_tokens"] for s in SEEN]))

        print("\n-- 5. 空正文 + reasoning + length → 重发一次并返回正文 --")
        SEEN.clear()
        PLAN[:] = [dict(EMPTY_REASONING), {"content": "TCP握手是三次确认过程", "finish": "stop"}]
        st, txt, hdrs = chat(port, TEXT, {"max_tokens": 64})
        check("状态 200", st == 200, str(st))
        got = (body_of(txt).get("choices") or [{}])[0].get("message", {}).get("content")
        check("客户端拿到正文", got == "TCP握手是三次确认过程", repr(got)[:60])
        check("上游被调用 2 次", len(SEEN) == 2, str(len(SEEN)))
        # 重发预算 = max(TG_REASONING_RETRY_TOKENS, 本次生效预算 × 4)。
        # 注意"本次生效预算"是**抬高之后**的值（64 已被抬到 2048），所以这里是 8192。
        expect_retry = max(RETRY_TOKENS, TEXT_FLOOR * 4)
        check(f"重发预算 = {expect_retry}（4× 生效预算）",
              len(SEEN) == 2 and SEEN[1]["max_tokens"] == expect_retry,
              str([s["max_tokens"] for s in SEEN]))
        check(f"响应头 X-Gateway-Reasoning-Retry={expect_retry}",
              str(hdrs.get("X-Gateway-Reasoning-Retry")) == str(expect_retry),
              str(hdrs.get("X-Gateway-Reasoning-Retry")))
        log = open(d + "/srv.log", errors="replace").read()
        check("日志记录重发与成功", "推理模型空正文" in log and "重发成功" in log)

        print("\n-- 6. 重发仍为空 → 只重发一次，不循环 --")
        SEEN.clear()
        PLAN[:] = [dict(EMPTY_REASONING), dict(EMPTY_REASONING)]
        st, txt, _ = chat(port, TEXT, {"max_tokens": 64})
        check("状态 200", st == 200, str(st))
        check("上游恰好 2 次（不循环）", len(SEEN) == 2, str(len(SEEN)))
        got = (body_of(txt).get("choices") or [{}])[0].get("message", {}).get("content")
        check("透传原响应（空正文，不编造）", got == "", repr(got)[:40])

        print("\n-- 7. 流式响应 → 不重发 --")
        SEEN.clear()
        PLAN[:] = [dict(EMPTY_REASONING)]
        st, txt, _ = chat(port, TEXT, {"max_tokens": 64, "stream": True})
        check("状态 200", st == 200, str(st))
        check("上游只被调用 1 次", len(SEEN) == 1, str(len(SEEN)))
        check("返回 SSE 分片", "data:" in txt, txt[:60])
    finally:
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
        srv.shutdown()
        shutil.rmtree(d, ignore_errors=True)

    # ---------- B. Rapid-MLX 既有语义不变 ----------
    stub2 = free_port()
    srv2 = ThreadingHTTPServer(("127.0.0.1", stub2), Stub)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    port2 = free_port()
    p2, d2 = start_gw(port2, {
        "TG_OLLAMA_HOST": f"http://127.0.0.1:{free_port()}",   # 死端口：路由必须走 Rapid
        "TG_RAPID_MLX_HOSTS": f"http://127.0.0.1:{stub2}",
        "TG_RAPID_MLX_VLM_HOSTS": f"http://127.0.0.1:{stub2}",
        "TG_MIN_MAX_TOKENS": "0",
    })
    try:
        print("\n-- 8. Rapid-MLX(VLM) 纯文本 → 仍不抬（既有语义） --")
        SEEN.clear(); PLAN.clear()
        st, _, _ = chat(port2, TEXT, {"max_tokens": 64})
        check("状态 200", st == 200, str(st))
        check("上游仍收到 64", SEEN and SEEN[-1]["max_tokens"] == 64,
              str([s["max_tokens"] for s in SEEN]))

        print("\n-- 9. Rapid-MLX(VLM) 纯文本 + tools → 仍抬到工具下限 --")
        SEEN.clear(); PLAN.clear()
        st, _, _ = chat(port2, TEXT, {"max_tokens": 64, "tools": TOOLS})
        check("状态 200", st == 200, str(st))
        check(f"上游收到 {TOOLS_FLOOR}", SEEN and SEEN[-1]["max_tokens"] == TOOLS_FLOOR,
              str([s["max_tokens"] for s in SEEN]))
    finally:
        p2.terminate()
        try:
            p2.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p2.kill()
        srv2.shutdown()
        shutil.rmtree(d2, ignore_errors=True)

    print("\n== 汇总 ==")
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
