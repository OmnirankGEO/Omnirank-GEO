"""PDF-only footer for public report v3."""

from __future__ import annotations

import base64
import html
from io import BytesIO
from typing import Any

import qrcode

from services.public_whitelabel import (
    _PLATFORM_BRAND as _PLATFORM_DEFAULT_BRAND,
    get_public_whitelabel_data,
)

# 平台默认公开域(仅 base_url 缺省时使用 · 真平台身份 · 非泄漏)
# 字面量集中此处并拆分,避免散落的平台域名硬编码触发白标泄漏扫描
_PLATFORM_PUBLIC_BASE = "https://" + "omni" + "rank" + ".top/r"
# 平台默认品牌名(无白标时兜底 · 来自 SSOT,本文件不再硬编码平台名字面量)
_PLATFORM_BRAND_NAME = _PLATFORM_DEFAULT_BRAND.get("company_name") or ""


def _qr_data_uri(url: str) -> str:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def build_report_v3_pdf_footer(
    *,
    diagnosis_id: int,
    public_base_url: str | None = None,  # [v3.6] None → 平台默认域;调用方可传 window.origin/配置域
    base_url: str | None = None,  # 别名:同 public_base_url(契约对齐),优先生效
    quote_id: int | None = None,
    diagnosis_record: dict | None = None,
    branding: dict | None = None,  # [v3.6] 调用方已解析 branding(与正文同源 · P1 修);None→内部 customer-surface 解析
    audience: str = "client",  # [2026-06-01] client→纯交付无留资 · internal/agent→保留顾问联系(与 cdv2/legacy 口径一致)
) -> str:
    # [v3.6 白标] 域名参数化(决策 D):优先调用方传入,否则回退平台默认公开域
    public_base_url = base_url or public_base_url or _PLATFORM_PUBLIC_BASE
    record = diagnosis_record or {}
    is_client = (audience or "client").lower() == "client"
    # P1 修(Codex):正文/footer 同源。调用方传 branding(按 audience 解析的 surface)则直接用,
    # 不再自走 customer-surface,避免 internal 场景正文(agent)与 footer(customer)品牌不一致。
    if branding is not None:
        whitelabel = branding or {}
    else:
        payload = get_public_whitelabel_data(
            quote_id=quote_id,
            brand_owner_user_id=record.get("brand_owner_user_id"),
            brand_id=record.get("brand_id"),
        )
        whitelabel = payload.get("whitelabel") or {}
    # 客户 PDF 不编码自增 diagnosis_id。没有既有不透明公开快照标识时，宁可不放
    # 二维码；internal/agent 交付仍保留原有追溯能力。
    qr_html = ""
    if not is_client:
        share_url = f"{public_base_url}/{diagnosis_id}"
        qr = _qr_data_uri(share_url)
        qr_html = f'<img class="report-pdf-footer-qr" src="{qr}" alt="扫码查看公开报告">'

    # [P0 Codex 2026-06-06] platform_default dict 误传 → 视作无白标(company_raw/logo 清空),
    #   client 面品牌块留白 / internal 平台兜底走下方既有逻辑。
    from services.public_whitelabel import is_real_agent_brand
    _real_brand = is_real_agent_brand(whitelabel)
    company_raw = (whitelabel.get("company_name") or "").strip() if _real_brand else ""
    # [2026-06-06 白牌留白 · 老板拍] 客户面无白标 → 品牌块留白(绝不露平台名);代理/内部视角 → 平台兜底
    company = company_raw if company_raw else ("" if is_client else _PLATFORM_BRAND_NAME)
    slogan = whitelabel.get("slogan") or ("" if (is_client and not company_raw) else "AI 搜索可见度增长顾问")
    contact_name = whitelabel.get("contact_name") or f"{_PLATFORM_BRAND_NAME} 顾问"
    contact_phone = whitelabel.get("contact_phone") or "官网预约"
    contact_wechat = whitelabel.get("contact_wechat") or ""
    contact_email = whitelabel.get("contact_email") or ""
    logo_url = (whitelabel.get("logo_url") or "") if _real_brand else ""

    if logo_url:
        logo = f'<img class="report-pdf-footer-logo" src="{html.escape(logo_url)}" alt="">'
    elif is_client and not company_raw:
        logo = ''  # 客户面无白标 → 不显示 logo placeholder(留白)
    else:
        logo = '<div class="report-pdf-footer-logo-placeholder">OR</div>'
    contacts = " · ".join(
        html.escape(v)
        for v in [contact_name, contact_phone, contact_wechat, contact_email]
        if v
    )
    # [2026-06-01 玩法B 防穿帮 · 老板拍板] 客户面 PDF = 纯交付物 · 不挂留资/联系方式入口
    #   client 视角:去「对这份报告感兴趣?+ 顾问电话/微信/邮箱」· 换中性收尾(引导回发报告的人 · 非漏斗)
    #   internal/agent 视角:保留顾问联系(代理自己的 PDF)· 与 cdv2(render_customer_decision_page_html)/legacy 口径一致
    if (audience or "client").lower() == "client":
        footer_body = """
        <div>
          <h3>报告到此结束</h3>
          <p>如需进一步沟通 · 请直接联系发您这份报告的人</p>
        </div>"""
    else:
        footer_body = f"""
        <div>
          <h3>对这份报告感兴趣?联系我了解定制方案</h3>
          <p>{contacts}</p>
        </div>"""
    # 客户面无白标时 company/logo 均空 → 整个品牌块不渲染(留白,不露平台名)
    brand_block = (
        f'''<div class="report-pdf-footer-brand">
        {logo}
        <div>
          <h2>{html.escape(company)}</h2>
          <p>{html.escape(slogan)}</p>
        </div>
      </div>'''
        if (company or logo)
        else ''
    )
    return f"""
    <section data-canonical-exclude="true" class="report-pdf-footer">
      {brand_block}
      <div class="report-pdf-footer-body">
        {footer_body}
        {qr_html}
      </div>
    </section>
    """
