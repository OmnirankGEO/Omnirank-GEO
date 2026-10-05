"""#185 c2 判据 · 端点层 —— 真库,真 UPDATE,真回显。

本文件跑的是 WO C1′ 那条主判据(「17 篇全部设为防御型再重生成标题」)以及
Review 判据①②(落库不折叠 / 文章级方向经 topic join 派生)。

🔴 [c3 改判] 上面这句原来写的是「全部防御型那条根本不进 LLM,不用打桩就能跑」——
   c3 之后**作废**:Owner 要求防御题也过 AI,于是这里每一条都要打桩。
   不打桩的后果不是红,是**静默走降级**:`get_or_build_playbook` 会把
   出网异常吞掉(fail-soft),判据照样绿,而它验的是兜底路径不是正常路径。
   2026-09-15 conftest 加了 `_no_real_network`(teardown 断言),
   当场点出 6 条判据一直在真连 api.deepseek.com。
"""
import asyncio
import types

import pytest

from .conftest import conn

QUOTE_ID = 900185
BRAND_ID = 900185
BRAND_NAME = "康之康"


def _seed(n_topics, *, user_choice=None, fixed_ids=(), brand_name=BRAND_NAME,
          profile=None):
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("INSERT INTO brands (id, name) VALUES (%s,%s)", (BRAND_ID, brand_name))
        cur.execute(
            "INSERT INTO quotes (id, brand_id, brand_name, industry, owner_user_id)"
            " VALUES (%s,%s,%s,%s,%s)",
            (QUOTE_ID, BRAND_ID, brand_name, "教育培训", 1))
        if profile is not None:
            cur.execute(
                "INSERT INTO client_profiles (id, name, brand_id, company_intro)"
                " VALUES (%s,%s,%s,%s)",
                ("cp-900185", brand_name, BRAND_ID, profile))
        for i in range(1, n_topics + 1):
            # 🔴 [#185 c3] `keyword_id` **必须真有值**:重生成那条路按
            #    `(keyword_id, slot_index)` 定位 LLM 回来的每一条标题,
            #    而 `keyword_id IS NULL` 会让这个键退化成 `(None, 0)` ——
            #    整批所有篇共用一个格子,标题互相覆盖,判据却照样绿。
            #    生产上 topics 必挂 confirmed_keywords,夹具得长成生产那样。
            cur.execute(
                "INSERT INTO confirmed_keywords (id, quote_id, brand_id, keyword,"
                "  required_articles) VALUES (%s,%s,%s,%s,1)",
                (QUOTE_ID * 10 + i, QUOTE_ID, BRAND_ID, "原关键词%d" % i))
            cur.execute(
                "INSERT INTO topics (id, quote_id, keyword_id, original_keyword,"
                "  optimized_title, user_choice, user_choice_source, style_code,"
                "  article_style, status, is_fixed)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)",
                (QUOTE_ID * 10 + i, QUOTE_ID, QUOTE_ID * 10 + i,
                 "原关键词%d" % i, "原标题%d" % i,
                 user_choice, "manual" if user_choice else None,
                 "qa_recommendation", "证据型问答", (QUOTE_ID * 10 + i) in fixed_ids))
    finally:
        c.close()
    return [QUOTE_ID * 10 + i for i in range(1, n_topics + 1)]


