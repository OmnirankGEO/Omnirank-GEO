"""[P4 标题批次级去重 2026-07-31] 保存前的批次级标题多样性收敛。

工单 `WORKORDER_REVIEW_GATE_AND_BATCH_AUDIT_2026-07-30.md` §7。

**病在哪(执行路径已自验)**
`keyword_topic_generator._safe_fallback_title` 用
``family_templates[slot_index % len(family_templates)]`` 选兜底模板,而每个文体族
**只有 3 条模板**。于是:
- 同一关键词要 4 篇 → 第 4 篇必与第 1 篇同模板;
- **更常见的是跨关键词**:不同关键词只要落到同一 (style, slot),就拿到同一条模板,
  整批看上去"全是一个公式换个词"。
生产近 30 天仍在产生完全重复标题(quote 94 · 2026-07-30),证明这条路是活的。

**四类检查(工单 §7 原文)**
1. 完全重复(归一化后一字不差)
2. 去年份/标点后的**模板重复**(同一公式换个关键词)
3. 高语义相似
4. 同一公式反复使用

**🔴 红线(工单原文)**:不得为多样性更换客户购买的问题、地区、产品或采购意图。
所以每个候选标题都要过 `assess_title_keyword_alignment` 复核;**改坏关键词身份的
候选一律丢弃**,宁可保留一个重复标题也不许动客户买的词。

**🔴 不设武断文体配额**:本模块只看"标题字面/公式是否重复",不碰 article_style
分布(文体配比是 `title_question_policy` / `direction_distribution` 的职责)。

**顺序:排在 `enforce_question_ratio` 之前**(我第一版写反了,被回归 A/B 抓出来)。
形态层会改写标题(`title_question_policy.py:471/483`),它必须是**最后一句话**:
  · 去重放在形态层之后 → 把刚收敛好的问句式比例重新打乱(`test_t1_lock1_*` 由
    7/10 变 9/10 超出容差);
  · 用户显式指定形态时形态层**早退不介入**,去重若此时换成问句式模板 =
    绕过形态层违反"用户 > 默认"。
形态收敛**之后**可能重新出现的相近公式,由 `_finalize_titles` 做**只探测不改写**
的复核并如实记账(再改写又会打乱比例)。`preserve_form` 只在用户锁定形态时打开。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Sequence

from writing.title_keyword_alignment import (
    assess_title_keyword_alignment,
    normalize_semantic_text,
    title_anchor_from_purchased_keyword,
)

#: 年份/纯数字 → 占位。去掉它们之后"2026年"与"2027年"版是同一个公式。
_YEAR_RE = re.compile(r"20\d{2}\s*年?")
_DIGIT_RE = re.compile(r"\d+")
#: 骨架比较时丢弃的装饰性符号(不影响公式判定)。
_PUNCT_RE = re.compile(r"[\s｜|丨:：;；,，.。!！?？、~—\-—_/\\()（）\[\]【】《》\"'“”‘’]+")

#: 语义相似判定阈值(2-gram Jaccard)。0.90 是刻意保守的:
#: 目标是抓"换个词的同一句话",不是抓"同一话题的两个角度"。
SEMANTIC_SIMILARITY_THRESHOLD = 0.90


def normalize_exact(title: object) -> str:
    """完全重复判定用的归一化(全角/半角、大小写、空白)。"""
    text = unicodedata.normalize("NFKC", str(title or "")).strip().lower()
    return _PUNCT_RE.sub("", text)


def title_formula_skeleton(title: object, keyword: object = None) -> str:
    """把标题压成**公式骨架**:去掉年份、数字、以及关键词锚本身。

    这样 `深圳装修怎么选？2026年多品牌同字段比较方法` 与
    `广州装修怎么选？2026年多品牌同字段比较方法` 落到同一骨架 —— 它们**不是**完全
    重复(关键词不同),但确实是"同一公式反复使用",正是工单第 2/4 类要抓的。
    """
    text = unicodedata.normalize("NFKC", str(title or ""))
    if keyword:
        anchor = title_anchor_from_purchased_keyword(keyword)
        # 先去长的再去短的,避免子串残留
        for token in sorted({str(keyword or ""), anchor}, key=len, reverse=True):
            if token:
                text = text.replace(token, "")
    text = _YEAR_RE.sub("", text)
    text = _DIGIT_RE.sub("", text)
    return _PUNCT_RE.sub("", text.lower())


def _bigrams(text: str) -> frozenset[str]:
    if len(text) < 2:
        return frozenset({text} if text else set())
    return frozenset(text[i:i + 2] for i in range(len(text) - 1))


def semantic_similarity(a: str, b: str) -> float:
    """2-gram Jaccard。纯函数、无外部依赖、结果可复现。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 1.0 if ga == gb else 0.0
    return len(ga & gb) / len(ga | gb)


