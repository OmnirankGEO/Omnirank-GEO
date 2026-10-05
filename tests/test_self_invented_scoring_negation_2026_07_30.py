"""[误报治理工单 2026-07-30 · §7.1] 七条锁 —— 判定侧与修复出口三态。

判别原则:
- **全部行为级**,真调 `writing.evidence_first_policy.evaluate_content_trust`
  (生产判定入口本体)与 `services.span_level_repair` 的真函数,不做源码串断言。
- 锁 1 的四个样本用**生产真实句原文**(2026-07-30 经只读通道从 `articles.content`
  取出,一字未改写),不是手写的近似句。
- 🔴 锁 1 与锁 2/3/4 构成**分布断言**:必须同时出现"不报"与"仍报 hard"两种结果。
  只有锁 1 的话,把整支正则删掉照样全绿 —— 那是本仓踩过的假绿形态。

跑法(锁 1-5、锁 6 的纯函数部分不需要库;锁 6 的额度落库部分需要一次性空库):
    python -m pytest tests/test_self_invented_scoring_negation_2026_07_30.py -q
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import span_level_repair as engine  # noqa: E402
from writing.evidence_first_policy import evaluate_content_trust  # noqa: E402

CODE = "self_invented_scoring_system"


def _judge(text: str) -> tuple[bool, bool]:
    """真判定 → (该 code 是否在 hard, 是否在 soft)。"""
    result = evaluate_content_trust("", text, evidence_mode="unknown")
    return (
        any(f.code == CODE for f in result.hard),
        any(f.code == CODE for f in result.soft),
    )


# ══════════════════════════════════════════════════════════════════════════
# 生产真实句(只读通道 2026-07-30 取自 articles.content · 禁改写)
# ══════════════════════════════════════════════════════════════════════════
PROD_1309 = "所有结论均以可核验的公开证据或企业提交资料为基础，不制造任何自创评分体系"
PROD_1351 = "本文不采用任何自创评分体系，所有比较均基于公开可核验的事实"
PROD_1402 = "本文不采用任何自创评分体系，所有比较均基于公开可核验的事实"
PROD_1295 = "**禁止自创评分体系**：本文不制造综合分、百分制、S/A/B评级或自创权重冒充独立评价"
#: 854 的**真实**命中句。⚠️ 排查文档 §1.2 表里给 854 引的是另一句
#: (「本报告评分体系设计参考了…」),那不是真正的命中处 —— 真命中是这句
#: (第三方批评语境:说别的商家在用自创评级)。以库里取出的为准。
PROD_854 = "**现象**：部分商家过度宣传“种水等级”但缺乏权威鉴定支撑，或使用自创评级体系混淆消费者"
#: 1299 / 1372 是**真命中**(锚分别为 ★★★★ 与 平台评分),必须继续拦住。
PROD_1299_ANCHOR = "综合表现★★★★，值得推荐"
PROD_1372_ANCHOR = "平台评分显示该品牌口碑良好"


# ── 锁 1:四篇误报转绿 ──────────────────────────────────────────────────
@pytest.mark.parametrize("name,text", [
    ("1309", PROD_1309), ("1351", PROD_1351),
    ("1402", PROD_1402), ("1295", PROD_1295),
])
def test_lock1_negated_compliance_statement_not_reported(name, text):
    """否定语境的合规声明:既不在 hard 也不在 soft。"""
    in_hard, in_soft = _judge(text)
    assert not in_hard, f"{name} 的合规声明仍被判 hard(误报未消)"
    assert not in_soft, f"{name} 不该改判成 soft,应当完全不报"


# ── 锁 2:反向对照 · 真违规仍红 ─────────────────────────────────────────
def test_lock2_real_violation_still_hard():
    """🔴 没有这条,"把整支正则删掉"照样让锁 1 全绿。"""
    in_hard, _ = _judge("本文采用自创评分体系，综合评分 92 分")
    assert in_hard, "真违规必须仍报 hard"


@pytest.mark.parametrize("name,text", [
    ("1299 ★★★★", PROD_1299_ANCHOR),
    ("1372 平台评分", PROD_1372_ANCHOR),
])
def test_lock2b_production_real_hits_still_hard(name, text):
    """生产里另外两篇 blocked 的真命中,不许被本次放宽顺手放行。"""
    in_hard, _ = _judge(text)
    assert in_hard, f"{name} 是真命中,必须仍报 hard"


# ── 锁 3:绕法(递进结构)仍红 ───────────────────────────────────────────
def test_lock3_progressive_not_treated_as_negation():
    """「不仅…还…」是递进(语义肯定),不是否定 —— 放行它等于开后门。"""
    in_hard, _ = _judge("本文不仅采用自创评分体系，还引入了自定权重")
    assert in_hard, "「不仅采用」是递进结构,必须仍报 hard"


# ── 锁 4:跨句不救 ──────────────────────────────────────────────────────
def test_lock4_negation_does_not_cross_sentence():
    """前句的否定不能救后句的肯定主张。"""
    in_hard, _ = _judge("我们不使用夸张表述。本文采用自创评分体系。")
    assert in_hard, "跨句否定不该生效,必须仍报 hard"


def test_lock4b_negation_must_precede_hit():
    """否定必须在命中**之前**;命中之后出现的否定不算。"""
    in_hard, _ = _judge("本文采用自创评分体系并非临时决定")
    assert in_hard, "命中之后的否定不该生效,必须仍报 hard"


def test_lock4c_contrast_terminates_negation_scope():
    """否定与命中之间出现转折 → 否定作用域已终止。"""
    in_hard, _ = _judge("本文不采用外部标准而是自创评分体系")
    assert in_hard, "「而是」终止否定作用域,必须仍报 hard"


# ── 锁 5:854 未被顺手放行(与**改动前**基准逐例相等) ───────────────────
def test_lock5_854_behaviour_unchanged_vs_baseline():
    """🔴 不写死期望值:直接把**改动前**的判定模块(从 git 取基线 SHA 的原文件,
    该模块只依赖标准库)加载起来逐例对比。854 与其余真命中样本必须**行为不变**,
    只有否定语境那一类允许改变。"""
    import importlib.util
    import subprocess
    import tempfile

    base_sha = "889702a3"
    src = subprocess.run(
        ["git", "show", f"{base_sha}:writing/evidence_first_policy.py"],
        capture_output=True, cwd=str(Path(__file__).resolve().parents[1]),
    )
    if src.returncode != 0:
        pytest.skip("基线 SHA 不可达(浅克隆/无 git),本锁需要 git 对象库")
    tmp = Path(tempfile.mkdtemp()) / "baseline_policy.py"
    tmp.write_bytes(src.stdout)
    spec = importlib.util.spec_from_file_location("baseline_policy", tmp)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules["baseline_policy"] = baseline
    spec.loader.exec_module(baseline)

    def base_hard(text: str) -> bool:
        return any(f.code == CODE
                   for f in baseline.evaluate_content_trust("", text, evidence_mode="unknown").hard)

    unchanged = {
        "854": PROD_854,
        "1299": PROD_1299_ANCHOR,
        "1372": PROD_1372_ANCHOR,
        "真违规": "本文采用自创评分体系，综合评分 92 分",
        "绕法递进": "本文不仅采用自创评分体系，还引入了自定权重",
        "跨句": "我们不使用夸张表述。本文采用自创评分体系。",
        "否定在后": "本文采用自创评分体系并非临时决定",
        "转折终止": "本文不采用外部标准而是自创评分体系",
    }
    for name, text in unchanged.items():
        before, after = base_hard(text), _judge(text)[0]
        assert before == after, f"{name} 行为被改变了(改前 {before} → 改后 {after}),本单不该动它"

    changed = {"1309": PROD_1309, "1351": PROD_1351, "1402": PROD_1402, "1295": PROD_1295}
    for name, text in changed.items():
        assert base_hard(text) is True, f"{name} 改动前应当是 hard(否则样本选错了)"
        assert _judge(text)[0] is False, f"{name} 改动后应当不报"


# ── 锁 6:修复出口三态 + 额度计次 ───────────────────────────────────────
def _finding_from_real_judgement() -> dict:
    """finding 由**真判定**产出,不手写 —— 判定改了这里会跟着红。"""
    text = "本文采用自创评分体系，综合评分 92 分，服务能力突出。"
    result = evaluate_content_trust("", text, evidence_mode="unknown")
    hit = next(f for f in result.hard if f.code == CODE)
    return {"code": hit.code, "matched_text": hit.matched_text, "message": hit.message}, text


def test_lock6a_cannot_fix_is_needs_human_without_retry():
    """CANNOT_FIX = 需人工,**不给重试**,且不归类为失败。"""
    from services.governance_alerts import cannot_fix_without_fabrication_alert

    finding, text = _finding_from_real_judgement()

    async def _llm(_prompt: str) -> str:
        return engine.CANNOT_FIX_MARKER

    out = asyncio.run(engine.repair_bottomline_span(text, finding, _llm))
    assert out["ok"] is False
    assert out["reason"] == "cannot_fix_without_fabrication"
    assert engine.repair_outcome(out["reason"]) == engine.OUTCOME_NEEDS_HUMAN

    alert = cannot_fix_without_fabrication_alert()
    assert alert["outcome"] == "needs_human"
    assert not [a for a in alert["actions"] if a.get("type") == "retry"], \
        "需人工的出口里不许有重试(点了必然同样结果,只会烧掉免费额度)"


def test_lock6b_mechanical_failure_is_failed_with_retry():
    """机械校验失败 = 真失败,给重试(重试可能有用)。"""
    from services.governance_alerts import span_repair_failed_alert

    finding, text = _finding_from_real_judgement()

    async def _llm(_prompt: str) -> str:
        # 违规命中串原样留着 → repair_violation_text_remains
        return "本文采用自创评分体系，综合评分 92 分，服务能力较好。"

    out = asyncio.run(engine.repair_bottomline_span(text, finding, _llm))
    assert out["ok"] is False
    assert engine.repair_outcome(out["reason"]) == engine.OUTCOME_FAILED

    alert = span_repair_failed_alert(out["reason"])
    assert alert["outcome"] == "failed"
    assert [a for a in alert["actions"] if a.get("type") == "retry"], "真失败必须给重试"


def test_lock6c_infra_error_is_service_unavailable_and_refunds_quota():
    """基础设施错误 = 服务不可用,且**额度未被扣**。"""
    from services.governance_alerts import span_repair_service_unavailable_alert

    finding, text = _finding_from_real_judgement()

    async def _llm(_prompt: str) -> str:
        raise TimeoutError("upstream timeout")

    out = asyncio.run(engine.repair_bottomline_span(text, finding, _llm))
    assert out["ok"] is False
    assert out["reason"] == "repair_llm_failed"
    assert engine.repair_outcome(out["reason"]) == engine.OUTCOME_SERVICE_UNAVAILABLE
    assert span_repair_service_unavailable_alert()["outcome"] == "service_unavailable"

    # 端点语义:占位在调模型之前 → 退还后 used 必须回到 0。
    fp = engine.finding_fingerprint(finding["code"], finding["matched_text"])
    quota = engine.reserve_free_repair({}, fp, code=finding["code"])
    assert engine.free_repairs_used({"span_repair_quota": quota}, fp) == 1
    assert engine.quota_should_refund(out["reason"], bool(out["llm_invoked"])) is True
    refunded = engine.release_free_repair({"span_repair_quota": quota}, fp)
    assert engine.free_repairs_used({"span_repair_quota": refunded}, fp) == 0, \
        "基础设施错误必须退还额度(是我们的故障,不是用户的问题)"


def test_lock6d_mechanical_failure_still_consumes_quota():
    """🔴 反向:真失败**仍然计次**。给了重试就必须有界,否则是无限免费重试。"""
    assert engine.quota_should_refund("repair_violation_text_remains", True) is False
    assert engine.quota_should_refund("cannot_fix_without_fabrication", True) is False


def test_lock6e_never_called_model_refunds():
    """压根没调模型(定位失败/高风险拒绝)→ 零平台成本 → 退还。

    这条把 `repair_bottomline_span` 早就回、但此前**没有任何调用方使用**的
    `llm_invoked` 契约接上了。"""
    assert engine.quota_should_refund("span_not_located", False) is True
    assert engine.quota_should_refund("high_risk_span", False) is True
