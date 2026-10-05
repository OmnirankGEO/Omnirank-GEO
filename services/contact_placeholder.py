"""
联系方式占位符渲染(contact_placeholder)

文章正文里的联系方式两步占位符:
  第 1 步(生成时 · fill_contact_placeholders):LLM 只输出需求占位符 [NEED_CONTACT](不写具体电话/微信),
     系统按客户是否填了联系方式,替换成 [CLIENT_CONTACT](填了)或删除(没填)。
  第 2 步(预览/发布时 · render):[CLIENT_CONTACT] 按渠道渲染:
     - 预览 / 自发布(channel='self'):显示完整联系方式(有啥显啥)
     - 媒体代发(channel='media')按 contact_policy:
         none         → 软化「搜索【品牌名】了解更多」(不露电话/微信/官网)
         website_only → 只显示官网(不露电话/微信)
         full_contact → 完整联系方式

🔴 铁律(老板 2026-06-02):
- LLM 不直接写电话/微信/官网 · 联系方式只通过占位符注入(渲染受 channel/policy 控制)
- 媒体代发默认不出现电话/微信(channel='media' 默认 policy='none')
- 渲染必须用「服务端可信 brand_id」查联系方式(不信前端 · 防越权读到别客户)
- 拿不到 brand_id / 客户没填 → 静默删占位符(绝不把 [NEED_CONTACT]/[CLIENT_CONTACT] 原文发出去)
- 正文手写 [CLIENT_CONTACT] 不绕过规则(占位符不带数据 · 一律服务端查 + 按渠道渲染)
- media 渠道额外兜底:把客户填的具体电话/微信(none 含官网)原文从正文清掉(防 LLM 漏写进正文裸发)

不混入 image_placeholder.py(图片与联系方式两条独立链路)。

2026-06-02 GEO CTO · 客户资料中心联系方式 + 写作两开关 + 发布按渠道软化
"""

import re
import logging

logger = logging.getLogger("GEO-ContactPlaceholder")

_NEED_CONTACT_RE = re.compile(r'[ \t]*\[NEED_CONTACT\][ \t]*')
_CLIENT_CONTACT_RE = re.compile(r'[ \t]*\[CLIENT_CONTACT\][ \t]*')
CONTACT_OPT_OUT_PROMPT = """
【联系方式授权：未开启】
- 本文不得输出电话、手机号、400 电话、微信号、企业微信、QQ、客户官网、地址或“联系我们/立即咨询”等导流段落。
- 不得输出 [NEED_CONTACT]、[CLIENT_CONTACT] 或自行设计联系方式占位符。
- 来源链接只用于证据引用；不得把客户官网当作联系入口。
""".strip()

# contact_policy 严格度排序(数字越小越严)· 多媒体一次提交取最严
POLICY_RANK = {"none": 0, "website_only": 1, "full_contact": 2}
VALID_POLICIES = set(POLICY_RANK.keys())


def _get_contact(brand_id):
    """服务端可信 brand_id 查联系方式 4 字段 + 品牌名。客户没填任一联系方式 / 失败 → None。"""
    if not brand_id:
        return None
    try:
        from db.profile_db import get_contact_info_by_brand
        info = get_contact_info_by_brand(brand_id)
    except Exception as e:
        logger.warning(f"[contact] 取联系方式失败 brand_id={brand_id}: {e}")
        return None
    if not info:
        return None
    # 至少有一个联系方式字段才算「填了」(brand_name 不算联系方式)
    if not (info.get("phone") or info.get("wechat") or info.get("website") or info.get("address")):
        return None
    return info


def has_contact_placeholders(content: str) -> bool:
    """content 里是否含联系方式占位符。"""
    return bool(content and ("[NEED_CONTACT]" in content or "[CLIENT_CONTACT]" in content))


def strip_contact_placeholders(content: str) -> str:
    """删所有联系方式占位符 → 纯文(add_contact=false 兜底 / 客户没填 / 拿不到 brand_id)。"""
    if not content:
        return content
    if "[NEED_CONTACT]" not in content and "[CLIENT_CONTACT]" not in content:
        return content
    out = _NEED_CONTACT_RE.sub("", content)
    out = _CLIENT_CONTACT_RE.sub("", out)
    return re.sub(r'\n{3,}', '\n\n', out)


