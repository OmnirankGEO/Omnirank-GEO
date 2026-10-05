"""报价经营包(GEO 域)· 默认模板 + 身份字段投影(零 schema 只读切片 · 2026-06-16)。

设计口径: docs/AI-CONTEXT/PRICING_OPERATION_PACKAGES_PLAN_2026-06-16.md
铁律(本模块只读切片严格遵守):
  - 经营包 = 算价 / 展示 / 建议售价 模板;**启用不扣费**,真实扣费仍走现有算力(完成才扣)。
  - 身份脱敏用 **allowlist 投影**(主防线):每个身份只 append 允许字段,内部字段永不出现在低权限投影里。
  - 客户面绝不见:成本 / 毛利 / 进货价 / 算力库存 / 内部系数 / 服务商利润。
  - 价格为 ¥ 测算展示,非一次性收款。
本切片边界:默认 4 包**硬编码只读**,无 DB、无 per-agent 定制(持久化 + 真实 cost_multiplier 投影是下一批)。
  → 普通用户/客户暂用平台基础成本(base · cost_multiplier=1.0)的默认建议价;真实分账界面一致性在持久化批接入。
"""
from __future__ import annotations

from typing import Any, Optional

# ============================================================
# 默认 4 包(数字来自设计文档 §3 平台基础成本 / §4 服务商经营视角 / §7 客户文案)
#   *_base_*  = §3 平台基础成本口径(cost_multiplier=1.0)
#   *_sub_*   = §4 含服务商成本系数的下级口径
# ============================================================
DEFAULT_PACKAGES: list[dict[str, Any]] = [
    {
        "key": "single_trial",
        "name": "单项试跑包",
        "scope": "1 条本地长尾搜索项",
        "search_item_min": 1, "search_item_max": 1,
        "fit_scene": "单点试水",
        "customer_copy": "先用 1 条本地长尾搜索项试跑一轮, 看 AI 搜索里有没有机会。",
        # §3 普通用户(平台基础成本)
        "base_cost_min": 180, "base_cost_max": 300,
        "base_suggested_price_min": 599, "base_suggested_price_max": 799,
        "base_margin_min": 299, "base_margin_max": 619,
        # §4 服务商视角(含成本系数的下级口径)
        "sub_cost_min": 300, "sub_cost_max": 399,
        "sub_suggested_price_min": 599, "sub_suggested_price_max": 799,
        "sub_margin_min": 200, "sub_margin_max": 500,
    },
    {
        "key": "three_validate",
        "name": "三项验证包",
        "scope": "3 条本地长尾搜索项",
        "search_item_min": 3, "search_item_max": 3,
        "fit_scene": "方向验证",
        "customer_copy": "用 3 条真实获客搜索项验证 AI 搜索优化方向。",
        "base_cost_min": 540, "base_cost_max": 900,
        "base_suggested_price_min": 1980, "base_suggested_price_max": 2980,
        "base_margin_min": 1080, "base_margin_max": 2440,
        "sub_cost_min": 900, "sub_cost_max": 1200,
        "sub_suggested_price_min": 1980, "sub_suggested_price_max": 2980,
        "sub_margin_min": 800, "sub_margin_max": 2000,
    },
    {
        "key": "first_month",
        "name": "首月启动包",
        "scope": "5-6 条搜索项",
        "search_item_min": 5, "search_item_max": 6,
        "fit_scene": "本地品牌首月启动",
        "customer_copy": "适合一个本地服务品牌启动首月 GEO 优化。",
        "base_cost_min": 1440, "base_cost_max": 2400,
        "base_suggested_price_min": 3980, "base_suggested_price_max": 5980,
        "base_margin_min": 1580, "base_margin_max": 4540,
        "sub_cost_min": 1800, "sub_cost_max": 2500,
        "sub_suggested_price_min": 3980, "sub_suggested_price_max": 5980,
        "sub_margin_min": 1500, "sub_margin_max": 3500,
    },
    {
        "key": "multi_operate",
        "name": "多项经营包",
        "scope": "10-15 条搜索项",
        "search_item_min": 10, "search_item_max": 15,
        "fit_scene": "多词多区域持续经营",
        "customer_copy": "适合多条搜索项、多区域持续优化。",
        "base_cost_min": 3000, "base_cost_max": 5000,
        "base_suggested_price_min": 8800, "base_suggested_price_max": 12800,
        "base_margin_min": 3800, "base_margin_max": 9800,
        "sub_cost_min": 4000, "sub_cost_max": 6000,
        "sub_suggested_price_min": 8800, "sub_suggested_price_max": 12800,
        "sub_margin_min": 3000, "sub_margin_max": 7000,
    },
]

