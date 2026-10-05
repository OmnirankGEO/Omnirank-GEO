"""#185 c3 判据 · 防御题走 LLM,八问降为把关与兜底。

Owner 2026-09-13 原话:「防御型公司词的标题生成也需要经过 AI,
要蒸馏一下研究一下防御型的这些文章怎么写。」

T1 走 LLM · T2 把关兜底 · T3 去重与容量 · T4 计费(**只钉不改**,等 Owner)
T5 playbook 缓存与 TTL · T6 正文单点注入 · T7 fail-soft · T8 sources_used 对得上

🔴 桩截在**出网那一步**,不是 `generate()`:
   桩在 `generate()` 上就拿不到生成器真正构造的 prompt,判据只能自己复现一遍
   同样的拼接再断言 —— 那是自洽不是证据。
"""
import asyncio
import types

import pytest

from tests.defensive_direction_2026_09_13.conftest import conn
from tests.defensive_direction_2026_09_13.test_defensive_endpoints import (  # noqa: E402
    BRAND_ID, BRAND_NAME as BRAND, QUOTE_ID, _regenerate, _rows, _seed)

#: `_seed` 给第 i 篇写的关键词(`test_defensive_endpoints.py:42`)。
#: 每个关键词各 1 篇 ⇒ 它在 `kw_groups` 里的 slot_index 恒为 0。
SEED_KEYWORD_1 = "原关键词1"

#: `_seed` 把 `quotes.owner_user_id` 写成 1(同文件 :30)。
#: 计费那几条判据要走**非 admin** 身份,owner 必须对得上,否则 403。
SEED_OWNER_USER_ID = 1


def _paying_request(user_id=SEED_OWNER_USER_ID):
    """普通(付费)身份 —— 与 c2 的 `_admin_request()` 相对。

    🔴 admin 走的是 `_bill_user_id=None`,**整段计费代码根本不执行**。
       用 admin 去断言"没扣钱",断言的是"计费被跳过了",不是
       "防御篇不进基数" —— 那个绿灯在把计费逻辑删光之后依然是绿的。
    """
    return types.SimpleNamespace(
        state=types.SimpleNamespace(user={"user_id": user_id, "is_admin": False}))


def _regenerate_as_payer(topic_ids, **kw):
    import server
    req = server.RegenerateRequest(topic_ids=list(topic_ids), **kw)
    return asyncio.run(server.api_regenerate_titles(req, _paying_request()))


