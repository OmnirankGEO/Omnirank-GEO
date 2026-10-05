from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathlib import Path
import asyncio
import pytest

import services.contact_placeholder as contact


CONTACT = {
    "phone": "13900000000",
    "wechat": "kzkwood-service",
    "website": "https://www.kzkwood.com",
    "address": "广东省深圳市龙岗区南湾街道",
    "brand_name": "客户品牌",
}

LENGTH_PLAN = {
    "version": "geo-article-length-v1.0",
    "minimum_chars": 3000,
    "target_chars": 4500,
    "maximum_chars": 8000,
    "evidence_limited": True,
}


def test_contact_consent_is_wired_through_prompt_save_rewrite_preview_and_dispatch():
    generator = Path("writing/article_generator_service.py").read_text(encoding="utf-8")
    lineage = Path("writing/article_lineage.py").read_text(encoding="utf-8")
    preview = Path("api/image_asset_api.py").read_text(encoding="utf-8")
    awaiting = Path("api/meijiehezi_api.py").read_text(encoding="utf-8")
    dispatch = Path("services/article_publish_dispatch.py").read_text(encoding="utf-8")
    frontend = Path("frontend/src/pages/Writing/WritingHall.tsx").read_text(encoding="utf-8")
    server = Path("server.py").read_text(encoding="utf-8")

    assert 'system_prompt = system_prompt + "\\n\\n" + CONTACT_OPT_OUT_PROMPT' in generator
    assert generator.count("apply_generation_contact_consent(") >= 3
    assert '"requested_add_contact"' in lineage and '"effective_add_contact"' in lineage
    assert 'a.generation_request_snapshot, q.brand_id' in preview
    assert "_fetch_article_preview_context(article_id)" in awaiting
    assert "_load_contact_consent_context(article_id)" in dispatch
    assert "contentRendered: undefined" in frontend
    assert "contactConsent: rd.contact_consent" in frontend
    assert "canonicalContent = data?.article?.content" in frontend
    assert '"content": _updated_content' in server


