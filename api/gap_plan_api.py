"""缺口作战计划 API(P4 · A2/A3/A4/A5/A6)

路径按设计包 04-P4-api-contract-draft.md 原样落地。

🔴 A5 行级归属:每个端点第一件事就是 `require_quote_access(..., allow_null=False)`,
   它会真读 quotes 行 → 校 brand 归属 → 校组织制品边界。**不依赖中间件兜底** ——
   系统缺一整层资源级归属是既有旧账(2026-08-04 IDOR 扫描结论),新端点必须自带。

🔴 工单 R2 的更正:R2 说归属列是 `price_quotes.buyer_user_id`,并让照抄
   `api/pricing_ssot_api.py:524-531`。经 2026-08-08 生产只读实测,那是**另一套报价**:
     · price_quotes = 算力充值现金报价单(quote_id 是 TEXT,表里根本没有 id 列,58 行)
     · quotes       = 内容交付报价单(id SERIAL,392 行)—— 报价 372 在这张表,brand_id=615
   照 R2 抄会写出对 372 恒 404 的端点。因此改用现役 `auth/brand_access.require_quote_access`。

🔴 错误合同(设计包 04 §7):每个错误都带 code / 人话 / 是否保留旧快照 / 主动作 / 备选动作。
   禁止只返错误码 —— 只返码的后果是前端只能显示"操作失败",运营不知道下一步做什么。
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from auth.brand_access import require_quote_access
from db import gap_plan_db
from services import gap_operation_plan as plan_service
from services.gap_operation_labels import (
    assert_no_internal_leak,
    progress_labels,
    translate_action,
    translate_status,
)

logger = logging.getLogger("GEO-GapPlan-API")

router = APIRouter(prefix="/api/quotes", tags=["缺口作战计划"])


# ════════════════════════════════════════════════════════════════
# 错误合同
# ════════════════════════════════════════════════════════════════

def _error(code: str, *, primary: str, secondary: str | None = None,
           keeps_last_snapshot: bool = False, status_code: int = 400) -> HTTPException:
    label = translate_status(code)
    body = {
        "success": False,
        "error": {
            "code": code,
            "label": label["label"],
            "message": label["explanation"],
            "tone": label["tone"],
            "keeps_last_snapshot": keeps_last_snapshot,
            "primary_action": translate_action(primary),
            "secondary_action": translate_action(secondary) if secondary else None,
        },
    }
    assert_no_internal_leak(body, where=f"error:{code}")
    return HTTPException(status_code=status_code, detail=body)


def _actor_id(request: Request) -> int | None:
    user = getattr(request.state, "user", None) or {}
    try:
        return int(user.get("user_id"))
    except (TypeError, ValueError):
        return None


# ════════════════════════════════════════════════════════════════
# 读取(A2)
# ════════════════════════════════════════════════════════════════

def _load_plan(request: Request, quote_id: int) -> tuple[dict, dict, plan_service.Capacity]:
    quote = require_quote_access(request, int(quote_id), allow_null=False)
    try:
        publications = gap_plan_db.get_publications(int(quote_id))
        capacity = plan_service.compute_capacity(quote, publications)
        bundle = plan_service.build_snapshot(quote, actor_user_id=_actor_id(request))
    except HTTPException:
        raise
    except Exception as exc:                        # noqa: BLE001
        logger.exception("交付计划快照生成失败 quote_id=%s", quote_id)
        raise _error(
            "snapshot_unavailable", primary="retry_snapshot", secondary="back_to_plan",
            keeps_last_snapshot=True, status_code=503,
        ) from exc
    return quote, bundle, capacity


@router.get("/{quote_id}/delivery-plan")
async def get_delivery_plan(quote_id: int, request: Request):
    """报价详情「交付计划」区块的唯一数据源。"""
    _, bundle, capacity = _load_plan(request, quote_id)
    # 🔴 受众显式写死 agent:本 router 挂在 /api/quotes/,全程 require_quote_access,
    #    只服务服务商执行面。客户售前版走 /api/s/{token}(见 api/selection_api.py),
    #    不共用这条路由 —— 那条路由在中间件层就是 401。
    presented = plan_service.present_snapshot(
        bundle, capacity=capacity, audience=plan_service.AUDIENCE_AGENT
    )
    presented["evidence"] = _evidence_block(int(quote_id), bundle["items"])
    assert_no_internal_leak(presented, where="delivery-plan")
    return {"success": True, "snapshot": presented}


@router.get("/{quote_id}/delivery-plan/items/{plan_item_id}")
async def get_delivery_plan_item(quote_id: int, plan_item_id: str, request: Request,
                                 expected_authority_generation: Optional[int] = None):
    """写作中心预填用。校 quote 归属 + 计划项存在 + **代际**。

    🔴 代际校验(判别测试 #2):`expected_authority_generation` 与当前快照代际不符时
       返回 plan_item_generation_stale,前端回词包总览并给可执行解释。
       删掉这段 → 旧链接仍能预填 → 锁 test_stale_generation_is_rejected 转红。
    """
    _, bundle, capacity = _load_plan(request, quote_id)
    snapshot = bundle["snapshot"]

    if (expected_authority_generation is not None
            and int(expected_authority_generation) != int(snapshot["authority_generation"])):
        raise _error("plan_item_generation_stale", primary="back_to_plan",
                     secondary="retry_snapshot", status_code=409)

    item = next((i for i in bundle["items"] if i["plan_item_id"] == plan_item_id), None)
    if item is None:
        raise _error("plan_item_not_found", primary="back_to_plan", status_code=404)

    actions = plan_service._actions_for_item(
        allocation_code=item["allocation_code"], capacity=capacity
    )
    out = {
        "success": True,
        "item": {
            "plan_item_id": item["plan_item_id"],
            "quote_id": item["quote_id"],
            "authority_generation": snapshot["authority_generation"],
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_version": snapshot["snapshot_version"],
            "status": translate_status(item["allocation_code"]),
            "frozen_spec": item.get("frozen_spec_jsonb") or {},
            "rationale": item.get("rationale_jsonb") or {},
            "actions": actions,
            # 🔴 预填**只是预填**:executable=False 时写作中心不得自动生成/扣费/发布。
            "executable": capacity.executable and item["allocation_code"] == "ready_to_execute",
        },
        "capacity": capacity.as_dict(),
    }
    assert_no_internal_leak(out, where="delivery-plan-item")
    return out


@router.get("/{quote_id}/delivery-plan/checkbacks")
async def get_checkbacks(quote_id: int, request: Request):
    require_quote_access(request, int(quote_id), allow_null=False)
    try:
        items = []
        latest = gap_plan_db.get_latest_snapshot(int(quote_id))
        if latest:
            items = gap_plan_db.list_items(latest["snapshot_id"])
        out = {"success": True, "evidence": _evidence_block(int(quote_id), items)}
    except Exception as exc:                        # noqa: BLE001
        logger.exception("回查查询失败 quote_id=%s", quote_id)
        raise _error("checkback_temporarily_unavailable", primary="retry_snapshot",
                     keeps_last_snapshot=True, status_code=503) from exc
    assert_no_internal_leak(out, where="checkbacks")
    return out


def _evidence_block(quote_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    """证据链四格。

    🔴 R9:P5 回查调度未上线前,只有「已发布」能亮。这里**必须**给出明确的
       `not_started_notice` 文案,让前端显示成"计划中的下一步",而不是故障或永远转圈。
    """
    pubs = gap_plan_db.get_publications(quote_id)
    checkbacks = gap_plan_db.list_checkbacks(quote_id)
    labels = progress_labels()

    blocks = {}
    for item in items:
        pid = item["plan_item_id"]
        pub = pubs.get(pid)
        stages = [
            {"key": "published", "label": labels["published"],
             "done": bool(pub and pub.get("evidence_published"))},
            {"key": "indexed", "label": labels["indexed"],
             "done": bool(pub and pub.get("evidence_indexed"))},
            {"key": "cited", "label": labels["cited"],
             "done": bool(pub and pub.get("evidence_cited"))},
            {"key": "customer_recommended", "label": labels["customer_recommended"],
             "done": bool(pub and pub.get("evidence_recommended"))},
        ]
        schedule = [
            {"due_day": cb["due_day"], "due_at": cb["due_at"], "status": cb["status"]}
            for cb in checkbacks.get(pid, [])
        ]
        blocks[pid] = {
            "stages": stages,
            "publication_url": (pub or {}).get("publication_url") or "",
            "published_at": (pub or {}).get("published_at"),
            "checkback_schedule": schedule,
            # R9 态文案:有发布记录但回查还没到点 / 还没发布,两种都不是故障
            "notice": (
                translate_status("checkback_not_started")["explanation"]
                if pub and not any(s["done"] for s in stages[1:])
                else ""
            ),
        }
    return blocks


# ════════════════════════════════════════════════════════════════
# 动作(A3)
# ════════════════════════════════════════════════════════════════

class PublicationLinkRequest(BaseModel):
    publication_url: str = Field(min_length=8, max_length=2048)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    expected_snapshot_version: Optional[str] = None
    expected_authority_generation: Optional[int] = None


_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _normalize_url(url: str) -> tuple[str, str]:
    text = str(url or "").strip()
    body = text.split("://", 1)[-1]
    host = body.split("/", 1)[0].split("?", 1)[0].strip().lower()
    if host.startswith("www."):
        host = host[4:]
    path = body[len(body.split("/", 1)[0]):].split("?", 1)[0].rstrip("/")
    return f"{host}{path}", host


@router.post("/{quote_id}/delivery-plan/items/{plan_item_id}/publication-link")
async def submit_publication_link(quote_id: int, plan_item_id: str,
                                  payload: PublicationLinkRequest, request: Request):
    """填发布链接 → 只点亮「已发布」 + 建 7/14/30 回查队列。

    🔴 不点亮其余三格:那三格只能由 P5 回查数据写。DB 有 CHECK 兜底,
       db 层的 INSERT 也根本不接受那三个参数(见 gap_plan_db.register_publication)。
    """
    _, bundle, capacity = _load_plan(request, quote_id)
    snapshot = bundle["snapshot"]

    if (payload.expected_authority_generation is not None
            and int(payload.expected_authority_generation) != int(snapshot["authority_generation"])):
        raise _error("plan_item_generation_stale", primary="back_to_plan",
                     secondary="retry_snapshot", status_code=409)

    item = next((i for i in bundle["items"] if i["plan_item_id"] == plan_item_id), None)
    if item is None:
        raise _error("plan_item_not_found", primary="back_to_plan", status_code=404)

    if not _URL_RE.match(payload.publication_url.strip()):
        raise _error("publication_link_invalid", primary="submit_publication_link",
                     secondary="open_publication_link", status_code=422)

    # 幂等:同 key 重复提交只重放结果,不重复建回查
    if payload.idempotency_key:
        existing = gap_plan_db.find_publication_by_idempotency_key(payload.idempotency_key)
        if existing and int(existing["quote_id"]) == int(quote_id):
            return {"success": True, "replayed": True,
                    "evidence": _evidence_block(int(quote_id), bundle["items"])}

    normalized, domain = _normalize_url(payload.publication_url)
    try:
        gap_plan_db.register_publication(
            quote_id=int(quote_id), plan_item_id=plan_item_id,
            publication_url=payload.publication_url.strip(),
            publication_url_normalized=normalized, publication_domain=domain,
            submitted_by=_actor_id(request), idempotency_key=payload.idempotency_key,
        )
    except Exception as exc:                        # noqa: BLE001
        logger.exception("发布链接登记失败 quote_id=%s item=%s", quote_id, plan_item_id)
        raise _error("checkback_temporarily_unavailable", primary="retry_snapshot",
                     keeps_last_snapshot=True, status_code=503) from exc

    out = {"success": True, "replayed": False,
           "evidence": _evidence_block(int(quote_id), bundle["items"])}
    assert_no_internal_leak(out, where="publication-link")
    return out


class DomainAccessRequest(BaseModel):
    accessible: bool
    reason: Optional[str] = Field(default="", max_length=500)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


@router.post("/{quote_id}/delivery-plan/items/{plan_item_id}/domain-access")
async def mark_domain_access(quote_id: int, plan_item_id: str,
                             payload: DomainAccessRequest, request: Request):
    """标记渠道可及/进不去 → 服务端重算替代落点(A3 + A6)。

    🔴 不静默换域名:把核实结论写回 media_outlets(全站受益),然后**重算快照**,
       新快照里这条卡要么换成可进入的落点,要么变成「建议换题」并保留额度。
    """
    _, bundle, _capacity = _load_plan(request, quote_id)
    item = next((i for i in bundle["items"] if i["plan_item_id"] == plan_item_id), None)
    if item is None:
        raise _error("plan_item_not_found", primary="back_to_plan", status_code=404)

    domain = item.get("target_domain") or ""
    if domain:
        gap_plan_db.mark_domain_entry_assessment(
            domain=domain,
            assessment="self_service" if payload.accessible else "unreachable",
            actor_user_id=_actor_id(request),
            note=str(payload.reason or ""),
        )

    # 重算:源事实变了 → data_version 变 → 新代际
    quote = require_quote_access(request, int(quote_id), allow_null=False)
    publications = gap_plan_db.get_publications(int(quote_id))
    capacity = plan_service.compute_capacity(quote, publications)
    fresh = plan_service.build_snapshot(quote, actor_user_id=_actor_id(request))
    presented = plan_service.present_snapshot(
        fresh, capacity=capacity, audience=plan_service.AUDIENCE_AGENT
    )
    presented["evidence"] = _evidence_block(int(quote_id), fresh["items"])

    out = {"success": True, "snapshot": presented}
    assert_no_internal_leak(out, where="domain-access")
    return out


# ════════════════════════════════════════════════════════════════
# 报价评估占位(合同 §13.3 · P4 禁止发写请求)
# ════════════════════════════════════════════════════════════════
# 🔴 这里**刻意不提供任何端点**。P4 的「加入报价评估」是纯前端占位按钮,
#    enabled=false,点击不发网络写请求。真正接线归 P1 篇数容量合同,
#    必须单独过容量、幂等、RBAC、账本、并发严审。
#    锁 test_no_quote_mutation_route_in_this_package 会扫本模块所有路由,
#    出现任何 capacity/quote 写路径即转红。


def gap_plan_route_paths() -> list[str]:
    """给锁用:本模块注册的全部路径。"""
    return [r.path for r in router.routes]  # type: ignore[attr-defined]


def facts_hash(payload: Any) -> str:
    import json as _json
    return hashlib.sha256(
        _json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
