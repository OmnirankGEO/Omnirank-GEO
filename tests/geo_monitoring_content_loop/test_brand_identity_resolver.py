from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import pytest

from services.brand_identity_resolver import (
    BrandIdentity,
    BrandIdentityResolver,
    BrandVerdict,
    VerificationResult,
    _default_structured_verifier,
    _parse_structured_verifier_response,
    load_brand_identity,
    normalize_brand_name,
    normalize_confirmed_display_names,
    parse_brand_display_names,
    split_brand_aliases,
)
from types import SimpleNamespace
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _identity(*names: str, aliases: tuple[str, ...] = ()) -> BrandIdentity:
    return BrandIdentity(
        brand_id=17,
        canonical_names=tuple(names),
        trusted_aliases=aliases,
        industry="酒店",
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"verdict":"NO","reason":"不同主体","matched_text":"",'
        '"window_index":null,"matched_start":null,"matched_end":null}',
        '```json\n{"verdict":"NO","reason":"不同主体","matched_text":""}\n```',
        '<think>核对专有前缀</think>\n结果：'
        '{"verdict":"NO","reason":"不同主体","matched_text":""}',
    ],
)
def test_structured_verifier_parser_accepts_json_contract_wrappers(raw: str):
    result = _parse_structured_verifier_response(raw)
    assert result.verdict is BrandVerdict.NO
    assert result.reason == "不同主体"
    assert result.matched_text is None


@pytest.mark.asyncio
async def test_default_verifier_uses_official_deepseek_contract(monkeypatch):
    from services.geo_observation import entity_review

    class FakeResponse:
        status_code = 200

        def __init__(self, content: str):
            self._content = content

        def json(self):
            return {
                "choices": [{"message": {"content": self._content}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }

    requests: list[tuple[dict, dict]] = []

    async def fake_paid_post(body, **kwargs):
        requests.append((body, kwargs))
        return FakeResponse('{"verdict":"NO","reason":"不同主体","matched_text":"",'
                            '"window_index":null,"matched_start":null,"matched_end":null}')

    monkeypatch.setattr(entity_review, "_paid_safe_deepseek_post", fake_paid_post)

    result = await _default_structured_verifier(
        identity=_identity("深圳市晨光富士电梯"),
        evidence_windows=("推荐江苏富士电梯",),
    )

    assert result.verdict is BrandVerdict.NO
    assert len(requests) == 1
    body, kwargs = requests[0]
    # 🔴 [WO_206 c1d' 翻面 2026-09-14] 原来这里写死 "deepseek-v4-flash"。
    #    官方 2026-09-13 把 Flash 档改名 deepseek-flash,Deploy 206-d2 实打:
    #    官方 /models 只剩 deepseek-flash 与 deepseek-v4-pro。继续钉旧名,
    #    这条判据就会和**正确的修法互斥**。改成取常量:本判据守的那件事
    #    (「结构化校验器发的是官方线那一档、关思考、要 JSON」)一个字没变。
    assert body["model"] == DEEPSEEK_OFFICIAL_FLASH
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert kwargs["url"] == entity_review.DEEPSEEK_OFFICIAL_URL
    assert kwargs["timeout"] == 30.0
    assert kwargs["call_purpose"] == "diagnosis_brand_identity"


@pytest.mark.asyncio
async def test_default_verifier_malformed_response_is_unknown_without_paid_retry(monkeypatch):
    from services.geo_observation import entity_review

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": "not-json"}}],
                "usage": {},
            }

    calls = 0

    async def fake_paid_post(_body, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse()

    monkeypatch.setattr(entity_review, "_paid_safe_deepseek_post", fake_paid_post)

    result = await _default_structured_verifier(
        identity=_identity("深圳市晨光富士电梯"),
        evidence_windows=("推荐江苏富士电梯",),
    )

    assert result.verdict is BrandVerdict.UNKNOWN
    assert result.reason == "parse_or_transport_error"
    assert calls == 1


@pytest.mark.asyncio
async def test_default_verifier_response_unknown_is_not_replayed(monkeypatch):
    from services.geo_observation import entity_review

    calls = 0

    async def fake_paid_post(_body, **_kwargs):
        nonlocal calls
        calls += 1
        raise entity_review.ProviderResultUnknown("simulated-read-timeout")

    monkeypatch.setattr(entity_review, "_paid_safe_deepseek_post", fake_paid_post)

    result = await _default_structured_verifier(
        identity=_identity("深圳市晨光富士电梯"),
        evidence_windows=("推荐晨光富士电梯",),
    )

    assert result.verdict is BrandVerdict.UNKNOWN
    assert result.reason == "provider_result_unknown"
    assert calls == 1


@pytest.mark.asyncio
async def test_default_verifier_distinguishes_request_definitely_not_sent(monkeypatch):
    from services.geo_observation import entity_review

    calls = 0

    async def fake_paid_post(_body, **_kwargs):
        nonlocal calls
        calls += 1
        raise entity_review._ProviderNotSent("no_deepseek_key")

    monkeypatch.setattr(entity_review, "_paid_safe_deepseek_post", fake_paid_post)

    result = await _default_structured_verifier(
        identity=_identity("深圳市晨光富士电梯"),
        evidence_windows=("推荐晨光富士电梯",),
    )

    assert result.verdict is BrandVerdict.UNKNOWN
    assert result.reason == "provider_not_sent"
    assert calls == 1


@pytest.mark.asyncio
async def test_default_verifier_bounds_parallel_calls(monkeypatch):
    import asyncio
    from services.geo_observation import entity_review

    active = 0
    peak = 0

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": (
                    '{"verdict":"NO","reason":"不同主体","matched_text":"",'
                    '"window_index":null,"matched_start":null,"matched_end":null}'
                )}}],
            }

    async def fake_paid_post(_body, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return FakeResponse()

    monkeypatch.setattr(entity_review, "_paid_safe_deepseek_post", fake_paid_post)
    results = await asyncio.gather(*[
        _default_structured_verifier(
            identity=_identity("深圳市晨光富士电梯"),
            evidence_windows=("推荐江苏富士电梯",),
        )
        for _ in range(24)
    ])

    assert all(result.verdict is BrandVerdict.NO for result in results)
    assert peak == 8


@pytest.mark.asyncio
async def test_tallin_exact_mention_after_5000_chars_is_yes_without_verifier():
    calls = 0

    async def verifier(**_kwargs):
        nonlocal calls
        calls += 1
        return VerificationResult(BrandVerdict.NO, "should-not-run")

    answer = "前文" * 2600 + "；岱林生物的方案值得关注"
    resolver = BrandIdentityResolver(_identity("岱林生物"), verifier=verifier)
    decision = await resolver.resolve(answer)

    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias == "岱林生物"
    assert decision.matched_start == answer.index("岱林生物")
    assert decision.matched_end == decision.matched_start + len("岱林生物")
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prefix_length", "total_length", "name"),
    [
        (847, 1161, "岱林生物"),
        (665, 1622, "雅栖酒店"),
    ],
)
async def test_production_evidence_positions_are_scanned_in_full(
    prefix_length: int,
    total_length: int,
    name: str,
):
    answer = "甲" * prefix_length + name
    answer += "乙" * (total_length - len(answer))

    decision = await BrandIdentityResolver(_identity(name)).resolve(answer)

    assert len(answer) == total_length
    assert answer.index(name) == prefix_length
    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_start == prefix_length
    assert decision.matched_end == prefix_length + len(name)


