"""Immutable, customer-facing quote pricing snapshots.

The pricing engine remains the only source of base prices.  This module only
applies a per-quote multiplier to already calculated customer price fields and
freezes the resulting payload for public links.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from typing import Any, Optional

from db.connection import get_connection
from services.quote_pricing_preferences import normalize_quote_markup_ratio


CALCULATION_VERSION = "quote-coefficient-application-v1"
LEGACY_FREEZE_VERSION = "legacy-exact-price-freeze-v1"
PRIVATE_CONTEXT_KEY = "_quote_calculation"
TIERS = ("entry", "standard", "flagship")

# [P1 容量合同 2026-08-08]「加入报价评估」= 把研究候选追加进**报价评估队列**,
#   走的就是本模块这套版本化不可变快照,不另起炉灶。
CAPACITY_EVALUATION_VERSION = "quote-capacity-evaluation-v1"
CAPACITY_EVALUATION_REASON = "capacity_evaluation_request"
CAPACITY_EVALUATION_KEY = "capacity_evaluation_requests"
MAX_CAPACITY_EVALUATION_CANDIDATES = 50


class QuoteSnapshotError(RuntimeError):
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
            "priority": "P1",
            "retryable": False,
            "details": self.details,
        }


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return deepcopy(default)
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return deepcopy(default)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _snapshot_hash(pricing_data: dict, clusters_data: Optional[dict]) -> str:
    payload = {"pricing_data": pricing_data, "clusters_data": clusters_data}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def quote_payload_hash(pricing_data: Any, clusters_data: Any) -> str:
    """Hash the exact persisted quote payload used as a mutation precondition."""
    return _snapshot_hash(
        _json_value(pricing_data, {}),
        _json_value(clusters_data, None),
    )


def stamp_calculation_context(
    pricing_data: dict,
    clusters_data: Optional[dict],
    *,
    coefficient: float,
) -> tuple[dict, Optional[dict]]:
    """Attach private source metadata needed for deterministic later previews."""
    normalized = normalize_quote_markup_ratio(coefficient)
    context = {
        "coefficient": normalized,
        "calculation_version": CALCULATION_VERSION,
    }
    pricing = deepcopy(pricing_data)
    pricing[PRIVATE_CONTEXT_KEY] = context
    clusters = deepcopy(clusters_data) if clusters_data else None
    if clusters is not None:
        clusters[PRIVATE_CONTEXT_KEY] = deepcopy(context)
    return pricing, clusters


def strip_private_context(payload: Any) -> None:
    if isinstance(payload, dict):
        payload.pop(PRIVATE_CONTEXT_KEY, None)


def _scaled_price(value: Any, factor: Decimal) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    try:
        amount = Decimal(str(value))
    except Exception:
        return value
    if amount <= 0:
        return value
    return int((amount * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _scale_pricing_data(pricing_data: dict, factor: Decimal) -> dict:
    pricing = deepcopy(pricing_data)
    for keyword in pricing.get("keywords") or []:
        if not isinstance(keyword, dict):
            continue
        for tier in TIERS:
            tier_value = keyword.get(tier)
            if isinstance(tier_value, dict) and "price" in tier_value:
                tier_value["price"] = _scaled_price(tier_value.get("price"), factor)
    for tier in TIERS:
        tier_value = (pricing.get("tiers") or {}).get(tier)
        if isinstance(tier_value, dict) and "total_price" in tier_value:
            tier_value["total_price"] = _scaled_price(tier_value.get("total_price"), factor)
    pricing.pop("brand_continuity", None)  # historical comparison is stale after a per-quote adjustment
    return pricing


def _scale_clusters_data(clusters_data: Optional[dict], factor: Decimal) -> Optional[dict]:
    if not clusters_data:
        return None
    clusters = deepcopy(clusters_data)
    for cluster in clusters.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        for keyword_group in ("core_keywords", "covered_keywords"):
            for keyword in cluster.get(keyword_group) or []:
                if not isinstance(keyword, dict):
                    continue
                for tier in TIERS:
                    tier_value = keyword.get(tier)
                    if isinstance(tier_value, dict) and "price" in tier_value:
                        tier_value["price"] = _scaled_price(tier_value.get("price"), factor)
        for tier in TIERS:
            tier_value = (cluster.get("pricing") or {}).get(tier)
            if not isinstance(tier_value, dict):
                continue
            for field in ("core_price", "full_price", "savings"):
                if field in tier_value:
                    tier_value[field] = _scaled_price(tier_value.get(field), factor)
    for tier in TIERS:
        tier_value = (clusters.get("tier_summaries") or {}).get(tier)
        if not isinstance(tier_value, dict):
            continue
        for field in ("core_price", "full_price", "savings"):
            if field in tier_value:
                tier_value[field] = _scaled_price(tier_value.get(field), factor)
    clusters.pop("brand_continuity", None)
    return clusters


def build_coefficient_preview(
    pricing_data: Any,
    clusters_data: Any,
    *,
    new_coefficient: float,
) -> dict:
    try:
        requested = float(new_coefficient)
    except (TypeError, ValueError) as exc:
        raise QuoteSnapshotError("QUOTE_COEFFICIENT_INVALID", "报价系数必须是 1.0–5.0 的数字。", status=422) from exc
    if not 1.0 <= requested <= 5.0:
        raise QuoteSnapshotError("QUOTE_COEFFICIENT_OUT_OF_RANGE", "报价系数必须在 1.0–5.0 之间。", status=422)
    pricing = _json_value(pricing_data, {})
    clusters = _json_value(clusters_data, None)
    context = pricing.get(PRIVATE_CONTEXT_KEY) if isinstance(pricing, dict) else None
    if not isinstance(context, dict) or context.get("coefficient") is None:
        raise QuoteSnapshotError(
            "QUOTE_COEFFICIENT_BASELINE_MISSING",
            "该历史报价缺少可验证的原始系数；请重新生成报价后再调整。",
            details={"recovery": "regenerate_quote"},
        )
    old_coefficient = normalize_quote_markup_ratio(context["coefficient"])
    normalized_new = normalize_quote_markup_ratio(requested)
    factor = Decimal(str(normalized_new)) / Decimal(str(old_coefficient))
    scaled_pricing = _scale_pricing_data(pricing, factor)
    scaled_clusters = _scale_clusters_data(clusters, factor)
    scaled_pricing, scaled_clusters = stamp_calculation_context(
        scaled_pricing,
        scaled_clusters,
        coefficient=normalized_new,
    )
    tiers = scaled_pricing.get("tiers") or {}
    summaries = {
        tier: {
            "total_price": int((tiers.get(tier) or {}).get("total_price") or 0),
            "total_articles": int((tiers.get(tier) or {}).get("total_articles") or 0),
        }
        for tier in TIERS
    }
    keywords = [
        {
            "id": keyword.get("id"),
            "keyword": keyword.get("keyword"),
            **{
                tier: int(((keyword.get(tier) or {}).get("price")) or 0)
                for tier in TIERS
            },
        }
        for keyword in scaled_pricing.get("keywords") or []
        if isinstance(keyword, dict)
    ]
    return {
        "old_coefficient": old_coefficient,
        "new_coefficient": normalized_new,
        "calculation_version": CALCULATION_VERSION,
        "summaries": summaries,
        "keywords": keywords,
        "keyword_count": len(keywords),
        "pricing_data": scaled_pricing,
        "clusters_data": scaled_clusters,
        "snapshot_hash": _snapshot_hash(scaled_pricing, scaled_clusters),
    }


def _next_version(cursor, quote_id: int) -> int:
    cursor.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM quote_pricing_snapshots WHERE quote_id = %s",
        (quote_id,),
    )
    return int(cursor.fetchone()["next_version"])


def _insert_snapshot(
    cursor,
    *,
    quote_id: int,
    brand_id: int,
    session_id: int,
    actor_user_id: int,
    actor_membership_id: Optional[int],
    old_coefficient: Optional[float],
    new_coefficient: Optional[float],
    reason: str,
    calculation_version: str,
    pricing_data: dict,
    clusters_data: Optional[dict],
) -> dict:
    version = _next_version(cursor, quote_id)
    snapshot_hash = _snapshot_hash(pricing_data, clusters_data)
    cursor.execute(
        """
        INSERT INTO quote_pricing_snapshots (
            quote_id, brand_id, selection_session_id, version, actor_user_id,
            actor_membership_id, previous_coefficient, coefficient, reason,
            calculation_version, pricing_snapshot, clusters_snapshot, snapshot_hash
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
        RETURNING id, version, created_at
        """,
        (
            quote_id, brand_id, session_id, version, actor_user_id,
            actor_membership_id, old_coefficient, new_coefficient, reason,
            calculation_version, _canonical_json(pricing_data),
            _canonical_json(clusters_data) if clusters_data is not None else None,
            snapshot_hash,
        ),
    )
    row = dict(cursor.fetchone())
    row["snapshot_hash"] = snapshot_hash
    return row


def save_coefficient_snapshot(
    quote_id: int,
    *,
    actor_user_id: int,
    actor_membership_id: Optional[int],
    new_coefficient: float,
    reason: str,
    expected_snapshot_hash: str,
) -> dict:
    reason = str(reason or "").strip()
    if not reason:
        raise QuoteSnapshotError("QUOTE_COEFFICIENT_REASON_REQUIRED", "保存本次报价系数前必须填写原因。", status=422)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.id AS session_id, s.brand_id, s.status, s.pricing_data, s.clusters_data,
                   q.active_pricing_snapshot_id
            FROM keyword_selection_sessions s
            JOIN quotes q ON q.id = s.quote_id
            WHERE s.quote_id = %s AND q.deleted_at IS NULL
            FOR UPDATE OF s, q
            """,
            (quote_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise QuoteSnapshotError("QUOTE_NOT_FOUND", "报价不存在或已归档。", status=404)
        if row["status"] != "pricing_pending_review":
            raise QuoteSnapshotError(
                "QUOTE_COEFFICIENT_STATE_CONFLICT",
                "只有待审核报价可以修改本次报价系数。",
                details={"status": row["status"]},
            )
        preview = build_coefficient_preview(
            row["pricing_data"], row.get("clusters_data"), new_coefficient=new_coefficient
        )
        if expected_snapshot_hash != preview["snapshot_hash"]:
            raise QuoteSnapshotError(
                "QUOTE_COEFFICIENT_PREVIEW_STALE",
                "报价已变化，请刷新预览后再保存。",
                details={"recovery": "refresh_preview"},
            )
        active_snapshot_id = row.get("active_pricing_snapshot_id")
        if active_snapshot_id:
            cursor.execute(
                "SELECT id, version, created_at, snapshot_hash FROM quote_pricing_snapshots "
                "WHERE id=%s AND quote_id=%s",
                (active_snapshot_id, quote_id),
            )
            active_snapshot = cursor.fetchone()
            if active_snapshot and active_snapshot["snapshot_hash"].strip() == preview["snapshot_hash"]:
                conn.rollback()
                return {**preview, "snapshot": dict(active_snapshot), "already_saved": True}
        snapshot = _insert_snapshot(
            cursor,
            quote_id=quote_id,
            brand_id=int(row["brand_id"]),
            session_id=int(row["session_id"]),
            actor_user_id=actor_user_id,
            actor_membership_id=actor_membership_id,
            old_coefficient=preview["old_coefficient"],
            new_coefficient=preview["new_coefficient"],
            reason=reason,
            calculation_version=CALCULATION_VERSION,
            pricing_data=preview["pricing_data"],
            clusters_data=preview["clusters_data"],
        )
        cursor.execute(
            """
            UPDATE keyword_selection_sessions
            SET pricing_data=%s, clusters_data=%s, active_pricing_snapshot_id=%s, updated_at=NOW()
            WHERE quote_id=%s
            """,
            (
                _canonical_json(preview["pricing_data"]),
                _canonical_json(preview["clusters_data"]) if preview["clusters_data"] is not None else None,
                snapshot["id"], quote_id,
            ),
        )
        cursor.execute(
            "UPDATE quotes SET active_pricing_snapshot_id=%s, updated_at=NOW() WHERE id=%s",
            (snapshot["id"], quote_id),
        )
        conn.commit()
        return {**preview, "snapshot": snapshot}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def publish_frozen_snapshot(
    quote_id: int,
    *,
    actor_user_id: int,
    actor_membership_id: Optional[int],
    pricing_data: dict,
    clusters_data: Optional[dict],
    reason: str,
    expected_source_hash: str,
) -> dict:
    """Atomically freeze the exact public payload and mark the session quoted."""
    context = pricing_data.get(PRIVATE_CONTEXT_KEY) or {}
    coefficient = context.get("coefficient")
    calculation_version = context.get("calculation_version") or LEGACY_FREEZE_VERSION
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.id AS session_id, s.brand_id, s.status, s.pricing_data,
                   s.clusters_data, q.active_pricing_snapshot_id
            FROM keyword_selection_sessions s JOIN quotes q ON q.id=s.quote_id
            WHERE s.quote_id=%s AND q.deleted_at IS NULL FOR UPDATE OF s, q
            """,
            (quote_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise QuoteSnapshotError("QUOTE_NOT_FOUND", "报价不存在或已归档。", status=404)
        if row["status"] == "quoted" and row.get("active_pricing_snapshot_id"):
            conn.rollback()
            return {"id": int(row["active_pricing_snapshot_id"]), "already_sent": True}
        if row["status"] != "pricing_pending_review":
            raise QuoteSnapshotError("QUOTE_PUBLISH_STATE_CONFLICT", "报价状态已变化，请刷新后重试。")
        current_source_hash = quote_payload_hash(row["pricing_data"], row.get("clusters_data"))
        if current_source_hash != expected_source_hash:
            raise QuoteSnapshotError(
                "QUOTE_PUBLISH_SOURCE_STALE",
                "报价已被其他操作更新，请刷新后重新审核发送。",
                details={"recovery": "refresh_quote"},
            )
        snapshot = _insert_snapshot(
            cursor,
            quote_id=quote_id,
            brand_id=int(row["brand_id"]),
            session_id=int(row["session_id"]),
            actor_user_id=actor_user_id,
            actor_membership_id=actor_membership_id,
            old_coefficient=coefficient,
            new_coefficient=coefficient,
            reason=reason,
            calculation_version=calculation_version,
            pricing_data=pricing_data,
            clusters_data=clusters_data,
        )
        cursor.execute(
            """
            UPDATE keyword_selection_sessions
            SET status='quoted', pricing_data=%s, clusters_data=%s,
                active_pricing_snapshot_id=%s, updated_at=NOW()
            WHERE quote_id=%s
            """,
            (
                _canonical_json(pricing_data),
                _canonical_json(clusters_data) if clusters_data is not None else None,
                snapshot["id"], quote_id,
            ),
        )
        cursor.execute(
            "UPDATE quotes SET active_pricing_snapshot_id=%s, updated_at=NOW() WHERE id=%s",
            (snapshot["id"], quote_id),
        )
        conn.commit()
        return snapshot
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_capacity_candidates(candidates: Any) -> list[dict]:
    """把「加入报价评估」的候选规成可冻结的最小形状。

    🔴 **候选里不许带价格**。定价是报价引擎的事,由服务商重新算价后走既有报价链出新版本;
       在这里收一个前端传来的价格 = 前端算钱 = 08_billing §3.3 明令禁止。
    """
    if not isinstance(candidates, list) or not candidates:
        raise QuoteSnapshotError(
            "QUOTE_CAPACITY_CANDIDATES_REQUIRED",
            "请至少选择一个要加入报价评估的候选问题。",
            status=422,
        )
    if len(candidates) > MAX_CAPACITY_EVALUATION_CANDIDATES:
        raise QuoteSnapshotError(
            "QUOTE_CAPACITY_CANDIDATES_TOO_MANY",
            f"一次最多加入 {MAX_CAPACITY_EVALUATION_CANDIDATES} 个候选,请分批提交。",
            status=422,
        )
    banned = {"price", "selling_price", "entry", "standard", "flagship", "cost_per_article",
              "markup_ratio", "required_articles", "articles"}
    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in candidates:
        source = item if isinstance(item, dict) else {"keyword": item}
        keyword = str(source.get("keyword") or "").strip()
        if not keyword:
            raise QuoteSnapshotError(
                "QUOTE_CAPACITY_CANDIDATE_EMPTY", "候选问题不能为空。", status=422
            )
        leaked = sorted(key for key in source.keys() if key in banned)
        if leaked:
            raise QuoteSnapshotError(
                "QUOTE_CAPACITY_CANDIDATE_PRICE_NOT_ALLOWED",
                "加入报价评估只提交问题本身,价格与篇数由报价引擎重新计算。",
                status=422,
                details={"rejected_fields": leaked},
            )
        if keyword in seen:
            continue
        seen.add(keyword)
        cleaned.append({
            "keyword": keyword,
            "plan_item_id": str(source.get("plan_item_id") or "").strip() or None,
            "gap_reason": str(source.get("gap_reason") or "").strip() or None,
        })
    return cleaned


def _existing_evaluation_keys(snapshot_payload: Any) -> set[str]:
    payload = _json_value(snapshot_payload, {})
    if not isinstance(payload, dict):
        return set()
    requests = payload.get(CAPACITY_EVALUATION_KEY)
    if not isinstance(requests, list):
        return set()
    return {
        str(entry.get("idempotency_key"))
        for entry in requests
        if isinstance(entry, dict) and entry.get("idempotency_key")
    }


def record_capacity_evaluation_request(
    quote_id: int,
    *,
    actor_user_id: int,
    actor_membership_id: Optional[int],
    candidates: Any,
    origin: str,
    idempotency_key: str,
) -> dict:
    """【P1 · 加入报价评估】把研究候选追加进报价评估队列,作为一个**新的不可变快照版本**。

    三条硬约束(资金类严审的落点):
      1. **不动已冻结的那一版**。新版本只是 append-only 记录,`quotes.active_pricing_snapshot_id`
         与 `keyword_selection_sessions.pricing_data` 一律不改 —— 客户面看到的报价一字不变。
         (08_billing §7.5:不得读取当前价格/关系/倍率重算历史。)
      2. **不定价、不改容量**。本调用只登记"请把这几个问题纳入下一次报价评估",
         真正的容量增加仍然走既有链路:服务商重新算价 → 新报价快照 → 客户确认付款。
      3. **幂等**。同一个 `idempotency_key` 重复提交只产生一条记录(不新开版本)。
    """
    reason_origin = str(origin or "").strip() or "unknown"
    key = str(idempotency_key or "").strip()
    if not key:
        raise QuoteSnapshotError(
            "QUOTE_CAPACITY_IDEMPOTENCY_REQUIRED", "缺少幂等键,请重试。", status=422
        )
    cleaned = _normalize_capacity_candidates(candidates)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT q.id, q.brand_id, q.active_pricing_snapshot_id,
                   s.id AS session_id
              FROM quotes q
              LEFT JOIN keyword_selection_sessions s ON s.quote_id = q.id
             WHERE q.id = %s AND q.deleted_at IS NULL
             FOR UPDATE OF q
            """,
            (quote_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise QuoteSnapshotError("QUOTE_NOT_FOUND", "报价不存在或已归档。", status=404)
        if not row.get("session_id"):
            raise QuoteSnapshotError(
                "QUOTE_CAPACITY_SESSION_MISSING",
                "该报价单没有关联选词会话,无法登记评估请求;请从报价中心重新生成报价。",
                details={"recovery": "regenerate_quote"},
            )
        active_id = row.get("active_pricing_snapshot_id")
        if not active_id:
            raise QuoteSnapshotError(
                "QUOTE_CAPACITY_SNAPSHOT_NOT_FROZEN",
                "该报价尚未冻结,请直接在报价中心把问题加进词包后重新算价。",
                details={"recovery": "add_keywords_before_freeze"},
            )
        cursor.execute(
            "SELECT id, version, pricing_snapshot, clusters_snapshot "
            "FROM quote_pricing_snapshots WHERE quote_id=%s ORDER BY version DESC LIMIT 1",
            (quote_id,),
        )
        latest = cursor.fetchone()
        if not latest:
            raise QuoteSnapshotError(
                "QUOTE_CAPACITY_SNAPSHOT_NOT_FROZEN",
                "该报价尚未冻结,请直接在报价中心把问题加进词包后重新算价。",
                details={"recovery": "add_keywords_before_freeze"},
            )
        if key in _existing_evaluation_keys(latest.get("pricing_snapshot")):
            conn.rollback()
            return {
                "already_recorded": True,
                "quote_id": quote_id,
                "version": int(latest["version"]),
                "snapshot_id": int(latest["id"]),
                "candidates": cleaned,
            }

        # 以**最新版本**为基,只往上追加一条评估请求。不重算任何价格字段。
        pricing_payload = _json_value(latest.get("pricing_snapshot"), {})
        if not isinstance(pricing_payload, dict):
            raise QuoteSnapshotError(
                "QUOTE_CAPACITY_SNAPSHOT_SHAPE",
                "该报价快照结构异常,请联系管理员处理。",
            )
        pricing_payload = deepcopy(pricing_payload)
        requests = pricing_payload.get(CAPACITY_EVALUATION_KEY)
        if not isinstance(requests, list):
            requests = []
        requests.append({
            "idempotency_key": key,
            "origin": reason_origin,
            "actor_user_id": int(actor_user_id),
            "candidates": cleaned,
            "status": "pending_repricing",
            "contract_version": CAPACITY_EVALUATION_VERSION,
        })
        pricing_payload[CAPACITY_EVALUATION_KEY] = requests

        snapshot = _insert_snapshot(
            cursor,
            quote_id=quote_id,
            brand_id=int(row["brand_id"]),
            session_id=int(row["session_id"]),
            actor_user_id=actor_user_id,
            actor_membership_id=actor_membership_id,
            old_coefficient=None,
            new_coefficient=None,
            reason=f"{CAPACITY_EVALUATION_REASON}:{reason_origin}",
            calculation_version=CAPACITY_EVALUATION_VERSION,
            pricing_data=pricing_payload,
            clusters_data=_json_value(latest.get("clusters_snapshot"), None),
        )
        # 🔴 刻意不写 quotes.active_pricing_snapshot_id / 不写 session.pricing_data:
        #    评估请求不是新报价,客户面冻结的那一版必须原封不动。
        conn.commit()
        return {
            "already_recorded": False,
            "quote_id": quote_id,
            "version": int(snapshot["version"]),
            "snapshot_id": int(snapshot["id"]),
            "candidates": cleaned,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_capacity_evaluation_requests(quote_id: int) -> list[dict]:
    """读最新版本里已登记的评估请求(只读 · 给报价中心/P4 显示用)。"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pricing_snapshot FROM quote_pricing_snapshots "
            "WHERE quote_id=%s ORDER BY version DESC LIMIT 1",
            (quote_id,),
        )
        row = cursor.fetchone()
        if not row:
            return []
        payload = _json_value(row.get("pricing_snapshot"), {})
        requests = payload.get(CAPACITY_EVALUATION_KEY) if isinstance(payload, dict) else None
        return [entry for entry in requests or [] if isinstance(entry, dict)]
    finally:
        conn.close()


def get_frozen_snapshot(quote_id: int) -> Optional[dict]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ps.id, ps.version, ps.pricing_snapshot, ps.clusters_snapshot,
                   ps.snapshot_hash, ps.calculation_version, ps.created_at
            FROM quotes q
            JOIN quote_pricing_snapshots ps ON ps.id=q.active_pricing_snapshot_id
            WHERE q.id=%s AND q.deleted_at IS NULL
            """,
            (quote_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
