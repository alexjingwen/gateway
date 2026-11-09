# 2026-11-09 三个问题复盘：误杀、档位、空 admin token

> 现场：`/tmp/token-gateway.log`、`~/.ollama-token-gateway/logs/watchdog-restart.log`、
> `~/.cache/gmlx/server-*-8090.log.1`。文中 API key 一律截断到前 12 字符。

## 结论速览

| # | 现象 | 根因 | 性质 |
|---|---|---|---|
| 1 | `:8090` 一小时「崩两次」 | 看门狗把「启动宽限期内的失败」计入同一计数器，告警时刻恒等于宽限期到期那一刻；真实下线是被 **SIGKILL**（外部终止），不是 gmlx 自身崩溃 | 监控伪信号 + 外部误杀，双层 |
| 2 | `auto` 选 27B 而非 flash-next | **配置设计使然，不是缺陷**。125B 被 `TG_AUTO_DEEP_ONLY` 锁进深度档，8B/3.8B 被 `TG_AUTO_EXCLUDE` 全部排除，轻量档候选池只剩 27B 一个 | 上一轮判断有误，此处更正 |
| 3 | `admin.token` 为空 | `_handle_admin` 用 `if configured_token and token != configured_token` 短路 —— token 为空即等于「关闭鉴权」，配置里确实是 `""` | 设计如此，但暴露面真实 |

---

## 问题 1：看门狗在报告一个不存在的时间点

完整时间线：

| 时刻 | 事件 |
|---|---|
| 20:18:04 | 网关实例启动，看门狗初始化，`next_attempt = now + 150s` |
| 20:18:24 → 20:20:24 | 7 次探测失败，全部落在宽限期内，只累加 `fails` 不动作 |
| **20:20:44** | 宽限期到期后的第一个 tick，`fails=8` ≥ 阈值 → 判死重启 |
| 20:21:04 | 恢复健康（20s，权重在页缓存里，冷启动很快） |
| 20:58:53–20:59:56 | launchd 作业 6 次 `Address already in use` 崩循环 |
| **21:21:38** | 网关重新启动，宽限期重置 |
| **21:24:18** | 同样模式：宽限期 150s 到期即判死，`fails=7` |

**为什么失败次数是 7/8 而不是阈值 3**：`tg_watchdog.py:232` 的
`if now < st["next_attempt"]: state="starting"; return` 在宽限期内提前返回，但
**前面已经执行了 `st["fails"] += 1`**（第 228 行）。于是宽限期内每 20s 累加一次，
待到点一次性判死。两次事件的失败数（8、7）恰好等于「150s ÷ 20s + 1」——
这是算术指纹，不是 8090 真的连续失败了 7 次。

**为什么说不是 gmlx 崩溃**：

- `~/.cache/gmlx/server-*-8090.log.1` 最后一行是 21:00:44 正常完成的请求，
  **没有 uvicorn 的 `Shutting down` / `Application shutdown complete`**
  （对比 8091 实例，它被正常停止时完整打印了这三行）
- 无 traceback、`~/Library/Logs/DiagnosticReports` 无当日 `.ips`、
  `/Library/Logs/DiagnosticReports` 无当日 JetsamEvent
- 无 shutdown 日志 + 无崩溃报告 = **SIGKILL 的典型签名**

**最可能的下手者**：20:18–21:24 之间有一整套 launchd 手术在进行 ——
`com.token-gateway.server.plist` 改于 20:18、`com.llama.flash-next.plist` 禁用改于 20:54、
`fix-launchd.sh` 改于 21:01，脚本里明确给出
`pkill -f 'token-gateway\.py'; launchctl kickstart -k` 的交接动作。
gmlx 的下线窗口与这套操作完全重叠，判为**同一批运维动作的连带误杀**，
置信度高但未拿到直接证据。

**验证缺口**：本沙箱内 `log show` 一律返回 `Cannot run while sandboxed`
（加 `dangerouslyDisableSandbox` 也一样）。要钉死这一幕需手工跑：

```
sudo log show --start "2026-11-09 20:55:00" --end "2026-11-09 21:26:00" \
  --predicate 'eventMessage CONTAINS "memorystatus" OR eventMessage CONTAINS "jetsam"'
```

结果为空即可排除内存被杀，「外部 kill」成立。

