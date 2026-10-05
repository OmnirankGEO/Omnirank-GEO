"""GEO acquisition content-package orchestration on the existing material chain."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from typing import Optional

from db import marketing_db
from services.marketing import image_client, material_storage
from services.marketing.content_center import (
    CHANNEL_CONTRACTS,
    channel_image_slots,
    claim_evidence_qa,
    diagnosis_case_evidence_ok,
    error_entry,
    generate_channel_content,
    provider_safe_evidence,
    scan_geo_claims,
    visual_prompt,
    warning_entry,
)
from services.marketing.evidence import live_brand_snapshot, require_frozen_evidence_live
from services.marketing.quality_assurance import visual_qa


logger = logging.getLogger("GEO-Content-Package-Factory")


def canonical_hash(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _display_status(job: dict, assets: list[dict]) -> str:
    status = str(job.get("status") or "pending")
    if status == "succeeded" and str(job.get("error_summary") or "").startswith("partial"):
        return "partial_success"
    return status


def _public_asset(asset: dict) -> dict:
    """Expose only durable delivery fields; provider URLs and storage internals stay server-side."""
    allowed = {
        "id", "asset_kind", "bundle_slot", "slot",
        "content_text", "width", "height", "mime_type", "rights_confirmed", "created_at",
    }
    result = {key: value for key, value in asset.items() if key in allowed}
    if asset.get("id") and (asset.get("url_stored") or asset.get("thumbnail_url")):
        result["download_url"] = f"/api/marketing/materials/{int(asset['id'])}/download"
    return result


def _public_error_summary(value: object) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if raw.startswith("freeze_failed"):
        return "billing_reservation_failed"
    if raw.startswith("preflight_revoked"):
        return "source_access_revoked"
    allowed = {
        "partial_free_release", "billing_commit_pending", "billing_release_pending",
        "billing_reservation_missing", "all_components_failed", "forbidden_content",
        "provider_submit_unknown", "provider_poll_pending",
        "provider_resolution_pending",
    }
    return raw if raw in allowed else "generation_failed"


def _public_deal(deal: Optional[dict]) -> Optional[dict]:
    """Expose deal lineage and component-level human reasons; never raw inputs."""
    if not deal:
        return None
    return {
        "draft_id": deal.get("draft_id"),
        "version": deal.get("version"),
        "redacted_material_count": len(deal.get("redacted_materials") or []),
        "skipped_components": [
            {"component_id": str(item.get("component_id") or ""), "reason": str(item.get("reason") or "")}
            for item in (deal.get("skipped_components") or [])
            if isinstance(item, dict)
        ],
    }


def _public_warnings(geo: dict) -> list[dict]:
    """合规提醒(Owner 2026-07-22 warn-not-block;SSOT 2026-07-23 五问合同)透出:
    每条 {code, message, reason, repair_hint, actions, rule_version, channel, component_id}。"""
    return [
        {
            "code": str(item.get("code") or ""),
            "message": str(item.get("message") or ""),
            "reason": str(item.get("reason") or ""),
            "repair_hint": str(item.get("repair_hint") or ""),
            "actions": [dict(action) for action in (item.get("actions") or []) if isinstance(action, dict)],
            "rule_version": str(item.get("rule_version") or ""),
            "channel": str(item.get("channel") or ""),
            "component_id": str(item.get("component_id") or ""),
        }
        for item in (geo.get("warnings") or [])
        if isinstance(item, dict) and item.get("code")
    ]


def _record_job_warnings(job: dict, geo: dict, entries: list[dict]) -> None:
    """把 QA 提醒幂等追加进冻结快照(warnings 数组),重放/恢复不重复落库。"""
    if not entries:
        return
    existing = geo.setdefault("warnings", [])
    seen = {
        (str(item.get("code") or ""), str(item.get("component_id") or ""))
        for item in existing
    }
    added = False
    for entry in entries:
        key = (str(entry.get("code") or ""), str(entry.get("component_id") or ""))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        existing.append({
            "code": key[0],
            "message": str(entry.get("message") or ""),
            "reason": str(entry.get("reason") or ""),
            "repair_hint": str(entry.get("repair_hint") or ""),
            "actions": [dict(action) for action in (entry.get("actions") or []) if isinstance(action, dict)],
            "rule_version": str(entry.get("rule_version") or ""),
            "channel": str(entry.get("channel") or ""),
            "component_id": key[1],
        })
        added = True
    if added:
        marketing_db.patch_job_input_fields(int(job["id"]), {"_geo": geo})


def effective_assets(job: dict, *, max_depth: int = 20,
                     deliverable_only: bool = False) -> list[dict]:
    """Merge immutable revision lineage, with the newest component winning."""
    lineage: list[dict] = []
    current = job
    seen: set[int] = set()
    for _ in range(max_depth):
        job_id = int(current["id"])
        if job_id in seen:
            raise ValueError("material_revision_cycle")
        seen.add(job_id)
        lineage.append(current)
        geo = (current.get("input_fields_jsonb") or {}).get("_geo") or {}
        parent_id = geo.get("parent_job_id")
        if not parent_id:
            break
        parent = marketing_db.get_job(int(parent_id))
        if not parent or int(parent["user_id"]) != int(job["user_id"]):
            raise ValueError("material_revision_parent_invalid")
        current = parent
    else:
        raise ValueError("material_revision_too_deep")
    merged: dict[str, dict] = {}
    for revision in reversed(lineage):
        if deliverable_only and not (
            str(revision.get("status") or "") == "succeeded"
            and str(revision.get("error_summary") or "") in {"", "partial_free_release"}
        ):
            continue
        for asset in marketing_db.list_assets(int(revision["id"])):
            merged[str(asset.get("bundle_slot") or asset.get("id"))] = asset
    return list(merged.values())


def _full_component_plan(geo: dict) -> list[dict]:
    return _component_plan({**geo, "only_components": []})


def job_public_state(job: dict, assets: Optional[list[dict]] = None) -> dict:
    supplied_assets = assets is not None
    assets = list(assets if supplied_assets else effective_assets(job))
    terminal = str(job.get("status") or "") in {"succeeded", "failed", "blocked"}
    origin_jobs: dict[int, dict] = {int(job["id"]): job}

    def origin_is_deliverable(asset: dict) -> bool:
        # Effective revisions merge settled parent output with a new child.
        # Preserve those parent deliveries while withholding every asset whose
        # own origin job has not converged to an allowed settlement terminal.
        origin_id = int(asset.get("job_id") or job["id"])
        origin = origin_jobs.get(origin_id)
        if origin is None:
            origin = marketing_db.get_job(origin_id) or {}
            origin_jobs[origin_id] = origin
        return (
            str(origin.get("status") or "") == "succeeded"
            and str(origin.get("error_summary") or "") in {"", "partial_free_release"}
        )

    # Component rows may exist while a paid package is still generating or its
    # settlement response is unknown. They are durable progress evidence, not
    # yet delivery authority.
    delivery_assets = (
        [asset for asset in assets if origin_is_deliverable(asset)]
        if supplied_assets else effective_assets(job, deliverable_only=True)
    )
    geo = (job.get("input_fields_jsonb") or {}).get("_geo") or {}
    frozen_contact = geo.get("contact") or {}
    public_contact = {
        "mode": str(frozen_contact.get("mode") or "none"),
        "text": str(frozen_contact.get("text") or "") if frozen_contact.get("mode") == "text" else "",
        "qr_validated": bool(frozen_contact.get("mode") == "qr" and frozen_contact.get("qr_reference")),
    }
    planned = _full_component_plan(geo)
    done = {str(asset.get("bundle_slot") or "") for asset in assets}
    components = [
        {
            **component,
            "status": (
                "succeeded" if component["component_id"] in done
                else ("failed" if terminal else "pending")
            ),
        }
        for component in planned
    ]
    return {
        "job_id": int(job["id"]),
        "status": status if (status := _display_status(job, assets)) else "pending",
        "error_summary": _public_error_summary(job.get("error_summary")),
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
        "teacher": geo.get("teacher"),
        "strategy": geo.get("strategy"),
        "evidence": provider_safe_evidence(geo.get("evidence") or {}),
        "trend": geo.get("trend"),
        "contact": public_contact,
        "channels": geo.get("channels") or [],
        "deal": _public_deal(geo.get("deal")),
        "warnings": _public_warnings(geo),
        "components": components,
        "assets": [_public_asset(asset) for asset in delivery_assets],
        "is_revision": bool(geo.get("parent_job_id")),
        "revision_no": int(geo.get("revision_no") or (2 if geo.get("parent_job_id") else 1)),
    }


def _component_plan(geo: dict) -> list[dict]:
    only = set(geo.get("only_components") or [])
    moments_layout = str(geo.get("moments_layout") or "single")
    result: list[dict] = []
    for channel in geo.get("channels") or []:
        if channel not in CHANNEL_CONTRACTS:
            continue
        copy_id = f"{channel}:copy"
        if not only or copy_id in only:
            result.append({"component_id": copy_id, "channel": channel, "kind": "copy"})
        for slot in channel_image_slots(channel, moments_layout=moments_layout):
            image_id = f"{channel}:image:{slot['slot']}"
            if not only or image_id in only:
                result.append({
                    "component_id": image_id,
                    "channel": channel,
                    "kind": "image",
                    "slot": dict(slot),
                })
    return result


def valid_component_ids(channels: list[str], *, moments_layout: str = "single") -> set[str]:
    return {
        item["component_id"]
        for item in _component_plan({
            "channels": list(channels), "only_components": [], "moments_layout": moments_layout,
        })
    }


def _feature_code(geo: dict, resolution: str) -> str:
    components = _component_plan(geo)
    if components and all(item["kind"] == "copy" for item in components):
        return "mktg_moments_copy"
    if len(components) == 1 and components[0]["kind"] == "image":
        return "mktg_poster_pro" if resolution in {"2k", "4k"} else "mktg_poster_basic"
    return "mktg_bundle_pro" if resolution in {"2k", "4k"} else "mktg_bundle_std"


def _guard_snapshot(geo: dict) -> dict:
    text = json.dumps(
        {
            "brief": geo.get("brief"),
            "strategy": geo.get("strategy"),
            "evidence": geo.get("evidence"),
            "contact_text": (geo.get("contact") or {}).get("text"),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return scan_geo_claims(text)


def _row_status(job: dict, *, replayed: bool = True) -> dict:
    return {
        "status": _display_status(job, marketing_db.list_assets(int(job["id"]))),
        "job_id": int(job["id"]),
        "replayed": replayed,
    }


def _qa_error_entries(errors: list) -> list[dict]:
    """QA 硬失败持久化前规范成五问合同(SSOT §6,2026-07-23 Deploy 回归):
    code + message + reason/repair_hint + actions + rule_version;
    条目自带的其余上下文字段原样透传,不覆盖规范化后的合同字段。"""
    entries: list[dict] = []
    for item in errors or []:
        if isinstance(item, dict):
            code = str(item.get("code") or "")
            extras = {
                key: value for key, value in item.items()
                if key not in {"code", "message", "reason", "repair_hint", "actions", "rule_version"}
            }
            entries.append({**error_entry(code), **extras} if code else dict(item))
        else:
            entries.append(error_entry(str(item)))
    return entries


def _reconcile_terminal_job(job: dict) -> dict:
    assets = effective_assets(job)
    geo = (job.get("input_fields_jsonb") or {}).get("_geo") or {}
    expected = len(_full_component_plan(geo))
    full = expected > 0 and len({str(a.get("bundle_slot") or "") for a in assets}) >= expected
    if full:
        marketing_db.update_job(int(job["id"]), status="succeeded", error_summary="")
    elif assets:
        marketing_db.update_job(int(job["id"]), status="succeeded", error_summary="partial_free_release")
    else:
        marketing_db.update_job(int(job["id"]), status="failed", error_summary="all_components_failed")
    return _row_status(marketing_db.get_job(int(job["id"])) or job)


def _require_actor_brand_live(job: dict) -> None:
    """Recheck legacy assignment immediately before any provider call."""
    brand_id = job.get("brand_id")
    if not brand_id:
        return
    brand = live_brand_snapshot(int(brand_id))
    user_id = int(job["user_id"])
    if int(brand["owner_user_id"]) == user_id:
        return
    try:
        from db.auth_db import get_user

        if (get_user(user_id) or {}).get("is_admin"):
            return
    except Exception:
        pass
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM user_clients WHERE user_id=%s AND brand_id=%s LIMIT 1",
            (user_id, int(brand_id)),
        )
        if not cur.fetchone():
            raise ValueError("brand_assignment_revoked")
    finally:
        conn.close()


#: 🔴🔴 [#113] 授权类失败的**具体归类**。默认档不许取具体档。
#:
#: 原来这里无差别贴 `live_authority_revoked_after_provider`,而它的对客文案是
#: 「品牌授权已撤销,成图未交付」。真实原因常常是**租约被轮询抢走**
#: (ORG_WORK_LEASE_LOST) —— 她的授权好好的,系统却告诉她授权没了。
#: **一句具体但错误的话比一句笼统的话更坏**:笼统让人继续找,错的具体让人停止找。
_LEASE_LOST_CODES = frozenset({"ORG_WORK_LEASE_LOST", "ORG_CHARGE_LEASE_LOST"})
_AUTHORITY_REVOKED_CODES = frozenset({"ORG_CHARGE_AUTHORITY_REVOKED",
                                      "ORG_LIVE_AUTHORITY_REVOKED"})


def _authority_failure_code(exc) -> str:
    """把底层 code 映射成落库/对客的具体档;**认不出就走笼统档**。"""
    code = getattr(exc, "code", None) or getattr(getattr(exc, "__cause__", None), "code", None)
    if code in _LEASE_LOST_CODES:
        return "worker_lease_lost_after_provider"
    if code in _AUTHORITY_REVOKED_CODES:
        return "live_authority_revoked_after_provider"
    return "live_authority_check_failed_after_provider"


async def _prepare_legacy(
    *,
    user_id: int,
    brand_id: Optional[int],
    geo: dict,
    request_hash: str,
    feature_code: str,
    resolution: str,
) -> dict:
    """Atomically create a job and physical freeze under one advisory lock."""
    from db.connection import get_connection
    from middleware.billing import freeze_points

    request_id = str(geo["request_id"])
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"marketing-material:{int(user_id)}:{request_id}",),
        )
        cur.execute(
            """
            SELECT * FROM marketing_material_jobs
            WHERE user_id=%s AND input_fields_jsonb #>> '{_geo,request_id}'=%s
            ORDER BY id DESC LIMIT 1
            """,
            (int(user_id), request_id),
        )
        existing = cur.fetchone()
        if existing:
            job = dict(existing)
            old_geo = (job.get("input_fields_jsonb") or {}).get("_geo") or {}
            if str(old_geo.get("request_hash") or "") != request_hash:
                raise ValueError("material_request_id_conflict")
            if str(job.get("status") or "") == "generating" and job.get("freeze_id"):
                conn.commit()
                return {
                    "status": _display_status(job, marketing_db.list_assets(int(job["id"]))),
                    "job_id": int(job["id"]),
                    "replayed": True,
                    "_ctx": {
                        "job_id": int(job["id"]),
                        "billing_kind": "legacy",
                        "freeze_id": job.get("freeze_id"),
                        "payer_user_id": int(user_id),
                        **({"settlement_only": True} if str(job.get("error_summary") or "") == "provider_resolution_pending" else {}),
                    },
                }
            if str(job.get("status") or "") not in {"succeeded", "failed", "blocked"}:
                cur.execute(
                    """
                    UPDATE marketing_material_jobs
                    SET status='failed',error_summary='billing_reservation_missing',finished_at=NOW()
                    WHERE id=%s RETURNING *
                    """,
                    (int(job["id"]),),
                )
                job = dict(cur.fetchone())
            conn.commit()
            return _row_status(job)

        cur.execute(
            """
            SELECT COUNT(*)::int AS c FROM marketing_material_jobs
            WHERE user_id=%s AND created_at::date=(NOW() AT TIME ZONE 'Asia/Shanghai')::date AND status<>'blocked'
            """,
            (int(user_id),),
        )
        daily = marketing_db.get_config_int("marketing.material.daily_limit", 20)
        if int(cur.fetchone()["c"]) >= daily:
            conn.rollback()
            return {"status": "blocked", "block_reason": "daily_limit", "limit": daily}

        task_ref = "geo_" + hashlib.sha256(f"{user_id}:{request_id}".encode()).hexdigest()[:32]
        cur.execute(
            """
            INSERT INTO marketing_material_jobs(
              owner_scope,user_id,brand_id,material_kind,feature_code,input_fields_jsonb,
              final_prompt,size,resolution,status,cost_points,billing_ref
            ) VALUES ('user',%s,%s,'bundle',%s,%s::jsonb,'','3:4',%s,'pending',0,'')
            RETURNING *
            """,
            (int(user_id), brand_id, feature_code, json.dumps({"_geo": geo}, ensure_ascii=False, default=str), resolution),
        )
        job = dict(cur.fetchone())
        freeze = await freeze_points(
            user_id=int(user_id),
            feature_code=feature_code,
            task_ref=task_ref,
            brand_id=brand_id,
            reason="GEO 获客内容包",
            _cursor=cur,
        )
        cur.execute(
            """
            UPDATE marketing_material_jobs
            SET status='generating',freeze_id=%s,cost_points=%s,billing_ref=%s
            WHERE id=%s RETURNING *
            """,
            (freeze.get("freeze_id"), int(freeze.get("amount") or 0), task_ref, int(job["id"])),
        )
        job = dict(cur.fetchone())
        conn.commit()
        return {
            "status": "generating",
            "job_id": int(job["id"]),
            "replayed": False,
            "_ctx": {
                "job_id": int(job["id"]),
                "billing_kind": "legacy",
                "freeze_id": freeze.get("freeze_id"),
                "payer_user_id": int(user_id),
            },
        }
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("legacy GEO package prepare failed: %s", exc)
        try:
            job, _ = marketing_db.create_or_get_material_job(
                user_id=int(user_id), request_id=request_id, request_hash=request_hash,
                brand_id=brand_id, material_kind="bundle", feature_code=feature_code,
                input_fields={"_geo": geo}, resolution=resolution,
            )
            marketing_db.update_job(int(job["id"]), status="failed", error_summary=f"freeze_failed:{str(exc)[:120]}")
            return {"status": "failed", "job_id": int(job["id"]), "error": "billing_reservation_failed"}
        except ValueError:
            raise
    finally:
        conn.close()


async def _prepare_organization(
    *,
    identity,
    user_id: int,
    brand_id: Optional[int],
    geo: dict,
    request_hash: str,
    feature_code: str,
    resolution: str,
) -> dict:
    from services.organization_billing import (
        claim_live_charge,
        claim_poll_only_charge,
        get_charge_reconciliation_state,
        reserve_charge,
    )

    job, created = marketing_db.create_or_get_material_job(
        user_id=int(user_id), request_id=str(geo["request_id"]), request_hash=request_hash,
        brand_id=brand_id, material_kind="bundle", feature_code=feature_code,
        input_fields={"_geo": geo}, resolution=resolution,
    )
    if not created:
        if job["status"] in {"succeeded", "failed", "blocked"}:
            return _row_status(job)
        if str(job.get("billing_ref") or "").startswith("org:"):
            charge_id = int(str(job["billing_ref"]).split(":", 1)[1])
            if str(job.get("error_summary") or "") in {
                "billing_commit_pending", "billing_release_pending", "provider_resolution_pending",
            }:
                charge = get_charge_reconciliation_state(identity, charge_link_id=charge_id)
                if str(charge.get("status") or "") in {"committed", "released", "refunded"}:
                    return _reconcile_terminal_job(job)
                return {
                    "status": "generating", "job_id": int(job["id"]), "replayed": True,
                    "_ctx": {
                        "job_id": int(job["id"]), "billing_kind": "organization",
                        "organization_charge_id": charge_id, "claim_token": "",
                        "actual_points": int(charge.get("estimated_points") or job.get("cost_points") or 0),
                        "settlement_only": True,
                        "recovery_identity": "geo_factory.prepare_geo_package_job:replay_pending_settlement",
                    },
                }
            recoverable = next((
                attempt for attempt in reversed(marketing_db.list_attempts(int(job["id"])))
                if str(attempt.get("provider_task_id") or "")
                and str(attempt.get("submit_state") or "") == "submitted"
                and str(attempt.get("poll_state") or "") in {"polling", "pending", "succeeded"}
                and str(attempt.get("status") or "") in {"pending", "running", "succeeded"}
            ), None)
            if recoverable is not None:
                claim = claim_poll_only_charge(
                    identity,
                    charge_link_id=charge_id,
                    attempt_id=int(recoverable["id"]),
                    provider_task_id=str(recoverable["provider_task_id"]),
                )
            elif str(job.get("error_summary") or "") == "provider_submit_unknown":
                # No task id means the POST outcome is unknown. Only the
                # explicit reconciliation endpoint may move that same attempt.
                return _row_status(job)
            else:
                claim = claim_live_charge(identity, charge_link_id=charge_id)
            if str(claim.get("status") or "") in {"committed", "released", "refunded"}:
                return _reconcile_terminal_job(job)
            if claim.get("in_progress"):
                return _row_status(job)
            return {
                "status": "generating", "job_id": int(job["id"]), "replayed": True,
                "_ctx": {
                    "job_id": int(job["id"]), "billing_kind": "organization",
                    "organization_charge_id": charge_id, "claim_token": claim["claim_token"],
                    "actual_points": int(claim.get("estimated_points") or 0),
                    "poll_only_attempt_id": int(recoverable["id"]) if recoverable else None,
                    "poll_only_task_id": str(recoverable["provider_task_id"]) if recoverable else "",
                },
            }

    daily = marketing_db.get_config_int("marketing.material.daily_limit", 20)
    # The newly-created row is already included in the count.  Replays bypass
    # this admission check so a committed job can always recover after a lost
    # response or worker restart.
    if marketing_db.count_jobs_today(int(user_id)) > daily:
        marketing_db.update_job(int(job["id"]), status="blocked", block_reason="daily_limit")
        return {"status": "blocked", "job_id": int(job["id"]), "block_reason": "daily_limit", "limit": daily}

    execution_id = f"geo-content:{int(identity.organization_id)}:{geo['request_id']}"
    charge = await reserve_charge(
        identity,
        execution_id=execution_id,
        feature_code=feature_code,
        work_kind="geo_content_package",
        payload={"job_id": int(job["id"]), "geo_snapshot": geo},
        brand_id=brand_id,
        task_ref=f"mktg_factory_{int(job['id'])}",
    )
    charge_id = int(charge["id"])
    marketing_db.update_job(
        int(job["id"]), status="generating", billing_ref=f"org:{charge_id}",
        cost_points=int(charge.get("estimated_points") or 0),
    )
    claim = claim_live_charge(identity, charge_link_id=charge_id)
    if claim.get("in_progress"):
        return {"status": "generating", "job_id": int(job["id"]), "replayed": True}
    return {
        "status": "generating", "job_id": int(job["id"]), "replayed": bool(charge.get("replayed")),
        "_ctx": {
            "job_id": int(job["id"]), "billing_kind": "organization",
            "organization_charge_id": charge_id, "claim_token": claim["claim_token"],
            "actual_points": int(charge.get("estimated_points") or 0),
        },
    }


async def prepare_geo_package_job(
    *,
    user_id: int,
    brand_id: Optional[int],
    geo_snapshot: dict,
    request_hash: str,
    resolution: str = "1k",
    organization_identity=None,
) -> dict:
    """Preflight, idempotency and billing reservation for one frozen package."""
    frozen_actor = geo_snapshot.get("actor_snapshot") or {}
    frozen_org_id = frozen_actor.get("organization_id")
    if frozen_org_id is not None:
        if organization_identity is None or int(organization_identity.organization_id) != int(frozen_org_id):
            raise ValueError("organization_identity_required_for_frozen_job")
        # 2026-07-23 Deploy 阻断 3:执行/恢复路径与 API 读取同口径比对冻结
        # actor 快照——撤权后重入会(新 membership 行)/成员暂停/组织与分配
        # 代际变化都 fail-closed,不得带着旧代际身份继续计费与出图。
        if str(organization_identity.membership_status or "") != "active":
            raise ValueError("organization_actor_generation_mismatch")
        organization_identity.require_active_organization()
        if frozen_actor.get("actor_kind") and str(organization_identity.actor_kind) != str(frozen_actor["actor_kind"]):
            raise ValueError("organization_actor_generation_mismatch")
        if frozen_actor.get("authority_version") is not None and str(organization_identity.authority_version) != str(frozen_actor["authority_version"]):
            raise ValueError("organization_actor_generation_mismatch")
        if frozen_actor.get("organization_authority_version") is not None and int(organization_identity.organization_authority_version or 0) != int(frozen_actor["organization_authority_version"]):
            raise ValueError("organization_actor_generation_mismatch")
        if frozen_actor.get("membership_id") is not None and int(organization_identity.membership_id or 0) != int(frozen_actor["membership_id"]):
            raise ValueError("organization_actor_generation_mismatch")
        if frozen_actor.get("membership_version") is not None and int(organization_identity.membership_version or 0) != int(frozen_actor["membership_version"]):
            raise ValueError("organization_actor_generation_mismatch")
        if frozen_actor.get("assignment_version") is not None and int(organization_identity.assignment_version or 0) != int(frozen_actor["assignment_version"]):
            raise ValueError("organization_actor_generation_mismatch")
    guard = _guard_snapshot(geo_snapshot)
    feature_code = _feature_code(geo_snapshot, resolution)
    # SSOT 2026-07-23:整包 blocked 只留法律禁止目录包命中项(广告法极限词/违法内容);
    # 承诺词/灰词等其他守卫命中一律放行,由 QA warnings 提醒。
    from services.marketing import guards as _guards

    if not guard["passed"] and _guards.hard_flag_hits(guard.get("flags")):
        job, _ = marketing_db.create_or_get_material_job(
            user_id=int(user_id), request_id=str(geo_snapshot["request_id"]), request_hash=request_hash,
            brand_id=brand_id, material_kind="bundle", feature_code=feature_code,
            input_fields={"_geo": geo_snapshot}, resolution=resolution,
        )
        marketing_db.update_job(int(job["id"]), status="blocked", block_reason="forbidden_content")
        return {"status": "blocked", "job_id": int(job["id"]), "flags": guard.get("flags") or []}
    if frozen_org_id is not None or organization_identity is not None:
        return await _prepare_organization(
            identity=organization_identity, user_id=user_id, brand_id=brand_id,
            geo=geo_snapshot, request_hash=request_hash, feature_code=feature_code,
            resolution=resolution,
        )
    return await _prepare_legacy(
        user_id=user_id, brand_id=brand_id, geo=geo_snapshot,
        request_hash=request_hash, feature_code=feature_code, resolution=resolution,
    )


def _exact_image_copy(channel: str, content: dict) -> dict:
    result = {}
    for key in ("title", "cta", "source_note"):
        if isinstance(content.get(key), str) and content[key].strip():
            result[key] = content[key].strip()
    if channel in {"private_chat", "deal_chat"}:
        if not result.get("title"):
            result["title"] = "一次低门槛 GEO 诊断" if channel == "private_chat" else "一次真实成交记录"
        # 聊天界面示意图必须显著标注"示例对话"；visual_qa 用 OCR 强制核验。
        result["scene_label"] = "示例对话"
    body = content.get("body")
    if isinstance(body, str) and body.strip():
        result["body"] = body.strip()[:80]
    return result


def _persisted_channel_copy(assets: list[dict], channel: str) -> Optional[dict]:
    """已持久化 copy 资产的渠道文案(恢复/重放路径专用,2026-07-23 外部审查 P1-3)。

    kill-9/轮询重放/局部重试恢复时,copy 资产已是耐久事实:图片组件的
    exact_copy 必须读这份持久化文案,绝不为同渠道重跑文案 LLM(否则恢复
    一次就多一次 LLM 调用,且图上的字可能和已交付文案不一致)。
    """
    slot = f"{channel}:copy"
    for asset in reversed(list(assets or [])):
        if str(asset.get("bundle_slot") or "") != slot:
            continue
        try:
            content = json.loads(str(asset.get("content_text") or ""))
        except (TypeError, ValueError):
            return None
        return content if isinstance(content, dict) and content else None
    return None


def _reused_channel_copy_qa(content: dict, geo: dict, channel: str) -> dict:
    """重放已持久化文案时的 QA:纯函数重算(零 LLM),口径同生成时 execute 循环。"""
    qa = claim_evidence_qa(
        content, evidence=geo.get("evidence") or {}, contact=geo.get("contact") or {}, channel=channel,
    )
    if channel == "diagnosis_case" and not diagnosis_case_evidence_ok(geo.get("evidence") or {}):
        # 与 generate_channel_content 的常青口径提醒保持同一份(恢复不丢警告)。
        qa = {
            **qa,
            "warnings": (qa.get("warnings") or [])
            + [warning_entry("diagnosis_case_requires_frozen_evidence")],
        }
    return qa


_DEAL_CHAT_REDACT_REASON = (
    "聊天晒单需要至少一张已完成打码并确认的聊天素材："
    "请先在晒成交草稿里对素材完成打码预览并确认，再重新生成。"
)


def deal_component_skip_reason(geo: dict, component: dict) -> Optional[str]:
    """deal_chat 图片组件必须有已确认打码素材;缺失时该组件失败但不阻断整包。"""
    deal = geo.get("deal") or {}
    if not deal:
        return None
    if str(component.get("kind") or "") != "image":
        return None
    if str(component.get("channel") or "") != "deal_chat":
        return None
    if deal.get("redacted_materials"):
        return None
    return _DEAL_CHAT_REDACT_REASON


def _brand_logo_pad(logo_url: str, *, anonymize: bool) -> str:
    """品牌 logo 垫图解析(照 QR 垫图链路):公网 https URL 直接透传;本地白标
    logo(``/uploads/whitelabel-logos/...``)读文件转 data URI。匿名晒单一律
    不垫。logo 读取失败静默降级为空(成品退化为纯文字品牌名),不阻断生成。
    """
    if anonymize:
        return ""
    value = str(logo_url or "").strip()
    if not value:
        return ""
    if value.startswith("https://"):
        return value[:500]
    try:
        return material_storage.whitelabel_logo_data_uri(value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("brand logo pad degraded to text-only: %s", exc)
        return ""


async def _generate_image_component(
    *,
    job: dict,
    component: dict,
    geo: dict,
    content: dict,
    owner_key: str,
    provider_guard=None,
    poll_guard=None,
) -> Optional[dict]:
    channel = component["channel"]
    slot = component["slot"]
    exact_copy = _exact_image_copy(channel, content)
    anonymize = bool(geo.get("anonymize_brand"))
    # 匿名晒单:provider 侧品牌名/logo 置空(visual_qa 的 brand_name_missing
    # 因 name 为空自然跳过),冻结快照里的 brand 原值保留供审计。
    provider_brand = (
        {**(geo.get("brand") or {}), "name": "", "product_name": "", "logo_url": "", "slogan": ""}
        if anonymize else (geo.get("brand") or {})
    )
    # logo 垫图先于 prompt 装配:只有真正垫进 image_urls 时,prompt 才要求
    # 忠实保留 logo(读取失败静默降级为纯文字品牌名,不再要求保留)。
    safe_logo = _brand_logo_pad(provider_brand.get("logo_url"), anonymize=anonymize)
    prompt = visual_prompt(
        slot=slot, channel=channel, strategy=geo["strategy"], content=content,
        evidence=geo["evidence"], trend=geo["trend"], contact=geo["contact"],
        brand=provider_brand,
        visual_style=geo.get("visual_style"),
        anonymize_brand=anonymize,
        angle=(geo.get("angles") or {}).get(channel),
        logo_attached=bool(safe_logo),
        facts_pack=geo.get("product_facts"),
    )
    contact = geo.get("contact") or {}
    qr = (contact.get("qr_reference") or {}) if contact.get("mode") == "qr" else {}
    qr_input = None
    if qr:
        # 读取校验(2026-07-23 外部审查 P1-1 同口径):冻结 QR 凭证的组织上下文
        # 必须与 job 冻结 actor 快照一致——撤权/跨组织重放 fail-closed,不出图。
        frozen_actor_org = ((geo.get("actor_snapshot") or {}).get("organization_id"))
        qr_org = qr.get("organization_id")
        qr_org = int(qr_org) if qr_org is not None else None
        frozen_actor_org = int(frozen_actor_org) if frozen_actor_org is not None else None
        if qr_org != frozen_actor_org:
            raise ValueError("qr_reference_org_mismatch")
        qr_input, qr_bytes = material_storage.qr_reference_data_uri(
            owner_key, str(qr.get("reference_id") or ""), str(qr.get("file_sha256") or ""),
        )
        from services.marketing.quality_assurance import validate_qr_reference
        local_qr = validate_qr_reference(qr_bytes)
        if str(local_qr["payload_hash"]) != str(qr.get("payload_hash") or ""):
            raise ValueError("qr_reference_payload_changed")
    deal_inputs: list[str] = []
    deal = geo.get("deal") or {}
    deal_pad_failed: Optional[str] = None
    if deal and str(component.get("channel") or "").startswith("deal_"):
        # 打码确认后的素材作为垫图;deal 渠道合同是"基于已打码确认的真实素材",
        # 全部加载失败时不得静默裸生成(组件失败 + 人话原因,见下方拦截)。
        from services.marketing import redaction as deal_redaction

        frozen_pads = list(deal.get("redacted_materials") or [])[:4]
        load_errors: list[str] = []
        for item in frozen_pads:
            try:
                deal_inputs.append(deal_redaction.redacted_data_uri(
                    str(item.get("ref") or ""), expected_sha256=str(item.get("sha256") or ""),
                ))
            except Exception as exc:  # noqa: BLE001
                load_errors.append(str(exc))
                logger.warning("deal redacted material unreadable job=%s: %s", job.get("id"), exc)
        if frozen_pads and not deal_inputs:
            deal_pad_failed = (
                "已确认打码素材全部无法读取(文件缺失或内容校验不一致),"
                "请回到晒成交草稿重新打码确认后再生成"
            )
    image_urls = list(dict.fromkeys(
        str(url) for url in (safe_logo, qr_input, *deal_inputs) if url
    )) or None
    marketing_db.update_job(int(job["id"]), final_prompt=prompt)
    component_id = str(component["component_id"])
    attempts = marketing_db.list_attempts(int(job["id"]), component_id)
    if deal_pad_failed:
        # deal 渠道垫图全部加载失败 → 组件如实失败(人话原因落 attempt),
        # 绝不脱离已打码真实素材"裸生成";不阻断整包其余组件。
        failed_attempt = marketing_db.add_attempt(
            job_id=int(job["id"]), attempt_no=len(attempts) + 1,
            component_id=component_id, status="pending",
        )
        marketing_db.finish_attempt(
            int(failed_attempt["id"]), status="failed", submit_state="not_sent",
            error_detail=f"deal_redacted_material_unreadable:{deal_pad_failed}"[:500],
        )
        marketing_db.add_event(
            event_type="geo_deal_component_failed", actor_id=int(job["user_id"]),
            message=deal_pad_failed,
            payload={"job_id": int(job["id"]), "component_id": component_id,
                     "reason": deal_pad_failed},
        )
        return None
    attempt = next((
        row for row in reversed(attempts)
        if row["status"] in {"pending", "running"}
        or (
            row["status"] == "succeeded"
            and str(row.get("materialization_state") or "not_started") != "materialized"
            and bool(row.get("provider_result_jsonb"))
        )
    ), None)
    if attempt and attempt.get("submit_state") == "anchored" and not attempt.get("provider_task_id"):
        attempt = marketing_db.update_attempt_provider_state(
            int(attempt["id"]), submit_state="outcome_unknown",
            resolution_state="manual_required", error_detail="worker_lost_after_submit_anchor",
        )
    for attempt_no in range((int(attempt["attempt_no"]) if attempt else len(attempts) + 1), 4):
        if attempt is None:
            attempt = marketing_db.add_attempt(
                job_id=int(job["id"]), component_id=component_id, attempt_no=attempt_no,
                provider="apimart-gpt-image-2", status="pending",
            )
        task_id = str(attempt.get("provider_task_id") or "")
        submit_state = str(attempt.get("submit_state") or "not_started")
        if submit_state == "outcome_unknown":
            return {"_pending": True, "error": "provider_submit_unknown"}
        if not task_id:
            guard_token = "submit_" + secrets.token_hex(24)
            marketing_db.anchor_attempt_before_submit(int(attempt["id"]), guard_token)
            try:
                result = await image_client.submit_image(
                    prompt, size=slot["size"], resolution=str(job.get("resolution") or "1k"),
                    image_urls=image_urls, before_request=provider_guard,
                )
            except image_client.LiveAuthorityRejected as exc:
                marketing_db.finish_attempt(
                    int(attempt["id"]), status="failed", submit_state="not_sent",
                    error_detail=f"live_authority_rejected:{str(exc)[:400]}",
                )
                raise
            state = str(result.get("state") or "outcome_unknown")
            if state == "outcome_unknown":
                marketing_db.update_attempt_provider_state(
                    int(attempt["id"]), submit_state="outcome_unknown",
                    resolution_state="manual_required",
                    error_detail=str(result.get("error") or "submit_outcome_unknown")[:500],
                )
                return {"_pending": True, "error": "provider_submit_unknown"}
            if state != "submitted":
                marketing_db.finish_attempt(
                    int(attempt["id"]), status="failed", submit_state=state,
                    error_detail=str(result.get("error") or "image_submit_failed")[:500],
                )
                attempt = None
                continue
            task_id = str(result["provider_task_id"])
            attempt = marketing_db.update_attempt_provider_state(
                int(attempt["id"]), submit_state="submitted", poll_state="polling",
                provider_task_id=task_id,
            )
        result_payload = dict(attempt.get("provider_result_jsonb") or {})
        if str(attempt.get("poll_state") or "") != "succeeded":
            async def before_poll() -> None:
                callback = poll_guard or provider_guard
                if callback is None:
                    return
                guarded = callback(int(attempt["id"]), task_id) if poll_guard is not None else callback()
                if asyncio.iscoroutine(guarded):
                    await guarded

            poll = await image_client.poll_image_task(task_id, before_request=before_poll)
            if poll.get("state") == "pending":
                marketing_db.update_attempt_provider_state(
                    int(attempt["id"]), poll_state="pending",
                    error_detail=str(poll.get("error") or "provider_poll_pending")[:500],
                )
                return {"_pending": True, "error": "provider_poll_pending"}
            if poll.get("state") != "succeeded":
                marketing_db.finish_attempt(
                    int(attempt["id"]), status="failed", provider_task_id=task_id,
                    submit_state="submitted", poll_state="failed", materialization_state="failed",
                    error_detail=str(poll.get("error") or "image_generation_failed")[:500],
                )
                attempt = None
                continue
            result_payload = {**result_payload, "image_url": str(poll["image_url"])}
            attempt = marketing_db.update_attempt_provider_state(
                int(attempt["id"]), poll_state="succeeded", provider_task_id=task_id,
                provider_result=result_payload, materialization_state="pending", error_detail="",
            )

        stored = result_payload.get("stored") if isinstance(result_payload.get("stored"), dict) else None
        if stored and result_payload.get("qa_passed") is True and material_storage.material_reference_exists(stored):
            recovered_warnings = list(result_payload.get("qa_warnings") or [])
            _record_job_warnings(job, geo, [
                {**warning, "channel": channel, "component_id": component_id}
                for warning in recovered_warnings
            ])
            return marketing_db.materialize_attempt_asset(
                attempt_id=int(attempt["id"]),
                provider_cost_usd={"1k": 0.006, "2k": 0.012, "4k": 0.024}.get(str(job.get("resolution") or "1k"), 0.006),
                safety_status="passed", safety_flags=recovered_warnings, asset_kind="bundle_item",
                url_provider=str(result_payload.get("image_url") or ""),
                url_stored=str(stored.get("public_url") or ""),
                thumbnail_url=str(stored.get("thumbnail_url") or ""),
                width=int(stored.get("width") or 0), height=int(stored.get("height") or 0),
                size_bytes=int(stored.get("size_bytes") or 0), sha256=str(stored.get("sha256") or ""),
            )

        image_url = str(result_payload.get("image_url") or "")
        if not image_url:
            if str(attempt.get("status") or "") in {"pending", "running"}:
                marketing_db.update_attempt_provider_state(
                    int(attempt["id"]), poll_state="pending",
                    error_detail="provider_success_result_missing_repoll",
                )
            return {"_pending": True, "error": "provider_submit_unknown"}
        try:
            if provider_guard is not None:
                guarded = provider_guard()
                if asyncio.iscoroutine(guarded):
                    await guarded
            image_bytes = await image_client.download_image(image_url)
            if not image_bytes:
                marketing_db.update_attempt_provider_state(
                    int(attempt["id"]), poll_state="succeeded", error_detail="download_pending",
                )
                return {"_pending": True, "error": "provider_poll_pending"}
            qa = await visual_qa(
                image_bytes, size=slot["size"], exact_copy=exact_copy,
                brand=provider_brand, evidence=geo["evidence"], contact=geo["contact"],
                before_vision=provider_guard,
                # 匿名晒单:冻结快照品牌名反向检测——provider 侧已置空,
                # 但名字仍可能经 copy 混进成图,OCR 命中即 flag → attempt 失败。
                forbidden_brand_name=(
                    str((geo.get("brand") or {}).get("name") or "") if anonymize else ""
                ),
                internal_lineage_values=[
                    str(value) for value in (
                        (geo.get("evidence") or {}).get("source_id"),
                        (geo.get("evidence") or {}).get("organization_id"),
                        (geo.get("evidence") or {}).get("created_by_membership_id"),
                        (geo.get("evidence") or {}).get("snapshot_hash"),
                    ) if value not in (None, "")
                ],
            )
        except image_client.LiveAuthorityRejected as exc:
            # The paid image result is already durable. Revocation must block
            # every later external call and close the attempt, while retaining
            # the immutable result for audit/reconciliation. It must never be
            # interpreted as a reason to submit a replacement image.
            current_attempt = marketing_db.get_attempt(int(attempt["id"])) or attempt
            if str(current_attempt.get("status") or "") in {"pending", "running"}:
                marketing_db.finish_attempt(
                    int(attempt["id"]), status="failed", provider_task_id=task_id,
                    submit_state="submitted", poll_state="succeeded",
                    materialization_state="failed", safety_status="blocked_output",
                    safety_flags=[{"code": _authority_failure_code(exc)}],
                    error_detail=f"{_authority_failure_code(exc)}:{str(exc)[:400]}",
                )
            raise
        if not qa["passed"]:
            # 硬失败只剩法律红线/功能正确性(QR 不一致、图片不可用、内部泄漏)。
            # safety_flags 落库前五问合同化(SSOT §6):仅 code 的裸结构不再持久化。
            qa_errors = _qa_error_entries(qa["errors"])
            marketing_db.finish_attempt(
                int(attempt["id"]), status="failed", provider_task_id=task_id,
                submit_state="submitted", poll_state="succeeded",
                safety_status="blocked_output", safety_flags=qa_errors, materialization_state="failed",
                error_detail=json.dumps(qa_errors, ensure_ascii=False)[:1000],
            )
            attempt = None
            continue
        # 提醒不阻断(Owner 2026-07-22):warnings 随成品落库并在 job 公开态透出。
        qa_warnings = list(qa.get("warnings") or [])
        # GEO assets remain behind the authenticated delivery endpoint so a
        # stale URL cannot bypass a later tenant/assignment revocation. Legacy
        # marketing materials keep their existing public storage contract.
        stored = material_storage.save_material_image(
            owner_key, image_bytes, f"{slot['slot']}.png", private=True,
        )
        result_payload = {**result_payload, "stored": stored, "qa_passed": True, "qa_warnings": qa_warnings}
        if str(attempt.get("status") or "") in {"pending", "running"}:
            attempt = marketing_db.update_attempt_provider_state(
                int(attempt["id"]), provider_result=result_payload, materialization_state="pending",
            )
        _record_job_warnings(job, geo, [
            {**warning, "channel": channel, "component_id": component_id}
            for warning in qa_warnings
        ])
        return marketing_db.materialize_attempt_asset(
            attempt_id=int(attempt["id"]),
            provider_cost_usd={"1k": 0.006, "2k": 0.012, "4k": 0.024}.get(str(job.get("resolution") or "1k"), 0.006),
            safety_status="passed", safety_flags=qa_warnings, asset_kind="bundle_item",
            url_provider=image_url, url_stored=stored["public_url"],
            thumbnail_url=stored.get("thumbnail_url") or "", width=int(stored.get("width") or 0),
            height=int(stored.get("height") or 0), size_bytes=int(stored.get("size_bytes") or 0),
            sha256=str(stored.get("sha256") or ""),
        )
        attempt = None
    return None


async def _settle_geo(job: dict, ctx: dict, assets: list[dict], expected: int, *, external_started: bool) -> dict:
    # 🔴 [#113 D2-b ②] full/partial 判定抽成 `delivery_state` 一处实现。
    #    窄门与终态两处都要问同一个问题「这个 job 交付齐了没有」——
    #    问题问两遍就会答出两种答案,而答歪了是**钱的方向**。
    _d = delivery_state(job, assets=assets, expected=expected)
    full, partial = _d["full"], _d["partial"]
    billing_kind = ctx["billing_kind"]
    try:
        if billing_kind == "organization":
            from services.organization_billing import (
                release_charge,
                renew_settlement_lease,
                settle_charge,
                settle_charge_via_reconciliation,
            )

            charge_id = int(ctx["organization_charge_id"])
            live_claim_token = str(ctx.get("claim_token") or "")
            if not ctx.get("settlement_only"):
                renew_settlement_lease(
                    charge_link_id=charge_id,
                    claim_token=live_claim_token,
                    lease_seconds=1800,
                )
            if external_started:
                outcome_payload = {
                    "job_id": int(job["id"]),
                    "status": "succeeded" if full else ("partial_success" if partial else "failed"),
                    "asset_ids": [int(asset["id"]) for asset in assets],
                }
                actual = int(ctx.get("actual_points") or 0) if full else 0
                if live_claim_token:
                    # 活跃 worker 绑定真实 claim 证据：settle_charge 强制校验
                    # 存活租约（外部独立审核裁决 2026-07-23，stale-worker 防护）。
                    await asyncio.shield(settle_charge(
                        charge_link_id=charge_id,
                        actual_points=actual,
                        result_payload=outcome_payload,
                        claim_token=live_claim_token,
                    ))
                else:
                    # settlement_only 恢复路径无存活 claim（kill-9 后 token 已
                    # 死），改调显式 reconciliation 专用入口：同一份
                    # external_side_effect + outcome 证据闸，并强制记录
                    # recovery_identity 进审计/日志。活跃路径不得走此分支。
                    await asyncio.shield(settle_charge_via_reconciliation(
                        charge_link_id=charge_id,
                        actual_points=actual,
                        result_payload=outcome_payload,
                        recovery_identity=str(
                            ctx.get("recovery_identity")
                            or "geo_factory._settle_geo:settlement_only"
                        ),
                    ))
            else:
                await asyncio.shield(release_charge(
                    charge_link_id=charge_id, reason="GEO 内容包未调用外部服务",
                    failure_snapshot={"job_id": int(job["id"]), "status": "failed"},
                ))
        else:
            from middleware.billing import commit_freeze, release_freeze

            freeze_id = ctx.get("freeze_id")
            payer = int(ctx["payer_user_id"])
            if full and freeze_id:
                result = await asyncio.shield(commit_freeze(
                    freeze_id=freeze_id, reason="GEO 获客内容包生成完成", user_id=payer,
                ))
                if isinstance(result, dict) and result.get("success") is False:
                    raise RuntimeError("billing_commit_rejected")
            elif freeze_id:
                result = await asyncio.shield(release_freeze(
                    freeze_id=freeze_id,
                    reason="GEO 内容包部分成功整单免单" if partial else "GEO 内容包生成失败退费",
                    user_id=payer,
                ))
                if isinstance(result, dict) and result.get("success") is False:
                    raise RuntimeError("billing_release_rejected")
    except BaseException:  # includes cancellation during settlement
        logger.exception("GEO package settlement failed closed job=%s", job["id"])
        pending_code = "billing_commit_pending" if external_started else "billing_release_pending"
        # A transport/cancellation exception says nothing about the committed
        # ledger state. Keep the job resumable and replay the unique billing
        # link; both organization and legacy primitives are idempotent.
        marketing_db.update_job(int(job["id"]), status="generating", error_summary=pending_code)
        return {"status": "generating", "job_id": int(job["id"]), "error": pending_code}

    geo = (job.get("input_fields_jsonb") or {}).get("_geo") or {}
    frozen_refs = {
        str(asset.get("bundle_slot") or ""): int(asset["id"])
        for asset in assets if asset.get("bundle_slot")
    }
    geo = {**geo, "effective_asset_refs": frozen_refs}
    marketing_db.patch_job_input_fields(int(job["id"]), {"_geo": geo})

    if full:
        marketing_db.update_job(int(job["id"]), status="succeeded", error_summary="")
        return {"status": "succeeded", "job_id": int(job["id"]), "assets": assets}
    if partial:
        marketing_db.update_job(int(job["id"]), status="succeeded", error_summary="partial_free_release")
        return {"status": "partial_success", "job_id": int(job["id"]), "assets": assets, "partial_free": True}
    marketing_db.update_job(int(job["id"]), status="failed", error_summary="all_components_failed")
    return {"status": "failed", "job_id": int(job["id"]), "error": "all_components_failed"}


async def execute_geo_package_job(ctx: dict) -> dict:
    """Generate channel-native components once; duplicate workers fail closed."""
    from db.connection import get_connection

    job_id = int(ctx["job_id"])
    lock_key = f"geo-content-execute:{job_id}"
    lock_acquired = False
    lock_conn = get_connection()
    try:
        cursor = lock_conn.cursor()
        cursor.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired",
            (lock_key,),
        )
        lock_acquired = bool(cursor.fetchone()["acquired"])
        if not lock_acquired:
            return {"status": "generating", "job_id": job_id, "in_progress": True}
        job = marketing_db.get_job(job_id)
        if not job:
            return {"status": "failed", "job_id": job_id, "error": "job_missing"}
        if job["status"] in {"succeeded", "failed", "blocked"}:
            return _row_status(job)
        geo = (job.get("input_fields_jsonb") or {}).get("_geo") or {}
        existing_assets = marketing_db.list_assets(job_id)
        done = {str(asset.get("bundle_slot") or "") for asset in existing_assets}
        durable_attempts = marketing_db.list_attempts(job_id)
        legacy_open = [
            row for row in durable_attempts
            if str(row.get("status") or "") in {"pending", "running"}
            and not str(row.get("component_id") or "")
        ]
        if legacy_open:
            # Pre-migration running attempts have no component or submit
            # anchor, so the process cannot prove whether a paid POST happened.
            # Quarantine instead of generating a replacement automatically.
            for row in legacy_open:
                marketing_db.update_attempt_provider_state(
                    int(row["id"]), submit_state="outcome_unknown", resolution_state="manual_required",
                    error_detail="legacy_attempt_submit_outcome_unknown",
                )
            marketing_db.update_job(job_id, status="generating", error_summary="provider_submit_unknown")
            return {"status": "generating", "job_id": job_id, "error": "provider_submit_unknown"}
        for row in durable_attempts:
            component_id = str(row.get("component_id") or "")
            if (
                str(row.get("status") or "") in {"pending", "running"}
                and component_id in done
            ):
                # A durable asset is the authoritative completion record. Close
                # a crash-left attempt so it cannot keep a merged revision in
                # generating forever.
                marketing_db.finish_attempt(
                    int(row["id"]), status="succeeded",
                    provider_cost_usd=float(row.get("provider_cost_usd") or 0),
                    provider_task_id=str(row.get("provider_task_id") or "") or None,
                    submit_state="submitted",
                    poll_state="succeeded", safety_status=str(row.get("safety_status") or "passed"),
                    safety_flags=list(row.get("safety_flags_jsonb") or []),
                )
        settlement_only = bool(ctx.get("settlement_only")) or str(job.get("error_summary") or "") in {"billing_commit_pending", "billing_release_pending"}
        plan = [] if settlement_only else [item for item in _component_plan(geo) if item["component_id"] not in done]
        expected = len(_full_component_plan(geo))
        assets = effective_assets(job)
        # Recovery must not infer "no provider call" merely because this
        # process has not called one yet. Durable attempts/assets and a pending
        # commit prove that an earlier worker crossed the external boundary.
        external_started = bool(
            existing_assets
            or any(
                str(row.get("submit_state") or "") in {"anchored", "submitted", "outcome_unknown", "rejected"}
                or bool(row.get("provider_task_id"))
                for row in durable_attempts
            )
            or str(job.get("error_summary") or "") == "billing_commit_pending"
        )
        try:
            def provider_guard() -> None:
                nonlocal external_started
                if ctx["billing_kind"] == "organization":
                    from services.organization_billing import mark_external_side_effect_started
                    mark_external_side_effect_started(
                        charge_link_id=int(ctx["organization_charge_id"]),
                        claim_token=str(ctx["claim_token"]), lease_seconds=1800,
                    )
                else:
                    _require_actor_brand_live(job)
                require_frozen_evidence_live(geo.get("evidence") or {})
                external_started = True

            def provider_poll_guard(attempt_id: int, provider_task_id: str) -> None:
                if (
                    ctx["billing_kind"] == "organization"
                    and int(ctx.get("poll_only_attempt_id") or 0) == int(attempt_id)
                    and str(ctx.get("poll_only_task_id") or "") == str(provider_task_id)
                ):
                    from services.organization_billing import renew_poll_only_lease
                    renew_poll_only_lease(
                        charge_link_id=int(ctx["organization_charge_id"]),
                        claim_token=str(ctx["claim_token"]), attempt_id=int(attempt_id),
                        provider_task_id=str(provider_task_id), lease_seconds=1800,
                    )
                    return
                provider_guard()

            # Preflight is useful, but each actual provider request invokes the
            # same live guard again immediately before network I/O.
            if job.get("brand_id"):
                live_brand_snapshot(int(job["brand_id"]))
            if ctx["billing_kind"] != "organization":
                _require_actor_brand_live(job)
            require_frozen_evidence_live(geo.get("evidence") or {})
            owner_key = f"u{int(job['user_id'])}"
            content_by_channel: dict[str, dict] = {}
            content_qa_by_channel: dict[str, dict] = {}
            # 恢复/重放路径的文案来源(2026-07-23 外部审查 P1-3):copy 组件不在
            # 本次 plan(已持久化)时,渠道文案读持久化 copy 资产,绝不重跑文案
            # LLM;copy 组件本身在 plan(显式重做)才允许再生成。
            copy_channels_in_plan = {
                str(item["channel"]) for item in plan if str(item.get("kind") or "") == "copy"
            }
            for component in plan:
                skip_reason = deal_component_skip_reason(geo, component)
                if skip_reason:
                    # 组件级失败给人话原因,不阻断整包;成功资产照常交付。
                    skipped = geo.setdefault("deal", {}).setdefault("skipped_components", [])
                    if not any(item.get("component_id") == component["component_id"] for item in skipped):
                        skipped.append({"component_id": component["component_id"], "reason": skip_reason})
                        marketing_db.add_event(
                            event_type="geo_deal_component_skipped", actor_id=int(job["user_id"]),
                            message=skip_reason,
                            payload={
                                "job_id": int(job["id"]),
                                "component_id": component["component_id"],
                                "reason": skip_reason,
                            },
                        )
                        # 首次跳过即落库 durable:恢复循环/重放重新读到 geo 时
                        # skipped_components 已在持久副本里,不会重复记账审计事件。
                        marketing_db.patch_job_input_fields(int(job["id"]), {"_geo": geo})
                    continue
                channel = component["channel"]
                if channel not in content_by_channel:
                    reused = None
                    if channel not in copy_channels_in_plan:
                        # 已持久化的 copy 资产是耐久事实:恢复/轮询重放直接重放
                        # 持久化文案,QA 纯函数重算;不新增任何文案 LLM 调用。
                        persisted = _persisted_channel_copy(assets, channel)
                        if persisted is not None:
                            reused = (persisted, _reused_channel_copy_qa(persisted, geo, channel))
                    if reused is not None:
                        content, copy_qa = reused
                    else:
                        content, copy_qa = await generate_channel_content(
                            channel, strategy=geo["strategy"], evidence=geo["evidence"],
                            trend=geo["trend"], contact=geo["contact"], teacher=geo["teacher"],
                            angle=(geo.get("angles") or {}).get(channel),
                            facts_pack=geo.get("product_facts"),
                            before_provider_call=provider_guard,
                        )
                    content_by_channel[channel] = content
                    content_qa_by_channel[channel] = copy_qa
                    if not copy_qa["passed"]:
                        logger.warning("copy QA legal-line errors job=%s channel=%s errors=%s", job_id, channel, copy_qa["errors"])
                content = content_by_channel[channel]
                if not content_qa_by_channel[channel]["passed"]:
                    # 法律红线(广告法极限词)才拦:文案含极限词时图片不得嵌这段
                    # 文案,该渠道全部组件跳过;warnings 一律放行不阻断。
                    continue
                if component["kind"] == "copy":
                    qa = claim_evidence_qa(
                        content, evidence=geo["evidence"], contact=geo["contact"], channel=channel,
                    )
                    if qa["passed"]:
                        assets.append(marketing_db.add_asset(
                            job_id=job_id, asset_kind="copy", bundle_slot=component["component_id"],
                            content_text=json.dumps(content, ensure_ascii=False, sort_keys=True),
                            is_final=True, rights_confirmed=0,
                        ))
                        _record_job_warnings(job, geo, [
                            {**warning, "channel": channel, "component_id": component["component_id"]}
                            for warning in (qa.get("warnings") or [])
                        ])
                else:
                    asset = await _generate_image_component(
                        job=job, component=component, geo=geo, content=content, owner_key=owner_key,
                        provider_guard=provider_guard, poll_guard=provider_poll_guard,
                    )
                    if asset and asset.get("_pending"):
                        marketing_db.update_job(job_id, status="generating", error_summary=str(asset.get("error") or "provider_poll_pending"))
                        return {"status": "generating", "job_id": job_id, "error": asset.get("error")}
                    if asset:
                        assets.append(asset)
        except Exception as exc:
            logger.warning("GEO package generation failed job=%s: %s", job_id, exc)
            if not external_started:
                marketing_db.update_job(job_id, error_summary=f"preflight_revoked:{str(exc)[:160]}")
        open_attempts = [
            row for row in marketing_db.list_attempts(job_id)
            if str(row.get("status") or "") in {"pending", "running"}
            or (
                str(row.get("status") or "") == "succeeded"
                and str(row.get("materialization_state") or "not_started") != "materialized"
            )
        ]
        if open_attempts:
            pending_code = (
                "provider_submit_unknown"
                if any(str(row.get("submit_state") or "") in {"anchored", "outcome_unknown"} and not row.get("provider_task_id") for row in open_attempts)
                else "provider_poll_pending"
            )
            marketing_db.update_job(job_id, status="generating", error_summary=pending_code)
            return {"status": "generating", "job_id": job_id, "error": pending_code}
        return await _settle_geo(job, ctx, assets, expected, external_started=external_started)
    finally:
        try:
            if lock_acquired:
                unlock_cursor = lock_conn.cursor()
                unlock_cursor.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                    (lock_key,),
                )
                lock_conn.commit()
        except Exception:
            logger.exception("GEO execution advisory unlock failed job=%s", job_id)
        finally:
            try:
                lock_conn.close()
            except Exception:
                pass


async def recover_reconciled_geo_job(job_id: int) -> dict:
    """Run the durable worker path after an audited unknown-outcome decision."""
    job = marketing_db.get_job(int(job_id))
    if not job or str(job.get("status") or "") != "generating":
        return {"status": "ignored", "job_id": int(job_id)}
    attempts = marketing_db.list_attempts(int(job_id))
    resolved = next((
        row for row in reversed(attempts)
        if str(row.get("resolution_state") or "") == "manual_resolved"
    ), None)
    if not resolved:
        return {"status": "ignored", "job_id": int(job_id)}
    manual = dict((resolved.get("provider_result_jsonb") or {}).get("manual_resolution") or {})
    resolution = str(manual.get("resolution") or "")
    if resolution == "not_sent":
        # A fresh paid POST still requires the tenant's current live authority;
        # the next authenticated poll re-enters prepare_geo_package_job.
        return {"status": "generating", "job_id": int(job_id), "awaiting_live_authority": True}
    billing_ref = str(job.get("billing_ref") or "")
    if billing_ref.startswith("org:"):
        charge_id = int(billing_ref.split(":", 1)[1])
        if resolution == "succeeded":
            from services.organization_billing import claim_poll_only_charge_by_anchor

            claim = claim_poll_only_charge_by_anchor(
                charge_link_id=charge_id, attempt_id=int(resolved["id"]),
                provider_task_id=str(resolved.get("provider_task_id") or ""),
            )
            ctx = {
                "job_id": int(job_id), "billing_kind": "organization",
                "organization_charge_id": charge_id, "claim_token": claim["claim_token"],
                "actual_points": int(claim.get("estimated_points") or job.get("cost_points") or 0),
                "poll_only_attempt_id": int(resolved["id"]),
                "poll_only_task_id": str(resolved.get("provider_task_id") or ""),
            }
        else:
            ctx = {
                "job_id": int(job_id), "billing_kind": "organization",
                "organization_charge_id": charge_id, "claim_token": "",
                "actual_points": int(job.get("cost_points") or 0), "settlement_only": True,
                "recovery_identity": "geo_factory.recover_reconciled_geo_job:manual_resolution",
            }
    else:
        ctx = {
            "job_id": int(job_id), "billing_kind": "legacy", "freeze_id": job.get("freeze_id"),
            "payer_user_id": int(job["user_id"]), "settlement_only": resolution == "failed",
        }
    return await execute_geo_package_job(ctx)


def _charge_is_unknown(billing_ref: str) -> bool:
    """组织 charge 是否已进 unknown(进了就不该再每分钟撞 409)。

    🔴 只认 `org:` 前缀那一支;legacy 冻结不走这条路,**不许顺手扩大作用域**。
    """
    ref = str(billing_ref or "")
    if not ref.startswith("org:"):
        return False
    try:
        from db.connection import get_connection

        charge_id = int(ref.split(":", 1)[1])
    except (ValueError, IndexError):
        return False
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM organization_charge_links WHERE id=%s", (charge_id,))
        row = cur.fetchone()
        return bool(row) and str(row.get("status") or "") == "unknown"
    except Exception as exc:      # noqa: BLE001 —— 读不到就按"没进 unknown"走原路
        logger.warning("[geo-factory] 读 charge=%s 状态失败(按未 unknown 处理):%s", ref, exc)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _finalise_job_after_auto_release(job: dict) -> str:
    """[#113 D2-b 返修] 窄门释放成功后给 job 写终态,返回写下的 error_summary。

    🔴 终态**按真实交付情况**写,不是一律 failed:
      零资产      -> status=failed,    error_summary=all_components_failed
      有资产不齐  -> status=succeeded,  error_summary=partial_free_release

    这两格不是我发明的,是 `_settle_geo` 收尾处已有的口径
    (`full = len(assets) >= expected` / `partial = bool(assets) and not full`,
     partial 那支的 release 理由原文就是「GEO 内容包部分成功整单免单」)。
    走窄门释放的 job 与正常释放的 job **必须同形** —— 否则同一件事在库里有两种
    长相,下游报表、前端文案、以后的普查都要各写一遍分支。

    🔴 为什么不能一律写 failed:一个已经交付了可用文案的 job 被标成
       failed,前端会告诉用户「生成失败」,而他手里明明有东西 ——
       **一个具体而错误的状态比没有状态更坏**,它让人不再去看那份资产。

    🔴 为什么不能留在 generating:那样 job 会永远显示"生成中",
       而它退出恢复队列只是因为 error_summary 恰好不在 `list_recoverable_geo_jobs`
       的 IN 列表里 —— 那是靠巧合退出,不是靠终态退出。
    """
    # 🔴 [②] 与窄门用**同一把尺**(`delivery_state`),不再自己 `if assets:`。
    #    窄门按 `not full` 决定退钱、终态按 `partial` 决定写 succeeded 还是 failed;
    #    两处各算一遍就可能「退了钱却标成功」或「标了部分成功却没退钱」,
    #    而这两种不一致都不会有任何东西报错。
    #    尤其是 R1F3 那条:重试 job 零新交付时 `assets` 非空但 partial=False,
    #    旧写法 `if assets:` 会把它标成 partial_free_release —— 错的具体状态。
    if delivery_state(job)["partial"]:
        code = "partial_free_release"
        marketing_db.update_job(int(job["id"]), status="succeeded", error_summary=code)
    else:
        code = "all_components_failed"
        marketing_db.update_job(int(job["id"]), status="failed", error_summary=code)
    return code

def delivery_state(job: dict, *, assets=None, expected=None) -> dict:
    """[#113 D2-b ②] **一处判定**:这个 job 交付了几件、算 full / partial / none。

    返回 `{delivered, expected, full, partial}`。`none` = 两个布尔都为假。

    🔴 规则原样取自 `_settle_geo`(本函数就是从那儿抽出来的),包含
       **R1F3 存量语义修正**:`assets` 是按血缘合并的视图(父 job 的成功资产也计入)。
       局部重试 job(`parent_job_id` 非空)若本 job **零新交付** —— 例如对已成功的
       组件发起「只重做这一项」而重试自身全败 —— 合并视图仍凑满 expected,
       按旧口径会 full 提交扣费「假成功」(用户为重做付了费却什么都没得到)。
       修正:重试 job 零新交付即按 none 处理(结算释放计费);
       血缘继承只用于**交付视图**,不计入本 job 的结算产出。

    🔴 抽这个 helper 的理由不是去重,是**窄门和终态必须用同一把尺**:
       窄门按 `not full` 决定退不退钱,终态按 `partial` 决定 job 写成
       succeeded 还是 failed。两处各算一遍的话,可以出现「退了钱却标成功」
       或「标了部分成功却没退钱」—— 而这两种不一致都不会有任何东西报错。

    ⚠️ 本仓还有**第三处**同名概念:`_reconcile_terminal_job`(:328)用的是
       `len({去重后的 bundle_slot}) >= expected`,与这里的 `len(assets) >= expected`
       **算法不同**(资产没有 bundle_slot 时会分岔)。**本次没有合并它** ——
       那是静默的行为变更,而我回归不了它。分岔本身由
       `test_the_two_delivery_rules_are_known_to_diverge` 钉住,
       免得后来人以为三处是同一把尺。
    """
    if assets is None:
        assets = effective_assets(job)
    geo = (job.get("input_fields_jsonb") or {}).get("_geo") or {}
    if expected is None:
        expected = len(_full_component_plan(geo))
    expected = int(expected or 0)
    delivered = len(assets)
    full = delivered >= expected and expected > 0
    partial = bool(assets) and not full
    if geo.get("parent_job_id") and assets:
        own_new = [a for a in assets
                   if int(a.get("job_id") or job["id"]) == int(job["id"])]
        if not own_new:
            # 重试 job 零新交付 ⇒ 既不 full 也不 partial(= none,该退钱)
            full = False
            partial = False
            delivered = 0
    return {"delivered": delivered, "expected": expected,
            "full": bool(full), "partial": bool(partial)}

def _undelivered_provider_success_evidence(job: dict) -> dict | None:
    """[#113 D2-b ②] 采窄门要用的**落库读数**;拿不齐返回 None(= 窄门不成立)。

    Owner 2026-09-06 拍板放宽:**部分交付也退**。依据不是新规则 —— 仓里正常路径
    `_settle_geo` 对 partial 走的就是 `release_freeze("GEO 内容包部分成功整单免单")`。
    窄门只是让**恢复路径**用上同一条既有规则,而不是自己发明一套。

    🔴 弃掉旧的「三计数」证据(provider_succeeded / materialized / assets 三条 SQL)。
       旧口径按「assets == 0」判零交付,与 `_settle_geo` 的 full/partial 是**两把尺**;
       现在统一走 `delivery_state`,窄门条件 = `not full`(delivered < expected)。

    四项各自的作用(缺一不可):
      delivered / expected        —— not full 才放(full 说明用户拿全了,该扣钱)
      provider_succeeded_attempts —— >=1 说明钱确实花在供应商上了
      in_flight_attempts          —— 🔴 必须 0。还有 attempt 在 pending/running,
                                     供应商**可能还会交付**,此时退钱等于免费执行。

    🔴 读不到一律返 None:失败方向必须**保守**,读库出错绝不能推出「可以退钱」。
    """
    from db.connection import get_connection

    try:
        d = delivery_state(job)
    except Exception as exc:      # noqa: BLE001 —— 血缘展开可能抛(环/父不属本人)
        logger.warning("[geo-factory] job=%s 交付态算不出(按窄门不成立处理):%s",
                       job.get("id"), exc)
        return None
    if int(d["expected"]) <= 0:
        # 期望件数算不出来 ⇒ `not full` 会恒真,窄门就成了「永远放」。保守退出。
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS c FROM marketing_material_generation_attempts"
            " WHERE job_id=%s AND poll_state='succeeded'",
            (int(job["id"]),),
        )
        provider_ok = int((cur.fetchone() or {}).get("c") or 0)
        cur.execute(
            "SELECT COUNT(*) AS c FROM marketing_material_generation_attempts"
            " WHERE job_id=%s AND status IN ('pending','running')",
            (int(job["id"]),),
        )
        in_flight = int((cur.fetchone() or {}).get("c") or 0)
    except Exception as exc:      # noqa: BLE001
        logger.warning("[geo-factory] job=%s 取 D2-b 证据失败(按窄门不成立处理):%s",
                       job.get("id"), exc)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {
        "delivered": int(d["delivered"]),
        "expected": int(d["expected"]),
        "provider_succeeded_attempts": provider_ok,
        "in_flight_attempts": in_flight,
    }

async def _try_auto_release_undelivered(*, job: dict, billing_ref: str) -> bool:
    """[#113 D2-b] 试着走自动窄门;走成了返回 True。

    Owner 2026-09-06 拍板:**用户没拿到东西不扣**,平台吸收供应商成本,
    图片结果留在 attempt 行里供审计。

    🔴 这是给 `_quarantine_locked_charge` 保守口径开的**一条窄门**,不是放宽它。
       那条口径的原话是「provider 边界之后的异常**不能证明**没干活」——
       而这里要求拿出**能证明**的落库读数。任何一项拿不到 ⇒ 退回人工(D2-a 告警)。

    🔴 任何异常都吞掉并返回 False:窄门走不通**绝不能**影响原来的处置路径。
       资金动作宁可不做,也不能因为一个新分支把旧的收口也带塌。
    """
    if not str(billing_ref or "").startswith("org:"):
        return False
    evidence = _undelivered_provider_success_evidence(job)
    if not evidence:
        return False
    if evidence["provider_succeeded_attempts"] < 1:
        return False
    if evidence["in_flight_attempts"]:
        # 🔴 还有 attempt 在跑 ⇒ 供应商可能还会交付,现在退钱等于免费执行。
        return False
    if evidence["delivered"] >= evidence["expected"]:
        # full ⇒ 用户拿全了,该扣钱不该退。窄门只放 `not full`。
        return False
    try:
        from services.organization_billing import auto_release_undelivered_charge

        await auto_release_undelivered_charge(
            charge_link_id=int(billing_ref.split(":", 1)[1]),
            reason="provider succeeded but nothing was delivered (D2-b)",
            evidence=evidence,
            recovery_identity="geo_factory.reconcile_recoverable_geo_jobs:d2b",
        )
    except Exception as exc:      # noqa: BLE001
        logger.warning(
            "[geo-factory] job=%s D2-b 自动释放未成(退回人工):%s",
            job["id"], exc)
        return False
    logger.info(
        "[geo-factory] job=%s D2-b 自动释放冻结 ref=%s 证据=%s",
        job["id"], billing_ref, evidence)
    return True

def _alert_reconcile_gave_up(*, job_id: int, billing_ref: str) -> None:
    """恢复链主动停手时开一条告警 —— **停手不能等于沉默**。

    停掉空转本身不解决问题:钱还冻着,只是不再每分钟撞墙。
    真正的出口(force_release_charge)需要人,所以必须有人被通知。
    """
    try:
        from db.ai_ops_db import upsert_alert

        upsert_alert(
            "geo_reconcile_gave_up",
            severity="critical",
            title="GEO 恢复链对 unknown charge 停止重试(需人工处置)",
            detail=("job=%s billing_ref=%s · charge 已 unknown,恢复链不再每 60 秒重试;"
                    "出口是 force_release_charge(owner/平台 admin,须 lease 过期)"
                    % (job_id, billing_ref)),
            fingerprint="geo_reconcile_gave_up:%s" % billing_ref,
            payload={"job_id": int(job_id), "billing_ref": str(billing_ref)},
        )
    except Exception as exc:      # noqa: BLE001
        logger.warning("[geo-factory] job=%s 停手告警没写成:%s", job_id, exc)


async def reconcile_recoverable_geo_jobs(limit: int = 100) -> list[dict]:
    """Cron-leader worker for settlement loss and audited provider outcomes."""
    results: list[dict] = []
    for job in marketing_db.list_recoverable_geo_jobs(limit=limit):
        job_id = int(job["id"])
        try:
            error = str(job.get("error_summary") or "")
            if error in {"billing_commit_pending", "billing_release_pending"}:
                assets = effective_assets(job)
                expected = len(_full_component_plan((job.get("input_fields_jsonb") or {}).get("_geo") or {}))
                billing_ref = str(job.get("billing_ref") or "")
                # 🔴🔴 [#113 D2-a] charge 已 unknown ⇒ **停掉每 60 秒的重试**。
                #
                #    生产事实:charge 一旦被 quarantine 成 unknown,
                #    `_settle_charge_core` 的 `status!='reserved'` 必然 409
                #    (ORG_CHARGE_NOT_RESERVED),`_settle_geo` 的 except 把它吞掉,
                #    job 保持 billing_commit_pending ⇒ **下一分钟原样再来一次**。
                #    24 小时跑了 564 次,每次都注定失败,**而没有一个人被通知**。
                #    这不是"重试",是一个不会停的空转 —— 它唯一的产出是日志噪音,
                #    还把真正需要人介入这件事**藏在噪音里**。
                if _charge_is_unknown(billing_ref):
                    # 🔴 [#113 D2-b] 放弃之前先试**自动窄门**:
                    #    供应商成功过(钱花了)且一件都没交付 ⇒ 用户不该被扣,
                    #    平台吸收成本。窄门不成立就原样走 D2-a 的停手 + 告警。
                    #    顺序不能反:先试窄门再告警,否则每次都会先惊动人。
                    if await _try_auto_release_undelivered(
                            job=job, billing_ref=billing_ref):
                        # 🔴 [返修] 原来这里只写 error_summary,job 留在 `generating` ——
                        #    前端会永远显示"生成中",而它退出恢复队列只是因为
                        #    error_summary 恰好不在选择器的 IN 列表里(靠巧合退出)。
                        #    改用与下面那支**同一个** helper 写真终态。
                        code = _finalise_job_after_auto_release(job)
                        results.append({"job_id": job_id, "status": "auto_released_undelivered",
                                        "error_summary": code})
                        continue
                    marketing_db.update_job(
                        job_id, error_summary="provider_resolution_pending")
                    _alert_reconcile_gave_up(job_id=job_id, billing_ref=billing_ref)
                    results.append({"job_id": job_id, "status": "provider_resolution_pending"})
                    continue
                if billing_ref.startswith("org:"):
                    ctx = {
                        "job_id": job_id, "billing_kind": "organization",
                        "organization_charge_id": int(billing_ref.split(":", 1)[1]),
                        "claim_token": "", "actual_points": int(job.get("cost_points") or 0),
                        "settlement_only": True,
                        "recovery_identity": "geo_factory.reconcile_recoverable_geo_jobs:cron",
                    }
                else:
                    ctx = {
                        "job_id": job_id, "billing_kind": "legacy", "freeze_id": job.get("freeze_id"),
                        "payer_user_id": int(job["user_id"]), "settlement_only": True,
                    }
                result = await _settle_geo(
                    job, ctx, assets, expected,
                    external_started=error == "billing_commit_pending",
                )
            elif error == "provider_resolution_pending":
                # 🔴 [#113 D2-b 返修 · 可达性] 这一支是 Review 判 NO-GO 的那条洞。
                #
                #    D2-a 一旦把 job 停进 `provider_resolution_pending`,它下一轮就
                #    落到 `else`,而窄门只长在上面那个 `if` 里 ⇒ **永远到不了**。
                #    生产实证(Deploy 只读取证 2026-09-06):job 12(charge 1 / freeze 795)
                #    此刻正停在 provider_resolution_pending —— 也就是说窄门刚上线
                #    就对它唯一的目标失效了。
                #
                #    🔴 我写了 22 条判据、下了 3 发毒,全都在**那条不可达的分支里面**。
                #    毒证明了窄门有牙,没有一条证明它够得着。**存在 ≠ 可达。**
                #
                #    对未来 job 同样成立:D2-b 首轮任一瞬时失败(读库异常 / 租约未过期 /
                #    释放 503)都会被 D2-a 立刻停进这一态,从此永久人工 ——
                #    窄门只有一次机会,而那次机会常常在租约还没过期时就用掉了。
                #
                #    这里**不再**调 `_alert_reconcile_gave_up`:人已经在 D2-a 那轮被叫过了。
                billing_ref = str(job.get("billing_ref") or "")
                if (billing_ref.startswith("org:") and _charge_is_unknown(billing_ref)
                        and await _try_auto_release_undelivered(
                            job=job, billing_ref=billing_ref)):
                    code = _finalise_job_after_auto_release(job)
                    results.append({"job_id": job_id, "status": "auto_released_undelivered",
                                    "error_summary": code})
                    continue
                # 窄门不成立 ⇒ 人工路径原样不动(可能有人已经做了 manual_resolved)。
                result = await recover_reconciled_geo_job(job_id)
            else:
                result = await recover_reconciled_geo_job(job_id)
            results.append(result)
        except Exception as exc:
            logger.warning("GEO durable reconciliation deferred job=%s: %s", job_id, exc)
            results.append({"status": "deferred", "job_id": job_id})
    return results
