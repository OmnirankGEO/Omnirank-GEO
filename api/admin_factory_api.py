"""
V3.5 W2 · Admin 工厂治理 API(结算审批 + 税务配置 + 价格管理 + 对账快照)

7 endpoints · admin-only · 可见 raw cost / tax 规则 / 全平台成本汇总
- AdminXxxResponse DTO 含 platform_cost_cents / tax_rate_bps / *_fee_bps
- 严禁前端 Agent 页消费这些端点

memory feedback_v35_factory_inventory_model_v6
"""

import json
import logging
import uuid
from typing import Any, Dict, Optional, List
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from db.connection import get_db
from services.agent_inventory_pricing import MAX_AGENT_PURCHASE_AMOUNT_CENTS
from services.agent_revenue import mark_settlement_paid
from schemas.v35_w2_dto import (
    AdminSettlementRequestItem,
    AdminSettlementListResponse,
    AdminSettlementPatchRequest,
    AdminTaxProfileResponse,
    AdminTaxProfilePutRequest,
    AdminSKUItem,
    AdminSKUListResponse,
    AdminSKUPutRequest,
    AdminInventoryAuditSummary,
)
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-V35-W2-AdminAPI")
router = APIRouter(prefix="/api/admin", tags=["V35-W2-Admin 工厂治理"])


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")
    return {
        "user_id": user.get("user_id") or current_user_id(user),
        "username": user.get("username"),
        "is_admin": True,
    }


