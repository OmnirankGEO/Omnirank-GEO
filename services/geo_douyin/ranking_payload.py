"""GEO 抖音榜单 · 冻结事实合同 `geo-ranking-visual-v1`

## 这个模块回答什么

规范 §1 的分工是「**业务 AI 决定写什么,生图模型只决定怎么呈现**」。
本模块就是那个"业务层"到"冻结层"的落点。它只做四件事,**不碰生图、不碰扣费、不碰发布**:

| 工序 | 出处 | 关键约束 |
|---|---|---|
| ① 候选取数 + 去重展示 | 工单 P1-3 | **不做跨名数据合并**(见下"归并边界") |
| ② 来源三分类清洗 | 工单 P1-3 | **清洗发生在冻结之前**;清洗是降级不是拒绝 |
| ③ 聚合合同七要素 | 工单 P1-2 | 七要素不齐 → **不许说"行业排名",只许说"某引擎对某问题列第 N"** |
| ④ 冻结 + 持久化八要素 | 工单 P1-4 | 冻结后快照**不可变**,渲染层只读 |

## 🔴 归并边界(为什么不激进合并)

生产实测:`geo_research_answer_entities.entity_key` 是 `me_<12hex>`,**只归并全角/半角括号
这类琐碎差异**(19,694 个 key 对应 1 个名,仅 286 个对应 2 个)。所以 `通力` / `通力电梯`、
`日立` / `日立电梯`、`深圳市恒通电梯有限公司` / `恒通电梯` 在库里是**四条独立记录**。

但**不能靠剥行业词来合并** —— `brand_identity_resolver.py:147-150` 用一个 P0 换来的边界写着:

> Industry nouns such as ``生物``/``电梯``/``酒店`` remain part of identity;
> dropping them would collapse distinct brands into the same short name.

实证:`通力`(KONE) / `巨人通力` / `智能通力电梯（惠州）` 是**三家不同公司**。剥掉「电梯」就合错了。

**本模块的两档处置**:

1. **确定性合并**(`safe_merge_key`)—— 只剥**行政区划前缀 + 法定组织形式后缀**,剥完完全相等才合。
   `深圳市恒通电梯有限公司` → `恒通电梯` == `恒通电梯` ✅ 合。数据可以加总。
2. **疑似同族**(`family_key`)—— 再剥行业通用尾词后相等。**不合并数据**,只在同一张榜里
   **最多取一个**(取提及数高的),另一个连同理由记进 `dropped_candidates` 留痕。
   `通力` vs `通力电梯` → 不加总、同榜只出一个。宁可少写一家,不制造假的"两家"。

## 🔴 广告法:同源不同匹配

词表**必须**来自 Owner 签发的版本化包(`services/marketing/guards.legal_pack()`,
`config/legal_prohibited_pack.json`,签发于 2026-07-23)—— 本模块**不自建第二份词表**。

但匹配**不能**沿用 `guards._find_words` 的纯子串:实测 1,164 条真实语料里「第一」61 次命中
有 17 次是「第一次 / 第一步 / 第一梯队 / 第一眼 / 第一口」等良性语境(**误报率 28%**)。
这正是「锚点撞到别处 → 判据永远红 → 人麻木」的形状。本模块用**良性续词否定前瞻**收窄。

## 🔴 执行层级:零硬拦

Owner 2026-08-01 二次拍板(supersede 治理 SSOT D8③「唯一硬门在发布边界」):
> 「广告法在写作提示词中写清楚不要触碰…**剩下客户审核不审核是他自己决定**。」

所以本模块对广告法命中的处置是 **降级 / 提示**,**不拒绝、不阻塞**。
签发包里的 `blocking: True` 描述的是**类别法律严重度**,执行层级由 08-01 拍板定 ——
文章链同样如此(`services/article_review_gate.py:338-346` 无视包的 blocking 自行降级为提示)。

⚠️ 另注:调用 `guards.scan_forbidden` 时**绝不传 `competitors=`** —— 它把竞品名当硬拦类
(营销链的需求),而榜单里点名竞品**正是本功能的主体**。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Optional, Sequence

logger = logging.getLogger("GEO-Douyin-Ranking")

RANKING_CONTRACT_VERSION: Final = "geo-ranking-visual-v1"

# ── 榜单实体数量(工单 §1.4:默认值,可覆盖;**不是判废闸**)────────────────
ENTITY_COUNT_MIN: Final = 3
ENTITY_COUNT_DEFAULT: Final = 4
ENTITY_COUNT_MAX: Final = 6

# ── 跨引擎共识门槛(入候选池的最低要求)────────────────────────────────
MIN_ENGINE_CONSENSUS: Final = 2


# ===========================================================================
# 一 · 实体归并(P1-3)
# ===========================================================================

def _org_suffixes() -> tuple[str, ...]:
    from services.brand_identity_resolver import _LEGAL_ENTITY_SUFFIXES
    return tuple(_LEGAL_ENTITY_SUFFIXES)


def _admin_prefixes() -> tuple[str, ...]:
    from services.brand_identity_resolver import _administrative_prefix_variants
    return _administrative_prefix_variants()


def _industry_tail_terms() -> tuple[str, ...]:
    """行业通用尾词 —— **只用于 family_key(疑似同族判定),绝不用于确定性合并**。"""
    from services.brand_identity_resolver import _GENERIC_INDUSTRY_TERMS
    return tuple(sorted(_GENERIC_INDUSTRY_TERMS, key=len, reverse=True))


def _norm(value: str) -> str:
    from services.brand_identity_resolver import normalize_brand_name
    return normalize_brand_name(value)


#: 通名行政区划前缀。`brand_identity_resolver._ADMINISTRATIVE_PREFIXES` 只枚举了
#: 省与主要城市,`惠州市`/`揭阳市`这类地级市不在表里 —— 而工商名称里它们同样是前缀。
#: 🔴 **必须带显式行政后缀才剥**:`北京银行`/`广州酒家`/`四川航空`没有「市/省」尾,
#:    不会被误剥;`深圳市晨光富士电梯` 才会。这是刻意保守。
_GENERIC_ADMIN_PREFIX_RE: Final = re.compile(
    r"^[一-龥]{2,4}(?:市|省|县|区|自治区|自治州|特别行政区)"
)

#: 剥完前缀后**剩下的部分至少要这么长** —— 否则不剥。
#: 🔴 这条不是保守洁癖,是防一类真 bug:`北京银行`/`上海银行` 都剥成 `银行` 就**合成了一家**;
#:    `广州酒家`/`深圳酒家` → `酒家`;`四川航空`/`广东航空` → `航空`。
#:    这与 `split_brand_aliases` 把「(深圳)」当别名那个 P0 是同一形态(地域词吃掉主体身份)。
_MIN_CORE_AFTER_ADMIN_STRIP: Final = 3


def _is_all_generic(core: str) -> bool:
    """剩下的是不是纯行业通名(`电梯`/`科技`/`酒店`)—— 是就不能当独立身份。"""
    from services.brand_identity_resolver import _GENERIC_DERIVED_NAME_TOKENS
    c = str(core or "").strip()
    if not c:
        return True
    return c in _GENERIC_DERIVED_NAME_TOKENS


def _strip_admin_prefix(core: str) -> str:
    """剥行政区划前缀。**剩余不够长或纯通名 → 原样返回,不剥。**"""
    candidate = ""
    for prefix in _admin_prefixes():
        if core.startswith(prefix) and len(core) > len(prefix):
            candidate = core[len(prefix):]
            break
    if not candidate:
        m = _GENERIC_ADMIN_PREFIX_RE.match(core)
        if m:
            candidate = core[m.end():]
    if not candidate:
        return core
    if len(candidate) < _MIN_CORE_AFTER_ADMIN_STRIP or _is_all_generic(candidate):
        return core
    return candidate


def safe_merge_key(name: str) -> str:
    """确定性合并键:剥行政区划前缀 + 法定组织形式后缀后的归一化名。

    只做**同一主体的不同书写形式**,不跨主体。
    ``深圳市恒通电梯有限公司`` 与 ``恒通电梯`` → 同键。
    ``通力`` 与 ``通力电梯`` → **不同键**(行业词是身份的一部分)。
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    core = _strip_admin_prefix(raw)
    for suffix in _org_suffixes():
        if core.endswith(suffix) and len(core) > len(suffix) + 1:
            core = core[: -len(suffix)]
            break
    # 🔴 剥到最后只剩纯通名(`电梯`/`科技`)→ 这个键**没有身份意义**,任何两个名字
    #    落到同一个通名上都会被错合。退回原名,宁可不合。
    if _is_all_generic(core):
        return _norm(raw)
    return _norm(core)


