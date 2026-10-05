"""E3-4 · 三件延期项的**最低条件**(Codex 二审 §6)。

三件各自的最低条件(工单逐字):
  1. P1-8   确认落库补 canonical request / token purpose·subject / actor 等
            **不可重建**事实;历史缺证据行标 ``legacy_unproven``,**禁伪造回填**;
  2. P1-9b  provider 未回传模型时标 ``planned_fallback``,禁写成 actual;
            不进可比组、不用于客户确定性表述;
  3. P1-10  legacy task 禁返 200/progressPct=0 ⇒ typed unavailable + nextAction;
            诊断报告监测卡片必须绑 snapshot/task/window,绑不了就隐藏该卡片。

🔴 本文件里**没有**真 HTTP / 真库判据 —— 那两类分别由
   ``tests/defensive_geo_w3_2026_08_21`` 与本包的 e1/e3 承担。
   这里打的是纯函数与结构面,跑得快、红得准。真链那一跳在交付文里如实列明。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════════════
# ① P1-8 · 受理证据
# ══════════════════════════════════════════════════════════════════════

def test_e4_01_canonical_request_is_stable_across_input_order():
    """同一笔确认必须每次算出**同一个** hash。

    🔴 不稳定的 hash 只能证明"我算过一次",证明不了"就是这一笔" ——
       而"就是这一笔"正是这一列存在的全部理由。
    """
    from services.defensive_geo import acceptance_evidence as ae

    a = ae.canonical_request(token="tok-1", tier="core",
                             selected_keyword_ids=[3, 1, 2])
    b = ae.canonical_request(token="tok-1", tier="core",
                             selected_keyword_ids=[2, 3, 1])
    assert a == b
    assert ae.canonical_request_hash(a) == ae.canonical_request_hash(b)


def test_e4_02_a_different_selection_is_a_different_request():
    """判别力自证:hash 必须真的跟着请求内容走。"""
    from services.defensive_geo import acceptance_evidence as ae

    base = ae.canonical_request(token="tok-1", tier="core",
                                selected_keyword_ids=[1, 2])
    for other in (
        ae.canonical_request(token="tok-1", tier="core",
                             selected_keyword_ids=[1, 2, 3]),
        ae.canonical_request(token="tok-1", tier="pro",
                             selected_keyword_ids=[1, 2]),
        ae.canonical_request(token="tok-2", tier="core",
                             selected_keyword_ids=[1, 2]),
    ):
        assert ae.canonical_request_hash(base) != ae.canonical_request_hash(other), other


def test_e4_03_the_raw_token_never_lands_in_the_evidence():
    """令牌原文不许出现在任何一格证据里 —— 审计列不是放密钥的地方。"""
    from services.defensive_geo import acceptance_evidence as ae

    token = "super-secret-token-value"
    payload = ae.canonical_request(token=token, tier="core",
                                   selected_keyword_ids=[1])
    blob = repr(payload) + ae.actor_of(token) + str(
        ae.token_subject_of(quote_id=7, brand_id=9))
    assert token not in blob, "令牌原文进了受理证据"
    assert ae.token_fingerprint(token) in blob, "指纹没进去 —— 那这份证据认不出主体"


def test_e4_10_a_fully_evidenced_row_is_proven():
    from services.defensive_geo import acceptance_evidence as ae

    row = {c: "x" for c in ae.REQUIRED_EVIDENCE_COLUMNS}
    assert ae.classify_acceptance(row) == ae.EVIDENCE_PROVEN
    assert ae.missing_evidence_columns(row) == ()


@pytest.mark.parametrize("missing", [
    c for c in (
        "customer_confirmed_token_purpose",
        "customer_confirmed_token_subject",
        "customer_confirmed_actor",
        "customer_confirmed_request_hash",
        "customer_confirmed_request",
    )
])
def test_e4_11_any_missing_audit_column_makes_it_legacy_unproven(missing: str):
    """**逐列**打一发 —— 分母是 REQUIRED_EVIDENCE_COLUMNS,不手抄。

    🔴 只打"全缺"那一格的话,漏写其中任意一列都不会让判据变红。
    """
    from services.defensive_geo import acceptance_evidence as ae

    row = {c: "x" for c in ae.REQUIRED_EVIDENCE_COLUMNS}
    row[missing] = None
    assert ae.classify_acceptance(row) == ae.EVIDENCE_LEGACY_UNPROVEN
    assert missing in ae.missing_evidence_columns(row)


def test_e4_12_never_confirmed_is_absent_not_unproven():
    """还没确认 ≠ 确认了没留证据。前者正常,后者是已知欠账。"""
    from services.defensive_geo import acceptance_evidence as ae

    assert ae.classify_acceptance(None) == ae.EVIDENCE_ABSENT
    assert ae.classify_acceptance({c: None for c in ae.REQUIRED_EVIDENCE_COLUMNS}) \
        == ae.EVIDENCE_ABSENT


def test_e4_13_a_legacy_pointer_only_row_is_unproven():
    """存量已确认行(三件组齐、审计列空)= ``legacy_unproven``。"""
    from services.defensive_geo import acceptance_evidence as ae

    row = {c: None for c in ae.REQUIRED_EVIDENCE_COLUMNS}
    for c in ae.POINTER_COLUMNS:
        row[c] = "x"
    assert ae.classify_acceptance(row) == ae.EVIDENCE_LEGACY_UNPROVEN


def test_e4_14_the_module_cannot_backfill():
    """结构性:本模块不写库 —— 回填 = 伪造受理证据。"""
    from services.defensive_geo import acceptance_evidence as ae

    assert ae.census()["writesToDatabase"] is False
    src = inspect.getsource(ae)
    for banned in ("UPDATE ", "INSERT ", "cursor", "get_connection"):
        assert banned not in src, f"受理证据模块里出现了写库面 {banned!r}"


def test_e4_15_both_confirm_modes_write_the_same_evidence():
    """集群模式与平铺模式必须走**同一个**证据组装函数。

    🔴 两处各写一份 = 同一谓词写两处;两条路径落的证据不一致那天,
       没有任何判据会红(本仓记过)。
    """
    tree = ast.parse((ROOT / "api/selection_api.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_acceptance_evidence"]
    assert len(calls) == 2, (
        f"``_acceptance_evidence`` 调用点 {len(calls)} 处(应为 2:集群 + 平铺)")

    # 🔴 两个调用点都走 ``asyncio.to_thread(_commit_frozen_quote_confirmation, ...)``,
    #    所以被调函数是**第一个位置参数**,不是 ``call.func``。
    #    第一版按 ``call.func`` 找,census 返 0 —— 而 0 == 0 不会红,
    #    只有那句 ``== 2`` 把它抓住了(分母必须自证非空,这里就是那一条)。
    commits = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and n.args
        and getattr(n.args[0], "id", "") == "_commit_frozen_quote_confirmation"]
    passed = [c for c in commits
              if any(k.arg == "accepted_request" for k in c.keywords)]
    assert len(passed) == len(commits) == 2, (
        f"{len(commits)} 处 confirm 调用里只有 {len(passed)} 处带受理证据 —— "
        "少一处就是一条静默无证据的确认路径")


def test_e4_16_migration_042_declares_the_two_missing_columns():
    """两列**真正缺**的列必须在 042 里 additive 加上。

    🔴 顺带钉住"**不许**把它们并进 group_complete CHECK":存量已确认行
       (三件组齐、审计列空)会让 ADD CONSTRAINT 当场失败 ⇒ prestart 非零退出。
    """
    sql = (ROOT / "db/migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql"
           ).read_text(encoding="utf-8")
    for col in ("customer_confirmed_token_subject", "customer_confirmed_request"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in sql, f"042 没加 {col}"
    group = sql.split("keyword_selection_sessions_confirmed_group_complete")[-1]
    group = group.split("END $$;")[0]
    for col in ("customer_confirmed_token_subject", "customer_confirmed_request",
                "customer_confirmed_actor", "customer_confirmed_request_hash",
                "customer_confirmed_token_purpose"):
        assert col not in group, (
            f"{col} 被并进了 group_complete CHECK —— 存量行会让 prestart 非零退出")


# ══════════════════════════════════════════════════════════════════════
# ② P1-9b · planned_fallback
# ══════════════════════════════════════════════════════════════════════

def test_e4_20_a_missing_provider_model_is_marked_planned_fallback():
    from services.monitoring_lineage import normalize_lineage_payload

    # 生产就是这么调的:adapter 的 raw payload 里**没有** model
    # (provider 还没把回显带回来),于是走回落。
    payload = normalize_lineage_payload(
        {"platform": "dashscope", "question": "这个行业有哪些品牌?",
         "target_brand": "被测品牌", "response_status": "success",
         "keyword_source": "confirmed", "keyword_type": "brand"},
        full_response="提到了被测品牌。", is_detected=True, mention_type="direct")
    assert payload["model_source"] == "planned_fallback", payload
    assert payload["model"] and payload["model"] != "unknown", (
        "回落值本身仍然要有 —— 打标不是把它清空")


def test_e4_21_an_echoed_model_is_marked_provider_echo():
    """判别力自证:**声明了真回显**时必须是 ``provider_echo``。

    🔴 [工单 V3-C · C-3 · 2026-08-28 改口径] 本条上一版断言的是
       「调用方给了 ``model`` ⇒ provider_echo」—— 那正是 Codex 三审 P1-5
       的缺陷本身:唯一的生产调用方恒给一个**计划**模型名,于是这条判据
       把"每一行都错标成已证实"**锁成了正确行为**。
       判据写错方向比没有判据更贵:它会在下一个人想改对的时候变红。
       现在的键是调用方的**显式声明**,不是"给没给值"。
    """
    from services.monitoring_lineage import normalize_lineage_payload

    payload = normalize_lineage_payload(
        {"platform": "dashscope", "question": "这个行业有哪些品牌?",
         "target_brand": "被测品牌", "response_status": "success",
         "keyword_source": "confirmed", "keyword_type": "brand",
         "model": "qwen-real-echo-2026", "model_source": "provider_echo",
         "model_revision": "rev-1"},
        full_response="提到了被测品牌。", is_detected=True, mention_type="direct")
    assert payload["model_source"] == "provider_echo", payload
    assert payload["model"] == "qwen-real-echo-2026"


def test_e4_21b_a_bare_model_without_a_declaration_is_not_an_echo():
    """反例臂:**只**给模型名、不声明来源 ⇒ 仍然是 planned_fallback。

    这一臂是上一条的对立面。少了它,把谓词改回"给了 model 就算回显"
    照样全绿 —— 缺陷会原样回来。
    """
    from services.monitoring_lineage import normalize_lineage_payload

    payload = normalize_lineage_payload(
        {"platform": "dashscope", "question": "这个行业有哪些品牌?",
         "target_brand": "被测品牌", "response_status": "success",
         "keyword_source": "confirmed", "keyword_type": "brand",
         "model": "qwen3-max", "model_revision": "rev-1"},
        full_response="提到了被测品牌。", is_detected=True, mention_type="direct")
    assert payload["model_source"] == "planned_fallback", payload
    assert payload["lineage_status"] == "model_unconfirmed", payload


def test_e4_22_a_planned_fallback_is_never_written_as_observed():
    """回落值**不许**被当成 observed 落进账本。

    🔴 打在真调用点上:``save_monitoring_result`` 里传给
       ``close_for_result(observed_model=...)`` 的那个表达式必须以
       ``model_source == provider_echo`` 为条件。
       只验 lineage 打了标、不验这一跳的话,标打了也没人看。
    """
    src = (ROOT / "db/monitoring_db.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "close_for_result"):
            continue
        for kw in node.keywords:
            if kw.arg != "observed_model":
                continue
            found.append(ast.dump(kw.value))
    assert found, "没找到 close_for_result(observed_model=...) 调用 —— 探针失效"
    for dumped in found:
        assert "model_source" in dumped and "provider_echo" in dumped, (
            "observed_model 无条件传了 lineage 的 model —— "
            "计划值会被当成「平台实际用的模型」确认下来")


def test_e4_25_the_conservative_default_is_pinned_and_load_bearing():
    """🔴 [外选 MUT-EXTE3-13 存活] ``CellIdentity.model_source`` 的**缺省**本身。

    上下两条(e4_23/e4_24)都**显式传** ``model_source``,于是那个缺省值
    在全仓没有任何消费者、也没有任何判据 —— 把它从 ``planned_fallback``
    翻成 ``provider_echo``,27 发外选里这一发全分母零红。
    缺省翻转的后果不是"少了个默认值":任何不传这一位的构造方**自动变成
    「已证实的真回显」**、直接进 matched cohort —— 也就是拿"我们打算发同一个
    模型"去证明"两边跑的是同一个模型"。今天没有真构造方,所以它是一句
    **口头承诺**;第一个真构造方接上那天,没人守。

    三件事一起判(缺任何一件这条就有一半是空的):
      ① 缺省**存在**(不传也能构造)—— 否则下面两条是对空气说的;
      ② 缺省的**值**取自另一个模块的 SSOT 产出,不手抄字面量;
      ③ 缺省的**行为**:不传 model_source 的格**进不了** matched cohort。
    """
    from services.defensive_geo.monitoring import comparability_feed as cf
    from services.monitoring_lineage import normalize_lineage_payload

    # ① 缺省存在:整条构造**不传** model_source
    default_cell = cf.CellIdentity(
        question_identity_key="q1", question_revision=1,
        public_platform="dashscope", actual_provider="dashscope",
        actual_model="m1", actual_model_revision="r1",
        planned_surface="s", search_mode="enhanced", run_index=1)
    assert "model_source" in cf.CellIdentity._field_defaults, (
        "model_source 没有缺省了 —— 那本条前提变了(改成必填也行,"
        "但要把这条判据改写成「必填」而不是删掉)")

    # ② 期望值不手抄:取 lineage 那一侧「没回显时该打什么标」的 SSOT 产出。
    #    分母来自**另一个模块**,与被测的缺省无关。
    fallback_marker = normalize_lineage_payload(
        {"platform": "dashscope", "question": "这个行业有哪些品牌?",
         "target_brand": "被测品牌", "response_status": "success",
         "keyword_source": "confirmed", "keyword_type": "brand"},
        full_response="提到了被测品牌。", is_detected=True,
        mention_type="direct")["model_source"]
    assert fallback_marker != cf.MODEL_SOURCE_COMPARABLE, "前提自证塌了"
    assert default_cell.model_source == fallback_marker, (
        f"缺省是 {default_cell.model_source!r},而"
        f"「供应商没回显」这件事的 SSOT 标记是 {fallback_marker!r} —— "
        "两处对不上时,不传这一位的构造方会被当成另一种东西")

    # ③ 行为那一位:缺省的格必须**进不了** cohort;显式传真回显才进得去。
    assert cf.comparable_cells([default_cell]) == (), (
        "不传 model_source 的格直接进了可比 cohort —— "
        "「保守缺省」翻成了「默认已证实」,所有还没接回显的调用方悄悄进 cohort")
    echoed = default_cell._replace(model_source=cf.MODEL_SOURCE_COMPARABLE)
    assert cf.comparable_cells([echoed]) == (echoed,), (
        "判别力自证:显式的真回显反而进不去 —— 那说明上一条断言是恒真的")


def test_e4_23_planned_fallback_cells_do_not_enter_the_matched_cohort():
    from services.defensive_geo.monitoring import comparability_feed as cf

    def cell(model_source: str, run_index: int = 1) -> cf.CellIdentity:
        return cf.CellIdentity(
            question_identity_key="q1", question_revision=1,
            public_platform="dashscope", actual_provider="dashscope",
            actual_model="m1", actual_model_revision="r1",
            planned_surface="s", search_mode="enhanced", run_index=run_index,
            model_source=model_source)

    both_planned = [cell("planned_fallback")]
    assert cf.comparable_cells(both_planned) == ()
    out = cf.project(
        baseline_snapshot_ref="b", current_snapshot_ref="c",
        baseline_as_of="2026-08-01T00:00:00Z", current_as_of="2026-08-02T00:00:00Z",
        scope={k: True for k in cf.SCOPE_TO_DIMENSION},
        baseline_cells=both_planned, current_cells=both_planned)
    assert out.matched_cells == 0, (
        "两边都是计划值却算成了一次成功匹配 —— "
        "那等于用「我们打算发同一个模型」去证明「两边跑的是同一个模型」")
    # 🔴 被滤掉的格仍然要算进分母,否则可比档位会被**抬高**。
    assert out.baseline_only_cells == 1 and out.current_only_cells == 1, out


def test_e4_24_echoed_cells_still_match():
    """判别力自证:真回显的格必须照常进 cohort。"""
    from services.defensive_geo.monitoring import comparability_feed as cf

    c = cf.CellIdentity(
        question_identity_key="q1", question_revision=1,
        public_platform="dashscope", actual_provider="dashscope",
        actual_model="m1", actual_model_revision="r1",
        planned_surface="s", search_mode="enhanced", run_index=1,
        model_source="provider_echo")
    # 🔴 多给一格 current-only:两边**完全**相同会落进 ``full`` 档,
    #    而 full 档要求非空 metric_comparisons(MET-43)—— 那与本条要问的
    #    "真回显的格能不能进 cohort" 无关,会红在一个不相干的地方。
    extra = c._replace(run_index=2)
    out = cf.project(
        baseline_snapshot_ref="b", current_snapshot_ref="c",
        baseline_as_of="2026-08-01T00:00:00Z", current_as_of="2026-08-02T00:00:00Z",
        scope={k: True for k in cf.SCOPE_TO_DIMENSION},
        baseline_cells=[c], current_cells=[c, extra],
        # partial/full 档都要求非空 comparison(MET-43)。形状逐字取
        # tests/defensive_geo_w2_2026_08_21 里那份既有夹具,不自己编一个。
        metric_comparisons=[{"comparable": True, "comparisonKey": "k1",
                             "metricDefinitionKey": "sov",
                             "cohortCommitment": "matched-cohort-v1"}])
    assert out.matched_cells == 1, out
    assert out.current_only_cells == 1, out


def test_e4_25_model_source_is_not_a_cohort_match_key():
    """``model_source`` 是**准入**条件,不是匹配键。

    混进匹配键会让"两边都是 planned_fallback"变成一次成功匹配 ——
    正好是要挡的那件事。
    """
    from services.defensive_geo.monitoring import comparability_feed as cf

    assert "model_source" not in cf.COHORT_MATCH_KEYS


# ══════════════════════════════════════════════════════════════════════
# ③ P1-10 · legacy 进度 / 报告卡片绑定
# ══════════════════════════════════════════════════════════════════════

def test_e4_30_the_legacy_run_code_is_typed_and_not_retryable():
    """老链监测 ⇒ 409 + 不可重试 + 人话 + 可渲染的下一步。

    🔴 不可重试是要点:再点一次仍然是同一次老链运行。retryable=true
       会让前端自动重试一个永远不会变的答案。
    """
    from api.defensive_geo_api import _safe_error

    exc = _safe_error("MONITORING_LEGACY_RUN")
    payload = exc.detail
    assert exc.status_code == 409, exc.status_code
    assert payload["retryable"] is False
    assert payload["publicExplanation"]
    assert payload["nextAction"]["label"]


def test_e4_31_the_legacy_copy_says_the_run_itself_is_fine():
    """文案必须先安抚"监测本身没事",并且不出现版本工程词。"""
    from services.defensive_geo.copy_registry import user_label

    text = user_label("reason", "monitoring_legacy_run")
    assert "没问题" in text or "没事" in text, text
    for banned in ("v2", "V2", "新链", "老链", "schema", "enrollment"):
        assert banned not in text, f"文案里出现工程词 {banned!r}:{text}"


def test_e4_32_the_progress_endpoint_branches_on_enrollment():
    """进度端点必须在**返回 200 之前**分流 legacy。

    分母 = 该函数的 AST:``resolve_monitoring_enrollment`` 的调用必须存在,
    且其结果必须导向一次 ``_safe_error("MONITORING_LEGACY_RUN")``。
    """
    src = (ROOT / "api/defensive_monitoring_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = ast.dump(node)
            if "RunProgressResponse" in body and "project_run_progress" in body:
                target = node
    assert target is not None, "没定位到进度端点 —— 探针失效"
    dumped = ast.dump(target)
    assert "MONITORING_LEGACY_RUN" in dumped, (
        "进度端点没有 legacy 分流 —— 老链任务仍会拿到 200/progressPct=0")

    # 🔴 [撕锁补洞 · MUT-E4-04 存活] 光看"字符串在不在"挡不住
    #    ``if False and not plan_cell_ids:`` —— 两个名字**都还在** AST 里,
    #    而那一支已经死了。本仓记过同形(「elif orelse 骗过接线锁」)。
    #    所以这里判**分支条件的形状**:那个 if 的 test 必须就是
    #    ``not <name>``,不许是掺了常量的 BoolOp。
    guard = None
    for node in ast.walk(target):
        if isinstance(node, ast.If) and "MONITORING_LEGACY_RUN" in ast.dump(node)                 and guard is None:
            guard = node
    assert guard is not None, "没定位到 legacy 分流那个 if —— 探针失效"
    assert isinstance(guard.test, ast.UnaryOp) and isinstance(guard.test.op, ast.Not), (
        f"分流条件不是 `not <plan_cell_ids>` 的形状:{ast.dump(guard.test)} —— "
        "掺常量(`if False and …`)会让这一支死掉而两个名字还在")
    assert getattr(guard.test.operand, "id", "") == "plan_cell_ids", (
        f"分流条件绑的不是 plan_cell_ids:{ast.dump(guard.test)}")

    # 🔴 结构锁只能证明"形状对";这一支**真的会走**由 w4 包的真 HTTP 判据守
    #    (`test_progress_on_a_task_without_a_v2_plan_is_typed_not_a_zero`)。
    #    两条一起才够 —— 单靠 AST 的那一版被 MUT-E4-04 整发穿过去了。
    # 🔴 分流判据必须是**这次运行有没有 v2 耐久计划**(plan_cell_ids 空),
    #    不是"这个品牌**现在**有没有 enrolled v2"。后者读当前报价快照,
    #    同一次历史运行的答案会随品牌之后买没买 v2 而改变 ——
    #    那正是本单 E3-1 消灭的「现读可变列」形态。
    assert "plan_cell_ids" in dumped, "分流没绑到这次运行的耐久计划上"
    assert "resolve_monitoring_enrollment" not in dumped, (
        "分流用了**当前** brand enrollment —— 历史运行的答案会随品牌状态漂移")


def test_e4_33_the_report_cards_query_is_window_scoped():
    """五卡取格的那条 SQL 必须带**采样窗**,不能只按 brand_id 取全部历史。

    🔴 只按 brand_id 取的后果不是"多算几格":这份报告是冻结的一份结论,
       而卡片会跟着这个品牌未来每一次监测漂 —— 今天一个数、下周另一个数,
       报告却还是同一份。
    """
    src = (ROOT / "api/defensive_geo_report_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cell_queries = []
    for node in ast.walk(tree):
        text = ""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values
                           if isinstance(v, ast.Constant)
                           and isinstance(v.value, str))
        if "monitoring_run_cells" in text and "plan_hash" in text:
            cell_queries.append(text)
    assert cell_queries, "没找到取格的那条 SQL —— 探针失效"
    for q in cell_queries:
        assert "created_at" in q, (
            f"取格 SQL 没有时间窗,会随未来的监测漂:{q}")

    assert "defgeo_report_snapshots" in src, "没有从报告快照取采样窗"
    assert "_CardsUnbound" in src, "没有「绑不了就隐藏」的那条出口"

def test_e4_26_the_ledger_carries_a_durable_model_source_marker(cur):
    """账本上那一列必须**落库**,不是只在进程里算一算。

    🔴 只在 lineage 里打标、不落库,等于"标了给自己看" ——
       事后任何一次对账都拿不到"这一行的模型到底是回显还是计划值"。
    🔴 缺省 ``planned_fallback``:open 那一刻按定义只能是计划值。
       缺省成 provider_echo 会让所有还没接回显的行冒充已证实。
    """
    from services.defensive_geo.monitoring import attempt_ledger as AL

    cur.execute(
        f"INSERT INTO public.{AL.TABLE} "
        " (attempt_id, plan_cell_id, attempt_ordinal, run_authority_id,"
        "  tenant_owner_user_id, brand_id, actual_provider, actual_model,"
        "  actual_surface, actual_search_mode, request_hash, ledger_version)"
        " VALUES (repeat('c',64), repeat('d',64), 1, 't', 4242, 7701,"
        "         'dashscope','planned-model','s','sm','rh','v') RETURNING *")
    row = dict(cur.fetchone())
    assert row["actual_model_source"] == "planned_fallback", row

    # 收口时**没有**回显 ⇒ 来源不许被改成 provider_echo。
    AL.close_attempt(cur, attempt_id=row["attempt_id"],
                     terminal_state="answered", monitoring_result_id=None,
                     observed_model=None)
    cur.execute(f"SELECT actual_model, actual_model_source FROM public.{AL.TABLE} "
                " WHERE attempt_id=%s", (row["attempt_id"],))
    after = dict(cur.fetchone())
    assert after["actual_model_source"] == "planned_fallback", after
    assert after["actual_model"] == "planned-model", after


def test_e4_27_a_real_echo_flips_the_marker(cur):
    """判别力自证:真回显必须把来源改成 ``provider_echo``。"""
    from services.defensive_geo.monitoring import attempt_ledger as AL

    cur.execute(
        f"INSERT INTO public.{AL.TABLE} "
        " (attempt_id, plan_cell_id, attempt_ordinal, run_authority_id,"
        "  tenant_owner_user_id, brand_id, actual_provider, actual_model,"
        "  actual_surface, actual_search_mode, request_hash, ledger_version)"
        " VALUES (repeat('e',64), repeat('f',64), 1, 't', 4242, 7701,"
        "         'dashscope','planned-model','s','sm','rh','v') RETURNING *")
    row = dict(cur.fetchone())
    AL.close_attempt(cur, attempt_id=row["attempt_id"],
                     terminal_state="answered", monitoring_result_id=None,
                     observed_model="qwen-echo-real")
    cur.execute(f"SELECT actual_model, actual_model_source FROM public.{AL.TABLE} "
                " WHERE attempt_id=%s", (row["attempt_id"],))
    after = dict(cur.fetchone())
    assert after["actual_model_source"] == "provider_echo", after
    assert after["actual_model"] == "qwen-echo-real", after

# ══════════════════════════════════════════════════════════════════════
# ④ [本单**新发现**,超出工单 E3-4 范围 —— 已报 Review] 两张计划模型表分叉
# ══════════════════════════════════════════════════════════════════════
#
# 账本 open 那一刻写的 provider/model 取自 ``services.engine_contract.
# PLATFORM_CONTRACT``;而**生产真正派发**用的是
# ``tools.monitoring.batch_monitor._resolve_runtime_lineage``。两张表都自称
# "唯一血缘表",内容却对不上 —— 于是账本记的 provider 与真实调用的 provider
# 在 4 个平台里有 3 个不同。
#
# 🔴 为什么不在本单直接改:改任何一张都会**改变生产记录的模型/供应商字符串**,
#    而那一列同时是保真度对账与计价的取数口(``_estimate_token_cost_placeholder``
#    按 QWEN_ENGINE 定价,账本却记 batch_monitor 的值)。这是商业口径决定,
#    按「小 BUG 直接修 / 大 BUG 交回 Owner」的分界,归 Review/Owner 裁。
#
# 🔴 那本单能做的是:把分叉**钉住**。冻结集大小写死 —— 再多一处分叉就红,
#    修好一处也要红(逼人来改这份清单),而不是让它继续悄悄长大。

#: 已知分叉的冻结集。**只许缩不许扩**,大小由下面那条判据钉死。
_KNOWN_LINEAGE_FORKS: frozenset[tuple[str, str]] = frozenset({
    ("dashscope", "model"),    # qwen3.7-plus(合同) vs qwen3-max(生产实发)
    #: 🔴 [WO_221-c1' 已修复,按 e4_40 的提示缩小] 原「合同写 dashscope、实发 deepseek_official」
    #:   的分叉已闭合:`services/engine_contract.py` 那一格改成 deepseek_official + deepseek-flash。
    #:   成因:2026-07-27 DeepSeek 监测换官方原生检索,合同没跟着改 ⇒
    #:   账本 open 时写进去的 actual_provider/actual_model 一直是百炼那套。
    #:   ('deepseek', 'model') 那条同时消失(同一处改动)。
    ("kimi", "provider"),      # kimi            vs moonshot
    ("doubao", "provider"),    # doubao          vs volc_ark
    ("doubao", "model"),       # 固定值          vs 随 search_mode 变
})


def test_e4_40_the_two_planned_model_tables_have_exactly_the_known_forks():
    """两张"唯一血缘表"的分叉集合必须**逐项等于**冻结集。

    分母 = ``PLATFORM_CONTRACT`` 的平台 × (provider, model),机械枚举,
    不手抄清单 —— 手抄的清单漏掉哪一项都不会让任何判据变红(本仓记过)。
    """
    from services.engine_contract import PLATFORM_CONTRACT
    from tools.monitoring.batch_monitor import _resolve_runtime_lineage

    forks = set()
    for platform, contract in PLATFORM_CONTRACT.items():
        provider, model, _surface, _mode = _resolve_runtime_lineage(
            platform, "enhanced")
        if provider != contract["provider"]:
            forks.add((platform, "provider"))
        if model != contract["model"]:
            forks.add((platform, "model"))

    assert forks == set(_KNOWN_LINEAGE_FORKS), (
        "两张计划模型表的分叉集合变了。"
        f" 新增分叉:{sorted(forks - _KNOWN_LINEAGE_FORKS)};"
        f" 已修复(请同步缩小冻结集):{sorted(_KNOWN_LINEAGE_FORKS - forks)}。"
        " 账本 open 时写的是 PLATFORM_CONTRACT,生产实发的是"
        " batch_monitor._resolve_runtime_lineage;两者不一致时,"
        "账本里的 actual_provider/actual_model 记的不是真实调用。")


def test_e4_41_the_fork_lock_has_a_pinned_size():
    """冻结集大小钉死 —— 防"顺手把新分叉加进白名单"。"""
    assert len(_KNOWN_LINEAGE_FORKS) == 4, (
        f"已知分叉冻结集大小变了({len(_KNOWN_LINEAGE_FORKS)},应为 4)——"
        "改这个数必须同时在交付文里说明为什么")


def test_e4_42_the_runtime_table_covers_every_contract_platform():
    """反向:合同里的每个平台,生产血缘表都必须登记。

    漏登记会让 ``_resolve_runtime_lineage`` 抛 ``MonitoringLineageUnregistered``,
    整格被记成 engine_error —— 客户看到"该平台未返回结果",真因是我们没登记。
    """
    from services.engine_contract import PLATFORM_CONTRACT
    from tools.monitoring.batch_monitor import (
        MonitoringLineageUnregistered, _resolve_runtime_lineage,
    )

    for platform in PLATFORM_CONTRACT:
        try:
            _resolve_runtime_lineage(platform, "enhanced")
        except MonitoringLineageUnregistered:  # pragma: no cover
            raise AssertionError(f"合同平台 {platform} 在生产血缘表里没登记")
