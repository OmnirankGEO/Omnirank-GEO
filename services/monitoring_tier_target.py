"""套餐档位 → 目标出现率 · 单点(SSOT 派生 + legacy 别名)。

[WO §8 快修 2026-08-08]

## 缺陷不是"打错一个字",是"三份拷贝 + 一份少了键"

档位码的 SSOT 是 `api/selection_api.TIER_CONFIG`,它用的是 **`flagship`**
(`entry` / `standard` / `flagship`,对应 50% / 65% / 75%)。
但"档位 → 目标检出率"这张表在仓里被抄了**三份**,其中两份只认 legacy 的 `premium`:

=================================  ==========================  =========
位置                               键                          结果
=================================  ==========================  =========
`api/monitoring_api.TIER_TARGET`   entry/standard/**premium**  🔴 缺 flagship
`server.py` 报告提示词里那份内联表  entry/standard/**premium**  🔴 缺 flagship
`db/monitoring_db._TIER_TARGET_MAP` entry/standard/premium/**flagship** ✅ 两个都有
=================================  ==========================  =========

前两份对 `tier='flagship'` 的品牌走 `.get(code, TIER_TARGET["standard"])`
→ **静默按标准版 65% 算**,而客户买的是旗舰版 75%。

## 🔴 生产数据说明:直接把 premium 改名成 flagship 是**错的**

2026-08-08 只读取证(`quotes.tier` 全量):

===========  =====  ============================
tier         报价数  有监测任务的品牌数
===========  =====  ============================
standard      371    25
entry          16     8
**flagship**    4     **0**
**premium**     1     **1**  ← 还活着,而且正在被监测
===========  =====  ============================

所以:
  · 只改名 → **修好 4 个 flagship 报价(当前 0 个在监测)**,
    但把**唯一一个正在被监测的 premium 品牌**从 75% 打回 65% —— 净负。
  · 正确做法 = **两个都认**,`flagship` 为规范码、`premium` 为 legacy 别名。

这也不是我发明的写法 —— 仓里已有两处同样的"两个都认":
`db/monitoring_db._TIER_TARGET_MAP`(同时列 premium 与 flagship)、
`services/quote_numeric_repair.py`(把 flagship/premium/pro/旗舰版/高级版 当同义词)。
本模块把它收成**一处**,并且**从 SSOT 派生数值**,不再手抄第四份 50/65/75。
"""
from __future__ import annotations

from typing import Any, Dict

#: legacy 档位码 → 规范码。
#: 🔴 只做**收敛**(老码 → 新码),绝不反向:写库一律写规范码。
#: 生产实证 2026-08-08:`quotes.tier` 里 `premium` 还有 1 条且正在被监测,
#: 所以这条别名**不能删**,删了那个客户的目标出现率会从 75% 掉回 65%。
LEGACY_TIER_ALIASES: Dict[str, str] = {
    "premium": "flagship",
}

#: 取不到 / 认不出时的兜底档(与被替换的三处旧实现行为一致,不引入新行为)。
DEFAULT_TIER_CODE = "standard"


def canonical_tier_code(raw: Any) -> str:
    """把任意来源的档位码收敛成规范码(`entry` / `standard` / `flagship`)。"""
    code = str(raw or "").strip().lower()
    if not code:
        return DEFAULT_TIER_CODE
    code = LEGACY_TIER_ALIASES.get(code, code)
    return code if code in _tier_config() else DEFAULT_TIER_CODE


def is_known_tier_code(raw: Any) -> bool:
    """这个码**认得出来**吗(含 legacy 别名)。

    🔴 存在的理由不是"好看":`server.py` 的报告提示词那一处,兜底档是 **entry 而不是
    standard** —— CTO-15.23 2026-05-06 P1 专门这么改的,理由是"fallback standard
    会无脑升档、误导客户"。调用方需要能区分「认得出」与「认不出」,
    才能各自保留自己的兜底语义,而不是被本模块统一成 standard。
    """
    code = str(raw or "").strip().lower()
    if not code:
        return False
    return LEGACY_TIER_ALIASES.get(code, code) in _tier_config()


def tier_target(raw: Any) -> Dict[str, Any]:
    """返回 ``{"tier", "tier_name", "target_rate"}``。

    ``tier`` 是**规范码**(legacy `premium` 会被收敛成 `flagship`);
    ``target_rate`` 从 SSOT 的 ``ai_probability``("75%")解析,不手抄数字。
    """
    code = canonical_tier_code(raw)
    cfg = _tier_config().get(code) or {}
    return {
        "tier": code,
        "tier_name": cfg.get("label") or "标准版",
        "target_rate": _rate_of(cfg),
    }


def tier_target_rates() -> Dict[str, int]:
    """规范码 + legacy 别名 → 目标出现率。给"要一整张表"的调用点用。"""
    table = {code: _rate_of(cfg) for code, cfg in _tier_config().items()}
    for legacy, canonical in LEGACY_TIER_ALIASES.items():
        if canonical in table:
            table[legacy] = table[canonical]
    return table


def _tier_config() -> Dict[str, Dict[str, Any]]:
    """SSOT = ``api.selection_api.TIER_CONFIG``(老板 2026-06-05 定稿的那一份)。

    延迟 import:`api.selection_api` 在 import 期会建 FastAPI router,
    模块级 import 会把这条依赖钉进 `db`/`services` 层的加载顺序里。
    """
    from api.selection_api import TIER_CONFIG

    return TIER_CONFIG


def _rate_of(cfg: Dict[str, Any]) -> int:
    """`ai_probability`("75%")→ 75。

    🔴 这里刻意**解析** SSOT 的字符串,而不是另写一份 {entry:50, standard:65,
    flagship:75} —— 那就是第四份拷贝,也就是这个工单本身的病因。
    解析不出来时回落到 SSOT 的 standard 档,不编数字。
    """
    raw = str(cfg.get("ai_probability") or "").strip().rstrip("%")
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        fallback = str(
            (_tier_config().get(DEFAULT_TIER_CODE) or {}).get("ai_probability") or "65"
        ).strip().rstrip("%")
        try:
            return int(float(fallback))
        except (TypeError, ValueError):  # pragma: no cover - SSOT 同时坏掉
            return 65
