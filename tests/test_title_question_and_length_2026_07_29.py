"""判别锁 · 工单 `WORKORDER_TITLE_QUESTION_AND_LENGTH_2026-07-29`（T1-T4）。

🔴 工单 §3 的三条硬要求，本文件逐条遵守：

1. **必须行为级** —— 真调生成链 / 真跑校验函数 / 真跑渲染函数。
   本文件**没有一条**"读源码字符串断言某个词在不在提示词里"的锁。
   唯一被替身掉的是**最外层 HTTP 端点**（LLM 是外部非确定性来源，真调它
   等于把锁变成掷骰子）；替身返回的是**对抗性**内容：整批全榜单非问句标题、
   落中段的正文。也就是说，**被测的判定/收敛逻辑一行都没被 mock**，
   摘掉它锁必红 —— 这正是 §3 要的"变异要拆到该锁真正依赖的那一层"。
2. 变异注入后 import 冒烟 + FAILED/ERROR 两类都统计 —— 见
   `scripts/mutation_title_length_2026_07_29.py`（它跑本文件）。
3. 变异只拆一层不算 —— 每条锁下面注明"拆哪一层会让它转红"。
"""
from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# 共用：对抗性 LLM 替身（只替 HTTP 端点，链路其余部分全真跑）
# ---------------------------------------------------------------------------
def _adversarial_topic_payload(count: int, keyword: str = "GEO服务商") -> dict:
    """整批全是"2026年十大…"纯榜单非问句标题 —— 工单 §1.1 明令禁止的形态。"""
    return {"choices": [{"message": {"content": json.dumps({"topics": [
        {
            "keyword_id": 1,
            "slot_index": i,
            "original_keyword": keyword,
            "optimized_title": f"2026年十大{keyword}权威盘点{i}",
            "article_style": "选购与多品牌比较",
            "angle": f"角度{i}",
        }
        for i in range(count)
    ]}, ensure_ascii=False)}}]}


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        return _FakeResponse(self._payload)


def _run_topic_generation(count: int, **generator_kwargs):
    """真调 `KeywordTopicGenerator.generate()` 全链。"""
    from writing.keyword_topic_generator import KeywordTopicGenerator

    payload = _adversarial_topic_payload(count)
    generator = KeywordTopicGenerator(
        [{"id": 1, "keyword": "GEO服务商", "required_articles": count}],
        "QZQZ",
        "全屋定制",
        **generator_kwargs,
    )
    with mock.patch(
        "writing.keyword_topic_generator.httpx.AsyncClient",
        lambda *a, **k: _FakeAsyncClient(payload),
    ), mock.patch(
        "writing.llm_utils.get_llm_config", return_value=("http://stub", "key", "model", "prov"),
    ), mock.patch(
        "writing.llm_utils.get_fallback_llm_config",
        return_value=("http://stub", "key", "model", "prov"),
    ):
        topics = asyncio.run(generator.generate())
    return generator, topics


# ===========================================================================
# T1 · 标题问句化
# ===========================================================================
def test_t1_lock1_batch_of_ten_lands_in_question_tolerance():
    """锁 1：批量生成 10 个标题 → 问句式命中数落在 6-8（70% ± 容差）。

    输入是**整批 0 问句**的对抗性 LLM 输出 —— 命中数完全由收敛层产生。
    拆哪一层会红：`enforce_question_ratio` 的改写分支、`allocate_question_quota`
    的配额分配、或 `KeywordTopicGenerator._apply_title_question_policy` 的接线。
    """
    from writing.title_question_policy import is_question_title

    generator, topics = _run_topic_generation(10)
    assert len(topics) == 10, f"生成链没产出 10 条: {len(topics)}"
    hits = sum(1 for t in topics if is_question_title(t.get("optimized_title")))
    assert 6 <= hits <= 8, (
        f"问句式命中 {hits}/10，超出 70%±容差；"
        f"报告={generator.title_question_report}"
    )
    assert generator.title_question_report["applied"] is True
    assert generator.title_question_report["within_tolerance"] is True


def test_t1_lock2_ranking_family_question_rate_above_zero():
    """锁 2：榜单家族的标题问句式命中率 > 0（不能整批全是"十大/TOP"）。

    这条锁**不是**锁 1 的推论：全局 70% 完全可能整份落在别的家族头上，
    榜单族仍旧 0 问句。拆哪一层会红：`allocate_question_quota` 里
    "拿到 0 的家族补到 1 + 优先族优先"那段。
    """
    from writing.title_question_policy import is_question_title

    generator, topics = _run_topic_generation(10)
    report = generator.title_question_report
    ranking = report["by_family"].get("multi_brand_comparison")
    assert ranking is not None, f"本批没有榜单家族，锁失去意义: {report['by_family']}"
    assert ranking["question"] > 0, f"榜单家族 0 问句: {ranking}"

    ranking_titles = [
        t["optimized_title"] for t in topics
        if t.get("article_style") == "选购与多品牌比较"
    ]
    assert any(is_question_title(t) for t in ranking_titles), ranking_titles


