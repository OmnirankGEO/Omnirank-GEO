"""#184 d1 返修 W1–W3 —— 驱动 **run_image_post_production 本体**。

🔴 为什么补这三条:原来那 10 条**没有一条驱动生产任务**。`_freeze` 助手直接调
   `_freeze_ordinary_revision`,J3 钉的是「函数返回 None」而不是「生产任务不落
   ready、退款」。于是 d1 在生产上真正跑的三段 —— :314 建任务改走新门、
   5b 冻版本、输家 release —— **零判据**。
   Review 的两发毒证明了这一点:把 5b 的 `if not contract_task` 翻成
   `if contract_task`(普通链永不冻版本)、把输家分支 `and False` 掉,
   那 10 条**全绿**。我验了 A 也验了 B,但 A 调 B 那行没人验。

这三条全部**真跑** `run_image_post_production`,只桩掉「花钱」与「花时间」的外部件:
生图、文案 LLM、billing。桩 billing 是**数调用次数**,不是看 outcome 字段 ——
outcome 字段是被测代码自己写的,拿它当证据等于自证。
"""
import asyncio

import pytest

from .conftest import USER_ID, make_post, post_row, revisions_of, tasks_of


class _Calls:
    """把 billing 的三个动作各记一笔。"""

    def __init__(self):
        self.freeze = 0
        self.commit = 0
        self.release = 0


@pytest.fixture
def stubbed(monkeypatch):
    """桩掉外部件。

    🔴 打在**源模块**上:生产任务是在函数体里
       `from middleware.billing import commit_freeze, ...` 的,查名发生在调用时,
       所以打源模块有效(打在 production_task 上无效 —— 那儿根本没有这些名字)。
    """
    import middleware.billing as billing
    from services.geo_douyin import content_generator, image_pipeline, pricing

    calls = _Calls()

    async def _freeze_points(*a, **kw):
        calls.freeze += 1
        return {"freeze_id": 77000 + calls.freeze, "success": True}

    async def _commit_freeze(*a, **kw):
        calls.commit += 1
        return {"success": True}

    async def _release_freeze(*a, **kw):
        calls.release += 1
        return {"success": True}

    async def _extra_card_points(n):
        return 0

    async def _content(*a, **kw):
        # 🔴 用**真的那个 dataclass**,不自造形状:自造的 dict 少一个字段
        #    就会在被测代码里炸成 AttributeError,而那种红看起来像被测对象的问题。
        return content_generator.GeneratedContent(
            body="正文", title="标题", hashtags=["#木作"],
            cards=[{"headline": "H%d" % i, "body": "b%d" % i} for i in range(1)],
            cover={"title": "封面", "subtitle": "副标"},
            closing={"headline": "收尾", "summary": "小结"},
            visual={"scene": "studio", "primary_color": "#111",
                    "accent_color": "#eee"})

    def _specs(content, style, **kw):
        return [{"index": i, "headline": "H%d" % i, "kind": "content",
                 "prompt": "p%d" % i} for i in range(3)]

    async def _render(post_id, specs, **kw):
        # 🔴 同样用**真的那两个 dataclass**。我先自造了一份,少了 `all_ok`
        #    (它是 CardBatchResult 的 property),被测代码当场 AttributeError,
        #    而那条红看起来像被测对象坏了 —— 桩的形状错会伪装成被测对象的缺陷。
        return image_pipeline.CardBatchResult(
            cards=[image_pipeline.CardImageResult(
                idx=i, ok=True, oss_key="geo/img/w/%d.png" % i)
                for i in range(len(specs))],
            total_cost_usd=0.0)

    monkeypatch.setattr(billing, "freeze_points", _freeze_points)
    monkeypatch.setattr(billing, "commit_freeze", _commit_freeze)
    monkeypatch.setattr(billing, "release_freeze", _release_freeze)
    monkeypatch.setattr(pricing, "extra_card_points", _extra_card_points)
    monkeypatch.setattr(content_generator, "generate_image_post_content", _content)
    monkeypatch.setattr(image_pipeline, "build_prompts_for_group", _specs)
    monkeypatch.setattr(image_pipeline, "render_prompt_group", _render)
    return calls


