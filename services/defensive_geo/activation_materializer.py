"""activation outbox 的**消费者**(规格 §3.1 / §3.4 / §12.3 覆盖项①)。

═══════════════════════════════════════════════════════════════════════
🔴 为什么这个文件必须存在(census 2026-08-24,排 tests 后逐函数点数)
═══════════════════════════════════════════════════════════════════════
    activation_outbox.enqueue_activation        → 1 个生产调用点(selection_api)
    activation_outbox.mark_materialized         → **0**
    activation_outbox.unmaterialized_roots      → **0**
    activation_outbox.has_external_start        → **0**
    activation_outbox.orphaned_activation_roots → **0**

也就是说:客户确认报价时**入队**了,然后没有任何东西把它取出来。
更要命的是 ``publish/reconciler.coverage()`` 第 ① 项写着

    {"case": "已收款但 activation 未物化",
     "coveredBy": "activation_outbox.orphaned_activation_roots"}

—— 而那个函数零调用点。**声称被别处覆盖,别处根本没人跑**:
census 是绿的,覆盖是假的。本仓记过同一形态(「门禁接了但接线是坏的」)。

本模块把这一项做成真的:领取 → 物化 → 标记,进 cron。

═══════════════════════════════════════════════════════════════════════
🔴 「物化」在 v1 里具体做什么:签一张 provider-private 执行预算快照
═══════════════════════════════════════════════════════════════════════
§3.4 逐字:accepted delivery plan 冻结交付/RMB,**另一张** provider-private
budget snapshot 冻结总执行 points 上限,两者以 accepted snapshot / service
projection 绑定。签发时机 = commercial basis 成立那一刻。

这一步**零 freeze、零钱包动作**(§3.1 / ACT-06/13 逐字:
「commercial_basis_established 不等于已冻结执行算力」)——
本模块因此**不 import 任何计费/钱包模块**,与 ``activation_outbox`` 同规矩。
结构锚判据就打在这条 import 面上。

═══════════════════════════════════════════════════════════════════════
🔴 幂等:重放同一份推导 = 同一个 budget id
═══════════════════════════════════════════════════════════════════════
``execution_budget_policy.derive`` 的 id 由推导内容 hash 派生。
所以"物化两次"撞的是主键,而不是签出第二张预算 ——
承重在 DB 的 PRIMARY KEY 上,不在 Python 的 if 上。
"""

from __future__ import annotations

import logging
from typing import Any

from services.defensive_geo import activation_outbox as _act
from services.defensive_geo import payer_classification as _payer
from services.defensive_geo.publish import execution_budget_policy as _policy

logger = logging.getLogger("GEO-DefGeoActivationMaterializer")

MATERIALIZER_VERSION = "defgeo-activation-materializer-v1"

#: 一次 tick 最多物化几条。小批量 + 高频优于大批量 —— 单条坏掉时波及面小。
BATCH = 20


class _Skip(RuntimeError):
    """这一条**现在**不该物化(不是坏了)。留在队列里等下一轮。"""


class _NeedsHuman(RuntimeError):
    """这一条**永远**自动物化不了,必须人来看。**立刻**落 ``needs_review``。

    🔴 与 :class:`_Skip` 的区别不是程度,是**方向**:
       ``_Skip`` 说的是"等一等会好"(所以留在 pending 里重试);
       本异常说的是"再等一万轮也不会好"(冻结的付款人身份不在,而它
       只能在入队那一刻产生,时间不会把它变出来)。

       用 ``_Skip`` 处置这一类的后果很具体:``_record_error(terminal=False)``
       永远只写回 ``pending``,而 ``_claim_pending`` 在 ``attempt_count``
       撞上 ``MAX_ATTEMPTS`` 之后就不再选中它 —— 于是它既不在运维面上
       (状态还是 pending),也永远不会再被处理。那是**永久搁置**,
       不是转人工。
    """


