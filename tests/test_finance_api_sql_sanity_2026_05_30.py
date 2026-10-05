"""
财务中心 · 离线 SQL 健全性测试 · GEO CTO-15.23 · 2026-05-30

本地无 DB · 用 fake cursor 跑通全部 finance 端点,验证:
  1. 每个端点逻辑能跑通不崩(fake 空数据)
  2. 每条 SQL 的 %s 占位符数 == 参数数(防真 DB 占位符不匹配)
  3. 无未渲染 f-string('{'/'}' 残留 · f-string-P0 教训)

真 SQL(真实列名/类型)第一次执行仍在 Deploy GATE 真 curl · 本测试聚焦语法/占位符/逻辑。
直接 exec 跑:python tests/test_finance_api_sql_sanity_2026_05_30.py
"""

import asyncio
import types

import db.connection
import api.finance_api as fa

_executed = []


class FakeCursor:
    def execute(self, sql, params=()):
        _executed.append((sql, tuple(params) if params else ()))

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def __iter__(self):
        return iter([])


class FakeConn:
    def cursor(self, *a, **k):
        return FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _fake_get_connection():
    return FakeConn()


# monkeypatch 连接获取(finance_api / finance_db 均 lazy `from db.connection import get_connection`)
db.connection.get_connection = _fake_get_connection


class FakeReq:
    def __init__(self):
        self.state = types.SimpleNamespace(user={"is_admin": True, "user_id": 1})


async def _run_all():
    req = FakeReq()
    res = {}
    res["overview"] = await fa.overview(req, period="year", basis="accrual", start=None, end=None)
    res["overview_cash"] = await fa.overview(req, period="month", basis="cash", start=None, end=None)
    res["pnl"] = await fa.pnl(req, period="month", basis="accrual", start=None, end=None)
    res["liabilities"] = await fa.liabilities(req)
    res["cashflow"] = await fa.cashflow(req, period="quarter", start=None, end=None)
    res["cost_center"] = await fa.cost_center(req, period="month", start=None, end=None)
    res["agent_settlement"] = await fa.agent_settlement(req, period="year", start=None, end=None)
    res["reconciliation"] = await fa.reconciliation(req)
    res["unit_economics"] = await fa.unit_economics(req, period="month", start=None, end=None)
    res["ledger"] = await fa.ledger(
        req, period="month", start=None, end=None, user_id=None, brand_id=None,
        feature_code=None, txn_type=None, point_type=None, limit=100, offset=0)
    res["opex_list"] = await fa.opex_list(req, period_start=None, period_end=None)
    return res


def main():
    res = asyncio.run(_run_all())

    # 1. 每个端点返回 dict
    for k, v in res.items():
        assert isinstance(v, dict), f"端点 {k} 未返回 dict: {type(v)}"

    # 2. SQL sanity
    problems = []
    for sql, params in _executed:
        if "{" in sql or "}" in sql:
            problems.append(("未渲染f-string", sql[:90]))
        # 占位符匹配 · 排除 api_costs 的 UNION SQL(成熟代码 · 含 %% 转义)
        if "UNION" not in sql.upper():
            ph = sql.count("%s")
            pc = len(params)
            if ph != pc:
                problems.append((f"占位符{ph}≠参数{pc}", sql[:90]))

    assert not problems, "SQL 问题:\n" + "\n".join(f"  - {t}: {s}" for t, s in problems)

    print(f"PASS · {len(_executed)} 条 SQL 全部 sanity OK · {len(res)} 端点逻辑不崩")
    # 抽样确认确实执行了 finance 自己的查询
    fin_sqls = [s for s, _ in _executed if "point_transactions" in s or "agent_revenue_ledger" in s
                or "operating_expenses" in s or "user_wallets" in s]
    assert fin_sqls, "未执行到任何 finance 核心表查询(测试无效)"
    print(f"  finance 核心表查询命中 {len(fin_sqls)} 条")


if __name__ == "__main__":
    main()
