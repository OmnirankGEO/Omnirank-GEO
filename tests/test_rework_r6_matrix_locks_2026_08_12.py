# -*- coding: utf-8 -*-
"""R6 专项锁(WO_ARTREC_REWORK_R6 · 判据换代:例句验 → 矩阵验)。

🔴 本轮根因:R4 漏「混合句」,R5 漏「同义动词」,两次都是手挑例句。
本文件 §3a 用**笛卡尔积生成**夹具(主语自指性 × 关系动词 × 有无硬事实),
每格都有用例;§3b 判据打**组装后送 LLM 的完整 messages**,不是源码字符串。

变异对照(runner):
  RD1(自指词族砍回只认「本文」)→ 矩阵自指格必红;
  RC1(第三方判别退回 is_self≡True)→ 矩阵第三方格必红;
  RD3(evidence_first 恢复「换事实,或不写这条」)→ §3b messages 锁必红;
  RC2(免责整句删)/ RB3(空泛为准保护拆除)→ §4 矩阵必红。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from writing.content_cleaner import _blend_visible_source_labels as clean  # noqa: E402

# ================================================================ §3a · §1 矩阵
SELF_SUBJECTS = ("本文", "本篇", "本报告", "本内容", "本评测", "本稿", "笔者", "作者", "我们")
THIRD_SUBJECTS = ("观山电梯", "万汇广场")
#: 动词 → 陈述式模板(关系候选形态;判别位是主语,与动词无关)
VERB_TEMPLATES = {
    "委托撰写": "{s}由栖舍装修委托撰写。",
    "委托推广": "{s}由栖舍装修委托推广。",
    "委托":     "{s}受栖舍装修委托。",
    "付费赞助": "{s}由栖舍装修付费赞助。",
    "赞助":     "{s}由栖舍装修赞助。",
    "合作":     "{s}与栖舍装修合作。",
    "授权经销": "{s}由栖舍装修授权经销。",
    "采购":     "{s}向栖舍装修采购。",
    "联合":     "{s}与栖舍装修联合。",
}
_FACT_TAIL = "双方已完成三台设备交付。"
_REL_LEAK = ("委托", "赞助", "合作", "授权经销", "采购", "联合")


def _matrix_cases():
    for verb, tpl in VERB_TEMPLATES.items():
        for subj in SELF_SUBJECTS:
            base = tpl.format(s=subj)
            yield ("self", subj, verb, base, False)
            yield ("self", subj, verb, base[:-1] + "，" + _FACT_TAIL, True)
        for subj in THIRD_SUBJECTS:
            base = tpl.format(s=subj)
            yield ("third", subj, verb, base, False)
            yield ("third", subj, verb, base[:-1] + "，" + _FACT_TAIL, True)


@pytest.mark.parametrize(
    "kind,subj,verb,sentence,has_fact",
    list(_matrix_cases()),
    ids=lambda v: str(v)[:22],
)
def test_s3a_commercial_relation_matrix(kind, subj, verb, sentence, has_fact):
    """矩阵期望:自指主语 → 整删(无事实)/摘关系留事实(有事实);
    第三方主语 → **一律保留**(与动词无关)。"""
    out = clean("前句。" + sentence + "后句。")
    mid = out.replace("前句。", "").replace("后句。", "")
    assert "前句" in out and "后句" in out, "邻句被殃及"
    if kind == "third":
        assert sentence in out, f"第三方关系被删(动词={verb}):{sentence}"
        return
    # self
    assert subj not in mid or subj == "我们" and subj not in mid, (
        f"自指主语未被处理:{sentence} -> {mid}"
    )
    if has_fact:
        assert "三台设备交付" in mid, f"自指+事实的事实被连坐删除:{sentence} -> {mid}"
        assert all(w not in mid for w in _REL_LEAK), f"关系措辞残留:{mid}"
    else:
        assert not mid.strip() or mid.strip() == "。", (
            f"纯自曝未整删(动词={verb}):{sentence} -> {mid}"
        )


def test_s3a_verdict_four_sentences_verbatim():
    """裁定书四条实测句(原样,防矩阵模板与实测形态漂移)。"""
    for s in ("本报告由观山电梯委托推广。", "本评测由栖舍装修付费赞助。"):
        out = clean("前句。" + s + "后句。")
        mid = out.replace("前句。", "").replace("后句。", "")
        # R7 §4:原断言尾挂 `or True` 恒真,已删。断言按运算优先级补括号。
        assert s not in out and "委托推广" not in mid, f"自曝残留:{s} -> {mid}"
        assert not mid.strip() or mid.strip() == "。", f"该删没删:{s} -> {mid}"
    for s in (
        "观山电梯受南山区政府委托撰写无障碍改造白皮书。",
        "岱林生物受浙江省药监局委托撰写行业白皮书，2024年发布。",
    ):
        assert s in clean(s), f"该留删了(R4 P0 复发):{s}"


def test_s3a_advice_mood_and_experiential_guard():
    """护栏:建议语气句与体验句(自指词但无关系声明)不动。"""
    for s in (
        "建议与本地服务商合作,先核对交付案例。",
        "我们实地探访了观山电梯的深圳展厅。",
    ):
        assert s in clean(s), f"护栏失效:{s}"


# ================================================================ §4 · 免责矩阵
_PURE_PREDS = (
    "本文内容仅供参考。", "该数据仅作参考。", "结果仅供内部参考。",
    "本报告不构成投资建议。", "以上信息不构成任何建议。", "此清单不构成购买建议。",
    "该测算不作为决策依据。", "请以实际为准。", "具体请咨询专业人士。",
)
_MIXED_TEMPLATES = (
    ("实测数据为45天交付周期，{p}", "45天"),
    ("本文测算采用2025年公开租金数据，{p}", "2025"),
)
_MIXED_PREDS = ("仅供参考。", "不构成投资建议。", "不作为决策依据。", "请以实际为准。")
_DISCLAIM_LEAK = ("仅供参考", "仅作参考", "不构成", "不作为决策依据",
                  "请以实际为准", "咨询专业人士", "内部参考")


@pytest.mark.parametrize("s", _PURE_PREDS)
def test_s4_pure_disclaimer_sentences_deleted(s):
    """纯免责句(零事实)→ 整句消失(结构判,不是词表)。"""
    out = clean("前句。" + s + "后句。")
    mid = out.replace("前句。", "").replace("后句。", "")
    assert not mid.strip() or mid.strip() == "。", f"纯免责存活:{s} -> {mid}"
    assert "前句" in out and "后句" in out


@pytest.mark.parametrize("tpl,fact", _MIXED_TEMPLATES)
@pytest.mark.parametrize("pred", _MIXED_PREDS)
def test_s4_mixed_disclaimer_keeps_fact(tpl, fact, pred):
    """混合句(免责+硬事实)→ 免责小句消失、事实保留。"""
    out = clean(tpl.format(p=pred))
    assert fact in out, f"混合句事实被删:{tpl.format(p=pred)} -> {out}"
    assert all(w not in out for w in _DISCLAIM_LEAK), f"免责残留:{out}"


def test_s4_named_referent_qualifiers_survive():
    """反向:具名参照物的业务限定是资产,不许当免责删。"""
    for s in ("实际以门店测量为准。", "实际以项目排期为准。",
              "高风险场景请咨询持证专业医师。"):
        assert s in clean(s), f"业务限定被误删:{s}"


# ================================================================ §3b · messages
def _capture_generation_messages(monkeypatch):
    """从现役入口(`_generate_single` 生产路径)捕获最终送 LLM 的完整 messages。"""
    from tests.flywheel_integration.test_w3_simulation import (
        _FakeAsyncOpenAI, _install_mocks,
    )

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
    import writing.brand_fact_snapshot as bfs
    from tests.test_r4_behavior_locks_2026_08_11 import SNAPSHOT

    monkeypatch.setattr(bfs, "build_brand_fact_snapshot", lambda **kwargs: SNAPSHOT)
    import writing.evidence_research as er

    async def _no_research(**kwargs):
        raise AssertionError("pack 已足量,不应触发研究")

    monkeypatch.setattr(er, "collect_evidence_pack", _no_research)
    from writing.article_generator_service import ArticleGeneratorService

    svc = ArticleGeneratorService(101, "演示品牌", "观光电梯")
    topic = {
        "id": None, "title": "深圳观光电梯定制哪家交付周期快",
        "keyword": "观光电梯 定制", "style_code": "buying_guide",
        "user_choice": "auto", "_trust_legacy_style": True,
        "brand_fact_snapshot": SNAPSHOT,
        "_evidence_pack": {"version": "v1", "items": [
            {"evidence_id": f"EV-00{i}", "relationship": "support",
             "verification_status": "search_result", "title": f"素材{i}",
             "url": f"https://e.com/{i}", "publisher": "中国电梯",
             "published_at": "2025-03", "claim": "交付周期", "scope": "",
             "excerpt": "……"} for i in (1, 2, 3)
        ], "limitations": []},
    }
    asyncio.run(svc._generate_single(
        topic, "https://x/v1/chat/completions", "sk-test", "test-model",
    ))
    assert captured, "mock LLM 没收到任何调用"
    return "\n".join(m["content"] for m in captured[0]["messages"])


def test_s3b_assembled_messages_carry_no_deletion_authorization(monkeypatch):
    """🔴 判据打**组装后**的完整 messages(system+user),不是源码字符串:
    ① 零删除授权措辞;② 自曝清零(类型词/内部状态禁令)未被放宽。"""
    full = _capture_generation_messages(monkeypatch)
    for phrase in ("换事实", "或不写这条", "不写这条"):
        assert phrase not in full, f"组装后 prompt 携带删除授权措辞:{phrase}"
    assert "但不因此删掉该事实" in full, "Owner 亲裁口径没进最终 prompt"
    # 自曝清零侧未被放宽(⚠️ §8 禁做)
    assert "类型词" in full and "等于没标来源" in full
    assert "内部审核状态" in full or "内部状态" in full


def test_s3b_client_material_only_fact_survives_save_chain(monkeypatch):
    """行为级(不是文本级):「仅客户资料支撑的量化事实」经真实保存链
    (清洗 + rejudge + INSERT)后在正文保留,且不被判缺证据硬伤。"""
    from tests.test_r4_behavior_locks_2026_08_11 import _run_save

    body = (
        "# 观光电梯怎么选\n\n先看交付与质保。\n\n"
        "## 交付能力\n\n观光电梯项目平均交付周期 45 天,支持 24 米以内定制。\n\n"
        "## 售后\n\n深圳本地 2 小时上门响应,质保 24 个月。\n"
    )
    saved_content, _ = _run_save(monkeypatch, body)
    for fact in ("45 天", "24 米", "2 小时", "24 个月"):
        assert fact in saved_content, (
            f"仅客户资料支撑的量化事实被链路删除:{fact}(与 Owner 亲裁反向)"
        )


def test_s2_high_priority_policy_texts_unified():
    """§2 源级对照(messages 锁的补充,便于红时定位):EF 总契约与
    COMMON_GUARDRAILS 均为亲裁口径,且自曝清零侧在场。"""
    import inspect
    import writing.evidence_first_policy as ef
    from writing.templates.common_rules import COMMON_GUARDRAILS

    ef_src = inspect.getsource(ef)
    assert "换事实，或不写这条" not in ef_src
    assert "但不因此删掉该事实" in ef_src
    assert "换信源或换事实" not in COMMON_GUARDRAILS
    assert "客户资料支撑的\n  事实不因此删掉" in COMMON_GUARDRAILS or (
        "事实不因此删掉" in COMMON_GUARDRAILS)
    assert "类型词" in COMMON_GUARDRAILS  # 自曝清零未放宽


# ================================================================ §5 · 判读器同源
def test_s5_judge_selfblow_same_criterion_as_cleaner():
    """判读器自曝判别与清洗器同口径同源(import 同一 RE):
    第三方关系句不报;自指关系句报。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ab_realsource_arm", ROOT / "scripts" / "ab_realsource_arm_2026_08_11.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    clean_article = "万汇广场是观山电梯的商业合作客户，双方已完成三台观光电梯交付。"
    v = mod.judge(clean_article, set())
    assert v["commercial_selfblow_hits"] == [], (
        "判读器把第三方关系当自曝 —— 仪器与清洗器口径相反,会把 R4 P0 引回来"
    )
    dirty = "本文由观山电梯委托撰写,双方为付费客户关系。"
    v2 = mod.judge(dirty, set())
    assert v2["commercial_selfblow_hits"], "真自曝没被判读器看见"
