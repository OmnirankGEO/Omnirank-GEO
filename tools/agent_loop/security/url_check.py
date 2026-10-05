"""SSRF defense for agent_loop web_visit · v1.3 加固版.

修复:
- P0-15 (Audit A) · DNS rebinding(只看 raw host 不解析 IP)
- P0-15 (Audit A) · 302 redirect to metadata 不挡(由 adapters/web.py disallow_redirects)
- P1-SEC-1 (Audit B) · DNS-host 缺口(metadata.google.internal 类 cloud metadata)
- Verify B 边缘 #2 · gethostbyname_ex sync syscall 在 async 阻塞 event loop
  · 提供 is_safe_url_async(url) · 内部 asyncio.to_thread 包装 · adapter 调用走 async 版
  · feedback_async_event_loop_block 配套 · WORKERS=1 高 QPS 防御

策略(layered defense):
1. scheme 白名单(http/https only)
2. 字符串黑名单(localhost / cloud metadata 主机名)
3. DNS 解析后 IP 检查(blocks DNS rebinding)
4. IP 私网 / loopback / metadata 服务地址段
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_HOSTS = {
    "localhost",
    "0.0.0.0",
    # P1-SEC-1 (Audit B) cloud metadata 主机名(DNS-host 缺口)
    "metadata.google.internal",
    "metadata",  # AWS / Azure shorthand
    "instance-data",  # AWS
    "metadata.azure.com",
    "metadata.tencentyun.com",  # 腾讯云
    "100.100.100.200",  # 阿里云 metadata IP
}

# AWS / GCP / Azure / Alibaba / Tencent metadata 服务地址段
_METADATA_IPS = {
    "169.254.169.254",   # AWS / GCP / Azure
    "169.254.170.2",     # AWS ECS task metadata
    "100.100.100.200",   # Aliyun
    "169.254.0.1",
}


def is_safe_url(url: str) -> tuple[bool, str]:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False, "scheme_not_allowed"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "missing_host"

    # Layer 1 · 字符串黑名单(DNS-host 缺口堵住)
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        return False, "private_host"
    # 子域名也拦(防 evil-metadata.google.internal · 同主机注入)
    if any(host.endswith("." + b) for b in _BLOCKED_HOSTS if "." in b):
        return False, "metadata_hostname"

    # Layer 2 · 直接 IP 类
    try:
        ip = ipaddress.ip_address(host)
        if str(ip) in _METADATA_IPS:
            return False, "metadata_ip"
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False, "private_ip"
        return True, "ok"
    except ValueError:
        pass

    # Layer 3 · DNS 解析后 IP 检查(防 DNS rebinding · P0-15 fix)
    # evil.attacker.com 解析到 169.254.169.254 → 应拦
    try:
        resolved = socket.gethostbyname_ex(host)
        # [0] hostname / [1] aliaslist / [2] ipaddrlist
        all_ips = list(resolved[2] or [])
        for ip_str in all_ips:
            try:
                ip_obj = ipaddress.ip_address(ip_str)
                if str(ip_obj) in _METADATA_IPS:
                    return False, "dns_resolves_to_metadata"
                if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_reserved:
                    return False, "dns_resolves_to_private"
            except ValueError:
                continue
    except (socket.gaierror, socket.herror, OSError):
        # DNS 解析失败 → 让 adapter 层处理(不阻断 schema-only safe URL)
        # 但若是公网 host DNS 都解析不到 · adapter 调 aiohttp 也会失败
        return True, "ok_dns_unresolvable"

    return True, "ok"


async def is_safe_url_async(url: str) -> tuple[bool, str]:
    """Verify B 边缘 #2 修 · async 版 · 防 gethostbyname_ex 阻塞 event loop.

    内部 asyncio.to_thread 包 sync is_safe_url(含 DNS gethostbyname_ex syscall)。
    adapters/web.py 必走此 async 版 · 不再直接调 is_safe_url(blocking)。
    feedback_async_event_loop_block 配套铁律 · WORKERS=1 高 QPS 防御。
    """
    return await asyncio.to_thread(is_safe_url, url)