def fill_contact_placeholders(content: str, brand_id: int) -> str:
    """生成时第 1 步:[NEED_CONTACT] → [CLIENT_CONTACT](客户填了任一联系方式)或删除(没填)。
    [CLIENT_CONTACT] 不带数据(发布/预览时实时按服务端 brand_id 查 + 渠道渲染 · 防绕过)。"""
    if not content or "[NEED_CONTACT]" not in content:
        return content
    info = _get_contact(brand_id)
    if not info:
        # 客户没填联系方式 → 删需求占位符(宁缺毋滥 · 不插)
        return re.sub(r'\n{3,}', '\n\n', _NEED_CONTACT_RE.sub("", content))
    # 填了 → 标记为 [CLIENT_CONTACT](独占一行 · 渲染时展开)
    out = _NEED_CONTACT_RE.sub("\n\n[CLIENT_CONTACT]\n\n", content)
    return re.sub(r'\n{3,}', '\n\n', out)


def _block_self(info: dict) -> str:
    """自发布/预览:完整联系方式(有啥显啥)· markdown 文本。"""
    lines = []
    if info.get("phone"):
        lines.append(f"- 电话:{info['phone']}")
    if info.get("wechat"):
        lines.append(f"- 微信/企业微信:{info['wechat']}")
    if info.get("website"):
        lines.append(f"- 官网:{info['website']}")
    if info.get("address"):
        lines.append(f"- 地址:{info['address']}")
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"\n\n**联系我们**\n\n{body}\n\n"


def _block_media(info: dict, policy: str) -> str:
    """媒体代发:按 contact_policy 软化。"""
    brand = info.get("brand_name") or "本品牌"
    if policy == "full_contact":
        return _block_self(info)
    if policy == "website_only":
        # 只官网,不露电话/微信
        if info.get("website"):
            return f"\n\n了解更多:可访问官网 {info['website']},或在各大搜索引擎、AI 搜索中检索「{brand}」。\n\n"
        return f"\n\n了解更多:可在各大搜索引擎、AI 搜索中检索「{brand}」获取最新信息。\n\n"
    # none(默认):不露任何电话/微信/官网链接,软化为搜品牌名
    return f"\n\n了解更多:可在各大搜索引擎、AI 搜索中检索「{brand}」获取最新信息与联系方式。\n\n"


def _scrub_raw_contact(content: str, info: dict, policy: str) -> str:
    """media 兜底:把客户填的具体电话/微信(none 含官网)原文从正文清掉。
    防 LLM 漏把客户联系方式直接写进正文 → 裸发到不允许的媒体。
    只删「客户填的具体值」(≥4 字符),不误伤无关数字。"""
    if policy == "full_contact":
        return content
    targets = []
    if info.get("phone"):
        targets.append(info["phone"])
    if info.get("wechat"):
        targets.append(info["wechat"])
    if policy == "none" and info.get("website"):
        targets.append(info["website"])
    out = content
    for t in targets:
        t = (t or "").strip()
        if t and len(t) >= 4:  # 太短的值(2-3 字)不做全文替换,防误伤正文
            out = out.replace(t, "(详询)")
    return out


