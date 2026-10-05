"""Single source of truth for GEO brand-mention decisions.

The resolver intentionally separates trusted identity data from answer analysis:

- trusted names come from one ``brands.id`` plus manually confirmed aliases bound
  to that same id;
- globally discovered aliases and industry-suffix guesses are never considered;
- exact trusted aliases and conservative legal-name abbreviations are checked
  across the complete answer;
- ambiguous spelling/store cases are sent as bounded evidence windows to one
  structured YES/NO/UNKNOWN verifier.

UNKNOWN is a first-class outcome. Monitoring persists source-anchored ambiguous
matches for human review; infrastructure failures remain ordinary hard errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Final, Iterable, Sequence

import httpx

from services.industry_taxonomy import display_name   # WO_267:industry_category 读侧翻译

logger = logging.getLogger("GEO-BrandIdentity")


class BrandVerdict(str, Enum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class VerificationResult:
    verdict: BrandVerdict
    reason: str = ""
    matched_text: str | None = None
    window_index: int | None = None
    matched_start: int | None = None
    matched_end: int | None = None


@dataclass(frozen=True)
class EvidenceWindow:
    text: str
    source_start: int
    source_end: int


@dataclass(frozen=True)
class BrandIdentity:
    brand_id: int | None
    canonical_names: tuple[str, ...]
    trusted_aliases: tuple[str, ...] = ()
    rejected_aliases: tuple[str, ...] = ()
    industry: str = ""
    load_error: bool = False

    @property
    def all_trusted_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for raw in (*self.canonical_names, *self.trusted_aliases):
            names.extend(split_brand_aliases(raw))
        return _dedupe_nonempty(names)


@dataclass(frozen=True)
class BrandDecision:
    verdict: BrandVerdict
    reason: str
    method: str
    matched_alias: str | None = None
    matched_start: int | None = None
    matched_end: int | None = None
    evidence_snippet: str | None = None
    #: [WO 2026-08-06 §1] 被 ``_is_plausible_verified_match`` 打回的复核候选原文。
    #:
    #: 🔴 **它不是 matched_alias**,两个字段的语义必须分开:``matched_alias`` 说
    #: 「本品牌就是以这个写法被提到的」(YES 时下游据此算位置/推荐档),
    #: ``near_miss_alias`` 只说「原文里有个长得像的写法,等人确认」。混用会让
    #: 下游按位置自动升格成"明确推荐"—— 那正是 08-05 城市别名 P0 的放大链路。
    #:
    #: 生产实证(诊断 561):16 格走 ``invalid_matched_text``,复核层交回的
    #: 「阿强龙虾」在这一步被就地丢弃,待确认卡片因此零候选可点。
    near_miss_alias: str | None = None


Verifier = Callable[..., Awaitable[VerificationResult]]

_BRACKET_RE = re.compile(r"[（(]([^）)]*)[）)]")
_NORMALIZE_RE = re.compile(r"[\s\u3000·•・,，.。;；:：'\"“”‘’\-_/\\（）()【】\[\]《》<>]+")
#: 🔴 [返工 2026-08-06] 补 又名/别名/亦称 —— 返工单点名「又名/品牌」是显式别名前缀,
#: 而原表只有「又称」。少一个词 = 一个人明确声明过的别名被行政标记闸误拒。
_ALIAS_PREFIX_RE = re.compile(
    r"^(?:简称|品牌简称|品牌名|英文名|英文简称|又称|又名|亦称|别名)\s*[:：]?\s*", re.I
)
#: 行政区划**块后缀**(判"这一块是不是一级行政区划",不是"字符里有没有这个字")。
#: 🔴 **刻意不含「村」**:含了「乡村基」会被切成 '乡村' 而命中 —— 那正是复审
#: 抓出来的六个真实品牌误杀之一。改结构判据不等于自动安全,后缀表本身就是判据。
_ADMINISTRATIVE_CHUNK_SUFFIXES = (
    "自治区", "特别行政区", "自治州", "自治县", "地区",
    "省", "市", "区", "县", "镇", "乡", "街道", "旗", "盟",
)


#: 单段地名的最短长度。单字("中""山")不构成地名,否则任何两字词都能被拆散。
_ADMIN_NAME_MIN_LEN = 2

#: 地级建制里 `_CITY_PREFIXES`(494 项)缺的那批**裸简称**。
#:
#: 🔴 [三轮复查抽样 · 2026-08-07] 494 表按"地级市"编,漏掉了地级建制的另外三类:
#: **30 个自治州 / 3 个盟 / 7 个地区**,外加两个新设地级市(三沙 2012 / 那曲 2017)。
#: 实测「(甘孜)」「(凉山)」「(黔东南)」「(博尔塔拉)」「(巴音郭楞)」等仍穿透。
#: 按地级建制全集(333)对照补齐,**只列我能确认的**;拿不准的宁缺 ——
#: 编一个错的地名进拒绝路,代价是误杀真品牌。
#:
#: 🔴 **只追加进拒绝路的全量名录**;剥前缀窄表(132 项)一个字不动(有锁兜)。
_PREFECTURE_LEVEL_EXTRA_NAMES = (
    # 新设地级市
    "三沙", "那曲",
    # 自治州(30)· 已在 494 表里的(延边/恩施/湘西/红河/文山/西双版纳/大理/德宏)不重列
    "阿坝", "甘孜", "凉山",
    "黔东南", "黔南", "黔西南",
    "楚雄", "怒江", "迪庆",
    "临夏", "甘南",
    "海北", "黄南", "果洛", "玉树", "海西",
    "昌吉", "博尔塔拉", "巴音郭楞", "克孜勒苏", "伊犁",
    # 盟(3)
    "兴安", "锡林郭勒", "阿拉善",
    # 地区(7)· 已在 494 表里的(阿克苏/喀什/和田)不重列
    "大兴安岭", "塔城", "阿勒泰", "阿里",
)


def _administrative_name_set() -> frozenset[str]:
    """全量行政区划名(归一后)。**只服务拒绝路,不碰任何剥前缀路。**

    🔴🔴 [三轮复审 · 2026-08-07] 上一版只有 ``_ADMINISTRATIVE_PREFIXES`` 手工枚举
    **93 项**,340 个地级市缺 260+。实测「(中山)」「(朝阳)」「(东莞)」「(佛山)」
    「(惠州)」「(汕头)」「(洛阳)」「(襄阳)」「(宜昌)」「(绵阳)」「(唐山)」「(保定)」
    **13/13 全部穿透成可信别名** —— 原 P0 根本没闭。手工枚举永远追不上行政区划表。

    数据源 = 仓内**已有**的 `tools/keyword_value_scorer._CITY_PREFIXES`(494 项,
    地级市 + 省 + 部分市辖区),**不另抄一份**(两份表必然漂移;而且我没有权威
    县级名录,编一份 2800 条的假表比缺表更坏)。该模块纯 stdlib、无模块级副作用,
    已在 `python:3.12-slim` 裸容器实测可导入。

    🔴 **这个集合与 ``_administrative_prefix_variants()`` 是两回事,不许合并**:
    后者被 ``_without_administrative_prefix`` / ``_derived_legal_name_forms``
    两条**剥前缀**路消费,把它换成 494 项会让「洛阳钼业」被剥成「钼业」、
    「东莞证券」被剥成「证券」——那是另一个方向的事故。分开是有意的,
    并配了逐消费方 A/B 锁。
    """
    names: set[str] = set()
    for source in (
        _ADMINISTRATIVE_PREFIXES,
        _administrative_prefix_variants(),
        # [三轮复查补丁] 自治州/盟/地区 —— 494 表按"地级市"编,漏了这三类
        _PREFECTURE_LEVEL_EXTRA_NAMES,
    ):
        for name in source:
            normalized = normalize_brand_name(name)
            if len(normalized) >= _ADMIN_NAME_MIN_LEN:
                names.add(normalized)
    try:
        from tools.keyword_value_scorer import _CITY_PREFIXES

        for name in _CITY_PREFIXES:
            normalized = normalize_brand_name(name)
            if len(normalized) >= _ADMIN_NAME_MIN_LEN:
                names.add(normalized)
    except Exception:  # pragma: no cover - 拿不到全量表就退回窄表(只会漏拒,不会误杀)
        logger.warning("administrative gazetteer unavailable; falling back to the narrow table")
    return frozenset(names)


def _decomposes_into_administrative_names(normalized: str) -> bool:
    """整串能否**完全**切成已知地名的拼接(「深圳南山」= 深圳 + 南山)。

    🔴 判据是**全覆盖**,不是"以地名开头"。这一条是上一轮误杀六个真实品牌的
    那个 `startswith` 规则的正确写法:
        深圳南山 → 深圳 + 南山           → 全覆盖 → 拒 ✅
        北京同仁堂 → 北京 + 「同仁堂」✗   → 切不完 → 放行 ✅
        上海家化 / 广州酒家 / 青岛啤酒 / 重庆小面 → 同上,全部放行 ✅
    """
    if len(normalized) < _ADMIN_NAME_MIN_LEN:
        return False
    names = _administrative_name_set()
    reachable = [False] * (len(normalized) + 1)
    reachable[0] = True
    for start in range(len(normalized)):
        if not reachable[start]:
            continue
        for end in range(start + _ADMIN_NAME_MIN_LEN, len(normalized) + 1):
            if normalized[start:end] in names:
                reachable[end] = True
    return reachable[-1]


def _looks_like_administrative_address(value: str) -> bool:
    """候选是不是一串**地址/行政区划**(而不是品牌别名)。

    两条判据,任一成立即判地址:
      ① **结构** —— 按 ``_admin_chunks`` 切块,任一块以行政后缀结尾
         (龙岗区平湖 / 深圳市南山区 / 茅台镇);
      ② **名录全覆盖** —— 整串能完全切成已知地名(中山 / 朝阳 / 深圳南山)。
    ① 管"带后缀的写法",② 管"裸地名"。缺任何一条都有一整族穿透。

    取不到切块器时只退化掉 ①,② 仍然生效;两条都取不到才 fail-open(放行)——
    误拒真品牌是**确定的**客户损失,漏拒地名还有下游整串匹配兜着。
    """
    text = (value or "").strip()
    if not text:
        return False
    try:
        from services.diagnosis_question_quality import _admin_chunks

        for chunk in _admin_chunks(text):
            for suffix in _ADMINISTRATIVE_CHUNK_SUFFIXES:
                if chunk.endswith(suffix) and len(chunk) > len(suffix):
                    return True
    except Exception:  # pragma: no cover - 依赖缺失时只丢掉 ①
        pass
    return _decomposes_into_administrative_names(normalize_brand_name(text))
_PARENTHETICAL_QUALIFIER_SUFFIXES = (
    "个体工商户", "有限合伙", "普通合伙", "分公司", "办事处", "旗舰店",
    "直营店", "总店", "总部", "厂区", "基地", "园区", "项目部", "事业部",
    "营业部", "门店",
)

# These may be useful context to an LLM but must never be the only fuzzy signal.
_GENERIC_SUFFIX_TOKENS = {
    "公司", "集团", "有限", "股份", "科技", "技术", "生物", "酒店", "门店",
    "电梯", "地产", "品牌", "服务", "中心", "中国", "控股", "实业", "企业",
}

_GENERIC_INDUSTRY_TERMS = {
    "制造业", "服务业", "酒店业", "零售业", "科技", "生物", "制造",
    "教育", "医疗", "金融", "地产", "电梯", "酒店", "门店", "品牌",
}
# A derived legal abbreviation must retain a proprietary token.  These tokens
# are deliberately broad industry descriptors: if a candidate can be composed
# entirely from them, ordinary industry prose must not become a deterministic
# brand hit.  The structured verifier remains available for ambiguous cases.
_GENERIC_DERIVED_NAME_TOKENS = {
    "生物", "科技", "技术", "医疗", "医药", "药业", "制药", "生物医药",
    "生命", "基因", "细胞", "蛋白", "疫苗", "诊断", "试剂", "器械", "医学",
    "健康", "网络", "信息", "数据", "智能", "互联网", "电子", "软件", "通信",
    "通讯", "系统", "数字", "自动化", "机械", "设备", "装备", "电器", "电气",
    "仪器", "仪表", "光电", "激光", "半导体", "光学", "光伏", "微电子",
    "集成电路", "芯片", "电路", "传感", "视觉", "影像", "量子", "精密",
    "精工", "纳米", "材料", "新材料", "能源", "新能源", "环保", "化工",
    "化学", "冶金", "钢铁", "建材", "石化", "电力", "储能", "风电", "航空",
    "航天", "船舶", "轨道", "机电", "控制", "测控", "食品", "农业", "农牧",
    "种业", "食品科技", "教育", "文化", "传媒", "影视", "金融", "投资",
    "建设", "工程", "贸易", "商贸", "实业", "发展", "制造", "智造", "物流",
    "供应链", "咨询", "管理", "服务", "商务", "产业", "生产", "研发", "检测",
    "检验", "认证", "环境", "生态", "节能", "工业", "装饰", "设计", "集团",
    "公司", "有限", "股份", "控股", "中心", "企业", "品牌", "酒店", "电梯",
    "地产", "门店",
}
_SHORT_PREFIX_CONTEXT = ("推荐", "选择", "入住", "体验", "关注", "品牌", "来自")
_SHORT_SUFFIX_CONTEXT = ("酒店", "品牌", "门店", "公司", "集团", "科技", "生物")

_LEGAL_ABBREVIATION_LEFT_CONTEXT = (
    "包括", "包含", "推荐", "选择", "关注", "例如", "比如", "如", "由", "与",
    "和", "及", "品牌", "企业", "公司", "厂商", "供应商", "服务商", "候选",
)
_LEGAL_ABBREVIATION_RIGHT_CONTEXT = (
    "的", "是", "为", "在", "由", "与", "和", "及", "可", "能", "将", "已",
    "也", "其", "等", "凭借", "提供", "推出", "专注", "拥有", "成立", "覆盖",
    "值得", "入选", "上榜", "位于", "作为", "通过",
)

# Legal-name shortening is intentionally narrow.  Derived forms are accepted
# deterministically only with a proprietary core and safe source boundaries.
# They are never persisted as aliases; ambiguous candidates still go through
# the structured verifier.
_LEGAL_ENTITY_SUFFIXES = (
    "有限责任公司",
    "股份有限公司",
    "集团有限公司",
    "股份公司",
    "有限公司",
    "公司",
)
# Only a trailing grammatical descriptor may be removed deterministically.
# Industry nouns such as ``生物``/``电梯``/``酒店`` remain part of identity;
# dropping them would collapse distinct brands into the same short name.
_LEGAL_TRAILING_DESCRIPTORS = ("技术",)
# Bounded storefront variants (ported from ec6be81d · 2026-07-21):
# a location-qualified hotel/store display form such as ``滨江南路雅栖酒店``
# is accepted deterministically only inside tight length/context bounds so
# distinct chain branches are never collapsed into one brand.
_STOREFRONT_SUFFIXES = ("酒店", "门店")
_STOREFRONT_MAX_LEADING_OMISSION = 3
_STOREFRONT_MAX_INSERTION = 2
_STOREFRONT_MIN_VARIANT_LENGTH = 7
_STOREFRONT_MIN_INSERTION_SUFFIX = 7
_STOREFRONT_LEFT_CONTEXT = (*_LEGAL_ABBREVIATION_LEFT_CONTEXT, "首选", "考虑", "选用")
_STOREFRONT_RIGHT_CONTEXT = (*_LEGAL_ABBREVIATION_RIGHT_CONTEXT, "适合")
_ADMINISTRATIVE_PREFIXES = (
    "内蒙古自治区", "广西壮族自治区", "宁夏回族自治区", "新疆维吾尔自治区",
    "西藏自治区", "香港特别行政区", "澳门特别行政区",
    "黑龙江省", "北京市", "天津市", "上海市", "重庆市",
    "河北省", "山西省", "辽宁省", "吉林省", "江苏省", "浙江省", "安徽省",
    "福建省", "江西省", "山东省", "河南省", "湖北省", "湖南省", "广东省",
    "海南省", "四川省", "贵州省", "云南省", "陕西省", "甘肃省", "青海省",
    "台湾省", "深圳市", "广州市", "杭州市", "南京市", "苏州市", "成都市",
    "武汉市", "长沙市", "郑州市", "西安市", "合肥市", "福州市", "厦门市",
    "宁波市", "青岛市", "济南市", "大连市", "沈阳市", "长春市", "哈尔滨市",
    "石家庄市", "太原市", "南昌市", "南宁市", "海口市", "昆明市", "贵阳市",
    "兰州市", "西宁市", "银川市", "乌鲁木齐市", "拉萨市",
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
)


def _administrative_prefix_variants() -> tuple[str, ...]:
    variants = set(_ADMINISTRATIVE_PREFIXES)
    for prefix in _ADMINISTRATIVE_PREFIXES:
        for suffix in ("特别行政区", "自治区", "省", "市"):
            if prefix.endswith(suffix) and len(prefix) > len(suffix):
                variants.add(prefix[:-len(suffix)])
                break
    return tuple(sorted(variants, key=len, reverse=True))

_DIAGNOSIS_VERIFIER_CONCURRENCY = 8
_DIAGNOSIS_VERIFIER_LIMITERS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)
_DIAGNOSIS_VERIFIER_LIMITERS_LOCK = threading.Lock()

_MAX_CONFIRMED_DISPLAY_NAMES = 12
_MAX_CONFIRMED_DISPLAY_NAME_LENGTH = 80


def _dedupe_nonempty(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = (value or "").strip()
        key = normalize_brand_name(cleaned)
        if not cleaned or not key or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return tuple(out)


def _safe_optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_brand_name(value: str) -> str:
    """Normalize punctuation/case without silently discarding bracket content."""
    if not value:
        return ""
    return _NORMALIZE_RE.sub("", value).strip().casefold()


def normalize_confirmed_display_names(
    values: Sequence[str] | str | None,
) -> tuple[str, ...]:
    """Validate operator-confirmed public brand names for the identity SSOT.

    These values are explicit business data, not LLM guesses.  The function is
    shared by diagnosis and brand-profile writes so the two entry points cannot
    drift.  Generic industry words are rejected because treating them as a
    trusted alias would turn ordinary prose into a false brand mention.
    """
    if values is None:
        return ()
    raw_values: Sequence[str]
    if isinstance(values, str):
        raw_values = _parse_display_names(values)
    else:
        raw_values = values

    cleaned: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            raise ValueError("品牌常用名必须是文字")
        value = _ALIAS_PREFIX_RE.sub("", raw.strip())
        normalized = normalize_brand_name(value)
        if not normalized:
            continue
        if len(normalized) < 2:
            raise ValueError("品牌常用名至少需要 2 个字符")
        if len(value) > _MAX_CONFIRMED_DISPLAY_NAME_LENGTH:
            raise ValueError(
                f"单个品牌常用名不能超过 {_MAX_CONFIRMED_DISPLAY_NAME_LENGTH} 个字符"
            )
        if normalized in _GENERIC_SUFFIX_TOKENS or normalized in _GENERIC_INDUSTRY_TERMS:
            raise ValueError(f"“{value}”过于宽泛，请填写更完整的品牌称呼")
        cleaned.append(value)

    result = _dedupe_nonempty(cleaned)
    if len(result) > _MAX_CONFIRMED_DISPLAY_NAMES:
        raise ValueError(f"品牌常用名不能超过 {_MAX_CONFIRMED_DISPLAY_NAMES} 个")
    return result


def persist_confirmed_display_names(
    brand_id: int,
    values: Sequence[str] | str | None,
) -> tuple[str, ...]:
    """Persist confirmed display names to the existing brand identity SSOT.

    ``brands.brand_display_names`` is authoritative.  The profile column is a
    compatibility mirror for existing profile consumers and is updated in the
    same transaction.  No global alias row is created and no other brand is
    touched.
    """
    normalized = normalize_confirmed_display_names(values)
    payload = json.dumps(list(normalized), ensure_ascii=False)

    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE brands
            SET brand_display_names = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (payload, int(brand_id)),
        )
        if cur.rowcount != 1:
            raise LookupError("品牌不存在")
        cur.execute(
            """
            UPDATE client_profiles
            SET brand_display_names = %s, updated_at = CURRENT_TIMESTAMP
            WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
            """,
            (payload, int(brand_id)),
        )
        conn.commit()
        return normalized
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_with_positions(value: str) -> tuple[str, tuple[int, ...]]:
    """Normalize text while retaining the source index for every output char."""
    normalized: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(value or ""):
        normalized_char = normalize_brand_name(char)
        normalized.extend(normalized_char)
        positions.extend([index] * len(normalized_char))
    return "".join(normalized), tuple(positions)


def _short_chinese_exact_is_safe(
    answer: str,
    start: int,
    end: int,
    normalized_alias: str,
    identity: BrandIdentity,
) -> bool:
    """Require brand-like context for short Chinese names and reject industry words."""
    normalized_industry = normalize_brand_name(identity.industry)
    if normalized_alias in _GENERIC_INDUSTRY_TERMS or (
        normalized_industry and normalized_alias == normalized_industry
    ):
        return False
    prefix = answer[max(0, start - 8):start]
    suffix = answer[end:min(len(answer), end + 8)]
    left_boundary = start == 0 or not re.match(r"[\u3400-\u9fffA-Za-z0-9]", answer[start - 1])
    right_boundary = end == len(answer) or not re.match(r"[\u3400-\u9fffA-Za-z0-9]", answer[end])
    has_prefix_context = any(
        prefix.endswith(marker)
        for marker in (*_SHORT_PREFIX_CONTEXT, *_LEGAL_ABBREVIATION_LEFT_CONTEXT)
    )
    has_suffix_context = any(
        suffix.startswith(marker)
        for marker in (*_SHORT_SUFFIX_CONTEXT, *_LEGAL_ABBREVIATION_RIGHT_CONTEXT)
    )
    return (left_boundary or has_prefix_context) and (right_boundary or has_suffix_context)


def _identity_hit_is_negated_or_uncertain(answer: str, start: int, end: int) -> bool:
    """Keep negative/uncertain identity statements out of deterministic YES.

    Seeing the legal name is not the same as asserting the entity exists or is
    recommended.  A bounded clause check is intentionally used here; ambiguous
    cases continue through the existing evidence/verifier path instead of being
    converted into a hard NO.
    """
    clause_start = max(
        answer.rfind(mark, 0, start)
        for mark in ("。", "！", "？", "!", "?", "；", ";", "\n", "，", ",")
    )
    clause_end_candidates = [
        answer.find(mark, end)
        for mark in ("。", "！", "？", "!", "?", "；", ";", "\n", "，", ",")
    ]
    clause_end_candidates = [index for index in clause_end_candidates if index >= 0]
    clause_end = min(clause_end_candidates) if clause_end_candidates else len(answer)
    before = answer[clause_start + 1:start][-32:]
    after = answer[end:clause_end][:32]
    quote_space = r"[\s\u3000‘’“”'\"《》【】（）()]*"
    negative_before = re.search(
        rf"(?:没有|并未|尚未|从未|未能|未|无法|不能|难以)"
        rf".{{0,18}}?(?:名为|叫做|所谓)?{quote_space}$",
        before,
    )
    direct_negation = re.search(
        rf"(?:不是|并非|不属于|不包括|不含|不推荐|未推荐){quote_space}$",
        before,
    )
    negative_after = re.match(
        rf"^{quote_space}(?:不存在|并不存在|没有(?:相关|公开)?(?:信息|资料|记录)|"
        rf"无法确认|尚待确认|未找到|未被提及|未获推荐|不在(?:名单|榜单))",
        after,
    )
    return bool(negative_before or direct_negation or negative_after)


def _trusted_exact_hit(
    answer: str,
    alias: str,
    identity: BrandIdentity,
) -> tuple[str, int, int] | None:
    """Return the actual source substring/span for a trusted exact alias."""
    cleaned = (alias or "").strip()
    if not cleaned:
        return None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+&'/-]*", cleaned):
        matches = re.finditer(
            rf"(?<![A-Za-z0-9]){re.escape(cleaned)}(?![A-Za-z0-9])",
            answer,
            flags=re.I,
        )
        for match in matches:
            if not _identity_hit_is_negated_or_uncertain(
                answer,
                match.start(),
                match.end(),
            ):
                return match.group(0), match.start(), match.end()
        return None

    normalized_alias = normalize_brand_name(cleaned)
    normalized_answer, source_positions = _normalize_with_positions(answer)
    normalized_start = normalized_answer.find(normalized_alias) if normalized_alias else -1
    while normalized_start >= 0:
        normalized_end = normalized_start + len(normalized_alias)
        source_start = source_positions[normalized_start]
        source_end = source_positions[normalized_end - 1] + 1
        if _short_chinese_exact_is_safe(
            answer,
            source_start,
            source_end,
            normalized_alias,
            identity,
        ) and not _identity_hit_is_negated_or_uncertain(
            answer,
            source_start,
            source_end,
        ):
            return answer[source_start:source_end], source_start, source_end
        normalized_start = normalized_answer.find(normalized_alias, normalized_start + 1)
    return None


def _trusted_legal_suffix_extension_hit(
    answer: str,
    identity: BrandIdentity,
) -> tuple[str, int, int] | None:
    """Match a trusted company stem followed by an exact legal suffix.

    Some legacy brand rows persist the legal company stem without ``有限公司``
    while providers return the complete registered name.  Accept only the
    contiguous trusted stem plus a recognized entity suffix, with the same
    proprietary-core and source-boundary protections used for legal-name
    abbreviations.  This does not create or persist a new alias.
    """
    normalized_answer, source_positions = _normalize_with_positions(answer)
    rejected_names = {
        normalize_brand_name(value)
        for value in identity.rejected_aliases
        if value
    }
    candidates: list[str] = []
    for trusted_name in identity.all_trusted_names:
        cleaned = (trusted_name or "").strip()
        normalized_trusted = normalize_brand_name(cleaned)
        if (
            not normalized_trusted
            or any(cleaned.endswith(suffix) for suffix in _LEGAL_ENTITY_SUFFIXES)
            or len(_legal_proprietary_core(cleaned)) < 2
            or _is_generic_only_derived_form(cleaned)
        ):
            continue
        for suffix in _LEGAL_ENTITY_SUFFIXES:
            normalized_candidate = normalized_trusted + normalize_brand_name(suffix)
            if normalized_candidate in rejected_names:
                continue
            candidates.append(normalized_candidate)

    for normalized_candidate in sorted(
        candidates,
        key=len,
        reverse=True,
    ):
        normalized_start = normalized_answer.find(normalized_candidate)
        while normalized_start >= 0:
            normalized_end = normalized_start + len(normalized_candidate)
            source_start = source_positions[normalized_start]
            source_end = source_positions[normalized_end - 1] + 1
            prefix = answer[max(0, source_start - 12):source_start]
            left_boundary = source_start == 0 or not re.match(
                r"[\u3400-\u9fffA-Za-z0-9]",
                answer[source_start - 1],
            )
            left_context = any(
                prefix.endswith(marker)
                for marker in _LEGAL_ABBREVIATION_LEFT_CONTEXT
            )
            # A complete legal suffix closes the entity name, so ordinary
            # Chinese prose may follow without punctuation.  The left boundary
            # still rejects the same stem embedded in a longer competitor name.
            if (left_boundary or left_context) and not _identity_hit_is_negated_or_uncertain(
                answer,
                source_start,
                source_end,
            ):
                return answer[source_start:source_end], source_start, source_end
            normalized_start = normalized_answer.find(
                normalized_candidate,
                normalized_start + 1,
            )
    return None


def _human_rejected_exact_hit(answer: str, rejected_name: str) -> tuple[str, int, int] | None:
    """Find a tenant-scoped human-rejected name without positive-alias heuristics."""
    normalized_name = normalize_brand_name(rejected_name)
    normalized_answer, source_positions = _normalize_with_positions(answer)
    if not normalized_name or len(normalized_name) < 2:
        return None
    normalized_start = normalized_answer.find(normalized_name)
    if normalized_start < 0:
        return None
    normalized_end = normalized_start + len(normalized_name)
    source_start = source_positions[normalized_start]
    source_end = source_positions[normalized_end - 1] + 1
    return answer[source_start:source_end], source_start, source_end


def _is_bounded_storefront_variant(candidate: str, trusted_name: str) -> bool:
    """Accept narrow location-store variants without collapsing chain branches."""
    normalized_candidate = normalize_brand_name(candidate)
    normalized_trusted = normalize_brand_name(trusted_name)
    if (
        normalized_candidate == normalized_trusted
        or len(normalized_candidate) < _STOREFRONT_MIN_VARIANT_LENGTH
    ):
        return False
    suffix = next(
        (
            normalize_brand_name(value)
            for value in _STOREFRONT_SUFFIXES
            if normalized_trusted.endswith(normalize_brand_name(value))
        ),
        "",
    )
    if not suffix or not normalized_candidate.endswith(suffix):
        return False

    length_delta = len(normalized_candidate) - len(normalized_trusted)
    if -_STOREFRONT_MAX_LEADING_OMISSION <= length_delta < 0:
        return normalized_trusted.endswith(normalized_candidate)
    if not 0 < length_delta <= _STOREFRONT_MAX_INSERTION:
        return False

    prefix_length = 0
    while (
        prefix_length < len(normalized_trusted)
        and normalized_candidate[prefix_length] == normalized_trusted[prefix_length]
    ):
        prefix_length += 1

    suffix_length = 0
    while (
        suffix_length < len(normalized_trusted) - prefix_length
        and normalized_candidate[-(suffix_length + 1)]
        == normalized_trusted[-(suffix_length + 1)]
    ):
        suffix_length += 1

    return (
        prefix_length >= 2
        and suffix_length >= _STOREFRONT_MIN_INSERTION_SUFFIX
        and prefix_length + suffix_length == len(normalized_trusted)
    )


def _storefront_variant_context_is_safe(answer: str, start: int, end: int) -> bool:
    prefix = answer[max(0, start - 12):start]
    suffix = answer[end:min(len(answer), end + 12)]
    left_boundary = start == 0 or not re.match(
        r"[㐀-鿿A-Za-z0-9]",
        answer[start - 1],
    )
    right_boundary = end == len(answer) or not re.match(
        r"[㐀-鿿A-Za-z0-9]",
        answer[end],
    )
    left_context = any(prefix.endswith(marker) for marker in _STOREFRONT_LEFT_CONTEXT)
    right_context = any(suffix.startswith(marker) for marker in _STOREFRONT_RIGHT_CONTEXT)
    return (left_boundary or left_context) and (right_boundary or right_context)


def _derived_storefront_exact_hit(
    answer: str,
    identity: BrandIdentity,
) -> tuple[str, int, int] | None:
    """Find a source-exact, bounded location-storefront display variant."""
    normalized_answer, source_positions = _normalize_with_positions(answer)
    if not normalized_answer:
        return None

    candidates: list[tuple[int, int, int, int]] = []
    for trusted_name in identity.all_trusted_names:
        normalized_trusted = normalize_brand_name(trusted_name)
        suffix = next(
            (
                normalize_brand_name(value)
                for value in _STOREFRONT_SUFFIXES
                if normalized_trusted.endswith(normalize_brand_name(value))
            ),
            "",
        )
        if not suffix:
            continue

        suffix_start = normalized_answer.find(suffix)
        while suffix_start >= 0:
            normalized_end = suffix_start + len(suffix)
            minimum_length = max(
                _STOREFRONT_MIN_VARIANT_LENGTH,
                len(normalized_trusted) - _STOREFRONT_MAX_LEADING_OMISSION,
            )
            maximum_length = len(normalized_trusted) + _STOREFRONT_MAX_INSERTION
            for candidate_length in range(minimum_length, maximum_length + 1):
                normalized_start = normalized_end - candidate_length
                if normalized_start < 0:
                    continue
                normalized_candidate = normalized_answer[normalized_start:normalized_end]
                if not _is_bounded_storefront_variant(
                    normalized_candidate,
                    normalized_trusted,
                ):
                    continue
                source_start = source_positions[normalized_start]
                source_end = source_positions[normalized_end - 1] + 1
                if not _storefront_variant_context_is_safe(
                    answer,
                    source_start,
                    source_end,
                ):
                    continue
                if _identity_hit_is_negated_or_uncertain(
                    answer,
                    source_start,
                    source_end,
                ):
                    continue
                candidates.append(
                    (
                        abs(candidate_length - len(normalized_trusted)),
                        -candidate_length,
                        source_start,
                        source_end,
                    )
                )
            suffix_start = normalized_answer.find(suffix, suffix_start + 1)

    if not candidates:
        return None
    _, _, source_start, source_end = min(candidates)
    return answer[source_start:source_end], source_start, source_end


def _is_plausible_name_form(candidate: str, identity: BrandIdentity) -> bool:
    normalized_match = normalize_brand_name(candidate)
    if len(normalized_match) < 2:
        return False
    for trusted_name in identity.all_trusted_names:
        normalized_trusted = normalize_brand_name(trusted_name)
        if normalized_match == normalized_trusted:
            return True
        if len(normalized_trusted) < 4 or len(normalized_match) != len(normalized_trusted):
            continue
        differences = sum(
            left != right
            for left, right in zip(normalized_match, normalized_trusted)
        )
        if differences == 1:
            return True
    for derived_name in _derived_legal_name_forms(identity):
        if normalized_match == normalize_brand_name(derived_name):
            return True
    return False


def _is_plausible_verified_match(matched_text: str, identity: BrandIdentity) -> bool:
    """Validate a source-exact verifier YES against trusted or legal short forms."""
    if _is_plausible_name_form(matched_text, identity):
        return True

    # Providers sometimes return a display form such as ``岱林生物（DAILIN）``.
    # The parenthetical text is presentation metadata, not independent identity
    # evidence: only the outside company name may satisfy the trusted-name check.
    # This avoids accepting ``错误主体（可信品牌）`` while preserving a source-
    # exact, uniquely anchored Chinese-name-plus-English-display match.
    if _BRACKET_RE.search(matched_text):
        outside_name = _BRACKET_RE.sub("", matched_text).strip()
        if outside_name and _is_plausible_name_form(outside_name, identity):
            return True
    return False


def _derived_legal_name_forms(identity: BrandIdentity) -> tuple[str, ...]:
    """Return conservative legal-name abbreviations for exact source matching.

    A form is derived only after removing a recognized legal entity suffix.
    The optional location-stripped form still retains the proprietary company
    core (for example ``深圳市晨光富士电梯`` -> ``晨光富士电梯``).  The forms
    remain ephemeral and are never added to or persisted as trusted aliases.
    """
    forms: list[str] = []
    for trusted_name in identity.all_trusted_names:
        cleaned = (trusted_name or "").strip()
        legal_base = ""
        legal_suffix = ""
        for suffix in _LEGAL_ENTITY_SUFFIXES:
            if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
                legal_base = cleaned[:-len(suffix)].strip()
                legal_suffix = suffix
                break
        if not legal_base:
            continue

        legal_cores = [legal_base]
        # Remove at most one controlled business descriptor. This derives legal
        # abbreviations without customer-specific mappings: ``岱林生物技术``
        # becomes ``岱林生物`` while the proprietary core remains required.
        for descriptor in _LEGAL_TRAILING_DESCRIPTORS:
            if legal_base.endswith(descriptor) and len(legal_base) > len(descriptor):
                rewritten = legal_base[:-len(descriptor)]
                if len(normalize_brand_name(rewritten)) >= 4:
                    legal_cores.append(rewritten)

        for legal_core in legal_cores:
            if len(normalize_brand_name(legal_core)) >= 4:
                forms.append(legal_core)
            for prefix in _administrative_prefix_variants():
                if legal_core.startswith(prefix):
                    without_location = legal_core[len(prefix):].strip()
                    if len(normalize_brand_name(without_location)) >= 4:
                        forms.append(without_location)
                        if legal_core == legal_base and legal_suffix:
                            forms.append(without_location + legal_suffix)
                    break
    return _dedupe_nonempty(forms)


def _without_administrative_prefix(value: str) -> str:
    for prefix in _administrative_prefix_variants():
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _legal_proprietary_core(value: str) -> str:
    """Remove known location and generic trailing business words."""
    core = normalize_brand_name(_without_administrative_prefix((value or "").strip()))
    suffixes = sorted(
        {
            *_GENERIC_SUFFIX_TOKENS,
            *_GENERIC_INDUSTRY_TERMS,
        },
        key=len,
        reverse=True,
    )
    changed = True
    while core and changed:
        changed = False
        for suffix in suffixes:
            normalized_suffix = normalize_brand_name(suffix)
            if normalized_suffix and core.endswith(normalized_suffix):
                core = core[:-len(normalized_suffix)]
                changed = True
                break
    return core


def _is_generic_only_derived_form(value: str) -> bool:
    """Return true when a legal short form contains no proprietary token."""
    core = normalize_brand_name(_without_administrative_prefix((value or "").strip()))
    if not core:
        return True

    tokens = sorted(
        {
            normalize_brand_name(token)
            for token in _GENERIC_DERIVED_NAME_TOKENS
            if normalize_brand_name(token)
        },
        key=len,
        reverse=True,
    )
    reachable = [False] * (len(core) + 1)
    reachable[0] = True
    for start in range(len(core)):
        if not reachable[start]:
            continue
        for token in tokens:
            if core.startswith(token, start):
                reachable[start + len(token)] = True
    return reachable[-1]


def _has_conflicting_adjacent_location(answer: str, start: int) -> bool:
    """Reject a stripped alias embedded in another location-qualified company."""
    prefix_text = answer[:start]
    return any(
        prefix_text.endswith(prefix)
        for prefix in _administrative_prefix_variants()
    )


def _legal_abbreviation_context_is_safe(
    answer: str,
    start: int,
    end: int,
) -> bool:
    prefix = answer[max(0, start - 12):start]
    suffix = answer[end:min(len(answer), end + 12)]
    left_boundary = start == 0 or not re.match(
        r"[\u3400-\u9fffA-Za-z0-9]",
        answer[start - 1],
    )
    right_boundary = end == len(answer) or not re.match(
        r"[\u3400-\u9fffA-Za-z0-9]",
        answer[end],
    )
    left_context = any(
        prefix.endswith(marker) for marker in _LEGAL_ABBREVIATION_LEFT_CONTEXT
    )
    right_context = any(
        suffix.startswith(marker) for marker in _LEGAL_ABBREVIATION_RIGHT_CONTEXT
    )
    return (left_boundary or left_context) and (right_boundary or right_context)


def _inside_parenthetical_text(answer: str, start: int, end: int) -> bool:
    return any(
        match.start() < start and end <= match.end()
        for match in _BRACKET_RE.finditer(answer)
    )


def _derived_legal_exact_hit(
    answer: str,
    identity: BrandIdentity,
) -> tuple[str, int, int] | None:
    """Find a conservative legal abbreviation without calling a paid verifier."""
    normalized_answer, source_positions = _normalize_with_positions(answer)
    forms = sorted(
        _derived_legal_name_forms(identity),
        key=lambda item: len(normalize_brand_name(item)),
        reverse=True,
    )
    for form in forms:
        normalized_form = normalize_brand_name(form)
        if (
            len(_legal_proprietary_core(form)) < 2
            or _is_generic_only_derived_form(form)
        ):
            continue
        normalized_start = normalized_answer.find(normalized_form)
        while normalized_start >= 0:
            normalized_end = normalized_start + len(normalized_form)
            source_start = source_positions[normalized_start]
            source_end = source_positions[normalized_end - 1] + 1
            location_stripped = normalize_brand_name(form) == normalize_brand_name(
                _without_administrative_prefix(form)
            )
            if (
                not _inside_parenthetical_text(answer, source_start, source_end)
                and not (
                    location_stripped
                    and _has_conflicting_adjacent_location(answer, source_start)
                )
                and _legal_abbreviation_context_is_safe(answer, source_start, source_end)
                and not _identity_hit_is_negated_or_uncertain(
                    answer,
                    source_start,
                    source_end,
                )
            ):
                return answer[source_start:source_end], source_start, source_end
            normalized_start = normalized_answer.find(normalized_form, normalized_start + 1)
    return None


def _diagnosis_verifier_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _DIAGNOSIS_VERIFIER_LIMITERS_LOCK:
        limiter = _DIAGNOSIS_VERIFIER_LIMITERS.get(loop)
        if limiter is None:
            limiter = asyncio.Semaphore(_DIAGNOSIS_VERIFIER_CONCURRENCY)
            _DIAGNOSIS_VERIFIER_LIMITERS[loop] = limiter
        return limiter


def _parenthetical_identity_alias(raw: str) -> str | None:
    """Return a parenthetical identity alias, never a location/legal qualifier."""
    candidate = (raw or "").strip()
    if not candidate:
        return None
    explicit_alias = bool(_ALIAS_PREFIX_RE.match(candidate))
    candidate = _ALIAS_PREFIX_RE.sub("", candidate).strip()
    normalized = normalize_brand_name(candidate)
    if not normalized:
        return None
    administrative_names = {
        normalize_brand_name(name) for name in _administrative_prefix_variants()
    }
    if normalized in administrative_names:
        return None
    # ── [返工 2026-08-06 · P1] 区/镇复合地名穿透 ──────────────────────────
    #
    # 🔴 上面那道 ``administrative_names`` 是**整串相等**判据,而它的表只到
    # 省/直辖市/主要地级市。Review 交叉重放实测:「深圳」堵住了,但
    # 「全域上榜(龙岗区平湖)科技有限公司」拆出的**「龙岗区平湖」照样进可信别名**
    # —— 同族假阳性只是从市级降到区/镇一级,没有消失。实测同族还有
    # 「深圳市南山区」「茅台镇」,都从整串相等这道闸底下走过去。
    #
    # 判据 = **行政区划结构**,不是字符级黑名单。
    #
    # 🔴🔴 第一版我写成"含 省/市/区/乡 任一字即拒",并把误伤当"残留风险"上报。
    # 复审实测直接打脸 —— 被误杀的是**六个真实品牌**,不是一个:
    #     乡村基 / 北京同仁堂 / 上海家化 / 广州酒家 / 青岛啤酒 / 重庆小面
    # 「已知真实品牌被误杀」不能当残留风险接受,那是把自己的偷懒记在客户账上。
    # "过滤过度是安全方向"说的是**同类形态之间**取严,不是允许拿真品牌陪葬。
    #
    # 结构判据:把候选按行政区划切块(``_admin_chunks``,与出题侧地名解析同源),
    # **任一块以行政后缀结尾**即判定为地址:
    #     龙岗区平湖   → ['龙岗区','平湖']   → '龙岗区' 命中 → 拒 ✅
    #     深圳市南山区 → ['深圳市','南山区'] → 命中 → 拒 ✅
    #     茅台镇       → ['茅台镇']         → 命中 → 拒 ✅
    #     乡村基       → ['乡村']           → 无块以行政后缀结尾 → 放行 ✅
    #     北京同仁堂   → ['北京同仁堂','北京','同仁堂'] → 放行 ✅
    #     上海家化 / 广州酒家 / 青岛啤酒 / 重庆小面      → 放行 ✅
    # 🔴 后缀表**不含「村」**:含了「乡村基」会被切成 '乡村' 再命中 —— 这一格
    # 就是"结构判据也能写歪"的证据,所以它单独有一条反向对照锁。
    if not explicit_alias and _looks_like_administrative_address(candidate):
        return None
    if any(
        normalized.endswith(normalize_brand_name(suffix))
        for suffix in _PARENTHETICAL_QUALIFIER_SUFFIXES
    ) and not explicit_alias:
        return None
    if (
        normalized in _GENERIC_SUFFIX_TOKENS
        or normalized in _GENERIC_INDUSTRY_TERMS
        or len(_legal_proprietary_core(candidate)) < 2
        or _is_generic_only_derived_form(candidate)
    ) and not explicit_alias:
        return None
    return candidate


def split_brand_aliases(value: str) -> tuple[str, ...]:
    """Return the full/outside identity plus genuine parenthetical aliases."""
    if not value or not value.strip():
        return ()
    raw_value = value.strip()
    bracket_aliases = [
        alias
        for match in _BRACKET_RE.finditer(raw_value)
        if (alias := _parenthetical_identity_alias(match.group(1)))
    ]
    outside = _BRACKET_RE.sub("", value).strip()
    return _dedupe_nonempty([raw_value, outside, *bracket_aliases])


def _parse_display_names(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return _dedupe_nonempty(str(item) for item in raw)
    text = str(raw).strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return _dedupe_nonempty(str(item) for item in parsed)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return _dedupe_nonempty(re.split(r"[,，;；\n]+", text))


def parse_brand_display_names(raw: object) -> tuple[str, ...]:
    """Read the existing JSON/list/plain-text storage forms without guessing."""
    return _parse_display_names(raw)


def load_brand_identity(
    brand_id: int | None,
    *,
    fallback_name: str = "",
    fallback_display_names: Sequence[str] | None = None,
) -> BrandIdentity:
    """Load trusted identity from one brand id; fail closed on alias provenance.

    When a new diagnosis has not persisted a brand yet, only explicit request
    names are used.  No global alias lookup is attempted without ``brand_id``.
    """
    # Persisted brands are the authority. Request text is trusted only for the
    # pre-persistence diagnosis path where no brand_id exists yet.
    canonical: list[str] = [] if brand_id is not None else [fallback_name]
    if brand_id is None:
        canonical.extend(fallback_display_names or [])
    trusted_aliases: list[str] = []
    rejected_aliases: list[str] = []
    industry = ""
    load_error = False

    if brand_id is not None:
        conn = None
        try:
            from db.connection import get_connection

            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT * FROM brands WHERE id = %s", (int(brand_id),))
            row = cur.fetchone()
            if row:
                row = dict(row)
                canonical.extend([row.get("name") or "", row.get("company_name") or ""])
                canonical.extend(_parse_display_names(row.get("brand_display_names")))
                # [WO_267] industry_category 新写入是英文大类 key —— 下面「短中文别名等于行业词就不算命中」
                #   的守卫要的是**中文**行业词,所以翻成大类中文名(存量旧中文名照样翻)。
                industry = (display_name(row.get("industry_category")) or row.get("industry") or "").strip()
            else:
                load_error = True
            cur.execute(
                """
                SELECT canonical_name, alias
                FROM brand_aliases
                WHERE brand_id = %s AND source = 'manual'
                ORDER BY id
                """,
                (int(brand_id),),
            )
            for alias_row in cur.fetchall():
                alias_row = dict(alias_row)
                trusted_aliases.extend(
                    [alias_row.get("canonical_name") or "", alias_row.get("alias") or ""]
                )
            cur.execute("SELECT pg_catalog.to_regclass('public.monitoring_identity_name_decisions') AS rel")
            if cur.fetchone().get("rel"):
                cur.execute(
                    """
                    SELECT display_name
                    FROM public.monitoring_identity_name_decisions
                    WHERE brand_id = %s AND decision = 'negative'
                    ORDER BY normalized_name
                    """,
                    (int(brand_id),),
                )
                rejected_aliases.extend(
                    (dict(item).get("display_name") or "") for item in cur.fetchall()
                )
        except Exception:
            # A trusted-name read failure must not broaden matching.  The explicit
            # request name remains available; caller-side verifier failures remain
            # UNKNOWN and are never converted to a negative result.
            logger.exception("brand identity read failed brand_id=%s", brand_id)
            load_error = True
        finally:
            if conn is not None:
                conn.close()

    return BrandIdentity(
        brand_id=brand_id,
        canonical_names=_dedupe_nonempty(canonical),
        trusted_aliases=_dedupe_nonempty(trusted_aliases),
        rejected_aliases=_dedupe_nonempty(rejected_aliases),
        industry=industry,
        load_error=load_error,
    )


#: 「工商名称矫正」话术标记。来自 2026-08-03 生产真实样本(诊断 513 · 深圳市弘匠数科科技有限公司):
#:   · deepseek「您询问的公司名称是"…数科…",而搜索结果显示的是"…数字…"」
#:   · doubao 「深圳市盈匠数字科技有限公司(你说的"弘匠数科",**工商标准名称**)」
#:   · deepseek「查询到的企业名称是"…数字…"(**而非**"弘匠数科"),这**很可能就是您所指的**那家公司」
#: 这类回答里 AI **确实认出了这个品牌**,只是改用了工商登记全名 —— 判「未提到」是丢分。
REGISTRY_CORRECTION_MARKERS: Final = (
    "工商标准名称",
    "工商登记名称",
    "工商注册名称",
    "工商全称",
    "标准名称是",
    "实际注册名",
    "注册名称为",
    "名称纠正",
    "名称更正",
    "而非",
    # 下面三条来自 Q1/deepseek 那条真实样本 —— 第一版标记集**漏了它**,
    # 被自己的锁当场抓出来(锁用的是生产原文,编的样本抓不到这个漏)。
    "搜索结果显示的是",
    "查询到的企业名称",
    "您询问的公司名称",
    "您所指的",
    "你所指的",
    "您指的",
    "你指的",
    "没有查到",
    "未查到",
)


def detect_registry_name_correction(answer: str, identity: BrandIdentity) -> bool:
    """答案是否属于「我方名称 + 工商名称矫正」场景。

    [工单 2026-08-03 ②] 存在的理由(生产实证):这三条样本的**确定性层判得是对的** ——
    `resolve_local` 返回 UNKNOWN/`local_evidence_requires_review`(该进待确认队列),
    但 LLM 复核层把它翻成 NO 直接丢分。同一场景下 Q6/doubao 却判 YES,
    **跨引擎不一致本身就是"这需要人来确认"的证据**。

    判据要两个条件**同时**成立,单独任一条都不够:
      ① 我方名称 token 确实出现在答案里(由调用方的 window_spans 非空保证,不在本函数重复算);
      ② 答案里出现工商矫正话术标记。
    只有 ② 而没有 ① 时,"而非""没有查到"这类词在任何否定性回答里都常见 —— 会把正常的
    「真没提到」大批误收进待确认队列。所以本函数只负责 ②,① 由调用点把关。
    """
    text = str(answer or "")
    if not text:
        return False
    return any(marker in text for marker in REGISTRY_CORRECTION_MARKERS)


def _candidate_tokens(identity: BrandIdentity) -> tuple[str, ...]:
    tokens: list[str] = []
    administrative_tokens = {
        normalize_brand_name(name) for name in _administrative_prefix_variants()
    }
    for alias in identity.all_trusted_names:
        normalized = normalize_brand_name(alias)
        if len(normalized) < 2:
            continue
        # Prefer distinctive adjacent pairs/triples.  Generic suffixes alone are
        # discarded, but proprietary prefixes such as 雅栖 / 观光 still survive.
        width = 3 if len(normalized) >= 5 else 2
        for size in (width, 2):
            for index in range(0, len(normalized) - size + 1):
                token = normalized[index:index + size]
                if token in _GENERIC_SUFFIX_TOKENS or token in administrative_tokens:
                    continue
                tokens.append(token)
    return _dedupe_nonempty(tokens)


def build_evidence_window_spans(
    answer: str,
    identity: BrandIdentity,
    *,
    radius: int = 420,
) -> tuple[EvidenceWindow, ...]:
    """Collect every candidate-nearby window and merge overlaps.

    There is deliberately no first-N cutoff.  The complete answer is scanned;
    only bounded context around identity-like tokens is sent to the verifier.
    """
    if not answer:
        return ()
    normalized_answer = normalize_brand_name(answer)
    if not normalized_answer:
        return ()

    # Normalization only removes characters, so positions remain monotonic but
    # are not byte-identical.  Search the raw case-folded text first, then fall
    # back to normalized chunks when punctuation separated a token.
    folded = answer.casefold()
    ranges: list[tuple[int, int]] = []
    for token in _candidate_tokens(identity):
        start_at = 0
        while True:
            pos = folded.find(token, start_at)
            if pos < 0:
                break
            ranges.append((max(0, pos - radius), min(len(answer), pos + len(token) + radius)))
            start_at = pos + max(1, len(token))

    if not ranges:
        # A normalized-only hit can happen with spaces/punctuation inside a name.
        # Use full bounded chunks so later content is not silently ignored.
        if any(token in normalized_answer for token in _candidate_tokens(identity)):
            return tuple(
                EvidenceWindow(answer[i:i + radius * 2], i, min(len(answer), i + radius * 2))
                for i in range(0, len(answer), radius * 2)
            )
        return ()

    ranges.sort()
    merged: list[list[int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple(EvidenceWindow(answer[start:end], start, end) for start, end in merged)


def build_evidence_windows(
    answer: str,
    identity: BrandIdentity,
    *,
    radius: int = 420,
) -> tuple[str, ...]:
    """Compatibility view for verifiers that accept bounded window strings."""
    return tuple(
        window.text
        for window in build_evidence_window_spans(answer, identity, radius=radius)
    )


def _parse_structured_verifier_response(raw: str) -> VerificationResult:
    """Parse one verifier response without trusting markdown/prose wrappers."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()

    parsed = None
    try:
        candidate = json.loads(text)
        if isinstance(candidate, dict):
            parsed = candidate
    except json.JSONDecodeError:
        pass

    if parsed is None:
        decoder = json.JSONDecoder()
        for offset, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    if parsed is None:
        raise ValueError("verifier_response_not_json_object")

    verdict = BrandVerdict(str(parsed.get("verdict") or "").upper())
    matched_text = str(parsed.get("matched_text") or "").strip()[:120] or None
    return VerificationResult(
        verdict,
        str(parsed.get("reason") or "")[:120],
        matched_text=matched_text,
        window_index=_safe_optional_int(parsed.get("window_index")),
        matched_start=_safe_optional_int(parsed.get("matched_start")),
        matched_end=_safe_optional_int(parsed.get("matched_end")),
    )


