from pathlib import Path


# [开源 E3 · B2 · 2026-09-28] 三个社媒评测脚本随社媒包删除,守它们的 6 格退役。


def test_geo_smoke_plan_is_five_case_and_live_requires_explicit_write_ack():
    from scripts.eval.geo_5_smoke_test import build_geo_smoke_plan, validate_geo_smoke_results

    plan = build_geo_smoke_plan()
    dry_results = [{"case_id": case["case_id"], "ok": True, "dry_run": True} for case in plan]

    assert [case["case_id"] for case in plan] == [
        "diagnosis_run",
        "quote_generate",
        "keyword_expand",
        "industry_research",
        "content_report",
    ]
    assert len(plan) == 5
    assert all(case["method"] == "POST" for case in plan)
    assert all(case["requires_explicit_live_ack"] is True for case in plan)
    assert validate_geo_smoke_results(dry_results)["all_ok"] is True


def test_step13_expected_doc_paths_are_stable():
    root = Path(__file__).resolve().parents[2]

    assert (root / "docs" / "AI-CONTEXT" / "social_audit_2026-05-15" / "03_STRESS_TEST_50.md").exists()
