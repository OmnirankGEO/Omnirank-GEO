"""
Phase 4 PLAN 02 Task 1 · one_click_geo_plan 重构单测

测试 8 个,对齐 PLAN 02 Task 1 <behavior>:
  1. test_progress_callback_called_5_stages · identify/expand/audit/cluster/pricing 都被调
  2. test_no_more_asyncio_wait_for · 源码 grep asyncio.wait_for 在非注释行为 0
  3. test_no_degraded_branch · 源码里 degraded=True 的 return 分支已删
  4. test_expand_fails_raises_external_api_error · expand 抛异常 → ExternalAPIError('expand_keywords')
  5. test_expand_low_count_raises_low_quality · expand 返 <10 词 → LowQualityResult('expand_too_few')
  6. test_cluster_zero_raises_low_quality · cluster 空 → LowQualityResult('cluster_empty')
  7. test_progress_callback_none_ok · 不传 callback 不报错
  8. test_brand_snapshot_used_when_passed · 传 brand_snapshot 时跳过查 DB

运行:
  docker exec omnirank-ai pytest tests/test_c_end_cost_estimate_progress.py -x -v
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    import pytest
except ImportError:
    class _PytestStub:
        @staticmethod
        def skip(msg): raise Exception(f'SKIP: {msg}')
        @staticmethod
        def fail(msg): raise AssertionError(msg)
        class fixture:
            def __init__(self, *a, **kw): pass
            def __call__(self, fn): return fn
        class mark:
            @staticmethod
            def asyncio(fn): return fn
        class raises:
            def __init__(self, exc): self.exc = exc
            def __enter__(self): return self
            def __exit__(self, t, v, tb):
                if t is None or not issubclass(t, self.exc):
                    raise AssertionError(f'expected {self.exc.__name__}, got {t}')
                return True
    pytest = _PytestStub()  # type: ignore[assignment]

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ============================================================
# 预先 import 所有将被 patch 的模块,保证 patch 时属性存在
# ============================================================


def _ensure_modules():
    """保证这些模块在 sys.modules,patch 时才能找到它们."""
    # 先 import,否则 late-import 后 patch 失效 (local 名字绑定早于 patch 生效)
    import importlib
    try:
        import services.placement_service  # noqa
    except Exception:
        pass
    try:
        import services.llm.advisor_llm  # noqa
    except Exception:
        pass
    try:
        import tools.keyword_expander  # noqa
    except Exception:
        pass
    try:
        import tools.batch_pricing  # noqa
    except Exception:
        pass
    # cache.redis_client + db.analytics_db 是可选模块,可能没有
    try:
        import cache.redis_client  # noqa
    except Exception:
        # 占位模块,避免 late-import 失败
        cache_pkg = sys.modules.setdefault("cache", types.ModuleType("cache"))
        stub = types.ModuleType("cache.redis_client")
        stub.redis_get_json = lambda *a, **kw: None
        stub.redis_set_json = lambda *a, **kw: None
        sys.modules["cache.redis_client"] = stub
        setattr(cache_pkg, "redis_client", stub)
    try:
        import db.analytics_db  # noqa
    except Exception:
        db_pkg = sys.modules.setdefault("db", types.ModuleType("db"))
        stub = types.ModuleType("db.analytics_db")
        stub.log_industry_mapping = lambda *a, **kw: None
        sys.modules["db.analytics_db"] = stub
        setattr(db_pkg, "analytics_db", stub)


_ensure_modules()


# ============================================================
# 通用 mock 工具
# ============================================================


def _make_success_expand(n: int = 30):
    """造 expand_keywords_for_client 的成功返回."""
    return {
        "success": True,
        "keywords": [
            {"keyword": f"测试词{i}", "source": "5118"} for i in range(n)
        ],
    }


def _make_success_cluster(n_clusters: int = 3):
    """造 generate_cluster_quote 的成功返回 · 至少含 clusters + tier_summaries."""
    clusters = []
    for c in range(n_clusters):
        clusters.append({
            "theme": f"主题{c}",
            "core_keywords": [
                {
                    "keyword": f"核心词{c}_1",
                    "source": "5118",
                    "entry": {"price": 100 + c * 10},
                    "standard": {"price": 200 + c * 10},
                    "flagship": {"price": 300 + c * 10},
                    "strong": {"price": 400 + c * 10},
                    "competitor_count": 20,
                    "effective_competition": 15,
                    "recommended_platforms": [{"platform": "知乎"}],
                }
            ],
            "covered_keywords": [],
        })
    return {
        "clusters": clusters,
        "tier_summaries": {
            "entry": {"core_price": 100, "full_price": 500},
            "standard": {"core_price": 200, "full_price": 1000},
            "flagship": {"core_price": 300, "full_price": 1500},
            "strong": {"core_price": 400, "full_price": 2000},
        },
        "stats": {"cluster_count": n_clusters},
    }


def _make_llm_identify_json():
    return '{"industry":"餐饮","industry_display":"餐饮","city":"北京","core_keywords":["老王家常菜","北京餐饮"]}'


class _FakeSvc:
    def get_all_industries(self):
        return ["餐饮", "美业", "通用"]

    def _match_research_industry(self, x, **kw):   # WO_267:真实接口多了 brand= 关键字
        return (None, x)


def _patch_all(
    expand_return=None,
    expand_side_effect=None,
    cluster_return=None,
    cluster_side_effect=None,
):
    """用 ExitStack 批量 patch 所有外部调用."""
    from contextlib import ExitStack
    stack = ExitStack()

    # 1. placement_service
    stack.enter_context(patch(
        "services.placement_service.get_placement_service",
        return_value=_FakeSvc(),
    ))
    # 2. advisor_flash (品牌识别 LLM · 如果用了 profile_data/brand_snapshot 会跳过)
    stack.enter_context(patch(
        "services.llm.advisor_llm.advisor_flash",
        new=AsyncMock(return_value=_make_llm_identify_json()),
    ))
    # 3. Redis
    stack.enter_context(patch("cache.redis_client.redis_get_json", return_value=None))
    stack.enter_context(patch("cache.redis_client.redis_set_json", return_value=None))
    # 4. 分析日志
    stack.enter_context(patch("db.analytics_db.log_industry_mapping", return_value=None))
    # 5. expand_keywords_for_client
    if expand_side_effect is not None:
        stack.enter_context(patch(
            "tools.keyword_expander.expand_keywords_for_client",
            new=AsyncMock(side_effect=expand_side_effect),
        ))
    else:
        stack.enter_context(patch(
            "tools.keyword_expander.expand_keywords_for_client",
            new=AsyncMock(return_value=expand_return if expand_return is not None else _make_success_expand()),
        ))
    # 6. generate_cluster_quote
    if cluster_side_effect is not None:
        stack.enter_context(patch(
            "tools.batch_pricing.generate_cluster_quote",
            new=AsyncMock(side_effect=cluster_side_effect),
        ))
    else:
        stack.enter_context(patch(
            "tools.batch_pricing.generate_cluster_quote",
            new=AsyncMock(return_value=cluster_return if cluster_return is not None else _make_success_cluster()),
        ))
    return stack


# ============================================================
# 测试
# ============================================================


async def test_progress_callback_called_5_stages():
    """happy path · progress_callback 至少覆盖 identify/expand/audit/cluster/pricing 5 阶段."""
    from tools.c_end_cost_estimate import one_click_geo_plan

    calls: list = []

    async def cb(stage, percent, message):
        calls.append((stage, percent, message))

    with _patch_all():
        # 传 profile_data 跳过 LLM 识别避免依赖 advisor_flash 真实调用
        result = await one_click_geo_plan(
            brand_name="老王家常菜",
            description="北京本地家常菜小馆",
            profile_data={"industry": "餐饮", "city": "北京", "business": "家常菜"},
            progress_callback=cb,
        )

    assert isinstance(result, dict)
    assert "clusters" in result and result["clusters"], f"result clusters 为空: {result}"
    stages = [c[0] for c in calls]
    for s in ("identify", "expand", "audit", "cluster", "pricing"):
        assert s in stages, f"缺少阶段 {s}, 实际 {stages}"
    percents = [c[1] for c in calls]
    assert percents == sorted(percents), f"进度非单调递增: {percents}"


def test_no_more_asyncio_wait_for():
    """重构后 tools/c_end_cost_estimate.py 源码 asyncio.wait_for 在非注释行上不再出现."""
    src = (Path(__file__).parent.parent / "tools" / "c_end_cost_estimate.py").read_text(encoding="utf-8")
    bad: list[tuple[int, str]] = []
    in_doc = False
    doc_quote = None
    for i, line in enumerate(src.splitlines(), start=1):
        stripped = line.strip()
        if not in_doc:
            if stripped.startswith('"""') or stripped.startswith("'''"):
                q = stripped[:3]
                doc_quote = q
                if stripped.count(q) >= 2:
                    continue
                in_doc = True
                continue
        else:
            if doc_quote and doc_quote in stripped:
                in_doc = False
            continue
        if stripped.startswith("#"):
            continue
        if "asyncio.wait_for" in line:
            bad.append((i, line.strip()))
    assert not bad, f"代码中仍有 asyncio.wait_for: {bad}"


