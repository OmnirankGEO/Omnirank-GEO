from scripts.product.update_expert_market_identities import analyze_mapping


def test_analyze_mapping_does_not_block_extra_active_advisors():
    report = analyze_mapping([
        {"id": "zhen-ge-zu-che"},
        {"id": "liang-ge-liu-xue"},
        {"id": "prod-only-active-advisor"},
    ])

    assert report["errors"] == []
    assert "prod-only-active-advisor" in report["unmapped_active_expert_ids"]


def test_analyze_mapping_blocks_when_merge_target_missing():
    report = analyze_mapping([
        {"id": "liang-ge-liu-xue"},
    ])

    assert any("Merge target missing" in error for error in report["errors"])
