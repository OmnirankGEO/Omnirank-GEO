from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "frontend" / "src" / "pages" / "MaterialConfirm" / "MaterialConfirmPage.tsx"
LIGHT_HOOK = ROOT / "frontend" / "src" / "pages" / "Selection" / "hooks" / "useForceLightMode.ts"


def test_material_confirm_page_has_wechat_safe_light_rendering_contract():
    source = PAGE.read_text(encoding="utf-8")
    hook = LIGHT_HOOK.read_text(encoding="utf-8")

    assert "bg-linear-to-" not in source
    assert "min-h-[100dvh]" not in source
    assert "bg-white/95 backdrop-blur" not in source
    assert "env(safe-area-inset-bottom" in source
    assert "colorScheme" in source
    assert 'name="color-scheme"' in hook
    assert "body.style.colorScheme" in hook


def test_material_confirm_page_shows_contact_and_image_confirmation_sections():
    source = PAGE.read_text(encoding="utf-8")

    assert "联系方式确认" in source
    assert "图片素材确认" in source
    assert "以下图片会作为推广素材候选" in source
    assert "m.contact" in source
    assert "m.images" in source


def test_material_confirm_page_supports_inline_edit_and_knowledge_sync_copy():
    source = PAGE.read_text(encoding="utf-8")

    assert "pendingEdits" in source
    assert "draftMaterials" in source
    assert "/materials" in source
    assert "修改此处" in source
    assert "保存本页修改" in source
    assert "同步更新客户档案" in source


def test_material_confirm_page_supports_image_title_inline_edit():
    source = PAGE.read_text(encoding="utf-8")

    assert "images.${i}.title" in source
    assert "图片 ${i + 1} 名称" in source
    assert "图片名称" in source
