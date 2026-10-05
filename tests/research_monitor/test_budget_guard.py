"""
A.5.4 budget_guard 纯 mock 测试 (不连真 PG)。

覆盖:
- get_budget_config: 读出 350/1000 / 配置缺失走默认值 / DB 异常走默认值
- get_round_cost / get_month_cost: SUM 返回值 cast float
- check_round_budget: ok / 超支两路径
- check_month_budget: ok / 超支两路径
- BudgetExhaustedError: 字段保留
"""
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

from services.research_monitor import budget_guard
from services.research_monitor.budget_guard import (
    BudgetExhaustedError,
    BudgetDataUnavailableError,
    get_budget_config,
    get_round_cost,
    get_month_cost,
    check_round_budget,
    check_month_budget,
    DEFAULT_BUDGET_PER_ROUND,
    DEFAULT_BUDGET_PER_MONTH,
)


def _make_fake_conn(fetchall_rows=None, fetchone_row=None):
    """构造 fake conn: cursor() / execute() / fetchall() / fetchone() / close()"""
    fake_cursor = MagicMock()
    fake_cursor.fetchall = MagicMock(return_value=fetchall_rows or [])
    fake_cursor.fetchone = MagicMock(return_value=fetchone_row)
    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)
    fake_conn.close = MagicMock()
    return fake_conn, fake_cursor


# ==================== TestGetBudgetConfig ====================

class TestGetBudgetConfig:

    def test_reads_both_keys_from_db(self):
        """
        cursor.fetchall 返两行 config, 应返 {'budget_per_round_yuan': 350.0, 'budget_per_month_yuan': 1000.0}
        """
        fake_conn, _ = _make_fake_conn(fetchall_rows=[
            {'key': 'budget_per_round_yuan', 'value_json': 350},
            {'key': 'budget_per_month_yuan', 'value_json': 1000},
        ])
        with patch.object(budget_guard, 'get_connection', return_value=fake_conn):
            cfg = get_budget_config()

        assert cfg['budget_per_round_yuan'] == 350.0
        assert cfg['budget_per_month_yuan'] == 1000.0
        # 必须 close
        fake_conn.close.assert_called_once()

    def test_partial_config_uses_default_for_missing_key(self):
        """只配 round 没配 month, month 应走默认 1000"""
        fake_conn, _ = _make_fake_conn(fetchall_rows=[
            {'key': 'budget_per_round_yuan', 'value_json': 500},
        ])
        with patch.object(budget_guard, 'get_connection', return_value=fake_conn):
            cfg = get_budget_config()

        assert cfg['budget_per_round_yuan'] == 500.0
        assert cfg['budget_per_month_yuan'] == DEFAULT_BUDGET_PER_MONTH

    def test_value_json_as_float_string_coerced(self):
        """value_json 即使是 '350.5' 字符串 (异常配置), 也应被 cast 成 350.5"""
        fake_conn, _ = _make_fake_conn(fetchall_rows=[
            {'key': 'budget_per_round_yuan', 'value_json': '350.5'},
            {'key': 'budget_per_month_yuan', 'value_json': 1000.0},
        ])
        with patch.object(budget_guard, 'get_connection', return_value=fake_conn):
            cfg = get_budget_config()

        assert cfg['budget_per_round_yuan'] == 350.5
        assert cfg['budget_per_month_yuan'] == 1000.0

    def test_value_json_non_numeric_falls_back_to_default(self):
        """value_json 是个 dict (异常), 该 key 走默认值不抛"""
        fake_conn, _ = _make_fake_conn(fetchall_rows=[
            {'key': 'budget_per_round_yuan', 'value_json': {'not': 'a number'}},
            {'key': 'budget_per_month_yuan', 'value_json': 1000},
        ])
        with patch.object(budget_guard, 'get_connection', return_value=fake_conn):
            cfg = get_budget_config()

        assert cfg['budget_per_round_yuan'] == DEFAULT_BUDGET_PER_ROUND
        assert cfg['budget_per_month_yuan'] == 1000.0


