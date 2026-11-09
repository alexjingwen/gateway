#!/usr/bin/env python3
"""回归：Flash 运行时看门狗（WG 内嵌后台线程）。

背景（2026-11-09）：
  本机 launchd 完全不可用（bootstrap/load 一律 `5: Input/output error`，最小 plist
  也失败），gmlx 与网关都是脚本强制启动的，推理进程崩了没人接管。看门狗因此内嵌进
  网关的后台线程：网关常驻，它活着就有人巡检。

  但"能自愈"和"不会自杀"之间只隔几条判断，所以这里把决策矩阵逐格钉死：
    ① 健康 → healthy，计数归零
    ② 端口没了但还没到阈值 → down，只观察
    ③ 到阈值 → 调用重启脚本（本测试 patch 掉真实重启）
    ④ 刚重启过（宽限期内）→ starting，即使仍然探不到也不再重启
       —— gmlx 加载 111 GB 权重约 75s，没有这条会把自己反复打断
    ⑤ 端口在监听但一直不响应 → 先 degraded，超过 HUNG_TIMEOUT 才判卡死
    ⑥ 有请求在飞时阈值翻倍 —— 131k 上下文的 prefill 会把服务堵住几十秒，
       这时候杀掉它等于自杀
    ⑦ 恢复后稳定足够久 → 退避计数归零，避免抖动期反复全速重试
    ⑧ /health 暴露巡检摘要（无鉴权路径，只放状态不放敏感信息）

用法：
  python3 tests/test_watchdog.py
"""
import http.server
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import urllib.request

SRC = pathlib.Path(os.environ.get(
    "TG_SRC",
    pathlib.Path(__file__).resolve().parent.parent / "token-gateway.py",
))
CFG = tempfile.mkdtemp(prefix="tg-wd-")
os.environ["TG_CONFIG_DIR"] = CFG
os.environ["TG_LOG_DIR"] = os.path.join(CFG, "logs")
os.environ["TG_WD_ENABLED"] = "1"
os.environ.pop("TG_WD_TARGETS", None)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        FAILS.append(name)


