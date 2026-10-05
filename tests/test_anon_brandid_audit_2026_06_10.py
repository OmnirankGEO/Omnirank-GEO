# -*- coding: utf-8 -*-
"""#13(audit · 2026-06-10):匿名公开面移除内部 user_id / brand_id,白标改后端内联解析。

防攻击链:枚举 /api/public/report/{id} 拿 brand_owner_user_id → 调 /api/public/whitelabel/{uid}
串联出"哪个品牌属于哪个代理"+ 代理画像。改为服务端内联 customer-surface 白标(已 gate+脱敏),
前端直接读 report.whitelabel / data.whitelabel,绝不下发也不按 user_id 二次调白标。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARE = (ROOT / "api" / "share_api.py").read_text(encoding="utf-8")
SELECTION = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
SHARED_REPORT = (ROOT / "frontend" / "src" / "pages" / "Public" / "SharedReport.tsx").read_text(encoding="utf-8")
SELECTION_PAGE = (ROOT / "frontend" / "src" / "pages" / "Selection" / "SelectionPage.tsx").read_text(encoding="utf-8")


def _fn_block(src: str, start_marker: str, end_marker: str) -> str:
    i = src.find(start_marker)
    assert i >= 0, f"未找到 {start_marker}"
    j = src.find(end_marker, i + len(start_marker))
    return src[i:(j if j > 0 else len(src))]


# ---------- 后端 share_api.get_public_report ----------

def test_public_report_response_no_internal_ids():
    """对外 report dict 不再下发 brand_owner_user_id / brand_id(内部代理 user_id / 内部标识)。"""
    blk = _fn_block(SHARE, "async def get_public_report(", "\nasync def ")
    ret = blk[blk.find('"report": {'):]
    assert ret, "get_public_report 的 report dict 未定位"
    assert '"brand_owner_user_id"' not in ret, "对外响应不得含 brand_owner_user_id"
    assert '"brand_id"' not in ret, "对外响应不得含 brand_id"


def test_public_report_inlines_whitelabel():
    """改为服务端内联 customer-surface 白标(已 gate+脱敏),随 report 下发。"""
    blk = _fn_block(SHARE, "async def get_public_report(", "\nasync def ")
    assert "get_public_whitelabel_data(" in blk, "需服务端内联解析白标"
    ret = blk[blk.find('"report": {'):]
    assert '"whitelabel"' in ret and '"branding_status"' in ret, "内联白标须随 report 下发"
    assert '"whitelabel_mode"' not in ret, "公开报告不得下发内部模式"


def test_public_report_sql_still_resolves_owner():
    """SQL 仍取 owner_user_id 供服务端解析白标(只是不下发对外)。"""
    blk = _fn_block(SHARE, "async def get_public_report(", "\nasync def ")
    assert "owner_user_id AS brand_owner_user_id" in blk, "服务端解析白标仍需 row 的 owner_user_id"


# ---------- 后端 selection_api.get_selection_page ----------

def test_selection_base_no_owner_user_id():
    """/s/{token} base 不再下发 owner_user_id;内联 whitelabel 保留。"""
    blk = _fn_block(SELECTION, "async def get_selection_page(", "\n@router.")
    base = blk[blk.find("base = {"):blk.find("if session")]
    assert base, "get_selection_page 的 base dict 未定位"
    assert '"owner_user_id"' not in base, "选词页 base 不得下发 owner_user_id"
    assert '"whitelabel"' in base, "内联 whitelabel 须保留"


# ---------- 前端 ----------

def test_shared_report_reads_inline_whitelabel():
    """SharedReport 改读后端内联 whitelabel · 不再按 user_id 调白标 hook。"""
    assert "?.brand_owner_user_id" not in SHARED_REPORT, "不得再访问 report.brand_owner_user_id"
    assert "useWhitelabel(" not in SHARED_REPORT, "不再按 user_id 调 useWhitelabel"
    assert "report?.whitelabel" in SHARED_REPORT, "应改读后端内联 report.whitelabel"


def test_selection_page_reads_inline_whitelabel():
    """SelectionPage 改读后端内联 whitelabel · 不再按 owner_user_id 调白标 hook。"""
    assert "data?.owner_user_id" not in SELECTION_PAGE, "不得再访问 data.owner_user_id"
    assert "useBranding(" not in SELECTION_PAGE, "不再按 user_id 调 useBranding"
    assert "data?.whitelabel" in SELECTION_PAGE, "应改读后端内联 data.whitelabel"


# ============================================================
# #10 Fable 返修(2026-06-10)· 终端枚举端点收敛 + 2 个 wire 端点去 owner_user_id
# ============================================================
_FULL_WL = {
    "whitelabel": {
        "company_name": "友牌科技", "logo_url": "/l.png", "slogan": "上榜专家",
        "brand_color": "#111", "product_name": "友牌", "favicon_url": "/f.ico",
        "contact_name": "张经理", "contact_phone": "138****2688",
        "contact_wechat": "abc***xy", "contact_email": "a***@x.com",
    },
    "display_scope": "approved_whitelabel",
}


def test_10_whitelabel_without_business_object_returns_platform(monkeypatch):
    """无 quote 等业务对象时不得按裸 user_id 枚举白标。"""
    import asyncio
    import api.share_api as sa
    resp = asyncio.run(sa.get_public_whitelabel())
    assert resp.get("whitelabel") is None
    assert resp.get("display_scope") == "platform"
    assert "resolved_user_id" not in resp, "resolved_user_id 不下发(不泄漏代理 user_id)"


def test_10_whitelabel_full_with_quote(monkeypatch):
    """[#10 返修 行为] 带 quote_id 凭证(客户面)→ 完整脱敏白标(含脱敏联系方式),但仍不回 resolved_user_id。"""
    import asyncio
    import api.share_api as sa
    monkeypatch.setattr(sa, "get_public_whitelabel_data", lambda quote_id=None: _FULL_WL)
    resp = asyncio.run(sa.get_public_whitelabel(quote_id=268))
    wl = resp.get("whitelabel") or {}
    assert wl.get("contact_phone") == "138****2688", "客户面保留脱敏联系方式"
    assert "resolved_user_id" not in resp, "对外永不回 resolved_user_id"


def test_10_whitelabel_resolves_via_quote_not_path_uid(monkeypatch):
    """[#10 返修] 带 quote_id 时须用 quote_id 解析 owner(传 None user_id),不按路径 uid 直查。"""
    import asyncio
    import api.share_api as sa
    seen = {}

    def _spy(quote_id=None):
        seen["quote_id"] = quote_id
        return _FULL_WL

    monkeypatch.setattr(sa, "get_public_whitelabel_data", _spy)
    asyncio.run(sa.get_public_whitelabel(quote_id=268))
    assert seen["quote_id"] == 268, "带 quote_id 须走业务对象解析"


def test_10_marketing_m_no_owner_user_id():
    """[#10 返修] /m/{token} 响应去 owner_user_id(白标已内联)。"""
    src = (ROOT / "api" / "marketing_confirm_api.py").read_text(encoding="utf-8")
    i = src.find('@router.get("/m/{token}")')
    blk = src[i:i + 2500]
    assert '"owner_user_id"' not in blk, "/m 响应不得含 owner_user_id"
    assert '"whitelabel"' in blk, "内联白标须保留"


def test_10_portal_verify_no_owner_user_id():
    """[#10 返修] /api/portal/verify(api_verify_token)响应去 owner_user_id。"""
    src = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
    i = src.find("def api_verify_token(")
    blk = src[i:i + 1600]
    assert '"owner_user_id"' not in blk, "portal verify 响应不得含 owner_user_id"
    assert '"whitelabel"' in blk, "内联白标须保留"


def test_10_frontend_consumers_read_inline_not_owner_id():
    """[#10 返修] MaterialConfirm 读内联白标;Portal 不再存/读 portal_owner_user_id。"""
    mc = (ROOT / "frontend" / "src" / "pages" / "MaterialConfirm" / "MaterialConfirmPage.tsx").read_text(encoding="utf-8")
    assert "data?.owner_user_id" not in mc, "MaterialConfirm 不得再按 owner_user_id 调白标"
    assert "inlineWhitelabel" in mc, "MaterialConfirm 须读内联白标"
    pl = (ROOT / "frontend" / "src" / "pages" / "Portal" / "PortalLogin.tsx").read_text(encoding="utf-8")
    assert "localStorage.setItem('portal_owner_user_id'" not in pl, "PortalLogin 不得再存 owner_user_id"
    pd = (ROOT / "frontend" / "src" / "pages" / "Portal" / "PortalDashboard.tsx").read_text(encoding="utf-8")
    assert "localStorage.getItem('portal_owner_user_id')" not in pd, "PortalDashboard 不得再读 owner_user_id"
