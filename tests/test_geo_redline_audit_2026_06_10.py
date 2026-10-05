# -*- coding: utf-8 -*-
"""GEO 红线批(audit · 2026-06-10 老板已批):
① list_active_subscriptions paid-only → 服务锚 SSOT(confirmed 玩法B真客户订阅僵尸态修复)【红线 monitoring_db】
② rollback_task trends 按日期全局删 → 本任务词集维度(防全平台趋势被抹)【红线 monitoring_db】
③ 诊断 4 引擎 fail-closed 闸门(0 分假报告 + 错扣费)【diagnosis_workflow 非红线·闸门在评分 try 外】"""
import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MON_DB = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
WF = (ROOT / "workflows" / "diagnosis_workflow.py").read_text(encoding="utf-8")


def _fn_block(src: str, anchor: str, span: int = 4500) -> str:
    i = src.find(anchor)
    assert i > 0, f"未找到: {anchor}"
    return src[i:i + span]


# ---------- ① 服务锚 SSOT ----------

def test_1_list_active_subscriptions_service_anchor():
    b = _fn_block(MON_DB, "def list_active_subscriptions(")
    assert "q.status = 'paid'  -- v1.2 paid-only" not in b, "paid-only 旧守护必须被 SSOT 口径替换"
    assert "quote_service_anchor_condition_sql" in b
    assert "compliant_days < service_days" in b
    assert "cancelled', 'expired', 'inactive'" not in b


def test_1_aligned_with_compliance_check():
    """引擎侧口径必须与 run_daily_compliance_check(2026-05-30 Deploy 修)同一 SSOT(此前正是两侧不一致致僵尸)。"""
    b = _fn_block(MON_DB, "def run_daily_compliance_check(", 6000)
    assert "COALESCE(service_start_date, paid_at) IS NOT NULL" in b


# ---------- ② rollback 词集维度 ----------

def _join_adjacent_literals(src: str) -> str:
    """把 Python 相邻字符串字面量的拼接缝合掉,还原成**逻辑 SQL**。

    源码里一条 SQL 常被拆成几段相邻字面量,行内锚点因此断在缝上 ——
    这不是代码的问题,是判据取样方式的问题。
    """
    return re.sub(r'"\s*\n\s*"', "", src)


def test_2_rollback_trends_scoped_to_task_keywords():
    b = _join_adjacent_literals(_fn_block(MON_DB, "def rollback_task("))

    # 🔴 2026-08-26 重锚(存量红三臂取证后)。
    #    这条判据原先钉的是**整条 SQL 字面量**:
    #        SELECT DISTINCT keyword_id FROM monitoring_results WHERE task_id = %s
    #    2026-07-21 `aed2ecc80` 把取词集改成 COALESCE(confirmed_keyword_id,
    #    keyword_id) 并滤 NULL —— 那是**加强**,红线没被破坏,但字面量锚当场失效。
    #    于是这条判据从那天起一直红,也就是说这条红线**整整一个月没人在守**:
    #    永远红的判据,和红线真被破坏,长得一模一样。
    #    所以重锚到**语义**上。别再改回字面量锚 —— 下一次合理重构会再把它打回红。

    # ① 词集必须取自**本任务**的 monitoring_results(不许全表、不许按日期)
    m_kwset = re.search(
        r"SELECT DISTINCT [^\"]*?keyword_id[^\"]*? "
        r"FROM monitoring_results WHERE task_id = %s", b)
    assert m_kwset, "词集必须 SELECT DISTINCT … FROM monitoring_results WHERE task_id"

    # ② 必须在删 results **之前**取(删完就取不到了)
    i_delete_results = b.find("DELETE FROM monitoring_results")
    assert 0 < m_kwset.start() < i_delete_results, "词集必须在删 results 之前取"

    # ③ 🔴 红线本体:**每一处** DELETE FROM keyword_trend_stats 都必须带词集维度。
    #    原判据是一条 `'…WHERE period_date = %s"' not in b` —— 只堵了**一种写法**,
    #    换个引号/换个空格就绕开,而且锚一旦失效就变成**恒真**(fail-open)。
    #    改成机械枚举:枚举出所有删除点,逐个要求 keyword 维度。
    deletes = [mm.start() for mm in re.finditer(r"DELETE FROM keyword_trend_stats", b)]
    assert deletes, "rollback_task 里找不到 keyword_trend_stats 的删除点 —— 判据脱靶了"
    for i in deletes:
        stmt = b[i:i + 300]
        assert "keyword_id = ANY(%s)" in stmt, (
            "存在**无词集维度**的 trends 删除 —— 一次回退会抹掉当天全平台所有租户的"
            f"趋势统计。出事的语句:{stmt[:160]!r}")

    # ④ fail-closed:词集空不删 trends
    assert "if task_date and _task_kw_ids:" in b


# ---------- ③ 诊断 fail-closed 闸门 ----------

