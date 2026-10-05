from pathlib import Path


WRITING_HALL = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "pages"
    / "Writing"
    / "WritingHall.tsx"
)


def _source() -> str:
    return WRITING_HALL.read_text(encoding="utf-8")


def test_upload_starts_background_autofill_polling():
    source = _source()
    upload_section = source[source.index("const uploadKnowledgeFiles"):source.index("const handleKnowledgeFileSelect")]

    assert "startKnowledgeAutofillPolling" in source
    assert "startKnowledgeAutofillPolling(brandId" in upload_section
    assert "startKnowledgeAutofillPolling(uploadedBrandId" in upload_section


def test_background_autofill_copy_is_explicit_for_operator():
    source = _source()

    assert "自动回填上方基础资料表，并同步到客户档案页" in source
    assert "文件较大时可以先继续写作" in source


def test_material_clean_notifies_client_profile_page():
    source = _source()

    assert "brand:materials-cleaned" in source
    assert "emitBrandUpdated(brandId" in source

