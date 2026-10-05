"""SSRF 安全的固定 IP HTTPS 探测传输层(逐跳校验 + pinned IP + 证书不放松)。

本模块是**纯搬家**:整块实现原在 ``api/referral_api.py``(白标 Logo/Favicon 外链
探测 R3/R4/R7 逐轮加固的成果),2026-08-19 因文章域「发布公开 URL 服务端核实」
需要复用同一条防线而抽到 services 层。

🔴 搬家纪律:``api/referral_api.py`` 改为从这里 import 同名符号,**行为按字节不变**;
   既有三份锁(``tests/test_whitelabel_url_probe_ssrf.py`` /
   ``tests/test_whitelabel_id132_regression.py`` / ``tests/test_whitelabel_v36.py``)
   仍 monkeypatch ``api.referral_api._pinned_https_roundtrip`` —— import 进来的是
   模块级全局名,patch 依旧生效,搬家有没有搬坏由它们判。

新增的只有 :func:`pinned_https_get_body` 一个读体入口(内容指纹要正文,原实现只读
响应头)。它复用同一套逐跳校验与 pinned 拨号,不另开一条出站路径。
"""
from __future__ import annotations

import http.client
import ipaddress
from urllib.parse import urlsplit


# [R7 · 复审 P1] is_global=True 但仍可指向内网的过渡/翻译地址族（显式拒绝）：
#   fec0::/10        站点本地（已废弃；Python is_global=True）
#   ::ffff:0:0:0/80  非标准移位"类映射"形式（攻击者手工构造，ipv4_mapped=None）
#   2001::/32        Teredo（内嵌地址混淆，不可能承载公网 HTTPS 图床）
_V6_PROBE_DENIED_NETWORKS = (
    ipaddress.ip_network("fec0::/10"),
    ipaddress.ip_network("::ffff:0:0:0/80"),
    ipaddress.ip_network("2001::/32"),
)
# 前 80 位全零的兼容/映射形式：仅标准 IPv4-mapped（::ffff/96）提取内嵌 v4 校验，
# 其余（v4-compatible 已废弃、未指定/环回、非标准形式）一律拒绝。
_V6_COMPAT_ZERO80_NETWORK = ipaddress.ip_network("::/80")
# NAT64 well-known prefix：内嵌 IPv4 在最后 32 位，须按同一门禁校验内嵌地址
# （64:ff9b::10.0.0.1 → 内嵌私网拒绝；64:ff9b::<公网v4> → 放行，拨 NAT64 地址本身）。
_V6_NAT64_WKP_NETWORK = ipaddress.ip_network("64:ff9b::/96")
# 6to4：内嵌 IPv4 经 sixtofour 提取后按同一门禁校验（跨 Python 版本语义一致）。
_V6_6TO4_NETWORK = ipaddress.ip_network("2002::/16")


def _assert_public_probe_host(host: str) -> tuple:
    """解析 host 并仅放行全球单播地址（SSRF 防线）。

    返回去重后的公网 IP 元组：它们同时是本次连接的固定拨号目标（pinned IP）。
    必要条件为 ``ip.is_global`` 且非 ``ip.is_multicast``——单纯排除 private/reserved
    等属性会漏掉 is_global=False 但 is_private=False 的非全球段（如 CGNAT
    100.64.0.0/10、文档段 192.0.2.0/24、ULA fd00::/7）；而 is_global 对组播地址
    （224.0.0.0/4、ff00::/8）仍返回 True，组播必须显式拒绝。
    过渡/翻译族（R7）：fec0::/10 站点本地、::ffff:0:0:0/80 非标准移位映射、Teredo
    显式拒绝；IPv4-mapped（::ffff/96）、NAT64 WKP（64:ff9b::/96）、6to4（2002::/16）
    提取内嵌 IPv4 按同一门禁校验；其余前 80 位全零形式一律拒绝。
    任一解析结果不合规即整体拒绝（fail-closed，不做"挑一个公网 IP"的部分放行）；
    解析失败或空结果同样抛 ValueError，由调用方转成统一 400 人话。
    """
    import socket

    resolved = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = []
    for info in resolved:
        if info and len(info) >= 5 and info[4]:
            raw = str(info[4][0])
            if raw not in ips:
                ips.append(raw)
    if not ips:
        raise ValueError("host 无法解析")
    for raw_ip in ips:
        ip = ipaddress.ip_address(raw_ip)
        if isinstance(ip, ipaddress.IPv6Address):
            if any(ip in net for net in _V6_PROBE_DENIED_NETWORKS):
                raise ValueError("host 指向非公网地址")
            if ip in _V6_NAT64_WKP_NETWORK:
                ip = ipaddress.IPv4Address(ip.packed[-4:])
            elif ip in _V6_6TO4_NETWORK:
                embedded = ip.sixtofour
                if embedded is None:
                    raise ValueError("host 指向非公网地址")
                ip = embedded
            elif ip in _V6_COMPAT_ZERO80_NETWORK:
                mapped = ip.ipv4_mapped
                if mapped is None:
                    raise ValueError("host 指向非公网地址")
                ip = mapped
        if ip.is_multicast or not ip.is_global:
            raise ValueError("host 指向非公网地址")
    return tuple(ips)