async def _default_structured_verifier(
    *,
    identity: BrandIdentity,
    evidence_windows: Sequence[str],
) -> VerificationResult:
    """Use the official DeepSeek verifier shared with GEO observation.

    The old path called a DashScope-compatible endpoint with hidden reasoning
    enabled and retried malformed/unknown results.  That split implementation
    caused valid answers to be excluded.  Diagnosis now shares the official,
    strict transport while mapping paid-response uncertainty to UNKNOWN exactly
    once instead of issuing a second potentially billable request.
    """
    if not evidence_windows:
        return VerificationResult(BrandVerdict.UNKNOWN, "no_evidence")

    from services.geo_observation.entity_review import (
        LeaseLost,
        ProviderResultUnknown,
        make_official_verifier,
    )

    verifier = make_official_verifier(
        timeout=30.0,
        call_purpose="diagnosis_brand_identity",
    )
    try:
        async with _diagnosis_verifier_limiter():
            return await verifier(identity=identity, evidence_windows=evidence_windows)
    except ProviderResultUnknown:
        logger.warning(
            "official brand verifier result unknown brand_id=%s windows=%d",
            identity.brand_id,
            len(evidence_windows),
        )
        return VerificationResult(BrandVerdict.UNKNOWN, "provider_result_unknown")
    except LeaseLost:
        # Diagnosis does not pass a lease guard, but fail closed if a future
        # caller supplies one through the shared verifier contract.
        return VerificationResult(BrandVerdict.UNKNOWN, "lease_lost")


