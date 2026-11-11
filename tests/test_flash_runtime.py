#!/usr/bin/env python3
"""回归：gmlx 运行时配置（flash-runtime.json / gmlx-serve.yaml / start-flash.py）。

背景（2026-11-11 夜）：
  · 27B 副本从 gmlx 下线 —— 它满速也只有 ~34 tok/s，而 125B 带 MTP 是 60~135
    tok/s，拿它分摊轻量请求反而更慢，还要多占一份常驻内存。
  · 前缀缓存 APC 打开（此前是关的），这是本机最大的一项延迟改善：24k 上下文
    冷启动 ttft≈26s，命中后 0.2s，尾部变体 1.0s。
  · APC 的检查点只能落在"分块网格"上，网格粗细由 prefill_step_size 决定，
    因此它被显式钉在 2048（实测 8192 与 2048 的 prefill 吞吐无差别）。
  · 有些开关只有 env 入口（GMLX_APC_CKPT_*），所以 start-flash.py 要能把
    flash-runtime.json 的 `env` 段注入子进程环境 —— 否则换终端重启后缓存
    行为会静默改变。

期望：
  ① config 模式命令走 --config，且 host/port/no-auth 仍由 json 覆盖
  ② 老的单模型命令行仍然可用（llama.cpp/gmlx 直连的回退路径没被破坏）
  ③ env 段逐项注入，值统一转字符串，NO_PROXY 强制 *，不污染入参
  ④ yaml 不变量：单模型、cache 开、prefill 2048、主力模型 pin+投机、无 27B
  ⑤ json 不变量：engine/config/env 三处齐备，llama.cpp 回退参数仍在

用法：
  python3 tests/test_flash_runtime.py
环境变量：
  REPO_ROOT  仓库根目录（默认本文件上一级）
"""
import importlib.util
import json
import os
import re
import sys

