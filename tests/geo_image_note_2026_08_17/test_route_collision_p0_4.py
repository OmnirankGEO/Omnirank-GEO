"""P0-4 · 全仓路由零冲突证明(裁定 2026-08-17 要求「附全仓路由零冲突证明」)

裁定原文:`GET /api/quotes/{quote_id}/delivery-plan` 已被 `api/gap_plan_api` 注册
(`server.py:772` 挂载,前缀 `/api/quotes`,服务文章侧缺口作战计划),图文侧改用
`/api/geo-douyin/quotes/{quote_id}/delivery-plan`。

🔴 判据形态:**机械枚举全部已注册路由**再求交,不是 grep 某个字符串、也不是肉眼看。
   每条「必须不冲突」都配一条「必须冲突」的反向对照 —— 否则"零冲突"可能只是
   枚举器压根没枚举到东西(本仓记过:锁全绿是因为夹具是空的)。
"""
from __future__ import annotations

import re

import pytest


# 图文侧本包新增/扩展的端点面(与 02 §9.0 登记表、01/02/03 文档同一份清单)
GEO_IMAGE_NOTE_PATHS: tuple[tuple[str, str], ...] = (
    ("GET", "/api/geo-douyin/quotes/{quote_id}/delivery-plan"),
)

# 裁定点名的、**必须被避开**的既有路径
COLLIDING_LEGACY_PATH = "/api/quotes/{quote_id}/delivery-plan"


def _template_shape(path: str) -> str:
    """把 `{任意参数名}` 归一成 `{}`,这样 `/a/{quote_id}/b` 与 `/a/{qid}/b` 判为同形。

    路由冲突看的是**形状**不是参数名 —— 只比字面量会漏掉真冲突。
    """
    return re.sub(r"\{[^}]*\}", "{}", path)


def _registered_routes() -> list[tuple[str, str]]:
    """枚举 server.py 装配后的全部路由。取的是真 app 对象,不是源码扫描。"""
    import server

    out: list[tuple[str, str]] = []
    for route in server.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path:
            continue
        for method in methods:
            out.append((str(method).upper(), str(path)))
    return out


@pytest.fixture(scope="module")
def routes() -> list[tuple[str, str]]:
    return _registered_routes()


def test_route_enumeration_is_not_empty(routes):
    """反向对照 ①:枚举器必须真的枚举到东西。

    「零冲突」最容易的自造法就是枚举出空集合。分母先立住,后面的结论才有意义。
    """
    assert len(routes) > 200, f"只枚举到 {len(routes)} 条路由 —— 枚举器没工作,后续判据全是恒真"


def test_colliding_legacy_path_really_is_occupied(routes):
    """反向对照 ②:裁定说的那条冲突路径**确实**被占。

    如果它其实没被占,那改名这件事就是无中生有 —— 这条会当场把假前提顶红。
    """
    shapes = {(m, _template_shape(p)) for m, p in routes}
    target = ("GET", _template_shape(COLLIDING_LEGACY_PATH))
    assert target in shapes, (
        f"{COLLIDING_LEGACY_PATH} 实际未被注册 —— 裁定 P0-4 的前提不成立,改名理由需要重写"
    )


def test_geo_image_note_paths_do_not_collide(routes):
    """正向:本包新增路径与全仓既有路由零冲突(同 method + 同形状 = 冲突)。"""
    existing = [(m, _template_shape(p)) for m, p in routes]
    offenders: dict[str, list[str]] = {}
    for method, path in GEO_IMAGE_NOTE_PATHS:
        shape = _template_shape(path)
        hits = [f"{m} {p}" for m, p in existing if m == method and p == shape]
        # 本包自己注册的那一条会命中自己 —— 冲突的定义是命中 **>1** 条
        if len(hits) > 1:
            offenders[f"{method} {path}"] = hits
    assert not offenders, f"新增图文路由与既有路由撞车:\n{offenders}"


def test_new_delivery_plan_path_is_actually_registered(routes):
    """接线锁:改名后的路径**真的**注册上了,而不是只在文档里改了个名字。

    没有这一条,上面那条「零冲突」可以靠"这个路由压根不存在"来恒绿。
    """
    shapes = {(m, _template_shape(p)) for m, p in routes}
    for method, path in GEO_IMAGE_NOTE_PATHS:
        assert (method, _template_shape(path)) in shapes, (
            f"{method} {path} 未注册 —— 文档改了名但代码没接线"
        )


def test_collision_detector_fires_on_a_synthetic_duplicate(routes):
    """反向对照 ③:把一条已存在的路由再"注册"一次,检测器必须报冲突。

    证明 test_geo_image_note_paths_do_not_collide 的判别力不是来自"永远找不到 2 条"。
    """
    existing = [(m, _template_shape(p)) for m, p in routes]
    victim = ("GET", _template_shape(COLLIDING_LEGACY_PATH))
    synthetic = existing + [victim]  # 人为造出第二条同形路由
    hits = [1 for m, p in synthetic if (m, p) == victim]
    assert len(hits) > 1, "合成重复路由后检测器仍未看到 >1 条 —— 检测逻辑失效"