# 共享(任何身份都可见)的非敏感骨架字段
_SHARED_PUBLIC_FIELDS = ("key", "name", "scope", "search_item_min", "search_item_max",
                         "fit_scene", "customer_copy")

# 客户面绝不可见的内部字段族(防御纵深 belt · 与 allowlist 投影主防线并列)。
#   命名前缀级黑名单:base_cost*/sub_cost*/*margin*/*经营空间*/系数/库存 …
_CUSTOMER_FORBIDDEN_PREFIXES = ("base_cost", "base_margin", "base_suggested_price",
                                "sub_cost", "sub_margin", "sub_suggested_price",
                                "agent_space", "cost_multiplier", "credit_inventory",
                                "platform_floor", "markup")


def _pick(pkg: dict, fields: tuple) -> dict:
    return {k: pkg[k] for k in fields if k in pkg}


def project_for_admin(pkg: dict) -> dict:
    """平台/Admin:全字段(模板 + 底价 + 成本系数口径)。"""
    return dict(pkg)


def project_for_agent(pkg: dict) -> dict:
    """服务商:自己成本口径 + 下级成本建议 / 下级建议售价 / 下级毛利 + 经营空间(=下级成本-自己成本)。
    不含:平台真实底价明细拆解 / 其他服务商成本(本切片无该数据)。"""
    out = _pick(pkg, _SHARED_PUBLIC_FIELDS)
    out.update(_pick(pkg, (
        "base_cost_min", "base_cost_max",                       # 服务商自己平台成本口径
        "sub_cost_min", "sub_cost_max",                         # 下级成本建议(含系数)
        "sub_suggested_price_min", "sub_suggested_price_max",   # 下级建议售价
        "sub_margin_min", "sub_margin_max",                     # 下级预计毛利
    )))
    # 服务商经营空间 = 下级成本建议 − 自己平台成本(差额提示 · 非平台内交易)。
    #   [复审修 2026-06-16] 按区间端点取【保守 min / 乐观 max】并钳 ≥0:
    #     min = max(0, 下级成本下限 − 自己成本上限);max = 下级成本上限 − 自己成本下限。
    #   原写法(sub_min−base_min, sub_max−base_max)在成本区间重叠时会出 min>max 的无意义区间。
    #   注:§3/§4 硬编码数字非同一 cost_multiplier 推导(区间会重叠),持久化批用 sub=base×系数 一致算后此钳位即恒成立。
    out["agent_space_min"] = max(0, pkg["sub_cost_min"] - pkg["base_cost_max"])
    out["agent_space_max"] = pkg["sub_cost_max"] - pkg["base_cost_min"]
    out["agent_space_note"] = "经营空间为成本差额提示, 按现有佣金/平台外服务费实现, 非平台内交易"
    return out


def project_for_normal_user(pkg: dict) -> dict:
    """普通用户/下级:自己拿到的成本 + 自己建议客户售价 + 预计毛利 + 可上架包。
    不含:上级真实底价 / 上级利润 / 平台底价 / 其他用户配置。
    本切片用平台基础成本(base · cost_multiplier=1.0);真实分账(扫码下级加价)在持久化批按本人系数算。"""
    out = _pick(pkg, _SHARED_PUBLIC_FIELDS)
    out.update(_pick(pkg, (
        "base_cost_min", "base_cost_max",
        "base_suggested_price_min", "base_suggested_price_max",
        "base_margin_min", "base_margin_max",
    )))
    return out


def project_for_customer(pkg: dict, customer_price: Optional[Any] = None,
                         customer_price_max: Optional[Any] = None) -> dict:
    """客户公开链接:仅客户白名单字段 + 服务商对客售价(customer_price)。
    绝不含成本/毛利/进货/库存/内部系数/服务商利润。
    customer_price 缺省时用默认建议价区间(服务商未自设时的展示基线)。"""
    out = _pick(pkg, _SHARED_PUBLIC_FIELDS)
    out["search_item_count"] = (pkg.get("search_item_min")
                                if pkg.get("search_item_min") == pkg.get("search_item_max")
                                else f"{pkg.get('search_item_min')}-{pkg.get('search_item_max')}")
    # 服务商对客售价:优先服务商自设,否则默认建议价区间(零 schema 切片基线)
    out["customer_price_min"] = customer_price if customer_price is not None else pkg["base_suggested_price_min"]
    out["customer_price_max"] = customer_price_max if customer_price_max is not None else pkg["base_suggested_price_max"]
    # 防御纵深 belt:再扫一遍,任何内部前缀字段一律剔除(allowlist 已保证不含,这里双保险)
    return strip_internal_package_fields(out)


