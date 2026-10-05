"""
v3.8 · 反哺差异阈值门控 (CTO-13.3 · 2026-04-19 PLAN Q15 · C2)

一期策略（CTO-15.2 避坑 #1）:
  - 复用 tools/knowledge_layers.py LAYER_CONFIG 的 unique_key 精确去重
  - 不引入 embedding / Jaccard (成本 + 延迟考虑)
  - 已有 __source_url / term / url / insight 等 unique_key 覆盖主要字段

二期可选升级 (留给未来):
  - counter_consensus insight 做 Jaccard < 0.5 语义去重
  - user_voices_pool 长文本做 DashScope embedding 相似度 < 0.7

设计目标: 回答"哪些候选 items 在共享池里已经有了 (重复)"，不改变 merge_with_layers 的合并行为。
"""

import json
from typing import Any, Optional
from tools.knowledge_layers import LAYER_CONFIG


def _key_of(item: Any, unique_key: Optional[str]) -> str:
    """按 unique_key 或整体 JSON 序列化生成去重键"""
    if isinstance(item, dict) and unique_key:
        k = item.get(unique_key)
        if k is not None:
            return str(k)
    if isinstance(item, str):
        return item
    try:
        return json.dumps(item, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(item)


def filter_duplicates(
    field_name: str,
    new_items: list,
    existing_items: Optional[list] = None,
) -> dict:
    """
    从 new_items 中过滤掉已在 existing_items 里出现的。

    Args:
        field_name: industry_knowledge 字段名（如 'industry_jargon' / 'user_voices_pool'）
        new_items: 候选新 items
        existing_items: 共享池已有 items

    Returns:
        {
          "unique_new": [...],    # 应被反哺入池的新 items
          "duplicates": [...],    # 已在库，不需要反哺
          "unique_key_used": str|None,
        }
    """
    cfg = LAYER_CONFIG.get(field_name, {})
    unique_key = cfg.get("unique_key")

    existing_items = existing_items or []
    existing_keys = {_key_of(it, unique_key) for it in existing_items}

    unique_new = []
    duplicates = []
    seen_in_new = set()  # 防 new_items 内部重复
    for it in new_items:
        k = _key_of(it, unique_key)
        if k in existing_keys or k in seen_in_new:
            duplicates.append(it)
            continue
        seen_in_new.add(k)
        unique_new.append(it)

    return {
        "unique_new": unique_new,
        "duplicates": duplicates,
        "unique_key_used": unique_key,
    }
