"""WP3 · 持久 worker:租约 / 心跳 / 回收(规格 02 §8.3)。

## 现状与目标

现役是进程内 `asyncio.create_task` —— 容器一重启,在途任务就**没有恢复真值**:
DB 里躺着 `running`,而进程里什么都没有,谁也不会再碰它。规格 §8.3 的目标:

  · task/outbox **先持久化**,API 才返回 202;
  · worker 用 **lease + heartbeat + CAS** 领取;
  · 启动 reconciler 回收租约过期且可安全重试的步骤;
  · **external-start 前后分别记录状态**;返回后、DB 终态前崩溃时**先 sync,不盲重投**。

## 三种崩溃点的恢复语义(03 §6 判据:「重启前 pending、running、渠道已返回三种任务
分别正确恢复」)

| 崩溃时状态 | 恢复动作 | 为什么 |
|---|---|---|
| `pending`,无租约 | 直接可领 | 还没开始,零外部副作用 |
| `running`,租约过期,**未** external-start | 回收后可重领 | 供应商侧没有任何痕迹,重跑安全 |
| `running`,租约过期,**已** external-start | **不重领**,转 `needs_action` | 供应商可能已经在做了;盲重投 = 重复外调 + 重复扣费 |

第三种是这套机制存在的全部理由。少了 `external_started_at` 这一列,前两种和第三种
在 DB 里长得一模一样,reconciler 只能二选一:要么全部重跑(重复外调),
要么全部不跑(任务永久卡死)。两个都是错的。
"""
from __future__ import annotations

from typing import Any, Final, Optional

#: 租约时长。取值依据:生产实测单篇图文生产 p95 约 3-5 分钟(4 张卡),
#: 取 10 分钟 ≈ 2 倍余量吃长尾。太短会让还在正常跑的任务被别人抢走(重复外调),
#: 太长会让真崩溃的任务卡住不动。两个方向都有害,所以它不是"越大越安全"。
DEFAULT_LEASE_SECONDS: Final = 600

#: 心跳间隔。必须 << 租约,否则正常跑的任务会因为一次心跳延迟就被判死。
DEFAULT_HEARTBEAT_SECONDS: Final = 60

#: `finish_task` 允许写入的终态。**两条链的并集**:
#: 老链 `succeeded`,合同链 §5.7 的 `ready` / `settlement_pending` / `failed` /
#: `cancelled`。写成显式集合而不是散在 if 里,是为了让"少了一个值"能被判据打中。
#:
#: 🔴 [第 3 棒 · Codex R2 P0-2] 加入 `needs_action`:**部分交付**(有成品、
#:    缺图仍在、冻结未收敛)既不是 `ready` 也不是 `failed`。少了这个值,
#:    worker 只能在两个都说谎的终态里二选一 —— 上一版选了 `ready`。
ALLOWED_FINISH_STATUSES: Final[frozenset[str]] = frozenset({
    "succeeded", "ready", "failed", "cancelled", "settlement_pending", "needs_action",
})

#: 领取作用域。`contract` = 只领图文合同链的任务(有 production_batch_id)。
SCOPE_ANY: Final = "any"
SCOPE_CONTRACT: Final = "contract"


class LeaseLost(RuntimeError):
    """租约已被别人接管。当前 worker 必须**立刻停手**,不得继续写任何结果。"""


_CLAIM_NEXT = """
UPDATE geo_douyin_post_tasks
   SET lease_owner = %(worker)s,
       lease_expires_at = now() + (%(lease_seconds)s || ' seconds')::interval,
       heartbeat_at = now(),
       status = 'running',
       started_at = COALESCE(started_at, now()),
       updated_at = now()
 WHERE id = (
     SELECT t.id FROM geo_douyin_post_tasks t
      WHERE t.superseded_at IS NULL
        -- 🔴 作用域闸(返工 · 链 3):`contract` 只领合同链任务。
        --    老链(`dispatch_production`)是**进程内** asyncio.Task 在跑,
        --    它的任务行没有 production_batch_id。durable worker 若把它也领走,
        --    同一篇会被跑两遍 —— 两次外调、两次图费。
        --    默认 `any` 保持既有判据的行为不变。
        AND (%(scope)s = 'any' OR t.production_batch_id IS NOT NULL)
        AND (
              -- ① 从没被领过。
              --    🔴 [返工 2026-08-18 · P1-1] 收两个值:合同链按规格 §5.7 写
              --    `queued`,而 034 之前落库的合同行是 `pending`。只认
              --    `pending` 时,合同链新建的任务**永远领不到** —— 那正是
              --    「worker 零生产调用者」之外的第二层不通。
              (t.status IN ('pending', 'queued') AND t.lease_owner IS NULL)
              -- ② 领过但租约过期,且**没有**外部副作用 → 重跑安全
           OR (t.status = 'running'
               AND t.lease_expires_at IS NOT NULL
               AND t.lease_expires_at < now()
               AND t.external_started_at IS NULL)
        )
      ORDER BY t.created_at
      FOR UPDATE SKIP LOCKED
      LIMIT 1
 )
RETURNING id, post_id, user_id, task_ref, request_snapshot, generation_epoch,
          lease_owner, lease_expires_at, freeze_id, freeze_table, reserved_amount,
          settlement_authority, settlement_status, payer_user_id,
          production_batch_id, batch_item_request_id, batch_item_ordinal,
          progress_total
"""


