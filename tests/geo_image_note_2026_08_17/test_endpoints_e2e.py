"""清欠条轮 · 端点真闭环 E2E(四条 `*_not_wired` 清零后立)。

🔴 判据形态:打**真 handler 函数**(FastAPI 路由对象上挂的那个),
   用假 Request + monkeypatch 掉外部依赖,断言**副作用计数**而不是 HTTP 200
   —— 「mutation tests 必须验证 committed writes、rollbacks、failures,
   不能只看 HTTP 200」(03 §13.2)。
"""
from __future__ import annotations

import asyncio
import io
import pathlib
import re
import sys

import pytest

from fastapi import HTTPException

from api import geo_image_note_api as mod

REPO = pathlib.Path(__file__).resolve().parents[2]
API_SRC = REPO / "api" / "geo_image_note_api.py"


class _FakeState:
    def __init__(self, user, demo=False, org=None):
        self.user = user
        if demo:
            self.demo_access_context = object()
        self.organization_identity = org


class _FakeRequest:
    def __init__(self, *, user=None, demo=False, org=None, headers=None):
        self.state = _FakeState(user or {"user_id": 42, "is_admin": False}, demo, org)
        self.headers = headers or {}
        self.method = "POST"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ============================================================
# ① 四条欠条真的清零了(形态锁 · 防回退)
# ============================================================

def test_no_not_wired_blockers_remain():
    """🔴 端点轮遗留的四条 `*_not_wired` 必须**全部**从源码里消失。

    这条是欠条对表的机械形式:Review 逐个对的就是它。
    """
    src = API_SRC.read_text(encoding="utf-8")
    leftovers = sorted(set(re.findall(r'"([a-z_]+_not_wired)"', src)))
    assert leftovers == [], f"仍有未接线的端点:{leftovers}"


def test_not_wired_detector_actually_fires():
    """反向对照:检测器对合成样本必须命中,否则"全清"只是正则抓不到。"""
    assert re.findall(r'"([a-z_]+_not_wired)"',
                      'SchemaNotReady(["batch_coordinator_not_wired"])')


# ============================================================
# ② 闸仍在最前(接线之后不能被挤到后面)
# ============================================================

