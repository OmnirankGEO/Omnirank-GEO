"""Regression guards for the recurring mobile ghost-container issue."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_sidebar_swipe_requires_real_touchmove_and_ignores_interactive_targets():
    src = (ROOT / "frontend/src/hooks/useSwipeGesture.ts").read_text(encoding="utf-8")

    assert "isInteractiveSwipeTarget" in src
    assert "observedMoveX" in src
    assert "observedMoveY" in src
    assert "observedMoveX < 40" in src or "observedMoveX <= 40" in src
    for selector in ["input", "button", "label", "[role=\"button\"]"]:
        assert selector in src


def test_refund_evidence_preview_object_urls_are_not_created_during_render():
    src = (ROOT / "frontend/src/pages/Admin/refunds/EvidenceUploader.tsx").read_text(encoding="utf-8")

    assert "previewUrl" in src
    assert "URL.revokeObjectURL" in src
    assert "src={URL.createObjectURL" not in src


def test_refund_console_does_not_create_nested_viewport_container():
    src = (ROOT / "frontend/src/pages/Admin/RefundConsole.tsx").read_text(encoding="utf-8")

    assert "min-h-screen" not in src
    assert "min-h-full" not in src


def test_refund_evidence_file_input_is_fully_hidden():
    src = (ROOT / "frontend/src/pages/Admin/refunds/EvidenceUploader.tsx").read_text(encoding="utf-8")

    assert 'type="file"' in src
    assert 'className="hidden"' in src
    assert 'className="sr-only"' not in src


def test_sidebar_shell_uses_dynamic_viewport_height():
    src = (ROOT / "frontend/src/components/ui/sidebar.tsx").read_text(encoding="utf-8")
    legacy_src = (ROOT / "frontend/src/components/layout/Sidebar.tsx").read_text(encoding="utf-8")

    assert 'className="flex h-screen w-full overflow-hidden"' not in src
    assert 'className="flex h-[100dvh] w-full overflow-hidden"' in src
    assert "{openMobile && (" in src
    assert "flex h-[100dvh] max-h-[100dvh] flex-col" in src
    assert '"fixed left-0 top-0 h-screen z-50"' not in legacy_src
    assert '"fixed left-0 top-0 h-[100dvh] z-50"' in legacy_src


def test_route_fallbacks_do_not_force_nested_screen_height():
    src = (ROOT / "frontend/src/components/auth/ProtectedRoute.tsx").read_text(encoding="utf-8")

    assert "h-screen" not in src
    assert "min-h-[calc(100dvh-3rem)]" in src


def test_collapsed_sidebar_groups_do_not_leak_document_height():
    src = (ROOT / "frontend/src/components/layout/AppSidebar.tsx").read_text(encoding="utf-8")
    layout_src = (ROOT / "frontend/src/components/layout/Layout.tsx").read_text(encoding="utf-8")
    sidebar_src = (ROOT / "frontend/src/components/ui/sidebar.tsx").read_text(encoding="utf-8")
    css = (ROOT / "frontend/src/index.css").read_text(encoding="utf-8")

    assert "grid overflow-hidden transition-[grid-template-rows]" in src
    assert "omnirank-shell-lock" in layout_src
    assert "flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto overflow-x-hidden py-1" in sidebar_src
    assert "contain: layout paint;" in css
    assert "html.omnirank-shell-lock" in css


def test_layout_nested_pages_do_not_use_top_level_screen_height():
    nested_page_paths = [
        "frontend/src/pages/Agent/LeadsPage.tsx",
        "frontend/src/pages/Brand/BrandDetailPage.tsx",
        "frontend/src/pages/Dashboard.tsx",
        "frontend/src/pages/Employees/MeetingRoom.tsx",
        "frontend/src/pages/Employees/MeetingHistory.tsx",
        "frontend/src/pages/Workspace/Workspace.tsx",
    ]

    offenders = [
        path for path in nested_page_paths
        if "min-h-screen" in (ROOT / path).read_text(encoding="utf-8")
    ]
    assert offenders == []
