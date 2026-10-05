"""交付计划面板拆分改造 · 验收锁(WO_GAPPLAN_RELOCATION_A 2026-08-10)

阶段 1 = 报价页按执行闸分档(Owner 2026-08-10 拍板"按状态分档",不是无条件摘)
阶段 2 = 售前版进客户 /s/:token 页(audience 裁剪 + 客户 token 链路接线)

判据设计的两条硬规矩(都在本包踩过):
  · 每条"必须"配一条同法的"必须不" —— 单向断言证明不了判别力
  · 🔴 判据打在**接线**上,不只打在函数上。所以"客户侧没有按钮"这条,
    既有直打 present_snapshot 的单元锁,也有真 ASGI 打 `GET /api/s/{token}` 的接线锁。
    只测函数 = 证明了函数会裁剪,证明不了那个端点真的用了裁剪后的那一份。

🔴 本文件所有"客户侧恒空"类判据一律用 **QUOTE_WITH_CAPACITY(900)**,不用 372:
   372 容量为 0,服务商侧本来就一个写动作都签发不出来 —— 拿它测"客户侧没有按钮"
   是正反两侧同时失效,恒绿且零判别力(记忆里 gate2 那次的同一个坑)。
   900 的服务商侧**确实**会出「去写这篇」,客户侧才有东西可被裁掉。
"""

from __future__ import annotations

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from services import gap_operation_plan as plan_service
from services.gap_operation_labels import translate_status
from tests.gap_plan_2026_08_08.conftest import AGENT_USER, make_app

# [WO_225-c1 §8.5] 372 是**已付款**的(paid_at 有值)。Owner 2026-09-15 ④ 之后,
#   已付款项目内写作不设容量闸 ⇒ 372 的闸现在是**开**的。
#   「闸关」那条锁的被测对象因此换成未付款的 998(夹具同批新增)。
QUOTE_PAID_EXHAUSTED = 372     # 已付款 + 15 槽用满 ⇒ 闸开、写动作带 over_capacity
QUOTE_UNPAID = 998             # 未付款 ⇒ 闸关、一条写动作都不给
QUOTE_ZERO_CAPACITY = QUOTE_UNPAID
QUOTE_WITH_CAPACITY = 900
BRAND_WITH_CAPACITY = 616

WRITE_ACTIONS = {"open_writing_task", "submit_publication_link"}

# 服务商侧独有、客户售前版一个都不该下发的块。
# 前四个是机读/运营口径,execution_gate 是阶段 1 的报价页开关 —— 客户页没有这个概念。
AGENT_ONLY_TOP_KEYS = (
    "capacity", "shortfall", "snapshot_id", "authority_generation", "execution_gate",
)


# ════════════════════════════════════════════════════════════════
# 夹具
# ════════════════════════════════════════════════════════════════

@pytest.fixture()
def duplicate_media_recommender(monkeypatch):
    """候选里有两条落在**同一个域名**上 → 第二条会被判成 rejected_duplicate_coverage。

    🔴 conftest 里的 `no_media_recommender` 三个域名各不相同,产不出合并项 ——
       第一版我用它测"合并项不给客户看",断言直接报"判据没有靶子"。
       这正是那条规矩起作用的样子:夹具造不出该状态时,判据要当场说自己是空的,
       而不是因为"确实没有合并项出现在客户侧"而绿。
    """
    def _fake(**kwargs):
        return {
            "vertical": [
                {"media_name": "博客园", "platform": "cnblogs.com",
                 "ai_citation_domain": "cnblogs.com", "price": 888},
                # 同域名第二条:dedupe_key=(gap_code, 'cnblogs.com') 已被占 → 合并
                {"media_name": "博客园(另一栏目)", "platform": "cnblogs.com",
                 "ai_citation_domain": "cnblogs.com", "price": 999},
            ],
            "generic": [
                {"media_name": "某行业榜单", "platform": "ranking.example.com",
                 "ai_citation_domain": "ranking.example.com", "price": 1200},
            ],
            "matched_industry": "建筑建材",
        }

    import services.placement_service as ps
    monkeypatch.setattr(ps, "recommend_for_publish_v2", _fake, raising=True)
    return _fake


