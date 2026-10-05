"""[D11 · SSOT v2.1 ①] 正文内部记号出站清洗器 —— 保存前的最后一道兜底。

提示词已改成"自然引用、正文零内部记号"（`canonical_family_templates._SHARED`），
但 LLM 有惯性：旧口径训练/少样本残留会让它继续吐 `[EV-3]`、"证据状态"表、
"待核验"。**客户看到的正文里出现这些，等于文章没写完**（生产客诉原文：
"核验"字样篇均 10+、BF-编号泄入正文）。

本模块在保存前把这些内部记号从正文剥掉，并**原样挪进 metadata**：
- 不是删除信息 —— 剥下来的内容全部落 `internal_markers`，内部审阅仍可查；
- 不是新增阻断 —— 清洗永远不失败、不拒存、不重跑整篇（D8 文章层零阻断）；
- 不碰真实引用 —— "据《XX》报道"这类自然引用句式一律保留，只剥内部编号/
  状态表/审查语言。

⚠️ 不负责"引用是否真实存在"。那是 H0 判别（引用必须在 evidence manifest 内），
由 `verify_citations_against_manifest` 承担，两者职责分离。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("GEO-Writing-Sanitizer")

# [P3 2026-08-08] v2 → v3:剥除清单新增「规格卡时代」的编制口径词。
# 版本号必须跟着涨 —— 它会随每次清洗落进 lineage 快照,不涨就分不出
# 「这篇是按哪一版清单洗的」(存量审计要靠它分组)。
# [W1 返工 ③ 2026-08-08] v3 → v4:补「披露式话术」的**变体**。
#
# 🔴 v3 的盲区是被实测按在地上的:黑名单是**精确短语**匹配,只认连续的「待核验」三个字。
# 而生产上篇均 10+ 的那些字样是「待**交叉**核验 / 待抽样复核 / 待核原件 / 待逐项确认 /
# 核验依据」—— 中间夹了字,一个都不命中。于是形成了本仓最讽刺的一个局面:
# **一边有清洗器和测试在拼命堵「待核验」,另一边我们自己的后处理函数在稳定地生产它的变体。**
#
# 所以这一版是两件事一起做,顺序不能反:
#   1. 先让 `source_disclosure_style` / `content_cleaner` /
#      `_sanitize_customer_facing_article_sources` **不再产出**审计腔(那是根因);
#   2. 这里的黑名单只作**兜底**,接住模型自己写出来的变体。
# 只做 2 不做 1,等于让清洗器去删我们自己刚拼上去的句子 —— 那不是清洗,是内耗。
SANITIZER_VERSION = "body-internal-marker-sanitizer-v4"

# ① 内联证据编号：[EV-3] / [BF-12] / （EV-3）/ [Evidence ID: EV-3]
_INLINE_ID_PATTERNS: tuple[re.Pattern, ...] = (
    # [返工 R3] 括号类必须覆盖 LLM 实际吐的全角/中文成对括号。staging 实测
    #   deepseek-v4-flash 吐的是〔EV-002〕,旧表只有 [ （ ( 三种 → 精确模式不命中,
    #   全靠第三条"裸编号"兜底。兜底能剥掉但很脆(换个形态就漏),故补齐。
    re.compile(r"[\[（(〔【「『［]\s*(?:Evidence\s*ID\s*[:：]?\s*)?(?:EV|BF)[-‑—]\s*\d+\s*[\]）)〕】」』］]", re.I),
    re.compile(r"[\[（(〔【「『［]\s*(?:EV|BF)[-‑—]\s*\d+(?:\s*[,，、;；]\s*(?:EV|BF)[-‑—]\s*\d+)+\s*[\]）)〕】」』］]", re.I),
    # 裸编号（前后是空白/标点，避免误伤正文中的 "BF-16 型号" 这类真实型号名）
    re.compile(r"(?<![A-Za-z0-9])(?:EV|BF)[-‑—]\d+(?![A-Za-z0-9-])"),
)

# ② 证据状态表：表头含"证据状态/核验状态/Evidence ID"的 Markdown 表格整块
_STATUS_TABLE_HEADER = re.compile(
    r"^\s*\|.*(?:证据状态|核验状态|待核验|Evidence\s*ID|证据编号).*\|\s*$",
    re.I | re.M,
)

# ③ 审查语言句式（整句/整行剥除）
#
# [P3 文体规格卡 2026-08-08 · 补章 R5 ②] 补规格卡时代的新形态。
# 规格卡给正文下发了「证据密度参考下限」「实体数目标区间」「已核验候选 N 家」这些
# **内部编制口径**,模型会把它们当成可以写进正文的话 —— 生产上"核验字样篇均 10+"
# 那次客诉就是同一种病:**内部口径漏进客户可见正文**。
# 这几条是规格卡上线后新增的漏法,提前堵上:
_REVIEW_PHRASES = (
    "待核验", "需逐家核验", "需交叉验证", "需另行核验", "有待核验",
    "暂无资料", "暂无公开资料", "证据不足", "无法核验", "尚未核验",
    # 规格卡时代新形态(P3):证据密度/实体数/规格版本这些编制口径不许出现在正文
    "已核验候选", "证据密度", "密度下限", "目标区间", "规格卡",
    "结构件分级", "默认必备", "提示词默认", "编制口径", "增援目标",
    # [W1 返工 ③] 披露式话术的**变体**。精确短语匹配抓不到夹字形态,逐个列。
    # 这些是「我们内部把这条标成了什么审核状态」,读者不需要知道,
    # 而 AI 引擎看到它们只会得出一个结论:这是一篇待审的推广稿。
    "待交叉核验", "待抽样复核", "待核原件", "待逐项确认", "待另行确认",
    "尚未完成独立交叉核验", "未经独立核验", "未经第三方独立验证", "未经第三方核验",
    "核验起点", "核验依据", "证据状态", "核验状态",
)
_REVIEW_LINE = re.compile(
    r"^.*(?:" + "|".join(map(re.escape, _REVIEW_PHRASES)) + r").*$",
    re.M,
)
# ④ 客户可见结构段里的"核验清单"小标题及其整节
_CHECKLIST_HEADING = re.compile(
    r"^#{1,6}\s*[^\n]*?(?:核验清单|证据状态|核验状态)[^\n]*$", re.M
)


def _strip_table_block(text: str) -> tuple[str, list[str]]:
    """整块剥除表头命中的 Markdown 表格（连续以 | 开头的行）。"""
    removed: list[str] = []
    lines = text.splitlines()
    keep: list[str] = []
    i = 0
    while i < len(lines):
        if _STATUS_TABLE_HEADER.match(lines[i] or ""):
            block: list[str] = []
            while i < len(lines) and (lines[i].lstrip().startswith("|") or not lines[i].strip()):
                if not lines[i].strip() and block:
                    break
                block.append(lines[i])
                i += 1
            removed.append("\n".join(block))
            continue
        keep.append(lines[i])
        i += 1
    return "\n".join(keep), removed


def _strip_checklist_sections(text: str) -> tuple[str, list[str]]:
    """剥除"核验清单/证据状态"整节（该标题到下一个同级或更高级标题之前）。"""
    removed: list[str] = []
    matches = list(_CHECKLIST_HEADING.finditer(text))
    if not matches:
        return text, removed
    for match in reversed(matches):
        # 只吞该标题后**连续的列表/表格行**（核验清单的真实形态），遇到下一个
        # 标题或第一段正常散文即停。
        #   早期两版都会丢正文：按"同级或更高级标题"找边界时，该节位于文末就
        #   一路吞到 EOF，把后面的正常段落删掉。清洗器丢正文比留记号严重得多，
        #   故改为**保守吞**——宁可漏剥一行，也不吞掉客户内容。
        tail_lines = text[match.end():].splitlines(keepends=True)
        consumed = 0
        for line in tail_lines:
            stripped = line.strip()
            if not stripped:
                consumed += len(line)
                continue
            if stripped.startswith("#"):
                break
            if re.match(r"^\s*(?:[-*+•]|\d+[.、)]|\|)", line):
                consumed += len(line)
                continue
            break
        end = match.end() + consumed
        removed.append(text[match.start():end].strip())
        text = text[:match.start()] + text[end:]
    return text, list(reversed(removed))


#: 从内联记号里抠出证据编号本身(EV-001 / BF-12),用于回查 manifest。
_EVIDENCE_ID_IN_MARKER = re.compile(r"((?:EV|BF)[-‑—]\s*\d+)", re.I)


def _attribution_index(
    evidence_pack: dict[str, Any] | None,
    client_domains: tuple[str, ...] = (),
    self_names: tuple[str, ...] = (),
) -> dict[str, str]:
    """``{"EV-001": "据中国电梯 2025 年 3 月", ...}``。

    🔴 [D5 信源增援 2026-08-10] 取值经 `evidence_pack.attribution_of()` 现取,
    不在这里拼字符串 —— 改那边的规则,这里必须跟着变(反向对照锁验)。
    """
    if not isinstance(evidence_pack, dict):
        return {}
    try:
        from writing.evidence_pack import attribution_of
    except Exception:  # noqa: BLE001 - 取不到就退回"删除"老行为,绝不阻断
        return {}
    index: dict[str, str] = {}
    # [R5] 归属句索引会**替换进正文** = 消费 → 唯一消费 API;候选条目不入
    # 索引,其 marker 走下方既有 fail-closed 回退(删除)。
    from writing.evidence_pack import iter_entity_admissible_items

    for item in iter_entity_admissible_items(evidence_pack):
        eid = str(item.get("evidence_id") or "").strip().upper()
        # 🔴 [复审返工 2026-08-10] fail-closed 同源:`attribution_of` 判不出
        # 独立第三方就返回空 prose,本函数于是不入索引 → `_replace_marker`
        # **回退到删除**(复审要的修法)。这里不再自己判一遍来源形态。
        prose = attribution_of(
            item, client_domains=client_domains, self_names=self_names,
        ).get("prose") or ""
        if eid and prose:
            index[eid] = prose
    return index


#: 承担事实主张的**硬数字**。裸年份(2025 年)不算 —— 它是时间状语不是主张,
#: 这是我在 gate2 判据上已经踩过一次的坑,这里直接排除掉。
_BARE_YEAR_TOKEN = re.compile(r"(?:19|20)\d{2}\s*年")
_HARD_NUMBER = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|‰|万|亿|倍|个月|天|小时|分钟|元|万元|亿元|"
    r"公里|千米|米|毫米|㎡|平方米|吨|公斤|人|名|家|次|项|款|台|套|条|年)"
)


def _has_hard_number_claim(sentence: str) -> bool:
    """这句话是不是承担了一个**带数量级的事实主张**。"""
    text = _BARE_YEAR_TOKEN.sub(" ", str(sentence or ""))
    return bool(_HARD_NUMBER.search(text))


# 🔴🔴 [最终接管 2026-08-10 · 工单 §9] claim 级整条删除的实现体
# `_drop_unsourced_number_claims` **不移植、不换名恢复**(旧归档里它已被
# P0-5 撤销为"定义着但不接线"的死实现)。裁定理由留档:
#   · 证据不足不能成为删除整条客户事实的理由 —— 中小企业客户的交付量/质保/
#     响应时长**只可能来自自己的材料**,删光 = 客户唯一能被 AI 复述成推荐
#     理由的素材被删光;
#   · 现契约 = 编号挂得上独立第三方就换归属句,挂不上就只删编号,**主张留下**
#     (不带假来源、不带内部编号);
#   · `tests/test_evidence_attribution_supply_2026_08_10.py` 的接线锁钉死
#     "claim 级删除不得回到保存链"。
# `_has_hard_number_claim` 保留:它是"裸年份不算硬数字"的判据 SSOT,
# `content_cleaner` 与测试复用它,不许各抄一份。


def self_names_from_article(article: dict[str, Any] | None) -> tuple[str, ...]:
    """从 article dict 里取客户品牌名(用于判「自有渠道」)。

    🔴 [复审返工 2026-08-10] 保存链没有 `self.brand_name` 可用,品牌名散在
    两个键上:顶层 `brand_name` 与 `brand_fact_snapshot.brand_name`
    (后者是 `writing/brand_fact_snapshot.py:64` 写入的)。两个都取,去重去空。
    取不到就返回空 —— 那时 fail-closed 仍然挡得住裸域名,只是少一道纵深。
    """
    if not isinstance(article, dict):
        return ()
    names: list[str] = []
    for value in (
        article.get("brand_name"),
        (article.get("brand_fact_snapshot") or {}).get("brand_name")
        if isinstance(article.get("brand_fact_snapshot"), dict) else None,
    ):
        text = str(value or "").strip()
        if text and text not in names:
            names.append(text)
    return tuple(names)


def sanitize_article_body(
    content: str, evidence_pack: dict[str, Any] | None = None,
    self_names: tuple[str, ...] = (),
) -> tuple[str, dict[str, Any]]:
    """把正文里的内部记号剥掉，返回 (清洗后正文, internal_markers)。

    永不抛异常、永不返回空正文（清洗把全文洗光时保留原文，宁可留记号也不丢稿）。

    🔴 [D5 信源增援 2026-08-10] 传了 ``evidence_pack`` 时，内联证据编号
    ``〔EV-001〕`` 会被**换成真实归属句**（「据中国电梯 2025 年 3 月」），
    而不是像以前那样直接删掉。

    为什么要改：旧行为是 ``pattern.sub("", text)`` —— 只抹编号、不补归属。
    于是模型按合同打了编号的那句话，保存后变成**没有来源的裸数字**。
    生产实测 5,185 条 evidence item 里 publisher 填充率 100%、日期 90%，
    而 publisher 出现在正文里只有 4.6% —— 素材一直都在，是这一步把它扔了。
    没传 pack 时退回老行为（删除），保持向后兼容。
    """
    original = content or ""
    if not original.strip():
        return original, {}

    removed: dict[str, list[str]] = {
        "evidence_ids": [], "status_tables": [],
        "review_phrase_lines": [], "checklist_sections": [],
    }
    restored: list[str] = []
    text = original

    text, removed["checklist_sections"] = _strip_checklist_sections(text)
    text, removed["status_tables"] = _strip_table_block(text)

    attribution = _attribution_index(evidence_pack, self_names=self_names)

    # 🔴 [复审返工 2026-08-10 · P1-2] claim 级清洗必须排在剥编号**之前** ——
    # 编号一旦删掉,就再也认不出"这句话原本声称有证据支撑"了。
    # 🔴 门槛是「pack 里真有可用条目」,不是「pack 是个 dict」。
    # 实测:`{"items": "x"}` / `{"items": [None, 3]}` 都是 dict,但它们表示
    # **没有判断依据**,不表示「这条主张没来源」—— 上一版在这里把有效正文
    # 连同 98.6% 一起删了(`test_bad_pack_never_raises` 抓到)。
    # 🔴🔴 [P0-5 撤销 2026-08-10] 原本在这里调 `_drop_unsourced_number_claims`,
    # 把"挂不上归属 + 带硬数字"的句子整条删掉。**已撤销,不再调用。**
    # 同 content_cleaner 的理由:删的正是客户唯一能拿出来的具体事实。
    # 编号仍会被剥掉(见下方 `_replace_marker`):有独立第三方归属就换成归属句,
    # 没有就只删编号 —— **主张留下**,不带假来源,也不带内部编号。

    def _replace_marker(match: re.Match[str]) -> str:
        marker = match.group(0)
        removed["evidence_ids"].append(marker)
        eid_m = _EVIDENCE_ID_IN_MARKER.search(marker)
        if not eid_m:
            return ""
        eid = re.sub(r"\s+", "", eid_m.group(1)).upper().replace("‑", "-").replace("—", "-")
        prose = attribution.get(eid)
        if not prose:
            return ""          # manifest 里没有归属 → 退回删除
        restored.append(f"{eid}→{prose}")
        return f"（{prose}）"

    for pattern in _INLINE_ID_PATTERNS:
        text = pattern.sub(_replace_marker, text)
    if restored:
        removed["restored_attributions"] = restored  # type: ignore[assignment]

    kept_lines: list[str] = []
    for line in text.splitlines():
        if not _REVIEW_LINE.match(line or ""):
            kept_lines.append(line)
            continue
        # 按**句**剥除，不删整行：同一行里的合法内容必须留下
        # （实测缺陷：整行删会连带丢掉合法首句，已由判别测试锁死）。
        sentences = re.split(r"(?<=[。！？；;])", line)
        kept: list[str] = []
        dropped: list[str] = []
        for sentence in sentences:
            if any(phrase in sentence for phrase in _REVIEW_PHRASES):
                if sentence.strip():
                    dropped.append(sentence.strip())
            else:
                kept.append(sentence)
        rebuilt = "".join(kept).rstrip()
        if dropped:
            removed["review_phrase_lines"].extend(dropped)
        if re.sub(r"[\s，,、。;；:：·\-]", "", rebuilt):
            kept_lines.append(rebuilt)
    text = "\n".join(kept_lines)

    # 清理剥除留下的空括号/多余空行/行尾空白
    text = re.sub(r"[（(〔【「『［]\s*[）)〕】」』］]", "", text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if not text.strip():
        # 全洗光 = 清洗器判断出错，宁可保留原文也不丢稿（D8 保存永不失败）。
        return original, {
            "sanitizer_version": SANITIZER_VERSION,
            "skipped_reason": "sanitized_body_empty_kept_original",
        }

    total = sum(len(v) for v in removed.values())
    if not total:
        return text, {}
    return text, {
        "sanitizer_version": SANITIZER_VERSION,
        "removed_counts": {k: len(v) for k, v in removed.items()},
        **{k: v for k, v in removed.items() if v},
    }


def sanitize_article_for_save(article: dict[str, Any]) -> dict[str, Any]:
    """保存链唯一入口：清洗 ``article["content"]`` 并把剥下的记号并入 quality_warning。

    **必须在 ``build_article_lineage`` 之前调用** —— lineage 用 ``article["content"]``
    算 ``current_content_hash``；若在其后清洗，入库正文与 hash 不一致 = 对象身份
    漂移（H0）。返回同一个 dict（就地更新），方便调用点原样继续。

    D8 零阻断：**永不抛异常、永不丢稿**。清洗器任何异常都吞掉并原样返回，
    宁可正文里留记号，也不能因为洗不动就让文章存不下来。
    """
    if not isinstance(article, dict):
        return article
    try:
        original = article.get("content")
        if not isinstance(original, str) or not original.strip():
            return article
        # 🔴 [D5] 把 evidence_pack 传进去 —— 不传的话内联编号只会被删掉,
        # 那句话就变成没有来源的裸数字(接线锁打在这个实参上)。
        # 🔴 [复审返工 2026-08-10] 第三个实参是新增的接线:没有它,
        # publisher 带客户自己名字的条目会被换成假的第三方归属句。
        cleaned, markers = sanitize_article_body(
            original, article.get("evidence_pack"),
            self_names=self_names_from_article(article),
        )
        if not cleaned.strip():
            return article
        article["content"] = cleaned
        if markers:
            warning = article.get("quality_warning")
            if not isinstance(warning, dict):
                warning = {"legacy": warning} if warning else {}
            warning["internal_markers"] = markers
            article["quality_warning"] = warning
    except Exception:  # noqa: BLE001 - 清洗失败绝不能阻断保存
        logger.warning("[D11] 正文清洗失败，按原文保存（不阻断）", exc_info=True)
    return article


# ---------------------------------------------------------------------------
# H0 判别：引用必须真实存在（D11 ⑤ 唯一保留的造假底线）
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s)）\]】,，。;；\"']+")


def verify_citations_against_manifest(
    content: str, evidence_pack: dict[str, Any] | None,
) -> dict[str, Any]:
    """正文中出现的 URL 必须在 evidence manifest 里真实存在。

    D11 ⑤：可以引用检索到的真实公开信源，但**严禁编造 URL/文献/数据**。
    返回 {"ok": bool, "fabricated_urls": [...], "manifest_size": int}。
    只做判别不做阻断——调用方按 H0 处理（正文引用造假是造假，不是文风问题）。
    """
    manifest: set[str] = set()
    # [R5] URL 存在性判别与实体轴无关(候选条目的 URL 也是真实 URL,不算
    # 编造)→ 结构访问器,不走消费门。
    from writing.evidence_pack import raw_pack_items

    for item in raw_pack_items(evidence_pack):
        url = str((item or {}).get("url") or "").strip()
        if url:
            manifest.add(url.rstrip("/"))
    found = {u.rstrip("/") for u in _URL_RE.findall(content or "")}
    fabricated = sorted(u for u in found if u not in manifest)
    return {
        "ok": not fabricated,
        "fabricated_urls": fabricated,
        "cited_urls": sorted(found),
        "manifest_size": len(manifest),
    }
