"""
services/quote_scope_lock.py — 报价选词 · 范围锁定裁决(Scope Lock)

工单: docs/AI-CONTEXT/QUOTE_KEYWORD_CORRECTION_WORKORDER_2026-07-26.md · P0-1 / P0-2 / P0-3

【为什么有这一层】
报价扩词此前把 `brands.cities`(常常是**注册地址**"广东省深圳市龙岗区")当服务市场用,
再叠加"约 60% 地域词且必须下钻子区域"的硬配额,于是给全国 B2B 服务商推出
"龙岗龙岗街道TikTok线上展厅搭建服务商"这类街道级废词;与此同时客户自己的城市
("深圳")因为整串地址提不出城市名而落进 150+ 硬编码城市黑名单 —— 真词全被打
"范围需复核",废词全放行。

本模块是**选词前置的一次范围裁决**,把"客户到底服务哪个市场、到哪一级"从
硬编码猜测改回推理:输入品牌资料 + 最近一次诊断报告摘要(有则必用),输出

    service_market   归一后的服务市场城市(不是注册地址)
    market_level     district / city / regional / national
    business_type    B2C / B2B / 政企
    buyer_persona    真实买家画像
    real_query_seeds 5-8 条真人会对 AI 说的问法种子(供扩词做风格锚)

裁决结果驱动地域策略(见 `geo_policy_for`):`national` 只到城市级且地域词占比低,
`city` 以城市级为主、区级少量、**禁街道**,只有 `district`(真街边店)才保留街道下钻。

【铁律】
- 永不中断(元指令 13):没有诊断报告、LLM 不可用、解析失败,一律回落 `heuristic_scope_lock`,
  绝不返回错误、绝不阻断扩词。
- 纯推理层:不写库、不计价、不做付费交付资格判定(那是 CommercialQueryPolicy 的唯一职责)。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

SCOPE_LOCK_VERSION = "quote-scope-lock-v1"

MARKET_LEVELS = ("district", "city", "regional", "national")
BUSINESS_TYPES = ("B2C", "B2B", "政企")

# 市场层级 → 关键词地域策略(P0-2:杀 60% 硬配额 · 街道下钻只留给 district)
_GEO_POLICY: dict[str, dict] = {
    "national": {
        "geo_ratio_hint": 10,
        "max_geo_depth": "city",
        "allow_street": False,
        "allow_sub_region": False,
        "drill_region": False,
        "guidance": (
            "全国服务商:地域词占比 0-20%,且**最深只到城市级**"
            "(如「深圳XX服务商推荐」);严禁区级/街道级地域词。"
        ),
    },
    "regional": {
        "geo_ratio_hint": 35,
        "max_geo_depth": "city",
        "allow_street": False,
        "allow_sub_region": False,
        "drill_region": True,
        "guidance": (
            "区域型服务商:地域词约 30-40%,只到**城市级**(可覆盖服务半径内多个城市),"
            "严禁下钻到区/县/街道。"
        ),
    },
    "city": {
        "geo_ratio_hint": 50,
        "max_geo_depth": "district",
        "allow_street": False,
        "allow_sub_region": True,
        "drill_region": True,
        "guidance": (
            "城市级服务商:地域词约 40-60%,以**城市级为主**,区级少量且必须带城市名前缀"
            "(如「深圳龙岗XX推荐」);**严禁街道/镇/片区级**地域词。"
        ),
    },
    "district": {
        "geo_ratio_hint": 60,
        "max_geo_depth": "street",
        "allow_street": True,
        "allow_sub_region": True,
        "drill_region": True,
        "guidance": (
            "街边店/到店型本地商家:地域词约 50-70%,可下钻到区/街道/商圈"
            "(区级及更细必须带城市名前缀)。"
        ),
    },
}

DEFAULT_MARKET_LEVEL = "city"


# ============================================================================
# 行政区划解析(P0-3 · "广东省深圳市龙岗区" → 省=广东 / 市=深圳 / 区=龙岗)
# ============================================================================

_PROVINCE_RE = r"(?:省|自治区|特别行政区)"
_CITY_RE = r"(?:市|自治州|地区|盟)"
_DISTRICT_RE = r"(?:区|县|旗)"
_STREET_RE = r"(?:街道|镇|乡|片区|社区|村)"

_ADMIN_RE = re.compile(
    rf"^(?P<province>.{{2,8}}?{_PROVINCE_RE})?"
    rf"(?P<city>.{{2,10}}?{_CITY_RE})?"
    rf"(?P<district>.{{1,8}}?{_DISTRICT_RE})?"
    rf"(?P<street>.{{1,8}}?{_STREET_RE})?$"
)

_SPLIT_RE = re.compile(r"[,，、;；\s/|]+")

_NON_MARKET_TOKENS = {"全国", "不限", "各地", "全网", "线上", "无", "-", "—"}


def normalize_admin_name(name: str) -> str:
    """去掉行政后缀,给出口语地名。

    与 `KeywordExpander._normalize_city` 的差别:那一个是**无上下文**的保守剥离
    (不敢剥"州/区/县",怕把"惠州"吃成"惠"),本函数只在**已确认该 token 的行政层级**
    之后调用,因此可以安全剥离对应后缀。
    """
    if not name:
        return ""
    text = str(name).strip()
    for suffix in ("特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区", "自治区",
                   "自治州", "自治县", "地区"):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    for suffix in ("省", "市", "盟", "区", "县", "旗"):
        # 单字后缀只在长度足够时剥(防"云县"→"云"这类过短残留)
        if text.endswith(suffix) and len(text) - len(suffix) >= 2:
            return text[: -len(suffix)]
    return text


def parse_admin_region(text: str) -> dict:
    """把一个地址 token 拆成 {province, city, district, raw, level}。

    命中不到任何行政后缀时(如"深圳"/"珠三角"),province/city/district 全 None,
    调用方应把 raw 当作主 token 使用(向后兼容旧行为)。
    """
    raw = str(text or "").strip()
    out = {
        "raw": raw, "province": None, "city": None,
        "district": None, "street": None, "level": "unknown",
    }
    if not raw:
        return out
    m = _ADMIN_RE.match(raw)
    if not m:
        return out
    province, city = m.group("province"), m.group("city")
    district, street = m.group("district"), m.group("street")
    if not any((province, city, district, street)):
        return out
    out["province"] = province
    out["city"] = city
    out["district"] = district
    out["street"] = street
    if street:
        out["level"] = "street"
    elif district:
        out["level"] = "district"
    elif city:
        out["level"] = "city"
    elif province:
        out["level"] = "province"
    return out


def parse_region_hierarchy(city_str: str) -> dict:
    """解析(可能多个、可能带完整地址的)地区字符串。

    Returns:
        {
          "cities": ["深圳"],            # 归一后的主城市(服务市场候选)
          "sub_regions": ["龙岗", "龙岗区"],  # 区/县(带与不带后缀两种写法)
          "provinces": ["广东"],
          "raw_parts": ["广东省深圳市龙岗区"],
          "deepest_level": "district",
        }
    """
    cities: list[str] = []
    sub_regions: list[str] = []
    provinces: list[str] = []
    streets: list[str] = []
    raw_parts: list[str] = []
    deepest = "unknown"
    _rank = {"unknown": 0, "province": 1, "city": 2, "district": 3, "street": 4}

    for part in _SPLIT_RE.split(str(city_str or "").strip()):
        part = part.strip()
        if not part or part in _NON_MARKET_TOKENS:
            continue
        raw_parts.append(part)
        parsed = parse_admin_region(part)
        if _rank.get(parsed["level"], 0) > _rank.get(deepest, 0):
            deepest = parsed["level"]
        if parsed["province"]:
            prov = normalize_admin_name(parsed["province"])
            if prov and prov not in provinces:
                provinces.append(prov)
        if parsed["city"]:
            city = normalize_admin_name(parsed["city"])
            if city and city not in cities:
                cities.append(city)
        if parsed["district"]:
            for name in (parsed["district"], normalize_admin_name(parsed["district"])):
                if name and name not in sub_regions:
                    sub_regions.append(name)
        if parsed["street"]:
            for name in (parsed["street"], normalize_admin_name(parsed["street"])):
                if name and name not in streets:
                    streets.append(name)
        if not parsed["city"]:
            # 没解析出市级 → 按老口径把整串当主 token(如"深圳"/"朝阳区"/"珠三角");
            # 只有"省"单独出现时才归一成口语省名(老 _normalize_city 同口径)。
            if parsed["province"] and not parsed["district"] and not parsed["street"]:
                fallback = normalize_admin_name(parsed["province"])
            else:
                fallback = parsed["raw"]
            if fallback and fallback not in cities:
                cities.append(fallback)

    return {
        "cities": cities,
        "sub_regions": sub_regions,
        "provinces": provinces,
        "streets": streets,
        "raw_parts": raw_parts,
        "deepest_level": deepest,
    }


# 省级行政区名(用**白名单**判"省",避免"北京装修省钱攻略"这类误伤)
_PROVINCE_NAMES = (
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
)
_PROVINCE_ADDRESS_RE = re.compile(
    rf"(?:{'|'.join(_PROVINCE_NAMES)})(?:省|自治区|特别行政区)"
)
# "XX市YY区"连写(开头)· 排除"上市/超市/菜市/夜市/早市"这类非行政"市"
_CITY_DISTRICT_ADDRESS_RE = re.compile(
    r"^[一-龥]{2,4}(?<!上)(?<!超)(?<!菜)(?<!夜)(?<!早)市[一-龥]{1,4}(?:区|县|旗)"
)


def is_full_address_form(text: str) -> bool:
    """判断关键词里是否夹着**整串行政地址**(真人不会这样问 AI)。

    命中例:"广东省深圳市龙岗区TikTok工厂出海获客服务推荐"、"深圳市龙岗区XX推荐"
    不命中:"深圳龙岗TikTok代运营推荐"、"深圳TikTok代运营哪家好"、"北京装修省钱攻略"
    """
    kw = str(text or "")
    if not kw:
        return False
    if _PROVINCE_ADDRESS_RE.search(kw):
        return True
    if _CITY_DISTRICT_ADDRESS_RE.search(kw):
        return True
    return False


# 通用街道级信号:只收误伤率极低的后缀。
# 「镇/乡/村」故意不在通用集里 —— "景德镇"是地级市、"中山镇"是地名残留,
# 通用匹配会误杀客户自己的城市;它们只在 `street_names`(真实下钻出的街道名)里生效。
STREET_TOKENS = ("街道", "片区", "商圈", "社区")


def contains_street_level(
    text: str,
    street_names: Optional[list] = None,
    allowed_names: Optional[list] = None,
) -> bool:
    """关键词是否含街道/片区级地名。

    `allowed_names` 里的地名(客户城市/省/区)占据的字符位置受保护,
    命中必须完全落在未受保护区间才算数(同 `_contains_excluded_city` 的思路,
    防"景德镇陶瓷厂家推荐"这类把城市名当街道误杀)。
    """
    kw = str(text or "")
    if not kw:
        return False

    protected: set[int] = set()
    for allowed in allowed_names or []:
        allowed = str(allowed or "").strip()
        if len(allowed) < 2:
            continue
        start = 0
        while True:
            idx = kw.find(allowed, start)
            if idx == -1:
                break
            protected.update(range(idx, idx + len(allowed)))
            start = idx + 1

    candidates = list(STREET_TOKENS)
    for name in street_names or []:
        name = str(name or "").strip()
        if len(name) >= 2:
            candidates.append(name)

    for token in candidates:
        start = 0
        while True:
            idx = kw.find(token, start)
            if idx == -1:
                break
            if not set(range(idx, idx + len(token))) & protected:
                return True
            start = idx + 1
    return False


def geo_policy_for(market_level: str) -> dict:
    """市场层级 → 地域策略(未知层级回落 city,永不抛错)。"""
    level = normalize_market_level(market_level)
    return dict(_GEO_POLICY[level])


def normalize_market_level(value: Any) -> str:
    level = str(value or "").strip().lower()
    if level in MARKET_LEVELS:
        return level
    # 常见同义写法
    if level in ("local", "town", "street"):
        return "district"
    if level in ("province", "provincial", "multi_city", "region"):
        return "regional"
    if level in ("nationwide", "china", "global", "overseas"):
        return "national"
    return DEFAULT_MARKET_LEVEL


def normalize_business_type(value: Any) -> str:
    text = str(value or "").strip()
    if text in BUSINESS_TYPES:
        return text
    upper = text.upper()
    if upper in ("B2B", "TOB", "TO B"):
        return "B2B"
    if upper in ("B2C", "TOC", "TO C"):
        return "B2C"
    if text in ("政企", "政府", "国企", "事业单位", "G端", "toG"):
        return "政企"
    return "B2C"


def city_scope_from_market_level(market_level: str) -> str:
    """market_level → 老字段 `city_scope`(local/national)映射。

    老的 M1b 4 分支 addon 只认 local/national,语义是"词里要不要有地域锚点":
      - district / city / regional → `local`(要地域锚点,深浅由 geo_policy 决定)
      - national                   → `national`(0 地域锚点)
    ⚠️ 地域词的**占比与最深层级**一律以 `geo_policy_for(market_level)` 为准,
       addon 文案里的百分比只是老口径的粗描述。
    """
    level = normalize_market_level(market_level)
    return "national" if level == "national" else "local"


# ============================================================================
# ScopeLock 数据结构
# ============================================================================

@dataclass(frozen=True)
class ScopeLock:
    """一次范围锁定裁决的结果(不可变 · 可 as_dict 落库/回前端)。"""

    service_market: tuple[str, ...] = ()
    market_level: str = DEFAULT_MARKET_LEVEL
    business_type: str = "B2C"
    buyer_persona: str = ""
    real_query_seeds: tuple[str, ...] = ()
    sub_regions: tuple[str, ...] = ()
    provinces: tuple[str, ...] = ()
    source: str = "heuristic"          # llm / heuristic / manual
    used_diagnosis: bool = False
    diagnosis_id: Optional[int] = None
    reason: str = ""
    version: str = SCOPE_LOCK_VERSION

    def as_dict(self) -> dict:
        policy = geo_policy_for(self.market_level)
        return {
            "version": self.version,
            "service_market": list(self.service_market),
            "market_level": self.market_level,
            "business_type": self.business_type,
            "city_scope": city_scope_from_market_level(self.market_level),
            "buyer_persona": self.buyer_persona,
            "real_query_seeds": list(self.real_query_seeds),
            "sub_regions": list(self.sub_regions),
            "provinces": list(self.provinces),
            "source": self.source,
            "used_diagnosis": self.used_diagnosis,
            "diagnosis_id": self.diagnosis_id,
            "reason": self.reason,
            "geo_policy": policy,
        }

    @property
    def geo_policy(self) -> dict:
        return geo_policy_for(self.market_level)


def scope_lock_from_dict(data: Any) -> Optional[ScopeLock]:
    """把前端回传/落库的 dict 还原成 ScopeLock(人工修改后的确认值走这条路)。"""
    if not isinstance(data, dict) or not data:
        return None
    market = [
        str(c).strip() for c in (data.get("service_market") or [])
        if str(c or "").strip()
    ]
    seeds = [
        str(s).strip() for s in (data.get("real_query_seeds") or [])
        if str(s or "").strip()
    ]
    return ScopeLock(
        service_market=tuple(dict.fromkeys(market)),
        market_level=normalize_market_level(data.get("market_level")),
        business_type=normalize_business_type(data.get("business_type")),
        buyer_persona=str(data.get("buyer_persona") or "").strip(),
        real_query_seeds=tuple(dict.fromkeys(seeds))[:8],
        sub_regions=tuple(str(s).strip() for s in (data.get("sub_regions") or []) if str(s or "").strip()),
        provinces=tuple(str(s).strip() for s in (data.get("provinces") or []) if str(s or "").strip()),
        source=str(data.get("source") or "manual"),
        used_diagnosis=bool(data.get("used_diagnosis")),
        diagnosis_id=data.get("diagnosis_id"),
        reason=str(data.get("reason") or "")[:300],
        version=str(data.get("version") or SCOPE_LOCK_VERSION),
    )


# ============================================================================
# 诊断报告摘要(P0-1 · 有诊断必用)
# ============================================================================

def build_diagnosis_summary(record: Optional[dict]) -> dict:
    """从一条 diagnosis_records 记录抽出范围裁决需要的信息。

    只读、宽容:任何字段缺失都不抛错(诊断结构历代变过好几版)。
    """
    if not isinstance(record, dict) or not record:
        return {}

    raw: dict = {}
    raw_json = record.get("raw_data_json")
    if isinstance(raw_json, str) and raw_json.strip():
        try:
            raw = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            raw = {}
    elif isinstance(raw_json, dict):
        raw = raw_json

    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    if not data and isinstance(record.get("data"), dict):
        data = record["data"]
    input_params = raw.get("input_params") if isinstance(raw.get("input_params"), dict) else {}
    business_context = data.get("business_context") if isinstance(data.get("business_context"), dict) else {}

    questions = business_context.get("real_user_questions") or data.get("questions") or []
    if isinstance(questions, str):
        questions = [questions]
    questions = [str(q).strip() for q in questions if str(q or "").strip()][:8]

    competitors = (
        business_context.get("competitors")
        or data.get("competitors")
        or business_context.get("competitor_names")
        or []
    )
    if isinstance(competitors, str):
        competitors = [competitors]
    competitors = [str(c).strip() for c in competitors if str(c or "").strip()][:8]

    summary = {
        "diagnosis_id": record.get("id"),
        "brand_name": record.get("brand_name") or "",
        "industry": record.get("industry") or "",
        "total_score": record.get("total_score"),
        "client_location": input_params.get("client_location") or "",
        "core_business": business_context.get("core_business") or "",
        "target_customers": business_context.get("target_customers") or "",
        "value_proposition": business_context.get("value_proposition") or "",
        "real_user_questions": questions,
        "competitors": competitors,
    }
    return {k: v for k, v in summary.items() if v not in ("", None, [], {})}


# ============================================================================
# 启发式裁决(LLM 不可用时的兜底 · 绝不阻断)
# ============================================================================

_NATIONAL_HINTS = (
    "全国", "跨境", "海外", "出海", "外贸", "国际", "线上", "saas", "软件", "系统",
    "平台", "代运营", "招商", "加盟", "电商", "远程", "云",
)
_B2B_HINTS = (
    "b2b", "企业", "工厂", "厂家", "供应链", "供应商", "批发", "采购", "招投标",
    "外贸", "代运营", "服务商", "解决方案", "系统集成", "saas", "crm", "erp",
    "工业", "机械", "设备", "制造",
)
_GOV_HINTS = ("政企", "政府", "国企", "事业单位", "招标", "信创", "等保", "央企")
_LOCAL_STORE_HINTS = (
    "到店", "门店", "上门", "附近", "本地生活", "餐厅", "餐饮", "美容", "美发",
    "洗车", "汽修", "宠物", "家政", "维修", "母婴", "口腔", "健身",
)


def heuristic_scope_lock(
    brand: Optional[dict] = None,
    diagnosis_summary: Optional[dict] = None,
) -> ScopeLock:
    """无 LLM 的确定性裁决:够用、可解释、绝不阻断。"""
    brand = brand or {}
    diag = diagnosis_summary or {}

    raw_cities = str(brand.get("cities") or brand.get("city") or "").strip()
    if not raw_cities:
        raw_cities = str(diag.get("client_location") or "").strip()
    hierarchy = parse_region_hierarchy(raw_cities)

    text_blob = " ".join(
        str(v) for v in (
            brand.get("name"), brand.get("industry"), brand.get("business_scope"),
            brand.get("core_business"), diag.get("core_business"),
            diag.get("target_customers"), diag.get("value_proposition"),
            brand.get("materials"),
        ) if v
    ).lower()

    if any(h in text_blob for h in _GOV_HINTS):
        business_type = "政企"
    elif any(h in text_blob for h in _B2B_HINTS):
        business_type = "B2B"
    else:
        business_type = "B2C"

    has_city = bool(hierarchy["cities"])
    national_signal = any(h in text_blob for h in _NATIONAL_HINTS)
    local_store_signal = any(h in text_blob for h in _LOCAL_STORE_HINTS)

    if not has_city:
        market_level = "national"
    elif national_signal and not local_store_signal:
        # 有注册城市但业务是全国/跨境型 → regional(保留本城主场,不推街道)
        market_level = "regional"
    elif local_store_signal and business_type == "B2C":
        market_level = "district"
    else:
        market_level = "city"

    persona_bits = [
        diag.get("target_customers") or brand.get("target_customers") or "",
        diag.get("core_business") or brand.get("industry") or "",
    ]
    buyer_persona = "·".join([p for p in persona_bits if p])[:120]

    seeds = _heuristic_seeds(
        cities=hierarchy["cities"],
        market_level=market_level,
        industry=str(brand.get("industry") or diag.get("industry") or "").strip(),
        core_terms=_core_terms(brand, diag),
    )

    return ScopeLock(
        service_market=tuple(hierarchy["cities"]),
        market_level=market_level,
        business_type=business_type,
        buyer_persona=buyer_persona,
        real_query_seeds=tuple(seeds),
        sub_regions=tuple(hierarchy["sub_regions"]),
        provinces=tuple(hierarchy["provinces"]),
        source="heuristic",
        used_diagnosis=bool(diag),
        diagnosis_id=diag.get("diagnosis_id"),
        reason="LLM 范围裁决不可用 · 按品牌资料/诊断摘要规则回落(不阻断流程)",
    )


def _core_terms(brand: dict, diag: dict) -> list[str]:
    terms: list[str] = []
    for value in (
        brand.get("core_keywords"), brand.get("business_scope"),
        diag.get("core_business"), brand.get("industry"), diag.get("industry"),
    ):
        if isinstance(value, (list, tuple)):
            terms.extend(str(v).strip() for v in value if str(v or "").strip())
        elif isinstance(value, str) and value.strip():
            terms.extend(
                p.strip() for p in re.split(r"[,，、;；/\n]+", value) if 2 <= len(p.strip()) <= 12
            )
    deduped: list[str] = []
    for t in terms:
        if t and t not in deduped:
            deduped.append(t)
    return deduped[:4]


def _heuristic_seeds(
    cities: list[str], market_level: str, industry: str, core_terms: list[str],
) -> list[str]:
    """兜底问法种子:只用真实存在的业务词 + 自然口语后缀,绝不发明术语。"""
    stems = [t for t in core_terms if t] or ([industry] if industry else [])
    if not stems:
        return []
    prefix = cities[0] if (cities and market_level in ("district", "city", "regional")) else ""
    suffixes = ("哪家好", "服务商推荐", "公司推荐", "多少钱")
    seeds: list[str] = []
    for suffix in suffixes:
        for stem in stems:
            seed = f"{prefix}{stem}{suffix}" if prefix else f"{stem}{suffix}"
            if 4 <= len(seed) <= 24 and seed not in seeds:
                seeds.append(seed)
            if len(seeds) >= 6:
                return seeds
    return seeds


# ============================================================================
# LLM 范围裁决
# ============================================================================

SCOPE_LOCK_PROMPT = """你是 B2B/B2C 市场定位分析师。请判断下面这家公司**真正的服务市场和层级**,
并给出真人会对 AI 说的问法种子。

