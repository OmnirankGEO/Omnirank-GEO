"""帮助中心身份隔离测试(离线 · 不连真实 DB)。

覆盖老板要求的关键点:
- 普通用户 FAQ 列表只拿 normal_user+both · 代理拿 agent+both · 管理员拿全部
- 普通用户没有可绕过列表直接按 id 拿 FAQ 内容的详情端点
- 文档白名单接口 /api/help/docs/acl 三身份:普通用户挡代理+管理员文档、代理挡管理员文档、管理员不挡
- 后端身份解析以真实身份(is_admin / agent_level)为准
- 管理端写 FAQ 时 visible_to 非法值被拒
"""
import types

import pytest
from fastapi import HTTPException


def _fake_request(user):
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


# ========== 纯函数 ==========

def test_visible_to_filter_by_identity():
    from db.faq_db import _visible_to_filter
    assert _visible_to_filter("admin") is None
    assert _visible_to_filter(None) is None
    assert set(_visible_to_filter("agent")) == {"agent", "both"}
    assert set(_visible_to_filter("normal_user")) == {"normal_user", "both"}


def test_resolve_identity(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 0)
    assert faq_api._resolve_identity({"id": 1, "is_admin": True}) == "admin"
    assert faq_api._resolve_identity({"id": 2, "is_admin": False, "agent_level": 1}) == "agent"
    assert faq_api._resolve_identity({"id": 5, "is_admin": False, "agent_level": 2}) == "l2"
    assert faq_api._resolve_identity({"id": 3, "is_admin": False, "agent_level": 0}) == "normal_user"
    # agent_level 不在 token 里时查库(此处 mock 成 0 → 普通用户)
    assert faq_api._resolve_identity({"id": 4, "is_admin": False}) == "normal_user"


def test_hidden_doc_slugs_per_role():
    from api.faq_api import (
        _hidden_doc_slugs, AGENT_ONLY_DOC_SLUGS, L2_ONLY_DOC_SLUGS,
        NORMAL_ONLY_DOC_SLUGS, ADMIN_ONLY_DOC_SLUGS,
    )
    normal = set(_hidden_doc_slugs("normal_user"))
    agent = set(_hidden_doc_slugs("agent"))    # L1
    l2 = set(_hidden_doc_slugs("l2"))          # L2
    admin = set(_hidden_doc_slugs("admin"))
    A, L, N, AD = (set(AGENT_ONLY_DOC_SLUGS), set(L2_ONLY_DOC_SLUGS),
                   set(NORMAL_ONLY_DOC_SLUGS), set(ADMIN_ONLY_DOC_SLUGS))
    assert normal == A | L | AD                # 普通用户:挡代理+L2+管理员
    assert len(normal) == 14                    # 说明书 ACL 基线不得因视频主页恢复而漂移
    assert agent == N | L | AD                 # 一级代理:挡客户专属+L2专属+管理员
    assert l2 == N | AD                        # 二级代理:挡客户专属+管理员(能看 L2)
    assert admin == set()
    # 关键断言
    assert "wallet-service-fee-history" in agent and "wallet-service-fee-history" not in l2  # L2专属
    assert "feedback" not in normal and "feedback" not in agent                              # feedback 共享
    assert "customer-wallet" in agent and "customer-wallet" not in normal                    # 客户专属·代理看不到


# ========== FAQ 列表按身份过滤(验证 wiring · 捕获传给 list_faq_items 的 identity) ==========

@pytest.mark.asyncio
async def test_faq_list_normal_user_identity(monkeypatch):
    import api.faq_api as faq_api
    captured = {}
    monkeypatch.setattr(faq_api, "list_faq_items", lambda **k: captured.update(k) or [])
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 0)
    await faq_api.api_list_faq(_fake_request({"id": 1, "is_admin": False}), None)
    assert captured["identity"] == "normal_user"
    assert captured["include_unpublished"] is False


@pytest.mark.asyncio
async def test_faq_list_agent_identity(monkeypatch):
    import api.faq_api as faq_api
    captured = {}
    monkeypatch.setattr(faq_api, "list_faq_items", lambda **k: captured.update(k) or [])
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 1)
    await faq_api.api_list_faq(_fake_request({"id": 2, "is_admin": False}), None)
    assert captured["identity"] == "agent"


