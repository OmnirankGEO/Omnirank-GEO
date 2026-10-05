"""
Profile memory event ledger for the Social IP flywheel.

This table is the durable audit trail behind "AI 最近学会了什么".
The older `profile_completeness.recent_gains` field remains as a fast UI cache,
but long-term memory should live here so it can be reviewed, decayed, or revoked.
"""

from __future__ import annotations

import json
import logging
import hashlib
import re
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("ProfileMemoryDB")

_ensured = False

CANONICAL_CONCEPTS = (
    "business_identity",
    "target_customer",
    "offer_and_proof",
    "voice_style",
    "guardrails",
)

MEMORY_REVIEW_STATUSES = (
    "auto",
    "pending",
    "approved",
    "rejected",
    "dismissed",
    "processing",
)

DEFAULT_IMPORTANCE_BY_CONCEPT = {
    "business_identity": 8,
    "target_customer": 9,
    "offer_and_proof": 9,
    "voice_style": 7,
    "guardrails": 10,
}


def normalize_memory_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[，。！？；：、“”‘’\"'`~|]+", "", text)
    return text[:500]


def infer_canonical_concept(*, label: str = "", text: str = "", dimension: Optional[str] = None) -> str:
    haystack = f"{label} {dimension or ''} {text}".lower()
    if any(k in haystack for k in ("客户", "用户", "人群", "画像", "受众", "妈妈", "宝妈", "老板", "粉丝", "学员")):
        return "target_customer"
    if any(k in haystack for k in ("案例", "效果", "成交", "交付", "产品", "卖点", "价格", "报价", "证明", "证据", "服务")):
        return "offer_and_proof"
    if any(k in haystack for k in ("口头禅", "说话", "语气", "语调", "风格", "节奏", "金句", "表达", "人设")):
        return "voice_style"
    if any(k in haystack for k in ("禁忌", "不要", "不能", "避开", "合规", "风险", "底线", "敏感")):
        return "guardrails"
    return "business_identity"


def make_event_fingerprint(
    profile_id: str,
    source: str,
    event_type: str,
    canonical_concept: str,
    text: str,
) -> Optional[str]:
    concept = canonical_concept if canonical_concept in CANONICAL_CONCEPTS else ""
    normalized = normalize_memory_text(text)
    if not profile_id or not source or not event_type or not concept or not normalized:
        return None
    raw = "|".join([str(profile_id)[:64], source[:40], event_type[:40], concept, normalized])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def is_review_transition_allowed(current_status: Optional[str], next_status: Optional[str]) -> bool:
    current = (current_status or "").strip().lower()
    target = (next_status or "").strip().lower()
    if current == "rejected" and target in {"approved", "auto", "processing"}:
        return False
    return True


def default_importance_score(canonical_concept: Optional[str], *, weight_delta: int = 1) -> int:
    base = DEFAULT_IMPORTANCE_BY_CONCEPT.get(canonical_concept or "", 5)
    if int(weight_delta or 0) >= 4:
        base += 1
    return max(1, min(int(base), 10))


def _get_conn():
    from db.connection import get_connection

    return get_connection()


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _json_load_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _merge_raw_payload(existing: Any, incoming: Any) -> dict:
    base = _json_load_dict(existing)
    new = _json_load_dict(incoming)
    evidence: list[Any] = []
    seen: set[str] = set()
    for payload in (base, new):
        raw_evidence = payload.get("evidence")
        if isinstance(raw_evidence, list):
            candidates = raw_evidence
        elif raw_evidence:
            candidates = [raw_evidence]
        else:
            candidates = []
        for item in candidates:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            evidence.append(item)
    for key, value in new.items():
        if key == "evidence":
            continue
        if value in (None, "", [], {}):
            continue
        if key not in base:
            base[key] = value
    if evidence:
        base["evidence"] = evidence[-20:]
    base["merge_count"] = int(base.get("merge_count") or 0) + 1
    base["last_seen_at"] = datetime.utcnow().isoformat(timespec="seconds")
    return base


