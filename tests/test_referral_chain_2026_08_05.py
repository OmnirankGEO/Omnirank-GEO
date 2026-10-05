"""WO_REFERRAL_CHAIN_2026-08-05_FINAL · 推荐链三修行为锁。

§1.3 名单/统计自洽:/api/referral/team 行数必须与 /api/referral/stats 的 l1/l2 计数一致
     (同源同表 referral_links;不一致即判 bug)。
§2   扫码归因:attribution 回显端点只回显示名,绝不回推荐码原文/手机号;
     短链 302 的归因 cookie 7 天 + 展示 cookie(非 HttpOnly · 只存短链 code)。
§3   注册闸拒绝路径必须带出口话术;SIGNUP_REQUIRE_REFERRAL 未显式声明必须启动告警。

只用测试库合成数据(conftest 已把 DATABASE_URL 切到 TEST_DATABASE_URL),不读生产。
变异自检:tests/mutation_runner_referral_chain.py(把判据挡的行为改回去 → 本文件必转红)。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


# ---------------- 测试基建 ----------------

class _StubRequest:
    """最小 Request 替身:referral 端点只用 request.state.user;
    auth 归因端点只用 request.cookies。"""

    def __init__(self, user=None, cookies=None):
        self.state = SimpleNamespace(user=user)
        self.cookies = cookies or {}
        self.headers = {}
        self.url = SimpleNamespace(scheme="https")


@pytest.fixture(scope="module")
def seeded_db():
    """建最小 schema + 种一组推荐关系(referrer 900 → L1×2 + L2×1)。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                display_name TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    from db.wallet_db import init_wallet_tables
    from api.share_api import init_share_tables

    init_wallet_tables()
    init_share_tables()

    # referral_codes 不走 init_referral_tables:那个 init 连带白标 backoffice 契约检查
    # (要求 migration_whitelabel_backoffice_scope 已应用),测试库只需要这一张表。
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_codes (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                code TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 幂等清场(只清本测试自己的行)
        cur.execute("DELETE FROM referral_links WHERE referrer_id IN (900) OR referred_id IN (901,902,903)")
        cur.execute("DELETE FROM short_links WHERE code IN ('tstcode1','tstcode2')")
        cur.execute("DELETE FROM referral_codes WHERE user_id IN (900, 910)")
        cur.execute("DELETE FROM users WHERE id IN (900,901,902,903,910)")
        cur.execute(
            """
            INSERT INTO users (id, username, display_name) VALUES
                (900, '13900000900', '推荐人九百'),
                (901, '13900000901', '下线一'),
                (902, '13900000902', ''),
                (903, '13900000903', '下线三'),
                (910, '13900000910', '张三邀请人')
            ON CONFLICT (id) DO NOTHING
            """
        )
        cur.execute(
            """
            INSERT INTO referral_links (referrer_id, referred_id, level, commission_rate) VALUES
                (900, 901, 1, 0.120),
                (900, 902, 1, 0.120),
                (900, 903, 2, 0.050)
            ON CONFLICT DO NOTHING
            """
        )
        # §2 归因链:短链 tstcode1 → metadata.referral_code=RC-TEST-910 → user 910(张三邀请人)
        cur.execute(
            "INSERT INTO referral_codes (user_id, code) VALUES (910, 'RC-TEST-910') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            """
            INSERT INTO short_links (code, target_url, creator_user_id, link_type, metadata)
            VALUES ('tstcode1', '/login?mode=register', 910, 'register',
                    '{"referral_code": "RC-TEST-910"}'::jsonb)
            ON CONFLICT (code) DO NOTHING
            """
        )
        conn.commit()
    finally:
        conn.close()
    return True


# ---------------- §1.3 名单/统计自洽 ----------------

def test_team_rows_match_stats_counts(seeded_db):
    """必须命中:/team 行数按 level 分组 == /stats 的 l1_count/l2_count(同源同表)。"""
    from api.referral_api import get_referral_stats, get_referral_network

    req = _StubRequest(user={"user_id": 900})
    stats = asyncio.run(get_referral_stats(req))["data"]
    team = asyncio.run(get_referral_network(req))["data"]

    l1_rows = [r for r in team if r["level"] == 1]
    l2_rows = [r for r in team if r["level"] == 2]
    assert stats["l1_count"] == len(l1_rows) == 2, (stats, team)
    assert stats["l2_count"] == len(l2_rows) == 1, (stats, team)


def test_team_rows_carry_display_fields(seeded_db):
    """§1.2 名单区需要的字段(昵称/注册时间/累计充值)后端必须齐。"""
    from api.referral_api import get_referral_network

    team = asyncio.run(get_referral_network(_StubRequest(user={"user_id": 900})))["data"]
    assert team, "种了 3 条关系却查不到"
    for row in team:
        for key in ("referred_id", "level", "created_at", "display_name", "username", "total_recharged"):
            assert key in row, f"/team 行缺字段 {key}: {row}"


def test_team_requires_auth(seeded_db):
    """反向对照:未登录(state.user=None)必须 401,名单不是公开数据。"""
    from fastapi import HTTPException
    from api.referral_api import get_referral_network

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_referral_network(_StubRequest(user=None)))
    assert exc.value.status_code == 401


# ---------------- §2.1 归因回显端点 ----------------

def _run_attribution(cookies):
    from api.auth_api import get_register_attribution

    return asyncio.run(get_register_attribution(_StubRequest(cookies=cookies)))["data"]


def test_attribution_recognized_returns_display_name_only(seeded_db):
    """必须命中:HttpOnly 归因 cookie → recognized + 邀请人显示名;
    隐私锁:载荷里绝不出现推荐码原文/短链码/手机号。"""
    data = _run_attribution({"omnirank_invite_attribution": "tstcode1"})
    assert data["recognized"] is True
    assert data["inviter_display_name"] == "张三邀请人"
    blob = json.dumps(data, ensure_ascii=False)
    assert "RC-TEST-910" not in blob, "推荐码原文泄漏(隐私口径被破)"
    assert "tstcode1" not in blob, "短链码回流进载荷"
    assert "13900000910" not in blob, "邀请人手机号泄漏"


def test_attribution_display_cookie_fallback(seeded_db):
    """展示 cookie(非 HttpOnly)同样能查到显示名(§2.2 供前端用)。"""
    data = _run_attribution({"omnirank_invite_display": "tstcode1"})
    assert data["recognized"] is True
    assert data["inviter_display_name"] == "张三邀请人"


def test_attribution_unrecognized_paths(seeded_db):
    """反向对照:无 cookie / 乱值 / 不存在的码 → recognized=false 且不抛错(不阻断注册)。"""
    assert _run_attribution({})["recognized"] is False
    assert _run_attribution({"omnirank_invite_attribution": "no-such-code-x"})["recognized"] is False
    assert _run_attribution({"omnirank_invite_attribution": "<script>"})["recognized"] is False


def test_attribution_empty_display_name_never_leaks_username(seeded_db):
    """display_name 空时给中性称呼,绝不裸露 username(手机号)。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET display_name = '' WHERE id = 910")
        conn.commit()
    finally:
        conn.close()
    try:
        data = _run_attribution({"omnirank_invite_attribution": "tstcode1"})
        assert data["recognized"] is True
        assert data["inviter_display_name"] == "你的邀请人"
        assert "13900000910" not in json.dumps(data)
    finally:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET display_name = '张三邀请人' WHERE id = 910")
            conn.commit()
        finally:
            conn.close()


# ---------------- §2.2 短链 302 cookie 口径 ----------------

def test_short_link_sets_seven_day_attribution_and_display_cookies(seeded_db):
    """必须命中:302 同时发两个 cookie —— 归因(HttpOnly · 7 天)+ 展示(非 HttpOnly · Path=/)。
    反向对照:report 类短链不发归因 cookie(下面单测)。"""
    from api.share_api import short_link_redirect

    resp = asyncio.run(short_link_redirect("tstcode1", _StubRequest()))
    cookies = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]
    attribution = [c for c in cookies if c.startswith("omnirank_invite_attribution=")]
    display = [c for c in cookies if c.startswith("omnirank_invite_display=")]
    assert attribution and display, f"cookie 缺失: {cookies}"
    assert "Max-Age=604800" in attribution[0], f"归因 cookie 不是 7 天: {attribution[0]}"
    assert "HttpOnly" in attribution[0]
    assert "Path=/api/auth/register" in attribution[0]
    assert "Max-Age=604800" in display[0], f"展示 cookie 不是 7 天: {display[0]}"
    assert "HttpOnly" not in display[0], "展示 cookie 必须非 HttpOnly(前端要读)"
    assert "Path=/;" in display[0] or display[0].rstrip().endswith("Path=/"), display[0]


def test_short_link_report_type_sets_no_attribution_cookie(seeded_db):
    """反向对照:非 register 类短链(report 等)不发归因 cookie —— 判据不是恒真。"""
    from db.connection import get_connection
    from api.share_api import short_link_redirect

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO short_links (code, target_url, creator_user_id, link_type, metadata)
            VALUES ('tstcode2', '/public/report/1?st=tstcode2', 910, 'report', '{}'::jsonb)
            ON CONFLICT (code) DO NOTHING
            """
        )
        conn.commit()
    finally:
        conn.close()
    resp = asyncio.run(short_link_redirect("tstcode2", _StubRequest()))
    cookies = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]
    assert not any("omnirank_invite" in c for c in cookies), cookies


# ---------------- §3 注册闸出口 + env 治理(源锁 · server.py 不可整包 import) ----------------

def _strip_py_comments(text: str) -> str:
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_register_refusal_carries_exit_wording():
    """拒绝路径必须给出口(向邀请人索取),不许光拒绝。"""
    src = Path(REPO / "api" / "auth_api.py").read_text(encoding="utf-8")
    assert "注册需要有效的推荐码,请向邀请你的人索取后再注册" in src


def test_signup_gate_env_governance_declared():
    """server.py 必须有 SIGNUP_REQUIRE_REFERRAL 未显式声明时的启动 WARNING(L0 治理)。"""
    src = _strip_py_comments(Path(REPO / "server.py").read_text(encoding="utf-8"))
    assert "_signup_gate_env_governance" in src
    assert "[L0治理]" in src, "启动告警文案被删"
    gov = src.split("_signup_gate_env_governance", 1)[1][:1500]
    assert "SIGNUP_REQUIRE_REFERRAL" in gov
    assert "logger.warning" in gov, "治理检查必须是 WARNING 级(不是 debug/info)"


def test_frontend_validation_allows_recognized_attribution():
    """LoginPage 提交校验:识别到归因时允许空推荐码(否则扫码用户仍被空框拦死)。"""
    src = Path(REPO / "frontend" / "src" / "pages" / "Login" / "LoginPage.tsx").read_text(encoding="utf-8")
    assert "!referral.trim() && !attribution?.recognized" in src, "免填条件被改回『必须手填』"
    assert "register/attribution" in src, "注册页没有调归因回显端点"
    assert "向邀请你" in src, "失效兜底出口话术缺失"
