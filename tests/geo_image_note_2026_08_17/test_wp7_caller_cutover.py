"""WP7 · caller census(全量分母自证)+ cutover 一致性锁。

规格 03 §10 的判别测试里有两条是冲着**可维护性**来的:

  「meijiehezi/history/writing-next/home-stats 若展示统一发布数必须走 projector;
    保留 raw attempts 时标签明确,**删 adapter 后一致性测试红**」
  「删除 quote predicate 或 attempt lineage 后测试红」

所以本文件的判据分两类:
  ① census —— 结构锚取全集,每个消费方必须显式归类,**没有默认放过**;
  ② cutover 锁 —— 删掉适配层/删掉 quote predicate/把口径改回品牌级,必须红。
"""
from __future__ import annotations

import ast
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 结构锚 = canonical 发布事实源表名 + 那个众所周知的冗余列。
#: 用表名而不是函数名做锚,是因为**函数名可以被绕过,表名绕不过** ——
#: 想读发布事实就必须提到其中一张表。
ANCHORS = (
    "media_publications",
    "publish_order_items",
    "publish_records",
    "gap_plan_publications",
    "first_published_at",
)


#: 🔴 cutover **成功**的文件可能一张 raw 表都不再提(`writing_next_step.py` 就是:
#:    它原来那条 `mhz_publish_order_items` 子查询被整条换掉了)。所以分母不能只用
#:    raw 表名 —— 那会让"改好了"和"根本不存在"长得一样,census 反而漏掉治理对象。
#:    分母 = 读 raw 表 **或** 走统一适配层。
ADAPTER_ANCHOR = "publication_stage_adapters"


def _anchor_files() -> set[str]:
    out = subprocess.run(
        ["git", "grep", "-l", "-E", "|".join(ANCHORS + (ADAPTER_ANCHOR,)), "--", "*.py"],
        cwd=REPO, capture_output=True, text=True)
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


# ---------------------------------------------------------------------------
# ① census · 全量分母自证
# ---------------------------------------------------------------------------

#: 已 cutover:展示 quote 级"统一发布数",必须走 `publication_stage_adapters`。
CUTOVER = {
    "api/m3_api.py",
    "services/customer_operation_plan.py",
    "services/report_writer_v2.py",
    "services/writing_next_step.py",
    "db/publish_db.py",
    "db/gap_plan_db.py",
}

#: 投影自身(canonical 实现)。
PROJECTOR = {
    "services/publication_stage_projection.py",
    "services/publication_stage_sources.py",
    "services/publication_stage_adapters.py",
    "services/publication_facts.py",
    "services/strict_article_outcomes.py",
    "services/publication_outcome_facts.py",
}

