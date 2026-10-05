"""[BUG-P3] charge_with_refund 后扣费失败被静默吞(TOCTOU)· 静态守护

根因:check_balance_only 预检无锁,并发多任务全过预检后业务全执行;第一个 deduct 扣穿余额,
后续 deduct 402 → charge_with_refund 仅 logger.error("只记 log 不挂用户")→ 不扣/不告警/不限流
→ 并发抽干余额时业务白送,且漏扣不可见、无法对账追回。
修(最低成本):升级 logger.critical + 结构化 [UNCHARGED] 告警(监控可抓)+ holder["uncharged"]
标记(调用方可感知),让运营发现并人工追扣。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_uncharged_failure_is_visible():
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    s = src.find("async def charge_with_refund")
    assert s >= 0
    func = src[s: src.find("\n@", s + 10) if src.find("\n@", s + 10) > 0 else s + 3000]
    assert "[UNCHARGED]" in func, "漏扣须打可监控告警标记"
    assert "logger.critical" in func, "漏扣须升级到 critical 告警级(非静默 error)"
    assert 'holder["uncharged"]' in func, "须标记 holder uncharged 供调用方感知"
