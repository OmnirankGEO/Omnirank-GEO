"""R7 HMAC 治理:域分离 / 确定性 / v1 单版本 / 缺密钥 fail-closed(公共晋升关闭,私域仍登记)。

必给证据 #5:HMAC 缺失/轮换 fail-closed。
"""
from __future__ import annotations

import asyncio

import pytest

from db.connection import get_connection
from services.brand_identity_resolver import BrandVerdict
from services.geo_observation import hmac_buckets, promotion, source_hooks
from services.geo_observation.promotion import DecisionContext

import _helpers as H

APPROVED = DecisionContext(True, "legal_v1", "consent_v1", None, outcome_gold_gate_passed=True)
VNO = H.const_verifier(BrandVerdict.NO)
LONG = "大昀装修在深圳口碑非常好,服务专业,施工质量稳定可靠,非常值得推荐给有装修需求的业主优先考虑选择。"


def test_domain_separation_and_determinism():
    assert hmac_buckets.user_bucket(124) != hmac_buckets.brand_bucket(124)  # 域分离
    assert hmac_buckets.user_bucket(124) == hmac_buckets.user_bucket(124)   # 确定性
    assert hmac_buckets.BUCKET_KEY_VERSION == 1                             # v1 单版本
    assert len(hmac_buckets.user_bucket(124)) == 64


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("GEO_OBSERVATION_HMAC_KEY_V1", raising=False)
    assert hmac_buckets.is_configured() is False
    with pytest.raises(hmac_buckets.HmacKeyUnavailable):
        hmac_buckets.user_bucket(124)


def test_illegal_key_raises(monkeypatch):
    monkeypatch.setenv("GEO_OBSERVATION_HMAC_KEY_V1", "!!!not-base64!!!")
    with pytest.raises(hmac_buckets.HmacKeyUnavailable):
        hmac_buckets.brand_bucket(1)


def test_missing_key_public_promotion_fail_closed(monkeypatch):
    """缺 HMAC → 客户来源公共晋升 fail-closed → private_only(私域仍登记,零 signal/桶)。"""
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "run_nohmac", owner=124, brand=501, run_status="committed", visibility="published",
                     answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "run_nohmac", H.now())
    conn.commit(); conn.close()

    monkeypatch.delenv("GEO_OBSERVATION_HMAC_KEY_V1", raising=False)
    r = asyncio.run(promotion.process_next_pending(ctx=APPROVED, verifier=VNO, outcome_classifier=H.const_outcome()))
    assert r["state"] == "private_only"
    assert "hmac_key_unavailable" in r["reasons"]

    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals")
    assert cur.fetchone()["n"] == 0     # 零公共信号
    cur.execute("SELECT count(*) AS n FROM geo_observation_contributor_buckets")
    assert cur.fetchone()["n"] == 0     # 零桶
    cur.execute("SELECT processing_state FROM geo_observation_events WHERE source_record_id='run_nohmac'")
    assert cur.fetchone()["processing_state"] == "private_only"   # 私域仍登记(不丢)
    conn.close()
