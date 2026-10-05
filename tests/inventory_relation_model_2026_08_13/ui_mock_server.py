"""invrel 包 · 浏览器验收用的确定性 HTTP 夹具(与 PostgreSQL 完全隔离)。

沿用 `tests/admin_user_governance/ui_mock_server.py` 的既有做法:
让 Playwright 渲染**真实的** Vite 应用 / 鉴权守卫 / 路由 / 页面,
而浏览器永远不指向生产数据。

场景开关:`GET /__scenario?name=zero|stocked|error`
  · zero    库存 0        → 必须出现可点的「去进货」,不得有灰色死按钮
  · stocked 库存 > 0      → 出现「转为可用算力」
  · error   余额接口 500  → 出现「重新读取」,且不得把未知显示成 0
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCENARIO = {"name": "zero"}

AGENT_USER = {
    "id": 946001,
    "username": "13900000001",
    "display_name": "上游服务商",
    "is_admin": False,
    "is_active": 1,
    "must_change_password": 0,
    "roles": [{"id": 2, "name": "agent", "display_name": "服务商"}],
    "permissions": ["agent.inventory"],
    "client_brand_ids": [],
    "agent_level": 2,
}

# 🔴 搜索命中「我的下线服务商」—— 这正是改造前返回空的那条(工单 §1.3)。
#
# 🔴 [xfer 工单 v2 §R1 · 判据判别力] `brand_name` 这里**故意给非空值**。
#    真实后端修好后会回 NULL,但那样一来前端标题判据就恒绿了 ——
#    `c.brand_name || c.display_name` 在 brand_name=null 时本来就取 display_name,
#    等于测了个空气。夹具模拟"后端仍在泄露"(旧后端 / 将来回退)的最坏输入,
#    前端**第二道**必须自己顶住:服务商标题只能是 display_name。
#    后端那一位真的被拿掉了,由 pytest
#    `test_transfer_lookup_provider_name_2026_08_17.py::test_r1_*` 打。
PROVIDER_LEAKED_BRAND = "贵州省禾椒香食品有限公司"

DOWNSTREAM_ITEM = {
    "customer_user_id": 946002,
    "display_name": "下线服务商",
    "phone_masked": "139****0002",
    "brand_name": PROVIDER_LEAKED_BRAND,
    "binding_status": "downstream_partner",
    "tool_credit_points": 0,
    "publish_credit_points": 0,
    "bonus_credit_points": 0,
    "relation": "downstream_partner",
    "target_identity": "service_provider",
    "headline": "这是你的下线服务商",
    "effect_note": (
        "算力进 TA 的库存算力,TA 可以继续向下分销,也可以在自己的库存中心"
        "按 1:1 转成可用算力自用。成本按你们已约定的进货价计。"
    ),
    "primary_action": {"label": "供货给下线", "action": "supply_downstream", "route": None},
    "secondary_actions": [
        {"label": "查看渠道关系", "action": "view_channel", "route": "/agent/channel-partners"},
        {"label": "复制进货提醒", "action": "copy_purchase_message", "route": None},
    ],
    "allowed_actions": ["supply_downstream", "view_channel", "copy_purchase_message"],
    "ledger_note": "inventory_wallet",
}

# [xfer 工单 v2 §R1 双向 + §R3] 普通客户目标 —— 标题**必须**还是品牌名(反向对照:
# 证明"服务商不用品牌名"不是把所有人的品牌名都砍了)。
# 余额两个数取自客户 `user_wallets`(充值 / 赠送),且**刻意互不相等且都非 0**,
# 否则判据分不出是真读到了还是碰巧都是 0。
PLAIN_CUSTOMER_ITEM = {
    "customer_user_id": 946003,
    "display_name": "张三",
    "phone_masked": "139****0003",
    "brand_name": "普通客户的品牌",
    "binding_status": "owned",
    "tool_credit_points": 52000,
    "publish_credit_points": 0,
    "bonus_credit_points": 4200,
    "relation": "customer",
    "target_identity": "level0",
    "headline": "这是你的客户",
    "effect_note": "算力进 TA 的可用算力,用于诊断、写作等功能消耗,不能再向下分销。",
    "primary_action": {"label": "划拨给客户", "action": "allocate_customer", "route": None},
    "secondary_actions": [],
    "allowed_actions": ["allocate_customer"],
    "ledger_note": "available_wallet",
}

# 🔴 字段名必须与 `toQuotedPurchaseOption`(InventoryCenter.tsx:162)逐字对齐:
#    页面读的是 `data.items[*].product_code / cash_price_cents / paid_inventory_points …`。
#    第一版夹具照着 UI 里的 `option_id / amount_cents / base_points` 写,结果
#    `items` 为空 → 一个档位都没渲染 → 焦点断言测的其实是**兜底输入框**,
#    也就是"绿了但没测到要测的东西"。夹具字段名对不上是最常见的假绿来源。
CATALOG = {
    "success": True,
    "catalog_version": "fixture-v1",
    "tier_progress": None,
    "items": [
        {"product_code": "opt-1000", "display_name": "1000 元档", "cash_price_cents": 100000,
         "paid_inventory_points": 100000, "bonus_inventory_points": 5000,
         "total_inventory_points": 105000, "reward_description": "另赠送 5,000 算力",
         "tier_at_order": "none", "tier_bonus_rate_bps": 500,
         "crosses_tier_threshold": False, "projected_rolling_12m_yuan": 1000},
        {"product_code": "opt-5000", "display_name": "5000 元档", "cash_price_cents": 500000,
         "paid_inventory_points": 500000, "bonus_inventory_points": 40000,
         "total_inventory_points": 540000, "reward_description": "另赠送 40,000 算力",
         "tier_at_order": "none", "tier_bonus_rate_bps": 800,
         "crosses_tier_threshold": False, "projected_rolling_12m_yuan": 5000},
    ],
}


# [P0-C §9] 一条**亮灯**的人工归属 —— 补录按钮必须因此出现
ADMIN_LIST_USER = {
    "user_id": 946101, "username": "13920000002", "display_name": "人工归属客户",
    "phone": "13920000002", "is_active": True, "business_identity": "ordinary_user",
    "platform_access": "standard", "total_points": 0, "customer_count": 0, "brand_count": 0,
    "service_mode": "service_provider", "account_origin": "self_signup",
    "needs_attention": True, "attention_label": "旧人工归属缺少直接审计",
    "created_at": None, "last_active_at": None,
    "versions": {"business_identity": 1, "commercial_binding": 3, "channel_relationship": 1,
                 "platform_access": 1, "password_security": 1, "wallet_adjustment": 1,
                 "account_status": 1},
}

ADMIN_USER_DETAIL = {
    "success": True,
    "overview": {
        **{k: ADMIN_LIST_USER[k] for k in (
            "user_id", "username", "display_name", "phone", "is_active",
            "business_identity", "platform_access", "total_points",
            "customer_count", "brand_count", "created_at", "last_active_at", "versions",
        )},
        "business_identity_label": "普通用户", "platform_access_label": "无管理员权限",
        "paid_points": 0, "bonus_points": 0, "total_recharged_points": 0,
        "company": None, "last_login_at": None,
        # 🔴 详情侧的亮灯标记(本次热修新加)—— 补录卡的显示条件
        "needs_attention": True, "attention_label": "旧人工归属缺少直接审计",
    },
    "relationships": {
        "registration": {"present": False, "source": "none", "evidence": {},
                         "account_origin": "self_signup", "organization": None},
        "commercial": {"mode": "service_provider", "provider": None, "binding_id": 946,
                       "binding_source": "admin_manual", "binding_source_label": "平台人工设置",
                       "bound_at": "2026-07-15T17:08:45+08:00", "relationship_version": 3,
                       "evidence": {}},
        "channel": {"mode": "not_applicable"},
        "dual_relationships_present": False, "notices": [],
    },
    "pricing_and_settlement": {"customer_pricing_route": "-", "procurement_pricing_route": "-",
                               "settlement_route": "-", "pricing_source": "-"},
    "clients_and_brands": {"clients": [], "brands": []},
    "wallet_and_billing": {"paid_points": 0, "bonus_points": 0, "total_points": 0,
                           "total_recharged_points": 0},
    "security_and_access": {"platform_access": "standard", "account_status_label": "正常",
                            "legacy_roles": []},
    "logs": [],
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/__scenario":
            SCENARIO["name"] = (query.get("name") or ["zero"])[0]
            return self._send({"ok": True, "scenario": SCENARIO["name"]})

        if path == "/api/auth/me":
            # scenario=admin 时换成管理员身份 —— /admin/users 需要 users 模块权限
            if SCENARIO["name"] == "admin":
                return self._send({"success": True, "user": {
                    **AGENT_USER, "id": 946100, "display_name": "平台治理管理员",
                    "is_admin": True, "agent_level": 0,
                    "roles": [{"id": 1, "name": "admin", "display_name": "系统管理员"}],
                    "permissions": ["users.view", "users.edit", "audit.view"],
                }})
            return self._send({"success": True, "user": AGENT_USER})

        # 服务商经营功能协议闸(`AgreementGate`)—— 不放行的话整页被拦截态盖住,
        # 后面所有按钮判据都会红,而红的原因跟本包毫无关系。
        if path == "/api/agent/agreement/v35-status":
            return self._send({"success": True, "status": "signed", "version": "v3.5"})

        if path == "/api/agent/inventory/balance":
            if SCENARIO["name"] == "error":
                # 🔴 故意 500:前端必须显示"重新读取",**不得**把未知显示成 0。
                return self._send({"detail": "fixture: balance unavailable"}, 500)
            stocked = SCENARIO["name"] == "stocked"
            return self._send({
                "paid_inventory_points": 120000 if stocked else 0,
                "bonus_inventory_points": 8000 if stocked else 0,
                "frozen_inventory_points": 0,
                "total_purchased_points": 128000 if stocked else 0,
                "total_allocated_points": 0,
                "alert_level": "normal" if stocked else "empty",
            })

        if path == "/api/agent/inventory/transactions":
            return self._send({"items": [], "total": 0})

        if path == "/api/pricing/procurement/catalog":
            return self._send(CATALOG)

        if path == "/api/agent/customers/lookup":
            keyword = (query.get("q") or [""])[0]
            # 只有搜"自己的下线"才命中;陌生号与不存在号一律**同一份空**(工单 §P0-2)。
            if keyword in ("13900000002", "946002", "下线服务商"):
                return self._send({"items": [DOWNSTREAM_ITEM], "total": 1})
            if keyword in ("13900000003", "946003", "张三"):
                return self._send({"items": [PLAIN_CUSTOMER_ITEM], "total": 1})
            return self._send({"items": [], "total": 0})

        if path == "/api/agent/channel-partners/requests":
            return self._send({"success": True, "items": [], "total": 0})

        if path == "/api/agent/channel-partners/preflight":
            target = int((query.get("target_user_id") or ["0"])[0])
            if target == 946002:
                return self._send({
                    "success": True, "target_user_id": 946002,
                    "target_display_name": "下线服务商",
                    "relation": "downstream_partner", "target_identity": "service_provider",
                    "headline": DOWNSTREAM_ITEM["headline"],
                    "effect_note": DOWNSTREAM_ITEM["effect_note"],
                    "entry_route": "/agent/channel-partners",
                })
            # 🔴 陌生账号 / 不存在账号 / 自己的上游 —— 同一份 404、同一句话。
            return self._send({"code": "TARGET_NOT_FOUND", "message": "未找到该账号"}, 404)

        # [P0-C §9] 管理员用户治理:列表 + 详情(详情带 needs_attention → 补录卡出现)
        if path == "/api/admin/user-governance/users":
            return self._send({"success": True, "users": [ADMIN_LIST_USER], "total": 1,
                               "page": 1, "page_size": 100})
        if path.startswith("/api/admin/user-governance/users/"):
            return self._send(ADMIN_USER_DETAIL)

        if path.startswith("/api/user/notifications"):
            if path.endswith("unread-count"):
                return self._send({"status": "success", "count": 0})
            return self._send({"status": "success", "notifications": []})

        if path == "/api/admin/agent-inventory/lot-drift":
            return self._send({"success": True, "drift_agent_count": 0,
                               "drift_total_points": 0, "drift_abs_points": 0, "agents": []})

        if path in ("/api/wallet", "/api/user/wallet", "/api/points/balance"):
            return self._send({"success": True, "paid_points": 0, "bonus_points": 0,
                               "frozen_points": 0, "agent_level": 2, "channel_tier": None})

        return self._send({"success": True})

    def do_POST(self):  # noqa: N802
        if self.path == "/api/agent/inventory/allocate-offline":
            # [xfer §R2] 划拨给普通客户 —— 前端据此断言两格映射真的发出去了。
            return self._send({
                "success": True, "customer_user_id": 946003,
                "new_tool_credit": 82000, "new_publish_credit": 0, "new_bonus_credit": 9200,
                "agent_paid_inventory_after": 90000, "agent_bonus_inventory_after": 3000,
            })
        if self.path == "/api/agent/inventory/revoke-offline":
            # [残留 1 · 增量授权 2026-08-17] 线下撤回 —— 前端据此断言两格映射真的发出去了。
            return self._send({
                "success": True, "customer_user_id": 946003,
                "new_tool_credit": 40000, "new_publish_credit": 0, "new_bonus_credit": 2200,
                "agent_paid_inventory_after": 132000, "agent_bonus_inventory_after": 10000,
            })
        if self.path == "/api/agent/inventory/supply-downstream":
            # [P0 热修] 供货成功 —— 前端据此断言"不再被阻断"
            return self._send({
                "success": True, "downstream_user_id": 946002,
                "supplied_paid": 10000, "supplied_bonus": 0,
                "agent_paid_inventory_after": 110000, "agent_bonus_inventory_after": 8000,
                "downstream_paid_inventory_after": 10000, "downstream_bonus_inventory_after": 0,
            })
        if self.path.endswith("/commercial-service-binding/audit-backfill"):
            return self._send({"success": True, "scope": "commercial_binding", "version": 4,
                               "request_id": "fixture-backfill", "backfill": True})
        if self.path == "/api/auth/refresh":
            return self._send({"success": True, "token": "playwright-agent-token"})
        return self._send({"success": True})

    def log_message(self, *args):  # noqa: A002
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8016), Handler).serve_forever()
