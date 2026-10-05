"""[T6b] 自动通过判定 + [T6] 建议通过判定(纯逻辑,阈值 SSOT)。"""
from services.media_binding_candidates import (
    AUTO_APPROVE_MIN_CONFIDENCE,
    RECOMMENDED_BINDING_MIN_CONFIDENCE,
    is_auto_approvable,
    is_recommended_binding,
)


def _cand(**kw):
    base = {
        "status": "candidate",
        "match_method": "domain_exact",
        "match_confidence": 0.98,
        "risk_flags": [],
        "can_approve": True,
    }
    base.update(kw)
    return base


def test_thresholds():
    assert RECOMMENDED_BINDING_MIN_CONFIDENCE == 0.90
    assert AUTO_APPROVE_MIN_CONFIDENCE == 0.95


def test_auto_approve_domain_exact_high_conf():
    assert is_auto_approvable(_cand(match_method="domain_exact", match_confidence=0.98)) is True


def test_auto_approve_rejects_name_alias():
    # 0.86 name_alias = 错绑高发,永不自动
    assert is_auto_approvable(_cand(match_method="name_alias", match_confidence=0.86)) is False


def test_auto_approve_rejects_below_095():
    assert is_auto_approvable(_cand(match_method="domain_exact", match_confidence=0.94)) is False


def test_auto_approve_rejects_risk_or_not_purchasable():
    assert is_auto_approvable(_cand(risk_flags=["共享平台域名需要名称证据"])) is False
    assert is_auto_approvable(_cand(can_approve=False)) is False  # can_approve 含可采购


def test_recommended_binding():
    assert is_recommended_binding(_cand(match_confidence=0.90)) is True
    assert is_recommended_binding(_cand(match_confidence=0.89)) is False
    assert is_recommended_binding(_cand(status="approved")) is False
    assert is_recommended_binding(_cand(can_approve=False)) is False
    assert is_recommended_binding(_cand(risk_flags=["x"])) is False
    # name_alias 0.86 不进建议通过(低于 0.90)
    assert is_recommended_binding(_cand(match_method="name_alias", match_confidence=0.86)) is False
