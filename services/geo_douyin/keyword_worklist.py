"""Read-only image-note worklist over existing confirmed keywords and real works.

Confirmed proposals are eligible inputs, not proof of payment. No generated topic
or delivery slot is written by this read path; keyword identity is never deduped by text.
"""
from __future__ import annotations

from typing import Any


def read_keyword_worklist(cur, *, brand_id: int, tenant_owner_user_id: int | None,
                          limit: int = 100, offset: int = 0) -> dict[str, Any]:
    where = """q.brand_id=%s AND q.status IN ('confirmed','paid') AND q.deleted_at IS NULL
               AND ck.archived_at IS NULL AND ck.is_core IS NOT FALSE
               AND NULLIF(btrim(ck.keyword),'') IS NOT NULL"""
    cur.execute("SELECT count(*) AS n FROM confirmed_keywords ck JOIN quotes q ON q.id=ck.quote_id WHERE " + where,
                (int(brand_id),))
    total = int(cur.fetchone()["n"])
    cur.execute("""
        SELECT ck.id AS confirmed_keyword_id, ck.keyword, ck.required_articles,
               q.id AS quote_id, q.status AS quote_status, q.city AS quote_city,
               p.id AS post_id, p.status AS post_status, p.title AS post_title,
               p.style_key, p.city AS post_city, p.publish_status, p.has_active_task,
               t.id AS topic_id, t.status AS topic_status, t.title AS topic_title,
               COALESCE(completed.n,0) AS produced_count
          FROM confirmed_keywords ck JOIN quotes q ON q.id=ck.quote_id
          LEFT JOIN LATERAL (
              SELECT work.id,work.status,work.title,work.style_key,work.city,work.publish_status,
                     job.id IS NOT NULL AS has_active_task
                FROM geo_douyin_posts work
                LEFT JOIN geo_douyin_post_tasks job ON job.post_id=work.id
                  AND job.status IN ('pending','running') AND job.superseded_at IS NULL
               WHERE work.brand_id=q.brand_id AND work.confirmed_keyword_id=ck.id
                 AND work.deleted_at IS NULL
                 AND (%s IS NULL OR COALESCE(work.tenant_owner_user_id,work.created_by)=%s)
               ORDER BY CASE WHEN job.id IS NOT NULL OR work.status IN ('generating','completing') THEN 0
                             WHEN work.status IN ('ready','publishing','published') THEN 1
                             WHEN work.status='draft' THEN 2 ELSE 3 END,
                        work.id DESC LIMIT 1
          ) p ON TRUE
          LEFT JOIN LATERAL (
              SELECT count(*) AS n FROM geo_douyin_posts work
               WHERE work.brand_id=q.brand_id AND work.confirmed_keyword_id=ck.id
                 AND work.deleted_at IS NULL AND work.status IN ('ready','publishing','published')
                 AND (%s IS NULL OR COALESCE(work.tenant_owner_user_id,work.created_by)=%s)
          ) completed ON TRUE
          LEFT JOIN LATERAL (
              SELECT id,status,title FROM geo_douyin_topics topic
               WHERE topic.brand_id=q.brand_id AND topic.confirmed_keyword_id=ck.id
                 AND topic.status <> 'archived'
               ORDER BY CASE WHEN topic.status='making' THEN 0
                             WHEN topic.status='pending' THEN 1 ELSE 2 END, id DESC LIMIT 1
          ) t ON TRUE
         WHERE """ + where + """
         ORDER BY CASE WHEN q.status='paid' THEN 0 ELSE 1 END,ck.id DESC LIMIT %s OFFSET %s
    """, (tenant_owner_user_id, tenant_owner_user_id, tenant_owner_user_id, tenant_owner_user_id,
          int(brand_id), min(200, max(1, int(limit))), max(0, int(offset))))
    rows = []
    for value in cur.fetchall():
        row = dict(value)
        status = row.get("post_status")
        if row.get("has_active_task"):
            state = "making"
        elif status == "draft":
            state = "incomplete"
        elif status in {"generating", "completing"}:
            state = "making"
        elif status in {"ready", "publishing", "published"}:
            state = "done"
        elif status in {"failed", "error"}:
            state = "failed"
        elif status:
            state = "unknown"
        elif row.get("topic_status") == "making":
            state = "making"
        else:
            state = "pending"
        row["production_status"] = state
        # The topic is usable only before a work exists; the keyword is not a title.
        row["topic_id"] = row["topic_id"] if state == "pending" and row.get("topic_status") == "pending" else None
        rows.append(row)
    return {"keywords": rows, "total": total, "limit": min(200, max(1, int(limit))), "offset": max(0, int(offset))}


def load_keyword_worklist(*, brand_id: int, tenant_owner_user_id: int | None,
                         limit: int = 100, offset: int = 0) -> dict[str, Any]:
    from db.connection import get_connection
    conn = get_connection()
    try:
        return read_keyword_worklist(conn.cursor(), brand_id=brand_id,
                                     tenant_owner_user_id=tenant_owner_user_id, limit=limit, offset=offset)
    finally:
        conn.close()