def _bundle_and_capacity(quote_id: int):
    """直接走服务层拿一份真快照(与端点同一套函数,不是 mock 出来的形状)。"""
    from db import gap_plan_db
    from db.diagnosis_db import get_quote

    quote = get_quote(int(quote_id))
    assert quote, f"夹具里没有报价 {quote_id}"
    publications = gap_plan_db.get_publications(int(quote_id))
    capacity = plan_service.compute_capacity(quote, publications)
    bundle = plan_service.build_snapshot(quote, actor_user_id=None)
    return bundle, capacity


def _agent(quote_id: int) -> dict:
    bundle, capacity = _bundle_and_capacity(quote_id)
    return plan_service.present_snapshot(
        bundle, capacity=capacity, audience=plan_service.AUDIENCE_AGENT
    )


def _customer(quote_id: int) -> dict:
    bundle, capacity = _bundle_and_capacity(quote_id)
    return plan_service.present_snapshot(
        bundle, capacity=capacity, audience=plan_service.AUDIENCE_CUSTOMER
    )


def _write_action_ids(presented: dict) -> set[str]:
    ids: set[str] = set()
    for item in presented["items"]:
        ids.update(a["action_id"] for a in item.get("actions") or [])
    return ids & WRITE_ACTIONS


def _all_action_ids(presented: dict) -> set[str]:
    ids: set[str] = set()
    for item in presented["items"]:
        ids.update(a["action_id"] for a in item.get("actions") or [])
    return ids


def _dumped(payload) -> str:
    # default=str:出参里带 datetime(observed_at / generated_at),不转会 TypeError。
    return json.dumps(payload, ensure_ascii=False, default=str)


# ════════════════════════════════════════════════════════════════
# 阶段 1 · 执行闸
# ════════════════════════════════════════════════════════════════

def test_execution_gate_is_closed_when_capacity_is_zero(db, no_media_recommender):
    """报价 372(容量 0)→ 闸关 + 必须给出那一句人话。

    删掉 `_execution_gate` 里的 `capacity.executable`(改成恒 True)→ 本条转红。
    """
    presented = _agent(QUOTE_ZERO_CAPACITY)
    gate = presented["execution_gate"]
    assert gate["open"] is False
    # 🔴 关着必须**说得出为什么**:只给一个 False 会让前端只能显示一片空白。
    assert gate["hint"], "闸关着却没有提示文案 —— 报价页会出现一个没有解释的空块"
    assert gate["hint"] == translate_status("execution_not_open_yet")["explanation"]


def test_execution_gate_is_open_when_capacity_is_available(db, no_media_recommender):
    """反向对照:报价 900(有额度)→ 闸开,且不再重复那句提示。

    没有这一条,上一条可能只是"闸恒关"——恒关和恒真一样废。
    """
    presented = _agent(QUOTE_WITH_CAPACITY)
    gate = presented["execution_gate"]
    assert gate["open"] is True
    assert gate["hint"] == ""


# [WO_225-c1] 三张单一起跑:未付款(闸关)/ 已付款有额度(闸开)/ 已付款用满(闸开)。
#   第三张是本单新放开的那一档 —— 不带上它,「不许分叉」这条锁看不到新口径。
@pytest.mark.parametrize("quote_id",
                         [QUOTE_UNPAID, QUOTE_WITH_CAPACITY, QUOTE_PAID_EXHAUSTED])