def _claim_pending(cur, *, limit: int) -> list[dict[str, Any]]:
    """租约式领取。``FOR UPDATE SKIP LOCKED`` —— 蓝绿两色同时跑也不抢同一条。

    ⚠️ 跨租户查询 —— 只给 worker 用,不许直接挂端点。
    """
    cur.execute(
        f"""
        UPDATE {_act.OUTBOX_TABLE} o
           SET status = 'claimed', claimed_at = NOW(),
               attempt_count = o.attempt_count + 1,
               available_at = NOW() + (%s || ' seconds')::INTERVAL
         WHERE o.id IN (
               SELECT id FROM {_act.OUTBOX_TABLE}
                WHERE status IN ('pending', 'claimed')
                  AND available_at <= NOW()
                  AND attempt_count < %s
                ORDER BY available_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s)
        RETURNING o.id, o.accepted_snapshot_id, o.quote_id, o.brand_id,
                  o.tenant_owner_id, o.attempt_count,
                  o.payer_user_id, o.payer_funding_policy, o.payer_principal_kind
        """,
        (_act.LEASE_SECONDS, _act.MAX_ATTEMPTS, int(limit)),
    )
    return _act._rows_as_dicts(cur)                      # noqa: SLF001 —— 同族读工具


#: 🔴 [工单 C-1] 冻结身份的**必需列全集**。判据拿它当分母机械遍历,不手抄 ——
#:    少检一列就等于给"半冻结身份"留一条静默通道。
FROZEN_IDENTITY_COLUMNS: tuple[str, ...] = (
    "tenant_owner_id", "payer_user_id", "payer_funding_policy", "payer_principal_kind",
)


def frozen_identity(row: dict[str, Any]) -> dict[str, Any]:
    """取**入队那一刻冻结**的 tenant 身份与**资金方向判定**。缺任何一格 ⇒ 转人工。

    🔴 [E2-3 · Owner 2026-08-26 拍板「动态」] 这三列冻的是**资金方向**,
       **不是物理付款账号**。具体说:
         · ``payer_user_id`` 存的恒是**租户 owner**(``selection_api`` 写的就是
           ``payer_user_id=int(_tenant_owner)``)—— 平台承担腿也一样;
         · 平台承担腿真正掏钱的是**平台直营服务账号**,而它是
           ``publish_funding._platform_service_user_id()`` 在**冻结那一刻现取**的。
       所以这个字段名对平台腿是会误导人的:它不是"付款人",
       是"确认时刻的租户身份 + 由它判出的资金方向"。
       口径 = **冻结前动态、冻结后钉死**:确认到冻结之间平台账号配置可变,
       新冻结用当时的配置;而已冻结的单,commit/release 一律打冻结行上那个账号
       (``command.payer_user_id`` 链路),永不改道。

    🔴 [工单 C-1 · Codex 终审 P1-6] 这里**没有**现读回落
    -------------------------------------------------
    上一版是这么写的::

        explicit = row.get("tenant_owner_id")
        if explicit:
            return int(explicit)
        SELECT owner_user_id FROM brands WHERE id = %s     # ← 现读

    而入队处从来不传 ``tenant_owner_id``(043 留了列,``selection_api``
    没传)⇒ 显式分支**恒不命中** ⇒ 每次物化都现读 ``brands.owner_user_id``。
    品牌在「客户确认报价」与「物化」之间被转移,同一个 accepted snapshot
    就解析出另一个 tenant、另一个付款方,provider 执行预算签在别人头上。

    现在冻结值由 ``enqueue_activation`` 在确认事务里写死(迁移 052)。
    存量 pending 行没有这几列 ⇒ **转人工**,而不是回落现读:
    回落等于把这个 bug 原样留在存量行上,并且没有任何判据会红。

    🔴 三个 payer 列由 052 的 ``defgeo_activation_outbox_payer_group``
       CHECK 保证同生同死,所以库层不可能出现"只冻了一半"。
       这里仍然逐列检查,是因为 ``tenant_owner_id`` 不在那条 CHECK 里
       (它是 043 的老列,存量行可能单独有值)。
    """
    missing = [c for c in FROZEN_IDENTITY_COLUMNS if not row.get(c)]
    if missing:
        raise _NeedsHuman(
            "这一条 activation 没有冻结的付款人身份(缺 %s)—— 转人工。"
            "不回落现读 brands.owner_user_id:品牌转移后现读会得到另一个付款方,"
            "而 commercial basis 成立那一刻的付款人是事实,不该被之后的转移改写。"
            % ",".join(missing)
        )
    # 🔴 冻结值也要**过一遍同一把尺子**:库里那一行可以被人手改,
    #    而 052 的 CHECK 只保证三列同生同死,不保证取值合法。
    #    分母取 ``payer_classification`` 的闭集,不手抄 —— 判别位加一格时
    #    这里自动跟上;而现场造一个策略名会让 ``derive`` 签出一张
    #    没有任何结算腿认识的预算。
    policy = str(row["payer_funding_policy"])
    if policy not in _payer.CLASSIFIABLE_POLICIES:
        raise _NeedsHuman(
            "冻结的付款方策略 %r 不在判别位闭集 %s 里 —— 转人工"
            % (policy, list(_payer.CLASSIFIABLE_POLICIES))
        )
    expected_kind = _payer.classify_payer(
        is_admin=(policy == "admin_platform_ledger"),
        user_id=None, platform_direct_user_id=None,
    ).principal_kind
    if str(row["payer_principal_kind"]) != expected_kind:
        raise _NeedsHuman(
            "冻结的 principal_kind %r 与策略 %r 对不上(应为 %r)—— 转人工"
            % (row["payer_principal_kind"], policy, expected_kind)
        )
    return {
        "tenant_owner_id": int(row["tenant_owner_id"]),
        "payer_user_id": int(row["payer_user_id"]),
        "payer_funding_policy": str(row["payer_funding_policy"]),
        "payer_principal_kind": str(row["payer_principal_kind"]),
    }