def test_persisted_dispatch_blocks_when_contact_consent_cannot_be_read(monkeypatch):
    import services.article_publish_dispatch as dispatch
    from services.article_review_gate import ArticlePublicationBlocked

    monkeypatch.setattr(
        dispatch,
        "_load_contact_consent_context",
        lambda article_id: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(ArticlePublicationBlocked) as exc_info:
        dispatch.prepare_article_dispatch_snapshot(
            article_id=301,
            source_title="持久化文章",
            source_content="正文内容",
            outgoing_title="持久化文章",
            outgoing_content="正文内容",
            source="contact_consent_failure_test",
        )

    assert exc_info.value.payload["reason"] == "contact_consent_unavailable"


def test_opt_out_removes_contact_block_but_preserves_same_website_as_evidence(monkeypatch):
    monkeypatch.setattr(contact, "_get_contact", lambda brand_id: CONTACT)
    source_url = CONTACT["website"]
    content = f"""## 参考来源
- 来源：{source_url}

## 联系我们
- 电话：{CONTACT['phone']}
- 官网：{source_url}
- 地址：{CONTACT['address']}
"""
    cleaned = contact.enforce_contact_opt_out(content, brand_id=7)
    assert f"来源：{source_url}" in cleaned
    assert "联系我们" not in cleaned
    assert "官网：" not in cleaned
    assert CONTACT["phone"] not in cleaned
    assert CONTACT["address"] not in cleaned


def test_opt_out_preserves_website_labeled_as_reference_but_removes_contact_entry(monkeypatch):
    monkeypatch.setattr(contact, "_get_contact", lambda brand_id: CONTACT)
    source_url = CONTACT["website"]
    content = f"""## 参考来源
- 官网：{source_url}

## 联系我们
- 官网：{source_url}
"""
    cleaned = contact.enforce_contact_opt_out(content, brand_id=7)
    assert "## 参考来源" in cleaned
    assert f"- 官网：{source_url}" in cleaned
    assert "## 联系我们" not in cleaned
    assert cleaned.count(source_url) == 1


def test_opt_out_removes_unlabelled_exact_website_and_address_but_keeps_evidence_url(monkeypatch):
    monkeypatch.setattr(contact, "_get_contact", lambda brand_id: CONTACT)
    source_url = CONTACT["website"]
    content = f"""更多信息请访问 {source_url}/contact 或 kzkwood.com/about，办公地点位于{CONTACT['address']}。

参考来源：{source_url}/reports/material-enf.pdf?source=archive
"""

    cleaned = contact.enforce_contact_opt_out(content, brand_id=7)

    assert f"{source_url}/contact" not in cleaned
    assert "kzkwood.com/about" not in cleaned
    assert CONTACT["address"] not in cleaned
    assert f"{source_url}/reports/material-enf.pdf?source=archive" in cleaned


def test_opt_in_never_trusts_model_written_contact_and_keeps_evidence_url(monkeypatch):
    monkeypatch.setattr(contact, "_get_contact", lambda brand_id: CONTACT)
    source_url = CONTACT["website"]
    content = f"""参考来源：{source_url}

**联系我们**
- 电话：000-00000000
- 官网：{source_url}
"""
    normalized = contact.apply_generation_contact_consent(content, brand_id=7, enabled=True)
    assert f"参考来源：{source_url}" in normalized
    assert "000-00000000" not in normalized
    assert normalized.count("[CLIENT_CONTACT]") == 1


class _Cursor:
    def __init__(self, row):
        self.row = row

    def execute(self, *args, **kwargs):
        return None

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return _Cursor(self.row)

    def close(self):
        return None


def test_real_preview_route_falls_back_without_leaking_when_primary_scrub_raises(monkeypatch):
    from api import image_asset_api
    import db.connection as connection

    source_url = CONTACT["website"]
    row = {
        "content": (
            f"参考来源：{source_url}\n\n**联系我们**\n\n"
            f"- 电话：{CONTACT['phone']}\n- 官网：{source_url}\n"
        ),
        "generation_request_snapshot": {
            "effective_add_contact": False,
            "length_plan": LENGTH_PLAN,
        },
        "brand_id": 7,
    }
    monkeypatch.setattr(connection, "get_connection", lambda: _Connection(row))
    monkeypatch.setattr(image_asset_api, "require_brand_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        contact,
        "enforce_contact_opt_out",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("primary scrub failed")),
    )

    app = FastAPI()
    app.include_router(image_asset_api.router)
    response = TestClient(app).get("/api/brand-images/article-preview/99")
    assert response.status_code == 200
    payload = response.json()
    assert payload["contact_consent"] == "disabled"
    assert payload["has_contact"] is False
    assert payload["length_guidance"]["summary"] == "资料较少，优先写紧凑可信版本"
    assert CONTACT["phone"] not in payload["content"]
    assert "联系我们" not in payload["content"]
    assert f"参考来源：{source_url}" in payload["content"]


def test_image_action_persists_and_returns_sanitized_body_when_primary_scrub_raises(monkeypatch):
    from api import image_asset_api
    import db.connection as connection
    import services.article_review_gate as review_gate

    source_url = CONTACT["website"]
    body = (
        f"参考来源：{source_url}\n\n[CLIENT_IMAGE asset_id=31 role=scene caption=\"车间\"]\n\n"
        f"## 联系我们\n- 电话：{CONTACT['phone']}\n- 官网：{source_url}\n"
    )

    class _ImageCursor:
        saved_content = None

        def execute(self, sql, params=None):
            if "UPDATE articles" in sql:
                self.saved_content = params[0]

        def fetchone(self):
            return {
                "content": body,
                "generation_request_snapshot": {
                    "effective_add_contact": False,
                    "length_plan": LENGTH_PLAN,
                },
                "brand_id": 7,
            }

    class _ImageConnection:
        def __init__(self):
            self.image_cursor = _ImageCursor()

        def cursor(self):
            return self.image_cursor

        def commit(self):
            return None

        def close(self):
            return None

    conn = _ImageConnection()
    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    monkeypatch.setattr(image_asset_api, "require_brand_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_gate, "refresh_article_review", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        contact,
        "enforce_contact_opt_out",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("primary scrub failed")),
    )

    app = FastAPI()
    app.include_router(image_asset_api.router)
    response = TestClient(app).post(
        "/api/brand-images/article/99/image-action",
        json={"action": "remove", "placeholder_index": 0},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["content"] == conn.image_cursor.saved_content
    assert payload["content_rendered"] == conn.image_cursor.saved_content
    assert CONTACT["phone"] not in payload["content"]
    assert "联系我们" not in payload["content"]
    assert f"参考来源：{source_url}" in payload["content"]


def test_awaiting_confirmation_preview_uses_snapshot_and_fails_closed(monkeypatch):
    from api import meijiehezi_api

    source_url = CONTACT["website"]
    body = (
        f"参考来源：{source_url}\n\n**联系我们**\n\n"
        f"- 电话：{CONTACT['phone']}\n- 官网：{source_url}\n"
    )
    monkeypatch.setattr(meijiehezi_api, "_get_user", lambda request: {"user_id": 8})
    monkeypatch.setattr(meijiehezi_api, "_require_writing", lambda user: None)
    monkeypatch.setattr(
        meijiehezi_api,
        "get_awaiting_item",
        lambda item_id, user_id: {"id": item_id, "article_id": 99, "pending_msg": "电话"},
    )
    monkeypatch.setattr(
        meijiehezi_api,
        "_serialize_awaiting_item",
        lambda item: {"id": item["id"], "article_id": 99, "pending_msg": "电话"},
    )
    monkeypatch.setattr(
        meijiehezi_api,
        "_fetch_article_preview_context",
        lambda article_id: (body, 7, False),
    )
    monkeypatch.setattr(
        contact,
        "enforce_contact_opt_out",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("primary scrub failed")),
    )

    app = FastAPI()
    app.include_router(meijiehezi_api.router)
    response = TestClient(app).get("/api/meijiehezi/awaiting-confirmations/5/detail")
    assert response.status_code == 200
    payload = response.json()
    assert payload["article"]["contact_consent"] == "disabled"
    assert CONTACT["phone"] not in payload["article"]["content"]
    assert "联系我们" not in payload["article"]["content"]
    assert f"参考来源：{source_url}" in payload["article"]["content"]