def load(**env):
    """按指定的环境变量重新加载模块（常量在 import 期解析）。"""
    os.environ.pop("TG_WD_TARGETS", None)
    for k, v in env.items():
        os.environ[k] = str(v)
    spec = importlib.util.spec_from_file_location("tg_wd", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Base(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def serve(status=200):
    h = type("H", (_Base,), {"status": status})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def closed_port():
    s = __import__("socket").socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def new_state(tg, base, **kw):
    st = {"state": "init", "fails": 0, "first_fail": 0.0, "restarts": 0,
          "last_check": 0.0, "last_ok": 0.0, "last_error": None,
          "next_attempt": 0.0, "healthy_since": 0.0}
    st.update(kw)
    tg._wd_states[base] = st
    return st


print("== 1. 目标解析 ==")
tg = load()
check("无配置时无目标（不巡空端口）", tg._wd_targets() == [], tg._wd_targets())
with open(os.path.join(CFG, "flash-runtime.json"), "w") as f:
    json.dump({"port": 9999}, f)
check("跟随 flash-runtime.json 的端口", tg._wd_targets() == ["http://127.0.0.1:9999"], tg._wd_targets())
tg = load(TG_WD_TARGETS="http://127.0.0.1:8090/,10.0.0.5:9000")
check("显式配置优先且归一化（去尾斜杠）",
      tg._wd_targets() == ["http://127.0.0.1:8090", "10.0.0.5:9000"], tg._wd_targets())

print("\n== 2. 地址解析与连通性 ==")
check("带 scheme", tg._wd_split("http://127.0.0.1:8090") == ("127.0.0.1", 8090))
check("不带 scheme", tg._wd_split("127.0.0.1:9001") == ("127.0.0.1", 9001))
check("https 默认 443", tg._wd_split("https://example.com")[1] == 443)
srv_ok, p_ok = serve(200)
srv_bad, p_bad = serve(500)
check("TCP 活性：在听 → True", tg._wd_tcp_alive("127.0.0.1", p_ok) is True)
check("TCP 活性：无监听 → False", tg._wd_tcp_alive("127.0.0.1", closed_port(), timeout=0.5) is False)
ok, err = tg._wd_probe(f"http://127.0.0.1:{p_ok}")
check("探测 200 → 健康", ok is True and err is None)
ok, err = tg._wd_probe(f"http://127.0.0.1:{p_bad}")
check("探测 500 → 不健康", ok is False and "500" in (err or ""), err)
ok, err = tg._wd_probe(f"http://127.0.0.1:{closed_port()}")
check("探测拒绝连接 → 不健康", ok is False and err, err)

print("\n== 3. 决策矩阵 ==")
base_ok = f"http://127.0.0.1:{p_ok}"
st = new_state(tg, base_ok)
tg._wd_tick(base_ok)
check("健康 → healthy 且 fails=0", st["state"] == "healthy" and st["fails"] == 0, st)

base_dead = f"http://127.0.0.1:{closed_port()}"
CALLS = []
tg._wd_restart = lambda base, s: (CALLS.append(base), True)[1]
tg.WD_FAIL_THRESHOLD = 2
st = new_state(tg, base_dead)
tg._wd_tick(base_dead)
check("第 1 次失败（未到阈值）→ down，不重启", st["state"] == "down" and not CALLS, st)
tg._wd_tick(base_dead)
check("第 2 次失败（到阈值）→ 触发重启", st["state"] == "restarting" and CALLS == [base_dead], (st["state"], CALLS))
check("重启后 restarts=1 且设置了下一次允许时间",
      st["restarts"] == 1 and st["next_attempt"] > time.time(), st)
tg._wd_tick(base_dead)
check("宽限期内即使仍探不到 → starting，不重复重启", st["state"] == "starting" and len(CALLS) == 1,
      (st["state"], CALLS))

base_hang = f"http://127.0.0.1:{p_bad}"
tg.WD_HUNG_TIMEOUT_S = 300
CALLS.clear()
st = new_state(tg, base_hang)
tg._wd_tick(base_hang)
check("端口在但 500 → degraded，不重启", st["state"] == "degraded" and not CALLS, st)
st["first_fail"] = time.time() - 1000
CALLS.clear()
tg._wd_tick(base_hang)
check("持续不响应超时 → 判定卡死并重启", st["state"] == "restarting" and CALLS == [base_hang], (st["state"], CALLS))

CALLS.clear()
tg.WD_FAIL_THRESHOLD = 2
st = new_state(tg, base_dead, fails=2)
tg._model_inflight["qwen3.8-flash-next"] = 1
try:
    tg._wd_tick(base_dead)
finally:
    tg._model_inflight.clear()
check("有请求在飞 → 阈值翻倍（2 次失败不重启）", st["state"] == "down" and not CALLS, (st["state"], CALLS))

st = new_state(tg, base_ok, state="down", fails=5, restarts=2, healthy_since=time.time() - 1000)
tg.WD_STABLE_RESET_S = 300
tg._wd_tick(base_ok)
check("恢复 → healthy 且 fails 归零", st["state"] == "healthy" and st["fails"] == 0, st)
check("稳定足够久 → 退避计数归零", st["restarts"] == 0, st)

st = new_state(tg, base_ok, restarts=3, healthy_since=time.time() - 10)
tg._wd_tick(base_ok)
check("刚恢复不久 → 保留退避计数（防抖动）", st["restarts"] == 3, st)

print("\n== 4. /health 快照 ==")
snap = tg._wd_snapshot()
check("enabled=True 且带间隔", snap["enabled"] is True and snap["interval_s"] == tg.WD_INTERVAL_S, snap)
check("包含巡检目标与状态字段",
      base_ok in snap["targets"] and {"state", "fails", "restarts", "last_error"} <= set(snap["targets"][base_ok]),
      snap["targets"].get(base_ok))
tg.WD_ENABLED = False
check("关闭时不暴露任何目标", tg._wd_snapshot() == {"enabled": False})
tg.WD_ENABLED = True

print("\n== 5. 端到端：/health 返回 watchdog ==")
srv = tg.ThreadedServer(("127.0.0.1", 0), tg.GatewayHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
        body = json.loads(r.read())
    check("HTTP 200 且 status=ok", body.get("status") == "ok" and r.status == 200, body)
    check("响应内嵌 watchdog 摘要", isinstance(body.get("watchdog"), dict) and body["watchdog"]["enabled"] is True,
          body.get("watchdog"))
    check("保留原有字段（向后兼容）", "uptime" in body and "version" in body, body)
finally:
    srv.shutdown()
    srv.server_close()

srv_ok.shutdown()
srv_bad.shutdown()

print("\n" + "=" * 46)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败: {FAILS}")
    sys.exit(1)
print("✅ 全部通过")
