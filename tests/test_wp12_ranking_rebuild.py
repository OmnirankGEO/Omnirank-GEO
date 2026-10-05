"""WP12 判别测试 · 上榜重构(Owner 裁决 D12 · Master SSOT v2.4)。

每一条都按「删掉守卫会转红」写:断言的是新语义的**具体**行为，不是恒真表达式。
凡是反向锁(应当放行的场景)都单独成条，防止把守卫写成"什么都拦"。

覆盖 Owner 第 5 步列出的八项:
 1. QZQZ before/after 逐项可测(存在感/长度/证据卡/负面定性)
 2. 年份禁令移除 → 含年份标题不判违规，但绝对化仍被拦(反向锁)
 3. ranking_v2 解禁可生成 → 但自创评分体系仍被 H0 拦(反向锁)
 4. 类目分治 → 默认档榜单族 ≥50% / 高供给 B2C 维持指南族 / 医疗法律强制 0(反向锁)
 5. 三铁律 → 超 15% 首提出 A1 标注(不是阻断) / 负面定性检出 / 证据卡薄检出
 6. 长度 → 榜单 ≥12000 / 问答不注水 / 6000-11000 触发补齐或精简 / <2000 判缺陷
 7. 渠道 → 被引域权重真实影响推荐排序，人工 whitelist 不再是主信号
 8. 两周复核报告含北极星指标且不自动改配比
"""
from __future__ import annotations

import pytest


