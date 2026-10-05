def test_masking_helpers_match_public_contract():
    from utils.masking import mask_email, mask_phone, mask_wechat

    assert mask_phone("13812345678") == "138****5678"
    assert mask_wechat("wechat888") == "wec***88"
    assert mask_email("agent@example.com") == "a***@example.com"


def test_get_public_whitelabel_data_resolves_quote_owner_and_masks(monkeypatch):
    from services import public_whitelabel

    rows = [
        {"owner_user_id": 17},
        {
            "company_name": "Acme Growth",
            "logo_url": "https://cdn/logo.png",
            "slogan": "增长更清楚",
            "contact_name": "顾问小林",
            "contact_phone": "13812345678",
            "contact_wechat": "wechat888",
            "contact_email": "agent@example.com",
            "brand_color": "#111827",
            # v3.6:customer surface 展示需 mode/status/unlocked 授权（is_whitelabel_active_for_customer）
            "whitelabel_mode": "external_only",
            "whitelabel_status": "active",
            "unlocked_by_admin": True,
        },
    ]

    class Cursor:
        def execute(self, query, params):
            pass

        def fetchone(self):
            return rows.pop(0)

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(public_whitelabel, "get_connection", lambda: Conn())

    payload = public_whitelabel.get_public_whitelabel_data(quote_id=99)

    assert payload["display_scope"] == "approved_whitelabel"
    assert "resolved_user_id" not in payload
    assert "agent_level" not in payload
    assert payload["whitelabel"]["contact_phone"] == "138****5678"
    assert payload["whitelabel"]["contact_wechat"] == "wec***88"
    assert payload["whitelabel"]["contact_email"] == "a***@example.com"
