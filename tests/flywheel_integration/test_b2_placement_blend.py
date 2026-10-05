"""[B2] 回归:
- B2-2 flywheel_score_blend 已外科退役(2026-07-03 收口):符号删除 + _quality_score_v2f 无 blend 参数 + 开关消失。
- B2-1 build_module_industry_landscape 有数据出段/无数据隐藏(保留,非 blend)。
"""
import inspect

import pytest  # noqa: F401  (B2-1 用例可能用到)

from services import placement_service as ps


# ---------- B2-2 blend 退役回归 ----------
def test_blend_symbols_removed():
    assert not hasattr(ps, "_resolve_flywheel_blend")
    assert not hasattr(ps, "_prefetch_shadow_scores")
    assert not hasattr(ps, "_recommend_for_publish_blended")


def test_quality_score_v2f_no_blend_param_and_ignores_shadow():
    assert "shadow_blend_weight" not in inspect.signature(ps._quality_score_v2f).parameters
    row = {"price": 45, "authority_media": 1, "geo_rank": 2,
           "portal_media": "其他门户", "geo_rank_platform": "豆包,Kimi,DeepSeek"}
    s = ps._quality_score_v2f(row)
    assert 0.0 <= s <= 1.0
    # 即便 row 带 shadow_score,也不再影响打分(blend 已删)
    assert ps._quality_score_v2f(dict(row, shadow_score=99)) == ps._quality_score_v2f(row)


def test_flywheel_score_blend_switch_removed():
    from writing.feature_switches import SWITCH_SPECS
    assert "flywheel_score_blend" not in SWITCH_SPECS


# ---------- B2-1 报告段 ----------
def _seed_engine_stats(conn, rows):
    c = conn.cursor()
    for (industry, engine, platform, rate) in rows:
        c.execute(
            """INSERT INTO geo_engine_stats
                   (industry, engine, platform, citation_count, total_queries, citation_rate, last_updated)
               VALUES (%s,%s,%s,%s,%s,%s, NOW())
               ON CONFLICT (industry, engine, platform) DO UPDATE SET citation_rate = EXCLUDED.citation_rate""",
            (industry, engine, platform, 10, 20, rate),
        )
    conn.commit()


def test_industry_landscape_section_has_data(db_with_clean_research):
    from services.report_writer_v2 import build_module_industry_landscape
    conn = db_with_clean_research
    _seed_engine_stats(conn, [
        ("测试行业", "豆包", "知乎", 0.6),
        ("测试行业", "Kimi", "知乎", 0.3),
        ("测试行业", "DeepSeek", "CSDN", 0.1),
    ])
    mod = build_module_industry_landscape({"industry": "测试行业"})
    md = mod["rendered_md"]
    assert "你所在行业的 AI 引用格局" in md
    assert "知乎" in md
    # 人话活跃度带,禁裸露百分比
    assert "高频引用" in md
    assert "%" not in md  # 不露原始 SOV 百分比


def test_industry_landscape_no_data_hidden(db_with_clean_research):
    from services.report_writer_v2 import build_module_industry_landscape
    # 空行业(无 geo_engine_stats 数据)→ 整段隐藏(rendered_md 为空)
    mod = build_module_industry_landscape({"industry": "不存在的行业_xyz"})
    assert mod["rendered_md"] == ""
    # 行业为空 → 同样隐藏
    mod2 = build_module_industry_landscape({"industry": ""})
    assert mod2["rendered_md"] == ""
