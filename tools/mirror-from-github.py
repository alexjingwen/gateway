#!/usr/bin/env python3
"""把 GitHub 上的 alexjingwen/gateway 镜像到本地目录（用于重建工作副本）。

为什么需要
----------
本机只放「活文件」，不是 git 仓库；推远端走 Git Data API。这样一个会话里对话结束、
临时目录被清掉之后，就没有本地历史可读了。这个脚本用 API 把远端 main 的每个 blob
拉下来重建一份**可提交的**工作副本，并按 git 的 blob 算法逐个校验 sha —— 字节不对就
报错而不是静默生成坏副本。

用法
----
    python3 tools/mirror-from-github.py --dest ~/gateway-repo
    python3 tools/mirror-from-github.py --dest /tmp/x --repo o/r --branch main
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


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


def api(path, token, raw=False, tries=5):
    # 本机代理偶发 502（Tunnel connection failed），一个文件失败就整个镜像白做，
    # 所以每个请求都给几次重试。502/超时才重试，4xx 直接抛（那是有意义的错误）。
    last = None
    for i in range(tries):
        req = urllib.request.Request(API + path)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "gateway-mirror")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
            return body if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except Exception as e:                       # URLError / 超时 / 连接重置
            last = e
        time.sleep(0.8 * (i + 1))
    raise SystemExit(f"{path}: {tries} 次重试仍失败（{type(last).__name__}: {last}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="alexjingwen/gateway")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--dest", required=True)
    args = ap.parse_args()

    token = get_token()
    dest = os.path.abspath(os.path.expanduser(args.dest))
    ref = api(f"/repos/{args.repo}/git/ref/heads/{args.branch}", token)
    commit_sha = ref["object"]["sha"]
    tree = api(f"/repos/{args.repo}/git/trees/{commit_sha}?recursive=1", token)
    if tree.get("truncated"):
        raise SystemExit("远端树被截断，这个脚本不支持超大树")
    print(f"远端 {args.repo}@{args.branch} = {commit_sha[:8]}，{len(tree['tree'])} 个条目")

    n = 0
    for e in tree["tree"]:
        rel = e["path"]
        path = os.path.join(dest, rel)
        if e["type"] == "tree":
            os.makedirs(path, exist_ok=True)
            continue
        if e["type"] != "blob":
            continue
        blob = api(f"/repos/{args.repo}/git/blobs/{e['sha']}", token, raw=True)
        data = json.loads(blob)
        if data.get("encoding") != "base64":
            raise SystemExit(f"{rel}: 非 base64 编码（{data.get('encoding')}），不支持")
        import base64
        content = base64.b64decode(data["content"])
        digest = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
        if digest != e["sha"]:
            raise SystemExit(f"{rel}: 字节对不上远端 blob（期望 {e['sha'][:8]}，实得 {digest[:8]}），已中止")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        os.chmod(path, 0o755 if e.get("mode") == "100755" else 0o644)
        n += 1
    print(f"[+] 写入 {n} 个文件 → {dest}")
    print(f"    下一步：cd {dest} && git init -b {args.branch} && git add -A && git commit -m baseline")


if __name__ == "__main__":
    sys.exit(main())
