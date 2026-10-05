"""
定价系数动态配置地基 (D1 · 2026-06-04)

统一管理可后台动态调整的【全局】定价系数:出厂进货折扣 / 积分换算 / 充值档位 /
bonus 赠送阶梯 / 代理进货档 / 报价&媒体默认系数。

读取优先级(仿 config/v3_3_1_flags + services/agent_pricing.get_platform_fee_config):
  1. 环境变量 PRICING_CONFIG (整个 JSON · 紧急回滚通道)
  2. system_settings.pricing_config (JSONB · admin 后台可调 · 不重启)
  3. 模块默认 _PRICING_DEFAULTS (= 当前线上写死值 · B5: 改前=改后 · 上线零感知)

设计原则:
  - 默认值【严格等于】当前各处硬编码值 → DB 无配置时行为零变化(B5 验收: 逐值比对)
  - 进程缓存 30s + reload_pricing_config() 热加载(无需重启)
  - DB 异常 fallback 默认 · 不阻塞核心扣费/报价
  - 浅合并:DB/env 只覆盖它显式给出的顶层 key · 其余回落默认
  - 红线:不碰 billing.py · per-agent 系数不在这里(走 D3)· 绝不写回全局价格锁缓存(B2)

关联:
  - docs/AI-CONTEXT/PRICING_COEFFICIENT_DYNAMIC_PLAN_2026-06-04.md (D1)
  - memory feedback_invited_user_inherits_inviter_markup / project_backend_dynamic_pricing_2026_06_04
"""

import copy
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("GEO-PricingConfig")

# ============================================================
# 默认值 = 当前线上写死值(实证 file:line · 2026-06-04)
# 改这里 = 改"上线零感知基线" · 必须与各处硬编码逐字对齐(B5)
# ============================================================

