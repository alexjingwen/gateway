#!/usr/bin/env python3
"""回归：管理面板的「系统资源」卡片（整机内存 / GPU / 进程 RSS）不能再次消失。

事故背景（2026-11-14 13:00 发现）：这块功能 300 行，**只存在于生产副本、没进仓库**。
12:42 用仓库版部署（API key 优先级那轮）时整块被覆盖，面板上的「💾 系统资源」直接
没了，而 `collect_system_metrics()` 也不再被 `/api/admin/status` 调用 —— 没有任何报错，
只是"功能静默消失"。所以这一组断言分两层：

  ① **链路存在性**（静态）：源码里必须同时有采集端、注入端、面板 DOM 与渲染函数。
     少任何一环都说明这条链路断了，且断开方式不会报错 —— 只能靠断言盯住。
  ② **口径与降级**（行为）：`used = active + wired + compressed`（对齐活动监视器），
     采不到就 `{"ok": false}` 而不是抛异常（面板不能因为一个子进程失败整页崩）。

用法：
  python3 tests/test_system_metrics.py
环境变量：
  TG_SRC  被测网关文件，默认仓库根的 token-gateway.py
"""
import ast
import ctypes
import ctypes.util
import os
import re
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("TG_SRC") or os.path.join(os.path.dirname(HERE), "token-gateway.py")

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}  {detail}")


WANTED_FUNCS = {"_sh_text", "_mem_stats", "_gpu_stats", "_swap_stats",
                "_top_process_rss", "_libproc_init", "collect_system_metrics"}
WANTED_CLASSES = {"_ProcTaskInfo"}
WANTED_ASSIGNS = {"_LIBPROC", "PROC_ALL_PIDS", "PROC_PIDTASKINFO",
                  "_SYS_CACHE", "_SYS_CACHE_LOCK", "SYS_CACHE_TTL"}


def load_module(src_path):
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in WANTED_FUNCS:
            nodes.append(n)
        elif isinstance(n, ast.ClassDef) and n.name in WANTED_CLASSES:
            nodes.append(n)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(getattr(t, "id", "") in WANTED_ASSIGNS for t in tgts):
                nodes.append(n)
    found = {n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    missing = (WANTED_FUNCS | WANTED_CLASSES) - found
    if missing:
        raise SystemExit(f"源码里找不到这些定义：{sorted(missing)}")
    ns = {"ctypes": ctypes, "ctypes_util": ctypes.util, "subprocess": subprocess,
          "threading": threading, "time": time, "re": re, "sys": sys,
          "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple}
    for n in nodes:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "<gateway>", "exec"), ns)
    return ns


NS = load_module(SRC)
SRC_TEXT = open(SRC, encoding="utf-8").read()
collect = NS["collect_system_metrics"]
mem_stats = NS["_mem_stats"]
top_procs = NS["_top_process_rss"]

print("1. 链路存在性：采集 → 接口 → DOM → 渲染，四段缺一不可")
check("采集函数 collect_system_metrics 存在", "def collect_system_metrics" in SRC_TEXT)
check("/api/admin/status 真的带上 system 字段",
      '"system": collect_system_metrics(),' in SRC_TEXT)
check("面板有 💾 系统资源 卡片", 'id="sysCard"' in SRC_TEXT and "系统资源" in SRC_TEXT)
check("有渲染函数 renderSys", "function renderSys" in SRC_TEXT)
check("仪表盘真的调用 renderSys(d.system)", "renderSys(d.system);" in SRC_TEXT)
check("样式类 .res-bar 存在（卡片不是裸文字）", ".res-bar" in SRC_TEXT)
check("ctypes 已 import（进程 RSS 靠它，不是 ps）",
      "import ctypes\n" in SRC_TEXT and "import ctypes.util" in SRC_TEXT)

