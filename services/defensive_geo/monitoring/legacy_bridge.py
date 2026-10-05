"""现役监测链 → attempt 账本的**接线**。

🔴 为什么接线要单独一个模块
--------------------------
本仓反复记过同一条:**新增「要被调用的函数」而没有接线锁,等于写了个死函数**。
所以这里做三件事,缺一不可:

1. 提供 :func:`capture_before_retry_overwrite` —— 真正被现役代码调用的那一跳;
2. 提供 :func:`ledger_is_available` —— 迁移 046 没上时**静默跳过**
   (MIG-04:「v2 schema/readiness 缺失只关闭 v2 enrollment,legacy 主链继续」);
3. 提供 :func:`call_sites` —— 让判据能**机械枚举**"谁真的调了它",
   而不是靠"我们接了"这句话。判据断言这个集合恰等于预期集合;
   把现役那一跳摘掉,判据立刻红。

🔴 接的是哪一跳,以及为什么是这一跳
----------------------------------
``db/monitoring_db.reserve_monitoring_cell_retry`` 里那条 UPDATE::

    SET state='running', ..., error_code=NULL, error_message=NULL, ...

**执行完,第一次尝试为什么失败就没有了**。所以捕获必须发生在这条 UPDATE
**之前**,而且必须在**同一个事务**里 —— 分开提交的话,进程在两者之间崩掉
会留下"账本记了但重试没发生"或反过来的半套状态。

🔴 fail-open 还是 fail-closed
-----------------------------
这里刻意 **fail-open**:账本写失败**不阻断**重试。
理由是 §13.1「监测老链一行不改语义」—— 一条**观测**账本不该有能力
把用户的重试挡下来。写失败会记 warning 并让 :func:`capture_before_retry_overwrite`
返回 ``False``,判据可以据此发现"账本静默没写"。

这是本包**唯一**一处 fail-open,且它不在任何资金/权限路径上。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from services.defensive_geo.monitoring import attempt_ledger as _ledger

_LOGGER = logging.getLogger("GEO-DefGeo-LegacyBridge")

BRIDGE_VERSION = "defgeo-legacy-bridge-v1"

#: 🔴 现役调用点的**期望全集**。判据用 AST census 从真源码导出实际调用点,
#:    与这张表逐值比对:
#:      · 实际 ⊋ 期望 ⇒ 有人在别处接了一跳,没人复核过;
#:      · 实际 ⊊ 期望 ⇒ 接线被摘掉了(死函数)。
#:    两个方向都必须红 —— 只判一个方向的锁抓不到另一半。
EXPECTED_CALL_SITES: tuple[str, ...] = (
    "db/monitoring_db.py",
)


def ledger_is_available(cur) -> bool:
    """迁移 046 上了没。

    MIG-04:schema 缺失只关闭 v2,legacy 主链继续 ——
    所以这里返回 False 时调用方**什么都不做**,不抛。
    """
    try:
        cur.execute("SELECT to_regclass('public.%s') AS t" % _ledger.TABLE)
        row = cur.fetchone()
        value = row["t"] if isinstance(row, Mapping) else (row[0] if row else None)
        return value is not None
    except Exception:  # pragma: no cover - 探测本身失败也按"没有"处理
        return False


def capture_before_retry_overwrite(cur, cell: Mapping[str, Any]) -> bool:
    """在现役重试把 ``error_code`` NULL 掉**之前**,把这一次尝试落账。

    返回是否真的落了账(False = 迁移未上 / 该格无可记录 / 写失败)。

    🔴 这一跳存在的全部理由:现役
       ``UPDATE monitoring_run_cells SET ... error_code=NULL, error_message=NULL``
       会抹掉"第一次为什么失败"。MON-03 / MON-10 要的就是那条被抹掉的信息。
    """
    if not ledger_is_available(cur):
        return False

    plan_cell_id = str(cell.get("plan_hash") or "")
    error_code = cell.get("error_code")
    if not plan_cell_id or not error_code:
        # 没有 plan 身份、或这一格本来就没有错误可记 —— 不造记录。
        return False

    # ══════════════════════════════════════════════════════════════════
    # [包F ① 2026-08-23] 本跳降级为**纯 backfill**
    # ══════════════════════════════════════════════════════════════════
    # 一期这一跳是账本的**唯一**写入方,所以它必须抢救那条即将被 NULL 掉的
    # 失败。包F 把执行链四点接上之后,``finish_monitoring_cell_error`` 已经
    # 在失败**发生的那一刻**就把它落成 engine_error attempt 了。
    #
    # 🔴 于是这一跳如果照旧无条件插入,同一次真实调用会在账本里留**两行**
    #    (ordinal 1 = 执行链收的口,ordinal 2 = 本跳又抢救一遍)。
    #    ``attemptRecords`` 因此比真实调用数多一 —— 而 MON-11 逐字要求
    #    「attemptRecords 与真实调用数一致」。这不是多余的谨慎:
    #    多算的那一次会让 progressPct 与五卡的 attempt 守恒式同时失真。
    #
    # 🔴 但不能干脆删掉本跳:**上线前**就已经 failed 的存量格在账本里
    #    一行都没有,它们的重试仍然需要这条抢救,否则那次失败永久丢失。
    #
    # 判据在这个分叉的**两边**各打一发:
    #   · 该格已有 attempt 行 ⇒ 本跳零新增(不许双算);
    #   · 该格零 attempt 行(存量) ⇒ 本跳恰新增一行(不许静默丢历史)。
    if _cell_is_already_ledgered(cur, plan_cell_id):
        return False

    # [工单 E3-1 · 2026-08-26 · Codex 二审 P1-F6] 第四个写点。
    #
    # 这里原来写 `int(cell.get("billing_user_id") or 0) or 0` —— 而
    # `monitoring_run_cells` 上**根本没有** billing_user_id 这一列
    # (生产 pg_dump 32 列逐列核对,零命中),所以它恒落 0:
    # 存量失败格的抢救记录全部挂在一个不存在的 0 号租户名下。
    #
    # 谓词与执行链那四个写点**同一个**(run_ledger_bridge._require_tenant_owner),
    # 不在这里抄第二份 —— 抄第二份必有一处没人验(本仓记过)。
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        TenantOwnerUnresolved, _require_tenant_owner,
    )
    try:
        tenant_owner = _require_tenant_owner(
            cell, what="capture_before_retry_overwrite", plan_cell_id=plan_cell_id)
    except TenantOwnerUnresolved:
        return False

    def _write() -> bool:
        ordinal = _ledger.next_ordinal(cur, plan_cell_id=plan_cell_id)
        cur.execute(
            f"""
            INSERT INTO public.{_ledger.TABLE}
                (attempt_id, plan_cell_id, attempt_ordinal, run_authority_id,
                 tenant_owner_user_id, brand_id, monitoring_cell_id,
                 actual_provider, actual_model, actual_surface, actual_search_mode,
                 request_hash, provider_called, terminal_state, error_code,
                 error_message, terminal_at, ledger_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,TRUE,'engine_error',%s,%s,NOW(),%s)
            ON CONFLICT (attempt_id) DO NOTHING
            """,
            (
                # 现役这一跳拿不到 provider/model 逐值(cell 行上没有那几列),
                # 所以身份用 (plan_cell_id, ordinal) 派生 —— 它在本格内唯一,
                # 而唯一性正是 MON-03 需要的那一位。
                _legacy_attempt_id(plan_cell_id, ordinal),
                plan_cell_id, ordinal,
                str(cell.get("task_id") or ""),
                tenant_owner,
                int(cell.get("brand_id") or 0),
                int(cell.get("id")) if cell.get("id") is not None else None,
                str(cell.get("platform") or "unknown"),
                "legacy_unrecorded",
                str(cell.get("platform") or "unknown"),
                "legacy_unrecorded",
                str(cell.get("plan_hash") or ""),
                str(error_code)[:64],
                (str(cell.get("error_message") or "") or None),
                _ledger.LEDGER_VERSION,
            ),
        )
        return True

    # 🔴 [包F ① 2026-08-23] 原来这里是裸 ``try/except`` —— 那**不是** fail-open,
    #    是把调用方的事务打废:PG 里任何一条语句报错都会让整个事务进入
    #    aborted 状态,之后 ``reserve_monitoring_cell_retry`` 后面那条
    #    retry_requests INSERT 与 cell UPDATE **全部**失败。
    #    也就是说一期那句"账本写失败不阻断重试"的承诺是**反的**:
    #    账本一失败,用户的重试就被账本挡下来了。
    #    改走 :func:`run_ledger_bridge.guarded`(SAVEPOINT + ROLLBACK TO),
    #    与执行链四点同一个出口 —— 同一个谓词不写两处。
    from services.defensive_geo.monitoring.run_ledger_bridge import guarded

    return bool(guarded(cur, "capture_before_retry_overwrite", _write))


def _cell_is_already_ledgered(cur, plan_cell_id: str) -> bool:
    """这一格在账本里有记录了没(哪怕一行)。

    有 ⇒ 执行链四点已经在管这一格,本跳不再抢救(否则双算);
    无 ⇒ 存量格,本跳是它唯一的历史来源。

    探测本身也走 SAVEPOINT:表缺失/权限异常时返回 ``True``(保守 = 不写),
    因为"探不出来"时宁可少记一条,也不要冒双算的风险 ——
    双算会让守恒式失真,而失真的分母比缺一行更难发现。
    """
    from services.defensive_geo.monitoring.run_ledger_bridge import guarded

    def probe() -> bool:
        cur.execute(
            f"SELECT 1 FROM public.{_ledger.TABLE} WHERE plan_cell_id=%s LIMIT 1",
            (plan_cell_id,))
        return cur.fetchone() is not None

    out = guarded(cur, "already-ledgered probe", probe)
    return True if out is None else bool(out)


def _legacy_attempt_id(plan_cell_id: str, ordinal: int) -> str:
    """现役桥的 attempt 身份。

    🔴 不走 :func:`lineage.attempt_id` —— 那条公式要 provider/model/revision
       逐值,而现役 cell 行上**没有**这几列。硬塞占位串会得到一个
       "看起来符合 §6.1 但其实是编的"身份,比诚实地另起一个域更糟。
       这里显式用 ``legacy:`` 域,判据据此把"从现役桥来的 attempt"
       与"v2 全字段 attempt"区分开,不会误以为前者具备完整 lineage。
    """
    import hashlib

    digest = hashlib.sha256(
        f"legacy:{plan_cell_id}:{ordinal}".encode("utf-8")).hexdigest()
    return digest


def census() -> dict[str, Any]:
    return {
        "bridgeVersion": BRIDGE_VERSION,
        "expectedCallSites": list(EXPECTED_CALL_SITES),
        "ledgerTable": _ledger.TABLE,
        "failOpen": True,
        "failOpenScope": "observation ledger only (no funding/permission path)",
    }
