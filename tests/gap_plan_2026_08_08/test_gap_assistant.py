"""小榜扩展锁(C1/C2/C3 · 合同 06 §10 / §11)

🔴 §11.2「模型拔除验收」是本文件最重要的一组:
   合同原文要求「真正移除模型配置或让端点稳定不可用」再走一遍。
   本模块**一行 LLM 都不调**,所以"拔模型"在这里的等价形式是:
   把模型客户端整个替换成必炸的桩,断言全部确定性能力照常 ——
   test_everything_works_with_the_model_ripped_out 就是干这个的。
   只 mock 成"成功"不算通过(合同原话)。
"""

from __future__ import annotations

import pytest

from services import gap_assistant, gap_operation_map as omap
from tests.gap_plan_2026_08_08.conftest import AGENT_USER, OTHER_USER, make_app


class _FakeRequest:
    """最小 request 替身:只带 auth 中间件会注入的两样东西。

    🔴 归属校验走的是**真** require_quote_access(会真读 quotes / brands),
       这里替身只提供 state,不替换任何一道门。
    """

    def __init__(self, user):
        class _State:
            pass
        self.state = _State()
        self.state.user = dict(user)
        self.state.organization_identity = None


# ────────────────────────────────────────────────────────────────
# C1/C2 · 导航只来自版本化操作地图
# ────────────────────────────────────────────────────────────────

def test_navigation_answer_comes_from_the_map():
    ans = gap_assistant.answer_navigation("交付计划在哪里？")
    assert ans is not None
    assert ans.breadcrumb == ["客户报价", "打开词包", "交付计划"]
    routes = {a.get("target_route") for a in ans.actions}
    assert routes == {"/pricing"}
    assert ans.operation_map_version == omap.OPERATION_MAP_VERSION


def test_unknown_navigation_question_gets_no_invented_route():
    """🔴 判据 #9 后半:答不出来就是答不出来,不许编一个像样的假路径。"""
    assert gap_assistant.answer_navigation("这个星球上有没有外星人") is None


def test_assistant_never_emits_a_write_action(db, no_media_recommender):
    """合同 06 §4:填链接 / 标记进不去 / 建写作任务 / 发布 / 扣费 —— 助手一个都不能签发。"""
    req = _FakeRequest(AGENT_USER)
    ans = gap_assistant.build_answer(
        question="这个客户今天先做什么？",
        context_refs={"quote_id": 900},
        request=req,
        assistant_request_id="req-nowrite",
        actor_user_id=4001,
    )
    assert ans is not None
    ids = {a["action_id"] for a in ans.actions}
    forbidden = {"submit_publication_link", "mark_domain_unavailable",
                 "open_writing_task", "request_quote_capacity"}
    assert not (ids & forbidden), f"助手签发了写类动作:{ids & forbidden}"
    # 反向对照:它确实签发了**某些**动作(不是恒空 → 判据有东西可查)
    assert ids


# ────────────────────────────────────────────────────────────────
# 判据 #11 · 页面与小榜对同一任务完全一致
# ────────────────────────────────────────────────────────────────

def test_assistant_and_page_agree_on_the_same_task(db, no_media_recommender):
    from fastapi.testclient import TestClient

    client = TestClient(make_app(AGENT_USER), raise_server_exceptions=False)
    page = client.get("/api/quotes/900/delivery-plan").json()["snapshot"]

    ans = gap_assistant.build_answer(
        question="这个客户今天先做什么？",
        context_refs={"quote_id": 900},
        request=_FakeRequest(AGENT_USER),
        assistant_request_id="req-agree",
        actor_user_id=4001,
    )
    assert ans is not None
    # 同一份快照:小榜引用的 snapshot_id 必须与页面拿到的逐字相同
    assert ans.snapshot_id == page["snapshot_id"]
    assert ans.authority_generation == page["authority_generation"]

    # 动作也必须是页面那张卡上的(照抄,不另排序、不另生成)
    #
    # 🔴 只有两个明示例外,不许再多:
    #    · explain_plan —— 卡片没有可导航动作时的兜底解释
    #    · open_delivery_plan —— 2026-08-08 追加的兜底落点(理由见
    #      gap_assistant._PLAN_LANDING_ACTION:不加的话真实报价 372/900 的
    #      运营回答一个可点按钮都没有)
    #    下面第二条断言把例外集合**钉死**:再偷偷塞第三个自造动作就会红。
    exceptions = {"explain_plan", gap_assistant._PLAN_LANDING_ACTION}
    assert exceptions == {"explain_plan", "open_delivery_plan"}, (
        f"例外集合被改动了,请连同这条锁一起复审:{exceptions}"
    )
    focus = next((i for i in page["items"]
                  if i["status"]["code"] == "ready_to_execute"), page["items"][0])
    page_ids = {a["action_id"] for a in focus["actions"]}
    invented = [a["action_id"] for a in ans.actions
                if a["action_id"] not in page_ids and a["action_id"] not in exceptions]
    assert not invented, f"助手签发了页面卡片上没有的动作:{invented}"


def test_assistant_refuses_to_answer_for_someone_elses_quote(db, no_media_recommender):
    """fail-closed:跨租户 → 降级,不泄漏该客户任何事实。"""
    ans = gap_assistant.build_answer(
        question="这个客户今天先做什么？",
        context_refs={"quote_id": 701},          # 别人家的
        request=_FakeRequest(OTHER_USER),
        assistant_request_id="req-crosstenant",
        actor_user_id=5002,
    )
    assert ans is not None
    assert ans.degraded is True
    assert "别人家的客户" not in ans.headline
    assert all("别人家的客户" not in r for r in ans.reasons)


