"""WP2 · 原子幂等 claim(规格 02 §8.1)。

## 判据要守的那句话

「同 key 同 payload 20 次真并发 → 一个 command/batch,每项一次 freeze,external-start 至多一次」。

## 为什么必须是 `INSERT ... ON CONFLICT DO NOTHING RETURNING`,不是先查后写

先查后写(check-then-create)在真并发下必然双开:两个请求同时查到"没有",
然后各自创建。本仓的既有 `publish_idempotency_keys` 入口就是 check-then-save
(规格 02 §3.7 点名),这正是要换掉的形态。

原子 claim 的完整形状是**三步**,少一步都不成立:

  1. `INSERT ... ON CONFLICT DO NOTHING RETURNING` —— 抢到就是新 claim;
  2. 没抢到(零行)→ `SELECT ... FOR UPDATE` 锁住既有行(不能裸读:裸读会和
     正在写响应的那个事务打架,读到半成品);
  3. 校验 owner / endpoint / **request_hash** 三者:
     全一致 → 回放当前状态;任一不同 → 409 `IDEMPOTENCY_CONFLICT`,零副作用。

## 寿命

规格 §8.1 末:「生产和发布是持久影响,幂等记录寿命与成品/command 审计寿命一致,
**不能 24 小时删除后复用**」。既有 `idx_idempotency_cleanup` + 24h 清理任务
只能作用于 `record_kind='legacy'` 的老行 —— 新链行(root/retry)必须排除在外。
本模块提供 `LEGACY_CLEANUP_PREDICATE` 给清理侧引用,免得两边各写一个 WHERE 又漂移。
"""
from __future__ import annotations

import json
from typing import Any, Final, Mapping, Optional

from services.article_closed_loop_contract import snapshot_hash

IDEMPOTENCY_HASH_SCHEMA: Final = "geo-image-note-idempotency-v1"

RECORD_KIND_LEGACY: Final = "legacy"
RECORD_KIND_ROOT: Final = "root"
RECORD_KIND_RETRY: Final = "retry"

COORDINATION_CLAIMED: Final = "claimed"
COORDINATION_PENDING_APPROVAL: Final = "pending_approval"
COORDINATION_MATERIALIZED: Final = "materialized"
COORDINATION_FAILED: Final = "coordination_failed"

#: 24h 清理**只允许**匹配老行。新链行寿命与审计寿命一致。
#: 🔴 写成 `record_kind IS NULL OR record_kind = 'legacy'` 而不是
#:    `record_kind <> 'root'` —— 后者对 NULL 恒 UNKNOWN,会把老行全放过去(反了)。
LEGACY_CLEANUP_PREDICATE: Final = "(record_kind IS NULL OR record_kind = 'legacy')"


