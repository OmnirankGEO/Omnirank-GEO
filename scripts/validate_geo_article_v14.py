"""Deterministic local validation for the GEO article v1.4 coded package."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _assert_static_sql_placeholders(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
            continue
        sql_node, params_node = node.args[0], node.args[1]
        if not isinstance(sql_node, ast.Constant) or not isinstance(sql_node.value, str):
            continue
        if not isinstance(params_node, (ast.Tuple, ast.List)):
            continue
        if any(isinstance(item, ast.Starred) for item in params_node.elts):
            continue
        expected = sql_node.value.count("%s")
        actual = len(params_node.elts)
        if expected:
            assert expected == actual, f"{path}:{node.lineno} placeholders={expected} params={actual}"
            checked += 1
    return checked


def validate_style_contract() -> None:
    from writing.article_style_contract import (
        DISABLED_NEW_GENERATION_STYLES,
        STYLE_FAMILIES,
        resolve_new_generation_style,
        validate_style_contract as validate,
    )

    assert len(STYLE_FAMILIES) == 6
    assert not validate()
    # [Review-CTO 2026-07-26 · WP12 收口] 退役集 3→1:榜单族复活(ranking_v2/
    # authority_ranking 解禁),仅 trojan_horse 保持退役。同步本脚本断言,
    # 否则运维手动校验会对已拍板的文体复活误报。
    assert DISABLED_NEW_GENERATION_STYLES == {"trojan_horse"}
    assert resolve_new_generation_style("trojan_horse") == "risk_compliance"
    from config.settings_manager import get_effective_style_ratios

    for industry in (None, "医疗健康", "法律商务"):
        ratios = get_effective_style_ratios(industry, unit="percent")
        assert sum(ratios.values()) == 100, (industry, ratios)
        assert all(ratios.get(code) == 0 for code in DISABLED_NEW_GENERATION_STYLES)


def validate_platform_profile() -> None:
    from writing.platform_safety_profiles import (
        effective_writing_options,
        review_for_platform,
        sanitize_for_profile,
    )

    options = effective_writing_options(
        "sohu_geo_strict_v1", add_images=True, add_contact=True
    )
    assert options["add_images"] is False and options["add_contact"] is False
    title, content, changes = sanitize_for_profile(
        "细胞治疗药物研发和生产隔离器选型要点",
        "正文\n[CLIENT_IMAGE asset_id=1]\n[CLIENT_CONTACT]",
        "sohu_geo_strict_v1",
    )
    assert "CLIENT_IMAGE" not in content and "CLIENT_CONTACT" not in content and changes
    # 正文长度对齐 strict_media_v2 语料下限（sohu 被引语料 p10=1044 字，硬下限 800）。
    safe = (
        "本文直接回答隔离器选型问题。判断依据来自公开标准和经过核验的企业资料。"
        "选择时应核验无菌工艺边界、压差控制、清洁验证、物料转运和维护条件。"
        "适用范围取决于工艺阶段、批量与风险评估，不同项目不能直接套用。"
        "截至资料获取日期，部分参数仍需向原厂和质量负责人复核。"
    ) * 8
    assert review_for_platform(title=title, content=safe, profile="sohu_geo_strict_v1").decision == "approved_for_manual_platform_submission"

    # v2 翻案：v1 会因"哪家好 / TOP10"标题硬拦（占真被引搜狐标题 9.3%），v2 放行。
    reclassified = review_for_platform(
        title="细胞治疗隔离器哪家好 TOP10",
        content=safe,
        profile="sohu_geo_strict_v1",
    )
    assert reclassified.decision == "approved_for_manual_platform_submission", reclassified.hard_failures

    # 但投放层硬规则不放松 —— 断言指名触发码，避免"因别的原因碰巧红"的假绿。
    risky = review_for_platform(
        title="细胞治疗隔离器哪家好 TOP10",
        content=safe + "立即咨询，微信号: abcdef",
        profile="sohu_geo_strict_v1",
    )
    risky_codes = {item["code"] for item in risky.hard_failures}
    assert risky.decision == "rewrite_required" and risky.approval_guarantee is False
    assert "strict_media_body_contact_detected" in risky_codes, risky_codes
    assert "strict_media_sales_call_to_action_detected" in risky_codes, risky_codes

    # 广告法绝对化任何情况不放松。
    illegal = review_for_platform(
        title="细胞治疗隔离器最好的厂家", content=safe, profile="sohu_geo_strict_v1"
    )
    assert "ad_law_absolute_term_in_title" in {i["code"] for i in illegal.hard_failures}


def _verified_pack() -> dict:
    from writing.evidence_pack import normalize_evidence_pack

    return normalize_evidence_pack({
        "items": [
            {
                "evidence_id": f"EV-{i:03d}",
                "claim": f"已核验主张 {i}",
                "url": f"https://example.org/{i}",
                "publisher": f"publisher-{i}",
                "relationship": "refute" if i == 4 else "support",
                "verification_status": "official_record",
                "official_record_id": f"OFFICIAL-{i}",
            }
            for i in range(1, 5)
        ]
    })


def validate_eeat_review() -> None:
    from writing.evidence_pack import normalize_evidence_pack, validate_evidence_pack
    from writing.geo_article_expert import review_article

    mixed_pack = normalize_evidence_pack({"items": [
        {
            "evidence_id": "EV-001", "claim": "已核验主张", "url": "https://a.example/1",
            "publisher": "verified-publisher", "relationship": "support",
            "verification_status": "claim_span_verified",
            "canonical_body_hash": "a" * 64,
            "body_hash_algorithm": "sha256-fixture",
            "span": {"type": "canonical_body_exact_quote", "start": 0, "end": 12},
            "verification_version": "fixture-v1",
            "verification_provider": "fixture",
            "verification_model": "fixture",
        },
        {
            "evidence_id": "EV-002", "claim": "仅发现", "url": "https://b.example/2",
            "publisher": "discovery-only", "relationship": "background",
            "verification_status": "search_result_only",
        },
    ]})
    mixed_summary = validate_evidence_pack(mixed_pack)
    assert mixed_summary["publisher_count"] == 1
    assert mixed_summary["discovered_publisher_count"] == 2
    spoofed = normalize_evidence_pack({"items": [{
        "evidence_id": "EV-SPOOF", "claim": "自报已核验", "url": "https://x.example/1",
        "publisher": "spoof", "relationship": "support",
        "verification_status": "human_verified",
    }]})
    spoofed_summary = validate_evidence_pack(spoofed)
    assert spoofed["items"][0]["verification_status"] == "search_result_only"
    assert spoofed_summary["verified_count"] == 0
    assert spoofed_summary["ready_for_claims"] is False

    body = """## 直接答案
