# 运行时看门狗（tg_watchdog）详细研究

> 研究时间：2026-11-12 19:38–19:45
> 对象：`tg_watchdog.py`（336 行）+ 网关集成（`token-gateway.py:5287-5397`）+ `tools/verify-watchdog.py` + `tests/test_watchdog.py`
> 方法：代码逐行读 + 8 组真实端点实验（不 mock probe）+ 线上日志回放
> 现场：网关 3000（pid 见 `/health` uptime）、上游 gmlx 8090（pid 11647）、`/tmp/token-gateway.log`、`~/.ollama-token-gateway/logs/watchdog-restart.log`

---

> **修订（2026-11-14）**：P1、P2 已修复并上线（`v2026.11.14.1`），两条都补了回归测试。
> 修复要点与"为什么代理那条必须用子进程才测得出来"记在 README「两条踩过的判据」。
> 下文的 P1/P2 段落保留原始分析，标注为「已修复」；P3 仍未修（成本取舍）。

## 0. 结论速览

| 维度 | 结论 |
|---|---|
| 设计目标 | 不是"探测 + 重启"，而是**在冷加载 / 长 prefill / 抖动三个场景下都不把自己弄死** |
| 当前状态 | 生产健康：`state=healthy`、`fails=0`、`restarts=0`、巡检间隔 20s、上一轮探测 6s 前 |
| 判据成熟度 | 高。11-09 的两个误判已修，11-11 的真实故障里判死失败次数从「7~8 次」降到「3 次」＝阈值本身 |
| 主要缺口 | ① 退避计数会被"稳定期"提前清零（有线上日志留痕 + 可复现）② 健康检查硬依赖 `NO_PROXY=*` ③ 只验"模型清单可读"，不验"能推理" |
| 复用性 | 真·零依赖：不 import 网关，4 个外部依赖全可注入，退化模式可跑 |

---

## 1. 体系全景：这个仓库里其实有 5 层守护

"watchdog"在口头上一件事，在代码里是五层，各自管不同的失效。混在一起看会得出错误结论（例如以为"网关挂了会有人拉起"）。

| 层 | 谁 | 管什么 | 判据 | 失效后果 | 当前是否生效 |
|---|---|---|---|---|---|
| L0 | launchd `com.token-gateway.server`（`KeepAlive=true`） | 网关进程退出 | 进程退出码 | 网关整体不可用 | 沙箱内 `launchctl` 读不到（返回空列表），**待人工确认** |
| L1 | `deploy/maintain-gateway.sh`（每 30 min） | 网关端口 200 | `curl /health` | 兜底 L0 | ✅ 实测每半点跑（19:00:21 / 19:30:21） |
| L2 | `tg_watchdog.py`（每 20s，网关内 daemon 线程） | 上游 gmlx `:8090` | `GET /v1/models` 2xx + TCP 活性 | 推理全挂 | ✅ `healthy` |
| L3 | 网关内另三条巡检线程 | 槽位泄漏 / 模型清单 / Ollama 保活 | 各自 | 并发假死、模型漂移、冷加载 | ✅ 均在跑（5s / 60s / 240s） |
| L4 | `mem-guard.mjs`（设备内存 90%） | 整机内存 | `vm_stat` 口径 | OOM 前预警 | ❌ **未安装**，且模板 plist XML 非法 |
| L5 | `tools/verify-watchdog.py`（手动） | 端到端自愈 | 真 SIGKILL 后能否生成 | —— | ✅ 只读模式可用（pid 11647） |

### 分工的边界（重要）

```
launchd ──管──> 网关进程 ──内含──> 看门狗线程 ──管──> gmlx 上游
   │                                    │
   └── 若 launchd 不可用 ──> start-gateway.py（手动）
                                        └── 网关死了，看门狗一起死
```

**看门狗无法救网关自己**：它是网关的 daemon 线程。网关一挂，巡检就没了。所以 L2 的可靠性上限由 L0/L1 决定，不是由 `tg_watchdog.py` 决定的。这是必须理解的第一件事。

### 为什么不做成独立守护进程（代码与文档给的三条理由，我认同）

1. 只有网关知道"有没有请求在飞"，而这个数决定巡检该不该动手；
2. 少一个进程少一个"守护者自己也挂了"的失效面；
3. 两个托管方会抢同一端口（`com.llama.flash-next.plist` 就是被 `.disabled` 掉的那个）。

