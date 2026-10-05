"""
services/marketing/executors/hooks.py — complete_recharge 成功后的营销钩子(post-commit · fail-open)

安装方式(§C.2 / §3 红线):在 complete_recharge 的**调用方外层包装**(api/wallet_api.py 的
5 个回调点),commit 之后调 on_recharge_completed(result)。绝不改 complete_recharge 函数体
(它在 db/wallet_db.py · 红线 diff=0)。本钩子**永不抛异常**(fail-open):营销 bug 绝不能回滚真实支付。

内置活动:
  · 首充双倍(firstcharge_double · 常驻无相对时间窗):首笔付费充值 → 赠 base_points × bonus_rate(bonus)。
  · 累充成长礼(milestone_growth):累计充值跨档 → 赠对应算力。
真实发放仅三闸全开时发生;否则 apply_grant 内部自动 dry_run(彩排账累计),M4b 点亮前零动钱包。
幂等:grant_key 语义键(首充=每人一次 · 里程碑=每档一次)。
"""
import logging
from datetime import datetime
from typing import Optional

import pytz

from db import marketing_db
from services.marketing.executors import grants

logger = logging.getLogger("GEO-Marketing-Hooks")

BEIJING_TZ = pytz.timezone("Asia/Shanghai")  # 与 reach.py 时区口径一致


def _campaign_live(camp: dict) -> bool:
    """活动判活:status=='active' 且在时间窗内(starts_at/ends_at 为 NULL 视为不限)。
    [返工 R1] 修复:钩子原先无视 status——draft/paused/ended 活动照发,"活动下线"回滚开关失效。
    优先用 DB 侧算好的 is_live(get_campaign_by_code · 单一时钟源:时间戳由 DB NOW() 写,
    必须用 DB NOW() 比——Python 本机钟与容器钟可能相差数小时);缺列时回退 Python 判(保守)。
    """
    if "is_live" in camp:
        return bool(camp["is_live"])
    if (camp.get("status") or "") != "active":
        return False
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    starts, ends = camp.get("starts_at"), camp.get("ends_at")
    if starts and now < starts:
        return False
    if ends and now > ends:
        return False
    return True


def _first_paid_count(user_id: int) -> int:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*)::int AS c FROM recharge_orders "
                    "WHERE user_id=%s AND payment_status='paid'", (user_id,))
        return int(cur.fetchone()["c"])
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        return 99  # 保守:查不到就当非首充,不误发
    finally:
        conn.close()


def _cumulative_paid_cents(user_id: int) -> int:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(amount_cents),0)::bigint AS s FROM recharge_orders "
                    "WHERE user_id=%s AND payment_status='paid'", (user_id,))
        return int(cur.fetchone()["s"])
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()


def _first_charge_double(user_id: int, order_id: str, base_points: int) -> None:
    camp = marketing_db.get_campaign_by_code("firstcharge_double")
    if not camp:
        return  # 无活动 = 无首充双倍
    if not _campaign_live(camp):
        return  # [返工 R1] 只有 active 且在时间窗内的活动才发放
    if _first_paid_count(user_id) != 1:
        return  # 只对首笔付费充值
    params = camp.get("params_jsonb") or {}
    rate = float(params.get("bonus_rate", 1.0) or 1.0)  # 1.0 = 双倍
    grant_points = int(round((base_points or 0) * rate))
    # [返工 R1] 单笔封顶(默认 65000 = ¥500 档;超大首充按封顶发,不再触硬顶被 suppressed)
    max_grant = int(params.get("max_grant_points", 65000) or 65000)
    grant_points = min(grant_points, max_grant)
    if grant_points <= 0:
        return
    # 活动预算水位
    cap = int(camp.get("budget_cap_points") or 0)
    spent = int(camp.get("spent_points") or 0)
    if cap and spent + grant_points > cap:
        marketing_db.add_event(event_type="campaign_budget_exhausted", campaign_id=camp["id"],
                               severity="warn", message="首充双倍预算耗尽,停发")
        return
    grants.apply_grant(user_id=user_id, points=grant_points,
                       grant_key=f"mktg_firstcharge:{user_id}", campaign_id=camp["id"],
                       related_order_id=order_id, grant_type="first_charge_double",
                       reason="首充双倍赠送算力")


def _milestone_check(user_id: int, order_id: str) -> None:
    camp = marketing_db.get_campaign_by_code("milestone_growth")
    if not camp:
        return
    if not _campaign_live(camp):
        return  # [返工 R1] 只有 active 且在时间窗内的活动才发放
    params = camp.get("params_jsonb") or {}
    tiers = params.get("tiers") or []  # [{"threshold_cents": N, "bonus_points": M}]
    if not tiers:
        return
    cum = _cumulative_paid_cents(user_id)
    for t in tiers:
        thr = int(t.get("threshold_cents", 0) or 0)
        pts = int(t.get("bonus_points", 0) or 0)
        if thr > 0 and pts > 0 and cum >= thr:
            grants.apply_grant(user_id=user_id, points=pts,
                               grant_key=f"mktg_milestone:{user_id}:{thr}", campaign_id=camp["id"],
                               related_order_id=order_id, grant_type="milestone",
                               reason=f"累充成长礼(满 {thr/100:.0f} 元)")


def on_recharge_completed(result: Optional[dict]) -> None:
    """complete_recharge 返回后调(post-commit)。永不抛异常。"""
    try:
        if not result or not isinstance(result, dict):
            return
        # [返工 R1] 服务商进货预付(钱进库存不进钱包)不触发个人首充/里程碑
        if (result.get("order_type") or "") == "agent_inventory_purchase":
            return
        if marketing_db.is_kill_switch_enabled():
            return
        user_id = result.get("user_id")
        order_id = result.get("id") or result.get("order_id") or ""
        if user_id is None:
            return
        base_points = int(result.get("base_points") or 0)
        _first_charge_double(int(user_id), str(order_id), base_points)
        _milestone_check(int(user_id), str(order_id))
    except Exception as e:  # noqa: BLE001 · fail-open:营销钩子绝不影响支付
        logger.warning("[hooks] on_recharge_completed 异常(已吞·不影响支付): %s", e)
