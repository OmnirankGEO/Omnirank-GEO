"""GEO v2 Phase 2 晒成交:三路输入合并/防编造确认单/打码引擎/showcase_deal 生成接线。

DB-free mock 风格(照抄 test_v2_contracts.py):不连真实 PG、不打真实 LLM/vision/ASR。
"""
import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db.connection
from api import marketing_material_api
from db import marketing_db
from services.marketing import content_center, deal_intake, geo_factory, redaction
from services.marketing.evidence import freeze_evidence
import tools.multi_llm_caller
import tools.vision.image_describe


# ---------------------------------------------------------------------------
# 共用桩
# ---------------------------------------------------------------------------
def _png_bytes(size: tuple[int, int] = (200, 300)) -> bytes:
    image = Image.new("RGB", size)
    pixels = image.load()
    for x in range(size[0]):
        for y in range(size[1]):
            pixels[x, y] = (x % 256, y % 256, (x + y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _stub_user(monkeypatch, user_id: int = 9):
    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": user_id})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: user_id)
    monkeypatch.setattr(marketing_material_api, "_principal_user_id", lambda _request, _user: user_id)


def _no_llm(monkeypatch):
    async def fake_call(_prompt, verbose=False, before_provider_call=None):
        return ""

    monkeypatch.setattr(tools.multi_llm_caller, "call_llm_with_fallback", fake_call)


class _SeqCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _SeqConn:
    def __init__(self, rows):
        self._cursor = _SeqCursor(rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 1. 三路输入:只聊天 / 只合同 / 只语音 / 只表单 / 混合
# ---------------------------------------------------------------------------
def test_intake_chat_screenshot_only(monkeypatch):
    _no_llm(monkeypatch)
    sheet = asyncio.run(deal_intake.extract_sheet(
        form={},
        ocr_texts=["王总:你们这个 GEO 诊断怎么收费\n我:年单 5万元\n王总:行,那定了"],
    ))
    assert sheet["deal_amount"]["value"] == "5万元"
    assert sheet["deal_amount"]["status"] == "tentative"  # OCR 提取一律待确认
    assert sheet["deal_amount"]["provenance"] == "customer_asserted"
    assert sheet["what_happened"]["value"]  # 附原文,不阻断
    assert sheet["what_happened"]["status"] == "tentative"
    assert sheet["customer_industry"]["value"] == ""  # 没有就空着,不编造
    assert sheet["_sources"]["ocr_texts"]


def test_intake_contract_image_only(monkeypatch):
    _no_llm(monkeypatch)
    sheet = asyncio.run(deal_intake.extract_sheet(
        form={},
        ocr_texts=["GEO 服务合同\n合同编号 HT-2026-071\n金额:12万元\n签订日期 2026-07-15"],
    ))
    assert sheet["deal_amount"]["value"] == "12万元"
    assert sheet["deal_amount"]["status"] == "tentative"
    assert sheet["deal_time"]["value"] == "2026-07-15"
    assert sheet["deal_time"]["status"] == "tentative"


def test_intake_voice_only(monkeypatch):
    _no_llm(monkeypatch)
    sheet = asyncio.run(deal_intake.extract_sheet(
        form={},
        voice_transcript="上周和一个装修客户签了 GEO 诊断年单,成交金额 5万元,他说最认可响应速度",
    ))
    assert sheet["deal_amount"]["value"] == "5万元"
    assert sheet["deal_amount"]["status"] == "tentative"
    assert sheet["_sources"]["voice_transcript"].startswith("上周")
    assert sheet["_meta"]["extraction"] == "rule_fallback"


def test_intake_form_only_fields_confirmed(monkeypatch):
    _no_llm(monkeypatch)
    sheet = asyncio.run(deal_intake.extract_sheet(
        form={"what_happened": "签了 GEO 诊断年单", "deal_amount": "5万元"},
    ))
    assert sheet["what_happened"]["status"] == "confirmed"  # 表单优先且已确认
    assert sheet["deal_amount"]["value"] == "5万元"
    assert sheet["deal_amount"]["status"] == "confirmed"
    assert sheet["deal_time"]["value"] == "" and sheet["deal_time"]["status"] == "tentative"


def test_intake_mixed_inputs_form_wins(monkeypatch):
    _no_llm(monkeypatch)
    sheet = asyncio.run(deal_intake.extract_sheet(
        form={"deal_amount": "5万元", "service_content": "GEO 诊断年单"},
        voice_transcript="客户是装修行业的",
        ocr_texts=["金额 8万元"],  # 与表单冲突:表单已确认,绝不覆盖
    ))
    assert sheet["deal_amount"]["value"] == "5万元"
    assert sheet["deal_amount"]["status"] == "confirmed"
    assert sheet["service_content"]["value"] == "GEO 诊断年单"


# ---------------------------------------------------------------------------
# 2. 防编造标注(Owner 2026-07-22):LLM 产出里语料不存在的具体数字/名称 →
#    保留值 + tentative + _meta.warnings 提醒(含字段名),不再剔除
# ---------------------------------------------------------------------------
def test_intake_llm_fabricated_amount_and_name_are_annotated_not_stripped(monkeypatch):
    async def fake_llm(_prompt, verbose=False, before_provider_call=None):
        return json.dumps({
            "deal_amount": "99万元",          # 语料只有 5万元 → 保留 + 标注
            "quotable_lines": "李总非常满意",   # 语料没有"李总" → 保留 + 标注
            "customer_praise": "响应速度特别快",  # 语料有"响应速度" → 保留,不标注
        }, ensure_ascii=False)

    monkeypatch.setattr(tools.multi_llm_caller, "call_llm_with_fallback", fake_llm)
    sheet = asyncio.run(deal_intake.extract_sheet(
        form={},
        voice_transcript="签了 5万元的单子,客户说响应速度特别快",
    ))
    assert sheet["deal_amount"]["value"] == "99万元"  # 保留 AI 提取值,不剔除
    assert sheet["deal_amount"]["status"] == "tentative"
    assert sheet["quotable_lines"]["value"] == "李总非常满意"
    assert sheet["quotable_lines"]["status"] == "tentative"
    assert sheet["customer_praise"]["value"] == "响应速度特别快"
    assert sheet["customer_praise"]["status"] == "tentative"  # 提取结果一律待确认
    assert sheet["_meta"]["extraction"] == "ai"
    warnings = {warning["field"]: warning for warning in sheet["_meta"]["warnings"]}
    assert set(warnings) == {"deal_amount", "quotable_lines"}
    assert all(warning["code"] == "ai_extraction_beyond_materials" for warning in warnings.values())
    assert "超出了你提供的材料" in warnings["deal_amount"]["message"]
    assert "成交金额" in warnings["deal_amount"]["message"]  # 含字段中文名


def test_intake_llm_amount_must_match_corpus_exactly(monkeypatch):
    async def fake_llm(_prompt, verbose=False, before_provider_call=None):
        return json.dumps({"deal_amount": "5万元"}, ensure_ascii=False)

    monkeypatch.setattr(tools.multi_llm_caller, "call_llm_with_fallback", fake_llm)
    sheet = asyncio.run(deal_intake.extract_sheet(
        form={}, voice_transcript="成交金额 5万元,年单",
    ))
    assert sheet["deal_amount"]["value"] == "5万元"
    assert sheet["deal_amount"]["status"] == "tentative"  # 一致也只是 tentative


def test_facts_and_strategy_only_use_confirmed_fields():
    sheet = deal_intake.empty_sheet()
    sheet["deal_amount"] = {"value": "5万元", "status": "confirmed", "provenance": "customer_asserted"}
    sheet["deal_time"] = {"value": "2026-07-15", "status": "tentative", "provenance": "customer_asserted"}
    sheet["service_content"] = {"value": "GEO 诊断年单", "status": "confirmed", "provenance": "customer_asserted"}
    facts = deal_intake.facts_from_sheet(sheet)
    keys = {fact["key"] for fact in facts}
    assert "deal_amount" in keys and "service_content" in keys
    assert "deal_time" not in keys  # tentative 永不进 facts
    assert {fact["provenance"] for fact in facts} == {"customer_asserted"}
    strategy = deal_intake.strategy_from_sheet(sheet)
    for field in (
        "audience", "audience_status", "action_resistance", "human_problem",
        "core_angle", "single_value", "evidence_statement", "single_action",
    ):
        assert strategy[field]  # 缺口由通用非承诺表述补齐
    assert strategy["provenance"] == "customer_asserted"
    assert "5万元" in strategy["evidence_statement"]


# ---------------------------------------------------------------------------
# 3. 打码:auto 检测 / 手动新增 / 点选恢复 / 强度 / 预览
# ---------------------------------------------------------------------------
def test_redaction_regions_from_ocr_line_estimation():
    ocr = "客户:王总\n转账 13800138000\n合同编号 HT-2026-071\n金额 5万元\n好的"
    regions = redaction.regions_from_ocr(ocr, width=400, height=1000)
    kinds = {region["kind"] for region in regions}
    assert "customer_name" in kinds
    assert "phone" in kinds
    assert "contract_no" in kinds
    assert "amount" in kinds
    assert all(region["source"] == "auto" and region["active"] for region in regions)
    # 整行区域估计:覆盖整宽,纵向互不越界
    for region in regions:
        x, y, w, h = region["box"]
        assert x == 0 and w == 400 and 0 <= y < 1000 and h > 0


def test_redaction_pixelate_restore_strength_preview():
    raw = _png_bytes((200, 300))
    state = redaction.default_redaction_state()
    region = redaction.add_manual_region(state, kind="phone", box=[0, 0, 100, 60])
    assert redaction.active_regions(state) == [region]

    preview = redaction.render_redacted(raw, redaction.active_regions(state), strength="medium")
    assert preview != raw
    with Image.open(io.BytesIO(preview)) as rendered, Image.open(io.BytesIO(raw)) as original:
        assert rendered.getpixel((150, 150)) == original.getpixel((150, 150))  # 区域外不动
        assert rendered.getpixel((5, 5)) != original.getpixel((5, 5))  # 区域内已像素化

    # 点选恢复:区域失活并进 restored_ids,不再参与遮挡
    redaction.restore_region(state, region["id"])
    assert redaction.active_regions(state) == []
    assert state["restored_ids"] == [region["id"]]
    restored = redaction.render_redacted(raw, redaction.active_regions(state), strength="medium")
    with Image.open(io.BytesIO(restored)) as rendered2, Image.open(io.BytesIO(raw)) as original2:
        assert rendered2.getpixel((5, 5)) == original2.getpixel((5, 5))

    # 强度档位:heavy 块更大;非法档位 fail-closed
    redaction.add_manual_region(state, kind="phone", box=[0, 0, 100, 60])
    heavy = redaction.render_redacted(raw, redaction.active_regions(state), strength="heavy")
    assert heavy != preview
    assert redaction.normalize_strength("MEDIUM") == "medium"
    with pytest.raises(ValueError, match="redaction_strength_invalid"):
        redaction.normalize_strength("extreme")


def test_redaction_auto_detect_fail_soft(monkeypatch):
    async def fake_describe(_bytes, _name, before_provider_call=None):
        return {"vision_ok": False, "ocr_text": "", "risk_flags": []}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", fake_describe)
    result = asyncio.run(redaction.auto_detect(_png_bytes(), width=200, height=300))
    assert result["regions"] == []
    assert result["detection"] == "unavailable"  # 明确提示,不阻断手动打码

    async def fake_ok(_bytes, _name, before_provider_call=None):
        return {"vision_ok": True, "ocr_text": "手机号 13800138000", "risk_flags": []}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", fake_ok)
    detected = asyncio.run(redaction.auto_detect(_png_bytes(), width=200, height=300))
    assert [region["kind"] for region in detected["regions"]] == ["phone"]
    assert detected["detection"] == "ocr_regex"


# ---------------------------------------------------------------------------
# 4. 草稿幂等:request_id 重复提交 replay;不同 payload → 409 冲突
# ---------------------------------------------------------------------------
def _draft_row(request_hash: str = "h" * 64) -> dict:
    return {
        "id": 31, "owner_user_id": 9, "brand_id": None,
        "request_id": "deal-draft-001", "form_jsonb": {"_request_hash": request_hash},
        "materials_jsonb": [], "sheet_jsonb": {}, "status": "draft",
        "created_at": None, "updated_at": None,
    }


def test_deal_draft_request_id_idempotent_replay_and_conflict(monkeypatch):
    # 首次创建:SELECT 空 → INSERT RETURNING
    conn = _SeqConn([None, _draft_row()])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    draft, created = marketing_db.create_or_get_deal_draft(
        owner_user_id=9, request_id="deal-draft-001", request_hash="h" * 64, form={},
    )
    assert created is True and draft["id"] == 31 and conn.commits == 1
    # 唯一锚点按属主收窄:SELECT 必须带 owner_user_id(与 (owner,request_id) 唯一索引同粒度)
    select_sql = next(sql for sql, _ in conn._cursor.executed if "FROM marketing_deal_drafts" in sql)
    assert "owner_user_id" in select_sql

    # 重复提交(同 payload):直接 replay,不再 INSERT
    conn = _SeqConn([_draft_row()])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    draft, created = marketing_db.create_or_get_deal_draft(
        owner_user_id=9, request_id="deal-draft-001", request_hash="h" * 64, form={},
    )
    assert created is False and draft["id"] == 31
    assert not any("INSERT INTO marketing_deal_drafts" in sql for sql, _ in conn._cursor.executed)

    # 同 request_id 不同 payload → 硬冲突(409 由 API 层映射)
    conn = _SeqConn([_draft_row(request_hash="x" * 64)])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    with pytest.raises(ValueError, match="deal_draft_request_id_conflict"):
        marketing_db.create_or_get_deal_draft(
            owner_user_id=9, request_id="deal-draft-001", request_hash="h" * 64, form={},
        )

    # 他人同 request_id 不再冲突:唯一约束按属主收窄,跨租户各建各的草稿
    # (防抢占 DoS/存在性 oracle;SELECT 按 owner 收窄,天然看不到他人行)
    other = {**_draft_row(), "id": 88, "owner_user_id": 77}
    conn = _SeqConn([None, other])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    draft, created = marketing_db.create_or_get_deal_draft(
        owner_user_id=77, request_id="deal-draft-001", request_hash="h" * 64, form={},
    )
    assert created is True and draft["owner_user_id"] == 77


def test_deal_draft_previous_hash_recipe_replays_only_same_authority(monkeypatch):
    """Hash-version rollout keeps lost-response recovery without reviving a
    previous membership generation or accepting a changed payload."""
    legacy_hash = "l" * 64
    row = {
        **_draft_row(request_hash=legacy_hash),
        "brand_id": 51,
        "organization_id": 7,
        "created_by_membership_id": 19,
        "form_jsonb": {"deal_amount": "3万元", "_request_hash": legacy_hash},
    }
    conn = _SeqConn([row])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    draft, created = marketing_db.create_or_get_deal_draft(
        owner_user_id=9,
        request_id="deal-draft-001",
        request_hash="n" * 64,
        compatible_request_hashes=(legacy_hash,),
        brand_id=51,
        organization_id=7,
        created_by_membership_id=19,
        form={"deal_amount": "3万元"},
    )
    assert created is False and draft["id"] == 31

    for mismatch in (
        {"created_by_membership_id": 20},
        {"organization_id": 8},
        {"brand_id": 52},
        {"form": {"deal_amount": "4万元"}},
    ):
        conn = _SeqConn([row])
        monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
        kwargs = {
            "owner_user_id": 9,
            "request_id": "deal-draft-001",
            "request_hash": "n" * 64,
            "compatible_request_hashes": (legacy_hash,),
            "brand_id": 51,
            "organization_id": 7,
            "created_by_membership_id": 19,
            "form": {"deal_amount": "3万元"},
            **mismatch,
        }
        with pytest.raises(ValueError, match="deal_draft_request_id_conflict"):
            marketing_db.create_or_get_deal_draft(**kwargs)


class _UniqueViolation(Exception):
    pgcode = "23505"


class _InsertFailCursor(_SeqCursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "INSERT INTO marketing_deal_drafts" in sql:
            raise _UniqueViolation("duplicate key value violates unique constraint")


class _InsertFailConn:
    def __init__(self, rows):
        self._cursor = _InsertFailCursor(rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def test_deal_draft_concurrent_insert_race_rechecks_instead_of_500(monkeypatch):
    """并发同 (owner, request_id) 首发:INSERT 撞唯一约束 → 锁外重查 → replay/409,不裸 500。"""
    # 同 hash:winner 行重查命中 → replay(created=False)
    race_conn = _InsertFailConn([None])
    winner_conn = _SeqConn([_draft_row()])
    conns = iter([race_conn, winner_conn])
    monkeypatch.setattr(db.connection, "get_connection", lambda: next(conns))
    draft, created = marketing_db.create_or_get_deal_draft(
        owner_user_id=9, request_id="deal-draft-001", request_hash="h" * 64, form={},
    )
    assert created is False and draft["id"] == 31

    # 异 hash:重查命中但 hash 不等 → 409 口径,不是 500
    race_conn = _InsertFailConn([None])
    winner_conn = _SeqConn([_draft_row(request_hash="x" * 64)])
    conns = iter([race_conn, winner_conn])
    monkeypatch.setattr(db.connection, "get_connection", lambda: next(conns))
    with pytest.raises(ValueError, match="deal_draft_request_id_conflict"):
        marketing_db.create_or_get_deal_draft(
            owner_user_id=9, request_id="deal-draft-001", request_hash="h" * 64, form={},
        )


def test_update_deal_draft_optimistic_lock_conflict(monkeypatch):
    # UPDATE ... WHERE id AND updated_at 未命中 + 行仍在 → 冲突信号(调用方重取)
    conn = _SeqConn([None, {"exists": 1}])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    with pytest.raises(ValueError, match="deal_draft_update_conflict"):
        marketing_db.update_deal_draft(31, materials=[], expected_updated_at="2026-07-22 00:00:00")
    update_sql = next(sql for sql, _ in conn._cursor.executed if sql.startswith("UPDATE marketing_deal_drafts"))
    assert "updated_at=%s" in update_sql  # 乐观锁条件真的带上了

    # 行不存在 → not_found 口径不变
    conn = _SeqConn([None, None])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    with pytest.raises(ValueError, match="deal_draft_not_found"):
        marketing_db.update_deal_draft(31, materials=[], expected_updated_at="2026-07-22 00:00:00")

    # 条件命中 → 正常返回
    conn = _SeqConn([_draft_row()])
    monkeypatch.setattr(db.connection, "get_connection", lambda: conn)
    updated = marketing_db.update_deal_draft(31, materials=[], expected_updated_at="2026-07-22 00:00:00")
    assert updated["id"] == 31 and conn.commits == 1


def test_deal_draft_api_create_conflict_maps_409(monkeypatch):
    _stub_user(monkeypatch)

    def conflict(**_kwargs):
        raise ValueError("deal_draft_request_id_conflict")

    monkeypatch.setattr(marketing_db, "create_or_get_deal_draft", conflict)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    response = TestClient(app).post("/api/marketing/deal-drafts", json={
        "request_id": "deal-draft-409", "form": {"deal_amount": "5万元"},
    })
    assert response.status_code == 409
    assert response.json()["detail"] == "deal_draft_request_id_conflict"

    bad = TestClient(app).post("/api/marketing/deal-drafts", json={
        "request_id": "bad id!!", "form": {},
    })
    assert bad.status_code == 422
    assert bad.json()["detail"] == "request_id_invalid"


# ---------------------------------------------------------------------------
# 5. 端点链路:素材上传(EXIF 剥离 + 私有存储)→ analyze → PATCH → 打码 → 授权读取
# ---------------------------------------------------------------------------
def _setup_draft_store(monkeypatch, tmp_path):
    """Statefull draft stub: update_deal_draft 直接改写内存里的草稿。"""
    monkeypatch.setenv("MARKETING_PRIVATE_UPLOAD_ROOT", str(tmp_path / "marketing-private"))
    draft = _draft_row()
    saved = {}

    def update(_draft_id, **kwargs):
        if kwargs.get("form") is not None:
            draft["form_jsonb"] = kwargs["form"]
        if kwargs.get("materials") is not None:
            draft["materials_jsonb"] = kwargs["materials"]
        if kwargs.get("sheet") is not None:
            draft["sheet_jsonb"] = kwargs["sheet"]
        if kwargs.get("status") is not None:
            draft["status"] = kwargs["status"]
        saved.update(kwargs)
        return dict(draft)

    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: dict(draft))
    monkeypatch.setattr(marketing_db, "update_deal_draft", update)
    monkeypatch.setattr(marketing_db, "add_event", lambda **kwargs: None)
    return draft, saved


def _upload_material(client, draft_id: int = 31) -> dict:
    response = client.post(
        f"/api/marketing/deal-drafts/{draft_id}/materials",
        files=[("files", ("chat.png", _png_bytes(), "image/png"))],
    )
    assert response.status_code == 200, response.text
    return response.json()["added"][0]


def test_deal_material_upload_analyze_patch_redact_chain(monkeypatch, tmp_path):
    _stub_user(monkeypatch)
    draft, _saved = _setup_draft_store(monkeypatch, tmp_path)
    _no_llm(monkeypatch)

    async def fake_describe(_bytes, _name, before_provider_call=None):
        return {"vision_ok": True, "ocr_text": "客户:王总\n成交金额 5万元", "risk_flags": []}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", fake_describe)

    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)

    # 上传:EXIF 剥离 + 私有存储 + 授权 URL 不外泄内部 key
    material = _upload_material(client)
    assert material["material_id"]
    assert "storage_key" not in material and "preview_key" not in json.dumps(material)
    stored_entry = draft["materials_jsonb"][0]
    assert stored_entry["storage_key"].startswith("private://marketing-materials/")
    raw_path = Path(tmp_path / "marketing-private" / "geo-materials" / "u9")
    assert raw_path.is_dir()  # 私有根之下,nginx 静态根之外

    # analyze:OCR 全部素材 + 三路合并 → 确认单 tentative
    analyze = client.post("/api/marketing/deal-drafts/31/analyze")
    assert analyze.status_code == 200, analyze.text
    sheet = analyze.json()["sheet"]
    assert sheet["deal_amount"]["value"] == "5万元"
    assert sheet["deal_amount"]["status"] == "tentative"
    assert draft["materials_jsonb"][0]["extracted"] is True
    assert draft["status"] == "analyzed"

    # PATCH:编辑确认单(编辑即确认)+ 未知字段 422
    bad = client.patch("/api/marketing/deal-drafts/31", json={"sheet": {"order_no": "X"}})
    assert bad.status_code == 422
    assert str(bad.json()["detail"]).startswith("deal_sheet_field_unknown")
    patched = client.patch("/api/marketing/deal-drafts/31", json={
        "sheet": {"deal_amount": "5万元", "customer_praise": {"value": "响应快", "status": "confirmed"}},
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["sheet"]["deal_amount"]["status"] == "confirmed"

    # 打码 auto:检测出 customer_name + amount 区域,预览图生成,与素材分开 key
    redacted = client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "auto",
    })
    assert redacted.status_code == 200, redacted.text
    state = redacted.json()["redaction"]
    assert state["status"] == "previewed"
    assert {region["kind"] for region in state["auto_regions"]} >= {"customer_name", "amount"}
    entry = draft["materials_jsonb"][0]["redaction"]
    assert entry["preview_key"] and entry["preview_key"] != draft["materials_jsonb"][0]["storage_key"]

    # 点选恢复一个自动区域 → 预览重生成
    region_id = state["auto_regions"][0]["id"]
    restored = client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "restore", "region_id": region_id,
    })
    assert restored.status_code == 200
    assert restored.json()["redaction"]["restored_ids"] == [region_id]

    # 强度调整 → 预览重生成;确认 → 成品与预览分开 key
    strengthened = client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "strength", "strength": "heavy",
    })
    assert strengthened.json()["redaction"]["strength"] == "heavy"
    confirmed = client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "confirm",
    })
    assert confirmed.status_code == 200, confirmed.text
    entry = draft["materials_jsonb"][0]["redaction"]
    assert entry["status"] == "confirmed"
    assert entry["output_key"] and entry["output_key"] != entry["preview_key"]
    assert draft["status"] == "confirmed"

    # 授权读取预览:private no-store;变体非法 → 422
    preview = client.get(redacted.json()["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["Cache-Control"] == "private, no-store"
    assert preview.headers["X-Content-Type-Options"] == "nosniff"
    assert preview.content != _png_bytes()  # 预览图已遮挡/重编码
    assert client.get(
        f"/api/marketing/deal-drafts/31/materials/{material['material_id']}/file?variant=hack"
    ).status_code == 422


def test_deal_draft_owner_isolation_and_voice_fail_soft(monkeypatch, tmp_path):
    _stub_user(monkeypatch, user_id=9)
    draft, _saved = _setup_draft_store(monkeypatch, tmp_path)
    draft["owner_user_id"] = 77  # 他人草稿
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    assert client.get("/api/marketing/deal-drafts/31").status_code == 404

    draft["owner_user_id"] = 9
    from services.marketing import deal_intake as _intake_module

    async def no_asr(_bytes, *, suffix=".webm"):
        return {"text": "", "status": "error", "provider": "none"}

    monkeypatch.setattr(_intake_module, "transcribe_voice_bytes", no_asr)
    voice = client.post(
        "/api/marketing/deal-drafts/31/voice",
        files={"file": ("v.wav", b"RIFF" + b"\x00" * 2000, "audio/wav")},
    )
    assert voice.status_code == 422  # ASR 失败不假成功

    async def ok_asr(_bytes, *, suffix=".webm"):
        return {"text": "上周签了 5万元的单子", "status": "success", "provider": "fake"}

    monkeypatch.setattr(_intake_module, "transcribe_voice_bytes", ok_asr)
    voice = client.post(
        "/api/marketing/deal-drafts/31/voice",
        files={"file": ("v.wav", b"RIFF" + b"\x00" * 2000, "audio/wav")},
    )
    assert voice.status_code == 200
    assert voice.json()["transcript"].startswith("上周")
    assert draft["form_jsonb"]["voice_transcript"].startswith("上周")


# ---------------------------------------------------------------------------
# 6. showcase_deal 生成接线
# ---------------------------------------------------------------------------
def _deal_sheet() -> dict:
    sheet = deal_intake.empty_sheet()
    sheet["what_happened"] = {"value": "签了 GEO 诊断年单", "status": "confirmed", "provenance": "customer_asserted"}
    sheet["deal_amount"] = {"value": "5万元", "status": "confirmed", "provenance": "customer_asserted"}
    sheet["customer_praise"] = {"value": "响应速度特别快", "status": "confirmed", "provenance": "customer_asserted"}
    return sheet


def _stub_package_prepare(monkeypatch, draft):
    _stub_user(monkeypatch)
    from auth import brand_access
    from services.marketing import strategy_teachers

    monkeypatch.setattr(brand_access, "require_brand_access", lambda _request, _brand_id: None)
    monkeypatch.setattr(
        strategy_teachers, "resolve_teacher",
        lambda **_kwargs: {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
    )
    monkeypatch.setattr(marketing_material_api, "_freeze_service_brand", lambda _p: {"name": "OmniRank"})
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: draft)
    captured = {}

    async def prepare(**kwargs):
        captured["geo_snapshot"] = kwargs["geo_snapshot"]
        captured["brand_id"] = kwargs.get("brand_id")
        return {"status": "generating", "job_id": 1}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    return TestClient(app), captured


def _showcase_payload(**overrides) -> dict:
    payload = {
        "request_id": "showcase-deal-001",
        "brief": "把这笔真实成交晒出去",
        "quick_task": "showcase_deal",
        "deal_draft_id": 31,
        "channels": ["deal_poster", "deal_chat"],
        "evidence": {"source_type": "none"},
        "contact": {"mode": "none"},
    }
    payload.update(overrides)
    return payload


def test_showcase_deal_never_blocked_by_missing_diagnosis(monkeypatch):
    draft = _draft_row()
    draft["sheet_jsonb"] = _deal_sheet()
    draft["status"] = "confirmed"
    client, captured = _stub_package_prepare(monkeypatch, draft)
    response = client.post("/api/marketing/content-packages", json=_showcase_payload())
    assert response.status_code == 200, response.text  # 无诊断不阻断
    snapshot = captured["geo_snapshot"]
    assert snapshot["quick_task"] == "showcase_deal"
    assert snapshot["evidence"]["source_type"] == "brand_facts"  # 不读诊断 SSOT
    assert {fact["provenance"] for fact in snapshot["evidence"]["facts"]} == {"customer_asserted"}
    assert {fact["key"] for fact in snapshot["evidence"]["facts"]} >= {"deal_amount"}
    strategy = snapshot["strategy"]
    for field in (
        "audience", "audience_status", "action_resistance", "human_problem",
        "core_angle", "single_value", "evidence_statement", "single_action",
    ):
        assert strategy[field]
    assert strategy["teacher_id"] == "shu" and strategy["provenance"] == "customer_asserted"
    deal = snapshot["deal"]
    assert deal["draft_id"] == 31 and deal["operator_user_id"] == 9
    assert deal["sheet_snapshot"]["deal_amount"]["value"] == "5万元"  # 来源快照落库
    assert snapshot["prompt_composer_version"] == "geo-vpc/2.0"

    missing_draft = client.post("/api/marketing/content-packages", json=_showcase_payload(
        request_id="showcase-deal-002", deal_draft_id=None,
    ))
    assert missing_draft.status_code == 422
    assert missing_draft.json()["detail"] == "deal_draft_required"


def test_showcase_deal_evidence_live_recheck_is_noop():
    facts = deal_intake.facts_from_sheet(_deal_sheet())
    frozen = freeze_evidence(brand_id=None, source_type="brand_facts", facts=facts)
    from services.marketing.evidence import require_frozen_evidence_live

    require_frozen_evidence_live(frozen)  # brand_facts 不触 DB、三重拒不适用
    assert frozen["facts"][0]["provenance"] == "customer_asserted"


def test_showcase_deal_contact_none_prompt_qa_chain_zero(monkeypatch):
    # 名称诚实化(2026-07-23 P2-4):生成执行被 stub,本用例覆盖的是 API → 快照 →
    # 回落文案 → QA → visual prompt 的层间合同,不是全链路;真实全链路见
    # test_review_cto_regressions 的 fastapi→worker→settlement 用例。
    draft = _draft_row()
    draft["sheet_jsonb"] = _deal_sheet()
    client, captured = _stub_package_prepare(monkeypatch, draft)
    response = client.post("/api/marketing/content-packages", json=_showcase_payload(
        channels=["deal_poster", "deal_data_card", "deal_feedback_card"],
    ))
    assert response.status_code == 200, response.text
    snapshot = captured["geo_snapshot"]
    assert snapshot["contact"] == {"mode": "none", "text": "", "qr_reference": None}

    # prompt 层:禁联系方式;exact_visible_text 无 contact
    strategy, evidence = snapshot["strategy"], snapshot["evidence"]
    for channel in ("deal_poster", "deal_data_card", "deal_feedback_card"):
        content = content_center.fallback_channel_content(channel, strategy, evidence, snapshot["contact"])
        qa = content_center.claim_evidence_qa(
            content, evidence=evidence, contact=snapshot["contact"], channel=channel,
        )
        assert qa["passed"], qa["warnings"]  # 文案 QA:联系方式关闭全链零(errors 为零)
        prompt = content_center.visual_prompt(
            slot={"slot": channel, "size": "3:4"}, channel=channel,
            strategy=strategy, content=content, evidence=evidence,
            trend={"used": False}, contact=snapshot["contact"], brand={"name": "OmniRank"},
        )
        document = json.loads(prompt.split("\n", 1)[1])
        assert "contact" not in document["exact_visible_text"]
        for word in ("联系方式", "手机号", "二维码"):
            assert word in document["privacy_and_truth_constraints"]


def test_showcase_deal_qr_reference_token_and_payload(monkeypatch):
    draft = _draft_row()
    draft["sheet_jsonb"] = _deal_sheet()
    client, captured = _stub_package_prepare(monkeypatch, draft)
    bad = client.post("/api/marketing/content-packages", json=_showcase_payload(
        request_id="showcase-deal-qr1",
        contact={"mode": "qr", "qr_reference": {
            "reference_id": "abc.png", "payload_hash": "p" * 64, "reference_token": "bad",
        }},
    ))
    assert bad.status_code == 422
    assert bad.json()["detail"] == "qr_reference_token_invalid"

    from services.marketing import quality_assurance

    monkeypatch.setattr(quality_assurance, "verify_qr_reference_token", lambda **_kwargs: True)
    ok = client.post("/api/marketing/content-packages", json=_showcase_payload(
        request_id="showcase-deal-qr2",
        contact={"mode": "qr", "qr_reference": {
            "reference_id": "abc.png", "payload_hash": "p" * 64, "reference_token": "good",
        }},
    ))
    assert ok.status_code == 200, ok.text
    contact = captured["geo_snapshot"]["contact"]
    assert contact["mode"] == "qr"
    assert contact["qr_reference"]["payload_hash"] == "p" * 64  # payload/hash 冻结一致


def test_deal_chat_requires_confirmed_redaction_but_package_survives(monkeypatch):
    # 未打码确认:deal_chat 图片组件失败给人话原因;其余组件不受影响
    geo = {"deal": {"draft_id": 31, "redacted_materials": []}}
    chat_image = {"component_id": "deal_chat:image:deal_chat_scene", "kind": "image", "channel": "deal_chat"}
    reason = geo_factory.deal_component_skip_reason(geo, chat_image)
    assert reason and "打码" in reason and "确认" in reason
    assert geo_factory.deal_component_skip_reason(geo, {"kind": "copy", "channel": "deal_chat"}) is None
    assert geo_factory.deal_component_skip_reason(geo, {"kind": "image", "channel": "deal_poster"}) is None
    confirmed = {"deal": {"redacted_materials": [{"material_id": "m1", "ref": "private://x", "sha256": ""}]}}
    assert geo_factory.deal_component_skip_reason(confirmed, chat_image) is None
    assert geo_factory.deal_component_skip_reason({}, chat_image) is None  # 非晒成交单不受门控

    ids = geo_factory.valid_component_ids(["deal_poster", "deal_chat"])
    assert ids == {
        "deal_poster:copy", "deal_poster:image:deal_poster",
        "deal_chat:copy", "deal_chat:image:deal_chat_scene",
    }

    # job 公开态:skipped_components 人话原因可见,成功资产照常列出
    geo_full = {
        "channels": ["deal_poster", "deal_chat"], "moments_layout": "single",
        "evidence": {"source_type": "brand_facts", "facts": []}, "contact": {"mode": "none"},
        "deal": {
            "draft_id": 31, "version": "2026-07-22", "redacted_materials": [],
            "skipped_components": [{"component_id": "deal_chat:image:deal_chat_scene", "reason": reason}],
        },
    }
    monkeypatch.setattr(marketing_db, "list_assets", lambda _job_id: [])
    job = {
        "id": 5, "user_id": 9, "status": "succeeded", "error_summary": "partial_free_release",
        "created_at": None, "finished_at": None, "input_fields_jsonb": {"_geo": geo_full},
    }
    state = geo_factory.job_public_state(job)
    assert state["status"] == "partial_success"  # 组件失败但整包保留成功项
    assert state["deal"]["skipped_components"][0]["reason"] == reason
    statuses = {item["component_id"]: item["status"] for item in state["components"]}
    assert statuses["deal_chat:image:deal_chat_scene"] == "failed"
    assert "form_snapshot" not in json.dumps(state["deal"])  # 原始输入不进公开态


def test_deal_chat_scene_keeps_example_dialogue_label():
    # 晒成交成品不强制"AI 生成/情景演示",但 deal_chat 聊天模拟必须保留"示例对话"
    exact = geo_factory._exact_image_copy("deal_chat", {"body": "客户确认了"})
    assert exact["scene_label"] == "示例对话"
    prompt = content_center.visual_prompt(
        slot={"slot": "deal_chat_scene", "size": "9:16"}, channel="deal_chat",
        strategy={"audience": "潜在客户", "single_action": "了解服务", "single_value": "真实成交"},
        content={"title": "成交记录", "body": "示例对话"}, evidence={"facts": []},
        trend={"used": False}, contact={"mode": "none", "text": "", "qr_reference": None}, brand={},
    )
    document = json.loads(prompt.split("\n", 1)[1])
    assert document["task_type"] == "simulated_chat_scene"
    assert document["exact_visible_text"]["scene_label"] == "示例对话"
    assert "示例对话" in document["composition_and_lighting"]
    # 晒成交场景不强制 AI 生成/情景演示免责标注(聊天示意图除外,只留"示例对话")
    constraints = document["privacy_and_truth_constraints"]
    mandatory_ai_label = [
        item for item in constraints
        if "AI 生成" in item and "不强制" not in item and "无需" not in item
    ]
    assert mandatory_ai_label == []  # 不存在任何强制 AI 生成标注的约束条目
    # 正向断言:必须真有一条"明示不强制"的条目在约束里(不是约束缺失造成的假通过)
    assert any("AI 生成" in item and "不强制" in item for item in constraints)
    # 聊天示意图的"示例对话"强制标注必须仍在(双模式边界不弱化)
    assert any("示例对话" in item and "必须" in item for item in constraints)
    assert "必须显著标注‘示例对话’" in prompt
    assert "必须标注‘AI 生成’" not in prompt and "必须添加‘AI 生成’" not in prompt


def test_deal_draft_routes_are_classified_for_members():
    from services.organization_route_contract import match_member_geo_route

    route = match_member_geo_route("POST", "/api/marketing/deal-drafts")
    assert route is not None and route.capability == "materials.write"
    route = match_member_geo_route("GET", "/api/marketing/deal-drafts/31/materials/ab12cd34ef56/file")
    assert route is not None and route.capability == "materials.read_assigned"
    assert route.resource_kind == "marketing_material"


# ---------------------------------------------------------------------------
# 7. 对抗审查 round-1 修复回归
# ---------------------------------------------------------------------------
def test_analyze_ocr_failure_is_retryable_and_vision_ok_exposed(monkeypatch, tmp_path):
    """#5:OCR 失败不置 extracted(可重试),vision_ok/ocr_error 透出给属主。"""
    _stub_user(monkeypatch)
    draft, _saved = _setup_draft_store(monkeypatch, tmp_path)
    _no_llm(monkeypatch)

    async def failing_describe(_bytes, _name, before_provider_call=None):
        return {"vision_ok": False, "ocr_text": "", "risk_flags": []}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", failing_describe)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    material = _upload_material(client)

    first = client.post("/api/marketing/deal-drafts/31/analyze")
    assert first.status_code == 200, first.text  # 失败不假成功,但不阻断确认单
    public = next(m for m in first.json()["draft"]["materials"] if m["material_id"] == material["material_id"])
    assert public["extracted"] is False  # 失败不标记已提取 → re-analyze 可补
    assert public["vision_ok"] is False
    assert public["ocr_error"]
    assert draft["materials_jsonb"][0]["extracted"] is False

    async def ok_describe(_bytes, _name, before_provider_call=None):
        return {"vision_ok": True, "ocr_text": "成交金额 5万元", "risk_flags": []}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", ok_describe)
    second = client.post("/api/marketing/deal-drafts/31/analyze")
    assert second.status_code == 200, second.text
    public = next(m for m in second.json()["draft"]["materials"] if m["material_id"] == material["material_id"])
    assert public["extracted"] is True and public["vision_ok"] is True
    assert second.json()["sheet"]["deal_amount"]["value"] == "5万元"  # OCR 语料真的补上了


def test_redaction_risk_flagged_confirm_passes_with_review_warning(monkeypatch, tmp_path):
    """#6 + Owner 2026-07-22:vision 报隐私风险但自动区域为空 → confirm 放行 + warning,
    不再 422;acknowledge 路径行为不变。"""
    _stub_user(monkeypatch)
    draft, _saved = _setup_draft_store(monkeypatch, tmp_path)
    _no_llm(monkeypatch)

    async def risky_describe(_bytes, _name, before_provider_call=None):
        return {"vision_ok": True, "ocr_text": "", "risk_flags": ["privacy"]}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", risky_describe)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    material = _upload_material(client)

    auto = client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "auto",
    })
    assert auto.status_code == 200, auto.text
    state = auto.json()["redaction"]
    assert state["detection"] == "risk_flagged_manual_required"
    # 能力边界明示:头像/签名/公章三类永无自动区域,需人工
    assert set(state["manual_required_kinds"]) == {"avatar", "seal", "signature"}
    assert state["manual_reviewed"] is False

    # 原 422 redaction_manual_review_required → 放行 + warning(confirm 照过)
    confirmed = client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "confirm",
    })
    assert confirmed.status_code == 200, confirmed.text
    assert draft["materials_jsonb"][0]["redaction"]["status"] == "confirmed"
    warnings = confirmed.json()["redaction"]["warnings"]
    assert [warning["code"] for warning in warnings] == ["redaction_manual_review_suggested"]
    assert "人工复核" in warnings[0]["message"]
    assert draft["materials_jsonb"][0]["redaction"]["warnings"] == ["redaction_manual_review_suggested"]

    # acknowledge 仍可用且幂等:warning 不重复追加
    acknowledged = client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "acknowledge",
    })
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["redaction"]["manual_reviewed"] is True