## 客户资料
- 品牌名: {brand_name}
- 行业: {industry}
- 资料里填的地区: {raw_cities}
  ⚠️ 这个字段经常填的是**工商注册地址**(如"广东省深圳市龙岗区"),不等于服务市场。
- 核心业务词: {core_keywords}
- 业务范围/卖点素材: {business_scope}

## 最近一次诊断报告摘要
{diagnosis_block}

## 判断规则
1. **service_market**: 这家公司实际接单的市场城市(口语地名,如"深圳",不要写"广东省深圳市龙岗区")。
   全国/跨境业务可以只写主场城市或留空数组。
2. **market_level** 四选一:
   - `district`: 街边店/到店消费型(餐厅、美发、洗车),客户只来自本区几公里
   - `city`: 服务半径 = 一座城市(本地装修、本地家政、城市级服务商)
   - `regional`: 服务半径 = 城市群/省(珠三角、长三角、全省)
   - `national`: 全国接单 / 跨境出海 / 线上交付(SaaS、外贸代运营、跨境电商)
   ⚠️ 判 `district` 必须有"到店/上门/本区"这类硬证据;B2B、外贸、代运营、SaaS **不可能**是 district。
3. **business_type**: B2B / B2C / 政企
4. **buyer_persona**: 谁掏钱做决定(一句话,如"深圳及珠三角外贸工厂老板/外贸经理")
5. **real_query_seeds**: 5-8 条**真人会对 DeepSeek/Kimi 说出口的原句**。
   - 必须是自然问法,像"深圳TikTok代运营哪家好"、"深圳工厂TikTok获客代运营服务商"
   - 严禁运营黑话/自造术语(如"线上展厅搭建""全链路赋能方案")
   - 严禁整串行政地址(如"广东省深圳市龙岗区XX")
   - market_level 是 national/regional 时,种子里**不要**出现区/街道名

