#!/bin/bash
set -e

PLIST_NAME="com.ollama.custom.plist"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "=========================================="
echo "  Ollama OpenAI Token Server - 安装脚本"
echo "=========================================="
echo ""

# 检查是否以 root 运行
if [ "$EUID" -eq 0 ]; then
    echo "❌ 请不要使用 sudo/root 运行此脚本，请使用当前用户运行"
    exit 1
fi

echo "[1/5] 检查 Ollama App 是否安装..."
if [ ! -f "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
    echo "❌ 未找到 Ollama App，请先安装 https://ollama.com"
    exit 1
fi
echo "   ✅ Ollama 已安装"

echo ""
echo "[2/5] 停止现有的 Ollama App 服务..."
osascript -e 'quit app "Ollama"' 2>/dev/null || true
sleep 2
# 杀掉残留进程
pkill -f "/Applications/Ollama.app/Contents/Resources/ollama serve" 2>/dev/null || true
pkill -f "ollama serve" 2>/dev/null || true
launchctl bootout gui/$UID/com.ollama.ollama 2>/dev/null || true
launchctl unload "$PLIST_DST" 2>/dev/null || true
sleep 1
echo "   ✅ 已停止旧服务"

echo ""
echo "[3/5] 复制 launchd plist 文件..."
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"
chmod 644 "$PLIST_DST"
echo "   ✅ 已复制到 $PLIST_DST"

echo ""
echo "[4/5] 加载并启动服务..."
launchctl load "$PLIST_DST"
sleep 3
echo "   ✅ 服务已加载"

echo ""
echo "[5/5] 验证服务状态..."
sleep 2
if curl -s -H "Authorization: Bearer sk-ollama-REPLACE_WITH_YOUR_KEY" \
    http://localhost:11434/v1/models > /dev/null 2>&1; then
    echo "   ✅ 服务运行正常！"
else
    echo "   ⚠️  服务可能还在启动中，请稍后手动验证..."
fi

echo ""
echo "=========================================="
echo "  安装完成！配置信息："
echo "=========================================="
echo ""
echo "📡 API 端点（OpenAI 兼容）："
echo "   本地:   http://localhost:11434/v1"
echo "   局域网: http://$(ifconfig | grep "inet " | grep -v 127.0.0.1 | head -1 | awk '{print $2}'):11434/v1"
echo ""
echo "🔑 API Key："
echo "   sk-ollama-REPLACE_WITH_YOUR_KEY"
echo ""
echo "📦 可用模型："
echo "   - gemma4:latest  (8B)"
echo "   - gemma4:26b     (25.8B)"
echo ""
echo "🛠️  常用命令："
echo "   查看状态: launchctl list | grep com.ollama.custom"
echo "   查看日志: tail -f /tmp/ollama-custom.log"
echo "   重启服务: launchctl unload ~/Library/LaunchAgents/com.ollama.custom.plist && launchctl load ~/Library/LaunchAgents/com.ollama.custom.plist"
echo "   停止服务: launchctl unload ~/Library/LaunchAgents/com.ollama.custom.plist"
echo ""
echo "🧪 测试命令（复制运行）："
echo '   curl -s http://localhost:11434/v1/chat/completions \'
echo '     -H "Content-Type: application/json" \'
echo '     -H "Authorization: Bearer sk-ollama-REPLACE_WITH_YOUR_KEY" \'
echo '     -d "{\"model\":\"gemma4:latest\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}],\"max_tokens\":50}"'
echo ""