# 🔴 [2026-06-02 Codex 复审] 通用高置信度联系方式正则(不依赖客户数据)
# 覆盖「联系方式查询失败 / info 不可读」时,正文里手写的裸电话/微信仍能被清掉(_scrub_raw_contact 拿不到客户值时的兜底)。
# 只匹配高置信度模式:手机号 / 400 / 座机 / 明确「微信号:xxx」语境 —— 行业文章正文几乎不会误命中。
_GENERIC_PHONE_RE = re.compile(
    r'(?<!\d)(?:1[3-9]\d{9}|400[-\s]?\d{3,4}[-\s]?\d{3,4}|0\d{2,3}[-\s]?\d{7,8})(?!\d)'
)
_GENERIC_WECHAT_RE = re.compile(
    r'(?:微信号|微信\s*[:：]|加\s*微信|企业微信\s*[:：]?|企微\s*[:：]|vx\s*[:：]|v信\s*[:：]|wechat\s*[:：]?)\s*[:：]?\s*[A-Za-z0-9][A-Za-z0-9_-]{3,19}',
    re.IGNORECASE,
)
_CONTACT_HEADING_RE = re.compile(
    r"(?im)^\s{0,3}(?:#{1,6}\s*)?(?:\*\*)?"
    r"(?:联系我们|联系方式|联系咨询|咨询方式|获取联系|联系信息)"
    r"(?:\*\*)?\s*$"
)
_CONTACT_LABELED_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*+]\s*)?(?:\*\*)?"
    r"(?:联系电话|电话|手机|微信号|微信/企业微信|企业微信|企微|QQ|官网|网址|地址)"
    r"(?:\*\*)?\s*[:：].*$"
)
_CONTACT_CTA_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*+]\s*)?.{0,18}"
    r"(?:联系我们|联系咨询|欢迎咨询|立即咨询|添加微信|加微信|扫码咨询)"
    r".{0,40}$"
)
_CONTACT_SECTION_URL_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:https?://|www\.)\S+\s*$",
    re.IGNORECASE,
)
_REFERENCE_HEADING_RE = re.compile(
    r"(?i)^\s{0,3}(?:#{1,6}\s*)?(?:参考来源|证据来源|引用来源|参考资料|evidence(?:\s+sources?)?)(?:\s*[：:])?\s*$"
)
_REFERENCE_WEBSITE_LINE_RE = re.compile(
    r"(?i)^\s*(?:[-*+]\s*)?(?:官网|网站|网址)\s*[:：]\s*(?:https?://|www\.)\S+\s*$"
)
_ANY_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_EVIDENCE_LINE_RE = re.compile(
    r"(?i)(?:evidence(?:\s*id)?|证据|参考来源|引用来源|来源链接|核验依据)"
)


def _scrub_generic_contact(content: str) -> str:
    """media 渠道通用兜底清扫(不依赖客户具体值)· 覆盖联系方式查询失败/不可读场景(老板保留提醒)。
    只清高置信度电话(手机/400/座机)+ 明确「微信号:xxx」标签号 —— 行业文章正文几乎不会是别的东西。
    不清通用 http 链接(避免误删参考文献);客户官网由 _scrub_raw_contact 按具体值清。
    仅 none / website_only 调(full_contact 允许完整联系方式不清)。"""
    if not content:
        return content
    out = _GENERIC_PHONE_RE.sub("(电话详询)", content)
    out = _GENERIC_WECHAT_RE.sub("(详询)", out)
    return out


