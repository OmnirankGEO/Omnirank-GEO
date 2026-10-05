import json
from pathlib import Path

import pytest

from writing.keyword_topic_generator import (
    KeywordTopicGenerator,
    _build_title_generator_prompt,
)
from writing.title_keyword_alignment import (
    assess_title_keyword_alignment,
    title_anchor_from_purchased_keyword,
)


ROOT = Path(__file__).resolve().parents[1]


def _generator(keywords):
    return KeywordTopicGenerator(
        keywords=keywords,
        brand_name="示例品牌",
        industry="生命科学",
        force_chinese_style="证据型问答",
    )


def test_title_prompt_freezes_purchased_keyword_without_forcing_one_template():
    prompt = _build_title_generator_prompt("生命科学")

    assert "客户已确认的商业事实" in prompt
    assert "最具体的业务对象" in prompt
    assert "六类文体只改变回答角度，不改变客户购买的问题" in prompt
    assert "不同决策子问题" in prompt
    assert "不同真实用户意图" not in prompt
    assert '"keyword_id": "原样返回输入 keyword_id"' in prompt
    assert "只能使用以下固定标题" not in prompt


def test_alignment_accepts_natural_paraphrases_but_rejects_adjacent_topics():
    keyword = "细胞治疗药物研发和生产隔离器推荐"
    natural = assess_title_keyword_alignment(
        "2026年细胞治疗隔离器怎么选？研发与生产阶段核验清单",
        keyword,
    )
    generic = assess_title_keyword_alignment(
        "2026年生命科学行业有哪些变化？政策、风险与行动建议",
        keyword,
    )
    lost_specific_object = assess_title_keyword_alignment(
        "2026年细胞治疗药物研发有哪些变化？政策与行动建议",
        keyword,
    )

    assert natural.aligned is True
    assert generic.aligned is False
    assert lost_specific_object.aligned is False


def test_alignment_preserves_geography_and_terminal_business_object():
    keyword = "深圳全屋定制板材品牌推荐"

    assert assess_title_keyword_alignment(
        "2026年深圳全屋定制板材怎么选？同字段核验指南",
        keyword,
    ).aligned
    assert not assess_title_keyword_alignment(
        "2026年深圳全屋定制趋势解读｜行业变化与行动建议",
        keyword,
    ).aligned
    assert not assess_title_keyword_alignment(
        "2026年全屋定制板材怎么选？同字段核验指南",
        keyword,
    ).aligned


@pytest.mark.parametrize(
    ("keyword", "crossed_title"),
    [
        ("深圳医用电梯", "2026年深圳家用电梯怎么选？井道与验收指南"),
        ("工业机器人", "2026年服务机器人怎么选？应用场景核验"),
    ],
)
def test_alignment_rejects_adjacent_product_qualifier_substitution(keyword, crossed_title):
    verdict = assess_title_keyword_alignment(crossed_title, keyword)

    assert verdict.aligned is False
    assert verdict.reason == "conflicting_business_qualifier"


@pytest.mark.parametrize(
    ("keyword", "expected_anchor"),
    [
        ("深圳装修公司推荐", "深圳装修公司"),
        ("深圳隔离器厂家推荐", "深圳隔离器厂家"),
        ("深圳GEO服务商推荐", "深圳GEO服务商"),
    ],
)
def test_provider_purchase_object_is_not_stripped_with_recommendation_intent(
    keyword,
    expected_anchor,
):
    """[改写] 原实现拿兜底模板产物验"锚点在不在标题里";模板退役后
    直接验**锚点抽取器**本身,再用一条 AI 形态标题验对齐器 —— 语义不变,
    不再依赖已经不存在的模板。
    """
    assert title_anchor_from_purchased_keyword(keyword) == expected_anchor
    ai_title = f"{expected_anchor}怎么挑？先看这三份可查的资料"
    assert assess_title_keyword_alignment(ai_title, keyword).aligned


@pytest.mark.parametrize(
    ("keyword", "lossy_title"),
    [
        ("C++开发公司", "2026年C开发公司怎么选？能力核验"),
        ("C#开发公司", "2026年C开发公司怎么选？能力核验"),
        ("A/B测试服务", "2026年AB测试服务怎么选？实施指南"),
    ],
)
def test_identity_bearing_symbols_cannot_be_normalized_away(keyword, lossy_title):
    verdict = assess_title_keyword_alignment(lossy_title, keyword)

    assert verdict.aligned is False
    assert verdict.reason == "symbol_identity_drift"


