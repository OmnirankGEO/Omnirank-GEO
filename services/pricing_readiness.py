"""双价目表/报价/渠道开闸只读门禁。

本模块只读 PostgreSQL 与随部署代码证据；不发布目录、不准备编号、不改关系、不翻 flag。
任一证据缺失均 fail-closed 返回 ready=false + 人话 blocker。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from db.connection import get_db
from services import account_codes, channel_pricing
from services.config_epoch import read_config_epoch_strict


_REQUIRED_COLUMNS = {
    "pricing_catalog_versions": {
        "id": "bigint", "catalog_type": "text", "scope_key": "text",
        "version_code": "text", "status": "text", "calc_meta_jsonb": "jsonb",
    },
    "pricing_catalog_entries": {
        "version_id": "bigint", "product_code": "text", "final_price_cents": "integer",
        "paid_points": "bigint", "bonus_points": "bigint", "source_ref_jsonb": "jsonb",
    },
    "price_quotes": {
        "quote_id": "text", "quote_type": "text", "buyer_user_id": "integer",
        "catalog_version_id": "bigint", "final_price_cents": "integer",
        "pricing_snapshot_jsonb": "jsonb", "status": "text", "used_order_id": "text",
        "idempotency_key": "text", "expires_at": "timestamp with time zone",
    },
    "agent_pricing_overrides": {
        "agent_user_id": "integer", "wholesale_numer": "integer",
        "wholesale_denom": "integer", "updated_at": "timestamp without time zone",
    },
    "public_account_codes": {
        "user_id": "integer", "service_account_code": "text", "channel_account_code": "text",
    },
    "channel_pricing_relationships": {
        "buyer_dealer_id": "integer", "upstream_channel_account_id": "integer",
        "relationship_version": "text", "cost_multiplier_bps": "integer", "status": "text",
    },
    "channel_revenue_ledger": {
        "channel_beneficiary_user_id": "integer", "buyer_dealer_id": "integer",
        "upstream_cost_basis_cents": "integer", "buyer_paid_cents": "integer",
        "channel_revenue_cents": "integer", "status": "text",
    },
    "recharge_orders": {
        "price_quote_id": "text", "pricing_catalog_version": "text",
        "pricing_snapshot_jsonb": "jsonb", "settlement_snapshot_jsonb": "jsonb",
        "idempotency_key": "text", "payment_status": "text", "order_type": "text",
    },
}


def _check_schema(cur) -> Dict[str, Any]:
    problems: List[str] = []
    actual: Dict[str, Dict[str, str]] = {}
    for table, required in _REQUIRED_COLUMNS.items():
        cur.execute(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name=%s""",
            (table,),
        )
        columns = {str(row["column_name"]): str(row["data_type"]) for row in cur.fetchall()}
        actual[table] = columns
        if not columns:
            problems.append(f"缺少数据表 {table}")
            continue
        for column, expected_type in required.items():
            got = columns.get(column)
            if got is None:
                problems.append(f"{table} 缺少列 {column}")
            elif got != expected_type:
                problems.append(f"{table}.{column} 类型应为 {expected_type}，实际 {got}")

    cur.execute(
        """SELECT indexname, indexdef FROM pg_indexes
           WHERE schemaname=current_schema()
             AND tablename=ANY(%s)""",
        (list(_REQUIRED_COLUMNS),),
    )
    index_defs = {str(row["indexname"]): str(row["indexdef"]) for row in cur.fetchall()}
    required_index_evidence = {
        "published catalog 单开放版本": (
            "pricing_catalog_versions", "UNIQUE", "catalog_type", "scope_key", "status = 'published'"
        ),
        "一报价一订单": ("recharge_orders", "UNIQUE", "price_quote_id"),
        "单买方活动直属上游": ("channel_pricing_relationships", "UNIQUE", "buyer_dealer_id"),
        "关系版本唯一": (
            "channel_pricing_relationships", "UNIQUE", "buyer_dealer_id", "relationship_version"
        ),
    }
    for label, needles in required_index_evidence.items():
        if not any(all(needle.lower() in definition.lower() for needle in needles)
                   for definition in index_defs.values()):
            problems.append(f"缺少/篡改索引约束：{label}")

    # The override writer relies on one durable row per agent.  pg_indexes text
    # matching is insufficient here: a partial or expression index can contain
    # the same words while leaving ordinary agent ids unconstrained.
    cur.execute(
        """SELECT EXISTS (
                   SELECT 1
                     FROM pg_constraint con
                     JOIN pg_class tbl ON tbl.oid=con.conrelid
                     JOIN pg_namespace ns ON ns.oid=tbl.relnamespace
                    WHERE ns.nspname=current_schema()
                      AND tbl.relname='agent_pricing_overrides'
                      AND con.contype IN ('p', 'u')
                      AND con.convalidated
                      AND cardinality(con.conkey)=1
                      AND (
                          SELECT att.attname
                            FROM pg_attribute att
                           WHERE att.attrelid=tbl.oid
                             AND att.attnum=con.conkey[1]
                      )='agent_user_id'
               ) AS has_exact_unique,
               EXISTS (
                   SELECT 1
                     FROM pg_attribute att
                     JOIN pg_class tbl ON tbl.oid=att.attrelid
                     JOIN pg_namespace ns ON ns.oid=tbl.relnamespace
                    WHERE ns.nspname=current_schema()
                      AND tbl.relname='agent_pricing_overrides'
                      AND att.attname='agent_user_id'
                      AND att.attnotnull
                      AND NOT att.attisdropped
               ) AS agent_user_id_not_null"""
    )
    override_identity = dict(cur.fetchone())
    if not (
        bool(override_identity.get("has_exact_unique"))
        and bool(override_identity.get("agent_user_id_not_null"))
    ):
        problems.append("缺少/篡改索引约束：专属折扣单服务商唯一")

    cur.execute(
        """SELECT conname, convalidated, pg_get_constraintdef(oid) AS definition
           FROM pg_constraint
           WHERE conrelid=ANY(%s::regclass[])""",
        ([
            "pricing_catalog_entries", "price_quotes", "recharge_orders",
            "public_account_codes", "channel_pricing_relationships",
            "channel_revenue_ledger",
        ],),
    )
    constraints = [dict(row) for row in cur.fetchall()]
    constraint_defs = [str(row["definition"]) for row in constraints if bool(row["convalidated"])]
    validated_constraint_names = {
        str(row["conname"]) for row in constraints if bool(row["convalidated"])
    }
    for constraint_name in (
        "chk_catalog_entry_source_ref_object",
        "chk_catalog_entry_money_nonnegative",
        "chk_price_quote_snapshot_object",
        "chk_price_quote_money_nonnegative",
        "chk_price_quote_snapshot_catalog_id",
        "chk_price_quote_status_fields_consistent",
        "chk_recharge_pricing_snapshot_object",
        "chk_recharge_quote_anchor_complete",
        "chk_channel_relationship_version_nonblank",
        "chk_channel_relationship_multiplier_floor",
        "chk_channel_revenue_nonnegative",
        "chk_public_service_code_format",
        "chk_public_channel_code_format",
        "fk_price_quotes_catalog_version_id",
        "fk_recharge_orders_price_quote_id",
        "fk_channel_relationship_buyer_user",
        "fk_channel_relationship_upstream_user",
    ):
        if constraint_name not in validated_constraint_names:
            problems.append(f"缺少/未验证 schema 约束：{constraint_name}")
    for label, needles in {
        "订单报价外键": ("FOREIGN KEY (price_quote_id)", "price_quotes"),
        "报价目录外键": ("FOREIGN KEY (catalog_version_id)", "pricing_catalog_versions"),
        "SV 编号格式": ("service_account_code", "SV-"),
        "CH 编号格式": ("channel_account_code", "CH-"),
        "关系版本非空": ("relationship_version", "btrim"),
        "渠道系数不低于上游成本": ("cost_multiplier_bps", ">= 10000"),
        "渠道收益非负": ("channel_revenue_cents", ">= 0"),
    }.items():
        if not any(all(needle.lower() in definition.lower() for needle in needles)
                   for definition in constraint_defs):
            problems.append(f"缺少/未验证 schema 约束：{label}")

    cur.execute(
        """SELECT c.relname AS table_name, t.tgname, p.proname AS function_name
           FROM pg_trigger t
           JOIN pg_class c ON c.oid=t.tgrelid
           JOIN pg_proc p ON p.oid=t.tgfoid
           WHERE c.relname=ANY(%s) AND NOT t.tgisinternal AND t.tgenabled <> 'D'""",
        (["pricing_catalog_versions", "pricing_catalog_entries", "price_quotes", "recharge_orders"],),
    )
    triggers = [dict(row) for row in cur.fetchall()]
    trigger_contract = {
        "trg_pricing_catalog_version_immutable": (
            "pricing_catalog_versions", "pricing_catalog_version_immutable_guard"
        ),
        "trg_pricing_catalog_entry_draft_only": (
            "pricing_catalog_entries", "pricing_catalog_entry_draft_only_guard"
        ),
        "trg_price_quote_immutable": ("price_quotes", "price_quote_immutable_guard"),
        "trg_recharge_order_pricing_immutable": (
            "recharge_orders", "recharge_order_pricing_immutable_guard"
        ),
    }
    trigger_by_name = {str(row["tgname"]): row for row in triggers}
    for trigger_name, (table, function_name) in trigger_contract.items():
        row = trigger_by_name.get(trigger_name)
        if not row:
            problems.append(f"{table} 缺少不可变快照 trigger {trigger_name}")
        elif str(row["table_name"]) != table or str(row["function_name"]) != function_name:
            problems.append(f"不可变 trigger {trigger_name} 指向被篡改")
    from services.agent_retail_sku_schema import schema_status as retail_sku_schema_status

    retail_sku_schema = retail_sku_schema_status(cur)
    problems.extend(str(item) for item in retail_sku_schema.get("blockers", []))
    invalid_procurement_overrides = 0
    if all(
        actual.get("agent_pricing_overrides", {}).get(column) == expected
        for column, expected in _REQUIRED_COLUMNS["agent_pricing_overrides"].items()
    ):
        cur.execute(
            """SELECT COUNT(*) AS c
                 FROM agent_pricing_overrides
                WHERE (wholesale_numer IS NULL) <> (wholesale_denom IS NULL)
                   OR (
                     wholesale_numer IS NOT NULL
                     AND (
                       wholesale_numer <= 0 OR wholesale_denom <= 0 OR updated_at IS NULL
                       OR wholesale_numer > wholesale_denom
                       OR wholesale_numer::bigint * 10 < wholesale_denom::bigint * 3
                     )
                   )"""
        )
        invalid_procurement_overrides = int(cur.fetchone()["c"] or 0)
        if invalid_procurement_overrides:
            problems.append(
                f"{invalid_procurement_overrides} 个服务商专属进货折扣缺少完整数值或版本"
            )
    return {
        "ready": not problems,
        "problems": problems,
        "agent_retail_sku": retail_sku_schema,
        "invalid_procurement_overrides": invalid_procurement_overrides,
        "validated_constraint_count": sum(1 for row in constraints if row["convalidated"]),
        "immutable_trigger_names": [row["tgname"] for row in triggers],
    }


