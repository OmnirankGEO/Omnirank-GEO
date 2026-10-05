"""tests/test_whitelabel_v36.py — v3.6 双档白标 surface 矩阵 + 授权隔离回归

覆盖 Codex P1 修复 + 老板要求的 5 例:
  1. legacy snapshot external_only:customer 显示 / agent 不显示
  2. oem snapshot:agent 显示
  3. admin active external_only/oem:unlocked 强制 True（消除 active+unlocked=false 矛盾态）
  4. 非 admin 调 admin 授权端点 → 403
  5. 代理 PUT /whitelabel 带授权字段 → 403

纯逻辑测试不碰 DB（resolver snapshot/admin 分支 + _normalize_whitelabel_grant +
_forbidden_whitelabel_authz_fields 均不调 get_connection）；端点 403 测试用 TestClient,
在任何 DB 调用前 raise。模块内 import 延迟到函数体,沿用 test_public_whitelabel.py 模式。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ============ T2 · resolve_branding_context surface 矩阵（snapshot 纯逻辑）============

def _resolve():
    from services.public_whitelabel import resolve_branding_context
    return resolve_branding_context


def _approved_snapshot(**overrides):
    return {
        "company_name": "代理品牌",
        "logo_url": "https://cdn.example/brand.png",
        "whitelabel_mode": "external_only",
        "whitelabel_status": "active",
        "unlocked_by_admin": True,
        "admin_approved": True,
        **overrides,
    }


def test_snapshot_external_only_customer_shows_brand():
    snap = _approved_snapshot()
    r = _resolve()(surface="customer", snapshot=snap)
    assert r["brand"]["company_name"] == "代理品牌"
    assert r["source"] == "agent_quote_snapshot"


def test_snapshot_external_only_agent_returns_platform():
    """修复点:external_only 快照在 agent surface 不得显示代理品牌（曾绕过 surface 矩阵）。"""
    snap = _approved_snapshot()
    r = _resolve()(surface="agent", snapshot=snap)
    assert r["brand"]["company_name"] == "OmniRank · 全域上榜"
    assert r["source"] == "platform_default"


def test_legacy_snapshot_without_approval_never_grandfathers():
    """旧快照无批准证据 → customer / agent 都回退平台。"""
    snap = {"company_name": "老快照代理"}
    assert _resolve()(surface="customer", snapshot=snap)["brand"]["company_name"] == "OmniRank · 全域上榜"
    assert _resolve()(surface="agent", snapshot=snap)["brand"]["company_name"] == "OmniRank · 全域上榜"


def test_snapshot_oem_agent_shows_brand():
    """D2（Owner 2026-07-22）：oem 快照 + agent surface 需 backoffice 授权证据。

    - 旧快照无 backoffice 授权位（无法证明档位）→ fail-closed 平台；
    - 快照自带 backoffice_brand_unlocked=True 且总闸开 → 显示（新冻结快照能力）。
    """
    snap = _approved_snapshot(company_name="OEM代理", whitelabel_mode="oem")
    r = _resolve()(surface="agent", snapshot=snap)
    assert r["brand"]["company_name"] == "OmniRank · 全域上榜"
    assert r["source"] == "platform_default"

    monkeypatch_snap = _approved_snapshot(
        company_name="OEM代理", whitelabel_mode="oem", backoffice_brand_unlocked=True,
    )
    from services import public_whitelabel as pwl
    old_flag = pwl.is_backoffice_brand_enabled
    pwl.is_backoffice_brand_enabled = lambda: True
    try:
        r2 = _resolve()(surface="agent", snapshot=monkeypatch_snap)
        assert r2["brand"]["company_name"] == "OEM代理"
        assert r2["source"] == "agent_quote_snapshot"
    finally:
        pwl.is_backoffice_brand_enabled = old_flag


def test_admin_surface_always_platform_even_with_oem_snapshot():
    snap = _approved_snapshot(whitelabel_mode="oem")
    r = _resolve()(surface="admin", snapshot=snap)
    assert r["brand"]["company_name"] == "OmniRank · 全域上榜"
    assert r["source"] == "platform_default"


def test_customer_snapshot_masks_contacts_agent_not():
    """customer 快照联系方式脱敏；agent surface 在 backoffice 授权下不脱敏（D2 后语义）。"""
    from services import public_whitelabel as pwl
    snap = _approved_snapshot(
        company_name="x", whitelabel_mode="oem",
        contact_phone="13812345678", backoffice_brand_unlocked=True,
    )
    assert _resolve()(surface="customer", snapshot=snap)["brand"]["contact_phone"] == "138****5678"
    old_flag = pwl.is_backoffice_brand_enabled
    pwl.is_backoffice_brand_enabled = lambda: True
    try:
        assert _resolve()(surface="agent", snapshot=snap)["brand"]["contact_phone"] == "13812345678"
    finally:
        pwl.is_backoffice_brand_enabled = old_flag


def test_can_show_brand_matrix():
    """D2（Owner 2026-07-22）：agent surface 改读 backoffice 独立授权位，mode='oem' 不再隐含。"""
    from services.public_whitelabel import _can_show_brand
    assert _can_show_brand("customer", "external_only") is True
    assert _can_show_brand("customer", "oem") is True
    assert _can_show_brand("customer", "none") is False
    # agent：只看 backoffice_allowed（独立授权位）· 与 mode 解耦 · 缺省 fail-closed
    assert _can_show_brand("agent", "external_only") is False
    assert _can_show_brand("agent", "oem") is False                          # D2:oem 不再自动隐含后台换肤
    assert _can_show_brand("agent", "oem", backoffice_allowed=True) is True  # 独立授权才显示
    assert _can_show_brand("agent", "external_only", backoffice_allowed=True) is True
    assert _can_show_brand("admin", "oem") is False
    assert _can_show_brand("admin", "oem", backoffice_allowed=True) is False


# ============ T3 · admin grant 规范化（纯逻辑）============

def test_grant_active_external_only_forces_unlocked_true():
    """修复点:active + external_only/oem 强制 unlocked=True（消除矛盾态）。"""
    from api.referral_api import _normalize_whitelabel_grant
    assert _normalize_whitelabel_grant("external_only", "active") == ("external_only", "active", True)
    assert _normalize_whitelabel_grant("oem", "active") == ("oem", "active", True)
    # locked 态也派生 True（已授权但未激活）
    assert _normalize_whitelabel_grant("external_only", "locked") == ("external_only", "locked", True)


def test_grant_none_revokes_unlock():
    from api.referral_api import _normalize_whitelabel_grant
    assert _normalize_whitelabel_grant("none", "active") == ("none", "locked", False)


def test_grant_invalid_raises():
    from api.referral_api import _normalize_whitelabel_grant
    with pytest.raises(ValueError):
        _normalize_whitelabel_grant("foo", "active")
    with pytest.raises(ValueError):
        _normalize_whitelabel_grant("oem", "bogus")


def test_forbidden_authz_fields_detection():
    from api.referral_api import _forbidden_whitelabel_authz_fields
    assert _forbidden_whitelabel_authz_fields({"company_name", "whitelabel_mode"}) == {"whitelabel_mode"}
    assert _forbidden_whitelabel_authz_fields({"hide_platform_branding"}) == {"hide_platform_branding"}
    # 品牌字段（含 T3 新增 product_name/favicon_url）全放行
    assert _forbidden_whitelabel_authz_fields(
        {"company_name", "product_name", "favicon_url", "logo_url", "slogan", "brand_color", "contact_phone"}
    ) == set()


def test_whitelabel_logo_magic_detector_accepts_only_safe_raster_formats():
    from api.referral_api import _detect_whitelabel_logo_ext

    assert _detect_whitelabel_logo_ext(b"\x89PNG\r\n\x1a\nabc") == ".png"
    assert _detect_whitelabel_logo_ext(b"\xff\xd8\xff\xe0abc") == ".jpg"
    assert _detect_whitelabel_logo_ext(b"RIFFxxxxWEBPabc") == ".webp"
    assert _detect_whitelabel_logo_ext(b"<svg><script>alert(1)</script></svg>") is None
    assert _detect_whitelabel_logo_ext(b"not an image") is None


# ============ T3 · 端点 403（TestClient · DB 调用前 raise）============

def _client(user):
    from api.referral_api import router
    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request, call_next):
        request.state.user = user
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def test_agent_put_whitelabel_authz_field_403():
    """代理 PUT /whitelabel 带授权字段(whitelabel_mode)→ hard 403（DB 调用前）。"""
    c = _client({"user_id": 78, "is_admin": False})
    r = c.put("/api/referral/whitelabel", json={"whitelabel_mode": "oem", "company_name": "x"})
    assert r.status_code == 403


def test_non_admin_grant_endpoint_403():
    """非 admin 调 admin 授权端点 → 403（DB 调用前）。"""
    c = _client({"user_id": 78, "is_admin": False})
    r = c.put(
        "/api/referral/admin/whitelabel/999",
        json={"whitelabel_mode": "external_only", "whitelabel_status": "active"},
    )
    assert r.status_code == 403


def test_upload_whitelabel_logo_returns_public_upload_url(monkeypatch, tmp_path):
    """上传 Logo 只落本地上传目录并返回 URL,不直接写白标配置 DB。"""
    monkeypatch.chdir(tmp_path)
    c = _client({"user_id": 78, "is_admin": False})
    png = b"\x89PNG\r\n\x1a\nminimal"

    r = c.post(
        "/api/referral/whitelabel/logo",
        files={"file": ("logo.png", png, "image/png")},
    )

    assert r.status_code == 200
    body = r.json()
    logo_url = body["data"]["logo_url"]
    assert logo_url.startswith("/uploads/whitelabel-logos/")
    assert "/78/" not in logo_url
    assert logo_url.endswith(".png")
    assert body["logo_url"] == logo_url
    assert (tmp_path / logo_url.lstrip("/")).exists()


def test_upload_whitelabel_logo_rejects_svg(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    c = _client({"user_id": 78, "is_admin": False})

    r = c.post(
        "/api/referral/whitelabel/logo",
        files={"file": ("logo.svg", b"<svg><script>alert(1)</script></svg>", "image/svg+xml")},
    )

    assert r.status_code == 400
    assert "PNG" in r.json()["detail"]


def test_whitelabel_settings_logo_upload_ui_contract():
    """白标设置页必须保留 URL 输入,并提供本地 Logo 上传入口。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "frontend" / "src" / "pages" / "Agent" / "WhitelabelSettings.tsx").read_text(encoding="utf-8")
    assert "/api/referral/whitelabel/logo" in source
    assert "上传 Logo" in source
    assert 'accept="image/png,image/jpeg,image/webp"' in source
    assert "点击「保存设置」后生效" in source


