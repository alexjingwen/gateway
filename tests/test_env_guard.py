#!/usr/bin/env python3
"""环境变量健壮性回归：数值型配置写错时，服务必须**降级而不崩**。

覆盖两类真实事故形态：
  1) 启动期解析失败 → 进程起不来，launchd 反复重启（整站不可用）
  2) 请求路径解析失败 → /health 正常、每个对话请求都 500，日志只有一句
     internal_error，排查方向极易跑偏（本仓库出现过同形态的 500）

分三段验证：
  A. 静态断言：源码不得出现裸 `int(os.environ.get(...))`，强制走 _env_num()
  B. 导入期：把数值型变量全部写坏，断言各常量回退到文档默认值（子进程隔离）
  C. 请求期：带工具请求打到 VLM 组上游（预算下限逻辑所在路径），断言 200、
     回退到 8192、启动横幅显示生效值而非原始字符串

环境变量（可选）：
  TG_PY    解释器，默认当前解释器
  TG_SRC   被测网关源码，默认 ../token-gateway.py
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
from pathlib import Path

PY = os.environ.get("TG_PY") or sys.executable
HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("TG_SRC") or (HERE.parent / "token-gateway.py")).resolve()
KEY = "sk-env-guard"
MODEL = "stub-a:latest"

# 全部故意写坏：带单位、纯字母、空串、零值、越界值
BAD_ENV = {
    "TG_PORT": "abc",
    "TG_QPS": "  ",
    "TG_CONCURRENCY": "x",
    "TG_AUTO_TIER_LIGHT_CHARS": "4oo",
    "TG_AUTO_TIER_DEEP_CHARS": "-5",
    "TG_AUTO_BUSY_THRESHOLD": "0",
    "TG_SPEED_WINDOW": "abc",
    "TG_SPEED_MIN_SAMPLES": "1",
    "TG_SPEED_TAU": "0",
    "TG_TOKENS_PER_CHAR": "0",
    "TG_READ_TIMEOUT": "600s",
    "TG_VLM_READ_TIMEOUT": "abc",
    "TG_CONNECT_TIMEOUT": "",
    "TG_MIN_MAX_TOKENS": "32k",
    "TG_MIN_MAX_TOKENS_TOOLS": "8k",
}

urllib.request.getproxies = lambda: {}

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def write_cfg(root: str, gw_port: int):
    cfg = {
        "keys": {KEY: {"name": "t", "daily_tokens": 0, "monthly_tokens": 0,
                       "qps": 100, "rate_limited": False, "active": True,
                       "created": "2026-11-07"}},
        "global": {"max_concurrency": 8, "allow_models": ["*"], "deny_models": [],
                   "listen_host": "127.0.0.1", "listen_port": gw_port},
        "admin": {"token": ""},
    }
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)


# ---------------- 假上游（同时扮演 Ollama 与 Rapid-MLX 的接口形状） ----------------
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
            self._json(200, {"object": "list", "data": [
                {"id": MODEL, "object": "model", "created": 0, "owned_by": "stub"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self._json(200, {
            "id": "chatcmpl-stub", "object": "chat.completion", "created": 0, "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        })


def start_stub():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def section_a():
    print("\n== A. 静态断言：不得有裸 int()/float() 解析环境变量 ==")
    src_text = SRC.read_text(encoding="utf-8")
    bad = []
    for i, line in enumerate(src_text.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        if "int(os.environ.get(" in s or "float(os.environ.get(" in s:
            bad.append(f"{i}: {s[:70]}")
    check("所有数值型配置都走 _env_num()", not bad, "; ".join(bad[:3]))
    check("_env_num() 已定义", "def _env_num(" in src_text)


PROBE = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("tg", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("__CONST__" + json.dumps({
    "LISTEN_PORT": m.LISTEN_PORT,
    "DEFAULT_QPS": m.DEFAULT_QPS,
    "DEFAULT_MAX_CONCURRENCY": m.DEFAULT_MAX_CONCURRENCY,
    "AUTO_TIER_LIGHT_CHARS": m.AUTO_TIER_LIGHT_CHARS,
    "AUTO_TIER_DEEP_CHARS": m.AUTO_TIER_DEEP_CHARS,
    "AUTO_BUSY_THRESHOLD": m.AUTO_BUSY_THRESHOLD,
    "SPEED_WINDOW": m.SPEED_WINDOW,
    "SPEED_MIN_SAMPLES": m.SPEED_MIN_SAMPLES,
    "SPEED_TAU": m.SPEED_TAU,
    "TOKENS_PER_CHAR": m.TOKENS_PER_CHAR,
}))
'''


def section_b():
    print("\n== B. 导入期：非法值必须回退到文档默认值 ==")
    root = tempfile.mkdtemp(prefix="gw-envguard-imp-")
    write_cfg(root, 3999)
    env = dict(os.environ)
    env.update(BAD_ENV)
    env.update({"TG_CONFIG_DIR": root, "TG_LOG_DIR": root, "TG_DISABLE_HTTPS": "1"})
    try:
        out = subprocess.run([PY, "-c", PROBE, str(SRC)], env=env,
                             capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        check("导入未超时", False)
        shutil.rmtree(root, ignore_errors=True)
        return
    line = next((l for l in out.stdout.splitlines() if l.startswith("__CONST__")), "")
    if not line:
        check("导入成功并输出常量", False, (out.stderr or out.stdout)[-200:])
        shutil.rmtree(root, ignore_errors=True)
        return
    c = json.loads(line[len("__CONST__"):])
    expect = {
        "LISTEN_PORT": 3000,          # "abc" → 默认端口
        "DEFAULT_QPS": 5,             # 空白 → 默认
        "DEFAULT_MAX_CONCURRENCY": 10,  # "x" → 默认
        "AUTO_TIER_LIGHT_CHARS": 400,
        "AUTO_TIER_DEEP_CHARS": 1,    # "-5" → 夹到下限
        "AUTO_BUSY_THRESHOLD": 1,     # "0" → 夹到下限
        "SPEED_WINDOW": 200,
        "SPEED_MIN_SAMPLES": 2,       # "1" → 夹到下限
        "SPEED_TAU": 0.5,             # "0" → 夹到下限
        "TOKENS_PER_CHAR": 0.01,      # "0" → 夹到下限
    }
    for k, v in expect.items():
        got = c.get(k)
        ok = (abs(float(got) - float(v)) < 1e-9) if isinstance(v, float) else got == v
        check(f"{k} 回退为 {v}", ok, f"got={got!r}")
    check("导入期非法值有告警", "非法" in out.stdout + out.stderr)
    shutil.rmtree(root, ignore_errors=True)


def section_c():
    print("\n== C. 请求期：带工具请求打到 VLM 组上游 ==")
    stub, stub_port = start_stub()
    root = tempfile.mkdtemp(prefix="gw-envguard-rt-")
    gw_port = free_port()
    write_cfg(root, gw_port)
    base = f"http://127.0.0.1:{gw_port}"

    env = dict(os.environ)
    env.update(BAD_ENV)
    env.update({
        "TG_CONFIG_DIR": root, "TG_LOG_DIR": root, "TG_DISABLE_HTTPS": "1",
        "NO_PROXY": "*", "TG_HOST": "127.0.0.1", "TG_PORT": str(gw_port),
        "TG_OLLAMA_HOST": f"http://127.0.0.1:{stub_port}",
        # 同一 stub 同时登记为 Rapid-MLX 与 VLM 组：预算下限逻辑正走这条分支
        "TG_RAPID_MLX_HOSTS": f"http://127.0.0.1:{stub_port}",
        "TG_RAPID_MLX_VLM_HOSTS": f"http://127.0.0.1:{stub_port}",
        "TG_MLX_VLM_API_KEY": "sk-stub",
    })
    log_path = os.path.join(root, "gw.log")
    logf = open(log_path, "w")
    p = subprocess.Popen([PY, "-u", str(SRC)], env=env,
                         stdout=logf, stderr=subprocess.STDOUT)

    ready = False
    for _ in range(80):
        if p.poll() is not None:
            break
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.25)
    check("进程未因环境变量非法而退出", p.poll() is None, f"returncode={p.poll()}")
    check("服务已就绪（/health 200）", ready)

    tools = [{"type": "function", "function": {
        "name": "noop", "description": "noop",
        "parameters": {"type": "object", "properties": {}}}}]
    body = {"model": MODEL, "stream": False, "tools": tools, "tool_choice": "auto",
            "messages": [{"role": "user", "content": "hello"}]}
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        method="POST")
    status, err = None, ""
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status = r.status
            r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        err = e.read().decode()[:150]
    except Exception as e:
        err = str(e)[:150]
    check("非法 TG_MIN_MAX_TOKENS_TOOLS 不影响请求（200）", status == 200, f"status={status} {err}")

    time.sleep(0.5)
    log = Path(log_path).read_text(encoding="utf-8", errors="replace")
    raise_line = [l for l in log.splitlines() if "提升 max_tokens" in l]
    check("工具请求预算回退为 8192（不是崩溃、也不是 8k）",
          any("→ 8192" in l for l in raise_line), raise_line[:1])
    check("请求期非法值有告警", "TG_MIN_MAX_TOKENS_TOOLS" in log and "非法" in log)
    banner = [l for l in log.splitlines() if "静默预算" in l]
    check("启动横幅显示生效值 900s（而非原始的 600ss）",
          any("文本 900s" in l for l in banner), banner[:1])
    check("速度参数被夹到下限（tau=0.5s）",
          any("tau=0.5s" in l for l in log.splitlines() if "速度因子" in l))
    check("无 Traceback / internal_error",
          "Traceback" not in log and "internal_error" not in log)

    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        p.kill()
    stub.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def main():
    print(f"被测源码: {SRC}")
    print(f"解释器:   {PY}")
    section_a()
    section_b()
    section_c()
    print(f"\n== 汇总 ==\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
