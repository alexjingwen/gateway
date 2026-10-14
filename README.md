# ollama-token-gateway · 运维工具：mem-guard

这个目录是 **ollama-token-gateway**（模型管理 / token gateway）的运行与运维目录。
本仓库**只纳入运维侧工具与脱敏配置**；真实的 `config.json`（含 `sk-ollama-***` token）、
TLS 私钥 `certs/server.key`、Tailscale 客户端二进制等均在 `.gitignore` 中，不会进入版本库。

## mem-guard.mjs —— 设备级内存守卫

监控**整台设备**的物理内存使用率，在触及上限前报警 / 采取行动，把"事后 OOM 救火"变成"事前预警"。

### 监控口径（macOS，对齐活动监视器「内存」）

```
使用量 = 匿名页(Anonymous) + 连线内存(Wired) + 压缩占用(Compressor occupied)
上限   = hw.memsize（整台设备物理内存）
使用率 = 使用量 / 上限
```

> 用的是「活动监视器口径」，不是 free 反推——free 把 inactive / purgeable 也算闲，会低估占用。

### 用法

```bash
# 一次性采样，打印 JSON
node mem-guard.mjs --sample

# 持续监控：每 30s 采样，越 90% 即写告警到 mem-guard.log
node mem-guard.mjs --watch 30 --at 0.9 --log mem-guard.log

# 越线时额外执行一条命令（例如重启网关，需自己保证幂等）
node mem-guard.mjs --watch 30 --at 0.9 --cmd "your-restart-command"

# 越线时把告警 POST 到一个 webhook
node mem-guard.mjs --watch 30 --at 0.9 --webhook https://your-endpoint/alert

# 回归自测（合成 vm_stat，不碰真机）
node mem-guard.mjs --selftest
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--at` | `0.9` | 触发阈值（占上限比例）。`0.9` = 90% |
| `--watch N` | 关 | 每 N 秒采样一次（不填则只采样一次） |
| `--cmd "..."` | 无 | 越线时执行的命令 |
| `--webhook URL` | 无 | 越线时 POST JSON 告警 |
| `--log PATH` | 无 | 越线 JSONL 追加路径 |
| `--fail-open` / `--no-fail-open` | fail-open | 读不到内存时是否静默放行（后者会显式报错，适合 CI） |

### 设计取舍

- **只依赖 node 内置**（`child_process` / `fs`），macOS 用 `sysctl` + `vm_stat` 原生读数，零安装。
- **fail-soft**：探针不可用（非 macOS / 沙箱）时默认静默放行，绝不把调用方搞崩；`--no-fail-open` 可反转。
- **只观察 + 可选动作**：越线保护不替网关决定业务策略（网关自己的限流在网关侧）；这里负责"看见 + 喊一声"。
- **常驻建议**：想 7×24 监控整台设备，用 `com.user.memguard.plist` 模板装一个 LaunchAgent（见文件内说明）。

### 与网关的关系

网关本身监听 `:3000`、按 `config.json` 做 token 配额与限流。mem-guard 是**旁路守卫**：
它看的是整台设备内存，越线时通过日志 / webhook / 命令提醒人或触发既定动作，**不直接改动网关进程**。
（整台设备 256GB 时，90% 阈值约 230GB；真机实测当前占用约 40%，远低于阈值。）

## 推送到 GitHub

这台机器的出网代理**拦 `github.com:443`**（直连超时、CONNECT 502），所以 **`git push` 用不了**，
`git fetch` 也拿不到 `origin` ref。改用 Git Data API 推：

```bash
python3 tools/push-via-api.py                 # auto：空仓库走 bootstrap，已有提交走 sync-tree
python3 tools/push-via-api.py --dry-run       # 只看会推什么
python3 tools/push-via-api.py --create        # 仓库不存在时尝试用 API 创建（需要 PAT 有建库权限）
python3 tools/push-via-api.py --repo alexjingwen/other --prefix subdir
                                               # 整个项目塞进**别的**仓库的子目录（嵌套时用）
```

PAT 运行时从 `~/.git-credentials` 读取，只走内存，不落盘、不打印。

两种模式的区别：

| 模式 | 触发条件 | 行为 |
|---|---|---|
| `bootstrap` | 远端分支不存在（刚建的空仓库） | 建 blob → tree → **无父**根提交 → 创建 `refs/heads/main`。会把本地多个提交压成一个 |
| `sync-tree` | 远端已有提交 | 以远端 tip 为 base_tree 补齐本地条目，**只增改，绝不删** |

> ⚠️ 若仓库还不存在：本机两个凭据（`~/.git-credentials` 与 `~/.netrc`）都是 fine-grained PAT，
> 实测**均没有建库权限**（`POST /user/repos` → 403）。请在 GitHub 网页上新建**空仓库**
> （不要勾 README / .gitignore），再跑 `python3 tools/push-via-api.py`。
> 若推送仍 403，说明该 PAT 被限定为「Only select repositories」，需要把新仓库加进它的授权列表。

---

## 仓库来源 / Provenance

本仓库（**gateway**）由 `auto-florr` 仓库中的 `ollama-token-gateway/` 目录迁移而来（2026-10-14），
即原项目中寄存的「openaiGateway」代码——一个 OpenAI 兼容的 token 网关，监听 `:3000` 做配额/限流，
配套 `mem-guard.mjs` 设备内存守卫。

- **源**：`alexjingwen/auto-florr` 的 `main` 分支，与本地 `/Users/claw/.ollama-token-gateway/` 工作副本逐字节一致。
- **迁移约束**：本机 `github.com:443` 被代理拦截、仅 `api.github.com` 可达，故推送走 `tools/push-via-api.py`（Git Data API）。
- **未纳入**：`config.json`（含真实 token）、`certs/`、`clients/`、`logs/` 等运维密钥在 `.gitignore` 中，不随仓库分发。
