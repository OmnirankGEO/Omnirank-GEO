# -*- coding: utf-8 -*-
"""WO_222-c0 · 普通账号(L0)不再被品牌/档案额度卡住。

Owner 原话(2026-09-15 13:27 北京):「之前有一段时间说的普通账户只能自己用,
后面想着普通账号也能给别人代运营,普通账号结果权限全部被关闭了……
除了经营后台,其他的权限应该和服务商一样才对。」

触发:真客户把一个客户认证成「我的品牌」后,**再也建不了第二个客户做体检报告**。
"""
from __future__ import annotations

import ast
import asyncio
import io
import pathlib
import types
import uuid

import pytest
from fastapi import HTTPException

REPO = pathlib.Path(__file__).resolve().parents[2]

L0_USER = 992201          # 普通账号(agent_level = 0)
AGENT_OWNER = 992202      # 服务商(组织 owner)
EMPLOYEE = 992203         # 该服务商雇的员工(自己的 agent_level 恒 0)


def _req(user_id: int, *, is_admin=False, principal_user_id=None,
         org_id=None, membership_id=None):
    """最小假 Request。端点只读 state.user / organization_identity / organization_brand_ids。"""
    org = None
    if principal_user_id is not None:
        #: 🔴 `add_client` 读四个属性:is_member / principal_user_id /
        #:   organization_id / membership_id,最后两个要往
        #:   `organization_brand_assignments` 里写(有外键)。
        #:   第一版夹具只给了前两个 ⇒ AttributeError,红在 brand_api:1212,
        #:   读起来像「本单改坏了」,其实是**夹具不像生产**
        #:   (本仓 an-unrealistic-fixture-hides-the-defect-the-poison-should-catch)。
        org = types.SimpleNamespace(is_member=True,
                                    principal_user_id=principal_user_id,
                                    organization_id=org_id,
                                    membership_id=membership_id)
    return types.SimpleNamespace(state=types.SimpleNamespace(
        user={"user_id": user_id, "is_admin": is_admin, "agent_level": 0},
        organization_identity=org,
        organization_brand_ids=[],
    ))


@pytest.fixture
def world(db):
    """三个用户 + 一个真组织(角色/成员齐),用后即删。

    🔴 组织三张表**都要真造**:`add_client` 的员工支会往
    `organization_brand_assignments` 写一行,而那张表对 organization_id /
    membership_id 有外键。夹具只给假 id,红出来的是外键错,
    读起来像「本单改坏了」—— 夹具得长成生产那样。
    """
    tag = uuid.uuid4().hex[:8]
    ctx = {"tag": tag}
    with db.cursor() as cur:
        for uid in (L0_USER, AGENT_OWNER, EMPLOYEE):
            cur.execute(
                "INSERT INTO users (id, username, password_hash, display_name, is_active) "
                "VALUES (%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
                (uid, "wo222_%d_%s" % (uid, tag), "WO222-%d" % uid))
        cur.execute(
            "INSERT INTO organizations (owner_user_id, creation_request_id, name) "
            "VALUES (%s,%s,%s) RETURNING id",
            (AGENT_OWNER, "wo222-org-%s" % tag, "WO222 组织 %s" % tag))
        ctx["org_id"] = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO organization_roles (organization_id, code, name, created_by_user_id) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (ctx["org_id"], "operator_%s" % tag, "操作员", AGENT_OWNER))
        role_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO organization_memberships (organization_id, user_id, role_id) "
            "VALUES (%s,%s,%s) RETURNING id",
            (ctx["org_id"], EMPLOYEE, role_id))
        ctx["membership_id"] = int(cur.fetchone()["id"])
    yield ctx
    with db.cursor() as cur:
        cur.execute("DELETE FROM organization_brand_assignments WHERE organization_id = %s",
                    (ctx["org_id"],))
        cur.execute("DELETE FROM organization_memberships WHERE organization_id = %s",
                    (ctx["org_id"],))
        cur.execute("DELETE FROM organization_roles WHERE organization_id = %s",
                    (ctx["org_id"],))
        cur.execute("DELETE FROM organizations WHERE id = %s", (ctx["org_id"],))
        cur.execute("DELETE FROM brands WHERE owner_user_id = ANY(%s)",
                    ([L0_USER, AGENT_OWNER, EMPLOYEE],))
        cur.execute("DELETE FROM users WHERE id = ANY(%s)",
                    ([L0_USER, AGENT_OWNER, EMPLOYEE],))


def _add_client(brand_mod, name, user_id, *, principal_user_id=None,
                org_id=None, membership_id=None):
    req_model = brand_mod.AddClientRequest(name=name, industry="租车", city="深圳")
    return asyncio.run(brand_mod.add_client(
        req_model, _req(user_id, principal_user_id=principal_user_id,
                        org_id=org_id, membership_id=membership_id)))


