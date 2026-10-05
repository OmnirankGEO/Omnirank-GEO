"""工单 C-3 判别锁(2026-07-27)· 审核器口径对齐 + findings 聚合 + 一键修复。

生产实况:深档文每篇 61-88 处 findings,多数是审核器没跟上三轮写作改革的结构性误报。
三处口径对撞的对齐**不是放松** —— 每处都配正反两向锁:
  合法形态不报(对齐生效) + 违规形态仍报(没放松),各配变异。
验收:生产同款 16000 字深档合成文实跑审核,findings 类型卡 ≤10 张。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 构造工具:已核验 evidence pack / 企业资料快照 / 16000 字深档合成文
# ---------------------------------------------------------------------------
def _verified_pack(claims: list[str]) -> dict:
    return {
        "items": [
            {
                "evidence_id": f"EV-{i + 1:03d}",
                "claim": claim,
                "excerpt": claim,
                "url": f"https://example.com/src/{i}",
                "publisher": f"媒体{i}",
                "verification_status": "human_verified",
                "human_reviewed_by": "reviewer",
                "human_reviewed_at": "2026-07-27",
                "human_review_reason": "C-3 判别锁构造",
            }
            for i, claim in enumerate(claims)
        ],
    }


_DECLARATION = (
    "以下企业相关信息依据本次提交的业务资料整理,尚未完成独立交叉核验;"
    "本文将其作为核验起点,而非最终结论(截至 2026 年 7 月)。"
)

_CRITERIA_SECTION = (
    "## 入选标准与排序依据\n\n"
    "本文的入选口径:近三年在本地有公开交付记录、可查到独立报道的服务商;"
    "排序依据为公开报道数量与资质记录,资料时点为发稿当月,排除条件为无公开信息可查。"
)


def _filler_paragraphs(chars: int) -> str:
    para = (
        "从需求梳理到方案沟通,再到交付后的回访,整个流程里最值得关注的是信息是否透明、"
        "口径是否一致、承诺是否落在合同里。读者在比较不同服务商时,建议把注意力放在"
        "可以自行查证的公开信息上,而不是只听宣传口径;把边界问清楚,后续合作才稳。\n\n"
    )
    out = []
    total = 0
    while total < chars:
        out.append(para)
        total += len(para)
    return "".join(out)


def _deep_article(*, with_criteria: bool, with_declaration: bool) -> str:
    parts = ["# 本地全屋定制哪家好?12 家服务商深度评测\n"]
    if with_declaration:
        parts.append(_DECLARATION + "\n")
    if with_criteria:
        parts.append(_CRITERIA_SECTION + "\n")
    for i in range(1, 13):
        parts.append(
            f"## 第{i}名 甲{i}装饰公司\n\n"
            f"甲{i}装饰公司主打本地交付,团队规模稳定,服务流程分为量房、设计、"
            "施工与售后四段,每一段都有专人对接。\n\n"
        )
    # 有支撑的数字(pack 内可对齐)+ 无支撑的数字(真·该报)
    parts.append("甲1装饰公司成立于2015年,累计服务超过3200家客户。\n\n")
    parts.append("甲2装饰公司宣称交付周期最短仅需7天。\n\n")
    # 企业侧百分比(声明覆盖)与行业口径百分比(声明盖不到)
    parts.append("该公司客户满意度提升23%,复购率也在稳步上升。\n\n")
    parts.append("有观点称行业平均转化率增长45%,这一说法值得再查证。\n\n")
    parts.append(_filler_paragraphs(15000))
    return "\n".join(parts)


# ===========================================================================
# T1-B · ordered_brand_candidates:披露豁免(对齐)+ 无披露仍报(不放松)
# ===========================================================================
_ORDERED_BODY = "\n".join(f"## 第{i}名 甲{i}装饰公司" for i in range(1, 6))


def test_ordered_ranking_with_disclosure_is_not_flagged():
    from writing.evidence_first_policy import evaluate_content_trust

    body = _CRITERIA_SECTION + "\n\n" + _ORDERED_BODY
    trust = evaluate_content_trust("", body, evidence_mode="unknown")
    assert not any(f.code == "ordered_brand_candidates" for f in trust.soft)


def test_ordered_ranking_without_disclosure_still_flagged_with_anchor():
    from writing.evidence_first_policy import evaluate_content_trust

    trust = evaluate_content_trust("", _ORDERED_BODY, evidence_mode="unknown")
    hits = [f for f in trust.soft if f.code == "ordered_brand_candidates"]
    assert hits, "无披露的编号排序必须照报(对齐不是放松)"
    assert hits[0].matched_text and hits[0].matched_text in _ORDERED_BODY


# ===========================================================================
# T1-C · unsourced_outcome_number:声明作用域(企业侧免、行业侧照报)
# ===========================================================================
def test_enterprise_outcome_covered_by_one_time_declaration():
    # [R3-A3 适配 2026-08-11 · C1] 「一次性声明」豁免已拆除:自曝声明本身是
    # 软文指纹(评估器认它作豁免 = 系统奖励自曝,selfinflicted 复盘实证),
    # 不再给声明作用域内的 outcome 数字免票。本测试原名保留作变更痕迹,
    # 断言翻向新契约:①声明不再豁免 → unsourced_outcome_number 照报;
    # ②声明自身被报 self_disclosed_source;③真正的豁免出路=具体载体名+日期
    # 归属(能力保留,不是一禁了之)。
    from writing.evidence_first_policy import evaluate_content_trust

    body = (
        _DECLARATION + "\n\n" + _filler_paragraphs(600)
        + "\n该公司客户满意度提升23%,复购率也在稳步上升。"
    )
    trust = evaluate_content_trust("", body, evidence_mode="unknown")
    codes = {f.code for f in trust.soft}
    assert "unsourced_outcome_number" in codes, "自曝声明重新拿到豁免权 —— C1 被退回"
    assert "self_disclosed_source" in codes, "自曝声明自身未被识别为软文指纹"
    # 反向:载体归属仍豁免(outcome 数字的正路出口没被堵死)
    attributed = (
        _filler_paragraphs(600)
        + "\n据澎湃新闻 2026 年 5 月报道,该公司客户满意度提升23%。"
    )
    t2 = evaluate_content_trust("", attributed, evidence_mode="unknown")
    assert not any(f.code == "unsourced_outcome_number" for f in t2.soft)


def test_outcome_without_declaration_still_flagged():
    from writing.evidence_first_policy import evaluate_content_trust

    body = _filler_paragraphs(600) + "\n该公司客户满意度提升23%。"
    trust = evaluate_content_trust("", body, evidence_mode="unknown")
    hits = [f for f in trust.soft if f.code == "unsourced_outcome_number"]
    assert hits, "无声明、无邻近来源的百分比必须照报"
    assert hits[0].matched_text


def test_industry_outcome_not_covered_by_enterprise_declaration():
    from writing.evidence_first_policy import evaluate_content_trust

    body = (
        _DECLARATION + "\n\n" + _filler_paragraphs(600)
        + "\n有观点称行业平均转化率增长45%,这一说法值得再查证。"
    )
    trust = evaluate_content_trust("", body, evidence_mode="unknown")
    assert any(
        f.code == "unsourced_outcome_number" for f in trust.soft
    ), "行业口径百分比不在企业声明作用域内,必须照报"


# ===========================================================================
# T1-A · claim_missing_inline_evidence:pack 血缘支撑(对齐)+ 真无支撑照报
# ===========================================================================
def test_number_supported_by_pack_lineage_is_not_flagged():
    from writing.evidence_precision_policy import evaluate_evidence_precision

    pack = _verified_pack(["甲1装饰公司成立于2015年,累计服务超过3200家客户,公开报道可查。"])
    body = "甲1装饰公司成立于2015年,累计服务超过3200家客户。"
    assessment = evaluate_evidence_precision(body, pack, None)
    assert not any(
        f.code == "claim_missing_inline_evidence" for f in assessment.warnings
    ), "pack 血缘可对齐的数字不该再报(正文零 ID 是模板铁律)"


def test_number_without_any_support_still_flagged_with_anchor():
    from writing.evidence_precision_policy import evaluate_evidence_precision

    pack = _verified_pack(["与本句无关的另一件事的报道。"])
    body = "甲2装饰公司宣称交付周期最短仅需7天。"
    assessment = evaluate_evidence_precision(body, pack, None)
    hits = [f for f in assessment.warnings if f.code == "claim_missing_inline_evidence"]
    assert hits, "pack 里对不上的数字必须照报(真无支撑)"
    assert hits[0].matched_text and hits[0].matched_text in body


def test_enterprise_number_supported_by_brand_facts_under_declaration():
    from writing.evidence_precision_policy import evaluate_evidence_precision

    snapshot = {
        "claims": [{
            "claim_id": "BF-001",
            "value": "客户回访记录:满意度提升23%(2026年上半年样本)",
            "field": "satisfaction",
            "provenance": "customer_provided",
            "verification_status": "customer_asserted",
        }],
    }
    body = _DECLARATION + "\n\n该公司客户满意度提升23%。"
    assessment = evaluate_evidence_precision(body, {}, snapshot)
    assert not any(
        f.code == "claim_missing_inline_evidence" for f in assessment.warnings
    ), "一次性声明作用域内、企业资料可对齐的数字不该报"
    # 反向:法规义务永远不能靠企业资料背书
    reg_body = _DECLARATION + "\n\n按照药品生产质量管理规范要求,企业必须每年完成23%产线的再验证。"
    reg = evaluate_evidence_precision(reg_body, {}, snapshot)
    assert any(
        f.code == "claim_missing_inline_evidence" for f in reg.warnings
    ), "法规义务类 claim 不在企业资料可支撑范围,必须照报"


# ===========================================================================
# T2 · findings 同类聚合
# ===========================================================================
def test_aggregate_groups_by_code_with_spans_and_counts():
    from services.article_findings_aggregate import aggregate_article_findings

    qw = {
        "evidence": {"soft": [
            {"code": "unsourced_outcome_number", "severity": "soft",
             "message": "m1", "matched_text": "提升23%", "evidence": "…提升23%…"},
            {"code": "unsourced_outcome_number", "severity": "soft",
             "message": "m1", "matched_text": "", "evidence": "…增长45%…"},
        ]},
        "evidence_precision": {"warnings": [
            {"code": "claim_missing_inline_evidence", "severity": "advisory",
             "message": "m2", "excerpt": "…7天…", "matched_text": "仅需7天"},
        ]},
        "evidence_legal": {"hard": [
            {"code": "absolute_first_claim", "severity": "hard",
             "message": "m3", "matched_text": "行业第一"},
        ]},
    }
    cards = aggregate_article_findings(qw)
    by_code = {card["code"]: card for card in cards}
    assert by_code["unsourced_outcome_number"]["count"] == 2
    assert by_code["unsourced_outcome_number"]["repairable_count"] == 1
    assert len(by_code["unsourced_outcome_number"]["spans"]) == 2
    assert by_code["claim_missing_inline_evidence"]["count"] == 1
    # 聚合总数 == 逐条总数(不丢条目)
    assert sum(card["count"] for card in cards) == 4
    # hard 排最前
    assert cards[0]["code"] == "absolute_first_claim"
    # 人话标题(不是裸 code)
    assert by_code["unsourced_outcome_number"]["title"] != "unsourced_outcome_number"


def test_project_detail_wires_findings_aggregate():
    src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    anchor = src.index("def get_writing_project_detail")
    end = src.index("\ndef ", anchor + 10)
    segment = src[anchor:end]
    import re as _re
    compact = _re.sub(r"\s+", "", segment)
    # 钉主路径赋值本身(except 兜底分支的同名串顶不了这条锁)
    assert 'topic["findings_aggregate"]=aggregate_article_findings(' in compact


# ===========================================================================
# §4 验收 · 16000 字深档合成文实跑:类型卡 ≤10 且 claim_missing 只报真无支撑
# ===========================================================================
def test_deep_article_16k_yields_at_most_ten_type_cards():
    from services.article_findings_aggregate import aggregate_article_findings
    from writing.evidence_first_policy import evaluate_content_trust
    from writing.evidence_precision_policy import evaluate_evidence_precision

    body = _deep_article(with_criteria=True, with_declaration=True)
    assert len(body) >= 16000, f"合成文只有 {len(body)} 字,不满足生产同款口径"

    pack = _verified_pack([
        "甲1装饰公司成立于2015年,累计服务超过3200家客户,公开报道可查。",
    ])
    trust = evaluate_content_trust("", body, evidence_mode="unknown")
    precision = evaluate_evidence_precision(body, pack, None)
    qw = {
        "evidence": trust.warning_payload(),
        "evidence_legal": trust.warning_payload(),
        "evidence_precision": precision.payload(),
    }
    cards = aggregate_article_findings(qw)
    assert len(cards) <= 10, (
        f"类型卡 {len(cards)} 张超验收上限:"
        + ",".join(f"{c['code']}x{c['count']}" for c in cards)
    )
    # 对齐生效:编号排序有披露段 → 不报;企业百分比在声明作用域内 → 不报
    codes = {card["code"] for card in cards}
    assert "ordered_brand_candidates" not in codes
    # claim_missing 只报真无支撑:有支撑的 3200家 不在任何 span 里,无支撑的 7天 在
    missing = [c for c in cards if c["code"] == "claim_missing_inline_evidence"]
    joined = "".join(
        span["matched_text"] + span["excerpt"]
        for card in missing for span in card["spans"]
    )
    assert "3200家" not in joined, "pack 可对齐的数字不该再进 claim_missing"
    assert "7天" in joined, "真无支撑的数字必须还在"


def test_deep_article_without_disclosure_regresses_to_flagged():
    """反向总锁:去掉披露段与声明 → 三类照报(证明对齐没把检测能力砍掉)。"""
    from writing.evidence_first_policy import evaluate_content_trust

    body = _deep_article(with_criteria=False, with_declaration=False)
    trust = evaluate_content_trust("", body, evidence_mode="unknown")
    codes = {f.code for f in trust.soft}
    assert "ordered_brand_candidates" in codes
    assert "unsourced_outcome_number" in codes


# ===========================================================================
# T3 · 修复链:自验扩展 + 端点上下文 + 前端类型卡
# ===========================================================================
def test_still_violates_checks_soft_and_precision_codes():
    from services.writing_span_repair import _still_violates

    ordered = "\n".join(f"{i}. 甲{i}装饰公司" for i in range(1, 5))
    assert _still_violates(ordered, "ordered_brand_candidates") is True
    fixed = _CRITERIA_SECTION + "\n\n" + ordered
    assert _still_violates(fixed, "ordered_brand_candidates") is False

    pack = _verified_pack(["累计服务超过3200家客户。"])
    assert _still_violates("累计服务超过3200家客户。", "claim_missing_inline_evidence", pack) is False
    assert _still_violates("交付周期最短仅需7天。", "claim_missing_inline_evidence", pack) is True


def test_repair_endpoint_carries_pack_context_and_recomputes_advisory():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    anchor = src.index("def api_repair_article_finding")
    # [span 级 AI 免费修复 2026-07-30] 端点变长了(高风险两级拒绝 + 免费额度占位),
    # 6000 字的窗口会把后半段(重算 advisory 那几行)切掉 → 断言假红。窗口放宽,
    # 断言本身一条不动。
    segment = src[anchor:anchor + 12000]
    # [span 级 AI 免费修复 2026-07-30] SQL 改成了带 quotes JOIN 的别名写法
    # (要 industry/title 做高风险判定 + brand_name 做保护词),旧的整串字面量
    # 不再存在。这里改锁**取到了哪几列**这个真意图,而不是当年那一行的写法。
    assert "a.evidence_pack" in segment and "a.brand_fact_snapshot" in segment
    assert '"evidence_pack": _repair_evidence_pack' in segment
    assert '_qw_patch["evidence_precision"]' in segment
    assert '"evidence": _trust.warning_payload()' in segment


_TSX = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"


def test_frontend_renders_type_cards_with_batch_repair_and_failure_governance():
    tsx = _TSX.read_text(encoding="utf-8")
    # 类型卡渲染(标题=类型+计数)与展开位置清单
    assert 'data-testid="findings-type-card"' in tsx
    assert "{card.title} × {card.count} 处" in tsx
    assert "展开位置清单" in tsx
    # 一键修复本类:批量循环 + 进度显示
    assert "一键修复本类" in tsx
    assert "修复中 ${progress.done}/${progress.total}" in tsx
    # 失败治理:原因可见 + 单处重试 + 成功/失败计数
    assert 'data-testid="span-repair-failure"' in tsx
    assert "重试这一处" in tsx
    assert "成功 {summary.ok} · 失败 {summary.fail}" in tsx
    # 全类操作
    assert "全部忽略本类" in tsx
    assert "本类已确认" in tsx
    assert "恢复显示" in tsx
    # 后端聚合优先,客户端兜底
    assert "topic.findings_aggregate" in tsx
    assert "aggregateFindingsClientSide" in tsx


def test_frontend_no_inline_style_in_type_card_component():
    tsx = _TSX.read_text(encoding="utf-8")
    start = tsx.index("function ArticleEvidenceAdvisory")
    end = tsx.index("// 写作进度类型", start)
    segment = tsx[start:end]
    assert "style={{" not in segment, "v7 规则:禁 inline style"
    assert "<style" not in segment


def test_frontend_call_sites_pass_on_repaired():
    tsx = _TSX.read_text(encoding="utf-8")
    usages = tsx.count("<ArticleEvidenceAdvisory")
    with_reload = tsx.count("onRepaired={() => { if (selectedProject) void loadProjectDetail(selectedProject.id); }}")
    assert usages == 3
    # 3 处 ArticleEvidenceAdvisory + 3 处 ArticleLegalFindings 都要能刷新
    assert with_reload >= 6
