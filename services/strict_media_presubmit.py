"""严审媒体下单前预审（工单 T6 · 把 52% 拒稿消灭在下单前）。

**这里补的是一个真实的洞**：`assert_publication_eligible` 已经会跑平台档，但它判的是
*文章自己存的* ``publication_profile``。而拒稿发生在**媒体维度** —— 用户完全可以把一篇
按 ``standard`` 档写的稿子下单到搜狐。实测（证据 §5）：

* 搜狐系 67 单 → published 9 / rejected 35 = **拒稿率 52.2%**
* 新浪系 18 单 → rejected 7 = 38.9%
* 其他媒体 283 单 → rejected 37 = 13.1%

所以下单前必须**按实际下单的媒体**再判一次严审档，而不是按文章写作时选的档。

设计边界（不越工单红线）：

* 只**拦**，不在下单事务里调 LLM 改写 —— 自动改写要花钱且要走既有 span 级修复入口，
  塞进扣费路径里会踩「不碰计费」红线。拦截返回 machine-readable 契约 + 一键修复动作，
  由前端路由到既有 ``ai_fix_this_span`` 修复流程，修完重下单。
* 拦在**扣费之前**（`_require_strict_media_presubmit` 与既有
  `_require_article_review_for_publish` 并列，都在 deduct 之前），所以拦截不产生退费。
* 通用档媒体（非严审族）**不受影响**，零行为变化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Iterable, Sequence

from writing.platform_safety_profiles import (
    PLATFORM_PROFILE_VERSION,
    STRICT_MEDIA_V2,
    STRICT_MEDIA_FAMILIES,
    resolve_strict_family,
    review_for_platform,
)

logger = logging.getLogger("GEO-StrictMediaPresubmit")

STRICT_PRESUBMIT_VERSION: str = "strict-media-presubmit-v1.0"


class StrictMediaPresubmitBlocked(Exception):
    """Raised before pricing/deduction when a strict-family order cannot pass."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(str(payload.get("message") or "strict_media_presubmit_blocked"))
        self.payload = payload


@dataclass
class StrictMediaPresubmitOutcome:
    checked: int = 0
    blocked_media: list[dict[str, Any]] = field(default_factory=list)
    passed_media: list[dict[str, Any]] = field(default_factory=list)
    version: str = STRICT_PRESUBMIT_VERSION
    profile_version: str = PLATFORM_PROFILE_VERSION

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_media)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "profile_version": self.profile_version,
            "checked": self.checked,
            "blocked": self.blocked,
            "blocked_media": list(self.blocked_media),
            "passed_media": list(self.passed_media),
        }


def is_strict_review_media(media_name: str = "", domain: str = "") -> bool:
    """True only for families we have corpus evidence for.

    The generic ``strict_default`` fallback is deliberately **not** treated as
    strict here: applying an unmeasured profile to every medium would be the
    same over-blocking mistake v1 made.
    """
    if not str(media_name or "").strip() and not str(domain or "").strip():
        return False
    family = resolve_strict_family(domain=domain, media_name=media_name)
    return family.key in {f.key for f in STRICT_MEDIA_FAMILIES}


def evaluate_strict_media_presubmit(
    *,
    title: str,
    content: str,
    media_items: Sequence[dict[str, Any]] | Iterable[dict[str, Any]],
    client_brand: str = "",
) -> StrictMediaPresubmitOutcome:
    """Run the v2 profile against every strict-family medium in this order."""
    outcome = StrictMediaPresubmitOutcome()
    seen: set[str] = set()
    for item in media_items or ():
        if not isinstance(item, dict):
            continue
        media_name = str(item.get("media_name") or "").strip()
        domain = str(item.get("domain") or item.get("entrance_link") or "").strip()
        if not is_strict_review_media(media_name=media_name, domain=domain):
            continue
        key = media_name or domain
        if key in seen:
            continue
        seen.add(key)
        outcome.checked += 1
        verdict = review_for_platform(
            title=title,
            content=content,
            profile=STRICT_MEDIA_V2,
            media_name=media_name,
            domain=domain,
            client_brand=client_brand,
        )
        record = {
            "media_id": item.get("media_id"),
            "media_name": media_name,
            "family_key": verdict.family_key,
            "family_label": verdict.family_label,
            "observed_reject_rate": (verdict.corpus_evidence or {}).get("observed_reject_rate"),
            "hard_failures": [dict(f) for f in verdict.hard_failures],
            "warnings": [dict(w) for w in verdict.warnings],
        }
        if verdict.decision == "rewrite_required":
            outcome.blocked_media.append(record)
        else:
            outcome.passed_media.append(record)
    return outcome