def test_redaction_manual_add_counts_as_manual_review(monkeypatch, tmp_path):
    """#6 补充:手动框选本身即人工复核,add 之后 confirm 不再要求 acknowledge。"""
    _stub_user(monkeypatch)
    draft, _saved = _setup_draft_store(monkeypatch, tmp_path)
    _no_llm(monkeypatch)

    async def risky_describe(_bytes, _name, before_provider_call=None):
        return {"vision_ok": True, "ocr_text": "", "risk_flags": ["privacy"]}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", risky_describe)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    material = _upload_material(client)
    assert client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "auto",
    }).status_code == 200
    added = client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "add",
        "kind": "avatar", "box": [0, 0, 40, 40],
    })
    assert added.status_code == 200, added.text
    assert added.json()["redaction"]["manual_reviewed"] is True
    confirmed = client.post("/api/marketing/deal-drafts/31/redact", json={
        "material_id": material["material_id"], "action": "confirm",
    })
    assert confirmed.status_code == 200, confirmed.text


def test_redaction_regex_covers_colloquial_amount_phone_address():
    """#7:金额口语写法/国际码手机/放宽地址都要命中自动检测。"""
    ocr = "尾款3万已转\n到账 8w\n+86 138-0013-8000\n收货:杭州市西湖区文三路100号\n普通的寒暄"
    regions = redaction.regions_from_ocr(ocr, width=400, height=1000)
    kinds = [region["kind"] for region in regions]
    assert kinds.count("amount") == 2  # "3万" 与 "8w" 两行都命中
    assert "phone" in kinds
    assert "address" in kinds


