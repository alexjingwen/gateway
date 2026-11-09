#!/usr/bin/env python3
"""
公网端口可达性检测脚本 v3
通过多个第三方服务从外部服务器检测端口是否开放。
内置 DNS 绕过（系统 DNS 异常时直接用公共 DNS 服务器）。
"""

import socket
import urllib.request
import urllib.parse
import json
import ssl
import struct
import time

# ========== 配置 ==========
PUBLIC_IP = "27.19.151.157"
PORTS_TO_CHECK = [7001, 7500, 7100]
TIMEOUT = 12
FALLBACK_DNS = ["223.5.5.5", "8.8.8.8", "119.29.29.29"]
# ==========================


_dns_cache = {}


def query_dns_server(hostname, dns_server, timeout=5):
    """通过指定 DNS 服务器直接查询 A 记录（UDP）"""
    txid = 0x1234
    flags = 0x0100  # 标准查询，递归
    header = struct.pack(">HHHHHH", txid, flags, 1, 0, 0, 0)
    qname = b""
    for part in hostname.split("."):
        qname += bytes([len(part)]) + part.encode()
    qname += b"\x00"
    packet = header + qname + struct.pack(">HH", 1, 1)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (dns_server, 53))
        data, _ = s.recvfrom(1024)
    finally:
        s.close()

    if len(data) < 12:
        return None
    rcode = data[3] & 0x0F
    if rcode != 0:
        return None
    ancount = struct.unpack(">H", data[4:6])[0]
    if ancount == 0:
        return None

    offset = 12
    while data[offset] != 0:
        offset += data[offset] + 1
    offset += 5

    for _ in range(ancount):
        if data[offset] & 0xC0 == 0xC0:
            offset += 2
        else:
            while data[offset] != 0:
                offset += data[offset] + 1
            offset += 1
        rtype = struct.unpack(">H", data[offset:offset+2])[0]
        offset += 8
        rdlength = struct.unpack(">H", data[offset:offset+2])[0]
        offset += 2
        if rtype == 1 and rdlength == 4:
            return ".".join(str(b) for b in data[offset:offset+4])
        offset += rdlength
    return None


def dns_resolve(hostname):
    """DNS 解析：先系统，失败则用公共 DNS 服务器"""
    if hostname in _dns_cache:
        return _dns_cache[hostname]

    # 1. 系统解析
    try:
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("0."):
            _dns_cache[hostname] = ip
            return ip
    except Exception:
        pass

    # 2. 公共 DNS 服务器
    for dns_server in FALLBACK_DNS:
        try:
            ip = query_dns_server(hostname, dns_server)
            if ip:
                _dns_cache[hostname] = ip
                return ip
        except Exception:
            continue
    return None