代价是第 1 节那条：看门狗与网关同生共死。

---

## 2. 核心状态机

代码里实际有 **7 个状态**（README 写的是另一套，见 §7 P5）：

| 状态 | 含义 | 谁会看到 |
|---|---|---|
| `init` | 刚初始化，还没探测过 | 启动瞬间 |
| `healthy` | 探测 2xx | 常态 |
| `starting` | 在宽限期 / 退避期内（`now < next_attempt`），**只观察不计数** | 重启后 150s |
| `degraded` | 端口在听（TCP 通），但 HTTP 不回或非 2xx | 卡死中 |
| `down` | 端口无监听，失败次数未到阈值 | 刚崩 |
| `restarting` | 已拉起重启脚本 | 重启后 |
| `failed` | 重启脚本都拉不起来 | 脚本缺失/异常 |

### 转移矩阵（默认参数下的真实时间）

| 故障形态 | 探测节奏 | 判死条件 | 检测延迟 | 动作 |
|---|---|---|---|---|
| 进程死了（端口无监听） | 每 20s | 连续 3 次（有在飞请求时 6 次） | **40~60s** | `start-flash.py` |
| 进程活着但卡死（端口在听不回） | 每 20s | `first_fail` 起 ≥300s | **≈305s** | `start-flash.py` |
| 重启还没好 | 每 20s | 不判（`starting`） | —— | 只记 `grace_fails` |
| 重启脚本缺失 | —— | 立即 | —— | 转 `failed`，只记日志 |

实测（实验 2，把 HUNG 缩到 6s 等价）：

```
t+ 1.0s  state=degraded  fails=1 first_fail=1.0 重启=0
t+ 3.2s  state=degraded  fails=2 first_fail=3.2 重启=0
t+ 5.4s  state=degraded  fails=3 first_fail=5.4 重启=0
t+ 7.6s  state=restarting fails=0 first_fail=0.0 重启=1
```

**卡死分支不参与阈值判定**——它只用 `first_fail` 计时。所以"连续 3 次"只对"端口无监听"生效。这一点在 README 里没写清楚，容易让人以为卡死也是 3 次 × 20s。

---

## 3. 五条反直觉判据：为什么要这么写

这部分是这套代码真正的价值。每一条都对应一次踩坑，注释里写明了"不这么做会怎样"。

### 3.1 宽限期内**不计数**（`grace_fails` 独立）

不这么写的后果是精确可算的：宽限期 150s ÷ 探测间隔 20s + 1 = 8 次，于是"告警时刻恒等于宽限期到期那一刻"。

线上留痕（`/tmp/token-gateway.log`）：

| 时刻 | 判死时的失败次数 | 说明 |
|---|---|---|
| 2026-11-09 20:20:44 | **8** | 修复前：宽限期内的失败被累加 |
| 2026-11-09 21:24:18 | **7** | 同上 |
| 2026-11-11 16:36:58 | **3** | 修复后：正好等于阈值，说明期间确实连续失败 3 次 |

这三行是整个修复最硬的证据：**同一套判据，修复后用真实故障复现出"3"，而不是算术产物"7/8"**。

### 3.2 宽限期必须 > 冷启动时间（150s vs 75s）

111 GB 权重冷加载约 75s。宽限设 30s → 服务还在加载就被判死 → 再重启一次 → 自杀循环。150s 是 2× 余量。

同一逻辑的第二个数字是 **`WD_HUNG_TIMEOUT=300s`**：要覆盖"冷加载 + 长 prefill"。131k 上下文的 prefill 可能把服务堵几十秒，这段时间端口是在听的（TCP 握手由内核 backlog 完成），HTTP 不回——正是 `degraded` 分支存在的理由。

### 3.3 有请求在飞时阈值翻倍

`need = WD_FAIL_THRESHOLD * (2 if inflight_total() else 1)`。

理由：正在服务用户的进程不该被巡检打断。131k prefill 期间杀掉它 = 自杀。

**但这道保护有个缺口**：`need` 只在"端口无监听"分支用。卡死分支（`alive=True`）不看 `need`，300s 到点就走。若某次 prefill 真的超过 300s，即使有在飞请求也会被重启。这是一处可以改进的地方（见 §7 P4）。

### 3.4 重启命令不复制启动参数

`restart()` 只 `Popen(tools/start-flash.py)`，不自己拼命令行。注释写得对：

