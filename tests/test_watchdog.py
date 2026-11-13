#!/usr/bin/env python3
"""回归：运行时看门狗（tg_watchdog 独立模块 + 网关集成）。

背景（2026-11-09）：
  上游模型服务是脚本拉起的，崩了没有别的托管方管。看门狗因此挂在网关的后台
  线程上：网关常驻，它活着就有人巡检。

  但"能自愈"和"不会自杀"之间只隔几条判断，所以这里把决策矩阵逐格钉死：
    ① 健康 → healthy，计数归零
    ② 端口没了但还没到阈值 → down，只观察
    ③ 到阈值 → 调用重启脚本（本测试 patch 掉真实重启）
    ④ 刚重启过（宽限期内）→ starting，即使仍然探不到也不再重启
       —— 上游加载 111 GB 权重约 75s，没有这条会把自己反复打断
    ⑤ 端口在监听但一直不响应 → 先 degraded，超过 HUNG_TIMEOUT 才判卡死
    ⑥ 有请求在飞时阈值翻倍 —— 131k 上下文的 prefill 会把服务堵住几十秒，
       这时候杀掉它等于自杀
    ⑦ 恢复后稳定足够久 → 退避计数归零，避免抖动期反复全速重试
    ⑧ /health 暴露巡检摘要（无鉴权路径，只放状态不放敏感信息）
    ⑨ 探测与代理配置解耦 —— 启动时有死代理，健康上游也不能被误判（2b，子进程）
    ⑩ 判死时 healthy_since 必须清零 —— 否则「重启→恢复→又挂」会把退避序列废掉（3b）

  第 1–4 节**完全不加载网关**，直接测 tg_watchdog 模块——这本身就是"能独立复用"
  的证明；第 5 节才加载网关，验证两边接口接得对。

用法：
  python3 tests/test_watchdog.py
  TG_WD_SRC=/path/to/tg_watchdog.py TG_SRC=/path/to/token-gateway.py python3 tests/test_watchdog.py
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

HERE = pathlib.Path(__file__).resolve().parent.parent
WD_SRC = pathlib.Path(os.environ.get("TG_WD_SRC", HERE / "tg_watchdog.py"))
GW_SRC = pathlib.Path(os.environ.get("TG_SRC", HERE / "token-gateway.py"))

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


def load_wd(**env):
    """按指定的环境变量重新加载看门狗模块（常量在 import 期解析）。"""
    os.environ.pop("TG_WD_TARGETS", None)
    for k, v in env.items():
        os.environ[k] = str(v)
    spec = importlib.util.spec_from_file_location("tg_wd_test", WD_SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.configure(config_dir=CFG)
    return m


def load_gw(**env):
    """加载网关模块（集成测试才用）。"""
    for k, v in env.items():
        os.environ[k] = str(v)
    sys.path.insert(0, str(GW_SRC.parent))
    spec = importlib.util.spec_from_file_location("tg_gw_test", GW_SRC)
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


def new_state(wd, base, **kw):
    st = {"state": "init", "fails": 0, "first_fail": 0.0, "restarts": 0,
          "last_check": 0.0, "last_ok": 0.0, "last_error": None,
          "next_attempt": 0.0, "healthy_since": 0.0}
    st.update(kw)
    wd.STATES[base] = st
    return st


print(f"== 0. 模块自足性 ==")
check("tg_watchdog.py 存在", WD_SRC.is_file(), WD_SRC)
src_text = WD_SRC.read_text(encoding="utf-8")
check("不 import 网关（可独立复用）",
      "import token-gateway" not in src_text and "token_gateway" not in src_text)
check("只用标准库", "from __future__" in src_text and "requests" not in src_text)

print("\n== 1. 目标解析 ==")
wd = load_wd()
check("无配置时无目标（不巡空端口）", wd.targets() == [], wd.targets())
with open(os.path.join(CFG, "flash-runtime.json"), "w") as f:
    json.dump({"port": 9999}, f)
check("跟随 flash-runtime.json 的端口", wd.targets() == ["http://127.0.0.1:9999"], wd.targets())
wd = load_wd(TG_WD_TARGETS="http://127.0.0.1:8090/,10.0.0.5:9000")
check("显式配置优先且归一化（去尾斜杠）",
      wd.targets() == ["http://127.0.0.1:8090", "10.0.0.5:9000"], wd.targets())

print("\n== 2. 地址解析与连通性 ==")
check("带 scheme", wd.split("http://127.0.0.1:8090") == ("127.0.0.1", 8090))
check("不带 scheme", wd.split("127.0.0.1:9001") == ("127.0.0.1", 9001))
check("https 默认 443", wd.split("https://example.com")[1] == 443)
srv_ok, p_ok = serve(200)
srv_bad, p_bad = serve(500)
check("TCP 活性：在听 → True", wd.tcp_alive("127.0.0.1", p_ok) is True)
check("TCP 活性：无监听 → False", wd.tcp_alive("127.0.0.1", closed_port(), timeout=0.5) is False)
ok, err = wd.probe(f"http://127.0.0.1:{p_ok}")
check("探测 200 → 健康", ok is True and err is None)
ok, err = wd.probe(f"http://127.0.0.1:{p_bad}")
check("探测 500 → 不健康", ok is False and "500" in (err or ""), err)
ok, err = wd.probe(f"http://127.0.0.1:{closed_port()}")
check("探测拒绝连接 → 不健康", ok is False and err, err)

print("\n== 2b. 探测必须与代理配置解耦 ==")
# 回归（2026-11-12 定位）：probe 曾用裸 `urlopen`，默认 opener 在**首次使用**时读
# HTTP_PROXY 并缓存。一个完全健康的上游，只要进程启动时环境里有死代理就被判不健康 ——
# 看门狗于是重启一个没病的服务，重启完再探还是红，形成「健康时的自杀循环」。当时靠
# plist 里的 NO_PROXY=* 遮蔽，但那是环境约定，漏一次就复发。
#
# 必须用**子进程**才测得出来：默认 opener 的代理表是在第一次 urlopen 时抓取并缓存的，
# 父进程早先的探测已经把它建好了，之后再改环境变量不会生效。生产里 launchd 是在
# 进程启动前注入环境的，正好对应这里「env 先置、模块后加载」的顺序。
_proxy_keys = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
               "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy")
_child = (
    "import importlib.util,sys\n"
    f"spec=importlib.util.spec_from_file_location('wd_child', {str(WD_SRC)!r})\n"
    "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    "ok,err=m.probe(sys.argv[1])\n"
    "print(('HEALTHY' if ok else 'UNHEALTHY'), '|', err)\n"
)


def probe_in_child(url, proxy):
    env = {k: v for k, v in os.environ.items() if k not in _proxy_keys}
    env["HTTP_PROXY"] = env["http_proxy"] = proxy
    env["HTTPS_PROXY"] = env["https_proxy"] = proxy
    r = __import__("subprocess").run([sys.executable, "-c", _child, url],
                                     capture_output=True, text=True, timeout=60, env=env)
    return (r.stdout or r.stderr).strip()


out = probe_in_child(f"http://127.0.0.1:{p_ok}", "http://127.0.0.1:1")
check("启动时就有死代理、且无 NO_PROXY → 健康上游仍判健康",
      out.startswith("HEALTHY"), out[-160:])
out = probe_in_child(f"http://127.0.0.1:{p_bad}", "http://127.0.0.1:1")
check("同环境下 500 上游仍判不健康（确实连上了，不是无脑返回健康）",
      out.startswith("UNHEALTHY") and "500" in out, out[-160:])

print("\n== 3. 决策矩阵 ==")
base_ok = f"http://127.0.0.1:{p_ok}"
st = new_state(wd, base_ok)
wd.tick(base_ok)
check("健康 → healthy 且 fails=0", st["state"] == "healthy" and st["fails"] == 0, st)

base_dead = f"http://127.0.0.1:{closed_port()}"
CALLS = []
wd.restart = lambda base, s: (CALLS.append(base), True)[1]
wd.WD_FAIL_THRESHOLD = 2
st = new_state(wd, base_dead)
wd.tick(base_dead)
check("第 1 次失败（未到阈值）→ down，不重启", st["state"] == "down" and not CALLS, st)
wd.tick(base_dead)
check("第 2 次失败（到阈值）→ 触发重启", st["state"] == "restarting" and CALLS == [base_dead], (st["state"], CALLS))
check("重启后 restarts=1 且设置了下一次允许时间",
      st["restarts"] == 1 and st["next_attempt"] > time.time(), st)
wd.tick(base_dead)
check("宽限期内即使仍探不到 → starting，不重复重启", st["state"] == "starting" and len(CALLS) == 1,
      (st["state"], CALLS))

# 回归：宽限期内每 20s 一次探测，失败曾被累加进 fails，150s 后攒到 7~8 次
# 一次性判死 —— 告警时刻恒等于宽限期到期那一刻。改成宽限期内只记 grace_fails。
base_grace = f"http://127.0.0.1:{closed_port()}"
CALLS.clear()
st = new_state(wd, base_grace, next_attempt=time.time() + 150, grace_fails=0)
for _ in range(8):          # 8 次 = 150s / 20s + 1，正是线上两次事件的失败数
    wd.tick(base_grace)
check("宽限期内 8 次探测失败 → fails 仍为 0（不攒判死票）",
      st["fails"] == 0 and not CALLS, (st["fails"], CALLS))
check("宽限期内的失败单记 grace_fails（可见但不判死）",
      st.get("grace_fails") == 8, st.get("grace_fails"))
check("宽限期内不写 first_fail（卡死计时起点必须是真失败）",
      st["first_fail"] == 0.0, st["first_fail"])
st["next_attempt"] = time.time() - 1
wd.tick(base_grace)
check("宽限期到期后第 1 次真失败 → fails=1，仍未到阈值",
      st["fails"] == 1 and st["state"] == "down" and not CALLS,
      (st["fails"], st["state"], CALLS))

base_hang = f"http://127.0.0.1:{p_bad}"
wd.WD_HUNG_TIMEOUT_S = 300
CALLS.clear()
st = new_state(wd, base_hang)
wd.tick(base_hang)
check("端口在但 500 → degraded，不重启", st["state"] == "degraded" and not CALLS, st)
st["first_fail"] = time.time() - 1000
CALLS.clear()
wd.tick(base_hang)
check("持续不响应超时 → 判定卡死并重启", st["state"] == "restarting" and CALLS == [base_hang], (st["state"], CALLS))

CALLS.clear()
wd.WD_FAIL_THRESHOLD = 2
st = new_state(wd, base_dead, fails=2)
wd.configure(inflight_fn=lambda: 1)
try:
    wd.tick(base_dead)
finally:
    wd.configure(inflight_fn=lambda: 0)
check("有请求在飞 → 阈值翻倍（2 次失败不重启）", st["state"] == "down" and not CALLS, (st["state"], CALLS))

st = new_state(wd, base_ok, state="down", fails=5, restarts=2, healthy_since=time.time() - 1000)
wd.WD_STABLE_RESET_S = 300
wd.tick(base_ok)
check("恢复 → healthy 且 fails 归零", st["state"] == "healthy" and st["fails"] == 0, st)
check("稳定足够久 → 退避计数归零", st["restarts"] == 0, st)

st = new_state(wd, base_ok, restarts=3, healthy_since=time.time() - 10)
wd.tick(base_ok)
check("刚恢复不久 → 保留退避计数（防抖动）", st["restarts"] == 3, st)

print("\n== 3b. 退避序列必须扛住「重启 → 恢复 → 又挂」 ==")
# 回归（2026-11-12 定位）：`healthy_since` 曾只写不清 —— 长跑数小时后再崩，恢复的
# 第一探拿几小时前的起点去比 STABLE_RESET_S，瞬间判定「已稳定」并把 restarts 归零，
# 于是退避 300/600/1200/1800 与封顶永不生效，抖动期退化成每 150s 捅一次上游。
# 线上留痕：16:36:58 判死 → 16:37:18 报「已稳定 149972s，重启退避计数归零」。
_alive = {"v": True}
_real_probe = wd.probe
wd.probe = lambda b: (True, None) if _alive["v"] else (False, "ConnectionRefusedError: refused")
try:
    base_flap = f"http://127.0.0.1:{closed_port()}"
    # 关键前提：崩溃之前它已经连续健康 2.7 小时，healthy_since 非 0
    st = new_state(wd, base_flap, state="healthy", healthy_since=time.time() - 10000)
    wd.WD_FAIL_THRESHOLD = 1
    wd.tick(base_flap)
    check("健康态 tick 不改写已存在的 healthy_since",
          st["healthy_since"] > 0 and time.time() - st["healthy_since"] > 9000, st["healthy_since"])
    _alive["v"] = False
    wd.tick(base_flap)
    d1 = st["next_attempt"] - time.time()
    check("第 1 次判死：restarts=1、退避≈150s",
          st["restarts"] == 1 and 140 <= d1 <= 160, (st["restarts"], d1))
    check("判死时 healthy_since 必须归零（否则「稳定期」从崩溃前算起）",
          st["healthy_since"] == 0.0, st["healthy_since"])
    _alive["v"] = True
    st["next_attempt"] = 0
    wd.tick(base_flap)
    check("恢复后首次健康不归零退避计数（关键回归）", st["restarts"] == 1, st["restarts"])
    _alive["v"] = False
    st["next_attempt"] = 0
    wd.tick(base_flap)
    d2 = st["next_attempt"] - time.time()
    check("第 2 次判死：退避升到 ≈300s，而不是重来 150s",
          st["restarts"] == 2 and 290 <= d2 <= 310, (st["restarts"], d2))
finally:
    wd.probe = _real_probe

print("\n== 4. 快照与退避序列 ==")
snap = wd.snapshot()
check("enabled=True 且带间隔", snap["enabled"] is True and snap["interval_s"] == wd.WD_INTERVAL_S, snap)
check("包含巡检目标与状态字段",
      base_ok in snap["targets"] and {"state", "fails", "restarts", "last_error"} <= set(snap["targets"][base_ok]),
      snap["targets"].get(base_ok))
wd.WD_ENABLED = False
check("关闭时不暴露任何目标", wd.snapshot() == {"enabled": False})
wd.WD_ENABLED = True

# 退避：150 → 300 → 600 → 1200 → 1800（封顶）
seq = []
for n in range(1, 7):
    seq.append(min(wd.WD_BACKOFF_BASE_S * (2 ** (n - 1)), wd.WD_BACKOFF_MAX_S))
check("连续重启退避并封顶", seq == [150, 300, 600, 1200, 1800, 1800], seq)

print("\n== 5. 集成：网关侧接得对 ==")
os.environ.pop("TG_WD_TARGETS", None)  # 清掉第 1 节留下的显式目标，回到跟随配置文件
gw = load_gw()
check("网关成功加载 tg_watchdog", gw.wd is not None, getattr(gw, "_WD_IMPORT_ERR", ""))
check("网关暴露 WD_ENABLED", gw.WD_ENABLED is True, gw.WD_ENABLED)
check("网关的巡检目标解析可用", gw._wd_targets() == ["http://127.0.0.1:9999"], gw._wd_targets())
snap2 = gw._wd_snapshot()
check("网关侧快照可用", snap2.get("enabled") is True and "interval_s" in snap2, snap2)
inflight = gw._wd_gw_inflight()
check("在飞计数已接线（返回 int）", isinstance(inflight, int), inflight)

srv = gw.ThreadedServer(("127.0.0.1", 0), gw.GatewayHandler)
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

print("\n== 6. 缺模块时的降级行为 ==")
import subprocess
code = (
    "import sys,importlib.util,pathlib\n"
    f"sys.path.insert(0, {str(GW_SRC.parent)!r})\n"
    "import tg_watchdog\n"
    "sys.modules['tg_watchdog'] = None\n"  # 强制 import 失败
    f"spec=importlib.util.spec_from_file_location('gw_no_wd', {str(GW_SRC)!r})\n"
    "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    "print('wd_is_none=', m.wd is None)\n"
    "print('enabled=', m.WD_ENABLED)\n"
    "print('snapshot=', m._wd_snapshot())\n"
)
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                   env={**os.environ, "TG_CONFIG_DIR": CFG})
check("模块缺失时网关仍能加载（不崩）", r.returncode == 0, r.stderr[-400:])
check("模块缺失时 WD_ENABLED=False", "enabled= False" in r.stdout, r.stdout[-300:])
check("模块缺失时 /health 明确报 reason", "tg_watchdog.py 未加载" in r.stdout, r.stdout[-300:])

srv_ok.shutdown()
srv_bad.shutdown()

print("\n" + "=" * 46)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败: {FAILS}")
    sys.exit(1)
print("✅ 全部通过")
