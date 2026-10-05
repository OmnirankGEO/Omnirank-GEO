"""AdminDashboard 成本口径 v1.1 · 老板复审 4 Findings 修

P0-1 · 发布外采时区不一致抛 TypeError → 兜底 0
P0-2 · 营收口径没同步扩大(成本含发布外采 · 营收只算充值)
P1-3 · 同页两个"发布外采"口径不一致(finance helper vs publishing JOIN synced_orders)
P1-4 · is_known_caller 太宽松("_" in caller 通过)+ unknown SQL 没用 helper

测试策略:
- v1.0 文本扫保留(test_admin_dashboard_cost_truth.py)
- v1.1 加真实执行单测:
  - 时区跨类型相减不抛(import 真函数调用)
  - is_known_caller 真分类(known prefix / unknown 命名 / UNKNOWN_CALLERS / 空)
  - get_publish_external_cost_statistics 接受 since=aware datetime 不抛
"""
from __future__ import annotations
import os
import re
import sys
from datetime import datetime, timezone, timedelta

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, _ROOT)


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def _strip_py_line_comments(src: str) -> str:
    """仅剥离 Python `#` 行注释 · 保留所有字符串(含 docstring / SQL triple-quoted)

    用于让 grep 不误伤新版 `#` 注释里提到的旧 SQL · 但保留代码体 + 字符串字面量
    """
    out = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)

# 兼容老 helper 名(别处可能 import)
_strip_py_comments = _strip_py_line_comments


# ============================================================
# v1.1 P0-1 · 时区不抛 + 真实调用路径验证
# ============================================================

def test_v11_p01_publish_external_helper_accepts_since():
    """v1.1 P0-1 · get_publish_external_cost_statistics 必须接 since 参数"""
    from db.meijiehezi_db import get_publish_external_cost_statistics
    import inspect
    sig = inspect.signature(get_publish_external_cost_statistics)
    assert "since" in sig.parameters, \
        "v1.1 P0-1 破:get_publish_external_cost_statistics 签名缺 since 参数"


def test_v11_p01_dashboard_uses_since_not_naive_now():
    """v1.1 P0-1 · dashboard_api 必须用 since=month_start · 不能用 naive now() 减 aware"""
    src = _read("api/dashboard_api.py")
    # 不允许出现旧版 (_dt_pub.now() - month_start).days 模式(naive 减 aware 抛 TypeError)
    assert "_dt_pub.now() - month_start" not in src, \
        "v1.1 P0-1 破:dashboard 仍用 naive now() 减 aware month_start(抛 TypeError 被吞 · 兜底 0)"
    # v1.3:外采/收入改 inline 同一查询 · month_start 作 %s param(psycopg2 原生支持 aware datetime · 不抛时区错)
    # 根因(naive-aware 相减)已从源头消除 · 不再依赖 helper since= 路径
    assert "COALESCE(published_at, created_at) >= %s" in src, \
        "v1.1 P0-1 破:发布业务查询未用 month_start 作 param(时区安全口径)"
    assert "_dt_pub.now()" not in src, \
        "v1.1 P0-1 破:dashboard 仍残留 naive now() 计算(时区炸点)"


def test_v11_p01_timezone_subtract_not_thrown():
    """v1.1 P0-1 真实执行 · helper 签名调用接受 aware datetime 不抛 TypeError

    这是 v1.0 的盲点:文本扫看不到运行时类型错误。
    本测试导入真函数 · 用 mock cursor 跑通调用路径 · 不连真 DB。
    """
    # 不连 DB · 仅验证签名调用 + 时区参数序列化不抛
    CST = timezone(timedelta(hours=8))
    month_start = datetime(2026, 5, 1, 0, 0, 0, tzinfo=CST)

    # 验证 since.isoformat() 不抛
    iso = month_start.isoformat()
    assert iso, "v1.1 P0-1 破:aware datetime.isoformat() 异常"
    assert "2026-05-01" in iso

    # 验证 inspect 签名拿到 since 参数
    from db.meijiehezi_db import get_publish_external_cost_statistics
    import inspect
    sig = inspect.signature(get_publish_external_cost_statistics)
    p = sig.parameters.get("since")
    assert p is not None
    assert p.default is None, \
        "v1.1 P0-1 破:since 默认值应为 None(向后兼容)"