def http_get(url, timeout=TIMEOUT, data=None, headers=None):
    """HTTP/HTTPS 请求，自动绕过系统 DNS"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    scheme = parsed.scheme
    hostname = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    # 解析 IP
    ip = dns_resolve(hostname)
    if not ip:
        raise RuntimeError(f"无法解析域名: {hostname}")

    # 建立 TCP 连接
    sock = socket.create_connection((ip, port), timeout=timeout)

    # HTTPS 包装
    if scheme == "https":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=hostname)

    # 构造 HTTP 请求
    method = "POST" if data else "GET"
    hdrs = {
        "Host": hostname + (f":{port}" if port not in (80, 443) else ""),
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "*/*",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    if data:
        hdrs["Content-Length"] = str(len(data))
        if "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"

    req_line = f"{method} {path} HTTP/1.1\r\n"
    header_block = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
    body = b""
    if data:
        if isinstance(data, str):
            data = data.encode()
        body = data

    request = (req_line + header_block + "\r\n").encode() + body

    sock.sendall(request)

    # 读取响应
    response = b""
    while True:
        try:
            chunk = sock.recv(8192)
            if not chunk:
                break
            response += chunk
        except socket.timeout:
            break
    sock.close()

    # 解析响应
    header_end = response.find(b"\r\n\r\n")
    if header_end < 0:
        return 0, response.decode("utf-8", errors="ignore")

    status_line = response[:response.find(b"\r\n")].decode()
    try:
        status_code = int(status_line.split()[1])
    except Exception:
        status_code = 0

    body_text = response[header_end+4:].decode("utf-8", errors="ignore")

    # 处理 chunked 编码
    if "transfer-encoding: chunked" in response[:header_end].decode().lower():
        body_text = decode_chunked(body_text)

    return status_code, body_text


def decode_chunked(text):
    """简单解码 chunked transfer encoding"""
    result = ""
    try:
        pos = 0
        while pos < len(text):
            crlf = text.find("\r\n", pos)
            if crlf < 0:
                break
            size = int(text[pos:crlf], 16)
            if size == 0:
                break
            result += text[crlf+2:crlf+2+size]
            pos = crlf + 2 + size + 2
    except Exception:
        pass
    return result


def get_public_ip():
    """获取当前公网 IP"""
    sources = [
        "https://api.ipify.org?format=json",
        "https://api.ip.sb/jsonip",
        "https://ipinfo.io/json",
    ]
    for url in sources:
        try:
            code, body = http_get(url, timeout=8)
            if code == 200:
                data = json.loads(body)
                ip = data.get("ip") or data.get("query") or data.get("ip_addr")
                if ip:
                    return ip
        except Exception:
            continue
    return None


def check_local(ip, port):
    """本机直连测试（受 NAT 回环影响，仅参考）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(6)
        start = time.time()
        result = s.connect_ex((ip, port))
        elapsed = time.time() - start
        s.close()
        if result == 0:
            return True, f"可连接 ({elapsed:.2f}s) - 可能是 NAT 回环，不代表公网可达"
        else:
            return False, f"连接失败 errno={result} (本机直连被拦截，正常)"
    except Exception as e:
        return False, f"异常: {e}"


def check_hackertarget(ip, port):
    """通过 hackertarget.com 的 nmap API 检测"""
    try:
        url = f"https://api.hackertarget.com/nmap/?q={ip}+-p+{port}"
        code, body = http_get(url, timeout=20)
        body_lower = body.lower()
        if "open" in body_lower:
            return True, f"开放 (hackertarget): {body.strip()[:100]}"
        elif "closed" in body_lower or "filtered" in body_lower:
            return False, f"关闭/过滤 (hackertarget): {body.strip()[:100]}"
        return None, f"无法解析: {body.strip()[:100]}"
    except Exception as e:
        return None, f"请求失败: {str(e)[:80]}"


def check_yougetsignal(ip, port):
    """通过 yougetsignal.com 检测"""
    try:
        data = urllib.parse.urlencode({
            "remoteAddress": ip,
            "portNumber": str(port),
        })
        code, body = http_get(
            "https://ports.yougetsignal.com/check-port.php",
            timeout=TIMEOUT,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.yougetsignal.com/tools/open-ports/",
            },
        )
        if "open" in body.lower():
            return True, "开放 (yougetsignal)"
        elif "closed" in body.lower():
            return False, "关闭 (yougetsignal)"
        return None, f"无法解析: {body.strip()[:100]}"
    except Exception as e:
        return None, f"请求失败: {str(e)[:80]}"


def check_aming(ip, port):
    """通过 aming 居检测（国内服务）"""
    try:
        url = f"https://tool.chinaz.com/iframe.ashx?t=port&q={ip}:{port}"
        code, body = http_get(url, timeout=TIMEOUT, headers={
            "Referer": "https://tool.chinaz.com/port",
        })
        body_lower = body.lower()
        if "open" in body_lower or "开放" in body:
            return True, "开放 (chinaz)"
        elif "closed" in body_lower or "关闭" in body:
            return False, "关闭 (chinaz)"
        return None, f"无法解析: {body.strip()[:100]}"
    except Exception as e:
        return None, f"请求失败: {str(e)[:80]}"


def icon(ok):
    if ok is True:
        return "✅"
    elif ok is False:
        return "❌"
    else:
        return "⚠️ "


