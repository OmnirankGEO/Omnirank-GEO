# -*- coding: utf-8 -*-
"""§0 裁决一专项锁(返修单 v3 §E-5 · 三条缺一不可)。

Owner 亲裁:处理判据 = 会不会被 AI 降权,不是证据够不够。
  · 客户/检索来的事实:**不删不降级**(零外部信源也不产生缺证据类警告);
  · 要处理的只有三类形态瑕疵:①自曝软文指纹 ②裸域名/自有渠道假冒第三方归属
    ③广告法绝对化用语;
  · 处理阶梯:**改写优先,删除是最后手段** —— 删的是"会被降权的形态",
    不是"承载它的事实"(防新删除机器)。
"""
from __future__ import annotations

from writing.body_internal_marker_sanitizer import sanitize_article_body
from writing.content_cleaner import _blend_visible_source_labels
from writing.evidence_precision_policy import evaluate_evidence_precision
from writing.evidence_first_policy import evaluate_content_trust

SNAPSHOT = {
    "version": "brand-fact-v1",
    "brand_name": "观山电梯",
    "claims": [
        {"claim_id": "BF-001", "field": "after_sales",
         "value": "整机质保 24 个月,深圳本地 2 小时上门",
         "provenance": "customer_provided", "verification_status": "customer_asserted"},
        {"claim_id": "BF-002", "field": "delivery_capability",
         "value": "累计交付 32 台观光电梯",
         "provenance": "customer_provided", "verification_status": "customer_asserted"},
    ],
}


# ---------------------------------------------------------- 锁 a:不删不降级
def test_customer_fact_numbers_survive_with_zero_external_evidence():
    """🔴 [裁决一 · a] 客户自有事实数字 + 零外部信源:
    正文保留数字,评估不产生降级/删除/缺证据类发现。"""
    body = (
        "# 观光电梯售后怎么看\n\n"
        "## 售后能力\n\n"
        "整机质保 24 个月,深圳本地 2 小时上门。〔BF-001〕\n\n"
        "## 交付记录\n\n累计交付 32 台观光电梯。〔BF-002〕\n"
    )
    # 全清洗链(编号剥除;pack 为空 = 零外部信源)
    cleaned, _ = sanitize_article_body(body, {"items": []})
    cleaned = _blend_visible_source_labels(cleaned)
    for fact in ("24 个月", "2 小时", "32 台"):
        assert fact in cleaned, f"客户事实数字被删:{fact}(违反裁决一)"
    # 评估层:客户血缘事实不产生缺证据类发现
    assessment = evaluate_evidence_precision(body, {}, SNAPSHOT)
    codes = {f.code for f in assessment.warnings} | {f.code for f in assessment.hard}
    assert "claim_missing_inline_evidence" not in codes, (
        f"客户事实被判缺证据:{codes}(裁决一:证据够不够不是判据)"
    )
    assert "customer_fact_source_boundary_missing" not in codes


# ------------------------------------------------- 锁 b:三类形态瑕疵仍被清除
def test_self_disclosure_fingerprint_still_removed():
    """① 自曝软文指纹仍被清除。"""
    out = _blend_visible_source_labels("质保 24 个月（来源：企业提供资料）。")
    assert "企业提供资料" not in out
    assert "24 个月" in out


def test_bare_domain_fake_attribution_still_blocked():
    """② 裸域名假冒第三方归属仍被拦(未收录域名零归属句)。"""
    from writing.evidence_pack import attribution_of

    a = attribution_of({"publisher": "some-unknown-site.cn", "title": "页面",
                        "url": "https://some-unknown-site.cn/a"})
    assert a["prose"] == "" and a["trust"] == "unverified"


def test_adlaw_absolute_terms_still_flagged():
    """③ 广告法绝对化用语仍被识别(Owner 保留红线)。"""
    from services.marketing.legal_context import find_absolute_violations

    assert find_absolute_violations("我们是行业第一品牌。")
    hard = [f.code for f in evaluate_content_trust(
        "t", "本公司是最佳选择。", evidence_mode="no_evidence").hard]
    assert "absolute_superlative_claim" in hard


# ------------------------------------------------- 锁 c:改写优先(防删除机器)
def test_rewrite_first_self_disclosure_stripped_fact_preserved():
    """🔴 [裁决一 · c] 「本地 2 小时上门(资料由企业提供)」→
    自曝部分消失、事实与数值**原样保留**。
    变异对照(runner MC1):把处理改成整句删除 → 本锁必红。"""
    raw = "观山电梯提供本地 2 小时上门服务（来源：企业提供资料），覆盖深圳全区。"
    out = _blend_visible_source_labels(raw)
    assert "企业提供资料" not in out, "自曝没被清除"
    assert "2 小时上门" in out, "事实被连坐删除 —— 造出了新的删除机器"
    assert "覆盖深圳全区" in out, "邻接事实被殃及"


def test_rewrite_first_holds_for_clause_form_too():
    """从句形态同验:「据企业提供资料，交付 32 台。」→ 摘归属留主张。"""
    out = _blend_visible_source_labels("据企业提供资料，累计交付 32 台。")
    assert "据企业提供资料" not in out
    assert "32 台" in out


def test_strip_self_disclosure_layer_removes_phrase_keeps_line():
    """🔴 [R3 订正1 2026-08-11] MC1 **同层**锁:Review 实证锁 c 走
    `_blend_visible_source_labels` 从句层,压根不经过 `strip_self_disclosure`,
    §4-c 把 MC1 的死记在锁 c 头上是归因错误(真正杀它的是 test_selfdisclose_zero)。
    本锁直接打 `polish_source_disclosure → strip_self_disclosure` 那一层:
    机械披露短语被摘、同行事实与数值原样保留。
    变异对照(MC1 整行删):短语所在整行被删 → 「32 台」消失 → 本锁必红。"""
    from writing.source_disclosure_style import polish_source_disclosure

    out = polish_source_disclosure("公司材料显示，累计交付 32 台观光电梯，覆盖深圳全区。")
    assert "公司材料显示" not in out, "机械披露短语没被摘"
    assert "32 台" in out, "事实被整行连坐删除(MC1 形态)—— 新删除机器"
    assert "覆盖深圳全区" in out, "同行邻接事实被殃及"
