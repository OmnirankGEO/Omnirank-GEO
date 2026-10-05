"""
services/keyword_delivery_decision.py — 报价关键词「三轴交付决策」唯一合同

工单: WO_QUOTE_KEYWORD_GEO_COMMERCIAL_DOUBLE_INVERSION_2026-08-09 · T3 / T4 / §3.2 / §3.3

【为什么有这一层】
在此之前,一个 `geo_recommend` 布尔值同时背了三件互不相干的事:
商业意图、客户业务范围、地域适配。于是深圳龙岗一个商业项目客户身上出现了**双向反转**:

  · `商场推荐`(无地域的全国泛词)只因带"推荐"就 `default_selected=True` 进了付费交付;
  · `深圳龙岗商场招商电话`(本地强成交问法)被统一解释成
    "该问法通常不会让 AI 推荐具体品牌、服务商、产品或方案" —— 一句根本不对的话。

本模块把这三件事**拆成三根独立的轴**,每根轴各自有自己的取值与证据,
合成一个可审计的结构化决策:

    commercial_intent : commercial | brand_direct | knowledge | uncertain | seo_fragment
    business_scope    : matched | mismatched | uncertain
    geo_scope         : matched | too_broad | outside_market | uncertain
    default_selected  : bool           ← 只有三轴都明确满足才为 True
    reason_code       : 稳定机器码       ← 前端据此分组,不靠中文串匹配
    reason_text       : 人话             ← 说清楚"为什么" + "该怎么办"
    human_override_allowed : bool       ← 只有四条 H0 硬边界才 False

【边界 · 本模块不做什么】
- **不发明第二套商业意图分类器**(工单 §8 明令)。第一根轴一律委托
  `services.commercial_query_policy.evaluate()` —— 那是全系统唯一文本引擎。
- **不持有第二份地名词典**。地域轴接调用方传进来的 `GeoContext`,
  由 `tools/keyword_expander._hard_filter` 那份**已有的** allowed_geo / exclude_cities
  计算结果驱动。本模块只写"什么组合判什么",不写"深圳算不算外地"。
- 不写库、不计价、不调 LLM(LLM 只能以 advisory 形式**传进来**,见 `ScopeAdvisory`)。

【H1 不是 H0】(工单 §3.1)
三根轴全部是 advisory:默认不选 + 说明原因 + **保留人工放行**。
物理禁选只剩 `commercial_query_policy.hard_block_reason()` 那四条
(法律 / 资金 / 越权 / 数据完整性)。禁止在本模块新增不可放行硬门。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from services.commercial_query_policy import (
    INTENT_BRAND_DIRECT,
    INTENT_COMMERCIAL,
    INTENT_KNOWLEDGE,
    INTENT_SEO_FRAGMENT,
    INTENT_UNCERTAIN,
    PolicyDecision,
    evaluate as evaluate_commercial_intent,
    hard_block_reason,
)

DELIVERY_POLICY_VERSION = "quote-keyword-delivery-decision-v1"

# ── 轴二:业务范围 ──────────────────────────────────────────────────────
SCOPE_MATCHED = "matched"
SCOPE_MISMATCHED = "mismatched"
SCOPE_UNCERTAIN = "uncertain"

# ── 轴三:地域范围 ──────────────────────────────────────────────────────
GEO_MATCHED = "matched"
GEO_TOO_BROAD = "too_broad"
GEO_OUTSIDE_MARKET = "outside_market"
GEO_UNCERTAIN = "uncertain"

# ── 稳定原因码(前端分组的唯一依据 · 禁止改串,改了要同步改前端与锁)──
REASON_OK = "DELIVERY_OK"
REASON_GEO_TOO_BROAD = "GEO_SCOPE_TOO_BROAD"
REASON_GEO_OUTSIDE = "GEO_SCOPE_OUTSIDE_MARKET"
REASON_GEO_UNCERTAIN = "GEO_SCOPE_UNCERTAIN"
REASON_SCOPE_MISMATCH = "BUSINESS_SCOPE_MISMATCH"
REASON_SCOPE_UNCERTAIN = "BUSINESS_SCOPE_UNCERTAIN"
REASON_INTENT_UNCERTAIN = "COMMERCIAL_INTENT_UNCERTAIN"
REASON_INTENT_KNOWLEDGE = "COMMERCIAL_INTENT_KNOWLEDGE"
REASON_INTENT_FRAGMENT = "COMMERCIAL_INTENT_SEO_FRAGMENT"
REASON_HARD_BLOCK = "HARD_BLOCK"

# 前端分组码(工单 T5 的六个区)。一个 reason_code 只落一个组。
GROUP_DELIVERED = "delivered"
GROUP_GEO_TOO_BROAD = "geo_too_broad"
GROUP_SCOPE_MISMATCH = "scope_mismatch"
GROUP_NEEDS_CONFIRM = "needs_confirm"
GROUP_KNOWLEDGE = "knowledge"
GROUP_GEO_OUTSIDE = "geo_outside"

_REASON_TO_GROUP = {
    REASON_OK: GROUP_DELIVERED,
    REASON_GEO_TOO_BROAD: GROUP_GEO_TOO_BROAD,
    REASON_GEO_OUTSIDE: GROUP_GEO_OUTSIDE,
    REASON_GEO_UNCERTAIN: GROUP_NEEDS_CONFIRM,
    REASON_SCOPE_MISMATCH: GROUP_SCOPE_MISMATCH,
    REASON_SCOPE_UNCERTAIN: GROUP_NEEDS_CONFIRM,
    REASON_INTENT_UNCERTAIN: GROUP_NEEDS_CONFIRM,
    REASON_INTENT_KNOWLEDGE: GROUP_KNOWLEDGE,
    REASON_INTENT_FRAGMENT: GROUP_KNOWLEDGE,
    REASON_HARD_BLOCK: GROUP_KNOWLEDGE,
}

GROUP_LABELS = {
    GROUP_DELIVERED: "已进入交付",
    GROUP_GEO_TOO_BROAD: "地域范围过宽",
    GROUP_SCOPE_MISMATCH: "与当前业务不匹配",
    GROUP_NEEDS_CONFIRM: "需要确认客户是否会这样问",
    GROUP_KNOWLEDGE: "知识/教程类,不建议计入交付",
    GROUP_GEO_OUTSIDE: "地域超出服务范围",
}


def group_for_reason(reason_code: str) -> str:
    """原因码 → 前端分组码。未知码一律落"需要确认"(不静默丢弃)。"""
    return _REASON_TO_GROUP.get(str(reason_code or ""), GROUP_NEEDS_CONFIRM)


# ============================================================================
# 轴二:业务范围
# ============================================================================
# 客户"卖什么"从 industry + business_scope + profile_data + scope_lock 里取,
# 切成业务 token;关键词里出现任一 token = 明确匹配。
#
# 反过来判"明确不匹配"要更谨慎:只有当关键词里出现一个**服务动作**
# (设计/施工/装修/咨询…)而客户业务里根本没有这个动作时,才判 mismatched。
# `商业综合体设计公司` 就是这么被拦下的 —— 它对一个做**商场经营/招商**的客户来说,
# 卖的是"设计服务",不是客户的生意;老逻辑只看见"公司"二字就放行。
_SERVICE_ACTIONS = (
    "设计", "施工", "装修", "装饰", "规划", "咨询", "策划", "培训", "教育",
    "代运营", "运营", "维修", "安装", "检测", "评估", "监理", "测绘",
    "翻译", "拍摄", "摄影", "印刷", "物流", "搬家", "保洁", "安保",
    "开发", "建站", "推广", "代理记账", "审计", "法律",
)

_SPLIT_TOKENS_RE = re.compile(r"[,，、;；/\|\n\r\t　\s]+")
# 业务 token 最短 2 字:1 字 token("店""场")会把几乎所有词都判成 matched。
_MIN_BUSINESS_TOKEN = 2
_MAX_BUSINESS_TOKEN = 12

# 业务描述里的**经营方式后缀** —— 剥掉它才拿到真正的经营对象。
#   "家具建材经营" → "家具建材" · "商铺租赁" → "商铺" · "品牌招商" → "品牌"
# 不剥的话,`家具建材经营` 整词不是 `深圳龙岗买家具建材去哪里好` 的子串,
# 真词会被判成"业务范围拿不准" —— 这是实现缺陷,不是判据问题(2026-08-10 实跑抓到)。
_BUSINESS_SUFFIXES = (
    "经营", "运营", "管理", "服务", "销售", "租赁", "招商", "入驻", "加盟",
    "批发", "零售", "代理", "业务", "项目", "中心", "公司", "有限", "集团",
)

# 太泛的 token 不作为"属于客户业务"的证据 —— 它们几乎能命中任何词。
_GENERIC_BUSINESS_STOPWORDS = frozenset({
    "服务", "业务", "项目", "公司", "企业", "客户", "用户", "市场", "行业",
    "产品", "方案", "平台", "系统", "中心", "专业", "优质", "高端", "本地",
    "全国", "线上", "线下", "我们", "提供", "各类", "各种", "相关",
})


def _business_object_tokens(token: str) -> list[str]:
    """一个业务描述片段 → 可用于匹配的经营对象 token 们。

    `家具建材经营` → ["家具建材经营", "家具建材", "家具", "建材"]
    四字对象二分是为了让 `建材市场有哪些` 也能命中 —— 中文双字名词是最小语义单位,
    再细切就会产生"具建"这类无意义碎片,所以**只对长度恰为 4 的对象二分**。
    """
    out: list[str] = [token]
    stem = token
    for suffix in _BUSINESS_SUFFIXES:
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 2:
            stem = stem[: -len(suffix)]
            break
    if stem != token and len(stem) >= 2:
        out.append(stem)
    if len(stem) == 4:
        out.append(stem[:2])
        out.append(stem[2:])
    return out


@dataclass(frozen=True)
class BusinessProfile:
    """客户"卖什么"的归一视图(纯数据 · 无 LLM 无 DB)。"""

    tokens: tuple[str, ...] = ()
    service_actions: tuple[str, ...] = ()   # 客户自己就在做的服务动作
    raw_sources: tuple[str, ...] = ()
    declared: bool = False                  # 见 `is_decisive`

    @property
    def is_empty(self) -> bool:
        return not self.tokens

    @property
    def is_decisive(self) -> bool:
        """业务轴**有没有否决力**。

        🔴 只有当代理真的填了「业务范围」(`business_scope` 或 profile 里的
        core_business / service_scope / 产品服务清单)时,"这个词没对上"才是一条
        有意义的结论。只有一个 `industry` 行业标签时,它是个粗分类,
        **不足以宣判某个词不属于客户业务**。

        为什么这条必须存在(2026-08-10 既有锁 test_keyword_expansion_quantity_gate
        实跑抓到):不加这条,资料薄的客户会被判成"每个词都业务待确认"→
        **一个词都不交付**,报价页整页空。那不是业务决策,是拿"没有依据"当负面结论,
        同时违反元指令 13(永远不中断)。

        ⚠️ 这不是工单 §8 说的"为了凑 50 个词降低标准":业务范围填了的客户,
        判据一分不松(§5.3 反向对照仍然要求删掉家具建材后不再 matched)。
        成对判据见 tests/quotegeo_2026_08_10/test_business_axis_decisiveness.py。
        """
        return self.declared and bool(self.tokens)


def _iter_text_fragments(value) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_text_fragments(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_text_fragments(item)
        return
    yield str(value)


def build_business_profile(
    *,
    industry: str = "",
    business_scope: str = "",
    profile_data: Optional[dict] = None,
    scope_lock: Optional[dict] = None,
    core_keywords: Optional[list] = None,
) -> BusinessProfile:
    """把客户资料切成业务 token 集合。

    只读现有字段(工单 T3:复用 `industry + business_scope + profile_data + scope_lock`,
    不新增第二套分类器、不新增入参要求)。任何字段缺失都不抛错 —— 缺得多就
    `is_empty`,后续一律判 `uncertain`,绝不因此把词判成"不匹配"而误杀。
    """
    sources: list[str] = []
    declared = False
    if str(industry or "").strip():
        sources.append(str(industry).strip())
    if str(business_scope or "").strip():
        sources.append(str(business_scope).strip())
        declared = True

    profile = profile_data if isinstance(profile_data, dict) else {}
    # 这几个字段 = 代理**明确声明**的经营范围 → 业务轴据此才获得否决力
    for key in ("core_business", "business_scope", "service_scope",
                "main_products", "products", "services"):
        for frag in _iter_text_fragments(profile.get(key)):
            if str(frag or "").strip():
                sources.append(str(frag).strip())
                declared = True
    # 这几个是**辅助语料**:进 token 池提高召回,但**不**赋予业务轴否决力
    for key in ("target_customers", "industry", "industry_brief",
                "selling_points", "unique_value", "use_scenarios"):
        for frag in _iter_text_fragments(profile.get(key)):
            if str(frag or "").strip():
                sources.append(str(frag).strip())

    lock = scope_lock if isinstance(scope_lock, dict) else {}
    # 🔴 只取 `buyer_persona`(常写明客户做哪一行),**刻意不取 `real_query_seeds`** ——
    #    那是"问法种子"不是"经营范围",拿问句去当业务证据会让几乎所有词都判成 matched,
    #    §5.3 的反向对照(删掉家具建材业务后必须不再 matched)就会失去判别力。
    for frag in _iter_text_fragments(lock.get("buyer_persona")):
        if str(frag or "").strip():
            sources.append(str(frag).strip())

    for frag in _iter_text_fragments(core_keywords):
        if str(frag or "").strip():
            sources.append(str(frag).strip())

    tokens: list[str] = []
    for source in sources:
        for piece in _SPLIT_TOKENS_RE.split(source):
            piece = piece.strip("·-—。.!！?？:：()（）【】[]")
            if not (_MIN_BUSINESS_TOKEN <= len(piece) <= _MAX_BUSINESS_TOKEN):
                continue
            for token in _business_object_tokens(piece):
                if (
                    _MIN_BUSINESS_TOKEN <= len(token) <= _MAX_BUSINESS_TOKEN
                    and token not in _GENERIC_BUSINESS_STOPWORDS
                    and token not in tokens
                ):
                    tokens.append(token)

    blob = " ".join(sources)
    actions = tuple(a for a in _SERVICE_ACTIONS if a in blob)

    return BusinessProfile(
        tokens=tuple(tokens),
        service_actions=actions,
        raw_sources=tuple(sources),
        declared=declared,
    )


@dataclass(frozen=True)
class ScopeAdvisory:
    """LLM 对"这个词属不属于该客户业务"的**结构化** advisory(工单 T3)。

    - 只允许 `matched / mismatched / uncertain` 三值,不接受自由文本裁决;
    - 模型不可用 / 没给这个词 → 调用方直接不传,主流程照跑(绝不阻断);
    - advisory **不能推翻确定性的 matched**:规则层已经在客户业务里找到证据时,
      模型说"不匹配"不采信(防模型幻觉把真词杀掉)。它只能把 `uncertain` 收紧。
    """

    verdicts: dict = field(default_factory=dict)   # {keyword: matched|mismatched|uncertain}
    source: str = "llm"

    def verdict_for(self, keyword: str) -> Optional[str]:
        value = self.verdicts.get(str(keyword or "").strip())
        if value in (SCOPE_MATCHED, SCOPE_MISMATCHED, SCOPE_UNCERTAIN):
            return value
        return None


def classify_business_scope(
    keyword: str,
    profile: Optional[BusinessProfile],
    advisory: Optional[ScopeAdvisory] = None,
) -> tuple[str, str]:
    """→ (verdict, evidence)。资料不足一律 `uncertain`,绝不猜成 `mismatched`。"""
    kw = str(keyword or "").strip()
    if not kw:
        return SCOPE_UNCERTAIN, "空词"
    if profile is None or not profile.is_decisive:
        return SCOPE_UNCERTAIN, "客户没填业务范围,这一项判不了(补上业务范围后本页会更准)"

    # ① 先判"这个问题求的是哪种服务"。关键词摆明了要买一项服务(设计/施工/咨询…),
    #    而客户根本不做这项服务 → 明确不匹配。
    #
    # 🔴 这一条**必须排在 token 命中之前**(2026-08-10 实跑抓到):
    #    客户 industry="商业地产运营" 会切出泛 token「商业」,于是
    #    `商业综合体设计公司` 靠两个字重合被判成 matched —— 正是工单 T3 点名的
    #    "不能仅因含公司二字自动入选"的同型错误。求什么服务是决定性的,
    #    名词碰巧重合不是。
    foreign = [a for a in _SERVICE_ACTIONS if a in kw and a not in profile.service_actions]
    if foreign:
        return (
            SCOPE_MISMATCHED,
            f"该问法求的是「{foreign[0]}」服务,不在客户已确认的业务范围内",
        )

    # ② 取**最长**命中项作为证据:同时命中「办公家具」和「家具」时,说前者更有说服力。
    hit = max((t for t in profile.tokens if t and t in kw), key=len, default="")
    if hit:
        return SCOPE_MATCHED, f"命中客户业务「{hit}」"

    verdict = advisory.verdict_for(kw) if advisory else None
    if verdict == SCOPE_MISMATCHED:
        return SCOPE_MISMATCHED, "AI 复核判定与客户业务不匹配(可人工放行)"
    if verdict == SCOPE_MATCHED:
        return SCOPE_MATCHED, "AI 复核判定属于客户业务"

    return SCOPE_UNCERTAIN, "未在客户业务范围里找到对应项,需人工确认"


# ============================================================================
# 轴三:地域范围
# ============================================================================

@dataclass(frozen=True)
class GeoContext:
    """地域判定所需的上下文。

    🔴 **本模块不自带地名词典**。`has_allowed_geo` / `has_outside_geo` 由调用方注入,
    生产路径注入的就是 `KeywordExpander._hard_filter` 里已经算好的
    allowed_geo / exclude_cities + `_contains_excluded_city` 智能子串匹配
    (它能避免"海口"误命中"上海口碑")。一处词典,一处口径。

    不注入时回落到朴素子串匹配 —— 只给单元测试用,生产不走这条。
    """

    market_level: str = "city"
    allowed_tokens: tuple[str, ...] = ()
    outside_tokens: tuple[str, ...] = ()
    has_allowed_geo: Optional[Callable[[str], bool]] = None
    has_outside_geo: Optional[Callable[[str], bool]] = None

    def _allowed(self, kw: str) -> bool:
        if self.has_allowed_geo is not None:
            return bool(self.has_allowed_geo(kw))
        return any(t and t in kw for t in self.allowed_tokens)

    def _outside(self, kw: str) -> bool:
        if self.has_outside_geo is not None:
            return bool(self.has_outside_geo(kw))
        return any(t and t in kw for t in self.outside_tokens)


def classify_geo_scope(keyword: str, geo: Optional[GeoContext]) -> tuple[str, str]:
    """→ (verdict, evidence)。

    工单 T4 的四条:
      · 带外地地域              → `outside_market`
      · 带目标城市/区县且不超范围 → `matched`
      · `national` 客户的无地域词 → `matched`(全国客户本来就该收全国泛词)
      · 本地客户(district/city/regional)的无地域全国泛词 → `too_broad`
    🔴 `geo_ratio_hint` 在这里**一次都没被读到** —— 它只是出词提示,
       不是资格判据也不是验收配额(工单 T4 首条)。反向锁见
       `tests/quotegeo_2026_08_10/test_geo_ratio_hint_is_not_a_gate.py`。
    """
    kw = str(keyword or "").strip()
    if not kw:
        return GEO_UNCERTAIN, "空词"
    if geo is None:
        return GEO_UNCERTAIN, "无范围锁定裁决,地域相容性无法判定"

    if geo._outside(kw):
        return GEO_OUTSIDE_MARKET, "关键词里的地域不在客户服务市场内"
    if geo._allowed(kw):
        return GEO_MATCHED, "关键词地域与客户服务市场相容"

    level = str(geo.market_level or "city").strip().lower()
    if level == "national":
        return GEO_MATCHED, "全国型客户,无地域泛词属于目标市场"

    if not geo.allowed_tokens and geo.has_allowed_geo is None:
        return GEO_UNCERTAIN, "客户服务市场未知,地域相容性无法判定"

    return (
        GEO_TOO_BROAD,
        "该词没有地域限定,面向全国;客户只服务本地市场,默认不计入交付",
    )


# ============================================================================
# 合成:三轴 → 一个可审计决策
# ============================================================================

@dataclass(frozen=True)
class KeywordDeliveryDecision:
    keyword: str
    policy_version: str
    delivery_policy_version: str
    commercial_intent: str
    business_scope: str
    geo_scope: str
    default_selected: bool
    reason_code: str
    reason_text: str
    reason_group: str
    human_override_allowed: bool
    needs_clarification: bool = False
    hard_block: bool = False
    hard_block_reason: str = ""
    commercial_reason_codes: tuple[str, ...] = ()
    business_scope_evidence: str = ""
    geo_scope_evidence: str = ""

    def as_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "policy_version": self.policy_version,
            "delivery_policy_version": self.delivery_policy_version,
            "commercial_intent": self.commercial_intent,
            "business_scope": self.business_scope,
            "geo_scope": self.geo_scope,
            "default_selected": self.default_selected,
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "reason_group": self.reason_group,
            "human_override_allowed": self.human_override_allowed,
            "needs_clarification": self.needs_clarification,
            "hard_block": self.hard_block,
            "hard_block_reason": self.hard_block_reason,
            "commercial_reason_codes": list(self.commercial_reason_codes),
            "business_scope_evidence": self.business_scope_evidence,
            "geo_scope_evidence": self.geo_scope_evidence,
        }


_INTENT_REASON = {
    INTENT_KNOWLEDGE: (
        REASON_INTENT_KNOWLEDGE,
        "这是知识/教程类问法,AI 回答时通常只讲概念,不会点名商家,默认不计入交付。",
    ),
    INTENT_SEO_FRAGMENT: (
        REASON_INTENT_FRAGMENT,
        "这是搜索词片段而不是真人问法,默认不计入交付。",
    ),
    INTENT_UNCERTAIN: (
        REASON_INTENT_UNCERTAIN,
        "看不出这是客户会拿去问 AI 的成交问法,先放这里等你确认。",
    ),
}


def decide(
    keyword: str,
    *,
    profile: Optional[BusinessProfile] = None,
    geo: Optional[GeoContext] = None,
    brand_name: Optional[str] = None,
    intent_hint: Optional[str] = None,
    advisory: Optional[ScopeAdvisory] = None,
    commercial: Optional[PolicyDecision] = None,
) -> KeywordDeliveryDecision:
    """一个关键词 → 一个三轴结构化决策(工单 §3.2 / §3.3)。

    判定顺序严格按 §3.3:商业意图 → 业务匹配 → 地域匹配 → 默认选择。
    三根轴**都会算完并全部落进结果**(即使第一根就否了)——
    前端要能同时说清"商业上成立、但地域过宽",而不是只报第一个否决项。
    """
    kw = str(keyword or "").strip()

    decision_commercial = commercial or evaluate_commercial_intent(
        kw, intent_hint=intent_hint, brand_name=brand_name,
    )
    intent = decision_commercial.intent_type

    scope_verdict, scope_evidence = classify_business_scope(kw, profile, advisory)
    geo_verdict, geo_evidence = classify_geo_scope(kw, geo)

    hard_code = hard_block_reason(kw)
    override_allowed = not hard_code

    def _build(reason_code: str, reason_text: str, selected: bool) -> KeywordDeliveryDecision:
        return KeywordDeliveryDecision(
            keyword=kw,
            policy_version=decision_commercial.policy_version,
            delivery_policy_version=DELIVERY_POLICY_VERSION,
            commercial_intent=intent,
            business_scope=scope_verdict,
            geo_scope=geo_verdict,
            default_selected=selected,
            reason_code=reason_code,
            reason_text=reason_text,
            reason_group=group_for_reason(reason_code),
            human_override_allowed=override_allowed,
            needs_clarification=decision_commercial.needs_clarification,
            hard_block=bool(hard_code),
            hard_block_reason=hard_code or "",
            commercial_reason_codes=tuple(decision_commercial.reason_codes),
            business_scope_evidence=scope_evidence,
            geo_scope_evidence=geo_evidence,
        )

    if hard_code:
        return _build(REASON_HARD_BLOCK, "触发硬边界,不能进入交付,也不能人工放行。", False)

    # ① 商业意图
    if intent not in (INTENT_COMMERCIAL, INTENT_BRAND_DIRECT):
        code, text = _INTENT_REASON.get(intent, _INTENT_REASON[INTENT_UNCERTAIN])
        return _build(code, text, False)

    # ② 业务匹配
    if scope_verdict == SCOPE_MISMATCHED:
        return _build(
            REASON_SCOPE_MISMATCH,
            f"这是真实的商业问法,但{scope_evidence},默认不计入交付。",
            False,
        )

    # ③ 地域匹配
    if geo_verdict == GEO_OUTSIDE_MARKET:
        return _build(
            REASON_GEO_OUTSIDE,
            "商业意图成立,但这个词的地域超出了客户当前的服务市场。",
            False,
        )
    if geo_verdict == GEO_TOO_BROAD:
        return _build(
            REASON_GEO_TOO_BROAD,
            "商业意图成立,但客户只服务本地市场,这个词地域过宽,默认不计入交付。",
            False,
        )
    if geo_verdict == GEO_UNCERTAIN:
        return _build(
            REASON_GEO_UNCERTAIN,
            "商业意图成立,但还没确认客户的服务市场,先放这里等你确认。",
            False,
        )

    # ②' 业务范围拿不准。分两种情况,处置**必须**不同:
    #   · 代理填了业务范围、这个词没对上 → 放待确认区(不静默入选,也不当知识题);
    #   · 代理压根没填业务范围        → 判不了,不能拿"没有依据"当负面结论,
    #     否则资料薄的客户会一个词都交付不出来(元指令 13:永远不中断)。
    if scope_verdict != SCOPE_MATCHED and profile is not None and profile.is_decisive:
        return _build(
            REASON_SCOPE_UNCERTAIN,
            "商业意图与地域都成立,但还不能确认属于客户业务范围,先放这里等你确认。",
            False,
        )

    # ④ 三轴都明确满足
    return _build(REASON_OK, "本地强意图问法,已进入交付。", True)