# ============ T4 · 白标配置访问 gate（agent_level → whitelabel_mode）============

def test_get_whitelabel_open_returns_data(monkeypatch):
    """P1（2026-06-06）:基础白标对所有 operator 放开 · GET /whitelabel 不再 mode=none 拦截,
    返回数据(新用户空 dict · 非旧的授权提示)。"""
    import api.referral_api as ra

    class _Cur:
        def execute(self, q, p=None): pass
        def fetchone(self): return None  # 新用户无白标记录
        def close(self): pass

    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass

    monkeypatch.setattr(ra, "get_connection", lambda: _Conn())
    c = _client({"user_id": 78, "is_admin": False})
    r = c.get("/api/referral/whitelabel")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"] == {"configuration_status": "draft", "display_scope": "platform"}


def test_put_whitelabel_requires_brand_terms_first(monkeypatch):
    """P1（2026-06-06）:基础白标放开 · 首次保存(未同意《对外品牌使用条款》)→ 403 BRAND_TERMS_REQUIRED
    (替代旧 mode=none 授权门;纯品牌字段不触发 T3 hard-403)。"""
    import api.referral_api as ra

    class _Cur:
        def execute(self, q, p=None): pass
        def fetchone(self):
            # 未同意条款 + 无白标记录(brand_terms_accepted_at=None)
            return {"whitelabel_mode": "none", "whitelabel_status": "none",
                    "company_name": None, "brand_terms_accepted_at": None}
        def close(self): pass

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass

    monkeypatch.setattr(ra, "get_db", lambda: _Conn())
    c = _client({"user_id": 78, "is_admin": False})
    r = c.put("/api/referral/whitelabel", json={"company_name": "x"})  # 无 accept_brand_terms
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "BRAND_TERMS_REQUIRED"