def _normalize_time(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value


def init_profile_memory_events_table() -> None:
    """Create profile memory event ledger and indexes idempotently."""
    global _ensured
    if _ensured:
        return

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_memory_events (
                id BIGSERIAL PRIMARY KEY,
                profile_id VARCHAR(64) NOT NULL,
                user_id INTEGER,
                source VARCHAR(40) NOT NULL,
                event_type VARCHAR(40) NOT NULL,
                dimension VARCHAR(40),
                canonical_concept VARCHAR(40),
                event_fingerprint TEXT,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                raw_payload JSONB DEFAULT '{}'::jsonb,
                confidence NUMERIC(4,3) DEFAULT 0.700,
                weight_delta INTEGER DEFAULT 1,
                is_active BOOLEAN DEFAULT TRUE,
                review_status VARCHAR(20) DEFAULT 'auto',
                reviewed_at TIMESTAMP,
                reviewed_by_user_id INTEGER,
                notes TEXT,
                last_accessed_at TIMESTAMP,
                access_count INTEGER DEFAULT 0,
                importance_score INTEGER DEFAULT 5,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
        # Production may have a partial table from an earlier helper version.
        # Heal all columns additively before constraints/indexes are touched.
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS user_id INTEGER")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS dimension VARCHAR(40)")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS canonical_concept VARCHAR(40)")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS event_fingerprint TEXT")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS title TEXT")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS text TEXT")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS raw_payload JSONB DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS confidence NUMERIC(4,3) DEFAULT 0.700")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS weight_delta INTEGER DEFAULT 1")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS review_status VARCHAR(20) DEFAULT 'auto'")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS reviewed_by_user_id INTEGER")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS notes TEXT")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMP")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS access_count INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS importance_score INTEGER DEFAULT 5")
        cur.execute("ALTER TABLE profile_memory_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")
        cur.execute("UPDATE profile_memory_events SET raw_payload = '{}'::jsonb WHERE raw_payload IS NULL")
        cur.execute("UPDATE profile_memory_events SET confidence = 0.700 WHERE confidence IS NULL")
        cur.execute("UPDATE profile_memory_events SET weight_delta = 1 WHERE weight_delta IS NULL")
        cur.execute("UPDATE profile_memory_events SET is_active = TRUE WHERE is_active IS NULL")
        cur.execute("UPDATE profile_memory_events SET review_status = 'auto' WHERE review_status IS NULL")
        cur.execute("UPDATE profile_memory_events SET access_count = 0 WHERE access_count IS NULL")
        cur.execute("UPDATE profile_memory_events SET importance_score = 5 WHERE importance_score IS NULL")
        cur.execute("UPDATE profile_memory_events SET created_at = NOW() WHERE created_at IS NULL")
        cur.execute("UPDATE profile_memory_events SET title = COALESCE(NULLIF(title, ''), NULLIF(text, ''), NULLIF(dimension, ''), event_type, '资料线索') WHERE title IS NULL OR title = ''")
        cur.execute("UPDATE profile_memory_events SET text = COALESCE(NULLIF(text, ''), title, '资料线索') WHERE text IS NULL OR text = ''")
        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_profile_memory_canonical_concept'
                      AND conrelid = 'profile_memory_events'::regclass
                ) THEN
                    ALTER TABLE profile_memory_events
                    ADD CONSTRAINT chk_profile_memory_canonical_concept
                    CHECK (
                        canonical_concept IS NULL OR canonical_concept IN (
                            'business_identity',
                            'target_customer',
                            'offer_and_proof',
                            'voice_style',
                            'guardrails'
                        )
                    );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_profile_memory_fingerprint_concept'
                      AND conrelid = 'profile_memory_events'::regclass
                ) THEN
                    ALTER TABLE profile_memory_events
                    ADD CONSTRAINT chk_profile_memory_fingerprint_concept
                    CHECK (event_fingerprint IS NULL OR canonical_concept IS NOT NULL);
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_profile_memory_review_status'
                      AND conrelid = 'profile_memory_events'::regclass
                ) THEN
                    ALTER TABLE profile_memory_events
                    ADD CONSTRAINT chk_profile_memory_review_status
                    CHECK (
                        review_status IS NULL OR review_status IN (
                            'auto',
                            'pending',
                            'approved',
                            'rejected',
                            'dismissed',
                            'processing'
                        )
                    );
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_profile_memory_confidence_range'
                      AND conrelid = 'profile_memory_events'::regclass
                ) THEN
                    ALTER TABLE profile_memory_events
                    ADD CONSTRAINT chk_profile_memory_confidence_range
                    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1));
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_profile_memory_importance_range'
                      AND conrelid = 'profile_memory_events'::regclass
                ) THEN
                    ALTER TABLE profile_memory_events
                    ADD CONSTRAINT chk_profile_memory_importance_range
                    CHECK (importance_score IS NULL OR (importance_score >= 1 AND importance_score <= 10));
                END IF;
            END $$;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_profile_memory_profile_time
            ON profile_memory_events(profile_id, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_profile_memory_active
            ON profile_memory_events(profile_id, is_active, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_profile_memory_source
            ON profile_memory_events(source, event_type, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_profile_memory_concept
            ON profile_memory_events(profile_id, canonical_concept, review_status, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_profile_memory_weighted
            ON profile_memory_events(profile_id, canonical_concept, review_status, is_active, importance_score DESC, access_count DESC, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_profile_memory_event_fingerprint
            ON profile_memory_events(profile_id, source, event_type, canonical_concept, event_fingerprint)
            WHERE event_fingerprint IS NOT NULL
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_profile_memory_pending
            ON profile_memory_events(profile_id, review_status, is_active, created_at DESC, id DESC)
            """
        )
        conn.commit()
        _ensured = True
        logger.info("[ProfileMemory] profile_memory_events table ready")
    except Exception:
        conn.rollback()
        logger.exception("[ProfileMemory] init table failed")
        raise
    finally:
        conn.close()


def record_profile_memory_event(
    profile_id: str,
    *,
    source: str,
    event_type: str,
    title: str,
    text: str,
    dimension: Optional[str] = None,
    canonical_concept: Optional[str] = None,
    event_fingerprint: Optional[str] = None,
    raw_payload: Optional[dict] = None,
    confidence: float = 0.7,
    weight_delta: int = 1,
    importance_score: Optional[int] = None,
    user_id: Optional[int] = None,
    review_status: str = "auto",
    bypass_pending_cap: bool = False,
) -> Optional[int]:
    """Insert or merge one memory event. Returns id; swallows errors for product flow safety.

    review_status:
      - 'auto'    : applied directly (legacy default — high-trust sources like
                    expert chat outcomes that user explicitly chose to save).
      - 'pending' : NOT applied to profile yet — needs user confirm via UI.
                    Used by P0-6 (R3) for async chat extraction so the AI can't
                    silently mutate structured_knowledge.
      - 'approved' / 'rejected' set later via review_profile_memory_event.

    Fingerprinted writes use a single PostgreSQL upsert so concurrent turns do
    not lose the winner id or trip over unique violations. Rejected/processing
    tombstones are not resurrected and return None to the caller.
    """
    if not profile_id or not source or not event_type or not title:
        return None

    normalized_status = (review_status or "auto").strip().lower()
    if normalized_status not in MEMORY_REVIEW_STATUSES:
        normalized_status = "auto"
    concept = canonical_concept if canonical_concept in CANONICAL_CONCEPTS else None
    if not concept and event_fingerprint:
        concept = infer_canonical_concept(label=title, text=text, dimension=dimension)
    fingerprint = event_fingerprint
    if concept and not fingerprint:
        fingerprint = make_event_fingerprint(profile_id, source, event_type, concept, text or title)
    if fingerprint and not concept:
        fingerprint = None
    normalized_importance = (
        default_importance_score(concept, weight_delta=weight_delta)
        if importance_score is None
        else max(1, min(int(importance_score or 5), 10))
    )

    try:
        init_profile_memory_events_table()
        conn = _get_conn()
        try:
            cur = conn.cursor()
            if concept and fingerprint:
                # Serialize per-profile pending writes so the soft cap remains
                # meaningful during rapid double submits or small concurrent tests.
                if normalized_status == "pending" and source[:40] == "profile_flywheel" and not bypass_pending_cap:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"profile_memory_pending:{str(profile_id)[:64]}",),
                    )
                    cur.execute(
                        """
                        SELECT id
                        FROM profile_memory_events
                        WHERE profile_id = %s
                          AND source = %s
                          AND event_type = %s
                          AND canonical_concept = %s
                          AND event_fingerprint = %s
                        """,
                        (str(profile_id)[:64], source[:40], event_type[:40], concept, fingerprint),
                    )
                    has_existing = cur.fetchone()
                    if not has_existing:
                        cur.execute(
                            """
                            SELECT COUNT(*)
                            FROM profile_memory_events
                            WHERE profile_id = %s
                              AND review_status = 'pending'
                              AND is_active = TRUE
                            """,
                            (str(profile_id)[:64],),
                        )
                        row_count = cur.fetchone() or [0]
                        pending_count = int(row_count[0] if not isinstance(row_count, dict) else row_count.get("count", 0))
                        if pending_count >= 20:
                            conn.commit()
                            return None

                cur.execute(
                    """
                    INSERT INTO profile_memory_events (
                        profile_id, user_id, source, event_type, dimension,
                        canonical_concept, event_fingerprint,
                        title, text, raw_payload, confidence, weight_delta, importance_score, review_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (profile_id, source, event_type, canonical_concept, event_fingerprint)
                    WHERE event_fingerprint IS NOT NULL
                    DO UPDATE SET
                        raw_payload = CASE
                            WHEN profile_memory_events.review_status IN ('pending', 'approved', 'auto') THEN
                                COALESCE(profile_memory_events.raw_payload, '{}'::jsonb)
                                || (COALESCE(EXCLUDED.raw_payload, '{}'::jsonb) - 'evidence')
                                || CASE
                                    WHEN COALESCE(EXCLUDED.raw_payload, '{}'::jsonb) ? 'evidence' THEN
                                        jsonb_build_object(
                                            'evidence',
                                            CASE
                                                WHEN jsonb_typeof(profile_memory_events.raw_payload->'evidence') = 'array'
                                                    THEN profile_memory_events.raw_payload->'evidence'
                                                ELSE '[]'::jsonb
                                            END
                                            || CASE
                                                WHEN jsonb_typeof(EXCLUDED.raw_payload->'evidence') = 'array'
                                                    THEN EXCLUDED.raw_payload->'evidence'
                                                ELSE '[]'::jsonb
                                            END
                                        )
                                    ELSE '{}'::jsonb
                                END
                            ELSE profile_memory_events.raw_payload
                        END,
                        confidence = CASE
                            WHEN profile_memory_events.review_status IN ('pending', 'approved', 'auto') THEN
                                GREATEST(profile_memory_events.confidence, EXCLUDED.confidence)
                            ELSE profile_memory_events.confidence
                        END,
                        weight_delta = CASE
                            WHEN profile_memory_events.review_status = 'pending' THEN
                                GREATEST(profile_memory_events.weight_delta, EXCLUDED.weight_delta)
                            ELSE profile_memory_events.weight_delta
                        END,
                        importance_score = CASE
                            WHEN profile_memory_events.review_status IN ('pending', 'approved', 'auto') THEN
                                GREATEST(COALESCE(profile_memory_events.importance_score, 5), COALESCE(EXCLUDED.importance_score, 5))
                            ELSE profile_memory_events.importance_score
                        END,
                        title = CASE
                            WHEN profile_memory_events.review_status = 'pending' THEN
                                COALESCE(NULLIF(EXCLUDED.title, ''), profile_memory_events.title)
                            ELSE profile_memory_events.title
                        END,
                        text = CASE
                            WHEN profile_memory_events.review_status = 'pending' THEN
                                COALESCE(NULLIF(EXCLUDED.text, ''), profile_memory_events.text)
                            ELSE profile_memory_events.text
                        END
                    RETURNING id, review_status
                    """,
                    (
                        str(profile_id)[:64],
                        user_id,
                        source[:40],
                        event_type[:40],
                        (dimension or "")[:40] or None,
                        concept,
                        fingerprint,
                        title[:500],
                        (text or title)[:2000],
                        _json_dump(raw_payload or {}),
                        max(0, min(float(confidence), 1)),
                        int(weight_delta or 0),
                        normalized_importance,
                        normalized_status[:20],
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                if not row:
                    return None
                returned_status = (row.get("review_status") if isinstance(row, dict) else row[1] or "").strip().lower()
                if returned_status in {"rejected", "processing"}:
                    return None
                return int(row["id"] if isinstance(row, dict) else row[0])

            cur.execute(
                """
                INSERT INTO profile_memory_events (
                    profile_id, user_id, source, event_type, dimension,
                    canonical_concept, event_fingerprint,
                    title, text, raw_payload, confidence, weight_delta, importance_score, review_status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(profile_id)[:64],
                    user_id,
                    source[:40],
                    event_type[:40],
                    (dimension or "")[:40] or None,
                    concept,
                    fingerprint,
                    title[:500],
                    (text or title)[:2000],
                    _json_dump(raw_payload or {}),
                    max(0, min(float(confidence), 1)),
                    int(weight_delta or 0),
                    normalized_importance,
                    normalized_status[:20],
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return int(row["id"]) if row and row.get("id") is not None else None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ProfileMemory] record failed: %s", exc)
        return None


def count_pending_profile_memory_events(profile_id: str) -> int:
    """Fast COUNT(*) for pending events · v1.11 P1 #2 fix.

    Used by flywheel engine 限流 (cap 20). Avoids the previous
    `len(list_pending(limit=100))` anti-pattern that fetched + decoded
    rows just to count them.

    v1.11 P1 #1: **raises on DB failure** (does not swallow). Caller is
    responsible for fallback + monitor counter. Empty profile_id returns 0.
    """
    if not profile_id:
        return 0
    init_profile_memory_events_table()
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM profile_memory_events
            WHERE profile_id = %s
              AND review_status = 'pending'
              AND is_active = TRUE
            """,
            (str(profile_id)[:64],),
        )
        row = cur.fetchone() or [0]
        return int(row[0] if not isinstance(row, dict) else row.get("count", 0))
    finally:
        conn.close()


def list_pending_profile_memory_events(profile_id: str, *, limit: int = 20) -> list[dict]:
    """List only pending (awaiting user confirmation) events for the AI 想记住 panel.

    P0-6 (R3): _extract_info_from_input writes pending events instead of
    silently mutating profile fields. UI reads these and surfaces "AI 想记住
    这 3 件事，是真的吗？" with confirm / dismiss actions.
    """
    if not profile_id:
        return []
    try:
        init_profile_memory_events_table()
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, profile_id, source, event_type, dimension,
                       canonical_concept, event_fingerprint, title, text,
                       raw_payload, confidence, weight_delta, review_status,
                       is_active, created_at
                FROM profile_memory_events
                WHERE profile_id = %s
                  AND review_status = 'pending'
                  AND is_active = TRUE
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (str(profile_id)[:64], max(1, min(int(limit or 20), 100))),
            )
            rows = cur.fetchall() or []
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ProfileMemory] list_pending failed: %s", exc)
        return []

    events: list[dict] = []
    for row in rows:
        item = dict(row)
        item["created_at"] = _normalize_time(item.get("created_at"))
        item["delta"] = item.get("weight_delta", 0)
        if not item.get("text"):
            item["text"] = item.get("title", "")
        events.append(item)
    return events


def get_profile_memory_event(event_id: int) -> Optional[dict]:
    """Fetch one event by id (used during confirm/reject)."""
    if not event_id:
        return None
    try:
        init_profile_memory_events_table()
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, profile_id, source, event_type, dimension,
                       canonical_concept, event_fingerprint, title, text,
                       raw_payload, confidence, weight_delta, review_status,
                       is_active, last_accessed_at, access_count, importance_score, created_at
                FROM profile_memory_events
                WHERE id = %s
                """,
                (int(event_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            item = dict(row)
            item["created_at"] = _normalize_time(item.get("created_at"))
            item["last_accessed_at"] = _normalize_time(item.get("last_accessed_at"))
            item["access_count"] = int(item.get("access_count") or 0)
            item["importance_score"] = int(item.get("importance_score") or 5)
            return item
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ProfileMemory] get_event failed: %s", exc)
        return None


def list_profile_memory_events(
    profile_id: str,
    *,
    limit: int = 20,
    active_only: bool = True,
    prompt_safe: bool = False,
    review_statuses: Optional[tuple] = None,
    include_inactive: bool = False,
    canonical_concept: Optional[str] = None,
    track_access: bool = False,
) -> list[dict]:
    """Read recent memory events in UI-friendly shape.

    prompt_safe=True: P0 fix · 排除 review_status='pending' 的事件,
    防止用户没确认的 AI 提取事实漏进 generate_script prompt
    (违反 P0-6 R3 "用户确认才生效" 设计原则).
    Default False 保留 UI 列表行为不变 (老板可能要看 pending 都列出来审).

    review_statuses=('approved', 'auto'): P2 memory control · 按 review_status 过滤,
    用于 "已记住" / "已忽略 (archived)" 三段 tab 展示.
    include_inactive=True: 同上 · 拉 is_active=false 的归档/拒绝项给"已忽略"段.
    """
    if not profile_id:
        return []

    try:
        init_profile_memory_events_table()
        conn = _get_conn()
        try:
            cur = conn.cursor()
            where = "profile_id = %s"
            params: list[Any] = [str(profile_id)]
            if active_only and not include_inactive:
                where += " AND is_active = TRUE"
            if prompt_safe:
                where += " AND review_status IN ('auto', 'approved')"
            if review_statuses:
                placeholders = ",".join(["%s"] * len(review_statuses))
                where += f" AND review_status IN ({placeholders})"
                params.extend(review_statuses)
            if canonical_concept:
                where += " AND canonical_concept = %s"
                params.append(str(canonical_concept)[:40])
            params.append(max(1, min(int(limit or 20), 100)))
            cur.execute(
                f"""
                SELECT id, profile_id, source, event_type, dimension,
                       canonical_concept, event_fingerprint, title, text,
                       raw_payload, confidence, weight_delta, review_status,
                       is_active, last_accessed_at, access_count, importance_score, created_at
                FROM profile_memory_events
                WHERE {where}
                ORDER BY
                    COALESCE(importance_score, 5) DESC,
                    COALESCE(access_count, 0) DESC,
                    COALESCE(confidence, 0.7) DESC,
                    COALESCE(weight_delta, 1) DESC,
                    created_at DESC,
                    id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall() or []
            if track_access and rows:
                ids = [int(row["id"] if isinstance(row, dict) else row[0]) for row in rows if (row["id"] if isinstance(row, dict) else row[0]) is not None]
                if ids:
                    cur.execute(
                        """
                        UPDATE profile_memory_events
                        SET access_count = COALESCE(access_count, 0) + 1,
                            last_accessed_at = NOW()
                        WHERE id = ANY(%s)
                        """,
                        (ids,),
                    )
                    conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ProfileMemory] list failed: %s", exc)
        return []

    events: list[dict] = []
    for row in rows:
        item = dict(row)
        item["created_at"] = _normalize_time(item.get("created_at"))
        item["last_accessed_at"] = _normalize_time(item.get("last_accessed_at"))
        item["delta"] = item.get("weight_delta", 0)
        item["access_count"] = int(item.get("access_count") or 0)
        item["importance_score"] = int(item.get("importance_score") or 5)
        if not item.get("text"):
            item["text"] = item.get("title", "")
        events.append(item)
    return events


def build_known_memory_context(profile_id: str, *, limit: int = 24) -> dict:
    """Compact approved/auto memories by canonical concept for prompt and UI chips."""
    rows = list_profile_memory_events(
        profile_id,
        limit=limit,
        active_only=True,
        prompt_safe=True,
        review_statuses=("approved", "auto"),
    )
    grouped: dict[str, list[dict]] = {concept: [] for concept in CANONICAL_CONCEPTS}
    for row in rows:
        concept = row.get("canonical_concept") or infer_canonical_concept(
            label=row.get("title") or "",
            text=row.get("text") or "",
            dimension=row.get("dimension"),
        )
        if concept not in grouped:
            concept = "business_identity"
        grouped[concept].append({
            "id": row.get("id"),
            "label": row.get("title") or row.get("dimension") or "已知信息",
            "text": row.get("text") or "",
            "source": row.get("source") or "",
            "review_status": row.get("review_status") or "",
        })
    return {
        "concepts": grouped,
        "chips": [
            {"concept": concept, **item}
            for concept, items in grouped.items()
            for item in items[:4]
            if item.get("text")
        ][:limit],
    }


def get_profile_memory_metrics(*, limit_profiles: int = 20) -> dict:
    """Small admin/health snapshot for flywheel memory gray rollout."""
    init_profile_memory_events_table()
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT source, review_status, COUNT(*) AS count
            FROM profile_memory_events
            GROUP BY source, review_status
            ORDER BY count DESC
            LIMIT 50
            """
        )
        by_source_status = [dict(row) for row in (cur.fetchall() or [])]
        cur.execute(
            """
            SELECT profile_id, COUNT(*) AS pending_count
            FROM profile_memory_events
            WHERE source = 'profile_flywheel'
              AND review_status = 'pending'
              AND is_active = TRUE
            GROUP BY profile_id
            ORDER BY pending_count DESC
            LIMIT %s
            """,
            (max(1, min(int(limit_profiles or 20), 100)),),
        )
        pending_by_profile = [dict(row) for row in (cur.fetchall() or [])]
        cur.execute(
            """
            SELECT COUNT(*) AS duplicate_fingerprints
            FROM (
                SELECT profile_id, source, event_type, canonical_concept, event_fingerprint
                FROM profile_memory_events
                WHERE event_fingerprint IS NOT NULL
                GROUP BY profile_id, source, event_type, canonical_concept, event_fingerprint
                HAVING COUNT(*) > 1
            ) dup
            """
        )
        dup = cur.fetchone() or {"duplicate_fingerprints": 0}
        cur.execute(
            """
            SELECT COUNT(*) AS invalid_fingerprints
            FROM profile_memory_events
            WHERE event_fingerprint IS NOT NULL
              AND canonical_concept IS NULL
            """
        )
        invalid = cur.fetchone() or {"invalid_fingerprints": 0}
        return {
            "by_source_status": by_source_status,
            "pending_by_profile": pending_by_profile,
            "duplicate_fingerprints": int(dup.get("duplicate_fingerprints", 0) if isinstance(dup, dict) else dup[0]),
            "invalid_fingerprints": int(invalid.get("invalid_fingerprints", 0) if isinstance(invalid, dict) else invalid[0]),
        }
    finally:
        conn.close()


def update_profile_memory_event_text(
    event_id: int,
    *,
    title: Optional[str] = None,
    text: Optional[str] = None,
    notes: Optional[str] = None,
    raw_payload: Optional[dict] = None,
) -> bool:
    """P2 memory control · 编辑已批准记忆的 title/text.
    只动 title / text / notes / raw_payload 四个用户可见字段, 不改 review_status / is_active / dimension.
    防止改 schema 元信息导致 UI 链路混乱.

    CTO-13.0 2026-05-10 加 raw_payload 支持(insight-decision edited 模式落库 merged payload)
    """
    if not event_id or all(v is None for v in (title, text, notes, raw_payload)):
        return False
    try:
        init_profile_memory_events_table()
        conn = _get_conn()
        try:
            cur = conn.cursor()
            sets: list[str] = []
            params: list[Any] = []
            if title is not None:
                sets.append("title = %s")
                params.append(str(title)[:500])
            if text is not None:
                sets.append("text = %s")
                params.append(str(text)[:2000])
            if notes is not None:
                sets.append("notes = %s")
                params.append(str(notes)[:500])
            if raw_payload is not None:
                sets.append("raw_payload = %s")
                params.append(_json_dump(raw_payload))
            sets.append("reviewed_at = NOW()")
            params.append(int(event_id))
            cur.execute(
                f"UPDATE profile_memory_events SET {', '.join(sets)} WHERE id = %s",
                tuple(params),
            )
            changed = cur.rowcount > 0
            conn.commit()
            return changed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ProfileMemory] update_text failed: %s", exc)
        return False


def review_profile_memory_event(
    event_id: int,
    *,
    is_active: bool,
    reviewed_by_user_id: Optional[int] = None,
    notes: Optional[str] = None,
    review_status: Optional[str] = None,
) -> bool:
    """Human review hook for future UI: keep/deactivate a learned memory.

    By default this keeps the original confirm/dismiss semantics: active means
    approved, inactive means rejected. Memory archive/restore endpoints may pass
    review_status explicitly so soft-archiving an approved memory does not turn
    it into a rejected pending dismissal.
    """
    try:
        init_profile_memory_events_table()
        conn = _get_conn()
        try:
            cur = conn.cursor()
            target_status = (review_status or ("approved" if is_active else "rejected"))[:20]
            cur.execute(
                """
                SELECT review_status
                FROM profile_memory_events
                WHERE id = %s
                FOR UPDATE
                """,
                (int(event_id),),
            )
            current = cur.fetchone()
            if not current:
                conn.rollback()
                return False
            if not is_review_transition_allowed(current.get("review_status"), target_status):
                conn.rollback()
                logger.info(
                    "[ProfileMemory] blocked review transition id=%s %s -> %s",
                    event_id,
                    current.get("review_status"),
                    target_status,
                )
                return False
            cur.execute(
                """
                UPDATE profile_memory_events
                SET is_active = %s,
                    review_status = %s,
                    reviewed_at = NOW(),
                    reviewed_by_user_id = %s,
                    notes = %s
                WHERE id = %s
                """,
                (
                    bool(is_active),
                    target_status,
                    reviewed_by_user_id,
                    notes,
                    int(event_id),
                ),
            )
            changed = cur.rowcount > 0
            conn.commit()
            return changed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ProfileMemory] review failed: %s", exc)
        return False


def claim_profile_memory_event_for_confirm(
    event_id: int,
    *,
    reviewed_by_user_id: Optional[int] = None,
) -> bool:
    """CAS guard for confirm flow.

    Marks one pending/auto event as processing before profile mutation so a
    double-click or concurrent request cannot merge the same memory twice.
    """
    try:
        init_profile_memory_events_table()
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE profile_memory_events
                SET review_status = 'processing',
                    reviewed_at = NOW(),
                    reviewed_by_user_id = %s,
                    notes = 'confirm processing'
                WHERE id = %s
                  AND (review_status IS NULL OR review_status IN ('pending', 'auto'))
                  AND is_active = TRUE
                """,
                (reviewed_by_user_id, int(event_id)),
            )
            changed = cur.rowcount > 0
            conn.commit()
            return changed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ProfileMemory] claim_confirm failed: %s", exc)
        return False


def reject_pending_profile_memory_event(
    event_id: int,
    *,
    reviewed_by_user_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> bool:
    """Reject only a still-pending memory event.

    Dismiss must not overwrite a concurrent confirm flow after the event has
    moved to processing/approved.
    """
    try:
        init_profile_memory_events_table()
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE profile_memory_events
                SET is_active = FALSE,
                    review_status = 'rejected',
                    reviewed_at = NOW(),
                    reviewed_by_user_id = %s,
                    notes = %s
                WHERE id = %s
                  AND (review_status IS NULL OR review_status IN ('pending', 'auto'))
                  AND is_active = TRUE
                """,
                (reviewed_by_user_id, notes, int(event_id)),
            )
            changed = cur.rowcount > 0
            conn.commit()
            return changed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ProfileMemory] reject_pending failed: %s", exc)
        return False