#: **写入方**:产生发布事实的地方。它们不展示统一发布数,不需要 cutover;
#: 但它们写的列正是投影读的列,所以列进 census 而不是默认放过。
WRITERS = {
    # 🔴 [#151 · 2026-09-08 · 窗口 C] 图文成品的发布终态收敛 sweeper。
    #    它**读** `mhz_publish_order_items` 的 status/publish_url,
    #    **写** `geo_douyin_posts.publish_status`/`published_url`
    #    (经既有的 `bind_publish_result`)—— 正是投影要读的那几列,
    #    所以归 WRITERS 而不是 PROJECTOR:它产生发布事实,不展示统一发布数。
    #    ⚠️ 归属说明:本条是**我自己**新加模块带进来的
    #    (父臂 cff1346bb 未归类 6 项,我的尖 7 项,多的就是这一条),
    #    与本文件另外那 6 项存量未归类**无关** —— 那 6 项不是我引入的,
    #    按 Review #161 裁定「不是因删除而红的单列报,不顺手修」,我没碰。
    "services/geo_douyin/publish_convergence.py",
    # [WO_273 · 2026-09-23] 原此处还有插件后端与插件死写入模块两条,文件随插件后端整体删除,
    #   census 同步删(下面「census 里的文件已不存在」那把反向锁本来就不许它们烂着)。
    "api/publish_api.py", "api/meijiehezi_api.py",
    "db/meijiehezi_db.py", "db/monitoring_db.py",
    "db/pipeline_stage_log_db.py", "db/diagnosis_db.py",
    "services/kuaiyibo/routing.py", "services/kuaiyibo/status_sync.py",
    "services/meijiehezi_client.py", "services/media_outcome_sync.py",
    "services/article_publication_snapshot.py", "services/article_generation_reset.py",
    "services/geo_douyin/publish_adapter.py", "services/geo_douyin/publish_command.py",
    "services/geo_douyin/publish_coordinator.py",
    # 🔴 [返工 2026-08-18 · 链 3] 两个**新增**的写入方,显式归类而不是让 census 放过:
    #    · `api/geo_image_note_api.py` —— publish-batch 经 materialize_command
    #      写 `mhz_publish_order_items`;
    #    · `services/geo_douyin/contract_worker.py` —— 提交后写回 item 的
    #      status / settlement_status(资金闭环那一半)。
    #    两者都**不展示**"统一发布数",所以不需要 cutover 到 projector;
    #    但它们写的列正是投影读的列,不列进来 census 就有洞。
    "api/geo_image_note_api.py", "services/geo_douyin/contract_worker.py",
    # 🔴 [并车 2026-08-20 · 36 班] 文章自报包随树带进三个触 publish_records 的
    #    服务件,逐个看过后显式归类(不默认放过):
    #    · `publication_url_verifier.py` —— 服务端探针/人工核实动作,
    #      **写** publish_records 的核实位与可达位(权威链的执行器);
    #    · `publication_receipt_projection.py` —— 回执双轴投影
    #      (project_publication_axes),读 publish_records 行判
    #      receipt/publication 两轴,不展示 quote 级统一发布数,无需 cutover;
    #    · `extension_platform_registry.py` —— 平台域名清单,docstring/常量里
    #      提到 publish_records(锚是字符串匹配,注释也算命中,照既有做法归类)。
    "services/publication_url_verifier.py",
    "services/publication_receipt_projection.py",
    "services/extension_platform_registry.py",
    # 🔴 `contract_ids.py` 只在 **docstring 病历**里提到 order_items 那张表
    #    (它记录的是"哪些列是 uuid")。结构锚是字符串匹配,注释也算命中 ——
    #    这是刻意的:锚要是能被注释绕过,"提到这张表就必须归类"就不成立了。
    #    所以它必须显式归类,而不是给锚加"忽略注释"的例外。
    "services/geo_douyin/contract_ids.py",
    # 🔴 [第 3 棒] `contract_seams.py` 同 `contract_ids.py` 的形态:它在
    #    **docstring 的状态对表**里提到 `mhz_publish_order_items`
    #    (「哪一侧写哪个值」那张表),结构锚是字符串匹配,注释也算命中。
    #    显式归类而不是给锚加"忽略注释"例外 —— 锚要是能被注释绕过,
    #    「提到这张表就必须归类」这条就不成立了。
    #    它自己不产生任何发布事实读数(纯映射,零 I/O),归 WRITERS 一侧
    #    是因为它决定了 worker 往那张表写什么值。
    "services/geo_douyin/contract_seams.py",
    # 🔴 [第 4 棒 · P0-7] `api/geo_douyin_api.py` 同前两例的形态:它在
    #    `/publish-result` 的 docstring 里**逐字引用了 Owner 的裁决原文**
    #    (那句话里有 `publish_records`),结构锚是字符串匹配,注释也算命中。
    #    照既有做法显式归类,**不**给锚加"忽略注释"的例外 ——
    #    锚要是能被注释绕过,「提到这张表就必须归类」这条就不成立了。
    #    它归 WRITERS 是因为 `/publish-result` 确实写作品的 `publish_status`
    #    (第 4 棒起只写**非终态线索值**);它**不读** `publish_records`,
    #    这件事由 `test_r3_p07_fences_pg16.py` 的负向锁(SQL 结构锚)钉死。
    "api/geo_douyin_api.py",
    "services/geo_douyin/account_eligibility.py",
    # 🔴 [WO-B ② 2026-08-20] 小榜五阶段的 execute adapter。它命中结构锚是因为
    #    docstring 里逐字写了资金判据要打的那几列
    #    (`mhz_publish_order_items.reserved_amount` / `freeze_id` / `billing_mode`)——
    #    与 contract_seams.py / geo_douyin_api.py 同一形态:注释也算命中,
    #    照既有做法**显式归类**,不给锚加"忽略注释"的例外
    #    (锚要是能被注释绕过,「提到这张表就必须归类」这条就不成立了)。
    #    归 WRITERS 一侧是因为它是发布 command 产线的**第二个入口**:
    #    它调 publish_batch_core.materialize_publish_batch → publish_coordinator
    #    → 真正 INSERT 那张表。它自己**不读**任何发布事实做展示。
    "services/xiaobang_publish_execute.py",
    "services/organization_artifacts.py", "services/admin_cross_tenant_governance.py",
    "services/publication_content_drift.py", "services/writing_outcome_backfill.py",
    "api/selection_api.py", "api/scheduler.py", "api/auth_api.py", "api/admin_api.py",
    "server.py", "scheduler.py",
}

