# gateway —— 本地局域网 OpenAI 兼容 token 网关

`gateway` 是一个**单文件、纯 Python 标准库**实现的 OpenAI 兼容 token 网关，面向**本地局域网 / Tailscale 内网**使用：统一代理 Ollama 与 Rapid-MLX 上游，做 per-key 配额 / QPS / 限流，并提供 Web 管理台与 `/api/admin/*` 管理接口。附带 `mem-guard.mjs` 设备内存守卫。

- **服务端**：`token-gateway.py`（无第三方依赖，Python 3.10+）
- **auto 虚拟模型**：客户端把 `model` 设为 `auto`，由网关自动从本地模型池分配，免去手动切换（详见 [auto 虚拟模型](#auto-虚拟模型自动分配)）
- **配置样例**：`config.example.json`（真实 `config.json` 含密钥，被 `.gitignore` 排除）
- **运维工具**：`mem-guard.mjs` + `com.user.memguard.plist`
- **推送工具**：`tools/push-via-api.py`（Git Data API，绕过被拦的 `github.com:443`）

> 仅用于**可信内网**，不要直接暴露到公网。安全细则见 [安全加固](#安全加固)。

## 目录

- [范围 / Scope](#范围--scope)
- [部署 / Deploy](#部署--deploy)
- [API 参考](#api-参考)
- [auto 虚拟模型（自动分配）](#auto-虚拟模型自动分配)
- [配置字段速查](#配置字段速查)
- [安全加固](#安全加固)
- [mem-guard.mjs —— 设备级内存守卫](#mem-guardmjs--设备级内存守卫)
- [故障排查 / 常见问题](#故障排查--常见问题)
- [运维细节（日志 / 权限 / 轮转）](#运维细节日志--权限--轮转)
- [推送到 GitHub](#推送到-github)
- [仓库来源 / Provenance](#仓库来源--provenance)

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
#    编辑 config.json：填写 keys.* 与 admin.token（字段含义见「配置字段速查」，安全要求见「安全加固」）
# 2) 启动（默认监听 0.0.0.0:3000；局域网/内网用 TG_DISABLE_HTTPS=1 关闭 HTTPS）
TG_DISABLE_HTTPS=1 TG_PORT=3000 python3 token-gateway.py
```
验证：`curl localhost:3000/health`、`curl localhost:3000/v1/models`。

### 环境变量（TG_*）
| 变量 | 默认 | 说明 |
|---|---|---|
| TG_HOST | 0.0.0.0 | 监听地址 |
| TG_PORT | 3000 | 监听端口（`config.json` 的 `listen_port` 优先） |
| TG_DISABLE_HTTPS | (空) | 设为 1 关闭 HTTPS（走明文 HTTP） |
| TG_HTTP_PORT | 3001 | 启用 HTTPS 时，此端口做 HTTP→HTTPS 重定向 |
| TG_OLLAMA_HOST | http://127.0.0.1:11434 | Ollama 上游 |
| TG_RAPID_MLX_HOSTS | http://127.0.0.1:8002,http://127.0.0.1:8088 | Rapid-MLX 上游（多节点逗号分隔） |
| TG_RAPID_MLX_VLM_HOSTS | http://127.0.0.1:8088 | 多模态 VLM 专用上游 |
| TG_MLX_VLM_API_KEY | sk-test | 访问 VLM 上游所需的 key |
| TG_MODEL_ALIASES | (空) | 模型别名，如 qwen3.5-122b=mlx-community/Qwen3.5-122B-A10B-4bit |
| TG_AUTO_ENABLED | 1 | `auto` 虚拟模型开关；设 0/false/no/off 关闭 |
| TG_AUTO_STRATEGY | least-busy | `auto` 分配策略：`least-busy`（默认，繁忙规避）\| `round-robin` \| `random` |
| TG_AUTO_BUSY_THRESHOLD | 1 | 繁忙阈值：在飞请求数 ≥ 此值即跳过该模型（默认 1 = 有人正在用就换） |
| TG_AUTO_EXCLUDE | (空) | `auto` 额外排除的模型（逗号分隔，叠加在 `deny_models` 与 `global.auto_exclude` 之上） |
| TG_QPS | 5 | 每 key 每秒请求数 |
| TG_CONCURRENCY | 10 | 全局最大并发 |
| TG_READ_TIMEOUT | 900 | 上游读取超时（秒） |
| TG_VLM_READ_TIMEOUT | 120 | 多模态请求读取超时（秒） |
| TG_CONNECT_TIMEOUT | 10 | 上游连接超时（秒） |
| TG_CONFIG_DIR | ~/.ollama-token-gateway | config.json / certs / logs 所在目录 |

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

## API 参考

所有 `/v1/*` 接口兼容 OpenAI 客户端。鉴权方式：
- **模型发现端点**（`/v1/models`、`/api/tags`）：**免鉴权**，无需 key。
- **推理端点**（`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/api/pull`、`/api/delete`）：`Authorization: Bearer <key>`（也支持 `?api_key=<key>`）；`<key>` 必须是 `config.json` 的 `keys` 中存在且 `active: true` 的 key。
- **管理端点**：`X-Admin-Token: <admin.token>`；当 `admin.token` 为空时**跳过校验**（依赖网络层鉴权，见 [安全加固](#安全加固)）。

> ⚠️ **鉴权头区分**：管理接口用 **`X-Admin-Token`** 头，**不是** `Authorization: Bearer`；后者只用于 `/v1/*` 推理端点。

### 公开端点（无需鉴权）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` `/admin` `/admin/` | Web 管理台（浏览器打开） |
| GET | `/admin/chat` | 管理台内置聊天页 |
| GET | `/health` `/healthz` | 健康检查 |
| GET | `/v1/models` | 模型列表（合并 Ollama + Rapid-MLX，末尾附虚拟模型 `auto`）；**免鉴权** |
| GET | `/api/tags` | Ollama 兼容模型列表；**免鉴权** |

**`GET /health` 响应**
```json
{"status": "ok", "version": "1.0.0", "uptime": 2506690}
```
`uptime` 为服务已运行秒数。

### auto 虚拟模型（自动分配）

网关内置一个虚拟模型名 **`auto`**：客户端不必在多个本地模型之间手动切换，网关会在**每次请求**时从「本地可用模型池」自动挑一个真实模型，然后完全按该模型正常路由（上下文窗口、图片过滤、上游选择、用量记录均按真实模型处理，与直接指定该模型等价）。

**启用**：默认开启。`TG_AUTO_ENABLED=0` 关闭后，`auto` 不再出现在 `/v1/models`，传入 `model: "auto"` 会走常规的存在性检查并返回 404。另外，在 `global.deny_models` 中写入 `"auto"` 等效于禁用 `auto`，请求直接返回 `403 model_not_allowed`。

**候选池规则**（每次请求实时计算；上游模型列表有 30 秒缓存）：

| 请求类型 | 候选池 |
|---|---|
| `/v1/chat/completions`、`/v1/completions` | 全部**对话**模型；自动排除名字含 `embed` / `bge` / `mxbai` / `gte-` / `e5-` / `rerank` 的嵌入、重排模型 |
| 带图请求（`content` 内含 `image_url` / `image` / `input_image`，或顶层 `images`） | 仅**多模态**模型：部署在 `TG_RAPID_MLX_VLM_HOSTS` 上的模型，或名字含 `vl` / `vision` / `llava` / `minicpm-v` 的模型；若无多模态候选，回退文本模型（图片会被网关按既有逻辑过滤） |
| `/v1/embeddings` | 仅**嵌入**模型；池内没有嵌入模型时返回 `503 auto_no_candidate` |

候选池的过滤顺序是：上游已加载 → `global.deny_models` / `global.auto_exclude` / `TG_AUTO_EXCLUDE` → `global.allow_models` → 能力匹配。若 `allow_models` 中**只写了 `auto`**，视为放行整个池子（仍受 `deny_models` 约束）；否则 `auto` 只能在 `allow_models` 列出的模型中选择。

> `global.auto_exclude` 与 `deny_models` 的区别：前者**只影响 `auto` 的候选池**，被排除的模型仍可被客户端显式调用；后者是全局白名单，显式调用也会 403。想「让 auto 别用某个模型」用前者。

**分配策略**：

| 策略 | 行为 |
|---|---|
| `least-busy`（默认） | 先按网关侧在飞请求数筛出**空闲**候选，在空闲集合内轮询；全部繁忙时退化为「选负载最低者」，不会直接失败。全部空闲时与 `round-robin` 完全一致 |
| `round-robin` | 忽略负载，纯轮流（候选按模型名排序，顺序可复现） |
| `random` | 随机挑选 |

繁忙判定基于**网关侧每模型的在飞请求数**（`TG_AUTO_BUSY_THRESHOLD`，默认 1）。含义是「已经有一个请求在使用该模型，就先换一个」，避免多个请求挤在同一模型上排队。这个计数只统计经过本网关的请求——**外部客户端直连上游（例如直接请求 `:11434`）造成的占用，网关无法感知**。

**如何确认选中了谁**：
- 响应头 **`X-Gateway-Model: <真实模型名>`**
- 响应体的 `model` 字段（上游回显的真实模型名）
- 服务日志：`🎲 auto → <模型> @ <上游>（<选择理由>）→ <客户端IP>`

**示例**
```bash
curl -s http://localhost:3000/v1/chat/completions -D - \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}],"stream":false}'
# 响应头会带：X-Gateway-Model: gemma4:latest
```

**注意**
- `auto` 每次请求独立挑选，**同一会话的连续请求可能落到不同模型**。上下文不会跨模型保持（客户端本来就会带全量 `messages`，功能上不受影响），但输出风格与能力可能变化。需要稳定输出时请显式指定模型。
- `least-busy` 的「繁忙」是**网关视角**的并发，不是上游的真实队列深度。若某模型被外部进程占满，网关仍会把它当作空闲模型选中，此时表现为该请求变慢而非被跳过。
- 被 `auto_exclude` 排除的模型**不会**从 `/v1/models` 或管理台消失——它们仍然可用，只是 `auto` 不再分配。这样既避免 `auto` 命中不合适的模型，又不影响显式调用。
- `auto` 只出现在 `/v1/models`，且排在列表**末尾**——不改变「客户端默认取第一个模型」的既有行为；`/api/tags`（Ollama 兼容端点）不含 `auto`。
- Web 管理台的「模型管理」页会额外显示一条 `auto`（紫色 `🎲 auto` 徽章、大小显示为 `—`），它来自 `/api/admin/models` 的虚拟条目，**不可拉取、不可删除**。

### OpenAI 兼容端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | 聊天补全，支持 `stream=true` |
| POST | `/v1/completions` | 文本补全 |
| POST | `/v1/embeddings` | 向量（代理上游） |
| — | `/models`(免鉴权) `/chat/completions` `/completions` `/embeddings` | 上述 `/v1/*` 的别名（`/models` 同 `/v1/models` 免鉴权） |
| — | `/api/pull` `/api/delete` | Ollama 兼容写操作，代理到 Ollama 上游（需 Bearer） |

**`GET /v1/models` 响应（免鉴权）**
```json
{
  "object": "list",
  "data": [
    {"id": "qwen2.5vl:3b", "object": "model", "created": 0, "owned_by": "ollama"},
    {"id": "gemma4:latest", "object": "model", "created": 0, "owned_by": "ollama"},
    {"id": "muse-glimmer", "object": "model", "created": 0, "owned_by": "rapid-mlx"}
  ]
}
```

**`POST /v1/chat/completions` 请求**
```bash
curl http://127.0.0.1:3000/v1/chat/completions \
  -H "Authorization: Bearer sk-ollama-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemma4:latest","messages":[{"role":"user","content":"你好"}],"stream":false}'
```
**非流式响应**（标准 OpenAI 格式，字段以上游为准）
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1730000000,
  "model": "gemma4:latest",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好！"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13}
}
```
**流式响应**（`stream: true`，SSE，每行 `data: {...}`，最后 `data: [DONE]`）
```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你"}}]}
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"好"}}]}
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":8,"completion_tokens":5,"total_tokens":13}}
data: [DONE]
```

**`POST /v1/embeddings` 请求/响应**（代理上游，字段以上游为准）
```bash
curl http://127.0.0.1:3000/v1/embeddings \
  -H "Authorization: Bearer sk-ollama-xxxxxxxx" -H "Content-Type: application/json" \
  -d '{"model":"nomic-embed-text:latest","input":"hello"}'
```
```json
{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0.013,-0.007]}],"model":"nomic-embed-text:latest","usage":{"prompt_tokens":1,"total_tokens":1}}
```

**错误响应格式**（所有错误统一）
```json
{"error": {"message": "无效的 API Key", "type": "invalid_api_key", "code": "invalid_api_key"}}
```
| HTTP | code | 含义 |
|---|---|---|
| 400 | `bad_request` | 请求体不是合法 JSON |
| 401 | `invalid_api_key` | 缺少或无效的 API Key |
| 401 | `admin_auth_failed` | 管理接口 token 错误 |
| 403 | `api_key_disabled` | 该 key 已停用（`active:false`） |
| 429 | `rate_limit_exceeded` | 超出该 key 的 QPS 限制 |
| 429 | `quota_exceeded` | 超出每日/每月 token 配额 |
| 429 | `too_many_requests` | 全局并发已满，请稍后重试 |
| 404 | `key_not_found` | 删除 key 时无匹配 |

> 响应头会暴露用量（CORS `Expose-Headers`）：`X-Token-Usage`、`X-Quota-Remaining`、`X-RateLimit-Limit`。

### 管理端点（`/api/admin/*`）

> 所有 `/api/admin/*` 均需 `X-Admin-Token: <admin.token>`；`admin.token` 为空时无需该头（依赖网络层鉴权）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/status` | 服务器状态 + 各 key 用量 |
| GET | `/api/admin/usage?days=7` | 历史用量统计 |
| POST | `/api/admin/keys` | 新增 / 更新 key |
| GET | `/api/admin/keys/list` | 完整 key 列表（含明文 key，慎用） |
| DELETE | `/api/admin/keys/<prefix>` | 按前缀删除 key |
| GET | `/api/admin/models` | 已安装模型（合并 Ollama + Rapid-MLX） |
| POST | `/api/admin/models/pull` | 拉取模型（异步任务） |
| GET | `/api/admin/models/pull/status` | 拉取进度 |
| DELETE | `/api/admin/models/pull/<taskId>` | 取消拉取 |
| DELETE | `/api/admin/models/<name>` | 删除模型 |
| GET | `/api/admin/recent-logs?limit=50` | 最近请求日志 |

**`GET /api/admin/status` 响应（节选）**
```json
{
  "status": "ok", "uptime": 3600,
  "ollama": "http://127.0.0.1:11434",
  "rapid_mlx_hosts": ["http://127.0.0.1:8090", "http://127.0.0.1:8088"],
  "active_concurrency": 0, "max_concurrency": 256,
  "listen": "0.0.0.0:3000",
  "keys": {
    "sk-ollama-abc...": {"name": "alice", "active": true,
      "daily_used": 1234, "daily_limit": 1000000,
      "monthly_used": 5678, "monthly_limit": 30000000, "daily_requests": 12}
  },
  "log_file": "/Users/you/.ollama-token-gateway/logs/usage.jsonl", "log_size_mb": 0.02
}
```

**`POST /api/admin/keys` 请求**（`key` 省略时服务端自动生成 `sk-ollama-<48hex>`）
```json
{"name": "alice-laptop", "daily_tokens": 1000000, "monthly_tokens": 30000000,
 "qps": 5, "rate_limited": true, "active": true}
```
响应：`{"message":"Key 已保存","key":"sk-ollama-...","name":...,"daily_tokens":...,...}`

**`DELETE /api/admin/keys/<prefix>`** 响应：`{"message":"已删除 1 个 Key","removed":["sk-ollama-..."]}`

```bash
ADMIN_TOKEN=...   # 若 config.json 的 admin.token 为空，可省略此头
curl http://127.0.0.1:3000/api/admin/status -H "X-Admin-Token: $ADMIN_TOKEN"
curl -X POST http://127.0.0.1:3000/api/admin/keys -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"alice","daily_tokens":1000000,"monthly_tokens":30000000,"qps":5,"rate_limited":true,"active":true}'
```

## 配置字段速查

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

> 注：通过 `POST /api/admin/keys` 新建时，服务端默认 `daily_tokens=1000000`、`monthly_tokens=30000000`、`rate_limited=true`；与直接在 `config.json` 手写（默认 `0` / `false`）不同，注意区分。

### `global` —— 全局运行参数
| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_concurrency` | int | `10`（`TG_CONCURRENCY`） | 全局最大并发请求数 |
| `allow_models` | string[] | `["*"]` | 允许的模型；`*` = 全部 |
| `deny_models` | string[] | `[]` | 拒绝的模型（全局生效，显式调用也 403） |
| `auto_exclude` | string[] | `[]` | 仅从 `auto` 候选池排除；不影响显式调用（需重启或 admin 操作后重载生效） |
| `listen_host` | string | `0.0.0.0` | 监听地址 |
| `listen_port` | int | `3000` | 监听端口（优先于 `TG_PORT`） |

### `admin`
| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `token` | string | `""` | `/api/admin/*` 鉴权 token；空 = 跳过（走网络层鉴权） |

### 关于 upstream（上游地址）
`config.json` **不含** upstream 字段——上游地址通过环境变量配置：
- `TG_OLLAMA_HOST`（默认 `http://127.0.0.1:11434`）
- `TG_RAPID_MLX_HOSTS`（默认 `http://127.0.0.1:8002,http://127.0.0.1:8088`，逗号分隔多节点）
- `TG_RAPID_MLX_VLM_HOSTS`（多模态 VLM 专用）
- `TG_MODEL_ALIASES`（模型别名，如 `qwen3.5-122b=mlx-community/Qwen3.5-122B-A10B-4bit`）

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
> 未写出的字段使用默认值（如 `active: true`、`daily_tokens: 0`、`qps: 5`）。生产环境请把 key 换成长随机串，并按 [安全加固](#安全加固) 设置 `admin.token`。

### 环境变量 vs config.json：冲突时的行为
- **优先级**：`config.json` 的 `global.listen_host` / `global.listen_port` **高于** `TG_HOST` / `TG_PORT`。
- `load_config` 只对 `global` / `admin` 用 `setdefault` 填充**缺失**字段；`keys` **不**从默认值合并。
- 示例：

| `config.json` | 环境变量 | 实际生效 |
|---|---|---|
| `listen_port: 3000` | `TG_PORT=8000` | **3000**（config 胜出） |
| 无 `listen_port` | `TG_PORT=8000` | **8000**（用 env 默认） |
| `admin.token: "s3cret"` | — | 管理接口要求 `X-Admin-Token: s3cret` |
| 无 `admin.token` 字段 | — | 默认空 → 管理接口**开放**（依赖网络鉴权） |

## 安全加固

本仓库面向**可信内网**。公开部署前请逐条确认：

### 1. 管理接口鉴权（admin.token）
- `admin.token` 为空时，`/api/admin/*` **无鉴权**——仅当你已用 Tailscale / VPN / 内网隔离做网络层鉴权时才安全。
- 若网关会暴露到不可信网络，**必须**设置强 `admin.token`（建议 32+ 位随机串）：
  ```bash
  # 生成一个强 token 填入 config.json 的 admin.token
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

### 2. 启用 HTTPS（证书放置与自签名）
HTTPS 的判定：**未设 `TG_DISABLE_HTTPS` 且 `certs/server.crt` 与 `certs/server.key` 均存在**时自动启用；否则退回明文 HTTP。

```bash
# 证书目录（默认 ~/.ollama-token-gateway/certs/）
mkdir -p ~/.ollama-token-gateway/certs

# 生成自签名证书（内网用；CN 换成你的主机名/IP）
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout ~/.ollama-token-gateway/certs/server.key \
  -out    ~/.ollama-token-gateway/certs/server.crt \
  -subj "/CN=openmac-studio.taila46635.ts.net"

# 启动时不设 TG_DISABLE_HTTPS 即走 HTTPS（主端口 3000）
python3 token-gateway.py
```
- 启用 HTTPS 时，另起 `TG_HTTP_PORT`（默认 **3001**）做 HTTP→HTTPS 重定向。
- 自签证书需客户端信任（或 `curl -k`）后才能校验通过。

### 3. `TG_DISABLE_HTTPS=1` 的风险
- 关闭 HTTPS 后所有请求（含 `Authorization` 头、API key、对话内容）**明文传输**。
- 仅在**受信任的局域网 / Tailscale / 本机回环**下可接受；跨不可信网络**禁止**使用。

### 4. 内网 vs 外网部署差异
| 场景 | HTTPS | admin.token | 监听地址 | 备注 |
|---|---|---|---|---|
| 本机 / 局域网 | 可用明文（`TG_DISABLE_HTTPS=1`） | 可留空（若已有网络隔离） | `127.0.0.1` 或内网 IP | 最简单 |
| Tailscale 内网 | 建议启用 | 可留空（Tailscale 已鉴权） | `0.0.0.0` | 当前生产形态 |
| 公网 / 不可信网络 | **必须启用** | **必须强 token** | 建议前置反向代理 | 另加防火墙 / 限流 |

### 5. 其他
- **密钥隔离**：真实 `config.json`（含 `sk-ollama-*`）、`certs/`（TLS 私钥）、`logs/` 均被 `.gitignore` 排除，**切勿提交**。
- **源码无内置密钥**：`token-gateway.py` 的 `DEFAULT_CONFIG.keys` 为空（fail-closed），首次运行不写入任何真实 key。
- **令牌不入日志**：启动横幅与日志不打印 admin token / API key 明文。
- **最小权限**：为每个调用方分配独立 key，设 `qps` / `daily_tokens` / `monthly_tokens`，便于审计与止损。

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
node mem-guard.mjs --sample                     # 一次性采样，打印 JSON
node mem-guard.mjs --watch 30 --at 0.9 --log mem-guard.log
node mem-guard.mjs --watch 30 --at 0.9 --cmd "your-restart-command"
node mem-guard.mjs --watch 30 --at 0.9 --webhook https://your-endpoint/alert
node mem-guard.mjs --selftest                   # 合成 vm_stat 回归自测
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--at` | `0.9` | 触发阈值（占上限比例） |
| `--watch N` | 关 | 每 N 秒采样一次（不填则只采样一次） |
| `--cmd "..."` | 无 | 越线时执行的命令 |
| `--webhook URL` | 无 | 越线时 POST JSON 告警 |
| `--log PATH` | 无 | 越线 JSONL 追加路径 |
| `--fail-open` / `--no-fail-open` | fail-open | 读不到内存时是否静默放行（后者显式报错） |

### `--fail-open` 在 CI / 非 macOS 环境的行为
- mem-guard 的读数是 **macOS 专属**（`sysctl` + `vm_stat`）。在 **Linux / 容器 / 沙箱 / 非 macOS** 上探针不可用。
- 默认 `--fail-open`：探针失败时**静默放行、退出码 0**，不会误报、也不会让调用方崩溃——适合把 mem-guard 作为"可选旁路"挂在生产脚本里。
- 若你希望"探针不可用即视为失败"（例如 **CI 校验**），用 `--no-fail-open`，此时会显式报错并以非 0 退出。
- 注意：`--fail-open` 不等于"在非 macOS 上也能监控"——它只是不报错；真实内存监控仅在 macOS 生效。

### 设计取舍

- **只依赖 node 内置**（`child_process` / `fs`），macOS 用 `sysctl` + `vm_stat` 原生读数，零安装。
- **fail-soft**：探针不可用（非 macOS / 沙箱）时默认静默放行，绝不把调用方搞崩；`--no-fail-open` 可反转。
- **只观察 + 可选动作**：越线保护不替网关决定业务策略（网关自己的限流在网关侧）；这里负责"看见 + 喊一声"。

### 安装为 LaunchAgent（`com.user.memguard.plist`）

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

## 故障排查 / 常见问题

### `curl localhost:3000/health` 异常
| 现象 | 可能原因 | 排查 / 处理 |
|---|---|---|
| `Connection refused` | 服务未启动 / 端口不对 | `lsof -nP -iTCP:3000 -sTCP:LISTEN`；确认 `config.json` 的 `listen_port`（它优先于 `TG_PORT`） |
| 进程在但 `/health` 超时 | 绑定地址不对（如只绑 `127.0.0.1` 却从别机访问） | 改 `listen_host` 为 `0.0.0.0` 或内网 IP |
| `/health` 正常但 `/v1/models` 为空 | 上游不可达 | `curl 127.0.0.1:11434/api/tags`；检查 `TG_RAPID_MLX_HOSTS` |
| 请求返回 500 | 上游异常 / 代码异常 | 看 `/tmp/token-gateway.log` 与 `/tmp/token-gateway.err.log` |
| 端口被占用（启动报 `Address already in use`） | 旧实例仍占用 3000 | 停旧实例，或改 `config.json` 的 `listen_port` |

### GitHub 推送相关
| 现象 | 可能原因 | 处理 |
|---|---|---|
| `git push` 超时 / CONNECT 502 | 本机代理拦 `github.com:443` | 改用 `python3 tools/push-via-api.py`（Git Data API，仅需 `api.github.com`） |
| `POST /user/repos` 返回 403 | fine-grained PAT 无建库权限 | 在 GitHub 网页新建**空仓库**（不勾 README/.gitignore），再推送 |
| 推送 403（仓库已存在） | PAT 限定「Only select repositories」 | 把该仓库加入 PAT 的授权列表；确认 `Contents: Read and write` |
| `push-via-api.py` 读不到 PAT | `~/.git-credentials` 缺失/格式不对 | 格式应为 `https://x-access-token:<PAT>@github.com` |

### LaunchAgent 加载失败
```bash
# 1) 校验 plist 语法
plutil -lint ~/Library/LaunchAgents/com.token-gateway.server.plist
# 2) 看是否已加载 / 运行状态
launchctl list | grep token-gateway
launchctl print gui/$(id -u)/com.token-gateway.server | head -40
# 3) 常见原因：ProgramArguments 里 python3 或 token-gateway.py 路径写错；
#    WorkingDirectory 不存在；端口被占；plist 放错目录（应为 ~/Library/LaunchAgents/）
# 4) 改完 plist 后需重新加载
launchctl unload ~/Library/LaunchAgents/com.token-gateway.server.plist
launchctl load   ~/Library/LaunchAgents/com.token-gateway.server.plist
# 5) 错误日志
tail -n 50 /tmp/token-gateway.err.log
```

### mem-guard 相关
| 现象 | 原因 | 处理 |
|---|---|---|
| 非 macOS 上 `--sample` 返回空/报错 | 探针是 macOS 专属 | 属预期；默认 `--fail-open` 会静默放行 |
| CI 里希望显式失败 | 默认 fail-open | 加 `--no-fail-open` |
| `launchctl` 加载后不采样 | node 路径或 `mem-guard.mjs` 路径错 | 按「安装为 LaunchAgent」核对 `ProgramArguments` |

## 运维细节（日志 / 权限 / 轮转）

### 日志路径
| 日志 | 路径 | 内容 |
|---|---|---|
| 网关 stdout | `/tmp/token-gateway.log`（launchd 下） | 启动横幅、请求日志 |
| 网关 stderr | `/tmp/token-gateway.err.log` | 异常堆栈 |
| 用量审计 | `~/.ollama-token-gateway/logs/usage.jsonl` | 每次请求一行 JSON（token 用量、key） |
| mem-guard 越线 | `~/.ollama-token-gateway/mem-guard.log` | 越线告警（JSONL） |
| mem-guard stdout/err | `~/.ollama-token-gateway/mem-guard.{out,err}.log` | LaunchAgent 输出 |

### 权限建议
`logs/` 与 `certs/` 含用量与私钥，建议收紧：
```bash
chmod 700 ~/.ollama-token-gateway/logs ~/.ollama-token-gateway/certs
chmod 600 ~/.ollama-token-gateway/config.json \
          ~/.ollama-token-gateway/logs/*.jsonl \
          ~/.ollama-token-gateway/certs/server.key
```

### 轮转建议
`usage.jsonl` 会随请求持续增长。服务端每次写入以**追加模式**打开，因此改名轮转是安全的（下次写入自动重建）：
```bash
# 手动 / 定时轮转
mv ~/.ollama-token-gateway/logs/usage.jsonl \
   ~/.ollama-token-gateway/logs/usage.jsonl.$(date +%F)
```
自动轮转可用 macOS `newsyslog`（在 `/etc/newsyslog.conf` 追加一行，按需调整路径/大小/保留份数）：
```
# logfilename                                   owner:group  mode count size(K) when flags
/Users/你的用户名/.ollama-token-gateway/logs/usage.jsonl  claw:staff   600   7    10240   *    J
```
或用一个每日 launchd/cron 任务执行上面的 `mv`。

### 日志格式示例（JSONL）

**`logs/usage.jsonl`**（每次请求一行）：
```json
{"ts": 1730000000, "date": "2026-11-02", "time": "22:10:05", "api_key": "sk-ollama-abc...", "model": "gemma4:latest", "prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13, "elapsed_s": 1.23, "client_ip": "100.97.225.7", "stream": false}
```

**`mem-guard.log`**（越线告警，JSONL）：
```json
{"t": "2026-11-02T14:10:05.123Z", "pct": 0.912, "usedBytes": 252000000000, "totalBytes": 274877906944, "at": 0.9, "msg": "内存使用率 91.2% 超过阈值 90%"}
```
> `--webhook` 的 POST body：`{"alert":"memory-guard","pct":...,"usedBytes":...,"totalBytes":...,"at":...,"msg":...,"at_iso":...}`。

### 备份 config.json
`config.json` 含真实 key，改动前先备份（`config.json.bak*` 已在 `.gitignore`，不会入库）：
```bash
cp ~/.ollama-token-gateway/config.json \
   ~/.ollama-token-gateway/config.json.bak.$(date +%Y%m%d-%H%M%S)
```

### 多实例部署
同一台机器跑多个网关实例时，必须隔离：
- **端口**：每个实例 `global.listen_port` 不同（它优先于 `TG_PORT`）。
- **配置/日志目录**：每个实例独立 `TG_CONFIG_DIR`（各自的 `config.json` / `certs/` / `logs/`）。
- **LaunchAgent 标签**：复制 plist 后改 `Label`（如 `com.token-gateway.server2`）及 `ProgramArguments` / `WorkingDirectory`。
- **凭据**：不同实例用不同 `admin.token` 与 key 命名空间，避免串用。

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
