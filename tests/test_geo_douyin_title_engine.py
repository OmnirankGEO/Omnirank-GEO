"""GEO 抖音图文管线 v1 · 标题引擎锁

锁的设计原则(踩过的教训):
  - 锁要打在【真机制】上,不是打在"最终都返回了字符串"这种恒真结论上;
  - 每条"必须命中"配一条"必须不命中",否则恒真实现也能满分。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from services.geo_douyin import config as dy_config
from services.geo_douyin.title_engine import (
    build_city_matrix,
    build_hashtags,
    build_title,
    title_form_isolation_marker,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
TITLE_ENGINE = ROOT / "services" / "geo_douyin" / "title_engine.py"


# ─────────────────────────────────────────────────────────────
# 1. 🔴 与 title_form 体系隔离(工单 §2.2 硬约束)
# ─────────────────────────────────────────────────────────────
def _imported_modules(path: pathlib.Path) -> set[str]:
    """用 AST 取真实 import,不用正则 —— 正则会把注释/字符串里的模块名算进去
    (踩过:整文件跑正则误报 18 处)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_title_engine_does_not_import_writing_title_modules():
    """抖音搜索面不得共用媒体文章面的 title_form 模板池。"""
    mods = _imported_modules(TITLE_ENGINE)
    forbidden = [
        m for m in mods
        if m.startswith("writing.")
        or m in {"writing"}
    ]
    assert not forbidden, (
        f"title_engine 不得 import writing.*(title_form 体系),实得 {forbidden}"
    )


def test_title_engine_has_its_own_pool_marker():
    """反向面:确实自成一套池,而不是空壳。"""
    assert title_form_isolation_marker() == "douyin_search_surface_pool_v1"


def test_title_engine_pool_is_not_empty_and_families_cover_evidence():
    """模板池必须真的有内容,且覆盖 §6 实证的三族。

    (这条是上一条的"必须不命中"面:一个空池也能通过 import 隔离检查。)
    """
    from services.geo_douyin.title_engine import _TEMPLATES
    assert len(_TEMPLATES) >= 5
    families = {t.family for t in _TEMPLATES}
    assert {"choice", "rank", "pitfall"} <= families


def test_choice_family_outweighs_rank_family():
    """§6 实证订正:选型问法 55% 远大于榜单 14% → 权重必须体现这个方向。

    若有人把榜单权重调到 >= 选型,这条转红(研究报告"压倒性是排行榜"的说法
    已被分形态统计证伪,不能照着做模板)。
    """
    from services.geo_douyin.title_engine import _TEMPLATES
    choice = sum(t.weight for t in _TEMPLATES if t.family == "choice")
    rank = sum(t.weight for t in _TEMPLATES if t.family == "rank")
    assert choice > rank * 2, f"选型问法权重({choice})必须显著高于榜单({rank})"


def test_highest_weight_template_is_choice_family():
    """更细的机制锁:**权重最高的那一条**必须是选型问法。

    只看族权重之和不够 —— 把主模板从 30 压到 1、让榜单当头名,族总和可能仍然
    "选型 > 榜单",总和锁照样绿(实测该变异存活)。主模板是谁才是真机制。
    """
    from services.geo_douyin.title_engine import _TEMPLATES
    top = max(_TEMPLATES, key=lambda t: t.weight)
    assert top.family == "choice", (
        f"权重最高的模板是 {top.family} 族({top.pattern}),"
        f"与 §6 实证(选型问法 55% / 榜单 14%)相悖")


def test_choice_family_dominates_actual_sampling():
    """行为面:真按 variant_index 采样,选型问法必须占多数、榜单必须是少数。"""
    import collections
    fams = collections.Counter(
        build_title("全屋定制", "深圳", variant_index=i).family
        for i in range(300)
    )
    total = sum(fams.values())
    assert fams["choice"] / total >= 0.6, f"选型问法占比过低: {fams}"
    assert fams["rank"] / total <= 0.25, f"榜单占比过高: {fams}"


# ─────────────────────────────────────────────────────────────
# 2. 🔴 城市变体必须错开(回归锁:这是真踩到过的 bug)
# ─────────────────────────────────────────────────────────────
def test_city_matrix_variants_use_distinct_sentence_patterns():
    """回归锁 · 曾经 5 个城市全出同一句式(只有城市名不同)。

    机制:加权池里权重 30 的模板独占 30 个连续槽位,按 i*7 步进落在同一模板内。
    平台会把"只换城市名的批量内容"判为重复铺量 → 这是功能缺陷不是美观问题。
    """
    cities = ["深圳", "广州", "杭州", "成都", "武汉"]
    variants = build_city_matrix("全屋定制", cities)
    assert len(variants) == len(cities)
    # 去掉城市名后,句式必须两两不同
    skeletons = {v.title.replace(v.city or "", "") for v in variants}
    assert len(skeletons) == len(variants), (
        f"{len(variants)} 个城市只出了 {len(skeletons)} 种句式,城市变体没错开:"
        f"{[v.title for v in variants]}"
    )


def test_city_matrix_dedupes_and_skips_blank_cities():
    variants = build_city_matrix("全屋定制", ["深圳", "深圳", "", "  ", "广州"])
    assert [v.city for v in variants] == ["深圳", "广州"]


def test_city_matrix_每个变体都带城市名():
    for v in build_city_matrix("全屋定制", ["深圳", "广州", "杭州"]):
        assert v.city and v.city in v.title