def _load_gate():
    m_cls = re.search(r"(class DiagnosisDataInsufficientError[\s\S]*?)\n\ndef _assert_ai_visibility_sufficient", WF)
    m_fn = re.search(r"(def _assert_ai_visibility_sufficient[\s\S]*?)\n\nasync def ", WF)
    assert m_cls and m_fn
    ns = {}
    exec(textwrap.dedent(m_cls.group(1)) + "\n\n" + m_fn.group(1), ns)
    return ns["_assert_ai_visibility_sufficient"], ns["DiagnosisDataInsufficientError"]


def test_3_gate_blocks_all_engines_failed():
    gate, Err = _load_gate()
    with pytest.raises(Err):
        gate({"summary": {"total_planned": 40, "total_tests": 0, "total_failed": 40}})


def test_3_gate_degrades_majority_failed_instead_of_erasing_valid_results():
    """[C组①·Owner 裁决 §12.1 · 按 §1.4 改写锁测试]

    旧规则:failed/planned>0.5 直接 H0 拦死 —— 会把**已有的有效结果整批抹掉**,且全额退款,
    客户既拿不到报告、平台也白跑,违反 §12.1「有足够有效样本 → 降级交付 + 未履约不计费」。
    新规则:仍有有效样本(16/40 成功)→ **不抛**,降级交付,由结算侧按覆盖率部分计费。
    反回归意图不变:零可用仍然必须拦(见下一条 test_3_gate_blocks_all_engines_failed)。
    """
    gate, Err = _load_gate()
    # 40 计划、16 次成功、24 次失败(60% 失败)→ 降级交付,不再抛
    gate({"summary": {"total_planned": 40, "total_tests": 16, "total_failed": 24}})

    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, billable_points, evaluate_sample
    v = evaluate_sample({"summary": {"total_planned": 40, "total_tests": 16, "total_failed": 24}})
    assert v.outcome == OUTCOME_DEGRADED and v.deliverable is True
    assert billable_points(v, 1000) == 400        # 未履约的 60% 不计费


def test_3_gate_blocks_phase2_timeout_shape():
    gate, Err = _load_gate()
    with pytest.raises(Err):
        gate({"error": "timeout"})


def test_3_gate_allows_legit_zero_detection():
    """测了但没检出(真隐形)≠ 采集失败 → 必须放行(0 分是真实结论)。"""
    gate, _ = _load_gate()
    gate({"summary": {"total_planned": 40, "total_tests": 40, "total_failed": 0, "total_detected": 0}})
    gate({"summary": {"total_planned": 40, "total_tests": 25, "total_failed": 15}})  # 37.5% 失败 < 50% 放行
    gate({})        # 老结构无 summary 无 error → 放行(向后兼容)
    gate(None)      # 防御 None


def test_3_gate_called_outside_scoring_try():
    """闸门必须在 lite 评分 try 之前(except 会吞成默认 0 分)。"""
    i_gate = WF.find('_assert_ai_visibility_sufficient(results["data"].get("ai_visibility"))')
    assert i_gate > 0
    i_try = WF.find("score_data = {\n            \"total_score\": 0,", i_gate)
    assert i_try > i_gate, "闸门必须先于评分默认值/try 块"
    # 王姐口径:用户可见文案不露工程词/供应商名
    seg = _fn_block(WF, "def _assert_ai_visibility_sufficient")
    for banned in ("API", "engine_error", "qwen", "deepseek", "kimi", "doubao", "freeze", "commit"):
        assert banned not in seg.split('"""')[2] if seg.count('"""') >= 2 else True
    assert "算力已退回" in seg


# ---------- #3 返修:跨分支部署依赖显式化 ----------

def test_3_deploy_dependency_on_geo_core_documented():
    """[#3 返修] 跨分支部署依赖必须在 docstring 显式标注:本分支 server.py 0 改,release_freeze 靠
    #2 geo-core 的 _run_diagnosis_impl re-raise;#2 须同批 / 先行部署(防被单独 cherry-pick 上线)。"""
    cls_doc = WF[WF.find("class DiagnosisDataInsufficientError"):WF.find("def _assert_ai_visibility_sufficient")]
    assert "geo-core" in cls_doc, "须标注依赖 #2 geo-core 分支"
    assert "re-raise" in cls_doc, "须标注靠 #2 的 re-raise"
    assert ("同批" in cls_doc or "先行" in cls_doc), "须标注 #2 必须同批/先行部署"
    assert "0 改" in cls_doc, "须标注本分支 server.py 0 改(claim 更正)"


def test_3_signal_error_is_real_exception_for_reraise():
    """[#3 返修] 信号以【抛异常】方式发出(非返回值)→ 才能被 #2 顶层 except 的 generic re-raise
    穿透到 run_diagnosis_task release_freeze。DiagnosisDataInsufficientError 须是 Exception 子类。"""
    gate, Err = _load_gate()
    assert issubclass(Err, Exception), "信号须是异常类型(供 re-raise 穿透)"
    raised = False
    try:
        gate({"error": "timeout"})
    except Err:
        raised = True
    assert raised, "闸门须以抛异常发信号(依赖 #2 re-raise → release_freeze)"