@pytest.mark.asyncio
async def test_faq_list_admin_sees_all(monkeypatch):
    import api.faq_api as faq_api
    captured = {}
    monkeypatch.setattr(faq_api, "list_faq_items", lambda **k: captured.update(k) or [])
    await faq_api.admin_list_faq(_fake_request({"id": 9, "is_admin": True}), None)
    assert captured["include_unpublished"] is True


# ========== 文档 ACL 白名单接口三身份 ==========

@pytest.mark.asyncio
async def test_acl_normal_user_hides_agent_and_admin(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 0)
    res = await faq_api.api_help_docs_acl(_fake_request({"id": 1, "is_admin": False}))
    assert res["role"] == "normal_user"
    hidden = set(res["hidden_slugs"])
    assert {"provider-guide", "leads", "stock-up", "set-pricing", "profit"} <= hidden
    assert {"pricing", "my-clients", "first-client", "whitelabel"}.isdisjoint(hidden)
    assert "standard-guide" not in hidden
    assert {"roles", "team-members", "audit-log"} <= hidden  # 管理员文档


@pytest.mark.asyncio
async def test_acl_agent_hides_admin_only(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 1)
    res = await faq_api.api_help_docs_acl(_fake_request({"id": 2, "is_admin": False}))
    assert res["role"] == "agent"
    hidden = set(res["hidden_slugs"])
    assert "provider-guide" not in hidden
    assert hidden.isdisjoint({"pricing", "stock-up", "set-pricing", "profit", "whitelabel"})
    assert "standard-guide" in hidden
    assert {"roles", "team-members", "audit-log"} <= hidden  # 但管理员文档看不到


@pytest.mark.asyncio
async def test_acl_admin_hides_nothing():
    import api.faq_api as faq_api
    res = await faq_api.api_help_docs_acl(_fake_request({"id": 9, "is_admin": True}))
    assert res["role"] == "admin"
    assert res["hidden_slugs"] == []


# ========== FAQ 详情无独立泄漏点 ==========

def test_no_user_facing_faq_detail_endpoint():
    """FAQ 内容只通过已按身份过滤的列表返回。用户侧没有 GET /api/faq/items/{id} 详情端点,
    普通用户无法绕过列表直接按 id 拿代理 FAQ 的 answer_md(只有管理员侧 /api/admin/faq/items/{id})。"""
    from api.faq_api import router
    user_detail = [r for r in router.routes if getattr(r, "path", "") == "/api/faq/items/{faq_id}"]
    assert user_detail == []


def test_help_docs_routes_registered_in_auth_mapping():
    """真实请求先过全局权限中间件；文档正文接口必须登记为仅需认证，
    具体身份隔离仍由 api.faq_api 内部做。"""
    from auth.module_mapping import resolve_permission
    assert resolve_permission("/api/help/docs/acl") is None
    assert resolve_permission("/api/help/docs/wallet") is None


# ========== 管理端写 FAQ 的 visible_to 校验 ==========

@pytest.mark.asyncio
async def test_admin_create_faq_rejects_bad_visible_to():
    import api.faq_api as faq_api
    from api.faq_api import CreateFAQRequest
    body = CreateFAQRequest(question="测试问题问题", category="billing", visible_to="boss")
    with pytest.raises(HTTPException) as exc:
        await faq_api.admin_create_faq(_fake_request({"id": 9, "is_admin": True}), body)
    assert exc.value.status_code == 400


# ========== 帮助文档正文真隔离:后端按身份返回正文 ==========

@pytest.mark.asyncio
async def test_doc_body_normal_user_denied_agent_slug(monkeypatch):
    """普通用户请求代理文档 slug → 403,拿不到正文(真隔离核心)。"""
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 0)
    for slug in ("agent-wallet", "stock-up", "set-pricing", "profit"):
        with pytest.raises(HTTPException) as exc:
            await faq_api.api_help_doc_body(slug, _fake_request({"id": 1, "is_admin": False}))
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_doc_body_agent_gets_agent_slug(monkeypatch):
    """代理请求代理文档 → 拿到正文。"""
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 1)
    res = await faq_api.api_help_doc_body("agent-wallet", _fake_request({"id": 2, "is_admin": False}))
    assert res["slug"] == "agent-wallet" and len(res["body"]) > 50