# ══════════════════════════════════════════════════════════════════
# 1. 主判据:真函数打真库
# ══════════════════════════════════════════════════════════════════
def test_l0_can_create_a_second_and_third_client(apis, world):
    """🔴 解封秦老师的那一条:L0 建第 2、3 个客户必须成功。

    旧行为:`agent_level < 1` 且已有 client/legacy 品牌 ≥1 ⇒ 402 UPGRADE_REQUIRED。
    这里用**三个不同名字**建三次 —— 同名会走合并路径,合并在旧代码里也是放行的,
    拿同名去测等于测了一条旧代码本来就不拦的路(本仓 poisons-prove-teeth-not-reachability)。
    """
    brand_mod, _ = apis
    ids = []
    for i in (1, 2, 3):
        try:
            resp = _add_client(brand_mod, "曜的客户%d_%s" % (i, world["tag"]), L0_USER)
        except HTTPException as e:
            pytest.fail("L0 建第 %d 个客户被拦:%s %s" % (i, e.status_code, e.detail))
        bid = resp.get("brand_id") or resp.get("id")
        assert bid, "第 %d 次建档没回 brand_id:%s" % (i, list(resp)[:8])
        ids.append(int(bid))
    assert len(set(ids)) == 3, (
        "三次建档只产生了 %d 个不同品牌 %s —— 有人被悄悄合并到已有那条上了,"
        "而调用方看到的是成功" % (len(set(ids)), ids))


def test_all_three_clients_are_visible_in_the_list(apis, world):
    """建出来还要**看得见** —— 建成功但列表吞掉,对用户是同一件事。"""
    brand_mod, _ = apis
    made = set()
    for i in (1, 2, 3):
        resp = _add_client(brand_mod, "曜可见性%d_%s" % (i, world["tag"]), L0_USER)
        made.add(int(resp.get("brand_id") or resp.get("id")))
    listed = asyncio.run(brand_mod.list_my_clients(
        _req(L0_USER), page=1, page_size=200, show_all=False, search="", include_test=True))
    items = listed.get("clients")
    assert items is not None, "响应里没有 clients 键:%s" % list(listed)[:8]
    seen = {int(x["id"]) for x in items}
    missing = made - seen
    assert not missing, (
        "这些刚建出来的客户在 L0 自己的列表里看不到:%s —— "
        "检查 brand_type 过滤是否吞了 L0 的 client 品牌" % sorted(missing))


def test_l0_second_profile_does_not_overwrite_the_first(apis, world, db):
    """🔴🔴 这条不在工单里,是我读代码时发现的 —— **旧代码不是只挡,是覆盖**。

    旧 `api_create_profile` 命中 L0 额度时,并不总是返 402:它会
    `update_profile(最近那条档案的 id, **本次提交的数据)`,
    然后返回 `success: True / reused: True / l0_single_profile_update: True`。

    也就是说 L0 给第二个客户建档,会把**第一个客户的档案内容原地冲掉**,
    而调用方、前端、用户看到的都是「建好了」。
    402 至少会挡住;这一条是**静默数据损坏**,比 402 更难发现。
    """
    brand_mod, profile_mod = apis
    b1 = int(_add_client(brand_mod, "覆盖测试甲_%s" % world["tag"], L0_USER)["brand_id"])
    b2 = int(_add_client(brand_mod, "覆盖测试乙_%s" % world["tag"], L0_USER)["brand_id"])

    p1 = asyncio.run(profile_mod.api_create_profile(
        profile_mod.ProfileCreate(name="甲档案_%s" % world["tag"], brand_id=b1, business="甲的生意"),
        _req(L0_USER)))
    p2 = asyncio.run(profile_mod.api_create_profile(
        profile_mod.ProfileCreate(name="乙档案_%s" % world["tag"], brand_id=b2, business="乙的生意"),
        _req(L0_USER)))

    assert not p2.get("l0_single_profile_update"), (
        "第二个档案走了 L0 单档案覆盖路径 —— 甲的档案已被乙的数据冲掉")
    #: profile_id 是 shortuuid 字符串(实测 "296eb525"),不是整数 —— 别按类型想当然
    assert str(p1["profile_id"]) != str(p2["profile_id"]), (
        "两个客户拿到了同一个 profile_id(%s)—— 第二次是覆盖不是新建" % p1["profile_id"])

    with db.cursor() as cur:
        cur.execute("SELECT business FROM client_profiles WHERE id = %s",
                    (str(p1["profile_id"]),))
        row = cur.fetchone()
    assert row and row["business"] == "甲的生意", (
        "甲的档案内容变成了 %r —— 被第二次建档覆盖了" % (row and row["business"]))


