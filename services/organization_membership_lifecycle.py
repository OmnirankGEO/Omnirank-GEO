"""Membership-side legacy access preservation and dedicated operator retirement.

Every helper receives a caller-owned cursor.  It never commits or rolls back,
so membership state, organization assignments, legacy projection and JWT
generation advance in one transaction.
"""

from __future__ import annotations

import json
from typing import Any

from services.organization_contract import OrganizationError, payload_hash


def snapshot_and_clear_legacy_access(
    cursor,
    *,
    organization_id: int,
    membership_id: int,
    user_id: int,
    dedicated_operator: bool = False,
) -> dict[str, Any]:
    """Freeze pre-membership ``user_clients`` and make empty mean zero clients."""
    cursor.execute(
        "SELECT id,permission_version,is_active FROM users WHERE id=%s FOR UPDATE",
        (int(user_id),),
    )
    user = cursor.fetchone()
    if not user or not bool(user["is_active"]):
        raise OrganizationError("ORG_INVITEE_INVALID", "当前账号已被停用，无法接受邀请", http_status=403)
    cursor.execute(
        "SELECT brand_id FROM user_clients WHERE user_id=%s ORDER BY brand_id FOR UPDATE",
        (int(user_id),),
    )
    brand_ids = [int(row["brand_id"]) for row in cursor.fetchall()]
    snapshot = {
        "organization_id": int(organization_id),
        "membership_id": int(membership_id),
        "user_id": int(user_id),
        "scope_kind": "invite_operator_empty" if dedicated_operator else "legacy_snapshot",
        "brand_ids": brand_ids,
        "permission_version_before": int(user.get("permission_version") or 0),
    }
    cursor.execute(
        """
        INSERT INTO organization_member_legacy_access_snapshots(
          organization_id,membership_id,user_id,scope_kind,brand_ids,
          permission_version_before,snapshot_hash,status
        ) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,'captured')
        ON CONFLICT(membership_id) DO NOTHING
        """,
        (
            int(organization_id),
            int(membership_id),
            int(user_id),
            snapshot["scope_kind"],
            json.dumps(brand_ids, separators=(",", ":")),
            snapshot["permission_version_before"],
            payload_hash(snapshot),
        ),
    )
    if cursor.rowcount != 1:
        cursor.execute(
            """SELECT user_id,scope_kind,brand_ids,status
               FROM organization_member_legacy_access_snapshots
               WHERE membership_id=%s FOR UPDATE""",
            (int(membership_id),),
        )
        replay = cursor.fetchone()
        if (
            not replay
            or int(replay["user_id"]) != int(user_id)
            or str(replay["scope_kind"]) != snapshot["scope_kind"]
            or sorted(int(value) for value in replay["brand_ids"]) != brand_ids
        ):
            raise OrganizationError(
                "ORG_LEGACY_SNAPSHOT_CONFLICT",
                "员工原客户范围快照冲突",
                http_status=409,
            )
    cursor.execute("DELETE FROM user_clients WHERE user_id=%s", (int(user_id),))
    cursor.execute(
        "UPDATE users SET permission_version=permission_version+1 WHERE id=%s RETURNING permission_version",
        (int(user_id),),
    )
    snapshot["permission_version"] = int(cursor.fetchone()["permission_version"])
    return snapshot


def rotate_permission_version(cursor, *, user_id: int) -> int:
    cursor.execute(
        """UPDATE users SET permission_version=permission_version+1
           WHERE id=%s RETURNING permission_version""",
        (int(user_id),),
    )
    row = cursor.fetchone()
    if not row:
        raise OrganizationError("ORG_MEMBER_INVALID", "员工账号不存在", http_status=409)
    return int(row["permission_version"])


def restore_legacy_access_or_retire_operator(
    cursor,
    *,
    membership_id: int,
    reason: str,
) -> dict[str, Any]:
    """Close the organization projection exactly once at final leave/remove."""
    cursor.execute(
        """SELECT m.user_id,m.organization_id,s.id AS snapshot_id,s.scope_kind,
                  s.brand_ids,s.status AS snapshot_status
           FROM organization_memberships m
           JOIN organization_member_legacy_access_snapshots s ON s.membership_id=m.id
           WHERE m.id=%s FOR UPDATE OF m,s""",
        (int(membership_id),),
    )
    row = cursor.fetchone()
    if not row:
        raise OrganizationError(
            "ORG_LEGACY_SNAPSHOT_MISSING",
            "员工原客户范围快照缺失，已拒绝完成退出",
            http_status=503,
        )
    user_id = int(row["user_id"])
    cursor.execute("SELECT id,is_active FROM users WHERE id=%s FOR UPDATE", (user_id,))
    if not cursor.fetchone():
        raise OrganizationError("ORG_MEMBER_INVALID", "员工账号不存在", http_status=503)
    if row["snapshot_status"] in {"restored", "retired"}:
        cursor.execute("SELECT permission_version FROM users WHERE id=%s", (user_id,))
        return {
            "user_id": user_id,
            "disposition": row["snapshot_status"],
            "permission_version": int(cursor.fetchone()["permission_version"] or 0),
            "replayed": True,
        }

    cursor.execute("DELETE FROM user_clients WHERE user_id=%s", (user_id,))
    if row["scope_kind"] == "invite_operator_empty":
        cursor.execute(
            """UPDATE organization_operator_accounts
               SET status='retired',retired_at=NOW(),retire_reason=%s
               WHERE membership_id=%s AND status='active'""",
            (str(reason), int(membership_id)),
        )
        if cursor.rowcount != 1:
            raise OrganizationError(
                "ORG_OPERATOR_ACCOUNT_ANCHOR_MISSING",
                "员工专用账号锚点缺失，已拒绝完成退出",
                http_status=503,
            )
        cursor.execute(
            """UPDATE users SET is_active=0,permission_version=permission_version+1
               WHERE id=%s RETURNING permission_version""",
            (user_id,),
        )
        disposition = "retired"
    else:
        brand_ids = sorted({int(value) for value in row["brand_ids"]})
        if brand_ids:
            cursor.executemany(
                """INSERT INTO user_clients(user_id,brand_id) VALUES (%s,%s)
                   ON CONFLICT(user_id,brand_id) DO NOTHING""",
                [(user_id, brand_id) for brand_id in brand_ids],
            )
        cursor.execute(
            """UPDATE users SET permission_version=permission_version+1
               WHERE id=%s RETURNING permission_version""",
            (user_id,),
        )
        disposition = "restored"
    permission_version = int(cursor.fetchone()["permission_version"])
    cursor.execute(
        """UPDATE organization_member_legacy_access_snapshots
           SET status=%s,restored_at=NOW() WHERE id=%s AND status='captured'""",
        (disposition, int(row["snapshot_id"])),
    )
    if cursor.rowcount != 1:
        raise OrganizationError(
            "ORG_LEGACY_SNAPSHOT_CONFLICT",
            "员工原客户范围恢复竞态",
            http_status=409,
            retryable=True,
        )
    return {
        "user_id": user_id,
        "disposition": disposition,
        "permission_version": permission_version,
        "replayed": False,
    }
