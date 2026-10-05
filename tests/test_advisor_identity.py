from services.advisor_identity import (
    build_advisor_identity_boundary,
    project_advisor_row,
    public_name_from,
    sanitize_advisor_identity_claims,
)


def test_project_advisor_row_hides_source_name_for_public_view():
    row = {
        "id": "luxury-car-xz",
        "name": "豪车租赁专家小臻",
        "public_name": "豪车租赁专家小臻",
        "source_name": "骐哥教你做租车",
        "identity_status": "legal_approved",
        "identity_notes": "approved by owner",
    }

    projected = project_advisor_row(row, is_admin=False, public_identity_enabled=True)

    assert projected["name"] == "豪车租赁专家小臻"
    assert projected["public_name"] == "豪车租赁专家小臻"
    assert "source_name" not in projected
    assert "identity_notes" not in projected


def test_project_advisor_row_includes_source_name_for_admins():
    row = {
        "id": "luxury-car-xz",
        "name": "豪车租赁专家小臻",
        "public_name": "豪车租赁专家小臻",
        "source_name": "骐哥教你做租车",
        "identity_status": "owner_approved",
        "identity_notes": "needs legal review",
    }

    projected = project_advisor_row(row, is_admin=True, public_identity_enabled=True)

    assert projected["name"] == "豪车租赁专家小臻"
    assert projected["source_name"] == "骐哥教你做租车"
    assert projected["identity_status"] == "owner_approved"
    assert projected["identity_notes"] == "needs legal review"


def test_project_advisor_row_can_return_legacy_name_when_flag_is_disabled():
    row = {
        "id": "luxury-car-xz",
        "name": "豪车租赁专家小臻",
        "public_name": "豪车租赁专家小臻",
        "source_name": "骐哥教你做租车",
    }

    projected = project_advisor_row(row, is_admin=False, public_identity_enabled=False)

    assert projected["name"] == "骐哥教你做租车"
    assert "source_name" not in projected


def test_admin_projection_uses_public_name_even_when_flag_is_disabled():
    row = {
        "id": "luxury-car-xz",
        "name": "豪车租赁专家小臻",
        "public_name": "豪车租赁专家小臻",
        "source_name": "骐哥教你做租车",
    }

    projected = project_advisor_row(row, is_admin=True, public_identity_enabled=False)

    assert projected["name"] == "豪车租赁专家小臻"
    assert projected["source_name"] == "骐哥教你做租车"


def test_public_name_prefers_public_name_then_name():
    assert public_name_from({"public_name": "本地生活增长顾问阿南", "name": "南哥"}) == "本地生活增长顾问阿南"
    assert public_name_from({"name": "内容策略顾问"}) == "内容策略顾问"
    assert public_name_from({}) == "这位专家"


def test_identity_boundary_uses_public_identity_not_source_persona_claim():
    boundary = build_advisor_identity_boundary(
        "豪车租赁专家小臻",
        source_names=["骐哥教你做租车", "舒老师"],
    )

    assert "豪车租赁专家小臻" in boundary
    assert "骐哥教你做租车" not in boundary
    assert "舒老师" not in boundary
    assert "我是基于" not in boundary
    assert "资料和方法论的 AI 专家顾问" not in boundary


def test_sanitize_advisor_identity_claims_removes_source_names_and_legacy_intro():
    text = (
        "我是骐哥教你做租车，我不是AI。"
        "我是基于「舒老师」资料和方法论的 AI 专家顾问。"
        "接下来给你一个报价话术。"
    )

    sanitized = sanitize_advisor_identity_claims(
        text,
        public_name="豪车租赁专家小臻",
        source_names=["骐哥教你做租车", "舒老师"],
    )

    assert "骐哥教你做租车" not in sanitized
    assert "舒老师" not in sanitized
    assert "我不是AI" not in sanitized
    assert "基于「" not in sanitized
    assert sanitized.startswith("我是豪车租赁专家小臻")
    assert "报价话术" in sanitized
