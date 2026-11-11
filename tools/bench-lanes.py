#!/usr/bin/env python3
"""跨实例并行基准：两条独立上下文分别打两个 gmlx 实例。

回答三个问题：
  1) 不同实例的同模型并发，会不会触发 mid-flight 注入缺陷（Cannot broadcast）？
  2) 墙钟是否真的重叠（并行），还是被同一块 GPU 串起来？
  3) 每条请求的 decode 掉多少 —— 这是并行的真实代价。

用法：
  python3 tools/bench-lanes.py --lanes http://127.0.0.1:8090,http://127.0.0.1:8092
"""
import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request

FILLER = "并行测试用的填充文本，只为把上下文撑长，内容本身没有意义。"


def build_prompt(tag, fill, count):
    body = (f"【档案 {tag}】" + FILLER) * fill
    return (f"下面是一段关于「{tag}」的材料。\n\n{body}\n\n"
            f"请从 1 数到 {count}，每个数字写一行，不要任何解释。")


def call(base, model, prompt, gen, timeout=1200):
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": gen, "stream": False, "temperature": 0.0}
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        u = d.get("usage", {})
        c = d["choices"][0]
        secs = time.time() - t
        ct = u.get("completion_tokens") or 0
        return {"status": 200, "start": t, "secs": secs,
                "pt": u.get("prompt_tokens"), "ct": ct,
                "tps": (ct / secs) if secs > 0 and ct else 0.0,
                "err": "", "out": c["message"].get("content") or ""}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "start": t, "secs": time.time() - t,
                "pt": None, "ct": 0, "tps": 0.0,
                "err": e.read()[:200].decode("utf-8", "replace"), "out": ""}
    except Exception as e:
        return {"status": "ERR", "start": t, "secs": time.time() - t,
                "pt": None, "ct": 0, "tps": 0.0,
                "err": f"{type(e).__name__}: {e}", "out": ""}


def run_serial(jobs, model, gen):
    return [call(base, model, p, gen) for base, p in jobs]


def run_parallel(jobs, model, gen):
    out = [None] * len(jobs)
    bar = threading.Barrier(len(jobs))

    def w(i, base, prompt):
        bar.wait()
        out[i] = call(base, model, prompt, gen)

    ts = [threading.Thread(target=w, args=(i, b, p))
          for i, (b, p) in enumerate(jobs)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def span(results):
    if not results:
        return 0.0
    lo = min(r["start"] for r in results)
    hi = max(r["start"] + r["secs"] for r in results)
    return hi - lo


def report(label, res):
    codes = [r["status"] for r in res]
    sp = span(res)
    agg = sum(r["ct"] for r in res) / sp if sp else 0
    print(f"  {label:<14} {codes}  span={sp:>6.2f}s  agg={agg:>6.1f} tok/s  "
          f"per-req={[round(r['tps'], 1) for r in res]}")
    for r in res:
        if r["err"]:
            print(f"     ERR {r['err'][:160]}")
    return {"codes": codes, "span": sp, "agg": agg,
            "per": [r["secs"] for r in res], "tps": [r["tps"] for r in res]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lanes", default="http://127.0.0.1:8090,http://127.0.0.1:8092")
    ap.add_argument("--model", default="qwen3.8-flash-next")
    ap.add_argument("--gen", type=int, default=256)
    ap.add_argument("--fill", type=int, default=12)
    ap.add_argument("--count", type=int, default=80)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    lanes = [x.strip().rstrip("/") for x in args.lanes.split(",") if x.strip()]
    assert len(lanes) >= 2, "至少两条 lane"

    pa = build_prompt("甲", args.fill, args.count)
    pb = build_prompt("乙", args.fill, args.count)

    print(f"lanes={lanes} model={args.model} gen={args.gen}")
    # 预热：各自建自己的 APC/KV 路径，避免首轮冷启动污染对比
    for i, base in enumerate(lanes[:2]):
        r = call(base, args.model, [pa, pb][i], 32)
        print(f"  预热 {base} {r['status']} {r['secs']:.2f}s pt={r['pt']}")

    jobs = [(lanes[0], pa), (lanes[1], pb)]
    rows = []

    print("\n[串行] 两条上下文先后跑（同一对 lane）")
    for rnd in range(args.rounds):
        rows.append(("seq", report(f"serial r{rnd + 1}", run_serial(jobs, args.model, args.gen))))
    print("\n[并行] 两条上下文同时跑（各自独立实例）")
    for rnd in range(args.rounds):
        rows.append(("par", report(f"parallel r{rnd + 1}", run_parallel(jobs, args.model, args.gen))))

    seq = [r for k, r in rows if k == "seq"]
    par = [r for k, r in rows if k == "par"]
    s_span = statistics.median([r["span"] for r in seq])
    p_span = statistics.median([r["span"] for r in par])
    s_agg = statistics.median([r["agg"] for r in seq])
    p_agg = statistics.median([r["agg"] for r in par])
    errs = sum(1 for r in seq + par for c in r["codes"] if c != 200)
    print(f"\n== 中位数 ==\n  串行 span={s_span:.2f}s agg={s_agg:.1f} tok/s\n"
          f"  并行 span={p_span:.2f}s agg={p_agg:.1f} tok/s\n"
          f"  speedup={s_span / p_span if p_span else 0:.2f}x  "
          f"agg 比={p_agg / s_agg if s_agg else 0:.2f}x  错误数={errs}")
    if args.json:
        print(json.dumps({"seq": {"span": s_span, "agg": s_agg},
                          "par": {"span": p_span, "agg": p_agg},
                          "speedup": s_span / p_span if p_span else 0,
                          "errors": errs}, ensure_ascii=False))


if __name__ == "__main__":
    main()
