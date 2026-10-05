"""T1 判别锁 —— 客户门户 token 交付成功后续期(工单 §1.5)。

7 条锁,全部行为级(真调 db.monitoring_db 的函数,真读真库):
  1. 交付成功 → 同一 token 值**不变**、expires_at 被延长
  2. 客户用**原 URL** 仍能打开(token 值没换 · verify_client_token 认得)
  3. 服务期已结束 → 触发交付成功事件 → **不续期**
  4. new_expires_at <= old → **不写库**(幂等,重复事件零副作用)
  5. 已过期但服务期内 → 续期后**可复活**、token 值不变
  6. 续期动作落审计,action 与 generate 可区分
  7. 续期抛异常 → **交付主链仍成功**
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from db import monitoring_db


def _token_rows(db, quote_id):
    db.execute(
        "SELECT id, token, is_active, expires_at FROM client_access_tokens "
        "WHERE quote_id=%s ORDER BY id",
        (quote_id,),
    )
    return db.fetchall()


# ---------------------------------------------------------------- 锁 1 + 锁 2
def test_lock1_renew_extends_expiry_without_rotating_token(db, seed):
    """交付成功 → expires_at 变长,token 值一个字符都不许变,也不许多签一行。"""
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    before = _token_rows(db, ctx["quote_id"])
    assert len(before) == 1

    result = monitoring_db.renew_client_token(ctx["quote_id"], trigger="test")

    after = _token_rows(db, ctx["quote_id"])
    assert result["status"] == "renewed", result
    # 🔴 锁 1:没有轮换 —— 行数不变、token 值不变、is_active 不变
    assert len(after) == 1, "续期不许新签 token 行(那是轮换)"
    assert after[0]["token"] == before[0]["token"], "续期不许换 token 值"
    assert after[0]["is_active"] == before[0]["is_active"]
    # 到期日确实被延长了
    assert after[0]["expires_at"] > before[0]["expires_at"]


def test_lock2_customer_original_url_still_works(db, seed):
    """锁 2:客户手上那条**原 URL** 续期后仍能打开(token 值没换)。"""
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    monitoring_db.renew_client_token(ctx["quote_id"], trigger="test")
    assert monitoring_db.verify_client_token(ctx["token"]) == ctx["quote_id"]


# ---------------------------------------------------------------------- 锁 3
def test_lock3_service_ended_does_not_renew(db, seed):
    """锁 3:服务期已结束 → 触发交付成功 → 到期日**一天都不动**。"""
    ctx = seed(service_days=30, started_days_ago=90, token_expires_in=1)
    before = _token_rows(db, ctx["quote_id"])[0]["expires_at"]

    result = monitoring_db.renew_client_token(ctx["quote_id"], trigger="test")

    assert result["status"] == "skipped"
    assert result["reason"] == "service_ended", result
    assert _token_rows(db, ctx["quote_id"])[0]["expires_at"] == before


# ---------------------------------------------------------------------- 锁 4
def test_lock4_idempotent_when_new_not_greater_than_old(db, seed):
    """锁 4:new <= old 直接跳过,一行不写(重复事件零副作用)。"""
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    first = monitoring_db.renew_client_token(ctx["quote_id"], trigger="test")
    assert first["status"] == "renewed"
    db.execute("SELECT COUNT(*) AS c FROM audit_logs WHERE action='portal.token.renew'")
    audits_after_first = db.fetchone()["c"]
    snapshot = _token_rows(db, ctx["quote_id"])[0]["expires_at"]

    second = monitoring_db.renew_client_token(ctx["quote_id"], trigger="test")

    assert second["status"] == "noop", second
    assert second["renewed"] == 0
    assert _token_rows(db, ctx["quote_id"])[0]["expires_at"] == snapshot
    db.execute("SELECT COUNT(*) AS c FROM audit_logs WHERE action='portal.token.renew'")
    assert db.fetchone()["c"] == audits_after_first, "幂等跳过不许再写一条审计"


# ---------------------------------------------------------------------- 锁 5
def test_lock5_expired_token_inside_service_window_is_revived(db, seed):
    """锁 5:已过期但仍在服务期内 → 复活,且 token 值不变。"""
    ctx = seed(service_days=180, started_days_ago=60, token_expires_in=-5)
    before = _token_rows(db, ctx["quote_id"])[0]
    assert before["expires_at"] < date.today(), "夹具前提:这条 token 现在就是过期的"
    assert monitoring_db.verify_client_token(ctx["token"]) is None

    result = monitoring_db.renew_client_token(ctx["quote_id"], trigger="test")

    after = _token_rows(db, ctx["quote_id"])[0]
    assert result["status"] == "renewed"
    assert after["token"] == before["token"], "复活不许换 token 值"
    assert after["expires_at"] >= date.today()
    assert monitoring_db.verify_client_token(ctx["token"]) == ctx["quote_id"]


# ---------------------------------------------------------------------- 锁 6
def test_lock6_renew_audit_is_distinguishable_from_generate(db, seed):
    """锁 6:续期落审计,且 action 与轮换的 portal.token.generate 分得开。"""
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    monitoring_db.renew_client_token(
        ctx["quote_id"], trigger="monitoring_round_completed", request_id="rid-1",
    )

    db.execute(
        "SELECT action, entity_id, after_snapshot, reason FROM audit_logs "
        "WHERE entity_type='customer_portal_credential' ORDER BY id"
    )
    rows = db.fetchall()
    assert len(rows) == 1, rows
    assert rows[0]["action"] == "portal.token.renew"
    assert rows[0]["action"] != "portal.token.generate"
    assert rows[0]["entity_id"] == ctx["quote_id"]
    assert "monitoring_round_completed" in rows[0]["after_snapshot"]


# ---------------------------------------------------------------------- 锁 7
def test_lock7_renew_failure_never_breaks_delivery(db, seed, monkeypatch):
    """锁 7:续期抛异常 → 交付主链(update_task_status)仍然成功。

    真调 update_task_status,把底层 renew_client_token 打成必抛,断言:
      · 函数仍返回成功;
      · monitoring_tasks 终态仍然写成了 completed(交付事实没被回滚)。
    """
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    db.execute(
        "INSERT INTO monitoring_tasks (quote_id, client_id, brand_id, status, total_tests, completed_tests, trigger_type) "
        "VALUES (%s, %s, %s, 'running', 4, 4, 'manual') RETURNING id",
        (ctx["quote_id"], str(ctx["quote_id"]), ctx["brand_id"]),
    )
    task_id = db.fetchone()["id"]

    def _boom(*args, **kwargs):
        raise RuntimeError("renew exploded")

    monkeypatch.setattr(monitoring_db, "renew_client_token", _boom)

    ok = monitoring_db.update_task_status(task_id, "completed", 4, {"detection_rate": 50})

    assert ok is True, "续期失败不得让交付主链失败"
    db.execute("SELECT status FROM monitoring_tasks WHERE id=%s", (task_id,))
    assert db.fetchone()["status"] == "completed"


# ------------------------------------------------- 接线:两个交付成功事件真触发
def test_wire_monitoring_round_completed_triggers_renew(db, seed):
    """监测轮次完成(真调 update_task_status)→ 门户 token 真的被续期。"""
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    before = _token_rows(db, ctx["quote_id"])[0]["expires_at"]
    db.execute(
        "INSERT INTO monitoring_tasks (quote_id, client_id, brand_id, status, total_tests, completed_tests, trigger_type) "
        "VALUES (%s, %s, %s, 'running', 4, 4, 'manual') RETURNING id",
        (ctx["quote_id"], str(ctx["quote_id"]), ctx["brand_id"]),
    )
    task_id = db.fetchone()["id"]

    monitoring_db.update_task_status(task_id, "completed", 4, {"detection_rate": 50})

    assert _token_rows(db, ctx["quote_id"])[0]["expires_at"] > before
    db.execute(
        "SELECT after_snapshot FROM audit_logs WHERE action='portal.token.renew' ORDER BY id DESC LIMIT 1"
    )
    assert "monitoring_round_completed" in db.fetchone()["after_snapshot"]


def test_wire_report_generated_triggers_renew(db, seed):
    """效果报告生成成功(真调 save_report)→ 门户 token 真的被续期。"""
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    before = _token_rows(db, ctx["quote_id"])[0]["expires_at"]

    monitoring_db.save_report(
        brand_id=ctx["brand_id"], report_type="weekly",
        period_start="2026-07-01", period_end="2026-07-07",
        summary_data={"x": 1}, content="报告正文", status="draft",
    )

    assert _token_rows(db, ctx["quote_id"])[0]["expires_at"] > before
    db.execute(
        "SELECT after_snapshot FROM audit_logs WHERE action='portal.token.renew' ORDER BY id DESC LIMIT 1"
    )
    assert "report_generated" in db.fetchone()["after_snapshot"]


# ------------------------------------------------------------------ 边界护栏
def test_renew_never_exceeds_service_end_plus_grace(db, seed):
    """续期上限:绝不超过「服务期结束日 + 宽限期」—— 不能变成无限续期。"""
    ctx = seed(service_days=180, started_days_ago=178, token_expires_in=1)
    grace, _max = monitoring_db._portal_renew_config()
    service_end = ctx["service_start"] + timedelta(days=ctx["service_days"])

    monitoring_db.renew_client_token(ctx["quote_id"], trigger="test")

    got = _token_rows(db, ctx["quote_id"])[0]["expires_at"]
    assert got <= service_end + timedelta(days=grace)


def test_renew_never_shortens_a_never_expiring_token(db, seed):
    """expires_at IS NULL(永不过期)不许被写成日期 —— 那是缩短不是延长。"""
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    db.execute("UPDATE client_access_tokens SET expires_at = NULL WHERE quote_id=%s", (ctx["quote_id"],))

    result = monitoring_db.renew_client_token(ctx["quote_id"], trigger="test")

    assert result["status"] == "noop"
    assert _token_rows(db, ctx["quote_id"])[0]["expires_at"] is None


def test_renew_does_not_touch_rotated_out_tokens(db, seed):
    """轮换掉的旧 token(is_active=0)不许被续期复活。"""
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    db.execute(
        "INSERT INTO client_access_tokens (quote_id, brand_name, token, is_active, expires_at) "
        "VALUES (%s, 'x', 'DEADTOKEN01', 0, %s)",
        (ctx["quote_id"], date.today() - timedelta(days=10)),
    )

    monitoring_db.renew_client_token(ctx["quote_id"], trigger="test")

    db.execute("SELECT expires_at FROM client_access_tokens WHERE token='DEADTOKEN01'")
    assert db.fetchone()["expires_at"] == date.today() - timedelta(days=10)


def test_delivery_quote_id_resolution_is_fail_closed(db, seed):
    """交付事件里的 client_id 万一是 brand_id 回落值,不许串到别人的 quote。

    场景复刻真实风险:`_resolve_id` 在某品牌**没有任何 quote** 时会把 client_id
    回落成 `str(brand_id)`;brand_id 与 quote_id 是两套自增序列,数值必然会撞 ——
    naive 实现会拿这个数字当 quote_id 去续别人家的 token。
    """
    ctx = seed(service_days=180, started_days_ago=29, token_expires_in=1)
    # 品牌 B 一个 quote 都没有
    db.execute("INSERT INTO brands (name, owner_user_id) VALUES ('无报价品牌', 999) RETURNING id")
    lonely_brand = db.fetchone()["id"]
    # 而恰好存在一个 id == lonely_brand 的 quote,归属**品牌 A**
    db.execute(
        "INSERT INTO quotes (id, brand_id, status, paid_at, service_start_date, service_days) "
        "VALUES (%s, %s, 'paid', NOW(), CURRENT_DATE, 180)",
        (lonely_brand, ctx["brand_id"]),
    )

    resolved = monitoring_db._resolve_delivery_quote_id(
        client_id=str(lonely_brand), brand_id=lonely_brand,
    )

    assert resolved is None, (
        f"品牌 {lonely_brand} 没有任何 quote,不许把别人家的 quote {lonely_brand} 解析出来"
    )