ROOT = os.environ.get("REPO_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def load_start_flash():
    path = os.path.join(ROOT, "tools", "start-flash.py")
    spec = importlib.util.spec_from_file_location("start_flash", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mini_yaml(text):
    """够用就好的 YAML 子集解析：嵌套 mapping + 标量。不处理列表/多行/引号。"""
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val.strip() == "":
            child = {}
            parent[key.strip()] = child
            stack.append((indent, child))
        else:
            v = val.strip()
            if v.lower() in ("true", "false"):
                parent[key.strip()] = v.lower() == "true"
            else:
                try:
                    parent[key.strip()] = int(v)
                except ValueError:
                    parent[key.strip()] = v
    return root


def main():
    sf = load_start_flash()

    print("\n== ① config 模式命令 ==")
    conf = {
        "gmlx": "~/gmlx-env/bin/gmlx",
        "config": "~/.ollama-token-gateway/gmlx-serve.yaml",
        "host": "127.0.0.1", "port": 8090, "no_auth": True,
    }
    cmd = sf.build_cmd(conf)
    check("首参是 serve 子命令", cmd[1] == "serve", cmd)
    check("走 --config 而不是位置参数模型", "--config" in cmd and os.path.expanduser(conf["config"]) in cmd, cmd)
    check("host/port 由 json 覆盖", cmd[cmd.index("--port") + 1] == "8090", cmd)
    check("no_auth 透传 --no-auth", "--no-auth" in cmd, cmd)
    check("不出菜单栏", "--no-menubar" in cmd, cmd)
    check("不再带单模型的模型路径", not any(str(a).endswith(".gguf") for a in cmd), cmd)

    print("\n== ② 老的单模型命令行仍可用 ==")
    legacy = {
        "gmlx": "~/gmlx-env/bin/gmlx",
        "model": "~/models/x.gguf", "mmproj": "~/models/mm.gguf",
        "speculative": True, "draft_gguf": "~/models/d.gguf",
        "speculative_width_cap": 1, "host": "127.0.0.1", "port": 8090,
        "no_auth": True, "thinking": "off",
    }
    c2 = sf.build_cmd(legacy)
    check("位置参数是模型", c2[2].endswith("x.gguf"), c2)
    check("--speculative + --draft-gguf", "--speculative" in c2 and "--draft-gguf" in c2, c2)
    check("--speculative-width-cap 透传", c2[c2.index("--speculative-width-cap") + 1] == "1", c2)
    check("--thinking 透传", c2[c2.index("--thinking") + 1] == "off", c2)

    print("\n== ③ env 注入 ==")
    e = sf.build_env({"env": {"GMLX_APC_CKPT_INTERVAL": "2048", "X_INT": 7}}, base={})
    check("字符串值注入", e.get("GMLX_APC_CKPT_INTERVAL") == "2048", e)
    check("非字符串值转 str", e.get("X_INT") == "7", e)
    check("NO_PROXY 强制 *", e.get("NO_PROXY") == "*", e)
    base = {"KEEP": "1"}
    sf.build_env({"env": {"A": "b"}}, base=base)
    check("不污染调用方字典", base == {"KEEP": "1"}, base)
    check("无 env 段时不报错", sf.build_env({}, base={})["NO_PROXY"] == "*")

    print("\n== ④ gmlx-serve.yaml 不变量 ==")
    ypath = os.path.join(ROOT, "gmlx-serve.yaml")
    with open(ypath, encoding="utf-8") as fh:
        y = mini_yaml(fh.read())
    try:                      # 有 PyYAML 就顺带交叉校验自家解析器
        import yaml as _yaml
        real = _yaml.safe_load(open(ypath, encoding="utf-8"))
        check("mini_yaml 与 PyYAML 一致（server 段）", real["server"] == y["server"],
              f"{real['server']} vs {y['server']}")
    except ImportError:
        print("  （无 PyYAML，跳过交叉校验）")
    srv = y.get("server", {})
    # 2026-11-11 深夜：泳道池要两条轨道，max_models 提到 2（125B + 27B 都 pin 常驻）
    check("两模型：max_models == 2", srv.get("max_models") == 2, srv)
    check("前缀缓存开启", (srv.get("cache") or {}).get("enabled") is True, srv.get("cache"))
    check("prefill 分块 2048（决定检查点网格）", srv.get("prefill_step_size") == 2048, srv)
    models = y.get("models", {})
    check("模型集合 = 主力 + 第二泳道",
          list(models) == ["qwen3.8-flash-next", "qwen3.8-27b-mlx"], list(models))
    main = models.get("qwen3.8-flash-next", {})
    check("主力模型 pin 常驻", main.get("pin") is True, main)
    check("主力模型带 MTP 投机", main.get("speculative") is True and "draft_gguf" in main, main)
    lane2 = models.get("qwen3.8-27b-mlx", {})
    check("第二泳道也 pin 常驻（否则被 max_models 换出，泳道形同虚设）", lane2.get("pin") is True, lane2)
    check("第二泳道不带投机（否则同样会踩 mid-flight 注入缺陷）",
          not lane2.get("speculative"), lane2)
    check("第二泳道配了 mmproj（它是 VLM，缺了起不来）", "mmproj" in lane2, lane2)
    check("没有把已证实不可用的 gemma4-9b 留在生产（基座模型不会聊天）",
          "gemma4-9b" not in models, list(models))
    check("不在上游配 aliases（别名只交网关）",
          not y.get("aliases") and "aliases" not in open(ypath, encoding="utf-8").read().split("#")[0],
          y.get("aliases"))

    print("\n== ⑤ flash-runtime.json 不变量 ==")
    with open(os.path.join(ROOT, "flash-runtime.json"), encoding="utf-8") as fh:
        rt = json.load(fh)
    check("engine 是 gmlx", rt.get("engine") == "gmlx", rt.get("engine"))
    check("config 指向 yaml", str(rt.get("config", "")).endswith("gmlx-serve.yaml"), rt.get("config"))
    env = rt.get("env") or {}
    check("注入检查点步长", env.get("GMLX_APC_CKPT_INTERVAL") == "2048", env)
    check("注入链上重型检查点配额", int(env.get("GMLX_APC_CKPT_HEAVY", "0")) >= 4, env)
    check("env 值全是字符串（Popen 要求）", all(isinstance(v, str) for v in env.values()), env)
    fb = rt.get("fallback") or {}
    check("llama.cpp 回退参数仍在", fb.get("engine") == "llama.cpp" and fb.get("argv"), fb.get("engine"))

    print("\n== ⑥ 回滚映射覆盖新配置 ==")
    rb = open(os.path.join(ROOT, "tools", "rollback.py"), encoding="utf-8").read()
    mapped = set(re.findall(r'"([^"]+\.(?:py|json|yaml|plist))":\s*\w+ /', rb))
    check("gmlx-serve.yaml 在回滚白名单里", "gmlx-serve.yaml" in mapped, sorted(mapped))
    check("flash-runtime.json 在回滚白名单里", "flash-runtime.json" in mapped, sorted(mapped))
    missing = [m for m in mapped if not os.path.exists(os.path.join(ROOT, m))]
    check("白名单里的仓库文件都存在", not missing, missing)

    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
