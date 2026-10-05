"""
P14-v14 · Jina key + settings 优先级反转 + research 专用 key 撤回 (C7)

覆盖:
  - JINA_CONCURRENCY 智能默认 (env 覆盖 · 默认 2 if key else 5)
  - Jina key 加到系统设置 UI (settings_manager + SettingsPage)
  - settings 优先级: 默认 env > settings.json · SETTINGS_JSON_OVERRIDE_ENV=true 反转
  - P14-v12 RESEARCH_MONITOR_DASHSCOPE_API_KEY / research 专用 DB key 撤干净
  - platforms.py _research_dashscope_key/_research_volc_key helper 仍保留 (走 env)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_jina_concurrency_smart_default():
    """JINA_CONCURRENCY 智能默认 (env 覆盖 · 默认 2 if key else 5 · 跟旧独立工具一致)"""
    src = (ROOT / "services" / "research_monitor" / "round_runner.py").read_text(encoding="utf-8")
    assert "os.getenv('JINA_CONCURRENCY'" in src or "os.getenv(\"JINA_CONCURRENCY\"" in src
    # 必须有 if Jina key 的智能默认 (2 vs 5)
    assert "2 if _jina_has_key else 5" in src or \
           ("(2 if" in src and "else 5)" in src), \
        "JINA_CONCURRENCY 必须智能默认: 有 key 2 · 无 key 5"


def test_jina_key_in_system_settings():
    """Jina key 必须加到系统设置 UI · 让 admin 改"""
    sm = (ROOT / "config" / "settings_manager.py").read_text(encoding="utf-8")
    assert "jina_api_key: str" in sm, "SystemSettings 必须有 jina_api_key 字段"
    assert '"jina_api_key": "JINA_API_KEY"' in sm, \
        "load_settings _env_key_map 必须含 jina_api_key 映射"
    assert "settings.jina_api_key" in sm, \
        "save_settings 必须 sync settings.jina_api_key 到 os.environ['JINA_API_KEY']"

    # 前端 SettingsPage 必须有 Jina 输入框
    sp = (ROOT / "frontend" / "src" / "pages" / "Settings" / "SettingsPage.tsx").read_text(encoding="utf-8")
    assert "jina_api_key" in sp, "SettingsPage UI 必须含 jina_api_key"
    assert "Jina" in sp, "必须显示 Jina 字样"


def test_settings_priority_env_first_with_optional_override():
    """review HIGH#3 · 默认 env > settings · SETTINGS_JSON_OVERRIDE_ENV=true 才反转"""
    sm = (ROOT / "config" / "settings_manager.py").read_text(encoding="utf-8")
    # 必须有 env gate · 不能强制反转
    assert "SETTINGS_JSON_OVERRIDE_ENV" in sm, \
        "必须有 SETTINGS_JSON_OVERRIDE_ENV env gate · 默认走 env > settings"
    assert "_override_env" in sm
    # 默认分支必须是 env 优先覆盖 settings
    assert "if env_val:" in sm, "默认模式 env 有值就覆盖 settings"
    # 反转分支必须 settings 空才 fallback env
    assert "if not cur_val and env_val:" in sm, \
        "反转模式 settings 空才 fallback env"
    # 必须 sync 最终值回 os.environ (让 os.getenv 也能拿到)
    assert "os.environ[env_name] = cur_val" in sm, \
        "load_settings 必须 sync 最终 key 回 os.environ · 让 platforms.py 用 os.getenv 也能拿到"


def test_research_keys_no_dedicated_db_layer():
    """老板 review HIGH#2 撤干净: 调研监测不再用单独的 DB key
    跟系统其他模块共用 DASHSCOPE_API_KEY / VOLC_API_KEY · 改 .env 或 UI(settings.json) 都生效
    """
    src = (ROOT / "services" / "research_monitor" / "platforms.py").read_text(encoding="utf-8")
    # 必须撤干净 _load_str_from_config + research_dashscope_api_key DB 读
    assert "_load_str_from_config" not in src, \
        "P14-v12 _load_str_from_config 必须撤干净 · 不再读 DB key"
    assert "research_dashscope_api_key" not in src
    assert "research_volc_api_key" not in src
    # config api schema 不能再有这俩
    cfg_src = (ROOT / "api" / "research_monitor_config_api.py").read_text(encoding="utf-8")
    assert "'research_dashscope_api_key'" not in cfg_src
    assert "'research_volc_api_key'" not in cfg_src
    # db seed 不能再有这俩
    db_src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    assert "research_dashscope_api_key" not in db_src
    assert "research_volc_api_key" not in db_src


def test_research_monitor_uses_dedicated_dashscope_key():
    """撤回 P14-v12: 不再用 RESEARCH_MONITOR_DASHSCOPE_API_KEY env
    跟系统其他模块共用 DASHSCOPE_API_KEY (settings.json/UI 改 → load_settings sync env)
    """
    src = (ROOT / "services" / "research_monitor" / "platforms.py").read_text(encoding="utf-8")
    # helper 仍存在 · 只是简化为单纯读 env
    assert "_research_dashscope_key" in src, "_research_dashscope_key helper 仍保留"
    # 但不能再有 RESEARCH_MONITOR_DASHSCOPE_API_KEY (P14-v12 撤了)
    assert "RESEARCH_MONITOR_DASHSCOPE_API_KEY" not in src, \
        "P14-v12 RESEARCH_MONITOR_DASHSCOPE_API_KEY 必须撤干净"
