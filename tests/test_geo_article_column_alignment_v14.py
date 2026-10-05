"""Cross-layer contract tests for the GEO six-family writing column."""
from collections import Counter
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FAMILIES = (
    "evidence_qa",
    "multi_brand_comparison",
    "implementation_guide",
    "trend_policy_risk",
    "case_data_roi",
    "company_facts",
)
EXPECTED_LABELS = (
    "证据型问答",
    "选购与多品牌比较",
    "方法与实施指南",
    "趋势、政策与风险分析",
    "案例、数据与 ROI",
    "企业事实与品牌说明",
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_six_family_contract_is_complete_and_self_consistent():
    from writing.article_style_contract import (
        DISABLED_NEW_GENERATION_STYLES,
        STYLE_FAMILIES,
        USER_CHOICE_OPTIONS,
        validate_style_contract,
    )

    assert tuple(STYLE_FAMILIES) == EXPECTED_FAMILIES
    assert tuple(family.name for family in STYLE_FAMILIES.values()) == EXPECTED_LABELS
    assert USER_CHOICE_OPTIONS == frozenset(("auto", *EXPECTED_FAMILIES))
    # [WP12/D12 收口 · 2026-07-28] 退役集 3→1：ranking_v2 / authority_ranking 已
    # 复活（附 RANKING_REVIVAL_CONTRACT），仅 trojan_horse 保持退役——它的病是
    # 假分析师身份，不是榜单形态。`scripts/validate_geo_article_v14.py` 已于
    # 2026-07-26 同步，本条是当时漏跟的最后一处。
    assert DISABLED_NEW_GENERATION_STYLES == {"trojan_horse"}
    assert validate_style_contract() == []


def test_legacy_choices_remain_readable_without_becoming_new_options():
    from writing.article_style_contract import (
        generation_style_for_user_choice,
        normalize_user_choice,
    )

    assert normalize_user_choice("price") == "case_data_roi"
    assert normalize_user_choice("comparison") == "multi_brand_comparison"
    assert generation_style_for_user_choice("price") == "data_report"
    assert normalize_user_choice("fictional") is None


def test_exact_distribution_is_assigned_to_title_slots():
    from writing.direction_distribution import build_user_choice_style_plan

    keywords = [
        {"id": 11, "keyword": "隔离器", "required_articles": 4},
        {"id": 12, "keyword": "细胞治疗", "required_articles": 2},
    ]
    requested = {
        "evidence_qa": 1,
        "multi_brand_comparison": 1,
        "implementation_guide": 1,
        "trend_policy_risk": 1,
        "case_data_roi": 1,
        "company_facts": 1,
    }
    plan = build_user_choice_style_plan(
        keywords, requested, source="operator_custom", industry="医疗健康"
    )

    assert len(plan) == 6
    assert Counter(item["user_choice"] for item in plan) == requested
    assert all(item["user_choice_source"] == "operator_custom" for item in plan)
    assert len({(item["keyword_id"], item["slot_index"]) for item in plan}) == 6


def test_fallback_titles_keep_the_same_six_family_lineage():
    from writing.keyword_topic_generator import KeywordTopicGenerator

    generator = KeywordTopicGenerator(
        [{"id": 21, "keyword": "细胞治疗药物研发和生产隔离器推荐", "required_articles": 6}],
        "测试客户",
        "医疗健康",
    )
    # [标题 AI-only 2026-08-17] `_fallback_generate` 退役 → `_pending_generate`。
    # 本锁验六族血缘(每槽的 article_style),不验标题文本。
    topics = generator._pending_generate()

    assert len(topics) == 6
    assert all(topic.get("user_choice") in EXPECTED_FAMILIES for topic in topics)
    # [标题 AI-only 2026-08-17] 系统推荐分布仍写 NULL;新增的合法取值只有一个 ——
    # 意图闸改判时留的痕(关键词带「推荐」= 选服务商意图 → 科普/趋势族被改判走)。
    # 静默改用户/系统的文体分配是不许的,所以改判必须留痕,这里把它纳入白名单。
    assert all(
        topic.get("user_choice_source") in (None, "intent_gate_reassigned")
        for topic in topics
    ), [t.get("user_choice_source") for t in topics]
    unsafe = re.compile(r"TOP\s*\d+|前\s*\d+\s*名|第一名|权威榜单|虚构", re.I)
    assert not any(unsafe.search(topic["optimized_title"]) for topic in topics)


def test_writing_hall_exposes_six_families_and_three_evidence_states():
    src = _read("frontend/src/pages/Writing/WritingHall.tsx")

    # [命名 SSOT 2026-07-28] 原断言要求 UI 标签**逐字**等于家族名。锁的真正意图是
    # "前端不许另发明分类"，而不是"不许加澄清后缀" —— Owner 明确要求在文体规划里
    # 看得见"排行榜单"回来了。改为：必须以家族名开头（同一分类），允许括号后缀。
    for code, label in zip(EXPECTED_FAMILIES, EXPECTED_LABELS):
        match = re.search(rf"value: '{code}', label: '([^']+)'", src)
        assert match, code
        assert match.group(1).startswith(label), (code, match.group(1), label)
    # 反向锁：六类之外不得出现第七个 family value
    declared = set(re.findall(r"value: '(\w+)', label: '", src))
    assert declared <= set(EXPECTED_FAMILIES) | {"auto"}, declared
    assert src.count("<TopicStyleSelector") >= 3
    assert "type CompetitorMode = 'real' | 'semi' | 'evidence_only'" in src
    assert all(label in src for label in ("已核验", "待核验", "仅写标准"))
    assert "'fictional'" not in src
    assert "AI 自己编" not in src


def test_settings_has_one_editable_taxonomy_and_publish_is_fail_closed():
    settings = _read("frontend/src/pages/Settings/SettingsPage.tsx")
    publish = _read("frontend/src/pages/Publishing/PublishCenter.tsx")
    placement = _read("services/placement_service.py")

    assert "CONTENT_ANGLES" not in settings
    assert "updateContentRatio" not in settings
    assert "GEO 文章文体比例（六类 SSOT）" in settings
    assert "publication_eligible" in publish
    assert "evaluate_publication_eligibility" in placement


def test_review_gate_defaults_off_until_explicit_canary_enable(monkeypatch):
    from services.article_review_gate import is_publication_review_gate_enabled

    monkeypatch.delenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", raising=False)
    assert is_publication_review_gate_enabled() is False

    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "true")
    assert is_publication_review_gate_enabled() is True


