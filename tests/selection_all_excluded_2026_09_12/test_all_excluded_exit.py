"""#178 判据 —— 全部行为臂:每条都真发请求 / 真读库,不 grep 源码。

判别力靠一组**实测过**的词(见 conftest 顶部):
  knowledge    传不传品牌名都不可交付
  brand_direct **只有**传品牌名才可交付   ← 本单的判别力全在这一行
  commercial   传不传都可交付             ← 反向对照
少了最后一条,"全绿"可能只是因为什么都放行了。
"""
import json

import pytest

from .conftest import (
    BRAND_NAME, KW_BRAND_DIRECT, KW_COMMERCIAL, KW_KNOWLEDGE,
    clear_outbox, make_keywords, outbox_rows, read_session, seed_session,
)

KNOWLEDGE_IDS = [1, 2, 3]


def _submit_lines(client, token, ids=(1,)):
    return client.post(f"/api/s/{token}/submit-business-lines",
                       json={"selected_business_line_ids": list(ids)})


def _submit_keywords(client, token, selected_ids=(), custom=()):
    return client.post(f"/api/s/{token}/submit-keywords",
                       json={"selected_ids": list(selected_ids),
                             "custom_keywords": list(custom)})


# ── C1 · 全知识词业务线 ⇒ 200 结构化,不是无出口的 400 ──────────────────
def test_c1_all_knowledge_business_line_returns_structured_200(client):
    token = seed_session("c178-c1", keywords=make_keywords())
    clear_outbox(token)
    res = _submit_lines(client, token)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["all_excluded"] is True
    assert body["status"] == "business_lines_submitted"
    assert body["keywords_auto_selected"] == 0
    assert body["next_action"]["kind"] == "add_commercial_keywords"

    excluded = body["delivery_excluded_keywords"]
    assert sorted(item["id"] for item in excluded) == KNOWLEDGE_IDS
    for item in excluded:
        # 逐条原因不是可选项:SSOT §3.3「绝不静默缩减」。
        assert item["reason"].strip(), item
        assert item["kind"] in ("knowledge_term_not_deliverable", "needs_clarification"), item

    row = read_session(token)
    assert row["status"] == "business_lines_submitted"
    assert (json.loads(row["selected_keyword_ids"]) if row["selected_keyword_ids"] else []) == []
    # keywords_submitted_at 代表"选词完成" —— 这里没完成,盖上就是假记录。
    assert row["keywords_submitted_at"] is None


# ── C2 · 品牌直问活下来,**因为**品牌名喂进了引擎 ───────────────────────
def test_c2_brand_direct_keyword_survives_and_knowledge_still_excluded(client):
    token = seed_session("c178-c2",
                         keywords=make_keywords(include_brand_direct=True, include_commercial=True))
    res = _submit_lines(client, token)
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["all_excluded"] is False
    # 品牌直问在:不传 brand_name 时它是 uncertain ⇒ 会被当"需澄清"剔掉。
    assert 90 not in [item["id"] for item in body["delivery_excluded_keywords"]]
    # 反向对照:知识词照剔。少了这条,"全绿"可能只是因为什么都放行。
    assert sorted(item["id"] for item in body["delivery_excluded_keywords"]) == KNOWLEDGE_IDS

    kept = json.loads(read_session(token)["selected_keyword_ids"])
    assert 90 in kept and 91 in kept


def test_c2b_custom_brand_direct_keyword_is_kept_and_knowledge_custom_is_not(client):
    # selected_ids 在请求模型上是 min_length=1(空数组直接 422),所以必须带一个
    # 可交付的选中词;顺便让这条测试只盯自定义词那条路。
    token = seed_session("c178-c2b", keywords=make_keywords(include_commercial=True))
    res = _submit_keywords(client, token, selected_ids=[91],
                           custom=[KW_BRAND_DIRECT, "钢琴的历史"])
    assert res.status_code == 200, res.text
    body = res.json()

    excluded_custom = [item["keyword"] for item in body["delivery_excluded_custom_keywords"]]
    assert KW_BRAND_DIRECT not in excluded_custom
    assert "钢琴的历史" in excluded_custom          # 反向对照
    assert body["all_excluded"] is False
    assert json.loads(read_session(token)["custom_keywords"]) == [KW_BRAND_DIRECT]