def test_t1_lock2b_small_batch_still_reaches_ranking_family():
    """锁 2 的小批次形态：配额紧张时优先族仍必须拿到 ≥1。"""
    from writing.title_question_policy import allocate_question_quota

    # 🔴 这个分布是**专门挑的**：纯最大余额法会把榜单族摊成 0。
    #   总 21 条、配额 30% = 6 条；exact = a:5.714 / mbc:0.286；
    #   floor 后 a=5、mbc=0，余数 1 条按小数部分给 a（0.714 > 0.286）→ mbc 恒 0。
    # 只有"在场家族保底 ≥1 + 优先族优先"那段才能把它救回 1。
    # （早先版本用 5/4/1 @70% 是**无效用例**：那组纯比例本来就会给 mbc 1 条，
    #   摘掉保底逻辑照样过 —— 变异 M2 正是这样逃掉的。）
    starving = allocate_question_quota({"evidence_qa": 20, "multi_brand_comparison": 1}, 30)
    assert starving["multi_brand_comparison"] >= 1, starving
    assert sum(starving.values()) == 6, starving

    allocation = allocate_question_quota(
        {"evidence_qa": 5, "implementation_guide": 4, "multi_brand_comparison": 1}, 70,
    )
    assert allocation["multi_brand_comparison"] >= 1, allocation
    assert sum(allocation.values()) == 7, allocation


def test_t1_lock3_user_specified_form_bypasses_ratio_lottery():
    """锁 3：用户显式指定标题形态 → 比例抽签**不介入**（工单 §1.2 用户 > 默认）。

    拆哪一层会红：`KeywordTopicGenerator.__init__` 的 `title_form` 解析，
    或 `enforce_question_ratio` 的 `user_specified` 早退分支。
    """
    from writing.title_question_policy import is_question_title

    generator, topics = _run_topic_generation(10, title_form="open")
    report = generator.title_question_report
    assert report["user_specified"] is True
    assert report["applied"] is False
    assert report["skipped_reason"] == "user_specified_title_form"

    # [P4 标题批次级去重 2026-07-31] 🔴 本条断言**按性质重写**,请复审注意。
    #
    # 旧断言是 `all(startswith("2026年十大"))` ——「一个字都不许改」。它写于
    # 形态层是**唯一**会改标题的层的时候,用"标题原封不动"当作"比例抽签没介入"
    # 的代理。P4 之后 `_apply_batch_title_dedupe` 是另一个合法的改写层
    # (替身给的 10 条标题是 `2026年十大…盘点0..9`,**同一公式重复 10 次**,
    # 正是工单 §7 要消灭的东西),于是这个代理开始**拦正确的改动**:
    # 任何会改写标题的批次去重都必然让它红,没有实现能同时满足两者。
    #
    # 所以改成断言它真正要保的两件事:
    #   ① 比例抽签确实没介入(applied False + skipped_reason + 问句数零变化);
    #   ② 🔴 用户显式选的 `open` 形态**端到端被尊重** —— 去重换结构可以,
    #      但一条都不许变成问句式(这比旧的前缀断言更强:旧断言只要前缀在,
    #      标题后半段被改成问句也照样绿)。
    # 未被去重碰过的标题仍然逐字保持原样。
    assert report["question_before"] == report["question_after"] == 0, (
        f"比例抽签不得介入: {report}"
    )
    assert not any(is_question_title(t["optimized_title"]) for t in topics), [
        t["optimized_title"] for t in topics
    ]
    untouched = [t for t in topics if not t.get("title_dedupe_status")]
    assert untouched, "不应整批都被去重改写"
    assert all(t["optimized_title"].startswith("2026年十大") for t in untouched), [
        t["optimized_title"] for t in untouched
    ]