def test_controlled_natural_object_alias_is_allowed_without_fuzzy_matching():
    verdict = assess_title_keyword_alignment(
        "2026年深圳货梯怎么选？载荷、井道与验收清单",
        "深圳载货电梯",
    )

    assert verdict.aligned is True
    assert verdict.reason == "controlled_object_alias"


def test_ranking_intent_preserved_and_absolute_claim_neutralised(monkeypatch):
    """[搬家] 排名/TOP/榜单是合法商业方向,只中和绝对化名次。

    原实现打在 `_safe_fallback_title` 的产物上(那里有一段
    `ABSOLUTE_RANKING_CLAIM_TERMS` 的正则中和)。兜底模板退役后,
    这条不变式的承担者回到它本来的 SSOT:`evidence_first_policy`
    —— 中和绝对化名次的词表与运行时硬门同源,一份没有第二份。
    """
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "false")
    from writing.evidence_first_policy import (
        ABSOLUTE_RANKING_CLAIM_TERMS,
        rewrite_legacy_ranking_title,
    )

    kept = rewrite_legacy_ranking_title(
        "深圳装修公司TOP10排行榜怎么挑？三项硬指标先看", "深圳装修公司TOP10排行榜",
    )
    assert "深圳装修公司" in kept
    assert ("TOP" in kept.upper()) or ("排行" in kept) or ("榜" in kept)

    # 绝对化名次词表非空(空表 = 判据恒真)
    assert len(ABSOLUTE_RANKING_CLAIM_TERMS) >= 1
    assert any(term in "第一名榜首绝对第一" for term in ABSOLUTE_RANKING_CLAIM_TERMS)


def test_correct_keyword_id_cannot_launder_an_unrelated_title():
    keywords = [{
        "id": 901,
        "keyword": "细胞治疗药物研发和生产隔离器推荐",
        "required_articles": 1,
    }]
    generator = _generator(keywords)
    response = json.dumps({
        "topics": [{
            "keyword_id": 901,
            "original_keyword": keywords[0]["keyword"],
            "slot_index": 0,
            "optimized_title": "2026年行业有哪些变化？政策、风险与行动建议",
            "article_style": "趋势、政策与风险分析",
        }]
    }, ensure_ascii=False)

    topic = generator._parse_response(response, keywords)[0]

    assert topic["keyword_id"] == 901
    assert topic["original_keyword"] == keywords[0]["keyword"]
    # [标题 AI-only 2026-08-17] 旧行为:身份冲突 → 立刻用硬编码模板"修复"成一条标题。
    # 新行为:该槽位标成待生成,交 AI 失败梯 —— **串写的标题一个字都不许落库**这条
    # 红线一字未改,变的只是"这一格暂时没有标题"而不是"这一格是模板"。
    assert topic["title_alignment_status"] == "pending_ai"
    assert topic["optimized_title"] == "", "串写的标题一个字都不许落库"
    assert "行业有哪些变化" not in str(topic["optimized_title"])
    assert topic["title_pending_reason"], "待生成必须带原因,不许空着"


def test_natural_paraphrase_is_not_replaced_by_a_hardcoded_template():
    """[强化] 本条的语义正是 Owner 2026-08-17 裁决的方向:自然改写不许被模板顶掉。
    模板退役后它变成"永远成立",但保留 —— 它是回归探针,不是恒真断言:
    只要有人把模板接回来并让它优先于 AI 产出,这条立刻红。"""
    keywords = [{
        "id": 902,
        "keyword": "细胞治疗药物研发和生产隔离器推荐",
        "required_articles": 1,
    }]
    generator = _generator(keywords)
    natural_title = "2026年细胞治疗隔离器怎么选？研发与生产阶段核验清单"
    response = json.dumps({
        "topics": [{
            "keyword_id": 902,
            "original_keyword": keywords[0]["keyword"],
            "slot_index": 0,
            "optimized_title": natural_title,
            "article_style": "证据型问答",
        }]
    }, ensure_ascii=False)

    topic = generator._parse_response(response, keywords)[0]

    assert topic["optimized_title"] == natural_title
    assert topic["title_alignment_status"] == "passed"


