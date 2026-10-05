"""Durable sidecar delivery plan for the frozen quote-to-writing flow.

The module never calls an LLM, never charges, and never creates a legacy topic.
It mirrors authoritative commercial facts into an idempotent outbox and compiles
an immutable shadow run plus append-only slot events/current projection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import secrets
import uuid
from typing import Any, Mapping

from psycopg2.extras import Json

from services.article_closed_loop_contract import (
    COMPILER_VERSION,
    CONTRACT_VERSION,
    EVENT_SOURCES,
    build_event_key,
    build_source_version,
    canonical_json,
    feature_flags,
    snapshot_hash,
)


logger = logging.getLogger("GEO-Article-Delivery-Plan")

OUTBOX_LEASE_SECONDS = 300
OUTBOX_MAX_ATTEMPTS = 8
OUTBOX_RETRY_SECONDS = 30
OUTBOX_LOCK_TIMEOUT_MS = 250
OUTBOX_STATEMENT_TIMEOUT_MS = 750


class AuthorityNotReady(RuntimeError):
    """The commercial transaction is incomplete; retry without guessing."""


class AuthorityConflict(RuntimeError):
    """The persisted facts are contradictory or outside the signed contract."""


@dataclass(frozen=True)
class AuthorityEvent:
    event_kind: str
    source_kind: str
    source_id: str
    source_version: str
    snapshot_hash: str
    event_key: str
    owner_user_id: int
    brand_id: int
    quote_id: int
    occurred_at: datetime
    snapshot: dict[str, Any]


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    raise TypeError("article delivery plan requires mapping cursor rows")


def _parse_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return value


def _jsonb(value: Any) -> Json:
    """Serialize with the signed canonical JSON rules, including datetimes."""
    return Json(json.loads(canonical_json(value)))


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if value:
        text = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _get_connection():
    from db.diagnosis_db import get_connection

    return get_connection()


def _load_confirmed_keywords(cur, quote_id: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT id AS confirmed_keyword_id, keyword, required_articles,
               monitoring_query, status, created_at
          FROM confirmed_keywords
         WHERE quote_id=%s
           AND (is_core IS NOT FALSE)
         ORDER BY id
        """,
        (int(quote_id),),
    )
    rows = [_row_dict(row) for row in cur.fetchall()]
    if not rows:
        raise AuthorityNotReady("confirmed_keyword_snapshot_empty")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        keyword = str(row.get("keyword") or "").strip()
        if not keyword:
            raise AuthorityConflict("confirmed_keyword_text_empty")
        # [P1 容量合同 2026-08-08] required_articles = **可交付容量上限**,不是必须完成量。
        #   原写法 `1 if None` 是在快照层凭空造一篇容量(= 第二套篇数逻辑)。
        #   生产实测 confirmed_keywords 里 NULL 行 = 0(2280 行全非空),故 fail-closed 零影响,
        #   且把"缺容量"从静默猜值变成显式人工确认(08_billing §11:历史快照缺字段必须人工)。
        raw_required_articles = row.get("required_articles")
        if raw_required_articles is None:
            raise AuthorityConflict("confirmed_keyword_capacity_unset")
        required_articles = int(raw_required_articles)
        if required_articles < 0:
            raise AuthorityConflict("confirmed_keyword_required_articles_negative")
        normalized.append(
            {
                "confirmed_keyword_id": int(row["confirmed_keyword_id"]),
                "keyword": keyword,
                "required_articles": required_articles,
                "monitoring_query": row.get("monitoring_query"),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
            }
        )
    # [P1 容量合同 2026-08-08] 容量为 0 是**合法商业态**(capacity_zero · 设计合同 §6),
    #   不是"数据没准备好"。这里仍然不编译 slot(没有容量就没有交付槽),
    #   但语义从「交付数为零 = 异常」改成「授权容量为零 = 无可编译容量」。
    if sum(int(item["required_articles"]) for item in normalized) <= 0:
        raise AuthorityNotReady("confirmed_keyword_authorized_capacity_zero")
    return normalized


