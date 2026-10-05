"""【A-4 = Codex 三审 P1-9】小榜组织席位把 actor 当 tenant owner。

坐标与病根(我先机械证伪,再修)
------------------------------
``api/xiaobang_operations_api._actor_binding`` 上一版::

    tenant_owner_id = int(getattr(context, "owner_user_id", 0) or actor_user_id or 0)

而 ``services/customer_operation_plan.AuthorizedAssistantContext`` 里
``owner_user_id`` 这个名字出现 **0 次**(机械核过:``grep -c`` = 0)——
所以那个 ``getattr`` 恒取到 0,``tenant_owner_id`` **恒等于操作者本人**。

  · 老板自己操作 ⇒ 两值本就相等 ⇒ 无感,所以上线以来没人发现;
  · **组织席位代操作** ⇒ 真租户是老板,账本却把员工冻成租户 ⇒
    intent / 执行归属整条错位,含 execute 的 ``WHERE tenant_owner_id=%s``。

代码里还自曝了这是「已知边界」并把它推给「未来 org-seat 专扫」。
🔴 **披露不等于可发车** —— 注释写得再清楚,这条链在生产上还是错的。

修法
----
``_actor_binding`` 改走 ``auth.principal_identity.resolve_principal_user_id``
—— 全仓那**一个**口径:组织员工取 ``identity.principal_user_id``,
其余(含组织 owner 本人)一律回落 ``fallback_user_id``,与个人场景完全一致。
不在这里另写一份 ``identity.principal_user_id`` 的取法。

判据形态(工单点名的两条纪律)
------------------------------
🔴 **禁止夹具自造 ``context.owner_user_id``** —— 本仓前科:第一版
   ``_frozen_reason_facts`` 就是因为判据里的假 ``_Ctx`` 有这个属性而全绿,
   生产上恒 None、接线等于没接。所以本文件的组织上下文是**真的**:
   真 organizations / organization_memberships / organization_roles 行,
   经生产解析器 ``db.organization_db.resolve_identity`` 现算出来的 IdentityContext。
🔴 **owner / employee 两个人**,不是一个人两种断言。
"""
from __future__ import annotations

import types
import uuid

import psycopg2
import pytest

from tests.defgeo_v3a_2026_08_28 import conftest as CT

pytestmark = pytest.mark.integration


def _conn(dsn):
    c = psycopg2.connect(dsn)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _seed_org(cur) -> dict:
    """造一个**真**组织:老板 + 员工 + 角色 + 两条 membership 行。

    全部走真表真列,一条都不是假对象 —— 组织身份是这条判据的被测面之一,
    夹具替它作答就等于没验。
    """
    base = 970_000 + (uuid.uuid4().int % 10_000) * 10
    owner, employee = base + 1, base + 2
    for uid, tag in ((owner, "owner"), (employee, "emp")):
        cur.execute(
            "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
            "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
            (uid, "v3a_%s_%d" % (tag, uid), "v3a_%s" % tag, "v3a_%d@example.com" % uid))
    cur.execute(
        "INSERT INTO organizations (owner_user_id, creation_request_id, name) "
        "VALUES (%s,%s,%s) RETURNING id",
        (owner, "v3a-" + uuid.uuid4().hex[:10], "V3A 组织"))
    org_id = int(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO organization_roles (organization_id, code, name, created_by_user_id) "
        "VALUES (%s,'member','成员',%s) RETURNING id", (org_id, owner))
    role_id = int(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO organization_memberships (organization_id, user_id, role_id, is_owner) "
        "VALUES (%s,%s,%s,%s)", (org_id, employee, role_id, False))
    return {"org_id": org_id, "owner": owner, "employee": employee}


def _request(user_id: int, identity=None):
    """一个只带被测代码真正会读的两样东西的请求替身:
    ``state.user``(键名逐字取自 ``auth/middleware`` 真写进去的 ``user_id``)
    与 ``state.organization_identity``。夹具发一个生产不会发的键,
    会让端点在生产必炸而判据全绿 —— 本仓 2026-08-20 实录。
    """
    state = types.SimpleNamespace(user={"user_id": int(user_id), "is_admin": False},
                                  organization_identity=identity,
                                  permission_version=None)
    return types.SimpleNamespace(state=state)


class _Ctx:
    """现役 ``AuthorizedAssistantContext`` 的**形状**替身 —— 故意**不带**
    ``owner_user_id``:生产上那个字段就是不存在的。

    判据里给它加一个,就是本仓记过的「夹具供了生产不会供的东西」,
    而那正是这个洞能活到三审的原因。
    """

    brand_id = 1
    quote_id = None


#: 当前判据正在用的库。``_resolve_real_identity`` 的健康自证要连它。
_ACTIVE_DSN = [None]


