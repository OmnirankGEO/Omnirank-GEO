"""诊断选词质量守卫（P0-4 · 2026-07-26）。

生产实证：驰鲸的 8 题关键词是

    ["TikTok工厂出海获客服务", "TikTok外贸精准询盘", "TikTok工厂全案代运营", …]

—— **全是服务名称，不是真实用户问法**。对比监测侧的真实词：
"深圳载货电梯哪家好" / "北京AI培训公司排名前十"。这直接导致
决策获客层 0/7、场景转化层 0/20：客户根本不会那样问 AI，AI 自然不会答出品牌。

本模块负责三件事（都在 ``CommercialQueryPolicy`` 之后、采集之前）：

1. **真实问法**：句式必须是用户会打给 AI 的问句（"{地域}{品类}哪家好/靠谱/
   推荐/怎么选"…），服务名称式短语一律替换；
2. **区域适配**：``business_scope='regional'``（默认）的客户，非品牌题必须带
   地域限定；``business_scope='national'`` 才允许无地域大词；
3. **每层样本下限**：题数分配保证「每层题数 × 引擎数 ≥ 5」
   （``config.ai_engines.min_questions_per_layer``），配合 P0-5 消除
   "满分 + 数据不足" 那种左右脑互搏。

边界：这里**不新增任何硬阻断**。不合格的题按原槽位就地替换成同层的真实问法
模板（数量不变、不静默丢词 · SSOT §9.6），替换过程可打印、可测。
付费交付资格仍由 ``CommercialQueryPolicy`` 唯一裁决，本模块不发明第二套资格标准。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

logger = logging.getLogger("GEO-QuestionQuality")

LAYER_BRAND: Final[str] = "brand_awareness"
LAYER_LOCAL: Final[str] = "regional_industry"
LAYER_SCENARIO: Final[str] = "super_tier1"
FUNNEL_LAYERS: Final[tuple[str, ...]] = (LAYER_BRAND, LAYER_LOCAL, LAYER_SCENARIO)

SCOPE_REGIONAL: Final[str] = "regional"
SCOPE_NATIONAL: Final[str] = "national"

# 真实问法的句式信号（用户真的会这么问 AI）。
# 与 CommercialQueryPolicy 的 _DIRECT_CHOICE_SIGNALS 有重叠是有意的：
# 那边判"是否商业意图"，这边判"是否像一句人话提问"。
_REAL_QUESTION_SIGNALS: Final[tuple[str, ...]] = (
    "哪家", "哪个", "哪些", "哪里", "推荐", "怎么选", "如何选", "怎么挑", "如何挑",
    "靠谱", "好吗", "值得", "排名", "排行", "前十", "前五", "榜单", "十大",
    "多少钱", "怎么收费", "收费标准", "价格", "报价", "性价比",
    "更适合", "适合", "对比", "比较", "选哪家", "选哪个", "找谁", "去哪找",
    "是什么公司", "是做什么", "怎么样", "有哪些",
)

_QUESTION_MARKS: Final[tuple[str, ...]] = ("?", "？", "吗", "呢")

# 服务名称/产品名式短语的指纹：营销词堆叠、没有提问动作。
_SERVICE_NAME_MARKERS: Final[tuple[str, ...]] = (
    "全案", "代运营", "获客服务", "精准询盘", "一站式", "解决方案", "整体方案",
    "服务方案", "托管服务", "运营服务", "推广服务", "营销服务", "落地服务",
)

_CITY_SUFFIX: Final[re.Pattern[str]] = re.compile(r"(省|市|区|县|自治区|自治州)$")

# [R1 · 2026-08-03] 括号形态：brands.cities 常写成 "贵州省遵义市仁怀市（茅台镇）"
# / "广东省深圳市（龙岗区平湖）"。整串结尾是"）"，_CITY_SUFFIX 的 $ 锚定命中不了、
# parse_admin_region 的 $ 也匹配不上 → 旧 normalize_city 把 14 字地址原样吐回，
# 再进 has_geo_qualifier 的 `city in raw` 就成了**恒假判据**（生产实证 brand 745）。
_BRACKET_RE: Final[re.Pattern[str]] = re.compile(r"[（(]([^）)]*)[）)]")

# 行政区划切块：从左到右逐块吃掉 "贵州省/遵义市/仁怀市"。
# 用 finditer 逐块扫，而不是 parse_admin_region 的整串单次 match ——
# 后者遇到"省+市+市"（县级市套地级市）会把 city 回溯成"遵义市仁怀市"，剥完是
# "遵义市仁怀"，仍是鬼话。
_ADMIN_CHUNK_RE: Final[re.Pattern[str]] = re.compile(
    r"[一-龥]{1,8}?"
    r"(?:特别行政区|维吾尔自治区|壮族自治区|回族自治区|自治区|自治州|自治县"
    r"|地区|省|市|盟|区|县|旗|街道|镇|乡|村)"
)
# 市级后缀：normalize_city 取**最后一个**市级块当主地名（"贵州省遵义市仁怀市" → 仁怀）。
_CITY_LEVEL_SUFFIX: Final[tuple[str, ...]] = ("自治州", "地区", "市", "盟")
# [Review §7.2] 镇/街道级 token **保留整词不剥后缀**：token 必须是"茅台镇"，
# 禁止产出"茅台" —— 酱酒行业"茅台"是品牌词，剥成"茅台"后任何提茅台品牌的题
# （"和茅台的区别"）都会被误判"带地域"而跳过矫正。
_SUBCITY_KEEP_WHOLE: Final[tuple[str, ...]] = ("街道", "镇", "乡", "村")

_NON_CITY_VALUES: Final[frozenset[str]] = frozenset({"全国", "全球", "海外", "不限", "各地"})

# ── [R6 · 2026-08-04] 分类学串黑名单 ────────────────────────────────────────
# 《国民经济行业分类》GB/T 4754 的 20 个**门类**名。这些是登记名录用语,不是
# 任何人会打给 AI 的词:"深圳卫生和社会工作哪家好?" / "深圳制造业哪家好?"。
# 生产实证(2026-08-04 逐题判定 · 反向对照已做):11 次诊断 / 11 个品牌的题面里
# 出现过 industry 整串,其中 9 家是真客户(529/515/511/489/129…)。
_INDUSTRY_L1_NAMES: Final[frozenset[str]] = frozenset({
    "农、林、牧、渔业", "采矿业", "制造业", "电力、热力、燃气及水生产和供应业",
    "建筑业", "批发和零售业", "交通运输、仓储和邮政业", "住宿和餐饮业",
    "信息传输、软件和信息技术服务业", "金融业", "房地产业", "租赁和商务服务业",
    "科学研究和技术服务业", "水利、环境和公共设施管理业",
    "居民服务、修理和其他服务业", "教育", "卫生和社会工作", "文化、体育和娱乐业",
    "公共管理、社会保障和社会组织", "国际组织",
})

# 门类之下的**大类/中类**名录里同样进不了题面的那些(生产 brands.industry 实际取值)。
# 只收"整串等于它才拦"的,不做子串匹配 —— "餐饮业"要拦,但"特色烤鱼正餐服务"不能误伤。
_INDUSTRY_TAXONOMY_EXTRA: Final[frozenset[str]] = frozenset({
    "商务服务业", "住宿业", "餐饮业", "正餐服务", "批发业", "零售业", "农林牧渔业",
    "互联网和相关服务", "软件和信息技术服务业", "科技推广和应用服务业",
    "建筑装饰、装修和其他建筑业", "专用设备制造业", "通用设备制造业",
    "电子显示器件制造", "制药专用设备制造", "其他未列明批发业",
    "多式联运和运输代理业", "居民服务业", "装卸搬运和仓储业", "文化艺术业",
    "农副食品加工业", "速冻调理食品制造业", "汽车租赁服务业", "电力设备",
    "跨境数字营销服务业", "其他",
})

# 口语化改写:登记用语 → 客户自己会说的词。只在**整串等于左边**时替换,
# 避免 "医疗美容服务 / 生活美容服务" 这种多段串被半截替换。
_TRADE_COLLOQUIAL: Final[dict[str, str]] = {
    "医疗美容服务": "医美", "医疗美容": "医美", "整形美容": "医美",
    "生活美容服务": "美容", "健康养生服务": "养生",
    "餐饮服务": "餐饮", "住宿服务": "酒店", "酒店住宿": "酒店",
    # 分类名录里**剥掉后缀就能用**的那几个：显式写进映射表，而不是靠通用剥后缀
    # ——"餐饮业"→"餐饮" 可以，"制造业"→"制造" 不行，两者用同一条通用规则分不开。
    "餐饮业": "餐饮", "正餐服务": "餐饮", "住宿业": "酒店", "零售业": "零售",
    "批发业": "批发", "汽车租赁服务业": "租车",
    "职业技能培训": "技能培训", "在线教育": "网课",
    "汽车租赁": "租车", "豪车租赁配司机": "豪车带司机",
    "货物运输代理": "货代", "跨境物流": "跨境物流",
    "建筑材料批发": "建材批发", "家居建材": "建材",
    "住宅室内设计（家装为主）": "家装", "家装领域": "家装",
    "生成式引擎优化（GEO）": "GEO优化", "数字营销服务": "数字营销",
}
# 通用后缀剥离:剥完仍要 ≥2 字,且剥完不能又落进分类学名录。
_TRADE_TAIL_SUFFIX: Final[tuple[str, ...]] = ("服务业", "制造业", "行业", "服务", "业")

_TRADE_MIN_LEN: Final[int] = 2
_TRADE_MAX_LEN: Final[int] = 14

# ── [R7 · 2026-08-04] 受众判定 ─────────────────────────────────────────────
# "哪家方案更适合中小企业" 这类 B2B 后缀**默认关闭**,只有行业命中 B2B 名录才启用。
# 生产实证:该后缀落在 12 次诊断上,其中 529(医美)/507(高端商业公馆)/503(尺八乐器)
# 明显不是 B2B —— 医美客户是个人求美者,不是"中小企业采购方案"。
# 默认关(而不是默认开)的理由与 R3「只升不降」同源:窄后缀猜错的代价远大于通用后缀。
AUDIENCE_B2B: Final[str] = "b2b"
AUDIENCE_CONSUMER: Final[str] = "consumer"

_B2B_MARKERS: Final[tuple[str, ...]] = (
    "B2B", "b2b", "ToB", "toB", "OEM", "ODM", "SaaS", "saas", "MarTech", "GEO",
    "代运营", "外贸", "出海", "跨境", "招商", "贴牌", "批发", "供应链", "经销",
    "工厂", "制造", "工业", "设备", "装备", "仪器", "元器件", "原材料", "建材",
    "企业服务", "企业级", "商务服务", "咨询", "财税", "法律服务", "律师",
    "广告", "公关", "会展", "营销", "获客", "询盘", "代理记账", "人力资源",
    "物流", "货运", "仓储", "海外仓", "工程", "总包", "分销", "加盟", "招投标",
    "系统开发", "软件开发", "信息技术服务", "解决方案商", "服务商",
    # 生产 brands.industry / 品牌名里实际出现过的常见 B2B 品类词，
    # 同时也是 trade_from_brand_name 扫品牌名的词表来源（"深圳市恒通电梯"→"电梯"）。
    "电梯", "五金", "线缆", "门窗", "模具", "包装", "印刷", "检测", "机械",
)
_CONSUMER_MARKERS: Final[tuple[str, ...]] = (
    "医美", "医疗美容", "整形", "美容", "美发", "口腔", "牙科", "眼科", "体检",
    "月子", "产后修复", "养生", "推拿", "按摩", "spa", "SPA",
    "餐饮", "正餐", "火锅", "烧烤", "烤鱼", "小吃", "咖啡", "茶饮", "烘焙", "私厨",
    "酒店", "民宿", "住宿", "旅游", "景区", "娱乐", "健身", "瑜伽", "普拉提",
    "宠物", "摄影", "婚纱", "婚庆", "亲子", "母婴", "早教", "驾校", "家政",
    "家装", "装修", "全屋定制", "翡翠", "珠宝", "饰品", "零售", "商场", "超市",
    "洗车", "维修", "搬家", "留学", "考研", "艺考",
)


def industry_audience(industry: Any, keywords: Any = None, trade: Any = "") -> str:
    """行业受众:``b2b`` 还是 ``consumer``(默认 consumer)。

    [R7 · 2026-08-04] 用来决定场景层能不能用 "哪家方案更适合中小企业" 这种
    **B2B 专属后缀**。判定只看文本(industry / 品类词 / 核心词),**不读
    ``brands.business_type``** —— 那个字段和 ``city_scope`` 一样是死默认
    (生产实证 ``server.py`` 自己的注释:business_type/city_scope 是死默认
    B2C/local),拿它当权威源等于把全部客户判成 C 端,是另一种"修 A 坏 B"。

    默认值取 ``consumer`` 而不是"未知":C 端/通用后缀("哪家口碑好""哪家正规")
    对 B2B 客户至多是**不够精准**,B2B 后缀对 C 端客户是**直接出鬼话**
    (生产实证 529「广东深圳龙岗医美哪家方案更适合中小企业?」)。
    不对称 → 默认取代价小的那一侧。

    >>> industry_audience("卫生和社会工作 / 医疗美容服务")
    'consumer'
    >>> industry_audience("科技推广和应用服务业 / TikTok海外B2B精准获客")
    'b2b'
    """
    haystack = " ".join(
        [str(industry or ""), str(trade or "")]
        + [str(k or "") for k in (keywords or [])]
    )
    if not haystack.strip():
        return AUDIENCE_CONSUMER
    b2b_hits = sum(1 for m in _B2B_MARKERS if m in haystack)
    consumer_hits = sum(1 for m in _CONSUMER_MARKERS if m in haystack)
    if b2b_hits > consumer_hits:
        return AUDIENCE_B2B
    return AUDIENCE_CONSUMER


def looks_like_taxonomy_term(text: Any) -> bool:
    """是不是「行业分类名录用语」——禁止直接进题面。

    [R6 · 2026-08-04] 三种形态都算:
      · 门类名(``制造业`` / ``卫生和社会工作`` / ``租赁和商务服务业``);
      · 大类/中类名(``商务服务业`` / ``专用设备制造业`` / ``正餐服务``);
      · **含 "/" 的复合登记串**(``卫生和社会工作 / 医疗美容服务``)—— 生产实证
        LLM 会把它逐字抄进题面(529 的两道题),因为出题 prompt 的示例里就写着
        ``"{city}{industry}哪家好"``。

    >>> looks_like_taxonomy_term("卫生和社会工作")
    True
    >>> looks_like_taxonomy_term("卫生和社会工作 / 医疗美容服务")
    True
    >>> looks_like_taxonomy_term("医美")
    False
    """
    raw = str(text or "").strip()
    if not raw:
        return True
    if re.search(r"[/／]", raw):
        return True
    if raw in _INDUSTRY_L1_NAMES or raw in _INDUSTRY_TAXONOMY_EXTRA:
        return True
    return False


def colloquialize_trade(text: Any) -> str:
    """登记用语口语化:``医疗美容服务`` → ``医美``、``餐饮业`` → ``餐饮``。

    [R6 · 2026-08-04] 先查显式表,再剥通用后缀(服务业/制造业/行业/服务/业)。
    剥完 <2 字、或剥完又落进分类学名录的,一律**回退到剥之前**交给调用方判废
    —— "制造业" 剥成 "制造" 不是口语化,是把鬼话缩短。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    mapped = _TRADE_COLLOQUIAL.get(raw)
    if mapped:
        return mapped
    if raw in _INDUSTRY_L1_NAMES or raw in _INDUSTRY_TAXONOMY_EXTRA:
        return raw  # 名录整串:不做后缀剥离,交给 looks_like_taxonomy_term 判废
    for suffix in _TRADE_TAIL_SUFFIX:
        if raw.endswith(suffix) and len(raw) - len(suffix) >= _TRADE_MIN_LEN:
            stripped = raw[: -len(suffix)]
            if stripped in _INDUSTRY_L1_NAMES or stripped in _INDUSTRY_TAXONOMY_EXTRA:
                return raw
            return _TRADE_COLLOQUIAL.get(stripped, stripped)
    return raw


