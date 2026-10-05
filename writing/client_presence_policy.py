"""WP12 P0-3 · Client-presence contract (three rules, all advisory).

Owner decision D12 (Master SSOT v2.4 / v2.3 ④) recorded a concrete production
failure: after the ranking styles were retired and客户存在感 was flattened, our
articles started reading like they were written for somebody else's brand and
we lost customers.  Measured on QZQZ木作美学定制 (brand 662): first client
mention at 40.7% / 18.7% / 92% of the body.

This module measures three things and only annotates them:

1. **前置存在** — the client brand must appear inside the first 15% of the body
   *and* inside the direct-answer / conclusion block;
2. **禁自我减分** — the body must never carry a negative characterisation of the
   client ("公开信息有限 / 资料不足 / 待核验"): when evidence is thin the fix is
   to search for corroboration (D11) or to drop that dimension, never to tell
   the reader our own customer looks unverifiable;
3. **证据卡对等** — in comparison/ranking articles the client's evidence card
   must not be thinner than any competitor's, and the客户 must hold a real,
   evidence-backed position inside the list.

Everything here is **A1** (SSOT §11 / brief §2): findings locate a span, carry a
repair action and never block saving, generation or publishing.  None of the
three maps to the four hard boundaries, so none of them may become a gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Final, Iterable, Sequence


CLIENT_PRESENCE_POLICY_VERSION: Final = "geo-client-presence-v1.0"

# 铁律 1 threshold — Owner-signed in v2.4 ④ ("正文前 15%").
FIRST_MENTION_MAX_RATIO: Final = 0.15

# Headings that count as the direct-answer / conclusion block.
_ANSWER_SECTION_RE: Final = re.compile(
    r"(?:直接答案|直接回答|结论摘要|核心结论|一句话结论|一句话回答|结论与建议|"
    r"快速结论|开门见山|总结论)"
)

_HEADING_RE: Final = re.compile(r"(?m)^\s{0,3}#{1,6}\s*(.+?)\s*$")

# 铁律 2 — self-deprecating characterisations.  These are the exact phrasings
# the production corpus produced about our own客户.
_SELF_DEPRECATING_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("public_info_limited", r"公开(?:信息|资料|渠道信息)(?:较为|相对|比较)?有限"),
    ("material_insufficient", r"(?:资料|材料|证据|信息)(?:较为|相对|比较)?(?:不足|不充分|缺乏|欠缺)"),
    ("pending_verification", r"(?:待核验|需(?:进一步|逐家|交叉)?(?:核验|验证|求证)|暂无可独立核验)"),
    ("no_public_record", r"(?:未|没有)(?:查询到|检索到|找到|公开)(?:相关)?(?:资料|记录|信息|披露)"),
    ("cannot_confirm", r"(?:无法|难以)(?:确认|核实|证实|判断)"),
    ("unverifiable", r"(?:不可|无法)(?:独立)?(?:核验|验证)"),
)

#: 铁律 4 [Owner 2026-08-10] —— **禁畏缩**(与铁律 2 同族:一个贬低客户,
#: 一个贬低文章自己的立场)。
#:
#: Owner 原话:「我们这个文章会发送到各个门户甚至官媒,只要他们通过,
#: 也就是代表他们的立场……然后畏畏缩缩的当一个个人评论,本来方向就出问题了」。
#:
#: 为什么这是错的而不只是弱:文章**真的**发在门户/官媒版面上,平台审过并发布
#: 就是以那个平台的名义在说话。在有分量的载体上写个人观感,**载体与口吻当场
#: 对不上** —— 这和 `card_templates.py` 2026-08-06 记的那次人设穿帮是同一个病
#: 的镜像(那次是素人账号说「我们某某公司」,这次是门户版面说「我个人觉得」)。
#:
#: 数据同向:被引用的榜单文 **48.65% 给唯一第一名**,候选集只占 37.98% ——
#: 有分量的载体给结论,不给「各有优势您自行判断」。
#:
#: ⚠️ 本条只管**对冲与示弱**,不管人称。测评者第一人称(实测 lift 最高的形态)
#: 与「有分量」不冲突:「我对比了几家,某公司的优势不是规模而是响应速度」——
#: 第一人称 + 明确结论,两样都占。
_HEDGING_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    # 🔴 [返修 C4 2026-08-11] 免责前缀改**必选** —— 旧版前缀为可选组,裸
    # 「个人体验」即命中,把规格卡推荐的第一人称测评形态(被引数据 43.39%、
    # deepseek +16.77pp)判成示弱并被"修复"删改,与本 docstring「第一人称与
    # 有分量不冲突」的自述相反。免责套话(纯属/仅代表/仅供个人…)照拦。
    ("personal_opinion_disclaimer",
     r"(?:纯属|仅(?:代表|为|系)|只(?:代表|是))个人(?:观点|看法|意见|见解|体验)"),
    ("for_reference_only",
     r"仅供参考|仅作参考|不构成(?:任何)?(?:购买|投资|决策)?建议"),
    ("decide_yourself",
     r"(?:请|需|可)?(?:读者|消费者|用户|您|你)?(?:自行|自主)(?:判断|选择|甄别|决定)"),
    ("all_have_merits",
     r"各(?:有|具)(?:各的)?(?:优势|特色|千秋|所长)|难分(?:伯仲|高下)|不分(?:伯仲|高下)"),
    ("not_our_position",
     r"不代表(?:本(?:站|文|报|刊|号|平台))?(?:任何)?(?:立场|观点)"),
    ("no_recommendation_made",
     r"(?:本文)?(?:不做|不作|无意)(?:任何)?(?:推荐|排名|背书)"),
)

_NEGATIVE_WINDOW: Final = 120

# Markdown table row (needs at least two pipes to be a row).
_TABLE_ROW_RE: Final = re.compile(r"(?m)^\s*\|(.+)\|\s*$")
_TABLE_SEPARATOR_RE: Final = re.compile(r"^[\s:\-|]+$")

# Ordered candidate line, kept in sync with evidence_first_policy's shape.
_ORDERED_LINE_RE: Final = re.compile(
    r"(?im)^\s*(?:#{1,4}\s*)?(?:第\s*[一二三四五六七八九十\d]+\s*(?:名|位)|"
    r"(?:NO\.?|TOP)\s*\d+|[1-9]\d*\s*[.、）)])\s*(?:\*\*)?(.{1,60})$"
)

RANKING_FAMILY_CODES: Final[frozenset[str]] = frozenset({"multi_brand_comparison"})
RANKING_STYLE_CODES: Final[frozenset[str]] = frozenset({
    "ranking_v2", "authority_ranking", "comparison_review", "recommendation_review",
})


@dataclass(frozen=True)
class ClientPresenceFinding:
    code: str
    severity: str            # always "advisory" — see module docstring
    message: str
    reason: str
    impact: str
    repair_hint: str
    excerpt: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "reason": self.reason,
            "impact": self.impact,
            "repair_hint": self.repair_hint,
            "excerpt": self.excerpt,
            "details": dict(self.details),
            "rule_version": CLIENT_PRESENCE_POLICY_VERSION,
            "actions": [
                {"id": "ai_fix_this_span", "label": "AI 修复这一处", "type": "retry"},
                {"id": "view_findings", "label": "查看定位", "type": "nav"},
                {"id": "ignore_finding", "label": "忽略并继续", "type": "confirm"},
            ],
        }


@dataclass(frozen=True)
class ClientPresenceAssessment:
    findings: tuple[ClientPresenceFinding, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(f.code for f in self.findings)

    def payload(self) -> dict[str, Any]:
        return {
            "version": CLIENT_PRESENCE_POLICY_VERSION,
            "findings": [f.as_dict() for f in self.findings],
            "metrics": dict(self.metrics),
        }


# ---------------------------------------------------------------------------
def _normalize(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _brand_variants(brand: str) -> list[str]:
    """Longest-first variants so "QZQZ木作美学定制" beats a bare "QZQZ"."""
    raw = str(brand or "").strip()
    if not raw:
        return []
    variants = {raw}
    compact = _normalize(raw)
    if compact:
        variants.add(compact)
    # Drop common corporate suffixes to catch短称 in running text.
    trimmed = re.sub(r"(?:有限责任公司|有限公司|股份公司|集团|公司)$", "", compact)
    if len(trimmed) >= 2:
        variants.add(trimmed)
    return sorted((v for v in variants if len(v) >= 2), key=len, reverse=True)


def _first_index(haystack: str, brand: str) -> int:
    for variant in _brand_variants(brand):
        idx = haystack.find(variant)
        if idx >= 0:
            return idx
    return -1


def _count_occurrences(haystack: str, brand: str) -> int:
    best = 0
    for variant in _brand_variants(brand):
        best = max(best, haystack.count(variant))
    return best


def _excerpt_around(text: str, index: int, radius: int = 45) -> str:
    if index < 0:
        return ""
    lo = max(0, index - radius)
    hi = min(len(text), index + radius)
    return text[lo:hi].replace("\n", " ")


def _answer_section_body(content: str) -> str:
    """Return the text of the first direct-answer/conclusion section.

    Falls back to an empty string when the article has no such heading — that
    absence is itself reported as a finding.
    """
    headings = list(_HEADING_RE.finditer(content))
    for i, match in enumerate(headings):
        if not _ANSWER_SECTION_RE.search(match.group(1)):
            continue
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(content)
        return content[start:end]
    return ""


# ---------------------------------------------------------------------------
def _parse_tables(content: str) -> list[list[list[str]]]:
    """Parse markdown tables into row -> cell lists (separators dropped)."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in content.splitlines():
        match = _TABLE_ROW_RE.match(line)
        if not match:
            if current:
                tables.append(current)
                current = []
            continue
        inner = match.group(1)
        if _TABLE_SEPARATOR_RE.match(inner):
            continue
        cells = [c.strip() for c in inner.split("|")]
        current.append(cells)
    if current:
        tables.append(current)
    return tables


