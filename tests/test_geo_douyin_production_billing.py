"""GEO 抖音图文管线 v1 · 计费闭合锁(B 类:freeze → commit / release)

要锁的机制只有一条,但它是资金面:
    **每一条失败路径都必须 release_freeze,成功路径必须且只能 commit_freeze。**
"冻结了但既没 commit 也没 release" = 用户积分被永久卡住,比报错更糟。

打法:替换 billing 的三个 seam,遍历每个失败阶段,断言最终账目动作。
每条"必须 release"都配一条"成功必须 commit 且绝不 release"的反向面。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List

import pytest

from services.geo_douyin import production_task as pt
from services.geo_douyin.content_generator import GeneratedContent
from services.geo_douyin.image_pipeline import CardBatchResult, CardImageResult


@dataclass
class _Ledger:
    """记账探针:记录 billing 三个动作各被调了几次。"""
    frozen: List[dict] = field(default_factory=list)
    committed: List[dict] = field(default_factory=list)
    released: List[dict] = field(default_factory=list)
    # §15:每次落产物写进去的东西(判失败卡有没有留占位)
    assets: List[dict] = field(default_factory=list)


@pytest.fixture
def ledger(monkeypatch):
    lg = _Ledger()

    async def fake_freeze(user_id, feature_code, **kw):
        lg.frozen.append({"user_id": user_id, "feature_code": feature_code, **kw})
        return {"freeze_id": 9001, "amount": 120}

    async def fake_commit(**kw):
        lg.committed.append(kw)
        return {"ok": True}

    async def fake_release(**kw):
        lg.released.append(kw)
        return {"ok": True}

    monkeypatch.setattr("middleware.billing.freeze_points", fake_freeze)
    monkeypatch.setattr("middleware.billing.commit_freeze", fake_commit)
    monkeypatch.setattr("middleware.billing.release_freeze", fake_release)

    # DB 全部打桩(本锁只关心账目动作,不关心落库)
    monkeypatch.setattr("db.geo_douyin_db.create_task", lambda **kw: 555)
    monkeypatch.setattr("db.geo_douyin_db.update_task", lambda *a, **kw: None)
    # 异步化后新增的记账/进度写入(都不是资金动作,纯记录)
    monkeypatch.setattr("db.geo_douyin_db.set_task_freeze", lambda *a, **kw: None)
    monkeypatch.setattr("db.geo_douyin_db.set_task_total", lambda *a, **kw: None)
    monkeypatch.setattr("db.geo_douyin_db.bump_task_progress", lambda *a, **kw: 1)
    # 🔴 §15 部分成功交付新增的落库点。**必须全部打桩** ——
    #    漏一个就会真连库、抛异常、被外层兜底 except 接住变成 release,
    #    于是"部分失败退款"这条测试**看起来还是绿的,但绿错了原因**。
    #    (实测踩到:set_task_completing 没打桩,B3 变异因此存活。)
    monkeypatch.setattr("db.geo_douyin_db.set_task_completing", lambda *a, **kw: None)
    monkeypatch.setattr("db.geo_douyin_db.claim_task_settlement", lambda *a, **kw: None)
    # 自动补齐会去真生图 —— 这里让它恒失败,把"补不齐"那条路走出来
    async def _no_fill(*a, **kw):
        from services.geo_douyin.image_pipeline import CardImageResult
        return CardImageResult(idx=0, ok=False, error="stubbed")
    monkeypatch.setattr("services.geo_douyin.image_pipeline.render_one_card", _no_fill)
    # 落产物:记下每次写进去的 oss_keys / cards,给占位对齐那条锁用
    lg_assets = []
    monkeypatch.setattr("db.geo_douyin_db.update_post_assets",
                        lambda pid, **kw: lg_assets.append(kw))
    lg.assets = lg_assets
    monkeypatch.setattr("db.geo_douyin_db.set_post_status", lambda *a, **kw: None)
    monkeypatch.setattr("db.geo_douyin_db.update_post_content", lambda *a, **kw: None)
    # 🔴 update_post_assets 的桩在上面(要记录写进去的内容)。
    #    这里**不能**再打一遍空桩 —— 后设的会覆盖先设的,记录就永远是空的,
    #    而断言"根本没落产物"会把人引到错误的方向去查(实测踩到)。
    monkeypatch.setattr("db.geo_douyin_db.set_style_key", lambda *a, **kw: None)
    monkeypatch.setattr("db.geo_douyin_db.clear_closing_stale", lambda *a, **kw: None)
    # 🔴 加张计价(2026-08-03)会去价目表读每张单价。**必须打桩** ——
    #    没有它,card_count>4 的用例会因为读不到价目走 fail-closed 分支
    #    (`pricing_unavailable`,连冻结都没发生),于是所有账目断言都对不上。
    #    这是**正确行为**(读不到价目宁可不下单也不白送),测试要认这个前提。
    #    单价刻意取 100 与生产同值,让 `_run(card_count=5)` 的加价额可推算。
    lg.extra_unit_points = 100
    monkeypatch.setattr("services.geo_douyin.pricing._read_unit_points_sync",
                        lambda code: 100)
    return lg


def _ok_content(n=2):
    """内容卡按新的同构字段构造(entity/points/caveat),并带封面与收尾卡。"""
    # 🔴 2026-08-05 起 title/hashtags 由业务 AI 出(不再有模板池兜底),
    #    正文还必须点到客户名字 —— 两者缺一,生产会明确失败并退款。
    #    桩必须跟着这个契约走,否则测的就不是计费语义了。
    return GeneratedContent(
        body="我们深圳某某定制在深圳做全屋定制。" + "正文" * 50,
        brand_name_expected="深圳某某定制",
        title="深圳全屋定制哪家好？5家实测对比与避坑要点一次说清",
        hashtags=["深圳全屋定制", "全屋定制避坑", "深圳装修", "定制衣柜", "板材选择"],
        cover={"title": "深圳全屋定制哪家好", "subtitle": "对比"},
        closing={"headline": "选购总结", "summary": "按预算对号入座",
                 "brand_line": "深圳某某定制在深圳做全屋定制，12 年老店"},
        cards=[{"entity": f"卡{i}", "one_liner": "定位", "points": ["要点"],
                "metric": "", "caveat": "注意事项", "headline": f"卡{i}"}
               for i in range(n)],
    )


def _batch(ok_flags):
    cards = [
        CardImageResult(idx=i + 1, ok=flag,
                        oss_key=f"geo_douyin/1/card_{i+1}.png" if flag else "",
                        error="" if flag else "poll_failed")
        for i, flag in enumerate(ok_flags)
    ]
    return CardBatchResult(cards=cards, total_cost_usd=0.012)


def _run(**over):
    return asyncio.run(pt.run_image_post_production(
        post_id=over.pop("post_id", 1),
        user_id=over.pop("user_id", 42),
        keyword=over.pop("keyword", "全屋定制"),
        city=over.pop("city", "深圳"),
        card_count=over.pop("card_count", 2),
        **over,
    ))


# ─────────────────────────────────────────────────────────────
# 成功面:必须 commit,且【绝不】release
# ─────────────────────────────────────────────────────────────
def test_success_commits_and_never_releases(monkeypatch, ledger):
    async def fake_content(*_a, **_k):
        return _ok_content(2)

    async def fake_render(*_a, **_k):
        return _batch([True, True, True, True])   # 封面+2内容+收尾

    monkeypatch.setattr("services.geo_douyin.content_generator."
                        "generate_image_post_content", fake_content)
    monkeypatch.setattr("services.geo_douyin.image_pipeline.render_prompt_group",
                        fake_render)

    out = _run()
    assert out.ok is True
    assert len(ledger.frozen) == 1
    assert len(ledger.committed) == 1, "成功必须 commit_freeze"
    assert ledger.released == [], "成功路径绝不能 release(否则白干还退钱)"
    assert out.refunded is False


# ─────────────────────────────────────────────────────────────
# 失败面:每个阶段都必须 release,且【绝不】commit
# ─────────────────────────────────────────────────────────────
def test_copy_failure_releases(monkeypatch, ledger):
    async def fake_content(*_a, **_k):
        return GeneratedContent(body="", ok=False, error="llm_unavailable")

    monkeypatch.setattr("services.geo_douyin.content_generator."
                        "generate_image_post_content", fake_content)
    out = _run()
    assert out.ok is False and out.refunded is True
    assert len(ledger.released) == 1, "文案失败必须退积分"
    assert ledger.committed == [], "失败路径绝不能 commit"


def test_partial_above_threshold_neither_commits_nor_releases(monkeypatch, ledger):
    """🔴 §15 改语义(2026-08-03 Review 裁定):部分失败**不再**等于整条作废。

    旧规则是"没全成就整条退",实测把它打穿了 —— 单卡 ~0.9 时五张联合只有 59%,
    生产 5 次 0 成品、两次都是 4/5,而**已经生成并付过费的 4 张被主动丢掉**。

    新规则:封面成功 且 成功卡 ≥ 一半 → 进「补齐中」,
    冻结**既不 commit 也不 release**(补齐全组才扣;补不齐 12h sweeper 兜底)。
    """
    async def fake_content(*_a, **_k):
        return _ok_content(3)

    async def fake_render(*_a, **_k):
        return _batch([True, False, True, True, True])   # 5 张里 1 张失败,封面成功

    monkeypatch.setattr("services.geo_douyin.content_generator."
                        "generate_image_post_content", fake_content)
    monkeypatch.setattr("services.geo_douyin.image_pipeline.render_prompt_group",
                        fake_render)

    out = _run(card_count=5)
    assert out.ok is True, "达门槛却报失败 = 又把 4 张成品丢了"
    assert out.completing is True
    assert out.refunded is False
    assert ledger.released == [], "补齐中却 release = 用户的成品白丢"
    assert ledger.committed == [], "还没补齐就 commit = 没完成先扣钱"


def test_partial_below_threshold_still_releases(monkeypatch, ledger):
    """必须不命中面:低于门槛(封面失败)**维持现行全退**。
    封面在预览/缩略图/发布封面三处都露脸,缺了对用户没价值。"""
    async def fake_content(*_a, **_k):
        return _ok_content(3)

    async def fake_render(*_a, **_k):
        return _batch([False, True, True, True, True])   # 封面挂了,其余全成

    monkeypatch.setattr("services.geo_douyin.content_generator."
                        "generate_image_post_content", fake_content)
    monkeypatch.setattr("services.geo_douyin.image_pipeline.render_prompt_group",
                        fake_render)

    out = _run(card_count=5)
    assert out.ok is False and out.refunded is True
    assert len(ledger.released) == 1, "封面失败必须整体退"
    assert ledger.committed == []


def test_failed_card_is_persisted_as_empty_placeholder(monkeypatch, ledger):
    """🔴 失败卡在 oss_keys 里留**空串占位**,不能被删掉。

    删掉会让后面所有卡的下标整体前移 —— 用户点"重抽第 3 张"会抽到第 4 张。
    预览 / 卡片元数据 / 重抽 card_index 三者按同一下标对齐。
    """
    async def fake_content(*_a, **_k):
        return _ok_content(3)

    async def fake_render(*_a, **_k):
        return _batch([True, False, True, True, True])

    monkeypatch.setattr("services.geo_douyin.content_generator."
                        "generate_image_post_content", fake_content)
    monkeypatch.setattr("services.geo_douyin.image_pipeline.render_prompt_group",
                        fake_render)

    _run(card_count=5)
    assert ledger.assets, "根本没落产物"
    keys = ledger.assets[-1]["oss_keys"]
    assert len(keys) == 5, f"占位丢了,长度变成 {len(keys)}"
    assert keys[1] == "", "失败那张没留空串占位 → 后面的卡下标会前移"
    assert all(keys[i] for i in (0, 2, 3, 4)), "成功的卡反而没落 key"


def test_unexpected_exception_still_releases(monkeypatch, ledger):
    """兜底面:未预期异常也必须退,不能把冻结永久卡住。"""
    async def boom(*_a, **_k):
        raise RuntimeError("unexpected boom")

    monkeypatch.setattr("services.geo_douyin.content_generator."
                        "generate_image_post_content", boom)
    out = _run()
    assert out.ok is False and out.refunded is True
    assert len(ledger.released) == 1
    assert ledger.committed == []
    assert "unexpected" in out.error


def test_freeze_failure_does_not_release_or_commit(monkeypatch, ledger):
    """冻结本身失败 → 没冻上,就不该有任何后续账目动作(退一笔不存在的钱是错的)。"""
    async def bad_freeze(*_a, **_k):
        raise RuntimeError("insufficient points")

    monkeypatch.setattr("middleware.billing.freeze_points", bad_freeze)
    out = _run()
    assert out.ok is False
    assert ledger.committed == [] and ledger.released == []
    assert out.refunded is False
    assert "freeze_failed" in out.error


# ─────────────────────────────────────────────────────────────
# 账目守恒(§15 修订)
# ─────────────────────────────────────────────────────────────
# 原不变量:frozen 必须**恰好**被 commit 或 release 之一收口。
# §15 之后多出一种合法态:**补齐中** —— 冻结**故意**留着不动,
#   等补齐后 commit,或等 12h sweeper release。
# 🔴 所以不变量要改写成"恰好收口一次 **或** 明确处在补齐中",
#   而且补齐中必须**逐场景显式列出来**,不能写成"收口 0 次也放过" ——
#   那就等于把"积分被永久卡住"这个真 bug 一并放行了。
@pytest.mark.parametrize("scenario,expect", [
    ("success", "closed"),
    ("copy_fail", "closed"),
    ("image_fail_above_threshold", "completing"),   # §15 新增的合法态
    ("image_fail_below_threshold", "closed"),       # 封面挂 → 仍是全退
    ("boom", "closed"),
])
def test_every_freeze_is_closed_or_explicitly_completing(monkeypatch, ledger,
                                                         scenario, expect):
    async def fake_content(*_a, **_k):
        if scenario == "copy_fail":
            return GeneratedContent(body="", ok=False, error="llm_unavailable")
        if scenario == "boom":
            raise RuntimeError("boom")
        return _ok_content(2)

    flags = {
        "image_fail_above_threshold": [True, False, True, True],   # 封面成功,4 成 3
        "image_fail_below_threshold": [False, True, True, True],   # 封面挂
    }.get(scenario, [True, True, True, True])

    async def fake_render(*_a, **_k):
        return _batch(flags)

    monkeypatch.setattr("services.geo_douyin.content_generator."
                        "generate_image_post_content", fake_content)
    monkeypatch.setattr("services.geo_douyin.image_pipeline.render_prompt_group",
                        fake_render)

    out = _run()
    closed = len(ledger.committed) + len(ledger.released)
    assert len(ledger.frozen) == 1
    if expect == "closed":
        assert closed == 1, (
            f"[{scenario}] 冻结 1 笔但收口 {closed} 笔 —— 积分被永久卡住或重复处理")
        assert out.completing is False
    else:
        assert closed == 0, f"[{scenario}] 补齐中不该有任何账目动作,却有 {closed} 笔"
        assert out.completing is True, (
            f"[{scenario}] 收口 0 笔却**没有**标成补齐中 —— "
            f"那就是积分被永久卡住,不是合法态")


def test_task_ref_is_unique_per_call():
    """task_ref 是 freeze/commit/release 的对账键,撞号会退错单。"""
    refs = {pt.build_task_ref(1) for _ in range(50)}
    assert len(refs) == 50
