# gateway —— 本地局域网 OpenAI 兼容 token 网关

`gateway` 是一个**单文件、纯 Python 标准库**实现的 OpenAI 兼容 token 网关，面向**本地局域网 / Tailscale 内网**使用：统一代理 Ollama 与 Rapid-MLX 上游，做 per-key 配额 / QPS / 限流，并提供 Web 管理台与 `/api/admin/*` 管理接口。附带 `mem-guard.mjs` 设备内存守卫。

- **服务端**：`token-gateway.py`（无第三方依赖，Python 3.10+）
- **auto 虚拟模型**：客户端把 `model` 设为 `auto`，由网关按**请求分档 + 多维加权**从本地池里挑最合适的模型（短问答走小模型、复杂任务上大模型，被占用时自动让位），免去手动切换（详见 [auto 虚拟模型](#auto-虚拟模型自动分配)）
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
- [自测 / Tests](#自测--tests)
- [变更记录](#变更记录)
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
| TG_AUTO_STRATEGY | balanced | `auto` 分配策略：`balanced`（默认，分档+加权综合）\| `quality`（纯精度选最强）\| `least-busy`（繁忙规避轮询）\| `round-robin` \| `random` |
| TG_AUTO_TIER | 1 | `balanced` 的请求分档开关；设 0/false/no/off 关闭后等价于纯加权质量排序 |
| TG_AUTO_TIER_IDEALS | 8,30,122 | 轻量/常规/深度三档的理想参数量（B），用于计算分档适配度 |
| TG_AUTO_TIER_LIGHT_CHARS | 400 | 轻量档的输入字符上限（小于此值且轮次少 → 轻量档） |
| TG_AUTO_TIER_DEEP_CHARS | 3000 | 深度档的输入字符阈值（大于此值 → 深度档） |
| TG_AUTO_WEIGHTS | tier=0.30,quality=0.30,speed=0.30,pref=0.15,quant=0.10 | 综合打分权重；`tier`/`quality`/`speed` 三项默认**等权** |
| TG_SPEED_WINDOW | 200 | 速度统计的滑动窗口：每个模型每个 prompt 桶保留的样本数 |
| TG_SPEED_MIN_SAMPLES | 12 | 速度统计的最小样本数；桶内不足此值视为「无实测」，该因子给中性分 0.40 |
| TG_SPEED_TAU | 10 | 速度软映射的时间常数（秒）：`speed = tau/(tau+耗时)`，越大越宽容 |
| TG_TOKENS_PER_CHAR | 0.5 | 字符→token 的粗略换算，仅用于把请求映射到速度桶，不参与计费 |
| TG_IMG_TOKENS_PER_MPIX | 980 | 图片折算 token 的系数（token / 百万像素）。实测本机 VLM 上游按面积线性计费：384²=146、768²=578、1536²=2306 |
| TG_IMG_TOKENS_NOMINAL | 600 | 拿不到图片宽高时（如远程 http URL）按此名义值计，对应一张 768² 的图 |
| TG_AUTO_QUALITY | (空) | 显式优先顺序（逗号分隔，从高到低）；表内模型优先于表外，表内再按加权排序；允许写别名 |
| TG_AUTO_BUSY_THRESHOLD | 1 | 繁忙阈值：在飞请求数 ≥ 此值即跳过该模型（默认 1 = 有人正在用就换） |
| TG_AUTO_EXCLUDE | (空) | `auto` 额外排除的模型（逗号分隔，叠加在 `deny_models` 与 `global.auto_exclude` 之上） |
| TG_QPS | 5 | 每 key 每秒请求数 |
| TG_CONCURRENCY | 10 | 全局最大并发 |
| TG_READ_TIMEOUT | 900 | **单次静默上限**（秒），不是整段耗时上限：等上游响应头、以及每次读取响应体，连续这么久没有任何字节才判超时。5 万 token 上下文的预填充本身就要 100~150s，生产建议 ≥600 |
| TG_VLM_READ_TIMEOUT | 120 | 多模态（带图）请求的单次静默上限（秒） |
| TG_BUFFER_NONSTREAM | 1 | 非流式请求**先缓冲完整响应再回给客户端**：上游失败时能返回真实 5xx 与 `Content-Length`，而不是"截断的 200"。设 0 退回旧行为（立即发头、边读边转发，失败即断流） |
| TG_CONNECT_TIMEOUT | 10 | 上游连接超时（秒） |
| TG_MIN_MAX_TOKENS | 32768 | **Rapid-MLX 非 VLM 上游**的 `max_tokens` 强制下限（防止客户端给小值被截断）；VLM 上游的带图请求与纯文本请求都不生效，设 0 关闭 |
| TG_MIN_MAX_TOKENS_TOOLS | 8192 | **纯文本 + 工具调用**请求的 `max_tokens` 下限（两个上游组都生效）。工具调用是整块下发的，预算不足时上游只回半截 `tool_call` + `finish_reason=length`，客户端会报 `Empty stream`；设 0 关闭该下限 |
| TG_MIN_MAX_TOKENS_TEXT | 2048 | **Ollama 等非 Rapid-MLX 上游的纯文本**请求 `max_tokens` 下限（带图请求不受限）。gemma4 这类"会思考"的模型先把预算烧在思维链上，预算不足时 `content` 为空、`finish_reason=length`，客户端只看到一条空回复；设 0 关闭 |
| TG_REASONING_RETRY_TOKENS | 4096 | 命中「空正文 + 有 `reasoning` + `finish_reason=length`」时的重发预算，实际取 `max(本值, 本次生效预算×4)`。仅对非流式、且响应尚未发往客户端的请求生效，**最多重发一次** |
| TG_STREAM_INCLUDE_USAGE | 0 | 流式请求是否向上游索取 `stream_options.include_usage` 以获得**精确** usage。三家上游都支持，但会在 `[DONE]` 前多发一个 `choices: []` 的帧，手写 SSE 解析器（`chunk["choices"][0]`）会崩，故默认关闭；关闭时按增量文本字符折算 |
| TG_BAD_MODEL_TTL | 120 | 上游判定某模型不可用后，把它从 auto 候选池与存在性检查里熔断多久（秒）。`/api/tags` 只说明 manifest 在不在，权重损坏 / 拉取中断时模型仍会留在列表里，靠这个开关避免反复选中它；设 0 关闭熔断 |
| TG_AUTO_MODEL_FALLBACK | 2 | auto 选中的模型在上游被判定不可用时，自动换下一个候选重试的次数。**显式点名模型的请求不降级**（要的就是它，拿不到应当明确报错）；设 0 关闭降级 |
| TG_CONFIG_DIR | ~/.ollama-token-gateway | config.json / certs / logs 所在目录 |
| TG_LOG_DIR | `$TG_CONFIG_DIR/logs` | 用量日志 `usage.jsonl` 所在目录 |

> **数值型变量写错不会让服务挂掉**：端口、并发、超时、预算、速度参数统一走 `_env_num()` 解析。
> 空串、带单位（`TG_READ_TIMEOUT=600s`）、非数字（`TG_MIN_MAX_TOKENS=32k`）一律**回退内置默认值**并打印
> `⚠️ 环境变量 XXX='600s' 非法，已回退默认值 900`；超出安全区间的值（如 `TG_SPEED_TAU=0`）夹到下限。
> 启动横幅打印的是**实际生效值**，不是原始字符串——所以看到 `文本 900s` 就说明 `600s` 没被采信。

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

**改了 plist 里的环境变量后，只做 `kickstart -k` 是不够的**——launchd 会沿用**加载时**记录的那份作业定义，进程重启了但 env 还是旧值（本仓库曾因此出现"把 `TG_READ_TIMEOUT` 改成 600 却仍报 240s"）。必须重新装载：

```bash
launchctl bootout gui/$(id -u)/com.token-gateway.server 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.token-gateway.server.plist
```

> 注：`bootstrap` 偶尔会返回 `Bootstrap failed: 5: Input/output error` 但作业其实已经起来了——先 `curl :3000/health` 确认，别急着重试成两个实例。
>
> 验证是否真的生效：看启动横幅里的 `⏱️ 静默预算: 文本 600s / 带图 150s  非流式转发: 缓冲`，或 `launchctl print gui/$(id -u)/com.token-gateway.server | grep TG_`。只改 Python 代码不需要重新装载，`kickstart -k` 即可。

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
| `balanced`（默认） | 先按 prompt 规模给请求**分档**（轻量/常规/深度），再把「分档适配」「绝对质量」「实测速度」按**等权**加权，加上显式偏好、量化保真算出综合分，取最高分。结果稳定可预期，短问答走小模型、复杂任务上大模型 |
| `quality` | 纯**模型精度**降序（参数量 → 量化），忽略请求体量，一律用最强模型；最优质的被占用时让位给次优的空闲模型 |
| `least-busy` | 先按网关侧在飞请求数筛出**空闲**候选，在空闲集合内轮询；全部繁忙时退化为「选负载最低者」，不会直接失败 |
| `round-robin` | 忽略负载与质量，纯轮流（候选按模型名排序，顺序可复现） |
| `random` | 随机挑选 |

繁忙判定基于**网关侧每模型的在飞请求数**（`TG_AUTO_BUSY_THRESHOLD`，默认 1）。含义是「已经有一个请求在使用该模型，就先换一个」，避免多个请求挤在同一模型上排队。这个计数只统计经过本网关的请求——**外部客户端直连上游（例如直接请求 `:11434`）造成的占用，网关无法感知**。

#### 分档与加权打分（`balanced` 策略）

**第一步：给请求分档**（只看请求体，不调用模型）：

| 档位 | 判定条件（任一深度信号命中即深度；否则满足全部轻量条件即轻量；其余常规） | 理想参数量 |
|---|---|---|
| `light` 轻量 | 文本 ≤ `auto_tier_light_chars`（默认 400 字符）且消息 ≤ 3 条且 `max_tokens` ≤ 1024（或不填） | 8B |
| `standard` 常规 | 不满足轻量，也没有深度信号 | 30B |
| `deep` 深度 | 带图、带 `tools`/`functions`、文本 ≥ `auto_tier_deep_chars`（默认 3000 字符）、`max_tokens` ≥ 2048、消息 ≥ 8 条 | 122B |

**第二步：加权求和**，取综合分最高的空闲模型：

```
score = Σ (wᵢ × fᵢ) / Σ wᵢ
```

| 因子 | 含义 | 默认权重 | 打分方式（0~1） |
|---|---|---|---|
| `tier` | **分档适配** | **0.30** | 模型参数量与本档理想值的**对数距离高斯衰减** `exp(-d²/2σ²)`，σ = 0.35/0.55/0.70（轻/常/深）；深度档只惩罚「偏小」，更大的模型同样算贴合 |
| `quality` | **绝对质量** | **0.30** | 参数量在候选池内的对数位置归一化；参数量取 Ollama `details.parameter_size`，缺失时解析模型名（`A10B` 激活参数会被忽略） |
| `pref` | 显式优先 | 0.15 | `global.auto_quality` 表内第 1 位 1.0，之后 `0.85^i`；未配置该表时此因子整体剔除 |
| `quant` | 量化保真 | 0.10 | `F32/BF16/F16 = 1.0 → Q8 ≈ 0.67 → Q4 ≈ 0.17 → Q3/Q2 = 0` |
| `speed` | **实测速度** | **0.30** | `tau/(tau+t)`，t 为该模型在**同 prompt 规模桶**内的实测耗时中位数（`tau` 默认 10s）；桶内不足 12 条样本时返回中性 0.40，**不用参数量猜** |

要点：

- **分档、质量、速度三项严格等权**，不存在「先按档位筛掉一批、再按质量挑」的硬顺序，三者各自独立打分后加权求和。权重只影响结果，不改变流程。
- **速度必须用实测，不能用参数量反推**：实测 122B（MLX + 前缀缓存）在 3k~20k 上下文区间比 8B（Ollama，无前缀缓存）快约 2.7 倍（12.1s vs 32.6s），「小模型一定更快」在本机并不成立。旧的 `speed = 1 − quality` 写法与质量完全共线，等价于把质量权重抵消掉一半。
- 速度按 prompt 规模分桶（<3k / 3k~20k / ≥20k token），**不做跨桶回退**：不同量级的耗时不可互换，拿短请求样本顶替长请求等于编造数字。
- 桶号由**上游真实会看到的 token 规模**估算，而不只是用户正文：工具清单与图片同样占 prompt token。实测 12 个工具、5,066 字符的 JSON ≈ 1,967 token，一张 768² 的图 ≈ 578 token。只数正文会把带工具/带图的长会话估进偏小的桶（那里全是纯文本样本，明显更快），使 speed 因子系统性偏高。图片宽高从 data URI 的图头解析（PNG/JPEG），拿不到时按 `TG_IMG_TOKENS_NOMINAL` 计。
- 速度软映射用**绝对时间**而非「相对最快者」：池内一个 0.2s 的微模型会把其余候选统一压到下限，使速度因子失去分辨力。
- 权重为 0 的因子、未配置的因子会从分母里剔除，不会拉低总分（所以配了 `pref` 表时其余因子会等比例放大）。
- **负载是硬约束**：先排除在飞请求数 ≥ `TG_AUTO_BUSY_THRESHOLD` 的候选，再在空闲集合内比综合分；全部繁忙时在负载最低的一撮里比综合分，不会直接 503。
- 参数量的对数尺度让 8B 与 122B 相差约 1.18 个数量级，跨档能拉开足够差距；`tier` 与 `quality` 同权时，**轻量档由分档项主导选小模型，常规/深度档由质量项主导选大模型**。

按 prompt 规模会实际发生什么（以 `gemma4:latest` 8B、`qwen3.5-122b` 122B 为例）：

| 请求 | 档位 | 选中 | 实测耗时 |
|---|---|---|---|
| 「只回复两个字：收到」（6 字符，`max_tokens=16`） | light | `gemma4:latest` | 0.46s |
| 约 500 字符的常规提问 | standard | `qwen3.5-122b` | ~1.3s |
| 4000+ 字符长文总结 / 带 tools / 带图 | deep | `qwen3.5-122b` | ~4s |

想调整倾向，直接改权重即可（env 与 config 二选一，config 优先且无需改 plist）：

```json
{
  "global": {
    "auto_weights": {"tier": 0.5, "quality": 0.3, "quant": 0.1, "pref": 0.1},
    "auto_tier_ideals": [8, 30, 122],
    "auto_tier_light_chars": 600,
    "auto_tier_deep_chars": 3000
  }
}
```

```bash
# 等价的环境变量写法（适合写进 launchd plist）
TG_AUTO_WEIGHTS="tier=0.5,quality=0.3,quant=0.1,pref=0.1"
TG_AUTO_TIER_LIGHT_CHARS=600
```

- 想更省时间：调高 `speed`（默认已与质量等权），或调大 `auto_tier_light_chars` 让更多请求落进轻量档。
- 想一律用最强：`quality` 调到 0.8 以上并把 `speed` 降到 0，或直接 `TG_AUTO_STRATEGY=quality`。
- 想更偏向实测最快的模型：把 `speed` 提到 0.5 以上；反过来把 `speed` 设为 0 即完全忽略实测耗时。
- 速度样本冷启动（新部署、新模型）：窗口内不足 `TG_SPEED_MIN_SAMPLES` 条时该因子给中性 0.40，运行 `TG_SPEED_MIN_SAMPLES` 次后会自然校准；也可调大 `TG_SPEED_TAU` 让速度分更平缓。

#### 质量排序规则（`quality` 策略）

评分按三级比较，越靠前越优先：

| 优先级 | 依据 | 说明 |
|---|---|---|
| 1 | **显式优先表** | `global.auto_quality` / `TG_AUTO_QUALITY` 列出的模型按列表顺序优先，可覆盖任何启发式 |
| 2 | **参数量** | 优先取上游元数据（Ollama `/api/tags` 的 `details.parameter_size`，如 `8.0B`）；取不到时从模型名解析（`122B`）。名字里 `A10B` 这类**激活参数**会被忽略，只认总参 |
| 3 | **量化精度** | 参数量相同时比较量化档位：`F32 > BF16/F16 > Q8 > Q6 > Q5 > Q4 > Q3/Q2` |

选择逻辑：

1. 候选按上述三项**降序**排序；
2. 跳过在飞请求数 ≥ `TG_AUTO_BUSY_THRESHOLD` 的模型；
3. 取剩余里的**第一个**，即最强的空闲模型；
4. 全部繁忙时，在**负载最低**的候选里取质量最高者——不会因为繁忙直接返回 503，也不会无脑降级到最弱模型。

`balanced` 策略同样尊重这张表：**表内模型只要在候选池里，就只在表内挑**（表内再按综合分排序），避免被分档或质量分反超；表内模型全部不可用时才回落全量候选。如果你认为参数量不能代表实际效果，可以直接指定顺序（别名会被自动展开）：

```json
{
  "global": {
    "auto_quality": ["qwen3.5-122b", "gemma4:latest", "qwen2.5vl:3b"]
  }
}
```

**如何确认选中了谁**：
- 响应头 **`X-Gateway-Model: <真实模型名>`**
- 响应头 **`X-Gateway-Auto-Tier: light|standard|deep`**（本次决策使用的请求档位）
- 响应体的 `model` 字段（上游回显的真实模型名）
- 服务日志：`🎲 auto → <模型> @ <上游>（<选择理由：档位 + 综合分 + 各因子得分>）→ <客户端IP>`

**示例**
```bash
curl -s http://localhost:3000/v1/chat/completions -D - \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}],"stream":false}'
# 响应头会带：X-Gateway-Model: <实际选中的模型>、X-Gateway-Auto-Tier: light
```

**注意**
- 默认 `balanced` 策略下，**同一条件下（同样的 prompt 规模与能力要求）连续请求会稳定命中同一个模型**（不会一会儿一个）。只有跨档（输入变长变短）、能力变化（带图 / tools / embeddings）或最优候选被占用时才会切换，切换原因与各因子得分都会写进服务日志。需要绝对固定的输出风格时，仍建议显式指定模型。
- 分档只看**请求特征**（字符数、消息条数、`max_tokens`、是否带图/工具），不做语义理解：一段 300 字的深奥问题仍会归入轻量档。如果任务对精度敏感，把 `auto_tier_light_chars` 调小（例如 120），或直接显式指定模型。
- 「繁忙」是**网关视角**的并发，不是上游的真实队列深度。若某模型被外部进程占满，网关仍会把它当作空闲模型选中，此时表现为该请求变慢而非被跳过。想更保守，可把 `TG_AUTO_BUSY_THRESHOLD` 调大。
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
| **504** | `upstream_timeout` | 上游在静默预算（`TG_READ_TIMEOUT`）内没返回任何字节 |
| **502** | `upstream_broken` | 上游中途断连，或声明了 `Content-Length` 却没发全 |
| **500** | `internal_error` | 网关内部未捕获异常；日志会带完整堆栈（`❌ do_POST 未捕获异常`） |

> 响应头会暴露用量（CORS `Expose-Headers`）：`X-Token-Usage`、`X-Quota-Remaining`、`X-RateLimit-Limit`。

#### 转发与超时语义（长上下文为什么会"被取消"）

网关对上游的读取**不是整段计时**：`TG_READ_TIMEOUT` 是"连续多久没有任何字节"的静默上限。理解这一点对排查长上下文失败很关键。

| 场景 | 网关行为 |
|---|---|
| 流式（`stream: true`） | 收到上游响应头就立即转发，正文边读边发；上游失败时补一条 `data: {"error":...}` 事件（**不发 `[DONE]`**，避免被当成正常结束） |
| 非流式（默认） | **先缓冲完整正文，读完再发**，并带上 `Content-Length`。成功 = 完整 JSON；失败 = 明确的 5xx，而不是一个"看起来正常但内容不完整"的 200 |
| 上游声明 `Content-Length` 但没收全 | 判为 `upstream_broken`（502），不会把半截内容当成功返回 |
| 上游在预算内一个字节都没给 | `upstream_timeout`（504），日志记 `⏱️ 上游超时 Ns（静默预算 Xs）` |
| 读取超时 | **不重试**：重试只会把已经等过的预算再翻一倍，5 万 token 的生成也没必要跑两遍 |

> 为什么要缓冲：旧版连上上游就先把 200 响应头发给客户端，之后若读取超时只能断流——客户端收到的是"截断的 200"，多数客户端会把它显示成"用户已取消 / 请求被取消"。缓冲后同样的故障会变成明确的 504，客户端能正确提示并重试。
>
> 代价是**首字节时间 = 上游完整生成时间**（非流式请求本来就拿不到增量）。若某个客户端对首字节延迟敏感（例如自带 300s 首包超时），可设 `TG_BUFFER_NONSTREAM=0` 退回旧行为。

**容量参考**（本机 122B 4-bit，M 系列 256GB）：5 万 token 上下文的预填充约 100~150s，加生成约 2~4 分钟；两条大请求排队时会成倍增长。这就是生产环境把 `TG_READ_TIMEOUT` 设为 **600** 的原因。


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

`config.json` 顶层三块：`keys` / `global` / `admin`（以 `_comment` 开头的键为自由备注，服务端忽略）。完整样例见 [`config.example.json`](config.example.json)。

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
| `auto_quality` | string[] | `[]` | `auto` 的显式优先顺序（从高到低）；表内模型优先于表外，表内再按综合分排序；允许写别名 |
| `auto_tier` | bool | `true` | `balanced` 的请求分档开关；`false` 时不做分档，退化为纯加权质量排序 |
| `auto_tier_ideals` | number[] | `[8, 30, 122]` | 轻量/常规/深度三档的理想参数量（B） |
| `auto_tier_light_chars` | int | `400` | 轻量档输入字符上限 |
| `auto_tier_deep_chars` | int | `3000` | 深度档输入字符阈值 |
| `auto_weights` | object \| string | `{tier:0.30, quality:0.30, speed:0.30, pref:0.15, quant:0.10}` | 综合打分权重，可写对象或 `"tier=0.5,quality=0.3"` 字符串；分档/质量/速度默认等权 |
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

### 模型在 `/v1/models` 里，但请求它必报 404

`/api/tags` 反映的是 manifest 是否存在，不代表权重能加载。权重 blob 缺失、下载中断、
config blob 损坏时，模型**仍会出现在列表里**，但上游一律回 `model 'x' not found`。

网关的处理：命中一次就熔断该模型 `TG_BAD_MODEL_TTL`（默认 120s）——期间 auto 候选池
跳过它、显式请求立即 404 并附可用列表；auto 请求会自动换下一个候选重试（最多
`TG_AUTO_MODEL_FALLBACK` 次）。日志里对应 `🚫 模型熔断` 与 `🔁 auto 降级`。

核查：对比每个 manifest 的 `config.digest` 与 `layers[].digest` 是否都在 `blobs/` 下；
修复只需 `ollama pull <模型名>`（已存在的层会跳过，只补缺口）。

### `curl localhost:3000/health` 异常
| 现象 | 可能原因 | 排查 / 处理 |
|---|---|---|
| `Connection refused` | 服务未启动 / 端口不对 | `lsof -nP -iTCP:3000 -sTCP:LISTEN`；确认 `config.json` 的 `listen_port`（它优先于 `TG_PORT`） |
| 进程在但 `/health` 超时 | 绑定地址不对（如只绑 `127.0.0.1` 却从别机访问） | 改 `listen_host` 为 `0.0.0.0` 或内网 IP |
| `/health` 正常但 `/v1/models` 为空 | 上游不可达 | `curl 127.0.0.1:11434/api/tags`；检查 `TG_RAPID_MLX_HOSTS` |
| 请求返回 500 | 上游异常 / 代码异常 | 看 `/tmp/token-gateway.log` 与 `/tmp/token-gateway.err.log` |
| 端口被占用（启动报 `Address already in use`） | 旧实例仍占用 3000 | 停旧实例，或改 `config.json` 的 `listen_port` |
| **长上下文请求"用到一半"变`用户已取消` / 请求被取消** | `TG_READ_TIMEOUT` 太小，上游还在预填充就被判超时，连接被截断；旧版先发 200 头再断流，客户端只能理解为"被取消" | 确认日志有 `⏱️ 上游超时` 或 `读取上游响应中断`；把 `TG_READ_TIMEOUT` 调大（生产用 600）。5 万 token 在 122B 上预填充约 100~150s，排队时更久。现版本超时会返回 **504 `upstream_timeout`**，不再是无提示断流 |
| 响应头到了、正文迟迟不来 | 非流式请求在等上游完整生成（缓冲模式），属预期 | 看日志 `⏱️ 上游超时` 与 `📊 ... 用时`；对首字节敏感可设 `TG_BUFFER_NONSTREAM=0` |
| `504 upstream_timeout` | 上游在静默预算内没给任何字节 | 重试；或换更小的模型 / 缩短上下文；确认 122B 未被其他客户端占满 |
| `502 upstream_broken` | 上游中途断连或声明了 `Content-Length` 却没收全 | 看 `⚠️ 读取上游响应中断` 后的具体异常；通常是上游 OOM 或进程重启 |
| `500 internal_error`（无堆栈、无上游错误）且**每个请求都 500** | 环境变量写法非法（如 `TG_READ_TIMEOUT=600s`、plist 里留空串），旧版在请求路径上裸 `int()` 解析抛 `ValueError` | 已修复（统一 `_env_num()` 兜底）：日志会出现 `⚠️ 环境变量 XXX=... 非法，已回退默认值`；确认版本 ≥ `4ce1cc0f` |
| `500 internal_error`（日志 `KeyError: 'max_tokens'`） | 请求超过上下文窗口、走"截短历史"分支时，客户端未传 `max_tokens`，旧版日志语句直接下标读取该字段 | 已在 2026-11-05 修复（改用局部变量）。确认版本 ≥ `144dbf9a`；升级后同样场景返回 200 并在日志留下 `✂️ 截短历史消息` |

### auto 分配相关
| 现象 | 可能原因 | 排查 / 处理 |
|---|---|---|
| `503 auto_no_candidate` | 候选池为空：上游没加载模型、全被 `deny_models` / `auto_exclude` 排除、或 `allow_models` 里没有可用模型 | `curl 127.0.0.1:11434/api/tags` 确认上游；检查 `global.allow_models`、`global.deny_models`、`global.auto_exclude`、`TG_AUTO_EXCLUDE` |
| `403 model_not_allowed`（请求 `auto`） | `global.deny_models` 里写了 `"auto"` | 删除该项即恢复；这是显式禁用 `auto` 的开关 |
| `404` 且提示模型不存在（请求 `auto`） | `auto` 被关闭：`TG_AUTO_ENABLED=0`，或 `allow_models` 不含 `auto`/`*` | 打开 `TG_AUTO_ENABLED`；把 `auto` 加进允许列表 |
| auto 老是选同一个模型 | 属预期：`balanced` / `quality` 都是确定性策略，同条件下不切换 | 看响应头 `X-Gateway-Auto-Tier` 与日志里的综合分；想分散请用 `TG_AUTO_STRATEGY=round-robin` |
| 短问题也走了 122B | 轻量档判定没命中：字符数超过 `auto_tier_light_chars`（默认 400）、消息数 > 3，或 `max_tokens` > 1024 | 调大 light 阈值、调高 `tier` 权重，或把 `quality` 降权（见[分档与加权打分](#分档与加权打分balanced-策略)） |
| 明明空闲却选了较弱的模型 | `TG_AUTO_BUSY_THRESHOLD` 判定为该模型繁忙（网关侧在飞计数 ≥ 阈值） | 日志里搜「跳过 N 个繁忙候选」；需要允许同模型排队就把阈值调大 |
| 切模型后输出风格变化大 | `auto` 本来就可能落到不同模型 | 需要稳定输出请显式指定 `model`；或用 `global.auto_quality` 收窄候选 |
| 调整了 `auto_weights` / `auto_tier_*` 但没变化 | 这些配置在**服务启动时**读入内存（`_cfg`），改文件不会热加载；且 plist 里的同名 env 会覆盖 config | 改完 `config.json` 或 plist 后重启：`launchctl kickstart -k gui/$(id -u)/com.token-gateway.server` |

### 客户端报 `Empty stream ... finish_reason=length`

| 现象 | 原因 | 处理 |
|---|---|---|
| IDE 报 `Empty stream: upstream ended the turn with finish_reason=length before emitting any model token (chunks=1, bytes=…)` | **工具调用被输出预算截断**：上游把整个 `tool_call` 放在**一个 chunk** 里下发，预算耗尽时该 chunk 只有半截 JSON，客户端解析不出完整调用，于是判定「没有任何模型 token」 | 网关默认把「纯文本 + `tools`」请求的 `max_tokens` 抬到 `TG_MIN_MAX_TOKENS_TOOLS`（8192），日志可见 `⬆️ 提升 max_tokens: 2048 → 8192 (工具调用预算)`。若仍复现，说明单次工具调用的载荷本身超过 8192，需在客户端调大输出上限或减少单次写入量 |

> 复现方式：带 `tools` 的请求传 `max_tokens=48`，会看到一个 chunk（约 444 bytes）、`finish_reason=length`、既无 `content` 也无完整 `tool_calls` —— 与客户端报错的 `chunks=1, bytes=1139` 同形。
> 注意这不是网关丢包：网关是逐帧转发的，截断发生在上游的 `max_tokens` 边界上。

### 回复"成功但为空"（模型没说话）

| 现象 | 原因 | 处理 |
|---|---|---|
| 客户端收到 200、`finish_reason=length`，但 `message.content` 是空字符串；同一句提示换一次请求又正常了 | **推理模型把输出预算全花在思维链上**：gemma4 / r1 这类模型先产出 `reasoning` 再产出 `content`，预算在 reasoning 阶段就耗尽了。而"是否触发思维链"本身是**概率性**的，所以表现为偶发空回复。实测 `max_tokens=64` → `content` 0 字符 / `reasoning` 233 字符 / `completion_tokens` 恰好等于预算；`max_tokens=4096` 时 reasoning 可长到 1,960 字符才出正文 | ① 预算下限：Ollama 侧纯文本请求抬到 `TG_MIN_MAX_TOKENS_TEXT`（默认 2048），日志 `⬆️ 提升 max_tokens: 64 → 2048 (文本输出下限)`；② 确定性兜底：非流式请求若「content 空 + reasoning 非空 + `finish_reason=length`」，网关以 `max(4096, 生效预算×4)` **重发一次**，日志 `🔁 推理模型空正文 … ✅ 重发成功`，响应头带 `X-Gateway-Reasoning-Retry: 4096`；③ 流式请求无法回滚已发出的分片，只能靠 ① 的下限预防 |

> Ollama 的 OpenAI 兼容端点**不支持**关闭思维链：`think: false` 与 `reasoning_effort: low` 均被忽略（已实测）。
> 想彻底避开这类模型的思考开销，只能换非推理模型，或把 `TG_REASONING_RETRY_TOKENS` 调大以覆盖更长的思维链。

### 配额掉得比预期快 / 用量数字大得离谱

| 现象 | 原因 | 处理 |
|---|---|---|
| 明明只问了几个短问题，`daily`/`monthly` 配额却掉了几万；`usage.jsonl` 里流式请求的 `completion_tokens` 远大于回复字数 | 流式响应的 SSE 默认不带 `usage`，网关要估算；旧实现用 `len(原始字节)//3`，把每帧约 230 字节的 `data: {...}` 框架也算成 token —— 实测一个真实 64 token 的回复被记成 4,389（约 68 倍） | 已改为按**增量文本字符**折算（`TOKENS_PER_CHAR`，默认 0.5），同一次请求 33,115 → 812。想要**精确**值就把 `TG_STREAM_INCLUDE_USAGE=1`（前提：客户端能耐受 `[DONE]` 前多出的 `choices: []` 帧）。历史记录不会自动修正，`usage.jsonl` 里旧数据仍是旧口径 |

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
# 6) 报 "Bootstrap failed: 5: Input/output error" 不等于没起来
#    launchd 有时会误报 EIO，作业其实已经启动。先查端口再重试，
#    否则可能起两个实例抢同一个端口
lsof -nP -iTCP:3000 -sTCP:LISTEN
launchctl print gui/$(id -u)/com.token-gateway.server | grep -E "state = |pid = "
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

## 上游加速：开启 prefix cache（APC）

网关本身**不缓存 KV**，每轮请求的 prefill 全部由上游承担。agent 类客户端每轮都会重发完整上下文（实测同一会话 58k → 62k tokens 递增），若上游不复用前缀，6 万 token 就要完整重算一遍。

### 现象与根因

mlx_vlm 内置 APC（Automatic Prefix Caching），但**默认关闭**——`apc.from_env()` 在 `APC_ENABLED` 未设置时直接返回 `None`。表现为每轮耗时与上下文长度同阶，而输出 token 极少：

```
📊 Model:Qwen3.5-122B Tokens:62172 (P:62105+C:67) 用时:247.8s
```

6 万 prompt token、67 个输出 token、247.8 秒——时间几乎全在 prefill，且每轮重复。

一条可复现的判据：同一 prompt 连发两次，若第二次耗时与第一次基本相同，就是没有缓存。

### 开启方式

在**上游**的 LaunchAgent 注入环境变量（网关侧无需改动）：

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>APC_ENABLED</key><string>1</string>
    <key>APC_EXACT_CACHE_ENTRIES</key><string>4</string>
</dict>
```

重启上游后，启动日志应出现：

```
APC enabled (block_size=16, num_blocks=2048, hash=fast, disk=False)
APC self-check ok mode=exact schema=v1 layers=48 ...
```

`mode=exact` 表示该模型被判为混合架构（linear attention + full attention），走"整前缀快照"路径；`mode=block` 则是逐块 KV 复用。

### 实测收益（29,716 token 上下文，`max_tokens=1`）

| 场景 | 开启前 | 开启后 | 加速 |
|---|---|---|---|
| 冷启动（首次全量 prefill） | 51.2s | 51.2s | — |
| 完全相同前缀 | 14.3s | **0.29s** | 49× |
| 追加一轮（agent 形态） | 14.4s | **0.50s** | 29× |
| 经网关端到端（`auto`→122B） | — | **0.30s** | — |

预填充约 581 tok/s，6 万 token 冷启动约 103s；命中后只算新增的几百 token，耗时降到亚秒级。

### 关键约束

1. **前缀必须完全一致**。APC 按 token 序列做前缀匹配，任何前缀扰动（system prompt 里塞时间戳、工具定义顺序变化、历史被编辑或摘要）都会让命中点前移甚至归零。
2. **混合架构走 exact 快照**，按整个 `prompt_cache` 复制，内存随上下文线性增长（本例 12 个 full-attention 层，约 24 KB/token，6 万 token ≈ 1.4 GB/快照）。`APC_EXACT_CACHE_ENTRIES` 控制保留几份，建议不小于 `--max-num-seqs`。
3. 非流式缓冲模式下**首字节时间 = 完整生成时间**；只有 prefill 接近归零，这个代价才真正可接受。
4. 冷启动那一轮无法避免（模型刚加载、权重未热），第一次请求仍按全量计费。

### 验证方法

同一前缀连发两次，第二次应显著变快：

```python
# 判读：若"完全相同"那次仍接近冷启动耗时，说明 APC 未生效，
# 先确认运行中的进程真的拿到了 APC_ENABLED
```

## 自测 / Tests

`tests/` 下是**自包含**回归用例：每个脚本自己在临时端口起一个真实网关实例（独立 config 与日志目录），并配一个本地 stub 上游，**不碰线上实例、不加载任何真实模型**。

```bash
python3 tests/test_ctx_trunc.py     # 超出上下文窗口被截短时不得 500（4 项）
python3 tests/test_timeout.py       # 静默预算 / 缓冲发送 / 504·502 语义 / 流式错误事件（27 项）
python3 tests/test_tools_budget.py  # 工具调用请求的输出预算下限（9 项）
python3 tests/test_speed_weight.py  # speed 因子：权重结构 / 分档分派 / 无样本不占便宜 / 单调性
python3 tests/test_env_guard.py     # 数值型环境变量写坏时降级不崩（静态断言 + 导入期回退 + 请求期 200，21 项）
python3 tests/test_prompt_estimate.py # prompt 规模估算：工具清单 / 图片面积 / 桶边界（17 项）
python3 tests/test_reasoning_retry.py # Ollama 预算下限 + 推理模型空正文修复（25 项）
python3 tests/test_stream_usage.py  # 流式记账：按增量文本折算、不把 SSE 框架算成 token（13 项）
python3 tests/test_model_fallback.py # 模型失效检测：熔断 / auto 降级 / 显式不降级 / 快速 503（19 项）
```

九个脚本都可通过环境变量改指向：

| 变量 | 作用 |
|---|---|
| `TG_SRC` | 被测网关文件，默认仓库根的 `token-gateway.py` |
| `TG_PY` | 启动网关用的解释器，默认当前解释器 |
| `TG_SRC_OLD` | 仅 `test_ctx_trunc.py`：给出旧版本文件时，额外跑「旧代码必返回 500 + 日志含 `KeyError`」的对照断言 |

> `test_timeout.py` 把静默预算压到 8s（生产 600s）以便快速跑完，断言针对**语义**而非具体秒数；`test_ctx_trunc.py` 会构造约 24 万字符的请求，单次约 10s。

## 变更记录

| 日期 | 提交 | 内容 |
|---|---|---|
| 2026-11-08 | `bbb6202d` | **上游模型失效时自动检测并降级**：`/api/tags` 只说明 manifest 在不在，权重损坏时模型仍留在列表里、请求必 404，而 auto 会按分档反复选中它。新增失效模型熔断表（`TG_BAD_MODEL_TTL`，默认 120s），auto 候选池与存在性检查都跳过它；`_proxy` 增加 auto 降级重试（`TG_AUTO_MODEL_FALLBACK`，默认 2 次），**显式点名模型的请求不降级**；HTTPError 响应体改为只读一次并缓存；`_refresh_model_caches` 的网络探测移出锁并加单飞，避免缓存过期瞬间并发请求排队等同一个 3s 探测。新增 `tests/test_model_fallback.py`（19 项） |
| 2026-11-07 | `e54d064f` | **流式请求记账不再把 SSE 框架算成 token**：SSE 默认不带 usage，旧实现退回 `len(原始字节)//3`，而每帧 `data: {...}` 约 230 字节 —— 一个真实 64 token 的流式回复被记成 4,389 token（约 68 倍），配额被几十倍地扣。改为按**增量文本字符**折算（`TOKENS_PER_CHAR`），同一次请求 33,115 → 812 token；prompt 估算同样改按字符并兼容 `content` 为分段 list；新增可选 `TG_STREAM_INCLUDE_USAGE`（默认 0）以换取上游精确 usage；新增 `tests/test_stream_usage.py`（13 项） |
| 2026-11-07 | `fdff934e` | **Ollama 侧补输出预算下限 + 推理模型空正文修复**：预算下限原先整块嵌在 `upstream_host in RAPID_MLX_HOSTS` 分支里，Ollama 上游完全不受约束；gemma4 这类推理模型先把预算烧在 `reasoning` 上，耗尽时 `content` 为空、`finish_reason=length`（实测 `max_tokens=64` → content 0 字符 / reasoning 233 字符 / completion 恰好等于预算），且是否触发思维链是**概率性**的，故单靠下限挡不住。新增 `_min_max_tokens_for()` 收口预算决策；非 Rapid-MLX 纯文本抬到 `TG_MIN_MAX_TOKENS_TEXT`（默认 2048）；非流式命中「空正文 + reasoning + length」时以 `max(4096, 生效预算×4)` **重发一次**（`TG_REASONING_RETRY_TOKENS`，响应头 `X-Gateway-Reasoning-Retry`），流式不重发；顺带确认 Ollama 的 OpenAI 兼容端点会忽略 `think` / `reasoning_effort`；新增 `tests/test_reasoning_retry.py`（25 项） |
| 2026-11-07 | `29a2b2eb` | **速度分桶改用真实 prompt 规模估算**：此前 `p_hat` 只数 messages 正文，漏掉工具清单与图片，带工具/带图的长会话被估进偏小的桶（该桶全是纯文本样本，明显更快），speed 因子系统性偏高。实测 12 个工具 5,066 字符 ≈ 1,967 token、一张 768² 图 ≈ 578 token（约 980 token/百万像素，384²/768²/1536² 三点一致）。新增 `_auto_req_prompt_chars`（正文 + tools/functions/response_format 序列化长度）与 `_auto_req_image_tokens`（解析 data URI 图头取宽高，拿不到按 `TG_IMG_TOKENS_NOMINAL`）；分档用的 `_auto_req_text_len` 口径不变；新增 `tests/test_prompt_estimate.py`（17 项） |
| 2026-11-07 | `cdfc4dbd` | **数值型环境变量统一兜底，写错不再让服务崩**：`TG_READ_TIMEOUT` / `TG_MIN_MAX_TOKENS` 等原先在请求路径裸 `int()` 解析，写成 `600s`、`32k` 或空串会抛 ValueError → 每个请求 500（/health 仍 200），启动期同错则进程起不来。改为 `_env_num()` 统一回退默认值并告警、越界值夹到端点，启动横幅改打印**生效值**；`LatencyStats` 预热与实时写入统一按「上游 id + 别名」双记（此前重启后按别名查不到速度样本），预热计数只统计有效样本；`config.example.json` 的 `auto_weights` 对齐新默认；新增 `tests/test_env_guard.py`（21 项） |
| 2026-11-07 | `4511ddf2` | **speed 因子改为实测耗时并与分档/质量等权**：旧的 `speed = 1 - quality` 与质量完全共线（开启等于抵消一半质量权重），故长期为 0；实测 122B 在 3k~20k 上下文比 8B 快 2.7 倍，参数量不能当速度代理。新增 LatencyStats 按 (模型, prompt 桶) 记录真实耗时并取中位数，`speed = tau/(tau+t)` 绝对软映射；样本不足给中性 0.40、不做跨桶回退；权重改为 tier/quality/speed 各 0.30 |
| 2026-11-07 | `aa5f4aec` | **修复工具调用被输出预算截断导致的 `Empty stream`**：上游把整个 `tool_call` 放在单个 chunk 里下发，客户端固定 `max_tokens=2048` 时命中「VLM 上游不强制」豁免 → 预算耗尽只回半截 JSON（`chunks=1`、`finish_reason=length`）→ 客户端判定零模型 token 并重试。豁免改为按请求类型判定：带图仍不强制，纯文本 + tools 抬到 `TG_MIN_MAX_TOKENS_TOOLS`（默认 8192）；新增 `tests/test_tools_budget.py` |
| 2026-11-05 | `67e5c773` | **回归用例入库 + 部署模板纠偏**：新增 `tests/test_ctx_trunc.py`（超窗截短不得 500）与 `tests/test_timeout.py`（静默预算 / 缓冲发送 / 504·502 / 流式错误事件，27 项），两者自包含、用临时端口与 stub 上游，不碰线上实例；`com.token-gateway.server.plist` 的 `TG_READ_TIMEOUT` 240 → 600 并补 `TG_BUFFER_NONSTREAM=1`，与生产一致 |
| 2026-11-05 | `21d14d3b` | **修复 `500 internal_error`（`KeyError: 'max_tokens'`）**：122B 上游属 VLM 组 → `TG_MIN_MAX_TOKENS` 对其实效为 0 → 网关不补写 `max_tokens`；客户端也未传；超上下文窗口进入"截短历史"分支时日志无条件下标读取该键即崩。改为统一用局部变量、加类型兜底，并让兜底异常记录完整堆栈 |
| 2026-11-05 | `4902de3e` | **上游开启 APC（prefix cache）**：mlx_vlm 的 `APC_ENABLED` 默认 0，导致 agent 每轮重发 6 万上下文全量 prefill（实测 62,105 P / 67 C 耗时 247.8s）；开启后 29.7k 上下文相同前缀 14.3s → 0.29s、追加一轮 0.50s。README 新增「上游加速」章节，并修正 launchd EIO 误报的处理方式 |
| 2026-11-05 | `17c183d0` | **长上下文不再"被取消"**：`TG_READ_TIMEOUT` 明确为单次静默预算；非流式改为缓冲后发送并带 `Content-Length`；超时返回 504 `upstream_timeout`、断流/未收全返回 502 `upstream_broken`；超时不重试；新增 `TG_BUFFER_NONSTREAM`；修复 auto 繁忙计数释放时机（上游读完即释放，避免误判繁忙跳到弱模型） |
| 2026-11-04 | `319fcbdf` | **auto 新增 `balanced` 策略并设为默认**：请求分档（light/standard/deep）+ 分档与质量**等权**加权（tier 0.35 / quality 0.35 / pref 0.15 / quant 0.10）；新增 `X-Gateway-Auto-Tier` 响应头与 `auto_weights`、`auto_tier_ideals`、`auto_tier_light_chars`、`auto_tier_deep_chars` 配置 |
| 2026-11-04 | `2c6f20a1` | auto 改为 `quality` 精度优先：参数量 → 量化排序，显式优先表可覆盖；繁忙时让位次优空闲模型 |
| 2026-11-04 | `bb5c6664` | auto 新增 `least-busy`：按网关侧 in-flight 避让繁忙模型，全繁忙时取最低负载 |
| 2026-11-03 | `c3d26fb9` | 管理台模型列表展示 `auto` 虚拟条目（`upstream:"gateway"`、`details.virtual:true`） |
| 2026-11-03 | `3fdf4231` | 修复 `deny_models: ["auto"]` 被静默忽略——现在明确返回 403 |
| 2026-11-03 | `157b8a79` | 引入 `auto` 虚拟模型：候选池按 allow/deny/能力（chat/embeddings/多模态）过滤，响应头回传 `X-Gateway-Model` |
| 2026-11-02 | `3b38e7da` | 文档修正：`/v1/models`、`/api/tags` 实为免鉴权的模型发现接口 |
| 2026-11-02 | `ab78d2fd` | 文档修正管理接口鉴权头为 `X-Admin-Token`；补充 embeddings 示例与用量日志格式 |
| 2026-11-02 | `c4492f2f` | README 补齐 API 示例、配置优先级、HTTPS 与安全、故障排查 |
| 2026-11-02 | `65cc0861` | README 补齐 API 概览、配置字段参考、mem-guard LaunchAgent 安装、安全提示归并 |
| 2026-11-02 | `5e8e83d3` | 部署章节补充 `admin.token` 安全建议 |
| 2026-11-02 | `18bf916d` | 安全：去掉弱默认 `admin.token`，启动横幅不再打印 token，空 token 时告警 |
| 2026-11-02 | `307290f6` | 安全：移除 `DEFAULT_CONFIG` 中硬编码的真实 key，改为空字典 fail-closed |
| 2026-10-18 | `cdd38830` | README 与已 vendor 的服务端源码对齐 |
| 2026-10-18 | `3b4adc22` | 明确 `config.json` 端口优先于 `TG_*` 环境变量 |

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
