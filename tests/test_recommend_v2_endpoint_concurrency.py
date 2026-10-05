"""`/api/publish/media/recommend-v2` 并发化判别锁(性能第二刀 §A)。

改前端点先后 await 两次 `recommend_for_publish_v2`(实测 media 4417ms + wemedia 2445ms
= 6862ms 串行)。两次调用互相独立,只差 ``media_type``,改 ``asyncio.gather`` 后
墙钟 = max(两者)。

这里锁的是**行为**不是源码串:用一个会真的睡 0.4 秒的替身跑端点,串行必然 ≥0.8 秒,
并发必然接近 0.4 秒。把 gather 改回先后 await,本用例立刻红。
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

STUB_SLEEP = 0.4


def _fake_request(user_id: int = 7):
    return types.SimpleNamespace(state=types.SimpleNamespace(user={"user_id": user_id}))


def _install_stub(monkeypatch, calls):
    def _fake_recommend(**kw):
        calls.append(kw)
        time.sleep(STUB_SLEEP)
        return {
            "vertical": [], "generic": [], "matched_industry": "建筑建材",
            # 灰度态置 False,免得端点再去建 T1「AI 引用主干」区(那一块与本锁无关)
            "media_balance_enabled": False,
            "media_balance_reason": "configured_off",
            "citation_ranking_signal": "legacy_v2f",
        }

    monkeypatch.setattr(
        "services.placement_service.recommend_for_publish_v2", _fake_recommend
    )


def _run_endpoint(**kwargs):
    from api.publish_api import recommend_media_v2

    return asyncio.run(recommend_media_v2(_fake_request(), **kwargs))


def test_media_and_wemedia_run_concurrently(monkeypatch):
    """🔒 两次推荐必须并发,不能先后跑。"""
    calls: list[dict] = []
    _install_stub(monkeypatch, calls)

    started = time.perf_counter()
    out = _run_endpoint(industry="建筑建材", limit=8)
    elapsed = time.perf_counter() - started

    assert out["status"] == "success"
    assert len(calls) == 2, "media / wemedia 两次调用都必须发生"
    assert {c["media_type"] for c in calls} == {"media", "wemedia"}
    assert elapsed < STUB_SLEEP * 1.8, (
        f"墙钟 {elapsed:.2f}s —— 串行会是 ~{STUB_SLEEP * 2:.1f}s,并发才会 ~{STUB_SLEEP:.1f}s"
    )


def test_wemedia_skips_duplicate_question_family_work(monkeypatch):
    """🔒 wemedia 侧不重复算 T2。

    问题族 mix 与 media_type 无关,两次算的是同一份,而端点只读 media 那一份 ——
    生产 flywheel_judgment_log 里判定**成对出现**就是这个重复的指纹。
    """
    calls: list[dict] = []
    _install_stub(monkeypatch, calls)

    _run_endpoint(industry="建筑建材", limit=8)

    by_type = {c["media_type"]: c for c in calls}
    assert by_type["wemedia"].get("with_question_family") is False
    # media 侧必须照常算(端点要读它的 question_family_mix / combination_plan)
    assert by_type["media"].get("with_question_family", True) is True


def test_both_calls_get_identical_inputs_except_media_type(monkeypatch):
    """两次调用除 media_type / with_question_family 外入参必须一致 —— 否则"结果只差
    media_type"这个并发前提就不成立了。"""
    calls: list[dict] = []
    _install_stub(monkeypatch, calls)

    _run_endpoint(industry="建筑建材", limit=8, keyword="装修哪家好", brand_id=152)

    a, b = ({k: v for k, v in c.items()
             if k not in ("media_type", "with_question_family")} for c in calls)
    assert a == b
    assert a["industry"] == "建筑建材" and a["limit"] == 8
    assert a["keyword"] == "装修哪家好" and a["brand_id"] == 152
    assert a["user_id"] == 7, "中间件注入的是 request.state.user 字典,不是 user_id"


@pytest.mark.parametrize("raw_user, expected", [
    ({"user_id": 42}, 42),
    ({"user_id": "portal_abc"}, 0),   # portal 端拿不到 int → 0
    (None, 0),
])
def test_user_id_extraction_unchanged(monkeypatch, raw_user, expected):
    """并发化不能顺手改坏 90 天去重赖以工作的 user_id 解析。"""
    calls: list[dict] = []
    _install_stub(monkeypatch, calls)

    from api.publish_api import recommend_media_v2

    req = types.SimpleNamespace(state=types.SimpleNamespace(user=raw_user))
    asyncio.run(recommend_media_v2(req, industry="建筑建材", limit=8))
    assert all(c["user_id"] == expected for c in calls)
