"""tests/test_whitelabel_url_probe_ssrf.py — R4 · P1-3 外链探测 DNS rebinding SSRF 防线

断言 api.referral_api._probe_brand_image_url（白标 Logo/Favicon 外链探测）：

  · 302 → http://169.254.169.254（云元数据链路本地）拒绝，第二跳发出前即拦截
  · 302 → http://192.168.x.x（内网）拒绝
  · 302 → https://内网解析域名 拒绝（DNS 层防线）
  · 直接 http:// 拒绝（scheme 非 https，不发请求）
  · https host 解析到私有/环回/链路本地 IP 拒绝
  · 合法 https 图床放行；安全 https 302 链放行；重定向超 3 跳拒绝
  · 对端错误状态码/非图片/超大 → 400 统一人话，不回显对端状态码/Content-Type
    （防内网存活 oracle）
  · R4 DNS rebinding：校验解析=公网、连接阶段 DNS 漂移=私网 → 私网绝不收到请求
    （校验通过的公网 IP 被固定为本跳唯一拨号目标；连接阶段零 DNS）
  · redirect 目标域名同类 rebinding → 每跳独立钉 IP，私网绝不收到请求
  · HEAD 405/501 → GET 降级复用同一跳 pinned IP，不再漂移
  · 普通 operator 入口 PUT /whitelabel 全链路覆盖（不止 helper 单测）

mock 面：socket.getaddrinfo（DNS 编排/计次）+ ra._pinned_https_roundtrip
（逐跳响应编排，记录每次请求的 pinned_ips）；拨号层另以
_PinnedIPHTTPSConnection._dial_pinned_ip 单测钉死（socket.create_connection 观测）。
全程不出站。
"""
import socket
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.referral_api as ra

LABEL = "Logo 地址"
URL = "https://cdn.example/a.png"
PUBLIC_IP = "93.184.216.34"
PUBLIC_IP_2 = "151.101.1.140"
PRIVATE_IP = "10.0.0.5"
UNIFIED_HINT = "不可用或不支持"


# ---------------------------------------------------------------------------
# 网络编排 fixture：DNS 映射（支持按调用次数漂移的 rebinding 编排）+ 逐跳路由
# ---------------------------------------------------------------------------

@pytest.fixture
def net(monkeypatch):
    state = {"dns": {}, "dns_calls": {}, "routes": {}, "calls": []}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        state["dns_calls"][host] = state["dns_calls"].get(host, 0) + 1
        spec = state["dns"].get(host)
        if spec is None:
            raise socket.gaierror(f"Name or service not known ({host})")
        # spec 可为 list（静态解析）或 callable(第n次解析)->list（rebinding 编排）
        ips = spec(state["dns_calls"][host]) if callable(spec) else spec
        if not ips:
            raise socket.gaierror(f"Name or service not known ({host})")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in ips]

    def fake_roundtrip(host, pinned_ips, method, target, timeout=5):
        state["calls"].append({
            "method": method, "host": host,
            "pinned_ips": tuple(pinned_ips), "target": target,
        })
        route = state["routes"].get((method, host, target))
        if route is None:
            raise AssertionError(f"未编排的 {method} 请求: {host}{target}")
        status, headers = route
        return status, dict(headers)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(ra, "_pinned_https_roundtrip", fake_roundtrip)
    return state


def _route(net, method, url, status, headers=None):
    parts = urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    net["routes"][(method, parts.hostname, target)] = (status, dict(headers or {}))


def _head(net, url, status, headers=None):
    _route(net, "HEAD", url, status, headers)


def _assert_unified_400(exc_info, *, not_in_detail=()):
    assert getattr(exc_info.value, "status_code", None) == 400
    detail = getattr(exc_info.value, "detail", "")
    assert UNIFIED_HINT in detail
    for leaked in not_in_detail:
        assert leaked not in detail


def _dialed_ips(net):
    """所有已发出请求的拨号目标集合（pinned_ips 即真实 TCP 目标）。"""
    return {ip for call in net["calls"] for ip in call["pinned_ips"]}


# ---------------------------------------------------------------------------
# 重定向目标 SSRF
# ---------------------------------------------------------------------------

