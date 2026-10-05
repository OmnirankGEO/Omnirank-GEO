"""
判别性回归测试 · FIX 第2轮 · api/withdrawal_api.py

source-inspection 锁:直接读源码断言修复标志,回退则失败。
不 import server.py / 不依赖 DB。
"""

import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

WITHDRAWAL_API = Path(__file__).resolve().parents[2] / "api" / "withdrawal_api.py"


def _read_source() -> str:
    return WITHDRAWAL_API.read_text(encoding="utf-8")


def _submit_withdrawal_block(src: str) -> str:
    """截取 submit_withdrawal 函数体(到下一个 @router 装饰器为止)。"""
    start = src.index("async def submit_withdrawal(")
    rest = src[start:]
    # 下一个路由端点开始处
    nxt = rest.find("@router.get(\"/withdrawals\")")
    return rest if nxt == -1 else rest[:nxt]


# ==================== GEO-R1-CAN-152 · 提现确认通知不再 misroute 到 brand_id=0 ====================

def test_withdrawal_notification_uses_user_scoped_channel():
    """提现确认通知必须由数据库状态事务写强类型 outbox。"""
    block = _submit_withdrawal_block(_read_source())
    assert "create_withdrawal(" in block
    assert "create_user_notification" not in block, "API 层不得在事务提交后补写通知"
    db_source = (WITHDRAWAL_API.parents[1] / "db" / "withdrawal_db.py").read_text(encoding="utf-8")
    assert '"submitted": NotificationEventType.AGENT_SETTLEMENT_SUBMITTED' in db_source
    assert "enqueue_notification_event(" in db_source


def test_withdrawal_notification_keyed_by_submitting_user():
    """确认通知不得再由 API 自由拼接或写入 brand_id=0。"""
    block = _submit_withdrawal_block(_read_source())
    # 剥离注释行后校验代码,避免解释性注释里的 brand_id 字样干扰
    code_only = "\n".join(
        ln for ln in block.splitlines() if not ln.lstrip().startswith("#")
    )
    # 不得再出现 brand_id=0 的错误路由(代码层)
    assert "brand_id=0" not in code_only, (
        "提现确认通知不得再写 brand-scoped notifications(brand_id=0 不可达)"
    )
    assert "create_notification" not in code_only
