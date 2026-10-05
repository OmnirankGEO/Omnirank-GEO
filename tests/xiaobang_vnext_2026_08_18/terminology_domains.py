"""术语四语义域分类器 —— **本文件不再持有实现**(R3-P7 ①)。

实现搬到了运行时模块 :mod:`services.kb_terminology_gate`,理由写在那份 docstring 里:
分类器留在 ``tests/`` 就只在 CI 里存在,而 KB 的写入路径在生产里天天开着
(管理员改一条 FAQ → 后台重建 → 小榜下一句就照着念),判据拦不住运行时发生的事。

这里保留一层 **re-export**,让既有判据的 import 路径不动,同时保证全仓
**只有一份实现** —— 抄第二份的话两边必然各自漂移,而漂移方向一定是
「线上那份更松」。判据 ``test_terminology_module_is_a_reexport_not_a_second_copy``
钉住这件事(它比对两个模块的函数对象**是同一个**,不是"行为看起来一样")。
"""

from __future__ import annotations

from services.kb_terminology_gate import (  # noqa: F401
    CASH_ANCHORS,
    CASH_POSSESSORS,
    HARD_FORBIDDEN_COMPOUNDS,
    LEGACY_UNIT,
    POOL_NAMES,
    PRICING_ANCHORS,
    QUOTA_EXEMPT,
    RULING_PATH,
    RULING_SHA256,
    STALE_ENTRY_NAMES,
    KbTerminologyViolation,
    all_violations,
    assert_chunk_rows_clean,
    assert_kb_text_clean,
    cash_semantic_violations,
    clauses,
    dead_scheme_violations,
    domain12_violations,
    fixes_for,
    kb_index_advisories,
    kb_index_residue,
    kb_write_violations,
    ruling_fingerprint_ok,
)

__all__ = [
    "CASH_ANCHORS", "CASH_POSSESSORS", "HARD_FORBIDDEN_COMPOUNDS", "LEGACY_UNIT",
    "POOL_NAMES", "PRICING_ANCHORS", "QUOTA_EXEMPT", "RULING_PATH", "RULING_SHA256",
    "STALE_ENTRY_NAMES", "KbTerminologyViolation", "all_violations",
    "assert_chunk_rows_clean", "assert_kb_text_clean", "cash_semantic_violations",
    "clauses", "dead_scheme_violations", "domain12_violations", "fixes_for",
    "kb_index_advisories", "kb_index_residue", "kb_write_violations",
    "ruling_fingerprint_ok",
]