def family_key(name: str) -> str:
    """疑似同族键:在 `safe_merge_key` 基础上再剥一层行业通用尾词。

    🔴 **只用于"同一张榜里别出现两次"的去重展示,不得用它加总任何数据。**
    合错的代价(把两家不同公司算成一家)远大于漏合(榜上少一家)。
    """
    core = safe_merge_key(name)
    if not core:
        return ""
    for term in _industry_tail_terms():
        t = _norm(term)
        if t and core.endswith(t) and len(core) > len(t) + 1:
            return core[: -len(t)]
    return core


# ===========================================================================
# 二 · 来源三分类与清洗(P1-3)· 清洗在冻结之前
# ===========================================================================

SOURCE_VERIFIABLE: Final = "verifiable"          # 有可核验公开来源 → 保留自然引用
SOURCE_AI_PARAPHRASE: Final = "ai_paraphrase"    # 仅 AI 转述 → 不上图或降中性
SOURCE_LEGAL_ABSOLUTE: Final = "legal_absolute"  # 法律绝对化 → 按 08-01 口径降级提示

#: 良性续词:命中词后面紧跟这些字时**不算**绝对化主张。
#: 实测来源:1,164 条真实抖音语料,「第一」61 次命中里 17 次是这些形态(误报率 28%)。
_BENIGN_CONTINUATION: Final[dict[str, tuple[str, ...]]] = {
    "第一": ("次", "步", "时间", "眼", "口", "梯队", "阶段", "部分", "章", "条",
             "线", "位", "天", "年", "季", "轮", "版", "代", "批", "印象",
             "反应", "手", "现场", "作者", "人称"),
    "最大": ("化", "限度", "程度", "努力"),
    "最好": ("先", "是先", "还是"),
    "首选": (),
    "最佳": (),
}

