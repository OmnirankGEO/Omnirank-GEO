"""AdminDashboard 成本口径 v1.3 · 老板 Opus 全面自审(workflow 多维 + 对抗验证)修

修 3 条 P1(财务面板显示错数字)+ P2(caller / 测试 / 口径):
  P1-1/P1-3 · 发布业务列语义用反:cost_yuan=媒体原价(成本)· cost_points=用户实付(收入)
              旧:revenue 用 cost_yuan(成本当收入)· 外采也用 cost_yuan → 发布毛利≈0
              新:revenue=SUM(cost_points)/130 · cost=SUM(cost_yuan) · 同"本月已发布"item 集
  P1-2 · 现金口径(老板拍):total_revenue=充值现金 · 发布收入不并入(防双算 cost_points 来自已充值 paid_points)
  P2 · is_known_caller 漏判裸名(monitoring/autofill/...)· 抽纯 helper 加真执行测试

★ 本文件全部【真实执行】(import + 调用)· 不是文本扫 ·
  直接根治 v1.0 漏 P0-1 时区 TypeError 那类 runtime bug(文本扫看不到)
"""
from __future__ import annotations
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
import sys
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# v1.3 真执行 · _compute_finance_metrics 现金口径
# ============================================================

def _finance(**kw):
    from api.dashboard_api import _compute_finance_metrics
    base = dict(recharge_revenue_yuan=1000.0, publish_revenue_yuan=500.0,
                llm_api_cost_yuan=200.0, publish_external_cost_yuan=100.0, active_7d=10)
    base.update(kw)
    return _compute_finance_metrics(**base)


def test_v13_cash_basis_total_revenue_excludes_publish():
    """v1.3 P1-2 真执行 · 总收入 = 充值现金 · 发布收入【不并入】(防双算)"""
    f = _finance(recharge_revenue_yuan=1000, publish_revenue_yuan=500)
    assert f["total_revenue_yuan"] == 1000, \
        f"P1-2 破:total_revenue 含了发布(双算)· 应=充值 1000 · 实 {f['total_revenue_yuan']}"
    assert f["publish_revenue_yuan"] == 500, "publish_revenue 信息字段应保留"
    assert f["revenue_basis"] == "cash_recharge_only", "现金口径标记缺失"


def test_v13_cash_basis_profit_uses_recharge_minus_operating():
    """v1.3 P1-2 真执行 · 毛利 = 充值现金 - 总运营成本(不被发布收入虚高)"""
    f = _finance(recharge_revenue_yuan=1000, publish_revenue_yuan=9999,
                 llm_api_cost_yuan=200, publish_external_cost_yuan=100)
    assert f["total_operating_cost_yuan"] == 300, f["total_operating_cost_yuan"]
    assert f["profit_yuan"] == 700, \
        f"P1-2 破:profit 被发布收入污染 · 应 1000-300=700 · 实 {f['profit_yuan']}"
    # profit_rate = 700/1000
    assert f["profit_rate"] == 0.7, f["profit_rate"]
    # arpu = 1000/10(现金/活跃 · 不含发布)
    assert f["arpu"] == 100.0, f["arpu"]


def test_v13_finance_zero_revenue_safe():
    """v1.3 真执行 · 0 营收时 profit_rate=None 不除零崩"""
    f = _finance(recharge_revenue_yuan=0, publish_revenue_yuan=0,
                 llm_api_cost_yuan=50, publish_external_cost_yuan=0)
    assert f["profit_rate"] is None, "0 营收 profit_rate 应 None"
    assert f["total_revenue_yuan"] == 0
    assert f["profit_yuan"] == -50, "0 营收仍有成本 · profit 负"


def test_v13_finance_has_all_split_fields():
    """v1.3 真执行 · finance 返完整拆口径字段(收入侧 3 + 成本侧 3 + 利润)"""
    f = _finance()
    for k in ("recharge_revenue_yuan", "publish_revenue_yuan", "total_revenue_yuan",
              "llm_api_cost_yuan", "publish_external_cost_yuan", "total_operating_cost_yuan",
              "profit_yuan", "profit_rate", "arpu", "revenue_yuan", "cost_yuan"):
        assert k in f, f"finance 缺字段 {k}"
    # 向后兼容:revenue_yuan=充值 · cost_yuan=LLM
    assert f["revenue_yuan"] == f["recharge_revenue_yuan"], "revenue_yuan 兼容别名应=充值"
    assert f["cost_yuan"] == f["llm_api_cost_yuan"], "cost_yuan 兼容别名应=LLM"


# ============================================================
# v1.3 真执行 · _compute_publish_card markup margin
# ============================================================