def test_novel_number_unit_pairs_flag_discount_and_rename_fabrication():
    """#11:数字+紧邻单位配对——语料"3天"放不过编造"3折";无头衔人名对齐语料。

    Owner 2026-07-22:_novel_tokens 仍是检测层,但命中后只标注不剔除。"""
    corpus = "3天交付,成交金额 5万元,客户说响应快"
    assert deal_intake._novel_tokens("给了3折优惠", corpus)  # 3天 ≠ 3折
    assert deal_intake._novel_tokens("成交率 95%", corpus)  # 折扣/百分比同口径
    assert deal_intake._novel_tokens("张伟说很满意", corpus)  # 无头衔人名也要在语料里
    assert not deal_intake._novel_tokens("3天交付", corpus)  # 同款配对放行
    assert not deal_intake._novel_tokens("成交金额 5万元", corpus)

    async def fake_llm(_prompt, verbose=False, before_provider_call=None):
        return json.dumps({"service_content": "GEO 诊断,给了3折优惠"}, ensure_ascii=False)

    import tools.multi_llm_caller as llm_module

    monkeypatch_llm = fake_llm
    original = llm_module.call_llm_with_fallback
    llm_module.call_llm_with_fallback = monkeypatch_llm
    try:
        sheet = asyncio.run(deal_intake.extract_sheet(
            form={}, voice_transcript="3天交付,GEO 诊断",
        ))
    finally:
        llm_module.call_llm_with_fallback = original
    assert "3折" in sheet["service_content"]["value"]  # 保留值,不剔除
    assert sheet["service_content"]["status"] == "tentative"
    warnings = sheet["_meta"]["warnings"]
    assert [warning["field"] for warning in warnings] == ["service_content"]
    assert warnings[0]["code"] == "ai_extraction_beyond_materials"