def test_clean_database_creates_articles_before_v14_foreign_keys():
    source = _read("db/diagnosis_db.py")

    articles_at = source.index("CREATE TABLE IF NOT EXISTS articles (")
    review_events_at = source.index("CREATE TABLE IF NOT EXISTS geo_article_review_events (")
    assignments_at = source.index("CREATE TABLE IF NOT EXISTS geo_article_experiment_assignments (")
    assert articles_at < review_events_at < assignments_at


def test_client_style_contract_test_has_no_false_green_skip():
    source = _read("tests/test_geo_v2_writing_hall.py")
    start = source.index("def test_client_api_ignores_fake_style_code():")
    end = source.find("\ndef test_", start + 1)
    body = source[start:] if end < 0 else source[start:end]

    assert "pytest.skip" not in body
    assert "except Exception" not in body


def test_historical_article_page_does_not_reopen_a_second_generation_route():
    src = _read("frontend/src/pages/Writing/WritingCenter.tsx")

    assert "历史文章" in src
    assert "navigate('/writing')" in src
    assert "重新生成文章" not in src



def test_legacy_writer_and_competitor_research_cannot_restore_unsafe_rules():
    writer = _read("writing/article_writer.py")
    generator = _read("writing/article_generator_service.py")
    server = _read("server.py")
    hall = _read("frontend/src/pages/Writing/WritingHall.tsx")

    for forbidden in ("TOP1必须是客户品牌", "虚拟竞品生成规则", "自动生成合理的虚拟竞品"):
        assert forbidden not in writer
    assert "candidate discovery now belongs to Evidence Pack" in writer
    assert "enable_search" not in writer[writer.index("async def _research_competitors"):writer.index("def _format_competitor_list")]
    assert "竞品可用虚构" not in generator
    assert '"verified": True, "source": "跳过（无API Key）"' not in server
    assert '"name_verified": False' in server
    assert 'new_mode = "semi"' in server
    assert server.index("result_mode = (") < server.index("# 持久化到数据库")
    assert "_writing_competitor_name_is_verified" in server
    assert "setCompetitorMode('real')" not in hall

def test_knowledge_docs_use_evidence_state_language():
    for relative in (
        "knowledge/system_kb/pages/writing.md",
        "knowledge/system_kb/REVIEW_ALL_26_PAGES.md",
    ):
        src = _read(relative)
        assert "竞品证据状态三态" in src
        assert "已核验" in src and "待核验" in src and "仅写标准" in src
        assert "推荐严选" not in src
        assert "严选模式" not in src

def test_no_active_legacy_template_can_restore_customer_first_or_fictional_competitors():
    root = Path(__file__).parents[1]
    active_files = [
        root / "writing" / "ranking_prompt_v9.py",
        root / "writing" / "update_ranking_list.py",
        *sorted((root / "writing" / "templates").glob("*.py")),
    ]
    forbidden = (
        "TOP1必须",
        "客户品牌必须排第一",
        "竞品可用虚构",
        "模拟第三方研究机构",
        "你是一家权威行业研究机构",
        "你是一位资深商业记者",
        "AI引用率",
        "ai_引用概率",
    )
    for path in active_files:
        source = path.read_text(encoding="utf-8")
        for phrase in forbidden:
            assert phrase not in source, f"{path.name} restores unsafe phrase: {phrase}"
