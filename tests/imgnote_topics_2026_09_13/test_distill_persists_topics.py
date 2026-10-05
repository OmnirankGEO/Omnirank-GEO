"""WO_204 c1 判据 · 蒸馏任务**调**落表那一行(C1 的接线臂)。

🔴 为什么单独有这个文件:第一版判据验了 `insert_distilled_topics`(A),
   也验了蒸馏任务本来就有的那套状态机(B),但**「A 调 B」那一行没人验** ——
   Review 的毒 T12 当场证明了:把落表那段改掉,38 条判据**全绿**。
   这正是本仓反复踩的那一类(`nobody-verified-the-line-where-a-calls-b`)。

本文件真跑 `run_distill_task`,只桩掉花钱(`charge_on_success`)与花时间
(`distill_topics`)的两个外部件,然后**读库**看选题在不在。
"""
import asyncio
import contextlib

import pytest

from .conftest import BRAND_ID, USER_ID, conn

FEATURE = "geo_douyin_distill"


class _Charge:
    """记一笔:进没进 with、**是不是正常退出**(正常退出 = 真扣了)。"""

    def __init__(self):
        self.entered = 0
        self.charged = 0
        self.aborted = 0


@pytest.fixture
def stubbed(monkeypatch):
    """桩掉扣费与蒸馏本体。

    🔴 打在**源模块**上:`run_distill_task` 在函数体里
       `from middleware.billing import charge_on_success` /
       `from services.geo_douyin.topic_distiller import distill_topics`,
       查名发生在调用时。打在 `distill_task` 上无效,而**无效的桩看起来
       跟生效一模一样** —— 判据照样绿,只是真花了钱、真调了 LLM。
    """
    import middleware.billing as billing
    from services.geo_douyin import topic_distiller

    charge = _Charge()

    @contextlib.asynccontextmanager
    async def _charge_on_success(user_id, feature_code, **kw):
        charge.entered += 1
        try:
            yield
        except BaseException:
            charge.aborted += 1
            raise
        charge.charged += 1

    async def _distill(**kw):
        # 用**真的那两个 dataclass**,不自造 dict:自造的少一个字段就会在
        # 被测代码里炸成 AttributeError,而那种红看起来像被测对象坏了。
        return topic_distiller.DistillResult(
            topics=[topic_distiller.DistilledTopic(
                title="蒸出来的第 %d 题" % i, angle="角度%d" % i,
                card_outline=["c%d" % i], keyword="装修公司")
                for i in range(3)],
            ok=True)

    monkeypatch.setattr(billing, "charge_on_success", _charge_on_success)
    monkeypatch.setattr(topic_distiller, "distill_topics", _distill)
    return charge


def _new_task():
    from db.geo_douyin_db import create_distill_task
    return create_distill_task(brand_id=BRAND_ID, user_id=USER_ID,
                               keywords=["装修公司"])


def _run_task(task_id):
    from services.geo_douyin.distill_task import run_distill_task
    return asyncio.run(run_distill_task(
        task_id=task_id, user_id=USER_ID, brand_id=BRAND_ID,
        keywords=["装修公司"], brand_name="某装修公司", city="杭州",
        industry_key="zhuangxiu", want=3, feature_code=FEATURE))


def _task_row(task_id):
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT status, stage, result FROM geo_douyin_distill_tasks"
                    " WHERE id=%s", (int(task_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        c.close()


@pytest.fixture(autouse=True)
def clean_tasks():
    c = conn()
    try:
        c.cursor().execute("DELETE FROM geo_douyin_distill_tasks WHERE brand_id=%s",
                           (BRAND_ID,))
    finally:
        c.close()
    yield


# ═══════════════════════════════════════════════════════════════
# 接线臂:任务跑完 ⇒ 选题真的在表里
# ═══════════════════════════════════════════════════════════════
def test_a_successful_distill_actually_persists_the_topics(stubbed):
    """C1 接线臂:蒸馏成功 ⇒ 选题**逐条落表**,条数与回包一致。

    读的是**库里的行**,不是任务 result 里的 jsonb —— 那一坨本来就有 topics,
    拿它当证据等于没验落表。
    """
    from db.geo_douyin_db import list_topics

    assert list_topics(brand_id=BRAND_ID)["total"] == 0
    task_id = _new_task()
    _run_task(task_id)

    page = list_topics(brand_id=BRAND_ID)
    assert page["total"] == 3, "蒸馏跑完了,选题表里却是 %d 条" % page["total"]
    assert {t["title"] for t in page["topics"]} == {
        "蒸出来的第 0 题", "蒸出来的第 1 题", "蒸出来的第 2 题"}
    assert {t["source"] for t in page["topics"]} == {"distilled"}
    assert {t["distill_task_id"] for t in page["topics"]} == {task_id}
    # 城市取任务入参(蒸馏一次一个城市)
    assert {t["city"] for t in page["topics"]} == {"杭州"}
    assert stubbed.charged == 1, "成功却没扣费"

    row = _task_row(task_id)
    assert row["status"] == "succeeded"
    assert int((row["result"] or {}).get("topics_persisted") or 0) == 3


def test_persisting_failure_aborts_the_charge(stubbed, monkeypatch):
    """🔴 落表**在扣费的 with 体内**:落不进去就不扣费。

    挪到 with 外面 = 「扣了 130、任务显示成功、列表里一条都没有」——
    而那种坏法从任务状态上完全看不出来(result jsonb 里明明有 topics)。

    这条是 T12 那发毒真正该钉的位置:判读看的是**扣没扣费**,
    不是"落表函数被调用过"。
    """
    from db import geo_douyin_db as ddb
    from db.geo_douyin_db import list_topics

    def _boom(**kw):
        raise RuntimeError("落库炸了")

    monkeypatch.setattr(ddb, "insert_distilled_topics", _boom)

    task_id = _new_task()
    _run_task(task_id)

    assert stubbed.entered == 1
    assert stubbed.charged == 0, "落表失败却扣了费 —— 落表跑到 with 外面去了"
    assert stubbed.aborted == 1
    assert list_topics(brand_id=BRAND_ID)["total"] == 0
    assert _task_row(task_id)["status"] == "failed", (
        "落表失败要落终态,否则前端永远转圈、唯一索引还会把这个客户锁死")


def test_a_failed_distill_persists_nothing_and_charges_nothing(stubbed, monkeypatch):
    """反向对照:蒸馏本身失败 ⇒ 一条不落、一分不扣(既有语义,不能被本单改坏)。"""
    from db.geo_douyin_db import list_topics
    from services.geo_douyin import topic_distiller

    async def _fail(**kw):
        return topic_distiller.DistillResult(topics=[], ok=False,
                                             error="llm_unavailable")

    monkeypatch.setattr(topic_distiller, "distill_topics", _fail)

    task_id = _new_task()
    _run_task(task_id)

    assert list_topics(brand_id=BRAND_ID)["total"] == 0
    assert stubbed.charged == 0
    assert _task_row(task_id)["status"] == "failed"


def test_rerunning_the_same_task_does_not_duplicate(stubbed):
    """同一个 task 再跑一遍(重放)⇒ 选题不翻倍。"""
    from db.geo_douyin_db import list_topics

    task_id = _new_task()
    _run_task(task_id)
    _run_task(task_id)
    assert list_topics(brand_id=BRAND_ID)["total"] == 3
