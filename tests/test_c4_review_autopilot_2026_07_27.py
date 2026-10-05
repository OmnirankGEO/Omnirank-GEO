"""工单 C-4 判别锁(2026-07-27)· 审核自动驾驶:用户不当审核员。

产品口径:hard 自动修复(无感)→ 汇总卡兜底 → soft 零打扰全落库。
底线锁(变异必转红):
  ① 残留 hard 仍拦发布(红线不放松);
  ② soft 后台记录一条不少;
  ③ 轮数硬上限:生成期自动 1 轮 / 一键累计 2 轮(防修复循环烧钱)。
既有人工审核流程(company_facts 推荐人审、set_human_review、mark-reviewed)一条不动
—— 由既有锁(test_p07_*)回归守护,本文件不重复。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_VIOLATING_PARA = "我们是行业第一的装饰公司,服务口碑有目共睹。"
_CLEAN_PARA = "我们在本地公开报道中被多次提及,口碑记录可自行查证。"
_BODY = f"# 本地装修怎么选\n\n{_VIOLATING_PARA}\n\n选择服务商时建议核对公开资质与交付记录。"


def _good_llm_calls() -> tuple[list[str], "callable"]:
    calls: list[str] = []

    async def _llm(prompt: str) -> str:
        calls.append(prompt)
        return _CLEAN_PARA

    return calls, _llm


def _bad_llm_calls() -> tuple[list[str], "callable"]:
    calls: list[str] = []

    async def _llm(prompt: str) -> str:
        calls.append(prompt)
        return _VIOLATING_PARA  # 永远修不好

    return calls, _llm


# ===========================================================================
# T1 · autopilot 服务行为
# ===========================================================================
def test_autopilot_repairs_hard_and_reports_clean():
    """可修 hard → 一轮修净:remaining 空、repaired 有记录、正文不再违规。

    变异(关自动修复/跳过修复)→ 本锁转红。
    """
    from services.article_review_autopilot import autopilot_repair_hard
    from writing.evidence_first_policy import evaluate_content_trust

    calls, llm = _good_llm_calls()
    outcome = asyncio.run(autopilot_repair_hard("", _BODY, llm_fn=llm))
    assert outcome["rounds_used"] == 1
    assert outcome["remaining"] == []
    assert len(outcome["repaired"]) >= 1
    assert "行业第一" not in outcome["content"]
    assert not evaluate_content_trust("", outcome["content"], evidence_mode="unknown").hard
    # 修复记录一条不少(可追溯)
    assert all({"code", "ok", "reason"} <= set(r) for r in outcome["records"])


def test_autopilot_honest_when_unfixable_and_round_capped():
    """修不好:remaining 保留(不谎报)、轮数硬上限 1 —— llm 只被烧一轮。

    变异(去掉/放宽轮数上限)→ 调用次数超一轮 → 本锁转红。
    """
    from services.article_review_autopilot import (
        MAX_AUTO_REPAIR_ROUNDS,
        autopilot_repair_hard,
    )

    assert MAX_AUTO_REPAIR_ROUNDS == 1
    calls, llm = _bad_llm_calls()
    outcome = asyncio.run(autopilot_repair_hard("", _BODY, llm_fn=llm))
    assert outcome["rounds_used"] == 1
    assert outcome["remaining"], "修不好必须诚实保留 remaining"
    assert outcome["repaired"] == [], "still_violating 不得谎报成已修复"
    assert "行业第一" in outcome["content"]  # still_violating → 原文一字不动
    assert len(calls) == 1, f"轮数上限 1:llm 只许被调 1 轮(实际 {len(calls)} 次)"
    # [span 级 AI 免费修复 2026-07-30] absolute_first_claim 现在路由到 span 级引擎,
    # 同一个"永远修不好"的替身被**更早、更准**的守卫拦下:命中串还在 →
    # repair_violation_text_remains(旧标签 still_violating 是重判阶段才给的)。
    # 契约完全不变(ok=False / 不谎报 / 正文一字不动),锁的是精确原因不是"随便一个红"。
    assert all(r["ok"] is False for r in outcome["records"]), "修不好不许记成 ok"
    assert all(r["reason"] == "repair_violation_text_remains" for r in outcome["records"])


def test_one_click_rounds_cap_is_two():
    """一键修复累计上限 2 轮(服务端硬闸口径)。变异(改上限)转红。"""
    from services.article_review_autopilot import (
        MAX_ONE_CLICK_ROUNDS,
        merge_auto_repair_state,
        one_click_rounds_left,
    )

    assert MAX_ONE_CLICK_ROUNDS == 2
    qw: dict = {}
    outcome = {"rounds_used": 1, "records": [], "repaired": [], "remaining": [], "at": "t"}
    qw = merge_auto_repair_state(qw, outcome, trigger="one_click")
    assert one_click_rounds_left(qw) == 1
    qw = merge_auto_repair_state(qw, outcome, trigger="one_click")
    assert one_click_rounds_left(qw) == 0
    # 自动轮不占一键额度(两条上限各查各的)
    qw2 = merge_auto_repair_state({}, outcome, trigger="auto")
    assert one_click_rounds_left(qw2) == 2


# ===========================================================================
# T1 · 保存链接线(生成 + rewrite 两条路径,自动修在 lineage 之前)
# ===========================================================================
def test_save_chains_wire_autopilot_before_lineage():
    src = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    save_body = src[src.index("async def _save_article"):src.index("async def rewrite_article")]
    rewrite_body = src[src.index("async def rewrite_article"):]
    for body, name in ((save_body, "_save_article"), (rewrite_body, "rewrite_article")):
        assert "apply_review_autopilot(" in body, f"{name} 未接自动修复"
        assert body.index("apply_review_autopilot(") < body.index("build_article_lineage("), (
            f"{name}:自动修复必须在 lineage/机审之前(修好才有'审核通过')"
        )
    helper = src[src.index("async def apply_review_autopilot"):src.index("def _normalize_article_title_and_h1")]
    assert "autopilot_repair_hard(" in helper
    assert "merge_auto_repair_state(" in helper
    # soft 记录一条不少:_save_article 仍写 evidence(soft)/evidence_precision
    assert "_qw['evidence_legal'] = _trust.warning_payload()" in save_body
    assert "_quality_warning['evidence'] = _trust.warning_payload()" in save_body
    assert "_quality_warning['evidence_precision'] = _precision.payload()" in save_body


def test_apply_review_autopilot_helper_behavior(monkeypatch):
    """保存链 helper 行为锁:hard 真被修掉、记录落 quality_warning、重评干净。

    语义变异(调了 autopilot 却丢弃修复结果/静默跳过)→ 本锁转红。
    """
    import services.article_review_autopilot as autopilot
    from writing.article_generator_service import apply_review_autopilot
    from writing.evidence_first_policy import evaluate_content_trust

    monkeypatch.setattr(autopilot, "default_repair_llm", lambda: _good_llm_calls()[1])
    article: dict = {}
    trust0 = evaluate_content_trust("", _BODY, evidence_mode="unknown")
    assert trust0.hard, "构造前提:原文必须先有 hard"
    content, trust = asyncio.run(apply_review_autopilot("", _BODY, {}, article, trust0))
    assert "行业第一" not in content, "修复结果必须真的替换进正文"
    assert not trust.hard, "修好后重评必须干净(后续机审才会 approved)"
    assert article["quality_warning"]["auto_repair"]["repaired_count"] >= 1
    assert article["quality_warning"]["auto_repair"]["auto_rounds"] == 1
    # 无 hard → 零调用直通(不烧钱)
    calls, llm = _good_llm_calls()
    monkeypatch.setattr(autopilot, "default_repair_llm", lambda: llm)
    content2, _ = asyncio.run(apply_review_autopilot("", content, {}, {}, trust))
    assert content2 == content and calls == []


# ===========================================================================
# 底线 ① · 残留 hard 仍拦发布(红线不放松)
# ===========================================================================
class _GateCursor:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self._row


def test_remaining_hard_is_still_reported_as_notice():
    """[§3A 降级 2026-08-01] 原断言"残留 hard → eligible=False 且不可覆盖"。

    🔴 Owner 拍板内容类不再阻塞发布 → 原断言若原样保留就与新口径直接打架。
    但本锁真正要守的东西没变:**自动修复跑完之后,没修掉的那条不许被吞掉**。
    所以断言从"仍然拦"改为"仍然报"——残留 hard 必须仍以 legal_hard 提示出现,
    且 reason/reason_class 分层不丢。变异(把 content_notices 清空 / 不再产出提示)
    → 本锁转红,判别力与原来等价。
    """
    import hashlib

    from services.article_review_gate import evaluate_publication_eligibility

    row = {
        "article_review_status": "blocked",
        "article_human_review_status": None,
        "article_review": {
            "decision": "blocked",
            "hard_failures": [{"code": "absolute_first_claim"}],
            "reviewed_content_hash": hashlib.sha256(_BODY.encode("utf-8")).hexdigest(),
            "reviewed_evidence_manifest_hash": "ev-hash",
        },
        "evidence_manifest_hash": "ev-hash",
        "style_family": "multi_brand_comparison",
        "style": "ranking_v2",
        "content": _BODY,
        "human_reviewed_by": None,
        "human_reviewed_at": None,
        "human_review_reason": None,
        "human_review_content_hash": None,
        "human_review_evidence_hash": None,
    }
    result = evaluate_publication_eligibility(1, cursor=_GateCursor(row))
    # [§3A] 翻面:提示级不再阻塞发布
    assert result["eligible"] is True
    assert result.get("overridable") is True
    # 🔴 实质:残留 hard 必须仍然报出来,一个字都不许被自动修复流程吞掉
    assert result["content_notice_classes"] == ["legal_hard"]
    assert result["reason"] == "blocked"
    assert result["reason_class"] == "legal_hard"
    assert result.get("reason_class") == "legal_hard"


# [P3 并入项 2 · 2026-08-01 · BACKLOG_C4_STATIC_ASSERTIONS_SPLIT]
# test_auto_repair_endpoint_refreshes_machine_review 已**原样搬到**
# tests/test_c4_static_assertions_2026_08_01.py:它只读 server.py 的源码文本,
# 却被本文件的 module 级 autouse fixture(_preload_server_module)拖着一起 ERROR。
# 搬走 = 无库环境也能跑到,不再静默积累死断言。语义未改。


# ===========================================================================
# 一键端点行为:mock-row 调真 handler(轮数硬闸 + 修复落盘)
# ===========================================================================
class _EpCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.updates = []

    def execute(self, sql, params=None):
        if "UPDATE articles" in str(sql):
            self.updates.append(params)

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _EpConn:
    def __init__(self, rows):
        self.cursor_obj = _EpCursor(rows)

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture(scope="module", autouse=True)
def _preload_server_module():
    """行为锁要调真 handler:server 在本文件任何测试前导入一次(同 C-2 模式)。"""
    import server  # noqa: F401
    yield


def test_endpoint_auto_repair_behavior_repairs_and_caps(monkeypatch):
    import server
    import db.diagnosis_db as ddb
    import services.article_review_autopilot as autopilot
    import services.article_review_gate as gate

    row = {
        "title": "本地装修怎么选",
        "content": _BODY,
        "evidence_pack": None,
        "brand_fact_snapshot": None,
        "quality_warning": {"evidence_legal": {"hard": [{"code": "absolute_first_claim"}]}},
    }
    conn = _EpConn([dict(row)])
    conn2 = _EpConn([])
    conns = [conn, conn2]
    monkeypatch.setattr(ddb, "get_connection", lambda: conns.pop(0) if conns else _EpConn([]))
    monkeypatch.setattr(server, "_require_article_access", lambda *a, **k: None)
    monkeypatch.setattr(autopilot, "default_repair_llm", lambda: _good_llm_calls()[1])
    monkeypatch.setattr(
        gate, "refresh_article_review",
        lambda article_id, cursor=None: {"article_review_status": "approved"},
    )

    resp = asyncio.run(server.api_auto_repair_article(5, object()))
    assert resp["success"] is True
    assert resp["rounds_used"] == 1
    assert resp["repaired_count"] >= 1
    assert resp["remaining"] == []
    assert resp["article_review_status"] == "approved"
    assert conn2.cursor_obj.updates, "修复结果必须落盘"
    saved_content = conn2.cursor_obj.updates[0][0]
    assert "行业第一" not in saved_content

    # 轮数用尽:诚实拒绝,不烧第 3 轮
    exhausted = dict(row)
    exhausted["quality_warning"] = {
        "evidence_legal": {"hard": [{"code": "absolute_first_claim"}]},
        "auto_repair": {"manual_rounds": 2},
    }
    conns.append(_EpConn([exhausted]))
    resp2 = asyncio.run(server.api_auto_repair_article(5, object()))
    assert resp2["success"] is False
    assert resp2["rounds_left"] == 0
    assert resp2.get("code") == "AUTO_REPAIR_ROUNDS_EXHAUSTED"


# ===========================================================================
# 底线 ② · soft 零打扰但记录一条不少
# ===========================================================================
def test_soft_records_survive_aggregation_untouched():
    """聚合视图(后台/质量记录入口的数据源)对 soft 的计数与逐条记录完全保真。

    变异(soft 丢记录)→ 本锁转红。
    """
    from services.article_findings_aggregate import aggregate_article_findings

    soft = [
        {"code": "unsourced_outcome_number", "severity": "soft", "message": "m", "matched_text": "提升23%"},
        {"code": "unsourced_outcome_number", "severity": "soft", "message": "m", "matched_text": ""},
        {"code": "anonymous_authority", "severity": "soft", "message": "m2"},
    ]
    qw = {"evidence": {"soft": soft}}
    cards = aggregate_article_findings(qw)
    assert sum(c["count"] for c in cards) == len(soft), "soft 记录不许丢一条"
    by_code = {c["code"]: c for c in cards}
    assert by_code["unsourced_outcome_number"]["count"] == 2
    assert len(by_code["unsourced_outcome_number"]["spans"]) == 2


# [P3 并入项 2 · 2026-08-01 · BACKLOG_C4_STATIC_ASSERTIONS_SPLIT]
# 另外三条**纯静态**断言(test_frontend_soft_is_background_reference_not_confirmation /
# test_frontend_hard_summary_card_replaces_flat_cards /
# test_admin_quality_panel_hard_only_uses_real_path)已原样搬到
# tests/test_c4_static_assertions_2026_08_01.py,连同 _TSX 常量。语义未改。
# 🔴 下面这条**没有**搬:它 `import server`,不是静态断言,搬过去会把
#    "任何环境都能跑" 这个约束当场破掉。


def test_existing_human_review_flows_still_reachable():
    """🔴 [发布门三态拆分 2026-07-31 · 工单 §1.1] 源码串断言 → 行为断言。

    旧断言 `assert "evidence_advisory_continue" in server_src`(旧 :385)是**读源码找
    子串**:改个变量名就红(重构误伤)、把参数删了只要注释里还留着这串就绿(换皮绕过)
    —— 双向脆。改成断言**真实可调用对象的签名**:参数真的在活路由上。

    同时订正本测试的名字与承诺:brand_story 推荐人审在本单里**语义确实变了**
    (从阻断发布 → 不阻断,§4.3),所以不能再叫 "untouched"。它必须保住的是
    "推荐人审这条路还在、reason 串不变(set_human_review 的 skip 守卫按它判)"。
    """
    import inspect

    import server

    sig = inspect.signature(server.api_mark_topic_reviewed)
    assert "evidence_advisory_continue" in sig.parameters, "人工确认继续的入参必须在活路由上"
    assert sig.parameters["evidence_advisory_continue"].default is False, "默认不得是 True"

    from services.article_review_gate import HUMAN_DECISIONS

    assert "skipped" in HUMAN_DECISIONS, "明示跳过必须仍是合法决策"


# ===========================================================================
# §5 验收 · 生产同款深档文:用户视角从"105 处需确认"变为自动修复/一张汇总卡
# ===========================================================================
def _deep_article_with_hards() -> str:
    hard1 = "我们是行业第一的装饰服务商,交付能力毋庸置疑。"
    hard2 = "论施工工艺,我们始终是本地最好的团队。"
    filler = (
        "从需求梳理到方案沟通,再到交付后的回访,值得关注的是信息是否透明、"
        "口径是否一致、承诺是否落在合同里;把边界问清楚,后续合作才稳。\n\n"
    )
    parts = ["# 本地全屋定制哪家好?12 家服务商深度评测\n"]
    for i in range(1, 13):
        parts.append(f"## 第{i}名 甲{i}装饰公司\n\n甲{i}装饰公司主打本地交付,流程分四段各有专人对接。\n")
    parts.append(hard1 + "\n")
    parts.append(filler * 120)
    parts.append(hard2 + "\n")
    parts.append(filler * 120)
    return "\n".join(parts)


def test_deep_article_autopilot_end_state(monkeypatch):
    """16k 深档 + 多处 hard:一轮自动修复全清 → 用户零操作即"审核通过"形态;
    修复记录一条不少;soft 聚合(后台质量记录数据源)保真。"""
    from services.article_review_autopilot import autopilot_repair_hard
    from services.article_findings_aggregate import aggregate_article_findings
    from writing.evidence_first_policy import evaluate_content_trust

    body = _deep_article_with_hards()
    assert len(body) >= 16000
    trust0 = evaluate_content_trust("", body, evidence_mode="unknown")
    assert len(trust0.hard) >= 2, "构造前提:深档文含多处 hard"

    calls, llm = _good_llm_calls()
    outcome = asyncio.run(autopilot_repair_hard("", body, llm_fn=llm))
    assert outcome["rounds_used"] == 1
    assert outcome["remaining"] == [], "可修 hard 必须一轮修净(用户零操作)"
    assert len(outcome["repaired"]) >= 2
    trust1 = evaluate_content_trust("", outcome["content"], evidence_mode="unknown")
    assert not trust1.hard

    # soft 全量落库口径:聚合记录数量与判定产出一致(零打扰不等于丢记录)
    qw = {"evidence": trust1.warning_payload()}
    cards = aggregate_article_findings(qw)
    assert sum(c["count"] for c in cards) == len(trust1.soft)


# ===========================================================================
# 返工(二审) · 保存链接入点行为锁:mock 走真保存流程,断言**落库的 content**
# 是修复后版本 —— 函数层锁不背调用层的险("调用点丢弃返回值"变异必转红)。
# ===========================================================================
class _PatternCursor:
    """按 SQL 特征应答的宽容游标;捕获 INSERT INTO articles 的参数。"""

    def __init__(self, rows_by_marker, sink):
        self._rows = rows_by_marker
        self._sink = sink
        self._pending = None

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        if "INSERT INTO articles" in text:
            self._sink.append(params)
            self._pending = {"id": 777}
            return
        for marker, row in self._rows:
            if marker in text:
                self._pending = dict(row) if isinstance(row, dict) else row
                return
        if "RETURNING" in text and text.startswith("UPDATE topics"):
            self._pending = {"id": 11}
            return
        self._pending = None

    def executemany(self, sql, seq=None):
        pass

    def fetchone(self):
        row, self._pending = self._pending, None
        return row

    def fetchall(self):
        return []

    def close(self):
        pass


class _PatternConn:
    def __init__(self, rows_by_marker, sink):
        self._rows = rows_by_marker
        self._sink = sink
        self.autocommit = False

    def cursor(self, *a, **k):
        return _PatternCursor(self._rows, self._sink)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _make_service():
    from writing.article_generator_service import ArticleGeneratorService

    service = ArticleGeneratorService.__new__(ArticleGeneratorService)
    service.quote_id = 9
    service.brand_name = "测试品牌"
    service.industry = "本地装修"
    return service


def _wire_fake_db(monkeypatch, rows_by_marker, sink):
    import db.connection as dbc
    import db.diagnosis_db as ddb

    factory = lambda *a, **k: _PatternConn(rows_by_marker, sink)  # noqa: E731
    monkeypatch.setattr(ddb, "get_connection", factory)
    monkeypatch.setattr(dbc, "get_connection", factory)


def test_save_article_persists_autopilot_repaired_content(monkeypatch):
    """_save_article 真保存流程:落库 content 必须是自动修复后的版本。

    变异「调用点丢弃 apply_review_autopilot 返回值(_ignored =)」→ 本锁转红。
    """
    import services.article_review_autopilot as autopilot
    import writing.article_generator_service as svc_mod

    monkeypatch.setattr(autopilot, "default_repair_llm", lambda: _good_llm_calls()[1])
    monkeypatch.setattr(svc_mod, "_copy_article_distilled_lineage", lambda *a, **k: None)
    inserts: list = []
    _wire_fake_db(monkeypatch, [
        ("SELECT q.brand_id, b.name AS brand_name", None),
        ("SELECT id FROM topics WHERE id=%s FOR UPDATE", {"id": 11}),
        ("COALESCE(MAX(version),0)", {"max_version": 0}),
        ("SELECT COALESCE(q.owner_user_id", None),
        ("SELECT brand_id FROM quotes", None),
    ], inserts)

    service = _make_service()
    monkeypatch.setattr(
        service, "_freeze_topic_delivery_options",
        lambda topic: topic.update(
            {"_effective_add_images": False, "_effective_add_contact": False}
        ),
        raising=False,
    )
    topic = {
        "id": 11,
        "publication_profile": "standard",
        "evidence_mode": "unknown",
        "style_code": "buying_guide",
        "title": "本地装修怎么选",
        "keyword": "本地装修",
    }
    article = {
        "topic_id": 11,
        "title": "本地装修怎么选",
        "content": _BODY,
        "word_count": len(_BODY),
        "style": "buying_guide",
        "publication_profile": "standard",
    }
    article_id = asyncio.run(service._save_article(topic, article))
    assert article_id == 777
    assert inserts, "必须真的走到 INSERT"
    saved_content = inserts[0][3]
    assert "行业第一" not in saved_content, "落库正文必须是修复后的版本(不是 helper 返回了什么)"
    assert _CLEAN_PARA[:12] in saved_content
    saved_qw = inserts[0][7]
    qw = getattr(saved_qw, "adapted", saved_qw)
    assert qw and qw.get("auto_repair", {}).get("repaired_count", 0) >= 1
    assert not qw.get("needs_legal_fix", False)


def test_rewrite_article_persists_autopilot_repaired_content(monkeypatch):
    """rewrite_article 真保存流程:落库 content 必须是自动修复后的版本(同上变异口径)。"""
    import services.article_review_autopilot as autopilot
    import writing.article_generator_service as svc_mod

    monkeypatch.setattr(autopilot, "default_repair_llm", lambda: _good_llm_calls()[1])
    monkeypatch.setattr(svc_mod, "_copy_article_distilled_lineage", lambda *a, **k: None)
    monkeypatch.setattr(
        svc_mod, "get_llm_config", lambda *a, **k: ("http://x", "key", "model", None),
        raising=False,
    )
    inserts: list = []
    topic_row = {
        "id": 11,
        "quote_id": 9,
        "optimized_title": "本地装修怎么选",
        "original_keyword": "本地装修",
        "article_style": "buying_guide",
        "style_code": "buying_guide",
        "user_choice": "implementation_guide",
        "publication_profile": "standard",
        "evidence_mode": "unknown",
        "status": "completed",
    }
    latest_row = {
        "version": 1,
        "publication_profile": "standard",
        "style_version": None,
        "generation_request_snapshot": None,
        "content": "旧正文",
        "quality_warning": None,
        "evidence_pack": None,
        "brand_fact_snapshot": None,
        "brand_id": None,
    }
    _wire_fake_db(monkeypatch, [
        ("SELECT * FROM topics WHERE id=%s", topic_row),
        ("FROM articles a LEFT JOIN quotes q", latest_row),
        ("SELECT q.brand_id, b.name AS brand_name", None),
        ("SELECT id FROM topics WHERE id=%s FOR UPDATE", {"id": 11}),
        ("COALESCE(MAX(version),0)", {"max_version": 1}),
        ("SELECT COALESCE(q.owner_user_id", None),
        ("SELECT brand_id FROM quotes", None),
    ], inserts)

    service = _make_service()

    async def _fake_generate(topic, api_url, api_key, model):
        return {"title": "本地装修怎么选", "content": _BODY}

    monkeypatch.setattr(service, "_generate_validated_with_rewrite_once", _fake_generate, raising=False)
    monkeypatch.setattr(service, "_is_invalid_content", lambda s: False, raising=False)
    monkeypatch.setattr(
        service, "_freeze_rewrite_delivery_options",
        lambda topic, latest: topic.update(
            {"_effective_add_images": False, "_effective_add_contact": False}
        ),
        raising=False,
    )

    result = asyncio.run(service.rewrite_article(11))
    assert not (isinstance(result, dict) and result.get("error")), f"rewrite 保存失败: {result}"
    assert inserts, "必须真的走到 INSERT"
    saved_content = inserts[0][3]
    assert "行业第一" not in saved_content, "rewrite 落库正文必须是修复后的版本"
    assert _CLEAN_PARA[:12] in saved_content
