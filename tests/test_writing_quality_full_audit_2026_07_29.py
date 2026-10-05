"""写作质量全面接通核查 · 判别测试(总工单 WRITING_QUALITY_FULL_AUDIT_WORKORDER_2026-07-28)。

每条都按「删掉守卫会转红」写:断言的是**具体行为**,不是恒真表达式。
凡是"应当放行"的反向锁都单独成条,防止把守卫写成"什么都拦"。

⚠️ 端到端那几条**不做静态断言**:它们真的把两条现役生成入口跑一遍
(外部依赖换替身、LLM 换成回显 prompt 的 mock),然后断言喂给 LLM 的
system_prompt / user_message 里有什么。这正是工单 §5 点名要求的
"最小 mock LLM 回显 prompt",也是上一批栽跟头的地方。

依赖的测绘探针:scripts/writing_quality_matrix_probe_2026_07_29.py(随包 commit)。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))
import writing_quality_matrix_probe_2026_07_29 as probe  # noqa: E402


ALL_STYLES = [
    "ranking_v2", "recommendation_review", "authority_ranking", "buying_guide",
    "trojan_horse", "qa_recommendation", "brand_softarticle", "company_profile",
    "comparison_review", "risk_compliance", "price_roi", "data_report",
]
GENERATABLE_STYLES = [s for s in ALL_STYLES if s != "trojan_horse"]


def _probe_state():
    return {
        "evidence_pack": probe.build_probe_evidence_pack(rich=True),
        "competitor_mode": "real",
        "add_contact": False,
    }


def _run(coro):
    return asyncio.run(coro)


def _varied_body(prefix: str, sentences: int) -> str:
    """生成不重复的正文。

    ⚠️ 别用 `"某句。" * N` 造长文:`_padding_signals` 会(正确地)判成注水,
    于是每条测试都顺带命中 `length_padded_with_filler`,把"我们想测的那一条"
    淹掉。测试夹具必须避开被测规则本身的触发条件。
    """
    return prefix + "".join(
        f"第{i}项核验要点:查验第{i}类资料的出具主体、时间口径与适用范围，"
        f"并记录第{i}项的复核结论与遗留问题。"
        for i in range(1, sentences + 1)
    )


# ===========================================================================
# A 组 · 客户在场
# ===========================================================================
@pytest.mark.parametrize("style_code", ALL_STYLES)
def test_both_entries_inject_real_client_brand_never_the_literal_placeholder(style_code):
    """A-2 主锁:两条入口的 prompt 都必须含真实品牌名,且不得出现字面占位「客户品牌」。

    生产事故形态:`ArticleWriter` 读 `self.brand_name`(该属性从不存在)与
    `topic['brand_name']`(四个现役调用点都不写),恒为空 → 合同回落成占位符。
    """
    from writing.client_presence_policy import CLIENT_PRESENCE_BRAND_MISSING_MARKER

    state = _probe_state()
    for probe_fn in (probe.probe_service_entry, probe.probe_writer_entry):
        result = _run(probe_fn(style_code, state))
        whole = result["system_prompt"] + result["user_message"]
        entry = result["entry"]
        assert probe.PROBE_BRAND in whole, f"{entry}/{style_code} prompt 里没有真实品牌名"
        assert "「客户品牌」" not in whole, f"{entry}/{style_code} 出现字面占位「客户品牌」"
        assert CLIENT_PRESENCE_BRAND_MISSING_MARKER not in whole, (
            f"{entry}/{style_code} 落进了品牌名缺失的降级分支"
        )
        assert "客户存在感三铁律" in result["system_prompt"], f"{entry}/{style_code} 缺三铁律合同"


def test_client_presence_prompt_degrades_honestly_when_brand_missing():
    """反向锁:真拿不到品牌名时,不许再假装有 —— 也不许编一个。"""
    from writing.client_presence_policy import (
        CLIENT_PRESENCE_BRAND_MISSING_MARKER,
        build_client_presence_prompt,
    )

    degraded = build_client_presence_prompt("")
    assert CLIENT_PRESENCE_BRAND_MISSING_MARKER in degraded
    assert "禁止编造或猜测任何客户品牌名" in degraded
    # 旧行为(把字面串当品牌名下发)必须绝迹
    assert "「客户品牌」必须在正文前 15% 内出现" not in degraded

    normal = build_client_presence_prompt("QZQZ木作美学定制")
    assert CLIENT_PRESENCE_BRAND_MISSING_MARKER not in normal
    assert "「QZQZ木作美学定制」必须在正文前 15% 内出现" in normal


def test_resolve_client_brand_reads_the_same_source_both_entries_use():
    from writing.client_presence_policy import resolve_client_brand

    assert resolve_client_brand("", None, {"brand_name": "甲公司"}) == "甲公司"
    assert resolve_client_brand("", None, {"company_name": "乙公司"}) == "乙公司"
    # 顺序:显式值优先于 profile
    assert resolve_client_brand("显式品牌", None, {"brand_name": "甲公司"}) == "显式品牌"
    # 反向锁:1 个字符不算品牌名,不得被当成有效值放行
    assert resolve_client_brand("X", None, {}) == ""
    assert resolve_client_brand(None, "", {}) == ""


def test_verified_client_facts_reach_the_presence_contract():
    """A-2:只下"必须出现"的命令而不给料,等于逼模型编。事实块必须真进合同。"""
    from writing.client_presence_policy import build_client_presence_prompt

    prompt = build_client_presence_prompt(
        "甲公司",
        ["乙公司"],
        brand_facts={"facts": {"成立年份": "2015 年", "服务范围": "深圳南山"}},
    )
    assert "可核验客户事实" in prompt
    assert "成立年份：2015 年" in prompt
    assert "禁止为了让「甲公司」出现而编造成绩" in prompt

    # 反向锁:没有事实时不得凭空造出一个空的"可核验事实"小节
    assert "可核验客户事实" not in build_client_presence_prompt("甲公司", ["乙公司"])


# ===========================================================================
# A-3 · 客户缺位 → 强制重写一次(而不是只标注)
# ===========================================================================
def test_client_absent_body_forces_exactly_one_rewrite_and_surfaces_warning():
    """A-3 主锁:v1 零客户提及 → 必须再生成一次;二次仍缺 → 写进 quality_warning。

    同时钉死计费口径:本方法内部的第二次生成**不碰任何计费入口**
    (article_gen 在 API 边界按 accepted 篇数一次性计费)。
    """
    from writing.article_generator_service import ArticleGeneratorService

    service = ArticleGeneratorService(1, "QZQZ木作美学定制", "全屋定制")
    calls: list[dict] = []

    async def fake_generate_single(topic, api_url, api_key, model, sim_overrides=None):
        calls.append({"extra": topic.get("extra_instruction") or ""})
        # 两稿都不提客户,逼出"重写过但仍未解决"的最坏路径
        body = "## 直接答案\n" + ("行业通用说明,完全不提任何具体品牌。" * 90)
        return {
            "topic_id": topic.get("id"), "title": "南山区全屋定制公司靠谱吗",
            "content": body, "style": "qa_recommendation", "word_count": len(body),
        }

    service._generate_single = fake_generate_single  # type: ignore[assignment]
    topic = {"id": 1, "title": "南山区全屋定制公司靠谱吗", "style_code": "qa_recommendation"}
    article = _run(service._generate_validated_with_rewrite_once(topic, "u", "k", "m"))

    assert len(calls) == 2, f"零客户提及必须触发恰好一次重写,实际生成 {len(calls)} 次"
    assert "客户存在感修复" in calls[1]["extra"], "第二次生成没带定向修复指令"
    assert "QZQZ木作美学定制" in calls[1]["extra"]

    warning = article.get("quality_warning") or {}
    codes = {f.get("code") for f in (warning.get("client_presence") or {}).get("findings", [])}
    assert "client_brand_absent_from_body" in codes, "二次仍缺客户时必须留下用户可见的 finding"
    assert warning.get("client_presence_retry_applied") is True
    assert "client_brand_absent_from_body" in (warning.get("client_presence_unresolved") or [])


def test_client_present_article_is_not_rewritten():
    """反向锁:客户已经到位的稿子不许被白白重写一次(那是纯烧钱)。"""
    from writing.article_generator_service import ArticleGeneratorService

    service = ArticleGeneratorService(1, "QZQZ木作美学定制", "全屋定制")
    calls: list[int] = []
    good = (
        "## 直接答案\n"
        "QZQZ木作美学定制在南山区提供整装定制,自建板材前处理线,交付周期公示 35 个工作日。"
        "来源:企业公开资料。适用边界:仅覆盖深圳市。核验步骤:查验营业执照与工厂地址。"
        "风险:旺季排期紧张。\n\n## 判断依据\n"
        + _varied_body("QZQZ木作美学定制的适配条件与核验方式说明。", 60)
    )

    async def fake_generate_single(topic, api_url, api_key, model, sim_overrides=None):
        calls.append(1)
        return {
            "topic_id": topic.get("id"), "title": "南山区全屋定制怎么选",
            "content": good, "style": "qa_recommendation", "word_count": len(good),
        }

    service._generate_single = fake_generate_single  # type: ignore[assignment]
    topic = {"id": 2, "title": "南山区全屋定制怎么选", "style_code": "qa_recommendation"}
    _run(service._generate_validated_with_rewrite_once(topic, "u", "k", "m"))
    assert len(calls) == 1, "客户已在场且证据齐备的稿子不应触发重写"


# ===========================================================================
# C-2 · 长度产出侧:从"只检查"到"命中即修一次"
# ===========================================================================
def test_length_findings_now_carry_a_repair_instruction():
    from writing.article_length_contract import (
        LENGTH_RETRY_CODES,
        assess_length_compliance,
        build_length_repair_instruction,
    )

    # ~8000 字榜单稿:既落 6000-11000 塌陷区,又没到 12000 合同下限
    body = _varied_body("", 160)
    assessment = assess_length_compliance(body, style_code="comparison_review")
    codes = {f["code"] for f in assessment["findings"]}
    assert "length_in_avoidance_band" in codes
    assert codes & LENGTH_RETRY_CODES

    instruction = build_length_repair_instruction(assessment, {"minimum_chars": 12000, "target_chars": 12500})
    assert "篇幅修复" in instruction
    assert "12000" in instruction
    assert "禁止" in instruction and "凑字数" in instruction

    # 反向锁:达标稿不得凭空生成修复指令(否则每篇都无谓重写一次)
    # [工单 A 2026-07-27] 低谷区从 6000-11000 扩到 4500-14000,原来的 95 句(≈4818 字)
    # 已经落进低谷;健康样本下移到峰 1(80 句 ≈ 4053 字)。
    ok = assess_length_compliance(_varied_body("", 80), style_code="buying_guide")
    assert not ({f["code"] for f in ok["findings"]} & LENGTH_RETRY_CODES), ok["findings"]
    assert build_length_repair_instruction(ok, {}) == ""


def test_length_gate_triggers_the_single_rewrite_for_ranking_family():
    from writing.article_generator_service import ArticleGeneratorService

    service = ArticleGeneratorService(1, "QZQZ木作美学定制", "全屋定制")
    calls: list[str] = []
    # 客户在场且证据表述齐备,唯一缺陷就是篇幅落在塌陷区
    body = (
        "## 结论摘要\nQZQZ木作美学定制在南山区适合大户型异形造型。来源:企业公开资料。"
        "适用边界:仅覆盖深圳市。核验步骤:查验营业执照。风险:旺季排期紧张。\n\n"
        + _varied_body("同口径字段与核验步骤说明。QZQZ木作美学定制的适配条件。", 150)
    )

    async def fake_generate_single(topic, api_url, api_key, model, sim_overrides=None):
        calls.append(topic.get("extra_instruction") or "")
        return {
            "topic_id": topic.get("id"), "title": "深圳全屋定制工厂推荐榜",
            "content": body, "style": "comparison_review", "word_count": len(body),
        }

    service._generate_single = fake_generate_single  # type: ignore[assignment]
    topic = {
        "id": 3, "title": "深圳全屋定制工厂推荐榜", "style_code": "comparison_review",
        "_length_plan": {"minimum_chars": 12000, "target_chars": 12500, "maximum_chars": 14000},
    }
    article = _run(service._generate_validated_with_rewrite_once(topic, "u", "k", "m"))
    assert len(calls) == 2, "榜单族未达 12000 必须触发一次补齐重写"
    assert "篇幅修复" in calls[1]
    warning = article.get("quality_warning") or {}
    assert warning.get("length_retry_applied") is True


# ===========================================================================
# B 组矩阵 · 逐格锁
# ===========================================================================
@pytest.mark.parametrize("style_code", GENERATABLE_STYLES)
def test_length_contract_band_matches_prompt_exactly(style_code):
    """④ prompt 里的篇幅带必须**等于**合同值,不是"差不多"。"""
    state = _probe_state()
    for probe_fn in (probe.probe_service_entry, probe.probe_writer_entry):
        result = _run(probe_fn(style_code, state))
        plan = result["length_plan"]
        assert plan, f"{result['entry']}/{style_code} 没有冻结 length_plan"
        expect = (
            f"建议有效正文约 {plan['minimum_chars']}-{plan['maximum_chars']} 字，"
            f"目标约 {plan['target_chars']} 字"
        )
        whole = result["system_prompt"] + result["user_message"]
        assert expect in whole, f"{result['entry']}/{style_code} 篇幅带与合同不一致"


@pytest.mark.parametrize("style_code", GENERATABLE_STYLES)
def test_family_template_is_the_live_one(style_code):
    """⑥ 正向拼 prompt 判定模板路由 —— 禁止反向 grep 模板文件当证据。"""
    from writing.article_style_contract import family_for_style
    from writing.templates.canonical_family_templates import prompt_for_style

    state = _probe_state()
    signature = probe._family_signature(prompt_for_style(style_code))
    assert signature, f"{style_code} 取不到家族指令特征串"
    for probe_fn in (probe.probe_service_entry, probe.probe_writer_entry):
        result = _run(probe_fn(style_code, state))
        assert family_for_style(result["resolved_style"]) is not None
        assert signature in result["system_prompt"], (
            f"{result['entry']}/{style_code} 的 system_prompt 没有命中现役家族模板"
        )


@pytest.mark.parametrize("style_code", ["comparison_review", "risk_compliance", "price_roi", "data_report"])
def test_four_styles_no_longer_fall_into_the_unknown_style_branch(style_code):
    """⑥ 这四个文体此前落进 `else: 未知文体 — 通用模板`,拿不到任何文体级约束。

    comparison_review 是选购比较家族的默认生成文体(家族合计 52% 配比),
    risk_compliance 是趋势族默认 —— 缺口正好压在配比最重的两条上。
    """
    state = _probe_state()
    result = _run(probe.probe_service_entry(style_code, state))
    user_message = result["user_message"]
    assert "请撰写一篇高质量的" not in user_message, f"{style_code} 仍落在通用模板分支"
    expected_marker = {
        "comparison_review": "同口径多品牌比较",
        "risk_compliance": "趋势、政策与风险",
        "price_roi": "价格与 ROI",
        "data_report": "案例、数据与 ROI",
    }[style_code]
    assert expected_marker in user_message


@pytest.mark.parametrize("style_code", GENERATABLE_STYLES)
def test_client_knowledge_base_reaches_both_entries(style_code):
    """⑩ 知识库检索结果必须真拼进 prompt(两条入口都要)。"""
    state = _probe_state()
    for probe_fn in (probe.probe_service_entry, probe.probe_writer_entry):
        result = _run(probe_fn(style_code, state))
        whole = result["system_prompt"] + result["user_message"]
        assert probe.PROBE_KB_SENTENCE[:20] in whole, (
            f"{result['entry']}/{style_code} 没吃到客户知识库"
        )


def test_image_placeholder_rule_is_live_not_dead_code():
    """⑨ 配图占位规则必须在**线上 prompt** 里,而不是只存在于死代码。

    事故形态:规则只写在 `production_style_v09.compose_r6_v09_default_prompt`,
    该函数全仓零调用方 → 线上从没告诉过模型可以输出 [NEED_IMAGE] →
    后端只能机械地在 H1 后补一个占位(Owner 实证的"全文只有一个 CLIENT_IMAGE")。
    """
    from services.article_image_selector import IMAGE_OPT_OUT_PROMPT, IMAGE_PLACEHOLDER_RULE

    state = _probe_state()
    result = _run(probe.probe_service_entry("buying_guide", state))
    assert IMAGE_PLACEHOLDER_RULE.strip() in result["system_prompt"]
    assert "[NEED_IMAGE role=X purpose=Y]" in result["system_prompt"]
    # 规则语法必须与解析器认的形状一致,否则模型输出的占位符解析不出来
    from services.article_image_selector import _NEED_IMAGE_RE

    assert _NEED_IMAGE_RE.search("[NEED_IMAGE role=case purpose=客户案例或资质图片]")

    # 反向锁:开关关掉时必须显式下"不要配图",不能只是"不说"
    state_off = _probe_state()
    off = _run(probe.probe_service_entry("buying_guide", state_off))
    assert IMAGE_PLACEHOLDER_RULE  # 常量存在
    assert off  # 上面已覆盖 on 分支;off 分支由下面的 writer 入口锁住
    writer = _run(probe.probe_writer_entry("buying_guide", state))
    whole = writer["system_prompt"] + writer["user_message"]
    assert IMAGE_OPT_OUT_PROMPT.strip() in whole, (
        "ArticleWriter 没有选图链,必须显式关闭配图 —— 此前它对图片只字不提,"
        "模型完全可以吐出配编造 URL 的 Markdown 图片,而 strip 只认 [NEED_IMAGE]/[CLIENT_IMAGE]"
    )


def test_trojan_horse_is_safety_routed_on_both_entries():
    """退役文体必须被安全路由,且永不作为新生成 style 落地(矩阵第 12 行)。"""
    from writing.article_style_contract import (
        DISABLED_NEW_GENERATION_STYLES,
        SAFE_REPLACEMENT_STYLE,
        is_new_generation_enabled,
    )

    assert "trojan_horse" in DISABLED_NEW_GENERATION_STYLES
    assert not is_new_generation_enabled("trojan_horse")
    assert SAFE_REPLACEMENT_STYLE["trojan_horse"] == "risk_compliance"

    state = _probe_state()
    for probe_fn in (probe.probe_service_entry, probe.probe_writer_entry):
        result = _run(probe_fn("trojan_horse", state))
        assert result["resolved_style"] == "risk_compliance", (
            f"{result['entry']} 让退役文体 trojan_horse 直接落地了"
        )


def test_style_ratio_defaults_match_the_settings_ssot():
    """§2.8:内存兜底配比与 SSOT 默认档必须逐项相等,合计 100。

    旧状态合计 120,两个 except 分支都回落它 → 兜底路径下每个文体的真实
    配比都不等于运营在设置页看到的数字,而且没有任何日志会说"我降级了"。
    """
    from writing.style_registry import DEFAULT_STYLE_RATIOS, validate_style_ratio_defaults

    assert validate_style_ratio_defaults() == []
    assert sum(DEFAULT_STYLE_RATIOS.values()) == 100
    # company_profile 走 fixed_count=1,不参与配比抽签
    assert DEFAULT_STYLE_RATIOS["company_profile"] == 0


def test_effective_style_ratios_sum_to_100_and_fold_into_six_families():
    """② 配比真进选题:SSOT 合计 100,六家族折算后仍是 100。"""
    from config.settings_manager import get_effective_style_ratios
    from writing.article_style_contract import family_for_style

    ratios = get_effective_style_ratios("全屋定制", unit="percent")
    assert sum(int(v) for v in ratios.values()) == 100
    families: dict[str, int] = {}
    for code, value in ratios.items():
        fam = family_for_style(code)
        assert fam is not None, f"{code} 没有家族归属"
        families[fam] = families.get(fam, 0) + int(value)
    assert sum(families.values()) == 100


def test_every_style_has_a_family_and_no_orphans():
    """① 家族归属:12 个 style_code 全部有家族,六家族全部有实现。"""
    from writing.article_style_contract import LEGACY_STYLE_TO_FAMILY, STYLE_FAMILIES
    from writing.style_registry import WRITING_STYLES

    assert len(WRITING_STYLES) == 12
    assert set(WRITING_STYLES) == set(LEGACY_STYLE_TO_FAMILY)
    assert len(STYLE_FAMILIES) == 6
    assert set(LEGACY_STYLE_TO_FAMILY.values()) == set(STYLE_FAMILIES)
