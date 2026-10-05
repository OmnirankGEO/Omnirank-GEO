# -*- coding: utf-8 -*-
"""D 安全批(audit 10×P1 · 2026-06-10):IDOR/越权/泄露契约锁。
#1 trend RBAC / #2 publications RBAC / #3 rollback×3+sync-trends 任务级归属 / #4 公开报告默认 strict /
#5 /api/s 客户面词级脱敏 / #6 schedule 全局写 admin-only / #7 distill require_diagnosis_access /
#8 deep-analyze 凑时长 360<480 / #9 clear-data/restore-data/archives 归属 / #10 reports generate 系列 RBAC"""
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


def _endpoint_block(src: str, decorator: str, span: int = 2200) -> str:
    i = src.find(decorator)
    assert i > 0, f"端点不存在: {decorator}"
    return src[i:i + span]


def test_1_trend_rbac():
    b = _endpoint_block(SERVER, '@app.get("/api/monitoring/trend")')
    assert "require_brand_access(request, brand_id)" in b
    assert "require_quote_access(request, _qid)" in b
    assert "非管理员必须指定" in b


def test_2_publications_rbac():
    b = _endpoint_block(SERVER, '@app.get("/api/publications/{quote_id}")')
    # [audit #2 返修 E4] 收紧为 allow_null=False(NULL-brand fail-closed)
    assert "require_quote_access(request, quote_id, allow_null=False)" in b
    assert "except HTTPException" in _endpoint_block(SERVER, '@app.get("/api/publications/{quote_id}")', 6000)


def test_3_rollback_family_access():
    assert "def _require_monitoring_task_access(" in SERVER
    helper = SERVER[
        SERVER.index("def _require_monitoring_task_access("):
        SERVER.index('@app.post("/api/monitoring/tasks/{task_id}/cells/{cell_id}/retry")')
    ]
    assert '_require_organization_artifact(request, "monitoring_task", task_id)' in helper
    for dec in ('@app.post("/api/monitoring/rollback/{task_id}")',
                '@app.post("/api/monitoring/sync-trends/{task_id}")'):
        assert "_require_monitoring_task_access(request, task_id)" in _endpoint_block(SERVER, dec)
    b = _endpoint_block(SERVER, '@app.post("/api/monitoring/rollback-batch")')
    assert "_require_monitoring_task_access(request, int(_tid))" in b
    b2 = _endpoint_block(SERVER, '@app.get("/api/monitoring/rollback/tasks")')
    assert "require_brand_access(request, brand_id, allow_null=False)" in b2
    assert "_organization_artifact_filled_page(" in b2
    assert '"monitoring_task"' in b2


def test_4_public_report_default_strict():
    src = (ROOT / "api" / "share_api.py").read_text(encoding="utf-8")
    assert 'PUBLIC_REPORT_REQUIRE_TOKEN", "true"' in src, "no-token 必须默认拒绝(防 251 份报告匿名枚举)"
    assert 'PUBLIC_REPORT_REQUIRE_TOKEN", "false"' not in src


def test_5_customer_strip_fields():
    src = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    assert "_CUSTOMER_INTERNAL_KW_FIELDS" in src
    # 三个核心机密字段必须在黑名单
    for f in ('"cost_per_article"', '"effective_competition"', '"geo_multiplier"', '"competition_ratio"'):
        assert f in src[src.find("_CUSTOMER_INTERNAL_KW_FIELDS"):src.find("def _strip_internal_pricing_fields")]
    # GET /s/{token} 两个吐数据分支都调脱敏(pricing+clusters 各一次 → 共 4 处)
    assert src.count("_strip_internal_pricing_fields(pricing_data)") == 2
    assert src.count("_strip_internal_pricing_fields(clusters_data)") == 2