@pytest.mark.asyncio
async def test_doc_body_normal_user_gets_both_slug(monkeypatch):
    """普通用户请求共享文档(both)→ 拿到正文。"""
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 0)
    res = await faq_api.api_help_doc_body("wallet", _fake_request({"id": 1, "is_admin": False}))
    assert res["slug"] == "wallet" and len(res["body"]) > 50


@pytest.mark.asyncio
async def test_doc_body_normal_user_denied_admin_slug(monkeypatch):
    """普通用户请求管理员文档 → 403。"""
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 0)
    with pytest.raises(HTTPException) as exc:
        await faq_api.api_help_doc_body("roles", _fake_request({"id": 1, "is_admin": False}))
    assert exc.value.status_code == 403


# ========== L2 专属:服务费流水(普通 403 / L1 403 / L2 200)==========

@pytest.mark.asyncio
async def test_service_fee_history_l1_denied(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 1)  # L1
    with pytest.raises(HTTPException) as exc:
        await faq_api.api_help_doc_body("wallet-service-fee-history", _fake_request({"id": 2, "is_admin": False}))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_service_fee_history_normal_denied(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 0)
    with pytest.raises(HTTPException) as exc:
        await faq_api.api_help_doc_body("wallet-service-fee-history", _fake_request({"id": 1, "is_admin": False}))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_service_fee_history_l2_ok(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 2)  # L2
    res = await faq_api.api_help_doc_body("wallet-service-fee-history", _fake_request({"id": 3, "is_admin": False}))
    assert res["slug"] == "wallet-service-fee-history" and len(res["body"]) > 50


# ========== feedback 共享(普通 / 代理都能看)==========

@pytest.mark.asyncio
async def test_feedback_normal_ok(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 0)
    res = await faq_api.api_help_doc_body("feedback", _fake_request({"id": 1, "is_admin": False}))
    assert res["slug"] == "feedback" and len(res["body"]) > 50


@pytest.mark.asyncio
async def test_feedback_agent_ok(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 1)
    res = await faq_api.api_help_doc_body("feedback", _fake_request({"id": 2, "is_admin": False}))
    assert res["slug"] == "feedback" and len(res["body"]) > 50


# ========== 客户额度页普通用户专属(代理 403 / 普通 200)==========

@pytest.mark.asyncio
async def test_customer_docs_agent_denied(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 1)  # 代理
    for slug in ("customer-wallet", "customer-recharge"):
        with pytest.raises(HTTPException) as exc:
            await faq_api.api_help_doc_body(slug, _fake_request({"id": 2, "is_admin": False}))
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_customer_docs_normal_ok(monkeypatch):
    import api.faq_api as faq_api
    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _uid: 0)
    res = await faq_api.api_help_doc_body("customer-wallet", _fake_request({"id": 1, "is_admin": False}))
    assert res["slug"] == "customer-wallet" and len(res["body"]) > 50


# ========== 导航 slug 与后端正文 slug 必须一致 ==========

def test_nav_matches_content():
    """docs-data.ts 的导航 slug 必须与 help_docs_content.json 完全一致:
    避免前端有入口但后端没正文(404),或后端有正文但前端没入口。"""
    import os, re, json
    root = os.getcwd()
    ts = open(os.path.join(root, "frontend/src/pages/Help/docs-data.ts"), encoding="utf-8").read()
    nav = set(re.findall(r"slug: '([^']+)'", ts))
    content = set(json.load(open(os.path.join(root, "api/help_docs_content.json"), encoding="utf-8-sig")).keys())
    assert nav == content, f"导航/正文 slug 不一致: 仅导航 {nav - content} · 仅正文 {content - nav}"


def test_help_docs_indexer_reads_backend_content_and_audience():
    """帮助文档正文已迁后端;小榜 doc 索引必须读取 help_docs_content.json,
    并保留 audience,否则旧 doc chunk 会按 both 泄漏给普通用户。"""
    from tools.xiaobang_kb_indexer import parse_docs_ts
    docs = {d["slug"]: d for d in parse_docs_ts()}
    assert len(docs) == 51
    assert len(docs["wallet"]["body"]) > 50
    assert docs["provider-guide"]["visible_to"] == "agent"
    assert docs["standard-guide"]["visible_to"] == "normal_user"
    assert docs["agent-wallet"]["visible_to"] == "agent"
    assert docs["customer-wallet"]["visible_to"] == "normal_user"
    assert docs["wallet-service-fee-history"]["visible_to"] == "l2"
    assert docs["feedback"]["visible_to"] == "both"


