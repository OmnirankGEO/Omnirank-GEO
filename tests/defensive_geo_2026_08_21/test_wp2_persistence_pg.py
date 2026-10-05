"""WP2 持久化:040/041 两张表 + 应用层守卫(真 PG)。

本文件逐条落 **Review-CTO 2026-08-21 裁定的附带条件**:

  ① additive-only                      → test_migrations_are_additive_only
  ② 迁移体零 DML                        → test_migrations_contain_zero_dml(尺子先自证)
  ③ frozen_payload 不可变强制 +「UPDATE 必被拒」正样本
                                        → test_*_update_is_rejected 系列
  ④ lifecycle 三值 CHECK                → test_lifecycle_check_*
  ⑤ 归属每请求现做                       → test_cross_tenant_* 系列
  ⑦ 禁第二套 settlement enum            → test_no_second_settlement_enum

(⑥ G-4「多返回一键→受控失败非裸 500」在 test_wp2_endpoints_pg.py,端点层)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from scripts.defgeo_census.migration_dml_census import (
    DEFGEO_MIGRATIONS,
    KNOWN_DML_MIGRATION,
    scan as scan_dml,
)
from services.defensive_geo import plan_store
from services.defensive_geo.question_plan import (
    PlannedQuestion,
    canonical_hash,
    counts,
    new_plan_id,
    question_identity_key,
    request_content_hash,
)

pytestmark = pytest.mark.integration

ROOT_TENANT = 5101
OTHER_TENANT = 5102


def _q(plan_id: str, seq: int, *, side="defensive", text=None, rev=1, ordinal=None):
    return PlannedQuestion(
        question_identity_key=question_identity_key(plan_id, seq),
        question_revision=rev,
        global_ordinal=ordinal or seq,
        text=text or f"第{seq}题:这个牌子靠谱吗",
        mode_side=side,
        family_key="identity_check",
        brand_exposure="named",
        origin="system",
        classifier_version="cls-v1",
    )


def _future(minutes=30):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _seed_plan(conn, *, tenant=ROOT_TENANT, brand_id=901, client_request_id=None,
               content_hash=None):
    """种一份 revision 1。

    ⚠️ ``content_hash`` 与 ``canonical_hash`` 是**两件不同的东西**,幂等挂前者:
       canonical_hash 含由 plan_id 派生的身份键,而 plan_id 每次新生成,
       拿它做幂等键会永不命中(真 HTTP 判据抓出来的缺陷,见 040 注释)。
       所以想验「幂等命中」的用例必须显式传同一个 ``content_hash``。
    """
    plan_id = new_plan_id()
    qs = (_q(plan_id, 1), _q(plan_id, 2))
    d, o, t = counts(qs)
    h = canonical_hash(brand_id=brand_id, profile_revision_id="prof-1", mode="defensive",
                       question_set_version="qs-v1", questions=qs)
    ch = content_hash or request_content_hash(
        brand_id=brand_id, profile_revision_id="prof-1", mode="defensive",
        question_set_version="qs-v1",
        questions=tuple((q.text, q.mode_side, q.family_key, q.brand_exposure) for q in qs),
    )
    with conn.cursor() as cur:
        row = plan_store.insert_plan_revision(
            cur, plan_id=plan_id, plan_revision=1, tenant_owner_user_id=tenant,
            brand_id=brand_id, profile_revision_id="prof-1", mode="defensive",
            question_set_version="qs-v1", canonical_hash=h, questions=qs,
            defensive_count=d, offensive_count=o, total_count=t,
            client_request_id=client_request_id or f"creq-{uuid.uuid4().hex[:8]}",
            request_content_hash=ch,
            expires_at=_future(), created_by_user_id=tenant,
        )
    return plan_id, row, qs, h


def _seed_preview(conn, *, tenant=ROOT_TENANT, plan_id=None, idem=None, lifecycle_open=True):
    plan_id = plan_id or new_plan_id()
    pid = plan_store.new_preview_id()
    with conn.cursor() as cur:
        row, replayed = plan_store.insert_run_preview(
            cur, preview_id=pid, tenant_owner_user_id=tenant, brand_id=901,
            created_by_user_id=tenant, question_plan_id=plan_id, question_plan_revision=1,
            question_plan_hash="a" * 64, profile_revision_id="prof-1",
            campaign_mode="defensive", frozen_payload={"cells": [{"i": 1}]},
            canonical_hash="b" * 64, feature_code="geo_diagnosis",
            pricing_catalog_version="cat-v1", base_points=100, extra_points=20,
            exact_total_points=120, funding_policy="personal_wallet",
            principal_kind="personal", sponsor_policy_ref=None,
            approval_requirement="not_required", planned_cells=8,
            idempotency_key=idem or f"idem-{uuid.uuid4().hex[:8]}",
            canonical_request_hash="c" * 64, expires_at=_future(),
        )
    return pid, row, replayed


# ══════════════════════ 条件① additive-only ═══════════════════════════════
def test_migrations_are_additive_only():
    """🔴 只许 CREATE 新表/索引/函数/触发器,**不许 ALTER 既有表**。

    判法:扫 ALTER TABLE 的目标表名,必须全部落在本包新建的两张表里。
    (040/041 内部确实用 ALTER TABLE 加自己的 CHECK/UNIQUE —— 那是给**自己**加,
     不是改别人;所以判据看的是**目标表**,不是有没有 ALTER 这个词。
     只判词会把合法的自我加固也误报,然后逼人加白名单,白名单迟早放过真货。)
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    own = {"defgeo_question_plans", "defgeo_diagnosis_run_previews"}
    for rel in DEFGEO_MIGRATIONS:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        text_nc = re.sub(r"--[^\n]*", " ", text)
        targets = {m.lower() for m in re.findall(r"ALTER\s+TABLE\s+([a-zA-Z_][\w.]*)", text_nc, re.I)}
        foreign = targets - own
        assert not foreign, f"{rel} 改了本包之外的表:{sorted(foreign)} —— 违反 additive-only"
        # 反向对照:正则必须真的抓得到目标(否则 foreign 恒空 = 恒绿)
        assert targets, f"{rel} 一个 ALTER TABLE 目标都没抓到 —— 抽取面坏了,上面的断言不算数"

    # 也不许 DROP/TRUNCATE 任何东西
    for rel in DEFGEO_MIGRATIONS:
        text_nc = re.sub(r"--[^\n]*", " ", (root / rel).read_text(encoding="utf-8", errors="replace"))
        assert not re.search(r"\bDROP\s+TABLE\b|\bTRUNCATE\b", text_nc, re.I), f"{rel} 含 DROP/TRUNCATE"


