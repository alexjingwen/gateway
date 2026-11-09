#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_SCRIPT="$PROJECT_DIR/ollama-openai-proxy.py"
PROXY_PLIST_SRC="$PROJECT_DIR/com.ollama.proxy.plist"
PROXY_PLIST_DST="$HOME/Library/LaunchAgents/com.ollama.proxy.plist"
API_KEY="sk-ollama-REPLACE_WITH_YOUR_KEY"

echo "=========================================="
echo "  Ollama API Key 代理层 - 安装脚本"
echo "=========================================="
echo ""

if [ "$EUID" -eq 0 ]; then
    echo "❌ 请不要使用 sudo/root 运行此脚本"
    exit 1
fi

echo "[1/4] 替换 plist 中的路径占位符..."
# 用 sed 替换 REPLACE_WITH_PROJECT_PATH
sed "s|REPLACE_WITH_PROJECT_PATH|$PROJECT_DIR|g" "$PROXY_PLIST_SRC" > "$PROXY_PLIST_SRC.tmp"
mv "$PROXY_PLIST_SRC.tmp" "$PROXY_PLIST_SRC"
echo "   ✅ 完成"

echo ""
echo "[2/4] 停止旧代理（如果存在）..."
launchctl unload "$PROXY_PLIST_DST" 2>/dev/null || true
pkill -f "ollama-openai-proxy.py" 2>/dev/null || true
sleep 1
echo "   ✅ 完成"

echo ""
echo "[3/4] 安装 launchd plist 并加载..."
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PROXY_PLIST_SRC" "$PROXY_PLIST_DST"
chmod 644 "$PROXY_PLIST_DST"
chmod +x "$PROXY_SCRIPT"
launchctl load "$PROXY_PLIST_DST"
sleep 2
echo "   ✅ 已加载 $PROXY_PLIST_DST"

echo ""
echo "[4/4] 验证代理服务..."
sleep 2

# 测试 401
echo -n "   不带 Key 访问 (预期401): "
CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/v1/models)
if [ "$CODE" = "401" ]; then
    echo "✅ $CODE 鉴权生效"
else
    echo "⚠️  $CODE (非401，可能还在启动中)"
fi

# 测试 200
echo -n "   带正确 Key 访问 (预期200): "
CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $API_KEY" \
    http://localhost:3000/v1/models)
if [ "$CODE" = "200" ]; then
    echo "✅ $CODE 正常响应"
else
    echo "⚠️  $CODE (请检查日志 /tmp/ollama-proxy.log)"
fi

LOCAL_IP=$(ifconfig | grep "inet " | grep -v 127.0.0.1 | head -1 | awk '{print $2}')

echo ""
echo "=========================================="
echo "  安装完成！代理层配置信息："
echo "=========================================="
echo ""
echo "🔐 代理层（带 API Key 鉴权，推荐外部应用使用）："
echo "   Base URL:  http://localhost:3000/v1"
echo "   局域网:    http://${LOCAL_IP}:3000/v1"
echo "   API Key:   ${API_KEY}"
echo ""
echo "🔓 直连 Ollama（无鉴权，仅限本机可信程序使用）："
echo "   Base URL:  http://localhost:11434/v1"
echo "   局域网:    http://${LOCAL_IP}:11434/v1"
echo ""
echo "📦 可用模型："
echo "   - gemma4:latest  (8B, Q4_K_M)"
echo "   - gemma4:26b     (25.8B, Q4_K_M)"
echo ""
echo "🧪 测试代理层（带鉴权）："
cat <<'EOF'
   curl -s http://localhost:3000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer sk-ollama-REPLACE_WITH_YOUR_KEY" \
     -d '{"model":"gemma4:latest","messages":[{"role":"user","content":"你好"}],\
"max_tokens":100,"stream":false}'
EOF
echo ""
echo "🛠️  常用命令："
echo "   代理日志:  tail -f /tmp/ollama-proxy.log"
echo "   重启代理:  launchctl unload ~/Library/LaunchAgents/com.ollama.proxy.plist && launchctl load ~/Library/LaunchAgents/com.ollama.proxy.plist"
echo "   停止代理:  launchctl unload ~/Library/LaunchAgents/com.ollama.proxy.plist"
echo ""
