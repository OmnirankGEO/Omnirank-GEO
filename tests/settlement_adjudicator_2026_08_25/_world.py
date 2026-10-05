"""审查员判据的真世界构造 —— 站在 P0-3c 的 `legacy_world` 上,不另造一套。

`legacy_world` 已经产出:真 `point_freezes` 冻结行 + 真 `diagnosis_records` 产物行 + paid run。
本模块只做三件它没做的:把 run 停到 `settlement_manual`、按需**抹掉产物证据**、写系数。
"""
from __future__ import annotations

import json

import psycopg2
from psycopg2.extras import RealDictCursor

from tests.p03c_org_guards_2026_08_25._world import legacy_world

from services.settlement_adjudicator import (
    SETTING_ENABLED,
    SETTING_MAX_FROZEN_POINTS,
    SETTING_SAME_CAUSE_LIMIT,
)


def conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    c.autocommit = True
    return c


def set_coefficients(dsn, *, enabled=True, max_frozen_points=10_000, same_cause_limit=3):
    """写系数。**判据里出现的数字是判据自己的夹具值,不是产品默认值** ——
    产品侧一个数字都没有(读不到就 fail-closed)。"""
    c = conn(dsn)
    cur = c.cursor()
    for k, v in ((SETTING_ENABLED, "true" if enabled else "false"),
                 (SETTING_MAX_FROZEN_POINTS, str(max_frozen_points)),
                 (SETTING_SAME_CAUSE_LIMIT, str(same_cause_limit))):
        cur.execute(
            "INSERT INTO system_settings (key, value) VALUES (%s,%s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (k, v))
    c.close()


def clear_coefficients(dsn):
    c = conn(dsn)
    c.cursor().execute(
        "DELETE FROM system_settings WHERE key IN (%s,%s,%s)",
        (SETTING_ENABLED, SETTING_MAX_FROZEN_POINTS, SETTING_SAME_CAUSE_LIMIT))
    c.close()


def manual_world(dsn, *, frozen=650, with_product=True,
                 failure_cause="reserved_split_order_unknown", **kw):
    """造一单**真的停在 settlement_manual** 的 run(真冻结行仍是 frozen)。

    `with_product=False` 时抹掉产物证据(total_score 置空)——
    这是"零交付 ⇒ 全额释放"那一臂的真世界,不是 mock。
    """
    w = legacy_world(dsn, frozen=frozen, **kw)
    c = conn(dsn)
    cur = c.cursor()
    if not with_product:
        cur.execute("UPDATE diagnosis_records SET total_score=NULL WHERE run_token=%s",
                    (w["run_token"],))
    cur.execute(
        "UPDATE diagnosis_runs SET run_status='settlement_manual', "
        "last_settlement_error=%s, status_changed_at=NOW() WHERE run_token=%s",
        ("%s: 造给判据的真世界" % failure_cause, w["run_token"]))
    c.close()
    w["failure_cause"] = failure_cause
    return w


def break_freeze_handle(dsn, run_token):
    """把 FreezeHandle 打断(freeze_id 置空)—— "证据不全"那一臂。"""
    c = conn(dsn)
    c.cursor().execute(
        "UPDATE diagnosis_runs SET freeze_id=NULL, freeze_backend=NULL WHERE run_token=%s",
        (run_token,))
    c.close()


def seed_streak(dsn, *, failure_cause, n):
    """往裁定表里灌 n 条**连续**同因记录 —— "同因≥N" 那一臂。"""
    c = conn(dsn)
    cur = c.cursor()
    for i in range(n):
        cur.execute(
            "INSERT INTO diagnosis_settlement_adjudications "
            "(run_token, phase, decision, escalation_code, failure_cause, frozen_points, "
            " rule_version, evidence_jsonb, outcome) "
            "VALUES (%s,'escalated','escalate','evidence_incomplete',%s,1,'seed','{}'::jsonb,'seed')",
            ("seed_%s_%d" % (failure_cause, i), failure_cause))
    c.close()


def observe(dsn, run_token):
    """裁定后的可观测面:run 状态 / 冻结行 / 049 行 / audit 行 / outbox 行。"""
    c = conn(dsn)
    cur = c.cursor()
    cur.execute("SELECT run_status, freeze_id, freeze_backend FROM diagnosis_runs "
                "WHERE run_token=%s", (run_token,))
    run = cur.fetchone()
    freeze = None
    if run and run["freeze_id"]:
        cur.execute("SELECT id, status, amount_total FROM point_freezes WHERE id=%s",
                    (run["freeze_id"],))
        freeze = cur.fetchone()
    cur.execute(
        "SELECT phase, decision, escalation_code, failure_cause, frozen_points, rule_version, "
        "outcome FROM diagnosis_settlement_adjudications WHERE run_token=%s ORDER BY id",
        (run_token,))
    adj = [dict(r) for r in (cur.fetchall() or [])]
    cur.execute(
        "SELECT operator, action, detail FROM diagnosis_settlement_audit WHERE run_token=%s "
        "ORDER BY id", (run_token,))
    audit = [dict(r) for r in (cur.fetchall() or [])]
    cur.execute(
        "SELECT event_type, terminal_state, recipient_kind, recipient_user_id "
        "FROM notification_outbox WHERE business_id=%s ORDER BY id", (run_token,))
    outbox = [dict(r) for r in (cur.fetchall() or [])]
    c.close()
    return {"run": dict(run) if run else None,
            "freeze": dict(freeze) if freeze else None,
            "adjudications": adj, "audit": audit, "outbox": outbox}


def wallet(dsn, uid):
    c = conn(dsn)
    cur = c.cursor()
    # 列名肉眼核过 prod schema:没有 `points`,是四池 paid/bonus/commission/frozen。
    cur.execute("SELECT paid_points, bonus_points, commission_points, frozen_points "
                "FROM user_wallets WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None


def dumps(v):
    return json.dumps(v, ensure_ascii=False, default=str)
