#!/usr/bin/env python3
"""拉起一个**专属网关用**的 Ollama 实例（默认 :11436），带 NUM_PARALLEL=2。

为什么要单独一个实例，而不是改系统那个 Ollama：
  · 系统那个（:11434）由 Ollama.app 托管，`launchctl list` 里根本没有对应作业，
    没有可靠的地方写环境变量；重启它还会把用户自己的会话一起掐掉。
  · `OLLAMA_NUM_PARALLEL` 只认环境变量（没有配置文件可写），必须换进程才生效。

为什么需要 NUM_PARALLEL=2：默认是 1，两个请求进同一个模型是**排队**而不是合批：
实测两并发 2.36s + 4.33s（墙钟 4.33s，等于串行 2.38×2）。
设成 2 之后两条各 3.30s（墙钟 3.30s，同一条泳道 1.44x），因为 gpt-oss 是
MoE（激活 3.6B），合批几乎不涨计算量。

两者关系：这个实例只是**多一条泳道容量**（网关里 gptoss 那条 cap=2），
不是替代 :11434 —— App 那个继续服务别的东西。

用法：
  python3 ~/.ollama-token-gateway/tools/start-ollama-lane.py            # 起（已起则只报告）
  python3 ~/.ollama-token-gateway/tools/start-ollama-lane.py --restart  # 强制重启
  python3 ~/.ollama-token-gateway/tools/start-ollama-lane.py --status   # 只看状态
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

OLLAMA_BIN = os.environ.get(
    "TG_OLLAMA_BIN", "/Applications/Ollama.app/Contents/Resources/ollama")
PORT = int(os.environ.get("TG_LANE_OLLAMA_PORT", "11436"))
NUM_PARALLEL = os.environ.get("TG_LANE_OLLAMA_PARALLEL", "2")
KEEP_ALIVE = os.environ.get("TG_LANE_OLLAMA_KEEPALIVE", "30m")
LOG = os.path.expanduser("~/.ollama-token-gateway/logs/ollama-lane.log")


def pids_on(port: int):
    """监听该端口的 pid。本机 ps 被限制，lsof 可用。"""
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout.split()
    return [int(x) for x in out if x.strip().isdigit()]


def api(path: str, timeout=2.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def status() -> int:
    ver = api("/api/version")
    if not ver:
        print(f"❌ :{PORT} 无响应（实例未运行）")
        return 1
    print(f"✅ :{PORT} ollama {ver.get('version')}  pid={pids_on(PORT)}")
    ps = api("/api/ps") or {}
    for m in ps.get("models") or []:
        print(f"   {m['name']}  {m['size'] / 1e9:.1f} GB  ctx={m.get('context_length')} "
              f"until={m.get('expires_at')}")
    if not ps.get("models"):
        print("   （当前没有模型驻留）")
    return 0


def wait_up(deadline_s=45.0) -> bool:
    end = time.time() + deadline_s
    while time.time() < end:
        if api("/api/version"):
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    if "--status" in sys.argv:
        return status()

    already = api("/api/version") is not None
    if already and "--restart" not in sys.argv:
        print(f"ℹ️  :{PORT} 已在运行，无需重启（--restart 可强制）")
        return status()

    if already:
        for pid in pids_on(PORT):
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"   SIGTERM {pid}")
            except ProcessLookupError:
                pass
        for _ in range(40):
            if not pids_on(PORT):
                break
            time.sleep(0.25)
        else:
            for pid in pids_on(PORT):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.5)

    env = dict(os.environ)
    env.update({
        "OLLAMA_HOST": f"127.0.0.1:{PORT}",
        "OLLAMA_NUM_PARALLEL": str(NUM_PARALLEL),
        "OLLAMA_MAX_LOADED_MODELS": env.get("OLLAMA_MAX_LOADED_MODELS", "1"),
        "OLLAMA_KEEP_ALIVE": KEEP_ALIVE,
        "NO_PROXY": "*", "no_proxy": "*",          # 本机回环别走代理
    })
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    logf = open(LOG, "ab")
    logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start "
               f"(port={PORT} parallel={NUM_PARALLEL} keep_alive={KEEP_ALIVE}) =====\n".encode())
    logf.flush()
    # start_new_session：脱离本会话的进程组，工具调用结束不会把它带走
    proc = subprocess.Popen([OLLAMA_BIN, "serve"], env=env,
                            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                            start_new_session=True, cwd=os.path.expanduser("~"))
    print(f"   launched pid={proc.pid} log={LOG}")
    if not wait_up():
        print(f"❌ 45s 内 :{PORT} 未就绪，看 {LOG}")
        return 1
    ver = api("/api/version") or {}
    print(f"✅ :{PORT} 就绪（ollama {ver.get('version')}, parallel={NUM_PARALLEL}）")
    print(f"   监听 pid={pids_on(PORT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
