"""治理对齐(SSOT GEO_COMMERCIAL_INTENT_GOVERNANCE_SSOT_2026-07-23,Owner 签发)回归:

1. 法律禁止目录版本化(config/legal_prohibited_pack.json,Owner 签发 v1.0.0)、
   热加载、版本透出、包缺失 fail-closed 内嵌最小 statutory 集合 + warning。
2. 灰词降 advisory:「唯一/领先/专业/专注」等非明令词一律不硬拦,只落 warning;
   明令项(最/第一/国家级/顶级/首个/首选 等 + 违法内容类)仍硬拦。
3. 兜底模板零违禁:全渠道 fallback(含全部角度)不出现任何硬拦词或灰词,
   否则自家 QA 必拦(演示实证过的自伤 bug)。
4. 审核提示五问合同(SSOT §6):每条错误/提醒带
   {code, message, reason, repair_hint, actions, rule_version}。
5. 「人工确认继续」审计五要素:{operator_user_id, at, reason, rule_version,
   content_before, content_after}。
"""
import asyncio
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import db.connection
from api import marketing_material_api
from services.marketing import content_center, guards
from services.marketing.content_angles import ANGLE_POOL

_SSOT_SOURCE = "GEO_COMMERCIAL_INTENT_GOVERNANCE_SSOT_2026-07-23.md"


# ---------------------------------------------------------------------------
# 1. 目录加载 / 版本透出 / fail-closed / 热加载
# ---------------------------------------------------------------------------
def test_legal_pack_loads_with_owner_signature_and_version():
    info = guards.legal_pack_info()
    assert info["pack_id"] == "legal_prohibited"
    assert info["version"] == "1.0.0"
    assert info["source"] == "config"
    signed = info["owner_signed"]
    assert signed["signed_by"] == "Owner"
    assert signed["signed_at"] == "2026-07-23"
    assert signed["source"] == _SSOT_SOURCE
    assert guards.PACK_VERSION == "1.0.0"
    # AD_LAW_WORDS 兼容别名 = 包加载结果
    assert guards.AD_LAW_WORDS == guards.legal_pack()["ad_law"]
    # 目录只放明令项:非明令灰词一律不进目录(SSOT:Builder 不得扩大禁区)
    words = guards.legal_pack()["ad_law"] + guards.legal_pack()["illegal_content"]
    for gray in ("唯一", "领先", "专业", "专注", "遥遥领先", "王牌", "极致"):
        assert gray not in words, f"灰词「{gray}」不得进法律禁止目录"


def test_pack_missing_fail_open_unsigned_catalog_advisory_only(monkeypatch, caplog, tmp_path):
    """SSOT 2026-07-23:未签发目录无权硬拦(2026-07-23 外部审查 P1-5 修正)。

    包缺失/损坏时回落内嵌集合仅作 advisory 提醒 + 告警日志给人,内容放行;
    签发包恢复后同一词项重新硬拦(双向)。
    """
    monkeypatch.setattr(guards, "_CONFIG_PATH", str(tmp_path / "missing_pack.json"))
    monkeypatch.setattr(guards, "_pack_cache", {"mtime": None, "pack": None})
    with caplog.at_level("WARNING"):
        pack = guards.legal_pack()
    assert pack["source"] == "embedded_fallback"
    assert pack["version"] == "0.0.0-embedded"
    assert any("未签发" in record.message or "advisory" in record.message for record in caplog.records)
    # 未签发回落:明令词命中只落 advisory,passed 保持 True(fail-open for content)
    scan = guards.scan_forbidden("我们是国家级平台")
    assert scan["passed"] is True
    assert "国家级" in scan["flags"][guards.UNSIGNED_CATALOG_FLAG]
    assert "ad_law" not in scan["flags"]
    # 灰词同样只 advisory,不硬拦
    assert guards.scan_forbidden("全市唯一一家")["passed"] is True


