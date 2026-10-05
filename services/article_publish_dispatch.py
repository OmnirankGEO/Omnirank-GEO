"""Single pre-dispatch choke point for every real article provider call.

The rollout gate is intentionally disabled by default.  When it is enabled,
the service proves that the caller rendered its payload from the current
canonical article, re-checks review/evidence state, reviews the exact outgoing
payload, freezes its hash, and only then performs the provider call.  No
database transaction is held across the network request.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import re
from typing import Any, Callable, Literal

from services.article_review_gate import (
    ArticlePublicationBlocked,
    assert_publication_eligible,
    is_publication_review_gate_enabled,
)
DispatchKind = Literal["publish", "publish_wemedia"]
logger = logging.getLogger("GEO-Article-Publish-Dispatch")

_INVALID_GENERATION_PLACEHOLDER = re.compile(
    r"(?:自动生成失败|生成失败.{0,12}占位|占位内容|正文生成中|文章生成中|正在生成正文|\[生成中\])",
    re.IGNORECASE,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ArticleDispatchSnapshot:
    article_id: int
    source: str
    title: str
    content: str
    content_hash: str
    canonical_content_hash: str | None
    evidence_manifest_hash: str | None
    publication_profile: str | None
    review_reason: str
    # [统一 R3 · 2026-07-23 §五] 发布冻结快照血缘:本次外发判定所用的法律
    # 禁止清单版本(additive · 默认 None 兼容旧调用方,不回填历史快照)。
    legal_prohibition_catalog_version: str | None = None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _blocked(article_id: int, reason: str, message: str) -> ArticlePublicationBlocked:
    return ArticlePublicationBlocked({
        "eligible": False,
        "article_id": article_id,
        "reason": reason,
        "message": message,
    })


def _load_contact_consent_context(article_id: int) -> tuple[bool | None, int | None]:
    """Read immutable article consent without changing legacy unknown rows."""
    if article_id <= 0:
        return None, None
    from db.connection import get_connection
    from services.contact_placeholder import contact_consent_from_snapshot

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.generation_request_snapshot, q.brand_id
              FROM articles a
              LEFT JOIN quotes q ON q.id=a.quote_id
             WHERE a.id=%s
            """,
            (article_id,),
        )
        row = cur.fetchone() or {}
        return (
            contact_consent_from_snapshot(row.get("generation_request_snapshot")),
            row.get("brand_id"),
        )
    finally:
        conn.close()


