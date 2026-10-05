"""工单 A 2026-07-27 · 文章结构规格(飞轮数据背书版)+ 长度四层卡点修复。

判别口径(工单 §5):
1. 7 家已核验候选 + 4 条证据 -> 进 deep(target >= 15000);
2. semi 模式 6 家已核验 -> 同样进 deep(旧代码这里恒 compact,是本单的主要收益);
3. 全未核验 -> compact 3500;
4. 深档结构规格只在深档注入,紧凑档一个字都不给(否则等于逼模型注水);
5. 对撞句 / 年份硬规则已按数据改口径。

每条断言都写成"改回旧实现就转红"的形状 —— 变异验证见 EXIT 文档。
"""
from pathlib import Path

from writing.article_length_contract import (
    AVOIDANCE_BAND,
    COMPACT_RESOLUTION_MAX_CHARS,
    COMPACT_TARGET_CHARS,
    RANKING_FAMILY_MINIMUM_CHARS,
    build_length_plan_for_topic,
    count_verified_candidates,
    in_avoidance_band,
)
from writing.templates.canonical_family_templates import (
    build_deep_ranking_structure_spec,
)


def _pack(count: int) -> dict:
    return {
        "items": [
            {
                "evidence_id": f"EV-{index}",
                "claim": f"claim {index}",
                "url": f"https://official-{index}.example/report",
                "publisher": f"publisher-{index % 4}",
                "relationship": "support",
                "verification_status": "official_record",
                "official_record_id": f"record-{index}",
            }
            for index in range(count)
        ]
    }


def _verified(count: int) -> list[dict]:
    return [{"name": f"候选{index}", "name_verified": True} for index in range(count)]


def _unverified(count: int) -> list[dict]:
    return [{"name": f"候选{index}"} for index in range(count)]


# ---------------------------------------------------------------------------
# §5-1 / §5-2 · 计数层修复的判别测试
# ---------------------------------------------------------------------------
def test_seven_verified_candidates_with_evidence_enter_the_deep_tier():
    topic = {
        "_evidence_pack": _pack(4),
        "_competitor_source": "real",
        "_researched_competitors": _verified(7),
        "_competitor_candidate_pool": _verified(7),
    }
    plan = build_length_plan_for_topic("comparison_review", topic)
    assert plan["verified_candidate_count"] == 8  # 7 家 + 客户品牌
    assert plan["target_chars"] >= RANKING_FAMILY_MINIMUM_CHARS
    assert "verified_candidates_support_ranking_deep" in plan["reasons"]


def test_semi_mode_verified_entries_also_enter_the_deep_tier():
    """本单的主要收益:semi 批里已核验的名字必须进容量计数。

    旧实现是 `if _competitor_source == 'real'` 的整体前置门,而 real 判定要求
    **全部** 竞品 name_verified —— 生产 semi 存量 26/32 已核验却一条都不算,
    榜单族因此恒走 few_verified_candidates_no_padding。
    """
    topic = {
        "_evidence_pack": _pack(4),
        "_competitor_source": "semi",
        # semi 模式下 `_researched_competitors` 按正文语义必须为空(不能进正文),
        # 已核验条目只出现在容量计数专用的候选池里。
        "_researched_competitors": [],
        "_competitor_candidate_pool": _verified(6) + _unverified(3),
    }
    plan = build_length_plan_for_topic("comparison_review", topic)
    assert plan["verified_candidate_count"] == 7  # 6 家已核验 + 客户品牌;3 家未核验不算
    assert plan["target_chars"] >= RANKING_FAMILY_MINIMUM_CHARS
    assert "few_verified_candidates_no_padding" not in plan["reasons"]


def test_all_unverified_candidates_stay_compact_at_3500():
    topic = {
        "_evidence_pack": _pack(4),
        "_competitor_source": "evidence_only",
        "_competitor_candidate_pool": _unverified(9),
    }
    plan = build_length_plan_for_topic("comparison_review", topic)
    assert plan["verified_candidate_count"] == 1  # 只有客户品牌
    assert plan["target_chars"] == COMPACT_TARGET_CHARS == 3500
    assert plan["maximum_chars"] == COMPACT_RESOLUTION_MAX_CHARS == 4500
    assert "few_verified_candidates_no_padding" in plan["reasons"]