def test_5_strip_function_value_level():
    """值级:从源码提取纯函数 exec(selection_api 模块 import 触发 DB 不可直接导入)。"""
    src = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    m = re.search(r"(_CUSTOMER_INTERNAL_KW_FIELDS = \([\s\S]*?\n\))", src)
    m2 = re.search(r"(def _strip_internal_pricing_fields[\s\S]*?)\n\ndef ", src)
    assert m and m2
    ns = {}
    exec(textwrap.dedent(m.group(1)) + "\n\n" + m2.group(1), ns)
    strip = ns["_strip_internal_pricing_fields"]
    pricing = {"tiers": {}, "keywords": [{"keyword": "测试词", "cost_per_article": 60,
                                          "effective_competition": 12, "entry": {"price": 400, "articles": 5},
                                          "super_red_ocean": False, "price_locked_until": "2026-06-17"}]}
    clusters = {"clusters": [{"core_keywords": [{"keyword": "a", "geo_multiplier": 0.45, "value_score": 1.3,
                                                 "standard": {"price": 800, "articles": 8}}],
                              "covered_keywords": [{"keyword": "b", "competition_ratio": 0.42, "upgradeable": True}]}]}
    strip(pricing)
    strip(clusters)
    kw = pricing["keywords"][0]
    assert "cost_per_article" not in kw and "effective_competition" not in kw
    assert kw["entry"]["price"] == 400 and kw["price_locked_until"] == "2026-06-17"  # 客户字段保留
    core = clusters["clusters"][0]["core_keywords"][0]
    cov = clusters["clusters"][0]["covered_keywords"][0]
    assert "geo_multiplier" not in core and "value_score" not in core and core["standard"]["price"] == 800
    assert "competition_ratio" not in cov and cov["upgradeable"] is True


def test_6_schedule_admin_only():
    assert "def _require_admin_for_global_monitoring(" in SERVER
    for dec in ('@app.post("/api/monitoring/schedule")',
                '@app.post("/api/monitoring/schedule/run-now")',
                '@app.post("/api/monitoring/schedule/stop")'):
        assert "_require_admin_for_global_monitoring(request)" in _endpoint_block(SERVER, dec)


def test_7_distill_access():
    b = _endpoint_block(SERVER, '@app.post("/api/distill/{diagnosis_id}")')
    # [audit #2 返修 E4] LLM 重写写端点收紧为 allow_null=False(NULL-brand fail-closed)
    assert "require_diagnosis_access(request, diagnosis_id, allow_null=False)" in b


def test_8_deep_analyze_padding_below_timeout():
    src = (ROOT / "api" / "content_api.py").read_text(encoding="utf-8")
    m = re.search(r"expected_min_seconds = 180 if is_reuse else (\d+)", src)
    t = re.search(r"_DEEP_ANALYZE_TOTAL_TIMEOUT = (\d+)", src)
    assert m and t
    assert int(m.group(1)) + 60 <= int(t.group(1)), "凑时长目标必须显著小于总超时(防成功后被误判超时退费)"


def test_9_clear_restore_archives_access():
    b = _endpoint_block(SERVER, '@app.post("/api/monitoring/clear-data")', 3000)
    assert "require_brand_access(request, int(brand_id))" in b
    assert "require_quote_access(request, int(quote_id))" in b
    b2 = _endpoint_block(SERVER, '@app.post("/api/monitoring/restore-data")', 3000)
    assert "require_brand_access" in b2 and "monitoring_data_archives" in b2
    b3 = _endpoint_block(SERVER, '@app.get("/api/monitoring/archives")')
    assert "require_brand_access(request, brand_id, allow_null=False)" in b3


def test_10_report_generate_family_rbac():
    assert "def _require_report_target_access(" in SERVER
    for dec in ('@app.post("/api/reports/generate")',
                '@app.post("/api/reports/generate/daily")',
                '@app.post("/api/reports/generate/weekly")',
                '@app.post("/api/reports/generate/monthly")',
                '@app.post("/api/reports/generate/quarterly")',
                '@app.post("/api/reports/generate/yearly")'):
        assert "_require_report_target_access(request, brand_id, client_id)" in _endpoint_block(SERVER, dec)
    b = _endpoint_block(SERVER, '@app.post("/api/reports/generate/daily/all")')
    assert "仅管理员可触发全客户报告生成" in b
