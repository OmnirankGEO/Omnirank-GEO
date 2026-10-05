"""AdminDashboard 成本口径 v1.2 · 老板 v1.1 复审 2 P1 修

P1-1 · publishing 卡时间窗错位
       v1.1:revenue 全量历史 · cost 本月外采 → profit = 历史收入 - 本月成本 无意义
       v1.2:6 SQL 全部 month_start 过滤 + UI 改"本月..."文案

P1-2 · 发布收入用 created_at 可能漏算
       v1.1:status='published' AND created_at >= month_start
            → 上月创建本月发布的 item 漏算
       v1.2:status='published' AND COALESCE(published_at, created_at) >= month_start
            → published_at NULL 历史数据兜底 created_at
"""
from __future__ import annotations
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# v1.2 P1-1 · publishing 全部本月口径
# ============================================================

def test_v12_p11_publishing_total_orders_month_filter():
    """v1.2 P1-1 · publishing.total_orders SQL 必须 WHERE created_at >= month_start"""
    src = _read("api/dashboard_api.py")
    # 找 publishing["total_orders"] 赋值附近的 SQL
    idx = src.find('publishing["total_orders"]')
    assert idx > 0
    # 前 500 字符内必须有 created_at >= %s 过滤
    region = src[max(0, idx - 600):idx]
    assert "FROM mhz_publish_order_items WHERE created_at >=" in region, \
        "v1.2 P1-1 破:publishing.total_orders SQL 仍全量(未加 month_start 过滤)"


def test_v12_p11_publishing_published_month_filter():
    """v1.2 P1-1 · publishing.published SQL 必须按 published_at 月口径过滤"""
    src = _read("api/dashboard_api.py")
    idx = src.find('publishing["published"]')
    assert idx > 0
    region = src[max(0, idx - 700):idx]
    # 必须含 status='published' + COALESCE(published_at, created_at) >= %s
    assert "status = 'published'" in region and "COALESCE(published_at, created_at)" in region, \
        "v1.2 P1-1 破:publishing.published 未用 status='published' + COALESCE(published_at, created_at) 月口径"


def test_v12_p11_publishing_rejected_month_filter():
    """v1.2 P1-1 · publishing.rejected SQL 必须 WHERE status='rejected' AND created_at >= month_start"""
    src = _read("api/dashboard_api.py")
    idx = src.find('publishing["rejected"]')
    assert idx > 0
    region = src[max(0, idx - 600):idx]
    assert "status = 'rejected'" in region and "created_at >=" in region, \
        "v1.2 P1-1 破:publishing.rejected 未加 month_start 过滤"


def test_v12_p11_publishing_revenue_aligned_with_finance():
    """v1.2 P1-1 · publishing.revenue_yuan 必须 = month_publish_revenue(跟 finance.publish_revenue_yuan 同源)"""
    src = _read("api/dashboard_api.py")
    # 不再 SUM(cost_yuan) WHERE status='published' 全量 SQL
    bad_pat = re.compile(
        r'publishing\["revenue_yuan"\]\s*=\s*round\(float\(cur\.execute.*WHERE status\s*=\s*\'published\'\)',
        re.DOTALL,
    )
    assert not bad_pat.search(src), \
        "v1.2 P1-1 破:publishing.revenue_yuan 仍直接 SQL 全量(未复用 month_publish_revenue · 时间窗会再次错位)"
    # v1.3:publishing 走 _compute_publish_card(publish_revenue_yuan=month_publish_revenue)· 同源
    assert "publish_revenue_yuan=month_publish_revenue" in src, \
        "v1.2 P1-1/v1.3 破:publishing 收入未复用 month_publish_revenue · 跟 finance 脱钩"
    assert 'publishing["revenue_yuan"] = _pc["revenue_yuan"]' in src, \
        "v1.2 P1-1/v1.3 破:publishing.revenue_yuan 未从 _compute_publish_card 取"


def test_v12_p11_publishing_marks_period():
    """v1.2 P1-1 · publishing 字段显式标 period='month' + period_start(防 UI / 调用方误读)"""
    src = _read("api/dashboard_api.py")
    pub_init_idx = src.find("publishing = {")
    assert pub_init_idx > 0
    region = src[pub_init_idx:pub_init_idx + 1000]
    assert '"period"' in region, \
        "v1.2 P1-1 破:publishing 未显式标 period(month vs all-time)"
    assert '"period_start"' in region, \
        "v1.2 P1-1 破:publishing 未显式标 period_start(无法验证时间窗起点)"


def test_v12_p11_publishing_no_full_history_left_over():
    """v1.2 P1-1 整体 · publishing 段不能再有不带 WHERE 的全量 SELECT"""
    src = _read("api/dashboard_api.py")
    # 抽 publishing 段(从 "代发业务" 注释到 "推荐排行" 注释)
    pub_start = src.find("# ========== 代发业务")
    pub_end = src.find("# ========== 推荐排行", pub_start)
    if pub_end < 0:
        pub_end = pub_start + 5000
    pub_block = src[pub_start:pub_end]

    # 全量 SELECT 模式:SELECT ... FROM mhz_publish_order_items 后面不能完全没 WHERE
    # 但 media_count 表(mhz_media)允许全量(媒体库统计 · 与时间无关)
    bad_pat = re.compile(
        r'cur\.execute\(\s*"SELECT [^"]*?FROM mhz_publish_order_items"\s*\)',
    )
    leftover = bad_pat.findall(pub_block)
    assert not leftover, \
        f"v1.2 P1-1 破:publishing 段仍有不带 WHERE 的全量 SQL: {leftover}"


