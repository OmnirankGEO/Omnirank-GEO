"""#139 · 一个没有钱包行的用户,把整个管理端用户列表拒读了。

## 现象与根因

Owner 2026-09-07 打开管理端用户设置,整页报
「用户 #174 的业务身份真相缺失,已停止读取,需先完成数据核验」。

`_assert_identity_ssot(cur, user_id=None)` 的 SQL 是
`... LEFT JOIN user_wallets w ... WHERE w.user_id IS NULL ... LIMIT 1`,
**不带 user_id 就是全表扫描**,命中任一用户即 `raise`。
列表路径(`list_admin_users`)正是不带 user_id 调它 ⇒ 一个坏行拖垮整页。

🔴 而列表**根本不需要那个前提**:它的查询本来就是 `LEFT JOIN` +
`COALESCE(w.agent_level,0)`,没有钱包行的用户照样能渲染。
守卫挡的是它自己不需要的东西。

## 🔴 产生「无钱包行用户」的路不止工单说的两条,是四条

我机械枚举后核出来的(工单只写了两条):

  1. `services/organization_onboarding.py` 邀请接受 —— 建 users 不建钱包
  2. `api/admin_api.py` admin 建号 —— 同上
  3. `db/auth_db.py:_seed_admin_user` 冷建库种子 —— 同上
  4. **自助注册本身**:`api/auth_api.py:549` 的建钱包在 `try` 里,
     `except` 只打 warning 然后继续 ⇒ 一次瞬时失败就留下无钱包用户

⇒ **靠枚举建号路径永远补不全**(第 4 条是 best-effort,第 3 条动它有
   建表时序风险)。所以承重的那半是**按行降级**,不是「把所有路都补上」:
   补路降低发生率,降级让残留不致命。

⚠️ 本文件**不**为 `_seed_admin_user` 补钱包 —— 它在 init 里跑,
   `user_wallets` 建表时序未核实;为它加调用万一撞顺序,炸的是「服务起不来」,
   比本 bug 严重得多。它在结构锁里是**带理由的冻结豁免**。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════
# 1. 结构锁:INSERT INTO users 的每一处都要有建钱包动作
# ══════════════════════════════════════════════════════════════

#: 🔴 冻结豁免。**带理由**,不是"跳过这个目录"式的宽免。
#:    新增第二个豁免必须显式改这里并写清理由。
INSERT_EXEMPTIONS = {
    "db/auth_db.py::_seed_admin_user":
        "冷建库种子;user_wallets 建表时序未核实,加调用有 init 失败风险。"
        "残留由列表按行降级兜住。",
}

#: 建钱包的规范动作 —— 全系统唯一实现在 `db/wallet_db.py`(保护文件,不改它)。
WALLET_ACTION = "get_or_create_wallet"

PRODUCTION_ROOTS = ("api", "db", "services")


def _calls_wallet_action(src_or_node) -> bool:
    """🔴 数**真调用**,不数名字出现。

    第一版用 `WALLET_ACTION in ast.unparse(fn)` —— 删掉调用之后,
    `from db.wallet_db import get_or_create_wallet` **那行 import 还在**,
    名字照样出现在源码里 ⇒ 毒下了、锁不红(Review 式毒 P2 实测 87 全绿)。

    🔴 这条我仓里早有记录(「接线锁被 import 行顶住」),今天又犯一次。
       所以改成走 AST 找 `ast.Call`,并**把 import 行排除在外**:
       只有真的调用才算数。
    """
    node = (ast.parse(src_or_node) if isinstance(src_or_node, str)
            else src_or_node)
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        if name == WALLET_ACTION:
            return True
    return False

def _sql_strings(fn) -> list:
    """取这个函数里**真正当 SQL 用**的字符串(Call 的实参),不含 docstring。

    🔴 第一版用 `ast.unparse(fn)` 整段找 —— `unparse` **保留 docstring**,
       于是 `admin_user_governance._account_origin` 里那句注释
       「全系统只有三处 INSERT INTO users」被算成了一个真的插入点。
       我在代码里写着「只算真 SQL,不算注释」,而过滤根本没做那件事 ——
       **注释声称了代码没做的事**。
    """
    out = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        for arg in list(n.args) + [k.value for k in n.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                out.append(ast.unparse(arg))
    return out


def _iter_production_functions():
    for root in PRODUCTION_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            try:
                tree = ast.parse(io.open(path, encoding="utf-8",
                                         errors="replace").read())
            except (OSError, SyntaxError):
                continue
            rel = str(path.relative_to(ROOT)).replace(chr(92), "/")
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield rel, fn


def _insert_users_sites() -> list:
    """机械枚举生产代码里**真的**执行 `INSERT INTO users` 的函数。"""
    return [("%s::%s" % (rel, fn.name), fn.name, ast.unparse(fn))
            for rel, fn in _iter_production_functions()
            if any("INSERT INTO users" in sql for sql in _sql_strings(fn))]


def _callers_of(func_name: str) -> list:
    """全仓生产代码里调用 `func_name` 的函数。"""
    out = []
    for rel, fn in _iter_production_functions():
        if fn.name == func_name:
            continue
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == func_name):
                out.append(("%s::%s" % (rel, fn.name), ast.unparse(fn)))
                break
    return out

def _reaches_wallet_action(caller_name: str, caller_src: str) -> bool:
    """调用方自己有建钱包动作,或它调用的**同文件**函数里有(一跳)。

    🔴 只允许一跳,不做全图可达 —— 全图可达会把"某处最终会建"也算成通过,
       那等于没有闸。一跳覆盖本仓真实形状(API 入口 → 收尾函数)。
    """
    if _calls_wallet_action(caller_src):
        return True
    rel = caller_name.split("::")[0]
    try:
        tree = ast.parse(io.open(ROOT / rel, encoding="utf-8",
                                 errors="replace").read())
    except (OSError, SyntaxError):
        return False
    called = {n.func.id for n in ast.walk(ast.parse(caller_src))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for fn in ast.walk(tree):
        if (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and fn.name in called and _calls_wallet_action(fn)):
            return True
    return False

def test_every_insert_into_users_creates_a_wallet_row():
    """🔴 建了用户就要建钱包行 —— 否则管理端会被这一行拖垮。

    判定范围是「同一函数,或同文件里调用它的那个函数」:
    `create_user` 自己不建钱包(它在 `db/` 层),建钱包在 API 层的调用点,
    与 `api/auth_api.py:549` 同形。所以先看本函数,再看同文件的调用方。
    """
    sites = _insert_users_sites()
    assert len(sites) >= 3, "分母只有 %d 个 INSERT INTO users —— 塌了,不是通过" % len(sites)
    offenders = []
    for name, fname, body in sites:
        if name in INSERT_EXEMPTIONS:
            continue
        if _calls_wallet_action(body):
            continue
        # 🔴 建钱包**允许在调用方**(`create_user` 在 db 层,建钱包在 API 层,
        #    与 `api/auth_api.py:549` 同形)。但要求**每一个**调用方都有 ——
        #    「有一个调用方做了」不算,漏掉的那个正是本次的病(admin 建号)。
        #    第一版只看同文件,跨文件的调用方看不见 ⇒ 误报 create_user。
        callers = _callers_of(fname)
        # 🔴 允许**一跳**:`onboard_operator` 调 `_finish_onboarding`,
        #    建钱包在后者体内。只看直接调用方的体会误报(第一版就是)。
        if callers and all(_reaches_wallet_action(n, src) for n, src in callers):
            continue
        bad = [n for n, src in callers if not _reaches_wallet_action(n, src)]
        offenders.append("%s(缺建钱包的调用方:%s)" % (name, bad or "无调用方"))
    assert not offenders, (
        "这些地方建了 users 行却没有任何 %s 动作 —— "
        "新账号会没有钱包行,管理端列表会把它标成未核验:%s" % (WALLET_ACTION, offenders))


def test_the_structural_lock_would_catch_a_new_bare_insert():
    """自证:合成一个只插 users 的函数,谓词必须抓到。"""
    fn = ast.parse("def make_user(cur):" + chr(10) +
                   "    cur.execute('INSERT INTO users(u) VALUES (%s)', (1,))"
                   ).body[0]
    assert any("INSERT INTO users" in sql for sql in _sql_strings(fn)), "锁是坏的"


def test_the_lock_does_not_count_a_mention_in_a_docstring():
    """🔴 反向对照:**注释里提到**不算插入点。这条是真实误报换来的:
    `_account_origin` 的 docstring 写着「全系统只有三处 INSERT INTO users」,
    第一版把它算成了第四处。只证「抓得到真的」证明不了「不会把注释算进来」。
    """
    fn = ast.parse("def note():" + chr(10) +
                   "    " + chr(39) * 3 + "全系统只有三处 INSERT INTO users。" + chr(39) * 3 + chr(10) +
                   "    return 1").body[0]
    assert not any("INSERT INTO users" in sql for sql in _sql_strings(fn)), (
        "把 docstring 里的提及当成了真插入点")

def test_the_exemption_list_is_not_a_blanket_skip():
    """豁免必须**逐条带理由**,而且只能是已知那一条。

    🔴 一条没有理由的豁免,和把整个检查关掉没有区别 ——
       区别只在于它看起来还在守。
    """
    assert set(INSERT_EXEMPTIONS) == {"db/auth_db.py::_seed_admin_user"}, (
        "豁免集变了 —— 新增豁免必须连同理由一起复审:%s" % sorted(INSERT_EXEMPTIONS))
    for name, reason in INSERT_EXEMPTIONS.items():
        assert len(reason) > 20, "豁免 %s 没写清理由" % name


# ══════════════════════════════════════════════════════════════
# 2. 守卫:列表按行降级,写动作仍 fail-closed
# ══════════════════════════════════════════════════════════════

def test_the_list_path_does_not_scan_the_whole_table():
    """🔴 列表路径不许再出现**不带 user_id** 的守卫调用。

    按数据流判(AST 实参个数),不按文本 —— 注释里引用那个函数名的地方本文件就有,
    文本判定会被自己的注释骗过去。
    """
    src = io.open(ROOT / "services" / "admin_user_governance.py",
                  encoding="utf-8", errors="replace").read()
    bare = [n.lineno for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_assert_identity_ssot" and len(n.args) == 1]
    assert not bare, (
        "又出现了不带 user_id 的全表守卫(行 %s)—— 一个坏行会再次拖垮整页" % bare)


def test_the_counterparty_guard_is_still_there():
    """🔴 反向对照:守卫不许被**整个删掉**。

    🔴 订正一句错的机理:我上一版写「`_actor` 守的是**操作者**」—— **不对**。
       AST 实测它的六个调用点传进去的都是**关系对手方**:
       推荐人 / 绑定的 agent_user_id / 上游渠道账号 / admin_override 用户 / 品牌 owner。
       既不是操作者,也不是被改的那个人。
       对的锁配错的机理最难发现 —— 没人复核一条正在通过的判据。

    读路径(列表 + 详情)已按行降级;唯一保留的这处守的是
    「渲染/校验一段关系时,对手方自己身份不明」—— 那不能降级:
    把身份不明的人当成关系的一端渲染出去,等于替他签了一个他没有的身份。
    """
    src = io.open(ROOT / "services" / "admin_user_governance.py",
                  encoding="utf-8", errors="replace").read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_assert_identity_ssot"]
    assert len(calls) == 1, (
        "身份守卫调用点应恰为 1(只剩 `_actor` 内那一处),实测 %d 处:%s"
        % (len(calls), [n.lineno for n in calls]))
    actor = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_actor")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "_assert_identity_ssot" for n in ast.walk(actor)), (
        "唯一那处守卫不在 `_actor` 里 —— 对手方侧没人守了")
    # 对手方语义的正样本:`_actor` 至少有一个调用点传的是关系里的另一端
    counterparty = [ast.unparse(n) for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_actor"]
    assert any("agent_user_id" in c or "referrer" in c for c in counterparty), (
        "`_actor` 不再被用在关系对手方上 —— 机理变了,请连同 docstring 一起复审")

def test_a_wallet_less_row_is_marked_not_silently_normal():
    """🔴 缺钱包行的那一行必须标成 **unverified + attention**,不是当成普通用户。

    `COALESCE(w.agent_level,0)` 会把缺行读成 0 = 普通用户 ——
    那是**具体而错误**的答案:管理员会以为已经核过了,从此不再去看。
    """
    src = io.open(ROOT / "services" / "admin_user_governance.py",
                  encoding="utf-8", errors="replace").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "list_admin_users")
    body = ast.unparse(fn)
    assert "wallet_missing" in body, "列表没有取缺行标记"
    assert "'unverified'" in body or '"unverified"' in body, (
        "缺行没有被标成 unverified")
    assert "or wallet_missing" in body or "wallet_missing or" in body, (
        "缺行没有进 attention 分母 —— 它会安静地躺在列表里")


def test_the_attention_message_says_where_to_go():
    """报文要告诉管理员**去哪核验**,不是一句具体但无处可去的话。

    原来整页那句「需先完成数据核验」——核什么?在哪核?
    一个具体而错误(或无处可去)的消息比笼统的更坏:它让人停止追问。
    """
    src = io.open(ROOT / "services" / "admin_user_governance.py",
                  encoding="utf-8", errors="replace").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "list_admin_users")
    body = ast.unparse(fn)
    assert "尚未初始化钱包" in body, "报文没说清是什么问题"
    assert "登录一次" in body, "报文没给出下一步动作"


# ══════════════════════════════════════════════════════════════
# 3. 两条建号路径:提交后建钱包
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("rel,fname", [
    ("services/organization_onboarding.py", "_finish_onboarding"),
    ("api/admin_api.py", "admin_create_user"),
])
def test_the_two_creation_paths_create_a_wallet(rel, fname):
    """邀请接受与 admin 建号都要建钱包行(与注册路径同形)。"""
    src = io.open(ROOT / rel, encoding="utf-8", errors="replace").read()
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fname), None)
    assert fn is not None, "%s 里没有 %s —— 锚点漂了,不是通过" % (rel, fname)
    body = ast.unparse(fn)
    assert _calls_wallet_action(fn), (
        "%s::%s 没有**调用**建钱包动作(只有 import 不算)" % (rel, fname))


def test_the_invite_wallet_call_is_outside_the_transaction():
    """🔴 邀请路径的建钱包必须在 `_onboard_operator_core` **返回之后**。

    那个函数的 `with get_db()` 一直开到末尾,而 `get_or_create_wallet`
    自开连接:放事务里的话,外层一旦回滚就会留下**指向不存在用户**的钱包行
    (或当场撞 FK)。所以判它在 `_finish_onboarding` 里、不在 core 里。
    """
    src = io.open(ROOT / "services" / "organization_onboarding.py",
                  encoding="utf-8", errors="replace").read()
    tree = ast.parse(src)
    core = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_onboard_operator_core")
    assert not _calls_wallet_action(core), (
        "建钱包被放进了 `_onboard_operator_core` 的事务里 —— 外层回滚会留孤儿行")


# ══════════════════════════════════════════════════════════════
# 4. 行为臂:驱动真库,不是只看代码长什么样
# ══════════════════════════════════════════════════════════════
#
# 🔴 上面三节全是静态锁(AST / 读文件)。它们证的是**代码的形状**,
#    证不了跑起来会怎样 —— 我 2026-09-06 刚在 #113 D2-b 上栽过:
#    22 条判据 + 3 发毒全都精确,却全站在一条**不可达**的分支里。
#    所以这一节必须驱动真 `list_admin_users()`,断言打返回的数据。

import psycopg2                                          # noqa: E402

WALLET_LESS_UID = 9174          # 影射生产上的 #174


def _make_wallet_less_user(dsn: str, uid: int = WALLET_LESS_UID) -> int:
    """造一个**只有 users 行、没有 user_wallets 行**的用户 —— 生产里 #174 的形状。"""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users(id,username,display_name,is_active)"
                " VALUES (%s,%s,%s,1) ON CONFLICT (id) DO NOTHING",
                (uid, "invited_%d" % uid, "邀请进来还没初始化钱包的人"))
            cur.execute("SELECT COUNT(*) FROM user_wallets WHERE user_id=%s", (uid,))
            assert cur.fetchone()[0] == 0, "夹具没造出缺钱包的形状 —— 分母塌了"
        conn.commit()
    finally:
        conn.close()
    return uid