# ---------------------------------------------------------------------------
# 🔴 [标题 AI-only 2026-08-17 · Owner 裁决] 模板选择器**已整体退役**。
#
# 退役符号(本文件内已删除,全仓零 caller):
#   `_stable_seed` / `template_is_question` / `filter_templates_by_form`
#   / `pick_diverse_template`
#
# 它们的唯一数据源是 `keyword_topic_generator` 的 6 族 × 3 条硬编码模板表,
# 那张表已随本裁决退役。去重冲突后的替换标题现在**只能来自 AI**
# (`candidates_by_index` = 一次性 LLM 重生成 / `regenerate` 回调);
# 两条都拿不出合格候选就如实记 `unresolved`,保留原来的 AI 标题 ——
# 一个重复的 AI 标题,好过一个十个客户共用的模板标题。
# ---------------------------------------------------------------------------


def _iter_conflict(
    title: str,
    keyword: object,
    *,
    used_exact: set[str],
    used_skeleton: set[str],
    used_semantic: List[str],
) -> str | None:
    """返回冲突类型;无冲突返回 None。"""
    exact = normalize_exact(title)
    if not exact:
        return None
    if exact in used_exact:
        return "exact_duplicate"
    skeleton = title_formula_skeleton(title, keyword)
    if skeleton and skeleton in used_skeleton:
        return "formula_reuse"
    normalized = normalize_semantic_text(title)
    for seen in used_semantic:
        if semantic_similarity(normalized, seen) >= SEMANTIC_SIMILARITY_THRESHOLD:
            return "semantic_near_duplicate"
    return None


def _is_question(title: str) -> bool:
    try:
        from writing.title_question_policy import is_question_title
        return bool(is_question_title(title))
    except Exception:
        return False


def _same_title_form(candidate: str, original: str) -> bool:
    """替换标题必须与原标题**同形态**(问句式 / 陈述式)。

    🔴 这条是回归 A/B 逼出来的真 bug,不是洁癖:用户显式指定 `title_form='open'`
    时,形态收敛层会早退**完全不介入**(工单 §1.2 用户 > 默认)。若去重此时把标题
    换成 `…怎么选？` 这类问句式兜底模板,等于**绕过形态层违反用户的显式选择**。
    保持同形态后,去重只换结构、不换形态,两条规则不再打架。
    """
    try:
        from writing.title_question_policy import is_question_title

        return bool(is_question_title(candidate)) == bool(is_question_title(original))
    except Exception:
        return True  # 判不出来就不拿形态卡人(去重本身仍受身份红线约束)


def _keyword_identity_preserved(title: str, keyword: object) -> bool:
    """🔴 红线复核:去重产生的新标题必须仍然扣住客户购买的关键词身份。"""
    if not str(keyword or "").strip():
        return True
    try:
        return bool(assess_title_keyword_alignment(title, keyword).aligned)
    except Exception:
        # 判不出来就当没保住(宁可不换标题,也不许把客户买的词换掉)
        return False


def _seed_used(
    existing_titles: Iterable[str],
) -> tuple[set[str], set[str], List[str]]:
    used_exact: set[str] = set()
    used_skeleton: set[str] = set()
    used_semantic: List[str] = []
    for existing in existing_titles:
        text = str(existing or "")
        if not text:
            continue
        used_exact.add(normalize_exact(text))
        skeleton = title_formula_skeleton(text, None)
        if skeleton:
            used_skeleton.add(skeleton)
        used_semantic.append(normalize_semantic_text(text))
    return used_exact, used_skeleton, used_semantic


def detect_title_conflicts(
    topics: List[Dict[str, Any]],
    *,
    existing_titles: Iterable[str] = (),
    title_key: str = "optimized_title",
    keyword_key: str = "original_keyword",
) -> List[Dict[str, Any]]:
    """**只探测、不改任何东西**。给"先探测 → 一次性重生成冲突项"这条链用。

    分成两步是刻意的:工单要求"冲突只重生成冲突项,不重跑整批"。先拿到冲突清单,
    才能把**恰好那几条**打进一次重生成请求,而不是每条一次(N 次调用)或整批重跑。
    """
    used_exact, used_skeleton, used_semantic = _seed_used(existing_titles)
    conflicts: List[Dict[str, Any]] = []
    for index, topic in enumerate(topics):
        if not isinstance(topic, dict):
            continue
        title = str(topic.get(title_key) or "")
        if not title:
            continue
        keyword = topic.get(keyword_key)
        reason = _iter_conflict(
            title, keyword,
            used_exact=used_exact, used_skeleton=used_skeleton, used_semantic=used_semantic,
        )
        if reason is not None:
            conflicts.append({
                "index": index, "reason": reason, "title": title,
                "keyword": keyword, "article_style": topic.get("article_style"),
                "angle": topic.get("angle"),
            })
        used_exact.add(normalize_exact(title))
        skeleton = title_formula_skeleton(title, keyword)
        if skeleton:
            used_skeleton.add(skeleton)
        used_semantic.append(normalize_semantic_text(title))
    return conflicts