_PRICING_DEFAULTS: Dict[str, Any] = {
    # 出厂进货折扣(示例 9 折)— services/agent_pricing.py:39-40 WHOLESALE_NUMER/DENOM
    #   factory_cents = ceil(points × numer / denom)
    #   points       = cents × denom // numer  (agent_workbench_api._calc_prepay_points 同源)
    "wholesale_numer": 225,
    "wholesale_denom": 325,

    # 积分换算 1 元 = 130 积分 — api/wallet_api.py:93 / services/agent_pricing.py:34
    "points_per_yuan": 130,

    # [D3 · SSOT business-governance-master §9.5/§9.6/§17.1] 竞品数量兜底(秘塔无结果时的
    # 定价启发式) — 默认 = tools/keyword_value_scorer.estimate_competition_from_keyword 原
    # 硬编码 15/10/3/5(改前=改后·上线零感知)。把它挪到 admin 可治理的 pricing_config 后,
    # 该"第二套 _INFO_PATTERNS"不再是硬编码暗改定价的 rogue 分类器,而是 admin 显式可配的
    # 定价设置(§9.5 例外的正解:加 admin 设置);DB 无配置时数值零变化(§9.6)。
    "competition_fallback_counts": {"strong": 15, "commercial": 10, "info": 3, "other": 5},

    # 报价 markup 系统默认 — config/settings_manager.py (quote_markup_ratio)
    # [§4.5/决策5 2026-06-06] 2.0→1.0:默认回成本(operator 自控·被邀请客户继承上级系数)
    "quote_markup_default": 1.0,

    # 媒体发布默认系数 — 真实生效在 mhz_config.markup_ratio(meijiehezi);此处仅兜底默认
    "media_markup_default": 2.0,

    # [P0-0 2026-06-13] 媒体成本 SSOT 接入开关(本批仅定义 · 报价路径暂不读 = 行为零变化)
    #   media_tier_cost_source: 'constant'=现状 transparent_pricing.MEDIA_TIER_COSTS(默认);
    #     batch-2(P0-A)校准后翻 'snapshot' 才真正接 tools/media_cost_ssot 真实底价 + 平台媒体倍率。
    #   cost_snapshot_schema_version: 与 media_cost_ssot.COST_SNAPSHOT_SCHEMA_VERSION 对齐。
    "media_tier_cost_source": "constant",
    "cost_snapshot_schema_version": "mcs_v1",

    # [P0-A/B/C 2026-06-13 · 完整修复 · 老板审核口径 · 默认=保守(本批仅 flag 开后读)]
    #   ⚠️ platform_media_markup 不在此另建 SSOT — 必须读 mhz_config.markup_ratio(代发定价·correction 2)。
    "article_service_overhead_yuan": 50,        # D1 写稿+运营 overhead(在上级进货倍率【之外】平价加·不×媒体倍率)
    "value_mult_max": 1.5,                      # D2 价值上限(首批不动·与 pricing_bands.V2_VALUE_MULT_MAX 对齐·后置分行业)
    # D3 联合爆价护栏(锁叠乘·不锁成本单独上抬)
    "guard_lever_two_threshold": 1.6,           # 任两杠杆 ratio >此 → needs_review
    "guard_lever_three_threshold": 1.4,         # 任三杠杆 ratio >此 → needs_review;不剥保证价
    # legacy 观测阈值:不再单独剥保证价。剥保证价以入门档售价阈值为准(见下)。
    "guard_factory_ceiling_niche_yuan": 6000,
    "guard_factory_ceiling_local_yuan": 3000,
    "guard_entry_selling_ceiling_niche_yuan": 12000,  # 入门档售价超过此值 → 全国/细分词参考价·人工核
    "guard_entry_selling_ceiling_local_yuan": 8000,   # 入门档售价超过此值 → 地域词参考价·人工核
    "guard_quote_total_ceiling_yuan": 25000,    # 整单 std 总价天花板 → needs_review
    "guard_quote_ratio_threshold": 3.0,         # 整单 new/old 倍数 → needs_review
    "guard_no_cache_selling_yuan": 8000,        # 搜索项 flagship selling >此 → 不进共享缓存
    "guard_national_unclamped_factory_ratio": 2.5,  # D4 全国词放飞专项 factory_ratio 阈
    "cost_procurement_max_depth": 8,            # correction 3 累计媒体倍率上溯深度上限(cycle guard 配套)

    # [P0-D 2026-06-14] 信任资产/引用难度因子(独立批·flag 默认关 0 变化·难度侧非利润侧·全可热调)
    #   缺背书 → 上调篇数侧 true_competition 难度;背书充足 → 反向小幅下调。绝不裸乘利润系数。
    "trust_low_readiness": 0.25,        # citation_readiness ≤ 此 = 缺背书 → 难度因子上探 max_uplift
    "trust_high_readiness": 0.65,       # citation_readiness ≥ 此 = 背书充足 → 难度因子下探 max_discount
    "trust_max_uplift": 1.20,           # 缺背书 true_competition 难度因子上限(篇数侧·非利润)
    "trust_max_discount": 0.92,         # 背书充足时下调下限(篇数侧)
    "trust_compound_value_mult": 1.5,   # 三合一「高价值」阈(value_multiplier ≥ 此)
    "trust_compound_true_comp": 30,     # 三合一「高竞争」阈(true_competition ≥ 此·对齐 five118_missing 口径)
    "trust_stale_days": 30,             # 快照超此天数 = stale(对齐 media_cost COST_SNAPSHOT_MAX_AGE_DAYS)

    # [B4-1 2026-07-03] 飞轮行业引用格局难度因子(与信任资产同 flag PRICING_P0D_TRUST_ASSET_ENABLED · 默认关 0 变化)
    #   读 geo_research_source_signals 该行业引用源集中度:头部域名份额高=行业引用越集中越难挤进→上调篇数;
    #   分散→反向小幅下调。保守 ±10% 篇数侧(非利润侧)。样本不足则完全不消费(零影响)。
    "industry_landscape_low_share": 0.15,    # top 域名份额 ≤ 此 = 分散 → 下探 max_discount
    "industry_landscape_high_share": 0.50,   # top 域名份额 ≥ 此 = 集中 → 上探 max_uplift
    "industry_landscape_max_uplift": 1.10,   # 集中行业 true_competition 上调上限(+10% 篇数侧)
    "industry_landscape_max_discount": 0.95, # 分散行业下调下限(-5% 篇数侧)
    "industry_landscape_min_sources": 20,    # 行业信号样本 < 此 = 数据不足 → 不消费

    # [A4 2026-06-05] 出厂词类 sanity band(markup=1 · 标准档 · 历史按客户价÷2.0 标定 · §4.5 默认markup翻1.0后 band 即客户成本价)
    #   tools/pricing_bands.py 读此覆盖默认 · admin 后台可热调 · ⚠️ provisional(上线后重跑 Q7 回填)
    #   {keyword_type: [出厂 base floor, 出厂 base ceiling]} · 入门×0.65/旗舰×1.45/strong×1.9 由 pricing_bands 缩放
    "keyword_type_factory_bands": {
        "brand_owned":    [200, 500],    # 示例值
        "local_county":   [300, 800],    # 示例值 · 县域
        "local_city":     [600, 1500],   # 示例值 · 城市商业
        "national_niche": [1500, 3000],  # 示例值 · 全国细分
        "national_head":  [3000, 6000],  # 示例值 · 全国红海大词
    },

    # 体验包赠送 — api/wallet_api.py:82 RECHARGE_PACKAGES[trial] / db.wallet_db.grant_trial_bonus
    "trial_bonus_points": 3888,

    # 代理进货 bonus 比例(目前硬编码 10%) — api/agent_workbench_api.py:175-185
    "agent_purchase_bonus_rate": 0.05,

    # 自定义充值金额 bonus 阶梯 — api/wallet_api.py:96-108 _calc_bonus_ratio
    #   规则:从前往后第一个满足 (lt is None) or (amount_yuan < lt) 命中其 rate
    "recharge_bonus_tiers": [
        {"lt": 1, "rate": 0.0},
        {"lt": 100, "rate": 0.10},
        {"lt": 500, "rate": 0.15},
        {"lt": 1000, "rate": 0.20},
        {"lt": 2000, "rate": 0.25},
        {"lt": None, "rate": 0.30},
    ],

    # 充值固定档位包 — api/wallet_api.py:81-88 RECHARGE_PACKAGES(完整快照)
    "recharge_packages": [
        {"id": "trial",    "amount": 0,      "base": 0,      "bonus": 3888,  "label": "体验包", "discount": "免费"},
        {"id": "starter",  "amount": 3000,   "base": 3900,   "bonus": 390,   "label": "试用包", "discount": "送10%"},
        {"id": "popular",  "amount": 10000,  "base": 13000,  "bonus": 1950,  "label": "常用包", "discount": "送15%"},
        {"id": "pro",      "amount": 50000,  "base": 65000,  "bonus": 13000, "label": "专业包", "discount": "送20%", "recommended": True},
        {"id": "flagship", "amount": 100000, "base": 130000, "bonus": 32500, "label": "旗舰包", "discount": "送25%"},
        {"id": "premium",  "amount": 200000, "base": 260000, "bonus": 78000, "label": "尊享包", "discount": "送30%"},
    ],

    # 代理进货档位 — 渠道激励默认档(2026-06-29)。
    # base/bonus 展示由 api/agent_workbench_api 按当前 wholesale + tier 配置实时重算。
    "agent_purchase_options": [
        {"amount_cents": 50000,   "base_points": 0, "bonus_points": 0,
         "label": "¥500 进货 · 达标认证渠道后自动配货", "is_first_month_bonus": True},
        {"amount_cents": 100000,  "base_points": 0, "bonus_points": 0,
         "label": "¥1000 进货 · 认证渠道常用档", "is_first_month_bonus": True},
        {"amount_cents": 300000,  "base_points": 0, "bonus_points": 0,
         "label": "¥3000 进货 · 达标优选渠道", "is_first_month_bonus": True},
        {"amount_cents": 500000,  "base_points": 0, "bonus_points": 0,
         "label": "¥5000 进货 · 优选渠道加速档", "is_first_month_bonus": True},
        {"amount_cents": 1000000, "base_points": 0, "bonus_points": 0,
         "label": "¥10000 进货 · 达标战略渠道", "is_first_month_bonus": True},
        {"amount_cents": 3000000, "base_points": 0, "bonus_points": 0,
         "label": "¥30000 进货 · 战略渠道经营档", "is_first_month_bonus": True},
    ],
    # 代理进货目录独立 OCC 命名空间；旧配置缺失时确定性视作 v1。
    "agent_purchase_catalog_version": "agent-purchase-v1",

    # [渠道激励 · 2026-06-28] 默认 OFF 的新机制配置。仅 CHANNEL_TIER_ENABLED 打开后读取。
    "agent_tier_config": {
        "certified": {"min_yuan": 500, "bonus_rate": 0.10, "is_enabled": True, "description": "达到认证渠道门槛后，进货享受认证奖励"},
        "preferred": {"min_yuan": 3000, "bonus_rate": 0.15, "is_enabled": True, "description": "达到优选渠道门槛后，进货享受优选奖励"},
        "strategic": {"min_yuan": 10000, "bonus_rate": 0.30, "is_enabled": True, "description": "达到战略渠道门槛后，进货享受战略奖励"},
    },
    "founding": {"cap": 10, "first_order_extra_bonus": 0.10, "min_first_order_yuan": 500},
    "bonus_validity_months": 12,
    "k_default": 1.5,

    # SKU 毛利标签阈值。服务价值包高毛利是正向经营信号,只有负毛利或极端异常才拦截。
    "margin_label_thresholds": {
        "loss_heavy_bps": -4000,
        "loss_light_bps": -1000,
        "healthy_bps": 2000,
        "profit_excellent_bps": 8000,
        "hard_block_bps": 250000,
    },
}

