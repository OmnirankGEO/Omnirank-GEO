"""Regression contract for the canonical customer-visible Markdown renderer."""

from __future__ import annotations

import re

import pytest

from services.safe_markdown_renderer import (
    markdown_structure_anomaly,
    render_safe_markdown,
)


def test_renders_consecutive_tables_chinese_and_escaped_pipes():
    source = """## AI 实测证据

| 问题 | 判断 |
| --- | --- |
| 中文内容 | 部分命中 |
| 深圳\\|龙岗 | 已核验 |
| 品牌｜产品 | 全角管道符保留 |

| 层级 | 命中引擎 |
| --- | --- |
| 品牌认知 | DeepSeek |
"""

    html = render_safe_markdown(source)

    assert html.count("<table>") == 2
    assert html.count('class="safe-markdown-table"') == 2
    assert "深圳|龙岗" in html
    assert "品牌｜产品" in html


def test_preserves_code_block_pipes_and_controlled_details():
    source = """<details>
<summary>展开证据</summary>

```text
left | right
```

> 仅代表本次样本。

</details>
"""

    html = render_safe_markdown(source)

    assert "<details>" in html
    assert "<summary>展开证据</summary>" in html
    assert "left | right" in html
    assert "<blockquote>" in html


@pytest.mark.parametrize(
    "payload",
    [
        '<script>alert(1)</script>安全正文',
        '<iframe src="https://evil.example"></iframe>安全正文',
        '<style>body{display:none}</style>安全正文',
        '<img src=x onerror="alert(1)">安全正文',
        '[危险](javascript:alert(1))',
        '[危险](data:text/html,boom)',
        '[危险](file:///etc/passwd)',
        '[邮件](mailto:test@example.com)',
        '<a href="jav&#x61;script:alert(1)" onclick="alert(2)">实体编码</a>',
        '[混淆](https://example.com\\@evil.example/path)',
    ],
)
def test_blocks_dangerous_html_and_urls(payload):
    html = render_safe_markdown(payload)
    lowered = html.lower()

    for forbidden in (
        "<script", "<iframe", "<style", "<img", "onerror=",
        "javascript:", "data:text", "file:", "mailto:", "onclick=", "href=",
    ):
        assert forbidden not in lowered
    assert "安全正文" in html or "危险" in html or "邮件" in html or "实体编码" in html or "混淆" in html


def test_http_links_are_hardened_and_bare_url_remains_prose():
    html = render_safe_markdown(
        "[可信来源](https://example.com/proof?q=1)\n\nhttp://example.org/raw"
    )

    assert html.count('target="_blank"') == 1
    assert html.count('rel="noopener noreferrer"') == 1
    assert 'href="https://example.com/proof?q=1"' in html
    assert 'href="http://example.org/raw"' not in html
    assert "http://example.org/raw" in html


def test_soft_line_breaks_are_preserved_without_affecting_code_blocks():
    html = render_safe_markdown("第一行\n第二行\n\n~~旧结论~~\n\n```text\nleft\nright\n```")

    assert "第一行<br>\n第二行" in html
    assert "<del>旧结论</del>" in html
    assert "left\nright" in html
    assert "left<br>" not in html


def test_raw_http_anchor_is_hardened_without_linkifying_code_or_prose():
    html = render_safe_markdown(
        '<a href="https://example.com/raw">原始链接</a>\n\n'
        '`https://example.com/code`\n\nhttps://example.com/prose'
    )

    assert '<a href="https://example.com/raw" target="_blank" rel="noopener noreferrer">' in html
    assert 'href="https://example.com/code"' not in html
    assert 'href="https://example.com/prose"' not in html


def test_long_content_is_not_truncated_and_empty_is_empty():
    tail = "末尾核验标记"
    source = "## 长文\n\n" + ("可验证内容。" * 20_000) + tail

    html = render_safe_markdown(source)

    assert tail in html
    assert len(html) > len(source)
    assert render_safe_markdown("") == ""
    assert render_safe_markdown(None) == ""


def test_internal_sentiment_markdown_exit_does_not_truncate_raw_evidence():
    from services.report_html_renderer import _render_sentiment_page

    tail = "情感证据末尾核验标记"
    raw = "### 原始判断\n\n" + ("完整证据。" * 500) + tail

    html = _render_sentiment_page(
        {"engines": [{"engine": "deepseek", "sentiment": "neutral", "raw_response": raw}]},
        "internal",
        "深圳驰鲸科技",
    )

    assert tail in html
    assert '<div class="safe-markdown"' in html


def test_flattened_history_is_readable_but_never_reconstructed():
    source = "| A | B | | --- | --- | | 1 | 2 |"
    assert markdown_structure_anomaly(source) == "flattened_table"

    html = render_safe_markdown(source)

    assert 'data-markdown-anomaly="flattened_table"' in html
    assert "历史内容的换行结构已缺失" in html
    assert "<table>" not in html


def test_one_line_details_keeps_its_existing_structure():
    source = "<details><summary>证据</summary>正文</details>"

    assert markdown_structure_anomaly(source) is None
    assert render_safe_markdown(source) == source


def _raw_report_data() -> dict:
    answer = """# 深圳驰鲸科技（Times Whale）

| 场景 | AI 判断 |
| --- | --- |
| 品牌认知 | **部分命中** |

<details>
<summary>代表原话</summary>

`Times Whale` 在回答中被提及。

</details>
"""
    return {
        "diagnosis_data": {
            "ai_visibility_data": {
                "detail_table": [
                    {
                        "question": "深圳驰鲸科技是做什么的？",
                        "results": {
                            "deepseek": {
                                "brand_detected": True,
                                "mentioned_brands": ["深圳驰鲸科技"],
                                "full_response": answer,
                            }
                        },
                    }
                ],
                "engines_tested": ["deepseek"],
                "question_types": {
                    "深圳驰鲸科技是做什么的？": "brand_awareness"
                },
            }
        }
    }


