"""GEO article style contract SSOT.

This module owns the outward six-family taxonomy, legacy routing, and the
non-negotiable writing contract.  Historical ``style_code`` values remain
readable, but high-risk styles can never be selected for new generation.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Final


STYLE_CONTRACT_VERSION: Final = "geo-article-style-v1.4"


@dataclass(frozen=True)
class ArticleStyleFamily:
    code: str
    name: str
    purpose: str
    required_sections: tuple[str, ...]
    evidence_requirements: tuple[str, ...]
    allowed_legacy_styles: tuple[str, ...]


STYLE_FAMILIES: Final[dict[str, ArticleStyleFamily]] = {
    "evidence_qa": ArticleStyleFamily(
        code="evidence_qa",
        name="证据型问答",
        purpose="直接回答一个或一组真实问题，每个答案可独立理解和复核。",
        required_sections=("直接答案", "判断依据", "步骤或条件", "边界", "延伸提示"),
        evidence_requirements=("官方定义或至少两个独立专业来源", "问题与答案一一对应", "事实旁给来源"),
        allowed_legacy_styles=("qa_recommendation",),
    ),
    "multi_brand_comparison": ArticleStyleFamily(
        code="multi_brand_comparison",
        name="选购与多品牌比较",
        purpose="按同一字段比较真实候选，可回答排名、推荐和适配问题，并披露依据、限制与核验方式。",
        required_sections=("结论摘要", "入选标准", "同口径表", "品牌资料卡", "场景建议", "采购要点"),
        evidence_requirements=("候选必须真实且可核验", "同一比较字段", "客户与竞品采用同一证据标准"),
        allowed_legacy_styles=("comparison_review", "recommendation_review", "ranking_v2", "authority_ranking"),
    ),
    "implementation_guide": ArticleStyleFamily(
        code="implementation_guide",
        name="方法与实施指南",
        purpose="帮助读者建立选择标准、预算边界和执行步骤。",
        required_sections=("目标", "前置条件", "步骤", "检查点", "风险", "验收"),
        evidence_requirements=("参数定义有来源", "价格注明时间与适用条件", "建议可执行"),
        allowed_legacy_styles=("buying_guide",),
    ),
    "trend_policy_risk": ArticleStyleFamily(
        code="trend_policy_risk",
        name="趋势、政策与风险分析",
        purpose="解释趋势、变化或数据的含义，形成趋势到影响再到行动的证据链。",
        required_sections=("变化", "证据", "影响", "风险", "行动", "不确定性"),
        evidence_requirements=("最新监管、标准或多源研究", "相关性不写成因果", "预测与事实分开"),
        allowed_legacy_styles=("trojan_horse", "risk_compliance"),
    ),
    "case_data_roi": ArticleStyleFamily(
        code="case_data_roi",
        name="案例、数据与 ROI",
        purpose="基于可验证案例或有边界的情景测算解释结果与成本收益。",
        required_sections=("背景", "行动", "数据", "限制", "情景测算", "复核路径"),
        evidence_requirements=("案例授权或公开", "时间窗、样本与口径明确", "价格版本可追溯"),
        allowed_legacy_styles=("data_report", "price_roi"),
    ),
    "company_facts": ArticleStyleFamily(
        code="company_facts",
        name="企业事实与品牌说明",
        purpose="说明企业能力与适配场景，明确区分官方事实、客户自述和独立证据。",
        required_sections=("快速事实", "产品或资质", "场景", "证据", "适用与不适用", "联系方式"),
        evidence_requirements=("Brand Fact Snapshot", "官方与独立证据分开"),
        allowed_legacy_styles=("company_profile", "brand_softarticle"),
    ),
}


LEGACY_STYLE_TO_FAMILY: Final[dict[str, str]] = {
    style: family.code
    for family in STYLE_FAMILIES.values()
    for style in family.allowed_legacy_styles
}

# These identifiers stay readable for historical records, but their original
# prompts and ratios are permanently unavailable for new generation.
#
# [WP12 P0-2 · Master SSOT v2.4 ①② / v2.3 ①②] ``ranking_v2`` and
# ``authority_ranking`` are back.  The flywheel refuted "榜单已死": ranking/
# recommendation shape held 44.7% -> 43.2% of cited articles across the 07-12
# inflection, and the single confirmed success case we own is a TOP10 ranking
# article on a local party-media portal.  What engines actually punish is the
# **fabricated scoring system**, not the list shape — so the two styles return
# under a stricter contract (RANKING_REVIVAL_CONTRACT) instead of staying
# retired.  ``trojan_horse`` stays retired: its defect is the fake-analyst
# identity, which no contract can repair.
DISABLED_NEW_GENERATION_STYLES: Final[frozenset[str]] = frozenset({"trojan_horse"})

# Revived under contract — kept as an explicit set so ratio validation, prompt
# composition and tests all read the same SSOT instead of hardcoding names.
REVIVED_RANKING_STYLES: Final[frozenset[str]] = frozenset({"ranking_v2", "authority_ranking"})

# The non-negotiable conditions attached to the revival.  Every generation path
# that can emit a ranking must render this contract; dropping any one of the
# three is what produced the fabricated-score articles engines de-ranked.
RANKING_REVIVAL_CONTRACT_VERSION: Final = "geo-ranking-revival-v1.0"
RANKING_REVIVAL_CONTRACT: Final = (
    "【榜单复活合同 " + RANKING_REVIVAL_CONTRACT_VERSION + "】\n"
    "1. 可核验依据：每一个位次都必须落在真实公开信息或飞轮蒸馏出的真实事实上，"
    "并在正文里用自然行文交代该依据（“据《XX》报道”“根据 XX 公开的资质记录”）。\n"
    "2. 禁自创评分：不得制造综合分、百分制、星级、S/A/B 等级或自创权重冒充独立评价。"
    "引用真实平台已公开的评分时必须写清平台、口径与时间。\n"
    "3. 依据不足的位次：不写死名次，改成条件化表述（“在 X 场景下更适合”）；"
    "系统会把该位次记进内部待人工确认清单，正文里不留任何核验痕迹。"
)

SAFE_REPLACEMENT_STYLE: Final[dict[str, str]] = {
    # Keep the only defensible part of the retired template: trend -> impact ->
    # action.  Routing it to a case/data template would silently change intent.
    "trojan_horse": "risk_compliance",
}

# New product surfaces persist the outward family code directly. Historical
# ``user_choice`` values remain readable through one explicit compatibility
# map, so UI, title planning and body generation cannot drift independently.
USER_CHOICE_FAMILY_CODES: Final[tuple[str, ...]] = tuple(STYLE_FAMILIES.keys())
USER_CHOICE_OPTIONS: Final[frozenset[str]] = frozenset(("auto", *USER_CHOICE_FAMILY_CODES))
USER_CHOICE_LABELS: Final[dict[str, str]] = {
    code: family.name for code, family in STYLE_FAMILIES.items()
}

# ---------------------------------------------------------------------------
# [#185] 对外方向 ⊋ 正文家族
# ---------------------------------------------------------------------------
#: 只在**对外**成立的方向 -> 它的正文家族。
#:
#: 🔴 防御型(公司词)**不是第七个家族**。WO_185 §6 的原话是「新值
#:    `defensive_company`(**映射 company_facts 家族**),不是复用」——
#:    它的正文合同**就是** company_facts 的:六段结构、长度策略、规格卡、
#:    家族提示词、禁多品牌对比,全部共用。区别只在**题的主语**是公司本身。
#:
#: 🔴 为什么不做成家族(2026-09-13 实测,c1 曾经做成家族并当场被十条既有判据抓住):
#:    `STYLE_FAMILIES` 是**正文合同**的注册表。多一个键,
#:    `FAMILY_LENGTH_POLICIES` / `_FAMILY_INSTRUCTIONS` / 标题年份规则 /
#:    规格卡就各缺一份 —— 缺的那份不报错,只是让防御篇**用默认合同生成**,
#:    正好不是「介绍公司」六段。补齐则是把 company_facts 复制一份,
#:    而复制出来的那份在改 company_facts 时**没有任何东西提醒你同步**。
#:    并且 `validate_style_contract` 的 `generation_style_family_mismatch`
#:    本来就会拒绝它(它映射 brand_softarticle,而该样式属 company_facts)。
OUTWARD_CHOICE_TO_FAMILY: Final[dict[str, str]] = {
    "defensive_company": "company_facts",
}

#: 用户**可以选**的方向全集 = auto + 六家族 + 对外方向。
#:
#: 🔴 与 `USER_CHOICE_OPTIONS` 分开是**故意的**,不是冗余:那个名字同时在扮
#:    四个角色 —— ①可选集校验 ②系统推荐配比的定义域 ③正文合同自洽校验
#:    ④飞轮策略输入。把新值塞进它,四个角色会一起被改:推荐配比凭空多一档、
#:    自洽校验当场红、飞轮开始学一个它不该看见的方向。
#:    所以**拆名**:能选的用这个,配比域与合同校验仍是那六个。
USER_CHOICE_SELECTABLE: Final[frozenset[str]] = frozenset(
    ("auto", *USER_CHOICE_FAMILY_CODES, *OUTWARD_CHOICE_TO_FAMILY)
)

#: 方向选择器(快捷模式 / 配比对话框 / 单篇下拉)要显示的标签。
#: 顺序有意义:六家族在前、对外方向在后,与 UI 排列一致。
USER_CHOICE_SELECTABLE_LABELS: Final[dict[str, str]] = {
    **USER_CHOICE_LABELS,
    "defensive_company": "防御型(公司词)",
}

FAMILY_TO_GENERATION_STYLE: Final[dict[str, str]] = {
    "evidence_qa": "qa_recommendation",
    "multi_brand_comparison": "comparison_review",
    "implementation_guide": "buying_guide",
    "trend_policy_risk": "risk_compliance",
    "case_data_roi": "data_report",
    "company_facts": "brand_softarticle",
}
# 🔴 [#185] 这张表**只覆盖六个家族**,防御型不在里面 —— 它不是家族。
#    它的生成样式经 `OUTWARD_CHOICE_TO_FAMILY` 落到 company_facts 再取,
#    所以 `topics.style_code` 与普通 company_facts 篇**逐字相同**
#    ⇒ side 不能由 style_code 派生,必须由 `topics.user_choice` 派生
#    (185-c1 逐跳实测后 Review 改判)。`validate_style_contract` 里
#    `generation_style_map_must_cover_six_families` 那条正是钉这张表的边界。

LEGACY_USER_CHOICE_TO_FAMILY: Final[dict[str, str]] = {
    "qa": "evidence_qa",
    "comparison": "multi_brand_comparison",
    "guide": "implementation_guide",
    "checklist": "implementation_guide",
    "risk": "trend_policy_risk",
    "price": "case_data_roi",
    "data": "case_data_roi",
    "case": "case_data_roi",
    "story": "company_facts",
}

# Preserve the precise implementation chosen by historical clients while all
# new product surfaces persist the broader six-family code.
LEGACY_USER_CHOICE_TO_GENERATION_STYLE: Final[dict[str, str]] = {
    "qa": "qa_recommendation",
    "comparison": "comparison_review",
    "guide": "buying_guide",
    "checklist": "buying_guide",
    "risk": "risk_compliance",
    "price": "price_roi",
    "data": "data_report",
    "case": "data_report",
    "story": "brand_softarticle",
}


def normalize_user_choice(
    user_choice: str | None,
    *,
    allow_auto: bool = True,
) -> str | None:
    """Normalize a new or historical UI choice to one outward family code.

    🔴 [#185] 对外方向(`OUTWARD_CHOICE_TO_FAMILY`)**原样返回,不折成家族**。
       折了的话 `topics.user_choice` 落库就是 `company_facts`,与普通
       company_facts 篇逐字相同 —— 而 side 正是靠这一列区分两者的,
       折叠之后「哪些篇是防御型」就**永久无法恢复**(style_code 也相同)。
       想拿正文家族请用 `family_for_user_choice`。
    """
    raw = str(user_choice or "").strip()
    if not raw or raw == "auto":
        return "auto" if allow_auto else None
    if raw in STYLE_FAMILIES or raw in OUTWARD_CHOICE_TO_FAMILY:
        return raw
    return LEGACY_USER_CHOICE_TO_FAMILY.get(raw)


def family_for_user_choice(user_choice: str | None) -> str | None:
    """对外方向 -> **正文家族**(六个之一)。

    与 `normalize_user_choice` 的分工:那个回答「这一列该存什么」,
    这个回答「正文该按哪份合同写」。防御型两者不同,别的方向两者相同 ——
    正因为只有一个方向两者不同,合起来写一个函数迟早会被用错。
    """
    code = normalize_user_choice(user_choice, allow_auto=False)
    if code is None:
        return None
    return OUTWARD_CHOICE_TO_FAMILY.get(code, code if code in STYLE_FAMILIES else None)


def generation_style_for_user_choice(user_choice: str | None) -> str | None:
    """Resolve a UI choice without exposing internal template identifiers."""
    family_code = family_for_user_choice(user_choice)
    if family_code is None:
        return None
    return FAMILY_TO_GENERATION_STYLE[family_code]


def family_for_style(style_code: str | None) -> str | None:
    """Return the outward family without guessing unknown historical values.

    [2026-08-09] 第三级:中文 ``article_style`` 别名(「问答FAQ」「趋势洞察」…)。
    这些值是选题生成器**自己写进 `topics.article_style` 的**,写作大厅拿它当徽章展示,
    可这里一律返 None —— 于是「展示什么默认什么」在存量选题上大面积失效。

    🔴 不新建第四张表:中文别名的 SSOT 早就有,是 ``style_registry.STYLE_ALIAS_TO_CODE``
    (`normalize_style_code` 的那张)。这里只是把它接上,别名怎么归一由它一家说了算。
    延迟导入 —— ``style_registry`` 反过来 import 本模块,模块级会成环。
    """
    raw = str(style_code or "").strip()
    if raw in STYLE_FAMILIES:
        return raw
    mapped = LEGACY_STYLE_TO_FAMILY.get(raw)
    if mapped:
        return mapped
    from writing.style_registry import normalize_style_code

    canonical = normalize_style_code(raw)
    return LEGACY_STYLE_TO_FAMILY.get(canonical or "")


def resolve_new_generation_style(style_code: str | None) -> str | None:
    """Route disabled historical choices to a safe maintained implementation."""
    raw = str(style_code or "").strip()
    if not raw:
        return None
    return SAFE_REPLACEMENT_STYLE.get(raw, raw)


def is_new_generation_enabled(style_code: str | None) -> bool:
    raw = str(style_code or "").strip()
    return bool(raw and raw not in DISABLED_NEW_GENERATION_STYLES)


def family_contract_prompt(style_code: str | None) -> str:
    """Render a compact prompt fragment from the canonical family contract."""
    family_code = family_for_style(style_code)
    family = STYLE_FAMILIES.get(family_code or "")
    if not family:
        return "文体映射未知：不得猜测文体效果；按证据优先的通用解释结构写作。"
    sections = " → ".join(family.required_sections)
    evidence = "；".join(family.evidence_requirements)
    return (
        f"【文体合同 {STYLE_CONTRACT_VERSION}】\n"
        f"外部文体：{family.name}（{family.code}）\n"
        f"目标：{family.purpose}\n"
        f"推荐结构：{sections}\n"
        f"证据要求：{evidence}\n"
    )


_LEGACY_PLAN_TYPE_TO_FAMILY: Final[dict[str, str]] = {
    "authority_ranking": "multi_brand_comparison",
    "deep_comparison": "multi_brand_comparison",
    "case_study": "case_data_roi",
    "avoid_pitfalls": "implementation_guide",
    "trend_insight": "trend_policy_risk",
    "faq": "evidence_qa",
    "selection_guide": "implementation_guide",
    "expert_opinion": "trend_policy_risk",
}


def _title_subject(industry: str, keywords: list[Any], index: int) -> str:
    raw: Any = keywords[(max(1, int(index)) - 1) % len(keywords)] if keywords else ""
    if isinstance(raw, dict):
        raw = raw.get("keyword") or raw.get("text") or raw.get("name") or ""
    subject = re.sub(r"\s+", "", str(raw or "").strip())
    return subject or f"{str(industry or '行业').strip()}服务"


def evidence_first_title_for_family(
    *,
    brand_name: str,
    industry: str,
    family_or_style: str,
    keywords: list[Any],
    index: int,
) -> str:
    """Deterministic title fallback shared by every legacy/new entry point.

    These are question-led, unordered titles.  They deliberately contain no
    TOP/rank/list/first-place/score vocabulary and require no LLM availability.

    [P2 标题年份合同 2026-08-08] 年份不再逐族手写。改动前这里的口径是
    "指南/趋势带年份、榜单不带",与同仓另外三层各不相同;现在统一从
    ``title_element_contract`` 取:该族默认带年份才有年份片段,且**一律不作开头**
    (硬规则④)。``conditional`` 族在这条无 LLM 的确定性路径上取保守侧。
    """
    raw = str(family_or_style or "").strip()
    family = (
        raw if raw in STYLE_FAMILIES
        else _LEGACY_PLAN_TYPE_TO_FAMILY.get(raw)
        or family_for_style(raw)
        or "implementation_guide"
    )
    subject = _title_subject(industry, keywords, index)
    from .title_element_contract import current_year, deterministic_year_token

    year = current_year()
    y = deterministic_year_token(family, year)
    templates = {
        "evidence_qa": f"{subject}怎么选？{y}常见问题、证据边界与核验方法",
        "multi_brand_comparison": f"{subject}服务商怎么比较？{y}同口径证据与适用场景",
        # 措辞一字未动(只摘了年份)。本条含「指南」字样、与选题 prompt 的
        # 「指南族禁体裁标签」相抵触 —— 那是本包**之外**的既存缺陷,已在交付单自曝,
        # 不在年份包里顺手改(改文案属另一口径,要另配判据)。
        "implementation_guide": f"{subject}选型指南：{y}条件、步骤、风险与验收方法",
        "trend_policy_risk": f"{subject}有哪些变化？{y}证据、影响、风险与行动建议",
        "case_data_roi": f"{subject}案例怎么核验？{y}数据口径、成本边界与复核路径",
        "company_facts": f"{str(brand_name or '企业').strip()}适合哪些{subject}场景？{y}事实、证据与适用边界",
    }
    title = templates[family]
    from .evidence_first_policy import evaluate_content_trust

    if evaluate_content_trust(title, "").hard:
        # Fail closed even if a future subject itself contains promotional
        # ranking language supplied by legacy data.  年份在这条兜底里一律不写:
        # 失去了 subject 就无从判断文体,取最保守侧。
        title = "相关服务怎么选？条件、证据边界与核验方法"
    return title


def ensure_evidence_first_title(
    title: str,
    *,
    brand_name: str,
    industry: str,
    family_or_style: str,
    keywords: list[Any],
    index: int,
) -> str:
    """Keep a safe LLM title or replace it with the six-family SSOT title."""
    candidate = str(title or "").strip()
    from .evidence_first_policy import evaluate_content_trust

    if candidate and not evaluate_content_trust(candidate, "").hard:
        return candidate
    return evidence_first_title_for_family(
        brand_name=brand_name,
        industry=industry,
        family_or_style=family_or_style,
        keywords=keywords,
        index=index,
    )


def validate_style_contract() -> list[str]:
    """Static SSOT audit used by tests and startup diagnostics."""
    errors: list[str] = []
    if len(STYLE_FAMILIES) != 6:
        errors.append("outward_family_count_must_equal_6")
    seen: set[str] = set()
    for family in STYLE_FAMILIES.values():
        for style in family.allowed_legacy_styles:
            if style in seen:
                errors.append(f"legacy_style_mapped_twice:{style}")
            seen.add(style)
    if set(FAMILY_TO_GENERATION_STYLE) != set(STYLE_FAMILIES):
        errors.append("generation_style_map_must_cover_six_families")
    for family_code, style_code in FAMILY_TO_GENERATION_STYLE.items():
        if LEGACY_STYLE_TO_FAMILY.get(style_code) != family_code:
            errors.append(f"generation_style_family_mismatch:{family_code}:{style_code}")
    if USER_CHOICE_OPTIONS != frozenset(("auto", *STYLE_FAMILIES.keys())):
        errors.append("user_choice_options_must_equal_auto_plus_six_families")
    # [#185] 对外方向的自洽:这段是新概念唯一的门,别的地方都只是读它。
    for choice, family_code in OUTWARD_CHOICE_TO_FAMILY.items():
        if choice in STYLE_FAMILIES:
            # 同名会让 `normalize_user_choice` 的两条分支互相遮蔽,
            # 而遮蔽的那一侧不会报错,只是行为悄悄换一条路。
            errors.append(f"outward_choice_shadows_family:{choice}")
        if family_code not in STYLE_FAMILIES:
            errors.append(f"outward_choice_family_unknown:{choice}:{family_code}")
        if choice in FAMILY_TO_GENERATION_STYLE:
            # 进了这张表就等于声称自己是家族,正文合同各表却没有它那一份。
            errors.append(f"outward_choice_must_not_be_a_family:{choice}")
    if USER_CHOICE_SELECTABLE != frozenset(
            ("auto", *STYLE_FAMILIES.keys(), *OUTWARD_CHOICE_TO_FAMILY)):
        errors.append("user_choice_selectable_must_equal_auto_plus_families_plus_outward")
    if set(USER_CHOICE_SELECTABLE_LABELS) != (USER_CHOICE_SELECTABLE - {"auto"}):
        # 少一个标签 = 那一档在 UI 上**根本不出现**,而后端照样接受它:
        # 用户选不到、接口却没拒,是最难从截图上看出来的一种坏法。
        errors.append("selectable_labels_must_cover_every_selectable_choice")
    if set(LEGACY_USER_CHOICE_TO_GENERATION_STYLE) != set(LEGACY_USER_CHOICE_TO_FAMILY):
        errors.append("legacy_user_choice_generation_map_mismatch")
    for legacy_choice, style_code in LEGACY_USER_CHOICE_TO_GENERATION_STYLE.items():
        family_code = LEGACY_USER_CHOICE_TO_FAMILY[legacy_choice]
        if LEGACY_STYLE_TO_FAMILY.get(style_code) != family_code:
            errors.append(f"legacy_user_choice_family_mismatch:{legacy_choice}:{style_code}")
    for style, replacement in SAFE_REPLACEMENT_STYLE.items():
        if style not in DISABLED_NEW_GENERATION_STYLES:
            errors.append(f"replacement_source_not_disabled:{style}")
        if replacement in DISABLED_NEW_GENERATION_STYLES:
            errors.append(f"replacement_is_disabled:{style}")
        if replacement not in LEGACY_STYLE_TO_FAMILY:
            errors.append(f"replacement_unknown:{replacement}")
    # [WP12 P0-2] 复活的榜单文体必须真的可生成,且不得再落进退役集合。
    for style in REVIVED_RANKING_STYLES:
        if style in DISABLED_NEW_GENERATION_STYLES:
            errors.append(f"revived_style_still_disabled:{style}")
        if style in SAFE_REPLACEMENT_STYLE:
            errors.append(f"revived_style_still_rerouted:{style}")
        if style not in LEGACY_STYLE_TO_FAMILY:
            errors.append(f"revived_style_unmapped:{style}")
    return errors
