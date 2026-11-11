#!/usr/bin/env python3
"""强化版：让 decode 持续足够久，把 mid-flight 注入窗口撑开。

旧复现（APC 关闭时代）用的是 max_tokens=16 的短请求也 500，但那时的 batch
调度路径不同。现在要回答的是：APC 开启 + speculative_width_cap=1 下，
同模型并发到底还崩不崩 —— 用长解码 + 多轮 + 多并发去撞。
"""
import argparse
import json
import threading
import time
import urllib.error
import urllib.request

FILLER = "并行测试用的填充文本，只为把前缀撑长，内容本身没有意义。"


def build_prompt(tag, fill, count):
    body = (f"【档案 {tag}】" + FILLER) * fill
    return (f"下面是一段关于「{tag}」的材料。\n\n{body}\n\n"
            f"请从 1 数到 {count}，每个数字写一行，不要任何解释。")


def call(url, model, prompt, gen, timeout=900):
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": gen, "stream": False, "temperature": 0.0}
    req = urllib.request.Request(url + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        c = d["choices"][0]
        body = c["message"].get("content") or ""
        return {"status": 200, "secs": round(time.time() - t, 2),
                "pt": d.get("usage", {}).get("prompt_tokens"),
                "ct": d.get("usage", {}).get("completion_tokens"),
                "fin": c.get("finish_reason"), "out": body, "err": ""}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "secs": round(time.time() - t, 2),
                "pt": None, "ct": 0, "fin": None, "out": "",
                "err": e.read()[:200].decode("utf-8", "replace")}
    except Exception as e:
        return {"status": "ERR", "secs": round(time.time() - t, 2),
                "pt": None, "ct": 0, "fin": None, "out": "",
                "err": f"{type(e).__name__}: {e}"}


def parallel(url, model, prompts, gen):
    out = [None] * len(prompts)
    bar = threading.Barrier(len(prompts))

    def w(i):
        bar.wait()
        out[i] = call(url, model, prompts[i], gen)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(len(prompts))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def brief(r):
    return (f"{r['status']:>3} {r['secs']:>6}s pt={r['pt']} ct={r['ct']} "
            f"fin={r['fin']} out={len(r['out'])} {r['err'][:90]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default="qwen3.8-flash-next")
    ap.add_argument("--gen", type=int, default=256)
    ap.add_argument("--fill", type=int, default=12)
    ap.add_argument("--count", type=int, default=80, help="提示词要求数到几")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--conc", default="2,3")
    args = ap.parse_args()

    concs = [int(x) for x in args.conc.split(",") if x.strip()]
    pa = build_prompt("甲", args.fill, args.count)
    pb = build_prompt("乙", args.fill, args.count)

    print(f"模型={args.model} gen={args.gen} fill={args.fill} conc={concs}")
    r = call(args.url, args.model, pa, args.gen)   # 预热 + 基线
    print(f"[基线·串行] {brief(r)}")
    ref = r["out"]

    summary = []
    for n in concs:
        for kind, prompts in (("same", [pa] * n), ("diff", [pa, pb] * (n // 2) + ([pa] if n % 2 else []))):
            bad = 0
            for rnd in range(args.rounds):
                res = parallel(args.url, args.model, prompts[:n], args.gen)
                codes = [x["status"] for x in res]
                errs = [x["err"] for x in res if x["err"]]
                ok = all(c == 200 for c in codes)
                if not ok:
                    bad += 1
                span = max(x["secs"] for x in res)
                ct = [x["ct"] for x in res]
                print(f"  [{kind} n={n} r{rnd + 1}] {codes} span={span:>6}s ct={ct}"
                      + ("" if ok else f"  ERR={errs[0][:150]}"))
            summary.append((kind, n, bad, args.rounds))

    print("\n== 汇总（失败轮次/总轮次） ==")
    for kind, n, bad, tot in summary:
        flag = "OK" if bad == 0 else "FAIL"
        print(f"  {kind:<5} n={n}  {bad}/{tot}  {flag}")


if __name__ == "__main__":
    main()
