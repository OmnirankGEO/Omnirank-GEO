from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _tracked_python_files():
    names = subprocess.check_output(
        ["git", "ls-files", "*.py"], cwd=ROOT, text=True, encoding="utf-8"
    ).splitlines()
    return [ROOT / name for name in names]


class _Provider:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def publish(self, **kwargs):
        self.calls.append(("publish", kwargs))
        return kwargs

    async def publish_wemedia(self, **kwargs):
        self.calls.append(("publish_wemedia", kwargs))
        return kwargs


class _RowCursor:
    def __init__(self, row):
        self.row = row

    def execute(self, _query, _params=None):
        return None

    def fetchone(self):
        return dict(self.row)


class _RowConnection:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return _RowCursor(self.row)

    def close(self):
        return None


def _dispatch(provider: _Provider, *, title: str, content: str):
    from services.article_publish_dispatch import dispatch_article_to_provider

    return asyncio.run(dispatch_article_to_provider(
        client=provider,
        dispatch_kind="publish",
        article_id=42,
        source_title=title,
        source_content=content,
        outgoing_title=title,
        outgoing_content=content,
        source="test_legacy",
        provider_kwargs={"media_ids": [1]},
    ))


def test_minimum_outbound_gate_remains_on_when_full_review_is_off(monkeypatch):
    import services.article_publish_dispatch as dispatch

    monkeypatch.setattr(dispatch, "is_publication_review_gate_enabled", lambda: False)
    # This test isolates the outbound sanity gate. Persisted-article consent
    # lookup has its own fail-closed coverage in test_article_contact_consent.
    monkeypatch.setattr(dispatch, "_load_contact_consent_context", lambda article_id: (None, None))
    provider = _Provider()
    result = _dispatch(
        provider,
        title="设备服务怎么选？条件与核验方法",
        content="本文说明公开资料的适用范围、风险和官方核验步骤。",
    )
    assert result["title"] == "设备服务怎么选？条件与核验方法"
    assert result["content_md"] == "本文说明公开资料的适用范围、风险和官方核验步骤。"
    assert len(provider.calls) == 1

    # A legitimate body may describe its own source list; the sanity gate is
    # not a broad case-insensitive keyword ban.
    sourced_provider = _Provider()
    sourced = _dispatch(
        sourced_provider,
        title="设备服务怎么选？来源核验方法",
        content="本文列出 Top 5 来源类别，并逐项说明公开资料的核验边界。",
    )
    assert sourced["content_md"].startswith("本文列出 Top 5 来源类别")
    assert len(sourced_provider.calls) == 1

    historical_provider = _Provider()
    from services.article_publish_dispatch import dispatch_article_to_provider
    asyncio.run(dispatch_article_to_provider(
        client=historical_provider,
        dispatch_kind="publish",
        article_id=None,
        source_title="旧文章发布",
        source_content="旧文章正文保留原样。",
        outgoing_title="旧文章发布",
        outgoing_content="旧文章正文保留原样。",
        source="legacy_without_article_identity",
        provider_kwargs={"media_ids": [1]},
    ))
    assert len(historical_provider.calls) == 1

    # [SSOT geo-commercial-intent-governance-v1.0 §4.4/§5.1 · Review-CTO
    #  2026-07-23] 发布门内容层硬阻断只保留法律禁止项(《广告法》第九条
    #  绝对化)与技术项(空正文/占位内容)。排名/榜单形态、无源数字等
    #  已降 advisory,不在发布门硬拦。
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", "false")
    # 排名形态标题:合法,照常发布(排序依据披露走 advisory + 人工确认)
    ranking_provider = _Provider()
    _dispatch(ranking_provider, title="2026年设备服务商TOP10推荐榜单", content="正常正文")
    assert len(ranking_provider.calls) == 1
    # 法律绝对化 + 技术占位/空正文:仍 fail-closed
    for title, content in (
        ("本设备绝对是行业唯一首选", "正常正文"),
        ("设备服务怎么选", "[自动生成失败，占位内容] 正文"),
        ("", "正常正文"),
    ):
        blocked_provider = _Provider()
        with pytest.raises(dispatch.ArticlePublicationBlocked):
            _dispatch(blocked_provider, title=title, content=content)
        assert blocked_provider.calls == []