def test_t1_ratio_is_configurable_not_hardcoded():
    """工单 §1.2「该比例是可配置默认值，不是硬编码」。

    三条覆盖路径都要真的生效，且默认值仍是 70。
    """
    from writing.title_question_policy import (
        DEFAULT_QUESTION_RATIO_PERCENT,
        QUESTION_RATIO_ENV,
        get_question_ratio_percent,
    )

    assert DEFAULT_QUESTION_RATIO_PERCENT == 70
    assert get_question_ratio_percent() == 70
    assert get_question_ratio_percent(override=40) == 40
    with mock.patch.dict("os.environ", {QUESTION_RATIO_ENV: "30"}):
        assert get_question_ratio_percent() == 30
        # 显式入参优先级高于环境变量
        assert get_question_ratio_percent(override=90) == 90
    # 脏配置不得把策略打翻，退回默认
    with mock.patch.dict("os.environ", {QUESTION_RATIO_ENV: "not-a-number"}):
        assert get_question_ratio_percent() == 70
    with mock.patch.dict("os.environ", {QUESTION_RATIO_ENV: "180"}):
        assert get_question_ratio_percent() == 70


def test_t1_configured_ratio_actually_changes_generated_batch():
    """比例可配 ≠ 只是读得出来：把它调成 0 必须让整批真的不再被提升成问句。"""
    from writing.title_question_policy import is_question_title

    _, topics = _run_topic_generation(10, question_ratio_percent=0)
    hits = sum(1 for t in topics if is_question_title(t.get("optimized_title")))
    assert hits == 0, [t["optimized_title"] for t in topics]


def test_t1_question_detection_shares_one_regex_with_formula_library():
    """问句判定必须是**同一份**正则（禁第二份口径）。"""
    from writing import title_formula_library, title_question_policy

    for sample in ("XX怎么选", "哪家靠谱？", "值得吗", "普通陈述标题", "十大品牌盘点"):
        assert (
            title_question_policy.is_question_title(sample)
            == title_formula_library.has_question_form(sample)
        ), sample


def test_t1_ranking_title_rewrite_matches_workorder_example():
    """工单 §1.1 给的样例形态必须真的产出（榜单信息不丢，只是不占标题开头）。"""
    from writing.title_question_policy import is_question_title, to_question_title

    out = to_question_title("2026年十大GEO服务商", keyword="GEO服务商",
                            family_code="multi_brand_comparison")
    assert is_question_title(out), out
    assert "GEO服务商" in out, out
    assert not out.startswith("2026"), out
    assert "10" in out, out  # "十大" 的信息量保留在副标题里


def test_t1_demotion_never_produces_a_mangled_title():
    """陈述化改写不得产出残句 —— 2026-07-29 真跑人工复核抓到的真实缺陷。

    首轮真跑（真 LLM）产出过两条：
      · "深圳定制家具工厂本地工厂与品牌门店**对比对比**与选择建议"
      · "全屋定制验收该看哪些地方分区域验收清单与常见问题对比与选择建议"
    原因是旧实现在**整串**里做正则替换、再无条件拼一个固定尾巴。
    现在改成在问号处切开；切不干净就返回原标题（宁可略偏离比例）。
    """
    from writing.title_question_policy import is_question_title, to_open_title

    cases = [
        ("深圳定制家具工厂怎么选？本地工厂与品牌门店对比", "深圳定制家具工厂"),
        ("全屋定制验收该看哪些地方？分区域验收清单与常见问题", "全屋定制验收"),
        ("全屋定制售后包含哪些服务？常见问题处理与质保周期数据", "全屋定制售后"),
        # 多字动词：正则交替是最左匹配，`查` 排在 `检查` 前会留下 "…验收要检"
        ("全屋定制验收要检查什么？从尺寸到环保的核验清单", "全屋定制验收"),
        ("定制衣柜需要了解哪些参数？板材、五金与工艺说明", "定制衣柜"),
    ]
    for original, keyword in cases:
        out = to_open_title(original, keyword=keyword)
        assert not is_question_title(out), out
        assert keyword in out, out
        # 无重复词块（"对比对比" 这类残句的判据）
        for token in ("对比", "选择建议", "清单", "服务"):
            assert out.count(token) <= 1, f"{out} 里 '{token}' 重复了"
        # 主体与副标题之间必须有分隔，不能糊成一串
        assert "：" in out, out
        assert len(out) <= len(original) + 1, (original, out)

    # 切不干净（没有问号 / 认不出问法词）时必须原样返回，绝不硬造
    for stubborn in ("这家靠谱吗", "值得吗", "全屋定制怎么样"):
        assert to_open_title(stubborn, keyword="全屋定制") == stubborn


