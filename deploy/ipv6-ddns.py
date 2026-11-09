#!/usr/bin/env python3
"""
IPv6 动态 DNS 更新脚本
定期检测本机稳定 IPv6 地址，变化时通过 DNSPod API 更新 AAAA 记录。
无 DNSPod token 时降级为本地记录文件，便于查看当前 IPv6。

使用方式:
  1. 仅本地记录（默认）: python3 ipv6-ddns.py
  2. 配合 DNSPod: 设置环境变量 DPOD_ID / DPOD_TOKEN / DPOD_DOMAIN / DPOD_SUBDOMAIN
"""

import os
import sys
import json
import socket
import time
import subprocess
import urllib.request
import urllib.parse
import ssl
from pathlib import Path

# ========== 配置 ==========
STATE_FILE = Path.home() / ".ollama-token-gateway" / "ipv6-ddns-state.json"
CHECK_INTERVAL = int(os.environ.get("DDNS_INTERVAL", "300"))  # 5 分钟

# DNSPod 配置（可选，未设置则只做本地记录）
DPOD_ID = os.environ.get("DPOD_ID", "")
DPOD_TOKEN = os.environ.get("DPOD_TOKEN", "")
DPOD_DOMAIN = os.environ.get("DPOD_DOMAIN", "")  # 例: example.com
DPOD_SUBDOMAIN = os.environ.get("DPOD_SUBDOMAIN", "ollama")  # 例: ollama -> ollama.example.com
# ==========================


def get_stable_ipv6():
    """获取本机稳定的全局 IPv6 地址（autoconf secured）"""
    try:
        result = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.split("\n")
        for i, line in enumerate(lines):
            if "inet6 240" in line or "inet6 2001" in line or "inet6 260" in line:
                # 全局 IPv6 地址
                parts = line.strip().split()
                if len(parts) >= 2:
                    ip = parts[1]
                    # 优先返回 secured（稳定）地址
                    if "secured" in line:
                        return ip
        # 没找到 secured，返回任何全局地址
        for line in lines:
            if ("inet6 240" in line or "inet6 2001" in line) and "fe80" not in line:
                parts = line.strip().split()
                if parts:
                    return parts[1]
    except Exception as e:
        print(f"获取 IPv6 失败: {e}")
    return None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_ipv6": "", "last_update": 0, "history": []}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def make_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def dnspod_api(action, params):
    """调用 DNSPod API"""
    if not DPOD_ID or not DPOD_TOKEN:
        return None, "DNSPod 凭据未配置"
    url = f"https://dnsapi.cn/{action}"
    data = {
        "login_token": f"{DPOD_ID},{DPOD_TOKEN}",
        "format": "json",
        "lang": "cn",
        **params,
    }
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"User-Agent": "IPv6-DDNS/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=make_ctx()) as r:
            return json.loads(r.read().decode()), None
    except Exception as e:
        return None, str(e)


def update_dnspod(ipv6):
    """更新 DNSPod AAAA 记录"""
    # 1. 获取记录列表
    resp, err = dnspod_api(
        "Record.List",
        {"domain": DPOD_DOMAIN, "sub_domain": DPOD_SUBDOMAIN, "record_type": "AAAA"},
    )
    if err:
        return False, f"获取记录失败: {err}"
    if resp.get("status", {}).get("code") != "1":
        return False, f"获取记录失败: {resp.get('status', {}).get('message')}"

    records = resp.get("records", [])
    if records:
        # 更新现有记录
        record_id = records[0]["id"]
        resp, err = dnspod_api(
            "Record.Modify",
            {
                "domain": DPOD_DOMAIN,
                "record_id": record_id,
                "sub_domain": DPOD_SUBDOMAIN,
                "record_type": "AAAA",
                "record_line": "默认",
                "value": ipv6,
                "ttl": "60",
            },
        )
    else:
        # 创建新记录
        resp, err = dnspod_api(
            "Record.Create",
            {
                "domain": DPOD_DOMAIN,
                "sub_domain": DPOD_SUBDOMAIN,
                "record_type": "AAAA",
                "record_line": "默认",
                "value": ipv6,
                "ttl": "60",
            },
        )

    if err:
        return False, f"更新失败: {err}"
    if resp.get("status", {}).get("code") != "1":
        return False, f"更新失败: {resp.get('status', {}).get('message')}"
    return True, "已更新"


def main():
    print("=" * 60)
    print("  IPv6 动态 DNS 更新脚本")
    print("=" * 60)

    state = load_state()
    current_ipv6 = get_stable_ipv6()

    if not current_ipv6:
        print("❌ 未检测到全局 IPv6 地址")
        return

    print(f"当前 IPv6: {current_ipv6}")
    print(f"上次记录: {state.get('last_ipv6', '无')}")

    if current_ipv6 == state.get("last_ipv6"):
        print("✅ IPv6 未变化，无需更新")
        return

    print(f"\n🔄 IPv6 已变化，开始更新...")
    print(f"  旧: {state.get('last_ipv6', '无')}")
    print(f"  新: {current_ipv6}")

    # 更新 DNSPod（如果配置了）
    if DPOD_ID and DPOD_TOKEN and DPOD_DOMAIN:
        ok, msg = update_dnspod(current_ipv6)
        if ok:
            print(f"✅ DNSPod 更新成功: {msg}")
            full_domain = f"{DPOD_SUBDOMAIN}.{DPOD_DOMAIN}"
            print(f"   访问域名: http://{full_domain}:3000/admin")
        else:
            print(f"❌ DNSPod 更新失败: {msg}")
    else:
        print("\n💡 未配置 DNSPod，仅本地记录")
        print("   如需自动更新域名解析，设置以下环境变量：")
        print("   DPOD_ID, DPOD_TOKEN, DPOD_DOMAIN, DPOD_SUBDOMAIN")

    # 保存状态
    state["last_ipv6"] = current_ipv6
    state["last_update"] = time.time()
    state["history"] = (state.get("history") or [])[-9:] + [
        {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ipv6": current_ipv6}
    ]
    save_state(state)
    print(f"\n📝 状态已保存到 {STATE_FILE}")

    # 显示访问方式
    print(f"\n{'='*60}")
    print(f"  📡 当前访问方式")
    print(f"{'='*60}")
    print(f"  IPv6 直连: http://[{current_ipv6}]:3000/admin")
    print(f"  API 地址:  http://[{current_ipv6}]:3000/v1")
    if DPOD_ID and DPOD_DOMAIN:
        full_domain = f"{DPOD_SUBDOMAIN}.{DPOD_DOMAIN}"
        print(f"  域名访问: http://{full_domain}:3000/admin")
    print()


if __name__ == "__main__":
    main()
