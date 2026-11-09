#!/bin/bash
# Ollama Token Gateway - 自动维护脚本
# 功能：健康检查、自动重启、日志轮转、用量备份
# 用法：手动执行，或由 launchd/cron 定期调用

set -euo pipefail

GATEWAY_DIR="$HOME/.ollama-token-gateway"
LOG_DIR="$GATEWAY_DIR/logs"
USAGE_LOG="$LOG_DIR/usage.jsonl"
BACKUP_DIR="$LOG_DIR/backup"
GW_HOST="127.0.0.1"
GW_PORT="3000"
LAUNCHD_LABEL="com.token-gateway.server"
MAX_LOG_MB=100

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log_msg() { echo "[$(ts)] $1"; }

mkdir -p "$BACKUP_DIR"

echo ""
echo "=========================================================="
echo " Ollama Token Gateway 自动维护脚本"
echo " 运行时间: $(ts)"
echo "=========================================================="
echo ""

# ===== 1. 健康检查 =====
echo "【1/5】健康检查..."
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://${GW_HOST}:${GW_PORT}/health" 2>/dev/null || echo "000")
if [ "$HEALTH" = "200" ]; then
    log_msg "✅ 网关服务正常 (HTTP 200)"
else
    log_msg "⚠️  网关服务异常 (HTTP $HEALTH)，尝试重启..."
    # 优先走 start-gateway.py：它从 plist 读环境变量，且不依赖 launchd。
    # 本机 launchctl bootstrap/load 一律 EIO，所以 launchctl 那一路只是兜底。
    if ! /opt/homebrew/bin/python3 "$GATEWAY_DIR/tools/start-gateway.py" >>"$LOG_DIR/maintain-restart.log" 2>&1; then
        launchctl unload "$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist" 2>/dev/null || true
        sleep 1
        launchctl load "$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist" 2>/dev/null || true
        sleep 4
    fi
    HEALTH2=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://${GW_HOST}:${GW_PORT}/health" 2>/dev/null || echo "000")
    if [ "$HEALTH2" = "200" ]; then
        log_msg "✅ 重启后网关已恢复"
    else
        log_msg "❌ 重启后仍不可用，请检查 launchctl 日志"
    fi
fi

# ===== 2. 检查 Ollama 上游 =====
echo ""
echo "【2/5】Ollama 上游检查..."
OLLAMA_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:11434/api/tags" 2>/dev/null || echo "000")
if [ "$OLLAMA_HEALTH" = "200" ]; then
    MODEL_COUNT=$(curl -s "http://127.0.0.1:11434/v1/models" --max-time 5 | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null || echo "?")
    log_msg "✅ Ollama 上游正常 (${MODEL_COUNT} models)"
else
    log_msg "⚠️  Ollama 不可用 (HTTP $OLLAMA_HEALTH)，网关将返回 502 给调用方"
fi

# ===== 3. 日志轮转 =====
echo ""
echo "【3/5】日志轮转..."
if [ -f "$USAGE_LOG" ]; then
    SIZE_KB=$(du -sk "$USAGE_LOG" 2>/dev/null | awk '{print $1}' || echo 0)
    SIZE_MB=$((SIZE_KB / 1024))
    if [ "$SIZE_MB" -ge "$MAX_LOG_MB" ]; then
        TS=$(date '+%Y%m%d-%H%M%S')
        BACKUP_FILE="$BACKUP_DIR/usage-${TS}.jsonl.bz2"
        log_msg "📦 用量日志 ${SIZE_MB}MB ≥ ${MAX_LOG_MB}MB，压缩备份..."
        bzip2 -c "$USAGE_LOG" > "$BACKUP_FILE"
        > "$USAGE_LOG"  # 清空当前日志
        BACKUP_SIZE=$(du -sh "$BACKUP_FILE" 2>/dev/null | awk '{print $1}')
        log_msg "✅ 已备份: $BACKUP_FILE ($BACKUP_SIZE)"
    else
        log_msg "📊 用量日志 ${SIZE_MB}MB / ${MAX_LOG_MB}MB，无需轮转"
    fi
else
    log_msg "ℹ️ 用量日志不存在，跳过轮转"
fi

# ===== 4. 用量报告 =====
echo ""
echo "【4/5】用量日报汇总..."
if [ -f "$USAGE_LOG" ]; then
    TODAY=$(date '+%Y-%m-%d')
    REPORT=$(python3 << PYEOF
import json
from pathlib import Path
from collections import defaultdict
f = Path("$USAGE_LOG")
today = "$TODAY"
daily = defaultdict(lambda: {"total":0,"prompt":0,"completion":0,"reqs":0})
per_key = defaultdict(lambda: defaultdict(int))
if f.exists():
    for line in f.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("date") == today:
                k = r.get("api_key","unknown")
                daily[k]["total"] += r.get("total_tokens",0)
                daily[k]["prompt"] += r.get("prompt_tokens",0)
                daily[k]["completion"] += r.get("completion_tokens",0)
                daily[k]["reqs"] += 1
        except: pass
    for k,v in daily.items():
        print(f"  Key {k}: {v['reqs']} 次请求, {v['total']:,} tokens (P:{v['prompt']} + C:{v['completion']})")
if not daily:
    print("  今日暂无调用记录")
PYEOF
)
    echo "📅 今日 ($TODAY):"
    echo "$REPORT"
fi

# ===== 5. 清理 7 天前的备份 =====
echo ""
echo "【5/5】清理旧备份..."
CLEANED_COUNT=$(find "$BACKUP_DIR" -name "*.jsonl.bz2" -mtime +7 -type f -print -delete 2>/dev/null | wc -l | tr -d ' ')
if [ "$CLEANED_COUNT" -gt 0 ]; then
    log_msg "🧹 已清理 ${CLEANED_COUNT} 个 7 天前的备份"
else
    log_msg "ℹ️ 无需清理（7 天内无过期备份）"
fi

echo ""
echo "=========================================================="
echo " 维护完成 @ $(ts)"
echo "=========================================================="
echo ""
