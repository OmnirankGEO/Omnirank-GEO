"""Immutable article snapshot captured at the first explicit publish success."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from psycopg2.extras import Json


SUCCESS_PUBLICATION_STATES = frozenset({"published"})
FAILURE_PUBLICATION_STATES = frozenset({"rejected", "withdrawn", "failed", "cancelled"})
SNAPSHOT_VERSION = "publication-snapshot-v1.0"


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record_publication_fact(cursor, *, quote_id, article_id, article_title,
                            platform_name, platform_url, publish_date, operator_id) -> bool:
    """[#104 2026-09-05] 在**同一事务**里把发布事实写进 `media_publications`。

    🔴 背景:真实发布走 mhz 代发,只更新 `articles.first_published_at`(加速列),
    **不写事实源**;唯一会写事实源的 `record_manual_publication` 只被
    「代理手动确认」那个 API 调,而自动写入的两处在 2026-05-25 被有意移除。
    于是 schema 注释说「加速列派生自 media_publications」,实际恰好相反:
    加速列是唯一的真实记录,被声明为事实源的那张表**建表以来零插入**。

    🔴 **字段不齐就不写**:当年移除的理由是「垃圾写入(quote_id=0 + 空字段)」——
    那是数据质量问题,不是「不该写」。所以这里补齐才写,补不齐宁可不写,
    不把当年那批垃圾行重新造出来。返回是否真的插入,便于判据观察。

    🔴 用调用方的 `cursor`,**不自开连接**:本函数在别人的事务里跑,
    自开连接会在深处造出第二个事务 —— 08-10 那次把生产打成 503 就是这么来的。
    """
    if not quote_id or not platform_name or not platform_url:
        return False
    cursor.execute(
        """
        SELECT id FROM media_publications
         WHERE quote_id = %s AND platform_url = %s
           AND (article_id = %s OR (article_id IS NULL AND %s IS NULL))
         LIMIT 1
        """,
        (int(quote_id), str(platform_url), article_id, article_id),
    )
    if cursor.fetchone():
        return False
    cursor.execute(
        """
        INSERT INTO media_publications
              (quote_id, platform_name, platform_url, article_title,
               publish_date, operator_id, article_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (int(quote_id), str(platform_name), str(platform_url),
         article_title or "", publish_date, str(operator_id or ""), article_id),
    )
    return True


def capture_publication_snapshot_with_cursor(
    cursor,
    *,
    article_id: int,
    source: str,
    source_id: int,
    success_state: str,
    observed_at: str | None = None,
    platform_name: str | None = None,
    platform_url: str | None = None,
    title_override: str | None = None,
    content_override: str | None = None,
    body_observation_level: str = "submission_snapshot",
) -> dict[str, Any]:
    """Capture once under the caller transaction; failure states are rejected."""
    state = str(success_state or "").strip().lower()
    if state not in SUCCESS_PUBLICATION_STATES:
        return {
            "captured": False,
            "reason": "non_success_state",
            "state_time_conflict": state in FAILURE_PUBLICATION_STATES,
        }
    cursor.execute(
        """
        SELECT id, title, quote_id, content, version, style, style_code, style_family,
               style_contract_version, style_version, evidence_manifest_hash,
               brand_snapshot_hash, current_content_hash, publication_profile,
               article_review_status, platform_review,
               generation_request_snapshot,
               publication_snapshot_at, publication_snapshot_hash
          FROM articles
         WHERE id = %s
         FOR UPDATE
        """,
        (article_id,),
    )
    row = cursor.fetchone()
    if not row:
        return {"captured": False, "reason": "article_not_found", "state_time_conflict": False}
    if row.get("publication_snapshot_at"):
        return {
            "captured": False,
            "reason": "already_captured",
            "snapshot_hash": row.get("publication_snapshot_hash"),
            "state_time_conflict": False,
        }

    # [统一 R3 · 2026-07-23 §五] 发布冻结快照血缘:传播文章生成期冻结的法律
    # 禁止清单版本(来自 generation_request_snapshot);历史文章无版本 → None,
    # 保持兼容、不以当前版本回填冒充历史判定口径。
    generation_snapshot = row.get("generation_request_snapshot")
    if isinstance(generation_snapshot, str):
        try:
            generation_snapshot = json.loads(generation_snapshot)
        except Exception:
            generation_snapshot = None
    legal_catalog_version = (
        generation_snapshot.get("legal_prohibition_catalog_version")
        if isinstance(generation_snapshot, dict) else None
    )
    legal_catalog_version = (
        str(legal_catalog_version).strip()[:64] if legal_catalog_version else None
    ) or None

    content = str(content_override if content_override is not None else row.get("content") or "")
    content_hash = _sha256(content)
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "article_id": int(article_id),
        "article_version": int(row.get("version") or 1),
        "title": str(title_override if title_override is not None else row.get("title") or ""),
        "content": content,
        "content_hash": content_hash,
        "style_code": row.get("style_code") or row.get("style"),
        "style_family": row.get("style_family"),
        "style_contract_version": row.get("style_contract_version"),
        "style_version": row.get("style_version"),
        "evidence_manifest_hash": row.get("evidence_manifest_hash"),
        "brand_snapshot_hash": row.get("brand_snapshot_hash"),
        "publication_profile": row.get("publication_profile") or "standard",
        "article_review_status": row.get("article_review_status"),
        "body_observation_level": str(body_observation_level or "unknown"),
        "platform_review": row.get("platform_review"),
        "legal_prohibition_catalog_version": legal_catalog_version,
        "source": source,
        "source_id": int(source_id),
        "success_state": state,
        "observed_at": timestamp,
    }
    snapshot_hash = _sha256(_canonical_json(snapshot))
    cursor.execute(
        """
        UPDATE articles
           SET publication_snapshot = %s,
               publication_snapshot_hash = %s,
               publication_snapshot_at = %s,
               publication_snapshot_source = %s,
               publication_snapshot_source_id = %s,
               first_published_at = COALESCE(first_published_at, %s),
               current_content_hash = %s
         WHERE id = %s AND publication_snapshot_at IS NULL
        RETURNING id
        """,
        (
            Json(snapshot), snapshot_hash, timestamp, source, source_id,
            timestamp, content_hash, article_id,
        ),
    )
    captured = cursor.fetchone() is not None
    if captured:
        _record_publication_fact(
            cursor,
            quote_id=row.get("quote_id"),
            article_id=article_id,
            article_title=row.get("title"),
            platform_name=platform_name,
            platform_url=platform_url,
            publish_date=timestamp,
            operator_id=source,
        )
    return {
        "captured": captured,
        "reason": "captured" if captured else "concurrent_capture",
        "snapshot_hash": snapshot_hash,
        "content_hash": content_hash,
        "state_time_conflict": False,
    }


