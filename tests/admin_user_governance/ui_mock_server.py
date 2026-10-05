"""Deterministic HTTP fixture for browser-only admin governance visual checks.

This server is intentionally isolated from PostgreSQL.  It lets Playwright render the
real Vite application, authentication guard, navigation, and governance page without
ever pointing a browser at production data.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


USERS = [
    {
        "user_id": 124,
        "username": "13800138124",
        "display_name": "林若晴 · 华东区品牌增长与数字化运营中心",
        "phone": "13800138124",
        "is_active": True,
        "business_identity": "ordinary_user",
        "platform_access": "standard",
        "total_points": 18240,
        "customer_count": 0,
        "brand_count": 2,
        "service_mode": "service_provider",
        "needs_attention": True,
        "attention_label": "旧人工归属缺少直接审计",
        "created_at": "2026-07-02T08:16:00+08:00",
        "last_active_at": "2026-07-15T09:40:00+08:00",
        "versions": {"business_identity": 1, "commercial_binding": 4, "channel_relationship": 1, "platform_access": 1, "password_security": 1, "wallet_adjustment": 1},
    },
    {
        "user_id": 129,
        "username": "13900138129",
        "display_name": "赵辰",
        "phone": "13900138129",
        "is_active": True,
        "business_identity": "ordinary_user",
        "platform_access": "standard",
        "total_points": 7600,
        "customer_count": 0,
        "brand_count": 1,
        "service_mode": "service_provider",
        "needs_attention": False,
        "attention_label": None,
        "created_at": "2026-07-06T10:00:00+08:00",
        "last_active_at": "2026-07-14T18:22:00+08:00",
        "versions": {"business_identity": 1, "commercial_binding": 2, "channel_relationship": 1, "platform_access": 1, "password_security": 1, "wallet_adjustment": 1},
    },
    {
        "user_id": 28,
        "username": "agent-sv-028",
        "display_name": "云峰品牌增长服务中心（华东大区长期服务主体）",
        "phone": None,
        "is_active": True,
        "business_identity": "service_provider",
        "platform_access": "standard",
        "total_points": 356000,
        "customer_count": 18,
        "brand_count": 26,
        "service_mode": "platform_direct",
        "needs_attention": False,
        "attention_label": None,
        "created_at": "2026-04-16T08:00:00+08:00",
        "last_active_at": "2026-07-15T10:02:00+08:00",
        "versions": {"business_identity": 3, "commercial_binding": 1, "channel_relationship": 2, "platform_access": 1, "password_security": 1, "wallet_adjustment": 1},
    },
    {
        "user_id": 102,
        "username": "ref-102",
        "display_name": "顾问周宁",
        "phone": None,
        "is_active": True,
        "business_identity": "ordinary_user",
        "platform_access": "standard",
        "total_points": 1200,
        "customer_count": 0,
        "brand_count": 0,
        "service_mode": "platform_direct",
        "needs_attention": False,
        "attention_label": None,
        "created_at": "2026-05-20T08:00:00+08:00",
        "last_active_at": "2026-07-12T12:00:00+08:00",
        "versions": {"business_identity": 1, "commercial_binding": 1, "channel_relationship": 1, "platform_access": 1, "password_security": 1, "wallet_adjustment": 1},
    },
    {
        "user_id": 123,
        "username": "agent-123",
        "display_name": "沪上增长伙伴（上海）有限公司",
        "phone": None,
        "is_active": True,
        "business_identity": "service_provider",
        "platform_access": "standard",
        "total_points": 82000,
        "customer_count": 6,
        "brand_count": 9,
        "service_mode": "platform_direct",
        "needs_attention": False,
        "attention_label": None,
        "created_at": "2026-05-18T08:00:00+08:00",
        "last_active_at": "2026-07-15T12:00:00+08:00",
        "versions": {"business_identity": 2, "commercial_binding": 1, "channel_relationship": 1, "platform_access": 1, "password_security": 1, "wallet_adjustment": 1},
    },
]


def actor(user_id: int, username: str, display_name: str, identity: str, **extra):
    return {
        "user_id": user_id,
        "username": username,
        "display_name": display_name,
        "is_active": True,
        "company": extra.get("company"),
        "business_identity": identity,
        "service_code": extra.get("service_code"),
        "channel_code": extra.get("channel_code"),
    }


def detail(user_id: int):
    base = next((item for item in USERS if item["user_id"] == user_id), USERS[0])
    is_124 = user_id == 124
    is_129 = user_id == 129
    provider = actor(
        123 if is_124 else 28,
        "agent-123" if is_124 else "agent-sv-028",
        "沪上增长伙伴（上海）有限公司" if is_124 else "云峰品牌增长服务中心（华东大区长期服务主体）",
        "service_provider",
        company="上海全域增长顾问有限公司" if is_124 else "杭州云峰数字科技集团有限公司",
        service_code="SV-00123" if is_124 else "SV-00028",
        channel_code="CH-3101" if is_124 else "CH-2801",
    )
    inviter = actor(
        102,
        "ref-102",
        "顾问周宁",
        "ordinary_user",
        company="宁波启航品牌咨询工作室",
    ) if is_129 else None
    evidence = {
        "status": "incomplete" if is_124 else "complete",
        "label": "旧人工归属缺少直接操作凭证" if is_124 else "直接治理审计完整",
        "operator_user_id": None if is_124 else 1,
        "operator_name": None if is_124 else "平台治理管理员",
        "reason": None if is_124 else "客户签署新的年度服务协议，按审批单调整商业服务归属。",
        "request_id": None if is_124 else "req-governance-20260715-0008",
        "happened_at": "2026-06-28T10:20:00+08:00" if is_124 else "2026-07-12T16:28:00+08:00",
    }
    relationship_notices = []
    if is_124:
        relationship_notices.append({
            "code": "HISTORICAL_EVIDENCE_INCOMPLETE",
            "severity": "warning",
            "title": "旧人工归属缺少直接审计",
            "detail": "该旧人工绑定没有可直接关联的操作人和原因；当前关系仍有效，可按现状复核。",
        })
    if is_129:
        relationship_notices.append({
            "code": "DUAL_RELATIONSHIPS_PRESENT",
            "severity": "info",
            "title": "双关系并存",
            "detail": "邀请来源与当前商业服务商不同，这是两个独立事实，不代表数据冲突。",
        })
    return {
        "success": True,
        "overview": {
            "user_id": user_id,
            "username": base["username"],
            "display_name": base["display_name"],
            "phone": base.get("phone"),
            "company": "上海星河消费科技与品牌管理有限公司（全国业务运营主体）" if is_124 else None,
            "is_active": base["is_active"],
            "business_identity": base["business_identity"],
            "business_identity_label": "服务商" if base["business_identity"] == "service_provider" else "普通用户",
            "platform_access": base["platform_access"],
            "platform_access_label": "内部管理员" if base["platform_access"] == "administrator" else "无管理员权限",
            "total_points": base["total_points"],
            "paid_points": 16000,
            "bonus_points": 2240,
            "total_recharged_points": 32000,
            "customer_count": base["customer_count"],
            "brand_count": base["brand_count"],
            "created_at": base["created_at"],
            "last_login_at": "2026-07-15T09:35:00+08:00",
            "last_active_at": base["last_active_at"],
            "versions": base["versions"],
        },
        "relationships": {
            "registration": {
                "present": inviter is not None,
                "inviter": inviter,
                "source": "referral_links" if inviter else "none",
                "registered_at": "2026-07-06T10:00:00+08:00" if inviter else None,
                "legacy_pointer_user_id": 102 if inviter else None,
                "evidence": {
                    "status": "complete" if inviter else "not_required",
                    "label": "注册邀请链路完整" if inviter else "无邀请来源",
                    "operator_user_id": None,
                    "operator_name": None,
                    "reason": "邀请码 REF-0102" if inviter else None,
                    "request_id": None,
                    "happened_at": "2026-07-06T10:00:00+08:00" if inviter else None,
                },
            },
            "commercial": {
                "mode": "service_provider",
                "provider": provider,
                "binding_id": 30 if is_124 else 42,
                "binding_source": "admin_manual" if is_124 else "invite_code",
                "binding_source_label": "管理员人工设置" if is_124 else "服务邀请码绑定",
                "bound_at": "2026-06-28T10:20:00+08:00" if is_124 else "2026-07-06T10:03:58+08:00",
                "relationship_version": base["versions"]["commercial_binding"],
                "dispute_status": None,
                "evidence": evidence,
            },
            "channel": {
                "mode": "not_applicable" if base["business_identity"] == "ordinary_user" else "upstream_channel",
                "upstream": None if base["business_identity"] == "ordinary_user" else actor(
                    123, "agent-123", "沪上增长伙伴（上海）有限公司", "service_provider",
                    service_code="SV-00123", channel_code="CH-3101",
                ),
                "relationship_version": None if base["business_identity"] == "ordinary_user" else "fixture-channel-v2",
                "cost_multiplier_bps": None if base["business_identity"] == "ordinary_user" else 12000,
                "effective_from": None if base["business_identity"] == "ordinary_user" else "2026-07-15T10:00:00+08:00",
                "reason": None if base["business_identity"] == "ordinary_user" else "运营审批设置直属渠道",
            },
            "dual_relationships_present": is_129,
            "dual_relationships_label": "邀请归属与商业服务关系同时存在，语义独立" if is_129 else None,
            "notices": relationship_notices,
        },
        "pricing_and_settlement": {
            "customer_pricing_route": "普通客户公开价目表",
            "procurement_pricing_route": "不适用：该账号不是服务商" if base["business_identity"] == "ordinary_user" else "服务商进货目录与专属折扣",
            "settlement_route": "由当前商业服务商承接，订单仍按不可变快照结算",
            "pricing_source": "全局公开售价",
            "special_pricing_note": "没有服务商专属价格覆盖",
        },
        "clients_and_brands": {
            "clients": [],
            "brands": [
                {"brand_id": 811, "name": "星河研选·家庭健康生活方式旗舰品牌", "industry": "消费健康", "status": "服务中"},
                {"brand_id": 812, "name": "LUMENA 长名称国际化新消费品牌亚太事业部", "industry": "新消费", "status": "资料完善中"},
            ] if is_124 else [],
        },
        "wallet_and_billing": {
            "paid_points": 16000,
            "bonus_points": 2240,
            "total_points": base["total_points"],
            "total_recharged_points": 32000,
            "recent_orders": [
                {"order_id": "R20260712000124", "amount_yuan": "¥1,200.00", "status_label": "已支付", "created_at": "2026-07-12T11:22:00+08:00", "paid_at": "2026-07-12T11:24:00+08:00"},
            ],
            "recent_transactions": [
                {"transaction_id": 8871, "direction_label": "收入", "points": 12000, "description": "算力充值到账（只读展示）", "created_at": "2026-07-12T11:24:00+08:00"},
            ],
        },
        "permissions_and_security": {
            "platform_access": base["platform_access"],
            "account_status_label": "正常",
            "must_change_password": False,
            "permission_version": 7,
            "legacy_roles": [
                {"role_id": 6, "internal_name": "geo_writer", "historical_label": "历史角色：GEO 编辑", "compatibility_status": "read_only_legacy"},
            ] if is_124 else [],
        },
        "operation_logs": [
            {
                "audit_id": 91,
                "scope": "commercial_binding",
                "action_label": "调整商业服务归属",
                "operator_user_id": 1,
                "operator_name": "平台治理管理员",
                "request_id": "req-governance-20260715-0008",
                "reason": "客户签署新的年度服务协议，按审批单调整商业服务归属。",
                "before_summary": "平台直营",
                "after_summary": provider["display_name"],
                "version_before": 3,
                "version_after": 4,
                "created_at": "2026-07-12T16:28:00+08:00",
            },
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib callback name
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/auth/me":
            return self._send({
                "success": True,
                "user": {
                    "id": 1,
                    "username": "admin-governance",
                    "display_name": "平台治理管理员",
                    "is_admin": True,
                    "is_active": 1,
                    "must_change_password": 0,
                    "roles": [{"id": 1, "name": "admin", "display_name": "系统管理员"}],
                    "permissions": ["users.view", "users.edit", "audit.view"],
                    "client_brand_ids": [],
                    "agent_level": 0,
                },
            })
        if path == "/api/admin/user-governance/platform-direct-readiness":
            return self._send({
                "success": True,
                "readiness": {
                    "configured": True,
                    "ready": True,
                    "status": "ready",
                    "label": "平台直营服务账号已就绪",
                    "service_user": actor(28, "agent-sv-028", "云峰品牌增长服务中心", "service_provider", service_code="SV-00028", channel_code="CH-2801"),
                    "checks": ["账号存在且已启用", "业务身份为服务商", "不是超级管理员个人账号"],
                },
            })
        if path == "/api/admin/user-governance/users":
            params = parse_qs(parsed.query)
            items = USERS
            wanted_identity = params.get("identity", [None])[0]
            if wanted_identity:
                items = [item for item in items if item["business_identity"] == wanted_identity]
            if params.get("attention_only", ["false"])[0] == "true":
                items = [item for item in items if item["needs_attention"]]
            search = params.get("search", [""])[0].strip().lower()
            if search:
                items = [item for item in items if search in item["display_name"].lower() or search in item["username"].lower()]
            return self._send({"success": True, "users": items, "total": len(items), "page": 1, "page_size": 100})
        if path.startswith("/api/admin/user-governance/users/"):
            try:
                user_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._send({"detail": "not found"}, 404)
            return self._send(detail(user_id))
        if path.startswith("/api/user/notifications"):
            if path.endswith("unread-count"):
                return self._send({"status": "success", "count": 0})
            return self._send({"status": "success", "notifications": []})
        return self._send({"success": True})

    def do_POST(self):  # noqa: N802 - stdlib callback name
        if self.path == "/api/auth/refresh":
            return self._send({"success": True, "token": "playwright-admin-token"})
        return self._send({"success": True})

    def do_PUT(self):  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/password"):
            return self._send({
                "success": True,
                "scope": "password_security",
                "version": int(body.get("expected_version", 1)) + 1,
                "request_id": "fixture-password-reset",
                "before": {"password_state": "active"},
                "after": {"password_state": "reset_required"},
            })
        if self.path.endswith("/wallet-adjustment"):
            return self._send({
                "success": True,
                "scope": "wallet_adjustment",
                "version": int(body.get("expected_version", 1)) + 1,
                "request_id": "fixture-wallet-adjustment",
                "before": {"paid_points": 18240, "bonus_points": 0},
                "after": {
                    "paid_points": 18240 + int(body.get("amount", 0)),
                    "bonus_points": 0,
                },
            })
        if self.path.endswith("/channel-relationship"):
            upstream = body.get("upstream_user_id")
            return self._send({
                "success": True,
                "scope": "channel_relationship",
                "version": int(body.get("expected_version", 1)) + 1,
                "request_id": "fixture-channel-change",
                "before": {
                    "channel_upstream_user_id": 123,
                    "channel_mode": "upstream_channel",
                    "cost_multiplier_bps": 12000,
                },
                "after": {
                    "channel_upstream_user_id": upstream,
                    "channel_mode": "upstream_channel" if upstream else "platform_root",
                    "cost_multiplier_bps": body.get("cost_multiplier_bps") if upstream else None,
                },
            })
        return self._send({"success": True})

    def log_message(self, format, *args):  # noqa: A002 - stdlib callback name
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8015), Handler).serve_forever()
