from pathlib import Path


# [开源 E3 · B2 · 2026-09-28] 社媒意图引擎 / 输出模式 / 内容工坊随包删除,守它们的 4 格退役。


def test_step22_geo_transparent_pricing_not_modified():
    diff = Path("tools/transparent_pricing.py").read_text(encoding="utf-8")

    assert "透明定价系统" in diff
