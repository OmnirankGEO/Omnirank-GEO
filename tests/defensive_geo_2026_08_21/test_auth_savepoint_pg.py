"""第四班 ①:`get_user_permissions` 组织能力推导段的 SAVEPOINT 隔离。

被测对象 = **调用方的事务纪律**,不是推导本身。

事故形态(2026-08-21 实测,修前裸探针红):
    `derive_for_user` 内部 `except Exception: return frozenset()` 把组织表查询的
    失败吞掉 → 返回值看着完全正常(空集 = fail-closed)→ 但 PostgreSQL 侧调用方
    的事务已废 → 下一条语句才炸,炸在 `db/auth_db.py:482`
    (`SELECT ... FROM user_clients`),真凶在几十行之外。

判据分两层,缺一层都不够:
  · **确定性层**(monkeypatch 掉被吞的那一侧)—— 与本地库有没有组织表**无关**,
    永远有区分力。防的是「哪天有人把组织迁移登记进 manifest,库里有表了,
    推导不再失败,这一整组判据静默变成恒绿」。
  · **真 schema 层** —— 拿本库真实的「组织表不存在」跑真 `derive_for_user`。
    它自带前置断言:如果哪天这个库真有组织表了,它会**红**并告诉维护者
    「探针没区分力了,去重新指」,而不是悄悄绿。

反向对照(``test_poison_really_poisons``)先证明毒是真毒 —— 否则「修好了」
只是「毒没生效」。
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
import uuid

import psycopg2
import pytest

import db.auth_db as auth_db
from db.connection import get_connection

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: conftest 播的种子用户。
SEED_USER = 8401


# ══════════════════════════════════════════════════════════════════════════
# 工具:一个「像 derive_for_user 那样吞异常、却把游标毒死」的假推导
# ══════════════════════════════════════════════════════════════════════════
def _poisoning_derivation(*, poison: bool = True, grants=frozenset()):
    """复刻真 `derive_for_user` 的**契约形状**:借游标 / 内部吞异常 / 返回集合。

    🔴 这个 fake 替代的是**被调方**,不是被测代码。被测代码是
       `get_user_permissions` 里那段 SAVEPOINT 纪律,它原封不动地在跑。
    """

    calls = {"n": 0}

    def _fake(user_id, *, cursor=None):
        calls["n"] += 1
        if poison and cursor is not None:
            try:
                # 真事故里这是 `SELECT ... FROM organization_memberships`(表不存在)。
                # 这里用一个必失败且与 schema 无关的语句,保证任何环境下都毒得死。
                cursor.execute("SELECT 1 FROM __defgeo_no_such_table_ever__")
            except Exception:
                pass  # ← 真 derive_for_user 就是这么吞的
        return frozenset(grants)

    _fake.calls = calls
    return _fake


def _baseline_permissions(user_id: int) -> list[str]:
    """只走 user_roles 的那一份权限 —— 「不多给」的参照系。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT rp.module, rp.level
            FROM role_permissions rp
            JOIN user_roles ur ON rp.role_id = ur.role_id
            WHERE ur.user_id = %s
            """,
            (user_id,),
        )
        return sorted(f"{r['module']}:{r['level']}" for r in cur.fetchall())
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 反向对照:先证明毒是真毒
# ══════════════════════════════════════════════════════════════════════════
def test_poison_really_poisons_an_unguarded_transaction():
    """没有 SAVEPOINT 时,吞掉的失败**确实**会打废调用方的下一条语句。

    这条不通过就说明后面所有「修好了」都是「毒没生效」。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        _poisoning_derivation()(SEED_USER, cursor=cur)
        with pytest.raises(psycopg2.errors.InFailedSqlTransaction):
            cur.execute("SELECT 1")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# ① 裸探针:推导必失败时 get_user 不再 InFailedSqlTransaction