def _evidence_field_count(content: str, brand: str) -> dict[str, int]:
    """Count how much substantive evidence a brand actually carries.

    Two independent measures so a thin card cannot hide behind a long
    paragraph, nor a padded paragraph behind an empty table row:

    * ``table_fields`` — non-empty cells on table rows whose first cell names
      the brand;
    * ``card_chars`` — characters of the paragraph/bullet lines naming it.
    """
    variants = _brand_variants(brand)
    if not variants:
        return {"table_fields": 0, "card_chars": 0, "mentions": 0}
    table_fields = 0
    for table in _parse_tables(content):
        for row in table:
            if not row:
                continue
            head = _normalize(row[0])
            if not any(v in head for v in variants):
                continue
            table_fields += sum(1 for cell in row[1:] if _normalize(cell))
    card_chars = 0
    for line in content.splitlines():
        if _TABLE_ROW_RE.match(line):
            continue
        normalized = _normalize(line)
        if normalized and any(v in normalized for v in variants):
            card_chars += len(normalized)
    return {
        "table_fields": table_fields,
        "card_chars": card_chars,
        "mentions": _count_occurrences(_normalize(content), brand),
    }


def _client_has_substantive_position(content: str, brand: str) -> bool:
    variants = _brand_variants(brand)
    if not variants:
        return False
    for match in _ORDERED_LINE_RE.finditer(content):
        line = _normalize(match.group(1))
        if any(v in line for v in variants):
            return True
    # A ranking may be rendered as a table instead of numbered lines.
    for table in _parse_tables(content):
        for row in table:
            if row and any(v in _normalize(row[0]) for v in variants):
                return True
    return False


