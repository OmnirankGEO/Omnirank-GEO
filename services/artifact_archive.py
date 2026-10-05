"""ID-only archive previews and recoverable quote lifecycle mutations."""

from __future__ import annotations

from typing import Any, Optional

from db.connection import get_connection


class ArtifactArchiveError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 409, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": False,
            "details": self.details,
        }


def _quote_preview_with_cursor(cursor, quote_id: int, *, lock: bool = False) -> dict:
    cursor.execute(
        f"""
        SELECT q.id, q.brand_id, q.brand_name, q.status, q.service_status,
               q.writing_status, q.paid_at, q.deleted_at,
               (SELECT COUNT(*) FROM keyword_selection_sessions s WHERE s.quote_id=q.id) AS session_count,
               (SELECT COUNT(*) FROM confirmed_keywords k WHERE k.quote_id=q.id) AS keyword_count,
               (SELECT COUNT(*) FROM topics t WHERE t.quote_id=q.id) AS topic_count,
               (SELECT COUNT(*) FROM articles a WHERE a.quote_id=q.id) AS article_count,
                EXISTS(
                    SELECT 1 FROM keyword_selection_sessions protected_session
                    WHERE protected_session.quote_id=q.id
                      AND protected_session.status IN ('confirmed','pending_payment','active','paid')
                ) AS selection_protected,
                (SELECT COUNT(*) FROM keyword_interaction_logs l
                  JOIN keyword_selection_sessions s ON s.id=l.session_id
                 WHERE s.quote_id=q.id) AS interaction_count
        FROM quotes q WHERE q.id=%s
        {"FOR UPDATE OF q" if lock else ""}
        """,
        (quote_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ArtifactArchiveError("QUOTE_NOT_FOUND", "报价不存在。", status=404)
    row = dict(row)
    protected = bool(
        row.get("paid_at")
        or row.get("service_status") == "active"
        or row.get("status") in {"paid", "confirmed", "active"}
        or row.get("selection_protected")
    )
    return {
        "object": {
            "type": "quote",
            "quote_id": int(row["id"]),
            "brand_id": int(row["brand_id"]),
            "display_name": row.get("brand_name") or f"报价 #{row['id']}",
            "status": row.get("status"),
            "writing_status": row.get("writing_status"),
        },
        "associations": {
            "selection_sessions": int(row.get("session_count") or 0),
            "keywords": int(row.get("keyword_count") or 0),
            "topics": int(row.get("topic_count") or 0),
            "articles": int(row.get("article_count") or 0),
            "interaction_events": int(row.get("interaction_count") or 0),
        },
        "impact": [
            "报价从列表归档",
            "公开选词/报价链接立即失效",
            "关键词、主题、文章和审计记录全部保留",
        ],
        "protected": protected,
        "blocked_reason": "已付款、已确认或服务中的报价必须走订单作废/退款流程" if protected else None,
        "already_archived": row.get("deleted_at") is not None,
        "recovery": {
            "action": "restore",
            "method": "POST",
            "target": f"/api/quotes/{int(row['id'])}/restore",
            "permission": "team.output_handoff",
        },
        "_row": row,
    }


def get_quote_archive_preview(quote_id: int) -> dict:
    conn = get_connection()
    try:
        preview = _quote_preview_with_cursor(conn.cursor(), quote_id)
        preview.pop("_row", None)
        return preview
    finally:
        conn.close()


def _lock_organization_quote(cursor, identity: Any, quote_id: int) -> None:
    if identity is None or not getattr(identity, "is_member", False):
        return
    from services.organization_artifacts import lock_artifact_access_in_transaction
    from services.organization_contract import OrganizationError

    try:
        lock_artifact_access_in_transaction(
            cursor, identity, artifact_type="quote", artifact_id=quote_id
        )
    except OrganizationError as exc:
        raise ArtifactArchiveError(
            "QUOTE_NOT_FOUND", "报价不存在或无权访问。", status=404
        ) from exc


def _lock_quote_brand_serialization_point(cursor, quote_id: int) -> int | None:
    """归档/恢复**翻转 canonical 谓词**,所以必须先拿品牌级序列化点。

    🔴 [工单 V5-A · Codex fix-of-fix2 P1-3] 为什么这里也得拿这把锁
    -----------------------------------------------------------
    门户轮换问的是「她看到的那个报价此刻仍是这个品牌的 canonical 吗」,
    而 ``canonical_quote_id()`` 的谓词是 ``deleted_at IS NULL``。
    V4-A 只给三个 **INSERT** 入口上了锁 —— 可 canonical 不只被"多一份报价"改变,
    **归档/恢复同样改变它**,而且改的是同一个谓词的另一半。
    Codex 真 PG16 确定性反例:轮换过了 canonical guard 之后阻塞,
    并发 archive 提交,轮换随后仍给 quote 6 签发新 token ——
    旧 token 被撤销,新的 active token 指向一个**已归档**的报价。

    这是本仓记过的那条:「漏掉的那一处不会让任何判据变红」。
    所以除了补锁,还配了一条**机械 census**:全仓对 quotes 表的更新语句里,
    SET 触及 ``deleted_at / brand_id / created_at`` 的那些是一个**冻结集**,
    新增一处就红。
    (这段话刻意不写出那两个 SQL 关键字的连写形式 —— 写了的话本函数的
     docstring 自己就会被那条 census 扫成一个「疑似 SQL 站点」。
     判据该盯代码,不该盯我的注释。)

    🔴 锁序:**品牌级 advisory 锁永远是本事务拿的第一把锁**,排在
       ``_lock_organization_quote`` 的组织锁与 ``FOR UPDATE`` 行锁之前。
       每条路径最多只拿**一个**品牌的这把锁,所以 advisory ↔ advisory
       之间不可能成环;与后面那些锁的顺序在所有路径上一致,也就不会互相成环。

    🔴 这里读 ``brand_id`` 是**不加锁**读的。它成立的前提是"没有任何生产写者
       改 ``quotes.brand_id``" —— 这个前提不是我说了算,而是上面那条 census
       的一部分:哪天真有人写 ``brand_id=``,census 当场红,由人来决定怎么办。
    """
    cursor.execute("SELECT brand_id FROM quotes WHERE id=%s", (int(quote_id),))
    row = cursor.fetchone()
    if not row:
        # 报价不存在 —— 交给后面那句原有的 404 去说,这里不抢它的话。
        return None
    brand_id = row[0] if isinstance(row, tuple) else row["brand_id"]
    if brand_id is None:
        return None
    from db.diagnosis_db import lock_brand_quote_serialization_point

    lock_brand_quote_serialization_point(cursor, int(brand_id))
    return int(brand_id)


def archive_quote(
    quote_id: int,
    *,
    actor_user_id: int,
    reason: str,
    organization_identity: Any = None,
) -> dict:
    reason = str(reason or "").strip() or "用户归档"
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 🔴 [V5-A · P1-3] 第一把锁 —— 归档要翻 canonical 谓词(deleted_at)。
        _lock_quote_brand_serialization_point(cursor, quote_id)
        _lock_organization_quote(cursor, organization_identity, quote_id)
        preview = _quote_preview_with_cursor(cursor, quote_id, lock=True)
        row = preview["_row"]
        if preview["already_archived"]:
            preview.pop("_row", None)
            return preview
        if preview["protected"]:
            raise ArtifactArchiveError(
                "QUOTE_ARCHIVE_PROTECTED",
                preview["blocked_reason"],
                details={"quote_id": quote_id, "recovery": "order_void_or_refund"},
            )
        cursor.execute(
            """
            UPDATE quotes
            SET status_before_archive=status, status='archived', deleted_at=NOW(),
                archived_by_user_id=%s, archive_reason=%s, updated_at=NOW()
            WHERE id=%s
            """,
            (actor_user_id, reason, quote_id),
        )
        cursor.execute(
            """
            UPDATE keyword_selection_sessions
            SET status_before_archive=status, status='expired', archived_at=NOW(),
                archived_by_user_id=%s, updated_at=NOW()
            WHERE quote_id=%s AND archived_at IS NULL
            """,
            (actor_user_id, quote_id),
        )
        conn.commit()
        preview.pop("_row", None)
        preview["archived"] = True
        preview["reason"] = reason
        return preview
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def restore_quote(
    quote_id: int,
    *,
    actor_user_id: int,
    organization_identity: Any = None,
) -> dict:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 🔴 [V5-A · P1-3] 恢复同样翻 canonical 谓词(deleted_at → NULL),
        #    与归档对称,锁序也必须一致:品牌锁在最前。
        _lock_quote_brand_serialization_point(cursor, quote_id)
        _lock_organization_quote(cursor, organization_identity, quote_id)
        cursor.execute(
            """
            SELECT q.id, q.brand_id, q.status_before_archive, b.deleted_at AS brand_deleted_at
            FROM quotes q JOIN brands b ON b.id=q.brand_id
            WHERE q.id=%s AND q.deleted_at IS NOT NULL
            FOR UPDATE OF q
            """,
            (quote_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise ArtifactArchiveError("QUOTE_ARCHIVE_NOT_FOUND", "未找到可恢复的归档报价。", status=404)
        if row.get("brand_deleted_at") is not None:
            raise ArtifactArchiveError(
                "QUOTE_RESTORE_BRAND_ARCHIVED",
                "客户仍在回收站中，请先恢复客户。",
                details={"brand_id": int(row["brand_id"]), "recovery": "restore_brand_first"},
            )
        cursor.execute(
            """
            UPDATE quotes
            SET status=COALESCE(NULLIF(status_before_archive,''),'draft'),
                status_before_archive=NULL, deleted_at=NULL, archived_by_user_id=NULL,
                archive_reason=NULL, updated_at=NOW()
            WHERE id=%s
            RETURNING id, brand_id, status
            """,
            (quote_id,),
        )
        restored = dict(cursor.fetchone())
        cursor.execute(
            """
            UPDATE keyword_selection_sessions
            SET status=COALESCE(NULLIF(status_before_archive,''),'selecting'),
                status_before_archive=NULL, archived_at=NULL, archived_by_user_id=NULL,
                updated_at=NOW()
            WHERE quote_id=%s AND archived_at IS NOT NULL
            """,
            (quote_id,),
        )
        restored["restored_sessions"] = cursor.rowcount
        restored["actor_user_id"] = actor_user_id
        conn.commit()
        return restored
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
