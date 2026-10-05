from __future__ import annotations

import json
import inspect
from types import SimpleNamespace

import pytest

from config import settings_manager
from tools.article_generator import BatchArticleGenerator, generate_replacement_article
from writing.article_writer import ARTICLE_PROMPTS, ArticleWriter
from writing.content_cleaner import clean_llm_article
from writing.evidence_first_policy import (
    EvidenceFirstViolation,
    POLICY_MARKER,
    evaluate_content_trust,
    is_evidence_first_enabled,
    repair_recoverable_trust_issues,
    rewrite_legacy_ranking_title,
)
from writing.intent_style_map import resolve_style_code
from writing.keyword_topic_generator import KeywordTopicGenerator
from writing.ranking_prompt_v9 import get_competitor_instruction
from writing.templates import get_template_prompt
from writing.templates.common_rules import COMMON_GUARDRAILS
from writing.topic_dispatcher import _normalize_evidence_topics


def _codes(assessment):
    return {finding.code for finding in assessment.hard}


def _soft_codes(assessment):
    return {finding.code for finding in assessment.soft}


def test_rollout_switch_defaults_on_and_has_explicit_rollback(monkeypatch):
    monkeypatch.delenv("GEO_EVIDENCE_FIRST_ENABLED", raising=False)
    assert is_evidence_first_enabled() is True

    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "false")
    assert is_evidence_first_enabled() is False


def test_ranking_title_is_preserved_not_neutralized(monkeypatch):
    """[SSOT geo-commercial-intent-governance-v1.0 §4.4 · 2026-07-23]

    旧行为(把排名标题改写成「怎么选？证据核验…」知识问句)已废止
    (归档索引 D):排名/推荐是合法核心方向,标题保留购买问题的商业意图;
    绝对化用语由 evaluate_content_trust 的法律硬门统一拦截。
    """
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    title = rewrite_legacy_ranking_title("2026深圳装修公司TOP10排名", "深圳装修公司")
    assert title == "2026深圳装修公司TOP10排名"

    # 排名形态是 advisory(排序依据披露),不是硬阻断
    assessment = evaluate_content_trust("2026深圳装修公司TOP10排名", "来源：公开样本。")
    assert "ordered_ranking_title" not in _codes(assessment)
    assert "ordered_ranking_title" in _soft_codes(assessment)