# ─────────────────────────────────────────────────────────────
# 3. 标题硬上限(SSOT 在发布侧,超限会被远端直接拒)
# ─────────────────────────────────────────────────────────────
def test_title_never_exceeds_publish_hard_limit():
    from services.geo_douyin.title_engine import _title_hard_limit
    limit = _title_hard_limit()
    long_kw = "超长关键词" * 12
    for idx in range(0, 40):
        t = build_title(long_kw, "深圳", variant_index=idx)
        assert len(t.title) <= limit, f"标题超硬上限 {len(t.title)}>{limit}: {t.title}"


def test_over_limit_title_is_marked_truncated():
    """必须不命中面:正常长度不能被误标 truncated。"""
    short = build_title("全屋定制", "深圳")
    assert short.truncated is False
    long_one = build_title("超长关键词" * 12, "深圳")
    assert long_one.truncated is True


def test_empty_keyword_rejected():
    with pytest.raises(ValueError):
        build_title("   ", "深圳")


# ─────────────────────────────────────────────────────────────
# 4. hashtag(§6 实证:众数 5 个)
# ─────────────────────────────────────────────────────────────
def test_hashtag_default_count_matches_evidence():
    tags = build_hashtags("全屋定制", "深圳")
    assert len(tags) == dy_config.HASHTAG_COUNT_DEFAULT == 5


def test_hashtags_are_unique_and_contain_city_and_bare_keyword():
    tags = build_hashtags("全屋定制", "深圳")
    assert len(tags) == len(set(tags)), f"hashtag 有重复: {tags}"
    assert any("深圳" in t for t in tags)


def test_hashtags_unique_in_the_case_that_actually_collides():
    """去重锁必须打在【真会撞】的场景上。

    带城市 + 默认 5 个时,两轮后缀循环还没走到重叠就够数了 —— 此时就算把去重
    删掉也测不出来(实测该变异存活)。**无城市 + 取满 7 个**才会让第二轮后缀
    重新推一遍已存在的标签,去重失效时立刻出现重复。
    """
    tags = build_hashtags("全屋定制", None, count=7)
    assert len(tags) == len(set(tags)), f"hashtag 去重失效,出现重复: {tags}"


def test_hashtag_count_clamped_into_evidence_range():
    assert len(build_hashtags("全屋定制", "深圳", count=99)) <= dy_config.HASHTAG_COUNT_MAX
    assert len(build_hashtags("全屋定制", "深圳", count=0)) >= dy_config.HASHTAG_COUNT_MIN


def test_hashtags_empty_for_blank_keyword():
    assert build_hashtags("   ", "深圳") == []


# ─────────────────────────────────────────────────────────────
# 5. 确定性(城市矩阵必须可复现,否则同内容两次生成对不上)
# ─────────────────────────────────────────────────────────────
def test_generation_is_deterministic():
    a = [v.title for v in build_city_matrix("全屋定制", ["深圳", "广州", "杭州"])]
    b = [v.title for v in build_city_matrix("全屋定制", ["深圳", "广州", "杭州"])]
    assert a == b


# ─────────────────────────────────────────────────────────────
# 6. 🔴 标题长度必须躲开最差档(§6c 实测)
# ─────────────────────────────────────────────────────────────
def test_titles_avoid_worst_length_band():
    """实测采纳率:16-25 字是**最差档 23.1%**,必须躲开。

    ≤15字 26.0% / 16-25字 23.1% / 26-40字 26.6% / 41-60字 27.9%
    / 61-100字 34.4% / >100字 35.6%
    发布平台标题硬上限 45 字 → 目标区间 26-45(顶到上限),剩余信息量由正文首句接力。
    """
    from services.geo_douyin.title_engine import TITLE_MIN_TARGET, _title_hard_limit
    limit = _title_hard_limit()
    bad = []
    for i in range(200):
        t = build_title("全屋定制", "深圳", variant_index=i)
        if not (TITLE_MIN_TARGET <= len(t.title) <= limit):
            bad.append((len(t.title), t.title))
    assert not bad, f"{len(bad)} 条标题落在最差档或超限,例:{bad[:3]}"


def test_city_matrix_titles_also_avoid_worst_band():
    from services.geo_douyin.title_engine import TITLE_MIN_TARGET
    for v in build_city_matrix("全屋定制", ["深圳", "广州", "杭州", "成都", "武汉"]):
        assert len(v.title) >= TITLE_MIN_TARGET, f"{len(v.title)}字 太短: {v.title}"


def test_title_min_target_is_above_worst_band():
    """反向面:下限本身必须真的高于最差档上沿(25),否则这条锁形同虚设。"""
    from services.geo_douyin.title_engine import TITLE_MIN_TARGET
    assert TITLE_MIN_TARGET > 25


def test_body_prompt_requires_relay_not_repeat():
    """正文首句要接力补信息,且禁止空转开场白。"""
    import pathlib as _p
    src = (_p.Path(__file__).resolve().parents[1]
           / "services" / "geo_douyin" / "content_generator.py").read_text(encoding="utf-8")
    # 🔴 每条要求各断各的,别用 or 串起来 ——
    #    实测:变异删掉"合起来 60-100 字"那句,而 or 的另一半还在,锁照样绿。
    assert "紧接标题" in src, "没要求正文首句紧接标题"
    assert "别重复标题" in src, "没禁止正文重复标题"
    assert "60-100" in src, "没给出「标题+正文首句」合起来的目标信息量(接力目标丢了)"
    assert "大家好" in src, "没有禁掉空转开场白"