def _load_evidence_precision_context(
    article_id: int,
) -> tuple[str | None, dict[str, Any], dict[str, Any], str | None]:
    """Load the versioned precision contract without opting legacy rows in."""
    if article_id <= 0:
        return None, {}, {}
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT generation_request_snapshot, evidence_pack, brand_fact_snapshot
              FROM articles
             WHERE id=%s
            """,
            (article_id,),
        )
        row = cur.fetchone() or {}
        snapshot = row.get("generation_request_snapshot")
        evidence_pack = row.get("evidence_pack")
        brand_fact_snapshot = row.get("brand_fact_snapshot")
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except Exception:
                snapshot = {}
        if isinstance(evidence_pack, str):
            try:
                evidence_pack = json.loads(evidence_pack)
            except Exception:
                evidence_pack = {}
        if isinstance(brand_fact_snapshot, str):
            try:
                brand_fact_snapshot = json.loads(brand_fact_snapshot)
            except Exception:
                brand_fact_snapshot = {}
        version = (
            snapshot.get("evidence_precision_contract_version")
            if isinstance(snapshot, dict) else None
        )
        legal_catalog_version = (
            snapshot.get("legal_prohibition_catalog_version")
            if isinstance(snapshot, dict) else None
        )
        return (
            str(version or "").strip() or None,
            evidence_pack if isinstance(evidence_pack, dict) else {},
            brand_fact_snapshot if isinstance(brand_fact_snapshot, dict) else {},
            str(legal_catalog_version or "").strip()[:64] or None,
        )
    finally:
        conn.close()


def prepare_article_dispatch_snapshot(
    *,
    article_id: int,
    source_title: str,
    source_content: str,
    outgoing_title: str,
    outgoing_content: str,
    source: str,
) -> ArticleDispatchSnapshot:
    """Return the immutable payload that the provider must receive.

    ``source_*`` is the canonical article value used by the caller before
    deterministic channel rendering.  ``outgoing_*`` is the exact payload
    about to be sent.  With the gate disabled this is a schema-free identity
    operation so legacy deployments remain compatible.
    """
    try:
        article_id = int(article_id or 0)
    except (TypeError, ValueError):
        article_id = 0
    outgoing_title = str(outgoing_title or "")
    outgoing_content = str(outgoing_content or "")
    from services.contact_placeholder import enforce_contact_opt_out, has_contact_risk

    # Every persisted article has an immutable article-level choice. Read it
    # even when heuristic scanning sees no phone/WeChat: an unlabelled exact
    # customer website or address must not bypass an explicit opt-out.
    if article_id > 0 or has_contact_risk(outgoing_content):
        try:
            contact_consent, contact_brand_id = _load_contact_consent_context(article_id)
        except Exception as exc:
            logger.warning("contact consent read failed article=%s: %s", article_id, exc)
            if article_id > 0:
                raise _blocked(
                    article_id,
                    "contact_consent_unavailable",
                    "文章联系方式授权状态暂时不可核验，provider 未调用。",
                )
            contact_consent, contact_brand_id = None, None
        if contact_consent is False:
            outgoing_content = enforce_contact_opt_out(outgoing_content, contact_brand_id)
    if not outgoing_title.strip() or not outgoing_content.strip():
        raise _blocked(article_id, "empty_dispatch_payload", "文章标题或正文为空，不能提交发布。")

    # This narrow sanity gate is never controlled by the staged review flag.
    # It blocks known invalid generation artifacts and the evidence-first
    # policy's explicit hard failures, while leaving ordinary legacy prose and
    # legitimate sourced mentions untouched.
    if _INVALID_GENERATION_PLACEHOLDER.search(f"{outgoing_title}\n{outgoing_content}"):
        raise _blocked(
            article_id,
            "invalid_generation_placeholder",
            "生成失败或生成中占位内容禁止进入发布通道。",
        )
    from writing.evidence_first_policy import evaluate_content_trust

    minimum_trust = evaluate_content_trust(
        outgoing_title,
        outgoing_content,
        respect_feature_flag=False,
    )
    if minimum_trust.hard:
        raise _blocked(
            article_id,
            "minimum_outbound_trust_failure",
            "实际发送内容触发不可关闭的证据优先硬门，provider 未调用。",
        )

    # Only rows generated under the versioned precision contract enter this
    # new hard gate.  Historical content remains on its signed compatibility
    # path; newly generated content can never shed claim-level lineage merely
    # because the broader human-review rollout flag is still off.
    frozen_legal_catalog_version: str | None = None
    if article_id > 0:
        try:
            precision_context = _load_evidence_precision_context(article_id)
            precision_version, precision_pack, brand_fact_snapshot = precision_context[:3]
            if len(precision_context) > 3:
                frozen_legal_catalog_version = precision_context[3]
        except Exception as exc:
            logger.warning("evidence precision read failed article=%s: %s", article_id, exc)
            raise _blocked(
                article_id,
                "evidence_precision_unavailable",
                "文章逐项证据状态暂时不可核验，provider 未调用。",
            )
        if precision_version:
            from writing.evidence_precision_policy import (
                EVIDENCE_PRECISION_CONTRACT_VERSION,
                evaluate_evidence_precision,
            )

            if precision_version != EVIDENCE_PRECISION_CONTRACT_VERSION:
                raise _blocked(
                    article_id,
                    "evidence_precision_version_unknown",
                    "文章逐项证据合同版本未知，必须重新审核后发布。",
                )
            precision = evaluate_evidence_precision(
                outgoing_content,
                precision_pack,
                brand_fact_snapshot,
            )
            if precision.hard:
                raise _blocked(
                    article_id,
                    "evidence_precision_failure",
                    "实际文章含未就地绑定来源的专业断言，provider 未调用。",
                )

    if not is_publication_review_gate_enabled():
        try:
            from services.article_review_shadow import record_dispatch_review_shadow

            record_dispatch_review_shadow(
                article_id=article_id,
                dispatch_source=str(source or "legacy_provider_dispatch"),
                outgoing_content=outgoing_content,
            )
        except Exception as exc:
            # Shadow collection is observational only and can never change a
            # legal legacy dispatch result.
            logger.warning("review shadow bypassed article=%s: %s", article_id, exc)
        return ArticleDispatchSnapshot(
            article_id=article_id,
            source=str(source or "legacy_provider_dispatch")[:80],
            title=outgoing_title,
            content=outgoing_content,
            content_hash=_sha256(outgoing_content),
            canonical_content_hash=None,
            evidence_manifest_hash=None,
            publication_profile=None,
            review_reason="publication_review_gate_disabled",
            legal_prohibition_catalog_version=frozen_legal_catalog_version,
        )

    if article_id <= 0:
        raise _blocked(
            article_id,
            "article_identity_required",
            "正文发布必须关联有效文章；短视频只能走独立短视频通道。",
        )

    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, content, evidence_manifest_hash, publication_profile,
                   generation_request_snapshot
              FROM articles
             WHERE id = %s
            """,
            (article_id,),
        )
        row = cur.fetchone()
        if not row:
            raise _blocked(article_id, "article_not_found", "文章不存在，不能提交发布。")

        canonical_title = str(row.get("title") or "")
        canonical_content = str(row.get("content") or "")
        generation_snapshot = row.get("generation_request_snapshot")
        if isinstance(generation_snapshot, str):
            try:
                generation_snapshot = json.loads(generation_snapshot)
            except Exception:
                generation_snapshot = None
        frozen_legal_catalog_version = (
            str(generation_snapshot.get("legal_prohibition_catalog_version") or "").strip()[:64]
            if isinstance(generation_snapshot, dict)
            else ""
        ) or None
        if str(source_title or "") != canonical_title:
            raise _blocked(
                article_id,
                "title_changed_before_dispatch",
                "文章标题已在渠道准备期间变化，必须重新准备并审核。",
            )
        if _sha256(str(source_content or "")) != _sha256(canonical_content):
            raise _blocked(
                article_id,
                "content_changed_before_dispatch",
                "文章正文已在渠道准备期间变化，必须重新准备并审核。",
            )

        eligibility = assert_publication_eligible(article_id, cursor=cur)

        # Review the exact bytes-equivalent text that the provider receives.
        # Canonical human/machine/evidence eligibility is checked above; this
        # final pass catches risks introduced by channel rendering itself.
        from writing.platform_safety_profiles import review_for_platform

        profile = str(row.get("publication_profile") or "standard")
        platform_review = review_for_platform(
            title=outgoing_title,
            content=outgoing_content,
            profile=profile,
        )
        if platform_review.decision == "rewrite_required":
            raise _blocked(
                article_id,
                "outgoing_platform_review_failed",
                "渠道渲染后的实际发送正文未通过平台安全审核，provider 未调用。",
            )

        return ArticleDispatchSnapshot(
            article_id=article_id,
            source=str(source or "article_provider_dispatch")[:80],
            title=outgoing_title,
            content=outgoing_content,
            content_hash=_sha256(outgoing_content),
            canonical_content_hash=_sha256(canonical_content),
            evidence_manifest_hash=row.get("evidence_manifest_hash"),
            publication_profile=profile,
            review_reason=str(eligibility.get("reason") or "eligible"),
            legal_prohibition_catalog_version=frozen_legal_catalog_version,
        )
    finally:
        conn.close()


