"""WO_211 c1 判据 · 自助调研从「仅服务商」放开到「登录即可」。

Owner 09-14:「发布中心这里的数据调研目前是需要服务商以上才能用嘛?普通用户不能用?
权限需要开一下,给他们用」。

🔴 这是一次**放开权限**的改动,所以反向对照比主臂更重要:
   放开一道闸,最容易顺手放开的是**别的闸**,而那种松动不会报错、只会在
   有人拿别人的 id 试一下时才现形。本文件里每一条"能用了"都配一条"仍然不能"。
"""
import ast
import io
import pathlib

import pytest
from fastapi import HTTPException

from .conftest import (AGENT_USER, OTHER_BRAND, OTHER_USER, PLAIN_BRAND,
                       PLAIN_USER, conn, fake_request)

API_SRC = pathlib.Path("api/research_selfserve_api.py")


def _gate(request):
    from api.research_selfserve_api import _require_logged_in_user
    return _require_logged_in_user(request)


# ═══════════════════════════════════════════════════════════════
# R1 · 普通用户过得了这道闸(主臂)
# ═══════════════════════════════════════════════════════════════
def test_a_plain_user_passes_the_gate():
    """`agent_level=0` 的登录用户不再被 403 挡住。

    夹具里这个人的 `agent_level` 是**显式写的 0**,不是靠列默认值 ——
    靠默认值凑出来的"普通用户"换一版 schema 就没了。
    """
    got = _gate(fake_request(PLAIN_USER))
    assert got == {"user_id": PLAIN_USER, "is_admin": False}


def test_an_agent_still_passes():
    """反向对照:服务商照旧能用 —— 放开不是把老用户换掉。"""
    assert _gate(fake_request(AGENT_USER))["user_id"] == AGENT_USER


def test_admin_still_marked_admin():
    """R4:admin 例外不变(读端点靠这一位放行全量)。"""
    got = _gate(fake_request(PLAIN_USER, is_admin=True))
    assert got["is_admin"] is True


def test_the_gate_no_longer_reads_agent_level():
    """闸不再查 `user_wallets.agent_level`。

    🔴 这条不是洁癖:留着那次查询,下一个人会以为返回值里有身份信息,
       于是在别处按它做判断 —— 而它已经不再是任何东西的依据。
       按**源码**验(它是一次 DB 读,行为上看不见)。
    """
    import inspect

    from api.research_selfserve_api import _require_logged_in_user
    body = inspect.getsource(_require_logged_in_user)
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith(("#", "·", "🔴"))
                     and '"""' not in l)
    assert "agent_level" not in code, "闸里还在查 agent_level"
    assert "user_wallets" not in code


# ═══════════════════════════════════════════════════════════════
# R2 · 未登录 / portal 仍然进不来(反向对照)
# ═══════════════════════════════════════════════════════════════
def test_no_login_is_still_401():
    """客户 portal / 公开 token 的形状就是「没有登录态」。"""
    with pytest.raises(HTTPException) as e:
        _gate(fake_request(None))
    assert e.value.status_code == 401


def test_broken_session_is_still_401():
    """有 user 但取不到 user_id —— 登录态异常,仍然 401,不当成匿名放行。"""
    req = fake_request(PLAIN_USER)
    req.state.user = {"is_admin": False}
    with pytest.raises(HTTPException) as e:
        _gate(req)
    assert e.value.status_code == 401


# ═══════════════════════════════════════════════════════════════
# R3 · 品牌范围一格没松(本单最重要的反向对照)
# ═══════════════════════════════════════════════════════════════
def test_a_plain_user_can_research_their_own_brand():
    from api.research_selfserve_api import _verify_brand_owner
    _verify_brand_owner(fake_request(PLAIN_USER), PLAIN_BRAND)   # 不抛即通过


def test_a_plain_user_cannot_research_someone_elses_brand():
    """🔴 放开身份闸**不等于**放开品牌闸。

    去掉 `_verify_brand_owner` 这一跳,上面那条"自己的品牌能用"照样绿 ——
    所以这一条才是承重的那根。

    🔴 码是 **404 不是 403**(工单写的 403 与实际不符,实测为准:
       `auth/brand_access.py:163,200` 两个拒绝出口都返 404「资源不存在」)。
       这不是笔误而是**纪律**:403 与 404 分开 = 把"这个品牌存在但不归你"
       告诉了调用方,那就是跨租户探测存在性的旁路。
       任务端点的 R#9 是同一条。所以这里**钉死 404** —— 有人"顺手改成 403"
       会让它红,而那正是要拦的那种改动。
    """
    from api.research_selfserve_api import _verify_brand_owner
    with pytest.raises(HTTPException) as e:
        _verify_brand_owner(fake_request(PLAIN_USER), OTHER_BRAND)
    assert e.value.status_code == 404, (
        "拒绝码从 404 变成 %s —— 那会把「存在但不归你」暴露出去"
        % e.value.status_code)
    assert str(OTHER_BRAND) not in str(e.value.detail or "")


def test_industry_level_research_without_a_brand_is_allowed():
    """`brand_id=None` 是行业级调研(`allow_null=True`),原有行为不变。"""
    from api.research_selfserve_api import _verify_brand_owner
    _verify_brand_owner(fake_request(PLAIN_USER), None)


def test_an_assigned_brand_is_reachable():
    """反向对照的反面:被**分配**的品牌可用(不是只认 owner)。

    少了它,「把品牌校验收紧成只认 owner」也能让上面两条绿,
    而那会把"被分配客户"的服务商挡在外面(GEO-R2-CAN-038 修过一次的病)。
    """
    from api.research_selfserve_api import _verify_brand_owner
    _verify_brand_owner(
        fake_request(AGENT_USER, client_brand_ids=[OTHER_BRAND]), OTHER_BRAND)


