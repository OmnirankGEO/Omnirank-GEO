from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GEO_RESEARCH = ROOT / "frontend" / "src" / "pages" / "GeoResearch" / "GeoResearchCenter.tsx"
PUBLISH_CENTER = ROOT / "frontend" / "src" / "pages" / "Publishing" / "PublishCenter.tsx"
PACKAGE_JSON = ROOT / "frontend" / "package.json"
LOOKBEHIND_GUARD = ROOT / "frontend" / "scripts" / "ensure-no-lookbehind-in-dist.mjs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_geo_research_mobile_selection_uses_master_detail_contract():
    source = _read(GEO_RESEARCH)

    assert "返回行业列表" in source
    assert "ArrowLeft" in source
    assert "lg:hidden" in source
    assert "selectedIndustry ? 'hidden lg:block' : ''" in source
    assert "!selectedIndustry ? 'hidden lg:block' : ''" in source


def test_publish_center_tabs_wrap_on_small_screens():
    source = _read(PUBLISH_CENTER)

    assert "flex-wrap" in source
    assert "lg:flex-nowrap" in source
    assert "参谋" in source
    assert "发布参谋" in source


def test_mobile_fix_does_not_reintroduce_unsupported_lookbehind_regex():
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] 原第三个文件(社媒工作台主页面)随宿主整删
    source = "\n".join([_read(GEO_RESEARCH), _read(PUBLISH_CENTER)])

    assert "(?<=" not in source
    assert "(?<!" not in source


def test_frontend_build_has_lookbehind_guard():
    package_json = _read(PACKAGE_JSON)
    guard_source = _read(LOOKBEHIND_GUARD)

    assert "ensure-no-lookbehind-in-dist.mjs" in package_json
    assert "lookbehind" in guard_source
    assert "process.exit(1)" in guard_source
