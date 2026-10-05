"""[工单 V3-C · C-3 · Codex 三审 P1-5] 计划模型不许冒充 provider echo。

被修的那件事
------------
``services.monitoring_lineage.build_monitoring_lineage`` 上一版的谓词是::

    model_source = "provider_echo" if str(model or "").strip() else "planned_fallback"

键在「调用方给没给 ``model``」。而唯一的生产调用方
``tools.monitoring.batch_monitor._resolve_runtime_lineage`` 对每个平台**恒返**
一个硬编码计划模型名 ⇒ ``planned_fallback`` **一次都不会触发**,
生产上每一行都被标成 ``provider_echo``。

后果不是"标签难看":
  · ``db/monitoring_db.py`` 用 ``model_source == "provider_echo"`` 决定要不要把
    这个值当 **observed_model** 写进 ``defgeo_monitoring_attempts.actual_model``
    —— 那一列同时是保真度对账与**计价**的取数口;
  · ``comparability_feed.comparable_cells`` 用同一个值决定这一格能不能进 cohort
    —— 进了就等于对客户说"两边跑的是同一个模型",而我们只知道"打算发同一个"。

🔴 这一课本身也要留判据(Review 2026-08-28 立规)
------------------------------------------------
旧代码那段注释把口径写**对**了(「生产实际供的全是计划值,所以 provider_echo
这一档在生产上还不会出现」),下面那一行却干了正相反的事,而且是同一笔
commit 出去的。正确的注释反而抑制了审查 —— 我引它当反证,却没回头查同笔实现。
所以:「诚实列明未做」的段落必须配一发**行为探针**。
:func:`test_e7_23_the_production_shaped_call_really_lands_planned_fallback`
就是那发探针:它不读注释,它跑一遍生产形状的调用再看落什么值。
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

_BASE = dict(
    platform="dashscope", question="有哪些靠谱的合规服务商", target_brand="B",
    full_response="B 是其中之一", response_status="success", is_detected=True,
    mention_type="direct", keyword_source="contract", keyword_type="core",
    keyword_resolver_status="resolved", surface="ai_search",
)


# ══════════════════════════════════════════════════════════════════════════
# 两臂(工单逐字:无回显 → planned_fallback 且 cohort 拒收;有真回显 → provider_echo)
# ══════════════════════════════════════════════════════════════════════════
def test_e7_21_planned_model_is_planned_fallback_and_not_complete():
    """臂 A:调用方给的是**计划**模型名、没声明来源 ⇒ 不许算证实。

    修前:这一臂返 ``provider_echo`` + ``complete``。
    """
    from services import monitoring_lineage as ml

    got = ml.build_monitoring_lineage(
        **_BASE, provider="dashscope", model="qwen3-max")

    assert got.model == "qwen3-max", "计划值本身仍要如实记下来"
    assert got.model_source == ml.MODEL_SOURCE_PLANNED_FALLBACK
    assert got.lineage_status == ml.LINEAGE_STATUS_MODEL_UNCONFIRMED, (
        "计划值行被标成 complete —— 一个叫 actual_model 的列被一个叫 complete "
        "的状态确认过,而两者都只是计划值")
    assert "model_unconfirmed" in got.lineage_error_reason, (
        "「为什么不是 complete」没写进库里,只能回头读代码才知道")


def test_e7_22_only_a_declared_real_echo_counts_as_provider_echo():
    """臂 B:拿到供应商回显、并**显式声明** ⇒ provider_echo + complete。"""
    from services import monitoring_lineage as ml

    got = ml.build_monitoring_lineage(
        **_BASE, provider="dashscope", model="qwen3-max-2026-08-01",
        model_source=ml.MODEL_SOURCE_PROVIDER_ECHO)

    assert got.model == "qwen3-max-2026-08-01", "回显值必须原样落库,不许被计划值盖掉"
    assert got.model_source == ml.MODEL_SOURCE_PROVIDER_ECHO
    assert got.lineage_status == ml.LINEAGE_STATUS_COMPLETE


def test_e7_23_the_production_shaped_call_really_lands_planned_fallback():
    """🔴 行为探针:走**生产真实形状**(adapter payload → normalize)再看落什么。

    上面两条打的是 builder 的直调口。这一条打的是生产真正走的那条:
    adapter 返回的 result dict → ``normalize_lineage_payload`` → 落库字段。
    没有它的话,"builder 改对了但 payload 没把 model_source 带过来"这种
    半接线会让上面两条全绿、生产照旧全错标。
    """
    from services import monitoring_lineage as ml
    from tools.monitoring.batch_monitor import _lineage_model_fields

    # adapter 在**没有回显**时给出的那两列 —— 不手搭,由生产函数算
    fields = _lineage_model_fields({"answer": "..."}, "qwen3-max")
    payload = ml.normalize_lineage_payload(
        {"platform": "dashscope", "sent_question_snapshot": _BASE["question"],
         "target_brand": "B", "response_status": "success",
         "provider": "dashscope", "surface": "ai_search",
         "keyword_source": "contract", "keyword_type": "core",
         "keyword_resolver_status": "resolved", **fields},
        full_response="B 是其中之一", is_detected=True, mention_type="direct")

    assert payload["model_source"] == ml.MODEL_SOURCE_PLANNED_FALLBACK
    assert payload["lineage_status"] == ml.LINEAGE_STATUS_MODEL_UNCONFIRMED
    assert payload["model"] == "qwen3-max"


def test_e7_24_the_probe_would_notice_a_real_echo():
    """判别力自证:同一条链在**有**回显时必须落 provider_echo。

    只留上一条时,把 ``_lineage_model_fields`` 写成恒返 planned_fallback
    照样全绿 —— 那就是零判别力。
    """
    from services import monitoring_lineage as ml
    from tools.monitoring.batch_monitor import _lineage_model_fields

    fields = _lineage_model_fields({"echoed_model": "qwen3-max-2026-08-01"},
                                   "qwen3-max")
    payload = ml.normalize_lineage_payload(
        {"platform": "dashscope", "sent_question_snapshot": _BASE["question"],
         "target_brand": "B", "response_status": "success",
         "provider": "dashscope", "surface": "ai_search",
         "keyword_source": "contract", "keyword_type": "core",
         "keyword_resolver_status": "resolved", **fields},
        full_response="B 是其中之一", is_detected=True, mention_type="direct")

    assert payload["model"] == "qwen3-max-2026-08-01"
    assert payload["model_source"] == ml.MODEL_SOURCE_PROVIDER_ECHO
    assert payload["lineage_status"] == ml.LINEAGE_STATUS_COMPLETE


def test_e7_25_cohort_refuses_planned_and_admits_echo():
    """cohort 准入两臂 —— 用 :func:`build_monitoring_lineage` 的**真输出**驱动。

    🔴 不手搭 ``CellIdentity(model_source="planned_fallback")``:那是判据自己
       构造被测的中间值,builder 怎么改都不会红(本仓记过)。
    """
    from services import monitoring_lineage as ml
    from services.defensive_geo.monitoring import comparability_feed as cf

    def _cell(lineage) -> cf.CellIdentity:
        return cf.CellIdentity(
            question_identity_key="q1", question_revision=1,
            public_platform="dashscope", actual_provider=lineage.provider,
            actual_model=lineage.model, actual_model_revision=None,
            planned_surface=lineage.surface, search_mode=lineage.search_mode,
            run_index=1, model_source=lineage.model_source)

    planned = _cell(ml.build_monitoring_lineage(
        **_BASE, provider="dashscope", model="qwen3-max"))
    echoed = _cell(ml.build_monitoring_lineage(
        **_BASE, provider="dashscope", model="qwen3-max-2026-08-01",
        model_source=ml.MODEL_SOURCE_PROVIDER_ECHO))

    assert cf.comparable_cells([planned]) == (), (
        "计划值格进了 cohort —— 那等于对客户断言两边跑的是同一个模型,"
        "而我们只知道两边打算发同一个模型")
    assert cf.comparable_cells([echoed]) == (echoed,)
    assert cf.comparable_cells([planned, echoed]) == (echoed,)


# ══════════════════════════════════════════════════════════════════════════
# 接线:两个返回点、五个 adapter,一个都不许漏
# ══════════════════════════════════════════════════════════════════════════
def _lineage_returns(func) -> list[ast.Dict]:
    """``PlatformAdapter.query`` 里所有**血缘返回字典**。

    分母机械枚举:凡是键里带 ``provider`` 的 dict 字面量都算 ——
    手抄"有两个返回点"的话,将来加第三个返回点不会让任何判据变红。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "provider" in keys:
            out.append(node)
    return out