#: 主体宣称语境:绝对化词只有落在**商业主张**里才算。
#: 目前用"良性续词否定"实现;若续词表覆盖不住,宁可漏报也不误伤(见模块 docstring)。


def absolute_law_hits(text: str) -> list[str]:
    """广告法绝对化命中 —— 词表同源自签发包,匹配用良性续词否定前瞻。

    与 `guards._find_words` 的差别只在**匹配方式**:词一个不多一个不少。
    """
    from services.marketing.guards import legal_pack
    body = str(text or "")
    if not body:
        return []
    hits: list[str] = []
    for word in legal_pack().get("ad_law") or ():
        w = str(word or "")
        if not w:
            continue
        benign = _BENIGN_CONTINUATION.get(w, ())
        start = 0
        while True:
            idx = body.find(w, start)
            if idx < 0:
                break
            tail = body[idx + len(w): idx + len(w) + 4]
            if not any(tail.startswith(b) for b in benign):
                hits.append(w)
                break
            start = idx + len(w)
    return hits


#: 别人的榜单结论 / 未经核实的资质宣称 —— 照搬进我们的图 = 替第三方做未核实宣称。
#: 生产真值实测样本:「TOP10榜单品牌」「10强服务商」「500强供应商」「A级/AAA级资质」
#: 「上市企业」「技术评分高达99.5分」「客户续费率96%」「承诺关键词进首页」。
_THIRD_PARTY_CLAIM_PATTERNS: Final[tuple[str, ...]] = (
    r"TOP\s*\d+\s*(榜单|品牌|服务商|企业)?",     # 别人的榜单结论
    r"\d+\s*强(服务商|企业|品牌|供应商)?",
    r"(排名|位列|荣获|入选).{0,6}(第\s*[一二三四五六七八九十\d]+|榜|奖)",
    r"[A-Za-z]{1,3}\s*级(资质|认证)",            # A级/AAA级资质
    r"\d+\s*强(供应商|客户|合作)",                # 500强供应商/客户/合作
    r"(上市|新三板|主板|创业板)\s*(公司|企业)?",
    r"(评分|得分|指数)\s*(高达|达)?\s*\d+(\.\d+)?\s*分?",
    r"(续费率|满意度|达成率|准确率|好评率|成功率|匹配度)\s*(高达|达|超过?)?\s*\d+(\.\d+)?\s*%?",
    r"(承诺|保证|包)\s*.{0,6}(首页|上榜|排名|见效|收录)",
)
_THIRD_PARTY_RE: Final = re.compile("|".join(_THIRD_PARTY_CLAIM_PATTERNS), re.IGNORECASE)


