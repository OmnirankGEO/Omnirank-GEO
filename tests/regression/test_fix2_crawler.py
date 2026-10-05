"""
Regression tests for GEO fix batch 2 · services/research_monitor/crawler.py

GEO-R1-CAN-123 (P2, SSRF): _check_robots_txt / should_pre_filter_url 会对不可信的
provider citation cite_url 直接发出站请求,缺失解析地址校验 → 可被引导访问
loopback / RFC1918 私网 / 169.254.169.254 云元数据等内网地址(SSRF)。

修复:在会真正发出站请求的 check_robots 路径上,解析 host 并拒绝任何非公网地址
(fail-closed),robots 抓取的重定向亦经 SSRF 校验。

测试策略:
- source-inspection 判别锁(读源码断言修复标志,回退则失败)。
- 附行为断言:monkeypatch DNS 解析,验证内网地址被拦、且不发 urlopen。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import services.research_monitor.crawler as crawler  # noqa: E402
from services.research_monitor.crawler import (  # noqa: E402
    should_pre_filter_url,
    PreFilterReason,
    _is_safe_public_host,
)

CRAWLER_SRC = Path(crawler.__file__).read_text(encoding="utf-8")


# ---------- source-inspection 判别锁 ----------

def test_source_has_fix_marker():
    """回退(移除修复)则失败:GEO-R1-CAN-123 注释标志必须存在。"""
    assert "[GEO-R1-CAN-123]" in CRAWLER_SRC


def test_source_defines_ssrf_helpers():
    assert "_is_safe_public_host" in CRAWLER_SRC
    assert "def _resolve_host_ips" in CRAWLER_SRC
    assert "PRIVATE_ADDRESS" in CRAWLER_SRC


def test_source_ssrf_gate_before_robots_fetch():
    """should_pre_filter_url 的 check_robots 分支必须先 SSRF 校验再调 _check_robots_txt。"""
    m = re.search(r"if check_robots:(.*?)return None", CRAWLER_SRC, re.DOTALL)
    assert m, "check_robots gate block not found"
    block = m.group(1)
    gate_idx = block.index("_is_safe_public_host")
    robots_idx = block.index("_check_robots_txt")
    assert gate_idx < robots_idx, "SSRF gate must precede robots fetch"
    assert "PRIVATE_ADDRESS" in block


def test_robots_fetch_uses_ssrf_safe_opener():
    """robots 抓取不再裸 urlopen,改走带重定向校验的 opener。

    [2026-07-16 Stage2 并发预筛 · 源码锁随语义载体迁移] 出站与"出站前二次校验
    host"从 _check_robots_txt 下沉到 _fetch_robots_parser(线程安全缓存的唯一
    真实出站点)。语义原样: 校验必须先于 opener 出站。
    """
    assert "_SSRF_SAFE_OPENER" in CRAWLER_SRC
    assert "_SSRFSafeRedirectHandler" in CRAWLER_SRC
    # 唯一真实出站点 = _fetch_robots_parser: 校验先于出站
    m = re.search(
        r"def _fetch_robots_parser.*?(?=\ndef )", CRAWLER_SRC, re.DOTALL,
    )
    assert m, "_fetch_robots_parser not found"
    body = m.group(0)
    assert "_is_safe_public_host" in body, "出站前必须二次校验 host"
    assert "_SSRF_SAFE_OPENER.open" in body
    assert body.index("_is_safe_public_host") < body.index("_SSRF_SAFE_OPENER.open"), (
        "SSRF host 校验必须先于 robots 出站"
    )
    # 判定入口 _check_robots_txt 仍存在且经缓存层取 parser(不自行裸出站)
    m2 = re.search(r"def _check_robots_txt.*?return not rp\.can_fetch", CRAWLER_SRC, re.DOTALL)
    assert m2 and "_get_robots_parser_cached" in m2.group(0)


# ---------- 行为断言(monkeypatch DNS,不依赖真实网络/DB) ----------

def _patch_dns(monkeypatch, ip: str):
    monkeypatch.setattr(
        crawler.socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", (ip, 0))],
    )


def test_loopback_host_rejected(monkeypatch):
    _patch_dns(monkeypatch, "127.0.0.1")
    # 若被引导发 urlopen,直接炸(证明未拦截)
    monkeypatch.setattr(
        crawler, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("urlopen must not be called for internal host")),
    )
    reason = should_pre_filter_url("http://evil.example/x", check_robots=True)
    assert reason == PreFilterReason.PRIVATE_ADDRESS


def test_metadata_ip_rejected(monkeypatch):
    _patch_dns(monkeypatch, "169.254.169.254")
    assert should_pre_filter_url("http://meta.example/latest", check_robots=True) == PreFilterReason.PRIVATE_ADDRESS


def test_rfc1918_rejected(monkeypatch):
    _patch_dns(monkeypatch, "10.1.2.3")
    assert should_pre_filter_url("http://intranet.example/a", check_robots=True) == PreFilterReason.PRIVATE_ADDRESS
    assert not _is_safe_public_host("intranet.example")


def test_ipv4_mapped_loopback_rejected(monkeypatch):
    _patch_dns(monkeypatch, "::ffff:127.0.0.1")
    assert not _is_safe_public_host("mapped.example")


def test_dns_failure_is_fail_closed(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no such host")
    monkeypatch.setattr(crawler.socket, "getaddrinfo", _boom)
    assert _is_safe_public_host("nope.example") is False


def test_public_host_passes_ssrf_gate(monkeypatch):
    _patch_dns(monkeypatch, "93.184.216.34")  # example.com public
    # 隔离掉真实 robots 抓取,只验 SSRF 闸放行
    monkeypatch.setattr(crawler, "_check_robots_txt", lambda url: False)
    assert should_pre_filter_url("https://good.example/article/1", check_robots=True) is None


def test_non_robots_path_unchanged(monkeypatch):
    """check_robots=False 路径不做 DNS 解析,行为与旧版一致(不误伤)。"""
    def _boom(*a, **k):
        raise AssertionError("getaddrinfo must not run on non-robots path")
    monkeypatch.setattr(crawler.socket, "getaddrinfo", _boom)
    assert should_pre_filter_url("https://example.com/report.pdf") == PreFilterReason.UNSUPPORTED_EXTENSION
    assert should_pre_filter_url("https://example.com/article/1") is None
