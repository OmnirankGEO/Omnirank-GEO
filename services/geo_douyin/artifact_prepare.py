"""协调器 · 发布素材准备的持久化与幂等(规格 02 §7.1 / §11.3)。

## 它不是纯读

现役 `prepare-publish-media` 会读 OSS 并**逐图调用渠道/代理上传** —— 那是外部副作用。
所以它必须有:独立持久 `request_id/request_hash/state/lease/heartbeat`,
同 key/hash 返回同一 artifact,异 hash 409。

## 三个状态的语义(不可合并)

| state | 含义 | 允许的下一步 |
|---|---|---|
| `preparing` | 已 claim,可能正在逐图上传 | 等待 / 租约到期后回收重试 |
| `ready` | 全部图就位,`manifest_hash` 已定稿 | 可进 publish-preview |
| `failed` | 明确失败,零外部残留 | **可**重试(沿用同 request_id,不重复冻结) |
| `unknown` | 远端已接受但本地 ack/URL 丢失 | **不自动重传**,转人工核对 |

🔴 `unknown` 与 `failed` 必须分开。合并成一个"失败"会让恢复逻辑对
「远端可能已经收了」的情形也重传 —— 那是重复外调。规格 §7.1 原文:
「远端已接受但本地 ack/URL 丢失时转 unknown 并 query/reconcile/manual,**不自动重传**」。

## 不冻结资金、不占频控

规格 §7.1 末:「该操作不冻结投放资金,也不占账号日容量」。
素材准备失败不该扣钱,也不该把账号当天的额度用掉。
"""
from __future__ import annotations

import json
from typing import Any, Final, Mapping, Optional

STATE_PREPARING: Final = "preparing"
STATE_READY: Final = "ready"
STATE_FAILED: Final = "failed"
STATE_UNKNOWN: Final = "unknown"

#: 可以沿用同一 request_id 重试的状态。`unknown` **不在**其中。
RETRYABLE_STATES: Final[frozenset[str]] = frozenset({STATE_FAILED})


class ArtifactRequestConflict(RuntimeError):
    """同 request_id、不同 hash。409,零副作用。"""


class ArtifactNotRetryable(RuntimeError):
    """对 `unknown` 态调重试。必须人工核对,不自动重传。"""


_CLAIM = """
INSERT INTO geo_douyin_publish_artifacts (
    geo_post_id, post_revision_id, tenant_owner_user_id,
    request_id, request_hash, state, card_statuses, status_version, created_at, updated_at
) VALUES (
    %(geo_post_id)s, %(post_revision_id)s, %(tenant_owner_user_id)s,
    %(request_id)s, %(request_hash)s, 'preparing', '[]'::jsonb, 1, now(), now()
)
ON CONFLICT DO NOTHING
RETURNING prepared_artifact_id, state, request_hash, manifest_hash, post_revision_id
"""

_LOCK_EXISTING = """
SELECT prepared_artifact_id, state, request_hash, manifest_hash, post_revision_id,
       geo_post_id, tenant_owner_user_id
  FROM geo_douyin_publish_artifacts
 WHERE tenant_owner_user_id = %(tenant_owner_user_id)s AND request_id = %(request_id)s
   FOR UPDATE
"""


def claim_artifact(cur, *, geo_post_id: int, post_revision_id: int,
                   tenant_owner_user_id: int, request_id: str,
                   request_hash: str) -> tuple[bool, dict[str, Any]]:
    """原子 claim。返回 `(is_new, row)`。

    与 `contract_idempotency.claim_request` 同一形态,且同样用**裸** ON CONFLICT ——
    该表除 `(owner, request_id)` 唯一外还有主键,写死冲突目标会在别的键上撞时抛错
    而不是走回放(本包在 publish_idempotency_keys 上刚栽过一次)。
    """
    cur.execute(_CLAIM, {
        "geo_post_id": int(geo_post_id),
        "post_revision_id": int(post_revision_id),
        "tenant_owner_user_id": int(tenant_owner_user_id),
        "request_id": str(request_id),
        "request_hash": str(request_hash),
    })
    row = cur.fetchone()
    if row is not None:
        return True, dict(row)

    cur.execute(_LOCK_EXISTING, {
        "tenant_owner_user_id": int(tenant_owner_user_id), "request_id": str(request_id)})
    existing = cur.fetchone()
    if existing is None:
        raise ArtifactRequestConflict(
            "素材准备记录在 claim 与锁定之间消失,请用新的请求 id 重试")
    data = dict(existing)
    # 🔴 `request_hash` 列是 CHARACTER(64) —— **bpchar 会用空格补齐到 64 位**。
    #    真实 sha256 恰好 64 位不受影响,但任何短于 64 的值(测试夹具、未来换算法)
    #    存进去再读出来就带尾随空格,裸比必然不等 ⇒ 幂等回放变成 409。
    #    两边都 strip,让比较不依赖"值恰好是 64 位"这个巧合。
    if str(data.get("request_hash") or "").strip() != str(request_hash).strip():
        raise ArtifactRequestConflict("同一请求 id 提交了不同的作品版本或图片清单")
    return False, data