def test_counting_keeps_the_legacy_real_mode_reading_for_name_only_batches():
    """老写作链 `_load_db_competitors` 只回名称字符串,不能因为改判定而算成 0。"""
    assert count_verified_candidates(
        {"_competitor_source": "real", "_researched_competitors": ["甲", "乙", "丙"]}
    ) == 4
    # 同一批同时挂在两个键上时按名称去重,不重复计数。
    pool = _verified(5)
    assert count_verified_candidates(
        {
            "_competitor_source": "real",
            "_researched_competitors": pool,
            "_competitor_candidate_pool": pool,
        }
    ) == 6


def test_comp_mode_downgrade_is_untouched_but_body_names_are_now_per_entry():
    """[工单 C · §1 语义同步] 原名 `..._body_semantics_are_untouched_by_the_counting_fix`。

    工单 A 时的口径是"计数放宽、正文写入语义一个字不动(semi 全禁名)"。
    Owner 2026-07-27 拍板改了后半句:**真实已核验的名字可以写,只是不要贬低**。
    生产实证已证明旧口径与深档规格正面冲突(规格要 12 家成卡、名单却是空的
    → 12 张卡无名可写 → 10037 / 5530 方差近倍)。

    所以这条断言同步改成新语义,但**没有放松防线**:
      · comp_mode 的降级判定仍然一个字不动(继续管批次可信度);
      · 未核验名仍然一个字都进不了正文。
    """
    source = Path("writing/article_generator_service.py").read_text(encoding="utf-8")
    # 降级判定原样保留 —— 这是本单明确不动的东西(§5)。
    assert "comp_mode = 'semi' if db_competitors else 'evidence_only'" in source
    # 正文名称不再按 mode 一刀切,改走逐条白名单 SSOT。
    assert "from writing.competitor_name_contract import" in source
    assert "_name_whitelist = build_name_whitelist(db_competitors, self.brand_name)" in source
    # 反向锁:旧的一刀切分支不得复活。
    assert "elif comp_mode == 'semi' and db_competitors:" not in source
    # `_researched_competitors` 改成"逐条已核验子集",不再按 mode 整批给/整批空。
    assert 'topic["_researched_competitors"] = db_competitors if comp_mode == "real" else []' not in source
    assert 'item.get("name_verified") is True or item.get("human_verified_name") is True' in source
    # 容量计数专用池仍是独立的第二个键,不复用上面那个。
    assert 'topic["_competitor_candidate_pool"] = db_competitors' in source


# ---------------------------------------------------------------------------
# §1 · 双峰分档
# ---------------------------------------------------------------------------
def test_avoidance_band_covers_the_whole_data_trough():
    assert AVOIDANCE_BAND == (4500, 14000)
    for trough in (4501, 5500, 8000, 12000, 13999):
        assert in_avoidance_band(trough), trough
    # 两个端点分别是峰 1 上界与峰 2 前沿,本身不算落谷。
    assert not in_avoidance_band(4500)
    assert not in_avoidance_band(14000)
    assert not in_avoidance_band(3500)
    assert not in_avoidance_band(16000)


def test_no_family_default_target_can_stop_inside_the_trough():
    from writing.article_length_contract import FAMILY_LENGTH_POLICIES

    for code, policy in FAMILY_LENGTH_POLICIES.items():
        assert not in_avoidance_band(policy.target_chars), code


# ---------------------------------------------------------------------------
# §3 · 深档结构规格 · 只在深档注入
# ---------------------------------------------------------------------------
def test_structure_spec_is_injected_only_when_the_plan_is_deep():
    deep_plan = build_length_plan_for_topic(
        "comparison_review",
        {
            "_evidence_pack": _pack(4),
            "_competitor_source": "real",
            "_researched_competitors": _verified(7),
        },
    )
    compact_plan = build_length_plan_for_topic(
        "comparison_review",
        {"_evidence_pack": _pack(4), "_competitor_candidate_pool": _unverified(9)},
    )

    spec = build_deep_ranking_structure_spec(deep_plan)
    assert spec
    # [工单 C · §2 语义同步] v1 的散文式规格换成了预算表驱动的 v2。
    # 断言的仍是同一批要求,只是它们现在有明确的字数预算。
    assert "深度评测结构规格 v2" in spec
    assert "区块字数预算表" in spec
    # [C-5 2026-07-28 · 语义收紧同步] "不以年份开头" → "默认不含年份(年度主题除外,
    # 且不作开头)":旧禁令是新句的真子集,锁随规格升级,防护力只增不减。
    # [P2 2026-08-08] 深档榜单规格只对榜单族渲染,而榜单族在标题要素合同里是
    # year=on(语料 59.06%)—— 于是这句从"默认不含年份"翻成"默认带当年年份"。
    # 断言改为**跟着合同渲染走**,不再钉死某一句文案(钉文案就是再造一份口径)。
    from writing.templates.canonical_family_templates import _ranking_title_year_clause

    assert _ranking_title_year_clause() in spec
    # 位置规则零回退:无论默认带不带,年份都不许作开头
    assert "不作开头" in spec
    assert "每段 50-90 字" in spec
    assert "分场景建议" in spec
    assert "FAQ 与对比表不强制" in spec
    assert "已核验候选 8 家" in spec  # 7 竞品 + 客户品牌,来自 plan 自身

    assert build_deep_ranking_structure_spec(compact_plan) == ""
    assert build_deep_ranking_structure_spec(None) == ""
    assert build_deep_ranking_structure_spec({}) == ""


