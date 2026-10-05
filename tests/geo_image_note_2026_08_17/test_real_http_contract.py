"""返工 · 判据制度 #1:**真 HTTP 端到端**(TestClient → 真路由 → 真 Pydantic → 真闸)。

## 为什么整包 PASS 会被撤销,而这一套是解药

上一轮我交了 481 条判据全绿,却漏掉了**前后端合同缝合面**:
  · 服务层判据直接调 Python 函数,绕过 Pydantic;
  · Playwright 判据把后端整个 mock 掉,前端发什么都"成功";
  · 于是"前端发的 payload 能不能过后端模型"这件事**从来没有任何一条判据在看**。
真实情况是主 CTA 一发请求就 422。

本文件的规矩:**payload 一律取自前端真实发送的形状**,经 TestClient 打真路由。任何 mock
都不许盖在"前端 → 后端模型"这一层上 —— 那正是要测的那一层。

[WO_271 · 2026-09-23] 原来从 `imageNoteApi.ts` 抽 payload 形状的两个抽取函数
(`_ts_payload_fields` / `_frontend_create_batch_payload`)连同只给它们用的样值表已删:
调用它们的两格随 #150 §4 退役(见下方两段退役注),此后零调用;09-21 重建的
`frontend/src/lib/imageNoteApi.ts` 是发布专用的新形状,没有 `createBatch`。
发布提交体「每项恰好七个键、键名逐个对上后端契约」现由 build 链里的
`frontend/scripts/verify-image-note-publish.mjs` N1 守。

## 判据分两种,不要混

  · `test_frontend_payload_*`:前端形状必须被后端**接受**(不是 422)。
    这类判据现在会红,红的就是返工要修的东西。
  · `test_*_rejects_*`:畸形 payload 必须被**拒绝**。没有它,"接受"可能只是
    模型把什么都放行(extra='ignore' 就是这么骗人的)。
"""
from __future__ import annotations

import importlib
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 剥注释:判据只看真代码
# ---------------------------------------------------------------------------


def _python_code_only(path: pathlib.Path) -> str:
    """抹掉 `#` 注释与 docstring,**其余逐字保留**。

    🔴 第一版用 `tokenize` 把 token 重新拼接,结果 `hasattr(req, "x")` 被拆成
       一行一个 token —— 字面量不再连续,扫描永远命中不到。自证判据当场抓到:
       "注释里的要剥掉"过了,"真代码里的要留下"没过。
       **只写"该剥掉"的一半,剥离器可以把整个文件剥空还显得正确。**
    """
    import ast
    import io
    import tokenize

    lines = path.read_text(encoding="utf-8").splitlines()

    # ① docstring:按 AST 给出的行区间整行抹掉
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return "\n".join(lines)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for ln in range(first.lineno - 1, (first.end_lineno or first.lineno)):
                if 0 <= ln < len(lines):
                    lines[ln] = ""

    # ② `#` 注释:按 token 位置只截掉注释那一段,同一行的代码保留
    try:
        with io.open(path, encoding="utf-8") as fh:
            for tok in tokenize.generate_tokens(fh.readline):
                if tok.type != tokenize.COMMENT:
                    continue
                ln = tok.start[0] - 1
                if 0 <= ln < len(lines):
                    lines[ln] = lines[ln][:tok.start[1]]
    except tokenize.TokenError:
        pass
    return "\n".join(lines)


def test_code_only_stripper_discriminates():
    """剥离器的**双向**对照:注释里的写法要剥掉,真代码里的要留下。

    只写前一半的话,一个"把整个文件剥空"的实现也能全绿。
    """
    import tempfile

    needle = 'hasattr(req, "request_id")'
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "probe.py"
        p.write_text(f'# {needle}\nx = 1\n', encoding="utf-8")
        assert needle not in _python_code_only(p), "注释没被剥掉"
        p.write_text(f'"""doc {needle}"""\nx = 1\n', encoding="utf-8")
        assert needle not in _python_code_only(p), "docstring 没被剥掉"
        p.write_text(f'y = {needle}\n', encoding="utf-8")
        assert needle in _python_code_only(p), "剥过头了 —— 真代码也被吃掉"
        p.write_text(f'y = {needle}  # 尾注释\n', encoding="utf-8")
        assert needle in _python_code_only(p), "行尾注释把同行代码一起剥了"


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_frontend_api_module_is_the_source_of_truth：抽取器的事实源 imageNoteApi.ts 随 #150 §4 删除
#   (09-21 同名文件以发布专用的新形状重建,旧函数没有回来;抽取器本身已于 WO_271 删除)