> 启动参数的事实来源是 `flash-runtime.json`，复制一份进看门狗迟早会漂移（症状是「手工起得动、自愈起不动」）。

配套细节：`start_new_session=True`（脱离会话）、`NO_PROXY=*`（重启脚本自己也要直连）、日志 fd 在父进程关掉（"免得每次重启漏一个句柄"）、`--timeout 300` 传给脚本。

### 3.5 稳定期才清零退避

`WD_STABLE_RESET=300`：恢复后连续健康 300s 才把 `restarts` 归零，避免抖动期反复全速重试。

**这条是本报告发现的 bug 所在**，见 §6 P1。

---

## 4. 实测行为（8 组实验）

全部用真实 HTTP/TCP 端点，不 patch `probe`。

### 4.1 四种端点的判定

| 端点 | probe 判定 | 耗时 | tcp_alive | 错误 |
|---|---|---|---|---|
| 200 正常 | 健康 | 0.0s | True | — |
| 500 报错 | 不健康 | 0.0s | True | `HTTPError: 500` |
| 在听但不回 | 不健康 | **2.0s**（= 探测超时） | **True** | `TimeoutError` |
| 无监听 | 不健康 | 0.0s | **False** | `HTTPError: 502` ※ |

※ 502 暴露了一个隐藏依赖：**probe 会走 `HTTP_PROXY`**。当前 shell 的代理是 `http://127.0.0.1:55641`，它把"连不上"翻译成了 502。见 §6 P2。

### 4.2 `healthy` 不等于"能推理"

| 端点行为 | 结果 |
|---|---|
| `GET /v1/models` → 200 | probe 判**健康** |
| `POST /v1/chat/completions` → 500 | 上游实际不可用 |

实测输出：

```
GET /v1/models → probe 判定：健康
POST /v1/chat/completions → 失败 500
```

这是设计上的取舍（每 20s 做一次真实生成成本太高），但必须知道：**模型在、清单能读、但推理全 500 时，看门狗不会动**。这正是 `verify-watchdog.py` 在重启后补一次真实生成的原因——它验的是链路，不是端口。

### 4.3 退避序列

| 场景 | 实测 |
|---|---|
| 连续三次故障、期间没恢复过 | `150 → 300 → 600` ✅ |
| 每次故障之间恢复过一次健康 | `150 → 150` ❌（第二轮应为 300，见 P1） |

---

## 5. 线上运行证据（历史事件回放）

`~/.ollama-token-gateway/logs/watchdog-restart.log` 一共 3 次重启，全部成功：

| 时刻 | 判死理由 | 恢复 |
|---|---|---|
| 2026-11-09 20:20:44 | 端口无监听（连续 **8** 次） | 20s 后 ready，新 pid 35091 |
| 2026-11-09 21:24:18 | 端口无监听（连续 **7** 次） | 新 pid 50905 |
| 2026-11-11 16:36:58 | 端口无监听（连续 **3** 次） | 新 pid 67162 |

当前（19:40）：

```json
"watchdog": {"enabled": true, "interval_s": 20,
  "targets": {"http://127.0.0.1:8090": {"state": "healthy", "fails": 0,
    "grace_fails": 0, "restarts": 0, "last_check_ago_s": 6, "last_error": null}}}
```

`verify-watchdog.py` 只读模式跑通：基线 healthy、`8090 up=True`、目标 pid 11647、给出下一步命令后 `exit 3`。默认不杀进程的门禁是对的。

---

## 6. 发现的问题（按严重度）

### P1 🔴 `healthy_since` 从不重置 → 退避序列实际失效　**[已修复 v2026.11.14.1]**

**代码**：`tick()` 健康分支

```python
if not st["healthy_since"]:
    st["healthy_since"] = now          # 只在 0 时写一次
if st["restarts"] and now - st["healthy_since"] >= WD_STABLE_RESET_S:
    st["restarts"] = 0                 # 用它当"自上次重启起"的计时器
```

`healthy_since` 的语义是"自某次成功探测起"，却被当成"自上次重启起"用。重启时不清零，于是**网关长跑后再重启上游，第一次恢复健康就会把 `restarts` 清零**。

**线上留痕**（`/tmp/token-gateway.log`，11-11）：

```
[2026-11-11 16:36:58] 🚑 8090 不可用（端口无监听（连续 3 次探测失败）），已拉起重启脚本
[2026-11-11 16:37:18] 🐕 8090 已稳定 149972s，重启退避计数归零
```