def _blocked_payload(outcome: StrictMediaPresubmitOutcome, article_id: int) -> dict[str, Any]:
    codes: list[str] = []
    for media in outcome.blocked_media:
        for failure in media.get("hard_failures") or ():
            code = str(failure.get("code") or "")
            if code and code not in codes:
                codes.append(code)
    names = "、".join(str(m.get("media_name") or m.get("family_label")) for m in outcome.blocked_media[:3])
    return {
        "eligible": False,
        "reason": "strict_media_presubmit_failed",
        "reason_class": "platform_profile_hard",
        "overridable": False,
        "article_id": article_id,
        "message": (
            f"{names} 属严审媒体，本稿未通过发布前预审，已在扣费前拦下（未产生费用）。"
        ),
        "repair_hint": "按提示修正后重新下单；这些媒体历史拒稿率高，先修比先发划算。",
        "presubmit": outcome.as_dict(),
        "hard_failure_codes": codes,
        "actions": [
            {"id": "ai_fix_this_span", "label": "AI 修复违规表述", "type": "retry"},
            {"id": "view_findings", "label": "查看定位", "type": "nav"},
            {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
        ],
        "rule_version": STRICT_PRESUBMIT_VERSION,
    }


def assert_strict_media_presubmit(
    *,
    article_id: int,
    title: str,
    content: str,
    media_items: Sequence[dict[str, Any]],
    client_brand: str = "",
    actor_user_id: int = 0,
) -> StrictMediaPresubmitOutcome:
    """Block strict-family orders that would predictably be rejected.

    Raises :class:`StrictMediaPresubmitBlocked` **before** any pricing or
    deduction happens.  Recording the interception is best-effort: an audit
    write must never be the reason a legitimate order fails.
    """
    outcome = evaluate_strict_media_presubmit(
        title=title, content=content, media_items=media_items, client_brand=client_brand
    )
    if not outcome.checked:
        return outcome
    try:
        record_presubmit_event(
            article_id=article_id, actor_user_id=actor_user_id, outcome=outcome
        )
    except Exception as exc:  # pragma: no cover - audit must never block
        logger.warning("[strict-presubmit] 审计写入失败 article=%s: %s", article_id, exc)
    if outcome.blocked:
        raise StrictMediaPresubmitBlocked(_blocked_payload(outcome, article_id))
    return outcome


def record_presubmit_event(
    *, article_id: int, actor_user_id: int, outcome: StrictMediaPresubmitOutcome
) -> None:
    """Persist one interception/pass so T6 拦截率 is queryable.

    Reuses ``geo_article_review_events``.  ``decision`` keeps its frozen domain
    {approved, rejected, skipped} — the reason-code CHECK is an anti-drift guard
    (schema contract ``ck_geo_article_review_decision``) and must not be widened
    for a reporting need.  The strict-media dimension rides on
    ``machine_review_status`` instead, which is unconstrained.
    """
    if not article_id or article_id <= 0 or not outcome.checked:
        return
    from db.connection import get_connection

    status = (
        "strict_media_presubmit_blocked" if outcome.blocked else "strict_media_presubmit_passed"
    )
    families = ",".join(
        sorted({str(m.get("family_key") or "") for m in (outcome.blocked_media + outcome.passed_media)})
    )
    codes = sorted({
        str(f.get("code") or "")
        for m in outcome.blocked_media
        for f in (m.get("hard_failures") or ())
    })
    reason = f"[{STRICT_PRESUBMIT_VERSION}] families={families} checked={outcome.checked}"
    if codes:
        reason += " codes=" + ",".join(codes)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_article_review_events (
                article_id, actor_user_id, decision, reason,
                machine_review_status, machine_review_version
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                int(article_id),
                int(actor_user_id or 0),
                "rejected" if outcome.blocked else "skipped",
                reason,
                status,
                PLATFORM_PROFILE_VERSION,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def presubmit_interception_stats(window_days: int = 180) -> dict[str, Any]:
    """T6 报表口径：预审拦截率（与拒稿率并排看才有意义）。"""
    from db.connection import get_connection

    window = max(1, min(720, int(window_days or 180)))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT machine_review_status AS status, COUNT(*) AS n
              FROM geo_article_review_events
             WHERE created_at >= NOW() - make_interval(days => %s)
               AND machine_review_status LIKE 'strict_media_presubmit_%%'
             GROUP BY 1
            """,
            (window,),
        )
        rows = {str(r["status"]): int(r["n"]) for r in cur.fetchall()}
    finally:
        conn.close()
    blocked = rows.get("strict_media_presubmit_blocked", 0)
    passed = rows.get("strict_media_presubmit_passed", 0)
    total = blocked + passed
    return {
        "window_days": window,
        "presubmit_checked": total,
        "presubmit_blocked": blocked,
        "presubmit_passed": passed,
        "interception_rate": round(blocked / total, 4) if total else None,
        "rule_version": STRICT_PRESUBMIT_VERSION,
    }