def test_redirect_to_cloud_metadata_ip_rejected(net):
    """302 → http://169.254.169.254（云元数据）→ 400；第二跳请求根本不会发出。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    _head(net, URL, 302, {"location": "http://169.254.169.254/latest/meta-data.png"})

    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info)
    assert [c["host"] for c in net["calls"]] == ["cdn.example"]


def test_redirect_to_private_ip_rejected(net):
    """302 → http://192.168.x.x（内网）→ 400。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    _head(net, URL, 302, {"location": "http://192.168.1.10/a.png"})

    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info)
    assert [c["host"] for c in net["calls"]] == ["cdn.example"]


def test_redirect_to_https_host_resolving_private_rejected(net):
    """302 → https://域名，但域名解析到 192.168.x.x → 400（https 也救不了内网 IP）。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    net["dns"]["internal.evil.example"] = ["192.168.1.10"]
    _head(net, URL, 302, {"location": "https://internal.evil.example/a.png"})

    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info)
    assert [c["host"] for c in net["calls"]] == ["cdn.example"]
    assert _dialed_ips(net) == {PUBLIC_IP}


# ---------------------------------------------------------------------------
# 初始 URL 防线
# ---------------------------------------------------------------------------

def test_direct_http_url_rejected(net):
    """直接 http:// → 400（scheme 非 https，不发任何请求）。"""
    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url("http://cdn.example/a.png", LABEL)

    _assert_unified_400(exc_info)
    assert net["calls"] == []


@pytest.mark.parametrize("bad_ip", [
    "10.0.0.5",              # RFC1918 私网
    "127.0.0.1",             # 环回
    "169.254.169.254",       # 链路本地（云元数据）
    "0.0.0.0",               # 未指定
    "100.64.0.1",            # CGNAT 100.64.0.0/10（is_private=False 但非全球 · R5 复审）
    "192.0.2.1",             # TEST-NET-1 文档段（非全球）
    "fd00::1",               # IPv6 ULA
    "fe80::1",               # IPv6 链路本地
    "2001:db8::1",           # IPv6 文档段
    "::ffff:10.0.0.1",       # IPv4-mapped IPv6（映射私网）
    "::ffff:100.64.0.1",     # IPv4-mapped IPv6（映射 CGNAT）
    "224.0.0.1",             # IPv4 组播（is_global=True 的组播陷阱 · R6 复审）
    "239.255.255.250",       # IPv4 组播（SSDP 等内部协议常用）
    "ff02::1",               # IPv6 链路本地组播
    "ff05::1",               # IPv6 站点本地组播
    "::ffff:239.0.0.1",      # IPv4-mapped IPv6（映射组播）
    "fec0::1",               # IPv6 站点本地（已废弃 · is_global=True · R7 复审）
    "::ffff:0:10.0.0.1",     # 非标准移位"类映射"（is_global=True · R7 复审）
    "64:ff9b::10.0.0.1",     # NAT64 WKP 内嵌私网（is_global=True · R7 复审）
    "64:ff9b:1::a00:1",      # NAT64 local-use 内嵌私网
    "2002:a00:1::1",         # 6to4 内嵌私网
    "2001::1",               # Teredo（内嵌混淆）
    "::10.0.0.1",            # IPv4-compatible 内嵌私网（已废弃 · is_global=True）
    "::93.184.216.34",       # IPv4-compatible 内嵌公网（已废弃形式，同样拒绝）
])
def test_host_resolving_to_non_public_ip_rejected(net, bad_ip):
    """https host 解析到非全球单播（含 is_global=True 的组播陷阱）→ 400，零请求发出。"""
    net["dns"]["cdn.example"] = [bad_ip]

    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info)
    assert net["calls"] == []


# ---------------------------------------------------------------------------
# 合法路径放行
# ---------------------------------------------------------------------------

def test_valid_https_image_passes(net):
    """合法 https 图床（公网 IP + image/png + ≤2MB）→ 放行；拨号目标=校验通过的公网 IP。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    _head(net, URL, 200, {"content-type": "image/png", "content-length": "1024"})

    assert ra._probe_brand_image_url(URL, LABEL) is None
    assert net["calls"] == [{
        "method": "HEAD", "host": "cdn.example",
        "pinned_ips": (PUBLIC_IP,), "target": "/a.png",
    }]


def test_safe_https_redirect_chain_passes(net):
    """302 → https://公网图床 → 200 image/jpeg → 放行（逐跳重校验+独立钉 IP 后跟随）。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    net["dns"]["cdn2.example"] = [PUBLIC_IP_2]
    _head(net, URL, 302, {"location": "https://cdn2.example/b.png"})
    _head(net, "https://cdn2.example/b.png", 200, {"content-type": "image/jpeg"})

    assert ra._probe_brand_image_url(URL, LABEL) is None
    assert [(c["host"], c["pinned_ips"]) for c in net["calls"]] == [
        ("cdn.example", (PUBLIC_IP,)),
        ("cdn2.example", (PUBLIC_IP_2,)),
    ]