def test_no_degraded_branch():
    """源码里 'degraded': True 的 return 分支应被删除."""
    src = (Path(__file__).parent.parent / "tools" / "c_end_cost_estimate.py").read_text(encoding="utf-8")
    offending = re.findall(r"[\"']degraded[\"']\s*:\s*True", src)
    assert not offending, f"源码仍含有 'degraded': True: {offending}"


async def test_expand_fails_raises_external_api_error():
    """expand_keywords_for_client 抛异常 → one_click_geo_plan raise ExternalAPIError."""
    from tools.c_end_cost_estimate import one_click_geo_plan, ExternalAPIError

    with _patch_all(expand_side_effect=ConnectionError("5118 timeout")):
        with pytest.raises(ExternalAPIError) as ei:
            await one_click_geo_plan(
                brand_name="老王家常菜",
                description="测试",
                profile_data={"industry": "餐饮", "city": "北京", "business": "家常菜"},
                progress_callback=None,
            )

    assert getattr(ei.value, "source", "") == "expand_keywords"


async def test_expand_low_count_raises_low_quality():
    """expand 返 <10 词 → LowQualityResult('expand_too_few')."""
    from tools.c_end_cost_estimate import one_click_geo_plan, LowQualityResult

    with _patch_all(expand_return=_make_success_expand(n=5)):
        with pytest.raises(LowQualityResult) as ei:
            await one_click_geo_plan(
                brand_name="老王家常菜",
                description="测试",
                profile_data={"industry": "餐饮", "city": "北京", "business": "家常菜"},
                progress_callback=None,
            )

    assert ei.value.reason == "expand_too_few"
    assert isinstance(ei.value.partial, dict)
    assert "keywords" in ei.value.partial