_MARK_READY = """
UPDATE geo_douyin_publish_artifacts
   SET state = 'ready', manifest_hash = %(manifest_hash)s,
       card_statuses = %(card_statuses)s::jsonb,
       status_version = status_version + 1, updated_at = now()
 WHERE prepared_artifact_id = %(artifact_id)s
   AND state = 'preparing'
RETURNING prepared_artifact_id, state, manifest_hash, post_revision_id
"""


def mark_ready(cur, *, artifact_id: int, manifest_hash: str,
               card_statuses: list[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """全部图就位。`AND state='preparing'` 挡住迟到 worker 把 failed/unknown 改回 ready。"""
    cur.execute(_MARK_READY, {
        "artifact_id": int(artifact_id),
        "manifest_hash": str(manifest_hash),
        "card_statuses": json.dumps(list(card_statuses or []), ensure_ascii=False),
    })
    row = cur.fetchone()
    return dict(row) if row is not None else None


_MARK_TERMINAL = """
UPDATE geo_douyin_publish_artifacts
   SET state = %(state)s, card_statuses = %(card_statuses)s::jsonb,
       status_version = status_version + 1, updated_at = now()
 WHERE prepared_artifact_id = %(artifact_id)s
   AND state = 'preparing'
RETURNING prepared_artifact_id, state
"""


def mark_failed(cur, *, artifact_id: int,
                card_statuses: list[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    cur.execute(_MARK_TERMINAL, {
        "artifact_id": int(artifact_id), "state": STATE_FAILED,
        "card_statuses": json.dumps(list(card_statuses or []), ensure_ascii=False)})
    row = cur.fetchone()
    return dict(row) if row is not None else None


def mark_unknown(cur, *, artifact_id: int,
                 card_statuses: list[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """远端已接受、本地 ack/URL 丢失。

    🔴 与 failed **分开**:合并会让恢复逻辑对"远端可能已经收了"的情形也重传。
    """
    cur.execute(_MARK_TERMINAL, {
        "artifact_id": int(artifact_id), "state": STATE_UNKNOWN,
        "card_statuses": json.dumps(list(card_statuses or []), ensure_ascii=False)})
    row = cur.fetchone()
    return dict(row) if row is not None else None


def assert_retryable(state: str) -> None:
    """重试前的闸。`unknown` 一律拒 —— 不自动重传是硬约束,不是保守选择。"""
    if str(state) not in RETRYABLE_STATES:
        raise ArtifactNotRetryable(
            f"当前状态 {state!r} 不可自动重试:"
            + ("结果未知,发布渠道可能已经收到,需人工核对后处理"
               if str(state) == STATE_UNKNOWN else "该状态无需重试")
        )


def assert_artifact_matches(artifact: Mapping[str, Any], *, post_revision_id: int,
                            manifest_hash: str) -> None:
    """下单只接受**这一份** artifact(规格 §11.3)。

    post 在准备后发生新 revision 时,旧 artifact 要么按已确认版本发布,
    要么明确 409 要求重新确认 —— **不能混用旧图新文案**。
    """
    if int(artifact.get("post_revision_id") or 0) != int(post_revision_id):
        raise ArtifactRequestConflict(
            "作品已产生新版本,这份发布素材对应的是旧版本;请重新准备并确认")
    if str(artifact.get("manifest_hash") or "") != str(manifest_hash):
        raise ArtifactRequestConflict("图片清单已变化,请重新准备并确认")
    if str(artifact.get("state") or "") != STATE_READY:
        raise ArtifactRequestConflict(
            f"发布素材尚未准备完成(当前 {artifact.get('state')!r}),不能进入下单")