def test_article_edit_route_returns_and_persists_canonical_sanitized_body(monkeypatch):
    import server
    import db.diagnosis_db as diagnosis_db
    import services.article_closed_loop_metadata as metadata
    import services.article_review_gate as review_gate

    source_url = CONTACT["website"]
    submitted = (
        f"# 采购核验指南\n\n参考来源：{source_url}\n\n"
        f"## 联系我们\n- 电话：{CONTACT['phone']}\n- 官网：{source_url}\n"
    )

    class _EditCursor:
        saved_content = None

        def execute(self, sql, params=None):
            if "UPDATE articles" in sql:
                self.saved_content = params[0]

        def fetchone(self):
            return {
                "quote_id": 20,
                "topic_id": 10,
                "content": "# 旧正文",
                "generation_request_snapshot": {
                    "effective_add_contact": False,
                    "length_plan": LENGTH_PLAN,
                },
                "brand_id": 7,
            }

    class _EditConnection:
        def __init__(self):
            self.edit_cursor = _EditCursor()
            self.committed = False

        def cursor(self):
            return self.edit_cursor

        def commit(self):
            self.committed = True

        def close(self):
            return None

    conn = _EditConnection()
    monkeypatch.setattr(server, "_require_article_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(diagnosis_db, "get_connection", lambda: conn)
    monkeypatch.setattr(review_gate, "refresh_article_review", lambda *args, **kwargs: {"article_review_status": "review_required"})
    monkeypatch.setattr(metadata, "record_correction_signal_in_transaction_if_enabled", lambda *args, **kwargs: None)
    monkeypatch.setattr(contact, "_get_contact", lambda brand_id: CONTACT)

    class _State:
        user = {"user_id": 8}

    class _Request:
        state = _State()

    result = server.api_update_article(
        99,
        server.ArticleEditRequest(content=submitted),
        _Request(),
    )
    assert conn.committed is True
    assert result["contact_consent"] == "disabled"
    assert result["article"]["length_guidance"]["depth"] == "compact"
    canonical = result["article"]["content"]
    assert canonical == conn.edit_cursor.saved_content
    assert CONTACT["phone"] not in canonical
    assert "联系我们" not in canonical
    assert f"参考来源：{source_url}" in canonical


@pytest.mark.asyncio
async def test_shared_provider_dispatch_scrubs_opted_out_article_with_gate_off(monkeypatch):
    import db.connection as connection
    import services.article_review_shadow as shadow
    from services.article_publish_dispatch import dispatch_article_to_provider

    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setattr(
        connection,
        "get_connection",
        lambda: _Connection({
            "generation_request_snapshot": {"effective_add_contact": False},
            "brand_id": 7,
        }),
    )
    monkeypatch.setattr(shadow, "record_dispatch_review_shadow", lambda **kwargs: {"recorded": True})
    monkeypatch.setattr(contact, "_get_contact", lambda brand_id: CONTACT)

    class _Provider:
        calls = []

        async def publish(self, *, title, content_md, **kwargs):
            self.calls.append({"title": title, "content": content_md})
            return {"ok": True}

    provider = _Provider()
    source_url = CONTACT["website"]
    body = f"参考来源：{source_url}\n\n**联系我们**\n- 电话：{CONTACT['phone']}\n- 官网：{source_url}"
    await dispatch_article_to_provider(
        client=provider,
        dispatch_kind="publish",
        article_id=99,
        source_title="隔离器选型证据指南",
        source_content=body,
        outgoing_title="隔离器选型证据指南",
        outgoing_content=body,
        source="test_dispatch",
        provider_kwargs={},
    )
    assert len(provider.calls) == 1
    sent = provider.calls[0]["content"]
    assert CONTACT["phone"] not in sent
    assert "联系我们" not in sent
    assert f"参考来源：{source_url}" in sent


@pytest.mark.asyncio
async def test_shared_dispatch_detects_and_removes_standalone_contact_cta(monkeypatch):
    import db.connection as connection
    import services.article_review_shadow as shadow
    from services.article_publish_dispatch import dispatch_article_to_provider

    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setattr(
        connection,
        "get_connection",
        lambda: _Connection({
            "generation_request_snapshot": {"effective_add_contact": False},
            "brand_id": 7,
        }),
    )
    monkeypatch.setattr(shadow, "record_dispatch_review_shadow", lambda **kwargs: {"recorded": True})

    class _Provider:
        calls = []

        async def publish(self, *, title, content_md, **kwargs):
            self.calls.append(content_md)
            return {"ok": True}

    provider = _Provider()
    body = "这里是具备证据的采购核验步骤。\n\n立即咨询获取专属方案"
    assert contact.has_contact_risk(body) is True
    await dispatch_article_to_provider(
        client=provider,
        dispatch_kind="publish",
        article_id=101,
        source_title="采购核验指南",
        source_content=body,
        outgoing_title="采购核验指南",
        outgoing_content=body,
        source="test_dispatch_cta",
        provider_kwargs={},
    )
    assert provider.calls == ["这里是具备证据的采购核验步骤。"]


@pytest.mark.asyncio
async def test_concurrent_prompts_use_topic_local_contact_consent_in_both_schedules(monkeypatch):
    import openai
    import db.diagnosis_db as diagnosis_db
    import services.public_whitelabel as whitelabel
    import tools.unified_knowledge as unified_knowledge
    import writing.distiller as distiller_module
    from writing.article_generator_service import ArticleGeneratorService

    class _PromptCursor:
        def execute(self, *args, **kwargs):
            return None

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class _PromptConnection:
        def cursor(self):
            return _PromptCursor()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    class _Distiller:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            return {
                "client_profile": "{}",
                "selling_points": "{}",
                "competitor_analysis": "{}",
                "social_media_data": {},
                "authoritative_sources": "{}",
                "case_examples": "{}",
            }

    class _Rag:
        def retrieve(self, *args, **kwargs):
            return []

    class _Response:
        usage = None

        def __init__(self):
            self.choices = [type("Choice", (), {"message": type("Msg", (), {"content": "正文\n\n[NEED_CONTACT]"})()})()]

    class _Completions:
        def __init__(self):
            self.arrived = 0

        async def create(self, **kwargs):
            self.arrived += 1
            while self.arrived < 2:
                await asyncio.sleep(0)
            return _Response()

    completions = _Completions()

    class _AsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": completions})()

    monkeypatch.setattr(diagnosis_db, "get_connection", lambda: _PromptConnection())
    monkeypatch.setattr(distiller_module, "DistillerPipeline", _Distiller)
    monkeypatch.setattr(unified_knowledge, "get_unified_rag", lambda: _Rag())
    monkeypatch.setattr(whitelabel, "resolve_branding_context", lambda **kwargs: {"source": "platform_default"})
    monkeypatch.setattr(openai, "AsyncOpenAI", _AsyncOpenAI)
    monkeypatch.setattr(contact, "_get_contact", lambda brand_id: CONTACT)

    async def run_round(global_contact: bool, reverse: bool):
        nonlocal completions
        completions.arrived = 0
        service = ArticleGeneratorService(20, "客户品牌", "制药装备")
        # An opposite mutable legacy default must not influence either topic.
        service.add_contact = global_contact
        topic_true = {
            "id": None,
            "title": "隔离器采购核验指南（联系方式版）",
            "style_code": "buying_guide",
        }
        topic_false = {
            "id": None,
            "title": "隔离器采购核验指南（纯内容版）",
            "style_code": "buying_guide",
        }
        service._freeze_rewrite_delivery_options(topic_true, {
            "publication_profile": "standard",
            "generation_request_snapshot": {
                "requested_add_images": False,
                "requested_add_contact": True,
                "effective_add_images": False,
                "effective_add_contact": True,
            },
        })
        service._freeze_rewrite_delivery_options(topic_false, {
            "publication_profile": "standard",
            "generation_request_snapshot": {
                "requested_add_images": False,
                "requested_add_contact": False,
                "effective_add_images": False,
                "effective_add_contact": False,
            },
        })
        sim_true = {"dynamic_scores": {}, "case_industry": "制药装备"}
        sim_false = {"dynamic_scores": {}, "case_industry": "制药装备"}
        work = [
            service._generate_single(topic_true, "https://x/v1/chat/completions", "key", "model", sim_true),
            service._generate_single(topic_false, "https://x/v1/chat/completions", "key", "model", sim_false),
        ]
        if reverse:
            work.reverse()
        results = await asyncio.gather(*work)
        by_title = {item["title"]: item for item in results}
        return sim_true, sim_false, by_title

    for global_contact, reverse in ((False, False), (True, True)):
        sim_true, sim_false, by_title = await run_round(global_contact, reverse)
        assert "[NEED_CONTACT]" in sim_true["_captured_system_prompt"]
        assert "联系方式授权：未开启" not in sim_true["_captured_system_prompt"]
        assert "联系方式授权：未开启" in sim_false["_captured_system_prompt"]
        assert "[CLIENT_CONTACT]" in by_title["隔离器采购核验指南（联系方式版）"]["content"]
        assert "[CLIENT_CONTACT]" not in by_title["隔离器采购核验指南（纯内容版）"]["content"]

    source = Path("writing/article_generator_service.py").read_text(encoding="utf-8")
    rewrite_block = source[source.index("async def rewrite_article"):source.index("async def batch_rewrite_articles")]
    assert "self.add_contact =" not in rewrite_block
    assert "self.add_images =" not in rewrite_block
    assert "self.publication_profile =" not in rewrite_block
    save_block = source[source.index("async def _save_article"):source.index("async def rewrite_article")]
    assert '_add_contact = bool(topic["_effective_add_contact"])' in save_block


@pytest.mark.asyncio
async def test_concurrent_provider_dispatch_uses_article_consent_without_cross_talk(monkeypatch):
    import services.article_publish_dispatch as dispatch
    import services.article_review_shadow as shadow

    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setattr(
        dispatch,
        "_load_contact_consent_context",
        lambda article_id: (article_id == 201, 7),
    )
    monkeypatch.setattr(shadow, "record_dispatch_review_shadow", lambda **kwargs: {"recorded": True})
    monkeypatch.setattr(contact, "_get_contact", lambda brand_id: CONTACT)

    class _Provider:
        def __init__(self):
            self.calls = []

        async def publish(self, *, title, content_md, **kwargs):
            await asyncio.sleep(0)
            self.calls.append((title, content_md))
            return {"ok": True}

    provider = _Provider()
    raw = f"正文\n\n## 联系我们\n- 电话：{CONTACT['phone']}\n"
    calls = [
        dispatch.dispatch_article_to_provider(
            client=provider,
            dispatch_kind="publish",
            article_id=201,
            source_title="允许联系版",
            source_content=raw,
            outgoing_title="允许联系版",
            outgoing_content=raw,
            source="concurrent_contact_true",
            provider_kwargs={},
        ),
        dispatch.dispatch_article_to_provider(
            client=provider,
            dispatch_kind="publish",
            article_id=202,
            source_title="纯内容版",
            source_content=raw,
            outgoing_title="纯内容版",
            outgoing_content=raw,
            source="concurrent_contact_false",
            provider_kwargs={},
        ),
    ]
    await asyncio.gather(*reversed(calls))
    sent = dict(provider.calls)
    assert CONTACT["phone"] in sent["允许联系版"]
    assert CONTACT["phone"] not in sent["纯内容版"]