# ═══════════════════════════════════════════════════════════════
# 读端点的属主校验与身份闸**互相独立**(我加的:放开闸不能造成越权读)
# ═══════════════════════════════════════════════════════════════
_seed_seq = [0]


def _seed_task(owner_id, round_id=None):
    """种一条自助调研任务行。

    🔴 必填列按**生产 schema** 逐列补齐(user_id / industry_raw / price_points /
       idempotency_key)。手写夹具会漏掉这几个 NOT NULL,于是判据在一个
       生产上不成立的世界里全绿 —— 本包的库从 prod dump 建,所以它们当场现形。
    """
    _seed_seq[0] += 1
    n = _seed_seq[0]
    c = conn()
    try:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO geo_research_selfserve_queue
                   (user_id, industry_raw, industry_key, status, round_id,
                    price_points, idempotency_key)
               VALUES (%s,'装修','zhuangxiu','completed',%s,3900,%s) RETURNING id""",
            (int(owner_id), round_id or ("round_211_%d" % n), "idem-211-%d" % n))
        return int(cur.fetchone()["id"])
    finally:
        c.close()


def test_a_plain_user_cannot_read_another_users_task():
    """🔴 原来"非服务商进不来"可能是唯一在限制"谁能读第 N 号任务"的东西。

    实测不是:每个读端点都以调用者 user_id 为条件,
    非属主与不存在**同返 404**(不给枚举旁路)。这条把它钉住 ——
    否则放开身份闸的那天,越权读会悄悄成立。
    """
    import asyncio

    from api.research_selfserve_api import get_task

    task_id = _seed_task(OTHER_USER)
    with pytest.raises(HTTPException) as e:
        asyncio.run(get_task(task_id, fake_request(PLAIN_USER)))
    assert e.value.status_code == 404
    assert (e.value.detail or {}).get("code") == "task_not_found"


def test_the_owner_can_read_their_own_task():
    """反向对照:属主读得到 —— 否则上面那条 404 可能只是"这个端点永远 404"。"""
    import asyncio

    from api.research_selfserve_api import get_task

    task_id = _seed_task(PLAIN_USER)
    out = asyncio.run(get_task(task_id, fake_request(PLAIN_USER)))
    assert int(out["task_id"]) == task_id
    # 🔴 状态取值由建表 CHECK 定:pending/queued/running/completed/failed/timeout/cancelled,
    #    **没有 succeeded** —— 我先写的是 succeeded,被生产 schema 的 CHECK 当场拦下。
    assert out["status"] == "completed"


def test_admin_can_read_anyones_task():
    import asyncio

    from api.research_selfserve_api import get_task

    task_id = _seed_task(OTHER_USER)
    out = asyncio.run(get_task(task_id, fake_request(PLAIN_USER, is_admin=True)))
    assert int(out["task_id"]) == task_id


# ═══════════════════════════════════════════════════════════════
# R5 · 结构:改名不留别名、每个端点都走新闸
# ═══════════════════════════════════════════════════════════════
def test_no_stale_agent_gate_left_in_this_file():
    """R5:该文件里不留 `NOT_AGENT` / 旧 helper 名 / 「仅服务方」文案。"""
    src = io.open(API_SRC, encoding="utf-8").read()
    for stale in ("NOT_AGENT", "_agent_user", "仅服务方可使用自助调研"):
        assert stale not in src, "该文件里还留着 %r" % stale


def test_every_endpoint_in_this_router_uses_the_new_gate():
    """🔴 接线臂:**每一个**路由函数体内都调新闸。

    只验"helper 改对了"是不够的 —— 漏改一个端点,那个端点会变成
    「谁都能调、连登录都不查」(它原来的 401 就来自这个 helper)。
    改名时最容易漏的正是这种:helper 全绿、某个调用点还在老路上。
    """
    tree = ast.parse(io.open(API_SRC, encoding="utf-8").read())
    routed = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            f = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id == "router":
                routed.append(node)
                break
    assert len(routed) >= 6, "只找到 %d 个路由函数 —— 锚过期了" % len(routed)

    for fn in routed:
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_require_logged_in_user" in called, (
            "端点 %s 没有走登录闸 —— 它会变成谁都能调" % fn.name)


def test_both_write_endpoints_still_verify_the_brand():
    """🔴 接线臂:两个**写**端点体内都要调品牌校验。

    上面 R3 那几条是直接调 `_verify_brand_owner` 的 —— 把它从端点里
    **删掉**,那几条照样全绿(锁钉住了 helper,调用点裸奔)。
    本仓一天栽过四次这种形状,所以这一条必须单独存在。

    读端点不在此列:它们靠**任务行属主**收敛(见上面那组),不走品牌。
    """
    tree = ast.parse(io.open(API_SRC, encoding="utf-8").read())
    write_eps = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            f = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id == "router" and f.attr == "post":
                write_eps[node.name] = node
    assert len(write_eps) >= 2, "只找到 %d 个写端点 —— 锚过期了" % len(write_eps)

    for name, fn in write_eps.items():
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_verify_brand_owner" in called, (
            "写端点 %s 没有校验品牌归属 —— 普通用户能对别人的客户下单" % name)


def test_billing_path_untouched():
    """计费不动:仍是同一个价目键 + fail-closed 取价。

    放开权限最危险的顺手改是"顺便让它免费" —— 普通用户花的是自己的算力。
    """
    src = io.open(API_SRC, encoding="utf-8").read()
    assert 'FEATURE_CODE = "geo_research_selfserve"' in src
    assert "_require_priced()" in src
    assert "charge_on_success" not in src or "freeze_points" in src
