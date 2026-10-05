"""带重试的平台调用测试"""
import pytest
import asyncio
from services.research_monitor.platforms import query_with_retry


class TestQueryWithRetry:

    @pytest.mark.asyncio
    async def test_first_attempt_success(self):
        """第 1 次就成功,不重试"""
        call_count = 0
        async def fetcher(prompt_id, prompt):
            nonlocal call_count
            call_count += 1
            return {'platform': 'test', 'ok': True, 'citations': [], 'answer': 'OK'}

        result = await query_with_retry(fetcher, prompt_id=1, prompt="test", max_retries=3, backoff_seconds=(0, 0, 0))
        assert result['ok'] is True
        assert call_count == 1  # 只调一次

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self):
        """第 1 次 RuntimeError,第 2 次成功"""
        call_count = 0
        async def flaky_fetcher(prompt_id, prompt):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("fake fail")
            return {'platform': 'test', 'ok': True, 'citations': [], 'answer': 'OK'}

        result = await query_with_retry(flaky_fetcher, prompt_id=1, prompt="test", max_retries=3, backoff_seconds=(0, 0, 0))
        assert result['ok'] is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted(self):
        """3 次重试都失败 → 抛 RuntimeError"""
        call_count = 0
        async def always_fail(prompt_id, prompt):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("always fail")

        with pytest.raises(RuntimeError, match="重试 3 次"):
            await query_with_retry(always_fail, prompt_id=1, prompt="test", max_retries=3, backoff_seconds=(0, 0, 0))
        assert call_count == 3  # 重试了 3 次

    @pytest.mark.asyncio
    async def test_no_retry_on_value_error(self):
        """ValueError(配置错误)不重试,直接抛"""
        call_count = 0
        async def bad_request(prompt_id, prompt):
            nonlocal call_count
            call_count += 1
            raise ValueError("invalid prompt")

        with pytest.raises(ValueError):
            await query_with_retry(bad_request, prompt_id=1, prompt="test", max_retries=3, backoff_seconds=(0, 0, 0))
        assert call_count == 1  # 只调一次,不重试

    @pytest.mark.asyncio
    async def test_no_retry_on_type_error(self):
        """TypeError 不重试"""
        async def bad_type(prompt_id, prompt):
            raise TypeError("wrong type")

        with pytest.raises(TypeError):
            await query_with_retry(bad_type, prompt_id=1, prompt="test", max_retries=3, backoff_seconds=(0, 0, 0))

    @pytest.mark.asyncio
    async def test_retry_on_httpx_error(self):
        """httpx.HTTPError 重试"""
        import httpx
        call_count = 0
        async def http_fail(prompt_id, prompt):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise httpx.ConnectError("connection refused")
            return {'platform': 'test', 'ok': True, 'citations': [], 'answer': 'OK'}

        result = await query_with_retry(http_fail, prompt_id=1, prompt="test", max_retries=3, backoff_seconds=(0, 0, 0))
        assert result['ok'] is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_timeout(self):
        """asyncio.TimeoutError 重试"""
        call_count = 0
        async def timeout_first(prompt_id, prompt):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise asyncio.TimeoutError("timeout")
            return {'platform': 'test', 'ok': True, 'citations': [], 'answer': 'OK'}

        result = await query_with_retry(timeout_first, prompt_id=1, prompt="test", max_retries=3, backoff_seconds=(0, 0, 0))
        assert result['ok'] is True
        assert call_count == 2
