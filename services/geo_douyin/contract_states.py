"""图文合同链的**状态枚举唯一处**(规格 02 §5.7 · Codex P1-1)。

## 为什么单独一个模块

制作任务的状态被四个地方读写:API 建行、durable worker 收尾、batch GET 投影、
前端 `BATCH_ITEM_COPY` 文案表。四处各写各的字符串,就会出现
「后端写 `succeeded`、前端表里只有 `ready`」这种**静默错位** ——
状态没翻译出来,界面上就是一个空标签,而没有任何一条判据会红。

Codex P1-1 报的正是这个:实现用 `pending/running/succeeded`,
规格 §5.7 定的是 `queued/running/settlement_pending/ready|failed|cancelled`;
`finish_task` 甚至**拒收** `ready`、只认 `settlement_pending`。

## §5.7 的六态,以及为什么 `settlement_pending` 不能省

```
queued ──► running ──► settlement_pending ──► ready
                 │                       └──► failed(已 release,算力已退回)
                 └──► cancelled(未开工,零外调)
                 └──► needs_action(渠道已开始但结果未知,保持 frozen 转人工)
```

`settlement_pending` 是「东西做完了、钱还没结」的那一格。少了它,
「图片已生成」与「已扣费」会被压成同一个状态 —— 于是 commit 失败时
既不能说"没做",也不能说"已扣",恢复逻辑无从下手。
本仓 §8.2 的整条资金闭环就挂在这一格上。

🔴 老链(`production_task.py` 的 `dispatch_production`)仍写
   `pending/running/succeeded/failed`,**一个字都不改** —— 它是另一条活着的链。
   两套枚举并存是事实,所以这里显式给出 `LEGACY_*` 与映射函数,
   让"两套"变成看得见的一件事,而不是散落在四处的 if。
"""
from __future__ import annotations

import logging
from typing import Final

logger = logging.getLogger("GEO-ImageNote-States")

# ── 规格 §5.7 的六态 ────────────────────────────────────────────
TASK_STATUS_QUEUED: Final = "queued"
TASK_STATUS_RUNNING: Final = "running"
TASK_STATUS_SETTLEMENT_PENDING: Final = "settlement_pending"
TASK_STATUS_READY: Final = "ready"
TASK_STATUS_FAILED: Final = "failed"
TASK_STATUS_CANCELLED: Final = "cancelled"
TASK_STATUS_NEEDS_ACTION: Final = "needs_action"

#: 合同链任务的全部合法状态。写成封闭集合而不是"不等于某几个" ——
#: 反向写法在加新状态时会静默放行。
CONTRACT_TASK_STATUSES: Final[frozenset[str]] = frozenset({
    TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_SETTLEMENT_PENDING,
    TASK_STATUS_READY, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED,
    TASK_STATUS_NEEDS_ACTION,
})

#: 终态:worker 不再碰、租约必须已释放。
TERMINAL_TASK_STATUSES: Final[frozenset[str]] = frozenset({
    TASK_STATUS_READY, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED,
})

#: 可被 durable worker 领取的起始状态。
CLAIMABLE_TASK_STATUSES: Final[frozenset[str]] = frozenset({TASK_STATUS_QUEUED})

# ── 老链枚举(`production_task.py` / `db.geo_douyin_db`)· 只读不改 ──
LEGACY_STATUS_PENDING: Final = "pending"
LEGACY_STATUS_SUCCEEDED: Final = "succeeded"

#: 老链的"补齐中":成品部分交付,冻结既没 commit 也没 release。
LEGACY_STATUS_COMPLETING: Final = "completing"
#: 老链在 `dispatch_production` 里用的在途值。
LEGACY_STATUS_SUPERSEDED: Final = "superseded"

#: 老 → 新的读侧映射。**只在读的时候用**,绝不反向把新链写成老值。
#: 用途:batch GET / 六阶段投影要能同时看懂两条链留下的行。
LEGACY_TO_CONTRACT: Final[dict[str, str]] = {
    LEGACY_STATUS_PENDING: TASK_STATUS_QUEUED,
    LEGACY_STATUS_SUCCEEDED: TASK_STATUS_READY,
    # 🔴 「补齐中」= 有成品、缺图、钱还冻着 ⇒ 读侧口径是 `needs_action`,
    #    不是 ready。这与 `contract_seams.resolve_production_terminal` 对
    #    `outcome.completing` 的判法**同源**:两条链看同一件事必须给同一个词。
    LEGACY_STATUS_COMPLETING: TASK_STATUS_NEEDS_ACTION,
    LEGACY_STATUS_SUPERSEDED: TASK_STATUS_CANCELLED,
    TASK_STATUS_RUNNING: TASK_STATUS_RUNNING,
    TASK_STATUS_FAILED: TASK_STATUS_FAILED,
    TASK_STATUS_CANCELLED: TASK_STATUS_CANCELLED,
    TASK_STATUS_NEEDS_ACTION: TASK_STATUS_NEEDS_ACTION,
    TASK_STATUS_SETTLEMENT_PENDING: TASK_STATUS_SETTLEMENT_PENDING,
    TASK_STATUS_READY: TASK_STATUS_READY,
    TASK_STATUS_QUEUED: TASK_STATUS_QUEUED,
}