def test_redirects_beyond_three_hops_rejected(net):
    """重定向链超过 3 跳 → 400 统一人话。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    hops = [URL] + [f"https://cdn.example/r{i}.png" for i in range(1, 5)]
    for current, nxt in zip(hops, hops[1:]):
        _head(net, current, 302, {"location": nxt})

    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info)
    # 3 跳上限 = 4 次请求（初始 + 3 次跟随），第 5 跳不发出
    assert [c["target"] for c in net["calls"]] == ["/a.png", "/r1.png", "/r2.png", "/r3.png"]


# ---------------------------------------------------------------------------
# 统一人话 · 无 oracle 回显
# ---------------------------------------------------------------------------

def test_peer_error_status_gives_unified_message_no_oracle(net):
    """对端 403 + text/html → 400 统一人话；不回显状态码/Content-Type。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    _head(net, URL, 403, {"content-type": "text/html"})

    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info, not_in_detail=("403", "text/html"))


def test_non_image_content_type_unified_message_no_echo(net):
    """200 但 Content-Type: text/html → 400 统一人话；不回显类型。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    _head(net, URL, 200, {"content-type": "text/html; charset=utf-8"})

    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info, not_in_detail=("text/html",))


def test_dns_failure_gives_unified_message(net):
    """DNS 解析失败 → 400 统一人话（与连接失败同口径）。"""
    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info)
    assert net["calls"] == []


def test_oversized_image_rejected_unified_message(net):
    """Content-Length > 2MB → 400 统一人话（≤2MB 既有约束保留）。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    _head(net, URL, 200, {
        "content-type": "image/png",
        "content-length": str(3 * 1024 * 1024),
    })

    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info)


def test_head_405_falls_back_to_get_same_pinned_ip(net):
    """对端不支持 HEAD（405）→ 退化 GET 只读头：复用同一跳 pinned IP，零二次 DNS。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    _head(net, URL, 405, {})
    _route(net, "GET", URL, 200, {"content-type": "image/webp"})

    assert ra._probe_brand_image_url(URL, LABEL) is None
    assert [(c["method"], c["pinned_ips"]) for c in net["calls"]] == [
        ("HEAD", (PUBLIC_IP,)),
        ("GET", (PUBLIC_IP,)),
    ]
    assert net["dns_calls"]["cdn.example"] == 1, "HEAD→GET 降级不得再次解析 DNS"


# ---------------------------------------------------------------------------
# 端口收敛（仅默认端口/443 · 灭内网任意端口盲打面）
# ---------------------------------------------------------------------------

def test_explicit_non_443_port_rejected(net):
    """初始 URL 显式非 443 端口（https://evil.com:8443/...）→ 400 统一人话,请求不发出。"""
    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url("https://evil.com:8443/logo.png", LABEL)

    _assert_unified_400(exc_info)
    assert net["calls"] == []


@pytest.mark.parametrize("port", [22, 80, 8080, 9200])
def test_redirect_to_non_443_port_rejected(net, port):
    """重定向到显式非 443 端口（含 :22/:8080 等内网服务端口）→ 400;第二跳不发出。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    _head(net, URL, 302, {"location": f"https://cdn.example:{port}/a.png"})

    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url(URL, LABEL)

    _assert_unified_400(exc_info)
    assert [c["host"] for c in net["calls"]] == ["cdn.example"]


def test_explicit_443_port_passes(net):
    """显式 :443 与默认端口同语义 → 放行。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    url443 = "https://cdn.example:443/a.png"
    _head(net, url443, 200, {"content-type": "image/png"})

    assert ra._probe_brand_image_url(url443, LABEL) is None
    assert [(c["host"], c["pinned_ips"]) for c in net["calls"]] == [
        ("cdn.example", (PUBLIC_IP,)),
    ]


