import asyncio
import inspect
from types import SimpleNamespace

import api.admin_llm_cost_api as llm_cost_api
import api.faq_api as faq_api
import db.auth_db as auth_db
import api.referral_api as referral_api
from api.referral_api import _normalize_quote_services
from api.wallet_api import serialize_pricing_for_user


def test_wallet_pricing_serializer_hides_internal_fields_for_non_admin():
    row = {
        "feature_code": "article_gen",
        "feature_name": "AI 写文章",
        "cost_points": 390,
        "cost_compute": 3,
        "requires_paid_points": True,
        "is_active": True,
        "wholesale_cents": 123,
        "wholesale_points": 9,
        "platform_cost_cents": 88,
        "raw_cost": "secret",
        "margin": 0.8,
        "cost_basis": "internal",
    }

    safe = serialize_pricing_for_user([row], is_admin=False)[0]
    assert set(safe) == {
        "feature_code",
        "feature_name",
        "cost_points",
        "cost_compute",
        "requires_paid_points",
        "is_active",
    }
    for key in (
        "wholesale_cents",
        "wholesale_points",
        "platform_cost_cents",
        "raw_cost",
        "margin",
        "cost_basis",
    ):
        assert key not in safe

    admin = serialize_pricing_for_user([row], is_admin=True)[0]
    assert admin["wholesale_cents"] == 123
    assert admin["cost_basis"] == "internal"


def test_llm_cost_by_client_uses_brands_name(monkeypatch):
    captured = {}

    def fake_query(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return [
            {
                "brand_id": 1,
                "brand_name": "测试客户",
                "calls": 2,
                "cost": 1.23,
                "last_quote_status": "paid",
                "last_paid_amount": 100,
            }
        ]

    monkeypatch.setattr(llm_cost_api, "_query_db", fake_query)
    req = SimpleNamespace(state=SimpleNamespace(user={"is_admin": True}))

    data = asyncio.run(llm_cost_api.llm_cost_by_client(req, days=7, top=5))

    assert "b.name AS brand_name" in captured["sql"]
    assert "b.brand_name" not in captured["sql"]
    assert "GROUP BY l.brand_id, b.name" in captured["sql"]
    assert data["items"][0]["brand_name"] == "测试客户"


def test_role_templates_match_prod_role_ssot_before_reseed_updates_roles():
    expected = {
        "geo_writer": {
            "display_name": "GEO编辑（未启用）",
            "permissions": {
                ("dashboard", "read"),
                ("diagnosis", "read"), ("diagnosis", "write"),
                ("history", "read"),
                ("insights", "read"), ("insights", "write"),
                ("monitoring", "read"), ("monitoring", "write"),
                ("reports", "read"), ("reports", "write"),
                ("social", "read"), ("social", "write"),
                ("writing", "read"), ("writing", "write"),
            },
        },
        "social_ops": {
            "display_name": "普通用户",
            "permissions": {
                ("diagnosis", "read"), ("diagnosis", "write"),
                ("history", "read"),
                ("insights", "read"),
                ("monitoring", "read"), ("monitoring", "write"),
                ("placement", "read"),
                ("publish", "read"), ("publish", "write"),
                ("quote", "read"), ("quote", "write"),
                ("reports", "read"), ("reports", "write"),
                ("social", "read"), ("social", "write"),
                ("writing", "read"), ("writing", "write"),
            },
        },
        "sales": {
            "display_name": "销售顾问（未启用）",
            "permissions": {
                ("diagnosis", "read"), ("diagnosis", "write"),
                ("history", "read"),
                ("monitoring", "read"),
                ("quote", "read"), ("quote", "write"),
                ("reports", "read"),
                ("social", "read"), ("social", "write"),
            },
        },
        "geo_user_basic": {"display_name": "GEO 用户", "permissions": set()},
        "geo_agent_full": {"display_name": "GEO 代理", "permissions": set()},
        "finance_reviewer": {"display_name": "财务审核员", "permissions": set()},
    }

    for role_name, role_expectation in expected.items():
        template = auth_db.ROLE_TEMPLATES[role_name]
        assert template["display_name"] == role_expectation["display_name"]
        assert set(template["permissions"]) == role_expectation["permissions"]


def test_seed_roles_patches_existing_roles_instead_of_skipping():
    src = inspect.getsource(auth_db._seed_roles)
    existing_branch = src.split("if existing:", 1)[1].split("else:", 1)[0]
    assert "continue" not in existing_branch
    assert "UPDATE roles" in existing_branch
    assert "ON CONFLICT DO NOTHING" in src


def test_admin_feedback_pending_status_is_valid(monkeypatch):
    monkeypatch.setattr(faq_api, "list_feedback", lambda **kwargs: [])
    req = SimpleNamespace(state=SimpleNamespace(user={"is_admin": True, "id": 1}))

    data = asyncio.run(faq_api.admin_list_feedback(req, status="pending", kind="bug"))

    assert data == {"items": []}


def test_agent_quote_services_are_normalized_without_changing_total_ssot():
    services = [
        {"name": "A", "quantity": "2", "unit_price": "99.5"},
        {"name": "B", "price": "30"},
        {"name": "C", "quantity": 0, "unit_price": 10, "subtotal": "5"},
        {"name": "D", "quantity": 3, "unit_price": 20, "subtotal": 0},
    ]

    out = _normalize_quote_services(services)

    assert out[0]["quantity"] == 2
    assert out[0]["unit_price"] == 99.5
    assert out[0]["subtotal"] == 199
    assert out[1]["quantity"] == 1
    assert out[1]["unit_price"] == 30
    assert out[1]["subtotal"] == 30
    assert out[2]["quantity"] == 1
    assert out[2]["subtotal"] == 5
    assert out[3]["subtotal"] == 60


def test_public_quote_read_normalizes_legacy_zero_subtotals(monkeypatch):
    class FakeCursor:
        def execute(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchone(self):
            return {
                "services": '[{"name":"旧报价项","quantity":3,"unit_price":20,"subtotal":0}]',
                "total_price": 120,
                "whitelabel": "{}",
                "created_at": "2026-07-02T00:00:00",
            }

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            self.closed = True

    monkeypatch.setattr(referral_api, "get_connection", lambda: FakeConnection())

    data = asyncio.run(referral_api.get_public_quote("share123"))

    service = data["quote"]["services"][0]
    assert service["quantity"] == 3
    assert service["unit_price"] == 20
    assert service["subtotal"] == 60
    assert data["quote"]["total_price"] == 120