# ============================================================
# 读取 + 缓存(30s · 仿 v3_3_1_flags)
# ============================================================

_SETTINGS_KEY = "pricing_config"
_ENV_KEY = "PRICING_CONFIG"
_CACHE_TTL_SECONDS = 30

_cache: Optional[Dict[str, Any]] = None
_cache_ts: float = 0.0
_cache_epoch: Optional[int] = None   # [P0-17] 加载时的配置纪元 · 跨容器改价探测


def _read_env_override() -> Optional[Dict[str, Any]]:
    """整个 JSON 从环境变量读(紧急回滚通道)· 非法 JSON 忽略"""
    raw = os.environ.get(_ENV_KEY)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.warning("pricing_config: env %s 非法 JSON · 忽略 · %s", _ENV_KEY, exc)
        return None


def _read_db_override() -> Optional[Dict[str, Any]]:
    """从 system_settings.pricing_config(JSONB) 读 · DB 异常 fallback None(不阻塞)"""
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM system_settings WHERE key = %s LIMIT 1", (_SETTINGS_KEY,))
            row = cur.fetchone()
            if not row:
                return None
            val = row["value"] if isinstance(row, dict) else row[0]
            parsed = json.loads(val) if isinstance(val, str) else val
            return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.warning("pricing_config: DB 读取失败 · fallback 默认 · %s", exc)
        return None


