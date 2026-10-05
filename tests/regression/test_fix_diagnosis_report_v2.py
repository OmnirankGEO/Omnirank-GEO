"""Regression lock for services/diagnosis_report_v2.py

GEO-R10-CAN-009 (P3 · funnel-fallback-zero-passes-persist-gate):
当 calculate_funnel_score 抛异常时,except 兜底会给出 total_score=0 / level='隐形级'
(非 None),旧 gate `funnel_total is not None and funnel_level is not None and not error`
会误判 True → 把 0 分/隐形级回写 diagnosis_records.total_score/level 与 brands.latest_score,
覆盖上一次有效总分。

修复:兜底 funnel_result 打 `calc_failed=True` 典型标记,write_funnel_columns gate 显式识别
该标记 → 跳过回写,保留最后一次有效规范分。

主形态 = source-inspection 判别锁(读源码文本断言修复标志存在,回退则 fail)。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "services" / "diagnosis_report_v2.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def test_fallback_funnel_result_marks_calc_failed():
    """漏斗兜底 dict 必须带 calc_failed=True 典型标记(回退修复则该标记消失 → fail)。"""
    # 定位漏斗降级 except 块
    assert "漏斗评分计算失败" in SRC, "找不到漏斗降级 except 块 · 结构变了需复核"
    block_start = SRC.index("漏斗评分计算失败")
    block = SRC[block_start:block_start + 900]
    assert '"calc_failed": True' in block, (
        "GEO-R10-CAN-009 回退:漏斗算分失败兜底缺 calc_failed=True 标记,"
        "0 分/隐形级会再次污染持久化 gate"
    )


def test_persist_gate_honors_calc_failed():
    """write_funnel_columns gate 必须显式排除 calc_failed(否则 0 分仍会被回写)。"""
    assert "write_funnel_columns = (" in SRC, "找不到 write_funnel_columns gate"
    gate_start = SRC.index("write_funnel_columns = (")
    gate_block = SRC[gate_start:gate_start + 300]
    assert "not funnel_calc_failed" in gate_block or "calc_failed" in gate_block, (
        "GEO-R10-CAN-009 回退:持久化 gate 未识别 calc_failed,0 分会覆盖有效总分"
    )
    # funnel_calc_failed 必须来自 funnel_score.get('calc_failed')
    assert "funnel_score.get(\"calc_failed\")" in SRC or "funnel_score.get('calc_failed')" in SRC, (
        "calc_failed 判定应读自 funnel_score dict"
    )


def test_behavior_gate_false_when_calc_failed():
    """纯逻辑复刻 gate:calc_failed 时即便 total/level 非 None 也不回写。"""
    def gate(funnel_score, v2_error):
        funnel_total = funnel_score.get("total_score") if isinstance(funnel_score, dict) else None
        funnel_level = funnel_score.get("level") if isinstance(funnel_score, dict) else None
        funnel_calc_failed = bool(isinstance(funnel_score, dict) and funnel_score.get("calc_failed"))
        return (
            funnel_total is not None
            and funnel_level is not None
            and not v2_error
            and not funnel_calc_failed
        )

    # 兜底(算分失败)形态:total=0 / level=隐形级 / calc_failed=True → 不写
    fallback = {"total_score": 0, "level": "隐形级", "calc_failed": True}
    assert gate(fallback, None) is False, "算分失败兜底不应回写 DB 列"

    # 正常有效分 → 写
    ok = {"total_score": 73, "level": "领先级"}
    assert gate(ok, None) is True, "有效漏斗分应正常回写"

    # 有效 0 分(真实全未命中,非算分异常)仍写(向后兼容)
    legit_zero = {"total_score": 0, "level": "隐形级"}
    assert gate(legit_zero, None) is True, "真实 0 分(无 calc_failed)保持原回写行为"


def test_gate_regex_shape_intact():
    """确认 gate 仍是 4 条 AND 布尔组合(防止有人改成短路吞掉 calc_failed)。"""
    m = re.search(r"write_funnel_columns = \((.*?)\n    \)", SRC, re.DOTALL)
    assert m, "gate 结构不可解析"
    body = m.group(1)
    assert "funnel_total is not None" in body
    assert "funnel_level is not None" in body
    assert "not v2_result.get(\"error\")" in body
    assert "not funnel_calc_failed" in body
