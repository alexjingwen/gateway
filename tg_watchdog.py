#!/usr/bin/env python3
"""tg_watchdog —— 上游模型服务的运行时看门狗（零依赖、可独立复用）

从 token-gateway.py 抽出来的独立实现。之所以值得单独成文件：这套逻辑的难点
不在"探测 + 重启"，而在**不把自己弄死**——冷加载、长 prefill、抖动重试
这三个场景各有一条反直觉的判断（见下方「判据」）。

判据（默认值，均可用环境变量覆盖）：

    连续失败 N 次才动手            默认 3。**有请求在飞时阈值翻倍**——正在服务
                                   用户的进程不该被巡检打断。
    重启后静默观察期              默认 150s。必须大于上游冷启动时间：111 GB
                                   权重约 75s 才 ready，宽限设 30s 会让它判
                                   服务已死、再重启一次，形成自杀循环。
    端口在听但不响应多久判卡死     默认 300s。要覆盖冷加载 + 长 prefill。
    连续重启退避                  150 → 300 → 600 → 1200 → 1800s（封顶）。
    恢复后连续健康多久才清零       默认 300s。避免抖动期反复全速重试。

独立用法：

    import tg_watchdog as wd
    wd.configure(config_dir="/path/to/cfg",          # 读 flash-runtime.json / 放日志
                 inflight_fn=lambda: my_inflight,     # 有多少请求在飞（可不给）
                 log_fn=print)
    wd.loop(wd.targets())                             # 阻塞；或自己调 tick()

不 configure 也能跑：日志走 print、在飞数恒为 0、重启脚本找不到就只记日志。

只依赖标准库。Python 3.9+。
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple


# ─────────────────────────── 参数（import 期解析一次） ───────────────────────────
def _env_num(name: str, default, *, cast=int, minimum=None):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = cast(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and val < minimum:
        return default
    return val


WD_ENABLED = os.environ.get("TG_WD_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
WD_INTERVAL_S = _env_num("TG_WD_INTERVAL", 20, minimum=5)
WD_FAIL_THRESHOLD = _env_num("TG_WD_FAIL_THRESHOLD", 3, minimum=1)
WD_PROBE_TIMEOUT_S = _env_num("TG_WD_PROBE_TIMEOUT", 5.0, cast=float, minimum=1.0)
WD_GRACE_S = _env_num("TG_WD_GRACE", 150, minimum=30)
WD_HUNG_TIMEOUT_S = _env_num("TG_WD_HUNG_TIMEOUT", 300, minimum=60)
WD_BACKOFF_BASE_S = _env_num("TG_WD_BACKOFF_BASE", 150, minimum=30)
WD_BACKOFF_MAX_S = _env_num("TG_WD_BACKOFF_MAX", 1800, minimum=60)
WD_STABLE_RESET_S = _env_num("TG_WD_STABLE_RESET", 300, minimum=60)
WD_RESTART_TIMEOUT_S = _env_num("TG_WD_RESTART_TIMEOUT", 300, minimum=60)

_LOCK = threading.Lock()
STATES: Dict[str, dict] = {}

# 外部依赖，全部可注入；默认值让本模块能独立跑
_CONFIG_DIR: Optional[pathlib.Path] = None
_LOG_FN: Optional[Callable[[str], None]] = None
_INFLIGHT_FN: Optional[Callable[[], int]] = None
_RESTART_SCRIPT: Optional[pathlib.Path] = None


def configure(*, config_dir=None, log_fn=None, inflight_fn=None,
              restart_script=None) -> None:
    """接上宿主项目的四个外部依赖。不调用也能跑（退化模式）。"""
    global _CONFIG_DIR, _LOG_FN, _INFLIGHT_FN, _RESTART_SCRIPT
    if config_dir is not None:
        _CONFIG_DIR = pathlib.Path(config_dir).expanduser()
    if log_fn is not None:
        _LOG_FN = log_fn
    if inflight_fn is not None:
        _INFLIGHT_FN = inflight_fn
    if restart_script is not None:
        _RESTART_SCRIPT = pathlib.Path(restart_script).expanduser()


def log(msg: str) -> None:
    if _LOG_FN is not None:
        _LOG_FN(msg)
        return
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _log_dir() -> pathlib.Path:
    base = _CONFIG_DIR if _CONFIG_DIR is not None else pathlib.Path("/tmp")
    return base / "logs"


# ─────────────────────────────── 探测 ───────────────────────────────
def targets() -> List[str]:
    """巡检目标。显式 TG_WD_TARGETS 优先，否则跟随 <config_dir>/flash-runtime.json 的端口。

    跟随配置文件而不是硬编码端口：换端口只改一处，不会去巡一个空端口。
    """
    raw = (os.environ.get("TG_WD_TARGETS") or "").strip()
    if raw:
        return [t.strip().rstrip("/") for t in raw.split(",") if t.strip()]
    if _CONFIG_DIR is None:
        return []
    try:
        with open(_CONFIG_DIR / "flash-runtime.json", encoding="utf-8") as f:
            port = int(json.load(f).get("port") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    return [f"http://127.0.0.1:{port}"] if port else []


def split(base: str) -> Tuple[str, int]:
    try:
        u = urllib.parse.urlsplit(base if "//" in base else "http://" + base)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        return host, port
    except ValueError:
        return "127.0.0.1", 80


def tcp_alive(host: str, port: int, timeout: float = 1.5) -> bool:
    """端口有没有人在听。用 TCP 连接判断而不是 lsof：不 fork 子进程。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe(base: str) -> Tuple[bool, Optional[str]]:
    """GET {base}/v1/models，2xx 视为健康。返回 (健康, 错误描述)。"""
    try:
        req = urllib.request.Request(
            base + "/v1/models", headers={"User-Agent": "tg-watchdog"})
        with urllib.request.urlopen(req, timeout=WD_PROBE_TIMEOUT_S) as r:
            if 200 <= r.status < 300:
                return True, None
            return False, f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001 —— 探测失败的原因不值得分类
        return False, f"{type(e).__name__}: {e}"


