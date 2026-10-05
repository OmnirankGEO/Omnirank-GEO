"""Deal-showcase ("晒成交") intake: three-way merge + structured deal sheet.

Inputs are the structured form, a voice transcript (ASR) and OCR texts of the
uploaded image materials.  The output is the editable 成交确认单: every field
carries ``{value, status, provenance}`` where ``status`` is ``confirmed`` or
``tentative`` ("待确认" — never fabricated, never blocking) and ``provenance``
is always ``customer_asserted``.

Anti-fabrication posture(Owner 2026-07-22 合规口径:提醒不阻断):

1. Prompt layer — the LLM is instructed to extract only facts literally
   present in the supplied materials and to leave unknown fields empty.
2. Output annotation — any concrete number, amount, name or identifier in the
   extracted value that is absent from the source corpus is **kept** but
   marked ``tentative`` and recorded in ``sheet["_meta"]["warnings"]``
   ("AI 提取内容超出你提供的材料,请核对").  原硬剔除口径已取消:系统职责
   是醒目标注提醒,不是阻断。tentative 字段永不进 facts/发布面,确认环节
   由用户核对兜底。

When no LLM is configured the whole extraction degrades to a pure rule merge
(form wins; ASR/OCR raw text is attached verbatim under ``_sources``).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

logger = logging.getLogger("GEO-Deal-Intake")

# 成交确认单十二字段(键名稳定,前端按 key 渲染中文标签)
SHEET_FIELDS: dict[str, str] = {
    "what_happened": "发生了什么",
    "deal_amount": "成交金额",
    "deal_time": "成交时间",
    "customer_industry": "客户行业",
    "service_content": "服务内容",
    "deal_reason": "成交原因",
    "customer_praise": "客户认可点",
    "quotable_lines": "可用原话",
    "image_materials": "图片素材",
    "privacy_redaction_items": "隐私遮挡项",
    "contact_and_qr": "联系方式与二维码",
    "target_channels": "目标渠道",
}

_FIELD_LIMITS = {field: 240 for field in SHEET_FIELDS}
_FIELD_LIMITS["quotable_lines"] = 500

PROVENANCE = "customer_asserted"


def _entry(value: str = "", status: str = "tentative") -> dict:
    return {
        "value": str(value or "").strip(),
        "status": status if status in {"confirmed", "tentative"} else "tentative",
        "provenance": PROVENANCE,
    }


def empty_sheet() -> dict:
    return {field: _entry() for field in SHEET_FIELDS}


# ---------------------------------------------------------------------------
# 确定性规则合并(LLM 不可用时的回落;表单优先,ASR/OCR 附原文)
# ---------------------------------------------------------------------------
# 金额必须带单位(万/千/百/元/块/rmb/人民币),避免把"2026年""3天"误当金额。
_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万|千|百)?\s*(元|块|块钱|rmb|人民币)?", re.IGNORECASE)
_DATE_RE = re.compile(r"(20\d{2}\s*[年\-/\.]\s*\d{1,2}\s*[月\-/\.]\s*\d{1,2}\s*日?|20\d{2}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日?)")
_INDUSTRY_RE = re.compile(r"(?:客户|对方|买家).{0,6}?(?:是|做|来自|属于)\s*([\u4e00-\u9fff]{2,12}?(?:行业|公司|店|厂|工作室|机构))")

# 中文数字层(简单映射,覆盖"五千块/三万"量级,不做全量中文数字解析器):
# 数字串内含十/百/千/万整体求值("五千"→5000,"十万"→100000,"两"=2)。
_CJK_DIGIT_VALUES = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CJK_MAGNITUDE_VALUES = {"十": 10, "百": 100, "千": 1000}
_CJK_AMOUNT_RE = re.compile(
    r"([零一二三四五六七八九十百千万两]{1,10})\s*(元|块|块钱|rmb|人民币)?", re.IGNORECASE,
)


def _parse_cjk_number(text: str) -> Optional[float]:
    """简单中文数字求值:零一二三四五六七八九两 + 十百千万 组合,非法返回 None。"""
    total = 0
    section = 0
    digit = 0
    seen = False
    for ch in str(text or ""):
        if ch in _CJK_DIGIT_VALUES:
            digit = _CJK_DIGIT_VALUES[ch]
        elif ch in _CJK_MAGNITUDE_VALUES:
            section += (digit or 1) * _CJK_MAGNITUDE_VALUES[ch]
            digit = 0
        elif ch == "万":
            section = (section + digit) * 10000
            total += section
            section = 0
            digit = 0
        else:
            return None
        seen = True
    if not seen:
        return None
    return float(total + section + digit)


def _amount_matches(text: str) -> list[tuple[str, str, str]]:
    matches = [
        (num, unit, currency)
        for num, unit, currency in _AMOUNT_RE.findall(str(text or ""))
        if unit or currency
    ]
    for num, currency in _CJK_AMOUNT_RE.findall(str(text or "")):
        if len(num) == 1 and num not in _CJK_DIGIT_VALUES:
            continue  # "千块"这类裸量词起点不是金额
        if not currency and (len(num) == 1 or num[-1] not in "十百千万"):
            # 无货币单位时须以量级字收尾("五千/十万"才算金额提法;
            # "万一/三百个"这类日常词不误判)。
            continue
        matches.append((num, "", currency))
    return matches


def _corpus(voice_transcript: str, ocr_texts: list[str]) -> str:
    parts = [str(voice_transcript or "").strip()]
    parts.extend(str(text or "").strip() for text in ocr_texts or [])
    return "\n".join(part for part in parts if part)


def merge_rule_based(
    form: Optional[dict] = None,
    voice_transcript: str = "",
    ocr_texts: Optional[list[str]] = None,
) -> dict:
    """Deterministic merge: form fields win; corpus fills obvious gaps tentative."""
    sheet = empty_sheet()
    form = form if isinstance(form, dict) else {}
    for field in SHEET_FIELDS:
        value = str(form.get(field) or "").strip()
        if value:
            sheet[field] = _entry(value[: _FIELD_LIMITS[field]], "confirmed")
    corpus = _corpus(voice_transcript, ocr_texts)
    if corpus:
        if not sheet["deal_amount"]["value"]:
            amounts = _amount_matches(corpus)
            if amounts:
                num, unit, currency = amounts[0]
                sheet["deal_amount"] = _entry(f"{num}{unit}{currency}", "tentative")
        if not sheet["deal_time"]["value"]:
            match = _DATE_RE.search(corpus)
            if match:
                sheet["deal_time"] = _entry(match.group(0), "tentative")
        if not sheet["customer_industry"]["value"]:
            match = _INDUSTRY_RE.search(corpus)
            if match:
                sheet["customer_industry"] = _entry(match.group(1), "tentative")
        if not sheet["what_happened"]["value"]:
            first_line = next((line.strip() for line in corpus.splitlines() if line.strip()), "")
            if first_line:
                sheet["what_happened"] = _entry(first_line[:240], "tentative")
    sheet["_sources"] = {
        "voice_transcript": str(voice_transcript or "")[:4000],
        "ocr_texts": [str(text or "")[:4000] for text in (ocr_texts or [])][:20],
    }
    sheet["_meta"] = {"extraction": "rule_fallback", "warnings": []}
    return sheet


# ---------------------------------------------------------------------------
# 输出标注(Owner 2026-07-22):提取值里出现语料不存在的具体数字/名称 →
# 保留值、标 tentative、_meta.warnings 追加提醒(含字段名),不再剔除。
# ---------------------------------------------------------------------------
def _canonical_amounts(text: str) -> set[float]:
    values: set[float] = set()
    for num, unit, _currency in _amount_matches(text):
        try:
            base = float(num)
        except (TypeError, ValueError):
            parsed = _parse_cjk_number(num)  # 中文数字层("五千"→5000)
            if parsed is None:
                continue
            base = parsed
        multiplier = {"万": 10000.0, "千": 1000.0, "百": 100.0}.get(unit, 1.0)
        values.add(base * multiplier)
    return values


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# 数字+紧邻单位上下文配对:折扣/百分比/量词都算单位。语料"3天交付" ≠ 编造"3折优惠"。
_NUMBER_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(%|折|万|亿|千|百|元|块|角|分|天|个|家|位|名|次|期|年|月|日|周|单|w|W|k|K)?"
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{3,}")
_CJK_NAME_RE = re.compile(r"[\u4e00-\u9fff]{1,3}(?:总|经理|老师|老板|女士|先生|姐|哥)")
_CJK_ORG_RE = re.compile(r"[\u4e00-\u9fff]{2,12}(?:公司|工作室|集团|门店|工厂|事务所)")
# 无头衔人名:"张伟说/李婷表示"这类 2-3 字中文名+言语动词提法同样对齐语料。
_CJK_BARE_NAME_RE = re.compile(r"([\u4e00-\u9fff]{2,3})(?:说|表示|反馈|评价|认可|回复|觉得)")


# 中文数字配对层(简单映射,非全量解析器):2 字以上数字串,或单字+强语境
# 单位(折/元/块/万/亿/%),避开"一个/一起"这类日常单字用法。命中后按
# _parse_cjk_number 转阿拉伯再配对——语料"3折"与提取"三折"同款放行;
# 跨表示边界(语料"5千块" vs 提取"五千块",阿拉伯层量词单位不折叠)仍判
# novel,方向 fail-closed(过 flag 不过漏)。
_CJK_NUMBER_UNIT_RE = re.compile(
    r"([零一二三四五六七八九十百千万两]{2,10}|[零一二三四五六七八九两](?=[%折元块万亿]))\s*"
    r"(%|折|万|亿|千|百|元|块|角|分|天|个|家|位|名|次|期|年|月|日|周|单|w|W|k|K)?"
)


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _number_unit_pairs(text: str) -> set[tuple[str, str]]:
    source = str(text or "")
    pairs = {(number, str(unit or "")) for number, unit in _NUMBER_UNIT_RE.findall(source)}
    for number, unit in _CJK_NUMBER_UNIT_RE.findall(source):
        if not unit and (len(number) == 1 or number[-1] not in "十百千万"):
            continue  # 无单位裸串须以量级字收尾,避开"万一"这类日常词
        canonical = _parse_cjk_number(number)
        if canonical is None:
            continue
        pairs.add((_format_number(canonical), str(unit or "")))
    return pairs


def _novel_tokens(value: str, corpus: str) -> list[str]:
    """Concrete numbers/identifiers/names present in value but not in corpus.

    数字按"数字+紧邻单位"配对校验:同一个数出现在语料但单位上下文不同
    (语料"3天" vs 编造"3折"、"5万" vs "5%")同样判 novel,裸子串匹配
    不再放行折扣/百分比改写。
    """
    corpus_compact = re.sub(r"\s+", "", str(corpus or ""))
    value_compact = re.sub(r"\s+", "", str(value or ""))
    novel: list[str] = []
    corpus_pairs = _number_unit_pairs(corpus)
    corpus_units_by_number: dict[str, set[str]] = {}
    for corpus_number, corpus_unit in corpus_pairs:
        corpus_units_by_number.setdefault(corpus_number, set()).add(corpus_unit)
    for number, unit in _number_unit_pairs(value_compact):
        if (number, unit) in corpus_pairs:
            continue
        if not unit and number in corpus_units_by_number and "" in corpus_units_by_number[number]:
            continue  # 语料同款裸数字,提取值也裸用 → 不算新事实
        novel.append(f"{number}{unit}")
    for token in _IDENTIFIER_RE.findall(value_compact):
        if token.lower() not in corpus_compact.lower():
            novel.append(token)
    for pattern in (_CJK_NAME_RE, _CJK_ORG_RE, _CJK_BARE_NAME_RE):
        for token in pattern.findall(value_compact):
            if token not in corpus_compact:
                novel.append(token)
    return novel


def _annotate_extracted(data: dict, *, corpus: str, base: dict) -> dict:
    """保留 AI 提取值;语料外内容标 tentative 并落 warnings(提醒不剔除)。

    Owner 2026-07-22 合规口径:防编造从硬剔除改为醒目标注——系统提醒
    "AI 提取内容超出你提供的材料,请核对",不阻断、不静默改写。金额
    canonical 不一致同样只标注不剔除。用户已 confirmed 的表单值仍不被
    LLM 覆盖(合并语义,非合规审查);tentative 字段永不进发布面 facts。
    """
    sheet = {field: dict(base.get(field) or _entry()) for field in SHEET_FIELDS}
    corpus_amounts = _canonical_amounts(corpus)
    warnings: list[dict] = []
    for field in SHEET_FIELDS:
        raw = data.get(field)
        value = str((raw.get("value") if isinstance(raw, dict) else raw) or "").strip()
        if not value:
            continue
        value = value[: _FIELD_LIMITS[field]]
        # 表单已确认的值不被 LLM 覆盖;提取结果一律 tentative 待人确认。
        if (base.get(field) or {}).get("status") == "confirmed":
            continue
        novel = _novel_tokens(value, corpus)
        amounts = _canonical_amounts(value)
        amount_mismatch = bool(amounts) and not amounts.issubset(corpus_amounts)
        if novel or amount_mismatch:
            # 五问合同(SSOT §6):warning_entry 带 reason/repair_hint/actions/rule_version。
            from services.marketing.content_center import warning_entry

            warnings.append(warning_entry(
                "ai_extraction_beyond_materials",
                field=field,
                message=f"AI 提取的「{SHEET_FIELDS[field]}」超出了你提供的材料，请核对",
            ))
        sheet[field] = _entry(value, "tentative")
    sheet["_meta"] = {"extraction": "ai", "warnings": warnings}
    return sheet


# ---------------------------------------------------------------------------
# LLM 结构化提取(fail-soft;无 key/异常 → 纯规则合并)
# ---------------------------------------------------------------------------
_EXTRACTION_PROMPT = (
    "你是成交素材整理助手。从用户提供的表单、语音转写和图片 OCR 文本中,提取一张「成交确认单」。\n"
    "严格返回一个 JSON 对象,键为以下 12 个英文字段,每个字段的值是字符串;不知道就返回空字符串:\n"
    "what_happened(发生了什么)/deal_amount(成交金额)/deal_time(成交时间)/customer_industry(客户行业)/"
    "service_content(服务内容)/deal_reason(成交原因)/customer_praise(客户认可点)/quotable_lines(可用原话)/"
    "image_materials(图片素材说明)/privacy_redaction_items(需要遮挡的隐私项)/"
    "contact_and_qr(联系方式与二维码说明)/target_channels(目标渠道)。\n"
    "铁律:只能提取输入材料里明确出现的具体数字、金额、客户名称、订单号和付款渠道;"
    "材料里没有的一律留空,绝不推测、绝不补造、绝不扩大金额;可用原话必须逐字摘自材料。"
)


async def extract_sheet(
    *,
    form: Optional[dict] = None,
    voice_transcript: str = "",
    ocr_texts: Optional[list[str]] = None,
) -> dict:
    """Merge the three inputs into the deal sheet; LLM extraction when available."""
    base = merge_rule_based(form, voice_transcript, ocr_texts)
    corpus = _corpus(voice_transcript, ocr_texts)
    if not corpus.strip():
        return base
    call_llm = None
    try:
        from tools.multi_llm_caller import call_llm_with_fallback

        call_llm = call_llm_with_fallback
    except Exception:
        call_llm = None
    if call_llm is None:
        return base
    form_text = "\n".join(
        f"{SHEET_FIELDS[field]}: {str(form.get(field) or '')[:240]}"
        for field in SHEET_FIELDS
        if isinstance(form, dict) and str(form.get(field) or "").strip()
    )
    prompt = (
        f"{_EXTRACTION_PROMPT}\n\n"
        f"【表单已填字段】\n{form_text or '(空)'}\n\n"
        f"【语音转写原文】\n{str(voice_transcript or '')[:4000] or '(无)'}\n\n"
        f"【图片 OCR 原文】\n{corpus[:8000] or '(无)'}"
    )
    try:
        raw = await call_llm(prompt, verbose=False)
        if raw:
            import json_repair

            data = json_repair.loads(raw)
            if isinstance(data, dict):
                sheet = _annotate_extracted(data, corpus=corpus, base=base)
                sheet["_sources"] = base["_sources"]
                return sheet
    except Exception as exc:  # noqa: BLE001
        logger.warning("[deal_intake] LLM 提取失败(回落规则合并): %s", exc)
    return base


def merge_sheet_preserving_confirmed(existing: dict, extracted: dict) -> dict:
    """analyze 重写 sheet 前的合并:用户已 confirmed 的字段不被重提取覆盖。

    重跑 analyze(补传素材/语音后)只刷新 tentative 字段与 _sources/_meta;
    用户在确认单上点过确认的字段原值保留——这是乐观锁之外防 lost-update
    的第二道(乐观锁挡并发交错,这道挡"先确认后重跑"的顺序操作)。
    """
    merged = dict(extracted or {})
    for field in SHEET_FIELDS:
        prior = (existing or {}).get(field)
        if not isinstance(prior, dict):
            continue
        if str(prior.get("status") or "") == "confirmed" and str(prior.get("value") or "").strip():
            merged[field] = dict(prior)
    return merged


# ---------------------------------------------------------------------------
# 确认单 → 生成侧装配(strategy 八字段 + 证据 facts,全部 customer_asserted)
# ---------------------------------------------------------------------------
def _sheet_value(sheet: dict, field: str) -> str:
    entry = (sheet or {}).get(field) or {}
    return str(entry.get("value") or "").strip()


def _confirmed(sheet: dict, field: str) -> bool:
    entry = (sheet or {}).get(field) or {}
    return bool(entry.get("value")) and entry.get("status") == "confirmed"


# 匿名晒单:确认单进 strategy/facts 前对客户名类提法做服务端掩码,
# 与 build_visual_brief 的"不出现品牌名/客户名"约束同口径。
_MASK_NAME_RE = re.compile(r"[一-龥]{1,3}(?:总|经理|老板|女士|先生|姐|哥|老师)")
_MASK_ORG_RE = re.compile(r"[一-龥]{2,12}(?:公司|集团|工作室|门店|工厂|事务所)")
# ASCII 人名/英文头衔(2026-07-23 外部审查 P1-4,与 redaction.py 同口径):
# "Alice Smith (CEO)"、"Mr. Zhang"、"张伟 CEO" 及裸两段大写英文名。
_MASK_ASCII_EXEC_TITLE = r"(?:CEO|CTO|COO|CFO|CMO|CIO|VP|GM|Manager|Director|President|Chairman|Founder)"
_MASK_ASCII_NAME_WITH_TITLE_RE = re.compile(
    r"[A-Z][A-Za-z]{1,19}(?:\s+[A-Z][A-Za-z]{1,19})?\s*[,(]?\s*" + _MASK_ASCII_EXEC_TITLE + r"\b\)?"
    r"|(?:Mr|Ms|Mrs|Miss|Dr|Prof)\.?\s+[A-Z][A-Za-z]{1,19}(?:\s+[A-Z][A-Za-z]{1,19})?"
    r"|[一-龥]{2,4}\s*" + _MASK_ASCII_EXEC_TITLE + r"\b"
)
_MASK_ASCII_BARE_NAME_RE = re.compile(r"\b[A-Z][a-z]{1,19}\s+[A-Z][a-z]{1,19}\b")


def mask_customer_names(text: str, *, extra_terms: Optional[list[str]] = None) -> str:
    """匿名掩码:冻结品牌名等字面词 + 客户公司/头衔人名/无头衔人名 + ASCII 人名/头衔。

    ``extra_terms``(如冻结快照 brand.name)按字面子串掩码,ASCII 大小写
    不敏感("OmniRank"/"omnirank" 同口径),替换成"服务商";无头衔 CJK 人名
    借 _CJK_BARE_NAME_RE 的言语动词语境模式("张伟说"→"客户说");ASCII 侧
    "Alice Smith (CEO)"/"Mr. Zhang"/"张伟 CEO"/裸两段大写英文名与打码引擎
    (redaction.py)同模式掩为"客户"。
    残余边界(明示不掩,fail-open 方向):无语境裸人名("张伟"单独立提)、
    无后缀公司名("和阿里签的合同")、谐音/变形改写;同数字同单位语义偷换
    (语料"3天交付"→文案"3天退款")该层不可检测,靠确认环节人审兜底。
    """
    masked = str(text or "")
    for term in extra_terms or []:
        term = str(term or "").strip()
        if len(term) >= 2:  # 单字词掩码误伤面太大,不掩
            masked = re.sub(re.escape(term), "服务商", masked, flags=re.IGNORECASE)
    masked = _MASK_ORG_RE.sub("客户公司", masked)
    masked = _MASK_NAME_RE.sub("客户", masked)
    masked = _MASK_ASCII_NAME_WITH_TITLE_RE.sub("客户", masked)
    masked = _MASK_ASCII_BARE_NAME_RE.sub("客户", masked)
    return _CJK_BARE_NAME_RE.sub(lambda m: "客户" + m.group(0)[len(m.group(1)):], masked)


_FACT_LABELS = {
    "what_happened": "成交事实",
    "deal_amount": "成交金额",
    "deal_time": "成交时间",
    "customer_industry": "客户行业",
    "service_content": "服务内容",
    "customer_praise": "客户认可点",
}


def facts_from_sheet(sheet: dict, *, anonymize: bool = False,
                     mask_terms: Optional[list[str]] = None) -> list[dict]:
    """Confirmed sheet fields become evidence facts; tentative never published.

    ``anonymize=True``(匿名晒单)对客户名/客户公司名提法做服务端掩码,
    冻结进 facts 的就不再是可还原的客户身份;``mask_terms`` 追加字面词
    掩码(如冻结快照 brand.name,含 ASCII,大小写不敏感)。
    """
    facts: list[dict] = []
    for field, label in _FACT_LABELS.items():
        if _confirmed(sheet, field):
            value = _sheet_value(sheet, field)
            if anonymize:
                value = mask_customer_names(value, extra_terms=mask_terms)
            facts.append({
                "key": field,
                "label": label,
                "value": value[:120],
                "provenance": PROVENANCE,
            })
    return facts


def strategy_from_sheet(sheet: dict, *, anonymize: bool = False,
                        mask_terms: Optional[list[str]] = None) -> dict:
    """Assemble the frozen eight strategy fields from the deal sheet.

    Gaps are filled with generic non-committal wording — never with invented
    specifics.  Every value derives from customer-asserted material.
    ``anonymize=True`` 时客户名类提法先过服务端掩码再装配;``mask_terms``
    追加字面词掩码(如冻结快照 brand.name)。
    """
    def _value(field: str) -> str:
        text = _sheet_value(sheet, field)
        return mask_customer_names(text, extra_terms=mask_terms) if anonymize else text

    service = _value("service_content")
    industry = _value("customer_industry")
    what = _value("what_happened")
    reason = _value("deal_reason")
    praise = _value("customer_praise")
    amount = _value("deal_amount") if _confirmed(sheet, "deal_amount") else ""
    deal_time = _value("deal_time") if _confirmed(sheet, "deal_time") else ""

    audience = f"正在考虑{service}的{industry}客户" if service and industry else (
        f"正在考虑{service}的潜在客户" if service else "和这位客户情况相似、正在观望的潜在客户"
    )
    human_problem = what or "客户在做决定前,想先看到一笔真实成交而不只是听介绍"
    core_angle = reason or "用一笔真实成交的完整过程,代替夸张的效果宣传"
    single_value = praise or service or "把成交过程和客户认可点如实讲清楚"
    amount_part = f"成交金额{amount}" if amount else "成交金额以客户确认单为准"
    time_part = f",{deal_time}" if deal_time else ""
    evidence_statement = (
        f"客户自述成交事实({amount_part}{time_part}),未经平台核验,生成时已冻结"
    )
    return {
        "audience": audience[:240],
        "audience_status": "潜在客户决策前会主动寻找真实成交证据"[:240],
        "action_resistance": "担心服务商夸大效果、没有可核验的真实交付"[:240],
        "human_problem": human_problem[:240],
        "core_angle": core_angle[:240],
        "single_value": single_value[:240],
        "evidence_statement": evidence_statement[:240],
        "single_action": "了解同类服务的真实交付过程"[:240],
        "source_brief": f"晒成交:{what[:200]}" if what else "晒成交",
        "provenance": PROVENANCE,
        "source": "deal_sheet",
    }


# ---------------------------------------------------------------------------
# 语音转写(复用仓内 ASR:豆包优先,DashScope+ffmpeg 回落;与原采访 router 同口径)
# ---------------------------------------------------------------------------
async def transcribe_voice_bytes(audio_bytes: bytes, *, suffix: str = ".webm") -> dict:
    """Transcribe uploaded audio; fail-soft with an explicit status string."""
    import os
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(tmp_fd, audio_bytes)
    finally:
        os.close(tmp_fd)
    try:
        try:
            from tools.asr.doubao_streaming_asr import is_available as doubao_available

            if doubao_available():
                from tools.asr.doubao_streaming_asr import transcribe_file

                result = await transcribe_file(tmp_path)
                text = str((result or {}).get("text") or "").strip()
                return {"text": text, "status": "success" if text else str((result or {}).get("status") or "empty"), "provider": "doubao"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[deal_intake] 豆包 ASR 失败(回落 DashScope): %s", exc)

        import subprocess

        wav_path = tmp_path + ".wav"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path, "-ar", "16000", "-ac", "1", wav_path],
                capture_output=True, timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[deal_intake] ffmpeg 转码失败: %s", exc)
        use_path = wav_path if os.path.exists(wav_path) else tmp_path

        def _whisper() -> dict:
            from tools.asr.asr_tool import transcribe_audio_sync

            return transcribe_audio_sync(use_path)

        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _whisper)
            text = str((result or {}).get("text") or "").strip()
            return {
                "text": text,
                "status": "success" if text else str((result or {}).get("status") or "empty"),
                "provider": "dashscope-qwen3-asr",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[deal_intake] DashScope ASR 失败: %s", exc)
            return {"text": "", "status": f"error: {exc}"[:200], "provider": "dashscope-qwen3-asr"}
    finally:
        for path in (tmp_path, tmp_path + ".wav"):
            try:
                os.unlink(path)
            except OSError:
                pass