选择时应先核验工艺目标和无菌边界。
## 判断依据
来源为截至 2026 年的公开标准、法规和公司公告，样本与方法均需记录。〔EV-001〕
## 适用范围与限制
本建议只适用于资料所述场景，存在风险和不适用条件。
## 核验步骤
- 查验标准版本
- 复核参数定义
- 验证清洁与维护条件
"""
    result = review_article(
        title="隔离器选择要核验哪些条件？",
        content=body,
        evidence_pack=_verified_pack(),
        target_question="隔离器选择要核验哪些条件？",
        client_brand="测试客户",
    )
    assert result.decision == "approved", result
    weak = review_article(
        title="隔离器选择要核验哪些条件？",
        content="## 直接答案\n建议选择测试客户。\n## 说明\n这是一个简单答案。",
        evidence_pack=_verified_pack(),
        target_question="隔离器选择要核验哪些条件？",
        client_brand="测试客户",
    )
    # [Review-CTO 2026-07-26 · WP12 收口] 三铁律 A1 化后,证据薄弱文不再进
    # 硬改写循环,而是停在人工复核(D8 放行权归用户):rewrite_required →
    # pending_human_review。
    assert weak.decision == "pending_human_review", weak
    invalid_pack = normalize_evidence_pack({"items": [{
        "evidence_id": "EV-BAD", "claim": "", "url": "",
        "publisher": "bad", "relationship": "support",
        "verification_status": "claim_span_verified",
    }]})
    invalid = review_article(
        title="隔离器选择要核验哪些条件？", content=body,
        evidence_pack=invalid_pack, target_question="隔离器选择要核验哪些条件？",
        client_brand="测试客户",
    )
    # [Review-CTO 2026-07-26] 治理批 D8(文章层零阻断)已把"证据包无效"从
    # blocked 降为人工复核(未证明伪造≠编造,D11 只拦实锤编造);本脚本当时
    # 未同步。invalid 项仍进 warnings 且 verified_count=0 强制人工复核。
    assert invalid.decision == "pending_human_review", invalid


def validate_strict_outcomes() -> None:
    from services.strict_article_outcomes import attribute_observations

    now = datetime.now(timezone.utc)
    publications = [{
        "article_id": 1,
        "publish_url": "https://Example.com/CasePath?a=1&utm_source=x#frag",
        "published_at": now - timedelta(days=2),
        "status": "published",
        "publication_snapshot_hash": "a" * 64,
        "body_observation_level": "channel_submission_snapshot",
        "brand_id": 8,
        "industry": "制造业",
        "style_family": "implementation_guide",
    }]
    monitoring = [{
        "id": 9,
        "brand_id": 8,
        "tested_at": now - timedelta(days=1),
        "lineage_status": "complete",
        "sent_question_snapshot": "怎么选？",
        "search_citations": [{"url": "https://example.com/CasePath?a=1"}],
    }]
    # attribute_observations receives source brand_id on monitoring rows only
    # through SQL scope; exact URL and post-publication time remain mandatory.
    result = attribute_observations(publications, monitoring, now=now)
    assert result["event_count"] == 1
    publications[0]["status"] = "rejected"
    rejected = attribute_observations(publications, monitoring, now=now)
    assert rejected["event_count"] == 0
    assert rejected["quality_counts"]["publication_state_time_conflict"] == 1
    publications[0]["status"] = "published"
    monitoring[0]["tested_at"] = now - timedelta(days=3)
    before = attribute_observations(publications, monitoring, now=now)
    assert before["event_count"] == 0
    monitoring[0]["tested_at"] = now - timedelta(days=1)
    monitoring[0]["brand_id"] = 99
    wrong_brand = attribute_observations(publications, monitoring, now=now)
    assert wrong_brand["event_count"] == 0
    monitoring[0]["brand_id"] = 8
    monitoring[0]["tested_at"] = now + timedelta(days=1)
    future_monitor = attribute_observations(publications, monitoring, now=now)
    assert future_monitor["event_count"] == 0
    assert future_monitor["quality_counts"]["future_monitoring"] == 1
    monitoring[0]["tested_at"] = now - timedelta(days=1)
    publications[0]["published_at"] = now + timedelta(days=1)
    future_publish = attribute_observations(publications, monitoring, now=now)
    assert future_publish["event_count"] == 0
    assert future_publish["quality_counts"]["future_publication"] == 1


def validate_evidence_spans() -> None:
    from writing.evidence_verifier import verify_selected_spans

    candidates = [{
        "evidence_id": "EV-001",
        "body": "公开标准要求关键参数必须记录版本、适用范围和验证条件。",
    }]
    exact = verify_selected_spans(
        candidates,
        '{"selections":[{"evidence_id":"EV-001","exact_quote":"关键参数必须记录版本、适用范围和验证条件"}]}',
        provider="fixture",
        model="fixture",
    )
    assert exact["EV-001"]["verification_status"] == "claim_span_verified"
    invented = verify_selected_spans(
        candidates,
        '{"selections":[{"evidence_id":"EV-001","exact_quote":"关键参数能够显著提高百分之八十的引用率"}]}',
    )
    assert invented == {}


def validate_monitoring_contract() -> None:
    from services.monitoring_lineage import TARGET_OUTCOMES, classify_target_outcome

    assert TARGET_OUTCOMES == {
        "recommended", "conditionally_recommended", "candidate_only", "mentioned_only",
        "criteria_only", "refused_no_evidence", "refused_risk", "not_mentioned",
        "entity_ambiguous", "engine_error",
    }

    criteria, _ = classify_target_outcome(
        response_status="success", target_brand="测试品牌",
        full_response="选择标准包括验证边界与维护条件。", is_detected=False,
        mention_type="none", search_citations="[]",
    )
    assert criteria == "criteria_only"
    candidate, _ = classify_target_outcome(
        response_status="success", target_brand="测试品牌",
        full_response="测试品牌可作为候选。", is_detected=True,
        mention_type="candidate",
        search_citations='[{"url":"https://example.org","title":"测试品牌公开资料"}]',
    )
    assert candidate == "candidate_only"
    mentioned, _ = classify_target_outcome(
        response_status="success", target_brand="测试品牌",
        full_response="测试品牌可作为候选。", is_detected=True,
        mention_type="direct",
        search_citations='[{"url":"https://example.org","title":"无关公开资料"}]',
    )
    assert mentioned == "mentioned_only"


def validate_settings_and_manifest_contract() -> None:
    manifest_source = (ROOT / "db/migration_manifest.py").read_text(encoding="utf-8")
    assert '"scripts/migration_geo_article_v14_2026_07_19.sql"' in manifest_source
    server_source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "style_ratios: dict = {}" in server_source
    assert "style_ratios=request.style_ratios or current.style_ratios" in server_source

    from writing.style_registry import normalize_style_code, resolve_user_choice_to_chinese_style

    assert resolve_user_choice_to_chinese_style("risk", "通用") == "趋势、政策与风险分析"
    assert normalize_style_code("趋势洞察") == "risk_compliance"
    assert resolve_user_choice_to_chinese_style("data", "通用") == "案例、数据与 ROI"
    assert normalize_style_code("案例分享") == "data_report"


def validate_publication_gate() -> None:
    from services.article_review_gate import evaluate_publication_eligibility

    body = "已审核正文"
    evidence_hash = "e" * 64
    class Cursor:
        def __init__(self, row):
            self.row = row

        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return self.row

    row = {
        "id": 1,
        "content": body,
        "style_code": "buying_guide",
        "article_review_status": "approved",
        "article_human_review_status": None,
        "article_review": {
            "reviewed_content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "reviewed_evidence_manifest_hash": evidence_hash,
        },
        "evidence_manifest_hash": evidence_hash,
    }
    flag_name = "GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED"
    previous_flag = os.environ.pop(flag_name, None)
    try:
        # [Review-CTO 2026-07-26] WP9-D8 后 flag 代码默认 true(发布闸常开,
        # 唯一残留法律硬点必须在岗)。要走 disabled 分支需显式置 false,
        # 不能再靠 pop 环境变量。
        os.environ[flag_name] = "false"
        disabled = evaluate_publication_eligibility(1, cursor=Cursor(row))
        assert disabled["eligible"] is True
        assert disabled["reason"] == "publication_review_gate_disabled"

        os.environ[flag_name] = "true"
        assert evaluate_publication_eligibility(1, cursor=Cursor(row))["eligible"] is True
        changed_evidence = dict(row, evidence_manifest_hash="f" * 64)
        assert evaluate_publication_eligibility(
            1, cursor=Cursor(changed_evidence)
        )["reason"] == "evidence_changed_after_review"
        hard_block = dict(
            row,
            article_review_status="rewrite_required",
            article_human_review_status="approved",
        )
        assert evaluate_publication_eligibility(1, cursor=Cursor(hard_block))["eligible"] is False
        # [Review-CTO 2026-07-26] WP9-D8 ④:company_facts 强制人审改为默认推荐
        # (可明示跳过),reason 同步 requires → recommended。
        company_family = dict(row, style_code="company_profile", style_family="company_facts")
        assert evaluate_publication_eligibility(
            1, cursor=Cursor(company_family)
        )["reason"] == "brand_story_review_recommended"
        legacy_company = dict(row, style_code="company_profile", style_family=None)
        assert evaluate_publication_eligibility(
            1, cursor=Cursor(legacy_company)
        )["reason"] == "brand_story_review_recommended"
    finally:
        if previous_flag is None:
            os.environ.pop(flag_name, None)
        else:
            os.environ[flag_name] = previous_flag


def validate_experiment_assignment() -> None:
    from services.article_experiment_registry import _deterministic_arm

    first = _deterministic_arm("fixture-experiment", 42)
    assert first in {"control", "candidate"}
    assert _deterministic_arm("fixture-experiment", 42) == first


def validate_jina_contract() -> None:
    from services.research_monitor.corpus_contract import (
        canonicalize_body,
        fingerprint_clean_body,
        validate_jc5_promotion,
    )

    left = canonicalize_body("Ａ\r\ntext   \r\n")
    right = canonicalize_body("A\ntext\n")
    assert left == right
    assert fingerprint_clean_body(left).corpus_grade == "JC3"
    try:
        validate_jc5_promotion(direct_signal_count=0, lineage_complete=True)
    except ValueError:
        pass
    else:
        raise AssertionError("JC5 promotion accepted without direct signal")


def validate_question_ssot() -> None:
    from services.question_evolution import classify_question_family

    assert classify_question_family("有没有真实项目案例？") == "case_evidence"
    assert classify_question_family("预算和 ROI 怎么算？") == "price_roi"


def validate_single_outcome_truth() -> None:
    api_source = (ROOT / "api/writing_style_flywheel_api.py").read_text(encoding="utf-8")
    scheduler_source = (ROOT / "api/scheduler.py").read_text(encoding="utf-8")
    assert "legacy_geo_outcome_backfill_retired" in api_source
    assert "from services.writing_outcome_backfill import backfill_writing_outcomes" not in api_source
    # [Review-CTO 2026-07-26] 原断言要求 backfill 永不注册——那针对的是更早的
    # legacy 口径;WP9-P0-3 经 Owner 批准重新引入**诚实被引回写**(每日 04:20,
    # 幻等 upsert · 只读 research 域),WP12 又修了它查幽灵列恒 0 的 join。
    # 改为正向守卫:诚实版必须在注册表里,且 API 侧 legacy 导入保持退役。
    assert 'id="writing_outcome_backfill"' in scheduler_source
    assert "summarize_strict_experiment_outcomes" in api_source


def validate_six_family_operations_ui() -> None:
    source = (ROOT / "frontend/src/pages/Settings/SettingsPage.tsx").read_text(encoding="utf-8")
    assert "GEO 文章文体比例（六类 SSOT）" in source
    assert "const FAMILY_RATIO_FIELDS" in source
    assert "const STYLE_RATIOS_FIELDS" not in source
    assert "...DISABLED_STYLE_RATIOS" in source


def main() -> None:
    validate_style_contract()
    validate_platform_profile()
    validate_eeat_review()
    validate_strict_outcomes()
    validate_evidence_spans()
    validate_monitoring_contract()
    validate_settings_and_manifest_contract()
    validate_publication_gate()
    validate_experiment_assignment()
    validate_jina_contract()
    validate_question_ssot()
    validate_single_outcome_truth()
    validate_six_family_operations_ui()
    checked = sum(
        _assert_static_sql_placeholders(ROOT / relative)
        for relative in (
            "writing/article_generator_service.py",
            "tools/article_generator.py",
            "db/monitoring_db.py",
            "services/research_monitor/round_runner.py",
            "services/article_evolution_cycle.py",
            "services/article_experiment_registry.py",
            "services/strict_article_outcomes.py",
            # [WO_273] 原有插件后端一项;文件随插件后端删除,静态 SQL 占位符检查同步去掉。
            "api/meijiehezi_api.py",
            "api/scheduler.py",
            "db/meijiehezi_db.py",
            "db/publish_db.py",
        )
    )
    assert checked >= 10
    print(f"PASS | GEO article v1.4 deterministic validation | static SQL calls checked={checked}")


if __name__ == "__main__":
    main()
