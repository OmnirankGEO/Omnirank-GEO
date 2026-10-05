"""P1-4 · 文章链引擎定向 —— 复用 geo_douyin 发布面先例,非新设计(2026-08-14)。

先例:`services/geo_douyin/ranking_router.py:84-140` —— `TARGET_ENGINE` 快照 +
`target_face()` 落 meta + 「参数继承、target 取当前值」。差异只有一点:
抖音图文的目标面是**产品事实**(服务端常量,不收前端传值);文章链的目标面是
**每个写作项目的选项**(服务商按客户策略定),所以这里是 quote 级设定:

  - 存储:`quotes.target_engine`(additive 列,NULL/空 = 不定向);
  - 继承:生成/重生成时从 quote 现值读取注入 topic;
  - 快照:每篇 lineage 记录生成当时的值;
  - 分类:A1 —— 空值/非法值走现行为,如实记录不硬编。

🔴 [R2-9 2026-08-15] 本模块**不再是第二份手写引擎目录**:可定向引擎键从
监测侧 SSOT(`services.monitoring_lineage._PLATFORM_CONTRACT`)**派生**,
写作层只保留「中文显示名」适配器。SSOT 增删引擎 → 写作层自动跟随
(新引擎无显示名时回落键名,不丢);两次发布之间监测/写作引擎不可能漂移
—— 漂移在源头就不存在(单一目录,不是两份目录的相等测试)。
"""
from __future__ import annotations

from typing import Final

#: 显示名**适配器**(不是目录):只做键 → 中文名的展示映射。
#: SSOT 里存在而这里没有的键,展示回落键名 —— 引擎不丢,只是名字不那么好看。
_DISPLAY_LABELS: Final[dict[str, str]] = {
    "dashscope": "千问",
    "deepseek": "DeepSeek",
    "kimi": "Kimi",
    "doubao": "豆包",
}

#: 常见别名 → 规范键(前端旧值/中文输入的归一;不认识的一律归空 = 不定向)。
_ALIASES: Final[dict[str, str]] = {
    "qwen": "dashscope",
    "千问": "dashscope",
    "qwen3-max": "dashscope",
    "moonshot": "kimi",
    "豆包": "doubao",
}


def _ssot_engine_keys() -> tuple[str, ...]:
    """现役引擎键,唯一来源 = 监测侧 SSOT。读不到 → 空元组(定向整体降级,A1)。"""
    try:
        from services.monitoring_lineage import _PLATFORM_CONTRACT

        return tuple(_PLATFORM_CONTRACT.keys())
    except Exception:
        return ()


def available_target_engines() -> dict[str, str]:
    """可定向引擎 {键: 显示名} —— 键集合从 SSOT 现取派生,每次调用现算。"""
    return {key: _DISPLAY_LABELS.get(key, key) for key in _ssot_engine_keys()}


def normalize_target_engine(raw: object) -> str:
    """归一目标引擎;空/非法 → ``""``(= 不定向,走现行为 —— A1)。"""
    key = str(raw or "").strip().lower()
    if not key:
        return ""
    engines = available_target_engines()
    if key in engines:
        return key
    alias = _ALIASES.get(key, "")
    return alias if alias in engines else ""


def target_engine_label(key: str) -> str:
    """规范键 → 用户可读名;空/非法 → ``""``。"""
    normalized = normalize_target_engine(key)
    return available_target_engines().get(normalized, "") if normalized else ""
