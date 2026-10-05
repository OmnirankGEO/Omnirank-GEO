"""Purchased-keyword to generated-title semantic alignment.

The commercial keyword identity is authoritative. This module deliberately
does not require every title to copy the whole keyword verbatim: a natural
abbreviation or paraphrase may pass when it still preserves enough of the
specific business object. Generic industry, brand-only, or adjacent-intent
headlines fail closed and are replaced by the caller's keyword-anchored safe
fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata


#: [WO_232 2026-09-17] v2 → v3:新增「公司注册名受控缩写」一路(见文件下方说明)。
#: 版本号落进 topic.title_alignment_version,事后归因时能分清是哪一版判的。
TITLE_KEYWORD_ALIGNMENT_VERSION = "purchased-keyword-title-alignment-v3"

# These are purchase/question suffixes, not business nouns. Removal is
# suffix-only so a legitimate term containing (for example) "公司" is not
# damaged in the middle of a product name.
_PURCHASE_INTENT_SUFFIX_RE = re.compile(
    r"(?:"
    r"哪家(?:好|靠谱)?|哪个好|怎么选|如何选择|如何选|"
    # Only remove the recommendation intent. ``公司/厂家/服务商`` can be
    # the purchased object itself (for example ``深圳装修公司推荐``), so
    # stripping the whole phrase would silently broaden the contract.
    # ``品牌`` is an intent dimension rather than the underlying product,
    # so ``板材品牌推荐`` may still naturally become ``板材怎么选``.
    r"品牌推荐|推荐|"
    # [Review-CTO 2026-07-23 P2 · SSOT §4.4] 排名/榜单/TOP/前十是**合法**商业
    # 方向,必须保留购买意图(不再从 anchor 剥离,否则"律师事务所排名"被泛化);
    # 绝对化名次(第一名/榜首)由法律硬门在标题+正文统一处理。
    r"价格多少|多少钱|费用多少|报价|收费标准|价格|费用|"
    r"怎么样|靠谱吗|好不好"
    r")[?？!！。]*$",
    re.IGNORECASE,
)
_DECORATION_RE = re.compile(r"20\d{2}\s*年?")
_NON_SEMANTIC_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)
_SYMBOL_REPLACEMENTS = (
    ("++", "plusplus"),
    ("+", "plus"),
    ("#", "sharp"),
    ("/", "slash"),
    ("\\", "backslash"),
)

# Explicit, reviewable aliases only. These are not fuzzy synonyms: every
# entry is a well-established short form for the same purchased object.
# New aliases require a regression fixture and a version bump.
_CONTROLLED_OBJECT_ALIASES = (
    ("载货电梯", ("货梯",)),
)

# ---------------------------------------------------------------------------
# [WO_232 2026-09-17] 公司注册名关键词的受控缩写
#
# 真客户品牌 995「广东星衍朗科技有限公司」买的就是自己的**完整注册名**。AI 写标题时
# 很自然地缩写它(「广东星衍朗科技怎么样?」/「星衍朗科技有限公司靠谱吗?」),
# 而下面的 replace 守卫是为「医用→家用」这类**换限定词**设计的 —— 它把
# 「丢掉 有限公司」也读成 replace,于是这类关键词**整梯全拒**,7 个槽位失败 5 个。
#
# 🔴 修法边界(工单 §3.1 原话:受控别名表,不是放宽 replace 规则):
#   · 只有当**关键词核逐字等于该品牌的注册名**时才展开缩写 —— 这一条把放宽
#     锁死在"这家公司自己的名字"范围内,任何别的关键词一个字都不受影响;
#   · replace 守卫本身**一行没动**:医用→家用 / 工业→服务 / 乘客→载货 仍然 FAIL;
#   · 缩写只做**删除**(去行政区划前缀、去组织形式/行业后缀),不做任何替换。
#
# 行政区划前缀两种认法:省/直辖市/自治区是**有界名单**;市/州/地区靠结构标记
# (「深圳市」带「市」字),不靠枚举全国城市 —— 城市是开集合,枚举必漏。
_ADMIN_DIVISION_PREFIXES = (
    "黑龙江", "内蒙古", "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁",
    "吉林", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北",
    "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "台湾", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
)
_ADMIN_DIVISION_TAIL_RE = re.compile(r"^[一-鿿]{2,4}?(?:市|自治州|地区)")
#: 组织形式后缀,**从长到短**匹配(「股份有限公司」要先于「有限公司」命中)。
_ORG_FORM_SUFFIXES = (
    "股份有限公司", "有限责任公司", "集团有限公司", "有限公司", "集团公司",
    "分公司", "集团", "公司",
)
#: 🔴 **行业词后缀不剥。** 工单 §3.1 的后缀清单里写了「科技」,我照做之后被自己的
#:   判据抓住:剥掉行业词只剩字号(「广东星衍朗」),而它是
#:   **「广东星衍朗贸易有限公司」的前缀** —— 那是另一家注册公司。
#:   于是一条讲别家公司的标题会被判成"同一商业对象"放行。
#:   行政区划 + 字号 + **行业** 合起来才唯一,只留字号就不唯一了。
#:   ⇒ 只剥行政区划前缀与组织形式后缀,这两者不参与区分两家公司。
#:   (代价:标题只写字号「星衍朗怎么样」会 FAIL。可接受 —— 这一路 fail-closed,
#:    而且重试 prompt 现在会要模型原样带上完整关键词。)
#: 缩写的下限。低于它的串太短,会在无关标题里随机命中。
_MIN_COMPANY_VARIANT_LEN = 3


# [WO_232] 判为"没对齐"时的 reason 分两类,上层据此区分**两种完全不同的失败**:
#   · NOT_GENERATED —— 模型根本没给出标题。重试/换模型有用,编辑标题也有用。
#   · ALIGNMENT_REJECTION —— 模型给了,是复核不收。重新生成大概率还是被拒,
#     真正管用的是"保留完整关键词"或手动编辑。
# 🔴 把 `empty_title` 当成复核拒绝会把「AI 全死」说成「标题没保留关键词」——
#    本包第一版就是这么错的,被既有判据 G1(LLM 全死)当场抓住。
NOT_GENERATED_REASONS = frozenset({"empty_title", "empty_purchased_keyword"})
ALIGNMENT_REJECTION_REASONS = frozenset({
    "symbol_identity_drift",
    "conflicting_business_qualifier",
    "keyword_scope_or_object_drift",
    "business_object_drift",
})


@dataclass(frozen=True)
class TitleKeywordAlignment:
    """Deterministic alignment verdict used at the persistence boundary."""

    aligned: bool
    reason: str
    coverage: float
    longest_anchor: int
    version: str = TITLE_KEYWORD_ALIGNMENT_VERSION


def normalize_semantic_text(value: object) -> str:
    """Normalize visible text while preserving identity-bearing symbols.

    ``C++``, ``C#`` and ``A/B`` must not collapse to ``C`` or ``AB`` at the
    commercial identity boundary. Symbols are therefore encoded before the
    generic punctuation pass instead of being discarded.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = _DECORATION_RE.sub("", text)
    for symbol, word in _SYMBOL_REPLACEMENTS:
        text = text.replace(symbol, word)
    return _NON_SEMANTIC_RE.sub("", text)


