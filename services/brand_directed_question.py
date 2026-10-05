"""品牌定向题判定 SSOT(WO_BRAND_QUESTION_LEAK_2026-08-05 §2.2)。

**为什么要有这个文件**(2026-08-05 · 报告 551 实证):

诊断 8 问里出现了**两道指向公司的题**,平台只认出一道,另一道带着场景层标签
进了竞争格局分母 → 平台提及率、场景转化层得分、竞品榜三处同时被自己的品牌词污染。

真凶不是"LLM 归错层"(工单原判),而是 ``tools/keyword_generator._enforce_commercial_questions``
的**等槽兜底池第 0 条恒为** ``f"{brand_name}是什么公司？"``:任何一道非品牌槽位的题
被商业意图闸判废时,顶上来的就是这道品牌题,并且**原样继承被替换槽位的层标签**。
生产实证:有 layer_key 的 32 份 v2 报告里 22 份中招(69%),漏网题永远是同一句模板。

所以本模块提供的是**与标签相互独立的文本判据**:题面含品牌名(或已确认别名)即判品牌题。
标签与文本打架时**以文本为准**并留痕 —— 上游标签坏掉时下游仍然守得住。

**判据边界(有意保守,不做模糊匹配)**:
  · 只做"归一后子串命中",不做编辑距离/分词/LLM;
  · token 归一长度 < 3 一律丢弃,且纯行业通用词(电梯/科技/地产…)一律丢弃 ——
    否则「深圳市恒通电梯有限公司」这种正经同行会被当成客户自己剔掉;
  · **不走** ``BrandIdentity.all_trusted_names``:品牌题归类只吃
    canonical_names / trusted_aliases 的**原串**,不继承解析器派生的简称、法律后缀或
    门店变体,避免扩大题面分类边界。

误判方向也是有意选的:多判成品牌题 = 竞争面样本变少(偏保守、不虚高);
少判 = 客户拿自己的品牌词把自己的分顶高,那是要退款的那种错。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from services.brand_identity_resolver import (
    _GENERIC_INDUSTRY_TERMS,
    _GENERIC_SUFFIX_TOKENS,
    normalize_brand_name,
)

# 机器 key 与中文 label 两套标签(旧产物只有中文 label)。
BRAND_DIRECTED_LAYER_KEYS = frozenset({"brand_awareness", "brand"})
BRAND_DIRECTED_LAYER_LABELS = frozenset({"品牌认知层"})

# 归一后的最短 token 长度。2 字 token(「深圳」「电梯」)拿去做子串判定必然误伤同行。
MIN_TOKEN_LENGTH = 3

_BRACKET_RE = re.compile(r"[（(][^）)]*[）)]")

# 长的排前面:一次性剥到位,避免「股份有限公司」被「有限公司」截半。
_LEGAL_SUFFIXES: tuple[str, ...] = (
    "股份有限责任公司",
    "有限责任公司",
    "股份有限公司",
    "集团有限公司",
    "有限公司",
    "股份公司",
    "责任公司",
    "分公司",
    "总公司",
    "集团",
    "公司",
)

_GENERIC_TOKENS = frozenset(_GENERIC_SUFFIX_TOKENS) | frozenset(_GENERIC_INDUSTRY_TERMS)


@dataclass(frozen=True)
class BrandDirectedVerdict:
    """一道题的品牌定向判定结果 + 判定来源(留痕用)。"""

    is_brand_directed: bool
    by_label: bool
    by_text: bool

    @property
    def relabeled_by_text(self) -> bool:
        """文本说是品牌题、标签说不是 —— 上游错标了,这个计数就是错标率。"""
        return self.by_text and not self.by_label


def _strip_legal_suffix(value: str) -> str:
    for suffix in _LEGAL_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[: -len(suffix)]
    return value


def _candidate_forms(raw: str) -> list[str]:
    """一个名字派生出的判据形态:原串 / 去括号段 / 去法人后缀 / 两者都去。"""
    base = (raw or "").strip()
    if not base:
        return []
    no_bracket = _BRACKET_RE.sub("", base).strip()
    forms = [base, no_bracket, _strip_legal_suffix(base), _strip_legal_suffix(no_bracket)]
    return [form for form in forms if form]


def brand_directed_tokens(
    brand_name: str, aliases: Sequence[str] | Iterable[str] = ()
) -> tuple[str, ...]:
    """返回归一后的判据 token(去重、按长度降序,长的先命中便于取证)。"""
    raw_names: list[str] = []
    if isinstance(brand_name, str) and brand_name.strip():
        raw_names.append(brand_name)
    for alias in aliases or ():
        if isinstance(alias, str) and alias.strip():
            raw_names.append(alias)

    tokens: set[str] = set()
    for raw in raw_names:
        for form in _candidate_forms(raw):
            token = normalize_brand_name(form)
            if len(token) < MIN_TOKEN_LENGTH:
                continue
            if token in _GENERIC_TOKENS:
                continue
            tokens.add(token)
    return tuple(sorted(tokens, key=lambda t: (-len(t), t)))


def matched_brand_token(
    question: str, brand_name: str, *, aliases: Sequence[str] | Iterable[str] = ()
) -> str:
    """命中的 token(没命中返回空串)。给留痕/取证用,判定请用 ``is_brand_directed_text``。"""
    normalized_question = normalize_brand_name(question or "")
    if not normalized_question:
        return ""
    for token in brand_directed_tokens(brand_name, aliases):
        if token in normalized_question:
            return token
    return ""


def is_brand_directed_text(
    question: str, brand_name: str, *, aliases: Sequence[str] | Iterable[str] = ()
) -> bool:
    """**与标签无关**的文本判据:题面含品牌名(或已确认别名)即品牌定向题。"""
    return bool(matched_brand_token(question, brand_name, aliases=aliases))


def is_brand_directed_label(layer_key: object = None, layer_label: object = None) -> bool:
    """旧口径:只读上游打的标签(机器 key 优先,兼容旧产物只有中文 label)。"""
    if isinstance(layer_key, str) and layer_key.strip():
        return layer_key.strip().lower() in BRAND_DIRECTED_LAYER_KEYS
    if isinstance(layer_label, str) and layer_label.strip():
        return layer_label.strip() in BRAND_DIRECTED_LAYER_LABELS
    return False


def resolve_brand_directed(
    question: str,
    *,
    layer_key: object = None,
    layer_label: object = None,
    brand_name: str = "",
    aliases: Sequence[str] | Iterable[str] = (),
    verbatim: bool = False,
) -> BrandDirectedVerdict:
    """标签判据 ∪ 文本判据。

    ``verbatim=True``(自定义/逐字模式)**只**关掉标签判据:
    用户自己写的题没经分层,平台不替她重新归层(工单 §2.2 明令保留)。
    但**题面判据照跑** —— 一道以品牌名开头的题必然命中自己,与题是谁写的无关。
    (WO_236-c1b 拆开了原来混在一起的两件事,详见函数体注释。)
    """
    by_text = is_brand_directed_text(question, brand_name, aliases=aliases)
    if verbatim:
        # 🔴 [WO_236-c1b · Review 裁定 2026-09-17] 原来这里是
        #    `return BrandDirectedVerdict(False, False, False)` —— 一见 verbatim 就
        #    连**题面判据**一起关掉。那是**用一条理由去决定两件事**:
        #
        #      ① 「不替客户重新分层她自己写的题」—— 合理。verbatim 的本意就是
        #         她问什么就照问、不替她改归层。**这条保留**(下面不取 by_label)。
        #      ② 「不把它排除出竞争分母」—— **不合理**。一道以品牌名开头的题
        #         必然命中自己,**与题是谁写的无关**。
        #
        #    谁写的决定「归哪一层」;题面本身决定「算不算竞争样本」。
        #
        #    实证代价(Review 生产只读):#700/#726 两份**真客户**报告因此把
        #    12 条全进竞争分母,页面给出「本品牌被提及 8/10 次」和竞品排行,
        #    而三道题全是品牌定向题 —— 真排除了样本该是 0。
        #
        #    ⚠️ 这里**只撤 verbatim 对题面判据的短路**,没有放宽题面判据本身
        #       (`is_brand_directed_text` 一个字没动)—— 同轴放宽会把真竞争题误剔。
        return BrandDirectedVerdict(by_text, False, by_text)
    by_label = is_brand_directed_label(layer_key, layer_label)
    return BrandDirectedVerdict(by_label or by_text, by_label, by_text)


# 与 ``ai_tester._exact_or_strict_match`` 同一条规则里的"短方最少字数"。
# 2-4 字短简称("盛邦"/"法制")做包含判定必然误中,所以两边都卡 6。
STRICT_CONTAINMENT_MIN_LENGTH = 6


def _fallback_exact_or_strict_match(target: str, candidate: str) -> bool:
    """``ai_tester._exact_or_strict_match`` 的**同规则**本地实现(只在它 import 不到时用)。

    🔴 这里**不能**降级成"归一后相等" —— 那正是本单要修的 551 竞品榜漏剔判据:
    「深圳市晨光富士电梯」与「深圳市晨光富士电梯有限公司」归一后不相等,客户就回到
    自家竞品榜了。而 ``ai_tester`` 会拖 ``agentscope`` / ``mcp``,依赖一有版本漂移就
    import 不到(2026-08-05 在 python:3.12 干净容器里实测到过)——
    "无条件剔除"(工单 §2.3)不能建立在"某个重依赖恰好装得上"之上。

    两份实现同规则必然有漂移风险,所以配了 ``test_fallback_matcher_agrees_with_ai_tester``
    在同一语料上逐条对齐:对方改规则 → 那条锁转红,而不是这里静默走偏。
    """
    target_norm = normalize_brand_name(target)
    candidate_norm = normalize_brand_name(candidate)
    if not target_norm or not candidate_norm:
        return False
    if target_norm == candidate_norm:
        return True
    if candidate_norm in target_norm or target_norm in candidate_norm:
        return min(len(candidate_norm), len(target_norm)) >= STRICT_CONTAINMENT_MIN_LENGTH
    return False


def is_client_own_brand(
    candidate: str, brand_name: str, *, aliases: Sequence[str] | Iterable[str] = ()
) -> bool:
    """候选名是不是客户自己(含已确认别名)—— 竞品榜无条件剔除用(工单 §2.3)。

    优先复用 ``ai_tester._exact_or_strict_match`` 这个命中判定 SSOT(双向包含 +
    短方 ≥6 字防误中),不另造一套;它 import 不到时走 **同规则** 的本地实现,
    不降级成更弱的判据。
    """
    name = (candidate or "").strip()
    if not name:
        return False
    targets = [brand_name, *[a for a in (aliases or ()) if isinstance(a, str)]]
    targets = [t for t in targets if isinstance(t, str) and t.strip()]
    if not targets:
        return False

    try:
        from tools.ai_visibility.ai_tester import _exact_or_strict_match
    except Exception:  # ai_tester 拖 agentscope/mcp,版本漂移时会 import 不到
        return any(_fallback_exact_or_strict_match(t, name) for t in targets)

    for target in targets:
        matched, _ = _exact_or_strict_match(target, [name])
        if matched:
            return True
    return False


def load_confirmed_aliases(brand_id: object) -> tuple[str, ...]:
    """读该品牌**已确认**的常用名(canonical + trusted 原串)。

    🔴 故意不用 ``BrandIdentity.all_trusted_names``:品牌题归类只认数据库确认的
    canonical/trusted 原串,不继承解析器派生名称。读不到 / 出异常一律返回空 ——
    别名缺失只是判据变窄,不影响主链。
    """
    try:
        bid = int(brand_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ()
    if bid <= 0:
        return ()
    try:
        from services.brand_identity_resolver import load_brand_identity

        identity = load_brand_identity(bid)
    except Exception:
        return ()
    names: list[str] = []
    for raw in (*(identity.canonical_names or ()), *(identity.trusted_aliases or ())):
        if isinstance(raw, str) and raw.strip():
            names.append(raw.strip())
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = normalize_brand_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return tuple(out)
