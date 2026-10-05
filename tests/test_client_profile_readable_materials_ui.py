from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRAND_DETAIL = ROOT / "frontend" / "src" / "pages" / "Brand" / "BrandDetailPage.tsx"
WRITING_HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
M3_MATERIAL_CONFIRM_API = ROOT / "api" / "m3_material_confirm_api.py"


def _brand_detail_source() -> str:
    return BRAND_DETAIL.read_text(encoding="utf-8")


def _writing_hall_source() -> str:
    return WRITING_HALL.read_text(encoding="utf-8")


def _m3_material_confirm_source() -> str:
    return M3_MATERIAL_CONFIRM_API.read_text(encoding="utf-8")


def test_brand_detail_renders_structured_marketing_fields_as_readable_text():
    source = _brand_detail_source()

    assert "function profileText" in source
    assert "function profileFieldWithFallback" in source
    assert "success_cases: profileFieldWithFallback(p?.success_cases, sk, ['success_cases', 'cases', 'case_studies'])" in source
    assert "testimonials: profileFieldWithFallback(p?.testimonials, sk, ['testimonials', 'reviews', 'customer_reviews'])" in source
    assert "success_cases: p?.success_cases || ''" not in source
    assert "testimonials: p?.testimonials || ''" not in source
    assert "[object Object]" not in source


def test_writing_hall_explains_existing_profile_and_optional_contact_fields():
    source = _writing_hall_source()

    assert "已有客户档案会自动参与整理" in source
    assert "基础资料表" in source
    assert "一句话业务描述" in source
    assert "目标客户" in source
    assert "产品/服务" in source
    assert "核心卖点" in source
    assert "案例/口碑" in source
    assert "禁用表达" in source
    assert "联系方式可选" not in source
    assert "可选联系方式" in source
    assert "没有联系方式也可以继续写文章" in source
    assert "整理/更新客户档案" in source


def test_writing_hall_clean_action_backfills_basic_material_form():
    source = _writing_hall_source()
    clean_section = source[
        source.index("const cleanKnowledgeMaterials"):
        source.index("const generateKnowledgeLink")
    ]

    assert "function mergeKnowledgeBasicsFromCleaned" in source
    assert "data?.cleaned_materials" in source
    assert "setKnowledgeBasics(nextBasics)" in source
    assert "setKnowledgeRawText(\"\")" in clean_section
    assert "setKnowledgeNotes(\"\")" in clean_section
    assert "resetKnowledgeDraft();" not in clean_section


def test_material_clean_response_exposes_structured_knowledge_for_basic_form():
    source = _m3_material_confirm_source()

    assert '"structured_knowledge": cleaned.get("structured_knowledge") or {}' in source