def test_retry_refreezes_deal_snapshot_with_latest_redaction(monkeypatch):
    """#2:打码补完后局部重试必须重新冻结 deal 快照(其余冻结字段不漂移)。"""
    _stub_user(monkeypatch)
    geo = {
        "request_id": "showcase-deal-001", "request_hash": "a" * 64,
        "channels": ["deal_poster", "deal_chat"], "moments_layout": "single",
        "strategy": {"audience": "潜在客户"},
        "evidence": {"source_type": "brand_facts", "facts": []},
        "contact": {"mode": "none", "text": ""},
        "teacher": {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
        "deal": {"draft_id": 31, "redacted_materials": [],
                 "skipped_components": [{"component_id": "deal_chat:image:deal_chat_scene", "reason": "先打码"}]},
    }
    job = {
        "id": 5, "user_id": 9, "status": "succeeded", "error_summary": "partial_free_release",
        "brand_id": None, "resolution": "1k", "input_fields_jsonb": {"_geo": geo},
    }
    monkeypatch.setattr(marketing_db, "get_job", lambda _id: dict(job))
    draft = _draft_row()
    draft["materials_jsonb"] = [{
        "material_id": "m1", "storage_key": "private://x",
        "redaction": {"status": "confirmed", "output_key": "private://out", "output_sha256": "s" * 64},
    }]
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: draft)
    captured = {}

    async def prepare(**kwargs):
        captured["geo_snapshot"] = kwargs["geo_snapshot"]
        return {"status": "generating", "job_id": 6}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    response = client.post("/api/marketing/jobs/5/retry", json={
        "request_id": "retry-deal-chat-01", "component_ids": ["deal_chat:image:deal_chat_scene"],
    })
    assert response.status_code == 200, response.text
    retry_geo = captured["geo_snapshot"]
    # deal 快照重冻:最新打码确认状态进来了
    assert retry_geo["deal"]["redacted_materials"] == [
        {"material_id": "m1", "ref": "private://out", "sha256": "s" * 64},
    ]
    assert retry_geo["deal"]["skipped_components"] == []  # 重新评估,不继承旧跳过
    # 其余冻结字段不漂移
    assert retry_geo["strategy"] == geo["strategy"]
    assert retry_geo["evidence"] == geo["evidence"]
    assert retry_geo["contact"] == geo["contact"]
    assert retry_geo["parent_job_id"] == 5
    assert retry_geo["only_components"] == ["deal_chat:image:deal_chat_scene"]


