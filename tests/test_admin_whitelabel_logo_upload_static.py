from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_FILE = ROOT / "api" / "referral_api.py"
PAGE_FILE = ROOT / "frontend" / "src" / "pages" / "Admin" / "WhitelabelGrant.tsx"


def test_admin_can_upload_operator_brand_logo_without_raw_url_only_flow():
    api_text = API_FILE.read_text(encoding="utf-8")
    page_text = PAGE_FILE.read_text(encoding="utf-8")

    assert '@router.post("/admin/whitelabel/{target_user_id}/logo")' in api_text
    assert "仅管理员可上传对外品牌 Logo" in api_text
    assert "_detect_whitelabel_logo_ext" in api_text
    assert "uploads/whitelabel-logos" in api_text

    assert "上传 Logo" in page_text
    assert "type=\"file\"" in page_text
    assert "/api/referral/admin/whitelabel/${row.user_id}/logo" in page_text
    assert "Logo 地址" not in page_text


def test_admin_whitelabel_page_uses_operator_facing_brand_language():
    page_text = PAGE_FILE.read_text(encoding="utf-8")

    assert "对外品牌授权" in page_text
    assert "服务商" in page_text
    assert "白标授权" not in page_text
    assert "代理" not in page_text
