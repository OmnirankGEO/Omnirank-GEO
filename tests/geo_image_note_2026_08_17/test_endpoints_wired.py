"""端点轮交验 · 六端点全部挂路由 + 全部过闸 + 全部登记(Review 指定的两条必带)。

🔴 判据形态:打**真 app 对象**与**真 AST**,不是 grep 源码字符串。
   grep 能被一句注释骗过去(本轮已被骗三次)。
"""
from __future__ import annotations

import ast
import io
import pathlib
import re

import pytest

from services.geo_douyin.contract_route_guard import GUARDED_MUTATION_ENDPOINTS
from services.organization_route_contract import (
    MEMBER_GEO_ROUTE_POLICIES, match_member_geo_route,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
API_MODULE = REPO / "api" / "geo_image_note_api.py"

#: 本轮接线的六个 mutation 端点 + 两个只读端点。
#: (method, 路径模板, 真实请求样本, 期望能力)
NEW_ENDPOINTS: tuple[tuple[str, str, str, str], ...] = (
    ("POST", "/api/geo-douyin/production-preview",
     "/api/geo-douyin/production-preview", "writing.generate"),
    ("PUT", "/api/geo-douyin/production-drafts/{draft_id}",
     "/api/geo-douyin/production-drafts/7", "writing.generate"),
    ("POST", "/api/geo-douyin/batches",
     "/api/geo-douyin/batches", "writing.generate"),
    ("GET", "/api/geo-douyin/batches/{batch_id}",
     "/api/geo-douyin/batches/7", "writing.read_own"),
    ("POST", "/api/geo-douyin/posts/{post_id}/prepare-publish-media-v2",
     "/api/geo-douyin/posts/101/prepare-publish-media-v2", "publish.execute"),
    ("POST", "/api/meijiehezi/image-notes/publish-preview",
     "/api/meijiehezi/image-notes/publish-preview", "publish.execute"),
    ("POST", "/api/meijiehezi/image-notes/publish-batch",
     "/api/meijiehezi/image-notes/publish-batch", "publish.execute"),
    ("GET", "/api/geo-douyin/quotes/{quote_id}/delivery-plan",
     "/api/geo-douyin/quotes/410/delivery-plan", "writing.read_own"),
)

#: 必须过 mutation 闸的 handler 名(只读端点不在其中 —— 它们只过 readiness)。
GUARDED_HANDLERS: tuple[str, ...] = (
    "api_production_preview",
    "api_save_production_draft",
    "api_create_batch",
    "api_image_note_publish_preview",
    "api_image_note_publish_batch",
    "api_prepare_publish_media_v2",
)


@pytest.fixture(scope="module")
def routes():
    import server

    out: list[tuple[str, str]] = []
    for route in server.app.routes:
        path = getattr(route, "path", None)
        for method in (getattr(route, "methods", None) or set()):
            if path:
                out.append((str(method).upper(), str(path)))
    return out


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


# ============================================================
# ① 六端点真的挂上了路由
# ============================================================

def test_route_enumeration_is_not_empty(routes):
    assert len(routes) > 200, f"只枚举到 {len(routes)} 条 —— 后面全是恒真"


@pytest.mark.parametrize("method,template,_sample,_ability", NEW_ENDPOINTS)
def test_endpoint_is_registered(routes, method, template, _sample, _ability):
    shapes = {(m, _shape(p)) for m, p in routes}
    assert (method, _shape(template)) in shapes, (
        f"{method} {template} 没注册 —— 闸写了但没有载体"
    )


@pytest.mark.parametrize("method,template,_sample,_ability", NEW_ENDPOINTS)
def test_endpoint_does_not_collide(routes, method, template, _sample, _ability):
    """全仓零冲突(P0-4 同一把尺子,范围扩到本轮全部新端点)。"""
    shape = _shape(template)
    hits = [1 for m, p in routes if m == method and _shape(p) == shape]
    assert len(hits) == 1, f"{method} {template} 命中 {len(hits)} 条路由 —— 撞车"


# ============================================================
# ② 六端点全部过 contract_route_guard(Review 必带 · AST 判据)
# ============================================================

def _handler_calls(func_name: str) -> set[str]:
    """AST 取该 handler 函数体里调用过的函数名。

    🔴 用 AST 不用 grep:grep 会被注释里出现的 `_guard(` 骗过去,
       而本轮已经被"打在注释上"骗了三次。
    """
    tree = ast.parse(io.open(API_MODULE, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return {
                sub.func.id for sub in ast.walk(node)
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            }
    raise AssertionError(f"handler {func_name} 不存在于 {API_MODULE.name}")


@pytest.mark.parametrize("handler", GUARDED_HANDLERS)
def test_every_mutation_handler_calls_the_guard(handler):
    assert "_guard" in _handler_calls(handler), (
        f"{handler} 没调用入口闸 —— schema/demo/ability 三道全部落空"
    )


def test_ast_probe_has_discriminating_power():
    """反向对照:AST 探针必须能区分"调用了"与"只在注释里出现"。"""
    tree = ast.parse("def f():\n    # _guard(x)\n    other()\n")
    names = {
        sub.func.id for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
    }
    assert "_guard" not in names and "other" in names


def test_guard_is_called_before_any_work(monkeypatch):
    """顺序判据:闸必须在**任何**副作用之前。

    把 `_guard` 换成会抛的哨兵,然后调 handler —— 如果闸之前已经有 DB/资金动作,
    那些动作会先炸出别的异常,而不是我们种的这个。
    """
    import asyncio

    from api import geo_image_note_api as mod

    class _Sentinel(Exception):
        pass

    def _boom(*args, **kwargs):
        raise _Sentinel()

    monkeypatch.setattr(mod, "_guard", _boom)
    monkeypatch.setattr(mod, "_user", lambda request: {"user_id": 1, "is_admin": False})

    req = mod.PublishBatchRequest(request_id="r", expected_total_price_points=0, items=[])
    with pytest.raises(_Sentinel):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            mod.api_image_note_publish_batch(req, request=object()))


# ============================================================
# ③ 组织路由同步登记(P0-7 口径)
# ============================================================

@pytest.mark.parametrize("method,_template,sample,ability", NEW_ENDPOINTS)
def test_every_endpoint_is_registered_in_route_contract(method, _template, sample, ability):
    policy = match_member_geo_route(method, sample)
    assert policy is not None, (
        f"{method} {sample} 未登记 MEMBER_GEO_ROUTE_POLICIES → 交付操作员守卫层 403"
    )
    assert policy.capability == ability, (
        f"{method} {sample} 能力是 {policy.capability},期望 {ability}"
    )
    assert policy.resource_kind, "没绑 resource_kind = 没人负责对象级二次授权"


def test_production_and_publish_stay_on_separate_abilities():
    """制作与发布分属两条能力 —— 合并 = 能做就能发(规格 §9)。"""
    produce = {p.capability for p in MEMBER_GEO_ROUTE_POLICIES
               if "/api/geo-douyin/batches" == p.path_template and p.method == "POST"}
    publish = {p.capability for p in MEMBER_GEO_ROUTE_POLICIES
               if "image-notes/publish-batch" in p.path_template}
    assert produce == {"writing.generate"}
    assert publish == {"publish.execute"}


def test_billing_features_are_mapped_for_billable_routes():
    """带 billing 的路由,其 feature code 必须有能力映射 —— 否则 member 走不通。"""
    from services.organization_contract import BILLABLE_FEATURE_CAPABILITIES

    for policy in MEMBER_GEO_ROUTE_POLICIES:
        if policy.billing_feature and "geo-douyin" in policy.path_template or (
                policy.billing_feature and "image-notes" in policy.path_template):
            assert policy.billing_feature in BILLABLE_FEATURE_CAPABILITIES, (
                f"{policy.path_template} 的 billing={policy.billing_feature} 没有能力映射"
            )


def test_unlisted_sibling_paths_remain_fail_closed():
    """反向对照:同 prefix 未列出的路径必须仍打不进去(不接受广义前缀匹配)。"""
    for method, path in (
        ("DELETE", "/api/geo-douyin/batches/7"),
        ("POST", "/api/geo-douyin/batches/7/secret"),
        ("POST", "/api/meijiehezi/image-notes/publish-batch/force"),
    ):
        assert match_member_geo_route(method, path) is None, f"{method} {path} 被放行"


def test_guarded_endpoint_constant_matches_reality():
    """`GUARDED_MUTATION_ENDPOINTS` 不许比实际接线少 —— 少了就意味着有端点没人管。"""
    templates = {t for _m, t, _s, _a in NEW_ENDPOINTS}
    for guarded in GUARDED_MUTATION_ENDPOINTS:
        base = guarded.rstrip("/")
        assert any(base in t or t.startswith(base) for t in templates), (
            f"闸清单里的 {guarded} 在本轮端点清单里找不到对应项 —— 两张清单漂移了"
        )
