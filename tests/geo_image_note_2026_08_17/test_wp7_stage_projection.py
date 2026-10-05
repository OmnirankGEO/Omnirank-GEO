"""WP7 · 六阶段投影的判别测试(规格 03 §10「判别测试」逐条)。

本文件全部打**纯函数核心**,不需要数据库 —— 于是每一条反向变异都能直接构造。
PG16 行为(quote predicate 真的写在 SQL 里、真库上双 quote 不串)在
`test_wp7_projection_pg16.py`。

## 判据纪律

每一条"必须命中"都配一条"必须不命中"。理由已经在本仓交过学费:
只断言正向的判据无法区分"逻辑对"与"恒真"。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from services.publication_stage_projection import (
    FAILED_STATES,
    OBSERVED_PUBLICATION_STATES,
    PUBLISHED_STATES,
    STAGES,
    SUBMITTED_STATES,
    ProjectionUnavailable,
    freeze_projection,
    has_published_occurrence,
    project_stages,
    projections_comparable,
    source_versions,
    unit_key,
)

T0 = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
# 🔴 [2026-08-20 WO-D ④] 原为写死的 `datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)`。
#   兄弟文件 test_wp7_projection_pg16.py 的同名锚**正是**在 2026-08-20 当天引爆的
#   (挂钟越过写死 CUTOFF → 三条判据永久红,红因与被测代码无关),已由 1165b5e80
#   改成相对锚。本文件当时没跟着改 —— 它全部走纯函数、每个输入都由 T0 显式推出,
#   所以没炸;但**留着绝对日期就是留着引信**,下一个往这里加真库夹具的人就会踩。
#   改成相对锚,值**逐位相同**(2026-08-01 + 19d = 2026-08-20T00:00Z),
#   由 test_cutoff_anchor_is_relative_not_wall_clock 钉住。
CUTOFF = T0 + timedelta(days=19)


def test_cutoff_anchor_is_relative_not_wall_clock():
    """🔴 判据自伤锁:本文件的时间锚**只许相对**,不许出现绝对日期字面量。

    兄弟文件 test_wp7_projection_pg16.py 的同名 CUTOFF 在 2026-08-20 当天引爆过
    (挂钟越过写死日期 → 三条判据永久红,红因与被测代码无关)。绝对日期在源码里
    活着,就还会有下一次。

    判据成对:
      · 必须命中 —— 源码里 T0/CUTOFF 两行不含 `datetime(20xx, ...)` 绝对构造;
        且 CUTOFF 与旧值**逐位相同**(改锚不许顺手改语义)。
      · 必须不命中 —— 把 CUTOFF 写回 `datetime(2026, 8, 20, ...)`,本条立刻红。
    """
    import pathlib as _p
    import re as _re

    src = _p.Path(__file__).read_text(encoding="utf-8")
    body = chr(10).join(l for l in src.splitlines()
                        if not l.strip().startswith(chr(35)))   # 去注释,别被本锁的说明文字自撞
    absolute = _re.findall(r"^\s*CUTOFF\s*=\s*datetime\(\s*20\d\d\s*,", body, _re.M)
    assert not absolute, (
        "CUTOFF 又写回绝对日期了 —— 挂钟越过它,判据会永久红且红因与被测代码无关。"
        "写法:CUTOFF = T0 + timedelta(days=N)"
    )
    # 改锚不许顺手改语义:值必须仍是 2026-08-20T00:00Z
    assert CUTOFF == datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    assert CUTOFF - T0 == timedelta(days=19)
Q1, Q2 = 9001, 9002
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"


def _pub(quote_id=Q1, *, slot="s1", status="published", published=T0,
         submitted=None, url=URL_A, body_proof=True, retracted=None,
         availability=None, article_id=None, post_id=None):
    return {
        "quote_id": quote_id,
        "delivery_slot_key": slot,
        "geo_post_id": post_id,
        "article_id": article_id,
        "status": status,
        "submitted_at": submitted if submitted is not None else published,
        "published_at": published,
        "retracted_at": retracted,
        "availability": availability,
        "normalized_url": url,
        "body_proof": body_proof,
    }


def _mon(quote_id=Q1, *, tested=T0 + timedelta(days=1), citations=None, kw=1, rid=1):
    return {
        "id": rid,
        "quote_id": quote_id,
        "tested_at": tested,
        "confirmed_keyword_id": kw,
        "search_citations": json.dumps([{"url": u} for u in (citations or [])]),
    }


def _project(**kw):
    base = dict(
        quote_id=Q1, cutoff=CUTOFF, allocated_capacity=3,
        produced_units=[], publication_attempts=[], monitoring_rows=[],
        quote_keyword_ids=[1],
    )
    base.update(kw)
    return project_stages(**base)


def _count(proj, stage):
    return proj["stages"][stage]["count"]


# --------------------------------------------------------------------------
# 0. 契约形态
# --------------------------------------------------------------------------

def test_stage_order_is_the_contract():
    proj = _project()
    assert tuple(proj["stages"]) == STAGES
    assert STAGES == (
        "allocated_capacity", "produced_ready", "submitted",
        "published_active", "monitored_covered", "strictly_attributed")


def test_observed_production_states_are_all_classified():
    """生产实际出现过的每一个 status 都必须落进三个集合之一。

    反向:构造一个不存在的 status,断言它**不在**并集里 —— 否则本判据恒真。
    """
    union = PUBLISHED_STATES | SUBMITTED_STATES | FAILED_STATES
    assert OBSERVED_PUBLICATION_STATES <= union
    assert "totally_made_up_state" not in union


def test_source_versions_carry_all_five_axes():
    versions = source_versions()
    for axis in ("projector", "schema", "monitoring_eligibility",
                 "attribution_metric", "url_normalization", "service_completion"):
        assert versions.get(axis), f"source_versions 缺 {axis}"


def test_eligibility_version_tracks_the_predicate_not_a_hand_constant():
    """改监测资格谓词 → 版本必须跟着变(手维护常量做不到)。"""
    import services.publication_stage_projection as mod
    before = source_versions()["monitoring_eligibility"]
    original = mod.AGGREGATE_ELIGIBLE_SQL
    try:
        mod.AGGREGATE_ELIGIBLE_SQL = original + " AND 1=1"
        after = source_versions()["monitoring_eligibility"]
    finally:
        mod.AGGREGATE_ELIGIBLE_SQL = original
    assert after != before
    assert source_versions()["monitoring_eligibility"] == before  # 恢复后一致


# --------------------------------------------------------------------------
# 1. 容量:None 与 0 是两件事
# --------------------------------------------------------------------------

def test_capacity_none_is_unavailable_not_zero():
    proj = _project(allocated_capacity=None)
    stage = proj["stages"]["allocated_capacity"]
    assert stage["available"] is False and stage["count"] is None
    assert stage["reason"] == "capacity_not_issued"


def test_capacity_zero_is_available_and_zero():
    """E02「报价图文为 0」:签发了但确实是 0 篇,必须是可用的 0,不是 unavailable。"""
    stage = _project(allocated_capacity=0)["stages"]["allocated_capacity"]
    assert stage["available"] is True and stage["count"] == 0


# --------------------------------------------------------------------------
# 2. quote 隔离(WP7 的头号缺陷)
# --------------------------------------------------------------------------

def test_other_quote_publication_never_counts():
    proj = _project(publication_attempts=[_pub(Q2, slot="sX")])
    assert _count(proj, "published_active") == 0
    assert proj["quality_counts"]["attempt_wrong_quote"] == 1


def test_q1_strict_hit_does_not_leak_into_q2():
    """规格:「同品牌 Q1 有 strict hit、Q2 无 hit:Q2 的 strictly_attributed 必须为 0」。"""
    q1_rows = [_pub(Q1, slot="s1", url=URL_A)]
    q1_mon = [_mon(Q1, citations=[URL_A])]
    q1 = project_stages(quote_id=Q1, cutoff=CUTOFF, allocated_capacity=1,
                        produced_units=[], publication_attempts=q1_rows,
                        monitoring_rows=q1_mon, quote_keyword_ids=[1])
    assert _count(q1, "strictly_attributed") == 1          # 正向:确实命中

    # Q2 拿到**同一批品牌数据**(brand merge 会发生的形态),但自己没有命中
    q2 = project_stages(quote_id=Q2, cutoff=CUTOFF, allocated_capacity=1,
                        produced_units=[], publication_attempts=q1_rows,
                        monitoring_rows=q1_mon, quote_keyword_ids=[1])
    assert _count(q2, "strictly_attributed") == 0
    assert _count(q2, "published_active") == 0


def test_monitoring_of_another_quote_is_rejected():
    proj = _project(publication_attempts=[_pub(Q1)], monitoring_rows=[_mon(Q2)])
    assert _count(proj, "monitored_covered") == 0
    assert proj["quality_counts"]["monitoring_wrong_quote"] == 1


def test_monitoring_keyword_outside_quote_is_rejected():
    proj = _project(publication_attempts=[_pub(Q1)],
                    monitoring_rows=[_mon(Q1, kw=777)], quote_keyword_ids=[1, 2])
    assert _count(proj, "monitored_covered") == 0
    assert proj["quality_counts"]["monitoring_keyword_outside_quote"] == 1


# --------------------------------------------------------------------------
# 3. pending / failed / unknown 不计 published_active
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["pending", "failed", "unknown", "rejected",
                                   "submitted", "awaiting_action"])
def test_non_published_states_do_not_count_as_published(state):
    proj = _project(publication_attempts=[_pub(status=state)])
    assert _count(proj, "published_active") == 0


@pytest.mark.parametrize("state", sorted(PUBLISHED_STATES))
def test_published_states_do_count(state):
    """反向对照:上一条如果是恒 0,这一条会红。"""
    proj = _project(publication_attempts=[_pub(status=state)])
    assert _count(proj, "published_active") == 1


def test_failed_after_submit_still_counts_as_submitted():
    proj = _project(publication_attempts=[
        _pub(status="failed", submitted=T0, published=None)])
    assert _count(proj, "submitted") == 1
    assert _count(proj, "published_active") == 0


def test_failed_before_submit_does_not_count_as_submitted():
    proj = _project(publication_attempts=[
        _pub(status="failed", submitted=None, published=None)])
    assert _count(proj, "submitted") == 0
    assert proj["quality_counts"]["attempt_failed_before_submit"] == 1


# --------------------------------------------------------------------------
# 4. 同 slot 多次 retry / 两次成功 仍最多计 1
# --------------------------------------------------------------------------

def test_same_slot_multiple_attempts_count_once():
    attempts = [
        _pub(slot="s1", status="failed", submitted=T0, published=None),
        _pub(slot="s1", status="published", published=T0 + timedelta(hours=1)),
        _pub(slot="s1", status="published", published=T0 + timedelta(hours=2)),
    ]
    proj = _project(publication_attempts=attempts)
    assert _count(proj, "submitted") == 1
    assert _count(proj, "published_active") == 1


def test_two_distinct_slots_count_two():
    """反向对照:去重不是"永远返回 1"。"""
    proj = _project(publication_attempts=[_pub(slot="s1"), _pub(slot="s2", url=URL_B)])
    assert _count(proj, "published_active") == 2


# --------------------------------------------------------------------------
# 5. 撤稿 / 替换
# --------------------------------------------------------------------------

def test_retracted_before_cutoff_drops_out_of_active():
    proj = _project(publication_attempts=[
        _pub(retracted=T0 + timedelta(days=2))])
    assert _count(proj, "published_active") == 0
    assert proj["quality_counts"]["publication_retracted_at_cutoff"] == 1


def test_retraction_after_cutoff_is_still_active_at_that_cutoff():
    """可复现性:同一 cutoff 重跑必须得到同一组数,撤稿不能倒着抹掉历史。"""
    proj = project_stages(
        quote_id=Q1, cutoff=T0 + timedelta(days=1), allocated_capacity=1,
        produced_units=[], monitoring_rows=[], quote_keyword_ids=[1],
        publication_attempts=[_pub(retracted=T0 + timedelta(days=5))])
    assert _count(proj, "published_active") == 1


def test_retracted_publication_keeps_occurrence():
    """规格:「撤稿仍保留 occurrence ... active 另按 cutoff 归零」。"""
    proj = _project(publication_attempts=[_pub(retracted=T0 + timedelta(days=2))])
    assert _count(proj, "published_active") == 0
    assert has_published_occurrence(proj) is True


def test_zero_occurrence_quote_reports_no_occurrence():
    """反向对照:occurrence 不是恒真。"""
    assert has_published_occurrence(_project()) is False


def test_availability_retracted_without_timestamp_falls_back_to_literal():
    proj = _project(publication_attempts=[_pub(availability="retracted")])
    assert _count(proj, "published_active") == 0


def test_strict_hit_on_retracted_a_cannot_be_stitched_onto_replacement_b():
    """规格:「A publication 有严格命中后撤稿、B replacement 未命中:
    不能用 A ledger + B active 拼成 strictly_attributed」。"""
    a = _pub(slot="s1", url=URL_A, published=T0, retracted=T0 + timedelta(days=2))
    b = _pub(slot="s1", url=URL_B, published=T0 + timedelta(days=3))
    mon = [_mon(Q1, tested=T0 + timedelta(days=1), citations=[URL_A])]  # 只命中 A
    proj = _project(publication_attempts=[a, b], monitoring_rows=mon)
    assert _count(proj, "published_active") == 1          # B 现役
    assert _count(proj, "strictly_attributed") == 0       # A 的命中不算在 B 头上


def test_replacement_b_does_not_inherit_a_monitoring_coverage():
    """规格:「A 已监测后撤稿、B replacement 刚发布但尚未监测:B 不得继承 A coverage」。"""
    a = _pub(slot="s1", url=URL_A, published=T0, retracted=T0 + timedelta(days=2))
    b = _pub(slot="s1", url=URL_B, published=T0 + timedelta(days=3))
    mon = [_mon(Q1, tested=T0 + timedelta(days=1))]       # 监测发生在 B 发布之前
    proj = _project(publication_attempts=[a, b], monitoring_rows=mon)
    assert _count(proj, "monitored_covered") == 0


def test_coverage_counts_when_monitoring_is_after_active_publication():
    """反向对照:上一条不是"覆盖永远 0"。"""
    b = _pub(slot="s1", url=URL_B, published=T0)
    mon = [_mon(Q1, tested=T0 + timedelta(days=1))]
    proj = _project(publication_attempts=[b], monitoring_rows=mon)
    assert _count(proj, "monitored_covered") == 1


# --------------------------------------------------------------------------
# 6. 严格归因:同源 + 正文证据 + 时序
# --------------------------------------------------------------------------

def test_strict_requires_body_proof():
    proj = _project(publication_attempts=[_pub(body_proof=False)],
                    monitoring_rows=[_mon(Q1, citations=[URL_A])])
    assert _count(proj, "strictly_attributed") == 0
    assert proj["quality_counts"]["publication_snapshot_missing"] == 1


def test_strict_requires_same_canonical_url():
    proj = _project(publication_attempts=[_pub(url=URL_A)],
                    monitoring_rows=[_mon(Q1, citations=[URL_B])])
    assert _count(proj, "strictly_attributed") == 0


def test_prepublication_citation_is_not_a_strict_hit():
    proj = _project(
        publication_attempts=[_pub(published=T0 + timedelta(days=5), url=URL_A)],
        monitoring_rows=[_mon(Q1, tested=T0 + timedelta(days=6), citations=[URL_A]),
                         _mon(Q1, tested=T0 + timedelta(days=1), citations=[URL_A], rid=2)])
    # 后发的那条命中,先于发布的那条被记 quality 而不是命中
    assert _count(proj, "strictly_attributed") == 1
    assert proj["quality_counts"]["prepublication_citation"] == 1


def test_strict_ledger_records_the_source_tuple():
    proj = _project(publication_attempts=[_pub(url=URL_A)],
                    monitoring_rows=[_mon(Q1, citations=[URL_A])])
    ledger = proj["strict_attribution_ledger"]
    assert len(ledger) == 1
    assert ledger[0]["normalized_url"] == URL_A
    assert ledger[0]["unit_key"] == "slot:s1"


# --------------------------------------------------------------------------
# 7. 稳定键缺失 → unattributed,绝不按品牌+日期猜
# --------------------------------------------------------------------------

def test_no_stable_key_stays_unattributed():
    """一个稳定键都没有(无 slot / 无 post / 无 article / **URL 也不合法**)→ unattributed。

    规格列的稳定键是「规范化 URL / provider publication id 等」,所以 URL 合法时
    它就是一个稳定键;真正该被拒的是**连 URL 都没有或不可规范化**的行。
    """
    row = _pub(slot=None, url="not-a-url")
    row["article_id"] = None
    row["geo_post_id"] = None
    proj = _project(publication_attempts=[row])
    assert _count(proj, "published_active") == 0
    assert proj["quality_counts"]["attempt_unattributed"] == 1


def test_url_only_publication_is_attributed():
    """反向对照:上一条不是"没 slot 就一律丢弃"。人工登记链常常只有 URL。"""
    row = _pub(slot=None, url=URL_A)
    row["article_id"] = None
    row["geo_post_id"] = None
    proj = _project(publication_attempts=[row])
    assert _count(proj, "published_active") == 1
    assert proj["unit_keys"]["published_active"] == [f"url:{URL_A}"]


def test_unit_key_priority_and_absence():
    assert unit_key({"delivery_slot_key": "s", "geo_post_id": 7, "article_id": 9}) == "slot:s"
    assert unit_key({"geo_post_id": 7, "article_id": 9}) == "post:7"
    assert unit_key({"article_id": 9}) == "article:9"
    assert unit_key({"normalized_url": URL_A}) == f"url:{URL_A}"
    # 🔴 只有品牌 + 日期时必须放弃,不许猜一个键出来
    assert unit_key({"brand_id": 5, "published_at": T0}) is None
    assert unit_key({"brand_id": 5, "normalized_url": "ftp://x/y"}) is None


# --------------------------------------------------------------------------
# 8. cutoff 与冻结可复现
# --------------------------------------------------------------------------

def test_publication_after_cutoff_is_invisible():
    proj = project_stages(
        quote_id=Q1, cutoff=T0, allocated_capacity=1, produced_units=[],
        monitoring_rows=[], quote_keyword_ids=[1],
        publication_attempts=[_pub(published=T0 + timedelta(days=1),
                                   submitted=T0 + timedelta(days=1))])
    assert _count(proj, "published_active") == 0
    assert _count(proj, "submitted") == 0


def test_same_cutoff_reproduces_identical_frozen_report():
    args = dict(publication_attempts=[_pub()], monitoring_rows=[_mon(Q1, citations=[URL_A])])
    a = freeze_projection(_project(**args))
    b = freeze_projection(_project(**args))
    assert a["reproducibility_hash"] == b["reproducibility_hash"]
    assert a["cutoff"] == b["cutoff"]


def test_different_stage_tuple_changes_the_frozen_hash():
    """反向对照:哈希不是常量。"""
    a = freeze_projection(_project(publication_attempts=[_pub()]))
    b = freeze_projection(_project(publication_attempts=[]))
    assert a["reproducibility_hash"] != b["reproducibility_hash"]


def test_version_change_blocks_silent_aggregation():
    a = freeze_projection(_project())
    b = freeze_projection(_project())
    assert projections_comparable(a, b) is True
    b["source_versions"]["projector"] = "quote-stage-projection-v9.9"
    assert projections_comparable(a, b) is False


def test_frozen_report_carries_watermark():
    frozen = freeze_projection(_project(), watermark={"attempt_rows": 4})
    assert frozen["watermark"] == {"attempt_rows": 4}
    assert set(frozen["source_versions"]) == set(source_versions())


# --------------------------------------------------------------------------
# 9. 已制作
# --------------------------------------------------------------------------

def test_produced_dedupes_and_respects_cutoff():
    units = [
        {"delivery_slot_key": "s1", "created_at": T0},
        {"delivery_slot_key": "s1", "created_at": T0},          # 同单元
        {"article_id": 5, "created_at": CUTOFF + timedelta(days=1)},  # cutoff 之后
        {"article_id": 6, "created_at": T0},
    ]
    proj = _project(produced_units=units)
    assert _count(proj, "produced_ready") == 2


def test_stages_need_not_be_monotonic():
    """容量 3、做了 1、发了 0 —— 合法形态,投影不做单调纠正。"""
    proj = _project(allocated_capacity=3,
                    produced_units=[{"delivery_slot_key": "s1", "created_at": T0}])
    assert (_count(proj, "allocated_capacity"), _count(proj, "produced_ready"),
            _count(proj, "published_active")) == (3, 1, 0)


# --------------------------------------------------------------------------
# 10. 投影不可用必须响亮
# --------------------------------------------------------------------------

def test_missing_quote_id_raises_instead_of_returning_zeros():
    from services.publication_stage_sources import load_quote_projection
    with pytest.raises(ProjectionUnavailable):
        load_quote_projection(object(), quote_id=0)