# ── C3 · 通知恰好一条,重放不加第二条 ──────────────────────────────────
def test_c3_no_deliverable_notice_enqueued_exactly_once_and_replay_adds_none(client):
    token = seed_session("c178-c3", keywords=make_keywords())
    clear_outbox(token)

    first = _submit_lines(client, token)
    assert first.status_code == 200
    rows = outbox_rows(token)
    assert len(rows) == 1, rows
    assert first.json()["next_action"]["notify_sent"] is True
    # 标题要说得出"要你做什么" —— 复用 business.action_required 就说不出。
    assert rows[0]["route"] == "/online-quote"

    second = _submit_lines(client, token)
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert len(outbox_rows(token)) == 1, outbox_rows(token)
    # 重放分支自己不推,但读面必须仍然说"已通知"(状态从 outbox 取,不是从本次调用推出来)。
    assert second.json()["next_action"]["notify_sent"] is True


# ── C4 · submit-keywords 零可交付:200 结构化 + 不落库 ────────────────
def test_c4_submit_keywords_zero_deliverable_does_not_commit(client):
    token = seed_session("c178-c4", keywords=make_keywords())
    before = read_session(token)
    res = _submit_keywords(client, token, selected_ids=KNOWLEDGE_IDS)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["all_excluded"] is True
    assert body["selected_count"] == 0
    assert body["status"] == "selecting"            # 保持原值,不是某个固定态
    assert body["next_action"]["kind"] == "add_commercial_keywords"

    after = read_session(token)
    assert after["status"] == "selecting"
    assert after["keywords_submitted_at"] is None
    assert after["selected_keyword_ids"] == before["selected_keyword_ids"]


def test_c4b_submit_keywords_zero_deliverable_twice_still_one_notice(client):
    """幂等键的**唯一**承重臂。

    business-lines 那条路第二次会走 replayed 分支、根本不推,所以那条路上
    「重放只有一条」即使幂等键坏掉也照样绿。submit-keywords 这条路每次都真推,
    两条才会因为键坏掉而出现。
    """
    token = seed_session("c178-c4b", keywords=make_keywords())
    clear_outbox(token)
    assert _submit_keywords(client, token, selected_ids=KNOWLEDGE_IDS).status_code == 200
    assert len(outbox_rows(token)) == 1, outbox_rows(token)
    assert _submit_keywords(client, token, selected_ids=KNOWLEDGE_IDS).status_code == 200
    assert len(outbox_rows(token)) == 1, outbox_rows(token)


# ── C5 · 客户侧与服务商侧对同一批词给同一个答案(本单要恢复的不变量) ──
def test_c5_customer_side_and_agent_side_agree_on_the_same_words(client):
    from services.quote_intent_gate import partition_by_commercial_policy

    words = KW_KNOWLEDGE + [KW_BRAND_DIRECT, KW_COMMERCIAL]
    quotable, excluded, _status = partition_by_commercial_policy(words, brand_name=BRAND_NAME)
    agent_excluded = {item["keyword"] for item in excluded}

    token = seed_session("c178-c5",
                         keywords=make_keywords(include_brand_direct=True, include_commercial=True))
    body = _submit_lines(client, token).json()
    customer_excluded = {item["keyword"] for item in body["delivery_excluded_keywords"]}

    assert customer_excluded == agent_excluded
    assert KW_BRAND_DIRECT in set(quotable) and KW_BRAND_DIRECT not in customer_excluded


# ── C6 · 刷新之后卡片还在(A 指出的读面缺口) ─────────────────────────
def test_c6_get_rebuilds_the_card_after_refresh(client):
    token = seed_session("c178-c6", keywords=make_keywords())
    clear_outbox(token)
    assert _submit_lines(client, token).status_code == 200

    page = client.get(f"/api/s/{token}")
    assert page.status_code == 200, page.text
    data = page.json()
    assert data["status"] == "business_lines_submitted"
    assert data["all_excluded"] is True
    assert sorted(item["id"] for item in data["delivery_excluded_keywords"]) == KNOWLEDGE_IDS
    assert all(item["reason"].strip() for item in data["delivery_excluded_keywords"])
    assert data["next_action"]["notify_sent"] is True


def test_c6b_normal_session_gets_no_all_excluded_key(client):
    """反向对照:没发生全排除时读面**一个键都不加**,否则等于给所有空态贴错标签。"""
    token = seed_session("c178-c6b", keywords=make_keywords(include_commercial=True))
    data = client.get(f"/api/s/{token}").json()
    assert "all_excluded" not in data
    assert "next_action" not in data


