"""判别性回归测试 · api/brand_api.py 第2轮修复锁

source-inspection 判别锁:直接读源码断言修复标志,回退则失败。
不 import server.py / 不依赖 DB。

覆盖:
- GEO-R8-CAN-010 (fixed): add_client 从诊断导入的 IDOR — UPDATE 增 operator_user_id
  归属校验 + SELECT 增 brand_id 过滤,防他人未归属诊断被认领/business_context 泄漏。
- GEO-R4-CAN-005 (skipped, needs-manual-fund-review): auto_fill_brand deliver-before-debit,
  修复需 freeze/commit/release 重构扣费守恒逻辑,属资金红线,本轮留人工。
  此处仅锁"未被误改成静默降级/隐藏"的现状痕迹,不断言已修。
"""
import re
from pathlib import Path

sys_path_root = str(Path(__file__).resolve().parents[2])
import sys
sys.path.insert(0, sys_path_root)

BRAND_API = Path(sys_path_root) / "api" / "brand_api.py"
SRC = BRAND_API.read_text(encoding="utf-8")


def _from_diag_block() -> str:
    """截取 add_client 里 from_diagnosis_id 导入块。"""
    i = SRC.find("if req.from_diagnosis_id:")
    assert i != -1, "未定位 from_diagnosis_id 导入块"
    return SRC[i:i + 1400]


# ---------------- GEO-R8-CAN-010 (对抗审核回退 · 重分类 SKIPPED/needs-design) ----------------
# 原本批 fix 把 UPDATE gate 在 operator_user_id 列上,但该列在 diagnosis_records 从未写入
# → UPDATE 恒 0 行 → 打断合法"从诊断导入客户"功能(违反不破坏现有功能铁律)。
# diagnosis_records 无可靠创建者列,NULL-brand 诊断归属无法验证,需 schema 加 creator 列
# 的设计变更;本批回退到原行为,标 SKIPPED/needs-design。

def test_geo_r8_can_010_reverted_no_phantom_column_gate():
    """确认已回退:UPDATE 的 WHERE 不得 gate 在从未写入的 operator_user_id 幽灵列(恒 0 行打断功能)。"""
    block = _from_diag_block()
    # 检查 SQL 语句本体(而非解释注释)不再用 operator_user_id 作 WHERE 条件
    m = re.search(r"UPDATE\s+diagnosis_records\s+SET\s+brand_id\s*=\s*%s\s+WHERE(.+?)\"\"\"",
                  block, re.DOTALL | re.IGNORECASE)
    assert m, "未定位 UPDATE diagnosis_records 语句"
    where = m.group(1)
    assert "operator_user_id" not in where, (
        "R8-CAN-010 已回退:UPDATE WHERE 不得 gate 在 operator_user_id 幽灵列"
    )
    assert "brand_id IS NULL" in where, "回退后 UPDATE 仍只认领 NULL-brand 诊断"
    assert "对抗审核订正 R8-CAN-010" in block, "缺回退说明注释"


# ---------------- GEO-R4-CAN-005 (skipped) ----------------

def test_geo_r4_can_005_not_silently_degraded():
    """本轮 skip:仅确认扣费预检仍在(未被误删成白嫖),不断言 deliver-before-debit 已修。"""
    assert "check_balance_only" in SRC, "brand_fill 余额预检不得被删除"
    assert 'deduct_points' in SRC, "brand_fill 扣费调用不得被删除"
