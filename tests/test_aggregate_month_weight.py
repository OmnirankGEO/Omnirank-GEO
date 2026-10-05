"""
test_aggregate_month_weight — 按月加权聚合算法单元测试

覆盖：
  - 默认指数衰减权重函数
  - 月题数低于门槛时整月跳过
  - 跨月加权聚合（老数据自然衰减）
  - 手动覆盖权重为 0 时该月被排除
  - 全局 decay_factor 改动后老月份权重相应变化
"""
from datetime import datetime, timedelta
import pytest

from services.placement_service import PlacementService


# ----------------------------------------------------------------
# helpers
# ----------------------------------------------------------------

def _seed_raw(conn, rows):
    """rows: list of (industry, engine, query, year_month, cited_platform)"""
    c = conn.cursor()
    for ind, eng, q, ym, plat in rows:
        c.execute(
            "INSERT INTO geo_research_raw "
            "(industry, engine, query, cited_platform, created_at) "
            "VALUES (%s, %s, %s, %s, %s::timestamp)",
            (ind, eng, q, plat, f"{ym}-15 12:00:00"),
        )
    conn.commit()


def _read_stats(conn, industry, engine):
    """返回 {platform: citation_rate}"""
    c = conn.cursor()
    c.execute(
        "SELECT platform, citation_rate FROM geo_engine_stats "
        "WHERE industry=%s AND engine=%s",
        (industry, engine),
    )
    return {r["platform"]: r["citation_rate"] for r in c.fetchall()}


# ----------------------------------------------------------------
# 1) 纯函数：默认衰减
# ----------------------------------------------------------------

def test_default_weight_decay_curve():
    svc = PlacementService()
    assert svc._default_weight(0, decay=0.3) == pytest.approx(1.0)
    assert svc._default_weight(1, decay=0.3) == pytest.approx(0.3)
    assert svc._default_weight(2, decay=0.3) == pytest.approx(0.09)
    assert svc._default_weight(3, decay=0.3) == pytest.approx(0.027, abs=0.0001)
    # 负数（未来日期）保护：返回 1.0
    assert svc._default_weight(-1, decay=0.3) == pytest.approx(1.0)


# ----------------------------------------------------------------
# 2) 月题数不足跳过
# ----------------------------------------------------------------

def test_month_with_too_few_queries_is_skipped(db_with_clean_research):
    """3 月只 5 题（< 10 门槛），4 月有 25 题。3 月数据应被跳过。"""
    now = datetime.now()
    cur_ym = now.strftime("%Y-%m")
    last_ym = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    rows = (
        [("装修建材", "豆包", f"q{i}", cur_ym, "知乎") for i in range(25)] +
        [("装修建材", "豆包", f"q{i}", last_ym, "房天下") for i in range(5)]   # 5 题 < 门槛
    )
    _seed_raw(db_with_clean_research, rows)
    PlacementService().aggregate_research_stats(["装修建材"])

    stats = _read_stats(db_with_clean_research, "装修建材", "豆包")
    assert stats.get("知乎") == pytest.approx(1.0, abs=0.001)
    # 房天下只在被跳过的月份出现 → 不应进结果
    assert "房天下" not in stats


# ----------------------------------------------------------------
# 3) 跨月加权（核心功能）
# ----------------------------------------------------------------

def test_old_month_decays_in_aggregation(db_with_clean_research):
    """
    场景：装修建材-豆包
      老月份（2 个月前）：25 题，房天下被全部 25 题引用
      当月：25 题，知乎被全部 25 题引用，房天下 0 次
    默认 decay=0.3：当月权重 1.0，2 个月前权重 0.09
      知乎     = 1.0 / (1.0 + 0.09) ≈ 0.917
      房天下   = 0.09 / (1.0 + 0.09) ≈ 0.083
    """
    now = datetime.now()
    cur_ym = now.strftime("%Y-%m")
    # 2 个月前（避免和"上月"门槛冲突）
    y, m = now.year, now.month - 2
    if m <= 0:
        y, m = y - 1, m + 12
    old_ym = f"{y:04d}-{m:02d}"

    rows = (
        [("装修建材", "豆包", f"q{i}", old_ym, "房天下") for i in range(25)] +
        [("装修建材", "豆包", f"q{i}", cur_ym, "知乎") for i in range(25)]
    )
    _seed_raw(db_with_clean_research, rows)
    PlacementService().aggregate_research_stats(["装修建材"])

    stats = _read_stats(db_with_clean_research, "装修建材", "豆包")
    assert stats["知乎"] == pytest.approx(1.0 / 1.09, abs=0.01)
    assert stats["房天下"] == pytest.approx(0.09 / 1.09, abs=0.01)