def test_a_wallet_less_user_does_not_break_the_whole_list(reset_governance_data):
    """🔴 **本次现象那一格**:列表必须 200 并把那一行渲染出来。

    修前:`_assert_identity_ssot(cur)` 全表扫描命中 ⇒ 整页 raise
    「用户 #174 的业务身份真相缺失」。
    """
    import os

    from services.admin_user_governance import list_admin_users

    uid = _make_wallet_less_user(os.environ["TEST_DATABASE_URL"])
    out = list_admin_users(page=1, page_size=100)

    rows = {int(u["user_id"]): u for u in out["users"]}
    assert uid in rows, "缺钱包的那一行根本没出现在列表里"
    assert len(rows) > 1, "列表只剩一行 —— 别的行被吃了"

    bad = rows[uid]
    assert bad["business_identity"] == "unverified", (
        "缺钱包被当成普通用户了 —— 那是具体而错误的答案:%r" % bad["business_identity"])
    assert bad["needs_attention"] is True, "缺钱包的行没进 attention 分母"
    assert "尚未初始化钱包" in (bad["attention_label"] or ""), bad["attention_label"]

    # 反向对照:有钱包的行不许被误伤
    normal = rows[123]
    assert normal["business_identity"] != "unverified", normal
    assert "尚未初始化钱包" not in (normal["attention_label"] or ""), normal


