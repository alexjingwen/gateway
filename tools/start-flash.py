#!/usr/bin/env python3
"""启动 Qwen3.8-Flash-Next 推理服务（gmlx + MTP 投机解码）。

为什么不用 launchd：本机 launchd 的 bootstrap/load 一律返回
`5: Input/output error`（连最小 plist 都失败），属于系统级故障，
因此沿用 start-gateway.py 的做法：脚本负责停旧进程、脱离会话拉起、轮询健康。

用法:
    python3 start-flash.py            # 启动（若已在跑则先停）
    python3 start-flash.py --status   # 只看状态
    python3 start-flash.py --dry-run  # 打印将执行的命令
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONF = os.path.expanduser("~/.ollama-token-gateway/flash-runtime.json")


def load_conf():
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


def port_pids(port):
    r = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True)
    return [int(x) for x in r.stdout.split() if x.strip().isdigit()]


def healthy(port, timeout=3):
    url = f"http://127.0.0.1:{port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def build_env(conf, base=None):
    """启动 gmlx 的环境变量：继承 shell，再叠加配置里的 `env` 段。

    为什么要有这个函数：gmlx 有些关键开关只有 env 入口（例如 APC 的
    检查点步长 GMLX_APC_CKPT_INTERVAL），配置里没有对应键。写在
    flash-runtime.json 的 `env` 段而不是依赖调用者的 shell —— 否则换一个
    终端重启，缓存行为就静默变了，而且没人知道它变过。
    """
    env = dict(os.environ if base is None else base)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    for key, val in (conf.get("env") or {}).items():
        env[str(key)] = str(val)
    return env


def build_cmd(conf):
    gmlx = os.path.expanduser(conf["gmlx"])
    cfg_file = conf.get("config")
    if cfg_file:
        # config 模式：模型清单、profile、投机参数全部在 YAML 里，因此可以同时
        # 驻留多个模型（不同模型各有自己的 BatchGenerator，能真正并行；同模型并发
        # 会触发 gmlx 的 mid-flight 注入缺陷）。host/port/no-auth 仍由
        # flash-runtime.json 给出并作为命令行覆盖，保持单一真相源。
        cmd = [gmlx, "serve", "--config", os.path.expanduser(cfg_file)]
        cmd += ["--host", conf.get("host", "127.0.0.1"), "--port", str(conf.get("port", 8090))]
        if conf.get("no_auth", True):
            cmd += ["--no-auth"]
        cmd += ["--no-menubar"]
        return cmd
    cmd = [gmlx, "serve", os.path.expanduser(conf["model"])]
    if conf.get("mmproj"):
        cmd += ["--mmproj", os.path.expanduser(conf["mmproj"])]
    if conf.get("speculative") and conf.get("draft_gguf"):
        cmd += ["--speculative", "--draft-gguf", os.path.expanduser(conf["draft_gguf"])]
    # 投机宽度上限：并发注入到繁忙 batch 时，MTP draft 张量与目标 batch 宽度不一致
    # 会让整批报 "Cannot broadcast array of shape ..."（两个并发请求即 100% 500）。
    # 限制为 1 行投机可避开该形状冲突；0 或不设 = 不限制（原行为）。
    cap = conf.get("speculative_width_cap")
    if cap is not None:
        cmd += ["--speculative-width-cap", str(int(cap))]
    cmd += ["--host", conf.get("host", "127.0.0.1"), "--port", str(conf.get("port", 8090))]
    if conf.get("no_auth", True):
        cmd += ["--no-auth"]
    if conf.get("thinking"):
        cmd += ["--thinking", conf["thinking"]]
    cmd += ["--no-menubar"]
    return cmd


def stop(port, wait=12):
    pids = port_pids(port)
    if not pids:
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + wait
    while time.time() < deadline:
        if not port_pids(port):
            return True
        time.sleep(0.5)
    for pid in port_pids(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(1)
    return not port_pids(port)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=240, help="等待健康检查的秒数")
    args = ap.parse_args()

    conf = load_conf()
    port = conf.get("port", 8090)

    if args.status:
        ok = healthy(port)
        print(f"端口 {port}: {'健康' if ok else '不可用'} | 进程 {port_pids(port) or '无'}")
        return 0 if ok else 1

    cmd = build_cmd(conf)
    if args.dry_run:
        print(" ".join(cmd))
        return 0

    if port_pids(port):
        print(f"检测到 {port} 已被占用，先停止…")
        stop(port)

    log_path = os.path.expanduser(conf.get("log", "/tmp/gmlx-flash.log"))
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "w")
    env = build_env(conf)

    print(f"启动: {' '.join(cmd[:4])} …")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True, env=env)

    # gmlx serve 会 fork 出真正的服务进程后父进程退出，所以用健康检查判定成功
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if healthy(port):
            pids = port_pids(port)
            print(f"✅ 服务就绪 http://127.0.0.1:{port} (pid {pids}) 日志 {log_path}")
            return 0
        if proc.poll() not in (None, 0):
            print(f"❌ 启动失败，返回码 {proc.returncode}，日志尾部：")
            with open(log_path, encoding="utf-8", errors="replace") as f:
                print("".join(f.readlines()[-20:]))
            return 1
        time.sleep(2)

    print(f"❌ {args.timeout}s 内未通过健康检查，日志 {log_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
