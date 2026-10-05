from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_FILE = ROOT / "api" / "writing_structure_guidance_api.py"
SERVICE_FILE = ROOT / "services" / "writing_structure_guidance.py"
SERVER_FILE = ROOT / "server.py"
HALL_FILE = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
ARTICLE_SERVICE = ROOT / "writing" / "article_generator_service.py"


def test_agent_guidance_endpoint_uses_quote_access_not_admin_gate():
    text = API_FILE.read_text(encoding="utf-8")
    service_text = SERVICE_FILE.read_text(encoding="utf-8")

    assert "require_quote_access(request, quote_id)" in text
    assert "build_structure_guidance_for_quote" in text
    assert "get_active_strategy_version" in service_text
    assert "production_takeover" in text
    assert "_require_admin" not in text


def test_start_articles_accepts_only_boolean_opt_in_and_reloads_server_guidance():
    text = SERVER_FILE.read_text(encoding="utf-8")

    # [WP9-P0-1] 默认开:无 active 策略回落基线/默认模板,不报错(§15.1 部署即用)。
    assert "use_structure_guidance: bool = True" in text
    assert "build_structure_guidance_for_quote" in text
    assert "build_bounded_structure_instruction" in text
    assert "title=_t.get('title') or ''" in text
    assert "user_choice=_t.get('user_choice') or 'auto'" in text
    assert "structure_guidance_instruction" in text
    assert "structure_guidance_text" not in text


def test_writing_hall_uses_backend_guidance_without_daily_style_picker():
    text = HALL_FILE.read_text(encoding="utf-8")

    assert "系统推荐写法" not in text
    assert "使用系统推荐写法" not in text
    assert "每条待写文章右侧可直接下拉选择文章方向" not in text
    assert "标题已经决定文章方向" in text
    assert "use_structure_guidance: useStructureGuidance" in text
    assert "/structure-guidance" in text
    assert "采纳样本 {structureGuidance.evidence" not in text
    assert "明确引用 {structureGuidance.evidence" not in text


def test_article_generation_injects_bounded_project_guidance_not_global_prompt():
    text = ARTICLE_SERVICE.read_text(encoding="utf-8")

    assert "【本项目文章结构参考】" in text
    assert "structure_guidance_instruction" in text