def test_multi_keyword_identity_conflicts_fail_closed_and_do_not_cross_save():
    keywords = [
        {"id": 11, "keyword": "深圳乘客电梯", "required_articles": 1},
        {"id": 12, "keyword": "深圳载货电梯", "required_articles": 1},
    ]
    generator = _generator(keywords)
    response = json.dumps({
        "topics": [
            {
                "keyword_id": 11,
                "original_keyword": "深圳载货电梯",
                "slot_index": 0,
                "optimized_title": "2026年深圳载货电梯怎么选？",
                "article_style": "证据型问答",
            },
            {
                "keyword_id": 12,
                "original_keyword": "深圳乘客电梯",
                "slot_index": 0,
                "optimized_title": "2026年深圳乘客电梯怎么选？",
                "article_style": "证据型问答",
            },
        ]
    }, ensure_ascii=False)

    topics = generator._parse_response(response, keywords)

    assert len(topics) == 2
    assert [(topic["keyword_id"], topic["original_keyword"]) for topic in topics] == [
        (11, "深圳乘客电梯"),
        (12, "深圳载货电梯"),
    ]
    # [标题 AI-only 2026-08-17] 旧行为:身份冲突 → 用硬编码模板"修复"。
    # 新行为:标成待生成,交 AI 失败梯。**不许跨写**这条红线一字未改 ——
    # 下面两条断言就是它:标题必须为空(模型给的那条串写标题被丢弃),
    # 而 keyword_id / original_keyword 仍是权威值。
    assert all(topic["title_alignment_status"] == "pending_ai" for topic in topics)
    assert all(topic["title_alignment_reason"] == "keyword_identity_conflict" for topic in topics)
    assert all(topic["optimized_title"] == "" for topic in topics), (
        "串写的标题被落库了 —— 跨写红线破了"
    )
    assert all("怎么选" not in str(topic["optimized_title"]) for topic in topics)


def test_unknown_model_keyword_id_can_only_recover_by_exact_authoritative_text():
    keywords = [{"id": 21, "keyword": "深圳医用电梯", "required_articles": 1}]
    generator = _generator(keywords)
    response = json.dumps({
        "topics": [{
            "keyword_id": 999999,
            "original_keyword": "深圳医用电梯",
            "slot_index": 0,
            "optimized_title": "2026年医疗行业趋势解读",
            "article_style": "趋势、政策与风险分析",
        }]
    }, ensure_ascii=False)

    topic = generator._parse_response(response, keywords)[0]

    assert topic["keyword_id"] == 21
    assert topic["title_alignment_reason"] == "keyword_identity_conflict"
    # [标题 AI-only 2026-08-17] 旧行为:身份冲突后用模板"修复"出一条对齐的标题。
    # 新行为:标成待生成交 AI 失败梯。要守的是**权威 id/文本被恢复、串写标题被丢弃**,
    # 这两条一字未改。
    assert topic["title_alignment_status"] == "pending_ai"
    assert topic["optimized_title"] == ""
    assert "医疗行业趋势解读" not in str(topic["optimized_title"])


def test_missing_model_keyword_id_recovers_exact_text_without_overwriting_title():
    keywords = [{"id": 22, "keyword": "深圳家用电梯", "required_articles": 1}]
    generator = _generator(keywords)
    response = json.dumps({
        "topics": [{
            "original_keyword": "深圳家用电梯",
            "slot_index": 0,
            "optimized_title": "深圳家用电梯怎么选？2026年井道条件与验收要点",
            "article_style": "选购与多品牌比较",
        }]
    }, ensure_ascii=False)

    topic = generator._parse_response(response, keywords)[0]

    assert topic["keyword_id"] == 22
    assert topic["optimized_title"] == "深圳家用电梯怎么选？2026年井道条件与验收要点"
    assert topic["title_alignment_status"] == "passed"


def test_keyword_id_and_symbol_bearing_keyword_text_conflict_fails_closed():
    keywords = [{"id": 23, "keyword": "C++开发公司", "required_articles": 1}]
    generator = _generator(keywords)
    response = json.dumps({
        "topics": [{
            "keyword_id": 23,
            "original_keyword": "C开发公司",
            "slot_index": 0,
            "optimized_title": "2026年C开发公司怎么选？能力核验",
            "article_style": "选购与多品牌比较",
        }]
    }, ensure_ascii=False)

    topic = generator._parse_response(response, keywords)[0]

    assert topic["original_keyword"] == "C++开发公司"
    assert topic["title_alignment_reason"] == "keyword_identity_conflict"
    # [标题 AI-only 2026-08-17] 同上:fail-closed 的方向没变,只是"这一格暂时没有标题"。
    assert topic["title_alignment_status"] == "pending_ai"
    assert topic["optimized_title"] == ""