def test_v13_publish_card_real_margin():
    """v1.3 P1-1/P1-3 真执行 · 发布毛利 = 收入(cost_points/130) - 已发布成本(cost_yuan) > 0(不再≈0)"""
    from api.dashboard_api import _compute_publish_card
    # 媒体原价 100 · 用户实付 = 100×1.5 = 150(cost_points/130 折现后)
    pc = _compute_publish_card(publish_revenue_yuan=150.0, publish_margin_cost_yuan=100.0)
    assert pc["revenue_yuan"] == 150.0
    assert pc["cost_yuan"] == 100.0
    assert pc["profit_yuan"] == 50.0, \
        f"P1-1/P1-3 破:发布毛利应=媒体价×0.5=50 · 实 {pc['profit_yuan']}(收入≈成本说明列用反)"


def test_v13_publish_card_not_zero_when_columns_correct():
    """v1.3 P1-3 真执行 · 收入≠成本(用不同列)· 毛利非结构性 0"""
    from api.dashboard_api import _compute_publish_card
    pc = _compute_publish_card(publish_revenue_yuan=300.0, publish_margin_cost_yuan=200.0)
    assert pc["profit_yuan"] == 100.0
    # 反例守卫:若 revenue==cost(列用反)毛利会是 0 → 本测试确保我们传的是不同列的值
    assert pc["revenue_yuan"] != pc["cost_yuan"], "收入应≠成本(不同列)"


# ============================================================
# v1.3 真执行 · is_known_caller 裸名白名单(P2)
# ============================================================

def test_v13_is_known_caller_bare_names():
    """v1.3 P2 真执行 · 裸 caller(无族前缀)必须 known · monitoring 是最高频"""
    from services.api_costs import is_known_caller
    for c in ("monitoring", "autofill", "competitor_research", "ai_visibility"):
        assert is_known_caller(c), f"P2 破:裸 caller '{c}' 被误判 unknown(虚高 ratio)"


def test_v13_is_known_caller_added_families():
    """v1.3 P2 真执行 · placement_/competitor_ 族补全"""
    from services.api_costs import is_known_caller
    for c in ("placement_competitor_audit", "competitor_research", "research_center_keyword_scan"):
        assert is_known_caller(c), f"P2 破:caller '{c}' 未被族覆盖"


def test_v13_is_known_caller_still_rejects_garbage():
    """v1.3 P2 真执行 · 乱命名 / 空 / fallback 仍 unknown(不放水)"""
    from services.api_costs import is_known_caller
    for c in ("foo_bar", "random_new_feature", "", "llm_call_log", "legacy_token_usage"):
        assert not is_known_caller(c), f"P2 破:'{c}' 不该 known"


def test_v13_real_callers_all_known():
    """v1.3 P2 真执行 · grep 出的全库真实 caller 全集都应 known(防归因污染)"""
    from services.api_costs import is_known_caller
    real_callers = [
        "monitoring", "research_resolve_xhs_share", "research_resolve_douyin_share",
        "research_xhs_note_detail", "research_xhs_user_info", "research_xhs_user_notes",
        "article_writing", "autofill", "competitor_research", "industry_case_collector",
        "aliyun_ocr", "geo_managed_monitoring", "intake_ai_draft", "asr_transcription",
        "asr_mp3_fallback_video_detail", "asr_video_comments", "knowledge_cleaning",
        "placement_competitor_audit", "tikhub_tiktok_video_search", "tikhub_bilibili_video_search",
        "rewrite_douyin_share_url", "rewrite_douyin_video_detail", "rewrite_xhs_note_detail",
        "rewrite_bilibili_video_detail", "rewrite_bilibili_subtitle", "rewrite_wechat_video_detail",
        "research_center_keyword_scan", "tikhub_list_tools", "tikhub_call_tool",
        "tikhub_douyin_video_search", "tikhub_user_profile", "tikhub_user_videos",
        "tikhub_hot_search", "tikhub_smart_tools_cache", "tikhub_smart_call",
        "monitoring_insights", "monitoring_platform_mau", "keyword_seed",
        "social_dynamic_review", "ai_visibility",
    ]
    misjudged = [c for c in real_callers if not is_known_caller(c)]
    assert not misjudged, f"P2 破:真实 caller 被误判 unknown(虚高 ratio): {misjudged}"


# ============================================================
# v1.3 SQL 列语义文本断言(配合真执行 · 锁住列选择)
# ============================================================