# ══════════════════════ 条件② 迁移体零 DML ════════════════════════════════
def test_dml_ruler_has_discriminating_power_first():
    """🔴 先证明尺子会响,再信它的 0。

    第一版尺子(一行 grep)对 040/041 报 0,**对已知含 DML 的 039 也报 0** ——
    反向对照为 0 时,那个 0 什么都不证明(本仓「全阴性先怀疑尺子」)。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    positive = scan_dml(root / KNOWN_DML_MIGRATION)
    assert positive, (
        f"反向对照锚 {KNOWN_DML_MIGRATION} 命中 0 条 DML —— 尺子坏了,"
        "下面那条『零 DML』不算数"
    )


def test_migrations_contain_zero_dml():
    """条件②:prestart 每次部署无条件重放全部迁移,体内 DML 是常驻地雷。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in DEFGEO_MIGRATIONS:
        hits = scan_dml(root / rel)
        assert hits == [], f"{rel} 含顶层 DML:{hits}"


def test_both_migrations_are_registered_in_manifest():
    """漏登记 = 上线后**永远不会跑**(本仓 2026-08-02 实例)。"""
    from db.migration_manifest import MIGRATIONS

    for rel in DEFGEO_MIGRATIONS:
        assert rel in MIGRATIONS, f"{rel} 没进 manifest —— prestart 只按 manifest 跑,不 glob 目录"