class _BillingSpy:
    """记下计费函数**被怎么调的**:调了几次、每次 extra_cost 多少。

    只记 `called` 布尔量是不够的:「扣了 1 篇的钱」和「扣了 3 篇的钱」
    都会让它为真,而这两者正是本组判据要分开的两件事。
    """

    def __init__(self):
        self.checks = []      # [extra_cost]
        self.deducts = []     # [extra_cost]

    @property
    def charged_articles(self):
        """换算回**篇数**:`extra_cost = (n-1) * base` ⇒ n = extra/base + 1。"""
        return [e // TOPIC_BASE + 1 for e in self.deducts]


#: 判据自带的单价,避免读生产 `feature_pricing`(那张表的值随运营调,
#: 判据会莫名其妙变色)。端点读 `get_feature_pricing("topic_gen")`,桩住它。
TOPIC_BASE = 390


@pytest.fixture
def billing_spy(monkeypatch):
    import db.wallet_db as wallet_db
    import middleware.billing as billing

    spy = _BillingSpy()

    async def _check(user_id, feature_code, extra_cost=0, **kw):
        spy.checks.append(extra_cost)
        return True

    async def _deduct(user_id, feature_code, extra_cost=0, **kw):
        spy.deducts.append(extra_cost)
        return True

    # 🔴 `middleware/billing.py` 是受保护文件 —— 只在判据里桩,不改它。
    monkeypatch.setattr(billing, "check_balance_only", _check)
    monkeypatch.setattr(billing, "deduct_points", _deduct)
    monkeypatch.setattr(wallet_db, "get_feature_pricing",
                        lambda code: {"cost_points": TOPIC_BASE})
    return spy


# ════════════════════════════════════════════════════════════════
# T1 · 防御槽**走 LLM**,提示块里有品牌名 + 问名 + playbook 角度
# ════════════════════════════════════════════════════════════════
def test_defensive_slots_go_through_the_llm_with_the_right_prompt(llm_stub):
    """毒:模板直出、LLM 调用 0 ⇒ 红。

    🔴 断言的是**真正发出去**的那份 prompt(桩截在 httpx 层),
       不是判据自己拼一份一样的再比。
    """
    calls = llm_stub()
    ids = _seed(3)
    resp = _regenerate(ids, user_choice="defensive_company")

    assert calls.count >= 1, "防御槽没进 LLM —— c3 的整件事就是让它进"
    prompt = calls.prompts[0]
    assert BRAND in prompt, "提示块里没有品牌名 —— 主语丢了就不是公司词"
    assert "防御型(公司词)槽位" in prompt, "防御提示块根本没拼进去"
    # 问名逐条出现:LLM 不许自己挑问法
    questions = [d["question"] for d in calls.plans[0]]
    assert questions, "生成器没拿到 defensive_plan"
    for q in questions:
        assert q in prompt, "问名「%s」没进提示块" % q
    assert "不点名" in prompt and "排名" in prompt, "缺「不点名不排名同行」这一条"
    assert len(resp["defensive_applied"]) == 3


def test_the_question_assignment_comes_from_the_plan_not_from_the_llm(llm_stub):
    """反向对照:问名由上游定死,LLM 换了问法也不改 `question` 字段。

    少了它,「八问覆盖」就没有分母 —— LLM 会挑好写的那几问,
    而用户看到的只有"防御型"三个字。
    """
    calls = llm_stub(lambda d: "%s随便写点别的" % BRAND)
    ids = _seed(3)
    resp = _regenerate(ids, user_choice="defensive_company")
    from writing.defensive_questions import DEFENSIVE_QUESTIONS

    got = [r["question"] for r in resp["defensive_applied"]]
    assert got == list(DEFENSIVE_QUESTIONS[:3]), (
        "问名被 LLM 的题面带跑了:%s" % got)


# ════════════════════════════════════════════════════════════════
# T2 · 把关:缺品牌名 ⇒ 落模板 + source=template_fallback
# ════════════════════════════════════════════════════════════════
def test_a_title_without_the_brand_falls_back_to_the_template(llm_stub):
    """毒:放行 ⇒ 红。

    🔴 同时钉 `source` 与 `fallback_reason`:静默兜底是最坏的一种 ——
       用户以为 AI 写了、实际是模板,而两者长得一样。
    """
    calls = llm_stub(lambda d: "这家公司%s" % d["question"])     # 故意不含品牌名
    ids = _seed(2)
    resp = _regenerate(ids, user_choice="defensive_company")

    assert calls.count >= 1
    for row in resp["defensive_applied"]:
        assert row["source"] == "template_fallback", row
        assert row["fallback_reason"] == "missing_brand", row
        assert BRAND in row["title"], "兜底题也必须含品牌名"


def test_a_good_llm_title_is_kept_and_marked_llm(llm_stub):
    """反向对照:过关的题**保留 LLM 原文**并标 `source='llm'`。

    少了它,「把关」可以靠"永远落模板"满足 —— 而那等于 c3 白做。
    """
    llm_stub(lambda d: "%s%s:三分钟说清" % (BRAND, d["question"]))
    ids = _seed(2)
    resp = _regenerate(ids, user_choice="defensive_company")
    for row in resp["defensive_applied"]:
        assert row["source"] == "llm", row
        assert row["title"].endswith("三分钟说清"), row
        assert "fallback_reason" not in row


def test_an_empty_slot_falls_back_instead_of_leaving_the_topic_unwritten(llm_stub):
    """LLM 漏了某一槽 ⇒ 那一篇落模板题,**不是"这一篇没更新"**。

    少了它,漏槽会表现成"标题还是旧的",而用户以为自己选了防御型。
    """
    ids = _seed(2)

    # 🔴 槽名不能猜:`_seed` 逐条写的是 `"原关键词%d" % i`
    #    (`test_defensive_endpoints.py:42`),每个关键词各 1 篇 ⇒ slot 恒为 0。
    #    猜一个不存在的关键词,`drop_slots` 会**一条都匹配不上**,
    #    于是判据读到的是"全都正常产出",却仍然叫 `..._falls_back_...`。
    calls = llm_stub(drop_slots=[(SEED_KEYWORD_1, 0)])
    resp = _regenerate(ids, user_choice="defensive_company")

    # 先证毒下成了:漏槽这件事得真发生,后面的断言才有意义。
    plan_slots = [(d["keyword"], d["slot_index"]) for d in calls.plans[0]]
    assert (SEED_KEYWORD_1, 0) in plan_slots, (
        "要漏的那一槽根本不在 plan 里(plan=%s)—— 夹具的关键词名变了" % plan_slots)

    first = [r for r in resp["defensive_applied"]][0]
    assert first["source"] == "template_fallback"
    assert first["fallback_reason"] == "empty"
    rows = {r["id"]: r for r in _rows()}
    assert BRAND in rows[first["topic_id"]]["optimized_title"], (
        "漏槽那一篇没被写回 —— 用户会看到旧标题")


# ════════════════════════════════════════════════════════════════
# T3 · 去重(跨整批,不只跨同一问)
# ════════════════════════════════════════════════════════════════
def test_two_slots_with_the_same_title_do_not_both_pass(llm_stub):
    """毒:放行 ⇒ 红。两条不同问名却写成同一句话,对用户就是重复。"""
    llm_stub(lambda d: "%s一模一样的标题" % BRAND)
    ids = _seed(3)
    resp = _regenerate(ids, user_choice="defensive_company")
    titles = [r["title"] for r in resp["defensive_applied"]]
    assert len(set(titles)) == len(titles), "出了重复标题:%s" % titles
    kept = [r for r in resp["defensive_applied"] if r["source"] == "llm"]
    assert len(kept) == 1, "同一句话只能留一条,其余落模板"


# ════════════════════════════════════════════════════════════════
# T4 · 计费 —— **只钉当前口径,不改行为**(Owner 未签字)
# ════════════════════════════════════════════════════════════════
def test_an_all_defensive_batch_charges_nothing(llm_stub, billing_spy):
    """🔴 当前口径 = **防御篇不进计费基数**,本判据钉的是这一侧。

    WO_185 §7 Review 裁定:「标题阶段收不收费」属**未定商业口径**,报 Owner;
    §8 那句「计费与其它方向相同」是**推导**不是 Owner 原话(Review 09-14 复核确认)。
    定下来之前取**少收**这一侧:少收可逆,多收不可逆。

    🔴 这条现在钉的是"排除",Owner 签字后要**翻面**并同时钉住另一侧
       (进基数 + 与其它方向逐篇同价)。两侧都钉,才叫"口径定了"。

    🔴 钉的是**计费函数收到的数**,不是回包里的 `expected_count`:
       `expected_count` 是给用户看的"这批该出几个标题",c3 之后它含防御篇
       (防御篇确实要出标题)。拿它当计费基数的读数,就是
       `readout-and-verdict-must-share-one-source` 那个病 —— 两个数会同时
       为真却互相说不上话,而钱走的是 `deduct_points` 那一条。

    🔴 c3 之后防御篇**在** `topics_to_regen` 里(要进 LLM),
       所以这是一次**主动排除** —— 不再是"它们本来就不在批里"。
       第一版实现正是在这里漏的:`updated` 含了防御篇,而扣费按 `updated`
       算 `(n-1)*base` ⇒ 3 篇防御照收 2 篇的钱,回包/日志/篇数全都对。
    """
    llm_stub()
    ids = _seed(3)
    resp = _regenerate_as_payer(ids, user_choice="defensive_company")

    assert len(resp["defensive_applied"]) == 3, "夹具没走到全防御那条路"
    assert resp["updated_count"] == 3, (
        "三篇进批、三篇改了标题,回包却说 %s —— 防御篇被重复计数了"
        % resp["updated_count"])
    assert billing_spy.deducts == [], (
        "防御篇被扣了钱:deduct_points(extra_cost=%s) —— 当前口径是不收,"
        "Owner 签字之后再翻,并且要两侧一起钉" % billing_spy.deducts)
    assert billing_spy.checks == [], (
        "整批不计费却仍进了余额预检(extra_cost=%s)。负的 extra_cost 会把"
        "「这批不计费」表达成「这批花 0 点」,下一个人看不出区别" % billing_spy.checks)


def test_a_mixed_batch_charges_only_the_non_defensive_articles(llm_stub, billing_spy):
    """反向对照:同一批里的**普通篇照收**,一篇不多一篇不少。

    🔴 少了它,上一条可以靠"这个接口根本不扣费了"满足 —— 而那是
       资金侧的事故,不是合规。一侧钉"不收"必须配一侧钉"照收"。
    """
    llm_stub()
    ids = _seed(3)
    # 逐篇指定方向:第 1 篇普通(证据型问答),后两篇防御型。
    plan = [{"topic_id": ids[0], "user_choice": "qa_recommendation"},
            {"topic_id": ids[1], "user_choice": "defensive_company"},
            {"topic_id": ids[2], "user_choice": "defensive_company"}]
    resp = _regenerate_as_payer(ids, style_plan=plan)

    assert len(resp["defensive_applied"]) == 2, (
        "混批没按计划分方向:defensive_applied=%s" % resp["defensive_applied"])
    assert billing_spy.charged_articles == [1], (
        "混批收的篇数不对:%s(应当只收那 1 篇普通的)"
        % billing_spy.charged_articles)


# ════════════════════════════════════════════════════════════════
# T7 · fail-soft:playbook 蒸不出来,标题照出且 reason 非空
# ════════════════════════════════════════════════════════════════
def test_playbook_failure_still_produces_titles_and_says_why(llm_stub, monkeypatch):
    """毒:静默 ⇒ 红。

    🔴 缺 playbook 时 `defensive_playbook.reason` 必须非空并回给前端 ——
       静默 None 会让界面显示成"一切正常",而 AI 那一层根本没参与。
    """
    import writing.defensive_playbook as pb

    def _boom(*a, **kw):
        raise RuntimeError("蒸馏挂了")

    monkeypatch.setattr(pb, "_distill_with_llm", _boom)
    llm_stub()
    ids = _seed(2)
    resp = _regenerate(ids, user_choice="defensive_company")

    assert len(resp["defensive_applied"]) == 2, "playbook 挂了就不出标题 —— fail-soft 没做到"
    meta = resp["defensive_playbook"]
    assert meta["version"] is None
    assert meta["reason"], "缺 playbook 却没给原因 —— 前端会显示成一切正常"
    # 🔴 钉**这一层**的 reason,不接受"两个里随便哪个"。
    #    fail-soft 有两层(内层管蒸馏、外层管取连接与调用本身),
    #    原来写成 `"distill_failed" in r or "unavailable" in r` ——
    #    于是「把内层 fail-soft 改成 raise」那一发毒全程是绿的:外层把它兜了,
    #    判据看到另一个词也满意。两层混成一句话之后,「蒸馏挂了」与
    #    「连库都没连上」在前端和日志里长得一模一样,而运维要做的处理不一样。
    assert meta["reason"].startswith("distill_failed:"), (
        "蒸馏失败被外层那道 fail-soft 兜走了(reason=%s)" % meta["reason"])


def test_a_playbook_lookup_that_blows_up_says_unavailable_not_distill_failed(
        llm_stub, monkeypatch):
    """外层那道 fail-soft 有**自己的** reason —— 与「蒸馏失败」分开。

    两层 fail-soft 是有意的(内层管蒸馏本身,外层管取连接/调用这件事),
    但两层必须给**不同的** reason。少了这一条,上一条就只能钉
    "两个词随便哪个",于是把内层 fail-soft 改成 `raise` 的毒会被外层兜住 ——
    判据全程绿,而 `distill_failed: <真实异常>` 这条线索被换成了
    `unavailable: <同一个异常>`,谁也不知道是哪一层出的事。
    """
    import writing.defensive_playbook as pb

    def _boom(*a, **kw):
        raise RuntimeError("取 playbook 整个挂了")

    monkeypatch.setattr(pb, "get_or_build_playbook", _boom)
    llm_stub()
    ids = _seed(2)
    resp = _regenerate(ids, user_choice="defensive_company")

    assert len(resp["defensive_applied"]) == 2, "外层 fail-soft 没兜住 —— 标题没出"
    meta = resp["defensive_playbook"]
    assert meta["version"] is None
    assert meta["reason"].startswith("unavailable:"), meta


def test_the_playbook_field_is_always_present(llm_stub):
    """`defensive_playbook` **恒在**(固定形状优于可选字段)。

    可选字段缺席时,前端分不出「这次没用上 playbook」与「后端忘了给」——
    而两者要显示的话不一样。
    """
    llm_stub()
    ids = _seed(1)
    resp = _regenerate(ids, user_choice="defensive_company")
    assert "defensive_playbook" in resp
    assert set(resp["defensive_playbook"]) >= {"version", "reason"}


# ════════════════════════════════════════════════════════════════
# T5 · playbook 缓存与 TTL
#
# 🔴 这一组全部用「蒸馏调用次数」当读数,而不是回包里的 reason:
#    reason 是端点自己写的一句话,它说 "cache_hit" 证明不了**没打**
#    —— 打没打只有出网那一层知道。
# ════════════════════════════════════════════════════════════════
def _playbook_rows():
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT id, brand_id, version, kb_fingerprint, expires_at"
                    "  FROM geo_defensive_playbooks WHERE brand_id=%s ORDER BY id",
                    (BRAND_ID,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        c.close()


def _exec(sql, args=()):
    c = conn()
    try:
        c.cursor().execute(sql, args)
    finally:
        c.close()


def test_the_second_call_within_the_ttl_does_not_distill_again(llm_stub):
    """同品牌 7 天内第二次 ⇒ 读缓存,**不打蒸馏 LLM**。

    毒:每次都蒸 ⇒ 红。少了它,`geo_defensive_playbooks` 这张表
    就只是个写日志的地方 —— 每批标题都要多打一次 LLM,而没人看得出来。
    """
    calls = llm_stub()
    ids = _seed(3)

    _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 1, "第一次没蒸 —— 后面的对比就没有基准"
    assert len(_playbook_rows()) == 1, "蒸出来的那一版没落库"

    second = _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 1, (
        "第二次又蒸了一遍(共 %s 次)—— 缓存没命中" % calls.distill_count)
    assert second["defensive_playbook"]["reason"] == "cache_hit"
    assert len(_playbook_rows()) == 1, "命中缓存却又写了一行"


def test_a_changed_knowledge_base_invalidates_the_cached_playbook(llm_stub):
    """反向对照:品牌事实变了 ⇒ 指纹变 ⇒ **重新蒸**。

    🔴 少了它,上一条可以靠"永远命中缓存"满足 —— 而那意味着客户补了资料
       之后,防御型写法仍然按旧素材来,且永不过期。
    """
    calls = llm_stub()
    ids = _seed(3, profile="我们是一家做成人教育的公司")
    _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 1

    # 客户补了资料 ⇒ 事实列变 ⇒ 指纹必须跟着变
    _exec("UPDATE client_profiles SET company_intro=%s WHERE brand_id=%s",
          ("补充过的公司简介:成人教育 + 职业培训", BRAND_ID))

    resp = _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 2, (
        "知识库变了却还在用旧 playbook(蒸馏仍是 %s 次)" % calls.distill_count)
    assert resp["defensive_playbook"]["reason"] == "built"
    rows = _playbook_rows()
    assert len(rows) == 2, "新版本没落库(旧版**不删**,要能回看)"
    assert rows[0]["kb_fingerprint"] != rows[1]["kb_fingerprint"]


def test_an_expired_playbook_is_not_reused(llm_stub):
    """TTL 到点 ⇒ 不再取用(行留着),重新蒸。

    毒:把 `expires_at > now()` 从查询里去掉 ⇒ 红。
    """
    calls = llm_stub()
    ids = _seed(2)
    _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 1

    _exec("UPDATE geo_defensive_playbooks SET created_at = now() - interval '30 days',"
          "       expires_at = now() - interval '1 day' WHERE brand_id=%s", (BRAND_ID,))

    _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 2, "过期的 playbook 仍被取用"
    assert len(_playbook_rows()) == 2, "过期行被删了 —— 应当留痕可回看"


def test_a_playbook_from_another_prompt_version_is_not_reused(llm_stub):
    """换了 prompt 版本 ⇒ 旧版不取用。

    少了它,`PLAYBOOK_VERSION` 就只是一个写进去、从来没人读的字段 ——
    改 prompt 之后旧角度会一直用到 TTL 到期,而那七天没人能解释产出为什么没变。
    """
    calls = llm_stub()
    ids = _seed(2)
    _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 1

    _exec("UPDATE geo_defensive_playbooks SET version=%s WHERE brand_id=%s",
          ("v0-上一版", BRAND_ID))

    _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 2, "上一版 prompt 蒸出来的 playbook 仍被取用"


# ════════════════════════════════════════════════════════════════
# T6 · 正文**单点**注入
# ════════════════════════════════════════════════════════════════
def test_the_body_prompt_carries_the_playbook_for_a_defensive_topic(llm_stub):
    """防御篇的正文提示块带上这家品牌蒸出来的写法。

    🔴 先让标题那一趟把 playbook 蒸出来并落库,再读正文侧 ——
       直接手塞一行进表,验的就不是"两边读的是同一份"。
    """
    from writing.defensive_playbook import (DEFENSIVE_BODY_HEADING,
                                            render_defensive_body_prompt)

    calls = llm_stub(angles={"怎么样": ["角度A"]})
    ids = _seed(2, profile="我们是一家做成人教育的公司")
    _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 1 and len(_playbook_rows()) == 1

    block = render_defensive_body_prompt(BRAND_ID, "defensive_company")
    assert DEFENSIVE_BODY_HEADING in block, (
        "正文没拿到 playbook —— 标题蒸的那一版正文读不到,"
        "说明两边算出来的指纹不是同一个")
    assert "正文要点1" in block and "不点名同行" in block, block
    assert calls.distill_count == 1, "正文侧**又蒸了一次** —— 只读不蒸"


def test_a_non_defensive_topic_gets_no_defensive_body_block(llm_stub):
    """反向对照:别的方向**一个字都不加**。

    少了它,"注入"可以靠"给所有文章都加一段"满足 —— 那会把公司词的
    写法规则套到榜单/指南类文章上,而那些文章的商业对象根本不是品牌自己。
    """
    from writing.defensive_playbook import render_defensive_body_prompt

    llm_stub()
    ids = _seed(2, profile="我们是一家做成人教育的公司")
    _regenerate(ids, user_choice="defensive_company")
    assert len(_playbook_rows()) == 1, "夹具没把 playbook 蒸出来,这条对照就没意义"

    assert render_defensive_body_prompt(BRAND_ID, "qa_recommendation") == ""
    assert render_defensive_body_prompt(BRAND_ID, None) == ""


def test_the_body_reads_the_playbook_at_exactly_one_place():
    """🔴 **单点**:正文侧读 playbook 的地方**只有一处**,且在正文生成路径上。

    两处读就会出现「标题按这一版角度写、正文按另一处拼的规则写」,
    而两边各自都是绿的 —— 这种分叉只有在客户问"为什么正文没按标题的角度写"
    时才会被发现。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    hits = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("tests/", "scripts/", ".venv/")) or "/_archive/" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(
                    node.func, "id", getattr(node.func, "attr", None)
            ) == "render_defensive_body_prompt":
                hits.append((rel, node.lineno))

    assert len(hits) == 1, "正文侧读 playbook 的地方不止一处:%s" % hits
    rel, lineno = hits[0]
    assert rel == "writing/article_writer.py", "注入点挪到了别处:%s" % rel

    # 🔴 "在文件里"不等于"在跑的那条路上":钉住它在 `ArticleWriter.write` 体内。
    tree = ast.parse((root / rel).read_text(encoding="utf-8"))
    inside = [
        fn.name
        for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef)
        for fn in cls.body
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and fn.lineno <= lineno <= (fn.end_lineno or fn.lineno)
    ]
    assert inside == ["write"], (
        "注入点不在 `ArticleWriter.write` 里(在 %s)—— "
        "挂在没人调的函数上,等于没接" % inside)

    # 🔴 "调了"还不等于"用了":返回值必须真的拼进 `system_prompt`。
    #    只数调用点的话,把 `system_prompt += ...` 改成
    #    `_unused = ...` 这一发毒会全程绿 —— 函数照调、结果扔掉。
    wired = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name) and node.target.id == "system_prompt"
        and node.lineno <= lineno <= (node.end_lineno or node.lineno)
    ]
    assert len(wired) == 1, (
        "playbook 块没有拼进 system_prompt(调了但没用)—— "
        "正文提示里不会出现它,而调用点看起来一切正常")


# ════════════════════════════════════════════════════════════════
# T8 · `sources_used` 的数,与真正跑的那几条查询对得上
# ════════════════════════════════════════════════════════════════
_CITE_SEQ = [0]


def _cite(question, *, brand_id=BRAND_ID, n=1):
    c = conn()
    try:
        cur = c.cursor()
        for i in range(n):
            # 🔴 `uq_geo_article_attr_result_publication` 唯一键 =
            #    (monitoring_result_id, publication_source, publication_source_id)。
            #    每行给一个新号,否则第二行起全被约束挡下 —— 而那会让
            #    「命中 2 条」这种读数变成"只插进去 1 条"的假象。
            _CITE_SEQ[0] += 1
            _seq = _CITE_SEQ[0]
            cur.execute(
                "INSERT INTO geo_article_citation_attributions"
                " (metric_version, url_normalization_version, monitoring_result_id,"
                "  publication_source, publication_source_id, brand_id, question,"
                "  question_family, publish_url_normalized, publish_domain,"
                "  published_at, tested_at, url_match, body_proof)"
                # 🔴 `chk_geo_article_attr_time_order` 要求 tested_at > published_at。
                #    夹具建在生产 schema 上就是为了吃到这种手写夹具漏掉的约束。
                " VALUES ('v1','v1',%s,'manual',%s,%s,%s,%s,'https://x/y','x.com',"
                "         now() - interval '1 day', now(), 'exact', true)",
                (_seq, _seq, brand_id, question,
                 None if i % 2 else "legacy_unknown"))
    finally:
        c.close()


def test_the_source_counts_match_what_was_actually_queried(llm_stub):
    """🔴 `sources_used` 的每个数都要能对上一条真跑过的查询。

    工单 T8。这条钉的是「计数不许凭印象写」:
      · `client_profile_facts` = **非空**的事实列数(不是这张表有几列);
      · `cited_samples.count`  = 命中八问问法的行数(不是这个品牌全部行数);
      · `cited_samples.scanned` = 扫过的行数 —— 两者分开记,
        否则「今天没命中」与「这张表根本没有这个品牌的数据」就分不出来。
    """
    llm_stub()
    ids = _seed(2, profile="我们是一家做成人教育的公司")
    _cite("康之康怎么样", n=2)          # 命中「怎么样」
    _cite("有什么优惠活动", n=3)        # 不命中任何一问
    _regenerate(ids, user_choice="defensive_company")

    rows = _playbook_rows()
    assert len(rows) == 1
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT sources_used FROM geo_defensive_playbooks WHERE id=%s",
                    (rows[0]["id"],))
        used = dict(cur.fetchone())["sources_used"]
    finally:
        c.close()

    # 只有 company_intro 一列有值(`_seed(profile=...)` 只写了它)
    assert used["client_profile_facts"] == 1, used
    assert used["client_profile_fields_queried"] >= 12, (
        "查询的列数对不上 QUESTION_FACT_FIELDS:%s" % used)
    assert used["cited_samples"]["count"] == 2, used["cited_samples"]
    assert used["cited_samples"]["scanned"] == 5, used["cited_samples"]
    assert used["cited_samples"]["table"] == "geo_article_citation_attributions"
    # NULL 单独一档:与「写了 legacy_unknown」是两件事
    dist = used["cited_samples"]["question_family_distribution"]
    assert dist.get("__NULL__", 0) + dist.get("legacy_unknown", 0) == 5, dist
    assert "__NULL__" in dist, "NULL 被并进了别的档 —— 合并之后就分不出没写和写了占位"


def test_an_unavailable_table_is_recorded_as_unavailable_not_as_zero(llm_stub):
    """🔴 「今天为 0」与「这张表答不了这个问题」必须分开记。

    少了它,下一个人会一直等 `geo_answer_adoption_metrics` 长出数据 ——
    而那张表结构上就没有 brand_id / question,永远不会长出来。
    """
    from writing.defensive_playbook import CITED_SAMPLE_TABLES_UNAVAILABLE

    llm_stub()
    ids = _seed(1)
    _regenerate(ids, user_choice="defensive_company")
    rows = _playbook_rows()
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT sources_used FROM geo_defensive_playbooks WHERE id=%s",
                    (rows[0]["id"],))
        used = dict(cur.fetchone())["sources_used"]
    finally:
        c.close()

    assert used["cited_samples"]["count"] == 0
    assert set(used["cited_samples_unavailable"]) == set(CITED_SAMPLE_TABLES_UNAVAILABLE)
    for table, why in used["cited_samples_unavailable"].items():
        assert why.strip(), "%s 说不可用却没说为什么" % table


# ════════════════════════════════════════════════════════════════
# c3' ② · 夹具自足 —— 参照行的值不许静默漂走
# ════════════════════════════════════════════════════════════════
def test_the_fixture_reference_rows_still_match_the_migrations():
    """夹具自建的 `monitoring_product_platform_matrices` 参照行,
    必须与迁移脚本里写的**逐字一致**。

    🔴 为什么要这条:生产 schema 快照是纯 schema(没有数据),所以夹具必须
       自己建这两行,否则 `confirmed_keywords` 的外键插不进去。而"自己建"
       就等于**抄了一份值**——抄来的值会和源头漂开,漂开那天判据不会红,
       只会让夹具悄悄代表另一个世界。这条判据就是那根绳子。
       (Review 09-14 在全新库上实测:不建这两行 ⇒ 本包 27 红;
        我的私库碰巧有,于是"在我机器上全绿"。)
    """
    import pathlib

    from tests.defensive_direction_2026_09_13.conftest import (
        _PLATFORM_MATRIX_MIGRATIONS, _PLATFORM_MATRIX_ROWS)

    root = pathlib.Path(__file__).resolve().parents[2]
    blob = ""
    for rel in _PLATFORM_MATRIX_MIGRATIONS:
        path = root / rel
        assert path.exists(), "迁移脚本不在了:%s —— 参照行的出处没了" % rel
        blob += path.read_text(encoding="utf-8")

    for version, platforms in _PLATFORM_MATRIX_ROWS:
        pair = "('%s', '%s')" % (version, platforms)
        assert pair in blob, (
            "夹具里的参照行 %s 在迁移脚本里找不到 —— 值漂了,"
            "夹具代表的已经不是生产那个世界" % pair)


def test_the_private_library_actually_has_those_reference_rows():
    """上一条钉的是"值对不对",这一条钉的是"真的插进去了"。

    🔴 两件事:`_seed_reference_rows()` 可能整个没被调到(比如挪错了位置),
       而那种情况下上一条判据照样全绿 —— 它只读文件,不读库。
    """
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT version, platforms FROM monitoring_product_platform_matrices")
        got = {r["version"]: r["platforms"] for r in cur.fetchall()}
    finally:
        c.close()

    from tests.defensive_direction_2026_09_13.conftest import _PLATFORM_MATRIX_ROWS

    for version, platforms in _PLATFORM_MATRIX_ROWS:
        assert got.get(version) == platforms, (
            "私库里缺参照行 %s(或值不对:%s)—— 全新库上 `_seed` 会外键失败"
            % (version, got.get(version)))


# ════════════════════════════════════════════════════════════════
# c3' ④ · 长度把关(Review 毒 X2:`_MAX_TITLE_CHARS` 48→480 全绿)
# ════════════════════════════════════════════════════════════════
def test_an_over_long_llm_title_falls_back_to_the_template(llm_stub):
    """超过 `_MAX_TITLE_CHARS` 的 LLM 标题 ⇒ 落模板 + `fallback_reason=="too_long"`。

    🔴 Review 09-14 毒 X2:把 `_MAX_TITLE_CHARS` 从 48 改到 480,**全包绿** ——
       长度这一档把关根本没有判据在看。`vet_llm_titles` 里四个 reason
       (empty / missing_brand / too_long / duplicate_title),原来只钉了三个。
       没人看的那一档,就是下一次静默放行的地方。

    🔴 用 `_MAX_TITLE_CHARS + 1` 而不是写死 49:钉的是**被使用的那个值**,
       阈值调整时判据跟着走,而不是变成一条恒真或恒假的断言。
    """
    from writing.defensive_questions import _MAX_TITLE_CHARS

    # 品牌名必须在(否则会先被 missing_brand 拦下,验的就不是长度这一档)
    long_tail = "超长" * _MAX_TITLE_CHARS
    calls = llm_stub(lambda d: (BRAND + long_tail)[:_MAX_TITLE_CHARS + 1])
    ids = _seed(2)
    resp = _regenerate(ids, user_choice="defensive_company")

    assert calls.count >= 1
    for row in resp["defensive_applied"]:
        assert row["source"] == "template_fallback", row
        assert row["fallback_reason"] == "too_long", (
            "超长题没按 too_long 兜底(reason=%s)" % row.get("fallback_reason"))
        assert len(row["title"]) <= _MAX_TITLE_CHARS, row


def test_a_title_exactly_at_the_limit_is_kept(llm_stub):
    """反向对照:**正好等于**上限的题保留。

    少了它,「超长落模板」可以靠"把上限调成 0、全部落模板"满足 ——
    而那等于 c3 白做。边界值两侧都钉,阈值才算被钉住。
    """
    from writing.defensive_questions import _MAX_TITLE_CHARS

    def _exact(d):
        base = BRAND + d["question"]
        pad = "好" * max(0, _MAX_TITLE_CHARS - len(base))
        return (base + pad)[:_MAX_TITLE_CHARS]

    llm_stub(_exact)
    ids = _seed(2)
    resp = _regenerate(ids, user_choice="defensive_company")
    for row in resp["defensive_applied"]:
        assert len(row["title"]) == _MAX_TITLE_CHARS, row
        assert row["source"] == "llm", (
            "正好卡在上限的题被当成超长了(off-by-one)—— %s" % row)


# ════════════════════════════════════════════════════════════════
# c3' ③ · 蒸馏那一次调用要进 `llm_call_log`
# ════════════════════════════════════════════════════════════════
DISTILL_CALLER = "defensive_playbook_distill"


def _log_rows(caller=DISTILL_CALLER):
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT caller, platform, model, input_tokens, output_tokens,"
                    "       estimated_cost, success FROM llm_call_log"
                    " WHERE caller=%s ORDER BY id", (caller,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        c.close()


def test_one_distillation_writes_exactly_one_cost_row(llm_stub):
    """蒸一次 ⇒ `llm_call_log` 落**一行**,且 tokens 不是 0。

    🔴 这是每品牌每 7 天一次的**真实成本**。不入账的话成本表上这条线恒为 0,
       而 0 与「没人用这个功能」长得一模一样 —— 等到有人问「防御型贵不贵」,
       能查到的只有一句"查不到"。
    🔴 同时钉 tokens:只钉"有一行"的话,`record()` 没调(全 0)照样绿,
       而全 0 的行在成本表上与不存在等价。
    """
    calls = llm_stub()
    ids = _seed(2)
    _regenerate(ids, user_choice="defensive_company")

    assert calls.distill_count == 1, "夹具没走到真蒸馏那一步"
    rows = _log_rows()
    assert len(rows) == 1, "蒸了 %s 次却落了 %s 行" % (calls.distill_count, len(rows))
    row = rows[0]
    assert row["input_tokens"] == 1234 and row["output_tokens"] == 567, row
    assert row["success"] is True, row
    # 回显的 model 才是真正跑的那个(供应商静默换模型时,账要记真的那个)
    assert row["model"] == "deepseek-v4-flash", row
    assert float(row["estimated_cost"] or 0) > 0, (
        "算出来的成本是 0 —— 单价表里没有这个模型,或 tokens 没送进去:%s" % row)


def test_a_cache_hit_writes_no_cost_row(llm_stub):
    """反向对照:命中缓存那一次**不记账**(因为它确实没调 LLM)。

    少了它,「一次蒸馏一行」可以靠"每次调用都记一行"满足 ——
    那会让成本表把没花的钱也算上,比不记账更难发现。
    """
    calls = llm_stub()
    ids = _seed(2)
    _regenerate(ids, user_choice="defensive_company")
    assert len(_log_rows()) == 1

    _regenerate(ids, user_choice="defensive_company")
    assert calls.distill_count == 1, "第二次又蒸了 —— 缓存没命中,这条对照不成立"
    assert len(_log_rows()) == 1, "命中缓存却也记了一笔账"


def test_a_failed_distillation_still_leaves_a_row(llm_stub):
    """蒸馏打挂了也要留痕(success=False),不是"这次没调过"。

    🔴 失败的调用**一样花了钱/占了配额**,更重要的是:
       失败不留痕时,"这个功能没人用"与"这个功能一直在挂"读起来一模一样。
    """
    llm_stub(distill_fails=True)
    ids = _seed(2)
    resp = _regenerate(ids, user_choice="defensive_company")

    # fail-soft:标题照出
    assert len(resp["defensive_applied"]) == 2
    rows = _log_rows()
    assert len(rows) == 1, "蒸馏失败一行都没留 —— 挂了和没人用分不出来"
    assert rows[0]["success"] is False, rows[0]


# ════════════════════════════════════════════════════════════════
# c3'' · 标题上限:**数值本身**要有人钉,且全仓只有一份
# ════════════════════════════════════════════════════════════════
def test_the_title_cap_is_a_single_constant_not_a_second_copy():
    """🔴 提示词里的上限与把关用的上限必须是**同一个常量**。

    Review 09-15 毒 X2:`_MAX_TITLE_CHARS` 48→480,**全包绿**。
    原因是我那条 `too_long` 判据用 `_MAX_TITLE_CHARS + 1` 造标题 ——
    钉的是**机制**,阈值一起动就一起绿,对数值漂移完全是瞎的。
    而 `keyword_topic_generator.py` 里还有**另一份** 48(告诉 LLM 的上限),
    根本没 import 那个常量:改一处另一处静默不同意 ——
    提示说 480、把关仍按 48 ⇒ LLM 照着 480 写、回来全被判 too_long 落模板,
    全程零报错,表现只是"AI 写的标题怎么都没用上"。

    这条钉「只有一份」:生成器里不许再出现裸的长度字面量。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    src = (root / "writing" / "keyword_topic_generator.py").read_text(encoding="utf-8")
    assert "_MAX_TITLE_CHARS" in src, (
        "生成器没引用那个常量 —— 提示词里的上限又变成第二份拷贝了")
    # 提示词那一行必须取常量,不是字面量
    bad = [ln for ln in src.splitlines()
           if "标题不超过" in ln or re.search(r"self\.brand_name,\s*\d+\)", ln)]
    for ln in bad:
        assert not re.search(r"self\.brand_name,\s*\d+\)", ln), (
            "提示词的上限又写死成字面量了:%s" % ln.strip())


def test_the_title_cap_value_itself_is_pinned():
    """🔴 钉**数值**,不只钉机制。

    上限是 48 不是别的数,理由要写在断言里 —— 否则下一个人只会看到一个
    "神秘常量"并顺手调大:
      · 前端选题卡一行放得下(超了折行,整页节奏乱);
      · 长标题在 AI 引擎里表现差(被截断后主语常常丢在后半段)。
    要改它 ⇒ 连同这两条理由一起重新评估,并且**同时**改前端与引擎侧的预期,
    不是单点调参。
    """
    from writing.defensive_questions import _MAX_TITLE_CHARS

    assert _MAX_TITLE_CHARS == 48, (
        "标题上限被改成了 %s。它不是随手可调的参数:48 的依据是"
        "①前端选题卡一行放得下 ②长标题在 AI 引擎里被截断后主语容易丢。"
        "确实要改 ⇒ 带着这两条依据重新评估,并同步前端与引擎侧预期,"
        "然后改这条判据里的数字(改判据是**有意为之**的那一步,不是绕过)。"
        % _MAX_TITLE_CHARS)


def test_the_prompt_really_carries_whatever_the_constant_says(llm_stub, monkeypatch):
    """🔴 **行为锁**:改了常量,发给 LLM 的那句话必须跟着变。

    Review 09-15 毒 Y2:把模板写死成「不超过 48 字」、元组里的常量去掉、
    注释和 import 都留着 —— 我那条「只有一份」的判据**仍然绿**。
    因为它是**源码正则**:认的是我当时写的那一种形状,而不是行为。
    形状锁挡不住"换个写法把同一件事做回去"
    (本仓 `shape-locks-dont-catch-a-broken-invariant`)。

    这条改成钉行为:把常量 monkeypatch 成 480,**真的渲染一次**防御提示块,
    断言发出去的 prompt 里写的是 480;还原后写的是 48。
    提示词里的上限与把关用的上限只要重新变成两份拷贝,这条当场红。
    """
    import writing.defensive_questions as dq

    calls = llm_stub()
    monkeypatch.setattr(dq, "_MAX_TITLE_CHARS", 480)
    ids = _seed(2)
    _regenerate(ids, user_choice="defensive_company")

    assert calls.prompts, "没发出 prompt —— 这条判据什么都没验"
    sent = calls.prompts[0]
    assert "不超过 480 字" in sent, (
        "常量改成 480,发给 LLM 的仍不是 480 —— 提示词那一侧又写死了。"
        "后果:LLM 照旧上限写、回来按新上限判,或反过来,两边静默不一致。"
        "实际发出去的那一句:%r"
        % next((l for l in sent.splitlines() if "不超过" in l), "<找不到这一句>"))
    assert "不超过 48 字" not in sent, sent[:200]


def test_the_prompt_carries_the_real_value_by_default(llm_stub):
    """反向对照:不动常量时,发出去的就是 48。

    少了它,上一条可以靠「提示词里干脆不写上限」满足 ——
    那样 LLM 没有长度约束,回来全被 `too_long` 判掉落模板,
    表现同样是"AI 写的标题怎么都没用上"。
    """
    from writing.defensive_questions import _MAX_TITLE_CHARS

    calls = llm_stub()
    ids = _seed(2)
    _regenerate(ids, user_choice="defensive_company")
    sent = calls.prompts[0]
    assert ("不超过 %d 字" % _MAX_TITLE_CHARS) in sent, (
        "提示词里根本没有长度上限这一句 —— LLM 没有约束,回来全落模板")