def test_a_wallet_less_subject_can_be_bound_after_backfill(reset_governance_data):
    """🔴 [返修三] 主体没有钱包行 ⇒ **补行后写成功**,不再直接拒。

    上一版我把这里钉成「两条写路径处置相反」的分裂。裁定(2026-09-07):
    **被治理主体**补行后继续,**关系对手方**保持严格。
    ⇒ 管理员终于能修那些「邀请进来还没初始化钱包」的账号;
      而原来的拒绝没保护任何东西 —— 它要求存在的那一行,补一下就有了。

    断言打**落库读数**(行真的被建出来),不打返回值。
    """
    import os

    import psycopg2 as _pg

    from services.admin_user_governance import change_commercial_binding

    dsn = os.environ["TEST_DATABASE_URL"]
    uid = _make_wallet_less_user(dsn)
    change_commercial_binding(
        uid, 28, expected_version=1, reason="pytest 主体补行",
        operator_user_id=1, operator_username="admin_one",
        request_id="bind-ok-%d" % uid, ip_address="127.0.0.1")
    conn = _pg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT agent_level FROM user_wallets WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            assert row is not None, "主体的钱包行没被补出来"
            assert int(row[0] or 0) == 0, "补出来的默认身份不是普通用户:%r" % (row,)
            cur.execute(
                "SELECT agent_user_id FROM customer_agent_bindings"
                " WHERE customer_user_id=%s", (uid,))
            assert cur.fetchone()[0] == 28, "绑定没写进去"
    finally:
        conn.close()


