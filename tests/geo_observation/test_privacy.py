"""隐私清洗单元:URL→domain、PII scrub、匿名题族(R6 确定性)、query 分类、signal denylist 守卫。"""
from __future__ import annotations

import pytest

from services.geo_observation import privacy
from services.geo_observation.contracts import PromptIntent, QueryKind, Sentiment


def test_normalize_domain_strips_path_query_token():
    assert privacy.normalize_domain("https://www.example.com/a/b?token=secret&utm=x#frag") == "example.com"
    assert privacy.normalize_domain("http://Sub.Example.COM:8080/path") == "sub.example.com"
    assert privacy.normalize_domain("not a url") is None
    assert privacy.normalize_domain("http://127.0.0.1/x") is None   # 内网拒绝
    assert privacy.normalize_domain("http://localhost/x") is None


def test_clean_source_domains_dedup_and_no_url():
    cites = [
        {"url": "https://a.com/x?token=1", "source_type": "citation"},
        {"url": "https://a.com/y", "source_type": "citation"},   # 同 domain 去重
        {"url": "https://b.com/z", "source_type": "source"},
    ]
    out = privacy.clean_source_domains(cites)
    assert [d["domain"] for d in out] == ["a.com", "b.com"]
    for d in out:
        assert "/" not in d["domain"] and "?" not in d["domain"]


def test_scrub_pii():
    t = privacy.scrub_pii("联系13800138000 邮箱a@b.com 订单1234567890 装修")
    assert "13800138000" not in t and "a@b.com" not in t and "1234567890" not in t
    assert "装修" in t


def test_anonymize_prompt_family_key_strips_brand_and_pii_deterministic():
    k1 = privacy.anonymize_prompt_family_key("大昀装修在深圳口碑好吗 电话13800138000", ["大昀装修"], "装修")
    k2 = privacy.anonymize_prompt_family_key("大昀装修在深圳口碑好吗 电话13800138000", ["大昀装修"], "装修")
    assert k1 == k2                       # 确定性
    assert "大昀装修" not in k1            # 品牌剥离
    assert "13800138000" not in k1        # PII 剥离
    assert k1.startswith("装修:")         # 行业前缀


def test_query_kind_and_intent():
    assert privacy.classify_query_kind("大昀装修怎么样", ["大昀装修"]) == QueryKind.branded
    assert privacy.classify_query_kind("深圳装修哪家好", []) == QueryKind.comparative
    assert privacy.classify_query_kind("深圳有哪些装修公司", []) == QueryKind.non_branded
    assert privacy.derive_prompt_intent("装修多少钱", False) == PromptIntent.transaction
    assert privacy.derive_prompt_intent("这家装修有投诉吗靠谱吗", False) == PromptIntent.risk
    assert privacy.derive_prompt_intent("推荐几家装修公司", False) == PromptIntent.category_recommendation


def test_sentiment():
    assert privacy.derive_sentiment("推荐,口碑好") == Sentiment.positive
    assert privacy.derive_sentiment("不推荐,差评多") == Sentiment.negative
    assert privacy.derive_sentiment("有推荐也有投诉") == Sentiment.mixed


def test_assert_signal_clean_rejects_forbidden():
    with pytest.raises(ValueError):
        privacy.assert_signal_clean({"owner_user_id": 1})
    with pytest.raises(ValueError):
        privacy.assert_signal_clean({"answer_text": "..."})
    with pytest.raises(ValueError):
        privacy.assert_signal_clean({"source_domains": [{"domain": "a.com/path"}]})
    with pytest.raises(ValueError):
        privacy.assert_signal_clean({"prompt_family_key": "联系13800138000"})
    # 干净 signal 通过
    privacy.assert_signal_clean({"prompt_family_key": "装修:哪家好", "source_domains": [{"domain": "a.com"}],
                                 "search_query_theme_keys": ["装修-推荐"]})