class TestGetBudgetConfigFallback:

    def test_empty_table_returns_defaults(self):
        """配置表完全没记录, fetchall 返空 list, 应返默认 350/1000"""
        fake_conn, _ = _make_fake_conn(fetchall_rows=[])
        with patch.object(budget_guard, 'get_connection', return_value=fake_conn):
            cfg = get_budget_config()

        assert cfg['budget_per_round_yuan'] == DEFAULT_BUDGET_PER_ROUND
        assert cfg['budget_per_month_yuan'] == DEFAULT_BUDGET_PER_MONTH

    def test_db_exception_returns_defaults(self):
        """get_connection 抛时不能 fail-open, 应交给 check_* fail-closed。"""
        with patch.object(
            budget_guard, 'get_connection',
            side_effect=RuntimeError('PG down'),
        ):
            with pytest.raises(BudgetDataUnavailableError):
                get_budget_config()


# ==================== TestGetRoundCost / TestGetMonthCost ====================

class TestGetRoundCost:

    def test_returns_float_from_decimal(self):
        """SUM 返 Decimal('123.4567'), 应被 cast 成 float"""
        fake_conn, _ = _make_fake_conn(fetchone_row={'total': Decimal('123.4567')})
        with patch.object(budget_guard, 'get_connection', return_value=fake_conn):
            cost = get_round_cost('round_xxx')

        assert isinstance(cost, float)
        assert abs(cost - 123.4567) < 1e-6

    def test_null_returns_zero(self):
        """SUM 返 0 (空表 COALESCE 兜底), 应返 0.0"""
        fake_conn, _ = _make_fake_conn(fetchone_row={'total': 0})
        with patch.object(budget_guard, 'get_connection', return_value=fake_conn):
            cost = get_round_cost('round_xxx')

        assert cost == 0.0

    def test_db_exception_returns_zero(self):
        """DB 异常不能按 0 处理, 否则预算保护 fail-open。"""
        with patch.object(
            budget_guard, 'get_connection',
            side_effect=RuntimeError('PG down'),
        ):
            with pytest.raises(BudgetDataUnavailableError):
                get_round_cost('round_xxx')


class TestGetMonthCost:

    def test_returns_float(self):
        fake_conn, _ = _make_fake_conn(fetchone_row={'total': Decimal('888.0')})
        with patch.object(budget_guard, 'get_connection', return_value=fake_conn):
            cost = get_month_cost()

        assert isinstance(cost, float)
        assert cost == 888.0

    def test_null_returns_zero(self):
        fake_conn, _ = _make_fake_conn(fetchone_row=None)
        with patch.object(budget_guard, 'get_connection', return_value=fake_conn):
            cost = get_month_cost()

        assert cost == 0.0


# ==================== TestCheckRoundBudget ====================

class TestCheckRoundBudgetOk:

    def test_within_budget_returns_ok(self):
        """spent=100, limit=350, 应返 ok=True, remaining=250"""
        with patch.object(budget_guard, 'get_round_cost', return_value=100.0), \
             patch.object(
                 budget_guard, 'get_budget_config',
                 return_value={
                     'budget_per_round_yuan': 350.0,
                     'budget_per_month_yuan': 1000.0,
                 },
             ):
            result = check_round_budget('round_xxx')

        assert result['ok'] is True
        assert result['spent'] == 100.0
        assert result['limit'] == 350.0
        assert result['remaining'] == 250.0
        assert result['reason'] is None

    def test_cost_read_failure_returns_not_ok(self):
        with patch.object(budget_guard, 'get_round_cost',
                          side_effect=BudgetDataUnavailableError('PG down')), \
             patch.object(
                 budget_guard, 'get_budget_config',
                 return_value={
                     'budget_per_round_yuan': 350.0,
                     'budget_per_month_yuan': 1000.0,
                 },
             ):
            result = check_round_budget('round_xxx')

        assert result['ok'] is False
        assert result['code'] == 'budget_data_unavailable'