def get_pricing_config() -> Dict[str, Any]:
    """返回合并后的定价配置(默认 ← DB ← env)· 进程缓存 30s。

    浅合并:DB/env 仅覆盖其显式给出的顶层 key(含整段 list 替换),其余回落默认。
    """
    global _cache, _cache_ts, _cache_epoch
    now = time.time()
    if _cache is not None and (now - _cache_ts) < _CACHE_TTL_SECONDS:
        # [P0-17] TTL 内仍做纪元探测(2s throttled),捕捉蓝绿另一容器的改价 → 及时失效
        try:
            from services.config_epoch import read_config_epoch
            if read_config_epoch() == _cache_epoch:
                return _cache
        except Exception:  # noqa: BLE001 · 纪元探测失败不阻塞 · 退回 TTL 语义
            return _cache

    merged = copy.deepcopy(_PRICING_DEFAULTS)
    db_override = _read_db_override()
    if db_override:
        merged.update(db_override)
    controlled_catalog_version = merged.get("agent_purchase_catalog_version", "agent-purchase-v1")
    env_override = _read_env_override()
    if env_override:
        merged.update(env_override)  # env 最高优先(紧急回滚)
        # revision 是 DB 事务推进的 OCC 令牌，不能被静态 env 固定后掩盖其他价格输入变更。
        merged["agent_purchase_catalog_version"] = controlled_catalog_version

    try:
        from services.config_epoch import read_config_epoch
        _cache_epoch = read_config_epoch(force=True)  # 回读验证生效版本(§16 Q28)
    except Exception:  # noqa: BLE001
        _cache_epoch = None
    _cache = merged
    _cache_ts = now
    return merged


def reload_pricing_config() -> None:
    """配置改后清缓存(admin 写端点调用 · 热加载无需重启)。

    [P0-17] 同时 bump 配置纪元 → 蓝绿另一容器下次纪元探测(~2s)即失效旧缓存,
    不再依赖等 30s TTL 不一致窗口。bump 失败不阻塞本地 reload。
    """
    global _cache, _cache_ts, _cache_epoch
    _cache = None
    _cache_ts = 0.0
    _cache_epoch = None
    try:
        from services.config_epoch import bump_epoch
        bump_epoch()
    except Exception:  # noqa: BLE001
        pass


def invalidate_pricing_config_cache() -> None:
    """仅清本进程缓存；供已在同一事务 bump epoch 的强一致写端使用。"""
    global _cache, _cache_ts, _cache_epoch
    _cache = None
    _cache_ts = 0.0
    _cache_epoch = None


def get_value(key: str) -> Any:
    """便捷读单个顶层配置值(回落默认)"""
    return get_pricing_config().get(key, _PRICING_DEFAULTS.get(key))