async def test_cluster_zero_raises_low_quality():
    """cluster 返空 clusters → LowQualityResult('cluster_empty')."""
    from tools.c_end_cost_estimate import one_click_geo_plan, LowQualityResult

    empty_cluster = {"clusters": [], "tier_summaries": {}, "stats": {"cluster_count": 0}}
    with _patch_all(cluster_return=empty_cluster):
        with pytest.raises(LowQualityResult) as ei:
            await one_click_geo_plan(
                brand_name="老王家常菜",
                description="测试",
                profile_data={"industry": "餐饮", "city": "北京", "business": "家常菜"},
                progress_callback=None,
            )

    assert ei.value.reason == "cluster_empty"


async def test_progress_callback_none_ok():
    """不传 progress_callback (= None) 时不报错,向后兼容老调用链."""
    from tools.c_end_cost_estimate import one_click_geo_plan

    with _patch_all():
        result = await one_click_geo_plan(
            brand_name="老王家常菜",
            description="北京本地家常菜小馆",
            profile_data={"industry": "餐饮", "city": "北京", "business": "家常菜"},
        )
    assert isinstance(result, dict)
    assert "clusters" in result


async def test_brand_snapshot_used_when_passed():
    """传 brand_snapshot 时不查实时 DB (D13 brand 中途被删防御)."""
    from tools.c_end_cost_estimate import one_click_geo_plan

    snap = {
        "id": 170,
        "name": "老王家常菜",
        "industry": "餐饮",
        "description": "snapshot 里的描述",
        "cities": "北京",
        "profile": {
            "business": "家常菜",
            "industry": "餐饮",
            "city": "北京",
            "target_users": "上班族",
            "industry_brief_status": "done",
        },
    }
    calls: list = []

    async def cb(stage, percent, message):
        calls.append((stage, percent))

    with _patch_all():
        result = await one_click_geo_plan(
            brand_snapshot=snap,
            progress_callback=cb,
        )
    assert result.get("brand_name") == "老王家常菜"
    assert any(c[0] == "identify" for c in calls)
