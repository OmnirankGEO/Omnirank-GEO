"""判别测试 · 问题族组合引擎（工单 T2 / T3 · 验收 1）。

夹具用的是生产实测分布（证据 §6，2026-07-29 只读采数复现工单 §〇②）：
"装修 + 哪家/靠谱/推荐" → to8to 36 / sohu 33 / cnblogs 18 / 知乎专栏 17 /
163 9 / 新浪家居 9 ≈ 垂类 45% : 主干 55%。
"""
from __future__ import annotations

import pytest

from services.citation_domain_weights import (
    ROLE_TECH_COMMUNITY,
    ROLE_VERTICAL,
    is_trunk_domain,
)
from services.media_publish_success import (
    MIN_SAMPLE_FOR_PENALTY,
    MediaSuccessStat,
    _factor_from_rate,
    best_channel_within_family,
)
from services.question_family_mix import (
    apply_inventory_substitution,
    derive_intent_bucket,
    inventory_keyword_for,
    mix_from_rows,
    plan_combination,
)

# 生产实测行（域名保留原始 host，归一化由被测代码自己做）
DECORATION_MIX_ROWS = [
    {"domain": "to8to.com", "citations": 36},
    {"domain": "sohu.com", "citations": 33},
    {"domain": "cnblogs.com", "citations": 18},
    {"domain": "zhuanlan.zhihu.com", "citations": 17},
    {"domain": "163.com", "citations": 9},
    {"domain": "yanjiao.jiaju.sina.cn", "citations": 9},
    {"domain": "mp.weixin.qq.com", "citations": 9},
]


def _mix():
    return mix_from_rows(
        DECORATION_MIX_ROWS, keyword="深圳装修公司哪家靠谱", industry="装修"
    )


def test_intent_bucket_derivation():
    # "哪家" 命中 recommendation；ranking 桶排在前但该串无排名词
    assert derive_intent_bucket("深圳装修公司哪家靠谱") == "recommendation"
    assert derive_intent_bucket("装修公司排行榜") == "ranking"
    assert derive_intent_bucket("装修多少钱") == "price"
    assert derive_intent_bucket("") == "general"


def test_real_mix_is_not_collapsed_into_one_role():
    """验收 1：真实 mix 折算出的组合必须同时含主干与垂类，比例可解释。"""
    mix = _mix()
    assert mix.total_citations == 131
    # 垂类 = to8to（行业垂类）；主干 = 搜狐/博客园/知乎/网易/新浪/公众号
    assert 0.5 <= mix.trunk_share <= 0.8, mix.role_shares()
    assert 0.2 <= mix.vertical_share <= 0.5, mix.role_shares()

    plan = plan_combination(mix, total_slots=6)
    assert plan.trunk_slots >= 1 and plan.vertical_slots >= 1, plan.as_dict()
    domains = [s.domain for s in plan.slots]
    # 四个真实被引主角都必须在默认组合里
    for expected in ("to8to.com", "sohu.com", "cnblogs.com", "zhihu.com"):
        assert expected in domains, domains


def test_mix_explanation_carries_real_numbers():
    """理由必须带数据 —— 「您这类问题 AI 引用 to8to 27%/搜狐 25%」。"""
    text = _mix().explanation()
    assert "to8to.com" in text and "%" in text
    assert "131" in text


def test_weixin_is_ugc_not_tencent_portal():
    """``mp.weixin.qq.com`` 归一化后是 qq.com —— 不能因此被判成腾讯网门户。"""
    entry = next(e for e in _mix().entries if "qq.com" in e.domain)
    assert entry.role == "ugc_qa", entry.as_dict()
    assert entry.self_serve is True


def test_trunk_membership_replaces_hardcoded_generic_list():
    """废除的 13 名单成员必须全部仍判为主干，且博客园/CSDN 由垂类升为主干。"""
    for legacy in ("知乎", "搜狐", "今日头条", "抖音", "百度", "新浪网",
                   "微信公众平台", "新浪", "腾讯", "网易", "凤凰", "微博"):
        assert is_trunk_domain("", legacy), legacy
    assert is_trunk_domain("", "博客园") and is_trunk_domain("", "CSDN")
    assert not is_trunk_domain("", "汽车之家")
    assert not is_trunk_domain("", "土巴兔")


# ---------------------------------------------------------------------------
# 缺货降位三规则
# ---------------------------------------------------------------------------
def _lookup_factory(available: dict[str, dict]):
    def _lookup(keyword: str):
        return available.get(keyword)
    return _lookup