def test_execution_gate_never_diverges_from_the_capacity_guard(db, no_media_recommender,
                                                               quote_id):
    """🔴 闸不是第二套容量口径:闸开 ⇔ 服务商侧真的签发得出写动作。

    这条守的是"两处各判一次就是第二套口径"那个坑:
    把 `_execution_gate` 改成读 `capacity.available >= 0`(恒真)之后,
    报价 372 会变成"闸开但一个写动作都没有" → 本条转红。
    两张单一起跑,才同时排除恒真与恒假。

    ── 🔴 已知边界(Review 2026-08-11 变异 X4b · 裁定为**行为等价**,不算锁弱)──
    复审把 `_execution_gate` 改成 `is_open = consumed < authorized`(**忽略 reserved**),
    本条**没有**转红。原因不是判据弱,是**现行合同里 reserved 恒为 0**
    (见 `services/gap_operation_plan.compute_capacity` 的 docstring:
     "合同当前把 reserved 恒定为 0(计划项还没落成槽)"),
    此时 `available = authorized - consumed - reserved` 与 `authorized - consumed`
    逐值相等 —— 那是一个真正的行为等价变异,任何判据都抓不到它。

    **但合同哪天启用 reserved > 0,这个第二口径就会真的分叉,而现有夹具抓不到**:
    QUOTE_ZERO_CAPACITY(authorized 15 / consumed 15 / reserved 0)与
    QUOTE_WITH_CAPACITY(15 / 0 / 0)两张单的 reserved 都是 0。
    → 到那时必须补一张 **reserved > 0 且 authorized - consumed > 0** 的分叉夹具
      (期望:available = 0 → 闸关;而 `consumed < authorized` 会误判成闸开)。
    """
    presented = _agent(quote_id)
    gate_open = presented["execution_gate"]["open"]
    has_write = bool(_write_action_ids(presented))
    assert gate_open == has_write, (
        f"报价 {quote_id}:闸 open={gate_open} 但可签发写动作={has_write} —— "
        "闸与容量守卫已经分叉,报价页会展开一个点不动的执行面(或反过来藏掉能干的活)"
    )


# ════════════════════════════════════════════════════════════════
# 阶段 2 · 受众裁剪(直打服务层)
# ════════════════════════════════════════════════════════════════

def test_customer_audience_emits_no_action_even_when_agent_side_can_write(
        db, no_media_recommender):
    """🔴 本包最硬的一条:同一份快照,服务商侧有「去写这篇」,客户侧一个动作都没有。

    正向(必须):客户侧每个 item 的 actions 恒为空。
    反向(必须不):同一张单的服务商侧**确实**签发得出写动作 ——
                 否则这条断言测的是一个本来就空的东西,恒绿。
    摘掉 present_snapshot 里 `for_customer` 那个分支 → 客户侧立刻拿到同一批按钮 → 转红。

    ── 🔴 本条锁的**准确范围**(Review 2026-08-11 变异 X1a 实证 · 交付单已同步降级)──
    复审在客户分支手工加入「**调用** `_actions_for_item` 但把结果丢弃」→ 131 条**全绿**,
    无一条转红。所以:
      · 被锁住的是 **输出恒空**(本条 + ASGI 接线锁,两层);
      · **没有**被锁住的是 "客户侧不调用那个出口" 这件事本身 ——
        源码里确实不调用,但那是**当前实现事实,不是机制保证**。
    刻意不补 spy/调用计数锁:真正伤人的方向(动作漏到客户浏览器)已被输出层两道锁覆盖,
    为凑一条机制锁引入 mock 会换来更脆的断点。
    → 但凡将来有人在客户分支里加中间层,**别指望这条锁拦住"调用"**,它只拦"发出去"。
    """
    agent_view = _agent(QUOTE_WITH_CAPACITY)
    customer_view = _customer(QUOTE_WITH_CAPACITY)

    # 反向对照先跑:判据必须有靶子
    assert "open_writing_task" in _all_action_ids(agent_view), (
        "服务商侧这张单本来就没有写动作 —— 那么下面那条'客户侧没有按钮'零判别力,"
        "夹具需要一张真的可执行的单"
    )

    assert _all_action_ids(customer_view) == set(), (
        f"客户售前版签发了动作:{_all_action_ids(customer_view)}"
    )
    for item in customer_view["items"]:
        assert item["actions"] == []


