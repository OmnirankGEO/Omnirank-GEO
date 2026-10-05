from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "frontend" / "src" / "App.tsx"
TRACE_PAGE = ROOT / "frontend" / "src" / "pages" / "Admin" / "AgentTrace.tsx"


# [开源 E3 · B2 · 2026-09-28] tools/agent_loop/trace.py 随 E3 删除(不可达孤儿),守它的格退役。


def test_step20_agent_trace_page_shows_trace_sections():
    text = TRACE_PAGE.read_text(encoding="utf-8")

    assert "/api/social/admin/agent-trace" in text
    assert "计划状态" in text
    assert "工具调用" in text
    assert "模型决策" in text
    assert "扣费" in text
    assert "turnId" in text