def _rows():
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT id, optimized_title, user_choice, user_choice_source,"
                    " style_code, article_style FROM topics"
                    " WHERE quote_id=%s ORDER BY id", (QUOTE_ID,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        c.close()


def _admin_request():
    """admin 身份:跳过计费(`_bill_user_id=None`),把判据聚焦在行为上。"""
    return types.SimpleNamespace(
        state=types.SimpleNamespace(user={"user_id": 1, "is_admin": True}))


def _regenerate(topic_ids, **kw):
    import server
    req = server.RegenerateRequest(topic_ids=list(topic_ids), **kw)
    return asyncio.run(server.api_regenerate_titles(req, _admin_request()))


# ════════════════════════════════════════════════════════════════
# WO C1′ 主臂
# ════════════════════════════════════════════════════════════════
def test_all_defensive_converts_up_to_capacity_and_names_the_rest(llm_stub):
    """17 篇全部设为防御型 ⇒ 14 篇公司词题,剩 3 篇**原标题逐字节不动**且被点名。

    这条同时钉三件事:
      · 转成功的每条都含品牌名、八问各一(前 8 条);
      · 超容量的篇**一个字都没动** —— 静默换成普通题的话这里会红;
      · 响应带 capacity / applied / unconverted 三件套。
    """
    llm_stub()          # [c3'] 不打桩就会真出网,验的是 fail-soft 降级路径
    from writing.defensive_questions import DEFENSIVE_QUESTIONS, defensive_capacity

    ids = _seed(17)
    before = {r["id"]: r["optimized_title"] for r in _rows()}

    resp = _regenerate(ids, user_choice="defensive_company")

    cap = defensive_capacity()
    assert resp["defensive_capacity"] == cap
    assert len(resp["defensive_applied"]) == cap
    assert len(resp["defensive_unconverted"]) == 17 - cap
    assert {r["reason"] for r in resp["defensive_unconverted"]} == {"capacity_exceeded"}

    after = _rows()
    changed = [r for r in after if r["optimized_title"] != before[r["id"]]]
    unchanged = [r for r in after if r["optimized_title"] == before[r["id"]]]
    assert len(changed) == cap and len(unchanged) == 17 - cap

    for r in changed:
        assert BRAND_NAME in r["optimized_title"], (
            "防御题 %r 没有品牌名" % r["optimized_title"])
    assert len({r["optimized_title"] for r in changed}) == cap, "出现重复标题"

    # 前 8 条覆盖八问各一
    first8 = [r["question"] for r in resp["defensive_applied"][:8]]
    assert first8 == list(DEFENSIVE_QUESTIONS)

    # 未转换的那几篇:方向也没被动
    untouched_ids = {r["topic_id"] for r in resp["defensive_unconverted"]}
    for r in after:
        if r["id"] in untouched_ids:
            assert r["user_choice"] is None, "未转换的篇被改了方向"


def test_user_choice_persists_unfolded_in_the_database(llm_stub):
    """Review 判据①:落库的是 `defensive_company`,不是 `company_facts`。

    这条必须查**库里的值**,不是查函数返回值 —— 中间任何一跳把它折掉,
    函数层的判据照样绿。
    """
    llm_stub()          # [c3'] 不打桩就会真出网,验的是 fail-soft 降级路径
    ids = _seed(3)
    _regenerate(ids, user_choice="defensive_company")
    got = {r["user_choice"] for r in _rows()}
    assert got == {"defensive_company"}, "落库值被折叠成了 %r" % got


def test_style_code_stays_identical_to_a_plain_company_facts_topic():
    """反向对照:防御篇的 style_code 与普通 company_facts 篇**相同**。

    相同是有意的(共用六段模板)。这条在这里,是为了让下一条
    (`is_defensive` 必须另有依据)不是空话 —— 没有它,
    「按 style_code 派生」这个错解法读起来也像成立。
    """
    from writing.style_registry import resolve_user_choice
    assert (resolve_user_choice("defensive_company", "教育培训")
            == resolve_user_choice("company_facts", "教育培训"))


def test_placement_articles_derives_direction_from_the_topic_not_style_code():
    """Review 判据②:文章级方向经 topic join 由 `user_choice` 派生。

    夹具里两篇的 `style_code` **逐字相同**,只有 `user_choice` 不同 ——
    按 style_code 派生的实现会把两者合并,这条就红。
    """
    from services.placement_service import PlacementService

    _seed(2)
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("UPDATE topics SET status='completed', style_code='brand_softarticle',"
                    " user_choice='defensive_company' WHERE id=%s", (QUOTE_ID * 10 + 1,))
        cur.execute("UPDATE topics SET status='completed', style_code='brand_softarticle',"
                    " user_choice='company_facts' WHERE id=%s", (QUOTE_ID * 10 + 2,))
    finally:
        c.close()

    rows = PlacementService().get_completed_articles(QUOTE_ID)
    by_id = {r["topic_id"]: r for r in rows}
    assert len(by_id) == 2

    a, b = by_id[QUOTE_ID * 10 + 1], by_id[QUOTE_ID * 10 + 2]
    assert a["style_code"] == b["style_code"], "夹具前提没成立:两篇 style_code 应当相同"
    assert a["is_defensive"] is True
    assert b["is_defensive"] is False
    assert a["user_choice"] == "defensive_company"


def _stub_llm(monkeypatch, seen):
    """把 LLM 换成桩。

    🔴 打在 `writing.keyword_topic_generator` 上,**不是** `server` 上:
       端点里是**函数内** `from writing.keyword_topic_generator import
       KeywordTopicGenerator`,每次调用都去源模块取名字 —— 打在 server 上
       完全无效,而无效的桩**看起来跟生效一模一样**(判据照样绿),
       区别只是它会去真的调一次 LLM。
    """
    class _StubGen:
        def __init__(self, **kw):
            seen["keywords"] = [k.get("keyword") for k in (kw.get("keywords") or [])]
            seen["n"] = sum(int(k.get("required_articles") or 0)
                            for k in (kw.get("keywords") or []))

        async def generate(self):
            seen["called"] = True
            return []      # 无产出 -> 端点 502。这里只关心"进没进 LLM 以及带了谁"

    monkeypatch.setattr("writing.keyword_topic_generator.KeywordTopicGenerator", _StubGen)
    return seen


def test_topics_without_the_defensive_direction_are_left_alone(monkeypatch):
    """反向对照:不带 user_choice 的老调用**照旧进 LLM**,不进防御支路。

    少了它,「无论传什么都走防御」也能让主臂全绿 ——
    而那会把所有重写标题的老入口一起改掉。
    """
    ids = _seed(2)
    before = {r["id"]: r["optimized_title"] for r in _rows()}
    seen = _stub_llm(monkeypatch, {})

    with pytest.raises(Exception):
        _regenerate(ids)

    assert seen.get("called") is True, "桩没被调到 —— 这条判据什么都没验(可能真调了 LLM)"
    assert seen["n"] == 2, "老调用应当把两篇都交给 LLM"
    assert [r["optimized_title"] for r in _rows()] == [before[i] for i in ids]


def test_mixed_batch_keeps_the_others_out_of_the_defensive_path(llm_stub, monkeypatch):
    """混合:2 篇防御 + 1 篇别的 ⇒ 只有那 2 篇被改成公司词题。

    🔴 [#185 c3 改判] 本条原来钉的是「防御那两篇**没送去** LLM」。
       c3 之后 Owner 要求防御题也过 AI,那句话已经作废 —— 三篇都进 LLM 批。
       但**这条判据要保住的那件事没变**:LLM 这一趟一无所获时(端点 502),
       防御那两篇仍然落库。它们的兜底是模板题、根本不依赖 LLM,
       不该被 LLM 的失败连坐。分两次提交是有意的,写在端点注释里;
       钉住它,免得下一个人以为是 bug 而"顺手改成全有全无"。
    """
    llm_stub()          # [c3'] 不打桩就会真出网,验的是 fail-soft 降级路径
    ids = _seed(3)
    seen = _stub_llm(monkeypatch, {})
    plan = [{"topic_id": ids[0], "user_choice": "defensive_company"},
            {"topic_id": ids[1], "user_choice": "defensive_company"},
            {"topic_id": ids[2], "user_choice": "evidence_qa"}]
    with pytest.raises(Exception):
        _regenerate(ids, style_plan=plan)

    # c3:三篇一起进 LLM 批(防御篇也要过 AI),而不是只送第三篇。
    assert seen.get("called") is True, "桩没被调到 —— 可能真调了 LLM"
    assert seen["n"] == 3, "c3 之后防御篇也要进 LLM 批,实到 %s 篇" % seen["n"]
    assert seen["keywords"] == ["原关键词1", "原关键词2", "原关键词3"]

    rows = {r["id"]: r for r in _rows()}
    assert BRAND_NAME in rows[ids[0]]["optimized_title"]
    assert BRAND_NAME in rows[ids[1]]["optimized_title"]
    assert rows[ids[2]]["optimized_title"] == "原标题3"
    assert rows[ids[0]]["user_choice"] == "defensive_company"
    assert rows[ids[2]]["user_choice"] is None


def test_fixed_slot_topic_is_reported_not_rewritten(llm_stub):
    """固定槽被点名、标题不动(Review 09-13 裁定:不动固定槽)。"""
    llm_stub()          # [c3'] 不打桩就会真出网,验的是 fail-soft 降级路径
    ids = _seed(3, fixed_ids=(QUOTE_ID * 10 + 2,))
    resp = _regenerate(ids, user_choice="defensive_company")
    assert [r["reason"] for r in resp["defensive_unconverted"]] == ["fixed_slot"]
    rows = {r["id"]: r for r in _rows()}
    assert rows[QUOTE_ID * 10 + 2]["optimized_title"] == "原标题2"
    assert len(resp["defensive_applied"]) == 2


def test_facts_hint_rides_along_with_each_defensive_topic(llm_stub):
    """c5:每条防御题带**按问算**的缺事实提示,只显示不阻断。"""
    llm_stub()          # [c3'] 不打桩就会真出网,验的是 fail-soft 降级路径
    ids = _seed(3, profile="我们是一家做成人教育的公司…")
    resp = _regenerate(ids, user_choice="defensive_company")
    hints = {r["question"]: r["facts_hint"] for r in resp["defensive_applied"]}
    assert hints["怎么样"]["has_facts"] is True
    assert hints["靠谱吗·口碑"]["has_facts"] is False
    assert "客户评价" in hints["靠谱吗·口碑"]["hint"]


def test_brand_without_a_name_converts_nothing_and_says_why(llm_stub):
    """没有品牌名 ⇒ 一篇都不转,逐篇点名 not_retitlable,标题一个字不动。"""
    llm_stub()          # [c3'] 不打桩就会真出网,验的是 fail-soft 降级路径
    ids = _seed(2, brand_name="")
    before = {r["id"]: r["optimized_title"] for r in _rows()}
    resp = _regenerate(ids, user_choice="defensive_company")
    assert resp["defensive_applied"] == []
    assert [r["reason"] for r in resp["defensive_unconverted"]] == ["not_retitlable"] * 2
    assert {r["id"]: r["optimized_title"] for r in _rows()} == before
