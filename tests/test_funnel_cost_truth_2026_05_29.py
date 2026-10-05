"""漏斗/成本口径真值修 · 2026-05-29 · 真执行测试

老板报"9 步漏斗 发布=2/监测=5 与真实(7/20)严重不符 + 本月成本只见 LLM ¥428"。
根因:publish/monitor 埋点 2026-05-25 才补(4天) vs 90 天窗口 → 低估;
     本月成本 publish 源用 item.cost_yuan(报价估值)≠ 真实采购 mhz_synced_orders.price。

修(老板 3 件全批):
  Q1 compute_flow_funnel:publish/monitor 改读真实表(COUNT DISTINCT brand_id)· 其余 5 stage 维持埋点
  Q2 _compute_cost_breakdown:cost 分项 {llm_api, publish_media(真实采购), monitoring_extra}

本测试用 fake cursor(按 SQL 路由 canned 结果)真跑 compute_flow_funnel 覆盖逻辑;
_compute_cost_breakdown 纯函数直接 import 真跑。
"""
from __future__ import annotations

import pytest


# ============================================================
# Q1 · compute_flow_funnel 真实表口径覆盖
# ============================================================
class _FakeCur:
    """按最近一次 execute 的 SQL 路由 fetchone;GROUP BY 走 fetchall。"""

    def __init__(self, pub_cnt=7, mon_cnt=20, raise_on_pub=False):
        self._sql = ""
        self.executed: list[str] = []
        self._pub_cnt = pub_cnt
        self._mon_cnt = mon_cnt
        self._raise_on_pub = raise_on_pub

    def execute(self, sql, params=None):
        self._sql = sql
        self.executed.append(sql)
        if self._raise_on_pub and "FROM mhz_publish_order_items" in sql:
            raise RuntimeError("模拟 publish 真实表查询失败")

    def fetchall(self):
        # pipeline_stage_log GROUP BY stage_name(埋点口径 · publish/monitor 故意低估)
        return [
            {"stage_name": "diagnosis", "cnt": 41},
            {"stage_name": "quote", "cnt": 10},
            {"stage_name": "pay", "cnt": 7},
            {"stage_name": "write", "cnt": 9},
            {"stage_name": "publish", "cnt": 2},
            {"stage_name": "monitor", "cnt": 5},
            {"stage_name": "report", "cnt": 3},
        ]

    def fetchone(self):
        if "FROM mhz_publish_order_items" in self._sql:
            return {"cnt": self._pub_cnt}
        if "FROM monitoring_tasks" in self._sql:
            return {"cnt": self._mon_cnt}
        return {"cnt": 0}

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.rolled_back = 0

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


def _patch_conn(monkeypatch, cur):
    from db import pipeline_stage_log_db as mod
    monkeypatch.setattr(mod, "_get_connection", lambda: _FakeConn(cur))
    return mod


def test_funnel_publish_monitor_overridden_by_real_tables(monkeypatch):
    cur = _FakeCur(pub_cnt=7, mon_cnt=20)
    mod = _patch_conn(monkeypatch, cur)
    stages = mod.compute_flow_funnel(days=90, actor_user_id=None)
    by = {s["name"]: s for s in stages}
    # publish/monitor 被真实表口径覆盖(7/20),不是埋点的 2/5
    assert by["publish"]["count"] == 7
    assert by["monitor"]["count"] == 20
    # 其余 5 stage 维持埋点口径
    assert by["diagnosis"]["count"] == 41
    assert by["quote"]["count"] == 10
    assert by["pay"]["count"] == 7
    assert by["write"]["count"] == 9
    assert by["report"]["count"] == 3
    # ratio 基于覆盖后的 count 重算(publish 7/41 ≈ 0.1707)
    assert by["publish"]["ratio"] == round(7 / 41, 4)
    assert by["monitor"]["ratio"] == round(20 / 41, 4)