@pytest.mark.parametrize("source", [
    "confirmed_resubmit:softarticle:none",
    "scheduler_retry:softarticle:none",
    "legacy_publish_batch:softarticle:none",
    "mhz_channel_submit:softarticle:none",
])
def test_full_gate_blocks_each_dispatch_lane_after_canonical_edit(monkeypatch, source):
    import db.connection as connection_module
    import services.article_publish_dispatch as dispatch

    canonical = "编辑后的规范正文：公开资料、限制和官方核验步骤。"
    row = {
        "id": 42,
        "title": "设备服务怎么选？条件与核验方法",
        "content": canonical,
        "evidence_manifest_hash": "e" * 64,
        "publication_profile": "standard",
    }
    monkeypatch.setattr(dispatch, "is_publication_review_gate_enabled", lambda: True)
    monkeypatch.setattr(connection_module, "get_connection", lambda: _RowConnection(row))
    monkeypatch.setattr(
        dispatch,
        "assert_publication_eligible",
        lambda *_args, **_kwargs: {"eligible": True, "reason": "machine_rules_approved"},
    )
    provider = _Provider()
    with pytest.raises(dispatch.ArticlePublicationBlocked) as exc:
        asyncio.run(dispatch.dispatch_article_to_provider(
            client=provider,
            dispatch_kind="publish",
            article_id=42,
            source_title=row["title"],
            source_content="下单时旧正文：公开资料、限制和官方核验步骤。",
            outgoing_title=row["title"],
            outgoing_content="下单时旧正文：公开资料、限制和官方核验步骤。",
            source=source,
            provider_kwargs={"media_ids": [1]},
        ))
    assert exc.value.payload["reason"] == "content_changed_before_dispatch"
    assert provider.calls == []


def test_full_gate_sends_the_exact_reviewed_snapshot_and_honors_revocation(monkeypatch):
    import db.connection as connection_module
    import services.article_publish_dispatch as dispatch

    title = "设备服务怎么选？条件与核验方法"
    content = "本文依据公开资料说明适用范围、风险和官方核验步骤。"
    row = {
        "id": 42,
        "title": title,
        "content": content,
        "evidence_manifest_hash": "e" * 64,
        "publication_profile": "standard",
    }
    monkeypatch.setattr(dispatch, "is_publication_review_gate_enabled", lambda: True)
    monkeypatch.setattr(connection_module, "get_connection", lambda: _RowConnection(row))
    monkeypatch.setattr(
        dispatch,
        "assert_publication_eligible",
        lambda *_args, **_kwargs: {"eligible": True, "reason": "human_approved_snapshot_match"},
    )
    provider = _Provider()
    frozen = []
    result = asyncio.run(dispatch.dispatch_article_to_provider(
        client=provider,
        dispatch_kind="publish",
        article_id=42,
        source_title=title,
        source_content=content,
        outgoing_title=title,
        outgoing_content=content,
        source="mhz_channel_submit:softarticle:none",
        provider_kwargs={"media_ids": [1]},
        snapshot_writer=frozen.append,
    ))
    assert result["title"] == frozen[0].title == title
    assert result["content_md"] == frozen[0].content == content
    assert provider.calls[0][1]["content_md"] == frozen[0].content

    def _revoked(*_args, **_kwargs):
        raise dispatch.ArticlePublicationBlocked({
            "eligible": False,
            "reason": "human_rejected",
            "message": "review revoked",
        })

    monkeypatch.setattr(dispatch, "assert_publication_eligible", _revoked)
    blocked_provider = _Provider()
    with pytest.raises(dispatch.ArticlePublicationBlocked):
        asyncio.run(dispatch.dispatch_article_to_provider(
            client=blocked_provider,
            dispatch_kind="publish",
            article_id=42,
            source_title=title,
            source_content=content,
            outgoing_title=title,
            outgoing_content=content,
            source="scheduler_retry:softarticle:none",
            provider_kwargs={"media_ids": [1]},
        ))
    assert blocked_provider.calls == []