#: **raw attempts 展示位**:允许保留原始尝试数,但标签必须明确不叫"已发布"。
#: 规格:「保留 raw attempts 时标签明确」。
RAW_ATTEMPT_VIEWS = {
    "api/dashboard_api.py",
    "api/admin_llm_cost_api.py",
    "services/media_publish_success.py",
    "services/channel_effectiveness_report.py",
    "services/writing_effectiveness_report.py",
    "services/ai_ops/report_metrics/publishing.py",
    "services/article_data_health.py",
    "services/article_experiment_registry.py",
    "services/marketing/signal_rules.py",
    "services/article_delivery_plan.py",
    "services/article_closed_loop_contract.py",
    "writing/keyword_topic_generator.py",
    "writing/platform_safety_profiles.py",
}

#: schema 契约 / 迁移清单 —— 不产生运行时读数。
SCHEMA = {
    "db/migration_manifest.py",
    "services/geo_douyin_schema_contract.py",
    "services/organization_schema_contract.py",
    "services/geo_article_v14_schema_contract.py",
    # 🔴 [R5 ⑤ 2026-08-21] brands 的建表/自愈/索引 SSOT 出口(批 1 从 db/diagnosis_db.py
    #    拆出来的零副作用叶子模块)。它命中结构锚**只因为一段注释**:
    #    `_safe_add_column_optional` 的 docstring 里有一张"表归属实查表",其中一行
    #    逐字写着 `media_publications  db/monitoring_db.py`。
    #    与 contract_seams.py / geo_douyin_api.py / xiaobang_publish_execute.py 同一形态:
    #    注释也算命中,照本文件既有做法**显式归类**,不给锚加"忽略注释"的例外
    #    (锚要是能被注释绕过,「提到这张表就必须归类」这条就不成立了)。
    #    归 SCHEMA 而不是 WRITERS:它只发 brands 的 DDL,**既不读也不写**任何发布事实
    #    —— 全文件对 ANCHORS 五张表零 SQL、零 import,唯一出现处就是上面那行注释。
    #    (对照:db/diagnosis_db.py 在 WRITERS,因为它真的写。)
    "db/brands_schema.py",
}

#: 一次性 ops / 回填脚本 —— 不在请求路径上。
SCRIPTS_PREFIX = "scripts/"


