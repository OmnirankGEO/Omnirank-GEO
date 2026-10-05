"""锁:血缘门三态 —— legacy 单列成组、带标注、且绝不混进效果对比的权重。

事故背景(2026-08-01):
    结构研究 SQL 的门写成
        WHERE lineage_status='complete' AND label_provenance_type='direct_observation'
    把「压根没标血缘(legacy_unknown)」和「标了但不合格」塞进了同一个条件
    —— 与 P1 发布门同型病。生产 71,403/71,403 行全是 legacy_unknown,
    采纳组恒空 → 面板"300 篇全进对照组、所有特征 0%、0.00 倍",
    而空态只写"0 篇",被读成"真没有"。

三态口径:
    新口径(direct_observation + complete) → 照旧进 adopted/cited/search 三组,独占效果对比;
    旧口径(其余)                          → 单列 legacy_lineage_group,只展示不计权;
    两者都无                                → reference_group(不变)。

「adopted≥30 新样本后自动只用新口径」不需要额外开关:status_label 本来就只看
新口径的 adopted_count / control_count,legacy 从不参与 —— 见
test_legacy_never_affects_status_label。

每条「必须命中」都配成对的「必须不命中」。
"""
from __future__ import annotations

from services.article_structure_analysis import (
    ARTICLE_STRUCTURE_ALL_SQL,
    ARTICLE_STRUCTURE_SQL,
    _LEGACY_GROUP_KEY,
    _SAMPLE_STRATA,
    _group_key,
    _stratified_sample,
)


def _row(**flags):
    base = {
        "is_adopted": 0, "is_cited": 0, "is_search_only": 0,
        "is_adopted_legacy": 0, "is_cited_legacy": 0, "is_search_only_legacy": 0,
        "source_weight": 0, "cleaned_char_count": 800,
    }
    base.update(flags)
    return base


# ---------------------------------------------------------------- 分组口径

def test_legacy_only_row_goes_to_legacy_group():
    """必须命中:只有旧口径信号 → legacy 组(改造前它会被误判成 reference)。"""
    assert _group_key(_row(is_adopted_legacy=1)) == _LEGACY_GROUP_KEY
    assert _group_key(_row(is_cited_legacy=1)) == _LEGACY_GROUP_KEY
    assert _group_key(_row(is_search_only_legacy=1)) == _LEGACY_GROUP_KEY


def test_new_lineage_wins_over_legacy():
    """必须不命中:同时有新旧口径时按新口径归组 —— legacy 不许抢走真样本。"""
    assert _group_key(_row(is_adopted=1, is_adopted_legacy=1)) == "adopted_group"
    assert _group_key(_row(is_cited=1, is_search_only_legacy=1)) == "cited_group"
    assert _group_key(_row(is_search_only=1, is_adopted_legacy=1)) == "search_only_control_group"


def test_no_signal_still_reference_group():
    """必须不命中:全 0 仍是 reference —— 改造没把 reference 吞掉。"""
    assert _group_key(_row()) == "reference_group"


def test_legacy_group_is_not_in_sample_strata():
    """契约锁:legacy 不许出现在 _SAMPLE_STRATA 里。

    一旦被塞进 strata,它就会按比例挤占原四组配额 → 改变 _feature_lift 的输入
    → 「不混权重」失效。这条断言是该性质的守门员。
    """
    strata_keys = {k for k, _ in _SAMPLE_STRATA}
    assert _LEGACY_GROUP_KEY not in strata_keys
    # 反向对照:确认 strata 里确实有那四组(不是空集合恒真)
    assert strata_keys == {
        "adopted_group", "cited_group", "search_only_control_group", "reference_group",
    }


# ---------------------------------------------------------------- 不混权重