def test_rule1_substitutes_inside_the_same_role():
    """博客园(技术社区)无货 → 补 CSDN/51CTO,而不是让门户把位置吃掉。"""
    mix = _mix()
    plan = plan_combination(mix, total_slots=6)
    available = {
        "搜狐": {"id": 1, "media_name": "搜狐网新闻（官方）"},
        "土巴兔": {"id": 2, "media_name": "土巴兔(家居资讯频道)"},
        "CSDN": {"id": 3, "media_name": "CSDN 博客"},
        "网易": {"id": 4, "media_name": "网易新闻房产"},
        "新浪": {"id": 5, "media_name": "新浪家居"},
        # 博客园 / 知乎 / 公众号 无货
    }
    plan = apply_inventory_substitution(
        plan, inventory_lookup=_lookup_factory(available), record_demand=False
    )
    cnblogs = next(s for s in plan.slots if s.domain == "cnblogs.com")
    assert cnblogs.substituted is True
    assert cnblogs.fulfilled_by == "CSDN 博客", cnblogs.as_dict()
    assert cnblogs.role == ROLE_TECH_COMMUNITY

    # 反证：技术社区的位置没有被门户顶掉
    assert cnblogs.fulfilled_by not in {"搜狐网新闻（官方）", "网易新闻房产"}


def test_rule2_substitution_is_labelled_never_silent():
    mix = _mix()
    plan = plan_combination(mix, total_slots=6)
    plan = apply_inventory_substitution(
        plan,
        inventory_lookup=_lookup_factory({"搜狐": {"id": 1, "media_name": "搜狐网新闻"},
                                          "CSDN": {"id": 3, "media_name": "CSDN 博客"}}),
        record_demand=False,
    )
    subs = [s for s in plan.slots if s.substituted]
    assert subs, plan.as_dict()
    for slot in subs:
        # 文案已按验收要求去黑话（「暂无供给」→「我们暂时买不到发布位」），断言同步。
        assert "买不到发布位" in slot.substitution_note
        assert "已换成同类的" in slot.substitution_note
        assert "暂无供给" not in slot.substitution_note
    # 无货且同类也没有的位置，也必须留话，不允许静默空着
    for slot in plan.slots:
        if not slot.fulfilled_by:
            assert slot.substitution_note


def test_rule3_self_serve_offer_for_ugc_platforms():
    """博客园/知乎/公众号缺货 → 给自助发布入口,不让用户空手而归。"""
    mix = _mix()
    plan = plan_combination(mix, total_slots=6)
    plan = apply_inventory_substitution(
        plan, inventory_lookup=_lookup_factory({"搜狐": {"id": 1, "media_name": "搜狐网新闻"}}),
        record_demand=False,
    )
    offers = {s.domain: s.self_serve_action for s in plan.slots if s.self_serve_action}
    assert "cnblogs.com" in offers or "zhihu.com" in offers, offers
    any_offer = next(iter(offers.values()))
    assert any_offer["id"] == "self_serve_publish"
    assert any_offer["target"] == "publish_center_self_serve"


def test_inventory_keyword_mapping():
    assert inventory_keyword_for("cnblogs.com") == "博客园"
    assert inventory_keyword_for("zhuanlan.zhihu.com") == "知乎"
    assert inventory_keyword_for("sohu.com") == "搜狐"


# ---------------------------------------------------------------------------
# T3 发布成功率
# ---------------------------------------------------------------------------
def test_success_factor_curve_matches_observed_baseline():
    """全局成功率 56.7%(185/326) 为中性点；搜狐系 13.4% 应显著降权。"""
    assert _factor_from_rate(0.567) == pytest.approx(1.0, abs=0.01)
    sohu_like = _factor_from_rate(9 / 67)
    assert 0.55 <= sohu_like < 0.75, sohu_like
    assert _factor_from_rate(1.0) > 1.0


def test_small_sample_is_never_penalised():
    from services.media_publish_success import build_media_success_table

    assert MIN_SAMPLE_FOR_PENALTY == 5
    stat = MediaSuccessStat(
        media_name="新媒体", orders=3, settled=3, published=0, rejected=3,
        success_rate=0.0, factor=1.0, sample_sufficient=False,
    )
    assert stat.factor == 1.0
    assert callable(build_media_success_table)


def test_best_channel_within_family_picks_highest_success():
    table = {
        "搜狐网新闻（官方）": MediaSuccessStat("搜狐网新闻（官方）", 5, 5, 2, 3, 0.4, 0.85, True),
        "搜狐网客户端": MediaSuccessStat("搜狐网客户端", 17, 16, 0, 12, 0.0, 0.55, True),
        "搜狐网娱乐": MediaSuccessStat("搜狐网娱乐", 7, 6, 0, 5, 0.0, 0.55, True),
    }
    picked = best_channel_within_family(
        table, ["搜狐网客户端", "搜狐网新闻（官方）", "搜狐网娱乐"]
    )
    assert picked == "搜狐网新闻（官方）"


def test_quality_score_unchanged_without_success_factor():
    """success_factor=None 时评分逐位不变 —— 无数据环境零行为变化。"""
    from services.placement_service import _quality_score_v2f

    row = {"price": 45, "authority_media": 1, "geo_rank": 3,
           "portal_media": "其他门户", "geo_rank_platform": "豆包,DeepSeek"}
    base = _quality_score_v2f(row, citation_strength=0.7)
    assert _quality_score_v2f(row, citation_strength=0.7, success_factor=None) == base
    assert _quality_score_v2f(row, citation_strength=0.7, success_factor=1.0) == pytest.approx(base)
    assert _quality_score_v2f(row, citation_strength=0.7, success_factor=0.55) < base