class TestCheckRoundBudgetExhausted:

    def test_over_budget_returns_not_ok_with_reason(self):
        """spent=360, limit=350, 应返 ok=False, reason 含 350 和 360"""
        with patch.object(budget_guard, 'get_round_cost', return_value=360.0), \
             patch.object(
                 budget_guard, 'get_budget_config',
                 return_value={
                     'budget_per_round_yuan': 350.0,
                     'budget_per_month_yuan': 1000.0,
                 },
             ):
            result = check_round_budget('round_xxx')

        assert result['ok'] is False
        assert result['spent'] == 360.0
        assert result['limit'] == 350.0
        assert result['remaining'] == -10.0
        assert result['reason'] is not None
        # reason 中包含两个数字 (中文叙述里花了多少 / 上限多少)
        assert '350' in result['reason']
        assert '360' in result['reason']

    def test_exactly_equal_to_limit_treated_as_exhausted(self):
        """spent == limit 也视为超支 (>= 触发熔断), 防越过"""
        with patch.object(budget_guard, 'get_round_cost', return_value=350.0), \
             patch.object(
                 budget_guard, 'get_budget_config',
                 return_value={
                     'budget_per_round_yuan': 350.0,
                     'budget_per_month_yuan': 1000.0,
                 },
             ):
            result = check_round_budget('round_xxx')

        assert result['ok'] is False
        assert result['remaining'] == 0.0


# ==================== TestCheckMonthBudget ====================

class TestCheckMonthBudgetOk:

    def test_within_budget_returns_ok(self):
        with patch.object(budget_guard, 'get_month_cost', return_value=500.0), \
             patch.object(
                 budget_guard, 'get_budget_config',
                 return_value={
                     'budget_per_round_yuan': 350.0,
                     'budget_per_month_yuan': 1000.0,
                 },
             ):
            result = check_month_budget()

        assert result['ok'] is True
        assert result['spent'] == 500.0
        assert result['limit'] == 1000.0
        assert result['remaining'] == 500.0
        assert result['reason'] is None

    def test_month_cost_read_failure_returns_not_ok(self):
        with patch.object(budget_guard, 'get_month_cost',
                          side_effect=BudgetDataUnavailableError('PG down')), \
             patch.object(
                 budget_guard, 'get_budget_config',
                 return_value={
                     'budget_per_round_yuan': 350.0,
                     'budget_per_month_yuan': 1000.0,
                 },
             ):
            result = check_month_budget()

        assert result['ok'] is False
        assert result['code'] == 'budget_data_unavailable'


class TestCheckMonthBudgetExhausted:

    def test_over_budget_returns_not_ok(self):
        with patch.object(budget_guard, 'get_month_cost', return_value=1100.0), \
             patch.object(
                 budget_guard, 'get_budget_config',
                 return_value={
                     'budget_per_round_yuan': 350.0,
                     'budget_per_month_yuan': 1000.0,
                 },
             ):
            result = check_month_budget()

        assert result['ok'] is False
        assert result['spent'] == 1100.0
        assert result['limit'] == 1000.0
        assert result['remaining'] == -100.0
        assert '1000' in result['reason']
        assert '1100' in result['reason']


# ==================== TestBudgetExhaustedError ====================

class TestBudgetExhaustedError:

    def test_fields_preserved(self):
        """level / spent / limit / reason 字段都应保留"""
        err = BudgetExhaustedError(
            level='round',
            spent=360.0,
            limit=350.0,
            reason='单轮预算超支 ¥360.00/¥350.00',
        )
        assert err.level == 'round'
        assert err.spent == 360.0
        assert err.limit == 350.0
        assert err.reason == '单轮预算超支 ¥360.00/¥350.00'
        assert str(err) == '单轮预算超支 ¥360.00/¥350.00'

    def test_is_exception(self):
        with pytest.raises(BudgetExhaustedError):
            raise BudgetExhaustedError(
                level='month',
                spent=1100.0,
                limit=1000.0,
                reason='月度预算超支',
            )