def test_critical_ranking_story_and_hotel_class_are_not_false_positives(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    title = "装修公司榜单为什么不可信？广告软文识别清单"
    content = (
        "本文解释榜单软文风险。来源：公开披露与样本复核。"
        "适用边界：五星酒店项目只代表一种工程场景。"
        "核验步骤：查验司法记录和合同。"
    )

    assert rewrite_legacy_ranking_title(title) == title
    assert evaluate_content_trust(title, content).passed is True

    negated = "根据当前资料，不能称甲公司为唯一首选，也不是排名第一。"
    assert evaluate_content_trust("装修公司怎么选", negated).passed is True


def test_only_legal_absolute_claim_is_hard_others_advisory(monkeypatch):
    """[SSOT v1.0 §5.1 + WP12 P0-2] 内容硬阻断只有两类:可定位条款的法律禁止项
    (《广告法》第九条绝对化用语)与自创评分体系(编造数据,D11 真红线)。
    排名形态/匿名权威仍为 advisory + 人工继续。"""
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    assessment = evaluate_content_trust(
        "2026深圳装修公司TOP10",
        "某研究院称，甲公司稳居第一，综合评分4.9/5。",
    )

    assert _codes(assessment) == {"absolute_first_claim", "self_invented_scoring_system"}
    assert {
        "ordered_ranking_title",
        "anonymous_authority",
    }.issubset(_soft_codes(assessment))


def test_real_platform_score_with_attribution_stays_advisory(monkeypatch):
    """[WP12 P0-2 反向锁] 自创评分是 H0,但引用真实平台已公开的评分不能误伤 ——
    否则可抽取性层要求的"评分4.6(2026-06,平台A)"会被自己的守卫打死。"""
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    attributed = evaluate_content_trust(
        "深圳装修公司哪家靠谱？2026年真实对比",
        "根据大众点评平台公开数据，该门店评分4.6（2026-06）。",
    )
    assert "self_invented_scoring_system" not in _codes(attributed)
    assert attributed.passed is True

    invented = evaluate_content_trust(
        "深圳装修公司哪家靠谱？2026年真实对比",
        "本榜自行评定各家实力，甲公司综合评分92分。",
    )
    assert "self_invented_scoring_system" in _codes(invented)


def test_score_and_order_findings_are_advisory_with_located_evidence(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    body = """来源：公开网页样本。
1. 名雕装饰：专业度 96 分
2. 居众装饰：★★★★★
3. 浩天装饰：推荐指数 88 分
适用边界：仅作网页样本检查。核验步骤：查验资质与司法记录。
"""
    assessment = evaluate_content_trust("深圳装修公司怎么选", body)

    # 无法律项 → 不硬阻断;发现项 advisory 且带定位摘录(§6 产品交互合同)
    assert assessment.passed is True
    soft = _soft_codes(assessment)
    assert "ordered_brand_candidates" in soft
    assert "manufactured_score" in soft
    located = [f for f in assessment.soft if f.code in ("ordered_brand_candidates", "manufactured_score")]
    assert all(f.evidence for f in located)


def test_repair_annotates_anonymous_authority_but_keeps_ranking_format(monkeypatch):
    """[SSOT v1.0 §4.4] 自动修复只做诚实性注释(匿名权威加注),不再剥掉
    品牌编号/名次(排序形态合法,依据问题走 advisory + 人工确认)。"""
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    body = """来源：公开网页样本。
1. 甲科技公司：适合需要本地交付的项目。
2. 乙网络公司：适合需要远程协作的项目。
3. 丙服务公司：适合需要驻场支持的项目。
某研究院认为这些公司都值得优先选择。
适用边界：资料有限。核验步骤：查验合同与项目记录。
"""

    initial = evaluate_content_trust("服务商怎么选", body)
    assert {"ordered_brand_candidates", "anonymous_authority"}.issubset(_soft_codes(initial))
    assert initial.passed is True  # advisory,不阻断保存

    repaired, repaired_codes = repair_recoverable_trust_issues(body)

    assert set(repaired_codes) == {"anonymous_authority"}
    # 排序编号保留(不中和商业形态)
    assert "1. 甲科技公司" in repaired
    assert "### 甲科技公司" not in repaired
    # 🔴 [自曝清零 2026-08-10] 契约变更:后处理**不再主动追加**这句免责。
    # 它同时在清洗器黑名单里,而 `_REVIEW_LINE` 整行删 → 加一句自曝的净效果
    # 是客户看到的正文**少了一整行**。归一化保留,追加删除。
    assert "需进一步核验" not in repaired, "后处理仍在主动追加自曝"
    assert "来源未具名" not in repaired
    assert "未具名材料" in repaired, "反向对照:匿名权威归一化必须仍然生效"


def test_repair_does_not_hide_score_or_absolute_claim(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    body = """1. 甲科技公司：综合评分 98 分，稳居第一。
2. 乙网络公司：普通候选。
3. 丙服务公司：普通候选。
"""

    repaired, repaired_codes = repair_recoverable_trust_issues(body)
    assessment = evaluate_content_trust("服务商怎么选", repaired)

    # 修复不吞问题:绝对化(法律硬)仍硬阻断;
    # [WP12 P0-2] 自创的"综合评分 98 分"没有任何来源归属 → 升为 H0(编造数据)
    assert repaired_codes == ()
    assert "absolute_first_claim" in _codes(assessment)
    assert "self_invented_scoring_system" in _codes(assessment)


def test_hard_gate_scans_full_article_and_requires_claim_local_source(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    body = "来源：企业2024年年报。" + ("普通说明。" * 900) + "品牌稳居第一。"
    assert "absolute_first_claim" in _codes(evaluate_content_trust("选型指南", body))

    unrelated_source = "来源：监管公示，仅用于核验企业登记。" + ("普通说明。" * 80)
    unrelated_source += "采用服务后转化率提升63%。"
    assessment = evaluate_content_trust("选型指南", unrelated_source)
    # [SSOT v1.0] 无源数字属证据问题 → advisory(定位该数字 + 人工继续)
    assert "unsourced_outcome_number" in _soft_codes(assessment)


def test_verifiable_balanced_article_passes_hard_gate(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    content = (
        "更新时间：2026年7月。来源：企业登记公示与项目记录。"
        "本文只核验成立时间和公开项目，不推断施工质量。"
        "适用边界：资料有限，预算与工期仍需现场确认。"
        "风险与不足：投诉记录需按地区进一步复核。"
        "核验步骤：通过官方渠道查验资质、司法记录与合同条款。"
    )
    assessment = evaluate_content_trust("深圳装修公司怎么选？证据核验清单", content)

    assert assessment.passed is True
    assert assessment.hard == ()


def test_topic_normalizer_removes_paid_position_but_keeps_title(monkeypatch):
    """[SSOT v1.0 §4.4] client_position(付费排位指令)仍必须清除(伪造
    名次属事实伪装);但标题的排名形态保留,不再改写成知识问句。"""
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    topics = _normalize_evidence_topics([
        {"title": "深圳装修公司TOP10", "type": "ranking", "client_position": 1},
        {"title": "装修合同怎么审", "type": "guide", "client_position": 1},
    ])

    assert all("client_position" not in topic for topic in topics)
    assert topics[0]["type"] == "comparison"
    assert topics[0]["title"] == "深圳装修公司TOP10"


def test_intent_map_routes_ranking_to_comparison():
    assert resolve_style_code("ranking", "ranking") == "comparison_review"


def test_persisted_legacy_ratios_are_zeroed_at_runtime(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    settings = SimpleNamespace(
        content_ratios={"authority": 20, "deep_dive": 30, "checklist": 50},
        style_ratios={
            "ranking_v2": 30,
            "authority_ranking": 20,
            "comparison_review": 25,
            "buying_guide": 25,
        },
        industry_overrides={},
    )
    monkeypatch.setattr(settings_manager, "get_current_settings", lambda: settings)

    content = settings_manager.get_effective_content_ratios(unit="percent")
    styles = settings_manager.get_effective_style_ratios(unit="percent")

    assert content["authority"] == 0
    assert sum(content.values()) == 100
    # [SSOT v1.0 §4.4 · 归档索引 D] 「排行/推荐生成比例永久为 0」的运行时
    # 强制已废止:配置值原样生效,排名方向按配比生成。
    assert styles["ranking_v2"] == 30
    assert styles["authority_ranking"] == 20
    assert sum(styles.values()) == 100


def test_llm_title_parser_keeps_ranking_form_maps_style(monkeypatch):
    """[SSOT v1.0 §4.4] 解析层不再改写排名标题(保留商业意图);文体归入
    「选购与多品牌比较」家族;绝对化用语(稳居第一)由持久化边界的法律
    硬门统一拦截,不由解析层静默清洗。"""
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    generator = KeywordTopicGenerator(
        keywords=[{"id": 7, "keyword": "深圳装修公司", "required_articles": 1}],
        brand_name="示例品牌",
        industry="装修",
    )
    response = json.dumps({
        "topics": [{
            "original_keyword": "深圳装修公司",
            "optimized_title": "2026深圳装修公司TOP10，示例品牌稳居第一",
            "article_style": "排行榜单",
        }]
    }, ensure_ascii=False)

    topic = generator._parse_response(response, generator.keywords)[0]
    assert topic["article_style"] == "选购与多品牌比较"
    assert "TOP" in topic["optimized_title"]  # 排名形态保留
    # 绝对化标题会在 evaluate_content_trust 处命中法律硬门
    gate = evaluate_content_trust(topic["optimized_title"], "来源：公开样本。")
    assert "absolute_first_claim" in _codes(gate)


def test_cleaner_does_not_turn_stars_into_another_fake_score():
    cleaned = clean_llm_article("| 品牌 | 评级 |\n| 甲 | 五星推荐 |")
    assert "4.8/5" not in cleaned
    assert "五星推荐" not in cleaned
    assert "需按证据核验" in cleaned


def test_legacy_template_code_resolves_to_evidence_contract():
    prompt = get_template_prompt("ranking_list_v2")
    assert "证据" in prompt
    assert "排名、推荐、TOP 和比较问题" in prompt
    assert "不能发布付费排位或制造无依据名次" in prompt
    assert "允许结论与排序参考" in prompt
    assert "客户品牌排第一" not in prompt


@pytest.mark.asyncio
async def test_direct_article_writer_cannot_bypass_prompt_or_hard_gate(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    writer = ArticleWriter({})
    monkeypatch.setattr(writer, "_check_title_duplicate", lambda title: title)

    async def no_knowledge(_title):
        return ""

    async def no_competitors(*_args, **_kwargs):
        return []

    captured = {}

    async def unsafe_llm(system_prompt, _user_message):
        captured["system_prompt"] = system_prompt
        return "某研究院称，客户品牌稳居第一，综合评分4.9/5。"

    monkeypatch.setattr(writer, "_load_client_knowledge", no_knowledge)
    monkeypatch.setattr(writer, "_load_db_competitors", lambda _quote_id: ([], None))
    monkeypatch.setattr(writer, "_research_competitors", no_competitors)
    monkeypatch.setattr(writer, "_build_user_message", lambda _topic: "请生成")
    monkeypatch.setattr(writer, "_call_llm", unsafe_llm)

    # [D8 · Master SSOT v1.8 ②「文章层零阻断」] 草稿层不再拒存:法律/评分类
    # findings 降为定位标注 + needs_legal_fix 草稿态,保存永不失败;真正的拦截点
    # 只在对外发布边界(article_review_gate 的 legal_hard)。本测试原先锁的是
    # D8 之前的 raise 语义,在基线上已是红的 —— 按 SSOT §1.4 改测试对齐已签发
    # 语义,而不是把 D8 回退成拒存。
    result = await writer.write({"title": "装修公司怎么选", "type": "guide"})

    warning = result.get("quality_warning") or {}
    assert warning.get("needs_legal_fix") is True
    assert "evidence_legal" in warning
    hard_codes = {
        item.get("code")
        for item in (warning["evidence_legal"].get("hard") or [])
    }
    # 绝对化(《广告法》第九条)与自创评分体系(WP12 P0-2)都必须被定位出来
    assert "absolute_first_claim" in hard_codes
    assert "self_invented_scoring_system" in hard_codes

    assert POLICY_MARKER in captured["system_prompt"]


def test_replacement_gate_precedes_every_persistence_boundary():
    source = inspect.getsource(generate_replacement_article)
    gate_at = source.index("trust = evaluate_content_trust")

    assert gate_at < source.index("output_dir = Path")
    assert gate_at < source.index("INSERT INTO articles")
    assert 'f"[补发] {safe_title}"' in source


def test_batch_fallback_topics_do_not_restore_advertorial_rankings(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    generator = BatchArticleGenerator(1, {"comparison": 15})
    topics = generator._generate_fallback_topics([], "示例品牌", "装修")

    assert len(topics) == 15
    assert all(
        evaluate_content_trust(topic["title"], "").passed
        for topic in topics
    )


def test_competitor_contract_never_allows_fictional_companies(monkeypatch):
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "true")
    instruction = get_competitor_instruction("企业服务")

    assert "禁止虚构公司" in instruction
    assert "低透明（可虚构）" not in instruction
    assert "低透明行业（品牌分散，公众不熟）→ 可使用虚构名称" not in COMMON_GUARDRAILS
    assert "不得为凑数量创建化名" in COMMON_GUARDRAILS


def test_live_scenario_and_guide_prompts_do_not_force_customer_wins():
    scenario = ARTICLE_PROMPTS["scenario"]
    guide = ARTICLE_PROMPTS["guide"]

    assert "客户在表格中排第一列" not in scenario
    assert "评分适当高于竞品" not in scenario
    assert "★★★★★" not in scenario
    assert "客户排第一" not in guide
    assert "不得虚构或把客户固定首位" in guide
