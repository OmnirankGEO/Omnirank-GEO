"""
Stage3 URL 质量预算 纯函数判别测试 + ≥10k 压测(2026-07-16 P1 · 禁假绿)

纯函数(不连 DB), 可 --noconftest 跑。覆盖:
- 信号聚合(normalize + count/platforms/min_pos/answer_cited)
- 质量打分(答案引用/权威/rank 单调性)
- 配额选择(总量上限/域名上限/行业覆盖/平台覆盖/确定性/budget<=0)
- ≥10,795 URL 压测: 选中 ≤ 上限 + 配额满足 + 高价值优先保留(覆盖率不降) + ≤1.5h 推算
"""
import hashlib

import pytest

from services.research_monitor.url_budget import (
    UrlSignals,
    BudgetConfig,
    normalize_and_aggregate_signals,
    score_candidate,
    select_url_budget,
    _rank_weight,
    JINA_THROUGHPUT_URL_PER_MIN,
)


def _uh(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


# ==================== 信号聚合 ====================

class TestAggregateSignals:
    def test_aggregates_count_platforms_minpos_answercited(self):
        # 同一 cite_url 多次(不同平台/位置/答案引用)→ 按 normalized_url 聚合
        raw = [
            {'cite_url': 'https://a.com/x', 'engine': 'qwen', 'cite_position': 3, 'is_answer_cited': False},
            {'cite_url': 'https://a.com/x', 'engine': 'kimi', 'cite_position': 1, 'is_answer_cited': True},
            {'cite_url': 'https://a.com/x', 'engine': 'qwen', 'cite_position': 5, 'is_answer_cited': False},
            {'cite_url': 'https://b.com/y', 'engine': 'doubao', 'cite_position': 2, 'is_answer_cited': False},
        ]
        sig = normalize_and_aggregate_signals(raw)
        from services.research_monitor.url_normalizer import normalize_url
        ka = normalize_url('https://a.com/x')
        assert sig[ka].citation_count == 3
        assert sig[ka].platforms == {'qwen', 'kimi'}   # 去重后 2 平台
        assert sig[ka].min_cite_position == 1          # 最靠前
        assert sig[ka].any_answer_cited is True        # 任一为 True
        kb = normalize_url('https://b.com/y')
        assert sig[kb].citation_count == 1
        assert sig[kb].any_answer_cited is False

    def test_empty_and_missing_fields_safe(self):
        raw = [
            {'cite_url': '', 'engine': 'qwen'},
            {'cite_url': 'https://c.com/z'},  # 无 engine/position/answer_cited
        ]
        sig = normalize_and_aggregate_signals(raw)
        from services.research_monitor.url_normalizer import normalize_url
        kc = normalize_url('https://c.com/z')
        assert kc in sig
        assert sig[kc].citation_count == 1
        assert sig[kc].platforms == set()
        assert sig[kc].min_cite_position is None


# ==================== 质量打分 ====================

class TestScoreCandidate:
    def test_answer_cited_boosts_score(self):
        base = UrlSignals(citation_count=1, platforms={'qwen'}, min_cite_position=3, any_answer_cited=False)
        cited = UrlSignals(citation_count=1, platforms={'qwen'}, min_cite_position=3, any_answer_cited=True)
        assert score_candidate('gray', cited) > score_candidate('gray', base)

    def test_whitelist_beats_gray(self):
        s = UrlSignals(citation_count=1, platforms={'qwen'}, min_cite_position=3)
        assert score_candidate('whitelist', s) > score_candidate('gray', s)

    def test_rank_monotonic(self):
        # 靠前 rank 权重更高
        assert _rank_weight(1) > _rank_weight(3) > _rank_weight(10)
        assert _rank_weight(None) == 0.5

    def test_more_citations_and_platforms_higher(self):
        few = UrlSignals(citation_count=1, platforms={'qwen'}, min_cite_position=3)
        many = UrlSignals(citation_count=5, platforms={'qwen', 'kimi', 'doubao'}, min_cite_position=3)
        assert score_candidate('gray', many) > score_candidate('gray', few)

    def test_none_signals_fallback(self):
        # candidate 无聚合信号也不炸(给权威+中性 rank)
        assert score_candidate('whitelist', None) > score_candidate('gray', None)


# ==================== 配额选择 ====================

def _mk(uh, ind, plat, dom, score):
    return {'url_hash': uh, 'normalized_url': f'https://{dom}/{uh[:6]}',
            'industry_name': ind, 'platform': plat, 'domain': dom, 'score': score}


class TestSelectBudget:
    def test_budget_max_zero_returns_empty(self):
        cands = [_mk(_uh(f'u{i}'), 'ind0', 'qwen', 'd.com', 1.0) for i in range(10)]
        assert select_url_budget(cands, BudgetConfig(budget_max=0)) == []

    def test_total_cap_respected(self):
        cands = [_mk(_uh(f'u{i}'), f'ind{i%5}', 'qwen', f'd{i%50}.com', float(i))
                 for i in range(500)]
        out = select_url_budget(cands, BudgetConfig(budget_max=100, max_urls_per_domain=100,
                                                    min_urls_per_industry=0, min_urls_per_platform=0))
        assert len(out) == 100

    def test_domain_cap_removes_low_value_dup(self):
        # 单域名 100 URL, max_per_domain=10 → 该域名最多 10
        cands = [_mk(_uh(f'spam{i}'), 'ind0', 'qwen', 'spam.com', 1.0) for i in range(100)]
        cands += [_mk(_uh(f'good{i}'), 'ind0', 'qwen', f'g{i}.com', 5.0) for i in range(50)]
        out = select_url_budget(cands, BudgetConfig(budget_max=1000, max_urls_per_domain=10,
                                                    min_urls_per_industry=0, min_urls_per_platform=0))
        spam = [c for c in out if c['domain'] == 'spam.com']
        assert len(spam) == 10, f"域名去重复失败: {len(spam)}"

    def test_industry_coverage_floor(self):
        # 5 行业, 每行业 100 候选, min_per_industry=20 → 每行业至少 20
        cands = []
        for ind in range(5):
            for i in range(100):
                cands.append(_mk(_uh(f'i{ind}_{i}'), f'ind{ind}', 'qwen', f'd{i}.com', float(i)))
        out = select_url_budget(cands, BudgetConfig(budget_max=200, max_urls_per_domain=100,
                                                    min_urls_per_industry=20, min_urls_per_platform=0))
        from collections import Counter
        by_ind = Counter(c['industry_name'] for c in out)
        for ind in range(5):
            assert by_ind[f'ind{ind}'] >= 20, f"ind{ind} 覆盖 {by_ind[f'ind{ind}']} < 20"

    def test_platform_coverage_floor(self):
        # 4 平台, min_per_platform=30
        cands = []
        for p, plat in enumerate(['qwen', 'kimi', 'doubao', 'deepseek']):
            for i in range(100):
                cands.append(_mk(_uh(f'{plat}_{i}'), 'ind0', plat, f'd{i}.com', float(i)))
        out = select_url_budget(cands, BudgetConfig(budget_max=200, max_urls_per_domain=100,
                                                    min_urls_per_industry=0, min_urls_per_platform=30,
                                                    max_urls_per_industry=10000))
        from collections import Counter
        by_plat = Counter(c['platform'] for c in out)
        for plat in ['qwen', 'kimi', 'doubao', 'deepseek']:
            assert by_plat[plat] >= 30, f"{plat} 覆盖 {by_plat[plat]} < 30"

    def test_many_industries_no_alphabetical_starvation(self):
        """[轮1审核修] 行业数 × min > budget 时: effective_floor 钳制 → 覆盖率【整体
        等比降级】而非字母序尾部行业被饿死为 0。60 行业 × 各 20 候选 · budget=120 ·
        min_per_industry=40(60×40=2400 » 120)→ 每行业应 ≈2(120//60), 无行业 = 0。"""
        cands = []
        for ind in range(60):
            for i in range(20):
                cands.append(_mk(_uh(f'i{ind:02d}_{i}'), f'ind{ind:02d}', 'qwen',
                                 f'd{ind}_{i}.com', float(i)))
        out = select_url_budget(cands, BudgetConfig(
            budget_max=120, min_urls_per_industry=40, min_urls_per_platform=0,
            max_urls_per_domain=100))
        from collections import Counter
        by_ind = Counter(c['industry_name'] for c in out)
        # 每行业都拿到 ≥1(不饿死); 无字母序尾部行业为 0
        covered = [f'ind{i:02d}' for i in range(60)]
        starved = [ind for ind in covered if by_ind.get(ind, 0) == 0]
        assert not starved, f"字母序尾部行业被饿死: {starved[:5]}"
        assert len(out) <= 120

    def test_deterministic(self):
        import random
        rng = random.Random(42)
        cands = [_mk(_uh(f'd{i}'), f'ind{i%7}', ['qwen', 'kimi'][i % 2], f'dom{i%40}.com',
                     rng.random() * 10) for i in range(600)]
        cfg = BudgetConfig(budget_max=150, max_urls_per_domain=8)
        a = select_url_budget(list(cands), cfg)
        b = select_url_budget(list(cands), cfg)
        assert [c['url_hash'] for c in a] == [c['url_hash'] for c in b], "非确定性"

    def test_higher_score_ranked_first(self):
        cands = [_mk(_uh(f'u{i}'), 'ind0', 'qwen', f'd{i}.com', float(i)) for i in range(50)]
        out = select_url_budget(cands, BudgetConfig(budget_max=10, max_urls_per_domain=100,
                                                    min_urls_per_industry=0, min_urls_per_platform=0))
        scores = [c['score'] for c in out]
        assert scores == sorted(scores, reverse=True), "budget_rank 未按 score 降序"
        assert out[0]['budget_rank'] == 1


# ==================== ≥10k 压测 ====================

class TestLargeScaleBudget:
    def _build_production_like(self):
        """模拟生产 10,795 URL: 19 行业 × 平台 × 域名池 + 热门域名重复 + 15% 答案引用。"""
        import random
        rng = random.Random(2026)
        industries = [f'行业{i}' for i in range(19)]
        platforms = ['qwen', 'kimi', 'doubao', 'deepseek']
        # 域名池: 20 个热门(会重复很多)+ 长尾
        hot_domains = [f'hot{i}.com' for i in range(20)]
        cands = []
        total = 10795
        for i in range(total):
            ind = rng.choice(industries)
            plat = rng.choice(platforms)
            if rng.random() < 0.55:
                dom = rng.choice(hot_domains)       # 55% 落热门域名(制造重复来源)
            else:
                dom = f'tail{i}.com'                 # 长尾唯一域名
            tier = 'whitelist' if rng.random() < 0.1 else 'gray'
            sig = UrlSignals(
                citation_count=rng.randint(1, 8),
                platforms=set(rng.sample(platforms, rng.randint(1, 4))),
                min_cite_position=rng.randint(1, 10),
                any_answer_cited=(rng.random() < 0.15),   # 15% 答案实际引用
            )
            score = score_candidate(tier, sig)
            cands.append({
                'url_hash': _uh(f'url{i}'),
                'normalized_url': f'https://{dom}/a{i}',
                'industry_name': ind, 'platform': plat, 'domain': dom,
                'score': score, 'tier': tier, '_answer_cited': sig.any_answer_cited,
            })
        return cands

    def test_10k_budget_caps_quotas_coverage_and_time(self):
        cands = self._build_production_like()
        assert len(cands) == 10795
        cfg = BudgetConfig(budget_max=1500, min_urls_per_industry=40,
                           min_urls_per_platform=50, max_urls_per_domain=30)
        out = select_url_budget(cands, cfg)

        # 1) 总量上限
        assert len(out) <= 1500, f"选中 {len(out)} > 上限 1500"
        assert len(out) >= 1000, f"选中 {len(out)} 过少(配额底线应撑起量)"

        from collections import Counter
        by_dom = Counter(c['domain'] for c in out)
        by_ind = Counter(c['industry_name'] for c in out)
        by_plat = Counter(c['platform'] for c in out)

        # 2) 域名上限(去低价值重复来源): 热门域名不得超 30
        for dom, n in by_dom.items():
            assert n <= 30, f"域名 {dom} 选中 {n} > 30"
        # 3) 行业覆盖: 每行业 ≥ min(40, 候选数)
        cand_by_ind = Counter(c['industry_name'] for c in cands)
        for ind, cn in cand_by_ind.items():
            assert by_ind[ind] >= min(40, cn), f"{ind} 覆盖 {by_ind[ind]} < {min(40, cn)}"
        # 4) 平台覆盖: 每平台 ≥ min(50, 候选数)
        cand_by_plat = Counter(c['platform'] for c in cands)
        for plat, cn in cand_by_plat.items():
            assert by_plat[plat] >= min(50, cn), f"{plat} 覆盖 {by_plat[plat]} < 50"

        # 5) 高价值优先保留(引用覆盖率不明显下降 · 要求3/7): 答案引用 URL 的保留率
        # 必须【强】—— 锚定实测 ~79%, 断言 ≥0.70(轮1审核: 原 *1.8≈25% 太弱可假绿)。
        # 注: 此压测全量全新(无预建 article), free 豁免不介入, 纯 select_url_budget 结果。
        overall_rate = len(out) / len(cands)                      # ≈ 14%
        sel_hashes = {c['url_hash'] for c in out}
        cited_total = [c for c in cands if c['_answer_cited']]
        cited_sel = [c for c in cited_total if c['url_hash'] in sel_hashes]
        cited_rate = len(cited_sel) / max(len(cited_total), 1)
        assert cited_rate >= 0.70, (
            f"答案引用保留率 {cited_rate:.2%} < 70% → 引用覆盖率明显下降(要求7)"
        )
        assert cited_rate > overall_rate * 3, (
            f"答案引用保留率 {cited_rate:.2%} 未数倍于整体 {overall_rate:.2%} → 高价值未优先"
        )
        # whitelist 保留率 > gray 保留率
        wl_total = [c for c in cands if c['tier'] == 'whitelist']
        wl_sel = [c for c in wl_total if c['url_hash'] in sel_hashes]
        gray_total = [c for c in cands if c['tier'] == 'gray']
        gray_sel = [c for c in gray_total if c['url_hash'] in sel_hashes]
        wl_rate = len(wl_sel) / max(len(wl_total), 1)
        gray_rate = len(gray_sel) / max(len(gray_total), 1)
        assert wl_rate > gray_rate, f"whitelist 保留率 {wl_rate:.2%} 未高于 gray {gray_rate:.2%}"

        # 6) 全轮 ≤1.5h 推算: stage3 Jina 时长 = 选中数 / 38 URL/min
        est_stage3_min = len(out) / JINA_THROUGHPUT_URL_PER_MIN
        assert est_stage3_min <= 45.0, (
            f"stage3 预计 {est_stage3_min:.0f}min · 选中 {len(out)} · 全轮预算不满足"
        )
        print(
            f"\n[S3-BUDGET] 10795 → 选中 {len(out)} · stage3≈{est_stage3_min:.0f}min · "
            f"答案引用保留率 {cited_rate:.0%}(整体 {overall_rate:.0%}) · "
            f"whitelist {wl_rate:.0%} vs gray {gray_rate:.0%}"
        )
