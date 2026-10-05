"""
判别性回归测试 · services/research_monitor/platforms.py 的 2 条 GEO 修复。

主形态 = source-inspection 判别锁(读源码文本断言修复标志存在,回退修复则断言失败)。
不依赖 DB / 不 import server.py。附一条 query_with_retry 纯行为单测(异步)。

- GEO-R2-CAN-029: is_search_failure 空答案+无引用 判失败(return True),不再静默当成功。
- GEO-R6-CAN-006: query_with_retry 在返回体上回报真实 attempts 次数。
"""
import asyncio
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PLATFORMS_SRC_PATH = ROOT / "services" / "research_monitor" / "platforms.py"
SRC = PLATFORMS_SRC_PATH.read_text(encoding="utf-8")


def _slice(src: str, start_marker: str, length: int = 1200) -> str:
    idx = src.find(start_marker)
    assert idx != -1, f"marker not found: {start_marker!r}"
    return src[idx: idx + length]


# ==================== GEO-R2-CAN-029 ====================

def test_can029_empty_answer_no_citation_is_failure_source():
    """is_search_failure 区域内:空答案分支必须 return True(而非旧的 return False)。"""
    region = _slice(SRC, "def is_search_failure(")
    # 修复标志:GEO-R2-CAN-029 注释 + 空答案分支 return True
    assert "GEO-R2-CAN-029" in region, "缺少 GEO-R2-CAN-029 修复标记"
    m = re.search(r"if not answer:\s*\n\s*return\s+(\w+)", region)
    assert m is not None, "未找到 `if not answer:` 分支"
    assert m.group(1) == "True", (
        "回退检测:空答案+无引用应判失败(return True),"
        f"实际 return {m.group(1)}"
    )


def test_can029_is_search_failure_behavior():
    """纯行为:导入 is_search_failure,空答案+无引用 应返回 True。"""
    from services.research_monitor.platforms import is_search_failure
    # 空答案 + 无引用 → 失败
    assert is_search_failure("", []) is True
    assert is_search_failure(None, []) is True
    # 有引用 → 非失败(向后兼容)
    assert is_search_failure("", [{"url": "http://x", "title": "t", "rank": 1}]) is False
    # 有正常答案 + 无引用 + 无降级文案 → 非失败(向后兼容)
    assert is_search_failure("这是一段正常的研究答案内容", []) is False


# ==================== GEO-R6-CAN-006 ====================

def test_can006_query_with_retry_reports_attempts_source():
    """query_with_retry 区域内:必须把真实 attempts 挂到返回体上。"""
    region = _slice(SRC, "async def query_with_retry(", length=2400)
    assert "GEO-R6-CAN-006" in region, "缺少 GEO-R6-CAN-006 修复标记"
    assert 'setdefault("attempts"' in region or '"attempts"' in region, (
        "回退检测:query_with_retry 应在返回体上回报 attempts 次数"
    )
    assert "attempt + 1" in region, "attempts 应基于真实 attempt+1 计数"


def test_can006_query_with_retry_attempts_behavior():
    """纯行为:fetcher 先失败两次再成功,返回体 attempts 应为 3。"""
    from services.research_monitor.platforms import query_with_retry

    calls = {"n": 0}

    async def flaky_fetcher(prompt_id, prompt):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return {"platform": "test", "answer": "ok", "citations": [], "ok": True}

    result = asyncio.run(
        query_with_retry(flaky_fetcher, prompt_id=1, prompt="p", backoff_seconds=(0, 0, 0))
    )
    assert calls["n"] == 3
    assert result["attempts"] == 3, f"应回报真实 attempts=3,实际 {result.get('attempts')}"


def test_can006_first_try_success_attempts_is_one():
    """首次即成功 → attempts=1。"""
    from services.research_monitor.platforms import query_with_retry

    async def ok_fetcher(prompt_id, prompt):
        return {"platform": "test", "ok": True}

    result = asyncio.run(query_with_retry(ok_fetcher, prompt_id=1, prompt="p"))
    assert result["attempts"] == 1
