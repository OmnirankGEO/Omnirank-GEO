"""运行时必需文件缺失的出声点(WO_272 · 2026-09-23)。

07-12 起镜像里 `knowledge/` 全是 cp437 乱码名,代码按正确名读不到,两处加载都**静默给空**
(`utils/knowledge_manager.py` 只 print 一行 Warning、`writing/distiller.py` 直接 return "")——
GEO 诊断与写作两个多月在没有知识库的状态下运行,没有任何东西红过。

这里只做两件事:`logger.error`(进容器日志,可 grep)+ 进程内计数(判据与排障可读)。
**不抛、不改调用方行为** —— 缺文件时调用方照旧降级,只是不再安静。
"""

import logging
import threading
from collections import Counter
from typing import Dict

_LOCK = threading.Lock()
_MISSING: Counter = Counter()


def report_missing(path, *, what: str, logger: logging.Logger) -> None:
    """记一次「运行时必需文件不存在」。`what` 用英文短语,日志里是 `<what> not found: <path>`。"""
    key = str(path)
    with _LOCK:
        _MISSING[key] += 1
        n = _MISSING[key]
    logger.error("[runtime-file-missing] %s not found: %s (本进程第 %d 次;功能在缺这份文件的状态下继续运行)",
                 what, key, n)


def missing_counts() -> Dict[str, int]:
    """本进程里各缺失路径被报告的次数(副本)。"""
    with _LOCK:
        return dict(_MISSING)
