"""
提现功能单元测试
- luhn_check: 银行卡号校验
- mask_card_number: 脱敏
- 手续费计算
"""

import pytest
from decimal import Decimal

from db.withdrawal_db import (
    luhn_check,
    mask_card_number,
    FEE_RATE,
    MIN_WITHDRAWAL_YUAN,
    POINTS_PER_YUAN,
)


# ==================== luhn_check ====================

class TestLuhnCheck:
    def test_valid_card(self):
        """有效的银行卡号（通过 Luhn 校验）"""
        assert luhn_check("6217000000000000004") is True

    def test_invalid_card(self):
        """无效的银行卡号（最后一位改错）"""
        assert luhn_check("6217000000000000003") is False

    def test_too_short(self):
        """卡号太短（< 16 位）"""
        assert luhn_check("621700000") is False

    def test_too_long(self):
        """卡号太长（> 19 位）"""
        assert luhn_check("62170000000000000000") is False

    def test_non_numeric(self):
        """非数字字符"""
        assert luhn_check("621700000000abcd") is False

    def test_empty_string(self):
        """空字符串"""
        assert luhn_check("") is False

    def test_none(self):
        """None 输入"""
        assert luhn_check(None) is False


# ==================== mask_card_number ====================

class TestMaskCardNumber:
    def test_normal_card(self):
        """正常卡号脱敏为 ****XXXX"""
        result = mask_card_number("6217000000000000004")
        assert result == "****0004"

    def test_short_card(self):
        """短于 4 位的输入"""
        assert mask_card_number("12") == "****"

    def test_exactly_four(self):
        """恰好 4 位"""
        assert mask_card_number("1234") == "****1234"

    def test_empty(self):
        """空字符串"""
        assert mask_card_number("") == "****"

    def test_none(self):
        """None 输入"""
        assert mask_card_number(None) == "****"


# ==================== 手续费计算 ====================

class TestFeeCalculation:
    def _calc_fee(self, amount_yuan: Decimal):
        """模拟 create_withdrawal 中的手续费计算逻辑"""
        fee = (amount_yuan * FEE_RATE).quantize(Decimal("0.01"))
        actual = amount_yuan - fee
        points = int(amount_yuan * POINTS_PER_YUAN)
        return fee, actual, points

    def test_min_withdrawal(self):
        """最低提现 100 元: 手续费 1 元, 实际 99 元"""
        fee, actual, points = self._calc_fee(MIN_WITHDRAWAL_YUAN)
        assert fee == Decimal("1.00")
        assert actual == Decimal("99.00")
        assert points == 13000

    def test_large_withdrawal(self):
        """大额提现 10000 元: 手续费 100 元, 实际 9900 元"""
        fee, actual, points = self._calc_fee(Decimal("10000"))
        assert fee == Decimal("100.00")
        assert actual == Decimal("9900.00")
        assert points == 1300000
