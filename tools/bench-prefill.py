#!/usr/bin/env python3
"""前置(prefill)/解码(decode) 基准 —— 打网关或直打上游，量 ttft 与 tok/s。

为什么需要它
------------
调 gmlx 的 prefill_step_size / 调度参数时，靠"感觉快了"没有说服力：
同一个 prompt 在不同上下文长度下的 prefill 速度差一个数量级，必须固定
prompt 长度、固定生成长度、流式取 first-token 时刻，才能横向比。

指标定义
--------
  ttft      首个非空 delta 到达的墙钟（含排队、含 prefill）
  prefill   服务端 prompt_tokens / (ttft - queue) ；这里用 prompt_tokens/ttft 近似，
            排队时长单独记（网关串行锁会让短请求等长请求）
  decode    completion_tokens / (total - ttft)

用法
----
  python3 tools/bench-prefill.py --sizes 4096,28672 --runs 1
  python3 tools/bench-prefill.py --target upstream --sizes 28672   # 绕开网关
  python3 tools/bench-prefill.py --json   # 机读输出，便于脚本比对
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

GATEWAY = "http://127.0.0.1:3000"
UPSTREAM = "http://127.0.0.1:8090"
DEFAULT_KEY_FILE = "/tmp/.benchkey"

FILLER = (
    "日志分析片段：请求进入网关后按 host 与上游真实模型分组排队，"
    "同一模型一次只放行一个生成请求，避免上游 batch 注入时的张量形状冲突。"
)


def api_key(path):
    if not path:
        return "sk-test"
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return fh.read().strip()


def build_prompt(target_tokens, tail=""):
    """按近似 1 token ≈ 1.6 汉字 的经验比例拼 prompt（只用于制造长度，不求精确）。

    tail: 追加在末尾的一句"变体"。真实的代理客户端每轮重发的正是
    「同一段长 system/上下文 + 不同的最后一句」，用它模拟前缀命中的真实形态。
    """
    unit = FILLER * 20
    reps = max(1, int(target_tokens / (len(unit) / 1.6)))
    body = (unit * reps)[: int(target_tokens * 1.6)]
    return (f"下面是一段约 {target_tokens} tokens 的材料，请只回答最后一句。\n\n"
            f"{body}\n\n问题：{tail or '这段材料反复出现的关键词是什么？请用一句话回答。'}")


def one_request(url, key, model, prompt, gen_tokens, timeout=600, system=""):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": gen_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(url + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    t0 = time.time()
    ttft = None
    first_chunk = None
    usage = {}
    chunks = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    chunks += 1
                    if ttft is None:
                        ttft = time.time() - t0
                        first_chunk = piece[:40]
    total = time.time() - t0
    ttft = ttft if ttft is not None else total
    ptok = usage.get("prompt_tokens")
    ctok = usage.get("completion_tokens") or chunks
    return {
        "ttft_s": round(ttft, 3),
        "total_s": round(total, 3),
        "prompt_tokens": ptok,
        "completion_tokens": ctok,
        "prefill_tok_s": round(ptok / ttft, 1) if ptok and ttft > 0 else None,
        "decode_tok_s": round(ctok / max(total - ttft, 1e-6), 1) if ctok else None,
        "head": first_chunk,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["gateway", "upstream"], default="gateway")
    ap.add_argument("--model", default="qwen3.8-flash-next")
    ap.add_argument("--sizes", default="4096,16384,28672",
                    help="逗号分隔的近似 prompt 长度（tokens）")
    ap.add_argument("--gen", type=int, default=32, help="生成 token 数上限")
    ap.add_argument("--runs", type=int, default=1, help="每个长度重复次数，取中位数")
    ap.add_argument("--system-tokens", type=int, default=0,
                    help="把长前缀放进 system 消息（模拟真实客户端：system 稳定、user 每轮不同）")
    ap.add_argument("--vary-tail", action="store_true",
                    help="每次换结尾提问（模拟真实客户端：长前缀相同、最后一问不同）")
    ap.add_argument("--nonce", action="store_true",
                    help="在正文最前面插入随机串，让整个前缀哈希失效（测冷启动代价）")
    ap.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    url = GATEWAY if args.target == "gateway" else UPSTREAM
    key = api_key(args.key_file) if args.target == "gateway" else "sk-test"
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    rows = []
    for size in sizes:
        okay = []
        for i in range(args.runs):
            tail = (f"第 {i + 1} 次提问：只回答编号 {i + 1}，不要其他内容。"
                    if args.vary_tail else "")
            nonce = (f"批次编号 {time.time_ns()}。" if args.nonce else "")
            prompt = nonce + build_prompt(size, tail)
            system = (nonce + build_prompt(args.system_tokens, "占位")
                      if args.system_tokens else "")
            try:
                r = one_request(url, key, args.model, prompt, args.gen, system=system)
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode("utf-8", "replace")
                print(f"[x] {size} tokens run{i+1}: HTTP {e.code} {body}", file=sys.stderr)
                continue
            except Exception as e:  # noqa: BLE001 —— 基准脚本，任何失败都只记录
                print(f"[x] {size} tokens run{i+1}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            r["target"] = args.target
            r["size_req"] = size
            r["run"] = i + 1
            okay.append(r)
            rows.append(r)
            print(f"[+] {args.target} ~{size:>6} tok: ttft={r['ttft_s']:>7.2f}s "
                  f"prefill={r['prefill_tok_s'] or 0:>7.1f} t/s "
                  f"decode={r['decode_tok_s'] or 0:>6.1f} t/s total={r['total_s']:.2f}s "
                  f"(prompt={r['prompt_tokens']})", file=sys.stderr)
        if len(okay) > 1:
            med = statistics.median(x["ttft_s"] for x in okay)
            print(f"[=] {args.target} ~{size} tok 中位 ttft={med:.2f}s "
                  f"（{len(okay)}/{args.runs} 成功）", file=sys.stderr)

    if args.json:
        print(json.dumps({"target": args.target, "model": args.model, "rows": rows},
                         ensure_ascii=False, indent=2))
    elif rows:
        avg_ttft = statistics.mean(r["ttft_s"] for r in rows)
        print(f"\n{args.target} / {args.model}: {len(rows)} 次，平均 ttft={avg_ttft:.2f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