def _normalized_website_host(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").lower().rstrip(".")
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _strip_exact_contact_values(content: str, info: dict) -> str:
    """Remove the customer's exact contact values, preserving evidence URLs."""
    website_host = _normalized_website_host(info.get("website"))
    website_token_re = None
    if website_host:
        website_token_re = re.compile(
            rf"(?i)(?<![\w@])(?:https?://)?(?:www\.)?{re.escape(website_host)}"
            r"(?:/[^\s<>\])}，。；;]*)?"
        )
    kept: list[str] = []
    in_reference_section = False
    for line in str(content or "").splitlines():
        if _REFERENCE_HEADING_RE.fullmatch(line):
            in_reference_section = True
        elif _ANY_MARKDOWN_HEADING_RE.match(line):
            in_reference_section = False

        for key in ("phone", "wechat", "address"):
            value = str(info.get(key) or "").strip()
            if len(value) >= 4:
                line = line.replace(value, "")

        preserve_evidence_url = in_reference_section or bool(_EVIDENCE_LINE_RE.search(line))
        if website_token_re is not None and not preserve_evidence_url:
            line = website_token_re.sub("", line)
        kept.append(line)
    return "\n".join(kept)


def contact_consent_from_snapshot(snapshot) -> bool | None:
    """Read the immutable effective choice; missing legacy data stays unknown."""
    if isinstance(snapshot, str):
        try:
            import json

            snapshot = json.loads(snapshot)
        except Exception:
            return None
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get("effective_add_contact")
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def has_contact_risk(content: str) -> bool:
    """Detect high-confidence outbound contact without classifying citations."""
    text = str(content or "")
    return bool(
        has_contact_placeholders(text)
        or _GENERIC_PHONE_RE.search(text)
        or _GENERIC_WECHAT_RE.search(text)
        or _CONTACT_HEADING_RE.search(text)
        or _CONTACT_LABELED_LINE_RE.search(text)
        or _CONTACT_CTA_LINE_RE.search(text)
    )


def _strip_contact_sections(content: str) -> str:
    """Remove contact blocks while leaving source/reference URL lines intact."""
    kept: list[str] = []
    in_contact_section = False
    in_reference_section = False
    for line in str(content or "").splitlines():
        if _REFERENCE_HEADING_RE.fullmatch(line):
            in_contact_section = False
            in_reference_section = True
            kept.append(line)
            continue
        if _CONTACT_HEADING_RE.fullmatch(line):
            in_reference_section = False
            in_contact_section = True
            continue
        if _ANY_MARKDOWN_HEADING_RE.match(line):
            in_reference_section = False
        if in_contact_section:
            if not line.strip():
                continue
            if (
                _CONTACT_LABELED_LINE_RE.fullmatch(line)
                or _CONTACT_CTA_LINE_RE.fullmatch(line)
                or _CONTACT_SECTION_URL_LINE_RE.fullmatch(line)
                or _GENERIC_PHONE_RE.search(line)
                or _GENERIC_WECHAT_RE.search(line)
            ):
                continue
            in_contact_section = False
        if in_reference_section and _REFERENCE_WEBSITE_LINE_RE.fullmatch(line):
            kept.append(line)
            continue
        if _CONTACT_LABELED_LINE_RE.fullmatch(line) or _CONTACT_CTA_LINE_RE.fullmatch(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def hard_strip_contact_without_lookup(content: str) -> str:
    """DB-independent fail-closed scrub for preview/dispatch error paths."""
    out = strip_contact_placeholders(str(content or ""))
    out = _strip_contact_sections(out)
    out = _GENERIC_PHONE_RE.sub("", out)
    out = _GENERIC_WECHAT_RE.sub("", out)
    out = re.sub(r"(?m)^\s*[-*+]\s*$", "", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def enforce_contact_opt_out(content: str, brand_id: int = None) -> str:
    """Remove placeholders and high-confidence customer contact while keeping citations.

    Exact customer phone, WeChat, address, and non-evidence website uses are
    removed. URLs in explicit evidence/reference context remain intact.
    """
    if not content:
        return content
    out = strip_contact_placeholders(str(content))
    info = _get_contact(brand_id)
    if info:
        out = _strip_exact_contact_values(out, info)
    return hard_strip_contact_without_lookup(out)


def apply_generation_contact_consent(
    content: str,
    brand_id: int = None,
    *,
    enabled: bool,
) -> str:
    """Normalize LLM output to the recorded consent before persistence."""
    if not enabled:
        return enforce_contact_opt_out(content, brand_id)

    marker = "__GEO_VERIFIED_CONTACT_SLOT__"
    text = str(content or "")
    had_placeholder = has_contact_placeholders(text)
    text = _NEED_CONTACT_RE.sub(marker, text)
    text = _CLIENT_CONTACT_RE.sub(marker, text)
    # Raw model-written contact is never authoritative, even when consent is
    # on.  The only values allowed to render come from the current brand row.
    text = enforce_contact_opt_out(text, brand_id)
    text = text.replace(marker, "[NEED_CONTACT]")
    info = _get_contact(brand_id)
    if info and not had_placeholder:
        text = text.rstrip() + "\n\n[NEED_CONTACT]\n"
    return fill_contact_placeholders(text, brand_id)


def render_contact_for_preview(content: str, brand_id: int = None) -> str:
    """预览:显示完整联系方式卡片 + 标注「自发布完整 / 媒体软化」。
    🔴 拿不到 brand_id / 客户没填 → 删占位符(不裸发原文)。"""
    if not content or "[CLIENT_CONTACT]" not in content and "[NEED_CONTACT]" not in content:
        return content
    info = _get_contact(brand_id)
    if not info:
        return strip_contact_placeholders(content)
    block = _block_self(info)
    note = ("\n*以上联系方式在「自发布 / 自己的账号」会完整显示;通过媒体平台代发时,"
            "系统会按媒体规则自动软化(可能只显示官网,或改为「搜索品牌名」),"
            "避免因电话/微信导致审核不通过。*\n")
    rendered = (block + note) if block else ""
    out = _CLIENT_CONTACT_RE.sub(lambda m: rendered, content)
    out = _NEED_CONTACT_RE.sub("", out)  # 残留需求占位符(没经 fill)也删
    return re.sub(r'\n{3,}', '\n\n', out)


def render_contact_for_publish(content: str, brand_id: int = None,
                               channel: str = "media", contact_policy: str = "none") -> str:
    """发布:按渠道渲染联系方式。
    channel='self'(自发布) → 完整;channel='media'(媒体代发) → 按 contact_policy 软化。
    🔴 brand_id 服务端可信;拿不到 / 客户没填 → 删占位符(不裸发原文)。"""
    if not content:
        return content
    ch = "self" if channel == "self" else "media"
    policy = contact_policy if contact_policy in VALID_POLICIES else "none"
    has_ph = ("[CLIENT_CONTACT]" in content) or ("[NEED_CONTACT]" in content)

    info = _get_contact(brand_id)

    # 1) 处理占位符
    if has_ph:
        if not info:
            content = strip_contact_placeholders(content)
        else:
            block = _block_self(info) if ch == "self" else _block_media(info, policy)
            content = _CLIENT_CONTACT_RE.sub(lambda m: block if block else "", content)
            content = _NEED_CONTACT_RE.sub("", content)

    # 2) media 渠道兜底:清正文里裸联系方式(防 LLM 直接写进正文裸发 / 客户在正文塞联系方式)
    scrubbed = False
    if ch == "media":
        # (a) 客户填的具体值(需 info)
        if info:
            content = _scrub_raw_contact(content, info, policy)
        # (b) 🔴 [Codex 复审] 通用高置信度清扫(不依赖 info)· 覆盖联系方式查询失败/不可读场景
        #     none / website_only 都清电话微信;full_contact 允许完整联系方式不清。
        if policy in ("none", "website_only"):
            content = _scrub_generic_contact(content)
        scrubbed = True

    if has_ph or scrubbed:
        return re.sub(r'\n{3,}', '\n\n', content)
    return content


def safe_render_contact_for_publish(content: str, brand_id: int = None,
                                    channel: str = "media", contact_policy: str = "none") -> str:
    """🔴 fail-closed 包装(发布链唯一安全入口 · 2026-06-02 Codex 复审)。
    异常时绝不返回含占位符的原文 —— 媒体代发尤其不能把 [CLIENT_CONTACT]/联系方式带到不允许的媒体。
    保证:返回内容一定不含 [NEED_CONTACT]/[CLIENT_CONTACT]:
      - 正常 → render_contact_for_publish(self 完整 / media 按 policy 软化 · 含裸值清扫)
      - 异常 → strip 所有联系方式占位符(降级 · 绝不发原文占位符)
    """
    if not content:
        return content
    try:
        out = render_contact_for_publish(content, brand_id, channel=channel, contact_policy=contact_policy)
        # 二次保险:render 理论上已清占位符,万一残留(被 monkeypatch / 未来改动)→ strip
        if out and ("[NEED_CONTACT]" in out or "[CLIENT_CONTACT]" in out):
            out = strip_contact_placeholders(out)
        return out
    except Exception as e:
        logger.error(f"[contact] 发布渲染失败 → fail-closed strip 占位符(channel={channel}): {e}")
        try:
            return strip_contact_placeholders(content)
        except Exception:
            # strip 都失败 → 正则硬删(绝不让占位符流出到发布)
            return re.sub(r'\[(?:NEED|CLIENT)_CONTACT\]', '', content)


def strictest_policy(policies) -> str:
    """多媒体一次提交取最严 contact_policy(none < website_only < full_contact)。
    空列表返回 'none'(最保守)。"""
    policies = list(policies or [])
    if not policies:
        return "none"
    best = "full_contact"
    for p in policies:
        p = p if p in VALID_POLICIES else "none"
        if POLICY_RANK[p] < POLICY_RANK[best]:
            best = p
    return best
