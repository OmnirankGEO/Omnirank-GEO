# -*- coding: utf-8 -*-
"""R4 · 两处核心接线的**行为级锁**(返修单 v3 §A-R4 · Review 变异实证 Y1a/Y2a)。

为什么必须行为级:源码字符串锁只防「删文本」(第七例形态),不防
「字面在语义死」(第八例形态)——`if False and _adv_block:` 让 153 条字符串锁
全绿而功能死透;两处调用点又都包在吞一切异常的 try 里,运行时断裂零告警。

本文件两把锁的判据全部打在**运行产物**上:
  · Y1a:走真 `_generate_single`(生产路径 `sim_overrides=None`),从 **mock LLM
    客户端实际收到的 messages** 里断言主优势块在场 —— 注入点被任何方式短路
    (删行 / if False / 异常吞掉)都转红;
  · Y2a:走真 `_save_article` 保存链到 INSERT 参数,断言落库 `quality_warning`
    带 `post_sanitize_delta` 且其判定基于**清洗后**文本 —— rejudge 被短路则
    delta 缺失,转红。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.flywheel_integration.test_w3_simulation import (  # noqa: E402
    _FakeAsyncOpenAI,
    _install_mocks,
)
from tests.test_c4_review_autopilot_2026_07_27 import (  # noqa: E402
    _make_service,
    _wire_fake_db,
)
from writing.post_sanitize_rejudge import DELTA_KEY  # noqa: E402

SNAPSHOT = {
    "version": "brand-fact-v1",
    "brand_name": "演示品牌",
    "claims": [
        {"claim_id": "BF-001", "field": "delivery_capability",
         "value": "观光电梯项目平均交付周期 45 天，支持 24 米以内定制",
         "provenance": "customer_provided", "verification_status": "customer_asserted"},
        {"claim_id": "BF-002", "field": "after_sales",
         "value": "深圳本地 2 小时上门响应，质保 24 个月",
         "provenance": "customer_provided", "verification_status": "customer_asserted"},
    ],
}


# ---------------------------------------------------------------- Y1a · D6-A
def test_primary_advantage_block_reaches_llm_messages(monkeypatch):
    """🔴 [Y1a 行为锁] 生产路径(`sim_overrides=None`)下,mock LLM 客户端
    实际收到的 system message 必须含【本篇主优势】块。"""
    captured: list[dict] = []

    class _RecordingClient(_FakeAsyncOpenAI):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            create = self.chat.completions.create

            async def _record(**call_kwargs):
                captured.append(call_kwargs)
                return await create(**call_kwargs)

            self.chat.completions.create = _record

    import openai

    _install_mocks(monkeypatch)
    monkeypatch.setattr(openai, "AsyncOpenAI", _RecordingClient)
    # 🔴 数据供给层 mock(与 distiller/知识库 mock 同层,生成链本身保持真):
    # 生成器在流程中会用 client_materials **重建并覆盖** topic 的
    # brand_fact_snapshot(:2001);假库无客户材料 → 重建出 claims=[] →
    # D6-A 零候选。这里把快照构建器钉到测试快照,让注入条件真实成立。
    import writing.brand_fact_snapshot as bfs

    monkeypatch.setattr(bfs, "build_brand_fact_snapshot", lambda **kwargs: SNAPSHOT)
    # 证据研究旁路:预置足量 pack,让主链不去发真检索(与 D6-A 注入无关的稳定化)
    import writing.evidence_research as er

    async def _no_research(**kwargs):
        raise AssertionError("不应触发研究 —— pack 已足量")

    monkeypatch.setattr(er, "collect_evidence_pack", _no_research)

    from writing.article_generator_service import ArticleGeneratorService

    svc = ArticleGeneratorService(101, "演示品牌", "观光电梯")
    topic = {
        "id": None, "title": "深圳观光电梯定制哪家交付周期快",
        "keyword": "观光电梯 定制",
        "style_code": "buying_guide", "user_choice": "auto",
        "_trust_legacy_style": True,
        "brand_fact_snapshot": SNAPSHOT,
        "_evidence_pack": {"version": "v1", "items": [
            {"evidence_id": f"EV-00{i}", "relationship": "support",
             "verification_status": "search_result", "title": f"素材{i}",
             "url": f"https://e.com/{i}", "publisher": "中国电梯",
             "published_at": "2025-03", "claim": "交付周期", "scope": "",
             "excerpt": "……"} for i in (1, 2, 3)
        ], "limitations": []},
    }
    article = asyncio.run(svc._generate_single(
        topic, "https://x/v1/chat/completions", "sk-test", "test-model",
    ))
    assert article.get("content"), "真调用链没产出正文 —— harness 断了"
    assert captured, "mock LLM 没收到任何调用"
    system_msg = next(
        m["content"] for m in captured[0]["messages"] if m["role"] == "system"
    )
    assert "【本篇主优势" in system_msg, (
        "发给模型的 system prompt 里没有主优势块 —— 注入点被短路(Y1a 形态)"
    )
    # 行为一致性:lineage 留痕必须与真实注入同真值
    payload = topic.get("_primary_advantage") or {}
    assert payload.get("injected") is True
    assert payload.get("planned_primary_advantage"), "留痕缺计划主优势"

    # 反向对照(必须不命中):没有客户事实候选时,不注入空块
    captured.clear()
    monkeypatch.setattr(
        bfs, "build_brand_fact_snapshot", lambda **kwargs: {"claims": []},
    )
    topic2 = dict(topic, brand_fact_snapshot={"claims": []})
    topic2.pop("_primary_advantage", None)
    asyncio.run(svc._generate_single(
        topic2, "https://x/v1/chat/completions", "sk-test", "test-model",
    ))
    system_msg2 = next(
        m["content"] for m in captured[0]["messages"] if m["role"] == "system"
    )
    assert "【本篇主优势" not in system_msg2, "零候选也注入了块 —— 空块污染 prompt"


# ---------------------------------------------------------------- Y2a · rejudge
_SELF_DISCLOSED_BODY = (
    "# 观光电梯怎么选\n\n"
    "先说结论:看交付与质保。\n\n"
    "## 交付能力\n\n"
    "以下企业相关信息来自企业提交资料。质保 24 个月,交付 32 台。\n\n"
    "## 售后\n\n本地服务网络成熟,响应及时。\n"
)


def _run_save(monkeypatch, body: str):
    import writing.article_generator_service as svc_mod

    async def _noop(title, content, topic, article, trust, target_entity=""):  # [R5.1] 契约同签名
        return content, trust

    monkeypatch.setattr(svc_mod, "apply_review_autopilot", _noop)
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
        "id": 11, "publication_profile": "standard", "evidence_mode": "unknown",
        "style_code": "buying_guide", "title": "观光电梯怎么选", "keyword": "观光电梯",
    }
    article = {
        "topic_id": 11, "title": "观光电梯怎么选", "content": body,
        "word_count": len(body), "style": "buying_guide",
        "publication_profile": "standard",
    }
    article_id = asyncio.run(service._save_article(topic, article))
    assert article_id == 777 and inserts, "没走到 INSERT INTO articles"
    saved_content = inserts[0][3]
    saved_qw = inserts[0][7]
    return saved_content, getattr(saved_qw, "adapted", saved_qw)


def test_rejudge_delta_lands_in_saved_quality_warning(monkeypatch):
    """🔴 [Y2a 行为锁] 真保存链落库的 quality_warning 必须带
    `post_sanitize_delta`,且判定对象是**清洗后**正文:
    清洗器删掉的自曝声明,不得再出现在重跑后的 evidence 判定里。"""
    import json

    saved_content, saved_qw = _run_save(monkeypatch, _SELF_DISCLOSED_BODY)
    qw = saved_qw if isinstance(saved_qw, dict) else json.loads(saved_qw)
    assert DELTA_KEY in qw, (
        "落库 quality_warning 没有 post_sanitize_delta —— rejudge 被短路(Y2a 形态)"
    )
    delta = qw[DELTA_KEY]
    assert delta.get("where") == "_save_article"
    # 判定基于清洗后文本:自曝声明已被清洗器删除 →
    #   ① 落库正文不含它;② 重跑后的 evidence 判定不得再报 self_disclosed_source
    assert "企业提交资料" not in saved_content, "清洗器没删自曝 —— 夹具或清洗链断了"
    rejudged_codes = [
        f.get("code") for f in (qw.get("evidence") or {}).get("soft") or []
    ]
    assert "self_disclosed_source" not in rejudged_codes, (
        "重跑判定还在报清洗前的自曝 —— rejudge 读的不是清洗后文本"
    )
    # 元判据:清洗前的正文**确实**会触发该信号(证明上面反向断言非空)
    from writing.evidence_first_policy import evaluate_content_trust

    raw_codes = [f.code for f in evaluate_content_trust(
        "观光电梯怎么选", _SELF_DISCLOSED_BODY, evidence_mode="unknown",
    ).soft]
    assert "self_disclosed_source" in raw_codes, "夹具没触发自曝信号,断言是空的"