def check_port(ip, port):
    """综合检测一个端口"""
    print(f"\n{'='*60}")
    print(f"🔍 检测端口: {ip}:{port}")
    print(f"{'='*60}")

    # 1. 本机直连（参考）
    print(f"\n[1/4] 本机直连（受 NAT 回环影响，仅参考）...")
    ok, msg = check_local(ip, port)
    print(f"      {icon(ok)} {msg}")
    local_ok = ok

    # 2. hackertarget（美国）
    print(f"\n[2/4] 通过 hackertarget.com 检测（美国服务器）...")
    ok, msg = check_hackertarget(ip, port)
    print(f"      {icon(ok)} {msg}")
    result_api1 = ok

    # 3. yougetsignal（美国）
    print(f"\n[3/4] 通过 yougetsignal.com 检测（美国服务器）...")
    ok, msg = check_yougetsignal(ip, port)
    print(f"      {icon(ok)} {msg}")
    result_api2 = ok

    # 4. chinaz（国内）
    print(f"\n[4/4] 通过 chinaz 站长之家检测（国内服务器）...")
    ok, msg = check_aming(ip, port)
    print(f"      {icon(ok)} {msg}")
    result_api3 = ok

    # 综合判断
    external_results = [r for r in [result_api1, result_api2, result_api3] if r is not None]
    if external_results:
        if any(external_results):
            return True
        else:
            return False
    return None


def main():
    print("=" * 60)
    print("  公网端口可达性检测脚本 v3")
    print("  通过多个外部服务从公网检测端口开放状态")
    print("  内置 DNS 绕过（系统 DNS 异常时直接用 223.5.5.5 等）")
    print("=" * 60)

    # 获取公网 IP
    print("\n📡 获取当前公网 IP...")
    current_ip = get_public_ip()
    if current_ip:
        print(f"   ✅ 当前公网 IP: {current_ip}")
        if current_ip != PUBLIC_IP:
            print(f"   ⚠️  公网 IP 已变化！（配置时是 {PUBLIC_IP}）")
            print(f"   💡 需更新路由器映射规则")
            use_ip = current_ip
        else:
            print(f"   ✅ 公网 IP 未变化")
            use_ip = PUBLIC_IP
    else:
        print(f"   ⚠️  无法获取公网 IP，使用配置中的 IP: {PUBLIC_IP}")
        use_ip = PUBLIC_IP

    print(f"\n🎯 检测目标: {use_ip}")
    print(f"   将从美国/国内多个外部服务器检测端口开放状态")

    results = {}
    for port in PORTS_TO_CHECK:
        results[port] = check_port(use_ip, port)

    # 汇总
    print(f"\n\n{'='*60}")
    print("  📊 检测结果汇总")
    print(f"{'='*60}")
    descs = {
        7001: "FRP 代理（外部访问入口）⭐",
        7500: "FRP Dashboard",
        7100: "frps 监听（不应公网开放）",
    }
    for port in PORTS_TO_CHECK:
        r = results.get(port)
        if r is True:
            status = "✅ 开放"
        elif r is False:
            status = "❌ 关闭"
        else:
            status = "⚠️  未知"
        print(f"  {use_ip}:{port:<6}  {status}  - {descs.get(port, '')}")

    # 诊断
    print(f"\n{'='*60}")
    print("  🔧 诊断建议")
    print(f"{'='*60}")
    r7001 = results.get(7001)
    if r7001 is True:
        print("  ✅ 7001 端口公网可达！配置成功！")
        print(f"     访问地址: http://{use_ip}:7001/admin")
        print(f"     API 地址:  http://{use_ip}:7001/v1")
    elif r7001 is False:
        print("  ❌ 7001 端口公网不可达，排查步骤:")
        print(f"     1. 登录路由器 192.168.3.1 确认 WAN IP = {use_ip}")
        print("        (若不一致，说明运营商 NAT，无法直接穿透)")
        print("     2. 确认端口映射规则已保存: 外部7001→192.168.3.67:7001 TCP")
        print("     3. 确认路由器防火墙未拦截")
        print("     4. 若以上都正常仍失败，可能运营商封端口，改用 8080:")
        print("        修改 frpc.toml 中 remotePort = 8080，路由器映射 8080→7001")
    else:
        print("  ⚠️  外部检测服务暂时不可用")
        print(f"     最终方案：用手机 4G 访问 http://{use_ip}:7001/admin")

    print()


if __name__ == "__main__":
    main()
