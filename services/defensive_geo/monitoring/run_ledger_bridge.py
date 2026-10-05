"""现役监测**执行链** → attempt 账本的接线(包F ① · Codex P1-B 的本体)。

🔴 一期为什么是死函数,以及这一层修的是哪一格
------------------------------------------------
一期把 :mod:`attempt_ledger` 的三个函数(``open_attempt`` /
``close_attempt`` / ``record_policy_skip``)写完了,**全仓零调用者**。
一期只接了 :mod:`legacy_bridge` 那**一跳**(重试覆盖前的抢救),
所以账本里只可能出现"重试过的失败格",正常跑完的格一条都没有。
于是 ``progressPct`` 恒 0、五卡 summary 恒空 —— 这就是 P1-B。

本模块接的是执行链的**四点**(外加一个一期没点到的第五点):

======  ==================================  ====================================
点      现役位置(唯一 chokepoint)          账本动作
======  ==================================  ====================================
①      ``claim_monitoring_run_cell``        ``open`` (在飞)
②      ``save_monitoring_result``           ``close`` answered / entity_ambiguous
③      ``finish_monitoring_cell_error``     ``close`` engine_error
④      ``create_monitoring_run_cells``      ``record_policy_skip``(未购平台)
⑤      人工身份确认(``pending_identity``    追加一条 answered attempt
        → ``succeeded``)                    (不改写 ② 那一行)
======  ==================================  ====================================

🔴 为什么接在 ``db/monitoring_db.py`` 而不是四个调用方
-----------------------------------------------------
``claim_monitoring_run_cell`` / ``finish_monitoring_cell_error`` 各有
**四个**生产调用方(``api/monitoring_api.py`` 两处 / ``api/scheduler.py`` /
``scheduler.py``)。接在调用方 = 同一个谓词写四处,本仓记过
「同一谓词写两处 ⇒ 必有一处没人验」。接在 db 层那一个函数体内,
四条路径自动全覆盖,且判据只需要证明**一个** chokepoint 被接上。

🔴 fail-soft 的真实形态 = SAVEPOINT,不是 try/except
---------------------------------------------------
一期 ``legacy_bridge.capture_before_retry_overwrite`` 写的是::

    try:
        cur.execute("INSERT INTO ...")
    except Exception:
        _LOGGER.warning(...); return False

**这不是 fail-soft,这是把调用方的事务打废。** PostgreSQL 里任何一条语句
报错(唯一约束冲突 / CHECK 冲突)都会让整个事务进入 aborted 状态,
之后每一条语句都返回 ``current transaction is aborted``。
于是"账本写失败不阻断用户重试"这句承诺是**反的**:账本一失败,
``reserve_monitoring_cell_retry`` 后面那条 INSERT + UPDATE 全部失败,
用户的重试被账本挡下来了。本仓记过这一条
(「try/except 包 SQL 无 SAVEPOINT = 打废调用方事务」)。

所以本模块每一次写都包在 ``SAVEPOINT`` 里,失败就 ``ROLLBACK TO SAVEPOINT``
——现役 ``save_monitoring_result`` 的 ``sp_token_usage`` 就是同一套路,
不另造第二种哲学。:func:`guarded` 是唯一出口,判据用 AST 证明
本模块**没有**任何一条裸 ``cur.execute`` 走在它外面。

🔴 崩在中途的在飞 attempt(一期审计点名的那个雷)
------------------------------------------------
``uq_defgeo_attempt_single_inflight`` 是 partial unique:一格同时只能有
一条 ``terminal_state IS NULL``。进程在 ``open`` 与 ``close`` 之间崩掉,
那一行就**永久**占着这个位子,该格从此再也 open 不进第二个 attempt。

解法是**两个**函数,论据不同(不是同一个条件放宽两次):

* :func:`reclaim_superseded` —— 在 :func:`open_for_claim` 里调。调用方
  **已经赢下这一格的 claim CAS**,所以"旧的在飞 attempt 已作废"是逻辑必然,
  不看 cell 状态、不等租约。这一条让占位在**它真正会伤人的那一点上**
  不可能发生,且不依赖别的函数先被调过;
* :func:`reclaim_orphans` —— 在现役崩溃恢复 chokepoint
  (``_recover_expired_monitoring_cells``)里调。这里问的是"它还在飞吗",
  所以必须去查**权威**:现役 ``monitoring_run_cells.state`` 才是
  "有没有 provider 调用在飞"的authority。一条在飞 attempt 若它的 cell
  已经不是 ``running``,那它**证明**不在飞了;只有 cell 行查不到时
  才回落到租约超时。

收口一律写 ``engine_error``,error_code 逐字沿用现役自己的两个恢复码
(``provider_outcome_unknown`` / ``worker_lost_before_dispatch``)——
账本的说法与现役 cell 的说法必须是同一个故事。

🔴 为什么收口成 ``engine_error`` 而不是丢掉
------------------------------------------
MON-02 逐字「401、429、超时不映射 absent」。一次崩掉的调用对客户就是
"我们没拿到回答",不是"AI 没提到你"。而 §6.1 canonical selection 里
``answered`` 排在 ``engine_error`` 之前 —— 后续重试成功时,卡上显示的
仍然是 answered,这一条崩掉的记录只作为诊断证据留着(MON-10)。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Mapping, Optional, Sequence

from services.defensive_geo.monitoring import attempt_ledger as _ledger

_LOGGER = logging.getLogger("GEO-DefGeo-RunLedger")

BRIDGE_VERSION = "defgeo-run-ledger-bridge-v1"

#: 🔴 现役调用点的**期望全集**。判据用 AST census 从真源码导出实际调用点,
#:    与这张表逐值比对,**两个方向都红**:
#:      · 实际 ⊋ 期望 ⇒ 有人在别处接了一跳,没人复核过;
#:      · 实际 ⊊ 期望 ⇒ 接线被摘掉了(死函数复发)。
EXPECTED_CALL_SITES: tuple[str, ...] = (
    "db/monitoring_db.py",
)

#: 本模块对外的五个接线函数 + 一个恢复函数。判据拿它当**分母**机械遍历,
#: 不手抄:少接一个点,census 立刻少一格。
WIRED_ENTRYPOINTS: tuple[str, ...] = (
    "open_for_claim",
    "close_for_result",
    "close_for_error",
    "record_skip_for_plan",
    "close_for_human_resolution",
    "reclaim_superseded",
    "reclaim_orphans",
    # [工单 C-3] ⑧ queued 格未形成 attempt 就被收走时的账本闭合。
    "close_unattempted_for_cells",
)

#: 现役 cell 租约上限是 1800s(``claim_monitoring_run_cell`` 的
#: ``min(int(lease_seconds), 1800)``)。账本租约必须**严格大于**它 ——
#: 否则账本会在现役还认为这一格在跑的时候先把 attempt 收掉,
#: 造出"账本说错了、cell 说在跑"的对不上账。
LIVE_CELL_MAX_LEASE_SECONDS = 1800
ATTEMPT_LEASE_SECONDS = LIVE_CELL_MAX_LEASE_SECONDS + 600

#: 现役自己的两个崩溃恢复码(``_recover_expired_monitoring_cells`` 逐字)。
#: 账本沿用它们,不另造一套词 —— 两套词会让同一件事在两张表里叫两个名字。
RECLAIM_CODE_DISPATCHED = "provider_outcome_unknown"
RECLAIM_CODE_NOT_DISPATCHED = "worker_lost_before_dispatch"
#: cell 行已经查不到(极端:任务被清理)时的回落码。
RECLAIM_CODE_LEASE_EXPIRED = "attempt_lease_expired"
#: 本格赢下了一个**新的** claim ⇒ 旧的在飞 attempt 按定义已作废。
#: 与上面两个码分开:那两个说的是"我们不知道结果",这一个说的是
#: "我们知道它没收口,因为已经有人重新开跑了"。
RECLAIM_CODE_SUPERSEDED = "attempt_superseded_by_new_claim"

#: ④ 未购平台的 skip 理由码。逐字沿用现役 ``create_monitoring_run_cells``
#: 写进 cell 的 ``error_code``,不另起名字。
POLICY_SKIP_REASON = "not_in_purchased_run_plan"

#: ⑧ [工单 C-3 · Codex 终审 P1-10] queued 格在**形成 attempt 之前**被收走时的理由码。
#:
#: 逐字沿用现役自己写进 cell 的那两个码,不另起名字 ——
#: 账本的说法与现役 cell 的说法必须是同一个故事(与 ⑥⑦ 的 reclaim 码同规矩):
#:   · ``recover_abandoned_monitoring_task_execution`` 对未派发格写
#:     ``worker_lost_before_dispatch``;
#:   · ``revoke_monitoring_task_coverage_for_organization_refund`` 写
#:     ``fulfillment_refunded``。
UNATTEMPTED_ABANDONED_REASON = "worker_lost_before_dispatch"
UNATTEMPTED_REFUNDED_REASON = "fulfillment_refunded"

#: 判据拿它当分母机械遍历:每一个"把 queued 格打到终态"的现役 chokepoint
#: 都必须配一个理由码,少一个就等于少一条闭合路径。
UNATTEMPTED_REASONS: tuple[str, ...] = (
    UNATTEMPTED_ABANDONED_REASON, UNATTEMPTED_REFUNDED_REASON,
)

#: ⑤ 人工身份确认那一跳的 provider 位。它**没有** provider 调用,
#: 所以不能借用平台名 —— 借用会让 attemptRecords 看起来多了一次真实调用。
HUMAN_RESOLUTION_PROVIDER = "__human_identity_review__"


class _LedgerUnavailable(RuntimeError):
    """迁移 046 没上。调用方**什么都不做**,不抛给现役链。"""


# ══════════════════════════════════════════════════════════════════════
# fail-soft 唯一出口
# ══════════════════════════════════════════════════════════════════════

#: SAVEPOINT 名字是**固定常量**,不拼任何入参 —— 拼入参就是 SQL 注入面。
_SAVEPOINT = "sp_defgeo_run_ledger"


def never_raises(fn: Callable[..., Any]) -> Callable[..., Any]:
    """公共接线函数的**外层**保险 —— 任何异常都不许逃到现役链。

    🔴 为什么 SAVEPOINT 不够(本包判据亲自抓到的洞):
       ``guarded`` 护的是**事务完整性**(某条 SQL 炸了不打废调用方事务),
       它护不住**控制流**。``open_for_claim`` 的函数体里除了那条 INSERT,
       还有 ``ledger_is_available`` / ``reclaim_superseded`` /
       ``_planned_lineage`` 三个调用 —— 其中任何一个抛非 SQL 异常
       (``_PLATFORM_CONTRACT`` 结构变了导致 KeyError、cell 里某个键类型
       意外导致 ``int()`` ValueError……)都会原样穿出去,
       把 ``claim_monitoring_run_cell`` 一起带崩。
       那时候用户看到的是"启动监测失败",而真因是一条**观测**账本。

       两层是**纵深**不是重复,判据分别拆:
       · ``test_ledger_failure_never_blocks_the_live_chain`` 打这一层;
       · ``test_every_bridge_write_goes_through_the_savepoint_guard`` 打那一层。

    🔴 返回 ``None`` / 不改变任何现役状态就是这里的正确行为:
       §13.1「监测老链一行不改语义」—— 一条观测账本不该有能力
       让用户的监测跑不起来。
    """
    import functools

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            _LOGGER.warning(
                "defgeo 账本 %s 整体异常(**不阻断**现役监测):%r",
                getattr(fn, "__name__", "?"), exc)
            return None

    _wrapped.__wrapped_by_never_raises__ = True     # 判据据此机械核对
    return _wrapped


def guarded(cur, what: str, fn: Callable[[], Any]) -> Any:
    """在 SAVEPOINT 里跑 ``fn``。失败只记 warning,**调用方事务完好**。

    🔴 返回 ``None`` 有两种含义(调用方都不需要区分):账本不可用、或写失败。
       两者都是"这一跳没落账",而现役链照常往下走。

    🔴 ``RELEASE`` 也包在 try 里:如果连 SAVEPOINT 都建不出来
       (调用方事务早已 aborted),我们**不能**再发任何语句去搅它。
    """
    try:
        cur.execute(f"SAVEPOINT {_SAVEPOINT}")
    except Exception as exc:  # 调用方事务已废 —— 不是我们造成的,也不去碰
        _LOGGER.warning("defgeo 账本 %s:SAVEPOINT 建不出来,跳过(%s)", what, exc)
        return None
    try:
        out = fn()
    except Exception as exc:
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            cur.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        except Exception:  # pragma: no cover - 回滚都失败时无事可做
            pass
        _LOGGER.warning(
            "defgeo 账本 %s 落账失败(**不阻断**现役监测):%s", what, exc)
        return None
    try:
        cur.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    except Exception:  # pragma: no cover
        pass
    return out


def ledger_is_available(cur) -> bool:
    """迁移 046 上了没。MIG-04:schema 缺失只关闭 v2,legacy 主链继续。"""
    def probe():
        cur.execute("SELECT to_regclass(%s) AS t", (f"public.{_ledger.TABLE}",))
        row = cur.fetchone()
        value = row["t"] if isinstance(row, Mapping) else (row[0] if row else None)
        return value is not None

    return bool(guarded(cur, "availability probe", probe))


# ══════════════════════════════════════════════════════════════════════
# 租户归属 —— 唯一谓词(工单 E3-1 · Codex 二审 P1-F6)
# ══════════════════════════════════════════════════════════════════════

#: 账本上冻结租户归属的**唯一**列名。判据拿它当分母去核六个写点,
#: 不手抄 —— 手抄的那一份漏掉谁都不会让任何判据变红(本仓记过)。
TENANT_OWNER_CELL_COLUMN = "tenant_owner_user_id"


class TenantOwnerUnresolved(RuntimeError):
    """这一格的租户归属取不到。

    🔴 唯一正确的处置是**不写这一行账**并告警,不是编一个。
       本单之前六个写点写的是 ``... or 0``:0 号用户不存在,于是整条运行
       在账本里挂在一个不存在的人名下,而任何判据都不会红。
       缺一行账是**看得见**的(五卡守恒当场判"数据坏了");
       挂在假租户名下是**看不见**的 —— 后者贵得多。
    """


#: 进程内计数:本进程拒绝落账过多少次。由 :func:`census` 对外暴露。
#:
#: 🔴 为什么用进程计数而不是**在这里**写 AI-Ops 告警 —— 这是被判据抓回来的:
#:    第一版在这里 ``from db.ai_ops_db import upsert_alert``。
#:    ``tests/defensive_geo_w4_2026_08_22::test_bridge_import_closure_touches_no_db_module``
#:    当场判红,报出新增的 db 边 ``db.ai_ops_db`` / ``db.connection``。
#:    那条锁守的是 2026-08-10 那次**自死锁**:在事务里惰性 import 一个会触发
#:    ``init_db`` 的模块,整条链卡死 16 分钟。
#:    ——「告警写库」这件事本身没错,错在**位置**:它不能发生在别人的事务里。
#:    所以这里只留日志 + 计数;把计数拉成告警是**扫描器**的活(见交付文
#:    "未完成"一节:本单没有加那个 sweeper,api/scheduler.py 是慎改区)。
_TENANT_UNRESOLVED_COUNT = {"total": 0}


def _alert_tenant_unresolved(*, what: str, plan_cell_id: str,
                             cell_id: Any, brand_id: Any, raw: Any) -> None:
    """fail loud:ERROR 日志 + 进程计数。**不在这里碰库**。"""
    _TENANT_UNRESOLVED_COUNT["total"] += 1
    _LOGGER.error(
        "[defgeo-ledger] %s 租户归属取不到(raw=%r · plan_cell=%s · cell=%s · brand=%s)"
        " —— **不落账、不编租户**,转人工;本进程累计 %d 次",
        what, raw, plan_cell_id[:16], cell_id, brand_id,
        _TENANT_UNRESOLVED_COUNT["total"])


def _require_tenant_owner(cell: Mapping[str, Any], *, what: str,
                          plan_cell_id: str = "") -> int:
    """从格上**冻结**的那一列取租户归属。取不到就抛,绝不回落。

    🔴 唯一真相来源 = ``monitoring_run_cells.tenant_owner_user_id``(迁移 054)。
       它在建格那一刻从 ``brands.owner_user_id`` 冻下来,之后品牌被转移也不改 ——
       "这次运行是谁的"在运行开始时就已经确定,不该由之后的转移改写。

    🔴 **不**回落读 ``brands.owner_user_id``:回落等于把 P1-F6 的第二半
       (⑤ ``close_unattempted_for_cells`` 现读可变列)原样留着,
       而且没有任何判据会红 —— 因为回落路径在正常数据下永远拿得到一个数。

    🔴 **不**回落读 ``monitoring_keyword_settlements.billing_user_id``:
       那一列在 ``admin_covered`` 场景恒 NULL(平台垫付),而租户仍然是品牌
       所有者 —— 拿"谁付钱"回答"这是谁的运行"是把两个身份混成一个
       (本仓「关系链条绑定」铁律:身份只决定进哪个钱包)。
       两处 SSOT 的一致性由判据交叉核验,不由代码回落。
    """
    raw = cell.get(TENANT_OWNER_CELL_COLUMN)
    try:
        owner = int(raw)
    except (TypeError, ValueError):
        owner = 0
    if owner > 0:
        return owner
    _alert_tenant_unresolved(
        what=what, plan_cell_id=str(plan_cell_id or ""),
        cell_id=cell.get("id"), brand_id=cell.get("brand_id"), raw=raw)
    raise TenantOwnerUnresolved(
        f"{what}: {TENANT_OWNER_CELL_COLUMN}={raw!r}")


# ══════════════════════════════════════════════════════════════════════
# 身份与 lineage 取值
# ══════════════════════════════════════════════════════════════════════

def _cell_attempt_id(plan_cell_id: str, ordinal: int) -> str:
    """本格第 N 次尝试的身份。

    🔴 **不**走 :func:`lineage.attempt_id`(§6.1 公式)。理由与一期
       ``legacy_bridge._legacy_attempt_id`` 完全相同,且这里更硬:
       §6.1 公式要 ``actual_model_revision``,而 revision 只有**拿到回答之后**
       才知道 —— 而 ``open`` 必须发生在**派发之前**(不然崩在中途的调用整条消失)。
       两个要求不可能同时满足。硬塞一个占位 revision 会得到一个
       "看起来符合 §6.1 其实是编的"身份。

       所以这里显式用 ``cell:`` 域:身份只承诺**本格内唯一**,不承诺携带
       完整 lineage。判据 ``test_live_attempt_id_is_not_the_spec_formula``
       钉住这一点,免得日后有人把它当成 §6.1 id 去做 observation_cell_id。

       真正的 lineage 在 :func:`close_for_result` 落进 ``actual_*`` 列 ——
       那几列不参与身份,所以"收口时才知道"不影响任何唯一性。
    """
    return hashlib.sha256(
        f"cell:{plan_cell_id}:{int(ordinal)}".encode("utf-8")).hexdigest()


def _human_attempt_id(plan_cell_id: str, ordinal: int) -> str:
    """⑤ 人工确认那一跳的身份。独立域 —— 它不是一次 provider 调用。"""
    return hashlib.sha256(
        f"human:{plan_cell_id}:{int(ordinal)}".encode("utf-8")).hexdigest()


def _planned_lineage(platform: str, cell: Mapping[str, Any]) -> dict[str, Any]:
    """派发前**已知**的 lineage 位,取自被测对象自己的 SSOT。

    🔴 ``provider`` / ``model`` / ``surface`` 逐值读
       ``services.monitoring_lineage._PLATFORM_CONTRACT`` —— 那是现役监测
       决定"这一格发给谁、发哪个模型"的同一张表。**只读不改**
       (包F 红线:被测对象的引擎模型值一个字不动)。

       在这里读它而不是抄一份,是为了让"模型换了而账本还记着旧名"
       这件事**不可能发生**:两边同源。判据
       ``test_planned_model_tracks_the_platform_contract`` 反向锁住
       (抄一份就会红)。
    """
    # 🔴 从**零依赖**的 engine_contract 取,不从 monitoring_lineage ——
    #    后者引 question_evolution → db.connection,而本函数跑在事务里。
    from services.engine_contract import PLATFORM_CONTRACT as _PLATFORM_CONTRACT

    contract = _PLATFORM_CONTRACT.get(str(platform)) or {}
    entitlement = cell.get("entitlement_snapshot")
    search_mode = ""
    if isinstance(entitlement, Mapping):
        search_mode = str(entitlement.get("search_mode") or "")
    return {
        # 平台不在合同表里时诚实落 unknown,不猜一个 —— 猜出来的 provider
        # 会让"有个平台在跑而我们不知道是谁"变得看不见。
        "actual_provider": str(contract.get("provider") or "unknown"),
        "actual_model": str(contract.get("model") or "unknown"),
        "actual_surface": str(contract.get("surface") or "unknown"),
        "actual_search_mode": search_mode or "unknown",
    }


def _plan_cell_id_of(cell: Mapping[str, Any]) -> str:
    return str(cell.get("plan_hash") or "")


# ══════════════════════════════════════════════════════════════════════
# ① 首轮/重试派发前登记
# ══════════════════════════════════════════════════════════════════════

@never_raises
def open_for_claim(cur, cell: Mapping[str, Any]) -> str | None:
    """现役 cell 被 claim 成 ``running`` 之后、provider 调用之前登记一条在飞 attempt。

    返回 ``attempt_id``,或 ``None``(账本不可用 / 没有 plan 身份 / 写失败)。

    🔴 先 :func:`reclaim_orphans` 再 open:上一次进程崩在中途留下的在飞行
       会被 ``uq_defgeo_attempt_single_inflight`` 拦住第二次 open。
       不先收口,那一格从此永久 open 不进来 —— 一期审计点名的正是这个雷。
    """
    plan_cell_id = _plan_cell_id_of(cell)
    if not plan_cell_id:
        return None
    if not ledger_is_available(cur):
        return None

    # 🔴 **不**在这里调 :func:`reclaim_orphans`。它判"已不在飞"靠
    #    ``cell.state <> 'running'``,而调用方**刚刚**把这一格 claim 成
    #    ``running`` —— 于是那个条件恒 false,一条都收不到。
    #    (写成 reclaim_orphans 会是一个"看起来接了防护、实际恒空转"的调用,
    #     比不写更糟:它让人以为这个雷已经排掉了。)
    #
    #    这里要的是另一条**更强**的论据:调用方已经赢下了这一格的 claim CAS,
    #    所以它是当前唯一的所有者;此刻还存在的在飞 attempt 按定义属于
    #    一次已经死掉的尝试。:func:`reclaim_superseded` 据此无条件收口,
    #    不看 cell 状态、不等租约 —— 这样"崩在中途永久占位"在**它真正会
    #    伤人的那一点上**结构性地不可能发生,而不依赖别的函数先被调过。
    reclaim_superseded(cur, plan_cell_id=plan_cell_id)

    # [工单 E3-1] ①②(首轮 claim 与重试臂共用本函数)的租户归属。
    # 在 SAVEPOINT **之前**解析:取不到就整跳不发生,不留半条账。
    try:
        tenant_owner = _require_tenant_owner(
            cell, what="open_for_claim", plan_cell_id=plan_cell_id)
    except TenantOwnerUnresolved:
        return None

    lineage = _planned_lineage(str(cell.get("platform") or ""), cell)

    def write():
        ordinal = _ledger.next_ordinal(cur, plan_cell_id=plan_cell_id)
        aid = _cell_attempt_id(plan_cell_id, ordinal)
        cur.execute(
            f"""
            INSERT INTO public.{_ledger.TABLE}
                (attempt_id, plan_cell_id, attempt_ordinal, parent_attempt_id,
                 run_authority_id, tenant_owner_user_id, brand_id,
                 monitoring_cell_id, actual_provider, actual_model,
                 actual_model_revision, actual_surface, actual_search_mode,
                 request_hash, provider_called, ledger_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,TRUE,%s)
            ON CONFLICT (attempt_id) DO NOTHING
            """,
            (aid, plan_cell_id, ordinal,
             # 第 2 次及以后的尝试挂到上一条 attempt 上(MON-03 的 child attempt)。
             (_cell_attempt_id(plan_cell_id, ordinal - 1) if ordinal > 1 else None),
             str(cell.get("task_id") or ""),
             tenant_owner,
             int(cell.get("brand_id") or 0),
             (int(cell["id"]) if cell.get("id") is not None else None),
             lineage["actual_provider"], lineage["actual_model"],
             lineage["actual_surface"], lineage["actual_search_mode"],
             plan_cell_id, _ledger.LEDGER_VERSION),
        )
        return aid

    return guarded(cur, "open_for_claim", write)


# ══════════════════════════════════════════════════════════════════════
# ② 成功收口(answered / entity_ambiguous)
# ══════════════════════════════════════════════════════════════════════

@never_raises
def close_for_result(
    cur, *, plan_cell_id: str, cell_state: str, monitoring_result_id: int,
    observed_provider: str | None = None,
    observed_model: str | None = None,
    observed_model_revision: str | None = None,
    observed_surface: str | None = None,
    observed_search_mode: str | None = None,
) -> bool:
    """现役 cell 收成 ``succeeded`` / ``pending_identity`` 时收口在飞 attempt。

    映射**逐值**对应,不是"差不多":

    ==========================  ==========================
    现役 ``monitoring_run_cells``  账本 ``terminal_state``
    ==========================  ==========================
    ``succeeded``                ``answered``
    ``pending_identity``         ``entity_ambiguous``
    ==========================  ==========================

    🔴 ``pending_identity`` → ``entity_ambiguous`` 而**不是** answered:
       §6.3 逐字「实体同名无法判断进入 entity_ambiguous……不能被压成未提及」,
       而它同样不能被抬成 answered —— 那会让"AI 说的可能不是你"这件事
       在客户卡上消失。

    🔴 ``observed_*`` 是收口时才知道的真 lineage(尤其 ``model_revision``)。
       它们**不参与** attempt 身份,所以此处写入不改变任何唯一性;
       而不写的话账本永远只有"计划用哪个模型",拿不到"实际是哪个版本"。
    """
    state_map = {"succeeded": "answered", "pending_identity": "entity_ambiguous"}
    terminal = state_map.get(str(cell_state))
    if terminal is None or not plan_cell_id:
        return False
    if not ledger_is_available(cur):
        return False

    # 🔴 现役 ``build_monitoring_lineage`` 在平台不给版本时填的是字符串
    #    ``"unknown"``(并另置 ``model_revision_unknown_reason``),
    #    而迁移 046 的语义是**平台不给版本 ⇒ NULL**(``NULL 与空串/占位串不同义``)。
    #    照抄 "unknown" 会让"平台没给"与"版本就叫 unknown"在库里长得一样,
    #    于后续保真对账里分不开。所以在这里折成 NULL。
    if observed_model_revision is not None and \
            str(observed_model_revision).strip().lower() in ("", "unknown"):
        observed_model_revision = None

    def write():
        return _ledger.close_attempt(
            cur,
            attempt_id=_inflight_attempt_id(cur, plan_cell_id),
            terminal_state=terminal,
            monitoring_result_id=int(monitoring_result_id),
            observed_provider=observed_provider,
            observed_model=observed_model,
            observed_model_revision=observed_model_revision,
            observed_surface=observed_surface,
            observed_search_mode=observed_search_mode,
        )

    return bool(guarded(cur, "close_for_result", write))


# ══════════════════════════════════════════════════════════════════════
# ③ 失败收口(engine_error)
# ══════════════════════════════════════════════════════════════════════

@never_raises
def close_for_error(
    cur, *, plan_cell_id: str, error_code: str, error_message: str = "",
) -> bool:
    """现役 cell 收成 ``failed`` / ``unavailable`` /
    ``pending_provider_confirmation`` 时收口。

    三种现役错误态**全部**落 ``engine_error`` —— 它们的共同事实是
    "这一次我们没拿到可用回答"。MON-02 逐字禁止把它们映射成 absent。
    ``pending_provider_confirmation``(请求已发、结果未确认)同样是
    engine_error:对客户而言"我们不知道"就是"这次没有结论",
    而它与"AI 没提到你"是两件事。
    """
    if not plan_cell_id:
        return False
    if not ledger_is_available(cur):
        return False

    code = str(error_code or "platform_unavailable")[:64]

    def write():
        return _ledger.close_attempt(
            cur,
            attempt_id=_inflight_attempt_id(cur, plan_cell_id),
            terminal_state="engine_error",
            error_code=code,
            error_message=(str(error_message or "")[:2000] or None),
        )

    return bool(guarded(cur, "close_for_error", write))


# ══════════════════════════════════════════════════════════════════════
# ④ policy skip(未购平台)
# ══════════════════════════════════════════════════════════════════════

@never_raises
def record_skip_for_plan(cur, cell: Mapping[str, Any]) -> str | None:
    """建格时把**未计划**的格(未购平台)记成 ``policy_skipped``。

    🔴 为什么这一格必须进账本:MON-11 逐字「skipped 无 provider attempt、
       **计 terminal 不计 attempted**」。不记它,五卡 summary 的
       ``terminal`` 永远到不了 ``planned`` ——
       :func:`projection.assert_terminal_conservation` 于是永远判"数据坏了",
       而真相是"这几个平台客户没买"。

    🔴 它**不影响**进度端点:那条链按 ``is_planned = TRUE`` 取格
       (一期 P1-C),未购平台不在进度分母里。两个分母不同是有意的:
       进度回答"我买的这些做到哪了",五卡回答"这次一共看了哪些格"。
    """
    if bool(cell.get("is_planned")):
        return None
    plan_cell_id = _plan_cell_id_of(cell)
    if not plan_cell_id:
        return None
    if not ledger_is_available(cur):
        return None

    # [工单 E3-1] ③ 未购平台 skip 的租户归属。与 ① 同一个谓词、同一列。
    try:
        tenant_owner = _require_tenant_owner(
            cell, what="record_skip_for_plan", plan_cell_id=plan_cell_id)
    except TenantOwnerUnresolved:
        return None

    def write():
        return _ledger.record_policy_skip(
            cur,
            plan_cell_id=plan_cell_id,
            run_authority_id=str(cell.get("task_id") or ""),
            tenant_owner_user_id=tenant_owner,
            brand_id=int(cell.get("brand_id") or 0),
            reason_code=str(cell.get("error_code") or POLICY_SKIP_REASON),
        )

    return guarded(cur, "record_skip_for_plan", write)


# ══════════════════════════════════════════════════════════════════════
# ⑤ 人工身份确认(一期四点之外的第五点)
# ══════════════════════════════════════════════════════════════════════

@never_raises
def close_for_human_resolution(
    cur, *, plan_cell_id: str, monitoring_result_id: int, actor_user_id: int,
    brand_id: int, run_authority_id: str, tenant_owner_user_id: int | None,
) -> str | None:
    """人工把 ``pending_identity`` 确认成 ``succeeded`` 时,**追加**一条 answered。

    🔴 为什么是追加而不是改写:② 已经把那条 attempt 收成
       ``entity_ambiguous``,而终态行由 ``trg_defgeo_attempt_terminal_immutable``
       结构性锁死(MON-03「不覆盖原始失败」)。人工确认是一条**新事实**,
       不是"上一条判错了要改" —— 原始那条身份待定的观测必须原样留着。

    🔴 追加之后卡上显示什么:§6.1 canonical selection 里 ``answered``
       的 rank(0)优于 ``entity_ambiguous``(1),所以
       :func:`attempt_ledger.canonical_attempt` 会选中这一条 ⇒ 卡片显示
       "认出来了"。不接这一跳的后果是**真实的数据错误**:客户已经人工
       确认过"这就是我家",而卡上永远写着"身份待确认"。

    🔴 ``provider_called=False``:这一跳零 provider 调用(现役那段
       ``resolve_local`` 是确定性判定,零算力)。于是它不进 ``attempted``,
       而 ``attempted`` 仍由 ② 那条 provider attempt 提供 —— 两个计数各自
       都对得上真实调用数(MON-11)。
    """
    if not plan_cell_id:
        return None
    if not ledger_is_available(cur):
        return None

    # [工单 E3-1] ⑥ 这一跳原来把 `actor_user_id` 写进 `tenant_owner_user_id` ——
    # 管理员替客户确认身份时,那一行 attempt 就归到管理员名下了。
    # "谁点的确认"进 request_hash(`human:{actor}`,下面那一行),
    # "这是谁的运行"进 tenant —— 两个身份不许合成一个。
    try:
        tenant_owner = _require_tenant_owner(
            {TENANT_OWNER_CELL_COLUMN: tenant_owner_user_id, "brand_id": brand_id},
            what="close_for_human_resolution", plan_cell_id=plan_cell_id)
    except TenantOwnerUnresolved:
        return None

    def write():
        ordinal = _ledger.next_ordinal(cur, plan_cell_id=plan_cell_id)
        aid = _human_attempt_id(plan_cell_id, ordinal)
        cur.execute(
            f"""
            INSERT INTO public.{_ledger.TABLE}
                (attempt_id, plan_cell_id, attempt_ordinal, parent_attempt_id,
                 run_authority_id, tenant_owner_user_id, brand_id,
                 monitoring_cell_id, actual_provider, actual_model,
                 actual_model_revision, actual_surface, actual_search_mode,
                 request_hash, provider_called, terminal_state,
                 monitoring_result_id, terminal_at, ledger_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,NULL,%s,%s,%s,
                    FALSE,'answered',%s,NOW(),%s)
            ON CONFLICT (attempt_id) DO NOTHING
            """,
            (aid, plan_cell_id, ordinal,
             (_cell_attempt_id(plan_cell_id, ordinal - 1) if ordinal > 1 else None),
             str(run_authority_id or ""), tenant_owner, int(brand_id),
             HUMAN_RESOLUTION_PROVIDER, HUMAN_RESOLUTION_PROVIDER,
             HUMAN_RESOLUTION_PROVIDER, HUMAN_RESOLUTION_PROVIDER,
             f"human:{int(actor_user_id)}",
             int(monitoring_result_id), _ledger.LEDGER_VERSION),
        )
        return aid

    return guarded(cur, "close_for_human_resolution", write)


# ══════════════════════════════════════════════════════════════════════
# 崩溃恢复 —— 在飞 attempt 不许永久占位
# ══════════════════════════════════════════════════════════════════════

def _inflight_attempt_id(cur, plan_cell_id: str) -> str:
    """本格当前那条在飞 attempt 的 id。

    🔴 从**库里现取**而不是让调用方传:现役三条收口路径
       (正常成功 / 正常失败 / 崩溃恢复)分布在不同函数、不同事务,
       让它们各自记着 attempt_id 就是把同一个身份写三处。
       partial unique 保证这里最多命中一行。

    查不到时返回空串 —— ``close_attempt`` 的 ``WHERE attempt_id=''``
    命中零行,``rowcount==0`` ⇒ 返回 False。**不抛**,因为"没有在飞 attempt
    可收"是合法状态(账本在这一格开始跑之后才上线)。
    """
    cur.execute(
        f"SELECT attempt_id FROM public.{_ledger.TABLE} "
        " WHERE plan_cell_id=%s AND terminal_state IS NULL",
        (plan_cell_id,))
    row = cur.fetchone()
    if not row:
        return ""
    return str(row["attempt_id"] if isinstance(row, Mapping) else row[0]).strip()


@never_raises
def reclaim_superseded(cur, *, plan_cell_id: str) -> int:
    """本格赢下新 claim ⇒ 收口旧的在飞 attempt。返回收了几条(0 或 1)。

    🔴 与 :func:`reclaim_orphans` 的区别是**论据不同,不是条件放宽**:

    · ``reclaim_orphans`` 问的是"它还在飞吗",所以必须去查权威(cell 状态)
      或等租约 —— 它可能在任何时候被调用,包括那一格真的还在跑的时候;
    · 本函数只在调用方**已经赢下 claim CAS** 之后调用。那一刻"旧的在飞
      attempt 已作废"是逻辑上的必然,不需要再去问任何人。

    partial unique ``uq_defgeo_attempt_single_inflight`` 保证这里最多一行。
    """
    if not plan_cell_id:
        return 0

    def write():
        cur.execute(
            f"""
            UPDATE public.{_ledger.TABLE}
               SET terminal_state = 'engine_error',
                   terminal_at    = NOW(),
                   error_code     = %s,
                   error_message  = '上一次尝试未收口,本格已重新开跑'
             WHERE plan_cell_id = %s AND terminal_state IS NULL
            """,
            (RECLAIM_CODE_SUPERSEDED, plan_cell_id),
        )
        return int(cur.rowcount)

    return int(guarded(cur, "reclaim_superseded", write) or 0)


@never_raises
def reclaim_orphans(
    cur, *,
    plan_cell_ids: Sequence[str] | None = None,
    monitoring_cell_ids: Sequence[int] | None = None,
    lease_seconds: int = ATTEMPT_LEASE_SECONDS,
) -> int:
    """把**证明已不在飞**的在飞 attempt 收成 ``engine_error``。返回收了几条。

    判"已不在飞"用两个信号,**权威优先**:

    1. 🥇 现役 ``monitoring_run_cells.state`` —— 它是"有没有 provider 调用
       在飞"的权威。cell 已不是 ``running``(或它的 claim 已过期)⇒
       这条 attempt **证明**不在飞。error_code 逐字沿用现役自己的恢复码:
       派发过 ⇒ ``provider_outcome_unknown``,没派发过 ⇒
       ``worker_lost_before_dispatch``。
    2. 🥈 租约超时 —— 只在 cell 行查不到(``monitoring_cell_id`` 为空,
       或那一行已被清理)时回落。落 ``attempt_lease_expired``。

    🔴 为什么不只用超时:超时是**猜**,而且必须猜得比现役租约长
       (否则会在现役还认为在跑的时候先收口)。cell 状态是**事实**,
       它在崩溃后的下一次 ``_recover_expired_monitoring_cells`` 就已经
       是终态了 —— 于是恢复发生得又快又准。判据
       ``test_reclaim_prefers_cell_state_over_timeout`` 打这个优先级:
       把 ① 摘掉只剩 ② 时,一条"cell 早已 failed 但只过了 1 秒"的
       在飞 attempt 收不掉 ⇒ 红。

    作用域必须给至少一个 —— **不许全表扫**。全表 reclaim 会在一个租户的
    恢复里顺手收掉另一个租户正在跑的格。
    """
    if not plan_cell_ids and not monitoring_cell_ids:
        return 0
    if not ledger_is_available(cur):
        return 0

    scope_sql = []
    params: list[Any] = []
    if plan_cell_ids:
        scope_sql.append("a.plan_cell_id = ANY(%s)")
        params.append([str(p) for p in plan_cell_ids])
    if monitoring_cell_ids:
        scope_sql.append("a.monitoring_cell_id = ANY(%s)")
        params.append([int(c) for c in monitoring_cell_ids])

    def write():
        # 🔴 用**相关子查询**而不是 UPDATE ... LEFT JOIN:后者在 PG 里要写成
        #    ``UPDATE ... FROM``,而 ``FROM`` 是 INNER 语义 —— cell 行不存在的
        #    那一批(回落分支 ②)会被整条漏掉,而那正是"租约超时"要管的那批。
        cur.execute(
            f"""
            UPDATE public.{_ledger.TABLE} a
               SET terminal_state = 'engine_error',
                   terminal_at    = NOW(),
                   error_code     = COALESCE((
                       SELECT CASE WHEN c.provider_dispatched_at IS NOT NULL
                                   THEN %s ELSE %s END
                         FROM public.monitoring_run_cells c
                        WHERE c.id = a.monitoring_cell_id
                          AND c.state <> 'running'
                   ), %s),
                   error_message  = '执行进程未收口该次尝试,账本按未确认收敛'
             WHERE a.terminal_state IS NULL
               AND ({" OR ".join(scope_sql)})
               AND (
                    -- ① 权威:cell 已不在 running(含崩溃恢复已把它终态化)
                    EXISTS (SELECT 1 FROM public.monitoring_run_cells c
                             WHERE c.id = a.monitoring_cell_id
                               AND c.state <> 'running')
                    -- ② 回落:没有可查的 cell 行时才看租约
                    OR (NOT EXISTS (SELECT 1 FROM public.monitoring_run_cells c
                                     WHERE c.id = a.monitoring_cell_id)
                        AND a.created_at < NOW() - (%s || ' seconds')::interval)
               )
            """,
            (RECLAIM_CODE_DISPATCHED, RECLAIM_CODE_NOT_DISPATCHED,
             RECLAIM_CODE_LEASE_EXPIRED, *params, int(lease_seconds)),
        )
        return int(cur.rowcount)

    return int(guarded(cur, "reclaim_orphans", write) or 0)


@never_raises
def close_unattempted_for_cells(
    cur, *, monitoring_cell_ids: Sequence[int], reason_code: str,
) -> int:
    """⑧ 把「**从来没形成过 attempt** 就被收走的 queued 格」在账本里闭合。返回闭合了几条。

    ═══════════════════════════════════════════════════════════════════
    🔴 这一格是资金侧的**第三态**(工单 C-3 / Codex 终审 P1-10)
    ═══════════════════════════════════════════════════════════════════
    ``reclaim_orphans`` 只动 ``terminal_state IS NULL`` 的**在飞** attempt 行。
    而一个 ``queued`` 格在被 ``recover_abandoned_monitoring_task_execution``
    或 ``revoke_monitoring_task_coverage_for_organization_refund`` 打到终态时,
    账本里**一行都没有**(``open_for_claim`` 只在 claim 成 running 时才写)——
    于是 reclaim 的 UPDATE 命中零行,它以为自己干完了。

    结果是这一格在账本里永远处于「计划了、没尝试、也没有任何终态记录」:

      · ``assert_terminal_conservation`` 的 ``terminal`` 永远到不了 ``planned``
        ⇒ 进度/五卡永远说"数据坏了"或"还在跑";
      · 资金侧更要命 —— 这一格既没被扣(没交付)、也没被退(整任务的
        release 只按 ``fulfillment_state`` 走,不看这一格有没有交代)、
        更没有人管它(它不在任何 worker 的候选集里)。
        **既没扣也没退也没人管**,正是资金侧不许存在的第三态。

    ═══════════════════════════════════════════════════════════════════
    🔴 为什么落 ``policy_skipped`` 而不是 ``engine_error``
    ═══════════════════════════════════════════════════════════════════
    ``policy_skipped`` 的语义逐字是「**零 provider 调用**,计 terminal 不计
    attempted」(MON-11)。而本函数的作用域恰好就是这一类:
    ``provider_dispatched_at IS NULL`` 且账本零行 ⇒ 一次外调都没发生过。

    落 ``engine_error`` 会破坏守恒:``cell_denominator_facts`` 按
    ``provider_called`` 算 ``isAttempted``,而 ``assert_attempted_conservation``
    要求 ``attempted == outcome + ambiguous + engine_error``。
    一条 ``provider_called=False`` 的 ``engine_error`` 会让右边多一、左边不动 ——
    等式当场破,而破的原因与真实缺陷毫无关系。
    落 ``policy_skipped`` 则两边都对:terminal +1、attempted 不动。
    它也不进 ``engine_error`` 那一档,所以不会被读成"平台故障"。

    ═══════════════════════════════════════════════════════════════════
    🔴 作用域三条,缺一条就会写出假记录
    ═══════════════════════════════════════════════════════════════════
    1. **cell 已是终态**(``state NOT IN ('queued','running')``)——
       还在跑的格不许被提前收口;
    2. **从未派发**(``provider_dispatched_at IS NULL``)—— 派发过就意味着
       可能真的调用过 provider,那时说"零 provider 调用"是**说谎**;
       那一类由 ``reclaim_orphans`` 按 ``provider_outcome_unknown`` 处置;
    3. **账本零行**(``NOT EXISTS``)—— 有行就说明 ``open_for_claim`` 跑过,
       归 ``reclaim_orphans`` 的作用域。两个函数**不重叠**,
       所以不存在"两把锁叠在同一条路径上互相吞变异"。

    调用方持有事务;本函数与 ⑥⑦ 同样是 fail-soft(``guarded`` + SAVEPOINT),
    账本故障不许阻断现役的恢复/退款动作。
    """
    ids = [int(c) for c in (monitoring_cell_ids or ()) if c is not None]
    if not ids:
        return 0
    if str(reason_code or "") not in UNATTEMPTED_REASONS:
        # 现场造一个理由码 = 账本与现役 cell 讲两个故事。宁可不写。
        _LOGGER.error("[defgeo-ledger] 未登记的未尝试收口理由码 %r,拒绝写账本", reason_code)
        return 0
    if not ledger_is_available(cur):
        return 0

    def read():
        cur.execute(
            f"""
            SELECT c.id, c.task_id, c.brand_id, c.plan_hash,
                   c.tenant_owner_user_id
              FROM public.monitoring_run_cells c
             WHERE c.id = ANY(%s)
               AND c.state NOT IN ('queued', 'running')
               AND c.provider_dispatched_at IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM public.{_ledger.TABLE} a
                    WHERE a.plan_cell_id = c.plan_hash
               )
            """,
            (ids,),
        )
        return _ledger_rows(cur)

    rows = guarded(cur, "close_unattempted_scan", read) or []
    closed = 0
    for row in rows:
        plan_cell_id = str(row.get("plan_hash") or "").strip()
        if not plan_cell_id:
            continue

        # [工单 E3-1] ⑤ 的租户归属。原来这里 `COALESCE(b.owner_user_id, 0)`
        # **现读**可变列:品牌在跑完与收口之间被转移,同一次运行的历史归属
        # 就跟着改了;取不到时还落 0。改读格上冻结的那一列,同一个谓词。
        try:
            tenant_owner = _require_tenant_owner(
                row, what="close_unattempted_for_cells", plan_cell_id=plan_cell_id)
        except TenantOwnerUnresolved:
            continue

        def write(_row=row, _pcid=plan_cell_id, _owner=tenant_owner):
            _ledger.record_policy_skip(
                cur,
                plan_cell_id=_pcid,
                run_authority_id=str(_row.get("task_id") or ""),
                tenant_owner_user_id=_owner,
                brand_id=int(_row.get("brand_id") or 0),
                reason_code=str(reason_code),
            )
            return 1

        closed += int(guarded(cur, "close_unattempted_for_cells", write) or 0)
    return closed


def _ledger_rows(cur) -> list[dict[str, Any]]:
    """兼容两种游标形态取结果集(与 ``activation_outbox._rows_as_dicts`` 同规矩)。"""
    rows = cur.fetchall() or []
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(r) for r in rows]
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def census() -> dict[str, Any]:
    return {
        "bridgeVersion": BRIDGE_VERSION,
        "expectedCallSites": list(EXPECTED_CALL_SITES),
        "wiredEntrypoints": list(WIRED_ENTRYPOINTS),
        "ledgerTable": _ledger.TABLE,
        "savepoint": _SAVEPOINT,
        "attemptLeaseSeconds": ATTEMPT_LEASE_SECONDS,
        "liveCellMaxLeaseSeconds": LIVE_CELL_MAX_LEASE_SECONDS,
        "reclaimCodes": [RECLAIM_CODE_DISPATCHED, RECLAIM_CODE_NOT_DISPATCHED,
                         RECLAIM_CODE_LEASE_EXPIRED, RECLAIM_CODE_SUPERSEDED],
        # [工单 C-3] ⑧ 未尝试即被收走的闭合理由码(判据拿它当分母,不手抄)。
        "unattemptedReasons": list(UNATTEMPTED_REASONS),
        "failSoft": True,
        "failSoftMechanism": "SAVEPOINT/ROLLBACK TO SAVEPOINT",
        # [工单 E3-1] 租户归属那条唯一谓词的可机读身份 + 本进程拒绝落账次数。
        # 🔴 计数**不是** 0 就代表健康:它只是"本进程"的。真正的存量口径
        #    要 census 库(见 scripts/backfill_monitoring_cell_tenant_owner_*.sql)。
        "tenantOwnerColumn": TENANT_OWNER_CELL_COLUMN,
        "tenantFabricationAllowed": False,
        "tenantUnresolvedInProcess": _TENANT_UNRESOLVED_COUNT["total"],
    }