def inflight_total() -> int:
    if _INFLIGHT_FN is None:
        return 0
    try:
        return int(_INFLIGHT_FN())
    except Exception:  # noqa: BLE001 —— 宿主计数坏了不该带崩巡检
        return 0


# ─────────────────────────────── 重启 ───────────────────────────────
def restart(base: str, st: dict) -> bool:
    """拉起上游。默认调 <config_dir>/tools/start-flash.py。

    刻意不在这里重建命令行：启动参数的事实来源是 flash-runtime.json，
    复制一份进看门狗迟早会漂移（症状是「手工起得动、自愈起不动」）。
    """
    script = _RESTART_SCRIPT or (
        (_CONFIG_DIR / "tools" / "start-flash.py") if _CONFIG_DIR is not None else None)
    if script is None or not script.exists():
        log(f"🚑 看门狗想重启 {base}，但找不到启动脚本 {script}")
        return False
    env = dict(os.environ)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    attempt = st["restarts"]
    try:
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "watchdog-restart.log"
        fh = open(log_path, "a", encoding="utf-8")
        fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 重启 {base}"
                 f"（第 {attempt} 次，原因：{st.get('last_error')}）=====\n")
        fh.flush()
        proc = subprocess.Popen(
            [sys.executable, str(script), "--timeout", str(WD_RESTART_TIMEOUT_S)],
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env, close_fds=True)
        fh.close()  # 子进程已继承 fd，父进程这份关掉免得每次重启漏一个句柄
        log(f"🚑 {base} 不可用（{st.get('last_error')}），已拉起重启脚本 pid {proc.pid}"
            f"（第 {attempt} 次，日志 {log_path}）")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"🚑 重启 {base} 失败: {type(e).__name__}: {e}")
        return False


