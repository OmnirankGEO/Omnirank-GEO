"""[BUG-P3] agent_pricing 费率配置进程级缓存无失效通道 · 静态守护

根因:_fee_config_cache 模块级,reload_fee_config 全仓 0 调用方、无 admin 写端点 →
进程生命周期内永不过期。改 system_settings.platform_fee_config 费率后,提现三段扣费/
结算费率继续用旧值,蓝绿两容器各持一套(切流量时同一服务商两次报价不同)。
修:加 60s TTL,改费率后最多 60s 自动生效。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fee_config_has_ttl():
    src = (ROOT / "services" / "agent_pricing.py").read_text(encoding="utf-8")
    assert "_FEE_CONFIG_TTL_SEC" in src, "费率缓存缺 TTL → 改费率后进程级永不过期"
    assert "_fee_config_cache_at" in src, "缺缓存时间戳"
    assert "monotonic" in src, "TTL 须用单调时钟判断"
    # get_platform_fee_config 不再无条件返回缓存(必带 TTL 判断)
    func = src[src.find("def get_platform_fee_config"):src.find("def reload_fee_config")]
    assert "_FEE_CONFIG_TTL_SEC" in func, "TTL 判断须在 get_platform_fee_config 内"
