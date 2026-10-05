"""
services/marketing/executors/grants.py — 涉钱执行器(活动上下线 + 批量发放)· M4b

工程安全规则一字不减(§C.2):
  · 抄 channel_tier.grant_tier_bonus 活范式:台账先落(幂等闸)→ 再入账(不碰 v32 僵尸)。
  · 语义幂等键(grant_key,不带版本号);pool 锁死 bonus(DB CHECK + 代码)。
  · 三级预算上限(单人/单批/单日 NOT NULL 配置)超停发 + 告警;单笔发放硬天花板。
  · dry_run 模式(flag 未点亮时批准只写模拟执行事件,供彩排对账)。
  · 撤销冲销(grants 负向冲销 · campaign 即时下线)。
  · 入账走一个事务:台账 ON CONFLICT DO NOTHING → UPDATE bonus_points → insert_transaction(现有公共函数)。
    绝不改 db/wallet_db.py / middleware/billing.py(红线 diff=0)· 只调 insert_transaction。

真实发放只在 marketing_agent.{enabled,execute,grant} 三闸全开 + 非 kill_switch 时发生;
否则一律 dry_run(台账 dry_run 行 · 零动钱包),M4b 点亮前的彩排账即由此累计。
"""
import json
import logging
from typing import Optional

from db import marketing_db

logger = logging.getLogger("GEO-Marketing-Grants")


def _grant_allowed() -> bool:
    """真实发放三闸:kill off + master + execute + grant 全开。"""
    if marketing_db.is_kill_switch_enabled():
        return False
    return (marketing_db.is_flag_enabled("marketing_agent.enabled", default=False)
            and marketing_db.is_flag_enabled("marketing_agent.execute.enabled", default=False)
            and marketing_db.is_flag_enabled("marketing_agent.grant.enabled", default=False))


def _budget_precheck(user_id: int, points: int) -> Optional[str]:
    """三级上限 + 单笔硬顶。返回超限原因或 None。"""
    hard = marketing_db.get_config_int("marketing.grant.hard_ceiling")
    if points > hard:
        return f"hard_ceiling({points}>{hard})"
    per_user = marketing_db.get_config_int("marketing.grant.cap_per_user")
    if marketing_db.sum_grants_for_user(user_id) + points > per_user:
        return f"per_user_cap({per_user})"
    per_day = marketing_db.get_config_int("marketing.grant.cap_per_day")
    if marketing_db.sum_grants_today() + points > per_day:
        return f"per_day_cap({per_day})"
    return None


