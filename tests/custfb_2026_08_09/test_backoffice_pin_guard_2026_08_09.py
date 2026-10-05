"""客户反馈④ · 对外品牌授权"失效" —— 钉死账号写入守卫锁 + 三条件组合矩阵。

🔴 本文件先说清楚一件事:**工单对 ④ 的成因判断是错的,而且证据在库里。**

  工单说:132(翔玉咨询)`oem+active+unlocked_by_admin=true` 而
          `backoffice_brand_unlocked=FALSE`,是"管理员漏点第三个开关"。
  实测:`whitelabel_audit` id=21 —— 2026-07-31 19:57:43,admin(user 1) 把 132 的
        `backoffice_brand_unlocked` 从 False 改成 **True**。审计表 append-only
        (触发器禁 UPDATE/DELETE),这条抹不掉。**他点了。**
        而今天该行是 FALSE,`backoffice_brand_granted_by=1` /
        `backoffice_brand_granted_at=2026-07-31 19:57:43` 两个授权戳还留着,
        审计里没有任何一条把它改回去的记录 —— 应用侧每次写都留痕,所以不是人改的。

  改回去的是 `scripts/migration_whitelabel_backoffice_scope_2026_07_22.sql` §4:
  它在 `db/migration_manifest.py` 里,`scripts/prestart.py` 每次部署全量重跑清单。

  那为什么不给 §4 加"别覆盖 admin 决定"的守卫?因为同一份迁移的 §6 定义了
  `whitelabel_backoffice_schema_blockers()`:"132 的 backoffice_brand_unlocked 为 TRUE"
  是一条 **blocker**,prestart / 运行时自检 / release readiness 三处 fail-closed。
  → 132 固定 customer-only 是**裁决的不变式**,不是忘拨的开关;放开它下一次部署会被
    自己的 readiness 合同挡住,比静默重置更糟。要不要放开归 Owner。

  所以本包只修**不改变授权语义**的那一半:堵住"能点、点了没用、还没人告诉他"的静默路径。

锁:
  锁1  一致性:`BACKOFFICE_BRAND_PINNED_USER_IDS` 与迁移文件里被点名的 id 集合逐字相同
  锁2  写入守卫:给钉死账号授予后台换肤 → 400 且带机器可读 code
  锁3  反向对照:同一端点给**非**钉死账号授予 → 不被这条守卫拦(证明不是恒 400)
  锁4  撤权方向不拦(把钉死账号设成 FALSE 是合法操作)
  锁5  库级不变式:走完迁移后,钉死账号必须是 FALSE;readiness 合同必须能判出违规
  锁6-9 三条件组合矩阵(mode × status × unlocked_by_admin → backoffice 映射)

环境:锁5-9 需要 TEST_DATABASE_URL 指向一次性 loopback 测试库。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
FORWARD_SQL = ROOT / "scripts" / "migration_whitelabel_backoffice_scope_2026_07_22.sql"

BASE_SETTINGS_DDL = """
CREATE TABLE public.users (id INTEGER PRIMARY KEY, username TEXT);
CREATE TABLE public.whitelabel_settings (
    user_id INTEGER PRIMARY KEY REFERENCES public.users(id),
    company_name TEXT,
    company_logo_url TEXT,
    logo_url TEXT,
    slogan TEXT,
    brand_color VARCHAR(20),
    contact_name TEXT,
    contact_phone TEXT,
    contact_wechat TEXT,
    contact_email TEXT,
    whitelabel_mode TEXT DEFAULT 'none',
    whitelabel_status TEXT DEFAULT 'locked',
    unlocked_by_admin BOOLEAN DEFAULT FALSE,
    approved_by INTEGER,
    approved_at TIMESTAMP,
    product_name TEXT,
    favicon_url TEXT,
    hide_platform_branding BOOLEAN DEFAULT FALSE,
    custom_domain TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


# ── 锁1 · 常量与迁移点名的 id 集合必须一致 ────────────────────────────────
def test_pinned_ids_match_migration():
    """应用侧常量不许和迁移各说各话(那就是第二份授权口径)。"""
    from api.referral_api import BACKOFFICE_BRAND_PINNED_USER_IDS

    sql_text = FORWARD_SQL.read_text(encoding="utf-8")
    # 只看**代码行**:注释里现在写满了 132 的取证说明,扫注释必然误判。
    code = "\n".join(
        ln for ln in sql_text.splitlines() if not re.match(r"^\s*--", ln)
    )
    ids_in_migration = {
        int(m) for m in re.findall(
            r"SET\s+backoffice_brand_unlocked\s*=\s*FALSE\s+WHERE\s+user_id\s*=\s*(\d+)",
            code, flags=re.IGNORECASE,
        )
    }
    assert ids_in_migration, "迁移里没找到任何被钉死的 user_id —— 正则或迁移变了,先查清楚"
    assert set(BACKOFFICE_BRAND_PINNED_USER_IDS) == ids_in_migration, (
        f"应用侧 {sorted(BACKOFFICE_BRAND_PINNED_USER_IDS)} 与迁移 {sorted(ids_in_migration)} 不一致"
    )


# ── 锁2-4 · 写入守卫(不碰库,守卫在进库之前) ─────────────────────────────
class _FakeState:
    def __init__(self, user):
        self.user = user


class _FakeRequest:
    def __init__(self, user):
        self.state = _FakeState(user)
        self.client = None


def _grant(target_user_id, backoffice):
    from api.referral_api import WhitelabelGrantRequest, admin_set_whitelabel

    req = WhitelabelGrantRequest(
        whitelabel_mode="oem", whitelabel_status="active",
        backoffice_brand_unlocked=backoffice,
    )
    return asyncio.run(admin_set_whitelabel(
        target_user_id, req, _FakeRequest({"user_id": 1, "is_admin": True})))


def test_pinned_account_grant_is_rejected():
    from fastapi import HTTPException

    from api.referral_api import BACKOFFICE_BRAND_PINNED_USER_IDS

    pinned_id = sorted(BACKOFFICE_BRAND_PINNED_USER_IDS)[0]
    with pytest.raises(HTTPException) as exc:
        _grant(pinned_id, True)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "BACKOFFICE_BRAND_PINNED_CUSTOMER_ONLY"


def test_non_pinned_account_is_not_blocked_by_this_guard():
    """反向对照:非钉死账号不许被这条守卫拦。

    它会往下走到真正的库操作并因为没有 DB 而抛别的错 —— 只要**不是**
    BACKOFFICE_BRAND_PINNED_CUSTOMER_ONLY,就证明这条守卫不是恒 400。
    """
    from fastapi import HTTPException

    from api.referral_api import BACKOFFICE_BRAND_PINNED_USER_IDS

    other_id = max(BACKOFFICE_BRAND_PINNED_USER_IDS) + 10_000
    try:
        _grant(other_id, True)
    except HTTPException as exc:
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code != "BACKOFFICE_BRAND_PINNED_CUSTOMER_ONLY", "守卫误伤了非钉死账号"
    except Exception:
        pass  # 走到库层才炸 = 已经越过守卫,正是本条要证明的


def test_pinned_account_revoke_direction_is_allowed():
    """撤权(设 FALSE)不该被拦 —— 守卫只挡"授予"这一个方向。"""
    from fastapi import HTTPException

    from api.referral_api import BACKOFFICE_BRAND_PINNED_USER_IDS

    pinned_id = sorted(BACKOFFICE_BRAND_PINNED_USER_IDS)[0]
    try:
        _grant(pinned_id, False)
    except HTTPException as exc:
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code != "BACKOFFICE_BRAND_PINNED_CUSTOMER_ONLY", "撤权方向不该被拦"
    except Exception:
        pass


# ── 锁5-9 · 真 PG:迁移映射矩阵 + readiness 合同判别力 ─────────────────────
def _skip_unless_throwaway_pg():
    if not PG_URL:
        pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)")
    parsed = urlsplit(PG_URL)
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        pytest.skip(f"只允许 loopback 一次性容器 DSN:{parsed.hostname}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"基础库名必须含 test 且不含 prod:{base_db}")


@pytest.fixture(scope="module")
def pgdb():
    _skip_unless_throwaway_pg()
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"custfb_wl_test_{uuid.uuid4().hex[:10]}"
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.close()


# 三条件组合矩阵:(mode, status, unlocked_by_admin) → 迁移后 backoffice 应为?
# §3 只映射三条件**全齐**的行;其余一律保持 FALSE(fail-closed)。
COMBOS = [
    (901, "oem", "active", True, True),            # 三条件齐 → 授权保留
    (902, "oem", "active", False, False),          # 缺 unlocked_by_admin
    (903, "oem", "locked", True, False),           # 缺 active
    (904, "external_only", "active", True, False), # 缺 oem(仅客户页面)
    (905, "none", "locked", False, False),         # 完全未授权
]


def test_three_condition_matrix_and_pin(pgdb):
    from api.referral_api import BACKOFFICE_BRAND_PINNED_USER_IDS

    pinned_id = sorted(BACKOFFICE_BRAND_PINNED_USER_IDS)[0]
    with pgdb.cursor() as cur:
        cur.execute(BASE_SETTINGS_DDL)
        rows = COMBOS + [(pinned_id, "oem", "active", True, False)]
        cur.executemany(
            "INSERT INTO public.users (id, username) VALUES (%s, %s)",
            [(r[0], f"u{r[0]}") for r in rows],
        )
        cur.executemany(
            "INSERT INTO public.whitelabel_settings "
            "(user_id, company_name, whitelabel_mode, whitelabel_status, unlocked_by_admin,"
            " approved_by, approved_at) VALUES (%s, %s, %s, %s, %s, 1, NOW())",
            [(r[0], f"C{r[0]}", r[1], r[2], r[3]) for r in rows],
        )
        cur.execute(FORWARD_SQL.read_text(encoding="utf-8"))
        cur.execute(
            "SELECT user_id, backoffice_brand_unlocked FROM public.whitelabel_settings "
            "ORDER BY user_id")
        actual = {int(r["user_id"]): bool(r["backoffice_brand_unlocked"])
                  for r in cur.fetchall()}

    expected = {r[0]: r[4] for r in rows}
    assert actual == expected, f"三条件映射矩阵不符:实际 {actual} / 期望 {expected}"
    # 元判据:矩阵里 True/False 两侧都有 —— 全 False 的期望表是零判别力的。
    assert any(expected.values()) and not all(expected.values())
    assert actual[pinned_id] is False, "钉死账号即使三条件齐也必须是 FALSE"


def test_readiness_contract_flags_pinned_violation(pgdb):
    """readiness 合同必须能判出"钉死账号被置 TRUE" —— 这正是不能给 §4 加守卫的理由。"""
    from api.referral_api import BACKOFFICE_BRAND_PINNED_USER_IDS

    pinned_id = sorted(BACKOFFICE_BRAND_PINNED_USER_IDS)[0]
    with pgdb.cursor() as cur:
        cur.execute(
            "SELECT blocker FROM public.whitelabel_backoffice_schema_blockers(true)")
        clean = [r["blocker"] for r in cur.fetchall()]
        assert not any(b.startswith("id132_backoffice_unlocked") for b in clean), clean

        cur.execute("BEGIN")
        cur.execute(
            "UPDATE public.whitelabel_settings SET backoffice_brand_unlocked = TRUE "
            "WHERE user_id = %s", (pinned_id,))
        cur.execute(
            "SELECT blocker FROM public.whitelabel_backoffice_schema_blockers(true)")
        dirty = [r["blocker"] for r in cur.fetchall()]
        cur.execute("ROLLBACK")
    assert any(b.startswith("id132_backoffice_unlocked") for b in dirty), (
        f"合同没判出违规 —— 它就成了摆设:{dirty}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
