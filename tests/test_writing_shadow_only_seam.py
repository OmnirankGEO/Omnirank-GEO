import hashlib

import pytest


def test_shadow_injection_flags_default_to_off_and_shadow_only():
    from writing.shadow_only_injection import load_shadow_injection_config

    config = load_shadow_injection_config(env={})

    assert config.enabled is False
    assert config.shadow_only is True
    assert config.customer_output_enabled is False


def test_shadow_route_stays_disabled_when_feature_flag_is_off():
    from writing.shadow_only_injection import load_shadow_injection_config, plan_shadow_route

    config = load_shadow_injection_config(env={})
    route = plan_shadow_route(
        case={"case_id": "C1", "risk_level": "low"},
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        config=config,
    )

    assert route["shadow_injection_route"] == "disabled"
    assert route["customer_output_allowed"] is False
    assert "feature_flag_off" in route["blockers"]


def test_high_liability_always_routes_to_manual_review_even_when_judges_pass():
    from writing.shadow_only_injection import load_shadow_injection_config, plan_shadow_route

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
        }
    )
    route = plan_shadow_route(
        case={"case_id": "HL1", "risk_level": "high", "high_liability_vertical": True},
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        metric={"decision": "pass"},
        config=config,
    )

    assert route["shadow_injection_route"] == "manual_review"
    assert route["manual_review_required"] is True
    assert route["customer_output_allowed"] is False
    assert "high_liability_manual_review" in route["manual_review_reasons"]


def test_shadow_route_reads_high_liability_from_route_context():
    from writing.shadow_only_injection import load_shadow_injection_config, plan_shadow_route

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
        }
    )
    route = plan_shadow_route(
        case={"case_id": "HL_ROUTE", "risk_level": "low"},
        route={"risk_level": "high"},
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        config=config,
    )

    assert route["shadow_injection_route"] == "manual_review"
    assert route["manual_review_required"] is True
    assert "high_liability_manual_review" in route["manual_review_reasons"]


def test_customer_output_gate_blocks_semantic_pass_without_route_and_sidecar_contract():
    from writing.shadow_only_injection import evaluate_customer_output_gate

    article = "岱林生物具备完整能力。"
    result = evaluate_customer_output_gate(
        case={"case_id": "C2", "evidence_mode": "with_evidence", "customer_article_mode": "stripped"},
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": False, "manual_review_required": False},
    )

    assert result["customer_output_allowed"] is False
    assert "route_customer_output_not_allowed" in result["blockers"]
    assert "missing_stripped_safe_binding_mode" in result["blockers"]
    assert "missing_evidence_bindings" in result["blockers"]


def test_customer_output_gate_blocks_missing_route_even_when_customer_flag_is_on():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": "NO_ROUTE", "evidence_mode": "no_evidence", "risk_level": "low"},
        article_text="这是一篇干净的无证据采购指南。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "route_missing" in result["blockers"]


@pytest.mark.parametrize(
    "risk_level",
    ["high risk", "high-risk", "high_liability_pharma", "highrisk"],
)
def test_customer_output_gate_blocks_high_liability_label_variants(risk_level):
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": f"RISK_{risk_level}", "evidence_mode": "no_evidence", "risk_level": risk_level},
        article_text="这是一篇干净的无证据采购指南。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "high_liability_manual_review_required" in result["blockers"]


@pytest.mark.parametrize(
    "risk_level",
    ["critical", "P0", "severe", "elevated", "高风险", "ＨＩＧＨ", "hi\u200bgh"],
)
def test_customer_output_gate_blocks_unknown_or_unicode_risk_labels(risk_level):
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": f"UNKNOWN_RISK_{risk_level}", "evidence_mode": "no_evidence", "risk_level": risk_level},
        article_text="这是一篇干净的无证据采购指南。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert (
        "risk_level_unrecognized" in result["blockers"]
        or "high_liability_manual_review_required" in result["blockers"]
    )


