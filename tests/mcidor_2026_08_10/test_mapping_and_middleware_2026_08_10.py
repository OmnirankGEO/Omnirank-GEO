"""P0 IDOR 热修 · 修法②③ 的元判据(不需要 DB / 不需要 import server)。

修法②:`/api/marketing-confirm/*` 必须由**它自己那条**映射决定,不能再被
        `/api/marketing` 前缀吞掉。
修法③:中间件 `_extract_brand_id` 认 path 参数 —— 但只认**白名单内**的形态,
        白名单外(尤其尾段是 quote_id / diagnosis_id 的路由)必须**不**命中,
        否则安全网会拿 quote_id 去比对 allowed_brands,把合法请求判成越权 403。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth.module_mapping import ROUTE_PREFIX_MAP, resolve_permission  # noqa: E402


# ============================================================================
# 修法② · 映射
# ============================================================================

MC_PATHS = [
    "/api/marketing-confirm/status/662",
    "/api/marketing-confirm/resend/662",
    "/api/marketing-confirm/generate-link",
]


def _first_matching_prefix(path: str) -> str:
    """复刻 `resolve_permission` 的匹配语义:**列表顺序优先**,第一个 startswith 命中即返回。

    🔴 本文件头部注释说"按最长前缀优先排序"是**人工约定,不是运行时行为** ——
       所以"新条目排在 /api/marketing 之前"这件事必须被锁住,不能只锁"条目存在"。
    """
    for prefix, _module in ROUTE_PREFIX_MAP:
        if path.startswith(prefix):
            return prefix
    return ""


def test_marketing_confirm_is_registered_on_its_own_entry():
    """三条路径都必须命中 marketing-confirm 自己的条目,不是 /api/marketing。"""
    for path in MC_PATHS:
        prefix = _first_matching_prefix(path)
        assert prefix.startswith("/api/marketing-confirm"), (
            f"{path} 命中的是「{prefix}」—— 又被 /api/marketing 前缀吞了"
        )


def test_marketing_confirm_entry_sorts_before_marketing():
    """顺序判据(这才是真正会坏的那件事)。

    有人把新条目挪到 `/api/marketing` 之后,`test_..._own_entry` 会红;
    但如果只写那一条,一个"条目存在但排在后面"的实现在**某些**路径上仍可能碰巧过 ——
    所以这里直接钉死索引先后。
    """
    prefixes = [p for p, _ in ROUTE_PREFIX_MAP]
    mc = min(i for i, p in enumerate(prefixes) if p.startswith("/api/marketing-confirm"))
    mk = min(i for i, p in enumerate(prefixes) if p == "/api/marketing" or p == "/api/marketing/")
    assert mc < mk, (
        f"marketing-confirm 条目(idx={mc})排在 /api/marketing(idx={mk})之后 —— "
        f"resolve_permission 是列表顺序优先,这样等于没登记"
    )


def test_marketing_factory_paths_are_untouched():
    """反向对照:营销物料工厂本身的路由判定必须**一个字没变**。

    没有这条,一个"把 /api/marketing 也一起改了"的实现同样能让上面两条绿。
    """
    for path in ("/api/marketing/templates", "/api/marketing/tasks", "/api/marketing"):
        assert _first_matching_prefix(path) in ("/api/marketing/", "/api/marketing"), path
        assert resolve_permission(path) is None, path


def test_marketing_confirm_resolves_to_login_only_not_unmapped():
    """判定值本身:仅需认证(None),不是 `__unmapped__`(那会把合法服务商全挡在门外)。"""
    for path in MC_PATHS:
        assert resolve_permission(path) is None, (path, resolve_permission(path))


# ============================================================================
# 修法③ · 中间件 path 参数
# ============================================================================

def _extract(path: str, query: dict | None = None):
    """直接跑真实 `_extract_brand_id`,喂一个最小的 request 替身。

    它只读 `request.query_params` 与 `request.url.path` —— 用 SimpleNamespace 足够,
    不 import server、不连 DB。
    """
    from auth.middleware import _extract_brand_id
    request = SimpleNamespace(
        query_params=query or {},
        url=SimpleNamespace(path=path),
    )
    return _extract_brand_id(request)


def test_whitelisted_path_brand_id_is_extracted():
    """白名单内:尾段确实是 brand_id → 必须被安全网看见。"""
    assert _extract("/api/marketing-confirm/status/662") == 662
    assert _extract("/api/marketing-confirm/resend/662") == 662
    # 带尾斜杠也要认
    assert _extract("/api/marketing-confirm/status/662/") == 662


def test_non_whitelisted_paths_are_not_misread_as_brand_id():
    """🔴 反向对照,而且是本条改动**最危险**的那一面。

    这些路由的尾段是 quote_id / diagnosis_id / article_id,**不是** brand_id。
    一旦被当成 brand_id,全局安全网会拿它去比对 allowed_brands →
    合法请求被判越权 403(fail-closed 方向的误伤,比漏更容易上生产才发现)。
    """
    for path in (
        "/api/publications/123",
        "/api/distill/123",
        "/api/quotes/123",
        "/api/reports/123",
        "/api/marketing/tasks/123",          # 营销物料工厂,不是 confirm
        "/api/marketing-confirm/generate-link",  # 无尾段 id(brand_id 在 body)
        "/api/m/some-token",                 # 公开门户,尾段是 token 不是 id
    ):
        assert _extract(path) is None, f"{path} 的尾段被误读成 brand_id 了"


def test_query_param_path_still_wins_and_old_behavior_intact():
    """既有行为零回归:query brand_id 优先,`/api/client-context/{id}` 照旧。"""
    assert _extract("/api/anything", {"brand_id": "77"}) == 77
    assert _extract("/api/client-context/88") == 88
    # query 优先于 path
    assert _extract("/api/marketing-confirm/status/662", {"brand_id": "77"}) == 77


def test_whitelist_entries_all_end_with_a_slash():
    """元判据:白名单条目必须以 `/` 结尾。

    少一个斜杠就会变成前缀吞并(正是 `/api/marketing` 吞掉 `/api/marketing-confirm` 的同型错误)。
    """
    from auth.middleware import _PATH_BRAND_ID_PREFIXES
    assert _PATH_BRAND_ID_PREFIXES, "白名单是空的 —— 判据失效,不是通过"
    for prefix in _PATH_BRAND_ID_PREFIXES:
        assert prefix.endswith("/"), prefix