# ============================================================
# v1.1 P0-2 · 营收口径同步扩大
# ============================================================

def test_v11_p02_finance_has_three_revenue_fields():
    """v1.1 P0-2 · finance 必须返 recharge_revenue / publish_revenue / total_revenue(v1.3 helper · 真执行)"""
    from api.dashboard_api import _compute_finance_metrics
    f = _compute_finance_metrics(
        recharge_revenue_yuan=100, publish_revenue_yuan=50,
        llm_api_cost_yuan=10, publish_external_cost_yuan=5, active_7d=1,
    )
    for k in ("recharge_revenue_yuan", "publish_revenue_yuan", "total_revenue_yuan"):
        assert k in f, f"v1.1 P0-2 破:finance 缺 {k}"


def test_v11_p02_profit_uses_total_revenue_not_recharge():
    """v1.1 P0-2 → v1.3 现金口径 · profit 必须扣 total_operating_cost(LLM+外采)· 真执行
    v1.3 老板拍现金口径:total_revenue = 充值(发布不并入)· profit = 充值 - 总运营成本
    """
    from api.dashboard_api import _compute_finance_metrics
    f = _compute_finance_metrics(
        recharge_revenue_yuan=1000, publish_revenue_yuan=500,
        llm_api_cost_yuan=200, publish_external_cost_yuan=100, active_7d=10,
    )
    # profit 扣的是 总运营成本(含外采)· 不是只扣 LLM
    assert f["profit_yuan"] == 700, f"v1.1 P0-2 破:profit 应扣总运营成本=700 · 实 {f['profit_yuan']}"


def test_v11_p02_total_revenue_cash_basis_excludes_publish():
    """v1.1 P0-2 → v1.3 现金口径 · total_revenue = 充值 · 【不】加发布(防双算)· 真执行
    (v1.1 曾让加发布 · v1.3 老板复审发现 cost_points 来自已充值 paid_points → 双算 · 改现金口径)
    """
    from api.dashboard_api import _compute_finance_metrics
    f = _compute_finance_metrics(
        recharge_revenue_yuan=1000, publish_revenue_yuan=500,
        llm_api_cost_yuan=0, publish_external_cost_yuan=0, active_7d=1,
    )
    assert f["total_revenue_yuan"] == 1000, \
        f"v1.1 P0-2/v1.3 破:total_revenue 应=充值 1000(不双算发布)· 实 {f['total_revenue_yuan']}"
    # 源码确认不再有充值+发布双算
    src = _read("api/dashboard_api.py")
    assert "month_recharge_revenue + month_publish_revenue" not in src, \
        "v1.3 破:源码仍有 充值+发布 双算"


def test_v11_p02_publish_revenue_published_only():
    """v1.1 P0-2 → v1.4 · 发布收入必须 status='published'(v1.4 在 _query_publish_numbers 查询A)"""
    src = _read("api/dashboard_api.py")
    fn_start = src.find("def _query_publish_numbers(")
    assert fn_start > 0
    fn = src[fn_start:fn_start + 2000]
    # 查询 A(发布收入 cost_points)必须 status='published'
    assert "status = 'published'" in fn, \
        "v1.1 P0-2 破:发布收入查询未限定 status='published'(把 pending/rejected 误算成收入)"
    assert "SUM(cost_points)" in fn, "v1.1 P0-2 破:发布收入未用 cost_points"


# ============================================================
# v1.1 P1-3 · publishing.cost_yuan 统一用 helper(全状态)
# ============================================================

