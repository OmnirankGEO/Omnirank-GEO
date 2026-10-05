"""
scoring_levels — 元指令 14 · 评分等级 SSOT(顶部结论/正文等级/前端徽章/导出 PDF 同源)

CTO-15.9 2026-04-25 · M2 T1

PRD M2 §R2 · 评分 3 处打架禁令(顶部 60 / 正文 55 / 徽章 65):
  SSOT · 所有调用方共用本模块
  改评分规则 · 只改本模块 · 其他地方自动跟随(类似 FeatureCostBadge SSOT 模式)

6 档级别(保前端 DiagnosisReport.getLevelColor 已有配色):
  >= 85 领先    emerald
  70-84  成熟    green
  55-69  成长    brand(indigo)
  40-54  起步    amber
  20-39  待提升  orange
  0-19   空白    muted

使用:
  from tools.scoring.scoring_levels import get_level, get_summary, LEVEL_META
  level = get_level(total_score)        # → "成长"
  meta  = LEVEL_META[level]             # → {"color": "brand", "min": 55, "max": 69, "label": "成长", ...}
  summary = get_summary(total_score, industry=None)  # 一句话综述
"""
from __future__ import annotations

from typing import Optional


LEVEL_META: dict[str, dict[str, object]] = {
    "领先": {
        "label": "领先",
        "min": 85, "max": 100,
        "color": "emerald",    # 前端 Tailwind 色名(DiagnosisReport.tsx 已对齐)
        "badge_class": "bg-emerald-500 text-white",
        "summary": "品牌在 AI 搜索中已建立领先地位 · 保持现有优势 · 关注竞品反超信号",
    },
    "成熟": {
        "label": "成熟",
        "min": 70, "max": 84,
        "color": "green",
        "badge_class": "bg-green-500 text-white",
        "summary": "品牌在 AI 搜索中表现成熟稳定 · 建议巩固头部内容 + 扩品类场景词",
    },
    "成长": {
        "label": "成长",
        "min": 55, "max": 69,
        "color": "brand",
        "badge_class": "bg-brand text-white",
        "summary": "品牌具备一定基础 · 建议重点提升 AI 引擎可见度 + 结构化内容",
    },
    "起步": {
        "label": "起步",
        "min": 40, "max": 54,
        "color": "amber",
        "badge_class": "bg-amber-500 text-white",
        "summary": "品牌 AI 搜索覆盖处于起步阶段 · 建议系统化 GEO 优化 · 30 天见效",
    },
    "待提升": {
        "label": "待提升",
        "min": 20, "max": 39,
        "color": "orange",
        "badge_class": "bg-orange-500 text-white",
        "summary": "品牌 AI 搜索可见度不足 · 建议加强基础建设 + 内容补强 · 45-60 天见效",
    },
    "空白": {
        "label": "空白",
        "min": 0, "max": 19,
        "color": "muted",
        "badge_class": "bg-muted-foreground text-white",
        "summary": "品牌在 AI 搜索中几乎不可见 · 从资料/关键词/首批文章全链启动 · 90 天系统建设",
    },
}

# 从高到低排序(get_level 查找用)
_LEVELS_SORTED: list[tuple[str, int, int]] = sorted(
    [(name, int(meta["min"]), int(meta["max"])) for name, meta in LEVEL_META.items()],
    key=lambda x: -x[1],  # 按 min 降序
)


def get_level(total_score: float | int | None) -> str:
    """根据总分返对应等级名 · 保证可映射

    Args:
        total_score: 0-100

    Returns:
        "领先" / "成熟" / "成长" / "起步" / "待提升" / "空白"
    """
    if total_score is None:
        return "空白"
    try:
        s = float(total_score)
    except (TypeError, ValueError):
        return "空白"
    s = max(0.0, min(100.0, s))
    for name, lo, hi in _LEVELS_SORTED:
        if s >= lo:
            return name
    return "空白"


def get_meta(total_score: float | int | None) -> dict[str, object]:
    """返回 {level, label, color, badge_class, summary, min, max}"""
    level = get_level(total_score)
    return {"level": level, **LEVEL_META[level]}


def get_summary(total_score: float | int | None, industry: Optional[str] = None) -> str:
    """一句话综述 · 报告顶部结论 + 正文等级表 + 前端徽章 必共用"""
    meta = get_meta(total_score)
    base = str(meta.get("summary", ""))
    if industry:
        return f"{industry}行业 · {base}"
    return base


def list_all_levels() -> list[dict[str, object]]:
    """返回所有 6 档 · 用于报告"等级说明"段渲染 · 按分数从高到低"""
    return [
        {"level": name, **LEVEL_META[name]}
        for name, _lo, _hi in _LEVELS_SORTED
    ]


# 报告 Module 1(封面结论)LLM prompt 拼装用 · 3 行模板
def build_cover_conclusion(
    total_score: float | int,
    industry: Optional[str] = None,
) -> str:
    """Module 1 · 封面结论(50-80 字)· M2 PRD §Epic 1"""
    meta = get_meta(total_score)
    level = meta["level"]
    summary = meta.get("summary", "")
    score_int = int(total_score or 0)
    prefix = f"{industry}行业 · " if industry else ""
    return f"{prefix}GEO 总分 {score_int}/100 · {level}级 · {summary}"


# CTO-G 2026-04-27 · 漏斗评分新等级 · 不替换 v1 LEVEL_META(向后兼容)
# 调用 funnel_score 模块走新等级 · 调 scoring_levels 模块走旧等级
# v2 客户视角报告统一改用 funnel_score · v1 老逻辑保留
from tools.scoring.funnel_score import (  # noqa: E402
    FUNNEL_LEVEL_META,
    calculate_funnel_score,
    get_funnel_meta,
    render_funnel_progress_bar,
)

__all__ = [
    # v1 SSOT(旧)
    "LEVEL_META", "get_level", "get_meta", "get_summary", "list_all_levels", "build_cover_conclusion",
    # v2 漏斗 SSOT(新 · 客户视角报告用)
    "FUNNEL_LEVEL_META", "calculate_funnel_score", "get_funnel_meta", "render_funnel_progress_bar",
]
