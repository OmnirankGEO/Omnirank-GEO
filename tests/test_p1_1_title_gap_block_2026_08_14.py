# -*- coding: utf-8 -*-
"""P1-1 · 问题缺口驱动标题规划 · 判别锁(2026-08-14)。

变异点:
  M1 拆空块等价(空 gap_block 也拼一段占位)→ test_empty_block_prompt_byte_identical 红;
  M2 拆样本闸(不足也出块)→ test_insufficient_sample_returns_empty 红;
  M3 拆出口句(删「打不动退回选词」)→ test_gap_block_carries_fallback_outlet 红;
  M4 拆接线(KTG 不调 build_title_gap_block)→ test_ktg_wiring 红。
"""
from __future__ import annotations

import inspect

import services.reco_outcome_feedback as rof
from writing.keyword_topic_generator import _build_title_generator_prompt


def _agg(total: int, families: list[dict]) -> dict:
    return {
        "available": True, "version": rof.D6B_VERSION, "window_days": 90,
        "min_sample": 30, "total_observations": total, "families": families,
        "citations": [], "coverage_note": "口径:仅已配对子样本…",
    }


def test_empty_block_prompt_byte_identical() -> None:
    """A1 反向对照(工单分类纪律):缺数据 → prompt 零残留,与现行为逐字一致。

    判据自洽性:不能只比「空 == 空」(两侧走同一段坏代码时恒等 —— 第一版
    就这样被变异 M1 穿透)。改为「带标记 prompt 去掉插入段 == 空块 prompt」:
    任何空块残留都会让两边对不上。"""
    p_empty = _build_title_generator_prompt("家居", gap_block="")
    assert p_empty == _build_title_generator_prompt("家居")
    p_marked = _build_title_generator_prompt("家居", gap_block="XGAPMARKX")
    assert p_marked.count("XGAPMARKX") == 1
    assert p_marked.replace("\n\nXGAPMARKX", "") == p_empty, "空块在 prompt 里留了残渣"


def test_nonempty_block_lands_once() -> None:
    marker = "监测缺口参考·测试标记块"
    prompt = _build_title_generator_prompt("家居", gap_block=marker)
    assert prompt.count(marker) == 1


def test_insufficient_sample_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(5, [
                            {"question_family": "价格", "provider": "doubao",
                             "observations": 5, "recommended": 0,
                             "mentioned": 0, "not_mentioned": 5}]))
    assert rof.build_title_gap_block(662) == ""


def test_gap_block_carries_fallback_outlet(monkeypatch) -> None:
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(60, [
                            {"question_family": "价格对比", "provider": "doubao",
                             "observations": 40, "recommended": 0,
                             "mentioned": 5, "not_mentioned": 30},
                            {"question_family": "选购推荐", "provider": "kimi",
                             "observations": 20, "recommended": 15,
                             "mentioned": 3, "not_mentioned": 2}]))
    block = rof.build_title_gap_block(662)
    assert "价格对比" in block, "缺口族没进块"
    assert "选购推荐" in block, "优势族没进块"
    assert "退回按关键词本身选题" in block, "「打不动退回选词」出口被删(08-07 七步第 4 步)"
    assert "已配对子样本" in block, "红线桥限定语被剥离(工单红线 6)"


def test_tiny_family_not_concluded(monkeypatch) -> None:
    # 反向:单族样本 <5 不下结论(只有分子的比较是空的 —— 调研数字铁律)
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(60, [
                            {"question_family": "小样本族", "provider": "doubao",
                             "observations": 3, "recommended": 0,
                             "mentioned": 0, "not_mentioned": 3},
                            {"question_family": "大样本族", "provider": "doubao",
                             "observations": 57, "recommended": 0,
                             "mentioned": 7, "not_mentioned": 50}]))
    block = rof.build_title_gap_block(662)
    assert "小样本族" not in block
    assert "大样本族" in block


def test_ktg_wiring() -> None:
    """接线锁:标题生成主链真调 build_title_gap_block 并传进 prompt。"""
    import writing.keyword_topic_generator as ktg

    src = inspect.getsource(ktg)
    assert "build_title_gap_block" in src
    assert "gap_block=_gap_block" in src, "缺口块算了但没传进 _build_title_generator_prompt(死接线)"