def load_quote_authority_event(
    cur,
    quote_id: int,
    event_kind: str,
    *,
    source_hint: str | None = None,
) -> AuthorityEvent:
    """Load and validate one signed source from current canonical facts."""
    if event_kind not in EVENT_SOURCES:
        raise AuthorityConflict(f"unsupported_event_kind:{event_kind}")
    cur.execute(
        """
        SELECT q.id AS quote_id,q.brand_id,q.owner_user_id AS quote_owner_user_id,
               q.status AS quote_status,q.service_status,q.source_type,q.paid_amount,
               q.writing_status,q.confirmed_at,q.paid_at,q.created_at,
               q.service_start_date,q.service_end_date,
               b.owner_user_id AS brand_owner_user_id,b.is_deleted AS brand_is_deleted
          FROM quotes q
          JOIN brands b ON b.id=q.brand_id
         WHERE q.id=%s
        """,
        (int(quote_id),),
    )
    quote = _row_dict(cur.fetchone())
    if not quote:
        raise AuthorityConflict("quote_not_found")
    if bool(quote.get("brand_is_deleted")):
        raise AuthorityConflict("brand_archived")
    quote_owner = quote.get("quote_owner_user_id")
    brand_owner = quote.get("brand_owner_user_id")
    if quote_owner is not None and brand_owner is not None and int(quote_owner) != int(brand_owner):
        raise AuthorityConflict("quote_brand_owner_mismatch")
    owner_user_id = quote_owner if quote_owner is not None else brand_owner
    if owner_user_id is None:
        raise AuthorityConflict("tenant_owner_missing")
    keywords = _load_confirmed_keywords(cur, int(quote_id))
    common = {
        "quote_id": int(quote_id),
        "owner_user_id": int(owner_user_id),
        "brand_id": int(quote["brand_id"]),
        "quote_status": str(quote.get("quote_status") or ""),
        "service_status": str(quote.get("service_status") or ""),
        "source_type": str(quote.get("source_type") or ""),
        "paid_amount": float(quote.get("paid_amount") or 0),
        "writing_status": str(quote.get("writing_status") or ""),
        "service_start_date": quote.get("service_start_date"),
        "service_end_date": quote.get("service_end_date"),
        "confirmed_keywords": keywords,
    }

    source_id: str
    occurred_at: datetime
    if event_kind == "quote_paid_standard":
        cur.execute(
            """
            SELECT id,status,confirmed_at,payment_received_at,updated_at
              FROM keyword_selection_sessions
             WHERE quote_id=%s
             ORDER BY id DESC LIMIT 1
            """,
            (int(quote_id),),
        )
        session = _row_dict(cur.fetchone())
        if not session or str(session.get("status") or "") != "active":
            raise AuthorityNotReady("standard_session_not_active")
        if common["quote_status"] != "confirmed" or common["service_status"] != "active":
            raise AuthorityNotReady("standard_quote_not_confirmed_active")
        snapshot = {
            "session_id": int(session["id"]),
            **{key: common[key] for key in ("quote_id", "owner_user_id", "brand_id")},
            "session_status": "active",
            "quote_status": common["quote_status"],
            "service_status": common["service_status"],
            "confirmed_keywords": keywords,
        }
        source_id = str(session["id"])
        occurred_at = _utc_datetime(
            session.get("payment_received_at") or session.get("confirmed_at") or quote.get("paid_at") or quote.get("confirmed_at")
        )
    elif event_kind in {"quote_paid_offline", "quote_paid_agent_activation"}:
        if common["quote_status"] not in {"paid", "active"} or common["service_status"] != "active":
            raise AuthorityNotReady("paid_quote_not_active")
        if source_hint and source_hint not in {event_kind, EVENT_SOURCES[event_kind].source_kind}:
            raise AuthorityConflict("activation_source_hint_mismatch")
        if event_kind == "quote_paid_offline":
            snapshot = {
                **{key: common[key] for key in ("quote_id", "owner_user_id", "brand_id")},
                "quote_status": common["quote_status"],
                "service_status": common["service_status"],
                "source_type": common["source_type"],
                "confirmed_keywords": keywords,
            }
        else:
            snapshot = {
                **{key: common[key] for key in ("quote_id", "owner_user_id", "brand_id")},
                "quote_status": common["quote_status"],
                "service_status": common["service_status"],
                "service_start_date": common["service_start_date"],
                "service_end_date": common["service_end_date"],
                "confirmed_keywords": keywords,
            }
        source_id = str(quote_id)
        occurred_at = _utc_datetime(quote.get("paid_at") or quote.get("confirmed_at"))
    elif event_kind == "zero_price_writing_project_created":
        if common["source_type"] not in {"c_end_geo_plan", "quick_writing"}:
            raise AuthorityConflict("zero_price_source_type_invalid")
        if common["quote_status"] != "confirmed" or common["paid_amount"] != 0 or common["writing_status"] != "pending":
            raise AuthorityNotReady("zero_price_quote_terminal_not_ready")
        snapshot = {
            **{key: common[key] for key in ("quote_id", "owner_user_id", "brand_id", "source_type")},
            "quote_status": common["quote_status"],
            "paid_amount": common["paid_amount"],
            "writing_status": common["writing_status"],
            "confirmed_keywords": keywords,
        }
        source_id = f"{quote_id}:{common['source_type']}"
        occurred_at = _utc_datetime(quote.get("confirmed_at") or quote.get("created_at"))
    elif event_kind in {"contract_add_on", "keyword_reassigned"}:
        if common["quote_status"] not in {"confirmed", "paid", "active"}:
            raise AuthorityNotReady("contract_change_quote_not_active")
        snapshot = {
            **{key: common[key] for key in ("quote_id", "owner_user_id", "brand_id")},
            "quote_status": common["quote_status"],
            "confirmed_keywords": keywords,
        }
        source_id = str(quote_id)
        occurred_at = max((_utc_datetime(item.get("created_at")) for item in keywords), default=datetime.now(timezone.utc))
    else:
        raise AuthorityConflict("publication_event_requires_canonical_publication_writer")

    source_version, source_snapshot_hash = build_source_version(event_kind, snapshot)
    identity_fields = EVENT_SOURCES[event_kind].source_identity_fields
    identity_values: dict[str, Any] = {}
    for field in identity_fields:
        if field == "session_id":
            identity_values[field] = snapshot["session_id"]
        elif field == "source_type":
            identity_values[field] = snapshot["source_type"]
        else:
            identity_values[field] = snapshot[field]
    event_key = build_event_key(event_kind, identity_values, source_version)
    return AuthorityEvent(
        event_kind=event_kind,
        source_kind=EVENT_SOURCES[event_kind].source_kind,
        source_id=source_id,
        source_version=source_version,
        snapshot_hash=source_snapshot_hash,
        event_key=event_key,
        owner_user_id=int(owner_user_id),
        brand_id=int(quote["brand_id"]),
        quote_id=int(quote_id),
        occurred_at=occurred_at,
        snapshot=snapshot,
    )


def _insert_event_cursor(cur, event: AuthorityEvent) -> int:
    cur.execute(
        """
        INSERT INTO geo_article_plan_outbox (
            event_key,event_kind,source_kind,source_id,source_version,source_snapshot_hash,
            owner_user_id,brand_id,quote_id,authority_snapshot,occurred_at,observed_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (event_key) DO NOTHING
        RETURNING id
        """,
        (
            event.event_key,
            event.event_kind,
            event.source_kind,
            event.source_id,
            event.source_version,
            event.snapshot_hash,
            event.owner_user_id,
            event.brand_id,
            event.quote_id,
            _jsonb(event.snapshot),
            event.occurred_at,
        ),
    )
    row = cur.fetchone()
    if row:
        return int(_row_dict(row).get("id") if isinstance(row, Mapping) else row[0])
    cur.execute("SELECT id FROM geo_article_plan_outbox WHERE event_key=%s", (event.event_key,))
    existing = cur.fetchone()
    return int(_row_dict(existing).get("id") if isinstance(existing, Mapping) else existing[0])