def _validated_source_span(
    matched_text: str,
    verification: VerificationResult,
    windows: Sequence[EvidenceWindow],
) -> tuple[int, int] | None:
    """Map verifier evidence to one exact source occurrence without global guessing."""
    if not matched_text:
        return None

    def _unique_span_in_window(window: EvidenceWindow) -> tuple[int, int] | None:
        positions: list[int] = []
        offset = 0
        while True:
            position = window.text.find(matched_text, offset)
            if position < 0:
                break
            positions.append(position)
            offset = position + max(1, len(matched_text))
        if len(positions) != 1:
            return None
        position = positions[0]
        return (
            window.source_start + position,
            window.source_start + position + len(matched_text),
        )

    if (
        verification.window_index is not None
        and verification.matched_start is not None
        and verification.matched_end is not None
    ):
        # The prompt numbers windows from 1, but real provider responses have
        # occasionally returned 0 for the first window. Accept that single
        # compatibility case; all other indices remain one-based.
        window_index = 0 if verification.window_index == 0 else verification.window_index - 1
        if window_index < 0 or window_index >= len(windows):
            return None
        window = windows[window_index]
        start = verification.matched_start
        end = verification.matched_end
        if (
            start >= 0
            and end > start
            and end <= len(window.text)
            and window.text[start:end] == matched_text
        ):
            return window.source_start + start, window.source_start + end

        # Character offsets are advisory, not identity evidence. If the model
        # selected a valid window but miscounted Unicode positions, re-anchor
        # only when the exact returned text occurs once in that same window.
        # This preserves source provenance without guessing between duplicates.
        return _unique_span_in_window(window)

    # Compatibility for deterministic test verifiers and old adapters: accept
    # only a unique occurrence across the exact evidence windows. Ambiguity is
    # UNKNOWN; selecting the first occurrence would corrupt recommendation rank.
    candidates: set[tuple[int, int]] = set()
    for window in windows:
        offset = 0
        while True:
            position = window.text.find(matched_text, offset)
            if position < 0:
                break
            candidates.add(
                (
                    window.source_start + position,
                    window.source_start + position + len(matched_text),
                )
            )
            offset = position + max(1, len(matched_text))
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