def classify_phrase(phrase: str, *, source_url: str = "") -> str:
    """给一条候选短语定来源类型。

    ⚠️ 顺序有意义:**先判法律绝对化**(它与有没有来源无关),再判有没有可核验来源。
    """
    text = str(phrase or "").strip()
    if not text:
        return SOURCE_AI_PARAPHRASE
    if absolute_law_hits(text):
        return SOURCE_LEGAL_ABSOLUTE
    if str(source_url or "").strip():
        return SOURCE_VERIFIABLE
    return SOURCE_AI_PARAPHRASE


@dataclass(frozen=True)
class CleanedPhrase:
    """清洗后的一条短语。`kept=False` 表示不上图,但**理由留痕**。"""
    text: str
    kept: bool
    source_type: str
    reason: str = ""
    original: str = ""


def clean_phrases(phrases: Iterable[str], *,
                  source_urls: Optional[Sequence[str]] = None) -> list[CleanedPhrase]:
    """来源驱动清洗(P1-3)。**降级,不拒绝** —— 整条内容永远继续生成。

    - 可核验来源 → 原样保留(渲染层按 D11 口径写成自然引用)
    - 仅 AI 转述 → 含第三方榜单结论/资质宣称的**不上图**;纯中性特征词保留
    - 法律绝对化 → 不上图(按 08-01 口径:写作侧就不该产出;发布侧另有提示层)
    """
    urls = list(source_urls or [])
    out: list[CleanedPhrase] = []
    for i, raw in enumerate(phrases or []):
        text = str(raw or "").strip()
        if not text:
            continue
        url = urls[i] if i < len(urls) else ""
        kind = classify_phrase(text, source_url=url)
        if kind == SOURCE_VERIFIABLE:
            out.append(CleanedPhrase(text=text, kept=True, source_type=kind, original=text))
            continue
        if kind == SOURCE_LEGAL_ABSOLUTE:
            out.append(CleanedPhrase(
                text="", kept=False, source_type=kind, original=text,
                reason="广告法绝对化用语,写作侧不产出"))
            continue
        if _THIRD_PARTY_RE.search(text):
            out.append(CleanedPhrase(
                text="", kept=False, source_type=kind, original=text,
                reason="第三方榜单结论/未核实资质宣称,仅 AI 转述不足以上图"))
            continue
        out.append(CleanedPhrase(text=text, kept=True, source_type=kind, original=text))
    return out


# ===========================================================================
# 三 · 聚合合同七要素(P1-2)
# ===========================================================================

AGGREGATION_ELEMENTS: Final[tuple[str, ...]] = (
    "query", "candidate_set", "window", "engine_scope",
    "name_normalization", "tie_rule", "algo_version",
)


@dataclass
class AggregationContract:
    """七要素齐 → 才允许说「综合/行业排名」;否则只许说「某引擎对某问题列第 N」。"""
    query: str = ""
    candidate_set: str = ""
    window: str = ""
    engine_scope: tuple[str, ...] = ()
    name_normalization: str = ""
    tie_rule: str = ""
    algo_version: str = ""

    def missing(self) -> list[str]:
        out: list[str] = []
        for k in AGGREGATION_ELEMENTS:
            v = getattr(self, k, None)
            if not v or (isinstance(v, (tuple, list)) and not [x for x in v if x]):
                out.append(k)
        return out

    @property
    def complete(self) -> bool:
        return not self.missing()

    def to_dict(self) -> dict:
        return {
            "query": self.query, "candidate_set": self.candidate_set,
            "window": self.window, "engine_scope": list(self.engine_scope),
            "name_normalization": self.name_normalization,
            "tie_rule": self.tie_rule, "algo_version": self.algo_version,
            "complete": self.complete, "missing": self.missing(),
        }


#: 「某引擎对某问题的回答里列第 N」这句话的**四元组** —— 必须同行取数。
#: 见 `ranking_source` 的 `DISTINCT ON` 注释:上一版各自聚合导致这句可以为假。
RANK_STATEMENT_TUPLE: Final = ("engine", "recommendation_rank", "query", "observed_at")