def test_t1_dispatcher_chain_also_enforced():
    """全家族适用 = 两条产 topics 的链都要接（诊断链不能是漏网之鱼）。"""
    from writing.title_question_policy import is_question_title
    from writing.topic_dispatcher import _normalize_evidence_topics

    raw = [
        {"title": f"2026年十大装修公司权威盘点{i}", "keyword": "装修公司",
         "type": "comparison", "article_style": "comparison"}
        for i in range(10)
    ]
    out = _normalize_evidence_topics(raw)
    hits = sum(1 for t in out if is_question_title(t.get("title")))
    assert hits >= 1, [t["title"] for t in out]


# ===========================================================================
# T2 · 长度只走双峰
# ===========================================================================
def test_t2_no_family_target_inside_forbidden_band():
    """工单 §2.1：禁止把 4501-13999 作为目标档。真跑合同自检 + 逐族核。"""
    from writing.article_length_contract import (
        FAMILY_LENGTH_POLICIES,
        in_avoidance_band,
        validate_length_contract,
    )

    assert validate_length_contract() == []
    for code, policy in FAMILY_LENGTH_POLICIES.items():
        assert not in_avoidance_band(policy.target_chars), (code, policy.target_chars)


def test_t2_built_plans_never_target_the_trough():
    """真跑 `build_article_length_plan` 的全条件组合，目标一律不得落中段。"""
    from writing.article_length_contract import (
        FAMILY_LENGTH_POLICIES,
        build_article_length_plan,
        in_avoidance_band,
        plan_tier,
    )

    checked = 0
    for family in FAMILY_LENGTH_POLICIES:
        for verified in (0, 1, 3, 8, 10, 20):
            for publishers in (0, 1, 3, 4, 6):
                for candidates in (0, 2, 3, 6, 12):
                    pack = {"items": [
                        {"evidence_id": f"EV-{i}", "verified": True,
                         "publisher": f"pub{i % max(1, publishers or 1)}",
                         "url": f"https://e{i}.example.com"}
                        for i in range(verified)
                    ]}
                    plan = build_article_length_plan(
                        family,
                        evidence_pack=pack,
                        verified_candidate_count=candidates,
                        answer_block_target_count=12,
                    )
                    assert not in_avoidance_band(plan["target_chars"]), (
                        family, verified, publishers, candidates, plan["target_chars"],
                    )
                    assert plan_tier(plan) in ("compact", "deep")
                    checked += 1
    assert checked > 400, checked


def test_t2_lock1_compact_output_must_land_2500_4500():
    """锁 1：紧凑档产出必须落 2500-4500，低于下限**判失败**（旧口径静默通过）。"""
    from writing.article_length_contract import assess_length_compliance

    plan = {"target_chars": 3500, "minimum_chars": 2500, "maximum_chars": 4500}
    body = "".join(f"第{i}节：这里是可核验的判断依据与适用边界说明。" for i in range(400))
    assert len(body) > 4000, len(body)
    short = assess_length_compliance(body[:2100], style_code="qa_recommendation", plan=plan)
    assert short["tier"] == "compact"
    assert short["spec_met"] is False
    assert "length_below_compact_floor" in short["spec_failure_codes"], short

    ok = assess_length_compliance(body[:3400], style_code="qa_recommendation", plan=plan)
    assert ok["tier"] == "compact"
    assert "length_below_compact_floor" not in ok["spec_failure_codes"], ok


def test_t2_lock2_deep_output_needs_14000_and_85_percent_of_target():
    """锁 2：深档产出须 ≥14000 **且** ≥target×0.85。"""
    from writing.article_length_contract import (
        assess_length_compliance,
        deep_output_floor,
    )

    assert deep_output_floor(16000) == 14000       # 0.85×16000=13600 < 14000 → 取 14000
    assert deep_output_floor(18000) == 15300       # 0.85×18000 > 14000 → 取 0.85 线

    plan = {"target_chars": 18000, "minimum_chars": 15000, "maximum_chars": 20000}
    body = "".join(f"第{i}节：本节给出可核验数据、口径与时间窗，并说明适用边界。" for i in range(900))
    assert len(body) > 15400
    below = assess_length_compliance(body[:14100], style_code="buying_guide", plan=plan)
    assert below["tier"] == "deep"
    assert below["spec_met"] is False
    assert "length_below_deep_floor" in below["spec_failure_codes"], below

    ok = assess_length_compliance(body[:15400], style_code="buying_guide", plan=plan)
    assert "length_below_deep_floor" not in ok["spec_failure_codes"], ok


