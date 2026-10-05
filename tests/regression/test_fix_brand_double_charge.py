"""判别性回归 · GEO-R6-CAN-004 brand_fill 双扣

_search_and_fill 走 HTTP loopback 到 /api/diagnosis/autofill(内层扣一次 brand_fill),
外层 _finalize 原会再扣一次 → 同一操作双扣 brand_fill。
修复:_search_and_fill 成功标记 _billed_via_loopback,_finalize 见标记不再扣。

源码判别锁:回退修复(去掉标记或去掉 _finalize 的跳过)则断言失败。
跑: pytest tests/regression/test_fix_brand_double_charge.py -v
"""
from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "api" / "brand_api.py").read_text(encoding="utf-8")


def test_search_and_fill_marks_loopback_billed():
    assert '"_billed_via_loopback": True' in SRC, \
        "R6-CAN-004: _search_and_fill 成功须标记 _billed_via_loopback(内层已扣)"


def test_finalize_skips_when_loopback_billed():
    # _finalize 必须读取标记并在其为真时跳过再次扣费
    i = SRC.find("async def _finalize(")
    assert i != -1
    body = "\n".join(SRC[i:].splitlines()[:12])
    assert "_billed_via_loopback" in body, "R6-CAN-004: _finalize 须检查 loopback 标记"
    assert "not _billed" in body, "R6-CAN-004: _finalize 须在已 loopback 扣费时跳过 _commit_brand_fill_charge"
    assert "_commit_brand_fill_charge" in body
