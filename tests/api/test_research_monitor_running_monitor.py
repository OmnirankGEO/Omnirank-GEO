"""
P14 · 运行监控可信度修复 (C8 运行监控)

覆盖:
  - /rounds/{round_id}/live-status endpoint 注册 + admin-only + first_seen_round_id
  - 前端 RunningMonitorView 组件存在 + RoundsPanel 二级 tab 接入
  - stage_3 progress 字段兼容 (total_urls / total · crawled+skipped)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_rounds_live_status_endpoint_registered():
    """/rounds/{round_id}/live-status endpoint 必须存在 + 必须在 {round_id} 之前注册"""
    src = (ROOT / "api" / "research_monitor_round_api.py").read_text(encoding="utf-8")
    assert '/rounds/{round_id}/live-status' in src
    assert "def get_round_live_status" in src
    assert "_require_admin(request)" in src

    # 必须聚合 round_call 表 (per-platform)
    assert "FROM geo_research_round_call" in src
    assert "platform" in src.lower()

    # articles 字段必须是 first_seen_round_id (不是 round_id)
    import re
    m = re.search(r'def get_round_live_status\(.*?\n(.*?)(?=\nasync def |\ndef )', src, re.DOTALL)
    assert m and 'first_seen_round_id' in m.group(1), \
        "live-status 查 articles 必须用 first_seen_round_id 字段 (articles 表没 round_id)"


def test_running_monitor_view_component_exists():
    """前端 RunningMonitorView 组件 + RoundsPanel 二级 tab 接入"""
    rm = ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor"
    assert (rm / "RunningMonitorView.tsx").exists(), \
        "必须有 RunningMonitorView 组件"
    rp = (rm / "RoundsPanel.tsx").read_text(encoding="utf-8")
    assert "RunningMonitorView" in rp, "RoundsPanel 必须 import RunningMonitorView"
    assert "subTab" in rp or "运行监控" in rp, "RoundsPanel 必须有二级 tab"
    # stage_3 progress 字段兼容 (total/total_urls + crawled+skipped)
    assert "pj.total_urls ?? pj.total" in rp or "pj.total_urls != null || pj.total != null" in rp


def test_live_status_returns_stage_3_summary_with_fallback():
    """P14.1 C3+ · live-status 必须返 stage_3_summary 字段 ·
    running 时优先 progress_json · completed/failed/cancelled 时 fallback summary_json.stage_3
    (老板复核 round_20260528_232257 暴露 stage 8 覆盖 progress_json 后 reasons 丢失)
    """
    src = (ROOT / "api" / "research_monitor_round_api.py").read_text(encoding="utf-8")
    # 1. endpoint 必须返 stage_3_summary 字段
    assert "'stage_3_summary': stage_3_summary" in src or \
           '"stage_3_summary": stage_3_summary' in src, \
        "live-status 必须返 stage_3_summary 顶层字段"

    # 2. SELECT 必须拉 summary_json (拿 stage_3 子字段)
    assert "summary_json AS summary" in src or "summary_json" in src, \
        "live-status 必须 SELECT summary_json 才能 fallback"
    assert "stage_3_from_summary" in src, \
        "endpoint 必须解析 summary_json.stage_3 备用"

    # 3. fallback 逻辑: 完成态优先 summary · running 时优先 progress
    assert "_is_terminal" in src, \
        "endpoint 必须区分 running / 完成态 (_is_terminal)"
    # terminal 集合必含 completed/failed/cancelled
    import re
    m = re.search(r"_is_terminal\s*=.*?\{([^}]+)\}", src, re.DOTALL)
    assert m, "找不到 _is_terminal 定义"
    terminal_set = m.group(1)
    for st in ['completed', 'failed', 'cancelled']:
        assert f"'{st}'" in terminal_set, \
            f"_is_terminal 集合必须含 '{st}'"


def test_live_status_stage_3_summary_includes_error_samples():
    """stage_3_summary 返回结构必须含 error_samples (前 5 条 Jina/OSS/unknown 失败)"""
    src = (ROOT / "api" / "research_monitor_round_api.py").read_text(encoding="utf-8")
    assert "'error_samples'" in src or '"error_samples"' in src, \
        "stage_3_summary 必须含 error_samples 字段"


def test_round_runner_stage3_collects_error_samples_with_classification():
    """round_runner crawl_one 必须在 jina_failed/oss_failed/failed_unknown 路径
    调 _record_error_sample · 且有 _classify_error 类型分类
    """
    src = (ROOT / "services" / "research_monitor" / "round_runner.py").read_text(encoding="utf-8")
    # 必须有 _record_error_sample helper
    assert "_record_error_sample" in src, \
        "round_runner stage 3 必须有 _record_error_sample helper"
    # 必须分类 timeout / connect / rate_limit / auth / server_5xx / other
    assert "_classify_error" in src, \
        "必须有 _classify_error 分类器 · 让前端 chip 显 'timeout/connect/...' 而非长 stack"
    for et in ['timeout', 'connect', 'rate_limit', 'auth', 'server_5xx']:
        assert f"'{et}'" in src, f"_classify_error 必须能返 '{et}' 类型"
    # error_samples 必须限上限 (防大轮次打爆 summary)
    assert "ERROR_SAMPLE_LIMIT" in src, \
        "必须有 ERROR_SAMPLE_LIMIT 防 error_samples 无限增长"
    # summary_json.stage_3 mirror 必须含 error_samples
    assert "'error_samples': list(error_samples)" in src, \
        "stage 3 终点镜像 summary_json.stage_3 必须含 error_samples"


def test_stage3_summary_view_component_exists():
    """P14.3 C1 · Stage3SummaryView 组件存在 + 双 export (chips + card)"""
    f = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor"
         / "components" / "Stage3SummaryView.tsx")
    assert f.exists(), "Stage3SummaryView 组件文件必须存在"
    src = f.read_text(encoding="utf-8")
    assert "export function Stage3SummaryChips" in src, "必须导出 Stage3SummaryChips (列表行紧凑 chip)"
    assert "export function Stage3SummaryCard" in src, "必须导出 Stage3SummaryCard (详情完整 grid)"
    assert "export function extractStage3FromSummary" in src, \
        "必须导出 extractStage3FromSummary helper"
    # 7 类 reasons 全在
    for k in ['crawled_new', 'crawled_dup', 'reused_existing',
              'skipped_short', 'skipped_jina_failed', 'skipped_oss_failed', 'failed_unknown']:
        assert f"'{k}'" in src, f"REASON_DEFS 必须含 '{k}'"
    # error_samples 区块
    assert "error_samples" in src, "Stage3SummaryCard 必须渲染 error_samples"


def test_rounds_panel_uses_stage3_summary_view():
    """P14.3 C1 · RoundsPanel 历史行 + 详情 dialog 接入 Stage3SummaryView"""
    f = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor" / "RoundsPanel.tsx")
    src = f.read_text(encoding="utf-8")
    # 1. import
    assert "Stage3SummaryChips" in src and "Stage3SummaryCard" in src, \
        "RoundsPanel 必须 import 两个组件"
    assert "extractStage3FromSummary" in src, "必须 import helper"
    # 2. 列表行接入 chips (r.summary_json)
    assert "<Stage3SummaryChips" in src, "列表行必须用 Stage3SummaryChips 标签"
    # 3. 详情 dialog 接入 card (detailData.summary_json)
    assert "<Stage3SummaryCard" in src, "详情 dialog 必须用 Stage3SummaryCard 标签"


def test_round_status_badge_supports_partial_failure_downgrade():
    """P14.3 C2 · RoundStatusBadge 支持 hasStageFailures prop · completed + 失败时降级 amber"""
    f = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor"
         / "components" / "RoundStatusBadge.tsx")
    src = f.read_text(encoding="utf-8")
    assert "hasStageFailures?: boolean" in src, "Props 必须新增 hasStageFailures (可选 boolean)"
    # 必须有 amber/"部分抓取失败" 文案 (老板口径)
    assert "完成 · 部分抓取失败" in src or "部分抓取失败" in src, \
        "completed + 失败时必须有 部分抓取失败 文案"
    assert "bg-amber-100 text-amber-700" in src, \
        "降级必须用 amber 色 (跟 partial_success 同色 · 文案区分)"
    # 必须 status='completed' && hasStageFailures 才降级 · 其他 status 走原 STATUS_CONFIG
    assert "status === 'completed' && hasStageFailures" in src or \
           "status === 'completed' &&" in src, \
        "只在 completed + hasStageFailures 时降级"


def test_stage3_summary_view_exports_has_jina_failures_helper():
    """P14.3 C2 · stage3HasJinaFailures helper 必须导出 · 供 3 处 callsite 复用"""
    f = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor"
         / "components" / "Stage3SummaryView.tsx")
    src = f.read_text(encoding="utf-8")
    assert "export function stage3HasJinaFailures" in src, \
        "必须导出 stage3HasJinaFailures helper"
    assert "skipped_jina_failed" in src, "helper 必须基于 skipped_jina_failed 字段判定"


def test_rounds_panel_passes_has_failures_to_badge():
    """P14.3 C2 · RoundsPanel 列表行 + 详情 dialog · RoundStatusBadge 必须传 hasStageFailures"""
    src = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor"
           / "RoundsPanel.tsx").read_text(encoding="utf-8")
    # 必须 import helper
    assert "stage3HasJinaFailures" in src, "RoundsPanel 必须 import stage3HasJinaFailures"
    # 必须传 hasStageFailures prop 到 RoundStatusBadge (至少 2 次 · 列表行 + 详情 dialog)
    assert src.count("hasStageFailures=") >= 2, \
        f"RoundsPanel 必须至少 2 处传 hasStageFailures (列表 + 详情) · 实际 {src.count('hasStageFailures=')}"


def test_running_monitor_view_uses_unified_badge():
    """P14.3 C2 · RunningMonitorView 必须用 RoundStatusBadge (跟历史列表口径一致)
    不能保留旧 inline 三元字符串 (老板口径: live-status / 历史列表口径一致)
    """
    src = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor"
           / "RunningMonitorView.tsx").read_text(encoding="utf-8")
    assert "RoundStatusBadge" in src, "RunningMonitorView 必须 import + 用 RoundStatusBadge"
    assert "stage3HasJinaFailures" in src, "必须 import stage3HasJinaFailures"
    # 必须传 hasStageFailures
    assert "hasStageFailures=" in src, "<RoundStatusBadge .../> 必须传 hasStageFailures"
    # 旧 inline 三元 (round.status === 'running' ? '运行中' ...) 必须删
    assert "round.status === 'running' ? '运行中'" not in src, \
        "旧 inline 状态文案三元必须删 · 统一用 RoundStatusBadge"


def test_sweep_mark_zombie_update_covers_pending():
    """P14 post-review fix (老板复核): sweep 不能只在查询时扩 pending · UPDATE 也得扩

    之前 mark_zombie_failed_resumable 的 WHERE 只匹配 status='running' ·
    跟 find_zombie_rounds 已扩到 IN ('running', 'pending') 不一致 ·
    pending 僵尸被检测出来但 UPDATE 0 行 · 卡 pending 的坑没真修上.

    锁: UPDATE SQL 必须 WHERE status IN ('running', 'pending')
    """
    src = (ROOT / "services" / "research_monitor" / "restart_recovery.py").read_text(encoding="utf-8")
    import re
    m = re.search(
        r'def mark_zombie_failed_resumable\(.*?\n(.*?)(?=\ndef |\Z)',
        src, re.DOTALL,
    )
    assert m, "找不到 mark_zombie_failed_resumable 函数"
    body = m.group(1)
    # 必须含 UPDATE geo_research_round
    assert "UPDATE geo_research_round" in body, "UPDATE SQL 缺失"
    # 关键: WHERE 子句必须 IN ('running', 'pending') · 不能是单 status='running'
    assert "status IN ('running', 'pending')" in body, \
        ("UPDATE WHERE 必须 status IN ('running', 'pending') · "
         "跟 find_zombie_rounds 对齐 · 否则 pending 僵尸检测出来但 UPDATE 0 行")
    # 不能再有旧 status = 'running' 单条件 (兜底防回归)
    assert "AND status = 'running'\n" not in body, \
        "旧 status='running' 单条件 WHERE 必须删 · 否则 pending 僵尸还是被漏"


def test_live_status_returns_intent_summary_for_stage45():
    """P15 · 运行监控必须展示文章意图分类进度。"""
    src = (ROOT / "api" / "research_monitor_round_api.py").read_text(encoding="utf-8")
    assert "intent_summary" in src, "live-status 必须返回 intent_summary 顶层字段"
    assert "stage_4_5_from_summary" in src, "完成态必须从 summary_json.stage_4_5 兜底"
    assert "intent_classified" in src, "必须从 articles 表统计已分类文章数"
    assert "stage_4_5" in src and "文章意图分类" in src, "阶段进度必须含文章意图分类行"


def test_running_monitor_view_renders_intent_summary_card():
    """P15 · RunningMonitorView 必须渲染文章意图分类卡片。"""
    src = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor" / "RunningMonitorView.tsx").read_text(encoding="utf-8")
    assert "intent_summary" in src, "RunningMonitorView 必须读取 live-status.intent_summary"
    assert "文章意图分类" in src, "前端必须有文章意图分类展示文案"
    for key in ["classified", "failed", "total", "model"]:
        assert key in src, f"文章意图分类卡片必须展示 {key}"


def test_research_monitor_api_types_include_intent_summary():
    """P15 · 前端 live-status 类型必须包含 intent_summary。"""
    src = (ROOT / "frontend" / "src" / "lib" / "researchMonitorApi.ts").read_text(encoding="utf-8")
    assert "LiveStatusIntentSummary" in src, "必须定义 LiveStatusIntentSummary 类型"
    assert "intent_summary: LiveStatusIntentSummary | null" in src, "LiveStatus 必须包含 intent_summary"


def test_stage45_is_first_class_resume_stage():
    """P15 收口 · stage_4_5 必须进入后端续跑阶段白名单。"""
    round_api = (ROOT / "api" / "research_monitor_round_api.py").read_text(encoding="utf-8")
    runner = (ROOT / "services" / "research_monitor" / "round_runner.py").read_text(encoding="utf-8")
    assert "'stage_4_5'" in round_api, "resume from_stage 必须允许 stage_4_5"
    assert "'stage_4_5'" in runner, "runner _should_skip 必须理解 stage_4_5"
    assert "if not _should_skip(resume_from_stage, 'stage_4_5')" in runner, \
        "stage 4.5 不能复用 stage_4 的跳过条件,否则断点续跑不精准"


def test_intent_classification_cost_is_logged_to_round_budget():
    """P15 收口 · DeepSeek 分类成本必须进入 geo_research_cost_log。"""
    runner = (ROOT / "services" / "research_monitor" / "round_runner.py").read_text(encoding="utf-8")
    assert "INTENT_CLASSIFY_COST_PER_ARTICLE" in runner, "必须显式声明分类成本单价"
    assert "estimated_increment_yuan=total * INTENT_CLASSIFY_COST_PER_ARTICLE" in runner, \
        "分类开始前必须把预计成本纳入预算检查"
    assert "_log_cost(round_id, 'intent_classify'" in runner, "分类完成后必须写入 cost_log"


def test_intent_classification_summary_keeps_error_samples():
    """P15 收口 · 分类失败不能只给一个数字,必须保留失败样本方便排查。"""
    runner = (ROOT / "services" / "research_monitor" / "round_runner.py").read_text(encoding="utf-8")
    api = (ROOT / "api" / "research_monitor_round_api.py").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor" / "RunningMonitorView.tsx").read_text(encoding="utf-8")
    assert "intent_error_samples" in runner, "runner 必须收集分类失败样本"
    assert "error_samples" in api, "live-status 必须把分类失败样本透出"
    assert "失败样本" in frontend, "运行监控必须展示分类失败样本"


def test_articles_panel_uses_one_column_intent_distribution():
    """P15 收口 · 文章意图分布要一眼看懂,用单列 8 行横条,不做多列小块。"""
    src = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor" / "ArticlesPanel.tsx").read_text(encoding="utf-8")
    assert "data-intent-distribution-list" in src, "意图分布列表必须有稳定标识"
    assert "grid-cols-1 md:grid-cols-2" not in src, "不要用两列网格展示意图占比,扫读成本高"
    assert "占比最高" in src, "顶部必须直接告诉管理员当前主导文章类型"



def test_completed_status_downgrades_for_intent_classification_failures():
    """completed ???????????????????????"""
    stage3 = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor" / "components" / "Stage3SummaryView.tsx").read_text(encoding="utf-8")
    rounds = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor" / "RoundsPanel.tsx").read_text(encoding="utf-8")
    running = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor" / "RunningMonitorView.tsx").read_text(encoding="utf-8")
    badge = (ROOT / "frontend" / "src" / "pages" / "Admin" / "ResearchMonitor" / "components" / "RoundStatusBadge.tsx").read_text(encoding="utf-8")
    assert "summaryHasIntentFailures" in stage3
    assert "summaryHasIntentFailures" in rounds
    assert "intent_summary?.failed" in running or "intent_summary.failed" in running
    assert "COMPLETED_PARTIAL_CONFIG" in badge
    assert "hasStageFailures" in badge
