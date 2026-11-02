# gateway —— 本地局域网 OpenAI 兼容 token 网关

`gateway` 是一个**单文件、纯 Python 标准库**实现的 OpenAI 兼容 token 网关，面向**本地局域网 / Tailscale 内网**使用：统一代理 Ollama 与 Rapid-MLX 上游，做 per-key 配额 / QPS / 限流，并提供 Web 管理台与 `/api/admin/*` 管理接口。附带 `mem-guard.mjs` 设备内存守卫。

- **服务端**：`token-gateway.py`（无第三方依赖，Python 3.10+）
- **配置样例**：`config.example.json`（真实 `config.json` 含密钥，被 `.gitignore` 排除）
- **运维工具**：`mem-guard.mjs` + `com.user.memguard.plist`
- **推送工具**：`tools/push-via-api.py`（Git Data API，绕过被拦的 `github.com:443`）

> 仅用于**可信内网**，不要直接暴露到公网。安全细则见 [安全](#安全)。

## 范围 / Scope

- 本仓库是**本地局域网（LAN）**使用的 OpenAI 兼容 token 网关的「配置 + 运维 + 服务端源码」家目录：含网关服务端 `token-gateway.py`（单文件、纯标准库）、脱敏配置样例、设备内存守卫 `mem-guard.mjs`、LaunchAgent 模板与 Git Data API 推送工具。
- 网关服务端的**源码 `token-gateway.py` 已纳入本仓库**（单文件、纯 Python 标准库，`git clone` 后可直接运行）；但运行期密钥 `config.json`、TLS 证书 `certs/`、`clients/`、`logs/` 仍由 `.gitignore` 排除、不随仓库分发。
- 仅用于可信的内网环境，不要直接暴露到公网。

## 部署 / Deploy

本仓库自带网关服务端 `token-gateway.py`：**单文件、纯 Python 标准库、无第三方依赖**（无需 `pip install`）。

### 前置条件
- Python 3.10+（仅标准库）
- 上游推理服务（按需）：
  - Ollama，默认 `http://127.0.0.1:11434`
  - Rapid-MLX（多模态 VLM 上游），默认 `http://127.0.0.1:8090` 与 `http://127.0.0.1:8088`

### 快速开始（命令行）
```bash
git clone https://github.com/alexjingwen/gateway
cd gateway
# 1) 准备配置（config.json 含真实 key，已被 .gitignore 排除，不入库）
mkdir -p ~/.ollama-token-gateway
cp config.example.json ~/.ollama-token-gateway/config.json
#    编辑 config.json：填写 keys.* 与 admin.token（字段含义见「配置字段说明」，安全要求见「安全」）
# 2) 启动（默认监听 0.0.0.0:3000；局域网/内网用 TG_DISABLE_HTTPS=1 关闭 HTTPS）
TG_DISABLE_HTTPS=1 TG_PORT=3000 python3 token-gateway.py
```
验证：`curl localhost:3000/health`、`curl localhost:3000/v1/models`。

### 环境变量（TG_*）
| 变量 | 默认 | 说明 |
|---|---|---|
| TG_HOST | 0.0.0.0 | 监听地址 |
| TG_PORT | 3000 | 监听端口 |
| TG_DISABLE_HTTPS | (空) | 设为 1 关闭 HTTPS |
| TG_OLLAMA_HOST | http://127.0.0.1:11434 | Ollama 上游 |
| TG_RAPID_MLX_HOSTS | http://127.0.0.1:8002,http://127.0.0.1:8088 | Rapid-MLX 上游（多节点逗号分隔） |
| TG_RAPID_MLX_VLM_HOSTS | http://127.0.0.1:8088 | 多模态 VLM 专用上游 |
| TG_MLX_VLM_API_KEY | sk-test | 访问 VLM 上游所需的 key |
| TG_MODEL_ALIASES | (空) | 模型别名，如 qwen3.5-122b=mlx-community/Qwen3.5-122B-A10B-4bit |
| TG_QPS | 5 | 每 key 每秒请求数 |
| TG_CONCURRENCY | 10 | 全局最大并发 |
| TG_READ_TIMEOUT | 900 | 上游读取超时（秒） |
| TG_VLM_READ_TIMEOUT | 120 | 多模态请求读取超时（秒） |
| TG_CONFIG_DIR | ~/.ollama-token-gateway | config.json / certs / logs 所在目录 |

> **端口/地址优先级**：`config.json` 的 `global.listen_host` / `global.listen_port` 高于 `TG_HOST` / `TG_PORT` 环境变量。通常直接改 `config.json` 即可，无需动环境变量。

### macOS 后台运行（launchd）
仓库内含 `com.token-gateway.server.plist` 模板：
```bash
cp com.token-gateway.server.plist ~/Library/LaunchAgents/
# 编辑 plist：把 WorkingDirectory 与 ProgramArguments 的路径改为你的实际 clone 位置，
# 并按本机环境修改 TG_RAPID_MLX_HOSTS 等上游地址
launchctl load ~/Library/LaunchAgents/com.token-gateway.server.plist
launchctl start com.token-gateway.server
# 查看状态 / 日志
launchctl list | grep token-gateway
tail -f /tmp/token-gateway.log
```

## API 接口概览

### 公开端点（无需鉴权）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` `/admin` `/admin/` | Web 管理台（浏览器打开） |
| GET | `/admin/chat` | 管理台内置聊天页 |
| GET | `/health` `/healthz` | 健康检查，返回 `{"status":"ok","version":"1.0.0","uptime":<秒>}` |

### OpenAI 兼容端点（鉴权：`Authorization: Bearer <key>`）
`<key>` 必须是 `config.json` 中 `keys` 里存在且 `active: true` 的 key，否则返回 `401` / `403`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 合并 Ollama + Rapid-MLX 的模型列表（OpenAI 格式） |
| POST | `/v1/chat/completions` | 聊天补全，支持 `stream=true` 流式 |
| POST | `/v1/completions` | 文本补全 |
| POST | `/v1/embeddings` | 向量（代理到上游） |
| — | `/models` `/chat/completions` `/completions` `/embeddings` | 上述 `/v1/*` 的别名 |
| — | `/api/tags` `/api/pull` `/api/delete` | Ollama 兼容入口，代理到 Ollama 上游 |

```bash
KEY=sk-ollama-xxxxxxxx
# 列出模型
curl http://127.0.0.1:3000/v1/models -H "Authorization: Bearer $KEY"
# 聊天补全
curl http://127.0.0.1:3000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"gemma4:latest","messages":[{"role":"user","content":"hi"}]}'
```

### 管理端点（鉴权：`X-Admin-Token: <admin.token>`）
当 `admin.token` 为空时**跳过校验**（依赖局域网 / Tailscale 网络层鉴权，见 [安全](#安全)）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/status` | 服务器状态 + 各 key 用量 |
| GET | `/api/admin/usage?days=7` | 历史用量统计 |
| POST | `/api/admin/keys` | 新增 / 更新 key |
| GET | `/api/admin/keys/list` | 完整 key 列表（含明文 key，慎用） |
| DELETE | `/api/admin/keys/<prefix>` | 按前缀删除 key |
| GET | `/api/admin/models` | 列出已安装模型（合并 Ollama + Rapid-MLX） |
| POST | `/api/admin/models/pull` | 拉取模型（异步任务） |
| GET | `/api/admin/models/pull/status` | 拉取进度 |
| DELETE | `/api/admin/models/pull/<taskId>` | 取消拉取 |
| DELETE | `/api/admin/models/<name>` | 删除模型 |
| GET | `/api/admin/recent-logs?limit=50` | 最近请求日志 |

```bash
curl http://127.0.0.1:3000/api/admin/status -H "X-Admin-Token: $ADMIN_TOKEN"
```

## 配置字段说明（config.example.json）

`config.json` 顶层三块：`keys` / `global` / `admin`（`_comment` 为自由备注，服务端忽略）。

### `keys` —— 键为 API key 明文，值为该 key 的配置
| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `name` | string | `""` | 备注名（仅展示） |
| `daily_tokens` | int | `0` | 每日 token 上限；`0` = 不限 |
| `monthly_tokens` | int | `0` | 每月 token 上限；`0` = 不限 |
| `qps` | int | `5` | 该 key 每秒请求数上限 |
| `rate_limited` | bool | `false` | 是否启用速率限制 |
| `active` | bool | `true` | 是否启用该 key（`false` 时请求返回 403） |
| `created` | string | 今天 | 创建日期（仅展示） |

### `global` —— 全局运行参数
| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_concurrency` | int | `10`（`TG_CONCURRENCY`） | 全局最大并发请求数 |
| `allow_models` | string[] | `["*"]` | 允许的模型；`*` = 全部 |
| `deny_models` | string[] | `[]` | 拒绝的模型 |
| `listen_host` | string | `0.0.0.0` | 监听地址 |
| `listen_port` | int | `3000` | 监听端口（优先于 `TG_PORT`） |

### `admin`
| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `token` | string | `""` | `/api/admin/*` 鉴权 token；空 = 跳过（走网络层鉴权） |

### 最小可用示例
```json
{
  "keys": {
    "sk-ollama-please-change-me": { "name": "my-laptop" }
  },
  "global": { "listen_host": "127.0.0.1", "listen_port": 3000, "allow_models": ["*"] },
  "admin": { "token": "" }
}
```
> 未写出的字段使用默认值（如 `active: true`、`daily_tokens: 0`、`qps: 5`）。生产环境请把 key 换成长随机串，并按 [安全](#安全) 设置 `admin.token`。

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

### 安装为 LaunchAgent（`com.user.memguard.plist`）

模板已放在仓库根目录，安装步骤：

```bash
# 1) 复制模板到用户级 LaunchAgents
cp com.user.memguard.plist ~/Library/LaunchAgents/

# 2) 编辑 plist：把 ProgramArguments 里的 node 路径与 mem-guard.mjs 路径改成本机真实路径，
#    并确认 --log 与 WorkingDirectory 指向你希望的工作目录（默认 ~/.ollama-token-gateway）
#    例：<string>/opt/homebrew/bin/node</string>
#        <string>/Users/你的用户名/.ollama-token-gateway/mem-guard.mjs</string>

# 3) 加载并启动
launchctl load  ~/Library/LaunchAgents/com.user.memguard.plist
launchctl start com.user.memguard

# 4) 查看状态 / 日志
launchctl list | grep memguard
tail -f ~/.ollama-token-gateway/mem-guard.log

# 卸载
launchctl unload ~/Library/LaunchAgents/com.user.memguard.plist
```

> 想让"越线就重启网关"，在 `ProgramArguments` 里补 `--cmd "重启命令"` 即可（注意幂等，别写成会自杀的循环）。

### 与网关的关系

网关本身监听 `:3000`、按 `config.json` 做 token 配额与限流。mem-guard 是**旁路守卫**：它看的是整台设备内存，越线时通过日志 / webhook / 命令提醒人或触发既定动作，**不直接改动网关进程**。

## 安全

本仓库面向**可信内网**。部署前请逐条确认：

1. **管理接口鉴权（admin.token）**
   - `admin.token` 为空时，`/api/admin/*` **无鉴权**——仅当你已用 Tailscale / VPN / 内网隔离做网络层鉴权时才安全。
   - 若网关会暴露到不可信网络，**必须**设置强 `admin.token`（建议 32+ 位随机串）。
2. **不要暴露公网**：优先 Tailscale（`*.ts.net`）/ 局域网 / VPN。确需公网时，至少启用 HTTPS 并设强 `admin.token`。
3. **密钥隔离**：真实 `config.json`（含 `sk-ollama-*`）、`certs/`（TLS 私钥）、`logs/` 均被 `.gitignore` 排除，**切勿提交**。
4. **源码无内置密钥**：`token-gateway.py` 的 `DEFAULT_CONFIG.keys` 为空（fail-closed），首次运行不会写入任何真实 key。
5. **令牌不入日志**：启动横幅与日志不打印 admin token / API key 明文。
6. **传输加密**：默认可用 HTTPS（需 `certs/server.crt|key`）；内网可用 `TG_DISABLE_HTTPS=1` 走明文 HTTP。
7. **最小权限**：为每个调用方分配独立 key，设 `qps` / `daily_tokens` / `monthly_tokens` 限制，便于审计与止损。

## 推送到 GitHub

这台机器的出网代理**拦 `github.com:443`**（直连超时、CONNECT 502），所以 **`git push` 用不了**，`git fetch` 也拿不到 `origin` ref。改用 Git Data API 推：

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

> ⚠️ 若仓库还不存在：本机两个凭据（`~/.git-credentials` 与 `~/.netrc`）都是 fine-grained PAT，实测**均没有建库权限**（`POST /user/repos` → 403）。请在 GitHub 网页上新建**空仓库**（不要勾 README / .gitignore），再跑 `python3 tools/push-via-api.py`。若推送仍 403，说明该 PAT 被限定为「Only select repositories」，需要把新仓库加进它的授权列表。

---

## 仓库来源 / Provenance

本仓库（**gateway**）由 `auto-florr` 仓库中的 `ollama-token-gateway/` 目录迁移而来（2026-10-14），即原项目中寄存的「openaiGateway」代码——一个 OpenAI 兼容的 token 网关，监听 `:3000` 做配额/限流，配套 `mem-guard.mjs` 设备内存守卫。

- **源**：`alexjingwen/auto-florr` 的 `main` 分支，与本地 `/Users/claw/.ollama-token-gateway/` 工作副本逐字节一致。
- **迁移约束**：本机 `github.com:443` 被代理拦截、仅 `api.github.com` 可达，故推送走 `tools/push-via-api.py`（Git Data API）。
- **未纳入**：`config.json`（含真实 token）、`certs/`、`clients/`、`logs/` 等运维密钥在 `.gitignore` 中，不随仓库分发。
- **服务端源码**：`token-gateway.py` 已纳入本仓库（单文件、纯标准库），`git clone` 后可直接运行。