def test_redirect_to_explicit_443_port_passes(net):
    """302 → https://host:443/... 标准端口链 → 放行。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    net["dns"]["cdn2.example"] = [PUBLIC_IP_2]
    _head(net, URL, 302, {"location": "https://cdn2.example:443/b.png"})
    _head(net, "https://cdn2.example:443/b.png", 200, {"content-type": "image/png"})

    assert ra._probe_brand_image_url(URL, LABEL) is None
    assert [(c["host"], c["pinned_ips"]) for c in net["calls"]] == [
        ("cdn.example", (PUBLIC_IP,)),
        ("cdn2.example", (PUBLIC_IP_2,)),
    ]


# ---------------------------------------------------------------------------
# R4 · P1-3：DNS rebinding 反例（校验=公网 / 连接阶段漂移=私网）
# ---------------------------------------------------------------------------

def test_dns_rebinding_at_connect_time_never_dials_private(net):
    """第一次解析=公网、之后解析=私网（经典 rebinding）：

    探测仍只拨校验时固定的公网 IP；连接阶段根本不触发第二次解析，
    私网 IP 永不成为拨号目标。
    """
    net["dns"]["cdn.example"] = lambda n: [PUBLIC_IP] if n == 1 else [PRIVATE_IP]
    _head(net, URL, 200, {"content-type": "image/png"})

    assert ra._probe_brand_image_url(URL, LABEL) is None
    assert net["dns_calls"]["cdn.example"] == 1, "连接阶段发生二次 DNS 解析（rebinding 窗口）"
    assert _dialed_ips(net) == {PUBLIC_IP}
    assert PRIVATE_IP not in _dialed_ips(net)


def test_redirect_chain_rebinding_never_dials_private_per_hop(net):
    """redirect 目标域名同类 rebinding：每跳独立钉 IP，任一跳私网都收不到请求。"""
    net["dns"]["cdn.example"] = lambda n: [PUBLIC_IP] if n == 1 else [PRIVATE_IP]
    net["dns"]["cdn2.example"] = lambda n: [PUBLIC_IP_2] if n == 1 else ["192.168.1.10"]
    _head(net, URL, 302, {"location": "https://cdn2.example/b.png"})
    _head(net, "https://cdn2.example/b.png", 200, {"content-type": "image/png"})

    assert ra._probe_brand_image_url(URL, LABEL) is None
    assert net["dns_calls"] == {"cdn.example": 1, "cdn2.example": 1}
    assert _dialed_ips(net) == {PUBLIC_IP, PUBLIC_IP_2}


def test_redirect_host_rebinding_private_after_validation_still_blocked(net):
    """redirect 域名首解=公网但后续解析全私网：探测完成且只拨公网；

    若实现改为"连接时重新解析"，本用例会拨到 192.168.1.10（断言失败）。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    net["dns"]["cdn2.example"] = lambda n: [PUBLIC_IP_2] if n == 1 else ["192.168.1.10"]
    _head(net, URL, 302, {"location": "https://cdn2.example/b.png"})
    _head(net, "https://cdn2.example/b.png", 200, {"content-type": "image/png"})

    assert ra._probe_brand_image_url(URL, LABEL) is None
    assert "192.168.1.10" not in _dialed_ips(net)
    assert net["dns_calls"]["cdn2.example"] == 1


def test_head_to_get_fallback_no_dns_redrift(net):
    """HEAD→GET 降级不得再次解析/漂移：同一 pinned IP，DNS 全程只解析一次。"""
    net["dns"]["cdn.example"] = lambda n: [PUBLIC_IP] if n == 1 else [PRIVATE_IP]
    _head(net, URL, 405, {})
    _route(net, "GET", URL, 200, {"content-type": "image/png"})

    assert ra._probe_brand_image_url(URL, LABEL) is None
    assert [c["method"] for c in net["calls"]] == ["HEAD", "GET"]
    assert {c["pinned_ips"] for c in net["calls"]} == {(PUBLIC_IP,)}
    assert net["dns_calls"]["cdn.example"] == 1


# ---------------------------------------------------------------------------
# R4 · P1-3：拨号层钉死（_PinnedIPHTTPSConnection）
# ---------------------------------------------------------------------------

def test_pinned_connection_dials_only_pinned_ip_no_dns(monkeypatch):
    """拨号层单测：_dial_pinned_ip 只拨 pinned_ip:443，连接阶段零 DNS；

    域名仍留在 conn.host（TLS SNI / 证书 hostname / HTTP Host 的来源）。"""
    dialed = []

    def fake_create_connection(address, timeout=None, source_address=None):
        dialed.append(address)
        return object()  # 裸 socket 替身（本用例不做 TLS）

    def boom_getaddrinfo(*a, **k):
        raise AssertionError("连接阶段发生 DNS 解析（rebinding 窗口）")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", boom_getaddrinfo)

    conn = ra._PinnedIPHTTPSConnection("cdn.example", (PUBLIC_IP,), timeout=5)
    sock = conn._dial_pinned_ip(("cdn.example", 443), 5, None)

    assert sock is not None
    assert dialed == [(PUBLIC_IP, 443)]
    assert conn.host == "cdn.example", "SNI/证书 hostname/Host 必须保持原始域名"