def test_all_article_provider_calls_are_owned_by_the_shared_choke_point():
    allowed = {
        Path("services/article_publish_dispatch.py"),
        Path("services/meijiehezi_client.py"),  # provider adapter self-test
        Path("services/meijiehezi/client.py"),  # provider implementation self-test
        # [WO_273] 原有插件的 Redis pubsub 命令总线一条;该文件随插件后端删除,放行条目同步删。
    }
    violations: list[str] = []
    for path in _tracked_python_files():
        relative = path.relative_to(ROOT)
        if "tests" in relative.parts or relative in allowed:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, SyntaxError):
            continue  # tracked IDE/binary artifacts are not executable source
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"publish", "publish_wemedia"}:
                continue
            owner = node.func.value
            if (
                node.func.attr == "publish"
                and isinstance(owner, ast.Name)
                and owner.id in {"_progress_bus", "progress_bus"}
            ):
                continue
            violations.append(f"{relative}:{node.lineno}:{node.func.attr}")
    assert violations == [], "direct article provider calls bypass dispatch: " + ", ".join(violations)


def test_short_video_lane_stays_outside_article_dispatch_census():
    source = (ROOT / "api/meijiehezi_api.py").read_text(encoding="utf-8")
    assert "client.publish_short_video(" in source
    start = source.index("async def api_publish_short_video(")
    end = source.find("\n@", start + 1)
    short_video_handler = source[start:end if end > start else None]
    assert "dispatch_article_to_provider" not in short_video_handler


# [WO_273 · 2026-09-23 肯定式退役] 原有 3 格 + 2 个构造器(`_extension_client` / `_extension_payload`):
#   `test_real_extension_route_applies_minimum_gate_to_articleless_publish`
#   `test_real_extension_route_requires_identity_when_full_gate_is_on`
#   `test_real_extension_route_keeps_canonical_article_authority`
# 它们钉的是「插件发布路由真的接在共享发布门上」。该路由随插件后端**整体删除** —— 那条出站车道没了,
# 不是换了实现。发布门本身的行为照旧由本文件上方直打 `dispatch_article_to_provider` 的各格守
# (最低出站门 / 全量门按车道拦改稿 / 发的正是审过的快照)。
# 接替:
#   · 「插件路由不许悄悄回来」→ tests/extension_retirement_2026_09_23 的路由缺席锁;
#   · 「任何新出站车道都得过共享门」→ 本文件 `test_all_article_provider_calls_are_owned_by_the_shared_choke_point`。


def test_disabled_legacy_title_types_use_six_family_evidence_first_ssot():
    from writing.article_style_contract import evidence_first_title_for_family
    from writing.evidence_first_policy import evaluate_content_trust

    forbidden = re.compile(r"TOP\s*\d+|排行榜|榜单|排名(?:出炉|第一|前)|第一名|权威测评|综合评分", re.I)
    for style in ("ranking_v2", "authority_ranking", "deep_comparison", "expert_opinion"):
        title = evidence_first_title_for_family(
            brand_name="客户品牌",
            industry="生命科学",
            family_or_style=style,
            keywords=["细胞治疗药物研发和生产隔离器"],
            index=1,
        )
        assert not forbidden.search(title), title
        assert not evaluate_content_trust(title, "").hard


def test_legacy_plan_runtime_fallback_contains_no_unsafe_title_template():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start = source.index("def generate_title_for_type(")
    end = source.index("@app.post(\"/api/articles/generate\")", start)
    body = source[start:end]
    assert "evidence_first_title_for_family" in body
    assert not re.search(r"TOP\s*\d+|排行榜|榜单|排名出炉|权威测评|综合评分", body, re.I)


