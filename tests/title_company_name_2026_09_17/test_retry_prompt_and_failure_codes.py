# -*- coding: utf-8 -*-
"""WO_232 §3.2/§3.3 · 重试 prompt 把拒绝原因喂回去 + 失败码分开。

§3.2 上一轮的重试和第一轮用的是同一套约束,模型当然又缩写一次 ——
     日志里「5/7 槽 → 2/7 槽」正是这么来的。把**复核要的那一串**直接写进 prompt。
§3.3 「复核拒绝」和「AI 不可用」原来共用一个码。真客户看到「AI 没能生成」,
     而 llm_call_log 写作线全 success —— 展示与真因不符,用户只能瞎点重试。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from writing import title_ai_only as tao  # noqa: E402

BRAND = "广东星衍朗科技有限公司"
KEYWORD = "广东星衍朗科技有限公司怎么样"
CORE = "广东星衍朗科技有限公司"


# ── §3.2 重试 prompt ────────────────────────────────────────────────

def _run_retry(monkeypatch, model_reply, sent_prompts):
    """跑真 `ai_retry_titles`,只把**出网那一跳**换成桩。

    🔴 桩只掉"调模型"这一层。`_build_retry_prompt` / `_parse_retry_payload` /
       复核过滤都跑真的 —— 桩掉的那层要是正好包住被测逻辑,判据就全瞎了
       (本仓 stubbing-the-layer-that-is-broken-makes-every-criterion-blind)。
    """
    monkeypatch.setattr(tao, "_retry_model_attempts",
                        lambda: [("http://stub", "k", "stub-model")])

    async def _fake_call(api_url, api_key, model, prompt, *, timeout=90.0):
        sent_prompts.append(prompt)
        return dict(model_reply)

    monkeypatch.setattr(tao, "_call_one_model", _fake_call)
    rejected = {}
    requests = [tao.TitleSlotRequest(key=0, keyword=KEYWORD, article_style="证据型问答")]
    got = asyncio.run(tao.ai_retry_titles(
        requests, brand_name=BRAND, industry="光伏", rejected_out=rejected))
    return got, rejected


def test_the_retry_prompt_carries_the_keyword_core(monkeypatch):
    """🔴 §3.2:真正发出去的那份 prompt 里带着复核要的那一串。

    断言的是**桩收到的 prompt**,不是我另写一份字符串去比
    —— 判据要取被服务方看到的东西。
    """
    sent = []
    _run_retry(monkeypatch, {"0": "广东星衍朗科技有限公司怎么样?光伏服务解析"}, sent)
    assert sent, "重试根本没发出去 —— 下面的断言无从谈起"
    prompt = sent[0]
    assert CORE in prompt, "prompt 里没有关键词核:\n%s" % prompt[:400]
    assert "must_contain" in prompt, "没有把「必须原样包含」这条约束喂回去"
    assert "原样包含" in prompt, "硬约束文案缺失"


def test_the_core_in_the_prompt_is_the_same_string_the_checker_uses(monkeypatch):
    """🔴 喂给模型的那一串,必须和复核器拿去比对的**同一个函数**产出。

    我另写一个"差不多"的核,模型照着写、复核照样拒 ——
    两个来源迟早会分家(本仓:判据与被判对象必须同源)。
    """
    import json
    import re

    from writing.title_keyword_alignment import title_anchor_from_purchased_keyword

    sent = []
    _run_retry(monkeypatch, {"0": "广东星衍朗科技有限公司怎么样?光伏服务解析"}, sent)

    # 🔴 断言**逐字相等**,不是"包含"。`in` 对"把整条关键词原样塞进去"这种
    #    改法天生没分辨力(关键词核是它的子串,包含判据照样绿)——
    #    本仓 existence-assertions-are-blind-to-a-widened-window。
    block = re.search(r"【待生成】\s*(\[[\s\S]*\])", sent[0])
    assert block, "prompt 里找不到【待生成】那段 JSON,下面的断言无从谈起:\n%s" % sent[0][-400:]
    items = json.loads(block.group(1))
    assert len(items) == 1, items
    assert items[0]["must_contain"] == title_anchor_from_purchased_keyword(KEYWORD), (
        "喂给模型的核与复核器用的核不是同一个:%r vs %r"
        % (items[0]["must_contain"], title_anchor_from_purchased_keyword(KEYWORD)))


# ── §3.3 失败码 ────────────────────────────────────────────────────

def test_a_rejected_title_is_recorded_as_alignment_rejected(monkeypatch):
    """模型**给了**标题、被复核拒掉 ⇒ 进 alignment_rejected,不算 AI 不可用。"""
    sent = []
    got, rejected = _run_retry(
        monkeypatch, {"0": "光伏一站式服务怎么选?行业解析"}, sent)
    assert got == {}, got
    assert rejected.get(0), "模型给了标题却没记成「复核拒绝」—— 上层就会说成 AI 不可用"


def test_a_silent_model_is_not_recorded_as_alignment_rejected(monkeypatch):
    """反臂:模型**什么都没给** ⇒ 不许记成复核拒绝(那才是真的 AI 不可用)。

    没有这一条,一个"永远记一笔"的实现也能让上一条绿,
    于是两个失败码又会合成同一个。
    """
    sent = []
    got, rejected = _run_retry(monkeypatch, {}, sent)
    assert got == {} and not rejected, rejected


def _report_for(monkeypatch, ladder, pending_reason):
    """跑真 `_resolve_pending_titles`,只把梯子换成桩,看它挑哪个码。"""
    from writing.keyword_topic_generator import KeywordTopicGenerator
    import writing.keyword_topic_generator as ktg

    gen = KeywordTopicGenerator(keywords=[], brand_name=BRAND, industry="光伏")

    async def _fake_ladder(requests, **kw):
        return ladder

    monkeypatch.setattr(ktg, "resolve_titles_ai_only", _fake_ladder, raising=False)
    monkeypatch.setattr(tao, "resolve_titles_ai_only", _fake_ladder, raising=False)
    topics = [{
        "keyword_id": 3170, "original_keyword": KEYWORD, "slot_index": 0,
        "article_style": "证据型问答", "optimized_title": "",
        "title_pending_reason": pending_reason,
    }]
    asyncio.run(gen._resolve_pending_titles(topics))
    return gen.title_failure_report


def test_alignment_rejection_gets_its_own_code(monkeypatch):
    """🔴 §3.3:复核拒的那一批,码是 TITLE_ALIGNMENT_REJECTED,文案说真话。"""
    ladder = tao.TitleLadderResult(
        failed_keys=[0], retry_attempted=True,
        alignment_rejected={0: ["光伏一站式服务怎么选"]})
    report = _report_for(monkeypatch, ladder, "conflicting_business_qualifier")
    assert report["error_code"] == tao.TITLE_ALIGNMENT_FAILURE_CODE, report
    assert "AI" not in (report["user_message"] or ""), (
        "文案还在说 AI —— 真因是我们自己拒的:%r" % report["user_message"])
    assert "关键词" in report["user_message"], report["user_message"]
    assert report["alignment_rejected"] == 1, report


def test_a_real_ai_outage_still_gets_the_old_code(monkeypatch):
    """反臂:模型真的没给东西 ⇒ 仍是 TITLE_AI_UNAVAILABLE。

    只钉新码不钉旧码的话,把所有失败一律改判成"复核拒绝"也会绿
    —— 那对真的 AI 故障就成了误导。
    """
    ladder = tao.TitleLadderResult(failed_keys=[0], retry_attempted=True)
    report = _report_for(monkeypatch, ladder, "")
    assert report["error_code"] == tao.TITLE_FAILURE_CODE, report
    assert report["user_message"] == tao.TITLE_FAILURE_USER_MESSAGE, report
    assert report["alignment_rejected"] == 0, report


def test_the_two_codes_are_not_the_same_string():
    assert tao.TITLE_ALIGNMENT_FAILURE_CODE != tao.TITLE_FAILURE_CODE


def test_the_failed_slot_carries_its_own_kind(monkeypatch):
    """每个失败槽自带 failure_kind —— 整批一个码会把混合批次说成一种病。"""
    ladder = tao.TitleLadderResult(
        failed_keys=[0], retry_attempted=True, alignment_rejected={0: ["x"]})
    report = _report_for(monkeypatch, ladder, "conflicting_business_qualifier")
    assert report["failed_slots"][0]["failure_kind"] == "alignment_rejected", report
    assert report["failed_slots"][0]["alignment_reason"] == "conflicting_business_qualifier"


@pytest.mark.parametrize("retries,expect", [(0, False), (1, True)])
def test_the_user_message_mentions_retries_only_when_it_retried(retries, expect):
    msg = tao.alignment_failure_user_message(CORE, retries)
    assert ("重试" in msg.split("。")[0]) is expect, msg
    assert CORE in msg, msg
    assert "重新生成" in msg and "编辑" in msg, "文案没给动作(元指令「提示二选一」)"


def test_an_empty_title_is_not_called_an_alignment_rejection(monkeypatch):
    """🔴 模型什么都没给(reason=empty_title)⇒ 仍是 AI 不可用。

    这一条是被既有判据 G1(`test_title_ai_only_2026_08_17` 的「LLM 全死」)
    打出来的:我第一版写成「pending_reason 非空即复核拒绝」,于是 AI 全死
    被说成「标题没保留完整关键词」—— 用户照着去编辑标题,而真问题是 key 没配。
    """
    ladder = tao.TitleLadderResult(failed_keys=[0], retry_attempted=True)
    report = _report_for(monkeypatch, ladder, "empty_title")
    assert report["error_code"] == tao.TITLE_FAILURE_CODE, report
    assert report["failed_slots"][0]["failure_kind"] == "ai_unavailable", report


def test_every_reason_the_checker_can_emit_is_classified():
    """🔴 分母腿:复核器能给出的**每一个** reason 都被归了类。

    机械枚举 `TitleKeywordAlignment(False, "...")` 的全部字面量,
    而不是我凭记忆列一份 —— 漏一个,那种失败就会被默默归成"AI 不可用",
    读数和"真的没有这种失败"一模一样。
    """
    import ast
    import io

    from writing.title_keyword_alignment import (
        ALIGNMENT_REJECTION_REASONS, NOT_GENERATED_REASONS,
    )

    src = io.open(REPO / "writing" / "title_keyword_alignment.py", encoding="utf-8").read()
    emitted = set()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "TitleKeywordAlignment" and len(node.args) >= 2):
            aligned, reason = node.args[0], node.args[1]
            if (isinstance(aligned, ast.Constant) and aligned.value is False
                    and isinstance(reason, ast.Constant)):
                emitted.add(reason.value)
    assert emitted, "一个 reason 都没扫到 —— 扫描器坏了,下面的比对不可解读"
    classified = ALIGNMENT_REJECTION_REASONS | NOT_GENERATED_REASONS
    assert emitted <= classified, "没归类的 reason:%s" % sorted(emitted - classified)
    assert ALIGNMENT_REJECTION_REASONS & NOT_GENERATED_REASONS == set(), "两类重叠了"