class IdempotencyConflict(ValueError):
    """同 request_id,不同 owner / endpoint / payload。409,零副作用。"""

    def __init__(self, reason: str, existing: Optional[Mapping[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.existing = dict(existing or {})


def request_hash(*, owner_user_id: int, endpoint: str, payload: Mapping[str, Any]) -> str:
    """canonical request hash。

    🔴 owner 与 endpoint 必须进摘要:同一个 request_id 在不同 owner / 不同端点下
       是**不同**的请求,只按 payload 求摘要会让跨端点重放看起来"一致"。
    """
    return snapshot_hash({
        "schema": IDEMPOTENCY_HASH_SCHEMA,
        "owner_user_id": int(owner_user_id),
        "endpoint": str(endpoint),
        "payload": dict(payload),
    })


def production_request_payload(*, draft_id: Any, draft_etag: Any, quote_id: Any,
                               contract_revision_id: Any, items: list[Mapping[str, Any]],
                               expected_total_price_points: Any) -> dict[str, Any]:
    """制作链的 canonical hash 输入(规格 §8.1:覆盖 owner、endpoint、draft revision、
    全部 slot/topic/settings、逐项制作价格指纹和 expected total)。

    🔴 逐项按 `item_request_id` 排序后入摘要 —— 同一批 item 换个顺序提交必须得到
       **同一个** hash,否则前端重排一下就绕过了幂等。
    """
    normalized_items = sorted(
        (
            {
                "item_request_id": str(item.get("item_request_id")),
                "delivery_slot_key": str(item.get("delivery_slot_key")),
                "topic_ref": item.get("topic_ref"),
                "expected_price_fingerprint": item.get("expected_price_fingerprint"),
                "settings": dict(item.get("settings") or {}),
            }
            for item in items
        ),
        key=lambda row: row["item_request_id"],
    )
    return {
        "draft_id": str(draft_id) if draft_id is not None else None,
        "draft_etag": str(draft_etag) if draft_etag is not None else None,
        "quote_id": int(quote_id) if quote_id is not None else None,
        "contract_revision_id": (
            str(contract_revision_id) if contract_revision_id is not None else None
        ),
        "expected_total_price_points": int(expected_total_price_points or 0),
        "items": normalized_items,
    }


def publish_request_payload(*, items: list[Mapping[str, Any]],
                            expected_total_price_points: Any) -> dict[str, Any]:
    """投放链的 canonical hash 输入(规格 §8.1:覆盖全部 post revision/artifact/manifest/
    account、逐项投放价格指纹和 expected total)。"""
    normalized_items = sorted(
        (
            {
                "item_request_id": str(item.get("item_request_id")),
                "geo_post_id": int(item.get("geo_post_id") or 0),
                "post_revision_id": str(item.get("post_revision_id")),
                "prepared_artifact_id": str(item.get("prepared_artifact_id")),
                "manifest_hash": item.get("manifest_hash"),
                "media_id": int(item.get("media_id") or 0),
                "expected_price_fingerprint": item.get("expected_price_fingerprint"),
            }
            for item in items
        ),
        key=lambda row: row["item_request_id"],
    )
    return {
        "expected_total_price_points": int(expected_total_price_points or 0),
        "items": normalized_items,
    }


# ---------------------------------------------------------------------------
# 原子 claim
# ---------------------------------------------------------------------------

_CLAIM_SQL = """
INSERT INTO publish_idempotency_keys (
    request_id, user_id, endpoint, response_json,
    record_kind, command_id, root_request_id, coordination_state, root_version,
    request_hash, tenant_owner_user_id, principal_user_id, payer_user_id,
    actor_user_id, actor_kind, organization_id, membership_id, membership_version,
    payer_policy_snapshot, created_at, updated_at
) VALUES (
    %(request_id)s, %(user_id)s, %(endpoint)s, '{}'::jsonb,
    %(record_kind)s, %(command_id)s, %(root_request_id)s, %(coordination_state)s, 1,
    %(request_hash)s, %(tenant_owner_user_id)s, %(principal_user_id)s, %(payer_user_id)s,
    %(actor_user_id)s, %(actor_kind)s, %(organization_id)s, %(membership_id)s,
    %(membership_version)s, %(payer_policy_snapshot)s, now(), now()
)
-- 🔴 **裸** ON CONFLICT DO NOTHING(不写冲突目标)。
--    2026-08-17 被本包自己的 20 并发判据抓到的真缺陷:原文写的是
--    `ON CONFLICT (request_id) DO NOTHING`,它**只**覆盖 request_id 那一把唯一键。
--    但 034 还建了 `uq_publish_idem_command_id`。当调用方按 request_id 确定性地
--    派生 command_id(一件非常自然的事)时,并发落败者撞的是 **command_id** 那把锁 ——
--    不在冲突目标里 ⇒ PG 直接抛 duplicate key,而不是返回零行走幂等回放。
--    结果:20 并发里 19 条炸错误,而不是 19 次回放。
--    裸形态覆盖**该表全部**唯一约束,零行即冲突,再由下面的 FOR UPDATE 分流。
ON CONFLICT DO NOTHING
RETURNING request_id, record_kind, command_id, coordination_state, root_version,
          request_hash, user_id, endpoint, response_json
"""

_LOCK_EXISTING_SQL = """
SELECT request_id, record_kind, command_id, root_request_id, coordination_state,
       root_version, request_hash, user_id, endpoint, response_json,
       tenant_owner_user_id, payer_user_id, actor_user_id
  FROM publish_idempotency_keys
 WHERE request_id = %(request_id)s
   FOR UPDATE
"""


def claim_request(cur, *, request_id: str, endpoint: str, owner_user_id: int,
                  expected_hash: str, identity: Mapping[str, Any],
                  record_kind: str = RECORD_KIND_ROOT,
                  command_id: Optional[str] = None,
                  root_request_id: Optional[str] = None,
                  coordination_state: str = COORDINATION_CLAIMED) -> tuple[bool, dict[str, Any]]:
    """在**调用方的事务**里原子 claim。

    返回 `(is_new, row)`:
      · `is_new=True`  —— 本次抢到,调用方继续往下做真实副作用;
      · `is_new=False` —— 已存在且三者一致,调用方**只回放**,不得再产生副作用。

    任一不一致 → 抛 `IdempotencyConflict`(调用方 409,零新增行/零资金/零外调)。

    🔴 本函数**不自己开事务、不 commit** —— 它必须与 slot claim、订单项、freeze
       在**同一个 PG 事务**里(规格 §8.2)。自开连接的 helper 是这条链上最容易
       出现"入口 claim 成功但后续 rollback,幂等记录留下了孤儿"的地方。
    """
    if record_kind not in (RECORD_KIND_LEGACY, RECORD_KIND_ROOT, RECORD_KIND_RETRY):
        raise ValueError(f"unknown record_kind: {record_kind!r}")

    params = {
        "request_id": str(request_id),
        "user_id": int(owner_user_id),
        "endpoint": str(endpoint),
        "record_kind": record_kind,
        "command_id": command_id,
        "root_request_id": root_request_id,
        "coordination_state": coordination_state,
        "request_hash": str(expected_hash),
        "tenant_owner_user_id": int(identity.get("tenant_owner_user_id") or owner_user_id),
        "principal_user_id": int(identity.get("principal_user_id") or owner_user_id),
        "payer_user_id": int(identity.get("payer_user_id") or owner_user_id),
        "actor_user_id": int(identity.get("actor_user_id") or owner_user_id),
        "actor_kind": identity.get("actor_kind") or "owner",
        "organization_id": identity.get("organization_id"),
        "membership_id": identity.get("membership_id"),
        "membership_version": identity.get("membership_version"),
        "payer_policy_snapshot": identity.get("payer_policy_snapshot_json"),
    }
    cur.execute(_CLAIM_SQL, params)
    row = cur.fetchone()
    if row is not None:
        return True, _as_dict(row)

    # 没抢到 —— 锁住既有行再比对。裸 SELECT 会读到正在写响应的半成品。
    cur.execute(_LOCK_EXISTING_SQL, {"request_id": str(request_id)})
    existing_row = cur.fetchone()
    if existing_row is None:
        # 冲突了、但按 request_id 又查不到 ⇒ 撞的是**别的**唯一键(现役只有 command_id)。
        # 这不是幂等回放,是调用方把一个已被占用的 command_id 用到了新请求上。
        # 必须报成明确冲突,不许静默当新建、也不许含糊成"记录消失让你重试"。
        if command_id:
            cur.execute(
                "SELECT request_id FROM publish_idempotency_keys WHERE command_id = %s",
                (str(command_id),),
            )
            owner_of_command = cur.fetchone()
            if owner_of_command is not None:
                raise IdempotencyConflict(
                    f"command_id {command_id!r} 已属于另一个请求 —— "
                    "command id 必须每次 claim 新生成,不能按 request id 确定性派生后复用",
                    _as_dict(owner_of_command),
                )
        # 走到这里才是真正的"claim 与锁定之间记录消失"(并发删除)。不重试、不假装成功。
        raise IdempotencyConflict("幂等记录在 claim 与锁定之间消失,请用新的 request id 重试")
    existing = _as_dict(existing_row)

    if int(existing.get("user_id") or 0) != int(owner_user_id):
        raise IdempotencyConflict("该请求 id 属于另一个账户", existing)
    if str(existing.get("endpoint") or "") != str(endpoint):
        raise IdempotencyConflict("该请求 id 已用于另一个操作", existing)
    if str(existing.get("request_hash") or "") != str(expected_hash):
        raise IdempotencyConflict("同一请求 id 提交了不同内容", existing)
    return False, existing


_RECORD_RESPONSE_SQL = """
UPDATE publish_idempotency_keys
   SET response_json = %(response_json)s::jsonb,
       coordination_state = %(coordination_state)s
 WHERE request_id = %(request_id)s
   AND user_id = %(user_id)s
RETURNING request_id
"""


def record_response(cur, *, request_id: str, owner_user_id: int,
                    response: Mapping[str, Any],
                    coordination_state: str = COORDINATION_MATERIALIZED) -> None:
    """把这次 claim 的**结果**写回幂等行,让回放答得出同一个答案。

    🔴 [返工 2026-08-18 · 链 3] 原来仓里**没有任何地方**写 `response_json` ——
       claim 抢到之后结果只存在于 HTTP 响应里。于是幂等回放
       (`if not is_new: return replay`)返回的是一个空 dict:
       用户双击一下,第二次拿到的是 `batch_id=None` 的"成功",
       页面从此轮询不到任何东西。
       **幂等不是"第二次别再做一遍",而是"第二次给同一个答案"** ——
       只做到前半句,等于把重复提交变成了静默的数据丢失。

    🔴 与 claim 在**同一事务**里写(共用调用方 cursor):结果行与真实副作用
       要么一起提交,要么一起回滚。分开写就会出现"库里有批次、幂等行说没有"。
    """
    cur.execute(_RECORD_RESPONSE_SQL, {
        "request_id": str(request_id),
        "user_id": int(owner_user_id),
        "response_json": json.dumps(dict(response or {}), ensure_ascii=False),
        "coordination_state": str(coordination_state),
    })
    if cur.fetchone() is None:
        raise IdempotencyConflict(
            f"幂等行 {request_id!r} 不存在或不属于本账户,结果无法回写")


def _as_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return dict(row)