@pytest.fixture()
def org_world(chain_db):
    dsn = chain_db("a4org")
    _ACTIVE_DSN[0] = dsn
    conn = _conn(dsn)
    try:
        cur = conn.cursor()
        world = _seed_org(cur)
    finally:
        conn.close()
    import db.connection as dbconn
    old = dbconn.DATABASE_URL
    dbconn.DATABASE_URL = dsn
    dbconn._pool = None
    world["dsn"] = dsn
    yield world
    dbconn.DATABASE_URL = old
    dbconn._pool = None


def _resolve_real_identity(user_id: int):
    """用**生产解析器**从真 membership 行现算身份。

    🔴 为什么要绕过 ``assert_ready``,以及凭什么这么做
    ------------------------------------------------
    ``resolve_identity`` 开头会 ``assert_ready(cur)``,而它在本包的一次性库上
    判 not-ready。**原因与本单无关,也与防御 GEO 迁移无关** —— 我做过定向双臂:

        臂A 只灌 08-19 生产 dump、**一条**防御 GEO 迁移都不跑 → ready=False,
            schema_contract 列数 537 / 期望 536
        臂B dump + 040~054 全跑                              → ready=False,同样 537 / 536

    两臂完全一致 ⇒ 这是 **dump 与组织 schema 契约指纹之间的存量漂移**,
    不是我们这批迁移带进来的(否则臂A 会是 ready)。所以这里绕过的是一个
    **环境闸**,不是身份语义。

    绕过之前先自证「身份相关的那几面是健康的」:缺表/缺列/缺约束/缺索引
    必须全空 —— 只允许指纹计数这一项对不上。否则就不是"环境闸",
    是真的缺东西,那时候绕过去等于拿夹具的洞冤枉被测代码。
    """
    import db.organization_db as odb
    import psycopg2.extras

    conn = psycopg2.connect(_ACTIVE_DSN[0])
    conn.autocommit = True
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        health = odb.readiness(cursor=cur)
    finally:
        conn.close()
    for key in ("missing_tables", "missing_columns", "missing_constraints",
                "missing_indexes", "wrong_object_kinds", "wrong_column_types"):
        assert not health.get(key), (
            "组织 schema 真的缺东西(%s=%r)—— 这不是环境闸,不能绕:"
            "缺着东西去验身份等于拿夹具的洞冤枉被测代码" % (key, health.get(key)))
    assert health["schema_contract"]["actual_counts"]["constraints"] ==         health["schema_contract"]["expected_counts"]["constraints"], health["schema_contract"]

    real_assert_ready = odb.assert_ready
    odb.assert_ready = lambda cursor: None
    try:
        return odb.resolve_identity(int(user_id), request_id="v3a-a4")
    finally:
        odb.assert_ready = real_assert_ready


def test_a4_the_context_class_still_has_no_owner_user_id():
    """机械前提:``AuthorizedAssistantContext`` 上确实没有 ``owner_user_id``。

    这一条是**分母自证**:上面所有判据的意义都建立在"生产上那个字段不存在"之上。
    哪天有人给它加上了,这条会红 —— 那时要重新想的是「租户该从哪来」,
    而不是让下面的判据继续绿着。
    """
    from services.customer_operation_plan import AuthorizedAssistantContext

    fields = set(getattr(AuthorizedAssistantContext, "__dataclass_fields__", {}))
    assert fields, "拿不到 dataclass 字段集 —— 这条自证会变成对空气说话"
    assert "owner_user_id" not in fields, (
        "AuthorizedAssistantContext 现在有 owner_user_id 了(%r)—— "
        "本单的租户解析是按「它不存在」修的,请重新裁定" % sorted(fields))


def test_a4_an_employee_operating_binds_the_tenant_to_the_organization_principal(org_world):
    """🔴 靶心:**员工代操作**时,tenant owner 必须是**老板**,不是员工。

    组织上下文是真的:真 membership 行 + 生产解析器 ``resolve_identity`` 现算。
    """
    from api.xiaobang_operations_api import _actor_binding

    identity = _resolve_real_identity(org_world["employee"])
    assert identity is not None, "真解析器没解析出组织身份 —— 世界没造对"
    assert identity.is_member, "员工的 actor_kind 不是 member(%r)" % identity.actor_kind
    assert int(identity.principal_user_id) == org_world["owner"], (
        "真解析器给出的 principal 不是老板 —— 世界没造对,后面比什么都没意义")

    actor = _actor_binding(_request(org_world["employee"], identity), _Ctx())
    assert actor.actor_user_id == org_world["employee"], (
        "操作者应当仍是员工本人:%r" % (actor,))
    assert actor.tenant_owner_id == org_world["owner"], (
        "组织席位代操作时租户被记成了**员工**(%s)而不是老板(%s)—— "
        "intent/执行账本整条归属错位,含 execute 的 WHERE tenant_owner_id=%%s"
        % (actor.tenant_owner_id, org_world["owner"]))
    assert actor.organization_id == org_world["org_id"], actor


