#!/usr/bin/env python3
"""
Tailscale 客户端分发服务
1. 在本机起一个 HTTP 服务，托管 Tailscale 各平台客户端安装包
2. 提供 HTML 下载页面，便于其他设备从局域网下载
3. 同时提供 Token 网关的连接信息
"""

import http.server
import socketserver
import os
import json
import socket
import time
from pathlib import Path
from typing import Optional

# ========== 配置 ==========
CLIENTS_DIR = Path.home() / ".ollama-token-gateway" / "clients"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("DIST_PORT", "8088"))

# Token 网关信息（用于在页面显示）
TOKEN_GATEWAY_PORT = 3000
# ==========================


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_tailscale_ip() -> Optional[str]:
    """获取本机 Tailscale IP（100.x.x.x）"""
    try:
        result = os.popen("ifconfig utun8 2>/dev/null || ifconfig 2>/dev/null | grep 'inet 100'").read()
        for line in result.split("\n"):
            if "inet 100" in line:
                parts = line.strip().split()
                if len(parts) >= 2:
                    return parts[1]
    except Exception:
        pass
    return None


def list_clients() -> list:
    """列出所有可下载的客户端文件"""
    if not CLIENTS_DIR.exists():
        return []
    clients = []
    file_map = {
        "Tailscale.pkg": {
            "platform": "macOS",
            "desc": "macOS 安装包（pkg 格式）",
            "icon": "🍎",
            "install": "双击 pkg 文件按提示安装",
        },
        "Tailscale-Windows.exe": {
            "platform": "Windows",
            "desc": "Windows 安装程序（exe 格式）",
            "icon": "🪟",
            "install": "双击 exe 文件按提示安装",
        },
        "Tailscale-Android.apk": {
            "platform": "Android",
            "desc": "Android 安装包（apk 格式）",
            "icon": "🤖",
            "install": "下载后允许安装未知来源应用",
        },
    }
    for f in sorted(CLIENTS_DIR.iterdir()):
        if not f.is_file():
            continue
        if f.stat().st_size < 1000:
            continue  # 跳过失败的下载
        info = file_map.get(f.name, {
            "platform": f.stem,
            "desc": f.name,
            "icon": "📄",
            "install": "下载后按系统提示安装",
        })
        clients.append({
            "filename": f.name,
            "size_mb": round(f.stat().st_size / 1024 / 1024, 2),
            "size_bytes": f.stat().st_size,
            "platform": info["platform"],
            "desc": info["desc"],
            "icon": info["icon"],
            "install": info["install"],
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
        })
    return clients


