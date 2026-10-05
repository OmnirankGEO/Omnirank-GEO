"""WP4 判据 · 五商业里程碑投影 + 迁移 042 的库层行为(ACT-01/03/07/08/09/13/14)。

库层判据**打真 PG16**,不打 mock。理由:本包核心是一条**复合外键**,
mock 一个 cursor 只能证明「我写的 Python 调了 UPDATE」,证明不了
「跨 quote 那一行真的插不进去」。本仓 2026-08-18 明确记过
「真 HTTP/真库判据必须打真库」。
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg2
import pytest

from services.defensive_geo.commercial_milestones import (
    ALLOWED_TRANSITIONS,
    MILESTONE_ORDER,
    NotEnrolled,
    customer_confirm_notice,
    progress_steps,
    project,
    user_label,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_042 = (
    ROOT / "db" / "migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql"
)

V2_SNAPSHOT = {"delivery_plan": {"schema_version": "geo-delivery-plan-v1"}}
LEGACY_SNAPSHOT = {"delivery_plan": {"schema_version": "geo-legacy-plan"}}


# ------------------------------------------------------------------ 里程碑投影

def test_live_session_states_map_to_expected_milestones():
    expected = {
        "quoted": "offer_sent",
        "confirmed": "customer_accepted",
        "pending_payment": "sales_validated",
        "active": "commercial_basis_established",
    }
    for status, milestone in expected.items():
        view = project(session_status=status, pricing_snapshot=V2_SNAPSHOT)
        assert view.milestone == milestone, status


def test_active_alone_is_not_service_activated():
    """🔴 现役只有一个 ``active`` 态,规格要求收款与开工分开(§3.1)。

    把 ``active`` 直接读成 ``service_activated`` 会让 ACT-06/ACT-13 静默变绿
    —— 那是「用复合态代表其中任一条件」的老毛病。这条判据钉死这一格。
    """
    view = project(session_status="active", pricing_snapshot=V2_SNAPSHOT)
    assert view.milestone == "commercial_basis_established"
    assert view.milestone != "service_activated"


def test_activation_facts_drive_the_last_two_milestones():
    pending = project(
        session_status="active", pricing_snapshot=V2_SNAPSHOT,
        has_durable_activation=True,
    )
    assert pending.milestone == "activation_pending"

    done = project(
        session_status="active", pricing_snapshot=V2_SNAPSHOT,
        has_durable_activation=True, activation_materialized=True,
    )
    assert done.milestone == "service_activated"


def test_materialized_flag_cannot_skip_ahead_before_payment():
    """物化事实只在 commercial basis 成立之后才有意义。

    没有这一条,「还没收款就服务中」是可达的。
    """
    view = project(
        session_status="confirmed", pricing_snapshot=V2_SNAPSHOT,
        has_durable_activation=True, activation_materialized=True,
    )
    assert view.milestone == "customer_accepted"


# --------------------------------------------------- ACT-01:legacy 零漂移 / 分流键

def test_legacy_snapshot_gets_no_milestone_view():
    assert isinstance(
        project(session_status="active", pricing_snapshot=LEGACY_SNAPSHOT), NotEnrolled
    )


def test_client_supplied_mode_cannot_enroll():
    """§3.2 L306:分流键必须来自服务端 snapshot schema,**禁信客户端 mode**。

    这条是安全判据:请求体里塞 mode=defensive 不得让 legacy 报价获得 v2 语义。
    """
    for forged in (
        {"mode": "defensive"},
        {"campaign_mode": "hybrid"},
        {"delivery_plan": {"campaign_mode": "defensive"}},
        {"delivery_plan": {"schema_version": "geo-delivery-plan-v2"}},
    ):
        assert isinstance(
            project(session_status="active", pricing_snapshot=forged), NotEnrolled
        ), forged


def test_unknown_session_status_degrades_to_draft_not_crash():
    view = project(session_status="some_future_state", pricing_snapshot=V2_SNAPSHOT)
    assert view.milestone == "draft"


# ------------------------------------------------- U-3 / U-2:对客文案与进度条

def test_progress_bar_is_exactly_the_five_signed_labels():
    """§0.5.5 U-3 L131 逐字五格。前端不得自造,所以服务端必须下发这一组。"""
    assert progress_steps() == (
        "报价已发", "客户已同意", "待核单", "已收款", "服务中",
    )


def test_internal_enum_names_never_reach_the_label():
    """U-3 L132:``commercial_basis_established`` 等内部名禁上屏。"""
    for milestone in MILESTONE_ORDER:
        label = user_label(milestone)
        assert milestone not in label
        assert not re.search(r"[a-z]+_[a-z_]+", label), (milestone, label)


def test_unknown_milestone_raises_instead_of_echoing_the_raw_string():
    """回退成原串 = 内部枚举裸串上屏 = 验收红。所以必须抛。"""
    with pytest.raises(ValueError):
        user_label("totally_unknown_state")


def test_every_milestone_has_exactly_one_next_action():
    for milestone in MILESTONE_ORDER:
        view = project(
            session_status=next(
                (s for s, m in {
                    "quoted": "offer_sent", "confirmed": "customer_accepted",
                    "pending_payment": "sales_validated",
                    "active": "commercial_basis_established",
                }.items() if m == milestone), "unknown_state"),
            pricing_snapshot=V2_SNAPSHOT,
        )
        assert view.next_action_kind
        assert view.next_action_label


def test_customer_confirm_notice_is_verbatim():
    """U-3 L133:这句话替销售挡住「我确认了然后呢」,是产品承诺不是装饰。"""
    assert customer_confirm_notice() == (
        "已确认。您的服务商将与您完成收款对账后开始服务"
    )


def test_transition_table_has_no_unreachable_or_dangling_state():
    """状态机自洽:除起点外每个态都可达,且目标态全在全集内。"""
    reachable = {"draft"}
    for targets in ALLOWED_TRANSITIONS.values():
        reachable |= targets
    assert reachable == set(MILESTONE_ORDER)
    for state, targets in ALLOWED_TRANSITIONS.items():
        assert state in MILESTONE_ORDER
        for t in targets:
            assert t in MILESTONE_ORDER, (state, t)


# ------------------------------------------------------------- 迁移 042 · 真库

def test_migration_is_dml_free():
    """prestart 每次部署**无条件重放**全部迁移 —— 体内 DML 是常驻地雷。"""
    text = MIGRATION_042.read_text(encoding="utf-8")
    dml = re.findall(
        r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|COPY)\s", text, re.I | re.M
    )
    assert dml == [], dml


def test_dml_detector_actually_detects_dml():
    """上一条的**反向对照**:同一把尺子量一个确实含 DML 的迁移必须非零。

    没有这条,「0 条 DML」和「正则根本没在工作」长得一模一样。
    """
    known_dml = ROOT / "scripts" / "migration_organization_all_accounts_onboarding_2026_07_22.sql"
    if not known_dml.exists():              # pragma: no cover
        pytest.skip("反向对照样本不在本树")
    hits = re.findall(
        r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|COPY)\s",
        known_dml.read_text(encoding="utf-8", errors="replace"), re.I | re.M,
    )
    assert len(hits) > 0


def test_migration_is_registered_in_manifest():
    """没登记 = 生产永远不会跑它(本仓踩过:迁移没进 manifest 就是没接线)。"""
    src = re.sub(r"#.*$", "", (ROOT / "db" / "migration_manifest.py").read_text(
        encoding="utf-8"), flags=re.M)
    body = re.search(r"MIGRATIONS\s*=\s*\[(.*?)^\]", src, re.S | re.M).group(1)
    assert MIGRATION_042.name in body


def test_migration_replays_idempotently(schema_loaded):
    """再跑一遍必须不炸 —— prestart 会重放。"""
    conn = psycopg2.connect(schema_loaded)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_042.read_text(encoding="utf-8"))
            cur.execute(MIGRATION_042.read_text(encoding="utf-8"))
    finally:
        conn.close()


def test_own_snapshot_pointer_is_accepted(conn, probe_rows):
    """活性自证:合法指向必须成功。它红了,下面三条「必须拒」就没有区分力。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE keyword_selection_sessions SET customer_confirmed_snapshot_id=%s,"
            " customer_confirmed_snapshot_hash=repeat('a',64),"
            " customer_confirmed_at=now() WHERE id=%s",
            (probe_rows["snap_a"], probe_rows["session_a"]),
        )
        assert cur.rowcount == 1