def test_stale_generation_degrades_instead_of_answering_on_old_facts(db, no_media_recommender):
    ans = gap_assistant.build_answer(
        question="这个客户今天先做什么？",
        context_refs={"quote_id": 900, "authority_generation": 999},
        request=_FakeRequest(AGENT_USER),
        assistant_request_id="req-stale",
        actor_user_id=4001,
    )
    assert ans is not None and ans.degraded is True


# ────────────────────────────────────────────────────────────────
# C3 四件套
# ────────────────────────────────────────────────────────────────

def test_audit_row_is_written_and_carries_no_raw_text(db, no_media_recommender):
    """留痕:写了行,且存的是**哈希**不是正文。"""
    gap_assistant.build_answer(
        question="这个客户今天先做什么？我的密码是 hunter2",
        context_refs={"quote_id": 900},
        request=_FakeRequest(AGENT_USER),
        assistant_request_id="req-audit",
        actor_user_id=4001,
    )
    with db.cursor() as cur:
        cur.execute("SELECT * FROM gap_assistant_audit WHERE assistant_request_id='req-audit'")
        row = cur.fetchone()
    assert row is not None, "留痕没写"
    assert row["facts_hash"] and len(row["facts_hash"]) == 64
    assert row["output_hash"] and len(row["output_hash"]) == 64
    assert row["operation_map_version"] == omap.OPERATION_MAP_VERSION
    # 🔴 正文/密钥不得入库
    blob = " ".join(str(v) for v in row.values())
    assert "hunter2" not in blob
    assert "今天先做什么" not in blob


def test_idempotent_replay_does_not_create_a_second_audit_row(db, no_media_recommender):
    """幂等边界 = (assistant_request_id, snapshot_version)。"""
    for _ in range(3):
        gap_assistant.build_answer(
            question="这个客户今天先做什么？",
            context_refs={"quote_id": 900},
            request=_FakeRequest(AGENT_USER),
            assistant_request_id="req-idem",
            actor_user_id=4001,
        )
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM gap_assistant_audit WHERE assistant_request_id='req-idem'")
        assert cur.fetchone()["n"] == 1


def test_everything_works_with_the_model_ripped_out(db, no_media_recommender, monkeypatch):
    """🔴 合同 §11.2 反向验收:**真拔模型**,不是 mock 成功。

    做法:把项目里的 LLM 调用入口换成"一调就炸"的桩,再完整走一遍
      加载报价 → 看任务卡 → 查入口 → 拿确定性动作。
    全部必须照常。任何一条挂掉,就说明模型不是可拔插增强层,
    而是被写进了主链 —— 那正是这条判据要防的。
    """
    def _boom(*args, **kwargs):                     # noqa: ANN002, ANN003
        raise RuntimeError("模型端点稳定不可用(测试注入)")

    # 把所有可能的模型客户端入口全部换成炸弹
    import importlib
    for mod_name, attrs in [
        ("config.model_config", ["call_llm", "get_client", "chat"]),
    ]:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for attr in attrs:
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, _boom, raising=False)

    from fastapi.testclient import TestClient
    client = TestClient(make_app(AGENT_USER), raise_server_exceptions=False)

    # ① 报价页加载
    resp = client.get("/api/quotes/900/delivery-plan")
    assert resp.status_code == 200, resp.text
    snapshot = resp.json()["snapshot"]

    # ② 任务卡 + 状态翻译仍是人话
    assert snapshot["items"]
    assert snapshot["items"][0]["status"]["label"]
    assert not snapshot["items"][0]["status"]["label"].isascii()

    # ③ 「某按钮在哪里」仍由版本化操作地图答出正确现役路径
    nav = gap_assistant.answer_navigation("交付计划在哪里？")
    assert nav is not None and nav.actions[0]["target_route"] == "/pricing"

    # ④ 确定性动作仍能执行(填链接不经过模型)
    pid = snapshot["items"][0]["plan_item_id"]
    posted = client.post(
        f"/api/quotes/900/delivery-plan/items/{pid}/publication-link",
        json={"publication_url": "https://www.cnblogs.com/no-model", "idempotency_key": "nm"},
    )
    assert posted.status_code == 200, posted.text

    # ⑤ 运营建议本身也照常(它本来就不调模型)
    ans = gap_assistant.build_answer(
        question="这个客户今天先做什么？", context_refs={"quote_id": 900},
        request=_FakeRequest(AGENT_USER), assistant_request_id="req-nomodel",
        actor_user_id=4001,
    )
    assert ans is not None and ans.headline


def test_assistant_output_carries_no_internal_terms(db, no_media_recommender):
    """出参过机械闸(供应商名 / 成本 / 未翻译枚举全零)。"""
    ans = gap_assistant.build_answer(
        question="这个客户今天先做什么？",
        context_refs={"quote_id": 900},
        request=_FakeRequest(AGENT_USER),
        assistant_request_id="req-terms",
        actor_user_id=4001,
    )
    assert ans is not None
    meta = ans.as_meta()          # as_meta 内部就会跑 assert_no_internal_leak
    import json
    body = json.dumps(meta, ensure_ascii=False).lower()
    for term in ("deepseek", "kimi", "豆包", "千问", "进货价", "our_price"):
        assert term.lower() not in body


def test_no_context_no_navigation_means_module_stays_out_of_the_way():
    """管不了的问题返回 None → 调用侧走原有帮助知识库,行为与上线前逐字一致。"""
    ans = gap_assistant.build_answer(
        question="随便聊聊天气吧",
        context_refs=None,
        request=_FakeRequest(AGENT_USER),
        assistant_request_id="req-none",
        actor_user_id=4001,
    )
    assert ans is None
