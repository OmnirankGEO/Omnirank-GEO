"""渠道有效性分级的锁(WO_DELIVERY_FLYWHEEL_CLOSURE §2.3)。

🔴 这套判定存在的理由,就是**防一个会误杀的直觉**:
   2026-08-06 生产实测 —— 我方在 cnblogs.com 发 45 条被引 0 条,直觉是"低权重位,降权";
   但 cnblogs.com 在 AI 引用池里是**第 3 大被引域**(710 篇 / 1067 次)。
   按域降权会砍掉一个 AI 确实在读的域,真正该降的是 mapp.to8to.com(发 13 条 / 池内 0 篇)。
   所以分级必须同时吃两个数,且**四个象限都要能出现** —— 否则就是个恒定结论的假分级。
"""
from __future__ import annotations

import pytest

from services.channel_effectiveness_report import (
    MIN_SAMPLE_FOR_VERDICT,
    POOL_PRESENT_MIN_ARTICLES,
    classify_channel,
)


def test_cnblogs_shaped_case_is_content_problem_not_channel():
    """我方 45 发 0 被引,但池内 710 篇 —— 必须判"改内容",不是"降权"。"""
    verdict = classify_channel(our_published=45, our_cited=0, pool_articles=710)
    assert verdict["verdict"] == "content_problem_not_channel"
    assert verdict["action"] == "别降权 · 改内容"


def test_to8to_shaped_case_is_channel_not_read():
    """我方 13 发 0 被引,池内 0 篇 —— 这个才是真该降权的。"""
    verdict = classify_channel(our_published=13, our_cited=0, pool_articles=0)
    assert verdict["verdict"] == "channel_not_read"
    assert verdict["action"] == "降权"


def test_proven_effective_case():
    verdict = classify_channel(our_published=10, our_cited=3, pool_articles=500)
    assert verdict["verdict"] == "proven_effective"
    assert verdict["action"] == "加码"


def test_effective_but_thin_pool_case():
    """被引到了但池子很薄 —— 别重仓(dongying.dzwww.com 形状:我方 4 发 1 被引 / 池内 7 篇)。"""
    verdict = classify_channel(our_published=6, our_cited=1, pool_articles=7)
    assert verdict["verdict"] == "effective_thin_pool"
    assert verdict["action"] == "小步加码"


def test_small_sample_refuses_to_conclude():
    """样本不足必须明说不下结论,而不是随便塞进某个象限。"""
    verdict = classify_channel(our_published=MIN_SAMPLE_FOR_VERDICT - 1, our_cited=0, pool_articles=0)
    assert verdict["verdict"] == "insufficient_sample"


def test_all_five_verdicts_are_reachable():
    """🔴 反向对照总闸:五种结论必须**都能出现**。

    如果哪天有人把分级改成恒定结论(比如一律 channel_not_read),这条会立刻红 ——
    这正是「恒绿的探针视同没有」在分级函数上的对应物。
    """
    seen = {
        classify_channel(our_published=p, our_cited=c, pool_articles=a)["verdict"]
        for p, c, a in (
            (2, 0, 0),                                  # insufficient_sample
            (45, 0, 710),                               # content_problem_not_channel
            (13, 0, 0),                                 # channel_not_read
            (10, 3, 500),                               # proven_effective
            (6, 1, 7),                                  # effective_thin_pool
        )
    }
    assert seen == {
        "insufficient_sample", "content_problem_not_channel", "channel_not_read",
        "proven_effective", "effective_thin_pool",
    }, seen


def test_pool_threshold_boundary_is_not_off_by_one():
    """阈值边界两侧必须给不同结论,证明阈值真的在起作用。"""
    below = classify_channel(
        our_published=20, our_cited=0, pool_articles=POOL_PRESENT_MIN_ARTICLES - 1)
    at = classify_channel(
        our_published=20, our_cited=0, pool_articles=POOL_PRESENT_MIN_ARTICLES)
    assert below["verdict"] == "channel_not_read"
    assert at["verdict"] == "content_problem_not_channel"


def test_report_declares_its_own_caveats():
    """口径局限必须跟着报表走 —— 全行业口径这件事不能只写在文档里。"""
    from services.channel_effectiveness_report import build_channel_report

    # 无 DB 环境下也应返回结构化结果并带 caveats(不许静默返回空 dict)
    report = build_channel_report(since_days=30)
    assert "caveats" in report and report["caveats"], report
    assert any("全行业" in c for c in report["caveats"]), "必须自曝 pool_articles 的口径局限"
