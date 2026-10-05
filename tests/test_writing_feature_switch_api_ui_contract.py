from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_FILE = ROOT / "api" / "writing_feature_switch_api.py"
SERVER_FILE = ROOT / "server.py"
COMPONENT = ROOT / "frontend" / "src" / "components" / "WritingSettingsDialog.tsx"
SWITCH_FILE = ROOT / "writing" / "feature_switches.py"


def test_feature_switch_api_is_admin_only_and_safe_surface():
    text = API_FILE.read_text(encoding="utf-8")

    assert 'APIRouter(prefix="/api/writing/feature-switches"' in text
    assert "def _require_admin" in text
    assert "status_code=401" in text
    assert "status_code=403" in text
    assert "@router.get" in text
    assert "@router.put" in text
    assert "r6h_customer_output" in text
    assert "customer_output_allowed" not in text
    assert "prompt_text" not in text
    assert "sidecar_path" not in text


def test_server_registers_feature_switch_router_fail_safe():
    text = SERVER_FILE.read_text(encoding="utf-8")

    assert "writing_feature_switch_api" in text
    assert "Writing Feature Switch API" in text
    assert "Could not import writing_feature_switch_api" in text


def test_writing_settings_has_operator_feature_switch_panel():
    text = COMPONENT.read_text(encoding="utf-8")
    switch_text = SWITCH_FILE.read_text(encoding="utf-8")

    assert 'value="feature-switches"' in text
    assert "功能开关" in text
    assert "日常总览" in text
    assert "高级工具" in text
    assert '"/api/writing/feature-switches"' in text
    assert "item.label" in text
    assert "item.description" in text
    assert "新模板用于后续文章" in switch_text
    assert "内部观察记录" in switch_text
    assert "实验内容给客户看" in switch_text
    assert "永久关闭" in text
    assert "使用新版默认" in text
    assert "当前可用模板" in text
    assert "新版内置模板" in text
    assert "请再次确认" in text
    assert "R6H_CUSTOMER_OUTPUT_ENABLED" not in text