def normalize_task_status(raw: object) -> str:
    """把任意一行任务的 `status` 读成 §5.7 的口径。**值域封闭**。

    🔴 [第 3 棒 · Codex R2 P1] 上一版未知值**原样透传**,理由写的是
       「不认识就照实说,让它在投影里显眼」。但透传出去的值会流进
       `summary` 的七个计数格与前端 `BATCH_ITEM_COPY` 文案表 ——
       两处都只认封闭集合,于是未知值的真实归宿是:计数**一个格子都不进**
       (总数对不上),文案表**查不到**(界面上是个空标签)。
       "显眼"这个意图没有实现,实现出来的是**静默消失**。

       封闭口径:认识的按表翻,不认识的一律 `needs_action`(集合内唯一
       表达"这条得有人看一眼"的值)+ 一条 warning 日志留原值。
       想看原值的读侧用 `describe_task_status`,它把 raw 一并给出。
    """
    value = str(raw or "").strip()
    mapped = LEGACY_TO_CONTRACT.get(value)
    if mapped is not None:
        return mapped
    logger.warning("[contract-states] 未知任务状态 %r,按 needs_action 归口", value)
    return TASK_STATUS_NEEDS_ACTION


def describe_task_status(raw: object) -> dict:
    """`{state, raw, recognized}`。给需要**同时**拿到封闭态与原值的读侧。

    分成两个函数而不是让 `normalize_task_status` 返回元组:调用点绝大多数
    只要那一个词,改成元组会让每一处都得解包 —— 而漏解包的那处会把
    整个元组当字符串塞进 DTO。
    """
    value = str(raw or "").strip()
    mapped = LEGACY_TO_CONTRACT.get(value)
    return {"state": mapped if mapped is not None else TASK_STATUS_NEEDS_ACTION,
            "raw": value, "recognized": mapped is not None}


# ── 批次(batch)对外状态 · §7.5 同族的**派生**投影 ──────────────
#
# 🔴 [第 3 棒 · Codex R2 P1] `geo_douyin_production_batches.status` 建行时写
#    `'accepted'`,**全仓没有任何一处更新它**。batch GET 把它当对外状态直接
#    下发 ⇒ 一批全做完了、全失败了,界面上永远是"已接受"。
#    与 §7.5「command_status 不是存字段而是投影」同一条理由:存字段必然漂移。
BATCH_ACCEPTED: Final = "accepted"
BATCH_PROCESSING: Final = "processing"
BATCH_NEEDS_ACTION: Final = "needs_action"
BATCH_COMPLETED: Final = "completed"
BATCH_PARTIAL_SUCCESS: Final = "partial_success"
BATCH_FAILED: Final = "failed"
BATCH_CANCELLED: Final = "cancelled"

BATCH_STATUSES: Final[frozenset[str]] = frozenset({
    BATCH_ACCEPTED, BATCH_PROCESSING, BATCH_NEEDS_ACTION, BATCH_COMPLETED,
    BATCH_PARTIAL_SUCCESS, BATCH_FAILED, BATCH_CANCELLED,
})


def project_batch_status(item_states: "list[str]") -> str:
    """按逐项状态算批次对外状态。优先级与 §7.5 的四条同构。

    1. 任一 `needs_action` → needs_action(有人得看一眼,别的都往后排);
    2. 仍有非终态:全部 queued → accepted,否则 processing
       (混合态 `ready + queued` 必须是 processing —— 说"已接受"等于告诉
       用户还没开始,而实际上已经做完一篇了);
    3. 全终态:全 ready → completed;有 ready 有别的 → partial_success;
       全 cancelled → cancelled;其余 → failed。
    """
    states = [str(s or "") for s in item_states]
    if not states:
        return BATCH_ACCEPTED
    if any(s == TASK_STATUS_NEEDS_ACTION for s in states):
        return BATCH_NEEDS_ACTION
    non_terminal = [s for s in states if s not in TERMINAL_TASK_STATUSES]
    if non_terminal:
        return (BATCH_ACCEPTED
                if all(s == TASK_STATUS_QUEUED for s in states)
                else BATCH_PROCESSING)
    if all(s == TASK_STATUS_READY for s in states):
        return BATCH_COMPLETED
    if any(s == TASK_STATUS_READY for s in states):
        return BATCH_PARTIAL_SUCCESS
    if all(s == TASK_STATUS_CANCELLED for s in states):
        return BATCH_CANCELLED
    return BATCH_FAILED


# ── settlement_status(034 的 CHECK 已允许这七个值)────────────
SETTLEMENT_FROZEN: Final = "frozen"
SETTLEMENT_PENDING: Final = "settlement_pending"
SETTLEMENT_COMMITTED: Final = "committed"
SETTLEMENT_RELEASED: Final = "released"
SETTLEMENT_MANUAL: Final = "manual"
SETTLEMENT_QUARANTINED: Final = "quarantined"
SETTLEMENT_EXEMPT: Final = "exempt"
