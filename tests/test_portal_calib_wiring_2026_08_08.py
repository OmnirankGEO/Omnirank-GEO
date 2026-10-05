"""门户校准「接线」判别锁 · 返工单 WO_PORTAL_CALIB_AND_REBUILD_DEADLOCK_2026-08-08 §5.A ①。

🔴🔴 **这一份存在的唯一理由:上一轮 20 条门禁全绿,bug 照样上线。**

上一轮所有锁喂的都是**手工构造的 `request.state.user`** —— 于是它们验的是
`_viewer_can_calibrate` 这个函数,而生产上这条路由 **`request.state.user` 从来不存在**
(`auth/middleware.py:116` 对 `/api/public/` 前缀提前放行,不解析 token、不填 state)。
函数是对的,接线是断的,锁一条都没打在接线上。

所以本文件的每一条后端锁都走**真的 ASGI 请求**(TestClient + 真中间件 + 真库),
**禁止**出现手工构造的 `request.state.user`。文件末尾有一条元判据机械地钉死这一点。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")

DIAG_ID = 77001
BRAND_ID = 77001
OWNER_UID = 112          # 归属服务商
STRANGER_UID = 114       # 另一个登录用户(非归属)
SHARE_TOKEN = "wiretoken77001"

pytestmark = pytest.mark.skipif(not PG_URL, reason="real PostgreSQL URL is required")


# ══════════════════════════════════════════════════════════════════════
# 夹具:真库 + 真中间件 + 真路由
# ══════════════════════════════════════════════════════════════════════

_MINIMAL_SCHEMA = """
CREATE TABLE diagnosis_records (
    id INTEGER PRIMARY KEY,
    brand_id INTEGER,
    brand_name TEXT,
    industry TEXT,
    total_score INTEGER,
    level TEXT,
    web_search_score REAL, platform_score REAL, content_quality_score REAL,
    authority_score REAL, brand_ownership_score REAL, ai_visibility_score REAL,
    ai_citation_score REAL, update_frequency_score REAL,
    report_md_path TEXT,
    keywords TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    report_v2_version TEXT,
    report_v2_client_md TEXT,
    report_v2_modules_jsonb JSONB,
    report_v2_generated_at TIMESTAMPTZ,
    data_completeness_score INTEGER,
    data_completeness_breakdown JSONB,
    result_visibility TEXT,
    share_token TEXT
);
-- [R5 ⑤ 批2] brands: 见下方 ensure_brands_schema()（生产 SSOT 出口），不在这里手搓。
CREATE TABLE short_links (
    id BIGSERIAL PRIMARY KEY, code TEXT, target_url TEXT
);
"""


def _build_modules_jsonb() -> dict:
    """真的 `3_raw` 附录 —— 用生产装配函数产出,不手搓结构。

    16 格「身份待确认」+ 4 格正常,与既有 `_presentation` 夹具同源。
    """
    import services.report_writer_v2 as rw

    detail = []
    for index in range(16):
        detail.append({"question": f"贵阳小龙虾哪家好？{index}", "results": {"dashscope": {
            "brand_verdict": "UNKNOWN", "brand_detected": False, "status": "error",
            "detection_reason": "invalid_matched_text",
            "detection_method": "deepseek_v4_flash_structured",
            "full_response": "贵阳夜宵推荐：阿强龙虾、大嘴龙虾。",
            "mentioned_brands": ["阿强龙虾", "大嘴龙虾"]}}})
    for index in range(4):
        detail.append({"question": f"贵阳龙虾馆推荐{index}", "results": {"dashscope": {
            "brand_verdict": "NO", "brand_detected": False, "status": "success",
            "detection_reason": "no_identity_candidate", "detection_method": "deterministic",
            "full_response": "推荐大嘴龙虾。", "mentioned_brands": ["大嘴龙虾"]}}})
    raw_module = rw.build_module_3_raw_ai_appendix({"diagnosis_data": {"ai_visibility_data": {
        "test_questions": [d["question"] for d in detail],
        "engines_tested": ["dashscope"], "detail_table": detail}}})
    return {
        "client": {
            "modules": {
                # is_client_report_ready 的契约:client.modules["1"] 里要有正文
                "1": {"insight": "本次采样里品牌可见度偏低。"},
                "3_raw": raw_module,
            }
        },
        "funnel": {"total_score": 20, "level": "隐形型"},
    }


@pytest.fixture(scope="module")
def wired():
    """真库 + 真中间件 + 真路由。返回 (client, make_token)。"""
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    database_name = f"calibwire_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    admin.close()

    parsed = urlsplit(PG_URL)
    target_url = urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
    )
    os.environ["DATABASE_URL"] = target_url
    # 公开报告端点默认要求 st —— 保持严格模式(线上就是严格的)
    os.environ["PUBLIC_REPORT_REQUIRE_TOKEN"] = "true"

    from db import connection as connection_db

    connection_db.DATABASE_URL = target_url
    connection_db._pool = None

    boot = psycopg2.connect(target_url, cursor_factory=RealDictCursor)
    boot.autocommit = True
    with boot.cursor() as cur:
        # [R5 ⑤ 批2 2026-08-21] brands 走生产 SSOT 出口（手搓版比生产窄）。
        #   只换 brands 一张表的 DDL 来源，本文件其余业务表一概不动 —— 批 1 实测证伪过
        #   「夹具改跑整个 init_db」：把 150 张表拖进只要十来张表的夹具，
        #   174 passed/0 failed 变 138 passed/34 failed、194s 变 589s。
        #   手搓版 3 列，生产 32 列。
        ensure_brands_schema(cur)
        cur.execute(_MINIMAL_SCHEMA)
        cur.execute(
            "INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s)",
            (BRAND_ID, "阿强小龙虾", OWNER_UID),
        )
        cur.execute(
            """
            INSERT INTO diagnosis_records
                (id, brand_id, brand_name, industry, total_score, level, keywords,
                 report_v2_version, report_v2_client_md, report_v2_modules_jsonb,
                 report_v2_generated_at, result_visibility, share_token)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'v2',%s,%s,NOW(),'published',%s)
            """,
            (DIAG_ID, BRAND_ID, "阿强小龙虾", "餐饮", 20, "隐形型", "贵阳小龙虾",
             "# 客户版报告", json.dumps(_build_modules_jsonb(), ensure_ascii=False),
             SHARE_TOKEN),
        )
    boot.close()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.share_api as share_api
    from auth.middleware import setup_auth_middleware

    app = FastAPI()
    # 🔴 真中间件必须在栈上 —— 本 bug 就是中间件对 /api/public/ 提前放行造成的。
    #    绕过它的测试等于把病因从测试环境里删掉,再绿也不算数。
    setup_auth_middleware(app)
    app.include_router(share_api.router)

    def make_token(user_id: int, *, is_admin: bool = False, expires_in: int = 3600) -> str:
        """用**生产那把签名函数**造 token,不自己实现签名。"""
        from auth import jwt_utils

        now = int(time.time())
        payload = {
            "user_id": user_id, "username": f"u{user_id}", "display_name": f"u{user_id}",
            "is_admin": is_admin, "roles": [], "permissions": [],
            "perm_version": 1, "iat": now, "exp": now + expires_in,
        }
        header = jwt_utils._base64url_encode(
            json.dumps({"alg": jwt_utils.JWT_ALGORITHM, "typ": "JWT"},
                       separators=(",", ":")).encode("utf-8"))
        body = jwt_utils._base64url_encode(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        return f"{header}.{body}.{jwt_utils._sign(f'{header}.{body}')}"

    with TestClient(app) as client:
        yield client, make_token

    connection_db._pool = None
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database_name))
        )
    admin.close()


def _get(client, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(f"/api/public/report/{DIAG_ID}?st={SHARE_TOKEN}", headers=headers)


def _calibration(response):
    body = response.json()
    return ((body.get("report") or {}).get("presentation") or {}).get("calibration")


# ══════════════════════════════════════════════════════════════════════
# ①-1 / ①-2 三态 —— 全部走真 ASGI 请求
# ══════════════════════════════════════════════════════════════════════

def test_middleware_is_really_in_the_stack(wired):
    """🔴【判别力前提】证明真中间件确实在栈上。

    否则"匿名看不到 calibration"这条可能只是因为**中间件根本没装** ——
    那三态断言全部退化成恒真。这里拿一条**非公开**路由当对照:
    没有 token 必须被中间件挡成 401。
    """
    client, _ = wired
    unprotected = client.get("/api/share/qrcode?url=https://example.com")
    assert unprotected.status_code == 401, (
        "非公开路由无 token 竟然没被挡 → 中间件没生效,本文件其余断言全部失去判别力"
    )


def test_anonymous_gets_200_and_no_calibration(wired):
    """匿名(客户拿到的那条链接)—— 200,且响应体里**没有** calibration 段。"""
    client, _ = wired
    response = _get(client)
    # 🔴 先断 200 再断内容:返工单 §5.B 点名,上一轮判⑧ 就栽在
    #    "404 错误体里当然没有 calibration" 这种假绿上。
    assert response.status_code == 200, response.text
    assert _calibration(response) is None
    assert "calibration" not in json.dumps(response.json(), ensure_ascii=False)


def test_owner_bearer_does_get_calibration_through_real_asgi(wired):
    """🔴🔴【本单核心 · ①-2 接线锁】归属服务商带真 JWT 走**真 ASGI 请求**必须拿到。

    这一条就是上一轮漏掉的那条。它不碰 `request.state.user`,
    走的是「HTTP 头 → 中间件 → 端点 → 响应体」整条真链路。
    """
    client, make_token = wired
    response = _get(client, make_token(OWNER_UID))
    assert response.status_code == 200, response.text
    calibration = _calibration(response)
    assert calibration is not None, (
        "归属服务商拿不到 calibration —— 端点内可选身份解析没接上"
    )
    assert calibration["decisionEndpoint"] == "/api/diagnosis/{diagnosis_id}/brand-cells/decision"
    # 正向内容也要真有:光有键、pendingCount=0 说明明细链断了
    assert calibration["pendingCount"] == 16
    assert len(calibration["items"]) == 16
    assert calibration["items"][0]["similarNames"]


def test_server_payload_satisfies_the_frontend_validator_shape(wired):
    """🔴【接线最后一米】服务端真发的这段,必须过前端 `validCalibration` 的形状要求。

    前端对非法载荷是**静默降级**(`logContractViolation` + 整段不渲染)——
    也就是说形状差一个字段,页面上表现得和"没修"一模一样,零报错。
    所以判据在这里对着前端校验器逐字段核,不靠"应该没问题"。
    """
    client, make_token = wired
    calibration = _calibration(_get(client, make_token(OWNER_UID)))
    assert isinstance(calibration["decisionEndpoint"], str)
    assert isinstance(calibration["pendingCount"], int)
    assert isinstance(calibration["items"], list)
    for item in calibration["items"]:
        assert isinstance(item["question"], str)
        assert isinstance(item["platformName"], str)
        assert isinstance(item["engine"], str)
        assert isinstance(item["decisionVersion"], int)
        assert isinstance(item["similarNames"], list)
        assert item["answerExcerpt"] is None or isinstance(item["answerExcerpt"], str)

    # 反向对照:上面这张字段表就是前端校验器里的那张 —— 它变了这条就该失效。
    validator = (ROOT / "frontend/src/features/publicReportPremium/transport"
                 / "mapDto.ts").read_text(encoding="utf-8")
    checker = validator.split("function validCalibration")[1].split("\n}")[0]
    for field in ("decisionEndpoint", "pendingCount", "items", "question",
                  "platformName", "engine", "decisionVersion", "similarNames",
                  "answerExcerpt"):
        assert field in checker, f"前端校验器不再校 {field} —— 上面的字段表要跟着改"


def test_stranger_bearer_gets_nothing(wired):
    """🔴【①-1 反向对照 · 资源级归属】任意登录用户拿到别人的分享链接 → 拿不到。

    「登录了就行」不算数。这条红了就说明归属判据被放宽成了"有身份即可"。
    """
    client, make_token = wired
    response = _get(client, make_token(STRANGER_UID))
    assert response.status_code == 200, response.text
    assert _calibration(response) is None


def test_admin_bearer_gets_calibration(wired):
    """admin 视角仍然放行(与 `_viewer_can_calibrate` 既有口径一致)。"""
    client, make_token = wired
    response = _get(client, make_token(STRANGER_UID, is_admin=True))
    assert response.status_code == 200, response.text
    assert _calibration(response) is not None


# ══════════════════════════════════════════════════════════════════════
# ①-3 坏 token 一律当匿名,且 HTTP 仍 200
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("header_value", [
    "Bearer not-a-jwt-at-all-but-long-enough-to-pass-the-portal-check",
    "Bearer eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoxMTJ9.forged-signature",
    "Bearer ",
    "Bearer",
    "Basic dXNlcjpwYXNz",
    "eyJhbGciOiJIUzI1NiJ9.x.y",     # 没有 Bearer 前缀
    "Bearer abc.def",               # 段数不对
], ids=["garbage", "forged_sig", "empty", "no_space", "basic", "no_prefix", "two_parts"])
def test_broken_authorization_is_treated_as_anonymous_not_rejected(wired, header_value):
    """🔴【①-3】坏 token / 畸形头 → **当匿名 + HTTP 仍 200**。

    反向对照就在断言本身:任何一种坏输入让状态码不是 200,这条就红 ——
    那正是"给客户面链接加了鉴权"的形态,等于再造一次 P0。
    """
    client, _ = wired
    response = client.get(
        f"/api/public/report/{DIAG_ID}?st={SHARE_TOKEN}",
        headers={"Authorization": header_value},
    )
    assert response.status_code == 200, f"{header_value!r} → {response.status_code}"
    assert _calibration(response) is None


def test_expired_jwt_is_anonymous(wired):
    """过期 token → 匿名。签名是对的,只有 exp 过了 —— 判据必须真看 exp。"""
    client, make_token = wired
    response = _get(client, make_token(OWNER_UID, expires_in=-10))
    assert response.status_code == 200, response.text
    assert _calibration(response) is None, "过期 token 竟然还认身份 —— decode_jwt 的 exp 判定被绕过"


def test_portal_short_token_is_not_an_agent_identity(wired):
    """🔴【红线④ fail-closed 方向】门户短 token 是**客户侧凭证**,不得当服务商身份。

    认了它 = 给客户开了校准入口,方向正好反了。
    """
    client, _ = wired
    response = client.get(
        f"/api/public/report/{DIAG_ID}?st={SHARE_TOKEN}",
        headers={"Authorization": "Bearer abc123def456"},   # ≤20 位且非 eyJ 开头
    )
    assert response.status_code == 200, response.text
    assert _calibration(response) is None


# ══════════════════════════════════════════════════════════════════════
# 源码面元判据 —— 钉死"不许再用构造 request 冒充接线"和三条红线
# ══════════════════════════════════════════════════════════════════════

def test_this_file_never_fabricates_request_state_user():
    """🔴【元判据】本文件不许出现手工构造的 `request.state.user`。

    上一轮 20 条门禁全绿而 bug 上线,根因就是全都在验函数。这条锁机械地
    保证本文件是**接线级**的:一旦有人图省事改回构造 request,这条立刻红。
    """
    import io
    import tokenize

    def code_only(text: str) -> str:
        """只留**代码 token**:注释和字符串字面量全丢掉。

        🔴 不能直接 grep 源文件:本文件的 docstring 里就写着这些词(讲的正是
        它们为什么不许出现),那会让判据撞上自己 —— 「锚点撞自己注释」的老坑。
        丢掉字符串还有一个附带好处:下面 needles 自己是字符串,天然不自撞。
        `"".join` 不加分隔符,`a . b . c` 会拼回 `a.b.c`,属性链判据才成立。
        """
        return "".join(
            token.string
            for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )

    needles = ("SimpleNamespace", "request.state.user")

    # 反向对照先跑:针必须真能命中"构造身份"的代码形态,
    # 否则拼错一个字母就让下面恒绿(恒绿和恒红一样废)。
    probe = code_only(
        "from types import SimpleNamespace\n"
        "request = SimpleNamespace(state=SimpleNamespace(user={'id': 1}))\n"
        "request.state.user = {'id': 1}\n"
    )
    assert all(needle in probe for needle in needles), "针拼错了 → 判据没有判别力"

    body = code_only(Path(__file__).read_text(encoding="utf-8"))
    for forbidden in needles:
        assert forbidden not in body, f"接线锁里出现了构造身份的痕迹:{forbidden}"


def test_auth_middleware_untouched_public_prefix_still_early_returns():
    """🔴【红线① 反向对照】`auth/middleware.py` 是受保护文件,本单不许动它。

    判据打在**行为**上:`/api/public/` 仍在公共前缀里、仍然提前放行。
    如果有人"顺手"把软鉴权加进中间件,这条会红 —— 那是必须先交 Owner 的改动。
    """
    middleware_src = (ROOT / "auth" / "middleware.py").read_text(encoding="utf-8")
    assert '"/api/public/",' in middleware_src, "公共前缀被动过"
    from auth import middleware as mw

    assert "/api/public/" in mw.PUBLIC_PREFIXES
    assert not mw._is_portal_protected("/api/public/report/1"), (
        "公开报告端点被划进受保护路径 = 给客户面链接加了鉴权(红线②)"
    )


def test_frontend_still_does_not_judge_identity():
    """🔴【红线③】前端渲染条件仍然只是"数据在不在",不做第二处身份判定。"""
    page = (ROOT / "frontend/src/features/publicReportPremium/components"
            / "ReportPage.tsx").read_text(encoding="utf-8")
    assert "state.report.calibration ?" in page
    for forbidden in ("owner_user_id", "isAdmin", "is_admin", "currentUser"):
        assert forbidden not in page, f"前端出现身份判定痕迹:{forbidden}"