def claim_next_task(cur, *, worker: str,
                    lease_seconds: int = DEFAULT_LEASE_SECONDS,
                    scope: str = SCOPE_ANY) -> Optional[dict[str, Any]]:
    """领一个可跑的任务。

    🔴 `FOR UPDATE SKIP LOCKED` 是多 worker 下不互相阻塞的关键;
       但真正保证"不重复领"的是 `WHERE id = (子查询)` 这个原子 UPDATE ——
       两个 worker 同时跑,只有一个的 UPDATE 会命中。
    🔴 条件②**显式**要求 `external_started_at IS NULL`:
       已经调过供应商的任务永远不进可领集合。删掉这半句,崩溃恢复就会变成重复外调。
    """
    if scope not in (SCOPE_ANY, SCOPE_CONTRACT):
        raise ValueError(f"unknown claim scope: {scope!r}")
    cur.execute(_CLAIM_NEXT, {"worker": worker, "lease_seconds": int(lease_seconds),
                              "scope": scope})
    row = cur.fetchone()
    return dict(row) if row is not None else None


_HEARTBEAT = """
UPDATE geo_douyin_post_tasks
   SET heartbeat_at = now(),
       lease_expires_at = now() + (%(lease_seconds)s || ' seconds')::interval,
       updated_at = now()
 WHERE id = %(task_id)s AND lease_owner = %(worker)s
RETURNING id
"""


def renew_lease(cur, *, task_id: int, worker: str,
                lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
    """续租。`AND lease_owner = %(worker)s` 让被接管的老 worker 续不上 ——
    它据此知道自己出局了,而不是继续闷头写结果。"""
    cur.execute(_HEARTBEAT, {"task_id": int(task_id), "worker": worker,
                             "lease_seconds": int(lease_seconds)})
    if cur.fetchone() is None:
        raise LeaseLost(f"task={task_id} 的租约已不属于 {worker}")


_MARK_EXTERNAL_START = """
UPDATE geo_douyin_post_tasks
   SET external_started_at = COALESCE(external_started_at, now()),
       updated_at = now()
 WHERE id = %(task_id)s AND lease_owner = %(worker)s
RETURNING id, external_started_at
"""


def mark_external_start(cur, *, task_id: int, worker: str) -> dict[str, Any]:
    """external-start 前**必须**先落这一笔,而且必须与后续外调在同一事务提交之后才外调。

    🔴 `COALESCE(external_started_at, now())` 而不是无条件覆盖:
       恢复路径重放时不能把时间戳改新,否则"什么时候开始外调的"这个事实会漂移。
    """
    cur.execute(_MARK_EXTERNAL_START, {"task_id": int(task_id), "worker": worker})
    row = cur.fetchone()
    if row is None:
        raise LeaseLost(f"task={task_id} 的租约已不属于 {worker},禁止发起外部调用")
    return dict(row)


_RECLAIM_STUCK_EXTERNAL = """
UPDATE geo_douyin_post_tasks
   SET status = 'needs_action',
       settlement_status = COALESCE(settlement_status, 'manual'),
       error_msg = COALESCE(NULLIF(error_msg, ''), %(reason)s),
       lease_owner = NULL,
       updated_at = now()
 WHERE status = 'running'
   AND superseded_at IS NULL
   AND external_started_at IS NOT NULL
   AND lease_expires_at IS NOT NULL
   AND lease_expires_at < now() - (%(grace_seconds)s || ' seconds')::interval
RETURNING id
"""


def reconcile_stuck_external_tasks(cur, *, grace_seconds: int = 900,
                                   reason: str = "渠道已开始但结果未知,待人工核对") -> list[int]:
    """把「已 external-start 但租约早已过期」的任务收敛成 `needs_action`。

    🔴 **不重跑、不 release**。规格 §8.2 末:「结果未知保持 frozen / 转 manual」。
       这里刻意把 `settlement_status` 置 `manual` 而不是 `released` ——
       release 意味着"确定没发生",而我们恰恰不确定。
    🔴 `grace_seconds` 在租约之外再留一段:租约刚过期那一刻可能只是心跳抖动,
       立刻判死会把正常任务打成需人工。
    """
    cur.execute(_RECLAIM_STUCK_EXTERNAL, {
        "grace_seconds": int(grace_seconds), "reason": reason})
    return [int(dict(r)["id"]) for r in (cur.fetchall() or [])]


_FINISH = """
UPDATE geo_douyin_post_tasks
   SET status = %(status)s,
       finished_at = now(),
       result_hash = %(result_hash)s,
       lease_owner = NULL,
       lease_expires_at = NULL,
       updated_at = now()
 WHERE id = %(task_id)s
   AND lease_owner = %(worker)s
   AND superseded_at IS NULL
RETURNING id, status
"""


def finish_task(cur, *, task_id: int, worker: str, status: str,
                result_hash: Optional[str] = None) -> dict[str, Any]:
    """收尾。仍然比 `lease_owner` —— 被接管的老 worker 不许写终态。"""
    # 🔴 [返工 2026-08-18 · P1-1] 原来这里**拒收 `ready`**、只认 `settlement_pending`
    #    与老链的 `succeeded` —— 而规格 §5.7 的成功终态就叫 `ready`。
    #    结果是:合同链 worker 一旦按规格收尾就抛 ValueError,
    #    任务永远停在 running。「枚举写在两处」的代价这次是整条链跑不完。
    if status not in ALLOWED_FINISH_STATUSES:
        raise ValueError(
            f"unknown terminal status: {status!r};"
            f"允许的是 {sorted(ALLOWED_FINISH_STATUSES)}")
    cur.execute(_FINISH, {"task_id": int(task_id), "worker": worker,
                          "status": status, "result_hash": result_hash})
    row = cur.fetchone()
    if row is None:
        raise LeaseLost(f"task={task_id} 的租约已不属于 {worker},终态写入被拒")
    return dict(row)
