"""WO_204 c1 判据 · C3 —— 「按标题制作」。

Owner 09-13:「修改后就**按照标题**来进行创作」。
用户改了标题却拿到一个 AI 自己想的标题,那就是改了个寂寞。

🔴 本文件**真跑** `run_image_post_production`,只桩掉花钱(billing)与花时间
   (文案 LLM / 生图)的外部件 —— 与 #184 d1 同一套做法。
   判据读的是**库里那一行**(`geo_douyin_posts.title` / `generation_meta`),
   不是被测函数自己返回的字段:返回值是它自己写的,拿它当证据等于自证。
"""
import asyncio
import json

import pytest

from .conftest import (BRAND_ID, USER_ID, conn, make_post, seed_topic,
                       topic_row)

LLM_TITLE = "AI 自己想的标题"
TOPIC_TITLE = "用户定好的标题"


@pytest.fixture
def stubbed(monkeypatch):
    """桩掉外部件。

    🔴 打在**源模块**上:生产任务在函数体里 `from middleware.billing import ...`,
       查名发生在调用时。打在 `production_task` 上无效 —— 那儿根本没有这些名字,
       而**无效的桩看起来跟生效一模一样**(判据照样绿,只是真调了外部件)。
    """
    import middleware.billing as billing
    from services.geo_douyin import content_generator, image_pipeline, pricing

    seen = {"freeze": 0, "commit": 0, "release": 0, "hint": None}

    async def _freeze_points(*a, **kw):
        seen["freeze"] += 1
        return {"freeze_id": 88000 + seen["freeze"], "success": True}

    async def _commit_freeze(*a, **kw):
        seen["commit"] += 1
        return {"success": True}

    async def _release_freeze(*a, **kw):
        seen["release"] += 1
        return {"success": True}

    async def _extra_card_points(n):
        return 0

    async def _content(*a, **kw):
        # 记下 extra_hint:C3 还要证明 angle / card_outline 是**当提示**用的,
        # 不是被拿去当标题。
        seen["hint"] = kw.get("extra_hint")
        # 用**真的那个 dataclass**,不自造形状:自造的 dict 少一个字段
        # 就会在被测代码里炸成 AttributeError,而那种红看起来像被测对象坏了。
        return content_generator.GeneratedContent(
            body="正文", title=LLM_TITLE, hashtags=["#装修"],
            cards=[{"headline": "H0", "body": "b0"}],
            cover={"title": "封面", "subtitle": "副标"},
            closing={"headline": "收尾", "summary": "小结"},
            visual={"scene": "studio", "primary_color": "#111",
                    "accent_color": "#eee"})

    def _specs(content, style, **kw):
        return [{"index": i, "headline": "H%d" % i, "kind": "content",
                 "prompt": "p%d" % i} for i in range(3)]

    async def _render(post_id, specs, **kw):
        return image_pipeline.CardBatchResult(
            cards=[image_pipeline.CardImageResult(
                idx=i, ok=True, oss_key="geo/img/c204/%d.png" % i)
                for i in range(len(specs))],
            total_cost_usd=0.0)

    monkeypatch.setattr(billing, "freeze_points", _freeze_points)
    monkeypatch.setattr(billing, "commit_freeze", _commit_freeze)
    monkeypatch.setattr(billing, "release_freeze", _release_freeze)
    monkeypatch.setattr(pricing, "extra_card_points", _extra_card_points)
    monkeypatch.setattr(content_generator, "generate_image_post_content", _content)
    monkeypatch.setattr(image_pipeline, "build_prompts_for_group", _specs)
    monkeypatch.setattr(image_pipeline, "render_prompt_group", _render)
    return seen


