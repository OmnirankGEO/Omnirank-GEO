"""
article_fact_check · C3.2 (CTO-15.9 session 3 · 2026-04-25)

Codex 0424 P1.5 文章事实核验层骨架(启动版 · 不调真 LLM 后续可加 LLM cross-check)

检测维度(全部启发式 · 不依赖 LLM):
  1. 虚构数字/百分比 · 数字声明无来源(% / 万 / 亿 / 倍)且无引用
  2. 虚构奖项 · "国家级" / "第一" / "唯一" 等绝对词(对齐 banned_words 广告法)
  3. 虚构案例 · "XX 案例" 但实际客户列表里无该客户
  4. 时效性问题 · "近 N 年" / "今年" / "最新" 但文章已发布超 N 天

返:
  {
    "has_risks": bool,
    "risks": [{
      "category": 'fake_data' | 'fake_award' | 'fake_case' | 'stale',
      "severity": 'high' | 'medium' | 'low',
      "phrase": str,        # 命中文字
      "context": str,       # 命中上下文(50 字内)
      "suggestion": str,    # 修改建议
    }],
    "summary": str,         # 人类可读总结
  }

后续可加(留 TODO):
- LLM cross-check(把可疑点丢给 advisor 验真伪 · API 成本 · 老板批后启)
- 客户名单交叉(从 quotes/brands 拉真案例名单)
"""
from __future__ import annotations
import logging
import re
from typing import Optional

logger = logging.getLogger("GEO-ArticleFactCheck")


# ============ 启发式规则集 ============

# 数字声明 pattern
_NUMBER_CLAIM_PATTERNS = [
    re.compile(r'(\d+(?:\.\d+)?\s*[%％])'),                # 73% / 12.5%
    re.compile(r'(\d+(?:\.\d+)?\s*[万亿千百])'),            # 5 万 / 200 万
    re.compile(r'(\d+(?:\.\d+)?\s*倍)'),                   # 3 倍 / 10 倍
    re.compile(r'(超过\s*\d+(?:\.\d+)?)'),                 # 超过 100 / 超过 1000 万
    re.compile(r'(\d+(?:\.\d+)?\s*年(?:连续)?第一)'),       # 5 年第一
]

# 引用源标识(数字旁边有这些 = 不算虚构)
_CITATION_MARKERS = [
    "来源", "据", "数据来自", "根据", "引自", "参考",
    "（截至", "(截至", "[", "（来源", "(来源",
    "http://", "https://",
    "报告", "白皮书", "调研", "统计",
]

# 虚构奖项关键词(广告法红线 + 行业经验)
_FAKE_AWARD_PATTERNS = [
    "国家级", "国家认证", "国家颁发",
    "中国第一", "全国第一", "行业第一",
    "唯一", "独家", "首创",
    "金奖", "权威认证", "央视报道",
]

# 时效性词
_STALENESS_PATTERNS = [
    re.compile(r'(今年|本年|本季度)'),
    re.compile(r'(近\s*\d+\s*年(?:内)?)'),
    re.compile(r'(最新|刚刚|最近)'),
]


def _extract_context(text: str, match_start: int, match_end: int, window: int = 25) -> str:
    """抽取命中上下文(前后 N 字)"""
    start = max(0, match_start - window)
    end = min(len(text), match_end + window)
    snippet = text[start:end].replace('\n', ' ')
    return ("..." if start > 0 else "") + snippet + ("..." if end < len(text) else "")


def _has_citation_nearby(text: str, pos: int, window: int = 60) -> bool:
    """命中数字附近有引用标识 = 算真实"""
    chunk = text[max(0, pos - window): min(len(text), pos + window)].lower()
    return any(m.lower() in chunk for m in _CITATION_MARKERS)