# ══════════════════════ 条件③ 不可变强制(配「UPDATE 必被拒」正样本)══════
def test_question_plan_row_is_created(db):
    """反向对照:先证明能写进去 —— 否则下面「改不动」可能只是因为压根没有行。"""
    _, row, _, _ = _seed_plan(db)
    assert row["plan_revision"] == 1
    assert row["total_count"] == 2


def test_question_plan_inplace_update_is_rejected_by_db(db):
    """🔴 条件③ 正样本:直接 UPDATE 冻结列 → **必被 DB 拒**。

    刻意绕过应用层,直接打 SQL —— 这正是应用层守卫挡不住的那条路
    (psql 手改 / 别的包顺手写 / ORM upsert)。
    """
    plan_id, _, _, _ = _seed_plan(db)
    with pytest.raises(psycopg2.errors.RestrictViolation) as exc:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE defgeo_question_plans SET frozen_payload='{}'::jsonb "
                "WHERE plan_id=%s AND plan_revision=1",
                (plan_id,),
            )
    assert "REV-01" in str(exc.value)
    db.rollback()


@pytest.mark.parametrize("column,value", [
    ("canonical_hash", "'" + "f" * 64 + "'"),
    ("total_count", "99"),
    ("mode", "'offensive'"),
    ("expires_at", "NOW() + INTERVAL '10 years'"),
    ("brand_id", "999"),
])
def test_every_frozen_column_is_locked(db, column, value):
    """逐列锁死。只锁 frozen_payload 一列会让别的冻结面从旁边溜走。"""
    plan_id, _, _, _ = _seed_plan(db)
    with pytest.raises(psycopg2.errors.RestrictViolation):
        with db.cursor() as cur:
            cur.execute(
                f"UPDATE defgeo_question_plans SET {column}={value} "
                "WHERE plan_id=%s AND plan_revision=1",
                (plan_id,),
            )
    db.rollback()


def test_question_plan_delete_is_rejected(db):
    plan_id, _, _, _ = _seed_plan(db)
    with pytest.raises(psycopg2.errors.RestrictViolation):
        with db.cursor() as cur:
            cur.execute("DELETE FROM defgeo_question_plans WHERE plan_id=%s", (plan_id,))
    db.rollback()


def test_superseded_marker_is_the_only_allowed_inplace_change(db):
    """反向对照:唯一允许的原地变更必须**真的能过** ——

    否则上面那堆 RestrictViolation 可能只是因为 trigger 无脑拒一切,
    那样「锁得对不对」就没被验过。
    """
    plan_id, _, _, _ = _seed_plan(db)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE defgeo_question_plans SET superseded_by_revision=2 "
            "WHERE plan_id=%s AND plan_revision=1 RETURNING superseded_by_revision",
            (plan_id,),
        )
        assert cur.fetchone()["superseded_by_revision"] == 2
    # 但已标记后不许再改回去 / 再改成别的
    with pytest.raises(psycopg2.errors.RestrictViolation):
        with db.cursor() as cur:
            cur.execute(
                "UPDATE defgeo_question_plans SET superseded_by_revision=NULL "
                "WHERE plan_id=%s AND plan_revision=1",
                (plan_id,),
            )
    db.rollback()


def test_db_trigger_is_load_bearing_not_the_app_guard(db):
    """🔴 证明**拆掉应用层守卫,DB 仍然挡得住**。

    这条是两层守卫分工的证据:应用层只负责把拒绝翻成人话,
    承重的是 DB。若哪天有人删了 plan_store 的守卫,这条仍然绿 ——
    而那正是我们想要的(纵深防御,非承重那层可以没有)。
    """
    plan_id, _, _, _ = _seed_plan(db)
    # 完全不经过 plan_store,直接打 DB
    with pytest.raises(psycopg2.errors.RestrictViolation):
        with db.cursor() as cur:
            cur.execute(
                "UPDATE defgeo_question_plans SET frozen_payload='{\"x\":1}'::jsonb "
                "WHERE plan_id=%s",
                (plan_id,),
            )
    db.rollback()