def test_anonymize_brand_masks_names_and_strips_brand_assets(monkeypatch):
    """#4:匿名晒单——确认单客户名服务端掩码 + provider 品牌置空 + prompt 约束。"""
    sheet = deal_intake.empty_sheet()
    sheet["what_happened"] = {"value": "王总签单", "status": "confirmed", "provenance": "customer_asserted"}
    sheet["customer_praise"] = {"value": "王总说星图装修公司靠谱", "status": "confirmed", "provenance": "customer_asserted"}
    strategy = deal_intake.strategy_from_sheet(sheet, anonymize=True)
    facts = deal_intake.facts_from_sheet(sheet, anonymize=True)
    assert "王总" not in json.dumps(strategy, ensure_ascii=False)
    assert "王总" not in json.dumps(facts, ensure_ascii=False)
    assert "星图装修公司" not in json.dumps(strategy, ensure_ascii=False)
    # 非匿名路径原样保留(掩码不得影响默认晒单)
    open_facts = deal_intake.facts_from_sheet(sheet)
    assert "王总" in json.dumps(open_facts, ensure_ascii=False)

    brand = {"name": "OmniRank", "logo_url": "https://cdn.example/logo.png", "brand_color": "#000"}
    safe = content_center.provider_safe_brand(brand, anonymize=True)
    assert safe["name"] == "" and safe["logo_url"] == ""
    prompt = content_center.visual_prompt(
        slot={"slot": "deal_poster", "size": "3:4"}, channel="deal_poster",
        strategy=strategy, content={"title": "成交喜报"}, evidence={"facts": facts},
        trend={"used": False}, contact={"mode": "none", "text": "", "qr_reference": None},
        brand=brand, anonymize_brand=True,
    )
    document = json.loads(prompt.split("\n", 1)[1])
    assert document["brand_assets"]["name"] == ""  # visual_qa 的 brand_name_missing 自然跳过
    assert any("品牌名" in item and "客户名" in item for item in document["privacy_and_truth_constraints"])

    # 冻结进 geo_snapshot
    draft = _draft_row()
    draft["sheet_jsonb"] = sheet
    client, captured = _stub_package_prepare(monkeypatch, draft)
    response = client.post("/api/marketing/content-packages", json=_showcase_payload(
        anonymize_brand=True,
    ))
    assert response.status_code == 200, response.text
    snapshot = captured["geo_snapshot"]
    assert snapshot["anonymize_brand"] is True
    assert "王总" not in json.dumps(snapshot["strategy"], ensure_ascii=False)