# ---------------------------------------------------------------------------
# 合同对表:前端字段 vs 后端 Pydantic 模型字段
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def api_module():
    return importlib.import_module("api.geo_image_note_api")


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_create_batch_item_field_names_match_frontend：createBatch 契约已撤；新契约由 production_quote 与 studio 判据钉



def test_create_batch_accepts_the_idempotency_key(api_module):
    """🔴 比 422 更糟的一种:字段被**静默丢弃**。

    `CreateBatchRequest` 没有 `request_id` 字段,而 Pydantic v2 默认
    `extra='ignore'` —— 前端发的幂等键**一声不响地消失**,
    服务端每次自己生成一个新 uuid,于是双击创建两个批次。
    模块层的 20 并发幂等判据是真的,但从 UI 根本走不到。
    """
    fields = set(api_module.CreateBatchRequest.model_fields)
    assert "request_id" in fields, (
        "CreateBatchRequest 没有 request_id —— 前端的幂等键被静默丢弃。"
        "『同一 request_id 只创建一个批次』这条保证从 UI 不可达。")


def test_no_always_false_hasattr_branch_on_pydantic_model():
    """🔴 `req.request_id if hasattr(req, "request_id") else batch_id`:

    模型上没有这个字段时 `hasattr` **恒假**,于是永远走 else ——
    一个看起来处理了两种情况、实际只有一条活路的分支。
    这类"永假条件"比缺失更难查:代码读起来像是考虑过了。
    """
    # 🔴 **第 6 次**踩「判据命中自己写的注释」:修复时我在代码里留了一句
    #    "原为 `... hasattr(req, "request_id") ...`" 的说明,扫描器当场把它算成违规。
    #    教训不是"注释别提旧写法"(那会让代码失去历史),
    #    而是**扫描类判据默认先剥注释**——这条我写过五遍,仍然是每次新写扫描器时重犯。
    src = _python_code_only(REPO / "api" / "geo_image_note_api.py")
    assert 'hasattr(req, "request_id")' not in src, (
        "存在 hasattr 永假分支;请让模型真的带上该字段,并直接使用它")


def test_publish_preview_post_revision_id_type_matches_frontend(api_module):
    """🔴 P0-13:前端 `post_revision_id` 是 number,后端声明成 `str`。

    Pydantic v2 默认**不**把 int 强转成 str,于是真 HTTP 422。
    (本包自己在协调器轮刚因为反方向的类型错配栽过一次 ——
     那次是 str 进 BIGINT 列;两次是同一个病:契约两端各写各的。)
    """
    field = api_module.PublishPreviewItem.model_fields["post_revision_id"]
    assert field.annotation is int, (
        f"post_revision_id 声明成 {field.annotation},而前端发的是 number;"
        "真 HTTP 会 422")


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_publish_batch_item_field_names_match_frontend：publishBatch 契约已撤，同上



def test_publish_preview_accepts_brand_and_quote_scope(api_module):
    """前端发 `brand_id` / `quote_id`(发布必须 quote-scoped),后端模型要收得下。

    收不下 = 作用域信息在合同缝合面上丢失,服务端只能靠猜 —— 而"猜作用域"
    正是 WP7 花整轮修掉的那类缺陷。
    """
    fields = set(api_module.PublishPreviewRequest.model_fields)
    for name in ("brand_id", "quote_id"):
        assert name in fields, f"PublishPreviewRequest 缺 {name},发布作用域在合同层丢失"


# ---------------------------------------------------------------------------
# 真 HTTP:主 CTA 全链不许 422
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """真路由 + 真 Pydantic 的 TestClient。

    🔴 **第一版是假绿**:直接打 `server.app`,请求被全局鉴权中间件在 Pydantic
       **之前**挡成 401,于是「不许 422」三条全部轻松通过 —— 探针根本没走到
       被测的那一层。配对判据(畸形 payload **必须** 422)当场把这个洞暴露出来:
       它也拿到 401。没有那条配对,我会带着三条假绿交上去。

    🔴 现在的做法:挂真 router 到一个干净 app,只用一个假鉴权中间件把
       `request.state.user` 填上。**唯一被 mock 的缝合面就是全局鉴权中间件**,
       它由既有的 `_guard` / demo / schema 三闸判据覆盖(见交付单 mock 缝合面清单)。
       路由、Pydantic 模型、handler 全是真的 —— 那才是这套要测的东西。
    """
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI, Request as FastAPIRequest

    from api.geo_image_note_api import router as image_note_router

    app = FastAPI()
    app.include_router(image_note_router)

    @app.middleware("http")
    async def _fake_auth(request: FastAPIRequest, call_next):
        request.state.user = {
            "user_id": 42, "id": 42, "username": "contract_probe",
            "is_admin": False, "client_brand_ids": [101],
            "permissions": ["writing:read", "writing:write"],
        }
        return await call_next(request)

    return fastapi_testclient.TestClient(app, raise_server_exceptions=False)