def materialize_one(cur, row: dict[str, Any]) -> dict[str, Any]:
    """物化一条 activation root。**调用方持有事务**。"""
    from services.defensive_geo.publish import store as _store

    identity = frozen_identity(row)
    tenant = identity["tenant_owner_id"]
    accepted = int(row["accepted_snapshot_id"])

    # 🔴 [包E R2 · Owner 已批平台腿启用] 付款方**显式判别**,不吃静默默认。
    #
    #    上一版这里不传 ``funding_policy``,于是吃 ``derive()`` 的默认值
    #    ``personal_wallet`` —— 而诊断链对同一个 admin 判的是
    #    ``admin_platform_ledger``。**同一个人在两条链上被判成两个付款方**:
    #    admin 在发布链会一路走到个人钱包腿,撞上 billing 的 admin 免单旁路
    #    (``freeze_points`` 对 ``is_admin`` 返回零句柄且不写任何表),
    #    被 ``publish_funding`` 那把零记账守卫拦下 → 对外裸 500。
    #
    #    判别位与诊断链**同源**(``payer_classification``),不在这里复写一份。
    #    普通身份走到的仍是 ``personal_wallet`` —— 与不传参数时**逐字节相同**
    #    (``derive`` 的 digest 把 ``fundingPolicy`` 算进 id,判据钉死那个 id)。
    #
    # 🔴 [工单 C-1] 判别**不再在这里现算** —— 读入队那一刻冻结的那一份。
    #    现算的问题不是"算错",是"算的时候身份已经不是当初那个了":
    #    ``classify_user_id`` 现查 ``roles``,租户在确认之后被授予/撤销 admin,
    #    同一个 accepted snapshot 会在两次物化里判出两个付款方。
    #    判别位本身仍然只有 ``payer_classification`` 一处实现 ——
    #    它现在由 ``selection_api`` 在确认事务里调用(见那里的说明)。
    draft = _policy.derive(
        cur, tenant_owner_id=tenant, accepted_snapshot_id=accepted,
        funding_policy=identity["payer_funding_policy"],
        payer_user_id=identity["payer_user_id"],
    )
    existing = _store.get_budget(
        cur, tenant_owner_id=tenant, accepted_snapshot_id=accepted,
        service_projection_id=draft.service_projection_id, scope_key=draft.scope_key,
    )
    created = False
    if existing is None:
        _store.insert_budget(cur, draft.as_store_values())
        created = True
    else:
        # [E2-3 · Codex 二审 P1-F3] 重放必须**核一致**,不许静默复用。
        #
        # `get_budget` 的键是 (tenant, accepted, service_projection_id, scope_key)
        # —— **不含 hash**。所以一份 hash 不同的旧预算照样会被找到:
        # 上一次物化用的政策/身份与这一次不同(比如那期间租户被授予了 admin,
        # 或 derive 的分母改过),这里会拿着旧预算 mark_materialized,
        # 而返回的 executionBudgetSnapshotId 是**新算的那个** —— 两者指向不同的行。
        # 下游按返回值去查预算会查到一份从没落库的快照。
        #
        # 🔴 处置对齐本链一贯口径:**转人工**,既不覆盖旧的也不按新的走。
        #    覆盖 = 把已经签过的预算改掉;按新的走 = 账实不符。
        _drift = [
            name for name, want in (
                ("execution_budget_snapshot_id", draft.execution_budget_snapshot_id),
                ("budget_hash", draft.budget_hash),
                ("funding_policy", draft.funding_policy),
            )
            if str(existing.get(name)) != str(want)
        ]
        if _drift:
            raise _NeedsHuman(
                "重放推导与已存预算不符(%s):已存 %r ≠ 本次 %r · "
                "转人工,既不覆盖已签预算也不按新推导走"
                % (",".join(_drift),
                   {k: existing.get(k) for k in _drift},
                   {"execution_budget_snapshot_id": draft.execution_budget_snapshot_id,
                    "budget_hash": draft.budget_hash,
                    "funding_policy": draft.funding_policy})
            )
    _act.mark_materialized(cur, int(row["id"]))
    logger.info(
        "[defgeo-activation] outbox=%s accepted=%s 物化完成(预算 %s · created=%s · %s)",
        row["id"], accepted, draft.execution_budget_snapshot_id, created, draft.derivation,
    )
    return {
        "outboxId": int(row["id"]),
        "acceptedSnapshotId": accepted,
        "executionBudgetSnapshotId": draft.execution_budget_snapshot_id,
        "created": created,
    }


