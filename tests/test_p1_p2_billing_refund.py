"""[BUG-P1/P2] billing.py 退款幂等 CAST 溢出 + 5 秒窗误并组 · 回归单测(纯函数+静态守护)

P1 CAST 溢出:refund_points / _refund_v35 的幂等子查询 CAST(order_id AS BIGINT),
  遇 27 位充值退款单号溢出 bigint → 整退费事务崩 → 调用方 try/except 吞 → 用户白扣。
  修:两处改 id::text 文本比较(语义等价,refund 流水 order_id=被退 consume id)。
P2 5 秒窗误并组:有 order_id 但不同的独立扣费被 5 秒窗并退(一次失败多退 2-3 笔)。
  修:抽 _select_split_refund_txs — 有 order_id 严格同 order_id 才并,5 秒窗仅兜底 NULL。
"""
from datetime import datetime, timedelta
from pathlib import Path

from middleware.billing import _select_split_refund_txs as sel

T = datetime(2026, 6, 10, 12, 0, 0)


def _tx(tid, order_id, dt_offset=0):
    return {"id": tid, "order_id": order_id, "created_at": T + timedelta(seconds=dt_offset)}


def test_same_order_id_grouped():
    # 同一次拆分扣费(共享 order_id)→ 3 笔全并组退
    txs = [_tx(1, "FRZ:abc"), _tx(2, "FRZ:abc"), _tx(3, "FRZ:abc")]
    assert len(sel(txs)) == 3


def test_different_order_id_not_grouped():
    # [P2 核心修复] 5 秒内但 order_id 不同(批量诊断逐笔)→ 只退第一笔,不误并别次扣费
    txs = [_tx(1, "OR:a", 0), _tx(2, "OR:b", 0.3), _tx(3, "OR:c", 0.5)]
    out = sel(txs)
    assert len(out) == 1 and out[0]["id"] == 1


def test_mixed_same_then_diff():
    # base 同 order_id 的并,不同的不并
    txs = [_tx(1, "FRZ:a", 0), _tx(2, "FRZ:a", 0.1), _tx(3, "OR:b", 0.2)]
    assert [t["id"] for t in sel(txs)] == [1, 2]


def test_null_order_id_within_5s_grouped():
    # 历史无 order_id 行 → 5 秒窗兜底并组
    txs = [_tx(1, None, 0), _tx(2, None, 3)]
    assert len(sel(txs)) == 2


def test_null_order_id_beyond_5s_not_grouped():
    txs = [_tx(1, None, 0), _tx(2, None, 6)]
    assert len(sel(txs)) == 1


def test_null_base_other_has_order_id_not_grouped():
    # base 为 NULL 但对方有 order_id → 不并(不同来源)
    txs = [_tx(1, None, 0), _tx(2, "OR:x", 1)]
    assert len(sel(txs)) == 1


def test_empty():
    assert sel([]) == []


def test_p1_cast_overflow_fixed_static():
    # [P1 静态守护] 不再 CAST order_id/related_order_id(防 27 位单号溢出 bigint),
    # 部分退款幂等按每条 consume 的文本 id 累计关联退款，不能用 NOT IN 整行排除。
    src = Path(__file__).resolve().parents[1].joinpath("middleware", "billing.py").read_text(encoding="utf-8")
    assert "CAST(order_id AS BIGINT)" not in src, "refund_points 仍 CAST order_id → 27 位单号溢出复发"
    assert "SUM(r.amount)" in src and "r.order_id = c.id::text" in src
    # [单账本收敛 2026-07-27] 原有两条针对 _refund_v35_customer_credit 的断言已移除:
    # 该函数(及其 related_order_id 相关 SQL)随 V3.5 分流一并删除,断言对象已不存在。
    # legacy 侧的防溢出与部分退款幂等断言【保留不动】—— 它现在是唯一路径,更该守。
    assert "id::text NOT IN" not in src, "任意部分退款不得排除整条原消费流水"
