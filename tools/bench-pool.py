#!/usr/bin/env python3
"""泳道池端到端基准：**一个模型一个上下文** 到底能不能真并行。

要回答的四个问题（每一个都用数字或状态码回答，不靠推理）：
  1) 把 model 填成 `pool`，不同上下文会不会落到**不同模型**上？
  2) 墙钟是不是真重叠（并行 ≈ max，而不是 ≈ sum）？
  3) 同一上下文第二次还回同一个模型吗（粘性，APC 前缀缓存的前提）？
  4) 并发数超过模型数时，多出来的上下文是**排队**还是报错？

用法：
  python3 tools/bench-pool.py --key sk-xxx --n 3 --gen 120
  python3 tools/bench-pool.py --key sk-xxx --n 4 --gen 200 --rounds 2 --json

判定口径：
  · speedup = 串行墙钟 / 并行墙钟。两条泳道时上限是 2.0，实际值被慢模型拖低。
  · 泳道分布取自响应头 X-Gateway-Lane（网关写入，客户端可见），不猜。
  · 排队判据：某个请求的 X-Gateway-Lane-Wait > 0.05s，且它最终仍 200。
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

FILLER = "泳道测试用的填充文本，只为把上下文撑长，内容本身没有意义。"


def build_prompt(tag, fill, count):
    body = (f"【档案 {tag}】" + FILLER) * fill
    return (f"下面是一段关于「{tag}」的材料。\n\n{body}\n\n"
            f"请从 1 数到 {count}，每个数字写一行，不要任何解释。")


def call(base, key, model, prompt, gen, ctx, timeout=1800):
    payload = {"model": model,
               "messages": [{"role": "system", "content": f"你是上下文 {ctx} 的助手。"},
                            {"role": "user", "content": prompt}],
               "max_tokens": gen, "stream": False, "temperature": 0.0}
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    # 显式声明上下文：客户端能给出稳定 id 时，网关就不必靠内容指纹推断
    req.add_header("X-TG-Context", ctx)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            hdr = {k.lower(): v for k, v in r.headers.items()}
            d = json.loads(r.read())
        u = d.get("usage", {})
        ct = u.get("completion_tokens") or 0
        secs = time.time() - t0
        return {"ctx": ctx, "status": 200, "secs": secs,
                "ct": ct, "tps": (ct / secs) if secs > 0 and ct else 0.0,
                "lane": hdr.get("x-gateway-lane", ""),
                "model": hdr.get("x-gateway-model", ""),
                "wait": float(hdr.get("x-gateway-lane-wait", "0") or 0),
                "err": ""}
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        return {"ctx": ctx, "status": e.code, "secs": time.time() - t0, "ct": 0,
                "tps": 0.0, "lane": "", "model": "", "wait": 0.0, "err": body}
    except Exception as e:                                  # noqa: BLE001
        return {"ctx": ctx, "status": "ERR", "secs": time.time() - t0, "ct": 0,
                "tps": 0.0, "lane": "", "model": "", "wait": 0.0,
                "err": f"{type(e).__name__}: {e}"}


def run_serial(base, key, model, jobs, gen):
    t0 = time.time()
    out = [call(base, key, model, p, gen, c) for c, p in jobs]
    return out, time.time() - t0


def run_parallel(base, key, model, jobs, gen):
    out = [None] * len(jobs)
    t0 = time.time()

    def one(i, ctx, prompt):
        out[i] = call(base, key, model, prompt, gen, ctx)

    ths = [threading.Thread(target=one, args=(i, c, p)) for i, (c, p) in enumerate(jobs)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    return out, time.time() - t0


def show(title, rows, wall):
    print(f"\n{title}  墙钟 {wall:.1f}s")
    print(f"  {'上下文':10s} {'状态':>4s} {'泳道':>6s} {'模型':22s} "
          f"{'耗时':>7s} {'tok/s':>7s} {'排队':>6s}")
    for r in rows:
        print(f"  {r['ctx']:10s} {str(r['status']):>4s} {r['lane'] or '-':>6s} "
              f"{(r['model'] or '-')[:22]:22s} {r['secs']:7.1f} "
              f"{r['tps']:7.1f} {r['wait']:6.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:3000/v1")
    ap.add_argument("--key", required=True)
    ap.add_argument("--model", default="pool")
    ap.add_argument("--n", type=int, default=3, help="并发上下文数")
    ap.add_argument("--gen", type=int, default=120, help="每个请求生成多少 token")
    ap.add_argument("--fill", type=int, default=20, help="上下文填充段数（越长 prefill 越重）")
    ap.add_argument("--rounds", type=int, default=1, help="并行轮数（第二轮看粘性）")
    ap.add_argument("--skip-serial", action="store_true", help="跳过串行基线（省时间）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    jobs = [(f"ctx-{i+1}", build_prompt(f"ctx-{i+1}", args.fill, args.gen))
            for i in range(args.n)]
    report = {"model": args.model, "n": args.n, "gen": args.gen, "rounds": []}

    serial_wall = None
    if not args.skip_serial:
        rows, serial_wall = run_serial(args.url, args.key, args.model, jobs, args.gen)
        show("串行基线（一个接一个）", rows, serial_wall)
        report["serial"] = {"wall": serial_wall, "rows": rows}

    for rnd in range(args.rounds):
        # 每轮用**新的上下文 id**：同一上下文是粘性的（认了模型就等它），拿上一轮绑定
        # 过的 id 再并发，测到的是"粘性排队"而不是"新上下文到达时的分派"。
        jobs = [(f"ctx-{i+1}-r{rnd+1}", build_prompt(f"ctx-{i+1}", args.fill, args.gen))
                for i in range(args.n)]
        rows, wall = run_parallel(args.url, args.key, args.model, jobs, args.gen)
        show(f"并行第 {rnd + 1} 轮（同时发 {args.n} 个）", rows, wall)
        ok = [r for r in rows if r["status"] == 200]
        lanes = [r["lane"] for r in ok]
        waited = [r for r in ok if r["wait"] > 0.05]
        print(f"  成功 {len(ok)}/{len(rows)}｜用到泳道 {sorted(set(lanes))}｜"
              f"排队过 {len(waited)} 个｜agg {sum(r['tps'] for r in ok):.1f} tok/s")
        if serial_wall:
            print(f"  相对串行 speedup = {serial_wall / wall:.2f}x "
                  f"（{len(set(lanes))} 条泳道，上限 {len(set(lanes))}.0x）")
        report["rounds"].append({"wall": wall, "rows": rows,
                                 "speedup": (serial_wall / wall) if serial_wall else None,
                                 "lanes": sorted(set(lanes))})

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    # 退出码：出现非 200 就算失败（这个基准的第一职责是"别 500"）
    bad = [r for rnd in report["rounds"] for r in rnd["rows"] if r["status"] != 200]
    if bad:
        print(f"\n❌ {len(bad)} 个请求失败：")
        for r in bad:
            print(f"   {r['ctx']} → {r['status']} {r['err'][:160]}")
        return 1
    print("\n✅ 全部 200")
    return 0


if __name__ == "__main__":
    sys.exit(main())
