"""V3.3.1 feature flags helper 单元测试(纯函数 · 不依赖 DB)"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_default_flags_all_off(monkeypatch):
    """默认所有 V3.3.1 总开关都是 OFF · 不影响生产"""
    # 清缓存 + 清环境变量
    from config import v3_3_1_flags
    v3_3_1_flags.clear_cache()
    for k in (
        "V3_3_1_ENABLED",
        "V3_3_1_DUAL_WRITE_OLD_TABLE",
        "V3_3_1_NET_CASH_REVENUE_BASE",
        "V3_3_1_SERVICE_FEE_CONVERSION_ENABLED",
        "V3_3_1_WITHDRAWAL_ENABLED",
        "V3_3_1_RBAC_GATE_ENABLED",
        "V3_3_1_INVITE_CODE_REQUIRED",
    ):
        monkeypatch.delenv(k, raising=False)

    # mock DB read (不连真 DB)
    monkeypatch.setattr(v3_3_1_flags, "_read_from_db", lambda key: None)
    v3_3_1_flags.clear_cache()

    assert v3_3_1_flags.is_v3_3_1_enabled() is False
    # dual_write 默认 True · 但总开关 OFF 时仍返 False
    assert v3_3_1_flags.is_dual_write_enabled() is False
    assert v3_3_1_flags.is_net_cash_revenue_base() is False
    assert v3_3_1_flags.is_service_fee_conversion_enabled() is False
    assert v3_3_1_flags.is_withdrawal_enabled() is False
    assert v3_3_1_flags.is_rbac_gate_enabled() is False
    assert v3_3_1_flags.is_invite_code_required() is False


def test_env_overrides_default(monkeypatch):
    from config import v3_3_1_flags
    monkeypatch.setattr(v3_3_1_flags, "_read_from_db", lambda key: None)
    monkeypatch.setenv("V3_3_1_ENABLED", "true")
    monkeypatch.setenv("V3_3_1_WITHDRAWAL_ENABLED", "1")
    v3_3_1_flags.clear_cache()

    assert v3_3_1_flags.is_v3_3_1_enabled() is True
    assert v3_3_1_flags.is_withdrawal_enabled() is True
    # 未设的仍 default
    assert v3_3_1_flags.is_rbac_gate_enabled() is False


def test_param_defaults(monkeypatch):
    from config import v3_3_1_flags
    monkeypatch.setattr(v3_3_1_flags, "_read_from_db", lambda key: None)
    v3_3_1_flags.clear_cache()

    assert v3_3_1_flags.get_service_fee_rate() == 0.22
    assert v3_3_1_flags.get_referral_bonus_rate() == 0.15
    assert v3_3_1_flags.get_conversion_bonus_rate() == 0.20
    assert v3_3_1_flags.get_conversion_min_age_days() == 7
    assert v3_3_1_flags.get_withdrawal_min_amount() == 100.0
    assert v3_3_1_flags.get_withdrawal_max_per_week() == 1
    assert v3_3_1_flags.get_l1_monthly_invite_quota() == 10


def test_conversion_quota_by_tier(monkeypatch):
    from config import v3_3_1_flags
    monkeypatch.setattr(v3_3_1_flags, "_read_from_db", lambda key: None)
    v3_3_1_flags.clear_cache()

    assert v3_3_1_flags.get_conversion_quota_yuan("standard") == 5000.0
    assert v3_3_1_flags.get_conversion_quota_yuan("premium") == 20000.0
    assert v3_3_1_flags.get_conversion_quota_yuan("strategic") >= 1_000_000
    # 兜底
    assert v3_3_1_flags.get_conversion_quota_yuan("unknown_tier") == 5000.0
    assert v3_3_1_flags.get_conversion_quota_yuan(None) == 5000.0


def test_source_validation():
    from config.v3_3_1_flags import (
        PaymentSource, is_revenue_triggering_source,
    )
    assert is_revenue_triggering_source(PaymentSource.EXTERNAL_CASH) is True
    assert is_revenue_triggering_source(PaymentSource.BALANCE_DEDUCTION) is False
    assert is_revenue_triggering_source(PaymentSource.SERVICE_FEE_CONVERSION) is False
    assert is_revenue_triggering_source(PaymentSource.EXTERNAL_MEDIA_PURCHASE) is False
    assert is_revenue_triggering_source(None) is False
    assert is_revenue_triggering_source("unknown") is False


def test_anomalous_refund_detection():
    from config.v3_3_1_flags import is_anomalous_refund
    assert is_anomalous_refund("legal_dispute") is True
    assert is_anomalous_refund("platform_force") is True
    assert is_anomalous_refund("fraud") is True
    assert is_anomalous_refund("chargeback") is True
    assert is_anomalous_refund("user_request") is False
    assert is_anomalous_refund("unknown") is False
    assert is_anomalous_refund(None) is False


def test_service_fee_status_enum():
    from config.v3_3_1_flags import ServiceFeeStatus, VALID_SERVICE_FEE_STATUSES
    expected = {
        "pending", "settled", "converted", "withdraw_requested",
        "withdrawn", "cancelled", "clawback", "rejected",
    }
    assert VALID_SERVICE_FEE_STATUSES == expected
    assert ServiceFeeStatus.PENDING == "pending"
    assert ServiceFeeStatus.WITHDRAW_REQUESTED == "withdraw_requested"
    assert ServiceFeeStatus.CLAWBACK == "clawback"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
