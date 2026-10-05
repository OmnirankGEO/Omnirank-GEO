"""判据 · 非 UUID 的 `X-Demo-Case-ID` 不许打成 500(窗 1 微单 · 2026-08-20 · 存量)。

## 病史(实测,不是推测)

`admin_demo_case_grants.case_id` 是 **uuid 列**。`services/demo_access.py` 的
`DemoSafeResponseMiddleware` 把请求头 `X-Demo-Case-ID`(**攻击者可控**)原样送进
`resolve_demo_case_access` → `g.case_id=%s`。修之前实测:

```
非 UUID   → psycopg2 InvalidTextRepresentation: invalid input syntax for type uuid: "not-a-uuid"
空串      → 同上
' OR 1=1 --  → 同上
HTTP 非 UUID      → 500
HTTP 合法但不存在  → 404
HTTP 合法且存在    → 200
```

500 不只是可用性问题:它把「这个 case 不存在」(404)与「你这串格式不对」(500)
变成**两种可区分的回答** = 一个免费的探测 oracle。所以修法是让格式不对的走**同一个 404**,
不是换一个更礼貌的错误码。

## 判据形状

正面:各种畸形 case_id ⇒ **恰好 404**(且断言不是 5xx —— 只写 `!= 500` 会漏掉 502/503 之类)。
反向对照两条(缺任何一条,「一律 404」也能让正面全绿):
  · 合法但不存在 ⇒ 404 照旧;
  · 合法且存在 ⇒ 200 + `X-Demo-Mode: demo`,走原逻辑。
"""

from __future__ import annotations

import psycopg2
import pytest
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.testclient import TestClient

from tests.admin_user_governance.test_cross_tenant_governance import grant_zhejiang_dailin
from services.demo_access import DemoSafeResponseMiddleware, resolve_demo_case_access

#: 畸形 case_id 家族。**硬编码在判据里**,不从被测模块取 —— 与被测逻辑共用一份真值
#: 就会两边一起错还一起绿。
MALFORMED_CASE_IDS: tuple[tuple[str, str], ...] = (
    ("plain_word", "not-a-uuid"),
    ("empty", ""),
    ("whitespace", "   "),
    ("sql_ish", "' OR 1=1 --"),
    ("truncated_uuid", "56cc6f1e-bd50-584f-8702"),
    ("overlong", "5" * 200),
    ("non_ascii", "演示案例"),
    ("null_byte", "56cc6f1e-bd50-584f-8702-2e044d20bce5\x00"),
)

#: HTTP 层能真正送出去的那部分。
#: 🔴 CJK 之类的非 ASCII **不在**这一组:HTTP 头值按规范是 ASCII/latin-1,
#:    httpx 在客户端就拒发,请求根本到不了服务端 —— 放进来测的是 httpx,不是我们。
#:    它仍留在上面的函数级家族里:`resolve_demo_case_access` 的调用方不止 HTTP 头这一条。
MALFORMED_HEADER_VALUES: tuple[tuple[str, str], ...] = tuple(
    (name, value) for name, value in MALFORMED_CASE_IDS
    if all(ord(ch) < 128 for ch in value)
)

VALID_ABSENT = "00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def demo_app():
    """真中间件 + 一个普通只读 handler。`raise_server_exceptions=False` 才能看到 500,
    否则 TestClient 会把异常直接抛给判据,断言的就不是用户拿到的那个码了。"""
    app = FastAPI()

    @app.get("/api/clients/601")
    async def safe_read(brand_id: int):
        return {"brand_id": brand_id, "name": "浙江岱林", "industry": "生命科学"}

    app.add_middleware(DemoSafeResponseMiddleware)

    @app.middleware("http")
    async def fake_auth(request: FastAPIRequest, call_next):
        request.state.user = {
            "user_id": 132, "id": 132, "username": "demo_132",
            "is_admin": False, "client_brand_ids": [],
        }
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def live_case_id():
    """**函数级**,不是模块级。

    🔴 本包 conftest 有一条 autouse 的**函数级** `reset_governance_data`,
    每条用例前 `TRUNCATE … RESTART IDENTITY CASCADE` 再重新 seed。
    模块级夹具在它之前跑,建出来的授权**当场被下一条用例的 TRUNCATE 抹掉**,
    而且它自己看到的还是上一条用例留下的残局(实测:整包顺跑时
    `brands.601` 已被更早的用例软删/删行,授权建不出来,报 CrossTenantNotFound)。
    函数级则排在 autouse 之后,每次拿到的都是干净 seed。
    """
    return grant_zhejiang_dailin(request_id="case-id-hardening-grant")["case_id"]