def materialize_pending(limit: int = BATCH) -> dict[str, Any]:
    """跑一轮。**每条一个事务** —— 一条坏掉不影响其它条。"""
    from db.connection import get_db

    done: list[dict[str, Any]] = []
    skipped = 0
    failed = 0
    needs_review = 0

    with get_db() as conn:
        claimed = _claim_pending(conn.cursor(), limit=int(limit))
    if not claimed:
        return {"materialized": 0, "skipped": 0, "failed": 0,
                "needsReview": 0, "items": []}

    for row in claimed:
        try:
            with get_db() as conn:
                done.append(materialize_one(conn.cursor(), row))
        except _NeedsHuman as exc:
            # 🔴 [工单 C-1] **立刻**转人工,不进重试队列。
            #    走 _record_error(terminal=True) 也不行:那一条要等
            #    attempt_count 撞上 MAX_ATTEMPTS 才落 needs_review,
            #    而在此之前它一直是 pending —— 运维面上看不见,
            #    每 30 秒白跑一次,8 轮之后彻底静默(永久搁置)。
            needs_review += 1
            logger.error("[defgeo-activation] outbox=%s 转人工:%s", row["id"], exc)
            _record_needs_review(row, str(exc))
        except _Skip as exc:
            skipped += 1
            logger.info("[defgeo-activation] outbox=%s 本轮跳过:%s", row["id"], exc)
            _record_error(row, str(exc), terminal=False)
        except Exception as exc:                          # noqa: BLE001
            failed += 1
            logger.error("[defgeo-activation] outbox=%s 物化失败:%s", row["id"], exc,
                         exc_info=True)
            _record_error(row, f"{type(exc).__name__}: {exc}", terminal=True)
    return {"materialized": len(done), "skipped": skipped, "failed": failed,
            "needsReview": needs_review, "items": done}