def enqueue_event_in_business_transaction(cur, event: AuthorityEvent) -> dict[str, Any]:
    """Best-effort savepoint writer; failure cannot poison the commercial transaction."""
    cur.execute("SAVEPOINT geo_article_plan_outbox_sp")
    try:
        cur.execute(
            "SELECT current_setting('lock_timeout') AS lock_timeout,"
            "current_setting('statement_timeout') AS statement_timeout"
        )
        setting_row = cur.fetchone()
        if isinstance(setting_row, Mapping):
            previous_lock_timeout = setting_row["lock_timeout"]
            previous_statement_timeout = setting_row["statement_timeout"]
        else:
            previous_lock_timeout, previous_statement_timeout = setting_row
        cur.execute(f"SET LOCAL lock_timeout='{OUTBOX_LOCK_TIMEOUT_MS}ms'")
        cur.execute(f"SET LOCAL statement_timeout='{OUTBOX_STATEMENT_TIMEOUT_MS}ms'")
        outbox_id = _insert_event_cursor(cur, event)
        cur.execute("SELECT set_config('lock_timeout',%s,true)", (str(previous_lock_timeout),))
        cur.execute("SELECT set_config('statement_timeout',%s,true)", (str(previous_statement_timeout),))
        cur.execute("RELEASE SAVEPOINT geo_article_plan_outbox_sp")
        return {"enqueued": True, "outbox_id": outbox_id, "event_key": event.event_key}
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT geo_article_plan_outbox_sp")
        cur.execute("RELEASE SAVEPOINT geo_article_plan_outbox_sp")
        logger.exception("article plan outbox savepoint failed quote=%s kind=%s", event.quote_id, event.event_kind)
        return {"enqueued": False, "reason": type(exc).__name__}


def enqueue_quote_event_durable(quote_id: int, event_kind: str, *, source_hint: str | None = None) -> dict[str, Any]:
    """Independent bridge for existing multi-connection/multi-commit routes."""
    if not feature_flags()["ARTICLE_PLAN_EVENT_OUTBOX_ENABLED"]:
        return {"enqueued": False, "reason": "flag_disabled"}
    conn = _get_connection()
    try:
        cur = conn.cursor()
        event = load_quote_authority_event(cur, int(quote_id), event_kind, source_hint=source_hint)
        result = enqueue_event_in_business_transaction(cur, event)
        conn.commit()
        return result
    except (AuthorityNotReady, AuthorityConflict) as exc:
        conn.rollback()
        return {"enqueued": False, "reason": str(exc)}
    except Exception as exc:
        conn.rollback()
        logger.exception("durable article plan event failed quote=%s kind=%s", quote_id, event_kind)
        return {"enqueued": False, "reason": type(exc).__name__}
    finally:
        conn.close()


def enqueue_quote_event_in_transaction_if_enabled(
    cur,
    quote_id: int,
    event_kind: str,
    *,
    source_hint: str | None = None,
) -> dict[str, Any]:
    """Attach an outbox fact to an existing business transaction without blocking it.

    The caller owns commit/rollback.  Every failure is contained by the savepoint;
    the reconciler remains the recovery path for an omitted or failed sidecar write.

    🔴 本仓有**两套语义相反**的 outbox 入队形态,都正确 —— 裁定 2026-08-21
    ---------------------------------------------------------------------
    本函数 = **fail-OPEN**(flag 关或任何异常都只返回 ``enqueued: False``)。
    对照组 = ``services/defensive_geo/activation_outbox.enqueue_activation``,
    它是 **fail-CLOSED**(失败即抛,业务事务一起回滚)。

    **判别标准 = 事实丢了,reconciler 能不能从别处重建?**
    本函数这条:能 —— :func:`reconcile_authoritative_quotes` 从 quote /
    confirmed_keywords 这些独立权威源重算计划应有的样子,outbox 只是加速器;
    丢一条不丢事实,所以不该拿旁挂 sidecar 去炸主事务。

    ⚠️ **照抄本函数形态前先回答两个问题**:①事实丢了能否重建?
    ②失败会不会以"成功形状"(一个正常返回值而非异常)交给调用方?
    资金/身份相邻的主链上,②的答案使 fail-open 不可接受 ——
    详见 ``activation_outbox`` 模块 docstring 的完整裁定记录。
    """
    if not feature_flags()["ARTICLE_PLAN_EVENT_OUTBOX_ENABLED"]:
        return {"enqueued": False, "reason": "flag_disabled"}
    cur.execute("SAVEPOINT geo_article_quote_event_sp")
    try:
        event = load_quote_authority_event(cur, int(quote_id), event_kind, source_hint=source_hint)
        result = enqueue_event_in_business_transaction(cur, event)
        cur.execute("RELEASE SAVEPOINT geo_article_quote_event_sp")
        return result
    except (AuthorityNotReady, AuthorityConflict) as exc:
        cur.execute("ROLLBACK TO SAVEPOINT geo_article_quote_event_sp")
        cur.execute("RELEASE SAVEPOINT geo_article_quote_event_sp")
        logger.warning(
            "article plan event not ready in business transaction quote=%s kind=%s reason=%s",
            quote_id,
            event_kind,
            exc,
        )
        return {"enqueued": False, "reason": str(exc)}
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT geo_article_quote_event_sp")
        cur.execute("RELEASE SAVEPOINT geo_article_quote_event_sp")
        logger.exception(
            "article plan event bridge failed in business transaction quote=%s kind=%s",
            quote_id,
            event_kind,
        )
        return {"enqueued": False, "reason": type(exc).__name__}


