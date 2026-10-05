"""
GEO 关键词扩展模块
双引擎：LLM 语义拓词 + 5118 API 数据验证

核心方法论：
- GEO 关键词 ≠ SEO 关键词
- SEO 关键词面向搜索引擎的索引机制（匹配词频、TF-IDF）
- GEO 关键词面向 AI 的语义理解（匹配用户意图、对话场景）
- 好的 GEO 关键词 = 用户真实会问 AI 的一句话，且 AI 回答时有理由推荐具体品牌
"""
import asyncio
import json
import os
import re
from typing import Optional
import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv('geo_agentscope/.env')
    load_dotenv('.env')
except Exception:
    pass

from .api_5118 import get_5118_client

try:
    # 统一商业意图合同(SSOT geo-commercial-intent-governance-v1.0)
    from services.commercial_query_policy import POLICY_VERSION as _POLICY_VERSION
    from services.commercial_query_policy import hard_block_reason
except Exception:  # pragma: no cover - 独立脚本运行时兜底,不改变判定逻辑
    _POLICY_VERSION = "geo-commercial-intent-governance-v1.0"

    def hard_block_reason(_text: str) -> str:  # type: ignore[misc]
        return ""

# [工单 GEO_COMMERCIAL_DOUBLE_INVERSION 2026-08-09 · T2/T3/T4]
#   三轴交付决策(商业意图 / 业务范围 / 地域范围)唯一合同。
#   扩词层从此**不再**只读一个布尔 `buyer_intent_eligible()`,而是消费完整决策。
from services.keyword_delivery_decision import (
    DELIVERY_POLICY_VERSION,
    GEO_MATCHED,
    GROUP_GEO_OUTSIDE,
    GROUP_KNOWLEDGE,
    GROUP_NEEDS_CONFIRM,
    REASON_GEO_OUTSIDE,
    REASON_INTENT_KNOWLEDGE,
    SCOPE_MISMATCHED,
    BusinessProfile,
    GeoContext,
    build_business_profile,
    decide as decide_keyword_delivery,
    group_for_reason,
)

# `_hard_filter` 结构性丢弃的真实理由码(R5:操作员必须看得出该放行、改词还是删)
DROP_GEO_OUTSIDE_MARKET = REASON_GEO_OUTSIDE          # 其他城市/地区
DROP_FORMAT_UNNATURAL = "FORMAT_UNNATURAL"            # 括号注释 / 整串行政地址
DROP_GEO_TOO_DEEP = "GEO_SCOPE_TOO_DEEP"              # 街道/片区级(超出该层级允许深度)
DROP_KNOWLEDGE_RESIDUE = REASON_INTENT_KNOWLEDGE      # 知识类残留

# 结构性理由码 → 前端分组(未登记的一律落"需要确认",不静默丢弃)
_DROP_REASON_TEXT = {
    DROP_GEO_OUTSIDE_MARKET: "这个词写的是别的城市/地区,超出客户当前服务市场。",
    DROP_FORMAT_UNNATURAL: "这个词带括号注释或整串行政地址,真人不会这样问 AI,建议改写后再用。",
    DROP_GEO_TOO_DEEP: "这个词精确到街道/片区,比客户的服务层级更细,先放这里等你确认。",
    DROP_KNOWLEDGE_RESIDUE: "这是知识/百科类问法,AI 回答时通常不会点名商家,默认不计入交付。",
}
_DROP_REASON_GROUP = {
    DROP_GEO_OUTSIDE_MARKET: GROUP_GEO_OUTSIDE,
    DROP_FORMAT_UNNATURAL: GROUP_NEEDS_CONFIRM,
    DROP_GEO_TOO_DEEP: GROUP_NEEDS_CONFIRM,
    DROP_KNOWLEDGE_RESIDUE: GROUP_KNOWLEDGE,
}

# [报价纠偏工单 2026-07-26 · P0-1/P0-2/P0-3] 范围锁定裁决 · 行政区划解析
from services.quote_scope_lock import (
    DEFAULT_MARKET_LEVEL,
    ScopeLock,
    city_scope_from_market_level,
    contains_street_level,
    geo_policy_for,
    is_full_address_form,
    normalize_market_level,
    parse_region_hierarchy,
    scope_lock_from_dict,
)

_GEO_DEPTH_LABELS = {
    "city": "城市级",
    "district": "区/县级",
    "street": "街道/片区级",
}

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")


# ========================================
# GEO 关键词方法论 Prompt（核心资产）
# ========================================

GEO_KEYWORD_METHODOLOGY = """## GEO 关键词方法论

### 核心试金石（每个词必须通过此检验！）

对每一个候选关键词，你必须做一次思想实验：

> **"假设用户把这句话发给 DeepSeek/Kimi，AI 的回答里会不会自然地列出具体的公司名或品牌名？"**

- 通过 -> 保留。如 "乌鲁木齐以租代购哪家公司正规" -> AI 会列出几家本地公司
- 不通过 -> 删除。如 "以租代购要注意哪些坑" -> AI 只会列注意事项，不提任何公司
- 不通过 -> 删除。如 "黑名单用户买车流程复杂吗" -> AI 只会解释流程，不推荐公司
- 不通过 -> 删除。如 "新能源车冬天续航会衰减吗" -> AI 只会科普，不推荐品牌

**判断技巧**：如果问题的答案是"知识/流程/道理"，AI 就不需要推荐公司。
只有当问题的答案必须包含"谁能提供这个服务/产品"时，AI 才会推荐。

---

### 第一原则：什么样的问法能让 AI 推荐品牌

AI 推荐品牌的本质：用户的问题暗含了"帮我选一个"的需求，AI 必须给出具体选项才算回答了问题。

**会推荐的问法模式**：
| 模式 | 为什么会推荐 | 示例 |
|------|------------|------|
| "推荐/哪家好" | 直接索要推荐列表 | "乌鲁木齐以租代购公司推荐" |
| "XX 去哪里买/找谁" | 需要具体服务商 | "新疆30万SUV去哪买靠谱" |
| "帮我选/适合XX的" | 需要具体方案 | "预算20万适合新疆跑的SUV推荐" |
| "排名/前十" | 索要排行榜 | "新疆二手车商口碑排名" |
| "XX多少钱"（服务类）| 需要报价就得提供商 | "乌鲁木齐以租代购月供多少" |
| "哪个平台/渠道靠谱" | 索要渠道推荐 | "新疆买二手车哪个平台靠谱" |

**不会推荐的问法模式（必须排除！）**：
| 模式 | 为什么不推荐 | 示例 |
|------|------------|------|
| "XX是什么/什么意思" | AI 只解释概念 | "以租代购是什么意思" |
| "XX流程/怎么办理" | AI 只列步骤 | "以租代购的流程是怎样的" |
| "要注意什么/有什么坑" | AI 只给建议 | "买二手车要注意哪些坑" |
| "XX会不会/能不能" | AI 只回答是/否 | "征信不好能以租代购吗" |
| "XX和XX有什么区别" | AI 只对比概念 | "以租代购和贷款买车有什么区别" |
| "XX政策/规定" | AI 只罗列规定 | "新疆二手车过户需要什么手续" |

**注意**：有些"避坑/注意事项"类的问题，如果加上地域+服务商选择的语境，可以变成推荐词：
- 差: "以租代购要注意哪些坑" -> 知识问答，AI 列注意事项
- 好: "乌鲁木齐哪家以租代购公司比较正规不会坑人" -> AI 会推荐靠谱的公司

### 第二原则：用户购买旅程中的推荐触发点

不是用户旅程的每个阶段都适合 GEO。只关注**AI 会推荐品牌**的阶段：

| 阶段 | 适合 GEO？ | 典型问法 |
|------|----------|---------|
| 有需求，找服务商 | YES | "XX推荐"、"去哪里XX"、"找谁做XX" |
| 比较服务商 | YES | "XX排名"、"哪家XX性价比高" |
| 确认价格 | YES（服务类）| "XX大概多少钱"、"XX报价" |
| 了解知识 | NO | "XX是什么"、"XX流程"、"XX注意事项" |
| 了解政策 | NO | "XX需要什么条件"、"XX政策" |
| 售后问题 | NO | "XX坏了怎么办"、"XX能退吗" |

### 第三原则：地域下钻 - GEO 的低垂果实

地域词是 GEO 优化中**性价比最高**的词类：
- 竞争度低：大品牌很少专门优化"南山区装修公司推荐"
- 精准度高：用户带地域搜索时购买意图强
- 覆盖面广：一个城市可以拆出十几个区/片区，每个区都是独立的关键词战场

**地域下钻方法**：
- 省/自治区 -> 拆到主要地级市（如新疆 -> 乌鲁木齐、喀什、伊犁、昌吉...），关键词直接用城市名
- 直辖市（北京/上海/天津/重庆） -> 拆到主要城区，但关键词必须带城市名前缀（如"上海浦东新区XX推荐"，不是"浦东新区XX推荐"）
- 地级市 -> 拆到主要城区/商圈（如深圳 -> 南山区、福田区、宝安区...）
- 每个子区域 × 核心业务词 = 一组高价值地域关键词"""


# ========================================
# Step 0: 地域下钻 Prompt
# ========================================

GEO_REGION_DRILL_PROMPT = """你是中国区域经济和本地生活专家。请对以下地区进行下钻拆解，并根据人口规模、经济活跃度、市场容量为每个子区域分配权重。

## 输入地区
{region}

## 下钻深度上限（**最高优先级 · 越界即错**）
{depth_rule}

## 拆解规则
1. 如果是**省/自治区**：列出该省内有商业活力的主要地级市（5-10个），加上常用的区域俗称（如"北疆"、"南疆"、"珠三角"）。region_level 填 "province"。
2. 如果是**直辖市**（北京、上海、天津、重庆）：列出主要城区/区县（5-10个），加上常用俗称（如"魔都"、"申城"、"帝都"）。region_level 填 **"municipality"**（不是 province！）。
   - 直辖市与省的关键区别：用户搜索时通常会带上直辖市名作为前缀，如"上海浦东新区XX推荐"而非单独"浦东新区XX推荐"。
3. 如果是**地级市**：列出主要城区/区县（5-10个），加上知名商圈/片区。region_level 填 "city"。
4. 如果是**区/县**：列出主要街道/镇/片区（3-5个）。region_level 填 "district"。
5. 如果无法识别或是"全国"：返回空列表。region_level 填 "unknown"。
6. 上面第 1-4 条一律**服从"下钻深度上限"**：超过上限的层级必须返回空列表，不许"顺手多给几个"。

## 权重分配规则（重要！）
- 权重代表该子区域应该分配的关键词比例，所有子区域的权重之和 = 100
- 权重依据：人口规模 > 经济活跃度 > 商业搜索热度
- 省会/核心城市的权重应该远高于偏远地区（例如新疆：乌鲁木齐 35-40，昌吉 10-12，喀什 8-10，其余更低）
- 区级行政区的权重也要区分：核心城区 > 新开发区 > 远郊区县

## 输出格式
严格输出 JSON，不要其他内容：
{{
  "parent_region": "输入的地区",
  "region_level": "province/municipality/city/district/unknown",
  "sub_regions": [
    {{"name": "子区域1", "weight": 35, "reason": "省会城市，人口最多"}},
    {{"name": "子区域2", "weight": 15, "reason": "第二大城市"}},
    ...
  ],
  "region_aliases": ["区域俗称1", "区域俗称2"]
}}"""


# ========================================
# Step 1: LLM 拓词 Prompt（含地域下钻）
# ========================================

# ========================================
# M1b · 业务类型 × 地域范围 4 分支 prompt addon(CTO-15.9 2026-04-25)
# 注入到 GEO_EXPAND_PROMPT 的 {industry_context} 位置前部 · 按 (business_type, city_scope) 组合选
# ========================================