def test_cross_quote_snapshot_pointer_is_rejected(conn, probe_rows):
    """🔴 ACT-08/09 的核心:不能把 A quote 的 snapshot 指给 B quote。

    注意 ``snapshot_hash`` **挡不住**这一格 —— 它只覆盖两个 JSONB payload,
    不覆盖 quote_id(services/quote_pricing_snapshot.py:65-67)。
    挡住它的是复合外键。所以这条判据打的是 FK,不是 hash。
    """
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            cur.execute(
                "UPDATE keyword_selection_sessions SET customer_confirmed_snapshot_id=%s,"
                " customer_confirmed_snapshot_hash=repeat('b',64),"
                " customer_confirmed_at=now() WHERE id=%s",
                (probe_rows["snap_b"], probe_rows["session_a"]),
            )


def test_half_state_pointer_without_audit_is_rejected(conn, probe_rows):
    """ACT-07「任一步异常均无可见半状态」在库层的那一半。"""
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "UPDATE keyword_selection_sessions"
                " SET customer_confirmed_snapshot_id=%s WHERE id=%s",
                (probe_rows["snap_b"], probe_rows["session_b"]),
            )


def test_malformed_confirmed_hash_is_rejected(conn, probe_rows):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "UPDATE keyword_selection_sessions SET customer_confirmed_snapshot_id=%s,"
                " customer_confirmed_snapshot_hash=%s, customer_confirmed_at=now()"
                " WHERE id=%s",
                (probe_rows["snap_b"], "NOTAHASH", probe_rows["session_b"]),
            )


