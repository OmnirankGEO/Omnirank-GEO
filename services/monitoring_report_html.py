"""Safe HTML document renderer for monitoring report print/PDF exports.

监测报告（v1/v2）打印与 PDF 导出的唯一整档渲染入口：
正文走 ``render_safe_markdown`` SSOT；元信息标签经 bleach 剥离后转义；
样式注入 ``SAFE_MARKDOWN_CSS``（含 ``@media print`` details/表格还原）。
页面、打印、PDF 仅容器样式不同，Markdown 语义与安全策略一致。
"""

from __future__ import annotations

from html import escape
from typing import Any

import bleach

from config import oss_attribution as _oss_attribution  # WO_329 开源版署名位(主仓 ENABLED=False ⇒ 空串,产物不变)
from services.safe_markdown_renderer import SAFE_MARKDOWN_CSS, render_safe_markdown


def _display(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "—"
    return f"{escape(str(value))}{suffix}"


def _display_label(value: Any) -> str:
    """渲染短元信息标签：先剥离一切标签再转义，不展示类标记垃圾。"""

    if value is None or value == "":
        return "—"
    text = bleach.clean(str(value), tags=set(), attributes={}, strip=True).strip()
    return escape(text) if text else "—"


def render_monitoring_report_html(report: dict) -> str:
    """Render every monitoring report version through the Markdown SSOT."""

    content = report.get("content") or ""
    summary = report.get("summary_data")
    summary = summary if isinstance(summary, dict) else {}
    is_v2 = report.get("version") == "v2" or content.lstrip().startswith("## 封面结论")
    badge = '<div class="v2-badge">报告 2.0 · M2 8 模块装配版</div>' if is_v2 else ""
    body_html = render_safe_markdown(content)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>监测报告</title>
  <style>
    body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; padding: 40px; line-height: 1.65; color: #2d3748; }}
    h1 {{ color: #1a365d; border-bottom: 2px solid #3182ce; padding-bottom: 10px; }}
    h2 {{ color: #1a365d; margin-top: 1.2em; padding-left: 10px; border-left: 4px solid #3182ce; }}
    h3 {{ color: #2c5282; margin-top: 1em; }}
    .meta {{ background: #f7fafc; padding: 15px; border-radius: 8px; margin: 20px 0; }}
    .meta-item {{ margin: 8px 0; }}
    .label {{ color: #718096; }}
    .value {{ color: #2d3748; font-weight: bold; }}
    .v2-badge {{ display: inline-block; background: #3182ce; color: white; padding: 4px 12px; border-radius: 12px; font-size: 12px; margin-bottom: 16px; }}
    .content {{ margin-top: 20px; }}
    {SAFE_MARKDOWN_CSS}
  </style>
</head>
<body>
  <h1>AI搜索可见度监测报告</h1>
  {badge}
  <div class="meta">
    <div class="meta-item"><span class="label">报告类型：</span><span class="value">{_display_label(report.get('report_type'))}</span></div>
    <div class="meta-item"><span class="label">监测周期：</span><span class="value">{_display(report.get('period_start'))} ~ {_display(report.get('period_end'))}</span></div>
    <div class="meta-item"><span class="label">检出率：</span><span class="value">{_display(summary.get('detection_rate'), '%')}</span></div>
    <div class="meta-item"><span class="label">监测次数：</span><span class="value">{_display(summary.get('task_count'), ' 次')}</span></div>
    <div class="meta-item"><span class="label">生成时间：</span><span class="value">{_display(report.get('created_at'))}</span></div>
  </div>
  <div class="content safe-markdown">{body_html}</div>{_oss_attribution.report_footer_html()}
</body>
</html>"""
