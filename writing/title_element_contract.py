"""Versioned title-element contract —— 标题要素默认值的唯一 SSOT。

## 这个模块存在的理由

生产尖 `0a0da20d` 上,"标题要不要带年份"这一条口径散在**四个地方、三种意见**:

| 层 | 位置 | 当时的口径 |
|---|---|---|
| 选题 LLM prompt | `keyword_topic_generator._build_title_generator_prompt` | 全族默认**不写**年份 |
| 确定性兜底模板 | `keyword_topic_generator._fallback_style_title_map` | 六族**全部写**年份(且 7 条以年份开头) |
| evidence-first 硬兜底 | `article_style_contract.evidence_first_title_for_family` | 指南/趋势写、榜单**不写** |
| 正文深档规格 | `templates/canonical_family_templates.build_deep_ranking_structure_spec` | 榜单深档默认**不写** |

四处各写一份 = 改一处必漏三处。本模块把"哪个文体默认带哪些标题要素"收成**一份可推导的
数据 + 两条规则**,上面四层全部改成读它,谁也不再自己拍。

## 数据来源(禁凭记忆写)

`article_structure_feature_rates.csv`(定稿附件,`GEO_ARTICLE_UPGRADE_RESEARCH_2026-08-07.md`
第一部分 §1.3)。被引语料按语义分类器分成 9 个研究文体,逐文体统计五个标题要素的出现率:

    article_type, feature(title_has_*), n, present_n, present_pct, spec_level

分级门槛与附件 `threshold_contract` 列**逐字同源**:>=70% 默认必备 / 40%-69.99% 推荐 /
<40% 可选。**三级都只是提示词默认,永不作保存或发布硬拦**(附件 `allocation_boundary` 列)。

⚠️ 口径差异留痕:研究正文 §3.1 写"榜单标题 55.83% 含年份",附件 CSV 给的是 59.06%。
两者分母不同(正文那句是"已预核实"子集)。**工单明令以附件为准**,故本模块取 59.06%。
"含年份中约 87% 是当年"这一条只在研究正文 §3.1 有,CSV 无对应列,故只作 lineage 记录,
不进任何判据。

## 两条推导规则(不手填结论,只填数据)

* **R-A 主卡**:每族声明其对应的研究文体(按 N 降序),第一条是**主卡**。
  要素分级 = 主卡该要素的 `present_pct` 落进上面的门槛。
* **R-B 年份默认三态**:
  - 主卡年份率 >= 40% → ``on``(默认带当年年份);
  - 否则若存在副卡年份率 >= 40% 且该副卡 N >= 主卡 N 的 25% → ``conditional``
    (族内分歧,由主题形态决定;少数侧占比不足 25% 的不算分歧);
  - 否则 → ``off``(维持"默认不写")。

代入当前数据的结果(由 `validate_title_element_contract()` 机械复核,不是我写在这里的结论):
多品牌比较 ``on`` / 案例数据 ROI ``conditional`` / 其余四族 ``off``。

## 两条边界(踩过的坑,别再踩)

1. **年份一律动态取**(`current_year()`),禁写死 2026 —— 仓里 `writing/config.py:271`
   与 `ranking_prompt_v9.py:134` 都写死过,后者靠 `style_registry._inject_dynamic_dates`
   事后字符串替换才没跨年翻车。
2. **确定性兜底不做主题形态判断**:`conditional` 在没有 LLM 的路径上一律取保守侧
   (等同 ``off``)。"年度变化主题例外"只在 LLM 侧可用 —— 兜底模板拿不到主题形态,
   在那里假装能判断就是自欺。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from .article_style_contract import STYLE_FAMILIES


TITLE_ELEMENT_CONTRACT_VERSION: Final = "geo-title-element-v1.0"

#: 附件 `article_structure_feature_rates.csv` 的 `threshold_contract` 列,逐字同源。
SPEC_LEVEL_REQUIRED_MIN: Final = 70.0
SPEC_LEVEL_RECOMMENDED_MIN: Final = 40.0
#: R-B 的"少数侧占比"门槛:副卡 N 不到主卡的这个比例,不算族内分歧。
MINORITY_CARD_SHARE_MIN: Final = 0.25

LEVEL_REQUIRED: Final = "required"
LEVEL_RECOMMENDED: Final = "recommended"
LEVEL_OPTIONAL: Final = "optional"

YEAR_DEFAULT_ON: Final = "on"
YEAR_DEFAULT_CONDITIONAL: Final = "conditional"
YEAR_DEFAULT_OFF: Final = "off"

#: 五个标题要素,与附件 `feature` 列的 `title_has_*` 一一对应。
TITLE_ELEMENTS: Final[tuple[str, ...]] = (
    "year", "region", "industry_marker", "ranking_marker", "number",
)
ELEMENT_LABELS: Final[dict[str, str]] = {
    "year": "当年年份",
    "region": "地区",
    "industry_marker": "行业对象",
    "ranking_marker": "榜单/推荐词",
    "number": "数字",
}
LEVEL_LABELS: Final[dict[str, str]] = {
    LEVEL_REQUIRED: "默认必备",
    LEVEL_RECOMMENDED: "推荐",
    LEVEL_OPTIONAL: "可选",
}


@dataclass(frozen=True)
class CorpusTitleCard:
    """一张研究文体的标题要素卡(= 附件里该 article_type 的 5 行 title_has_*)。"""

    corpus_type: str
    label: str
    n: int
    #: 要素 → present_pct(单位 %,附件原值,不四舍五入不改写)
    rates: dict[str, float]


# ---------------------------------------------------------------------------
# 附件原值。逐行抄自 `article_structure_feature_rates.csv` 的 title_has_* 五行。
# 改这里的任何一个数字都必须同步改附件,并说明重跑了哪条复现命令(§6.3)。
# ---------------------------------------------------------------------------
CORPUS_TITLE_CARDS: Final[dict[str, CorpusTitleCard]] = {
    "ranking": CorpusTitleCard("ranking", "榜单/推荐", 5753, {
        "year": 59.06, "region": 22.91, "industry_marker": 71.11,
        "ranking_marker": 67.84, "number": 67.96,
    }),
    "other": CorpusTitleCard("other", "其他", 2179, {
        "year": 19.92, "region": 13.13, "industry_marker": 53.74,
        "ranking_marker": 34.28, "number": 32.95,
    }),
    "tutorial": CorpusTitleCard("tutorial", "教程/选型指南", 684, {
        "year": 15.20, "region": 10.09, "industry_marker": 39.47,
        "ranking_marker": 14.18, "number": 27.49,
    }),
    "case_study": CorpusTitleCard("case_study", "案例文", 558, {
        "year": 9.68, "region": 24.19, "industry_marker": 61.29,
        "ranking_marker": 17.20, "number": 23.12,
    }),
    "comparison": CorpusTitleCard("comparison", "比较文", 544, {
        "year": 33.64, "region": 9.19, "industry_marker": 58.09,
        "ranking_marker": 18.38, "number": 50.37,
    }),
    "long_form": CorpusTitleCard("long_form", "深度长文", 394, {
        "year": 19.04, "region": 12.18, "industry_marker": 54.31,
        "ranking_marker": 24.37, "number": 30.20,
    }),
    "data_report": CorpusTitleCard("data_report", "数据报告", 303, {
        "year": 40.92, "region": 26.07, "industry_marker": 38.28,
        "ranking_marker": 20.46, "number": 62.71,
    }),
    "news": CorpusTitleCard("news", "新闻/动态", 210, {
        "year": 8.57, "region": 22.86, "industry_marker": 33.81,
        "ranking_marker": 4.29, "number": 27.14,
    }),
    "definition": CorpusTitleCard("definition", "定义/解释", 72, {
        "year": 0.00, "region": 13.89, "industry_marker": 41.67,
        "ranking_marker": 4.17, "number": 9.72,
    }),
}

#: 只在 lineage 里记,不进判据 —— CSV 没有这一列(研究正文 §3.1)。
RANKING_YEAR_IS_CURRENT_YEAR_SHARE_NOTE: Final = (
    "研究正文 §3.1:榜单标题含年份的样本中约 87% 是**当年**年份。"
    "附件 CSV 无对应列,故本条只解释「为什么默认取当年而不是任意年份」,不作判据。"
)


@dataclass(frozen=True)
class FamilyTitleProfile:
    """六族之一 → 它对应的研究文体卡(按 N 降序,第一张是主卡)。"""

    family_code: str
    corpus_types: tuple[str, ...]
    #: 为什么是这几张卡。语料分类器是按内容形态分的,与我们的六族(按购买问题
    #: 意图分)不是同一套坐标,所以这里必须逐族写清映射理由,不许默认对齐。
    rationale: str


# ---------------------------------------------------------------------------
# 映射。**这是本模块唯一一处人工判断**,其余全是数据与两条规则的推导结果。
#
# 🔴 诚实边界:语料的 9 个研究文体是按**内容形态**分的(榜单/教程/案例/长文…),
# 我们的六族是按**购买问题意图**分的。两套坐标不同构,所以:
#   · 形态与意图能直接对上的写 direct(榜单↔多品牌比较、教程↔实施指南…);
#   · 语料里根本没有对应形态的(证据型问答、企业事实),只能用残余桶 `other` 作**代理**,
#     并在此明说是代理。代理卡的推导结果当前全部落在 `off`/`可选` 侧 —— 也就是说
#     代理映射没有把任何一族**抬高**过默认,只是维持现状。谁以后改这张表,
#     `validate_title_element_contract()` 会把"代理卡抬高了默认"直接判红。
# ---------------------------------------------------------------------------
FAMILY_TITLE_PROFILES: Final[dict[str, FamilyTitleProfile]] = {
    "multi_brand_comparison": FamilyTitleProfile(
        "multi_brand_comparison", ("ranking", "comparison"),
        "direct:本族标题就是榜单/推荐与比较两种形态,语料里正好有这两类;"
        "榜单 N=5753 是比较文 N=544 的十倍,故榜单作主卡。",
    ),
    "case_data_roi": FamilyTitleProfile(
        "case_data_roi", ("case_study", "data_report"),
        "direct:本族同时承接案例文与数据报告两种形态(生成层的 legacy style 也正是 "
        "`data_report` 与 `price_roi`);两张卡年份率一低一高,是真实的族内分歧。",
    ),
    "implementation_guide": FamilyTitleProfile(
        "implementation_guide", ("tutorial",),
        "direct:教程/选型指南与本族一一对应。",
    ),
    "trend_policy_risk": FamilyTitleProfile(
        "trend_policy_risk", ("news",),
        "direct:语料里最接近「趋势/政策/风险」的时效性形态是新闻/动态。"
        "注意 N=210 偏小,故本族只用它定默认,不用它做任何强结论。",
    ),
    "evidence_qa": FamilyTitleProfile(
        "evidence_qa", ("other", "definition"),
        "proxy:语料分类器没有「问答」这一类,残余桶 `other` 与定义/解释是最近的代理。"
        "两张卡年份率同侧(19.92% / 0.00%),推导结果 off = 维持现行口径,未抬高默认。",
    ),
    "company_facts": FamilyTitleProfile(
        "company_facts", ("other",),
        "proxy:语料分类器没有「企业事实」这一类,同样落残余桶。"
        "推导结果 off = 维持现行口径,未抬高默认。",
    ),
}


def current_year() -> int:
    """当年年份。**唯一取法**,禁在任何调用方写死年份字面量。"""
    return datetime.now().year


def _card(corpus_type: str) -> CorpusTitleCard:
    card = CORPUS_TITLE_CARDS.get(corpus_type)
    if card is None:
        raise KeyError(f"unknown_corpus_type:{corpus_type}")
    return card


def _profile(family_code: str | None) -> FamilyTitleProfile | None:
    return FAMILY_TITLE_PROFILES.get(str(family_code or "").strip())


def level_for_rate(rate: float) -> str:
    """门槛 → 分级。与附件 `threshold_contract` 列同源,禁在别处再写一份。"""
    value = float(rate or 0.0)
    if value >= SPEC_LEVEL_REQUIRED_MIN:
        return LEVEL_REQUIRED
    if value >= SPEC_LEVEL_RECOMMENDED_MIN:
        return LEVEL_RECOMMENDED
    return LEVEL_OPTIONAL


def primary_card(family_code: str | None) -> CorpusTitleCard | None:
    """R-A:主卡 = 该族语料卡里 N 最大的那张(声明顺序已按 N 降序)。"""
    profile = _profile(family_code)
    if profile is None:
        return None
    return _card(profile.corpus_types[0])


def element_rate(family_code: str | None, element: str) -> float | None:
    card = primary_card(family_code)
    if card is None or element not in TITLE_ELEMENTS:
        return None
    return card.rates.get(element)


def element_level(family_code: str | None, element: str) -> str | None:
    """R-A:要素分级取主卡值。未知族/未知要素返回 None(**不猜**)。"""
    rate = element_rate(family_code, element)
    return None if rate is None else level_for_rate(rate)


def year_default_for_family(family_code: str | None) -> str:
    """R-B:年份默认三态。未知族按最保守的 ``off``。"""
    profile = _profile(family_code)
    if profile is None:
        return YEAR_DEFAULT_OFF
    primary = _card(profile.corpus_types[0])
    if level_for_rate(primary.rates["year"]) != LEVEL_OPTIONAL:
        return YEAR_DEFAULT_ON
    for corpus_type in profile.corpus_types[1:]:
        secondary = _card(corpus_type)
        if level_for_rate(secondary.rates["year"]) == LEVEL_OPTIONAL:
            continue
        if secondary.n >= primary.n * MINORITY_CARD_SHARE_MIN:
            return YEAR_DEFAULT_CONDITIONAL
    return YEAR_DEFAULT_OFF


def deterministic_year_default(family_code: str | None) -> bool:
    """确定性兜底(无 LLM、拿不到主题形态)该不该写年份。

    ``conditional`` 在这里**一律取保守侧**。见模块 docstring 边界 2:兜底路径判断不了
    主题是"数据报告型"还是"案例型",在那里假装能判断就是自欺。
    """
    return year_default_for_family(family_code) == YEAR_DEFAULT_ON


def family_code_for_label(label: str | None) -> str | None:
    """六族中文名 → family_code。兜底模板表用中文名作键,靠这个接回合同。"""
    text = str(label or "").strip()
    for code, family in STYLE_FAMILIES.items():
        if family.name == text:
            return code
    return None


def deterministic_year_token(family_label_or_code: str | None, year: int | None = None) -> str:
    """确定性兜底模板里的年份片段:该族默认带年份就给 ``"{year}年"``,否则给 ``""``。

    🔴 这是**真接线**不只是判据:把合同里某族从 ``on`` 改成 ``off``,该族兜底模板的
    年份会当场消失。别在调用方另写一份"哪族带年份"的清单。
    """
    code = family_code_for_label(family_label_or_code) or str(family_label_or_code or "").strip()
    if not deterministic_year_default(code):
        return ""
    return f"{int(year or current_year())}年"


def conditional_year_families() -> tuple[str, ...]:
    return tuple(
        code for code in FAMILY_TITLE_PROFILES
        if year_default_for_family(code) == YEAR_DEFAULT_CONDITIONAL
    )


def _family_name(family_code: str) -> str:
    family = STYLE_FAMILIES.get(family_code)
    return family.name if family else family_code


def _conditional_clause(family_code: str) -> str:
    """条件默认族:说清"哪一侧带、哪一侧不带",依据来自两张卡的真实差值。"""
    profile = _profile(family_code)
    assert profile is not None
    on_cards, off_cards = [], []
    for corpus_type in profile.corpus_types:
        card = _card(corpus_type)
        (on_cards if level_for_rate(card.rates["year"]) != LEVEL_OPTIONAL else off_cards).append(card)
    on_text = "、".join(f"{c.label}({c.rates['year']:.2f}%)" for c in on_cards)
    off_text = "、".join(f"{c.label}({c.rates['year']:.2f}%)" for c in off_cards)
    return f"主题偏{on_text}时带年份;偏{off_text}时不带"


def build_title_year_rule_prompt(*, year: int | None = None) -> str:
    """选题 prompt 里的年份规则块。**唯一渲染点**,别处不许再写一句年份口径。"""
    resolved_year = int(year or current_year())
    lines = [
        f"【标题年份默认 {TITLE_ELEMENT_CONTRACT_VERSION}】"
        "年份是**分文体条件默认**,不是全站开关——被引语料里各文体的标题含年份率"
        f"从 {CORPUS_TITLE_CARDS['definition'].rates['year']:.2f}% 到 "
        f"{CORPUS_TITLE_CARDS['ranking'].rates['year']:.2f}% 差了一个数量级,"
        "一刀切写或一刀切不写都是错的。",
    ]
    for family_code in STYLE_FAMILIES:
        state = year_default_for_family(family_code)
        card = primary_card(family_code)
        name = _family_name(family_code)
        rate_text = f"{card.label} {card.rates['year']:.2f}%" if card else "无对应语料"
        if state == YEAR_DEFAULT_ON:
            lines.append(
                f"- **{name}**:默认带**当年年份「{resolved_year}」**(语料 {rate_text})。"
                f"{RANKING_YEAR_IS_CURRENT_YEAR_SHARE_NOTE.split('。')[0]}。"
            )
        elif state == YEAR_DEFAULT_CONDITIONAL:
            lines.append(
                f"- **{name}**:条件默认——{_conditional_clause(family_code)}"
                f"(带年份时写当年「{resolved_year}」)。"
            )
        else:
            lines.append(
                f"- **{name}**:默认**不写**年份(语料 {rate_text});"
                "仅当主题本身就是「年度变化」(政策调整/趋势盘点/年度更新)时才写。"
            )
    lines.append(
        "- **全族通用两条**:① 年份一律不作标题开头(与标题公式库硬规则④同一条,不是两套);"
        f"② 写了年份就只能写当年「{resolved_year}」,且正文时点必须与之一致,"
        "不得当装饰词乱贴。"
    )
    return "\n".join(lines)


def build_title_year_position_rule() -> str:
    """**与文体无关**的那一条:年份不作标题开头。

    [P3 2026-08-08] 正文侧的 `common_rules.COMMON_GUARDRAILS` 与
    `style_registry._inject_dynamic_dates` 原先各抄了一份这条规则(30.7%/42.1% 这组
    飞轮数字在仓里一度有三份手抄)。现在两处都渲染这个函数,数字从
    `title_formula_library.ADOPTION_TITLE_FEATURE_CONTRAST` 单点取。
    """
    from .title_formula_library import ADOPTION_TITLE_FEATURE_CONTRAST

    contrast = ADOPTION_TITLE_FEATURE_CONTRAST["year"]
    return (
        "标题里的年份**不作开头**(带对照组的采纳样本里,带年份标题在采纳组占 "
        f"{contrast['adopted']:.1%}、对照组占 {contrast['control']:.1%},是反向信号);"
        "**要不要带年份由文体合同在选题阶段已经定好**,正文不重新决定 —— "
        "正文只需保证时点与标题一致,不得把年份当与内容无关的装饰词。"
    )


def build_title_element_defaults_prompt() -> str:
    """P2-2 可解释默认:逐族给出五个标题要素的分级 + 出现率,让模型有据可依。

    🔴 这张表是**默认值**不是勾选清单:要素按购买问题**自然组合**,凑不齐就不凑
    (工单 P2-2「不四件强塞」)。这句话必须随表下发,否则模型会把它当四件套硬塞。
    """
    header = (
        f"【标题要素可解释默认 {TITLE_ELEMENT_CONTRACT_VERSION}】"
        "下表是被引语料里各文体标题**实际长什么样**的出现率,用来解释"
        "「为什么默认带这个要素」。"
        f"分级门槛:>={SPEC_LEVEL_REQUIRED_MIN:g}% 默认必备 / "
        f">={SPEC_LEVEL_RECOMMENDED_MIN:g}% 推荐 / 其余可选。"
    )
    lines = [header, "", "| 文体 | " + " | ".join(ELEMENT_LABELS[e] for e in TITLE_ELEMENTS) + " |",
             "|---|" + "---|" * len(TITLE_ELEMENTS)]
    for family_code in STYLE_FAMILIES:
        card = primary_card(family_code)
        if card is None:
            continue
        cells = []
        for element in TITLE_ELEMENTS:
            # 走公开查询函数,不直接掏卡:这样"表里显示的分级"与"外部查到的分级"
            # 必然同源 —— 两条路径各算各的正是本包要根治的病。
            rate = element_rate(family_code, element)
            level = element_level(family_code, element)
            cells.append(f"{LEVEL_LABELS[level]} {rate:.2f}%")
        lines.append(f"| {_family_name(family_code)} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        "⚠️ 上表「当年年份」列给的是**主卡**出现率;条件默认族(族内两张卡分居门槛两侧)"
        "的年份口径以上一节【标题年份默认】为准,那里把两张卡都摊开了。"
    )
    lines.append(
        "🔴 **这是默认值不是勾选清单**:要素必须由该关键词的**购买问题**自然带出来 ——"
        "关键词里没有地区就不许硬加地区,没有可核验数量就不许编数字。"
        "「默认必备」的含义是「这个要素在场时标题更像真实被引标题」,"
        "不是「四件套一个都不能少」。凑不齐就少写,**永不作保存或发布硬拦**。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 实验预登记(工单补章 R1:终局由 A/B 数据裁,不由本工单拍死)
#
# ⚠️ 诚实边界:`services/article_experiment_registry` 的臂位绑定是钉在 **style_version**
# 上的(`assign_article` 校验 `article.style_version == arm 的 version_id`)。
# 本包改的是"文体默认值",不是一个 style_version —— 也就是说**现有随机化机制
# 接不住本次改动**,除非另建两个 style version 承载两侧默认。
# 因此本模块只提供**预登记声明**(假设/主指标/臂位定义/样本门槛),真正建实验行由
# `scripts/preregister_title_year_experiment_2026_08_08.py` 在两个 style version
# 就位后执行。没就位时那个脚本会**响亮失败**,不会静默造一条跑不起来的实验。
# ---------------------------------------------------------------------------
TITLE_YEAR_EXPERIMENT_DIMENSION: Final = "title_elements"

TITLE_YEAR_EXPERIMENT_PREREGISTRATION: Final[dict[str, object]] = {
    "contract_version": TITLE_ELEMENT_CONTRACT_VERSION,
    "style_family": "multi_brand_comparison",
    "single_change_dimension": TITLE_YEAR_EXPERIMENT_DIMENSION,
    "hypothesis": (
        "榜单族标题默认带当年年份,对「发布后同品牌精确 URL 被引率」不劣于默认不带。"
        "两侧证据互补而非冲突:飞轮实证(采纳 30.7% vs 曝光 42.1%)说的是**全体**标题,"
        "被引语料说的是**榜单形态**(59.06%,其中约 87% 为当年);深度长文两侧一致偏低"
        "(19.04%)。故只在榜单族开默认,并由本实验裁定终局。"
    ),
    "min_arm_articles": 30,
    "minimum_weeks": 4,
    "review_checkpoints_days": (7, 14, 30),
    "arms": {
        "control": "标题年份沿用改动前口径(全族默认不写)",
        "candidate": f"标题年份按 {TITLE_ELEMENT_CONTRACT_VERSION} 分文体条件默认",
    },
    "effect_claim_boundary": (
        "coded/deployed 阶段**不许**用「看起来更好」申报效果;"
        "唯一认账的是上线后 7/14/30 天的精确 URL 被引回查,且两臂各 >=30 篇。"
    ),
}


def title_year_experiment_preregistration() -> dict[str, object]:
    """预登记声明的唯一读取点(注册脚本与测试都读它,不各抄一份)。"""
    return dict(TITLE_YEAR_EXPERIMENT_PREREGISTRATION)


def validate_title_element_contract() -> list[str]:
    """静态 SSOT 自审。被启动诊断与测试共用。"""
    errors: list[str] = []
    if set(FAMILY_TITLE_PROFILES) != set(STYLE_FAMILIES):
        errors.append("family_profiles_must_cover_six_families")
    for family_code, profile in FAMILY_TITLE_PROFILES.items():
        if not profile.corpus_types:
            errors.append(f"family_without_corpus_card:{family_code}")
            continue
        cards = []
        for corpus_type in profile.corpus_types:
            if corpus_type not in CORPUS_TITLE_CARDS:
                errors.append(f"unknown_corpus_type:{family_code}:{corpus_type}")
                continue
            cards.append(CORPUS_TITLE_CARDS[corpus_type])
        # R-A 前提:声明顺序必须按 N 降序,否则"第一张是主卡"这句话就不成立。
        if cards != sorted(cards, key=lambda c: -c.n):
            errors.append(f"corpus_cards_not_sorted_by_n_desc:{family_code}")
        # 代理映射不得抬高默认(见映射表上方的红字)。
        if profile.rationale.startswith("proxy") and \
                year_default_for_family(family_code) != YEAR_DEFAULT_OFF:
            errors.append(f"proxy_mapping_must_not_raise_year_default:{family_code}")
    for corpus_type, card in CORPUS_TITLE_CARDS.items():
        if set(card.rates) != set(TITLE_ELEMENTS):
            errors.append(f"corpus_card_element_set_mismatch:{corpus_type}")
        for element, rate in card.rates.items():
            if not (0.0 <= float(rate) <= 100.0):
                errors.append(f"rate_out_of_range:{corpus_type}:{element}")
        if card.n <= 0:
            errors.append(f"corpus_card_n_must_be_positive:{corpus_type}")
    prereg = TITLE_YEAR_EXPERIMENT_PREREGISTRATION
    if prereg["style_family"] not in STYLE_FAMILIES:
        errors.append("preregistration_style_family_unknown")
    if year_default_for_family(str(prereg["style_family"])) != YEAR_DEFAULT_ON:
        # 预登记的实验族必须真的是本次被改默认的那一族,否则实验测的不是这次改动。
        errors.append("preregistration_family_is_not_the_changed_family")
    return errors
