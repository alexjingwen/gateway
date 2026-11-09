#!/bin/bash
# Open WebUI 初始化配置脚本
# 在 Open WebUI 容器启动后运行，配置 OpenAI 兼容端点
set -e

OPEN_WEBUI_URL="http://localhost:8081"
API_KEY="sk-ollama-REPLACE_WITH_YOUR_KEY"

echo "=========================================="
echo "  Open WebUI 配置脚本"
echo "=========================================="
echo ""

echo "[1/3] 检查 Open WebUI 是否在线..."
if ! curl -s -o /dev/null -w "%{http_code}" "$OPEN_WEBUI_URL/health" | grep -q "200"; then
    echo "   ❌ Open WebUI 未就绪，请等待容器启动后重试"
    echo "   检查命令: docker logs open-webui"
    exit 1
fi
echo "   ✅ Open WebUI 已在线"

echo ""
echo "[2/3] 检查 Ollama 连接..."
if curl -s --max-time 5 http://host.docker.internal:11434/api/tags > /dev/null 2>&1 || \
   curl -s --max-time 5 http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "   ✅ Ollama 可达"
else
    echo "   ⚠️  Ollama 可能不可达（容器内需通过 host.docker.internal 访问）"
fi

echo ""
echo "[3/3] 配置说明"
echo ""
echo "   Open WebUI 已配置以下环境变量："
echo "   - OLLAMA_BASE_URL=http://host.docker.internal:11434"
echo "   - WEBUI_AUTH=true（需要注册登录）"
echo ""
echo "   首次使用步骤："
echo "   1. 打开浏览器访问: http://localhost:8081"
echo "   2. 注册管理员账号（第一个注册的用户自动成为管理员）"
echo "   3. 进入 设置 → 连接 → 检查 Ollama 连接"
echo "   4. 选择模型 gemma4:latest 或 gemma4:26b 开始聊天"
echo ""
echo "   如需添加 OpenAI 兼容端点（带 API Key 的代理层）："
echo "   1. 进入 设置 → 连接 → OpenAI API"
echo "   2. Base URL: http://host.docker.internal:3000/v1"
echo "   3. API Key:  ${API_KEY}"
echo "   4. 保存后即可在模型选择中使用代理端点"
echo ""
echo "=========================================="
