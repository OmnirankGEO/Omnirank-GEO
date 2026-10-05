"""#67 · 服务商自己名下的品牌**永远看得见**,别人的照旧按 is_test 隐藏。

缺陷:名字含「测试|test|_demo|_test|验收」的品牌被自动打 `is_test=true`(4 个写入点,
本单**不动**),而 `/api/my-clients` 一律按 is_test 过滤 ⇒
服务商自己建的「测试科技有限公司」在她自己的客户列表里**凭空消失**,没有任何提示。

M3 铁律 5 的隔离目标是**平台级视图与统计**,不是服务商自己的列表 —— 本单只改可见性。
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest

AGENT_A = 990101
AGENT_B = 990102
ADMIN = 990103


def _req(user_id: int, *, is_admin: bool = False):
    """最小假 Request:端点只读 state.user / organization_identity / organization_brand_ids。"""
    return types.SimpleNamespace(state=types.SimpleNamespace(
        user={"user_id": user_id, "is_admin": is_admin},
        organization_identity=None,
        organization_brand_ids=[],
    ))


def _call(mod, user_id, *, is_admin=False, show_all=False, include_test=False):
    return asyncio.run(mod.list_my_clients(
        _req(user_id, is_admin=is_admin),
        page=1, page_size=200, show_all=show_all, search="", include_test=include_test))


def _ids(resp) -> set:
    """把响应里的品牌 id 取成集合 —— 比数量,数量相等而内容不同在计数式读数上同形。"""
    items = resp.get("clients") if isinstance(resp, dict) else None
    assert items is not None, f"响应里没有 clients 键:{list(resp)[:8]}"
    return {int(x["id"]) for x in items}


@pytest.fixture
def world(db):
    """A 名下一个 is_test 品牌 + B 名下一个 is_test 品牌。用后即删。"""
    tag = uuid.uuid4().hex[:8]
    made = {}
    with db.cursor() as cur:
        for uid in (AGENT_A, AGENT_B, ADMIN):
            cur.execute(
                "INSERT INTO users (id, username, password_hash, display_name, is_active) "
                "VALUES (%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
                (uid, f"wo67_{uid}_{tag}", f"WO67-{uid}"))
        for key, uid in (("a", AGENT_A), ("b", AGENT_B)):
            cur.execute(
                "INSERT INTO brands (name, owner_user_id, brand_type, is_test) "
                "VALUES (%s,%s,'client',TRUE) RETURNING id",
                (f"测试科技有限公司_{key}_{tag}", uid))
            made[key] = int(cur.fetchone()["id"])
    db.commit()
    yield made
    with db.cursor() as cur:
        cur.execute("DELETE FROM brands WHERE id = ANY(%s)", (list(made.values()),))
        cur.execute("DELETE FROM users WHERE id = ANY(%s)", ([AGENT_A, AGENT_B, ADMIN],))
    db.commit()


def test_fixture_really_made_test_brands(db, world):
    """🔴 分母自证:两个品牌确实 is_test=true。

    这一条为 0 时,下面每一条「看得见」都不可解读 —— 它可能只是因为根本没打上标记。
    """
    with db.cursor() as cur:
        cur.execute("SELECT id, is_test FROM brands WHERE id = ANY(%s)", (list(world.values()),))
        rows = {int(r["id"]): r["is_test"] for r in cur.fetchall()}
    assert rows == {world["a"]: True, world["b"]: True}, rows


def test_own_test_brand_is_visible_to_its_owner(brand_api, world):
    """存量臂:库里已是 is_test=true 且归 A 的品牌,A 自己必须看得见。"""
    assert world["a"] in _ids(_call(brand_api, AGENT_A))


def test_other_agents_test_brand_stays_invisible(brand_api, world):
    """他人臂:B 的品牌对 A 不可见 —— 归属过滤本来就管这件事,与 is_test 无关。

    🔴 没有这一臂,「存量臂绿」也可能是因为**过滤整个失效了**(谁都能看见谁的),
       那是越权,比原缺陷严重得多,而两者在存量臂读数上完全同形。
    """
    assert world["b"] not in _ids(_call(brand_api, AGENT_A))


def test_platform_view_still_isolates_test_brands(brand_api, world):
    """平台臂:admin + show_all 的**平台级视图**仍然隔离测试客户(铁律 5 未被削弱)。

    🔴 这是本单的边界:改的是「服务商自己的列表」,不是平台视图。
       这一条红 ⇒ 我把隔离整个拆了,而不是只拆了自己名下那一支。
    """
    seen = _ids(_call(brand_api, ADMIN, is_admin=True, show_all=True))
    assert world["a"] not in seen and world["b"] not in seen


def test_admin_escape_hatch_still_works(brand_api, world):
    """🔁 反臂:admin 的 `include_test=true` 逃生口照旧 —— 平台视图能看到它们。

    与上一条成对:上一条证「默认隔离还在」,这一条证「隔离不是永久的死路」。
    只有上一条时,把平台支写成恒排除(连 admin 也解不开)同样绿。
    """
    seen = _ids(_call(brand_api, ADMIN, is_admin=True, show_all=True, include_test=True))
    assert world["a"] in seen and world["b"] in seen