# ── 补发按钮:幂等 + 状态守卫 ─────────────────────────────────────────
def test_notify_endpoint_is_idempotent_and_refuses_wrong_state(client):
    token = seed_session("c178-n1", keywords=make_keywords())
    clear_outbox(token)
    assert _submit_lines(client, token).status_code == 200
    assert len(outbox_rows(token)) == 1

    again = client.post(f"/api/s/{token}/notify-no-deliverable")
    assert again.status_code == 200, again.text
    assert again.json()["already_sent"] is True
    assert len(outbox_rows(token)) == 1

    other = seed_session("c178-n2", keywords=make_keywords(include_commercial=True))
    refused = client.post(f"/api/s/{other}/notify-no-deliverable")
    assert refused.status_code == 400


# ══════════════════════════════════════════════════════════════════════
# #178-B(Review 09-12 裁定 1 + 记在我名下的 P2)
# ══════════════════════════════════════════════════════════════════════

def test_c7_business_line_without_any_keyword_is_the_same_exit_with_its_own_reason(client):
    """相邻那条 400(「选中业务线下没有可用关键词」)收进同一份契约。

    判别力在 **reason**:两种零可交付的补救动作不同 ——
    一种是"补商业选型问法",一种是"这个方向还没生成词"。
    给报价方推错的那一句,他会去找被排除的词,而那里什么都没有。
    """
    token = seed_session("c178-c7", keywords=make_keywords(line_id=1), extra_line=True)
    clear_outbox(token)
    res = _submit_lines(client, token, ids=(2,))          # 选的是**没挂任何词**的那条方向

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["all_excluded"] is True
    assert body["reason"] == "no_keywords_for_lines"
    assert body["delivery_excluded_keywords"] == []       # 本来就没有可列的词
    assert body["status"] == "business_lines_submitted"
    assert body["next_action"]["kind"] == "add_commercial_keywords"

    row = read_session(token)
    assert row["status"] == "business_lines_submitted"
    assert row["keywords_submitted_at"] is None

    rows = outbox_rows(token)
    assert len(rows) == 1, rows
    # 两种原因的报文必须分开 —— 这一条不该说"都是知识/百科类"。
    assert "知识/百科" not in rows[0]["content"]
    assert "暂无任何候选问法" in rows[0]["content"]


def test_c7b_no_keywords_reason_survives_refresh(client):
    token = seed_session("c178-c7b", keywords=make_keywords(line_id=1), extra_line=True)
    assert _submit_lines(client, token, ids=(2,)).status_code == 200
    data = client.get(f"/api/s/{token}").json()
    assert data["all_excluded"] is True
    assert data["reason"] == "no_keywords_for_lines"
    assert data["delivery_excluded_keywords"] == []


def test_c8_excluded_path_keeps_its_own_reason(client):
    """反向对照:有词但全被排除的那一档 reason 必须**不是** no_keywords_for_lines。

    少了这条,把 reason 写死成任意一个常量都能让 c7 绿。
    """
    token = seed_session("c178-c8", keywords=make_keywords())
    body = _submit_lines(client, token).json()
    assert body["all_excluded"] is True
    assert body["reason"] == "all_excluded"
    assert len(body["delivery_excluded_keywords"]) == 3
    rows_content = outbox_rows(token)[0]["content"]
    assert "知识/百科" in rows_content


def test_c9_submit_keywords_zero_deliverable_leaves_a_usable_notify_button(client):
    """[P2] 零可交付**不改状态**,但戳要落库 —— 否则补发按钮第二次被守卫挡死。

    原来这条路直接 rollback:首推通知若失败,客户点「让报价方补充」会 400,
    **第一次不阻断、第二次却挡死**。
    """
    token = seed_session("c178-c9", keywords=make_keywords())
    clear_outbox(token)
    before = read_session(token)
    assert _submit_keywords(client, token, selected_ids=KNOWLEDGE_IDS).status_code == 200

    after = read_session(token)
    # 约束逐字保持:不落 keywords_submitted、不动选择、不写提交时间
    assert after["status"] == "selecting"
    assert after["keywords_submitted_at"] is None
    assert after["selected_keyword_ids"] == before["selected_keyword_ids"]
    # 但戳落库了 —— 判定结果的唯一真相源
    stamped = [k for k in json.loads(after["keywords_snapshot"]) if k.get("delivery_exclusion")]
    assert len(stamped) == 3, stamped

    # 补发按钮活着(这是本条的承重断言)
    again = client.post(f"/api/s/{token}/notify-no-deliverable")
    assert again.status_code == 200, again.text
    assert len(outbox_rows(token)) == 1