def strip_internal_package_fields(pkg: dict) -> dict:
    """[防御纵深 belt] 剔除任何客户禁见的内部字段(与 selection_api._strip 同philosophy)。
    allowlist 投影是主防线;此函数是出口兜底,防将来有人往客户投影里误加内部字段。"""
    return {k: v for k, v in pkg.items()
            if not any(k.startswith(p) for p in _CUSTOMER_FORBIDDEN_PREFIXES)}


# ============================================================
# 持久化行投影(per-服务商 DB 行 · 2026-06-17 持久化批)
#   复用上面的 allowlist 投影主防线,叠加管理字段(id/启用/排序/对客价)。
#   DB 行字段名与 DEFAULT_PACKAGES 同名(base_*/sub_*),故可直接喂给 project_for_*。
# ============================================================
# 管理字段(仅服务商可见 · 客户面绝不出现)
_PERSISTED_MANAGE_FIELDS = ("id", "enabled", "sort_order", "template_key",
                            "customer_price_min", "customer_price_max")

# 0.6 营销测算文案(仅服务商/普通用户经营测算可见 · 绝不进客户面 · 绝不进报价算法)
ECONOMICS_NOTE = ("保守按系统建议成本估算;实际交付常见可按 60%-100% 成本消耗估算经营空间。"
                  "预计成本/毛利为测算展示, 不承诺上榜 / 不保证收益, 实际以最终执行为准。")


def _row_with_key(row: dict) -> dict:
    """DB 行用 template_key, 投影需要 key 作稳定标识(自定义副本 template_key=NULL → pkg_{id})。"""
    r = dict(row)
    if not r.get("key"):
        r["key"] = r.get("template_key") or (f"pkg_{r.get('id')}" if r.get("id") else "pkg")
    return r


def project_persisted_for_agent(row: dict) -> dict:
    """服务商管理视图(持久化行):成本口径 + 经营空间 + 管理字段(可编辑/启用/排序)。"""
    r = _row_with_key(row)
    out = project_for_agent(r)
    out.update(_pick(r, _PERSISTED_MANAGE_FIELDS))
    if "enabled" in out:
        out["enabled"] = bool(out["enabled"])
    return out


def project_persisted_for_normal_user(row: dict) -> dict:
    """普通用户视图(持久化行):自己成本 + 建议售价 + 毛利 + 管理字段(不含下级/经营空间)。"""
    r = _row_with_key(row)
    out = project_for_normal_user(r)
    out.update(_pick(r, _PERSISTED_MANAGE_FIELDS))
    if "enabled" in out:
        out["enabled"] = bool(out["enabled"])
    return out


def project_persisted_for_customer(row: dict) -> dict:
    """客户公开视图(持久化行):仅客户白名单字段 + 服务商对客售价。无成本/毛利/系数/id/owner。"""
    r = _row_with_key(row)
    return project_for_customer(r, r.get("customer_price_min"), r.get("customer_price_max"))


# 身份 → 投影函数(API 层按 role 取用)
def project_packages(role: str, customer_prices: Optional[dict] = None) -> list[dict]:
    """role ∈ {'admin','agent','normal','customer'}。customer_prices: {key: (min,max)} 服务商自设对客价。"""
    if role == "admin":
        return [project_for_admin(p) for p in DEFAULT_PACKAGES]
    if role == "agent":
        return [project_for_agent(p) for p in DEFAULT_PACKAGES]
    if role == "normal":
        return [project_for_normal_user(p) for p in DEFAULT_PACKAGES]
    if role == "customer":
        cp = customer_prices or {}

        def _price_pair(key):
            # [复审硬化 2026-06-16] 只接受合法 (min,max) 二元组;畸形(单值/3元组/字符串/None)→ 回落默认,绝不崩。
            v = cp.get(key)
            return v if isinstance(v, (tuple, list)) and len(v) == 2 else (None, None)

        return [project_for_customer(p, *_price_pair(p["key"])) for p in DEFAULT_PACKAGES]
    # fail-closed:未知 role → 当客户处理(最小可见面)
    return [project_for_customer(p) for p in DEFAULT_PACKAGES]