@pytest.mark.parametrize(
    ("guard_decision", "semantic_decision", "metric_decision", "expected_blocker"),
    [
        ("Blocked", "pass", "pass", "guard_blocked"),
        ("Retry-Required", "pass", "pass", "guard_retry_required"),
        ("pass", "Incomplete", "pass", "semantic_incomplete"),
        ("pass", "pass", "Manual_review", "metric_retention_manual_review"),
    ],
)
def test_customer_output_gate_normalizes_decision_tokens_before_checking_blocks(
    guard_decision, semantic_decision, metric_decision, expected_blocker
):
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": "NORMALIZE_DECISION", "evidence_mode": "no_evidence", "risk_level": "low"},
        article_text="这是一篇干净的无证据采购指南。",
        guard={"shadow_decision": guard_decision},
        semantic={
            "semantic_decision": semantic_decision,
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        metric={"decision": metric_decision},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert expected_blocker in result["blockers"]


def test_customer_output_gate_blocks_unknown_guard_and_semantic_decisions():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": "UNKNOWN_DECISION", "evidence_mode": "no_evidence", "risk_level": "low"},
        article_text="这是一篇干净的无证据采购指南。",
        guard={"shadow_decision": "looks_good"},
        semantic={
            "semantic_decision": "looks_good",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "guard_unrecognized_decision" in result["blockers"]
    assert "semantic_unrecognized_decision" in result["blockers"]


@pytest.mark.parametrize("evidence_mode", ["With_Evidence", "WITH_EVIDENCE", "with_evidence "])
def test_customer_output_gate_normalizes_evidence_mode_before_binding_checks(evidence_mode):
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={
            "case_id": "EVIDENCE_MODE_NORMALIZE",
            "evidence_mode": evidence_mode,
            "customer_article_mode": "stripped_safe_binding",
        },
        article_text="岱林生物拥有大量专利。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "missing_evidence_bindings" in result["blockers"]
    assert "missing_customer_article_sha256" in result["blockers"]


@pytest.mark.parametrize("evidence_mode", ["withevidence", "with_evidence_v2", "with evidence!"])
def test_customer_output_gate_blocks_unknown_evidence_mode(evidence_mode):
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": "UNKNOWN_EVIDENCE_MODE", "evidence_mode": evidence_mode},
        article_text="可治愈癌症疗效提升99%保证第一。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "evidence_mode_unrecognized" in result["blockers"]


def test_customer_output_gate_allows_clean_explicit_route_when_customer_flag_is_on():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": "ALLOW_ROUTE", "evidence_mode": "no_evidence", "risk_level": "low"},
        article_text="这是一篇干净的无证据采购指南。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["shadow_gate_allowed"] is True
    assert result["customer_output_allowed"] is True
    assert result["blockers"] == []


def test_customer_output_gate_blocks_visible_markers_and_sha_mismatch():
    from writing.shadow_only_injection import evaluate_customer_output_gate

    article = "岱林生物拥有大量专利[E1]。"
    result = evaluate_customer_output_gate(
        case={
            "case_id": "C3",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": hashlib.sha256("other".encode("utf-8")).hexdigest(),
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
    )

    assert result["customer_output_allowed"] is False
    assert "visible_evidence_markers_in_customer_text" in result["blockers"]
    assert "customer_article_sha_mismatch" in result["blockers"]


@pytest.mark.parametrize(
    "marker",
    [
        "[E1,E2]",
        "[E1; E3]",
        "[E1、E2]",
        "[E1 E2]",
        "[E1/E2]",
        "［E1］",
        "【E1】",
        "[ E1 ]",
        "[e1]",
    ],
)
def test_customer_output_gate_blocks_visible_marker_variants_in_no_evidence_article(marker):
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": f"MARKER_{marker}", "evidence_mode": "no_evidence"},
        article_text=f"无证据文章不应该残留内部标记{marker}。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "visible_evidence_markers_in_customer_text" in result["blockers"]


def test_customer_output_gate_blocks_with_evidence_missing_sha():
    from writing.shadow_only_injection import evaluate_customer_output_gate

    article = "岱林生物拥有大量专利。"
    result = evaluate_customer_output_gate(
        case={
            "case_id": "C3B",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
    )

    assert result["customer_output_allowed"] is False
    assert "missing_customer_article_sha256" in result["blockers"]


def test_customer_output_gate_blocks_provider_conflict_and_metric_failures():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": "MULTI_GATE", "evidence_mode": "no_evidence"},
        article_text="这是一篇干净的无证据采购指南。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 2,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        conflict={"conflict_count": 1},
        metric={"decision": "manual_review"},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "semantic_required_provider_missing" in result["blockers"]
    assert "source_conflict_unresolved" in result["blockers"]
    assert "metric_retention_manual_review" in result["blockers"]


def test_customer_output_gate_blocks_binding_span_and_claim_text_mismatch():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "岱林生物拥有大量专利。"
    sha = hashlib.sha256(article.encode("utf-8")).hexdigest()
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )

    span_result = evaluate_customer_output_gate(
        case={
            "case_id": "BIND_SPAN",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": sha,
            "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
            "evidence_bindings": [
                {"evidence_id": "E1", "claim_text": "不存在的段落", "claim_span": {"start": 0, "end": 999}}
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    mismatch_result = evaluate_customer_output_gate(
        case={
            "case_id": "BIND_MISMATCH",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": sha,
            "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
            "evidence_bindings": [
                {"evidence_id": "E1", "claim_text": "岱林生物拥有较少专利", "claim_span": {"start": 0, "end": 10}}
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert "binding_span_out_of_range" in span_result["blockers"]
    assert "binding_claim_text_mismatch" in mismatch_result["blockers"]


def test_customer_output_gate_blocks_missing_claim_text_and_overwide_binding_span():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "第一句是正文。第二句也是正文。第三句仍然是正文。"
    sha = hashlib.sha256(article.encode("utf-8")).hexdigest()
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )

    result = evaluate_customer_output_gate(
        case={
            "case_id": "BIND_WIDE",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": sha,
            "evidence_packet": [{"evidence_id": "E1", "text": article}],
            "evidence_bindings": [
                {"evidence_id": "E1", "claim_span": {"start": 0, "end": len(article)}}
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "binding_missing_claim_text" in result["blockers"]
    assert "binding_span_too_broad" in result["blockers"]


def test_customer_output_gate_blocks_unbound_high_risk_claim_sentence():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "可治愈100%癌症。岱林生物拥有大量专利。"
    sha = hashlib.sha256(article.encode("utf-8")).hexdigest()
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    claim = "岱林生物拥有大量专利"
    start = article.index(claim)
    result = evaluate_customer_output_gate(
        case={
            "case_id": "UNBOUND_RISKY",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": sha,
            "evidence_bindings": [
                {"evidence_id": "E1", "claim_text": claim, "claim_span": {"start": start, "end": start + len(claim)}}
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "unbound_high_risk_claim" in result["blockers"]


def test_customer_output_gate_blocks_fake_evidence_id_even_when_claim_text_matches():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "全球唯一通过国际认证。"
    sha = hashlib.sha256(article.encode("utf-8")).hexdigest()
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={
            "case_id": "FAKE_EVIDENCE",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": sha,
            "evidence_packet": [{"evidence_id": "E1", "text": "企业拥有完整产品线。"}],
            "evidence_bindings": [
                {
                    "evidence_id": "E_TOTALLY_FAKE",
                    "claim_text": article.rstrip("。"),
                    "claim_span": {"start": 0, "end": len(article.rstrip("。"))},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "binding_evidence_id_unknown" in result["blockers"]


def test_customer_output_gate_blocks_with_evidence_missing_evidence_packet():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "岱林生物拥有大量专利。"
    sha = hashlib.sha256(article.encode("utf-8")).hexdigest()
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={
            "case_id": "MISSING_PACKET",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": sha,
            "evidence_bindings": [
                {"evidence_id": "E1", "claim_text": "岱林生物拥有大量专利", "claim_span": {"start": 0, "end": 10}}
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "missing_evidence_packet" in result["blockers"]


def test_customer_output_gate_blocks_unbound_qualitative_authority_claim_sentence():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "国家卫健委指定的唯一战略合作伙伴。企业拥有完整产品线。"
    sha = hashlib.sha256(article.encode("utf-8")).hexdigest()
    safe_claim = "企业拥有完整产品线"
    start = article.index(safe_claim)
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={
            "case_id": "QUAL_AUTHORITY",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": sha,
            "evidence_packet": [{"evidence_id": "E1", "text": "企业拥有完整产品线。"}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": safe_claim,
                    "claim_span": {"start": start, "end": start + len(safe_claim)},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert (
        "unbound_claim_sentence" in result["blockers"]
        or "unbound_high_risk_claim" in result["blockers"]
    )


def test_customer_output_gate_blocks_binding_that_misses_final_content_character():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "复购率约百分之三十"
    covered = "复购率约百分之三"
    sha = hashlib.sha256(article.encode("utf-8")).hexdigest()
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={
            "case_id": "OFF_BY_ONE_CONTENT",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": sha,
            "evidence_packet": [{"evidence_id": "E1", "text": covered}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": covered,
                    "claim_span": {"start": 0, "end": len(covered)},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "unbound_high_risk_claim" in result["blockers"]


def test_customer_output_gate_allows_binding_that_only_omits_sentence_punctuation():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "复购率约百分之三。"
    covered = "复购率约百分之三"
    sha = hashlib.sha256(article.encode("utf-8")).hexdigest()
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
            case={
                "case_id": "PUNCT_ONLY",
                "evidence_mode": "with_evidence",
                "evidence_packet_source": "trusted_research_storage",
                "customer_article_mode": "stripped_safe_binding",
                "customer_article_sha256": sha,
                "evidence_packet": [{"evidence_id": "E1", "text": covered}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": covered,
                    "claim_span": {"start": 0, "end": len(covered)},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is True


def test_customer_output_gate_blocks_multisentence_final_digit_escape():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "第一句有证据。增长约99"
    second_claim = "增长约9"
    start = article.index(second_claim)
    sha = hashlib.sha256(article.encode("utf-8")).hexdigest()
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={
            "case_id": "OFF_BY_ONE_MULTISENTENCE",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": sha,
            "evidence_packet": [
                {"evidence_id": "E1", "text": "第一句有证据"},
                {"evidence_id": "E2", "text": second_claim},
            ],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "第一句有证据",
                    "claim_span": {"start": 0, "end": len("第一句有证据")},
                },
                {
                    "evidence_id": "E2",
                    "claim_text": second_claim,
                    "claim_span": {"start": start, "end": start + len(second_claim)},
                },
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is False
    assert "unbound_high_risk_claim" in result["blockers"]


def test_customer_output_gate_warns_but_does_not_block_guard_pass_with_warning():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )
    result = evaluate_customer_output_gate(
        case={"case_id": "WARN_ONLY", "evidence_mode": "no_evidence", "risk_level": "low"},
        article_text="这是一篇干净的无证据采购指南。",
        guard={"shadow_decision": "pass_with_warning"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["customer_output_allowed"] is True
    assert result["warnings"] == ["guard_pass_with_warning"]


def test_shadow_route_sends_conflict_and_metric_to_manual_review():
    from writing.shadow_only_injection import load_shadow_injection_config, plan_shadow_route

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
        }
    )
    route = plan_shadow_route(
        case={"case_id": "ROUTE_MANUAL", "risk_level": "low"},
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        conflict={"conflict_count": 1},
        metric={"decision": "Manual_review"},
        config=config,
    )

    assert route["shadow_injection_route"] == "manual_review"
    assert "source_conflict_manual_review" in route["manual_review_reasons"]
    assert "metric_retention_manual_review" in route["manual_review_reasons"]


def test_customer_output_gate_blocks_visible_markers_in_no_evidence_article():
    from writing.shadow_only_injection import evaluate_customer_output_gate

    result = evaluate_customer_output_gate(
        case={"case_id": "NE1", "evidence_mode": "no_evidence"},
        article_text="无证据文章不应该出现内部标记[E1]。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
    )

    assert result["customer_output_allowed"] is False
    assert "visible_evidence_markers_in_customer_text" in result["blockers"]


def test_clean_shadow_candidate_can_pass_internal_gate_but_still_not_customer_output_by_default():
    from writing.shadow_only_injection import evaluate_customer_output_gate, load_shadow_injection_config

    article = "岱林生物拥有大量专利。"
    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "false",
        }
    )
    result = evaluate_customer_output_gate(
            case={
                "case_id": "C4",
                "evidence_mode": "with_evidence",
                "evidence_packet_source": "trusted_research_storage",
                "customer_article_mode": "stripped_safe_binding",
                "customer_article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
                "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        config=config,
    )

    assert result["shadow_gate_allowed"] is True
    assert result["customer_output_allowed"] is False
    assert "customer_output_feature_flag_off" in result["blockers"]


def test_shadow_artifact_recording_is_noop_when_feature_flag_is_off(tmp_path):
    from writing.shadow_only_injection import create_shadow_run_artifacts, load_shadow_injection_config

    result = create_shadow_run_artifacts(
        case={"case_id": "OFF1", "evidence_mode": "no_evidence"},
        article_text="一篇仅用于 shadow 的文章。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": False, "manual_review_required": False},
        output_root=tmp_path / "admin_shadow_runs",
        config=load_shadow_injection_config(env={}),
    )

    assert result["status"] == "disabled"
    assert result["artifact_written"] is False
    assert not (tmp_path / "admin_shadow_runs").exists()


def test_shadow_artifact_recording_writes_admin_only_manifest_and_sidecar(tmp_path):
    from writing.shadow_only_injection import create_shadow_run_artifacts, load_shadow_injection_config

    article = "岱林生物拥有大量专利。"
    result = create_shadow_run_artifacts(
        case={
            "case_id": "RUN1",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        output_root=tmp_path / "admin_shadow_runs",
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
                "R6H_CUSTOMER_OUTPUT_ENABLED": "false",
            }
        ),
    )

    assert result["status"] == "recorded"
    assert result["artifact_written"] is True
    assert result["customer_output_allowed"] is False
    assert result["safety"]["database_touched"] is False
    assert result["safety"]["published"] is False

    paths = result["artifact_paths"]
    article_path = paths["article"]
    sidecar_path = paths["sidecar"]
    manifest_path = paths["manifest"]
    report_path = paths["report"]

    assert article_path.exists()
    assert sidecar_path.exists()
    assert manifest_path.exists()
    assert report_path.exists()
    assert "[E1]" not in article_path.read_text(encoding="utf-8")
    assert "_internal" in sidecar_path.parts

    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "shadow_only_run_recorded"
    assert manifest["shadow_only"] is True
    assert manifest["customer_output_allowed"] is False
    assert manifest["sidecar_visibility"] == "admin_only_internal"
    assert manifest["customer_article_sha256"] == hashlib.sha256(article.encode("utf-8")).hexdigest()


def test_shadow_artifact_recording_clamps_customer_output_even_when_gate_would_allow(tmp_path):
    from writing.shadow_only_injection import create_shadow_run_artifacts, load_shadow_injection_config

    result = create_shadow_run_artifacts(
        case={"case_id": "CLAMP1", "evidence_mode": "no_evidence", "risk_level": "low"},
        article_text="这是一篇干净的无证据采购指南。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        output_root=tmp_path / "admin_shadow_runs",
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
                "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
            }
        ),
        run_id="CLAMP1_RUN",
    )

    manifest_path = result["artifact_paths"]["manifest"]
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))

    assert result["shadow_gate_allowed"] is True
    assert result["customer_output_allowed"] is False
    assert manifest["shadow_gate_allowed"] is True
    assert manifest["customer_output_allowed"] is False


def test_shadow_artifact_recording_rejects_public_storage_path(tmp_path):
    import pytest
    from writing.shadow_only_injection import create_shadow_run_artifacts, load_shadow_injection_config

    with pytest.raises(ValueError, match="admin-only"):
        create_shadow_run_artifacts(
            case={"case_id": "PUB1", "evidence_mode": "no_evidence"},
            article_text="不应写入 public 路径。",
            guard={"shadow_decision": "pass"},
            semantic={
                "semantic_decision": "pass",
                "needs_human_review": False,
                "provider_count": 3,
                "required_provider_count": 3,
            },
            route={"customer_output_allowed": False, "manual_review_required": False},
            output_root=tmp_path / "public",
            config=load_shadow_injection_config(
                env={
                    "R6H_SHADOW_INJECTION_ENABLED": "true",
                    "R6H_SHADOW_ONLY": "true",
                }
            ),
        )


def test_shadow_artifact_recording_rejects_public_like_storage_path(tmp_path):
    from writing.shadow_only_injection import create_shadow_run_artifacts, load_shadow_injection_config

    with pytest.raises(ValueError, match="admin-only"):
        create_shadow_run_artifacts(
            case={"case_id": "PUBLIKE1", "evidence_mode": "no_evidence"},
            article_text="不应写入 customerfiles 这类路径。",
            guard={"shadow_decision": "pass"},
            semantic={
                "semantic_decision": "pass",
                "needs_human_review": False,
                "provider_count": 3,
                "required_provider_count": 3,
            },
            route={"customer_output_allowed": False, "manual_review_required": False},
            output_root=tmp_path / "customerfiles",
            config=load_shadow_injection_config(
                env={
                    "R6H_SHADOW_INJECTION_ENABLED": "true",
                    "R6H_SHADOW_ONLY": "true",
                }
            ),
        )


def test_shadow_artifact_recording_rejects_invalid_run_id(tmp_path):
    import pytest
    from writing.shadow_only_injection import create_shadow_run_artifacts, load_shadow_injection_config

    with pytest.raises(ValueError, match="invalid shadow run id"):
        create_shadow_run_artifacts(
            case={"case_id": "BAD_RUN", "evidence_mode": "no_evidence"},
            article_text="畸形 run id 不应落盘。",
            guard={"shadow_decision": "pass"},
            semantic={
                "semantic_decision": "pass",
                "needs_human_review": False,
                "provider_count": 3,
                "required_provider_count": 3,
            },
            route={"customer_output_allowed": False, "manual_review_required": False},
            output_root=tmp_path / "admin_shadow_runs",
            config=load_shadow_injection_config(
                env={
                    "R6H_SHADOW_INJECTION_ENABLED": "true",
                    "R6H_SHADOW_ONLY": "true",
                }
            ),
            run_id="../BAD_RUN",
        )


def test_shadow_artifact_listing_hides_sidecar_contents(tmp_path):
    from writing.shadow_only_injection import (
        create_shadow_run_artifacts,
        list_shadow_run_artifacts,
        load_shadow_injection_config,
    )

    article = "岱林生物拥有大量专利。"
    create_shadow_run_artifacts(
            case={
                "case_id": "LIST1",
                "evidence_mode": "with_evidence",
                "evidence_packet_source": "trusted_research_storage",
                "customer_article_mode": "stripped_safe_binding",
                "customer_article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
                "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        route={"customer_output_allowed": True, "manual_review_required": False},
        output_root=tmp_path / "admin_shadow_runs",
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
            }
        ),
        run_id="LIST1_RUN",
    )

    listing = list_shadow_run_artifacts(tmp_path / "admin_shadow_runs")

    assert listing["status"] == "listed"
    assert listing["artifact_count"] == 1
    row = listing["runs"][0]
    assert row["case_id"] == "LIST1"
    assert row["sidecar_present"] is True
    assert row["blocker_count"] == 1
    assert "evidence_bindings" not in row
    assert "sidecar_path" not in row


def test_shadow_artifact_listing_returns_observability_summary(tmp_path):
    from writing.shadow_only_injection import (
        create_shadow_run_artifacts,
        list_shadow_run_artifacts,
        load_shadow_injection_config,
        record_live_generation_shadow_artifact,
    )

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
        }
    )
    root = tmp_path / "admin_shadow_runs"
    article = "岱林生物拥有大量专利。"
    create_shadow_run_artifacts(
        case={
            "case_id": "OBS-HL",
            "evidence_mode": "with_evidence",
            "evidence_packet_source": "trusted_research_storage",
            "risk_level": "high",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
            "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass_with_warning"},
        semantic={
            "semantic_decision": "pass_with_warning",
            "needs_human_review": True,
            "manual_review_reasons": ["soft_fabrication_warn"],
            "provider_count": 3,
            "required_provider_count": 3,
            "warn_count": 1,
            "issue_count": 1,
        },
        route={
            "shadow_injection_route": "manual_review",
            "customer_output_allowed": False,
            "manual_review_required": True,
            "manual_review_reasons": ["high_liability_manual_review", "soft_fabrication_warn"],
        },
        output_root=root,
        config=config,
        run_id="OBS_HL_RUN",
    )
    record_live_generation_shadow_artifact(
        quote_id=123,
        brand_name="岱林生物",
        industry="制药装备",
        topic={"id": 456, "title": "采购指南", "style_code": "buying_guide"},
        article={"id": 789, "title": "采购指南", "content": "一篇 live shadow 文章。", "style": "buying_guide"},
        output_root=root,
        config=config,
    )

    listing = list_shadow_run_artifacts(root)

    observability = listing["observability"]
    assert observability["total_runs"] == 2
    assert observability["live_run_count"] == 1
    assert observability["manual_review_required_count"] == 2
    assert observability["customer_output_open_count"] == 0
    assert observability["customer_output_closed_count"] == 2
    assert observability["sidecar_present_count"] == 2
    assert observability["by_evidence_mode"]["with_evidence"] == 1
    assert observability["by_evidence_mode"]["no_evidence"] == 1
    assert observability["by_guard_decision"]["missing"] == 1
    assert observability["by_guard_decision"]["pass_with_warning"] == 1
    assert observability["by_semantic_decision"]["missing"] == 1
    assert observability["by_semantic_decision"]["pass_with_warning"] == 1
    assert observability["top_blockers"][0]["label"]
    assert "artifact_paths" not in observability
    assert "evidence_bindings" not in observability


def test_shadow_seam_adapter_default_off_does_not_write_artifacts(tmp_path):
    from writing.shadow_only_injection import run_shadow_only_seam_adapter

    result = run_shadow_only_seam_adapter(
        case={"case_id": "ADAPT_OFF", "evidence_mode": "no_evidence"},
        article_text="默认关闭时不能落盘。",
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        output_root=tmp_path / "admin_shadow_runs",
    )

    assert result["status"] == "disabled"
    assert result["artifact_written"] is False
    assert not (tmp_path / "admin_shadow_runs").exists()


def test_shadow_seam_adapter_records_artifact_but_never_customer_output(tmp_path):
    from writing.shadow_only_injection import load_shadow_injection_config, run_shadow_only_seam_adapter

    article = "无证据文章采用泛化采购建议。"
    result = run_shadow_only_seam_adapter(
        case={"case_id": "ADAPT_ON", "evidence_mode": "no_evidence", "risk_level": "low"},
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        output_root=tmp_path / "admin_shadow_runs",
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
                "R6H_CUSTOMER_OUTPUT_ENABLED": "false",
            }
        ),
        run_id="ADAPT_ON_RUN",
    )

    assert result["status"] == "recorded"
    assert result["artifact_written"] is True
    assert result["route"]["shadow_injection_route"] == "shadow_candidate"
    assert result["customer_output_allowed"] is False
    assert "customer_output_feature_flag_off" in result["blockers"]


def test_shadow_seam_adapter_blocks_with_evidence_without_packet_source(tmp_path):
    from writing.shadow_only_injection import load_shadow_injection_config, run_shadow_only_seam_adapter

    article = "岱林生物拥有大量专利。"
    result = run_shadow_only_seam_adapter(
        case={
            "case_id": "SRC_MISSING",
            "evidence_mode": "with_evidence",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
            "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        output_root=tmp_path / "admin_shadow_runs",
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
            }
        ),
        run_id="SRC_MISSING_RUN",
    )

    assert result["status"] == "recorded"
    assert result["customer_output_allowed"] is False
    assert "missing_evidence_packet_source" in result["blockers"]
    assert result["shadow_gate_allowed"] is False


def test_shadow_seam_adapter_blocks_client_or_llm_packet_source(tmp_path):
    from writing.shadow_only_injection import load_shadow_injection_config, run_shadow_only_seam_adapter

    article = "岱林生物拥有大量专利。"
    result = run_shadow_only_seam_adapter(
        case={
            "case_id": "SRC_CLIENT",
            "evidence_mode": "with_evidence",
            "evidence_packet_source": "client_input",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
            "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        output_root=tmp_path / "admin_shadow_runs",
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
            }
        ),
        run_id="SRC_CLIENT_RUN",
    )

    assert result["status"] == "recorded"
    assert "untrusted_evidence_packet_source" in result["blockers"]
    assert result["shadow_gate_allowed"] is False


def test_shadow_seam_adapter_accepts_trusted_packet_source(tmp_path):
    from writing.shadow_only_injection import load_shadow_injection_config, run_shadow_only_seam_adapter

    article = "岱林生物拥有大量专利。"
    result = run_shadow_only_seam_adapter(
        case={
            "case_id": "SRC_TRUSTED",
            "evidence_mode": "with_evidence",
            "evidence_packet_source": "TRUSTED-RESEARCH-STORAGE",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
            "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass"},
        semantic={
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        output_root=tmp_path / "admin_shadow_runs",
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
            }
        ),
        run_id="SRC_TRUSTED_RUN",
    )

    assert result["status"] == "recorded"
    assert "missing_evidence_packet_source" not in result["blockers"]
    assert "untrusted_evidence_packet_source" not in result["blockers"]


def _clean_with_evidence_gate_case(article: str, source: str | None = None) -> dict:
    case = {
        "case_id": "GATE_SOURCE",
        "evidence_mode": "with_evidence",
        "risk_level": "low",
        "customer_article_mode": "stripped_safe_binding",
        "customer_article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
        "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
        "evidence_bindings": [
            {
                "evidence_id": "E1",
                "claim_text": "岱林生物拥有大量专利",
                "claim_span": {"start": 0, "end": 10},
            }
        ],
    }
    if source is not None:
        case["evidence_packet_source"] = source
    return case


def _customer_output_on_config():
    from writing.shadow_only_injection import load_shadow_injection_config

    return load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
            "R6H_CUSTOMER_OUTPUT_ENABLED": "true",
        }
    )


def _clean_gate_context():
    return {
        "guard": {"shadow_decision": "pass"},
        "semantic": {
            "semantic_decision": "pass",
            "needs_human_review": False,
            "provider_count": 3,
            "required_provider_count": 3,
        },
        "route": {"customer_output_allowed": True, "manual_review_required": False},
    }


def test_customer_output_gate_blocks_with_evidence_missing_packet_source_directly():
    from writing.shadow_only_injection import evaluate_customer_output_gate

    article = "岱林生物拥有大量专利。"
    context = _clean_gate_context()
    result = evaluate_customer_output_gate(
        case=_clean_with_evidence_gate_case(article),
        article_text=article,
        config=_customer_output_on_config(),
        **context,
    )

    assert result["customer_output_allowed"] is False
    assert "missing_evidence_packet_source" in result["blockers"]


@pytest.mark.parametrize("source", ["client_input", "LLM_output", "customer_submitted"])
def test_customer_output_gate_blocks_untrusted_packet_source_directly(source):
    from writing.shadow_only_injection import evaluate_customer_output_gate

    article = "岱林生物拥有大量专利。"
    context = _clean_gate_context()
    result = evaluate_customer_output_gate(
        case=_clean_with_evidence_gate_case(article, source=source),
        article_text=article,
        config=_customer_output_on_config(),
        **context,
    )

    assert result["customer_output_allowed"] is False
    assert "untrusted_evidence_packet_source" in result["blockers"]


def test_customer_output_gate_accepts_trusted_packet_source_directly():
    from writing.shadow_only_injection import evaluate_customer_output_gate

    article = "岱林生物拥有大量专利。"
    context = _clean_gate_context()
    result = evaluate_customer_output_gate(
        case=_clean_with_evidence_gate_case(article, source="TRUSTED-RESEARCH-STORAGE"),
        article_text=article,
        config=_customer_output_on_config(),
        **context,
    )

    assert result["customer_output_allowed"] is True
    assert "missing_evidence_packet_source" not in result["blockers"]
    assert "untrusted_evidence_packet_source" not in result["blockers"]


def test_customer_output_gate_does_not_require_packet_source_for_no_evidence():
    from writing.shadow_only_injection import evaluate_customer_output_gate

    context = _clean_gate_context()
    result = evaluate_customer_output_gate(
        case={"case_id": "GATE_NO_SOURCE", "evidence_mode": "no_evidence", "risk_level": "low"},
        article_text="无证据采购指南采用泛化建议。",
        config=_customer_output_on_config(),
        **context,
    )

    assert result["customer_output_allowed"] is True
    assert "missing_evidence_packet_source" not in result["blockers"]
    assert "untrusted_evidence_packet_source" not in result["blockers"]