@pytest.mark.asyncio
async def test_ascii_trusted_short_alias_requires_token_boundary():
    async def verifier(**_kwargs):
        return VerificationResult(BrandVerdict.NO, "not-an-exact-token")

    resolver = BrandIdentityResolver(
        _identity("小米科技", aliases=("MI",)),
        verifier=verifier,
    )

    assert (await resolver.resolve("MI 发布了新品")).verdict is BrandVerdict.YES
    assert (await resolver.resolve("Kimi 发布了新品")).verdict is BrandVerdict.NO


def test_parenthetical_name_preserves_full_identity_and_real_brand_alias():
    assert split_brand_aliases("浙江岱林生物技术股份有限公司（岱林生物）") == (
        "浙江岱林生物技术股份有限公司（岱林生物）",
        "浙江岱林生物技术股份有限公司",
        "岱林生物",
    )


@pytest.mark.parametrize(
    ("name", "forbidden_alias"),
    [
        ("全域上榜(深圳)科技有限公司", "深圳"),
        ("示例企业（北京）", "北京"),
        ("示例主体（个体工商户）", "个体工商户"),
        ("示例酒店（尖沙咀旗舰店）", "尖沙咀旗舰店"),
        # ── [返工 2026-08-06 · P1] 区/镇复合地名穿透 ────────────────────
        # 🔴 上面四格用的是「整串等于某个省市名」或「以资质后缀结尾」两条判据,
        #    Review 交叉重放实测:下面这些**从两条判据底下全部走过去**,
        #    同族假阳性只是从市级降到区/镇一级。「龙岗区平湖」是返工验收探针。
        ("全域上榜（龙岗区平湖）科技有限公司", "龙岗区平湖"),
        ("全域上榜（深圳市南山区）科技有限公司", "深圳市南山区"),
        ("全域上榜（茅台镇）酒业有限公司", "茅台镇"),
        ("示例企业（浦东新区张江）", "浦东新区张江"),
    ],
)
def test_parenthetical_qualifiers_never_become_independent_brand_aliases(
    name: str,
    forbidden_alias: str,
):
    aliases = split_brand_aliases(name)

    assert name in aliases
    assert forbidden_alias not in aliases


@pytest.mark.parametrize(
    ("name", "expected_alias"),
    [
        ("通力电梯（KONE）", "KONE"),
        ("知乎（知+）", "知+"),
        ("浙江岱林生物技术股份有限公司（岱林生物）", "岱林生物"),
        # 显式别名前缀是**人明确声明过**的 → 行政标记闸不适用,必须仍放行。
        # 「城市之光」含「市」字,没有这条逃生口就会被一并误拒。
        ("示例企业（又名：城市之光）", "城市之光"),
        ("示例企业（简称：美构）", "美构"),
        # ── [复审 P1 · 2026-08-06] 真实品牌反例 ────────────────────────
        # 🔴 第一版用**字符级**黑名单(含 市/区/乡 即拒),把下面六个真实品牌
        #    全部误杀,我却在交付单里只点了「乡村基」一个、还写成"有意接受"。
        #    「已知真实品牌被误杀」不能当残留风险 —— 那是把偷懒记在客户账上。
        #    改成行政区划**结构**判据后这六个必须全部放行,且它们**不带**
        #    任何显式别名前缀(带前缀就成了走逃生口,证明不了结构判据本身)。
        ("重庆某某（乡村基）", "乡村基"),
        ("某某集团（北京同仁堂）", "北京同仁堂"),
        ("某某集团（上海家化）", "上海家化"),
        ("某某集团（广州酒家）", "广州酒家"),
        ("某某集团（青岛啤酒）", "青岛啤酒"),
        ("某某餐饮（重庆小面）", "重庆小面"),
    ],
)
def test_real_parenthetical_aliases_still_pass_after_the_administrative_gate(
    name: str,
    expected_alias: str,
):
    """🔴【反向对照】行政标记闸不许把真别名一起杀掉。

    没有这一组,把 ``_parenthetical_identity_alias`` 写成 ``return None``
    也能让上面那 10 格全绿 —— 那是恒真,不是修好了。
    """
    assert expected_alias in split_brand_aliases(name)


def test_explicit_parenthetical_brand_alias_is_preserved():
    assert split_brand_aliases("浙江岱林生物技术股份有限公司（简称：岱林生物）") == (
        "浙江岱林生物技术股份有限公司（简称：岱林生物）",
        "浙江岱林生物技术股份有限公司",
        "岱林生物",
    )


@pytest.mark.asyncio
async def test_location_parenthetical_cannot_turn_city_mentions_into_brand_hits():
    verifier_calls = 0

    async def must_not_verify(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("a city-only answer has no target identity evidence")

    decision = await BrandIdentityResolver(
        _identity("全域上榜(深圳)科技有限公司"),
        verifier=must_not_verify,
    ).resolve("深圳有多家 GEO 服务商，可按行业经验和交付能力进一步比较。")

    assert decision.verdict is BrandVerdict.NO
    assert decision.reason == "no_identity_candidate"
    assert decision.matched_alias is None
    assert verifier_calls == 0


@pytest.mark.asyncio
async def test_parenthetical_full_legal_name_remains_a_positive_identity():
    decision = await BrandIdentityResolver(
        _identity("全域上榜(深圳)科技有限公司")
    ).resolve("推荐全域上榜(深圳)科技有限公司，可进一步了解其 GEO 服务。")

    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias == "全域上榜(深圳)科技有限公司"


@pytest.mark.asyncio
async def test_negative_full_identity_statement_is_not_deterministic_yes():
    async def reject_unconfirmed_identity(**_kwargs):
        return VerificationResult(BrandVerdict.NO, "target-not-confirmed")

    decision = await BrandIdentityResolver(
        _identity("全域上榜(深圳)科技有限公司"),
        verifier=reject_unconfirmed_identity,
    ).resolve("没有查到名为‘全域上榜(深圳)科技有限公司’的可靠公开信息。")

    assert decision.verdict is not BrandVerdict.YES
    assert decision.matched_alias is None


@pytest.mark.asyncio
async def test_later_positive_occurrence_survives_earlier_negative_occurrence():
    answer = "未找到 MI 的旧资料；MI 发布了新品。"
    decision = await BrandIdentityResolver(
        _identity("小米科技", aliases=("MI",))
    ).resolve(answer)

    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias == "MI"
    assert decision.matched_start == answer.rfind("MI")


@pytest.mark.asyncio
async def test_real_parenthetical_brand_alias_stays_deterministic():
    decision = await BrandIdentityResolver(
        _identity("浙江岱林生物技术股份有限公司（岱林生物）")
    ).resolve("推荐岱林生物，可进一步核对其解决方案。")

    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias == "岱林生物"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "answer"),
    [
        (
            "深圳市晨光富士电梯",
            "未找到深圳市晨光富士电梯有限公司的可靠公开信息。",
        ),
        (
            "浙江岱林生物技术股份有限公司",
            "不推荐岱林生物，建议继续比较其他候选企业。",
        ),
        (
            "揭阳滨江南路雅栖酒店",
            "不推荐滨江南路雅栖酒店，建议结合行程重新比较。",
        ),
    ],
)
async def test_negative_identity_context_cannot_enter_any_deterministic_yes_path(
    target: str,
    answer: str,
):
    async def reject_negative_context(**_kwargs):
        return VerificationResult(BrandVerdict.NO, "negative-identity-context")

    decision = await BrandIdentityResolver(
        _identity(target),
        verifier=reject_negative_context,
    ).resolve(answer)

    assert decision.verdict is not BrandVerdict.YES
    assert decision.matched_alias is None


