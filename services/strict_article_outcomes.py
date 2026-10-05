"""Strict, read-only article citation attribution from existing source facts.

``monitoring_results`` remains the sole monitoring SSOT.  This module derives
events in memory; it does not write an observation ledger or mutate source rows.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


URL_NORMALIZATION_VERSION: Final = "strict-url-v1.0"
OUTCOME_METRIC_VERSION: Final = "article-question-outcome-v1.0"
TRACKING_KEYS: Final = frozenset({
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "mc_cid", "mc_eid",
    "spm", "ref_src", "ref_url",
})


def normalize_publication_url(raw: str) -> str | None:
    """Normalize identity while preserving path case and business query params."""
    try:
        parsed = urlsplit(str(raw or "").strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        scheme = parsed.scheme.casefold()
        host = parsed.hostname.casefold().rstrip(".")
        port = parsed.port
        netloc = host
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
        kept = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            lowered = key.casefold()
            if lowered.startswith("utm_") or lowered in TRACKING_KEYS:
                continue
            kept.append((key, value))
        kept.sort(key=lambda pair: (pair[0], pair[1]))
        return urlunsplit((scheme, netloc, parsed.path or "/", urlencode(kept, doseq=True), ""))
    except Exception:
        return None


def parse_citation_urls(raw: Any) -> dict[str, Any]:
    """Only valid JSON arrays and object ``url``/``link`` fields are eligible."""
    if raw in (None, ""):
        return {"status": "empty", "urls": [], "item_count": 0}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"status": "invalid_json", "urls": [], "item_count": 0}
    if not isinstance(value, list):
        return {"status": "not_array", "urls": [], "item_count": 0}
    urls: list[str] = []
    invalid_items = 0
    for item in value:
        if not isinstance(item, dict):
            invalid_items += 1
            continue
        normalized = normalize_publication_url(item.get("url") or item.get("link") or "")
        if normalized:
            urls.append(normalized)
        else:
            invalid_items += 1
    return {
        "status": "valid" if not invalid_items else "valid_with_invalid_items",
        "urls": sorted(set(urls)),
        "item_count": len(value),
        "invalid_items": invalid_items,
    }


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def publication_domain(normalized_url: str) -> str:
    """Host of an already-normalized URL. Empty string when unparseable."""
    try:
        return (urlsplit(str(normalized_url or "")).hostname or "").casefold()
    except Exception:
        return ""


def attribute_observations(
    publications: list[dict[str, Any]],
    monitoring_results: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    identity_by_brand: dict[int, str] | None = None,
    include_unverified_body: bool = False,
) -> dict[str, Any]:
    """Pure attribution engine used by SQL-backed metrics and reverse fixtures.

    ``identity_by_brand`` (WO_DELIVERY_FLYWHEEL_CLOSURE §2.2) maps ``brand_id`` to a
    brand-identity key so that one real company split across several ``brand_id`` rows
    still attributes. 2026-08-06 production evidence: QZQZ publishes under brand 662 but
    its monitoring history lives under brand 10, so a strict ``brand_id`` equality test
    can never match. **Fallback is unchanged**: when either side has no mapping, the
    original strict ``brand_id`` equality applies, so callers that pass nothing keep the
    exact previous behaviour.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    identity_map = {int(k): str(v) for k, v in (identity_by_brand or {}).items() if v}

    def _owner_key(brand_id: Any) -> Any:
        """Identity key when both mapped, else the raw brand_id (strict fallback)."""
        try:
            return identity_map.get(int(brand_id)) or brand_id
        except (TypeError, ValueError):
            return brand_id

    by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    quality = defaultdict(int)
    for publication in publications:
        state = str(publication.get("status") or "").casefold()
        if state not in {"published", "manual_confirmed"}:
            if publication.get("published_at"):
                quality["publication_state_time_conflict"] += 1
            continue
        url = normalize_publication_url(publication.get("publish_url") or "")
        if not url:
            quality["publication_url_missing_or_invalid"] += 1
            continue
        published_at = _aware(publication.get("published_at"))
        if not published_at:
            quality["publication_time_unknown"] += 1
            continue
        if published_at > now:
            quality["future_publication"] += 1
            continue
        # 正文证据等级。**默认严格**:没有提交快照 = 无法证明"被引的就是我们发的那一版",
        # 这类行不进主指标。
        #
        # [WO_DELIVERY_FLYWHEEL_CLOSURE §2.1 · 2026-08-06] `include_unverified_body=True` 时
        # 这两道闸从"丢弃"降为"降级标注",事件带 body_proof=False +
        # url_match='exact_normalized_no_body_proof'。
        # 🔴 为什么需要它,以及为什么它绝不能混进主指标:
        #    生产实测 —— 晨光富士 11 个被引 URL 里有 8 个对应的监测行 lineage_status=complete
        #    (38 条引用行),监测侧完全合格;唯一卡点是 59 条发布里 58 条没有任何内容快照
        #    (2026-05 的历史缺陷;快照覆盖率 07 月 35% → 08 月 97%,前向已自愈)。
        #    于是这个客户唯一的真实成果在系统里显示为 0,而他正在要求退款。
        #    但**事后拿当前 article 正文补一个哈希 = 伪造证据等级**(文章可能改过),
        #    绝不能做。诚实的做法只有一条:承认"URL 匹配成立、正文无法自证",
        #    单独计数、单独展示、绝不并入主指标。
        body_proof = True
        if not publication.get("publication_snapshot_hash"):
            if not include_unverified_body:
                quality["publication_snapshot_missing"] += 1
                continue
            body_proof = False
            quality["downgraded_snapshot_missing"] += 1
        elif publication.get("body_observation_level") not in {
            "submission_snapshot", "channel_submission_snapshot",
            "extension_channel_submission_snapshot",
        }:
            if not include_unverified_body:
                quality["publication_body_not_submission_snapshot"] += 1
                continue
            body_proof = False
            quality["downgraded_body_level"] += 1
        publication = {**publication, "body_proof": body_proof}
        if publication.get("brand_id") is None:
            quality["publication_brand_unknown"] += 1
            continue
        by_url[url].append({**publication, "normalized_url": url, "published_at_utc": published_at})

    unique_url: dict[str, dict[str, Any]] = {}
    for url, rows in by_url.items():
        article_ids = {row.get("article_id") for row in rows}
        if len(article_ids) != 1:
            quality["publication_url_article_ambiguous"] += 1
            continue
        unique_url[url] = min(rows, key=lambda row: row["published_at_utc"])

    events: list[dict[str, Any]] = []
    eligible_monitoring = 0
    for result in monitoring_results:
        if (
            str(result.get("identity_review_state") or "not_required") == "pending"
            or str(result.get("response_status") or "legacy_unknown")
            == "brand_identity_unresolved"
        ):
            quality["monitoring_identity_pending"] += 1
            continue
        tested_at = _aware(result.get("tested_at"))
        if not tested_at:
            quality["monitoring_time_unknown"] += 1
            continue
        if tested_at > now:
            quality["future_monitoring"] += 1
            continue
        if str(result.get("lineage_status") or "") != "complete":
            quality["monitoring_lineage_incomplete"] += 1
            continue
        if not str(result.get("sent_question_snapshot") or "").strip():
            quality["monitoring_question_missing"] += 1
            continue
        if result.get("brand_id") is None:
            quality["monitoring_brand_unknown"] += 1
            continue
        eligible_monitoring += 1
        parsed = parse_citation_urls(result.get("search_citations"))
        if parsed["status"] in {"invalid_json", "not_array"}:
            quality[f"citation_{parsed['status']}"] += 1
            continue
        if parsed.get("invalid_items"):
            quality["citation_invalid_items"] += int(parsed["invalid_items"])
        for url in parsed["urls"]:
            publication = unique_url.get(url)
            if not publication:
                continue
            if _owner_key(result.get("brand_id")) != _owner_key(publication.get("brand_id")):
                continue
            if tested_at <= publication["published_at_utc"]:
                quality["prepublication_citation"] += 1
                continue
            events.append({
                "metric_version": OUTCOME_METRIC_VERSION,
                "monitoring_result_id": result.get("id"),
                "article_id": publication.get("article_id"),
                "publication_source": publication.get("publication_source"),
                "publication_source_id": publication.get("publication_source_id"),
                "publication_snapshot_hash": publication.get("publication_snapshot_hash"),
                "brand_id": publication.get("brand_id"),
                # 实体键:未建档时为 None,归因仍按 brand_id 走(见 _owner_key 的回落口径)
                "identity_key": identity_map.get(publication.get("brand_id"))
                if isinstance(publication.get("brand_id"), int) else None,
                "monitoring_brand_id": result.get("brand_id"),
                # 渠道维度(WO §2.3 白名单口径的原料:被引的到底是哪个媒体位)
                "publish_url_normalized": url,
                "publish_domain": publication_domain(url),
                "industry": publication.get("industry"),
                "style_family": publication.get("style_family") or "mapping_unknown",
                "question": result.get("sent_question_snapshot"),
                "question_family": result.get("question_family") or "mapping_unknown",
                "question_family_version": result.get("question_family_version") or "legacy_unknown",
                "provider": result.get("provider"),
                "model": result.get("model"),
                "model_revision": result.get("model_revision"),
                "surface": result.get("surface"),
                "target_outcome": result.get("target_outcome"),
                "tested_at": tested_at.isoformat(),
                "published_at": publication["published_at_utc"].isoformat(),
                "body_proof": bool(publication.get("body_proof", True)),
                "url_match": (
                    "exact_normalized" if publication.get("body_proof", True)
                    else "exact_normalized_no_body_proof"
                ),
            })
    proven = [e for e in events if e.get("body_proof", True)]
    return {
        "metric_version": OUTCOME_METRIC_VERSION,
        "url_normalization_version": URL_NORMALIZATION_VERSION,
        "events": events,
        "event_count": len(events),
        # 主指标只数有正文证据的;降级行单独一列,永不并入
        "proven_event_count": len(proven),
        "unverified_body_event_count": len(events) - len(proven),
        "eligible_monitoring_count": eligible_monitoring,
        "unique_publication_url_count": len(unique_url),
        "quality_counts": dict(sorted(quality.items())),
    }