@pytest.mark.parametrize("col,val", [
    ("base_points", "0"),
    ("exact_total_points", "0"),
    ("funding_policy", "'organization_budget'"),
    ("approval_requirement", "'required'"),
    ("frozen_payload", "'{}'::jsonb"),
    ("expires_at", "NOW() + INTERVAL '10 years'"),
    ("canonical_hash", "repeat('f',64)"),
    ("question_plan_revision", "99"),
])
def test_preview_frozen_bytes_never_change_with_balance_or_approval(db, col, val):
    """🔴 §19 变异 134:preview 随余额/审批变化。冻结面逐列锁死。

    ⚠️ **一列一条,不在一个用例里循环**。第一版写成循环 + 循环内 `db.rollback()`,
       结果第一轮回滚把种子行冲掉,第二轮起 UPDATE 匹配 **0 行** → 不抛 → 判据红,
       而红的原因跟被测代码毫无关系(裸 SQL 下 trigger 明明会拒)。
       「UPDATE 到 0 行」和「UPDATE 被拒」在 `pytest.raises` 眼里长得一样 ——
       所以下面先断言这一行真的存在,再去改它。
    """
    pid, _, _ = _seed_preview(db)
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM defgeo_diagnosis_run_previews WHERE preview_id=%s", (pid,))
        assert cur.fetchone() is not None, "种子行不在 —— 下面的『改不动』会是假绿"

    with pytest.raises(psycopg2.errors.RestrictViolation):
        with db.cursor() as cur:
            cur.execute(
                f"UPDATE defgeo_diagnosis_run_previews SET {col}={val} WHERE preview_id=%s",
                (pid,),
            )
    db.rollback()


def test_update_matching_zero_rows_would_not_raise(db):
    """反向对照,坐实上面那条注释:打一个**不存在**的 preview_id,UPDATE 静默成功。

    这条存在的意义 = 证明「没抛异常」有两种成因,判据必须先排除第二种。
    """
    with db.cursor() as cur:
        cur.execute(
            "UPDATE defgeo_diagnosis_run_previews SET base_points=0 WHERE preview_id=%s",
            (str(uuid.uuid4()),),
        )
        assert cur.rowcount == 0
    db.rollback()


# ══════════════════════ 条件④ lifecycle 三值 CHECK ════════════════════════
@pytest.mark.parametrize("bad", ["running", "committed", "frozen", "queued", "OPEN", ""])
def test_lifecycle_check_rejects_anything_but_three_values(db, bad):
    """🔴 条件④。特别注意 `running`/`committed`/`frozen` —— 那些是
    settlement 语义的词,一旦能存进来,这张表就变成第二套 settlement enum 了。"""
    pid = plan_store.new_preview_id()
    with pytest.raises(psycopg2.errors.CheckViolation):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO defgeo_diagnosis_run_previews "
                "(preview_id,tenant_owner_user_id,brand_id,created_by_user_id,question_plan_id,"
                " question_plan_revision,question_plan_hash,profile_revision_id,campaign_mode,"
                " frozen_payload,canonical_hash,feature_code,pricing_catalog_version,"
                " base_points,extra_points,exact_total_points,funding_policy,principal_kind,"
                " approval_requirement,planned_cells,lifecycle,idempotency_key,"
                " canonical_request_hash,expires_at) "
                "VALUES (%s,1,1,1,%s,1,%s,'p','defensive','{}'::jsonb,%s,'geo_diagnosis','c',"
                " 1,0,1,'personal_wallet','personal','not_required',1,%s,'k',%s,NOW())",
                (pid, str(uuid.uuid4()), "a" * 64, "b" * 64, bad, "c" * 64),
            )
    db.rollback()


