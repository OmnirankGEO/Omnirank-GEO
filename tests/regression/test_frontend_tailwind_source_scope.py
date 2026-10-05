from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_tailwind_scans_only_production_frontend_sources():
    css = (ROOT / "frontend" / "src" / "index.css").read_text(encoding="utf-8")

    assert "@import 'tailwindcss' source(none);" in css
    assert "@source './';" in css
    assert "@source '../index.html';" in css
    assert "@source '../tests" not in css
    assert "@source '../../tests" not in css
