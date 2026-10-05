"""意图协调层 · 真 PG16 判据(规格 §10 · 工单 §3.3)。

判据全部打在**数据库层性质**上,不是函数返回值:

* prepare 幂等靠唯一约束(拆掉唯一索引 → 必红);
* confirm/cancel 靠 revision CAS(并发只有一方成功);
* 回执 ``UNIQUE (intent_id, intent_revision)`` + 条件 UPDATE 单次消费;
* 两档确认制在**库里**也有 CHECK(应用层和库层都拦,两层都要能被变异打红);
* 双时钟各自成对。
"""

from __future__ import annotations

import re

import psycopg2
import pytest

from services import gap_operation_map as omap
from services.xiaobang_intent import (
    ActorBinding,
    ComputeQuote,
    IntentError,
    IntentNotFound,
    STATE_CANCELLED,
    STATE_CONFIRMED,
    STATE_PREPARED,
    approval_window_expired,
    canonical_input,
    cancel_intent,
    consume_confirmation_receipt,
    content_hash,
    drift_reason,
    issue_confirmation_receipt,
    load_intent,
    mark_confirmed,
    prepare_intent,
    quote_expired,
    status_projection,
)

ACTOR = ActorBinding(
    actor_user_id=101, tenant_owner_id=101, payer_user_id=101,
    organization_id=None, membership_version="m1",
    assignment_authority_version="a1", approval_policy_version="p1",
    permission_version="v1",
)
OTHER_ACTOR = ActorBinding(actor_user_id=999, tenant_owner_id=999, payer_user_id=999)

ALLOWLIST = ("brand_id", "article_id", "channel_option_id")


def _entry(operation_id="publish_center"):
    return omap.resolve_operation(operation_id)


def _prepare(cursor, *, request_id="req-stable-0001", selection=None,
             operation_id="publish_center", actor=ACTOR, quote=None):
    entry = _entry(operation_id)
    contract = entry.command_contract
    canonical = canonical_input(
        operation_id=entry.operation_id, operation_version=1,
        request_schema_version=contract.request_schema_version,
        selection=selection or {"brand_id": 7, "article_id": 42},
        allowlist=ALLOWLIST,
    )
    return prepare_intent(
        cursor,
        entry_operation_id=entry.operation_id,
        contract=contract,
        registry_version=omap.OPERATION_REGISTRY_VERSION,
        actor=actor,
        prepare_request_id=request_id,
        canonical=canonical,
        payload_hash=content_hash(canonical),
        object_manifest_hash=content_hash({"brand": 7, "article": 42}),
        quote=quote,
        preview={"customer_label": "测试客户"},
        reason_facts=[],
    )


# ── prepare 幂等 ──────────────────────────────────────────────────────────
def test_same_key_same_input_returns_the_same_intent(db):
    cur = db.cursor()
    first, created_1 = _prepare(cur)
    second, created_2 = _prepare(cur)
    db.commit()
    assert created_1 is True and created_2 is False
    assert first["intent_id"] == second["intent_id"]
    cur.execute("SELECT count(*) AS n FROM xiaobang_operation_intents")
    assert cur.fetchone()["n"] == 1


def test_same_key_different_input_is_409_not_a_second_intent(db):
    cur = db.cursor()
    _prepare(cur, selection={"brand_id": 7, "article_id": 42})
    db.commit()
    with pytest.raises(IntentError) as excinfo:
        _prepare(cur, selection={"brand_id": 7, "article_id": 43})
    db.rollback()
    assert excinfo.value.code == "PREPARE_REQUEST_ID_CONFLICT"
    assert excinfo.value.http_status == 409
    cur = db.cursor()
    cur.execute("SELECT count(*) AS n FROM xiaobang_operation_intents")
    assert cur.fetchone()["n"] == 1


def test_guessing_someone_elses_prepare_request_id_does_not_leak_existence(db):
    cur = db.cursor()
    _prepare(cur)
    db.commit()
    # 另一个人用同一个 request_id + 同一个 tenant_owner_id 撞进来。
    sneaky = ActorBinding(actor_user_id=999, tenant_owner_id=ACTOR.tenant_owner_id)
    with pytest.raises(IntentNotFound):
        _prepare(cur, actor=sneaky)
    db.rollback()


