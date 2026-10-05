"""WO-B2 ①②:深链 **producer** + 素材页合同登记(规格 §12.2/§12.3)。

## 这一包补的是哪一格

WO-B 把三个页面的**消费方**接通了,但交付单 §8-2 自己写着:

    全仓**没有任何地方**构造 `?xiaobang_intent=`(XiaobangDrawer 零 intent 用法)。
    三页的消费方现在都通了,但今天只能靠手拼 URL 到达。

也就是说那条链**只有下半截**。本包补上半截:

    小榜答案里的链接卡 ──(带 operation_id)──▶ POST …/{op}/prepare
      ──▶ intent_id + 服务端签发的 target_route ──▶ /route?xiaobang_intent=xint_…
      ──▶ 目标页解析 → 预填上屏

浏览器那一跳(卡片→URL→预填渲染)在
`frontend/tests/xiaobang-prefill/deeplink-producer.spec.ts`;
本文件打的是**服务端那半截**与两条结构锁。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import gap_operation_map as omap

REPO = pathlib.Path(__file__).resolve().parents[2]
CHAT_API = REPO / "api" / "xiaobang_api.py"

ADMIN = {"user_id": 9101, "username": "b2-admin", "is_admin": True,
         "agent_level": 0, "permissions": []}


# ══════════════════════════════════════════════════════════════════════════
# ① producer · 路由 → operation 的映射是**机械派生**的
# ══════════════════════════════════════════════════════════════════════════

def test_every_commandable_route_maps_back_to_its_operation():
    """🔴 分母来自注册表:每一条**有合同**的 operation,它的路由都要能反查回它。

    手写映射表的失效方式是沉默的 —— 加一条合同却忘了加映射,表现是
    "这张卡不再签发深链",没有任何东西会变红。
    """
    entries = list(omap.commandable_operations())
    assert entries, "注册表零 commandable —— 本条判据没有分母"
    for entry in entries:
        got = omap.commandable_operation_for_route(entry.route_template)
        assert got == entry.operation_id, (entry.operation_id, entry.route_template, got)


def test_routes_without_a_contract_get_no_deep_link():
    """🔁 反向对照:没有合同的路由**不许**拿到 operation_id。

    给它签发深链 = 给用户一个必然 404 的链接(`_resolve_entry` 对无合同一律 404)。
    分母同样机械取:注册表里所有**没有**合同的条目。
    """
    from urllib.parse import urlsplit

    commandable_paths = {urlsplit(e.route_template).path
                         for e in omap.commandable_operations()}
    assert commandable_paths, "注册表零 commandable —— 反向对照没有分母"
    plain = [e for e in omap.all_operations()
             if urlsplit(e.route_template).path not in commandable_paths]
    assert plain, "所有条目都落在 commandable 路径上 —— 反向对照没有分母"
    for entry in plain:
        got_none = omap.commandable_operation_for_route(entry.route_template)
        assert got_none is None, (entry.operation_id, got_none)

    # 🔴 说清楚一个**真实存在**的形态,别让下一个人以为它被覆盖了:
    #    `media_library` 与 `publish_center` **共用 /publish 这一条路由**。
    #    映射按路径做(卡片手里只有路由),所以它会解析成 publish_center。
    #    这是**期望行为**:目标页是同一页,给一个能预填的 intent 好过给一张空表单;
    #    但必须写出来 —— 不写的话「没有合同就一定拿不到深链」会被当成全称命题。
    shared = [e for e in omap.all_operations()
              if e.command_contract is None
              and urlsplit(e.route_template).path in commandable_paths]
    for entry in shared:
        got = omap.commandable_operation_for_route(entry.route_template)
        assert got is not None and got != entry.operation_id, (entry.operation_id, got)
    # 彻底不存在的路由同样是 None(不是抛,也不是猜一个)
    assert omap.commandable_operation_for_route("/no-such-page") is None
    assert omap.commandable_operation_for_route("") is None


def test_every_link_emission_goes_through_the_producer_helper():
    """🔴 接线锁:`meta["link"] = …` 的**每一处**都必须过 `_with_operation`。

    `api/xiaobang_api.py` 里有四处独立的 link 签发点(preset / canned /
    降级 intent map / `_pick_link`)。漏掉任何一处 = 那条路径上的卡片**不签深链**,
    而它看起来完全正常 —— 用户只是"点了没有预填"。

    分母**从源码 AST 取**(所有把 `link` 键赋值的语句),不是我数的四处。
    """
    tree = ast.parse(CHAT_API.read_text(encoding="utf-8"))
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "link"):
                sites.append(node)
    assert len(sites) >= 4, "link 签发点少于 4 处 —— 分母不对,锁要跟着改"
    for node in sites:
        src = ast.unparse(node.value)
        # 🔴 判的是「**不许直接拼一个 dict 字面量塞进去**」。
        #    唯一合法的例外是把 `_pick_link()` 的结果赋过去(它内部已经过 helper),
        #    那一支由下面 test_pick_link_returns_through_the_producer_helper 单独钉 ——
        #    两条判据加起来才没有洞。
        assert not isinstance(node.value, ast.Dict), (
            "这一处 link 签发直接拼了 dict 字面量,绕过 producer helper:" + src[:80]
            + ";它签发的卡片不会带 operation_id ⇒ 那条路径上没有深链")
        assert src.startswith("_with_operation(") or "_pick_link" in src or src == "link_out", (
            "这一处 link 签发既不过 helper 也不来自 _pick_link:" + src[:80])


def test_pick_link_returns_through_the_producer_helper():
    """🔴 `_pick_link` 的**每一条非 None return** 都必须过 `_with_operation`。

    上一条放行了「把 `_pick_link` 的结果赋给 link」这一支,所以这一支的
    producer 保证必须在这里补上 —— 否则两条判据加起来仍留着一个洞。
    """
    tree = ast.parse(CHAT_API.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_pick_link")
    # 🔴 只取**直属** `_pick_link` 的 return:`ast.walk` 会把内嵌的
    #    `title_match()` 也走进去,它的 `return sum(...)` 与 link 签发无关。
    #    量错了东西,结论再自洽也没用。
    nested = {id(n) for f in ast.walk(fn)
              if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f is not fn
              for n in ast.walk(f)}
    returns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Return) and id(n) not in nested
               and n.value is not None
               and not (isinstance(n.value, ast.Constant) and n.value.value is None)]
    assert len(returns) >= 2, "_pick_link 的非空 return 少于 2 条 —— 分母不对"
    for node in returns:
        src = ast.unparse(node.value)
        assert src.startswith("_with_operation("), (
            "_pick_link 这一条 return 没过 helper:" + src[:80])


def test_the_producer_helper_only_adds_a_registry_backed_operation():
    """helper 本身:有合同才加键,没有就**原样返回**(老行为逐字节不变)。"""
    import api.xiaobang_api as chat

    with_op = chat._with_operation({"route": "/publish", "label": "去发布"})
    assert with_op["operation_id"] == "publish_center", with_op
    assert with_op["route"] == "/publish" and with_op["label"] == "去发布"

    without = chat._with_operation({"route": "/pricing", "label": "去报价"})
    assert "operation_id" not in without, without


# ══════════════════════════════════════════════════════════════════════════
# ① producer · 服务端那半截真跑一次(三条 operation 各一发)
# ══════════════════════════════════════════════════════════════════════════

def _ops_client(monkeypatch):
    """真 HTTP。鉴权替身只替对象归属,其余全走真的(prepare 真落库)。"""
    import api.xiaobang_operations_api as ops

    class _Ctx:
        owner_user_id = 9101

        def public_context(self):
            return {"brand_id": 101, "brand_name": "甲品牌", "data_updated_at": None}

    monkeypatch.setattr(ops, "_authorized_context", lambda request, refs, page="": _Ctx())

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = dict(ADMIN)
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(ops.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def pricing_table(db):
    """报价链要读的目录表。

    🔴 本包的一次性库只跑迁移 038(两张协调层新表),没有 `feature_pricing`。
       不建它的话 `_resolve_compute_quote` 会撞 `UndefinedTable` —— 它只 catch
       `ValueError` ⇒ prepare 500。那是**环境缺表**不是产品缺陷;
       但用 monkeypatch 把 `_resolve_compute_quote` 换掉就等于不验报价链那一跳,
       所以这里**建真表**,让报价链真的跑一遍。
    """
    cur = db.cursor()
    # 🔴 列**按 `db/wallet_db.get_feature_pricing` 真查的那几列**建,不是我记得几列:
    #    少一个 `is_active` 就是 `UndefinedColumn` → 500,而那是夹具的问题不是产品的。
    cur.execute("CREATE TABLE IF NOT EXISTS feature_pricing ("
                " feature_code TEXT PRIMARY KEY, cost_points INTEGER NOT NULL,"
                " feature_name TEXT, is_active BOOLEAN NOT NULL DEFAULT TRUE,"
                " updated_at TIMESTAMPTZ DEFAULT NOW())")
    for code in ("media_publish", "article_gen"):
        cur.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name)"
                    " VALUES (%s, 390, %s) ON CONFLICT (feature_code) DO NOTHING",
                    (code, code))
    db.commit()
    yield


@pytest.mark.parametrize("operation_id", sorted(
    e.operation_id for e in omap.commandable_operations()))
def test_prepare_returns_everything_the_deep_link_needs(monkeypatch, pricing_table,
                                                        operation_id):
    """🔴 producer 的服务端半截:prepare 必须同时给出 **intent_id** 与 **target_route**。

    少任何一个,前端就拼不出深链 —— 而拼不出的表现是"点了照常跳,只是没预填",
    没有报错。分母是注册表里**全部**有合同的 operation(含本包新登记的素材页)。
    """
    with _ops_client(monkeypatch) as client:
        got = client.post(
            "/api/xiaobang/operations/" + operation_id + "/prepare",
            json={"prepare_request_id": "b2-producer-" + operation_id,
                  "selection": {"brand_id": 101}})
    assert got.status_code == 200, got.text[:600]
    body = got.json()
    assert body.get("replayed") is False, "幂等命中旧行 —— 这条没走到当前 producer"
    assert re.fullmatch(r"xint_[A-Za-z0-9_-]{8,64}", str(body["intent_id"])), body["intent_id"]
    route = (body.get("deep_link") or {}).get("target_route")
    assert route and str(route).startswith("/"), body.get("deep_link")
    # 🔴 服务端签发的路由必须与注册表一致(前端"不拼、不猜、不兜底旧路径")
    assert route == omap.resolve_operation(operation_id).route_template, route


def test_the_materials_page_can_now_mint_an_intent(monkeypatch, pricing_table):
    """🔴 ② 的收口:`/marketing-materials` 的深链 intent **真产生得出来**。

    登记合同之前,`_resolve_entry` 对无合同一律 404 ⇒ 这一页的 intent
    从物理上就产生不出来,WO-B 接的那个消费方是够不到的。
    """
    with _ops_client(monkeypatch) as client:
        got = client.post("/api/xiaobang/operations/geo_content_center/prepare",
                          json={"prepare_request_id": "b2-materials-0001",
                                "selection": {"brand_id": 101}})
        assert got.status_code == 200, got.text[:600]
        intent_id = got.json()["intent_id"]
        served = client.get("/api/xiaobang/operations/intents/" + intent_id + "/prefill")
    assert served.status_code == 200, served.text[:600]
    assert served.json()["target_route"] == "/marketing-materials", served.json()
    # 🔴 不新增扣费点:这一档报不出价,也不该假装报得出
    assert "compute_quote" not in got.json(), got.json()


# ══════════════════════════════════════════════════════════════════════════
# ② 能力发现列得出它 + DLP 闸仍绿(WO-B 那道豁免没被新条目打红)
# ══════════════════════════════════════════════════════════════════════════

def test_capabilities_lists_the_new_contract_and_survives_the_dlp_gate(monkeypatch):
    """🔴 登记之后:能力发现端点**列得出它**,且出参闸放行。

    WO-B 那道字段级机器面豁免是按 `confirmation_policy.bind` 的**取值集合**收窄的,
    新条目的 bind 来自同一组源码常量 ⇒ 应当自动被覆盖。
    这条就是工单说的「你自己修的那道豁免别被新条目打红」。
    """
    # 🔴 **相对导入**。本包有一条锁禁止 `tests.` 开头的绝对导入
    #    (`test_no_module_in_this_package_imports_itself_by_absolute_path`)——
    #    绝对路径导入会让这个包在不同 rootdir 下时灵时不灵。锁抓到过我这一处。
    from .test_capabilities_dlp_exemption_2026_08_20 import CAPS_URL, _app

    with _app(monkeypatch, identity_key="admin") as client:
        got = client.get(CAPS_URL)
    assert got.status_code == 200, got.text[:600]
    body = got.json()
    ids = {c["operation_id"] for c in body["commands"]}
    assert "geo_content_center" in ids, ids
    entry = next(c for c in body["commands"] if c["operation_id"] == "geo_content_center")
    assert entry["side_effect"] == "compute_only", entry
    assert entry["billing_feature"] is None, entry
    assert entry["executable"] is False, entry
    # bind 真的在出参里(不是"因为字段没了所以不泄漏")
    assert entry["confirmation_policy"]["bind"], entry
