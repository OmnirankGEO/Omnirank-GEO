"""W5 写作进化看板前端静态契约。

防回归:采纳样本已超过 30 时,第二层进度文字不能再直显 477/30,
否则会和后端"样本已够,等待蒸馏/缺对照"状态互相打架。
"""
from __future__ import annotations

from pathlib import Path


SRC = Path(__file__).resolve().parents[2] / "frontend/src/components/flywheel/EvolutionBoard.tsx"


def test_over_threshold_progress_copy_is_not_raw_denominator():
    text = SRC.read_text(encoding="utf-8")
    assert "sampleProgressLabel(adopted)" in text
    assert "{adopted}/30" not in text
    assert "已达标" in text

