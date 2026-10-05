"""v2.html admin 预览「接线」判别锁 · `WO_PUBLIC_PREFIX_SOFT_AUTH_SURVEY_2026-08-08` §5.3(甲案)。

🔴🔴 **这一份存在的唯一理由与第 15 班车完全相同,只是换了一条路由。**

`/api/public/report/{id}/v2.html` 在 `auth/middleware.py` 的 `PUBLIC_PREFIXES` 下,
中间件命中就提前 `return await call_next(request)`,**压根不填 `request.state.user`**。
下游 `services/report_v3_gating.is_admin_preview_request` 读它 → 恒 `False`
→ `?preview_v3=1` 从上线起一次都没生效过。函数是对的,接线是断的。

**2026-08-08 生产实测证伪**(QA admin 112 · diagnosis 536 · 带真 share_token):
匿名 / 匿名+preview / admin+preview / admin 四条响应 HTTP 全 200、**逐字节同 hash**、
且都不含 v3 渲染器特征串 `report-v3-section` —— 这排除了「v3 已全局开启所以都一样」
这个混淆解释。缺陷由 L1 推导升级为 L2 实证。

所以本文件每条锁都走**真的 ASGI 请求**(TestClient + 真中间件 + 真库 + 生产签名函数),
**禁止**手工构造 `request.state.user`;末尾有元判据机械钉死这一点。
夹具形态照抄 `tests/test_portal_calib_wiring_2026_08_08.py`(第 15 班车已在生产验证过的同形)。
"""
from __future__ import annotations

import json
import os
import re
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

DIAG_ID = 78001
BRAND_ID = 78001
ADMIN_UID = 112       # QA 超管
PLAIN_UID = 114       # 普通用户(非 admin)
SHARE_TOKEN = "softauthtoken78001"

# v3 渲染器独有特征串。生产实测过它在 `services/report_html_renderer_v3.py` 命中 4 次
# (反向对照:证明这不是一个永远搜不到的串),而 legacy 渲染结果里 0 次。
V3_MARKER = "report-v3-section"

