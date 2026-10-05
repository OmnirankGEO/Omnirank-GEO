"""Public report v3 HTML renderer."""

from __future__ import annotations

import html
from typing import Any

from config import oss_attribution as _oss_attribution  # WO_329 开源版署名位(主仓 ENABLED=False ⇒ 空串,产物不变)
from services.report_v3_themes import get_theme
from services.safe_markdown_renderer import (
    SAFE_MARKDOWN_CSS,
    render_safe_markdown,
)

SECTION_TITLES = {
    "visibility": "AI 可见度",
    "ai_citation": "AI 引用链路",
    "content_quality": "内容结构质量",
    "authority": "权威证据",
    "competitors": "竞品差距",
    "keywords": "关键词机会",
    "conversion": "转化路径",
    "roadmap": "30 天行动路线",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# v3.6 白标 · 平台默认品牌从 SSOT 导入(本文件不硬编码平台名字面量)
from services.public_whitelabel import _PLATFORM_BRAND as _V3_PLATFORM_BRAND


def _v3_brand_company(branding: dict | None) -> str:
    """branding(resolve_branding_context surface=customer 的 brand dict)取公司名;
    None 或缺省 → 平台默认。决策 C:external_only 客户面不回退平台,用 company_name。"""
    if branding:
        c = (branding.get("company_name") or "").strip()
        if c:
            return c
    return _V3_PLATFORM_BRAND["company_name"]


def _safe_markdown(text: str | None) -> str:
    # 2026-07-22 sink census B10：删本地 bleach(attributes={} 无属性白名单 +
    # bleach=None 正则假兜底)，统一走 canonical safe Markdown SSOT。
    return f'<div class="safe-markdown">{render_safe_markdown(text)}</div>'


def _audience_modules(modules_jsonb: dict | None) -> dict:
    from services.report_html_renderer import get_client_report_modules

    return get_client_report_modules(modules_jsonb)


def _list_items(items: list[Any], limit: int = 6) -> str:
    if not items:
        return "<li>暂无明确条目，建议补充客户材料后重新生成。</li>"
    return "".join(f"<li>{_esc(item if not isinstance(item, dict) else item.get('title') or item.get('name') or item)}</li>" for item in items[:limit])


def _extract_rich_narrative(modules: dict, narrative: dict | None) -> dict:
    if narrative:
        return narrative
    rich = modules.get("rich_narrative") or {}
    return rich if isinstance(rich, dict) else {}


def _render_section(title: str, summary: str, body: str, *, as_pdf: bool) -> str:
    details_class = "is-open" if as_pdf else ""
    return f"""
    <section class="report-v3-section">
      <div class="section-kicker">下一步</div>
      <h2>{_esc(title)}</h2>
      <p class="section-summary">{_esc(summary)}</p>
      <div class="section-deep {details_class}">{body}</div>
    </section>
    """


def _css(theme: dict) -> str:
    return f"""
    * {{ box-sizing: border-box; }}
    html {{ -webkit-text-size-adjust: 100%; }}
    body {{
      margin: 0;
      background: {theme['bg']};
      color: {theme['ink']};
      font-family: Inter, 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif;
      line-height: 1.66;
    }}
    .report-v3 {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 48px 28px 72px;
    }}
    .report-v3-paper {{
      background: {theme['paper']};
      border: 1px solid {theme['line']};
      border-radius: 8px;
      overflow: hidden;
    }}
    .report-v3-cover {{
      min-height: 620px;
      padding: 70px 72px 48px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      border-bottom: 1px solid {theme['line']};
    }}
    .eyebrow {{ color: {theme['accent']}; font-size: 12px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }}
    h1 {{ margin: 18px 0 16px; font-size: 48px; line-height: 1.08; letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 25px; letter-spacing: 0; }}
    h3 {{ margin: 0 0 8px; font-size: 18px; letter-spacing: 0; }}
    p {{ margin: 0 0 10px; }}
    .score-row {{ display:flex; align-items:flex-end; gap:18px; margin-top: 28px; }}
    .score {{ font-size: 92px; line-height: .9; font-weight: 800; color: {theme['accent']}; }}
    .score-meta {{ color: {theme['muted']}; padding-bottom: 10px; }}
    .executive-summary {{ padding: 42px 72px; background: {theme['accent_soft']}; border-bottom: 1px solid {theme['line']}; }}
    .summary-grid {{ display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:16px; margin-top: 20px; }}
    .summary-card {{ background: {theme['paper']}; border:1px solid {theme['line']}; border-radius: 8px; padding:16px; min-height: 116px; }}
    .report-v3-section {{ padding: 38px 72px; border-bottom:1px solid {theme['line']}; }}
    .section-kicker {{ color:{theme['accent']}; font-size:12px; font-weight:700; margin-bottom:8px; }}
    .section-summary {{ color:{theme['muted']}; font-size: 16px; }}
    .section-deep {{ margin-top:18px; color:{theme['ink']}; }}
    .section-deep:not(.is-open) {{ max-height: 220px; overflow: hidden; }}
    .action-list {{ margin: 16px 0 0; padding-left: 20px; }}
    .report-pdf-footer {{ padding: 48px 72px; background: {theme['paper']}; border-top: 2px solid {theme['accent']}; }}
    .report-pdf-footer-brand {{ display:flex; align-items:center; gap:18px; margin-bottom:28px; }}
    .report-pdf-footer-logo {{ width:56px; height:56px; object-fit:contain; border-radius:8px; }}
    .report-pdf-footer-logo-placeholder {{ width:56px; height:56px; display:flex; align-items:center; justify-content:center; border-radius:8px; background:{theme['accent_soft']}; color:{theme['accent']}; font-weight:800; }}
    .report-pdf-footer-body {{ display:flex; justify-content:space-between; gap:28px; align-items:center; }}
    .report-pdf-footer-qr {{ width:116px; height:116px; }}
    @media print {{
      .report-v3 {{ padding: 0; max-width: none; }}
      .report-v3-paper {{ border:0; border-radius:0; }}
      .report-v3-cover, .report-v3-section, .executive-summary, .report-pdf-footer {{ break-after: page; min-height: 260mm; }}
      .report-pdf-footer {{ break-after: auto; }}
    }}
    @media (max-width: 760px) {{
      .report-v3 {{ padding: 18px 12px 36px; }}
      .report-v3-cover, .executive-summary, .report-v3-section, .report-pdf-footer {{ padding: 28px 22px; }}
      h1 {{ font-size: 34px; }}
      .summary-grid {{ grid-template-columns: 1fr; }}
      .score {{ font-size: 68px; }}
    }}
    """


def render_report_v3_html(
    *,
    meta: dict,
    modules_jsonb: dict,
    narrative: dict | None = None,
    theme: str = "light_corporate",
    as_pdf: bool = False,
    pdf_footer_html: str = "",
    branding: dict | None = None,
) -> str:
    """v3 公开报告 HTML。

    branding: v3.6 白标 brand dict(来自 resolve_branding_context(surface='customer'))·
        None → 平台默认品牌(向后兼容)。封面 eyebrow 走此字段。
    """
    theme_name, tokens = get_theme(theme)
    # [2026-06-06 白牌留白 · 老板拍 + P0 Codex] v3 = 对外公开报告 · 无白标/平台默认 dict 误传 → 封面品牌留白
    #   (不露平台名;is_real_agent_brand 识别 resolve_branding_context 对无白标返回的 _PLATFORM_BRAND dict)
    from services.public_whitelabel import is_real_agent_brand
    _brand_company = (branding.get("company_name") or "").strip() if is_real_agent_brand(branding) else ""
    modules = _audience_modules(modules_jsonb)
    from services.report_html_renderer import (
        is_client_report_ready,
        render_client_report_not_ready_html,
    )
    if not is_client_report_ready(modules_jsonb):
        return render_client_report_not_ready_html(meta)
    rich = _extract_rich_narrative(modules, narrative)
    # Old deterministic fallback rows contain generic, unverified brand claims.
    # They are not evidence and must not override the real numeric V2 modules.
    if str(rich.get("version") or "").startswith("v3_fallback_"):
        rich = {}
    # A previously enriched row may itself have been sourced from the stale
    # executive_summary alias.  Do not let that durable copy bypass the new
    # numeric-module precedence rule.
    if str(rich.get("source_module") or "").startswith("executive_summary"):
        rich = {}
    rich_sections = rich.get("sections") or {}
    # [CTO-15.23 2026-05-13 hotfix] v2 modules 真实 schema 用数字 key '0'~'8' + rendered_md
    # 老板报"303 公开报告什么信息都没有"根因:codex renderer 找 executive_summary key 找不到
    # fallback 链:modules['1'] 真实结论 → rich.narrative → 历史别名 → 占位
    module_one = modules.get("1") or modules.get(1) or {}
    findings = rich.get("key_findings") or []
    if not isinstance(findings, (list, tuple)):
        findings = []
    findings = [
        item.strip()
        for item in findings
        if isinstance(item, str) and item.strip()
    ]
    rich_summary = rich.get("executive_summary")
    if isinstance(rich_summary, str) and rich_summary.strip() == "证据不足，暂无可核验结论。":
        rich_summary = ""
    executive_summary = (
        module_one.get("insight")
        or module_one.get("conclusion_text")
        or rich_summary
        or "证据不足，暂无可核验结论。"
    )

    sections = []
    if rich_sections:
        for key, section in rich_sections.items():
            if not isinstance(section, dict):
                continue
            steps = section.get("next_steps") or []
            steps_html = f"<ul class=\"action-list\">{_list_items(steps)}</ul>" if steps else ""
            sections.append(
                _render_section(
                    SECTION_TITLES.get(key, str(key)),
                    section.get("summary") or "本节基于诊断数据生成，建议结合客户资料复核优先级。",
                    _safe_markdown(section.get("deep_analysis") or section.get("body") or "") + steps_html,
                    as_pdf=as_pdf,
                )
            )
    # [CTO-15.23 2026-05-13 hotfix] v2 modules['0'..'8'] rendered_md fallback
    # 当无 LLM narrative (auto_enrich=False · Phase 1 影子链路) 时 · v3 用 v2 预渲染 markdown
    # 真实 schema:modules['0']={module:0, rendered_md:"## ...", completeness:{...}}
    if not sections:
        numeric_keys = sorted(
            [k for k in modules if isinstance(k, str) and k.isdigit()],
            key=int,
        )
        v2_section_titles = {
            "0": "本次判断依据",
            "1": "1 分钟结论",
            "2": "客户拿不到的 4 件事",
            "3": "AI 搜索可见度详情",
            "4": "竞品对标分析",
            "5": "内容缺口诊断",
            "6": "可见度提升机会",
            "7": "下一步行动建议",
            "8": "评分依据明细",
        }
        for key in numeric_keys:
            mod = modules.get(key) or {}
            md = mod.get("rendered_md") or ""
            if not md:
                continue
            sections.append(
                _render_section(
                    v2_section_titles.get(key, f"模块 {key}"),
                    "",
                    _safe_markdown(md),
                    as_pdf=as_pdf,
                )
            )
        # SAP · 权威背书(3_authority 非数字 key · 补一节 fallback · v3 专属 narrative 二期)
        _auth_md = (modules.get("3_authority") or {}).get("rendered_md") or ""
        if _auth_md:
            sections.append(
                _render_section(
                    "权威背书情况",
                    "",
                    _safe_markdown(_auth_md),
                    as_pdf=as_pdf,
                )
            )
        # [B2-1] 行业 AI 引用格局(industry_landscape 非数字 key · 同 3_authority 补一节 fallback)
        _landscape_md = (modules.get("industry_landscape") or {}).get("rendered_md") or ""
        if _landscape_md:
            sections.append(
                _render_section(
                    "行业 AI 引用格局",
                    "",
                    _safe_markdown(_landscape_md),
                    as_pdf=as_pdf,
                )
            )
    if not sections:
        if executive_summary != "证据不足，暂无可核验结论。":
            sections = [
                _render_section(
                    "报告说明",
                    "本次已生成综合结论，暂无可展开的分项内容。",
                    _safe_markdown("后续补充分项证据后，可重新生成完整客户报告。"),
                    as_pdf=as_pdf,
                )
            ]
        else:
            # Do not manufacture a diagnosis or action plan from absent evidence.
            sections = [
                _render_section(
                    "数据说明",
                    "本次证据不足，暂不形成品牌判断或行动建议。",
                    _safe_markdown("请补充诊断数据后重新生成客户报告。"),
                    as_pdf=as_pdf,
                )
            ]
    if pdf_footer_html and as_pdf:
        footer = pdf_footer_html
        if "data-canonical-exclude" not in footer:
            footer = f'<section data-canonical-exclude="true" class="report-pdf-footer">{footer}</section>'
    else:
        footer = ""

    title = f"{_esc(meta.get('brand_name') or '品牌')} · GEO 诊断公开报告"
    score = meta.get("total_score")
    score_html = "—" if score is None else _esc(score)
    if findings:
        findings_html = "".join(
            f"<div class=\"summary-card\"><h3>结论 {idx}</h3><p>{_esc(item)}</p></div>"
            for idx, item in enumerate(findings[:3], 1)
        )
    else:
        findings_html = ""
        if executive_summary == "证据不足，暂无可核验结论。":
            findings_html = (
                '<div class="summary-card"><h3>数据说明</h3>'
                '<p>证据不足，暂无可核验结论。</p></div>'
            )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=2">
  <title>{title}</title>
  <style>{_css(tokens)}{SAFE_MARKDOWN_CSS}</style>
</head>
<body data-theme="{_esc(theme_name)}">
  <main class="report-v3">
    <article class="report-v3-paper">
      <section class="report-v3-cover">
        <div>
          <div class="eyebrow">{(_esc(_brand_company) + ' ') if _brand_company else ''}GEO Report</div>
          <h1>{_esc(meta.get('brand_name') or '品牌')}<br>AI 搜索可见度诊断</h1>
          <p>{_esc(meta.get('industry') or '')} {_esc(meta.get('created_at') or '')}</p>
        </div>
        <div class="score-row">
          <div class="score">{score_html}</div>
          <div class="score-meta">综合得分 / 100<br>{_esc(meta.get('level') or '')}</div>
        </div>
      </section>
      <section class="executive-summary">
        <h2>执行摘要</h2>
        <p>{_esc(executive_summary)}</p>
        <div class="summary-grid">{findings_html}</div>
      </section>
      {''.join(sections)}
      {footer}{_oss_attribution.report_footer_html()}
    </article>
  </main>
</body>
</html>"""
