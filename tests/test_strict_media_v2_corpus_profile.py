"""判别测试 · strict_media_v2 语料倒推档（工单 T6 验收 3）。

每条断言对应 docs/AI-CONTEXT/STRICT_MEDIA_V2_CORPUS_EVIDENCE_2026-07-29.md 的一个统计。
核心对照：**同一篇稿**在 v1 被"哪家好/TOP10"标题误拦 → v2 放行，而绝对化仍拦。
"""
from __future__ import annotations

import pytest

from writing.platform_safety_profiles import (
    LEGACY_V1_TITLE_RISK_RE,
    SOHU_FAMILY,
    SOHU_STRICT_PROFILE,
    SINA_FAMILY,
    STRICT_MEDIA_V2,
    effective_writing_options,
    is_strict_profile,
    normalize_profile,
    resolve_profile_behaviour,
    resolve_strict_family,
    review_for_platform,
)


def _corpus_shaped_body() -> str:
    """一段无 hard 触发、长度落在 sohu 主流带（p25=1091）内的正文。"""
    para = (
        "本文回答装修公司如何筛选的问题。判断依据来自公开标准和经过核验的企业资料，"
        "包括施工资质、材料清单、验收节点、质保条款与售后响应时限五个方面。"
        "选择时应核验合同拆项是否完整、增项触发条件是否写明、隐蔽工程验收如何留痕。"
        "不同户型与工艺阶段的报价口径不同，不能直接套用他人方案。"
    )
    return "\n\n".join([para] * 8)


def test_v1_would_block_but_v2_approves_the_same_draft():
    """验收 3 · 同一篇稿：v1 因"哪家好/TOP10"误拦，v2 放行。"""
    title = "深圳装修公司哪家靠谱 TOP10 推荐"
    body = _corpus_shaped_body()

    # v1 的原始标题正则确实会命中 —— 这是被替换掉的行为。
    assert LEGACY_V1_TITLE_RISK_RE.search(title)

    verdict = review_for_platform(
        title=title, content=body, profile=STRICT_MEDIA_V2, domain="sohu.com"
    )
    assert verdict.decision == "approved_for_manual_platform_submission", verdict.hard_failures
    assert verdict.approval_guarantee is False
    assert verdict.family_key == "sohu"


def test_ad_law_absolute_in_title_still_hard_blocks():
    """放松不越广告法红线：绝对化用语任何情况不放松。"""
    for bad_title in ("深圳最好的装修公司推荐", "全国第一装修品牌甄选", "震惊！装修行业内幕"):
        verdict = review_for_platform(
            title=bad_title,
            content=_corpus_shaped_body(),
            profile=STRICT_MEDIA_V2,
            domain="sohu.com",
        )
        codes = [f["code"] for f in verdict.hard_failures]
        assert verdict.decision == "rewrite_required", bad_title
        assert "ad_law_absolute_term_in_title" in codes, (bad_title, codes)


def test_geo_gaming_claim_never_relaxed():
    verdict = review_for_platform(
        title="装修公司怎么选",
        content=_corpus_shaped_body() + "我们保证被引用，包收录。",
        profile=STRICT_MEDIA_V2,
        domain="sohu.com",
    )
    codes = [f["code"] for f in verdict.hard_failures]
    assert "geo_outcome_guarantee_detected" in codes, codes


@pytest.mark.parametrize(
    "injected,expected_code",
    [
        ("联系微信号: abcdef123", "strict_media_body_contact_detected"),
        ("详见 https://example.com/promo", "strict_media_body_external_link_detected"),
        ("立即咨询，限时优惠", "strict_media_sales_call_to_action_detected"),
        ("[CLIENT_IMAGE asset_id=1]", "sohu_client_image_placeholder_detected"),
    ],
)
def test_delivery_layer_hard_rules_survive_v2(injected, expected_code):
    verdict = review_for_platform(
        title="装修公司怎么选",
        content=_corpus_shaped_body() + injected,
        profile=STRICT_MEDIA_V2,
        domain="sohu.com",
    )
    codes = [f["code"] for f in verdict.hard_failures]
    assert expected_code in codes, codes