def test_put_complete_brand_activates_customer_surface_without_oem(monkeypatch):
    import api.referral_api as ra

    # D1（Owner 2026-07-22）：外链 Logo 需 HEAD 探测 · 测试注入假探测跳过网络
    monkeypatch.setattr(ra, "_probe_brand_image_url", lambda url, label: None)

    executed: list[tuple[str, object]] = []
    rows = [
        {
            "whitelabel_mode": "none",
            "whitelabel_status": "locked",
            "company_name": None,
            "brand_terms_accepted_at": None,
        },
        {
            "user_id": 78,
            "company_name": "服务商品牌",
            "logo_url": "https://cdn.example/brand.png",
            "company_logo_url": "https://cdn.example/brand.png",
            "whitelabel_mode": "none",
            "whitelabel_status": "locked",
            "unlocked_by_admin": False,
            "brand_terms_accepted_at": None,
        },
    ]

    class _Cur:
        def execute(self, query, params=None):
            executed.append((" ".join(query.split()), params))

        def fetchone(self):
            return rows.pop(0)

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(ra, "get_db", lambda: _Conn())
    response = _client({"user_id": 78, "is_admin": False}).put(
        "/api/referral/whitelabel",
        json={
            "company_name": "服务商品牌",
            "logo_url": "https://cdn.example/brand.png",
            "accept_brand_terms": True,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["configuration_status"] == "approved"
    # D2（Owner 2026-07-22）：display_scope 只管后台换肤（backoffice 独立授权）；
    # external_only 客户侧生效但后台 fail-closed 平台 → display_scope='platform'
    assert data["display_scope"] == "platform"
    assert data["customer_branding_active"] is True
    assert data["backoffice_branding_active"] is False
    assert data["backoffice_brand_allowed"] is False
    transition = next(query for query, _params in executed if "SET whitelabel_mode = 'external_only'" in query)
    assert "unlocked_by_admin = FALSE" in transition
    # D1：审计 + 版本化（同事务）
    audit_inserts = [query for query, _params in executed if "INSERT INTO whitelabel_audit" in query]
    assert audit_inserts, "品牌字段变更必须写 whitelabel_audit"
    assert any("brand_version = COALESCE(brand_version, 1) + 1" in query for query, _params in executed)


# ============ T4b · 显示 gate（is_whitelabel_active_for_customer + get_public_whitelabel_data）============

def test_is_whitelabel_active_for_customer_predicate():
    """customer surface 显示判定（覆盖老板 5 场景的客户面 + 边界）。"""
    from services.public_whitelabel import is_whitelabel_active_for_customer as f
    base = {
        "company_name": "X", "logo_url": "https://cdn.example/x.png",
        "whitelabel_status": "active", "unlocked_by_admin": True,
    }
    assert f({**base, "whitelabel_mode": "external_only"}) is True   # legacy ext → customer 显示
    assert f({**base, "whitelabel_mode": "oem"}) is True             # oem → customer 也显示
    assert f({**base, "whitelabel_mode": "none"}) is False           # 未授权 → 平台
    assert f({**base, "whitelabel_mode": "external_only", "whitelabel_status": "suspended"}) is False  # 暂停一律不生效
    assert f({**base, "whitelabel_mode": "external_only", "unlocked_by_admin": False}) is True  # 客户侧自助生效
    assert f({**base, "whitelabel_mode": "oem", "unlocked_by_admin": False}) is False                  # 对照:oem 仍需 admin 解锁
    assert f({"company_name": "", "whitelabel_mode": "external_only",
              "whitelabel_status": "active", "unlocked_by_admin": True}) is False  # 无 company_name
    assert f(None) is False


def test_agent_serializer_separates_selfserve_customer_brand_from_oem_governance():
    """D2（Owner 2026-07-22）：display_scope 只反映后台换肤（backoffice 独立授权）；

    客户侧品牌生效看 customer_branding_active / configuration_status。
    """
    from api.referral_api import _serialize_agent_whitelabel_config

    external = _serialize_agent_whitelabel_config({
        "company_name": "服务商品牌",
        "logo_url": "https://cdn.example/brand.png",
        "whitelabel_mode": "external_only",
        "whitelabel_status": "active",
        "unlocked_by_admin": False,
    })
    assert external["configuration_status"] == "approved"          # 客户侧自助生效（D1 免审批 · 不变）
    assert external["customer_branding_active"] is True
    # D2：external_only 无 backoffice 授权 → 后台 fail-closed 平台
    assert external["display_scope"] == "platform"
    assert external["backoffice_branding_active"] is False
    assert external["backoffice_brand_allowed"] is False
    assert external["backoffice_brand_unlocked"] is False
    assert external["brand_version"] == 1

    locked_oem = _serialize_agent_whitelabel_config({
        "company_name": "服务商品牌",
        "logo_url": "https://cdn.example/brand.png",
        "whitelabel_mode": "oem",
        "whitelabel_status": "active",
        "unlocked_by_admin": False,
    })
    assert locked_oem["configuration_status"] == "draft"
    assert locked_oem["display_scope"] == "platform"
    assert locked_oem["backoffice_branding_active"] is False


def _pwl_client_rows(monkeypatch, wl_row):
    """monkeypatch quote owner lookup + approved brand lookup。"""
    from services import public_whitelabel
    rows = [{"owner_user_id": 17}, wl_row]

    class _Cur:
        def execute(self, q, p=None):
            pass

        def fetchone(self):
            return rows.pop(0)

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr(public_whitelabel, "get_connection", lambda: _Conn())
    return public_whitelabel


def test_get_public_whitelabel_data_active_returns_brand_and_mode(monkeypatch):
    """legacy 回填后（ext/active/unlocked）→ customer 公开端点返代理品牌 + mode（联系方式脱敏 · 报告路径）。"""
    pwl = _pwl_client_rows(monkeypatch, {
        "company_name": "Acme", "logo_url": "https://cdn.example/acme.png",
        "contact_phone": "13812345678",
        "whitelabel_mode": "external_only", "whitelabel_status": "active", "unlocked_by_admin": True,
    })
    out = pwl.get_public_whitelabel_data(quote_id=17)
    assert out["display_scope"] == "approved_whitelabel"
    assert "whitelabel_mode" not in out
    assert out["whitelabel"]["company_name"] == "Acme"
    assert out["whitelabel"]["contact_phone"] == "138****5678"


def test_get_public_whitelabel_data_unauthorized_no_brand(monkeypatch):
    """未授权（mode=none）→ whitelabel None + mode none（客户看平台 OmniRank）。"""
    pwl = _pwl_client_rows(monkeypatch, {
        "company_name": "Acme", "whitelabel_mode": "none",
        "whitelabel_status": "locked", "unlocked_by_admin": False,
    })
    out = pwl.get_public_whitelabel_data(quote_id=17)
    assert out["whitelabel"] is None and out["display_scope"] == "platform"
    assert "whitelabel_mode" not in out


# ============ T4b 修 · 客户报告 3 分支闭环（Codex P1：v3/legacy 曾露 OmniRank）============

def test_select_customer_report_branch_authorized_forces_whitelabelable():
    """授权白标强制可白标网页；其余 V2 默认网页，legacy 仅显式回滚。

    覆盖 Codex #3：授权 external_only/oem 的客户报告不会落到会露 OmniRank 的 v3/legacy 分支。
    """
    from api.share_api import _select_customer_report_branch as sel
    # 授权白标：无论 V3 / rollback 都强制可白标分支
    assert sel(True, True, False) == "customer_decision"    # 本会 v3 → 改 customer_decision
    assert sel(True, False, False) == "customer_decision"   # 本会 legacy → 改 customer_decision
    assert sel(True, True, True) == "customer_decision"
    assert sel(True, False, True) == "customer_decision"
    # 未授权：V3 gate 优先；普通 V2 默认 customer-decision；显式回滚才 legacy。
    assert sel(False, True, False) == "v3"
    assert sel(False, True, True) == "v3"            # should_v3 优先
    assert sel(False, False, True) == "legacy"
    assert sel(False, False, False) == "customer_decision"


def test_whitelabel_error_page_no_platform_and_escaped():
    """fail-closed 错误页：0 平台品牌 + company_name 转义防 XSS（修 Codex T4b P1）。"""
    from api.share_api import _render_whitelabel_error_page
    h = _render_whitelabel_error_page({"company_name": "<script>x</script>代理X公司"})
    assert "OmniRank" not in h and "全域上榜" not in h
    assert "<script>" not in h          # 已转义
    assert "代理X公司" in h


def test_authorized_whitelabel_fallback_never_legacy_no_omnirank():
    """授权白标 + customer_decision 抛异常 → 白标错误页（绝不调 legacy / 0 OmniRank · fail-closed）。"""
    from api.share_api import _render_authorized_whitelabel_report_or_error

    def _boom():
        raise RuntimeError("customer_decision render failed")

    h = _render_authorized_whitelabel_report_or_error({"company_name": "代理X公司"}, _boom)
    assert "OmniRank" not in h and "全域上榜" not in h
    assert "代理X公司" in h