def _check_catalogs(cur, code_status: Dict[str, Any]) -> Dict[str, Any]:
    problems: List[str] = []
    catalogs: List[Dict[str, Any]] = []
    scopes = [("procurement", "PLATFORM_BASE")]
    for row in code_status.get("service_accounts", []):
        if row.get("code"):
            scopes.append(("retail", str(row["code"])))
    for catalog_type, scope_key in scopes:
        cur.execute(
            """
            SELECT v.id, v.version_code, v.calc_meta_jsonb, COUNT(e.id) AS entry_count
            FROM pricing_catalog_versions v
            LEFT JOIN pricing_catalog_entries e ON e.version_id=v.id
            WHERE v.catalog_type=%s AND v.scope_key=%s
              AND v.status='published' AND v.effective_to IS NULL
            GROUP BY v.id
            """,
            (catalog_type, scope_key),
        )
        row = cur.fetchone()
        if not row:
            problems.append(f"{catalog_type}/{scope_key} 没有已发布目录")
            catalogs.append({"catalog_type": catalog_type, "scope_key": scope_key, "published": False})
            continue
        entry_count = int(row["entry_count"] or 0)
        if entry_count <= 0:
            problems.append(f"{catalog_type}/{scope_key} 已发布目录没有明细")
        meta = row.get("calc_meta_jsonb")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = None
        if not isinstance(meta, dict) or not meta.get("source_fingerprint"):
            problems.append(f"{catalog_type}/{scope_key} 缺少发布来源 fingerprint")
        catalogs.append({
            "catalog_type": catalog_type, "scope_key": scope_key, "published": True,
            "version_id": int(row["id"]), "version_code": row["version_code"],
            "entry_count": entry_count,
            "source_fingerprint": meta.get("source_fingerprint") if isinstance(meta, dict) else None,
            "config_epoch": meta.get("config_epoch") if isinstance(meta, dict) else None,
        })

    try:
        from services import pricing_publication
        drift = pricing_publication.publication_status(cur=cur)
        for blocker in drift.get("blockers", []):
            problems.append(str(blocker))
        needs_publication = int(drift.get("needs_publication_count") or 0)
        if drift.get("ready") is not True or needs_publication > 0:
            problems.append(
                f"管理定价源仍有 {needs_publication} 个目录需要重新发布，禁止开闸"
            )
    except Exception as exc:  # fail closed: publication provenance is a gate requirement
        drift = {"ready": False, "blockers": [f"无法验证管理源发布漂移: {exc}"]}
        problems.extend(drift["blockers"])
    return {"ready": not problems, "catalogs": catalogs, "publication": drift, "problems": problems}


