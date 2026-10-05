"""
域名信誉分级测试

3 档分级:
- whitelist (白名单): 头部可信媒体,文章评分 >=80 自动 approve(后期演进,本期不实现 auto-approve,只标识)
- gray (灰名单, 默认): 普通 SEO 站点 / 行业站
- blacklist (黑名单): SEO 农场 / 抖音短回答 / 已知垃圾站点
"""
import pytest
from services.research_monitor.domain_tiering import get_domain_tier, is_blacklisted


# 纯单元测试,无需 DB。覆盖 conftest 的 autouse DB fixture 为 no-op。
@pytest.fixture(autouse=True)
def clean_research_tables():
    yield


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    yield


class TestDomainTiering:

    def test_whitelist_36kr(self):
        assert get_domain_tier("36kr.com") == "whitelist"

    def test_whitelist_caixin(self):
        assert get_domain_tier("caixin.com") == "whitelist"

    def test_blacklist_douyin(self):
        assert get_domain_tier("douyin.com") == "blacklist"
        assert is_blacklisted("douyin.com") is True

    def test_blacklist_xiaohongshu(self):
        assert get_domain_tier("xiaohongshu.com") == "blacklist"

    def test_blacklist_b_station(self):
        assert get_domain_tier("bilibili.com") == "blacklist"

    def test_gray_default(self):
        assert get_domain_tier("some-random-blog.com") == "gray"
        assert is_blacklisted("some-random-blog.com") is False

    def test_handles_subdomain(self):
        assert get_domain_tier("www.36kr.com") == "whitelist"
        assert get_domain_tier("m.douyin.com") == "blacklist"

    def test_handles_uppercase(self):
        assert get_domain_tier("36KR.COM") == "whitelist"

    def test_empty_domain(self):
        assert get_domain_tier("") == "gray"
        assert is_blacklisted("") is False

    def test_url_pattern_blacklist(self):
        """URL 形状黑名单 - 短链域名"""
        assert get_domain_tier("t.cn") == "blacklist"
        assert get_domain_tier("dwz.cn") == "blacklist"

    def test_handles_host_with_port(self):
        # host:port 形态应能识别白名单
        assert get_domain_tier("36KR.COM:8080") == "whitelist"

    def test_handles_host_with_path(self):
        # host/path 形态应能识别白名单
        assert get_domain_tier("36kr.com/some/path") == "whitelist"
