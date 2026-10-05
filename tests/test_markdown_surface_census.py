"""Machine gate preventing customer Markdown from bypassing the renderer SSOT.

口径：docs/AI-CONTEXT/MARKDOWN_SINK_CENSUS_2026-07-22（sink-based census ·
Review-CTO §13.3）。与 frontend/scripts/verify-markdown-ssot.mjs 互为镜像。

延期集成名单（deferred）：机制保留；2026-07-22 集成收尾后板块 A 两诊断页 +
板块 C MeetingRoom 已走 SSOT 并移入 CANONICAL，名单清空（防漂移断言持续生效）。
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"

# census §2 判定为 Markdown 且必须走 SSOT 的关键出口
CANONICAL_MARKDOWN_EXITS = {
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] M3 报告页、旧社媒操盘手两页、社媒工作台附件栏随宿主整删
    "pages/Public/SharedReport.tsx",
    "pages/Portal/PortalDashboard.tsx",
    "pages/Reports/index.tsx",
    "pages/Monitoring/components/IdentityReviewPanel.tsx",
    "pages/Monitoring/components/ProgressPanel.tsx",
    "features/publicReportPremium/components/markdown/MarkdownView.tsx",
    "pages/Writing/WritingHall.tsx",
    "pages/Writing/WritingCenter.tsx",
    "pages/Writing/ReferenceLibrary.tsx",
    "pages/Writing/ImitatedArticleDetail.tsx",
    "pages/Admin/ResearchMonitor/ArticlesPanel.tsx",
    "pages/Admin/ResearchMonitor/CitationsPanel.tsx",
    "pages/Admin/MarketingAdvisor/components/Effect.tsx",
    "pages/Admin/HelpCenterAdmin/FAQItemEditPage.tsx",
    "pages/Admin/HelpCenterAdmin/FAQFeedbackTab.tsx",
    "pages/Admin/AiOpsCenter/components/shared.tsx",
    "pages/Feedback/FeedbackPage.tsx",
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] 顾问管理页随顾问团前端整删
    # [2026-07-22 集成收尾] 原 deferred 三文件已走 SSOT（板块A 两诊断页 + 板块C MeetingRoom）
    "pages/Diagnosis/DiagnosisReport.tsx",
    "pages/Diagnosis/ReportV2View.tsx",
    "pages/Employees/MeetingRoom.tsx",
}

# 延期集成名单：owner 板块收尾后必须移除（2026-07-22 集成收尾后已清空，保留机制供未来延期使用）
DEFERRED_REACT_MARKDOWN_SURFACES: set = set()

TRUSTED_INNER_HTML_BOUNDARIES = {
    # 后端报告整档（markdown 片段已过 services.safe_markdown_renderer）
    "pages/Public/SharedReport.tsx",
    # escapeHtml 后受控 <mark> 高亮 backdrop
    "components/publishing/AwaitingConfirmDialog.tsx",
    # 同源认证接口 CAPTCHA SVG；非 Markdown
    "pages/Login/LoginPage.tsx",
}


def _tsx_files():
    return list(FRONTEND.rglob("*.tsx"))


def test_only_safe_markdown_may_import_react_markdown_runtime():
    offenders = []
    for path in _tsx_files():
        text = path.read_text(encoding="utf-8")
        if re.search(r"from\s+['\"]react-markdown['\"]", text):
            rel = path.relative_to(FRONTEND).as_posix()
            if rel != "components/SafeMarkdown.tsx" and rel not in DEFERRED_REACT_MARKDOWN_SURFACES:
                offenders.append(rel)
    assert offenders == []


def test_no_surface_imports_gfm_outside_ssot():
    # remark-gfm 只允许 SSOT 集中引用（patches 已去 lookbehind）+ deferred 延期名单
    offenders = []
    for path in _tsx_files():
        text = path.read_text(encoding="utf-8")
        if re.search(r"from\s+['\"]remark-gfm['\"]", text):
            rel = path.relative_to(FRONTEND).as_posix()
            if rel != "components/SafeMarkdown.tsx" and rel not in DEFERRED_REACT_MARKDOWN_SURFACES:
                offenders.append(rel)
    assert offenders == []


def test_deferred_surfaces_still_exist_and_still_direct():
    # 防漂移：deferred 文件若已被对应板块改走 SSOT，必须同步移出名单
    stale = []
    for rel in sorted(DEFERRED_REACT_MARKDOWN_SURFACES):
        text = (FRONTEND / rel).read_text(encoding="utf-8")
        if not re.search(r"from\s+['\"]react-markdown['\"]", text):
            stale.append(rel)
    assert stale == []


def test_known_customer_markdown_exits_use_ssot():
    missing = []
    for rel in sorted(CANONICAL_MARKDOWN_EXITS):
        text = (FRONTEND / rel).read_text(encoding="utf-8")
        if "@/components/SafeMarkdown" not in text:
            missing.append(rel)
    assert missing == []


def test_dangerous_inner_html_is_small_and_documented():
    actual = set()
    for path in _tsx_files():
        if re.search(r"dangerouslySetInnerHTML\s*=", path.read_text(encoding="utf-8")):
            actual.add(path.relative_to(FRONTEND).as_posix())
    assert actual == TRUSTED_INNER_HTML_BOUNDARIES


def test_python_v2_and_v3_delegate_to_server_ssot():
    v2 = (ROOT / "services" / "report_html_renderer.py").read_text(encoding="utf-8")
    v3 = (ROOT / "services" / "report_html_renderer_v3.py").read_text(encoding="utf-8")

    assert "from services.safe_markdown_renderer import" in v2
    assert "return render_safe_markdown(text)" in v2
    assert "from services.safe_markdown_renderer import" in v3
    assert "render_safe_markdown(text)" in v3
    assert "MarkdownIt(" not in v2
    assert "MarkdownIt(" not in v3
    assert "{_esc(m7_md)}</pre>" not in v2
    assert "{_md_lite_v2(m7_md)}" in v2
    assert "{_md_lite_v2(raw)}" in v2


def test_pdf_generator_expands_safe_markdown_details_before_rendering():
    generator = (ROOT / "scripts" / "pptx_workspace" / "generate_v2_pdf.js").read_text(encoding="utf-8")
    assert "page.locator('details').evaluateAll" in generator
    assert "node.open = true" in generator


def test_monitoring_report_html_adapter_uses_server_ssot():
    adapter = (ROOT / "services" / "monitoring_report_html.py").read_text(encoding="utf-8")
    assert "render_safe_markdown(content)" in adapter
    assert "SAFE_MARKDOWN_CSS" in adapter


def test_export_report_pdf_delegates_to_monitoring_report_html():
    """server.py 替换片段由集成者落地；落地前 skip，落地后转强制断言。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    start = server.index("def export_report_pdf(")
    end = server.index('@app.get("/api/reports/{report_id}/export/excel")')
    endpoint = server[start:end]

    if "render_monitoring_report_html(report)" not in endpoint:
        pytest.skip(
            "server.py export_report_pdf 替换片段待集成者落地"
            "（片段见板块B交付报告延期集成清单）"
        )
    assert "markdown.markdown" not in endpoint
    assert "replace(chr(10), '<br>')" not in endpoint
    assert "node.open = true" in endpoint  # PDF 前 details 展开


def test_safe_markdown_component_keeps_controlled_details_parser():
    component = (FRONTEND / "components" / "SafeMarkdown.tsx").read_text(encoding="utf-8")
    # 无 rehype-raw 任意 HTML（查 import，注释中的"禁止 rehype-raw"字样不算）；details 走结构化预处理
    assert not re.search(r"from\s+['\"]rehype-raw['\"]", component)
    assert not re.search(r"rehypePlugins\s*=", component)
    assert "parseDetailsSegments" in component
    # 无 lookbehind 正则（旧 iOS/微信 WebView 兼容红线）
    assert "(?<" not in component
    # 链接受控 + 打印展开 + 表格横滚
    for marker in ("safeHttpUrl", "target=\"_blank\"", "noopener", "safe-markdown-table", "beforeprint"):
        assert marker in component