def test_generator_composes_the_spec_from_the_plan_not_from_the_family_template():
    from writing.templates.canonical_family_templates import prompt_for_style

    # 规格段不能写死进 family 模板 —— 否则紧凑档也会拿到深档规格。
    assert "深度评测结构规格" not in prompt_for_style("comparison_review")

    source = Path("writing/article_generator_service.py").read_text(encoding="utf-8")
    assert "build_deep_ranking_structure_spec" in source
    assert "_deep_spec = build_deep_ranking_structure_spec(" in source
    # [工单 C · §2] 规格的成卡名单现在与正文可用名白名单同源
    assert "whitelist=_name_whitelist" in source


# ---------------------------------------------------------------------------
# §3-1 / §3-3 · 对撞句与年份口径
# ---------------------------------------------------------------------------
def test_length_contract_has_no_competing_instruction_in_common_rules():
    from writing.templates.common_rules import COMMON_GUARDRAILS

    # 篇幅唯一来源是 render_length_instruction;全局守则不得再说"不强制字数"。
    assert "不强制字数" not in COMMON_GUARDRAILS
    assert "段落应自包含。" in COMMON_GUARDRAILS


def test_year_in_title_is_downgraded_from_hard_rule_to_optional():
    from writing.templates.common_rules import COMMON_GUARDRAILS
    from writing.title_formula_library import (
        ADOPTION_TITLE_FEATURE_CONTRAST,
        build_title_formula_prompt,
        evaluate_title_formula,
        formulas_for_family,
    )

    assert "标题应带当前年份" not in COMMON_GUARDRAILS
    # [P3 2026-08-08] 这一条从**本文件手写**改成渲染标题要素合同(仓里这组飞轮数字
    # 一度有三份手抄)。判据随之从"某句逐字文案在不在"升级为"是不是合同渲染的那一份"
    # —— 措辞可以再改,口径只能有一个来源。
    from writing.title_element_contract import build_title_year_position_rule

    assert build_title_year_position_rule() in COMMON_GUARDRAILS
    # 位置规则本身零回退(措辞变了,规则没变)
    assert "不作开头" in COMMON_GUARDRAILS

    # 对照组数据:问句正向、年份反向。
    assert ADOPTION_TITLE_FEATURE_CONTRAST["question"]["adopted"] > \
        ADOPTION_TITLE_FEATURE_CONTRAST["question"]["control"]
    assert ADOPTION_TITLE_FEATURE_CONTRAST["year"]["adopted"] < \
        ADOPTION_TITLE_FEATURE_CONTRAST["year"]["control"]

    # 问句式升为榜单族/问答族的首选形态。
    assert formulas_for_family("multi_brand_comparison")[0].key == "B_question"
    assert formulas_for_family("evidence_qa")[0].key == "B_question"

    # 三式一条都没删,只是重排 + 年份挪出开头。
    assert {f.key for f in formulas_for_family("multi_brand_comparison")} == {
        "B_question", "A_ranking",
    }
    prompt = build_title_formula_prompt(year=2026, region="深圳", category="全屋定制")
    assert "不以年份开头" in prompt
    assert "年份必带" not in prompt

    # 缺年份不再报;以年份开头才报。
    assert evaluate_title_formula("深圳全屋定制哪家好？真实对比与选择建议")["compliant"] is True
    codes = {
        f["code"]
        for f in evaluate_title_formula("2026年深圳全屋定制哪家好？")["findings"]
    }
    assert "title_leads_with_year" in codes
    assert "title_missing_year" not in codes
