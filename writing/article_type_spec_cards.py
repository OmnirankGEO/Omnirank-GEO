"""Versioned article-type spec cards —— 9 文体正文规格的唯一 SSOT。

## 这个模块管什么、不管什么

**管**:结构件分级(必备/推荐/可选)、证据密度下限、实体数目标区间、每实体字数参考、
段落形态参考,以及引擎特化附卡。全部来自定稿附件,**只作写作默认,永不作配额或验收线**。

**不管**:**字数目标**。字数是 `writing/article_length_contract.py` 的领地(它按篇幅合同
给 target/min/max,还带避谷带与 0.85 覆盖锁)。规格卡里存了语料的 body_chars 分位数,
但**只作 lineage 与"实体数够不够"的推导输入,不渲染进提示词** —— 渲染两套字数就是
第二套数字逻辑,本仓在别处已经被这个咬过。规格卡的提示词块里出现字数目标 =
`validate_article_spec_cards()` 判红。

## 数据来源(禁凭记忆写)

三份定稿附件(`GEO_ARTICLE_UPGRADE_RESEARCH_2026-08-07.md` §6.2):

| 附件 | 本模块取哪几列 |
|---|---|
| `article_type_spec_cards.csv` | 字数/段落/实体数/每实体字数/证据密度 各分位数 |
| `article_structure_feature_rates.csv` | **10 个正文结构件**的 present_pct 与分级 |
| `engine_structure_significance.csv` | `addendum=yes` 的格子(= N>=30 且显著) |

分级门槛与 `title_element_contract` **同一份**(>=70 必备 / >=40 推荐 / <40 可选),
直接 import 复用,不在这里再写一份。

🔴 **标题类要素(`title_has_*`)不在本模块**:它们归 `title_element_contract`(P2)。
结构件表里那 5 行标题要素在这里被**显式排除**,并有锁钉死 —— 两个模块各管一头,
谁把标题要素抄进来,`validate_article_spec_cards()` 判红。

## 文体映射复用 P2

家族 → 语料文体的映射(以及"主卡按 N 降序"这条规则)已经在
`title_element_contract.FAMILY_TITLE_PROFILES` 里定义并配了锁。本模块**直接复用**,
不再写第二份 —— 同一个"我们这一族的文章长得像语料里哪一类"问题,不该有两个答案。

## 版本门禁

照 `style_registry.get_prompt_for_style` 的范式:`writing_config.json` 里的覆盖
**只有在声明的合同版本 == 代码里的 `ARTICLE_SPEC_CARD_VERSION` 时才生效**,
否则一律回落代码内建值。生产当前**没有配置任何覆盖**,所以线上走的永远是内建值;
覆盖分支由构造用例覆盖(放行标准⑦:生产形态 + 构造形态双用例)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from .title_element_contract import (
    FAMILY_TITLE_PROFILES,
    LEVEL_LABELS,
    LEVEL_OPTIONAL,
    LEVEL_RECOMMENDED,
    LEVEL_REQUIRED,
    SPEC_LEVEL_RECOMMENDED_MIN,
    SPEC_LEVEL_REQUIRED_MIN,
    level_for_rate,
)


ARTICLE_SPEC_CARD_VERSION: Final = "geo-article-spec-card-v1.0"

#: 正文结构件(10 个)。**标题类 `title_has_*` 五个要素刻意不在此列** —— 归 P2 合同。
BODY_STRUCTURE_FEATURES: Final[tuple[str, ...]] = (
    "has_headings", "has_list", "has_comparison_table", "has_image_reference",
    "scenario_recommendation", "has_case", "has_qualification",
    "has_external_source", "has_price", "first_person_experience",
)
#: 明确划给 P2 的标题要素前缀 —— 出现在本模块任何数据里都判红。
TITLE_FEATURE_PREFIX: Final = "title_has_"
#: 「小标题」结构件的键。格式要求那一行钉在它身上(见 `_heading_format_line`)。
HEADING_FEATURE: Final = "has_headings"

# ---------------------------------------------------------------------------
# 🔴 **进数据、但不进提示词**的结构件。
#
# `has_image_reference` 在语料里是引擎附卡中**最大的正向差**(榜单 deepseek +52.62pp),
# 但配图在本仓是**另一条有闸的链路**:`services/article_image_selector.IMAGE_PLACEHOLDER_RULE`
# 按自己的条件注入,拿不到授权图时留 `[NEED_IMAGE ...]` 占位,再由
# `services/image_placeholder.render_for_publish` 在发布期剥除。
# 生产上这条链已经出过事故:占位符原文被当正文发到渠道 + `platform_safety_profiles`
# 见 `[NEED_IMAGE` 判 hard → 保底占位反而成了发布阻断
# (见 `tests/test_need_image_never_reaches_publish.py` 的 docstring)。
#
# 规格卡去劝模型"多写图片位"= 绕过那条闸从写作侧制造占位符,踩的正是同一个坑。
# 所以:**数据保留**(不藏),**提示词里一个字不提**,由 `validate` 钉死。
# 要不要多配图是配图链自己的决定,不是文体规格卡的。
# ---------------------------------------------------------------------------
PROMPT_EXCLUDED_FEATURES: Final[tuple[str, ...]] = ("has_image_reference",)

FEATURE_LABELS: Final[dict[str, str]] = {
    "has_headings": "小标题",
    "has_list": "列表块",
    "has_comparison_table": "对比表",
    "has_image_reference": "图片位",
    "scenario_recommendation": "分场景推荐",
    "has_case": "案例",
    "has_qualification": "资质",
    "has_external_source": "外部来源",
    "has_price": "价格信息",
    "first_person_experience": "第一人称体验",
}


@dataclass(frozen=True)
class ArticleTypeSpecCard:
    """一个研究文体的正文规格卡(= 附件里该 article_type 的那一行 + 结构件分级)。"""

    corpus_type: str
    label: str
    n: int
    #: 字数分位数 —— **只作 lineage 与推导输入,不渲染进提示词**(字数归篇幅合同)。
    body_chars_p25: int
    body_chars_median: int
    body_chars_p75: int
    paragraphs_p25: float
    paragraphs_median: float
    paragraphs_p75: float
    avg_paragraph_chars_median: float
    entity_count_p25: float
    entity_count_median: float
    entity_count_p75: float
    chars_per_entity_median: int
    data_evidence_p25: float
    data_evidence_median: float
    case_count_p25: float
    case_count_median: float
    qualification_count_p25: float
    qualification_count_median: float
    external_source_count_p25: float
    external_source_count_median: float
    #: 结构件 → present_pct(附件原值)
    structure_rates: dict[str, float]


# ---------------------------------------------------------------------------
# 附件原值。逐行抄自 `article_type_spec_cards.csv` + `article_structure_feature_rates.csv`
# 的 10 个正文结构件。改任何一个数字都必须同步改附件并说明重跑了哪条复现命令(§6.3)。
# ---------------------------------------------------------------------------
SPEC_CARDS: Final[dict[str, ArticleTypeSpecCard]] = {
    "ranking": ArticleTypeSpecCard(
        "ranking", "榜单/推荐", 5753,
        2244, 4149, 12701, 32.0, 59.0, 122.0, 79.7,
        5.0, 5.0, 10.0, 752,
        5.0, 16.0, 0.0, 4.0, 1.0, 3.0, 0.0, 0.0,
        {"has_headings": 97.62, "has_list": 60.30, "has_comparison_table": 6.80,
         "has_image_reference": 14.48, "scenario_recommendation": 61.20,
         "has_case": 72.00, "has_qualification": 78.46,
         "has_external_source": 43.99, "has_price": 38.17,
         "first_person_experience": 43.39},
    ),
    "other": ArticleTypeSpecCard(
        "other", "其他", 2179,
        289, 597, 2816, 7.0, 15.0, 40.5, 47.3,
        0.0, 0.0, 1.0, 2324,
        0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        {"has_headings": 71.82, "has_list": 33.69, "has_comparison_table": 1.33,
         "has_image_reference": 7.80, "scenario_recommendation": 1.61,
         "has_case": 26.76, "has_qualification": 24.83,
         "has_external_source": 41.44, "has_price": 12.80,
         "first_person_experience": 18.45},
    ),
    "tutorial": ArticleTypeSpecCard(
        "tutorial", "教程/选型指南", 684,
        1092, 2216, 4512, 20.0, 42.0, 77.0, 53.1,
        0.0, 1.0, 2.0, 1219,
        0.0, 4.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0,
        {"has_headings": 94.74, "has_list": 51.17, "has_comparison_table": 7.16,
         "has_image_reference": 4.68, "scenario_recommendation": 32.60,
         "has_case": 53.51, "has_qualification": 55.12,
         "has_external_source": 20.47, "has_price": 32.16,
         "first_person_experience": 37.43},
    ),
    "case_study": ArticleTypeSpecCard(
        "case_study", "案例文", 558,
        1549, 2700, 12782, 18.2, 39.0, 113.0, 86.0,
        1.0, 1.0, 1.0, 2575,
        4.0, 13.0, 2.0, 6.0, 1.0, 3.0, 0.0, 0.0,
        {"has_headings": 95.88, "has_list": 50.90, "has_comparison_table": 1.43,
         "has_image_reference": 17.56, "scenario_recommendation": 24.37,
         "has_case": 86.56, "has_qualification": 77.06,
         "has_external_source": 40.86, "has_price": 39.25,
         "first_person_experience": 44.09},
    ),
    "comparison": ArticleTypeSpecCard(
        "comparison", "比较文", 544,
        1621, 2701, 4761, 27.0, 42.0, 71.2, 62.5,
        3.0, 4.0, 6.0, 659,
        3.0, 9.0, 0.0, 1.0, 0.0, 2.0, 0.0, 0.0,
        {"has_headings": 97.79, "has_list": 52.21, "has_comparison_table": 14.89,
         "has_image_reference": 4.23, "scenario_recommendation": 77.39,
         "has_case": 61.40, "has_qualification": 68.93,
         "has_external_source": 23.71, "has_price": 39.52,
         "first_person_experience": 33.64},
    ),
    "long_form": ArticleTypeSpecCard(
        "long_form", "深度长文", 394,
        2103, 5404, 21047, 30.0, 61.5, 191.0, 99.4,
        1.0, 1.0, 1.0, 4907,
        3.2, 11.0, 1.0, 4.0, 1.0, 2.0, 0.0, 0.0,
        {"has_headings": 97.72, "has_list": 63.71, "has_comparison_table": 3.81,
         "has_image_reference": 18.27, "scenario_recommendation": 35.79,
         "has_case": 77.41, "has_qualification": 75.38,
         "has_external_source": 40.61, "has_price": 37.06,
         "first_person_experience": 49.24},
    ),
    "data_report": ArticleTypeSpecCard(
        "data_report", "数据报告", 303,
        1216, 2691, 5076, 21.0, 42.0, 76.0, 65.7,
        0.0, 1.5, 10.0, 350,
        4.0, 20.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0,
        {"has_headings": 98.02, "has_list": 43.89, "has_comparison_table": 5.28,
         "has_image_reference": 6.27, "scenario_recommendation": 9.90,
         "has_case": 54.79, "has_qualification": 44.55,
         "has_external_source": 35.97, "has_price": 32.01,
         "first_person_experience": 26.73},
    ),
    "news": ArticleTypeSpecCard(
        "news", "新闻/动态", 210,
        1225, 2853, 28643, 18.0, 41.0, 52.0, 95.2,
        0.0, 0.0, 1.0, 1714,
        4.0, 12.5, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0,
        {"has_headings": 97.14, "has_list": 49.05, "has_comparison_table": 0.95,
         "has_image_reference": 31.43, "scenario_recommendation": 0.00,
         "has_case": 51.43, "has_qualification": 37.14,
         "has_external_source": 52.38, "has_price": 47.14,
         "first_person_experience": 34.29},
    ),
    "definition": ArticleTypeSpecCard(
        "definition", "定义/解释", 72,
        802, 1800, 4895, 26.8, 43.5, 74.2, 37.3,
        0.0, 0.0, 1.0, 1282,
        4.0, 8.5, 0.0, 1.5, 0.0, 0.5, 0.0, 0.0,
        {"has_headings": 98.61, "has_list": 33.33, "has_comparison_table": 5.56,
         "has_image_reference": 6.94, "scenario_recommendation": 1.39,
         "has_case": 62.50, "has_qualification": 50.00,
         "has_external_source": 20.83, "has_price": 23.61,
         "first_person_experience": 20.83},
    ),
}


# ---------------------------------------------------------------------------
# 引擎特化附卡(P3-7)。
#
# 取 `engine_structure_significance.csv` 里 **`addendum` 列 == "yes"** 的格子 ——
# 那一列就是研究方按「N>=30 且显著」判出来的结论,我们**直接采用而不是自己再判一次**
# (自己按 p_value 重判 = 第二套判定逻辑)。
#
# 🔴 两条边界:
# 1. **只收正文结构件**。CSV 里 `comparison/qwen` 有两行 `title_has_*` 也标了 yes
#    (标题含年份 -26.97pp、标题含数字 -33.70pp),但标题归 P2 合同、而且**一篇只有
#    一个标题**,没法按引擎分版 —— 所以它们被本模块显式排除,由 `validate` 钉死。
# 2. **正文仍只写一版**(工单 P3-7 原话)。附卡是**同一篇正文里的侧重提示**,
#    不是"给每个引擎各写一篇"。渲染时说清楚这一点。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EngineAddendum:
    corpus_type: str
    engine: str
    n: int
    feature: str
    engine_pct: float
    global_pct: float
    delta_pp: float


ENGINE_ADDENDA: Final[tuple[EngineAddendum, ...]] = (
    EngineAddendum("case_study", "deepseek", 33, "has_image_reference", 66.67, 17.56, 49.10),
    EngineAddendum("case_study", "deepseek", 33, "has_external_source", 72.73, 40.86, 31.87),
    EngineAddendum("case_study", "qwen", 34, "has_list", 85.29, 50.90, 34.40),
    EngineAddendum("case_study", "qwen", 34, "has_image_reference", 67.65, 17.56, 50.08),
    EngineAddendum("case_study", "qwen", 34, "has_external_source", 79.41, 40.86, 38.55),
    EngineAddendum("comparison", "qwen", 30, "has_image_reference", 30.00, 4.23, 25.77),
    EngineAddendum("other", "deepseek", 170, "has_headings", 94.12, 71.82, 22.30),
    EngineAddendum("other", "deepseek", 170, "has_external_source", 69.41, 41.44, 27.97),
    EngineAddendum("other", "qwen", 146, "has_headings", 93.15, 71.82, 21.33),
    EngineAddendum("other", "qwen", 146, "has_external_source", 73.97, 41.44, 32.53),
    EngineAddendum("ranking", "deepseek", 389, "has_list", 82.26, 60.30, 21.96),
    EngineAddendum("ranking", "deepseek", 389, "has_image_reference", 67.10, 14.48, 52.62),
    EngineAddendum("ranking", "deepseek", 389, "has_external_source", 76.61, 43.99, 32.61),
    EngineAddendum("ranking", "deepseek", 389, "has_price", 56.30, 38.17, 18.13),
    EngineAddendum("ranking", "deepseek", 389, "first_person_experience", 60.15, 43.39, 16.77),
    EngineAddendum("ranking", "qwen", 401, "has_list", 78.55, 60.30, 18.25),
    EngineAddendum("ranking", "qwen", 401, "has_image_reference", 54.86, 14.48, 40.38),
    EngineAddendum("ranking", "qwen", 401, "has_external_source", 65.84, 43.99, 21.84),
    EngineAddendum("tutorial", "deepseek", 34, "has_image_reference", 50.00, 4.68, 45.32),
    EngineAddendum("tutorial", "deepseek", 34, "has_external_source", 58.82, 20.47, 38.36),
    EngineAddendum("tutorial", "deepseek", 34, "first_person_experience", 61.76, 37.43, 24.34),
    EngineAddendum("tutorial", "qwen", 37, "has_image_reference", 29.73, 4.68, 25.05),
    EngineAddendum("tutorial", "qwen", 37, "first_person_experience", 59.46, 37.43, 22.03),
)

#: CSV 里被本模块**有意丢弃**的 addendum 行(标题类)。留痕以证明"丢弃是决定不是遗漏"。
DISCARDED_TITLE_ADDENDA: Final[tuple[tuple[str, str, str, float], ...]] = (
    ("comparison", "qwen", "title_has_year", -26.97),
    ("comparison", "qwen", "title_has_number", -33.70),
)

#: 榜单族的实体数目标区间(补章 R2:**不是家数常量,是核验增援目标**)。
#: 取语料 ranking 卡的 entity_count P25-P75。
RANKING_ENTITY_TARGET_LOW: Final = int(SPEC_CARDS["ranking"].entity_count_p25)    # 5
RANKING_ENTITY_TARGET_HIGH: Final = int(SPEC_CARDS["ranking"].entity_count_p75)   # 10


def corpus_type_for_family(family_code: str | None) -> str | None:
    """复用 P2 的映射与"主卡按 N 降序"规则,不在这里写第二份。"""
    profile = FAMILY_TITLE_PROFILES.get(str(family_code or "").strip())
    if profile is None or not profile.corpus_types:
        return None
    return profile.corpus_types[0]


def spec_card_for_family(family_code: str | None) -> ArticleTypeSpecCard | None:
    corpus_type = corpus_type_for_family(family_code)
    return SPEC_CARDS.get(corpus_type or "")


def _config_override(corpus_type: str) -> dict[str, Any] | None:
    """版本门禁(照 `style_registry.get_prompt_for_style` 范式)。

    只有 `writing_config.json` 里声明的合同版本**逐字等于**当前代码版本时,
    该文体的覆盖才生效;版本对不上、或配置读不到,一律回落内建值。
    """
    try:
        from writing.style_registry import _get_writing_config

        config = _get_writing_config()
        overrides = config.get("article_spec_card_overrides") or {}
        versions = config.get("article_spec_card_contract_versions") or {}
        if (
            corpus_type in overrides
            and overrides[corpus_type]
            and versions.get(corpus_type) == ARTICLE_SPEC_CARD_VERSION
        ):
            return dict(overrides[corpus_type])
    except Exception:
        return None
    return None


def structure_rate(corpus_type: str, feature: str) -> float | None:
    card = SPEC_CARDS.get(corpus_type)
    if card is None or feature not in BODY_STRUCTURE_FEATURES:
        return None
    override = _config_override(corpus_type) or {}
    rates = override.get("structure_rates") or {}
    if feature in rates:
        try:
            return float(rates[feature])
        except (TypeError, ValueError):
            return card.structure_rates.get(feature)
    return card.structure_rates.get(feature)


def structure_level(corpus_type: str, feature: str) -> str | None:
    rate = structure_rate(corpus_type, feature)
    return None if rate is None else level_for_rate(rate)


def features_by_level(corpus_type: str) -> dict[str, list[str]]:
    """结构件按三级分桶(P3-4)。**不进提示词的结构件在这里就被摘掉**。"""
    buckets: dict[str, list[str]] = {
        LEVEL_REQUIRED: [], LEVEL_RECOMMENDED: [], LEVEL_OPTIONAL: [],
    }
    for feature in BODY_STRUCTURE_FEATURES:
        if feature in PROMPT_EXCLUDED_FEATURES:
            continue
        level = structure_level(corpus_type, feature)
        if level:
            buckets[level].append(feature)
    return buckets


#: 平台引擎名 → 语料 CSV 里的引擎名。
#
# 🔴 这张表是被自己的锁逼出来的:平台侧 `config.ai_engines.UNIFIED_ENGINES` 用的是
# **供应商名** `dashscope`,而研究附件用的是**模型名** `qwen` —— 直接把
# `UNIFIED_ENGINES` 传进来筛选,会把 qwen 的 8 条附卡**静默丢光**,而且表面上
# "附卡照常渲染"看不出任何异常。名字对不上的过滤器是最难发现的一种空判据。
ENGINE_NAME_ALIASES: Final[dict[str, str]] = {"dashscope": "qwen"}
#: 语料里出现过的引擎名(用于反向核对别名表没写歪)。
CORPUS_ENGINES: Final[frozenset[str]] = frozenset(a.engine for a in ENGINE_ADDENDA)


def normalize_engine_name(name: str) -> str:
    key = str(name or "").strip().lower()
    return ENGINE_NAME_ALIASES.get(key, key)


def addenda_for(corpus_type: str, engines: tuple[str, ...] | list[str] = ()) -> list[EngineAddendum]:
    """该文体的引擎附卡。**不传引擎就不给附卡**(显式 opt-in,避免"顺手全给")。"""
    wanted = {normalize_engine_name(e) for e in engines if str(e or "").strip()}
    if not wanted:
        return []
    rows = [
        a for a in ENGINE_ADDENDA
        if a.corpus_type == corpus_type
        and a.feature not in PROMPT_EXCLUDED_FEATURES
        and a.engine in wanted
    ]
    return sorted(rows, key=lambda a: (-abs(a.delta_pp), a.engine, a.feature))


# ---------------------------------------------------------------------------
# [W1 返工 2026-08-08] 小标题的**格式**要求。
#
# 返工原因(Codex 终审亲核 + Review 采纳):本卡此前只渲染出
# 「默认必备:小标题(97.62%)」—— **通篇没有一行说清「小标题 = 真实 Markdown `##`」**。
# 于是模型有相当概率改用独占一行的 `**粗体**` 冒充小标题。后果不是排版难看,是**功能性失效**:
#   · `article_writer.H2_PATTERN`(`^##\s+`)一条都匹配不到 → H4 硬失败;
#   · `count_effective_answer_blocks` 靠 `HEADING_PATTERN` 切块 → 答案块数直接归零;
#   · 审核自动驾驶(`apply_review_autopilot`)修的是证据类 hard,**修不了格式**。
# 生产实测(20 班本地预演,同客户同批):历史批 0/10 出现,新批 4/10 出现。
#
# 🔴 提示词只是**第一道**。间歇性漂移(4/10 而非 10/10)本来就说明"劝"不彻底 ——
# 所以同一次返工里还有第二道 `writing/markdown_heading_repair.py`(落库前机械转换)。
# 两道缺一道都不算修完:只加提示词 = 还有 4/10 概率漏;只加修复器 = 模型继续产劣稿,
# 后面每一环都在给它擦屁股。
# ---------------------------------------------------------------------------
HEADING_MARKDOWN_RULE: Final = (
    "🔴 **小标题必须是真实 Markdown 的二级标题**:行首写 `## `(两个井号 + 一个空格)"
    "再接标题文字,单独成行。**禁止用独占一行的 `**粗体**` 当小标题** —— "
    "抽取器只认 `## `,粗体行会被当成普通正文,整篇的小标题数按 0 计。"
    "正确:`## 怎么核验一家供应商的资质`;错误:`**怎么核验一家供应商的资质**`。"
    "(本卡自己用的 `**加粗**` 是提示词排版,不是正文示例。)"
)


def _heading_format_line(buckets: dict[str, list[str]]) -> str:
    """只有当「小标题」这个结构件真的进了本卡渲染时才出这条格式要求。

    钉在 `HEADING_FEATURE` 是否落进分桶上,而不是无条件输出 —— 若哪天该结构件
    被移出提示词(例如进了 `PROMPT_EXCLUDED_FEATURES`),这条格式要求应当跟着
    消失,而不是孤零零留一句去要求一个卡里根本没提的东西。
    """
    for names in buckets.values():
        if HEADING_FEATURE in names:
            return HEADING_MARKDOWN_RULE
    return ""


def _evidence_floor_line(card: ArticleTypeSpecCard) -> str:
    """P3-5 证据密度下限。**P25 是增援参考,不是发布硬拦**(附件 evidence_density_contract)。"""
    parts = []
    if card.data_evidence_p25 > 0:
        parts.append(f"可核验数据点 {card.data_evidence_p25:g} 处")
    if card.case_count_p25 > 0:
        parts.append(f"案例 {card.case_count_p25:g} 个")
    if card.qualification_count_p25 > 0:
        parts.append(f"资质/认证 {card.qualification_count_p25:g} 项")
    if card.external_source_count_p25 > 0:
        parts.append(f"外部来源 {card.external_source_count_p25:g} 条")
    if not parts:
        return (
            "本文体语料的证据密度 P25 全为 0 —— 说明被引样本里它本来就不靠堆证据取胜,"
            "**不要为了凑密度硬塞**;有真证据就写,没有就把话说清楚。"
        )
    return (
        "证据密度参考下限(被引语料 P25):" + "、".join(parts) +
        "。**这是资料增援的目标,不是发布硬拦** —— 达不到就按 A1 去补真证据,"
        "绝不用编造的数字凑数;补不到就如实收短。"
    )


def build_spec_card_prompt(
    family_code: str | None,
    *,
    engines: tuple[str, ...] | list[str] = (),
    verified_entity_count: int | None = None,
) -> str:
    """渲染该族的正文规格卡块。未知族返回 ""(**不猜**)。

    🔴 这里**不写任何字数目标** —— 字数归 `article_length_contract`。
    """
    card = spec_card_for_family(family_code)
    if card is None:
        return ""
    corpus_type = card.corpus_type
    buckets = features_by_level(corpus_type)
    lines = [
        f"【文体规格卡 {ARTICLE_SPEC_CARD_VERSION} · {card.label}(被引语料 N={card.n})】",
        "以下是**被引语料里这类文章实际长什么样**的统计默认值。"
        f"分级门槛:>={SPEC_LEVEL_REQUIRED_MIN:g}% 默认必备 / "
        f">={SPEC_LEVEL_RECOMMENDED_MIN:g}% 推荐 / 其余可选。"
        "🔴 **三级都只是写作默认,永不作保存或发布硬拦**;"
        "篇幅目标不在本卡内(以篇幅合同为准,本卡不重复给字数)。",
        "",
    ]
    for level in (LEVEL_REQUIRED, LEVEL_RECOMMENDED, LEVEL_OPTIONAL):
        names = buckets[level]
        if not names:
            continue
        rendered = "、".join(
            f"{FEATURE_LABELS[f]}({structure_rate(corpus_type, f):.2f}%)" for f in names
        )
        lines.append(f"- **{LEVEL_LABELS[level]}**:{rendered}")
    # 紧贴分级表下面 —— 上一行刚说完「必备:小标题(97.62%)」,这一行就说清
    # 小标题长什么样。中间隔别的内容会让这条要求和它约束的对象失去关联。
    _heading_rule = _heading_format_line(buckets)
    if _heading_rule:
        lines.append(_heading_rule)
    lines.append("")
    lines.append(_evidence_floor_line(card))
    if card.avg_paragraph_chars_median:
        lines.append(
            f"段落形态参考:被引样本段均约 {card.avg_paragraph_chars_median:g} 字、"
            f"中位 {card.paragraphs_median:g} 段。短段化更容易被整段抽取。"
        )
    if corpus_type == "ranking":
        lines.append(_ranking_entity_line(verified_entity_count))
    addenda = addenda_for(corpus_type, engines)
    if addenda:
        lines.append("")
        lines.append(_addendum_block(addenda))
    return "\n".join(lines)


def _ranking_entity_line(verified_entity_count: int | None) -> str:
    """P3-2(补章 R2):实体数是**核验增援目标**,不是家数硬拦。"""
    base = (
        f"实体数目标区间:被引榜单的实体数 P25-P75 = "
        f"{RANKING_ENTITY_TARGET_LOW}-{RANKING_ENTITY_TARGET_HIGH} 家,"
        f"每个实体约 {SPEC_CARDS['ranking'].chars_per_entity_median} 字。"
        "🔴 **这是核验增援目标,不是家数上下限** —— 成卡名单永远等于已核验白名单,"
        "**绝不为了凑区间虚构候选**。"
    )
    if verified_entity_count is None:
        return base
    count = max(0, int(verified_entity_count))
    if count < RANKING_ENTITY_TARGET_LOW:
        return base + (
            f"\n  ⚠️ 本篇已核验候选只有 {count} 家(低于目标区间下限"
            f"{RANKING_ENTITY_TARGET_LOW})。**不要补虚构候选**;"
            "按已有候选写实,或改写成分场景推荐榜(按适用场景并列,不排名次)。"
        )
    return base + f"\n  本篇已核验候选 {count} 家,落在目标区间内。"


def _addendum_block(addenda: list[EngineAddendum]) -> str:
    """P3-7:引擎特化附卡。**正文仍只写一版**。"""
    lines = [
        "【引擎特化附卡】以下是该文体在**单个引擎**引用池里显著高于全局的结构件"
        "(仅 N>=30 且显著的格子)。🔴 **正文仍然只写一版** —— 附卡是同一篇里的"
        "侧重提示,不是给每个引擎各写一篇;证据不支持就不写,不为附卡编内容。",
    ]
    for row in addenda:
        lines.append(
            f"- {row.engine} · {FEATURE_LABELS[row.feature]}:"
            f"该引擎 {row.engine_pct:.2f}% vs 全局 {row.global_pct:.2f}%"
            f"(+{row.delta_pp:.2f}pp,N={row.n})"
        )
    return "\n".join(lines)


def validate_article_spec_cards() -> list[str]:
    """静态 SSOT 自审。被启动诊断与测试共用。"""
    errors: list[str] = []
    if len(SPEC_CARDS) != 9:
        errors.append("spec_cards_must_cover_nine_corpus_types")
    for corpus_type, card in SPEC_CARDS.items():
        if card.corpus_type != corpus_type:
            errors.append(f"corpus_type_key_mismatch:{corpus_type}")
        if card.n <= 0:
            errors.append(f"card_n_must_be_positive:{corpus_type}")
        if set(card.structure_rates) != set(BODY_STRUCTURE_FEATURES):
            errors.append(f"structure_rate_set_mismatch:{corpus_type}")
        for feature, rate in card.structure_rates.items():
            if feature.startswith(TITLE_FEATURE_PREFIX):
                errors.append(f"title_feature_leaked_into_body_card:{corpus_type}:{feature}")
            if not (0.0 <= float(rate) <= 100.0):
                errors.append(f"structure_rate_out_of_range:{corpus_type}:{feature}")
        if not (card.body_chars_p25 <= card.body_chars_median <= card.body_chars_p75):
            errors.append(f"body_chars_quantiles_not_monotonic:{corpus_type}")
        if not (card.entity_count_p25 <= card.entity_count_median <= card.entity_count_p75):
            errors.append(f"entity_quantiles_not_monotonic:{corpus_type}")
    # 引擎附卡只收正文结构件
    for row in ENGINE_ADDENDA:
        if row.feature.startswith(TITLE_FEATURE_PREFIX):
            errors.append(f"title_feature_in_engine_addendum:{row.corpus_type}:{row.feature}")
        if row.feature not in BODY_STRUCTURE_FEATURES:
            errors.append(f"unknown_addendum_feature:{row.feature}")
        if row.corpus_type not in SPEC_CARDS:
            errors.append(f"unknown_addendum_corpus_type:{row.corpus_type}")
        if row.n < 30:
            errors.append(f"addendum_below_min_n:{row.corpus_type}:{row.engine}")
    # 丢弃留痕必须真的是标题类(否则"丢弃"就成了藏数据)
    for corpus_type, engine, feature, _delta in DISCARDED_TITLE_ADDENDA:
        if not feature.startswith(TITLE_FEATURE_PREFIX):
            errors.append(f"discarded_row_is_not_a_title_feature:{corpus_type}:{feature}")
    # 六族必须都能落到一张卡(映射复用 P2,断这条说明 P2 那张表被改坏了)
    for family_code in FAMILY_TITLE_PROFILES:
        if spec_card_for_family(family_code) is None:
            errors.append(f"family_without_spec_card:{family_code}")
    for feature in PROMPT_EXCLUDED_FEATURES:
        if feature not in BODY_STRUCTURE_FEATURES:
            errors.append(f"excluded_feature_not_in_body_set:{feature}")
    # 别名表必须真的指向语料里存在的引擎名,否则过滤器会静默筛空
    for platform_name, corpus_name in ENGINE_NAME_ALIASES.items():
        if corpus_name not in CORPUS_ENGINES:
            errors.append(f"engine_alias_target_not_in_corpus:{platform_name}:{corpus_name}")
        if platform_name in CORPUS_ENGINES:
            errors.append(f"engine_alias_source_shadows_corpus_name:{platform_name}")
    # 🔴 元判据:规格卡提示词块里不许出现字数目标(那是篇幅合同的领地),
    # 也不许出现"进数据不进提示词"的结构件(那是配图链的领地)。
    for family_code in FAMILY_TITLE_PROFILES:
        block = build_spec_card_prompt(family_code, engines=("deepseek", "qwen", "kimi", "doubao"))
        for banned in ("目标篇幅", "全文不得低于", "target_chars", "字数目标"):
            if banned in block:
                errors.append(f"spec_card_block_must_not_set_length_target:{family_code}:{banned}")
        for feature in PROMPT_EXCLUDED_FEATURES:
            if FEATURE_LABELS[feature] in block:
                errors.append(f"excluded_feature_leaked_into_prompt:{family_code}:{feature}")
        # [W1 返工] 元判据:只要卡里提了「小标题」,就必须同时说清它是 `## `。
        # 这条是本次返工的核心 —— 少了它,卡就退回"要求一个东西却不说它长什么样"。
        if FEATURE_LABELS[HEADING_FEATURE] in block and "`## `" not in block:
            errors.append(f"heading_feature_without_markdown_format_rule:{family_code}")
    return errors