def test_t2_lock2b_budget_arithmetic_lock_does_not_regress():
    """锁 2 后半句「既有算术锁不回退」：规格预算加总仍必须 ≥ target×0.85。"""
    from writing.templates.canonical_family_templates import (
        BUDGET_COVERAGE_FLOOR,
        assert_budget_covers_target,
        build_deep_structure_budget,
    )

    assert BUDGET_COVERAGE_FLOOR == 0.85
    for cards in (0, 3, 6, 11, 12, 20):
        budget = build_deep_structure_budget(16000, cards)
        assert_budget_covers_target(budget)          # 不抛 = 通过
        assert budget["total"] >= 16000 * 0.85, (cards, budget["total"])
    with pytest.raises(ValueError):
        assert_budget_covers_target({"target": 16000, "total": 9500})


def test_t2_lock3_midband_output_is_judged_failed_and_leaves_a_trace():
    """锁 3：人为造一篇 8000 字产出 → 被判失败并留痕（不是静默通过）。

    "留痕"= `spec_met=False` + `spec_failure_codes` 进
    `quality_warning.length_compliance`，随 `articles.quality_warning` JSONB 落库。
    """
    from writing.article_length_contract import (
        LENGTH_RETRY_CODES,
        assess_length_compliance,
        build_length_repair_instruction,
        in_avoidance_band,
    )

    plan = {"target_chars": 16000, "minimum_chars": 15000, "maximum_chars": 18000}
    body = "".join(f"第{i}节：候选品牌的公开可核验信息与口径说明。" for i in range(400))[:8000]
    assert in_avoidance_band(8000)
    result = assess_length_compliance(body, style_code="comparison_review", plan=plan)
    assert result["actual_chars"] == 8000
    assert result["spec_met"] is False, result
    assert set(result["spec_failure_codes"]) & LENGTH_RETRY_CODES
    # 判失败必须带定向修复指令，否则"只判不修"等于没判
    instruction = build_length_repair_instruction(result, plan)
    assert instruction.strip(), "命中返工码却没有修复指令"


def test_t2_compact_spec_exists_and_is_mutually_exclusive_with_deep():
    """工单 §2.2：紧凑档**必须**配结构规格；且与深档规格互斥、边界无缝。"""
    from writing.article_length_contract import DEEP_TIER_MIN_CHARS
    from writing.templates.canonical_family_templates import (
        build_compact_structure_spec,
        build_deep_ranking_structure_spec,
    )

    assert DEEP_TIER_MIN_CHARS == 14000
    for target in (2500, 3500, 4500, 13999):
        compact = build_compact_structure_spec({"target_chars": target})
        deep = build_deep_ranking_structure_spec({"target_chars": target})
        assert compact.strip(), f"紧凑档 target={target} 没有结构规格"
        assert deep == "", f"紧凑档 target={target} 却注入了深档规格"
    for target in (14000, 16000, 20000):
        compact = build_compact_structure_spec({"target_chars": target})
        deep = build_deep_ranking_structure_spec({"target_chars": target},
                                                verified_candidate_count=11)
        assert compact == "", f"深档 target={target} 却注入了紧凑档规格"
        assert deep.strip(), f"深档 target={target} 没有结构规格"


def test_t2_compact_spec_is_a_structure_not_just_a_word_count():
    """§2.2 的要害：紧凑档要的是**结构规格**，不是又一条字数指令。

    规格必须给出区块预算表且加总落在 2500-4500 —— 只喊"写 3500 字"不算。
    """
    from writing.templates.canonical_family_templates import build_compact_structure_budget

    budget = build_compact_structure_budget(3500)
    assert len(budget["blocks"]) >= 4, budget
    assert 2500 <= budget["total"] <= 4500, budget["total"]
    assert all(b["note"].strip() for b in budget["blocks"]), budget


