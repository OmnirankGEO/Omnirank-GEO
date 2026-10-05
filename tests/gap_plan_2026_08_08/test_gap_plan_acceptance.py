"""P4 缺口作战计划 · 验收锁(合同 §12 十二条 + 工单 D 节四条)

真库 + 真 ASGI。每条"必须"都配一条同法的"必须不" —— 单向断言证明不了判别力。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.gap_plan_2026_08_08.conftest import (
    ADMIN_USER, AGENT_USER, OTHER_USER, make_app,
)

QUOTE_PAID_EXHAUSTED = 372     # 生产真实样本:已付款(paid_at 有值),15 槽用满
QUOTE_UNPAID = 998             # [WO_225-c1] 未付款(paid_amount=0):仍不给执行动作
QUOTE_ZERO_CAPACITY = QUOTE_UNPAID   # 「容量 0 不给写」这条锁的被测对象已换成未付款那张
QUOTE_WITH_CAPACITY = 900      # 对照:除 total_articles=5 外与 372 同形
QUOTE_OF_OTHERS = 701


def _client(user=AGENT_USER):
    return TestClient(make_app(user), raise_server_exceptions=False)


def _plan(client, quote_id):
    resp = client.get(f"/api/quotes/{quote_id}/delivery-plan")
    assert resp.status_code == 200, resp.text
    return resp.json()["snapshot"]


def _all_action_ids(snapshot) -> set[str]:
    ids = set()
    for item in snapshot["items"]:
        ids.update(a["action_id"] for a in item["actions"])
    return ids


def _all_labels(snapshot) -> str:
    """把整份出参里所有会渲染的文案拼起来,给"不得出现"类断言用。"""
    import json
    return json.dumps(snapshot, ensure_ascii=False)


def _leak_scan_body(payload) -> str:
    """扫描业务出参，但排除哈希/ID 这类不透明元数据里的偶然数字片段。"""
    import json

    opaque = {
        "snapshot_id", "plan_item_id", "assistant_request_id",
        "data_version", "source_hash", "generation_id",
    }

    def scrub(value):
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items() if key not in opaque}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return json.dumps(scrub(payload), ensure_ascii=False)


# ════════════════════════════════════════════════════════════════
# 合同判据 #2 / #10 · 容量守卫(本包最硬的一条)
# ════════════════════════════════════════════════════════════════

def test_capacity_zero_never_emits_write_action(db, no_media_recommender):
    """🔴 判据 #10(WO_225-c1 §8.5 改口径):**未付款**单全程不得出现「去写这篇」。

    改口径前这条打的是报价 372 —— 那张单 paid_at 有值,是**已付款**的。
    Owner 2026-09-15 ④ 明令已付款项目内写作不设容量闸,所以 372 现在**应当**
    出写动作;继续拿它当「不许出」的被测对象,等于用判据钉住已被推翻的行为
    (本仓记着的「判据钉住缺陷会跟修法打架」)。被测对象因此换成未付款的 998。

    把 `_actions_for_item` / `_allocation_code_for` 的 `and not capacity.payable`
    去掉 → 未付款单同样会拿到写动作 → 本条转红。
    """
    client = _client()
    snapshot = _plan(client, QUOTE_UNPAID)

    cap = snapshot["capacity"]
    # 未付款:合同侧折算成 available 0 + capacity_zero,authorized 照实显示。
    assert cap["available_articles"] == 0
    assert cap["authorized_articles"] == 15
    assert cap["quote_is_payable"] is False, "被测对象必须真的是未付款的那张单"
    assert cap["display_status"] == "capacity_zero"
    assert cap["contract_version"] == "article-capacity-v1"

    actions = _all_action_ids(snapshot)
    assert "open_writing_task" not in actions, f"容量 0 却签发了写动作:{actions}"
    assert "submit_publication_link" not in actions

    # 文案层同样不得出现(动作 id 挡住了,但按钮文字是另一条路径)
    assert "去写这篇" not in _all_labels(snapshot)

    # 该出现的占位必须真的出现,否则这张卡是死状态
    assert "request_quote_capacity" in actions
    placeholder = next(
        a for item in snapshot["items"] for a in item["actions"]
        if a["action_id"] == "request_quote_capacity"
    )
    assert placeholder["enabled"] is False
    assert placeholder["label"] == "报价评估待接入"
    assert placeholder.get("implementation_phase") == "P1"


def test_capacity_available_does_emit_write_action(db, no_media_recommender):
    """🔴 反向对照:有额度时必须真的出现「去写这篇」。

    没有这一条,上面那条在"永远不签发任何写动作"的实现下也会绿 —— 即恒红=恒废。
    """
    client = _client()
    snapshot = _plan(client, QUOTE_WITH_CAPACITY)

    cap = snapshot["capacity"]
    assert cap["available_articles"] > 0
    # 有额度但一篇没排 → 合同的第三态「额度已保留」(不是「快去写满」)
    assert cap["display_status"] == "capacity_reserved"
    actions = _all_action_ids(snapshot)
    assert "open_writing_task" in actions, (
        "有额度却签不出写动作 —— 容量守卫成了恒红,判据零判别力"
    )


def test_actions_guard_strips_write_actions_even_if_state_says_ready():
    """🔴 直打守卫函数,喂一个上游**不会产生**的组合:状态说 ready + 容量不可执行。

    这道守卫的意义就是防上游哪天改松,所以必须用上游产生不出来的输入来钉它 ——
    否则它是一段永远走不到、也无法被证伪的代码(变异 M2 分诊出来的正是这一点)。
    """
    from services import article_capacity_contract as cc
    from services.gap_operation_plan import Capacity, _actions_for_item

    # 容量态一律经合同构造 —— 测试里也不许自己拼一个 P4 私有的容量对象,
    # 否则测的是"我以为的容量语义"而不是生产会拿到的那个。
    # [WO_225-c1 §8.5] 第一臂改成**未付款且用满** —— 已付款用满现在是放行的那一档,
    #   拿它当「必须剔除」的输入等于钉住一个已被 Owner ④ 推翻的行为。
    broke = Capacity(cc.build_capacity_view(
        authorized_articles=15, consumed_articles=15, quote_is_payable=False))  # 未付款且用满
    assert broke.executable is False
    assert broke.payable is False
    ids = {a["action_id"] for a in
           _actions_for_item(allocation_code="ready_to_execute", capacity=broke)}
    assert "open_writing_task" not in ids
    assert "submit_publication_link" not in ids

    # 正臂:已付款 + 用满 ⇒ 照发,并带 over_capacity。
    paid_full = Capacity(cc.build_capacity_view(
        authorized_articles=15, consumed_articles=15, quote_is_payable=True))
    assert paid_full.executable is False and paid_full.payable is True
    acts = _actions_for_item(allocation_code="ready_to_execute", capacity=paid_full)
    assert "open_writing_task" in {a["action_id"] for a in acts}
    assert all(a.get("over_capacity") is True for a in acts
               if a["action_id"] == "open_writing_task")
    # 反向:有额度时**不许**带这个标(否则它变成一个恒真的装饰)
    assert all(a.get("over_capacity") is None for a in _actions_for_item(
        allocation_code="ready_to_execute",
        capacity=Capacity(cc.build_capacity_view(
            authorized_articles=5, consumed_articles=1, quote_is_payable=True))))

    ok = Capacity(cc.build_capacity_view(
        authorized_articles=5, consumed_articles=1, quote_is_payable=True))     # 反向对照
    assert ok.executable is True
    ids_ok = {a["action_id"] for a in
              _actions_for_item(allocation_code="ready_to_execute", capacity=ok)}
    assert "open_writing_task" in ids_ok

    # 🔴 第三档:未付款。合同把它折算成 capacity_zero / available 0,
    #    P4 因此不必(也不许)再判一次付款状态。
    unpaid = Capacity(cc.build_capacity_view(
        authorized_articles=15, consumed_articles=0, quote_is_payable=False))
    assert unpaid.executable is False
    assert unpaid.display_status == cc.CAPACITY_STATUS_ZERO
    assert "open_writing_task" not in {
        a["action_id"] for a in
        _actions_for_item(allocation_code="ready_to_execute", capacity=unpaid)}


def test_capacity_guard_lives_on_the_server_not_the_client(db, no_media_recommender):
    """守卫必须在服务端:端点出参里就没有写动作,而不是前端把按钮藏起来。"""
    client = _client()
    resp = client.get(f"/api/quotes/{QUOTE_UNPAID}/delivery-plan")
    assert "open_writing_task" not in resp.text


def test_paid_over_capacity_still_emits_write_action(db, no_media_recommender):
    """🔴 [WO_225-c1 §8.5] 已付款 + 槽用满 ⇒ 写类动作**照发**,并带 over_capacity。

    Owner 2026-09-15 ④ 原话:「我们不是可以一直写嘛…正常计费即可,不用阻断不用提示」。
    这是与上一条配对的**正臂**:少了它,把守卫改成「谁都不许写」也能让上一条绿
    (恒红 = 恒废)。

    把 `_actions_for_item` 的条件改回 `not capacity.executable` → 本条立刻转红。
    """
    client = _client()
    snapshot = _plan(client, QUOTE_PAID_EXHAUSTED)

    cap = snapshot["capacity"]
    # 先证明被测对象真的是「已付款且一个槽都不剩」,否则下面的断言测的是别的场景。
    assert cap["quote_is_payable"] is True
    assert cap["authorized_articles"] == 15
    assert cap["consumed_articles"] == 15, "15 条 completed 必须真的折算成 15 槽"
    assert cap["available_articles"] == 0

    actions = _all_action_ids(snapshot)
    assert "open_writing_task" in actions, (
        f"已付款项目内写作不设闸(Owner ④),却没签出写动作:{actions}")

    # 标记只进出参给人看,**不改 enabled** —— Owner 明说不用阻断不用提示。
    writes = [a for item in snapshot["items"] for a in item["actions"]
              if a["action_id"] == "open_writing_task"]
    assert writes, "上面断言过了,这里必须取得到"
    assert all(a.get("over_capacity") is True for a in writes), writes
    assert all(a.get("enabled") is not False for a in writes), (
        f"over_capacity 成了第二道软闸,与 Owner ④「不用阻断」相违:{writes}")


# ════════════════════════════════════════════════════════════════
# A5 · 行级归属(不依赖中间件兜底)
# ════════════════════════════════════════════════════════════════

def test_other_tenant_cannot_read_the_plan(db, no_media_recommender):
    client = _client(OTHER_USER)
    resp = client.get(f"/api/quotes/{QUOTE_OF_OTHERS}/delivery-plan")
    assert resp.status_code in (403, 404), resp.text


def test_owner_can_read_own_plan(db, no_media_recommender):
    """反向对照:归属校验不是恒拒。"""
    client = _client(AGENT_USER)
    resp = client.get(f"/api/quotes/{QUOTE_ZERO_CAPACITY}/delivery-plan")
    assert resp.status_code == 200, resp.text


def test_admin_can_read_any_plan(db, no_media_recommender):
    client = _client(ADMIN_USER)
    assert client.get(f"/api/quotes/{QUOTE_OF_OTHERS}/delivery-plan").status_code == 200


def test_write_endpoints_also_check_ownership(db, no_media_recommender):
    """🔴 读挡住了不代表写挡住了 —— 两条路径分别测。"""
    client = _client(OTHER_USER)
    resp = client.post(
        f"/api/quotes/{QUOTE_OF_OTHERS}/delivery-plan/items/Q701-P01/publication-link",
        json={"publication_url": "https://example.com/x"},
    )
    assert resp.status_code in (403, 404), resp.text


# ════════════════════════════════════════════════════════════════
# 合同判据 #6 / D3 · 依据脱敏与术语泄漏
# ════════════════════════════════════════════════════════════════

def test_evidence_platforms_are_anonymized(db, no_media_recommender):
    """判据 #6:四平台依据只出脱敏数据,不出模型/供应商名。"""
    client = _client()
    snapshot = _plan(client, QUOTE_ZERO_CAPACITY)
    cards = snapshot["evidence_platforms"]
    assert cards, "依据抽屉不能是空的(四引擎答案环境里明明有数据)"

    labels = {c["platform_label"] for c in cards}
    assert labels <= {"AI 平台一", "AI 平台二", "AI 平台三", "AI 平台四"}, labels

    body = _all_labels(snapshot).lower()
    for supplier in ("deepseek", "kimi", "豆包", "千问", "moonshot", "qwen"):
        assert supplier.lower() not in body, f"出参泄漏供应商名:{supplier}"


