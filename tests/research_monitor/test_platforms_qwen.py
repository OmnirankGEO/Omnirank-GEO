"""Qwen DashScope 原生 API 抓取器测试"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from services.research_monitor.platforms import query_qwen


@pytest.fixture(autouse=True)
def _set_dashscope_env(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-dashscope-key')


def _make_mock_response(json_data):
    mock_response = MagicMock()
    mock_response.json.return_value = json_data
    mock_response.raise_for_status = MagicMock()
    return mock_response


@pytest.fixture
def successful_response():
    return {
        "output": {
            "choices": [{
                "message": {"content": "ERP 系统选型推荐..."}
            }],
            "search_info": {
                "search_results": [
                    {"url": "https://x.com/p/1", "title": "X 文章", "index": 1},
                    {"url": "https://y.com/p/2", "title": "Y 文章", "index": 2}
                ]
            }
        }
    }


class TestQwenPlatform:

    @pytest.mark.asyncio
    async def test_qwen_success(self, successful_response):
        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_make_mock_response(successful_response))
            mock_client_cls.return_value = mock_client

            result = await query_qwen(prompt_id=1, prompt="ERP 哪个好")
            assert result['platform'] == 'qwen'
            assert result['ok'] is True
            assert 'ERP 系统选型推荐' in result['answer']
            assert len(result['citations']) == 2
            assert result['citations'][0]['url'] == 'https://x.com/p/1'

    @pytest.mark.asyncio
    async def test_qwen_no_search_results(self):
        empty_response = {
            "output": {
                "choices": [{"message": {"content": "ERP 简介..."}}],
                "search_info": {}
            }
        }
        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_make_mock_response(empty_response))
            mock_client_cls.return_value = mock_client

            result = await query_qwen(prompt_id=1, prompt="生僻问题")
            assert result['ok'] is True
            assert result['citations'] == []

    @pytest.mark.asyncio
    async def test_qwen_text_field_fallback(self):
        # 旧版 DashScope 返回 output.text 不是 output.choices[0].message.content
        old_format = {
            "output": {
                "text": "ERP 简介旧格式",
                "search_info": {"search_results": []}
            }
        }
        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_make_mock_response(old_format))
            mock_client_cls.return_value = mock_client

            result = await query_qwen(prompt_id=1, prompt="测试")
            assert 'ERP 简介旧格式' in result['answer']

    @pytest.mark.asyncio
    async def test_qwen_dedup_urls(self):
        # 重复 URL 只算一次
        dup_response = {
            "output": {
                "choices": [{"message": {"content": "..."}}],
                "search_info": {
                    "search_results": [
                        {"url": "https://x.com/p/1", "title": "X", "index": 1},
                        {"url": "https://x.com/p/1", "title": "X duplicate", "index": 2}
                    ]
                }
            }
        }
        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_make_mock_response(dup_response))
            mock_client_cls.return_value = mock_client

            result = await query_qwen(prompt_id=1, prompt="测试")
            assert len(result['citations']) == 1

    @pytest.mark.asyncio
    async def test_qwen_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
        with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
            await query_qwen(prompt_id=1, prompt="测试")
