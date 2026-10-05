"""
判别性回归测试 · 第2轮 · api/managed_campaign_api.py

以 source-inspection 断言锁为主(不 import server.py / 不依赖 DB):
读源码断言修复标志存在,一旦回退即失败。

覆盖:
- GEO-R1-CAN-069 (fixed): post_confirm_recharge 的 UniqueViolation 分支现在会调用
  _refund_managed_campaign_failure 退款(此前直接抛 409 不退款,失败方净扣钱)。
- GEO-R1-CAN-072 / GEO-R1-CAN-071 / GEO-R6-CAN-015 / GEO-R1-CAN-070 (skipped):
  记录为人工复核项,此处仅做“未被误改成危险实现”的存在性说明,不做修复断言。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TARGET = Path(__file__).resolve().parents[2] / "api" / "managed_campaign_api.py"


def _read():
    return TARGET.read_text(encoding="utf-8")


def _confirm_recharge_block(src: str) -> str:
    """截取 post_confirm_recharge 函数体(到下一个 @router 装饰器为止)。"""
    start = src.index("async def post_confirm_recharge")
    rest = src[start:]
    nxt = rest.find("\n@router.")
    return rest if nxt == -1 else rest[:nxt]


def test_uniqueviolation_branch_now_refunds():
    """GEO-R1-CAN-069: UniqueViolation 分支必须调用退款补偿,否则回退。"""
    src = _read()
    block = _confirm_recharge_block(src)

    # 定位 UniqueViolation except 分支
    uv_idx = block.index("except psycopg2.errors.UniqueViolation")
    # 该分支到下一个 except/return-顶层 之间的片段(取到下一个 'except Exception')
    after = block[uv_idx:]
    generic_idx = after.index("except Exception as e:")
    uv_branch = after[:generic_idx]

    assert "_refund_managed_campaign_failure" in uv_branch, (
        "GEO-R1-CAN-069 回退: UniqueViolation 分支未调用 _refund_managed_campaign_failure,"
        "并发失败方会被扣费却不退款"
    )
    # 必须带修复标记注释
    assert "GEO-R1-CAN-069" in uv_branch, "缺少 [GEO-R1-CAN-069] 修复标记注释"


def test_uniqueviolation_refund_feature_code_matches_charge():
    """退款 feature_code 必须与扣费一致(managed_campaign_recharge),否则退错池。"""
    src = _read()
    block = _confirm_recharge_block(src)
    uv_idx = block.index("except psycopg2.errors.UniqueViolation")
    after = block[uv_idx:]
    uv_branch = after[: after.index("except Exception as e:")]
    assert 'feature_code="managed_campaign_recharge"' in uv_branch, (
        "UniqueViolation 退款 feature_code 与扣费不一致"
    )


def test_uniqueviolation_still_returns_409():
    """修复后仍应返回 409(重复语义),不能被误改成 200/500。"""
    src = _read()
    block = _confirm_recharge_block(src)
    uv_idx = block.index("except psycopg2.errors.UniqueViolation")
    after = block[uv_idx:]
    uv_branch = after[: after.index("except Exception as e:")]
    assert "status_code=409" in uv_branch, "UniqueViolation 分支应仍返回 409 冲突语义"


def test_refund_helper_still_present():
    """补偿 helper 未被误删。"""
    src = _read()
    assert "async def _refund_managed_campaign_failure" in src


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
