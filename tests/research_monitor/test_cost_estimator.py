"""
跑批成本估算测试

输入: 行业数 / prompts 数 / 平台数 / 历史 url 引用率(从 DB 算)
输出: 预估元 + 预估各项分解
"""
import pytest
from services.research_monitor.cost_estimator import estimate_round_cost


class TestCostEstimator:

    def test_basic_estimation(self):
        result = estimate_round_cost(
            industry_count=17,
            prompts_per_industry=25,
            platforms_count=4,
            estimated_url_per_call=2.0
        )
        assert 'total_yuan' in result
        assert 'breakdown' in result
        assert 'ai_fetch_yuan' in result['breakdown']
        assert 'jina_crawl_yuan' in result['breakdown']
        assert 'llm_clean_yuan' in result['breakdown']
        assert 'llm_score_yuan' in result['breakdown']

    def test_total_in_range(self):
        # 17 行业 × 25 prompts × 4 平台 = 1700 calls,成本应落在一个合理区间(开源版单价为示例值)
        result = estimate_round_cost(17, 25, 4, 2.0)
        assert 50 <= result['total_yuan'] <= 350

    def test_zero_industries_zero_cost(self):
        result = estimate_round_cost(0, 25, 4, 2.0)
        assert result['total_yuan'] == 0

    def test_breakdown_sums_to_total(self):
        result = estimate_round_cost(17, 25, 4, 2.0)
        breakdown_sum = sum(result['breakdown'].values())
        assert abs(breakdown_sum - result['total_yuan']) < 0.5

    def test_more_prompts_more_cost(self):
        a = estimate_round_cost(17, 10, 4, 2.0)
        b = estimate_round_cost(17, 25, 4, 2.0)
        assert b['total_yuan'] > a['total_yuan']

    def test_more_platforms_more_cost(self):
        a = estimate_round_cost(17, 25, 2, 2.0)
        b = estimate_round_cost(17, 25, 4, 2.0)
        assert b['total_yuan'] > a['total_yuan']

    def test_negative_input_raises(self):
        # 负数应 raise ValueError
        with pytest.raises(ValueError):
            estimate_round_cost(-1, 10, 4, 2.0)
        with pytest.raises(ValueError):
            estimate_round_cost(10, -1, 4, 2.0)

    def test_none_input_raises(self):
        # None 入参应 raise ValueError(防上游 None TypeError 崩)
        with pytest.raises(ValueError):
            estimate_round_cost(None, 10, 4, 2.0)
        with pytest.raises(ValueError):
            estimate_round_cost(10, None, 4, 2.0)

    def test_huge_values_no_overflow(self):
        # 极大值不会 overflow
        result = estimate_round_cost(100, 100, 4, 2.0)
        assert result['total_yuan'] > 0
        assert isinstance(result['total_yuan'], (int, float))
        assert result['estimated_calls'] == 100 * 100 * 4
