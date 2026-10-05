"""平台算力「按需铸造」统一护栏 · T1(充值链平台跳)与 T2(赠送链)共用同一份实现。

工单:docs/AI-CONTEXT/WORKORDER_ONDEMAND_MINTING_2026-07-29.md §2 §3
设计:docs/AI-CONTEXT/DESIGN_ONDEMAND_MINTING_2026-07-29.md §3

🔴 为什么必须有这个模块:
    改成按需铸造之前,"厂家余额不足"是一道**隐性熔断** —— 有 bug 超卖会撞到
    余额停下来。拆掉预铸余额后这道保险消失,bug 会一直铸下去直到有人发现。
    因此必须补**显式**护栏,而且两条铸造路径(paid 平台跳 / bonus 渠道赠送)
    必须调**同一个** assert_mint_allowed;各写一份 = 新路有闸老路没闸。

四道护栏(阈值 Owner 可调 · system_settings[inventory_minting_guard_config]):
    1 一致性锁(主防线)  铸造量必须锚在**独立落库**的订单/履约计划声明量上,超出即拒;
                        平台跳还额外要求与 dealer_resale_fulfillment_hops.points 精确相等。
    2 单笔上限          默认 5,000 万算力,超过即拒。
    3 订单号强制(最硬)  无 related_order_id、或订单在 recharge_orders 不存在 → **拒绝 + 告警**。
                        它把铸造从「管理动作」变成「订单的必然结果」,顺带解决
                        boss_authorized 自填无凭证的问题。
    4 日累计**相对**告警 当日铸造量 > 过去 N 日中位数 × multiple 且 ≥ 地板 → 告警,**不拦**。

🔴 [执行方订正 · 单笔上限] 工单 §3 的 500 万上限会挡死**当前已发布目录里最大的启用进货选项**。
   生产实证(2026-07-29):
     · 已发布进货目录 proc-38a14f190058da75609691d9 与 system_settings.pricing_config 的
       wholesale 10000/13000 **一致 = 130 算力/元**(50,000 分→65,000 算力;
       2,000,000 分→2,600,000;5,000,000 分→6,500,000);
     · 最大**启用**选项 apo_dbd270c97e4b = ¥50,000 → **6,500,000 算力 > 500 万** → 当场被拒;
     · 叠加 strategic 70% 奖励另有 4,550,000 算力的 bonus 铸造。
   订正为单笔 5,000 万:放行 ¥50,000(650 万)乃至 ¥200,000(2,600 万),
   同时挡住"最大启用选项多打一个零"(¥500,000 → 6,500 万)。

   🔴 顺带订正我自己上一版的论据(它也是错的,而且错在同一个坑上):
   我引用"¥10,000 → 1,368,421 算力 = 136.84 算力/元,与 pricing_config 10000/13000 一致"——
   **136.84 ≠ 10000/13000(那是 130)**,且 136.84 是**已被取代的历史出厂率**,
   来自历史订单 AIP630C3D7087214A65。同表还能看到更老的出厂率
   (AIPE5CB8A9B898C4550)。生产 system_settings 里**搜不到 9500**,
   agent_pricing_overrides 的 wholesale_numer/denom 两列**全为 NULL**。
   → 出厂率在本项目出现过多个历史值,**引用必须带版本与出处**,
     不能只报一个数。当前唯一有效口径 = 已发布目录 + pricing_config = **130 算力/元**。

🔴 [执行方订正 · 日累计改相对阈值] 绝对阈值必然要么误报要么漏报:
   100 万/日 → 一笔正常大单(650 万)第一天就打穿;
   5,000 万/日 → 正常日均才几万,等于每天泄漏 1,000 万也不响。
   改为 **当日 > 过去 7 日中位数 × 10** 且 **≥ 地板**才告警,随业务自适应。
   ⚠️ 地板取 2,000 万而不是 50 万,依据是生产铸造的**稀疏性**:近两个月只有 8 个自然日
   有正向库存流水,按自然日补零后中位数长期为 0 → 实际生效的就是地板。
   地板若低于单日正常量(1 笔 ¥50,000 = 650 万),就退化成"一笔大单天天报警",
   正是相对阈值要避免的那个病。2,000 万 ≈ 3 笔当前最大启用选项。
   业务量长起来后中位数会自动接管地板 —— 这才是相对阈值的意义。

🔴 [执行方订正 · 一致性锁形态] 工单 §3-1 写「铸造量必须等于订单声明量」。
   直接做成「同订单累计铸造 == 声明量」会打断 dispute 裁决重结算
   (services/dispute_settlement.py 对同一 order_id 再跑一次 canonical 结算)。
   故拆成两层:**单笔 > 声明量 → 硬拒**(fail-closed);
   **同订单累计 > 声明量 → 告警不拦**(慢性重复铸造照样看得见)。
   平台跳另有与 hops.points 的精确相等,且计划 settled 后幂等短路,双铸在那条路上不可能发生。

🔴 护栏 3 的告警走 ai_ops 独立连接(upsert_alert 自开连接并 commit),
   因此**调用方事务回滚后告警仍在** —— 拒绝必须留痕,静默拒绝等于没有护栏。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("GEO-V35-MintGuard")

GUARD_SETTINGS_KEY = "inventory_minting_guard_config"

# ai_ops 告警管道(与 services/inventory_audit.py 同一 rule_key 家族,便于运营一处看)
ALERT_RULE_KEY = "inventory_minting_guard"
ALERT_FP_ORPHAN = "orphan_mint"
ALERT_FP_DAILY = "daily_volume"
ALERT_FP_OVERMINT = "order_cumulative_overmint"

POOL_PAID = "paid"
POOL_BONUS = "bonus"

# 铸造来源(只用于留痕与 declaration 解析口径,不影响护栏强度)
SOURCE_RESALE_PLATFORM_ROOT = "resale_platform_root"
SOURCE_INVENTORY_PREPAY = "inventory_prepay"
SOURCE_INVENTORY_PURCHASE_AUTO = "inventory_purchase_auto"
SOURCE_INVENTORY_ADMIN_ADJUST = "inventory_admin_adjust"

# 🔴「铸造」= 不从任何账户扣减、凭空产生库存的流水类型。
#    resale_transfer_in / revoke_from_customer / *_refund_in 都有对侧扣减或来自反向冲正,
#    不是铸造,不进护栏也不进日累计。
MINT_TX_TYPES: Tuple[str, ...] = (
    "manufacturer_origin_in",
    "purchase_prepay",
    "purchase_auto",
    "purchase_admin_adjust",
)

# 铸造成本基准(平台厂家单位账面成本)。
# 🔴 默认值 = 生产现有 manufacturer_origin lot 的单位成本
#    (DML7B00B20BB2BCB2AED9C6F92C:130,000,000 算力 / 100,000,000 分),
#    与今天 FIFO 取到的 cost_basis 逐笔一致(floor 同口径),
#    因此逐跳毛利在切换前后**不变** —— 工单 §7「不动逐跳毛利计算」。
DEFAULTS: Dict[str, int] = {
    "max_single_mint_points": 50_000_000,
    # 护栏 4 = 相对阈值:当日 > 过去 N 日中位数 × multiple 且 ≥ 地板 → 告警
    "daily_baseline_days": 7,
    "daily_relative_multiple": 10,
    "daily_alert_floor_points": 20_000_000,
    "mint_cost_numerator_cents": 100_000_000,
    "mint_cost_denominator_points": 130_000_000,
}


class MintingGuardError(RuntimeError):
    """护栏拒绝 · code 用于判别锁与调用方分类。"""

    def __init__(self, code: str, message: str, **context: Any) -> None:
        self.code = str(code)
        self.message = str(message)
        self.context = dict(context)
        super().__init__(f"{self.code}: {self.message}")


# ============================================================
# 配置(Owner 可调 · 非法值 fail-closed)
# ============================================================

def load_guard_config(cursor) -> Dict[str, int]:
    """读护栏配置 · 缺 key 用默认值 · 🔴 有 key 但值非法 → 拒绝铸造(不静默回落默认)。

    静默回落会把 Owner 的一次误配变成「看起来还在按老阈值工作」,
    等于用一个新的静默失败换掉旧的。
    """
    config = dict(DEFAULTS)
    cursor.execute("SELECT value FROM system_settings WHERE key=%s", (GUARD_SETTINGS_KEY,))
    row = cursor.fetchone()
    if not row:
        return config
    raw = row["value"] if isinstance(row, dict) else row[0]
    if raw is None or str(raw).strip() == "":
        return config
    try:
        override = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise MintingGuardError(
            "MINT_GUARD_CONFIG_INVALID", f"铸造护栏配置不是合法 JSON:{exc}",
        ) from exc
    if not isinstance(override, dict):
        raise MintingGuardError("MINT_GUARD_CONFIG_INVALID", "铸造护栏配置必须是 JSON 对象")
    # 🔴 不认识的 key 直接拒:否则 Owner 把 key 名打错(比如沿用已废弃的
    #    daily_alert_points),会以为阈值改了、实际一点没变 —— 又一个静默失败。
    unknown = sorted(set(override) - set(DEFAULTS))
    if unknown:
        raise MintingGuardError(
            "MINT_GUARD_CONFIG_INVALID",
            f"铸造护栏配置含未知字段:{','.join(unknown)}(可用:{','.join(sorted(DEFAULTS))})",
        )
    for key in DEFAULTS:
        if key not in override:
            continue
        value = override[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise MintingGuardError(
                "MINT_GUARD_CONFIG_INVALID", f"铸造护栏配置 {key} 必须是整数",
            )
        if value <= 0:
            raise MintingGuardError(
                "MINT_GUARD_CONFIG_INVALID", f"铸造护栏配置 {key} 必须为正",
            )
        config[key] = int(value)
    return config


def mint_cost_basis_cents(points: int, config: Optional[Dict[str, int]] = None) -> int:
    """铸造批次的账面成本(分)· floor 同口径 + 最低 1 分。

    floor 是为了与切换前 FIFO 的 `remaining_cost * take // remaining_points`
    **逐笔相等**(生产实测:11818→9090、108333→83333、65000→50000 全对上)。
    最低 1 分是因为 dealer_inventory_lots / fulfillment_allocations 都有
    `cost > 0` 的 CHECK;老路径在极小额时会因 cost=0 报 SELLER_COST_INVALID,
    这里补足下界(严格更宽松,不构成回归)。
    """
    cfg = dict(DEFAULTS)
    if config:
        cfg.update(config)
    points = int(points)
    if points <= 0:
        raise MintingGuardError("MINT_POINTS_INVALID", "铸造算力必须为正")
    numerator = int(cfg["mint_cost_numerator_cents"])
    denominator = int(cfg["mint_cost_denominator_points"])
    if numerator <= 0 or denominator <= 0:
        raise MintingGuardError("MINT_GUARD_CONFIG_INVALID", "铸造成本基准配置非法")
    return max(1, points * numerator // denominator)


# ============================================================
# 声明量解析(全部读**独立落库**的行,不信调用方口述)
# ============================================================

def _order_exists(cursor, order_id: str) -> bool:
    cursor.execute("SELECT 1 FROM recharge_orders WHERE id=%s", (str(order_id),))
    return cursor.fetchone() is not None


def _declared_capacity(cursor, *, order_id: str, pool: str, agent_user_id: int) -> int:
    """该订单**声明**了多少算力(上界)。

    基线 = `recharge_orders.base_points + bonus_points`(订单声明的算力总量)。
    🔴 为什么按订单总量而不是按池分别卡:池的切分是结算内部口径,会合法漂移
       —— CONSUMER 单 base+bonus 整笔经 resale 链进 paid 池
       (见 _reserve_v2_consumer_sale 的 base+bonus == plan.points 校验);
       充值主路径的 paid 部分取 pricing_snapshot 的 tool+publish 而不是 base_points。
       按池卡会把这些合法形态判成"铸多了",用一个新的 fail-closed 误伤换掉旧的无闸。
       订单总量是外部支付网关校验过的硬锚,超过它一定是错的。

    bonus 额外并上该订单的 `bonus_grants` 授予合计:渠道等级把 tier 奖励记在
    recharge_orders.bonus_points,创始首单额外奖励只记在 bonus_grants,
    两个都是独立落库的声明,取上界。
    """
    cursor.execute(
        "SELECT base_points, bonus_points FROM recharge_orders WHERE id=%s",
        (str(order_id),),
    )
    row = cursor.fetchone()
    if not row:
        return 0
    base = int((row["base_points"] if isinstance(row, dict) else row[0]) or 0)
    bonus = int((row["bonus_points"] if isinstance(row, dict) else row[1]) or 0)
    declared_total = max(0, base + bonus)
    if pool == POOL_PAID:
        return declared_total
    # 🔴 先探表:bonus_grants 在部分环境/夹具里不存在,直接查会把**调用方事务**
    #    打成 aborted(支付回调整笔失败),而不是退化成"少一个声明来源"。
    cursor.execute("SELECT to_regclass('public.bonus_grants') AS t")
    probe = cursor.fetchone()
    if not ((probe["t"] if isinstance(probe, dict) else probe[0]) if probe else None):
        return declared_total
    cursor.execute(
        """SELECT COALESCE(SUM(granted_points), 0) AS granted
             FROM bonus_grants
            WHERE related_order_id=%s AND owner_type='agent' AND owner_id=%s""",
        (str(order_id), int(agent_user_id)),
    )
    grant_row = cursor.fetchone()
    granted = int((grant_row["granted"] if isinstance(grant_row, dict) else grant_row[0]) or 0)
    return max(declared_total, granted)


def _minted_so_far(cursor, *, order_id: str, pool: str, agent_user_id: int) -> int:
    cursor.execute(
        """SELECT COALESCE(SUM(points), 0) AS minted
             FROM agent_inventory_transactions
            WHERE related_order_id=%s AND pool=%s AND agent_user_id=%s
              AND points > 0 AND type = ANY(%s)""",
        (str(order_id), str(pool), int(agent_user_id), list(MINT_TX_TYPES)),
    )
    row = cursor.fetchone()
    return int((row["minted"] if isinstance(row, dict) else row[0]) or 0)


def daily_minted_points(cursor) -> int:
    """今日已铸造合计(全站 · 两池 · 两条路)。

    🔴 用 LOCALTIMESTAMP 而不是 NOW():agent_inventory_transactions.created_at 是
       `timestamp without time zone`,拿 timestamptz 比较会走隐式换算,
       跨时区部署时静默错窗口。
    """
    cursor.execute(
        """SELECT COALESCE(SUM(points), 0) AS minted
             FROM agent_inventory_transactions
            WHERE points > 0 AND type = ANY(%s)
              AND created_at >= date_trunc('day', LOCALTIMESTAMP)""",
        (list(MINT_TX_TYPES),),
    )
    row = cursor.fetchone()
    return int((row["minted"] if isinstance(row, dict) else row[0]) or 0)


def daily_mint_baseline_points(cursor, *, baseline_days: int) -> int:
    """过去 N 个自然日(不含今日)日铸造量的**中位数**。

    🔴 按自然日补零,不是只对有铸造的日子取中位数 —— 否则"最近只有 1 天铸过"
       会把那天的量当成基线,基线随即被泄漏本身抬高。
    """
    days = max(1, int(baseline_days))
    cursor.execute(
        """WITH span AS (
               SELECT generate_series(
                   date_trunc('day', LOCALTIMESTAMP) - (%s || ' days')::interval,
                   date_trunc('day', LOCALTIMESTAMP) - INTERVAL '1 day',
                   INTERVAL '1 day'
               ) AS day
           ),
           per_day AS (
               SELECT s.day,
                      COALESCE(SUM(t.points), 0) AS minted
                 FROM span s
                 LEFT JOIN agent_inventory_transactions t
                        ON t.points > 0 AND t.type = ANY(%s)
                       AND t.created_at >= s.day
                       AND t.created_at < s.day + INTERVAL '1 day'
                GROUP BY s.day
           )
           SELECT COALESCE(
               percentile_cont(0.5) WITHIN GROUP (ORDER BY minted), 0
           ) AS median FROM per_day""",
        (str(days), list(MINT_TX_TYPES)),
    )
    row = cursor.fetchone()
    return int(float((row["median"] if isinstance(row, dict) else row[0]) or 0))


# ============================================================
# 告警(独立连接 · 调用方回滚后仍留痕)
# ============================================================

def emit_guard_alert(
    fingerprint: str, *, severity: str, title: str, detail: str,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """fail-soft:告警管道挂了绝不能把资金主链带崩,但必须落 error 日志。"""
    try:
        from db import ai_ops_db

        ai_ops_db.upsert_alert(
            ALERT_RULE_KEY, severity=severity, title=title, detail=detail,
            fingerprint=fingerprint, payload=payload or {},
        )
    except Exception:  # noqa: BLE001
        logger.exception("[MINT-GUARD] 告警发送失败(不阻断)· fp=%s · %s", fingerprint, title)


ALERT_FP_DISTRIBUTION = "mint_distribution"


def mint_distribution_status(cursor, *, lookback_days: int = 30) -> Dict[str, Any]:
    """分布级健康检查(工单 §6 判别锁 9)。

    按需铸造与「平台跳」是 1:1 的:每个已结算、mint_points>0 的 platform_root 跳
    必须恰好对应一笔带订单号的 manufacturer_origin_in。两种**单边形态**都是病:

      A 长期零铸造 —— 有已结算的铸造跳,却一笔铸造流水都没有
        (真实故障长这样:铸造那步被注释掉/被异常吞掉,而订单照样"成功")
      B 铸造多于订单 —— 铸造笔数超过铸造跳数(重复铸造/孤儿铸造漏网)

    🔴 这个函数是**真判据**,不是恒真断言:它比对两张独立表的行数。
       在数据层造出「有订单但零铸造」的样本,它必须转红。

    带 related_order_id 的 manufacturer_origin_in 才算按需铸造;
    admin 显式发行(issue_manufacturer_lot)不带订单号,不参与本比对。
    """
    lookback = max(1, int(lookback_days))
    # 🔴 先探表再查:直接查不存在的表会让**调用方的整个事务**进入 aborted 态,
    #    对账那一行 INSERT 会被连带回滚 —— 表面"程序没报错",实际一行没落。
    #    (同形态事故:try/except 包 SQL 而无 SAVEPOINT，把整个事务打废。)
    cursor.execute("SELECT to_regclass('public.dealer_resale_fulfillment_hops') AS t")
    row = cursor.fetchone()
    table = (row["t"] if isinstance(row, dict) else row[0]) if row else None
    if not table:
        return {
            "lookback_days": lookback,
            "applicable": False,
            "expected_mints": 0,
            "actual_mints": 0,
            "healthy": True,
            "anomaly_form": None,
        }
    cursor.execute(
        """SELECT COALESCE(COUNT(*), 0) AS hops
             FROM dealer_resale_fulfillment_hops
            WHERE state = 'settled' AND mint_points > 0
              AND COALESCE(settled_at, created_at) >= NOW() - (%s || ' days')::interval""",
        (str(lookback),),
    )
    row = cursor.fetchone()
    expected = int((row["hops"] if isinstance(row, dict) else row[0]) or 0)
    cursor.execute(
        """SELECT COALESCE(COUNT(*), 0) AS mints
             FROM agent_inventory_transactions
            WHERE type = 'manufacturer_origin_in' AND related_order_id IS NOT NULL
              AND created_at >= LOCALTIMESTAMP - (%s || ' days')::interval""",
        (str(lookback),),
    )
    row = cursor.fetchone()
    actual = int((row["mints"] if isinstance(row, dict) else row[0]) or 0)
    if actual == expected:
        form = None
    elif actual < expected:
        form = "silent_zero_mint"
    else:
        form = "mint_exceeds_orders"
    status = {
        "lookback_days": lookback,
        "applicable": True,
        "expected_mints": expected,
        "actual_mints": actual,
        "healthy": form is None,
        "anomaly_form": form,
    }
    if form:
        emit_guard_alert(
            ALERT_FP_DISTRIBUTION,
            severity="critical",
            title=f"铸造分布异常 · {form} · 铸造跳 {expected} vs 铸造流水 {actual}",
            detail=(
                f"lookback_days={lookback} expected_mints={expected} actual_mints={actual} "
                f"form={form}"
            ),
            payload=status,
        )
    return status


# ============================================================
# 主入口 · 两条铸造路径都必须先过这里
# ============================================================

def assert_mint_allowed(
    cursor,
    *,
    source: str,
    agent_user_id: int,
    pool: str,
    points: int,
    related_order_id: Optional[str],
    plan_declared_points: Optional[int] = None,
    config: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """四道护栏 · 通过返回留痕 dict,不通过 raise MintingGuardError。

    plan_declared_points:履约计划(dealer_resale_fulfillment_hops.points)独立落库的
    声明量;平台跳传它,要求**精确相等**。其余路径为 None,只走订单声明量上界。
    """
    cfg = config if config is not None else load_guard_config(cursor)
    points = int(points)
    agent_user_id = int(agent_user_id)
    pool = str(pool)

    if pool not in (POOL_PAID, POOL_BONUS):
        raise MintingGuardError("MINT_POOL_INVALID", f"铸造池非法:{pool}")
    if points <= 0:
        raise MintingGuardError("MINT_POINTS_INVALID", "铸造算力必须为正")

    # ---------- 护栏 3 · 订单号强制(最硬 · 拒绝 + 告警) ----------
    order_id = str(related_order_id or "").strip()
    if not order_id or not _order_exists(cursor, order_id):
        emit_guard_alert(
            ALERT_FP_ORPHAN,
            severity="critical",
            title="孤儿铸造被拒绝 · 无有效订单号",
            detail=(
                f"source={source} agent={agent_user_id} pool={pool} points={points} "
                f"related_order_id={order_id or '<empty>'}"
            ),
            payload={
                "source": source, "agent_user_id": agent_user_id, "pool": pool,
                "points": points, "related_order_id": order_id or None,
            },
        )
        raise MintingGuardError(
            "MINT_ORPHAN_REJECTED",
            "铸造必须绑定真实订单 · 无订单号的孤儿铸造已拒绝",
            source=source, agent_user_id=agent_user_id, pool=pool, points=points,
        )

    # ---------- 护栏 1 · 一致性锁(主防线) ----------
    if plan_declared_points is not None and int(plan_declared_points) != points:
        raise MintingGuardError(
            "MINT_AMOUNT_MISMATCH",
            f"铸造量与履约计划声明量不一致:铸造 {points} · 声明 {int(plan_declared_points)}",
            source=source, order_id=order_id, pool=pool,
        )
    capacity = _declared_capacity(
        cursor, order_id=order_id, pool=pool, agent_user_id=agent_user_id,
    )
    minted_before = _minted_so_far(
        cursor, order_id=order_id, pool=pool, agent_user_id=agent_user_id,
    )
    if capacity <= 0 or points > capacity:
        raise MintingGuardError(
            "MINT_AMOUNT_MISMATCH",
            f"铸造量超出订单声明量:本次 {points} · 该订单该池声明上限 {capacity}",
            source=source, order_id=order_id, pool=pool,
            capacity=capacity, minted_before=minted_before,
        )
    overminted = minted_before + points > capacity
    if overminted:
        # 单笔合法但同订单累计越线 → 只可能是重复铸造(裁决重结算是合法重复,
        # 慢性泄漏也长这样)。不拦,但必须响。
        emit_guard_alert(
            ALERT_FP_OVERMINT,
            severity="warn",
            title=f"同订单累计铸造越过声明量 · order={order_id} pool={pool}",
            detail=(
                f"source={source} agent={agent_user_id} points={points} "
                f"minted_before={minted_before} capacity={capacity}"
            ),
            payload={
                "source": source, "agent_user_id": agent_user_id, "pool": pool,
                "points": points, "related_order_id": order_id,
                "minted_before": minted_before, "declared_capacity": capacity,
            },
        )

    # ---------- 护栏 2 · 单笔上限 ----------
    max_single = int(cfg["max_single_mint_points"])
    if points > max_single:
        raise MintingGuardError(
            "MINT_SINGLE_CAP_EXCEEDED",
            f"单笔铸造 {points} 超过上限 {max_single}",
            source=source, order_id=order_id, pool=pool, max_single=max_single,
        )

    # ---------- 护栏 4 · 日累计**相对**告警(不拦) ----------
    # 🔴 相对而非绝对:业务量会长,绝对阈值必然要么误报要么漏报。
    #    绝对 5,000 万 = 正常日均的一千多倍 → 每天泄漏 1,000 万也不会响(漏报);
    #    绝对 100 万 → 一笔正常大单第一天就打穿(误报)。
    #    中位数 × N 随业务自适应,泄漏在任何量级都抓得住。
    #    地板只用来压住冷启动噪音:基线为 0 时不至于任何一笔都报。
    daily_after = daily_minted_points(cursor) + points
    baseline_days = int(cfg["daily_baseline_days"])
    baseline = daily_mint_baseline_points(cursor, baseline_days=baseline_days)
    multiple = int(cfg["daily_relative_multiple"])
    floor = int(cfg["daily_alert_floor_points"])
    daily_threshold = max(floor, baseline * multiple)
    daily_alerted = daily_after >= floor and daily_after > baseline * multiple
    if daily_alerted:
        emit_guard_alert(
            ALERT_FP_DAILY,
            severity="warn",
            title=(
                f"当日铸造量越阈 · {daily_after} > 基线中位数 {baseline} × {multiple}"
                f"(地板 {floor})"
            ),
            detail=(
                f"source={source} agent={agent_user_id} pool={pool} points={points} "
                f"order={order_id} daily_after={daily_after} baseline_median={baseline} "
                f"multiple={multiple} floor={floor} baseline_days={baseline_days}"
            ),
            payload={
                "source": source, "agent_user_id": agent_user_id, "pool": pool,
                "points": points, "related_order_id": order_id,
                "daily_after": daily_after, "baseline_median": baseline,
                "multiple": multiple, "floor": floor, "baseline_days": baseline_days,
            },
        )

    return {
        "source": source,
        "agent_user_id": agent_user_id,
        "pool": pool,
        "points": points,
        "related_order_id": order_id,
        "declared_capacity": capacity,
        "minted_before": minted_before,
        "overminted": overminted,
        "daily_after": daily_after,
        "daily_baseline_median": baseline,
        "daily_threshold": daily_threshold,
        "daily_alerted": daily_alerted,
        "max_single_mint_points": max_single,
    }
