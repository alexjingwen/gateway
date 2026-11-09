#!/usr/bin/env python3
"""把线上部署回滚到 GitHub 上的某个历史版本 —— 「旧版本备份」的取用端。

前提
----
本项目的每次推送都是**全树快照**（Git Data API sync-tree），每个提交都是一份完整、
可独立还原的代码树；不需要 git 历史链也能取用，也不需要本地 clone。

关键：仓库根 ≠ 线上部署目录
---------------------------
仓库里既有部署件（token-gateway.py、plist），也有文档与测试（README、tests/）；
而它们落到本机的位置分散在三个目录。所以本脚本用显式 MAPPING 表决定「取哪个、
放哪里」，**只碰白名单里的部署文件**，其余仓库文件仅作参考、不落盘。

    仓库路径                            本机真实位置
    token-gateway.py                 →  <LIVE>/token-gateway.py
    tg_watchdog.py                   →  <LIVE>/tg_watchdog.py
    flash-runtime.json               →  ~/.ollama-token-gateway/flash-runtime.json
    com.token-gateway.server.plist   →  ~/Library/LaunchAgents/…
    tools/start-flash.py             →  ~/.ollama-token-gateway/tools/…
    tools/start-gateway.py           →  ~/.ollama-token-gateway/tools/…

用法
----
  python3 tools/rollback.py --list                  # 只读：最近 20 个版本
  python3 tools/rollback.py --to 3d3ca3a2           # 只读：diff（部署件 + 仓库件分开列）
  python3 tools/rollback.py --to 3d3ca3a2 --apply   # 真执行：快照 → 覆盖 → 编译校验 → 重启
  python3 tools/rollback.py --to 3d3ca3a2 --apply --no-restart
  python3 tools/rollback.py --tag v2026.11.09       # 只读：给远端 HEAD 打标签的预览
  python3 tools/rollback.py --tag v2026.11.09 --apply

为什么默认只读
--------------
这个脚本会覆盖正在服务的代码。教训来自 verify-watchdog.py：会改动生产状态的工具，
默认必须只读，动作要显式 --apply。

安全垫（--apply 自动执行）
--------------------------
1. 先把要动的 6 个部署件整份快照到 ~/.ollama-token-gateway/backups/rollback-<ts>/
2. 目标版本里没有、本机却存在的部署件（如新架构的 tg_watchdog.py）→ 移进备份，不删
3. 写盘后 py_compile 校验；失败立刻还原并退出 4
4. 重启后轮询 /health；非 200 只告警（可能有别的启动故障），并打印还原命令

退出码
------
0 成功 / 1 参数错误 / 2 网络或 API 失败 / 3 版本找不到
4 校验失败已自动还原 / 5 只读预览（需要用 --apply）
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = os.environ.get("TG_REPO", "alexjingwen/gateway")
LIVE = Path(os.environ.get("TG_LIVE_DIR") or
            "/Users/claw/Library/Application Support/TRAE SOLO CN/ModularData/"
            "ai-agent/work-mode-projects/6a6e89c7d72e5be37140c8ad")
HOME_DIR = Path(os.environ.get("TG_HOME_DIR") or "~/.ollama-token-gateway").expanduser()
LAUNCH_AGENTS = Path(os.environ.get("TG_LAUNCH_AGENTS")
                     or "~/Library/LaunchAgents").expanduser()
BACKUP_ROOT = HOME_DIR / "backups"
CREDS = Path("~/.git-credentials").expanduser()
HEALTH = os.environ.get("TG_GW", "http://127.0.0.1:3000") + "/health"
LABEL = os.environ.get("TG_LABEL", "com.token-gateway.server")
DOMAIN = os.environ.get("TG_DOMAIN", f"gui/{os.getuid()}")

# 仓库路径 → 本机部署位置。**只有这里列出的文件会被写入**。
MAPPING = {
    "token-gateway.py": LIVE / "token-gateway.py",
    "tg_watchdog.py": LIVE / "tg_watchdog.py",
    "flash-runtime.json": HOME_DIR / "flash-runtime.json",
    "com.token-gateway.server.plist": LAUNCH_AGENTS / "com.token-gateway.server.plist",
    "tools/start-flash.py": HOME_DIR / "tools/start-flash.py",
    "tools/start-gateway.py": HOME_DIR / "tools/start-gateway.py",
}


# ─────────────────────────── GitHub API ───────────────────────────

def _token():
    for line in CREDS.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if "github.com" not in line:
            continue
        rest = line.split("://", 1)[-1]
        if "@" not in rest:
            continue
        cred = rest.split("@", 1)[0]
        if ":" in cred:
            return cred.split(":", 1)[1]
    return None


TOKEN = _token()


def api(path, method="GET", body=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + (TOKEN or ""))
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"API {method} {path} -> HTTP {e.code}\n  {detail}")


def blob_bytes(sha):
    d = api(f"/repos/{REPO}/git/blobs/{sha}")
    if d.get("encoding") == "base64":
        return base64.b64decode(d["content"])
    return d.get("content", "").encode()


def resolve(ref):
    """tag / 短 sha → (commit_sha, tree_sha)。"""
    try:
        r = api(f"/repos/{REPO}/git/ref/tags/{ref}")
        obj = r["object"]
        if obj["type"] == "tag":
            obj = api(f"/repos/{REPO}/git/tags/{obj['sha']}")["object"]
        c = api(f"/repos/{REPO}/git/commits/{obj['sha']}")
        return obj["sha"], c["tree"]["sha"]
    except SystemExit:
        pass
    for c in api(f"/repos/{REPO}/commits?per_page=100"):
        if c["sha"].startswith(ref):
            d = api(f"/repos/{REPO}/git/commits/{c['sha']}")
            return c["sha"], d["tree"]["sha"]
    raise SystemExit(3)


def remote_tree(tree_sha):
    t = api(f"/repos/{REPO}/git/trees/{tree_sha}?recursive=1")
    return {e["path"]: e["sha"] for e in (t.get("tree") or []) if e["type"] == "blob"}


# ─────────────────────────── 只读 ───────────────────────────

def cmd_list(limit):
    commits = api(f"/repos/{REPO}/commits?per_page={min(int(limit), 100)}")
    print(f"{REPO} —— 最近 {len(commits)} 个版本（新 → 旧）\n")
    print(f"{'sha':10} {'日期':11} 提交信息")
    for c in commits:
        d = c["commit"]["committer"]["date"][:10]
        print(f"{c['sha'][:8]:10} {d:11} {c['commit']['message'].splitlines()[0][:64]}")
    print("\n  python3 tools/rollback.py --to <sha>            # 先看 diff")
    print("  python3 tools/rollback.py --to <sha> --apply    # 真执行")


def cmd_diff(ref):
    sha, tree = resolve(ref)
    meta = api(f"/repos/{REPO}/git/commits/{sha}")
    print(f"目标版本 {sha[:8]}  {meta['committer']['date']}")
    print(f"  {meta['message'].splitlines()[0]}\n")
    files = remote_tree(tree)

    rows, only_remote, gone = [], [], []
    for path, dest in MAPPING.items():
        if path not in files:
            if dest.exists():
                gone.append((path, dest))
            continue
        new = blob_bytes(files[path])
        if not dest.exists():
            rows.append(("新增", path, dest, len(new)))
        elif dest.read_bytes() == new:
            rows.append(("未变", path, dest, len(new)))
        else:
            rows.append(("修改", path, dest, len(new)))
    for path in sorted(files):
        if path not in MAPPING:
            only_remote.append(path)

    print(f"部署件（会被写入）：{len(rows)} 个")
    for kind, path, dest, size in rows:
        mark = {"新增": "+", "修改": "~", "未变": "="}[kind]
        print(f"  {mark} {kind}  {path:34} → {dest}  ({size} B)")
    if gone:
        print(f"\n目标版本没有、本机却存在的部署件 {len(gone)} 个（--apply 时移进备份，不删）：")
        for path, dest in gone:
            print(f"  - 移出  {path:34} → {dest}")
    print(f"\n仓库其余文件 {len(only_remote)} 个：仅作参考，不落到本机"
          f"（README / tests / LICENSE / config.example.json 等）")
    print("\n以上为只读预览。确认无误后加 --apply 执行。")
    return 5


# ─────────────────────────── 写入 ───────────────────────────

def snapshot(paths, tag):
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_ROOT / f"rollback-{datetime.now():%Y%m%d-%H%M%S}-{tag}"
    dst.mkdir(parents=True)
    saved = []
    for path, dest in paths:
        if dest.exists():
            t = dst / path.replace("/", "__")
            shutil.copy2(dest, t)
            saved.append(t)
    return dst, saved


def verify(dests):
    """内存里编译，避免在部署目录留下 __pycache__。"""
    bad = []
    for p in dests:
        if p.suffix != ".py":
            continue
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as e:
            bad.append((p, f"line {e.lineno}: {e.msg}"))
        except Exception as e:  # noqa: BLE001
            bad.append((p, f"{type(e).__name__}: {e}"))
    return bad


def health_ok(timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(HEALTH, timeout=5) as r:
                if r.status == 200:
                    return True, json.load(r)
        except Exception:
            pass
        time.sleep(2)
    return False, None


def restart():
    r = subprocess.run(["launchctl", "kickstart", "-k", f"{DOMAIN}/{LABEL}"],
                       capture_output=True, text=True)
    return r.returncode, (r.stderr or r.stdout).strip()


def cmd_apply(ref, do_restart=True):
    sha, tree = resolve(ref)
    meta = api(f"/repos/{REPO}/git/commits/{sha}")
    files = remote_tree(tree)
    print(f"目标版本 {sha[:8]}  {meta['committer']['date']}")
    print(f"  {meta['message'].splitlines()[0]}")

    targets = [(p, d) for p, d in MAPPING.items() if p in files]
    if not targets:
        print("\n✗ 该版本里没有任何部署件（可能是最早期的纯文档提交）")
        return 3

    bk, saved = snapshot([(p, d) for p, d in MAPPING.items() if d.exists()], sha[:8])
    print(f"\n① 快照本机现有部署件 {len(saved)} 个 → {bk}")

    written = []
    for path, dest in targets:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob_bytes(files[path]))
        written.append(dest)
    print(f"② 写入 {len(written)} 个文件")

    moved = []
    for path, dest in MAPPING.items():
        if path in files or not dest.exists():
            continue
        t = bk / path.replace("/", "__")
        shutil.move(str(dest), str(t))
        moved.append(path)
    if moved:
        print(f"③ 移出目标版本不存在的部署件 {len(moved)} 个 → 备份")
        for m in moved:
            print(f"     - {m}")
    else:
        print("③ 无需要移出的部署件")

    bad = verify(written)
    if bad:
        print("\n✗ 编译校验失败，正在还原：")
        for p, err in bad:
            print(f"    {p.name}: {err}")
        for path, dest in targets:
            src = bk / path.replace("/", "__")
            if src.exists():
                shutil.copy2(src, dest)
        for path in moved:
            src = bk / path.replace("/", "__")
            if src.exists():
                shutil.copy2(src, MAPPING[path])
        print(f"  已还原。备份仍在 {bk}")
        return 4
    print(f"④ py_compile 通过（{sum(1 for d in written if d.suffix == '.py')} 个 .py）")

    if not do_restart:
        print("⑤ 跳过重启（--no-restart）")
        return 0
    rc, out = restart()
    print(f"⑤ 重启 rc={rc} {out or ''}")
    ok, h = health_ok()
    if ok:
        w = (h or {}).get("watchdog") or {}
        print(f"⑥ /health 200 | watchdog enabled={w.get('enabled')}")
        print(f"\n回滚完成：{sha[:8]}")
        print(f"  快照：{bk}")
        return 0
    print("⑥ /health 未在 90s 内返回 200 —— 代码已就位但服务没起来：")
    print("     tail -30 /tmp/token-gateway.log")
    print(f"  还原：cp {bk}/* 回各自位置（文件名里的 __ 对应目录分隔符）")
    return 0


def cmd_tag(name, apply_it):
    head = api(f"/repos/{REPO}/git/ref/heads/main")["object"]["sha"]
    meta = api(f"/repos/{REPO}/git/commits/{head}")
    print(f"HEAD = {head[:8]}  {meta['committer']['date']}")
    print(f"标签 {name} → {meta['message'].splitlines()[0][:70]}")
    if not apply_it:
        print("\n只读预览。确认后加 --apply 创建。")
        return 5
    try:
        api(f"/repos/{REPO}/git/refs", "POST", {"ref": f"refs/tags/{name}", "sha": head})
        print(f"\n✓ 已创建 refs/tags/{name}")
        return 0
    except SystemExit as e:
        if "422" in str(e):
            print(f"\n✓ 标签 {name} 已存在，未改动")
            return 0
        raise


# ─────────────────────────── 入口 ───────────────────────────

def main(argv):
    if not TOKEN:
        print("✗ 没找到 GitHub token（~/.git-credentials）", file=sys.stderr)
        return 2
    args = argv[1:]
    flags = {a for a in args if a.startswith("--")}
    if "--help" in flags or "-h" in flags or not args:
        print(__doc__)
        return 0

    def val(flag, default=None):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    if "--list" in flags:
        return cmd_list(val("--limit", 20))
    if "--tag" in flags:
        return cmd_tag(val("--tag"), "--apply" in flags)
    if "--to" in flags:
        ref = val("--to")
        if not ref:
            print("--to 需要一个版本（sha 或 tag）", file=sys.stderr)
            return 1
        if "--apply" not in flags:
            return cmd_diff(ref)
        return cmd_apply(ref, do_restart="--no-restart" not in flags)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