def _deep_merge_nested(default: Any, override: Any) -> Any:
    """深合并 dict 配置；list/标量按 override 整体替换。

    注意:get_pricing_config 继续保持历史浅合并行为。这个 helper 只给渠道激励
    这类嵌套配置读取器使用,避免 DB 只覆盖 preferred 时把 certified/strategic 丢掉。
    """
    if isinstance(default, dict) and isinstance(override, dict):
        merged = copy.deepcopy(default)
        for key, value in override.items():
            merged[key] = _deep_merge_nested(merged.get(key), value)
        return merged
    if override is None:
        return copy.deepcopy(default)
    return copy.deepcopy(override)


# ============================================================
# 强类型便捷读取(各调用点统一用这些 · 默认=线上值)
# ============================================================

def get_wholesale_ratio() -> Tuple[int, int]:
    """出厂折扣整数分子分母 (numer, denom)。默认 (225, 325) = 9 折(示例值)。"""
    cfg = get_pricing_config()
    return int(cfg.get("wholesale_numer", 225)), int(cfg.get("wholesale_denom", 325))


def get_points_per_yuan() -> int:
    return int(get_pricing_config().get("points_per_yuan", 130))


def get_quote_markup_default() -> float:
    return float(get_pricing_config().get("quote_markup_default", 1.0))


def get_media_markup_default() -> float:
    return float(get_pricing_config().get("media_markup_default", 2.0))


# ---- [P0-A/B/C 2026-06-13 完整修复] 成本/护栏/层级 便捷读取 ----
def get_article_overhead_yuan() -> float:
    """写稿+运营 overhead(在上级进货倍率之外平价加 · 不参与媒体倍率)。默认 ¥30。"""
    return float(get_pricing_config().get("article_service_overhead_yuan", 50))


def get_value_mult_max() -> float:
    """价值系数上限(首批 1.75 · 与 pricing_bands.V2_VALUE_MULT_MAX 对齐)。"""
    return float(get_pricing_config().get("value_mult_max", 1.5))


def get_cost_procurement_max_depth() -> int:
    """累计媒体倍率上溯深度上限(cycle guard 配套)。默认 8。"""
    return int(get_pricing_config().get("cost_procurement_max_depth", 8))


def get_blowup_guard_config() -> Dict[str, float]:
    """联合爆价护栏阈值(全可热调 · 默认见 _PRICING_DEFAULTS)。"""
    cfg = get_pricing_config()
    return {
        "lever_two": float(cfg.get("guard_lever_two_threshold", 1.6)),
        "lever_three": float(cfg.get("guard_lever_three_threshold", 1.4)),
        "factory_ceiling_niche": float(cfg.get("guard_factory_ceiling_niche_yuan", 6000)),
        "factory_ceiling_local": float(cfg.get("guard_factory_ceiling_local_yuan", 3000)),
        "entry_selling_ceiling_niche": float(cfg.get("guard_entry_selling_ceiling_niche_yuan", 12000)),
        "entry_selling_ceiling_local": float(cfg.get("guard_entry_selling_ceiling_local_yuan", 8000)),
        "quote_total_ceiling": float(cfg.get("guard_quote_total_ceiling_yuan", 25000)),
        "quote_ratio": float(cfg.get("guard_quote_ratio_threshold", 3.0)),
        "no_cache_selling": float(cfg.get("guard_no_cache_selling_yuan", 8000)),
        "national_unclamped_factory_ratio": float(cfg.get("guard_national_unclamped_factory_ratio", 2.5)),
    }


def get_trust_asset_config() -> Dict[str, Any]:
    """[P0-D 2026-06-14] 信任资产难度因子阈值(全可热调 · 默认见 _PRICING_DEFAULTS)。"""
    cfg = get_pricing_config()
    return {
        "low_readiness": float(cfg.get("trust_low_readiness", 0.25)),
        "high_readiness": float(cfg.get("trust_high_readiness", 0.65)),
        "max_uplift": float(cfg.get("trust_max_uplift", 1.20)),
        "max_discount": float(cfg.get("trust_max_discount", 0.92)),
        "compound_value_mult": float(cfg.get("trust_compound_value_mult", 1.5)),
        "compound_true_comp": float(cfg.get("trust_compound_true_comp", 30)),
        "stale_days": int(cfg.get("trust_stale_days", 30)),
    }


