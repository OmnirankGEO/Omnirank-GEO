"""判别性回归 · auth/middleware.py 红线修复(老板批准 3 项)

- GEO-R1-CAN-061: 微信退款回调 /api/wallet/wechat-refund-callback 须在 PUBLIC_PATHS(否则 401 退款不到账)
- GEO-R1-CAN-031: 后缀白名单仅 GET/HEAD 放行(堵 mutating 绕过鉴权)
- GEO-R1-CAN-097: 软刷新异常兜底须剥离 is_admin(抖动窗口内不放大特权)

源码判别锁:回退任一修复对应断言失败。
"""
from __future__ import annotations
import re
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "auth" / "middleware.py").read_text(encoding="utf-8")


def test_r1_can_061_refund_callback_in_public_paths():
    # PUBLIC_PATHS 集合内必须含退款回调
    m = re.search(r"PUBLIC_PATHS\s*=\s*\{(.+?)\}", SRC, re.DOTALL)
    assert m, "未定位 PUBLIC_PATHS"
    assert "/api/wallet/wechat-refund-callback" in m.group(1), \
        "R1-CAN-061: 微信退款回调必须在 PUBLIC_PATHS(否则中间件 401 → 退款不到账)"


def test_r1_can_031_suffix_bypass_get_head_only():
    # 后缀白名单放行必须限定 GET/HEAD
    m = re.search(r"if\s+([^\n:]*PUBLIC_SUFFIXES[^\n:]*):", SRC)
    assert m, "未定位后缀白名单放行判断"
    cond = m.group(1)
    assert 'method in ("GET", "HEAD")' in cond or "method in ('GET', 'HEAD')" in cond, \
        "R1-CAN-031: 后缀白名单必须限 GET/HEAD,否则 mutating 方法可绕鉴权"


def test_r1_can_097_refresh_failure_returns_503():
    """[Deploy-CTO NO-GO finding 4 v3 · 老板改判 2026-07-12]
    软刷新失败必须【直接返回 503 AUTH_REFRESH_UNAVAILABLE · 绝不 call_next】,
    覆盖 required_module=None 的 auth-only 路由。废弃 permissions=[] 的假 fail-closed。
    """
    i = SRC.find("soft-refresh 异常")
    assert i != -1
    block = SRC[i:i + 1400]
    assert "status_code=503" in block and "AUTH_REFRESH_UNAVAILABLE" in block, \
        "R1-CAN-097 v3: 软刷新失败必须返回 503 AUTH_REFRESH_UNAVAILABLE"
    # 假 fail-closed helper 必须移除(不再被调用);except 块到 503 之间不得【调用】call_next(request)
    assert "_failclosed_fallback(" not in SRC, "废弃的假 fail-closed helper 必须移除"
    j = SRC.find("AUTH_REFRESH_UNAVAILABLE", i)
    assert "call_next(request)" not in SRC[i:j], "软刷新失败到 503 之间不得 call_next"