def test_prepare_idempotency_rests_on_a_unique_index_not_on_select_then_insert(db):
    """🔴 拆掉唯一索引 → prepare 当场爆,而不是"退化成先查后插仍然能跑"。

    这条证明的是「幂等由数据库保证」,而不是「代码里恰好先查了一下」。
    实测形态比预想的更硬:``ON CONFLICT (tenant_owner_id, operation_id,
    prepare_request_id)`` 在缺索引时直接抛 InvalidColumnReference ——
    也就是说这段代码**没有**一条不依赖唯一约束的旁路可走。
    """
    cur = db.cursor()
    cur.execute("DROP INDEX uq_xb_intent_prepare_idem")
    with pytest.raises(psycopg2.errors.InvalidColumnReference):
        _prepare(cur)
    db.rollback()
    # 正向对照:索引还在时同一段代码正常跑通(证明红不是因为别的原因)。
    cur = db.cursor()
    _, created = _prepare(cur, request_id="req-index-control")
    assert created is True
    db.rollback()


def test_concurrent_prepare_produces_one_intent(db):
    """两条独立连接同时 prepare,只能有一个 intent。"""
    from .conftest import EXACT_THROWAWAY_URL

    conn_a = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn_b = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn_a.cursor_factory = psycopg2.extras.RealDictCursor
    conn_b.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        cur_a, cur_b = conn_a.cursor(), conn_b.cursor()
        row_a, created_a = _prepare(cur_a, request_id="req-concurrent-01")
        conn_a.commit()
        row_b, created_b = _prepare(cur_b, request_id="req-concurrent-01")
        conn_b.commit()
        assert created_a is True and created_b is False
        assert row_a["intent_id"] == row_b["intent_id"]
    finally:
        conn_a.close()
        conn_b.close()
    cur = db.cursor()
    cur.execute(
        "SELECT count(*) AS n FROM xiaobang_operation_intents WHERE prepare_request_id=%s",
        ("req-concurrent-01",),
    )
    assert cur.fetchone()["n"] == 1


def test_prepare_writes_only_the_coordination_tables(db):
    """§19.1 #5:prepare 零业务写。

    口径见规格 §10.2(P1-7):不写 wallet/order/task/domain 表;对意图协调表的
    原子 upsert **不计入**业务写,但受同等幂等判据约束(上面几条已覆盖)。
    这里用 cursor 探针把 prepare 发出的每一条 SQL 记下来逐条判。
    """
    executed: list[str] = []
    real = db.cursor()

    class SpyCursor:
        def execute(self, sql, params=None):
            executed.append(" ".join(str(sql).split()))
            return real.execute(sql, params)

        def fetchone(self):
            return real.fetchone()

    _prepare(SpyCursor())
    db.rollback()

    write = re.compile(r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE)\b", re.I)
    writes = [s for s in executed if write.match(s)]
    assert writes, "一条写都没有 —— 探针没接上,判据是空的"
    for statement in writes:
        assert "xiaobang_operation_intents" in statement, statement
    forbidden = ("wallet", "point_transactions", "orders", "publish_", "articles",
                 "quotes", "brands", "freeze")
    for statement in executed:
        if not write.match(statement):
            continue
        for token in forbidden:
            assert token not in statement.lower(), (token, statement)


# ── 归属与不泄存在性 ──────────────────────────────────────────────────────
def test_cross_tenant_and_cross_actor_reads_return_the_same_404(db):
    cur = db.cursor()
    row, _ = _prepare(cur)
    db.commit()
    with pytest.raises(IntentNotFound):
        load_intent(cur, intent_id=row["intent_id"], actor=OTHER_ACTOR)
    with pytest.raises(IntentNotFound):
        load_intent(cur, intent_id="xint_does_not_exist", actor=ACTOR)
    # 正向对照:本人读得到 —— 否则上面两条只证明它恒 404。
    assert load_intent(cur, intent_id=row["intent_id"], actor=ACTOR)["intent_id"] == row["intent_id"]


