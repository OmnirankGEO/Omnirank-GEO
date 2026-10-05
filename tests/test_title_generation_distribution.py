from writing.keyword_topic_generator import KeywordTopicGenerator, _build_title_generator_prompt
from writing.article_generator_service import ArticleGeneratorService
from writing.style_registry import normalize_style_code, normalize_trusted_topic_style
from writing.title_distribution_policy import get_title_distribution_policy
from writing.topic_dispatcher import TopicDispatcher


def _ranking_title_count(topics):
    signals = ("TOP", "榜", "排名", "前十")
    return sum(
        1
        for topic in topics
        if topic.get("article_style") == "排行榜单"
        or any(signal in topic.get("optimized_title", "") for signal in signals)
    )


def test_keyword_topic_prompt_no_longer_forces_70_percent_ranking():
    prompt = _build_title_generator_prompt(industry="装修")

    assert "70-15-15" not in prompt
    assert "70%概率" not in prompt
    assert "第1篇：必须是「排行榜单」" not in prompt
    # [SSOT geo-commercial-intent-governance-v1.0 §4.4 · 2026-07-23]
    # 「有序榜单/排名类 — 0%(新内容禁用)」与「排名/推荐一律改写为
    # 条件式选型」两条中和规则已废止(归档索引 D):
    assert "有序榜单/排名类 — 0%" not in prompt
    assert "一律改写为条件式选型" not in prompt
    assert "合法核心方向" in prompt
    assert "排名依据不足" in prompt  # 依据问题走提示+人工,不改写意图


def test_keyword_topic_prompt_reads_runtime_style_ratios(monkeypatch):
    from config import settings_manager

    def fake_ratios(industry=None, *, unit):
        assert unit == "percent"
        return {
            "ranking_v2": 0,
            "authority_ranking": 0,
            "recommendation_review": 6,
            "buying_guide": 20,
            "trojan_horse": 0,
            "qa_recommendation": 10,
            "brand_softarticle": 4,
            "comparison_review": 20,
            "risk_compliance": 22,
            "price_roi": 8,
            "data_report": 10,
        }

    monkeypatch.setattr(settings_manager, "get_effective_style_ratios", fake_ratios)

    policy = get_title_distribution_policy("装修")
    prompt = _build_title_generator_prompt(industry="装修")

    assert policy["ranking_percent"] == 0
    # 配比来自运行时配置(此 fake 配 0),prompt 不再携带「永久禁用」硬文案
    assert "有序榜单/排名类 — 0%" not in prompt
    assert "新内容禁用" not in prompt


def test_topic_dispatcher_default_distribution_uses_balanced_research_ratio():
    dispatcher = TopicDispatcher({}, diagnosis_data={"industry": "装修"})

    distribution = dispatcher._format_distribution()

    assert "70-15-15" not in distribution
    assert "70%概率" not in distribution
    assert "排名/推荐商业方向" in distribution
    assert "新内容 0%" not in distribution
    assert "允许 TOP/榜/排名/前十等商业问法" in distribution
    assert "不得把客户固定第一" in distribution


def test_keyword_topic_fallback_caps_ranking_titles_for_generic_batch():
    generator = KeywordTopicGenerator(
        keywords=[{"id": 1, "keyword": "深圳装修公司", "required_articles": 14}],
        brand_name="深圳哪家装修公司靠谱",
        industry="装修",
    )

    # [标题 AI-only 2026-08-17] `_fallback_batch` 退役,继任者 `_pending_batch`
    # 产的是**待生成槽位**(文体/计划仍在,标题留空)。本锁验的是文体分配,
    # 与标题文本无关,所以直接换成继任者。
    topics = generator._pending_batch(generator.keywords)

    assert len(topics) == 14
    assert _ranking_title_count(topics) == 0


def test_db_article_style_aliases_normalize_to_canonical_codes():
    assert normalize_style_code("排行榜单") == "ranking_v2"
    assert normalize_style_code("问答FAQ") == "qa_recommendation"
    assert normalize_style_code("方法指南") == "buying_guide"
    assert normalize_style_code("对比评测") == "comparison_review"
    assert normalize_style_code("unknown-style") is None


def test_high_risk_industry_does_not_trust_ranking_topic_style():
    assert normalize_trusted_topic_style("排行榜单", "医疗健康") is None
    assert normalize_trusted_topic_style("权威榜单", "法律商务") is None
    assert normalize_trusted_topic_style("问答FAQ", "医疗健康") == "qa_recommendation"


def test_article_generation_uses_trusted_title_style_when_user_choice_is_auto():
    service = ArticleGeneratorService(quote_id=1, brand_name="深圳栖舍", industry="装修")

    assert service._resolve_style_code_for_topic({
        "user_choice": "auto",
        "style_code": "问答FAQ",
        "_trust_legacy_style": True,
    }) == "qa_recommendation"

    assert service._resolve_style_code_for_topic({
        "user_choice": "auto",
        "style_code": "排行榜单",
        "_trust_legacy_style": True,
    }) == "comparison_review"


def test_manual_user_choice_overrides_trusted_title_style():
    service = ArticleGeneratorService(quote_id=1, brand_name="深圳栖舍", industry="装修")

    assert service._resolve_style_code_for_topic({
        "user_choice": "guide",
        "style_code": "排行榜单",
        "_trust_legacy_style": True,
    }) == "buying_guide"