def _g(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _dict_row(row, keys: list) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {k: row[i] for i, k in enumerate(keys) if i < len(row)}


# ============================================================
# 1-2. 结算审批
# ============================================================

@router.get("/settlements", response_model=AdminSettlementListResponse)
async def admin_settlements_list(
    request: Request,
    status: Optional[str] = Query(None, description="pending / approved / paid / rejected"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    _require_admin(request)
    where = "WHERE 1=1"
    params = []
    if status:
        where += " AND r.status = %s"
        params.append(status)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT COUNT(*) AS c FROM agent_settlement_requests r {where}
        """, params)
        total = int(_g(cur.fetchone(), "c", 0) or 0)

        cur.execute(f"""
            SELECT r.id, r.agent_user_id, u.display_name AS agent_display_name, u.phone AS agent_phone,
                   r.request_amount_cents AS amount_cents,
                   -- [批1D] 财务按 net 打款 · 历史申请(net=0 · 1A 前 ledger 已净额)回落 gross 全额打款
                   COALESCE(NULLIF(r.net_amount_cents, 0), r.request_amount_cents) AS net_amount_cents,
                   COALESCE(r.bank_name, '') || ' / ' || COALESCE(r.account_holder, '') AS payout_method_label,
                   r.bank_account AS payout_account,
                   r.status, r.created_at,
                   (SELECT COUNT(*) FROM agent_settlement_request_items WHERE settlement_request_id = r.id) AS ledger_count
            FROM agent_settlement_requests r
            LEFT JOIN users u ON u.id = r.agent_user_id
            {where}
            ORDER BY r.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        items = []
        for row in cur.fetchall():
            d = dict(row) if isinstance(row, dict) else {}
            gross = int(d["amount_cents"] or 0)
            # net 已在 SQL 层 COALESCE(NULLIF(net,0), gross) 回落(历史 net=0 → gross)· 此处 `or gross` 仅防 None · 总扣费 = gross − net
            net = int(d.get("net_amount_cents") or 0) or gross
            items.append(AdminSettlementRequestItem(
                id=d["id"],
                agent_user_id=d["agent_user_id"],
                agent_display_name=d.get("agent_display_name"),
                agent_phone=d.get("agent_phone"),
                amount_cents=gross,
                net_amount_cents=net,
                total_fee_cents=max(0, gross - net),
                payout_method=d.get("payout_method_label", ""),
                payout_account=d.get("payout_account", ""),
                status=d["status"],
                created_at=d["created_at"],
                ledger_count=int(d.get("ledger_count", 0) or 0),
            ))
    return AdminSettlementListResponse(items=items, total=total)


@router.patch("/settlements/{request_id}")
async def admin_settlements_patch(
    request_id: int, req: AdminSettlementPatchRequest, request: Request
):
    a = _require_admin(request)
    if req.action not in ("approve", "reject", "mark_paid"):
        raise HTTPException(400, "action 必须是 approve / reject / mark_paid")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT status, agent_user_id, request_amount_cents,
                   COALESCE(gateway_fee_cents, 0) + COALESCE(settlement_fee_cents, 0)
                     + COALESCE(tax_cents, 0) AS total_fee_cents,
                   COALESCE(NULLIF(net_amount_cents, 0), request_amount_cents) AS net_amount_cents
            FROM agent_settlement_requests WHERE id = %s FOR UPDATE
        """, (request_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "提现申请不存在")
        cur_status = _g(row, "status", row[0] if not isinstance(row, dict) else None)
        agent_user_id = int(_g(row, "agent_user_id"))
        gross_cents = int(_g(row, "request_amount_cents", 0) or 0)
        total_fee_cents = int(_g(row, "total_fee_cents", 0) or 0)
        net_amount_cents = int(_g(row, "net_amount_cents", gross_cents) or gross_cents)
        event_type = None
        terminal_state = None
        event_facts = None

        if req.action == "approve":
            if cur_status != "pending":
                raise HTTPException(400, f"状态 {cur_status} 不能 approve")
            cur.execute("""
                UPDATE agent_settlement_requests
                SET status = 'approved', approved_by_user_id = %s, approved_at = NOW()
                WHERE id = %s
            """, (a["user_id"], request_id))
            from services.notification_events import NotificationEventType
            event_type = NotificationEventType.AGENT_SETTLEMENT_APPROVED
            terminal_state = "approved"
            event_facts = {
                "business_no": f"SETTLEMENT-{request_id}",
                "amount": f"{gross_cents / 100:.2f} 元",
                "status": "审核通过，等待打款",
            }
        elif req.action == "reject":
            if cur_status not in ("pending", "approved"):
                raise HTTPException(400, f"状态 {cur_status} 不能 reject")
            cur.execute("""
                UPDATE agent_settlement_requests
                SET status = 'rejected',
                    rejected_by_user_id = %s, rejected_at = NOW(),
                    reject_reason = %s
                WHERE id = %s
            """, (a["user_id"], req.reject_reason or "", request_id))
            from services.notification_events import NotificationEventType
            event_type = NotificationEventType.AGENT_SETTLEMENT_REJECTED
            terminal_state = "rejected"
            event_facts = {
                "business_no": f"SETTLEMENT-{request_id}",
                "status": "审核未通过",
                "reason": req.reject_reason or "请在提现页面查看处理建议。",
            }
        elif req.action == "mark_paid":
            if cur_status != "approved":
                raise HTTPException(400, f"状态 {cur_status} 不能 mark_paid · 必须先 approve")
            # 凭证至少一个非空(W2 验收 gate #8)
            if not req.transfer_proof_url and not req.wire_transfer_no:
                raise HTTPException(400, "标记已打款必须填写 transfer_proof_url 或 wire_transfer_no")
            # 调 W1 service mark_settlement_paid · 该函数会处理 ledger items + status
            mark_settlement_paid(
                cur,
                request_id=request_id,
                admin_user_id=a["user_id"],
                transfer_proof_url=req.transfer_proof_url,
                admin_note=req.admin_note,
            )
            # 补 wire_transfer_no(W2 新加列)
            if req.wire_transfer_no:
                cur.execute("""
                    UPDATE agent_settlement_requests
                    SET wire_transfer_no = %s
                    WHERE id = %s
                """, (req.wire_transfer_no, request_id))
            from services.notification_events import NotificationEventType
            event_type = NotificationEventType.AGENT_SETTLEMENT_PAID
            terminal_state = "paid"
            event_facts = {
                "business_no": f"SETTLEMENT-{request_id}",
                "amount": f"{gross_cents / 100:.2f} 元",
                "fee_amount": f"{total_fee_cents / 100:.2f} 元",
                "net_amount": f"{net_amount_cents / 100:.2f} 元",
                "status": "已打款",
            }
        cur.execute("SELECT clock_timestamp() AS event_at")
        event_at_row = cur.fetchone()
        event_facts["occurred_at"] = _g(event_at_row, "event_at").isoformat(timespec="seconds")
        from services.notification_events import RecipientKind
        from services.notification_outbox import enqueue_notification_event
        enqueue_notification_event(
            cur,
            event_type=event_type,
            business_id=str(request_id),
            terminal_state=str(terminal_state),
            recipient_user_id=agent_user_id,
            recipient_kind=RecipientKind.AGENT,
            facts=event_facts,
        )
        conn.commit()
    return {"success": True, "action": req.action, "request_id": request_id}


# ============================================================
# 3-4. 税务配置
# ============================================================

@router.get("/tax-profiles/{agent_user_id}", response_model=AdminTaxProfileResponse)
async def admin_tax_profile_get(agent_user_id: int, request: Request):
    _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT agent_user_id, entity_type, default_tax_rate_bps, default_tax_mode,
                   tax_id, invoice_capability, notes, updated_at
            FROM agent_tax_profiles WHERE agent_user_id = %s
        """, (agent_user_id,))
        row = cur.fetchone()
    if not row:
        # 返回空 default profile · 不报 404 让 admin UI 可填充
        return AdminTaxProfileResponse(agent_user_id=agent_user_id)
    d = dict(row) if isinstance(row, dict) else _dict_row(row, [
        "agent_user_id","entity_type","default_tax_rate_bps","default_tax_mode",
        "tax_id","invoice_capability","notes","updated_at",
    ])
    return AdminTaxProfileResponse(**d)


@router.put("/tax-profiles/{agent_user_id}")
async def admin_tax_profile_put(
    agent_user_id: int, req: AdminTaxProfilePutRequest, request: Request
):
    _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO agent_tax_profiles
                (agent_user_id, entity_type, default_tax_rate_bps, default_tax_mode,
                 tax_id, invoice_capability, notes, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (agent_user_id) DO UPDATE SET
                entity_type = COALESCE(EXCLUDED.entity_type, agent_tax_profiles.entity_type),
                default_tax_rate_bps = COALESCE(EXCLUDED.default_tax_rate_bps, agent_tax_profiles.default_tax_rate_bps),
                default_tax_mode = COALESCE(EXCLUDED.default_tax_mode, agent_tax_profiles.default_tax_mode),
                tax_id = COALESCE(EXCLUDED.tax_id, agent_tax_profiles.tax_id),
                invoice_capability = COALESCE(EXCLUDED.invoice_capability, agent_tax_profiles.invoice_capability),
                notes = COALESCE(EXCLUDED.notes, agent_tax_profiles.notes),
                updated_at = NOW()
        """, (
            agent_user_id, req.entity_type, req.default_tax_rate_bps,
            req.default_tax_mode, req.tax_id, req.invoice_capability, req.notes,
        ))
        conn.commit()
    return {"success": True, "agent_user_id": agent_user_id}


# ============================================================
# 5-6. 价格管理(admin 可见全部 raw cost)
# ============================================================

@router.get("/pricing/skus", response_model=AdminSKUListResponse)
async def admin_pricing_skus(
    request: Request,
    category: Optional[str] = None,
    is_active: Optional[bool] = None,
):
    _require_admin(request)
    where_clauses = []
    params = []
    if category:
        where_clauses.append("sku_type = %s")
        params.append(category)
    if is_active is not None:
        where_clauses.append("is_active = %s")
        params.append(is_active)
    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with get_db() as conn:
        cur = conn.cursor()
        # W1 schema: sku_templates 无 platform_cost_cents 列(在 feature_pricing/mhz_media)
        # admin SKU 列表仅展示 SKU 模板本身 · raw cost 由 /admin/feature-pricing 单独管理(W4 整合)
        cur.execute(f"""
            SELECT id AS sku_template_id, template_code AS sku_key, sku_type AS category,
                   default_name AS display_name, default_subtitle AS subtitle,
                   points_granted, wholesale_cents,
                   suggested_retail_cents AS retail_cents,
                   is_active
            FROM sku_templates
            {where}
            ORDER BY sku_type, id
        """, params)
        items = []
        for row in cur.fetchall():
            d = dict(row) if isinstance(row, dict) else {}
            items.append(AdminSKUItem(
                sku_template_id=d["sku_template_id"],
                sku_key=d.get("sku_key", ""),
                category=d.get("category", ""),
                display_name=d.get("display_name", ""),
                subtitle=d.get("subtitle"),
                points_granted=int(d.get("points_granted", 0) or 0),
                wholesale_cents=int(d.get("wholesale_cents", 0) or 0),
                retail_cents=int(d.get("retail_cents", 0) or 0),
                is_active=bool(d.get("is_active", True)),
            ))
    return AdminSKUListResponse(items=items)


@router.put("/pricing/skus/{sku_template_id}")
async def admin_pricing_sku_put(
    sku_template_id: int, req: AdminSKUPutRequest, request: Request
):
    """
    Admin SKU 编辑 · [boss r2 修正]:
    SKU 量纲铁律(points × 200 == cents × 325)必须按**最终值**校验
    不仅是本次 patch 同时带两个字段才校验 · 否则直接 API 单独改一个字段可绕过
    """
    a = _require_admin(request)
    # W1 真字段:suggested_retail_cents(不是 default_retail_cents)· sku_templates 无 platform_cost_cents
    field_map = {
        "points_granted": req.points_granted,
        "wholesale_cents": req.wholesale_cents,
        "suggested_retail_cents": req.retail_cents,
        "is_active": req.is_active,
        "default_name": req.display_name,
        "default_subtitle": req.subtitle,
    }
    patch = {col: val for col, val in field_map.items() if val is not None}
    if not patch:
        raise HTTPException(400, "至少改一个字段")

    with get_db() as conn:
        cur = conn.cursor()
        # 加 FOR UPDATE 锁 + 读当前值 · 合并 patch · 按最终值校验量纲
        cur.execute("""
            SELECT points_granted, wholesale_cents
            FROM sku_templates WHERE id = %s FOR UPDATE
        """, (sku_template_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"sku_template {sku_template_id} 不存在")
        cur_points = row["points_granted"] if isinstance(row, dict) else row[0]
        cur_wholesale = row["wholesale_cents"] if isinstance(row, dict) else row[1]

        final_points = patch.get("points_granted", cur_points)
        final_wholesale = patch.get("wholesale_cents", cur_wholesale)

        # SKU 量纲铁律: points × numer == wholesale_cents × denom(按最终值校验)
        # [D2 动态化] numer/denom 从 pricing_config 读(默认 225/325=9折·后台可调出厂折扣时同步放松)
        # 防直接 API 单独改一个字段绕过
        from config.pricing_config import get_wholesale_ratio
        _numer, _denom = get_wholesale_ratio()
        if not _is_standard_price(final_points, final_wholesale, _numer, _denom):
            # 🔴 [BH-015b] 报错必须带**出路**:改之前它只说等式不成立,还要求
            #    「同时改两个字段保持等式」—— 而 points 不是 13 倍数时那是**做不到**的,
            #    admin 会一直改一直被拒,没有任何提示告诉他该填什么。
            from services.agent_pricing import calc_factory_cents
            _want = calc_factory_cents(int(final_points), _numer, _denom)
            raise HTTPException(
                400,
                f"SKU 进货价不符合当前出厂折扣({_numer}/{_denom}):"
                f"points_granted={final_points} 对应的 wholesale_cents 应为 {_want} 分,"
                f"当前填的是 {final_wholesale} 分。"
                f"把 wholesale_cents 改成 {_want} 即可保存;"
                f"如果这是活动包/人工谈价,请改用 POST /pricing/skus/{{id}}/save(软校验·只标记不阻断)。"
            )

        updates = []
        params = []
        for col, val in patch.items():
            updates.append(f"{col} = %s")
            params.append(val)
        params.append(sku_template_id)
        cur.execute(f"""
            UPDATE sku_templates SET {', '.join(updates)}, updated_at = NOW()
            WHERE id = %s
        """, params)
        conn.commit()
    logger.info(
        f"[admin_pricing] admin={a['user_id']} 改 sku={sku_template_id} "
        f"fields={list(patch.keys())} final_points={final_points} final_wholesale={final_wholesale}"
    )
    return {"success": True, "sku_template_id": sku_template_id}


# ============================================================
# 6a. 算力定价中心(合并「算力包管理」+「定价系数配置」· admin-only)
#     聚合读 GET /pricing/center · 软校验增/改/复制/上下架/重算
#     ⚠️ 资金铁律:仅 UPDATE/INSERT sku_templates · 绝不回写 recharge_orders(历史订单靠 pricing_snapshot 隔离)
#     ⚠️ 量纲:进货价复用 services.agent_pricing.calc_factory_cents(points×numer/denom ceil)· is_standard 精确比对
# ============================================================

# sku_type 白名单(软校验·不在白名单 raise)
_VALID_SKU_TYPES = {"credit_pack", "scenario_pack", "addon_pack"}


def _discount_to_ratio(discount: float):
    """折扣(如 0.9=9折) → 出厂量纲 (numer, denom)。
    denom = points_per_yuan × 100(1 元 = points_per_yuan 积分 · 价以分计)
    numer = round(discount × 10000)
    验证:p=130,discount=0.9 → denom=13000,numer=9000(等价 225/325·量纲对 130 倍数精确)。
    """
    from config.pricing_config import get_pricing_config
    points_per_yuan = int(get_pricing_config().get("points_per_yuan", 130))
    denom = points_per_yuan * 100
    numer = int(round(discount * 10000))
    return numer, denom


def _ratio_to_discount(numer: int, denom: int) -> float:
    """出厂量纲 (numer, denom) → 折扣小数(4 位)。
    验证:225/325,p=130 → round(225×130/(325×100),4)=0.9 ✅
    """
    from config.pricing_config import get_pricing_config
    points_per_yuan = int(get_pricing_config().get("points_per_yuan", 130))
    if denom <= 0:
        return 0.0
    return round(numer * points_per_yuan / (denom * 100), 4)



# ══ BH-015a · 批量重算的「为什么没动」必须说得出来 ═══════════════════
# 🔴 [2026-09-05] `POST /pricing/skus/recalc-all` 在**默认参数**下
#    (`include_non_standard=False`)`updated_count` **结构性恒为 0** ——
#    这不是「今天恰好都已是最新价」,是两个条件互斥:
#
#      能走到 UPDATE 的行必须同时满足
#        (a) points × numer == old × denom        ← 否则被当活动包/人工谈价跳过
#        (b) ceil(points × numer / denom) != old  ← 否则 `if new != old` 不成立
#      而 (a) 说明 points×numer/denom **恰为整数** old ⇒ ceil 就等于 old ⇒ (b) 恒假。
#
#    实测:7 组系数 × points∈[0,4000) 里所有满足 (a) 的 6256 个样本,
#    重算后会变的 **0 个**。
#
#    更糟的是这个端点存在的**意义**正是「系数改了,拿新系数重算旧价」,
#    而按新系数衡量,所有按旧系数定的价都是 non_standard ⇒ 全被跳过。
#    「需要更新」与「是标准价」在同一套系数下互斥。
#
#    为什么不改成「真正更新」:`sku_templates` 生产 schema 13 列里
#    **没有**任何标记能区分「人工谈价」与「按旧系数算出的价」
#    (已查 2026-08-19 生产快照)。加列也回填不了历史行 —— 没有可依据的数据。
#    所以这里做的是**如实说出发生了什么**,而不是假装动过。



# ══ BH-015b · 「这个进货价标不标准」只许有一个判法 ═══════════════════
# 🔴 [2026-09-05] 改之前这个谓词在本文件里**手写了五份**,五份都是同一个错法:
#
#      is_standard = (points × numer == wholesale × denom)     ← 精确等式
#
#    而定价侧算的是 `calc_factory_cents = ceil(points × numer / denom)`。
#    两者只在 `denom/gcd(numer,denom)` 整除 points 时一致 ——
#    默认 225/325 ⇒ **points 必须是 13 的倍数**,否则不管 wholesale 填什么整数,
#    等式都不可能成立:系统自己算出来的价会被判成「人工谈价」。
#
#    最狠的是 `admin_pricing_sku_put`(PUT /pricing/skus/{id}):它不是软标记,
#    是 `raise HTTPException(400)` **硬阻断** ⇒ points 不是 13 倍数的 SKU
#    通过这个端点**永远改不动**,而且报错还让 admin「同时改两个字段保持等式」——
#    一个做不到的要求。
#
#    改成与规则输出比对,语义正好是注释一直想表达的那句:
#    non_standard = 「这个价**不是规则算出来的**」。
#
#    🔴 为什么抽成函数而不是原地改五次:五份手写副本正是这个 bug 铺开到五处的原因。
#       同一个谓词写五处,必有一处在下次改动时被漏掉。


def _is_standard_price(points, wholesale, numer: int, denom: int) -> bool:
    """进货价是否**就是当前出厂规则算出来的那个数**。

    非标(`not _is_standard_price(...)`)= 活动包 / 人工谈价 —— 即「这个价不是规则算的」。
    """
    from services.agent_pricing import calc_factory_cents
    return int(wholesale) == calc_factory_cents(int(points), numer, denom)


def _recalc_summary(items, numer: int, denom: int,
                    include_non_standard: bool, only_active: bool) -> dict:
    """把一次批量重算的结果讲清楚。

    `no_op_reason` 只在 `updated_count == 0` 时非空 —— 它回答的是
    「为什么一行都没动」,而不是复述「动了 0 行」。
    """
    updated_count = sum(1 for it in items if not it.get("skipped") and it["old"] != it["new"])
    skipped_count = sum(1 for it in items if it.get("skipped"))
    # 被跳过的行里,进货价**确实**与当前出厂规则不同的有几个
    skipped_would_change = sum(1 for it in items
                               if it.get("skipped") and it.get("would_change"))

    no_op_reason = None
    if updated_count == 0:
        scope = "已上架的" if only_active else ""
        if not items:
            no_op_reason = f"没有符合筛选条件的{scope}算力包,本次没有可处理的对象。"
        elif not include_non_standard:
            no_op_reason = (
                f"默认模式下不会有任何一行被更新,这是**规则本身决定的**,"
                f"不是「今天恰好都已是最新价」:没被跳过的行,进货价按定义就等于"
                f"按 {numer}/{denom} 算出来的值,再算一次还是同一个数。"
                f"本次 {len(items)} 个{scope}算力包中,{skipped_count} 个被当作"
                f"活动包/人工谈价跳过"
                + (f",其中 {skipped_would_change} 个的进货价确实和当前出厂规则不一样 —— "
                   f"要重算这些请勾选「强制覆盖特殊定价包」。"
                   if skipped_would_change else "。")
            )
        else:
            no_op_reason = (
                f"{len(items)} 个{scope}算力包的进货价都已等于按 {numer}/{denom} "
                f"算出的值,没有需要更新的。")

    return {"updated_count": updated_count, "skipped_count": skipped_count,
            "skipped_would_change": skipped_would_change, "no_op_reason": no_op_reason}


def _factory_and_standard(points: int, numer: int, denom: int, explicit_wholesale):
    """返回 (wholesale_cents, is_standard)。
    explicit_wholesale None → 按规则算(必标准);否则用显式值 · is_standard 走精确量纲比对。
    """
    from services.agent_pricing import calc_factory_cents
    rule = calc_factory_cents(int(points), numer, denom)
    if explicit_wholesale is None:
        return rule, True
    is_standard = _is_standard_price(points, explicit_wholesale, numer, denom)
    return int(explicit_wholesale), is_standard


class AdminSKUCenterCreateRequest(BaseModel):
    """新增算力包(默认不上架·软校验不阻止保存)。"""
    sku_type: str
    display_name: str
    subtitle: Optional[str] = None
    capability_pitch: Optional[str] = None  # 写 default_capability_pitch(适合场景)
    points_granted: int
    retail_cents: int
    wholesale_cents: Optional[int] = None    # 不填按规则算
    is_active: bool = False


class AdminSKUCenterSaveRequest(BaseModel):
    """编辑算力包(软校验·non_standard 只标记不阻止·新页面抽屉用)。"""
    display_name: Optional[str] = None
    subtitle: Optional[str] = None
    capability_pitch: Optional[str] = None
    points_granted: Optional[int] = None
    wholesale_cents: Optional[int] = None
    retail_cents: Optional[int] = None
    is_active: Optional[bool] = None


class AdminSKUToggleActiveRequest(BaseModel):
    is_active: bool


class AdminSKURecalcAllRequest(BaseModel):
    only_active: bool = False
    # [复审返修2] 默认跳过活动包/人工谈价包(non_standard)· 前端覆盖须二次确认才传 true
    include_non_standard: bool = False


class AdminSyncAgentPricesRequest(BaseModel):
    """Legacy request shape retained only for the disabled 410 route."""

    dry_run: bool = True
    only_loss: bool = True
    agent_user_id: Optional[int] = None


@router.get("/pricing/center")
async def admin_pricing_center(request: Request):
    """聚合读:默认出厂规则 + 全部算力包 + 概览统计(给前端「算力定价中心」一次拿全)。admin-only。"""
    _require_admin(request)
    from config.pricing_config import get_agent_purchase_catalog_version, get_pricing_config

    cfg = get_pricing_config()
    catalog_version = get_agent_purchase_catalog_version(cfg)
    numer = int(cfg.get("wholesale_numer", 225))
    denom = int(cfg.get("wholesale_denom", 325))
    points_per_yuan = int(cfg.get("points_per_yuan", 130))
    bonus_rate = float(cfg.get("agent_purchase_bonus_rate", 0.05))
    discount = _ratio_to_discount(numer, denom)
    # "1 元进货 ≈ X 算力":折扣越低同样的钱买到越多算力
    points_after_discount = round(points_per_yuan / discount, 1) if discount > 0 else 0.0

    packages = []
    active_wholesale_vals = []
    active_retail_vals = []
    active_count = 0
    non_standard_count = 0

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, template_code, sku_type, default_name, default_subtitle,
                   default_capability_pitch, points_granted, wholesale_cents,
                   suggested_retail_cents, is_active
            FROM sku_templates
            ORDER BY sku_type, id
        """)
        for row in cur.fetchall():
            d = dict(row) if isinstance(row, dict) else {}
            pts = int(d.get("points_granted", 0) or 0)
            wholesale = int(d.get("wholesale_cents", 0) or 0)
            retail = int(d.get("suggested_retail_cents", 0) or 0)
            is_active = bool(d.get("is_active", False))
            # 精确量纲:non_standard = 进货价不符合当前出厂规则
            non_standard = not _is_standard_price(pts, wholesale, numer, denom)
            if non_standard:
                non_standard_count += 1
            if is_active:
                active_count += 1
                if wholesale > 0:
                    active_wholesale_vals.append(wholesale)
                if retail > 0:
                    active_retail_vals.append(retail)
            packages.append({
                "sku_template_id": d.get("id"),
                "sku_key": d.get("template_code", ""),
                "sku_type": d.get("sku_type", ""),
                "display_name": d.get("default_name", ""),
                "subtitle": d.get("default_subtitle"),
                "capability_pitch": d.get("default_capability_pitch"),  # 适合场景
                "points_granted": pts,
                "wholesale_cents": wholesale,
                "retail_cents": retail,
                "margin_cents": retail - wholesale,
                "is_active": is_active,
                "non_standard": non_standard,
            })

    overview = {
        "active_count": active_count,
        "min_wholesale_cents": min(active_wholesale_vals) if active_wholesale_vals else 0,
        "retail_range": [
            min(active_retail_vals) if active_retail_vals else 0,
            max(active_retail_vals) if active_retail_vals else 0,
        ],
        "non_standard_count": non_standard_count,
    }

    from config.pricing_config import get_pricing_env_override
    env_override = get_pricing_env_override() or {}
    rule_env_keys = sorted({"wholesale_numer", "wholesale_denom", "agent_purchase_bonus_rate"}.intersection(env_override))
    return {
        "success": True,
        "catalog_version": catalog_version,
        "default_rule": {
            "points_per_yuan": points_per_yuan,
            "wholesale_discount": discount,
            "agent_purchase_bonus_rate": bonus_rate,
            "points_per_yuan_after_discount": points_after_discount,  # 1 元进货 ≈ X 算力
        },
        "packages": packages,
        "overview": overview,
        "environment_override": {
            "active": bool(rule_env_keys),
            "keys": rule_env_keys,
            "message": "环境配置覆盖中，默认进货规则只读" if rule_env_keys else "",
        },
    }


@router.post("/pricing/skus")
async def admin_pricing_sku_create(req: AdminSKUCenterCreateRequest, request: Request):
    """新增算力包(默认不上架·软校验)。non_standard 只标记不阻止保存。admin-only。
    ⚠️ 只 INSERT sku_templates · 绝不碰 recharge_orders。"""
    import shortuuid
    a = _require_admin(request)
    if req.sku_type not in _VALID_SKU_TYPES:
        raise HTTPException(400, f"sku_type 必须是 {sorted(_VALID_SKU_TYPES)} 之一")
    # [复审返修2] display_name 空值兜底(防全空格脏数据)· 存库用 strip 后
    _name = (req.display_name or "").strip()
    if not _name:
        raise HTTPException(400, "display_name 不能为空")
    if req.points_granted is None or int(req.points_granted) < 0:
        raise HTTPException(400, "points_granted 须 >= 0")
    if req.retail_cents is None or int(req.retail_cents) < 0:
        raise HTTPException(400, "retail_cents 须 >= 0")
    # [复审返修1] 显式进货价也必须非负(后端兜底·不只靠前端挡)
    if req.wholesale_cents is not None and int(req.wholesale_cents) < 0:
        raise HTTPException(400, "wholesale_cents 须 >= 0")

    from config.pricing_config import get_wholesale_ratio
    numer, denom = get_wholesale_ratio()
    wholesale_cents, is_standard = _factory_and_standard(
        int(req.points_granted), numer, denom, req.wholesale_cents
    )
    template_code = f"adm_{req.sku_type}_{shortuuid.uuid()[:8]}"

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sku_templates
                    (template_code, sku_type, default_name, default_subtitle,
                     default_capability_pitch, points_granted, wholesale_cents,
                     suggested_retail_cents, is_active, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                RETURNING id
            """, (
                template_code, req.sku_type, _name, req.subtitle,
                req.capability_pitch, int(req.points_granted), int(wholesale_cents),
                int(req.retail_cents), bool(req.is_active),
            ))
            row = cur.fetchone()
            new_id = row["id"] if isinstance(row, dict) else row[0]
            conn.commit()
    except Exception as exc:
        # [对抗审计 P1] template_code UNIQUE 冲突(shortuuid 碰撞·~1/10^14 极罕见)→ 409 友好报错
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(409, "算力包编号生成冲突,请重试")
        raise
    logger.info(
        f"[算力定价中心] admin={a['user_id']} 新增算力包 id={new_id} code={template_code} "
        f"type={req.sku_type} points={req.points_granted} wholesale={wholesale_cents} "
        f"retail={req.retail_cents} active={req.is_active} non_standard={not is_standard}"
    )
    return {"success": True, "sku_template_id": new_id, "non_standard": not is_standard}


@router.post("/pricing/skus/{sku_template_id}/save")
async def admin_pricing_sku_save(
    sku_template_id: int, req: AdminSKUCenterSaveRequest, request: Request
):
    """编辑算力包(软校验·不阻止保存·新页面抽屉用)。
    non_standard(进货价不符当前出厂规则)只返回标记·正常 UPDATE(老板第六点:不阻止保存·只标记)。
    现有硬量纲校验 PUT /pricing/skus/{id} 保留不动。admin-only。
    ⚠️ 只 UPDATE sku_templates · 绝不碰 recharge_orders。"""
    a = _require_admin(request)
    # [对抗审计 P0] save 与 create 同口径负数校验(防写入无效资金数据)
    if req.points_granted is not None and int(req.points_granted) < 0:
        raise HTTPException(400, "points_granted 须 >= 0")
    if req.retail_cents is not None and int(req.retail_cents) < 0:
        raise HTTPException(400, "retail_cents 须 >= 0")
    if req.wholesale_cents is not None and int(req.wholesale_cents) < 0:
        raise HTTPException(400, "wholesale_cents 须 >= 0")
    # [复审返修2] display_name 传了但 strip 后为空 → 400(防全空格脏数据)· 存库用 strip
    _name = req.display_name.strip() if req.display_name is not None else None
    if req.display_name is not None and not _name:
        raise HTTPException(400, "display_name 不能为空")
    field_map = {
        "default_name": _name,
        "default_subtitle": req.subtitle,
        "default_capability_pitch": req.capability_pitch,
        "points_granted": req.points_granted,
        "wholesale_cents": req.wholesale_cents,
        "suggested_retail_cents": req.retail_cents,
        "is_active": req.is_active,
    }
    patch = {col: val for col, val in field_map.items() if val is not None}
    if not patch:
        raise HTTPException(400, "至少改一个字段")
    from config.pricing_config import get_wholesale_ratio
    numer, denom = get_wholesale_ratio()

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT points_granted, wholesale_cents
            FROM sku_templates WHERE id = %s FOR UPDATE
        """, (sku_template_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"sku_template {sku_template_id} 不存在")
        cur_points = row["points_granted"] if isinstance(row, dict) else row[0]
        cur_wholesale = row["wholesale_cents"] if isinstance(row, dict) else row[1]

        final_points = int(patch.get("points_granted", cur_points) or 0)
        final_wholesale = int(patch.get("wholesale_cents", cur_wholesale) or 0)
        # 软校验:non_standard 只标记 · 不 raise(老板第六点)
        is_standard = _is_standard_price(final_points, final_wholesale, numer, denom)

        updates = []
        params = []
        for col, val in patch.items():
            updates.append(f"{col} = %s")
            params.append(val)
        params.append(sku_template_id)
        cur.execute(f"""
            UPDATE sku_templates SET {', '.join(updates)}, updated_at = NOW()
            WHERE id = %s
        """, params)
        conn.commit()
    logger.info(
        f"[算力定价中心] admin={a['user_id']} 保存算力包 id={sku_template_id} "
        f"fields={list(patch.keys())} final_points={final_points} "
        f"final_wholesale={final_wholesale} non_standard={not is_standard}"
    )
    return {"success": True, "sku_template_id": sku_template_id, "non_standard": not is_standard}


@router.post("/pricing/skus/{sku_template_id}/copy")
async def admin_pricing_sku_copy(sku_template_id: int, request: Request):
    """复制算力包(新 template_code · 名字 +(副本)· 默认不上架 · 其余继承)。admin-only。
    ⚠️ 只 INSERT sku_templates · 绝不碰 recharge_orders。"""
    import shortuuid
    a = _require_admin(request)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT sku_type, default_name, default_subtitle, default_capability_pitch,
                       points_granted, wholesale_cents, suggested_retail_cents, recommended_use_jsonb
                FROM sku_templates WHERE id = %s
            """, (sku_template_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"sku_template {sku_template_id} 不存在")
            d = dict(row) if isinstance(row, dict) else {}
            new_code = f"adm_{d.get('sku_type', 'credit_pack')}_{shortuuid.uuid()[:8]}"
            new_name = f"{d.get('default_name', '') or ''}(副本)"
            cur.execute("""
                INSERT INTO sku_templates
                    (template_code, sku_type, default_name, default_subtitle,
                     default_capability_pitch, points_granted, wholesale_cents,
                     suggested_retail_cents, recommended_use_jsonb, is_active, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, NOW(), NOW())
                RETURNING id
            """, (
                new_code, d.get("sku_type"), new_name, d.get("default_subtitle"),
                d.get("default_capability_pitch"), int(d.get("points_granted", 0) or 0),
                int(d.get("wholesale_cents", 0) or 0), int(d.get("suggested_retail_cents", 0) or 0),
                d.get("recommended_use_jsonb"),
            ))
            new_row = cur.fetchone()
            new_id = new_row["id"] if isinstance(new_row, dict) else new_row[0]
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        # [对抗审计 P1] template_code UNIQUE 冲突(shortuuid 碰撞·~1/10^14 极罕见)→ 409 友好报错
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(409, "算力包编号生成冲突,请重试")
        raise
    logger.info(f"[算力定价中心] admin={a['user_id']} 复制算力包 src={sku_template_id} → new={new_id} code={new_code}")
    return {"success": True, "sku_template_id": new_id}


@router.post("/pricing/skus/{sku_template_id}/toggle-active")
async def admin_pricing_sku_toggle_active(
    sku_template_id: int, req: AdminSKUToggleActiveRequest, request: Request
):
    """上架 / 下架算力包。admin-only。
    ⚠️ 只 UPDATE sku_templates.is_active · 绝不碰 recharge_orders。"""
    a = _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE sku_templates SET is_active = %s, updated_at = NOW()
            WHERE id = %s
        """, (bool(req.is_active), sku_template_id))
        if cur.rowcount == 0:
            raise HTTPException(404, f"sku_template {sku_template_id} 不存在")
        conn.commit()
    logger.info(f"[算力定价中心] admin={a['user_id']} 算力包 id={sku_template_id} is_active={req.is_active}")
    return {"success": True, "is_active": bool(req.is_active)}


@router.post("/pricing/skus/{sku_template_id}/recalc")
async def admin_pricing_sku_recalc(sku_template_id: int, request: Request):
    """单个算力包按当前出厂规则重算进货价(wholesale_cents)。admin-only。
    ⚠️ 只 UPDATE sku_templates · 绝不碰 recharge_orders(已 paid 历史订单永不重算·靠 pricing_snapshot 隔离)。"""
    from config.pricing_config import get_wholesale_ratio
    from services.agent_pricing import calc_factory_cents
    a = _require_admin(request)
    numer, denom = get_wholesale_ratio()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT points_granted FROM sku_templates WHERE id = %s FOR UPDATE
        """, (sku_template_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"sku_template {sku_template_id} 不存在")
        points = int((row["points_granted"] if isinstance(row, dict) else row[0]) or 0)
        new_wholesale = calc_factory_cents(points, numer, denom)
        cur.execute("""
            UPDATE sku_templates SET wholesale_cents = %s, updated_at = NOW()
            WHERE id = %s
        """, (int(new_wholesale), sku_template_id))
        conn.commit()
    logger.info(f"[算力定价中心] admin={a['user_id']} 重算算力包 id={sku_template_id} → wholesale={new_wholesale}")
    return {"success": True, "wholesale_cents": int(new_wholesale)}


@router.post("/pricing/skus/recalc-all")
async def admin_pricing_sku_recalc_all(req: AdminSKURecalcAllRequest, request: Request):
    """批量按当前出厂规则重算所有(或仅上架)算力包进货价。admin-only。
    ⚠️ 只 UPDATE sku_templates · 绝不碰历史订单 recharge_orders。"""
    from config.pricing_config import get_wholesale_ratio
    from services.agent_pricing import calc_factory_cents
    a = _require_admin(request)
    numer, denom = get_wholesale_ratio()
    items = []
    with get_db() as conn:
        cur = conn.cursor()
        # [对抗审计 P0] 参数化(only_active 是 bool 不可注入·但避免 f-string 拼 SQL 坏模式)
        if req.only_active:
            cur.execute("""
                SELECT id, points_granted, wholesale_cents
                FROM sku_templates WHERE is_active = %s FOR UPDATE
            """, (True,))
        else:
            cur.execute("""
                SELECT id, points_granted, wholesale_cents
                FROM sku_templates FOR UPDATE
            """)
        rows = cur.fetchall()
        for row in rows:
            d = dict(row) if isinstance(row, dict) else dict(zip(["id", "points_granted", "wholesale_cents"], row))
            sku_id = d.get("id")
            points = int(d.get("points_granted") or 0)
            old_wholesale = int(d.get("wholesale_cents") or 0)
            # [复审返修2] non_standard(活动包/人工谈价)默认跳过·不被批量重算覆盖
            #   除非显式 include_non_standard=true(前端需二次确认)
            is_standard = _is_standard_price(points, old_wholesale, numer, denom)
            rule_cents = calc_factory_cents(points, numer, denom)
            if not is_standard and not req.include_non_standard:
                # 🔴 [BH-015a] 跳过的行也要算出规则值并带上 `would_change` ——
                #    否则「跳过了 N 个」这句话里,admin 看不出哪些是真的需要处理。
                items.append({"id": sku_id, "old": old_wholesale, "new": old_wholesale,
                              "skipped": True, "rule_cents": int(rule_cents),
                              "would_change": int(rule_cents) != old_wholesale})
                continue
            new_wholesale = rule_cents
            if new_wholesale != old_wholesale:
                cur.execute("""
                    UPDATE sku_templates SET wholesale_cents = %s, updated_at = NOW()
                    WHERE id = %s
                """, (int(new_wholesale), sku_id))
            items.append({"id": sku_id, "old": old_wholesale, "new": int(new_wholesale)})
        conn.commit()
    s = _recalc_summary(items, numer, denom, req.include_non_standard, req.only_active)
    updated_count = s["updated_count"]
    logger.info(
        f"[算力定价中心] admin={a['user_id']} 批量重算算力包 only_active={req.only_active} "
        f"include_non_standard={req.include_non_standard} total={len(items)} "
        f"updated={updated_count} skipped={s['skipped_count']} "
        f"skipped_would_change={s['skipped_would_change']} no_op={bool(s['no_op_reason'])}"
    )
    return {
        "success": True,
        "updated_count": updated_count,
        "skipped_count": s["skipped_count"],
        # 🔴 [BH-015a] 一行都没动时必须说得出为什么 —— 静默返回 0 会让 admin
        #    以为「已经是最新的了」,而真相可能是「这个模式下永远不会动」。
        "skipped_would_change": s["skipped_would_change"],
        "no_op_reason": s["no_op_reason"],
        "items": items,
        "notice": (
            "已重算平台进货价；服务商零售价不自动跟随平台建议价。"
            "成本或渠道关系变化后，服务商必须按当前有效成本重新定价并发布。"
        ),
    }


@router.post("/pricing/skus/sync-agent-prices")
async def admin_pricing_sync_agent_prices(req: AdminSyncAgentPricesRequest, request: Request):
    """Disabled: platform suggested prices are not a writable retail source."""
    _require_admin(request)
    raise HTTPException(
        410,
        detail={
            "code": "PLATFORM_SUGGESTED_PRICE_SYNC_DISABLED",
            "message": "平台建议售价不再覆盖服务商零售目录；服务商须按当前有效成本独立定价",
        },
    )



# ============================================================
# 6b. 定价系数动态配置(全局 + per-agent · D5 · admin-only)
#     docs/AI-CONTEXT/PRICING_COEFFICIENT_DYNAMIC_PLAN_2026-06-04.md
# ============================================================

class GlobalPricingConfigRequest(BaseModel):
    """只传要改的字段(其余保留)· None 不动。
    [FIX·Codex#2/数据包] 仅暴露【真正接入生效】的系数。移除 4 个"错觉字段"(可改但不全链路生效·避免后台误判):
      - points_per_yuan(130 散 9 文件未全改·跨 A2 返利轮)
      - quote_markup_default(系统默认走 settings_manager.quote_markup_ratio·非此·admin 用 /api/settings 改)
      - media_markup_default(媒体真实生效走 mhz_config·admin 用 /api/meijiehezi/admin/config/markup 改)
      - trial_bonus_points(受 V3_3_1_DISABLE_INSTANT_TRIAL_BONUS 控发放+未接 grant_trial_bonus·改了不生效)
    """
    model_config = ConfigDict(extra="forbid")
    expected_catalog_version: str = Field(..., pattern=r"^agent-purchase-v[1-9][0-9]*$")
    wholesale_numer: Optional[int] = None
    wholesale_denom: Optional[int] = None
    wholesale_discount: Optional[float] = None  # 友好字段:折扣%(如 0.8=8折)· 与 numer/denom 二选一(都给以 discount 优先)
    agent_purchase_bonus_rate: Optional[float] = None
    # [FIX·复核P1-2] 移除复杂数组(recharge_bonus_tiers/recharge_packages/agent_purchase_options):
    #   API 可写但无结构校验·写坏会致充值/进货页运行时崩或算错 → 留后续专门编辑器(完整 validator+单测)再开放。
    #   admin 暂只改标量(出厂折扣/进货赠送率·均有护栏);复杂档位结构走代码默认 / 紧急 env PRICING_CONFIG。


class InventoryCatalogItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: Optional[str] = Field(default=None, max_length=68)
    amount_cents: int = Field(..., gt=0, le=MAX_AGENT_PURCHASE_AMOUNT_CENTS)
    is_enabled: bool
    sort_order: int = Field(..., ge=0, le=1_000_000)


class InventoryCatalogPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_catalog_version: str = Field(..., pattern=r"^agent-purchase-v[1-9][0-9]*$")
    options: List[InventoryCatalogItemRequest] = Field(..., min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_catalog(self):
        enabled_count = sum(1 for item in self.options if item.is_enabled)
        if enabled_count == 0:
            raise ValueError("至少保留一个启用档位")
        if enabled_count > 3:
            raise ValueError("最多启用 3 个固定进货档位；服务商仍可使用自由金额进货")
        amounts = [item.amount_cents for item in self.options]
        if len(amounts) != len(set(amounts)):
            raise ValueError("所有档位的进货金额不得重复")
        ids = [item.option_id for item in self.options if item.option_id]
        if len(ids) != len(set(ids)):
            raise ValueError("option_id 不得重复")
        return self


_CATALOG_ENV_KEYS = {
    "agent_purchase_options",
    "wholesale_numer", "wholesale_denom", "agent_purchase_bonus_rate",
    "agent_tier_config", "founding",
}


def _parse_config_value(row) -> Dict[str, Any]:
    if not row:
        return {}
    raw = row.get("value") if isinstance(row, dict) else row[0]
    if isinstance(raw, str):
        raw = json.loads(raw or "{}")
    return dict(raw) if isinstance(raw, dict) else {}


def _catalog_env_status() -> Dict[str, Any]:
    from config.pricing_config import get_pricing_env_override
    env = get_pricing_env_override() or {}
    keys = sorted(_CATALOG_ENV_KEYS.intersection(env))
    return {
        "active": bool(keys),
        "keys": keys,
        "message": "环境配置覆盖中，后台已设为只读" if keys else "",
    }


def _catalog_response(cursor, config: Dict[str, Any], agent_user_id: Optional[int] = None) -> Dict[str, Any]:
    from config.pricing_config import get_agent_purchase_catalog_version
    from services.agent_inventory_pricing import build_purchase_snapshot, normalize_catalog_options

    version = get_agent_purchase_catalog_version(config)
    options = normalize_catalog_options(config.get("agent_purchase_options", []))
    output = []
    for option in options:
        snapshot = build_purchase_snapshot(
            cursor,
            config=config,
            catalog_version=version,
            agent_user_id=agent_user_id,
            amount_cents=option["amount_cents"],
            option=option,
        )
        output.append({
            "option_id": option["option_id"],
            "amount_cents": option["amount_cents"],
            "is_enabled": option["is_enabled"],
            "sort_order": option["sort_order"],
            "reward_description": snapshot["reward_description"],
            "preview": {
                "base_points": snapshot["base_points"],
                "bonus_points": snapshot["bonus_points"],
                "total_points": snapshot["total_points"],
                "discount_source": snapshot["discount_source"],
                "tier_at_order": snapshot["tier_at_order"],
                "bonus_rate_bps": snapshot["bonus_rate_bps"],
                "quote_fingerprint": snapshot["quote_fingerprint"],
            },
        })
    return {
        "success": True,
        "catalog_version": version,
        "options": output,
        "preview_agent_user_id": agent_user_id,
        "environment_override": _catalog_env_status(),
        "save_notice": "只影响保存后的新订单，历史及待支付订单不变",
    }


@router.get("/pricing/inventory-purchase-catalog")
async def admin_get_inventory_purchase_catalog(
    request: Request,
    agent_user_id: Optional[int] = Query(default=None, ge=1),
):
    _require_admin(request)
    from config.pricing_config import merge_pricing_config
    from services.config_epoch import read_config_epoch_strict

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_advisory_xact_lock_shared(920713, 1)")
        epoch_before = read_config_epoch_strict(cursor)
        cursor.execute("SELECT value FROM system_settings WHERE key='pricing_config'")
        raw = _parse_config_value(cursor.fetchone())
        if agent_user_id is not None:
            cursor.execute(
                "SELECT COALESCE(w.agent_level, 0) AS agent_level FROM users u "
                "LEFT JOIN user_wallets w ON w.user_id=u.id WHERE u.id=%s",
                (agent_user_id,),
            )
            agent = cursor.fetchone()
            level = (agent.get("agent_level") if isinstance(agent, dict) else (agent[0] if agent else 0)) or 0
            if int(level) < 1:
                raise HTTPException(400, "预览对象不是服务商")
        response = _catalog_response(cursor, merge_pricing_config(raw), agent_user_id)
        if read_config_epoch_strict(cursor) != epoch_before:
            raise HTTPException(503, detail={"code": "PRICING_FRESHNESS_UNCONFIRMED"})
        return response


@router.put("/pricing/inventory-purchase-catalog")
async def admin_put_inventory_purchase_catalog(
    req: InventoryCatalogPutRequest,
    request: Request,
):
    admin = _require_admin(request)
    env_status = _catalog_env_status()
    if env_status["active"]:
        raise HTTPException(423, detail={"code": "ENV_OVERRIDE_ACTIVE", **env_status})

    from config.pricing_config import (
        get_agent_purchase_catalog_version,
        invalidate_pricing_config_cache,
        merge_pricing_config,
    )
    from services.agent_inventory_pricing import (
        OPTION_ID_RE,
        insert_pricing_audit,
        next_catalog_version,
        normalize_catalog_options,
    )
    from services.config_epoch import accept_committed_epoch, bump_epoch_cursor, read_config_epoch_strict

    request_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )
    committed_epoch = None
    response = None
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_advisory_xact_lock(920713, 1)")
            read_config_epoch_strict(cursor)
            cursor.execute("SELECT value FROM system_settings WHERE key='pricing_config' FOR UPDATE")
            db_raw = _parse_config_value(cursor.fetchone())
            current = merge_pricing_config(db_raw, include_env=False)
            current_version = get_agent_purchase_catalog_version(current)
            if req.expected_catalog_version != current_version:
                raise HTTPException(
                    409,
                    detail={"code": "CATALOG_VERSION_CONFLICT", "current_catalog_version": current_version},
                )
            previous = {row["option_id"]: row for row in normalize_catalog_options(current.get("agent_purchase_options", []))}
            submitted_ids = {item.option_id for item in req.options if item.option_id}
            omitted_ids = sorted(set(previous).difference(submitted_ids))
            if omitted_ids:
                raise HTTPException(
                    422,
                    detail={
                        "code": "CATALOG_OPTION_OMISSION_FORBIDDEN",
                        "message": "已有档位不能硬删除，请改为停用",
                        "option_ids": omitted_ids,
                    },
                )
            rows = []
            for item in req.options:
                option_id = item.option_id or f"apo_{uuid.uuid4().hex[:16]}"
                if not OPTION_ID_RE.fullmatch(option_id):
                    raise HTTPException(422, "option_id 格式非法")
                old = previous.get(option_id)
                if item.option_id and old is None:
                    raise HTTPException(422, "新增档位的 option_id 必须由服务端生成")
                rows.append({
                    "option_id": option_id,
                    "amount_cents": item.amount_cents,
                    "is_enabled": item.is_enabled,
                    "sort_order": item.sort_order,
                    "reward_eligible": bool(old.get("reward_eligible", True)) if old else True,
                })
            rows = normalize_catalog_options(rows)
            new_version = next_catalog_version(current_version)
            merged_raw = dict(db_raw)
            merged_raw["agent_purchase_options"] = rows
            merged_raw["agent_purchase_catalog_version"] = new_version
            cursor.execute(
                """
                INSERT INTO system_settings (key, value, value_type, description, updated_at)
                VALUES ('pricing_config', %s, 'json', '定价系数动态配置(D5)', NOW())
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, value_type='json',
                    description=EXCLUDED.description, updated_at=NOW()
                """,
                (json.dumps(merged_raw, ensure_ascii=False),),
            )
            committed_epoch = bump_epoch_cursor(cursor)
            insert_pricing_audit(
                cursor,
                admin_user_id=admin["user_id"],
                admin_username=admin.get("username"),
                request_id=request_id,
                module="inventory_purchase_catalog",
                summary="更新服务商进货价目表",
                before_config={
                    "options": normalize_catalog_options(current.get("agent_purchase_options", []))
                },
                after_config={"options": rows},
                previous_catalog_version=current_version,
                catalog_version=new_version,
                ip_address=request.client.host if request.client else None,
            )
            if read_config_epoch_strict(cursor) != committed_epoch:
                raise RuntimeError("配置纪元回读不一致")
            response = _catalog_response(cursor, merge_pricing_config(merged_raw, include_env=False))
            response["request_id"] = request_id
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    accept_committed_epoch(int(committed_epoch))
    invalidate_pricing_config_cache()
    return response


@router.get("/pricing/global-config")
async def admin_get_global_pricing_config(request: Request):
    """读当前全局定价配置(默认+DB+env 合并)。admin-only。
    补 wholesale_discount 友好字段(让前端显示"8折"不露分子分母)。"""
    _require_admin(request)
    from config.pricing_config import get_agent_purchase_catalog_version, get_pricing_config
    cfg = dict(get_pricing_config())
    _n = int(cfg.get("wholesale_numer", 225))
    _d = int(cfg.get("wholesale_denom", 325))
    cfg["wholesale_discount"] = _ratio_to_discount(_n, _d)
    return {
        "success": True,
        "catalog_version": get_agent_purchase_catalog_version(cfg),
        "config": cfg,
    }


@router.put("/pricing/global-config")
async def admin_put_global_pricing_config(req: GlobalPricingConfigRequest, request: Request):
    """改全局定价配置 → 写 system_settings.pricing_config(merge) + 热加载(reload·无需重启)。
    admin-only · 护栏 · 审计(updated_at)。"""
    a = _require_admin(request)
    import json
    # 友好字段 wholesale_discount 给了 → 转 numer/denom 覆盖(与 numer/denom 入参二选一·都给以 discount 优先)
    eff_numer = req.wholesale_numer
    eff_denom = req.wholesale_denom
    if req.wholesale_discount is not None:
        if not (0.39 <= req.wholesale_discount <= 1.0):
            raise HTTPException(400, f"出厂折扣 wholesale_discount={req.wholesale_discount} 不合理(须 0.39-1.0·1.0=不打折)")
        eff_numer, eff_denom = _discount_to_ratio(req.wholesale_discount)
    # [FIX·P1-1] 出厂折扣量纲校验:numer/denom 必须成对 + 比值合理(防 201/326 坏值致现存 SKU 量纲错乱)
    if (eff_numer is None) != (eff_denom is None):
        raise HTTPException(400, "wholesale_numer / wholesale_denom 必须成对提供(避免半填致量纲错乱)")
    if eff_numer is not None:
        if eff_numer <= 0 or eff_denom <= 0:
            raise HTTPException(400, "wholesale_numer / wholesale_denom 须 > 0")
        ratio = eff_numer / eff_denom
        if not (0.3 <= ratio <= 1.0):
            raise HTTPException(400, f"出厂折扣比值 numer/denom={ratio:.3f} 不合理(须 0.3-1.0·1.0=不打折/0.3=3折底线)")
    if req.agent_purchase_bonus_rate is not None and not (0.0 <= req.agent_purchase_bonus_rate <= 1.0):
        raise HTTPException(400, "agent_purchase_bonus_rate 须在 0-1")
    patch = req.model_dump(exclude_none=True)
    expected_catalog_version = patch.pop("expected_catalog_version")
    # wholesale_discount 是友好入参 · 不入库(库里存 numer/denom SSOT)· 用换算结果覆盖
    patch.pop("wholesale_discount", None)
    if eff_numer is not None:
        patch["wholesale_numer"] = eff_numer
        patch["wholesale_denom"] = eff_denom
    if not patch:
        raise HTTPException(400, "至少改一个字段")
    from config.pricing_config import get_pricing_env_override
    env_override = get_pricing_env_override() or {}
    overridden = sorted(set(patch).intersection(env_override))
    if overridden:
        raise HTTPException(
            423,
            detail={
                "code": "ENV_OVERRIDE_ACTIVE",
                "keys": overridden,
                "message": "环境配置覆盖中，默认进货规则无法保存",
            },
        )
    from config.pricing_config import (
        get_agent_purchase_catalog_version,
        invalidate_pricing_config_cache,
        merge_pricing_config,
    )
    from services.agent_inventory_pricing import insert_pricing_audit, next_catalog_version
    from services.config_epoch import (
        accept_committed_epoch,
        bump_epoch_cursor,
        read_config_epoch_strict,
    )
    request_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )
    committed_epoch = None
    new_version = None
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT pg_advisory_xact_lock(920713, 1)")
            read_config_epoch_strict(cur)
            cur.execute("SELECT value FROM system_settings WHERE key = 'pricing_config' FOR UPDATE")
            row = cur.fetchone()
            existing = {}
            if row:
                val = row["value"] if isinstance(row, dict) else row[0]
                try:
                    existing = json.loads(val) if isinstance(val, str) else (val or {})
                except Exception:
                    existing = {}
            effective_before = merge_pricing_config(existing, include_env=False)
            current_version = get_agent_purchase_catalog_version(effective_before)
            if expected_catalog_version != current_version:
                raise HTTPException(
                    409,
                    detail={
                        "code": "CATALOG_VERSION_CONFLICT",
                        "current_catalog_version": current_version,
                    },
                )
            merged = {**existing, **patch}
            new_version = next_catalog_version(current_version)
            merged["agent_purchase_catalog_version"] = new_version
            cur.execute("""
                INSERT INTO system_settings (key, value, value_type, description, updated_at, updated_by)
                VALUES ('pricing_config', %s, 'json', '定价系数动态配置(D5)', NOW(), %s)
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value, value_type = 'json', updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
            """, (json.dumps(merged, ensure_ascii=False), a["user_id"]))
            committed_epoch = bump_epoch_cursor(cur)
            effective_after = merge_pricing_config(merged, include_env=False)
            audit_keys = ("wholesale_numer", "wholesale_denom", "agent_purchase_bonus_rate")
            insert_pricing_audit(
                cur,
                admin_user_id=a["user_id"],
                admin_username=a.get("username"),
                request_id=request_id,
                module="inventory_default_rules",
                summary="更新服务商默认进货规则",
                before_config={key: effective_before.get(key) for key in audit_keys},
                after_config={key: effective_after.get(key) for key in audit_keys},
                previous_catalog_version=current_version,
                catalog_version=new_version,
                ip_address=request.client.host if request.client else None,
            )
            if read_config_epoch_strict(cur) != committed_epoch:
                raise RuntimeError("配置纪元回读不一致")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    accept_committed_epoch(int(committed_epoch))
    invalidate_pricing_config_cache()
    return {
        "success": True,
        "updated_fields": list(patch.keys()),
        "catalog_version": new_version,
        "request_id": request_id,
    }


class AgentPricingOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_catalog_version: Optional[str] = Field(
        default=None, pattern=r"^agent-purchase-v[1-9][0-9]*$"
    )
    quote_markup_override: Optional[float] = None
    sku_markup_override: Optional[float] = None
    wholesale_numer: Optional[int] = None
    wholesale_denom: Optional[int] = None
    note: Optional[str] = None

    @model_validator(mode="after")
    def require_version_for_wholesale(self):
        if (
            self.wholesale_numer is not None or self.wholesale_denom is not None
        ) and not self.expected_catalog_version:
            raise ValueError("修改专属进货折扣必须携带 expected_catalog_version")
        return self


@router.get("/pricing/agent-lookup")
async def admin_pricing_agent_lookup(request: Request, q: str = Query(..., description="服务商编号/手机号/用户名")):
    """[复审返修4] 按 user_id / 手机号 / 用户名 / 显示名 查服务商(避免 admin 死记 user_id)。admin-only。
    返回匹配列表(标 agent_level·前端选服务商;PUT agent-override 再校验 agent_level>=1)。"""
    _require_admin(request)
    kw = (q or "").strip()
    if not kw:
        raise HTTPException(400, "请输入服务商编号 / 手机号 / 用户名")
    results = []
    with get_db() as conn:
        cur = conn.cursor()
        if kw.isdigit():
            cur.execute("""
                SELECT u.id, u.username, u.phone, u.display_name,
                       COALESCE(w.agent_level, 0) AS agent_level
                FROM users u LEFT JOIN user_wallets w ON w.user_id = u.id
                WHERE u.id = %s OR u.phone = %s OR u.username = %s
                ORDER BY u.id LIMIT 10
            """, (int(kw), kw, kw))
        else:
            cur.execute("""
                SELECT u.id, u.username, u.phone, u.display_name,
                       COALESCE(w.agent_level, 0) AS agent_level
                FROM users u LEFT JOIN user_wallets w ON w.user_id = u.id
                WHERE u.phone = %s OR u.username = %s OR u.display_name = %s
                ORDER BY u.id LIMIT 10
            """, (kw, kw, kw))
        for row in cur.fetchall():
            d = dict(row) if isinstance(row, dict) else {}
            results.append({
                "user_id": d.get("id"),
                "username": d.get("username"),
                "phone": d.get("phone"),
                "display_name": d.get("display_name"),
                "agent_level": int(d.get("agent_level", 0) or 0),
            })
    return {"success": True, "results": results}


@router.get("/pricing/agent-override/{agent_user_id}")
async def admin_get_agent_pricing_override(agent_user_id: int, request: Request):
    """读 per-agent 定价覆盖(无→空 dict)。admin-only。"""
    _require_admin(request)
    from config.pricing_config import get_agent_purchase_catalog_version, get_pricing_config
    from services.agent_pricing_overrides import get_agent_pricing_override
    return {"success": True, "agent_user_id": agent_user_id,
            "override": get_agent_pricing_override(agent_user_id) or {},
            "catalog_version": get_agent_purchase_catalog_version(get_pricing_config())}


@router.put("/pricing/agent-override/{agent_user_id}")
async def admin_put_agent_pricing_override(agent_user_id: int, req: AgentPricingOverrideRequest, request: Request):
    """平台给单个服务商设系数 override(quote/sku/出厂折扣)。admin-only · 护栏 · 审计。
    优先级:此 override > 服务商自设 > 全局默认(见 quote_pricing_preferences / agent_pricing)。"""
    a = _require_admin(request)
    # [FIX·复核P2] 校验目标是服务商账号(agent_level>=1)·避免误给普通客户配服务商价格 override(资金护栏)
    with get_db() as _vc:
        _vcur = _vc.cursor()
        _vcur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (agent_user_id,))
        _vr = _vcur.fetchone()
    _lvl = int((_vr["agent_level"] if isinstance(_vr, dict) else _vr[0]) or 0) if _vr else 0
    if _lvl < 1:
        raise HTTPException(400, f"目标 user_id={agent_user_id} 不是服务商账号(agent_level={_lvl})·不能配服务商价格 override")
    if req.quote_markup_override is not None and not (1.0 <= req.quote_markup_override <= 5.0):
        raise HTTPException(400, "quote_markup_override 须在 1.0-5.0")
    if req.sku_markup_override is not None and req.sku_markup_override <= 0:
        raise HTTPException(400, "sku_markup_override 须 > 0")
    # [FIX·P1-2] per-agent 出厂折扣量纲校验:成对 + 比值合理(防半填 numer 不填 denom / 非理性比值 999999/1)
    if (req.wholesale_numer is None) != (req.wholesale_denom is None):
        raise HTTPException(400, "wholesale_numer / wholesale_denom 必须成对提供")
    if req.wholesale_numer is not None:
        if req.wholesale_numer <= 0 or req.wholesale_denom <= 0:
            raise HTTPException(400, "wholesale_numer / wholesale_denom 须 > 0")
        ratio = req.wholesale_numer / req.wholesale_denom
        if not (0.3 <= ratio <= 1.0):
            raise HTTPException(400, f"出厂折扣比值 numer/denom={ratio:.3f} 不合理(须 0.3-1.0)")
    from services.agent_pricing_overrides import (
        AgentPricingOverrideVersionConflict,
        write_agent_pricing_override_atomic,
    )
    request_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )
    try:
        result = write_agent_pricing_override_atomic(
            agent_user_id,
            quote_markup_override=req.quote_markup_override,
            sku_markup_override=req.sku_markup_override,
            wholesale_numer=req.wholesale_numer,
            wholesale_denom=req.wholesale_denom,
            note=req.note,
            expected_catalog_version=req.expected_catalog_version,
            admin_user_id=a["user_id"],
            admin_username=a.get("username"),
            request_id=request_id,
            ip_address=request.client.host if request.client else None,
            audit_module="admin_pricing_agent_override",
        )
    except AgentPricingOverrideVersionConflict as exc:
        raise HTTPException(
            409,
            detail={
                "code": "CATALOG_VERSION_CONFLICT",
                "current_catalog_version": exc.current_catalog_version,
            },
        ) from exc
    return {
        "success": True,
        "agent_user_id": agent_user_id,
        "catalog_version": result["catalog_version"],
        "request_id": result["request_id"],
    }


@router.delete("/pricing/agent-override/{agent_user_id}")
async def admin_clear_agent_pricing_override(
    agent_user_id: int,
    request: Request,
    expected_catalog_version: str = Query(..., pattern=r"^agent-purchase-v[1-9][0-9]*$"),
):
    """清空 per-agent 覆盖(回落全局)。admin-only。"""
    a = _require_admin(request)
    from services.agent_pricing_overrides import (
        AgentPricingOverrideVersionConflict,
        write_agent_pricing_override_atomic,
    )
    request_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )
    try:
        result = write_agent_pricing_override_atomic(
            agent_user_id,
            clear_fields=[
                "quote_markup_override", "sku_markup_override",
                "wholesale_numer", "wholesale_denom",
            ],
            expected_catalog_version=expected_catalog_version,
            admin_user_id=a["user_id"],
            admin_username=a.get("username"),
            request_id=request_id,
            ip_address=request.client.host if request.client else None,
            audit_module="admin_pricing_agent_override",
        )
    except AgentPricingOverrideVersionConflict as exc:
        raise HTTPException(
            409,
            detail={
                "code": "CATALOG_VERSION_CONFLICT",
                "current_catalog_version": exc.current_catalog_version,
            },
        ) from exc
    return {
        "success": True,
        "agent_user_id": agent_user_id,
        "cleared": True,
        "catalog_version": result["catalog_version"],
        "request_id": result["request_id"],
    }


# ============================================================
# 7. 对账快照(W2 只读 · W4 才做日报告警)
# ============================================================

@router.get("/inventory-audit/summary", response_model=AdminInventoryAuditSummary)
async def admin_inventory_audit(request: Request):
    """
    对账等式即时计算 · **账实相符式**(2026-07-29 返修 · 与 services/inventory_audit.py 同一口径):
        diff = SUM(wallets.paid + bonus + frozen) − SUM(agent_inventory_transactions.points)

    🔴 这里原来是第二份【枚举式】:
        agent_total + customer_total + platform_consumed − historical_purchased ± admin_adjust
    与 run_audit 那份同根同源 —— 只统计已知 type,manufacturer_origin_in 的 1.3 亿天生在等式外,
    且把已停写的 customer_credit_transactions 计入。同一个 /admin/inventory-audit 页面上方
    显示本接口的即时差额、下方显示 run_audit 的历史差额,两者口径不一致就会一红一绿自相矛盾。
    故本接口一并收敛到 SSOT 等式;其余分项字段保留作审计留痕(不参与 diff)。
    """
    _require_admin(request)
    from datetime import datetime as _dt
    with get_db() as conn:
        cur = conn.cursor()

        # 账面【实】· 三池(frozen 必须计入:冻结是池内搬运 · 不写流水)
        cur.execute("""
            SELECT COALESCE(SUM(paid_inventory_points), 0) AS paid,
                   COALESCE(SUM(bonus_inventory_points), 0) AS bonus,
                   COALESCE(SUM(frozen_inventory_points), 0) AS frozen
            FROM agent_inventory_wallets
        """)
        _w = cur.fetchone()
        agent_paid = int(_g(_w, "paid", 0) or 0)
        agent_bonus = int(_g(_w, "bonus", 0) or 0)
        agent_frozen = int(_g(_w, "frozen", 0) or 0)
        agent_total = agent_paid + agent_bonus
        wallet_total = agent_total + agent_frozen

        # 账面【账】· 全量流水净额 · 🔴 不带 WHERE type IN (...)
        cur.execute("""
            SELECT COALESCE(SUM(points), 0) AS s
            FROM agent_inventory_transactions
        """)
        ledger_total = int(_g(cur.fetchone(), "s", 0) or 0)

        # [历史账本只读 · 2026-08-17] customer_agent_credit_wallets 自 2026-07-29 停写,
        # 三池已清零 → 本项恒 0,是守恒等式里的历史项,不删以免追溯改写历史期间报表。
        cur.execute("""
            SELECT COALESCE(SUM(tool_credit_points), 0)
                 + COALESCE(SUM(publish_credit_points), 0)
                 + COALESCE(SUM(bonus_credit_points), 0) AS s
            FROM customer_agent_credit_wallets
        """)
        customer_total = int(_g(cur.fetchone(), "s", 0) or 0)

        # 平台消费(customer_credit_transactions 中 consume / negative · 取绝对值)
        cur.execute("""
            SELECT COALESCE(SUM(ABS(points)), 0) AS s
            FROM customer_credit_transactions
            WHERE type = 'consume'
        """)
        platform_consumed = int(_g(cur.fetchone(), "s", 0) or 0)

        # ⚪ 分项快照(审计留痕 · 供人看明细)· 🔴 绝不进 diff
        cur.execute("""
            SELECT COALESCE(SUM(points), 0) AS s
            FROM agent_inventory_transactions
            WHERE type IN ('purchase_prepay', 'purchase_auto')
              AND points > 0
        """)
        historical_purchased = int(_g(cur.fetchone(), "s", 0) or 0)

        cur.execute("""
            SELECT COALESCE(SUM(points), 0) AS s
            FROM agent_inventory_transactions
            WHERE type IN ('purchase_admin_adjust', 'admin_adjust')
        """)
        admin_adjust = int(_g(cur.fetchone(), "s", 0) or 0)

    diff = wallet_total - ledger_total
    diff_status = "ok" if abs(diff) <= 1 else "drift_warning"

    return AdminInventoryAuditSummary(
        agent_total_points=agent_total,
        agent_frozen_points=agent_frozen,
        wallet_total_points=wallet_total,
        ledger_total_points=ledger_total,
        customer_total_points=customer_total,
        platform_consumed_points=platform_consumed,
        historical_purchased_points=historical_purchased,
        historical_admin_adjust_points=admin_adjust,
        diff_points=diff,
        diff_status=diff_status,
        snapshot_at=_dt.utcnow(),
    )