print("\n2. 内存口径：used = active + wired + compressed（对齐活动监视器）")
PAGE = 16384
FAKE = f"""Mach Virtual Memory Statistics: (page size of {PAGE} bytes)
Pages free:                              100000.
Pages active:                           1000000.
Pages inactive:                          200000.
Pages speculative:                        50000.
Pages wired down:                        300000.
Pages purgeable:                          10000.
"Translation faults":                  99999999.
Pages occupied by compressor:             80000.
"""
NS["_sh_text"] = lambda argv, timeout=4.0: (
    FAKE if argv and "vm_stat" in argv[0]
    else ("274877906944" if "hw.memsize" in argv else ""))
m = mem_stats()
check("解析成功", isinstance(m, dict), m)
check("total = hw.memsize", m and m["total"] == 274877906944, m and m["total"])
check("active 按页换算", m and m["active"] == 1000000 * PAGE, m and m["active"])
check("wired 取 pages wired down", m and m["wired"] == 300000 * PAGE, m and m["wired"])
check("compressed 取 occupied by compressor",
      m and m["compressed"] == 80000 * PAGE, m and m["compressed"])
check("used 是三者之和（不是 1-free）",
      m and m["used"] == (1000000 + 300000 + 80000) * PAGE, m and m["used"])
check("used_pct 保留一位小数", m and m["used_pct"] == round(m["used"] / m["total"] * 100, 1),
      m and m["used_pct"])

print("\n3. 采集失败必须降级，不能抛（面板整页不能因为一个子进程挂掉）")
NS["_sh_text"] = lambda argv, timeout=4.0: ""
check("vm_stat 读不到 → None", mem_stats() is None)
NS["_sh_text"] = lambda argv, timeout=4.0: "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
check("缺 pages active → None（不猜 0）", mem_stats() is None)

NS["_mem_stats"] = lambda: None
NS["_SYS_CACHE"]["data"] = None
d = collect()
check("快照降级为 ok=false 而不是抛异常", d.get("ok") is False, d)
check("降级快照仍带 ts 与 error", "ts" in d and d.get("error") == "unavailable", d)
NS["_mem_stats"] = mem_stats

print("\n4. 真实采集（本机）与缓存")
if sys.platform != "darwin":
    print("  ⏭  非 macOS，跳过真机采集")
else:
    NS["_sh_text"] = NS["_sh_text"]  # 复位到原始实现
    NS = load_module(SRC)             # 重新加载，拿回没被打桩的 _sh_text
    collect = NS["collect_system_metrics"]
    d = collect()
    if not d.get("ok"):
        print(f"  ⚠️  本机采集不可用（{d.get('error')}），跳过真机断言")
    else:
        check("mem.total ≈ 物理内存（> 1GB）", d["mem"]["total"] > 2**30, d["mem"]["total"])
        check("used 不超过 total", d["mem"]["used"] <= d["mem"]["total"], d["mem"]["used"])
        check("used_pct 在 0~100", 0 <= d["mem"]["used_pct"] <= 100, d["mem"]["used_pct"])
        check("gpu 段存在（Apple Silicon 统一内存，无独立 VRAM）", "gpu" in d and isinstance(d["gpu"], dict))
        check("swap 段存在", "swap" in d and isinstance(d["swap"], dict))
        procs = d.get("procs") or []
        if procs:
            check("进程列表按 RSS 降序", all(procs[i]["rss"] >= procs[i+1]["rss"]
                                          for i in range(len(procs)-1)),
                  [p["rss"] for p in procs])
            check("每行有 pid/name/rss", all({"pid", "name", "rss"} <= set(p) for p in procs))
            check("RSS 至少有一项 > 100MB（不是全零）", procs[0]["rss"] > 100 * 2**20, procs[0])
        else:
            print("  ⚠️  libproc 在本环境不可用（沙箱常见），跳过进程列表断言")
        ts1 = collect()["ts"]
        ts2 = collect()["ts"]
        check("2s 内命中缓存（同一快照，不重复起子进程）", ts1 == ts2, (ts1, ts2))

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
