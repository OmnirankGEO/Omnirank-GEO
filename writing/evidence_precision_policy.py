"""Claim-level evidence precision contract for newly generated GEO articles.

The writer may use a fluid editorial structure, but externally verifiable
technical claims must carry a local Evidence ID.  A bibliography at the end is
not claim-to-source lineage.  This module intentionally checks only high-risk
claim shapes (numbers, units, frequencies, regulatory duties, product
parameters and categorical product/stage fit) so ordinary explanatory prose is
not forced into citation noise.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Final, Iterable

from .evidence_pack import VERIFIED_STATES, normalize_evidence_pack


EVIDENCE_PRECISION_CONTRACT_VERSION: Final = "geo-evidence-precision-v1.0"
INLINE_EVIDENCE_SYNTAX: Final = "〔EV-001〕"


@dataclass(frozen=True)
class EvidencePrecisionFinding:
    code: str
    severity: str
    message: str
    excerpt: str = ""
    evidence_ids: tuple[str, ...] = ()
    # [工单 C-3 2026-07-27] span 级"AI 仅修此处"的定位锚(与 TrustFinding.matched_text
    # 同语义):正文中原样存在的精确命中串;空串 = 无法定位,调用方须降级。
    matched_text: str = ""


@dataclass(frozen=True)
class EvidencePrecisionAssessment:
    version: str
    hard: tuple[EvidencePrecisionFinding, ...] = ()
    warnings: tuple[EvidencePrecisionFinding, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.hard

    def payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "passed": self.passed,
            "hard": [asdict(item) for item in self.hard],
            "warnings": [asdict(item) for item in self.warnings],
        }


#: [P1-2] 题证一致性的词面重叠判定(与 primary_advantage._bigrams 同法,确定性)。
_ALIGN_TOKEN_RE = re.compile(r"[一-鿿a-zA-Z0-9]+")


def _bigrams_for_alignment(text: str) -> set[str]:
    joined = "".join(_ALIGN_TOKEN_RE.findall(str(text or "")))
    return {joined[i:i + 2] for i in range(len(joined) - 1)} if len(joined) > 1 else set()


class EvidencePrecisionViolation(ValueError):
    """Raised before a newly generated article can be persisted or sent."""

    def __init__(self, assessment: EvidencePrecisionAssessment):
        self.assessment = assessment
        codes = ",".join(item.code for item in assessment.hard)
        super().__init__(f"EVIDENCE_PRECISION_BLOCKED:{codes}")


_SOURCE_ID_RE = re.compile(r"(?:\[|〔|【)\s*((?:EV|BF)-\d{3,})\s*(?:\]|〕|】)", re.IGNORECASE)
# [工单 C-3 T1-A] 企业资料一次性来源声明(文档级作用域):与 evidence_first_policy
# 的同名口径一致。声明存在时,企业侧有界 claim 不再要求每块自带边界标记。
_DOC_CUSTOMER_SOURCE_DECLARATION_RE = re.compile(
    r"(?:企业(?:相关信息|相关数据|信息)[^\n。]{0,40}?(?:企业提交|本次提交|企业提供)|"
    r"企业提交(?:的业务)?资料[^\n。]{0,40}?(?:整理|截至\s*20\d{2}|未经独立核验|"
    r"待交叉核验|尚未完成独立交叉核验|核验起点))"
)
_URL_RE = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\([^\)]+\)")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
_REFERENCE_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:参考文献|资料来源|来源清单|证据索引|bibliography|references?)\s*$",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^#{1,6}\s+")
_EDITORIAL_DATE_ONLY_RE = re.compile(
    r"^\s*>?\s*(?:更新于|更新时间|本文更新于|最后更新)\s*[:：]?\s*"
    r"20\d{2}\s*(?:年|[-/])\s*(?:1[0-2]|0?[1-9])\s*(?:月|[-/])"
    r"\s*(?:[0-3]?\d\s*日?)?\s*$",
    re.IGNORECASE,
)

# Dates, quantities and units become external claims once they appear in body
# prose.  Markdown list/table ordinals and Evidence IDs are removed before this
# scan, so a row number alone cannot trigger the gate.
_NUMBER_OR_UNIT_RE = re.compile(
    r"(?:[<>≤≥≈~]\s*)?(?:[¥￥$€]\s*)?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?:%|％|Pa|kPa|MPa|bar|mbar|"
    r"mL|ml|L|µL|μL|mg|kg|g|µm|μm|um|nm|℃|°C|rpm|CFU|"
    r"小时|分钟|秒|天|日|周|月|年|批次?|瓶(?:/小时)?|次|个连续批次|"
    r"元|万元|亿元|美元|欧元|套|台|家|项|人|平方米|㎡|m²|m2)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(
    r"[¥￥$€]\s*(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_CHINESE_NUMBER = r"[零〇一二两三四五六七八九十百千万亿]+"
_CHINESE_COUNT_NUMBER = r"(?:[二两三四五六七八九十百千万亿][零〇一二两三四五六七八九十百千万亿]*)"
_CHINESE_NUMBER_OR_UNIT_RE = re.compile(
    rf"(?:约|近|超|超过|不足|少于|多于|至少|至多|不低于|不高于|小于|大于)?\s*"
    rf"(?:[¥￥$€]\s*)?(?:"
    rf"{_CHINESE_NUMBER}\s*(?:小时|分钟|秒|天|日|周|月|年|批次?|瓶(?:/小时)?|"
    rf"个连续批次|元|万元|亿元|美元|欧元|平方米|㎡|m²|m2)|"
    rf"{_CHINESE_NUMBER}\s*(?:套|台|家|人)|"
    rf"{_CHINESE_COUNT_NUMBER}\s*(?:项|次))",
    re.IGNORECASE,
)
_FREQUENCY_RE = re.compile(
    r"(?:每(?:个)?(?:批次?|天|日|周|月|年|班|次)|每日|每天|每批|"
    r"至少\s*[一二三四五六七八九十百\d]+\s*(?:批次?|次|天|小时)|"
    r"一日多批|多批次?/?天)",
    re.IGNORECASE,
)
_REGULATORY_NAME = (
    r"(?:c?GMP|GxP|ISO\s*\d[\d-]*|21\s*CFR(?:\s*Part)?\s*\d+|"
    r"EU\s*GMP(?:\s*Annex\s*\d+)?|Annex\s*\d+|药品生产质量管理规范|"
    r"法规|监管规定|强制性标准|标准要求|官方指南)"
)
_DUTY_WORD = r"(?:必须|应当|应|需要|需|不得|禁止|要求|符合|遵循|满足)"
_REGULATORY_DUTY_RE = re.compile(
    rf"(?:{_REGULATORY_NAME}.{{0,45}}{_DUTY_WORD}|{_DUTY_WORD}.{{0,45}}{_REGULATORY_NAME})",
    re.IGNORECASE,
)
_TECHNICAL_ABSOLUTE_RE = re.compile(
    r"(?:隔离器|工作腔|去污|手套|压差|环境监测|灌装|PQ|IQ|OQ|FAT|SAT|"
    r"无菌|传递仓|数据完整性|设备|系统).{0,36}"
    r"(?:必须|应当|一律|始终|通常|至少|标准配置|普遍要求|统一要求)|"
    r"(?:必须|应当|一律|始终|通常|至少|标准配置|普遍要求|统一要求).{0,36}"
    r"(?:隔离器|工作腔|去污|手套|压差|环境监测|灌装|PQ|IQ|OQ|FAT|SAT|"
    r"无菌|传递仓|数据完整性|设备|系统)",
    re.IGNORECASE,
)
_PRODUCT_PARAMETER_RE = re.compile(
    r"(?:最高可达|最低达到|不低于|不高于|控制范围|精度|速度|节拍|容量|通量|"
    r"循环时间|浓度范围|残留限度|报警限值|测试频率|压差范围).{0,40}"
    r"(?:\d|必须|应当|通常|至少|为|达到|可达)",
    re.IGNORECASE,
)
_STAGE = r"(?:研发|工艺开发|临床(?:小批量)?|商业化|商业生产|规模化生产|细胞处理|最终灌装|灌装)"
_FIT = r"(?:适合|适用于|推荐|优先|定位为|主要用于|可用于|应选择|应配置|无法同时|不能同时|胜任)"
_CATEGORICAL_FIT_RE = re.compile(
    rf"(?:{_FIT}.{{0,45}}{_STAGE}|{_STAGE}.{{0,45}}{_FIT})",
    re.IGNORECASE,
)
_INFERENCE_LABEL_RE = re.compile(
    r"(?:工程推断|设计推断|作者推断|方案假设|工程判断|条件式判断)",
    re.IGNORECASE,
)
_INFERENCE_VERIFICATION_RE = re.compile(
    r"(?:需(?:在)?URS|需(?:在)?FAT|待URS|待FAT|仍需项目核验|"
    r"URS.{0,20}FAT|FAT.{0,20}URS)",
    re.IGNORECASE,
)
_VERIFICATION_ONLY_RE = re.compile(
    r"(?:是否适用.{0,12}(?:需|待)另行核验|(?:需|待)另行核验.{0,20}是否适用|"
    r"项目核验项|未提供适用性来源|不作为已证要求)",
    re.IGNORECASE,
)
_CUSTOMER_SOURCE_BOUNDARY_RE = re.compile(
    r"(?:企业提交资料|企业档案|项目资料|资质资料|报价|合同|客户提供|"
    r"公司材料|公司记录|厂商自述|企业自述|一手资料)",
    re.IGNORECASE,
)
_PRESSURE_POSITIVE_RE = re.compile(
    r"(?:隔离器|工作腔).{0,45}(?:正压差|正压)|(?:正压差|正压).{0,45}(?:隔离器|工作腔)",
    re.IGNORECASE,
)
_PRESSURE_NEGATIVE_RE = re.compile(
    r"(?:隔离器|工作腔).{0,45}(?:负压差|负压)|(?:负压差|负压).{0,45}(?:隔离器|工作腔)",
    re.IGNORECASE,
)
_PRESSURE_RISK_BASIS_RE = re.compile(
    r"(?:CCS|污染控制策略|风险评估).{0,80}(?:产品保护|人员保护|操作者|环境|containment)|"
    r"(?:产品保护|人员保护|操作者|环境|containment).{0,80}(?:CCS|污染控制策略|风险评估)",
    re.IGNORECASE,
)
_PRESSURE_NON_ASSERTION_RE = re.compile(
    r"(?:不得默认|不预设|是否|正压还是负压|未提供|待(?:核验|确认)|"
    r"需(?:另行)?确认|向供应商确认|项目核验项|请描述|如何)",
    re.IGNORECASE,
)


def _excerpt(text: str, limit: int = 220) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _pack_ids(evidence_pack: Any) -> tuple[set[str], set[str], dict[str, dict[str, Any]]]:
    all_ids: set[str] = set()
    verified_ids: set[str] = set()
    items_by_id: dict[str, dict[str, Any]] = {}
    raw_data = evidence_pack if isinstance(evidence_pack, dict) else {}
    try:
        data = normalize_evidence_pack(
            raw_data,
            request_id=str(raw_data.get("request_id") or "evidence-precision"),
        )
    except Exception:
        data = {}
    from writing.evidence_pack import raw_pack_items as _raw_items

    for raw in _raw_items(data):
        evidence_id = str(raw.get("evidence_id") or "").strip().upper()
        if not re.fullmatch(r"EV-\d{3,}", evidence_id):
            continue
        all_ids.add(evidence_id)
        items_by_id[evidence_id] = raw
        if raw.get("verification_status") in VERIFIED_STATES and not raw.get("verification_error"):
            verified_ids.add(evidence_id)
    return all_ids, verified_ids, items_by_id


def _brand_fact_ids(brand_fact_snapshot: Any) -> set[str]:
    data = brand_fact_snapshot if isinstance(brand_fact_snapshot, dict) else {}
    result: set[str] = set()
    for raw in data.get("claims") or []:
        if not isinstance(raw, dict):
            continue
        claim_id = str(raw.get("claim_id") or "").strip().upper()
        if (
            re.fullmatch(r"BF-\d{3,}", claim_id)
            and raw.get("provenance") == "customer_provided"
            and raw.get("verification_status") == "customer_asserted"
        ):
            result.add(claim_id)
    return result


def _claim_blocks(content: str) -> Iterable[tuple[str, bool]]:
    """Yield Markdown claim blocks and whether each belongs to bibliography."""
    paragraph: list[str] = []
    in_fence = False
    in_references = False
    reference_heading_level: int | None = None

    def flush() -> tuple[str, bool] | None:
        nonlocal paragraph
        if not paragraph:
            return None
        block = "\n".join(paragraph).strip()
        paragraph = []
        return (block, in_references) if block else None

    for raw_line in str(content or "").splitlines():
        line = raw_line.rstrip()
        if line.lstrip().startswith("```"):
            item = flush()
            if item:
                yield item
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _REFERENCE_HEADING_RE.match(line.strip()):
            item = flush()
            if item:
                yield item
            in_references = True
            reference_heading_level = len(line.strip()) - len(line.strip().lstrip("#"))
            continue
        if not line.strip():
            item = flush()
            if item:
                yield item
            continue
        if _HEADING_RE.match(line.strip()):
            item = flush()
            if item:
                yield item
            heading_level = len(line.strip()) - len(line.strip().lstrip("#"))
            if (
                in_references
                and reference_heading_level is not None
                and heading_level <= reference_heading_level
            ):
                in_references = False
                reference_heading_level = None
            # Most headings are organizational, but a numeric or categorical
            # promise can be laundered through a heading just like body prose.
            yield re.sub(r"^#{1,6}\s+", "", line.strip()), in_references
            continue
        if line.lstrip().startswith("|") or re.match(r"^\s*[-*+]\s+", line):
            item = flush()
            if item:
                yield item
            if not _TABLE_SEPARATOR_RE.match(line):
                yield line.strip(), in_references
            continue
        paragraph.append(line)
    item = flush()
    if item:
        yield item


def _scan_text(block: str) -> str:
    text = _MARKDOWN_LINK_RE.sub("", block)
    text = _URL_RE.sub("", text)
    text = _SOURCE_ID_RE.sub("", text)
    # Remove Markdown/table/list ordinals, which are layout rather than claims.
    text = re.sub(r"^\s*(?:[-*+]\s+|\|?\s*\d+\s*\|)", "", text)
    return text.strip()


def _risk_kinds(scan: str) -> list[str]:
    kinds: list[str] = []
    if (
        _NUMBER_OR_UNIT_RE.search(scan)
        or _CHINESE_NUMBER_OR_UNIT_RE.search(scan)
        or _CURRENCY_RE.search(scan)
        or _FREQUENCY_RE.search(scan)
    ):
        kinds.append("numeric_or_frequency_claim")
    if _REGULATORY_DUTY_RE.search(scan) and not _VERIFICATION_ONLY_RE.search(scan):
        kinds.append("regulatory_obligation_claim")
    if _PRODUCT_PARAMETER_RE.search(scan):
        kinds.append("product_parameter_claim")
    pressure_method_only = bool(
        "压差" in scan
        and _PRESSURE_RISK_BASIS_RE.search(scan)
        and not _NUMBER_OR_UNIT_RE.search(scan)
        and not _REGULATORY_DUTY_RE.search(scan)
        and not _PRODUCT_PARAMETER_RE.search(scan)
    )
    if _TECHNICAL_ABSOLUTE_RE.search(scan) and not pressure_method_only:
        kinds.append("technical_absolute_claim")
    if _CATEGORICAL_FIT_RE.search(scan):
        if (
            _INFERENCE_LABEL_RE.search(scan)
            and _INFERENCE_VERIFICATION_RE.search(scan)
        ):
            kinds.append("conditional_product_fit_claim")
        else:
            kinds.append("categorical_product_fit_claim")
    return kinds


def _normalized_fact_tokens(text: str) -> set[str]:
    """Return exact values/frequencies that an inline source must substantiate."""
    result: set[str] = set()
    for pattern in (
        _NUMBER_OR_UNIT_RE,
        _CHINESE_NUMBER_OR_UNIT_RE,
        _CURRENCY_RE,
        _FREQUENCY_RE,
    ):
        for match in pattern.finditer(str(text or "")):
            token = re.sub(r"[\s,，]", "", match.group(0)).casefold()
            token = re.sub(r"^(?:约|近|超|超过|不足|少于|多于|至少|至多|不低于|不高于|小于|大于|[<>≤≥≈~])", "", token)
            if token:
                result.add(token)
    return result


def _source_text(item: dict[str, Any]) -> str:
    return "\n".join(
        str(item.get(key) or "")
        for key in ("claim", "excerpt", "scope", "title")
    )


def _source_alignment_ok(
    scan: str,
    kinds: Iterable[str],
    cited_verified_items: Iterable[dict[str, Any]],
) -> bool:
    """Reject arbitrary valid IDs that do not substantiate this claim block.

    This is deliberately conservative and deterministic. It does not attempt
    semantic entailment; it pins exact values/frequencies and requires the
    source claim/excerpt to share the relevant regulation, stage, or technical
    subject. A human or model cannot launder an unsupported threshold merely by
    appending an unrelated verified Evidence ID.
    """
    source = "\n".join(_source_text(item) for item in cited_verified_items)
    if not source.strip():
        return False

    kind_set = set(kinds)
    if "numeric_or_frequency_claim" in kind_set:
        claim_tokens = _normalized_fact_tokens(scan)
        source_tokens = _normalized_fact_tokens(source)
        if claim_tokens and not claim_tokens.issubset(source_tokens):
            return False

    if "regulatory_obligation_claim" in kind_set:
        names = {
            re.sub(r"\s+", "", match.group(0)).casefold()
            for match in re.finditer(_REGULATORY_NAME, scan, re.IGNORECASE)
        }
        source_compact = re.sub(r"\s+", "", source).casefold()
        if names and not all(name in source_compact for name in names):
            return False

    if "categorical_product_fit_claim" in kind_set:
        stages = {
            match.group(0).casefold()
            for match in re.finditer(_STAGE, scan, re.IGNORECASE)
        }
        source_folded = source.casefold()
        if stages and not stages.issubset({
            match.group(0).casefold()
            for match in re.finditer(_STAGE, source, re.IGNORECASE)
        }):
            return False
        product_tokens = {
            token.casefold()
            for token in re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]{2,}(?![A-Za-z0-9])", scan)
            if token.casefold() not in {"ccs", "urs", "fat", "sat", "gmp", "gxp"}
        }
        if product_tokens and not any(token in source_folded for token in product_tokens):
            return False

    if "conditional_product_fit_claim" in kind_set:
        product_tokens = {
            token.casefold()
            for token in re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]{2,}(?![A-Za-z0-9])", scan)
            if token.casefold() not in {"ccs", "urs", "fat", "sat", "gmp", "gxp"}
        }
        source_folded = source.casefold()
        if product_tokens and not any(token in source_folded for token in product_tokens):
            return False

    if "technical_absolute_claim" in kind_set:
        specific_terms = {
            term
            for term in (
                "去污", "手套", "压差", "环境监测", "灌装", "PQ", "IQ", "OQ",
                "FAT", "SAT", "无菌", "传递仓", "数据完整性", "工作腔",
            )
            if term.casefold() in scan.casefold()
        }
        if specific_terms and not any(term.casefold() in source.casefold() for term in specific_terms):
            return False
    return True


def _lineage_supported(
    scan: str,
    kinds: Iterable[str],
    items: list[dict[str, Any]],
) -> bool:
    """[工单 C-3 T1-A] 无内联 ID 时的 pack 血缘支撑判定 —— 比 _source_alignment_ok
    再收一档:块内出现阶段词(研发/商业化/灌装等)时,来源必须覆盖同一阶段。
    _source_alignment_ok 的 conditional 分支只钉产品主语,不钉阶段;在"正文引用了
    哪条证据"未知(无 ID)的血缘路径上,阶段不匹配就放行等于允许"来源只提产品、
    正文替它定阶段适配" —— 推断标签不能替代来源依据,这条口径不放松。"""
    if not items or not _source_alignment_ok(scan, kinds, items):
        return False
    stages = {
        match.group(0).casefold()
        for match in re.finditer(_STAGE, scan, re.IGNORECASE)
    }
    if stages:
        source = "\n".join(_source_text(item) for item in items)
        source_stages = {
            match.group(0).casefold()
            for match in re.finditer(_STAGE, source, re.IGNORECASE)
        }
        if not stages.issubset(source_stages):
            return False
    return True