def test_customer_audience_has_no_channel_access_wording(db, no_media_recommender):
    """屏蔽「能否进入」话术:五个 access 态的 label/explanation 一条都不得出现。

    生产实测 media_outlets.entry_assessment 27,004 行全 NULL(0% 已核实)——
    客户在掏钱决策阶段读到「这个网站还没有人核实过能否进入」,读到的是
    "你们连能不能发都没搞清楚"。

    反向对照:服务商侧同一份数据里 access 文案**确实**出现,证明这段话术真的存在,
    不是"两边都没有所以两边都过"。
    """
    agent_text = _dumped(_agent(QUOTE_WITH_CAPACITY))
    customer_view = _customer(QUOTE_WITH_CAPACITY)
    customer_text = _dumped(customer_view)

    access_phrases = []
    for code in plan_service.ACCESS_STATUS_CODES:
        entry = translate_status(code)
        access_phrases += [entry["label"], entry["explanation"]]

    assert any(p in agent_text for p in access_phrases), (
        "服务商侧一句 access 话术都没有 —— 判据没有靶子,下面那条恒绿"
    )
    hits = [p for p in access_phrases if p in customer_text]
    assert not hits, f"客户售前版出现 access 话术:{hits}"

    # 字段层面同步核:access/落点/执行态三个键整体不下发,标题里也不带「发到 xxx」
    # 🔴 `status` 也必须不在 —— 这条是本锁第一轮**真的抓到的**泄漏:
    #    `hold_until_domain_access_confirmed` 是 allocation 码,人话却是
    #    「等渠道确认…避免文章写好却没有合适落点」,只屏蔽 access 字段挡不住它。
    for item in customer_view["items"]:
        for leaked in ("access", "target_domain", "status", "duplicate_of_item_id"):
            assert leaked not in item, f"客户售前版下发了执行侧字段 {leaked}"
        assert "发到" not in item["title"]
        # 「怎么发」那行不是删掉留空洞,而是换成团队安排话术
        assert item["rationale"]["how"] == translate_status(
            "access_arranged_by_team")["explanation"]

    # 反向对照:服务商侧这四个键**确实**都在(证明上面不是"两边都没有")
    for item in _agent(QUOTE_WITH_CAPACITY)["items"]:
        for present in ("access", "target_domain", "status"):
            assert present in item, f"服务商侧少了 {present} —— 上面那条判据没有靶子"


def test_customer_preview_drops_merged_items_and_renumbers(db, duplicate_media_recommender):
    """已合并项整条不出现,且序号连排(不留 1、3、4 的空档)。

    反向对照:服务商侧**保留**合并项(运营需要知道"这一篇为什么没了"),
    两边不同 → 证明过滤真的发生了,不是夹具里本来就没有合并项。
    """
    agent_items = _agent(QUOTE_WITH_CAPACITY)["items"]
    customer_items = _customer(QUOTE_WITH_CAPACITY)["items"]

    merged = [i for i in agent_items if i["status"]["code"] == "rejected_duplicate_coverage"]
    assert merged, "夹具里没有合并项 —— 本条判据没有靶子,需要能产生重复覆盖的候选"

    merged_ids = {i["plan_item_id"] for i in merged}
    assert not (merged_ids & {i["plan_item_id"] for i in customer_items})
    assert [i["ordinal"] for i in customer_items] == list(range(1, len(customer_items) + 1))
    for item in customer_items:
        assert item["title"].startswith(f"第 {item['ordinal']} 篇")


def test_customer_audience_drops_operator_only_blocks(db, no_media_recommender):
    """客户版不下发容量/不足额/机读代际/执行闸。反向对照:服务商版这些全在。"""
    agent_view = _agent(QUOTE_WITH_CAPACITY)
    customer_view = _customer(QUOTE_WITH_CAPACITY)

    for key in AGENT_ONLY_TOP_KEYS:
        assert key in agent_view, f"服务商版少了 {key} —— 判据没靶子"
        assert key not in customer_view, f"客户售前版下发了服务商块 {key}"

    assert customer_view["audience"] == plan_service.AUDIENCE_CUSTOMER
    assert agent_view["audience"] == plan_service.AUDIENCE_AGENT


def test_customer_summary_uses_the_presale_next_step(db, no_media_recommender):
    """客户版 next_step 走字典里的售前固定话术,不复用写给服务商的那一句。"""
    customer_view = _customer(QUOTE_WITH_CAPACITY)
    assert customer_view["summary"]["next_step"] == translate_status(
        "customer_plan_preview_next_step")["explanation"]
    # 运营口径的三样不下发
    for key in ("status_counts", "capacity_notice", "capacity_semantics"):
        assert key not in customer_view["summary"]