def load_brand_identity_map(cursor: Any) -> dict[int, str]:
    """brand_id → identity_key. Fail-soft: missing table (migration 027 not yet run)
    returns an empty map, which restores strict brand_id equality attribution.

    🔴 SAVEPOINT is mandatory, not defensive style: on PostgreSQL a failed statement
    aborts the whole transaction, so a bare try/except around a query against a table
    that does not exist would poison every later query on this same connection — the
    caller would see "current transaction is aborted" instead of a clean empty map.
    """
    cursor.execute("SAVEPOINT identity_map_probe")
    try:
        cursor.execute(
            "SELECT brand_id, identity_key FROM brand_identity_members WHERE brand_id IS NOT NULL"
        )
        rows = cursor.fetchall() or []
    except Exception:
        cursor.execute("ROLLBACK TO SAVEPOINT identity_map_probe")
        return {}
    finally:
        try:
            cursor.execute("RELEASE SAVEPOINT identity_map_probe")
        except Exception:
            pass
    mapping: dict[int, str] = {}
    for row in rows:
        data = dict(row)
        try:
            mapping[int(data["brand_id"])] = str(data["identity_key"])
        except (KeyError, TypeError, ValueError):
            continue
    return mapping


def load_strict_outcomes(
    *, since_days: int = 180, include_unverified_body: bool = False,
) -> dict[str, Any]:
    """Load the three active publication chains and source monitoring rows.

    ``include_unverified_body`` 见 ``attribute_observations`` 的说明:默认严格,
    开启后缺正文快照的发布物降级标注而不是丢弃,结果里 ``proven_event_count`` 与
    ``unverified_body_event_count`` 永远分开计数。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        from services.monitoring_identity_review import aggregate_eligible_sql
        cur.execute(
            f"""
            WITH publication_facts AS (
                SELECT o.article_id, i.publish_url, i.status,
                       i.published_at, i.published_at AS observed_at,
                       NULL::date AS manual_reported_publish_date,
                       'mhz_publish_order_items'::text AS publication_source,
                       i.id::bigint AS publication_source_id,
                       i.submitted_content_snapshot_hash AS body_snapshot_hash,
                       CASE WHEN i.submitted_content_snapshot_at IS NOT NULL
                            THEN 'channel_submission_snapshot'::text END AS body_observation_level
                  FROM mhz_publish_order_items i
                  JOIN mhz_publish_orders o ON o.id = i.order_id
                 WHERE i.published_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                UNION ALL
                SELECT o.article_id, i.publish_url, i.status,
                       i.published_at, i.published_at, NULL::date,
                       'publish_order_items'::text,
                       i.id::bigint,
                       NULL::char(64), 'submission_snapshot'::text
                  FROM publish_order_items i
                  JOIN publish_orders o ON o.id = i.order_id
                 WHERE i.published_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                UNION ALL
                SELECT m.article_id, m.platform_url, 'manual_confirmed'::text,
                       NULL::timestamp AS published_at, m.created_at,
                       m.publish_date,
                       'media_publications'::text,
                       m.id::bigint,
                       NULL::char(64), 'manual_registration_current_body_unverified'::text
                  FROM media_publications m
                 WHERE m.created_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                UNION ALL
                SELECT p.article_id, p.public_url, 'published'::text,
                       p.created_at, p.created_at, NULL::date,
                       'publish_records'::text, p.id::bigint,
                       p.submitted_content_snapshot_hash,
                       'extension_channel_submission_snapshot'::text
                  FROM publish_records p
                 -- [WO 自报收口 2026-08-19] 闸从「浏览器显式回报过」换成「服务端核实过」。
                 -- reported_explicitly 是来源位,证明不了事实为真;能进效果归因/KPI 的
                 -- 只有 verification_state='verified' 这一档。
                 WHERE p.status='success'
                   AND p.public_url_verification_state = 'verified'
                   AND NULLIF(BTRIM(p.public_url), '') IS NOT NULL
                   AND p.created_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
            )
            SELECT p.*,
                   COALESCE(
                       p.body_snapshot_hash,
                       CASE WHEN a.publication_snapshot_source=p.publication_source
                                  AND a.publication_snapshot_source_id=p.publication_source_id
                            THEN a.publication_snapshot->>'content_hash' END
                   ) AS publication_snapshot_hash,
                   a.style_family,
                   q.brand_id, q.industry
              FROM publication_facts p
              LEFT JOIN articles a ON a.id = p.article_id
              LEFT JOIN quotes q ON q.id = a.quote_id
            """,
            (since_days, since_days, since_days, since_days),
        )
        publications = [dict(row) for row in cur.fetchall()]
        cur.execute(
            f"""
            SELECT mr.id, mr.tested_at, mr.search_citations,
                   mr.sent_question_snapshot, mr.question_family,
                   mr.question_family_version, mr.keyword_type, mr.provider,
                   mr.model, mr.model_revision, mr.surface, mr.target_outcome,
                   mr.lineage_status, mr.identity_review_state,
                   mr.response_status, mt.brand_id
              FROM monitoring_results mr
              JOIN monitoring_tasks mt ON mt.id=mr.task_id
             WHERE mr.tested_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
               AND {aggregate_eligible_sql('mr')}
            """,
            (since_days,),
        )
        results = [dict(row) for row in cur.fetchall()]
        identity_by_brand = load_brand_identity_map(cur)
        return attribute_observations(
            publications, results,
            identity_by_brand=identity_by_brand,
            include_unverified_body=include_unverified_body,
        )
    finally:
        conn.close()