# ══════════════════════════════════════════════════════════════════════════
def test_get_user_survives_poisoned_derivation(monkeypatch):
    import auth.organization_module_derivation as omd

    monkeypatch.setattr(omd, "derive_for_user", _poisoning_derivation())

    user = auth_db.get_user(SEED_USER)

    assert user is not None, "种子用户不存在 —— 判据够不到被测行"
    # 真事故炸在这个键上(auth_db.py:482 的 UNION 查询)。它存在 = 那条语句跑成了。
    assert "client_brand_ids" in user
    assert isinstance(user["permissions"], list)


def test_get_user_permissions_with_borrowed_conn_survives(monkeypatch):
    """中间件软刷新走的是「借调用方 conn」这条路 —— 事故的真实入口。"""
    import auth.organization_module_derivation as omd

    monkeypatch.setattr(omd, "derive_for_user", _poisoning_derivation())

    conn = get_connection()
    try:
        cur = conn.cursor()
        perms = auth_db.get_user_permissions(SEED_USER, conn=conn)
        assert isinstance(perms, list)
        # 借出去的 conn 必须**还能用** —— 这是整条修复的意义所在
        cur.execute("SELECT 42 AS still_alive")
        assert cur.fetchone()["still_alive"] == 42
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# ② fail-closed 语义保持:推导失败只少给,不多给
# ══════════════════════════════════════════════════════════════════════════
def test_failed_derivation_grants_nothing_extra(monkeypatch):
    import auth.organization_module_derivation as omd

    monkeypatch.setattr(omd, "derive_for_user", _poisoning_derivation())

    baseline = _baseline_permissions(SEED_USER)
    got = sorted(auth_db.get_user_permissions(SEED_USER))

    assert got == baseline, (
        f"推导失败却改变了权限集合 —— fail-closed 被破坏。baseline={baseline} got={got}"
    )


def test_derivation_failure_never_yields_forbidden_modules(monkeypatch):
    """即便假推导**试图**发放后台管理面,失败路径也一个字都不许漏出去。"""
    import auth.organization_module_derivation as omd

    monkeypatch.setattr(
        omd,
        "derive_for_user",
        _poisoning_derivation(poison=True, grants=frozenset()),
    )
    got = set(auth_db.get_user_permissions(SEED_USER))
    forbidden = {"users", "roles", "audit", "settings", "ai_agents", "social"}
    assert not {p.split(":", 1)[0] for p in got} & forbidden


# ══════════════════════════════════════════════════════════════════════════
# ③ 反面:SAVEPOINT 不许把推导**本身**关掉
# ══════════════════════════════════════════════════════════════════════════
def test_successful_derivation_still_grants_through_the_savepoint(monkeypatch):
    """🔴 这条是防「我把推导整个禁掉,于是所有 fail-closed 判据都恒绿」。

    没有这条,「少给」可以用「永远不给」来满足 —— 那是把 WP6 的集成缺口
    又打回去,而且没有任何东西会因此变红。
    """
    import auth.organization_module_derivation as omd

    fake = _poisoning_derivation(poison=False, grants=frozenset({"quote:read", "quote:write"}))
    monkeypatch.setattr(omd, "derive_for_user", fake)

    got = set(auth_db.get_user_permissions(SEED_USER))

    assert fake.calls["n"] >= 1, "推导压根没被调用 —— SAVEPOINT 把它绕过去了"
    assert {"quote:read", "quote:write"} <= got, f"推导成功却没并入权限:{sorted(got)}"


def test_derivation_result_survives_the_unconditional_rollback(monkeypatch):
    """无条件 ROLLBACK TO 只回滚**库端**片段,不能把已经算出来的返回值也丢掉。"""
    import auth.organization_module_derivation as omd

    monkeypatch.setattr(
        omd,
        "derive_for_user",
        _poisoning_derivation(poison=True, grants=frozenset({"monitoring:read"})),
    )
    # 即便推导过程中毒死了游标,它**返回**的集合仍然应当被采纳:
    # fail-closed 的口径是「异常 → 空集」,不是「碰过库就作废」。
    got = set(auth_db.get_user_permissions(SEED_USER))
    assert "monitoring:read" in got


