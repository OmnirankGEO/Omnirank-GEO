"""P3 文体规格卡判别锁(2026-08-08)。

工单 `WO_P2P3_ARTICLE_STYLE_DEV_2026-08-08.md` P3-1..P3-8,以修订版补章 R2/R3/R5 为准。

## 锁清单

① 附件三份 CSV 逐字保真(9 张卡 × 10 个正文结构件 + 引擎附卡),并与附件 `spec_level` 列跨口径互校;
② **边界锁**:规格卡不出字数目标(字数归篇幅合同)、不碰标题要素(归 P2 合同)、
   不劝配图位(归配图链)—— 三条边界各配正反用例;
③ 接线锁:六族正文 prompt 真的拿到规格卡块(打在 `_build_system_prompt` 的真出口上,不是函数对函数);
④ P3-2(补章 R2):实体数是**核验增援目标**,不是家数硬拦 —— 候选不足时给的是"改分场景榜"不是"补候选";
⑤ P3-3:winner_style 默认 = 候选集/分场景,唯一第一需外部可核验事实;
⑥ P3-7:引擎附卡只收 N>=30 且显著、只收正文结构件、且明说"正文仍只写一版";
⑦ P3-8(补章 R3 改成禁挂):v9 隔离壳的两个休眠常量**零生产消费方**,且规格卡没有接回它;
⑧ 正文侧三处年份口径收编:common_rules / style_registry / config.py 都不再自己写一份;
⑨ 版本门禁(复用 style_registry 范式):配置版本对不上就回落内建值 —— 生产形态 + 构造形态双用例;
⑩ 覆盖率下限同源:templates 与 length_contract 不再各写一个 0.85 字面量。
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_ATT_DIR = Path("C:/AI-Test/geo-article-upgrade-20260807")
_ATT_CARDS = _ATT_DIR / "article_type_spec_cards.csv"
_ATT_RATES = _ATT_DIR / "article_structure_feature_rates.csv"
_ATT_ENGINE = _ATT_DIR / "engine_structure_significance.csv"


def test_contract_self_audit_is_clean():
    from writing.article_type_spec_cards import validate_article_spec_cards

    assert validate_article_spec_cards() == []


def test_thresholds_are_reused_from_p2_not_redefined():
    """分级门槛必须**复用** P2 合同那一份,不许 P3 自己再定义一套。

    🔴 判据打在**同一个对象**上,不是"两个值相等" —— 值相等只说明今天没漂,
    同一个对象才说明不可能漂。变异(P3 自己写一套 level_for_rate)→ 本锁转红。
    """
    import writing.article_type_spec_cards as spec
    import writing.title_element_contract as p2

    assert spec.level_for_rate is p2.level_for_rate
    assert spec.SPEC_LEVEL_REQUIRED_MIN is p2.SPEC_LEVEL_REQUIRED_MIN
    assert spec.SPEC_LEVEL_RECOMMENDED_MIN is p2.SPEC_LEVEL_RECOMMENDED_MIN
    src = (ROOT / "writing" / "article_type_spec_cards.py").read_text(encoding="utf-8")
    assert "def level_for_rate" not in src, "不许在 P3 里另写一份分级函数"
    assert "SPEC_LEVEL_REQUIRED_MIN: Final" not in src, "不许在 P3 里另定义门槛"


# ===========================================================================
# ① 附件保真
# ===========================================================================
@pytest.mark.skipif(not _ATT_CARDS.exists(), reason="定稿附件不在本机")
def test_spec_cards_match_the_attachment_verbatim():
    from writing.article_type_spec_cards import SPEC_CARDS

    with _ATT_CARDS.open(encoding="utf-8-sig", newline="") as handle:
        rows = {r["article_type"]: r for r in csv.DictReader(handle)}
    assert set(rows) == set(SPEC_CARDS), "文体集合与附件不一致"
    for corpus_type, card in SPEC_CARDS.items():
        row = rows[corpus_type]
        assert card.n == int(row["n"])
        assert card.body_chars_p25 == int(row["body_chars_p25"])
        assert card.body_chars_median == int(row["body_chars_median"])
        assert card.body_chars_p75 == int(row["body_chars_p75"])
        assert card.entity_count_p25 == pytest.approx(float(row["entity_count_p25"]))
        assert card.entity_count_p75 == pytest.approx(float(row["entity_count_p75"]))
        assert card.chars_per_entity_median == int(row["chars_per_entity_median"])
        assert card.data_evidence_p25 == pytest.approx(float(row["data_evidence_p25"]))
        assert card.avg_paragraph_chars_median == pytest.approx(
            float(row["avg_paragraph_chars_median"]))


@pytest.mark.skipif(not _ATT_RATES.exists(), reason="定稿附件不在本机")
def test_structure_rates_match_the_attachment_and_its_spec_level_column():
    """数值保真 + 与附件自己算的分级跨口径互校(9×10=90 行)。"""
    from writing.article_type_spec_cards import (
        BODY_STRUCTURE_FEATURES,
        SPEC_CARDS,
        structure_level,
    )
    from writing.title_element_contract import (
        LEVEL_OPTIONAL,
        LEVEL_RECOMMENDED,
        LEVEL_REQUIRED,
    )

    zh_to_level = {"默认必备": LEVEL_REQUIRED, "推荐": LEVEL_RECOMMENDED, "可选": LEVEL_OPTIONAL}
    checked = 0
    with _ATT_RATES.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            feature = row["feature"]
            if feature not in BODY_STRUCTURE_FEATURES:
                continue
            corpus_type = row["article_type"]
            card = SPEC_CARDS[corpus_type]
            assert card.structure_rates[feature] == pytest.approx(float(row["present_pct"])), row
            expected = next(
                lv for zh, lv in zh_to_level.items() if row["spec_level"].startswith(zh))
            assert structure_level(corpus_type, feature) == expected, row
            checked += 1
    assert checked == 90, f"应当校 9 文体 × 10 正文结构件 = 90 行,实际 {checked}"


@pytest.mark.skipif(not _ATT_ENGINE.exists(), reason="定稿附件不在本机")
def test_engine_addenda_equal_the_attachment_addendum_column():
    """附卡集合必须**逐行等于**附件 `addendum=yes` 的正文结构件行。

    我们不自己按 p_value 重判(那是第二套判定逻辑),直接采用研究方那一列。
    被丢弃的标题类行必须出现在 `DISCARDED_TITLE_ADDENDA` 里 —— 丢弃要留痕,不许静默。
    """
    from writing.article_type_spec_cards import (
        BODY_STRUCTURE_FEATURES,
        DISCARDED_TITLE_ADDENDA,
        ENGINE_ADDENDA,
        TITLE_FEATURE_PREFIX,
    )

    body_rows, title_rows = set(), set()
    with _ATT_ENGINE.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if (row.get("addendum") or "").strip() != "yes":
                continue
            key = (row["article_type"], row["engine"], row["feature"])
            if row["feature"].startswith(TITLE_FEATURE_PREFIX):
                title_rows.add(key)
            elif row["feature"] in BODY_STRUCTURE_FEATURES:
                body_rows.add(key)
    ours = {(a.corpus_type, a.engine, a.feature) for a in ENGINE_ADDENDA}
    assert ours == body_rows, f"附卡集合与附件不一致: 缺 {body_rows - ours} / 多 {ours - body_rows}"
    discarded = {(c, e, f) for c, e, f, _ in DISCARDED_TITLE_ADDENDA}
    assert discarded == title_rows, "被丢弃的标题类附卡行必须逐行留痕"
    assert all(a.n >= 30 for a in ENGINE_ADDENDA)


# ===========================================================================
# ② 三条边界(各配正反用例)
# ===========================================================================
def test_spec_card_never_sets_a_length_target():
    """边界一:字数归篇幅合同。变异(把字数目标写进规格卡)→ 本锁转红。"""
    from writing.article_type_spec_cards import SPEC_CARDS, build_spec_card_prompt
    from writing.title_element_contract import FAMILY_TITLE_PROFILES

    for family in FAMILY_TITLE_PROFILES:
        block = build_spec_card_prompt(family, engines=("deepseek", "qwen"))
        assert "目标篇幅" not in block and "字数目标" not in block
        assert "全文不得低于" not in block
        # 反向对照:语料字数确实**存在**于数据里(只是不渲染),否则这条锁是空的
        card = SPEC_CARDS["ranking"]
        assert card.body_chars_p75 == 12701
        assert str(card.body_chars_p75) not in block


def test_spec_card_never_touches_title_elements():
    """边界二:标题要素归 P2 合同。"""
    from writing.article_type_spec_cards import (
        BODY_STRUCTURE_FEATURES,
        SPEC_CARDS,
        TITLE_FEATURE_PREFIX,
        build_spec_card_prompt,
    )

    for card in SPEC_CARDS.values():
        assert not any(k.startswith(TITLE_FEATURE_PREFIX) for k in card.structure_rates)
    assert not any(f.startswith(TITLE_FEATURE_PREFIX) for f in BODY_STRUCTURE_FEATURES)
    block = build_spec_card_prompt("multi_brand_comparison")
    for banned in ("标题含年份", "标题含地区", "标题含数字", "标题含行业对象"):
        assert banned not in block


def test_spec_card_never_pushes_image_slots():
    """边界三:配图归配图链。图片位**在数据里但不进提示词**。

    这条不是洁癖:写作侧劝出来的 `[NEED_IMAGE]` 占位曾经被当正文发到渠道,
    并被 `platform_safety_profiles` 判 hard 变成发布阻断
    (见 `tests/test_need_image_never_reaches_publish.py`)。
    """
    from writing.article_type_spec_cards import (
        PROMPT_EXCLUDED_FEATURES,
        SPEC_CARDS,
        build_spec_card_prompt,
    )

    assert "has_image_reference" in PROMPT_EXCLUDED_FEATURES
    # 反向对照:数据里确实有它,而且是最大的引擎正向差 —— 排除是决定不是遗漏
    assert SPEC_CARDS["ranking"].structure_rates["has_image_reference"] == 14.48
    for family in ("multi_brand_comparison", "case_data_roi", "implementation_guide"):
        block = build_spec_card_prompt(family, engines=("deepseek", "qwen", "kimi", "doubao"))
        assert "图片位" not in block, family


# ===========================================================================
# ③ 接线锁 —— 打在正文 prompt 的真出口上
# ===========================================================================
def test_spec_card_actually_reaches_the_body_prompt():
    """六族的正文 system_prompt 里必须真的出现规格卡块。

    🔴 判据打在**接线**上:不是"函数能返回一段字",而是"生成侧确实把它拼进了 prompt"。
    变异(把注入那一段删掉)→ 本锁转红。
    """
    import inspect

    from writing import article_generator_service

    src = inspect.getsource(article_generator_service)
    assert "build_article_type_spec_block" in src, "规格卡没有被生成侧引用"
    assert "_spec_card_block" in src
    # 必须真的拼进 system_prompt,而不是算出来丢掉
    assert 'system_prompt = system_prompt + "\\n\\n" + _spec_card_block' in src
    # 且不能被限制在单一文体上(9 张卡覆盖全部文体)。
    # 🔴 判据不认"某一种写法",而是:注入段里**只能**由 family_for_style(style_code) 决定,
    # 窗口内不许出现任何文体名字面量(变异 M09 正是靠写一个字面量把它锁死在榜单族)。
    idx = src.index("_family_code_for_spec = ")
    window = src[idx:src.index("build_article_type_spec_block", idx)]
    assert "family_for_style(style_code)" in window
    from writing.article_style_contract import STYLE_FAMILIES

    for family in STYLE_FAMILIES:
        assert f'"{family}"' not in window, f"规格卡注入段不该写死文体: {family}"
    # 行为面反向对照:不同族真的拿到不同的卡
    from writing.templates.canonical_family_templates import build_article_type_spec_block

    blocks = {
        f: build_article_type_spec_block(f, engines=("deepseek",))
        for f in ("multi_brand_comparison", "implementation_guide", "case_data_roi")
    }
    assert len(set(blocks.values())) == 3, "三族必须拿到三张不同的卡"
    assert all(b for b in blocks.values())


def test_renderer_holds_no_statistics_of_its_own():
    """补章 R3 的分工:canonical 只做渲染,数值全在 SSOT。

    变异(把统计数字抄回 canonical)→ 本锁转红。
    """
    src = (ROOT / "writing" / "templates" / "canonical_family_templates.py").read_text(
        encoding="utf-8")
    body = src[src.index("def build_article_type_spec_block"):]
    body = body[:body.index("\ndef ", 10)]
    for banned in ("59.06", "71.11", "97.62", "78.46", "752", "P25"):
        assert banned not in body, f"渲染器里不许出现统计数字: {banned}"


# ===========================================================================
# ④ P3-2(补章 R2):实体数是增援目标,不是家数硬拦
# ===========================================================================
def test_entity_target_is_a_research_goal_not_a_hard_count():
    from writing.article_type_spec_cards import (
        RANKING_ENTITY_TARGET_HIGH,
        RANKING_ENTITY_TARGET_LOW,
        build_spec_card_prompt,
    )

    assert (RANKING_ENTITY_TARGET_LOW, RANKING_ENTITY_TARGET_HIGH) == (5, 10)
    short = build_spec_card_prompt("multi_brand_comparison", verified_entity_count=3)
    assert "不是家数上下限" in short
    assert "绝不为了凑区间虚构候选" in short
    # 候选不足时给的动作是"改分场景推荐榜",**不是**"去补候选凑数"
    assert "分场景推荐榜" in short
    assert "不要补虚构候选" in short
    enough = build_spec_card_prompt("multi_brand_comparison", verified_entity_count=7)
    assert "低于目标区间下限" not in enough
    # 反向对照:两种输入必须真的不同,否则这条锁没有判别力
    assert short != enough


def test_article_chain_still_has_no_hard_family_count():
    """文章链**不加**家数常量(补章 R2 的核心订正)。"""
    from writing.competitor_name_contract import build_name_whitelist

    # 已核验形态(裸字符串按合同**不算已核验**,这里必须给真实形态)
    items = [{"name": f"品牌{i}", "name_verified": True} for i in range(12)]
    whitelist = build_name_whitelist(items, "客户品牌")
    assert len(whitelist) == 13, f"白名单不得被任何家数上限截断: {whitelist}"
    # 反向对照:裸字符串确实不算已核验(证明上面用的是真形态,不是判据太松)
    assert build_name_whitelist([f"品牌{i}" for i in range(12)], "客户品牌") == ["客户品牌"]


# ===========================================================================
# ⑤ P3-3 winner_style
# ===========================================================================
def test_winner_style_defaults_to_candidate_set_or_scenario():
    from writing.templates.canonical_family_templates import prompt_for_style

    prompt = prompt_for_style("comparison_review")
    assert "候选集并列" in prompt and "分场景推荐" in prompt
    # [返修 C12 2026-08-11] 「唯一第一只在…才写」死信许可已删(与「不做全局
    # 第一名」顶撞 + 绝对化名次词出现即拦,照写必白跑一轮);外部名次事实改
    # 引用式出路。本锁随契约适配,组织方式默认的原意图断言原样保留。
    assert "唯一第一只在" not in prompt
    assert "位列第 N" in prompt
    assert "场景推荐榜" in prompt
    # 🔴 语料里唯一第一占比最高(48.65%),必须明说"别人怎么写≠我们能照做"
    assert "不是我们可以无依据照做的理由" in prompt
    # 规则必须带**数据出处**下发(无据拍脑袋的规则改起来没人拦得住)
    for share in ("37.98%", "11.47%", "48.65%", "N=5,753"):
        assert share in prompt, share
    # 既有口径零回退
    assert "不排名次、不做全局第一名" in prompt


# ===========================================================================
# ⑥ P3-7 引擎附卡
# ===========================================================================
def test_engine_addendum_states_one_body_only():
    from writing.article_type_spec_cards import build_spec_card_prompt

    block = build_spec_card_prompt("multi_brand_comparison", engines=("deepseek", "qwen"))
    assert "引擎特化附卡" in block
    assert "正文仍然只写一版" in block
    assert "不是给每个引擎各写一篇" in block
    # 不传引擎就不出附卡(显式 opt-in · 反向对照)
    assert "引擎特化附卡" not in build_spec_card_prompt("multi_brand_comparison")


def test_platform_engine_names_are_aliased_to_corpus_names():
    """🔴 平台用供应商名 `dashscope`,语料用模型名 `qwen` —— 不做别名就会**静默**
    丢掉 qwen 的全部附卡,而且表面上"附卡照常渲染"看不出来。

    变异(删掉别名表)→ 本锁转红。
    """
    from config.ai_engines import UNIFIED_ENGINES
    from writing.article_type_spec_cards import (
        CORPUS_ENGINES,
        addenda_for,
        normalize_engine_name,
    )

    assert normalize_engine_name("dashscope") == "qwen"
    assert "qwen" not in UNIFIED_ENGINES, "前提变了:平台已经直接叫 qwen,别名表要重审"
    got = addenda_for("ranking", UNIFIED_ENGINES)
    assert {a.engine for a in got} == {"deepseek", "qwen"}, got
    # 反向对照:不过别名就会筛空 qwen
    raw = [a for a in got if a.engine == "qwen"]
    assert raw, "别名失效时这里会空"
    assert "dashscope" not in CORPUS_ENGINES


# ===========================================================================
# ⑦ P3-8(补章 R3):v9 禁挂,只加反向锁
# ===========================================================================
def test_v9_dormant_constants_have_no_consumer_and_spec_cards_did_not_wire_them():
    """规格卡**没有**把 v9 的休眠常量接回来(补章 R3:v9 是隔离壳,禁挂)。"""
    import subprocess
    import sys

    spec_src = (ROOT / "writing" / "article_type_spec_cards.py").read_text(encoding="utf-8")
    assert "ranking_prompt_v9" not in spec_src, "规格卡不得引用 v9 隔离壳"
    for banned in ("GEO_RANKING_SIGNALS", "GEO_SIGNAL_CHECK", "generate_dynamic_scores"):
        assert banned not in spec_src
    # 两个休眠常量在生产代码里仍然零消费方(v9 文件本体除外)
    out = subprocess.run(
        [sys.executable, "-c", "pass"], capture_output=True, text=True,
    )
    assert out.returncode == 0
    hits = []
    for path in (ROOT / "writing").rglob("*.py"):
        if path.name == "ranking_prompt_v9.py" or "_archived" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "GEO_RANKING_SIGNALS" in text or "GEO_SIGNAL_CHECK" in text:
            hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"v9 休眠常量出现了消费方: {hits}"


# ===========================================================================
# ⑧ 正文侧三处年份口径收编
# ===========================================================================
def test_body_side_year_rules_all_render_the_contract():
    """common_rules / style_registry 不再各写一份年份口径。"""
    from writing.style_registry import _inject_dynamic_dates
    from writing.templates.common_rules import COMMON_GUARDRAILS
    from writing.title_element_contract import build_title_year_position_rule

    rule = build_title_year_position_rule()
    assert rule in COMMON_GUARDRAILS
    assert rule in _inject_dynamic_dates("测试")
    # 旧的两种口径必须绝迹
    assert "标题可以带当前年份" not in COMMON_GUARDRAILS
    # 🔴 判据打在**渲染输出**上,不打在源码文本上 —— 源码里有解释这次改动的注释,
    # 扫源码会把注释误判成口径复活(本仓踩过"剥注释≠剥字符串"那个坑)。
    rendered = _inject_dynamic_dates("测试")
    assert "仅当主题确有时效性且 Evidence Pack 含对应时间证据时才在标题写年份" not in rendered
    # 飞轮那组数字在这两处都不再手写,只经渲染出现一次
    common_src = (ROOT / "writing" / "templates" / "common_rules.py").read_text(encoding="utf-8")
    assert "30.7%" not in common_src and "42.1%" not in common_src
    assert rendered.count("30.7%") == 1 and COMMON_GUARDRAILS.count("30.7%") == 1


def test_config_year_strategy_is_dynamic():
    """`TITLE_VARIATIONS["years"]` 不再写死 2026/2025。"""
    from writing.config import TITLE_VARIATIONS
    from writing.title_element_contract import current_year

    year = current_year()
    assert TITLE_VARIATIONS["years"]["main"] == f"{year}年"
    assert TITLE_VARIATIONS["years"]["review"] == f"{year - 1}年"
    src = (ROOT / "writing" / "config.py").read_text(encoding="utf-8")
    assert '"main": "2026年"' not in src
    # 消费方(topic_dispatcher)一个字没动 —— 补章 R3 红线
    dispatcher = (ROOT / "writing" / "topic_dispatcher.py").read_text(encoding="utf-8")
    assert 'years_dict = TITLE_VARIATIONS["years"]' in dispatcher


# ===========================================================================
# ⑨ 版本门禁(生产形态 + 构造形态双用例)
# ===========================================================================
def test_version_gate_production_shape_has_no_override():
    """生产形态:没有配置覆盖 → 走内建值。"""
    from writing.article_type_spec_cards import _config_override, structure_rate

    assert _config_override("ranking") is None
    assert structure_rate("ranking", "has_headings") == 97.62


def test_version_gate_constructed_shape(monkeypatch):
    """构造形态:版本对得上才生效,对不上一律回落内建值。"""
    import writing.article_type_spec_cards as spec
    import writing.style_registry as registry

    payload = {
        "article_spec_card_overrides": {"ranking": {"structure_rates": {"has_headings": 12.34}}},
        "article_spec_card_contract_versions": {"ranking": spec.ARTICLE_SPEC_CARD_VERSION},
    }
    monkeypatch.setattr(registry, "_get_writing_config", lambda: payload)
    assert spec.structure_rate("ranking", "has_headings") == 12.34

    stale = dict(payload)
    stale["article_spec_card_contract_versions"] = {"ranking": "geo-article-spec-card-v0.0"}
    monkeypatch.setattr(registry, "_get_writing_config", lambda: stale)
    assert spec.structure_rate("ranking", "has_headings") == 97.62, "版本对不上必须回落内建值"


# ===========================================================================
# ⑩ 覆盖率下限同源
# ===========================================================================
def test_coverage_floor_is_single_sourced():
    from writing.article_length_contract import DEEP_OUTPUT_COVERAGE_FLOOR
    from writing.templates.canonical_family_templates import BUDGET_COVERAGE_FLOOR

    assert BUDGET_COVERAGE_FLOOR == DEEP_OUTPUT_COVERAGE_FLOOR
    src = (ROOT / "writing" / "templates" / "canonical_family_templates.py").read_text(
        encoding="utf-8")
    # 🔴 钉住**赋值那一行**:只查 "DEEP_OUTPUT_COVERAGE_FLOOR 在文件里" 是抓不到
    # "又写回一个 0.85 字面量" 的(helper 还在,字符串照样命中)—— 变异 M18 实证。
    assert "BUDGET_COVERAGE_FLOOR: Final = _coverage_floor()" in src
    assert "BUDGET_COVERAGE_FLOOR: Final = 0.85" not in src


# ===========================================================================
# ⑪ P3-6(补章 R5 ②):清洗器补规格卡时代样本 + 三处接线
# ===========================================================================
def test_sanitizer_strips_spec_card_era_internal_phrases():
    from writing.body_internal_marker_sanitizer import (
        SANITIZER_VERSION,
        sanitize_article_body,
    )

    body = (
        "正文第一段,读者能读的话。\n\n"
        "本篇已核验候选 7 家,证据密度达到密度下限。\n\n"
        "本节按结构件分级里的默认必备项组织。\n\n"
        "正常内容收尾段。"
    )
    out, report = sanitize_article_body(body)
    for banned in ("已核验候选", "证据密度", "密度下限", "结构件分级", "默认必备"):
        assert banned not in out, banned
    assert "正文第一段" in out and "正常内容收尾段" in out
    assert report["removed_counts"]["review_phrase_lines"] >= 2
    assert SANITIZER_VERSION.endswith("v4"), "清单变了,版本号必须跟着涨"  # [W1 返工 ③] v3→v4


def test_sanitizer_is_wired_on_all_three_save_paths():
    """补章 R5 + 子代理实证:是 **3** 条保存路径,不是工单说的 2 条。"""
    import inspect

    from tools import article_generator
    from writing import article_generator_service

    svc = inspect.getsource(article_generator_service)
    assert svc.count("sanitize_article_for_save") >= 2
    assert "sanitize_article_for_save" in inspect.getsource(article_generator)