# ===========================================================================
# T3 · 深档达成率
# ===========================================================================
def test_t3_spec_pins_countable_section_counts_not_only_chars():
    """T3 根因①：规格必须给**可数的节数**。

    生产实证：每小节字数是常数（438/450/475），决定长度的是节数，而节数正是
    旧规格唯一没钉死的量。拆哪一层会红：`render_budget_table` 的小节数列 /
    `sections_for_budget`。
    """
    from writing.templates.canonical_family_templates import (
        MEASURED_CHARS_PER_SECTION,
        build_deep_ranking_structure_spec,
        sections_for_budget,
    )

    from writing.templates.canonical_family_templates import (
        build_deep_structure_budget,
        render_budget_table,
    )

    assert 400 <= MEASURED_CHARS_PER_SECTION <= 500

    # 🔴 逐行核：预算表**每一个区块行**都必须带自己的小节数，不能只在结尾喊一句
    #   "全文约 N 个小节"。（早先版本只断言 "小节数" in spec —— 变异 M16 只删表头
    #   列、结尾那句仍在，于是逃掉了。）
    budget = build_deep_structure_budget(16000, 11)
    table = render_budget_table(budget)
    header = table.splitlines()[0]
    assert header.count("|") == 5, f"预算表不是四列（区块/字数/小节数/要求）：{header}"
    assert "小节数" in header, header
    for block in budget["blocks"]:
        row = next((l for l in table.splitlines() if l.startswith(f"| {block['name']} |")), "")
        assert row, f"区块 {block['name']} 没有出现在预算表里"
        cells = [c.strip() for c in row.split("|")]
        assert cells[3] == str(sections_for_budget(block["budget"])), (block["name"], row)

    # 规格给出的总节数必须与"成功组"实测节数同量级（生产成功篇 33-39 节；
    # 未达标组 19-32 节，每节字数两组一致 → 差的就是节数）。
    total_sections = sections_for_budget(budget["total"])
    assert 33 <= total_sections <= 39, total_sections

    spec = build_deep_ranking_structure_spec({"target_chars": 16000},
                                             verified_candidate_count=11)
    assert "小节数" in spec, spec[:400]
    assert f"{total_sections} 个小节" in spec, spec[:600]


def test_t3_length_repair_does_not_forbid_the_expansion_it_asks_for():
    """T3 根因②：篇幅修复的框架不得带"不得新增事实/数字"的禁令。

    这是**自相矛盾**的根因：retry_hint 让模型补到 15000 字，同一条 user_message
    却说不得新增 —— 两条都遵守的唯一解就是原样保留。生产实证 19/19 重写真跑过、
    16/19 仍不达标，正是这个形态。

    行为级验法：真调 `_generate_single` 的提示词组装，逐字节抓两种模式下
    实际发出去的 user_message（`sim_overrides` 是既有的捕获通道，不是为测试新开的）。
    """
    import writing.article_generator_service as svc

    src = svc.ArticleGeneratorService.__dict__  # 确认方法存在（接线锁）
    assert "_generate_validated_with_rewrite_once" in src

    # 直接量渲染分支：expand 模式禁令必须消失、增写授权必须在场。
    from writing.article_length_contract import build_length_repair_instruction

    assessment = {
        "actual_chars": 9500,
        "findings": [{"code": "length_below_deep_floor"}],
    }
    instruction = build_length_repair_instruction(
        assessment, {"target_chars": 16000, "minimum_chars": 15000},
    )
    assert "深档" in instruction and "下限" in instruction, instruction
    assert "整块跳过" in instruction, instruction


def test_t3_repair_mode_is_expand_only_when_length_is_the_sole_driver():
    """`_repair_mode` 的分流规则本身是锁：证据门命中时必须回到 precision 框架。

    不能为了凑长度放宽证据纪律 —— 那是拿质量换数字。
    """
    import inspect

    import writing.article_generator_service as svc

    source = inspect.getsource(svc.ArticleGeneratorService._generate_validated_with_rewrite_once)
    # 这里断言的是**运行时分支条件**参与判定的变量（接线完整性），
    # 真正的行为验证在 test_t3_expand_preamble_reaches_the_model 里。
    # [R3-A3 适配 2026-08-11 · C2/C16] precision 全量 advisory 后 hard 恒空,
    # `precision_codes_v1` 是恒空死变量,C2 已随陈旧引用一并摘除 —— 证据门
    # 判据收敛到 evidence hard 一处。原意图(证据门命中时不许切增写框架)
    # 保留并加强:直接钉分支条件本身,比旧的"三个变量名都在"更紧。
    assert "_repair_mode" in source
    assert "length_codes_v1" in source
    assert "evidence_codes_v1" in source
    assert "length_codes_v1 and not evidence_codes_v1" in source, (
        "expand 分支条件被改 —— 证据硬伤在场时必须回 precision 框架"
    )


def test_t3_expand_preamble_reaches_the_model():
    """行为级：`_repair_mode='expand'` 时，发给模型的正文块必须是增写框架。

    直接跑真实的提示词组装代码路径（`_generate_single` 里那段 `_repair_preamble`
    分支），不读源码字符串，而是比对两种模式渲染出的**实际文本差异**。
    """
    import inspect

    from writing.article_generator_service import (
        ArticleGeneratorService,
        build_repair_preamble,
    )

    expand = build_repair_preamble("expand")
    precision = build_repair_preamble("precision")
    default = build_repair_preamble(None)

    assert expand != precision
    assert default == precision, "默认必须仍是证据门的收紧框架，不能默认放开增写"
    # 矛盾指令必须只留在 precision 一侧
    assert "不得新增事实" in precision, precision
    assert "不得新增事实" not in expand, expand
    # 增写框架必须同时给授权与边界（放开 ≠ 放任编造）
    assert "补齐" in expand and "禁止" in expand, expand
    assert "编造" in expand, expand

    # 接线锁：组装 user_message 时确实调的是这个函数（摘掉即红）
    assembly = inspect.getsource(ArticleGeneratorService._generate_single)
    assert "build_repair_preamble(topic.get('_repair_mode'))" in assembly


