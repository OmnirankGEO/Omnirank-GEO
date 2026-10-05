"""[BUG-P3] charge_subscription_entitlement 无订阅+fallback_to_points=False 时 NameError 500 · 静态守护

根因:used/limit 仅在 `if sub:` 块内赋值;sub=None(无活跃订阅)且 fallback_to_points=False 时,
函数末尾 InsufficientQuotaError 的 f-string `配额已用满({used}/{limit})` 引用未定义变量 →
UnboundLocalError → 把"配额不足"业务错误变成 500(误导上层退费/补偿逻辑)。
修:函数开头预初始化 used=limit=0。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_used_limit_preinitialized():
    src = (ROOT / "middleware" / "subscription_billing.py").read_text(encoding="utf-8")
    s = src.find("async def charge_subscription_entitlement")
    assert s >= 0
    e = src.find("def _consume_entitlement(", s)
    func = src[s: e if e > 0 else len(src)]
    assert "used = 0" in func and "limit = 0" in func, \
        "未预初始化 used/limit → sub=None+fallback=False 时 UnboundLocalError 500"
    # init 必须在函数开头(cache 命中逻辑之前),即早于任何使用 used/limit 的分支
    assert func.find("used = 0") < func.find("# 1. cache 命中"), \
        "used/limit 初始化须在函数开头(早于所有 used/limit 引用)"