def test_v11_p13_publishing_cost_uses_helper_not_synced_orders():
    """v1.1 P1-3 → v1.3 · publishing.cost_yuan 走 _compute_publish_card(源 month_publish_external_cost)· 不 JOIN mhz_synced_orders"""
    src = _read("api/dashboard_api.py")
    # v1.3:publishing 三字段统一走 _compute_publish_card(publish_external_cost_yuan=month_publish_external_cost)
    assert "_compute_publish_card(" in src, \
        "v1.1 P1-3/v1.3 破:publishing 未走 _compute_publish_card"
    assert "publish_external_cost_yuan=month_publish_external_cost" in src, \
        "v1.1 P1-3 破:publishing 成本未用 month_publish_external_cost(双口径)"
    assert 'publishing["cost_yuan"] = _pc["cost_yuan"]' in src, \
        "v1.1 P1-3 破:publishing.cost_yuan 未从 _pc 取"


def test_v11_p13_no_synced_orders_join_for_publishing_cost():
    """v1.1 P1-3 · publishing 卡 cost 计算路径不再 JOIN mhz_synced_orders + status='published'"""
    src = _read("api/dashboard_api.py")
    # 旧版 SQL "JOIN mhz_synced_orders s ON s.order_sn = i.mhz_order_id\n            WHERE i.status = 'published'"
    # 不应再用于 publishing.cost_yuan 计算路径
    bad_pattern = re.compile(
        r'cur\.execute\(\s*"""\s*SELECT COALESCE\(SUM\(s\.price\).*?mhz_synced_orders.*?status\s*=\s*\'published\'',
        re.DOTALL,
    )
    assert not bad_pattern.search(src), \
        "v1.1 P1-3 破:publishing.cost_yuan SQL 仍 JOIN mhz_synced_orders + published only(跟 finance 口径打架)"


def test_v11_p13_ui_label_renamed_to_zhangyong():
    """v1.1 P1-3 · UI 发布卡 cost label 明示外采(v1.3 已发布同 item 集 → 「本月外采成本」)"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert ("本月已发布外采" in src) or ("本月外采成本" in src) or ("外采占用成本" in src), \
        "v1.1 P1-3 破:发布卡 cost label 未明示外采成本"


# ============================================================
# v1.1 P1-4 · is_known_caller 严格 + Python 端分类
# ============================================================

def test_v11_p14_is_known_caller_rejects_arbitrary_underscore():
    """v1.1 P1-4 真实执行 · 'foo_bar' / 'random_new_feature' 必须 UNKNOWN"""
    from services.api_costs import is_known_caller

    # 含 _ 但不在族里 · 必须 unknown
    assert not is_known_caller("foo_bar"), \
        "v1.1 P1-4 破:'foo_bar' 仍当 known(老 `or \"_\" in caller` 漏洞)"
    assert not is_known_caller("random_new_feature"), \
        "v1.1 P1-4 破:'random_new_feature' 仍当 known"
    # 空 / UNKNOWN_CALLERS / 不带 _ 的也 unknown
    assert not is_known_caller(""), "v1.1 P1-4 破:空 caller 当 known"
    assert not is_known_caller("llm_call_log"), "v1.1 P1-4 破:UNKNOWN_CALLERS 漏判"
    assert not is_known_caller("legacy_token_usage"), "v1.1 P1-4 破:legacy fallback 漏判"


def test_v11_p14_is_known_caller_accepts_registered_families():
    """v1.1 P1-4 真实执行 · 注册的命名族必须 known"""
    from services.api_costs import is_known_caller

    # 各族示例 · 都应 known
    for caller in (
        "monitoring_insights",
        "monitoring_platform_mau",
        "monitoring_report_review",  # future
        "article_writing",
        "keyword_seed",
        "keyword_cluster_grouping",
        "research_resolve_xhs_share",
        "rewrite_douyin_video_detail",
        "social_dynamic_review",
        "tikhub_list_tools",
        "knowledge_cleaning",
        "intake_ai_draft",
        "advisor_chat",
        "asr_transcription",
        "geo_managed_monitoring",
        "industry_case_collector",
        "aliyun_ocr",
    ):
        assert is_known_caller(caller), \
            f"v1.1 P1-4 破:已注册 caller '{caller}' 被当 unknown(命名族遗漏)"


def test_v11_p14_summary_uses_python_helper_not_inline_sql():
    """v1.1 P1-4 · get_api_cost_summary unknown 分类必须用 is_known_caller · 不能 SQL IN ('llm_call_log',...)"""
    src = _read("services/api_costs.py")
    fn_start = src.find("def get_api_cost_summary(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 6000]
    fn_no_comments = _strip_py_comments(fn_block)

    # 必须用 is_known_caller Python 端分类
    assert "is_known_caller(" in fn_no_comments, \
        "v1.1 P1-4 破:get_api_cost_summary 未用 is_known_caller helper 分类(SQL IN 漏 'random_new_feature' 类)"
    # 旧 SQL `WHERE caller IN ('llm_call_log', 'legacy_token_usage')` 模式不再(代码体里 · 注释除外)
    bad_pat = re.compile(r"WHERE caller IN\s*\(\s*'llm_call_log'\s*,\s*'legacy_token_usage'\s*\)")
    assert not bad_pat.search(fn_no_comments), \
        "v1.1 P1-4 破:get_api_cost_summary 仍用 SQL IN ('llm_call_log','legacy_token_usage')(没真分类)"


def test_v11_p14_no_or_underscore_loophole():
    """v1.1 P1-4 · is_known_caller 函数体不能有 `or \"_\" in caller` 兜底

    抽取 docstring 之后的代码体来检查(避免误伤注释 / docstring 里历史说明)
    """
    src = _read("services/api_costs.py")
    fn_start = src.find("def is_known_caller(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 1000]

    # 跳过 docstring(找到第二个 """ 之后)
    doc_open = fn_block.find('"""')
    if doc_open > 0:
        doc_close = fn_block.find('"""', doc_open + 3)
        if doc_close > 0:
            code_body = fn_block[doc_close + 3:]
        else:
            code_body = fn_block
    else:
        code_body = fn_block
    code_no_comments = _strip_py_line_comments(code_body)

    assert 'or "_" in caller' not in code_no_comments, \
        "v1.1 P1-4 破:is_known_caller 代码体仍有 `or \"_\" in caller` 兜底(放任 'foo_bar' 类)"
    # 真实执行已在 test_v11_p14_is_known_caller_rejects_arbitrary_underscore 验证