def _effective_source(fn) -> str:
    """函数自己的源码 + 它单跳委派到的 ``*_impl``。

    🔴 ``query_doubao_search`` 是个薄壳,真正发请求的是
       ``_query_doubao_search_impl``。只读壳的源码会得出"这个 adapter 拿不到
       原始响应",于是它被错误地放进豁免集 —— 一个**真该接线**的 adapter
       就此永久免检。分母要跟着调用走,不能停在壳上。
    """
    src = inspect.getsource(fn)
    mod = inspect.getmodule(fn)
    for node in ast.walk(ast.parse(textwrap.dedent(src))):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", "")
            if name.endswith("_impl") and getattr(mod, name, None) is not None:
                src += "\n" + inspect.getsource(getattr(mod, name))
    return src


def test_e7_26_every_lineage_return_gets_model_source_from_one_place():
    """两个返回点都必须由 ``_lineage_model_fields`` 铺开 model / model_source。

    🔴 分开在两处各写一遍就是"同一谓词写两处":必有一处没人验,
       而两条路径落的来源不一致时没有任何判据会红(本仓记过)。
    """
    from tools.monitoring.batch_monitor import PlatformAdapter

    dicts = _lineage_returns(PlatformAdapter.query)
    assert len(dicts) >= 2, f"血缘返回点只找到 {len(dicts)} 个 —— 探针失效"

    for d in dicts:
        keys = {k.value for k in d.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        assert "model" not in keys, (
            "这个返回点自己手写了 model —— 必须走 _lineage_model_fields")
        spread = [v for k, v in zip(d.keys, d.values) if k is None]
        assert any(isinstance(v, ast.Call)
                   and getattr(v.func, "id", "") == "_lineage_model_fields"
                   for v in spread), "这个返回点没有铺开 _lineage_model_fields"


def test_e7_27_every_monitoring_adapter_surfaces_the_provider_echo():
    """五个被监测 adapter 一个不漏地把响应里的回显带出来。

    🔴 分母不是手抄的函数名单:取
       ``batch_monitor.PlatformAdapter.SUPPORTED_PLATFORMS`` 的值 —— 真正会被派发
       的那些函数。手抄一份的话,新接一个平台不会让任何判据变红。
    🔴 拿不到原始响应 JSON 的 adapter(元宝走 envelope 抽象)如实豁免,
       并把豁免集**钉住大小** —— 悄悄多豁免一个会当场红。
    """
    import tools.monitoring.batch_monitor as bm

    #: 结构上拿不到原始供应商 JSON 的 adapter。**冻结集**:只减不增。
    _NO_RAW_PAYLOAD = {"query_yuanbao"}

    dispatched = {f.__name__: f
                  for f in bm.PlatformAdapter.SUPPORTED_PLATFORMS.values()}
    assert len(dispatched) >= 4, f"派发表太小,探针失效:{sorted(dispatched)}"

    missing = []
    for name, fn in sorted(dispatched.items()):
        src = _effective_source(fn)
        if "data = response.json()" not in src:
            assert name in _NO_RAW_PAYLOAD, (
                f"{name} 拿不到原始响应 JSON,但不在冻结豁免集里")
            continue
        if "_echoed_model(" not in src:
            missing.append(name)
    assert not missing, f"这些 adapter 没把供应商回显带出来:{missing}"
    assert len(_NO_RAW_PAYLOAD) == 1, "豁免集被悄悄放大了"


def _code_strings(path) -> str:
    """文件里**真正会执行**的字符串字面量,拼成一段文本。

    🔴 为什么不直接扫原文:注释与 docstring 里**引用**这条规则本身
       (「原来这里写死 lineage_status = 'complete'」)会把裸串锁打红 ——
       本仓记过这一条(引用裁决原文触发结构锁)。剔掉 docstring 与
       ``#`` 注释(AST 天然不含后者)之后,锁打的才是行为不是文字。
    """
    import pathlib as _pl

    tree = ast.parse(_pl.Path(path).read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            head = node.body[0] if node.body else None
            if (isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant)
                    and isinstance(head.value.value, str)):
                docs.add(id(head.value))
    return "\n".join(
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and id(n) not in docs)


def test_e7_28_no_production_query_hardcodes_complete_for_monitoring_rows():
    """没有生产代码再手抄 ``lineage_status = 'complete'`` 去筛监测行。

    🔴 C-3 之后 ``complete`` 只留给"模型被回显证实"的行。谁还写死它,
       谁就会**静默少掉一批样本** —— 而样本变少不会让它自己的判据变红。
    """
    import pathlib
    import re

    from services import monitoring_lineage as ml

    root = pathlib.Path(ml.__file__).resolve().parents[1]
    #: 打 ``geo_research_source_signals``(另一张表、另一套三态)的那条不在作用域里。
    _OTHER_TABLE = {"article_structure_analysis.py"}
    pat = re.compile(r"lineage_status\s*=\s*'complete'")
    hits = []
    for f in sorted((root / "services").glob("*.py")):
        if f.name in _OTHER_TABLE:
            continue
        if pat.search(_code_strings(f)):
            hits.append(f.name)
    assert not hits, f"这些文件还在写死 complete:{hits}"
    # 反向自证:豁免的那个文件里确实有这个形状,否则上面的正则可能根本没用
    assert pat.search(_code_strings(root / "services" / "article_structure_analysis.py")),         "正则失效 —— 连已知的那处都匹配不到"


def test_e7_29_recorded_statuses_is_the_ssot_and_excludes_the_broken_one():
    """SSOT 自身:recorded 集合含两档、且**不含** explicit_unknown。"""
    from services import monitoring_lineage as ml

    assert set(ml.LINEAGE_RECORDED_STATUSES) == {
        ml.LINEAGE_STATUS_COMPLETE, ml.LINEAGE_STATUS_MODEL_UNCONFIRMED}
    assert ml.LINEAGE_STATUS_EXPLICIT_UNKNOWN not in ml.LINEAGE_RECORDED_STATUSES, (
        "血缘真丢了的行混进了「如实记录过」的集合")
    sql = ml.recorded_lineage_sql("mr")
    assert sql.startswith("mr.lineage_status IN (")
    for s in ml.LINEAGE_RECORDED_STATUSES:
        assert f"'{s}'" in sql


@pytest.mark.parametrize("payload,expected", [
    ({"model": "deepseek-v4-flash"}, "deepseek-v4-flash"),
    ({"model": "  kimi-k2.6  "}, "kimi-k2.6"),
    ({"output": {"model": "qwen3-max-0801"}}, "qwen3-max-0801"),
    ({"model": ""}, ""),
    ({"output": {}}, ""),
    ({}, ""),
    (None, ""),
    ("not-a-dict", ""),
])
def test_e7_30_echo_extractor_never_invents_a_model(payload, expected):
    """回显抽取器取不到就返空串 —— **不猜**。猜出来的值会被当成"已证实"。"""
    from tools.ai_visibility.ai_tester import _echoed_model

    assert _echoed_model(payload) == expected