def _credit_bonus_txn(*, user_id: int, points: int, grant_key: str, campaign_id: Optional[int],
                      related_order_id: str, grant_type: str, metadata: dict, reason: str) -> dict:
    """真实入账(一个事务:台账幂等闸 → 入 bonus_points → insert_transaction)。
    调 db.wallet_db.insert_transaction(现有公共函数,零改红线文件)。"""
    from db.connection import get_db
    from db.wallet_db import insert_transaction
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO marketing_grants
              (grant_key, campaign_id, user_id, points, pool, grant_type,
               related_order_id, status, dry_run, metadata_jsonb, applied_at)
            VALUES (%s,%s,%s,%s,'bonus',%s,%s,'applied',FALSE,%s::jsonb,NOW())
            ON CONFLICT (grant_key) DO NOTHING
            RETURNING id
            """,
            (grant_key, campaign_id, user_id, int(points), grant_type, related_order_id,
             json.dumps(metadata or {}, ensure_ascii=False)),
        )
        row = cur.fetchone()
        if not row:
            return {"granted": False, "reason": "idempotent_exists"}
        grant_id = row["id"]
        cur.execute(
            "UPDATE user_wallets SET bonus_points = bonus_points + %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE user_id = %s RETURNING bonus_points",
            (int(points), user_id))
        wrow = cur.fetchone()
        if not wrow:
            raise ValueError(f"user_wallets 缺行 user_id={user_id}")  # get_db 回滚整个事务
        insert_transaction(cur, user_id, "bonus", "bonus", int(points), wrow["bonus_points"],
                           description=(reason or "营销赠送算力"), order_id=grant_key, source="marketing")
    return {"granted": True, "grant_id": grant_id, "balance": wrow["bonus_points"]}


def apply_grant(*, user_id: int, points: int, grant_key: str, campaign_id: Optional[int] = None,
                related_order_id: str = "", grant_type: str = "marketing",
                reason: str = "营销赠送算力", metadata: Optional[dict] = None,
                force_dry_run: bool = False) -> dict:
    """发一笔赠送算力(bonus)。三闸未全开 / force → dry_run 台账(零动钱包)。

    返回 {status: granted|dry_run|suppressed|idempotent, ...}。
    """
    if points <= 0:
        return {"status": "suppressed", "reason": "zero_points"}

    dry_run = force_dry_run or (not _grant_allowed())

    # 预算/硬顶预检(dry_run 也跑,彩排账真实反映会不会超停)
    over = _budget_precheck(user_id, points)
    if over:
        marketing_db.add_event(event_type="grant_over_budget", campaign_id=campaign_id, severity="warn",
                               message=f"发放超限 user={user_id} points={points} · {over}",
                               payload={"user_id": user_id, "points": points, "reason": over})
        return {"status": "suppressed", "reason": over, "dry_run": dry_run}

    if dry_run:
        grant, inserted = marketing_db.create_grant_ledger(
            grant_key=f"dryrun:{grant_key}", user_id=user_id, points=points,
            campaign_id=campaign_id, grant_type=grant_type, related_order_id=related_order_id,
            dry_run=True, metadata={**(metadata or {}), "rehearsal": True})
        return {"status": "dry_run", "inserted": inserted, "grant_id": grant.get("id")}

    try:
        res = _credit_bonus_txn(user_id=user_id, points=points, grant_key=grant_key,
                                campaign_id=campaign_id, related_order_id=related_order_id,
                                grant_type=grant_type, metadata=metadata or {}, reason=reason)
    except Exception as e:  # noqa: BLE001
        logger.warning("[grants] 入账失败 user=%s: %s", user_id, e)
        marketing_db.add_event(event_type="grant_failed", campaign_id=campaign_id, severity="error",
                               message=f"发放入账失败 user={user_id}: {e}")
        return {"status": "failed", "error": str(e)[:120]}

    if not res.get("granted"):
        return {"status": "idempotent", "reason": res.get("reason")}
    if campaign_id:
        marketing_db.add_campaign_spent(campaign_id, points)
    marketing_db.add_event(event_type="grant_applied", campaign_id=campaign_id,
                           message=f"发放 {points} 算力 → user={user_id}",
                           payload={"user_id": user_id, "points": points, "grant_id": res["grant_id"]})
    return {"status": "granted", "grant_id": res["grant_id"], "balance": res.get("balance")}


def batch_grant(*, user_ids: list[int], points_each: int, campaign_id: Optional[int],
                key_prefix: str, grant_type: str = "marketing", reason: str = "营销批量赠送算力",
                force_dry_run: bool = False) -> dict:
    """批量发放。单批总额超 per_batch 上限 → 停发 + 告警。"""
    per_batch = marketing_db.get_config_int("marketing.grant.cap_per_batch")
    total = points_each * len(user_ids)
    if total > per_batch:
        marketing_db.add_event(event_type="grant_batch_over", campaign_id=campaign_id, severity="warn",
                               message=f"单批发放 {total} 超单批上限 {per_batch},停发",
                               payload={"total": total, "cap": per_batch})
        return {"ok": False, "error": "per_batch_cap", "total": total, "cap": per_batch}

    counters = {"granted": 0, "dry_run": 0, "suppressed": 0, "idempotent": 0, "failed": 0}
    for uid in user_ids:
        r = apply_grant(user_id=uid, points=points_each, grant_key=f"{key_prefix}:{uid}",
                        campaign_id=campaign_id, grant_type=grant_type, reason=reason,
                        force_dry_run=force_dry_run)
        counters[r["status"]] = counters.get(r["status"], 0) + 1
    return {"ok": True, **counters}


def revoke_grant(grant_id: int) -> dict:
    """撤销冲销:台账负向冲销 + 若已真实入账则回扣 bonus_points(floor 0)。"""
    grant = None
    try:
        from db.connection import get_db
        from db.wallet_db import insert_transaction
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM marketing_grants WHERE id=%s FOR UPDATE", (grant_id,))
            grant = cur.fetchone()
            if not grant or grant["status"] != "applied":
                conn.rollback()
                return {"ok": False, "error": "not_applied"}
            pts = int(grant["points"])
            # 负向冲销台账
            rev_key = f"reverse:{grant['grant_key']}"
            cur.execute(
                """INSERT INTO marketing_grants
                   (grant_key, campaign_id, user_id, points, pool, grant_type, related_order_id,
                    status, dry_run, reversed_of, metadata_jsonb, reversed_at)
                   VALUES (%s,%s,%s,%s,'bonus',%s,%s,'reversed',FALSE,%s,%s::jsonb,NOW())
                   ON CONFLICT (grant_key) DO NOTHING RETURNING id""",
                (rev_key, grant["campaign_id"], grant["user_id"], -pts, grant["grant_type"],
                 grant["related_order_id"], grant_id, json.dumps({"reversal_of": grant_id})))
            cur.execute("UPDATE marketing_grants SET status='reversed', reversed_at=NOW() WHERE id=%s",
                        (grant_id,))
            # 回扣 bonus(不为负)· [返工 R6-10] 流水记"实际扣减值"(用户已花掉部分只能扣到 0,账实一致)
            cur.execute("SELECT bonus_points FROM user_wallets WHERE user_id = %s FOR UPDATE",
                        (grant["user_id"],))
            _before = cur.fetchone()
            before_pts = int(_before["bonus_points"]) if _before else 0
            cur.execute(
                "UPDATE user_wallets SET bonus_points = GREATEST(bonus_points - %s, 0), "
                "updated_at = CURRENT_TIMESTAMP WHERE user_id = %s RETURNING bonus_points",
                (pts, grant["user_id"]))
            wrow = cur.fetchone()
            if wrow:
                actual_debit = before_pts - int(wrow["bonus_points"])  # ≤ pts
                insert_transaction(cur, grant["user_id"], "bonus", "bonus", -actual_debit,
                                   wrow["bonus_points"],
                                   description=f"营销赠送算力冲销(应冲 {pts})",
                                   order_id=rev_key, source="marketing")
        marketing_db.add_event(event_type="grant_reversed", campaign_id=grant["campaign_id"],
                               message=f"冲销发放 grant={grant_id} · -{pts} 算力")
        return {"ok": True, "reversed_points": pts}
    except Exception as e:  # noqa: BLE001
        logger.warning("[grants] revoke 失败 grant=%s: %s", grant_id, e)
        return {"ok": False, "error": str(e)[:120]}


# —— 活动上下线 ——
def activate_campaign(campaign_id: int) -> dict:
    marketing_db.set_campaign_status(campaign_id, "active")
    # [返工 R6-1] 上线时 starts_at 为空则自动置 NOW:补发时间窗(只补上线后漏发)依赖它必有值
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE marketing_campaigns SET starts_at = NOW() "
                        "WHERE id = %s AND starts_at IS NULL", (campaign_id,))
    except Exception as e:  # noqa: BLE001
        logger.warning("[grants] activate 置 starts_at 失败 campaign=%s: %s", campaign_id, e)
    marketing_db.add_event(event_type="campaign_activated", campaign_id=campaign_id, message="活动上线")
    return {"ok": True, "status": "active"}


def end_campaign(campaign_id: int) -> dict:
    """即时下线(回滚活动)。"""
    marketing_db.set_campaign_status(campaign_id, "ended")
    marketing_db.add_event(event_type="campaign_ended", campaign_id=campaign_id, message="活动即时下线")
    return {"ok": True, "status": "ended"}


def seed_builtin_campaigns() -> int:
    """幂等 seed 两个内置活动模板(§C.3):首充双倍(常驻)+ 累充成长礼(里程碑)。
    默认 status='draft' + grant 闸关 → 不真实发放;运营激活(status='active')+ M4b 点亮后才生效
    (发放钩子已真校验 status/时间窗,见 hooks._campaign_live · 返工 R1)。
    [返工 R2] seed_mode=True:仅首次插入,重启不覆盖运营的 activate/预算/参数。"""
    n = 0
    try:
        marketing_db.upsert_campaign(
            campaign_code="firstcharge_double", campaign_type="first_charge_double",
            name="首充双倍(常驻)", budget_cap_points=1000000,
            params={"bonus_rate": 1.0, "max_grant_points": 65000},
            is_resident=True, status="draft", seed_mode=True)
        n += 1
        marketing_db.upsert_campaign(
            campaign_code="milestone_growth", campaign_type="milestone",
            name="累充成长礼", budget_cap_points=1000000,
            params={"tiers": [
                {"threshold_cents": 50000,  "bonus_points": 6500},
                {"threshold_cents": 200000, "bonus_points": 39000},
            ]}, is_resident=True, status="draft", seed_mode=True)
        n += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("[grants] seed 内置活动失败: %s", e)
    return n