20 秒后判"已稳定 149972s"（= 41.6 小时 = 网关 11-09 22:57 启动至今）。而设计是"稳定 300s 才归零"。

**可复现实验**（真实端口起落，非 mock）：

```
初始 healthy_since = 3 小时前
① 判死：restarts=1 退避=150s（期望 150s）
② 恢复：state=healthy restarts=0     ← 被「已稳定 10800s」清掉
③ 再判死：restarts=1 退避=150s（设计应为 300s）
```

**影响**：只要"重启 → 恢复过一次 → 又挂"，退避永远停在 150s，`300/600/1200/1800` 与封顶永不生效。抖动场景下会变成"每 150s 拍一次重启"，对上游是持续打扰。

**修法**（一行）：

```python
# tick() 的判死段，设置 next_attempt 之后
st["healthy_since"] = 0.0     # 重启后重新计「稳定期」，否则下一次健康立刻清零计数
```

**回归测试**（补进 `tests/test_watchdog.py` §3）：构造 `restarts=2, healthy_since=now-10000`，tick 健康后断言 `restarts == 2`；再过 300s（伪造 `healthy_since = now-301`）后才为 0。

### P2 🔴 健康检查硬依赖 `NO_PROXY=*`　**[已修复 v2026.11.14.1]**

`probe()` 用 `urllib.request.urlopen`，默认 opener 会读 `HTTP_PROXY`。子进程首调实验：

| 环境 | probe 结果 |
|---|---|
| `HTTP_PROXY=http://127.0.0.1:1`（死代理） | **不健康** `Connection refused` |
| `HTTP_PROXY=http://127.0.0.1:65388`（真代理） | **不健康**（当时代理未起） |
| `HTTP_PROXY=... + NO_PROXY=*`（线上 plist 配置） | **健康** |

**影响**：当前 `com.token-gateway.server.plist` 有 `NO_PROXY=*`，所以没出事。但只要有人改 plist 时漏掉这一项，代理一挂，**一个完全健康的上游会被判死并反复重启**——这是最坏的一类误判（健康时的自杀循环）。

另有一个隐蔽细节：`urllib` 的默认 opener 在**进程内首次调用时固化**代理配置，之后改环境变量不生效。所以这类问题只能靠新进程复现，排障时容易看错。

**修法**：让 `probe()` 自己拿一个"无代理 opener"，不依赖环境：

```python
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def probe(base):
    ...
    with _OPENER.open(req, timeout=WD_PROBE_TIMEOUT_S) as r:
```

`tcp_alive()` 已经是裸 socket，不受影响。

### P3 🟡 健康判据只覆盖"清单可读"

`GET /v1/models` 200 + 推理 500 = 判健康（实验 4.2）。这是成本取舍，不建议默认改（每 20s 一次真实生成太贵）。可选方案：

- 加 `TG_WD_DEEP_PROBE=0`（默认关）：开启后每 N 轮做一次 `max_tokens=1` 的生成；
- 或由网关的 5xx 计数驱动：连续 M 个 5xx 时把该上游标成"可疑"，触发一次深度探测。

现状至少有一层兜底：`verify-watchdog.py` 在自愈后会真跑一次生成。

### P4 🟡 多目标时重启脚本是单例

`TG_WD_TARGETS` 支持逗号分隔多个目标，`STATES` 按 base 分开记，但 `restart()` 只有一个脚本（默认 `<config_dir>/tools/start-flash.py`）。配了 `11436`（Ollama 泳道）也不会拉起它，只会重复拉 gmlx。

**修法**：`configure()` 增加 `restart_map={"http://127.0.0.1:11436": "tools/start-ollama-lane.py"}`；或目标写成 `url=script` 形式。当前生产只有一个目标，不影响使用，属于扩展性缺口。

### P5 🟡 README 与代码漂移（4 处）

| README 写的 | 代码实际 |
|---|---|
| state 枚举 `healthy / degraded / restarting / backoff / grace` | `init / healthy / starting / degraded / down / restarting / failed`（没有 backoff、grace；有 down、failed） |
| JSON 快照只有 5 个字段 | 实有 `grace_fails`、`last_error`（排查时最需要的两个恰恰缺了） |
| 日志 `🐕 看门狗: 目标 X 连续 N 次探测失败 → 重启` | 实际 `🚑 {base} 不可用（{reason}），已拉起重启脚本 pid N（第 K 次，日志 …）` |
| `tg_watchdog.py`（323 行） | 336 行 |