_BUSINESS_TYPE_ADDONS: dict[tuple, str] = {
    ("B2C", "local"): """
## 业务类型 · B2C 本地服务(默认场景)
- 目标客户 = 终端消费者(C 端)
- 关键词风格:口语化 · 自然提问 · 带"哪家好"/"推荐"/"靠谱"类消费决策词
- 地域密度:约 60% 词带城市 / 区域锚点
""",
    ("B2C", "national"): """
## 业务类型 · B2C 全国(无地域属性 · 跨省服务)
- 目标客户 = 终端消费者 · 服务覆盖全国(SaaS / 电商 / 知识付费 / 远程服务等)
- 关键词风格:100% 通用场景词 · 禁止编造具体城市名
- 地域密度:**0%** · 地域锚点违反全国场景定位
- 参考词型:"XX 软件推荐"/"XX 课程哪家好"/"XX 平台对比"
""",
    ("B2B", "local"): """
## 业务类型 · B2B 本地(区域行业供应链)
- 目标客户 = **企业采购决策链**(老板 / 采购经理 / 总工)
- 关键词意图 = 找供应商 · 解决方案选型 · 方案对比 · 询价
- 每个词必须要求 AI 给出可选择的供应商、厂家、服务商、方案或报价，不能只问知识
- 关键词风格示例:
  · "XX 机械设备供应商推荐"
  · "XX 解决方案服务商对比"
  · "XX 方案报价对比"
  · "有行业落地案例的 XX 厂家推荐"
- 地域密度:约 40%(比 B2C 低 · 企业采购跨区域)
""",
    ("B2B", "national"): """
## 业务类型 · B2B 全国(跨省企业服务)
- 目标客户 = 企业采购决策链(跨省/全国)
- 关键词意图 = 行业全局供应商评估 · 解决方案对比 · 招投标准备
- 禁止 C 端消费词 · 禁止本地城市锚点
- 关键词风格:"XX 行业头部供应商"/"XX 解决方案对比 2026"/"XX 国产替代方案"
- 地域密度:**0%** · 地域锚点违反全国 B2B 定位
""",
    ("政企", "local"): """
## 业务类型 · 政企(合规/招投标/审批)
- 目标客户 = 政府 / 事业单位 / 国企采购部
- 关键词意图 = 合格供应商选择 · 方案对比 · 有案例服务商筛选
- 关键词风格:
  · "有政府采购案例的 XX 服务商推荐"
  · "具备 XX 资质的供应商有哪些"
  · "XX 合规方案供应商对比"
- 地域密度:约 50%(部分与行政区划强绑定)
- 自然选择问法(哪家靠谱 / 推荐 / 对比 / 报价 / 排名)同样合法有效,
  优先叠加资质、案例、合规等政企语境,不得因语气"像 C 端"而排除
""",
    ("政企", "national"): """
## 业务类型 · 政企(全国级 · 央企/部委)
- 目标客户 = 央企 / 部委 / 全国事业单位
- 关键词意图 = 央采名录 · 国标对齐 · 信创兼容 · 等保合规
- 关键词风格:"XX 信创名录厂商"/"XX 国标 + 合规认证"/"XX 等保方案"
- 地域密度:**0%**(央企跨省)
- 自然选择问法(厂商推荐 / 供应商对比 / 哪家靠谱 / 报价)同样合法有效,
  保持央企 / 部委语境即可,不得因语气"像 C 端"而排除
""",
}


def _resolve_business_type_addon(business_type: str, city_scope: str) -> str:
    """M1b · 按 (business_type, city_scope) 选 addon · fallback B2C/local"""
    bt = (business_type or "B2C").strip() or "B2C"
    cs = (city_scope or "local").strip() or "local"
    if bt not in ("B2C", "B2B", "政企"):
        bt = "B2C"
    if cs not in ("local", "national"):
        cs = "local"
    return _BUSINESS_TYPE_ADDONS.get((bt, cs), "")


# ============================================================================
# B4 (CTO-15.9 session 3 · 2026-04-25 · M1b §M2 + Codex 0424 P1.2a/b)
# 6 层关键词矩阵 + 行业 4 模板
# 注入到 GEO_EXPAND_PROMPT 的 {industry_context} 前部 · 与 business_type addon 正交
# ============================================================================

_KEYWORD_LAYERS_MATRIX = """
## 购买决策关键词 6 层矩阵(参考框架 · 不是硬配额)

下面 6 层是**理解买家决策路径的参考框架**，占比是参考值不是必须凑够的指标。
六层都必须要求 AI 给出品牌、服务商、方案、价格或比较选项；不能生成知识问答、流程说明或事实核验题。

**红线(高于本框架)**:每个词都必须是**真人会对 AI 说出口的一句话**。
宁可某一层是 0 个词，也不许为了凑占比把卖点素材拼成运营黑话
(如把"线上展厅"拼成"TikTok线上展厅搭建服务商" —— 没有真人这样问 AI)。

1. **类目服务商层**(参考占比 ~20%)· 抢占品类决策入口
   - 词型:"XX 服务商推荐" / "XX 厂家排名" / "XX 供应商有哪些"
   - 目的:用户要找具体供给方，AI 必须给出选项

2. **场景方案层**(参考占比 ~20%)· 抢占具体场景下的采购决策
   - 词型:"{场景}解决方案推荐" / "{场景}设备型号对比" / "适合{场景}的产品推荐"
   - 目的:用户描述使用场景，AI 给出可购买的产品或方案
   - ⚠️ {场景} 只能填**真人会说的场景口语**，不能填卖点素材里的自造名词

3. **地域转化层**(参考占比见下文「地域策略」· 以那里为准)· 锁定本地需求
   - 词型:"{城市}{品类}哪家好" / "{城市}{服务}推荐"
   - 目的:本地用户搜索时 AI 优先推本地服务商

4. **价格询盘层**(参考占比 ~15%)· 承接预算和报价需求
   - 词型:"XX 报价" / "XX 费用对比" / "预算 XX 选哪个方案"
   - 目的:用户准备采购，AI 给出价格区间及供给选项
   - ⛔ 反例(禁止):"XX收费一般是多少" / "XX大概多少钱" / "XX平均价格" /
     "XX市场行情" —— 泛均价问法 AI 只回行情科普不点名商家,不是询盘

5. **方案比较层**(参考占比 ~15%)· 承接比较和替代需求
   - 词型:"{竞品}和XX对比" / "{竞品}替代品" / "比{竞品}更好的"
   - 目的:用户需要在具体选项间做决定

6. **证据限定选择层**(参考占比 ~10%)· 用证据条件筛选供给方
   - 词型:"有 XX 资质的服务商推荐" / "有落地案例的 XX 厂家有哪些"
   - 目的:证据只是选择条件，问题本身仍必须索要具体供给方
"""


_INDUSTRY_TEMPLATE_ADDONS: dict[str, str] = {
    "local_service": """
## 行业模板 · 本地服务(餐饮/家装/医美/婚摄/教培等)
- 决策路径:看评价 → 比价 → 到店/上门 → 服务体验
- 关键词侧重:"哪家好" / "推荐" / "价格" / "口碑排名" / "有案例的服务商"
- 必含:6 层矩阵 3 地域转化层(地理强相关)≥ 20%
- 长尾词模式:"{城市}{品类}{推荐/哪家好/价格/口碑排名}"
""",
    "consumer": """
## 行业模板 · 消费品(美妆/服装/零食/3C 等)
- 决策路径:种草 → 比价 → 看评测 → 下单
- 关键词侧重:"评测" / "对比" / "测评" / "性价比" / "推荐"
- 必含:6 层矩阵 5 方案比较层 ≥ 20%(同品类竞争激烈)
- 长尾词模式:"{产品类型} 推荐 / 测评 / 对比"
""",
    "B2B": """
## 行业模板 · B2B(SaaS/CRM/ERP/外贸 等)
- 决策路径:需求调研 → 方案对比 → 试用 → POC → 采购
- 关键词侧重:"服务商" / "供应商" / "报价" / "方案对比" / "选型"
- 必含:6 层矩阵 1 类目服务商 + 6 证据限定选择 共 ≥ 25%
- 禁止只问评估标准、选型指南或行业案例，必须明确索要供给选项
- 长尾词模式:"{业务场景} 服务商推荐 / 解决方案对比 / 方案报价"
""",
    "industrial": """
## 行业模板 · 工业采购(机械/化工/制造/设备 等)
- 决策路径:技术参数对比 → 厂家考察 → 样品 → 招投标 → 订单
- 关键词侧重:"厂家" / "供应商" / "型号选型" / "方案报价" / "应用案例限定"
- 必含:6 层矩阵 6 证据限定选择层 ≥ 25%(资质权重大)
- 禁止只问参数、标准、认证或应用案例，证据条件后仍须索要厂家或方案
- 长尾词模式:"{设备品类} 厂家推荐 / 型号对比 / 供应商报价"
""",
}


def _resolve_industry_template(industry: str) -> str:
    """B4 · 子串匹配 industry → 4 行业模板 addon · 不命中返空(走 6 层矩阵 + business_type)"""
    ind = (industry or "").strip()
    if not ind:
        return ""
    # 顺序敏感:先匹配最特殊的(industrial > B2B > consumer > local_service)
    for kw in ("机械", "化工", "制造", "设备", "工业", "工程", "重工"):
        if kw in ind:
            return _INDUSTRY_TEMPLATE_ADDONS["industrial"]
    for kw in ("SaaS", "CRM", "ERP", "软件", "外贸", "B2B", "云服务", "IT 服务"):
        if kw in ind:
            return _INDUSTRY_TEMPLATE_ADDONS["B2B"]
    for kw in ("电商", "零食", "美妆", "服装", "数码", "3C", "消费品", "快消"):
        if kw in ind:
            return _INDUSTRY_TEMPLATE_ADDONS["consumer"]
    for kw in (
        "家装", "装修", "美容", "医美", "餐饮", "婚", "摄影",
        "教育", "培训", "律", "口腔", "医院", "美发", "宠物",
        "家政", "搬家", "维修", "汽修",
    ):
        if kw in ind:
            return _INDUSTRY_TEMPLATE_ADDONS["local_service"]
    return ""


GEO_EXPAND_PROMPT = """你是 GEO（生成式搜索优化）关键词策略师。你的目标：生成那些**用户问 AI 后，AI 会在回答中推荐到「{brand_name}」**的关键词。

{methodology}
{industry_context}
---

## 客户信息
- **目标品牌: {brand_name}**（所有词都必须围绕 AI 推荐到这个品牌展开）
- 核心业务词: {core_keywords}
- 行业: {industry}
- 主要地区: {city}
- 业务范围: {business_scope}
{scope_context}{region_detail}

---

## 你的任务

第一步：深入理解「{brand_name}」的业务——他的目标客户有哪些类型？在什么**不同场景**下需要他的服务？

**场景发散思维（必须覆盖多种场景！）**：
想象这个行业的全部使用场景，不要只围绕核心业务词的字面意思。例如：
- 汽车出行/租车行业 → 商务接待、婚车、机场接送、展会活动、旅游包车、企业长期用车、个人短租、特定车型（迈巴赫/埃尔法/GL8/V260等）
- 装修行业 → 全屋定制、厨卫翻新、办公室装修、商铺装修、二手房翻新、别墅装修
- 教育培训 → K12辅导、成人技能、考证培训、企业内训、一对一、线上课
**你必须至少覆盖 4-6 个不同的使用场景**，不要所有词都集中在一个场景上！

第二步：对每个场景，站在目标客户角度，想象他们会怎么问 AI 来**找到并选择**这类服务商。

第三步：对每个关键词做**品牌锚定试金石** —— "用户把这个问题发给 AI，AI 的回答里**有合理概率推荐到「{brand_name}」**或类似{brand_name}这种规模/定位/服务范围的服务商吗？"
- ✅ 通过：业务范围 + 决策场景（"{city}哪家XX靠谱"/"性价比高"/"售后好"/"细分场景XX"）
- ❌ 不通过 1：**纯泛市场探询词**（如"抖音卖XX哪家好"、"XX一条街哪家"、"X市X地段哪家XX好" —— AI 推综合榜单/网红店/批发市场，不会推具体品牌）
- ❌ 不通过 2：**知识类**（"XX是什么"、"XX流程"、"XX注意什么"、"XX能不能"、"XX会不会"）
- ❌ 不通过 3：**政策法规类**（"XX需要什么条件"、"XX怎么办理"）

然后生成 {count} 个关键词。**确保至少 4 种不同使用场景的词都有覆盖。**

### 质量红线（生成时务必逐条满足 · 不达标的词后续会被标注原因单列，不影响其余合格词交付）
- **每个词都必须是真人会对 AI 说出口的一句话**（第一红线）。把它念出来不像人话，就删掉重写。
  - 反例："TikTok线上展厅搭建服务商"、"全链路赋能方案供应商" —— 这是运营黑话，没有真人这样问 AI
  - 卖点素材/业务范围只用来**理解客户在做什么**，禁止把素材里的名词直接拼成关键词
- **每个词都必须通过品牌锚定试金石**：AI 回答里推荐到「{brand_name}」或同类品牌的概率合理（不是 0）
- **严禁出现纯泛市场词**（含"哪家店好"/"哪家靠谱"但没有 {brand_name} 业务范围具体特征的词 → AI 推平台/榜单不推具体品牌）
- 知识类问题一律不要
- **泛均价问法一律不要**("收费一般是多少"/"大概多少钱"/"平均价格"/"市场行情"
  —— AI 回价格区间科普,不推荐商家;价格词必须绑定选择语境:对比/哪家/预算选型)
- 政策法规类一律不要
- **严禁出现客户服务区域以外的城市**（客户在{city}，不要出现北京、上海、杭州等其他城市的词）
- **严禁整串行政地址**（如"广东省深圳市龙岗区XX推荐"、"深圳市龙岗区XX" —— 真人只会说"深圳龙岗XX"）
- **严禁出现竞品品牌名**（如"喜相逢"、"弹个车"等具体公司名，我们要的是通用推荐词，不是帮竞品引流）
- **严禁出现「{brand_name}」自己的品牌名作为词内出现**（GEO 目的是让 AI 在通用问题里推荐你，不是搜你自己的名字）
- **严禁括号注释**（如"库尔勒（巴州）"，用户不会这样问AI，直接写"库尔勒"或"巴州"）

### 地域策略（**本节优先级高于上文任何地域密度描述**）
{geo_policy}
- **按权重分配词量**：权重高的区域（人口多、市场大）分配更多词，权重低的少分配
- 不带地域的词也只面向客户的目标市场，不要出现其他城市
- **直辖市特别注意**：如果客户在北京/上海/天津/重庆，地域词必须带城市名前缀！写"上海浦东新区XX推荐"而不是"浦东新区XX推荐"。用户搜直辖市的区时，习惯带上城市名。

### 词的形式
- 像真实用户对 AI 说的一句话，自然口语化
- 长度控制在 5-18 字，太长的词不像口语
- 不要他人品牌词
- 不要括号、书名号等非口语符号

## 输出格式
每行一个关键词，不需要标注阶段，直接输出关键词本身。
不要编号、不要解释、不要其他内容。"""