def test_valid_empty_provider_output_keeps_existing_no_output_contract():
    keywords = [{"id": 22, "keyword": "深圳医用电梯", "required_articles": 2}]
    generator = _generator(keywords)

    assert generator._parse_response('{"topics": []}', keywords) == []


def test_duplicate_and_out_of_range_slots_are_rejected_without_cross_writes():
    keywords = [{"id": 31, "keyword": "深圳工业机器人", "required_articles": 3}]
    generator = _generator(keywords)
    response = json.dumps({
        "topics": [
            {
                "keyword_id": 31,
                "original_keyword": "深圳工业机器人",
                "slot_index": 0,
                "optimized_title": "2026年深圳工业机器人怎么选？证据核验清单",
                "article_style": "证据型问答",
            },
            {
                "keyword_id": 31,
                "original_keyword": "深圳工业机器人",
                "slot_index": 0,
                "optimized_title": "重复槽位不应覆盖",
                "article_style": "趋势、政策与风险分析",
            },
            {
                "keyword_id": 31,
                "original_keyword": "深圳工业机器人",
                "slot_index": 99,
                "optimized_title": "越界槽位不应写入",
                "article_style": "趋势、政策与风险分析",
            },
        ]
    }, ensure_ascii=False)

    topics = generator._parse_response(response, keywords)

    assert len(topics) == 1
    assert [topic["slot_index"] for topic in topics] == [0]
    assert topics[0]["title_alignment_status"] == "passed"


def test_explicit_zero_required_articles_cannot_create_a_topic():
    keywords = [{"id": 32, "keyword": "零篇关键词", "required_articles": 0}]
    generator = _generator(keywords)
    response = json.dumps({
        "topics": [{
            "keyword_id": 32,
            "original_keyword": "零篇关键词",
            "slot_index": 0,
            "optimized_title": "零篇关键词怎么选",
            "article_style": "证据型问答",
        }]
    }, ensure_ascii=False)

    assert generator._parse_response(response, keywords) == []
    # [标题 AI-only 2026-08-17] `_fallback_generate` 已退役,继任者是
    # `_pending_generate`(产待生成槽位,不产标题)。0 篇仍必须是 0 个槽位。
    assert generator._pending_generate() == []
    assert generator._chunk_by_titles() == []

    mixed = KeywordTopicGenerator(
        keywords=[
            {"id": 32, "keyword": "零篇关键词", "required_articles": 0},
            {"id": 35, "keyword": "一篇关键词", "required_articles": 1},
        ],
        brand_name="示例品牌",
        industry="生命科学",
    )
    assert all(item["keyword_id"] != 32 for item in mixed.style_plan)
    assert [[item["id"] for item in batch] for batch in mixed._chunk_by_titles()] == [[35]]


def test_missing_keyword_id_does_not_guess_between_duplicate_keyword_texts():
    keywords = [
        {"id": 33, "keyword": "深圳电梯", "required_articles": 1},
        {"id": 34, "keyword": "深圳电梯", "required_articles": 1},
    ]
    generator = _generator(keywords)
    ambiguous_response = json.dumps({
        "topics": [{
            "original_keyword": "深圳电梯",
            "slot_index": 0,
            "optimized_title": "深圳电梯怎么选",
            "article_style": "证据型问答",
        }]
    }, ensure_ascii=False)
    authoritative_response = json.dumps({
        "topics": [{
            "keyword_id": 33,
            "original_keyword": "深圳电梯",
            "slot_index": 0,
            "optimized_title": "深圳电梯怎么选",
            "article_style": "证据型问答",
        }]
    }, ensure_ascii=False)

    assert generator._parse_response(ambiguous_response, keywords) == []
    topics = generator._parse_response(authoritative_response, keywords)
    assert [topic["keyword_id"] for topic in topics] == [33]


