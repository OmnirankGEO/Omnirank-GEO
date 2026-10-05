"""防御型 GEO · WP0 census ②:``ai_visibility.detail_table[].results[engine]`` 的键 census。

存在的理由 —— 一个真实的静默零
--------------------------------
``services/geo_observation/source_hooks.py::_extract_diagnosis_observations`` 读
``er["citations"]``,但 detail_table 的**全部** producer 只发 ``search_citations``。
键对不上不会报错,只会让付费诊断观测的 citations **恒空**:下游
``promotion._build_signal`` 的 ``source_count`` / ``citation_count`` 恒 0,
``quality_score_bps`` 恒少 domain 那 +2500 分。全绿、无日志、无告警。

这正是本仓反复踩的那一类:**「消费方读的键,生产方从来不发」**。
所以它不能只被修一次就算完 —— 得有一个能重跑的 census 盯着「生产者键集合」
和「消费者键集合」的差集,并且它是判据的分母来源。

两个集合各自怎么来
------------------
生产者集合 = ``tools/ai_visibility/ai_tester.py`` 里往单引擎结果字典写的键。
  真实形态两种,都要抓:
    · 字典字面量里的 ``"key": value``
    · ``visibility_result["key"] = value`` / ``visibility["key"] = value`` 赋值
消费者集合 = ``source_hooks._extract_diagnosis_observations`` 里从 ``er`` 取的键
  (``er.get("key")`` / ``er["key"]``)。

差集的含义
----------
  · 消费者读了、生产者从不发  → 🔴 **静默零**。这就是 CUR-07 的真因。
  · 生产者发了、消费者不读     → 🟡 信息在抽取层被丢弃(CUR-07 原始表述那一半)。

用法::

    python scripts/defgeo_census/detail_table_key_census.py
    python scripts/defgeo_census/detail_table_key_census.py --json
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PRODUCER_FILE = ROOT / "tools" / "ai_visibility" / "ai_tester.py"
CONSUMER_FILE = ROOT / "services" / "geo_observation" / "source_hooks.py"
CONSUMER_FUNC = "_extract_diagnosis_observations"

#: 生产者侧承载单引擎结果的变量名。取自 ai_tester 真实写法。
_RESULT_VARS = ("visibility_result", "visibility")

#: 这些键是 detail_table 结构本身的(问题/引擎层),不是单引擎结果字段,
#: 拿来算差集会制造噪声。
_STRUCTURAL_KEYS = frozenset({"question", "query", "results", "engine"})

_ASSIGN_RE = re.compile(
    r"(?:" + "|".join(_RESULT_VARS) + r")\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]\s*="
)


def producer_keys() -> dict[str, list[str]]:
    """生产者写进单引擎结果的键 → 出处行号。"""
    text = PRODUCER_FILE.read_text(encoding="utf-8", errors="replace")
    out: dict[str, list[str]] = {}
    rel = PRODUCER_FILE.relative_to(ROOT).as_posix()

    # ① 下标赋值
    for lineno, line in enumerate(text.splitlines(), start=1):
        for key in _ASSIGN_RE.findall(line):
            out.setdefault(key, []).append(f"{rel}:{lineno}")

    # ② 字典字面量。用 AST 而不是正则 —— 正则会把注释里的 "key": 也吃进来。
    #    只收「值里出现过 brand_detected / answer_summary 之类锚键」的字典,
    #    避免把全文件所有字典都当成单引擎结果。
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            literal_keys = [
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
            # 锚:单引擎结果字典必然含这两个之一。没有锚就不是我们要的字典。
            if not ({"brand_detected", "answer_summary"} & set(literal_keys)):
                continue
            for key in literal_keys:
                out.setdefault(key, []).append(f"{rel}:{node.lineno}")
    return {k: sorted(set(v)) for k, v in out.items() if k not in _STRUCTURAL_KEYS}


def consumer_keys() -> dict[str, list[str]]:
    """``_extract_diagnosis_observations`` 从 ``er`` 取的键 → 出处行号。

    用 AST 定位函数体,再在体内找 ``er.get("k")`` / ``er["k"]`` ——
    这样不会把文件里别处的 ``.get`` 算进来。
    """
    text = CONSUMER_FILE.read_text(encoding="utf-8", errors="replace")
    rel = CONSUMER_FILE.relative_to(ROOT).as_posix()
    tree = ast.parse(text)
    target = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == CONSUMER_FUNC),
        None,
    )
    if target is None:
        raise SystemExit(
            f"[census] 在 {rel} 找不到 {CONSUMER_FUNC} —— 函数被改名/搬走了,"
            "请更新 census,不要让它静默返回空集(零分母 = 恒绿)。"
        )
    out: dict[str, list[str]] = {}
    for node in ast.walk(target):
        key = None
        # er.get("k")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "er"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            key = node.args[0].value
        # er["k"]
        elif (isinstance(node, ast.Subscript)
              and isinstance(node.value, ast.Name) and node.value.id == "er"
              and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            key = node.slice.value
        if key and key not in _STRUCTURAL_KEYS:
            out.setdefault(key, []).append(f"{rel}:{node.lineno}")
    return {k: sorted(set(v)) for k, v in out.items()}


def build() -> dict:
    produced = producer_keys()
    consumed = consumer_keys()
    if not produced:
        raise SystemExit("[census] 生产者键集合为空 —— 抽取面坏了,拒绝返回零分母")
    if not consumed:
        raise SystemExit("[census] 消费者键集合为空 —— 抽取面坏了,拒绝返回零分母")
    return {
        "producer_file": PRODUCER_FILE.relative_to(ROOT).as_posix(),
        "consumer_file": CONSUMER_FILE.relative_to(ROOT).as_posix(),
        "consumer_func": CONSUMER_FUNC,
        "produced_keys": sorted(produced),
        "consumed_keys": sorted(consumed),
        "producer_sites": produced,
        "consumer_sites": consumed,
        # 🔴 非空 = 静默零缺陷:消费方读了一个生产方从来不发的键。
        "consumed_but_never_produced": sorted(set(consumed) - set(produced)),
        # 🟡 生产了但抽取层丢弃。
        "produced_but_not_consumed": sorted(set(produced) - set(consumed)),
    }


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    data = build()
    if "--json" in argv:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    print(f"生产者 {data['producer_file']} 写出 {len(data['produced_keys'])} 个键")
    print(f"消费者 {data['consumer_file']}::{data['consumer_func']} 读 {len(data['consumed_keys'])} 个键")
    bad = data["consumed_but_never_produced"]
    if bad:
        print("\n🔴 消费方读了、生产方从不发(静默零 · 不报错只恒空):")
        for key in bad:
            for site in data["consumer_sites"][key]:
                print(f"  · {key} @ {site}")
    else:
        print("\n✅ 消费方读的每个键,生产方都真的发(无静默零)")
    if data["produced_but_not_consumed"]:
        print("\n🟡 生产了但抽取层丢弃(信息在这里断链):")
        for key in data["produced_but_not_consumed"]:
            print(f"  · {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