# ============================================================
# v1.2 P1-2 · 发布收入用 COALESCE(published_at, created_at)
# ============================================================

def test_v12_p12_finance_publish_revenue_uses_published_at_fallback():
    """v1.2 P1-2 → v1.4 · 发布收入 SQL 必须 COALESCE(published_at, created_at)(在 _query_publish_numbers 查询A)"""
    src = _read("api/dashboard_api.py")
    fn_start = src.find("def _query_publish_numbers(")
    assert fn_start > 0
    fn = src[fn_start:fn_start + 2000]
    # 已发布查询必须 COALESCE(published_at, created_at)(上月创建本月发布不漏算)
    assert "COALESCE(published_at, created_at)" in fn, \
        "v1.2 P1-2 破:发布收入未用 COALESCE(published_at, created_at) 兜底(上月创建本月发布漏算)"


def test_v12_p12_no_pure_created_at_only():
    """v1.2 P1-2 · 不能再用纯 created_at >= month_start 算发布收入(漏跨月)"""
    src = _read("api/dashboard_api.py")
    # 抽 month_publish_revenue 段 ~ 50 行
    idx = src.find("month_publish_revenue = 0")
    if idx < 0:
        # 兼容不同初始化模式 · 找 cur.execute 含 status='published' 的发布收入 SQL
        idx = src.find("# 发布收入")
    if idx < 0:
        return  # 找不到 · skip
    region = src[idx:idx + 1500]
    # 该区段不能只用 created_at(漏 published_at)
    # 精确正则:只能匹配 "AND created_at >= %s" 不伴随 COALESCE(published_at, ...)
    only_created_pat = re.compile(
        r"status\s*=\s*'published'\s*AND\s*created_at\s*>=\s*%s",
        re.IGNORECASE,
    )
    if only_created_pat.search(region):
        # 还要确认附近没有 COALESCE 替代
        assert "COALESCE(published_at" in region, \
            "v1.2 P1-2 破:发布收入 SQL 仍用纯 created_at 过滤(没 COALESCE published_at 兜底)"


# ============================================================
# v1.2 UI 文案 · "本月..." 前缀全覆盖
# ============================================================

def test_v12_ui_card_header_renamed_本月代发业务():
    """v1.2 · UI 发布卡 header 改"本月代发业务"(明示时间窗)"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert "本月代发业务" in src, \
        "v1.2 破:UI 卡 header 仍叫「代发业务」未明示「本月」"


def test_v12_ui_subtitles_marked_本月():
    """v1.2 · UI 发布卡 6 子项全标"本月..."(本月新订单 / 已发布 / 已拒稿 / 发布收入 / 外采占用 / 发布毛利)"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    for label in (
        "本月新订单",
        "本月已发布",
        "本月已拒稿",
        "本月发布收入",
        "本月已发布外采",  # v1.4:已发布子集成本(跟顶部全状态总外采区分)
        "本月发布毛利",
    ):
        assert label in src, \
            f"v1.2 破:UI 发布卡缺「{label}」文案(防岁月静好误读历史)"


# ============================================================
# v1.2 整体 · 时间窗一致性硬约束
# ============================================================

def test_v12_publishing_revenue_cost_profit_same_period():
    """v1.2 整体 · revenue 用 month_publish_revenue · cost 用 month_publish_external_cost · profit 二者相减
    三者必须同时间窗(month_start)· 老板 P1-1 核心
    """
    src = _read("api/dashboard_api.py")
    pub_start = src.find("# ========== 代发业务")
    pub_end = src.find("# ========== 推荐排行", pub_start)
    if pub_end < 0:
        pub_end = pub_start + 5000
    pub_block = src[pub_start:pub_end]

    # v1.4:发布卡走 _compute_publish_card(收入=month_publish_revenue · 成本=month_publish_margin_cost 已发布子集)
    assert "publish_revenue_yuan=month_publish_revenue" in pub_block, \
        "v1.2 整体/v1.4 破:publish card 收入未用 month_publish_revenue"
    assert "publish_margin_cost_yuan=month_publish_margin_cost" in pub_block, \
        "v1.2 整体/v1.4 破:publish card 成本未用 month_publish_margin_cost(已发布子集)"
    assert 'publishing["revenue_yuan"] = _pc["revenue_yuan"]' in pub_block, \
        "v1.2 整体/v1.4 破:publishing.revenue_yuan 未从 _pc 取"
    assert 'publishing["cost_yuan"] = _pc["cost_yuan"]' in pub_block, \
        "v1.2 整体/v1.4 破:publishing.cost_yuan 未从 _pc 取"
    assert 'publishing["profit_yuan"] = _pc["profit_yuan"]' in pub_block, \
        "v1.2 整体/v1.4 破:publishing.profit_yuan 未从 _pc 取"
    # 真执行验证毛利 = revenue - cost
    from api.dashboard_api import _compute_publish_card
    _pc = _compute_publish_card(publish_revenue_yuan=150.0, publish_margin_cost_yuan=100.0)
    assert _pc["profit_yuan"] == 50.0