文档是最先被读的东西，`state` 枚举写错会让人在 `/health` 里看到 `down` 时以为出了新故障。

### P6 🟡 内存层（L4）完全没接上

- `~/Library/LaunchAgents/com.user.memguard.plist` **不存在**（只在仓库里）；
- 仓库里那份 XML **非法**：注释里出现 `--cmd`，XML 注释不允许 `--`，`plistlib.load` 直接 `not well-formed (line 14, column 41)`；
- 也就是说，`launchctl load` 必然失败，内存 90% 预警从来没跑过。

**修法**：把注释里的 `--cmd` 改成 `--cmd（详见 README）` 或整段移出注释；再做一次真实 load 验证。

### P7 ⚪ launchd 托管状态无法在沙箱内确认

`launchctl list` 返回 **0 行**（连系统作业都没有），`launchctl print gui/$(id -u)/com.token-gateway.server` 报 `Could not find service … 503`。这两条都可能是沙箱限制，不能据此断言"launchd 没托管"。

README 记录过实测（kill -9 后 2s 恢复 200），倾向于是可信的。**需要人工在普通终端确认**：

```bash
launchctl print gui/$(id -u)/com.token-gateway.server | head -20
```

若确实没加载，L0 就是空的，网关存活只剩 L1 的 30 分钟兜底——意味着最长 30 分钟不可用。

---

## 7. 参数标定：每个数是怎么来的

| 参数 | 值 | 依据 | 若改小 |
|---|---|---|---|
| `TG_WD_INTERVAL` | 20s | 探测开销 vs 发现速度 | 5s 下限；更密只是更早发现，判死次数不变 |
| `TG_WD_FAIL_THRESHOLD` | 3 | 连续 3 次 ≈ 40~60s | 1 次 → 单次抖动就重启 |
| `TG_WD_PROBE_TIMEOUT` | 5s | 要容忍 prefill 期间的 accept 排队 | 太小 → 正常繁忙被判不健康 |
| `TG_WD_GRACE` | 150s | 冷加载 75s × 2 | <75s → 自杀循环 |
| `TG_WD_HUNG_TIMEOUT` | 300s | 冷加载 + 长 prefill | 太小 → 长 prefill 被当卡死 |
| `TG_WD_BACKOFF_BASE/MAX` | 150 / 1800 | 抖动期降频 | 基数太小 → 反复拍上游 |
| `TG_WD_STABLE_RESET` | 300s | 抖动判定窗口 | 太小 → 退避形同虚设（当前 bug 让它恒等于"立即"） |

**端到端 SLA（默认参数）**：

```
进程崩溃 → 检测 40~60s（3 次 × 20s，含 5s 探测超时）
        → 重启 20s（页缓存热）/ 75s（冷）
        → 合计 60~135s 不可用
```

线上三次实测都是 20s 恢复（页缓存热），与估算一致。

---

## 8. 复用清单（搬到别的项目）

代码本身是零依赖的，真正会踩的是这四件事：

1. **重启脚本必须是启动参数的唯一事实来源**，不要在 watchdog 里拼命令行；
2. **`inflight_fn` 一定要接**，否则长 prefill 期间会被误打断；
3. **`GRACE` > 冷启动时间**，这是最容易抄错的数（本机 75s → 150s）；
4. **`probe` 显式绕代理**（见 P2），别依赖调用方的环境变量。

---

## 9. 建议的落地顺序

| 优先级 | 动作 | 工作量 | 风险 |
|---|---|---|---|
| 1 | 修 P1（重置 `healthy_since`）+ 补回归测试 | 1 行 + 15 行测试 | 极低 |
| 2 | 修 P2（无代理 opener）+ 测试 | 3 行 | 极低 |
| 3 | 修 P5（README 的 state 枚举 / 字段 / 日志文案 / 行数） | 文档 | 无 |
| 4 | 修 P6（memguard plist 注释里的 `--`） | 1 处 | 无（本来就没跑） |
| 5 | 人工确认 P7（launchd 是否真的托管网关） | 一条命令 | —— |
| 6 | P4 多目标 restart 映射 | 设计改动 | 中，等真有第二个目标再做 |
| 7 | P3 深度探测开关 | 新增能力 | 中，默认关 |

前 4 项都是"不做就静默变差"的类型：P1/P2 的失效方式都是"看起来在守护，实际在瞎守护"。