class BrandIdentityResolver:
    def __init__(self, identity: BrandIdentity, *, verifier: Verifier | None = None):
        self.identity = identity
        self._verifier = verifier or _default_structured_verifier

    @classmethod
    def for_brand(
        cls,
        brand_id: int | None,
        *,
        fallback_name: str = "",
        fallback_display_names: Sequence[str] | None = None,
        verifier: Verifier | None = None,
    ) -> "BrandIdentityResolver":
        return cls(
            load_brand_identity(
                brand_id,
                fallback_name=fallback_name,
                fallback_display_names=fallback_display_names,
            ),
            verifier=verifier,
        )

    def resolve_local(self, answer: str) -> BrandDecision:
        """Resolve deterministic facts only; this method never calls a provider."""
        if self.identity.load_error:
            return BrandDecision(BrandVerdict.UNKNOWN, "identity_load_failed", "deterministic")
        if not answer or not answer.strip() or not self.identity.all_trusted_names:
            return BrandDecision(BrandVerdict.NO, "empty_answer_or_identity", "deterministic")

        trusted_normalized = {
            normalize_brand_name(value) for value in self.identity.all_trusted_names if value
        }
        rejected_normalized = {
            normalize_brand_name(value) for value in self.identity.rejected_aliases if value
        }
        if trusted_normalized & rejected_normalized:
            return BrandDecision(BrandVerdict.UNKNOWN, "identity_decision_conflict", "deterministic")

        for alias in self.identity.all_trusted_names:
            exact_hit = _trusted_exact_hit(answer, alias, self.identity)
            if exact_hit:
                matched_text, matched_start, matched_end = exact_hit
                return BrandDecision(
                    BrandVerdict.YES,
                    "trusted_exact_alias",
                    "trusted_exact",
                    matched_alias=matched_text,
                    matched_start=matched_start,
                    matched_end=matched_end,
                )

        legal_extension_hit = _trusted_legal_suffix_extension_hit(answer, self.identity)
        if legal_extension_hit:
            matched_text, matched_start, matched_end = legal_extension_hit
            return BrandDecision(
                BrandVerdict.YES,
                "trusted_name_with_legal_suffix",
                "trusted_legal_suffix_exact",
                matched_alias=matched_text,
                matched_start=matched_start,
                matched_end=matched_end,
            )

        for alias in self.identity.rejected_aliases:
            negative_hit = _human_rejected_exact_hit(answer, alias)
            if negative_hit:
                matched_text, matched_start, matched_end = negative_hit
                return BrandDecision(
                    BrandVerdict.NO,
                    "human_rejected_name",
                    "human_negative_exact",
                    matched_alias=matched_text,
                    matched_start=matched_start,
                    matched_end=matched_end,
                )

        storefront_hit = _derived_storefront_exact_hit(answer, self.identity)
        if storefront_hit:
            matched_text, matched_start, matched_end = storefront_hit
            return BrandDecision(
                BrandVerdict.YES,
                "bounded_storefront_variant",
                "derived_storefront_exact",
                matched_alias=matched_text,
                matched_start=matched_start,
                matched_end=matched_end,
            )

        legal_hit = _derived_legal_exact_hit(answer, self.identity)
        if legal_hit:
            matched_text, matched_start, matched_end = legal_hit
            return BrandDecision(
                BrandVerdict.YES,
                "conservative_legal_abbreviation",
                "derived_legal_exact",
                matched_alias=matched_text,
                matched_start=matched_start,
                matched_end=matched_end,
            )

        window_spans = build_evidence_window_spans(answer, self.identity)
        windows = tuple(window.text for window in window_spans)
        if not window_spans:
            return BrandDecision(BrandVerdict.NO, "no_identity_candidate", "deterministic")

        return BrandDecision(
            BrandVerdict.UNKNOWN,
            "local_evidence_requires_review",
            "deterministic_local",
            evidence_snippet=window_spans[0].text,
            # 🔴 [WO_239-乙 2026-09-18] 原来只给 `evidence_snippet`(原文),
            #    **不给任何可点的名字** ⇒ 待确认卡片零候选可点。
            #    而名字本来就算出来了:`window_spans` 是证据窗口,上一行刚 guard 过非空,
            #    `.text` 就是我方名称在答案里出现的那个写法。
            #    生产 14 条诊断(其中 12 条真客户 + 有分享链接)卡在这个 reason 上。
            #
            #    🔴 填 `near_miss_alias` 而**不是** `matched_alias` ——
            #    本文件 :85-88 逐字:`matched_alias` 意思是「本品牌就是以这个写法被提到的」,
            #    **YES 时下游据此算位置/推荐档**,混用会按位置自动升格成「明确推荐」,
            #    那正是 08-05 城市别名 P0 的放大链路。这里 verdict 是 UNKNOWN(待确认),
            #    只能用「长得像,等人确认」那一档。
            #
            #    不需要 :1671 那种 span 守卫:那里防的是复核层**编**一个原文没有的名字,
            #    而 `window_spans` 本身就是从答案里切出来的,不存在幻觉。
            near_miss_alias=window_spans[0].text,
        )

    async def resolve(self, answer: str) -> BrandDecision:
        local = self.resolve_local(answer)
        if local.verdict is not BrandVerdict.UNKNOWN or local.reason != "local_evidence_requires_review":
            return local

        window_spans = build_evidence_window_spans(answer, self.identity)
        windows = tuple(window.text for window in window_spans)

        try:
            verification = await self._verifier(
                identity=self.identity,
                evidence_windows=windows,
            )
        except (TimeoutError, httpx.TimeoutException):
            verification = VerificationResult(BrandVerdict.UNKNOWN, "timeout")
        except Exception as exc:
            logger.warning(
                "brand verifier callback failed brand_id=%s error=%s",
                self.identity.brand_id,
                type(exc).__name__,
            )
            verification = VerificationResult(BrandVerdict.UNKNOWN, "verifier_exception")

        if verification.verdict is BrandVerdict.YES:
            matched_text = (verification.matched_text or "").strip()
            source_span = _validated_source_span(
                matched_text,
                verification,
                window_spans,
            )
            if source_span is None or not _is_plausible_verified_match(
                matched_text,
                self.identity,
            ):
                # [WO 2026-08-06 §1] 判定照旧 UNKNOWN(**不放宽**可信契约),
                # 但把复核层交回的原文候选带出去 —— 它是这一格唯一的、且源文
                # 逐字存在的身份线索。丢掉它 = 待确认卡片零候选可点(561 实证)。
                # 🔴 只在 span 有效(候选确实出现在证据窗口里)时才带:span 为 None
                # 说明复核层编了一个原文没有的名字,那是幻觉不是变体。
                return BrandDecision(
                    BrandVerdict.UNKNOWN,
                    "invalid_matched_text",
                    "deepseek_v4_flash_structured",
                    evidence_snippet=local.evidence_snippet,
                    near_miss_alias=(matched_text if source_span is not None else None),
                )
            matched_start, matched_end = source_span
            return BrandDecision(
                BrandVerdict.YES,
                verification.reason,
                "deepseek_v4_flash_structured",
                matched_alias=matched_text,
                matched_start=matched_start,
                matched_end=matched_end,
                evidence_snippet=next(
                    (
                        window.text
                        for window in window_spans
                        if window.source_start <= matched_start < window.source_end
                    ),
                    None,
                ),
            )

        matched_text = (verification.matched_text or "").strip()
        source_span = _validated_source_span(matched_text, verification, window_spans)
        if (
            verification.verdict is BrandVerdict.UNKNOWN
            and source_span is not None
            and _is_plausible_verified_match(matched_text, self.identity)
        ):
            matched_start, matched_end = source_span
            return BrandDecision(
                BrandVerdict.UNKNOWN,
                verification.reason or "ambiguous_identity_candidate",
                "deepseek_v4_flash_structured",
                matched_alias=matched_text,
                matched_start=matched_start,
                matched_end=matched_end,
                evidence_snippet=next(
                    (
                        window.text
                        for window in window_spans
                        if window.source_start <= matched_start < window.source_end
                    ),
                    local.evidence_snippet,
                ),
            )

        # [工单 2026-08-03 ②] 「工商名称矫正」不得直接丢分,应回到「待确认」队列。
        #
        # 生产实证(诊断 513 · 深圳市弘匠数科科技有限公司,8 题 × 4 引擎):
        #   3 条被判 NO 的样本,`resolve_local` 全部返回 UNKNOWN/local_evidence_requires_review
        #   —— 确定性层判得对,是 LLM 复核层翻成 NO。而同一场景 Q6/doubao 判 YES,
        #   **跨引擎不一致**恰恰说明这不是机器该拍板的事。
        #
        # 闸的两个条件缺一不可(见 detect_registry_name_correction 文档):
        #   ① `window_spans` 非空 = 我方名称确实出现在答案里(不是凭话术词就收);
        #   ② 答案带工商矫正话术。
        # 命中则降级为 UNKNOWN 并**带上证据窗口** —— 待确认卡片要有原文才能让人判。
        # 🔴 只对 NO 生效:YES/UNKNOWN 分支在上面已各自返回,不受影响。
        if (
            verification.verdict is BrandVerdict.NO
            and window_spans
            and detect_registry_name_correction(answer, self.identity)
        ):
            return BrandDecision(
                BrandVerdict.UNKNOWN,
                "registry_name_correction_requires_review",
                "deterministic_local",
                evidence_snippet=local.evidence_snippet or window_spans[0].text,
                # 🔴 [WO_239-乙 2026-09-18] 与上面 `local_evidence_requires_review` 同一缺陷:
                #    这道闸自己的注释写着「命中则降级为 UNKNOWN 并**带上证据窗口** ——
                #    待确认卡片要有原文才能让人判」,而它只给了原文、**没给可点的名字**。
                #    #727 广东星衍朗(真客户)落库 `identity_candidates: []` 就是这么来的;
                #    生产 61 条诊断卡在这个 reason 上,其中 48 条真客户 + 有分享链接
                #    —— 是本类里量最大的一处。
                #
                #    这里用 `window_spans[0].text` 而不是 `local.evidence_snippet`:
                #    闸的成立条件 ① 就是 `window_spans` 非空 =「我方名称确实出现在答案里」,
                #    所以 span 的文本一定是答案里的真实写法;`evidence_snippet` 可能来自
                #    别处、不保证是一个"名字"。
                #
                #    🔴 同样填 `near_miss_alias` 不是 `matched_alias`(理由见上一处)。
                near_miss_alias=window_spans[0].text,
            )

        return BrandDecision(
            verification.verdict,
            verification.reason,
            "deepseek_v4_flash_structured",
            evidence_snippet=local.evidence_snippet,
        )