# ══════════════════════════════════════════════════════════════════════════
# ④ autocommit 连接:不许发 SAVEPOINT(事务块外非法,硬发会把好路径打红)
# ══════════════════════════════════════════════════════════════════════════
def test_autocommit_connection_takes_the_no_savepoint_path(monkeypatch):
    """🔴 这条断言的是「推导**真的跑了**」,不是「函数没崩」。

    第一版只断言「返回 list + 连接还活着」,结果变异 M4(把 `_derive_in_txn`
    写死 True)**存活**了:autocommit 下 `SAVEPOINT` 是非法语句 → 抛 → 被外层
    `except` 吞 → 推导被静默跳过。函数照样返回 list、连接照样活着,而员工的
    组织权限**一条都没发**。「没崩」和「干了活」是两回事,判据必须钉后者。
    """
    import auth.organization_module_derivation as omd

    fake = _poisoning_derivation(poison=False, grants=frozenset({"reports:read"}))
    monkeypatch.setattr(omd, "derive_for_user", fake)

    conn = get_connection()
    try:
        conn.autocommit = True
        perms = auth_db.get_user_permissions(SEED_USER, conn=conn)

        assert fake.calls["n"] >= 1, "autocommit 连接上推导压根没被调用"
        assert "reports:read" in perms, (
            f"autocommit 连接上推导结果没并入权限:{sorted(perms)} —— "
            "多半是 SAVEPOINT 在事务块外抛了,被外层 except 静默吞掉"
        )

        cur = conn.cursor()
        cur.execute("SELECT 7 AS ok")
        assert cur.fetchone()["ok"] == 7
    finally:
        try:
            conn.autocommit = False
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 真 schema 层:本库组织表真的不存在,跑真 derive_for_user
# ══════════════════════════════════════════════════════════════════════════
def _org_tables_present() -> bool:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.organization_memberships') AS t")
        return cur.fetchone()["t"] is not None
    finally:
        conn.close()


@contextlib.contextmanager
def _org_memberships_hidden():
    """在**一次性私库**里临时把 organization_memberships 改名藏起来,退出时必改回。

    生产形状库(快照 / prestart)里这张表永远在(migration_organization_internal_seats_2026_07_20 建),
    「库里没有组织表」这个自然前置已经不会再成立;按本判据原注释「改指,不许删」,改成自己造出这个条件。
    """
    conn = get_connection()
    dbname = conn.get_dsn_parameters().get("dbname", "")
    assert "test" in dbname and "prod" not in dbname, f"只许在一次性测试库上改名:{dbname!r}"
    hidden = "organization_memberships__hidden_" + uuid.uuid4().hex[:8]
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"ALTER TABLE organization_memberships RENAME TO {hidden}")
    try:
        yield
    finally:
        cur.execute(f"ALTER TABLE {hidden} RENAME TO organization_memberships")
        conn.autocommit = False
        conn.close()


def test_real_derivation_failure_does_not_break_get_user():
    """不打桩,跑真 `derive_for_user`。

    🔴 前置断言不是礼节:推导不真失败,这条判据会**变成恒绿**却什么都没验。
       生产形状库里组织表总在,所以前置由 `_org_memberships_hidden()` 造出来,
       并先证「推导真失败」(借进去的游标被毒死),再证 get_user 不受牵连。
       牙证:去掉改名 ⇒ 推导不失败 ⇒ 下面的 pytest.raises 这一格红。
    """
    import auth.organization_module_derivation as omd

    assert _org_tables_present(), "生产形状库应当有 organization_memberships —— 前置变了,重看这条探针"
    with _org_memberships_hidden():
        assert not _org_tables_present()
        # ① 推导真失败:它内部吞了异常,但借进去的游标所在事务已被毒死
        conn = get_connection()
        try:
            cur = conn.cursor()
            omd.derive_for_user(SEED_USER, cursor=cur)
            with pytest.raises(psycopg2.errors.InFailedSqlTransaction):
                cur.execute("SELECT 1")
        finally:
            conn.rollback()
            conn.close()
        # ② 同一条件下 get_user 不受牵连(SAVEPOINT 三件套的真 schema 证据)
        user = auth_db.get_user(SEED_USER)
        assert user is not None
        assert "client_brand_ids" in user, "真 schema 下仍然炸在 auth_db.py:482"
    assert _org_tables_present(), "organization_memberships 改名没还原"


