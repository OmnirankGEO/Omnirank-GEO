"""Kimi DashScope OpenAI 兼容协议 + REFERENCES 解析测试 (A.3.3)"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from services.research_monitor.platforms import query_kimi


@pytest.fixture(autouse=True)
def _set_dashscope_env(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-dashscope-key')


def _make_mock_response(json_data):
    mock_response = MagicMock()
    mock_response.json.return_value = json_data
    mock_response.raise_for_status = MagicMock()
    return mock_response


@pytest.fixture
def kimi_final_response_with_references():
    """Kimi 最终回答含 ---REFERENCES--- 列表"""
    return {
        "choices": [{
            "message": {
                "content": (
                    "ERP 系统选型推荐:\n"
                    "1. 鼎捷数智...\n"
                    "2. 用友...\n\n"
                    "---REFERENCES---\n"
                    "1. [ERP 排行榜 2026](https://www.digiwin.com/p/13752.html)\n"
                    "2. [ERP 解析](https://www.51cto.com/article/837899.html)\n"
                    "3. [中国 ERP TOP10](https://m.sohu.com/a/991906867)"
                )
            },
            "finish_reason": "stop"
        }]
    }


class TestKimiPlatform:

    @pytest.mark.asyncio
    async def test_kimi_single_turn_with_references(self, kimi_final_response_with_references):
        """单轮回答(无 tool_call) + REFERENCES 列表正确解析"""
        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_make_mock_response(kimi_final_response_with_references))
            mock_client_cls.return_value = mock_client

            result = await query_kimi(prompt_id=1, prompt="ERP 系统哪个好")

            assert result['platform'] == 'kimi'
            assert result['ok'] is True
            assert 'ERP 系统选型推荐' in result['answer']
            # answer 不应包含 REFERENCES 部分
            assert '---REFERENCES---' not in result['answer']
            # 3 条引用解析正确
            assert len(result['citations']) == 3
            assert result['citations'][0]['url'] == 'https://www.digiwin.com/p/13752.html'

    @pytest.mark.asyncio
    async def test_kimi_multi_turn_tool_call_loop(self):
        """多轮 tool_call: 第 1 轮 finish=tool_calls, 第 2 轮 finish=stop"""
        # 第 1 轮: 模型调 web_search
        round1_response = {
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_001",
                        "function": {
                            "name": "$web_search",
                            "arguments": '{"query": "ERP 排行"}'
                        }
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        }
        # 第 2 轮: 拿到 tool 结果后给最终回答
        round2_response = {
            "choices": [{
                "message": {
                    "content": (
                        "ERP 推荐 1. 鼎捷 2. 用友\n\n"
                        "---REFERENCES---\n"
                        "1. [鼎捷数智](https://digiwin.com/x)\n"
                    )
                },
                "finish_reason": "stop"
            }]
        }

        responses = [_make_mock_response(round1_response), _make_mock_response(round2_response)]

        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(side_effect=responses)
            mock_client_cls.return_value = mock_client

            result = await query_kimi(prompt_id=1, prompt="ERP")
            assert result['ok'] is True
            assert 'ERP 推荐' in result['answer']
            assert len(result['citations']) == 1
            # 验证调了 2 次 (第 1 轮 + 第 2 轮)
            assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_kimi_no_references_block(self):
        """LLM 没按格式吐 ---REFERENCES--- 列表(失败兜底场景)"""
        bad_response = {
            "choices": [{
                "message": {"content": "ERP 简单介绍...(没引用列表)"},
                "finish_reason": "stop"
            }]
        }
        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_make_mock_response(bad_response))
            mock_client_cls.return_value = mock_client

            result = await query_kimi(prompt_id=1, prompt="测试")
            # 没引用列表 → answer 是全文 + citations 空
            assert result['ok'] is True
            assert 'ERP 简单介绍' in result['answer']
            assert result['citations'] == []

    @pytest.mark.asyncio
    async def test_kimi_max_tool_call_loops(self):
        """C1: 5 轮 tool_call 跑完仍未收敛 → raise RuntimeError 让上层重试,不能静默返空"""
        tool_call_response = {
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_x",
                        "function": {"name": "$web_search", "arguments": "{}"}
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        }
        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_make_mock_response(tool_call_response))
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="未收敛"):
                await query_kimi(prompt_id=1, prompt="测试")
            # 5 轮上限,不死循环
            assert mock_client.post.call_count <= 5

    @pytest.mark.asyncio
    async def test_kimi_empty_final_content_raises(self):
        """C1 兜底: finish=stop 但 content 为空也 raise 让 retry"""
        empty_stop_response = {
            "choices": [{
                "message": {"content": ""},
                "finish_reason": "stop"
            }]
        }
        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_make_mock_response(empty_stop_response))
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="最终回答为空"):
                await query_kimi(prompt_id=1, prompt="测试")

    @pytest.mark.asyncio
    async def test_kimi_references_with_parens_in_title(self):
        """I5: REFERENCES title 含括号(中文/英文)也能正确解析"""
        tricky_response = {
            "choices": [{
                "message": {
                    "content": (
                        "推荐如下:\n"
                        "1. OmniRank\n\n"
                        "---REFERENCES---\n"
                        "1. [OmniRank (官网)](https://example.com/a)\n"
                        "2. [品牌测评 (2026 最新版)](https://example.com/b)\n"
                        "3. [Acme Corp - 旗舰产品 (Pro)](https://example.com/c)\n"
                    )
                },
                "finish_reason": "stop"
            }]
        }
        with patch('services.research_monitor.platforms.httpx.AsyncClient') as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = AsyncMock(return_value=_make_mock_response(tricky_response))
            mock_client_cls.return_value = mock_client

            result = await query_kimi(prompt_id=1, prompt="测试")
            assert result['ok'] is True
            assert len(result['citations']) == 3
            # title 含括号必须完整保留(不被 ) 截断)
            assert result['citations'][0]['title'] == 'OmniRank (官网)'
            assert result['citations'][1]['title'] == '品牌测评 (2026 最新版)'
            assert result['citations'][2]['title'] == 'Acme Corp - 旗舰产品 (Pro)'
            assert result['citations'][0]['url'] == 'https://example.com/a'

    @pytest.mark.asyncio
    async def test_kimi_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
        with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
            await query_kimi(prompt_id=1, prompt="测试")
