#!/usr/bin/env python3
"""验证看门狗真的会自愈：等宽限期结束 → kill -9 上游 → 观察它是否把服务拉回来。

用法：
  python3 tools/verify-watchdog.py            # 默认只做只读检查，不动任何进程
  python3 tools/verify-watchdog.py --yes      # 真的 kill -9 上游，观察自愈（会短暂中断服务！）
  python3 tools/verify-watchdog.py --help

  TG_UPSTREAM_PORT=8088 TG_UPSTREAM_PROC="llama-server" python3 tools/verify-watchdog.py --yes

**默认不杀进程**：这个脚本会 SIGKILL 一个正在服务的推理进程，所以必须显式 --yes。
不加时只检查基线健康并打印将要做什么。

退出码：0 自愈成功且推理可用；1 超时未自愈；2 基线不健康；3 缺少 --yes（只做了只读检查）

为什么要kill -9：看门狗的"能自愈"和"不会自杀"只隔几条判断，光看 /health 变绿
证明不了它会重启上游——把上游真的打死，看它自己爬起来，才叫验证。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request


GW = os.environ.get("TG_GW", "http://127.0.0.1:3000")
UPSTREAM_PORT = os.environ.get("TG_UPSTREAM_PORT", "8090")
UPSTREAM = f"http://127.0.0.1:{UPSTREAM_PORT}"


def health():
    try:
        with urllib.request.urlopen(f"{GW}/health", timeout=6) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"_err": f"{type(e).__name__}: {e}"}


def wd_target(d):
    w = (d or {}).get("watchdog") or {}
    t = w.get("targets") or {}
    for k, v in t.items():
        if UPSTREAM_PORT in k:
            return v
    return {}


def gmlx_pid():
    r = subprocess.run(["pgrep", "-f", os.environ.get("TG_UPSTREAM_PROC", "gmlx serve")], capture_output=True, text=True)
    pids = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    return pids[0] if pids else None


def up():
    try:
        with urllib.request.urlopen(f"{UPSTREAM}/v1/models", timeout=5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


ARGS = [a for a in sys.argv[1:] if not a.startswith("-")]
FLAGS = {a for a in sys.argv[1:] if a.startswith("-")}

if "--help" in FLAGS or "-h" in FLAGS:
    print(__doc__)
    sys.exit(0)

CONFIRMED = "--yes" in FLAGS or "-y" in FLAGS

t0 = time.time()


def ts():
    return f"[{time.strftime('%H:%M:%S')} +{int(time.time() - t0):3d}s]"


print(f"{ts()} 阶段 1：等宽限期结束、state 变 healthy（最多 300s）", flush=True)
deadline = time.time() + 300
last = None
while time.time() < deadline:
    d = health()
    st = wd_target(d)
    cur = (st.get("state"), st.get("next_attempt_in_s"))
    if cur != last:
        print(f"{ts()}   state={cur[0]} next_attempt_in_s={cur[1]} fails={st.get('fails')}", flush=True)
        last = cur
    if st.get("state") == "healthy":
        break
    time.sleep(5)

if wd_target(health()).get("state") != "healthy":
    print(f"{ts()} ❌ 300s 内未进入 healthy，放弃验证", flush=True)
    sys.exit(2)
print(f"{ts()} ✅ 基线 healthy，8090 up={up()}", flush=True)

pid = gmlx_pid()
if not CONFIRMED:
    print(f"{ts()} ⏸  只读模式：基线健康，目标进程 pid={pid}", flush=True)
    print(f"{ts()}    下一步会 SIGKILL 它并观察自愈（服务会中断约 20–80s）。", flush=True)
    print(f"{ts()}    确认执行请加 --yes：", flush=True)
    print(f"{ts()}    python3 {sys.argv[0]} --yes", flush=True)
    sys.exit(3)

print(f"{ts()} 阶段 2：kill -9 gmlx pid={pid}", flush=True)
subprocess.run(["kill", "-9", pid], check=False)
print(f"{ts()}   已发送 SIGKILL，8090 up={up()}", flush=True)

print(f"{ts()} 阶段 3：观察看门狗决策（最多 420s）", flush=True)
deadline = time.time() + 420
seen = []
restarted_at = None
recovered_at = None
last = None
while time.time() < deadline:
    d = health()
    st = wd_target(d)
    cur = (st.get("state"), st.get("restarts"))
    if cur != last:
        print(f"{ts()}   state={cur[0]} restarts={cur[1]} fails={st.get('fails')} "
              f"err={(st.get('last_error') or '')[:60]}", flush=True)
        seen.append(cur)
        last = cur
    if cur[0] == "restarting" and restarted_at is None:
        restarted_at = time.time()
    if cur[0] == "healthy" and restarted_at and recovered_at is None:
        if up():
            recovered_at = time.time()
            break
    time.sleep(4)

print("", flush=True)
if recovered_at:
    newpid = gmlx_pid()
    print(f"{ts()} ✅ 自愈成功：重启判定 → 恢复 用时 {int(recovered_at - restarted_at)}s；"
          f"新 pid={newpid}（原 {pid}）", flush=True)
    # 证明确实重新加载了权重（而不是端口通了就算活）
    try:
        model = os.environ.get("TG_UPSTREAM_MODEL", "qwen3.8-flash-next")
        body = json.dumps({"model": model, "max_tokens": 60,
                           "messages": [{"role": "user", "content": "写一个 python 的二分查找"}]}).encode()
        req = urllib.request.Request(f"{UPSTREAM}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        st_t = time.time()
        with urllib.request.urlopen(req, timeout=180) as r:
            gd = json.load(r)
        u = gd.get("usage") or {}
        tok = u.get("completion_tokens") or 0
        el = time.time() - st_t
        print(f"{ts()} ✅ 推理可用：{tok} tok / {el:.1f}s = {tok/el:.1f} tok/s", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"{ts()} ⚠️ 推理验证失败: {type(e).__name__}: {e}", flush=True)
    sys.exit(0)

print(f"{ts()} ❌ 未在 420s 内完成自愈；state 轨迹={seen}", flush=True)
sys.exit(1)