def _identity_symbols(value: object) -> frozenset[str]:
    """Return identity-bearing symbols that a generated title must preserve."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    markers = set()
    if "++" in text:
        markers.add("plusplus")
        text = text.replace("++", "")
    if "+" in text:
        markers.add("plus")
    if "#" in text:
        markers.add("sharp")
    if "/" in text:
        markers.add("slash")
    if "\\" in text:
        markers.add("backslash")
    return frozenset(markers)


def _keyword_core(keyword: object) -> str:
    """Remove only trailing purchase-language while preserving the object."""

    text = title_anchor_from_purchased_keyword(keyword).lower()
    text = _DECORATION_RE.sub("", text)
    return _NON_SEMANTIC_RE.sub("", text)


#: [标题自然化包③ · 2026-08-01] 问句式关键词的**疑问外壳**。
#:
#: 病 A:"深圳哪家装修公司靠谱"是一个完整问句,被当名词短语整串塞进模板 →
#: "深圳哪家装修公司靠谱怎么选?" / "走近深圳哪家装修公司靠谱" 语法坏死。
#: 客户买的商业对象是「深圳装修公司」,"哪家…靠谱"只是他提问的外壳。
#: 剥掉外壳 = 拿回可组句的名词核心;红线(地区/产品/采购意图不得更换)由下面的
#: 语义对齐器**逐条复核**,剥完对不上就整串回退 —— 不赌。
#: 🔴 只收**量词型**疑问词(直接修饰后面那个名词的),刻意**不含「哪里/哪裡」**。
#: 实测反例:「揭阳120平三房买哪里好」里的「哪里」是宾语不是量词修饰,
#: 剥掉会得到「揭阳120平三房买好」这种语法垃圾 —— 比不剥更糟。
#: 判断标准是"剥完还能不能当名词短语用",不是"是不是疑问词"。
_QUESTION_SHELL_LEAD_RE = re.compile(
    r"(?:哪家|哪個|哪个|哪些|哪种|哪種|哪款|什么样的|什麼樣的|怎样的|怎樣的)"
)
_QUESTION_SHELL_TAIL_RE = re.compile(
    r"(?:比较好|比較好|好不好|好用吗|好用嗎|靠谱吗|靠譜嗎|靠谱|靠譜|"
    r"怎么样|怎麼樣|怎样|怎樣|如何|好吗|好嗎|值得吗|值得嗎)$"
)


def title_anchor_from_purchased_keyword(keyword: object) -> str:
    """Return a readable source anchor without purchase/ranking suffixes.

    [包③ 2026-08-01] 追加一层**问句外壳压缩**:关键词本身是问句时,先剥疑问壳
    拿到名词核心,再用 `assess_title_keyword_alignment` 复核身份没丢;
    对不上就原样返回。**红线一字未改** —— 变的只是"整串逐字"这个错误执行口径。
    """

    original = unicodedata.normalize("NFKC", str(keyword or "")).strip()
    text = _DECORATION_RE.sub("", original).strip()
    previous = None
    while text and text != previous:
        previous = text
        text = _PURCHASE_INTENT_SUFFIX_RE.sub("", text).strip()
    anchor = text.strip(" |｜丨:：—-，,。?？!！") or original

    compressed = _QUESTION_SHELL_TAIL_RE.sub("", _QUESTION_SHELL_LEAD_RE.sub("", anchor)).strip()
    compressed = compressed.strip(" |｜丨:：—-，,。?？!！")
    if compressed and compressed != anchor and len(compressed) >= 3:
        # 🔴 这里**不能**调 `assess_title_keyword_alignment` 做复核:
        # 对齐器内部走 `_keyword_core()` → 又回调本函数 → 无限递归
        # (第一版就是这么写的,整批锁 RecursionError)。
        #
        # 改用**非递归的等价守卫**,安全性来自两点:
        #   ① 压缩是**纯删除**(两条正则只做 sub 成空串),不会替换成别的词 ——
        #      而对齐器判"换了商业对象"靠的正是 replace 类 opcode,纯删除天然不触发;
        #   ② 被删的只可能是疑问壳(量词型疑问词 / 句尾评价词),名单里没有任何
        #      业务限定词(医用/工业/载货 这类一个都不在),所以删不掉业务身份。
        # 再加一道:数字/拉丁等强身份符号一个都不许丢(「120平」「TOP10」)。
        if _identity_symbols(anchor).issubset(_identity_symbols(compressed)):
            return compressed
    return anchor


def _controlled_core_variants(core: str) -> tuple[str, ...]:
    """Expand a canonical core into explicitly approved equivalent forms."""

    variants = [core]
    for canonical, aliases in _CONTROLLED_OBJECT_ALIASES:
        normalized_canonical = normalize_semantic_text(canonical)
        normalized_aliases = tuple(normalize_semantic_text(alias) for alias in aliases)
        if normalized_canonical in core:
            variants.extend(
                core.replace(normalized_canonical, alias)
                for alias in normalized_aliases
                if alias
            )
        for alias in normalized_aliases:
            if alias and alias in core:
                variants.append(core.replace(alias, normalized_canonical))
    return tuple(dict.fromkeys(variant for variant in variants if variant))


def _strip_admin_division(name: str) -> str:
    """去掉一个行政区划前缀。省/直辖市/自治区查有界名单;市/州/地区按结构标记。"""
    for province in sorted(_ADMIN_DIVISION_PREFIXES, key=len, reverse=True):
        for form in (province + "省", province + "市", province):
            if name.startswith(form) and len(name) - len(form) >= _MIN_COMPANY_VARIANT_LEN:
                return name[len(form):]
    match = _ADMIN_DIVISION_TAIL_RE.match(name)
    if match and len(name) - match.end() >= _MIN_COMPANY_VARIANT_LEN:
        return name[match.end():]
    return name


def _strip_one_suffix(name: str, suffixes: tuple) -> str:
    for suffix in sorted(suffixes, key=len, reverse=True):
        if name.endswith(suffix) and len(name) - len(suffix) >= _MIN_COMPANY_VARIANT_LEN:
            return name[: -len(suffix)]
    return name


def company_name_variants(name: object) -> tuple:
    """一个公司注册名的**受控缩写全集**(含它自己),已归一化。

    [WO_232] 只做删除:去行政区划前缀 × 去组织形式后缀。
    删除天然不会把「医用」变成「家用」—— 换限定词那一路一个字都没放宽。
    🔴 **不剥行业词**(理由见 `_ORG_FORM_SUFFIXES` 上方那段:剥了会撞上
       同字号、不同行业的另一家注册公司)。
    """
    base = normalize_semantic_text(name)
    if not base:
        return ()
    stems = {base}
    without_division = _strip_admin_division(base)
    stems.add(without_division)

    variants = set()
    for stem in stems:
        variants.add(stem)
        variants.add(_strip_one_suffix(stem, _ORG_FORM_SUFFIXES))

    return tuple(sorted(
        (v for v in variants if len(v) >= _MIN_COMPANY_VARIANT_LEN),
        key=len, reverse=True,
    ))


def _company_alias_hit(core: str, normalized_title: str, brand_names) -> str:
    """关键词核由某个品牌注册名(可带**后缀**)构成时,标题含受控缩写算同一商业对象。

    两种入口,都要求注册名落在**核的开头**:

    ① `核 == 注册名` —— 标题含任一受控缩写即可。
    ② `核 == 注册名 + 尾巴`(如「…有限公司**是做什么的**」)—— 标题必须含
       **「受控缩写 + 同一条尾巴」且连在一起**。尾巴一个字都不许变:它是客户
       问的那件事,换掉就不是同一个需求了。

    🔴 ② 不是把 ① 放宽,是把**同一条规则**延伸到带尾巴的核上:
       仍然只做删除(删的还是行政区划 / 组织形式),replace 规则一个字没动。
    🔴 尾巴必须**紧跟在缩写后面**。允许它飘到标题别处,等于只校验"两个串都出现过"
       —— 本仓 a-string-anchor-satisfied-by-an-unrelated-line。
    🔴 只认前缀位。注册名出现在核的中间/末尾时不走这条路:那时"尾巴"是什么、
       该不该跟着缩写,没有一个说得清的答案,说不清就 fail-closed。
    """
    for raw in (brand_names or ()):
        canonical = normalize_semantic_text(raw)
        if not canonical:
            continue
        if core == canonical:
            tail = ""
        elif core.startswith(canonical):
            tail = core[len(canonical):]
        else:
            continue
        for variant in company_name_variants(raw):
            if variant and (variant + tail) in normalized_title:
                return variant + tail
    return ""


def assess_title_keyword_alignment(
    title: object,
    purchased_keyword: object,
    *,
    brand_names: "tuple | list | None" = None,
) -> TitleKeywordAlignment:
    """Check that a generated title preserves the purchased business object.

    Exact containment is the strongest path. Otherwise matching blocks allow
    natural forms such as ``深圳载货电梯`` -> ``载货电梯怎么选`` or
    ``细胞治疗…隔离器推荐`` -> ``细胞治疗隔离器选型``. Isolated matching
    characters do not count, preventing a generic title from passing merely
    because it shares common characters such as ``行`` or ``业``.
    """

    normalized_title = normalize_semantic_text(title)
    normalized_keyword = normalize_semantic_text(purchased_keyword)
    core = _keyword_core(purchased_keyword)

    if not normalized_title:
        return TitleKeywordAlignment(False, "empty_title", 0.0, 0)
    if not normalized_keyword or not core:
        return TitleKeywordAlignment(False, "empty_purchased_keyword", 0.0, 0)
    required_symbols = _identity_symbols(purchased_keyword)
    if not required_symbols.issubset(_identity_symbols(title)):
        return TitleKeywordAlignment(False, "symbol_identity_drift", 0.0, 0)
    if normalized_keyword in normalized_title:
        return TitleKeywordAlignment(True, "full_keyword", 1.0, len(normalized_keyword))

    core_variants = _controlled_core_variants(core)
    if core in normalized_title:
        return TitleKeywordAlignment(True, "keyword_core", 1.0, len(core))
    for alias_core in core_variants[1:]:
        if alias_core in normalized_title:
            return TitleKeywordAlignment(
                True,
                "controlled_object_alias",
                1.0,
                len(alias_core),
            )

    # [WO_232] 关键词核就是这家公司的注册名时,标题里的自然缩写不算换商业对象。
    # 放在 replace 守卫**之前**:正是那道守卫把「丢掉 有限公司」读成了换限定词。
    company_alias = _company_alias_hit(core, normalized_title, brand_names)
    if company_alias:
        return TitleKeywordAlignment(
            True,
            "company_name_controlled_alias",
            1.0,
            len(company_alias),
        )

    matcher = SequenceMatcher(None, core, normalized_title, autojunk=False)
    # A substitution inside the purchased phrase is a different commercial
    # object by default: 医用→家用、工业→服务、乘客→载货 all fail closed.
    # Natural compression is still allowed through insert/delete operations,
    # and explicit equivalents are handled above by the alias table.
    if any(
        tag == "replace" and i1 != i2 and j1 != j2
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
    ):
        return TitleKeywordAlignment(False, "conflicting_business_qualifier", 0.0, 0)

    # Preserve both ends of a longer source phrase. In Chinese commercial
    # queries the leading boundary commonly carries geography/application and
    # the trailing boundary carries the concrete product (for example
    # ``深圳…板材`` or ``细胞治疗…隔离器``). This prevents a high overlap on a
    # broad prefix from hiding the loss of the most specific object.
    if len(core) > 4:
        leading_anchor = core[:2]
        trailing_anchor = core[-2:]
        if leading_anchor not in normalized_title or trailing_anchor not in normalized_title:
            return TitleKeywordAlignment(False, "keyword_scope_or_object_drift", 0.0, 0)

    blocks = matcher.get_matching_blocks()
    meaningful_lengths = [block.size for block in blocks if block.size >= 2]
    matched = sum(meaningful_lengths)
    longest = max(meaningful_lengths, default=0)
    coverage = matched / len(core) if core else 0.0

    # A short business object (for example "电梯") must remain contiguous.
    # Longer phrases may be naturally compressed, but must retain at least one
    # substantial anchor and more than a token fragment of the source object.
    if len(core) <= 4:
        aligned = longest == len(core)
    else:
        aligned = longest >= 3 and coverage >= 0.35

    return TitleKeywordAlignment(
        aligned,
        "meaningful_paraphrase" if aligned else "business_object_drift",
        round(coverage, 4),
        longest,
    )
