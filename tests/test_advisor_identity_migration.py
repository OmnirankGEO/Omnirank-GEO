from scripts.advisor_identity_migration import (
    replace_sources_in_value,
    scan_value_for_sources,
    validate_identity_mappings,
)


def test_scan_value_for_sources_unwraps_jsonb_arrays_and_objects():
    value = {
        "tags": ["舒老师方法", "品牌定位"],
        "quick_questions": ["我想问舒老师怎么做定位"],
        "nested": {"owner": "不相关"},
    }

    hits = scan_value_for_sources(value, ["舒老师"], path="advisor")

    assert [hit["path"] for hit in hits] == [
        "advisor.tags[0]",
        "advisor.quick_questions[0]",
    ]
    assert all(hit["source_name"] == "舒老师" for hit in hits)


def test_replace_sources_in_value_preserves_json_structure():
    value = {
        "tags": ["舒老师方法", "定位"],
        "meta": {"speaker": "舒老师", "score": 1},
    }

    replaced = replace_sources_in_value(value, {"舒老师": "品牌定位顾问小舒"})

    assert replaced == {
        "tags": ["品牌定位顾问小舒方法", "定位"],
        "meta": {"speaker": "品牌定位顾问小舒", "score": 1},
    }


def test_validate_identity_mappings_rejects_missing_names_and_unapproved_apply():
    mappings = [
        {"advisor_id": "ok", "source_name": "舒老师", "public_name": "品牌定位顾问小舒", "identity_status": "legal_approved"},
        {"advisor_id": "bad-source", "public_name": "豪车租赁专家小臻", "identity_status": "owner_approved"},
        {"advisor_id": "bad-status", "source_name": "亮哥", "public_name": "留学申请顾问小亮", "identity_status": "draft"},
    ]

    errors = validate_identity_mappings(mappings, require_approved=True)

    assert any("bad-source" in err and "source_name" in err for err in errors)
    assert any("bad-status" in err and "legal_approved" in err for err in errors)
    assert not any("ok" in err for err in errors)