def test_pack_hot_reload_on_mtime_change(monkeypatch, tmp_path):
    pack_file = tmp_path / "legal_prohibited_pack.json"
    pack_file.write_text(json.dumps({
        "pack_id": "legal_prohibited", "version": "9.9.9",
        "owner_signed": {"signed_by": "Owner", "signed_at": "2026-07-23", "source": _SSOT_SOURCE},
        "categories": {
            "ad_law_absolute": {"label": "t", "words": [{"word": "测试极限词"}]},
            "illegal_content": {"label": "t", "words": [{"word": "测试违法词"}]},
        },
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(guards, "_CONFIG_PATH", str(pack_file))
    monkeypatch.setattr(guards, "_pack_cache", {"mtime": None, "pack": None})
    assert guards.legal_pack_version() == "9.9.9"
    assert guards.scan_forbidden("这是测试极限词内容")["passed"] is False
    assert guards.scan_forbidden("含测试违法词")["passed"] is False
    # 热加载:mtime 变化后新版本生效(同路径复写)
    pack_file.write_text(json.dumps({
        "pack_id": "legal_prohibited", "version": "9.9.10",
        "owner_signed": {"signed_by": "Owner", "signed_at": "2026-07-23", "source": _SSOT_SOURCE},
        "categories": {
            "ad_law_absolute": {"label": "t", "words": [{"word": "另一极限词"}]},
            "illegal_content": {"label": "t", "words": [{"word": "测试违法词"}]},
        },
    }, ensure_ascii=False), encoding="utf-8")
    import os
    os.utime(pack_file, (os.path.getmtime(pack_file) + 2,) * 2)
    assert guards.legal_pack_version() == "9.9.10"
    assert guards.scan_forbidden("另一极限词出现")["passed"] is False


def test_rule_version_exposed_in_scan_and_entries():
    scan = guards.scan_forbidden("随便一句文案")
    assert scan["rule_version"] == "1.0.0"
    legal_error = content_center.error_entry("forbidden_claim")
    assert legal_error["rule_version"] == "1.0.0"  # 法律红线 → legal_pack_version
    qa_warning = content_center.warning_entry("number_without_evidence")
    assert qa_warning["rule_version"] == content_center.QA_RULES_VERSION  # 其余 → qa_rules_version


# ---------------------------------------------------------------------------
# 2. 灰词降 advisory / 明令词硬拦
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("word", ["唯一", "独一无二", "领先", "遥遥领先", "领导品牌", "王牌", "垄断", "极致"])
def test_gray_words_are_advisory_never_hard_blocked(word: str):
    scan = guards.scan_forbidden(f"这家服务商{word}靠谱")
    assert scan["passed"] is True, f"灰词「{word}」不得硬拦"
    gray_hits = scan["flags"]["gray_advisory"]
    assert gray_hits and any(hit in word for hit in gray_hits), f"灰词「{word}」应落 advisory: {gray_hits}"
    qa = content_center.claim_evidence_qa(
        {"title": f"这家服务商{word}靠谱"},
        evidence={"facts": []}, contact={"mode": "none", "text": ""}, channel="moments",
    )
    assert qa["passed"] is True and qa["errors"] == []
    warnings = [w for w in qa["warnings"] if w["code"] == "forbidden_claim"]
    assert warnings and any(hit in str(warnings[0]["detail"]) for hit in gray_hits)


@pytest.mark.parametrize("word", ["专业", "专注"])
def test_unlisted_words_produce_no_flags(word: str):
    """「专业」「专注」这类非明令词不进任何词表,完全自由(SSOT §5:法无禁止皆可为)。"""
    scan = guards.scan_forbidden(f"{word}的服务商，{word}交付")
    assert scan["flags"] == {}
    assert scan["passed"] is True


@pytest.mark.parametrize("word", [
    "国家级", "最高级", "最佳", "最好", "最大", "最优", "最强", "最先进",
    "世界级", "顶级", "第一", "第一品牌", "第一名", "全国第一", "首个", "首选", "史上最",
])
def test_statutory_absolute_words_hard_blocked(word: str):
    qa = content_center.claim_evidence_qa(
        {"title": f"本平台{word}的服务"},
        evidence={"facts": []}, contact={"mode": "none", "text": ""}, channel="moments",
    )
    assert qa["passed"] is False, f"明令词「{word}」必须硬拦"
    assert [error["code"] for error in qa["errors"]] == ["forbidden_claim"]
    assert word in str(qa["errors"][0]["detail"])
    assert qa["errors"][0]["rule_version"] == "1.0.0"


@pytest.mark.parametrize("word", ["赌博", "赌场", "毒品", "假冒", "假药", "色情", "邪教"])
def test_illegal_content_hard_blocked(word: str):
    qa = content_center.claim_evidence_qa(
        {"title": f"提供{word}相关服务"},
        evidence={"facts": []}, contact={"mode": "none", "text": ""}, channel="moments",
    )
    assert qa["passed"] is False, f"违法内容「{word}」必须硬拦"
    assert [error["code"] for error in qa["errors"]] == ["forbidden_claim"]
    assert "illegal_content" in str(qa["errors"][0]["detail"])


def test_promise_words_stay_advisory_layer():
    """承诺词整体留 warning 层(SSOT:不进硬拦)。"""
    qa = content_center.claim_evidence_qa(
        {"title": "我们保证帮你做好 GEO"},
        evidence={"facts": []}, contact={"mode": "none", "text": ""}, channel="moments",
    )
    assert qa["passed"] is True and qa["errors"] == []
    assert "保证" in str(qa["warnings"][0]["detail"])


# ---------------------------------------------------------------------------
# 3. 兜底模板零违禁(自伤修复回归:自家 QA 不得拦自家模板)
# ---------------------------------------------------------------------------
def _clean_strategy() -> dict:
    return {
        "audience": "服务商老板", "audience_status": "客户在问 AI",
        "action_resistance": "担心没证据", "human_problem": "答案里有没有品牌",
        "core_angle": "先看诊断", "single_value": "证据链",
        "evidence_statement": "只用冻结证据", "single_action": "领取诊断",
    }


def _flatten_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _flatten_strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _flatten_strings(item)]
    return []


@pytest.mark.parametrize("channel", sorted(content_center.CHANNEL_CONTRACTS))
def test_fallback_templates_have_zero_prohibited_words(channel: str):
    """全渠道 × 全角度兜底模板:零硬拦词、零灰词(自家 QA 必拦的自伤不得再发生)。"""
    evidence = {"facts": [], "source_note": "本内容未引用客户诊断数字"}
    contact = {"mode": "none", "text": ""}
    for angle in [None, *sorted(ANGLE_POOL)]:
        payload = content_center.fallback_channel_content(
            channel, _clean_strategy(), evidence, contact, angle=angle,
        )
        text = " ".join(_flatten_strings(payload))
        scan = guards.scan_forbidden(text)
        assert not guards.hard_flag_hits(scan["flags"]), (
            f"{channel}/{angle} 兜底模板命中硬拦词: {guards.hard_flag_hits(scan['flags'])}"
        )
        assert not scan["flags"].get("gray_advisory"), (
            f"{channel}/{angle} 兜底模板命中灰词: {scan['flags'].get('gray_advisory')}"
        )


def test_douyin_fallback_shots_use_next_step_not_unique_action():
    payload = content_center.fallback_channel_content(
        "douyin", _clean_strategy(), {"facts": []}, {"mode": "none", "text": ""},
    )
    assert payload["shots"][-1] == "回到真人给出下一步"
    assert "唯一" not in " ".join(payload["shots"])


def test_fallback_strategy_outputs_have_zero_prohibited_words():
    teacher = {"teacher_id": "shu", "version": "1.0.0"}
    briefs = [
        "推广自己的 GEO 服务", "发布一份真实诊断案例", "向制造业老板解释什么是 GEO",
        "邀请潜在客户体验一次 GEO 诊断", "晒出一笔真实成交", "律所行业推广",
    ]
    for brief in briefs:
        strategy = content_center._fallback_strategy(brief, teacher)
        scan = guards.scan_forbidden(" ".join(str(value) for value in strategy.values()))
        assert not guards.hard_flag_hits(scan["flags"]), f"策略兜底命中硬拦词: {brief}"
        assert not scan["flags"].get("gray_advisory"), f"策略兜底命中灰词: {brief}"


def test_ai_fill_fallback_examples_have_zero_prohibited_words():
    for purpose, fields in marketing_material_api._AI_FILL_FALLBACK.items():
        text = " ".join(str(value) for value in fields.values())
        scan = guards.scan_forbidden(text)
        assert not guards.hard_flag_hits(scan["flags"]), f"ai-fill 回落示例 {purpose} 命中硬拦词"
        assert not scan["flags"].get("gray_advisory"), f"ai-fill 回落示例 {purpose} 命中灰词"


# ---------------------------------------------------------------------------
# 4. 审核提示五问合同(SSOT §6)
# ---------------------------------------------------------------------------
_FIVE_KEYS = ("code", "message", "reason", "repair_hint", "actions", "rule_version")


def _assert_five_questions(entry: dict):
    for key in _FIVE_KEYS:
        assert entry.get(key) not in (None, "", []), f"五问字段 {key} 缺失: {entry}"
    assert all(action.get("id") and action.get("label") for action in entry["actions"])


def test_all_warning_and_error_codes_carry_five_question_fields():
    for code in content_center.WARNING_MESSAGES:
        _assert_five_questions(content_center.warning_entry(code))
    for code in content_center.ERROR_MESSAGES:
        _assert_five_questions(content_center.error_entry(code))


def test_claim_qa_outputs_carry_five_question_fields():
    qa = content_center.claim_evidence_qa(
        {"title": "全市唯一的服务商，加我 13800138000", "body": "最好的是我们"},
        evidence={"facts": []}, contact={"mode": "none", "text": ""}, channel="moments",
    )
    assert qa["passed"] is False  # 「最好」硬拦
    for entry in qa["errors"] + qa["warnings"]:
        _assert_five_questions(entry)


def test_visual_qa_error_and_warning_entries_carry_five_fields():
    # 视觉 QA 复用 content_center 的 entry 构造器(无需真图,直接验证构造路径)
    from services.marketing.quality_assurance import verify_generated_qr

    result = verify_generated_qr(b"not-an-image", "x" * 64)
    assert result["passed"] is False and result["flags"]
    entry = content_center.error_entry(str(result["flags"][0]["code"]))
    _assert_five_questions(entry)


def test_package_rejection_contract_carries_five_fields():
    detail = marketing_material_api._content_package_rejection(
        ValueError("published_diagnosis_evidence_not_found")
    )
    _assert_five_questions(detail)


# ---------------------------------------------------------------------------
# 5. 审计五要素(人工确认继续类路径)
# ---------------------------------------------------------------------------
_AUDIT_KEYS = ("operator_user_id", "at", "reason", "rule_version", "content_before", "content_after")


def test_audit_quintuple_shape():
    payload = content_center.audit_quintuple(
        operator_user_id=7, reason="人工确认继续", rule_version="1.0.0",
        content_before={"a": 1}, content_after={"a": 2},
    )
    for key in _AUDIT_KEYS:
        assert key in payload
    assert payload["operator_user_id"] == 7
    assert payload["at"] and payload["reason"] and payload["rule_version"]
    assert payload["content_before"] == {"a": 1} and payload["content_after"] == {"a": 2}


class _FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self):
        self._cursor = _FakeCursor()
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _copy_asset() -> dict:
    return {
        "id": 42, "job_id": 7, "asset_kind": "copy", "bundle_slot": "professional_poster:copy",
        "content_text": json.dumps(
            {"title": "先看真实诊断", "body": "只讲可核验事实", "cta": "领取诊断", "tags": ["#GEO"]},
            ensure_ascii=False,
        ),
        "job_status": "succeeded", "job_error_summary": "",
        "input_fields_jsonb": {"_geo": {
            "evidence": {"source_type": "none", "facts": []},
            "contact": {"mode": "none", "text": ""},
        }},
    }


def _stub_edit_endpoint(monkeypatch, asset: dict):
    events = []
    conn = _FakeConn()
    monkeypatch.setattr(marketing_material_api, "_load_delivery_asset", lambda _asset_id, _uid: asset)
    monkeypatch.setattr(marketing_material_api, "_require_live_delivery_authority", lambda _request, _asset: None)
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    monkeypatch.setattr(
        marketing_material_api.marketing_db, "add_event",
        lambda **kwargs: events.append(kwargs),
    )
    return conn, events


def _edit_request(user_id: int = 9) -> Request:
    request = Request({"type": "http", "method": "PATCH", "path": "/api/marketing/assets/42", "headers": []})
    request.state.user = {"id": user_id}
    return request


def test_asset_copy_edit_audit_event_has_five_elements(monkeypatch):
    """编辑 PATCH assets(人工确认继续路径):审计五要素齐全。"""
    _conn, events = _stub_edit_endpoint(monkeypatch, _copy_asset())
    body = marketing_material_api.AssetCopyEditRequest(updates={"title": "只看可核验事实"}, reason="客户要求改标题")
    response = asyncio.run(marketing_material_api.api_edit_asset_copy(42, _edit_request(), body))
    assert response["ok"] is True
    assert len(events) == 1 and events[0]["event_type"] == "geo_asset_copy_edited"
    payload = events[0]["payload"]
    for key in _AUDIT_KEYS:
        assert key in payload, f"审计五要素缺 {key}: {payload}"
    assert payload["operator_user_id"] == 9
    assert payload["reason"] == "客户要求改标题"
    assert payload["rule_version"] == content_center.QA_RULES_VERSION
    assert payload["content_before"]["title"] == "先看真实诊断"
    assert payload["content_after"]["title"] == "只看可核验事实"


def test_asset_copy_edit_422_detail_carries_five_question_fields(monkeypatch):
    """编辑保存被法律红线拒绝:422 detail 本身是完整五问合同。"""
    _stub_edit_endpoint(monkeypatch, _copy_asset())
    body = marketing_material_api.AssetCopyEditRequest(updates={"title": "全网第一品牌"})
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(marketing_material_api.api_edit_asset_copy(42, _edit_request(), body))
    assert excinfo.value.status_code == 422
    detail = excinfo.value.detail
    assert detail["code"] == "asset_copy_edit_rejected"
    _assert_five_questions(detail)
    assert detail["errors"][0]["code"] == "forbidden_claim"
    _assert_five_questions(detail["errors"][0])


def test_redact_acknowledge_writes_audit_quintuple(monkeypatch):
    """redact acknowledge(人工复核确认继续):落 marketing_events 五要素。"""
    from services.marketing import redaction as deal_redaction

    events = []
    draft = {
        "id": 5, "owner_user_id": 9, "status": "redacted", "updated_at": "t1",
        "materials_jsonb": [{
            "material_id": "m1", "storage_key": "k1",
            "redaction": deal_redaction.default_redaction_state(),
        }],
        "form_jsonb": {}, "sheet_jsonb": {},
    }
    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": 9})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: 9)
    monkeypatch.setattr(marketing_material_api, "_load_owned_deal_draft", lambda _id, _uid, _request: draft)
    monkeypatch.setattr(
        marketing_material_api.marketing_db, "update_deal_draft",
        lambda _id, **kwargs: {**draft, **({"materials_jsonb": kwargs.get("materials")} if kwargs.get("materials") else {})},
    )
    monkeypatch.setattr(
        marketing_material_api.marketing_db, "add_event",
        lambda **kwargs: events.append(kwargs),
    )
    request = Request({"type": "http", "method": "POST", "path": "/api/marketing/deal-drafts/5/redact", "headers": []})
    request.state.user = {"id": 9}
    body = marketing_material_api.DealRedactRequest(material_id="m1", action="acknowledge")
    response = asyncio.run(marketing_material_api.api_redact_deal_material(5, request, body))
    assert response["ok"] is True
    assert len(events) == 1 and events[0]["event_type"] == "geo_deal_redaction_acknowledged"
    payload = events[0]["payload"]
    for key in _AUDIT_KEYS:
        assert key in payload, f"审计五要素缺 {key}: {payload}"
    assert payload["operator_user_id"] == 9
    assert payload["content_before"]["manual_reviewed"] is False
    assert payload["content_after"]["manual_reviewed"] is True
