from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_agent_template_endpoint_returns_no_pricing_data():
    source = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    start = source.index("async def agent_pricing_templates")
    end = source.index('\n\n@router.post("/pricing/skus"', start)
    body = source[start:end]

    assert 'return {"templates": []}' in body
    assert "sku_templates" not in body
    assert "wholesale_cents" not in body
    assert "suggested_retail_cents" not in body


def test_agent_create_dialog_does_not_fetch_or_render_recommended_templates():
    source = (ROOT / "frontend" / "src" / "pages" / "Agent" / "PricingCenter.tsx").read_text(
        encoding="utf-8"
    )
    api_source = (ROOT / "frontend" / "src" / "lib" / "v35w2Api.ts").read_text(
        encoding="utf-8"
    )
    dialog_start = source.index("function CreateSKUDialog")
    dialog_end = source.index("\nfunction AgentPricing", dialog_start) if "\nfunction AgentPricing" in source[dialog_start:] else len(source)
    dialog = source[dialog_start:dialog_end]

    assert "agentApi.listTemplates" not in dialog
    assert "从推荐模板开始" not in dialog
    assert "建议售价" not in dialog
    assert "listTemplates:" not in api_source