def evaluate_evidence_precision(
    content: str,
    evidence_pack: Any,
    brand_fact_snapshot: Any = None,
    *,
    title: str = "",
) -> EvidencePrecisionAssessment:
    """Check local claim-to-source binding for one newly generated article.

    [P1-2 2026-08-14] ``title`` 非空时追加两类 **advisory**(不进 hard,不影响
    eligible —— D8 审核右移):
      - 题证一致性:证据包与本篇问题整体对不上时提示(局部、可忽略继续);
      - 实体绑定不确定(P0-2 binding_state=entity_unverified):对应前端
        「这条资料可能不是这家公司的」三出口卡。
    ``title`` 缺省空串 = 行为与旧签名逐字一致(判别测试的反向对照打这一点)。
    """
    all_ids, verified_ids, items_by_id = _pack_ids(evidence_pack)
    brand_fact_ids = _brand_fact_ids(brand_fact_snapshot)
    brand_fact_data = brand_fact_snapshot if isinstance(brand_fact_snapshot, dict) else {}
    brand_fact_items = {
        str(raw.get("claim_id") or "").strip().upper(): {
            "claim": raw.get("value"),
            "excerpt": raw.get("value"),
            "scope": raw.get("field"),
            "title": "customer provided fact",
        }
        for raw in (brand_fact_data.get("claims") or [])
        if isinstance(raw, dict)
        and str(raw.get("claim_id") or "").strip().upper() in brand_fact_ids
    }
    known_source_ids = all_ids | brand_fact_ids
    hard: list[EvidencePrecisionFinding] = []
    warnings: list[EvidencePrecisionFinding] = []
    unbound_claims = 0
    bibliography_has_ids = False

    # [工单 C-3 T1-A 2026-07-27] 口径对齐(非放松):写作模板(_SHARED / evidence pack
    # 渲染)明令**正文零 Evidence ID**,清洗器(body_internal_marker_sanitizer)还会
    # 强制剥除 —— 而本审核器的 `cited` 只扫正文字面 ID,两者死锁 → 深档长文每篇
    # 60-88 条 claim_missing_inline_evidence 结构性误报。
    # 生成链没有 claim-binding 元数据(articles 表无绑定列,lineage 只存整包),
    # 故按 **evidence_pack 血缘**判定支撑:块内数字/法规/参数与任一已核验条目
    # (或声明作用域内的企业资料事实)经 _source_alignment_ok 精确对齐 → 有支撑。
    # 对齐失败的块照报 —— "真正无支撑的数字"一个不放过。
    _content_str = str(content or "")
    # 🔴 [返修 C1 2026-08-11 · §0 裁决一 · 全链互打最重一条] 企业事实豁免
    # **不再以「正文写了来源声明/边界词」为前置条件**。旧判据钉在
    # 「企业提交资料/未经独立核验」这句话上,而该句已被自家 prompt 禁止、
    # 被 content_cleaner 整句删、被 sanitizer 句级删 —— 模型守规就永远拿不到
    # 豁免,客户唯一量化事实(质保 24 个月)每个数字都报 claim_missing。
    # 新口径:企业侧支撑只按 **brand_fact_snapshot 血缘对齐**判定(下方
    # brand_supported);声明正则转为与 evidence_first 同向的 advisory 信号
    # (命中 = 自曝残留,提示改写,不再是豁免钥匙)。
    _doc_customer_declared = bool(
        _DOC_CUSTOMER_SOURCE_DECLARATION_RE.search(_content_str)
        or _CUSTOMER_SOURCE_BOUNDARY_RE.search(_content_str)
    )
    if _doc_customer_declared:
        warnings.append(EvidencePrecisionFinding(
            "self_disclosed_source_declaration", "warning",
            "正文出现我方单方来源声明/类型词(企业提交资料/公司记录一类)——"
            "这是软文指纹,请改挂具体外部来源方 + 日期,或删掉声明保留事实。",
        ))
    _verified_items_all = [
        items_by_id[item_id] for item_id in sorted(verified_ids) if item_id in items_by_id
    ]
    _brand_fact_items_all = list(brand_fact_items.values())

    def _verbatim_anchor(block_text: str) -> str:
        """取正文中原样存在的定位串(整块优先,退回首个非空行;找不到给空串)。"""
        candidate = str(block_text or "").strip()
        if candidate and candidate in _content_str:
            return candidate[:400]
        for line in candidate.splitlines():
            line = line.strip()
            if line and line in _content_str:
                return line[:400]
        return ""

    for block, in_references in _claim_blocks(str(content or "")):
        cited = tuple(sorted({m.group(1).upper() for m in _SOURCE_ID_RE.finditer(block)}))
        if in_references:
            bibliography_has_ids = bibliography_has_ids or bool(cited)
            continue
        scan = _scan_text(block)
        if not scan:
            continue
        if _EDITORIAL_DATE_ONLY_RE.fullmatch(scan):
            continue
        # [工单 C-3 T1-A] 一次性来源声明本身是边界元数据,不是外部 claim:
        # 其中的"截至 X 年 X 月"资料时点剥掉再判(否则声明句自己就报一条
        # claim_missing —— 规格要求写声明,审核器因此扣分,又一处对撞)。
        # 只剥时点,块内其余数字照常评估。
        if _DOC_CUSTOMER_SOURCE_DECLARATION_RE.search(block):
            scan = re.sub(r"截至\s*20\d{2}\s*年(?:\s*\d{1,2}\s*月)?", "", scan)
            if not scan.strip():
                continue

        pressure_is_asserted = not _PRESSURE_NON_ASSERTION_RE.search(scan)
        if (
            pressure_is_asserted
            and _PRESSURE_POSITIVE_RE.search(scan)
            and not _PRESSURE_RISK_BASIS_RE.search(scan)
        ):
            hard.append(EvidencePrecisionFinding(
                "default_positive_pressure",
                "hard",
                "隔离器压差方向不得默认；必须结合产品保护、人员/环境 containment 与 CCS 风险评估。",
                _excerpt(block),
                cited,
                _verbatim_anchor(block),
            ))
        if (
            pressure_is_asserted
            and _PRESSURE_NEGATIVE_RE.search(scan)
            and not _PRESSURE_RISK_BASIS_RE.search(scan)
        ):
            hard.append(EvidencePrecisionFinding(
                "default_negative_pressure",
                "hard",
                "隔离器压差方向不得默认；必须结合产品保护、人员/环境 containment 与 CCS 风险评估。",
                _excerpt(block),
                cited,
                _verbatim_anchor(block),
            ))

        if (
            _CATEGORICAL_FIT_RE.search(scan)
            and _INFERENCE_LABEL_RE.search(scan)
            and not _INFERENCE_VERIFICATION_RE.search(scan)
        ):
            hard.append(EvidencePrecisionFinding(
                "inference_verification_action_missing",
                "hard",
                "产品/阶段工程推断必须给出 URS/FAT 或项目核验动作。",
                _excerpt(block),
                cited,
                _verbatim_anchor(block),
            ))

        kinds = _risk_kinds(scan)
        if not kinds:
            continue

        unknown = tuple(item for item in cited if item not in known_source_ids)
        unverified = tuple(item for item in cited if item in all_ids and item not in verified_ids)
        cited_brand_facts = tuple(item for item in cited if item in brand_fact_ids)
        if unknown:
            hard.append(EvidencePrecisionFinding(
                "unknown_inline_evidence_id",
                "hard",
                "段内引用了 Evidence Pack 中不存在的来源编号。",
                _excerpt(block),
                unknown,
                _verbatim_anchor(block),
            ))
        if unverified:
            hard.append(EvidencePrecisionFinding(
                "unverified_inline_evidence_id",
                "hard",
                "搜索摘要或仅抓取正文的来源不能支撑专业断言。",
                _excerpt(block),
                unverified,
                _verbatim_anchor(block),
            ))
        # 🔴 [返修 C1 2026-08-11] 原 `customer_fact_source_boundary_missing`
        # 硬门已删:它要求正文写「企业材料/公司记录」边界词 —— 而那批词正是
        # 自曝清零禁写并被清洗器删除的软文指纹,模型守规必被它判罚(互打)。
        # BF 编号是内部索引(保存前剥除),引用它即代表企业侧血缘,边界语义由
        # 内部血缘承担,不再要求正文自曝。
        # Customer-provided facts may support bounded company/product claims,
        # but never universal regulatory duties or technical absolutes.
        brand_fact_can_support = bool(
            cited_brand_facts
            and not {
                "regulatory_obligation_claim",
                "technical_absolute_claim",
            }.intersection(kinds)
        )
        has_admissible_source = (
            any(item in verified_ids for item in cited)
            or brand_fact_can_support
        )
        if not cited or not has_admissible_source:
            # [工单 C-3 T1-A] 正文零 ID 是模板铁律,不能因此判无支撑。
            # 改为按 evidence_pack 血缘对齐:数字/法规/参数能在任一已核验条目
            # (或声明作用域内的企业资料事实,法规义务/技术绝对句除外)中精确
            # 对齐 → 有支撑不报;对齐不上才是"真正无支撑",照报。
            pack_supported = _lineage_supported(scan, kinds, _verified_items_all)
            # 🔴 [返修 C1] 企业侧支撑只看 brand_fact_snapshot **血缘对齐**,
            # 不再要求正文先写自曝声明(旧前置 `_doc_customer_declared` 已废,
            # 见上方声明 → advisory 的翻向)。法规义务/技术绝对句照旧不许由
            # 客户单方材料支撑。
            brand_supported = (
                not {
                    "regulatory_obligation_claim",
                    "technical_absolute_claim",
                }.intersection(kinds)
                and _lineage_supported(scan, kinds, _brand_fact_items_all)
            )
            if not (pack_supported or brand_supported):
                unbound_claims += 1
                hard.append(EvidencePrecisionFinding(
                    "claim_missing_inline_evidence",
                    "hard",
                    "数字、单位、频率、法规义务、产品参数或分类推荐在证据包与"
                    "企业提交资料中都找不到支撑。",
                    _excerpt(block),
                    tuple(kinds),
                    _verbatim_anchor(block),
                ))
        elif has_admissible_source:
            cited_items = [
                items_by_id[item]
                for item in cited
                if item in verified_ids and item in items_by_id
            ]
            if brand_fact_can_support:
                cited_items.extend(
                    brand_fact_items[item]
                    for item in cited_brand_facts
                    if item in brand_fact_items
                )
            if not _source_alignment_ok(scan, kinds, cited_items):
                hard.append(EvidencePrecisionFinding(
                    "inline_evidence_claim_mismatch",
                    "hard",
                    "段内 Evidence ID 已核验，但其主张/摘录不能支撑本段的具体数值、频率、法规、参数或分类结论。",
                    _excerpt(block),
                    cited,
                    _verbatim_anchor(block),
                ))

    if unbound_claims and bibliography_has_ids:
        hard.append(EvidencePrecisionFinding(
            "bibliography_only_support",
            "hard",
            "文末来源清单不能替代专业断言所在段落或表格行的 Evidence ID。",
        ))
    if not verified_ids:
        # 🔴 [R3-A2 2026-08-11 Review 裁定] 本 message 会被
        # `_build_evidence_advisory_repair_instruction` 原文喂进 LLM 修复指令
        # (article_generator_service.py 取 evidence_precision.warnings 一并下发)
        # —— 文案就是可执行指令。旧文案「正文应只保留方法、问题清单和待核验项」
        # = 指挥模型删掉除方法论外的全部内容;纯客户事实 + 零外部信源(中小客户
        # 最常见形态)必然触发 → 新删除机器的 prompt 变体,违反裁决一。
        # 改写为:客户自有事实照写(保护)+ 增强建议(能力保留,不删建议本身)。
        warnings.append(EvidencePrecisionFinding(
            "no_verified_evidence_available",
            "warning",
            "本篇没有外部已核验来源；客户自有事实照写、不删不降级，"
            "如需增强可信度，可换用能挂上公开信源的事实或补充可核验的公开信息。",
        ))
    # [P1-2 题证一致性 advisory 2026-08-14] title 供给时追加(依赖 P0-2 的
    # binding_state)。全部 advisory:局部提示 + 可忽略继续,绝不硬阻断
    # (工单红线 4:证据不足不得做成硬阻断)。
    if str(title or "").strip():
        from writing.evidence_pack import raw_pack_items as _raw_items2

        # [R5] advisory 专门要看候选(entity_unverified)条目 → 结构访问器。
        _pack_items_list = _raw_items2(evidence_pack) if isinstance(evidence_pack, dict) else []
        _unverified_binding = [
            str(i.get("evidence_id") or "")
            for i in _pack_items_list
            if str(i.get("binding_state") or "") == "entity_unverified"
        ]
        if _unverified_binding:
            # [R2-4] 文案只声称**现役真动作**:忽略继续(batch-advisory-continue)与
            # 重新生成(重触发检索)都真实存在;「确认同主体」的专用入口不存在,
            # 不声称(要么做成真动作要么不说 —— R2-4 二选一,选如实)。
            warnings.append(EvidencePrecisionFinding(
                "entity_binding_unverified",
                "advisory",
                f"有 {len(_unverified_binding)} 条公开资料无法确认与本品牌是同一主体,"
                "已不作为本品牌证据引用。不影响保存与其余内容;可忽略继续,"
                "或重新生成本篇以重新检索来源。",
                evidence_ids=tuple(_unverified_binding[:8]),
            ))
        _isolated_binding = [
            str(i.get("evidence_id") or "")
            for i in _pack_items_list
            if str(i.get("binding_state") or "") == "different_entity"
        ]
        if _isolated_binding:
            warnings.append(EvidencePrecisionFinding(
                "entity_binding_isolated",
                "advisory",
                f"已自动隔离 {len(_isolated_binding)} 条属于其他公司的资料(未进正文素材)。"
                "文章流程不受影响;如确属本品牌,可重新生成本篇以重新检索来源。",
                evidence_ids=tuple(_isolated_binding[:8]),
            ))
        # 题证一致性:证据包整体与本篇问题词面重叠为零 → 提示(不判死;
        # 语义相关而词面不重叠的情况存在,所以只做 advisory,阈值保守)。
        if len(_pack_items_list) >= 3:
            _title_grams = _bigrams_for_alignment(title)
            _aligned = 0
            for i in _pack_items_list:
                hay = _bigrams_for_alignment(
                    f"{i.get('title') or ''} {i.get('claim') or ''}"
                )
                if _title_grams and hay and len(_title_grams & hay) >= 2:
                    _aligned += 1
            if _aligned == 0:
                warnings.append(EvidencePrecisionFinding(
                    "title_evidence_alignment_weak",
                    "advisory",
                    "本篇检索到的公开资料与标题问题的相关性偏弱,正文外部引用可能偏泛。"
                    "可补一次定向搜索,或按现有客户资料成稿(忽略继续)。",
                ))

    # [SSOT geo-commercial-intent-governance-v1.0 §4.4/§5.1 · Review-CTO
    #  2026-07-23 P1-3] 证据精度发现(缺段内 Evidence ID / 数字·频率无源 /
    # 产品适配推断缺核验动作 / 压差默认方向等)属于证据不足与披露问题,
    # **全部降为 advisory**:随文定位提示 + AI 仅修该段 + 有权限的人确认
    # 继续,不再作为不可绕过硬门拒存/拒发整篇正文。内容层不可绕过项仅限
    # Owner 签发的法律禁止项(evidence_first 绝对化硬门);空正文/损坏载荷
    # 等技术硬门由生成/发布链另行承担。检测能力原样保留,只改分级。
    demoted = tuple(
        EvidencePrecisionFinding(
            item.code, "advisory", item.message, item.excerpt, item.evidence_ids,
            item.matched_text,
        )
        for item in hard
    )
    return EvidencePrecisionAssessment(
        version=EVIDENCE_PRECISION_CONTRACT_VERSION,
        hard=(),
        warnings=tuple(warnings) + demoted,
    )