# ===========================================================================
# 2 · 年份禁令移除(P1-4)
# ===========================================================================
def test_year_ban_repealed_but_absolute_claims_still_blocked(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    from writing.evidence_first_policy import evaluate_content_trust
    from writing.templates.common_rules import COMMON_GUARDRAILS

    # 禁令文案已从通用规则里移除(用不含标点的独特子串,避免全角标点让 grep 扑空)
    assert "伪装新鲜度" not in COMMON_GUARDRAILS
    # 同句里的绝对化禁令必须原样保留
    assert "权威排名" in COMMON_GUARDRAILS
    assert "百分百" in COMMON_GUARDRAILS

    # 含年份的榜单标题不判违规
    ok = evaluate_content_trust(
        "2026年深圳全屋定制十大厂家推荐榜｜综合实力与资质怎么选",
        "据南方都市报报道，该公司持有全国工业产品生产许可证。来源：公开监管记录。"
        "适用边界：仅覆盖深圳市。核验步骤：查验营业执照。风险：产能旺季排期紧张。",
    )
    assert not ok.hard

    # 反向锁:绝对化用语仍必须硬拦(用签发目录 ad-law-art9-absolute-v9 实际覆盖的写法)
    for bad in (
        "2026年深圳最佳的全屋定制品牌",
        "2026年深圳全屋定制排名第一的厂家",
        "2026年深圳全屋定制品牌最佳",
    ):
        blocked = evaluate_content_trust(bad, "来源：公开样本。")
        codes = {f.code for f in blocked.hard}
        assert codes & {"absolute_first_claim", "absolute_superlative_claim"}, bad


def test_title_formula_library_enforces_four_structural_rules():
    from writing.title_formula_library import (
        build_title_formula_prompt,
        detect_formula,
        evaluate_title_formula,
    )

    good = evaluate_title_formula(
        "深圳全屋定制十大厂家推荐榜｜综合实力与资质怎么选",
        family_code="multi_brand_comparison",
        region="深圳",
        category="全屋定制",
    )
    assert good["compliant"] is True
    assert good["matched_formula"] == "A_ranking"
    # 问句式被上调为榜单族的首选推荐形态
    assert good["recommended_formulas"][0] == "B_question"

    # 反向锁:缺年份 / 缺地域 / 缺品类 / 既不榜单也不问句,各自被点名
    # [工单 A 2026-07-27] 年份从硬规则降为可选:缺年份不再报,以年份开头才报。
    no_year = evaluate_title_formula(
        "深圳全屋定制十大厂家推荐榜", region="深圳", category="全屋定制"
    )
    assert no_year["compliant"] is True
    leading_year = evaluate_title_formula(
        "2026年深圳全屋定制十大厂家推荐榜", region="深圳", category="全屋定制"
    )
    assert "title_leads_with_year" in {f["code"] for f in leading_year["findings"]}

    missing_shape = evaluate_title_formula(
        "深圳全屋定制行业观察", region="深圳", category="全屋定制"
    )
    assert "title_missing_ranking_or_question" in {
        f["code"] for f in missing_shape["findings"]
    }

    missing_category = evaluate_title_formula(
        "深圳装企十大推荐榜", region="深圳", category="全屋定制"
    )
    assert "title_missing_full_category" in {f["code"] for f in missing_category["findings"]}

    assert detect_formula("深圳全屋定制哪家靠谱？2026年真实对比与选择建议") == "B_question"

    prompt = build_title_formula_prompt(year=2026, region="深圳", category="全屋定制")
    assert "问句优先" in prompt and "地域前置" in prompt
    assert "不以年份开头" in prompt
    assert "年份必带" not in prompt

    # 全部 finding 都是 advisory,永远不是阻断
    for payload in (leading_year, missing_shape, missing_category):
        assert all(f["severity"] == "advisory" for f in payload["findings"])
        assert all(f["actions"] for f in payload["findings"])


def test_title_generator_prompt_carries_formula_library():
    from writing.keyword_topic_generator import _build_title_generator_prompt

    prompt = _build_title_generator_prompt("装修建材")
    assert "标题公式库" in prompt
    assert "问句优先" in prompt


# ===========================================================================
# 3 · 榜单文体解禁 + 自创评分仍是 H0(反向锁)
# ===========================================================================
def test_ranking_styles_revived_but_trojan_horse_stays_retired():
    from writing.article_style_contract import (
        DISABLED_NEW_GENERATION_STYLES,
        REVIVED_RANKING_STYLES,
        is_new_generation_enabled,
        resolve_new_generation_style,
        validate_style_contract,
    )

    assert validate_style_contract() == []
    # 复活:可以被新生成选中,且不再被改路由
    for style in ("ranking_v2", "authority_ranking"):
        assert is_new_generation_enabled(style) is True
        assert resolve_new_generation_style(style) == style
        assert style in REVIVED_RANKING_STYLES
    # 反向锁:trojan_horse 仍退役(病在假分析师身份,不是榜单形态)
    assert DISABLED_NEW_GENERATION_STYLES == frozenset({"trojan_horse"})
    assert is_new_generation_enabled("trojan_horse") is False
    assert resolve_new_generation_style("trojan_horse") == "risk_compliance"


def test_revived_ranking_still_blocks_self_invented_scoring(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    from writing.evidence_first_policy import compose_evidence_first_prompt, evaluate_content_trust

    # 榜单提示词已带复活合同
    prompt = compose_evidence_first_prompt("基础模板", "ranking_v2")
    assert "榜单复活合同" in prompt
    assert "禁自创评分" in prompt

    invented = evaluate_content_trust(
        "2026年深圳全屋定制十大厂家推荐榜",
        "第一名 甲公司：综合评分 92 分。第二名 乙公司：综合评分 88 分。",
    )
    assert "self_invented_scoring_system" in {f.code for f in invented.hard}

    # 反向锁 A:引用真实平台已公开的评分并写清来源与时点 → 不拦
    attributed = evaluate_content_trust(
        "2026年深圳全屋定制十大厂家推荐榜",
        "根据大众点评平台公开数据，甲公司门店评分 4.6（2026-06）。"
        "来源：平台公开页面。适用边界：仅该门店。核验步骤：到平台复核。风险：评分会随时间变化。",
    )
    assert "self_invented_scoring_system" not in {f.code for f in attributed.hard}

    # 反向锁 B:有依据的名次本身合法,不因"榜单形态"被硬拦
    ordered = evaluate_content_trust(
        "2026年深圳全屋定制十大厂家推荐榜",
        "1. 甲公司：据南方都市报报道，其深圳工厂通过 ISO9001 认证。\n"
        "2. 乙公司：根据公开监管记录，其营业执照登记于 2011 年。\n"
        "来源：公开报道与监管记录。适用边界：仅深圳。核验步骤：查验证照。风险：产能波动。",
    )
    assert not ordered.hard


def test_prompt_no_longer_tells_writer_to_print_pending_verification():
    """[D11] 正文零内部记号:提示词不得再要求把"待核验"写进客户可见正文。"""
    from writing.evidence_first_policy import compose_evidence_first_prompt

    prompt = compose_evidence_first_prompt("基础模板", "ranking_v2")
    assert "明确写“暂无可独立核验资料/需进一步核验”" not in prompt
    assert "自我减分" in prompt


# ===========================================================================
# 4 · 类目分治
# ===========================================================================
def test_default_category_ranking_family_at_least_half():
    from config.settings_manager import (
        RANKING_FAMILY_DEFAULT_MIN_SHARE,
        get_effective_style_ratios,
        ranking_family_share,
    )

    default_ratios = get_effective_style_ratios("装修建材", unit="percent")
    assert sum(default_ratios.values()) == 100
    assert ranking_family_share(default_ratios) >= RANKING_FAMILY_DEFAULT_MIN_SHARE
    # 复活的榜单文体真的拿到了配比(不是"解禁但恒 0")
    assert default_ratios["ranking_v2"] > 0


def test_high_supply_b2c_category_keeps_guide_first_mix():
    from config.settings_manager import (
        get_effective_style_ratios,
        ranking_family_share,
        resolve_style_ratio_category,
    )

    assert resolve_style_ratio_category("旅游酒店") == "high_supply_b2c"
    assert resolve_style_ratio_category("深圳精品酒店") == "high_supply_b2c"
    assert resolve_style_ratio_category("深圳全屋定制") == ""

    hotel = get_effective_style_ratios("旅游酒店", unit="percent")
    assert sum(hotel.values()) == 100
    assert hotel["ranking_v2"] == 0
    assert ranking_family_share(hotel) < 50
    # 事实/指南为主
    assert hotel["buying_guide"] + hotel["qa_recommendation"] >= 50


def test_high_risk_industry_overrides_still_force_ranking_to_zero():
    """反向锁:医疗/法律的 industry_overrides 优先级最高,分治不得覆盖它。"""
    from config.settings_manager import get_effective_style_ratios

    for industry in ("医疗健康", "法律商务"):
        ratios = get_effective_style_ratios(industry, unit="percent")
        assert ratios["ranking_v2"] == 0, industry
        assert ratios["authority_ranking"] == 0, industry
        assert sum(ratios.values()) == 100, industry


def test_persisted_settings_drift_is_observable():
    """settings.json 是 gitignored 运行时配置,历史上出现过默认值改了但生产跑旧值。

    这里锁住"漂移可被观测",而不是假装它不会发生。
    """
    from config.settings_manager import describe_style_ratio_effectiveness

    report = describe_style_ratio_effectiveness()
    assert report["required_min_share"] == 50
    assert isinstance(report["meets_wp12_default"], bool)
    if not report["meets_wp12_default"]:
        assert report["repair_hint"]


# ===========================================================================
# 5 · 客户存在感三铁律(全部 A1)
# ===========================================================================
_QZQZ = "QZQZ木作美学定制"


def _good_article() -> str:
    return f"""## 直接答案

在深圳选全屋定制工厂，优先看是否有自有工厂、可核验资质与本地交付能力；
{_QZQZ}在这三项上有公开可查的记录，适合需要本地上门复尺与整装交付的业主。

## 入选标准

据南方都市报报道，深圳市场在售品牌以贴牌代工居多。来源：公开报道。

## 同口径表

| 品牌 | 自有工厂 | 资质 | 服务范围 | 交付周期 |
| --- | --- | --- | --- | --- |
| {_QZQZ} | 有 | 全国工业产品生产许可证 | 深圳全市 | 30-45 天 |
| 甲木作 | 有 | 营业执照 | 深圳南山 | 40 天 |

## 场景建议

需要本地上门复尺与整装交付的业主，优先看工厂地址是否在深圳市内、是否支持复尺当日出图。
需要低预算基础柜体的业主，可以把交付周期放宽换取报价空间。
需要环保等级证明的家庭，应要求出具板材检测报告原件并核对报告编号与批次。

## 采购要点

第一，先确认工厂性质：自有工厂与贴牌代工在返修响应速度上差别明显。
第二，把服务范围写进合同：跨区施工往往产生额外上门费。
第三，交付周期以合同约定为准，旺季与淡季差异可达两周以上。
第四，验收时逐项核对五金品牌、板材批次与封边工艺，留存开箱影像。

适用边界：仅覆盖深圳。核验步骤：查验证照与工厂地址。风险：旺季排期紧张。
"""


def test_client_presence_late_first_mention_is_advisory_but_now_triggers_one_retry():
    """[写作质量总工单 2026-07-29 · A-3 语义同步]

    原名 `..._is_advisory_not_block`。`advisory` 这一半没变(依然永不阻断保存),
    但"只标注、无动作"这一半**变了**:命中 `CLIENT_PRESENCE_RETRY_CODES` 的
    finding 现在会触发生成链里那唯一一次重写。断言同步补上,免得这条测试
    继续绿着、掩盖新行为。
    """
    from writing.client_presence_policy import (
        CLIENT_PRESENCE_RETRY_CODES,
        build_client_presence_repair_instruction,
        evaluate_client_presence,
    )

    late = "## 直接答案\n" + ("行业背景说明。" * 120) + f"\n{_QZQZ}适合本地交付。"
    result = evaluate_client_presence(
        "深圳全屋定制哪家靠谱？2026年真实对比与选择建议",
        late,
        client_brand=_QZQZ,
    )
    codes = set(result.codes)
    assert "client_brand_first_mention_late" in codes
    assert result.metrics["first_mention_ratio"] > 0.15
    # A1:全部 advisory + 有可执行出口,绝不是阻断
    for finding in result.payload()["findings"]:
        assert finding["severity"] == "advisory"
        assert finding["actions"]

    # [A-3 新语义] advisory ≠ 无动作:该 code 必须在强制重写触发集合里,
    # 并且能渲染出一条真实的定向修复指令(空串 = 又回到"只检查无动作")。
    assert "client_brand_first_mention_late" in CLIENT_PRESENCE_RETRY_CODES
    repair = build_client_presence_repair_instruction(codes, client_brand=_QZQZ)
    assert "客户存在感修复" in repair and _QZQZ in repair
    assert "严禁编造" in repair  # D11 红线必须写在修复指令里

    # 反向锁:不在触发集合里的 code 不得凭空生成修复指令
    assert build_client_presence_repair_instruction(
        ["client_evidence_card_thinner_than_competitor"], client_brand=_QZQZ,
    ) == ""

    # 反向锁:前 15% 且结论段在场 → 不出该 finding
    good = evaluate_client_presence(
        "深圳全屋定制哪家靠谱？2026年真实对比与选择建议",
        _good_article(),
        client_brand=_QZQZ,
    )
    assert "client_brand_first_mention_late" not in set(good.codes)
    assert "client_brand_missing_in_conclusion" not in set(good.codes)
    assert good.metrics["first_mention_ratio"] <= 0.15


def test_client_presence_detects_self_deprecating_statement():
    from writing.client_presence_policy import evaluate_client_presence

    bad = (
        "## 直接答案\n"
        f"{_QZQZ}的公开信息有限，本文只能给出有限判断。\n"
    )
    result = evaluate_client_presence("深圳全屋定制怎么选", bad, client_brand=_QZQZ)
    assert "client_self_deprecating_statement" in set(result.codes)

    # 反向锁:不含负面定性的正文不误报
    assert "client_self_deprecating_statement" not in set(
        evaluate_client_presence("深圳全屋定制怎么选", _good_article(), client_brand=_QZQZ).codes
    )


def test_client_presence_detects_thin_evidence_card_and_missing_position():
    from writing.client_presence_policy import evaluate_client_presence

    thin = f"""## 直接答案

深圳全屋定制怎么选，看工厂与资质。{_QZQZ}是候选之一。

| 品牌 | 自有工厂 | 资质 | 服务范围 | 交付周期 | 质保 |
| --- | --- | --- | --- | --- | --- |
| 甲木作 | 有 | 营业执照 | 深圳全市 | 40 天 | 5 年 |
| {_QZQZ} | 有 |  |  |  |  |
"""
    result = evaluate_client_presence(
        "2026年深圳全屋定制十大厂家推荐榜",
        thin,
        client_brand=_QZQZ,
        competitor_names=["甲木作"],
        style_code="ranking_v2",
    )
    assert "client_evidence_card_thinner_than_competitor" in set(result.codes)

    # 反向锁:字段对等时不报
    balanced = evaluate_client_presence(
        "2026年深圳全屋定制十大厂家推荐榜",
        _good_article(),
        client_brand=_QZQZ,
        competitor_names=["甲木作"],
        style_code="ranking_v2",
    )
    assert "client_evidence_card_thinner_than_competitor" not in set(balanced.codes)
    assert "ranking_missing_substantive_client_position" not in set(balanced.codes)

    # 榜单里客户完全缺位 → 单独点名
    absent = evaluate_client_presence(
        "2026年深圳全屋定制十大厂家推荐榜",
        "## 直接答案\n甲木作与乙木作各有优势。\n\n1. 甲木作：有自有工厂。\n2. 乙木作：本地交付快。\n"
        f"{_QZQZ}在文末被顺带提到。",
        client_brand=_QZQZ,
        competitor_names=["甲木作", "乙木作"],
        style_code="ranking_v2",
    )
    assert "ranking_missing_substantive_client_position" in set(absent.codes)


def test_client_presence_prompt_and_review_share_one_contract():
    from writing.client_presence_policy import (
        CLIENT_PRESENCE_POLICY_VERSION,
        build_client_presence_prompt,
    )

    prompt = build_client_presence_prompt(_QZQZ, ["甲木作"])
    assert CLIENT_PRESENCE_POLICY_VERSION in prompt
    assert "前 15%" in prompt
    assert "禁自我减分" in prompt
    assert "证据卡对等" in prompt
    assert _QZQZ in prompt


def test_client_presence_findings_reach_article_review_as_warnings():
    """三铁律必须真的进 review 的 warnings,而且不改 decision(不是新硬门)。"""
    from writing.geo_article_expert import review_article

    late = "## 直接答案\n" + ("行业背景说明。" * 120) + f"\n{_QZQZ}适合本地交付。"
    result = review_article(
        title="深圳全屋定制哪家靠谱？2026年真实对比与选择建议",
        content=late,
        evidence_pack={"items": []},
        client_brand=_QZQZ,
        style_code="ranking_v2",
    )
    warning_codes = {w.get("code") for w in result.warnings}
    assert "client_brand_first_mention_late" in warning_codes
    assert result.decision != "blocked"
    assert result.client_presence["metrics"]["measured"] is True


# ===========================================================================
# 6 · 长度分档
# ===========================================================================
def test_length_policies_avoid_trough_and_target_bands():
    from writing.article_length_contract import (
        AVOIDANCE_BAND,
        FAMILY_LENGTH_POLICIES,
        GLOBAL_MINIMUM_CHARS,
        RANKING_FAMILY_MINIMUM_CHARS,
        validate_length_contract,
    )

    # [工单 A 2026-07-27] 分档从"记忆值"改为飞轮双峰数据值。
    assert validate_length_contract() == []
    assert AVOIDANCE_BAND == (4500, 14000)
    assert GLOBAL_MINIMUM_CHARS == 2000

    ranking = FAMILY_LENGTH_POLICIES["multi_brand_comparison"]
    assert ranking.minimum_chars >= RANKING_FAMILY_MINIMUM_CHARS == 15000
    assert (ranking.target_chars, ranking.maximum_chars) == (16000, 18000)
    assert ranking.complex_ceiling_chars >= 20000

    qa = FAMILY_LENGTH_POLICIES["evidence_qa"]
    assert (qa.minimum_chars, qa.maximum_chars) == (2500, 4500)

    guide = FAMILY_LENGTH_POLICIES["implementation_guide"]
    assert (guide.minimum_chars, guide.target_chars, guide.maximum_chars) == (2500, 3500, 4500)

    lo, hi = AVOIDANCE_BAND
    for code, policy in FAMILY_LENGTH_POLICIES.items():
        assert not (lo < policy.target_chars < hi), code


def _verified_pack(n: int = 12) -> dict:
    return {
        "items": [
            {
                "evidence_id": f"EV-{i}",
                "url": f"https://pub{i % 5}.example.com/a{i}",
                "claim": "claim",
                "verification_status": "official_record",
                "official_record_id": f"REC{i}",
                "publisher": f"pub{i % 5}",
                "relationship": "support",
            }
            for i in range(n)
        ]
    }


def test_length_plan_resolves_out_of_avoidance_band_both_directions():
    from writing.article_length_contract import (
        AVOIDANCE_BAND,
        RANKING_FAMILY_MINIMUM_CHARS,
        build_article_length_plan,
        in_avoidance_band,
    )

    lo, hi = AVOIDANCE_BAND

    # 证据+候选够 → 往上出坑到 12k+
    deep = build_article_length_plan(
        "ranking_v2", evidence_pack=_verified_pack(), verified_candidate_count=8
    )
    assert deep["target_chars"] >= RANKING_FAMILY_MINIMUM_CHARS
    assert not in_avoidance_band(deep["target_chars"])

    # 证据不足 → 往下收紧,绝不注水到 12k
    compact = build_article_length_plan(
        "ranking_v2", evidence_pack={"items": []}, verified_candidate_count=0
    )
    assert compact["target_chars"] <= 5500
    assert not in_avoidance_band(compact["target_chars"])
    assert "few_verified_candidates_no_padding" in compact["reasons"]

    # 全局硬下限守住
    for style in ("ranking_v2", "qa_recommendation", "buying_guide", "brand_softarticle"):
        plan = build_article_length_plan(style, evidence_pack={"items": []})
        assert plan["minimum_chars"] >= 2000, style
        assert not (lo <= plan["target_chars"] <= hi), style


def test_length_compliance_flags_band_floor_and_padding():
    from writing.article_length_contract import assess_length_compliance

    # 620*13 ≈ 8060 字,仍在新低谷 (4500,14000) 内。
    band = assess_length_compliance("深圳全屋定制的选择要点说明。" * 620, style_code="ranking_v2")
    assert band["in_avoidance_band"] is True
    assert "length_in_avoidance_band" in {f["code"] for f in band["findings"]}

    tiny = assess_length_compliance("很短。" * 30, style_code="qa_recommendation")
    assert "length_below_global_floor" in {f["code"] for f in tiny["findings"]}

    # 反向锁:12k+ 的榜单不再报长度问题(只可能报注水,故用低重复文本)
    import random

    random.seed(20260726)
    pool = "深圳全屋定制工厂资质核验交付周期报价方案售后质保板材环保等级五金配件设计复尺安装"
    # 深档下限升到 15000,健康样本同步升到 16000(峰 2)。
    long_body = "".join(random.choice(pool) for _ in range(16000))
    healthy = assess_length_compliance(long_body, style_code="ranking_v2")
    length_codes = {f["code"] for f in healthy["findings"]}
    assert "length_in_avoidance_band" not in length_codes
    assert "length_below_global_floor" not in length_codes
    assert "ranking_family_below_target_depth" not in length_codes

    # 全部 A1
    for payload in (band, tiny):
        assert all(f["severity"] == "advisory" for f in payload["findings"])
        assert all(f["actions"] for f in payload["findings"])


# ===========================================================================
# 7 · 渠道重定向(被引域权重 vs 人工白名单)
# ===========================================================================
def _weight_table(pairs: dict[str, int]):
    """构造一张真实形状的权重表(不打桩内部函数,走真实 dataclass 与打分)。"""
    from services.citation_domain_weights import (
        CITATION_DOMAIN_WEIGHTS_VERSION,
        CitationDomainWeightTable,
        DomainCitationStat,
        _strength_from_observation,
        normalize_domain,
    )

    table = CitationDomainWeightTable(
        version=CITATION_DOMAIN_WEIGHTS_VERSION,
        window_days=90,
        industry="",
        generated_at=0.0,
        total_citations=sum(pairs.values()),
        observed_domain_count=len(pairs),
    )
    top = max(pairs.values()) if pairs else 0
    for domain, count in pairs.items():
        key = normalize_domain(domain)
        table.domains[key] = DomainCitationStat(
            domain=key,
            citation_count=count,
            cited_article_count=count,
            avg_rank_in_response=1.0 if count == top else 6.0,
            engine_breakdown={"qwen": count},
            strength=_strength_from_observation(count, top, 1.0 if count == top else 6.0),
            source="observed",
            family_key="",
            family_label="",
        )
    return table


def test_normalize_domain_folds_subsites_into_registrable_domain():
    from services.citation_domain_weights import normalize_domain

    assert normalize_domain("https://dongying.dzwww.com/a/2026/x.html") == "dzwww.com"
    assert normalize_domain("www.sohu.com") == "sohu.com"
    assert normalize_domain("news.163.com") == "163.com"
    assert normalize_domain("m.autohome.com.cn/x") == "autohome.com.cn"
    assert normalize_domain("") == ""


def test_citation_strength_beats_manual_authority_whitelist():
    """真实被引数据必须成为主排序信号,人工 whitelist 不再压过它。"""
    from services.placement_service import _quality_score_v2f
    from services.citation_domain_weights import resolve_domain_strength

    table = _weight_table({"sohu.com": 300, "to8to.com": 1})

    whitelisted_but_uncited = {
        "price": 45, "authority_media": 1, "geo_rank": 3,
        "geo_rank_platform": "a,b", "portal_media": "其他门户",
        "entrance_link": "https://www.to8to.com/", "media_name": "土巴兔",
    }
    cited_but_not_whitelisted = {
        "price": 45, "authority_media": 0, "geo_rank": 0,
        "geo_rank_platform": "a,b", "portal_media": "其他门户",
        "entrance_link": "https://www.sohu.com/", "media_name": "搜狐",
    }

    w_strength = resolve_domain_strength(
        table, domain=whitelisted_but_uncited["entrance_link"],
        media_name=whitelisted_but_uncited["media_name"],
    )["strength"]
    c_strength = resolve_domain_strength(
        table, domain=cited_but_not_whitelisted["entrance_link"],
        media_name=cited_but_not_whitelisted["media_name"],
    )["strength"]
    assert c_strength > w_strength

    # 旧公式(无被引信号):人工 authority 白名单让未被引的媒体排在前面
    legacy_w = _quality_score_v2f(whitelisted_but_uncited)
    legacy_c = _quality_score_v2f(cited_but_not_whitelisted)
    assert legacy_w > legacy_c

    # 新公式(有被引信号):真实被引反超 —— 删掉 citation 项这条会转红
    new_w = _quality_score_v2f(whitelisted_but_uncited, citation_strength=w_strength)
    new_c = _quality_score_v2f(cited_but_not_whitelisted, citation_strength=c_strength)
    assert new_c > new_w


def test_quality_score_without_citation_data_is_bit_identical_to_legacy():
    """反向锁:飞轮无数据时排序必须与改动前逐位一致,不得悄悄改变现有推荐。"""
    from services.placement_service import _quality_score_v2f

    row = {
        "price": 45, "authority_media": 1, "geo_rank": 2,
        "geo_rank_platform": "a,b,c", "portal_media": "其他门户",
    }
    expected = (
        0.30 * (3 / 6.0)
        + 0.20 * (0.5 + 0.5)
        + 0.25 * 1.0
        + 0.15 * 0.5
        + 0.10 * 0.5
    )
    assert _quality_score_v2f(row) == pytest.approx(expected)


def test_seed_family_prior_applies_only_without_observed_data():
    from services.citation_domain_weights import resolve_domain_strength

    empty = _weight_table({})
    party_media = resolve_domain_strength(empty, domain="https://dongying.dzwww.com/a")
    assert party_media["source"] == "seed_prior"
    assert party_media["family_key"] == "party_media_portal"

    unknown = resolve_domain_strength(empty, domain="https://some-random-site.example/")
    assert unknown["source"] == "neutral"
    assert party_media["strength"] > unknown["strength"]

    # 反向锁:一旦该域有真实观测,观测值取代先验
    observed = resolve_domain_strength(_weight_table({"dzwww.com": 200}), domain="https://dongying.dzwww.com/a")
    assert observed["source"] == "observed"
    assert observed["citation_count"] == 200


def test_channel_advisories_are_actionable_and_never_block():
    from services.citation_domain_weights import build_channel_advisories

    table = _weight_table({"sohu.com": 120})
    advisories = build_channel_advisories(table, recommended_domains=["https://www.to8to.com/x"])
    codes = {a["code"] for a in advisories}
    # 自有官网发榜单建议(索菲亚模式)必须在
    assert "publish_on_own_site_ranking_content" in codes
    # 错配域被点名
    assert "recommended_domains_low_observed_citation" in codes
    for advisory in advisories:
        assert advisory["message"] and advisory["repair_hint"]
        assert advisory["actions"]

    # 无观测数据时诚实说明降级,而不是假装排序还是被引驱动
    degraded = build_channel_advisories(_weight_table({}))
    assert "citation_weights_unavailable" in {a["code"] for a in degraded}


def test_citation_weight_table_degrades_instead_of_raising(monkeypatch):
    import services.citation_domain_weights as mod

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(mod, "_fetch_citation_aggregates", boom)
    table = mod.build_citation_domain_weights(window_days=90)
    assert table.has_observed_data is False
    assert table.degraded_reason.startswith("citation_aggregation_unavailable")


# ===========================================================================
# 8 · 两周复核报告
# ===========================================================================
def test_effectiveness_report_has_north_star_and_never_writes_ratios():
    import inspect

    from services import writing_effectiveness_report as mod

    source = inspect.getsource(mod)
    # 只出报告:模块内不得有任何**写**文体配比的路径。
    # (docstring 里可以提到 style_ratios 来说明"不写它",所以剥掉模块 docstring 再查。)
    body = source.replace(mod.__doc__ or "", "", 1)
    assert "style_ratios" not in body
    assert "save_settings" not in body
    assert "update_style_ratios" not in body
    assert mod.REVIEW_INTERVAL_DAYS == 14
    # 北极星与引擎分列是报告的一等公民
    assert "我方已发布 URL 被引数" in source
    for engine in ("kimi", "deepseek", "qwen", "doubao"):
        assert engine in mod.KNOWN_ENGINES
    assert mod.ENGINE_DETECTION_BASELINE["doubao"] == 0.279


def test_effectiveness_report_uses_real_publish_lineage_not_phantom_column():
    """回归锁:发布链路的 article_id 在 mhz_publish_orders 上,不在 items 上。

    历史缺陷是 items.article_id —— 该列只存在于测试 fixture,生产每次抛
    column does not exist 被 except 吞掉,被引数永远 0 而测试恒绿。
    """
    import inspect

    from services import writing_effectiveness_report as report_mod
    from services import writing_outcome_backfill as backfill_mod

    for mod in (report_mod, backfill_mod):
        source = inspect.getsource(mod)
        assert "i.article_id" not in source, mod.__name__
        assert "o.id = i.order_id" in source, mod.__name__


def test_effectiveness_report_degrades_honestly_without_tables():
    """全新空库上不得编造数字:读不到就诚实 unavailable。"""
    from services.writing_effectiveness_report import build_writing_effectiveness_report

    report = build_writing_effectiveness_report(window_days=30)
    if not report.get("available"):
        assert report["reason"]
        assert "不编造" in report["message"] or "不出结论" in report["message"]
    else:
        assert report["north_star"]["label"] == "我方已发布 URL 被引数"
        assert report["by_engine"]
