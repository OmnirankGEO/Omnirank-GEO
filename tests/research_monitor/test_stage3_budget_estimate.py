"""
P14.4 C2 (2026-06) · stage 3 预算预估必须乘 JINA_RETRY_MAX_ATTEMPTS
之前 estimated_increment_yuan=len(urls) * COST_PER_JINA_CRAWL 假设 1 attempt
P14.3 加 retry 后 worst case = len * COST * MAX_ATTEMPTS · 否则预算保护偏低

锁:
  1. (inspect) _run_pipeline 源码 stage 3 预算调用必须含 * JINA_RETRY_MAX_ATTEMPTS
  2. (inspect) 不再有裸 len(urls) * COST_PER_JINA_CRAWL 不含 attempts 乘子
"""
from __future__ import annotations

import inspect
import re


class TestStage3BudgetEstimateIncludesRetryAttempts:

    def test_stage3_estimate_multiplies_max_attempts(self):
        from services.research_monitor import round_runner
        src = inspect.getsource(round_runner._run_pipeline)
        # stage 3 调用形如:
        # estimated_increment_yuan=len(urls) * COST_PER_JINA_CRAWL * JINA_RETRY_MAX_ATTEMPTS,
        pattern = re.compile(
            r"estimated_increment_yuan\s*=\s*len\(urls\)\s*\*\s*COST_PER_JINA_CRAWL\s*\*\s*JINA_RETRY_MAX_ATTEMPTS",
        )
        assert pattern.search(src), (
            "stage 3 预算预估必须乘 JINA_RETRY_MAX_ATTEMPTS · "
            "否则 retry 全打满时会超单轮预算才发现"
        )

    def test_no_bare_jina_estimate_without_attempts(self):
        """防回退 · 不允许出现裸 len(urls) * COST_PER_JINA_CRAWL (不含 attempts)"""
        from services.research_monitor import round_runner
        src = inspect.getsource(round_runner._run_pipeline)
        # 剥离 Python 注释行防误报(注释里写 "之前 len(urls) * COST..." 这种)
        non_comment_src = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        bare = re.findall(
            r"len\(urls\)\s*\*\s*COST_PER_JINA_CRAWL(?!\s*\*\s*JINA_RETRY_MAX_ATTEMPTS)",
            non_comment_src,
        )
        assert not bare, (
            f"_run_pipeline 不应再有裸 len(urls)*COST_PER_JINA_CRAWL "
            f"(不含 *JINA_RETRY_MAX_ATTEMPTS) · 找到 {len(bare)} 处"
        )

    def test_jina_retry_max_attempts_used_in_estimate_expression(self):
        """import 路径锁:JINA_RETRY_MAX_ATTEMPTS 必须是 module 级常量(不是局部变量)"""
        from services.research_monitor.round_runner import JINA_RETRY_MAX_ATTEMPTS
        assert isinstance(JINA_RETRY_MAX_ATTEMPTS, int)
        assert JINA_RETRY_MAX_ATTEMPTS >= 1