def _record_needs_review(row: dict[str, Any], message: str) -> None:
    """🔴 [工单 C-1] **立刻**把这一条推到 ``needs_review``。**独立事务**。

    与 :func:`_record_error` 的差别是**时机**不是措辞:那一条要等
    ``attempt_count >= MAX_ATTEMPTS`` 才落 ``needs_review``,在此之前
    状态一直是 ``pending``;而"冻结身份不在"这件事再等一万轮也不会变,
    多等 8 轮只是把它从运维面上多藏 4 分钟。

    ``WHERE status = 'claimed'`` 是 CAS:本轮领到的才由本轮处置。
    """
    from db.connection import get_db

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {_act.OUTBOX_TABLE}
                   SET status = 'needs_review', last_error = %s,
                       claim_token = NULL, claimed_at = NULL
                 WHERE id = %s AND status = 'claimed'
                """,
                (message[:2000], int(row["id"])),
            )
    except Exception as exc:                              # noqa: BLE001
        logger.warning("[defgeo-activation] 转人工回写失败(交下一轮租约超时兜底):%s", exc)


def _record_error(row: dict[str, Any], message: str, *, terminal: bool) -> None:
    """把失败原因写回队列行。**独立事务** —— 上一个事务已经因异常回滚了。

    ``terminal`` 只决定要不要在耗尽重试后落 ``needs_review``:
    落 ``needs_review`` 的那一条会出现在运维面上,而不是安静地循环重试到天荒地老。
    """
    from db.connection import get_db

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {_act.OUTBOX_TABLE}
                   SET status = CASE WHEN %s AND attempt_count >= %s
                                     THEN 'needs_review' ELSE 'pending' END,
                       last_error = %s,
                       available_at = NOW() + (%s || ' seconds')::INTERVAL
                 WHERE id = %s AND status = 'claimed'
                """,
                (bool(terminal), _act.MAX_ATTEMPTS, message[:2000],
                 _act.RETRY_SECONDS, int(row["id"])),
            )
    except Exception as exc:                              # noqa: BLE001
        logger.warning("[defgeo-activation] 回写失败原因也失败了(交下一轮租约超时兜底):%s", exc)


def materialize_pending_sync() -> dict[str, Any]:
    """APScheduler 入口。与 ``run_executor.consume_confirmed_runs_sync`` 同形。

    本函数体里没有 await,但仍然保留 sync 包装:调度器注册的是**这个名字**,
    接线锁钉的也是这个名字;将来物化步骤需要 async 时,改这里一处即可,
    调度侧不用动。
    """
    try:
        return materialize_pending()
    except Exception as exc:                              # noqa: BLE001 —— job 不许把调度线程带走
        logger.error("[defgeo-activation] tick 异常:%s", exc, exc_info=True)
        return {"materialized": 0, "skipped": 0, "failed": 0,
                "needsReview": 0, "items": []}


def census() -> dict[str, Any]:
    return {
        "materializerVersion": MATERIALIZER_VERSION,
        "consumes": _act.OUTBOX_TABLE,
        "produces": "defgeo_provider_execution_budgets",
        "freezesPoints": False,
        "coversReconcilerItem": 1,
        "batch": BATCH,
        # 🔴 [工单 C-1] 机械分母:判据从这里取冻结身份的必需列,不手抄。
        "frozenIdentityColumns": list(FROZEN_IDENTITY_COLUMNS),
        # 可机读断言:本模块**不再**现读 brands.owner_user_id 解析归属。
        "readsLiveBrandOwner": False,
    }

