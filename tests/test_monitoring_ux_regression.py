from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_monitoring_table_has_roomy_columns_and_batch_archive():
    text = _read("frontend/src/pages/Monitoring/components/KeywordTable.tsx")

    assert "onArchiveKeywords" in text
    assert "归档所选" in text
    assert "min-w-[1080px]" in text
    assert "window.location.reload()" not in text
    assert "kw.is_compliant && (" not in text


def test_archived_keywords_can_restore_without_renewing_service_period():
    archived = _read("frontend/src/pages/Monitoring/components/ArchivedKeywordsList.tsx")
    server = _read("server.py")

    assert "handleRestore" in archived
    assert "/restore" in archived
    assert "恢复" in archived
    assert "服务期将从今天重新计算" in archived

    assert '@app.post("/api/monitoring/keywords/{keyword_id}/restore")' in server
    assert "archive_reason = NULL" in server
    assert "archived_at = NULL" in server
    assert "service_start_date = CURRENT_DATE" not in server.split(
        '@app.post("/api/monitoring/keywords/{keyword_id}/restore")', 1
    )[1].split('@app.', 1)[0]


def test_bridge_status_uses_monitoring_truth_before_payment_cta():
    api = _read("frontend/src/services/m3/api.ts")
    bridge = _read("frontend/src/components/workbench/DecisionBarBridge.tsx")
    mapper = _read("frontend/src/services/m3/lifecycleMapper.ts")

    assert "loadMonitoringSummary" in api
    assert "`/api/monitoring/clients/${quoteId}/keywords`" in api
    assert "monitoringSummary: snapshot.monitoring?.summary ?? null" in bridge
    assert "if (monitoring?.running)" in mapper


def test_monthly_report_removed_from_sidebar_but_report_manager_remains():
    sidebar = _read("frontend/src/components/layout/AppSidebar.tsx")
    action_cards = _read("frontend/src/pages/Monitoring/components/ActionCards.tsx")

    assert "label: '月度报告'" not in sidebar
    assert "报告管理" in action_cards
    assert "navigate('/reports')" in action_cards


def test_monitoring_runs_enhanced_mode_without_mode_picker():
    page = _read("frontend/src/pages/Monitoring/index.tsx")
    api = _read("api/monitoring_api.py")
    scheduler = _read("api/scheduler.py")

    assert "searchMode" not in page
    assert "search_mode: 'enhanced'" in page
    assert "setSearchMode" not in page
    assert "{ value: 'standard'" not in page
    assert "{ value: 'auto'" not in page
    assert 'search_mode: str = "enhanced"' in api
    assert 'search_mode="enhanced"' in scheduler


def test_compliant_active_keywords_are_labeled_delivered_not_monitoring():
    table = _read("frontend/src/pages/Monitoring/components/KeywordTable.tsx")

    # 2026-05-10 老板报"单点 is_compliant=true 1 天 ≠ 服务交付完成":
    # is_compliant 单点判定显"达标中" · 真"已交付"留给 lifecycle='monitoring' AND
    # remaining_days<=0(服务期跑完)场景(KeywordTable.tsx:790-791 仍保留)
    # 老 assert 'kw.is_compliant ? "已交付" : "监测中"' 已不存在 · 更新测试断言
    # 2026-05-22 上线前最后一轮:同步测试断言 · 否则 1 failed 阻塞 release
    assert "达标中" in table  # 单点 is_compliant=true 文案
    assert "已交付" in table  # 服务期跑完 remaining_days<=0 文案(保留)
    assert 'kw.is_compliant ? "达标中" : "监测中"' in table


def test_schedule_dialog_exposes_client_interval_and_start_hour():
    dialog = _read("frontend/src/pages/Monitoring/components/ScheduleDialog.tsx")
    page = _read("frontend/src/pages/Monitoring/index.tsx")

    assert "监测节奏" in dialog
    assert "monitoringIntervalHours" in page
    assert "/api/monitoring/client/${selectedClient}/monitoring-config" in page
    assert "系统每小时巡检一次" in dialog
    assert "默认每天一次" in dialog


def test_schedule_save_accepts_backend_success_shapes_and_surfaces_errors():
    page = _read("frontend/src/pages/Monitoring/index.tsx")
    server = _read("server.py")

    assert "globalData.status === 'success' || globalData.success === true" in page
    assert "globalData.detail || globalData.message || globalData.error || '未知错误'" in page
    assert "clientData.status === 'success' || clientData.success === true" in page
    assert "clientData.detail || clientData.message || clientData.error || '未知错误'" in page

    schedule_endpoint = server.split('@app.post("/api/monitoring/schedule")', 1)[1].split("@app.", 1)[0]
    assert '"success": True' in schedule_endpoint
    assert '"success": False' in schedule_endpoint