def post_row(post_id):
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT title, status, generation_meta FROM geo_douyin_posts"
                    " WHERE id=%s", (int(post_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def _run(post_id, **kw):
    from services.geo_douyin.production_task import run_image_post_production
    return asyncio.run(run_image_post_production(
        post_id=post_id, user_id=USER_ID, keyword="装修公司",
        brand_id=BRAND_ID, brand_name="某装修公司", city="杭州",
        card_count=3, **kw))


def _meta(row):
    meta = row["generation_meta"] or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return meta


# ═══════════════════════════════════════════════════════════════
# C3 主臂
# ═══════════════════════════════════════════════════════════════
def test_topic_title_wins_over_the_llm_title(stubbed):
    """C3:带选题标题 ⇒ 落库标题**逐字**等于它,`title_source='topic'`。

    桩里的 AI 恒返 `LLM_TITLE` —— 所以"标题等于选题标题"只有在
    「以选题为准」真的生效时才成立。去掉那一跳,落库的就是 AI 那句。
    """
    post_id = make_post()
    outcome = _run(post_id, topic_title=TOPIC_TITLE)
    assert outcome.ok is True, outcome.error

    row = post_row(post_id)
    assert row["title"] == TOPIC_TITLE, (
        "落库标题是 %r —— 用户改的标题没生效" % row["title"])
    assert row["title"] != LLM_TITLE
    assert _meta(row).get("title_source") == "topic"


def test_without_a_topic_title_the_llm_title_is_kept(stubbed):
    """反向对照:不传选题标题 ⇒ **现有行为逐字不变**(AI 出标题、source=llm)。

    少了它,「无论如何都用某个固定标题」也能让主臂绿,
    而那会把所有不带选题的老路径一起改掉。
    """
    post_id = make_post()
    outcome = _run(post_id)
    assert outcome.ok is True, outcome.error

    row = post_row(post_id)
    assert row["title"] == LLM_TITLE
    assert _meta(row).get("title_source") == "llm"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_topic_title_falls_back_to_the_llm(stubbed, blank):
    """空/空白的选题标题不算"用户定了标题" —— 落回 AI,而不是落一个空标题。"""
    post_id = make_post()
    _run(post_id, topic_title=blank if blank is not None else "")
    row = post_row(post_id)
    assert row["title"] == LLM_TITLE
    assert _meta(row).get("title_source") == "llm"


def test_the_llm_still_runs_and_supplies_body_and_tags(stubbed):
    """「以选题为准」只管**标题** —— 正文/标签仍由 AI 出。

    钉住它,是因为"干脆不调 LLM 了"是个看起来更省的错解法:
    那样正文和标签会空,而 `title_or_hashtags_missing` 那道判空会在别处炸,
    读起来像别的缺陷。
    """
    post_id = make_post()
    _run(post_id, topic_title=TOPIC_TITLE)
    assert stubbed["hint"] is not None, "文案 LLM 根本没被调用"
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT body_text, hashtags FROM geo_douyin_posts WHERE id=%s",
                    (post_id,))
        row = dict(cur.fetchone())
    finally:
        c.close()
    assert row["body_text"] == "正文"
    assert list(row["hashtags"]) == ["#装修"]


# ═══════════════════════════════════════════════════════════════
# 选题状态机随制作走(挂在 dispatch 外壳上,一处覆盖全部出口)
# ═══════════════════════════════════════════════════════════════
def _dispatch_and_wait(**kwargs):
    from services.geo_douyin.production_task import dispatch_production

    async def _go():
        task = dispatch_production(**kwargs)
        return await task

    return asyncio.run(_go())


def test_dispatch_marks_the_topic_done_with_its_post(stubbed):
    """做成了 ⇒ 选题 `done` + 写 post_id。"""
    tid = seed_topic(title=TOPIC_TITLE, status="making")
    post_id = make_post()
    _dispatch_and_wait(post_id=post_id, user_id=USER_ID, keyword="装修公司",
                       brand_id=BRAND_ID, brand_name="某装修公司", city="杭州",
                       card_count=3, topic_id=tid, topic_title=TOPIC_TITLE)
    row = topic_row(tid)
    assert (row["status"], row["post_id"]) == ("done", post_id)


def test_dispatch_releases_the_topic_when_production_fails(stubbed, monkeypatch):
    """做失败了 ⇒ 选题回到 `failed`,**不留 post_id**。

    🔴 不收尾的话这一条永远卡在"制作中":用户点不动,列表也不解释为什么。
       状态收尾挂在 dispatch 外壳上就是为了覆盖**所有**失败出口 ——
       `run_image_post_production` 有两个成功出口和六七个失败出口,
       逐个挂钩必漏一个。
    """
    from services.geo_douyin import content_generator

    async def _no_content(*a, **kw):
        # 标题/标签缺失 = 生产判废(真实失败路径之一)
        return content_generator.GeneratedContent(
            body="", title="", hashtags=[], cards=[], cover={}, closing={},
            visual={})

    monkeypatch.setattr(content_generator, "generate_image_post_content", _no_content)

    tid = seed_topic(title=TOPIC_TITLE, status="making")
    post_id = make_post()
    _dispatch_and_wait(post_id=post_id, user_id=USER_ID, keyword="装修公司",
                       brand_id=BRAND_ID, brand_name="某装修公司", city="杭州",
                       card_count=3, topic_id=tid, topic_title=TOPIC_TITLE)
    row = topic_row(tid)
    assert row["status"] == "failed"
    assert row["post_id"] is None


def test_dispatch_without_a_topic_touches_no_topic_row(stubbed):
    """反向对照:不带 topic_id 的老调用一行选题都不碰。"""
    from db.geo_douyin_db import list_topics

    tid = seed_topic(title="不该被动的题")
    before = topic_row(tid)
    post_id = make_post()
    _dispatch_and_wait(post_id=post_id, user_id=USER_ID, keyword="装修公司",
                       brand_id=BRAND_ID, brand_name="某装修公司", city="杭州",
                       card_count=3)
    after = topic_row(tid)
    assert (after["status"], after["updated_at"]) == (before["status"],
                                                      before["updated_at"])
    assert list_topics(brand_id=BRAND_ID)["total"] == 1
