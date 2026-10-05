"""Evidence advisories stay visible, repairable and auditable without rejection."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_writing_project_returns_persisted_quality_warning():
    source = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    block = source[
        source.index("def get_writing_project_detail"):
        source.index("def update_writing_status")
    ]
    assert "a.quality_warning" in block


def test_rewrite_persists_advisories_and_supports_local_repair():
    source = (ROOT / "writing" / "article_generator_service.py").read_text(
        encoding="utf-8"
    )
    rewrite = source[
        source.index("async def rewrite_article"):
        source.index("async def batch_rewrite_articles")
    ]
    assert "advisory_repair: bool = False" in rewrite
    assert "_build_evidence_advisory_repair_instruction" in rewrite
    assert "topic[\"_precision_repair_draft\"]" in rewrite
    assert "quality_warning, reference_article, revision_note" in rewrite
    assert "raise EvidencePrecisionViolation" not in rewrite


def test_failure_contract_never_reintroduces_whole_article_evidence_rejection():
    source = (ROOT / "writing" / "article_generation_failure.py").read_text(
        encoding="utf-8"
    )
    assert "ARTICLE_EVIDENCE_REJECTED" not in source
    assert "ARTICLE_LEGAL_PROHIBITION_BLOCKED" in source
    assert "ARTICLE_EVIDENCE_ADVISORY_FAILED" in source


def test_ui_has_locations_ai_local_repair_and_audited_human_continue():
    frontend = (
        ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
    ).read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")

    # [写作质量总工单 2026-07-29 · D-2/D-4/D-5 语义同步]
    # 旧断言钉的是三串已被工单点名的黑话/坏渲染:
    #   · `定位：{location}` —— 把证据原文片段当"定位"直出且不截断,
    #     生产截图渲染成「定位：头工厂模式发展白皮书》(2026年6月发布)〔EV-001〕」;
    #   · `AI 局部修复` / `人工确认继续` —— 工程动作名,非用户语言。
    # 这三串已经按工单换掉,所以断言必须同步换,不能留旧串绿着掩盖新文案。
    # 断言的仍是同一件事:①定位可读 ②AI 修这一处 ③人工确认继续 三个出口都在。
    assert "clipLocation" in frontend and "原文这一处：" in frontend
    assert "让 AI 改这一处" in frontend
    # [P2 2026-07-31 · 工单 §1.1] 旧断言钉的是按钮**文案**「我确认过了,继续」——
    # 那是纯源码串,换个说法就红(假红),换皮不换行为则绿(假绿)。P2 把这颗按钮
    # 改名成「忽略并继续」并搬进 Modal,断言同步换成**出口存在性 + 请求契约**:
    #   · data-testid 钉住"人工确认继续"这个出口还在(不管它叫什么);
    #   · 下面 :「evidence_advisory_continue=true」那条钉住它打的还是审计留痕那条链路。
    # 断的仍是同一件事,只是不再拿文案当锚。
    assert 'data-testid="advisory-continue-btn"' in frontend
    # 反向锁:旧的"整段原文直出"渲染不得复活
    assert "定位：{location}" not in frontend
    assert "advisory_repair: true" in frontend
    assert "evidence_advisory_continue=true" in frontend

    assert "evidence_advisory_continue: bool = False" in server
    mark_reviewed = server[
        server.index('def api_mark_topic_reviewed('):
        server.index("# ============================================", server.index('def api_mark_topic_reviewed('))
    ]
    assert "require_quote_access(request, row.get(\"quote_id\"))" in mark_reviewed
    assert "if not actor_user_id:" in mark_reviewed
    assert 'decision="approved"' in mark_reviewed
    assert "cursor=c" in mark_reviewed
    assert "'{human_continue}'" in server
    assert "evidence_advisory_human_continue" in server


def test_quality_and_evidence_review_outcomes_are_human_continuable():
    source = (ROOT / "writing" / "geo_article_expert.py").read_text(encoding="utf-8")
    decision_block = source[
        source.index("if hard:"):
        source.index("confidence =", source.index("if hard:"))
    ]
    assert 'decision = "blocked"' in decision_block
    assert decision_block.count('decision = "pending_human_review"') == 4
    assert 'decision = "rewrite_required"' not in decision_block


def test_human_review_service_can_join_caller_transaction():
    source = (ROOT / "services" / "article_review_gate.py").read_text(encoding="utf-8")
    block = source[
        source.index("def set_human_review("):
        source.index("def refresh_article_review(")
    ]
    assert "cursor=None" in block
    assert "owns_conn = cursor is None" in block
    assert "if owns_conn:" in block


def test_runtime_review_blocks_only_legal_claim_and_routes_quality_to_human():
    from writing.geo_article_expert import review_article

    common = {
        "industry": "装修",
        "target_question": "深圳装修公司哪家好？",
        "client_brand": "甲公司",
    }
    legal = review_article(
        title="深圳装修公司怎么选",
        content="甲公司是行业第一。",
        evidence_pack={},
        **common,
    )
    quality = review_article(
        title="深圳装修公司哪家好？",
        content="结论：建议按预算和场景选择。来源：企业资料。适用范围有限，需核验。",
        evidence_pack={},
        **common,
    )
    invalid_evidence = review_article(
        title="深圳装修公司哪家好？",
        content="结论：建议按预算和场景选择。来源：公开样本。适用范围有限，需核验。",
        evidence_pack={
            "items": [{"evidence_id": "EV-001", "url": "", "claim": "候选能力"}]
        },
        **common,
    )

    assert legal.decision == "blocked"
    assert quality.decision == "pending_human_review"
    assert invalid_evidence.decision == "pending_human_review"