def get_industry_landscape_config() -> Dict[str, Any]:
    """[B4-1 2026-07-03] 飞轮行业引用格局难度因子阈值(全可热调 · 默认见 _PRICING_DEFAULTS)。"""
    cfg = get_pricing_config()
    return {
        "low_share": float(cfg.get("industry_landscape_low_share", 0.15)),
        "high_share": float(cfg.get("industry_landscape_high_share", 0.50)),
        "max_uplift": float(cfg.get("industry_landscape_max_uplift", 1.10)),
        "max_discount": float(cfg.get("industry_landscape_max_discount", 0.95)),
        "min_sources": int(cfg.get("industry_landscape_min_sources", 20)),
    }


def get_trial_bonus_points() -> int:
    return int(get_pricing_config().get("trial_bonus_points", 3888))


def get_agent_purchase_bonus_rate() -> float:
    return float(get_pricing_config().get("agent_purchase_bonus_rate", 0.05))


def calc_recharge_bonus_ratio(amount_yuan: Optional[float]) -> float:
    """自定义充值金额 → bonus 比例(替代 api/wallet_api._calc_bonus_ratio · 默认阶梯一致)"""
    if amount_yuan is None:
        return 0.0
    tiers = get_pricing_config().get("recharge_bonus_tiers") or _PRICING_DEFAULTS["recharge_bonus_tiers"]
    for tier in tiers:
        lt = tier.get("lt")
        if lt is None or amount_yuan < lt:
            return float(tier.get("rate", 0.0))
    return 0.0


def get_recharge_packages() -> List[Dict[str, Any]]:
    return get_pricing_config().get("recharge_packages") or _PRICING_DEFAULTS["recharge_packages"]


def get_agent_purchase_options() -> List[Dict[str, Any]]:
    cfg = get_pricing_config()
    # 只有 key 缺失才回退默认。显式空列表/全部停用是有效配置，不能把默认档复活。
    raw = cfg["agent_purchase_options"] if "agent_purchase_options" in cfg else _PRICING_DEFAULTS["agent_purchase_options"]
    from services.agent_inventory_pricing import normalize_catalog_options
    return normalize_catalog_options(raw)


def get_agent_purchase_catalog_version(config: Optional[Dict[str, Any]] = None) -> str:
    """读取代理进货目录版本；旧配置缺字段确定性为 agent-purchase-v1。"""
    cfg = config if config is not None else get_pricing_config()
    raw = cfg.get("agent_purchase_catalog_version", "agent-purchase-v1")
    text = str(raw or "agent-purchase-v1")
    import re
    if not re.fullmatch(r"agent-purchase-v[1-9][0-9]*", text):
        logger.warning("非法代理进货目录版本 %r · 按 v1 兼容读取", raw)
        return "agent-purchase-v1"
    return text


def merge_pricing_config(db_override: Optional[Dict[str, Any]], *, include_env: bool = True) -> Dict[str, Any]:
    """无缓存合并 helper，供持锁事务读取配置；不访问 DB、无副作用。"""
    merged = copy.deepcopy(_PRICING_DEFAULTS)
    if isinstance(db_override, dict):
        merged.update(copy.deepcopy(db_override))
    if include_env:
        env_override = _read_env_override()
        if env_override:
            controlled_catalog_version = merged.get("agent_purchase_catalog_version", "agent-purchase-v1")
            merged.update(copy.deepcopy(env_override))
            merged["agent_purchase_catalog_version"] = controlled_catalog_version
    return merged


def get_pricing_env_override() -> Optional[Dict[str, Any]]:
    """供管理端显示/阻止环境覆盖；返回副本，避免调用方修改解析结果。"""
    value = _read_env_override()
    return copy.deepcopy(value) if value else None


def get_agent_tier_config() -> Dict[str, Dict[str, float]]:
    cfg = get_pricing_config()
    return _deep_merge_nested(_PRICING_DEFAULTS["agent_tier_config"], cfg.get("agent_tier_config"))


def get_founding_config() -> Dict[str, float]:
    cfg = get_pricing_config()
    return _deep_merge_nested(_PRICING_DEFAULTS["founding"], cfg.get("founding"))


def get_bonus_validity_months() -> int:
    return int(get_pricing_config().get("bonus_validity_months", _PRICING_DEFAULTS["bonus_validity_months"]))


def get_k_default() -> float:
    return float(get_pricing_config().get("k_default", _PRICING_DEFAULTS["k_default"]))


def get_margin_label_thresholds() -> Dict[str, float]:
    cfg = get_pricing_config()
    return _deep_merge_nested(
        _PRICING_DEFAULTS["margin_label_thresholds"],
        cfg.get("margin_label_thresholds"),
    )
