"""判别测试 · 项1 单发端点 ``media_type`` 不落库(WO-ACCEPTANCE-3FIX 2026-08-05)。

坐实的事故(2026-08-05 生产实测,不是推断):

    item 509 · media_id=268322(mhz_wemedia「梦梦生活」)· 请求体 media_type='wemedia'
    · 落库 media_type = (NULL) · 外部单号 122026080510402910910457608

全表分布 ``mhz 467 / wemedia 21 / (NULL) 9 / svideo 1``。根因是单发端点
``api_publish`` 构造 ``items`` 时**整个键都没有**,而 ``create_order`` 用
``it.get("media_type")`` 取值 → ``None`` → NULL。批量端点 ``api_publish_batch``
一直带着这个键(``item.media_types[i] ...``),所以只有单发那一支中招。

代价在**重试链**:``api_confirm_resubmit`` 里 ``item.get("media_type") or "mhz"``
会把 NULL 的自媒体单当软文重发(走 ``publish`` 而不是 ``publish_wemedia``)。

每条「必须命中」都配了成对的「必须不命中」,命名 ``*__must_hit`` / ``*__must_not_hit``。
变异清单见 ``tests/mutation_acc3fix_media_type.py``。
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from db.meijiehezi_db import (
    MEDIA_TYPE_MHZ,
    MEDIA_TYPE_SVIDEO,
    MEDIA_TYPE_WEMEDIA,
    canonical_media_type,
)


# ===========================================================================
# A. 归一函数本身(纯函数,穷举)
# ===========================================================================
def test_canonical_wemedia_stays_wemedia__must_hit():
    assert canonical_media_type("wemedia") == MEDIA_TYPE_WEMEDIA


def test_canonical_wemedia_is_not_softtext__must_not_hit():
    """成对反向:自媒体绝不许被归一成软文 —— 那正是本单要挡的错。"""
    assert canonical_media_type("wemedia") != MEDIA_TYPE_MHZ


def test_canonical_article_and_empty_become_mhz__must_hit():
    """请求侧软文写法('article' 是 PublishOrderRequest 的默认值)→ DB 口径 'mhz'。"""
    assert canonical_media_type("article") == MEDIA_TYPE_MHZ
    assert canonical_media_type("") == MEDIA_TYPE_MHZ
    assert canonical_media_type("mhz") == MEDIA_TYPE_MHZ


def test_canonical_article_does_not_leak_raw_word__must_not_hit():
    """成对反向:'article' 不许原样落库。

    落了就会在 ``publish-history`` 的筛选下拉里多出第 4 个值 ——
    ``SELECT DISTINCT media_type ... IS NOT NULL AND <> ''``(db/meijiehezi_db.py)
    直接把它做成一个新选项,而前端 ``PublishHistory.tsx`` 对 'article' 和 'mhz'
    都渲染成"软文" → 两个一模一样的选项。
    """
    assert canonical_media_type("article") != "article"


def test_canonical_none_stays_none__must_hit():
    """🔴 工单 §1.4 的红线:``None`` 是"不知道",不是"软文"。

    ``create_order`` 拿到的 ``None`` 必须继续是 ``None``,在那里兜底成 'mhz'
    会把「自媒体单被当软文」固化成正确行为,比 NULL 更难查。
    """
    assert canonical_media_type(None) is None


def test_canonical_none_is_not_mhz__must_not_hit():
    assert canonical_media_type(None) != MEDIA_TYPE_MHZ


def test_canonical_svideo_untouched__must_hit():
    """svideo 独立路径不受影响(工单 §1.5 第 4 行)。"""
    assert canonical_media_type("svideo") == MEDIA_TYPE_SVIDEO


def test_canonical_svideo_not_normalized_to_mhz__must_not_hit():
    assert canonical_media_type("svideo") != MEDIA_TYPE_MHZ


def test_canonical_output_fits_varchar10__must_hit():
    """落库列是 VARCHAR(10):归一结果必须都塞得下。

    生产实测 ``character_maximum_length = 10``。远端接口把短视频叫
    ``short_video``(11 字符),原样落库会 INSERT 报错。
    """
    for probe in ("wemedia", "article", "", "mhz", "svideo", "short_video", "自媒体x"):
        out = canonical_media_type(probe)
        assert out is not None and len(out) <= 10, probe


# ===========================================================================
# B. 单发端点真调用:items 里到底有没有这个键
#    —— 断言打在**行为**(传给 create_order 的载荷)上,不是打在源码字符串上。
# ===========================================================================
class _Req:
    """最小 PublishOrderRequest 替身:只带 api_publish 会读的字段。"""

    def __init__(self, media_type: str):
        self.article_id = 4001
        self.article_title = "测试稿"
        self.media_ids = [268322]
        self.media_names = ["梦梦生活"]
        self.cost_points = [130]
        self.cost_yuan = [1.0]
        self.media_type = media_type
        self.brand_id = None
        self.request_id = None
        self.republish_from_sn = None

    def model_dump(self):
        return dict(self.__dict__)


class _Captured(RuntimeError):
    pass


def _run_publish_capture(monkeypatch, media_type: str) -> dict:
    """跑一遍真的 ``api_publish``,把它传给 ``create_order`` 的第 4 个参数抓回来。

    做法:``create_order`` 替身记录 items 后抛异常 → 端点走退费补偿分支 → 抛 500。
    退费也一并替身掉(否则会去打真的钱包)。我们只要 items。
    """
    import api.meijiehezi_api as mod
    import middleware.billing as billing

    box: dict = {}

    def _fake_create_order(user_id, article_id, title, items, **kw):
        box["items"] = items
        raise _Captured("captured")

    async def _fake_deduct(*a, **kw):
        return {"success": True, "charge_tx_id": "tx-test", "admin_exempt": False}

    async def _fake_refund(*a, **kw):
        return {"success": True}

    monkeypatch.setattr(mod, "_get_user", lambda request: {"user_id": 112, "is_admin": True})
    monkeypatch.setattr(mod, "_publish_approval_gate", lambda request, **kw: None)
    monkeypatch.setattr(mod, "_require_article_access", lambda request, aid: None)
    monkeypatch.setattr(mod, "_require_article_review_for_publish", lambda aid: None)
    monkeypatch.setattr(mod, "_require_strict_media_presubmit", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_get_publish_markup", lambda: 1.0)
    monkeypatch.setattr(mod, "_recompute_publish_charge", lambda specs, markup: [(130, 1.0)])
    monkeypatch.setattr(mod, "_get_canonical_article_title", lambda aid: "测试稿")
    monkeypatch.setattr(mod, "create_order", _fake_create_order)
    monkeypatch.setattr(billing, "deduct_points", _fake_deduct)
    monkeypatch.setattr(billing, "refund_points", _fake_refund)

    # 去重检查与退费通知都是函数内 import,替身要打在被 import 的模块上
    import db.meijiehezi_db as mhz_db
    monkeypatch.setattr(mhz_db, "find_active_orders_for_media", lambda aid, mids: [])
    fake_notif = types.ModuleType("services.notification_events")
    fake_notif.publication_refund_context = lambda key: {"key": key}
    monkeypatch.setitem(sys.modules, "services.notification_events", fake_notif)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.api_publish(_Req(media_type), request=object()))
    assert exc.value.status_code == 500  # 走的是 create_order 失败补偿分支
    assert "items" in box, "create_order 根本没被调到,后面的断言全部无效"
    return box["items"]


def test_single_publish_wemedia_lands_wemedia__must_hit(monkeypatch):
    """事故本体:单发 media_type='wemedia' → items 里必须是 'wemedia'。"""
    items = _run_publish_capture(monkeypatch, "wemedia")
    assert items and items[0].get("media_type") == MEDIA_TYPE_WEMEDIA


def test_single_publish_wemedia_is_never_null_or_mhz__must_not_hit(monkeypatch):
    """成对反向:既不许 NULL(=事故现状),也不许 'mhz'(=重试走错分支)。"""
    items = _run_publish_capture(monkeypatch, "wemedia")
    assert items[0].get("media_type") is not None
    assert items[0].get("media_type") != MEDIA_TYPE_MHZ


def test_single_publish_softtext_lands_mhz__must_hit(monkeypatch):
    """软文单(前端不传 media_type → 模型默认 'article')落 'mhz'。

    🔴 与改前的差别只有 NULL → 'mhz',**所有消费点对这两者逐位等价**:
      · ``api/meijiehezi_api.py:1614`` ``(oi.get("media_type") or "mhz") not in (...)``
      · ``api/meijiehezi_api.py:3695`` ``it.get("media_type") or "mhz"``
      · ``api/meijiehezi_api.py:3860`` ``item.get("media_type") or "mhz"``
      · ``db/meijiehezi_db.py`` 筛选下拉 ``DISTINCT ... IS NOT NULL AND <> ''``
        —— 'mhz' 已经是 467 行里的既有值,不产生新选项。
    等价证明见交付说明 §项1-等价性。
    """
    for probe in ("article", ""):
        items = _run_publish_capture(monkeypatch, probe)
        assert items[0].get("media_type") == MEDIA_TYPE_MHZ, probe


def test_single_publish_softtext_and_wemedia_differ__must_not_hit(monkeypatch):
    """🔴 工单 §1.5 的反向对照原文:两条 media_type 必须**不同**。

    若都相同,说明这个字段根本没在生效,上面所有断言作废。
    """
    soft = _run_publish_capture(monkeypatch, "article")[0].get("media_type")
    wm = _run_publish_capture(monkeypatch, "wemedia")[0].get("media_type")
    assert soft != wm, f"软文与自媒体落库同值({soft!r}),字段没生效,判据作废"


# ===========================================================================
# C. 批量端点不许被顺手"统一" —— 它是对的
# ===========================================================================
def test_batch_endpoint_still_builds_its_own_media_type__must_hit():
    """批量那一支仍用 ``item.media_types[i]``,没被改成走单发的归一。"""
    import inspect

    import api.meijiehezi_api as mod

    src = inspect.getsource(mod.api_publish_batch)
    assert '"media_type": item.media_types[i]' in src


def test_batch_endpoint_does_not_call_canonical__must_not_hit():
    """成对反向:批量端点里不许出现单发那条归一调用(否则就是"统一"了两支)。"""
    import inspect

    import api.meijiehezi_api as mod

    src = inspect.getsource(mod.api_publish_batch)
    assert "_canonical_media_type(" not in src


def test_create_order_still_reads_raw_key__must_hit():
    """🔴 工单 §1.4:``create_order`` 那边**不许**加默认值。

    这是本包最容易被"顺手优化"掉的一条:在 DB 层 ``or "mhz"`` 会把
    「自媒体单被当软文」固化成正确行为,比 NULL 更难查。
    """
    import inspect

    import db.meijiehezi_db as mhz_db

    src = inspect.getsource(mhz_db.create_order)
    assert 'it.get("media_type")' in src
    assert 'it.get("media_type") or' not in src
    assert 'it.get("media_type", ' not in src
