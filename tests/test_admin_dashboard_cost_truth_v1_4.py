"""AdminDashboard 成本口径 v1.4 · Codex 复审 v1.3 后 1 P1 修

P1 · 总运营成本低估发布外采
     v1.3 把"发布毛利成本(已发布)"和"总运营外采成本(全状态)"复用同一已发布口径
     → 本月已提交但未发布/拒稿/失败的外采占用漏算 → Dashboard 低估总成本
     (helper docstring 明确:外采成本按提交占用 · 全状态都算)

修法:三口径分离
  - 发布收入        已发布集 cost_points/130
  - 发布毛利成本    已发布集 cost_yuan(配对收入算发布卡毛利)
  - 总外采成本      全状态集 cost_yuan WHERE created_at >= month_start(喂总运营成本)

★ 全【真实执行】· _query_publish_numbers 用 Fake cursor 跑通双查询 · 直接验 Codex 场景
"""
from __future__ import annotations
import os
import re
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


class _FakeCursor:
    """模拟 psycopg2 RealDictCursor · 按 execute 顺序返预设 fetchone 行"""
    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        r = self._rows[self._i] if self._i < len(self._rows) else None
        self._i += 1
        return r


# ============================================================
# v1.4 P1 真执行 · _query_publish_numbers 三口径分离
# ============================================================

def test_v14_query_publish_numbers_splits_three_basis():
    """v1.4 P1 真执行 · Codex 场景:已发布外采 100 · 全状态外采 300(含 pending/rejected 200)"""
    from api.dashboard_api import _query_publish_numbers
    # 查询 A(已发布):cost_points=19500(=¥150) · cost_yuan=100
    # 查询 B(全状态):cost_yuan=300
    cur = _FakeCursor([{"pts": 19500, "yuan": 100.0}, {"yuan": 300.0}])
    revenue, margin_cost, external_all = _query_publish_numbers(cur, "2026-05-01")
    assert revenue == 150.0, f"发布收入应=19500/130=150 · 实 {revenue}"
    assert margin_cost == 100.0, f"已发布毛利成本应=100 · 实 {margin_cost}"
    assert external_all == 300.0, \
        f"v1.4 P1 破:全状态总外采应=300(含未发布占用)· 实 {external_all}"


def test_v14_external_all_geq_margin_cost():
    """v1.4 P1 真执行 · 全状态总外采 >= 已发布毛利成本(全状态是超集)"""
    from api.dashboard_api import _query_publish_numbers
    cur = _FakeCursor([{"pts": 13000, "yuan": 80.0}, {"yuan": 250.0}])
    revenue, margin_cost, external_all = _query_publish_numbers(cur, "2026-05-01")
    assert external_all >= margin_cost, \
        f"v1.4 P1 破:全状态外采({external_all}) < 已发布成本({margin_cost})· 口径反了"


def test_v14_query_publish_numbers_two_queries():
    """v1.4 P1 真执行 · 必须两个独立查询(已发布 status='published' + 全状态无 status 过滤)"""
    from api.dashboard_api import _query_publish_numbers
    cur = _FakeCursor([{"pts": 0, "yuan": 0}, {"yuan": 0}])
    _query_publish_numbers(cur, "2026-05-01")
    assert len(cur.executed) == 2, f"v1.4 P1 破:应 2 个查询(已发布+全状态)· 实 {len(cur.executed)}"
    # 查询 A 含 status='published' · 查询 B 不含(全状态)
    assert "status = 'published'" in cur.executed[0], "查询A 应过滤 status='published'"
    assert "status = 'published'" not in cur.executed[1], \
        "v1.4 P1 破:全状态查询B 不该过滤 status(漏未发布占用)"
    assert "cost_points" in cur.executed[0], "查询A 应取 cost_points(收入)"
    assert "created_at >= %s" in cur.executed[1], "查询B 应按 created_at(提交时间)全状态"


def test_v14_query_publish_numbers_null_safe():
    """v1.4 真执行 · 空结果(无订单)不崩 · 返 0"""
    from api.dashboard_api import _query_publish_numbers
    cur = _FakeCursor([{"pts": None, "yuan": None}, {"yuan": None}])
    revenue, margin_cost, external_all = _query_publish_numbers(cur, "2026-05-01")
    assert (revenue, margin_cost, external_all) == (0.0, 0.0, 0.0)


# ============================================================
# v1.4 P1 · 总运营成本用全状态外采(不低估)
# ============================================================

def test_v14_total_operating_uses_all_status_external():
    """v1.4 P1 真执行 · total_operating_cost 含全状态外采(300)· 不是已发布子集(100)"""
    from api.dashboard_api import _compute_finance_metrics
    # 全状态外采 300 喂 finance
    f = _compute_finance_metrics(
        recharge_revenue_yuan=1000, publish_revenue_yuan=150,
        llm_api_cost_yuan=0, publish_external_cost_yuan=300, active_7d=10,
    )
    assert f["total_operating_cost_yuan"] == 300.0, \
        f"v1.4 P1 破:总运营成本应含全状态外采 300 · 实 {f['total_operating_cost_yuan']}"
    assert f["publish_external_cost_yuan"] == 300.0, "顶部发布外采成本应=全状态 300"


def test_v14_publish_card_uses_published_subset_cost():
    """v1.4 P1 真执行 · 发布卡毛利用【已发布】成本(100)· 不是全状态(300)"""
    from api.dashboard_api import _compute_publish_card
    pc = _compute_publish_card(publish_revenue_yuan=150, publish_margin_cost_yuan=100)
    assert pc["cost_yuan"] == 100.0, "发布卡成本应=已发布子集 100"
    assert pc["profit_yuan"] == 50.0, "发布毛利=150-100=50(用已发布成本)"


# ============================================================
# v1.4 · 源码 / UI 口径分离断言
# ============================================================

def test_v14_dashboard_uses_query_helper():
    """v1.4 · dashboard 走 _query_publish_numbers(三口径分离 helper)"""
    src = _read("api/dashboard_api.py")
    assert "_query_publish_numbers(cur, month_start)" in src, \
        "v1.4 破:dashboard 未走 _query_publish_numbers"
    # total_operating 用全状态 external · 发布卡用 margin_cost
    assert "publish_margin_cost_yuan=month_publish_margin_cost" in src, \
        "v1.4 破:发布卡未用 margin_cost(已发布子集)"
    assert "month_llm_api_cost + month_publish_external_cost" in src, \
        "v1.4 破:总运营成本未用全状态 external_cost"


def test_v14_external_cost_distinct_from_margin_cost():
    """v1.4 · 源码三变量分离(revenue / margin_cost / external_cost)· 不复用"""
    src = _read("api/dashboard_api.py")
    for v in ("month_publish_revenue", "month_publish_margin_cost", "month_publish_external_cost"):
        assert v in src, f"v1.4 破:缺变量 {v}(三口径未分离)"


def test_v14_ui_distinguishes_two_cost_concepts():
    """v1.4 · UI 顶部「发布外采成本(全状态占用)」vs 发布卡「本月已发布外采」区分清"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    # 顶部全状态
    assert "全状态占用" in src, "v1.4 破:顶部发布外采成本未标「全状态占用」"
    # 发布卡已发布子集
    assert "本月已发布外采" in src, "v1.4 破:发布卡未标「本月已发布外采」(跟顶部全状态区分)"
