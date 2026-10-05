"""激活 outbox —— **fail-closed** 入队 + reconciler 重建路径。

规格 §3.1 / §3.2 / §12.3。RFC 三问已批(2026-08-21):准建表、fail-closed 并存获背书、编号 043。
判据 = ACT-06 / ACT-07 / ACT-11 / ACT-13 / ACT-14 / ACT-15。

═══════════════════════════════════════════════════════════════════════
🔴 本仓存在**两套语义相反**的 outbox 入队形态,都正确 —— 裁定 2026-08-21
═══════════════════════════════════════════════════════════════════════

    本模块 :fail-CLOSED —— 入队失败 ⇒ 抛,整个业务事务一起回滚。
    对照组 :``services/article_delivery_plan.py``
             :func:`~services.article_delivery_plan.enqueue_quote_event_in_transaction_if_enabled`
             fail-OPEN —— flag 关或任何异常 ⇒ savepoint 回滚后返回 ``enqueued: False``。

**判别标准(裁定给定)= 事实丢了,reconciler 能不能从别处重建?**

- 文章计划那条:能。``reconcile_authoritative_quotes`` 从 quote/confirmed_keywords
  这些**独立权威源**重算出计划应有的样子,outbox 只是加速器。丢一条不丢事实
  ⇒ 允许 fail-open,不拿旁挂 sidecar 去炸主事务。
- 本模块这条:**也能**(见 :func:`orphaned_activation_roots` —— 从
  ``keyword_selection_sessions.customer_confirmed_snapshot_id`` 反查
  "已确认但没有 activation root"的会话),但**仍然 fail-closed**,理由是第二条:
  fail-open 会把失败**伪装成成功形状**返回给调用方(``{"enqueued": False}``
  是一个正常返回值,不是异常)。资金相邻的 activation 事务如果拿到这种返回并继续
  提交 commercial basis,就产生了 ACT-07 明令禁止的"可见半状态"。
  **能重建 ≠ 可以静默失败**;重建是纵深防御,不是入队可以骗人的许可。

所以两处形态并存的正确读法:**看它是不是资金/身份相邻的主链,不是看它叫不叫 outbox。**
照着近处那台抄之前,先回答"事实丢了能不能重建"和"失败会不会伪装成成功"两个问题。

═══════════════════════════════════════════════════════════════════════

🔴 零资金副作用:本模块**不 import 任何钱包/计费模块**,也不接受 points 参数。
   §3.1「commercial_basis_established 只写耐久 activation event;
   不等于已冻结执行算力」、ACT-06/13「activation 本身新增执行算力 freeze=0」。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

import psycopg2

OUTBOX_TABLE = "defgeo_activation_outbox"

#: 队列状态闭集。与迁移 043 的 CHECK 同源 —— 判据从这里取分母,不手抄。
STATUSES: tuple[str, ...] = (
    "pending", "claimed", "materialized", "failed", "needs_review",
)

#: 重试上限与退避。形态照抄现役 article outbox 已被验证的那一组。
MAX_ATTEMPTS = 8
RETRY_SECONDS = 30
LEASE_SECONDS = 300


class ActivationEnqueueError(RuntimeError):
    """入队失败。**必须**让调用方的业务事务一起失败。

    这个异常存在的全部意义:让 fail-closed 无法被"顺手 try 一下"退化成 fail-open。
    捕获它并继续提交 commercial basis = 制造 ACT-07 的可见半状态。
    """


class ActivationRoot(NamedTuple):
    outbox_id: int
    accepted_snapshot_id: int
    quote_id: int
    #: True = 本次真的插入了;False = 已存在,返回的是原对象(§3.2「重放返回原对象」)。
    created: bool



# ══════════════════════════════════════════════════════════════════════════
# 游标形态适配 —— 🔴 这不是洁癖,是一个真缺陷的修复
# ══════════════════════════════════════════════════════════════════════════
# 本模块原来一律按**位置**取列(``row[0]`` / ``dict(zip(cols, r))``)。
# 那只在 tuple 游标下成立,而生产 ``db.connection.get_connection()`` 用的是
# ``RealDictCursor``(本仓全局约定)。接进 `/s/{token}/confirm-quote` 的真实
# 事务时当场炸 ``KeyError: 0``。
#
# 更危险的是 reconciler 那两处:``dict(zip(cols, r))`` 在 RealDictCursor 下
# 不会抛 —— ``r`` 已经是 dict,``zip`` 会去遍历它的**键**,于是安静地返回一堆
# 值全错的行。对账器不报错、只是给出错误答案,这种缺陷靠"跑通了"永远发现不了。
#
# 之所以一直没暴露:窗B 自己的判据底座用的是 tuple 游标 ——
# **夹具用的游标类型是生产从来不会发的那一种**。
def _first_value(row: Any) -> Any:
    """取结果行第一列,兼容 tuple 游标与 ``RealDictCursor``。"""
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()))
    return row[0]


def _rows_as_dicts(cur) -> list[dict[str, Any]]:
    """把结果集统一成 dict 列表,兼容两种游标形态。"""
    rows = cur.fetchall() or []
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(r) for r in rows]
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def enqueue_activation(
    cur,
    *,
    accepted_snapshot_id: int,
    quote_id: int,
    brand_id: int,
    accepted_snapshot_hash: str,
    event_kind: str = "commercial_basis_established",
    tenant_owner_id: int | None = None,
    actor_user_id: int | None = None,
    actor_membership_id: int | None = None,
    approval_id: int | None = None,
    # 🔴 [E2-3] 恒为**租户 owner**(见 selection_api 的调用点)。它标的是
    #    「谁的单 + 什么资金方向」,**不是**物理付款钱包 —— 平台承担腿的
    #    物理账号在冻结那一刻现取。名字容易误读,故在此明示。
    payer_user_id: int | None = None,
    payer_funding_policy: str | None = None,
    payer_principal_kind: str | None = None,
) -> ActivationRoot:
    """在**调用方的业务事务内**登记一条 activation root。fail-closed。

    调用方持有 commit/rollback。本函数:

    - **不看任何 feature flag**。flag 决定"要不要开这条链",不该决定
      "已经开了的链能不能悄悄丢事实"。
    - **不吞异常**。除了唯一约束冲突(那是幂等,不是失败),其余一律抛
      :class:`ActivationEnqueueError`。
    - **不开 SAVEPOINT**。开了就等于给调用方一个"我失败了但你的事务还好好的"
      的假象 —— 而那正是本模块要禁止的形状。

    并发语义:§3.2「同一 accepted snapshot 至多激活一个 activation root;
    重放返回原对象」。20 并发下第 2..20 个会撞
    ``defgeo_activation_outbox_root_unique``,在这里被翻译成
    ``created=False`` + 原 root —— **承重的是数据库唯一约束,不是这段 Python**。

    🔴 [工单 C-1 · 迁移 052] ``payer_*`` 三件套是**冻结的付款人身份**
    ---------------------------------------------------------------
    commercial basis 成立那一刻的付款人,不该被之后的品牌转移改写。
    在这之前 materializer 是**现读** ``brands.owner_user_id`` 的
    (043 的 ``tenant_owner_id`` 列存在但入队处从不传 ⇒ 显式分支恒不命中),
    于是同一个 accepted snapshot 在品牌转移前后会解析出两个付款人。

    这里只**存**判别结果,不在本模块里判 —— 本模块的结构锁
    (``test_activation_module_imports_no_money``)禁止它 import 任何
    钱包/计费面;判别位在 ``services.defensive_geo.payer_classification``,
    由调用方在业务事务里算好传进来。三列由 052 的
    ``defgeo_activation_outbox_payer_group`` CHECK 保证同生同死。
    """
    if not isinstance(accepted_snapshot_hash, str) or len(accepted_snapshot_hash) != 64:
        raise ActivationEnqueueError(
            f"accepted_snapshot_hash 形态不合法:{accepted_snapshot_hash!r}"
        )
    try:
        cur.execute(
            f"""
            INSERT INTO {OUTBOX_TABLE}
                (accepted_snapshot_id, quote_id, brand_id, event_kind,
                 accepted_snapshot_hash, tenant_owner_id, actor_user_id,
                 actor_membership_id, approval_id, status,
                 payer_user_id, payer_funding_policy, payer_principal_kind)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)
            ON CONFLICT ON CONSTRAINT defgeo_activation_outbox_root_unique
            DO NOTHING
            RETURNING id
            """,
            (accepted_snapshot_id, quote_id, brand_id, event_kind,
             accepted_snapshot_hash, tenant_owner_id, actor_user_id,
             actor_membership_id, approval_id,
             payer_user_id, payer_funding_policy, payer_principal_kind),
        )
        row = cur.fetchone()
        if row is not None:
            return ActivationRoot(
                outbox_id=int(_first_value(row)),
                accepted_snapshot_id=accepted_snapshot_id,
                quote_id=quote_id,
                created=True,
            )
        # ON CONFLICT DO NOTHING ⇒ 已存在。回读原对象(重放返回原对象)。
        cur.execute(
            f"SELECT id FROM {OUTBOX_TABLE}"
            f" WHERE accepted_snapshot_id=%s AND quote_id=%s",
            (accepted_snapshot_id, quote_id),
        )
        existing = cur.fetchone()
        if existing is None:                    # pragma: no cover - 理论不可达
            raise ActivationEnqueueError(
                "唯一约束报冲突但回读不到原行 —— 约束与查询口径不一致,拒绝继续。"
            )
        return ActivationRoot(
            outbox_id=int(_first_value(existing)),
            accepted_snapshot_id=accepted_snapshot_id,
            quote_id=quote_id,
            created=False,
        )
    except ActivationEnqueueError:
        raise
    except psycopg2.Error as exc:
        # 🔴 这里**只把异常换个名字往上抛**,不做任何补救。
        #    任何形式的"吞掉并返回 False"都会让本模块退化成 fail-open。
        raise ActivationEnqueueError(
            f"activation 入队失败,业务事务必须一起回滚:{type(exc).__name__}: {exc}"
        ) from exc


def mark_external_start(cur, outbox_id: int, marker: str) -> None:
    """§12.3 kill window 3:外调**之前**写 canonical marker。

    marker 一旦写下永不清除 —— 清除等于把"我可能已经外调过"这条事实抹掉,
    恢复时就会盲目二次外调。这里用 ``WHERE external_start_marker IS NULL``
    保证只写第一次,并发下也只有一个 winner。
    """
    if not marker:
        raise ActivationEnqueueError("external-start marker 不得为空")
    cur.execute(
        f"UPDATE {OUTBOX_TABLE} SET external_start_marker=%s, external_start_at=NOW()"
        f" WHERE id=%s AND external_start_marker IS NULL",
        (marker, outbox_id),
    )


def has_external_start(cur, outbox_id: int) -> bool:
    """恢复时的必查项:任一 marker 存在均不得盲目二次外调。"""
    cur.execute(
        f"SELECT external_start_marker IS NOT NULL FROM {OUTBOX_TABLE} WHERE id=%s",
        (outbox_id,),
    )
    row = cur.fetchone()
    return bool(row is not None and _first_value(row))


def mark_materialized(cur, outbox_id: int) -> bool:
    """物化完成 ⇒ 终态。只有 pending/claimed 能进 materialized。

    返回是否**本次**完成 —— 已经是终态时返回 False,让重放不重复物化。
    """
    cur.execute(
        f"UPDATE {OUTBOX_TABLE} SET status='materialized', materialized_at=NOW(),"
        f" claim_token=NULL, claimed_at=NULL"
        f" WHERE id=%s AND status IN ('pending','claimed')",
        (outbox_id,),
    )
    return cur.rowcount == 1


def orphaned_activation_roots(cur, limit: int = 100) -> list[dict[str, Any]]:
    """🔴 裁定判据的正面兑现:**事实丢了,从别处重建**。

    扫「客户已确认(``customer_confirmed_snapshot_id`` 非空)但没有 activation
    root」的会话 —— 即使 043 那一行因任何原因没写成,这条查询也能把它找回来。

    这证明 activation 事实**是可重建的**(与 article 计划同级),
    因此 fail-closed 不是因为"丢了就没救",而是因为
    "失败不许伪装成成功"(见模块 docstring)。两者是独立的两条理由。
    """
    # 🔴 [工单 V3-C · C-2] 取**受理证据全集**,不只取三件组。
    #    分母来自 ``acceptance_evidence.REQUIRED_EVIDENCE_COLUMNS``,机械展开 ——
    #    手抄一份会漏列,而漏掉的那一列不会让任何判据变红(本仓记过)。
    from services.defensive_geo import acceptance_evidence as _ae

    _cols = ", ".join(f"s.{c}" for c in _ae.REQUIRED_EVIDENCE_COLUMNS)
    cur.execute(
        f"""
        SELECT s.id AS session_id, s.quote_id, s.brand_id, {_cols}
          FROM keyword_selection_sessions s
          LEFT JOIN {OUTBOX_TABLE} o
                 ON o.accepted_snapshot_id = s.customer_confirmed_snapshot_id
                AND o.quote_id = s.quote_id
         WHERE s.customer_confirmed_snapshot_id IS NOT NULL
           AND o.id IS NULL
         ORDER BY s.id
         LIMIT %s
        """,
        (limit,),
    )
    rows = _rows_as_dicts(cur)
    # 🔴 重建 activation 事实之前,先说清楚**这一行的受理证据够不够**。
    #    ``legacy_unproven`` 的行也会出现在这里(存量确认行有指针、没审计列),
    #    照着它重建 activation root = 拿一份不可复核的受理去激活服务。
    #    这里不替运维做决定(不静默跳过、也不静默激活),而是把档位和缺哪几列
    #    一起交出去 —— 处置是人的裁定,可机读是我们的义务。
    for r in rows:
        r["acceptance_evidence"] = _ae.classify_acceptance(r)
        r["missing_evidence_columns"] = list(_ae.missing_evidence_columns(r))
    return rows


def unmaterialized_roots(cur, limit: int = 100) -> list[dict[str, Any]]:
    """§12.3 reconciler 覆盖项之一:「已收款但 activation 未物化」。"""
    cur.execute(
        f"SELECT id, accepted_snapshot_id, quote_id, status, attempt_count,"
        f" external_start_marker IS NOT NULL AS externally_started"
        f" FROM {OUTBOX_TABLE} WHERE materialized_at IS NULL"
        f" ORDER BY id LIMIT %s",
        (limit,),
    )
    return _rows_as_dicts(cur)
