"""
判别性回归锁 — auth/sms_service.py

Finding GEO-R1-CAN-036 (P2, nonatomic-attempt-counter):
verify_sms_code 原先 read-check-increment-write 非原子 + _update_attempts 写绝对值
(last-write-wins)，并发错误猜测可绕过 3 次上限。修复：单事务 FOR UPDATE 行锁 +
原子条件自增 (attempts = attempts + 1)。

本测试为 source-inspection 判别锁：回退修复则断言失败。不依赖 DB / 不 import server。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "auth" / "sms_service.py").read_text(encoding="utf-8")


def _slice(text, start_marker, end_marker=None):
    i = text.index(start_marker)
    j = text.index(end_marker, i) if end_marker else len(text)
    return text[i:j]


def test_atomic_verify_helper_exists():
    # 新增原子校验辅助函数
    assert "def _verify_db_atomic(" in SRC


def test_verify_uses_for_update_row_lock():
    # [GEO-R1-CAN-036] 行锁串行化同一 phone 的并发校验
    body = _slice(SRC, "def _verify_db_atomic(", "def verify_sms_code(")
    assert "FOR UPDATE" in body, "缺少 SELECT ... FOR UPDATE 行锁"


def test_verify_uses_atomic_conditional_increment():
    # 原子自增而非写绝对值
    body = _slice(SRC, "def _verify_db_atomic(", "def verify_sms_code(")
    assert "attempts = attempts + 1" in body, "缺少原子自增 attempts=attempts+1"
    assert "RETURNING attempts" in body, "自增未用 RETURNING 读回真实值"


def test_finding_marker_present():
    assert "GEO-R1-CAN-036" in SRC


def test_verify_sms_code_calls_atomic_path_first():
    # verify_sms_code 先走原子路径，DB 可用时不再落到非原子 record 分支
    body = _slice(SRC, "def verify_sms_code(", "def cleanup_expired(")
    assert "_verify_db_atomic(phone, code, purpose)" in body
    # 原子路径调用必须位于 _get_code(phone) 之前（DB 优先）
    assert body.index("_verify_db_atomic") < body.index("_get_code(phone)")


def test_no_absolute_write_in_atomic_path():
    # 原子分支内禁止再出现绝对值写 SET attempts = %s
    body = _slice(SRC, "def _verify_db_atomic(", "def verify_sms_code(")
    assert "SET attempts = %s" not in body


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