def test_the_client_really_reaches_pydantic(client):
    """🔴 判别力自证:证明这个 client **走得到** Pydantic 那一层。

    没有这一条,"不许 422" 可能只是因为请求在更早的地方就被挡住了 ——
    那正是第一版的病。畸形 payload 必须真的拿到 422。
    """
    resp = client.post("/api/geo-douyin/batches", json={"quote_id": "not-an-int"})
    assert resp.status_code == 422, (
        f"畸形 payload 没拿到 422 而是 {resp.status_code} —— "
        "说明请求没走到 Pydantic,后面所有『不许 422』的判据都是假绿")


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_create_batch_does_not_422_on_the_real_frontend_payload：payload 源头 imageNoteApi.ts 已删，无真实形状可取
#   (现在的整批下单体由 pages/Writing/imageNoteProduction.productionBatch 拼,不在本文件的抽取面里)



def test_publish_preview_does_not_422_on_the_real_frontend_payload(client):
    resp = client.post("/api/meijiehezi/image-notes/publish-preview", json={
        "brand_id": 101,
        "quote_id": 1,
        "items": [{
            # uuid:`mhz_publish_order_items.item_request_id` 是 uuid 列
            "item_request_id": "44444444-4444-4444-8444-444444444444",
            "geo_post_id": 77,
            "post_revision_id": 3,          # 前端发 number
            "prepared_artifact_id": "art-1",
            "manifest_hash": "h" * 64,
            "media_id": 501,
        }],
    })
    assert resp.status_code != 422, (
        "发布预览合同缝合面断裂:\n"
        + json.dumps(resp.json(), ensure_ascii=False, indent=2)[:1200])


def test_publish_batch_does_not_422_on_the_real_frontend_payload(client):
    resp = client.post("/api/meijiehezi/image-notes/publish-batch", json={
        "request_id": "pubcmd-probe-1",
        "expected_total_price_points": 28080,
        "items": [{
            "item_request_id": "44444444-4444-4444-8444-444444444444",
            "geo_post_id": 77,
            "post_revision_id": 3,
            "prepared_artifact_id": "art-1",
            "manifest_hash": "h" * 64,
            "media_id": 501,
            "expected_price_fingerprint": "publish-price-v1:bbb",
        }],
    })
    assert resp.status_code != 422, (
        "发布提交合同缝合面断裂:\n"
        + json.dumps(resp.json(), ensure_ascii=False, indent=2)[:1200])


# ---------------------------------------------------------------------------
# 批次读回:不许是空壳
# ---------------------------------------------------------------------------

def test_batch_get_is_not_an_unconditional_404_shell():
    """🔴 P0-04:`api_get_batch` 无条件 `raise HTTPException(404)`。

    前端提交后就靠它轮询进度;它恒 404,意味着**任何批次都查不到状态**。
    这条打源码形态而不是打行为,因为"恒 404"在行为上与"这个批次不存在"
    长得一模一样 —— 只有看实现才分得出。
    """
    import ast

    src = (REPO / "api" / "geo_image_note_api.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_get_batch")
    body = ast.unparse(fn)
    assert "批次不存在" not in body or "SELECT" in body, (
        "batch GET 是无条件 404 空壳 —— 它必须真的去查批次与逐项状态")


# ---------------------------------------------------------------------------
# Owner A4:资格与频控不许硬编码
# ---------------------------------------------------------------------------

def test_daily_limit_is_not_hardcoded_to_one():
    """🔴 P0-12:`IMAGE_NOTE_DAILY_LIMIT = 1` 违 Owner A4 裁决。

    A4 定的是走**真实资格 + today_remaining**;写死 1 等于把一个业务参数
    钉在代码里,而且钉成了最保守的那个值 —— 账号一天只能发一篇。
    """
    src = (REPO / "api" / "geo_image_note_api.py").read_text(encoding="utf-8")
    assert not re.search(r"^IMAGE_NOTE_DAILY_LIMIT\s*=\s*1\s*$", src, re.M), (
        "日限额硬编码为 1;应走真实资格目录的 today_remaining")