def _strip_brackets(text: str) -> tuple[str, list[str]]:
    """拆出 (括号外主串, [括号内内容...])。"""
    inner = [m.group(1).strip() for m in _BRACKET_RE.finditer(text) if m.group(1).strip()]
    outer = _BRACKET_RE.sub("", text).strip()
    return outer, inner


#: 无行政后缀时可作为前缀切开的省级/直辖市名(白名单)。
#: 直辖市不在 ``quote_scope_lock._PROVINCE_NAMES`` 里(那份表判的是"省"),
#: 但"北京朝阳""上海浦东"是同一种写法,一并收进来。
_BARE_GEO_PREFIXES: Final[tuple[str, ...]] = ("北京", "上海", "天津", "重庆")


def _split_suffixless_geo(tail: str) -> list[str]:
    """把「贵州贵阳」这种**无行政后缀的省市连写**切成 ['贵州','贵阳']。

    🔴 [WO 2026-08-06 §3] 存在的理由是生产落库快照自证,不是设想:

      诊断 561 / 553 的 ``question_quality.city`` 都是 ``"贵州贵阳"``(代理在体检
      发起表单里就是这么打的),而 ``repairs`` 里 7 条 ``result="prefixed"``:

        original    "贵阳小龙虾夜宵店哪家好吃？"          ← LLM 出的题**本来就带贵阳**
        replacement "贵州贵阳贵阳小龙虾夜宵店哪家好吃？"  ← 修复层自己加出来的重复

      链路:``_ADMIN_CHUNK_RE`` 要求每块自带 省/市/区/县 后缀,"贵州贵阳"一个后缀
      都没有 → 整串落进 tail → 单 token ``('贵州贵阳',)`` → ``has_geo_qualifier``
      拿这个 token 去题面里找,找不到 → 判 ``missing_geo_qualifier`` → 补前缀。

      **所以工单 §3 写的「LLM 提示词把所在地和关键词都塞进去没去重」是错的** ——
      提示词没问题、LLM 出的题也没问题,重复是矫正层制造的。修提示词一个字都不解决。

    切法是**只加不减**:整串仍然保留为一个 token,再额外吐出前缀与余串。
    这与 R5 合并 ``brands.cities`` 是同一种单调安全性 —— token 池只会变大 →
    ``has_geo_qualifier`` 只会更容易判 True → 只会**减少**前缀/替换,
    不存在"拆完反而多改一道题"的路径。真误伤(比如有人把"重庆火锅"填进城市栏)
    的后果也只是少加一个前缀,不会产出错地名。
    """
    if len(tail) < 4:
        return []
    try:
        from services.quote_scope_lock import _PROVINCE_NAMES

        prefixes: tuple[str, ...] = (*_PROVINCE_NAMES, *_BARE_GEO_PREFIXES)
    except Exception:  # pragma: no cover - 依赖缺失时不拆(退回旧行为)
        prefixes = _BARE_GEO_PREFIXES
    for prefix in sorted(prefixes, key=len, reverse=True):
        if tail.startswith(prefix) and len(tail) - len(prefix) >= 2:
            return [prefix, tail[len(prefix):]]
    return []


