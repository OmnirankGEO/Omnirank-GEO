def _modules():
    return {
        "client": {
            "modules": {
                "1": {
                    "insight": "品牌真实诊断结论。",
                    "rendered_md": "## 1 分钟结论\n\n品牌真实诊断结论。",
                },
                "executive_summary": {
                    "summary": "品牌已经具备基础认知，但 AI 搜索引用链路还需要补强。",
                    "key_findings": ["引用不足", "内容结构需要重做", "客户案例缺少证据"],
                },
                "actions": {
                    "items": [
                        {"title": "补齐高意图关键词页面", "priority": "P0", "owner": "内容负责人"}
                    ]
                },
                "rich_narrative": {
                    "executive_summary": "这是给老板看的 800 字摘要开头。",
                    "sections": {
                        "deep_analysis": {
                            "summary": "深度分析摘要",
                            "deep_analysis": "正常 **加粗** <script>alert(1)</script> [坏链接](javascript:alert(1))",
                        }
                    },
                },
            }
        }
    }


def test_render_report_v3_html_sanitizes_narrative_and_has_no_cta_controls():
    from services.report_html_renderer_v3 import render_report_v3_html

    html = render_report_v3_html(
        meta={"brand_name": "测试品牌", "total_score": 72, "level": "B"},
        modules_jsonb=_modules(),
        narrative=None,
        theme="light_corporate",
        as_pdf=False,
    )

    assert "<script" not in html.lower()
    assert "javascript:" not in html.lower()
    assert "<button" not in html.lower()
    assert "<form" not in html.lower()
    assert "测试品牌" in html


def test_render_report_v3_pdf_footer_is_canonical_excluded():
    from services.report_html_renderer_v3 import render_report_v3_html

    html = render_report_v3_html(
        meta={"brand_name": "测试品牌", "total_score": 72, "level": "B"},
        modules_jsonb=_modules(),
        narrative=None,
        theme="light_corporate",
        as_pdf=True,
        pdf_footer_html="<div>报告到此结束 · 联系发您报告的人</div>",
    )

    assert 'data-canonical-exclude="true"' in html
    assert "报告到此结束" in html


def test_pdf_footer_client_no_lead_capture():
    """[2026-06-01 玩法B 防穿帮] 客户面 PDF footer 禁留资/联系方式 · internal 视角保留(回归)"""
    from services.report_v3_pdf_footer import build_report_v3_pdf_footer

    brand = {
        "company_name": "王顾问工作室", "contact_name": "王顾问",
        "contact_phone": "13800138000", "contact_wechat": "wx123", "contact_email": "x@example.com",
    }
    client_footer = build_report_v3_pdf_footer(diagnosis_id=1, branding=brand, audience="client")
    for forbidden in ["对这份报告感兴趣", "联系我了解", "13800138000", "wx123", "x@example.com"]:
        assert forbidden not in client_footer, f"客户面 PDF footer 泄漏留资: {forbidden}"
    assert "报告到此结束" in client_footer
    assert "report-pdf-footer-qr" not in client_footer

    # internal/agent 视角保留顾问联系(回归:别误伤代理 PDF)
    internal_footer = build_report_v3_pdf_footer(diagnosis_id=1, branding=brand, audience="internal")
    assert "13800138000" in internal_footer
    assert "report-pdf-footer-qr" in internal_footer
