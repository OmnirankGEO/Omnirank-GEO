"""Durable name-evidence verification for persisted writing competitors."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import httpx

from tools.api_source_pool import SourcePool, acall_with_failover, get_metaso_pool
from tools.llm_call_tracker import llm_track


_VERIFICATION_LOCK_NAMESPACE = 920721
_PROVIDER_PURPOSE = "writing_competitor_name_verify"
_DEFAULT_MAX_INFLIGHT = 8


class CompetitorVerificationUnavailable(RuntimeError):
    """Raised before any state change when every verification source is unavailable."""


class CompetitorVerificationConflict(RuntimeError):
    """Raised when the persisted verification inputs changed during a provider call."""


class CompetitorVerificationForbidden(RuntimeError):
    """Raised when live quote access no longer exists at commit time."""


class CompetitorVerificationNotFound(RuntimeError):
    """Raised when the quote disappears before the verification result is persisted."""


class CompetitorVerificationInProgress(RuntimeError):
    """Raised before provider work when the same quote is already being verified."""


class CompetitorVerificationCapacityExceeded(RuntimeError):
    """Raised before any DB checkout when this process has no verification capacity."""


class _ProviderAttemptError(RuntimeError):
    """Sanitized provider error safe for durable telemetry."""


class VerificationAdmissionGate:
    """Non-blocking process-local admission guard for long provider requests."""

    def __init__(self, limit: int):
        self.limit = max(1, min(int(limit), 16))
        self._semaphore = threading.BoundedSemaphore(self.limit)

    def try_acquire(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()


def _configured_max_inflight() -> int:
    raw = str(os.getenv("WRITING_COMPETITOR_VERIFY_MAX_INFLIGHT", _DEFAULT_MAX_INFLIGHT)).strip()
    try:
        return max(1, min(int(raw), 16))
    except ValueError:
        return _DEFAULT_MAX_INFLIGHT


_VERIFICATION_ADMISSION_GATE = VerificationAdmissionGate(_configured_max_inflight())


def _normalized_text(value: Any) -> str:
    text = str(value or "").casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _public_http_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return ""
    try:
        if not ipaddress.ip_address(host).is_global:
            return ""
    except ValueError:
        pass
    return raw[:1000]


def _active_items(items: Iterable[object]) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if isinstance(item, dict) and not item.get("excluded") and str(item.get("name") or "").strip()
    ]


def competitor_name_is_verified(item: object) -> bool:
    return bool(
        isinstance(item, dict)
        and (item.get("name_verified") is True or item.get("human_verified_name") is True)
    )


def competitor_verification_fingerprint(
    raw_list: object,
    raw_mode: object,
    industry: object,
    region: object,
    brand_id: object,
) -> str:
    """Hash every persisted input that can change the verification result or its tenant."""

    encoded = json.dumps(
        {
            "brand_id": brand_id,
            "competitor_list": raw_list or "",
            "competitor_mode": raw_mode or "evidence_only",
            "industry": str(industry or ""),
            "region": str(region or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def try_acquire_quote_verification_lock(connection: Any, quote_id: int) -> bool:
    """Acquire a crash-recoverable, cross-worker session lock without waiting."""

    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
            (_VERIFICATION_LOCK_NAMESPACE, int(quote_id)),
        )
        row = cursor.fetchone()
        return bool(row and (row.get("acquired") if isinstance(row, dict) else row[0]))
    finally:
        cursor.close()


def release_quote_verification_lock(connection: Any, quote_id: int) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT pg_advisory_unlock(%s, %s)",
            (_VERIFICATION_LOCK_NAMESPACE, int(quote_id)),
        )
    finally:
        cursor.close()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.isdigit():
            parsed = int(normalized)
            return parsed if parsed > 0 else None
    return None


def require_live_quote_access(cursor: Any, user: dict[str, Any], brand_id: Any) -> None:
    """Lock and validate the current owner/assignment rows in the write transaction."""

    if bool(user.get("is_admin")):
        return
    user_id = _positive_int(user.get("user_id") or user.get("id"))
    if not user_id:
        raise CompetitorVerificationForbidden("登录状态已失效，请重新登录")
    if brand_id is None:
        return
    normalized_brand_id = _positive_int(brand_id)
    if not normalized_brand_id:
        raise CompetitorVerificationForbidden("客户归属异常，请刷新后重试")

    cursor.execute(
        """
        SELECT owner_user_id
          FROM brands
         WHERE id = %s
           AND (is_deleted IS NULL OR is_deleted = FALSE)
         FOR SHARE
        """,
        (normalized_brand_id,),
    )
    brand = cursor.fetchone()
    if not brand:
        raise CompetitorVerificationForbidden("核验期间客户访问权限已变更，请刷新后重试")
    owner_user_id = brand.get("owner_user_id") if isinstance(brand, dict) and brand else (brand[0] if brand else None)
    if owner_user_id == user_id:
        return

    cursor.execute(
        """
        SELECT 1
          FROM user_clients
         WHERE user_id = %s
           AND brand_id = %s
         FOR SHARE
        """,
        (user_id, normalized_brand_id),
    )
    if cursor.fetchone() is None:
        raise CompetitorVerificationForbidden("核验期间客户访问权限已变更，请刷新后重试")


def load_competitor_verification_inputs(
    connection: Any,
    *,
    quote_id: int,
    user: dict[str, Any],
) -> tuple[dict[str, Any], list[object], str]:
    """Read inputs and perform the pre-provider live auth on the lock session."""

    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT competitor_list, competitor_mode, industry, city, brand_id
              FROM quotes
             WHERE id = %s
            """,
            (quote_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise CompetitorVerificationNotFound("报价单不存在")

        raw_list = row.get("competitor_list") or ""
        try:
            candidates = json.loads(raw_list) if raw_list else []
        except (TypeError, ValueError):
            raise CompetitorVerificationConflict("候选名单格式异常，请刷新后重试") from None
        if not isinstance(candidates, list):
            raise CompetitorVerificationConflict("候选名单格式异常，请刷新后重试")

        require_live_quote_access(cursor, user, row.get("brand_id"))
        fingerprint = competitor_verification_fingerprint(
            raw_list,
            row.get("competitor_mode"),
            row.get("industry"),
            row.get("city"),
            row.get("brand_id"),
        )
        return row, candidates, fingerprint
    finally:
        cursor.close()


def persist_competitor_verification_result(
    connection: Any,
    *,
    quote_id: int,
    expected_fingerprint: str,
    competitors: list[object],
    result_mode: str,
    user: dict[str, Any],
) -> None:
    """CAS and persist a provider result under live authorization in one transaction."""

    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT competitor_list, competitor_mode, industry, city, brand_id
              FROM quotes
             WHERE id = %s
             FOR UPDATE
            """,
            (quote_id,),
        )
        current = cursor.fetchone()
        if not current:
            raise CompetitorVerificationNotFound("报价单不存在")
        current_fingerprint = competitor_verification_fingerprint(
            current.get("competitor_list"),
            current.get("competitor_mode"),
            current.get("industry"),
            current.get("city"),
            current.get("brand_id"),
        )
        if current_fingerprint != expected_fingerprint:
            raise CompetitorVerificationConflict("候选名单或业务范围已更新，请刷新后重试")

        require_live_quote_access(cursor, user, current.get("brand_id"))
        cursor.execute(
            """
            UPDATE quotes
               SET competitor_list = %s,
                   competitor_mode = %s
             WHERE id = %s
            """,
            (json.dumps(competitors, ensure_ascii=False), result_mode, quote_id),
        )
    finally:
        cursor.close()


async def verify_persisted_competitor_names(
    competitors: list[object],
    *,
    industry: str,
    region: str,
    quote_id: int,
    request_id: str,
    user_id: int | None = None,
    provider_pool: SourcePool | None = None,
    max_concurrency: int = 4,
) -> dict[str, Any]:
    """Verify pending names via the repository's throttled, failover-capable Metaso pool."""

    updated = [dict(item) if isinstance(item, dict) else item for item in competitors]
    pending_indexes = [
        index
        for index, item in enumerate(updated)
        if isinstance(item, dict)
        and not item.get("excluded")
        and str(item.get("name") or "").strip()
        and not competitor_name_is_verified(item)
    ]
    if not pending_indexes:
        active = _active_items(updated)
        return {
            "competitors": updated,
            "attempted_count": 0,
            "newly_verified": 0,
            "verified_count": len(active),
            "pending_names": [],
            "provider_error_count": 0,
        }

    pool = provider_pool or get_metaso_pool()
    if pool.size == 0:
        raise CompetitorVerificationUnavailable("名称核验服务暂不可用，请稍后重试")

    semaphore = asyncio.Semaphore(max(1, min(int(max_concurrency or 1), 8)))

    async with httpx.AsyncClient(timeout=15.0) as client:
        async def verify_one(index: int) -> tuple[int, str, bool]:
            item = updated[index]
            assert isinstance(item, dict)
            name = str(item.get("name") or "").strip()
            needle = _normalized_text(name)
            if len(needle) < 2:
                return index, "", False
            query = " ".join(part for part in (f'"{name}"', region, industry) if part).strip()

            async def provider_attempt(api_key: str) -> dict[str, Any]:
                metadata = {
                    "candidate_index": index,
                    "purpose": _PROVIDER_PURPOSE,
                    "quote_id": int(quote_id),
                    "request_id": request_id,
                }
                async with semaphore:
                    async with llm_track(
                        _PROVIDER_PURPOSE,
                        "metaso",
                        model="search",
                        quote_id=int(quote_id),
                        user_id=user_id,
                        metadata=metadata,
                    ) as tracker:
                        try:
                            response = await client.post(
                                "https://metaso.cn/api/v1/search",
                                headers={
                                    "Authorization": f"Bearer {api_key}",
                                    "Accept": "application/json",
                                    "Content-Type": "application/json",
                                },
                                json={
                                    "q": query,
                                    "scope": "webpage",
                                    "size": 10,
                                    "includeSummary": False,
                                    "conciseSnippet": True,
                                },
                            )
                            if response.status_code != 200:
                                raise _ProviderAttemptError(f"metaso_http_{int(response.status_code)}")
                            payload = response.json()
                            if not isinstance(payload, dict):
                                raise _ProviderAttemptError("metaso_invalid_payload")
                            rows = payload.get("webpages", payload.get("results", []))
                            if not isinstance(rows, list):
                                raise _ProviderAttemptError("metaso_invalid_payload")
                        except _ProviderAttemptError as exc:
                            tracker.record(success=False, error_msg=str(exc))
                            raise
                        except (httpx.HTTPError, ValueError, TypeError):
                            tracker.record(success=False, error_msg="metaso_transport_error")
                            raise _ProviderAttemptError("metaso_transport_error") from None
                        tracker.record(success=True)
                        return {"rows": rows}

            try:
                payload = await acall_with_failover(
                    pool,
                    provider_attempt,
                    throttle=True,
                    primary_first=True,
                )
            except Exception:
                return index, "", True

            rows = payload["rows"]
            for row in rows[:10]:
                if not isinstance(row, dict):
                    continue
                haystack = _normalized_text(
                    f"{row.get('title') or ''} {row.get('snippet') or row.get('conciseSnippet') or ''}"
                )
                source_url = _public_http_url(row.get("link") or row.get("url"))
                if source_url and needle in haystack:
                    return index, source_url, False
            return index, "", False

        results = await asyncio.gather(*(verify_one(index) for index in pending_indexes))

    provider_error_count = sum(1 for _, _, failed in results if failed)
    if results and provider_error_count == len(results):
        raise CompetitorVerificationUnavailable("名称核验服务暂不可用，请稍后重试")

    verified_at = datetime.now(timezone.utc).isoformat()
    newly_verified = 0
    for index, source_url, _ in results:
        item = updated[index]
        assert isinstance(item, dict)
        if source_url:
            item.update(
                {
                    "name_verified": True,
                    "name_verified_at": verified_at,
                    "name_verification_method": "metaso_name_search_v1",
                    "name_verification_source_url": source_url,
                    "verify_source": source_url,
                }
            )
            newly_verified += 1

    active = _active_items(updated)
    pending_names = [
        str(item.get("name") or "").strip()
        for item in active
        if not competitor_name_is_verified(item)
    ]
    return {
        "competitors": updated,
        "attempted_count": len(pending_indexes),
        "newly_verified": newly_verified,
        "verified_count": len(active) - len(pending_names),
        "pending_names": pending_names,
        "provider_error_count": provider_error_count,
    }


async def verify_quote_competitors_with_session_lock(
    connection: Any,
    *,
    quote_id: int,
    request_id: str,
    user: dict[str, Any],
    user_id: int | None = None,
    provider_pool: SourcePool | None = None,
) -> dict[str, Any]:
    """Run read, provider work and final CAS with one session-locked connection."""

    lock_acquired = False
    connection.autocommit = True
    try:
        if not try_acquire_quote_verification_lock(connection, quote_id):
            raise CompetitorVerificationInProgress("名称核验正在进行，请稍后查看结果")
        lock_acquired = True

        row, candidates, initial_fingerprint = load_competitor_verification_inputs(
            connection,
            quote_id=quote_id,
            user=user,
        )

        # The session lock survives transaction boundaries. Autocommit remains enabled
        # while the provider is running, so no open transaction or row lock spans I/O.
        result = await verify_persisted_competitor_names(
            candidates,
            industry=str(row.get("industry") or ""),
            region=str(row.get("city") or ""),
            quote_id=quote_id,
            request_id=request_id,
            user_id=user_id,
            provider_pool=provider_pool,
        )
        verified_candidates = result["competitors"]
        active = [
            item
            for item in verified_candidates
            if isinstance(item, dict) and not item.get("excluded")
        ]
        result_mode = (
            "real"
            if active and all(competitor_name_is_verified(item) for item in active)
            else ("semi" if active else "evidence_only")
        )

        connection.autocommit = False
        try:
            persist_competitor_verification_result(
                connection,
                quote_id=quote_id,
                expected_fingerprint=initial_fingerprint,
                competitors=verified_candidates,
                result_mode=result_mode,
                user=user,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.autocommit = True

        return {**result, "mode": result_mode}
    finally:
        if not connection.autocommit:
            try:
                connection.rollback()
            finally:
                connection.autocommit = True
        if lock_acquired:
            release_quote_verification_lock(connection, quote_id)


async def execute_competitor_verification_request(
    *,
    quote_id: int,
    request_id: str,
    user: dict[str, Any],
    user_id: int | None,
    initial_access_check: Callable[[], None],
    connection_factory: Callable[[], Any],
    provider_pool: SourcePool | None = None,
    admission_gate: VerificationAdmissionGate | None = None,
) -> dict[str, Any]:
    """Apply non-blocking admission before any DB checkout and own one DB lease."""

    gate = admission_gate or _VERIFICATION_ADMISSION_GATE
    if not gate.try_acquire():
        raise CompetitorVerificationCapacityExceeded("名称核验请求较多，请稍后重试")

    connection = None
    try:
        initial_access_check()
        connection = connection_factory()
        return await verify_quote_competitors_with_session_lock(
            connection,
            quote_id=quote_id,
            request_id=request_id,
            user=user,
            user_id=user_id,
            provider_pool=provider_pool,
        )
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            gate.release()
