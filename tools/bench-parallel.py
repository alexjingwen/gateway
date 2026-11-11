#!/usr/bin/env python3
"""并发/并行压测：量「同时进行」和「互不影响」两件事，别把前者当后者。

一个场景 = 一串请求（每个请求指定模型）。对每个场景跑两遍：

  seq  逐条串行执行          → T_seq
  par  同时发起（线程）      → T_par

由此得到三个数：

  speedup      T_seq / T_par。>1 表示并发省了时间；接近 1 就是纯粹在排队。
  concurrency  Σ(单请求墙钟) / T_par。等于「平均同时在飞的请求数」。
               2 并发完全重叠 ≈ 2.0；完全串行 ≈ 1.0。
  agg tok/s    总生成 token / 墙钟。这是真正能落地的吞吐。

为什么两个都要看：speedup 会因为「排队反而摊薄了 prefill」而虚高，
concurrency 才直接反映 GPU 上是不是真的两个 batch 在跑。

用法：
  # 同模型 2 并发（验证会不会崩 / 会不会变快）
  python3 tools/bench-parallel.py --url http://127.0.0.1:8091 --models gemma4-9b --n 2

  # 异模型 2 并发（gemma + qwen）
  python3 tools/bench-parallel.py --url http://127.0.0.1:8091 \\
          --models gemma4-9b,qwen3.8-27b-mlx --n 2

  # 经网关（验证锁粒度）
  python3 tools/bench-parallel.py --url http://127.0.0.1:3000 --key-file /tmp/.benchkey \\
          --models qwen3.8-flash-next,qwen3.8-27b-mlx --n 2
"""
import argparse
import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request

DEFAULT_KEY_FILE = "/tmp/.benchkey"

FILLER = ("并行测试材料。这里只关心解码阶段的争用，所以 prompt 尽量短、"
          "生成尽量长，把 prefill 的影响压到最小。")


def api_key(path):
    if not path or not os.path.exists(path):
        return ""
    return open(path).read().strip()


def build_prompt(seed, tokens=0):
    """短 prompt、长生成：让 decode 段主导，才看得出带宽争用。

    不用长填充文本：gemma4 这个 blob 是**基座模型**（Ollama 的模板块就是
    `{{ .Prompt }}`），长前缀会让它直接吐 EOS 或复读。短提示词能让它稳定
    生成到 max_tokens，作为「同形状算力负载」是够的 —— 本脚本不评质量。
    """
    return f"编号 {seed}。请从 1 数到 40，每个数字写一行，不要任何解释。"


def one_request(url, model, prompt, gen, key="", timeout=900):
    """流式发一条请求，记录 ttft / 总时长 / 首尾时间戳。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": gen,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)

    t0 = time.time()
    first = None
    tokens = 0
    text = []
    err = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    obj = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if obj.get("error"):
                    err = str(obj["error"])[:200]
                    break
                usage = obj.get("usage")
                if usage:
                    tokens = usage.get("completion_tokens") or tokens
                for ch in obj.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content") or ""
                    if piece:
                        if first is None:
                            first = time.time()
                        text.append(piece)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        err = f"HTTP {e.code}: {detail}"
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    t1 = time.time()

    if not tokens:
        tokens = len("".join(text).split())  # 粗略回退
    return {
        "model": model,
        "start": t0,
        "end": t1,
        "wall": t1 - t0,
        "ttft": (first - t0) if first else None,
        "tokens": tokens,
        "decode_tps": (tokens / (t1 - first)) if first and t1 > first else None,
        "err": err,
    }


def run_seq(plan, gen, key):
    out = []
    for model, base, prompt in plan:
        out.append(one_request(base, model, prompt, gen, key))
    return out


def run_par(plan, gen, key):
    out = [None] * len(plan)
    ready = threading.Barrier(len(plan))

    def worker(i, model, base, prompt):
        ready.wait()  # 尽量同时出发，否则第一个请求的 prefill 就成了别人的起跑线
        out[i] = one_request(base, model, prompt, gen, key)

    threads = [threading.Thread(target=worker, args=(i, m, b, p))
               for i, (m, b, p) in enumerate(plan)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def summarize(tag, results, key_errors):
    span = max(r["end"] for r in results) - min(r["start"] for r in results)
    wall_sum = sum(r["wall"] for r in results)
    tokens = sum(r["tokens"] for r in results)
    ttfts = [r["ttft"] for r in results if r["ttft"]]
    tps = [r["decode_tps"] for r in results if r["decode_tps"]]
    errs = [r["err"] for r in results if r["err"]]
    for e in errs:
        key_errors.append(e)
    return {
        "tag": tag,
        "span": span,
        "wall_sum": wall_sum,
        "tokens": tokens,
        "agg_tps": tokens / span if span else 0,
        "concurrency": wall_sum / span if span else 0,
        "ttft_med": statistics.median(ttfts) if ttfts else None,
        "ttft_max": max(ttfts) if ttfts else None,
        "decode_med": statistics.median(tps) if tps else None,
        "errors": errs,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8091")
    ap.add_argument("--models", required=True,
                    help="逗号分隔。--n 大于模型数时循环取用")
    ap.add_argument("--n", type=int, default=2, help="并发请求数（一个场景）")
    ap.add_argument("--gen", type=int, default=256, help="每条请求生成的 token 数")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--warmup", type=int, default=64)
    ap.add_argument("--key-file", default="", help="经网关时需要；不传则不带 Authorization")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    models = []
    for spec in args.models.split(","):
        spec = spec.strip()
        if not spec:
            continue
        if "@" in spec:
            name, base = spec.rsplit("@", 1)
            models.append((name, base))
        else:
            models.append((spec, args.url))
    key = api_key(args.key_file)

    # 预热：首次请求含权重加载，混进测量会把结论带偏
    for name, base in models:
        r = one_request(base, name, build_prompt(0), args.warmup, key)
        if r["err"]:
            print(f"[预热失败] {name}: {r['err'][:180]}")
            return 2

    plan = [(models[i % len(models)][0], models[i % len(models)][1],
             build_prompt(i + 1)) for i in range(args.n)]
    rows = []
    errs = []
    for rnd in range(args.rounds):
        seq = run_seq(plan, args.gen, key)
        par = run_par(plan, args.gen, key)
        s = summarize(f"seq#{rnd + 1}", seq, errs)
        p = summarize(f"par#{rnd + 1}", par, errs)
        s["speedup"] = None
        p["speedup"] = s["span"] / p["span"] if p["span"] else None
        rows += [s, p]

    if args.json:
        print(json.dumps({"rows": rows, "models": [m for m, _ in models], "n": args.n,
                          "gen": args.gen}, ensure_ascii=False, indent=2))
        return 0

    print(f"\n模型={[m for m, _ in models]} n={args.n} gen={args.gen}")
    print(f"{'run':<8}{'span':>8}{'Σwall':>8}{'tokens':>8}{'agg t/s':>9}"
          f"{'并发度':>8}{'ttft中位':>9}{'decode中位':>11}{'speedup':>9}")
    for r in rows:
        def fmt(v, p=2):
            return "-" if v is None else f"{v:.{p}f}"
        print(f"{r['tag']:<8}{r['span']:>8.2f}{r['wall_sum']:>8.2f}{r['tokens']:>8}"
              f"{r['agg_tps']:>9.1f}{r['concurrency']:>8.2f}"
              f"{fmt(r['ttft_med']):>9}{fmt(r['decode_med'], 1):>11}"
              f"{fmt(r['speedup']):>9}")
    if errs:
        print(f"\n错误 {len(errs)} 条：")
        for e in errs[:5]:
            print("  -", e[:200])
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