def generate_html() -> str:
    lan_ip = get_lan_ip()
    ts_ip = get_tailscale_ip()
    clients = list_clients()

    clients_html = ""
    for c in clients:
        clients_html += f"""
        <div class="card">
            <div class="card-header">
                <span class="icon">{c['icon']}</span>
                <div>
                    <div class="title">{c['platform']}</div>
                    <div class="desc">{c['desc']}</div>
                </div>
            </div>
            <div class="card-meta">
                <span>{c['size_mb']} MB</span>
                <span>{c['mtime']}</span>
            </div>
            <a href="/download/{c['filename']}" class="btn" download>⬇️ 下载</a>
            <div class="install-hint">📋 {c['install']}</div>
        </div>"""

    ts_section = ""
    if ts_ip:
        ts_section = f"""
        <div class="info-box success">
            <h3>🌐 Tailscale 已就绪</h3>
            <p>本机 Tailscale IP: <code>{ts_ip}</code></p>
            <p>其他设备装好 Tailscale 并登录同一账号后，访问：</p>
            <ul>
                <li>管理面板: <code>http://{ts_ip}:{TOKEN_GATEWAY_PORT}/admin</code></li>
                <li>API 地址: <code>http://{ts_ip}:{TOKEN_GATEWAY_PORT}/v1</code></li>
            </ul>
        </div>"""
    else:
        ts_section = """
        <div class="info-box warning">
            <h3>⚠️ Tailscale 未启动</h3>
            <p>本机尚未启动 Tailscale。请先在本机：</p>
            <ol>
                <li>双击下载的 Tailscale.pkg 安装</li>
                <li>打开 /Applications/Tailscale.app</li>
                <li>登录账号（Google/Microsoft/GitHub/Apple 都可以）</li>
            </ol>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ollama Token Gateway - 客户端分发中心</title>
<style>
:root{{--bg:#0b0e14;--surface:#141824;--surface2:#1c2030;--border:#262b3d;--text:#e2e8f0;--dim:#64748b;--accent:#6366f1;--accent2:#8b5cf6;--green:#22c55e;--yellow:#eab308;--red:#ef4444}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:20px}}
.container{{max-width:900px;margin:0 auto}}
header{{text-align:center;margin-bottom:32px;padding:24px 0}}
header h1{{font-size:28px;margin-bottom:8px;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
header p{{color:var(--dim);font-size:14px}}
.info-box{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px}}
.info-box.success{{border-color:var(--green)}}
.info-box.warning{{border-color:var(--yellow)}}
.info-box h3{{font-size:15px;margin-bottom:12px}}
.info-box p{{font-size:13px;color:var(--dim);line-height:1.8;margin-bottom:8px}}
.info-box ul,.info-box ol{{margin-left:20px;font-size:13px;color:var(--dim);line-height:1.8}}
.info-box code{{background:var(--surface2);padding:2px 8px;border-radius:4px;color:var(--accent);font-family:'SF Mono',monospace;font-size:12px;word-break:break-all}}
section{{margin-bottom:32px}}
section h2{{font-size:18px;margin-bottom:16px;display:flex;align-items:center;gap:8px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;display:flex;flex-direction:column;gap:12px}}
.card-header{{display:flex;align-items:center;gap:12px}}
.card-header .icon{{font-size:32px}}
.card-header .title{{font-size:16px;font-weight:600}}
.card-header .desc{{font-size:12px;color:var(--dim)}}
.card-meta{{display:flex;justify-content:space-between;font-size:11px;color:var(--dim)}}
.btn{{display:block;text-align:center;background:var(--accent);color:#fff;padding:10px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;transition:background .2s}}
.btn:hover{{background:var(--accent2)}}
.install-hint{{font-size:11px;color:var(--dim);text-align:center}}
.guide{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px}}
.guide h3{{font-size:15px;margin-bottom:16px}}
.guide ol{{margin-left:20px;font-size:13px;line-height:2;color:var(--text)}}
.guide ol code{{background:var(--surface2);padding:2px 8px;border-radius:4px;color:var(--accent);font-family:'SF Mono',monospace;font-size:12px}}
footer{{text-align:center;color:var(--dim);font-size:12px;padding:20px 0;border-top:1px solid var(--border);margin-top:32px}}
.app-stores{{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:12px}}
.app-stores a{{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 16px;border-radius:8px;font-size:12px;text-decoration:none;transition:all .2s}}
.app-stores a:hover{{border-color:var(--accent)}}
</style>
</head>
<body>
<div class="container">
<header>
<h1>🚀 Ollama Token Gateway</h1>
<p>客户端分发中心 · 下载 Tailscale 加入虚拟内网</p>
</header>

{ts_section}

<div class="info-box">
<h3>📡 连接信息</h3>
<p>局域网 IP: <code>{lan_ip}</code></p>
<p>Token 网关端口: <code>{TOKEN_GATEWAY_PORT}</code> (OpenAI 兼容 API + 管理 UI)</p>
<p>本下载服务端口: <code>{LISTEN_PORT}</code></p>
</div>

<section>
<h2>📦 各平台客户端下载</h2>
<div class="cards">
{clients_html if clients else '<p style="color:var(--dim);padding:20px;">暂无可下载的客户端文件</p>'}
</div>
<div class="app-stores">
<a href="https://apps.apple.com/ca/app/tailscale/id1475387142" target="_blank">🍎 macOS App Store</a>
<a href="https://apps.apple.com/us/app/tailscale/id1475387142" target="_blank">📱 iOS App Store</a>
<a href="https://play.google.com/store/apps/details?id=com.tailscale.ipn" target="_blank">🤖 Google Play</a>
<a href="https://tailscale.com/download/linux" target="_blank">🐧 Linux 安装指南</a>
</div>
</section>

<section>
<h2>📖 使用步骤</h2>
<div class="guide">
<h3>1️⃣ 在本机（服务器）安装并启动 Tailscale</h3>
<ol>
<li>双击下载的 <code>Tailscale.pkg</code> 安装</li>
<li>打开 <code>/Applications/Tailscale.app</code></li>
<li>点击菜单栏 Tailscale 图标 → Log in</li>
<li>使用 Google/Microsoft/GitHub/Apple 账号登录（首次登录会自动注册）</li>
<li>登录后获得一个 <code>100.x.x.x</code> 的虚拟 IP</li>
</ol>

<h3 style="margin-top:20px;">2️⃣ 在其他设备安装 Tailscale</h3>
<ol>
<li>从上方下载对应平台的客户端（或从 App Store / Play Store 安装）</li>
<li>启动 Tailscale，<strong>登录与第 1 步相同的账号</strong></li>
<li>在 <a href="https://login.tailscale.com/admin/machines" target="_blank">Tailscale 管理后台</a> 确认设备已加入</li>
</ol>

<h3 style="margin-top:20px;">3️⃣ 从其他设备访问 Token 服务</h3>
<ol>
<li>获取服务器 Tailscale IP（在本机执行 <code>tailscale ip -4</code> 查看）</li>
<li>在客户端设备访问：<code>http://[服务器Tailscale-IP]:{TOKEN_GATEWAY_PORT}/admin</code></li>
<li>API 调用：<code>http://[服务器Tailscale-IP]:{TOKEN_GATEWAY_PORT}/v1</code></li>
</ol>
</div>
</section>

<footer>
<p>🌐 局域网访问: http://{lan_ip}:{LISTEN_PORT}</p>
<p>本机访问: http://localhost:{LISTEN_PORT}</p>
<p>Powered by Ollama Token Gateway · {time.strftime('%Y-%m-%d %H:%M')}</p>
</footer>
</div>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 简化日志（兼容 Python 3.14 BaseHTTPRequestHandler 的多参数调用）
        try:
            msg = format % args
        except Exception:
            msg = format
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} - {msg}")

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = generate_html()
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/download/"):
            filename = self.path[10:]
            # 防路径穿越
            filename = os.path.basename(filename)
            filepath = CLIENTS_DIR / filename
            if not filepath.exists() or filepath.stat().st_size < 1000:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"File not found")
                return
            try:
                size = filepath.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(filepath, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Download error: {e}".encode())
        elif self.path == "/api/clients":
            clients = list_clients()
            data = json.dumps({"clients": clients}, ensure_ascii=False, indent=2)
            body = data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not found")


def main():
    # 使用 IPv4 + IPv6 双栈
    class DualStackServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        address_family = socket.AF_INET6
        daemon_threads = True
        allow_reuse_address = True

        def server_bind(self):
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (AttributeError, socket.error):
                pass
            super().server_bind()

    try:
        server = DualStackServer(("::", LISTEN_PORT), Handler)
    except OSError:
        server = socketserver.ThreadingMixIn.http.server.HTTPServer(
            ("0.0.0.0", LISTEN_PORT), Handler
        )

    lan_ip = get_lan_ip()
    ts_ip = get_tailscale_ip()

    print("=" * 60)
    print("  Tailscale 客户端分发服务")
    print("=" * 60)
    print(f"\n📡 本机访问:    http://localhost:{LISTEN_PORT}")
    print(f"🌐 局域网访问:  http://{lan_ip}:{LISTEN_PORT}")
    if ts_ip:
        print(f"🔗 Tailscale:   http://{ts_ip}:{LISTEN_PORT}")
    print(f"📁 客户端目录:  {CLIENTS_DIR}")
    print(f"\n📋 可下载文件:")
    clients = list_clients()
    if not clients:
        print("   ⚠️ 暂无客户端文件")
    else:
        for c in clients:
            print(f"   {c['icon']} {c['platform']}: {c['filename']} ({c['size_mb']} MB)")
    print(f"\n🚀 服务已启动，按 Ctrl+C 停止\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 停止服务")
        server.shutdown()


if __name__ == "__main__":
    main()