# ─────────────────────────── 状态机（核心） ───────────────────────────
def tick(base: str) -> None:
    st = STATES[base]
    now = time.time()
    host, port = split(base)
    ok, err = probe(base)
    st["last_check"] = now
    if ok:
        if st["state"] not in ("healthy", "init"):
            log(f"✅ {base} 已恢复（{st['state']} → healthy，累计重启 {st['restarts']} 次）")
        st["state"] = "healthy"
        st["fails"] = 0
        st["first_fail"] = 0.0
        st["last_ok"] = now
        st["last_error"] = None
        if not st["healthy_since"]:
            st["healthy_since"] = now
        if st["restarts"] and now - st["healthy_since"] >= WD_STABLE_RESET_S:
            log(f"🐕 {base} 已稳定 {int(now - st['healthy_since'])}s，重启退避计数归零")
            st["restarts"] = 0
        return

    st["last_error"] = err
    st["fails"] += 1
    if not st["first_fail"]:
        st["first_fail"] = now
    # 宽限期 / 退避期：只观察。冷加载期间健康检查本来就是红的
    if now < st["next_attempt"]:
        st["state"] = "starting"
        return

    alive = tcp_alive(host, port)
    # 有请求在飞时不轻易判死：131k 上下文的 prefill 可能把服务堵住几十秒，
    # 这时候杀掉它等于自杀。阈值翻倍，给它把手上活干完的机会
    need = WD_FAIL_THRESHOLD * (2 if inflight_total() else 1)
    if alive:
        if now - st["first_fail"] < WD_HUNG_TIMEOUT_S:
            st["state"] = "degraded"
            return
        reason = f"端口在监听但 {int(now - st['first_fail'])}s 无响应"
    else:
        if st["fails"] < need:
            st["state"] = "down"
            return
        reason = f"端口无监听（连续 {st['fails']} 次探测失败）"

    st["last_error"] = reason
    st["restarts"] += 1
    delay = min(WD_BACKOFF_BASE_S * (2 ** (st["restarts"] - 1)), WD_BACKOFF_MAX_S)
    st["next_attempt"] = now + max(delay, WD_GRACE_S)
    st["fails"] = 0
    st["first_fail"] = 0.0
    st["state"] = "restarting"
    if not restart(base, st):
        st["state"] = "failed"


def init_states(targets_list: List[str], *, grace: Optional[float] = None) -> None:
    now = time.time()
    g = WD_GRACE_S if grace is None else grace
    with _LOCK:
        for base in targets_list:
            STATES[base] = {
                "state": "init", "fails": 0, "first_fail": 0.0, "restarts": 0,
                "last_check": 0.0, "last_ok": 0.0, "last_error": None,
                # 启动后先给一个宽限期：看门狗可能比运行时先起来，此时健康检查本来就红
                "next_attempt": now + g, "healthy_since": 0.0,
            }


def loop(targets_list: List[str], stop_event: Optional[threading.Event] = None) -> None:
    """阻塞巡检。stop_event 便于测试与优雅退出。"""
    init_states(targets_list)
    log(f"🐕 看门狗已启动（每 {WD_INTERVAL_S}s 巡检 {', '.join(targets_list)}，"
        f"失败 {WD_FAIL_THRESHOLD} 次判死 / 卡死 {WD_HUNG_TIMEOUT_S}s）")
    while True:
        if stop_event is not None:
            if stop_event.wait(WD_INTERVAL_S):
                return
        else:
            time.sleep(WD_INTERVAL_S)
        for base in targets_list:
            try:
                tick(base)
            except Exception as e:  # noqa: BLE001 —— 单点异常不该终止整轮巡检
                log(f"⚠️ 看门狗巡检 {base} 异常: {type(e).__name__}: {e}")


def snapshot() -> dict:
    """给 /health 用的巡检快照。不带敏感信息，可在无鉴权路径暴露。"""
    if not WD_ENABLED:
        return {"enabled": False}
    now = time.time()
    with _LOCK:
        items = {b: st for b, st in STATES.items()}
    out = {}
    for base, st in items.items():
        out[base] = {
            "state": st["state"],
            "fails": st["fails"],
            "restarts": st["restarts"],
            "last_check_ago_s": int(now - st["last_check"]) if st["last_check"] else None,
            "last_error": st["last_error"],
            "next_attempt_in_s": int(st["next_attempt"] - now) if st["next_attempt"] > now else 0,
        }
    return {"enabled": True, "interval_s": WD_INTERVAL_S, "targets": out}


if __name__ == "__main__":
    # 独立试用：python3 tg_watchdog.py [http://127.0.0.1:8090]
    tg = sys.argv[1:] or targets()
    if not tg:
        print("没有巡检目标：给一个地址，或设 TG_WD_TARGETS / 提供 config_dir")
        raise SystemExit(2)
    configure(config_dir=os.environ.get("TG_CONFIG_DIR"))
    try:
        loop(tg)
    except KeyboardInterrupt:
        print("\n已停止")
