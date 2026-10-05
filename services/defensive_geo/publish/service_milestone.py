"""从**现役事实**解析这一单的服务里程碑(§3.1 / ACT-11)。

判据:ACT-11 / ACT-15 / §19 变异 73。

═══════════════════════════════════════════════════════════════════════
🔴 本模块存在的理由:**闸接上了不等于闸是活的**
═══════════════════════════════════════════════════════════════════════
``work_admission.admit`` 的第一道闸是里程碑闸。如果调用方给它传一个
写死的 ``"service_activated"``,这道闸就永远放行 —— 代码里有它、判据里有它、
但它从来没拦过任何东西。本仓记过这种形态:「门禁接了但接线是坏的」。

所以里程碑必须从**库里的真事实**算出来:

  ① ``keyword_selection_sessions.status``(现役会话状态)
     —— 经 ``accepted_snapshot_id + quote_id`` 复合定位(迁移 042 的复合 FK
        保证这一对不会跨 quote 错指);
  ② ``quote_pricing_snapshots.pricing_snapshot``(判 v2 enrollment);
  ③ ``defgeo_activation_outbox``(迁移 043)的 status
     —— ``materialized`` ⇒ 物化已完成;其余非终态 ⇒ 已收款未物化。

然后交给窗B 的 ``commercial_milestones.project`` 投影,**不自己重算状态机**
(重算 = 第二套状态机,§3.1 明令禁止)。

🔴 解析不出来时返回 ``draft`` 而不是抛
--------------------------------------
返回 ``draft`` 的效果是:``work_admission`` 判定 ``service_not_activated``,
给出「查看当前进度」的出口 —— 一个**可解释、有出口**的拒绝。
抛异常的效果是 500,那是死路。§0.5.6 铁律要的是前者。

🔴🔴 每一次读都必须包在 SAVEPOINT 里
------------------------------------
第一版这里是裸 ``try/except Exception``。在 PostgreSQL 里,**语句一报错整个事务
就进入 aborted**;函数「安全地」返回 ``draft`` 之后,调用方
(``_do_confirm`` 里紧接着的预算锁、freeze、INSERT)每一条都变成
``InFailedSqlTransaction`` —— 整条 confirm 受控 500。

也就是说:注释写着「不抛」,实际效果是**把调用方的事务打废**,
比抛还糟(抛至少能一眼看出真因)。这正是本仓 2026-08 记过的
「try/except 包 SQL 无 SAVEPOINT = 打废调用方事务」。

而且这条 except **是被设计成可达的**(schema 漂移、legacy 库缺列都会走到),
所以它不是理论风险。修法:每次读前 ``SAVEPOINT``,失败 ``ROLLBACK TO`` ——
事务回到读之前的干净点,调用方毫发无损。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, NamedTuple

from services.defensive_geo import commercial_milestones as _cm

logger = logging.getLogger("GEO-DefGeoServiceMilestone")

MILESTONE_RESOLVER_VERSION = "defgeo-publish-service-milestone-v1"

#: 解析不出来时的安全默认。**不是** service_activated —— 默认放行是最贵的默认。
SAFE_DEFAULT_MILESTONE = "draft"


class MilestoneFacts(NamedTuple):
    """解析用到的三条现役事实。判据可以直接构造它做纯函数验证。"""

    session_status: str | None
    pricing_snapshot: Mapping[str, Any] | None
    has_durable_activation: bool
    activation_materialized: bool


def resolve(cur, *, accepted_snapshot_id: int, tenant_owner_id: int) -> str:
    """读三条现役事实 → 投影 → 返回里程碑字符串。"""
    facts = read_facts(cur, accepted_snapshot_id=accepted_snapshot_id,
                       tenant_owner_id=tenant_owner_id)
    return project(facts)


def project(facts: MilestoneFacts) -> str:
    """纯函数部分。委托窗B 的投影,**不自己重算状态机**。"""
    view = _cm.project(
        session_status=facts.session_status,
        pricing_snapshot=facts.pricing_snapshot,
        has_durable_activation=facts.has_durable_activation,
        activation_materialized=facts.activation_materialized,
    )
    milestone = getattr(view, "milestone", None)
    if not milestone:
        # NotEnrolled(不是 v2 交付计划)—— 本 façade 只服务 server-enrolled v2,
        # 所以它在这里等价于「还没到能开工的态」。legacy 走它自己的现役 admission,
        # 不经过本模块(§11.1「legacy offensive 不得被这道新 gate 阻断」)。
        return SAFE_DEFAULT_MILESTONE
    return str(milestone)


_SAVEPOINT = "defgeo_milestone_probe"


def _safe_read(cur, label: str, sql: str, params: tuple) -> Any | None:
    """在 SAVEPOINT 保护下读一行。失败 → ``ROLLBACK TO`` 后返回 ``None``。

    🔴 ``ROLLBACK TO SAVEPOINT`` 是这段代码的**全部意义**:
       没有它,一次失败的 SELECT 会让调用方后续每一条语句都
       ``InFailedSqlTransaction``,而本函数还「正常返回」了 —— 失败伪装成成功。
    🔴 ``RELEASE`` 也不能少:同名 savepoint 反复建而不释放会在长事务里堆积。
    """
    try:
        cur.execute(f"SAVEPOINT {_SAVEPOINT}")
    except Exception as exc:                              # noqa: BLE001 - 连接已废
        logger.warning("[defgeo-milestone] 建 savepoint 失败(%s):%s", label, exc)
        return None
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
    except Exception as exc:                              # noqa: BLE001
        logger.warning("[defgeo-milestone] 读%s失败,退回安全默认:%s", label, exc)
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            cur.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        except Exception:                                 # noqa: BLE001 - 事务已不可用
            pass
        return None
    try:
        cur.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    except Exception:                                     # noqa: BLE001
        pass
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    return {d[0]: v for d, v in zip(cur.description, row)}


def read_facts(cur, *, accepted_snapshot_id: int, tenant_owner_id: int) -> MilestoneFacts:
    """从真库读三条事实。**任何一步读不到都退回安全默认**,且不打废调用方事务。"""
    session_status: str | None = None
    pricing_snapshot: Mapping[str, Any] | None = None
    quote_id: int | None = None

    row = _safe_read(
        cur, "会话",
        """
        SELECT s.status AS session_status, s.quote_id
          FROM keyword_selection_sessions s
         WHERE s.customer_confirmed_snapshot_id = %s
         ORDER BY s.id DESC
         LIMIT 1
        """,
        (int(accepted_snapshot_id),),
    )
    if row is not None:
        session_status = row.get("session_status")
        quote_id = row.get("quote_id")

    row = _safe_read(
        cur, "报价快照",
        "SELECT pricing_snapshot FROM quote_pricing_snapshots WHERE id = %s",
        (int(accepted_snapshot_id),),
    )
    if row is not None:
        pricing_snapshot = row.get("pricing_snapshot")

    has_durable = False
    materialized = False
    if quote_id is not None:
        row = _safe_read(
            cur, "activation outbox",
            """
            SELECT status FROM defgeo_activation_outbox
             WHERE accepted_snapshot_id = %s AND quote_id = %s
             LIMIT 1
            """,
            (int(accepted_snapshot_id), int(quote_id)),
        )
        if row is not None:
            has_durable = True
            materialized = str(row.get("status")) == "materialized"

    return MilestoneFacts(session_status, pricing_snapshot, has_durable, materialized)


def census() -> dict[str, Any]:
    return {
        "resolverVersion": MILESTONE_RESOLVER_VERSION,
        "safeDefault": SAFE_DEFAULT_MILESTONE,
        "milestoneOrder": list(_cm.MILESTONE_ORDER),
        "readsFrom": [
            "keyword_selection_sessions.status(经 customer_confirmed_snapshot_id 定位)",
            "quote_pricing_snapshots.pricing_snapshot(判 v2 enrollment)",
            "defgeo_activation_outbox.status(pending/claimed → 已收款未物化;"
            "materialized → 物化完成)",
        ],
    }