def _assert_safe_probe_hop(url: str) -> tuple:
    """每一跳（含初始 URL 与每个 302 目标）重新校验 scheme + 端口 + 公网 IP。

    返回 (host, pinned_ips, target)：
      · host        原始域名（TLS SNI / 证书 hostname / HTTP Host 头必须用它）；
      · pinned_ips  本跳连接的唯一合法拨号目标（校验通过的公网 IP）；
      · target      origin-form 请求路径（含 query）。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValueError("重定向地址格式非法") from None
    if (parts.scheme or "").lower() != "https":
        raise ValueError("重定向目标必须是 https")
    host = parts.hostname or ""
    if not host:
        raise ValueError("重定向目标缺少域名")
    # [2026-07-22 R3 · P2] 端口收敛:仅默认端口/显式 443。显式非 443 端口可把探测
    # 变成内网任意端口盲打（公网域名:8443 → NAT/反代后段服务）。urlsplit.port 对
    # 非法端口抛 ValueError，同属拒绝。
    try:
        port = parts.port
    except ValueError:
        raise ValueError("端口非法") from None
    if port not in (None, 443):
        raise ValueError("仅支持默认 443 端口")
    pinned_ips = _assert_public_probe_host(host)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return host, pinned_ips, target


class _PinnedIPHTTPSConnection(http.client.HTTPSConnection):
    """固定 IP 的 HTTPS 连接（R4 · P1-3 防 DNS rebinding）。

    TCP 只拨 pinned_ips（校验阶段已验证为公网）；连接阶段绝不发起 DNS 解析。
    TLS 握手仍走 stdlib 默认流程：SNI 与证书 hostname 校验使用原始域名
    （self.host），证书验证保持开启；HTTP Host 头同为原始域名。
    """

    def __init__(self, host: str, pinned_ips, timeout: float = 5):
        super().__init__(host, timeout=timeout)
        self._pinned_ips = tuple(pinned_ips)
        # 仅替换"拨号"这一层（stdlib connect() 经 self._create_connection 建裸
        # socket 后再做 TLS wrap）；address 参数（域名）被有意忽略，杜绝二次解析。
        self._create_connection = self._dial_pinned_ip

    def _dial_pinned_ip(self, address, timeout=None, source_address=None):
        import socket

        last_error = None
        for pinned_ip in self._pinned_ips:
            try:
                return socket.create_connection((pinned_ip, 443), timeout, source_address)
            except OSError as exc:  # 某个 pinned IP 不可达 → 按序尝试其余已验证 IP
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError("no pinned IP available")


def _pinned_https_roundtrip(host: str, pinned_ips, method: str, target: str,
                            timeout: float = 5) -> tuple:
    """单跳固定 IP HTTPS 请求；返回 (status, headers)（header 名小写）。

    每个连接对象只服务一个请求：HEAD 405/501 降级 GET 时新建连接，但复用同一跳
    已固定的 pinned_ips（绝不二次 DNS）。所有网络/TLS/HTTP 异常抛给上层统一转
    400 人话。测试可 monkeypatch 本函数注入对端编排，无需出站。
    """
    conn = _PinnedIPHTTPSConnection(host, pinned_ips, timeout=timeout)
    try:
        conn.request(method, target, headers={
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "OmniRank-Whitelabel-Probe/1.0",
        })
        resp = conn.getresponse()
        headers = {}
        for key, value in resp.getheaders():
            headers[str(key).lower()] = str(value)
        return resp.status, headers
    finally:
        conn.close()


# ============================================================
# 读体入口(文章域「公开 URL 服务端核实」用 · 2026-08-19 新增)
# ============================================================
# 原 `_pinned_https_roundtrip` 只读响应头(图片探测够用)。内容指纹必须看正文,
# 因此这里补一个**读体**入口:同一套 pinned 拨号、同一套证书校验,只是多读
# 至多 `max_bytes` 字节并按 charset 解码。上限是硬的 —— 对端可以是任意公网站点,
# 不设上限等于把内存交给它。
PROBE_BODY_MAX_BYTES = 512 * 1024


def pinned_https_get_body(host: str, pinned_ips, target: str,
                          timeout: float = 8,
                          max_bytes: int = PROBE_BODY_MAX_BYTES,
                          user_agent: str = "OmniRank-Publication-Probe/1.0") -> tuple:
    """单跳固定 IP HTTPS GET;返回 (status, headers, body_text)。

    body 至多读 ``max_bytes`` 字节;解码失败按 utf-8 errors="replace" 兜底(指纹匹配
    容得下少量替换字符,而抛异常会把"页面能取到"误判成"取不到")。
    """
    conn = _PinnedIPHTTPSConnection(host, pinned_ips, timeout=timeout)
    try:
        conn.request("GET", target, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": user_agent,
        })
        resp = conn.getresponse()
        headers = {}
        for key, value in resp.getheaders():
            headers[str(key).lower()] = str(value)
        raw = resp.read(max_bytes) or b""
        charset = "utf-8"
        ctype = headers.get("content-type", "")
        if "charset=" in ctype.lower():
            charset = ctype.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")
        return resp.status, headers, body
    finally:
        conn.close()
