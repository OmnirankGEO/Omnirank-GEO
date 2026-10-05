"""
历史 authority_ranking 模板兼容入口。

该历史代码由现役多品牌比较模板承接。排名、推荐和榜单问题可以直接回答，
但不得模拟研究机构、制造无依据名次、固定客户排位或虚构竞品。
"""

from .canonical_family_templates import prompt_for_style

AUTHORITY_RANKING_META = {
    "name": "选购与多品牌比较（历史兼容）",
    "code": "authority_ranking",
    "suitable_platforms": [],
    "word_count": {"min": 2500, "max": 5000, "optimal": 3500},
    "target_ai_engines": [],
    "version": "safe-compat-1",
    "status": "legacy_disabled",
}

AUTHORITY_RANKING_PROMPT = prompt_for_style("comparison_review")