# ========================================
# Step 3: 5118 筛选 Prompt
# ========================================

GEO_FILTER_PROMPT = """你是 GEO 关键词质量审核专家。请用试金石标准审核以下从 5118 获取的 SEO 关键词。

{methodology}

---

## 客户信息
- 城市: {city}
- 行业: {industry}
- 业务范围: {business_scope}

## 你的任务

对每个关键词做试金石检验：**"用户把这句话发给 AI，AI 的回答里会不会列出具体的公司/品牌/服务商？"**

判断后对每个词执行以下三种操作之一：
1. 通过试金石且问法自然 → 保留原词
2. 词根有商业价值但当前问法不会触发AI推荐 → 直接写出优化后的关键词
3. 不通过试金石且无法优化 → 删除

## 待审核关键词
{keyword_list}

## 输出格式（强制规则，务必逐行严格遵守 · 解析按行进行，个别不合格行会被跳过，不影响其余行）

每行严格一条，只允许以下三种格式，不允许任何其他内容：

```
原词 -> 新关键词
原词 -> [保留]
原词 -> [删除]
```

### ⛔ 绝对禁止的输出（出现任何一条即视为格式错误）：
- 禁止写"改造为："/"优化为："/"调整为："等前缀，-> 右边只能是纯关键词
- 禁止写解释、理由、备注、括号说明
- 禁止一行输出多个关键词（如"XX？或者YY"）
- 禁止输出超过25个汉字的关键词
- 禁止在关键词前后加引号

### ✅ 正确输出示例：
汽车租赁 -> 山东包车公司推荐
以租代购 -> 乌鲁木齐以租代购哪家公司靠谱
青岛租车 -> [保留]
租车流程 -> [删除]

### ❌ 错误输出示例（全部禁止）：
汽车租赁 -> 改造为：山东包车公司推荐
汽车租赁 -> 改造为 山东包车公司推荐
汽车租赁 -> 山东哪家汽车服务公司靠谱？或者山东包车公司推荐
汽车租赁 -> "山东包车公司推荐"（加了地域）"""