def test_confirmed_display_names_are_normalized_deduplicated_and_reject_generic_words():
    assert normalize_confirmed_display_names([
        "品牌简称：岱林生物",
        "岱林生物",
        "DAILIN",
        " ",
    ]) == ("岱林生物", "DAILIN")

    with pytest.raises(ValueError, match="过于宽泛"):
        normalize_confirmed_display_names(["电梯"])

    assert parse_brand_display_names('["岱林生物", "DAILIN"]') == (
        "岱林生物",
        "DAILIN",
    )


@pytest.mark.asyncio
async def test_confusable_brand_embedded_in_another_brand_requires_verifier():
    verifier_calls = 0

    async def reject_other_brand(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.NO, "different-brand")

    decision = await BrandIdentityResolver(
        _identity("富士电梯"),
        verifier=reject_other_brand,
    ).resolve("晨光富士电梯拥有独立的产品与服务体系。")

    assert decision.verdict is BrandVerdict.NO
    assert verifier_calls == 1


@pytest.mark.asyncio
async def test_generic_chain_name_inside_a_specific_property_requires_verifier():
    verifier_calls = 0

    async def reject_other_property(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.NO, "different-property")

    decision = await BrandIdentityResolver(
        _identity("雅栖酒店"),
        verifier=reject_other_property,
    ).resolve("北京国贸雅栖酒店位于核心商圈。")

    assert decision.verdict is BrandVerdict.NO
    assert verifier_calls == 1


@pytest.mark.asyncio
async def test_standalone_confirmed_brand_name_remains_deterministic():
    verifier_calls = 0

    async def must_not_verify(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("standalone confirmed name must be deterministic")

    decision = await BrandIdentityResolver(
        _identity("富士电梯"),
        verifier=must_not_verify,
    ).resolve("推荐富士电梯，建议结合项目需求进一步考察。")

    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias == "富士电梯"
    assert verifier_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "深圳市晨光富士电梯有限公司主要提供电梯定制和维保服务。",
        "# 深圳市晨光富士电梯有限公司业务介绍",
        "深圳市晨光富士电梯有限公司依托本地团队提供维保服务。",
        "推荐名单包括**深圳市晨光富士电梯有限公司**，可进一步核验方案。",
    ],
)
async def test_trusted_company_stem_accepts_exact_legal_suffix_without_provider(answer: str):
    verifier_calls = 0

    async def must_not_verify(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("an exact trusted legal-name extension is deterministic")

    decision = await BrandIdentityResolver(
        _identity("深圳市晨光富士电梯"),
        verifier=must_not_verify,
    ).resolve(answer)

    assert decision.verdict is BrandVerdict.YES
    assert decision.method == "trusted_legal_suffix_exact"
    assert "深圳市晨光富士电梯有限公司" in decision.matched_alias
    assert answer[decision.matched_start:decision.matched_end] == decision.matched_alias
    assert verifier_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "广州市晨光富士电梯有限公司提供本地维保服务。",
        "深圳市晨光富士电梯工程有限公司提供安装服务。",
        "江苏富士电梯有限公司覆盖多个城市。",
        "北京国贸雅栖酒店有限公司位于核心商圈。",
    ],
)
async def test_legal_suffix_extension_does_not_launder_competitors_or_branch_names(answer: str):
    verifier_calls = 0

    async def reject_candidate(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.NO, "not-the-target-brand")

    identity_name = "雅栖酒店" if "雅栖" in answer else "深圳市晨光富士电梯"
    decision = await BrandIdentityResolver(
        _identity(identity_name),
        verifier=reject_candidate,
    ).resolve(answer)

    assert decision.verdict is not BrandVerdict.YES
    assert verifier_calls <= 1


@pytest.mark.asyncio
async def test_human_rejected_full_legal_name_wins_over_derived_suffix_extension():
    identity = BrandIdentity(
        brand_id=592,
        canonical_names=("深圳市晨光富士电梯",),
        rejected_aliases=("深圳市晨光富士电梯有限公司",),
        industry="电梯",
    )
    decision = await BrandIdentityResolver(identity).resolve(
        "深圳市晨光富士电梯有限公司提供电梯服务。"
    )

    assert decision.verdict is BrandVerdict.NO
    assert decision.reason == "human_rejected_name"


