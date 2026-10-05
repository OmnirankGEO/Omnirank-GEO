"""WO_WHITELABEL_COPY_UX_2026-08-05 · 项 1 白标平台默认值渗漏回归锁。

事故(生产实证 2026-08-05):服务商 #133 oem/active/unlocked、company_name 有值、
logo 为 opaque 安全路径 → display_scope=approved_whitelabel(门控**没有**挡他),
但 `_brand_from_row` 用 `{**_PLATFORM_BRAND, **overrides}` 合并,product_name=NULL
继承平台默认 "OmniRank" → 公开报告 API 下发 `product_name: "OmniRank"` →
前端 TopNav `productName ?? companyName` 回落永不触发 → 客户看到
「服务商 logo + OmniRank」。

锁的判据:
  · 必须命中:approved 品牌里服务商没填的字段必须是 None,任何值都不得含平台标识。
  · 必须不命中(反向对照):platform 分支(无白标/未授权)仍返回完整平台品牌
    (product_name == "OmniRank")——证明修复没把平台自身标识一并抹掉。
变异自检(tests/mutation_runner_whitelabel_copy_ux.py):
  把 `_brand_from_row` 的中性合并基还原为 `_PLATFORM_BRAND` → 本文件必须转红。

只用合成行,不读生产库。
"""
from __future__ import annotations

import pytest

# 与生产 #133 同构的合成行(oem + active + unlocked + 安全 opaque logo 路径 + product_name 缺省)
ROW_133_SHAPE = {
    "company_name": "合成服务商教育科技有限公司",
    "product_name": None,
    "logo_url": "/uploads/whitelabel-logos/SyNtHeTiCoPaQuEdIr/logo_test.png",
    "favicon_url": None,
    "slogan": None,
    "brand_color": "#669d34",
    "contact_name": "测试联系人",
    "contact_phone": "16600004444",
    "contact_wechat": None,
    "contact_email": None,
    "whitelabel_mode": "oem",
    "whitelabel_status": "active",
    "unlocked_by_admin": True,
}

# 平台标识字符串:approved 品牌任何字段里出现任意一个都算渗漏
PLATFORM_MARKS = ("OmniRank", "全域上榜", "/logo-192.png")


def _assert_no_platform_leak(brand: dict) -> None:
    for key, value in brand.items():
        if value is None:
            continue
        text = str(value)
        for mark in PLATFORM_MARKS:
            assert mark not in text, f"approved 品牌字段 {key}={text!r} 渗漏了平台标识 {mark!r}"


def test_approved_customer_brand_missing_product_name_stays_none():
    """#133 同构:approved 且 product_name 缺省 → 下发 None,前端才有机会回落 company_name。"""
    from services.public_whitelabel import public_branding_from_record

    out = public_branding_from_record(dict(ROW_133_SHAPE), surface="customer")
    assert out["display_scope"] == "approved_whitelabel"
    assert out["brand"]["company_name"] == ROW_133_SHAPE["company_name"]
    assert out["brand"]["product_name"] is None
    _assert_no_platform_leak(out["brand"])


def test_approved_customer_brand_never_inherits_any_platform_value():
    """所有可缺省字段逐个置 None,approved 品牌不得从平台默认继承任何值。"""
    from services.public_whitelabel import public_branding_from_record

    row = dict(ROW_133_SHAPE)
    for field in ("product_name", "favicon_url", "slogan", "brand_color",
                  "contact_name", "contact_phone", "contact_wechat", "contact_email"):
        row[field] = None
    out = public_branding_from_record(row, surface="customer")
    assert out["display_scope"] == "approved_whitelabel"
    _assert_no_platform_leak(out["brand"])
    assert out["brand"]["product_name"] is None
    assert out["brand"]["favicon_url"] is None
    assert out["brand"]["slogan"] is None


def test_approved_brand_own_product_name_passes_through():
    """服务商自己填了 product_name → 原样下发(修复不许影响已填值)。"""
    from services.public_whitelabel import public_branding_from_record

    row = {**ROW_133_SHAPE, "product_name": "晨曦慧远 AI 实训"}
    out = public_branding_from_record(row, surface="customer")
    assert out["display_scope"] == "approved_whitelabel"
    assert out["brand"]["product_name"] == "晨曦慧远 AI 实训"


def test_platform_branch_keeps_platform_identity():
    """反向对照(必须不命中):无白标 → 平台品牌完整保留,product_name 仍是 OmniRank。

    证明修复只切断 approved 分支的继承,没把平台自身标识抹掉
    (agent/admin 未授权面显示 OmniRank 是合法平台标识,不是泄漏)。
    """
    from services.public_whitelabel import public_branding_from_record

    out = public_branding_from_record(None, surface="customer")
    assert out["display_scope"] == "platform"
    assert out["brand"]["product_name"] == "OmniRank"
    assert out["brand"]["company_name"] == "OmniRank · 全域上榜"
    assert out["brand"]["logo_url"] == "/logo-192.png"


def test_snapshot_branch_missing_product_name_stays_none():
    """resolve_branding_context 快照分支(冻结报价品牌)走同一序列化 → 同样不得渗漏。

    不传 owner/quote/brand 线索 → 快照分支不触库(_load_live... 直接 None),纯内存可测。
    """
    from services.public_whitelabel import resolve_branding_context

    snapshot = {k: v for k, v in ROW_133_SHAPE.items()}
    ctx = resolve_branding_context(surface="customer", snapshot=snapshot)
    assert ctx["display_scope"] == "approved_whitelabel"
    assert ctx["brand"].get("product_name") is None
    _assert_no_platform_leak(ctx["brand"])


def test_agent_surface_brand_missing_product_name_is_none_safe(monkeypatch):
    """agent surface approved 品牌 product_name=None 时,助手名解析不得抛异常。

    钉住 content_api 那处 `.get("product_name", "").strip()`(社媒主路径 router 那处已随 E3 删)
    的修复:键存在值为 None → 旧写法 None.strip() 直接 AttributeError。
    """
    from services import public_whitelabel as pwl

    monkeypatch.setattr(pwl, "is_backoffice_brand_enabled", lambda: True)
    row = {**ROW_133_SHAPE, "backoffice_brand_unlocked": True}
    out = pwl.public_branding_from_record(row, surface="agent")
    assert out["display_scope"] == "approved_whitelabel"
    # 复现调用方的取值路径(与 content_api._resolve_agent_assistant 修复后同型)
    brand = out["brand"]
    assistant = ((brand or {}).get("product_name") or "").strip() or "默认助手"
    assert assistant == "默认助手"


def test_fix_source_no_platform_merge_base():
    """源锁:_brand_from_row 不得再以 _PLATFORM_BRAND 为合并基(剥注释后扫描)。"""
    import inspect
    import re

    from services import public_whitelabel as pwl

    src = inspect.getsource(pwl._brand_from_row)
    # 剥 docstring 与 # 注释,只扫代码(判据别被自己的注释骗 · 2026-08-04 教训)
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "_PLATFORM_BRAND" not in src, "_brand_from_row 代码里不得引用 _PLATFORM_BRAND"


def test_assistant_resolvers_use_none_safe_pattern():
    """源锁:两处助手名解析必须用 None 安全写法(or "" 在 .strip() 之前)。"""
    from pathlib import Path

    for rel in ("api/content_api.py",):  # 社媒主路径 router 随 E3 删
        text = Path(rel).read_text(encoding="utf-8")
        assert '.get("product_name", "").strip()' not in text, f"{rel} 仍有 None 不安全取值"
        assert '.get("product_name") or "").strip()' in text, f"{rel} 未见 None 安全写法"
