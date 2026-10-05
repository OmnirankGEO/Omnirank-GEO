"""[T2] 文章结构分层采样:adopted 占满不再挤出对照组(cited/search/reference)。"""
from services.article_structure_analysis import _stratified_sample, _group_key


def _row(tier: str, i: int) -> dict:
    return {
        "id": i,
        "is_adopted": 1 if tier == "adopted" else 0,
        "is_cited": 1 if tier == "cited" else 0,
        "is_search_only": 1 if tier == "search" else 0,
        "source_weight": 1.0,
        "cleaned_char_count": 800,
    }


def test_adopted_flood_does_not_squeeze_out_control_groups():
    # 300 采纳 + 各对照组充足 → limit=100 时四组都应有样本(原 ORDER BY is_adopted DESC LIMIT 会让对照组=0)
    pool = (
        [_row("adopted", i) for i in range(300)]
        + [_row("cited", 1000 + i) for i in range(50)]
        + [_row("search", 2000 + i) for i in range(50)]
        + [_row("reference", 3000 + i) for i in range(20)]
    )
    out = _stratified_sample(pool, limit=100)
    groups = {}
    for r in out:
        groups[_group_key(r)] = groups.get(_group_key(r), 0) + 1
    assert groups.get("adopted_group", 0) > 0
    assert groups.get("cited_group", 0) > 0, "对照组 cited 不应被采纳挤空"
    assert groups.get("search_only_control_group", 0) > 0, "对照组 search 不应被挤空"
    assert groups.get("reference_group", 0) > 0
    # 采纳占 ~40% 配额,不吃满全部 100
    assert groups["adopted_group"] <= 45
    assert len(out) <= 100


def test_spillover_when_group_short():
    # 采纳组只有 5 条(不足 40 配额)→ 余额溢出给其它组,总数仍尽量填满
    pool = (
        [_row("adopted", i) for i in range(5)]
        + [_row("cited", 1000 + i) for i in range(100)]
        + [_row("search", 2000 + i) for i in range(100)]
    )
    out = _stratified_sample(pool, limit=100)
    groups = {}
    for r in out:
        groups[_group_key(r)] = groups.get(_group_key(r), 0) + 1
    assert groups.get("adopted_group", 0) == 5  # 全取(不足配额)
    # 溢出:cited+search 补足,总数接近 limit
    assert len(out) >= 95


def test_small_pool_returns_all():
    pool = [_row("adopted", 1), _row("cited", 2), _row("search", 3)]
    out = _stratified_sample(pool, limit=300)
    assert len(out) == 3
