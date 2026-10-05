"""[v4→v5 req4] 文章生成 commit→thread.start 窗口:精确退款 + 耐久补偿 · 结构/契约判别锁。

[v5] 窗口收口逻辑已从 server.py 内联抽到 services/article_write_recovery.py(可真单测),server.py 闭包只注入依赖。
行为级 4 分支(退款成功/退款 false/退款异常/状态写失败 · 真落耐久工单)由 test_nogo_v5_thread_window_durable.py 覆盖。
本文件锁契约:① server 两个 except 都委托收口;② 收口模块按 charge_tx_id 精确退款且退款成功【才】恢复可重试;
             ③ 退款失败标 write_timeout + 落耐久工单;④ 全程无裸 except: pass。

判别性:把收口改回"扣费一律 write_timeout / except: pass 吞失败" → 本锁断言失败。
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRV = (ROOT / "server.py").read_text(encoding="utf-8")
REC = (ROOT / "services" / "article_write_recovery.py").read_text(encoding="utf-8")


def test_server_delegates_both_except_blocks():
    """HTTPException + 通用 Exception 两个 except 都必须调窗口收口(不遗漏任一失败路径)。"""
    assert SRV.count("await _handle_write_window_failure()") >= 2, \
        "两个 except 块都必须调用窗口收口"
    assert "handle_write_window_failure" in SRV, "server 必须委托到可测收口函数"
    assert "charge_tx_id=_ag_charge_txid" in SRV, "必须把本批 charge_tx_id 注入收口(精确退款依据)"


def test_recovery_module_refunds_by_charge_tx_id():
    assert "charge_tx_id=charge_tx_id" in REC, "🔴 已扣费必须按 charge_tx_id 精确退款"
    assert "refunded_ok" in REC, "必须有退款成功判定"


def test_recovery_reset_retriable_only_after_refund_success():
    # 结构:退款成功 → release(恢复可重试);退款 false/异常 → timeout + 落耐久工单
    ok_idx = REC.find("if refunded_ok:")
    rel_use = REC.find("release_fn(", ok_idx)
    else_idx = REC.find("else:", ok_idx)
    to_use = REC.find("timeout_fn(", else_idx)
    assert 0 < ok_idx < rel_use < else_idx < to_use, \
        "结构必须为:退款成功→release;else→write_timeout(退款成功才恢复可重试)"
    assert 'recovery_fn("refund"' in REC, "退款失败必须落耐久补偿工单(refund)"
    assert 'recovery_fn("state_fix"' in REC, "退款成功但状态恢复失败必须落 state_fix 工单"


def test_no_bare_except_pass_in_recovery():
    import re
    # 行首锚定的【代码】裸吞检测(不误伤 docstring 里提到 "except: pass" 的散文)
    assert not re.search(r"(?m)^\s*except\b[^\n]*:\s*pass\s*$", REC), "禁裸 except ...: pass 静默吞"
