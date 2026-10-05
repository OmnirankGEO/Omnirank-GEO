"""[B1-1 + B1-2] 回归:aggregate_research_stats 归一 engine + 过滤空名平台哨兵。

真跑 PG(conftest 把 DATABASE_URL 指向 TEST_DATABASE_URL)。
断言:
  - geo_engine_stats.engine 只出现中文 canonical(小写别名被归一、跨拼写合并)
  - platform='' 空名哨兵行不进 geo_engine_stats
  - 同引擎跨拼写(doubao + 豆包)的题被合并计入同一行
"""
import pytest

import db.diagnosis_db  # noqa: F401  触发建表(geo_research_raw / geo_engine_stats / geo_aggregation_config)
from services.placement_service import PlacementService


def _seed_raw(conn, rows):
    c = conn.cursor()
    for (industry, engine, query, platform) in rows:
        c.execute(
            """INSERT INTO geo_research_raw
                   (industry, query, engine, cited_platform, cite_position, created_at)
               VALUES (%s, %s, %s, %s, %s, NOW())""",
            (industry, query, engine, platform, 3),
        )
    conn.commit()


def _read_stats(conn):
    c = conn.cursor()
    c.execute(
        "SELECT industry, engine, platform, citation_count FROM geo_engine_stats ORDER BY 1, 2, 3"
    )
    return [dict(r) for r in c.fetchall()]


def test_aggregate_normalizes_engine_and_filters_empty_platform(db_with_clean_research):
    conn = db_with_clean_research
    rows = []
    for i in range(25):
        rows.append(("GEO", "doubao", f"q{i}", "知乎"))   # 小写别名
    for i in range(25):
        rows.append(("GEO", "豆包", f"p{i}", "知乎"))      # 中文,应与 doubao 合并成 豆包
    for i in range(25):
        rows.append(("GEO", "kimi", f"s{i}", ""))          # 空名哨兵 → 不得进 geo_engine_stats
    _seed_raw(conn, rows)

    PlacementService().aggregate_research_stats(["GEO"])
    stats = _read_stats(conn)

    engines = {s["engine"] for s in stats}
    platforms = {s["platform"] for s in stats}

    # 归一:无小写别名残留
    assert "doubao" not in engines and "kimi" not in engines and "qwen" not in engines, \
        f"engine 未归一: {engines}"
    assert engines <= {"豆包", "Kimi", "DeepSeek", "千问"}, f"出现非 canonical engine: {engines}"

    # 空名平台哨兵被过滤
    assert "" not in platforms, f"空名平台未过滤: {platforms}"

    # 跨拼写合并:豆包/知乎 应合并 doubao(25) + 豆包(25) = 50 题
    zhihu = [s for s in stats if s["engine"] == "豆包" and s["platform"] == "知乎"]
    assert zhihu, f"豆包/知乎 行缺失: {stats}"
    assert int(zhihu[0]["citation_count"]) == 50, f"未合并跨拼写题数: {zhihu[0]}"