def test_settle_retry_with_zero_new_delivery_fails_and_releases(monkeypatch):
    """#10(v1 存量语义修正):重试零新交付且全败 → failed+释放计费,不按血缘合并假成功。"""
    import middleware.billing as billing

    releases = []

    async def fake_release(**kwargs):
        releases.append(kwargs)
        return {"success": True}

    async def fake_commit(**kwargs):
        raise AssertionError("零新交付不得 commit 扣费")

    monkeypatch.setattr(billing, "release_freeze", fake_release)
    monkeypatch.setattr(billing, "commit_freeze", fake_commit)
    monkeypatch.setattr(marketing_db, "patch_job_input_fields", lambda _id, _patch: None)
    job_updates = []
    monkeypatch.setattr(marketing_db, "update_job", lambda _id, **fields: job_updates.append(fields))
    retry_job = {
        "id": 6, "user_id": 9,
        "input_fields_jsonb": {"_geo": {"parent_job_id": 5, "only_components": ["deal_poster:copy"]}},
    }
    inherited = {"id": 41, "job_id": 5, "bundle_slot": "deal_poster:copy"}  # 血缘继承,非本 job 新交付
    ctx = {"billing_kind": "legacy", "freeze_id": "fz-1", "payer_user_id": 9}
    result = asyncio.run(geo_factory._settle_geo(
        retry_job, ctx, [inherited], 1, external_started=True,
    ))
    assert result["status"] == "failed"
    assert job_updates[-1]["status"] == "failed"
    assert job_updates[-1]["error_summary"] == "all_components_failed"
    assert releases  # 计费释放,不扣费

    # 对照:本 job 有新交付 → full 结算口径不变
    commits = []

    async def ok_commit(**kwargs):
        commits.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(billing, "commit_freeze", ok_commit)
    own = {"id": 42, "job_id": 6, "bundle_slot": "deal_poster:copy"}
    result = asyncio.run(geo_factory._settle_geo(
        retry_job, ctx, [own], 1, external_started=True,
    ))
    assert result["status"] == "succeeded" and commits