def _admin_chunks(segment: str) -> list[str]:
    """把一段地址切成行政区划块；末尾无后缀的残串也算一块。"""
    chunks: list[str] = []
    pos = 0
    for m in _ADMIN_CHUNK_RE.finditer(segment):
        chunks.append(m.group(0))
        pos = m.end()
    tail = segment[pos:].strip()
    if len(tail) >= 2:
        chunks.append(tail)
        # [WO 2026-08-06 §3] 无后缀省市连写再拆一层(只加不减,见上面函数注释)。
        chunks.extend(_split_suffixless_geo(tail))
    return chunks


def _token_of(chunk: str) -> str:
    """一个行政块 → 一个地名 token。镇/街道级保留整词，其余剥后缀。

    镇/街道那一条是**显式写出来的契约**，不是纯粹必需：``normalize_admin_name``
    今天的后缀表里本来就没有 镇/街道/乡/村，所以删掉这个分支当下行为不变。
    写出来是为了让 §7.2 的"茅台镇不许剥成茅台"不依赖**另一个模块的顺带行为**——
    那边哪天加一条"镇"，这边就会静默把品牌词误判成地名。
    """
    chunk = chunk.strip()
    if not chunk:
        return ""
    if chunk.endswith(_SUBCITY_KEEP_WHOLE):
        return chunk
    from services.quote_scope_lock import normalize_admin_name

    return normalize_admin_name(chunk) or chunk


def geo_tokens(value: Any) -> tuple[str, ...]:
    """把（可能是整串地址的）城市值拆成地名 token 集。

    [R1 · 2026-08-03] `has_geo_qualifier` 旧判据要求题面**逐字**含整串城市值，
    "贵州省遵义市仁怀市（茅台镇）" 这种写法下等于恒假 —— 生产实证 brand 745
    的 6 条 LLM 好题（"仁怀市哪家酒厂能做53度坤沙酒OEM贴牌？"…）全被判缺地域
    并整条替换。改成 token 命中：任一级地名出现在题面即算带地域。

    >>> geo_tokens("贵州省遵义市仁怀市（茅台镇）")
    ('贵州', '遵义', '仁怀', '茅台镇')
    """
    raw = str(value or "").strip()
    if not raw or raw in _NON_CITY_VALUES:
        return ()
    tokens: list[str] = []
    for part in re.split(r"[、,，/／|;；\s]+", raw):
        part = part.strip()
        if not part or part in _NON_CITY_VALUES:
            continue
        outer, inner = _strip_brackets(part)
        for segment in ([outer] if outer else []) + inner:
            for chunk in _admin_chunks(segment):
                token = _token_of(chunk)
                if len(token) >= 2 and token not in tokens:
                    tokens.append(token)
    return tuple(tokens)


def normalize_business_scope(value: Any) -> str:
    """业务范围归一：全国 / 区域（默认区域）。

    区域是**默认值**：大多数代理客户是本地/区域生意，给他们出无地域大词
    等于让 AI 在全国竞品里比，必然 0 命中（驰鲸案例）。
    """
    raw = str(value or "").strip().lower()
    if raw in {"national", "全国", "全球", "nationwide", "global"}:
        return SCOPE_NATIONAL
    if raw in {"hybrid", "混合"}:
        # hybrid 既做本地又做全国 → 按区域处理（保证地域题不缺），
        # 场景层仍可出大词（见 required_geo_layers）。
        return SCOPE_REGIONAL
    return SCOPE_REGIONAL


#: [WO_251] 街道段:**街名词**(最长优先)+ 它前面最多 3 个汉字的街名主体。
#: 只收「大道/街道/路/街/道/巷/里/弄/号」这类街名词,**不收「区/县/镇」**——
#: 后者是行政区划,既有解析本来就认得,切掉反而丢信息。
#: 🔴 为什么要回退 3 个字而不是让正则自己吃:非贪婪会把城市名也吃进来,
#:    '揭阳滨江南路…' 的匹配会从**下标 0** 开始,切完就什么都不剩。
#:    回退固定字数之后,切点落在街名主体的开头('环市北'),前面那段才是地址。
#: ⚠️ 3 这个数不必精确:切多切少只要**把市级块留在前面那段里**,
#:    下面既有的解析就仍然取得到市('广东省揭阳市榕' 照样解出揭阳)。
_STREET_TAIL_RE = re.compile(
    r"(?P<name>[一-龥]{1,3})(?:大道|街道|路|街|道|巷|里|弄|号)"
)


