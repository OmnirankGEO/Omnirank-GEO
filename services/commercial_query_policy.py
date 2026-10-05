"""
services/commercial_query_policy.py — 商业意图唯一策略合同(CommercialQueryPolicy)

SSOT: docs/DECISION/GEO_COMMERCIAL_INTENT_GOVERNANCE_SSOT_2026-07-23.md
     (geo-commercial-intent-governance-v1.0)

【唯一引擎原则 · Review-CTO 2026-07-23 NO-GO 返工】
本模块内的 `_buyer_intent_gate()` 是全系统唯一的商业意图文本判定引擎
(自 tools/keyword_expander._check_geo_feasibility 原样移入,行为等价):
  - 报价扩词 `KeywordExpander._check_geo_feasibility` **委托本引擎**;
  - 报价提交(自定义词/快照分区)走 `evaluate()`;
  - 诊断 8 问生成走 `evaluate()` 校验 + 等槽修复;
  - 监测/写作消费的是已经通过本合同准入的确认问题/购买关键词
    (§4.3 不可变复用 · 不在消费端二次过滤丢弃付费数据),
    消费端对可疑遗留数据只做 advisory 标注。
禁止在任何入口另行发明相反的"优质问题"标准。

统一返回:policy_version / commercial_delivery_eligible / intent_type /
reason_codes / normalized_question / needs_clarification

分级语义(§3.2 / §3.3 / §5 · **2026-07-26 报价纠偏工单 P0-4 修订为 advisory**):
  - eligible=True  → 可进入付费交付;证据/表达/歧义问题只能 advisory +
    局部修复 + 人工确认继续。
  - eligible=False + intent_type in (knowledge, seo_fragment)
    → **默认**不被选择、不计价、不确认、不监测,在「未进入交付」区展示原因;
      **但操作员可人工放行**(记审计 · 依据裁决 D8「放行权归用户」)。
      纯 regex 文本判定不是可以物理禁用户的理由 —— 生产实证它会把
      "…培训哪里正规"这类真问法判成知识词。
  - eligible=False + needs_clarification=True(uncertain)
    → 「需澄清」区:不得静默删除;澄清或人工放行前不计价/确认/监测/生成。
  - `hard_block_reason()` 命中 → **物理禁选**,人工也放行不了。
    仅限四条硬边界:法律 / 资金 / 越权 / 数据完整性。

本模块是纯规则层(无 LLM、无 DB、无网络)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

POLICY_VERSION = "geo-commercial-intent-governance-v1.0"

INTENT_COMMERCIAL = "commercial"        # 推荐/比较/采购/价格/选型类 → 付费交付
INTENT_BRAND_DIRECT = "brand_direct"    # 品牌直问 → 实体识别对照(可交付,不挤占推荐位)
INTENT_KNOWLEDGE = "knowledge"          # 定义/教程/趋势/政策/核验方法 → 不进付费交付
INTENT_SEO_FRAGMENT = "seo_fragment"    # 裸短词/词根/不自然片段 → 不进付费交付
INTENT_UNCERTAIN = "uncertain"          # 未证明购买意图 → 需澄清区(不得静默删除)

_WHITESPACE_RE = re.compile(r"\s+")

_PROVIDER_NOUNS = (
    "品牌", "厂家", "公司", "机构", "平台", "服务商", "供应商", "门店",
)

# ============================================================================
# 商业对象 × 商业动作(T1 · WO_QUOTE_KEYWORD_GEO_COMMERCIAL_DOUBLE_INVERSION 2026-08-09)
# ============================================================================
# 【为什么加这一层】
# 老 `_PROVIDER_NOUNS` 只认「谁来提供服务」(公司/厂家/服务商),不认**实体经营场所**
# 与**招商交易**。于是深圳龙岗一个商业项目客户的六条真成交问法
#   深圳龙岗商场招商电话 / 深圳龙岗建材市场有哪些 / 深圳龙岗租商铺做餐饮哪里合适 …
# 全部落到 `UNPROVEN_BUYER_INTENT`,而「商业综合体设计公司」只因带"公司"二字被放行。
#
# 【为什么不是词表白名单】(工单 §8 明令禁止)
# 这里登记的是**两类结构成分**——商业对象(名词)与商业动作(动词/交易行为),
# 判定一律要求**组合**成立。单独一个 `在哪 / 有哪些 / 电话` 绝不构成商业信号,
# 否则 `商场洗手间在哪` 会被误收(工单 T1 点名的反例)。
#
# 【顺序铁律】本节全部规则排在 `_NO_RECOMMEND_PATTERNS` / `_COMMERCIAL_KNOWLEDGE_PATTERNS`
# **之后**:知识题先被拦掉,才轮到商业组合判定。所以
#   商场是什么 / 商场招商流程 / 建材市场发展趋势 / 租商铺需要什么证件
# 依旧是知识题 —— 它们分别命中 `是什么` / `流程` / `趋势` / `需要什么`。
# 改动这个顺序会让上面四条反例全部翻车,`tests/quotegeo_2026_08_10/` 里有成对判据钉死。

# 商业对象 · 实体经营场所与可交易铺位(名词)
_VENUE_NOUNS = (
    "商业综合体", "商业广场", "购物中心", "购物广场", "商业中心",
    "批发市场", "建材市场", "家具城", "农贸市场", "专业市场",
    "商场", "商超", "超市", "市场",
    "商铺", "店铺", "铺位", "旺铺", "档口", "摊位", "店面",
    "写字楼", "办公楼", "产业园", "工业园", "创意园", "园区",
    "展位", "场地", "厂房", "仓库",
)

# 商业动作 · 招商/租赁/开店/采购这类**会促成交易**的行为(动词或交易名词)
_TRADE_ACTIONS = (
    "招商", "招租", "入驻", "进驻", "开店", "开业", "选址", "转让", "转租",
    "出租", "租赁", "承租", "加盟", "进货", "批发", "团购", "订购", "拿货",
)

_VENUE_GROUP = "|".join(_VENUE_NOUNS)

# 规则 A 之补:单字交易动词(租/买/开/盘)必须**紧贴商业对象**才算数,
# 防"出租车""买单""开心"这类含字不含义的误收。
_TRADE_VERB_VENUE_RE = re.compile(
    rf"(?:租|承租|转租|买|购|盘|开|找)(?:下|个|间|家|块)?(?:{_VENUE_GROUP})"
)

# 规则 B:方位/枚举/联系方式疑问必须**紧跟在商业对象之后**。
#   深圳龙岗建材市场有哪些   → "市场" 紧接 "有哪些"      ✅ 商业
#   深圳龙岗办公家具批发市场在哪 → "批发市场" 紧接 "在哪"   ✅ 商业
#   商场洗手间在哪            → "商场" 后面是"洗手间"     ❌ 不是商业(工单点名反例)
_VENUE_LOCATOR_RE = re.compile(
    rf"(?:{_VENUE_GROUP})(?:在哪儿?里?|有哪些|有几家|在什么地方|地址|电话|联系方式|怎么走)"
)

# 规则 C:购买/租赁动作 + 地点选择疑问 = 典型到店消费问法,不要求出现场所名词。
#   深圳龙岗买家具建材去哪里好 → "买" + "去哪里好"  ✅
#   租商铺需要什么证件          → 无地点疑问,且先被知识题拦掉 ❌
_BUY_WHERE_RE = re.compile(
    r"(?:买|购买|采购|批发|进货|订购|拿货|租|租赁|承租)"
    r"[^,，。?？]{0,10}"
    r"(?:去哪儿?里?|在哪儿?里?|哪里买|哪里好|哪里合适|哪里便宜|哪儿好|上哪买)"
)

# —— 以下判定规则原样移入自报价扩词硬门(经 test_quote_keyword_buyer_intent_gate
#    91 例 + §六.1/§六.2 矩阵长期锤炼),是唯一权威文本引擎 ——

_ANTI_RECOMMEND_SIGNALS = ("不推荐", "不建议", "推荐标准", "评价标准", "审核标准")

_KNOWLEDGE_OBJECT_PATTERNS = [
    r"(选择|评估|选型|选购|审核|准入|推荐).{0,4}(标准|规则|要求|条件|方法|流程|口径)",
    r"资质.{0,4}(要求|条件|标准|规则|有哪些)",
    r"(排名|排行|报价|价格|费用|成本|对比|比较).{0,8}(算法|规则|计算|公式|组成|方法|口径|标准)",
    r"(算法|规则|公式|口径).{0,8}(是什么|有哪些|怎么|如何)",
    # [SSOT §3.1] "怎么选/如何挑选"是合法选型问题;仅"怎么计算/如何评估"
    # 这类询问方法本身的组合按知识题处理。
    r"(怎么|如何).{0,4}(计算|评估)",
    r"(价格|报价|费用|成本).{0,8}(由什么组成|怎么组成|如何组成)",
    r"(案例|标准|规范|政策|法规|流程|方法|效果|真实性|数据|参数|功能|原理|机制).{0,8}(对比|比较)",
    r"(收费|费用|价格|报价|成本).{0,8}(包含|包括|涵盖|明细|构成|组成).{0,6}(什么|哪些|内容)?",
]

_COMMERCIAL_KNOWLEDGE_PATTERNS = [
    r"(价格|费用|收费|报价|预算|成本).{0,12}(政策|法规|流程|步骤|走势|趋势|解读|定义|是什么意思|怎么做|如何做|怎么计算|如何计算|计算方法|组成|公式|算法|口径)",
    r"(政策|法规|流程|步骤|走势|趋势|解读|定义|是什么意思|怎么做|如何做|怎么计算|如何计算|计算方法|组成|公式|算法|口径).{0,12}(价格|费用|收费|报价|预算|成本)",
    # [Review-CTO 2026-07-27 · Owner 点名 session 201 残留] 泛均价问法 =
    # 市场行情科普,AI 回价格区间不点名商家("深圳TikTok代运营公司收费一般
    # 多少"借"公司/收费"信号漏过)。只杀"一般/大概/平均+多少"的均价形,
    # 价格决策形(收费对比 / 哪家性价比高 / 预算X选哪家 / 一般怎么收费)不碰
    # ——价格询盘是 6 层矩阵正当层,一刀切杀价格词会拐回老护栏的坑。
    r"(收费|价格|费用|报价|成本)(一般|大概|大约|通常|平均)(是|要)?(多少|几)",
    r"(一般|大概|大约|通常|平均)(收费|价格|费用|报价)(是|要)?(多少|几)",
    r"(大概|大约|平均|通常)(要|得)?多少钱",
    r"(平均|市场)(价格|收费|费用|价位|行情)",
]

_NO_RECOMMEND_PATTERNS = [
    r"是什么|什么是|的定义|的概念|的含义|是什么意思",
    r"原理|机制|为什么|的原因|怎么回事",
    r"怎么做|如何做|怎么办|自己做|教程|步骤|流程",
    r"注意事项|注意什么|需要什么",
    r"政策|法规|规范|规定|条例",
    r"选择标准|评估标准|选型标准|选购标准|资质要求|资质条件",
    # [T1 2026-08-09] 规范/标准类问法 = 问规则本身,不会让 AI 推荐商家。
    #   `商业综合体设计标准` 必须留在知识题(工单 §5.5 反例)。
    #   刻意**不收**「服务标准」——那一条与供应商尽调(§3.1)有重叠,不在本单范围。
    r"(设计|建设|施工|验收|规划|技术|国家|行业)标准",
    # [Review-CTO 2026-07-23] 「优缺点」自反例中移除:SSOT §3.1 明列
    # 「对比、优缺点、口碑」为合法商业方向;方法论型对比(参数对比方法等)
    # 已由 _KNOWLEDGE_OBJECT_PATTERNS 先行识别。
    r"区别|利弊|种类|分类|类型|有几种",
    r"行情|走势|趋势|前景|现状|发展|市场价",
    r"历史|发展史|起源",
    r"案例分享|案例介绍|案例解析|成功案例$|案例展示$|案例汇总$|案例盘点$",
    r"就业率|通过率|成功率|录取率|转化率",
    r"实战多吗|真实吗|可靠真实吗|数据真实吗",
    r"有.{1,24}吗$|提供.{1,24}吗$|能不能|会不会|可不可以",
]

_DIRECT_CHOICE_SIGNALS = [
    "推荐", "哪家好", "哪个好", "排名", "排行", "前十", "前五",
    "十佳", "榜单", "找谁", "去哪找", "去哪买", "哪里买",
    "哪家", "哪个平台", "选哪个", "选哪家", "买哪一个",
    "哪个值得买", "值得买吗", "哪个更适合", "哪个适合", "更适合",
    "替代品", "国产替代", "口碑",
    "价格", "多少钱", "报价", "费用", "收费", "预算",
    "性价比", "月供", "首付", "长租", "包月",
    # [SSOT §3.1] 采购/选型/尽调是核心商业意图;知识组合(采购流程/
    # 预算怎么做)已被上方知识题反例先行拦截。
    "采购", "选型", "尽调",
]

_PROVIDER_GROUP = "|".join(_PROVIDER_NOUNS)

# 提供方信任问法("XX公司靠谱吗/正规吗")= 供应商尽调式商业问题(§3.1),
# 与泛核验("就业率真实吗")区分:必须带供给方名词。
_PROVIDER_TRUST_RE = re.compile(
    rf"(?:{_PROVIDER_GROUP}).{{0,12}}(靠谱|正规|专业|可靠)吗"
)


def normalize_question(text: str) -> str:
    """归一:去首尾空白 · 合并连续空白,不改写语义。"""
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", str(text)).strip()


def _gate_normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip("，,。.!！?？;；:：")


def _buyer_intent_gate(keyword: str) -> tuple[bool, str]:
    """唯一商业意图文本引擎 · 返回 (eligible, reason_code)。

    True = 该问题会促使 AI 给出品牌/服务商/产品/方案/价格/比较选项;
    False = 知识/核验/未证明购买意图(fail-closed,自动扩词默认不进)。
    """
    kw = _gate_normalize(keyword)
    if len(kw) < 3:
        return False, "TOO_SHORT_FRAGMENT"

    has_provider = any(noun in kw for noun in _PROVIDER_NOUNS)

    if any(signal in kw for signal in _ANTI_RECOMMEND_SIGNALS):
        return False, "KNOWLEDGE_RULE_QUERY"

    if any(re.search(pattern, kw) for pattern in _KNOWLEDGE_OBJECT_PATTERNS):
        return False, "KNOWLEDGE_METHOD_QUERY"

    if has_provider and re.search(r"^有.{0,20}(靠谱|正规|合适|专业|附近|本地|预算内).{0,20}吗$", kw):
        return True, "PROVIDER_AVAILABILITY_QUERY"

    # [Review-CTO 2026-07-23] "深圳装修公司靠谱吗"类供给方信任问法 =
    # 供应商尽调(§3.1),先于泛"…吗"核验反例放行。
    if _PROVIDER_TRUST_RE.search(kw):
        return True, "PROVIDER_TRUST_QUERY"

    if re.search(rf"(有哪些.{{0,20}}(?:{_PROVIDER_GROUP})|(?:{_PROVIDER_GROUP}).{{0,20}}有哪些)", kw):
        return True, "PROVIDER_LIST_QUERY"
    if has_provider and any(signal in kw for signal in ("名录", "名单", "清单")):
        return True, "PROVIDER_LIST_QUERY"

    if any(re.search(pattern, kw) for pattern in _COMMERCIAL_KNOWLEDGE_PATTERNS):
        return False, "PRICE_KNOWLEDGE_QUERY"

    if any(re.search(pattern, kw) for pattern in _NO_RECOMMEND_PATTERNS):
        return False, "KNOWLEDGE_PATTERN"

    # —— 商业对象 × 商业动作(T1)。必须排在上面全部知识题反例**之后** ——
    has_venue = any(noun in kw for noun in _VENUE_NOUNS)
    has_trade = any(action in kw for action in _TRADE_ACTIONS)

    # A. 场所 + 交易行为:`深圳龙岗商场招商电话` / `深圳龙岗哪个商场适合租商铺开店`
    if has_venue and has_trade:
        return True, "VENUE_TRADE_QUERY"
    # A'. 单字交易动词紧贴场所:`租商铺做餐饮哪里合适`
    if _TRADE_VERB_VENUE_RE.search(kw):
        return True, "VENUE_TRADE_QUERY"
    # B. 场所 + 紧邻的方位/枚举/联系方式疑问:`建材市场有哪些` / `批发市场在哪`
    #    紧邻是判别力所在 —— `商场洗手间在哪` 在这里必须落空。
    if _VENUE_LOCATOR_RE.search(kw):
        return True, "VENUE_LOCATOR_QUERY"
    # C. 购买/租赁动作 + 地点选择疑问:`买家具建材去哪里好`
    if _BUY_WHERE_RE.search(kw):
        return True, "PURCHASE_LOCATION_QUERY"

    if has_provider:
        return True, "PROVIDER_OBJECT"

    if any(signal in kw for signal in _DIRECT_CHOICE_SIGNALS):
        return True, "DIRECT_CHOICE_SIGNAL"
    if re.search(r"(前\d+|TOP\d+)", kw, re.IGNORECASE):
        return True, "RANKING_FORM"

    if re.search(r"(哪款|哪个型号|哪种型号).{0,24}(好|合适|适合|推荐|值得买|性价比高)", kw):
        return True, "MODEL_CHOICE_QUERY"
    if re.search(r"(哪款|哪个型号|哪种型号).{1,24}", kw):
        return True, "MODEL_CHOICE_QUERY"
    if re.search(r"用什么.{0,24}(产品|系统|设备|方案|软件|工具|平台)", kw):
        return True, "PRODUCT_CHOICE_QUERY"
    if re.search(r"(适合.{1,20}的)?.{1,24}(产品|系统|设备|方案|软件|工具).{0,8}有哪些", kw):
        return True, "PRODUCT_LIST_QUERY"

    if re.search(r"(哪里有|附近).{1,24}(店|公司|机构|厂家|供应商|服务商|平台|产品|设备|方案)", kw):
        return True, "LOCAL_SUPPLY_QUERY"
    if re.search(r"(找|寻找).{1,24}(公司|机构|厂家|供应商|服务商|平台|产品|设备|方案)", kw):
        return True, "SUPPLY_SEARCH_QUERY"

    if "对比" in kw or "比较" in kw:
        return True, "COMPARISON_QUERY"

    return False, "UNPROVEN_BUYER_INTENT"


def buyer_intent_eligible(text: str) -> bool:
    """报价扩词硬门委托入口(fail-closed 布尔语义,行为与原硬门等价)。"""
    ok, _ = _buyer_intent_gate(text)
    return ok


@dataclass(frozen=True)
class PolicyDecision:
    """CommercialQueryPolicy 的统一返回合同。"""

    policy_version: str
    commercial_delivery_eligible: bool
    intent_type: str
    reason_codes: tuple[str, ...]
    normalized_question: str
    needs_clarification: bool = False

    def as_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "commercial_delivery_eligible": self.commercial_delivery_eligible,
            "intent_type": self.intent_type,
            "reason_codes": list(self.reason_codes),
            "normalized_question": self.normalized_question,
            "needs_clarification": self.needs_clarification,
        }


_QUESTION_HINTS = ("吗", "?", "？", "怎么", "如何", "哪", "什么", "几")
_SEO_FRAGMENT_MAX_LEN = 6


def evaluate(
    text: str,
    *,
    intent_hint: str | None = None,
    brand_name: str | None = None,
) -> PolicyDecision:
    """对一个问题/关键词做商业交付资格裁决(唯一引擎)。

    intent_hint: 调用方已有的 LLM intent。文本引擎判商业时优先于 hint
        (防模型把"哪家好"误标 informational 后静默丢单,§3.3);
        引擎无法证明意图时,hint=informational 归入知识词。
    brand_name: 提供时,含品牌名的直问判为 brand_direct(实体识别对照,
        含"XX靠谱吗"类品牌信任题,§4.2)。
    """
    normalized = normalize_question(text)
    if not normalized:
        return PolicyDecision(
            POLICY_VERSION, False, INTENT_SEO_FRAGMENT,
            ("EMPTY_QUERY",), normalized,
        )

    # 品牌直问 = 实体识别对照,不被知识/核验反例误杀
    if brand_name and brand_name.strip() and brand_name.strip() in normalized:
        return PolicyDecision(
            POLICY_VERSION, True, INTENT_BRAND_DIRECT,
            ("BRAND_DIRECT",), normalized,
        )

    eligible, code = _buyer_intent_gate(normalized)
    if eligible:
        return PolicyDecision(
            POLICY_VERSION, True, INTENT_COMMERCIAL,
            (f"GATE:{code}",), normalized,
        )

    # —— 引擎判不进付费交付 → 细分知识 / 裸词 / 需澄清 ——
    if code in ("KNOWLEDGE_RULE_QUERY", "KNOWLEDGE_METHOD_QUERY",
                "PRICE_KNOWLEDGE_QUERY", "KNOWLEDGE_PATTERN"):
        return PolicyDecision(
            POLICY_VERSION, False, INTENT_KNOWLEDGE,
            (f"GATE:{code}",), normalized,
        )

    hint = (intent_hint or "").strip().lower()
    if hint == "informational":
        return PolicyDecision(
            POLICY_VERSION, False, INTENT_KNOWLEDGE,
            (f"GATE:{code}", "INTENT_HINT:informational"), normalized,
        )

    gate_kw = _gate_normalize(normalized)
    has_question_shape = any(h in normalized for h in _QUESTION_HINTS)
    if (
        code == "TOO_SHORT_FRAGMENT"
        or (len(gate_kw) <= _SEO_FRAGMENT_MAX_LEN and not has_question_shape)
    ):
        return PolicyDecision(
            POLICY_VERSION, False, INTENT_SEO_FRAGMENT,
            (f"GATE:{code}", "SEO_FRAGMENT_SHORT"), normalized,
        )

    # 未证明购买意图 → 需澄清区(§3.3:不得静默删除;澄清前不得计价/
    # 确认/监测/生成)。
    return PolicyDecision(
        POLICY_VERSION, False, INTENT_UNCERTAIN,
        (f"GATE:{code}", "NEEDS_CLARIFICATION"), normalized,
        needs_clarification=True,
    )


# ============================================================================
# 四条硬边界(2026-07-26 报价纠偏工单 P0-4)
# ============================================================================
# 物理禁选**只**保留下面四类;其余全部 advisory(默认不选 + 显示原因 + 可人工放行)。
#   法律   —— 违法/违规内容,放行会让平台承担法律责任
#   资金   —— 会造成错误扣费/错误结算的输入
#   越权   —— 越过 RBAC 边界的输入
#   数据完整性 —— 破坏快照/计价结构的输入(空词、超长词)
HARD_BLOCK_LEGAL = "HARD_BLOCK_LEGAL"
HARD_BLOCK_FUNDS = "HARD_BLOCK_FUNDS"
HARD_BLOCK_AUTHZ = "HARD_BLOCK_AUTHZ"
HARD_BLOCK_DATA_INTEGRITY = "HARD_BLOCK_DATA_INTEGRITY"

# 关键词最大长度:超过即无法作为自然问法,也会撑爆快照/标题生成(数据完整性边界)
MAX_KEYWORD_LENGTH = 40


def hard_block_reason(text: str) -> str:
    """返回硬边界代码;非硬边界返回空串(= 允许人工放行)。

    ⚠️ 这里只判**结构性**硬边界,不判"这个问法商业不商业"——
    后者是 advisory,由 `_buyer_intent_gate` 标注、由人决定。
    """
    normalized = normalize_question(text)
    if not normalized:
        return HARD_BLOCK_DATA_INTEGRITY
    if len(_gate_normalize(normalized)) > MAX_KEYWORD_LENGTH:
        return HARD_BLOCK_DATA_INTEGRITY
    return ""


def human_override_allowed(text: str) -> bool:
    """该词是否允许操作员人工放行进付费交付(裁决 D8)。"""
    return not hard_block_reason(text)


def is_knowledge_or_fragment(decision: PolicyDecision) -> bool:
    """知识词/SEO 片段 → **默认**不选、不计价;人工放行需走审计(§3.2 · P0-4 修订)。"""
    return decision.intent_type in (INTENT_KNOWLEDGE, INTENT_SEO_FRAGMENT)


def excluded_from_paid_delivery(decision: PolicyDecision) -> bool:
    """是否不得进入计价/确认/监测/生成:知识词、裸词、以及待澄清词(§3.2/§3.3)。

    区别:知识词/裸词无人工改选入口;待澄清词澄清后可重新提交。
    """
    return not decision.commercial_delivery_eligible


def eligible_for_paid_delivery(text: str, **kwargs) -> bool:
    """便捷判断:是否可进入付费交付。"""
    return evaluate(text, **kwargs).commercial_delivery_eligible