def enqueue_publication_locked_in_transaction_if_enabled(
    cur,
    *,
    publication_source: str,
    publication_source_id: int,
    article_id: int,
    platform: str,
    provider_receipt: str,
    public_url: str,
    submitted_content_hash: str,
    published_at: Any = None,
) -> dict[str, Any]:
    """Best-effort publication lineage that cannot poison the fact writer."""
    if not feature_flags()["ARTICLE_PLAN_EVENT_OUTBOX_ENABLED"]:
        return {"enqueued": False, "reason": "flag_disabled"}
    cur.execute("SAVEPOINT geo_article_publication_outbox_sp")
    try:
        result = _enqueue_publication_locked_with_cursor(
            cur,
            publication_source=publication_source,
            publication_source_id=publication_source_id,
            article_id=article_id,
            platform=platform,
            provider_receipt=provider_receipt,
            public_url=public_url,
            submitted_content_hash=submitted_content_hash,
            published_at=published_at,
        )
        cur.execute("RELEASE SAVEPOINT geo_article_publication_outbox_sp")
        return result
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT geo_article_publication_outbox_sp")
        cur.execute("RELEASE SAVEPOINT geo_article_publication_outbox_sp")
        logger.exception(
            "publication lineage sidecar failed without affecting publication source=%s id=%s",
            publication_source,
            publication_source_id,
        )
        return {"enqueued": False, "reason": type(exc).__name__}


def _enqueue_publication_locked_with_cursor(
    cur,
    *,
    publication_source: str,
    publication_source_id: int,
    article_id: int,
    platform: str,
    provider_receipt: str,
    public_url: str,
    submitted_content_hash: str,
    published_at: Any = None,
) -> dict[str, Any]:
    """Mirror only a strict successful publication fact linked to a sidecar slot."""
    url = str(public_url or "").strip()
    content_hash = str(submitted_content_hash or "").strip().lower()
    if not url or len(content_hash) != 64:
        return {"enqueued": False, "reason": "strict_publication_fact_incomplete"}
    cur.execute(
        """
        SELECT a.id,a.quote_id,a.delivery_slot_key,q.brand_id,
               COALESCE(q.owner_user_id,b.owner_user_id) AS owner_user_id,b.is_deleted
          FROM articles a
          JOIN quotes q ON q.id=a.quote_id
          JOIN brands b ON b.id=q.brand_id
          JOIN geo_article_delivery_slots s
            ON s.delivery_slot_key=a.delivery_slot_key AND s.quote_id=a.quote_id
         WHERE a.id=%s AND s.current_state IN ('active','blocked')
         FOR UPDATE OF s
        """,
        (int(article_id),),
    )
    scope = _row_dict(cur.fetchone())
    if not scope:
        return {"enqueued": False, "reason": "article_not_sidecar_linked"}
    if bool(scope.get("is_deleted")) or scope.get("owner_user_id") is None:
        return {"enqueued": False, "reason": "tenant_scope_unavailable"}
    snapshot = {
        "publication_source": str(publication_source),
        "publication_source_id": int(publication_source_id),
        "owner_user_id": int(scope["owner_user_id"]),
        "brand_id": int(scope["brand_id"]),
        "quote_id": int(scope["quote_id"]),
        "article_id": int(article_id),
        "status": "published",
        "platform": str(platform or "unknown"),
        "provider_receipt": str(provider_receipt or "").strip(),
        "public_url": url,
        "submitted_content_hash": content_hash,
        "published_at": _utc_datetime(published_at),
        "delivery_slot_key": str(scope["delivery_slot_key"]),
    }
    if not snapshot["provider_receipt"]:
        return {"enqueued": False, "reason": "provider_receipt_missing"}
    source_version, source_snapshot_hash = build_source_version("publication_locked", snapshot)
    event = AuthorityEvent(
        event_kind="publication_locked",
        source_kind=EVENT_SOURCES["publication_locked"].source_kind,
        source_id=f"{publication_source}:{int(publication_source_id)}",
        source_version=source_version,
        snapshot_hash=source_snapshot_hash,
        event_key=build_event_key(
            "publication_locked",
            {
                "publication_source": str(publication_source),
                "publication_source_id": int(publication_source_id),
            },
            source_version,
        ),
        owner_user_id=int(scope["owner_user_id"]),
        brand_id=int(scope["brand_id"]),
        quote_id=int(scope["quote_id"]),
        occurred_at=snapshot["published_at"],
        snapshot=snapshot,
    )
    return enqueue_event_in_business_transaction(cur, event)


def _desired_assignments(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把冻结容量展开成交付槽。**槽 = 容量上限,不是交付义务** —— 开出来的槽允许一直空着
    (0..capacity),用不满由 P4 缺口计划带原因回流,见 services/article_capacity_contract。
    """
    desired: list[dict[str, Any]] = []
    for keyword in snapshot.get("confirmed_keywords") or []:
        keyword_id = int(keyword["confirmed_keyword_id"])
        # [P1 2026-08-08] 快照在 _load_confirmed_keywords 已规整过,这里再猜一次 `1 if None`
        #   就是同一份逻辑的第二份实现(两处独立手写 = 改一处另一处静默不跟)。改成硬失败。
        raw_count = keyword.get("required_articles")
        if raw_count is None:
            raise AuthorityConflict("confirmed_keyword_capacity_unset")
        count = int(raw_count)
        for keyword_ordinal in range(1, count + 1):
            desired.append(
                {
                    "keyword_id": keyword_id,
                    "keyword": keyword["keyword"],
                    "keyword_ordinal": keyword_ordinal,
                    "monitoring_query_present": bool(str(keyword.get("monitoring_query") or "").strip()),
                }
            )
    return desired


def _existing_slots(cur, quote_id: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT delivery_slot_key,contract_revision_id,contract_ordinal,current_state,
               projection_version,current_event_id,last_event_key,keyword_id,topic_id,article_id,
               created_at
          FROM geo_article_delivery_slots
         WHERE quote_id=%s AND current_state IN ('active','blocked')
         ORDER BY created_at,delivery_slot_key
         FOR UPDATE
        """,
        (int(quote_id),),
    )
    return [_row_dict(row) for row in cur.fetchall()]