def test_unknown_audience_is_fail_closed(db, no_media_recommender):
    """受众认不出当场炸,不做"当成 agent"的兜底 —— 那个兜底的失败方向是把按钮发给客户。"""
    bundle, capacity = _bundle_and_capacity(QUOTE_WITH_CAPACITY)
    with pytest.raises(ValueError):
        plan_service.present_snapshot(bundle, capacity=capacity, audience="ops")
    # 反向对照:两个合法值都不炸(证明上面炸的是受众校验,不是别的地方坏了)
    for audience in (plan_service.AUDIENCE_AGENT, plan_service.AUDIENCE_CUSTOMER):
        plan_service.present_snapshot(bundle, capacity=capacity, audience=audience)


# ════════════════════════════════════════════════════════════════
# 阶段 2 · 接线(真 ASGI)
# ════════════════════════════════════════════════════════════════

def test_delivery_plan_endpoint_still_serves_the_agent_audience(db, no_media_recommender):
    """服务商侧端点不回归:受众仍是 agent、执行闸在、动作照签。"""
    client = TestClient(make_app(AGENT_USER), raise_server_exceptions=False)
    resp = client.get(f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan")
    assert resp.status_code == 200, resp.text
    snapshot = resp.json()["snapshot"]
    assert snapshot["audience"] == plan_service.AUDIENCE_AGENT
    assert snapshot["execution_gate"]["open"] is True
    assert "open_writing_task" in _all_action_ids(snapshot)


# ════════════════════════════════════════════════════════════════
# 阶段 2 · 客户 token 链路接线(真 ASGI · 无任何登录态)
#
# 🔴 这一组才是本改造的**接线判据**。上面那些直打 present_snapshot 的
#    只能证明"函数会裁剪",证明不了 `GET /api/s/{token}` 真的用了裁剪后的那一份 ——
#    记忆里「判据打在接线上不是函数上」已经踩过三次,这里不再踩第四次。
# ════════════════════════════════════════════════════════════════

def _selection_app():
    """真 ASGI:挂真 selection_api router,**不注入任何用户** ——

    /api/s/ 在 auth 中间件的 PUBLIC_PREFIXES 里,线上就是不注入 request.state.user
    的裸 fetch。`_is_owning_agent_for_session` 拿不到 Authorization 头 → False
    → 走客户(脱敏)分支。这里刻意复刻那个形态,不是"图省事不注入"。
    """
    from fastapi import FastAPI

    from api.selection_api import router

    app = FastAPI()
    app.include_router(router)
    return app


def _seed_selection_session(conn, *, status: str, quote_id: int = QUOTE_WITH_CAPACITY,
                            brand_id: int = BRAND_WITH_CAPACITY) -> str:
    token = secrets.token_urlsafe(12)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO keyword_selection_sessions
            (token, quote_id, brand_id, keywords_snapshot, status, expires_at,
             selected_keyword_ids, pricing_data, clusters_data, selected_tier)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (token, quote_id, brand_id, json.dumps([]), status, "2099-01-01 00:00:00",
         json.dumps([]), json.dumps({"keywords": []}), json.dumps({}), "standard"),
    )
    return token