pytestmark = pytest.mark.skipif(not PG_URL, reason="real PostgreSQL URL is required")


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
    report_v2_internal_md TEXT,
    report_v2_client_md TEXT,
    report_v2_modules_jsonb JSONB,
    report_v2_generated_at TIMESTAMPTZ,
    data_completeness_score INTEGER,
    data_completeness_breakdown JSONB,
    raw_data_json JSONB,
    result_visibility TEXT,
    share_token TEXT
);
-- [R5 ⑤ 批2] brands: 见下方 ensure_brands_schema()（生产 SSOT 出口），不在这里手搓。
CREATE TABLE short_links (
    id BIGSERIAL PRIMARY KEY, code TEXT, target_url TEXT
);
"""


@pytest.fixture(scope="module")
def wired():
    """真库 + 真中间件 + 真路由。返回 (client, make_token)。"""
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    database_name = f"softauth_{uuid.uuid4().hex[:12]}"
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
    # 线上就是严格模式 —— 不放宽,否则测的不是生产形态
    os.environ["PUBLIC_REPORT_REQUIRE_TOKEN"] = "true"

    from db import connection as connection_db

    connection_db.DATABASE_URL = target_url
    connection_db._pool = None

    modules_jsonb = {
        "client": {"modules": {"1": {"insight": "本次采样里品牌可见度偏低。"}}},
        "funnel": {"total_score": 20, "level": "隐形型"},
    }

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
            (BRAND_ID, "软鉴权测试品牌", ADMIN_UID),
        )
        cur.execute(
            """
            INSERT INTO diagnosis_records
                (id, brand_id, brand_name, industry, total_score, level, keywords,
                 report_v2_version, report_v2_internal_md, report_v2_client_md,
                 report_v2_modules_jsonb, report_v2_generated_at,
                 result_visibility, share_token)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'v2',%s,%s,%s,NOW(),'published',%s)
            """,
            (DIAG_ID, BRAND_ID, "软鉴权测试品牌", "制造", 20, "隐形型", "测试关键词",
             "# 内部版", "# 客户版报告正文",
             json.dumps(modules_jsonb, ensure_ascii=False), SHARE_TOKEN),
        )
    boot.close()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.share_api as share_api
    from auth.middleware import setup_auth_middleware

    app = FastAPI()
    # 🔴 真中间件必须在栈上 —— 本 bug 的病因就是它对 /api/public/ 提前放行。
    #    绕过它 = 把病因从测试环境里删掉,再绿也不算数。
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


def _get(client, *, token: str | None = None, preview: bool = False,
         raw_auth: str | None = None):
    url = f"/api/public/report/{DIAG_ID}/v2.html?st={SHARE_TOKEN}"
    if preview:
        url += "&preview_v3=1"
    headers: dict[str, str] = {}
    if raw_auth is not None:
        headers["Authorization"] = raw_auth
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    return client.get(url, headers=headers)


# ══════════════════════════════════════════════════════════════════════
# 判别力前提 —— 没有这条,后面所有断言都可能是恒真
# ══════════════════════════════════════════════════════════════════════

def test_middleware_is_really_in_the_stack(wired):
    """🔴 证明真中间件确实在栈上。

    否则「非 admin 看不到 v3」这类断言可能只是因为**中间件根本没装**,全部退化成恒真。
    拿一条**非公开**路由当对照:无 token 必须被挡成 401。
    """
    client, _ = wired
    resp = client.get("/api/share/qrcode?url=https://example.com")
    assert resp.status_code == 401, (
        "非公开路由无 token 竟然没被挡 → 中间件没生效,本文件其余断言全部失去判别力"
    )


def test_v3_marker_is_a_real_string_not_a_typo():
    """🔴 反向对照物必须已知非零。

    第 16→18 班车连踩两次「反向对照物本身为零」—— 断言一个永远搜不到的串,
    等于零判别力却全绿。这里先证明 `V3_MARKER` 真的是 v3 渲染器独有的串。
    """
    src = (ROOT / "services" / "report_html_renderer_v3.py").read_text(encoding="utf-8")
    assert src.count(V3_MARKER) > 0, f"{V3_MARKER!r} 在 v3 渲染器里一次都没出现 → 针拼错了"
    legacy = (ROOT / "services" / "report_html_renderer.py").read_text(encoding="utf-8")
    assert V3_MARKER not in legacy, f"{V3_MARKER!r} 在 legacy 渲染器里也有 → 不是 v3 独有,不能当判别物"


# ══════════════════════════════════════════════════════════════════════
# ① admin 预览 —— 成对
# ══════════════════════════════════════════════════════════════════════

def test_1_admin_with_preview_flag_gets_v3(wired):
    """🔴 本包的核心:admin + 有效 JWT + `?preview_v3=1` → 真的走 v3 渲染。

    生产实测基线(改动前)是**不生效**。这条转绿才说明接线补上了。
    """
    client, make_token = wired
    resp = _get(client, token=make_token(ADMIN_UID, is_admin=True), preview=True)
    assert resp.status_code == 200
    assert V3_MARKER in resp.text, "admin 带 preview_v3=1 仍未走 v3 → 接线没生效"


def test_1b_admin_without_preview_flag_does_not_get_v3(wired):
    """反向对照:同一个 admin **不带** `preview_v3=1` → 必须不预览。

    没有这条,「无条件放行」的错误实现也能让 ① 通过。
    """
    client, make_token = wired
    resp = _get(client, token=make_token(ADMIN_UID, is_admin=True), preview=False)
    assert resp.status_code == 200
    assert V3_MARKER not in resp.text, "不带 preview_v3 也走 v3 = 无条件放行"


def test_2_non_admin_with_preview_flag_does_not_get_v3(wired):
    """② 非 admin 带有效 JWT + `?preview_v3=1` → 必须 False。"""
    client, make_token = wired
    resp = _get(client, token=make_token(PLAIN_UID, is_admin=False), preview=True)
    assert resp.status_code == 200
    assert V3_MARKER not in resp.text, "非 admin 也能预览 v3 = is_admin 判据失效"


# ══════════════════════════════════════════════════════════════════════
# ③ fail-open 铁律 —— 四条一起判(第 15 班车的口径,本单照搬)
#    客户面门户是 token-only 不登录的,绝不能引入 401
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "case,kwargs",
    [
        ("匿名(客户拿到的那条链接)", {}),
        ("坏 token(签名错)",        {"token": "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoxfQ.badsig"}),
        ("门户短 token(客户侧凭证)", {"token": "GETM8YEK56H4"}),
        ("畸形头 Bearer 后为空",     {"raw_auth": "Bearer "}),
        ("畸形头 无 Bearer 前缀",    {"raw_auth": "Token abc123"}),
        ("畸形头 超长串",            {"raw_auth": "Bearer " + "x" * 8192}),
    ],
)
def test_3_fail_open_never_401(wired, case, kwargs):
    """🔴 六种身份解析失败形态,全部 **HTTP 200 且不预览** —— 一条都不许 401。"""
    client, _ = wired
    resp = _get(client, preview=True, **kwargs)
    assert resp.status_code == 200, f"{case} → 竟然不是 200(status={resp.status_code})· fail-open 被破坏"
    assert V3_MARKER not in resp.text, f"{case} → 竟然预览了 v3"


# ══════════════════════════════════════════════════════════════════════
# ④ 元判据 + 红线反向对照
# ══════════════════════════════════════════════════════════════════════

def _code_only(text: str) -> str:
    """剥掉注释与三引号文档串 —— 否则本文件自己的说明文字会命中禁用词。"""
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)
    return text


def _asgi_test_sources() -> dict[str, str]:
    """抽出**接线锁**(签名带 `wired` 夹具的那批)的函数体源码。

    🔴 第一版我拿整份文件去扫,结果元判据抓到了它自己的 needles 字面量、
    也抓到了 `test_6` 那个刻意的纯函数契约替身 —— **范围写宽 = 自造假红**。
    该管的从来只是「走真 ASGI 的那批锁不许手搓身份」,不是"全文件不许出现这几个字"。
    """
    text = Path(__file__).read_text(encoding="utf-8")
    out: dict[str, str] = {}
    blocks = re.split(r"^(?=def |@pytest)", text, flags=re.MULTILINE)
    for block in blocks:
        m = re.search(r"^def (test_\w+)\((.*?)\)", block, flags=re.MULTILINE | re.DOTALL)
        if m and "wired" in m.group(2):
            out[m.group(1)] = _code_only(block)
    return out


def test_4_asgi_locks_never_hand_construct_identity():
    """🔴 元判据:**接线锁**不许出现手工构造 `request.state.user` / 直接调门禁函数的痕迹。

    上一轮 20 条门禁全绿而 bug 照样上线,根因就是所有锁喂的都是手搓的 state.user ——
    验的是函数,不是接线。这条把「接线锁必须走真 ASGI」机械钉死。
    """
    needles = ("request.state.user", "state.user =", "is_admin_preview_request(")
    probe = "request.state.user = {}\nstate.user = 1\nis_admin_preview_request(req)"
    assert all(n in probe for n in needles), "针拼错了 → 判据没有判别力"

    sources = _asgi_test_sources()
    # 反向对照:接线锁必须真的存在且数量合理,否则"零个函数全部合规"是恒真
    assert len(sources) >= 6, f"只抽到 {len(sources)} 条接线锁 —— 抽取口径写废了,判据恒真"

    for name, body in sources.items():
        for forbidden in needles:
            assert forbidden not in body, f"接线锁 {name} 里出现了构造身份的痕迹:{forbidden}"


def test_5_auth_middleware_untouched(wired):
    """🔴【红线③ 反向对照】本单明确**不走乙案**,`auth/middleware.py` 一个字不许动。

    判据打在**行为**上,不是打在源码 diff 上:`/api/public/` 仍在公共前缀里、仍提前放行。
    若有人"顺手"把软鉴权加进中间件,这条会红 —— 那是必须先交 Owner 的改动。
    """
    from auth import middleware as mw

    assert "/api/public/" in mw.PUBLIC_PREFIXES, "公共前缀被动过"
    assert not mw._is_portal_protected("/api/public/report/1/v2.html"), (
        "公开报告端点被划进受保护路径 = 给客户面链接加了鉴权(红线①,会引入 401)"
    )


def test_6_gating_predicate_untouched():
    """🔴【红线② 反向对照】`is_admin_preview_request` 的判据一个字不改。

    它是对的(第 15 班车同一教训:函数对、接线缺),本单只补它读的那一层。
    """
    from services.report_v3_gating import is_admin_preview_request, should_render_report_v3

    class _S:  # 轻量替身:这里测的是**纯函数契约**,不是接线(接线由上面真 ASGI 那批测)
        pass

    class _R:
        def __init__(self, user, qp):
            self.state = _S()
            self.state.user = user
            self.query_params = qp

    assert is_admin_preview_request(_R({"is_admin": True}, {"preview_v3": "1"})) is True
    assert is_admin_preview_request(_R({"is_admin": True}, {})) is False
    assert is_admin_preview_request(_R({"is_admin": False}, {"preview_v3": "1"})) is False
    assert is_admin_preview_request(_R(None, {"preview_v3": "1"})) is False
    # 第一条分支仍是 admin 预览短路
    assert should_render_report_v3(object(), None, True) is True
