# -*- coding: utf-8 -*-
"""WO_287 · 结构门:真 app 的每条非公开 /api 路由都必须在 auth/module_mapping.py 里有归属。

没有归属 ⇒ resolve_permission 回 "__unmapped__" ⇒ 全局中间件 fail-closed,只放管理员 ——
对服务商就是「上线即 403 UNMAPPED_ROUTE」,不报错、不告警,只有真人去点才发现
(WO_286 /api/industry-taxonomy 就是这么漏的,MUST_RUN / ONESHOT 全绿都没看见)。

判据站在出口:取的是**真 server.app 的 app.routes**(含 server.py 里直接 @app 的路由与各 include_router 的前缀),
不是静态扫 api/*.py —— 旧的 tests/test_geo_douyin_followup_2026_08_03.py 静态扫描看不见 server.py 的路由。
公开路由(auth.middleware 的 PUBLIC_PATHS / PUBLIC_PREFIXES / PUBLIC_SUFFIXES)不要求映射。
例外只许写进同目录 admin_only_allowlist.tsv(路径 · 类别 · 原因),而且该表**只许减**:
  · 新出现未映射路由、又不在表里 ⇒ 红;
  · 表里的行已被映射或路由已消失 ⇒ 也红(逼着删行,表不许腐烂)。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
ALLOW = pathlib.Path(__file__).with_name("admin_only_allowlist.tsv")
CATEGORIES = {"NO_FRONTEND", "PENDING_REVIEW"}


def _allowlist() -> dict:
    rows = {}
    for line in ALLOW.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.split("\t")
        assert len(cols) == 3, f"允许名单每行三栏(路径 / 类别 / 原因):{line!r}"
        rows[cols[0]] = (cols[1], cols[2])
    return rows


@pytest.fixture(scope="module")
def api_routes():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from server import app

    return sorted({r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")})


def _is_public(probe: str) -> bool:
    from auth.middleware import PUBLIC_PATHS, PUBLIC_PREFIXES, PUBLIC_SUFFIXES

    return probe in PUBLIC_PATHS or probe.startswith(PUBLIC_PREFIXES) or any(probe.endswith(s) for s in PUBLIC_SUFFIXES)


def unmapped(paths) -> list:
    from auth.module_mapping import resolve_permission

    out = []
    for p in paths:
        probe = re.sub(r"\{[^}]+\}", "x", p)   # 中间件拿到的是具体路径;参数段填个占位
        if not _is_public(probe) and resolve_permission(probe) == "__unmapped__":
            out.append(p)
    return out


def test_scanner_sees_the_real_app(api_routes):
    assert len(api_routes) > 1000, f"只扫到 {len(api_routes)} 条 /api 路由,取路由的方式坏了"
    for must in ("/api/industry-taxonomy", "/api/advisors", "/api/c-end/settings/mode"):
        assert must in api_routes, f"连 {must} 都没扫到"


def test_checker_has_teeth():
    assert unmapped(["/api/__wo287_definitely_not_registered__"]) == ["/api/__wo287_definitely_not_registered__"]
    assert unmapped(["/api/industry-taxonomy", "/api/advisors"]) == [], "已映射的路由被误判成未映射"


def test_allowlist_rows_are_well_formed():
    rows = _allowlist()
    assert rows, "允许名单读不到行"
    for path, (cat, why) in rows.items():
        assert cat in CATEGORIES, f"{path}:类别 {cat!r} 不在 {sorted(CATEGORIES)}"
        assert len(why.strip()) >= 2, f"{path}:原因为空"


def test_no_unmapped_route_outside_the_allowlist(api_routes):
    bad = sorted(set(unmapped(api_routes)) - set(_allowlist()))
    assert not bad, ("以下 /api 路由没有模块归属,上线后服务商一律 403 UNMAPPED_ROUTE —— 去 auth/module_mapping.py 映射;"
                     "真要只给 admin,写进 admin_only_allowlist.tsv 并写明原因:\n  " + "\n  ".join(bad))


def test_allowlist_does_not_rot(api_routes):
    live_unmapped = set(unmapped(api_routes))
    stale = sorted(p for p in _allowlist() if p not in live_unmapped)
    assert not stale, "允许名单里这些行已被映射或路由已不存在,删掉它们:\n  " + "\n  ".join(stale)