def normalize_city(value: Any) -> str:
    """城市归一：去掉行政区划后缀，"全国/海外" 这类非城市值返回空串。

    [Review-CTO 2026-07-27 · Owner 生产实测 diagnosis 489] brands.cities 常见
    整串注册地址（"广东省深圳市龙岗区"），只剥尾缀会剩"广东省深圳市龙岗"，
    直接拼进题面成鬼话。先走报价线已锤炼的省市区解析提取城市，再走旧逻辑兜底。

    [R1 · 2026-08-03] 再加括号形态：`XX（YY）` / `XX(YY)`。
    [Review-CTO §7.1] 主地名 = **剥括号后**按既有规则解析（仁怀 / 深圳），
    括号内容（茅台镇 / 龙岗区平湖）**不当主地名**，只进 ``geo_tokens``。
    理由是生产天然 A/B：美构用"深圳"作主名 0 replacements 出好题；若把括号内
    当主名，"广东省深圳市（龙岗区平湖）" 的模板前缀会变"龙岗区平湖"——没人这么搜。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw in _NON_CITY_VALUES:
        return ""
    # [WO_251 2026-09-20] 先切掉**街道段**,只拿它前面那部分去解析。
    #
    # 生产事故(客户揭阳雅栖酒店):街名里的「市」被当成了市级后缀 ——
    #   `normalize_city('揭阳滨江南路雅栖酒店')` → **'揭阳环'**
    #   `normalize_city('广东省揭阳市榕城区滨江南路')` → **'环市'**
    # 下游竞品调研拿它去搜「揭阳环 餐饮食品 服务商 知名」,搜的是一个不存在的地方。
    #
    # 🔴 为什么是"切街道段"而不是"改省市区解析优先":
    #    `quote_scope_lock._ADMIN_RE` 是**锚定**的(`^...$`),而且它的 `_STREET_RE`
    #    只认「街道|镇|乡|片区|社区|村」,不认「路|街|道」⇒ 带街名的整串**解析必失败**,
    #    于是落到下面「取最后一个市级块」那条,而街名里的「市」正好被它吃掉。
    #    换句话说:解析没有"失败返回半截",是**它压根没参与**,半截来自兜底那条。
    #    所以要治的是**喂给它的东西**,不是它的优先级。
    #
    # 🔴 只在**街道段前面还有内容**时才切:「大道科技」这类以街道词开头的名字
    #    切完是空串,那就不是地址,保持原样交给下面的旧逻辑。
    _street_cut = _STREET_TAIL_RE.search(raw)
    if _street_cut and _street_cut.start("name") > 0:
        raw = raw[: _street_cut.start("name")].strip()
        if not raw:
            return ""
    # 多城市输入取第一个（"深圳/广州" → "深圳"）
    raw = re.split(r"[、,，/／|;；\s]+", raw)[0].strip()
    if not raw:
        return ""
    # 括号内容不参与主地名判定（§7.1）
    outer, _inner = _strip_brackets(raw)
    if outer and outer in _NON_CITY_VALUES:
        return ""
    base = outer or raw
    # 取**最后一个**市级块："贵州省遵义市仁怀市" → 仁怀（县级市比地级市更贴近
    # 客户自称）；"广东省深圳市龙岗区" → 深圳（旧行为不变）。
    city_chunks = [c for c in _admin_chunks(base) if c.endswith(_CITY_LEVEL_SUFFIX)]
    if city_chunks:
        return _token_of(city_chunks[-1])
    # [WO 2026-08-06 §3] 无后缀省市连写("贵州贵阳")→ 主地名取更具体的那一半。
    #   与上面"取最后一个市级块"(贵州省遵义市仁怀市 → 仁怀)同一条取向:
    #   越具体越贴近客户自称。不修这里,前缀会是"贵州贵阳"整串。
    suffixless = _split_suffixless_geo(base)
    if suffixless:
        return suffixless[-1]
    try:
        from services.quote_scope_lock import parse_admin_region

        parsed = parse_admin_region(base)
        city = str(parsed.get("city") or "").strip()
        if city:
            return _CITY_SUFFIX.sub("", city) or city
    except Exception:
        pass
    stripped = _CITY_SUFFIX.sub("", base)
    return stripped or base


def city_level_name(value: Any) -> str:
    """只取**市级**主地名;拿不到(只有省级 / 只有乡镇 / 空 / 非城市值)返回空串。

    [R5 · 2026-08-04] ``normalize_city`` 拿不到市级块时会兜底吐省名("广东省"→"广东"),
    分不出"用户填了市"和"用户只填了省"。R5 的合并规则要靠这个区分,所以单独一个
    **只认市级**的判据。

    >>> city_level_name("广东省深圳市龙岗区")
    '深圳'
    >>> city_level_name("广东省，香港")
    ''
    """
    raw = str(value or "").strip()
    if not raw or raw in _NON_CITY_VALUES:
        return ""
    raw = re.split(r"[、,，/／|;；\s]+", raw)[0].strip()
    if not raw:
        return ""
    outer, _inner = _strip_brackets(raw)
    base = outer or raw
    if base in _NON_CITY_VALUES:
        return ""
    chunks = [c for c in _admin_chunks(base) if c.endswith(_CITY_LEVEL_SUFFIX)]
    if chunks:
        return _token_of(chunks[-1])
    # [WO 2026-08-06 §3] "贵州贵阳" 的后半截就是市级意图(与 resolve_primary_geo
    # 对"光秃秃市名"的既有取向一致:没有"市"字不代表不是市级)。
    suffixless = _split_suffixless_geo(base)
    return suffixless[-1] if suffixless else ""


def _is_province_level(raw_city: Any, normalized: str) -> bool:
    """表单值是不是**只给到省级**。

    [R5 · 2026-08-04] 判据 = 归一后的主地名落在省名表里,且原串里没有市级块。
    "广东省，香港" → 主地名"广东" + 无市级块 → True(529 病灶);
    "贵阳" / "深圳" → 不在省名表里 → False(哪怕它们没有"市"后缀);
    "广东省深圳市龙岗区" → 有市级块 → False。
    """
    if city_level_name(raw_city):
        return False
    try:
        from services.quote_scope_lock import _PROVINCE_NAMES

        provinces = set(_PROVINCE_NAMES)
    except Exception:  # pragma: no cover - 依赖缺失时退回"不当省级"(不触发回落)
        return False
    return normalized in provinces or f"{normalized}省" in provinces


def merged_geo_tokens(city: Any, brand_cities: Any = "") -> tuple[str, ...]:
    """地域 token 池 = **表单值 ∪ 品牌档案 ``brands.cities``**。

    [R5 · 2026-08-04] 生产病灶(brand 737 深圳港融医疗美容 · 诊断 529):
    表单「客户区域」被改成 ``广东省，香港``,而档案 ``cities='广东省深圳市龙岗区'``。
    旧 token 池只吃表单值 → ('广东','香港') → LLM 出的 4 道**本来就带"深圳"**的好题
    全被判 ``missing_geo_qualifier`` 并前缀成"广东深圳…"(双地名赘字)。
    同一品牌 8-01 的诊断 509 表单值等于档案值 → 0 repairs、8 道题全干净 ——
    **天然 A/B 证明病根是 token 池的来源太窄,不是矫正规则本身**。

    合并方向是**单调安全**的:池子只会变大 → ``has_geo_qualifier`` 只会更容易判 True
    → 只会**减少**替换/前缀。不存在"合并后反而多改了一道题"的路径。
    """
    tokens = list(geo_tokens(city))
    for token in geo_tokens(brand_cities):
        if token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def resolve_primary_geo(city: Any, brand_cities: Any = "") -> str:
    """主地名(拼进模板题的那个前缀)。

    [R5 · 2026-08-04] 返工单原文是"主地名优先档案解析出的市级"。**实现成
    「表单没到市级时才用档案的市级」**,这是一处**有意偏离**,理由是生产分布:

      · 全库 37 个品牌的 ``brands.cities`` 与最近一次诊断的表单值不一致;
        其中十余个是**表单值才是对的**(brand 126 档案"江苏省扬州市宝应县夏集镇"
        / 表单"哈尔滨市";brand 111 档案"上海" / 表单"中国·深圳（龙岗区）";
        brand 76 档案"中国·深圳市龙岗区" / 表单"贵阳")。档案无条件优先 =
        把这十几家客户的诊断城市改成一个他们没选的城市 —— 又一轮"修 A 坏 B"
        (与 R3 ``city_scope`` 只升不降同型)。
      · 529 的病灶恰恰是**表单只到省级**("广东省，香港")而档案有市级("深圳")。
        只补这一格就完整修好病灶,且不碰任何"表单已到市级"的客户。

    规则:
      · 表单解析不出任何地名(空 / "全国" / "海外") → 返回空串,**矫正闸保持关闭**
        (旧行为逐字节不变 —— 不因为档案有地名就给一个从不出地域题的客户开闸);
      · 表单给出的**不是省名** → 一律用表单的(用户的选择权威)。这里判的是
        "是不是省名",**不是**"带没带'市'后缀" —— 生产里大量表单值是光秃秃的
        市名("贵阳" / "深圳" / "杭州" / "成都"),它们没有"市"字但完全是市级意图;
        若按"有没有市后缀"判,brand 76(表单"贵阳" / 档案"中国·深圳市龙岗区")
        会被静默改成深圳,正是要防的那类"修 A 坏 B";
      · 表单只给到省级 且 档案有市级 → 用档案的市级(529 病灶就这一格)。
    """
    form_town = normalize_city(city)
    if not form_town:
        return ""
    if not _is_province_level(city, form_town):
        return form_town
    archive_city = city_level_name(brand_cities)
    if archive_city and archive_city != form_town:
        logger.info(
            "[选词质量] 表单城市 %r 只到省级/非市级 → 主地名改用品牌档案的市级 %r"
            "(brands.cities=%r)",
            form_town, archive_city, str(brand_cities or "")[:40],
        )
        return archive_city
    return form_town


def brand_core_name(brand_name: Any) -> tuple[str, ...]:
    """品牌名核心词（去公司后缀 + 去括号）——与 ``_filter_brand_from_questions`` 同口径。

    [R2 · 2026-08-03] 额外产出**去掉注册地前缀**的第二个变体："贵州禾泉酒业" →
    ("贵州禾泉酒业", "禾泉酒业")。中国企业名普遍是"注册省/市 + 字号 + 行业"，
    客户自己填的核心词往往省掉省名（"禾泉酒业招商"）——只比对全名的话这条会被
    当成品类词，拼进模板就成了非品牌层的题里带品牌名（出题 prompt 明令禁止）。
    """
    core = str(brand_name or "").strip()
    for suffix in ("(中国)有限公司", "（中国）有限公司", "有限公司", "股份有限公司",
                   "集团", "公司"):
        if core.endswith(suffix) and len(core) > len(suffix) + 1:
            core = core[: -len(suffix)]
    core = _BRACKET_RE.sub("", core).strip()
    if len(core) < 2:
        return ()
    variants = [core]
    from services.quote_scope_lock import _PROVINCE_NAMES

    for province in _PROVINCE_NAMES:
        # 剥完至少还要剩 2 字，否则"广东"这种品牌名会被剥成空
        if core.startswith(province) and len(core) - len(province) >= 2:
            variants.append(core[len(province):])
            break
    return tuple(variants)


def distill_trades(
    industry: Any,
    keywords: Any = None,
    *,
    limit: int = 4,
    brand_name: Any = "",
    for_question: bool = True,
) -> list[str]:
    """把"行业 + 核心词"炼成**多个**能进问句的品类词。

    [R2 · 2026-08-03] 旧 ``distill_trade`` 只产出 1 个品类词，模板池 13 条全用它
    → 前缀完全同质（生产实证 brand 745：6 个核心词只留"商务送礼酱香酒"，
    8 题里 7 题是同前缀换后缀）。这里按原口径（短的更像人话）取前 N 个，
    调用方按品类词轮转出题。

    品牌名不得混进品类词（复用 ``brand_core_name`` 的品牌名泄漏防护）。

    [R6 · 2026-08-04] ``for_question=True``（默认，出题面用）时，industry 回落链
    变成「L2 段 → 口语化 → 判废 → 品牌经营词 → 空」，炼不出宁可返回空列表；
    ``for_question=False`` 保留旧口径（server.py 竞品调研检索 query 在用）。
    """
    cores = brand_core_name(brand_name)
    picked: list[str] = []
    for kw in sorted((str(k or "").strip() for k in (keywords or [])), key=len):
        if not (4 <= len(kw) <= 14):
            continue
        if looks_like_real_question(kw):
            continue
        if any(core in kw for core in cores):
            # 品类词里混进品牌名 → 非品牌层的题会被 _filter_brand_from_questions 洗掉
            continue
        if kw in picked:
            continue
        picked.append(kw)
        if len(picked) >= limit:
            break
    if picked:
        return picked
    if not for_question:
        # [R6 · 2026-08-04] 非题面用途(server.py 竞品调研主语纠偏是**检索 query**,
        # 不给用户看)保留旧口径,行为逐字节不变 —— R6 的"禁吐分类学串"是题面规则,
        # 不该顺手改掉一个只是共用了这个函数的检索路径。
        raw = str(industry or "").strip()
        if raw:
            tail = re.split(r"[/／]", raw)[-1].strip()
            head = re.split(r"[、,，;；]", tail)[0].strip()
            if 2 <= len(head) <= 14 and not any(core in head for core in cores):
                return [head]
        return ["相关服务"]
    from_industry = trade_from_industry(industry, cores)
    if from_industry:
        return [from_industry]
    from_brand = trade_from_brand_name(brand_name, cores)
    if from_brand:
        logger.info(
            "[选词质量] industry=%r 是分类名录用语/不可用 → 回落品牌经营词 %r",
            str(industry or "")[:60], from_brand,
        )
        return [from_brand]
    # [R6 · 2026-08-04] **宁可模板题少出,也不出鬼话**。旧版这里返回 "相关服务",
    # 拼出来是"深圳相关服务哪家好?"——同样没人会问。返空 → 模板池空 → 层配额
    # 补不齐(允许低于下限),报告层照旧看得到 layer_counts。必须留痕,不许静默。
    logger.warning(
        "[选词质量] 炼不出可用品类词(industry=%r · 核心词 %d 个 · 品牌 %r)"
        " → 模板池置空,本次层配额允许低于下限(宁缺毋滥)",
        str(industry or "")[:60], len(list(keywords or [])), str(brand_name or "")[:30],
    )
    return []


def trade_from_industry(industry: Any, brand_cores: tuple[str, ...] = ()) -> str:
    """从 ``industry`` 登记串炼一个能进题面的品类词;炼不出返回空串。

    [R6 · 2026-08-04] 顺序 = 去括号 → 取 "/" **最后一段(L2)** → 取顿号/逗号第一段
    → 口语化 → 三道判废(分类名录 / 长度 / 品牌名泄漏)。

    >>> trade_from_industry("卫生和社会工作 / 医疗美容服务")
    '医美'
    >>> trade_from_industry("制造业")
    ''
    >>> trade_from_industry("专用设备制造业（工业显示设备 / 智能显示装备）")
    '智能显示装备'
    """
    text = str(industry or "").strip()
    if not text:
        return ""
    outer, inner = _strip_brackets(text)
    # 括号外先试;括号外只剩分类名录时(“专用设备制造业（工业显示设备 / 智能显示装备）”)
    # 再试括号内 —— 那种写法里真正的品类信息全在括号里。
    for segment in [outer] + inner:
        cand = _trade_candidate(segment, brand_cores)
        if cand:
            return cand
    return ""


def _trade_candidate(segment: Any, brand_cores: tuple[str, ...] = ()) -> str:
    """一段登记串 → 一个候选品类词(过三道判废);判废返回空串。"""
    raw = str(segment or "").strip()
    if not raw:
        return ""
    # 含 "/" → L2 段(最后一段);"制造业-酒、饮料和精制茶制造业" 这类带连字符的
    # 登记串同样取最后一段,否则顿号切出来是"制造业-酒"。
    tail = re.split(r"[/／]", raw)[-1].strip()
    tail = re.split(r"[-—－]", tail)[-1].strip()
    head = re.split(r"[、,，;；]", tail)[0].strip()
    if not head:
        return ""
    cand = colloquialize_trade(head)
    if looks_like_taxonomy_term(cand):
        return ""
    if not (_TRADE_MIN_LEN <= len(cand) <= _TRADE_MAX_LEN):
        return ""
    if any(core in cand for core in brand_cores):
        return ""
    return cand


def trade_from_brand_name(brand_name: Any, brand_cores: tuple[str, ...] = ()) -> str:
    """从品牌名里认出**经营词**(品类线索);认不出返回空串。

    [R6 · 2026-08-04] 返工单的第二级回落("仍不可用退品牌经营词")。中国企业名
    普遍是"地名 + 字号 + 经营范围"(深圳港融**医疗美容** / 深圳市联通**电梯**),
    经营范围那一截就是能进题面的品类词。做法是拿已知品类词表去**扫**品牌名并取
    最长命中 —— 不做"切掉字号"的猜测(切错就是把字号当品类,比不出题更糟)。

    >>> trade_from_brand_name("深圳港融医疗美容")
    '医美'
    >>> trade_from_brand_name("贵州禾泉酒业")
    ''
    """
    name = _BRACKET_RE.sub("", str(brand_name or "")).strip()
    if not name:
        return ""
    if looks_like_taxonomy_term(name):
        # 品牌名**本身**就是分类名录用语时,它提供不了经营词。生产实证:有 4 个
        # 品牌的 name 逐字就是 "制造业"(id 161/408/409/413),扫词表会命中 B2B
        # 标记 "制造" → 吐出 "深圳制造哪家好？",和 R6 要挡的鬼话是同一类。
        return ""
    vocabulary = set(_TRADE_COLLOQUIAL) | set(_CONSUMER_MARKERS) | set(_B2B_MARKERS)
    hits = [word for word in vocabulary if len(word) >= 2 and word in name]
    if not hits:
        return ""
    best = colloquialize_trade(max(hits, key=len))
    if looks_like_taxonomy_term(best):
        return ""
    if not (_TRADE_MIN_LEN <= len(best) <= _TRADE_MAX_LEN):
        return ""
    if any(core in best for core in brand_cores):
        return ""
    return best


def distill_trade(industry: Any, keywords: Any = None) -> str:
    """把"行业"炼成能进问句的品类词（单值口径 · server.py 竞品主语纠偏在用）。

    [Review-CTO 2026-07-27] industry 常是登记名录整串
    （"科技推广和应用服务业 / TikTok海外B2B精准获客、外贸社媒全案营销"），
    原样进 f"{geo}{trade}哪家好？" 就是没人会问的鬼话。提炼顺序：
      1. 客户核心词里挑最短的服务型短词（"TikTok工厂代运营"这种最像人话）；
      2. industry 取 "/" 最后一段再取顿号/逗号第一段，且 ≤14 字才用；
      3. 兜底"相关服务"。

    [R6 · 2026-08-04] 本函数**只服务于 server.py 竞品调研主语纠偏**（检索 query，
    不进题面），所以走 ``for_question=False`` —— 行为与 R6 之前逐字节一致。
    出题面请用 ``distill_trades()``（它会拒绝分类名录用语并可能返回空）。
    """
    return distill_trades(industry, keywords, limit=1, for_question=False)[0]


def required_geo_layers(scope: str) -> tuple[str, ...]:
    """哪些层的题必须带地域限定。

    区域客户：决策获客层 + 场景转化层都必须带地域（他不在全国市场竞争）；
    全国客户：只有决策获客层带地域（那一层本来就是"行业+地区"），
              场景层允许无地域大词。
    品牌认知层永远不要求地域（问的是品牌本身）。
    """
    if normalize_business_scope(scope) == SCOPE_NATIONAL:
        return (LAYER_LOCAL,)
    return (LAYER_LOCAL, LAYER_SCENARIO)


def looks_like_real_question(text: Any) -> bool:
    """是否像一句真实用户问法（而不是服务名称/关键词短语）。"""
    raw = str(text or "").strip()
    if len(raw) < 4:
        return False
    if any(mark in raw for mark in _QUESTION_MARKS):
        return True
    if any(signal in raw for signal in _REAL_QUESTION_SIGNALS):
        return True
    return False


def looks_like_service_name(text: Any) -> bool:
    """是否是服务名称式短语（营销词堆叠 + 没有提问动作）。"""
    raw = str(text or "").strip()
    if not raw:
        return True
    if looks_like_real_question(raw):
        return False
    return any(marker in raw for marker in _SERVICE_NAME_MARKERS) or len(raw) <= 20


def has_geo_qualifier(text: Any, city: str, brand_cities: Any = "") -> bool:
    """题面是否带地域限定。

    [R1 · 2026-08-03] 判据从"整串城市值逐字命中"改成 **token 任一命中**：
    ``city`` 传的是**原始**城市串（"贵州省遵义市仁怀市（茅台镇）"），拆成
    ('贵州','遵义','仁怀','茅台镇') 后任一出现在题面即算带地域。
      · "仁怀市哪家酒厂…"          → 含"仁怀"      → True
      · "遵义茅台镇纯粮酱酒…"      → 含"遵义/茅台镇" → True
      · "酱香型白酒哪家好？"       → 一个都不含    → False（真没地域的题仍判缺）
      · "酱香酒和茅台的区别是什么？" → 只含"茅台"不含"茅台镇" → False
        （§7.2 品牌语境不算地域：镇级 token 不剥后缀就是为了挡这一格）

    [R5 · 2026-08-04] token 池再并上品牌档案 ``brands.cities``：
      · city="广东省，香港" / brand_cities="广东省深圳市龙岗区"
      · "深圳做私密整形哪家医院比较靠谱？" → 表单 token 一个都不含，档案 token 含"深圳"
        → True → **不再前缀成"广东深圳…"**（生产实证 529 的 4 条双地名赘字）
    """
    raw = str(text or "")
    if not raw:
        return False
    if any(token in raw for token in merged_geo_tokens(city, brand_cities)):
        return True
    # 用户可能自己写了别的地名/区域词；这些同样算带地域。
    return bool(re.search(r"(本地|附近|周边|同城|市内|省内|全国范围)", raw))


def dedupe_geo_prefix(town: str, text: str) -> str:
    """给题面补地域前缀,但**绝不制造重复地名**;返回补完的题面。

    [WO 2026-08-06 §3] 这是第二道防线。第一道是 ``_split_suffixless_geo`` ——
    它让 ``has_geo_qualifier`` 认出"贵阳小龙虾…"本来就带地域,压根不走到这里。
    但地名写法千奇百怪(简称/别称/老地名),token 池不可能穷尽,所以**拼接这一步
    自己也要能挡住重复**:判据不是"上游有没有判对",而是"我拼出来的串里有没有
    同一个地名出现两次"。

    规则(顺序不可换):
      1. 题面已含整串 town → 一个字都不加(原样返回);
      2. town 能拆成多段(省+市)且题面**已含其中任一段** → 也不加。
         这一条挡的正是 561 那 5 道题("贵州贵阳" + "贵阳小龙虾…")。
         注意是"任一段就不加"而不是"补上缺的那段":补出来是「贵阳贵州劳务派遣」
         —— 不重复了,但依然是没人会那么搜的鬼话。带了地域就是带了,别再叠。
      3. 其余 → 照旧整串前缀。**不含地名的题仍然要被加上前缀** —— 工单判据
         明写"别把前缀一起删",这一条就是那个反向对照点。
    """
    prefix = str(town or "").strip()
    body = str(text or "")
    if not prefix:
        return body
    if prefix in body:
        return body
    segments = [seg for seg in _split_suffixless_geo(prefix) if seg]
    if any(seg in body for seg in segments):
        return body
    return f"{prefix}{body}"


def build_question_templates(
    *,
    brand_name: str,
    industry: str,
    city: str,
    scope: str,
    keywords: Any = None,
    brand_cities: Any = "",
) -> dict[str, list[str]]:
    """按层给出真实问法模板池（对标监测侧真实词的句式）。

    [2026-07-27] trade 不再用 industry 原文——登记名录整串进题面就是鬼话；
    keywords 可选（不传=只按 industry 提炼，兼容旧调用）。

    [R2 · 2026-08-03] 池子**按品类词轮转**，不是按后缀轮转。生产实证 brand 745：
    6 个核心词只炼出 1 个品类词，8 题里 7 题成了"同 14 字前缀 + 换后缀"。
    品类词不足 2 个时才回落到后缀轮转，且**必须 log 说明回落原因**（不许静默）。
    """
    brand = str(brand_name or "").strip() or "该品牌"
    trades = distill_trades(industry, keywords, brand_name=brand_name)
    # [R5 · 2026-08-04] 主地名 = 表单值(到市级)优先，否则用品牌档案解析出的市级。
    town = resolve_primary_geo(city, brand_cities)
    geo = town or "本地"
    tokens = merged_geo_tokens(city, brand_cities)
    national_ok = normalize_business_scope(scope) == SCOPE_NATIONAL
    # [R7 · 2026-08-04] B2B 专属后缀（"哪家方案更适合中小企业"）默认关闭。
    audience = industry_audience(industry, keywords, trades[0] if trades else "")

    if not trades:
        # [R6 · 2026-08-04] 品类词炼不出 → **非品牌层模板池置空**，宁可层配额补不齐。
        # distill_trades 已 warning 留痕，这里再记一条带层信息的，便于报告层解释
        # "本层题数低于下限" 的原因。
        logger.warning(
            "[选词质量] 无可用品类词 → 决策获客层/场景转化层模板池置空"
            "(品牌 %r · industry=%r)；品牌认知层不受影响",
            str(brand_name or "")[:30], str(industry or "")[:60],
        )
    elif len(trades) < 2:
        logger.info(
            "[选词质量] 品类词仅 %d 个(%s) → 回落后缀轮转；"
            "核心词 %d 个里没有更多 4-14 字、非问句、不含品牌名的短词",
            len(trades), "/".join(trades), len(list(keywords or [])),
        )

    def _geo(trade: str) -> str:
        """品类词自带地名时不再加前缀（"仁怀茅台镇酱酒厂家招商"是鬼话）。"""
        return "" if any(tok in trade for tok in tokens) else geo

    def _pool(patterns: list[str], *, with_geo: bool) -> list[str]:
        """patterns 用 {geo}/{trade} 占位；按品类词轮转填充。

        [R6] ``trades`` 为空 → 返回空池（模板题宁可少出，也不出"深圳相关服务哪家好？"）。
        """
        if not trades:
            return []
        out: list[str] = []
        for idx, pattern in enumerate(patterns):
            trade = trades[idx % len(trades)]
            out.append(pattern.format(geo=_geo(trade) if with_geo else "", trade=trade))
        return out

    brand_pool = [
        f"{brand}是什么公司？",
        f"{brand}怎么样？靠谱吗",
        f"{brand}主要做什么业务？",
    ]
    local_pool = _pool([
        "{geo}{trade}哪家好？",
        "{geo}{trade}哪家靠谱？",
        "{geo}{trade}推荐几家",
        # [WO 2026-08-06 §2.2-2] 「怎么选？」「一般怎么收费？」两条原样在池里,
        # 但它们一个提名信号都没有 —— AI 答方法论/价格区间,**不点名厂商**,
        # 进诊断必然 0 命中还占一个付费槽。模板池是等槽替换的供给方,
        # 它自己产不合格题 = 出口修了兜底池还在产(brandq 同款教训)。
        # 换成同角度、但会让 AI 列出商家的问法。
        "{geo}{trade}哪几家口碑好？",
        "{geo}{trade}哪家性价比高？",
        "{geo}{trade}收费实惠的有哪几家？",
        # [R7] 末条带行业名词：B2B 说"公司"，C 端说"人气"（"深圳哪些医美公司口碑好"
        # 读着就不像消费者会问的；医美/餐饮的主语是机构/店，不是公司）。
        "{geo}哪些{trade}公司口碑好？" if audience == AUDIENCE_B2B
        else "{geo}{trade}哪家人气高？",
    ], with_geo=True)
    if national_ok and audience == AUDIENCE_B2B:
        scenario_pool = _pool([
            "{trade}哪家好？推荐几家靠谱的",
            "{trade}公司排名前十",
            # [WO 2026-08-06 §2.2-2] 「怎么选」型无提名信号 → 换成会让 AI 点名的问法。
            "{trade}公司有哪些？推荐几家靠谱的",
            "{trade}哪家最专业？",
            "{trade}一般怎么收费？哪家性价比高",
            "国内做{trade}比较成熟的公司有哪些",
        ], with_geo=False)
    elif national_ok:
        scenario_pool = _pool([
            "{trade}哪家好？推荐几家靠谱的",
            "{trade}排名前十有哪些？",
            # [WO 2026-08-06 §2.2-2] 「怎么选」型无提名信号 → 换成会让 AI 点名的问法。
            "{trade}公司有哪些？推荐几家靠谱的",
            "{trade}哪家最专业？",
            "{trade}一般怎么收费？哪家性价比高",
            "国内做{trade}比较有名的有哪些",
        ], with_geo=False)
    elif audience == AUDIENCE_B2B:
        # 区域客户的场景层也必须带地域，否则等于把他丢进全国竞品堆里比
        scenario_pool = _pool([
            "{geo}{trade}哪家方案更适合中小企业？",
            # [WO 2026-08-06 §2.2-2] 「怎么选」型无提名信号 → 换成会让 AI 点名的问法。
            "{geo}{trade}哪家不容易踩坑？推荐几家",
            "{geo}{trade}哪家做过类似案例？",
            "{geo}{trade}对比下来哪家更值得合作？",
            "{geo}{trade}哪家服务响应快？",
            "{geo}做{trade}的公司哪几家口碑好？",
        ], with_geo=True)
    else:
        # [R7 · 2026-08-04] C 端/到店行业（医美 / 餐饮 / 酒店 / 健身…）的场景层。
        # 生产实证 529：医美客户被出了"广东深圳龙岗医美哪家方案更适合中小企业？"
        # —— 医美的买方是个人求美者，"中小企业采购方案"这套话术整条都是鬼话。
        scenario_pool = _pool([
            "{geo}{trade}哪家口碑好？",
            # [WO 2026-08-06 §2.2-2] 「怎么选」型无提名信号 → 换成会让 AI 点名的问法。
            "{geo}{trade}哪家不容易踩坑？推荐几家",
            "{geo}{trade}哪家正规？",
            "{geo}{trade}哪家评价高？",
            "{geo}{trade}哪家体验好？",
            "{geo}做{trade}比较好的有哪几家？",
        ], with_geo=True)
    return {
        LAYER_BRAND: brand_pool,
        LAYER_LOCAL: local_pool,
        LAYER_SCENARIO: scenario_pool,
    }


def _layer_of(question: str, question_types: dict) -> str:
    layer = question_types.get(question)
    return layer if layer in FUNNEL_LAYERS else LAYER_SCENARIO


def enforce_question_quality(
    questions: list[str],
    question_types: dict,
    *,
    brand_name: str,
    industry: str,
    city: str = "",
    business_scope: str = SCOPE_REGIONAL,
    engine_count: int = 5,
    keywords: Any = None,
    max_tested_questions: int = 0,
    brand_cities: Any = "",
) -> dict[str, Any]:
    """就地修复选词质量；返回 {questions, question_types, repairs, layer_counts}。

    不缩减题量（SSOT §9.6 不静默丢词），只做**等槽替换**与**层配额补齐**。

    [Review-CTO 2026-07-27 · diagnosis 489] max_tested_questions>0 时做**保层重排**：
    下游 ai_test 只测前 N 题（diagnosis_workflow [:8]），而配额补齐原来只会
    append 到尾部 → 补的场景层题全被切掉 → 报告"本层未实测"。重排保证截断后
    每层仍拿到自己的下限样本。不传（=0）行为与旧版完全一致。
    """
    from config.ai_engines import min_questions_per_layer

    scope = normalize_business_scope(business_scope)
    # [R5 · 2026-08-04] 主地名与 build_question_templates 必须同源：这里算 town 用于
    # "只缺地域 → 补前缀"，模板池内部也算一次。两处都走 resolve_primary_geo，
    # 否则前缀用"广东"、模板用"深圳"，同一次诊断里出现两套地名。
    town = resolve_primary_geo(city, brand_cities)
    geo_layers = required_geo_layers(scope)
    # [R1/R2 · 2026-08-03] 传**原始** city 串:模板内部自己 normalize 出主地名(仁怀),
    # 同时还要 geo_tokens 拿到下级地名(茅台镇)来判"品类词是否自带地名"。传归一后的
    # town 会丢掉镇级 token → "仁怀茅台镇酱酒厂家招商哪家好?" 这种双地名鬼话。
    templates = build_question_templates(
        brand_name=brand_name, industry=industry, city=city, scope=scope,
        keywords=keywords, brand_cities=brand_cities,
    )
    used: set[str] = set()
    repairs: list[dict[str, str]] = []

    def _take(layer: str) -> str | None:
        for candidate in templates.get(layer, []):
            if candidate not in used:
                used.add(candidate)
                return candidate
        return None

    out: list[str] = []
    out_types: dict[str, str] = {}

    for question in list(questions or []):
        text = str(question or "").strip()
        if not text:
            continue
        layer = _layer_of(text, question_types if isinstance(question_types, dict) else {})
        problems: list[str] = []
        # 注意：``looks_like_service_name`` 对"看起来是真实问句"的文本恒返回 False，
        # 所以它不能写成 elif 分支（那是永远进不去的死代码）。这里用它给出更准确的
        # 问题码：不像人话提问的短语里，带营销词堆叠的标成 service_name_phrase。
        if not looks_like_real_question(text):
            problems.append(
                "service_name_phrase" if looks_like_service_name(text) else "not_a_real_question"
            )
        # [R1 · 2026-08-03] 判地域用**原始** city 串（不是归一后的 town）：
        # 归一后只剩主地名"仁怀"，"遵义茅台镇纯粮酱酒…"这类带下级地名的好题
        # 会重新被判缺地域。geo_tokens 要拿到整串才能拆出全部层级。
        if layer in geo_layers and town and not has_geo_qualifier(text, city, brand_cities):
            problems.append("missing_geo_qualifier")

        if not problems:
            used.add(text)
            out.append(text)
            out_types[text] = layer
            continue

        # [R4 · 2026-08-03] 只缺地域 → **补前缀**，不整条推倒。
        # 旧逻辑一判不合格就从模板池抓一条顶上，原题里的品类信息（OEM贴牌 /
        # 基酒批发 / 招商代理）全部丢失 —— 生产实证 brand 745 的 6 条好题全没了。
        # 同时触发多个问题（既不像真实问句、又是服务名称式短语）才整条替换。
        if problems == ["missing_geo_qualifier"] and town:
            # [WO 2026-08-06 §3] 拼接自己挡重复,不靠上游判对(见 dedupe_geo_prefix)。
            prefixed = dedupe_geo_prefix(town, text)
            if prefixed != text and prefixed not in used:
                used.add(prefixed)
                out.append(prefixed)
                out_types[prefixed] = layer
                repairs.append({"original": text, "replacement": prefixed, "layer": layer,
                                "problems": ",".join(problems), "result": "prefixed"})
                continue

        replacement = _take(layer)
        if replacement is None:
            # 模板池枯竭 → 保留原题（绝不静默缩减），交报告层提示
            used.add(text)
            out.append(text)
            out_types[text] = layer
            repairs.append({"original": text, "replacement": "", "layer": layer,
                            "problems": ",".join(problems), "result": "template_pool_exhausted"})
            continue
        out.append(replacement)
        out_types[replacement] = layer
        repairs.append({"original": text, "replacement": replacement, "layer": layer,
                        "problems": ",".join(problems), "result": "replaced"})

    # ── 每层配额下限：题数 × 引擎数 ≥ 5（config.ai_engines SSOT）──
    floor = min_questions_per_layer(engine_count)
    layer_counts = {layer: sum(1 for q in out if out_types.get(q) == layer) for layer in FUNNEL_LAYERS}
    for layer in FUNNEL_LAYERS:
        while layer_counts[layer] < floor:
            addition = _take(layer)
            if addition is None:
                break
            out.append(addition)
            out_types[addition] = layer
            layer_counts[layer] += 1
            repairs.append({"original": "", "replacement": addition, "layer": layer,
                            "problems": "layer_below_sample_floor", "result": "added"})

    # ── 保层重排：下游只测前 max_tested_questions 题时,截断集必须每层先拿到
    #    floor 个样本,余位按原顺序回填;截断集之外的题原顺序缀后(不丢词)。──
    if max_tested_questions and len(out) > max_tested_questions:
        head: list[str] = []
        for layer in FUNNEL_LAYERS:
            picked = 0
            for q in out:
                if picked >= floor:
                    break
                if out_types.get(q) == layer and q not in head:
                    head.append(q)
                    picked += 1
        for q in out:
            if len(head) >= max_tested_questions:
                break
            if q not in head:
                head.append(q)
        tail = [q for q in out if q not in head]
        out = head + tail

    return {
        "questions": out,
        "question_types": out_types,
        "repairs": repairs,
        "layer_counts": layer_counts,
        "business_scope": scope,
        "city": town,
        "min_questions_per_layer": floor,
        # [R5/R7 · 2026-08-04] 落库快照补两项取证字段。529 事后复盘卡在
        # "闸内 city 是'广东'但档案是'深圳市龙岗区'" 上——当时快照只有归一后的
        # city，看不出**输入是什么、档案是什么、为什么选了它**。
        "city_inputs": {
            "form": str(city or ""),
            "brand_cities": str(brand_cities or ""),
            "geo_tokens": list(merged_geo_tokens(city, brand_cities)),
        },
        "audience": industry_audience(industry, keywords),
        "rule_version": "diagnosis-question-quality-v2",
    }