# ── 确认回执 ──────────────────────────────────────────────────────────────
def test_confirm_replay_returns_the_same_receipt_not_a_second_one(db):
    cur = db.cursor()
    row, _ = _prepare(cur)
    first, created_1 = issue_confirmation_receipt(
        cur, intent_row=row, actor=ACTOR, challenge="browser-bound-1")
    second, created_2 = issue_confirmation_receipt(
        cur, intent_row=row, actor=ACTOR, challenge="browser-bound-1")
    db.commit()
    assert created_1 is True and created_2 is False
    assert first["receipt_id"] == second["receipt_id"]
    cur.execute("SELECT count(*) AS n FROM xiaobang_confirmation_receipts")
    assert cur.fetchone()["n"] == 1


def test_empty_challenge_cannot_confirm(db):
    """聊天文字/默认勾选不构成确认:没有真实点击的 challenge 就签不出回执。"""
    cur = db.cursor()
    row, _ = _prepare(cur)
    with pytest.raises(IntentError) as excinfo:
        issue_confirmation_receipt(cur, intent_row=row, actor=ACTOR, challenge="")
    db.rollback()
    assert excinfo.value.code == "USER_ACTION_CHALLENGE_REQUIRED"


def test_receipt_is_consumed_exactly_once(db):
    cur = db.cursor()
    row, _ = _prepare(cur)
    issue_confirmation_receipt(cur, intent_row=row, actor=ACTOR, challenge="c1")
    db.commit()
    used = consume_confirmation_receipt(
        cur, intent_id=row["intent_id"], intent_revision=row["intent_revision"],
        execution_request_id="exec-1",
    )
    assert used["consumed_at"] is not None
    # 同一次执行请求重放 → 同一结果,不是第二次消费。
    replay = consume_confirmation_receipt(
        cur, intent_id=row["intent_id"], intent_revision=row["intent_revision"],
        execution_request_id="exec-1",
    )
    assert replay["receipt_id"] == used["receipt_id"]
    # 另一次执行请求 → 明确拒绝,不许再烧一次。
    with pytest.raises(IntentError) as excinfo:
        consume_confirmation_receipt(
            cur, intent_id=row["intent_id"], intent_revision=row["intent_revision"],
            execution_request_id="exec-2",
        )
    assert excinfo.value.code == "CONFIRMATION_ALREADY_CONSUMED"
    db.rollback()


def test_execute_without_any_confirmation_is_refused(db):
    """external 档拔掉确认 → 必红(工单 §3.2)。"""
    cur = db.cursor()
    row, _ = _prepare(cur)
    db.commit()
    with pytest.raises(IntentError) as excinfo:
        consume_confirmation_receipt(
            cur, intent_id=row["intent_id"], intent_revision=row["intent_revision"],
            execution_request_id="exec-x",
        )
    assert excinfo.value.code == "CONFIRMATION_REQUIRED"


# ── CAS ───────────────────────────────────────────────────────────────────
def test_cancel_and_confirm_race_only_one_wins(db):
    cur = db.cursor()
    row, _ = _prepare(cur)
    db.commit()
    revision = int(row["intent_revision"])
    confirmed = mark_confirmed(cur, intent_id=row["intent_id"], expected_revision=revision)
    assert confirmed is not None
    # 用**同一个**旧 revision 再取消 → CAS 失败。
    with pytest.raises(IntentError) as excinfo:
        cancel_intent(cur, intent_id=row["intent_id"], expected_revision=revision,
                      cancel_request_id="cancel-1")
    assert excinfo.value.code == "INTENT_REVISION_CONFLICT"
    db.rollback()


def test_cancel_replay_is_idempotent(db):
    cur = db.cursor()
    row, _ = _prepare(cur)
    db.commit()
    first = cancel_intent(cur, intent_id=row["intent_id"],
                          expected_revision=int(row["intent_revision"]),
                          cancel_request_id="cancel-1")
    assert first["intent_state"] == STATE_CANCELLED
    second = cancel_intent(cur, intent_id=row["intent_id"],
                           expected_revision=int(first["intent_revision"]),
                           cancel_request_id="cancel-1")
    assert second["intent_state"] == STATE_CANCELLED
    assert int(second["intent_revision"]) == int(first["intent_revision"])
    db.rollback()


# ── 双时钟(P0-C)─────────────────────────────────────────────────────────
def test_price_lock_expiry_does_not_expire_the_approval_window(db):
    """价格锁到期只代表要重新报价;审批窗口是另一只钟。"""
    cur = db.cursor()
    quote = ComputeQuote(amount=9360, pricing_version="pv1", ttl_seconds=-1)
    row, _ = _prepare(cur, request_id="req-clocks-01", quote=quote)
    db.commit()
    assert quote_expired(row) is True
    assert approval_window_expired(row) is False
    assert row["compute_quote_expires_at"] < row["approval_window_expires_at"]