def test_composite_fk_target_uniqueness_still_exists(conn):
    """复合 FK 立得住的前提 = 目标表那条 UNIQUE(id, quote_id) 还在。

    有人日后删掉它,FK 会先炸;这条判据让原因一眼可读,
    而不是留一个「迁移莫名其妙建不起来」的谜。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_constraint WHERE conrelid='quote_pricing_snapshots'::regclass"
            " AND contype='u' AND pg_get_constraintdef(oid)='UNIQUE (id, quote_id)'"
        )
        assert cur.fetchone() is not None


PROTECTED_FILES = (
    "middleware/billing.py", "db/connection.py", "auth/middleware.py",
    "auth/jwt_utils.py", "config/pricing_config.py",
    "tools/transparent_pricing.py", "db/wallet_db.py",
)
PROTECTED_BASE_SHA = "bb7a7c2ce"


def changed_protected_files(base: str = PROTECTED_BASE_SHA, *, env=None) -> str:
    """对 ``base`` 有 diff 的保护文件清单(空串 = 零 diff)。

    🔴 [2026-08-29 · Review 照准] 抽出来是为了**可判**:原来这段直接内联在判据里,
       只断言 ``out.stdout.strip() == ""`` 而**不看 returncode** ——
       git 一失败(二进制不在 / base 取不到 / PATH 被换)``stdout`` 就是空串,
       断言照样通过,而它声称守的是「七个保护文件零 diff」。

       这不是理论:Codex fix-of-fix2 的容器轮里,它的 offline git shim 没预置
       这条 ``diff --name-only bb7a7c2ce``,落进末尾 ``exit 2`` 的空输出分支,
       **这一格在那一整轮里是空转的**。同一个文件里 w3 的同类判据都写了
       ``assert returncode == 0``,在同样条件下正确地红了 —— 一件事两种写法,
       一种守住一种没有。

       抽成函数之后,「git 跑不起来必须红」这件事本身也能被正样本直接打
       (见 ``test_the_protected_file_check_goes_red_when_git_cannot_run``)。
    """
    import subprocess

    out = subprocess.run(
        ["git", "diff", "--name-only", base, "--", *PROTECTED_FILES],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, (
        f"git 跑不起来(rc={out.returncode}),这一格没有任何东西在守:"
        f"{(out.stderr or '').strip()[:300]}")
    return out.stdout.strip()


#: 已授权的保护文件改动。**授权是一笔一授,不是永久解锁** ——
#: 授权用完(billing.py 回到零 diff)这条也会红,逼人显式收回。
#: 🔴 [#106b · 2026-09-06] 第二笔授权进集:Owner 2026-09-06 在 C 窗口亲口批准三项(#106b db/wallet_db.py 删 keyword_expand 种子行 / #120 待收款提醒写库 / D2-b 自动窄门)。
#:    顺序按 git 输出(字典序)。第三个文件被改、或这两个之一改回不动,都红。
AUTHORIZED_PROTECTED_CHANGES = ("db/wallet_db.py", "middleware/billing.py")
OWNER_AUTHORIZATION_SHA256 = "b673b188ff01e377a5cc173bf635f21b1162819d47b4c19f2118297a6e30d9bf"


def test_seven_protected_files_match_the_authorized_change_set():
    """七保护文件的改动集必须**恰好等于已授权的那一个**。

    🔴 [2026-09-05] 本判据原为「零 diff」。`middleware/billing.py` 因 #79 信封被改,
      该改动**经 Owner 亲口授权**(`C:/AI-Test/OWNER_AUTHORIZATIONS_2026-09-05.md`,
      sha256 `b673b188ff01e377…`;Owner 原话:「billing.py 这笔我批了」)。

    🔴 处理方式是**收窄而不是放宽**:
      · 不删这把锁、不改成「随便改」;
      · 断言从「集合为空」变成「集合恰等于 {middleware/billing.py}」——
        **其余六个文件任一被改,仍然停车**;
      · 连 billing.py **不再被改** 也会红(集合变空)⇒ 授权用完后必须显式收回,
        不会有一条「曾经批过所以永远敞着」的门留在这里。

    授权文件明写:本授权**不跨班**、**不解锁另外六个**。下一班若又动 billing.py,
    需要新的一句,并且要再回来改这条断言 —— 那正是它该有的摩擦。
    """
    changed = tuple(x for x in changed_protected_files().split(chr(10)) if x.strip())
    assert changed == AUTHORIZED_PROTECTED_CHANGES, (
        "保护文件改动集 %r != 已授权集 %r · 授权:Owner 原话「billing.py 这笔我批了」"
        " · 授权文件 sha256 %s" % (changed, AUTHORIZED_PROTECTED_CHANGES,
                                  OWNER_AUTHORIZATION_SHA256[:16]))


def test_the_protected_file_check_goes_red_when_git_cannot_run():
    """🔴 正样本 —— 点名规则:git 跑不起来时,这一格**必须红**,不许读成「干净」。

    毒:给一个 git 一定认不出的 base。它的表现与「git 二进制不在」逐字同形 ——
    **非零退出 + 空 stdout** —— 而空 stdout 正是旧断言读成「干净」的那个值。
    走的是**同一个函数**,不是复刻一遍断言。

    为什么用坏 base 而不是只用 PATH 注毒:坏 base 在每个平台上都确定成立,
    PATH 注毒在 Windows 上未必生效(``subprocess`` 解析可执行文件时不一定看
    传进去的 ``env["PATH"]``)—— 那样这条正样本会在半数环境里静默 skip,
    而**会 skip 的正样本守不住任何东西**。PATH 那一路作为第二毒放在下一条。
    """
    with pytest.raises(AssertionError) as exc:
        changed_protected_files("zzzz-not-a-commit-zzzz")
    assert "git 跑不起来" in str(exc.value)


def test_the_protected_file_check_goes_red_when_git_binary_is_poisoned(tmp_path):
    """第二毒:PATH 前面塞一个必失败的 ``git``(Review 点名的那一路)。

    先自证毒真的生效,没生效就 skip —— 但**上一条不依赖它**,
    所以这里 skip 掉也不会让「git 失败必须红」这件事失去守卫。
    """
    import os
    import stat
    import subprocess

    fake = tmp_path / ("git.bat" if os.name == "nt" else "git")
    fake.write_text("@echo off\r\nexit /b 3\r\n" if os.name == "nt"
                    else "#!/bin/sh\nexit 3\n", encoding="utf-8", newline="")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    env = dict(os.environ)
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")

    probe = subprocess.run(["git", "--version"], cwd=ROOT, env=env,
                           capture_output=True, text=True)
    if probe.returncode == 0:
        pytest.skip("PATH 注毒没生效(本平台解析 git 不看传入的 env PATH)")

    with pytest.raises(AssertionError) as exc:
        changed_protected_files(env=env)
    assert "git 跑不起来" in str(exc.value)