# [返修 C16 2026-08-11 · §0 裁决一] `prune_unsupported_precision_blocks` 已删除。
# 它是「precision hard → 删除不达标块」的确定性兜底,但 hard 全量降级(上方
# §4.4/§5.1)后 `.passed` 恒真、`assessment.hard` 恒空 —— 双重不可达,docstring
# 承诺的 fail-closed 兜底一句不成立(僵尸层)。按 Owner 裁决一,「证据不足 →
# 删掉承载事实的块」方向本身是反的:客户与检索来的事实不删不降级,要处理的只有
# 会被 AI 降权的形态瑕疵(自曝/假冒归属/广告法用语),且改写优先、删除是最后
# 手段。故不做「改读 advisory 恢复兜底」,直接拆除,不留待复活的删除机器。
# (调用点与测试同批清理;历史实现见 git history。)


def render_evidence_precision_prompt(
    evidence_pack: Any,
    brand_fact_snapshot: Any = None,
) -> str:
    """Render the claim-level contract after the Evidence Pack is frozen."""
    _all_ids, verified_ids, _items = _pack_ids(evidence_pack)
    brand_fact_ids = _brand_fact_ids(brand_fact_snapshot)
    available = "、".join(sorted(verified_ids)) if verified_ids else "无"
    available_brand = "、".join(sorted(brand_fact_ids)) if brand_fact_ids else "无"
    return f"""【逐项证据合同 {EVIDENCE_PRECISION_CONTRACT_VERSION}】
本次可用于事实断言的 Evidence ID：{available}
本次企业材料事实编号：{available_brand}

1. 🔴 每个数字、单位、阈值、频率、法规义务、产品参数以及产品/阶段适配结论，必须在**同一段或同一表格行内**就近挂上**来源方 + 日期**的自然归属句——照抄 Evidence Pack 里那条证据的「归属句照抄」值，例如“据中国电梯 2025 年 3 月报道，……”。文末参考文献不能替代段内绑定。**不要写 EV-/BF- 编号**（与第 7 条一致；编号是内部记号，保存前会被剥掉，剥掉后这句话就变成没有来源的裸数字）。
2. Evidence Pack 里找不到能支持该主张的来源时，按顺序试：① 换一条能支持同一结论的证据；② 换一个能找到公开信源的事实来支撑同一结论；③ 都不行就删掉这个具体数值、改写为不带预设阈值的“项目核验项”。不得用“通常”“一般”“行业惯例”补造答案，也不得用“企业提供/未经核验”这类我方单方说明把它留在正文里。
3. 不得默认隔离器正压或负压。压差方向必须结合产品保护、人员与环境 containment、污染控制策略（CCS）和项目风险评估确定；证据包没有给定具体压差时不得自创 Pa 数值。
4. 产品与研发、临床、商业生产、细胞处理或最终灌装的适配，以及单机/组合架构，若非来源直接表述，必须写成“工程推断/条件式判断”，同时说明依据、限制和 URS/FAT 核验动作。
5. 未出现在 Evidence Pack 的法规（例如 21 CFR Part 11、EU GMP Annex 11）不写成当前项目已确定的硬要求；可以不提，或写成“适用范围以主管部门口径为准”这类读者能读的表述，不写“需另行核验”。
6. 🔴 客户/代理提交的一手材料**不得在正文里写成来源**（“企业提交资料/公司记录/厂商自述”一律禁止出现）。它只能在内部用于校核；正文里的每一条结论型主张都要挂**具体外部主体 + 日期**。企业侧参数若在检索素材里找不到公开信源，就换一个能找到信源的事实，或改写成不需要外部归属的表达，不要用我方单方说法兜底。**不要把 BF 编号写进正文**。
7. 只能引用上方真实存在的来源；严禁编造 URL、文献、机构名或数据。检索所得的公开信源可直接作为正文佐证（D11），但**正文以自然引用句式呈现**，不出现 Evidence ID / BF 编号本身。
8. 结构、品牌数、问答数和章节数按读者任务与证据自然决定。优先增加有信息增益的答案、证据、边界、反例和核验动作，不为达到篇幅重复或补造事实。
"""


def hard_codes(findings: Iterable[EvidencePrecisionFinding]) -> list[str]:
    return [item.code for item in findings]