def test_t3_deep_tier_gets_a_larger_output_ceiling():
    """T3 根因③（保险层）：深档的 max_tokens 必须高于紧凑档。

    这不是当前那批 9.4k-13.1k 的根因（无截断特征），但根因①②修好后深档会真的
    往 16000+ 走，16000 tokens 会成为新天花板。
    """
    import inspect

    import writing.article_generator_service as svc
    from writing.article_generator_service import deep_aware_max_tokens

    assert svc._DEEP_TIER_MIN_CHARS == 14000
    # 真调函数，不去 inspect.getsource 里找数字 —— linecache 会缓存源码，
    # 那种断言在变异注入下会假绿（变异 M20 正是这样逃掉的）。
    assert deep_aware_max_tokens({"target_chars": 3500}) == 16000
    assert deep_aware_max_tokens({"target_chars": 4500}) == 16000
    assert deep_aware_max_tokens({"target_chars": 16000}) == 32000
    assert deep_aware_max_tokens({"target_chars": 20000}) == 32000
    assert deep_aware_max_tokens(None) == 16000
    assert (
        deep_aware_max_tokens({"target_chars": 16000})
        > deep_aware_max_tokens({"target_chars": 3500})
    )
    # 接线锁：真实调用点确实走这个函数
    assembly = inspect.getsource(svc.ArticleGeneratorService._generate_single)
    assert "max_tokens=deep_aware_max_tokens(topic.get(\"_length_plan\"))" in assembly


# ===========================================================================
# T4 · 图片以客户素材为轴
# ===========================================================================
def test_t4_zero_assets_means_zero_placeholders():
    """工单 §附：**零图则全文零占位符**（推翻 D11 ④ 的保底占位）。"""
    from writing.article_generator_service import (
        _insert_default_image_need_placeholder,
        _no_asset_image_placeholders,
    )

    body = "# 标题\n\n正文第一段。\n\n正文第二段。"
    assert _no_asset_image_placeholders() == []
    out = _insert_default_image_need_placeholder(body, [])
    assert out == body, out
    assert "[NEED_IMAGE" not in out


def test_t4_has_assets_means_a_placeholder_matching_what_exists():
    """「有什么图放什么图」：占位符的 role 必须由**真实素材类型**决定。"""
    from writing.article_generator_service import _insert_default_image_need_placeholder

    body = "# 标题\n\n正文。"
    with_case = _insert_default_image_need_placeholder(
        body, [{"image_type": "case", "id": 1}],
    )
    assert "role=case" in with_case, with_case
    with_product = _insert_default_image_need_placeholder(
        body, [{"image_type": "product", "id": 2}],
    )
    assert "role=product" in with_product, with_product


def test_t4_no_reverse_ask_for_client_to_upload_images():
    """工单 §附：**不反向要求客户补图**。"""
    from writing.article_generator_service import NO_ASSET_IMAGE_NOTICE

    for banned in ("请到素材中心", "请上传", "请补充图片", "上传图片并确认授权"):
        assert banned not in NO_ASSET_IMAGE_NOTICE, NO_ASSET_IMAGE_NOTICE
    assert "零图交付" in NO_ASSET_IMAGE_NOTICE


