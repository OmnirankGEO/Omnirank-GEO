"""协调器 · 制作草稿的 ETag CAS 落库(规格 02 §5.2)。

## 草稿**不做**的四件事

不 claim slot、不建 post/task、不调 provider、不冻结算力。
写在这里是因为"草稿"最容易被顺手加副作用 —— 一旦草稿开始占 slot,
用户关掉页面就把别人的容量占住了。

## 为什么必须是服务端草稿而不是 React state

01 §3.3:「所有选择和设置由带 ETag 的后端 production draft 保存,不只放 React state;
若窄 RFC 未通过,则删除跨设备恢复承诺,**不得用浏览器本地状态冒充服务端草稿**」。
两标签页同改同一草稿时,旧 ETag 提交必须返回可理解的冲突,而不是静默覆盖。

## CAS 形态

`UPDATE ... WHERE batch_id=? AND etag_revision=? RETURNING` —— 零行即冲突。
**不用**「先读 etag、比一比、再写」:那在并发下两边都会读到同一个 etag 然后双双写入。
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

STATUS_DRAFT = "draft"
STATUS_ACCEPTED = "accepted"


class DraftConflict(RuntimeError):
    """ETag 不匹配。409/412 + `reload_latest` / `compare_changes`。"""

    def __init__(self, current_etag: Optional[int] = None):
        super().__init__("DRAFT_REVISION_CONFLICT")
        self.current_etag = current_etag


def etag_for(revision: int) -> str:
    """对外 ETag。形态与规格 §5.2 示例一致:`"draft-7"`。"""
    return f'"draft-{int(revision)}"'


def parse_etag(value: str) -> Optional[int]:
    if not value:
        return None
    token = value.strip().strip('"')
    if not token.startswith("draft-"):
        return None
    try:
        return int(token.split("-", 1)[1])
    except (ValueError, IndexError):
        return None


_UPSERT_NEW = """
INSERT INTO geo_douyin_production_batches (
    batch_id, tenant_owner_user_id, payer_user_id, actor_user_id, payer_policy_snapshot,
    brand_id, quote_id, contract_revision_id, etag_revision, price_snapshot,
    expected_total_price_points, status, created_by, created_at, updated_at
) VALUES (
    %(batch_id)s, %(tenant_owner_user_id)s, %(payer_user_id)s, %(actor_user_id)s,
    %(payer_policy_snapshot)s, %(brand_id)s, %(quote_id)s, %(contract_revision_id)s,
    1, %(price_snapshot)s::jsonb, %(expected_total)s, 'draft', %(created_by)s, now(), now()
)
ON CONFLICT (batch_id) DO NOTHING
RETURNING batch_id, etag_revision, status
"""

_CAS_UPDATE = """
UPDATE geo_douyin_production_batches
   SET price_snapshot = %(price_snapshot)s::jsonb,
       expected_total_price_points = %(expected_total)s,
       quote_id = %(quote_id)s,
       contract_revision_id = %(contract_revision_id)s,
       etag_revision = etag_revision + 1,
       updated_at = now()
 WHERE batch_id = %(batch_id)s
   AND tenant_owner_user_id = %(tenant_owner_user_id)s
   AND etag_revision = %(expected_etag)s
   AND status = 'draft'
RETURNING batch_id, etag_revision, status
"""

_READ_CURRENT = """
SELECT batch_id, etag_revision, status, tenant_owner_user_id
  FROM geo_douyin_production_batches
 WHERE batch_id = %(batch_id)s
"""


def save_draft(cur, *, batch_id: str, identity: Mapping[str, Any], brand_id: Optional[int],
               quote_id: Optional[int], contract_revision_id: Optional[str],
               payload: Mapping[str, Any], expected_total_price_points: int,
               expected_etag: Optional[int]) -> dict[str, Any]:
    """新建或 CAS 更新。返回 `{batch_id, etag_revision, etag}`。

    🔴 `AND tenant_owner_user_id = ...` 进 WHERE 而不是只在读时校验:
       跨租户拿到 batch_id 也改不动,而且**零行**就是拒绝 —— 不需要先读一次再判,
       少一次读就少一个 TOCTOU 窗口。
    """
    params = {
        "batch_id": str(batch_id),
        "tenant_owner_user_id": int(identity["tenant_owner_user_id"]),
        "payer_user_id": int(identity["payer_user_id"]),
        "actor_user_id": int(identity["actor_user_id"]),
        "payer_policy_snapshot": json.dumps(
            dict(identity.get("payer_policy_snapshot") or {}), ensure_ascii=False),
        "brand_id": int(brand_id) if brand_id else None,
        "quote_id": int(quote_id) if quote_id else None,
        "contract_revision_id": int(contract_revision_id) if contract_revision_id else None,
        "price_snapshot": json.dumps(dict(payload or {}), ensure_ascii=False),
        "expected_total": int(expected_total_price_points or 0),
        "created_by": int(identity["actor_user_id"]),
    }

    if expected_etag is None:
        cur.execute(_UPSERT_NEW, params)
        row = cur.fetchone()
        if row is not None:
            data = dict(row)
            return {**data, "etag": etag_for(int(data["etag_revision"]))}
        # 已存在却没带 If-Match ⇒ 拒绝(否则第一个不带 etag 的请求会覆盖别人的编辑)
        cur.execute(_READ_CURRENT, {"batch_id": str(batch_id)})
        existing = cur.fetchone()
        raise DraftConflict(int(dict(existing)["etag_revision"]) if existing else None)

    cur.execute(_CAS_UPDATE, {**params, "expected_etag": int(expected_etag)})
    row = cur.fetchone()
    if row is None:
        cur.execute(_READ_CURRENT, {"batch_id": str(batch_id)})
        existing = cur.fetchone()
        raise DraftConflict(int(dict(existing)["etag_revision"]) if existing else None)
    data = dict(row)
    return {**data, "etag": etag_for(int(data["etag_revision"]))}


_ACCEPT = """
UPDATE geo_douyin_production_batches
   SET status = 'accepted', etag_revision = etag_revision + 1, updated_at = now()
 WHERE batch_id = %(batch_id)s
   AND tenant_owner_user_id = %(tenant_owner_user_id)s
   AND etag_revision = %(expected_etag)s
   AND status = 'draft'
RETURNING batch_id, etag_revision, status
"""


def accept_draft(cur, *, batch_id: str, tenant_owner_user_id: int,
                 expected_etag: int) -> dict[str, Any]:
    """提交时把 `draft → accepted`,**只成功一次**(规格 §5.2 末)。

    `AND status = 'draft'` 是"只成功一次"的物理保证:第二次调用时状态已是
    accepted,零行 → DraftConflict。不靠调用方自己记得判重。
    """
    cur.execute(_ACCEPT, {"batch_id": str(batch_id),
                          "tenant_owner_user_id": int(tenant_owner_user_id),
                          "expected_etag": int(expected_etag)})
    row = cur.fetchone()
    if row is None:
        cur.execute(_READ_CURRENT, {"batch_id": str(batch_id)})
        existing = cur.fetchone()
        raise DraftConflict(int(dict(existing)["etag_revision"]) if existing else None)
    return dict(row)