def test_no_cost_fields_leak_from_the_recommender(db, no_media_recommender):
    """🔴 现役推荐机器的原始项带 price / our_price_points / our_price_yuan。

    本包做的是逐字段白名单转录。改成整包透传 → 本条转红。
    """
    client = _client()
    body = _leak_scan_body(_plan(client, QUOTE_ZERO_CAPACITY))
    for term in ("our_price_points", "our_price_yuan", "publish_success_factor", "888", "66.6"):
        assert term not in body, f"交付计划出参泄漏内部采购字段:{term}"

    # 🔴 item 端点是**另一条出口**:frozen_spec 直达浏览器,plan 出参里没有它。
    #    只扫 plan 出参 → 变异 M6(frozen_spec 整包展开 cand)会存活。
    #    两个出口分别扫,才叫"判据打在接线上"。
    plan = _plan(client, QUOTE_WITH_CAPACITY)
    pid = plan["items"][0]["plan_item_id"]
    detail = client.get(f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{pid}")
    assert detail.status_code == 200, detail.text
    detail_body = _leak_scan_body(detail.json())
    for term in ("our_price_points", "our_price_yuan", "publish_success_factor", "888", "66.6"):
        assert term not in detail_body, f"item 端点泄漏内部采购字段:{term}"


def test_no_untranslated_internal_enum_reaches_the_wire(db, no_media_recommender):
    """B5 机械闸:界面拿到的每个状态都必须是人话,不是 ascii 枚举。"""
    client = _client()
    snapshot = _plan(client, QUOTE_ZERO_CAPACITY)
    for item in snapshot["items"]:
        assert item["status"]["label"] and not item["status"]["label"].isascii(), item["status"]
        assert item["status"]["explanation"]


def test_leak_scanner_actually_catches_a_planted_term():
    """🔴 反向对照:证明术语扫描器不是恒绿。

    没有这条,上面三条在"扫描器什么都不查"的实现下全绿。
    """
    from services.gap_operation_labels import InternalTermLeak, assert_no_internal_leak

    assert_no_internal_leak({"headline": "客户还没进入推荐名单"})   # 好的必须放行
    with pytest.raises(InternalTermLeak):
        assert_no_internal_leak({"headline": "本次由 DeepSeek 生成"})
    with pytest.raises(InternalTermLeak):
        assert_no_internal_leak({"summary": {"state": "attack_absence"}})
    with pytest.raises(InternalTermLeak):
        assert_no_internal_leak({"note": "进货价 12 元"})


# ════════════════════════════════════════════════════════════════
# 合同判据 #3 / 判别测试 #2 · 深链与代际
# ════════════════════════════════════════════════════════════════

def test_item_lookup_freezes_the_spec(db, no_media_recommender):
    """判据 #3:写作中心同时锁定 quote_id 与 plan_item_id,并拿到冻结规格。"""
    client = _client()
    snapshot = _plan(client, QUOTE_WITH_CAPACITY)
    pid = snapshot["items"][0]["plan_item_id"]

    resp = client.get(f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{pid}")
    assert resp.status_code == 200, resp.text
    item = resp.json()["item"]
    assert item["plan_item_id"] == pid
    assert item["quote_id"] == QUOTE_WITH_CAPACITY
    spec = item["frozen_spec"]
    assert spec["target_question"]
    assert spec["content_form"]


def test_stale_generation_is_rejected(db, no_media_recommender):
    """🔴 判别测试 #2:代际过期的旧链接不得预填。

    删掉 api/gap_plan_api 里那段 expected_authority_generation 比对 → 本条转红。
    """
    client = _client()
    snapshot = _plan(client, QUOTE_WITH_CAPACITY)
    pid = snapshot["items"][0]["plan_item_id"]
    current = snapshot["authority_generation"]

    ok = client.get(
        f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{pid}"
        f"?expected_authority_generation={current}"
    )
    assert ok.status_code == 200, "同代际必须放行(反向对照,否则判据恒红)"

    stale = client.get(
        f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{pid}"
        f"?expected_authority_generation={current - 1}"
    )
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]["error"]
    assert detail["code"] == "plan_item_generation_stale"
    assert detail["message"]                     # 错误合同:必须有人话
    assert detail["primary_action"]["label"]     # 必须有可执行出口


def test_unknown_plan_item_returns_actionable_error(db, no_media_recommender):
    """合同 §3.2:计划不存在时回词包总览 + 可执行解释,不落空白页。"""
    client = _client()
    resp = client.get(f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/Q999-P99")
    assert resp.status_code == 404
    err = resp.json()["detail"]["error"]
    assert err["code"] == "plan_item_not_found"
    assert err["primary_action"]["action_id"] == "back_to_plan"


# ════════════════════════════════════════════════════════════════
# 合同判据 #5 / #4 · 等渠道 与 重复覆盖
# ════════════════════════════════════════════════════════════════

def test_waiting_channel_card_offers_both_exits(db, no_media_recommender):
    """判据 #5:等渠道卡必须同时给「查渠道」和「换方案」两个出口。"""
    client = _client()
    snapshot = _plan(client, QUOTE_WITH_CAPACITY)
    waiting = [i for i in snapshot["items"]
               if i["status"]["code"] == "hold_until_domain_access_confirmed"]
    assert waiting, "喂了 mediated 渠道却没有一张等渠道卡"
    ids = {a["action_id"] for a in waiting[0]["actions"]}
    assert {"open_media_library", "mark_domain_unavailable"} <= ids


def test_duplicate_coverage_says_it_saved_a_slot_not_that_it_failed(db, no_media_recommender):
    """判据 #4:重复覆盖 = 省额度,不是失败。tone 不得是 red。"""
    from services.gap_operation_labels import translate_status

    merged = translate_status("rejected_duplicate_coverage")
    assert "省下一篇额度" in merged["label"]
    assert merged["tone"] == "neutral"
    assert "失败" not in merged["explanation"]


# ════════════════════════════════════════════════════════════════
# 合同判据 #7 + R9 · 证据链
# ════════════════════════════════════════════════════════════════

def test_publication_link_lights_only_published_and_queues_7_14_30(db, no_media_recommender):
    client = _client()
    snapshot = _plan(client, QUOTE_WITH_CAPACITY)
    pid = snapshot["items"][0]["plan_item_id"]

    resp = client.post(
        f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{pid}/publication-link",
        json={"publication_url": "https://www.cnblogs.com/real-post", "idempotency_key": "k1"},
    )
    assert resp.status_code == 200, resp.text
    ev = resp.json()["evidence"][pid]

    done = {s["key"] for s in ev["stages"] if s["done"]}
    assert done == {"published"}, f"只有已发布能由人工点亮,实得 {done}"
    assert sorted(c["due_day"] for c in ev["checkback_schedule"]) == [7, 14, 30]
    # R9:回查未启用时必须是「计划中」的话术,不是故障
    assert ev["notice"]
    assert "失败" not in ev["notice"] and "错误" not in ev["notice"]


def test_manual_link_cannot_light_beyond_published(db, no_media_recommender):
    """🔴 反向对照:DB CHECK 真的挡得住跳级(不是只靠代码不写那三列)。"""
    import psycopg2

    with db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO gap_plan_publications (quote_id, plan_item_id, publication_url,"
                " publication_url_normalized, published_at, evidence_published,"
                " evidence_indexed, evidence_cited)"
                " VALUES (900,'Q900-PX','u','u',NOW(),TRUE,FALSE,TRUE)"
            )


def test_publication_link_is_idempotent(db, no_media_recommender):
    client = _client()
    snapshot = _plan(client, QUOTE_WITH_CAPACITY)
    pid = snapshot["items"][0]["plan_item_id"]
    body = {"publication_url": "https://www.cnblogs.com/p", "idempotency_key": "same-key"}

    first = client.post(
        f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{pid}/publication-link", json=body)
    second = client.post(
        f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{pid}/publication-link", json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["replayed"] is True

    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM gap_plan_checkbacks WHERE plan_item_id=%s", (pid,))
        assert cur.fetchone()["n"] == 3, "重复提交不得重复建回查"


def test_invalid_link_gets_a_repair_exit(db, no_media_recommender):
    client = _client()
    snapshot = _plan(client, QUOTE_WITH_CAPACITY)
    pid = snapshot["items"][0]["plan_item_id"]
    resp = client.post(
        f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{pid}/publication-link",
        # 🔴 刻意用一个**够长**的非链接串:短串会先被 Pydantic 的 min_length 拦成
        #    FastAPI 自带的 422(detail 是 list),测不到本包的错误合同。
        json={"publication_url": "这不是一个合法的链接地址请检查"},
    )
    assert resp.status_code == 422, resp.text
    err = resp.json()["detail"]["error"]
    assert err["code"] == "publication_link_invalid"
    assert err["message"]
    assert err["primary_action"] and err["secondary_action"]


def test_too_short_link_is_also_rejected(db, no_media_recommender):
    """反向对照:模型层校验也真的在挡(两条路径分别有人守)。"""
    client = _client()
    snapshot = _plan(client, QUOTE_WITH_CAPACITY)
    pid = snapshot["items"][0]["plan_item_id"]
    resp = client.post(
        f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{pid}/publication-link",
        json={"publication_url": "短"},
    )
    assert resp.status_code == 422


# ════════════════════════════════════════════════════════════════
# A3/A6 · 标记进不去 → 换方案
# ════════════════════════════════════════════════════════════════

def test_marking_domain_unreachable_writes_back_and_recomputes(db, no_media_recommender):
    client = _client()
    snapshot = _plan(client, QUOTE_WITH_CAPACITY)
    target = next(i for i in snapshot["items"] if i["target_domain"] == "cnblogs.com")

    resp = client.post(
        f"/api/quotes/{QUOTE_WITH_CAPACITY}/delivery-plan/items/{target['plan_item_id']}/domain-access",
        json={"accessible": False, "reason": "渠道当前无法安排"},
    )
    assert resp.status_code == 200, resp.text

    # 结论写回媒体目录 → 全站受益,不是只改这一张卡
    with db.cursor() as cur:
        cur.execute("SELECT entry_assessment FROM media_outlets WHERE platform='cnblogs.com'")
        assert cur.fetchone()["entry_assessment"] == "unreachable"

    # 新快照代际必须前进(源事实变了)
    fresh = resp.json()["snapshot"]
    assert fresh["authority_generation"] > snapshot["authority_generation"]


# ════════════════════════════════════════════════════════════════
# A1 · 三入口消费同一份
# ════════════════════════════════════════════════════════════════

def test_same_facts_reuse_the_same_snapshot_id(db, no_media_recommender):
    """源事实没变 → 两次调用拿到同一个 snapshot_id,而不是各算一份。"""
    client = _client()
    first = _plan(client, QUOTE_WITH_CAPACITY)
    second = _plan(client, QUOTE_WITH_CAPACITY)
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["authority_generation"] == second["authority_generation"]


def test_snapshot_records_which_capacity_source_it_used(db, no_media_recommender):
    """口径明示:半年后必须能复现「当时为什么判成容量 0」。"""
    client = _client()
    snapshot = _plan(client, QUOTE_ZERO_CAPACITY)
    # 口径来源 = 合同版本号,不再是某张表名
    assert snapshot["capacity"]["capacity_source"] == (
        "services.article_capacity_contract@article-capacity-v1")
    # 篇数是**上限**不是完成率(合同 semantics),这句必须能传到前端
    assert snapshot["capacity"]["semantics"] == "upper_bound_0_to_capacity"
    assert snapshot["summary"]["capacity_semantics"] == "upper_bound_0_to_capacity"


# ════════════════════════════════════════════════════════════════
# 合同 §13.3 · 报价写链不属于 P4
# ════════════════════════════════════════════════════════════════

def test_package_registers_no_quote_mutation_route():
    """P4 禁止新增任何报价 mutation / 容量调整 / 订单 / 资金端点。"""
    from api.gap_plan_api import gap_plan_route_paths

    paths = gap_plan_route_paths()
    assert paths, "路由表是空的,这条判据没有东西可查"
    for p in paths:
        assert "capacity" not in p, p
        assert "order" not in p, p
        assert "recharge" not in p and "wallet" not in p and "points" not in p, p
    # 反向对照:确实注册了本包该有的路径
    assert any(p.endswith("/delivery-plan") for p in paths)


def test_capacity_display_status_is_one_of_p1_three_states(db, no_media_recommender):
    """🔴 display_status 只许是 P1 三态。

    并轨前我自己加过第四态 `capacity_unpaid`(报价尚未收款)—— 那就是第二套口径:
    未付款该由合同折算成 capacity_zero,P4 再判一次付款状态等于两处各判一次。
    字典里那条已删,这条锁钉死不许回来。
    """
    from services import article_capacity_contract as cc

    client = _client()
    for qid in (QUOTE_ZERO_CAPACITY, QUOTE_WITH_CAPACITY):
        status = _plan(client, qid)["capacity"]["display_status"]
        assert status in cc.CAPACITY_STATUSES, status
    assert len(cc.CAPACITY_STATUSES) == 3
    # 反向对照:第四态确实已经从字典里消失(留着就会有人再用)
    from services.gap_operation_labels import known_status_codes
    assert "capacity_unpaid" not in known_status_codes()


def test_capacity_source_comes_from_the_data_not_a_constant():
    """🔴 口径出处必须从**真实返回值**派生(Review 2026-08-08 指出)。

    写成模块常量的话,谁把 compute_capacity 改回自算,capacity_source 照样印
    "用了 P1 合同" —— 落库的那行审计就是一句**永远不会被发现**的假话。
    """
    from services import article_capacity_contract as cc
    from services.gap_operation_plan import Capacity

    view = cc.build_capacity_view(
        authorized_articles=5, consumed_articles=1, quote_is_payable=True)
    assert Capacity(view).source.endswith(view["contract_version"])

    # 合同哪天升版本,source 必须跟着变(证明它不是写死的)
    bumped = {**view, "contract_version": "article-capacity-v9"}
    assert Capacity(bumped).source.endswith("article-capacity-v9")

    # 反向对照:没有版本串 = 这份容量不是合同给的 → 构造即炸,不许放行
    import pytest as _pytest
    with _pytest.raises(ValueError):
        Capacity({k: v for k, v in view.items() if k != "contract_version"})


def test_unknown_contract_status_is_fail_closed():
    """合同若给出三态之外的值 → 当场炸,不 fallback 成"可写"。

    🔴 载荷**必须带合法的 contract_version** —— 否则另一道守卫(出处检查)会先炸,
       这条用例就有了两个失败理由,分不出是哪道守卫在守。
       变异 M12(拆掉状态检查)当场证伪了第一版:它存活,不是锁弱,
       是我的用例被另一道守卫顶了包。
    """
    from services import article_capacity_contract as cc
    from services.gap_operation_plan import Capacity

    good = cc.build_capacity_view(
        authorized_articles=5, consumed_articles=1, quote_is_payable=True)
    Capacity(good)                                    # 反向对照:合法载荷必须放行

    with pytest.raises(ValueError):
        Capacity({**good, "display_status": "capacity_whatever"})


def test_shortfall_reasons_flow_back_to_the_contract(db, no_media_recommender):
    """🔴 用不满不是失败,但**必须说得出为什么**。

    合同原文:P4 负责给 reasons;没给 → capacity_shortfall_unexplained,
    不许静默当成"已完成"。这条锁证明 P4 真的把缺口态翻成了合同的原因词。
    """
    from services import article_capacity_contract as cc
    from services.gap_operation_plan import Capacity, shortfall_for

    cap = Capacity(cc.build_capacity_view(
        authorized_articles=5, consumed_articles=1, quote_is_payable=True))
    out = shortfall_for(cap, [
        {"allocation_code": "hold_until_domain_access_confirmed"},
        {"allocation_code": "rejected_duplicate_coverage"},
    ])
    assert out["shortfall_articles"] == 4
    assert out["counts_as_failure"] is False
    assert set(out["reasons"]) == {
        cc.SHORTFALL_REASON_DOMAIN_BLOCKED, cc.SHORTFALL_REASON_DUPLICATE_COVERAGE}

    # 反向对照:说不出原因时必须留下 unexplained,不能是空列表
    silent = shortfall_for(cap, [{"allocation_code": "ready_to_execute"}])
    assert silent["reasons"] == [cc.SHORTFALL_REASON_UNEXPLAINED]


def test_placeholder_action_is_not_executable():
    from services.gap_operation_labels import translate_action

    action = translate_action("request_quote_capacity")
    assert action["enabled"] is False
    assert action["implementation_phase"] == "P1"
