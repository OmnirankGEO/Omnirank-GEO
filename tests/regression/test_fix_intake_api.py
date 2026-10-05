"""
判别性回归锁 · api/intake_api.py GEO 缺陷修复

主形态 = source-inspection: 直接读源码文本断言修复标志存在.
回退修复(改回 check-then-increment)则断言失败.
不依赖 DB / 不 import server.py.

覆盖 candidate:
  - GEO-R1-CAN-104: quota check-then-increment race → 原子占位取代读-判-写
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "api" / "intake_api.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _slice(marker_start: str, marker_end: str) -> str:
    i = SRC.find(marker_start)
    assert i != -1, f"未找到区段起点: {marker_start}"
    j = SRC.find(marker_end, i)
    if j == -1:
        j = len(SRC)
    return SRC[i:j]


# ==================== GEO-R1-CAN-104 ====================

def test_atomic_reserve_helper_exists():
    """必须存在原子占位 helper (带谓词的 conditional UPDATE)."""
    assert "def _reserve_ai_suggest_slot(" in SRC, "缺原子占位 helper _reserve_ai_suggest_slot"


def test_reserve_uses_conditional_predicate():
    """占位 UPDATE 必须带 ai_suggest_count < cap 谓词 + RETURNING (一步原子占坑)."""
    body = _slice("def _reserve_ai_suggest_slot(", "def _release_ai_suggest_slot(")
    assert "UPDATE intake_tokens" in body
    # 关键: 带谓词的条件自增, 而非无条件 +1
    assert re.search(r"ai_suggest_count\s*<\s*%s", body), "占位 UPDATE 缺 ai_suggest_count < cap 谓词"
    assert "RETURNING ai_suggest_count" in body, "占位 UPDATE 缺 RETURNING"


def test_handler_reserves_before_model_call():
    """ai-suggest handler: 名额占位必须发生在 generate_intake_draft 之前."""
    handler = _slice("async def api_public_ai_suggest(", "async def api_public_save_step(")
    idx_reserve = handler.find("_reserve_ai_suggest_slot(")
    idx_model = handler.find("await generate_intake_draft(")
    assert idx_reserve != -1, "handler 未调用原子占位 _reserve_ai_suggest_slot"
    assert idx_model != -1, "handler 未调用 generate_intake_draft"
    assert idx_reserve < idx_model, "占位必须在模型调用之前 (否则并发仍会越额烧钱)"


def test_handler_dropped_check_then_increment():
    """旧的 check-then-increment 组合必须移除: 不再有非条件的 increment_ai_suggest_count."""
    handler = _slice("async def api_public_ai_suggest(", "async def api_public_save_step(")
    assert "increment_ai_suggest_count(" not in handler, "handler 仍在用非原子 increment_ai_suggest_count (未修复竞态)"


def test_reserve_none_returns_429():
    """占位失败 (已达上限) 必须 429, 不静默降级."""
    handler = _slice("async def api_public_ai_suggest(", "async def api_public_save_step(")
    assert "reserved is None" in handler, "缺占位失败判定"
    # 429 就在 reserved is None 之后
    seg = handler[handler.find("reserved is None"):]
    assert "status_code=429" in seg[:400], "占位失败未返回 429"


def test_hard_failure_releases_slot():
    """模型硬失败必须归还名额 (保持 degraded 才计数语义)."""
    handler = _slice("async def api_public_ai_suggest(", "async def api_public_save_step(")
    assert "_release_ai_suggest_slot(" in handler, "硬失败路径未归还名额"
    assert "def _release_ai_suggest_slot(" in SRC, "缺 _release_ai_suggest_slot helper"


def test_cid_marker_present():
    """修复处应带 candidate 标记, 便于回溯."""
    assert "GEO-R1-CAN-104" in SRC


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