def rank_statement(item: "RankingItem", contract: AggregationContract) -> str:
    """名次怎么说人话 —— **这一句是 P1-2 的落地点**。

    七要素不齐时**绝不**产出「行业第 N / 综合排名第 N」,只产出可核验的那一句。

    🔴 2026-08-06 返工:不完整分支**只允许引用 `item.source` 里的同行四元组**。
       上一版拿 `contract.query`(聚合层的问题)去配 `item.source['engine']`
       (某一行的引擎),两者不必然同源 —— 那句话就成了拼出来的。
       四元组缺任何一元 → **不作任何引擎/名次断言**,退回中性表述。
    """
    if contract.complete:
        return f"综合排名第 {item.rank}"

    src = item.source or {}
    eng = str(src.get("engine") or "").strip()
    q = str(src.get("query") or "").strip()
    n = src.get("recommendation_rank")
    observed = str(src.get("observed_at") or "").strip()

    # 引擎与问题任一缺失 → 这句话的主语就不成立,不许拼
    if not eng or not q or not observed:
        return "被 AI 搜索提到过"
    if not n:
        return f"在 {eng} 回答「{q}」时被提到"
    return f"在 {eng} 回答「{q}」时列第 {n}"


def consensus_statement(engine_count: int) -> str:
    """跨引擎共识 —— 这是**计数事实**,不是排名,任何时候都可以说。"""
    n = max(0, int(engine_count or 0))
    if n <= 1:
        return ""
    return f"{n} 个 AI 都提到"


# ===========================================================================
# 四 · 冻结合同 + 持久化八要素(P1-4)
# ===========================================================================

@dataclass
class RankingItem:
    rank: int = 0
    entity_id: str = ""
    display_name: str = ""
    best_for: str = ""
    reason: str = ""
    tags: tuple[str, ...] = ()
    is_client: bool = False
    knowledge_field_paths: tuple[str, ...] = ()
    authorized_asset_ids: tuple[str, ...] = ()
    #: 举证链自带快照(**不存指针** —— `keyword_insights` 会被
    #: `ON CONFLICT (task_id, keyword) DO UPDATE` 原地覆盖)
    source: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rank": self.rank, "entity_id": self.entity_id,
            "display_name": self.display_name, "best_for": self.best_for,
            "reason": self.reason, "tags": list(self.tags),
            "is_client": self.is_client,
            "knowledge_field_paths": list(self.knowledge_field_paths),
            "authorized_asset_ids": list(self.authorized_asset_ids),
            "source": dict(self.source),
        }


#: 形态:有据进榜给实名次 / 无据换形态并**改名**(治理 SSOT D12④ 客户存在感三铁律)
FORM_RANKING: Final = "ranking"            # 可称「榜单/推荐榜」
FORM_SCENARIO: Final = "scenario_pick"     # 场景推荐
FORM_MATRIX: Final = "selection_matrix"    # 选型矩阵

#: 只有 FORM_RANKING 允许在标题/正文里出现"榜单/排行"字样。
FORMS_ALLOWING_RANKING_WORDING: Final = frozenset({FORM_RANKING})