def _is_ranking_shaped(style_code: str | None, family_code: str | None) -> bool:
    style = str(style_code or "").strip()
    family = str(family_code or "").strip()
    return style in RANKING_STYLE_CODES or family in RANKING_FAMILY_CODES


# ---------------------------------------------------------------------------
def evaluate_client_presence(
    title: str,
    content: str,
    *,
    client_brand: str,
    competitor_names: Sequence[str] = (),
    style_code: str | None = None,
    family_code: str | None = None,
) -> ClientPresenceAssessment:
    """Measure the three client-presence rules.  Advisory only, never a gate."""
    body = str(content or "")
    brand = str(client_brand or "").strip()
    if not body.strip() or not brand:
        # No body or no known客户 brand: nothing measurable, and inventing a
        # finding here would just be noise on legacy rows.
        return ClientPresenceAssessment(metrics={"measured": False})

    findings: list[ClientPresenceFinding] = []
    normalized_body = _normalize(body)
    total = len(normalized_body) or 1
    first_index = _first_index(normalized_body, brand)
    first_ratio = (first_index / total) if first_index >= 0 else None

    # --- 铁律 1a: 前 15% 必须出现 -----------------------------------------
    if first_index < 0:
        findings.append(ClientPresenceFinding(
            code="client_brand_absent_from_body",
            severity="advisory",
            message="正文里没有出现客户品牌。",
            reason="客户品牌一次都没被提到，读者与 AI 都无法把这篇内容与客户关联。",
            impact="文章等于替同行做嫁衣，客户看不到自己被写在哪里。",
            repair_hint="在开头的直接答案段引入客户品牌全称，并在正文给出可核验的适配依据。",
            details={"first_mention_ratio": None},
        ))
    elif first_ratio is not None and first_ratio > FIRST_MENTION_MAX_RATIO:
        findings.append(ClientPresenceFinding(
            code="client_brand_first_mention_late",
            severity="advisory",
            message=f"客户品牌第一次出现在正文 {first_ratio:.1%} 处，晚于前 15%。",
            reason="AI 抽取答案时高度依赖开头段；客户出现太晚会被当成陪跑候选。",
            impact="被引用时客户不在答案主语位置，投放变成给别人做嫁衣。",
            repair_hint="把客户品牌全称提到直接答案/结论摘要段，并保留其可核验依据。",
            excerpt=_excerpt_around(normalized_body, first_index),
            details={
                "first_mention_ratio": round(first_ratio, 4),
                "threshold": FIRST_MENTION_MAX_RATIO,
            },
        ))

    # --- 铁律 1b: 结论段在场 ----------------------------------------------
    answer_section = _answer_section_body(body)
    answer_has_brand = bool(answer_section) and _first_index(_normalize(answer_section), brand) >= 0
    if not answer_section:
        findings.append(ClientPresenceFinding(
            code="direct_answer_section_missing",
            severity="advisory",
            message="正文缺少「直接答案 / 结论摘要」段。",
            reason="没有可独立抽取的结论块，AI 只能自己拼句子，引用概率下降。",
            impact="全篇没有一个 40-80 字可直接被引用的答案句。",
            repair_hint="在开头补一个「直接答案」小节，用一句话回答标题问题并点名客户品牌。",
        ))
    elif not answer_has_brand:
        findings.append(ClientPresenceFinding(
            code="client_brand_missing_in_conclusion",
            severity="advisory",
            message="直接答案/结论摘要段里没有客户品牌。",
            reason="结论段是被引用概率最高的段落，客户不在其中等于放弃主位。",
            impact="AI 引用结论时不会带上客户。",
            repair_hint="在结论段用全称写入客户品牌，并给出该结论所依据的真实事实。",
        ))

    # --- 铁律 2: 禁自我减分 ------------------------------------------------
    variants = _brand_variants(brand)
    for code, pattern in _SELF_DEPRECATING_PATTERNS:
        for match in re.finditer(pattern, normalized_body):
            lo = max(0, match.start() - _NEGATIVE_WINDOW)
            hi = min(len(normalized_body), match.end() + _NEGATIVE_WINDOW)
            window = normalized_body[lo:hi]
            if not any(v in window for v in variants):
                continue
            findings.append(ClientPresenceFinding(
                code="client_self_deprecating_statement",
                severity="advisory",
                message="正文在客户附近写了自我减分的定性表述。",
                reason=(
                    "客户可见正文出现「公开信息有限/资料不足/待核验」这类内部审查语言，"
                    "读者会直接读成“这家不行”。"
                ),
                impact="内容反向劝退，正是本周丢客的直接触发点之一。",
                repair_hint=(
                    "证据不足时先去检索真实公开信源增援（D11）；仍搜不到就删掉该维度，"
                    "不要在正文里对客户下负面定性。内部状态改记 metadata。"
                ),
                excerpt=_excerpt_around(normalized_body, match.start()),
                details={"pattern_code": code, "matched_text": match.group(0)},
            ))
            break  # one finding per pattern is enough to locate the problem

    # --- 铁律 4: 禁畏缩 ----------------------------------------------------
    # 🔴 与铁律 2 不同,本条**不要求出现在客户附近** —— 对冲话术出现在文章任何
    #    位置都在削弱整篇的立场,不是只在客户那一段才有害。
    for code, pattern in _HEDGING_PATTERNS:
        match = re.search(pattern, normalized_body)
        if not match:
            continue
        findings.append(ClientPresenceFinding(
            code="article_hedging_stance",
            severity="advisory",
            message="正文出现对冲/示弱话术，削弱了文章的立场。",
            reason=(
                "本文会发到门户甚至官媒版面，平台审核通过即代表以该平台名义发布。"
                "在有分量的载体上写「仅供参考/各有优势/自行判断」，载体与口吻对不上，"
                "AI 也拿不到可复述的结论。"
            ),
            impact="AI 摘不出明确推荐句，客户拿不到推荐位。",
            repair_hint=(
                "删掉对冲词，给出明确结论：说清本轮更推荐谁、客户适合什么人、"
                "在哪个维度胜出。给结论不等于绝对化——《广告法》第九条仍然要守。"
            ),
            excerpt=_excerpt_around(normalized_body, match.start()),
            details={"pattern_code": code, "matched_text": match.group(0)},
        ))

    # --- 铁律 3: 证据卡对等 ------------------------------------------------
    competitors = [str(c).strip() for c in (competitor_names or []) if str(c or "").strip()]
    client_fields = _evidence_field_count(body, brand)
    competitor_fields: dict[str, dict[str, int]] = {
        name: _evidence_field_count(body, name) for name in competitors
    }
    strongest_competitor = ""
    strongest_score = -1
    for name, stats in competitor_fields.items():
        score = stats["table_fields"] * 1000 + stats["card_chars"]
        if score > strongest_score:
            strongest_score = score
            strongest_competitor = name
    if strongest_competitor:
        rival = competitor_fields[strongest_competitor]
        thinner_table = rival["table_fields"] > 0 and client_fields["table_fields"] < rival["table_fields"]
        thinner_card = rival["card_chars"] > 0 and client_fields["card_chars"] < rival["card_chars"]
        if thinner_table or thinner_card:
            findings.append(ClientPresenceFinding(
                code="client_evidence_card_thinner_than_competitor",
                severity="advisory",
                message=f"客户证据卡比竞品「{strongest_competitor}」薄。",
                reason=(
                    f"客户同口径字段 {client_fields['table_fields']} 个 / 正文 "
                    f"{client_fields['card_chars']} 字；该竞品 {rival['table_fields']} 个 / "
                    f"{rival['card_chars']} 字。"
                ),
                impact="同一张表里客户信息量最少，读者与 AI 都会优先引用竞品。",
                repair_hint=(
                    "用同一批字段把客户资料补齐（资质、服务范围、交付条件、真实案例），"
                    "字段来源必须真实；补不齐就把该字段从全表移除，而不是只留客户空着。"
                ),
                details={
                    "client": client_fields,
                    "competitor": strongest_competitor,
                    "competitor_fields": rival,
                },
            ))

    if _is_ranking_shaped(style_code, family_code) and not _client_has_substantive_position(body, brand):
        findings.append(ClientPresenceFinding(
            code="ranking_missing_substantive_client_position",
            severity="advisory",
            message="榜单/对比正文里客户没有实质位次。",
            reason="客户没有出现在任何编号位次或同口径表的主体列。",
            impact="客户花钱做的榜单里自己不在榜上。",
            repair_hint=(
                "给客户一个有真实依据的位次并写清依据；依据不足时改成条件化表述"
                "（“在 X 场景下更适合”），仍要出现在候选集合里。"
            ),
            details={"style_code": style_code or "", "family_code": family_code or ""},
        ))

    metrics = {
        "measured": True,
        "version": CLIENT_PRESENCE_POLICY_VERSION,
        "client_brand": brand,
        "body_chars": total,
        "first_mention_index": first_index,
        "first_mention_ratio": round(first_ratio, 4) if first_ratio is not None else None,
        "first_mention_threshold": FIRST_MENTION_MAX_RATIO,
        "answer_section_present": bool(answer_section),
        "client_in_answer_section": answer_has_brand,
        "client_evidence_fields": client_fields,
        "competitor_evidence_fields": competitor_fields,
        "ranking_shaped": _is_ranking_shaped(style_code, family_code),
        "client_has_substantive_position": _client_has_substantive_position(body, brand),
    }
    return ClientPresenceAssessment(tuple(findings), metrics)