# ══════════════════════════════════════════════════════════════════════════
# ⑥ 结构锚:SAVEPOINT 三件套在位 + 借游标调用点分母
# ══════════════════════════════════════════════════════════════════════════
def _get_user_permissions_source() -> str:
    import inspect

    return inspect.getsource(auth_db.get_user_permissions)


def test_savepoint_triplet_present_in_the_derivation_block():
    src = _get_user_permissions_source()
    for stmt in (
        "SAVEPOINT org_module_derivation",
        "ROLLBACK TO SAVEPOINT org_module_derivation",
        "RELEASE SAVEPOINT org_module_derivation",
    ):
        assert stmt in src, f"推导段缺 {stmt!r}"
    # ROLLBACK 必须在 RELEASE 之前:INERROR 状态下只有 ROLLBACK TO 合法
    assert src.index("ROLLBACK TO SAVEPOINT") < src.index("RELEASE SAVEPOINT")
    # 且必须在 finally 里 —— 放在 try 尾巴上,推导抛异常时就不会执行
    assert "finally:" in src.split("SAVEPOINT org_module_derivation", 1)[1]


def _swallowing_borrowed_cursor_call_sites() -> set[tuple[str, str]]:
    """机械分母:生产代码里把**借来的游标**传进「吞异常包装器」的调用点。

    轴的作用域 = 「吞异常 + 借游标」这一种形态。`resolve_identity` 直调不在轴上
    (它往上抛,调用方看得见);`cursor=None` 分支也不在(它自己 `with get_db()`)。
    """
    swallowing = {"derive_for_user", "operator_context_for"}
    skip_dirs = {"tests", "node_modules", "frontend", ".git", "docs", "scripts"}
    found: set[tuple[str, str]] = set()
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if any(rel.startswith(d + "/") for d in skip_dirs):
            continue
        if rel == "auth/organization_module_derivation.py":
            continue  # 定义处自己的内部调用,不是外部调用点
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in swallowing:
                continue
            for kw in node.keywords:
                if kw.arg == "cursor" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is None
                ):
                    found.add((rel, name))
    return found


def test_borrowed_cursor_into_swallowing_wrapper_has_exactly_one_call_site():
    """普查锁:多出第二个借游标调用点 → 红。

    它不是洁癖 —— 第二个调用点就是第二处同样的静默事务污染,而 SAVEPOINT
    只装在了第一处。这条红了要么补 SAVEPOINT,要么改这里的分母并说明为什么。
    """
    sites = _swallowing_borrowed_cursor_call_sites()
    assert sites == {("db/auth_db.py", "derive_for_user")}, (
        f"借游标调用「吞异常包装器」的点变了:{sorted(sites)}。"
        "每一个这样的点都必须自带 SAVEPOINT,否则就是又一处静默打废调用方事务。"
    )


def test_derivation_chain_stays_read_only():
    """无条件 ROLLBACK TO 的前提:推导链只读。

    这条红 = 有人让推导写库了,那么本函数的无条件回滚会**静默吞掉那些写**。
    到那时必须改成「按事务状态条件回滚」,而不是把这条判据删掉。
    """
    src = (ROOT / "auth" / "organization_module_derivation.py").read_text(encoding="utf-8")
    body = src.split("_assert_map_is_safe()", 1)[-1]
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in body.upper(), f"推导模块出现写语句 {verb!r} —— 无条件回滚会吞掉它"

    import db.organization_db as odb
    import inspect

    resolve_src = inspect.getsource(odb.resolve_identity).upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in resolve_src, f"resolve_identity 出现写语句 {verb!r}"