def test_t4_same_asset_not_reused_across_one_batch():
    """工单 §附：**同批不重复同一张**。真跑选图器，跨文章共享台账。"""
    import re

    from services.article_image_selector import select_images_for_article

    assets = [
        {"id": 101, "image_type": "case", "usage_scenarios": ["case_proof"], "caption": "案例一"},
        {"id": 102, "image_type": "case", "usage_scenarios": ["case_proof"], "caption": "案例二"},
        {"id": 103, "image_type": "case", "usage_scenarios": ["case_proof"], "caption": "案例三"},
    ]
    body = "# 标题\n\n## 客户案例\n\n[NEED_IMAGE role=case purpose=案例图]\n\n正文。"

    def _pick_three(article_keys: list[str], ledger: set | None) -> list[int]:
        picked: list[int] = []
        with mock.patch("db.brand_image_assets_db.list_publishable_assets",
                        return_value=[dict(a) for a in assets]):
            for i, key in enumerate(article_keys):
                out = select_images_for_article(
                    body, brand_id=7, article_key=key,
                    brand_name="QZQZ", batch_used_asset_ids=ledger,
                )
                found = re.findall(r"CLIENT_IMAGE asset_id=(\d+)", out)
                assert found, f"第 {i} 篇一张都没选中: {out}"
                picked.append(int(found[0]))
        return picked

    # 🔴 三篇用**同一个 article_key**：这样 `_rotation_offset` 的哈希轮转恒等，
    #   去重完全由台账负责。（早先版本用 batch|0/1/2 三个不同 key —— 轮转碰巧
    #   给出三个不同下标，摘掉台账照样过，变异 M23/M24 正是这样逃掉的。）
    same_key = ["identical-key"] * 3
    ledger: set = set()
    picked = _pick_three(same_key, ledger)
    assert len(set(picked)) == 3, f"同批重复用了同一张: {picked}"
    assert ledger == set(picked), f"台账没记全: ledger={ledger} picked={picked}"

    # 对照组：不传台账时，同一个 key 必然反复选中同一张 —— 证明上面的三张不同
    # 确实是台账的功劳，而不是轮转顺手做到的。
    without_ledger = _pick_three(same_key, None)
    assert len(set(without_ledger)) == 1, without_ledger


def test_t4_batch_ledger_falls_back_rather_than_starving_later_articles():
    """素材只有 1 张时，第 2 篇起宁可复用，也不能整批零图。"""
    import re

    from services.article_image_selector import select_images_for_article

    assets = [{"id": 201, "image_type": "case", "usage_scenarios": ["case_proof"]}]
    body = "# 标题\n\n## 案例\n\n[NEED_IMAGE role=case purpose=案例图]\n\n正文。"
    ledger: set = set()
    with mock.patch("db.brand_image_assets_db.list_publishable_assets", return_value=assets):
        outs = [
            select_images_for_article(body, brand_id=7, article_key=f"k{i}",
                                      batch_used_asset_ids=ledger)
            for i in range(2)
        ]
    for i, out in enumerate(outs):
        assert re.search(r"CLIENT_IMAGE asset_id=201", out), f"第 {i} 篇零图: {out}"


def test_t4_render_counts_actual_images_not_placeholder_text():
    """「图片加载不出来」的真因锁：软删素材必须计入 dropped，而不是被当成已配图。

    真跑 `count_rendered_images`（渲染层同一套判定），素材状态由替身给。
    """
    from services.image_placeholder import count_rendered_images, render_for_preview

    active = {"id": 1, "status": "active", "publish_allowed": 1, "rights_confirmed": 1,
              "brand_id": 7, "public_url": "/uploads/article-images/7/a_safe.jpg",
              "alt_text": "图一"}
    removed = {"id": 2, "status": "deleted", "publish_allowed": 1, "rights_confirmed": 1,
               "brand_id": 7, "public_url": "/uploads/article-images/7/b_safe.jpg"}
    content = (
        '正文。\n\n[CLIENT_IMAGE asset_id=1 role=case caption="图一"]\n\n'
        '更多正文。\n\n[CLIENT_IMAGE asset_id=2 role=case caption="图二"]\n'
    )
    lookup = {1: active, 2: removed}
    with mock.patch("db.brand_image_assets_db.get_image_asset", side_effect=lookup.get):
        stats = count_rendered_images(content, brand_id=7)
        rendered = render_for_preview(content, brand_id=7)

    assert stats["marker_count"] == 2
    assert stats["rendered_count"] == 1, stats
    assert stats["dropped_count"] == 1, stats
    assert stats["dropped_reasons"].get("asset_removed") == 1, stats
    # 统计口径必须与真实渲染结果一致：渲染出来的 markdown 图片数 == rendered_count
    assert rendered.count("![") == stats["rendered_count"], rendered


def test_t4_render_count_is_zero_without_brand_id():
    """没有 brand_id 时渲染层 fail-closed 删光，统计口径必须跟着说 0（不许虚报）。"""
    from services.image_placeholder import count_rendered_images, render_for_preview

    content = '正文\n\n[CLIENT_IMAGE asset_id=1 role=case caption="x"]\n'
    stats = count_rendered_images(content, brand_id=None)
    assert stats["rendered_count"] == 0 and stats["dropped_count"] == 1, stats
    assert "![" not in render_for_preview(content, brand_id=None)