class KeywordExpander:
    """GEO 关键词扩展器 - LLM语义引擎 + 5118数据引擎"""

    # 主要城市→省份映射，用于将客户城市的省名加入 allowed_geo
    CITY_TO_PROVINCE = {
        "广州": "广东", "深圳": "广东", "东莞": "广东", "佛山": "广东", "珠海": "广东",
        "中山": "广东", "惠州": "广东",
        "杭州": "浙江", "宁波": "浙江", "温州": "浙江", "绍兴": "浙江", "嘉兴": "浙江",
        "台州": "浙江", "金华": "浙江", "湖州": "浙江", "衢州": "浙江", "丽水": "浙江", "舟山": "浙江",
        "南京": "江苏", "苏州": "江苏", "无锡": "江苏", "常州": "江苏", "徐州": "江苏",
        "南通": "江苏", "盐城": "江苏", "扬州": "江苏", "泰州": "江苏", "淮安": "江苏",
        "连云港": "江苏", "宿迁": "江苏", "镇江": "江苏",
        "济南": "山东", "青岛": "山东", "烟台": "山东", "潍坊": "山东", "临沂": "山东",
        "济宁": "山东", "威海": "山东", "日照": "山东", "德州": "山东", "菏泽": "山东",
        "聊城": "山东", "泰安": "山东", "滨州": "山东", "枣庄": "山东", "东营": "山东", "淄博": "山东",
        "成都": "四川", "绵阳": "四川", "德阳": "四川", "宜宾": "四川", "南充": "四川",
        "乐山": "四川", "泸州": "四川", "达州": "四川", "眉山": "四川", "自贡": "四川",
        "武汉": "湖北", "襄阳": "湖北", "宜昌": "湖北", "荆州": "湖北", "十堰": "湖北",
        "孝感": "湖北", "黄冈": "湖北", "荆门": "湖北", "咸宁": "湖北",
        "长沙": "湖南", "株洲": "湖南", "湘潭": "湖南", "衡阳": "湖南", "岳阳": "湖南",
        "常德": "湖南", "郴州": "湖南", "益阳": "湖南",
        "郑州": "河南", "洛阳": "河南", "开封": "河南", "新乡": "河南", "南阳": "河南",
        "安阳": "河南", "信阳": "河南", "许昌": "河南", "商丘": "河南", "周口": "河南",
        "西安": "陕西",
        "合肥": "安徽", "芜湖": "安徽", "蚌埠": "安徽", "阜阳": "安徽", "安庆": "安徽",
        "马鞍山": "安徽", "宿州": "安徽", "滁州": "安徽", "六安": "安徽", "亳州": "安徽",
        "福州": "福建", "厦门": "福建", "泉州": "福建", "漳州": "福建",
        "昆明": "云南", "曲靖": "云南", "大理": "云南", "丽江": "云南",
        "贵阳": "贵州", "遵义": "贵州",
        "南昌": "江西", "赣州": "江西", "九江": "江西", "上饶": "江西",
        "石家庄": "河北", "唐山": "河北", "保定": "河北", "邯郸": "河北", "秦皇岛": "河北",
        "太原": "山西",
        "沈阳": "辽宁", "大连": "辽宁",
        "长春": "吉林",
        "哈尔滨": "黑龙江",
        "南宁": "广西", "桂林": "广西", "柳州": "广西", "北海": "广西",
        "海口": "海南", "三亚": "海南",
        "兰州": "甘肃", "天水": "甘肃",
        "银川": "宁夏",
        "西宁": "青海",
        "拉萨": "西藏",
        "乌鲁木齐": "新疆", "喀什": "新疆", "伊犁": "新疆", "昌吉": "新疆",
        "呼和浩特": "内蒙古",
    }

    def __init__(self):
        self.llm_api_key = DASHSCOPE_API_KEY
        self.llm_endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

    @staticmethod
    def _normalize_city(name: str) -> str:
        """去除城市名的行政后缀。

        [CTO-15.5 2026-04-20 P0 fix] 修"惠州→惠"/"广州→广"吞字 bug:
          原 suffix 列表含单字符"州"/"县",导致"惠州".endswith("州") → 返"惠"
          受害城市:广州/杭州/苏州/郑州/兰州/温州/福州/柳州/徐州/贵州/惠州...
          → 5118/LLM prompt 用"惠"作地名,生成"惠电梯"等乱码关键词

        修法:
          - 长后缀(自治区/自治州/自治县/特别行政区/兵团) → 保留(精确匹配不会吞)
          - 单字符"市"/"省"/"盟" → 保留(不会吞常见地名)
          - 单字符"区"/"县"/"州" → 去掉(会吞常见地名)
          - "朝阳区"不再 normalize 成"朝阳",但上游 allowed_geo 同时加原始值,不影响匹配
        """
        if not name:
            return name
        # 长后缀优先(精确匹配,不会吞字)
        for suffix in ("特别行政区", "自治区", "自治州", "自治县"):
            if name.endswith(suffix) and len(name) > len(suffix):
                return name[:-len(suffix)]
        # 单字符后缀只保留安全的(不含"区"/"县"/"州",这些会吞常见地名)
        for suffix in ("市", "省", "盟"):
            if name.endswith(suffix) and len(name) > len(suffix):
                return name[:-len(suffix)]
        return name

    @staticmethod
    def _parse_region_hierarchy(city_str: str) -> dict:
        """[工单 P0-3] 省/市/区三级地址解析。

        `brands.cities` 里塞的常常是**工商注册地址**("广东省深圳市龙岗区"),
        旧 `_normalize_city` 对整串原样返回,提不出"深圳" → 客户自己的城市落进
        `_hard_filter` 的 150+ 城市黑名单 → 所有含"深圳"的真词被判"范围需复核"。

        这里改成真正解析层级:
            "广东省深圳市龙岗区" → cities=[深圳] · sub_regions=[龙岗区,龙岗] · provinces=[广东]
        没有行政后缀的输入("深圳"/"珠三角"/"朝阳区")按老口径原样当主 token。
        """
        return parse_region_hierarchy(city_str)

    @staticmethod
    def _parse_cities(city_str: str) -> list[str]:
        """将逗号分隔的城市字符串解析为**服务市场城市**列表。

        [工单 P0-3 2026-07-26] 整串地址先走 `_parse_region_hierarchy` 归一到市级
        ("广东省深圳市龙岗区" → "深圳"),区/街道进 sub_regions 由调用方按
        market_level 决定要不要用。无行政后缀的输入行为与旧版一致。
        """
        if not city_str:
            return []
        return list(KeywordExpander._parse_region_hierarchy(city_str)["cities"])

    @staticmethod
    def _contains_excluded_city(kw: str, exclude_list: list[str], allowed_geo: set) -> bool:
        """智能检查关键词是否包含排除地名，避免子串误匹配。

        原理: 先标记 allowed_geo 在关键词中占据的字符位置（受保护），
        再检查 exclude 地名是否完全落在未受保护的位置上。

        示例: 客户在上海，"上海口碑好的XX" → "上海"占据 [0,1]，
        "海口" 占据 [1,2]，位置1被保护 → 不算命中 → 不会被误过滤。
        """
        # 标记 allowed_geo 在 kw 中占据的所有字符位置
        protected = set()
        for allowed in allowed_geo:
            start = 0
            while True:
                idx = kw.find(allowed, start)
                if idx == -1:
                    break
                for i in range(idx, idx + len(allowed)):
                    protected.add(i)
                start = idx + 1

        # 检查每个 exclude 地名
        for name in exclude_list:
            start = 0
            while True:
                idx = kw.find(name, start)
                if idx == -1:
                    break
                match_positions = set(range(idx, idx + len(name)))
                if not match_positions & protected:
                    return True  # 完全不受保护的匹配 → 真正的排除地名
                start = idx + 1

        return False

    @staticmethod
    def _minimum_candidate_count(core_keywords: list[str], target_count: int) -> int:
        """Return the smallest useful candidate set for an interactive quote.

        The upstream engines may legitimately return fewer than ``target_count``
        high-quality rows, but a non-empty response is not automatically a useful
        response.  One to three rows used to be reported as a successful expansion.
        Keep the floor proportional to the customer's own seeds and bounded so the
        deterministic supplement never tries to manufacture all 50 requested rows.
        """
        requested = max(0, int(target_count or 0))
        if requested == 0:
            return 0
        seed_count = len({str(kw).strip().lower() for kw in core_keywords if str(kw).strip()})
        return min(requested, max(8, min(20, seed_count * 5)))

    def _build_rule_supplements(
        self,
        core_keywords: list[str],
        city: str,
        limit: int,
    ) -> list[dict]:
        """Build conservative candidates from customer-provided seed phrases.

        This is a resilience layer, not a replacement for the semantic and 5118
        engines.  Every candidate stays anchored to an original seed and only adds
        a common recommendation, comparison, selection, or price intent.  It does
        not invent products, capabilities, locations, or brand claims.
        """
        if limit <= 0:
            return []

        cities = self._parse_cities(city)
        local_city = cities[0] if len(cities) == 1 else ""
        general_intent_suffixes = (
            "推荐",
            "品牌推荐",
            "厂家推荐",
            "供应商推荐",
            "哪家好",
            "哪个好",
            "服务商对比",
            "价格",
            "报价",
            "性价比",
            "品牌对比",
            "口碑排名",
        )
        provider_intent_suffixes = (
            "推荐",
            "哪家好",
            "有哪些",
            "对比",
            "价格",
            "报价",
            "性价比",
            "口碑排名",
        )
        terminal_intents = sorted(
            {*general_intent_suffixes, *provider_intent_suffixes},
            key=len,
            reverse=True,
        )
        provider_nouns = (
            "品牌", "厂家", "公司", "机构", "平台", "服务商", "供应商", "门店",
        )
        seeds: list[tuple[str, str, tuple[str, ...]]] = []
        candidates: list[dict] = []
        seen: set[str] = set()

        for raw_core in core_keywords:
            core = re.sub(r"\s+", "", str(raw_core or "")).strip()
            if len(core) < 2:
                continue
            stem = core
            for suffix in terminal_intents:
                if stem.endswith(suffix) and len(stem) > len(suffix) + 1:
                    stem = stem[:-len(suffix)].rstrip("，,、;；：: ")
                    break
            suffixes = (
                provider_intent_suffixes
                if stem.endswith(provider_nouns)
                else general_intent_suffixes
            )
            seeds.append((core, stem, suffixes))

        # Rotate by intent first so every customer seed receives coverage before
        # any one seed accumulates a long tail of near-duplicate variants.
        max_suffixes = max((len(suffixes) for _, _, suffixes in seeds), default=0)
        for suffix_index in range(max_suffixes):
            for _core, stem, suffixes in seeds:
                if suffix_index >= len(suffixes):
                    continue
                suffix = suffixes[suffix_index]
                variant = f"{stem}{suffix}"
                scoped_variants = [variant]
                if local_city and not variant.startswith(local_city):
                    scoped_variants.insert(0, f"{local_city}{variant}")
                for keyword in scoped_variants:
                    key = keyword.lower()
                    if len(keyword) < 3 or key in seen:
                        continue
                    seen.add(key)
                    candidates.append({
                        "keyword": keyword,
                        "source": "rule_supplement",
                        "sem_price": 0,
                        "competition": 0,
                        "category": self._categorize_by_intent(keyword),
                    })
                    if len(candidates) >= limit:
                        return candidates

        return candidates

    async def expand_keywords(
        self,
        core_keywords: list[str],
        industry: str = "",
        city: str = "",
        business_scope: str = "",
        target_count: int = 100,
        profile_data: dict | None = None,  # P0.3 CTO-15.7 2026-04-24 注入 v3.6/v3.7 道法术器素材
        brand_name: str = "",  # [CTO-15.23 2026-05-11] 品牌锚定 · 解"碧玉良缘扩出深圳水贝泛词"事故
        scope_lock: dict | None = None,  # [工单 2026-07-26 P0-1] 范围锁定裁决(服务市场/市场层级/问法种子)
    ) -> dict:
        """
        双引擎关键词扩展

        Returns:
            {
                "success": True/False,
                "keywords": [...],
                "summary": { ... },
                "region_drill": { ... },
                "scope_lock": { ... }
            }
        """
        results = {"success": False, "keywords": [], "summary": {}}

        # [工单 P0-1/P0-2] 范围锁定裁决 → 服务市场 + 地域策略
        lock = scope_lock if isinstance(scope_lock, ScopeLock) else scope_lock_from_dict(scope_lock)
        hierarchy = self._parse_region_hierarchy(city)

        # 解析多城市输入(裁决出的服务市场优先于资料里的注册地址)
        if lock and lock.service_market:
            cities_list = list(lock.service_market)
        else:
            cities_list = list(hierarchy["cities"])
        market_level = (
            lock.market_level if lock
            else ("national" if not cities_list else DEFAULT_MARKET_LEVEL)
        )
        geo_policy = geo_policy_for(market_level)
        # 只有真街边店(district)才允许把区级地址当下钻起点;其余一律向上归一到城市
        address_sub_regions = list(hierarchy["sub_regions"]) if geo_policy["allow_sub_region"] else []
        address_streets = list(hierarchy.get("streets") or [])

        is_multi_city = len(cities_list) > 1
        # primary_city 用于地域下钻（仅单城市时下钻）
        primary_city = cities_list[0] if cities_list else ""
        # city_display 用于 LLM prompt 展示
        city_display = "、".join(cities_list) if cities_list else "全国"

        if is_multi_city:
            print(f"  [多城市模式] 检测到 {len(cities_list)} 个目标城市: {city_display}")
        print(f"  [范围锁定] market_level={market_level} · 服务市场={city_display} · "
              f"地域词参考占比 {geo_policy['geo_ratio_hint']}% · 最深 {geo_policy['max_geo_depth']}")

        # Step 0: 地域下钻（和 5118 并行）
        region_info = None
        region_detail_str = ""

        if not geo_policy["drill_region"]:
            print(f"  Step 0: market_level={market_level} 不做地域下钻(全国场景无子区域词)")
        elif primary_city and primary_city not in ("全国", "不限", ""):
            if is_multi_city:
                # ===== 多城市模式：不做地域下钻，直接用城市列表 =====
                print(f"  Step 0: 多城市模式，跳过地域下钻，直接使用 {len(cities_list)} 个城市")

                # 构建多城市的region_info，供_hard_filter使用
                region_info = {
                    "parent_region": city_display,
                    "region_level": "multi_city",
                    "sub_regions": [{"name": c, "weight": round(100 / len(cities_list))} for c in cities_list],
                    "region_aliases": [],
                }

                # 按关键词数量平均分配到各城市
                per_city_count = max(1, target_count // (len(cities_list) + 1))  # +1给通用词留余量
                city_list_str = "、".join(cities_list)
                region_detail_str = f"""
### 多城市覆盖模式
客户需要覆盖以下 {len(cities_list)} 个城市: **{city_list_str}**

**地域词生成要求**：
1. 每个城市都需要生成地域关键词，确保覆盖均匀
2. 地域词格式：「城市名 + 业务词 + 推荐/哪家好等」
3. 例如："{cities_list[0]}XX公司推荐"、"{cities_list[1] if len(cities_list) > 1 else cities_list[0]}XX哪家好"
4. 每个城市分配约 {per_city_count} 个地域词
5. 另外生成约 {target_count - per_city_count * len(cities_list)} 个不带地域的通用推荐词
6. **严禁出现以上列表以外的城市名！**
7. 直辖市（北京/上海/天津/重庆）如果在列表中，可以下钻到区级（如"北京朝阳区XX推荐"），但必须带城市名前缀"""

                results["region_drill"] = region_info
            else:
                # ===== 单城市模式：正常地域下钻(深度由 market_level 决定) =====
                print(f"  Step 0: 地域下钻 [{primary_city}] · 深度上限 {geo_policy['max_geo_depth']}...")
                region_info = await self._drill_region(primary_city, market_level=market_level)
                if region_info and region_info.get("sub_regions"):
                    subs = region_info["sub_regions"]
                    aliases = region_info.get("region_aliases", [])
                    level = region_info.get("region_level", "unknown")
                    level_label = {"province": "省/自治区", "municipality": "直辖市", "city": "地级市", "district": "区/县"}.get(level, "地区")

                    # 兼容新格式（带权重的对象列表）和旧格式（纯字符串列表）
                    if subs and isinstance(subs[0], dict):
                        sub_names = [s["name"] for s in subs]
                        # 构建带权重的区域表格
                        region_table_lines = []
                        for s in subs:
                            name = s["name"]
                            weight = s.get("weight", 0)
                            reason = s.get("reason", "")
                            region_table_lines.append(f"| {name} | {weight}% | {reason} |")
                        region_table = "\n".join(region_table_lines)

                        print(f"    [{level_label}] 下钻出 {len(subs)} 个子区域:")
                        for s in subs[:5]:
                            print(f"      {s['name']}: {s.get('weight', '?')}%")
                        if len(subs) > 5:
                            print(f"      ...及 {len(subs) - 5} 个其他区域")

                        # 直辖市特殊处理：地域词必须带城市名前缀
                        if level == "municipality":
                            region_detail_str = f"""
### 地域下钻结果（直辖市 — 关键词必须带"{primary_city}"前缀！）
- 地区级别: {level_label}
{f'- 区域俗称: {", ".join(aliases)}' if aliases else ''}

| 城区 | 关键词分配权重 | 依据 |
|--------|:------------:|------|
{region_table}

**直辖市关键词规则（必须严格遵守！）**：
1. 地域词分三类：
   - **城市级**（约40%）：只带"{primary_city}"，如"{primary_city}XX推荐"、"{primary_city}XX哪家好"
   - **城区级**（约40%）：带"{primary_city}+区名"，如"{primary_city}浦东新区XX推荐"（**不要**写成单独的"浦东新区XX推荐"！用户搜索直辖市的区时，习惯带上城市名）
   - **俗称级**（约20%）：用俗称替代城市名，如"{'、'.join(aliases[:2]) if aliases else '俗称'}XX推荐"
2. **严禁**生成只有区名没有城市名的关键词（如"浦东新区XX推荐"），这不符合真实搜索习惯
3. 权重高的城区分配更多关键词"""
                        else:
                            region_detail_str = f"""
### 地域下钻结果（按权重分配关键词！）
- 地区级别: {level_label}
{f'- 区域俗称: {", ".join(aliases)}' if aliases else ''}

| 子区域 | 关键词分配权重 | 依据 |
|--------|:------------:|------|
{region_table}

**要求**：权重高的区域分配更多关键词，权重低的少分配。例如权重35%的区域应该分到约35%的地域词，权重5%的区域分到1-2个词即可。"""
                    else:
                        # 旧格式降级处理
                        sub_names = subs
                        print(f"    [{level_label}] 下钻出 {len(subs)} 个子区域: {', '.join(subs[:8])}")
                        region_detail_str = f"""
### 地域下钻结果
- 地区级别: {level_label}
- 子区域列表: {', '.join(subs)}
{f'- 区域俗称: {", ".join(aliases)}' if aliases else ''}

请根据各子区域的人口规模和市场容量按比例分配关键词，核心城市/城区多分配，偏远地区少分配。"""

                    results["region_drill"] = region_info

        # Step 1 & 2: LLM 语义拓词 + 5118 数据拓词（并行）
        print("  Step 1+2: LLM语义拓词 + 5118数据拓词（并行）...")

        # 多城市模式：增加LLM目标数量，确保每个城市都有足够的词
        llm_count = target_count // 2
        if is_multi_city:
            llm_count = max(llm_count, len(cities_list) * 6)  # 每城市至少6个词

        llm_task = self._llm_expand(
            core_keywords=core_keywords,
            industry=industry,
            city=city_display,  # 传完整城市列表给LLM
            business_scope=business_scope,
            count=llm_count,
            region_detail=region_detail_str,
            profile_data=profile_data,  # P0.3 透传 v3.6/v3.7 行业素材
            brand_name=brand_name,  # [CTO-15.23 2026-05-11] 品牌锚定透传
            scope_lock=lock,  # [工单 2026-07-26] 范围裁决透传(地域策略 + 问法种子)
        )
        try:
            source_timeout = float(os.getenv("KEYWORD_5118_EXPAND_TIMEOUT_SECONDS", "25"))
        except (TypeError, ValueError):
            source_timeout = 25.0
        source_timeout = min(60.0, max(5.0, source_timeout))
        api_task = asyncio.wait_for(
            self._5118_expand(
                core_keywords=core_keywords,
                count_per_keyword=target_count // max(len(core_keywords), 1),
            ),
            timeout=source_timeout,
        )

        llm_result, api_keywords = await asyncio.gather(
            llm_task, api_task, return_exceptions=True
        )

        # 处理异常
        llm_keywords = llm_result if isinstance(llm_result, list) else []
        api_keywords = api_keywords if isinstance(api_keywords, list) else []
        print(f"    LLM: {len(llm_keywords)} 个 | 5118: {len(api_keywords)} 个")

        # Step 3: LLM 筛选 5118 关键词
        print("  Step 3: LLM 语义筛选 5118 关键词...")
        filtered_api_keywords = await self._llm_filter(
            keywords=api_keywords,
            industry=industry,
            city=city_display,  # 传完整城市列表给LLM筛选
            business_scope=business_scope
        )
        print(f"    筛选后保留 {len(filtered_api_keywords)} 个")

        # Step 4: 智能融合去重
        print("  Step 4: 融合去重...")
        all_keywords = {}

        for kw_text, category in llm_keywords:
            key = kw_text.lower().strip()
            if key and len(key) >= 3 and key not in all_keywords:
                all_keywords[key] = {
                    "keyword": kw_text.strip(),
                    "source": "llm",
                    "sem_price": 0,
                    "competition": 0,
                    "category": category
                }

        overlap_count = 0
        for item in filtered_api_keywords:
            key = item["keyword"].lower().strip()
            if key in all_keywords:
                all_keywords[key]["source"] = "both"
                all_keywords[key]["sem_price"] = item.get("sem_price", 0)
                overlap_count += 1
            elif key and len(key) >= 3:
                all_keywords[key] = {
                    "keyword": item["keyword"],
                    "source": "5118",
                    "sem_price": item.get("sem_price", 0),
                    "competition": item.get("bidword_kwc", 0),
                    "category": self._categorize_by_intent(item["keyword"])
                }

        keyword_list = list(all_keywords.values())

        # ================================================================
        # Step 5: 三轴交付决策(工单 GEO_COMMERCIAL_DOUBLE_INVERSION 2026-08-09)
        # ================================================================
        # 老实现在这里只问一个布尔:`_check_geo_feasibility()`。一个布尔背三件事,
        # 于是「商场推荐」(商业成立但地域过宽)被自动勾进付费交付,而
        # 「深圳龙岗商场招商电话」(本地强成交)被统一解释成"不会让 AI 推荐品牌"。
        #
        # 现在:商业意图 → 业务匹配 → 地域匹配 → 默认选择,三根轴各自留证据、
        # 各自有稳定原因码;`uncertain` 不再被写成 `knowledge`(R2)。
        business_profile = build_business_profile(
            industry=industry,
            business_scope=business_scope,
            profile_data=profile_data,
            scope_lock=(lock.as_dict() if lock else None),
            core_keywords=core_keywords,
        )
        # 地域上下文取自 `_hard_filter` 自己算的那份唯一地名词典(不另造第二份):
        # 用空列表跑一次只为把 allowed_geo / exclude_* 取出来,不产生任何过滤副作用。
        _geo_sets: dict = {}
        self._hard_filter(
            [], city, region_info,
            cities_list=cities_list, industry=industry, business_scope=business_scope,
            market_level=market_level,
            extra_allowed_geo=[*address_sub_regions, *hierarchy["provinces"]],
            street_names=address_streets,
            geo_sets_out=_geo_sets,
        )
        _allowed_geo = _geo_sets.get("allowed_geo") or set()
        _outside_names = [
            *(_geo_sets.get("exclude_cities") or []),
            *(_geo_sets.get("exclude_regions") or []),
        ]
        geo_context = GeoContext(
            market_level=market_level,
            allowed_tokens=tuple(sorted(str(g) for g in _allowed_geo if len(str(g)) >= 2)),
            outside_tokens=tuple(_outside_names),
            has_allowed_geo=lambda kw: any(
                g in kw for g in _allowed_geo if len(str(g)) >= 2
            ),
            has_outside_geo=lambda kw: self._contains_excluded_city(
                kw, _outside_names, _allowed_geo,
            ),
        )

        def _decide(item: dict) -> tuple[dict, "object"]:
            """给一个候选打上三轴决策 + 派生老字段(T6:老字段一律派生,不再各写一套)。"""
            decision = decide_keyword_delivery(
                item.get("keyword", ""),
                profile=business_profile,
                geo=geo_context,
                brand_name=brand_name,
                intent_hint=item.get("intent"),
            )
            enriched = {**item, **decision.as_dict()}
            enriched["keyword"] = item.get("keyword", "")
            enriched["policy_version"] = _POLICY_VERSION
            enriched["delivery_policy_version"] = DELIVERY_POLICY_VERSION
            # —— 老响应字段:全部由三轴派生(工单 T6)——
            enriched["commercial_delivery_eligible"] = decision.commercial_intent in (
                "commercial", "brand_direct",
            )
            enriched["geo_recommend"] = enriched["commercial_delivery_eligible"]
            enriched["scope_match"] = not (
                decision.business_scope == SCOPE_MISMATCHED
                or decision.geo_scope != GEO_MATCHED
            )
            enriched["rejection_reason"] = (
                "" if decision.default_selected else decision.reason_text
            )
            return enriched, decision

        decided_all = [_decide(item) for item in keyword_list]
        keyword_list = [e for e, d in decided_all if d.default_selected]
        review_bucket = [e for e, d in decided_all if not d.default_selected]
        if review_bucket:
            _by_group: dict = {}
            for e in review_bucket:
                _by_group[e["reason_group"]] = _by_group.get(e["reason_group"], 0) + 1
            print(
                f"  Step 5: 三轴决策 —— 进入交付 {len(keyword_list)} 个,"
                f"待确认 {len(review_bucket)} 个 {_by_group}"
            )

        # Step 5a: 结构性硬过滤(括号注释/整串行政地址/超深地名/知识残留)。
        #   地域与商业意图已经在 Step 5 判过 —— 这一层只兜结构问题,
        #   且每一条丢弃都带**真实**理由码回到待确认区,不再压成一句糊涂话(R5)。
        _drops: list = []
        keyword_list = self._hard_filter(
            keyword_list,
            city,
            region_info,
            cities_list=cities_list,
            industry=industry,
            business_scope=business_scope,
            market_level=market_level,
            extra_allowed_geo=[*address_sub_regions, *hierarchy["provinces"]],
            street_names=address_streets,
            drops=_drops,
        )
        hard_filter_rejected = []
        for dropped_item, drop_code in _drops:
            rejected_item = dict(dropped_item)
            rejected_item["default_selected"] = False
            # 结构性理由只在三轴表达不了它时才覆盖(§3.3 判定顺序:商业意图优先)。
            if drop_code in (DROP_FORMAT_UNNATURAL, DROP_GEO_TOO_DEEP) or \
                    rejected_item.get("reason_code") == "DELIVERY_OK":
                rejected_item["reason_code"] = drop_code
                rejected_item["reason_text"] = _DROP_REASON_TEXT.get(drop_code, "")
                rejected_item["reason_group"] = _DROP_REASON_GROUP.get(
                    drop_code, group_for_reason(drop_code),
                )
            rejected_item["rejection_reason"] = rejected_item.get("reason_text") or ""
            rejected_item["scope_match"] = False
            hard_filter_rejected.append(rejected_item)
        removed = len(hard_filter_rejected)
        if removed > 0:
            print(f"  Step 5a: 结构性过滤标记 {removed} 个不合规词 (剩余 {len(keyword_list)} 个)")

        # Step 5b: 数量质量门。LLM 或 5118 只返 1-3 条时，不能把非空短结果
        # 包装成一次完整扩词。仅用客户原始核心词补充保守候选，再走同一硬过滤。
        minimum_expected = self._minimum_candidate_count(core_keywords, target_count)
        supplemented_count = 0
        if len(keyword_list) < minimum_expected:
            existing = {item["keyword"].lower().strip() for item in keyword_list}
            supplement_pool = self._build_rule_supplements(
                core_keywords=core_keywords,
                city=city,
                limit=max(target_count, minimum_expected * 2),
            )
            supplement_pool = self._hard_filter(
                supplement_pool,
                city,
                region_info,
                cities_list=cities_list,
                industry=industry,
                business_scope=business_scope,
                market_level=market_level,
                extra_allowed_geo=[*address_sub_regions, *hierarchy["provinces"]],
                street_names=address_streets,
            )
            # [T2] 补充候选走**同一条**三轴决策,不走第二套判据。
            _supplement_decided = [_decide(item) for item in supplement_pool]
            for enriched, decision in _supplement_decided:
                key = enriched["keyword"].lower().strip()
                if not key or key in existing:
                    continue
                if not decision.default_selected:
                    # 补充词也不许绕过三轴 —— 不达标的进待确认区,不静默丢弃。
                    review_bucket.append(enriched)
                    existing.add(key)
                    continue
                keyword_list.append(enriched)
                existing.add(key)
                supplemented_count += 1
                if len(keyword_list) >= minimum_expected:
                    break
            if supplemented_count:
                print(
                    f"  Step 5b: 上游仅产出 {len(keyword_list) - supplemented_count} 个有效词，"
                    f"按客户核心词补充 {supplemented_count} 个（质量门 {minimum_expected}）"
                )

        keyword_list = keyword_list[:target_count]

        # Step 6: 到这里的候选**都已经带着自己的三轴决策**(Step 5 打的)。
        #   🔴 老代码在这里把 geo_recommend / scope_match / intent_type 一律硬写成
        #      True/True/"commercial" —— 那正是 R4「业务范围没有形成最终可审计决策」的来源:
        #      裁决被一行赋值抹平,前端看到的 scope_match 与真实判定无关。
        #      现在只做一件事:确认每条都真的带了决策,缺了就地补(绝不硬写)。
        for index, item in enumerate(keyword_list):
            if not item.get("reason_code"):
                keyword_list[index], _ = _decide(item)

        # 统计地域词比例
        geo_count = sum(1 for kw in keyword_list if self._has_region(kw["keyword"], city, region_info, cities_list=cities_list))

        results["success"] = len(keyword_list) >= minimum_expected
        results["keywords"] = keyword_list
        # 待确认区 = 三轴任一没到位的词 + 结构性丢弃的词。
        # 🔴 每一条都带 reason_code / reason_group / reason_text,前端据码分组,
        #    不再靠中文串猜;也不静默删除任何原始关键词(工单 T2)。
        _rejected_seen: set = set()
        rejected_all: list[dict] = []
        for entry in [*review_bucket, *hard_filter_rejected]:
            key = str(entry.get("keyword") or "").strip().lower()
            if not key or key in _rejected_seen:
                continue
            _rejected_seen.add(key)
            rejected_all.append(entry)
        results["rejected_keywords"] = rejected_all
        rejected_non_decision = sum(
            1 for e in rejected_all
            if e.get("reason_group") == GROUP_KNOWLEDGE
        )
        results["rejected_by_group"] = {
            group: sum(1 for e in rejected_all if e.get("reason_group") == group)
            for group in sorted({str(e.get("reason_group") or "") for e in rejected_all})
            if group
        }
        # [工单 P0-1] 裁决结果随响应回前端(报价页露出 + 可人工修改确认)
        results["scope_lock"] = lock.as_dict() if lock else None
        results["summary"] = {
            "market_level": market_level,
            "service_market": list(cities_list),
            "geo_ratio_target": geo_policy["geo_ratio_hint"],
            "scope_lock_source": lock.source if lock else "none",
            "used_diagnosis": bool(lock.used_diagnosis) if lock else False,
            "total": len(keyword_list),
            "requested_count": max(0, int(target_count or 0)),
            "actual_count": len(keyword_list),
            "minimum_expected": minimum_expected,
            "supplemented": supplemented_count,
            "quality_gate_passed": len(keyword_list) >= minimum_expected,
            "underfilled": len(keyword_list) < max(0, int(target_count or 0)),
            "delivery_status": (
                "complete"
                if len(keyword_list) >= max(0, int(target_count or 0))
                else "partial"
            ),
            "from_llm": len(llm_keywords),
            "from_5118_raw": len(api_keywords),
            "from_5118_filtered": len(filtered_api_keywords),
            "overlap": overlap_count,
            "rejected_hard_filter": removed,
            "rejected_non_decision": rejected_non_decision,
            "rejected_non_decision_total": len(rejected_all),
            # 🔴 `geo_ratio_target` 只是**出词提示**(prompt 里的参考占比),
            #    从来不是资格判据、也不是验收配额(工单 T4 首条)。
            #    反向锁:tests/quotegeo_2026_08_10/test_geo_ratio_hint_is_not_a_gate.py
            "geo_ratio_is_hint_only": True,
            "geo_keywords": geo_count,
            "geo_ratio": round(geo_count / max(len(keyword_list), 1) * 100, 1)
        }

        if not results["success"]:
            results["error"] = "KEYWORD_EXPANSION_UNDERFILLED"

        print(f"  Done: {len(keyword_list)} 个关键词 (地域词 {geo_count} 个, {results['summary']['geo_ratio']}%)")
        return results

    # 四大直辖市
    MUNICIPALITIES = {"北京", "上海", "天津", "重庆"}

    def _hard_filter(self, keywords: list[dict], city: str, region_info: dict = None,
                     cities_list: list[str] = None, industry: str = "", business_scope: str = "",
                     market_level: str = DEFAULT_MARKET_LEVEL,
                     extra_allowed_geo: list[str] = None,
                     street_names: list[str] = None,
                     drops: list | None = None,
                     geo_sets_out: dict | None = None) -> list[dict]:
        """
        硬过滤 — 程序化兜底，弥补 LLM 筛选遗漏。

        [工单 GEO_COMMERCIAL_DOUBLE_INVERSION 2026-08-09 · T2/T4] 两个**显式出参**:

          drops:        传入一个 list,本函数会 append `(item, reason_code)` ——
                        每一条被丢弃的词都带上**真实**的丢弃理由,而不是被压成一句
                        "地域、格式或客户业务范围不匹配"(R5 那句糊涂话的来源)。
          geo_sets_out: 传入一个 dict,本函数会把它算出来的
                        allowed_geo / exclude_cities / exclude_regions 回填进去。

        🔴 为什么用出参而不是把这段计算搬去公共模块:那 150+ 城市黑名单 + allowed_geo
           推导是**全系统唯一一份地名词典**,搬家就得改 `_contains_excluded_city` 的
           保护区间语义,风险远大于收益。三轴决策(services/keyword_delivery_decision.py)
           因此**接这里算好的结果**,不自带第二份词典 —— 一处词典,一处口径。

        过滤规则：
        1. 包含客户服务区域以外的城市名/地区名
        2. 包含括号注释等不自然格式
        3. 知识类残留
        4. 直辖市：区名前必须带城市名（符合搜索习惯）
        5. 竞品品牌词 → [工单 P1-7] 改 advisory：只标注不丢弃
        6. [工单 P0-3] 整串行政地址（"广东省深圳市龙岗区XX"）—— 真人不这样问 AI
        7. [工单 P0-2] 街道/片区级地域词（market_level != district 时）

        [工单 P0-3 2026-07-26] `allowed_geo` 现在以**解析出来的城市 + 省 + 区**为准，
        150+ 硬编码城市黑名单降级为兜底：客户自己的城市绝不可能再落进黑名单
        （老 bug：cities="广东省深圳市龙岗区" 提不出"深圳" → 含"深圳"的真词全被拒）。
        """
        # ---- 城市黑名单（覆盖主要城市 + 常见地级市）----
        all_major_cities = [
            "北京", "上海", "广州", "深圳", "成都", "杭州", "天津", "武汉",
            "南京", "重庆", "西安", "苏州", "郑州", "长沙", "青岛", "大连",
            "厦门", "宁波", "无锡", "佛山", "合肥", "福州", "昆明", "哈尔滨",
            "济南", "沈阳", "长春", "石家庄", "太原", "南昌", "贵阳", "兰州",
            "海口", "三亚", "银川", "西宁", "拉萨", "呼和浩特", "南宁", "乌鲁木齐",
            "珠海", "东莞", "中山", "惠州", "温州", "绍兴", "嘉兴", "台州",
            "徐州", "常州", "烟台", "潍坊", "洛阳", "漳州", "泉州", "芜湖",
            # 补充更多地级市，防止漏网
            "聊城", "临沂", "济宁", "泰安", "威海", "日照", "德州", "菏泽",
            "滨州", "枣庄", "东营", "淄博", "莱芜",
            "唐山", "保定", "邯郸", "秦皇岛", "张家口", "承德", "廊坊", "沧州",
            "邢台", "衡水",
            "洛阳", "开封", "新乡", "安阳", "焦作", "许昌", "南阳", "信阳",
            "商丘", "周口", "驻马店", "平顶山", "漯河", "三门峡", "鹤壁", "濮阳",
            "镇江", "盐城", "扬州", "泰州", "淮安", "连云港", "宿迁", "南通",
            "湖州", "金华", "衢州", "丽水", "舟山",
            "株洲", "湘潭", "衡阳", "岳阳", "常德", "益阳", "郴州", "永州",
            "怀化", "娄底", "邵阳", "张家界",
            "桂林", "柳州", "北海", "梧州", "玉林", "百色", "贺州", "河池",
            "遵义", "六盘水", "安顺", "毕节", "铜仁",
            "曲靖", "玉溪", "大理", "丽江", "红河", "楚雄",
            "绵阳", "德阳", "宜宾", "南充", "乐山", "泸州", "达州", "眉山",
            "遂宁", "内江", "自贡", "攀枝花",
            "襄阳", "宜昌", "荆州", "黄冈", "十堰", "孝感", "咸宁", "荆门",
            "鄂州", "黄石", "随州",
            "赣州", "九江", "上饶", "宜春", "吉安", "抚州", "景德镇", "新余",
            "萍乡", "鹰潭",
            "芜湖", "蚌埠", "阜阳", "安庆", "马鞍山", "宿州", "滁州", "六安",
            "亳州", "池州", "宣城", "铜陵", "淮南", "淮北",
        ]
        # 省/自治区/州名也加入检测（防止 "黔西南" "凉山" 这类地区名漏网）
        all_region_names = [
            "黔西南", "黔东南", "黔南", "凉山", "甘孜", "阿坝", "湘西", "恩施",
            "延边", "大兴安岭", "红河", "文山", "德宏", "迪庆", "怒江", "西双版纳",
            "临夏", "甘南", "海东", "海西", "海南州", "黄南", "果洛", "玉树",
            "阿里", "那曲", "日喀则", "山南", "林芝", "昌都",
            "巴音郭楞", "博尔塔拉", "克孜勒苏", "阿克苏", "和田", "喀什",
            "伊犁", "塔城", "阿勒泰", "吐鲁番", "哈密", "昌吉",
            "锡林郭勒", "兴安盟", "阿拉善", "巴彦淖尔", "乌兰察布",
            "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江",
            "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
            "广东", "广西", "海南", "四川", "贵州", "云南", "西藏",
            "陕西", "甘肃", "青海", "宁夏", "新疆", "内蒙古", "台湾",
        ]

        # 允许的地域：客户城市本身 + 各个城市 + 其子区域 + 别名 + 所在省份
        allowed_geo = set()
        if city:
            allowed_geo.add(self._normalize_city(city))
            allowed_geo.add(city)  # 也保留原始输入
            # [工单 P0-3] 整串地址必须先解析出市/省/区再进 allowed_geo,
            # 否则客户自己的城市名会被下面的城市黑名单当成"外地城市"拒掉。
            _hier = self._parse_region_hierarchy(city)
            allowed_geo.update(_hier["cities"])
            allowed_geo.update(_hier["provinces"])
            allowed_geo.update(_hier["sub_regions"])
        for extra in (extra_allowed_geo or []):
            extra = str(extra or "").strip()
            if extra:
                allowed_geo.add(extra)
        # 多城市模式：将每个独立城市都加入允许列表
        if cities_list:
            for c in cities_list:
                allowed_geo.add(c)
                allowed_geo.add(self._normalize_city(c))
        if region_info:
            for sub in region_info.get("sub_regions", []):
                name = sub["name"] if isinstance(sub, dict) else sub
                allowed_geo.add(name)
                allowed_geo.add(self._normalize_city(name))
            for alias in region_info.get("region_aliases", []):
                allowed_geo.add(alias)

        # 添加客户城市所在省份到 allowed_geo（防止"广东深圳XX"被省名过滤）
        all_customer_cities = cities_list if cities_list else self._parse_cities(city)
        for c in all_customer_cities:
            allowed_geo.add(c)
            province = self.CITY_TO_PROVINCE.get(c)
            if province:
                allowed_geo.add(province)

        allowed_geo = {g for g in allowed_geo if str(g or "").strip()}
        # 黑名单只是兜底:任何落在 allowed_geo 里的地名都不参与排除(含客户自己的城市/省/区)
        exclude_cities = [c for c in all_major_cities if c not in allowed_geo]
        exclude_regions = [r for r in all_region_names if r not in allowed_geo]

        geo_policy = geo_policy_for(market_level)
        # 街道级地名来源:资料地址里的街道 + 下钻结果里残留的街道名
        drill_street_names = []
        if region_info and not geo_policy["allow_street"]:
            for sub in region_info.get("sub_regions", []) or []:
                name = sub.get("name") if isinstance(sub, dict) else sub
                name = str(name or "").strip()
                if name and contains_street_level(name):
                    drill_street_names.append(name)
        all_street_names = [*(street_names or []), *drill_street_names]

        # ---- 直辖市：子区域名单（用于检测缺少城市名前缀的关键词）----
        # 单城市模式下检测直辖市；多城市模式下不做区名检测（LLM已被提示）
        # [工单 P0-3] 用解析后的城市判直辖市:老口径对"北京市朝阳区"整串返回,
        # 认不出"北京" → 直辖市补前缀规则形同虚设。
        actual_cities = cities_list if cities_list else self._parse_cities(city)
        municipality_cities_in_list = [c for c in actual_cities if c in self.MUNICIPALITIES]
        is_municipality = len(actual_cities) == 1 and actual_cities[0] in self.MUNICIPALITIES
        municipality_districts = set()
        if is_municipality and region_info:
            for sub in region_info.get("sub_regions", []):
                name = sub["name"] if isinstance(sub, dict) else sub
                municipality_districts.add(name)

        # ---- 格式黑名单 ----
        bracket_pattern = re.compile(r"[【】\[\]（）\(\){}｛｝]")

        # [T2/T4] 把这份唯一地名词典的计算结果交给调用方(三轴决策要用同一口径)
        if geo_sets_out is not None:
            geo_sets_out.update({
                "allowed_geo": set(allowed_geo),
                "exclude_cities": list(exclude_cities),
                "exclude_regions": list(exclude_regions),
                "geo_policy": dict(geo_policy),
                "all_street_names": list(all_street_names),
                "market_level": market_level,
            })

        def _drop(rejected_item: dict, reason_code: str) -> None:
            if drops is not None:
                drops.append((rejected_item, reason_code))

        filtered = []
        for item in keywords:
            kw = item["keyword"]

            # 规则1: 其他城市（使用智能子串匹配，避免"海口"误匹配"上海口碑"）
            if self._contains_excluded_city(kw, exclude_cities, allowed_geo):
                _drop(item, DROP_GEO_OUTSIDE_MARKET)
                continue

            # 规则1b: 其他地区（州/盟/省名），同样使用智能匹配
            if self._contains_excluded_city(kw, exclude_regions, allowed_geo):
                _drop(item, DROP_GEO_OUTSIDE_MARKET)
                continue

            # 规则2: 括号/注释
            if bracket_pattern.search(kw):
                _drop(item, DROP_FORMAT_UNNATURAL)
                continue

            # 规则2b: [工单 P0-3] 整串行政地址不是真人问法
            #   "广东省深圳市龙岗区TikTok工厂出海获客服务推荐" —— 没人这样问 AI
            if is_full_address_form(kw):
                _drop(item, DROP_FORMAT_UNNATURAL)
                continue

            # 规则2c: [工单 P0-2] 街道/镇/片区级地域词(只有真街边店 district 才允许)
            if not geo_policy["allow_street"] and contains_street_level(
                kw, street_names=all_street_names, allowed_names=list(allowed_geo),
            ):
                _drop(item, DROP_GEO_TOO_DEEP)
                continue

            # 规则3: 知识类残留（双重保险）
            if re.search(r"是什么意思|是什么$|怎么办理|需要什么条件|什么是", kw):
                _drop(item, DROP_KNOWLEDGE_RESIDUE)
                continue

            # 规则4: 直辖市区名必须带城市名前缀
            # 如 "浦东新区XX" 应改为 "上海浦东新区XX"
            if is_municipality and municipality_districts:
                norm_city = actual_cities[0]  # 已 normalize 过
                has_bare_district = False
                for dist in municipality_districts:
                    if dist in kw and norm_city not in kw:
                        # 检查是否有俗称（魔都、申城等）替代了城市名
                        has_alias = any(alias in kw for alias in region_info.get("region_aliases", []))
                        if not has_alias:
                            has_bare_district = True
                            break
                if has_bare_district:
                    # 不是直接过滤，而是自动补上城市名前缀
                    for dist in municipality_districts:
                        if dist in kw and norm_city not in kw:
                            item = {**item, "keyword": kw.replace(dist, f"{norm_city}{dist}", 1)}
                            break

            # 规则5: [工单 P1-7] 竞品品牌词 → advisory
            #   "前缀≥3字 + 非地域 + 非通用描述 = 竞品" 是纯猜测,对复合业务词
            #   (外贸社媒/跨境电商 前缀)误伤严重。改为**标注不丢弃**:词照常进候选,
            #   带 competitor_suspect 让操作员(或后续 LLM 复核)决定。
            if self._is_likely_competitor_brand(kw, industry, business_scope, allowed_geo):
                item = {
                    **item,
                    "competitor_suspect": True,
                    "advisory_reason": "疑似竞品品牌词（启发式判断，请复核后再决定是否使用）",
                }

            filtered.append(item)

        return filtered

    # ---- 竞品品牌词检测 ----
    # 通用描述词：这些词可以作为行业词的前缀，不是品牌名
    _GENERIC_DESCRIPTORS = {
        # 规模/级别
        "大型", "小型", "中型", "微型", "高端", "低端", "中端", "中高端", "顶级", "高级",
        # 性质/属性
        "专业", "正规", "知名", "连锁", "直营", "上门", "到店",
        "私人", "个人", "商务", "工业", "家用", "商用", "民用",
        "国产", "进口", "国内", "国外", "海外",
        # 场景/位置
        "户外", "室内", "室外", "线上", "线下", "本地", "当地",
        # 风格/时代
        "新型", "传统", "现代", "创意", "时尚", "简约", "古典", "复古",
        "智能", "自动", "手动", "电动",
        # 价格/经济
        "便宜", "平价", "实惠", "经济", "免费", "低价", "高价",
        # 类型/方式
        "全屋", "整体", "定制", "批量", "零售", "批发",
        "环保", "节能", "绿色", "有机", "天然",
        # 三字通用描述
        "新中式", "全自动", "半自动", "一站式", "多功能", "高品质", "纯手工",
        "全方位", "一体化", "数字化", "智能化", "自动化", "个性化", "定制化",
    }

    # 搜索意图信号词：包含这些词的关键词不是纯品牌导航词
    _INTENT_SIGNALS = [
        "推荐", "哪家好", "哪个好", "排名", "排行", "前十", "前五",
        "靠谱", "正规", "口碑", "怎么选", "如何选", "怎么样",
        "多少钱", "价格", "费用", "收费", "报价", "加盟",
        "对比", "最好", "性价比", "去哪", "哪里", "找",
        "有哪些", "有没有", "附近", "十大", "品牌",
        "好不好", "值得", "评价", "测评", "电话", "地址",
    ]

    def _is_likely_competitor_brand(self, keyword: str, industry: str, business_scope: str,
                                    allowed_geo: set) -> bool:
        """
        检测关键词是否为竞品品牌词。

        竞品品牌词特征：[品牌名] + [行业词]，无搜索意图修饰语。
        例如："金色童年儿童摄影" = "金色童年"(品牌) + "儿童摄影"(行业)
        这类词AI只会介绍该品牌本身，不会推荐其他商家，GEO优化无价值。
        """
        kw = keyword

        # 有搜索意图修饰语的不是纯品牌导航词
        if any(s in kw for s in self._INTENT_SIGNALS):
            return False

        # 提取行业后缀词
        industry_suffixes = set()
        if industry:
            for part in re.split(r'[/、，,]', industry):
                part = part.strip()
                if 2 <= len(part) <= 8:
                    industry_suffixes.add(part)
        if business_scope:
            for part in re.split(r'[/、，,\s]', business_scope):
                part = part.strip()
                if 2 <= len(part) <= 8:
                    industry_suffixes.add(part)

        if not industry_suffixes:
            return False

        # 检查是否匹配 [prefix][industry_suffix] 模式
        for suffix in industry_suffixes:
            if not kw.endswith(suffix):
                continue
            prefix = kw[:-len(suffix)]

            # 前缀太短（0-2字符）通常是描述词，不是品牌名
            if len(prefix) < 3:
                continue

            # 前缀包含允许的地域词 → 地域搜索，不是品牌词
            if any(geo in prefix for geo in allowed_geo if len(geo) >= 2):
                return False

            # 前缀是通用描述词 → 不是品牌词
            if prefix in self._GENERIC_DESCRIPTORS:
                return False

            # 前缀包含通用描述词（如"全屋定制" 包含 "全屋"+"定制"）→ 不是品牌词
            if any(d in prefix for d in self._GENERIC_DESCRIPTORS if len(d) >= 2):
                return False

            # 前缀 ≥3字符，不是地域，不是通用描述 → 很可能是竞品品牌词
            return True

        return False

    def _has_region(self, keyword: str, city: str, region_info: dict = None, cities_list: list[str] = None) -> bool:
        """判断关键词是否包含地域信息"""
        if not city and not cities_list:
            return False
        # 检查任一城市名是否出现在关键词中
        if cities_list:
            for c in cities_list:
                if c in keyword:
                    return True
        elif city and city in keyword:
            return True
        if region_info:
            for sub in region_info.get("sub_regions", []):
                # 兼容新格式（dict）和旧格式（str）
                name = sub["name"] if isinstance(sub, dict) else sub
                if name in keyword:
                    return True
            for alias in region_info.get("region_aliases", []):
                if alias in keyword:
                    return True
        return False

    async def _call_llm(self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, max_retries: int = 2) -> str:
        """统一的 LLM 调用，带自动重试"""
        if not self.llm_api_key:
            print("    LLM API Key 未配置")
            return ""

        for attempt in range(max_retries + 1):
            try:
                from tools.llm_call_tracker import infer_platform_from_url, llm_track, usage_from_response_payload

                async with aiohttp.ClientSession() as session:
                    async with llm_track(
                        "keyword_expander",
                        infer_platform_from_url(self.llm_endpoint),
                        model="deepseek-v4-flash",
                        metadata={"attempt": attempt + 1},
                    ) as tracker:
                        async with session.post(
                            self.llm_endpoint,
                            headers={
                                "Authorization": f"Bearer {self.llm_api_key}",
                                "Content-Type": "application/json"
                            },
                            json={
                                "model": "deepseek-v4-flash",
                                "messages": [{"role": "user", "content": prompt}],
                                "max_tokens": max_tokens,
                                "temperature": temperature,
                            },
                            timeout=aiohttp.ClientTimeout(total=60)
                        ) as response:
                            data = await response.json()
                        input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                        if "error" in data:
                            tracker.record(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cached_tokens=cached_tokens,
                                success=False,
                                error_msg=str(data["error"])[:200],
                            )
                            print(f"    LLM API 错误 (尝试 {attempt+1}/{max_retries+1}): {data['error']}")
                            if attempt < max_retries:
                                await asyncio.sleep(2)
                                continue
                            return ""
                        if "choices" in data and data["choices"]:
                            tracker.record(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cached_tokens=cached_tokens,
                                success=True,
                            )
                            return data["choices"][0]["message"]["content"]
                        tracker.record(success=False, error_msg=f"LLM API 空响应: {str(data)[:200]}")
                        return ""
            except Exception as e:
                print(f"    LLM 调用失败 (尝试 {attempt+1}/{max_retries+1}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2)
                    continue
                return ""
        return ""

    # [工单 P0-2] 下钻深度上限文案 · market_level 驱动
    _DRILL_DEPTH_RULES = {
        "district": (
            "本客户是**街边店/到店消费型**,允许下钻到街道/镇/片区(3-5 个)。"
        ),
        "city": (
            "本客户是**城市级服务商**,最深只能下钻到**区/县**。"
            "**严禁**输出任何街道、镇、片区、社区名;输入本身就是区/县时返回空列表。"
        ),
        "regional": (
            "本客户是**区域型服务商**(城市群/全省),最深只能下钻到**地级市**。"
            "**严禁**输出区/县/街道;输入是地级市或更细时返回空列表。"
        ),
        "national": (
            "本客户是**全国型服务商**,不需要地域下钻,直接返回空列表。"
        ),
    }

    async def _drill_region(self, region: str, market_level: str = DEFAULT_MARKET_LEVEL) -> dict:
        """LLM 地域下钻：将省/市/区拆解为子区域列表(深度受 market_level 约束)。

        [工单 P0-2 2026-07-26] 老版本对区级输入无条件下钻到街道,给全国 B2B 服务商
        推出"龙岗龙岗街道TikTok线上展厅搭建服务商"。现在街道下钻**只对 district
        (真街边店)开放**,其余层级越界的子区域在解析后再兜底剔除一次。
        """
        level = normalize_market_level(market_level)
        prompt = GEO_REGION_DRILL_PROMPT.format(
            region=region,
            depth_rule=self._DRILL_DEPTH_RULES.get(level, self._DRILL_DEPTH_RULES["city"]),
        )
        content = await self._call_llm(prompt, temperature=0.1, max_tokens=800)
        if not content:
            return {}

        # 解析 JSON
        try:
            # 清理 markdown 代码块
            text = content.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            text = text.strip()
            parsed = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            print(f"    地域下钻 JSON 解析失败: {content[:200]}")
            return {}

        return self._enforce_drill_depth(parsed, level)

    # 下钻结果里"父级本身已经是城市或更细"的层级标记
    _CITY_OR_FINER_LEVELS = ("city", "municipality", "district")

    @staticmethod
    def _enforce_drill_depth(region_info: dict, market_level: str) -> dict:
        """程序化兜底:LLM 越界给出的子区域一律剔除(prompt 只是软约束)。

        [工单 P0-2] 三条守卫:
          1. 策略只允许到城市级(regional/national),而父级本身已是城市/直辖市/区
             → 子区域**全部丢弃**(再下钻就是越界);
          2. 策略允许到区县级,父级是城市/直辖市 → 子区域必须是区/县形态
             ("坂田"/"横岗"/"布吉"这类街道镇名直接丢);
          3. 任何层级下,含"街道/片区/商圈/社区"的名字在非 district 策略下一律丢。
        父级是省时,子区域是地级市(无区县后缀),不受第 2 条影响。
        """
        if not isinstance(region_info, dict):
            return {}
        policy = geo_policy_for(market_level)
        if policy["allow_street"]:
            return region_info

        subs = region_info.get("sub_regions") or []
        region_level = str(region_info.get("region_level") or "").strip()
        parent_is_city_or_finer = region_level in KeywordExpander._CITY_OR_FINER_LEVELS
        max_depth = policy["max_geo_depth"]

        if max_depth == "city" and parent_is_city_or_finer:
            if subs:
                print(
                    f"    [下钻深度守卫] market_level 只允许到城市级 · 父级已是"
                    f"{region_level or '城市'} → 丢弃全部 {len(subs)} 个子区域"
                )
            region_info = dict(region_info)
            region_info["sub_regions"] = []
            region_info["depth_enforced"] = market_level
            return region_info

        kept, dropped = [], []
        for sub in subs:
            name = sub.get("name") if isinstance(sub, dict) else sub
            name = str(name or "").strip()
            if not name:
                continue
            if contains_street_level(name):
                dropped.append(name)
                continue
            if (
                max_depth == "district"
                and parent_is_city_or_finer
                and not name.endswith(("区", "县", "旗"))
            ):
                # 地级市/直辖市下钻出的非"区/县"名字 = 街道/镇/片区俗名
                dropped.append(name)
                continue
            kept.append(sub)
        if dropped:
            print(f"    [下钻深度守卫] 剔除 {len(dropped)} 个越界子区域: {'、'.join(dropped[:5])}")
        region_info = dict(region_info)
        region_info["sub_regions"] = kept
        region_info["depth_enforced"] = market_level
        return region_info

    @staticmethod
    def _build_scope_context_block(scope_lock: ScopeLock | None) -> str:
        """[工单 P0-1/P0-5] 把范围裁决结果写进 prompt:服务市场 + 买家画像 + 真实问法种子。

        `real_query_seeds` 是**风格锚**不是答案:让 LLM 照着真人问法的语气造词,
        堵住"卖点素材拼成运营黑话"那条路。
        """
        if not scope_lock:
            return ""
        lines = ["\n### 范围锁定裁决（选词前置 · 以下为准，资料里的注册地址不作数）"]
        if scope_lock.service_market:
            lines.append(f"- 服务市场: {'、'.join(scope_lock.service_market)}")
        lines.append(f"- 市场层级: {scope_lock.market_level}")
        lines.append(f"- 业务类型: {scope_lock.business_type}")
        if scope_lock.buyer_persona:
            lines.append(f"- 买家画像: {scope_lock.buyer_persona}")
        if scope_lock.real_query_seeds:
            lines.append("- **真实问法种子（风格锚 · 生成的词要像这些句子）**:")
            lines.extend(f"  · {seed}" for seed in scope_lock.real_query_seeds)
            lines.append(
                "  ⚠️ 种子是**语气与颗粒度的参照**，不要原样复制，也不要偏离这种"
                "「真人一句话」的说法去造术语。"
            )
        return "\n".join(lines) + "\n"

    async def _llm_expand(
        self,
        core_keywords: list[str],
        industry: str,
        city: str,
        business_scope: str,
        count: int,
        region_detail: str = "",
        profile_data: dict | None = None,  # P0.3
        brand_name: str = "",  # [CTO-15.23 2026-05-11] 品牌锚定 · prompt 试金石升级
        scope_lock: ScopeLock | None = None,  # [工单 2026-07-26 P0-1/P0-2/P0-5]
    ) -> list[tuple[str, str]]:
        """LLM 语义拓词，返回 [(关键词, 意图阶段), ...]"""

        # P0.3 (CTO-15.7 2026-04-24): 注入 v3.6/v3.7 道法术器行业素材池
        # 事故:"老王家常菜"拓出"大家好入池" · 跑题率 18% · v3.6 素材池 0 引用
        industry_context = ""
        if industry:
            try:
                from tools.industry_knowledge_collector import get_industry_context
                industry_context = get_industry_context({
                    "industry": industry,
                    "category": (profile_data or {}).get("industry_category") or (profile_data or {}).get("category") or "",
                    "industry_brief": (profile_data or {}).get("industry_brief") or {},
                }) or ""
                # 截断避免 prompt token 超限
                if industry_context:
                    industry_context = "\n" + industry_context[:1500] + "\n"
            except Exception:
                industry_context = ""

        # P0.3 · 全国场景禁编造随机城市词(老板批)
        city_guard = ""
        if not city or city in ("全国", "不限"):
            city_guard = "\n- **全国场景**:不要编造具体城市名,只生成全国通用场景词\n"

        # M1b · business_type + city_scope 4 分支 addon 注入
        # profile_data 支持:
        #   business_type ∈ {B2C, B2B, 政企} · 来自 client_profiles.business_type
        #   city_scope / service_scope ∈ {local, national, hybrid} · fallback 由 city 推
        # [工单 P0-1 2026-07-26] 范围裁决结果优先于 profile 里的死默认值:
        #   生产实证 brands.business_type/city_scope 313/314 行 = B2C/local(从未真实采集),
        #   直接吃这个默认值 = 把全国 B2B 服务商当街边店推词。
        _pd = profile_data or {}
        if scope_lock:
            _bt = scope_lock.business_type
            _cs = city_scope_from_market_level(scope_lock.market_level)
        else:
            _bt = (_pd.get("business_type") or "B2C")
            _cs = (_pd.get("city_scope") or _pd.get("service_scope") or "").strip()
            if not _cs:
                _cs = "national" if (not city or city in ("全国", "不限")) else "local"
            # hybrid 归 local(保留地域锚点)· 不单独分支
            if _cs == "hybrid":
                _cs = "local"
        business_type_addon = _resolve_business_type_addon(_bt, _cs)

        # B4 (CTO-15.9 session 3 · 2026-04-25 · M1b §M2 + Codex 0424 P1.2a/b)
        # 6 层关键词矩阵(全行业强制)+ 行业 4 模板(子串匹配)
        # 注入顺序:6 层矩阵(最先 · 强制约束)→ 行业模板(若命中)→ business_type → industry_context
        industry_template_addon = _resolve_industry_template(industry)

        # 把 addon 拼进 industry_context 最前面(保持 prompt 模板结构)
        combined_context = _KEYWORD_LAYERS_MATRIX  # 6 层矩阵全行业强制
        if industry_template_addon:
            combined_context += industry_template_addon
        if business_type_addon:
            combined_context += business_type_addon
        if industry_context:
            combined_context += industry_context

        # [工单 P0-2] 地域策略由 market_level 决定(替代无条件 60% 硬配额)
        geo_policy = geo_policy_for(scope_lock.market_level if scope_lock else DEFAULT_MARKET_LEVEL)
        depth_label = _GEO_DEPTH_LABELS.get(geo_policy["max_geo_depth"], "城市级")
        geo_policy_block = (
            f"- {geo_policy['guidance']}\n"
            f"- 地域词参考占比:约 {geo_policy['geo_ratio_hint']}%（参考值,不是必须凑够的配额）\n"
            f"- 地域锚点最深只到:**{depth_label}**"
        )

        prompt = GEO_EXPAND_PROMPT.format(
            methodology=GEO_KEYWORD_METHODOLOGY,
            industry_context=combined_context,
            scope_context=self._build_scope_context_block(scope_lock),
            geo_policy=geo_policy_block,
            # [CTO-15.23 2026-05-11] 品牌锚定:LLM 在试金石阶段评估"AI 是否会推荐到此品牌"
            #   不传时退化为"客户品牌"兜底文案 · prompt 仍工作但锚定力较弱
            brand_name=brand_name.strip() or "客户品牌",
            core_keywords=", ".join(core_keywords),
            industry=industry or "未指定",
            city=city or "全国",
            business_scope=business_scope or "未指定",
            count=count,
            region_detail=(region_detail or "") + city_guard,
        )

        # v1_2 (CTO-14.0 2026-04-19): temperature 0.7 → 0.1 降场景发散波动
        # 目的：同一品牌同行业同地域多次查询关键词池尽量一致，避免用户感觉"每次算出来不一样"
        content = await self._call_llm(prompt, temperature=0.1, max_tokens=3000)
        if not content:
            return []

        results = []

        # 尝试解析 JSON 数组格式（有些 LLM 返回 ["关键词1", "关键词2", ...]）
        text = content.strip()
        if text.startswith("```"):
            # 提取 markdown 代码块内容
            blocks = text.split("```")
            if len(blocks) >= 3:
                inner = blocks[1]
                if inner.startswith("json"):
                    inner = inner[4:]
                text = inner.strip()

        if text.startswith("["):
            try:
                arr = json.loads(text)
                for item in arr:
                    if isinstance(item, str) and len(item.strip()) >= 3:
                        kw = item.strip()
                        results.append((kw, self._categorize_by_intent(kw)))
                if results:
                    return results[:count]
            except (json.JSONDecodeError, TypeError):
                pass  # 降级到逐行解析

        # 逐行解析（标准格式）
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue

            # 跳过明显的标题/分隔行
            if line.startswith(("##", "---", "```", "**")):
                continue

            # 兼容：如果 LLM 仍输出 [阶段] 标注，清理掉
            for tag in ("认知", "考察", "对比", "决策", "通用"):
                line = line.replace(f"[{tag}]", "").strip()

            # 清理 markdown 列表前缀（"- "、"* "）
            line = re.sub(r"^[\-\*]\s+", "", line).strip()

            # 清理编号前缀（如 "1. "、"2、"、"3) "），但保留关键词中的数字
            cleaned = re.sub(r"^\d+[\.\、\)\]\s]+", "", line).strip()

            # 清理引号包裹
            if len(cleaned) >= 2 and cleaned[0] in ('"', '"', '"', '「') and cleaned[-1] in ('"', '"', '"', '」'):
                cleaned = cleaned[1:-1].strip()

            if cleaned and len(cleaned) >= 3 and not cleaned.startswith(("#", ">", "|")):
                category = self._categorize_by_intent(cleaned)
                results.append((cleaned, category))

        return results[:count]

    async def _5118_expand(
        self,
        core_keywords: list[str],
        count_per_keyword: int
    ) -> list[dict]:
        """5118 API 数据拓词，带重试"""
        client = await get_5118_client()
        all_keywords = []

        for keyword in core_keywords:
            for attempt in range(3):
                try:
                    result = await client.mine_longtail_keywords(
                        keyword=keyword,
                        page_size=min(count_per_keyword, 100)
                    )
                    if result.get("success"):
                        kws = result.get("keywords", [])
                        all_keywords.extend(kws)
                        break  # 成功则跳出重试循环
                    elif attempt < 2:
                        print(f"    5118 返回空结果({keyword})，重试 {attempt+2}/3...")
                        await asyncio.sleep(1)
                except Exception as e:
                    print(f"    5118 拓词失败({keyword}, 尝试 {attempt+1}/3): {e}")
                    if attempt < 2:
                        await asyncio.sleep(1)

        return all_keywords

    async def _llm_filter(
        self,
        keywords: list[dict],
        industry: str,
        city: str,
        business_scope: str
    ) -> list[dict]:
        """LLM 语义筛选：将 SEO 词转化为 GEO 词"""
        if not keywords:
            return []

        if not self.llm_api_key:
            print("    LLM 未配置，使用规则筛选")
            return self._rule_filter(keywords, city, industry)

        # 构建关键词列表
        kw_lines = []
        for item in keywords:
            sem = item.get("sem_price", 0)
            kw_lines.append(f"{item['keyword']} (SEM:¥{sem})" if sem else item["keyword"])

        prompt = GEO_FILTER_PROMPT.format(
            methodology=GEO_KEYWORD_METHODOLOGY,
            city=city or "全国",
            industry=industry,
            business_scope=business_scope or "未指定",
            keyword_list="\n".join(kw_lines)
        )

        # v1_2 (CTO-14.0 2026-04-19): temperature 0.3 → 0.1 进一步稳定化过滤结果
        content = await self._call_llm(prompt, temperature=0.1, max_tokens=4000)
        if not content:
            return self._rule_filter(keywords, city, industry)

        # 解析优化结果
        original_map = {item["keyword"].lower(): item for item in keywords}
        optimized = []

        for line in content.split("\n"):
            line = line.strip()
            if not line or "->" not in line:
                continue

            parts = line.split("->", 1)
            if len(parts) != 2:
                continue

            original = parts[0].strip()
            # 移除 SEM 标记
            if " (SEM:" in original:
                original = original.split(" (SEM:")[0].strip()

            result = parts[1].strip()
            orig_data = original_map.get(original.lower())

            if "[删除]" in result:
                continue
            elif "[保留]" in result:
                if orig_data:
                    optimized.append(orig_data)
            else:
                new_kw = result.strip()
                # 清理LLM常见的"改造为："/"优化为："等前缀
                for prefix in ("改造为：", "改造为:", "改造为 ", "优化为：", "优化为:", "优化为 ",
                               "改写为：", "改写为:", "改写为 ", "调整为：", "调整为:", "调整为 "):
                    if new_kw.startswith(prefix):
                        new_kw = new_kw[len(prefix):].strip()
                        break
                # 清理LLM附带的解释内容：如果包含"？或者"/"（"等，只取第一个有效短句
                for sep in ("？或者", "? 或者", "？ 或者", "（", "(", "，或者"):
                    if sep in new_kw:
                        new_kw = new_kw.split(sep)[0].strip().rstrip("？?")
                        break
                # 关键词不应超过30字（超过说明LLM附带了多余内容）
                if len(new_kw) > 30:
                    continue
                if new_kw and len(new_kw) >= 3:
                    optimized.append({
                        "keyword": new_kw,
                        "sem_price": orig_data.get("sem_price", 0) if orig_data else 0,
                        "bidword_kwc": orig_data.get("bidword_kwc", 0) if orig_data else 0,
                        "optimized_from": original
                    })

        return optimized

    def _rule_filter(self, keywords: list[dict], city: str, industry: str) -> list[dict]:
        """规则筛选（LLM 不可用时的降级方案）"""
        major_cities = [
            "北京", "上海", "广州", "深圳", "成都", "杭州", "天津", "武汉",
            "南京", "重庆", "西安", "苏州", "郑州", "长沙", "青岛", "大连",
            "厦门", "宁波", "无锡", "佛山", "合肥", "福州", "昆明", "哈尔滨",
            "济南", "沈阳", "长春", "石家庄", "太原", "南昌", "贵阳", "兰州",
            "海口", "三亚", "银川", "西宁", "拉萨", "南宁", "乌鲁木齐",
        ]
        # 解析多城市，所有客户城市都应保留
        allowed = set(self._parse_cities(city)) if city else set()
        # 添加省份
        # [2026-07-26 Deploy-CTO] 必须遍历快照:原写法 `for c in allowed: allowed.add(...)`
        # 边迭代边改集合 → RuntimeError: Set changed size during iteration。
        # 这个 bug 一直休眠,是本批 P0-3「整串地址先解析出市/省/区」把它激活的 ——
        # 旧版 _parse_cities('广东省深圳市龙岗区') 返回整串、命中不了 CITY_TO_PROVINCE,
        # 循环体从不执行;新版返回 ['深圳'] → 命中 '广东' → 当场炸。
        # 触发面 = LLM 筛选失败的降级路径(1879 / 1898 两个调用点),
        # 即「LLM 挂了本该降级」变成「扩词整个 500」,安全网自己破洞。
        for c in list(allowed):
            province = self.CITY_TO_PROVINCE.get(c)
            if province:
                allowed.add(province)
        exclude_cities = [c for c in major_cities if c not in allowed] if allowed else []

        filtered = []
        for item in keywords:
            kw = item["keyword"]
            if self._contains_excluded_city(kw, exclude_cities, allowed):
                continue
            filtered.append(item)

        return filtered

    def _categorize_by_intent(self, keyword: str) -> str:
        """基于用户意图对关键词分类"""
        kw = keyword

        if any(w in kw for w in ["价格", "多少钱", "报价", "费用", "收费", "预算", "成本", "性价比", "月供", "首付"]):
            return "决策"
        if any(w in kw for w in ["排名", "排行", "对比", "哪家好", "哪个好", "最好", "前十", "口碑"]):
            return "对比"
        if any(w in kw for w in ["怎么选", "如何选", "靠谱", "正规", "怎么样", "可靠", "坑人"]):
            return "考察"
        if any(w in kw for w in ["推荐", "有没有", "哪里有", "附近", "找", "去哪", "有哪些", "哪家", "哪个平台", "找谁"]):
            return "认知"

        return "通用"

    def _check_geo_feasibility(self, keyword: str) -> bool:
        """判断关键词在 AI 搜索中是否会推荐商家/品牌。

        [Review-CTO 2026-07-23 返工 · 唯一引擎] 本方法不再自带规则:
        全量判定逻辑已移入 services.commercial_query_policy._buyer_intent_gate
        (行为等价迁移),报价扩词与提交、诊断 8 问、自定义词共用同一
        CommercialQueryPolicy 引擎,禁止再各自发明标准(SSOT §3)。

        报价扩词保持 fail-closed:未证明购买决策意图的候选默认不进报价。
        """
        from services.commercial_query_policy import buyer_intent_eligible
        return buyer_intent_eligible(keyword)


async def expand_keywords_for_client(
    core_keywords: list[str],
    industry: str = "",
    city: str = "",
    business_scope: str = "",
    target_count: int = 100,
    profile_data: dict | None = None,  # P0.3 CTO-15.7 2026-04-24
    brand_name: str = "",  # [CTO-15.23 2026-05-11] 品牌锚定 · 解扩词出泛市场词事故
    scope_lock: dict | None = None,  # [工单 2026-07-26 P0-1] 范围锁定裁决
) -> dict:
    """
    为客户扩展关键词

    Args:
        profile_data: (P0.3 新)可选品牌深度数据 · 含 industry_brief (v3.6 service_scope/
                      local_competitors/hot_formats 等)· 透传到 _llm_expand 调
                      get_industry_context 注入行业素材池(道法术器)
        scope_lock: (工单 2026-07-26 P0-1)范围锁定裁决结果 ·
                    `services.quote_scope_lock.resolve_scope_lock(...).as_dict()` ·
                    决定服务市场/市场层级/地域策略/问法种子。不传 = 按资料保守推断
                    (有城市 → city 级 · 无城市 → national),**街道下钻默认关闭**。

    示例:
        result = await expand_keywords_for_client(
            core_keywords=["装修公司", "家装"],
            industry="装修",
            city="深圳",
            business_scope="家装、办公室装修、别墅装修",
            target_count=100,
            profile_data={"industry_brief": {...}, "industry_category": "家装"},
        )
    """
    expander = KeywordExpander()
    return await expander.expand_keywords(
        core_keywords=core_keywords,
        industry=industry,
        city=city,
        business_scope=business_scope,
        target_count=target_count,
        profile_data=profile_data,  # P0.3
        brand_name=brand_name,  # [CTO-15.23 2026-05-11] 品牌锚定透传
        scope_lock=scope_lock,  # [工单 2026-07-26 P0-1] 范围锁定裁决透传
    )
