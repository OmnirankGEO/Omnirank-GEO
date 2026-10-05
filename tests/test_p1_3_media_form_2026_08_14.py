# -*- coding: utf-8 -*-
"""P1-3 · 媒体形态分档 + 署名适配 · 判别锁(2026-08-14)。

变异点:
  M1 拆 policy 地板(floor 恒原样)→ test_editorial_forms_floor_policy 红;
  M2 往 byline 模板塞「据XX报道」→ test_no_editorial_attribution_any_form 红(分档判据);
  M3 拆发布接线(meijiehezi_api 不调 floor/byline)→ test_publish_wiring 红;
  M4 拆迁移登记 → test_migration_registered 红。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.media_form_adaptation import (
    _EDITORIAL_ATTRIBUTION,
    VALID_MEDIA_FORMS,
    floor_contact_policy,
    media_form_byline,
    normalize_media_form,
)

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 元判据
def test_editorial_attribution_regex_has_teeth() -> None:
    """判据自证:分档判据的正则必须真的响(恒不响的锁视同没有)。"""
    for bad in ("据搜狐网报道", "本报讯", "新华社讯:", "记者 王某"):
        assert _EDITORIAL_ATTRIBUTION.search(bad), bad
    assert not _EDITORIAL_ATTRIBUTION.search("—— 本文由 甲品牌 发布")


# ------------------------------------------------------------------ policy 地板
def test_editorial_forms_floor_policy() -> None:
    assert floor_contact_policy("full_contact", ["portal_site"]) == "none"
    assert floor_contact_policy("website_only", ["vertical_media", "platform_account"]) == "none"


def test_non_editorial_forms_keep_policy() -> None:
    # 反向对照:平台号/自站/未分档不加严(O1:目录缺失 = 既有降级)
    assert floor_contact_policy("website_only", ["platform_account"]) == "website_only"
    assert floor_contact_policy("full_contact", ["self_site"]) == "full_contact"
    assert floor_contact_policy("full_contact", []) == "full_contact"
    assert floor_contact_policy("full_contact", ["", "unknown_form"]) == "full_contact"


# ------------------------------------------------------------------ 署名段
def test_self_site_byline_carries_brand() -> None:
    byline = media_form_byline(["self_site"], "甲品牌")
    assert "甲品牌" in byline and byline


@pytest.mark.parametrize("forms", [
    ["platform_account"], ["portal_site"], ["vertical_media"],
    ["self_site", "platform_account"],  # 混合下单按最保守档 → 不署
    [], [""],
])
def test_non_self_forms_no_byline(forms) -> None:
    assert media_form_byline(forms, "甲品牌") == ""


@pytest.mark.parametrize("forms", [[f] for f in sorted(VALID_MEDIA_FORMS)] + [["self_site"]])
def test_no_editorial_attribution_any_form(forms) -> None:
    """🔴 分档判据(工单红线):任何档的署名段禁出编辑归属形态。"""
    byline = media_form_byline(forms, "搜狐网测试品牌")
    assert not _EDITORIAL_ATTRIBUTION.search(byline or "")


def test_empty_brand_no_byline() -> None:
    assert media_form_byline(["self_site"], "") == ""


def test_normalize_media_form() -> None:
    assert normalize_media_form("Portal_Site") == "portal_site"
    assert normalize_media_form("nonsense") == ""
    assert normalize_media_form(None) == ""


# ------------------------------------------------------------------ 接线锁
def test_publish_wiring() -> None:
    src = (ROOT / "api/meijiehezi_api.py").read_text(encoding="utf-8")
    assert "floor_contact_policy(" in src, "policy 地板没接进发布主链(死函数)"
    assert "media_form_byline(" in src, "署名段没接进发布主链(死函数)"
    assert "get_media_source_domains(" in src, "媒体域名取形态的桥没接"


def test_directory_read_returns_form() -> None:
    src = (ROOT / "services/media_domain_directory.py").read_text(encoding="utf-8")
    assert "COALESCE(media_form, '')" in src, "目录读侧没带出 media_form"


def test_migration_registered() -> None:
    assert (ROOT / "scripts/migration_media_form_2026_08_14.sql").exists()
    manifest = (ROOT / "db/migration_manifest.py").read_text(encoding="utf-8")
    assert "scripts/migration_media_form_2026_08_14.sql" in manifest, (
        "迁移没登记 = 生产永远不会跑(preflight 第 2 节同款判据)"
    )


def test_admin_surface_wired() -> None:
    """admin 可改:端点 + 前端页 + 路由 + 侧栏入口四件齐(端点无前端 = 死功能)。"""
    admin_src = (ROOT / "api/admin_api.py").read_text(encoding="utf-8")
    assert '"/media-domain-directory"' in admin_src or "media-domain-directory" in admin_src
    page = ROOT / "frontend/src/pages/Admin/MediaDirectoryAdmin.tsx"
    assert page.exists()
    app_src = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    assert "admin/media-directory" in app_src
    sidebar = (ROOT / "frontend/src/components/layout/AppSidebar.tsx").read_text(encoding="utf-8")
    assert "/admin/media-directory" in sidebar


def test_source_domain_not_exposed_to_frontend() -> None:
    """供应商信息纪律:source_domain 是内部列,admin 列表端点也不吐它。"""
    admin_src = (ROOT / "api/admin_api.py").read_text(encoding="utf-8")
    start = admin_src.index("admin_list_media_domain_directory")
    block = admin_src[start:start + 2200]
    assert "source_domain" not in block