**不是 bug 的疑点**：两次都记「第 1 次」是正常的 —— `WD_STABLE_RESET_S=300`，
20:26:04 的 `已稳定 300s，重启退避计数归零` 证明计数被正确复位。

---

## 问题 2：不是缺陷，是配置决定的

`~/Library/LaunchAgents/com.token-gateway.server.plist` 里的环境变量决定了候选池：

```
TG_AUTO_DEEP_ONLY = qwen3.8-flash-next        # 125B 只服务深度档
TG_AUTO_EXCLUDE   = gemma4:latest,qwen2.5vl:3b  # 8B 与 3.8B 全部排除
TG_MODEL_PARAMS   = qwen3.8-flash-next=125    # 参数量覆写已就位
```

网关日志的完整判词：

> `auto → qwen3.8:27b（文本请求；1 个模型仅限深度档，本档（轻量）不参与；分档 轻量（短问答 9 字符）综合 0.546 择优 27.3B Q4_K_M（tier=0.31 quality=0.75 speed=0.70 quant=0.17））`

轻量档里 125B 被摘掉、8B/3.8B 本就 exclude，`quality=0.75` 是「池里只剩一个尺寸」
的中性值 → **候选仅 1 个，选 27B 是唯一合法结果**。
上一轮说「与以 flash 为主不一致」是把 125B 当成了小模型，实际上 flash-next 就是 125B
（alias `qwen3.8-flash-125b` 可证），所以轻量档不用它**是正确的**。

**但存在真实成本问题**：轻量档唯一残留的 27B 反而最慢。同一句话实测：

| 路由 | 用时 | 输出 |
|---|---|---|
| `auto` → qwen3.8:27b | **11.3 s** | 61 tokens（46 个是 reasoning） |
| `qwen3.8-flash` → flash-next | **1.4 s** | 19 tokens |

原因是日志里那句 `提升 max_tokens: 16 → 2048（文本输出下限）` 把短请求的 max_tokens
抬到 2048，27B 于是展开推理链；而 125B 走 gmlx 投机解码，短输出反而更快。
**「按档位省成本」在候选池只剩 27B 时完全失效。**

---

## 问题 3：admin.token 为空 = 鉴权关闭

代码（`token-gateway.py:3792-3798`）：

```python
token = self.headers.get("X-Admin-Token", "")
configured_token = cfg["admin"].get("token", "")
if configured_token and token != configured_token:   # 空字符串 → 短路，直接放行
```

`config.json` 里 `"admin": {"token": ""}`，实测：

| 请求 | 结果 |
|---|---|
| `GET /api/admin/status` 无任何头 | **200**，返回完整内容 |
| 同上 + `X-Admin-Token: wrong` | **200**（错误 token 也不拦） |

泄露面比「无鉴权」更严重：`/api/admin/status` 的 `keys` 字段**以完整 API Key 为键**
返回（`sk-ollama-b254b42b…`），并带 `POST /api/admin/keys` 增删能力。
同时 `listen_host = 0.0.0.0`，局域网 `192.168.3.67:3000` 可直接访问；本机还有
`frps`（7100/7500/8080）与 ipv6-ddns 在跑，若 3000 被端口转发出去，等于公网裸奔。

设计意图在代码注释与启动横幅里写明 —— 「依赖 Tailscale 网络层鉴权」，
启动会主动打印 `⚠️ 安全警告：admin.token 为空`。所以是**有意的取舍**，
但前提是「只经 Tailscale 访问」，而 `0.0.0.0` 监听与这个前提矛盾。

---

## 待办（按风险排序）

1. **补 `admin.token`**：设强 token 并 `launchctl kickstart -k gui/$(id -u)/com.token-gateway.server`；
   若不设，至少把 `listen_host` 收到 `127.0.0.1` 并确认 3000 未被 frp 转发。
2. **看门狗遥测修复**：宽限期内不累加 `fails`，或单设 `grace_fails`；判死理由改用
   「自 `first_real_fail` 起的时长」。不改这条，「崩了几次」永远说不清。
3. **轻量档要么真有轻模型，要么取消该档**：把 `gemma4:latest` 从
   `TG_AUTO_EXCLUDE` 移除并预热速度样本，或 `TG_AUTO_TIER=0` 取消分档。
