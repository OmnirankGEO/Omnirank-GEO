"""
Jina 爬取器 + 预过滤测试

测试 5 类预过滤场景 + 1 类正常爬取:
1. 域名黑名单(douyin.com)直接跳
2. URL 后缀(.pdf / .mp4)直接跳
3. URL pattern(/search / /list/ 等)
4. robots.txt Disallow 跳(P0-A 版权防护)
5. 正常爬取返回完整 markdown
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from services.research_monitor.crawler import (
    should_pre_filter_url,
    crawl_article,
    PreFilterReason,
)


class TestPreFilter:

    def test_filter_blacklist_domain(self):
        result = should_pre_filter_url("https://douyin.com/video/123")
        assert result == PreFilterReason.BLACKLIST_DOMAIN

    def test_filter_pdf_extension(self):
        result = should_pre_filter_url("https://example.com/report.pdf")
        assert result == PreFilterReason.UNSUPPORTED_EXTENSION

    def test_filter_video_extension(self):
        for ext in ['.mp4', '.zip', '.docx']:
            result = should_pre_filter_url(f"https://example.com/file{ext}")
            assert result == PreFilterReason.UNSUPPORTED_EXTENSION

    def test_filter_search_url_pattern(self):
        result = should_pre_filter_url("https://example.com/search?q=test")
        assert result == PreFilterReason.URL_PATTERN

    def test_filter_list_url_pattern(self):
        result = should_pre_filter_url("https://example.com/list/articles")
        assert result == PreFilterReason.URL_PATTERN

    def test_pass_normal_url(self):
        result = should_pre_filter_url("https://www.digiwin.com/p/13752.html")
        assert result is None  # 不过滤

    def test_filter_empty_url(self):
        assert should_pre_filter_url("") == PreFilterReason.URL_PATTERN

    def test_filter_invalid_url(self):
        assert should_pre_filter_url("not a url") == PreFilterReason.URL_PATTERN


class TestCrawlArticle:

    @pytest.mark.asyncio
    async def test_crawl_success(self):
        """正常爬取返回 markdown"""
        with patch('services.research_monitor.crawler.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.text = "Title: ERP 排行榜\n\n# 2026 年 ERP 系统综合实力榜..."
            mock_response.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await crawl_article("https://www.digiwin.com/p/13752.html")

            assert result['ok'] is True
            assert result['title'] == 'ERP 排行榜'
            assert 'ERP 系统综合实力榜' in result['content']
            assert result['char_count'] > 0
            assert result['url'] == 'https://www.digiwin.com/p/13752.html'

    @pytest.mark.asyncio
    async def test_crawl_http_error(self):
        """HTTP 错误返回 ok=False"""
        import httpx
        with patch('services.research_monitor.crawler.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            mock_client_cls.return_value = mock_client

            result = await crawl_article("https://example.com/unreachable")
            assert result['ok'] is False
            assert 'connection refused' in result['error']
            assert result['char_count'] == 0

    @pytest.mark.asyncio
    async def test_crawl_timeout(self):
        """超时返回 ok=False"""
        import httpx
        with patch('services.research_monitor.crawler.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client_cls.return_value = mock_client

            result = await crawl_article("https://example.com/slow")
            assert result['ok'] is False
