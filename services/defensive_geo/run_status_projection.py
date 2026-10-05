"""防御型 GEO · 现役 ``diagnosis_runs.run_status`` → 三轴只读投影(规格 §3.5)。

规格把「一个混合 enum」拆成互不represent的三条轴:

  · preview record 只有 ``open|expired|consumed``            —— 不在本模块(属 run preview)
  · ``runState``     = 跑到哪一步了(给人看进度)
  · ``fundingState`` = 钱在哪个方向(给账看)

🔴 三条铁律(逐条都对应一发变异):

  1. **本模块只读**。confirm 之后唯一可写的 settlement authority 仍是现役
     ``diagnosis_runs.run_status``;这里绝不另建第二套 mutable enum。
  2. **未知值双轴 quarantined**,并原样保留 ``rawStatus`` 血缘。
     绝不能因为「没见过」就默认 completed 或 released ——
     前者让客户白拿结果,后者让平台白退钱。
  3. **分母来自 census,不是手抄**。``scripts/defgeo_census/run_status_census.py``
     从迁移 SQL 的 ``chk_diag_runs_status`` 机械抽出允许值全集;
     判据拿那个全集当分母逐值核对本表。手抄清单漏掉的那一项不会让任何判据变红。

映射依据 = ``services/diagnosis_runs.py`` 的真实语义(逐值在 41 班代码里核过,
出处写在每一行的注释里),不是按名字猜。
"""

from __future__ import annotations

from typing import Literal, NamedTuple

#: 本投影表的版本。规格 §3.5 要求 runState 是「版本化只读投影」;
#: 改任何一行映射都必须升版,否则旧客户端会拿新语义当旧语义读。
PROJECTION_VERSION = "diagnosis-run-status-projection-v1"

RunState = Literal[
    "queued", "running", "settlement_pending", "completed",
    "needs_action", "failed", "cancelled", "quarantined",
]
FundingState = Literal[
    "frozen", "committed", "released", "pending_reconciliation",
    "quarantined", "exempt_recorded",
]


class RunStatusProjection(NamedTuple):
    run_state: RunState
    funding_state: FundingState
    #: 原始现役值。永远随行下发 —— 出问题时要能从投影反查回真相。
    raw_status: str
    #: 是否是本表已知值。False ⇒ 双轴 quarantined。
    known: bool


#: 现役 13 值逐值映射。
#:
#: 🔴 **不许出现「其余按 X 处理」的兜底行** —— 兜底行会让「漏一个值」
#:    和「映射对了」在判据眼里长得一样(本仓 2026-08-18 记过
#:    「删除 unknown fallback / 错映任一值」是 DIA-FIN-12 的必红变异)。
#:    未知走 project() 的显式 quarantine,不走本表。
_PROJECTION: dict[str, tuple[RunState, FundingState]] = {
    # ── 未终态(services/diagnosis_runs.py::ACTIVE_STATUSES)────────────────
    # freeze 已发出、结果未知。钱可能已冻可能没冻 → 只能 pending_reconciliation,
    # 绝不能报 released(R0 分支 5:平台请求已发出后未知不得释放)。
    "pending_freeze": ("queued", "pending_reconciliation"),
    # freeze 确认成功(或 exempt 免单),worker 在跑。钱已冻住。
    "running": ("running", "frozen"),
    # 结果已出,正在 commit。钱仍冻着,尚未落账。
    "commit_pending": ("settlement_pending", "frozen"),
    # 判定零执行,正在 release。钱仍冻着,尚未退回。
    "release_pending": ("settlement_pending", "frozen"),

    # ── 终态(services/diagnosis_runs.py::TERMINAL_STATUSES)────────────────
    # 成功且已扣。唯一允许 runState=completed 的付费分支。
    "committed": ("completed", "committed"),
    # 明确零执行,已退回。
    "released": ("cancelled", "released"),
    # 确定性失败(billing 400/402 已知业务拒绝),没冻成钱。
    "cancelled": ("cancelled", "released"),
    # pending_freeze 双表连续查空确认无冻结 → 零退款零扣费。
    # 🔴 funding 不是 released(没有钱被退回),而是 exempt_recorded:
    #    本次根本没有形成冻结,账面上是「无此笔」。
    "cancelled_no_freeze": ("cancelled", "exempt_recorded"),
    # 免单(admin / 组织成员 adapter)成功完成。钱走平台成本账,不走钱包。
    "completed_exempt": ("completed", "exempt_recorded"),
    # 免单失败。同样不涉及钱包方向。
    "failed_exempt": ("failed", "exempt_recorded"),
    # 钱已结算但产物缺失(非成功终态)。客户视角需要人介入 → needs_action;
    # 钱的方向此时已定(committed 那一侧),不能报成 frozen。
    "delivery_repair_pending": ("needs_action", "committed"),

    # ── 转人工(资金对不上,等 ops 处置)────────────────────────────────────
    # 🔴 这两个值的 funding 必须是 quarantined:钱到底冻没冻/退没退是**未知**的,
    #    这正是它们转人工的原因。报成 frozen 等于替 ops 下了结论。
    "settlement_manual": ("needs_action", "quarantined"),
    "manual_resolving": ("needs_action", "quarantined"),
}


def known_statuses() -> frozenset[str]:
    """本表覆盖的现役值集合。判据拿它跟 census 分母求差。"""
    return frozenset(_PROJECTION)


def project(raw_status: object) -> RunStatusProjection:
    """现役 ``run_status`` → 三轴投影。未知/空/非字符串一律双轴 quarantined。

    未知不抛异常:一个没见过的状态值不该让整个进度端点 500 ——
    那会把「有个值没映射」升级成「客户看不到任何进度」。
    但它也绝不静默变成 completed/released:双轴 quarantined + 原值随行,
    让人能查、让判据能红。
    """
    if not isinstance(raw_status, str) or not raw_status.strip():
        return RunStatusProjection("quarantined", "quarantined", str(raw_status), False)
    value = raw_status.strip()
    mapped = _PROJECTION.get(value)
    if mapped is None:
        return RunStatusProjection("quarantined", "quarantined", value, False)
    return RunStatusProjection(mapped[0], mapped[1], value, True)
