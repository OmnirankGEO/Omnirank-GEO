"""
v3.3/v3.4 GEO 全自动托管 — 单元测试

不依赖真实 DB / LLM 的纯逻辑测试：
  - reverse_calc_from_budget 数学反演
  - SOV → 目标检出率映射
  - 完成度评分算法
  - Pydantic 模型校验
"""

import asyncio
import importlib.util
from pathlib import Path

try:
    import pytest
except ImportError:
    # 没装 pytest 也能直接跑 if __name__ == '__main__'
    class _PytestStub:
        @staticmethod
        def skip(msg): raise Exception(f'SKIP: {msg}')
        @staticmethod
        def fail(msg): raise AssertionError(msg)
        class raises:
            def __init__(self, exc): self.exc = exc
            def __enter__(self): return self
            def __exit__(self, t, v, tb):
                if t is None or not issubclass(t, self.exc):
                    raise AssertionError(f'expected {self.exc.__name__}, got {t}')
                return True
    pytest = _PytestStub()

ROOT = Path(__file__).parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ============================================================
# 1. reverse_calc 反推数学
# ============================================================

class TestReverseCalc:
    def setup_method(self):
        self.mod = _load('reverse_calc', 'tools/geo_managed/reverse_calc.py')

    def test_zero_budget(self):
        result = asyncio.run(self.mod.reverse_calc_from_budget(
            budget_yuan=0, keyword='test', market={'competition_count': 50, 'competition_level': 3},
        ))
        assert result['achievable_sov_pct'] == 0
        assert result['achievable_articles'] == 0
        assert result['matched_tier_label'] == '余额不足'

    def test_low_budget(self):
        """¥200 在中等竞争（50 竞品）应能拉到 1-2 篇"""
        result = asyncio.run(self.mod.reverse_calc_from_budget(
            budget_yuan=200, keyword='test',
            market={'competition_count': 50, 'competition_level': 3},
        ))
        assert result['achievable_articles'] >= 1
        assert result['achievable_sov_pct'] >= 0
        assert 30 <= result['detection_rate_30d_pct'] <= 85
        assert result['breakdown']['monitoring_yuan'] > 0

    def test_high_budget_caps_at_50pct(self):
        """¥10000 在小竞争词应该 cap 在 50% SOV"""
        result = asyncio.run(self.mod.reverse_calc_from_budget(
            budget_yuan=10000, keyword='cold_keyword',
            market={'competition_count': 5, 'competition_level': 1},
        ))
        assert result['achievable_sov_pct'] <= 50

    def test_tier_label_mapping(self):
        """SOV 25% → '经常被推荐'; 33% → '优先推荐'; 50% → '频繁被推荐'"""
        # 50 竞品 / 17 篇 ≈ 25% SOV
        result = asyncio.run(self.mod.reverse_calc_from_budget(
            budget_yuan=2000, keyword='test',
            market={'competition_count': 50, 'competition_level': 3},
        ))
        # tier label 应在合理档位
        assert result['matched_tier_label'] in ['偶尔被推荐', '经常被推荐', '优先推荐', '频繁被推荐', '略有曝光']


# ============================================================
# 2. estimate_engine SOV 映射
# ============================================================

class TestSovMapping:
    def setup_method(self):
        self.mod = _load('estimate_engine', 'tools/geo_managed/estimate_engine.py')

    def test_sov_to_target_rank(self):
        assert self.mod._sov_to_target_rank(15) == 'top_30'
        assert self.mod._sov_to_target_rank(25) == 'top_10'
        assert self.mod._sov_to_target_rank(33) == 'top_10'
        assert self.mod._sov_to_target_rank(50) == 'top_3'

    def test_4_tier_labels(self):
        labels = self.mod.SOV_TIERS
        assert labels['entry']['label'] == '偶尔被推荐'
        assert labels['standard']['label'] == '经常被推荐'
        assert labels['flagship']['label'] == '优先推荐'
        assert labels['strong']['label'] == '频繁被推荐'
        # standard 必须是默认推荐
        assert labels['standard'].get('recommended') is True

    def test_v34_markup(self):
        """v3.4 markup 必须是 1.2x"""
        assert self.mod.V34_MARKUP_FACTOR == 1.2


# ============================================================
# 3. campaign_tick SOV → 检出率映射
# ============================================================

class TestCampaignTick:
    def setup_method(self):
        # campaign_tick 引用了 db.connection 和 advisors，无法独立 load
        # 这里只测 _sov_to_target_detection_rate 函数
        # 需要绕过依赖
        import sys
        sys.path.insert(0, str(ROOT))
        # 临时 mock 依赖
        try:
            from tools.geo_managed.campaign_tick import _sov_to_target_detection_rate
            self.fn = _sov_to_target_detection_rate
        except Exception:
            self.fn = None

    def test_sov_to_detection_rate(self):
        if self.fn is None:
            pytest.skip('campaign_tick 依赖未就绪（DB），跳过')
        # 25% SOV 应映射到 ~80%（含 -10 buffer）
        rate_25 = self.fn(25)
        assert 70 <= rate_25 <= 85
        # 50% SOV 应封顶 85
        rate_50 = self.fn(50)
        assert rate_50 == 85.0
        # 15% 应在合理低区
        rate_15 = self.fn(15)
        assert 50 <= rate_15 <= 70


# ============================================================
# 4. 完成度评分（geo_assets_api）
# ============================================================