@pytest.mark.asyncio
async def test_trusted_and_conservative_legal_short_names_are_deterministic():
    trusted = BrandIdentityResolver(_identity("浙江岱林生物技术股份有限公司", aliases=("岱林生物",)))
    assert (await trusted.resolve("推荐岱林生物")).verdict is BrandVerdict.YES
    assert (await trusted.resolve("岱林生物是国内相关领域的长期参与者。")).verdict is BrandVerdict.YES

    verifier_calls = 0

    async def must_not_verify(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("conservative legal abbreviations must not use a paid verifier")

    unbound = BrandIdentityResolver(
        _identity("浙江岱林生物技术股份有限公司"),
        verifier=must_not_verify,
    )
    answer = "细胞治疗设备供应商包括岱林生物（DAILIN），其隔离系统值得关注。"
    decision = await unbound.resolve(answer)

    assert decision.verdict is BrandVerdict.YES
    assert decision.method == "derived_legal_exact"
    assert decision.matched_alias == "岱林生物"
    assert answer[decision.matched_start:decision.matched_end] == "岱林生物"
    assert verifier_calls == 0

    legal_suffix_answer = "候选企业：岱林生物技术股份有限公司，隔离器方案值得关注。"
    legal_suffix_decision = await unbound.resolve(legal_suffix_answer)
    assert legal_suffix_decision.verdict is BrandVerdict.YES
    assert legal_suffix_decision.method == "derived_legal_exact"
    assert legal_suffix_decision.matched_alias == "岱林生物技术股份有限公司"
    assert verifier_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legal_name", "generic_phrase"),
    [
        ("北京激光科技有限公司", "激光科技是重要的发展方向。"),
        ("上海光电科技有限公司", "光电科技是重要的发展方向。"),
        ("深圳半导体科技有限公司", "半导体科技是重要的发展方向。"),
        ("苏州科技有限公司", "推荐：苏州科技，值得关注。"),
    ],
)
async def test_generic_only_legal_short_form_requires_verifier(
    legal_name: str,
    generic_phrase: str,
):
    verifier_calls = 0

    async def reject_generic_phrase(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.NO, "generic-industry-phrase")

    decision = await BrandIdentityResolver(
        _identity(legal_name),
        verifier=reject_generic_phrase,
    ).resolve(generic_phrase)

    assert decision.verdict is BrandVerdict.NO
    assert decision.method == "deepseek_v4_flash_structured"
    assert verifier_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "候选企业包括江苏岱林生物，其设备方案值得关注。",
        "候选企业包括江苏岱林生物技术股份有限公司，其设备方案值得关注。",
        "候选企业包括岱林生物医药有限公司，其设备方案值得关注。",
        "候选企业应具备生物技术能力。",
    ],
)
async def test_derived_legal_short_name_rejects_competitors_and_generic_terms(answer: str):
    verifier_calls = 0

    async def reject_ambiguous_candidate(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.NO, "not-the-target-brand")

    resolver = BrandIdentityResolver(
        _identity("浙江岱林生物技术股份有限公司"),
        verifier=reject_ambiguous_candidate,
    )
    decision = await resolver.resolve(answer)

    assert decision.verdict is BrandVerdict.NO
    assert decision.matched_alias is None
    assert verifier_calls <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("canonical_name", "unsafe_short_name"),
    [
        ("浙江岱林生物股份有限公司", "浙江岱林"),
        ("深圳市晨光富士电梯有限公司", "晨光富士"),
    ],
)
async def test_legal_abbreviation_keeps_industry_nouns_that_distinguish_brands(
    canonical_name: str,
    unsafe_short_name: str,
):
    verifier_calls = 0

    async def reject_unsafe_shortening(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.NO, "industry-noun-is-identity")

    answer = f"候选企业包括{unsafe_short_name}，建议进一步核验主体。"
    decision = await BrandIdentityResolver(
        _identity(canonical_name),
        verifier=reject_unsafe_shortening,
    ).resolve(answer)

    assert decision.verdict is BrandVerdict.NO
    assert decision.method == "deepseek_v4_flash_structured"
    assert verifier_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "matched_text",
    ["浙江岱林生物技术", "岱林生物技术", "浙江岱林生物", "岱林生物"],
)
async def test_conservative_legal_name_abbreviation_does_not_require_official_verifier(
    matched_text: str,
):
    answer = f"候选企业包括{matched_text}，其隔离设备方案值得关注。"
    verifier_calls = 0

    async def must_not_verify(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("safe legal abbreviations are deterministic")

    identity = BrandIdentity(
        brand_id=596,
        canonical_names=("浙江岱林生物技术股份有限公司",),
        industry="生物技术",
    )
    decision = await BrandIdentityResolver(identity, verifier=must_not_verify).resolve(answer)

    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias == matched_text
    assert answer[decision.matched_start:decision.matched_end] == matched_text
    assert decision.method == "derived_legal_exact"
    assert verifier_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer_name", "expected_match"),
    [
        ("岱林生物（DAILIN）", "岱林生物"),
        ("浙江岱林生物", "浙江岱林生物"),
    ],
)
async def test_legal_abbreviation_returns_exact_source_span_without_verifier_offsets(
    answer_name: str,
    expected_match: str,
):
    answer = f"候选企业的隔离设备适配能力较强，推荐{answer_name}，建议进一步评估。"
    verifier_calls = 0

    async def verifier(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("deterministic legal names do not need provider offsets")

    identity = BrandIdentity(
        brand_id=596,
        canonical_names=("浙江岱林生物技术股份有限公司",),
        industry="生物技术",
    )
    decision = await BrandIdentityResolver(identity, verifier=verifier).resolve(answer)

    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias == expected_match
    assert answer[decision.matched_start:decision.matched_end] == expected_match
    assert verifier_calls == 0


@pytest.mark.asyncio
async def test_wrong_provider_offsets_do_not_guess_between_duplicate_matches():
    matched_text = "雅栖酒占"
    answer = f"{matched_text}是误写，稍后又出现{matched_text}。"

    async def verifier(**_kwargs):
        return VerificationResult(
            BrandVerdict.YES,
            "ambiguous-provider-offset",
            matched_text=matched_text,
            window_index=1,
            matched_start=999,
            matched_end=1003,
        )

    decision = await BrandIdentityResolver(
        _identity("雅栖酒店"),
        verifier=verifier,
    ).resolve(answer)

    assert decision.verdict is BrandVerdict.UNKNOWN
    assert decision.reason == "invalid_matched_text"


@pytest.mark.asyncio
async def test_parenthetical_trusted_name_cannot_launder_wrong_outside_company():
    matched_text = "江苏富士电梯（晨光富士电梯）"
    answer = f"候选名单包含{matched_text}，建议进一步核验。"

    async def verifier(**_kwargs):
        return VerificationResult(
            BrandVerdict.YES,
            "provider-confused-parenthetical-identity",
            matched_text=matched_text,
            window_index=1,
            matched_start=answer.index(matched_text),
            matched_end=answer.index(matched_text) + len(matched_text),
        )

    decision = await BrandIdentityResolver(
        _identity("深圳市晨光富士电梯有限公司"),
        verifier=verifier,
    ).resolve(answer)

    assert decision.verdict is BrandVerdict.UNKNOWN
    assert decision.reason == "invalid_matched_text"


@pytest.mark.asyncio
@pytest.mark.parametrize("matched_text", ["江苏富士电梯", "富士电梯"])
async def test_verified_competitor_or_over_shortened_name_cannot_bypass_proprietary_prefix(
    matched_text: str,
):
    answer = f"推荐{matched_text}，覆盖多个城市。"

    async def verifier(**kwargs):
        window = kwargs["evidence_windows"][0]
        start = window.index(matched_text)
        return VerificationResult(
            BrandVerdict.YES,
            "simulated-provider-error",
            matched_text=matched_text,
            window_index=1,
            matched_start=start,
            matched_end=start + len(matched_text),
        )

    identity = BrandIdentity(
        brand_id=592,
        canonical_names=("深圳市晨光富士电梯有限公司",),
        industry="电梯",
    )
    decision = await BrandIdentityResolver(identity, verifier=verifier).resolve(answer)

    assert decision.verdict is BrandVerdict.UNKNOWN
    assert decision.reason == "invalid_matched_text"


@pytest.mark.asyncio
async def test_generic_technology_suffix_does_not_erase_proprietary_prefix():
    matched_text = "富士电梯"
    answer = f"推荐{matched_text}，覆盖多个城市。"

    async def verifier(**kwargs):
        window = kwargs["evidence_windows"][0]
        start = window.index(matched_text)
        return VerificationResult(
            BrandVerdict.YES,
            "simulated-provider-error",
            matched_text=matched_text,
            window_index=1,
            matched_start=start,
            matched_end=start + len(matched_text),
        )

    identity = BrandIdentity(
        brand_id=592,
        canonical_names=("江苏晨光富士电梯技术有限公司",),
        industry="电梯",
    )
    decision = await BrandIdentityResolver(identity, verifier=verifier).resolve(answer)

    assert decision.verdict is BrandVerdict.UNKNOWN
    assert decision.reason == "invalid_matched_text"


@pytest.mark.asyncio
async def test_atour_single_character_typo_can_be_verified_yes_from_nearby_window():
    seen_windows: list[str] = []

    async def verifier(**kwargs):
        seen_windows.extend(kwargs["evidence_windows"])
        return VerificationResult(
            BrandVerdict.YES,
            "single-character-typo",
            matched_text="雅栖酒占",
        )

    resolver = BrandIdentityResolver(_identity("雅栖酒店"), verifier=verifier)
    decision = await resolver.resolve("这次推荐雅栖酒占，服务稳定。")

    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias == "雅栖酒占"
    assert decision.matched_start == 4
    assert decision.matched_end == 8
    assert any("雅栖酒占" in window for window in seen_windows)


@pytest.mark.asyncio
async def test_verified_duplicate_text_uses_declared_window_span_not_first_global_hit():
    answer = "雅栖酒占是误写。" + "中段" * 600 + "最终推荐雅栖酒占。"

    async def verifier(**kwargs):
        windows = kwargs["evidence_windows"]
        window_index = next(
            index for index, window in enumerate(windows, 1) if "最终推荐雅栖酒占" in window
        )
        window = windows[window_index - 1]
        start = window.index("雅栖酒占", window.index("最终推荐"))
        return VerificationResult(
            BrandVerdict.YES,
            "declared-source-span",
            matched_text="雅栖酒占",
            window_index=window_index,
            matched_start=start,
            matched_end=start + len("雅栖酒占"),
        )

    decision = await BrandIdentityResolver(_identity("雅栖酒店"), verifier=verifier).resolve(answer)

    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_start == answer.rindex("雅栖酒占")
    assert answer[decision.matched_start:decision.matched_end] == "雅栖酒占"


@pytest.mark.asyncio
async def test_verified_duplicate_text_without_evidence_coordinates_is_unknown():
    async def verifier(**_kwargs):
        return VerificationResult(
            BrandVerdict.YES,
            "ambiguous-source",
            matched_text="雅栖酒占",
        )

    answer = "雅栖酒占是误写。后来仍写成雅栖酒占。"
    decision = await BrandIdentityResolver(_identity("雅栖酒店"), verifier=verifier).resolve(answer)
    assert decision.verdict is BrandVerdict.UNKNOWN
    assert decision.reason == "invalid_matched_text"


@pytest.mark.asyncio
async def test_short_alias_requires_brand_context_and_industry_term_is_not_brand():
    async def verifier(**_kwargs):
        return VerificationResult(BrandVerdict.NO, "not-a-brand-entity")

    atour = BrandIdentityResolver(_identity("雅栖酒店", aliases=("雅栖",)), verifier=verifier)
    assert (await atour.resolve("推荐雅栖酒店")).verdict is BrandVerdict.YES
    assert (await atour.resolve("这是雅栖儿的个人昵称")).verdict is BrandVerdict.NO

    manufacturing = BrandIdentityResolver(
        BrandIdentity(17, ("制造业",), industry="制造业"),
        verifier=verifier,
    )
    assert (await manufacturing.resolve("制造业正在转型升级")).verdict is BrandVerdict.NO


@pytest.mark.asyncio
async def test_structured_yes_without_valid_source_substring_is_unknown():
    async def verifier(**_kwargs):
        return VerificationResult(
            BrandVerdict.YES,
            "hallucinated-evidence",
            matched_text="雅栖酒店",
        )

    decision = await BrandIdentityResolver(
        _identity("雅栖酒店"),
        verifier=verifier,
    ).resolve("这次推荐雅栖酒占，服务稳定。")

    assert decision.verdict is BrandVerdict.UNKNOWN
    assert decision.reason == "invalid_matched_text"
    assert decision.matched_alias is None


@pytest.mark.asyncio
async def test_short_alias_one_character_fuzzy_match_is_rejected():
    async def verifier(**_kwargs):
        return VerificationResult(
            BrandVerdict.YES,
            "unsafe-short-fuzzy",
            matched_text="雅栖占",
        )

    decision = await BrandIdentityResolver(
        _identity("雅栖店"),
        verifier=verifier,
    ).resolve("这次推荐雅栖占。")

    assert decision.verdict is BrandVerdict.UNKNOWN
    assert decision.reason == "invalid_matched_text"


@pytest.mark.asyncio
async def test_different_address_atour_store_is_no():
    async def verifier(**_kwargs):
        return VerificationResult(BrandVerdict.NO, "different-store-address")

    resolver = BrandIdentityResolver(_identity("北京望京雅栖酒店"), verifier=verifier)
    decision = await resolver.resolve("推荐上海虹桥雅栖酒店，靠近会展中心。")
    assert decision.verdict is BrandVerdict.NO


@pytest.mark.parametrize(
    "answer",
    [
        "商务出差可以选择滨江南路雅栖酒店，停车和洗衣更方便。",
        "本次推荐揭阳阁滨江南路雅栖酒店，适合连住差旅。",
    ],
)
@pytest.mark.asyncio
async def test_location_storefront_variants_are_deterministic(answer: str):
    verifier_calls = 0

    async def must_not_verify(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("bounded storefront variants must not spend a verifier call")

    decision = await BrandIdentityResolver(
        _identity("揭阳滨江南路雅栖酒店"),
        verifier=must_not_verify,
    ).resolve(answer)

    assert decision.verdict is BrandVerdict.YES
    assert decision.reason == "bounded_storefront_variant"
    assert decision.method == "derived_storefront_exact"
    assert decision.matched_alias in {"滨江南路雅栖酒店", "揭阳阁滨江南路雅栖酒店"}
    assert answer[decision.matched_start:decision.matched_end] == decision.matched_alias
    assert verifier_calls == 0


@pytest.mark.parametrize(
    "answer",
    [
        "推荐上海虹桥雅栖酒店，靠近会展中心。",
        "推荐北京雅栖酒店，交通方便。",
        "雅栖酒店适合商务出行。",
        "滨江南路酒店选择很多。",
        "推荐普宁滨江南路雅栖酒店，适合当地出行。",
    ],
)
@pytest.mark.asyncio
async def test_storefront_variant_rule_does_not_collapse_distinct_properties(answer: str):
    verifier_calls = 0

    async def reject_unconfirmed_property(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.NO, "different-or-insufficient-property")

    decision = await BrandIdentityResolver(
        _identity("揭阳滨江南路雅栖酒店"),
        verifier=reject_unconfirmed_property,
    ).resolve(answer)

    assert decision.verdict is BrandVerdict.NO
    assert decision.reason in {"different-or-insufficient-property", "no_identity_candidate"}
    assert decision.method != "derived_storefront_exact"
    assert verifier_calls in {0, 1}


@pytest.mark.parametrize(
    ("target", "answer"),
    [
        ("晨光富士电梯", "推荐江苏富士电梯，覆盖多个城市。"),
        ("江苏富士电梯", "晨光富士电梯提供维保服务。"),
    ],
)
@pytest.mark.asyncio
async def test_same_industry_suffix_different_company_is_no(target: str, answer: str):
    async def verifier(**_kwargs):
        return VerificationResult(BrandVerdict.NO, "different-proprietary-prefix")

    decision = await BrandIdentityResolver(_identity(target), verifier=verifier).resolve(answer)
    assert decision.verdict is BrandVerdict.NO


@pytest.mark.asyncio
async def test_verifier_timeout_is_unknown_not_no():
    async def timeout_verifier(**_kwargs):
        raise TimeoutError("simulated")

    resolver = BrandIdentityResolver(_identity("雅栖酒店"), verifier=timeout_verifier)
    decision = await resolver.resolve("本次提到雅栖酒占，可能是输入错误。")
    assert decision.verdict is BrandVerdict.UNKNOWN


@pytest.mark.asyncio
async def test_identity_read_failure_is_unknown():
    identity = BrandIdentity(
        brand_id=17,
        canonical_names=(),
        load_error=True,
    )
    decision = await BrandIdentityResolver(identity).resolve("任何回答")
    assert decision.verdict is BrandVerdict.UNKNOWN


@pytest.mark.asyncio
async def test_connection_acquisition_failure_is_unknown(monkeypatch):
    import db.connection

    def fail_connection():
        raise RuntimeError("isolated pool unavailable")

    monkeypatch.setattr(db.connection, "get_connection", fail_connection)
    identity = load_brand_identity(17, fallback_name="不应被信任的请求名")

    assert identity.load_error is True
    assert identity.canonical_names == ()
    decision = await BrandIdentityResolver(identity).resolve("不应被信任的请求名")
    assert decision.verdict is BrandVerdict.UNKNOWN


@pytest.mark.asyncio
async def test_ai_tester_uses_verified_typo_span_for_position(monkeypatch):
    from services import brand_identity_resolver
    from tools.ai_visibility import ai_tester

    async def verifier(**_kwargs):
        return VerificationResult(
            BrandVerdict.YES,
            "single-character-typo",
            matched_text="雅栖酒占",
        )

    # [P1-7 2026-07-26] 抽取器新增 status_sink（区分"真没同行"与"抽取失败"），
    #   test double 跟着放开 kwargs；行为不变（仍返回空名单）。
    async def no_competitors(_answer, *_args, **_kwargs):
        return []

    monkeypatch.setattr(brand_identity_resolver, "_default_structured_verifier", verifier)
    monkeypatch.setattr(ai_tester, "_extract_mentioned_brands_llm", no_competitors)
    answer = "第一行\n这次推荐雅栖酒占，服务稳定。"

    result = await ai_tester._analyze_visibility(
        answer,
        "酒店推荐",
        "雅栖酒店",
        "deepseek",
    )

    assert result["brand_verdict"] == "YES"
    assert result["matched_text"] == "雅栖酒占"
    assert result["brand_position"] == answer.index("雅栖酒占")
    assert result["is_recommended"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["dashscope", "deepseek", "doubao", "yuanbao", "kimi"])
async def test_ai_tester_counts_verified_legal_abbreviation_instead_of_engine_error(
    monkeypatch,
    engine,
):
    from services import brand_identity_resolver
    from tools.ai_visibility import ai_tester

    matched_text = "岱林生物"
    answer = f"细胞治疗设备供应商包括{matched_text}，其隔离系统值得关注。"

    verifier_calls = 0

    async def must_not_verify(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("production-shaped legal abbreviation must be deterministic")

    # [P1-7 2026-07-26] 抽取器新增 status_sink（区分"真没同行"与"抽取失败"），
    #   test double 跟着放开 kwargs；行为不变（仍返回空名单）。
    async def no_competitors(_answer, *_args, **_kwargs):
        return []

    def trusted_identity(_brand_id, **_kwargs):
        return BrandIdentity(
            brand_id=596,
            canonical_names=("浙江岱林生物技术股份有限公司",),
            industry="生物技术",
        )

    monkeypatch.setattr(brand_identity_resolver, "_default_structured_verifier", must_not_verify)
    monkeypatch.setattr(brand_identity_resolver, "load_brand_identity", trusted_identity)
    monkeypatch.setattr(ai_tester, "_extract_mentioned_brands_llm", no_competitors)

    result = await ai_tester._analyze_visibility(
        answer,
        "细胞治疗药物研发和生产隔离器推荐",
        "浙江岱林生物技术股份有限公司",
        engine,
        brand_id=596,
    )

    assert result["brand_verdict"] == "YES"
    assert result["brand_detected"] is True
    assert result["matched_text"] == matched_text
    assert result.get("engine_error") is not True
    assert "brand_detection_unknown" not in result
    assert verifier_calls == 0


@pytest.mark.asyncio
async def test_ai_tester_does_not_retry_after_provider_returned_429(monkeypatch):
    from services import brand_identity_resolver
    from tools.ai_visibility import ai_tester

    verifier_calls = 0
    answer = "候选名单包括雅栖酒占，建议核验门店位置。"

    async def verifier(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.UNKNOWN, "http_429")

    # [P1-7 2026-07-26] 抽取器新增 status_sink（区分"真没同行"与"抽取失败"），
    #   test double 跟着放开 kwargs；行为不变（仍返回空名单）。
    async def no_competitors(_answer, *_args, **_kwargs):
        return []

    monkeypatch.setattr(brand_identity_resolver, "_default_structured_verifier", verifier)
    monkeypatch.setattr(
        brand_identity_resolver,
        "load_brand_identity",
        lambda *_args, **_kwargs: _identity("雅栖酒店"),
    )
    monkeypatch.setattr(ai_tester, "_extract_mentioned_brands_llm", no_competitors)

    result = await ai_tester._analyze_visibility(
        answer,
        "酒店推荐",
        "雅栖酒店",
        "doubao",
        brand_id=17,
    )

    assert verifier_calls == 1
    assert result["brand_detection_attempts"] == 1
    assert result["brand_verdict"] == "UNKNOWN"
    assert result["error_code"] == "brand_identity_unresolved"


@pytest.mark.asyncio
async def test_ai_tester_retries_identity_once_when_provider_request_was_not_sent(monkeypatch):
    from services import brand_identity_resolver
    from tools.ai_visibility import ai_tester

    verifier_calls = 0
    answer = "候选名单包括雅栖酒占，建议核验门店位置。"

    async def verifier(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        if verifier_calls == 1:
            return VerificationResult(BrandVerdict.UNKNOWN, "provider_not_sent")
        return VerificationResult(
            BrandVerdict.YES,
            "single-character-typo",
            matched_text="雅栖酒占",
        )

    # [P1-7 2026-07-26] 抽取器新增 status_sink（区分"真没同行"与"抽取失败"），
    #   test double 跟着放开 kwargs；行为不变（仍返回空名单）。
    async def no_competitors(_answer, *_args, **_kwargs):
        return []

    monkeypatch.setattr(brand_identity_resolver, "_default_structured_verifier", verifier)
    monkeypatch.setattr(
        brand_identity_resolver,
        "load_brand_identity",
        lambda *_args, **_kwargs: _identity("雅栖酒店"),
    )
    monkeypatch.setattr(ai_tester, "_extract_mentioned_brands_llm", no_competitors)

    result = await ai_tester._analyze_visibility(
        answer,
        "酒店推荐",
        "雅栖酒店",
        "doubao",
        brand_id=17,
    )

    assert verifier_calls == 2
    assert result["brand_detection_attempts"] == 2
    assert result["identity_retry_count"] == 1
    assert result["brand_verdict"] == "YES"
    assert result["matched_text"] == "雅栖酒占"


@pytest.mark.asyncio
async def test_ai_tester_marks_request_not_sent_retry_exhaustion(monkeypatch):
    from services import brand_identity_resolver
    from tools.ai_visibility import ai_tester

    verifier_calls = 0

    async def verifier(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.UNKNOWN, "provider_not_sent")

    monkeypatch.setattr(brand_identity_resolver, "_default_structured_verifier", verifier)
    monkeypatch.setattr(
        brand_identity_resolver,
        "load_brand_identity",
        lambda *_args, **_kwargs: _identity("雅栖酒店"),
    )

    result = await ai_tester._analyze_visibility(
        "候选名单包括雅栖酒占。",
        "酒店推荐",
        "雅栖酒店",
        "doubao",
        brand_id=17,
    )

    assert verifier_calls == 2
    assert result["brand_detection_attempts"] == 2
    assert result["brand_verdict"] == "UNKNOWN"
    assert result["error_code"] == "brand_identity_retry_exhausted"


@pytest.mark.asyncio
async def test_ai_tester_does_not_replay_provider_result_unknown(monkeypatch):
    from services import brand_identity_resolver
    from tools.ai_visibility import ai_tester

    verifier_calls = 0

    async def verifier(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        return VerificationResult(BrandVerdict.UNKNOWN, "provider_result_unknown")

    monkeypatch.setattr(brand_identity_resolver, "_default_structured_verifier", verifier)
    monkeypatch.setattr(
        brand_identity_resolver,
        "load_brand_identity",
        lambda *_args, **_kwargs: _identity("雅栖酒店"),
    )

    result = await ai_tester._analyze_visibility(
        "候选名单包括雅栖酒占。",
        "酒店推荐",
        "雅栖酒店",
        "doubao",
        brand_id=17,
    )

    assert verifier_calls == 1
    assert result["brand_detection_attempts"] == 1
    assert result["brand_verdict"] == "UNKNOWN"
    assert result["engine_error"] is True
    assert result["error_code"] == "brand_identity_unresolved"


@pytest.mark.asyncio
async def test_monitoring_default_matrix_matches_single_engine_source(
    monkeypatch,
):
    """[P0-2 · Owner 2026-07-26 裁决] 监测默认矩阵 = 统一五引擎。

    旧断言是"矩阵 = classic4 且不含元宝"（元宝当时只属付费诊断）。Owner 已裁决
    诊断与监测统一五引擎，元宝进监测执行矩阵。断言改为与唯一常量源
    ``config.ai_engines`` 对齐 —— 改常量测试跟着变，这正是 P0-2 要防的双源打架。
    """
    from config.ai_engines import MONITORING_ENGINES
    from db.monitoring_db import DEFAULT_MONITORING_PLATFORMS
    from tools.monitoring.batch_monitor import PlatformAdapter

    calls: list[dict] = []

    async def fake_query(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=[{
            "type": "text",
            "text": json.dumps({
                "response": "推荐岱林生物（DAILIN）。",
                "brand_detected": True,
                "brand_verdict": "YES",
                "matched_text": "岱林生物",
                "platform_key": "yuanbao",
                "surface_key": "yuanbao_hy3_tokenhub",
            }, ensure_ascii=False),
        }])

    configured = tuple(DEFAULT_MONITORING_PLATFORMS.split(","))
    assert configured == MONITORING_ENGINES
    # 每个默认平台都必须真的有采集实现，否则会被静默剔除（P0-2 复发形态）
    for platform in configured:
        assert platform in PlatformAdapter.SUPPORTED_PLATFORMS, platform

    monkeypatch.setattr(
        PlatformAdapter,
        "SUPPORTED_PLATFORMS",
        {platform: fake_query for platform in configured},
    )
    # [2026-08-04] 裁 eligible 还要过账本可执行面(Owner 裁决先取 classic4 四路)。
    # 这里把两件事拆开断言，因为它们坏起来是两回事：
    #   · eligible 少了谁 → 客户看到的引擎清单和实跑不一致（本测试原本的题目）；
    #   · query 少带观测参数 → 元宝那格的血缘写不进观测 SSOT（下面那段的题目）。
    # 观测 SSOT 那段必须继续覆盖**元宝**，所以下面的循环走 configured（已售矩阵）
    # 而不是 eligible —— 否则元宝被账本面裁掉，这段覆盖会跟着悄悄消失。
    from config.ai_engines import MONITORING_RUN_CELL_PLATFORMS

    eligible = PlatformAdapter.eligible_monitoring_platforms(configured)
    assert eligible == [p for p in configured if p in MONITORING_RUN_CELL_PLATFORMS]
    assert "yuanbao" not in eligible, "账本存不下元宝单元，监测不得把它排进实跑面"

    for platform in configured:
        result = await PlatformAdapter.query(
            platform=platform,
            question="细胞治疗药物研发和生产隔离器推荐",
            target_brand="浙江岱林生物技术股份有限公司",
            brand_id=596,
            user_id=29,
            monitoring_task_id=454,
            keyword_id=9001,
            keyword="细胞治疗药物研发和生产隔离器推荐",
        )
        assert result["status"] == "success"

    assert len(calls) == len(configured)
    assert {call["brand_id"] for call in calls} == {596}
    assert {call["check_brand"] for call in calls} == {
        "浙江岱林生物技术股份有限公司"
    }
    # 元宝走统一观测 SSOT：监测语境必须**显式**声明 source_kind='monitoring'，
    #   不得借用 collect_paid_delivery 的 ingest 豁免（service 侧会 fail-closed）。
    #   其余平台是直连 provider，不带观测参数。
    yuanbao_calls = [c for c in calls if c.get("observation_source_kind")]
    assert len(yuanbao_calls) == 1
    assert yuanbao_calls[0]["observation_source_kind"] == "monitoring"
    assert yuanbao_calls[0]["observation_round_id"]
    assert yuanbao_calls[0]["observation_request_id"]
    assert len([c for c in calls if "observation_source_kind" not in c]) == len(configured) - 1


@pytest.mark.asyncio
async def test_daily_monitoring_filters_non_collectable_platforms(
    monkeypatch,
):
    from api import monitoring_api

    calls: list[str] = []

    async def fake_query(**kwargs):
        calls.append(kwargs["platform"])
        return {
            "status": "success",
            "is_detected": True,
            "mention_type": "direct",
            "full_response": "推荐岱林生物。",
        }

    monkeypatch.setattr(monitoring_api.PlatformAdapter, "query", fake_query)

    result = await monitoring_api.run_detection_for_keyword(
        keyword="细胞治疗药物研发和生产隔离器推荐",
        target_brand="浙江岱林生物技术股份有限公司",
        brand_id=596,
        user_id=29,
    )

    # [P0-2 2026-07-26] 日常监测按统一五引擎跑；顺序取配置里的顺序。
    # [2026-08-04 订正] 实跑面收敛到账本可执行面（Owner 裁决先取 classic4 四路）：
    #   monitoring_run_cells 的 CHECK 存不下元宝，排进去只会让整批炸掉。
    #   本测试的题目("filters non collectable platforms")没变，变的是"什么叫
    #   non-collectable" —— 现在它包含"账本存不下"这一类。
    from config.ai_engines import MONITORING_ENGINES, MONITORING_RUN_CELL_PLATFORMS

    expected = [p for p in MONITORING_ENGINES if p in MONITORING_RUN_CELL_PLATFORMS]
    assert set(calls) == set(expected)
    assert len(calls) == len(expected)
    assert result["total_tested"] == len(expected)
    # 成对反向对照：不是"随便少跑几个都算过"，少跑的必须**恰好**是账本存不下的那些。
    assert set(MONITORING_ENGINES) - set(calls) == {"yuanbao"}


def test_diagnosis_alias_lookup_is_bound_to_brand_id_without_global_fuzzy_fallback():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    block = source[source.index("async def _run_diagnosis_impl"):]
    block = block[:block.index("# Notify start")]

    assert "SELECT brand_display_names FROM brands WHERE id = %s" in block
    assert "WHERE name LIKE" not in block
    assert "request.brand_display_names" in block
    start_block = source[source.index("async def start_diagnosis"):]
    start_block = start_block[:start_block.index("async def start_diagnosis_alias")]
    assert "persist_confirmed_display_names" in start_block

    brand_api = (ROOT / "api" / "brand_api.py").read_text(encoding="utf-8")
    assert "_persist_confirmed_display_names(brand_id, req.brand_display_names)" in brand_api
    assert "brand_updates[\"brand_display_names\"]" not in brand_api
    assert "profile_updates[\"brand_display_names\"]" not in brand_api

    new_diagnosis = (
        ROOT / "frontend" / "src" / "pages" / "Diagnosis" / "NewDiagnosis.tsx"
    ).read_text(encoding="utf-8")
    assert "brandDisplayNames: newDisplayNames," in new_diagnosis
    assert "brandDisplayNames: newDisplayNames || prev.brandDisplayNames" not in new_diagnosis

    latest_params = source[source.index("def api_get_brand_latest_diagnosis_params"):]
    assert "if not has_display_name_ssot:" in latest_params
    assert 'if not result["brand_display_names"]:' not in latest_params


@pytest.mark.asyncio
async def test_unknown_is_excluded_from_visibility_denominator(monkeypatch):
    from tools.ai_visibility import ai_tester

    async def unknown_query(*_args, **_kwargs):
        payload = {
            "response": "雅栖酒占可能是输入错误",
            "brand_detected": False,
            "brand_verdict": "UNKNOWN",
            "engine_error": True,
            "mentioned_brands_inline": [],
        }
        return SimpleNamespace(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])

    monkeypatch.setattr(ai_tester, "query_dashscope_search", unknown_query)
    result = await ai_tester.detailed_ai_visibility_test(
        questions=["酒店推荐"],
        check_brand="雅栖酒店",
        engines=["dashscope"],
    )
    summary = result["brand_detection_summary"]
    assert summary["total_tests"] == 0
    assert summary["total_failed"] == 1
    assert result["detail_table"][0]["results"]["dashscope"]["status"] == "error"


# ══════════════════════════════════════════════════════════════════════
# [三轮复审 2026-08-07] 裸城市名穿透 · 全量行政区划名录
# ══════════════════════════════════════════════════════════════════════

#: 复审点名的十二城探针。上一版 `_ADMINISTRATIVE_PREFIXES` 只有 93 项手工枚举,
#: 340 个地级市缺 260+,这十二个**全部穿透成可信别名** —— 原 P0 根本没闭。
TWELVE_CITY_PROBES = (
    "中山", "珠海", "东莞", "佛山", "惠州", "汕头",
    "洛阳", "襄阳", "宜昌", "绵阳", "唐山", "保定",
)


@pytest.mark.parametrize("city", TWELVE_CITY_PROBES)
def test_bare_prefecture_city_names_never_become_aliases(city: str):
    """🔴【必须命中 · 三轮复审】裸地级市名不许成为可信别名。"""
    from services.brand_identity_resolver import _parenthetical_identity_alias

    assert _parenthetical_identity_alias(city) is None, f"{city} 穿透了"
    assert city not in split_brand_aliases(f"示例企业（{city}）")


@pytest.mark.parametrize("name", ["中山", "朝阳", "南山", "深圳南山", "内蒙古鄂尔多斯"])
def test_named_leak_cases_are_closed(name: str):
    """🔴【必须命中】复审专项点名的两个(中山/朝阳)+ 上一轮的已知残留。

    「深圳南山」由**名录全覆盖**判据关掉(深圳 + 南山 都在表里),
    而不是靠 `startswith(省/市)` —— 后者正是上一轮误杀六个真品牌的写法。
    """
    from services.brand_identity_resolver import _parenthetical_identity_alias

    assert _parenthetical_identity_alias(name) is None


def test_gazetteer_covers_the_probes_and_is_not_a_stub():
    """【反向对照】名录必须真的加载到全量,而不是悄悄退回 93 项窄表。

    没有这条,把 `_administrative_name_set` 写成 `return frozenset()` 之后
    上面那些用例会红 —— 但若有人改成"退回窄表且不报错",红的地方会散在各处
    难以定位。这里直接钉规模与关键覆盖。
    """
    from services.brand_identity_resolver import _administrative_name_set

    names = _administrative_name_set()
    assert len(names) > 400, f"名录只有 {len(names)} 项 —— 疑似退回了手工窄表"
    for city in TWELVE_CITY_PROBES:
        assert city in names


# ── 🔴 判据② 逐消费方 A/B:剥前缀两路行为必须零变化 ──────────────────────

#: 城市名开头的真实企业名(复审点名的形态)。剥前缀路一旦跟着换全量名录,
#: 「洛阳钼业」会被剥成「钼业」、「东莞证券」被剥成「证券」—— 另一个方向的事故。
CITY_PREFIXED_REAL_COMPANIES = (
    "洛阳钼业", "东莞证券", "中山公用", "唐山港", "保定天威",
    "佛山照明", "宜昌人福药业", "绵阳富临精工",
)


@pytest.mark.parametrize("name", CITY_PREFIXED_REAL_COMPANIES)
def test_prefix_stripping_consumers_are_untouched_by_the_gazetteer(name: str):
    """🔴【必须不命中 · 复审②】全量名录**只服务拒绝路**,不许流进剥前缀路。

    判据是行为不变:这些名字里的「洛阳/东莞/中山/唐山/保定/佛山/宜昌/绵阳」
    都**不在**窄表 `_administrative_prefix_variants()` 里,所以剥前缀必须
    原样返回。谁把两个集合合并了,这里立刻转红。
    """
    from services.brand_identity_resolver import (
        _legal_proprietary_core,
        _without_administrative_prefix,
    )

    assert _without_administrative_prefix(name) == name
    assert _legal_proprietary_core(name) == normalize_brand_name(name)


def test_narrow_prefix_table_is_not_widened():
    """🔴【必须不命中 · 复审②】窄表规模钉死 —— 防"顺手合并两个集合"。"""
    from services.brand_identity_resolver import (
        _administrative_name_set,
        _administrative_prefix_variants,
    )

    narrow = _administrative_prefix_variants()
    assert len(narrow) == 132, f"剥前缀窄表被改动了(现 {len(narrow)} 项,应 132)"
    assert len(_administrative_name_set()) > len(narrow), "两个集合必须是不同的东西"


def test_real_brands_survive_the_full_gazetteer():
    """🔴【反向对照】上全量名录后,六个真实品牌仍然必须全部放行。

    这是三轮里代价最高的一条:二轮我用 startswith(省/市) 把它们全杀了,
    还写成"有意接受"。名录变大 = 误杀风险变大,所以这条必须跟着名录一起跑。
    """
    from services.brand_identity_resolver import _parenthetical_identity_alias

    for brand in ("乡村基", "北京同仁堂", "上海家化", "广州酒家",
                  "青岛啤酒", "重庆小面"):
        assert _parenthetical_identity_alias(brand) == brand, f"{brand} 被误杀"


# ══════════════════════════════════════════════════════════════════════
# [三轮复查微补丁 2026-08-07] 地级建制的另外三类:自治州 / 盟 / 地区
# ══════════════════════════════════════════════════════════════════════

#: 复查抽样点名的十个穿透样本。494 表按"地级市"编,漏了自治州(30)/盟(3)/地区(7)
#: 以及两个新设地级市(三沙 2012 / 那曲 2017)。
PREFECTURE_GAP_PROBES = (
    "三沙", "那曲", "阿坝", "甘孜", "凉山",
    "黔东南", "黔南", "黔西南", "博尔塔拉", "巴音郭楞",
)

#: 名字里含地级建制名的**真实企业**。名单变大 = 误杀风险变大,
#: 所以每次动名录都必须跟着跑这一组。「延边敖东」是复查点名的那一个。
REAL_COMPANIES_WITH_PREFECTURE_NAMES = (
    "延边敖东", "阿里巴巴", "伊犁老窖", "昌吉农商",
    "大理药业", "和田玉都", "玉树藏药", "甘孜农牧",
)


@pytest.mark.parametrize("name", PREFECTURE_GAP_PROBES)
def test_autonomous_prefecture_league_and_region_names_are_rejected(name: str):
    """🔴【必须命中 · 三轮复查】自治州/盟/地区/新设地级市的裸简称不许成为别名。"""
    from services.brand_identity_resolver import _parenthetical_identity_alias

    assert _parenthetical_identity_alias(name) is None, f"{name} 穿透了"
    assert name not in split_brand_aliases(f"示例企业（{name}）")


@pytest.mark.parametrize("name", REAL_COMPANIES_WITH_PREFECTURE_NAMES)
def test_real_companies_named_after_prefectures_still_pass(name: str):
    """🔴【反向对照 · 三轮复查】补名单不许误杀这些真实企业。

    没有这一组,把新名单写成"含地级名即拒"也能让上面十条全绿 ——
    那就是二轮 startswith 事故的重演(只不过换了张更大的表)。
    """
    from services.brand_identity_resolver import _parenthetical_identity_alias

    assert _parenthetical_identity_alias(name) == name, f"{name} 被误杀"


def test_prefecture_extra_names_only_feed_the_reject_path():
    """🔴【必须不命中 · 三轮复查①】补的名字**只进拒绝路**,窄表一个字不动。"""
    from services.brand_identity_resolver import (
        _PREFECTURE_LEVEL_EXTRA_NAMES,
        _administrative_name_set,
        _administrative_prefix_variants,
    )

    narrow = _administrative_prefix_variants()
    assert len(narrow) == 132, f"剥前缀窄表被动了(现 {len(narrow)} 项)"
    for name in _PREFECTURE_LEVEL_EXTRA_NAMES:
        assert name not in narrow, f"{name} 混进了剥前缀窄表"
        assert normalize_brand_name(name) in _administrative_name_set()