def test_help_entry_opens_operation_manual_directly():
    """帮助入口继续直达说明书，同时视频主页有独立路由。"""
    import os

    root = os.getcwd()
    app = open(os.path.join(root, "frontend/src/App.tsx"), encoding="utf-8").read()
    help_center = open(
        os.path.join(root, "frontend/src/pages/Help/HelpCenter.tsx"),
        encoding="utf-8",
    ).read()
    help_docs = open(
        os.path.join(root, "frontend/src/pages/Help/HelpDocs.tsx"),
        encoding="utf-8",
    ).read()
    assert 'path="help" element={<ProtectedRoute><HelpDocs /></ProtectedRoute>}' in app
    assert "const HelpCenter = lazy(() => import('@/pages/Help/HelpCenter'))" in app
    assert 'path="help/home" element={<ProtectedRoute><HelpCenter /></ProtectedRoute>}' in app
    assert 'to="/help/home"' in help_docs
    assert "export { default } from './HelpDocs'" not in help_center
    assert "视频教程主页" in help_center


def test_help_home_restart_contract_and_guidance_paths_match():
    """老用户能从真实路径重走教程，两个提示入口与实现保持一致。"""
    import os

    root = os.getcwd()
    help_center = open(
        os.path.join(root, "frontend/src/pages/Help/HelpCenter.tsx"),
        encoding="utf-8",
    ).read()
    welcome = open(
        os.path.join(root, "frontend/src/components/onboarding/WelcomeChoiceModal.tsx"),
        encoding="utf-8",
    ).read()
    banner = open(
        os.path.join(root, "frontend/src/sandbox/SandboxBanner.tsx"),
        encoding="utf-8",
    ).read()
    assert "resetSandboxTutorialState()" in help_center
    assert "enterSandbox()" in help_center
    assert "帮助中心 → 视频教程 → 重走新手教程" in welcome
    assert "欢迎使用 OmniRank" not in welcome
    assert "帮助中心」→「视频教程」→「重走新手教程" in banner
    assert "window.location.href = '/help/home'" in banner


def test_step_videos_declare_audience():
    """九条工作流视频必须逐条声明受众，普通用户主页不能靠标题猜权限。"""
    import os
    import re

    root = os.getcwd()
    source = open(
        os.path.join(root, "frontend/src/components/help/videos-data.ts"),
        encoding="utf-8",
    ).read()
    step_blocks = re.findall(
        r"\{[^{}]*category: 'step',[^{}]*\}",
        source,
        flags=re.DOTALL,
    )
    assert len(step_blocks) == 9
    assert all(re.search(r"audience: '(?:all|agent)'", block) for block in step_blocks)


def test_help_content_uses_current_product_and_runtime_pricing():
    """帮助正文不得继续传播旧品牌、历史固定价格或过度阻断语义。"""
    import json
    import os
    import re

    root = os.getcwd()
    content = json.load(
        open(
            os.path.join(root, "api/help_docs_content.json"),
            encoding="utf-8-sig",
        )
    )
    assert len(content) == 51
    all_body = "\n".join(str(doc["body"]) for doc in content.values())
    assert "OmniRank" not in all_body
    assert "全域上榜GEO交付系统" in all_body
    assert not re.search(r"\b\d+(?:\.\d+)?\s*算力", all_body)
    assert "给客户写文章前系统**会强制你回来补全**" not in all_body
    assert "正文未通过证据校验，系统已拒绝保存" not in all_body
    assert "算力和模型以操作弹窗为准" in all_body


def test_help_content_covers_current_high_value_workflows():
    import json
    import os

    root = os.getcwd()
    content = json.load(
        open(
            os.path.join(root, "api/help_docs_content.json"),
            encoding="utf-8-sig",
        )
    )
    required = {
        "provider-guide",
        "standard-guide",
        "team-and-seats",
        "employee-invitation",
        "demo-cases",
        "customer-portal",
        "geo-content-center",
        "article-review-and-repair",
        "monitoring-retry-and-brand-confirmation",
    }
    assert required <= set(content)
    for slug in required:
        assert len(content[slug]["body"]) > 200
    assert "真实页面" in content["demo-cases"]["body"]
    assert "AI 修复这一处" in content["article-review-and-repair"]["body"]