def test_a4_the_owner_operating_still_binds_to_themselves(org_world):
    """配对的必须不命中:**老板自己**操作时,两值仍然都是老板。

    少了它,一个「永远取 principal」的实现在个人场景下会去解析一个不存在的组织身份;
    而一个「永远取 actor」的实现(= 修之前)在这条上照样绿 ——
    所以这两条**必须同时**在,单独任何一条都分不出修没修。
    """
    from api.xiaobang_operations_api import _actor_binding

    identity = _resolve_real_identity(org_world["owner"])
    actor = _actor_binding(_request(org_world["owner"], identity), _Ctx())
    assert actor.actor_user_id == org_world["owner"]
    assert actor.tenant_owner_id == org_world["owner"], (
        "老板自己操作,租户却不是他自己:%r" % (actor,))


def test_a4_a_personal_scenario_with_no_organization_falls_back_to_the_actor(org_world):
    """个人场景(没有任何组织身份)⇒ 租户就是操作者本人,行为与改动前完全一致。

    这一条守的是**不许把个人用户也拖进组织解析** —— 那会让所有非组织用户
    的归属变成 0 或抛异常。
    """
    from api.xiaobang_operations_api import _actor_binding

    solo = 979_999
    actor = _actor_binding(_request(solo, None), _Ctx())
    assert actor.actor_user_id == solo
    assert actor.tenant_owner_id == solo, (
        "个人场景下租户应当回落成本人,实得 %r" % (actor.tenant_owner_id,))


def test_a4_every_phase_goes_through_the_one_actor_binding():
    """五阶段(prepare/confirm/execute/status/retry)必须共用**同一个** ActorBinding 解析。

    机械枚举:``ActorBinding(`` 在本模块里只许由 ``_actor_binding`` 构造。
    谁在别处自己拼一个,就有了第二份租户真相 —— 而那一份不会有人验。
    """
    import ast
    import inspect
    import textwrap

    import api.xiaobang_operations_api as mod

    src = textwrap.dedent(inspect.getsource(mod))
    tree = ast.parse(src)
    builders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and (
                    getattr(sub.func, "id", None) == "ActorBinding"):
                builders.append(node.name)
    assert builders == ["_actor_binding"], (
        "ActorBinding 在这些函数里被构造:%r —— 只许 _actor_binding 一处,"
        "别处再拼一个就是第二份租户真相" % sorted(set(builders)))

    callers = sorted({
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "_actor_binding"
    })
    assert len(callers) >= 5, (
        "只有 %d 个函数在调 _actor_binding(%r)—— 五阶段应当都走它;"
        "少了的那个阶段就是没被这次修复覆盖到的那个" % (len(callers), callers))


def test_a4_no_code_path_reads_owner_user_id_off_the_context_any_more():
    """那一行 ``getattr(context, "owner_user_id", …)`` 必须**从模块里消失**。

    留着它就是留着一条恒取默认值的死路;而"死路取到的默认值"正是这个洞的形状。
    谓词走 AST 只认代码不认注释 —— 交付文里怎么描述这段历史都不影响。
    """
    import ast
    import inspect
    import textwrap

    import api.xiaobang_operations_api as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod)))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "getattr":
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                and node.args[1].value == "owner_user_id":
            target = getattr(node.args[0], "id", None) or ast.dump(node.args[0])[:40]
            bad.append(target)
    assert not bad, (
        "还有代码在从 %r 上取 owner_user_id —— 现役 context 类上没有这个字段,"
        "取到的永远是默认值" % (bad,))


def test_a4_the_tenant_resolution_uses_the_repo_wide_principal_helper():
    """租户解析必须走 ``auth.principal_identity.resolve_principal_user_id`` 这**一个**口径。

    自己在这里写一份 ``identity.principal_user_id`` 的取法也能让上面的行为判据绿,
    但那就是同一谓词的第二份实现 —— 迟早有一份漏掉 ``is_member`` 之类的分支,
    而漏掉的那一份不会让任何判据变红(本仓「同一谓词写两处必有一处没人验」)。
    """
    import ast
    import inspect
    import textwrap

    import api.xiaobang_operations_api as mod

    fn_src = textwrap.dedent(inspect.getsource(mod._actor_binding))
    tree = ast.parse(fn_src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
             == "resolve_principal_user_id"]
    assert len(calls) == 1, (
        "_actor_binding 里 resolve_principal_user_id 的调用点不是恰好一处(%d)"
        % len(calls))
    # 🔴 谓词必须走 AST 不能用裸子串:上一版写成
    #    ``"identity.principal_user_id" not in fn_src``,结果被**我自己写在注释里
    #    引用这句话的那一行**判红(本仓「引用裁决原文会让裸串结构锁判红」)。
    #    只认代码不认注释,注释里怎么讲这段历史都不影响。
    second_impl = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "principal_user_id"
        and getattr(n.value, "id", None) == "identity"
    ]
    assert not second_impl, (
        "_actor_binding 里又出现了直接读 identity.principal_user_id —— "
        "那是同一谓词的第二份实现")