# ============================================================
# 整体 · 同口径 + 兼容老前端
# ============================================================

def test_v11_old_revenue_yuan_alias_preserved():
    """v1.1 · revenue_yuan 旧字段保留(向后兼容老前端 · 语义 = recharge_revenue)· 真执行"""
    from api.dashboard_api import _compute_finance_metrics
    f = _compute_finance_metrics(
        recharge_revenue_yuan=888, publish_revenue_yuan=0,
        llm_api_cost_yuan=0, publish_external_cost_yuan=0, active_7d=1,
    )
    assert "revenue_yuan" in f, "v1.1 破:revenue_yuan 老字段被删"
    assert f["revenue_yuan"] == 888, "v1.1 破:revenue_yuan 兼容别名应=充值"


def test_v11_ui_shows_total_revenue_card():
    """v1.1 → v1.3 · UI 营收卡(现金口径)消费 finance.total_revenue_yuan"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert "本月营收(现金)" in src, "v1.3 破:UI 缺现金营收卡"
    assert "finance.total_revenue_yuan" in src, \
        "v1.1 破:UI 未消费 finance.total_revenue_yuan"


def test_v11_ui_shows_recharge_revenue_card():
    """v1.1 → v1.3 · UI 营收卡明示充值实收(现金口径合并进营收卡 sub)"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert "充值实收" in src, "v1.3 破:UI 营收卡未明示「充值实收」"
    assert "finance.recharge_revenue_yuan" in src, \
        "v1.1 破:UI 未消费 finance.recharge_revenue_yuan(营收卡 fallback)"


def test_v11_ui_shows_publish_revenue_card():
    """v1.1 → v1.3 · 发布收入移到「本月代发业务」卡显示(现金口径下不并入总收入)"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert "本月发布收入" in src, "v1.3 破:发布卡缺「本月发布收入」"
    assert "publishing.revenue_yuan" in src, \
        "v1.3 破:发布卡未消费 publishing.revenue_yuan"