def test_pinned_connection_tries_validated_ips_in_order(monkeypatch):
    """首个 pinned IP 不可达 → 按序拨其余已验证 IP（全部公网，不回退 DNS）。"""
    dialed = []

    def fake_create_connection(address, timeout=None, source_address=None):
        dialed.append(address)
        if address[0] == PUBLIC_IP:
            raise OSError("connection refused")
        return object()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("连接阶段发生 DNS 解析")))

    conn = ra._PinnedIPHTTPSConnection("cdn.example", (PUBLIC_IP, PUBLIC_IP_2), timeout=5)
    conn._dial_pinned_ip(("cdn.example", 443), 5, None)

    assert dialed == [(PUBLIC_IP, 443), (PUBLIC_IP_2, 443)]


def test_assert_public_probe_host_returns_validated_ips(net):
    """校验函数返回的 pinned IP 即解析出的全球地址（去重保序）；任一非全球即整体拒绝。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP, PUBLIC_IP_2, PUBLIC_IP]
    assert ra._assert_public_probe_host("cdn.example") == (PUBLIC_IP, PUBLIC_IP_2)

    net["dns"]["mixed.example"] = [PUBLIC_IP, "192.168.1.10"]
    with pytest.raises(ValueError):
        ra._assert_public_probe_host("mixed.example")

    # R5 复审：混合结果含 CGNAT（is_private=False 但非全球）同样整体拒绝
    net["dns"]["cgnat.example"] = [PUBLIC_IP, "100.64.0.1"]
    with pytest.raises(ValueError):
        ra._assert_public_probe_host("cgnat.example")

    # R6 复审：混合结果含组播（is_global=True 的陷阱）同样整体拒绝
    net["dns"]["mcast.example"] = [PUBLIC_IP, "239.0.0.1"]
    with pytest.raises(ValueError):
        ra._assert_public_probe_host("mcast.example")

    # IPv6 全球单播与 IPv4-mapped 公网照常放行（不误杀）
    net["dns"]["v6.example"] = ["2606:4700:4700::1111"]
    assert ra._assert_public_probe_host("v6.example") == ("2606:4700:4700::1111",)
    net["dns"]["mapped.example"] = ["::ffff:93.184.216.34"]
    assert ra._assert_public_probe_host("mapped.example") == ("::ffff:93.184.216.34",)

    # R7 复审：NAT64 WKP / 6to4 内嵌【公网】IPv4 同样放行（拨翻译后地址本身）
    net["dns"]["nat64.example"] = ["64:ff9b::5db8:d822"]  # 内嵌 93.184.216.34
    assert ra._assert_public_probe_host("nat64.example") == ("64:ff9b::5db8:d822",)
    net["dns"]["6to4.example"] = ["2002:5db8:d822::1"]    # 内嵌 93.184.216.34
    assert ra._assert_public_probe_host("6to4.example") == ("2002:5db8:d822::1",)


def test_valid_ipv6_global_image_passes(net):
    """IPv6 全球单播图床 → 放行；pinned IP 即该 v6 地址。"""
    net["dns"]["cdn6.example"] = ["2606:4700:4700::1111"]
    url6 = "https://cdn6.example/a.png"
    _head(net, url6, 200, {"content-type": "image/png"})

    assert ra._probe_brand_image_url(url6, LABEL) is None
    assert [(c["host"], c["pinned_ips"]) for c in net["calls"]] == [
        ("cdn6.example", ("2606:4700:4700::1111",)),
    ]


# ---------------------------------------------------------------------------
# R4 · P1-3：普通 operator 入口 PUT /whitelabel 全链路
# ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, rows):
        self._rows = list(rows)

    def execute(self, query, params=None):
        pass

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _Conn:
    def __init__(self, rows):
        self._cur = _Cur(rows)

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def close(self):
        pass


def _operator_client(monkeypatch, rows):
    monkeypatch.setattr(ra, "get_db", lambda: _Conn(rows))
    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request, call_next):
        request.state.user = {"user_id": 132, "is_admin": False}
        return await call_next(request)

    app.include_router(ra.router)
    return TestClient(app)


_BEFORE_ROW = {
    "user_id": 132, "company_name": "旧名", "logo_url": "/uploads/whitelabel-logos/a/logo_1_x.png",
    "whitelabel_mode": "external_only", "whitelabel_status": "active",
    "unlocked_by_admin": False, "brand_terms_accepted_at": "2026-07-01",
}


def test_operator_put_whitelabel_rebinding_dns_never_dials_private(net, monkeypatch):
    """普通 operator PUT /whitelabel 提交外链 Logo，DNS 校验后漂移为私网：

    保存成功（200），但全程只拨校验固定的公网 IP；私网 IP 零请求。"""
    net["dns"]["cdn.example"] = lambda n: [PUBLIC_IP] if n == 1 else [PRIVATE_IP]
    _head(net, URL, 200, {"content-type": "image/png", "content-length": "1024"})
    after = {**_BEFORE_ROW, "logo_url": URL, "company_logo_url": URL}
    client = _operator_client(monkeypatch, [dict(_BEFORE_ROW), after])

    r = client.put("/api/referral/whitelabel", json={"logo_url": URL})

    assert r.status_code == 200, r.text
    assert net["dns_calls"]["cdn.example"] == 1
    assert _dialed_ips(net) == {PUBLIC_IP}
    assert PRIVATE_IP not in _dialed_ips(net)


def test_operator_put_whitelabel_private_dns_rejected_400(net, monkeypatch):
    """普通 operator 入口：域名首解即私网 → PUT 400 统一人话，零请求发出。"""
    net["dns"]["cdn.example"] = [PRIVATE_IP]
    client = _operator_client(monkeypatch, [])

    r = client.put("/api/referral/whitelabel", json={"logo_url": URL})

    assert r.status_code == 400
    assert UNIFIED_HINT in r.json()["detail"]
    assert net["calls"] == []


def test_operator_put_whitelabel_redirect_rebinding_never_dials_private(net, monkeypatch):
    """operator 入口 302 链：第二跳域名 rebinding → 仍只拨两跳各自校验的公网 IP。"""
    net["dns"]["cdn.example"] = lambda n: [PUBLIC_IP] if n == 1 else [PRIVATE_IP]
    net["dns"]["cdn2.example"] = lambda n: [PUBLIC_IP_2] if n == 1 else ["192.168.1.10"]
    _head(net, URL, 302, {"location": "https://cdn2.example/b.png"})
    _head(net, "https://cdn2.example/b.png", 200, {"content-type": "image/jpeg"})
    after = {**_BEFORE_ROW, "logo_url": URL, "company_logo_url": URL}
    client = _operator_client(monkeypatch, [dict(_BEFORE_ROW), after])

    r = client.put("/api/referral/whitelabel", json={"logo_url": URL})

    assert r.status_code == 200, r.text
    assert _dialed_ips(net) == {PUBLIC_IP, PUBLIC_IP_2}
    assert "192.168.1.10" not in _dialed_ips(net)
    assert PRIVATE_IP not in _dialed_ips(net)


def test_operator_put_whitelabel_valid_external_logo_passes(net, monkeypatch):
    """operator 入口公网合法图片仍通过（回归：修复不得误杀正常外链）。"""
    net["dns"]["cdn.example"] = [PUBLIC_IP]
    _head(net, URL, 200, {"content-type": "image/png", "content-length": "2048"})
    after = {**_BEFORE_ROW, "logo_url": URL, "company_logo_url": URL}
    client = _operator_client(monkeypatch, [dict(_BEFORE_ROW), after])

    r = client.put("/api/referral/whitelabel", json={"logo_url": URL})

    assert r.status_code == 200, r.text
    assert _dialed_ips(net) == {PUBLIC_IP}


def test_operator_put_whitelabel_local_upload_path_still_compatible(net, monkeypatch):
    """本地上传相对路径 /uploads/whitelabel-logos/... 继续兼容（不触发外链探测）。"""
    local_url = "/uploads/whitelabel-logos/bucket9/logo_2_y.png"
    after = {**_BEFORE_ROW, "logo_url": local_url, "company_logo_url": local_url}
    client = _operator_client(monkeypatch, [dict(_BEFORE_ROW), after])

    r = client.put("/api/referral/whitelabel", json={"logo_url": local_url})

    assert r.status_code == 200, r.text
    assert net["calls"] == [] and net["dns_calls"] == {}