def _get(client, case_id: str, request_id: str):
    return client.get(
        "/api/clients/601?brand_id=601",
        headers={"X-Demo-Brand-ID": "601", "X-Demo-Case-ID": case_id,
                 "X-Request-ID": request_id},
    )


@pytest.mark.parametrize("name,case_id", MALFORMED_HEADER_VALUES,
                         ids=[n for n, _ in MALFORMED_HEADER_VALUES])
def test_malformed_case_id_is_404_never_500(demo_app, live_case_id, name, case_id):
    response = _get(demo_app, case_id, f"malformed-{name}")
    assert response.status_code < 500, (
        f"{name}: 畸形 case_id 打出 {response.status_code} —— 服务端异常穿透了")
    assert response.status_code == 404, (name, response.status_code, response.text[:200])


@pytest.mark.parametrize("name,case_id", MALFORMED_CASE_IDS, ids=[n for n, _ in MALFORMED_CASE_IDS])
def test_resolver_returns_none_instead_of_raising(name, case_id):
    """函数级:合同是「拿得到 context 或 None」,畸形输入不许抛给调用方。"""
    try:
        assert resolve_demo_case_access(132, case_id) is None, name
    except psycopg2.Error as exc:                                   # pragma: no cover
        pytest.fail(f"{name}: 畸形 case_id 让驱动抛了 {type(exc).__name__}")


def test_the_http_sample_set_did_not_collapse():
    """分母自证:HTTP 那组不许被过滤到只剩一两条 —— 空分母的参数化是「空即通过」。"""
    assert len(MALFORMED_HEADER_VALUES) >= 6, MALFORMED_HEADER_VALUES
    assert len(MALFORMED_CASE_IDS) > len(MALFORMED_HEADER_VALUES), "非 ASCII 那条被漏掉了"


def test_valid_but_absent_case_id_still_404(demo_app, live_case_id):
    """反向对照 1:合法 UUID 但没有对应授权 ⇒ 404 照旧(与畸形**同一种回答**,不给 oracle)。"""
    response = _get(demo_app, VALID_ABSENT, "valid-absent")
    assert response.status_code == 404, response.text[:200]
    assert resolve_demo_case_access(132, VALID_ABSENT) is None


def test_valid_and_existing_case_id_goes_through_the_original_path(demo_app, live_case_id):
    """反向对照 2:合法且存在 ⇒ 200 走原逻辑。

    没有这条,上面所有 404 断言都可能只是「这条路整个坏了」。
    """
    response = _get(demo_app, live_case_id, "valid-present")
    assert response.status_code == 200, response.text[:200]
    assert response.headers["X-Demo-Mode"] == "demo"
    context = resolve_demo_case_access(132, live_case_id)
    assert context is not None and context.case_id == live_case_id


def test_uuid_spelling_variants_of_a_real_case_still_resolve(live_case_id):
    """归一化不放宽授权:同一个 UUID 的等价写法(大写 / 无短横 / 花括号)
    仍然解析到**同一个** case;它们本来就是 PostgreSQL uuid 输入认的形态。"""
    compact = live_case_id.replace("-", "")
    for variant in (live_case_id.upper(), compact, "{" + live_case_id + "}"):
        context = resolve_demo_case_access(132, variant)
        assert context is not None and context.case_id == live_case_id, variant