@pytest.mark.parametrize("good", ["open", "expired", "consumed"])
def test_lifecycle_check_accepts_the_three(db, good):
    """反向对照:三个合法值必须真能存 —— 否则上面那组可能只是恒拒。"""
    pid = plan_store.new_preview_id()
    consumed_cols = (", consumed_command_id, consumed_at" if good == "consumed" else "")
    consumed_vals = (", 'run_x', NOW()" if good == "consumed" else "")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO defgeo_diagnosis_run_previews "
            "(preview_id,tenant_owner_user_id,brand_id,created_by_user_id,question_plan_id,"
            " question_plan_revision,question_plan_hash,profile_revision_id,campaign_mode,"
            " frozen_payload,canonical_hash,feature_code,pricing_catalog_version,"
            " base_points,extra_points,exact_total_points,funding_policy,principal_kind,"
            " approval_requirement,planned_cells,lifecycle,idempotency_key,"
            f" canonical_request_hash,expires_at{consumed_cols}) "
            "VALUES (%s,1,1,1,%s,1,%s,'p','defensive','{}'::jsonb,%s,'geo_diagnosis','c',"
            f" 1,0,1,'personal_wallet','personal','not_required',1,%s,%s,%s,NOW(){consumed_vals}) "
            "RETURNING lifecycle",
            (pid, str(uuid.uuid4()), "a" * 64, "b" * 64, good, uuid.uuid4().hex, "c" * 64),
        )
        assert cur.fetchone()["lifecycle"] == good
    db.rollback()


def test_consumed_must_point_back_to_a_command(db):
    """consumed 却指不回 command = 半状态。双向锁,不是只锁一半。"""
    pid = plan_store.new_preview_id()
    with pytest.raises(psycopg2.errors.CheckViolation):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO defgeo_diagnosis_run_previews "
                "(preview_id,tenant_owner_user_id,brand_id,created_by_user_id,question_plan_id,"
                " question_plan_revision,question_plan_hash,profile_revision_id,campaign_mode,"
                " frozen_payload,canonical_hash,feature_code,pricing_catalog_version,"
                " base_points,extra_points,exact_total_points,funding_policy,principal_kind,"
                " approval_requirement,planned_cells,lifecycle,idempotency_key,"
                " canonical_request_hash,expires_at) "
                "VALUES (%s,1,1,1,%s,1,%s,'p','defensive','{}'::jsonb,%s,'geo_diagnosis','c',"
                " 1,0,1,'personal_wallet','personal','not_required',1,'consumed','k',%s,NOW())",
                (pid, str(uuid.uuid4()), "a" * 64, "b" * 64, "c" * 64),
            )
    db.rollback()


def test_open_may_not_carry_a_command(db):
    """反方向同样锁:open 却挂着 command 也是半状态。"""
    pid = plan_store.new_preview_id()
    with pytest.raises(psycopg2.errors.CheckViolation):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO defgeo_diagnosis_run_previews "
                "(preview_id,tenant_owner_user_id,brand_id,created_by_user_id,question_plan_id,"
                " question_plan_revision,question_plan_hash,profile_revision_id,campaign_mode,"
                " frozen_payload,canonical_hash,feature_code,pricing_catalog_version,"
                " base_points,extra_points,exact_total_points,funding_policy,principal_kind,"
                " approval_requirement,planned_cells,lifecycle,idempotency_key,"
                " canonical_request_hash,expires_at,consumed_command_id,consumed_at) "
                "VALUES (%s,1,1,1,%s,1,%s,'p','defensive','{}'::jsonb,%s,'geo_diagnosis','c',"
                " 1,0,1,'personal_wallet','personal','not_required',1,'open','k',%s,NOW(),'run_x',NOW())",
                (pid, str(uuid.uuid4()), "a" * 64, "b" * 64, "c" * 64),
            )
    db.rollback()