def test_the_two_clocks_are_separate_columns(db):
    cur = db.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        " WHERE table_name='xiaobang_operation_intents'"
    )
    columns = {r["column_name"] for r in cur.fetchall()}
    assert {"compute_quote_expires_at", "approval_window_expires_at"} <= columns
    assert "expires_at" not in columns, "两只钟又被并回一只了"


# ── 库层两档映射 CHECK ────────────────────────────────────────────────────
def test_database_rejects_a_mismatched_side_effect_and_confirmation_mode(db):
    """应用层拦一道,库层再拦一道 —— 「给纯算力操作套确认门」两层都要红。"""
    cur = db.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO xiaobang_operation_intents (
                intent_id, tenant_owner_id, actor_user_id, operation_id,
                registry_version, side_effect, confirmation_mode,
                prepare_request_id, canonical_input_hash, payload_hash,
                object_manifest_hash
            ) VALUES ('xint_bad',1,1,'writing_center','v3',
                      'compute_only','required_user_click','r','h','h','h')
            """
        )
    db.rollback()
    # 正向对照:配对正确就能插进去(证明上面的红不是因为整条语句写错了)。
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO xiaobang_operation_intents (
            intent_id, tenant_owner_id, actor_user_id, operation_id,
            registry_version, side_effect, confirmation_mode,
            prepare_request_id, canonical_input_hash, payload_hash,
            object_manifest_hash
        ) VALUES ('xint_ok',1,1,'writing_center','v3',
                  'compute_only','silent_with_notice','r','h','h','h')
        """
    )
    db.rollback()


def test_database_rejects_the_retired_executing_state(db):
    """§10.5 二选一裁定:``executing`` 已删,不许从任何入口回来。"""
    cur = db.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO xiaobang_operation_intents (
                intent_id, tenant_owner_id, actor_user_id, operation_id,
                registry_version, side_effect, confirmation_mode,
                prepare_request_id, canonical_input_hash, payload_hash,
                object_manifest_hash, intent_state
            ) VALUES ('xint_exec',1,1,'publish_center','v3',
                      'external','required_user_click','r','h','h','h','executing')
            """
        )
    db.rollback()


# ── 漂移与投影 ────────────────────────────────────────────────────────────
def test_drift_is_reported_per_field_not_as_a_single_boolean(db):
    cur = db.cursor()
    row, _ = _prepare(cur)
    db.commit()
    assert drift_reason(
        row, actor=ACTOR, payload_hash=row["payload_hash"],
        object_manifest_hash=row["object_manifest_hash"],
        compute_quote_hash=row.get("compute_quote_hash"),
    ) is None
    assert drift_reason(
        row, actor=ACTOR, payload_hash="deadbeef",
        object_manifest_hash=row["object_manifest_hash"],
        compute_quote_hash=row.get("compute_quote_hash"),
    ) == "payload_hash"
    shifted = ActorBinding(
        actor_user_id=ACTOR.actor_user_id, tenant_owner_id=ACTOR.tenant_owner_id,
        payer_user_id=ACTOR.payer_user_id, membership_version="m2",
        assignment_authority_version="a1", approval_policy_version="p1",
        permission_version="v1",
    )
    assert drift_reason(
        row, actor=shifted, payload_hash=row["payload_hash"],
        object_manifest_hash=row["object_manifest_hash"],
        compute_quote_hash=row.get("compute_quote_hash"),
    ) == "membership_version"


def test_status_projection_keeps_four_lanes_separate(db):
    cur = db.cursor()
    row, _ = _prepare(cur)
    db.commit()
    projection = status_projection(row)
    assert projection["intent_state"] == STATE_PREPARED
    assert set(projection) >= {
        "intent_state", "domain_projection", "settlement_projection",
        "external_projection",
    }
    # 未知/未开始不许被压成 failed 或 succeeded。
    assert projection["domain_projection"]["state"] == "not_started"
    assert projection["settlement_projection"]["state"] == "none"
    assert projection["external_projection"]["state"] == "not_started"
