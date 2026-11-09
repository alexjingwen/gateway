#!/bin/bash
set -e

# install-frp.sh 位于 frp/ 子目录下，项目根目录是其父目录
FRP_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$FRP_DIR/.." && pwd)"
FRPS_BIN="$FRP_DIR/bin/frps"
FRPC_BIN="$FRP_DIR/bin/frpc"
FRPS_CONF="$FRP_DIR/frps.toml"
FRPC_CONF="$FRP_DIR/frpc.toml"
FRPS_PLIST_SRC="$FRP_DIR/com.frp.server.plist"
FRPC_PLIST_SRC="$FRP_DIR/com.frp.client.plist"
FRPS_PLIST_DST="$HOME/Library/LaunchAgents/com.frp.server.plist"
FRPC_PLIST_DST="$HOME/Library/LaunchAgents/com.frp.client.plist"

echo "=========================================="
echo "  FRP 内网穿透 - 安装脚本"
echo "=========================================="
echo ""
echo "项目目录: $PROJECT_DIR"
echo ""

if [ "$EUID" -eq 0 ]; then
    echo "❌ 请不要使用 sudo/root 运行此脚本"
    exit 1
fi

if [ ! -x "$FRPS_BIN" ]; then
    echo "❌ 找不到 frps 二进制: $FRPS_BIN"
    exit 1
fi
if [ ! -x "$FRPC_BIN" ]; then
    echo "❌ 找不到 frpc 二进制: $FRPC_BIN"
    exit 1
fi

echo "[1/6] 替换配置文件中的路径占位符..."
python3 -c "
import sys
project_dir = sys.argv[1]
files = sys.argv[2:]
for f in files:
    with open(f, 'r') as fh:
        c = fh.read()
    c = c.replace('REPLACE_WITH_PROJECT_PATH', project_dir)
    with open(f, 'w') as fh:
        fh.write(c)
" "$PROJECT_DIR" "$FRPS_CONF" "$FRPC_CONF" "$FRPS_PLIST_SRC" "$FRPC_PLIST_SRC"
echo "   ✅ frps.toml / frpc.toml / plist 路径已替换"

echo ""
echo "[2/6] 停止旧 FRP 服务（如果存在）..."
launchctl unload "$FRPS_PLIST_DST" 2>/dev/null || true
launchctl unload "$FRPC_PLIST_DST" 2>/dev/null || true
pkill -f "frp/bin/frps" 2>/dev/null || true
pkill -f "frp/bin/frpc" 2>/dev/null || true
sleep 1
echo "   ✅ 完成"

echo ""
echo "[3/6] 安装 launchd plist..."
mkdir -p "$HOME/Library/LaunchAgents"
cp "$FRPS_PLIST_SRC" "$FRPS_PLIST_DST"
cp "$FRPC_PLIST_SRC" "$FRPC_PLIST_DST"
chmod 644 "$FRPS_PLIST_DST" "$FRPC_PLIST_DST"
echo "   ✅ 已复制到 ~/Library/LaunchAgents/"

echo ""
echo "[4/6] 启动 frps 服务端..."
launchctl load "$FRPS_PLIST_DST"
sleep 2
if pgrep -f "frp/bin/frps" > /dev/null; then
    echo "   ✅ frps 已运行 (PID: $(pgrep -f 'frp/bin/frps' | head -1))"
else
    echo "   ⚠️  frps 可能未启动，检查日志: /tmp/frps.err.log"
fi

echo ""
echo "[5/6] 启动 frpc 客户端..."
launchctl load "$FRPC_PLIST_DST"
sleep 3
if pgrep -f "frp/bin/frpc" > /dev/null; then
    echo "   ✅ frpc 已运行 (PID: $(pgrep -f 'frp/bin/frpc' | head -1))"
else
    echo "   ⚠️  frpc 可能未启动，检查日志: /tmp/frpc.err.log"
fi

echo ""
echo "[6/6] 验证服务连通性..."
echo ""
echo "   本地直连 Token 网关 (3000):"
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:3000/health 2>/dev/null || echo "失败")
echo "      /health -> $CODE"

echo ""
echo "   通过 FRP 代理访问 (7001 -> 3000):"
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:7001/health 2>/dev/null || echo "失败")
echo "      /health -> $CODE"

echo ""
echo "   FRP Dashboard (7500):"
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:7500/ 2>/dev/null || echo "失败")
echo "      -> $CODE"

LOCAL_IP=$(ifconfig | grep "inet " | grep -v 127.0.0.1 | head -1 | awk '{print $2}')
PUBLIC_IP=$(curl -s --max-time 5 https://myip.ipip.net 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "未知")

echo ""
echo "=========================================="
echo "  FRP 内网穿透配置完成！"
echo "=========================================="
echo ""
echo "📡 网络信息："
echo "   内网 IP:  $LOCAL_IP"
echo "   公网 IP:  $PUBLIC_IP"
echo ""
echo "🔌 端口说明："
echo "   3000  - Token 网关（本地服务）"
echo "   7100  - frps 监听端口（frpc 连接用，仅本机）"
echo "   7001  - FRP 代理端口（外部访问 Token 网关）⭐"
echo "   7500  - FRP Dashboard 管理面板"
echo ""
echo "🌐 访问方式："
echo "   本机访问:    http://localhost:7001/admin"
echo "   局域网访问:  http://$LOCAL_IP:7001/admin"
echo "   公网访问:    http://$PUBLIC_IP:7001/admin  (需路由器端口转发)"
echo ""
echo "🔐 FRP Dashboard："
echo "   地址: http://localhost:7500"
echo "   账号: admin"
echo "   密码: frp-admin-2024"
echo ""
echo "⚠️  路由器端口转发配置（必须）："
echo "   登录路由器后台（通常 192.168.1.1 或 192.168.3.1）"
echo "   添加端口转发规则："
echo "     外部端口 7001 -> 内部 IP $LOCAL_IP -> 内部端口 7001 (TCP)"
echo "     外部端口 7500 -> 内部 IP $LOCAL_IP -> 内部端口 7500 (TCP) [可选, Dashboard]"
echo ""
echo "🛠️  常用命令："
echo "   查看 frps 日志:  tail -f $FRP_DIR/logs/frps.log"
echo "   查看 frpc 日志:  tail -f $FRP_DIR/logs/frpc.log"
echo "   重启 frps:       launchctl unload $FRPS_PLIST_DST && launchctl load $FRPS_PLIST_DST"
echo "   重启 frpc:       launchctl unload $FRPC_PLIST_DST && launchctl load $FRPC_PLIST_DST"
echo "   停止 FRP:        launchctl unload $FRPS_PLIST_DST; launchctl unload $FRPC_PLIST_DST"
echo ""
