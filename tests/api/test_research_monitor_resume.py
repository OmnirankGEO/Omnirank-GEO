"""
P14-v8/v9 · round list 行业字段 + 续跑断点续 (C3 round list + 续跑)

覆盖:
  - _infer_resume_from_stage 断点续跑逻辑 (_done → 下一个 / 中断 → 重跑该 stage)
  - cancelled round 可续 (API RESUMABLE_STATUSES + round_state SQL + RoundsPanel UI)
  - resume endpoint 用 _infer_resume_from_stage 而非硬编码 stage_1
  - RoundsPanel 禁勾无 active prompts 的行业 (active_prompt_count UI 消费)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_infer_resume_from_stage_breakpoint_logic():
    """P14-v9 · resume 必须断点续 · 不能每次从 stage_1 重跑"""
    from api.research_monitor_round_api import _infer_resume_from_stage, _STAGE_ORDER
    # 完成态 → 跳下一个
    assert _infer_resume_from_stage('stage_1_ai_fetch_done') == 'stage_2'
    assert _infer_resume_from_stage('stage_2_prefilter_done') == 'stage_3'
    assert _infer_resume_from_stage('stage_3_crawl_done') == 'stage_4'
    assert _infer_resume_from_stage('stage_4_clean_done') == 'stage_5'
    assert _infer_resume_from_stage('stage_5_filter_done') == 'stage_6'
    assert _infer_resume_from_stage('stage_7_aggregate_done') == 'stage_8'
    # 中断/失败态 → 重跑该 stage
    assert _infer_resume_from_stage('stage_1_ai_fetch') == 'stage_1'
    assert _infer_resume_from_stage('stage_3_crawl') == 'stage_3'
    assert _infer_resume_from_stage('stage_7_aggregate_failed') == 'stage_7'
    # 不识别 / fallback
    assert _infer_resume_from_stage(None) == 'stage_1'
    assert _infer_resume_from_stage('legacy_imported') == 'stage_1'
    # 已到最后 · 兜底 stage_8 (不会越界)
    assert _infer_resume_from_stage('stage_8_notify_done') == 'stage_8'
    # STAGE_ORDER 完整性
    # P15 (2026-06-01): 加 stage_4_5 (文章意图分类) · 排在 4 和 5 之间
    assert _STAGE_ORDER == ['stage_1', 'stage_2', 'stage_3', 'stage_4', 'stage_4_5',
                            'stage_5', 'stage_6', 'stage_7', 'stage_8']


def test_cancelled_round_is_resumable():
    """P14-v9 · cancelled round 也必须可续跑 (老板反馈: cancel 通常因环境问题 · 数据没污染)"""
    # 后端
    src_api = (ROOT / "api" / "research_monitor_round_api.py").read_text(encoding="utf-8")
    assert "RESUMABLE_STATUSES" in src_api
    import re
    m = re.search(r'RESUMABLE_STATUSES\s*=\s*\{([^}]+)\}', src_api)
    assert m and 'cancelled' in m.group(1), \
        "RESUMABLE_STATUSES 必须含 'cancelled'"

    # 前端
    src_fe = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor"
              / "RoundsPanel.tsx").read_text(encoding="utf-8")
    assert "'cancelled'" in src_fe or '"cancelled"' in src_fe
    m_fe = re.search(r"const RESUMABLE = new Set\(\[([^\]]+)\]\)", src_fe)
    assert m_fe and 'cancelled' in m_fe.group(1), \
        "前端 RESUMABLE Set 必须含 'cancelled'"

    # round_state.py 内 mark 抢占窗口
    # [2026-07-16 提并发+自动续跑批] SQL 从字面 IN ('failed_resumable','cancelled')
    # 参数化为 IN %s + allowed_statuses 形参; P14-v9 语义(cancelled 可人工续跑)
    # 由【默认值】承载不变, 静态锁随语义载体迁移到签名默认值;
    # 自动续跑路径显式收窄 ('failed_resumable',)(防复活 admin cancel, 出口审核修)。
    # 行为级双验证: tests/research_monitor/test_round_concurrency_autoresume.py
    # ::TestCancelNotResurrected::test_mark_allowed_statuses_where_layer_guard
    # (cancelled 轮: 人工默认窗口 claimed=True / 自动窗口 claimed=False)。
    src_st = (ROOT / "services" / "research_monitor" / "round_state.py").read_text(encoding="utf-8")
    assert "allowed_statuses: tuple = ('failed_resumable', 'cancelled')" in src_st, \
        "mark_round_resume_requested 默认抢占窗口必须含 failed_resumable + cancelled(P14-v9)"
    assert "AND status IN %s" in src_st, \
        "mark SQL 必须按 allowed_statuses 参数化生效(WHERE 层原子窗口)"


def test_resume_endpoint_uses_breakpoint_inference():
    """锁 resume endpoint 必须用 _infer_resume_from_stage · 不能再硬编码 stage_1"""
    src = (ROOT / "api" / "research_monitor_round_api.py").read_text(encoding="utf-8")
    import re
    m = re.search(
        r'async def resume_round\(.*?\n(.*?)(?=\nasync def |\ndef |\n@router\.)',
        src, re.DOTALL,
    )
    assert m, "找不到 resume_round endpoint"
    body = m.group(1)
    assert "_infer_resume_from_stage" in body, \
        "resume_round 必须调 _infer_resume_from_stage 推断起点 · 不能硬编码 stage_1"
    assert 'resume_from_stage = "stage_1"' not in body, \
        "旧硬编码 resume_from_stage = \"stage_1\" 必须删 · 改用 _infer_resume_from_stage"


def test_rounds_panel_disables_industry_without_prompts():
    """RoundsPanel 手动跑批 dialog 必须禁用无 active prompts 的行业"""
    src = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor"
           / "RoundsPanel.tsx").read_text(encoding="utf-8")
    assert "active_prompt_count" in src, \
        "RoundsPanel 必须读 active_prompt_count"
    assert "disabled={disabled}" in src, "勾选框必须 disabled"
    assert "无 active Prompts" in src or "无 Prompts" in src, \
        "必须有无 prompts 的视觉/提示"
