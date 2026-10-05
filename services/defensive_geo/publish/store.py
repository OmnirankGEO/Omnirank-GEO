"""发布链落库层(迁移 044 的六张表)。**全包唯一写 SQL 的地方**。

判据:MED-13 / MED-19 / MED-20 / FIN-01/02/03/14 / API-04。

═══════════════════════════════════════════════════════════════════════
🔴 三条本文件自己的规矩
═══════════════════════════════════════════════════════════════════════
1. **零 ``SELECT *``**。每条查询逐列写明 —— CUR-10 的病根就是 ``SELECT *`` 直出。
   列清单写成模块常量,判据可以拿它跟 information_schema 对账。
2. **调用方持有事务**。所有函数吃 ``cur``,不自己 commit/rollback。
   §12.3「写入业务对象、资金冻结和 outbox 处于同一数据库事务」——
   本层自己开事务就等于把那句话变成做不到。
3. **并发收敛靠约束不靠 Python**。``ensure_slot`` / ``insert_snapshot`` /
   ``insert_command`` 全部走 ``ON CONFLICT`` 或依赖 partial unique 抛错,
   而不是 ``SELECT ... 然后 IF NOT EXISTS INSERT``(那在并发下必然有窗口)。

🔴 游标形态:生产 ``db.connection.get_connection()`` 用 ``RealDictCursor``,
   而判据底座可能用 tuple 游标。窗B 在 activation_outbox 上被这件事咬过一次
   (``dict(zip(cols, r))`` 在 RealDictCursor 下不抛错,只是安静地返回错值)。
   所以这里统一走 :func:`_rows` / :func:`_one`,两种形态都吃。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Sequence

import psycopg2

logger = logging.getLogger("GEO-DefGeoPublishStore")

#: 🔴 [包E R2] 与迁移 044 ``chk_defgeo_pcmd_platform_state`` **逐字同一个字面值**。
#:    从 ``payer_classification`` 取,不在这里再抄一遍字符串。
from services.defensive_geo.payer_classification import (      # noqa: E402
    PLATFORM_PRINCIPAL_KIND as _PLATFORM_PRINCIPAL_KIND,
)

SLOT_TABLE = "defgeo_publish_slots"
SNAPSHOT_TABLE = "defgeo_publish_decision_snapshots"
COMMAND_TABLE = "defgeo_publish_commands"
OUTBOX_TABLE = "defgeo_publish_outbox"
REVIEW_TABLE = "defgeo_settlement_review_entries"
BUDGET_TABLE = "defgeo_provider_execution_budgets"

#: 🔴 逐列写明。判据拿它跟 information_schema 对账 —— 少一列会当场红。
SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "decision_snapshot_id", "publish_slot_id", "snapshot_version", "canonical_hash",
    "frozen_payload", "lifecycle", "expires_at",
    "superseded_by_snapshot_id", "superseded_by_snapshot_hash", "supersession_kind",
    "parent_snapshot_id", "consumed_command_id",
    "idempotency_key", "request_canonical_hash", "tenant_owner_id",
    "created_at", "lifecycle_changed_at",
)

COMMAND_COLUMNS: tuple[str, ...] = (
    "publish_command_id", "publish_slot_id", "decision_snapshot_id",
    "decision_snapshot_hash", "command_canonical_hash",
    "parent_command_id", "command_generation", "lineage_kind",
    "tenant_owner_id", "actor_user_id", "brand_id", "publish_item_request_id",
    "article_revision_id", "article_hash", "public_media_key", "canonical_root_domain_key",
    "funding_policy", "principal_kind", "payer_user_id", "exact_settlement_points",
    "freeze_id", "freeze_task_ref", "freeze_backend", "organization_charge_ref",
    "approval_ref", "sponsor_policy_ref", "platform_cost_ref", "funding_state",
    "command_state", "canonical_publication_state",
    "raw_state_source_table", "raw_state_source_column", "raw_state_value",
    "url_verification_state", "url_availability_state", "public_url",
    "external_start_at", "external_start_token", "provider_call_count",
    # [047 · 包E] 供应商单据引用。**外部引用,不是第四条状态轴** ——
    # 没有它,「provider 已接受」就没有任何键能与上游那张单对上,
    # 而 §12.3 窗口④ 要求 reconciler 从 canonical outcome 原子收敛,
    # outcome 得先能取回来。
    "provider_order_ref", "provider_last_polled_at",
    "legal_rule_id", "legal_rule_version", "legal_passage_ref", "legal_passage_excerpt",
    "replacement_policy_ref",
    "idempotency_key", "request_canonical_hash", "status_version", "status_reason",
    # [051 · 工单B B-1] 结算重试账。``bump_settlement_attempt`` 是唯一写点。
    "settlement_attempts", "last_settlement_error",
    "created_at", "updated_at", "settled_at",
)

#: 🔴 [B-4] ``defgeo_publish_commands.funding_state`` 的**全集**。
#:
#:    与迁移 044 的 ``chk_defgeo_pcmd_funding_state`` 同源 —— 判据
#:    ``test_b4_01`` 直接从那条 SQL 里正则抽出来逐字对账,**不手抄**
#:    (本仓记过:手写分母漏掉的那一项不会让任何判据变红)。
FUNDING_STATES: tuple[str, ...] = (
    "frozen", "committed", "released",
    "pending_reconciliation", "quarantined", "exempt_recorded",
)

#: 每一格资金态对**执行预算占用**的贡献。**闭表,无 else**。
#:
#: ═══════════════════════════════════════════════════════════════════════
#: 🔴 [B-4 = Codex P1-3] 平台发布腿过去对 scope cap 的贡献恒为 0
#: ═══════════════════════════════════════════════════════════════════════
#: ``budget_usage`` 原来的分母是手抄的两个 IN 列表
#: (reserved = frozen/pending_reconciliation/quarantined,committed = committed)。
#: 而迁移 044 的 ``chk_defgeo_pcmd_platform_state`` 保证平台腿恒
#: ``exempt_recorded`` ⇒ **它一格都不落在分母里**:每一笔平台发布对
#: global/scope 两道 cap 的贡献都是 0,cap 说"还剩很多"而平台钱包已经
#: 真冻出去了(``publish_funding._freeze_platform`` 走的是真
#: ``freeze_points(platform_uid, ...)``)。
#:
#: 修法不是"再手抄一格进去",是**让分母机械枚举全集**:
#: 这张表必须覆盖 :data:`FUNDING_STATES` 的每一格,少一格 ``budget_usage``
#: 当场抛(而不是静默按 0 计)。第七格哪天进 CHECK,这里不表态就红。
#:
#: ``by_settlement`` 这一格的含义:平台腿的 ``funding_state`` 是常量,
#: 钱向落在 ``settled_at``(收没收尾)+ ``canonical_publication_state``
#: (收成哪一向)两列上 —— 所以它的归属由结算事实算,不由这一列算。
_BUDGET_CONTRIBUTION: dict[str, str] = {
    "frozen": "reserved",
    "pending_reconciliation": "reserved",
    "quarantined": "reserved",
    "committed": "committed",
    "released": "none",
    "exempt_recorded": "by_settlement",
}

#: 预算贡献的合法取值。闭集,判据拿它当分母。
BUDGET_CONTRIBUTIONS: tuple[str, ...] = ("reserved", "committed", "none", "by_settlement")


def budget_contribution_map() -> dict[str, str]:
    """机械分母 + **覆盖自证**。分母漏一格在这里响亮地红,不在下游静默按 0。"""
    missing = sorted(set(FUNDING_STATES) - set(_BUDGET_CONTRIBUTION))
    extra = sorted(set(_BUDGET_CONTRIBUTION) - set(FUNDING_STATES))
    if missing or extra:
        raise StoreError(
            f"预算分母与 fundingState 全集对不上:缺 {missing} / 多 {extra} —— "
            "没表态的那一格会被静默按 0 计进 cap"
        )
    bad = sorted(v for v in _BUDGET_CONTRIBUTION.values() if v not in BUDGET_CONTRIBUTIONS)
    if bad:
        raise StoreError(f"未知的预算贡献取值 {bad};合法 = {list(BUDGET_CONTRIBUTIONS)}")
    return dict(_BUDGET_CONTRIBUTION)


BUDGET_COLUMNS: tuple[str, ...] = (
    "execution_budget_snapshot_id", "budget_version", "tenant_owner_id",
    "accepted_snapshot_id", "service_projection_id",
    "global_cap_points", "scope_key", "scope_cap_points",
    "funding_policy", "payer_user_id", "organization_id",
    "sponsor_policy_ref", "approval_ref", "budget_hash", "created_at",
)

#: 非终态 command 的集合 —— 与迁移 044 的 partial unique 谓词**同源**。
#: 两处写不一样会让「DB 拦住了但应用层以为没拦」这种最难查的状态出现。
LIVE_COMMAND_STATES: tuple[str, ...] = (
    "accepted", "queued", "running", "settlement_pending", "needs_action", "quarantined",
)
TERMINAL_COMMAND_STATES: tuple[str, ...] = ("completed", "failed", "cancelled")

#: 🔴 与 ``defgeo_pcmd_one_live_per_slot`` 谓词**逐字同源**的例外子句。
#:    法律门命中且钱已退干净的 item 对外仍是 needs_action,但在 slot 全序里已终结
#:    —— 详见迁移 044 那条索引上方的整段注释。
#:    写成常量而不是各处手抄 SQL 片段:同一谓词写两处必有一处没人验。
LIVE_EXCLUSION_SQL = "NOT (legal_rule_id IS NOT NULL AND funding_state = 'released')"


class StoreError(RuntimeError):
    """落库层失败。**抛,让调用方的事务一起回滚**。"""


class IdempotencyConflict(StoreError):
    """同 key 异 payload —— §15.8 ``IDEMPOTENCY_CONFLICT`` 409。"""


def _one(cur) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _rows(cur) -> list[dict[str, Any]]:
    rows = cur.fetchall() or []
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(r) for r in rows]
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _cols(prefix: str, columns: Sequence[str]) -> str:
    return ", ".join(f"{prefix}.{c}" for c in columns)


# ══════════════════════════════════════════════════════════════════════════
# slot
# ══════════════════════════════════════════════════════════════════════════
def ensure_slot(
    cur,
    *,
    publish_slot_id: str,
    tenant_owner_id: int,
    service_projection_id: str,
    accepted_snapshot_id: int,
    plan_item_key: str,
    brand_id: int,
    publish_item_request_id: str,
    command_kind: str = "media_publication",
) -> dict[str, Any]:
    """幂等建格。并发下第 2..N 个走 ``ON CONFLICT DO NOTHING`` 后回读。"""
    cur.execute(
        f"""
        INSERT INTO {SLOT_TABLE}
            (publish_slot_id, tenant_owner_id, service_projection_id, accepted_snapshot_id,
             plan_item_key, command_kind, brand_id, publish_item_request_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (publish_slot_id) DO NOTHING
        """,
        (publish_slot_id, tenant_owner_id, service_projection_id, accepted_snapshot_id,
         plan_item_key, command_kind, brand_id, publish_item_request_id),
    )
    cur.execute(
        f"""
        SELECT publish_slot_id, tenant_owner_id, service_projection_id, accepted_snapshot_id,
               plan_item_key, command_kind, brand_id, publish_item_request_id, created_at
        FROM {SLOT_TABLE} WHERE publish_slot_id = %s
        """,
        (publish_slot_id,),
    )
    row = _one(cur)
    if row is None:                                     # pragma: no cover - 不该发生
        raise StoreError(f"slot {publish_slot_id} 建完却读不到")
    return row


def lock_slot(cur, publish_slot_id: str) -> None:
    """§12.2「confirm/override/cancel 与新 preview 共用 slot advisory/row lock」。

    用 ``SELECT ... FOR UPDATE`` 锁 slot 行 —— 它是这几条路径唯一共同的父对象。
    没有它,partial unique 仍能挡住双写,但失败方会拿到一个裸 UniqueViolation
    而不是一个可解释的 typed 409。
    """
    cur.execute(f"SELECT 1 FROM {SLOT_TABLE} WHERE publish_slot_id = %s FOR UPDATE",
                (publish_slot_id,))


# ══════════════════════════════════════════════════════════════════════════
# snapshot
# ══════════════════════════════════════════════════════════════════════════
def open_snapshot(cur, *, tenant_owner_id: int, publish_slot_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"SELECT {', '.join(SNAPSHOT_COLUMNS)} FROM {SNAPSHOT_TABLE} "
        f"WHERE tenant_owner_id = %s AND publish_slot_id = %s AND lifecycle = 'open'",
        (tenant_owner_id, publish_slot_id),
    )
    return _one(cur)


def get_snapshot(cur, *, decision_snapshot_id: str, tenant_owner_id: int) -> dict[str, Any] | None:
    """MED-13:exact GET **逐次重验归属**;跨租户与不存在同形 404 ——
    实现形态就是把 tenant 放进 WHERE(而不是查到之后再比)。"""
    cur.execute(
        f"SELECT {', '.join(SNAPSHOT_COLUMNS)} FROM {SNAPSHOT_TABLE} "
        f"WHERE decision_snapshot_id = %s AND tenant_owner_id = %s",
        (decision_snapshot_id, tenant_owner_id),
    )
    return _one(cur)


def find_by_idempotency(
    cur, *, tenant_owner_id: int, publish_slot_id: str, idempotency_key: str,
) -> list[dict[str, Any]]:
    cur.execute(
        f"SELECT {', '.join(SNAPSHOT_COLUMNS)} FROM {SNAPSHOT_TABLE} "
        f"WHERE tenant_owner_id = %s AND publish_slot_id = %s AND idempotency_key = %s "
        f"ORDER BY snapshot_version DESC",
        (tenant_owner_id, publish_slot_id, idempotency_key),
    )
    return _rows(cur)


def supersede_open(
    cur,
    *,
    tenant_owner_id: int,
    publish_slot_id: str,
    successor_id: str,
    successor_hash: str,
    kind: str,
) -> str | None:
    """CAS:把当前 open snapshot 原子标成 superseded。返回被替代者 id(没有则 None)。

    🔴 ``WHERE lifecycle='open'`` 是 CAS 的全部 —— 两个并发 override 只有一个
       能把同一行从 open 改走,另一个 rowcount=0 于是零副作用(MED-19
       「20 并发恰一 winner」)。
    """
    cur.execute(
        f"""
        UPDATE {SNAPSHOT_TABLE}
           SET lifecycle = 'superseded',
               superseded_by_snapshot_id = %s,
               superseded_by_snapshot_hash = %s,
               supersession_kind = %s,
               lifecycle_changed_at = NOW()
         WHERE tenant_owner_id = %s AND publish_slot_id = %s AND lifecycle = 'open'
        RETURNING decision_snapshot_id
        """,
        (successor_id, successor_hash, kind, tenant_owner_id, publish_slot_id),
    )
    row = _one(cur)
    return row["decision_snapshot_id"] if row else None


def insert_snapshot(
    cur,
    *,
    decision_snapshot_id: str,
    publish_slot_id: str,
    snapshot_version: int,
    canonical_hash: str,
    frozen_payload: Mapping[str, Any],
    expires_at: str,
    idempotency_key: str,
    request_canonical_hash: str,
    tenant_owner_id: int,
    parent_snapshot_id: str | None = None,
) -> dict[str, Any]:
    """写一条 open snapshot。

    🔴 同 slot 第二个 open 会撞 ``defgeo_pds_one_open_per_slot``。
       这里**不吞** UniqueViolation —— 调用方必须先 CAS supersede 再插,
       吞掉等于允许「两个可 confirm 的 snapshot」短暂存在。
    """
    try:
        cur.execute(
            f"""
            INSERT INTO {SNAPSHOT_TABLE}
                (decision_snapshot_id, publish_slot_id, snapshot_version, canonical_hash,
                 frozen_payload, lifecycle, expires_at, parent_snapshot_id,
                 idempotency_key, request_canonical_hash, tenant_owner_id)
            VALUES (%s, %s, %s, %s, %s::jsonb, 'open', %s, %s, %s, %s, %s)
            RETURNING {', '.join(SNAPSHOT_COLUMNS)}
            """,
            (decision_snapshot_id, publish_slot_id, int(snapshot_version), canonical_hash,
             json.dumps(frozen_payload, ensure_ascii=False), expires_at, parent_snapshot_id,
             idempotency_key, request_canonical_hash, int(tenant_owner_id)),
        )
    except psycopg2.errors.UniqueViolation as exc:       # type: ignore[attr-defined]
        raise StoreError(
            f"同 slot 已有 open snapshot,不能再插第二个(约束 {exc.diag.constraint_name}) —— "
            "任何时刻不能有两个可 confirm 的 snapshot(§12.2)"
        ) from exc
    row = _one(cur)
    if row is None:                                      # pragma: no cover
        raise StoreError("insert_snapshot 没有返回行")
    return row


def cas_lifecycle(
    cur, *, decision_snapshot_id: str, tenant_owner_id: int,
    expect: str, to: str, consumed_command_id: str | None = None,
) -> bool:
    """把 snapshot 的 lifecycle 从 ``expect`` 原子改成 ``to``。返回是否赢。"""
    cur.execute(
        f"""
        UPDATE {SNAPSHOT_TABLE}
           SET lifecycle = %s,
               consumed_command_id = %s,
               lifecycle_changed_at = NOW()
         WHERE decision_snapshot_id = %s AND tenant_owner_id = %s AND lifecycle = %s
        """,
        (to, consumed_command_id, decision_snapshot_id, tenant_owner_id, expect),
    )
    return cur.rowcount == 1


def expire_stale_open(cur, *, tenant_owner_id: int, publish_slot_id: str) -> int:
    """把已过期的 open snapshot 标成 expired。

    🔴 **不延长 expiry**,也不顺手重新推荐(MED-13 逐字)。过期就是过期。
    """
    cur.execute(
        f"""
        UPDATE {SNAPSHOT_TABLE}
           SET lifecycle = 'expired', lifecycle_changed_at = NOW()
         WHERE tenant_owner_id = %s AND publish_slot_id = %s
           AND lifecycle = 'open' AND expires_at <= NOW()
        """,
        (tenant_owner_id, publish_slot_id),
    )
    return cur.rowcount


# ══════════════════════════════════════════════════════════════════════════
# command
# ══════════════════════════════════════════════════════════════════════════
def live_command(cur, *, publish_slot_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"SELECT {', '.join(COMMAND_COLUMNS)} FROM {COMMAND_TABLE} "
        f"WHERE publish_slot_id = %s AND command_state = ANY(%s) AND {LIVE_EXCLUSION_SQL} "
        f"ORDER BY created_at DESC LIMIT 1",
        (publish_slot_id, list(LIVE_COMMAND_STATES)),
    )
    return _one(cur)


def latest_command(cur, *, publish_slot_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"SELECT {', '.join(COMMAND_COLUMNS)} FROM {COMMAND_TABLE} "
        f"WHERE publish_slot_id = %s ORDER BY command_generation DESC, created_at DESC LIMIT 1",
        (publish_slot_id,),
    )
    return _one(cur)


def get_command(cur, *, publish_command_id: str, tenant_owner_id: int) -> dict[str, Any] | None:
    cur.execute(
        f"SELECT {', '.join(COMMAND_COLUMNS)} FROM {COMMAND_TABLE} "
        f"WHERE publish_command_id = %s AND tenant_owner_id = %s",
        (publish_command_id, tenant_owner_id),
    )
    return _one(cur)


def get_command_any_tenant(cur, *, publish_command_id: str) -> dict[str, Any] | None:
    """**只给 admin / reconciler 用**。服务商面一律走 :func:`get_command`。"""
    cur.execute(
        f"SELECT {', '.join(COMMAND_COLUMNS)} FROM {COMMAND_TABLE} "
        f"WHERE publish_command_id = %s",
        (publish_command_id,),
    )
    return _one(cur)


def insert_command(cur, values: Mapping[str, Any]) -> dict[str, Any]:
    """写一条 command。列名从 :data:`COMMAND_COLUMNS` 取,**不接受表外键**。"""
    writable = [c for c in COMMAND_COLUMNS
                if c not in ("created_at", "updated_at", "settled_at",
                             "settlement_attempts", "last_settlement_error")]
    unknown = set(values) - set(writable)
    if unknown:
        raise StoreError(f"insert_command 收到表外字段 {sorted(unknown)}")
    cols = [c for c in writable if c in values]
    placeholders = ", ".join(["%s"] * len(cols))
    try:
        cur.execute(
            f"INSERT INTO {COMMAND_TABLE} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"RETURNING {', '.join(COMMAND_COLUMNS)}",
            tuple(values[c] for c in cols),
        )
    except psycopg2.errors.UniqueViolation as exc:       # type: ignore[attr-defined]
        raise StoreError(
            f"同 slot 已有非终态 command 或已履约(约束 {exc.diag.constraint_name})—— "
            "换 HTTP key/snapshot/revision 都不能顺序二发二扣(MED-20)"
        ) from exc
    row = _one(cur)
    if row is None:                                      # pragma: no cover
        raise StoreError("insert_command 没有返回行")
    return row


def bump_status(
    cur,
    *,
    publish_command_id: str,
    expect_status_version: int | None = None,
    **updates: Any,
) -> dict[str, Any] | None:
    """耐久状态变化 → ``status_version`` **原子递增**(§15.7 逐字)。

    ``expect_status_version`` 非空时做 CAS —— reconciler 与 worker 并发收敛
    只能有一个赢。
    """
    allowed = {
        "command_state", "canonical_publication_state", "funding_state",
        "raw_state_source_table", "raw_state_source_column", "raw_state_value",
        "url_verification_state", "url_availability_state", "public_url",
        "external_start_at", "external_start_token", "provider_call_count",
        "legal_rule_id", "legal_rule_version", "legal_passage_ref", "legal_passage_excerpt",
        "replacement_policy_ref", "status_reason", "freeze_id", "freeze_backend",
        "organization_charge_ref", "platform_cost_ref",
        "provider_order_ref", "provider_last_polled_at",
    }
    # 🔴 [R4-②] ``settled_at`` **不在** allowed 里 —— 它只能由 :func:`mark_settled` 写。
    #    R3 引入它当结算终态标记时,这里还留着通用直写的口子:虽然当时零调用,
    #    但通用路径**没有"至多一次"保护**(``mark_settled`` 有 ``WHERE settled_at IS NULL``),
    #    任何人日后顺手 ``bump_status(settled_at=...)`` 就能把已结算的时间戳改掉,
    #    而"已结算"正是 ④⑦ 候选集与 Z-1 队列**共同**依赖的那个事实。
    #    门开着 = 迟早有人走进来。结构锁 ``test_r4_02``:全包只有 mark_settled 写它。
    unknown = set(updates) - allowed
    if unknown:
        raise StoreError(f"bump_status 不允许改 {sorted(unknown)}")
    if not updates:
        raise StoreError("bump_status 至少要改一列 —— 空更新会白白递增 statusVersion")
    # ══════════════════════════════════════════════════════════════════════
    # 🔴 [包E R2] 平台成本腿的 ``funding_state`` 是**常量**,不是状态
    # ══════════════════════════════════════════════════════════════════════
    # 迁移 044 的 ``chk_defgeo_pcmd_platform_state`` 逐字规定
    #     principal_kind <> 'platform_cost_center' OR funding_state = 'exempt_recorded'
    # 而收敛器/派发器有 6 处会写 ``funding_state``(pending_reconciliation /
    # quarantined / released / committed)。平台腿启用后,其中任何一处碰到平台单
    # 都会**违反 CHECK ⇒ 整个事务当场炸** —— 一条卡住的平台发布会把整轮
    # 收敛(或整轮派发)打死。我在真库上实测过这一发:
    #     ERROR: new row for relation "defgeo_publish_commands"
    #            violates check constraint "chk_defgeo_pcmd_platform_state"
    #
    # 所以规则写在**写这一列的唯一那条语句**里,而不是在 6 个调用点各写一遍
    # (「同一谓词写两处 ⇒ 必有一处没人验」,6 处就是 6 次机会写错)。
    # 谓词与 CHECK 逐字同形,用同一个字面值。
    #
    # 这不是"吞掉调用方的意图":对平台腿来说,「需要人工核验」的正确表达是
    # ``command_state='settlement_pending'``,那一列照写不误 —— 被挡的只有
    # 那个在这条腿上**没有定义**的资金方向。挡掉的事实会落日志,不静默。
    sets_parts: list[str] = []
    params: list[Any] = []
    for _col, _val in updates.items():
        if _col == "funding_state":
            sets_parts.append(
                "funding_state = CASE WHEN principal_kind = %s "
                "THEN funding_state ELSE %s END"
            )
            params.extend([_PLATFORM_PRINCIPAL_KIND, _val])
        else:
            sets_parts.append(f"{_col} = %s")
            params.append(_val)
    sets = ", ".join(sets_parts)
    where = "publish_command_id = %s"
    params.append(publish_command_id)
    if expect_status_version is not None:
        where += " AND status_version = %s"
        params.append(int(expect_status_version))
    cur.execute(
        f"UPDATE {COMMAND_TABLE} SET {sets}, status_version = status_version + 1, "
        f"updated_at = NOW() WHERE {where} RETURNING {', '.join(COMMAND_COLUMNS)}",
        tuple(params),
    )
    fresh = _one(cur)
    if (
        fresh is not None
        and "funding_state" in updates
        and str(fresh.get("funding_state")) != str(updates["funding_state"])
    ):
        # 挡掉了 —— 落日志,不静默(唯一会走到这里的就是平台成本腿)。
        logger.warning(
            "[defgeo-store] %s 是 %s,fundingState 恒为 %s;本次请求的 %r 未写入",
            publish_command_id, fresh.get("principal_kind"),
            fresh.get("funding_state"), updates["funding_state"],
        )
    return fresh


#: 🔴 [P0-4] 结算**终态**的两格资金态。与 §15.7 真值表 commit/release 两行同源。
#:    ``publish_funding._TERMINAL_FUNDING_STATE`` 的 values 必须与它逐字相等
#:    (判据 ``test_b1_*`` 机械对账 —— 同一谓词写两处必有一处没人验)。
SETTLEMENT_TERMINAL_FUNDING_STATES: tuple[str, ...] = ("committed", "released")


def write_settlement_terminal(
    cur, *, publish_command_id: str, funding_state: str,
    command_state: str | None = None, expect_status_version: int | None = None,
) -> dict[str, Any] | None:
    """🔴 [P0-4] 落**结算业务终态**的唯一入口。返回 None = CAS 落空,没写。

    ═══════════════════════════════════════════════════════════════════════
    为什么要有这个名字(而不是让调用点直接 ``bump_status(funding_state=…)``)
    ═══════════════════════════════════════════════════════════════════════
    终审 P0-4 的形态是:7 个结算调用点在 ``commit_exact``/``release_exact``
    之后**无条件**写一句 ``bump_status(funding_state="committed"/"released")``,
    billing 返 ``success=false`` / ``ambiguous`` / 相反幂等时照写不误。

    收成一个**有名字**的函数之后,"谁有资格写终态"就成了一个可以被 AST
    机械枚举的问题:判据锁住它**只有一个调用方**
    (``publish_funding._write_terminal``,即物理结算成功且方向一致之后的
    那一行),同时锁住全包**没有任何地方**再用字面量把这两格写出去。

    SQL 仍然只有 ``bump_status`` 一条(本函数委托给它)—— 平台腿那条 CASE
    的单点收口不受影响。
    """
    if funding_state not in SETTLEMENT_TERMINAL_FUNDING_STATES:
        raise StoreError(
            f"write_settlement_terminal 只接受结算终态 "
            f"{list(SETTLEMENT_TERMINAL_FUNDING_STATES)},实得 {funding_state!r}"
        )
    updates: dict[str, Any] = {"funding_state": funding_state}
    if command_state:
        updates["command_state"] = command_state
    return bump_status(
        cur, publish_command_id=publish_command_id,
        expect_status_version=expect_status_version, **updates,
    )


def mark_settled(cur, *, publish_command_id: str) -> bool:
    """🔴 [R3] 落**结算终态标记**。至多一次(``WHERE settled_at IS NULL``)。

    ═══════════════════════════════════════════════════════════════════
    为什么 R3 之后非有它不可
    ═══════════════════════════════════════════════════════════════════
    收敛器 ④⑦ 的候选集是按 ``funding_state`` 圈的:钱包腿结算后那一列会变
    (frozen → committed/released),于是**自动掉出候选集**。
    但平台成本腿的 ``funding_state`` 是**常量** ``exempt_recorded`` ——
    R3 让它和钱包腿走同一条结算路之后,它**永远掉不出候选集**,
    每一轮收敛都会把它再结算一遍(``commit_freeze`` 幂等,钱不会重复动,
    但动作会无限重复,Z-1 队列与告警也跟着无限重复)。

    所以终态得有一个**与 fundingState 无关**的标记。``settled_at`` 这一列
    迁移 044 建表时就有、全仓**从来没有人写过** —— 它本来就是干这个的。

    落点在**结算原语内部**(``commit_exact`` / ``release_exact``),不在调用点:
    7 个调用点就是 7 次忘记的机会。
    """
    cur.execute(
        f"UPDATE {COMMAND_TABLE} SET settled_at = NOW(), "
        f"status_version = status_version + 1, updated_at = NOW() "
        f"WHERE publish_command_id = %s AND settled_at IS NULL",
        (publish_command_id,),
    )
    return cur.rowcount > 0


def bump_settlement_attempt(
    cur, *, publish_command_id: str, error: str, increment: bool = True,
) -> int:
    """🔴 [B-1] 结算重试计数的**单写点**(诊断链 ``_bump_attempts_retry`` 同构)。

    与 :func:`mark_settled` 同一个理由放在这里:``settlement_attempts`` /
    ``last_settlement_error`` 两列不进 :func:`bump_status` 的 ``allowed``,
    所以全包只有本函数能写它们 —— 谁顺手在别处 ``+1`` 一下,
    「commit 重试到第几次该转人工」这个判断就有了第二份真相。

    返回**递增之后**的次数(调用方拿它比上限)。行不存在时返 0。

    ``increment=False`` 只记原因、不动计数 —— 转人工那一手要留下"为什么",
    但它**不是又一次重试**:算进去会让「第几次转人工」这个数字比真实多一。
    """
    step = 1 if increment else 0
    cur.execute(
        f"""
        UPDATE {COMMAND_TABLE}
           SET settlement_attempts = settlement_attempts + %s,
               last_settlement_error = %s,
               status_version = status_version + 1, updated_at = NOW()
         WHERE publish_command_id = %s
        RETURNING settlement_attempts
        """,
        (step, str(error)[:480], publish_command_id),
    )
    row = _one(cur)
    return int((row or {}).get("settlement_attempts") or 0)


#: 允许外发的**资金态** —— 钱还握在我们手里的那两种。
#: ``exempt_recorded`` 必须在内:平台腿由 ``chk_defgeo_pcmd_platform_state``
#: 强制从建单起就是它(实测生产形态 platform_cost_center 恒 exempt_recorded)。
DISPATCHABLE_FUNDING_STATES: tuple[str, ...] = ("frozen", "exempt_recorded")
#: 明确判**不可**外发的资金态。与上一组的并集必须等于库 CHECK 的整个域 ——
#: 判据机械对账,新增一个状态就必须在这里表态,不许悄悄落进默认那一侧。
NON_DISPATCHABLE_FUNDING_STATES: tuple[str, ...] = (
    "committed", "released", "pending_reconciliation", "quarantined")

#: 允许外发的**命令态**。实测确认路径落 ``queued``(列默认);
#: ``accepted``/``running`` 本链未写入,但语义同属"在飞未收尾",一并允许。
DISPATCHABLE_COMMAND_STATES: tuple[str, ...] = ("accepted", "queued", "running")
#: 明确判**不可**外发的命令态。``needs_action`` 是收敛器⑦ release 之后摆的那个
#: —— 它不是终局命令态,正因如此旧 worker 才能一路走到外调,是 P0-F1 的载体。
NON_DISPATCHABLE_COMMAND_STATES: tuple[str, ...] = (
    "settlement_pending", "needs_action", "completed", "failed",
    "cancelled", "quarantined")


#: [P2-5 · Codex 三审] 派发路径认的那一种 outbox event。
#: 🔴 今天只有这一种;`mark_external_start` 的 fencing 按 (command, kind) 认行,
#:    而 `db/migration_044:588 UNIQUE (publish_command_id, event_kind)` 让这一对
#:    唯一确定一行 —— 于是"绑 kind"与"绑 outbox_id"等价。
#:    加第二种 kind 的人必须显式把它穿进来:判据锁着这个集合,加了就红。
DISPATCH_EVENT_KIND = "publish_command_created"
KNOWN_OUTBOX_EVENT_KINDS = frozenset({DISPATCH_EVENT_KIND})


def mark_external_start(
    cur, *, publish_command_id: str, token: str, claim_token: str,
    event_kind: str = DISPATCH_EVENT_KIND,
) -> bool:
    """§12.3:worker 外调前写 canonical external-start marker。

    ═══════════════════════════════════════════════════════════════════════
    🔴 [E1-1 = Codex 二审 P0-F1] 这一条 UPDATE 是「能不能外发」的**唯一裁决点**
    ═══════════════════════════════════════════════════════════════════════
    改之前 WHERE 只有 ``publish_command_id = ? AND external_start_at IS NULL``。
    于是这条时序可达(Codex 双连接 PG16 已复现,不是推演):

      t0  worker A 读到一份**还没结算**的 command mapping,暂停;
      t1  A 的租约过期,worker B / 收敛器⑦ 完成 release ——
          钱退了,``settled_at`` 落库,commandState 摆成 ``needs_action``;
      t2  A 用**手里那份旧 mapping** 恢复。``dispatch_once`` 开头那道
          "已结算不外发" 读的正是这份旧 mapping,所以放行;
      t3  A 写 marker **成功**(WHERE 不看 settled_at、不看租约)→ 真调 provider。

      观测:``funding_after=released`` 且 ``provider_calls=1`` ——
      **钱已经退了,货还是发出去了。**

    根因是「判定依据」与「写入时刻」之间隔着一个可被抢跑的窗口:
    读的是**事务开始时的快照**,写的是**现在**。所以修法不是再加一句 if ——
    多加几句 if 只是把窗口挪短,窗口还在。修法是把三件事塞进**同一条 UPDATE 的
    WHERE**,让数据库在写这一行的那一刻用**服务端当下的事实**一次性裁决:

      ① ``EXISTS(outbox 仍被我这次租约持有)`` —— fencing。租约过期被别人重领,
         或者这条已经被收尾,我就不是有权外发的那个人了;
      ② ``settled_at IS NULL``           —— 钱还没按任何方向收过尾;
      ③ 资金态 / 命令态在**允许外发**的集合里 —— 纵深,且让"退款后 needs_action"
         这个 P0-F1 的载体状态显式出局。

    退款与 external-start 于是变成**同一行上的竞争**:退款先到 ⇒ 这条 UPDATE
    0 行 ⇒ 一次 provider 都不发;marker 先到 ⇒ 退款侧看到 marker 走核验路径。
    只可能有一方成功。

    🔴 ``claim_token`` **必填、无默认值**:与 ``settle_outbox`` 同一条先例 ——
       有默认值就等于给"忘了传"留了一条静默绕过整道闸的路。

    返回 False 的含义因此从"已经有 marker"扩展为"**此刻不允许外发**"
    (已有 marker / 已结算 / 租约不在我手里 / 状态不允许)。
    调用方一律按"不外调"处理 —— 三种都不该盲目重传。
    """
    if not claim_token:
        raise StoreError(
            "mark_external_start 必须带 claim token —— 无租约凭据的外发就是"
            "「退款后旧 worker 仍可发货」那个窗口本身")
    cur.execute(
        f"""
        UPDATE {COMMAND_TABLE} c
           SET external_start_at = NOW(), external_start_token = %s,
               provider_call_count = c.provider_call_count + 1,
               status_version = c.status_version + 1, updated_at = NOW()
         WHERE c.publish_command_id = %s
           AND c.external_start_at IS NULL
           AND c.settled_at IS NULL
           AND c.funding_state = ANY(%s)
           AND c.command_state = ANY(%s)
           AND EXISTS (
                 SELECT 1 FROM {OUTBOX_TABLE} o
                  WHERE o.publish_command_id = c.publish_command_id
                    AND o.status = 'claimed'
                    AND o.claim_token = %s
                    -- 🔴 [P2-5] 绑到**确切那一行**:接第二种 event 之后,
                    --    同一条 command 会有多行 outbox,"任意一行被我领着"
                    --    就能授权外发 = fencing 认错行。
                    AND o.event_kind = %s)
        """,
        (token, publish_command_id,
         list(DISPATCHABLE_FUNDING_STATES), list(DISPATCHABLE_COMMAND_STATES),
         str(claim_token), str(event_kind)),
    )
    return cur.rowcount == 1


def command_by_provider_order_ref(
    cur, *, provider_order_ref: str,
) -> dict[str, Any] | None:
    """[B-5] 这个上游单号现在绑在哪条命令上?没绑返回 None。

    🔴 ``provider_order_ref IS NOT NULL`` 显式写出来:051 的部分唯一索引
       就是按这个谓词建的,查询谓词与索引谓词同形,不给"NULL 也算撞"留位置。
    """
    if not provider_order_ref:
        return None
    cur.execute(
        f"SELECT {', '.join(COMMAND_COLUMNS)} FROM {COMMAND_TABLE} "
        f"WHERE provider_order_ref IS NOT NULL AND provider_order_ref = %s LIMIT 1",
        (str(provider_order_ref),),
    )
    return _one(cur)


def live_child(cur, *, parent_command_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"SELECT {', '.join(COMMAND_COLUMNS)} FROM {COMMAND_TABLE} "
        f"WHERE parent_command_id = %s AND command_state = ANY(%s) "
        f"AND {LIVE_EXCLUSION_SQL} LIMIT 1",
        (parent_command_id, list(LIVE_COMMAND_STATES)),
    )
    return _one(cur)


# ══════════════════════════════════════════════════════════════════════════
# outbox
# ══════════════════════════════════════════════════════════════════════════
def enqueue_outbox(
    cur, *, publish_command_id: str, publish_slot_id: str,
    event_kind: str = "publish_command_created",
) -> bool:
    """fail-closed 入队。返回 True=本次真插;False=已存在(幂等重放)。

    🔴 与 043 的 activation outbox 同口径:**不吞异常、不开 SAVEPOINT**。
       事实丢了 reconciler 能不能重建?能(从 command 表反查无 outbox 的行),
       但**能重建 ≠ 可以静默失败** —— 失败伪装成成功形状会让调用方继续提交
       一个「钱冻了但没人会去发」的事务。
    """
    cur.execute(
        f"""
        INSERT INTO {OUTBOX_TABLE} (publish_command_id, publish_slot_id, event_kind)
        VALUES (%s, %s, %s)
        ON CONFLICT (publish_command_id, event_kind) DO NOTHING
        RETURNING id
        """,
        (publish_command_id, publish_slot_id, event_kind),
    )
    return _one(cur) is not None


def outbox_rows(cur, *, publish_command_id: str) -> list[dict[str, Any]]:
    cur.execute(
        f"SELECT id, publish_command_id, publish_slot_id, event_kind, status, "
        f"attempt_count, available_at, claimed_at, claim_token, last_error, occurred_at "
        f"FROM {OUTBOX_TABLE} WHERE publish_command_id = %s ORDER BY id",
        (publish_command_id,),
    )
    return _rows(cur)


def claim_outbox(cur, *, claim_token: str, limit: int = 10) -> list[dict[str, Any]]:
    """租约式领取。``FOR UPDATE SKIP LOCKED`` —— 多 worker 不抢同一条。

    ═══════════════════════════════════════════════════════════════════════
    🔴 [B-2 = Codex P1-1] 返回 **fencing token**,收尾必须带着它回来
    ═══════════════════════════════════════════════════════════════════════
    原来 ``RETURNING`` 里没有 ``claim_token``,``settle_outbox`` / ``defer_outbox``
    也只按 ``status = 'claimed'`` 收尾。于是这条时序是可达的:

      t0  worker A 领走 outbox#7(租约 300s),开始外调,卡住;
      t1  租约过期,worker B 重新领走 #7(``claim_token`` 换成 B 的);
      t2  A 醒过来 ``settle_outbox(#7, 'dispatched')`` —— 状态仍是 ``claimed``,
          **A 的收尾把 B 的租约盖掉**,B 那一次派发从队列上消失。

    与收敛器组合之后这就是「先退款、后外发」的那个窗口:⑦ 见到
    ``o.status = 'needs_review'`` 会 release,而一个还活着的旧 worker
    仍握着这条命令往外发。

    fencing token 让"迟到的收尾"**0 行**即放弃:谓词从"还是 claimed 吗"
    变成"**还是我**领的那一次吗"。
    """
    cur.execute(
        f"""
        UPDATE {OUTBOX_TABLE} o
           SET status = 'claimed', claimed_at = NOW(), claim_token = %s,
               attempt_count = o.attempt_count + 1,
               available_at = NOW() + INTERVAL '300 seconds'
         WHERE o.id IN (
               SELECT id FROM {OUTBOX_TABLE}
                WHERE status IN ('pending','claimed') AND available_at <= NOW()
                ORDER BY available_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s)
        RETURNING o.id, o.publish_command_id, o.publish_slot_id, o.event_kind,
                  o.status, o.attempt_count, o.claim_token
        """,
        (claim_token, int(limit)),
    )
    return _rows(cur)


#: outbox 的终局状态。与迁移 044 的 ``chk_defgeo_pout_status`` **同源**;
#: 判据从这里取分母,不手抄(手抄漏掉的那一个不会让任何判据变红)。
OUTBOX_TERMINAL_STATUSES: tuple[str, ...] = ("dispatched", "failed", "needs_review")
OUTBOX_STATUSES: tuple[str, ...] = ("pending", "claimed", *OUTBOX_TERMINAL_STATUSES)


def settle_outbox(
    cur, *, outbox_id: int, claim_token: str, status: str,
    last_error: str | None = None,
) -> bool:
    """把**自己领的**那一条 outbox 收成终局。返回 True = 本次真的改了这一行。

    🔴 [B-2] ``claim_token`` 是**必填**的(没有默认值)—— 结构上不可能忘。
       ``WHERE status = 'claimed' AND claim_token = %s`` 两个谓词各挡一半:
       前者挡"已经收过尾了",后者挡"这已经不是我那一次租约了"。
       只有前者时,租约过期被重投的命令会被迟到的旧 worker 盖掉进度。
    """
    if status not in OUTBOX_TERMINAL_STATUSES:
        raise StoreError(
            f"settle_outbox 只接受终局态 {list(OUTBOX_TERMINAL_STATUSES)},实得 {status!r}"
        )
    if not claim_token:
        raise StoreError("settle_outbox 必须带 claim token —— 无租约凭据的收尾就是覆盖别人")
    cur.execute(
        f"""
        UPDATE {OUTBOX_TABLE}
           SET status = %s, last_error = %s, observed_at = NOW(),
               claim_token = NULL
         WHERE id = %s AND status = 'claimed' AND claim_token = %s
        """,
        (status, (last_error or None), int(outbox_id), str(claim_token)),
    )
    return cur.rowcount == 1


def defer_outbox(
    cur, *, outbox_id: int, claim_token: str, seconds: int, last_error: str,
) -> bool:
    """外发通道没就绪之类的**可重试**失败:把租约放回去、记原因、延后再来。

    🔴 不改 ``attempt_count``(``claim_outbox`` 已经加过一次),也不置终局 ——
       「这次没轮到」与「这条坏了」是两件事,压成一件会让通道恢复后
       所有排队命令永远躺在 failed 里。
    🔴 [B-2] 与 :func:`settle_outbox` 同一道 fencing:过期租约放回去的那一手
       会把新持有者的 ``available_at`` 推后 —— 那是**倒退**,不是让位。
    """
    if not claim_token:
        raise StoreError("defer_outbox 必须带 claim token —— 无租约凭据的延后就是覆盖别人")
    cur.execute(
        f"""
        UPDATE {OUTBOX_TABLE}
           SET status = 'pending', claim_token = NULL, last_error = %s,
               available_at = NOW() + (%s || ' seconds')::INTERVAL,
               observed_at = NOW()
         WHERE id = %s AND status = 'claimed' AND claim_token = %s
        """,
        (last_error[:2000], int(seconds), int(outbox_id), str(claim_token)),
    )
    return cur.rowcount == 1


def commands_awaiting_outcome(cur, *, limit: int = 50) -> list[dict[str, Any]]:
    """已外调、还没拿到终态的命令(§12.3 窗口③→④ 之间的那一段)。

    ⚠️ 跨租户 —— 只给 worker/reconciler 用,不许直接挂端点。
    """
    cur.execute(
        f"SELECT {', '.join(COMMAND_COLUMNS)} FROM {COMMAND_TABLE} "
        f"WHERE external_start_at IS NOT NULL "
        f"  AND canonical_publication_state IN "
        f"      ('queued','submitting','reported_success_unverified') "
        f"ORDER BY provider_last_polled_at ASC NULLS FIRST LIMIT %s",
        (int(limit),),
    )
    return _rows(cur)


# ══════════════════════════════════════════════════════════════════════════
# Z-1 核验留痕
# ══════════════════════════════════════════════════════════════════════════
def insert_review_entry(
    cur,
    *,
    publish_command_id: str,
    entry_kind: str,
    actor_user_id: int,
    actor_role: str,
    funding_state_before: str,
    funding_state_after: str | None = None,
    reason: str | None = None,
    evidence_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cur.execute(
        f"""
        INSERT INTO {REVIEW_TABLE}
            (publish_command_id, entry_kind, actor_user_id, actor_role, reason,
             evidence_payload, funding_state_before, funding_state_after)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        RETURNING id, publish_command_id, entry_kind, actor_user_id, actor_role,
                  reason, evidence_payload, funding_state_before, funding_state_after, created_at
        """,
        (publish_command_id, entry_kind, int(actor_user_id), actor_role, reason,
         json.dumps(evidence_payload or {}, ensure_ascii=False),
         funding_state_before, funding_state_after),
    )
    row = _one(cur)
    if row is None:                                      # pragma: no cover
        raise StoreError("insert_review_entry 没有返回行")
    return row


def review_entries(cur, *, publish_command_id: str) -> list[dict[str, Any]]:
    cur.execute(
        f"SELECT id, publish_command_id, entry_kind, actor_user_id, actor_role, reason, "
        f"evidence_payload, funding_state_before, funding_state_after, created_at "
        f"FROM {REVIEW_TABLE} WHERE publish_command_id = %s ORDER BY created_at DESC, id DESC",
        (publish_command_id,),
    )
    return _rows(cur)


def review_queue(cur, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Z-1 队列 = commands 上的**投影**,不另存一份状态。

    ⚠️ 本查询跨租户 —— 只能挂在平台 admin 端点后面。
    """
    cur.execute(
        f"""
        SELECT c.publish_command_id, c.publish_slot_id, c.tenant_owner_id, c.brand_id,
               c.funding_policy, c.principal_kind, c.exact_settlement_points,
               c.funding_state, c.command_state, c.canonical_publication_state,
               c.external_start_at, c.provider_call_count, c.status_reason,
               c.created_at, c.updated_at,
               EXTRACT(EPOCH FROM (NOW() - c.updated_at))::BIGINT AS pending_seconds,
               (SELECT count(*) FROM {REVIEW_TABLE} r
                 WHERE r.publish_command_id = c.publish_command_id) AS review_entry_count
          FROM {COMMAND_TABLE} c
         WHERE (
                 c.funding_state IN ('pending_reconciliation','quarantined')
              OR (
                 -- 🔴 [R3] 平台成本腿的 fundingState 是**常量**,按它圈永远圈不到。
                 --    它的"待人工核验"落在 commandState 上,且必须**还没结算**。
                 --    谓词与 ``settlement_review.is_queue_member`` 同一个意思;
                 --    判据 ``test_r2_44`` 两边对账,不让它们各飘各的。
                    c.principal_kind = '{_PLATFORM_PRINCIPAL_KIND}'
                AND c.settled_at IS NULL
                AND c.command_state IN ('settlement_pending','quarantined','needs_action')
                 )
               )
         ORDER BY c.updated_at ASC
         LIMIT %s OFFSET %s
        """,
        (int(limit), int(offset)),
    )
    return _rows(cur)


# ══════════════════════════════════════════════════════════════════════════
# provider-private 执行预算
# ══════════════════════════════════════════════════════════════════════════
def get_budget(
    cur, *, tenant_owner_id: int, accepted_snapshot_id: int,
    service_projection_id: str, scope_key: str = "media_publication",
    for_update: bool = False,
) -> dict[str, Any] | None:
    """取该 accepted snapshot 上**最新版**的 provider-private 预算快照。

    ``for_update=True`` 时锁住这一行 —— §3.4「多 item 并发必须锁定同一
    provider budget snapshot/version 或用等价 CAS」。
    """
    cur.execute(
        f"SELECT {', '.join(BUDGET_COLUMNS)} FROM {BUDGET_TABLE} "
        f"WHERE tenant_owner_id = %s AND accepted_snapshot_id = %s "
        f"  AND service_projection_id = %s AND scope_key = %s "
        f"ORDER BY budget_version DESC LIMIT 1" + (" FOR UPDATE" if for_update else ""),
        (tenant_owner_id, accepted_snapshot_id, service_projection_id, scope_key),
    )
    return _one(cur)


def insert_budget(cur, values: Mapping[str, Any]) -> dict[str, Any]:
    writable = [c for c in BUDGET_COLUMNS if c != "created_at"]
    unknown = set(values) - set(writable)
    if unknown:
        raise StoreError(f"insert_budget 收到表外字段 {sorted(unknown)}")
    cols = [c for c in writable if c in values]
    cur.execute(
        f"INSERT INTO {BUDGET_TABLE} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING {', '.join(BUDGET_COLUMNS)}",
        tuple(values[c] for c in cols),
    )
    row = _one(cur)
    if row is None:                                      # pragma: no cover
        raise StoreError("insert_budget 没有返回行")
    return row


def _commit_direction_states() -> tuple[str, ...]:
    """canonical state 里**钱向为扣款**的那些格。从 §15.7 真值表机械算出来。

    🔴 不手抄 ``('verified_published','retracted')``:那张表哪天多一格
       commit 向的态,手抄的清单不会有任何判据变红。
    """
    from services.defensive_geo.publish import publish_settlement as _s   # noqa: PLC0415

    return tuple(
        st for st in _s.CANONICAL_STATES
        if _s.settlement_direction(st) in ("commit", "preserve_historical_commit")
    )


def budget_usage(
    cur, *, execution_budget_snapshot_id: str,
) -> dict[str, int]:
    """现算 reserved / committed。**不存运行值** —— 存两份必有一份陈旧。

    分母 = :func:`budget_contribution_map` 机械枚举的 ``funding_state`` 全集
    (见那张表的注释:平台腿过去恒 0 就是因为分母是手抄的)。

    reserved = 还冻着的(含**未结算的平台腿**);
    committed = 已扣的(含**已按 commit 向收尾的平台腿**);
    released / 已按 release 向收尾的平台腿两边都不算
    (§3.4「明确 release 后才可由 child retry 重新占用该预算」)。
    """
    contribution = budget_contribution_map()
    reserved_states = sorted(k for k, v in contribution.items() if v == "reserved")
    committed_states = sorted(k for k, v in contribution.items() if v == "committed")
    by_settlement = sorted(k for k, v in contribution.items() if v == "by_settlement")
    commit_states = list(_commit_direction_states())

    # 平台腿(``by_settlement``):没收尾 = 还占着;按 commit 向收尾了 = 已扣;
    # 按 release 向收尾了 = 两边都不算。
    reserved_filter = (
        "funding_state = ANY(%(reserved)s) "
        "OR (funding_state = ANY(%(by_settle)s) AND settled_at IS NULL)"
    )
    committed_filter = (
        "funding_state = ANY(%(committed)s) "
        "OR (funding_state = ANY(%(by_settle)s) AND settled_at IS NOT NULL "
        "    AND canonical_publication_state = ANY(%(commit_states)s))"
    )
    cur.execute(
        f"""
        SELECT
          COALESCE(SUM(exact_settlement_points)
                   FILTER (WHERE {reserved_filter}), 0)::BIGINT AS reserved,
          COALESCE(SUM(exact_settlement_points)
                   FILTER (WHERE {committed_filter}), 0)::BIGINT AS committed
          FROM {COMMAND_TABLE}
         WHERE decision_snapshot_id IN (
               SELECT decision_snapshot_id FROM {SNAPSHOT_TABLE}
                WHERE frozen_payload ->> 'executionBudgetSnapshotId' = %(snap)s)
        """,
        {
            "reserved": reserved_states, "committed": committed_states,
            "by_settle": by_settlement, "commit_states": commit_states,
            "snap": execution_budget_snapshot_id,
        },
    )
    row = _one(cur) or {}
    return {
        "reservedPoints": int(row.get("reserved") or 0),
        "committedPoints": int(row.get("committed") or 0),
    }


def census() -> dict[str, Any]:
    return {
        "tables": {
            "slot": SLOT_TABLE, "snapshot": SNAPSHOT_TABLE, "command": COMMAND_TABLE,
            "outbox": OUTBOX_TABLE, "review": REVIEW_TABLE, "budget": BUDGET_TABLE,
        },
        "snapshotColumns": list(SNAPSHOT_COLUMNS),
        "commandColumns": list(COMMAND_COLUMNS),
        "budgetColumns": list(BUDGET_COLUMNS),
        "liveCommandStates": list(LIVE_COMMAND_STATES),
        "terminalCommandStates": list(TERMINAL_COMMAND_STATES),
        # [B-4] 预算分母的机械枚举 —— 判据从这里取,不手抄。
        "fundingStates": list(FUNDING_STATES),
        "budgetContribution": budget_contribution_map(),
        "commitDirectionStates": list(_commit_direction_states()),
    }