def test_terminal_lifecycle_cannot_be_reopened(db):
    """🔴 §19 变异 96/134:expired/consumed 再开放。"""
    pid, _, _ = _seed_preview(db)
    with db.cursor() as cur:
        assert plan_store.mark_preview_consumed(
            cur, preview_id=pid, tenant_owner_user_id=ROOT_TENANT, command_id="run_abc")
    with pytest.raises(psycopg2.errors.RestrictViolation):
        with db.cursor() as cur:
            cur.execute(
                "UPDATE defgeo_diagnosis_run_previews SET lifecycle='open' WHERE preview_id=%s",
                (pid,),
            )
    db.rollback()


def test_consume_is_cas_exactly_once(db):
    """DIA-FIN-02 的地基:第二次 consume 必须返回 False,而不是再建一个 command。"""
    pid, _, _ = _seed_preview(db)
    with db.cursor() as cur:
        first = plan_store.mark_preview_consumed(
            cur, preview_id=pid, tenant_owner_user_id=ROOT_TENANT, command_id="run_1")
        second = plan_store.mark_preview_consumed(
            cur, preview_id=pid, tenant_owner_user_id=ROOT_TENANT, command_id="run_2")
    assert first is True
    assert second is False, "第二次 consume 成功了 —— 会产生两个 command / 两次扣费"
    with db.cursor() as cur:
        cur.execute("SELECT consumed_command_id FROM defgeo_diagnosis_run_previews WHERE preview_id=%s", (pid,))
        assert cur.fetchone()["consumed_command_id"] == "run_1", "第二次把 command 覆盖掉了"
    db.rollback()


# ══════════════════════ 条件⑤ 归属每请求现做 ══════════════════════════════
def test_cross_tenant_plan_read_is_indistinguishable_from_missing(db):
    """🔴 ACT-02:跨租户与不存在返回**同一个**异常,不泄露对象存在性。"""
    plan_id, _, _, _ = _seed_plan(db, tenant=ROOT_TENANT)
    with db.cursor() as cur:
        # 真主人读得到
        got = plan_store.get_plan_exact_revision(
            cur, plan_id=plan_id, plan_revision=1, tenant_owner_user_id=ROOT_TENANT)
        assert got["plan_id"] == uuid.UUID(plan_id) or str(got["plan_id"]) == plan_id
        # 别人读不到,且异常与「压根不存在」同形
        with pytest.raises(plan_store.PlanNotFound) as cross:
            plan_store.get_plan_exact_revision(
                cur, plan_id=plan_id, plan_revision=1, tenant_owner_user_id=OTHER_TENANT)
        with pytest.raises(plan_store.PlanNotFound) as absent:
            plan_store.get_plan_exact_revision(
                cur, plan_id=str(uuid.uuid4()), plan_revision=1, tenant_owner_user_id=OTHER_TENANT)
    assert type(cross.value) is type(absent.value)
    db.rollback()


def test_cross_tenant_preview_read_is_indistinguishable_from_missing(db):
    pid, _, _ = _seed_preview(db, tenant=ROOT_TENANT)
    with db.cursor() as cur:
        plan_store.get_run_preview(cur, preview_id=pid, tenant_owner_user_id=ROOT_TENANT)
        with pytest.raises(plan_store.PlanNotFound):
            plan_store.get_run_preview(cur, preview_id=pid, tenant_owner_user_id=OTHER_TENANT)
    db.rollback()


def test_cross_tenant_cannot_consume_someone_elses_preview(db):
    """归属进 WHERE,不是查出来再比 —— 写路径同样要挡。"""
    pid, _, _ = _seed_preview(db, tenant=ROOT_TENANT)
    with db.cursor() as cur:
        assert plan_store.mark_preview_consumed(
            cur, preview_id=pid, tenant_owner_user_id=OTHER_TENANT, command_id="run_x") is False
        # 反向对照:真主人可以
        assert plan_store.mark_preview_consumed(
            cur, preview_id=pid, tenant_owner_user_id=ROOT_TENANT, command_id="run_ok") is True
    db.rollback()


