"""[BUG-P2] 利润换算力/提现 FIFO 防双花在 READ COMMITTED 下快照竞态 · 静态守护

根因:防双花靠 SELECT ...,(子查询 SUM(items.locked)) AS already_locked FROM ledger FOR UPDATE。
PostgreSQL READ COMMITTED 下 tx2 在 FOR UPDATE 等 tx1 提交;锁到手后因 ledger 行本身未被 UPDATE
(设计就是不动 ledger.status)无 EvalPlanQual 重查,SELECT 列表里子查询仍用语句开始旧快照 →
看不到 tx1 刚插入的 redemption/settlement items → already_locked 偏小 → 两边都锁满同一 ledger
→ 同一笔 settled 利润换两次算力 / 提现+换算力双发。WORKERS>1(compose 默认 4)/sync 端点/蓝绿切即触发。
修:redeem 与 withdraw 入口加 pg_advisory_xact_lock(同 classid 920506, agent_user_id),
把同一 agent 的两条出金路径串行化(锁到主事务结束)。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_both_paths_advisory_lock_same_class():
    redeem = (ROOT / "services" / "agent_commission_redeem.py").read_text(encoding="utf-8")
    rev = (ROOT / "services" / "agent_revenue.py").read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock(920506" in redeem, "利润换算力入口须加 advisory xact lock"
    assert "pg_advisory_xact_lock(920506" in rev, "提现入口须加同 classid advisory xact lock(与换算力互斥)"