def test_report_writer_to_internal_and_customer_decision_v2_real_chain():
    from services.report_html_renderer import (
        _build_raw_tests_summary_v2,
        _render_raw_ai_appendix_page,
        render_customer_decision_page_html,
    )
    from services.report_writer_v2 import build_module_3_raw_ai_appendix

    raw = build_module_3_raw_ai_appendix(_raw_report_data())
    assert "\n| # | 层级 |" in raw["rendered_md"]
    stored_answer = raw["tests"][0]["results"][0]["full_response"]
    # 基线生产端契约（2026-07-22 板块B 不改报告原文/证据生成行为）：
    # _clean_text 仍把 AI 原文里的闭合标签写为 <\/…> 防结构破坏；
    # SSOT 对转义/未转义两种历史形态都安全渲染（详见 census §3 B1 与交付报告偏差说明）。
    assert "<summary>" in stored_answer
    assert "<\\/summary>" in stored_answer
    assert "<\\/details>" in stored_answer

    internal_html = _render_raw_ai_appendix_page({"3_raw": raw})
    customer_evidence_html = _build_raw_tests_summary_v2(raw["tests"])
    modules = {
        "client": {
            "modules": {
                "1": {"conclusion_text": "客户可见的真实结论。"},
                "2": {"summary": {"total_score": 52, "level": "成长级"}},
                "3": {"evidences": []},
                "3_raw": raw,
                "4": {"top_competitors": []},
                "6": {"todos": []},
            }
        }
    }
    public_html = render_customer_decision_page_html(
        meta={
            "brand_name": "深圳驰鲸科技",
            "total_score": 52,
            "level": "成长级",
            "generated_at": "2026-07-22T00:00:00Z",
        },
        modules_jsonb=modules,
        completeness={"score": 80, "level": "资料良好"},
    )

    for html in (internal_html, customer_evidence_html, public_html):
        assert "深圳驰鲸科技是做什么的？" in html
        assert "<details" in html
        assert "<summary" in html
        assert "<strong>部分命中</strong>" in html
        assert "<code>Times Whale</code>" in html
        assert not re.search(r"\|\s*---\s*\|", html)
        # 生产端写入的外层 details/summary 结构完整存活（外层标签未转义）
        assert "</details>" in html or "</summary>" in html
    assert "<table>" in internal_html
    assert "safe-markdown" in public_html


def test_internal_action_and_sentiment_markdown_use_same_safe_renderer():
    from services.report_html_renderer import _render_actions_page, _render_sentiment_page

    action_html = _render_actions_page(
        {
            "7": {
                "rendered_md": (
                    "## 30 天计划\n\n"
                    "| 周次 | 动作 |\n| --- | --- |\n| 1 | **补齐证据** |"
                )
            }
        },
        "internal",
    )
    sentiment_html = _render_sentiment_page(
        {
            "engines": [
                {
                    "engine": "deepseek",
                    "sentiment": "neutral",
                    "raw_response": (
                        "### 原始判断\n\n"
                        "| 证据 | 结论 |\n| --- | --- |\n| EV-01 | **中性** |\n\n"
                        '<script>window.__unsafe = true</script>'
                    ),
                }
            ]
        },
        "internal",
        "测试品牌",
    )

    for html in (action_html, sentiment_html):
        assert '<div class="safe-markdown"' in html
        assert "<table>" in html
        assert "<strong>" in html
        assert "<script" not in html


def test_monitoring_report_print_export_uses_same_safe_semantics():
    from services.monitoring_report_html import render_monitoring_report_html

    source = """## 监测结论

| 平台 | 命中 |
| --- | --- |
| DeepSeek | **是** |

<details>
<summary>查看证据</summary>

`EV-01` [来源](https://example.com/evidence)

</details>
"""
    html = render_monitoring_report_html({
        "version": "v2",
        "content": source,
        "report_type": '<img src=x onerror="alert(1)">',
        "period_start": "2026-07-01",
        "period_end": "2026-07-22",
        "summary_data": {"detection_rate": 50, "task_count": 12},
    })

    assert '<div class="content safe-markdown">' in html
    assert "<table>" in html
    assert "<details>" in html
    assert "<strong>是</strong>" in html
    assert 'rel="noopener noreferrer"' in html
    assert "<img" not in html
    assert "onerror=" not in html
    assert ".safe-markdown-table" in html
    assert "@media print" in html


def test_json_snapshot_round_trip_preserves_markdown_line_boundaries():
    import json

    source = "## 标题\n\n| A | B |\n| --- | --- |\n| 一 | 二 |\n"
    payload = {"client": {"modules": {"3_raw": {"rendered_md": source}}}}

    restored = json.loads(json.dumps(payload, ensure_ascii=False))

    assert restored["client"]["modules"]["3_raw"]["rendered_md"] == source
    assert restored["client"]["modules"]["3_raw"]["rendered_md"].count("\n") == source.count("\n")


def test_frontend_backend_parity_fixtures():
    """前后端一致性 fixture 的后端半（前端半：frontend/scripts/markdown-parity-check.mjs）。"""
    import json
    from pathlib import Path

    fixture_path = Path(__file__).resolve().parent / "fixtures" / "markdown_parity" / "cases.json"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 6

    for case in cases:
        html = render_safe_markdown(case["markdown"])
        for marker in case["backend_contains"]:
            assert marker in html, f"{case['id']}: backend missing {marker!r}\n{html}"
        for marker in case["not_contains"]:
            assert marker not in html, f"{case['id']}: backend leaked {marker!r}\n{html}"