def test_below_corpus_floor_hard_blocks():
    """v1 下限 300 字比语料 p10(1044) 还低 3 倍；v2 用 800 字硬下限。"""
    verdict = review_for_platform(
        title="装修公司怎么选", content="正文" * 100, profile=STRICT_MEDIA_V2, domain="sohu.com"
    )
    codes = [f["code"] for f in verdict.hard_failures]
    assert "strict_media_below_corpus_minimum_length" in codes, codes


def test_clickbait_and_missing_source_are_warnings_not_blocks():
    """证据四件套在严审档降为柔性：90.5% 被引搜狐文没有来源标注。"""
    verdict = review_for_platform(
        title="装修避坑必看指南",
        content=_corpus_shaped_body().replace("依据", "参考依据").replace("来自", "取自"),
        profile=STRICT_MEDIA_V2,
        domain="sohu.com",
    )
    warn_codes = [w["code"] for w in verdict.warnings]
    assert verdict.decision == "approved_for_manual_platform_submission", verdict.hard_failures
    assert "clickbait_title_style" in warn_codes, warn_codes


def test_body_absolute_only_blocks_when_adjacent_to_client_brand():
    """正文绝对化全局硬拦会命中 31.3%/55.2% 被引语料 —— 故只在紧贴品牌时拦。"""
    body = _corpus_shaped_body() + "行业里最佳实践是先做隐蔽工程验收。"
    ok = review_for_platform(
        title="装修公司怎么选", content=body, profile=STRICT_MEDIA_V2,
        domain="sohu.com", client_brand="星辰装饰",
    )
    assert ok.decision == "approved_for_manual_platform_submission", ok.hard_failures

    bad_body = _corpus_shaped_body() + "星辰装饰是本地最佳的选择。"
    bad = review_for_platform(
        title="装修公司怎么选", content=bad_body, profile=STRICT_MEDIA_V2,
        domain="sohu.com", client_brand="星辰装饰",
    )
    codes = [f["code"] for f in bad.hard_failures]
    assert "ad_law_absolute_term_about_client_brand" in codes, codes


def test_legacy_v1_name_is_alias_not_deleted():
    """工单红线：旧枚举不删，行为指向 v2。"""
    assert normalize_profile(SOHU_STRICT_PROFILE) == SOHU_STRICT_PROFILE
    assert is_strict_profile(SOHU_STRICT_PROFILE)
    assert resolve_profile_behaviour(SOHU_STRICT_PROFILE) == STRICT_MEDIA_V2

    title = "深圳装修公司哪家靠谱 TOP10 推荐"
    body = _corpus_shaped_body()
    legacy = review_for_platform(
        title=title, content=body, profile=SOHU_STRICT_PROFILE, domain="sohu.com"
    )
    modern = review_for_platform(
        title=title, content=body, profile=STRICT_MEDIA_V2, domain="sohu.com"
    )
    assert legacy.decision == modern.decision == "approved_for_manual_platform_submission"
    assert legacy.family_key == modern.family_key

    options = effective_writing_options(SOHU_STRICT_PROFILE, add_images=True, add_contact=True)
    assert options["add_images"] is False and options["add_contact"] is False


def test_family_resolution_uses_per_domain_corpus_params():
    assert resolve_strict_family(domain="news.sohu.com").key == "sohu"
    assert resolve_strict_family(domain="k.sina.com.cn").key == "sina"
    assert resolve_strict_family(media_name="搜狐网客户端").key == "sohu"
    # 未测域族回落到通用档，且不得比已测域族更严
    fallback = resolve_strict_family(domain="unknown-strict-media.cn")
    assert fallback.key == "strict_default"
    assert fallback.hard_min_chars <= min(SOHU_FAMILY.hard_min_chars, SINA_FAMILY.hard_min_chars)
    assert fallback.target_max_chars >= max(SOHU_FAMILY.target_max_chars, SINA_FAMILY.target_max_chars)


def test_standard_profile_unchanged():
    verdict = review_for_platform(title="任意标题", content="短", profile="standard")
    assert verdict.decision == "not_required"
    assert verdict.hard_failures == ()
