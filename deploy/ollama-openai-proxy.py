#!/usr/bin/env python3
"""
Ollama OpenAI 兼容 API 反向代理
功能：
  - 在 Ollama (11434) 前加一层 API Key 鉴权
  - 兼容 OpenAI SDK / cURL / 任何 OpenAI 格式客户端
  - 支持 Token 计数、流式响应
  - 轻量级，仅依赖 Python 标准库
"""

import http.server
import socketserver
import urllib.request
import urllib.error
import json
import os
import sys
import threading
import time
from typing import Optional

# ========== 配置 ==========
# 从环境变量或硬编码读取
API_KEY = os.environ.get("OLLAMA_PROXY_API_KEY", "sk-ollama-REPLACE_WITH_YOUR_KEY")
OLLAMA_HOST = os.environ.get("OLLAMA_PROXY_UPSTREAM", "http://127.0.0.1:11434")
LISTEN_HOST = os.environ.get("OLLAMA_PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("OLLAMA_PROXY_PORT", "3000"))
DEBUG = os.environ.get("OLLAMA_PROXY_DEBUG", "0") == "1"
# ==========================


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """反向代理请求处理器"""

    # 禁用默认的 stderr 日志
    def log_message(self, format, *args):
        if DEBUG:
            log(f"DEBUG: {format % args}")

    def _check_auth(self) -> bool:
        """校验 Authorization header"""
        auth_header = self.headers.get("Authorization", "")
        # 兼容两种格式：Bearer xxx 或 直接 xxx
        key = auth_header
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            # OpenAI 有些客户端放在 query 里，不过标准是 header
            return False
        return key == API_KEY

    def _send_json_error(self, status: int, code: str, message: str):
        body = json.dumps({
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": code,
            }
        }).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """CORS 预检"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _forward(self, method: str):
        # 1. 鉴权
        if not self._check_auth():
            log(f"❌ 401 未授权: {self.client_address[0]} {method} {self.path}")
            self._send_json_error(401, "invalid_api_key",
                "Incorrect API key provided. You can find your API key at the proxy configuration.")
            return

        # 2. 读取请求体
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # 3. 构造上游请求
        upstream_url = f"{OLLAMA_HOST}{self.path}"
        upstream_headers = {}
        # 透传必要的 header，去掉鉴权（Ollama 本端不需要）
        for h in ("Content-Type", "Accept", "Accept-Encoding"):
            v = self.headers.get(h)
            if v:
                upstream_headers[h] = v
        upstream_headers["X-Forwarded-For"] = self.client_address[0]

        req = urllib.request.Request(upstream_url, data=body, method=method, headers=upstream_headers)

        log(f"➡️  {method} {self.path} -> {upstream_url}  ({len(body)}B)  from {self.client_address[0]}")

        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                # 透传状态码
                self.send_response(resp.status)
                # 透传响应头（过滤掉 Content-Length / Transfer-Encoding 让 http.server 自己处理）
                for k, v in resp.headers.items():
                    kl = k.lower()
                    if kl in ("content-length", "transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                # 确保有 CORS
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
                self.end_headers()

                # 流式转发（支持 SSE streaming）
                total = 0
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    total += len(chunk)
                log(f"✅ 200 完成: 响应 {total}B")

        except urllib.error.HTTPError as e:
            err_body = e.read() or b""
            self.send_response(e.code)
            for k, v in e.headers.items():
                kl = k.lower()
                if kl in ("content-length", "transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if err_body:
                self.wfile.write(err_body)
            log(f"⚠️  上游错误 {e.code}: {e.reason}")

        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            log(f"❌ 连接 Ollama 失败: {e}")
            self._send_json_error(502, "bad_gateway",
                f"Failed to connect to Ollama at {OLLAMA_HOST}: {str(e)}")

        except BrokenPipeError:
            log("⚠️  客户端断开连接")

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    def do_PUT(self):
        self._forward("PUT")

    def do_DELETE(self):
        self._forward("DELETE")

    def do_PATCH(self):
        self._forward("PATCH")


class ThreadedTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """多线程 HTTP 服务器，支持流式并发"""
    daemon_threads = True
    allow_reuse_address = True


def main():
    server = ThreadedTCPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    log("=" * 50)
    log("  Ollama OpenAI Token Server 代理启动成功")
    log("=" * 50)
    log(f"  监听地址:  http://{LISTEN_HOST}:{LISTEN_PORT}")
    log(f"  本地端点:  http://127.0.0.1:{LISTEN_PORT}/v1")
    log(f"  上游 Ollama: {OLLAMA_HOST}")
    log(f"  API Key (前8位): {API_KEY[:8]}...")
    log("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("\n🛑 收到停止信号，退出中...")
        server.shutdown()


if __name__ == "__main__":
    main()
