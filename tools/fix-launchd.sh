#!/bin/bash
# 诊断并修复「launchctl 加作业报 5: Input/output error」
#
#   这台机器 2026-11-09 实测：print / bootout / enable / disable 全正常，
#   只有 bootstrap 与 legacy load 恒定 EIO（任何 plist、任何路径、任何调用方）。
#   **结论：加 sudo 就成功**——根因是当前执行上下文对 GUI 域没有「新增作业」权限，
#   launchd 把权限失败泛化成了 EIO。本脚本把判据固化成一条命令，避免重新摸索。
#
# 用法：
#   bash tools/fix-launchd.sh                  # 诊断 + 无 sudo 尝试
#   sudo bash tools/fix-launchd.sh --apply     # 用 root 重试（已验证这是有效解法）
#   bash tools/fix-launchd.sh --verify         # 只看网关是否在跑，不动 launchd
#
# 退出码：0 已加载/修复；2 诊断完成但 EIO 仍在（需 sudo / reboot userspace）；3 环境检查失败
set -u

UID_N=$(id -u)
LABEL="com.token-gateway.server"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
GW_PORT="${TG_PORT:-3000}"
MODE="${1:-}"

say() { printf '%s\n' "$*"; }
hr()  { printf '%.0s-' {1..64}; echo; }

probe_health() {
  curl -s --noproxy '*' -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${GW_PORT}/health" 2>/dev/null
}

show_state() {
  if launchctl print "gui/${UID_N}/${LABEL}" >/dev/null 2>&1; then
    say "[✓] 作业已在 gui/${UID_N} 中注册"
    launchctl print "gui/${UID_N}/${LABEL}" 2>/dev/null | sed -n '1,10p' | sed 's/^/    /'
    return 0
  fi
  say "[✗] 作业未注册（launchctl print 查不到 ${LABEL}）"
  return 1
}

if [ "$MODE" = "--verify" ]; then
  hr; show_state || true
  say "    /health → $(probe_health)（200 = 网关在跑）"
  exit 0
fi

hr; say "launchd 加作业诊断    uid=${UID_N}  plist=${PLIST}"; hr

# ── 0) 前置：plist 必须存在且语法正确 ──
if [ ! -f "$PLIST" ]; then
  say "[x] 缺少 $PLIST —— 先 cp com.token-gateway.server.plist 过去并改路径"
  exit 3
fi
if ! plutil -lint "$PLIST" >/dev/null 2>&1; then
  say "[x] plist 语法错误："; plutil -lint "$PLIST"; exit 3
fi
say "[✓] plist 语法 OK（$(stat -f '%Sp %Su:%Sg %zB' "$PLIST")）"

# ── 1) 已在跑就直接收工 ──
if show_state; then
  code=$(probe_health)
  say "    /health → ${code}"
  [ "$code" = "200" ] && { say "[=] 已在运行，无需修复"; exit 0; }
  say "[!] 已注册但 /health 不是 200，看 /tmp/token-gateway.err.log"
  exit 2
fi

# ── 2) 加载前必须先清场：手动实例占着端口会让新作业无限「Address already in use」 ──
#    实测症状：bootstrap 成功、print 显示 running，但 runs 一路涨、:3000 仍是旧进程，
#    /tmp/token-gateway.log 里刷 "❌ 端口 3000 被占用"。这种"看起来成功"最坑。
if [ "$(probe_health)" = "200" ]; then
  say ""
  say "[!] :${GW_PORT} 上已有实例在响应，但 launchd 里没有这个作业"
  say "    → 这是手动/脚本拉起的实例（tools/start-gateway.py）。"
  say "    launchd 接管前必须先停掉它，否则新作业会一直 Address already in use 重试。"
  pgrep -f 'token-gateway\.py' 2>/dev/null | sed 's/^/      候选 pid: /'
  say "    交接命令： pkill -f 'token-gateway\.py'; sleep 2; launchctl kickstart -k gui/${UID_N}/${LABEL}"
fi

# ── 3) 尝试加载 ──
say ""; say "→ launchctl bootstrap gui/${UID_N} …"
out=$(launchctl bootstrap "gui/${UID_N}" "$PLIST" 2>&1); rc=$?
say "    rc=${rc}  ${out:-（无输出）}"

if [ $rc -eq 0 ] && show_state; then
  launchctl kickstart -k "gui/${UID_N}/${LABEL}" >/dev/null 2>&1
  say "[✓] 已修复，/health → $(probe_health)"; exit 0
fi

# ── 4) 失败：跑判据，确认是「域级」故障而不是这份 plist 的问题 ──
say ""; hr; say "失败判据（全部通过 = 问题在 launchd 域，不在本项目）"; hr

printf '%s' "[1] 最小 plist（/bin/sleep 120）…… "
TMPP="$(mktemp -d)/mini.plist"
cat > "$TMPP" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.claw.launchprobe</string>
<key>ProgramArguments</key><array><string>/bin/sleep</string><string>120</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
PL
mini_out=$(launchctl bootstrap "gui/${UID_N}" "$TMPP" 2>&1)
mini_rc=$?
launchctl bootout "gui/${UID_N}/com.claw.launchprobe" >/dev/null 2>&1
rm -rf "$(dirname "$TMPP")"
if [ $mini_rc -eq 0 ]; then say "OK（说明是你这份 plist 的问题）"; else say "同样失败 rc=${mini_rc} → 域级故障"; fi

say "[2] 域是否可读：$(launchctl print "gui/${UID_N}" 2>/dev/null | awk -F'= ' '/service count/{print $2; exit}') 个服务"
say "[3] 域是否可写：enable/disable rc=$(launchctl enable "gui/${UID_N}/${LABEL}" >/dev/null 2>&1; echo $?)（0=可写）"
say "[4] 状态文件语法：$(plutil -lint /var/db/com.apple.xpc.launchd/disabled."${UID_N}".plist 2>&1 | awk '{print $NF}')"
say "[5] 磁盘可用：$(df -h / | awk 'NR==2{print $4}')"
say "[6] 该 label 的禁用态：$(launchctl print-disabled "gui/${UID_N}" 2>/dev/null | grep "${LABEL}" | head -1)"

# ── 5) root 重试 ──
if [ "$MODE" = "--apply" ] && [ "$UID_N" = "0" ]; then
  say ""; hr; say "root 重试"; hr
  out=$(launchctl bootstrap "gui/$(stat -f %u /dev/console)" "$PLIST" 2>&1); rc=$?
  say "    rc=${rc}  ${out:-（无输出）}"
  if [ $rc -eq 0 ]; then
    sleep 2
    say "[✓] root 下加载成功，/health → $(probe_health)"
    say "    （若 runs 一直涨且日志是 Address already in use，见上面第 2 步的清场提示）"
    exit 0
  fi
fi

say ""; hr; say "处置（按代价排序）"; hr
cat <<EOF
① 加 sudo 重试——**本机 2026-11-09 用这条修好了**：
     sudo launchctl bootstrap gui/${UID_N} ~/Library/LaunchAgents/${LABEL}.plist
   根因是当前执行上下文对 GUI 域没有「新增作业」权限，launchd 把它报成 EIO。
   成功后先 pkill 手动实例再 kickstart，否则会抢 :${GW_PORT}。
② 只重启用户态、不重启内核（约 10s，GUI 应用会退出重登）：
     sudo launchctl reboot userspace
③ 常规：注销后重新登录 / 重启

等不及也能跑：网关可以由脚本强制拉起
     python3 tools/start-gateway.py
EOF
exit 2