def test_tables_have_no_foreign_keys(db):
    """条件⑤ 的结构面:刻意不加 FK —— 归属不能靠 FK 假装做过。"""
    with db.cursor() as cur:
        cur.execute(
            "SELECT conname, conrelid::regclass::text AS t FROM pg_constraint "
            "WHERE contype='f' AND conrelid::regclass::text IN "
            "('defgeo_question_plans','defgeo_diagnosis_run_previews')"
        )
        fks = cur.fetchall()
    assert fks == [], f"出现 FK:{fks} —— FK 会让人以为归属已被校验,且软删/租户迁移时是部署期地雷"


# ══════════════════════ 条件⑦ 禁第二套 settlement enum ════════════════════
def test_no_second_settlement_enum(db):
    """🔴 §3.5:confirm 后唯一 settlement authority 是 diagnosis_runs.run_status。

    分母从 **census 机械导出**的现役 run_status 值表来,不手抄:
    只要 preview 表里出现任何一列能装下那些值的 settlement 语义列,就红。
    """
    from scripts.defgeo_census.run_status_census import build as build_run_status

    live_values = set(build_run_status()["allowed_values"])
    assert live_values, "run_status census 分母为空 —— 本条恒绿,不算验过"

    with db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='defgeo_diagnosis_run_previews'"
        )
        cols = {r["column_name"] for r in cur.fetchall()}

    forbidden = {"run_status", "run_state", "funding_state", "settlement_state",
                 "billing_mode", "status"}
    hit = cols & forbidden
    assert not hit, (
        f"preview 表出现 settlement 语义列 {sorted(hit)} —— 那就是第二套 settlement enum。"
        f"现役权威值表({len(live_values)} 值)只属于 diagnosis_runs.run_status"
    )
    # 反向对照:lifecycle 必须在(否则「没有 settlement 列」可能只是因为表是空的)
    assert "lifecycle" in cols


def test_lifecycle_values_do_not_overlap_live_run_status(db):
    """三个 lifecycle 值与现役 13 个 run_status 值**零交集** ——

    有交集就意味着两套状态机的词开始混用,下一步必然有人拿 lifecycle 当结算依据。
    """
    from scripts.defgeo_census.run_status_census import build as build_run_status

    live = set(build_run_status()["allowed_values"])
    lifecycle = {"open", "expired", "consumed"}
    assert not (live & lifecycle), f"词表重叠:{sorted(live & lifecycle)}"


# ══════════════════════ 幂等 ══════════════════════════════════════════════
def test_plan_insert_is_idempotent_on_same_request_content(db):
    """§15.2:换 HTTP key 但同 clientRequestId + 同请求内容仍返回同一 plan/revision。

    🔴 幂等挂 ``request_content_hash``,**不是** ``canonical_hash``。
       第二次调用刻意用一个**全新的 plan_id**(因而 canonical_hash 也不同)——
       这正是生产的真实形状:每个请求都会先 mint 一个新 plan_id。
       若幂等挂在 canonical_hash 上,这条必红。
    """
    creq = f"creq-{uuid.uuid4().hex[:8]}"
    content = request_content_hash(
        brand_id=901, profile_revision_id="prof-1", mode="defensive",
        question_set_version="qs-v1",
        questions=(("第1题:这个牌子靠谱吗", "defensive", "identity_check", "named"),
                   ("第2题:这个牌子靠谱吗", "defensive", "identity_check", "named")),
    )
    plan_id, row1, qs, h = _seed_plan(db, client_request_id=creq, content_hash=content)

    other_plan_id = new_plan_id()
    other_qs = (_q(other_plan_id, 1), _q(other_plan_id, 2))
    other_hash = canonical_hash(brand_id=901, profile_revision_id="prof-1", mode="defensive",
                                question_set_version="qs-v1", questions=other_qs)
    assert other_hash != h, "两次的 canonical_hash 应当不同(plan_id 不同)—— 否则本条没在验真形状"

    with db.cursor() as cur:
        row2 = plan_store.insert_plan_revision(
            cur, plan_id=other_plan_id, plan_revision=1, tenant_owner_user_id=ROOT_TENANT,
            brand_id=901, profile_revision_id="prof-1", mode="defensive",
            question_set_version="qs-v1", canonical_hash=other_hash, questions=other_qs,
            defensive_count=2, offensive_count=0, total_count=2,
            client_request_id=creq, request_content_hash=content,
            expires_at=_future(), created_by_user_id=ROOT_TENANT,
        )
    assert row2["id"] == row1["id"], "同 clientRequestId + 同请求内容建出了第二行"
    db.rollback()


