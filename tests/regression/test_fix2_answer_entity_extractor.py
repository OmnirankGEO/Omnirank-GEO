"""判别性回归锁 · Fix2 · services/research_monitor/answer_entity_extractor.py

source-inspection 为主(读源码断言修复标志,回退则失败),不 import server.py / 不依赖 DB。
覆盖:
- GEO-R1-CAN-011: 答案超预算截断时 fact.quality_flag 标 'truncated'(loss-aware)。
- GEO-R1-CAN-012: rebuild_answer_entities 顶层错误返回携带 status='failed'(供 bridge 归并不误判 success)。
skipped(留人工·断言仍是当前行为的现状锁):
- GEO-R7-CAN-003 / GEO-R5-CAN-009: 修复位于 db/research_answer_entity_db.py(本轮单文件范围外)。
"""

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TARGET = (
    Path(__file__).resolve().parents[2]
    / "services" / "research_monitor" / "answer_entity_extractor.py"
)
_SRC = _TARGET.read_text(encoding="utf-8")


def test_source_compiles():
    ast.parse(_SRC)  # 语法锁:回退成语法错会直接失败


# ---- GEO-R1-CAN-011 截断标记 ----

def test_can011_truncation_tracked():
    # 必须真检测截断(len > _MAX_ANSWER_CHARS),而非无脑截断丢弃
    assert re.search(r"answer_truncated\s*=\s*len\(\s*raw_answer\s*\)\s*>\s*_MAX_ANSWER_CHARS", _SRC), \
        "回退:未按 _MAX_ANSWER_CHARS 计算 answer_truncated"


def test_can011_truncated_flag_on_fact():
    # 截断且非 degraded → fact 落 'truncated' 标记;degraded 语义不被覆盖
    assert re.search(r"if\s+answer_truncated\s+and\s+quality_flag\s*!=\s*[\"']degraded[\"']", _SRC), \
        "回退:未在截断且非 degraded 时区分 fact_flag"
    assert re.search(r"fact_flag\s*=\s*[\"']truncated[\"']", _SRC), \
        "回退:未把 fact 标记为 'truncated'"
    # fact 必须使用 fact_flag 而不是直接透传原 quality_flag
    assert re.search(r"_build_fact\([^)]*fact_flag\)", _SRC), \
        "回退:_build_fact 未接收 fact_flag(截断标记未落到 fact)"


def test_can011_entities_gate_unchanged():
    # entities 门控仍用 distiller 原始 quality_flag,不因截断改变写实体行为
    assert 'if quality_flag != "degraded":' in _SRC, \
        "回退:entities 门控被误改(应保持用 distiller 原始 quality_flag)"


# ---- GEO-R1-CAN-012 错误传播 ----

def test_can012_error_return_carries_failed_status():
    # 定位 rebuild_answer_entities 顶层 except 的 error 返回,断言含 status='failed'
    m = re.search(r"return\s*\{\s*[\"']mode[\"']\s*:\s*[\"']error[\"'][^}]*\}", _SRC, re.DOTALL)
    assert m, "未找到 mode:'error' 返回字典"
    err_return = m.group(0)
    assert re.search(r"[\"']status[\"']\s*:\s*[\"']failed[\"']", err_return), \
        "回退:error 返回未携带 status='failed' → bridge setdefault 会误判为 success"


def test_can012_status_key_present_module_level():
    # 交叉锁:整文件必须出现 status:failed(防止上面正则被绕过)
    assert re.search(r"[\"']status[\"']\s*:\s*[\"']failed[\"']", _SRC)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("ALL GREEN")
