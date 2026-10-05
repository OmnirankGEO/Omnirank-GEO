# -*- coding: utf-8 -*-
"""§3 后端不许信前端 —— 非管理员传 billing_mode=platform 一律 403。

🔴 这条是「非管理员不能记平台账」的**真安全边界**。
前端少给一个入口不是边界:端点可以直接调。
(前端那条反向对照做不成真渲染 —— 实测非管理员访问 /monitoring 会被路由到 M3 工作台,
 监测词条表根本不渲染,没有可观测落点。所以它落在这里,不是省略。)
"""
from __future__ import annotations

import re

SRV = open("server.py", encoding="utf-8").read()


def _enable_endpoint_body() -> str:
    i = SRV.index('@app.post("/api/monitoring/keyword/{keyword_id}/enable")')
    j = SRV.index("\n@app.", i + 10)
    return SRV[i:j]


def _strip_prose(body: str) -> str:
    """剥掉 # 注释 —— 判据只打代码,不打讲代码的话(本轮已付过三次学费)。"""
    return "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))


def test_platform_requires_admin_403():
    body = _strip_prose(_enable_endpoint_body())
    assert 'if _billing_mode == "platform":' in body, "端点没有对 platform 单独把关"
    seg = body[body.index('if _billing_mode == "platform":'):]
    assert 'is_admin' in seg and '403' in seg, \
        "非管理员传 platform 没有被 403 拒 —— 端点可直接调,前端给的东西一律不可信"


def test_default_is_brand_owner_not_platform():
    """🔴 默认值锁:默认成平台承担 = 任何人随手一点都是平台成本敞口。"""
    sig = SRV[SRV.index("async def enable_keyword_monitor_subscription("):][:400]
    assert 'billing_mode: str = "brand_owner"' in sig, f"enable 端点默认值不是 brand_owner:{sig[:200]}"


def test_platform_fail_closed_when_account_unavailable():
    body = _strip_prose(_enable_endpoint_body())
    seg = body[body.index('if _billing_mode == "platform":'):]
    assert 'get_platform_direct_service_user_id' in seg and '503' in seg, \
        "平台账户不可用时没有 fail-closed —— 会开成「以为平台付、实际扣服务商」"


def test_value_whitelist_matches_migration_check():
    """取值白名单必须与迁移 CHECK 同口径 —— 两处不一致就会出现"过了端点被库拒"。"""
    body = _strip_prose(_enable_endpoint_body())
    assert '("brand_owner", "platform")' in body, "端点白名单口径变了"
    mig = open("scripts/migration_kms_billing_mode_2026_08_16.sql", encoding="utf-8").read()
    assert "billing_mode IN ('brand_owner', 'platform')" in mig, "迁移 CHECK 口径变了"