def _allocate_slot_keys(existing: list[dict[str, Any]], desired: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(desired) < len(existing):
        raise AuthorityConflict("delivery_count_decrease_requires_authoritative_cancel_or_refund_detail")
    unused = list(existing)
    allocated: list[dict[str, Any]] = []
    for contract_ordinal, assignment in enumerate(desired, start=1):
        match = next((slot for slot in unused if slot.get("keyword_id") == assignment["keyword_id"]), None)
        if match is None and unused:
            match = unused[0]
        if match is not None:
            unused.remove(match)
            slot_key = str(match["delivery_slot_key"])
            prior = match
        else:
            slot_key = str(uuid.uuid4())
            prior = None
        allocated.append(
            {
                **assignment,
                "contract_ordinal": contract_ordinal,
                "delivery_slot_key": slot_key,
                "prior": prior,
            }
        )
    return allocated


def _legacy_comparison(cur, quote_id: int, desired_count: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT COUNT(*) AS topic_count,
               COUNT(*) FILTER (WHERE article_id IS NOT NULL) AS article_count,
               COUNT(DISTINCT keyword_id) FILTER (WHERE keyword_id IS NOT NULL) AS covered_keywords
          FROM topics WHERE quote_id=%s
        """,
        (int(quote_id),),
    )
    row = _row_dict(cur.fetchone())
    topic_count = int(row.get("topic_count") or 0)
    return {
        "contract_delivery_count": desired_count,
        "legacy_topic_count": topic_count,
        "legacy_article_count": int(row.get("article_count") or 0),
        "legacy_covered_keywords": int(row.get("covered_keywords") or 0),
        "delivery_count_delta": topic_count - desired_count,
        "commercial_invariant_verdict": "PASS" if topic_count in {0, desired_count} else "FAIL",
        "canary_verdict": "INSUFFICIENT_SAMPLES",
    }


def compile_shadow_plan(cur, outbox_row: Mapping[str, Any]) -> dict[str, Any]:
    """Compile once under quote lock; repeat and kill-9 replay return one run."""
    outbox = dict(outbox_row)
    quote_id = int(outbox["quote_id"])
    cur.execute("SELECT id FROM quotes WHERE id=%s FOR UPDATE", (quote_id,))
    if not cur.fetchone():
        raise AuthorityConflict("quote_not_found_during_compile")
    snapshot = _parse_json(outbox["authority_snapshot"])
    if not isinstance(snapshot, dict):
        raise AuthorityConflict("authority_snapshot_not_object")
    reconstructed_version, reconstructed_hash = build_source_version(str(outbox["event_kind"]), snapshot)
    if (
        reconstructed_hash != str(outbox["source_snapshot_hash"])
        or reconstructed_version != str(outbox["source_version"])
    ):
        raise AuthorityConflict("authority_snapshot_hash_mismatch")
    desired = _desired_assignments(snapshot)
    if not desired:
        raise AuthorityNotReady("delivery_count_zero")
    revision_key = snapshot_hash(
        {"contract_version": CONTRACT_VERSION, "event_key": str(outbox["event_key"]), "source_version": str(outbox["source_version"])}
    )
    cur.execute(
        """
        INSERT INTO geo_article_contract_revisions (
            revision_key,source_event_key,source_version,owner_user_id,brand_id,quote_id,
            authority_snapshot,authority_snapshot_hash,delivery_count,contract_version
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (revision_key) DO NOTHING RETURNING id
        """,
        (
            revision_key,
            outbox["event_key"],
            outbox["source_version"],
            int(outbox["owner_user_id"]),
            int(outbox["brand_id"]),
            quote_id,
            _jsonb(snapshot),
            outbox["source_snapshot_hash"],
            len(desired),
            CONTRACT_VERSION,
        ),
    )
    row = cur.fetchone()
    if row:
        revision_id = int(_row_dict(row).get("id") if isinstance(row, Mapping) else row[0])
    else:
        cur.execute("SELECT id FROM geo_article_contract_revisions WHERE revision_key=%s", (revision_key,))
        existing_revision = cur.fetchone()
        revision_id = int(_row_dict(existing_revision).get("id") if isinstance(existing_revision, Mapping) else existing_revision[0])

    run_key = snapshot_hash(
        {"revision_key": revision_key, "compiler_version": COMPILER_VERSION, "run_mode": "shadow"}
    )
    cur.execute("SELECT * FROM geo_article_plan_runs WHERE run_key=%s FOR UPDATE", (run_key,))
    existing_run = cur.fetchone()
    if existing_run and str(_row_dict(existing_run).get("status")) == "completed":
        return _row_dict(existing_run)

    current_slots = _existing_slots(cur, quote_id)
    allocated = _allocate_slot_keys(current_slots, desired)
    public_slots = [
        {
            "delivery_slot_key": item["delivery_slot_key"],
            "contract_ordinal": item["contract_ordinal"],
            "keyword_id": item["keyword_id"],
            "keyword": item["keyword"],
            "keyword_ordinal": item["keyword_ordinal"],
            "monitoring_query_present": item["monitoring_query_present"],
        }
        for item in allocated
    ]
    output_snapshot = {
        "contract_version": CONTRACT_VERSION,
        "compiler_version": COMPILER_VERSION,
        "quote_id": quote_id,
        "delivery_count": len(public_slots),
        "slots": public_slots,
        "automatic_title_generation": False,
        "automatic_article_generation": False,
        "automatic_billing": False,
    }
    comparison = _legacy_comparison(cur, quote_id, len(public_slots))
    input_hash = snapshot_hash(snapshot)
    output_hash = snapshot_hash(output_snapshot)
    if existing_run:
        run_id = int(_row_dict(existing_run)["id"])
    else:
        cur.execute(
            """
            INSERT INTO geo_article_plan_runs (
                run_key,contract_revision_id,source_outbox_id,owner_user_id,brand_id,quote_id,
                run_mode,compiler_version,input_snapshot,input_snapshot_hash,status,verdict
            ) VALUES (%s,%s,%s,%s,%s,%s,'shadow',%s,%s,%s,'pending','NOT_EVALUATED')
            RETURNING id
            """,
            (
                run_key,
                revision_id,
                int(outbox["id"]),
                int(outbox["owner_user_id"]),
                int(outbox["brand_id"]),
                quote_id,
                COMPILER_VERSION,
                _jsonb(snapshot),
                input_hash,
            ),
        )
        inserted_run = cur.fetchone()
        run_id = int(_row_dict(inserted_run).get("id") if isinstance(inserted_run, Mapping) else inserted_run[0])

    for item in allocated:
        prior = item["prior"]
        slot_key = item["delivery_slot_key"]
        slot_version = int(prior.get("projection_version") or 0) + 1 if prior else 1
        event_kind = "created" if prior is None else "reassigned"
        # A contract revision may rebind a keyword, but it is not an operator
        # resolution.  Preserve an existing blocked state until the explicit
        # unblock command records its evidence.
        target_state = str(prior.get("current_state") or "active") if prior else "active"
        event_key = snapshot_hash(
            {
                "slot_event_version": "geo-article-slot-events-v1",
                "delivery_slot_key": slot_key,
                "slot_version": slot_version,
                "revision_key": revision_key,
                "keyword_id": item["keyword_id"],
                "target_state": target_state,
            }
        )
        cur.execute(
            """
            INSERT INTO geo_article_delivery_slot_events (
                event_key,delivery_slot_key,slot_version,event_kind,target_state,
                contract_revision_id,plan_run_id,owner_user_id,brand_id,quote_id,
                source_version,payload,occurred_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_key) DO NOTHING RETURNING id
            """,
            (
                event_key,
                slot_key,
                slot_version,
                event_kind,
                target_state,
                revision_id,
                run_id,
                int(outbox["owner_user_id"]),
                int(outbox["brand_id"]),
                quote_id,
                outbox["source_version"],
                _jsonb({"keyword_id": item["keyword_id"], "contract_ordinal": item["contract_ordinal"]}),
                outbox["occurred_at"],
            ),
        )
        event_row = cur.fetchone()
        if event_row:
            event_id = int(_row_dict(event_row).get("id") if isinstance(event_row, Mapping) else event_row[0])
        else:
            cur.execute("SELECT id FROM geo_article_delivery_slot_events WHERE event_key=%s", (event_key,))
            event_existing = cur.fetchone()
            event_id = int(_row_dict(event_existing).get("id") if isinstance(event_existing, Mapping) else event_existing[0])
        if prior is None:
            cur.execute(
                """
                INSERT INTO geo_article_delivery_slots (
                    delivery_slot_key,contract_revision_id,contract_ordinal,owner_user_id,brand_id,
                    quote_id,current_state,projection_version,current_event_id,last_event_key,keyword_id
                ) VALUES (%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s)
                """,
                (
                    slot_key,
                    revision_id,
                    item["contract_ordinal"],
                    int(outbox["owner_user_id"]),
                    int(outbox["brand_id"]),
                    quote_id,
                    slot_version,
                    event_id,
                    event_key,
                    item["keyword_id"],
                ),
            )
        else:
            cur.execute(
                """
                UPDATE geo_article_delivery_slots
                   SET contract_revision_id=%s,contract_ordinal=%s,current_state=%s,
                       projection_version=%s,current_event_id=%s,last_event_key=%s,keyword_id=%s,
                       updated_at=NOW()
                 WHERE delivery_slot_key=%s AND projection_version=%s
                """,
                (
                    revision_id,
                    item["contract_ordinal"],
                    target_state,
                    slot_version,
                    event_id,
                    event_key,
                    item["keyword_id"],
                    slot_key,
                    slot_version - 1,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("slot_projection_cas_failed")

    cur.execute(
        """
        UPDATE geo_article_plan_runs
           SET output_snapshot=%s,output_snapshot_hash=%s,comparison_snapshot=%s,
               status='completed',verdict='INSUFFICIENT_SAMPLES',finished_at=NOW()
         WHERE id=%s AND status='pending'
        RETURNING *
        """,
        (_jsonb(output_snapshot), output_hash, _jsonb(comparison), run_id),
    )
    completed = cur.fetchone()
    if not completed:
        raise RuntimeError("shadow_run_completion_cas_failed")
    return _row_dict(completed)


def claim_plan_events(limit: int = 20) -> tuple[str, list[dict[str, Any]]]:
    token = secrets.token_urlsafe(24)
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH due AS (
                SELECT id
                  FROM geo_article_plan_outbox
                 WHERE manual_resolution_status IS NULL
                   AND (
                       (status='pending' AND available_at<=NOW())
                       OR (status='processing' AND claimed_at<NOW()-(%s * INTERVAL '1 second'))
                   )
                 ORDER BY available_at,id
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
            UPDATE geo_article_plan_outbox o
               SET status='processing',claim_token=%s,claimed_at=NOW(),
                   attempt_count=o.attempt_count+1,updated_at=NOW()
              FROM due WHERE o.id=due.id
            RETURNING o.*
            """,
            (OUTBOX_LEASE_SECONDS, max(1, min(int(limit), 100)), token),
        )
        rows = [_row_dict(row) for row in cur.fetchall()]
        conn.commit()
        return token, rows
    finally:
        conn.close()


def _mark_processing_failure(outbox_id: int, claim_token: str, error: str, attempt_count: int) -> None:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        dead = int(attempt_count) >= OUTBOX_MAX_ATTEMPTS
        cur.execute(
            """
            UPDATE geo_article_plan_outbox
               SET status=%s,available_at=NOW()+(%s * INTERVAL '1 second'),
                   claim_token=NULL,claimed_at=NULL,last_error=%s,
                   manual_resolution_status=CASE WHEN %s THEN 'required' ELSE NULL END,
                   updated_at=NOW()
             WHERE id=%s AND status='processing' AND claim_token=%s
            """,
            ("dead_letter" if dead else "pending", OUTBOX_RETRY_SECONDS, error[:2000], dead, int(outbox_id), claim_token),
        )
        conn.commit()
    finally:
        conn.close()


def _process_publication_locked(cur, outbox: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _parse_json(outbox.get("authority_snapshot")) or {}
    source = str(snapshot.get("publication_source") or "")
    source_id = int(snapshot.get("publication_source_id") or 0)
    article_id = int(snapshot.get("article_id") or 0)
    public_url = str(snapshot.get("public_url") or "").strip()
    if source == "mhz_publish_order_items":
        cur.execute(
            """
            SELECT o.article_id,i.status,i.publish_url,i.published_at
              FROM mhz_publish_order_items i JOIN mhz_publish_orders o ON o.id=i.order_id
             WHERE i.id=%s
            """,
            (source_id,),
        )
        fact = _row_dict(cur.fetchone())
        fact_url = fact.get("publish_url")
        fact_published_at = fact.get("published_at")
    elif source == "publish_order_items":
        cur.execute(
            """
            SELECT o.article_id,i.status,i.publish_url,i.published_at
              FROM publish_order_items i JOIN publish_orders o ON o.id=i.order_id
             WHERE i.id=%s
            """,
            (source_id,),
        )
        fact = _row_dict(cur.fetchone())
        fact_url = fact.get("publish_url")
        fact_published_at = fact.get("published_at")
    elif source == "publish_records":
        cur.execute(
            """
            SELECT article_id,status,public_url,public_url_verification_state,created_at
              FROM publish_records WHERE id=%s
            """,
            (source_id,),
        )
        fact = _row_dict(cur.fetchone())
        fact_url = fact.get("public_url")
        fact_published_at = fact.get("created_at")
        # [WO 自报收口 2026-08-19] 交付槽投影是**客户可见的核验链**,权威来源必须是
        # 服务端核实过的事实;浏览器"显式回报过"不构成权威。
        if str(fact.get("public_url_verification_state") or "") != "verified":
            raise AuthorityConflict("publication_public_url_not_server_verified")
    else:
        raise AuthorityConflict("publication_source_not_strict")
    expected_status = "success" if source == "publish_records" else "published"
    if (
        not fact
        or int(fact.get("article_id") or 0) != article_id
        or str(fact.get("status") or "").lower() != expected_status
        or str(fact_url or "").strip() != public_url
        or _utc_datetime(fact_published_at) != _utc_datetime(snapshot.get("published_at"))
    ):
        raise AuthorityConflict("publication_authority_changed_or_incomplete")

    cur.execute(
        """
        SELECT s.*,e.plan_run_id AS current_plan_run_id
          FROM geo_article_delivery_slots s
          JOIN geo_article_delivery_slot_events e ON e.id=s.current_event_id
         WHERE s.delivery_slot_key=%s AND s.quote_id=%s
           AND EXISTS (
               SELECT 1 FROM articles a
                WHERE a.id=%s AND a.quote_id=s.quote_id
                  AND a.delivery_slot_key=s.delivery_slot_key
           )
         FOR UPDATE OF s
        """,
        (snapshot.get("delivery_slot_key"), int(outbox["quote_id"]), article_id),
    )
    slot = _row_dict(cur.fetchone())
    if not slot:
        raise AuthorityConflict("publication_slot_binding_missing")
    next_version = int(slot.get("projection_version") or 0) + 1
    slot_event_key = snapshot_hash(
        {
            "outbox_event_key": outbox["event_key"],
            "delivery_slot_key": str(slot["delivery_slot_key"]),
            "slot_version": next_version,
            "event_kind": "publication_locked",
        }
    )
    cur.execute(
        """
        INSERT INTO geo_article_delivery_slot_events (
            event_key,delivery_slot_key,slot_version,event_kind,target_state,
            contract_revision_id,plan_run_id,owner_user_id,brand_id,quote_id,
            source_version,payload,occurred_at
        ) VALUES (%s,%s,%s,'publication_locked',%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (event_key) DO UPDATE SET event_key=EXCLUDED.event_key
        RETURNING id
        """,
        (
            slot_event_key,
            slot["delivery_slot_key"],
            next_version,
            slot["current_state"],
            slot["contract_revision_id"],
            slot["current_plan_run_id"],
            outbox["owner_user_id"],
            outbox["brand_id"],
            outbox["quote_id"],
            outbox["source_version"],
            _jsonb({
                "publication_source": source,
                "publication_source_id": source_id,
                "article_id": article_id,
                "public_url": public_url,
                "submitted_content_hash": snapshot.get("submitted_content_hash"),
            }),
            outbox["occurred_at"],
        ),
    )
    event_id = int(_row_dict(cur.fetchone())["id"])
    cur.execute(
        """
        UPDATE geo_article_delivery_slots
           SET projection_version=%s,current_event_id=%s,last_event_key=%s,
               completion_evidence=%s,updated_at=NOW()
         WHERE delivery_slot_key=%s AND projection_version=%s
        """,
        (
            next_version,
            event_id,
            slot_event_key,
            f"{source}:{source_id}",
            slot["delivery_slot_key"],
            slot["projection_version"],
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError("publication_slot_projection_cas_failed")
    return {"processed": True, "publication_locked": True, "slot_event_id": event_id}


def process_claimed_event(outbox_id: int, claim_token: str) -> dict[str, Any]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM geo_article_plan_outbox WHERE id=%s AND status='processing' AND claim_token=%s FOR UPDATE",
            (int(outbox_id), claim_token),
        )
        outbox = _row_dict(cur.fetchone())
        if not outbox:
            return {"processed": False, "reason": "claim_lost"}
        if str(outbox["event_kind"]) == "publication_locked":
            result = _process_publication_locked(cur, outbox)
            cur.execute(
                """
                UPDATE geo_article_plan_outbox
                   SET status='completed',completed_at=NOW(),claim_token=NULL,claimed_at=NULL,
                       last_error=NULL,updated_at=NOW()
                 WHERE id=%s AND status='processing' AND claim_token=%s
                """,
                (int(outbox_id), claim_token),
            )
            if cur.rowcount != 1:
                raise RuntimeError("outbox_completion_claim_lost")
            conn.commit()
            return result
        current = load_quote_authority_event(cur, int(outbox["quote_id"]), str(outbox["event_kind"]), source_hint=str(outbox["source_kind"]))
        if current.source_version != str(outbox["source_version"]):
            _insert_event_cursor(cur, current)
            cur.execute(
                """
                UPDATE geo_article_plan_outbox
                   SET status='completed',completed_at=NOW(),claim_token=NULL,claimed_at=NULL,
                       last_error='superseded_by_new_authority_version',updated_at=NOW()
                 WHERE id=%s AND claim_token=%s
                """,
                (int(outbox_id), claim_token),
            )
            conn.commit()
            return {"processed": True, "superseded": True, "event_key": current.event_key}
        run = compile_shadow_plan(cur, outbox)
        cur.execute(
            """
            UPDATE geo_article_plan_outbox
               SET status='completed',completed_at=NOW(),claim_token=NULL,claimed_at=NULL,
                   last_error=NULL,updated_at=NOW()
             WHERE id=%s AND status='processing' AND claim_token=%s
            """,
            (int(outbox_id), claim_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("outbox_completion_claim_lost")
        conn.commit()
        return {"processed": True, "run_id": int(run["id"]), "run_key": str(run["run_key"])}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dispatch_plan_events(limit: int = 20) -> dict[str, int]:
    flags = feature_flags()
    if not flags["ARTICLE_PLAN_RECONCILER_ENABLED"] or not flags["ARTICLE_PLAN_SHADOW_ENABLED"]:
        return {"claimed": 0, "completed": 0, "failed": 0}
    token, rows = claim_plan_events(limit)
    completed = 0
    failed = 0
    for row in rows:
        try:
            result = process_claimed_event(int(row["id"]), token)
            completed += 1 if result.get("processed") else 0
        except Exception as exc:
            failed += 1
            logger.exception("article plan event processing failed id=%s", row.get("id"))
            _mark_processing_failure(int(row["id"]), token, f"{type(exc).__name__}:{exc}", int(row.get("attempt_count") or 1))
    return {"claimed": len(rows), "completed": completed, "failed": failed}


def reconcile_authoritative_quotes(limit: int = 100) -> dict[str, int]:
    """Backstop for missing activation events; no quote-status-only guessing."""
    if not feature_flags()["ARTICLE_PLAN_RECONCILER_ENABLED"]:
        return {"scanned": 0, "enqueued": 0, "ambiguous": 0}
    conn = _get_connection()
    candidates: list[tuple[int, str]] = []
    ambiguous = 0
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH activation_audit AS (
                SELECT entity_id AS quote_id,
                       COUNT(DISTINCT action) FILTER (
                           WHERE action IN ('quote_offline_mark_paid','agent_activate_service')
                       ) AS action_count,
                       MAX(action) FILTER (
                           WHERE action IN ('quote_offline_mark_paid','agent_activate_service')
                       ) AS only_action
                  FROM audit_logs
                 WHERE entity_type='quote'
                   AND action IN ('quote_offline_mark_paid','agent_activate_service')
                 GROUP BY entity_id
            )
            SELECT DISTINCT q.id,
                   CASE
                     WHEN s.id IS NOT NULL AND s.status='active' AND q.status='confirmed' AND q.service_status='active'
                       THEN 'quote_paid_standard'
                     WHEN q.source_type IN ('c_end_geo_plan','quick_writing') AND q.status='confirmed'
                          AND COALESCE(q.paid_amount,0)=0 AND q.writing_status='pending'
                       THEN 'zero_price_writing_project_created'
                     WHEN s.id IS NULL AND q.status IN ('paid','active') AND q.service_status='active'
                          AND aa.action_count=1 AND aa.only_action='quote_offline_mark_paid'
                       THEN 'quote_paid_offline'
                     WHEN s.id IS NULL AND q.status IN ('paid','active') AND q.service_status='active'
                          AND aa.action_count=1 AND aa.only_action='agent_activate_service'
                       THEN 'quote_paid_agent_activation'
                     ELSE NULL
                   END AS event_kind,
                   CASE WHEN s.id IS NULL AND q.status IN ('paid','active') AND q.service_status='active'
                                  AND COALESCE(aa.action_count,0)<>1
                        THEN TRUE ELSE FALSE END AS ambiguous_source
              FROM quotes q
              JOIN brands b ON b.id=q.brand_id AND COALESCE(b.is_deleted,FALSE)=FALSE
              LEFT JOIN keyword_selection_sessions s ON s.quote_id=q.id
              LEFT JOIN activation_audit aa ON aa.quote_id=q.id
             WHERE (
                    (s.status='active' AND q.status='confirmed' AND q.service_status='active')
                 OR (q.source_type IN ('c_end_geo_plan','quick_writing') AND q.status='confirmed'
                     AND COALESCE(q.paid_amount,0)=0 AND q.writing_status='pending')
                 OR (s.id IS NULL AND q.status IN ('paid','active') AND q.service_status='active')
             )
             ORDER BY q.id LIMIT %s
            """,
            (max(1, min(int(limit), 1000)),),
        )
        rows = [_row_dict(row) for row in cur.fetchall()]
        candidates = [(int(row["id"]), str(row["event_kind"])) for row in rows if row.get("event_kind")]
        ambiguous = sum(1 for row in rows if row.get("ambiguous_source"))
    finally:
        conn.close()
    enqueued = 0
    for quote_id, event_kind in candidates:
        result = enqueue_quote_event_durable(quote_id, event_kind)
        enqueued += 1 if result.get("enqueued") else 0
    return {"scanned": len(candidates) + ambiguous, "enqueued": enqueued, "ambiguous": ambiguous}


def article_plan_cron() -> dict[str, Any]:
    reconcile = reconcile_authoritative_quotes()
    dispatch = dispatch_plan_events()
    return {"reconcile": reconcile, "dispatch": dispatch}