def test_different_request_content_creates_a_different_plan(db):
    """反向对照:内容真的不同就必须建新行 —— 否则上面那条可以靠「永远返回旧行」通过。"""
    creq = f"creq-{uuid.uuid4().hex[:8]}"
    _, row1, _, _ = _seed_plan(db, client_request_id=creq, content_hash="a" * 64)
    _, row2, _, _ = _seed_plan(db, client_request_id=creq, content_hash="b" * 64)
    assert row1["id"] != row2["id"], "内容不同却被当成同一份题单"
    db.rollback()


def test_preview_replay_is_flagged_not_silently_successful(db):
    """🔴 幂等重放必须**明示** —— 「返回成功」不等于「这次真的冻了钱」。"""
    idem = f"idem-{uuid.uuid4().hex[:8]}"
    pid1, row1, replayed1 = _seed_preview(db, idem=idem)
    pid2, row2, replayed2 = _seed_preview(db, idem=idem)
    assert replayed1 is False
    assert replayed2 is True, "第二次没被标成 replay —— 调用方会以为该再冻一次钱"
    assert row2["preview_id"] == row1["preview_id"]
    db.rollback()


def test_zero_planned_cells_is_refused(db):
    """§15.4:零计划格不得启动。progressPct 的分母不能是 0。"""
    with pytest.raises(psycopg2.errors.CheckViolation):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO defgeo_diagnosis_run_previews "
                "(preview_id,tenant_owner_user_id,brand_id,created_by_user_id,question_plan_id,"
                " question_plan_revision,question_plan_hash,profile_revision_id,campaign_mode,"
                " frozen_payload,canonical_hash,feature_code,pricing_catalog_version,"
                " base_points,extra_points,exact_total_points,funding_policy,principal_kind,"
                " approval_requirement,planned_cells,lifecycle,idempotency_key,"
                " canonical_request_hash,expires_at) "
                "VALUES (%s,1,1,1,%s,1,%s,'p','defensive','{}'::jsonb,%s,'geo_diagnosis','c',"
                " 1,0,1,'personal_wallet','personal','not_required',0,'open','k',%s,NOW())",
                (plan_store.new_preview_id(), str(uuid.uuid4()), "a" * 64, "b" * 64, "c" * 64),
            )
    db.rollback()


def test_points_arithmetic_is_enforced_at_db_level_too(db):
    """算术守恒在应用层(funding_projection)与 DB CHECK **各写一次**。

    两处同时改错的概率远低于一处。这条验的是 DB 那一处。
    """
    with pytest.raises(psycopg2.errors.CheckViolation):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO defgeo_diagnosis_run_previews "
                "(preview_id,tenant_owner_user_id,brand_id,created_by_user_id,question_plan_id,"
                " question_plan_revision,question_plan_hash,profile_revision_id,campaign_mode,"
                " frozen_payload,canonical_hash,feature_code,pricing_catalog_version,"
                " base_points,extra_points,exact_total_points,funding_policy,principal_kind,"
                " approval_requirement,planned_cells,lifecycle,idempotency_key,"
                " canonical_request_hash,expires_at) "
                "VALUES (%s,1,1,1,%s,1,%s,'p','defensive','{}'::jsonb,%s,'geo_diagnosis','c',"
                " 100,20,999,'personal_wallet','personal','not_required',1,'open','k',%s,NOW())",
                (plan_store.new_preview_id(), str(uuid.uuid4()), "a" * 64, "b" * 64, "c" * 64),
            )
    db.rollback()