def test_a_wallet_less_counterparty_is_still_refused(reset_governance_data):
    """🔴 对手方**保持严格**:没有钱包行的人本来就不可能是服务商。

    🔴 实测订正裁定文本:真实拒绝码是 **`PROVIDER_UNAVAILABLE`**
       (「目标承接方未通过资质/权限校验」),不是 `BUSINESS_IDENTITY_SSOT_UNAVAILABLE`。
       后者是**主体**侧那条(已改成补行)。两个码不能混 —— 它们指向不同的处置:
       主体缺行是「补一下」,对手方缺行是「这个人根本不是服务商」。
       而且 `PROVIDER_UNAVAILABLE` 的文案更好:它说清了为什么拒。

    配零写入臂:被拒时不许留下半截绑定。
    """
    import os

    import psycopg2 as _pg
    import pytest as _pytest

    from services.admin_user_governance import (
        GovernanceValidationError,
        change_commercial_binding,
    )

    dsn = os.environ["TEST_DATABASE_URL"]
    conn = _pg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users(id,username,display_name,is_active)"
                " VALUES (9028,%s,%s,1) ON CONFLICT (id) DO NOTHING",
                ("provider_no_wallet", "没有钱包行的冒充服务商"))
            cur.execute("DELETE FROM user_wallets WHERE user_id=9028")
        conn.commit()
    finally:
        conn.close()
    with _pytest.raises(GovernanceValidationError) as e:
        change_commercial_binding(
            124, 9028, expected_version=1, reason="pytest 对手方",
            operator_user_id=1, operator_username="admin_one",
            request_id="bind-bad-9028", ip_address="127.0.0.1")
    assert e.value.code == "PROVIDER_UNAVAILABLE", e.value.code
    conn = _pg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM customer_agent_bindings"
                " WHERE customer_user_id=124 AND agent_user_id=9028")
            assert cur.fetchone()[0] == 0, "被拒了却留下了绑定"
    finally:
        conn.close()