async def dispatch_article_to_provider(
    *,
    client: Any,
    dispatch_kind: DispatchKind,
    article_id: int,
    source_title: str,
    source_content: str,
    outgoing_title: str,
    outgoing_content: str,
    source: str,
    provider_kwargs: dict[str, Any],
    snapshot_writer: Callable[[ArticleDispatchSnapshot], Any] | None = None,
) -> Any:
    """Review, freeze and immediately send one exact article payload."""
    if "title" in provider_kwargs or "content_md" in provider_kwargs:
        raise ValueError("provider_kwargs must not override reviewed title/content")
    snapshot = prepare_article_dispatch_snapshot(
        article_id=article_id,
        source_title=source_title,
        source_content=source_content,
        outgoing_title=outgoing_title,
        outgoing_content=outgoing_content,
        source=source,
    )
    if snapshot_writer is not None:
        snapshot_writer(snapshot)

    # Keep the provider call adjacent to the completed review.  The immutable
    # snapshot strings below are the only title/body values sent on the wire.
    if dispatch_kind == "publish":
        return await client.publish(
            title=snapshot.title,
            content_md=snapshot.content,
            **provider_kwargs,
        )
    if dispatch_kind == "publish_wemedia":
        return await client.publish_wemedia(
            title=snapshot.title,
            content_md=snapshot.content,
            **provider_kwargs,
        )
    raise ValueError(f"unsupported article dispatch kind: {dispatch_kind}")