def check_article(content: str, *, real_client_names: Optional[list[str]] = None) -> dict:
    """文章事实核验主入口

    Args:
        content: 文章 markdown 或纯文本
        real_client_names: 可选 · 真实客户名单(用于核 fake_case)

    Returns:
        {has_risks, risks: [...], summary}
    """
    if not content:
        return {"has_risks": False, "risks": [], "summary": "空文章 · 无风险"}

    risks: list[dict] = []
    real_names = set(n.strip() for n in (real_client_names or []) if n and n.strip())

    # 1. 虚构数字
    for pat in _NUMBER_CLAIM_PATTERNS:
        for m in pat.finditer(content):
            if _has_citation_nearby(content, m.start()):
                continue
            risks.append({
                "category": "fake_data",
                "severity": "medium",
                "phrase": m.group(1),
                "context": _extract_context(content, m.start(), m.end()),
                "suggestion": f"数字 '{m.group(1)}' 缺引用源 · 加 (来源: XXX) 或删除",
            })

    # 2. 虚构奖项
    for kw in _FAKE_AWARD_PATTERNS:
        idx = content.find(kw)
        while idx != -1:
            risks.append({
                "category": "fake_award",
                "severity": "high",
                "phrase": kw,
                "context": _extract_context(content, idx, idx + len(kw)),
                "suggestion": f"'{kw}' 触广告法极限词 · 改 '业内领先' '专业服务' 等中性表达",
            })
            idx = content.find(kw, idx + 1)

    # 3. 虚构案例(检测 "XX 客户" 模式 + 与 real_client_names 比对)
    if real_names:
        case_pat = re.compile(r'([A-Za-z一-龥]{2,12})\s*(?:客户|公司|集团|品牌|案例)')
        for m in case_pat.finditer(content):
            mentioned = m.group(1).strip()
            if mentioned and mentioned not in real_names and mentioned != "我们":
                risks.append({
                    "category": "fake_case",
                    "severity": "medium",
                    "phrase": mentioned,
                    "context": _extract_context(content, m.start(), m.end()),
                    "suggestion": f"'{mentioned}' 不在已知客户列表 · 核实是否真实案例 · 否则改为 '某行业客户' 类匿名表述",
                })

    # 4. 时效性
    for pat in _STALENESS_PATTERNS:
        for m in pat.finditer(content):
            risks.append({
                "category": "stale",
                "severity": "low",
                "phrase": m.group(1),
                "context": _extract_context(content, m.start(), m.end()),
                "suggestion": f"时效词 '{m.group(1)}' · 30 天后会显得过时 · 建议改为具体年份 (如 2025 年)",
            })

    # 去重(同 category + phrase)
    seen = set()
    deduped: list[dict] = []
    for r in risks:
        key = (r["category"], r["phrase"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    # 按 severity 排
    sev_order = {"high": 0, "medium": 1, "low": 2}
    deduped.sort(key=lambda r: sev_order.get(r["severity"], 3))

    has_risks = bool(deduped)
    if not has_risks:
        summary = "✓ 未发现明显虚构/广告法/时效性风险"
    else:
        high_count = sum(1 for r in deduped if r["severity"] == "high")
        medium_count = sum(1 for r in deduped if r["severity"] == "medium")
        low_count = sum(1 for r in deduped if r["severity"] == "low")
        parts = []
        if high_count:
            parts.append(f"高风险 {high_count} 条(广告法极限词 · 必改)")
        if medium_count:
            parts.append(f"中风险 {medium_count} 条(数据/案例缺源 · 建议加引用)")
        if low_count:
            parts.append(f"低风险 {low_count} 条(时效词 · 30 天后过时)")
        summary = " · ".join(parts)

    return {
        "has_risks": has_risks,
        "risks": deduped,
        "summary": summary,
        "high_count": sum(1 for r in deduped if r["severity"] == "high"),
        "medium_count": sum(1 for r in deduped if r["severity"] == "medium"),
        "low_count": sum(1 for r in deduped if r["severity"] == "low"),
    }