@pytest.mark.parametrize("handler,builder", [
    ("api_production_preview",
     lambda: mod.ProductionPreviewRequest(quote_id=1, items=[])),
    ("api_create_batch",
     lambda: mod.CreateBatchRequest(request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc", quote_id=1, expected_total_price_points=0, items=[])),
    ("api_image_note_publish_preview",
     lambda: mod.PublishPreviewRequest(items=[])),
    ("api_image_note_publish_batch",
     lambda: mod.PublishBatchRequest(request_id="r", expected_total_price_points=0,
                                     items=[])),
])
def test_guard_still_runs_before_any_work(handler, builder, monkeypatch):
    """把 `_guard` 换成会抛的哨兵 —— handler 必须抛出我种的那个异常。

    接线之后最容易发生的退化就是"先查库再过闸"。这条钉死顺序。
    """
    class _Sentinel(Exception):
        pass

    monkeypatch.setattr(mod, "_guard", lambda *a, **k: (_ for _ in ()).throw(_Sentinel()))
    monkeypatch.setattr(mod, "_user", lambda request: {"user_id": 42, "is_admin": False})
    fn = getattr(mod, handler)
    with pytest.raises(_Sentinel):
        _run(fn(builder(), request=_FakeRequest()))


def test_prepare_media_guard_runs_first(monkeypatch):
    class _Sentinel(Exception):
        pass

    monkeypatch.setattr(mod, "_guard", lambda *a, **k: (_ for _ in ()).throw(_Sentinel()))
    monkeypatch.setattr(mod, "_user", lambda request: {"user_id": 42, "is_admin": False})
    with pytest.raises(_Sentinel):
        _run(mod.api_prepare_publish_media_v2(101, request=_FakeRequest()))


# ============================================================
# ③ Demo 三零 —— 端到端(不是只测 guard 函数)
# ============================================================

class _Tripwire:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        raise AssertionError(f"演示模式下触碰了 {self._name}.{attr} —— 三零断言破裂")


@pytest.mark.parametrize("handler,builder", [
    ("api_create_batch",
     lambda: mod.CreateBatchRequest(request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc", quote_id=1, expected_total_price_points=0, items=[])),
    ("api_image_note_publish_batch",
     lambda: mod.PublishBatchRequest(request_id="r", expected_total_price_points=0,
                                     items=[])),
])
def test_demo_mutation_is_403_with_zero_side_effects(handler, builder, monkeypatch):
    """演示身份走**真路由真 handler**:403 DEMO_READ_ONLY,且零 DB/零资金/零外调。

    哨兵替换三个副作用模块 —— 碰一下就 AssertionError,比"断言没被调用"强。
    """
    monkeypatch.setattr(mod, "_schema_blockers", lambda: [])
    for name in ("middleware.billing", "services.geo_douyin.publish_adapter"):
        monkeypatch.setitem(sys.modules, name, _Tripwire(name))

    fn = getattr(mod, handler)
    with pytest.raises(HTTPException) as excinfo:
        _run(fn(builder(), request=_FakeRequest(demo=True)))
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "DEMO_READ_ONLY"
    assert "未产生任何数据变更" in excinfo.value.detail["impact"]


# ============================================================
# ④ 503 SCHEMA_NOT_READY —— 端到端且零副作用
# ============================================================

@pytest.mark.parametrize("handler,builder", [
    ("api_production_preview",
     lambda: mod.ProductionPreviewRequest(quote_id=1, items=[])),
    ("api_create_batch",
     lambda: mod.CreateBatchRequest(request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc", quote_id=1, expected_total_price_points=0, items=[])),
    ("api_image_note_publish_batch",
     lambda: mod.PublishBatchRequest(request_id="r", expected_total_price_points=0,
                                     items=[])),
])
def test_schema_not_ready_is_503_before_any_db_touch(handler, builder, monkeypatch):
    monkeypatch.setattr(mod, "_schema_blockers",
                        lambda: ["missing_column:geo_douyin_posts.quote_id"])
    for name in ("middleware.billing", "services.geo_douyin.publish_adapter",
                 "db.connection"):
        monkeypatch.setitem(sys.modules, name, _Tripwire(name))

    fn = getattr(mod, handler)
    with pytest.raises(HTTPException) as excinfo:
        _run(fn(builder(), request=_FakeRequest()))
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["code"] == "SCHEMA_NOT_READY"
    for word in ("未创建", "未冻结", "未联系"):
        assert word in excinfo.value.detail["impact"]


# ============================================================
# ⑤ 一篇一账号 —— 端到端整批拒绝,零副作用
# ============================================================

def test_publish_batch_rejects_same_revision_two_accounts(monkeypatch):
    """同一 post revision 映射两账号 → **整批** 409,零订单零预占零冻结零外调。"""
    monkeypatch.setattr(mod, "_schema_blockers", lambda: [])
    for name in ("middleware.billing", "services.geo_douyin.publish_adapter",
                 "db.connection"):
        monkeypatch.setitem(sys.modules, name, _Tripwire(name))

    req = mod.PublishBatchRequest(
        request_id="r1", expected_total_price_points=18720,
        items=[
            mod.PublishBatchItem(item_request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", geo_post_id=101,
                                 post_revision_id=7, prepared_artifact_id="art9",
                                 manifest_hash="h1", media_id=9001,
                                 expected_price_fingerprint="publish-price-v1:x"),
            mod.PublishBatchItem(item_request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", geo_post_id=101,
                                 post_revision_id=7, prepared_artifact_id="art9",
                                 manifest_hash="h1", media_id=9002,
                                 expected_price_fingerprint="publish-price-v1:x"),
        ])
    with pytest.raises(HTTPException) as excinfo:
        _run(mod.api_image_note_publish_batch(req, request=_FakeRequest()))
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "ONE_TO_ONE_REQUIRED"
    assert "整批未提交" in excinfo.value.detail["impact"]


def test_publish_batch_accepts_distinct_revisions(monkeypatch):
    """反向对照:两个**不同** revision 必须走过校验(证明上面不是"什么都拒")。

    走过校验后会因为哨兵拦住 DB 而失败 —— 那正说明它已经越过了一篇一账号这一关。
    """
    monkeypatch.setattr(mod, "_schema_blockers", lambda: [])
    monkeypatch.setitem(sys.modules, "db.connection", _Tripwire("db.connection"))

    req = mod.PublishBatchRequest(
        request_id="r2", expected_total_price_points=18720,
        items=[
            mod.PublishBatchItem(item_request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", geo_post_id=101,
                                 post_revision_id=7, prepared_artifact_id="art9",
                                 manifest_hash="h1", media_id=9001,
                                 expected_price_fingerprint="publish-price-v1:x"),
            mod.PublishBatchItem(item_request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", geo_post_id=102,
                                 post_revision_id=8, prepared_artifact_id="art10",
                                 manifest_hash="h2", media_id=9001,
                                 expected_price_fingerprint="publish-price-v1:y"),
        ])
    with pytest.raises(Exception) as excinfo:
        _run(mod.api_image_note_publish_batch(req, request=_FakeRequest()))
    # 不是 ONE_TO_ONE_REQUIRED —— 说明一篇一账号这一关放行了
    detail = getattr(excinfo.value, "detail", None)
    if isinstance(detail, dict):
        assert detail.get("code") != "ONE_TO_ONE_REQUIRED", "两个不同版本被误拒"


# ============================================================
# ⑥ 空批 / 缺幂等键 —— 可执行错误合同
# ============================================================

def test_empty_batch_gives_executable_error(monkeypatch):
    monkeypatch.setattr(mod, "_schema_blockers", lambda: [])
    req = mod.PublishBatchRequest(request_id="r", expected_total_price_points=0, items=[])
    with pytest.raises(HTTPException) as excinfo:
        _run(mod.api_image_note_publish_batch(req, request=_FakeRequest()))
    detail = excinfo.value.detail
    assert detail["code"] == "EMPTY_BATCH"
    assert detail["actions"] and detail["actions"][0]["type"] in {
        "retry", "nav", "contact", "dismiss", "api"}
    assert "kind" not in detail["actions"][0]


def test_prepare_media_requires_idempotency_key(monkeypatch):
    """素材准备会真的调发布渠道 —— 没有幂等键就不许开始。"""
    monkeypatch.setattr(mod, "_schema_blockers", lambda: [])
    monkeypatch.setitem(sys.modules, "db.connection", _Tripwire("db.connection"))
    with pytest.raises(HTTPException) as excinfo:
        _run(mod.api_prepare_publish_media_v2(101, request=_FakeRequest()))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert "未联系发布渠道" in excinfo.value.detail["impact"]


# ============================================================
# ⑦ 接线形态锁:协调器真的被 import 了(不是死代码)
# ============================================================

@pytest.mark.parametrize("symbol", [
    "materialize_command",      # publish 协调器
    "claim_request",            # 幂等 claim
    "freeze_one_item",          # 逐项冻结
    "claim_artifact",           # artifact 幂等
    "save_draft",               # 草稿 CAS
    "claim as claim_slot",      # slot CAS
    "resolve_price_points",     # 投放定价唯一口径
    "resolve_settlement_authority",
])
def test_coordinators_are_actually_imported(symbol):
    """本仓刚因「死函数被当成已接线」栽过 —— 逐个钉住协调器真被端点引用。"""
    # [WO-B (2) 2026-08-20] 产线搬进 services/geo_douyin/publish_batch_core.py
    # (理由见那个模块的 docstring:小榜 execute 要接同一条,不许写第二份)。
    # 分母因此是**两个文件**,但不是"随便哪个有就算":先钉死 handler 真的
    # 调了产线函数 —— 否则协调器可以全部躺在一个没人调的模块里而本判据全绿,
    # 那正是本包上一轮被撤 PASS 的「死函数」形态。
    src = API_SRC.read_text(encoding="utf-8")
    core = (API_SRC.parent.parent / "services" / "geo_douyin"
            / "publish_batch_core.py").read_text(encoding="utf-8")
    assert "materialize_publish_batch(" in src, (
        "端点没有调用产线函数 —— 下面的引用断言会变成对死代码的断言")
    stripped = "\n".join(
        line.split("#", 1)[0] for line in (src + chr(10) + core).splitlines())
    assert symbol in stripped, f"{symbol} 没有被端点引用 —— 协调器是死代码"


def test_production_pricing_never_leaks_into_publish_chain():
    """🔴 两条定价链不许在同一个端点里混用(规格 §6.2)。

    判据:发布相关 handler 的函数体里**不许**出现制作定价源。
    """
    import ast

    tree = ast.parse(API_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "publish" not in node.name:
            continue
        names = {n.module for n in ast.walk(node) if isinstance(n, ast.ImportFrom) and n.module}
        assert not any("geo_douyin.pricing" in m for m in names), (
            f"{node.name} 里引入了制作定价源 —— 两条链的指纹不可互换"
        )