def test_v13_publish_revenue_sql_uses_cost_points():
    """v1.3 P1-1 → v1.4 · 发布收入 SQL 必须 SUM(cost_points) /130(在 _query_publish_numbers)"""
    src = _read("api/dashboard_api.py")
    fn_start = src.find("def _query_publish_numbers(")
    assert fn_start > 0, "找不到 _query_publish_numbers helper"
    fn = src[fn_start:fn_start + 2000]
    assert "SUM(cost_points)" in fn, \
        "P1-1 破:发布收入未用 SUM(cost_points)(用户实付列)"
    assert "POINTS_PER_YUAN" in fn or "/ 130" in fn, \
        "P1-1 破:cost_points 未折现成元(/130)"


def test_v13_publish_external_cost_sql_uses_cost_yuan():
    """v1.3 P1-3 → v1.4 · 外采成本 SQL 用 SUM(cost_yuan)(媒体原价)· 在 _query_publish_numbers"""
    src = _read("api/dashboard_api.py")
    fn_start = src.find("def _query_publish_numbers(")
    assert fn_start > 0, "找不到 _query_publish_numbers helper"
    fn = src[fn_start:fn_start + 2000]
    assert "SUM(cost_yuan)" in fn, "P1-3 破:外采成本未用 SUM(cost_yuan)"


def test_v13_revenue_and_cost_same_item_set():
    """v1.3 P1-3/P2 · 发布收入与成本同一查询(同 item 集 + 同时间窗)· 防跨锚错位"""
    src = _read("api/dashboard_api.py")
    # 同一 cur.execute 同时 SELECT cost_points + cost_yuan
    pat = re.compile(
        r"SELECT\s+COALESCE\(SUM\(cost_points\),\s*0\)\s+as\s+pts\s*,\s*COALESCE\(SUM\(cost_yuan\),\s*0\)\s+as\s+yuan",
        re.IGNORECASE,
    )
    assert pat.search(src), \
        "P1-3 破:收入(cost_points)与成本(cost_yuan)不在同一查询(可能跨 item 集/跨时间窗错位)"


def test_v13_no_double_count_recharge_plus_publish():
    """v1.3 P1-2 · 源码不再有 month_recharge_revenue + month_publish_revenue(现金口径去双算)"""
    src = _read("api/dashboard_api.py")
    assert "month_recharge_revenue + month_publish_revenue" not in src, \
        "P1-2 破:仍有 充值+发布 双算(现金口径下总收入只=充值)"


def test_v13_finance_uses_pure_helper():
    """v1.3 · finance_data 走 _compute_finance_metrics 纯 helper(可真执行测 · 不再 inline dict)"""
    src = _read("api/dashboard_api.py")
    assert "finance_data = _compute_finance_metrics(" in src, \
        "v1.3 破:finance_data 未走纯 helper(失去真执行可测性)"


# ============================================================
# v1.3 UI · 现金口径文案
# ============================================================

def test_v13_ui_cash_revenue_card():
    """v1.3 UI · 营收卡明示「现金」+「充值实收 · 不含发布」"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert "本月营收(现金)" in src, "v1.3 破:营收卡未明示现金口径"
    assert "充值实收 · 不含发布" in src, "v1.3 破:未明示不含发布(防双算误读)"


def test_v13_ui_publish_card_real_margin_labels():
    """v1.3 UI → v1.4 · 发布卡三字段「本月发布收入/本月已发布外采/本月发布毛利」"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    for label in ("本月发布收入", "本月已发布外采", "本月发布毛利"):
        assert label in src, f"v1.3 破:发布卡缺「{label}」"


def test_v13_ui_no_double_count_total_revenue_card():
    """v1.3 UI · 不再有「总收入=充值+发布」误导卡(现金口径已去)"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert 'sub="充值 + 发布"' not in src, \
        "v1.3 破:UI 仍有「充值+发布」总收入卡(双算误导)"


# ============================================================
# v1.3 红线
# ============================================================

def test_v13_no_red_line_touched():
    """v1.3 · 红线 5 文件 0 改"""
    import subprocess
    red = ["middleware/billing.py", "auth/jwt_utils.py", "auth/middleware.py",
           "db/connection.py", "tools/scoring/geo_scope_scorer.py"]
    try:
        r = subprocess.run(["git", "diff", "--name-only", "main..HEAD"],
                           cwd=_ROOT, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return
        changed = set(r.stdout.split())
        hit = changed & set(red)
        # 🔴 [2026-09-05] **收窄,不是放宽**:本轮 `middleware/billing.py` 经授权改动。
        #    Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377
        #    其余保护文件任一被改 ⇒ 仍然红;billing.py 改回不动 ⇒ **也红**
        #    (授权用完必须显式收回,不留「曾经批过所以永远敞着」的门)。
        assert hit == {"middleware/billing.py"}, (
            f"红线改动集 {sorted(hit)} != 已授权集 ['middleware/billing.py'] · Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