def capture_mhz_publication_snapshot_with_cursor(
    cursor,
    *,
    item_id: int,
    success_state: str,
) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT o.article_id, o.article_title,
               o.article_content_snapshot, o.article_content_snapshot_at,
               o.article_content_snapshot_source,
               i.submitted_title_snapshot, i.submitted_content_snapshot,
               i.submitted_content_snapshot_at,
               i.submitted_content_snapshot_source,
               i.media_name, i.publish_url
          FROM mhz_publish_order_items i
          JOIN mhz_publish_orders o ON o.id = i.order_id
         WHERE i.id = %s
        """,
        (item_id,),
    )
    row = cursor.fetchone()
    if not row or not row.get("article_id"):
        return {"captured": False, "reason": "article_link_missing", "state_time_conflict": False}
    result = capture_publication_snapshot_with_cursor(
        cursor,
        article_id=int(row["article_id"]),
        source="mhz_publish_order_items",
        source_id=int(item_id),
        success_state=success_state,
        # 🔴 [#104] 事实源要的平台与 URL 只有这一层拿得到 ——
        #    被委派的那个函数是**以文章为中心**的,它不知道发到了哪儿。
        #    不传就等于字段不齐,helper 会跳过写入(宁可不写,不造垃圾行)。
        platform_name=row.get("media_name"),
        platform_url=row.get("publish_url"),
        title_override=(row.get("submitted_title_snapshot") or row.get("article_title")),
        content_override=(
            row.get("submitted_content_snapshot")
            if row.get("submitted_content_snapshot_at") is not None
            else row.get("article_content_snapshot")
        ),
        body_observation_level=(
            "channel_submission_snapshot"
            if row.get("submitted_content_snapshot_at") is not None
            else "order_creation_current_body_unverified"
        ),
    )
    result["article_id"] = int(row["article_id"])
    return result
