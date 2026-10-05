"""
report_html_renderer — M2 W5 · 咨询报告 HTML 渲染(网页 + PDF 共用)

CTO-B 2026-04-26 W5 · 决策点 3:Playwright HTML → PDF · 网页和 PDF 同一套组件
决策点 4:light 默认 · 不接 dark
老板验收第 4/7 条:客户版公开报告是 light 专业报告 · PDF 版式像咨询报告

输入:
  - v2_modules_jsonb dict(从 diagnosis_records.report_v2_modules_jsonb 读)
    含 internal/client 两套 modules · 选 audience 决定走哪套
  - completeness dict
  - meta dict(brand_name, total_score, level, created_at, audience='client'|'internal')

输出:
  完整 HTML 字符串 · A4-friendly · light 主题 · 含:
    封面页 / 执行摘要 / 评分页 / 证据页 / 竞品页 / 行动计划 / 30 天计划 / 方案承接 / 附录

PDF 生成走 Playwright:
  await page.set_content(html); await page.pdf(format='A4', ...)
"""
from __future__ import annotations

import html as _html_lib
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from config import oss_attribution as _oss_attribution  # WO_329 开源版署名位(主仓 ENABLED=False ⇒ 空串,产物不变)

from services.safe_markdown_renderer import (
    SAFE_MARKDOWN_CSS,
    render_safe_markdown,
)

logger = logging.getLogger("GEO-ReportHTMLRenderer")


# ============================================================================
# CSS · light 主题 · A4 友好(决策点 4)
# ============================================================================

_BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html { -webkit-text-size-adjust: 100%; }
body {
  font-family: 'PingFang SC', 'Helvetica Neue', 'Microsoft YaHei', Arial, sans-serif;
  font-size: 14px;
  line-height: 1.65;
  color: #1f2937;
  background: #ffffff;
}
.report {
  max-width: 920px;
  margin: 0 auto;
  padding: 0;
}
@media print {
  body { font-size: 12px; }
  .page { page-break-after: always; padding: 32mm 22mm; min-height: 297mm; }
  .page:last-child { page-break-after: auto; }
  .no-print { display: none !important; }
}
.page {
  padding: 56px 64px;
  background: #ffffff;
  border-bottom: 1px solid #f3f4f6;
}
h1, h2, h3, h4 { color: #0f172a; font-weight: 600; }
h1 { font-size: 32px; line-height: 1.25; margin-bottom: 8px; }
h2 { font-size: 22px; line-height: 1.3; margin: 28px 0 14px; padding-bottom: 8px; border-bottom: 2px solid #e5e7eb; }
h3 { font-size: 16px; line-height: 1.4; margin: 18px 0 10px; color: #111827; }
h4 { font-size: 14px; margin: 12px 0 6px; color: #374151; }
p { margin: 6px 0; }
ul, ol { padding-left: 22px; margin: 8px 0; }
li { margin: 3px 0; }
strong { font-weight: 600; color: #111827; }
.muted { color: #6b7280; font-size: 12px; }
.subtle { color: #9ca3af; font-size: 11px; }

/* 封面页 · 大数字 + 等级 + 完整度 */
.cover {
  min-height: 70vh;
  display: flex; flex-direction: column; justify-content: center;
  background: linear-gradient(180deg, #ffffff 0%, #f9fafb 100%);
}
.cover-eyebrow { font-size: 12px; letter-spacing: 0.18em; color: #6b7280; text-transform: uppercase; margin-bottom: 16px; }
.cover-brand { font-size: 36px; font-weight: 700; color: #0f172a; margin-bottom: 6px; }
.cover-meta { color: #6b7280; font-size: 13px; margin-bottom: 36px; }
.cover-score-row { display: flex; align-items: baseline; gap: 18px; margin: 8px 0 28px; }
.cover-score-num {
  font-size: 88px; font-weight: 800; line-height: 1;
  color: var(--score-color, #0f172a);
  font-feature-settings: "tnum";
}
.cover-score-frac { font-size: 22px; color: #9ca3af; }
.cover-level-badge {
  display: inline-block; padding: 6px 14px; border-radius: 20px;
  font-size: 13px; font-weight: 600;
  background: var(--badge-bg, #e5e7eb);
  color: var(--badge-fg, #111827);
}
.cover-conclusion {
  margin-top: 24px; padding: 18px 22px;
  background: #f9fafb; border-left: 4px solid var(--score-color, #6366f1);
  font-size: 15px; line-height: 1.7; color: #1f2937; border-radius: 4px;
}

/* 本次判断依据 banner */
.completeness-banner {
  margin: 18px 0 24px; padding: 14px 18px;
  border-radius: 8px; border: 1px solid;
  display: flex; align-items: flex-start; gap: 12px;
}
.completeness-banner.high { border-color: #d1fae5; background: #ecfdf5; }
.completeness-banner.mid  { border-color: #fde68a; background: #fffbeb; }
.completeness-banner.low  { border-color: #fecaca; background: #fef2f2; }
.completeness-banner .num { font-size: 28px; font-weight: 700; line-height: 1; }
.completeness-banner.high .num { color: #059669; }
.completeness-banner.mid  .num { color: #d97706; }
.completeness-banner.low  .num { color: #dc2626; }
.completeness-banner .body { flex: 1; }

/* 评分总览 · 5 维度 + 3 层关键词 */
.score-grid {
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px;
  margin: 18px 0;
}
.score-tile {
  border: 1px solid #e5e7eb; border-radius: 8px;
  padding: 14px 12px; background: #ffffff;
}
.score-tile-label { font-size: 11px; color: #6b7280; margin-bottom: 6px; }
.score-tile-value { font-size: 22px; font-weight: 700; color: #0f172a; }
.score-tile-max { font-size: 12px; color: #9ca3af; margin-left: 4px; }
.score-tile-status { font-size: 11px; margin-top: 4px; padding: 2px 8px; border-radius: 4px; display: inline-block; }
.status-good   { background: #d1fae5; color: #065f46; }
.status-mid    { background: #fef3c7; color: #92400e; }
.status-low    { background: #fee2e2; color: #991b1b; }
.status-empty  { background: #e5e7eb; color: #374151; }
.status-na     { background: #f3f4f6; color: #6b7280; }

.strata-table { width: 100%; border-collapse: collapse; margin: 12px 0; }
.strata-table th, .strata-table td {
  border: 1px solid #e5e7eb; padding: 8px 12px; text-align: left; font-size: 13px;
}
.strata-table th { background: #f9fafb; color: #374151; font-weight: 600; }

/* 证据卡片 */
.evidence-card {
  border: 1px solid #e5e7eb; border-left: 4px solid #6366f1;
  border-radius: 6px; padding: 12px 16px; margin: 10px 0;
  background: #fafbff;
}
.evidence-card.level-A { border-left-color: #059669; background: #f0fdf4; }
.evidence-card.level-B { border-left-color: #d97706; background: #fffbeb; }
.evidence-card.level-C { border-left-color: #6b7280; background: #f9fafb; }
.evidence-meta { font-size: 11px; color: #6b7280; margin-bottom: 6px; }
.evidence-quote { font-size: 13px; line-height: 1.6; color: #1f2937; }

/* 原始 AI 问答附录 · 必须完整展开,不得被卡片/iframe裁剪 */
.raw-ai-page details {
  margin: 10px 0;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #ffffff;
  overflow: visible;
}
.raw-ai-page details[open] { overflow: visible; }
.raw-ai-page summary {
  cursor: pointer;
  list-style: none;
  padding: 12px 14px;
  font-weight: 600;
  color: #111827;
}
.raw-ai-page summary::-webkit-details-marker { display: none; }
.raw-ai-page summary::before {
  content: "展开";
  display: inline-block;
  margin-right: 8px;
  padding: 1px 6px;
  border-radius: 999px;
  background: #eef2ff;
  color: #4338ca;
  font-size: 11px;
  font-weight: 600;
}
.raw-ai-page details[open] > summary::before { content: "收起"; }
.raw-ai-question-body { padding: 0 14px 14px; border-top: 1px solid #f3f4f6; }
.raw-ai-engine {
  margin: 12px 0;
  padding: 12px;
  border-radius: 8px;
  background: #f8fafc;
  border: 1px solid #e5e7eb;
}
.raw-ai-engine-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
  color: #475569;
  font-weight: 600;
}
.raw-ai-status-hit { color: #047857; }
.raw-ai-status-miss { color: #b91c1c; }
.raw-ai-answer {
  white-space: pre-wrap;
  word-break: break-word;
  overflow: visible;
  max-height: none;
  margin: 0;
  padding: 12px;
  border-left: 3px solid #c7d2fe;
  background: #ffffff;
  color: #1f2937;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
}
.raw-ai-fold-note {
  margin: 10px 0;
  padding: 8px 10px;
  border: 1px solid #fde68a;
  border-radius: 8px;
  background: #fffbeb;
  color: #92400e;
  font-size: 12px;
  line-height: 1.6;
}
.raw-ai-original {
  margin-top: 10px;
  border: 1px dashed #cbd5e1 !important;
  background: #f8fafc !important;
}
.raw-ai-original summary {
  color: #475569;
  font-size: 12px;
  padding: 10px 12px;
}
.raw-ai-answer-original {
  border-left-color: #94a3b8;
  background: #f8fafc;
}
.raw-ai-fallback {
  white-space: pre-wrap;
  word-break: break-word;
  max-height: none;
  overflow: visible;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 14px;
}

/* 行动卡片 */
.action-card {
  border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 18px;
  margin: 12px 0; background: #ffffff;
}
.action-card .priority {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 11px; font-weight: 600; margin-right: 8px;
}
.priority.P0 { background: #fee2e2; color: #991b1b; }
.priority.P1 { background: #fef3c7; color: #92400e; }
.priority.P2 { background: #e0e7ff; color: #3730a3; }
.action-meta-grid {
  display: grid; grid-template-columns: 110px 1fr; gap: 6px 14px;
  margin-top: 8px; font-size: 12px;
}
.action-meta-grid .key { color: #6b7280; }
.action-meta-grid .val { color: #1f2937; }

/* CTA / 方案承接 */
.cta-card {
  margin: 28px 0; padding: 28px 32px;
  background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
  color: #ffffff; border-radius: 12px;
  text-align: center;
}
.cta-card h3 { color: #ffffff; font-size: 20px; margin-bottom: 10px; }
.cta-card p { color: rgba(255, 255, 255, 0.92); font-size: 13px; margin: 6px 0; }
.cta-button {
  display: inline-block; margin-top: 14px;
  padding: 10px 26px; background: #ffffff; color: #4f46e5;
  border-radius: 999px; font-weight: 600; font-size: 14px;
  text-decoration: none;
}

/* 表格 */
table { width: 100%; border-collapse: collapse; margin: 10px 0; }
th, td { border: 1px solid #e5e7eb; padding: 8px 12px; text-align: left; font-size: 13px; }
th { background: #f9fafb; color: #374151; font-weight: 600; }

/* 异常 banner */
.error-banner {
  margin: 16px 0; padding: 14px 18px;
  border: 1px solid #fecaca; background: #fef2f2; border-radius: 8px;
  color: #991b1b;
}

/* 附录 */
.appendix {
  margin-top: 32px; padding-top: 18px; border-top: 1px dashed #e5e7eb;
  font-size: 11px; color: #9ca3af;
}
.appendix p { margin: 4px 0; }

/* 漏斗评分(CTO-G 2026-04-27) */
.funnel-hero {
  border-radius: 16px;
  padding: 32px 40px;
  margin: 24px 0;
  text-align: center;
}
.funnel-score-num {
  font-size: 96px;
  font-weight: 800;
  line-height: 1;
  font-feature-settings: "tnum";
}
.funnel-score-frac { font-size: 24px; color: #6b7280; margin-top: 4px; }
.funnel-level-badge {
  display: inline-block;
  padding: 8px 20px;
  border-radius: 999px;
  font-size: 16px;
  font-weight: 600;
  margin: 16px 0;
}
.funnel-meaning {
  font-size: 14px;
  color: #374151;
  max-width: 600px;
  margin: 0 auto;
  line-height: 1.6;
}
.funnel-container { display: flex; flex-direction: column; gap: 14px; margin: 18px 0; }
.funnel-layer {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 14px 18px;
  background: #ffffff;
}
.funnel-layer-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 8px;
}
.funnel-layer-head strong { font-size: 15px; color: #0f172a; }
.layer-weight { font-size: 11px; color: #9ca3af; }
.funnel-layer-bar {
  position: relative;
  height: 24px;
  background: #f3f4f6;
  border-radius: 4px;
  overflow: hidden;
  margin-bottom: 6px;
}
.funnel-layer-bar .bar-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.4s ease;
}
.funnel-layer-bar .bar-rate {
  position: absolute;
  top: 50%;
  right: 10px;
  transform: translateY(-50%);
  font-size: 12px;
  font-weight: 600;
  color: #1f2937;
}
.funnel-layer-meta {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  margin-top: 4px;
}
.layer-score { font-weight: 600; color: #0f172a; }
.layer-verdict {
  margin-top: 6px;
  font-size: 12px;
  font-weight: 500;
}
.verdict-good { color: #059669; }
.verdict-mid  { color: #d97706; }
.verdict-low  { color: #dc2626; }
"""


def _esc(s: Any) -> str:
    return _html_lib.escape(str(s) if s is not None else "")


def _normalize_public_evidence_url(value: Any) -> str | None:
    """Return a canonical clickable HTTP(S) URL or fail closed to plain text."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if (
        not raw
        or "\\" in raw
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw)
    ):
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if not parsed.netloc or parsed.username is not None or parsed.password is not None:
            return None
        # Accessing port also rejects malformed values such as :not-a-port.
        port = parsed.port
        host = parsed.hostname
        if not host:
            return None
        normalized_host = host.encode("idna").decode("ascii").lower()
        normalized_netloc = (
            f"[{normalized_host}]" if ":" in normalized_host else normalized_host
        )
        if port is not None:
            normalized_netloc = f"{normalized_netloc}:{port}"
        return urlunsplit(
            (
                parsed.scheme.lower(),
                normalized_netloc,
                parsed.path or "",
                parsed.query or "",
                parsed.fragment or "",
            )
        )
    except (UnicodeError, ValueError):
        return None


CLIENT_REPORT_NOT_READY_CODE = "CLIENT_REPORT_NOT_READY"
CLIENT_REPORT_NOT_READY_MESSAGE = (
    "客户报告尚未就绪，本次客户版数据不足。请稍后刷新，或联系发给你链接的人。"
)


def get_client_report_modules(modules_jsonb: Any) -> dict:
    """Return the explicit customer artifact; never borrow another audience."""
    root = modules_jsonb if isinstance(modules_jsonb, dict) else {}
    client = root.get("client")
    if not isinstance(client, dict):
        return {}
    modules = client.get("modules")
    return modules if isinstance(modules, dict) else {}


def is_client_report_ready(modules_jsonb: Any) -> bool:
    """Require the canonical customer conclusion before exposing or billing.

    Appendices, enrichment aliases and arbitrary module keys are supplemental;
    they cannot prove that the customer report itself reached its terminal
    artifact state.  The V2 producer's numeric module 1 is the shared contract.
    """
    modules = get_client_report_modules(modules_jsonb)
    module_one = modules.get("1") or modules.get(1)
    if not isinstance(module_one, dict):
        return False
    return any(
        isinstance(module_one.get(field), str) and module_one[field].strip()
        for field in ("insight", "conclusion_text", "rendered_md")
    )


def render_client_report_not_ready_html(meta: dict | None = None) -> str:
    """Render a privacy-safe placeholder when customer modules are absent.

    A public report is a separately generated customer artifact.  It must not
    borrow agent-only modules just to keep a page renderable.
    """
    safe_meta = meta if isinstance(meta, dict) else {}
    brand_name = _esc(safe_meta.get("brand_name") or "品牌")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{brand_name} · 客户报告尚未就绪</title>
</head>
<body data-report-status="not_ready" data-error-code="{CLIENT_REPORT_NOT_READY_CODE}" style="margin:0;background:#f8fafc;color:#0f172a;font-family:Inter,'PingFang SC','Microsoft YaHei',sans-serif;">
  <main style="max-width:720px;margin:0 auto;padding:72px 24px;">
    <section style="background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:36px;box-shadow:0 12px 32px rgba(15,23,42,.06);">
      <p style="margin:0 0 10px;color:#64748b;font-size:14px;">{brand_name} · GEO 诊断报告</p>
      <h1 style="margin:0 0 16px;font-size:28px;line-height:1.3;">客户报告尚未就绪</h1>
      <p style="margin:0;color:#475569;line-height:1.8;">{_esc(CLIENT_REPORT_NOT_READY_MESSAGE)}</p>
    </section>
  </main>
</body>
</html>"""


def _with_canonical_score(modules: dict, meta: dict) -> dict:
    """Overlay canonical score metadata without mutating persisted modules."""
    normalized = dict(modules or {})
    score_module = dict(normalized.get("2") or {})
    summary = dict(score_module.get("summary") or {})
    funnel = dict(score_module.get("funnel") or {})
    original_summary_level = summary.get("level")
    original_level_meta = dict(summary.get("level_meta") or {})
    score = (meta or {}).get("total_score")
    level = (meta or {}).get("level")

    for target in (summary, funnel):
        if score is None:
            target.pop("total_score", None)
        else:
            target["total_score"] = score
        if level:
            target["level"] = level
        else:
            target.pop("level", None)

    level_meta = {}
    if level:
        try:
            from tools.scoring.funnel_score import FUNNEL_LEVEL_META

            level_meta = dict(FUNNEL_LEVEL_META.get(level) or {})
        except Exception:
            level_meta = {}
    if not level_meta and original_summary_level == level:
        level_meta = original_level_meta
    summary["level_meta"] = level_meta
    funnel["level_meta"] = level_meta
    score_module["summary"] = summary
    score_module["funnel"] = funnel
    normalized["2"] = score_module
    return normalized


# ============================================================================
# v3.6 白标 · branding 归一化(SSOT resolve_branding_context 的 brand dict）
#   branding=None → 平台默认(向后兼容现有调用方)
#   branding 来自 services.public_whitelabel.resolve_branding_context(surface='customer')
#   决策 C：external_only 客户面不回退平台,代理未填字段用 company_name 兜底
#   平台默认常量从 SSOT(_PLATFORM_BRAND)导入,本文件不再硬编码平台名字面量
# ============================================================================
from services.public_whitelabel import _PLATFORM_BRAND as _PLATFORM_DEFAULT_BRAND


def _norm_branding(branding: dict | None) -> dict:
    """归一化 branding dict;None 时返回平台默认(SSOT 平台品牌)。"""
    if not branding:
        return dict(_PLATFORM_DEFAULT_BRAND)
    b = dict(_PLATFORM_DEFAULT_BRAND)
    for k in _PLATFORM_DEFAULT_BRAND:
        v = branding.get(k)
        if v is not None and (not isinstance(v, str) or v.strip()):
            b[k] = v
    # product_name 缺省回退 company_name(决策 C 兜底)
    if not (b.get("product_name") or "").strip():
        b["product_name"] = b.get("company_name") or _PLATFORM_DEFAULT_BRAND["company_name"]
    return b


def _brand_company(branding: dict | None) -> str:
    return _norm_branding(branding).get("company_name") or _PLATFORM_DEFAULT_BRAND["company_name"]


def _brand_product(branding: dict | None) -> str:
    b = _norm_branding(branding)
    return (b.get("product_name") or b.get("company_name") or _PLATFORM_DEFAULT_BRAND["company_name"])


def _brand_company_client(branding: dict | None, audience: str | None) -> str:
    """[2026-06-06 白牌留白 · 老板拍] 客户面无白标 → 空(留白·绝不露平台名);有白标→公司名;内部/agent→平台兜底。"""
    from services.public_whitelabel import is_real_agent_brand
    # [P0 Codex 2026-06-06] platform_default dict 误传 → 视作无白标(否则 client 面露平台名)
    co = (branding.get("company_name") or "").strip() if is_real_agent_brand(branding) else ""
    if co:
        return co
    if (audience or "").lower() == "client":
        return ""
    return _brand_company(branding)


def _level_color_vars(level: str) -> tuple[str, str, str]:
    """返回 (score_color, badge_bg, badge_fg) HSL 色

    支持新漏斗 6 档(主导/健康/成长/边缘/危急/隐形)+ 老 6 档(领先/成熟/成长/起步/待提升/空白)
    """
    if not level:
        return "#1f2937", "#e5e7eb", "#111827"
    # 漏斗新等级(CTO-G 2026-04-27)
    if "主导" in level:
        return "#059669", "#d1fae5", "#065f46"
    if "健康" in level:
        return "#16a34a", "#dcfce7", "#14532d"
    if "边缘" in level:
        return "#ea580c", "#ffedd5", "#9a3412"
    if "危急" in level:
        return "#dc2626", "#fee2e2", "#991b1b"
    if "隐形" in level:
        return "#9f1239", "#ffe4e6", "#881337"
    # 老等级
    if "领先" in level:
        return "#059669", "#d1fae5", "#065f46"
    if "成熟" in level:
        return "#16a34a", "#dcfce7", "#14532d"
    if "成长" in level:  # 漏斗"成长级"或老"成长" 共用
        return "#d97706", "#fef3c7", "#92400e"
    if "起步" in level:
        return "#d97706", "#fef3c7", "#92400e"
    if "待提升" in level:
        return "#ea580c", "#ffedd5", "#9a3412"
    return "#9ca3af", "#f3f4f6", "#374151"


def _completeness_class(score: int | None) -> str:
    if score is None:
        return "low"
    if score >= 60:
        return "high"
    if score >= 35:
        return "mid"
    return "low"


def _status_class(status: str) -> str:
    if status == "良好":
        return "status-good"
    if status == "一般":
        return "status-mid"
    if status == "待提升":
        return "status-low"
    if status == "空白":
        return "status-empty"
    return "status-na"


# ============================================================================
# 模块渲染 helpers · 从 modules_jsonb 数据生成 HTML 段
# ============================================================================

def _render_completeness_banner(completeness: dict) -> str:
    if not completeness or completeness.get("score") is None:
        return ""
    cls = _completeness_class(completeness.get("score"))
    notes = completeness.get("impact_notes") or []
    miss = completeness.get("missing_summary") or ""
    notes_html = ""
    if notes:
        notes_html = "<ul style='margin:6px 0 0;padding-left:20px;font-size:12px;'>" + "".join(
            f"<li>{_esc(n)}</li>" for n in notes
        ) + "</ul>"
    return f"""
    <div class="completeness-banner {cls}">
      <div class="num">{_esc(completeness.get('score'))}<span style="font-size:14px;color:#6b7280;">/100</span></div>
      <div class="body">
        <div style="font-weight:600;font-size:14px;color:#0f172a;">资料完整度 · {_esc(completeness.get('level',''))}</div>
        <div class="muted">只表示本次判断依据是否充足，不是 GEO 综合评分。</div>
        <div class="muted">{_esc(miss)}</div>
        {notes_html}
      </div>
    </div>
    """


def _render_cover(meta: dict, modules: dict, completeness: dict, branding: dict | None = None) -> str:
    brand = _esc(meta.get("brand_name", "品牌"))
    industry = _esc(meta.get("industry", ""))
    city = _esc(meta.get("city", ""))
    created_at = _esc(meta.get("created_at", ""))
    audience = meta.get("audience", "client")

    # 评分与等级由 services.report_v2_score 统一解析后注入 meta。
    # 渲染器不得再从模块摘要或旧列自行重算，否则网页/PDF 会重新漂移。
    score = meta.get("total_score")
    level = meta.get("level") or "数据不足"
    score_display = "—" if score is None else _esc(score)

    score_color, badge_bg, badge_fg = _level_color_vars(level)

    cover_module = modules.get("1") or {}
    conclusion = cover_module.get("conclusion_text") or cover_module.get("insight") or ""

    industry_city = " · ".join(filter(None, [industry, city]))

    completeness_html = _render_completeness_banner(completeness)

    # 漏斗等级名已含"级"字(危急级/主导级/...)· 老等级名不含 · 兼容两套
    display_level = _client_level_name(level, audience)  # WJ-08 客户面等级名中性化(颜色仍用真实 level)
    badge_text = _esc(display_level)
    if display_level and not str(display_level).endswith("级"):
        badge_text = f"{badge_text}级"

    return f"""
    <section class="page cover" style="--score-color:{score_color};--badge-bg:{badge_bg};--badge-fg:{badge_fg};">
      <div class="cover-eyebrow">{(_esc(_brand_company_client(branding, audience)) + ' · ') if _brand_company_client(branding, audience) else ''}GEO 诊断报告 · {'客户版' if audience == 'client' else '内部版'}</div>
      <h1 class="cover-brand">{brand}</h1>
      <p class="cover-meta">{industry_city}{' · ' if industry_city else ''}{created_at}</p>
      <div class="cover-score-row">
        <div class="cover-score-num">{score_display}</div>
        <div class="cover-score-frac">/100</div>
        <div class="cover-level-badge">{badge_text}</div>
      </div>
      <div class="cover-conclusion">{_esc(conclusion)}</div>
      {completeness_html}
    </section>
    """


def _render_executive_summary(modules: dict, audience: str = "client") -> str:
    """执行摘要 · 抽取 Module 1 + Module 2 关键数据 + Module 6 top 3 行动"""
    cover = modules.get("1") or {}
    radar = modules.get("2") or {}
    actions = modules.get("6") or {}
    evidence = modules.get("3") or {}

    summary_obj = radar.get("summary") or {}
    dims = radar.get("dimensions") or []
    strata = radar.get("keyword_strata") or []
    todos = (actions.get("personalized_actions") or actions.get("todos") or [])[:3]

    # weakest 2 dims
    weakest = sorted(
        [d for d in dims if d.get("score") is not None],
        key=lambda d: d.get("percent", 100)
    )[:2]
    weakest_html = "".join(
        f"<li><strong>{_esc(d['label'])}</strong> · "
        f"{d['score']}/{d['max']} ({d['percent']}%)</li>"
        for d in weakest
    )

    strata_lines = []
    for s in strata:
        if s.get("data_sufficient"):
            strata_lines.append(
                f"<li><strong>{_esc(s['label'])}:</strong> {s['rate_pct']}% "
                f"({s['detected']}/{s['total']} 命中 · 置信 {_esc(s['confidence'])})</li>"
            )
        else:
            strata_lines.append(
                f"<li><strong>{_esc(s['label'])}:</strong> "
                f"<span class='muted'>{_esc(s['rate_text'])}</span></li>"
            )

    actions_html = ""
    for a in todos:
        # personalized_action 字段(W3)· 兼容 todo 简版
        title = (
            a.get("recommended_content")
            or a.get("action")
            or a.get("original_action")
            or "（动作未定义）"
        )
        priority = a.get("priority") or a.get("original_priority") or "P1"
        actions_html += (
            f"<li><span class='priority {priority}' style='margin-right:6px;'>{priority}</span>"
            f"{_esc(title)}</li>"
        )

    evidence_count = evidence.get("evidence_count") or {}
    evidence_total = evidence.get("evidence_total", 0)

    return f"""
    <section class="page">
      <h2>执行摘要</h2>
      <h3>核心结论</h3>
      <p>{_esc(cover.get('conclusion_text', ''))}</p>

      <h3>当前状况</h3>
      <ul>
        <li>综合评分 <strong>{_esc(summary_obj.get('total_score') if summary_obj.get('total_score') is not None else '—')}/{_esc(summary_obj.get('max_score', 100))}</strong> ·
            等级 <strong>{_esc(_client_level_name(summary_obj.get('level') or '数据不足', audience))}</strong></li>
        <li>AI 实测证据 <strong>{evidence_total} 条</strong>
            (直接证据 {evidence_count.get('A', 0)} · 参考证据 {evidence_count.get('B', 0)} · 待验证线索 {evidence_count.get('C', 0)})</li>
      </ul>

      <h3>最拖后腿的 2 件事</h3>
      <ul>{weakest_html or '<li class="muted">数据不足</li>'}</ul>

      <h3>AI 推荐发生在哪一层</h3>
      <ul>{''.join(strata_lines) or '<li class="muted">暂无</li>'}</ul>

      <h3>建议先做的 3 件事</h3>
      <ol>{actions_html or '<li class="muted">本期未生成行动项</li>'}</ol>
    </section>
    """


def _render_score_page(modules: dict, audience: str = "client") -> str:
    """评分总览(漏斗 3 层版本 · CTO-G 2026-04-27)"""
    m2 = modules.get("2") or {}
    funnel = m2.get("funnel") or {}
    layers = funnel.get("layers", [])
    total = funnel.get("total_score")
    level = funnel.get("level") or "数据不足"
    meta = funnel.get("level_meta") or {}
    color_hex = meta.get("color_hex", "#9f1239")

    # 大字总分框
    score_block = f"""
    <div class="funnel-hero" style="background:{color_hex}10;border:2px solid {color_hex};">
      <div class="funnel-score-num" style="color:{color_hex};">{_esc(total if total is not None else '—')}</div>
      <div class="funnel-score-frac">/ 100</div>
      <div class="funnel-level-badge" style="background:{color_hex};color:#fff;">{_esc(_client_level_name(level, audience))}</div>
      <div class="funnel-meaning">{_esc(meta.get('business_meaning',''))}</div>
    </div>
    """

    # 3 层漏斗进度条
    layer_blocks = []
    for layer in layers:
        rate = layer.get("rate", 0) or 0
        rate_pct = layer.get("rate_pct", 0) or 0
        bar_pct = min(100, rate_pct)
        sufficient = layer.get("data_sufficient", True)

        if rate >= 0.7:
            verdict_class = "verdict-good"
            verdict_text = "✅ AI 稳定提到你"
        elif rate >= 0.3:
            verdict_class = "verdict-mid"
            verdict_text = "🟡 部分命中"
        else:
            verdict_class = "verdict-low"
            verdict_text = "🔴 几乎不提你"

        # total=0 才用占位 · total<5 但 >0 仍展示 rate% 加 "数据不足" 短缀
        if (layer.get("total") or 0) <= 0:
            rate_label = "(无样本 · 数据不足)"
        elif not sufficient:
            rate_label = f"{rate_pct}% · 数据不足"
        else:
            rate_label = f"{rate_pct}%"

        # [audit #7 返修] 分母用 effective_weight(重归一后真实计分上限),空样本层显示"无样本"
        #   (原 score/weight 用原始权重 → 重归一后会出现 "100 / 20" 类矛盾数)。
        _eff_w = layer.get("effective_weight")
        if (layer.get("total") or 0) <= 0:
            _score_display = "无样本"
        else:
            _score_display = f"{layer.get('score', 0)} / {_eff_w if _eff_w else layer.get('weight', 0)}"

        layer_blocks.append(f"""
        <div class="funnel-layer">
          <div class="funnel-layer-head">
            <strong>{_esc(layer.get('label',''))}</strong>
            <span class="layer-weight">权重 {_esc(layer.get('weight', 0))}</span>
          </div>
          <div class="funnel-layer-bar">
            <div class="bar-fill" style="width:{bar_pct}%;background:{color_hex};"></div>
            <span class="bar-rate">{_esc(rate_label)}</span>
          </div>
          <div class="funnel-layer-meta">
            <span class="muted">{_esc(layer.get('business',''))}</span>
            <span class="layer-score">{_esc(_score_display)}</span>
          </div>
          <div class="layer-verdict {verdict_class}">{verdict_text}</div>
        </div>
        """)

    return f"""
    <section class="page">
      <h2>GEO 总分</h2>
      {score_block}
      <h3>30 秒看懂 · 3 层漏斗</h3>
      <p class="muted">GEO = 让 AI 在回答用户问题时主动推荐你。漏斗层 = 客户从"知道你"到"考虑购买"的三个问题阶段。</p>
      <table>
        <thead><tr><th>报告词</th><th>客户能听懂的意思</th></tr></thead>
        <tbody>
          <tr><td>品牌认知层</td><td>客户已经知道你,问 AI"这家公司怎么样",AI 能不能说清楚。</td></tr>
          <tr><td>决策获客层</td><td>客户还不知道你,问"本地哪家靠谱",AI 会不会把你列入候选。</td></tr>
          <tr><td>场景转化层</td><td>客户已经在比较方案、价格、避坑,AI 会不会引用你来支撑决策。</td></tr>
        </tbody>
      </table>
      <div class="funnel-container">
        {''.join(layer_blocks)}
      </div>
    </section>
    """


def _render_evidence_page(modules: dict) -> str:
    ev = modules.get("3") or {}
    evidences = ev.get("evidences") or []
    total = ev.get("evidence_total", 0)
    shortfall = ev.get("evidence_shortfall", 0)

    if not evidences:
        return f"""
        <section class="page">
          <h2>AI 实测证据</h2>
          <div class="error-banner">
            <strong>⚠ 数据不足 · 本期 0 条有效 AI 实测证据(目标 ≥ 5 条)</strong>
            <p>本期不做趋势推断 · 报告余下模块只给方向判断。建议后续用同一组问题复测,对比 AI 是否开始提到品牌。</p>
          </div>
        </section>
        """

    cards = []
    for e in evidences[:10]:
        level = e.get("evidence_level", "C")
        snippet = (e.get("response_snippet") or "")[:200]
        cards.append(f"""
        <div class="evidence-card level-{level}">
          <div class="evidence-meta">
            <strong>{_esc({'A': '直接证据', 'B': '参考证据', 'C': '待验证线索'}.get(level, '待验证线索'))}</strong> · {_esc(e.get('source_label',''))} ·
            关键词 <code>{_esc(e.get('keyword',''))}</code> ·
            {_esc(e.get('tested_at','')[:10])}
          </div>
          <div class="evidence-quote">「{_esc(snippet)}」</div>
        </div>
        """)

    shortfall_html = ""
    if shortfall > 0:
        shortfall_html = (
            f"<div class='error-banner'>⚠ 本期共 {total} 条 · 距 ≥ 5 条目标缺 {shortfall} 条 · "
            f"以下结论只做方向判断 · 建议后续用同一组问题复测</div>"
        )

    return f"""
    <section class="page">
      <h2>AI 实测证据</h2>
      {shortfall_html}
      {''.join(cards)}
    </section>
    """


def _render_raw_ai_appendix_page(modules: dict) -> str:
    """客户版原始 AI 回复附录 · 展示全部测试词和各引擎完整回答."""
    raw = modules.get("3_raw") or {}
    tests = raw.get("tests") or []
    # [返修 2026-06-07] custom_tests 上移到守卫之前 · 系统题缺失/旧数据/部分失败时自定义付费题仍渲染
    custom_tests_legacy = raw.get("custom_tests") or []
    if tests or custom_tests_legacy:
        def _one_q_block(t):
            results = t.get("results") or []
            engine_blocks = []
            for r in results:
                brands = r.get("mentioned_brands") or []
                brands_label = f" · 提到:{_esc('、'.join(str(x) for x in brands[:8]))}" if brands else ""
                detected = bool(r.get("brand_detected"))
                status_cls = "raw-ai-status-hit" if detected else "raw-ai-status-miss"
                answer = r.get("full_response") or "本引擎未返回可展示回答。"
                raw_answer = r.get("raw_full_response") or ""
                folded_note = r.get("fold_note") or ""
                folded_html = ""
                if r.get("was_folded") and raw_answer:
                    folded_html = f"""
                  <div class="raw-ai-fold-note">
                    { _esc(folded_note or '原文存在连续重复片段,为保证客户阅读体验,默认折叠重复内容。') }
                    证据未丢失,可展开下方查看未折叠原文。
                  </div>
                  <details class="raw-ai-original">
                    <summary>查看未折叠原文</summary>
                    <pre class="raw-ai-answer raw-ai-answer-original">{_esc(_scrub_vendor(raw_answer))}</pre>
                  </details>
                    """
                engine_blocks.append(f"""
                <div class="raw-ai-engine">
                  <div class="raw-ai-engine-head">
                    <span>{_esc(_neutral_engine(r.get('engine'), r.get('engine_label') or r.get('engine') or '未知引擎'))}</span>
                    <span class="{status_cls}">{_esc(r.get('status') or ('提到品牌' if detected else '未提到品牌'))}</span>
                    <span class="muted">{brands_label}</span>
                  </div>
                  <!-- 2026-05-22 BUG 5 修:答案走 _md_lite_v2 渲染 markdown(table/bold/header)
                       原 <pre>{{_esc(answer)}}</pre> 让 LLM markdown 原文显在客户报告 · 老板报"markdown 没渲染"
                       改 <div> + _md_lite_v2:table → HTML table · bold/list/header 正常 · 仍 escape XSS
                       2026-07-22 sink census B3:_md_lite_v2 改调 safe_markdown SSOT · 容器换 safe-markdown class -->
                  <div class="raw-ai-answer safe-markdown">{_md_lite_v2(_scrub_vendor(answer))}</div>
                  {folded_html}
                </div>
                """)
            _layer_label = t.get('layer') or ('自定义' if t.get('source') == 'custom' else '')
            return f"""
            <details>
              <summary>{_esc(t.get('index', ''))}. {_esc(t.get('question', ''))} · {_esc(_layer_label)} · 命中 {_esc(t.get('hit_count', 0))}/{_esc(t.get('engine_count', len(results) or 1))}</summary>
              <div class="raw-ai-question-body">
                {''.join(engine_blocks)}
              </div>
            </details>
            """

        question_blocks = [_one_q_block(t) for t in tests]
        # [2026-06-07 修复客户付费看不到] 自定义题(custom_tests)结构同系统题 · 复用单题渲染 · 独立 section 追加
        #   cdv2 与本 legacy appendix 原本都只渲染 tests · custom_tests 早在 module 数据里却没渲染 → 客户付费看不到
        custom_blocks = [_one_q_block(t) for t in custom_tests_legacy]

        return f"""
        <section class="page raw-ai-page">
          <h2>原始测试数据</h2>
          <p class="muted">
            本次共设计 {_esc(raw.get('questions', len(tests)))} 个测试问题 ·
            计划 {_esc(raw.get('total_planned', '—'))} 次 AI 问答 ·
            有效 {_esc(raw.get('total_tests', '—'))} 次 ·
            品牌被提及 {_esc(raw.get('detected_count', '—'))} 次。
          </p>
          <p class="muted">默认收起,方便先看结论;需要核对证据时,可以逐题展开查看每个 AI 引擎的完整原文。</p>
          {''.join(question_blocks)}
          {('<div class="raw-ai-custom" style="margin-top:18px;padding-top:14px;border-top:1px dashed #cbd5e1;"><h3>自定义检测问题(您本次填写)</h3><p class="muted">这些是您填写的专属业务问题 · 独立于系统基础题 · 作为补充证据展示,不改变本期基础评分口径。</p>' + ''.join(custom_blocks) + '</div>') if custom_blocks else ''}
        </section>
        """

    rendered_md = (raw.get("rendered_md") or "").strip()
    if not rendered_md:
        return ""
    return f"""
    <section class="page raw-ai-page">
      <h2>原始测试数据</h2>
      <p class="muted">这份报告生成较早,只保存了 Markdown 附录。以下按原始文本完整展示,不做截断。</p>
      <details>
        <summary>查看原始问答附录</summary>
        <div class="raw-ai-question-body">
          <div class="raw-ai-fallback safe-markdown">{_md_lite_v2(rendered_md)}</div>
        </div>
      </details>
    </section>
    """


def _render_competitor_page(modules: dict) -> str:
    m4 = modules.get("4") or {}
    declared = m4.get("declared_competitors") or []
    top = m4.get("top_competitors") or []

    if not declared and not top:
        return ""

    rows = ""
    for c in declared[:5]:
        if not isinstance(c, dict):
            continue
        rows += f"""
        <tr>
          <td><strong>{_esc(c.get('name',''))}</strong></td>
          <td>{_esc(c.get('url') or '—')}</td>
          <td>{_esc(c.get('note') or '—')}</td>
        </tr>
        """

    top_rows = ""
    if top:
        for i, item in enumerate(top[:5], start=1):
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                title, cnt = item[0], item[1]
            else:
                title, cnt = str(item), 0
            top_rows += f"<tr><td>{i}</td><td>{_esc(title)[:80]}</td><td>{_esc(cnt)}</td></tr>"

    return f"""
    <section class="page">
      <h2>竞品分析(AI 搜索同频)</h2>
      {f'<h3>代理建档竞品({len(declared)} 个 · 真数据)</h3><table><thead><tr><th>竞品名</th><th>URL</th><th>备注</th></tr></thead><tbody>{rows}</tbody></table>' if declared else ''}
      {f'<h3>AI 搜索同频引用源(启发式)</h3><table><thead><tr><th>排名</th><th>竞品/引用源</th><th>同频出现</th></tr></thead><tbody>{top_rows}</tbody></table>' if top_rows else ''}
    </section>
    """


def _render_actions_page(modules: dict, audience: str) -> str:
    m5 = modules.get("5") or {}
    m6 = modules.get("6") or {}
    m7 = modules.get("7") or {}

    if (audience or "").lower() == "client":
        return _render_interpretation_page_plain(modules)

    personalized = m6.get("personalized_actions") or []
    cards = ""
    for a in personalized[:8]:
        prio = a.get("original_priority") or "P1"
        cards += f"""
        <div class="action-card">
          <h4><span class="priority {prio}">{prio}</span>{_esc(a.get('original_issue','(未命名)'))}</h4>
          <div class="action-meta-grid">
            <div class="key">薄弱维度</div><div class="val">{_esc(a.get('weak_dimension',''))}</div>
            <div class="key">目标关键词层</div><div class="val">{_esc(a.get('target_stratum',''))}</div>
            <div class="key">推荐发布内容</div><div class="val"><strong>{_esc(a.get('recommended_content',''))}</strong></div>
            <div class="key">证据基础</div><div class="val muted">{_esc(a.get('evidence_basis',''))}</div>
            <div class="key">30 天验收</div><div class="val">{_esc(a.get('acceptance_30d',''))}</div>
          </div>
        </div>
        """

    # Module 5 机会估算 · 只解释可见度缺口,不输出固定周期承诺
    m5_info = ""
    if m5 and not m5.get("skipped"):
        gap_p50 = m5.get("gap_to_p50", 0)
        gap_p90 = m5.get("gap_to_p90", 0)
        low = m5.get("est_aiq_lift_pct_low", 0)
        high = m5.get("est_aiq_lift_pct_high", 0)
        formula_note = '<p class="muted">该区间只用于理解可见度缺口,不代表固定收益或执行承诺。</p>'
        m5_info = f"""
        <h3>机会解读</h3>
        <p>距「健康级」(70 分)还差 <strong>{gap_p50} 分</strong> · 距「主导级」(85 分)还差 <strong>{gap_p90} 分</strong></p>
        <p>AI 可见度缺口参考: <strong>+{low}% ~ +{high}%</strong></p>
        {formula_note}
        """

    # Module 7 30 天计划
    m7_md = m7.get("rendered_md", "") if m7 else ""

    return f"""
    <section class="page">
      <h2>行动计划</h2>
      {m5_info}
      <h3>个性化行动建议(Top {len(personalized[:8])})</h3>
      {cards or '<p class="muted">本期未生成行动项 · 数据不足</p>'}
      <details style="margin-top:18px;">
        <summary class="muted" style="cursor:pointer;">查看完整解读提纲</summary>
        <div class="safe-markdown" style="font-size:13px;color:#374151;">{_md_lite_v2(m7_md)}</div>
      </details>
    </section>
    """


def _render_offer_page(modules: dict, audience: str, branding: dict | None = None) -> str:
    """报告客观收束 · 诊断报告不在此生成销售规划。"""
    internal_hint = ""
    if (audience or "").lower() != "client":
        internal_hint = (
            '<p class="muted" style="margin-top:12px;">代理视角:建议围绕证据、趋势和数据边界沟通;'
            '具体商业沟通可另行展开,不要把报告解读写成效果承诺。</p>'
        )

    return f"""
    <section class="page">
      <h2>报告客观收束</h2>
      <div style="margin:18px 0;padding:18px 22px;border:1px solid #e5e7eb;border-left:4px solid #64748b;border-radius:8px;background:#f8fafc;">
        <p><strong>如需进一步沟通,建议围绕这份报告逐项解读。</strong></p>
        <ul>
          <li>哪些结论有直接证据支持。</li>
          <li>哪些结论只是趋势判断,需要保守理解。</li>
          <li>哪些结论因为数据不足,暂时不能下确定判断。</li>
          <li>客户自己的业务范围、目标客户和现有公开资料是否与报告判断一致。</li>
        </ul>
        <p class="muted">本页不生成执行承诺,只说明这份报告适合如何被阅读和核对。</p>
      </div>
      {internal_hint}
    </section>
    """


def _render_sentiment_page(sentiment: dict | None, audience: str, brand_name: str) -> str:
    """舆情诊断 section · 仅 internal 视角(代理端)显示。

    Q4=B 老板拍板:仅代理看 · 客户决策页(render_customer_decision_page_html)不调用本函数
    Q3=B+C:4 引擎独立 marker(透明)+ 任一负面红色警示 banner(防漏掉低概率风险)

    输入 sentiment 结构(由 tools.sentiment_classifier.analyze_brand_sentiment 输出):
      engines: [{engine, sentiment, summary, key_concerns, raw_response}, ...]
      consensus: positive | neutral | negative | mixed
      alert: bool
      alert_reason: str
    """
    if not sentiment or audience != "internal":
        return ""
    engines = sentiment.get("engines") or []
    if not engines:
        return ""
    engine_labels = []
    for engine in engines:
        label = _neutral_engine(engine.get("engine"), engine.get("engine_label") or engine.get("engine") or "")
        if label and label not in engine_labels:
            engine_labels.append(label)
    engine_scope = "、".join(_esc(label) for label in engine_labels) or "本次 AI 实测"

    SENTIMENT_COLOR = {"positive": "#10b981", "neutral": "#6b7280", "negative": "#ef4444", "mixed": "#f59e0b"}
    SENTIMENT_LABEL = {"positive": "正面", "neutral": "中性", "negative": "负面", "mixed": "混合"}
    SENTIMENT_ICON = {"positive": "🟢", "neutral": "⚪", "negative": "🔴", "mixed": "🟡"}

    consensus = sentiment.get("consensus") or "neutral"
    alert = bool(sentiment.get("alert"))
    alert_reason = sentiment.get("alert_reason") or ""
    query = sentiment.get("query") or ""

    engine_cards = []
    for e in engines:
        eng_name = _esc(e.get("engine") or "?")
        s = e.get("sentiment") or "neutral"
        color = SENTIMENT_COLOR.get(s, "#6b7280")
        label = SENTIMENT_LABEL.get(s, "未知")
        icon = SENTIMENT_ICON.get(s, "⚪")
        summary = _esc(e.get("summary") or "(无摘要)")
        concerns = e.get("key_concerns") or []
        raw = e.get("raw_response") or ""

        concerns_html = ""
        if concerns:
            items = "".join(f"<li>{_esc(c)}</li>" for c in concerns if c)
            if items:
                concerns_html = (
                    f'<ul style="margin:8px 0 0;padding-left:20px;color:#dc2626;'
                    f'font-size:13px;line-height:1.6;">{items}</ul>'
                )
        raw_html = ""
        if raw:
            raw_html = (
                '<details style="margin-top:10px;">'
                '<summary style="cursor:pointer;color:#6b7280;font-size:12px;">'
                f'查看 {eng_name} 原始回复'
                '</summary>'
                '<div class="safe-markdown" style="margin-top:8px;padding:10px;background:#f9fafb;'
                'border-radius:6px;font-size:12px;color:#4b5563;'
                'line-height:1.6;max-height:300px;'
                f'overflow-y:auto;">{_md_lite_v2(raw)}</div>'
                '</details>'
            )
        engine_cards.append(
            f'<div style="padding:14px;border:1px solid #e5e7eb;'
            f'border-left:4px solid {color};border-radius:8px;background:#fff;">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">'
            f'<span style="font-size:16px;">{icon}</span>'
            f'<span style="font-weight:600;color:#111827;">{eng_name}</span>'
            f'<span style="padding:2px 8px;border-radius:4px;background:{color}1A;'
            f'color:{color};font-size:11px;font-weight:600;">{label}</span>'
            f'</div>'
            f'<div style="color:#374151;font-size:13px;line-height:1.6;">{summary}</div>'
            f'{concerns_html}{raw_html}</div>'
        )

    alert_banner = ""
    if alert and alert_reason:
        alert_banner = (
            '<div style="margin-bottom:16px;padding:14px 16px;border:1px solid #fca5a5;'
            'background:#fef2f2;border-radius:8px;">'
            '<div style="display:flex;gap:10px;align-items:flex-start;">'
            '<span style="font-size:20px;">⚠️</span>'
            '<div>'
            '<div style="font-weight:700;color:#991b1b;font-size:14px;margin-bottom:4px;">'
            '需关注 · 检测到负面口碑信号'
            '</div>'
            f'<div style="color:#7f1d1d;font-size:13px;line-height:1.6;">{_esc(alert_reason)}</div>'
            '<div style="margin-top:6px;color:#991b1b;font-size:12px;">'
            '建议代理深入调研后再决定签约价位 · 防止低价签亏'
            '</div>'
            '</div></div></div>'
        )

    consensus_color = SENTIMENT_COLOR.get(consensus, "#6b7280")
    consensus_label = SENTIMENT_LABEL.get(consensus, "未知")
    consensus_icon = SENTIMENT_ICON.get(consensus, "⚪")

    return f"""
    <section class="page">
      <div style="margin-bottom:8px;font-size:11px;color:#6b7280;letter-spacing:0.12em;text-transform:uppercase;font-weight:700;">SENTIMENT · 本次舆情诊断</div>
      <h2 style="font-size:24px;font-weight:700;color:#111827;margin:0 0 6px;">品牌口碑监控 <span style="font-size:13px;color:#6b7280;font-weight:500;margin-left:8px;">仅代理可见</span></h2>
      <p style="color:#6b7280;font-size:13px;margin:0 0 16px;">模拟客户决策视角 · {engine_scope} 对「{_esc(brand_name)}」的回复 · 拿到的是"客户决策时听到什么"</p>
      <div style="margin-bottom:16px;padding:10px 14px;background:#f9fafb;border-radius:8px;font-size:12px;color:#6b7280;"><strong>诊断 query</strong>: {_esc(query)}</div>
      <div style="margin-bottom:16px;padding:14px 16px;border:1px solid {consensus_color}33;background:{consensus_color}0D;border-radius:8px;display:flex;align-items:center;gap:10px;">
        <span style="font-size:22px;">{consensus_icon}</span>
        <div>
          <div style="font-size:11px;color:#6b7280;font-weight:600;letter-spacing:0.05em;">本次实测共识</div>
          <div style="font-size:18px;font-weight:700;color:{consensus_color};">{consensus_label}</div>
        </div>
      </div>
      {alert_banner}
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;">
        {''.join(engine_cards)}
      </div>
    </section>
    """


def _render_appendix(
    meta: dict,
    completeness: dict,
    audience: str,
    branding: dict | None = None,
    engine_scope_text: str = "本次 AI 实测",
) -> str:
    groups = (completeness or {}).get("groups") or []
    rows = ""
    for g in groups:
        miss = g.get("missing_fields") or []
        miss_label = "、".join(f.get("label", "") for f in miss[:5]) + ("..." if len(miss) > 5 else "")
        rows += f"""
        <tr>
          <td>{_esc(g.get('label',''))}</td>
          <td>{_esc(g.get('score',0))}/{_esc(g.get('weight',0))}</td>
          <td>{_esc(g.get('percent',0))}%</td>
          <td>{_esc(miss_label or '—')}</td>
        </tr>
        """
    return f"""
    <section class="page">
      <h2>附录</h2>
      <h3>本次判断依据明细</h3>
      <table>
        <thead><tr><th>依据组</th><th>充分度</th><th>占比</th><th>缺口</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>

      <div class="appendix">
        <p>报告版本: {(_esc(_brand_company_client(branding, audience)) + ' ') if _brand_company_client(branding, audience) else ''}GEO 报告 2.0 · {('客户版' if audience == 'client' else '内部版')}</p>
        <p>生成时间: {_esc(meta.get('created_at','') or '')}</p>
        <p>数据来源: {_esc(engine_scope_text)} · 本报告快照中的公开资料</p>
        <p>声明: 报告中"数据不足"代表当前证据还不够支撑强结论 · 非系统故障。</p>
      </div>{_oss_attribution.report_footer_html()}
    </section>
    """


# ============================================================================
# 主入口
# ============================================================================

def _render_authority_inner_html(pack: dict | None) -> str:
    """SAP(权威背书)→ CSS 无关 HTML 片段(说人话 · native 元素 + 极简 inline style + 原生 details 折叠)。
    全部动态文本经 _esc 防 XSS;无 3_authority / 不可用 → 返回 ''(调用端不渲染该区,不报错)。
    决策页与普通 v2/PDF 共用此内片段。
    """
    if not pack or not pack.get("available"):
        return ""
    plain = pack.get("plain") or {}
    gc = plain.get("grouped_counts") or {}
    total = int(gc.get("authority", 0)) + int(gc.get("portal", 0)) + int(gc.get("common", 0)) + int(gc.get("risk", 0))
    conclusion = _esc(plain.get("conclusion", ""))
    note = _esc(plain.get("note", ""))
    parts = [f'<p style="font-size:15px;line-height:1.7;margin:8px 0;">{conclusion}</p>']

    if total == 0:
        if note:
            parts.append(f'<p style="font-size:12px;color:#94A3B8;margin-top:10px;">{note}</p>')
        return "".join(parts)

    # 三类来源数量(首屏 · chips)
    chips = [
        ("权威媒体/政府/百科", gc.get("authority", 0)),
        ("门户/行业网站", gc.get("portal", 0)),
        ("普通网站/自媒体", gc.get("common", 0)),
    ]
    if int(gc.get("risk", 0)) > 0:
        chips.append(("低可信来源", gc.get("risk", 0)))
    chip_html = "".join(
        f'<span style="display:inline-block;padding:4px 10px;margin:4px 6px 4px 0;'
        f'border-radius:14px;background:#F1F5F9;font-size:13px;">{_esc(lbl)} <b>{int(cnt)}</b></span>'
        for lbl, cnt in chips
    )
    parts.append(f'<div style="margin:10px 0;">{chip_html}</div>')

    # 背书强度(次要 · 明确与诊断总分隔离)
    score = int(plain.get("score", 0))
    smax = int(plain.get("score_max", 20))
    parts.append(
        f'<p style="font-size:12px;color:#94A3B8;margin:4px 0 14px;">'
        f'权威背书强度 {score}/{smax} · 仅供参考 · 不计入诊断总分</p>'
    )

    # 给你的建议(三段式 · 王姐可直接讲)
    sug = plain.get("suggestion") or {}
    parts.append(
        '<div style="background:#F8FAFC;border-left:3px solid #6CBE1E;padding:12px 14px;'
        'border-radius:6px;margin:8px 0;">'
        f'<p style="margin:4px 0;"><b>现在的问题是:</b>{_esc(sug.get("problem", ""))}</p>'
        f'<p style="margin:4px 0;"><b>对客户意味着:</b>{_esc(sug.get("meaning", ""))}</p>'
        f'<p style="margin:4px 0;"><b>下一步建议:</b>{_esc(sug.get("next_step", ""))}</p>'
        '</div>'
    )

    # 来源明细(详情 · 折叠到“查看证据来源”· 手机首屏不堆字)
    sources = plain.get("sources") or []
    if sources:
        rows = "".join(
            f'<tr><td style="padding:6px 8px;border-bottom:1px solid #EEE;">{_esc(s.get("name", ""))}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #EEE;">{_esc(s.get("type", ""))}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #EEE;">{_esc(s.get("situation", ""))}</td></tr>'
            for s in sources
        )
        parts.append(
            '<details style="margin:8px 0;"><summary style="cursor:pointer;font-size:14px;color:#475569;">'
            '查看证据来源</summary>'
            '<table style="width:100%;border-collapse:collapse;margin-top:8px;font-size:13px;">'
            '<thead><tr>'
            '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #E2E8F0;">来源</th>'
            '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #E2E8F0;">类型</th>'
            '<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #E2E8F0;">来源情况</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></details>'
        )

    if note:
        parts.append(f'<p style="font-size:12px;color:#94A3B8;margin-top:10px;">{note}</p>')
    return "".join(parts)


def _render_authority_page(modules: dict) -> str:
    """普通 v2 / PDF(/report-v2.pdf)路径的权威背书区。无 3_authority → ''。"""
    inner = _render_authority_inner_html((modules or {}).get("3_authority"))
    if not inner:
        return ""
    return (
        '<section class="report-section" style="margin-top:28px;padding:0 4px;">'
        '<h2 style="font-size:18px;margin-bottom:6px;">权威背书情况</h2>'
        '<p style="color:#64748B;font-size:13px;margin-bottom:10px;">'
        'AI 现在引用的来源里,有多少是权威媒体、百科,有多少只是普通网站</p>'
        + inner + '</section>'
    )


def render_report_html(
    *,
    meta: dict,
    modules_jsonb: dict,
    completeness: dict,
    audience: str = "client",
    branding: dict | None = None,
) -> str:
    """渲染完整 v2 咨询报告 HTML(网页 + PDF 共用)

    Args:
        meta: {brand_name, total_score, level, industry, city, created_at, audience}
        modules_jsonb: diagnosis_records.report_v2_modules_jsonb 反序列化值 · 含 internal+client
        completeness: services.report_metrics.compute_data_completeness 输出
        audience: 'client'(默认 · 公开页/客户门户)/ 'internal'(代理审)
        branding: v3.6 白标 brand dict(来自 resolve_branding_context(surface='customer'))·
            None → 平台默认品牌(向后兼容)。封面/CTA/页脚品牌位走此字段。

    Returns:
        完整 HTML 字符串(含 doctype + style + 内容)
    """
    audience = (audience or "client").lower()
    audience_key = "client" if audience == "client" else "internal"
    if audience_key == "client":
        if not is_client_report_ready(modules_jsonb):
            return render_client_report_not_ready_html(meta)
        modules = get_client_report_modules(modules_jsonb)
    else:
        side = (modules_jsonb or {}).get("internal") or {}
        modules = side.get("modules") or {}
    if not modules:
        # Internal tools may use the customer report when the internal edition
        # is absent. The reverse direction is a data disclosure.
        other = (modules_jsonb or {}).get("client") or {}
        modules = other.get("modules") or {}

    # 给 meta 补 audience(封面页用)
    meta = dict(meta or {})
    meta["audience"] = audience_key
    modules = _with_canonical_score(modules, meta)

    cover = _render_cover(meta, modules, completeness, branding)
    summary = _render_executive_summary(modules, audience_key)
    score_page = _render_score_page(modules, audience_key)
    evidence_page = _render_evidence_page(modules)
    raw_ai_page = _render_raw_ai_appendix_page(modules)
    authority_page = _render_authority_page(modules)  # SAP · 权威背书(3_authority)· 无则空串
    competitor_page = _render_competitor_page(modules)
    actions_page = _render_actions_page(modules, audience_key)
    offer_page = _render_offer_page(modules, audience_key, branding)
    raw_module = modules.get("3_raw") or {}
    engine_labels = _derive_engine_labels_v2(
        raw_module.get("tests") or [],
        raw_module.get("custom_tests") or [],
    )
    engine_scope_text = (
        f"本次测试：{'、'.join(engine_labels)}"
        if engine_labels else "本次 AI 实测"
    )
    appendix = _render_appendix(
        meta,
        completeness,
        audience_key,
        branding,
        engine_scope_text=engine_scope_text,
    )

    # 舆情诊断仅 internal 视角(代理端)显示 · client 视角返回空字符串
    sentiment = (modules_jsonb or {}).get("sentiment") or {}
    sentiment_page = _render_sentiment_page(sentiment, audience_key, meta.get("brand_name", ""))

    title = f"{_esc(meta.get('brand_name', '品牌'))} · {_esc(meta.get('level') or '数据不足')} · GEO 诊断报告 2.0"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=2">
  <title>{title}</title>
  <style>{_BASE_CSS}{SAFE_MARKDOWN_CSS}</style>
</head>
<body>
  <main class="report">
    {cover}
    {summary}
    {score_page}
    {evidence_page}
    {raw_ai_page}
    {authority_page}
    {sentiment_page}
    {competitor_page}
    {actions_page}
    {offer_page}
    {appendix}
  </main>
</body>
</html>"""


# ============================================================================
# PR-A · 客户决策页 v2 (CTO-15.23 2026-05-03)
#   定位: HTML 是客户决策页(不是普通 report viewer)· PDF 是离线汇报件 ·
#         两者共用同一份 modules_jsonb · 不再两边各写文案
#   设计: 5 重点 - 首屏专业感 / 证据可信度 / CTA 转化 / 操作者通知闭环 / 快照版本管理
#   feature flag: settings.customer_decision_v2_enabled (默认 OFF)
# ============================================================================

import re as _re
import math as _math


def _soften_promise_text(text: str) -> str:
    """v1.1.1 P0.5-1 · 安全包装 modules_jsonb LLM 输出 · 删硬承诺词

    "X-X 天见效" → "建议观察窗口 X-X 天"
    "X 天见效" → "建议观察窗口 X 天"
    同样处理 "X 个月见效" / "快速见效" / "立即见效"
    保护客户首屏不出现硬承诺
    """
    if not text:
        return text
    text = _re.sub(r'(\d+\s*[-~–]\s*\d+\s*天)\s*见效', r'建议观察窗口 \1', text)
    text = _re.sub(r'(\d+\s*天)\s*见效', r'建议观察窗口 \1', text)
    text = _re.sub(r'(\d+\s*[-~–]\s*\d+\s*个月)\s*见效', r'建议观察窗口 \1', text)
    text = _re.sub(r'(\d+\s*个月)\s*见效', r'建议观察窗口 \1', text)
    text = text.replace('快速见效', '建议观察')
    text = text.replace('立即见效', '建议观察')
    return text


def _gen_safe_summary(ev: dict) -> str:
    """v1.1.1 P0.5-2 · GEO-only 安全摘要 · 不嵌 keyword

    Codex 反馈:默认摘要不能露 "深圳社媒搜索优化公司靠谱吗" 等社媒原始 query
    keyword 进折叠区受 disclaimer 保护
    """
    level = ev.get('evidence_level', 'C')
    src = ev.get('source_label', '未知')
    mention_type = ev.get('mention_type', '')

    # [P0-3 · 2026-07-26] 词表变更：direct 已归一为 mentioned，新库还会出现
    #   recommended。这里按"品牌确实出现"判定，避免新报告掉进泛化文案。
    from services.mention_vocabulary import (
        MENTION_RECOMMENDED,
        is_brand_present,
        normalize_mention_type,
    )

    if level == 'A' and normalize_mention_type(mention_type) == MENTION_RECOMMENDED:
        return f'{src} 在相关获客/GEO 查询中,把您的品牌列入推荐/候选并给出描述。'
    if level == 'A' and is_brand_present(mention_type):
        return f'{src} 在相关获客/GEO 查询中,直接提及您的品牌并给出描述。'
    elif level == 'A':
        return f'{src} 在相关 AI 查询中,提及您的品牌并展开介绍。'
    elif level == 'B':
        return f'{src} 在相关 AI 查询中,有间接相关引用。'
    else:
        return f'{src} 在相关 AI 查询中,有推断性相关信号。'


def _radar_chart_svg(dims: list, size: int = 320) -> str:
    """本次诊断维度雷达图；不承载尚无版本化数据源的行业对比层。"""
    if not dims:
        return ''
    cx = cy = size / 2
    radius = size / 2 - 48
    n = len(dims)
    angles = [-_math.pi / 2 + 2 * _math.pi * i / n for i in range(n)]

    def pt(pct: float, i: int) -> tuple[float, float]:
        return (cx + radius * pct * _math.cos(angles[i]),
                cy + radius * pct * _math.sin(angles[i]))

    def poly(pcts: list) -> str:
        return ' '.join(f'{x:.1f},{y:.1f}' for x, y in (pt(p, i) for i, p in enumerate(pcts)))

    you_pcts = [(d.get('percent', 0) or 0) / 100 for d in dims]

    rings = ''
    for r in (0.25, 0.5, 0.75, 1.0):
        rings += f'<polygon points="{poly([r]*n)}" fill="none" stroke="rgba(15,23,42,0.06)" stroke-width="1"/>'
    axes = ''
    for i in range(n):
        x, y = pt(1, i)
        axes += f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="rgba(15,23,42,0.04)" stroke-width="1"/>'

    you_layer = f'<polygon points="{poly(you_pcts)}" fill="rgba(59,130,246,0.20)" stroke="#3B82F6" stroke-width="2.2" stroke-linejoin="round"/>'
    you_dots = ''
    for i, p in enumerate(you_pcts):
        x, y = pt(p, i)
        you_dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#3B82F6"/>'

    labels = ''
    for i, d in enumerate(dims):
        a = angles[i]
        lx = cx + (radius + 24) * _math.cos(a)
        ly = cy + (radius + 24) * _math.sin(a)
        anchor = 'middle'
        if _math.cos(a) > 0.3:
            anchor = 'start'
        elif _math.cos(a) < -0.3:
            anchor = 'end'
        score = d.get('score', 0) or 0
        max_v = d.get('max', 0) or 0
        labels += (
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" fill="#475569" font-size="11" font-weight="600">{_esc(d.get("label",""))}</text>'
            f'<text x="{lx:.1f}" y="{ly+13:.1f}" text-anchor="{anchor}" fill="#3B82F6" font-size="10" font-weight="700">{score}/{max_v}</text>'
        )
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg">'
        f'{rings}{axes}{you_layer}{you_dots}{labels}</svg>'
    )


def _render_evidence_card_v2(ev: dict) -> str:
    """v1.1.1 客户决策页证据卡 · 安全摘要默认 + 原文 + keyword 折叠"""
    level = ev.get('evidence_level', 'C')
    level_color = {'A': '#10B981', 'B': '#F59E0B', 'C': '#94A3B8'}.get(level, '#94A3B8')
    level_label = {'A': '直接证据', 'B': '参考证据', 'C': '待验证线索'}.get(level, '待验证线索')
    src = _esc(ev.get('source_label', '未知'))
    keyword = _esc(ev.get('keyword', ''))
    snippet_raw = ev.get('response_snippet', '') or ''
    snippet = _esc(snippet_raw[:280] + ('…' if len(snippet_raw) > 280 else ''))
    safe_summary = _esc(_gen_safe_summary(ev))

    citations = ev.get('search_citations', []) or []
    cite_html = ''
    if citations:
        items = []
        for c in citations[:3]:
            title = _esc((c.get('title', '') or '')[:40])
            safe_url = _normalize_public_evidence_url(c.get('url'))
            if safe_url:
                items.append(
                    f'<a href="{_esc(safe_url)}" target="_blank" rel="noopener noreferrer" class="cdv2-ev-cite">'
                    f'<span class="cdv2-ev-cite-dot"></span>{title}</a>'
                )
            else:
                items.append(
                    f'<span class="cdv2-ev-cite cdv2-ev-cite-disabled">'
                    f'<span class="cdv2-ev-cite-dot"></span>{title}</span>'
                )
        cite_html = f'<div class="cdv2-ev-citations">{"".join(items)}</div>'

    return f'''
    <div class="cdv2-ev-card">
      <div class="cdv2-ev-head">
        <span class="cdv2-ev-level" style="background:{level_color}1A;color:{level_color};border-color:{level_color}33;">{level} · {level_label}</span>
        <span class="cdv2-ev-src">{src}</span>
      </div>
      <div class="cdv2-ev-summary">{safe_summary}</div>
      <details class="cdv2-ev-details">
        <summary class="cdv2-ev-toggle">查看 AI 引擎测试问题 + 原始回答 <span class="cdv2-ev-arrow"></span></summary>
        <div class="cdv2-ev-keyword-detail">测试问题:「{keyword}」</div>
        <div class="cdv2-ev-snippet">{snippet}</div>
        {cite_html}
      </details>
    </div>'''


def _build_dim_rows_v2(dims: list) -> str:
    """5 维度详情 · 维度色点(无 emoji)+ 进度条 + 状态 + 解释"""
    DOT_COLOR = {
        'AI引擎推荐率': '#3B82F6', 'AI推荐率': '#3B82F6',
        '网页内容资产': '#06B6D4', '网页内容': '#06B6D4',
        '权威背书': '#F59E0B',
        '结构化内容': '#8B5CF6',
        '品牌基础': '#10B981',
    }
    out = ''
    for d in dims or []:
        label = d.get('label', '')
        dot = DOT_COLOR.get(label, '#3B82F6')
        score = d.get('score', 0) or 0
        max_v = d.get('max', 0) or 0
        pct = d.get('percent', 0) or 0
        status = _esc(d.get('status', ''))
        expl = _esc(d.get('explanation', ''))
        out += f'''
        <div class="cdv2-dim-row">
          <div class="cdv2-dim-head">
            <span class="cdv2-dim-dot" style="background:{dot};"></span>
            <span class="cdv2-dim-label">{_esc(label)}</span>
            <span class="cdv2-dim-status" data-status="{status}">{status}</span>
            <span class="cdv2-dim-score">{score}<span class="cdv2-dim-max">/{max_v}</span></span>
          </div>
          <div class="cdv2-dim-bar">
            <div class="cdv2-dim-fill" style="width:{pct}%;background:linear-gradient(90deg,{dot},{dot}99);"></div>
          </div>
          <div class="cdv2-dim-explanation">{expl}</div>
        </div>'''
    return out


def _nonnegative_int(value) -> int | None:
    """严格读取计数，拒绝 bool、负数、非整数浮点和带杂字符的字符串。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        return int(normalized) if normalized.isdigit() else None
    return None


def _build_competitor_rows_v2(competitor_module: dict, total_evidence: int | None) -> str:
    """竞品同频引用源 · TOP 5 · 兼容 [name,count] / dict 两种 v2 schema"""
    raw = (competitor_module or {}).get('top_competitors') or []
    denominator_available = isinstance(total_evidence, int) and total_evidence > 0
    rows = ''
    for idx, c in enumerate(raw[:5], 1):
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            name, count = c[0], c[1]
        elif isinstance(c, dict):
            name, count = c.get('name', ''), c.get('count', 0)
        else:
            continue
        name_text = str(name or '').strip()
        if not name_text:
            continue
        name = _esc(name_text)
        count_n = _nonnegative_int(count)
        ratio_available = (
            denominator_available
            and count_n is not None
            and count_n <= total_evidence
        )
        if ratio_available:
            pct = round(count_n / total_evidence * 100)
            freq = '高频引用' if pct >= 30 else ('中频引用' if pct >= 15 else '零星出现')
            count_display = f'{count_n} / {total_evidence}'
        elif count_n is not None:
            freq = '数据不足'
            count_display = f'{count_n} 次'
        else:
            freq = '数据不足'
            count_display = '数据不足'
        rows += f'''
        <tr>
          <td class="cdv2-comp-rank">#{idx}</td>
          <td class="cdv2-comp-name" title="{name}">{name}</td>
          <td class="cdv2-comp-cnt">{count_display}</td>
          <td class="cdv2-comp-freq">{freq}</td>
        </tr>'''
    return rows or '<tr><td colspan="4" class="cdv2-comp-empty">本次没有足够数据形成同频对照</td></tr>'


def _derive_engine_labels_v2(*test_groups: list) -> list[str]:
    """从本次真实问答结果派生产品名，保持首次出现顺序并去重。"""
    labels: list[str] = []
    seen: set[str] = set()
    for tests in test_groups:
        for test in tests or []:
            if not isinstance(test, dict):
                continue
            for result in test.get('results') or []:
                if not isinstance(result, dict):
                    continue
                label = _neutral_engine(
                    result.get('engine'),
                    result.get('engine_label') or result.get('engine') or '',
                ).strip()
                if not label or label in seen:
                    continue
                seen.add(label)
                labels.append(label)
    return labels


# 客户决策页 CSS · 完全独立命名空间(cdv2-) · 不污染 _BASE_CSS
# ============================================================
# Phase 08 (CTO-15.23 2026-05-04) · v2 决策页 redesign helpers
# 替代 5 维资产健康度雷达(技术指标) → 3 层漏斗(业务视角 · 客户更易懂)
# ============================================================

def _layer_color_class(key: str, idx: int) -> str:
    """根据 layer key/index 决定颜色类"""
    if key == 'brand' or idx == 0:
        return 'cdv2-funnel-good'  # 品牌认知层 · 通常表现好
    if key == 'local' or idx == 1:
        return 'cdv2-funnel-warn'  # 决策获客层 · 通常待提升
    return 'cdv2-funnel-bad'  # 场景转化层 · 通常空白


def _layer_status_text(rate_pct: float) -> tuple:
    """rate_pct → (status_class, status_text)"""
    if rate_pct >= 50:
        return ('good', '✓ 守住底线')
    if rate_pct >= 10:
        return ('warn', '⚠ 部分流失')
    return ('bad', '✕ 几乎完全断')


def _build_funnel_layers_v2(layers: list) -> str:
    """3 层漏斗 section HTML(Phase 08)

    输入 layers · 输出 HTML 字符串(每层一卡片 · 含命中比 / 进度条 / 状态)
    """
    if not layers:
        return ''
    rows = []
    for idx, layer in enumerate(layers[:3]):  # 最多 3 层
        key = layer.get('key', '')
        label = _esc(layer.get('label') or '')
        weight = int(layer.get('weight') or 0)
        detected = int(layer.get('detected') or 0)
        total = int(layer.get('total') or 0)
        rate_pct = float(layer.get('rate_pct') or 0)
        score = float(layer.get('score') or 0)
        desc = _esc(layer.get('desc') or '')
        business = _esc(layer.get('business') or '')
        # [audit #7 返修] 分母用 effective_weight(重归一真实上限);空样本层显示"无样本"
        #   (原 score/weight 用原始权重 → 重归一后出现 "100.0 / 20" 类矛盾数)。
        _eff_v2 = layer.get('effective_weight')
        if total <= 0:
            _score_block = '<span class="cdv2-funnel-score-num">无样本</span>'
        else:
            _denom_v2 = _eff_v2 if _eff_v2 else weight
            _score_block = (
                f'<span class="cdv2-funnel-score-num">{score:.1f}</span>'
                f'<span class="cdv2-funnel-score-max">/ {_denom_v2}</span>'
            )

        color_class = _layer_color_class(key, idx)
        status_class, status_text = _layer_status_text(rate_pct)
        bar_width = max(0, min(100, rate_pct))

        rows.append(f"""
        <div class="cdv2-funnel-card {color_class}">
          <div class="cdv2-funnel-head">
            <div>
              <div class="cdv2-funnel-name">
                <span class="cdv2-funnel-num">{idx + 1}</span>
                <span class="cdv2-funnel-label">{label}</span>
                <span class="cdv2-funnel-status cdv2-funnel-status-{status_class}">{status_text}</span>
              </div>
              <div class="cdv2-funnel-weight">权重 {weight}% · {desc}</div>
            </div>
            <div class="cdv2-funnel-score">
              {_score_block}
            </div>
          </div>
          <div class="cdv2-funnel-bar"><div class="cdv2-funnel-bar-fill" style="width:{bar_width:.1f}%"></div></div>
          <div class="cdv2-funnel-meta">
            <span><b>命中</b> {detected}/{total} 题</span>
            <span><b>覆盖</b> {rate_pct:.1f}%</span>
            {f'<span class="cdv2-funnel-business">{business}</span>' if business else ''}
          </div>
        </div>
        """)
    return ''.join(rows)


def _build_four_things_v2(layers: list) -> str:
    """4 件事表格 · 客户阅读入口(Phase 08)

    从 funnel layers 派生 3 行 + 第 4 行通用核对提示。
    """
    if not layers:
        return ''
    questions = [
        ('AI 认识我吗?', '老客户回头搜你时能不能找到你', 0),
        ('新客户会被 AI 推荐给我吗?', '客户搜"哪家好 / 推荐 / 靠谱"时会不会流向竞品', 1),
        ('高转化问题里有没有我?', '客户搜"怎么做 / 对比 / 避坑"时能不能进入候选', 2),
    ]
    cards = []
    for q, impact, layer_idx in questions:
        if layer_idx >= len(layers):
            continue
        layer = layers[layer_idx]
        detected = int(layer.get('detected') or 0)
        total = int(layer.get('total') or 0)
        rate_pct = float(layer.get('rate_pct') or 0)

        if rate_pct >= 50:
            card_cls = 'cdv2-thing-good'
            answer = f'认识 · {detected}/{total} 命中' if layer_idx == 0 else f'多数推荐 · {detected}/{total} 命中'
            answer_color = 'cdv2-thing-text-good'
        elif rate_pct >= 10:
            card_cls = 'cdv2-thing-warn'
            answer = f'多数不会推荐 · {detected}/{total} 命中'
            answer_color = 'cdv2-thing-text-warn'
        else:
            card_cls = 'cdv2-thing-bad'
            answer = f'基本没有覆盖 · {detected}/{total} 命中' if layer_idx == 2 else f'多数不会推荐 · {detected}/{total} 命中'
            answer_color = 'cdv2-thing-text-bad'

        cards.append(f"""
        <div class="cdv2-thing-card {card_cls}">
          <div class="cdv2-thing-q">{_esc(q)}</div>
          <div class="cdv2-thing-a">
            <span class="{answer_color}">{_esc(answer)}</span>
          </div>
          <div class="cdv2-thing-impact"><b>对生意的影响:</b> {_esc(impact)}</div>
        </div>
        """)

    cards.append("""
        <div class="cdv2-thing-card cdv2-thing-neutral">
          <div class="cdv2-thing-q">优先核对什么?</div>
          <div class="cdv2-thing-a">原始回答 · 可引用来源 · 复测口径</div>
          <div class="cdv2-thing-impact"><b>阅读提示:</b> 把结论落到证据 · 不把单次分数当承诺</div>
        </div>
    """)

    return ''.join(cards)


def _build_business_translate_v2(layers: list) -> str:
    """业务语言翻译 · 底线 / 核心 / 天花板(Phase 08)"""
    if not layers or len(layers) < 3:
        return ''
    tiers = [
        ('底线', '守得住老客回头', 'AI 在品牌认知层能稳定提到你 · 老客户搜你公司名时能找到你'),
        ('核心', '新客做品牌选择时', 'AI 几乎不替你说话 · 客户搜"哪家好 / 推荐 / 靠谱"时进不到候选'),
        ('天花板', '高客单决策客户', 'AI 几乎不提你 · 客户问"对比 / 方案 / 避坑"时拿不到候选'),
    ]
    cards = []
    for idx, (tier, name, desc) in enumerate(tiers):
        layer = layers[idx]
        rate_pct = float(layer.get('rate_pct') or 0)
        if rate_pct >= 50:
            icon_cls = 'cdv2-translate-good'
            verdict_cls = 'cdv2-translate-verdict-good'
            verdict_text = '✅ 这一层稳'
            icon = '✓'
        elif rate_pct >= 10:
            icon_cls = 'cdv2-translate-warn'
            verdict_cls = 'cdv2-translate-verdict-warn'
            verdict_text = '⚠ 这一层勉强'
            icon = '!'
        else:
            icon_cls = 'cdv2-translate-bad'
            verdict_cls = 'cdv2-translate-verdict-bad'
            verdict_text = '❌ 客户在这一层流失'
            icon = '✕'

        cards.append(f"""
        <div class="cdv2-translate-card">
          <div class="cdv2-translate-icon {icon_cls}">{icon}</div>
          <div class="cdv2-translate-tier">{tier}</div>
          <div class="cdv2-translate-name">{name}</div>
          <div class="cdv2-translate-desc">{desc}</div>
          <div class="cdv2-translate-verdict {verdict_cls}">{verdict_text}</div>
        </div>
        """)
    return ''.join(cards)


def _render_interpretation_card_v2(module: dict | None) -> str:
    """客户决策页的报告解读卡 · 只渲染客观解读,不放行动按钮。"""
    module = module or {}
    headline = _esc(module.get("headline") or "先看漏斗,再看证据,最后看数据边界。")
    points = module.get("reading_points") or [
        "先看三层漏斗:品牌认知、决策获客、场景转化分别回答不同业务问题。",
        "再看原始 AI 回答:关键不只是得分,而是 AI 有没有自然提到品牌、竞品和来源。",
        "最后看数据边界:证据不足处只做保守判断,不把趋势当成确定结论。",
    ]
    business = _esc(module.get("business_translation") or "这份报告适合用来核对 AI 当前如何理解品牌,不单独作为执行承诺。")
    caveat = _esc(module.get("data_caveat") or "本报告代表本次采样窗口的客观结果,AI 回答会随模型和实时检索变化。")

    point_html = "".join(
        f'<li>{_esc(point)}</li>'
        for point in points
        if isinstance(point, str) and point.strip()
    )
    if not (headline or point_html or business or caveat):
        return ""

    return f"""
<section class="cdv2-section cdv2-interpretation-section" style="background:#fff;">
  <div class="cdv2-container">
    <div class="cdv2-sec-eyebrow">报告解读</div>
    <h2 class="cdv2-sec-title">这份报告应该怎么读</h2>
    <div class="cdv2-interpretation-card">
      {f'<div class="cdv2-interpretation-headline">{headline}</div>' if headline else ''}
      {f'<ul class="cdv2-interpretation-list">{point_html}</ul>' if point_html else ''}
      {f'<div class="cdv2-interpretation-translation"><b>业务翻译:</b>{business}</div>' if business else ''}
      {f'<div class="cdv2-interpretation-caveat"><b>数据边界:</b>{caveat}</div>' if caveat else ''}
    </div>
  </div>
</section>
"""


def _render_interpretation_page_plain(modules: dict) -> str:
    """普通 v2/PDF 客户路径的报告解读页。"""
    module = (modules or {}).get("1_interpretation") or {}
    outline_md = ((modules or {}).get("7") or {}).get("rendered_md") or ""

    # 兼容旧报告快照:历史 module 7 可能仍是 30 天计划/销售 CTA。
    # 客户路径即使读取旧 JSONB,也不能把旧计划藏进"解读提纲"里。
    legacy_sales_terms = (
        "30 天", "30天", "30 / 60 / 90", "ROI", "当前预算", "预算建议",
        "月费", "投入测算", "预约 GEO", "规划师", "行动优先级", "验收标准",
        "执行计划",
    )
    if outline_md and any(term in outline_md for term in legacy_sales_terms):
        outline_md = ""

    headline = _esc(module.get("headline") or "先看漏斗,再看证据,最后看数据边界。")
    points = module.get("reading_points") or [
        "先看三层漏斗:品牌认知、决策获客、场景转化分别回答不同业务问题。",
        "再看原始 AI 回答:关键不只是得分,而是 AI 有没有自然提到品牌、竞品和来源。",
        "最后看数据边界:证据不足处只做保守判断,不把趋势当成确定结论。",
    ]
    point_html = "".join(
        f"<li>{_esc(point)}</li>"
        for point in points
        if isinstance(point, str) and point.strip()
    )
    business = _esc(module.get("business_translation") or "")
    caveat = _esc(module.get("data_caveat") or "")
    outline_html = ""
    if outline_md:
        outline_html = (
            '<details style="margin-top:18px;">'
            '<summary class="muted" style="cursor:pointer;">查看完整解读提纲</summary>'
            f'<div class="safe-markdown" style="font-size:13px;color:#374151;">{_md_lite_v2(outline_md)}</div>'
            '</details>'
        )

    return f"""
    <section class="page">
      <h2>报告解读</h2>
      <h3>这份报告应该怎么读</h3>
      <p><strong>{headline}</strong></p>
      {f'<ul>{point_html}</ul>' if point_html else ''}
      {f'<p><strong>业务翻译:</strong>{business}</p>' if business else ''}
      {f'<p class="muted"><strong>数据边界:</strong>{caveat}</p>' if caveat else ''}
      {outline_html}
    </section>
    """


_CLIENT_TAIL_LEGACY_TERMS = (
    "30 天", "30天", "30 / 60 / 90", "ROI", "当前预算", "预算建议",
    "月费", "投入测算", "预约 GEO", "规划师", "行动优先级", "验收标准",
    "执行计划", "方案承接", "积分", "报价", "客户版(精简)",
)


def _client_tail_md_is_safe(markdown: str) -> bool:
    """客户分享页 05-08 模块安全阀。

    历史快照里 5-8 曾包含销售承诺/报价/积分等表达;公开分享页只能展示
    当前中性解读口径,旧快照命中遗留词时改用兜底说明。
    """
    return bool(markdown and not any(term in markdown for term in _CLIENT_TAIL_LEGACY_TERMS))


def _strip_first_markdown_heading(markdown: str) -> str:
    if not markdown:
        return ""
    return _re.sub(r"^\s*#{1,3}\s+.*(?:\n|$)", "", markdown, count=1).strip()


def _tail_fallback_opportunity(module: dict) -> str:
    parts = [
        "## 机会解读",
        "",
        "当前差距只说明 AI 可见度和证据覆盖还有改善空间,不代表固定收益或执行承诺。",
    ]
    gap_to_p50 = module.get("gap_to_p50")
    gap_to_p90 = module.get("gap_to_p90")
    lift_low = module.get("est_aiq_lift_pct_low")
    lift_high = module.get("est_aiq_lift_pct_high")
    if gap_to_p50 is not None:
        parts.append(f"- 距行业中位参考差距:{gap_to_p50} 分")
    if gap_to_p90 is not None:
        parts.append(f"- 距头部参考差距:{gap_to_p90} 分")
    if lift_low is not None and lift_high is not None:
        parts.append(f"- 可见度改善参考区间:{lift_low}%-{lift_high}%")
    return "\n".join(parts)


def _tail_fallback_observation() -> str:
    return (
        "## 报告观察清单\n\n"
        "- 优先核对 AI 原始回答里没有稳定提到品牌的问题。\n"
        "- 对照竞品被提及时的来源、理由和上下文。\n"
        "- 对样本不足或证据等级偏低的部分保持保守判断。"
    )


def _tail_fallback_outline() -> str:
    return (
        "## 报告解读提纲\n\n"
        "- 先看总分和三层漏斗,判断问题集中在哪一层。\n"
        "- 再看 AI 原始回答和证据等级,确认结论来源。\n"
        "- 最后看数据边界,区分已验证事实和待验证判断。"
    )


def _tail_fallback_limitations() -> str:
    return (
        "## 报告局限说明\n\n"
        "本报告代表本次采样窗口的客观结果。AI 回答会随模型、检索和公开资料变化,"
        "适合逐项解读,不单独作为执行承诺。"
    )


def _render_customer_tail_card(label: str, title: str, markdown: str, fallback_markdown: str) -> str:
    source_md = _soften_promise_text(markdown or "")
    if not _client_tail_md_is_safe(source_md):
        source_md = fallback_markdown
    body_md = _strip_first_markdown_heading(source_md)
    body_html = _md_lite_v2(body_md) if body_md else ""
    if not body_html:
        return ""
    return f"""
      <article class="cdv2-tail-card">
        <div class="cdv2-tail-label">{_esc(label)}</div>
        <h3 class="cdv2-tail-title">{_esc(title)}</h3>
        <div class="cdv2-tail-body safe-markdown">{body_html}</div>
      </article>
    """


def _render_customer_tail_sections_v2(modules: dict) -> str:
    """客户决策页补齐 05-08 后半段。

    数据来自同一份 modules_jsonb;不是另起一份短报告。旧快照命中遗留销售词时,
    只展示中性兜底解读,避免把历史 ROI/计划/报价口径带到客户分享页。
    """
    modules = modules or {}
    module5 = modules.get("5") or {}
    cards = [
        _render_customer_tail_card(
            "05 · 机会解读",
            "差距意味着什么",
            module5.get("rendered_md") or "",
            _tail_fallback_opportunity(module5),
        ),
        _render_customer_tail_card(
            "06 · 报告观察清单",
            "优先核对什么",
            ((modules.get("6") or {}).get("rendered_md") or ""),
            _tail_fallback_observation(),
        ),
        _render_customer_tail_card(
            "07 · 报告解读提纲",
            "怎么逐项读这份报告",
            ((modules.get("7") or {}).get("rendered_md") or ""),
            _tail_fallback_outline(),
        ),
        _render_customer_tail_card(
            "08 · 报告局限说明",
            "哪些结论需要保守理解",
            ((modules.get("8") or {}).get("rendered_md") or ""),
            _tail_fallback_limitations(),
        ),
    ]
    cards_html = "".join(card for card in cards if card)
    if not cards_html:
        return ""
    return f"""
<section class="cdv2-section cdv2-tail-section" style="background:#fff;">
  <div class="cdv2-container">
    <div class="cdv2-sec-eyebrow">05-08 · 深度解读</div>
    <h2 class="cdv2-sec-title">报告后半段解读</h2>
    <p class="cdv2-sec-sub">这里补充机会、观察清单、解读顺序和数据边界 · 用于客观理解报告,不构成执行承诺</p>
    <div class="cdv2-tail-grid">{cards_html}</div>
  </div>
</section>
"""


def _md_lite_v2(text: str) -> str:
    """向后兼容别名：统一走 canonical safe Markdown SSOT。

    2026-07-22 sink census B1/B2：正则假渲染（吞表格/压平换行/`|`→`·` 降级）
    已删除，语义与安全策略由 services.safe_markdown_renderer 承担。
    """

    return render_safe_markdown(text)


# [2026-06-06 老板订正 · 反转 WJ-08 匿名] 模型名是给客户看诊断的核心要素 ——
# 客户就是来看「我在通义千问/DeepSeek/豆包/元宝里长什么样」,匿名成 ABCD 背离初心。
# 故客户面【显示被诊断的 AI 搜索产品真名】；历史报告中的 Kimi 仍按真实名称保留。
# 仍要隐藏的是【内部基础设施/数据 API 平台】(DashScope 平台名/metaso/tikhub/5118)——那些在数据源头已用产品名,不进本表。
_VENDOR_NEUTRAL = [
    ('深度求索', 'DeepSeek'), ('deepseek', 'DeepSeek'),
    ('tencent_tokenhub', '元宝'), ('hunyuan', '元宝'), ('hy3', '元宝'), ('yuanbao', '元宝'), ('元宝', '元宝'),
    ('moonshot', 'Kimi'), ('kimi', 'Kimi'),
    ('doubao', '豆包'), ('豆包', '豆包'),
    # dashscope 是调用通义千问的 API 平台名(要隐藏)→ 统一显示产品名「通义千问」
    ('dashscope', '通义千问'), ('通义', '通义千问'), ('千问', '通义千问'), ('qwen', '通义千问'),
    ('秘塔代理', 'AI 搜索引擎'), ('metaso', 'AI 搜索引擎'),
]


def _neutral_engine(engine_key: str, engine_label: str) -> str:
    """供应商/平台引擎名 → 被诊断的 AI 搜索产品真名；历史 Kimi 不改名。

    [2026-06-06 反转 WJ-08] 客户面显示真实搜索引擎产品名(诊断核心要素),不再匿名 ABCD。
    """
    hay = f"{engine_key or ''} {engine_label or ''}".lower()
    for vendor, neutral in _VENDOR_NEUTRAL:
        if vendor.lower() in hay:
            return neutral
    return engine_label or 'AI 搜索引擎'


def _scrub_vendor(text: str) -> str:
    """正文残留供应商名脱敏(AI 自述/互提)· 大小写不敏感 · 客户面净区"""
    if not text:
        return text
    import re as _re
    for vendor, neutral in _VENDOR_NEUTRAL:
        text = _re.sub(_re.escape(vendor), neutral, text, flags=_re.IGNORECASE)
    return text


def _client_level_name(level: str, audience: str = "client") -> str:
    """保留评分 SSOT 的真实等级；audience 参数仅为向后兼容。"""
    del audience
    return level or ""


def _build_raw_tests_summary_v2(tests: list) -> str:
    """8 题 4 引擎原文 · 默认折叠(Phase 08)

    从 modules_jsonb.client.modules.3_raw.tests 抽取
    """
    if not tests:
        return ''
    items = []
    for idx, test in enumerate(tests):
        question = _esc(test.get('question') or test.get('question_text') or f'问题 {idx + 1}')
        tier = _esc(test.get('tier') or test.get('layer_label') or '')
        results = test.get('results') or []
        hits = sum(1 for r in results if r.get('brand_detected'))
        total = len(results)
        if hits == total and total > 0:
            hit_cls = 'full'
        elif hits > 0:
            hit_cls = 'partial'
        else:
            hit_cls = 'miss'

        engine_blocks = []
        for r in results:
            engine_label = _esc(_neutral_engine(r.get('engine'), r.get('engine_label') or r.get('engine') or '未知引擎'))
            engine_key = (r.get('engine') or '').lower()
            # WJ-08 · class 名中性化(a/b/c/d 对齐 AI 引擎 A/B/C/D · grep 不到厂商名)·
            # engine_label 已脱敏,改用原始 engine_key 判断
            engine_cls = 'd' if 'qwen' in engine_key or '通义' in engine_key else \
                        'a' if 'deepseek' in engine_key else \
                        'b' if 'yuanbao' in engine_key or '元宝' in engine_key else \
                        'b' if 'kimi' in engine_key else \
                        'c' if 'doubao' in engine_key or '豆包' in engine_key else 'default'
            detected = bool(r.get('brand_detected'))
            status_text = '✓ 提到品牌' if detected else '✕ 未提到品牌'
            status_cls = 'cdv2-engine-hit' if detected else 'cdv2-engine-miss'
            brands = r.get('mentioned_brands') or []
            brands_text = _esc('、'.join(str(x) for x in brands[:6])) if brands else ''
            answer = _scrub_vendor(r.get('full_response') or r.get('answer') or '本引擎未返回可展示回答')

            engine_blocks.append(f"""
            <div class="cdv2-engine-block">
              <div class="cdv2-engine-head">
                <span class="cdv2-engine-name cdv2-engine-{engine_cls}">{engine_label}</span>
                <span class="cdv2-engine-status {status_cls}">{status_text}</span>
                {f'<span class="cdv2-engine-mention">提到:{brands_text}</span>' if brands_text else ''}
              </div>
              <div class="cdv2-engine-body safe-markdown">{_md_lite_v2(answer)}</div>
            </div>
            """)

        items.append(f"""
        <details class="cdv2-test-q">
          <summary>
            <span class="cdv2-test-q-num">Q{idx + 1}</span>
            <span class="cdv2-test-q-text">{question}</span>
            {f'<span class="cdv2-test-q-tier">{tier}</span>' if tier else ''}
            <span class="cdv2-test-q-hit cdv2-test-q-hit-{hit_cls}">命中 {hits}/{total}</span>
          </summary>
          {''.join(engine_blocks)}
        </details>
        """)
    return ''.join(items)


_CUSTOMER_DECISION_CSS = """
*,*::before,*::after{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;background:#F8FAFC;color:#0F172A;line-height:1.6;-webkit-font-smoothing:antialiased;padding-bottom:64px;}
@media(min-width:768px){body{padding-bottom:0;}}
a{color:inherit;text-decoration:none;}
img,svg{display:block;max-width:100%;}
.cdv2-container{max-width:880px;margin:0 auto;padding:0 16px;}

/* Top bar */
.cdv2-topbar{position:sticky;top:0;z-index:50;background:rgba(255,255,255,0.92);backdrop-filter:saturate(180%) blur(12px);border-bottom:1px solid rgba(15,23,42,0.06);}
.cdv2-topbar-inner{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;max-width:880px;margin:0 auto;}
.cdv2-brand-mark{display:flex;align-items:center;gap:8px;font-weight:700;color:#0F172A;font-size:15px;}
/* Default brand mark: ::before 渐变方块 · 白标有 logo 时整组 cdv2-brand-mark-with-logo 关掉 ::before */
.cdv2-brand-mark::before{content:"";display:inline-block;width:18px;height:18px;border-radius:5px;background:linear-gradient(135deg,#3B82F6,#06B6D4);}
.cdv2-brand-mark.cdv2-brand-mark-with-logo::before{display:none;}
.cdv2-brand-logo-img{height:22px;width:auto;display:block;border-radius:4px;}
.cdv2-brand-mark-text{display:inline-block;}
.cdv2-snapshot-tag{font-size:10px;color:#94A3B8;font-family:Menlo,Monaco,monospace;}

/* HERO */
.cdv2-hero{padding:32px 16px 28px;background:linear-gradient(180deg,#FFFFFF 0%,#F1F5F9 100%);border-bottom:1px solid rgba(15,23,42,0.06);}
.cdv2-hero-grid{display:grid;grid-template-columns:1fr;gap:24px;align-items:center;}
@media(min-width:768px){.cdv2-hero-grid{grid-template-columns:1.1fr 0.9fr;gap:32px;}}
.cdv2-hero-meta{font-size:12px;color:#64748B;margin-bottom:6px;letter-spacing:0.06em;}
.cdv2-hero-name{font-size:22px;font-weight:800;color:#0F172A;line-height:1.25;margin:0 0 8px;letter-spacing:-0.01em;}
@media(min-width:768px){.cdv2-hero-name{font-size:28px;}}
.cdv2-hero-industry{font-size:13px;color:#475569;margin-bottom:18px;}
.cdv2-score-block{display:flex;align-items:baseline;gap:10px;margin-bottom:14px;}
.cdv2-score-num{font-family:"SF Pro Display",-apple-system,sans-serif;font-size:64px;font-weight:800;line-height:1;color:#0F172A;letter-spacing:-0.03em;}
@media(min-width:768px){.cdv2-score-num{font-size:88px;}}
.cdv2-score-max{font-size:24px;color:#94A3B8;font-weight:600;}
.cdv2-score-suffix{font-size:11px;color:#64748B;letter-spacing:0.08em;text-transform:uppercase;font-weight:600;}
.cdv2-level-badge{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:6px;background:#F59E0B1A;color:#B45309;font-size:13px;font-weight:700;border:1px solid #F59E0B33;}
.cdv2-level-badge::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:#F59E0B;}
.cdv2-level-summary{font-size:13px;color:#334155;margin-top:14px;line-height:1.6;max-width:42ch;}
.cdv2-hero-radar{display:flex;justify-content:center;align-items:center;}
.cdv2-hero-radar-desktop{display:none;}
.cdv2-radar-mobile-section{padding:24px 0;background:#fff;border-bottom:1px solid rgba(15,23,42,0.05);}
@media(min-width:768px){.cdv2-hero-radar-desktop{display:flex;}.cdv2-radar-mobile-section{display:none;}}

.cdv2-cta-row{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px;}
.cdv2-btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:14px 22px;border-radius:10px;font-size:15px;font-weight:700;border:0;cursor:pointer;transition:all 0.18s;font-family:inherit;}
.cdv2-btn-primary{background:linear-gradient(135deg,#3B82F6,#0891B2);color:#fff;box-shadow:0 4px 12px rgba(59,130,246,0.25);}
.cdv2-btn-primary:hover{box-shadow:0 6px 18px rgba(59,130,246,0.32);transform:translateY(-1px);}
.cdv2-btn-secondary{background:#fff;color:#0F172A;border:1px solid rgba(15,23,42,0.12);}
.cdv2-btn-secondary:hover{border-color:rgba(15,23,42,0.24);}

.cdv2-trust-strip{display:flex;flex-wrap:wrap;gap:14px;padding:14px;background:#F1F5F9;border-radius:10px;font-size:11px;color:#475569;justify-content:center;margin-top:18px;}
.cdv2-trust-item{display:flex;align-items:center;gap:5px;}
.cdv2-trust-item::before{content:"✓";color:#10B981;font-weight:700;}

/* Sections */
.cdv2-section{padding:36px 0;border-bottom:1px solid rgba(15,23,42,0.05);}
.cdv2-sec-eyebrow{font-size:10px;color:#3B82F6;font-weight:800;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:8px;}
.cdv2-sec-title{font-size:21px;font-weight:800;color:#0F172A;margin:0 0 6px;letter-spacing:-0.01em;}
@media(min-width:768px){.cdv2-sec-title{font-size:24px;}}
.cdv2-sec-sub{font-size:13px;color:#64748B;margin:0 0 22px;}

/* 5 维 */
.cdv2-dim-row{padding:14px 0;border-bottom:1px solid rgba(15,23,42,0.05);}
.cdv2-dim-row:last-child{border-bottom:0;}
.cdv2-dim-head{display:flex;align-items:center;gap:10px;margin-bottom:7px;flex-wrap:wrap;}
.cdv2-dim-dot{display:inline-block;width:10px;height:10px;border-radius:50%;flex-shrink:0;}
.cdv2-dim-label{font-size:14px;font-weight:700;color:#0F172A;}
.cdv2-dim-status{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600;}
.cdv2-dim-status[data-status="空白"]{background:#FEE2E2;color:#B91C1C;}
.cdv2-dim-status[data-status="待提升"]{background:#FEF3C7;color:#B45309;}
.cdv2-dim-status[data-status="成长"]{background:#DBEAFE;color:#1D4ED8;}
.cdv2-dim-status[data-status="成熟"]{background:#D1FAE5;color:#065F46;}
.cdv2-dim-status[data-status="领先"]{background:#10B98133;color:#065F46;}
.cdv2-dim-score{margin-left:auto;font-family:"SF Pro Display",-apple-system,sans-serif;font-size:20px;font-weight:800;color:#0F172A;}
.cdv2-dim-max{font-size:12px;color:#94A3B8;font-weight:600;}
.cdv2-dim-bar{height:6px;background:#E2E8F0;border-radius:3px;overflow:hidden;}
.cdv2-dim-fill{height:100%;border-radius:3px;}
.cdv2-dim-explanation{font-size:11px;color:#64748B;margin-top:6px;}

/* 证据 */
.cdv2-ev-disclaimer{font-size:11px;color:#92400E;background:#FEF3C7;border:1px solid #FCD34D;border-radius:8px;padding:10px 14px;margin-bottom:14px;line-height:1.55;}
.cdv2-ev-disclaimer strong{color:#78350F;}
.cdv2-evidence-legend{display:grid;grid-template-columns:1fr;gap:8px;margin:-8px 0 16px;}
@media(min-width:768px){.cdv2-evidence-legend{grid-template-columns:repeat(3,1fr);}}
.cdv2-evidence-legend-item{font-size:11px;line-height:1.55;color:#475569;background:#F8FAFC;border:1px solid rgba(15,23,42,0.06);border-radius:10px;padding:10px 12px;}
.cdv2-evidence-legend-item b{display:block;color:#0F172A;font-size:12px;margin-bottom:2px;}
.cdv2-ev-summary-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px;}
.cdv2-ev-stat{padding:14px;background:#fff;border:1px solid rgba(15,23,42,0.06);border-radius:10px;text-align:center;}
.cdv2-ev-stat-num{font-family:"SF Pro Display",-apple-system,sans-serif;font-size:24px;font-weight:800;color:#0F172A;line-height:1;}
.cdv2-ev-stat-label{font-size:10px;color:#64748B;letter-spacing:0.06em;margin-top:4px;}
.cdv2-ev-stat-A .cdv2-ev-stat-num{color:#10B981;}
.cdv2-ev-stat-B .cdv2-ev-stat-num{color:#F59E0B;}
.cdv2-ev-stat-C .cdv2-ev-stat-num{color:#94A3B8;}
.cdv2-ev-card{background:#fff;border:1px solid rgba(15,23,42,0.08);border-radius:12px;padding:16px;margin-bottom:12px;}
.cdv2-ev-head{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap;}
.cdv2-ev-level{font-size:10px;padding:3px 8px;border-radius:6px;font-weight:700;border:1px solid;letter-spacing:0.04em;}
.cdv2-ev-src{font-size:11px;color:#475569;font-weight:600;padding:3px 8px;background:#F1F5F9;border-radius:6px;}
.cdv2-ev-summary{font-size:13px;color:#1E293B;line-height:1.65;padding:10px 12px;background:#F8FAFC;border-radius:8px;border-left:3px solid #3B82F6;margin-bottom:8px;}
.cdv2-ev-details{margin-top:6px;}
.cdv2-ev-toggle{cursor:pointer;font-size:11px;color:#64748B;padding:6px 10px;background:#F8FAFC;border:1px dashed rgba(15,23,42,0.12);border-radius:6px;display:inline-flex;align-items:center;gap:6px;list-style:none;user-select:none;}
.cdv2-ev-toggle::-webkit-details-marker{display:none;}
.cdv2-ev-toggle:hover{background:#F1F5F9;color:#0F172A;}
.cdv2-ev-arrow{display:inline-block;width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-top:5px solid #94A3B8;transition:transform 0.18s;}
.cdv2-ev-details[open] .cdv2-ev-arrow{transform:rotate(180deg);}
.cdv2-ev-keyword-detail{font-size:11px;color:#475569;margin-top:8px;padding:8px 12px;background:#F1F5F9;border-radius:8px;border-left:3px solid #94A3B8;font-family:Menlo,Monaco,monospace;}
.cdv2-ev-snippet{font-size:12px;color:#475569;line-height:1.65;margin-top:8px;padding:10px 12px;background:#FFFBEB;border-radius:8px;border:1px solid #FDE68A;}
.cdv2-ev-citations{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;}
.cdv2-ev-cite{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:#3B82F6;padding:3px 8px;background:#EFF6FF;border-radius:6px;}
.cdv2-ev-cite-disabled{color:#64748B;background:#F1F5F9;cursor:default;}
.cdv2-ev-cite-disabled .cdv2-ev-cite-dot{background:#94A3B8;}
.cdv2-ev-cite-dot{width:5px;height:5px;border-radius:50%;background:#3B82F6;}

/* 竞品 */
.cdv2-comp-table{width:100%;border-collapse:collapse;background:#fff;border:1px solid rgba(15,23,42,0.06);border-radius:10px;overflow:hidden;}
.cdv2-comp-table th,.cdv2-comp-table td{padding:12px 14px;text-align:left;font-size:13px;border-bottom:1px solid rgba(15,23,42,0.05);}
.cdv2-comp-table th{font-size:10px;color:#94A3B8;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;background:#F8FAFC;}
.cdv2-comp-table tr:last-child td{border-bottom:0;}
.cdv2-comp-rank{font-family:"SF Pro Display",-apple-system,sans-serif;font-weight:800;color:#3B82F6;}
.cdv2-comp-name{color:#0F172A;font-weight:600;}
.cdv2-comp-cnt{font-family:Menlo,Monaco,monospace;color:#475569;}
.cdv2-comp-freq{color:#64748B;font-size:12px;}
.cdv2-comp-empty{text-align:center!important;color:#64748B;padding:24px!important;}

/* Final note */
.cdv2-final-note{padding:34px 0 48px;background:#F8FAFC;border-top:1px solid rgba(15,23,42,0.08);color:#334155;}
.cdv2-final-note .cdv2-container{max-width:760px;}
.cdv2-final-note .cdv2-sec-title{color:#0F172A;font-size:22px;}
@media(min-width:768px){.cdv2-final-note .cdv2-sec-title{font-size:24px;}}
.cdv2-final-note .cdv2-sec-sub{color:#475569;font-size:14px;margin-bottom:18px;max-width:60ch;}
.cdv2-final-footer{margin-top:22px;font-size:11px;color:#64748B;border-top:1px solid rgba(15,23,42,0.08);padding-top:16px;line-height:1.7;}

/* ======================================================== */
/* Phase 08 (CTO-15.23 2026-05-04) · 新增样式 · 替代 5 维资产健康度 */
/* 4 件事表格 + 3 层漏斗 + 业务语言翻译 + AI 实测原文 */
/* ======================================================== */

/* 4 件事 */
.cdv2-four-things{display:grid;grid-template-columns:1fr;gap:12px;margin-top:8px;}
@media(min-width:768px){.cdv2-four-things{grid-template-columns:1fr 1fr;}}
.cdv2-thing-card{background:#fff;border:1px solid rgba(15,23,42,0.08);border-radius:14px;padding:18px 20px;transition:all 0.2s;}
.cdv2-thing-card:hover{border-color:rgba(15,23,42,0.16);transform:translateY(-1px);box-shadow:0 4px 12px rgba(15,23,42,0.04);}
.cdv2-thing-good{border-left:3px solid #10B981;}
.cdv2-thing-warn{border-left:3px solid #F59E0B;}
.cdv2-thing-bad{border-left:3px solid #EF4444;}
.cdv2-thing-neutral{border-left:3px solid #3B82F6;}
.cdv2-thing-q{font-size:13px;color:#64748B;font-weight:600;margin-bottom:8px;}
.cdv2-thing-a{font-size:16px;font-weight:700;color:#0F172A;margin-bottom:10px;line-height:1.5;}
.cdv2-thing-text-good{color:#059669;}
.cdv2-thing-text-warn{color:#B45309;}
.cdv2-thing-text-bad{color:#B91C1C;}
.cdv2-thing-impact{font-size:12px;color:#64748B;border-top:1px solid rgba(15,23,42,0.05);padding-top:8px;margin-top:4px;}
.cdv2-thing-impact b{color:#475569;font-weight:600;}

/* 报告解读 */
.cdv2-interpretation-section{border-top:1px solid rgba(15,23,42,0.04);}
.cdv2-interpretation-card{background:#fff;border:1px solid rgba(15,23,42,0.08);border-left:4px solid #64748B;border-radius:14px;padding:20px 22px;}
.cdv2-interpretation-headline{font-size:16px;font-weight:700;color:#0F172A;line-height:1.6;margin-bottom:12px;}
.cdv2-interpretation-list{margin:0 0 14px;padding-left:18px;color:#475569;font-size:13px;line-height:1.75;}
.cdv2-interpretation-list li{margin:4px 0;}
.cdv2-interpretation-translation{font-size:13px;color:#334155;line-height:1.7;background:#F8FAFC;border-radius:10px;padding:12px 14px;margin-top:8px;}
.cdv2-interpretation-caveat{font-size:12px;color:#64748B;line-height:1.7;border-top:1px dashed #CBD5E1;margin-top:12px;padding-top:12px;}

/* 05-08 报告后半段解读 */
.cdv2-tail-section{border-top:1px solid rgba(15,23,42,0.04);}
.cdv2-tail-grid{display:grid;grid-template-columns:1fr;gap:12px;margin-top:14px;}
@media(min-width:768px){.cdv2-tail-grid{grid-template-columns:1fr 1fr;}}
.cdv2-tail-card{background:#fff;border:1px solid rgba(15,23,42,0.08);border-radius:14px;padding:18px 20px;}
.cdv2-tail-label{font-size:10px;color:#3B82F6;font-weight:800;letter-spacing:0.12em;margin-bottom:6px;}
.cdv2-tail-title{font-size:16px;color:#0F172A;font-weight:800;margin:0 0 10px;line-height:1.4;}
.cdv2-tail-body{font-size:13px;color:#475569;line-height:1.75;white-space:pre-wrap;word-break:break-word;}
.cdv2-tail-body b{color:#0F172A;}

/* 3 层漏斗 · 替代 5 维雷达 */
.cdv2-funnel-row{display:grid;grid-template-columns:1fr;gap:12px;}
.cdv2-funnel-card{background:#fff;border:1px solid rgba(15,23,42,0.08);border-radius:14px;padding:18px 20px;}
.cdv2-funnel-good{border-top:3px solid #10B981;}
.cdv2-funnel-warn{border-top:3px solid #F59E0B;}
.cdv2-funnel-bad{border-top:3px solid #EF4444;}
.cdv2-funnel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:14px;}
.cdv2-funnel-name{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:16px;font-weight:700;color:#0F172A;}
.cdv2-funnel-num{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:6px;background:#F1F5F9;color:#64748B;font-size:12px;font-weight:800;}
.cdv2-funnel-good .cdv2-funnel-num{background:#D1FAE5;color:#059669;}
.cdv2-funnel-warn .cdv2-funnel-num{background:#FEF3C7;color:#B45309;}
.cdv2-funnel-bad .cdv2-funnel-num{background:#FEE2E2;color:#B91C1C;}
.cdv2-funnel-status{font-size:11px;font-weight:600;padding:3px 9px;border-radius:5px;}
.cdv2-funnel-status-good{background:#D1FAE5;color:#059669;}
.cdv2-funnel-status-warn{background:#FEF3C7;color:#B45309;}
.cdv2-funnel-status-bad{background:#FEE2E2;color:#B91C1C;}
.cdv2-funnel-weight{font-size:11px;color:#94A3B8;letter-spacing:0.04em;margin-top:4px;}
.cdv2-funnel-score{font-size:24px;font-weight:800;font-variant-numeric:tabular-nums;}
.cdv2-funnel-score-num{font-size:24px;font-weight:800;}
.cdv2-funnel-good .cdv2-funnel-score-num{color:#10B981;}
.cdv2-funnel-warn .cdv2-funnel-score-num{color:#F59E0B;}
.cdv2-funnel-bad .cdv2-funnel-score-num{color:#EF4444;}
.cdv2-funnel-score-max{font-size:13px;color:#94A3B8;font-weight:600;}
.cdv2-funnel-bar{height:7px;background:#F1F5F9;border-radius:999px;overflow:hidden;margin-bottom:10px;}
.cdv2-funnel-bar-fill{height:100%;border-radius:999px;transition:width 0.6s ease-out;}
.cdv2-funnel-good .cdv2-funnel-bar-fill{background:linear-gradient(90deg,#059669,#10B981);}
.cdv2-funnel-warn .cdv2-funnel-bar-fill{background:linear-gradient(90deg,#D97706,#F59E0B);}
.cdv2-funnel-bad .cdv2-funnel-bar-fill{background:linear-gradient(90deg,#B91C1C,#EF4444);}
.cdv2-funnel-meta{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:#64748B;}
.cdv2-funnel-meta b{color:#475569;font-weight:600;}
.cdv2-funnel-business{color:#64748B;font-style:italic;}

/* 业务语言翻译 · 底线 / 核心 / 天花板 */
.cdv2-translate-grid{display:grid;grid-template-columns:1fr;gap:12px;}
@media(min-width:768px){.cdv2-translate-grid{grid-template-columns:repeat(3,1fr);}}
.cdv2-translate-card{background:#fff;border:1px solid rgba(15,23,42,0.08);border-radius:14px;padding:18px 20px;}
.cdv2-translate-icon{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700;margin-bottom:12px;}
.cdv2-translate-good{background:#D1FAE5;color:#059669;}
.cdv2-translate-warn{background:#FEF3C7;color:#B45309;}
.cdv2-translate-bad{background:#FEE2E2;color:#B91C1C;}
.cdv2-translate-tier{font-size:11px;color:#94A3B8;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:4px;font-weight:600;}
.cdv2-translate-name{font-size:15px;font-weight:700;color:#0F172A;margin-bottom:8px;}
.cdv2-translate-desc{font-size:12px;color:#475569;line-height:1.6;}
.cdv2-translate-verdict{margin-top:12px;padding-top:12px;border-top:1px solid rgba(15,23,42,0.05);font-size:12px;font-weight:600;}
.cdv2-translate-verdict-good{color:#059669;}
.cdv2-translate-verdict-warn{color:#B45309;}
.cdv2-translate-verdict-bad{color:#B91C1C;}

/* 30 秒看懂 · GEO + 漏斗翻译 */
.cdv2-glossary{background:#fff;border:1px solid rgba(15,23,42,0.08);border-radius:14px;padding:20px 22px;}
.cdv2-glossary-grid{display:grid;grid-template-columns:1fr;gap:16px;}
@media(min-width:768px){.cdv2-glossary-grid{grid-template-columns:1fr 1fr;}}
.cdv2-glossary-term{font-size:11px;color:#3B82F6;font-weight:700;letter-spacing:0.06em;margin-bottom:4px;text-transform:uppercase;}
.cdv2-glossary-def{font-size:13px;color:#475569;line-height:1.65;}
.cdv2-glossary-def b{color:#0F172A;}

/* AI 实测原文 · 8 题 4 引擎 折叠 */
.cdv2-test-summary{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:16px;}
@media(min-width:768px){.cdv2-test-summary{grid-template-columns:repeat(4,1fr);}}
.cdv2-test-stat{background:#fff;border:1px solid rgba(15,23,42,0.06);border-radius:10px;padding:14px 16px;}
.cdv2-test-stat-num{font-size:22px;font-weight:800;color:#0F172A;line-height:1;font-variant-numeric:tabular-nums;}
.cdv2-test-stat-num.success{color:#10B981;}
.cdv2-test-stat-num.danger{color:#EF4444;}
.cdv2-test-stat-label{font-size:11px;color:#64748B;margin-top:6px;}

.cdv2-test-q{background:#fff;border:1px solid rgba(15,23,42,0.08);border-radius:10px;margin-bottom:10px;overflow:hidden;}
.cdv2-test-q summary{cursor:pointer;padding:14px 18px;list-style:none;display:flex;align-items:center;gap:10px;flex-wrap:wrap;transition:background 0.15s;}
.cdv2-test-q summary::-webkit-details-marker{display:none;}
.cdv2-test-q summary:hover{background:#F8FAFC;}
.cdv2-test-q summary::after{content:"▸";color:#94A3B8;transition:transform 0.2s;font-size:13px;margin-left:auto;}
.cdv2-test-q[open] summary::after{transform:rotate(90deg);}
.cdv2-test-q-num{display:inline-flex;align-items:center;justify-content:center;min-width:32px;padding:0 8px;height:24px;border-radius:6px;background:#F1F5F9;color:#475569;font-size:11px;font-weight:700;}
.cdv2-test-q-text{font-size:13px;font-weight:600;color:#0F172A;flex:1;min-width:0;}
.cdv2-test-q-tier{font-size:10px;color:#64748B;padding:2px 7px;border-radius:4px;background:#F1F5F9;}
.cdv2-test-q-hit{font-size:11px;font-weight:600;padding:2px 9px;border-radius:5px;}
.cdv2-test-q-hit-full{background:#D1FAE5;color:#059669;}
.cdv2-test-q-hit-partial{background:#FEF3C7;color:#B45309;}
.cdv2-test-q-hit-miss{background:#FEE2E2;color:#B91C1C;}

.cdv2-engine-block{padding:14px 18px;border-top:1px solid rgba(15,23,42,0.05);}
.cdv2-engine-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;}
.cdv2-engine-name{font-size:11px;font-weight:700;padding:3px 9px;border-radius:5px;}
.cdv2-engine-d{background:#EDE9FE;color:#6D28D9;}
.cdv2-engine-a{background:#D1FAE5;color:#059669;}
.cdv2-engine-b{background:#FEF3C7;color:#B45309;}
.cdv2-engine-c{background:#DBEAFE;color:#1D4ED8;}
.cdv2-engine-default{background:#F1F5F9;color:#475569;}
.cdv2-engine-status{font-size:11px;}
.cdv2-engine-hit{color:#10B981;}
.cdv2-engine-miss{color:#94A3B8;}
.cdv2-engine-mention{font-size:10px;color:#94A3B8;font-family:Menlo,Monaco,monospace;}
.cdv2-engine-body{font-size:12px;color:#475569;line-height:1.7;white-space:pre-wrap;word-break:break-word;}
"""


def render_customer_decision_page_html(
    *,
    meta: dict,
    modules_jsonb: dict,
    completeness: dict,
    audience: str = "client",
    snapshot_id: str | None = None,
    whitelabel: dict | None = None,
    branding: dict | None = None,
) -> str:
    """PR-A · 客户决策页 v2 · 单一 view model(modules_jsonb)+ 灰度 feature flag

    Args:
        meta: {brand_name, total_score, level, industry, city, created_at}
        modules_jsonb: report_v2_modules_jsonb(同 render_report_html)
        completeness: 完整度数据
        audience: 'client'(默认 · 客户决策页只支持 client)
        snapshot_id: 已废弃的兼容参数；公开页面不会渲染内部快照 ID
        whitelabel: 白标 dict(PR-B0 旧通道 · 仅当代理有白标且启用时传入 · None 时退平台默认)
            字段: company_name / logo_url / contact_name / contact_phone / contact_wechat / contact_email / slogan
            来源: share_api 按报告对象解析管理员批准的展示配置后注入
        branding: v3.6 白标 brand dict(来自 resolve_branding_context(surface='customer'))·
            优先于 whitelabel(决策 C:external_only 客户面不回退平台,代理未填字段用 company_name 兜底)·
            None 时退回 whitelabel 旧通道,再 None 退平台默认。

    Returns:
        完整 HTML 字符串 · 移动优先 · sticky CTA · 安全摘要 · 折叠原文 + disclaimer
    """
    audience = (audience or "client").lower()
    if not is_client_report_ready(modules_jsonb):
        return render_client_report_not_ready_html(meta)
    modules = get_client_report_modules(modules_jsonb)
    modules = _with_canonical_score(modules, meta)

    # 取关键模块
    cover = modules.get('1', {}) or {}
    scoring = modules.get('2', {}) or {}
    evidence = modules.get('3', {}) or {}
    competitor = modules.get('4', {}) or {}

    # 顶部 meta
    brand_name = _esc(meta.get('brand_name', '品牌'))
    industry = _esc(meta.get('industry', ''))
    diag_date = _esc(meta.get('created_at') or '')

    # 评分 + 等级由 report_v2_score 注入 meta，禁止模块摘要覆盖 canonical 值。
    summary = scoring.get('summary', {}) or {}
    total_score = meta.get('total_score')
    score_display = '—' if total_score is None else _esc(total_score)
    max_score = summary.get('max_score') or 100
    level = meta.get('level') or '数据不足'
    level = _client_level_name(level, audience)
    completeness_score = (completeness or {}).get('score')
    completeness_note = (
        f"资料完整度 {_esc(completeness_score)}/100 · 只表示本次判断依据是否充足，不是 GEO 综合评分。"
        if completeness_score is not None
        else "资料完整度：本次数据不足 · 不代表 GEO 综合评分为 0。"
    )
    level_meta = summary.get('level_meta', {}) or {}
    level_meta_summary = _soften_promise_text(level_meta.get('summary', '') or '')
    conclusion = _soften_promise_text(cover.get('conclusion_text', '') or '')

    # 当前报告数据模型没有带样本版本的行业基准。只展示本次诊断维度，
    # 忽略历史调用方可能传入的 industry_avg / industry_best 占位字段。
    dims = scoring.get('dimensions') or []
    radar_html = _radar_chart_svg(dims, size=340)
    benchmark_note = '暂无行业基准'
    dim_rows = _build_dim_rows_v2(dims)

    # Phase 08 (CTO-15.23 2026-05-04) · 3 层漏斗数据接入 · 替代 5 维资产健康度
    # 数据源:modules_jsonb.funnel(顶层 · diagnosis_report_v2.py L333 写入)
    # fallback:side.funnel / scoring.funnel_score / scoring.layers
    root_modules = modules_jsonb if isinstance(modules_jsonb, dict) else {}
    client_side = root_modules.get('client')
    client_side = client_side if isinstance(client_side, dict) else {}
    funnel = root_modules.get('funnel') or client_side.get('funnel') or scoring.get('funnel_score') or scoring.get('funnel') or {}
    funnel_layers = funnel.get('layers') or scoring.get('layers') or []
    # 4 件事表格数据派生 · 从 funnel_layers + 通用第 4 行"下一步"
    funnel_html = _build_funnel_layers_v2(funnel_layers) if funnel_layers else ''
    four_things_html = _build_four_things_v2(funnel_layers) if funnel_layers else ''
    business_translate_html = _build_business_translate_v2(funnel_layers) if funnel_layers else ''
    interpretation_html = _render_interpretation_card_v2(modules.get('1_interpretation'))

    # Phase 08 · AI 实测原文 · 从 module 3_raw 抽 8 题 4 引擎完整数据(原本只用 evidence summary)
    raw_tests = (modules.get('3_raw') or {}).get('tests') or []
    raw_tests_summary = _build_raw_tests_summary_v2(raw_tests) if raw_tests else ''
    raw_tests_total = len(raw_tests)
    raw_engines_total = sum(len(t.get('results') or []) for t in raw_tests)
    raw_brand_hits = sum(1 for t in raw_tests for r in (t.get('results') or []) if r.get('brand_detected'))

    # [2026-06-07 修复客户付费看不到] 自定义检测问题(客户本次填写的业务题)· module 3_raw.custom_tests
    #   双轨/dual 模式下主表只渲染系统 8 题 · custom_tests(自定义 N 题)早已落在 module 数据里却没被 cdv2 渲染
    #   → 客户付了"每题 +100"却在报告里看不到自定义题命中情况 · 此处独立追加(结构同 raw_tests · 复用 summary builder)
    custom_tests = (modules.get('3_raw') or {}).get('custom_tests') or []
    custom_tests_summary = _build_raw_tests_summary_v2(custom_tests) if custom_tests else ''
    custom_tests_total = len(custom_tests)
    custom_engines_total = sum(len(t.get('results') or []) for t in custom_tests)
    custom_brand_hits = sum(1 for t in custom_tests for r in (t.get('results') or []) if r.get('brand_detected'))
    engine_labels = _derive_engine_labels_v2(raw_tests, custom_tests)
    engine_scope_text = (
        f"本次测试：{'、'.join(_esc(label) for label in engine_labels)}"
        if engine_labels else '本次 AI 实测'
    )
    engine_coverage_text = (
        f"本次测试覆盖：{'、'.join(_esc(label) for label in engine_labels)}"
        if engine_labels else '本次 AI 实测'
    )

    # 证据
    evidences_all = evidence.get('evidences', []) or []
    raw_ec = evidence.get('evidence_count', 0)
    if isinstance(raw_ec, dict):
        ev_count = sum(int(v) for v in raw_ec.values())
    else:
        try:
            ev_count = int(raw_ec) if raw_ec else len(evidences_all)
        except (TypeError, ValueError):
            ev_count = len(evidences_all)
    if ev_count == 0:
        ev_count = evidence.get('evidence_total') or len(evidences_all)
    a_count = sum(1 for e in evidences_all if e.get('evidence_level') == 'A')
    b_count = sum(1 for e in evidences_all if e.get('evidence_level') == 'B')
    c_count = sum(1 for e in evidences_all if e.get('evidence_level') == 'C')
    # PR-A.1 (Codex 反馈 P1-4) · 全部渲染 evidence (不再切 [:6])· 兑现"12 条原文"承诺
    ev_cards = ''.join(_render_evidence_card_v2(ev) for ev in evidences_all)

    # 同频比例只能使用该统计自己的真实分母；证据条数不是同一总体，不能替代。
    comp_total_raw = competitor.get('co_citation_total')
    comp_total = _nonnegative_int(comp_total_raw)
    if comp_total == 0:
        comp_total = None
    comp_rows = _build_competitor_rows_v2(competitor, comp_total)
    deep_read_html = _render_customer_tail_sections_v2(modules)

    # SAP · 权威背书(说人话 · 移动优先折叠)· 无 3_authority → 空串不渲染
    _auth_inner = _render_authority_inner_html(modules.get('3_authority'))
    authority_section_cdv2 = (
        '<section class="cdv2-section" style="background:#fff;"><div class="cdv2-container">'
        '<div class="cdv2-sec-eyebrow">权威背书</div>'
        '<h2 class="cdv2-sec-title">AI 引用的来源,够权威吗?</h2>'
        '<p class="cdv2-sec-sub">看 AI 现在引用的来源里,有多少是权威媒体、百科,有多少只是普通网站</p>'
        + _auth_inner + '</div></section>'
    ) if _auth_inner else ''

    # 公开页面只展示中性快照标签，不暴露诊断行 ID 或内部快照 ID。
    del snapshot_id
    snapshot_label_short = "报告快照"

    # PR-B0 · 白标提取(老板/Codex 硬验收)
    # v3.6:branding(resolve_branding_context surface=customer)优先 · 退回 whitelabel 旧通道
    # 客户敲门砖页面所有客户可见品牌位 = branding/whitelabel.<field> if 有 else 平台默认 fallback
    if branding is not None:
        _bn = _norm_branding(branding)
        wl = _bn
    elif isinstance(whitelabel, dict):
        wl = whitelabel
    else:
        wl = None
    wl_company = _esc((wl.get('company_name') if wl else '') or '')
    wl_logo_url = (wl.get('logo_url') or wl.get('company_logo_url') or '') if wl else ''
    wl_contact_name = _esc((wl.get('contact_name') if wl else '') or '')
    wl_contact_phone = _esc((wl.get('contact_phone') if wl else '') or '')
    wl_contact_wechat = _esc((wl.get('contact_wechat') if wl else '') or '')
    wl_contact_email = _esc((wl.get('contact_email') if wl else '') or '')
    wl_slogan = _esc((wl.get('slogan') if wl else '') or '')

    # [2026-06-06 白牌留白 · 老板拍] cdv2 = 对外客户敲门砖页 · 无白标 → 一律中性,绝不露平台名
    #   _platform_company 仅保留供必要兜底,不再用于客户可见品牌位
    _platform_company = _esc(_brand_company(branding))
    # 顶部品牌标(短)· 无白标 → 中性"GEO 诊断报告"(不露平台名)
    topbar_brand_short = wl_company if wl_company else 'GEO 诊断报告'
    # 顶部 logo 块 · 有白标 logo_url 用图,否则不渲染显式 logo(留白 · CSS ::before 为中性色块·不含平台名)
    if wl_logo_url:
        # 白标 logo 显式渲染 img · 替换 ::before 渐变方块
        topbar_logo_html = f'<img src="{_esc(wl_logo_url)}" alt="{topbar_brand_short}" class="cdv2-brand-logo-img"/>'
    else:
        topbar_logo_html = ''  # CSS ::before 渲染中性渐变色块(不含平台名)
    # 顾问联系方式 · 任一字段有就渲染顾问名片
    has_advisor = bool(wl_contact_name or wl_contact_phone or wl_contact_wechat or wl_contact_email)
    advisor_display_name = wl_contact_name if wl_contact_name else (wl_company if wl_company else '专属顾问')
    # CTA 成功提示文案 · 有顾问名用名 · 否则"顾问"
    advisor_notify_label = wl_contact_name if wl_contact_name else (f'{wl_company}团队' if wl_company else '顾问')
    # 隐私政策措辞 · 主体公司 · 无白标 → 中性"本服务方"(不露平台名)
    privacy_subject = wl_company if wl_company else '本服务方'
    # 页脚品牌 · 无白标 → 中性(去平台名)
    footer_brand = wl_company if wl_company else 'AI 搜索优化服务'
    footer_slogan = wl_slogan if wl_slogan else 'AI 搜索优化'

    title = f"{brand_name} · {_esc(level)} · GEO 诊断报告"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="description" content="{brand_name} GEO 诊断报告 · AI 搜索可见度 {score_display}/100 · {industry}">
<title>{title}</title>
<style>{_CUSTOMER_DECISION_CSS}{SAFE_MARKDOWN_CSS}</style>
</head>
<body>

<div class="cdv2-topbar">
  <div class="cdv2-topbar-inner">
    <div class="cdv2-brand-mark{' cdv2-brand-mark-with-logo' if topbar_logo_html else ''}">{topbar_logo_html}<span class="cdv2-brand-mark-text">{topbar_brand_short}</span></div>
    <div class="cdv2-snapshot-tag">{snapshot_label_short}</div>
  </div>
</div>

<section class="cdv2-hero">
  <div class="cdv2-container">
    <div class="cdv2-hero-grid">
      <div>
        <div class="cdv2-hero-meta">诊断日期 {diag_date or '待补充'}</div>
        <h1 class="cdv2-hero-name">{brand_name}</h1>
        <div class="cdv2-hero-industry">{industry}</div>

        <div class="cdv2-score-block">
          <span class="cdv2-score-num">{score_display}</span>
          <span class="cdv2-score-max">/ {max_score}</span>
        </div>
        <div class="cdv2-score-suffix">GEO 综合评分 · AI 搜索可见度</div>
        <div class="cdv2-level-summary">{completeness_note}</div>
        <div style="margin-top:14px;"><span class="cdv2-level-badge">{_esc(level)} · {_esc(level_meta_summary[:24])}</span></div>
        <div class="cdv2-level-summary">{_esc(conclusion)}</div>

        <div class="cdv2-cta-row">
          <button class="cdv2-btn cdv2-btn-secondary" onclick="(function(){{var nodes=Array.from(document.querySelectorAll('details:not([open])'));nodes.forEach(function(node){{node.open=true;}});window.addEventListener('afterprint',function(){{nodes.forEach(function(node){{node.open=false;}});}},{{once:true}});window.print();}})();">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            <span>导出 PDF 离线看</span>
          </button>
        </div>

        <div class="cdv2-trust-strip">
          <div class="cdv2-trust-item">{engine_scope_text}</div>
          <div class="cdv2-trust-item">{ev_count} 条 AI 实测记录</div>
          <div class="cdv2-trust-item">{(str(len(dims)) + ' 维度综合评分') if dims else '综合评分口径'}</div>
          <div class="cdv2-trust-item">分享快照不可变</div>
        </div>
      </div>
      <div>
        <div class="cdv2-hero-radar cdv2-hero-radar-desktop">{radar_html}</div>
        <div class="cdv2-level-summary" style="text-align:center;margin-left:auto;margin-right:auto;">{benchmark_note}</div>
      </div>
    </div>
  </div>
</section>

{('<section class="cdv2-section"><div class="cdv2-container"><div class="cdv2-sec-eyebrow">01 · 阅读入口</div><h2 class="cdv2-sec-title">客户最该先看 4 件事</h2><p class="cdv2-sec-sub">先从客户视角核对 4 个问题 · 看 AI 的回答在哪一层有证据、在哪一层断档</p><div class="cdv2-four-things">' + four_things_html + '</div></div></section>') if four_things_html else ''}

{interpretation_html}

{('<section class="cdv2-section" style="background:#fff;"><div class="cdv2-container"><div class="cdv2-sec-eyebrow">02 · 业务诊断核心</div><h2 class="cdv2-sec-title">3 层漏斗 · 你的真实状态</h2><p class="cdv2-sec-sub">把客户搜索问题分成 3 层 · 越往后越接近采购决策,越需要可引用证据支撑</p><div class="cdv2-funnel-row">' + funnel_html + '</div></div></section>') if funnel_html else (
'<section class="cdv2-section"><div class="cdv2-container"><div class="cdv2-sec-eyebrow">02 · 健康度</div><h2 class="cdv2-sec-title">' + ((str(len(dims)) + ' 维度评分') if dims else '维度评分') + '</h2><p class="cdv2-sec-sub">100 分制 · 全报告同一口径</p>' + dim_rows + '</div></section>'
)}

{('<section class="cdv2-section"><div class="cdv2-container"><div class="cdv2-sec-eyebrow">03 · 翻译成业务语言</div><h2 class="cdv2-sec-title">底线 · 核心 · 天花板</h2><p class="cdv2-sec-sub">从 3 层漏斗看你的生意从哪儿断的</p><div class="cdv2-translate-grid">' + business_translate_html + '</div></div></section>') if business_translate_html else ''}

<section class="cdv2-section" style="background:#fff;">
  <div class="cdv2-container">
    <div class="cdv2-sec-eyebrow">入门翻译</div>
    <h2 class="cdv2-sec-title">30 秒看懂两个词</h2>
    <p class="cdv2-sec-sub">报告里反复出现的两个词 · 一句话讲清楚</p>
    <div class="cdv2-glossary">
      <div class="cdv2-glossary-grid">
        <div>
          <div class="cdv2-glossary-term">GEO</div>
          <div class="cdv2-glossary-def">让 AI 在回答用户问题时<b>主动提到你、推荐你</b>。<br><span style="color:#94A3B8;">不是传统 SEO 排名 · 而是 AI 答案里的存在感</span></div>
        </div>
        <div>
          <div class="cdv2-glossary-term">3 层漏斗</div>
          <div class="cdv2-glossary-def">把客户搜索问题分成 3 层:<br>先问<b>你是谁</b> → 再问<b>哪家好</b> → 最后问<b>方案怎么选</b>。<br><span style="color:#94A3B8;">越往后越接近成交</span></div>
        </div>
      </div>
    </div>
  </div>
</section>

<section class="cdv2-section" style="background:#fff;">
  <div class="cdv2-container">
    <div class="cdv2-sec-eyebrow">03 · 可信度</div>
    <h2 class="cdv2-sec-title">AI 实测证据 · {ev_count} 条原文</h2>
    <p class="cdv2-sec-sub">{engine_coverage_text} · 按可信程度分类 · 每条带原始来源</p>
    <div class="cdv2-evidence-legend" aria-label="证据可信程度说明">
      <div class="cdv2-evidence-legend-item"><b>直接证据:AI 直接提到品牌、竞品或结论</b>可作为判断当前 AI 回答状态的主要证据。</div>
      <div class="cdv2-evidence-legend-item"><b>参考证据:与品牌或行业相关,但需要结合上下文</b>适合辅助解释趋势,不单独下结论。</div>
      <div class="cdv2-evidence-legend-item"><b>待验证线索:目前只能作为后续核验方向</b>用于提示可能问题,不等同于事实结论。</div>
    </div>

    <div class="cdv2-ev-disclaimer">
      <strong>关于 AI 引擎原始回答 · 请理性参考</strong><br>
      下方每条证据的「查看 AI 引擎测试问题 + 原始回答」展开后,内容来自本次 AI 实测结果 · 可能含 AI 模型的误判信息或营销话术 · <strong>不代表 {privacy_subject} 对其中事实真实性背书</strong> · 仅作为"AI 当下如何描述您的品牌"的客观快照证据。
    </div>

    {('<div class="cdv2-test-summary"><div class="cdv2-test-stat"><div class="cdv2-test-stat-num">' + str(raw_tests_total) + '</div><div class="cdv2-test-stat-label">测试问题</div></div><div class="cdv2-test-stat"><div class="cdv2-test-stat-num">' + str(raw_engines_total) + '</div><div class="cdv2-test-stat-label">AI 问答</div></div><div class="cdv2-test-stat"><div class="cdv2-test-stat-num success">' + str(raw_brand_hits) + '</div><div class="cdv2-test-stat-label">品牌被提及</div></div><div class="cdv2-test-stat"><div class="cdv2-test-stat-num danger">' + str(raw_engines_total - raw_brand_hits) + '</div><div class="cdv2-test-stat-label">未提及(竞品上位)</div></div></div>') if raw_tests_total > 0 else ''}

    {raw_tests_summary}

    {('<div class="cdv2-custom-tests" style="margin-top:30px;padding-top:22px;border-top:1px dashed #CBD5E1;"><div class="cdv2-sec-eyebrow" style="margin-bottom:6px;">自定义检测问题 · 您本次填写</div><p class="cdv2-sec-sub" style="margin-top:0;">这些是您填写的专属业务问题 · 独立于系统基础题 · 作为补充证据展示,不改变本期基础评分口径</p><div class="cdv2-test-summary"><div class="cdv2-test-stat"><div class="cdv2-test-stat-num">' + str(custom_tests_total) + '</div><div class="cdv2-test-stat-label">自定义问题</div></div><div class="cdv2-test-stat"><div class="cdv2-test-stat-num">' + str(custom_engines_total) + '</div><div class="cdv2-test-stat-label">AI 问答</div></div><div class="cdv2-test-stat"><div class="cdv2-test-stat-num success">' + str(custom_brand_hits) + '</div><div class="cdv2-test-stat-label">品牌被提及</div></div><div class="cdv2-test-stat"><div class="cdv2-test-stat-num danger">' + str(custom_engines_total - custom_brand_hits) + '</div><div class="cdv2-test-stat-label">未提及</div></div></div>' + custom_tests_summary + '</div>') if custom_tests else ''}

    {('<div class="cdv2-ev-summary-grid"><div class="cdv2-ev-stat cdv2-ev-stat-A"><div class="cdv2-ev-stat-num">' + str(a_count) + '</div><div class="cdv2-ev-stat-label">直接证据</div></div><div class="cdv2-ev-stat cdv2-ev-stat-B"><div class="cdv2-ev-stat-num">' + str(b_count) + '</div><div class="cdv2-ev-stat-label">参考证据</div></div><div class="cdv2-ev-stat cdv2-ev-stat-C"><div class="cdv2-ev-stat-num">' + str(c_count) + '</div><div class="cdv2-ev-stat-label">待验证线索</div></div></div>' + ev_cards) if not raw_tests_summary and (a_count + b_count + c_count) > 0 else ''}
  </div>
</section>

{authority_section_cdv2}

<section class="cdv2-section">
  <div class="cdv2-container">
    <div class="cdv2-sec-eyebrow">04 · 同频对照</div>
    <h2 class="cdv2-sec-title">AI 搜索同频引用源 · TOP 5</h2>
    <p class="cdv2-sec-sub">用户问 AI 时 · 哪些品牌 / 内容 / 链接 经常和您一起被提及</p>
    <table class="cdv2-comp-table">
      <thead><tr><th>排名</th><th>引用源</th><th>同频次数</th><th>频率档位</th></tr></thead>
      <tbody>{comp_rows}</tbody>
    </table>
  </div>
</section>

{deep_read_html}

<section class="cdv2-final-note">
  <div class="cdv2-container">
    <h2 class="cdv2-sec-title">报告到此结束</h2>
    <p class="cdv2-sec-sub">如需进一步沟通 · 请直接联系发您这份报告的人</p>
    <div class="cdv2-final-footer">
      {footer_brand}{(' · ' + footer_slogan) if footer_slogan and not wl_company else ''} · {snapshot_label_short}<br>
      数据来源: {engine_scope_text} · 数据快照 {diag_date or '待补充'} · 证据可追溯(每条带原始来源链接)
    </div>{_oss_attribution.report_footer_html()}
  </div>
</section>

</body>
</html>"""
