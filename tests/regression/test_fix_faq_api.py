"""Regression lock for api/faq_api.py GEO fixes.

Source-inspection discriminative lock: reverting the fix makes these assertions
fail. No DB, no server import.

Covers:
  GEO-R10-CAN-027 · faq reindex fire-and-forget hardening:
    - strong-reference task tracking (防 GC)
    - bounded self-healing retry (缩短破坏性 clear-then-insert 的空/残缺窗口)
"""
import re
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FAQ_API = ROOT / "api" / "faq_api.py"
SRC = FAQ_API.read_text(encoding="utf-8")


def _region(src: str, start_marker: str, end_marker: str) -> str:
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def test_fix_marker_present():
    # 修复标记存在(回退即消失)
    assert "[GEO-R10-CAN-027]" in SRC


def test_reindex_task_strong_reference_prevents_gc():
    """create_task 返回值必须持强引用,否则任务可能在完成前被 GC(fire-and-forget 真 bug)。"""
    region = _region(SRC, "def _trigger_xiaobang_faq_reindex", "# ==========")
    # 不再丢弃 create_task 返回值
    assert "task = asyncio.create_task(" in region
    # 持强引用到模块级集合 + 完成后回收
    assert "_FAQ_REINDEX_TASKS.add(task)" in region
    assert "task.add_done_callback(_FAQ_REINDEX_TASKS.discard)" in region
    # 模块级强引用容器存在
    assert re.search(r"_FAQ_REINDEX_TASKS\s*:\s*set\s*=\s*set\(\)", SRC)


def test_reindex_has_bounded_retry():
    """破坏性 clear-then-insert 中途失败 → 有界重试自愈,缩短空/残缺窗口。"""
    assert "async def _run_faq_reindex_with_retry" in SRC
    region = _region(SRC, "async def _run_faq_reindex_with_retry", "def _trigger_xiaobang_faq_reindex")
    # 循环重试 + 退避
    assert "for attempt in range(1, _FAQ_REINDEX_MAX_RETRY" in region
    assert "await asyncio.sleep(" in region
    # 重试耗尽记 error(不再仅 warning 静默吞掉)
    assert "logger.error(" in region
    assert '_run_faq_reindex_with_retry()' in SRC  # 被 trigger 调用


def test_no_naked_fire_and_forget_left():
    """确保旧的裸 fire-and-forget(直接 create_task(trigger_reindex(...)) 丢返回值)已消失。"""
    assert "asyncio.create_task(trigger_reindex(scope=\"faq\"))" not in SRC


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
