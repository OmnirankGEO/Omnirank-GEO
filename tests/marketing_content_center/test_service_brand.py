"""品牌轴心(Owner 2026-07-22:填了就是用户的品牌/未填留白/零平台兜底/匿名优先)。

品牌三态:已配置(公司名+logo)/未配置(留白)/匿名晒单优先;
logo 垫图(https 透传 + 本地 data URI + 读取失败静默降级为纯文字品牌名)。
"""
import asyncio
import io
import json

import pytest
from PIL import Image
from starlette.requests import Request

import db.connection
from api import marketing_material_api
from services.marketing import content_center, geo_factory, material_storage


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row=None):
        self._cursor = _FakeCursor(row)

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _stub_brand_row(monkeypatch, row):
    monkeypatch.setattr(db.connection, "get_connection", lambda: _FakeConn(row))


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def test_freeze_service_brand_configured_company_and_logo(monkeypatch):
    """已配置:company_name 非空即生效——不看 OEM 门禁/surface 矩阵,无需授权。"""
    _stub_brand_row(monkeypatch, {
        "company_name": "远山营销",
        "product_name": None,
        "logo_url": "/uploads/whitelabel-logos/opaque/logo.png",
        "slogan": "把活干好",
        "brand_color": "#123456",
        # OEM 门禁字段刻意保持未授权:营销专用解析不得读它们
        "whitelabel_mode": "none",
        "whitelabel_status": "locked",
        "unlocked_by_admin": False,
    })
    brand = marketing_material_api._freeze_service_brand(9)
    assert brand["name"] == "远山营销"
    assert brand["logo_url"] == "/uploads/whitelabel-logos/opaque/logo.png"
    assert brand["brand_color"] == "#123456"
    assert brand["source"] == "whitelabel_settings"
    assert "OmniRank" not in json.dumps(brand, ensure_ascii=False)
    assert "全域上榜" not in json.dumps(brand, ensure_ascii=False)


def test_freeze_service_brand_unconfigured_is_blank_never_platform(monkeypatch):
    """未配置:零品牌留白,绝不回落 OmniRank/全域上榜;读取失败同样留白不阻断。"""
    _stub_brand_row(monkeypatch, None)  # 无 whitelabel 设置行
    brand = marketing_material_api._freeze_service_brand(9)
    assert brand["name"] == "" and brand["logo_url"] == ""
    assert "OmniRank" not in json.dumps(brand, ensure_ascii=False)
    assert "全域上榜" not in json.dumps(brand, ensure_ascii=False)

    _stub_brand_row(monkeypatch, {  # 公司名空白 = 未填
        "company_name": "   ", "logo_url": "/uploads/whitelabel-logos/opaque/logo.png",
    })
    blank = marketing_material_api._freeze_service_brand(9)
    assert blank["name"] == "" and blank["logo_url"] == ""

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db.connection, "get_connection", _boom)
    failed = marketing_material_api._freeze_service_brand(9)  # 静默降级,不抛
    assert failed["name"] == "" and failed["logo_url"] == ""


def test_freeze_service_brand_drops_account_derived_logo_path(monkeypatch):
    """legacy 账号路径 logo(泄漏账号身份)不进营销成品;品牌名保留,静默纯文字。"""
    _stub_brand_row(monkeypatch, {
        "company_name": "远山营销",
        "logo_url": "/uploads/whitelabel-logos/173/logo.png",  # 账号衍生旧路径
    })
    brand = marketing_material_api._freeze_service_brand(9)
    assert brand["name"] == "远山营销"
    assert brand["logo_url"] == ""


def _bootstrap_request() -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/api/marketing/content-center/bootstrap", "headers": []})
    request.state.user = {"id": 9}
    return request


def test_bootstrap_reports_actual_brand_status(monkeypatch):
    from services.marketing import strategy_teachers

    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": 9})
    monkeypatch.setattr(strategy_teachers.marketing_db, "get_policy", lambda _key: None)
    monkeypatch.setattr(
        marketing_material_api, "_freeze_service_brand",
        lambda _p: {"name": "远山营销", "logo_url": "/uploads/whitelabel-logos/opaque/logo.png"},
    )
    response = asyncio.run(marketing_material_api.api_content_center_bootstrap(_bootstrap_request()))
    assert response["service_brand"] == {
        "configured": True, "name": "远山营销", "has_logo": True,
    }

    monkeypatch.setattr(
        marketing_material_api, "_freeze_service_brand",
        lambda _p: {"name": "", "logo_url": ""},
    )
    empty = asyncio.run(marketing_material_api.api_content_center_bootstrap(_bootstrap_request()))
    assert empty["service_brand"] == {"configured": False, "name": "", "has_logo": False}