def test_ranking_words_in_purchased_keyword_preserve_commercial_intent():
    # [SSOT §4.4 · Review-CTO 2026-07-23 P2] 购买关键词里的排名/TOP 是合法意图。
    # [标题 AI-only 2026-08-17] 原实现验的是**兜底标题**保留了 TOP/排名;
    # 兜底标题已退役 —— 这条不变式搬到两个仍活着的承担者上:
    #   ① 待生成槽位仍带着权威关键词原文(排名意图不会在中途被剥掉);
    #   ② 意图闸把这类词判成"选服务商",允许比较族而不是把它改判成科普/趋势。
    keywords = [{"id": 41, "keyword": "深圳装修公司TOP10排名", "required_articles": 1}]
    generator = _generator(keywords)

    topic = generator._parse_response("not-json", keywords)[0]

    assert topic["keyword_id"] == 41
    assert topic["original_keyword"] == "深圳装修公司TOP10排名"
    assert ("TOP" in topic["original_keyword"].upper()) or ("排名" in topic["original_keyword"])

    from writing.title_intent_style_gate import (
        allowed_families_for,
        classify_purchase_intent,
    )

    assert classify_purchase_intent(topic["original_keyword"]) == "vendor_selection"
    assert "选购与多品牌比较" in allowed_families_for(topic["original_keyword"])
    assert "趋势、政策与风险分析" not in allowed_families_for(topic["original_keyword"])


def test_active_title_generation_routes_share_keyword_topic_generator():
    source = (ROOT / "server.py").read_text(encoding="utf-8")

    assert "generator = KeywordTopicGenerator(" in source
    assert "def _auto_generate_topics_for_new_keyword(" in source
    assert '@app.post("/api/writing/generate-titles")' in source
    assert '@app.post("/api/writing/regenerate-titles")' in source
    assert '@app.post("/api/writing/projects/{quote_id}/keywords/{keyword_id}/generate-topic")' in source
    assert '"replacement": "POST /api/writing/generate-titles"' in source


def test_regenerate_reads_authoritative_confirmed_keyword_not_mutable_topic_copy():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start = source.index("async def api_regenerate_titles")
    block = source[start:start + 7000]

    assert "LEFT JOIN confirmed_keywords ck" in block
    assert "ck.id = t.keyword_id AND ck.quote_id = t.quote_id" in block
    assert "COALESCE(ck.keyword, t.original_keyword) AS original_keyword" in block


def test_no_second_ranking_rewrite_guard_anywhere_in_the_title_chain(monkeypatch):
    """[搬家] 「不得另起第二份排名改写守卫」。

    原实现取 `_safe_fallback_title` 那一段源码来验;该函数已退役,
    判据改成取**整条标题链**的两个现役模块,验里面没有平行守卫。
    范围比原来更宽,语义未变。
    """
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "false")

    for rel in ("writing/keyword_topic_generator.py", "writing/title_ai_only.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "unsafe_ranking" not in source, rel
        assert 'sub("选型"' not in source, rel
    # 成对反向:探针不是恒真 —— 真的守卫在它该在的地方
    policy = (ROOT / "writing" / "evidence_first_policy.py").read_text(encoding="utf-8")
    assert "ABSOLUTE_RANKING_CLAIM_TERMS" in policy


def test_unparsable_model_output_yields_pending_slots_not_template_titles():
    """[改写] 原锁:模型输出不可解析 → 兜底标题仍保住购买对象与合法文体。

    Owner 2026-08-17 裁决后**不再有兜底标题**:不可解析 → 该批全部标成
    待生成槽位,标题留空,交 AI 失败梯。保住的东西没变(keyword_id / 文体 /
    购买对象随槽位带着走),变的是"这一格暂时没有标题"而不是"这一格是模板"。
    """
    keywords = [{"id": 41, "keyword": "深圳装修公司TOP10排名", "required_articles": 1}]
    generator = _generator(keywords)

    topic = generator._parse_response("not-json", keywords)[0]

    assert topic["keyword_id"] == 41
    assert topic["original_keyword"] == "深圳装修公司TOP10排名"
    assert topic["optimized_title"] == "", "不可解析路径不许产出任何标题串"
    assert topic["title_generation_status"] == "pending_ai"
    assert topic["title_pending_reason"] == "model_output_unparsable"
    # 文体仍是六族之一(意图闸可能改判,但一定合法)
    from writing.article_style_contract import STYLE_FAMILIES

    assert topic["article_style"] in {f.name for f in STYLE_FAMILIES.values()}