def _run_production(post_id):
    from services.geo_douyin.production_task import run_image_post_production
    return asyncio.run(run_image_post_production(
        post_id=post_id, user_id=USER_ID, keyword="木作定制",
        brand_id=None, brand_name="QZQZ木作", city="深圳", card_count=3))


# ── W1 顺利路径:跑完 ⇒ ready + 有版本 + 版本与 post 逐字一致 ────────────
def test_w1_production_leaves_a_ready_post_with_a_matching_revision(stubbed):
    post_id = make_post(cards=3, status="generating")

    outcome = _run_production(post_id)

    assert outcome.ok is True, outcome.error
    row = post_row(post_id)
    assert str(row["status"]) == "ready"
    assert row["active_revision_id"] is not None, (
        "落 ready 了却没有锁定版本 ⇒ v2 素材准备会对这一篇 409,发不出去")
    rev = [r for r in revisions_of(post_id) if str(r["status"]) == "active"]
    assert len(rev) == 1
    manifest = rev[0]["asset_manifest"]
    assert list(manifest["oss_keys"]) == list(row["oss_keys"]), (
        "版本与库里那一版对不上 —— 发布链按版本发,页面按 post 看,两者会分叉")
    assert stubbed.commit == 1 and stubbed.release == 0


# ── W2 输家:先建第二代,再让第一代跑完 ────────────────────────────────
def test_w2_superseded_generation_releases_and_does_not_write_ready(stubbed):
    """CAS 输了 ⇒ 退款不结算、不落 ready、自己的任务 superseded。

    🔴 断言 billing 的**调用次数**,不是 outcome 字段:
       outcome 是被测代码自己填的,拿它当证据等于自证。
    """
    from db import geo_douyin_db as ddb
    from services.geo_douyin.production_task import run_image_post_production

    post_id = make_post(cards=3, status="generating")

    async def _race():
        task = asyncio.create_task(run_image_post_production(
            post_id=post_id, user_id=USER_ID, keyword="木作定制",
            brand_id=None, brand_name="QZQZ木作", city="深圳", card_count=3))
        # 🔴 必须**等生产把自己的任务行建出来**再插赢家:
        #    `sleep(0)` 只让出一次,而建任务在 to_thread 里 —— 赢家会抢在前面插,
        #    于是撞唯一索引的是**我的测试**,不是被测对象。那种红看起来一模一样。
        for _ in range(200):
            if await asyncio.to_thread(lambda: len(tasks_of(post_id))) >= 1:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("生产一直没建出任务行 —— 竞态没造出来,这条判据没测到东西")
        await asyncio.to_thread(
            ddb.create_task_with_generation, post_id=post_id, user_id=USER_ID,
            task_ref="w2-winner-%d" % post_id, freeze_id=None, progress_total=3)
        return await task

    outcome = asyncio.run(_race())

    assert outcome.superseded is True, "被新一代接管却没标 superseded"
    assert outcome.ok is False
    # 钱:退了一笔、**一笔都没结**
    assert stubbed.release == 1, "输家没退款 ⇒ 用户为一份作废的成品付了钱"
    assert stubbed.commit == 0, "输家把钱结了 ⇒ 同一篇被收两次"
    # 作品不许被输家写成 ready
    assert str(post_row(post_id)["status"]) != "ready"
    # 输家自己的任务标 superseded(不是 failed —— 没有任何东西坏掉)
    supers = [t for t in tasks_of(post_id) if str(t["status"]) == "superseded"]
    assert supers, tasks_of(post_id)


# ── W3 建任务门:生产跑出来的任务就是代际持有者 ─────────────────────────
def test_w3_production_task_is_the_generation_holder(stubbed):
    post_id = make_post(cards=3, status="generating")

    outcome = _run_production(post_id)

    row = post_row(post_id)
    assert row["active_generation_task_id"] == outcome.task_id, (
        ":314 没走 create_task_with_generation ⇒ 代际持有者不是这个任务 ⇒ CAS 恒零行")
    assert int(row["generation_epoch"]) >= 1