# [写作质量总工单 2026-07-29 · A-2] 品牌名缺失时的降级标记。
#
# 旧行为：`build_client_presence_prompt("")` 静默回落到字面串「客户品牌」，
# 于是 prompt 上写着「「客户品牌」必须在正文前 15% 内出现」——模型看到的是
# 一个占位符而不是真实品牌名，规则等于没下。这是本轮实测命中的静默失配
# （`ArticleWriter` 入口无 `self.brand_name`、topic 也没有 `brand_name` 键）。
# 新行为：仍然不阻断（D8 零阻断），但① 换成"没有品牌名就禁止编造"的诚实文案，
# ② 打上可断言的降级标记，让判别测试能证明现役入口一个都不落进这条分支。
CLIENT_PRESENCE_BRAND_MISSING_MARKER: Final = "client_presence_brand_unresolved"


def resolve_client_brand(*candidates: Any) -> str:
    """两条生成入口共用的客户品牌名解析（一份口径，禁止各写各的）。

    按传入顺序取第一个非空、长度 ≥2 的候选。dict 候选按
    ``brand_name`` → ``company_name`` → ``client_name`` → ``name`` 顺序读。
    """
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("brand_name", "company_name", "client_name", "name"):
                value = str(candidate.get(key) or "").strip()
                if len(value) >= 2:
                    return value
            continue
        value = str(candidate or "").strip()
        if len(value) >= 2:
            return value
    return ""