# ══════════════════════════════════════════════════════════════
# 5. 🔴 HTTP 层臂:走真 router + response_model
# ══════════════════════════════════════════════════════════════
#
# 🔴 这一节是 Review 判 NO-GO 换来的。
#
#    我上一版返回 `business_identity="unverified"`,而 `schemas` 里
#    `BusinessIdentity = Literal["ordinary_user","service_provider"]`,
#    `GET /users` 带 `response_model` ⇒ 校验失败 ⇒ **HTTP 整页 500**。
#    也就是说旧现象只是换了个状态码回来。
#
#    而我上一版的行为臂直接调 `list_admin_users()` —— **在响应层之下**,
#    天生看不见 response_model。判据全站在缝的一侧,与我 09-06 在 D2-b 上
#    栽的那次同族(那次是不可达分支,这次是层)。
#
#    ⇒ 凡是「改了端点返回值形状」的修复,必须有一条**走真 HTTP 出口**的臂。


def _admin_client():
    """真 router + response_model;用中间件塞一个 admin,绕开登录链路。

    `raise_server_exceptions=False`:让 500 以**状态码**回来,
    否则 TestClient 会把它抛成异常,断言看到的是 traceback 不是现象。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.admin_user_governance_api import router

    app = FastAPI()

    @app.middleware("http")
    async def _inject_admin(request, call_next):     # noqa: ANN001
        request.state.user = {"user_id": 1, "username": "admin_one", "is_admin": True}
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_http_list_is_200_for_a_wallet_less_user(reset_governance_data):
    """🔴 本次 NO-GO 那一格:HTTP 出口必须 200,不是 500。"""
    import os

    uid = _make_wallet_less_user(os.environ["TEST_DATABASE_URL"])
    resp = _admin_client().get("/api/admin/user-governance/users?page=1&page_size=100")
    assert resp.status_code == 200, (
        "HTTP %s —— 缺钱包的行过不了 response_model(Literal 少了 unverified?):%s"
        % (resp.status_code, resp.text[:300]))
    rows = {int(u["user_id"]): u for u in resp.json()["users"]}
    assert uid in rows, "缺钱包的行没出现在 HTTP 响应里"
    assert rows[uid]["business_identity"] == "unverified", rows[uid]


def test_http_detail_is_200_for_a_wallet_less_user(reset_governance_data):
    """详情页同样 —— 管理员打不开详情就到不了能补钱包的写动作。"""
    import os

    uid = _make_wallet_less_user(os.environ["TEST_DATABASE_URL"])
    resp = _admin_client().get("/api/admin/user-governance/users/%d" % uid)
    assert resp.status_code == 200, (
        "HTTP %s —— 详情对缺钱包用户仍不可读:%s" % (resp.status_code, resp.text[:300]))
    overview = resp.json()["overview"]
    assert overview["business_identity"] == "unverified", overview
    assert "尚未初始化钱包" in (overview.get("business_identity_label") or ""), overview


def test_the_writable_identity_literal_still_refuses_unverified():
    """🔴 读可以是未知,**写不能以未知为目标**。

    如果把 "unverified" 加进共享的 `BusinessIdentity`,请求体也会开始接受它 ——
    那是把「查不出来」变成一个可以主动设置的状态,方向完全相反。
    所以读写两个 Literal **必须分开**,这条守住分家。
    """
    import pytest as _pytest
    from pydantic import ValidationError

    from schemas.admin_user_governance import (
        AdminUserListItem,
        ChangeBusinessIdentityRequest,
    )

    # 读:必须接受
    assert "unverified" in str(AdminUserListItem.model_fields["business_identity"].annotation)
    # 写:必须拒绝
    with _pytest.raises(ValidationError):
        ChangeBusinessIdentityRequest(
            business_identity="unverified", expected_version=1,
            reason="x", request_id="r" * 8)


def test_changing_identity_backfills_the_wallet_row(reset_governance_data):
    """🔴 正向锁(取代上一版钉的「缺口」):对无钱包用户改身份 ⇒ **行被建出来**。

    上一版我把「写动作没有 subject 守卫」钉成了一个缺口。Review 指出那不是缺口:
    `change_business_identity` 里本来就有
    `INSERT INTO user_wallets(user_id) ... ON CONFLICT DO NOTHING` ——
    写时补行**就是现状**,而且比「拒绝管理员去修」合理得多:
    管理员要修的往往正是这种账号,把他挡在外面不会让任何人更安全。
    """
    import os

    import psycopg2 as _pg

    from services.admin_user_governance import change_business_identity

    dsn = os.environ["TEST_DATABASE_URL"]
    uid = _make_wallet_less_user(dsn)
    change_business_identity(
        user_id=uid, business_identity="service_provider",
        expected_version=1, reason="pytest 补行",
        operator_user_id=1, operator_username="admin_one",
        request_id="req-backfill-%d" % uid, ip_address="127.0.0.1",
    )
    conn = _pg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM user_wallets WHERE user_id=%s", (uid,))
            assert cur.fetchone()[0] == 1, "改身份没有把钱包行补出来"
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 6. #141 · 建号与建钱包必须原子(不是「失败就喊」)
# ══════════════════════════════════════════════════════════════
#
# 🔴 Deploy 只读取证:用户 #174 是 09-05 **自助注册**,有 user_settings/user_roles
#    却没有钱包行 —— 断在 `api/auth_api.py:549` 那个 `try`(`except` 只打 warning
#    然后继续)。全系统恰好这一个人,而它把整个管理端用户列表拒读了两天。
#
# 🔴 修法不是「失败就 fail-loud」:那仍然留着一个窗口 ——
#    建号成功、补钱包失败、然后指望有人去看日志。
#    改成把钱包行放进 `create_user` **同一个事务**:
#    要么两者都有,要么两者都没有,中间那个状态**根本不存在**。
#    能消灭的轴就别去诊断它。


def _give_users_table_a_sequence(dsn: str) -> None:
    """把 `users.id` 补成生产那样的自增(幂等)。

    起点取当前 `MAX(id)+1`,避开种子里那些显式 id(1/2/28/102/…/300)与
    本文件用的 9174 —— 否则新建用户会撞主键,而那种失败长得像「create_user 坏了」。
    """
    import psycopg2 as _pg

    conn = _pg.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE SEQUENCE IF NOT EXISTS users_id_seq_test")
            cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM users")
            start = int(cur.fetchone()[0])
            cur.execute("SELECT setval(%s, %s, false)", ("users_id_seq_test", start))
            cur.execute(
                "ALTER TABLE users ALTER COLUMN id SET DEFAULT"
                " nextval('users_id_seq_test')")
    finally:
        conn.close()

def test_create_user_always_leaves_a_wallet_row(reset_governance_data):
    """🔴 行为臂:`create_user` 返回了 id ⇒ 那个 id **一定**有钱包行。

    驱动真函数打真库,不是看源码形状。
    """
    import os

    import psycopg2 as _pg

    from db.auth_db import create_user

    # 🔴 本包的 `users` 表与**生产不同构**:conftest 建的是
    #    `id INTEGER PRIMARY KEY`(无序列,一直用显式 id 种数据),
    #    而生产是自增。`create_user` 的 `INSERT ... RETURNING id` 因此拿不到 id。
    #    ⇒ 想驱动真 `create_user`,就得先把这一维补成生产形状。
    #    这不是给判据开后门:补的是**夹具缺的那一维**,
    #    补完之后跑的仍然是生产那段代码。
    #    (显式 id 的插入不受影响,序列只对省略 id 的插入生效。)
    _give_users_table_a_sequence(os.environ["TEST_DATABASE_URL"])

    uid = create_user(
        username="atomic_probe_%d" % WALLET_LESS_UID,
        password="Pw!23456", display_name="原子性探针",
        role_ids=None, client_brand_ids=None, must_change_password=1)
    assert uid, "夹具没建出用户 —— 分母塌了,不是通过"
    conn = _pg.connect(os.environ["TEST_DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT agent_level FROM user_wallets WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            assert row is not None, (
                "create_user 返回了 user_id 却没有钱包行 —— "
                "这正是 #174 的形状:有用户、没钱包、管理端整页拒读")
            assert int(row[0] or 0) == 0
    finally:
        conn.close()


def test_the_wallet_insert_is_inside_the_same_transaction():
    """🔴 结构臂:钱包 INSERT 必须在 `conn.commit()` **之前**、与 users 行同一个 try。

    这条才是「不可能被吞」的来源。放到 commit 之后(或另开连接)的话,
    行为臂在顺利路径上照样绿 —— 而那正是 #174 走的那条路:
    每一步都成功过,只是不在一个事务里。
    """
    import ast as _ast
    import io as _io

    src = _io.open(ROOT / "db" / "auth_db.py", encoding="utf-8").read()
    fn = next(n for n in _ast.walk(_ast.parse(src))
              if isinstance(n, _ast.FunctionDef) and n.name == "create_user")
    ins_line = None
    for n in _ast.walk(fn):
        if isinstance(n, _ast.Call):
            for arg in n.args:
                if (isinstance(arg, _ast.Constant) and isinstance(arg.value, str)
                        and "INSERT INTO user_wallets" in arg.value):
                    ins_line = n.lineno
    commit_lines = [n.lineno for n in _ast.walk(fn)
                    if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
                    and n.func.attr == "commit"]
    assert ins_line is not None, "create_user 里没有建钱包的 INSERT"
    assert commit_lines, "create_user 里找不到 commit —— 锚点假设变了,请重锚"
    assert ins_line < min(commit_lines), (
        "钱包 INSERT 在 commit 之后(行 %s vs %s)—— 不再是同一个事务,"
        "「有用户没钱包」那个窗口回来了" % (ins_line, commit_lines))


def test_the_atomicity_probe_would_notice_an_insert_after_commit():
    """结构臂自证:喂一段「先 commit 再插钱包」的合成源码,谓词必须判它违规。"""
    import ast as _ast

    bad = ("def create_user():" + chr(10) +
           "    cur.execute('INSERT INTO users(u) VALUES (1)')" + chr(10) +
           "    conn.commit()" + chr(10) +
           "    cur.execute('INSERT INTO user_wallets(user_id) VALUES (1)')" + chr(10))
    fn = _ast.parse(bad).body[0]
    ins = max(n.lineno for n in _ast.walk(fn)
              if isinstance(n, _ast.Call)
              for a in n.args
              if isinstance(a, _ast.Constant) and isinstance(a.value, str)
              and "INSERT INTO user_wallets" in a.value)
    com = min(n.lineno for n in _ast.walk(fn)
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
              and n.func.attr == "commit")
    assert ins > com, "合成样本本身就不违规 —— 自证是假的"


# ══════════════════════════════════════════════════════════════
# 7. 漂移锁:「建默认钱包行」这条 SQL 在全仓有多份
# ══════════════════════════════════════════════════════════════

import os as _os                                          # noqa: E402
import re as _re                                          # noqa: E402

#: 🔴 意图**不同**的那一处,显式排除并写明理由(不是"跳过这个文件"式的宽免)。
#:    `consume_invite_code` 插的是 `agent_level` / `agent_tier` 且走 `DO UPDATE` ——
#:    那是**提升身份**,不是建默认行。把它拉进来"统一"会把提升语义抹掉。
WALLET_INSERT_EXCLUDED = {
    "services/identity_service.py::consume_invite_code":
        "插 agent_level/agent_tier 且 DO UPDATE = 提升身份,不是建默认行。",
}

#: `db/wallet_db.py` 是**保护文件**,本窗口不改它。它的语句是默认建行
#: 再加一个 `RETURNING *`(它要把行读回去)。允许这一处差异,但**只允许这一处**。
WALLET_INSERT_RETURNING_SITE = "db/wallet_db.py::get_or_create_wallet"

WALLET_SCAN_ROOTS = ("api", "db", "services", "workflows", "middleware", "agents", "tools")


def _normalise_sql(text: str) -> str:
    t = _re.sub(r"\s+", " ", text).strip()
    return _re.sub(r"\s*([(),])\s*", r"\1", t)


def _wallet_insert_sites() -> dict:
    """全仓**真的**执行 `INSERT INTO user_wallets` 的地方 → 归一化后的 SQL。

    只看 Call 的字符串实参 —— 注释/docstring 里提到这串的地方本文件就有好几处,
    整段 `ast.unparse` 会把它们算进来(#139 里我已经栽过一次)。
    """
    out = {}
    for root in WALLET_SCAN_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                src = io.open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if "INSERT INTO user_wallets" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            rel = str(path.relative_to(ROOT)).replace(_os.sep, "/")
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for n in ast.walk(fn):
                    if not isinstance(n, ast.Call):
                        continue
                    for a in n.args:
                        if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                                and "INSERT INTO user_wallets" in a.value):
                            out["%s::%s" % (rel, fn.name)] = _normalise_sql(a.value)
    return out


def test_every_default_wallet_insert_is_byte_identical():
    """🔴 「建默认钱包行」这条 SQL 全仓有 **5** 份,必须逐字相同。

    `db/wallet_db.py` 是保护文件不能动,所以做不到「一处实现」——
    代价由这条漂移锁承担:任一处被改(比如将来多插一列)会当场红并**点名其余几处**,
    把「静默分家」换成「响的失败」。

    ⚠️ 分母比工单说的宽:我机械枚举出 **7** 处、3 种形状,
       其中 `api/trial_pass_api.py::approve_trial_pass` 是工单没提到的第 6 处。
       ⇒ 立锁前先数,别照抄别人给的份数。
    """
    sites = _wallet_insert_sites()
    assert len(sites) >= 6, "分母只有 %d 处 —— 塌了,不是通过:%s" % (len(sites), sorted(sites))
    default_shape = {k: v for k, v in sites.items()
                     if k not in WALLET_INSERT_EXCLUDED and k != WALLET_INSERT_RETURNING_SITE}
    assert len(default_shape) >= 5, "默认建行只剩 %d 处:%s" % (
        len(default_shape), sorted(default_shape))
    shapes = set(default_shape.values())
    assert len(shapes) == 1, (
        "「建默认钱包行」出现了 %d 种写法,已经分家:\n  %s"
        % (len(shapes), "\n  ".join("%s\n      %s" % (k, v)
                                    for k, v in sorted(default_shape.items()))))


def test_the_protected_copy_differs_only_by_returning():
    """保护文件那一份:允许多一个 `RETURNING *`,**只允许这一处差异**。

    它要把行读回去,所以有 RETURNING;除此之外必须与另外 5 处逐字相同。
    """
    sites = _wallet_insert_sites()
    assert WALLET_INSERT_RETURNING_SITE in sites, (
        "%s 的那处 INSERT 不见了 —— 锚点漂了,不是通过" % WALLET_INSERT_RETURNING_SITE)
    protected = sites[WALLET_INSERT_RETURNING_SITE]
    default = next(v for k, v in sites.items()
                   if k not in WALLET_INSERT_EXCLUDED and k != WALLET_INSERT_RETURNING_SITE)
    assert protected == default + " RETURNING *", (
        "保护文件那份与其余几处的差异不止 RETURNING:\n  %s\n  %s" % (protected, default))


def test_the_excluded_site_is_named_with_a_reason():
    """排除项必须**逐条带理由**,而且只能是已知那一条。

    🔴 一条没有理由的排除,与把检查关掉没有区别 —— 区别只在于它看起来还在守。
    """
    assert set(WALLET_INSERT_EXCLUDED) == {
        "services/identity_service.py::consume_invite_code"}, sorted(WALLET_INSERT_EXCLUDED)
    for name, reason in WALLET_INSERT_EXCLUDED.items():
        assert len(reason) > 20, "排除 %s 没写清理由" % name
    # 正样本:被排除的那处**确实**是另一种形状(不是被误排的同形)
    sites = _wallet_insert_sites()
    for name in WALLET_INSERT_EXCLUDED:
        assert name in sites, "%s 的 INSERT 不见了 —— 排除项过期" % name
        assert "agent_level" in sites[name], (
            "%s 已经不是提升语义了 —— 它可能该回到统一分母里" % name)


def test_the_drift_lock_catches_an_extra_column():
    """漂移锁自证:正反两臂。

    合成一处只插 `user_id` 的 ⇒ 与现有形状相同(必绿);
    合成一处多插一列的 ⇒ 形状不同(必红)。
    只证「抓得到多插一列」证明不了「不会误伤同形的」。
    """
    sites = _wallet_insert_sites()
    canonical = next(v for k, v in sites.items()
                     if k not in WALLET_INSERT_EXCLUDED and k != WALLET_INSERT_RETURNING_SITE)
    same = _normalise_sql(
        "INSERT INTO user_wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING")
    extra = _normalise_sql(
        "INSERT INTO user_wallets(user_id, agent_level) VALUES (%s, 0)"
        " ON CONFLICT(user_id) DO NOTHING")
    assert same == canonical, "同形的被判成不同 —— 归一化太严,会误伤"
    assert extra != canonical, "多插一列没被判成不同 —— 归一化太松,漂了也不红"