def test_retired_billed_topic_endpoint_is_a_side_effect_free_410_tombstone():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start = source.index('@app.post("/api/topics/generate")')
    end = source.index('@app.get("/api/quotes")', start)
    body = source[start:end]
    assert 'status_code=410' in body
    assert '"code": "TOPICS_GENERATE_RETIRED"' in body
    assert '"replacement": "POST /api/writing/generate-titles"' in body
    for forbidden in (
        "get_keywords_by_quote", "deduct_points", "refund_points",
        "generate_single_title", "save_topics", 'article_style": "authority',
    ):
        assert forbidden not in body


def test_runtime_title_fallback_census_has_only_reviewed_legacy_inputs():
    """New unsafe direct title/fallback literals fail unless explicitly classified."""
    forbidden = re.compile(r"TOP\s*\d+|排行榜|榜单|排名(?:出炉|第一|前)|第一名|权威测评|综合评分", re.I)
    allowlisted_inputs = {
        Path("services/writing_style_simulation.py"),  # offline historical simulation
        Path("tools/keyword/longtail_matrix.py"),  # purchased keyword research prompt, not article fallback
        Path("writing/article_writer.py"),  # prompt example consumed behind evidence-first output gate
        Path("writing/evidence_first_policy.py"),  # the detector SSOT itself
        Path("writing/ranking_prompt_v9.py"),  # disabled historical prompt, new generation 0%
    }
    violations: list[str] = []
    for path in _tracked_python_files():
        rel = path.relative_to(ROOT)
        if "tests" in rel.parts or "scripts" in rel.parts or rel in allowlisted_inputs:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(
                    isinstance(target, ast.Name)
                    and ("title" in target.id.lower() or "fallback" in target.id.lower())
                    for target in targets
                ):
                    candidates = [node.value]
            elif isinstance(node, ast.Dict):
                candidates = [
                    value for key, value in zip(node.keys, node.values)
                    if isinstance(key, ast.Constant) and key.value == "title"
                ]
            for candidate in filter(None, candidates):
                if forbidden.search(ast.unparse(candidate)):
                    violations.append(f"{rel}:{node.lineno}")
    assert violations == [], "unsafe runtime title fallback literals: " + ", ".join(violations)


def test_human_approval_is_row_locked_and_hash_bound():
    """[发布门三态拆分 2026-07-31] 旧断言写死整条 SQL 文本
    `"SELECT id FROM articles WHERE id = %s FOR UPDATE"`。三态包把这条 SELECT 的
    列表扩成 `SELECT id, article_human_review_status ...`(§4.3 五字段留痕的 before
    必须与行锁**同一次读**取回,否则并发签发会插进来写出 before==after 的假审计)
    —— 行锁本身没动,只是多取了一列,旧断言却红了:典型的**源码串断言重构误伤**。

    改成断言**要保的性质**而不是那一行字面:
      ① 锁的目标是 articles 这张表(不是别的表);
      ② 用的是 FOR UPDATE 行锁(不是 FOR SHARE / 无锁);
      ③ 锁**先于**任何 review/evidence 状态读取发生(顺序才是这条锁的意义)。
    这样列表怎么改都不误伤,而把 FOR UPDATE 删掉/换成 FOR SHARE/挪到读之后 → 仍然红。
    """
    source = (ROOT / "services/article_review_gate.py").read_text(encoding="utf-8")
    start = source.index("def set_human_review(")
    end = source.index("def refresh_article_review(", start)
    block = source[start:end]

    lock_stmt = re.search(
        r"SELECT\s+.*?\s+FROM\s+articles\s+WHERE\s+id\s*=\s*%s\s+FOR\s+UPDATE",
        block, re.IGNORECASE | re.DOTALL,
    )
    assert lock_stmt, "set_human_review 必须对 articles 取 FOR UPDATE 行锁"
    # 行锁必须排在快照/资格读取之前(顺序错了等于没锁)
    assert lock_stmt.start() < block.index("evaluate_publication_eligibility"), (
        "行锁必须先于资格评估,否则并发签发可绕过"
    )
    assert "human_approval_content_changed" in source
    assert "human_approval_evidence_changed" in source
    assert "ORDER BY id DESC" in source