def test_caller_census_covers_the_full_denominator():
    files = _anchor_files()
    assert files, "结构锚零命中 —— census 没有分母,后面全是恒真"

    classified = CUTOVER | PROJECTOR | WRITERS | RAW_ATTEMPT_VIEWS | SCHEMA
    ignorable = {f for f in files
                 if f.startswith("tests/") or f.startswith(SCRIPTS_PREFIX)}
    unclassified = files - classified - ignorable
    assert not unclassified, (
        "census 漏了这些发布事实消费方(每个都必须显式归类,不许默认放过):\n%s"
        % sorted(unclassified))

    # 反向对照:census 不许比现实活得久。
    stale = {f for f in classified if not (REPO / f).is_file()}
    assert not stale, f"census 里的文件已不存在:{sorted(stale)}"

    # 反向对照之二:分类之间不许重叠 —— 重叠会让"归到哪一类"失去意义。
    buckets = {"cutover": CUTOVER, "projector": PROJECTOR, "writers": WRITERS,
               "raw": RAW_ATTEMPT_VIEWS, "schema": SCHEMA}
    names = list(buckets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            overlap = buckets[a] & buckets[b]
            assert not overlap, f"{a} 与 {b} 重叠:{sorted(overlap)}"


def test_census_anchor_actually_matches_the_cutover_files():
    """判别力自证:锚必须真的命中每一个已 cutover 的文件。

    锚打不进正样本时,上一条的"零 unclassified"就是恒真 ——
    本仓 2026-08-17 刚为「驳回反驳前先证明探针打得进正样本」交过学费。
    """
    files = _anchor_files()
    missed = CUTOVER - files
    assert not missed, f"结构锚打不到这些已 cutover 的文件:{sorted(missed)}"


# ---------------------------------------------------------------------------
# ② cutover 锁 · 删了就红
# ---------------------------------------------------------------------------

def _src(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


def _code_only(node: ast.AST) -> str:
    """剥掉 docstring 之后的源码文本。

    🔴 为什么必须有这一步:本仓已经**五次**出现"判据命中了自己写的注释"。
       `ast.unparse` 会把 docstring 一起吐出来,于是"禁止出现 all(" 这类判据
       会被解释这条禁令的那句话本身触发 —— 恒红,和恒真一样废。
    """
    clone = ast.parse(ast.unparse(node)).body[0]
    for sub in ast.walk(clone):
        body = getattr(sub, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            sub.body = body[1:] or [ast.Pass()]
    return ast.unparse(clone)


@pytest.mark.parametrize("rel", sorted(CUTOVER - {"db/gap_plan_db.py"}))
def test_cutover_files_go_through_the_adapter(rel):
    """每个已 cutover 的展示位都必须 import 适配层。删适配层 → import 失败 → 红。"""
    assert "publication_stage_adapters" in _src(rel), (
        f"{rel} 没有走统一适配层 —— cutover 被回退了")


def test_gap_plan_goes_through_the_adapter_for_canonical_numbers():
    src = _src("db/gap_plan_db.py")
    assert "publication_stage_adapters" in src
    assert "is_canonical_source" in src


def test_m3_no_longer_reads_first_published_at_as_the_published_count():
    """`articles.first_published_at` 是 P0.4 的冗余列:代发/插件链不写(少算)、
    写上后永不回退(撤稿仍显示已发布,多算)。它不许再当发布数用。"""
    src = _src("api/m3_api.py")
    assert "quote_published_active" in src
    # 反向:确认那条老 SQL 真的没了(不是"新旧并存,老的还在用")
    assert "first_published_at IS NOT NULL" not in src


def test_report_writer_no_longer_merges_brand_wide_publications():
    """周/月报原来 `WHERE q.brand_id = %s` 把品牌下全部报价合成一个数。"""
    src = _src("services/report_writer_v2.py")
    assert "brand_quote_projections" in src
    assert "q.brand_id = %s AND a.first_published_at IS NOT NULL" not in src
    assert "JOIN quotes q ON q.id = mp.quote_id\n                WHERE q.brand_id" not in src


def test_operation_plan_no_longer_maxes_brand_outcome_with_quote_evidence():
    """规格:「客户 operation plan 不再把 brand outcome 与 quote evidence 取 max」。"""
    src = _src("services/customer_operation_plan.py")
    assert "max(int(current or 0), total)" not in src
    assert "gap_plan_checkback" in src


def test_publication_outcome_summary_declares_its_scope():
    """未传 quote_id 时必须自曝是品牌级口径,否则下游会当成本合同的成果渲染。"""
    from services.customer_operation_plan import _publication_outcome_summary

    assert _publication_outcome_summary(None) == {
        "available": False, "load_failed": False,
        "articles_cited": None, "articles_observable": None}
    src = _src("services/customer_operation_plan.py")
    assert '"scope": "brand"' in src and '"scope": "quote"' in src


def test_publish_outcome_no_longer_passes_brand_window_as_causal():
    """规格:「不得用 brand+时间窗/累计 citation 冒充 same-source 因果;
    删除 strict source join 后红」。"""
    src = _src("db/publish_db.py")
    assert "_fetch_strict_same_source_citations" in src
    assert "strict_same_source_citations" in src
    # 老口径的**名字**必须自曝(brand_window_*),不许再叫 ai_citations_delta
    assert "brand_window_detections_30d" in src
    assert 'payload.get("ai_citations_delta_30d")' not in src


def test_strict_scope_check_forbids_brand_form():
    """schema 层挡住品牌级口径从新列回来。"""
    sql = _src("db/migration_036_publication_stage_strict_source_2026_08_18.sql")
    assert "ck_por_strict_scope" in sql
    assert "^quote:[0-9]+$" in sql
    assert "brand:" not in sql.split("CHECK")[1].split(";")[0]


def test_migration_036_is_additive_zero_dml():
    sql = _src("db/migration_036_publication_stage_strict_source_2026_08_18.sql")
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for banned in ("UPDATE ", "DELETE ", "INSERT ", "DROP COLUMN", "DROP TABLE",
                   "ALTER COLUMN", "DROP CONSTRAINT"):
        assert banned not in body.upper(), f"036 出现非 additive 语句:{banned}"
    assert body.upper().count("ADD COLUMN IF NOT EXISTS") == 3


# ---------------------------------------------------------------------------
# ③ 形态锁 · 品牌级合并入口不许存在
# ---------------------------------------------------------------------------

def test_adapter_has_no_brand_merge_entry_point():
    from services.publication_stage_adapters import assert_no_brand_merge_helper

    assert_no_brand_merge_helper()   # 正向


def test_brand_merge_lock_actually_fires():
    """反向对照:上一条不是恒真 —— 真加一个品牌级入口必须被抓到。"""
    import services.publication_stage_adapters as mod

    mod.brand_published_total = lambda brand_id: 0
    try:
        with pytest.raises(AssertionError):
            mod.assert_no_brand_merge_helper()
    finally:
        del mod.brand_published_total
    mod.assert_no_brand_merge_helper()


def test_quote_predicates_present_in_every_source_chain():
    from services.publication_stage_sources import assert_quote_predicates_present

    assert_quote_predicates_present()


def test_quote_predicate_guard_actually_fires():
    """反向对照:抹掉任一条链的 quote predicate,守卫必须红。"""
    import services.publication_stage_sources as mod

    original = mod._ATTEMPTS_SQL
    try:
        mod._ATTEMPTS_SQL = original.replace(
            "WHERE m.quote_id = %(quote_id)s", "WHERE TRUE", 1)
        with pytest.raises(AssertionError):
            mod.assert_quote_predicates_present()
    finally:
        mod._ATTEMPTS_SQL = original
    mod.assert_quote_predicates_present()


def test_new_publication_chain_without_census_entry_fires():
    """新增一条发布链却没登记进 `PUBLICATION_SOURCES` → 段数对不上 → 红。

    这条守的是**未来**:今天四条链都对,明天有人加第五条链忘了登记,
    那条链的 quote 隔离就无人看管。
    """
    import services.publication_stage_sources as mod

    original = mod._ATTEMPTS_SQL
    try:
        mod._ATTEMPTS_SQL = original + "\nUNION ALL\nSELECT 1 FROM foo WHERE TRUE\n"
        with pytest.raises(AssertionError, match="发布链段数"):
            mod.assert_quote_predicates_present()
    finally:
        mod._ATTEMPTS_SQL = original


# ---------------------------------------------------------------------------
# ④ raw attempts 展示位:标签必须明确
# ---------------------------------------------------------------------------

def test_raw_attempt_label_is_not_published():
    from services.publication_stage_adapters import RAW_ATTEMPT_LABEL, stage_labels

    assert "已发布" not in RAW_ATTEMPT_LABEL
    assert "尝试" in RAW_ATTEMPT_LABEL
    # 六阶段的用户面文案不许出现工程术语
    joined = "".join(stage_labels().values())
    for jargon in ("stage", "slot", "token", "draft", "portal", "SOV", "attempt"):
        assert jargon not in joined


def test_stage_labels_cover_every_stage():
    from services.publication_stage_adapters import stage_labels
    from services.publication_stage_projection import STAGES

    assert tuple(stage_labels()) == STAGES


# ---------------------------------------------------------------------------
# ⑤ W6 透传:永不合成 quote 级 complete
# ---------------------------------------------------------------------------

def test_service_completion_never_synthesizes_quote_complete():
    """规格:「W6 未 live 或 quote 聚合未签发时显示 unavailable,不猜 quote 总 complete」。"""
    src = _src("services/publication_stage_sources.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "load_service_completion")
    body = _code_only(fn)
    # 不许出现把 per-keyword 合成一个布尔的写法
    assert not re.search(r"\ball\s*\(", body), "出现了 all(...) —— 那就是在合成 quote 级 complete"
    assert "'quote_complete': None" in body


def test_code_only_stripper_has_discriminating_power():
    """🔴 判据打到自己的注释,本仓已犯过五次。剥离器必须自带两条对照:
    ① docstring 里的 `all(` 必须被剥掉;② 真代码里的 `all(` 必须留下。"""
    doc = ast.parse('def f():\n    """用 all() 合成"""\n    return 1\n').body[0]
    code = ast.parse('def f():\n    return all([1])\n').body[0]
    assert not re.search(r"\ball\s*\(", _code_only(doc)), "docstring 没被剥掉"
    assert re.search(r"\ball\s*\(", _code_only(code)), "剥过头了 —— 真代码也被吃掉"


def test_service_completion_unavailable_when_w6_missing(monkeypatch):
    import services.publication_stage_sources as mod

    monkeypatch.setattr(
        "db.monitoring_db.get_keyword_compliance_summary",
        lambda quote_id: {}, raising=False)
    out = mod.load_service_completion(1)
    assert out["available"] is False
    assert out["reason"] == "quote_aggregate_not_issued"
    assert out["per_keyword"] == []


def test_service_completion_passes_through_per_keyword(monkeypatch):
    """反向对照:上一条不是"永远 unavailable"。"""
    import services.publication_stage_sources as mod

    monkeypatch.setattr(
        "db.monitoring_db.get_keyword_compliance_summary",
        lambda quote_id: {11: {"compliant_days": 30, "service_days": 30},
                          12: {"compliant_days": 3, "service_days": 30}},
        raising=False)
    out = mod.load_service_completion(1)
    assert out["available"] is True
    assert out["quote_complete"] is None          # 永不合成
    by_kw = {row["keyword_id"]: row for row in out["per_keyword"]}
    assert by_kw[11]["complete"] is True and by_kw[12]["complete"] is False


# ---------------------------------------------------------------------------
# ⑥ 门户 `/api/publications/{quote_id}` 必须真的按 quote 取,不按 brand
# ---------------------------------------------------------------------------

def _portal_handler_src() -> str:
    """只取该 handler 的函数体,避免判据被 server.py 别处的同名字符串误伤。"""
    tree = ast.parse(_src("server.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "get_publications_list")
    return ast.unparse(fn)


def test_portal_publications_is_quote_scoped_not_brand_scoped():
    """规格:「门户只查当前 quote,不混入同品牌其他报价」。

    🔴 真缺陷:这个端点的路径参数是 quote_id,原实现却用 `o.brand_id = %s`
       把该品牌**全部报价**的代发订单一起返回。同品牌两张报价(续费/加词是常态)
       互相看得见对方的投放记录。
    """
    body = _portal_handler_src()
    assert "o.brand_id = %s" not in body, "门户又按 brand 拉代发订单了"
    assert "COALESCE(gp.quote_id, a.quote_id, t.quote_id) = %s" in body
    assert "i.brand_id = %s" not in body


def test_portal_returns_contract_stage_tuple_with_versions():
    body = _portal_handler_src()
    assert "quote_stage_tuple" in body
    assert "source_versions" in body
    # raw 条目与合同数字必须分开标注,不许混成一个数
    assert "records_basis" in body and "contract_stages" in body


def test_portal_projection_runs_before_the_connection_closes():
    """🔴 顺序判据:投影复用同一个 cursor,必须在 `conn.close()` **之前**。

    放在 close 之后会永远抛"connection already closed",被 except 吞掉 ——
    门户静默永远看不到合同数字,而且不报错。这类"降级分支把失败吃干净"的形态
    正是本包一路在防的那一类。
    """
    body = _portal_handler_src()
    proj_at = body.index("quote_stage_tuple")
    closes = [m.start() for m in re.finditer(r"conn\.close\(\)", body)]
    after = [c for c in closes if c > proj_at]
    before = [c for c in closes if c < proj_at]
    assert after, "投影之后没有 close —— 连接泄漏"
    assert not before, "投影跑在 conn.close() 之后,cursor 已失效"


def test_home_stats_raw_count_is_labelled():
    """规格:「保留 raw attempts 时标签明确」。"""
    src = _src("api/dashboard_api.py")
    assert '"published_count_basis": "raw_attempts_mhz_chain_only"' in src
    assert "RAW_ATTEMPT_LABEL" in src


def test_monitoring_schedule_uses_the_shared_occurrence_predicate():
    """三处"已发文"守卫必须用**同一个**谓词。

    口径不同会造出"哪个名单都不在"的 quote —— 客户付了钱、发了稿、监测不跑,
    而且不报错。
    """
    src = _src("db/monitoring_db.py")
    assert src.count("published_occurrence_predicate(\"q.id\")") == 3
    # 老守卫的形态必须消失(它只认人工登记链回写的那一列)
    assert "a.first_published_at IS NOT NULL\n                  )" not in src


def test_occurrence_predicate_rejects_placeholder_injection():
    from services.publication_stage_sources import published_occurrence_predicate

    assert "q.id" in published_occurrence_predicate("q.id")
    with pytest.raises(ValueError):
        published_occurrence_predicate("%(quote_id)s")
    with pytest.raises(ValueError):
        published_occurrence_predicate("")


def test_occurrence_predicate_covers_all_four_chains():
    from services.publication_stage_sources import (
        PUBLICATION_SOURCES, published_occurrence_predicate,
    )

    sql = published_occurrence_predicate("q.id")
    for table in ("media_publications", "mhz_publish_order_items",
                  "publish_order_items", "publish_records"):
        assert table in sql, f"occurrence 谓词漏了 {table} 这条链"
    assert sql.count("EXISTS") == len(PUBLICATION_SOURCES)


def test_report_period_delta_is_not_cumulative():
    """🔴 「本期新发 N 篇」不许拿累计投影去填。

    六阶段投影按定义是"截至 cutoff 的累计"。把它直接写进"本期新发",
    数字会一路虚高 —— **换了口径却没换标签**,比读错数更隐蔽。
    正确形态:同一投影取两个 cutoff(期末/期初)相减。
    """
    tree = ast.parse(_src("services/report_writer_v2.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_build_action_attribution_md")
    # 数**调用点**,不数字符串出现次数 —— import 那一行也含这个名字,
    # 按出现次数数会得到 3,判据就变成在数无关的东西。
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "brand_quote_projections"]
    assert len(calls) == 2, f"投影调用点 {len(calls)} 个;只取一个 cutoff = 拿累计冒充本期"
    body = _code_only(fn)
    assert "_period_start" in body
    assert "max(0, int(_active) - int(_active0))" in body


def test_report_breakdown_only_when_multiple_quotes():
    """单报价品牌读起来和以前一样;两张才拆开列 —— 拆开是为了不串,不是为了多话。"""
    body = _code_only(next(
        n for n in ast.walk(ast.parse(_src("services/report_writer_v2.py")))
        if isinstance(n, ast.FunctionDef) and n.name == "_build_action_attribution_md"))
    assert "len(quote_stage_lines) > 1" in body


# ---------------------------------------------------------------------------
# ⑦ 降级分支不许吃掉真错误(本轮自己踩到的那一类)
# ---------------------------------------------------------------------------

class _FakeCursor:
    """够用的假 cursor:任何 SELECT 都返回一行空字典。"""

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchone(self):
        return {}

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self):
        self.rolled_back = 0

    def cursor(self):
        return _FakeCursor()

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


def test_operation_plan_projection_receives_the_real_quote_id(monkeypatch):
    """🔴 本轮真踩到的形态:cutover 块里写了个**作用域里不存在的变量**,
    NameError 被自己写的 `except Exception` 吞掉 → 投影永远走不到,
    界面显示"不可用",而且不报错。

    判据不看源码,看**投影到底有没有被调用、拿到的 quote_id 对不对**。
    """
    import services.customer_operation_plan as cop
    import services.publication_stage_adapters as adapters

    seen: list = []

    def _spy(quote_id, *, cutoff=None, cursor=None):
        seen.append(int(quote_id))
        return {"stages": {"published_active": {"count": 3, "available": True}},
                "source_versions": {"projector": "x"}}

    monkeypatch.setattr(adapters, "quote_stage_tuple", _spy)
    monkeypatch.setattr("db.connection.get_connection", lambda: _FakeConn(), raising=False)

    metrics = cop._load_operational_metrics(4321)
    assert seen == [4321], f"投影没被调到(或 quote_id 错了):{seen}"
    assert metrics["publication"]["published"] == 3
    assert metrics["publication"]["available"] is True


def test_operation_plan_rolls_back_when_projection_fails(monkeypatch):
    """投影失败必须 rollback,否则 PostgreSQL 的 aborted 事务会把**后面本来读得到的**
    section 一起连坐成"不可用" —— 一处失败连累一片,而且看不出真因。"""
    import services.customer_operation_plan as cop
    import services.publication_stage_adapters as adapters

    conn = _FakeConn()

    def _boom(quote_id, *, cutoff=None, cursor=None):
        raise RuntimeError("投影炸了")

    monkeypatch.setattr(adapters, "quote_stage_tuple", _boom)
    monkeypatch.setattr("db.connection.get_connection", lambda: conn, raising=False)

    metrics = cop._load_operational_metrics(4322)
    assert conn.rolled_back >= 1, "投影失败没有 rollback"
    assert metrics["publication"]["available"] is False