def _check_order_snapshots(cur) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE price_quote_id IS NOT NULL) AS quoted_orders,
          COUNT(*) FILTER (WHERE price_quote_id IS NULL) AS legacy_orders,
          COUNT(*) FILTER (
            WHERE price_quote_id IS NOT NULL
              AND (pricing_catalog_version IS NULL OR pricing_snapshot_jsonb IS NULL)
          ) AS incomplete_quoted_orders,
          COUNT(*) FILTER (
            WHERE payment_status='pending'
              AND order_type IN ('customer_recharge','agent_inventory_purchase')
              AND (price_quote_id IS NULL OR pricing_catalog_version IS NULL OR pricing_snapshot_jsonb IS NULL)
          ) AS incomplete_pending_orders,
          COUNT(*) FILTER (
            WHERE payment_status='pending'
              AND order_type='customer_recharge'
              AND (
                (sku_template_id IS NOT NULL AND agent_user_id IS NOT NULL)
                OR pricing_snapshot_jsonb->>'amount_source'='sku_snapshot'
              )
              AND COALESCE(pricing_snapshot_jsonb->>'commercial_resolution','')
                  NOT IN ('BOUND','PLATFORM_DIRECT')
          ) AS pending_missing_commercial_resolution
        FROM recharge_orders
        """
    )
    row = dict(cur.fetchone())
    from services.procurement_cash_anchor import CASH_ANCHOR_SEMANTIC_VERSION

    cur.execute(
        """SELECT
             COUNT(*) AS quote_count,
             COUNT(*) FILTER (WHERE catalog_version_id IS NULL OR pricing_snapshot_jsonb IS NULL) AS incomplete_quotes,
             COUNT(*) FILTER (WHERE status='issued' AND expires_at > NOW()) AS active_quotes,
             COUNT(*) FILTER (
               WHERE quote_type='procurement' AND status='issued' AND expires_at > NOW()
                 AND COALESCE(
                   pricing_snapshot_jsonb #>>
                     '{order_pricing_snapshot,cash_anchor_semantic_version}',
                   ''
                 ) <> %s
             ) AS active_legacy_procurement_quotes
           FROM price_quotes""",
        (CASH_ANCHOR_SEMANTIC_VERSION,),
    )
    quote_row = dict(cur.fetchone())
    cur.execute(
        """
        SELECT id
        FROM recharge_orders
        WHERE payment_status='pending'
          AND order_type='customer_recharge'
          AND (
            (sku_template_id IS NOT NULL AND agent_user_id IS NOT NULL)
            OR pricing_snapshot_jsonb->>'amount_source'='sku_snapshot'
          )
          AND COALESCE(pricing_snapshot_jsonb->>'commercial_resolution','')
              NOT IN ('BOUND','PLATFORM_DIRECT')
        ORDER BY created_at,id
        """
    )
    pending_resolution_review_orders = [str(item["id"]) for item in cur.fetchall()]
    problems: List[str] = []
    if int(row["incomplete_quoted_orders"] or 0):
        problems.append(f"{row['incomplete_quoted_orders']} 个报价订单缺少完整目录/定价快照")
    if int(row["incomplete_pending_orders"] or 0):
        problems.append(f"{row['incomplete_pending_orders']} 个待支付现役订单缺少报价或完整快照")
    if int(row["pending_missing_commercial_resolution"] or 0):
        problems.append(
            f"{row['pending_missing_commercial_resolution']} 个待支付锁定订单缺少 commercial_resolution；"
            "部署前必须逐笔核验延迟回调风险，禁止批量补造"
        )
    if int(quote_row["incomplete_quotes"] or 0):
        problems.append(f"{quote_row['incomplete_quotes']} 个报价缺少目录版本或定价快照")
    if int(quote_row["active_legacy_procurement_quotes"] or 0):
        problems.append(
            f"{quote_row['active_legacy_procurement_quotes']} 个未消费进货报价仍使用旧金额语义；"
            "须等待失效或由用户重新报价"
        )
    total = int(row["quoted_orders"] or 0) + int(row["legacy_orders"] or 0)
    coverage = (int(row["quoted_orders"] or 0) / total) if total else 1.0
    return {
        "ready": not problems,
        "quoted_orders": int(row["quoted_orders"] or 0),
        "legacy_orders": int(row["legacy_orders"] or 0),
        "quote_order_coverage": coverage,
        "incomplete_quoted_orders": int(row["incomplete_quoted_orders"] or 0),
        "incomplete_pending_orders": int(row["incomplete_pending_orders"] or 0),
        "pending_missing_commercial_resolution": int(row["pending_missing_commercial_resolution"] or 0),
        "pending_resolution_review_orders": pending_resolution_review_orders,
        "quote_count": int(quote_row["quote_count"] or 0),
        "active_quotes": int(quote_row["active_quotes"] or 0),
        "active_legacy_procurement_quotes": int(
            quote_row["active_legacy_procurement_quotes"] or 0
        ),
        "incomplete_quotes": int(quote_row["incomplete_quotes"] or 0),
        "problems": problems,
    }


def _check_entry_wiring() -> Dict[str, Any]:
    """Validate the explicit active GEO purchase contract without parsing TSX text."""

    problems: List[str] = []
    warnings = ["社媒购买入口不纳入 GEO 定价开闸门禁，需由社媒范围单独治理"]
    try:
        from api import agent_workbench_api, pricing_ssot_api, wallet_api
        retail_capability = getattr(wallet_api, "PRICING_QUOTE_WIRING_CAPABILITY", None)
        procurement_capability = getattr(
            agent_workbench_api, "PROCUREMENT_QUOTE_WIRING_CAPABILITY", None
        )
        backend_behaviors = {
            "customer-recharge": getattr(wallet_api, "PRICING_QUOTE_WIRING_BEHAVIOR", None),
            "agent-inventory-purchase": getattr(
                agent_workbench_api, "PROCUREMENT_QUOTE_WIRING_BEHAVIOR", None
            ),
        }
    except Exception as exc:
        retail_capability = procurement_capability = None
        backend_behaviors = {}
        problems.append(f"无法加载现役购买入口能力: {exc}")
    if retail_capability != "retail-price-quote-v1":
        problems.append("客户充值后端未声明 retail price_quote 接线能力")
    if procurement_capability != "procurement-price-quote-v1":
        problems.append("服务商进货后端未声明 procurement price_quote 接线能力")

    project_root = Path(__file__).resolve().parents[1]
    contract_path = project_root / "frontend/src/contracts/pricing-entry-contracts.json"
    try:
        frontend_document = json.loads(contract_path.read_text(encoding="utf-8"))
    except Exception as exc:
        frontend_document = {}
        problems.append(f"无法加载 GEO 购买行为契约: {exc}")

    expected = {
        "customer-recharge": {
            "active_route": "/customer/recharge",
            "catalog_endpoint": "/api/pricing/retail/catalog",
            "quote_endpoints": {
                "fixed": "/api/pricing/retail/quote",
                "custom_amount": "/api/pricing/retail/custom-amount-quote",
            },
            "order_endpoint": "/api/wallet/recharge",
            "order_method": "POST",
            "quote_type": "retail",
            "required_order_field": "price_quote_id",
        },
        "agent-inventory-purchase": {
            "active_route": "/agent/inventory",
            "catalog_endpoint": "/api/pricing/procurement/catalog",
            "quote_endpoints": {
                "fixed": "/api/pricing/procurement/quote",
                "custom_amount": "/api/agent/inventory/purchase-preview",
            },
            "order_endpoint": "/api/agent/inventory/purchase",
            "order_method": "POST",
            "quote_type": "procurement",
            "required_order_field": "price_quote_id",
        },
    }

    if frontend_document.get("schema_version") != 1:
        problems.append("GEO 购买行为契约 schema_version 非法")
    if frontend_document.get("contract_version") != "geo-persisted-price-quote-v2":
        problems.append("GEO 购买行为契约版本不匹配")
    if frontend_document.get("scope") != "geo":
        problems.append("购买行为契约范围必须严格为 geo")
    frontend_rows = frontend_document.get("entries")
    frontend_rows = frontend_rows if isinstance(frontend_rows, list) else []
    frontend_by_id = {
        str(row.get("entry_id")): row
        for row in frontend_rows
        if isinstance(row, dict) and row.get("entry_id")
    }
    if len(frontend_by_id) != len(frontend_rows):
        problems.append("GEO 购买行为契约存在空或重复 entry_id")
    if set(frontend_by_id) != set(expected):
        problems.append("GEO 现役购买入口清单必须且只能包含客户充值和服务商进货")

    def _route_present(router, path: str, method: str) -> bool:
        return any(
            getattr(route, "path", None) == path
            and method in (getattr(route, "methods", None) or set())
            for route in router.routes
        )

    active_entries: List[Dict[str, Any]] = []
    for entry_id, contract in expected.items():
        entry_problems: List[str] = []
        frontend = frontend_by_id.get(entry_id)
        if not frontend:
            entry_problems.append("前端结构化契约缺失")
        else:
            for field, expected_value in contract.items():
                if frontend.get(field) != expected_value:
                    entry_problems.append(f"前端契约字段 {field} 不匹配")

        backend = backend_behaviors.get(entry_id)
        if not isinstance(backend, dict):
            entry_problems.append("后端行为契约缺失")
        else:
            for field in (
                "contract_version", "entry_id", "active_route", "quote_type",
                "order_endpoint", "required_order_field",
            ):
                expected_value = (
                    "geo-persisted-price-quote-v2" if field == "contract_version"
                    else entry_id if field == "entry_id"
                    else contract.get(field)
                )
                if backend.get(field) != expected_value:
                    entry_problems.append(f"后端行为字段 {field} 不匹配")
            if backend.get("quote_persistence") != "price_quotes":
                entry_problems.append("报价未声明持久化到 price_quotes")
            if backend.get("callback_pricing_source") != "order_snapshot":
                entry_problems.append("回调未声明只读订单快照")
            if backend.get("quote_required_enforcement") != "request_pre_writer":
                entry_problems.append("强制报价未声明在订单 writer 前执行")
            if entry_id == "agent-inventory-purchase":
                if backend.get("cash_semantics") != "payable_amount_immutable":
                    entry_problems.append("服务商进货未声明金额锚定语义")
                if backend.get("relationship_effect") != "paid_inventory_points_only":
                    entry_problems.append("服务商进货未声明关系只影响到账算力")
            if entry_id == "customer-recharge":
                if backend.get("supports_custom_amount") is not True:
                    entry_problems.append("客户充值后端未声明支持自由金额持久化报价")
                if backend.get("cash_semantics") != "payable_amount_immutable":
                    entry_problems.append("客户自由充值未声明金额锚定语义")
                if backend.get("relationship_effect") != "delivered_points_only":
                    entry_problems.append("客户自由充值未声明关系只影响到账算力")

        if entry_id == "customer-recharge":
            route_checks = {
                "catalog": _route_present(
                    pricing_ssot_api.router, contract["catalog_endpoint"], "GET"
                ),
                "fixed_quote": _route_present(
                    pricing_ssot_api.router, contract["quote_endpoints"]["fixed"], "POST"
                ),
                "custom_amount_quote": _route_present(
                    pricing_ssot_api.router,
                    contract["quote_endpoints"]["custom_amount"],
                    "POST",
                ),
                "order": _route_present(wallet_api.router, contract["order_endpoint"], "POST"),
            }
            order_fields = set(wallet_api.RechargeRequest.model_fields)
            quote_fields = set(pricing_ssot_api.RetailQuoteRequest.model_fields)
            custom_quote_fields = set(
                pricing_ssot_api.RetailCustomAmountQuoteRequest.model_fields
            )
            if not {"price_quote_id", "idempotency_key"}.issubset(order_fields):
                entry_problems.append("客户订单请求模型未强制具备报价/幂等字段")
            if not {"product_code", "idempotency_key"}.issubset(quote_fields):
                entry_problems.append("客户报价请求模型字段不完整")
            if not {
                "amount_cents", "idempotency_key", "terms_acceptance_id"
            }.issubset(custom_quote_fields):
                entry_problems.append("客户自由金额报价请求模型字段不完整")
        else:
            route_checks = {
                "catalog": _route_present(
                    pricing_ssot_api.router, contract["catalog_endpoint"], "GET"
                ),
                "fixed_quote": _route_present(
                    pricing_ssot_api.router, contract["quote_endpoints"]["fixed"], "POST"
                ),
                "custom_amount_quote": _route_present(
                    agent_workbench_api.router,
                    contract["quote_endpoints"]["custom_amount"],
                    "POST",
                ),
                "order": _route_present(
                    agent_workbench_api.router, contract["order_endpoint"], "POST"
                ),
            }
            order_fields = set(agent_workbench_api.AgentPurchaseWiringRequest.model_fields)
            preview_fields = set(
                agent_workbench_api.AgentPurchasePreviewWiringRequest.model_fields
            )
            preview_response_fields = set(
                agent_workbench_api.AgentPurchasePreviewWiringResponse.model_fields
            )
            quote_fields = set(pricing_ssot_api.ProcurementQuoteRequest.model_fields)
            if not {"price_quote_id", "idempotency_key"}.issubset(order_fields):
                entry_problems.append("服务商订单请求模型未具备报价/幂等字段")
            if not {"amount_cents", "idempotency_key"}.issubset(preview_fields):
                entry_problems.append("自由金额报价请求模型字段不完整")
            if "price_quote_id" not in preview_response_fields:
                entry_problems.append("自由金额预览未返回持久化报价 ID")
            if not {"product_code", "idempotency_key"}.issubset(quote_fields):
                entry_problems.append("固定进货报价请求模型字段不完整")
            if not backend or backend.get("supports_custom_amount") is not True:
                entry_problems.append("后端未声明支持自由金额持久化报价")

        missing_routes = sorted(name for name, present in route_checks.items() if not present)
        if missing_routes:
            entry_problems.append(f"FastAPI 路由缺失: {','.join(missing_routes)}")
        if entry_problems:
            problems.extend(f"{entry_id}: {item}" for item in entry_problems)
        active_entries.append({
            "entry_id": entry_id,
            "active_route": contract["active_route"],
            "quote_type": contract["quote_type"],
            "route_checks": route_checks,
            "submits_persisted_price_quote_id": not entry_problems,
        })

    return {
        "ready": not problems,
        "retail_backend_capability": retail_capability,
        "procurement_backend_capability": procurement_capability,
        "contract_version": frontend_document.get("contract_version"),
        "active_geo_entries": active_entries,
        "frontend_source_evidence": frontend_by_id,
        "active_legacy_bypasses": [],
        "warnings": warnings,
        "problems": problems,
    }


def _read_flags(cur) -> Dict[str, Any]:
    keys = ["PRICING_DUAL_SSOT_ENABLED", "CHANNEL_PRICING_ENABLED", "PRICING_QUOTE_REQUIRED"]
    cur.execute("SELECT key, value FROM system_settings WHERE key=ANY(%s)", (keys,))
    raw = {row["key"]: str(row["value"]).strip().lower() in ("1", "true", "yes", "on")
           for row in cur.fetchall()}
    flags = {key: bool(raw.get(key, False)) for key in keys}
    problems: List[str] = []
    if flags["PRICING_QUOTE_REQUIRED"] and not flags["PRICING_DUAL_SSOT_ENABLED"]:
        problems.append("PRICING_QUOTE_REQUIRED 不能先于 PRICING_DUAL_SSOT_ENABLED")
    if flags["CHANNEL_PRICING_ENABLED"] and not flags["PRICING_DUAL_SSOT_ENABLED"]:
        problems.append("CHANNEL_PRICING_ENABLED 开启前必须启用双价目表")
    return {"ready": not problems, "values": flags, "problems": problems}


def get_readiness() -> Dict[str, Any]:
    """返回 200-ready=false 风格数据；调用方只负责 admin RBAC 和 HTTP 包装。"""
    checks: Dict[str, Any] = {}
    blockers: List[str] = []
    warnings: List[str] = []
    try:
        codes = account_codes.account_code_status()
        checks["public_account_codes"] = codes
        blockers.extend(codes.get("blockers", []))
    except Exception as exc:
        checks["public_account_codes"] = {"ready": False, "error": str(exc)}
        blockers.append(f"公开编号状态检查失败: {exc}")

    try:
        relationships = channel_pricing.relationship_status()
        checks["channel_relationships"] = relationships
        blockers.extend(relationships.get("blockers", []))
    except Exception as exc:
        checks["channel_relationships"] = {"ready": False, "error": str(exc)}
        blockers.append(f"渠道关系检查失败: {exc}")

    try:
        with get_db() as conn:
            cur = conn.cursor()
            epoch = read_config_epoch_strict(cursor=cur)
            checks["cache_freshness"] = {
                "ready": isinstance(epoch, int) and epoch >= 0,
                "pricing_config_epoch": epoch,
                "catalog_cache": "postgres_direct",
                "redis": "not_required",
                "reason": "已发布目录和报价均直接读取 PostgreSQL；无 Redis 目录缓存",
            }
            schema = _check_schema(cur)
            checks["schema"] = schema
            blockers.extend(schema["problems"])
            catalogs = _check_catalogs(cur, checks["public_account_codes"])
            checks["catalogs"] = catalogs
            blockers.extend(catalogs["problems"])
            orders = _check_order_snapshots(cur)
            checks["orders_and_quotes"] = orders
            blockers.extend(orders["problems"])
            flags = _read_flags(cur)
            checks["flags"] = flags
            blockers.extend(flags["problems"])
    except Exception as exc:
        checks["database"] = {"ready": False, "error": str(exc)}
        blockers.append(f"数据库 readiness 检查失败: {exc}")

    wiring = _check_entry_wiring()
    checks["entry_wiring"] = wiring
    blockers.extend(wiring["problems"])
    warnings.extend(wiring.get("warnings", []))
    blockers = list(dict.fromkeys(str(blocker) for blocker in blockers if blocker))
    warnings = list(dict.fromkeys(str(warning) for warning in warnings if warning))
    return {
        "ready": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "flags_changed": False,
    }
