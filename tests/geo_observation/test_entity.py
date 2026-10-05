"""R5(Q2)实体审核:官方 verifier 最小化/PII 清洗/官方参数、强类型校验失败→UNKNOWN、
400 重试不降级晋升、混淆品牌确定性拒绝、trusted_exact 提及。
"""
from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import asyncio
import json

from db.connection import get_connection
from services.brand_identity_resolver import BrandIdentity, BrandVerdict
from services.geo_observation.contracts import EntityState
from services.geo_observation.entity_review import (
    decision_to_entity_state, make_official_verifier, resolve_entity,
)

import _helpers as H

IDENTITY = BrandIdentity(brand_id=501, canonical_names=("晨光富士电梯",), trusted_aliases=(), industry="电梯")
WINDOWS = ("这段讨论富士电梯与晨光富士电梯的区别,联系电话13800138000,邮箱user@example.com,订单1234567890。",)


def _run(v, identity=IDENTITY, windows=WINDOWS):
    return asyncio.run(v(identity=identity, evidence_windows=windows))


def test_verifier_minimization_official_params_and_pii_scrub():
    captured = []
    res = _run(make_official_verifier(H.make_capturing_post(captured, verdict="NO")))
    assert res.verdict == BrandVerdict.NO
    body = captured[0]
    blob = json.dumps(body, ensure_ascii=False)
    # 官方参数
    # 🔴 [WO_206 c1c 翻面 2026-09-14] 原来这里写死 "deepseek-v4-flash"。
    #    官方 2026-09-13 把 Flash 档改名 deepseek-flash,Deploy 206-d2 实打:
    #    官方 /models 只剩 deepseek-flash 与 deepseek-v4-pro。继续钉旧名,
    #    这条判据就会和**正确的修法互斥**。改成取常量:本判据守的那件事一个字没变,
    #    而「既定模型叫什么」只剩一个出处(config/deepseek_models)。
    assert body["model"] == DEEPSEEK_OFFICIAL_FLASH
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    # 最小化:目标品牌名允许;PII / 租户标识禁传
    assert "晨光富士电梯" in body["messages"][0]["content"]
    assert "13800138000" not in blob and "user@example.com" not in blob and "1234567890" not in blob
    for forbidden in ("owner_user_id", "brand_id", "run_token", "quote_id", "upstream", "cost_multiplier"):
        assert forbidden not in blob


def test_verifier_bad_json_unknown():
    res = _run(make_official_verifier(H.make_capturing_post([], bad_json=True)))
    assert res.verdict == BrandVerdict.UNKNOWN


def test_verifier_missing_verdict_field_unknown():
    async def _post(body):
        return H.FakeResp({"choices": [{"message": {"content": json.dumps({"reason": "no verdict"})}}]})
    res = _run(make_official_verifier(_post))
    assert res.verdict == BrandVerdict.UNKNOWN   # Pydantic 缺字段 → UNKNOWN


def test_verifier_extra_field_unknown():
    async def _post(body):
        payload = {"verdict": "YES", "reason": "x", "matched_text": "晨光富士电梯",
                   "window_index": 1, "matched_start": 0, "matched_end": 6, "injected": "evil"}
        return H.FakeResp({"choices": [{"message": {"content": json.dumps(payload)}}]})
    res = _run(make_official_verifier(_post))
    assert res.verdict == BrandVerdict.UNKNOWN   # extra='forbid' → UNKNOWN


def test_verifier_400_retry_then_valid():
    calls = {"n": 0}
    async def _post(body):
        calls["n"] += 1
        if calls["n"] == 1:
            assert "response_format" in body       # 首次带 json 模式
            return H.FakeResp({}, status_code=400)
        assert "response_format" not in body       # 重试去掉 json 模式
        return H.FakeResp({"choices": [{"message": {"content": json.dumps(
            {"verdict": "NO", "reason": "ok", "matched_text": "", "window_index": None,
             "matched_start": None, "matched_end": None})}}]})
    res = _run(make_official_verifier(_post))
    assert res.verdict == BrandVerdict.NO and calls["n"] == 2


def test_verifier_persistent_error_unknown_no_auto_promote():
    async def _post(body):
        return H.FakeResp({}, status_code=500)
    res = _run(make_official_verifier(_post))
    assert res.verdict == BrandVerdict.UNKNOWN     # 5xx → UNKNOWN(绝不自动晋升)


def test_decision_to_entity_state_mapping():
    from services.brand_identity_resolver import BrandDecision
    assert decision_to_entity_state(BrandDecision(BrandVerdict.YES, "", "trusted_exact")) == EntityState.confirmed_mention
    assert decision_to_entity_state(BrandDecision(BrandVerdict.NO, "", "deterministic")) == EntityState.confirmed_non_mention
    assert decision_to_entity_state(BrandDecision(BrandVerdict.UNKNOWN, "timeout", "x")) == EntityState.provider_error
    assert decision_to_entity_state(BrandDecision(BrandVerdict.UNKNOWN, "invalid_matched_text", "x")) == EntityState.ambiguous


def test_confusable_brand_correct_no_verifier_non_mention():
    conn = get_connection()
    H.seed_brand(conn, 700, "晨光富士电梯", 124)
    conn.close()
    answer = "本地市场上有富士电梯和富士通电梯两个不同的品牌,消费者需要注意区分它们的资质和服务差异。"
    # 正确的官方 verifier 会判 NO(专有前缀不同)→ confirmed_non_mention
    state, decision, identity = asyncio.run(resolve_entity(700, answer, verifier=H.const_verifier(BrandVerdict.NO)))
    assert state == EntityState.confirmed_non_mention


def test_confusable_brand_wrong_yes_never_becomes_mention():
    """安全性:即便 LLM 误判 YES 且给出富士电梯这类混淆命中,resolver 也绝不确认为提及(降级 UNKNOWN/ambiguous)。"""
    conn = get_connection()
    H.seed_brand(conn, 700, "晨光富士电梯", 124)
    conn.close()
    answer = "本地市场上有富士电梯和富士通电梯两个不同的品牌,消费者需要注意区分它们的资质和服务差异。"
    state, decision, identity = asyncio.run(resolve_entity(
        700, answer, verifier=H.const_verifier(BrandVerdict.YES, matched_text="富士电梯", window_index=1, start=0, end=4)))
    assert state != EntityState.confirmed_mention   # 高风险相似品牌绝不误确认为提及


def test_trusted_exact_mention():
    conn = get_connection()
    H.seed_brand(conn, 701, "大昀装修", 124)
    conn.close()
    answer = "在深圳装修市场里,大昀装修的口碑和服务都很不错,值得业主重点考虑和优先选择。"
    # 注入 NO verifier;trusted_exact 命中 → 仍 YES(不依赖 LLM)
    state, decision, identity = asyncio.run(resolve_entity(701, answer, verifier=H.const_verifier(BrandVerdict.NO)))
    assert state == EntityState.confirmed_mention and decision.method == "trusted_exact"