def test_funnel_global_scope_no_owner_filter(monkeypatch):
    cur = _FakeCur()
    mod = _patch_conn(monkeypatch, cur)
    mod.compute_flow_funnel(days=90, actor_user_id=None)
    pub_sql = next(s for s in cur.executed if "FROM mhz_publish_order_items" in s)
    mon_sql = next(s for s in cur.executed if "FROM monitoring_tasks" in s)
    # global(actor=None)不带 owner 过滤
    assert "owner_user_id" not in pub_sql
    assert "owner_user_id" not in mon_sql


def test_funnel_agent_scope_adds_owner_filter(monkeypatch):
    cur = _FakeCur()
    mod = _patch_conn(monkeypatch, cur)
    mod.compute_flow_funnel(days=30, actor_user_id=77)
    pub_sql = next(s for s in cur.executed if "FROM mhz_publish_order_items" in s)
    mon_sql = next(s for s in cur.executed if "FROM monitoring_tasks" in s)
    # agent scope:按 brands.owner_user_id 过滤(鉴权口径与埋点一致)
    assert "b.owner_user_id = %s" in pub_sql and "JOIN brands b" in pub_sql
    assert "b.owner_user_id = %s" in mon_sql and "JOIN brands b" in mon_sql
    # 窗口天数注入(int 安全)
    assert "INTERVAL '30 days'" in pub_sql


def test_funnel_publish_query_uses_published_status_and_brand_chain(monkeypatch):
    cur = _FakeCur()
    mod = _patch_conn(monkeypatch, cur)
    mod.compute_flow_funnel(days=90, actor_user_id=None)
    pub_sql = next(s for s in cur.executed if "FROM mhz_publish_order_items" in s)
    # 真实链:item→order→article→quote.brand_id · 只数 published · 排除 brand 为空
    assert "i.status = 'published'" in pub_sql
    assert "JOIN mhz_publish_orders mo" in pub_sql
    assert "JOIN quotes q" in pub_sql
    assert "q.brand_id IS NOT NULL" in pub_sql


def test_funnel_falls_back_to_stage_log_on_real_query_error(monkeypatch):
    # publish 真实表查询抛错 → 回退埋点口径(2)· 且 rollback 防污染事务
    cur = _FakeCur(raise_on_pub=True)
    conn_holder = {}
    from db import pipeline_stage_log_db as mod

    def _mkconn():
        c = _FakeConn(cur)
        conn_holder["c"] = c
        return c
    monkeypatch.setattr(mod, "_get_connection", _mkconn)
    stages = mod.compute_flow_funnel(days=90, actor_user_id=None)
    by = {s["name"]: s for s in stages}
    assert by["publish"]["count"] == 2          # 回退埋点
    assert by["monitor"]["count"] == 20          # monitor 仍成功覆盖
    assert conn_holder["c"].rolled_back >= 1      # 失败后 rollback


# ============================================================
# Q2 · _compute_cost_breakdown 纯函数
# ============================================================
def test_cost_breakdown_three_components_sum():
    from api.dashboard_api import _compute_cost_breakdown
    out = _compute_cost_breakdown(
        llm_api_cost_yuan=428.19,
        publish_procurement_cost_yuan=10596.00,
    )
    assert out["cost_breakdown"] == {"llm_api": 428.19, "publish_media": 10596.0, "monitoring_extra": 0.0}
    assert out["total_cost_yuan"] == 11024.19          # 老板锁定值
    assert "毛利" in out["cost_margin_note"]            # 毛利延后 phase 标注


def test_cost_breakdown_handles_none_and_zero():
    from api.dashboard_api import _compute_cost_breakdown
    out = _compute_cost_breakdown(llm_api_cost_yuan=None, publish_procurement_cost_yuan=0)
    assert out["cost_breakdown"]["llm_api"] == 0.0
    assert out["cost_breakdown"]["publish_media"] == 0.0
    assert out["total_cost_yuan"] == 0.0


def test_cost_breakdown_revenue_untouched():
    """Q2 只动 cost · 不引入任何 revenue 字段(revenue 口径维持 v1.4 cash_recharge_only)。"""
    from api.dashboard_api import _compute_cost_breakdown
    out = _compute_cost_breakdown(llm_api_cost_yuan=1, publish_procurement_cost_yuan=2)
    assert not any("revenue" in k for k in out.keys())
