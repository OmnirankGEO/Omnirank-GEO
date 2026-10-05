"""[WP9-P0-7 ④ · Owner P1]decision='skipped' 在**两种起点**跑完启动链后都能插入。

起点 A(新库):init_db 的 inline CHECK 直接带 skipped。
起点 B(旧库):既有 CHECK 只含 approved/rejected → 启动链里的 widen_review_decision_check
就地放宽。两者跑完都必须能写入 skipped 审计事件;且**第四个值仍被拒**(反漂移意图不变)。

用 TEMP 表影子真表,ON COMMIT DROP,不碰持久数据。
"""
import os

import psycopg2
import psycopg2.extras
import pytest

# 轻量 import:diagnosis_db 在 import 时会跑整条 init_db() 建表链,判别测试不该拉起全量
# schema。widen_review_decision_check 单独成模块(无 import 期副作用),启动链调用的是同一函数。
from db.review_decision_schema import widen_review_decision_check

_FRESH_DDL = """
CREATE TEMP TABLE geo_article_review_events (
  id BIGSERIAL PRIMARY KEY, article_id BIGINT NOT NULL, actor_user_id INTEGER NOT NULL,
  decision VARCHAR(40) NOT NULL CHECK (decision IN ('approved', 'rejected', 'skipped')),
  reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
) ON COMMIT DROP;
"""

_LEGACY_DDL = """
CREATE TEMP TABLE geo_article_review_events (
  id BIGSERIAL PRIMARY KEY, article_id BIGINT NOT NULL, actor_user_id INTEGER NOT NULL,
  decision VARCHAR(40) NOT NULL
      CONSTRAINT ck_geo_article_review_decision CHECK (decision IN ('approved', 'rejected')),
  reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
) ON COMMIT DROP;
"""


@pytest.fixture()
def cur():
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"],
                            cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn.cursor()
    finally:
        conn.rollback()
        conn.close()


def _insert(cur, decision: str):
    cur.execute(
        "INSERT INTO geo_article_review_events(article_id,actor_user_id,decision,reason) "
        "VALUES (1, 7, %s, '品牌故事已由负责人过目,明示跳过')",
        (decision,),
    )


def test_fresh_schema_accepts_skipped(cur):
    cur.execute(_FRESH_DDL)
    widen_review_decision_check(cur)  # 幂等:新库再跑一次也不出错
    _insert(cur, "skipped")
    cur.execute("SELECT COUNT(*) AS c FROM geo_article_review_events WHERE decision='skipped'")
    assert cur.fetchone()["c"] == 1


def test_legacy_schema_rejects_skipped_before_and_accepts_after_startup_chain(cur):
    cur.execute(_LEGACY_DDL)
    # 放宽之前:旧 CHECK 必须拒 skipped(证明本判别真的在测放宽,而不是恒过)
    cur.execute("SAVEPOINT before_widen")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(cur, "skipped")
    cur.execute("ROLLBACK TO SAVEPOINT before_widen")
    # 跑启动链的放宽后:可写入
    widen_review_decision_check(cur)
    _insert(cur, "skipped")
    cur.execute("SELECT COUNT(*) AS c FROM geo_article_review_events WHERE decision='skipped'")
    assert cur.fetchone()["c"] == 1


def test_widen_is_idempotent_and_still_rejects_unknown_decision(cur):
    cur.execute(_LEGACY_DDL)
    widen_review_decision_check(cur)
    widen_review_decision_check(cur)  # 重复执行不报错、不叠约束
    _insert(cur, "approved")
    _insert(cur, "rejected")
    _insert(cur, "skipped")
    # 反漂移:第四个值仍必须被拒
    cur.execute("SAVEPOINT unknown_decision")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(cur, "pending")
    cur.execute("ROLLBACK TO SAVEPOINT unknown_decision")


# ===== [返修 P2-2] 四张 target_kind 表的放宽范围必须"有意为之",不能靠代码纪律 =====

def test_username_widening_scope_is_intentional_and_locked():
    """生产有四张表带 target_kind CHECK,本轮只放宽了两张:
      放宽:organization_invites / organization_operator_accounts(会真的出现 username 行)
      不放宽:organization_invite_verification_challenges / organization_invite_delivery_outbox
             (username 邀请显式不创建 challenge/outbox —— 更严的约束正是护栏)
    这条判别把"只放宽两张"锁成**显式契约**:哪天有人给 username 邀请加了 challenge/outbox,
    插入会被更严的 CHECK 挡住并触发本判别的复核,而不是静默写坏数据。
    """
    from pathlib import Path
    root = Path(__file__).parent.parent
    sql = (root / "scripts" / "migration_organization_username_invite_2026_07_25.sql").read_text(encoding="utf-8")
    for widened in ("organization_invites", "organization_operator_accounts"):
        assert widened in sql, f"{widened} 应被放宽"
    for kept_strict in ("organization_invite_verification_challenges",
                        "organization_invite_delivery_outbox"):
        assert kept_strict not in sql, f"{kept_strict} 不应被放宽(username 不入该表)"
    # 代码侧对应护栏:username 邀请显式拒绝创建 challenge、且不入投递队列
    onboarding = (root / "services" / "organization_onboarding.py").read_text(encoding="utf-8")
    assert "ORG_INVITE_CREDENTIAL_MODE" in onboarding, "username 邀请必须显式拒绝创建 challenge"
    org_service = (root / "services" / "organization_service.py").read_text(encoding="utf-8")
    assert 'target_kind != "username"' in org_service, "username 邀请必须跳过投递队列"


# ===== [Deploy-CTO 返修]"写了迁移"≠"生产会跑迁移":必须登记进唯一权威清单 =====

def test_username_invite_migration_is_registered_in_prestart_manifest():
    """本迁移必须在 db/migration_manifest.MIGRATIONS 里,否则生产永远不会应用它。

    背景(本条判别的来由):PG 判别测试是自己 read_text 后手动 apply 这份 SQL 的,
    因此**即使漏登记,测试仍全绿**;而生产 prestart 只跑 MIGRATIONS 清单
    (scripts/prestart.py 与 server.py 共用的单一权威源)。target_kind 又**没有**
    像 decision 那样的运行时兜底放宽函数 → 漏登记 = 生产 CHECK 停在 (phone,email),
    用户名式邀请写库直接 CheckViolation。删掉 manifest 里那一行,本条必须转红。
    """
    from db.migration_manifest import MIGRATIONS

    name = "scripts/migration_organization_username_invite_2026_07_25.sql"
    assert name in MIGRATIONS, "用户名式邀请迁移漏登记 → prestart 不会应用 → 生产必挂"
    # 依赖顺序:两张被 ALTER 的表由更早的组织席位/开户迁移建立,故必须排在它们之后。
    for prerequisite in (
        "scripts/migration_organization_internal_seats_2026_07_20.sql",
        "scripts/migration_organization_all_accounts_onboarding_2026_07_22.sql",
    ):
        assert prerequisite in MIGRATIONS, f"前置迁移缺失: {prerequisite}"
        assert MIGRATIONS.index(name) > MIGRATIONS.index(prerequisite), (
            f"{name} 必须排在 {prerequisite} 之后(否则 ALTER 的表还不存在)"
        )
    # 文件真实存在(防登记了一个拼错/已删的路径)
    from pathlib import Path
    assert (Path(__file__).parent.parent / name).is_file(), f"清单登记的迁移文件不存在: {name}"
