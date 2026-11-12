#!/usr/bin/env python3
"""多模型并发对照：单独跑 vs 同时跑，逐个报 ttft / decode tok/s / 墙钟。

用途：判断"某模型能不能当泳道"。判据只有两条：
  ① 单独 decode 是否接近主力模型（否则并行也追不回来，见 README「多模型泳道」）；
  ② 与主力同时跑时，双方各掉多少（抢 GPU 的代价）。
两条都过，才值得写进 TG_LANES。

用法：
  # 默认对比 125B（gmlx:8090）、gpt-oss:20b（Ollama:11434）、27B（gmlx:8090）
  python3 tools/bench-gptoss.py --gen 200

  # 只测其中两个，或换成自己的模型（name@url 写法，冒号留在模型名里）
  python3 tools/bench-gptoss.py --models gpt-oss:20b@http://127.0.0.1:11434,qwen3.8-flash-next@http://127.0.0.1:8090

  # 只跑并发那一半（模型已经热了）
  python3 tools/bench-gptoss.py --mode par

注意：会真实占用模型，不要在有人正用的时候跑；125B 冷启动要几十秒。
"""
import argparse
import json
import sys
import threading
import time
import urllib.request

DEFAULT = ("qwen3.8-flash-next@http://127.0.0.1:8090,"
           "gpt-oss:20b@http://127.0.0.1:11434")


def parse_models(raw):
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        model, _, url = item.rpartition("@")
        if not model:
            model, url = url, "http://127.0.0.1:11434"
        out.append((model, url.rstrip("/")))
    return out


def gen(base, model, prompt, max_tokens, out, tag, timeout=900):
    """流式生成一个请求，记录 ttft / 总时长 / 正文分片数（≈ 生成的 token 数）。"""
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": 0, "stream": True}
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    ttft = None
    n = 0
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
                    j = json.loads(body)
                except Exception:
                    continue
                delta = (j.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    if ttft is None:
                        ttft = time.time() - t0
                    n += 1
        out[tag] = {"ttft": ttft, "total": time.time() - t0, "tokens": n}
    except Exception as e:                      # noqa: BLE001
        out[tag] = {"err": f"{type(e).__name__}: {e}"}


def run(jobs):
    out = {}
    threads = [threading.Thread(target=gen, kwargs=dict(out=out, **j)) for j in jobs]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    rows = []
    for j in jobs:
        r = out.get(j["tag"], {})
        if "err" in r:
            rows.append((j["tag"], None, None, r["err"]))
        else:
            decode_s = r["total"] - (r["ttft"] or 0)
            rows.append((j["tag"], r["ttft"], r["tokens"] / max(decode_s, 1e-6), ""))
    return wall, rows


def show(label, jobs, wall, rows):
    tok_sum = sum((r[2] or 0) for r in rows)
    print(f"\n{label}   墙钟 {wall:.2f}s   合计 {tok_sum:.0f} tok/s")
    for tag, ttft, tps, err in rows:
        if err:
            print(f"    {tag:26s} 失败 {err}")
        else:
            print(f"    {tag:26s} ttft {ttft:5.2f}s   decode {tps:6.1f} tok/s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", type=int, default=200, help="要求生成的数字个数（≈ 输出 token 数）")
    ap.add_argument("--models", default=DEFAULT, help="name@url，逗号分隔")
    ap.add_argument("--mode", choices=["all", "solo", "par"], default="all")
    args = ap.parse_args()

    models = parse_models(args.models)
    prompt = f"从 1 数到 {args.gen}，每行一个数字，不要解释。"
    mt = args.gen + 32

    solo = {}
    SOLO_WALL = {}
    if args.mode in ("all", "solo"):
        print("== 单模型基线（串行发起，模型可能需要冷加载）==")
        for model, url in models:
            tag = f"{model} 单独"
            wall, rows = run([dict(base=url, model=model, prompt=prompt,
                                   max_tokens=mt, tag=tag)])
            show(f"▶ {model}", None, wall, rows)
            solo[model] = rows[0]
            SOLO_WALL[model] = wall
        total = sum((r[2] or 0) for r in solo.values())
        if len(solo) > 1:
            print(f"\n串行汇总：{sum(1 for r in solo.values() if r[2])} 个模型，"
                  f"合计 {total:.0f} tok/s")

    if args.mode in ("all", "par"):
        print("\n== 全部同时发起 ==")
        jobs = [dict(base=url, model=model, prompt=prompt, max_tokens=mt,
                     tag=f"{model} 并行中") for model, url in models]
        wall, rows = run(jobs)
        show(f"▶ {len(jobs)} 个并发", None, wall, rows)
        if solo and all(r[2] for r in solo.values()):
            # 只比**墙钟**：两模型的输出长度并不相同（同一个"数到 200"有的出 231 token、
            # 有的 154），把 tok/s 简单相加会得到一个既非串行也非并行的数。用户体感是
            # "这批活多久干完"，那就比这个。
            serial_wall = sum(SOLO_WALL.values())
            print(f"\n墙钟：串行 {serial_wall:.2f}s → 并行 {wall:.2f}s "
                  f"= {serial_wall / max(wall, 1e-6):.2f}x"
                  f"（{len(jobs)} 个泳道，上限 {len(jobs):.1f}x）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