def test_logo_data_uri_roundtrip_and_failures_degrade(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logo_dir = tmp_path / "uploads" / "whitelabel-logos" / "opaque"
    logo_dir.mkdir(parents=True)
    (logo_dir / "logo.png").write_bytes(_png_bytes())

    uri = material_storage.whitelabel_logo_data_uri("/uploads/whitelabel-logos/opaque/logo.png")
    assert uri.startswith("data:image/png;base64,")

    # 文件缺失 → ValueError;垫图解析静默降级为空(纯文字品牌名,不阻断)
    with pytest.raises(ValueError):
        material_storage.whitelabel_logo_data_uri("/uploads/whitelabel-logos/opaque/missing.png")
    assert geo_factory._brand_logo_pad(
        "/uploads/whitelabel-logos/opaque/missing.png", anonymize=False,
    ) == ""
    # 路径穿越/非法前缀一律拒绝
    for bad in (
        "/uploads/whitelabel-logos/../secret.png",
        "/uploads/marketing-materials/u9/logo.png",
        "file:///etc/passwd",
        "",
    ):
        with pytest.raises(ValueError):
            material_storage.whitelabel_logo_data_uri(bad)
    # 公网 https 透传;匿名晒单一律不垫
    assert geo_factory._brand_logo_pad(
        "https://cdn.example.com/logo.png", anonymize=False,
    ) == "https://cdn.example.com/logo.png"
    assert geo_factory._brand_logo_pad(
        "https://cdn.example.com/logo.png", anonymize=True,
    ) == ""


def _visual_document(brand: dict, **overrides) -> dict:
    prompt = content_center.visual_prompt(
        slot={"slot": "professional_poster", "size": "3:4"}, channel="professional_poster",
        strategy={"audience": "服务商", "single_action": "领取诊断", "single_value": "证据"},
        content={"title": "先看诊断", "body": "只讲可核验事实"},
        evidence={"facts": []}, trend={"used": False},
        contact={"mode": "none", "text": "", "qr_reference": None},
        brand=brand, **overrides,
    )
    return json.loads(prompt.split("\n", 1)[1])


def test_brand_logo_instruction_only_when_actually_attached():
    brand = {"name": "远山营销", "logo_url": "https://cdn.example.com/logo.png"}
    attached = _visual_document(brand, logo_attached=True)
    logo = attached["subject"]["brand_logo"]
    assert logo["attached"] is True
    assert "忠实保留" in logo["instruction"]
    assert "不重绘" in logo["instruction"] and "不变形" in logo["instruction"]
    # logo 读取失败(未垫图)→ 不要求保留,纯文字品牌名路径
    detached = _visual_document(brand, logo_attached=False)
    assert detached["subject"]["brand_logo"] is None
    assert detached["brand_assets"]["name"] == "远山营销"


def test_anonymize_brand_wins_over_brand_config():
    """匿名晒单优先:品牌名/logo 均不进 provider 输入,logo 保留指令也不出现。"""
    brand = {"name": "远山营销", "logo_url": "https://cdn.example.com/logo.png"}
    document = _visual_document(brand, logo_attached=True, anonymize_brand=True)
    assert document["brand_assets"]["name"] == ""
    assert document["brand_assets"]["logo_url"] == ""
    assert document["subject"]["brand_logo"] is None
    constraints = " ".join(document["privacy_and_truth_constraints"])
    assert "品牌 logo" in constraints  # 匿名约束:品牌名与 logo 一律不上图


def test_blank_brand_stays_blank_in_visual_and_copy_path():
    """未配置留白:brand_assets 全空(视觉 prompt 与 QA 的空品牌跳过路径接管)。"""
    document = _visual_document({"name": "", "logo_url": ""})
    assert document["brand_assets"]["name"] == ""
    assert document["brand_assets"]["logo_url"] == ""
    assert document["subject"]["brand_logo"] is None
    safe = content_center.provider_safe_brand({})
    assert safe["name"] == "" and safe["logo_url"] == ""