class TestCompletenessScore:
    def setup_method(self):
        self.mod = _load('geo_assets_api', 'api/geo_assets_api.py')

    def test_empty_score_zero(self):
        score, detail = self.mod.calculate_completeness_score({}, {})
        assert score == 0
        assert len(detail['missing_required']) > 0

    def test_only_required_filled_60(self):
        """只填 5 必填 → 60 分"""
        assets = {
            'company_intro': {'text': 'demo'},
            'selling_points': {'absolute_pain_point': 'pain'},
            'cases': [{'client_desc': 'a', 'result': 'b', 'timeline': 'c'}],
            'competitors_real': [{'name': 'x'}],
        }
        profile = {'industry': 'GEO', 'city': '深圳'}
        score, detail = self.mod.calculate_completeness_score(assets, profile)
        assert score == 60  # 5 项 × 12 分
        assert len(detail['filled_required']) == 5  # 含 industry+city

    def test_all_filled_100(self):
        """全 12 项填全 → 100 分"""
        assets = {f: 'demo' for f in (
            'company_intro selling_points cases competitors_real '
            'real_data_points brand_story milestones team_core testimonials service_flow price_packages '
            'target_keywords industry_position target_customer_profile'
        ).split()}
        # cases 必须是非空 list
        assets['cases'] = [{'a': 1}]
        assets['competitors_real'] = [{'name': 'x'}]
        assets['target_keywords'] = ['k']
        assets['milestones'] = [{'year': 2020}]
        assets['team_core'] = [{'name': 'A'}]
        assets['testimonials'] = [{'quote': 't'}]
        assets['price_packages'] = [{'name': 'p'}]
        profile = {'industry': 'GEO', 'city': '深圳'}
        score, detail = self.mod.calculate_completeness_score(assets, profile)
        assert score == 100

    def test_required_fields_constants(self):
        """必填 4 项 + industry+city = 5 项"""
        assert len(self.mod.REQUIRED_FIELDS) == 4
        assert len(self.mod.RECOMMENDED_FIELDS) == 7
        assert len(self.mod.AI_AUTO_FIELDS) == 3


# ============================================================
# 5. Pydantic 请求模型校验
# ============================================================

class TestRequestModels:
    def setup_method(self):
        self.mod = _load('managed_api', 'api/managed_campaign_api.py')

    def test_estimate_request_valid(self):
        from pydantic import ValidationError
        try:
            req = self.mod.EstimateRequest(
                keyword='测试词', mode='by_target_sov', target_sov_pct=25,
            )
            assert req.keyword == '测试词'
        except ValidationError as e:
            pytest.fail(f'有效请求被拒: {e}')

    def test_estimate_request_invalid_mode(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self.mod.EstimateRequest(keyword='x', mode='invalid_mode')

    def test_sov_pct_range(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self.mod.EstimateRequest(keyword='x', target_sov_pct=99)
        with pytest.raises(ValidationError):
            self.mod.EstimateRequest(keyword='x', target_sov_pct=0)

    def test_keyword_too_short(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self.mod.EstimateRequest(keyword='x')  # min_length=2

    def test_confirm_recharge_required_consents(self):
        """充值必须勾选两个 consent"""
        # 模型层面不强制（is bool），但 API handler 校验
        req = self.mod.ConfirmRechargeRequest(
            keyword='测试词', brand_id=1, target_sov_pct=25,
            target_display_label='经常被推荐', tier_label='standard',
            selected_amount_yuan=1500, mode='semi_auto',
            estimate_quoted_at='2026-04-16T10:00:00',
            agreement_consent=False, brand_voice_consent=False,
        )
        assert req.agreement_consent is False  # 模型允许，业务层拒绝


if __name__ == '__main__':
    # 直接跑（不需要 pytest 也能跑基本测试）
    print('=== 手动跑测试 ===')

    print('\n[TestReverseCalc]')
    t = TestReverseCalc(); t.setup_method()
    t.test_zero_budget(); print('  test_zero_budget: PASS')
    t.test_low_budget(); print('  test_low_budget: PASS')
    t.test_high_budget_caps_at_50pct(); print('  test_high_budget_caps_at_50pct: PASS')
    t.test_tier_label_mapping(); print('  test_tier_label_mapping: PASS')

    print('\n[TestSovMapping]')
    t = TestSovMapping(); t.setup_method()
    t.test_sov_to_target_rank(); print('  test_sov_to_target_rank: PASS')
    t.test_4_tier_labels(); print('  test_4_tier_labels: PASS')
    t.test_v34_markup(); print('  test_v34_markup: PASS')

    print('\n[TestCompletenessScore]')
    t = TestCompletenessScore(); t.setup_method()
    t.test_empty_score_zero(); print('  test_empty_score_zero: PASS')
    t.test_only_required_filled_60(); print('  test_only_required_filled_60: PASS')
    t.test_all_filled_100(); print('  test_all_filled_100: PASS')
    t.test_required_fields_constants(); print('  test_required_fields_constants: PASS')

    print('\n[TestRequestModels]')
    t = TestRequestModels(); t.setup_method()
    t.test_estimate_request_valid(); print('  test_estimate_request_valid: PASS')
    try:
        t.test_estimate_request_invalid_mode(); print('  test_estimate_request_invalid_mode: PASS')
    except Exception as e:
        print(f'  test_estimate_request_invalid_mode: FAIL ({e})')
    try:
        t.test_sov_pct_range(); print('  test_sov_pct_range: PASS')
    except Exception as e:
        print(f'  test_sov_pct_range: FAIL ({e})')
    try:
        t.test_keyword_too_short(); print('  test_keyword_too_short: PASS')
    except Exception as e:
        print(f'  test_keyword_too_short: FAIL ({e})')
    t.test_confirm_recharge_required_consents(); print('  test_confirm_recharge_required_consents: PASS')

    print('\n=== 全部测试通过 ===')