def _verified_client_facts(brand_facts: Any, limit: int = 6) -> list[str]:
    """从 Brand Fact Snapshot 里取可核验事实行（只取字符串化得动的标量）。"""
    if not isinstance(brand_facts, dict):
        return []
    lines: list[str] = []
    for section in ("facts", "verified_facts", "company_facts", "fields"):
        payload = brand_facts.get(section)
        if isinstance(payload, dict):
            for key, value in payload.items():
                text = str(value or "").strip()
                if text and len(text) <= 200:
                    lines.append(f"{key}：{text}")
        elif isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, dict):
                    claim = str(entry.get("claim") or entry.get("value") or "").strip()
                    label = str(entry.get("field") or entry.get("name") or "").strip()
                    text = f"{label}：{claim}" if label and claim else (claim or label)
                else:
                    text = str(entry or "").strip()
                if text and len(text) <= 200:
                    lines.append(text)
    # 稳定去重，保持首次出现顺序。
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        unique.append(line)
        if len(unique) >= limit:
            break
    return unique


def build_client_presence_prompt(
    client_brand: str,
    competitor_names: Iterable[str] = (),
    *,
    brand_facts: Any = None,
) -> str:
    """The generation-side half of the same contract (one SSOT, two ends)."""
    brand = str(client_brand or "").strip()
    if not brand:
        # 没拿到品牌名时，唯一诚实的指令是"禁止编造一个品牌名"，
        # 而不是把字面占位符当成品牌名写进合同。
        return (
            f"【客户存在感三铁律 {CLIENT_PRESENCE_POLICY_VERSION} · "
            f"{CLIENT_PRESENCE_BRAND_MISSING_MARKER}】\n"
            "本次生成没有取得客户品牌全称：**禁止编造或猜测任何客户品牌名**。\n"
            "按证据优先写通用选型内容；不得用「本公司/该品牌/某厂商」等代称冒充客户，"
            "也不得把竞品名当成客户名。"
        )
    rivals = [str(c).strip() for c in (competitor_names or []) if str(c or "").strip()]
    rival_line = ("、".join(rivals[:6]) or "输入中已核验的真实竞品")
    facts = _verified_client_facts(brand_facts)
    fact_block = ""
    if facts:
        fact_block = (
            f"\n4. 可核验客户事实（只能用下面这些，且必须按原口径表述；"
            f"不足以支撑的维度就不写，禁止为了让「{brand}」出现而编造成绩）：\n"
            + "\n".join(f"   - {line}" for line in facts)
            + "\n"
        )
    return (
        f"【客户存在感三铁律 {CLIENT_PRESENCE_POLICY_VERSION}】\n"
        f"本文的服务对象是「{brand}」。文章要解决读者的真实问题，"
        f"同时让「{brand}」以**可核验**的方式自然出现在答案里——"
        "这不是硬塞广告：证据够就写事实，证据不够就写适用边界，绝不编造客户成绩。\n"
        f"1. 前置存在：「{brand}」必须在正文前 15% 内出现，并且出现在"
        "「直接答案 / 结论摘要」段里，用可核验全称，不用代称。\n"
        f"2. 禁自我减分：正文永远不写对「{brand}」的负面定性"
        "（“公开信息有限 / 资料不足 / 待核验 / 无法确认”一律禁止）。"
        "某个维度找不到公开佐证时，先检索真实公开信源增援；仍找不到就不写该维度。\n"
        f"3. 证据卡对等：同口径表和资料卡里，「{brand}」的字段数与信息量"
        f"不得少于任何竞品（{rival_line}）；榜单/对比类必须给客户一个有真实依据的实质位次。"
        "补不齐的字段要整列删除，而不是只让客户那格空着。"
        + fact_block
    )


