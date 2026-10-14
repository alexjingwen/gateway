#!/usr/bin/env python3
"""把本地提交推到 GitHub —— 走 Git Data API，不走 `git push`。

为什么需要它
------------
本机出网代理**拦 github.com:443**（直连超时 / CONNECT 502），只有 api.github.com 放通。
所以 `git push` 连不上，也 fetch 不到 origin ref —— 凡是要读 `origin/<branch>` 的方案
在这里都不可行。这个脚本改成用 REST API 直接把本地 HEAD 的树写上去。

三种模式
--------
  bootstrap  远端仓库是**空的**（没有任何 ref）：建 blob → 建 tree → 建**无父** commit
             → `POST /git/refs` 创建分支。适用于刚在网页上新建、没勾 README 的仓库。
  sync-tree  远端已有提交：以远端 tip 为 base_tree 补齐本地条目（**只增改，绝不删**）。
  auto（默认）先探测分支是否存在，自动二选一。

注意：bootstrap 会把本地的多个提交压成一个（无父根提交）。本仓库推的时候只有 1 个提交，
所以无损；若以后本地累积了多个提交且远端还是空的，历史会被压平。

用法
----
    python3 tools/push-via-api.py                       # auto
    python3 tools/push-via-api.py --create              # 仓库不存在时顺手创建（需要 PAT 有建库权限）
    python3 tools/push-via-api.py --repo o/r --branch main --dry-run

PAT 只在运行时从 ~/.git-credentials 读出，通过内存传给 API，不落盘、不打印。
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
DEFAULT_REPO = "alexjingwen/gateway"


def git(*args, binary=False):
    out = subprocess.run(["git", *args], capture_output=True, check=True).stdout
    return out if binary else out.decode("utf-8", "replace")


def get_token():
    path = os.path.expanduser("~/.git-credentials")
    try:
        with open(path) as fh:
            for line in fh:
                m = re.match(r"https?://[^:/]+:([^@]+)@github\.com", line.strip())
                if m:
                    return m.group(1)
    except FileNotFoundError:
        pass
    raise SystemExit("~/.git-credentials 里没有 github.com 的凭据")


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:600]}")
        self.status = status
        self.body = body


class Api:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "ollama-token-gateway-push")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read()
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace"))


def local_files(prefix=""):
    """返回 [(mode, path, blob_sha)]，来自本地 HEAD 的树。

    prefix: 给所有路径套一层远端子目录（用于把整个项目塞进**别的**仓库的子目录里），
            形如 "ollama-token-gateway"。空串表示平铺到远端根目录。
    """
    pre = prefix.strip("/") + "/" if prefix.strip("/") else ""
    out = []
    for line in git("ls-tree", "-r", "HEAD").splitlines():
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        mode, _typ, sha = meta.split()
        if mode == "120000":          # 符号链接：内容就是目标路径，按普通文件存
            mode = "100644"
        out.append((mode, pre + path, sha))
    return out


def make_entries(api, repo, files, dry_run):
    """把本地文件逐个建成远端 blob，带字节级自检。"""
    entries = []
    for mode, path, sha in files:
        # 直接按 blob sha 取字节，**不走路径**：开了 --prefix 时远端路径在本地并不存在
        # （本地是 .gitignore，远端是 ollama-token-gateway/.gitignore），用 HEAD:{path} 会 128。
        blob = git("cat-file", "blob", sha, binary=True)
        # 自检：git blob sha = sha1(b"blob <len>\0" + 内容)，不是对内容直接取 sha1。
        # 对不上说明取字节这一段坏了 —— 宁可不推，也不要静默产出坏历史。
        digest = hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest()
        if digest != sha:
            raise SystemExit(f"取回的字节对不上 blob（{path}）：git 取字节这一段不可信，已中止")
        if dry_run:
            entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
            continue
        created = api.call("POST", f"/repos/{repo}/git/blobs", {
            "content": base64.b64encode(blob).decode(), "encoding": "base64",
        })
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": created["sha"]})
    return entries


def bootstrap(api, repo, branch, files, dry_run):
    print(f"[bootstrap] 远端 {branch} 不存在 → 建无父根提交，含 {len(files)} 个文件")
    for _m, p, _s in files:
        print(f"      {p}")
    if dry_run:
        print("[dry-run] 到此为止，未写入远端。")
        return
    entries = make_entries(api, repo, files, dry_run)
    tree = api.call("POST", f"/repos/{repo}/git/trees", {"tree": entries})
    message = git("log", "-1", "--format=%B", "HEAD").strip() or "initial commit"
    commit = api.call("POST", f"/repos/{repo}/git/commits", {
        "message": message, "tree": tree["sha"], "parents": [],
    })
    api.call("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    print(f"\n[+] 已推送 -> https://github.com/{repo}/tree/{branch}  ({commit['sha'][:8]})")


def sync_tree(api, repo, branch, files, dry_run):
    base = api.call("GET", f"/repos/{repo}/git/ref/heads/{branch}")["object"]["sha"]
    base_tree = api.call("GET", f"/repos/{repo}/git/commits/{base}")["tree"]["sha"]
    rmap = {}
    for e in api.call("GET", f"/repos/{repo}/git/trees/{base_tree}?recursive=1")["tree"]:
        if e["type"] == "blob":
            rmap[e["path"]] = e

    todo = [(m, p, s) for (m, p, s) in files
            if rmap.get(p) is None or rmap[p]["sha"] != s or rmap[p]["mode"] != m]
    print(f"[sync-tree] 本地 {len(files)} / 远端 {len(rmap)} → 待同步 {len(todo)} 个（只增改，绝不删）")
    for _m, p, _s in todo:
        print(f"      {p}")
    if not todo:
        print("[=] 远端已与本地一致，无需推送。")
        return
    if dry_run:
        print("[dry-run] 到此为止，未写入远端。")
        return
    entries = make_entries(api, repo, todo, dry_run)
    tree = api.call("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": entries})
    commit = api.call("POST", f"/repos/{repo}/git/commits", {
        "message": "chore: 本地树与远端对齐", "tree": tree["sha"], "parents": [base],
    })
    api.call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": commit["sha"]})
    print(f"\n[+] 已推送 -> https://github.com/{repo}/tree/{branch}  ({commit['sha'][:8]})")


def ensure_repo(api, repo, create, private, dry_run):
    """确认仓库存在；不存在时按 --create 决定是否创建。返回 True 表示可继续。"""
    try:
        info = api.call("GET", f"/repos/{repo}")
        print(f"[i] 远端仓库已存在：{info['full_name']}（{'private' if info['private'] else 'public'}）")
        return True
    except ApiError as e:
        if e.status != 404:
            raise
    print(f"[!] 远端仓库 {repo} 不存在（404）。")
    if not create:
        print("    当前 PAT 是 fine-grained token，实测**没有建库权限**（POST /user/repos -> 403）。")
        print("    请在 GitHub 网页上新建一个**空仓库**（不要勾 README / .gitignore）：")
        print(f"        https://github.com/new  name=gateway  visibility={'private' if private else 'public'}")
        print("    或者给 PAT 加上 Administration: write（建库）权限后重跑本脚本并加 --create。")
        return False
    if dry_run:
        print("[dry-run] 将创建仓库，实际未创建。")
        return False
    try:
        api.call("POST", "/user/repos", {
            "name": repo.split("/", 1)[1], "private": private,
            "description": "Ollama token gateway + device memory guard",
        })
        print(f"[+] 已创建仓库 {repo}")
        return True
    except ApiError as e:
        print(f"[x] 建仓库失败 {e.status}\n{e.body[:500]}")
        print("    PAT 缺少建库权限。请改用网页新建空仓库，或给 PAT 加 Administration: write。")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--create", action="store_true", help="仓库不存在时尝试用 API 创建")
    ap.add_argument("--public", action="store_true", help="配合 --create：建公开仓库（默认私有）")
    ap.add_argument("--prefix", default="", help="把所有文件放到远端的这个子目录下（例如 ollama-token-gateway）")
    args = ap.parse_args()

    repo, branch = args.repo, args.branch
    api = Api(get_token())
    files = local_files(args.prefix)
    print(f"[+] 本地 HEAD 共 {len(files)} 个文件"
          + (f"，远端路径前缀 = {args.prefix.strip('/')}/" if args.prefix.strip("/") else "（平铺到远端根目录）"))

    if not ensure_repo(api, repo, args.create, not args.public, args.dry_run):
        return 1

    try:
        api.call("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        sync_tree(api, repo, branch, files, args.dry_run)
    except ApiError as e:
        if e.status in (404, 409):
            bootstrap(api, repo, branch, files, args.dry_run)
        else:
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