def dedupe_topic_titles(
    topics: List[Dict[str, Any]],
    *,
    existing_titles: Iterable[str] = (),
    regenerate: Callable[[Dict[str, Any], List[str]], Iterable[str]] | None = None,
    candidates_by_index: Dict[int, Sequence[str]] | None = None,
    # 形态保护只在**形态层不会兜底**时才需要(即用户显式指定了 title_form,
    # `enforce_question_ratio` 会早退)。常规链路里形态层排在去重之后,会把形态
    # 统一收敛,这里强行保形只会白白把可用模板池再砍一半。默认关,由调用方按
    # `bool(self.title_form)` 显式打开。
    preserve_form: bool = False,
    title_key: str = "optimized_title",
    keyword_key: str = "original_keyword",
) -> Dict[str, Any]:
    """批次级去重。**就地只改冲突项**,非冲突项一个字节都不碰。

    Args:
        existing_titles: 同 quote/project 下**已存在(未发布)**的标题。工单要求
            "同时检查同 quote/project 已存在的未发布标题" —— 只跟本批比会让第二批
            原样复现第一批。
        regenerate: ``(topic, used_titles) -> 候选标题序列``。用于把"本批已用标题与
            已用表达角度"带进重生成提示词。返回的候选逐个过红线复核,第一个既不冲突
            又保住关键词身份的胜出;全不合格则退到结构化兜底。

    Returns:
        report dict(调用方打印/落审计用)。topics 就地修改。
    """
    # [标题 AI-only 2026-08-17] 曾经从 `keyword_topic_generator` import
    # 硬编码模板表来"结构化兜底"。该表已整体退役 —— 去重换出来的标题
    # **同样必须是 AI 产物**,要不到就如实记 `unresolved`(保留原 AI 标题),
    # 绝不换成模板串。
    used_exact, used_skeleton, used_semantic = _seed_used(existing_titles)
    report: Dict[str, Any] = {
        "total": len(topics),
        "existing_considered": len(used_exact),
        "conflicts": 0,
        "regenerated": 0,
        "unresolved": 0,
        "identity_rejected": 0,
        "form_rejected": 0,
        "by_reason": {},
        "changed_topic_indexes": [],
    }
    for index, topic in enumerate(topics):
        if not isinstance(topic, dict):
            continue
        title = str(topic.get(title_key) or "")
        keyword = topic.get(keyword_key)
        if not title:
            continue

        reason = _iter_conflict(
            title, keyword,
            used_exact=used_exact, used_skeleton=used_skeleton, used_semantic=used_semantic,
        )
        if reason is None:
            # 非冲突项:一个字节都不动(工单锁 ③)
            used_exact.add(normalize_exact(title))
            skeleton = title_formula_skeleton(title, keyword)
            if skeleton:
                used_skeleton.add(skeleton)
            used_semantic.append(normalize_semantic_text(title))
            continue

        report["conflicts"] += 1
        report["by_reason"][reason] = report["by_reason"].get(reason, 0) + 1

        candidates: List[str] = []
        # 候选**只能**来自 AI:① 一次性重生成好的 LLM 候选 ② regenerate 回调
        # (回调本身也是 AI 路径)。两者都拿不出合格候选 → `unresolved`。
        if candidates_by_index:
            candidates.extend(str(c) for c in (candidates_by_index.get(index) or []))
        if regenerate is not None:
            try:
                candidates.extend(str(c) for c in (regenerate(topic, sorted(used_exact)) or []))
            except Exception:
                pass  # 重生成失败绝不阻断选题产出(与本仓既有兜底口径一致)

        resolved = False
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate:
                continue
            if preserve_form and not _same_title_form(candidate, title):
                report["form_rejected"] += 1
                continue  # 🔴 不得绕过形态层改变用户显式选择的标题形态
            if not _keyword_identity_preserved(candidate, keyword):
                report["identity_rejected"] += 1
                continue  # 🔴 红线:宁可留重复,也不改客户买的词
            if _iter_conflict(
                candidate, keyword,
                used_exact=used_exact, used_skeleton=used_skeleton, used_semantic=used_semantic,
            ) is not None:
                continue
            topic[title_key] = candidate
            topic["title_dedupe_status"] = "regenerated"
            topic["title_dedupe_reason"] = reason
            report["regenerated"] += 1
            report["changed_topic_indexes"].append(index)
            title = candidate
            resolved = True
            break

        if not resolved:
            # 🔴 无声上限是不允许的:分化不出来就如实标记,别假装去重成功了。
            topic["title_dedupe_status"] = "unresolved"
            topic["title_dedupe_reason"] = reason
            report["unresolved"] += 1

        used_exact.add(normalize_exact(title))
        skeleton = title_formula_skeleton(title, keyword)
        if skeleton:
            used_skeleton.add(skeleton)
        used_semantic.append(normalize_semantic_text(title))

    return report
