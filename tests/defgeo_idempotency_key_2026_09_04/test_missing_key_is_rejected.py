"""#31 · `Idempotency-Key` 缺失必须被拒 —— 把现有行为**钉住**
(工单 post-train #31)。

## 🔴 工单的坐标与修法都要订正

工单原文:「`defensive_geo_api.py:622-623` Idempotency-Key **签名可选、端点体必需**,
修法:签名改必需或文档钉住 + 配一条『不给 key 必须被拒』判据」。

**坐标错**:`:622-623` 是响应构造(`QuestionPlanResponse(...)`),与 Idempotency 无关。
真正的三处(AST 取,不靠行号窗口):

| 端点 | 行 | 签名 | 体内引用次数 | 缺 key 时 |
|---|---|---|---|---|
| `create_question_plan_preview` | 528 | `Optional[...] = Header(default=None)` | **0** | 照常成功 |
| `create_run_preview`           | 704 | 同上 | 3 | `_safe_error("VALIDATION_FAILED")` ⇒ 422 |
| `confirm_run_preview`          | 1483 | 同上 | 3 | 同上 |

**修法方向也要订正**:「签名改必需」**不会**让错误信封变好 ——
FastAPI 的 `RequestValidationError` 会被 `TypedErrorRoute` 转成**同一个** `VALIDATION_FAILED`
(实测 422 / retryable=False)。所以那不是修复,只是换一条到达同一处的路,
却把「拒绝理由」从代码里显式的一行搬进框架的隐式行为里。

⇒ **这一单真正缺的东西是判据,不是改动**:后两个端点的拒绝**没有任何锁**,
谁把 `:711` / `:1509` 那两行删掉,今天没有一条判据会红。本文件补上。

## `:528` 那个参数是死的 —— 单独报,不在本单动

`create_question_plan_preview` 声明了 `Idempotency-Key` 却**一次都不用**
(AST:体内引用 0 次)。它的幂等实际由请求体的 `clientRequestId` + 内容哈希实现。
⇒ **OpenAPI 里对外宣告了一个不起作用的头**。删它是**对外 schema 变更**,
应由 Review/Owner 裁,不在本单顺手改;本文件只把「它今天确实没用」钉成判据,
将来谁给它接上线,这条会红,提醒同步文档与本注释。

## 判据为什么不需要真库

三个端点里 `_tenant(request)`(只读 `request.state.user`)与缺 key 检查都排在
任何 DB 访问**之前**,所以注入一个假 user 就能打到那一行,不必起 PG。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.defensive_geo_api import router

ROOT = Path(__file__).resolve().parents[2]
API_FILE = ROOT / "api" / "defensive_geo_api.py"

#: 缺 key 必须被拒的端点。**机械枚举**:凡是体内引用 idempotency_key 的都在这里,
#: 由 `test_denominator_is_mechanically_derived` 反查,漏一个会红。
#: 🔴 请求体必须**合法**(用模型的 alias,camelCase)。
#:    第一版我用了 snake_case ⇒ 体校验先炸 ⇒ 框架的 RequestValidationError 也被
#:    TypedErrorRoute 转成**同一个** 422 VALIDATION_FAILED ——
#:    与"缺 key"的拒绝**读数完全同形**,主判据零区分力。
#:    是下面那条反臂把它抓出来的(带了 key 仍 422)。
MUST_REJECT = [
    ("post", "/api/defensive-geo/run-previews",
     {"questionPlanId": "plan_probe", "questionPlanRevision": 1,
      "profileRevisionId": "brand-1", "platformKeys": ["deepseek"]}),
    ("post", "/api/defensive-geo/run-previews/prev_x/confirm",
     {"expectedHash": "h" * 64}),
]


def _client() -> TestClient:
    """挂真 router,注入一个假 user —— 缺 key 检查排在任何 DB 访问之前。"""
    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request, call_next):
        request.state.user = {"user_id": 1}
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _envelope_code(resp) -> str | None:
    try:
        d = resp.json().get("detail")
    except Exception:
        return None
    return d.get("code") if isinstance(d, dict) else None


def _functions_using_key() -> dict[str, int]:
    """AST:每个声明了 `idempotency_key` 的 handler,体内引用了它几次。"""
    tree = ast.parse(io.open(API_FILE, encoding="utf-8").read())
    out: dict[str, int] = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = [a.arg for a in n.args.args] + [a.arg for a in n.args.kwonlyargs]
        if "idempotency_key" not in names:
            continue
        out[n.name] = sum(1 for x in ast.walk(n)
                          if isinstance(x, ast.Name) and x.id == "idempotency_key")
    return out


# ── 分母自证 ──────────────────────────────────────────────────────
def test_denominator_is_mechanically_derived():
    """声明了这个头的 handler 恰好三个,且用/不用的分布与本文件的假设一致。

    **红了说明什么**:有人新加了一个收 `Idempotency-Key` 的端点(或改了现有的),
    而 `MUST_REJECT` 是手写的 ⇒ **手写分母漏掉的那一项不会让任何判据变红**,
    所以由这条来反查。红了就回来把新端点补进 `MUST_REJECT`,不要直接放宽本条。
    """
    used = _functions_using_key()
    assert set(used) == {"create_question_plan_preview", "create_run_preview",
                         "confirm_run_preview"}, f"声明该头的 handler 变了:{sorted(used)}"
    assert used["create_run_preview"] > 0 and used["confirm_run_preview"] > 0
    assert used["create_question_plan_preview"] == 0, (
        "`create_question_plan_preview` 开始使用 idempotency_key 了 —— "
        "那它就该进 MUST_REJECT,并同步更新本文件顶部那张表与 OpenAPI 文档说明")


# ── 主判据:缺 key 必须被拒 ────────────────────────────────────────
@pytest.mark.parametrize("method,path,body", MUST_REJECT,
                         ids=[p for _, p, _ in MUST_REJECT])
def test_missing_idempotency_key_is_rejected(method, path, body):
    """不带 `Idempotency-Key` ⇒ 422 + VALIDATION_FAILED。

    **红了说明什么**:那一行显式检查(`:711` / `:1509`)被删了或被挪到 DB 访问之后 ——
    前者意味着重复提交会真的重复执行,后者意味着拒绝之前已经打了库。
    """
    r = getattr(_client(), method)(path, json=body)
    assert r.status_code == 422, f"{path} 缺 key 却返 {r.status_code}"
    assert _envelope_code(r) == "VALIDATION_FAILED", (
        f"{path} 缺 key 的信封 code = {_envelope_code(r)}")


@pytest.mark.parametrize("method,path,body", MUST_REJECT,
                         ids=[p for _, p, _ in MUST_REJECT])
def test_present_key_gets_past_the_missing_key_gate(method, path, body):
    """反臂:带上 key 之后**不再**落在缺 key 那条拒绝上。

    **红了说明什么**:上面那条 422 不是"缺 key"造成的,而是这两个端点**对什么请求都** 422
    ⇒ 主判据零区分力。这条只断言"结果不同",**不钉具体码** ——
    带了 key 之后会走到取计划/打库,那一步的失败与本单无关,钉死它会引入与本改动无关的红。
    """
    r = getattr(_client(), method)(path, json=body,
                                   headers={"Idempotency-Key": "c14-31-probe"})
    assert _envelope_code(r) != "VALIDATION_FAILED" or r.status_code != 422, (
        f"{path} 带了 key 仍落在缺 key 的拒绝上(code={_envelope_code(r)} "
        f"status={r.status_code})⇒ 主判据无法区分")


def test_question_plan_preview_does_not_require_the_header_today():
    """把 `:528` 今天的**实际**行为钉住:它不因缺 key 而拒。

    **红了说明什么**:有人给 preview 也加了缺 key 拒绝 —— 那是行为变更,
    要同步 §15.3 文档与前端(前端目前只在 body 里发 `clientRequestId`,不发这个头)。
    🔴 这条**不是**在说"现状是对的";它是在说"现状是这样,改了要有人知道"。
    """
    r = _client().post("/api/defensive-geo/question-plans/preview", json={})
    assert not (r.status_code == 422 and _envelope_code(r) == "VALIDATION_FAILED"
                and "Idempotency" in r.text), (
        "preview 端点开始因缺 Idempotency-Key 而拒 —— 行为变更,需同步文档与前端")


# ══════════════════════════════════════════════════════════════════════
# #58 · 「缺 key」与「体不合法」必须可区分
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 改之前两者给客户端的是**一模一样**的信封:422 + `validation_failed` 那句
#    「这次提交的内容有一处填得不对,返回改一下就能继续」。
#    她照那句去检查自己填的内容 —— 而这个标识是**前端自动带的**,根本不是她填的。
#    一句具体但**指错方向**的提示,比笼统的提示更糟:它让人停止寻找。
#
# 🔴 可程序化区分的是哪一格:`reason_key` **不进 payload**
#    (`_safe_error` 只放 code / retryable / publicExplanation / nextAction / details)。
#    进 payload 的是 `nextAction.kind` 与 `actionRef`,而后者由 (kind, target)
#    **确定性**导出 ⇒ 判据钉这两个,不钉句子(钉句子 = 把判据钉在文案上)。

def _envelope(resp) -> dict:
    d = resp.json().get("detail")
    return d if isinstance(d, dict) else {}


@pytest.mark.parametrize("method,path,body", MUST_REJECT,
                         ids=[p for _, p, _ in MUST_REJECT])
def test_missing_key_has_its_own_action(method, path, body):
    """缺 key ⇒ `nextAction.kind == refresh_and_retry`,不是默认的 fix_input。

    **红了说明什么**:两处拒绝又退回默认信封 ⇒ 客户端无法区分
    「你忘了带标识」与「你填错了内容」,而这两件事的**处置完全相反**。
    """
    env = _envelope(getattr(_client(), method)(path, json=body))
    assert env.get("code") == "VALIDATION_FAILED"
    assert env.get("nextAction", {}).get("kind") == "refresh_and_retry", (
        f"{path} 缺 key 的 nextAction = {env.get('nextAction')}")


def test_missing_key_and_bad_body_are_machine_distinguishable():
    """两种拒绝在 **payload 层**必须不同 —— 不靠读中文句子。

    **红了说明什么**:客户端只能按文案分支才能区分二者,
    而本仓明令前端不许按文案分支(§15.8:前端只渲染服务端给的话)。
    """
    c = _client()
    miss = _envelope(c.post("/api/defensive-geo/run-previews",
                            json=MUST_REJECT[0][2]))                      # 无 key,体合法
    bad = _envelope(c.post("/api/defensive-geo/run-previews",
                           json={"nope": 1},
                           headers={"Idempotency-Key": "c14-58-probe"}))  # 有 key,体不合法
    assert miss.get("code") == bad.get("code") == "VALIDATION_FAILED", (
        f"前提变了:两者的 code 不再都是 VALIDATION_FAILED({miss.get('code')} / {bad.get('code')})")
    a_miss = miss.get("nextAction", {})
    a_bad = bad.get("nextAction", {})
    assert a_miss.get("actionRef") != a_bad.get("actionRef"), (
        f"两种拒绝的 actionRef 相同({a_miss.get('actionRef')})⇒ 机器分不开")
    assert miss.get("publicExplanation") != bad.get("publicExplanation"), (
        "两种拒绝给她的是同一句话 ⇒ 人也分不开")


def test_bad_body_still_uses_the_default_envelope():
    """反臂:体不合法这条路**一个字不变**(证明我没有把所有 422 都改掉)。

    **红了说明什么**:我为了区分缺 key,把框架的体校验出口也改了 ——
    那会波及所有端点的所有校验失败,远超本单范围。
    """
    env = _envelope(_client().post("/api/defensive-geo/run-previews", json={"nope": 1},
                                   headers={"Idempotency-Key": "c14-58-probe"}))
    assert env.get("nextAction", {}).get("kind") != "refresh_and_retry"


def test_new_copy_entries_are_registered_and_renderable():
    """两条新文案必须真的在 registry 里取得到。

    **红了说明什么**:`_safe_error` 的构造期 fail-closed 会先炸,
    但那只在**跑到那条 raise 时**才炸;这条在任何时候都能查,
    且它顺带钉住「动作 label 非空」——没有 label 的动作是死动作。
    """
    from services.defensive_geo.copy_registry import try_user_label

    assert try_user_label("reason", "idempotency_key_missing"), "reason 文案没登记"
    assert try_user_label("action", "refresh_and_retry"), "action label 没登记"