# ----------------------------------------------------------------
# 4) 手动覆盖权重 = 0 → 该月被排除
# ----------------------------------------------------------------

def test_manual_weight_override_zero_excludes_month(db_with_clean_research):
    now = datetime.now()
    cur_ym = now.strftime("%Y-%m")
    y, m = now.year, now.month - 2
    if m <= 0:
        y, m = y - 1, m + 12
    old_ym = f"{y:04d}-{m:02d}"

    rows = (
        [("装修建材", "豆包", f"q{i}", old_ym, "房天下") for i in range(25)] +
        [("装修建材", "豆包", f"q{i}", cur_ym, "知乎") for i in range(25)]
    )
    _seed_raw(db_with_clean_research, rows)

    # 手动把老月份权重设为 0
    c = db_with_clean_research.cursor()
    c.execute(
        "INSERT INTO geo_month_weights (industry, year_month, weight) VALUES (%s, %s, %s)",
        ("装修建材", old_ym, 0.0),
    )
    db_with_clean_research.commit()

    PlacementService().aggregate_research_stats(["装修建材"])
    stats = _read_stats(db_with_clean_research, "装修建材", "豆包")

    # 只有当月生效
    assert stats["知乎"] == pytest.approx(1.0, abs=0.001)
    assert "房天下" not in stats


# ----------------------------------------------------------------
# 5) 全局 decay_factor 调整
# ----------------------------------------------------------------

def test_disappearing_platform_no_stale_row(db_with_clean_research):
    """
    回归测试：当一个平台的所有月份都被排除（覆盖权重=0），
    它在 geo_engine_stats 中的旧记录应被清理，而不是残留。
    """
    now = datetime.now()
    cur_ym = now.strftime("%Y-%m")
    y, m = now.year, now.month - 2
    if m <= 0: y, m = y - 1, m + 12
    old_ym = f"{y:04d}-{m:02d}"

    rows = (
        [("装修建材", "豆包", f"q{i}", old_ym, "房天下") for i in range(25)] +
        [("装修建材", "豆包", f"q{i}", cur_ym, "知乎") for i in range(25)]
    )
    _seed_raw(db_with_clean_research, rows)

    # 第一次聚合：默认权重，房天下应该有 ~8.3% 的引用率
    PlacementService().aggregate_research_stats(["装修建材"])
    stats_v1 = _read_stats(db_with_clean_research, "装修建材", "豆包")
    assert "房天下" in stats_v1
    assert stats_v1["房天下"] == pytest.approx(0.083, abs=0.01)

    # 把老月份权重置 0 后重聚合：房天下应该完全消失（不残留旧 8.3%）
    c = db_with_clean_research.cursor()
    c.execute(
        "INSERT INTO geo_month_weights (industry, year_month, weight) VALUES (%s, %s, %s)",
        ("装修建材", old_ym, 0.0),
    )
    db_with_clean_research.commit()

    PlacementService().aggregate_research_stats(["装修建材"])
    stats_v2 = _read_stats(db_with_clean_research, "装修建材", "豆包")
    assert "知乎" in stats_v2 and stats_v2["知乎"] == pytest.approx(1.0, abs=0.001)
    assert "房天下" not in stats_v2, "权重置 0 后，房天下应在 geo_engine_stats 中消失，不应残留旧值"


def test_global_decay_factor_change(db_with_clean_research):
    """改全局 decay 0.3 → 0.5 后，2 个月前的权重从 0.09 升到 0.25"""
    now = datetime.now()
    cur_ym = now.strftime("%Y-%m")
    y, m = now.year, now.month - 2
    if m <= 0:
        y, m = y - 1, m + 12
    old_ym = f"{y:04d}-{m:02d}"

    rows = (
        [("装修建材", "豆包", f"q{i}", old_ym, "房天下") for i in range(25)] +
        [("装修建材", "豆包", f"q{i}", cur_ym, "知乎") for i in range(25)]
    )
    _seed_raw(db_with_clean_research, rows)

    c = db_with_clean_research.cursor()
    c.execute("UPDATE geo_aggregation_config SET value=%s WHERE key='decay_factor'", ("0.5",))
    db_with_clean_research.commit()

    PlacementService().aggregate_research_stats(["装修建材"])
    stats = _read_stats(db_with_clean_research, "装修建材", "豆包")

    # decay=0.5: 2 月前权重=0.25, 当月=1.0
    # 知乎     = 1.0 / 1.25 = 0.8
    # 房天下   = 0.25 / 1.25 = 0.2
    assert stats["知乎"] == pytest.approx(0.8, abs=0.01)
    assert stats["房天下"] == pytest.approx(0.2, abs=0.01)