@dataclass
class FrozenRankingPayload:
    """冻结事实合同。构建完成后**视觉层只读**;重抽单张只改视觉不回到这里。"""
    schema_version: str = RANKING_CONTRACT_VERSION
    template_id: str = ""
    form: str = FORM_SCENARIO
    industry_key: str = ""
    city: str = ""
    buyer_question: str = ""
    title: str = ""
    subtitle: str = ""
    ranking_basis: tuple[str, ...] = ()
    items: tuple[RankingItem, ...] = ()
    client_brand: str = ""
    aggregation: AggregationContract = field(default_factory=AggregationContract)
    missing_fields: tuple[str, ...] = ()
    dropped_candidates: tuple[dict, ...] = ()
    cleaning_log: tuple[dict, ...] = ()
    contact_mode: str = "none"
    visual: dict = field(default_factory=lambda: {"aspect_ratio": "9:16", "image_count": 1})

    # ── 八要素之 ②:版本 hash ──────────────────────────────────────
    def canonical(self) -> dict:
        """参与 hash 的规范化视图。**顺序稳定、不含易变字段**。"""
        return {
            "schema_version": self.schema_version,
            "template_id": self.template_id,
            "form": self.form,
            "industry_key": self.industry_key,
            "city": self.city,
            "buyer_question": self.buyer_question,
            "title": self.title,
            "subtitle": self.subtitle,
            "ranking_basis": list(self.ranking_basis),
            "items": [i.to_dict() for i in self.items],
            "client_brand": self.client_brand,
            "aggregation": self.aggregation.to_dict(),
        }

    def contract_hash(self) -> str:
        blob = json.dumps(self.canonical(), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ── 八要素之 ③:幂等键 ────────────────────────────────────────
    def idempotency_key(self, post_id: int) -> str:
        return f"ranking:{int(post_id)}:{self.contract_hash()[:16]}"

    def allows_ranking_wording(self) -> bool:
        """能不能挂"榜单/排行"这个名 —— 无据分支一律不许(D12④)。"""
        return self.form in FORMS_ALLOWING_RANKING_WORDING

    def to_dict(self) -> dict:
        d = self.canonical()
        d.update({
            "missing_fields": list(self.missing_fields),
            "dropped_candidates": [dict(x) for x in self.dropped_candidates],
            "cleaning_log": [dict(x) for x in self.cleaning_log],
            "contact_mode": self.contact_mode,
            "visual": dict(self.visual),
            "contract_hash": self.contract_hash(),
        })
        return d


def select_entities(candidates: Sequence[dict], *,
                    want: int = ENTITY_COUNT_DEFAULT,
                    client_brand: str = "") -> tuple[list[dict], list[dict]]:
    """候选池 → 上榜名单。返回 (选中, 被排除并留痕)。

    两步:
      ① `safe_merge_key` 相同 → **确定性合并**(数据加总,取提及数最高的展示名)
      ② `family_key` 相同 → **不合并数据**,同榜只留提及数最高的一个,另一个留痕

    ⚠️ 客户本人若在候选里,**不在这里剔除** —— 有据分支要让它进榜给实名次(D12④)。
       是否点名客户由上层按 form 决定。
    """
    n = max(ENTITY_COUNT_MIN, min(int(want or ENTITY_COUNT_DEFAULT), ENTITY_COUNT_MAX))

    merged: dict[str, dict] = {}
    for c in candidates or []:
        name = str(c.get("entity_name") or c.get("display_name") or "").strip()
        if not name:
            continue
        k = safe_merge_key(name)
        if not k:
            continue
        cur = merged.get(k)
        if cur is None:
            merged[k] = {**c, "entity_name": name,
                         "_merged_names": [name],
                         "mention_count": int(c.get("mention_count") or 0),
                         "engine_count": int(c.get("engine_count") or 0)}
            continue
        cur["mention_count"] = int(cur.get("mention_count") or 0) + int(c.get("mention_count") or 0)
        cur["engine_count"] = max(int(cur.get("engine_count") or 0), int(c.get("engine_count") or 0))
        cur["_merged_names"].append(name)
        # 展示名取更长的那个(信息量更足:`深圳市恒通电梯有限公司` > `恒通电梯`)
        if len(name) > len(str(cur.get("entity_name") or "")):
            cur["entity_name"] = name

    ordered = sorted(merged.values(),
                     key=lambda x: (int(x.get("engine_count") or 0),
                                    int(x.get("mention_count") or 0)),
                     reverse=True)

    picked: list[dict] = []
    dropped: list[dict] = []
    seen_family: dict[str, str] = {}
    # 🔴 **走完整表再停**,不在选满时 break —— 否则「通力电梯 因与通力同族被排除」
    #    这条最需要被审计看见的留痕会丢掉,而丢掉的痕迹在事后无法与"没轮到"区分。
    for c in ordered:
        name = str(c.get("entity_name") or "")
        if int(c.get("engine_count") or 0) < MIN_ENGINE_CONSENSUS:
            dropped.append({"name": name, "reason": "跨引擎共识不足",
                            "engine_count": int(c.get("engine_count") or 0)})
            continue
        # 🔴 同族去重发生在**候选池层面**,与名额无关 —— 否则「通力电梯 与 通力 同族」
        #    会被"名额已满"盖掉,事后看不出它是被去重还是没轮到。
        fam = family_key(name)
        if fam and fam in seen_family:
            dropped.append({"name": name, "reason": "疑似与更靠前的候选同族,同榜只取一个",
                            "same_family_as": seen_family[fam]})
            continue
        if fam:
            seen_family[fam] = name
        if len(picked) >= n:
            dropped.append({"name": name, "reason": "名额已满(候选合格但本次不上榜)"})
            continue
        picked.append(c)
    return picked, dropped