## 输出格式(严格 JSON,不要任何其他内容)
{{"service_market": ["深圳"],
  "market_level": "city",
  "business_type": "B2B",
  "buyer_persona": "...",
  "real_query_seeds": ["...", "..."],
  "reason": "一句话依据"}}"""


async def resolve_scope_lock(
    brand: Optional[dict] = None,
    diagnosis_summary: Optional[dict] = None,
    llm_call: Optional[Callable[[str], Awaitable[str]]] = None,
) -> ScopeLock:
    """范围锁定裁决主入口。

    Args:
        brand: {name, industry, cities, business_scope, core_keywords, ...}
        diagnosis_summary: `build_diagnosis_summary()` 的产物(有诊断则必传)
        llm_call: 可注入的异步 LLM 调用(测试/离线用);默认走 multi_llm_caller

    永不抛错:LLM 挂了、JSON 烂了、字段缺了,一律返回 `heuristic_scope_lock`。
    """
    brand = brand or {}
    diag = diagnosis_summary or {}
    fallback = heuristic_scope_lock(brand, diag)

    try:
        caller = llm_call or _default_llm_call
        prompt = _build_scope_prompt(brand, diag)
        content = await caller(prompt)
        parsed = _parse_scope_json(content)
        if not parsed:
            return fallback
        return _merge_llm_decision(parsed, fallback, diag)
    except Exception as exc:  # pragma: no cover - 防御性:任何异常都不许阻断扩词
        print(f"    [scope_lock] LLM 范围裁决失败 · 回落启发式: {exc}")
        return fallback


async def _default_llm_call(prompt: str) -> str:
    from tools.multi_llm_caller import call_llm_with_fallback
    return await call_llm_with_fallback(prompt, verbose=False)


def _build_scope_prompt(brand: dict, diag: dict) -> str:
    if diag:
        lines = []
        for label, key in (
            ("核心业务", "core_business"),
            ("目标客户", "target_customers"),
            ("核心价值", "value_proposition"),
            ("诊断填写的服务地区", "client_location"),
        ):
            if diag.get(key):
                lines.append(f"- {label}: {diag[key]}")
        if diag.get("real_user_questions"):
            lines.append("- 诊断实测的真实用户问题(8 问):")
            lines.extend(f"  · {q}" for q in diag["real_user_questions"])
        if diag.get("competitors"):
            lines.append(f"- 竞品格局: {'、'.join(diag['competitors'])}")
        diagnosis_block = "\n".join(lines) if lines else "(诊断报告无可用摘要)"
    else:
        diagnosis_block = "(该客户暂无诊断报告 · 仅凭资料裁决,不要因此中断)"

    core_keywords = brand.get("core_keywords") or []
    if isinstance(core_keywords, (list, tuple)):
        core_keywords = "、".join(str(k).strip() for k in core_keywords if str(k or "").strip())

    return SCOPE_LOCK_PROMPT.format(
        brand_name=str(brand.get("name") or brand.get("brand_name") or "(未提供)"),
        industry=str(brand.get("industry") or diag.get("industry") or "(未提供)"),
        raw_cities=str(brand.get("cities") or brand.get("city") or "(未填)"),
        core_keywords=str(core_keywords or "(未提供)")[:300],
        business_scope=str(brand.get("business_scope") or "(未提供)")[:800],
        diagnosis_block=diagnosis_block,
    )


def _parse_scope_json(content: Any) -> Optional[dict]:
    if not content or not isinstance(content, str):
        return None
    text = content.strip()
    if text.startswith("```"):
        blocks = text.split("```")
        if len(blocks) >= 2:
            text = blocks[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _merge_llm_decision(parsed: dict, fallback: ScopeLock, diag: dict) -> ScopeLock:
    market_level = normalize_market_level(parsed.get("market_level"))
    business_type = normalize_business_type(parsed.get("business_type"))

    # 硬约束:B2B/政企/全国型业务绝不判 district(街道下钻的唯一入口不能被误开)
    if market_level == "district" and business_type in ("B2B", "政企"):
        market_level = "city"

    market = []
    for city in parsed.get("service_market") or []:
        name = normalize_admin_name(str(city or "").strip())
        if name and name not in market:
            market.append(name)
    if not market:
        market = list(fallback.service_market)

    seeds: list[str] = []
    for seed in parsed.get("real_query_seeds") or []:
        text = re.sub(r"\s+", "", str(seed or "")).strip()
        if not (4 <= len(text) <= 28):
            continue
        if is_full_address_form(text):
            continue
        if market_level in ("national", "regional") and contains_street_level(text):
            continue
        if text not in seeds:
            seeds.append(text)
    if not seeds:
        seeds = list(fallback.real_query_seeds)

    return ScopeLock(
        service_market=tuple(market),
        market_level=market_level,
        business_type=business_type,
        buyer_persona=str(parsed.get("buyer_persona") or fallback.buyer_persona or "")[:200],
        real_query_seeds=tuple(seeds[:8]),
        sub_regions=fallback.sub_regions,
        provinces=fallback.provinces,
        source="llm",
        used_diagnosis=bool(diag),
        diagnosis_id=diag.get("diagnosis_id"),
        reason=str(parsed.get("reason") or "")[:300],
    )
