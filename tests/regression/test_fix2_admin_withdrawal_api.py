"""
判别性回归测试 · Fix2 · api/admin_withdrawal_api.py
source-inspection 锁：断言修复标志存在，回退则失败。
不 import server.py / 不依赖 DB。

覆盖:
- GEO-R1-CAN-151 (reject_withdrawal 通知误投 brand_id=0 → 改用户级 user_notifications)
- GEO-R1-CAN-150 (mark_paid 通知误投 brand_id=0 → 改用户级 user_notifications)
"""

import re
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SRC = Path(__file__).resolve().parents[2] / "api" / "admin_withdrawal_api.py"
TEXT = SRC.read_text(encoding="utf-8")


def _section(marker: str) -> str:
    """截取某个端点函数体（从签名到下一个 @router 或文件尾）。"""
    idx = TEXT.index(marker)
    tail = TEXT[idx:]
    nxt = tail.find("\n@router", 1)
    return tail if nxt == -1 else tail[:nxt]


def test_no_brandzero_create_notification_in_reject_and_paid():
    """回退锁：代码（非注释）不得再出现 brand_id=0 的旧误投通知调用。"""
    code_lines = [ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "brand_id=0" not in code, "仍存在 brand_id=0 通知误投代码（未修复）"
    assert "from db.notifications import create_notification" not in code, \
        "仍从 db.notifications 引入 create_notification（brand 级误投通道）"


def test_reject_uses_user_scoped_notification():
    """GEO-R1-CAN-151: 驳回通知由状态写入函数同事务投递到 outbox。"""
    body = _section("async def reject_withdrawal")
    assert "admin_reject(" in body
    assert "create_user_notification" not in body, "API 层不得在事务外补写通知"
    db_source = (Path(__file__).resolve().parents[2] / "db" / "withdrawal_db.py").read_text(encoding="utf-8")
    assert '"rejected": NotificationEventType.AGENT_SETTLEMENT_REJECTED' in db_source
    assert "enqueue_notification_event(" in db_source


def test_paid_uses_user_scoped_notification():
    """GEO-R1-CAN-150: 打款通知由状态写入函数同事务投递到 outbox。"""
    body = _section("async def mark_paid")
    assert "admin_mark_paid(" in body
    assert "create_user_notification" not in body, "API 层不得在事务外补写通知"
    db_source = (Path(__file__).resolve().parents[2] / "db" / "withdrawal_db.py").read_text(encoding="utf-8")
    assert '"paid": NotificationEventType.AGENT_SETTLEMENT_PAID' in db_source
    assert "enqueue_notification_event(" in db_source


def test_transactional_outbox_helper_accepts_existing_cursor():
    """通知 helper 必须接受业务事务 cursor，不能自行连接/commit。"""
    source = (Path(__file__).resolve().parents[2] / "services" / "notification_outbox.py").read_text(encoding="utf-8")
    assert "def enqueue_notification_event(\n    cursor," in source
