#!/usr/bin/env python3
"""按 plist 里的环境变量把 token-gateway 拉起来（脱离当前会话）。

为什么需要它：本机 launchd 的 bootstrap/load 一律返回
`5: Input/output error`（连最小 plist 也失败），所以 com.token-gateway.server
和 com.llama.flash-next 这两个 LaunchAgent 实际都用不上。为了不把环境变量
散落在命令行历史里（漏一个就换了一套行为），启动参数一律从 plist 读取，
plist 仍是唯一事实来源；真到 launchd 可用的机器上，直接 bootstrap 即可。

用法：
  python3 ~/.ollama-token-gateway/tools/start-gateway.py            # 起或重启
  python3 ~/.ollama-token-gateway/tools/start-gateway.py --status   # 只看状态
  python3 ~/.ollama-token-gateway/tools/start-gateway.py --dry-run  # 只打印环境

行为：
  · 先从监听端口上找出旧进程并 SIGTERM（优雅退出，等在飞请求落地）
  · 用 plist 的 EnvironmentVariables + ProgramArguments 启动新进程
  · 轮询 /health 直到就绪，打印 PID 与端口
"""
import json
import os
import plistlib
import signal
import socket
import subprocess
import sys
import time
import urllib.request

PLIST = os.path.expanduser("~/Library/LaunchAgents/com.token-gateway.server.plist")
LOG = "/tmp/token-gateway.log"
ERR = "/tmp/token-gateway.err.log"


def load_plist():
    with open(PLIST, "rb") as f:
        return plistlib.load(f)


def pids_on(port: int):
    """找出监听该端口的 pid（lsof 比 ps 更准，且本机 ps 被限制）。"""
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout.split()
    return [int(x) for x in out if x.strip().isdigit()]


def health_ok(port: int, timeout=2.0) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    pl = load_plist()
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (pl.get("EnvironmentVariables") or {}).items()})
    args = pl["ProgramArguments"]
    port = int(env.get("TG_PORT", "3000"))
    host = env.get("TG_HOST", "0.0.0.0")

    if "--dry-run" in sys.argv:
        for k in sorted(k for k in env if k.startswith(("TG_", "NO_PROXY", "no_proxy"))):
            print(f"  {k}={env[k]}")
        print("  argv:", " ".join(args))
        return 0

    old = pids_on(port)
    if "--status" in sys.argv:
        print(f"端口 {port}: pid {old or '无'}｜健康 {'ok' if health_ok(port) else 'unreachable'}")
        return 0

    for pid in old:
        print(f"停止旧进程 {pid} …")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 15
    while time.time() < deadline and pids_on(port):
        time.sleep(0.3)
    leftover = pids_on(port)
    if leftover:
        print(f"⚠️ 旧进程未退出：{leftover}（不强制 kill，请人工确认）")
        return 1

    log = open(LOG, "a")
    err = open(ERR, "a")
    p = subprocess.Popen(args, cwd=pl.get("WorkingDirectory") or None, env=env,
                         stdout=log, stderr=err, start_new_session=True)
    for _ in range(60):
        if health_ok(port):
            print(f"✅ 已启动 pid={p.pid} 监听 {host}:{port}")
            return 0
        if p.poll() is not None:
            print(f"❌ 进程退出 code={p.returncode}，看 {ERR}")
            return 1
        time.sleep(0.5)
    print(f"⚠️ 30s 内 /health 未就绪，pid={p.pid} 仍在跑，请查 {LOG}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