def test_deal_component_skip_is_durable_on_first_skip(monkeypatch):
    """#12:首次跳过即落库;恢复重放不重复记 geo_deal_component_skipped 审计事件。"""
    events = []
    monkeypatch.setattr(marketing_db, "add_event", lambda **kwargs: events.append(kwargs))
    patched = []
    state_geo = {
        "request_id": "showcase-deal-skip", "request_hash": "b" * 64,
        "channels": ["deal_chat"], "moments_layout": "single",
        "strategy": {"audience": "潜在客户"},
        "evidence": {"source_type": "none", "facts": []},
        "contact": {"mode": "none", "text": ""},
        "teacher": {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
        "trend": {"used": False},
        "deal": {"draft_id": 31, "redacted_materials": []},
    }
    live = {"geo": state_geo}
    job = {
        "id": 7, "user_id": 9, "status": "generating", "error_summary": "",
        "brand_id": None, "resolution": "1k",
    }

    def get_job(_id):
        return {**job, "input_fields_jsonb": {"_geo": live["geo"]}}

    monkeypatch.setattr(marketing_db, "get_job", get_job)
    monkeypatch.setattr(marketing_db, "list_assets", lambda _id: [])
    monkeypatch.setattr(marketing_db, "list_attempts", lambda *a, **k: [])
    monkeypatch.setattr(marketing_db, "update_job", lambda *a, **k: None)

    def patch_fields(_id, patch):
        patched.append(patch)
        live["geo"] = patch["_geo"]  # durable:重放读到的是已落库快照

    monkeypatch.setattr(marketing_db, "patch_job_input_fields", patch_fields)
    added_assets = []
    monkeypatch.setattr(marketing_db, "add_asset", lambda **kwargs: added_assets.append(kwargs) or {
        "id": 90 + len(added_assets), "job_id": 7, "bundle_slot": kwargs["bundle_slot"],
    })

    async def content(*_args, **_kwargs):
        return ({"title": "成交记录", "body": "示例对话"}, {"passed": True, "errors": [], "warnings": []})

    monkeypatch.setattr(geo_factory, "generate_channel_content", content)
    monkeypatch.setattr(geo_factory, "claim_evidence_qa", lambda *_a, **_k: {"passed": True, "errors": [], "warnings": []})

    import middleware.billing as billing

    async def fake_release(**kwargs):
        return {"success": True}

    monkeypatch.setattr(billing, "release_freeze", fake_release)

    class _LockCursor:
        def execute(self, *_a, **_k):
            pass

        def fetchone(self):
            return {"acquired": True}

    class _LockConn:
        def cursor(self):
            return _LockCursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(db.connection, "get_connection", lambda: _LockConn())
    ctx = {"job_id": 7, "billing_kind": "legacy", "freeze_id": "fz-1", "payer_user_id": 9}
    asyncio.run(geo_factory.execute_geo_package_job(dict(ctx)))
    asyncio.run(geo_factory.execute_geo_package_job(dict(ctx)))  # 恢复重放第二次
    skipped_events = [e for e in events if e.get("event_type") == "geo_deal_component_skipped"]
    assert len(skipped_events) == 1  # 重复执行不重复记账
    assert any("_geo" in p for p in patched)  # 首次跳过即落库 durable


def test_deal_image_component_fails_when_all_pads_unreadable(monkeypatch):
    """#14:deal 渠道垫图全部加载失败 → 组件失败+人话原因,不静默裸生成。"""
    from services.marketing import redaction as deal_redaction

    def unreadable(_ref, *, expected_sha256=""):
        raise ValueError("redaction_image_hash_mismatch")

    monkeypatch.setattr(deal_redaction, "redacted_data_uri", unreadable)
    monkeypatch.setattr(marketing_db, "list_attempts", lambda *a, **k: [])
    monkeypatch.setattr(marketing_db, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(marketing_db, "add_attempt", lambda **kwargs: {"id": 77, **kwargs})
    finished = []
    monkeypatch.setattr(marketing_db, "finish_attempt", lambda _id, **kwargs: finished.append(kwargs) or {"id": _id})
    events = []
    monkeypatch.setattr(marketing_db, "add_event", lambda **kwargs: events.append(kwargs))

    async def forbidden_submit(*_a, **_k):
        raise AssertionError("垫图全灭不得发起裸生成")

    from services.marketing import image_client

    monkeypatch.setattr(image_client, "submit_image", forbidden_submit)
    geo = {
        "strategy": {"audience": "潜在客户"},
        "evidence": {"source_type": "brand_facts", "facts": []},
        "trend": {"used": False},
        "contact": {"mode": "none", "text": ""},
        "brand": {"name": "OmniRank"},
        "deal": {"draft_id": 31, "redacted_materials": [
            {"material_id": "m1", "ref": "private://gone", "sha256": "s" * 64},
        ]},
    }
    component = {
        "component_id": "deal_chat:image:deal_chat_scene", "kind": "image",
        "channel": "deal_chat", "slot": {"slot": "deal_chat_scene", "size": "9:16"},
    }
    result = asyncio.run(geo_factory._generate_image_component(
        job={"id": 8, "user_id": 9}, component=component, geo=geo,
        content={"title": "成交记录", "body": "示例对话"}, owner_key="u9",
    ))
    assert result is None  # 组件失败,不交付
    assert finished and "deal_redacted_material_unreadable" in finished[0]["error_detail"]
    assert "打码" in finished[0]["error_detail"] and "重新" in finished[0]["error_detail"]  # 人话原因
    assert events[0]["event_type"] == "geo_deal_component_failed"


# ---------------------------------------------------------------------------
# 10. 二轮点修:N1 匿名品牌名反向检测 / N2 掩码扩面 / N3 sheet 乐观锁 /
#     N4 上传孤儿清理 / N6 中文数字层
# ---------------------------------------------------------------------------
def test_visual_qa_anonymize_brand_name_leak_is_warning_not_block(monkeypatch):
    """N1 + Owner 2026-07-22:匿名晒单——冻结品牌名出现在成图 OCR → warning 提醒,不再硬失败。"""
    from services.marketing.quality_assurance import visual_qa

    async def vision_leak(*_args, **_kwargs):
        return {"vision_ok": True, "ocr_text": "本海报由 OmniRank 提供服务", "risk_flags": []}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", vision_leak)
    qa = asyncio.run(visual_qa(
        _png_bytes(), size="2:3", exact_copy={}, brand={}, evidence={},
        contact={"mode": "none"}, forbidden_brand_name="OmniRank",
    ))
    assert qa["passed"] is True  # 提醒不阻断
    assert qa["errors"] == []
    assert {warning["code"] for warning in qa["warnings"]} == {"brand_name_forbidden_when_anonymous"}
    assert qa["warnings"][0]["message"]  # 人话 message

    # 大小写不敏感:_compact_text 同口径(与 brand_name_missing 同数据源)
    async def vision_upper(*_args, **_kwargs):
        return {"vision_ok": True, "ocr_text": "OMNIRANK 出品", "risk_flags": []}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", vision_upper)
    qa = asyncio.run(visual_qa(
        _png_bytes(), size="2:3", exact_copy={}, brand={}, evidence={},
        contact={"mode": "none"}, forbidden_brand_name="omnirank",
    ))
    assert {warning["code"] for warning in qa["warnings"]} == {"brand_name_forbidden_when_anonymous"}

    # OCR 干净 / 非匿名(不传 forbidden)→ 不新增 warning
    async def vision_clean(*_args, **_kwargs):
        return {"vision_ok": True, "ocr_text": "真实成交喜报", "risk_flags": []}

    monkeypatch.setattr(tools.vision.image_describe, "describe_image_structured", vision_clean)
    qa = asyncio.run(visual_qa(
        _png_bytes(), size="2:3", exact_copy={}, brand={}, evidence={},
        contact={"mode": "none"}, forbidden_brand_name="OmniRank",
    ))
    assert qa["passed"] is True and qa["warnings"] == []
    qa = asyncio.run(visual_qa(
        _png_bytes(), size="2:3", exact_copy={}, brand={}, evidence={},
        contact={"mode": "none"},
    ))
    assert qa["passed"] is True


def test_anonymize_image_qa_wiring_warns_and_materializes_with_frozen_brand(monkeypatch):
    """N1 接线 + Owner 2026-07-22:anonymize → visual_qa 收到冻结品牌名;
    品牌名泄漏是 warning → 组件成功交付,warnings 落 safety_flags 与 job 快照。"""
    from services.marketing import image_client, material_storage

    geo = {
        "anonymize_brand": True,
        "brand": {"name": "OmniRank", "logo_url": "", "product_name": "", "slogan": ""},
        "strategy": {}, "evidence": {}, "trend": {}, "contact": {"mode": "none"},
    }
    job = {"id": 5, "user_id": 9, "resolution": "1k"}
    component = {
        "component_id": "professional_poster:image:professional_poster",
        "channel": "professional_poster", "kind": "image",
        "slot": {"slot": "professional_poster", "size": "3:4"},
    }
    attempt_row = {
        "id": 41, "attempt_no": 1, "status": "pending", "submit_state": "not_started",
        "poll_state": "", "provider_task_id": "", "provider_result_jsonb": {},
    }
    monkeypatch.setattr(marketing_db, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(marketing_db, "list_attempts", lambda *a, **k: [])
    monkeypatch.setattr(marketing_db, "add_attempt", lambda **k: dict(attempt_row))
    monkeypatch.setattr(marketing_db, "anchor_attempt_before_submit", lambda *a, **k: dict(attempt_row))

    def update_attempt(_id, **kwargs):
        if "provider_result" in kwargs:
            kwargs["provider_result_jsonb"] = kwargs.pop("provider_result")
        attempt_row.update(kwargs)
        return dict(attempt_row)

    monkeypatch.setattr(marketing_db, "update_attempt_provider_state", update_attempt)
    finished = []
    monkeypatch.setattr(marketing_db, "finish_attempt", lambda _id, **kwargs: finished.append(kwargs))
    materialized = []
    monkeypatch.setattr(
        marketing_db, "materialize_attempt_asset",
        lambda **kwargs: materialized.append(kwargs) or {"id": 99, **kwargs},
    )
    patched = []
    monkeypatch.setattr(
        marketing_db, "patch_job_input_fields",
        lambda _id, patch: patched.append(patch),
    )
    monkeypatch.setattr(
        material_storage, "save_material_image",
        lambda *_a, **_k: {
            "public_url": "private://marketing-materials/u9/poster.png",
            "thumbnail_url": "", "width": 768, "height": 1024,
            "size_bytes": 100, "sha256": "s" * 64,
        },
    )

    async def submit(*_a, **_k):
        return {"state": "submitted", "provider_task_id": "t-1"}

    async def poll(*_a, **_k):
        return {"state": "succeeded", "image_url": "https://cdn.example/x.png"}

    monkeypatch.setattr(image_client, "submit_image", submit)
    monkeypatch.setattr(image_client, "poll_image_task", poll)
    monkeypatch.setattr(image_client, "download_image", lambda *_a, **_k: asyncio.sleep(0, result=_png_bytes()))
    qa_kwargs = []

    async def qa_capture(*_a, **kwargs):
        qa_kwargs.append(kwargs)
        return {
            "passed": True, "errors": [],
            "warnings": [{"code": "brand_name_forbidden_when_anonymous", "message": "匿名晒单的成图里疑似出现了品牌名，请检查后再发布"}],
        }

    monkeypatch.setattr(geo_factory, "visual_qa", qa_capture)
    result = asyncio.run(geo_factory._generate_image_component(
        job=job, component=component, geo=geo,
        content={"title": "成交喜报", "body": "真实交付"}, owner_key="u9",
    ))
    assert result is not None  # warning 不阻断:组件成功交付
    assert finished == []  # 不再有 finish_attempt(failed)
    assert qa_kwargs and all(
        kwargs.get("forbidden_brand_name") == "OmniRank" for kwargs in qa_kwargs
    )  # 冻结快照品牌名传入反向检测(provider 侧已置空,传的是 geo["brand"])
    assert len(materialized) == 1
    assert materialized[0]["safety_status"] == "passed"
    assert materialized[0]["safety_flags"] == [
        {"code": "brand_name_forbidden_when_anonymous", "message": "匿名晒单的成图里疑似出现了品牌名，请检查后再发布"},
    ]
    # warnings 落库进 geo 快照(job_public_state 透出)
    assert patched and patched[0]["_geo"]["warnings"] == [{
        "code": "brand_name_forbidden_when_anonymous",
        "message": "匿名晒单的成图里疑似出现了品牌名，请检查后再发布",
        # SSOT 2026-07-23 五问合同:落库条目带五字段(此处桩 warning 未带,缺省为空)
        "reason": "", "repair_hint": "", "actions": [], "rule_version": "",
        "channel": "professional_poster",
        "component_id": "professional_poster:image:professional_poster",
    }]

    # 非匿名:不启用反向检测
    qa_kwargs.clear()
    asyncio.run(geo_factory._generate_image_component(
        job=job, component=component, geo={**geo, "anonymize_brand": False},
        content={"title": "成交喜报", "body": "真实交付"}, owner_key="u9",
    ))
    assert qa_kwargs and all(kwargs.get("forbidden_brand_name") == "" for kwargs in qa_kwargs)


def test_anonymize_mask_covers_brand_substring_and_bare_cjk_names(monkeypatch):
    """N2:掩码扩面——冻结品牌名(ASCII 大小写不敏感)+ 无头衔 CJK 人名语境掩码。"""
    masked = deal_intake.mask_customer_names("张伟说很好用,和OmniRank签了年单", extra_terms=["OmniRank"])
    assert masked == "客户说很好用,和服务商签了年单"
    assert deal_intake.mask_customer_names("omnirank 响应快", extra_terms=["OmniRank"]) == "服务商 响应快"
    # 残余边界(注释明示不掩):无后缀公司名原样保留
    assert deal_intake.mask_customer_names("和阿里签的合同", extra_terms=["OmniRank"]) == "和阿里签的合同"

    sheet = deal_intake.empty_sheet()
    sheet["customer_praise"] = {
        "value": "张伟说 OmniRank 响应快", "status": "confirmed", "provenance": "customer_asserted",
    }
    facts = deal_intake.facts_from_sheet(sheet, anonymize=True, mask_terms=["OmniRank"])
    serialized = json.dumps(facts, ensure_ascii=False)
    assert "张伟" not in serialized and "omnirank" not in serialized.lower()
    strategy = deal_intake.strategy_from_sheet(sheet, anonymize=True, mask_terms=["OmniRank"])
    serialized_strategy = json.dumps(strategy, ensure_ascii=False)
    assert "张伟" not in serialized_strategy and "omnirank" not in serialized_strategy.lower()

    # API 接线:匿名晒单冻结进 snapshot 的 strategy/facts 已掩掉冻结品牌名+无头衔人名
    draft = _draft_row()
    draft["sheet_jsonb"] = sheet
    client, captured = _stub_package_prepare(monkeypatch, draft)
    response = client.post("/api/marketing/content-packages", json=_showcase_payload(anonymize_brand=True))
    assert response.status_code == 200, response.text
    snapshot = captured["geo_snapshot"]
    frozen = json.dumps(
        {"strategy": snapshot["strategy"], "facts": snapshot["evidence"]["facts"]},
        ensure_ascii=False,
    )
    assert "张伟" not in frozen and "omnirank" not in frozen.lower()


def test_sheet_patch_uses_optimistic_lock_with_single_replay(monkeypatch):
    """N3:PATCH sheet 带 expected_updated_at;冲突重取重放一次,双冲突 409。"""
    _stub_user(monkeypatch)
    draft = _draft_row()
    # 读取三次:端点属主预检一次 + 乐观锁循环两轮各重取一次
    versions = iter([
        {**draft, "updated_at": "t0"},
        {**draft, "updated_at": "t1"},
        {**draft, "updated_at": "t2"},
    ])
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: dict(next(versions)))
    calls = []

    def update(_id, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ValueError("deal_draft_update_conflict")
        return dict(draft)

    monkeypatch.setattr(marketing_db, "update_deal_draft", update)
    monkeypatch.setattr(marketing_db, "add_event", lambda **kwargs: None)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    response = client.patch("/api/marketing/deal-drafts/31", json={"sheet": {"deal_amount": "5万元"}})
    assert response.status_code == 200, response.text
    assert [call.get("expected_updated_at") for call in calls] == ["t1", "t2"]  # 重取后重放一次
    assert calls[0]["sheet"]["deal_amount"]["status"] == "confirmed"  # 编辑即确认语义不变

    # 双冲突 → 409(有界 (1,2),不死循环)
    monkeypatch.setattr(marketing_db, "get_deal_draft", lambda _id: dict(draft))
    calls.clear()

    def always_conflict(_id, **kwargs):
        calls.append(kwargs)
        raise ValueError("deal_draft_update_conflict")

    monkeypatch.setattr(marketing_db, "update_deal_draft", always_conflict)
    conflict = client.patch("/api/marketing/deal-drafts/31", json={"sheet": {"deal_amount": "6万元"}})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "deal_draft_update_conflict"
    assert len(calls) == 2
    assert all("expected_updated_at" in call for call in calls)


def test_voice_upload_uses_optimistic_lock_and_409_on_double_conflict(monkeypatch, tmp_path):
    """N3:voice 端点同传 expected_updated_at;双冲突 409。"""
    _stub_user(monkeypatch, user_id=9)
    draft, saved = _setup_draft_store(monkeypatch, tmp_path)

    async def ok_asr(_bytes, *, suffix=".webm"):
        return {"text": "上周签了 5万元的单子", "status": "success", "provider": "fake"}

    monkeypatch.setattr(deal_intake, "transcribe_voice_bytes", ok_asr)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    ok = client.post(
        "/api/marketing/deal-drafts/31/voice",
        files={"file": ("v.wav", b"RIFF" + b"\x00" * 2000, "audio/wav")},
    )
    assert ok.status_code == 200, ok.text
    assert "expected_updated_at" in saved  # 乐观锁参数真的带上了
    assert draft["form_jsonb"]["voice_transcript"].startswith("上周")

    def always_conflict(_id, **kwargs):
        raise ValueError("deal_draft_update_conflict")

    monkeypatch.setattr(marketing_db, "update_deal_draft", always_conflict)
    conflict = client.post(
        "/api/marketing/deal-drafts/31/voice",
        files={"file": ("v.wav", b"RIFF" + b"\x00" * 2000, "audio/wav")})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "deal_draft_update_conflict"


def test_analyze_preserves_user_confirmed_sheet_fields(monkeypatch, tmp_path):
    """N3:analyze 重写 sheet 时,用户已 confirmed 字段不被重提取覆盖。"""
    _stub_user(monkeypatch)
    draft, _saved = _setup_draft_store(monkeypatch, tmp_path)
    _no_llm(monkeypatch)
    draft["form_jsonb"] = {"voice_transcript": "成交金额 8万元,上周签约"}
    draft["sheet_jsonb"] = {
        "deal_amount": {"value": "5万元", "status": "confirmed", "provenance": "customer_asserted"},
    }
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    response = client.post("/api/marketing/deal-drafts/31/analyze")
    assert response.status_code == 200, response.text
    sheet = response.json()["sheet"]
    assert sheet["deal_amount"]["value"] == "5万元"  # 已确认值不被语料 "8万元" 覆盖
    assert sheet["deal_amount"]["status"] == "confirmed"
    assert sheet["what_happened"]["value"]  # 未确认字段照常刷新

    # 纯函数口径:confirmed 保留 / tentative 刷新 / _meta(含 warnings)透传
    merged = deal_intake.merge_sheet_preserving_confirmed(
        {"deal_amount": {"value": "5万元", "status": "confirmed"}},
        {
            "deal_amount": {"value": "8万元", "status": "tentative"},
            "what_happened": {"value": "签约", "status": "tentative"},
            "_meta": {"extraction": "ai", "warnings": []},
        },
    )
    assert merged["deal_amount"]["value"] == "5万元"
    assert merged["what_happened"]["value"] == "签约"
    assert merged["_meta"] == {"extraction": "ai", "warnings": []}


def test_upload_orphan_files_cleaned_on_total_exceeded_and_double_conflict(monkeypatch, tmp_path):
    """N4:422(materials_total_exceeded)/双冲突 409 路径清理已落盘孤儿文件。"""
    _stub_user(monkeypatch)
    draft, _saved = _setup_draft_store(monkeypatch, tmp_path)
    draft["materials_jsonb"] = [
        {"material_id": f"m{index}", "storage_key": f"private://old/{index}"}
        for index in range(marketing_material_api._DEAL_MATERIAL_TOTAL_MAX)
    ]
    from services.marketing import material_storage

    deleted = []
    monkeypatch.setattr(
        material_storage, "delete_material_reference",
        lambda key: deleted.append(key) or True,
    )
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    client = TestClient(app)
    exceeded = client.post(
        "/api/marketing/deal-drafts/31/materials",
        files=[("files", ("chat.png", _png_bytes(), "image/png"))],
    )
    assert exceeded.status_code == 422
    assert exceeded.json()["detail"] == "materials_total_exceeded"
    assert len(deleted) == 1  # 本批已落盘文件收到清理
    assert deleted[0].startswith("private://marketing-materials/")

    # 双冲突 409:两次 update 都撞锁,added 文件同样清理(只清一次,非每轮重试)
    draft["materials_jsonb"] = []
    deleted.clear()

    def always_conflict(_id, **kwargs):
        raise ValueError("deal_draft_update_conflict")

    monkeypatch.setattr(marketing_db, "update_deal_draft", always_conflict)
    conflict = client.post(
        "/api/marketing/deal-drafts/31/materials",
        files=[("files", ("chat.png", _png_bytes(), "image/png"))],
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "deal_draft_update_conflict"
    assert len(deleted) == 1


def test_delete_material_reference_removes_file_and_rejects_foreign_refs(monkeypatch, tmp_path):
    """N4 单元:delete_material_reference 真删主文件+缩略图;外部引用拒删。"""
    from services.marketing import material_storage

    monkeypatch.setenv("MARKETING_PRIVATE_UPLOAD_ROOT", str(tmp_path / "marketing-private"))
    stored = material_storage.save_material_image("u9", _png_bytes(), "chat.png", private=True)
    main_path = Path(stored["storage_key"])
    thumb_path = Path(stored["thumbnail_key"]) if stored.get("thumbnail_key") else None
    assert main_path.is_file()
    assert material_storage.delete_material_reference(stored["public_url"]) is True
    assert not main_path.exists()
    if thumb_path:
        assert not thumb_path.exists()  # 同源缩略图一并清理
    # 外部/越界/缺失引用:不删、不抛
    assert material_storage.delete_material_reference("https://evil.example/x.png") is False
    assert material_storage.delete_material_reference("") is False
    assert material_storage.delete_material_reference(
        "private://marketing-materials/u9/../etc/passwd",
    ) is False
    assert material_storage.delete_material_reference(stored["public_url"]) is False  # 已删


def test_cjk_numerals_covered_in_amount_redaction_and_pairing():
    """N6:中文数字简单映射——"五千块" 打码/金额层命中,"三折" 配对层拦截。"""
    # 打码层:无"金额"关键词的纯中文数字行也命中;"万一" 不误判
    regions = redaction.regions_from_ocr("尾款五千块已转\n万一出了问题再谈", width=400, height=1000)
    amount_lines = [region for region in regions if region["kind"] == "amount"]
    assert len(amount_lines) == 1

    # 金额层:canonical 对齐("五千块" == "5千块" == 5000),日常词不算金额
    assert deal_intake._canonical_amounts("成交金额五千块") == {5000.0}
    assert deal_intake._canonical_amounts("成交金额5千块") == {5000.0}
    assert deal_intake._canonical_amounts("万一出了问题") == set()
    sheet = deal_intake.merge_rule_based(form={}, voice_transcript="成交金额五千块")
    assert sheet["deal_amount"]["value"] == "五千块"

    # 配对层:同款放行(中文数字与阿拉伯同款对齐)/编造拦截
    assert deal_intake._novel_tokens("给了三折优惠", "3天交付")  # 3天 ≠ 三折
    assert not deal_intake._novel_tokens("给了三折优惠", "3折大促")  # 同款配对放行
    assert not deal_intake._novel_tokens("成交金额五千块", "成交金额五千块")


def test_cjk_numeral_fabrication_annotated_end_to_end():
    """N6 端到端 + Owner 2026-07-22:LLM 编造中文数字折扣,语料无同款配对 →
    保留值 + tentative + warning(不再剔除回落)。"""
    async def fake_llm(_prompt, verbose=False, before_provider_call=None):
        return json.dumps({"service_content": "GEO 诊断,给了三折优惠"}, ensure_ascii=False)

    original = tools.multi_llm_caller.call_llm_with_fallback
    tools.multi_llm_caller.call_llm_with_fallback = fake_llm
    try:
        sheet = asyncio.run(deal_intake.extract_sheet(
            form={}, voice_transcript="3天交付,GEO 诊断",
        ))
    finally:
        tools.multi_llm_caller.call_llm_with_fallback = original
    assert "三折" in sheet["service_content"]["value"]  # 保留,不剔除
    assert sheet["service_content"]["status"] == "tentative"
    warnings = sheet["_meta"]["warnings"]
    assert [warning["field"] for warning in warnings] == ["service_content"]
    assert "超出了你提供的材料" in warnings[0]["message"]
