from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_operation_logs_request_carries_active_brand_id():
    source = read("frontend/src/pages/Monitoring/index.tsx")

    assert "params.set('brand_id'" in source or 'params.set("brand_id"' in source
    assert "operator_id=${currentBrandId}" not in source


def test_trend_chart_uses_clamped_bar_height():
    source = read("frontend/src/pages/Monitoring/components/TrendChartDialog.tsx")

    assert "barHeight(item.rate)" in source
    assert "rate * 2.5" not in source


def test_monitoring_dialog_tables_are_mobile_safe():
    results = read("frontend/src/pages/Monitoring/components/MonitoringResultsDialog.tsx")
    rollback = read("frontend/src/pages/Monitoring/components/RollbackDialog.tsx")
    archives = read("frontend/src/pages/Monitoring/components/ArchivesDialog.tsx")

    for source in (results, rollback, archives):
        assert "sticky top-0" in source
        assert "overflow-x-auto" in source
        assert "whitespace-nowrap" in source

    assert "min-w-[720px]" in results
    assert "min-w-[720px]" in rollback
    assert "min-w-[680px]" in archives


def test_portal_long_markdown_has_mobile_scroll_container():
    source = read("frontend/src/pages/Portal/PortalDashboard.tsx")

    assert "WebkitOverflowScrolling" in source
    assert "overscroll-contain" in source
    assert "max-h-[60vh]" in source
    assert "max-h-[70vh]" in source