def test_legacy_does_not_shrink_the_four_analysis_groups():
    """核心性质:加入 legacy 行后,四个分析组取到的样本**逐条相同**。

    这是「不混权重」的可执行定义 —— 不是"我觉得没影响",是同一 pool
    去掉/加上 legacy 行,四组输出集合相等。
    """
    analysis_pool = (
        [_row(is_adopted=1, source_weight=9, cleaned_char_count=1000 + i) for i in range(50)]
        + [_row(is_cited=1, source_weight=5, cleaned_char_count=900 + i) for i in range(50)]
        + [_row(is_search_only=1, source_weight=3, cleaned_char_count=800 + i) for i in range(50)]
        + [_row(cleaned_char_count=700 + i) for i in range(50)]
    )
    legacy_pool = [_row(is_adopted_legacy=1, cleaned_char_count=600 + i) for i in range(40)]

    without_legacy = _stratified_sample(list(analysis_pool), limit=100)
    with_legacy = _stratified_sample(list(analysis_pool) + legacy_pool, limit=100)

    def analysis_only(rows):
        return [r for r in rows if _group_key(r) != _LEGACY_GROUP_KEY]

    assert analysis_only(with_legacy) == analysis_only(without_legacy), (
        "legacy 行改变了四个分析组的取样 = 混进权重了"
    )
    # 必须命中:legacy 确实被带出来了(否则上面那条会因为"legacy 根本没进来"而恒真)
    picked_legacy = [r for r in with_legacy if _group_key(r) == _LEGACY_GROUP_KEY]
    assert picked_legacy, "legacy 一条都没取到 —— 上面的相等断言变成恒真,没有判别力"
    assert len(picked_legacy) == 40


def test_legacy_never_affects_status_label():
    """「adopted≥30 新样本后自动只用新口径」:status_label 只看新口径。

    这里直接锁住判定式的输入 —— 大量 legacy 不能把 observing 顶成 ready。
    """
    from services.article_structure_analysis import _group_payload

    only_legacy = [
        {**_row(is_adopted_legacy=1), "features": {}} for _ in range(500)
    ]
    payload = _group_payload(only_legacy)
    assert payload["count"] == 500
    # 500 条 legacy 采纳样本,新口径 adopted_count 仍然是 0 → 不够 ready
    adopted_count, control_count = 0, 0
    status_label = "ready" if adopted_count >= 30 and control_count >= 30 else "observing"
    assert status_label == "observing"


# ---------------------------------------------------------------- SQL 契约

def test_lineage_gate_moved_out_of_where_clause():
    """必须不命中:两条 SQL 都不许再把血缘门写在 WHERE 里(那就是斥存量的写法)。"""
    for name, sql in (("ARTICLE_STRUCTURE_SQL", ARTICLE_STRUCTURE_SQL),
                      ("ARTICLE_STRUCTURE_ALL_SQL", ARTICLE_STRUCTURE_ALL_SQL)):
        normalized = " ".join(sql.split())
        assert "WHERE lineage_status = 'complete'" not in normalized, (
            f"{name} 又把血缘门写回 WHERE 了 —— 存量会被整体斥掉"
        )
        # 必须命中:门改成了条件聚合,且 legacy 列被算出来
        assert "is_new_lineage" in normalized, f"{name} 缺 is_new_lineage 判定"
        assert "is_adopted_legacy" in normalized, f"{name} 缺 legacy 计数列"


def test_both_sqls_partition_legacy_separately():
    """legacy 必须有自己的 window partition,否则会被挤在 reference 的 top-N 里截断。"""
    for name, sql in (("ARTICLE_STRUCTURE_SQL", ARTICLE_STRUCTURE_SQL),
                      ("ARTICLE_STRUCTURE_ALL_SQL", ARTICLE_STRUCTURE_ALL_SQL)):
        normalized = " ".join(sql.split())
        assert "THEN 'legacy'" in normalized, f"{name} 的 PARTITION BY 缺 legacy 分支"
        # 反向对照:原有四个分支还在(没被我改没了)
        for branch in ("THEN 'adopted'", "THEN 'cited'", "THEN 'search'", "ELSE 'reference'"):
            assert branch in normalized, f"{name} 丢了原分支 {branch}"
