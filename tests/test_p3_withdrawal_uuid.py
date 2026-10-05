"""[BUG-P3] 提现 idempotency_key 类型契约不一致(str vs DB UUID)→ 非 UUID 入参 500

根因:WithdrawalRequest.idempotency_key 声明 str,但 withdrawal_requests.idempotency_key 列为
UUID NOT NULL;非 UUID 字符串在幂等 SELECT(WHERE idempotency_key=%s)被 PG 当 UUID 比较 →
invalid input syntax for type uuid → 500(本应幂等命中/拒;且换 key 重试可能重复冻结)。
修:Pydantic field_validator 入口校验合法 UUID,非法 → 422(不到 DB)。
"""
import uuid

import pytest
from pydantic import ValidationError

from api.withdrawal_api import WithdrawalRequest


def test_valid_uuid_passes():
    r = WithdrawalRequest(amount_yuan=100, bank_card_id=1, idempotency_key=str(uuid.uuid4()))
    assert r.idempotency_key


def test_invalid_uuid_rejected():
    with pytest.raises(ValidationError):
        WithdrawalRequest(amount_yuan=100, bank_card_id=1, idempotency_key="abc")


def test_empty_idempotency_rejected():
    with pytest.raises(ValidationError):
        WithdrawalRequest(amount_yuan=100, bank_card_id=1, idempotency_key="")
