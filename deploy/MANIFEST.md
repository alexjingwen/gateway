# deploy/ —— 线上部署目录的非核心文件

这个目录镜像线上部署目录里**除网关主程序之外**的东西。主程序（`token-gateway.py`、
`tg_watchdog.py`、plist、`flash-runtime.json`、`gmlx-serve.yaml`、`start-*.py`）在仓库根和
`tools/`，由 `tools/rollback.py` 的 MAPPING 负责还原到本机。

这里的文件**没有自动还原路径**：它们是参考资料 —— 想知道「这套网关周边还跑着什么、
当初怎么装的」时看这里，而不是靠记忆。

## 归档范围

| 类别 | 文件 |
|---|---|
| 客户端分发 | `client-distributor.py` + plist |
| 面板 | `ollama-panel.py` + plist |
| OpenAI 兼容代理 | `ollama-openai-proxy.py` + plist |
| 维护任务 | `maintain-gateway.sh` + `gateway.maintain.plist` |
| 安装脚本 | `install-ollama-service.sh`、`install-proxy.sh`、`configure-openwebui.sh` |
| 模型工具 | `merge_gguf_v2.py` |
| 网络 | `ipv6-ddns.py` |
| frp 内网穿透 | `frp/install-frp.sh`、`frp/check-public.py`、`frp/com.frp.{client,server}.plist` |

## 脱敏

**10 处**硬编码的现网 API key 已替换为 `sk-ollama-REPLACE_WITH_YOUR_KEY`：

| 文件 | 处数 |
|---|---|
| `install-ollama-service.sh` | 3 |
| `install-proxy.sh` | 2 |
| `configure-openwebui.sh` | 1 |
| `ollama-openai-proxy.py` | 1 |
| `ollama-panel.py` | 1 |
| `com.ollama.custom.plist` | 1 |
| `com.ollama.proxy.plist` | 1 |

所以**这些文件不能直接拷回线上用**，要先填真实 key（或用环境变量
`OLLAMA_PROXY_API_KEY` 注入）。这是刻意的：仓库是 public，而这颗 key 从 2026-10-18
起就在历史里泄露过（见根目录 README 的安全章节）。

顺带记一个维护事实：**轮换这颗 key 要改 7 个文件**（这 7 个 + `config.json`），
不是只改配置。

## 故意不归档

| 内容 | 原因 |
|---|---|
| `frp/bin/frpc`、`frp/bin/frps`（33 MB） | 二进制，版本由 `install-frp.sh` 决定 |
| `frp/frpc.toml`、`frp/frps.toml` | 含 `auth.token`（38 字符）与 `webServer.password` |
| `token-gateway.py.bak*`（21 个，3.2 MB） | 手工备份，**其中 4 个含旧 key** —— 历史泄露源；GitHub 本身就是版本存档，不需要它们 |
| `frp/logs/*.log`、`logs/` | 运行时产物 |
| `.venv/`、`__pycache__/` | 环境 |
| `clients/`（分发产物） | 生成的安装包 |

`.gitignore` 已按通配挡住上述类别，`tools/push-via-api.py` 推送前还会扫整棵树，
命中 `sk-` key / GitHub PAT / 私钥 / `auth.token` 类字段就中止。
