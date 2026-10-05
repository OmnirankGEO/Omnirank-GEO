"""锁:桥接 stage 级失败必须冒到顶层,不许只埋在 stages jsonb 里。

事故背景(2026-08-01):
    flywheel_bridge 的 source_signals stage 因 SQL 保留字别名每轮 100% 失败。
    但 _safe_stage 是 fail-soft:只把 {"status":"failed","error":...} 写进
    stages jsonb,run 级 error 保持 NULL、status 写 'partial'。
    /bridge/health 顶层只暴露 last_bridge_status='partial' 和 last_error=None
    → 面板看着"基本正常" → 信号断供 14 天无人察觉。

    「放松守卫那半边不能静默」(SSOT §1.9):fail-soft 允许流程继续,
    但**必须留下顶层可见的痕迹**。

本文件锁两件事:
    1. extract_failed_stages 能把嵌套的 failed stage 提出来(含真事故 payload);
    2. get_flywheel_bridge_health() 的返回里有 failed_stages / has_failed_stage /
       stuck_run_count 这几个顶层键 —— 契约锁,防止有人"简化"时把它们删掉。

每条「必须命中」都配了成对的「必须不命中」,防止断言退化成恒真。
"""
from __future__ import annotations

import json

from db.flywheel_bridge_db import extract_failed_stages, get_flywheel_bridge_health

# 生产 geo_flywheel_bridge_runs id=10(round_20260801_020003_602591)的真 stages 摘录
REAL_ACCIDENT_STAGES = {
    "media_entities": {
        "failed": 0, "loaded": 4817, "status": "success",
        "skipped": 0, "updated": 4817, "written": 4817, "inserted": 0, "industries": 11,
    },
    "source_signals": {
        "error": 'syntax error at or near "fetch"\nLINE 11:  '
                 "art.canonical_body_hash, fetch.id AS arti...\n",
        "status": "failed",
    },
    "answer_adoption": {
        "pages": 0, "failed": 0, "loaded": 0, "status": "success",
        "skipped": 0, "updated": 0, "written": 0, "inserted": 0,
    },
}


def test_extract_failed_stages_catches_the_real_accident():
    """必须命中:真事故 payload 里 source_signals 被提出来,且带上错误原文。"""
    failed = extract_failed_stages(REAL_ACCIDENT_STAGES)
    assert len(failed) == 1, f"应恰好提出 1 个失败 stage,实际 {failed}"
    assert failed[0]["stage"] == "source_signals"
    assert "syntax error" in failed[0]["error"]


def test_extract_failed_stages_quiet_when_all_good():
    """必须不命中:全 success 时返回空 —— 防止实现退化成"永远报警"。"""
    all_good = {
        k: {**v, "status": "success", "error": None}
        for k, v in REAL_ACCIDENT_STAGES.items()
    }
    assert extract_failed_stages(all_good) == []


def test_extract_failed_stages_accepts_json_string_and_junk():
    """jsonb 可能以 str 回来(驱动差异);垃圾输入不许炸。"""
    assert extract_failed_stages(json.dumps(REAL_ACCIDENT_STAGES))[0]["stage"] == "source_signals"
    # 必须不命中 + 不抛异常
    assert extract_failed_stages(None) == []
    assert extract_failed_stages("not json at all") == []
    assert extract_failed_stages([1, 2, 3]) == []
    assert extract_failed_stages({"s": "not a dict"}) == []


def test_skipped_is_not_treated_as_failed():
    """必须不命中:skipped 是正常态(如 flag 关着),不算失败,否则告警会被噪声淹没。"""
    assert extract_failed_stages({"answer_entities": {"status": "skipped"}}) == []


def test_health_surfaces_failed_stage_keys_at_top_level(db_with_clean_research):
    """契约锁:health 顶层必须有这些键,不许"简化"掉。

    这里断言的是**键存在**而不是值 —— 值取决于测试库里有没有跑过桥;
    键缺失才是回归(那意味着面板又看不到 stage 失败了)。
    """
    health = get_flywheel_bridge_health()
    for key in ("failed_stages", "has_failed_stage", "failed_stage_message",
                "stuck_run_count", "stuck_run_threshold_hours", "stuck_run_message"):
        assert key in health, f"/bridge/health 顶层缺 `{key}` —— stage 失败又要静默了"
    assert isinstance(health["failed_stages"], list)
    assert isinstance(health["has_failed_stage"], bool)
    assert isinstance(health["stuck_run_count"], int)
    # 反向对照:确认这个断言不是恒真(health 本身确实返回了有内容的 dict)
    assert health.get("stale_threshold_hours") == 24.0