def _owner_auth_header() -> dict[str, str]:
    """签一个 admin JWT,走 `_is_owning_agent_for_session` 的 is_admin 直通。

    🔴 为什么接线锁走**归属方**分支而不是匿名分支 —— 说清楚,不含糊:
       `quoted` 状态下**非归属方**读的是 `quote_pricing_snapshots` 里的冻结快照,
       而那张表由 `migration_quote_snapshots_and_archives_2026_07_21.sql` 建,
       该迁移又依赖 organization_internal_seats → pricing_catalog_versions …
       在本包这套 init_db 测试库上要一路补齐五六个迁移才跑得起来。

       但**我要测的那行接线在 if/else 两个分支之外**(定价分支决定的是 pricing_data
       来自活草稿还是冻结快照;delivery_plan 是在它之后按 status + pricing_data 判的),
       所以归属方分支能一模一样地走到它。
       用 `_sign` 自己签 token 是用本模块的签名原语,不是绕过校验:
       decode_jwt 照常验签 + 验 exp。

    🔴 **诚实边界**:这条锁证明的是"这个端点会下发裁剪后的售前计划",
       **不证明**匿名客户分支端到端(那一段的差异全在定价来源上,本包一行没动)。
       匿名可达性由下面 `test_selection_page_is_reachable_without_any_auth` 单独证;
       真 token 的端到端由工单验收 ① 的生产实测承担。
    """
    import json as _json
    import time as _time

    from auth.jwt_utils import _base64url_encode, _sign

    header = _base64url_encode(
        _json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    payload = _base64url_encode(
        _json.dumps({"user_id": 1, "username": "root", "is_admin": True,
                     "exp": int(_time.time()) + 3600},
                    separators=(",", ":")).encode()
    )
    return {"Authorization": f"Bearer {header}.{payload}.{_sign(f'{header}.{payload}')}"}


def test_selection_page_serves_the_customer_cropped_plan(db, no_media_recommender):
    """🔴 接线锁:打 `GET /api/s/{token}` 拿到的是**裁剪后**的那一份,不是服务商那份。

    正向:delivery_plan 非空、受众 customer、每个 item 的 actions 恒空。
    反向:同一张报价(900)在服务商端点上**确实**签发得出「去写这篇」
         (test_delivery_plan_endpoint_still_serves_the_agent_audience)。
         两条合起来才成立"同一份数据,两个受众两种出参";
         少了反向那条,本条测的可能只是"这张单本来就没动作"。
    """
    token = _seed_selection_session(db, status="quoted")
    client = TestClient(_selection_app(), raise_server_exceptions=False)
    resp = client.get(f"/api/s/{token}", headers=_owner_auth_header())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    plan = body.get("delivery_plan")
    assert plan, "端点没有下发售前交付计划 —— 阶段 2 的接线没接上"
    assert plan["audience"] == plan_service.AUDIENCE_CUSTOMER
    assert plan["items"], "售前计划是空的,客户看不到任何东西"
    for item in plan["items"]:
        assert item["actions"] == []
        assert "access" not in item and "target_domain" not in item and "status" not in item
    for key in AGENT_ONLY_TOP_KEYS:
        assert key not in plan, f"客户页下发了服务商块 {key}"


def test_selection_page_is_reachable_without_any_auth(db, no_media_recommender):
    """匿名可达性 + 反向对照:还没出价的状态下**不**下发售前计划。

    一条断言两件事:
      · /api/s/{token} 无任何 Authorization 也是 200(它就是客户裸 fetch 的那条链路)
      · delivery_plan 不是无条件塞进每个响应 —— 只在客户真看得到价格的两档才有
    """
    token = _seed_selection_session(db, status="keywords_submitted")
    client = TestClient(_selection_app(), raise_server_exceptions=False)
    resp = client.get(f"/api/s/{token}")          # 刻意不带任何头
    assert resp.status_code == 200, resp.text
    assert resp.json().get("delivery_plan") is None


def test_selection_page_survives_a_broken_plan(db, no_media_recommender, monkeypatch):
    """售前计划算炸了,客户看报价这件主事不能跟着 500。

    🔴 这条不是"容错好看":客户页是**收款链路**上的一屏。
       为一个加分模块把它拖成 500,是拿锦上添花换掉了雪中送炭。
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("MUTATION: 快照生成炸了")

    monkeypatch.setattr(plan_service, "build_snapshot", _boom, raising=True)

    token = _seed_selection_session(db, status="quoted")
    client = TestClient(_selection_app(), raise_server_exceptions=False)
    resp = client.get(f"/api/s/{token}", headers=_owner_auth_header())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("delivery_plan") is None
    # 主链路字段照常在(证明这条不是"整个响应都没了所以也没报错")
    assert "pricing_data" in body and "keywords" in body