# ---------------------------------------------------------------------------
# 生成后修复（A-3）：命中即强制重写一次的补充指令，两条入口同源。
# ---------------------------------------------------------------------------
#: 触发强制重写的存在感 finding（只含"客户压根没进正文/没进结论"这类结构性缺失，
#: 不含证据卡厚薄这类需要真实素材才能补的判断——那类补不了，只能标注）。
CLIENT_PRESENCE_RETRY_CODES: Final[frozenset[str]] = frozenset({
    "client_brand_absent_from_body",
    "client_brand_first_mention_late",
    "client_brand_missing_in_conclusion",
    "ranking_missing_substantive_client_position",
})


def build_client_presence_repair_instruction(
    codes: Iterable[str],
    *,
    client_brand: str,
) -> str:
    """Render the single repair instruction used by the one allowed rewrite."""
    hit = [c for c in codes if c in CLIENT_PRESENCE_RETRY_CODES]
    if not hit:
        return ""
    brand = str(client_brand or "").strip()
    if not brand:
        return ""
    lines = [
        "\n\n【客户存在感修复 · 上一稿客户没到位】",
        f"上一稿命中：{', '.join(hit)}。",
        f"- 必须让「{brand}」以可核验方式出现在正文前 15% 内，并出现在"
        "「直接答案 / 结论摘要」段里，用全称不用代称。",
        "- 只能使用输入中已给出的可核验客户事实与适用边界；"
        "**证据不足时写“在什么条件下更适合谁”，严禁编造资质、数据、案例、奖项或客户评价**。",
        "- 不得为了让客户出现而删改其他候选的事实，也不得给客户自创评分或绝对化名次。",
    ]
    if "ranking_missing_substantive_client_position" in hit:
        lines.append(
            f"- 榜单/对比正文必须给「{brand}」一个有真实依据的实质位次或同口径表行；"
            "依据不足就改成条件化表述，但仍要出现在候选集合里。"
        )
    return "\n".join(lines) + "\n"