def test_employee_path_still_attributes_to_the_principal(apis, world, db):
    """员工代 owner 建客户:品牌仍落在**商业主体**名下,不因撤额度而回退。

    额度没了,但归属这一半的逻辑还活着(`organization_identity.principal_user_id`)。
    撤一样东西时,最容易顺手带走挨着它的另一样 —— 本仓
    `deleting-a-branch-orphans-producers-in-files-you-never-touched`。
    """
    brand_mod, _ = apis
    resp = _add_client(brand_mod, "员工代建_%s" % world["tag"], EMPLOYEE,
                       principal_user_id=AGENT_OWNER,
                       org_id=world["org_id"],
                       membership_id=world["membership_id"])
    bid = int(resp.get("brand_id") or resp.get("id"))
    with db.cursor() as cur:
        cur.execute("SELECT owner_user_id FROM brands WHERE id = %s", (bid,))
        row = cur.fetchone()
    assert row and int(row["owner_user_id"]) == AGENT_OWNER, (
        "员工建的品牌落在了 %s 名下,应落在商业主体 %s ——"
        "会变成 owner 看不见的孤儿品牌" % (row and row["owner_user_id"], AGENT_OWNER))


def test_effective_agent_level_helper_is_not_deleted():
    """工单点名保留:`effective_agent_level` 是员工继承等级的**单点**,别处还在用。

    撤额度顺手删函数,会把那些调用点一起带走。
    """
    from auth.principal_identity import effective_agent_level
    assert callable(effective_agent_level)


# ══════════════════════════════════════════════════════════════════
# 2. 字面量腿(补充,不单独当证据)
# ══════════════════════════════════════════════════════════════════
def _fn_source(rel, name):
    src = io.open(REPO / rel, encoding="utf-8").read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return "\n".join(src.split("\n")[n.lineno - 1:n.end_lineno])
    raise AssertionError("锚过期:%s 里没有 %s" % (rel, name))


@pytest.mark.parametrize("rel,fn", [
    ("api/brand_api.py", "add_client"),
    ("api/profile_api.py", "api_create_profile"),
])
def test_neither_endpoint_can_still_emit_upgrade_required(rel, fn):
    """这两个函数体里不许再**发出** UPGRADE_REQUIRED / 402。

    🔴 只看字符串会被注释满足 —— 而本单恰好在注释里写了「原逻辑是 402 UPGRADE_REQUIRED」。
       所以用 AST:只看 `raise` 语句里的实参,注释与 docstring 天然不在语法树里
       (本仓 `a-wrong-comment-outlives-a-wrong-assertion`)。
    """
    src = _fn_source(rel, fn)
    tree = ast.parse("if True:\n" + "\n".join("    " + l for l in src.split("\n")))
    bad = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Raise) or n.exc is None:
            continue
        blob = ast.dump(n.exc)
        if "UPGRADE_REQUIRED" in blob or "402" in blob:
            bad.append(getattr(n, "lineno", "?"))
    assert not bad, ("%s::%s 仍会发出 402/UPGRADE_REQUIRED(raise 行 %s)" % (rel, fn, bad))


def test_the_silent_l0_overwrite_return_shape_is_gone():
    """`l0_single_profile_update` 这个返回标记必须在生产**代码**里彻底消失。

    它是那条**静默覆盖**路径的唯一出口标记;只要还有人能返回它,那条路就还在。

    🔴 第一版用 `git grep` 写,当场被**我自己写的注释**判红 ——
       我在 profile_api 的替换注释里引用了这个名字来说明它为什么危险。
       文本锚分不出「代码里有」和「注释里提到」,而这两件事的含义正好相反。
       改走 AST:注释根本不进语法树,docstring 单独排掉。
       同一个坑本单已经踩过一次(402 那条一开始也想用串锚)。
    """
    import subprocess
    MARK = "l0_single" + "_profile_update"   # 拆开写,免得判据文件自己被将来的文本扫描命中
    out = subprocess.run(["git", "grep", "-l", MARK, "--", "*.py"],
                         cwd=str(REPO), capture_output=True)
    cands = [l.replace(chr(92), "/") for l in out.stdout.decode("utf-8", "replace").splitlines()
             if l.strip() and not l.replace(chr(92), "/").startswith("tests/")]
    offenders = []
    for rel in cands:
        tree = ast.parse(io.open(REPO / rel, encoding="utf-8").read())
        docstrings = {id(n.value) for n in ast.walk(tree)
                      if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
        for n in ast.walk(tree):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value == MARK and id(n) not in docstrings):
                offenders.append("%s:%s" % (rel, getattr(n, "lineno", "?")))
    assert not offenders, (
        "静默覆盖路径的返回标记仍在生产代码里(非注释非 docstring):%s" % offenders)
