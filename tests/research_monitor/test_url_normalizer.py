"""
URL 归一化函数测试

主要测的是 4 大类归一化场景:
1. 域名前缀(www / m / mobile / wap / 3g)统一去掉
2. 追踪参数(utm_* / fbclid / spm / ref / source / from)统一去掉
3. 锚点 (#xxx) 统一去掉
4. 末尾斜杠统一去掉(根路径除外)
5. 大小写统一为小写(host 部分,path 保留大小写)
"""
import pytest
from services.research_monitor.url_normalizer import normalize_url, compute_url_hash


class TestUrlNormalizer:

    def test_strip_www_prefix(self):
        assert normalize_url("https://www.example.com/p/123") == "https://example.com/p/123"

    def test_strip_m_prefix(self):
        assert normalize_url("https://m.sohu.com/article/456") == "https://sohu.com/article/456"

    def test_strip_mobile_prefix(self):
        assert normalize_url("https://mobile.toutiao.com/x/y") == "https://toutiao.com/x/y"

    def test_strip_utm_params(self):
        assert normalize_url("https://example.com/p?utm_source=ai&utm_medium=cpc") == "https://example.com/p"

    def test_strip_fbclid_param(self):
        assert normalize_url("https://example.com/p?fbclid=ABC123") == "https://example.com/p"

    def test_keep_meaningful_params(self):
        # id 这种业务参数不能丢
        assert normalize_url("https://example.com/p?id=42&utm_source=x") == "https://example.com/p?id=42"

    def test_strip_anchor(self):
        assert normalize_url("https://example.com/p/123#section-2") == "https://example.com/p/123"

    def test_strip_trailing_slash(self):
        assert normalize_url("https://example.com/p/123/") == "https://example.com/p/123"

    def test_keep_root_slash(self):
        # 根路径的 / 不能去
        assert normalize_url("https://example.com/") == "https://example.com/"

    def test_lowercase_host(self):
        assert normalize_url("https://EXAMPLE.com/Path") == "https://example.com/Path"

    def test_preserve_path_case(self):
        # path 的大小写不动(可能区分大小写)
        assert normalize_url("https://example.com/User/Profile") == "https://example.com/User/Profile"

    def test_complex_real_url(self):
        # 真实场景: 4 个不同 URL 应该归一化成同一个
        urls = [
            "https://www.digiwin.com/p/13752.html",
            "https://m.digiwin.com/p/13752.html",
            "https://digiwin.com/p/13752.html?utm_source=baidu",
            "https://digiwin.com/p/13752.html#footer"
        ]
        normalized = [normalize_url(u) for u in urls]
        assert all(n == "https://digiwin.com/p/13752.html" for n in normalized)

    def test_empty_url(self):
        # 边界场景
        assert normalize_url("") == ""

    def test_invalid_url(self):
        # 不是 URL 的字符串(无 scheme/netloc)返回空字符串让上游过滤
        assert normalize_url("not a url") == ""

    def test_idempotent(self):
        # 归一化后再归一化应该一样
        url = "https://www.example.com/p?utm=x#section"
        once = normalize_url(url)
        twice = normalize_url(once)
        assert once == twice

    def test_javascript_scheme_returns_empty(self):
        # 协议白名单防御: javascript: 应返空字符串
        assert normalize_url("javascript:alert(1)") == ""

    def test_protocol_relative_url_returns_empty(self):
        # 协议相对 URL(没有 scheme)应返空字符串
        assert normalize_url("//example.com/path") == ""

    def test_ftp_scheme_returns_empty(self):
        # FTP 不在白名单
        assert normalize_url("ftp://example.com/file") == ""

    def test_uppercase_query_tracking_stripped(self):
        # 大写 query 参数也要识别为追踪参数
        assert normalize_url("https://x.com/p?UTM_SOURCE=ai") == "https://x.com/p"


class TestComputeUrlHash:

    def test_normal_url_returns_40_char_hex(self):
        h = compute_url_hash("https://example.com/p/123")
        assert len(h) == 40
        assert all(c in '0123456789abcdef' for c in h)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            compute_url_hash("")

    def test_same_url_same_hash(self):
        url = "https://example.com/p/123"
        assert compute_url_hash(url) == compute_url_hash(url)

    def test_different_urls_different_hash(self):
        a = compute_url_hash("https://example.com/p/123")
        b = compute_url_hash("https://example.com/p/456")
        assert a != b
