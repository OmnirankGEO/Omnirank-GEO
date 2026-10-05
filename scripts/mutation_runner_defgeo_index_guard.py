"""撕锁 runner · 索引守卫加固包(表绑定 + 三轴负向锁 + readiness 机械分母)。

规矩沿用 ``scripts/mutation_runner_p0_close_publish.py``:
  · 变异 = **就地字符串替换**,锚点必须在目标文件里恰好命中 1 次(不唯一 ⇒ 该发作废,
    不算"杀不掉" —— 锚点不唯一等于在考错题);
  · 还原 = **内存里的原始字节写回 + 逐字节核对**,**禁 git checkout**;
  · 裁定 = 期望红集合与实测红集合**逐项相等**(不是 any());
  · 先跑一遍基线,基线不全绿则整轮作废。

🔴 出题人分两半(本仓 2026-08-24 教训:自己写判据又自己挑变异 = 自己出题自己批改):
   ``SELF-*``  = 交付方(判据作者)自选
   ``EXT-*``   = **独立审计方**提出、交付方未参与挑选
   报数时两半分开报,不合并成一个"全杀"的漂亮数字。

跑法(``TEST_DATABASE_URL`` **必须显式给**,本 runner 没有兜底 DSN)::

    TEST_DATABASE_URL=postgresql://geo_admin:<pw>@localhost:<port>/<db>_test \\
        python scripts/mutation_runner_defgeo_index_guard.py

⚠️ 本 runner 会**就地改源文件**再还原 —— 同一工作树上禁止并发跑第二个 runner。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.defgeo_readiness_gen import READINESS_FILES  # noqa: E402

SUITE = "tests/defensive_geo_w3_2026_08_21/test_migration_index_scope_pg.py"
SN = "test_migration_index_scope_pg.py"

M040 = ROOT / "db" / "migration_040_defgeo_question_plans_2026_08_21.sql"
M044 = ROOT / "db" / "migration_044_defgeo_publish_decision_2026_08_21.sql"
M045 = ROOT / "db" / "migration_045_defgeo_run_dispatch_2026_08_22.sql"
M046 = ROOT / "db" / "migration_046_defgeo_monitoring_lineage_2026_08_22.sql"
M034 = ROOT / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql"
TRGM = ROOT / "scripts" / "migration_mhz_media_name_trgm_2026_07_28.sql"
GOV = ROOT / "scripts" / "migration_admin_user_governance_2026_07_15.sql"
BILL = ROOT / "scripts" / "migration_billing_deduction_idempotency_2026_07_19.sql"
PRICING = ROOT / "scripts" / "migration_pricing_dual_ssot_2026_07_12.sql"
FUND7 = ROOT / "scripts" / "migration_v7_fund_recovery_fencing_2026_07_13.sql"
SHORTCODE = ROOT / "scripts" / "migration_organization_short_code_2026_07_28.sql"
CAMPAIGN = ROOT / "scripts" / "migration_v3_3_managed_campaign.sql"
HARNESS = ROOT / "scripts" / "defgeo_index_ab_harness.py"
SCAN = ROOT / "scripts" / "defgeo_index_guard_scan.py"
FUND6 = ROOT / "scripts" / "migration_v6_fund_recovery_2026_07_13.sql"
GEOOBS = ROOT / "scripts" / "migration_geo_observation_aggregate_basis_2026_07_20.sql"
INVENTORY = ROOT / "scripts" / "defgeo_drop_index_inventory.py"
# R2 收口新增的被测文件(运行期裸 DROP 机制那一组 + runner 自身的兜底 DSN)
SELFRUNNER = ROOT / "scripts" / "mutation_runner_defgeo_index_guard.py"
MONDB = ROOT / "db" / "monitoring_db.py"
DIAGDB = ROOT / "db" / "diagnosis_db.py"
SELFHEAL = ROOT / "scripts" / "m1m2_db_self_heal.py"
SERVERPY = ROOT / "server.py"
SCANRT = ROOT / "scripts" / "defgeo_runtime_drop_scan.py"
SUITEFILE = ROOT / SUITE

TARGETS = {M040, M044, M045, M046, M034, TRGM, GOV, BILL, SCAN,
           PRICING, FUND7, SHORTCODE, CAMPAIGN, HARNESS,
           FUND6, GEOOBS, INVENTORY,
           SELFRUNNER, MONDB, DIAGDB, SELFHEAL, SERVERPY,
           SCANRT, SUITEFILE}

#: 🔴 **落盘产物**(2026-08-24 新规,轴C 起生效):裁定不能只在 console 里 ——
#:    逐发的期望红集合 / 实测红集合 / 判定,连同基线,一并写进这两个文件,
#:    路径写进交付单,Review 亲毒时可以直接比对,不用重跑。
ARTIFACT_DIR = ROOT / "docs" / "AI-CONTEXT" / "AXISC_ARTIFACTS_2026-08-24"
ARTIFACT_JSON = ARTIFACT_DIR / "mutation_verdicts.json"
ARTIFACT_LOG = ARTIFACT_DIR / "mutation_verdicts.txt"

# 🔴 不许有兜底 DSN。陈旧默认 = 静默打错库:R2 之前这里写死的还是 55620/geo_defgeo_w3c_test
#    那一轮的端口和库名,换容器之后照着跑会连到别人的库(或连不上被当成"判据挂了")。
#    缺环境变量就当场响亮失败,不猜。
DB_URL = os.environ.get("TEST_DATABASE_URL", "")
if not DB_URL:
    raise SystemExit(
        "TEST_DATABASE_URL 未设置。本 runner 不提供兜底 DSN(陈旧默认会静默打错库),"
        "请显式给出一次性测试库,例如:\n"
        "  TEST_DATABASE_URL=postgresql://geo_admin:<pw>@localhost:<port>/<db>_test "
        "python scripts/mutation_runner_defgeo_index_guard.py"
    )
if "test" not in DB_URL.rsplit("/", 1)[-1].split("?")[0].lower():
    raise SystemExit(f"安全栓:TEST_DATABASE_URL 的库名不含 'test' —— 拒绝在非一次性库上跑撕锁:{DB_URL}")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def run_criteria() -> tuple[set[str], int, int]:
    env = dict(os.environ, TEST_DATABASE_URL=DB_URL, PYTHONIOENCODING="utf-8")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (p.stdout or "") + (p.stderr or "")
    red: set[str] = set()
    for line in out.splitlines():
        m = re.match(r"^(FAILED|ERROR)\s+(\S+\.py)(::\S+)?$", line.strip())
        if not m:
            continue
        # 🔴 只把 **路径那一段** 砍成文件名;``::`` 之后原样保留。
        #    照抄 mutation_runner_p0_close_publish 的 ``.split("/")[-1]`` 会把
        #    参数化 id 里带 ``/`` 的部分(本包的判据参数就是迁移文件相对路径)
        #    一起砍掉 —— 实测把 21 条红全部归一成 5 个乱码 token,
        #    于是每一发都报"该红没红 + 溢出红",裁定全废。
        red.add("py::" + m.group(2).replace("\\", "/").split("/")[-1] + (m.group(3) or ""))
    g = re.search(r"(\d+) passed", out)
    return red, int(g.group(1)) if g else 0, p.returncode


def q(name: str) -> str:
    return f"py::{SN}::{name}"


# ══════════════════════════════════════════════════════════════════════════
# 变异清单
# ══════════════════════════════════════════════════════════════════════════
def _guard_block(path: Path, index: str) -> str:
    """取出某条 ``@index-guard`` 守卫的**整块原文**(锚注释 → ``END $idxguard$;``)。

    整块字面量抄进源码太长且易抄错,这里现取;锚点唯一性仍由 main() 的
    ``src.count(m["from"]) != 1`` 自证,取错了会当场作废该发。
    """
    src = io.open(path, encoding="utf-8", newline="").read()
    head = f"-- @index-guard {index} ON "
    i = src.index(head)
    j = src.index("END $idxguard$;", i) + len("END $idxguard$;")
    return src[i:j]


def _drop_guard_block(path: Path, index: str) -> str:
    """取出某条 ``@drop-index-guard`` 守卫的整块原文。"""
    src = io.open(path, encoding="utf-8", newline="").read()
    head = f"-- @drop-index-guard {index} ON "
    i = src.index(head)
    j = src.index("END $dropguard$;", i) + len("END $dropguard$;")
    return src[i:j]


_DROPGUARD_FUND6 = _drop_guard_block(FUND6, "uniq_fund_recovery_open")
_POISON_FUND = q("test_naked_drop_would_silently_delete_another_tables_index"
                 "[uniq_fund_recovery_open-fund_recovery_orders]")


_INFLIGHT_BLOCK = _guard_block(M046, "uq_defgeo_attempt_single_inflight")
_OLD_FORM_INFLIGHT = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_defgeo_attempt_single_inflight\n"
    "    ON defgeo_monitoring_attempts (plan_cell_id)\n"
    "    WHERE terminal_state IS NULL;")

_GOV_TUPLE = (
    "(tablename, indexname) IN (\n"
    "          ('admin_user_governance_audits',  'idx_admin_user_governance_audit_subject'),\n"
    "          ('admin_user_governance_audits',  'idx_admin_user_governance_audit_operator'),\n"
    "          ('admin_user_governance_audits',  'uq_admin_user_governance_audits_request_id'),\n"
    "          ('customer_agent_binding_history','uq_customer_agent_binding_history_active'),\n"
    "          ('customer_agent_binding_history','uq_customer_agent_binding_history_created_request'),\n"
    "          ('customer_agent_binding_history','idx_customer_agent_binding_history_timeline')\n")
_GOV_NAME_ONLY = (
    "indexname IN (\n"
    "          'idx_admin_user_governance_audit_subject',\n"
    "          'idx_admin_user_governance_audit_operator',\n"
    "          'uq_admin_user_governance_audits_request_id',\n"
    "          'uq_customer_agent_binding_history_active',\n"
    "          'uq_customer_agent_binding_history_created_request',\n"
    "          'idx_customer_agent_binding_history_timeline'\n")

_DECOY_INFLIGHT = q(
    "test_decoy_same_named_index_makes_replay_fail_loudly"
    "[uq_defgeo_attempt_single_inflight-defgeo_monitoring_attempts-True-"
    "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql]")
_DECOY_QPLAN = q(
    "test_decoy_same_named_index_makes_replay_fail_loudly"
    "[idx_defgeo_qplan_brand_latest-defgeo_question_plans-False-"
    "db/migration_040_defgeo_question_plans_2026_08_21.sql]")
_READY_SET = {t: q(f"test_readiness_expected_set_equals_declared_set[{t}-{r}]")
              for t, r in READINESS_FILES.items()}
_READY_DENOM = {t: q(f"test_readiness_index_denominator_is_nonzero[{t}-{r}]")
                for t, r in READINESS_FILES.items()}
_READY_BOUND = {t: q(f"test_readiness_block_actually_checks_indexes_bound_to_table[{t}-{r}]")
                for t, r in READINESS_FILES.items()}

#: 🔴 **重放依赖型**判据:它们各自现跑一次 manifest 重放。
#:    凡是让某个迁移重放失败、或让 readiness 报定义漂移的变异,这一组一定跟着红。
#:    集中成常量,免得逐发手抄(抄漏一条就是一次"溢出红"误判)。
_REPLAY_DEPENDENT = {
    q(f"test_naked_drop_would_silently_delete_another_tables_index[{i}-{tb}]")
    for i, tb in (("uniq_fund_recovery_open", "fund_recovery_orders"),
                  ("idx_geoplan_brand_status", "geo_plan_tasks"),
                  ("uq_diag_refund_extref", "diagnosis_refund_records"))
} | {
    q(f"test_without_decoy_the_drop_still_does_its_job[{i}-{tb}]")
    for i, tb in (("uniq_fund_recovery_open", "fund_recovery_orders"),
                  ("idx_geoplan_brand_status", "geo_plan_tasks"),
                  ("uq_diag_refund_extref", "diagnosis_refund_records"))
} | {
    # R2 收口:strict 站点那三条也各自现跑 replay(`_replayed(probe_db(...))`),
    # 凡是打断重放的变异都会连带把它们打红 —— 收进常量,别逐发手抄。
    q("test_strict_site_absent_index_diverts_before_the_undefined_object_leg"),
    q("test_strict_site_wrong_table_diverts_before_the_wrong_object_type_leg"),
    q("test_strict_site_live_leg_really_drops_and_rebuilds"),
}


#: 🔴 **锚依赖型**判据:它们都要先 `scan_bound_index_guards` 找到 `@index-guard` 锚。
#:    把那条锚正则挖空 ⇒ 8 条 A1 档的"现推"推不出期望表、两对 drop-then-recreate
#:    也找不到声明处。同样收成常量,免得逐发手抄。
_ANCHOR_DEPENDENT = {
    q(f"test_drop_guard_expected_table_matches_its_provenance[{i:02d}A1:{n}]")
    for i, n in ((0, "uq_mhz_item_live_revision_ro"), (1, "uq_selfserve_active_idem"),
                 (2, "idx_geoplan_brand_status"), (3, "uniq_fund_recovery_open"),
                 (4, "uniq_fund_recovery_open"), (5, "uniq_fund_recovery_geoplan_o"),
                 (6, "idx_geoplan_brand_status"), (7, "uniq_fund_recovery_channel_o"))
} | {q("test_drop_then_recreate_pairs_end_up_with_the_widened_definition")}


MUTATIONS: list[dict] = [
    # ── SELF:交付方(判据作者)自选 ──────────────────────────────────
    {
        "id": "SELF-01", "file": M046,
        "desc": "把 partial unique 守卫整块退回 CREATE UNIQUE INDEX IF NOT EXISTS(= 本单整个没做)",
        "from": _INFLIGHT_BLOCK, "to": _OLD_FORM_INFLIGHT,
        "expect": {
            q("test_axis_a_catches_inverted_guard_branch"),
            q("test_every_guard_is_byte_identical_to_rewriting_the_base_version"),
            q("test_axis_a_no_ifne_index_statement_remains"),
            q("test_axis_a_guard_defect_detector_has_discriminating_power"),
            _READY_SET["046"],
            _DECOY_INFLIGHT,
            q("test_readiness_catches_missing_index_when_guard_is_neutered"),
        },
        "expect_note": "旧形态回来 ⇒ 轴A 红;守卫没了 ⇒ 判别力样本的锚点没了;"
                       "@index-guard 锚没了 ⇒ 该索引不在声明集里而 readiness 里还有 ⇒ 幽灵对象红;"
                       "诱饵在 ⇒ 静默跳过 ⇒ 毒夹具红;"
                       "neuter 判据的变异锚点就打在这条 CREATE 上 ⇒ 锚点没了当场红",
    },
    {
        "id": "SELF-02", "file": M046,
        "desc": "把异表同名的 RAISE EXCEPTION 降级成 RAISE NOTICE(守卫退回假守卫的核心形态)",
        "from": "        RAISE EXCEPTION '[index-guard] uq_defgeo_attempt_single_inflight ",
        "to": "        RAISE NOTICE '[index-guard] uq_defgeo_attempt_single_inflight ",
        "expect": {
            q("test_axis_a_anchor_count_equals_guard_body_count"),
            q("test_every_guard_is_byte_identical_to_rewriting_the_base_version"),
            q("test_axis_a_every_guard_has_all_three_legs"),
            q("test_axis_a_guard_defect_detector_has_discriminating_power"),
            _DECOY_INFLIGHT,
        },
    },
    {
        "id": "SELF-03", "file": M040,
        "desc": "把 040 守卫**承重 IF 分支**的 indrelid 绑定拆掉(RAISE 子查询里仍有 indrelid 这个词)",
        "from": ("WHERE c.relname = 'idx_defgeo_qplan_brand_latest' AND "
                 "i.indrelid = to_regclass('public.defgeo_question_plans')) THEN"),
        "to": "WHERE c.relname = 'idx_defgeo_qplan_brand_latest') THEN",
        "expect": {
            q("test_every_guard_is_byte_identical_to_rewriting_the_base_version"),
            q("test_axis_a_every_guard_has_all_three_legs"),
            _DECOY_QPLAN,
        },
        "expect_note": "第一版缺腿检测只查 body 里有没有 indrelid 这个词,这一发当时**存活**;"
                       "现在比的是逐字模板,必须红",
    },
    {
        "id": "SELF-04", "file": SCAN,
        "desc": "把旧形态扫描正则挖空(census 负向锁的活性自证:尺子瞎了必须有人喊)",
        "from": 'r"CREATE\\s+(?P<uniq>UNIQUE\\s+)?INDEX\\s+(?P<conc>CONCURRENTLY\\s+)?"',
        "to": 'r"ZZ_NEVER_MATCHES_(?P<uniq>UNIQUE\\s+)?(?P<conc>CONCURRENTLY\\s+)?"',
        "expect": {
            q("test_axis_a_catches_quoted_identifier_form"),
            q("test_do_block_comments_are_comments_but_execute_strings_are_not"),
            q("test_every_guard_is_byte_identical_to_rewriting_the_base_version"),
            q("test_axis_a_scanner_catches_every_old_shape"),
},
        "expect_note": "轴A 的 ==0 反而更绿 —— 正是这一发要证明的:没有活性判据时,把尺子挖空是隐形的",
    },
    {
        "id": "SELF-05", "file": SCAN,
        "desc": "把新守卫的锚正则挖空(分母自证:表绑定守卫数塌到 0)",
        "from": 'r"--\\s*@index-guard\\s+(?P<idx>[A-Za-z_][A-Za-z_0-9$]*)\\s+ON\\s+"',
        "to": 'r"--\\s*@ZZ-never-guard\\s+(?P<idx>[A-Za-z_][A-Za-z_0-9$]*)\\s+ON\\s+"',
        "expect": {
            q("test_axis_a_anchor_count_equals_guard_body_count"),
            q("test_axis_a_catches_inverted_guard_branch"),
            q("test_every_declared_index_lands_on_its_target_table_after_cold_replay"),
            q("test_every_guard_is_byte_identical_to_rewriting_the_base_version"),
            # R2:动态形态那两条自证也吃 scan_bound_index_guards 的分母
            q("test_dynamic_index_site_is_genuinely_outside_the_census"),
            q("test_dynamic_generated_names_never_collide_with_declared_indexes"),
            q("test_axis_a_no_ifne_index_statement_remains"),
            q("test_axis_a_unique_guards_stay_unique"),
            q("test_axis_a_guard_defect_detector_has_discriminating_power"),
            *_READY_DENOM.values(),
            *_READY_SET.values(),
            *_ANCHOR_DEPENDENT,
        },
    },
    {
        "id": "SELF-06", "file": TRGM,
        "desc": "轴B:把 mhz trgm「索引建成了没」反查的 indrelid 绑定去掉(同块里另一条谓词仍绑着)",
        "from": "           AND ix.indrelid = to_regclass('public.mhz_media')\n", "to": "",
        "expect": {
            q("test_axis_b_no_unbound_index_name_predicate_remains"),
            q("test_axis_b_lock_catches_each_binding_reverted[axisb1]"),
            q("test_axis_b_lock_catches_each_binding_reverted[axisb2]"),
        },
        "expect_note": "第一版轴B 作用域是块级,同块里另一条谓词的 indrelid 替它顶了绿,这一发当时**存活**",
    },
    {
        "id": "SELF-07", "file": M045,
        "desc": "轴B:把 045 领取索引反查的 tablename 绑定去掉",
        "from": "           AND tablename = 'diagnosis_runs'\n", "to": "",
        "expect": {
            q("test_axis_b_no_unbound_index_name_predicate_remains"),
            q("test_axis_b_lock_catches_each_binding_reverted[axisb0]"),
        },
    },
    {
        "id": "SELF-08", "file": M034,
        "desc": "轴B:把 034 pg_get_indexdef 取行的 indrelid 绑定去掉(它还驱动一条 DROP INDEX)",
        "from": ("      JOIN pg_index ix ON ix.indexrelid = c.oid\n"
                 "     WHERE c.relname = 'uq_mhz_item_live_revision_root'\n"
                 "       AND ix.indrelid = to_regclass('public.mhz_publish_order_items');"),
        "to": "     WHERE c.relname = 'uq_mhz_item_live_revision_root';",
        "expect": {
            q("test_axis_b_no_unbound_index_name_predicate_remains"),
            q("test_axis_b_lock_catches_each_binding_reverted[axisb3]"),
            # R2 收口:这条绑定就是 strict 站点的**上游分流**。拆掉之后,
            # "同名索引长在别的表上" 会一路走进 DROP 守卫、由 [drop-index-guard] 报错,
            # 而不再被下游 [index-guard] 拦下 —— 不可达的那条腿当场变可达。
            q("test_strict_site_wrong_table_diverts_before_the_wrong_object_type_leg"),
        },
        "expect_note": "轴B 的绑定同时在替 strict 站点挡门 —— 一条绑定坏掉,"
                       "两根轴的判据一起响",
    },
    {
        "id": "SELF-09", "file": GOV,
        "desc": "轴B:把 governance 索引集合反查从 (tablename, indexname) 退回只数名字",
        "from": _GOV_TUPLE, "to": _GOV_NAME_ONLY,
        "expect": {
            q("test_axis_b_no_unbound_index_name_predicate_remains"),
            q("test_axis_b_lock_catches_each_binding_reverted[axisb4]"),
        },
    },
    {
        "id": "SELF-10", "file": M046,
        "desc": "readiness 期望集合里删掉一条约束(手挑子集的老毛病)",
        "from": ("        ('uq_defgeo_attempt_cell_ordinal', 'public.defgeo_monitoring_attempts', "
                 "'UNIQUE (plan_cell_id, attempt_ordinal)'),\n"),
        "to": "",
        "expect": {
            q("test_readiness_block_is_byte_identical_to_generator_output"
              "[046-db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql]"),
            _READY_SET["046"],
},
    },
    {
        "id": "SELF-11", "file": M046,
        "desc": "readiness 索引循环退回按名判存(不绑表)—— 本单要修的病长在 readiness 上",
        "from": "         WHERE c.relname = r.cname AND i.indrelid = r.tname::regclass;",
        "to": "         WHERE c.relname = r.cname;",
        "expect": {
            q("test_readiness_block_is_byte_identical_to_generator_output"
              "[046-db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql]"),
            _READY_BOUND["046"],
            q("test_axis_b_no_unbound_index_name_predicate_remains"),
        },
        "expect_note": "这一发退回去的那条循环**本身就是**一条不绑表的索引名判存 ——\n                       轴B 的锁跨过来把它也抓住,是两根轴该有的交叉覆盖",
    },
    {
        "id": "SELF-12", "file": M045,
        "desc": "轴C:新增一处 DROP INDEX(冻结集合必须当场红)",
        "from": "-- @index-guard idx_diag_runs_defgeo_undispatched ON diagnosis_runs plain",
        "to": ("DROP INDEX IF EXISTS zz_mutation_probe_idx;\n"
               "-- @index-guard idx_diag_runs_defgeo_undispatched ON diagnosis_runs plain"),
        "expect": {q("test_axis_c_no_naked_drop_index_remains")},
    },
    {
        "id": "SELF-13", "file": M044,
        "desc": "把一条 UNIQUE 守卫的 UNIQUE 拿掉(唯一性静默消失,索引照样建成)",
        "from": "        CREATE UNIQUE INDEX defgeo_pcmd_one_live_per_slot ON public.defgeo_publish_commands",
        "to": "        CREATE INDEX defgeo_pcmd_one_live_per_slot ON public.defgeo_publish_commands",
        "expect": {
            q("test_every_declared_index_lands_on_its_target_table_after_cold_replay"),
            *_REPLAY_DEPENDENT,
            q("test_every_guard_is_byte_identical_to_rewriting_the_base_version"),
            q("test_axis_a_unique_guards_stay_unique"),
            q("test_axis_a_every_guard_has_all_three_legs"),
            q("test_readiness_catches_missing_index_when_guard_is_neutered"),
            q("test_partial_unique_single_inflight_is_actually_enforced"),
            q("test_readiness_catches_index_definition_drift"),
            q("test_replay_twice_is_a_noop"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[idx_defgeo_qplan_brand_latest-defgeo_question_plans-False-"
              "db/migration_040_defgeo_question_plans_2026_08_21.sql]"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[idx_mhz_media_name_trgm-mhz_media-False-"
              "scripts/migration_mhz_media_name_trgm_2026_07_28.sql]"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[uq_defgeo_attempt_single_inflight-defgeo_monitoring_attempts-True-"
              "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql]"),
        },
        "expect_note": "锚说 unique 而建的不是 ⇒ UNIQUE 计数锁 + 逐字模板锁各报各的;\n                       建出来的索引与 044 readiness 的机械期望串不等 ⇒ 重放当场「定义漂移」\n                       ⇒ **凡是自己现跑 replay 的判据全红**(这正是 readiness 在承重的证据)",
    },
    {
        "id": "SELF-14", "file": M046,
        "desc": "把 partial unique 的谓词改宽(索引在、但唯一性覆盖的行集变了)",
        "from": ("        CREATE UNIQUE INDEX uq_defgeo_attempt_single_inflight "
                 "ON public.defgeo_monitoring_attempts (plan_cell_id) WHERE terminal_state IS NULL;"),
        "to": ("        CREATE UNIQUE INDEX uq_defgeo_attempt_single_inflight "
               "ON public.defgeo_monitoring_attempts (plan_cell_id) "
               "WHERE terminal_state IS NULL AND brand_id > 0;"),
        "expect": {
            q("test_every_declared_index_lands_on_its_target_table_after_cold_replay"),
            *_REPLAY_DEPENDENT,
            q("test_every_guard_is_byte_identical_to_rewriting_the_base_version"),
            q("test_readiness_catches_missing_index_when_guard_is_neutered"),
            q("test_partial_unique_single_inflight_is_actually_enforced"),
            q("test_readiness_catches_index_definition_drift"),
            q("test_replay_twice_is_a_noop"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[idx_defgeo_qplan_brand_latest-defgeo_question_plans-False-"
              "db/migration_040_defgeo_question_plans_2026_08_21.sql]"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[idx_mhz_media_name_trgm-mhz_media-False-"
              "scripts/migration_mhz_media_name_trgm_2026_07_28.sql]"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[uq_defgeo_attempt_single_inflight-defgeo_monitoring_attempts-True-"
              "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql]"),
        },
        "expect_note": "readiness 期望串是机械导出的旧定义 ⇒ 重放当场「定义漂移」;"
                       "凡是自己现跑 replay 的判据都会红,这一发考的是 readiness 真的在比定义",
    },
    # ── EXT:独立审计方(2026-08-24)提出,交付方未参与挑选 ──────────────
    #    这一批里 10 发在提出时**全部存活**,是它们逼出了本包对扫描器/判据的返修。
    #    现在挂进 runner,是为了让同样的绕过以后不能再回来。
    {
        "id": "EXT-01", "file": PRICING,
        "desc": "IM-01 · 用双引号索引名把旧形态偷渡进 manifest(加两个引号就绕过轴A)",
        "from": "-- @index-guard ux_recharge_orders_idem ON recharge_orders unique",
        "to": ('CREATE UNIQUE INDEX IF NOT EXISTS "ux_recharge_orders_idem2" '
               "ON public.recharge_orders (user_id, idempotency_key);\n"
               "-- @index-guard ux_recharge_orders_idem ON recharge_orders unique"),
        "expect": {q("test_axis_a_no_ifne_index_statement_remains")},
        "expect_note": "提出时存活(_IFNE_HEAD 只认裸标识符);扫描器补了引号形态后必须红",
    },
    {
        "id": "EXT-02", "file": M034,
        "desc": "IM-03 · 把承重分支 IF EXISTS 改成 IF NOT EXISTS(一个词让索引一辈子建不出来)",
        "from": ("    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid\n"
                 "                WHERE c.relname = 'uq_publish_idem_command_id'"),
        "to": ("    IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid\n"
               "                WHERE c.relname = 'uq_publish_idem_command_id'"),
        "expect": {
            q("test_axis_a_every_guard_has_all_three_legs"),
            q("test_every_guard_is_byte_identical_to_rewriting_the_base_version"),
            q("test_every_declared_index_lands_on_its_target_table_after_cold_replay"),
        },
        "expect_note": "提出时三条腿全绿、真 PG16 上发布幂等 UNIQUE 静默消失且重放零报错",
    },
    {
        "id": "EXT-03", "file": M044,
        "desc": "IM-05 · 把 044 readiness 的绑表条件短路成 (… OR TRUE)",
        "from": "i.indrelid = r.tname::regclass",
        "to": "(i.indrelid = r.tname::regclass OR TRUE)",
        "expect": {
            q("test_readiness_block_is_byte_identical_to_generator_output"
              "[044-db/migration_044_defgeo_publish_decision_2026_08_21.sql]"),
        },
        "expect_note": "提出时存活(旧判据是 substring);逐字锁补上后必须红",
    },
    {
        "id": "EXT-04", "file": M044,
        "desc": "IM-06 · 把 044 的「索引不在目标表上」分支变死代码(IF actual IS NULL → IF FALSE)",
        "from": "IF actual IS NULL THEN\n            RAISE EXCEPTION\n                '[044] ",
        "to": "IF FALSE THEN\n            RAISE EXCEPTION\n                '[044] ",
        "expect": {
            q("test_canonical_readiness_lock_has_discriminating_power"),
            q("test_readiness_block_is_byte_identical_to_generator_output"
              "[044-db/migration_044_defgeo_publish_decision_2026_08_21.sql]"),
        },
        "expect_note": "提出时存活;040/041/044 当时都没有这一支的行为判据",
    },
    {
        "id": "EXT-05", "file": M046,
        "desc": "IM-07 · 把 DML 藏进 EXECUTE 串(行首是 EXECUTE ⇒ 零 DML 判据看不见)",
        "from": "DO $readiness046$\nDECLARE r RECORD; actual TEXT;\nBEGIN\n",
        "to": ("DO $readiness046$\nDECLARE r RECORD; actual TEXT;\nBEGIN\n"
               "    EXECUTE 'DELETE FROM defgeo_monitoring_attempts "
               "WHERE terminal_state IS NULL';\n"),
        "expect": {
            q("test_migration_body_stays_zero_dml"
              "[db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql]"),
            q("test_readiness_block_is_byte_identical_to_generator_output"
              "[046-db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql]"),
        },
        "expect_note": "提出时存活;prestart 每次部署都会把在飞 attempt 删光而判据报绿",
    },
    {
        "id": "EXT-06", "file": FUND7,
        "desc": "IM-08 · 用逗号列表把第二个索引名藏进 DROP INDEX(原锚已被轴C 消费,改锚到守卫体内)",
        "from": "        DROP INDEX IF EXISTS uniq_fund_recovery_open;",
        "to": "        DROP INDEX IF EXISTS uniq_fund_recovery_open, ux_recharge_orders_idem;",
        "expect": {
            q("test_axis_c_no_naked_drop_index_remains"),
            q("test_axis_c_every_drop_guard_has_all_three_legs"),
            q("test_every_drop_guard_is_byte_identical_to_rewriting_the_base_version"),
            q("test_every_declared_index_lands_on_its_target_table_after_cold_replay"),
        },
        "expect_note": "🔴 **锚点迁移记录**:提出时(R1)它打的是 FUND7 里的裸 "
                       "`DROP INDEX IF EXISTS uniq_fund_recovery_open;`,当时轴C 只有冻结集、"
                       "这一发存活(真 PG16 实测充值订单幂等 UNIQUE 被每次部署永久删掉)。"
                       "轴C 把那条裸 DROP 换成了守卫,原锚不复存在 ⇒ 改锚到守卫体内的真删语句,"
                       "考的仍是同一件事:逗号列表的第二个名字不许逃出扫描口径。",
    },
    {
        "id": "EXT-07", "file": SHORTCODE,
        "desc": "IM-10 · 把索引名判存的操作数写反('lit' = relname)⇒ 整条谓词退出轴B 分母",
        "from": "    IF NOT EXISTS (\n        SELECT 1\n          FROM pg_index i\n",
        "to": ("    IF NOT EXISTS (SELECT 1 FROM pg_index i2 JOIN pg_class c2 "
               "ON c2.oid = i2.indexrelid\n"
               "                    WHERE 'organizations_short_code_unique' = c2.relname) THEN\n"
               "        RAISE EXCEPTION 'short_code index missing';\n    END IF;\n"
               "    IF NOT EXISTS (\n        SELECT 1\n          FROM pg_index i\n"),
        "expect": {q("test_axis_b_no_unbound_index_name_predicate_remains")},
        "expect_note": "提出时存活;preds 计数不变、unbound 仍为 0",
    },
    {
        "id": "EXT-08", "file": SCAN,
        "desc": "IM-13 · 扫描器返回处切掉尾部 5 个迁移(恰好是 042–046)⇒ 四条计数地板一条不响",
        "from": "    return [root / rel for rel in manifest_relpaths(root)]",
        "to": "    return [root / rel for rel in manifest_relpaths(root)][:112]",
        "expect": {q("test_manifest_denominator_is_live")},
        "expect_note": "提出时存活(files 112≥110 / bound 432≥400 / unique 110≥100 / preds 24≥20 / drops==19);"
                       "改成集合锁后必须红",
    },
    {
        "id": "EXT-09", "file": HARNESS,
        "desc": "IM-14 · 把 A/B 台架的 identical 判定改成常量(等价性证据从此恒真)",
        "from": '        same = all(not v["only_in_a"] and not v["only_in_b"] for v in report.values())',
        "to": "        same = True",
        "expect": {q("test_ab_harness_diff_actually_detects_a_difference")},
        "expect_note": "提出时存活:全仓 tests/ 无一处 import 这个台架",
    },
    {
        "id": "EXT-10", "file": CAMPAIGN,
        "desc": "IM-16 · 把建索引腿的列清单收紧((brand_id, keyword) → (brand_id))",
        "from": ("        CREATE UNIQUE INDEX idx_campaigns_unique_active "
                 "ON public.managed_campaigns (brand_id, keyword) WHERE status = 'active';"),
        "to": ("        CREATE UNIQUE INDEX idx_campaigns_unique_active "
               "ON public.managed_campaigns (brand_id) WHERE status = 'active';"),
        "expect": {q("test_every_guard_is_byte_identical_to_rewriting_the_base_version")},
        "expect_note": "提出时存活:逐字模板只比到 `ON public.<表> ` 前缀为止,"
                       "列清单与 WHERE 谓词此前完全不在判据面里",
    },
    {
        "id": "EXT-11", "file": CAMPAIGN,
        "desc": "IM-17 · 只删锚注释(运行期行为一点不变,索引却静默退出全部机械分母)",
        "from": "-- @index-guard idx_campaigns_unique_active ON managed_campaigns unique\n",
        "to": "",
        "expect": {
            q("test_axis_a_anchor_count_equals_guard_body_count"),
            q("test_every_guard_is_byte_identical_to_rewriting_the_base_version"),
        },
        "expect_note": "提出时存活:459→458、unique 117→116,所有地板照样绿",
    },
    # ── AXISC:轴C(三单)DROP 守卫 ─────────────────────────────────────
    {
        "id": "AXISC-01", "file": FUND6,
        "desc": "把资金域的 DROP 守卫整块退回裸 DROP INDEX(= 轴C 整个没做)",
        "from": _DROPGUARD_FUND6, "to": "DROP INDEX IF EXISTS uniq_fund_recovery_open;",
        "expect": {
            q("test_axis_c_no_naked_drop_index_remains"),
            q("test_no_create_guard_is_shadowed_by_a_naked_drop"),
            q("test_adjudication_covers_every_drop_guard"),
            q("test_drop_guard_expected_table_matches_its_provenance[03A1:uniq_fund_recovery_open]"),
            q("test_every_drop_guard_is_byte_identical_to_rewriting_the_base_version"),
            _POISON_FUND,
        },
        "expect_note": "裸 DROP 回来 ⇒ 轴C 负向锁 + §7.1 遮蔽锁双红;"
                       "守卫没了 ⇒ 审定双向锁 + 逐字等价锁 + 毒夹具红",
    },
    {
        "id": "AXISC-02", "file": FUND6,
        # R2 收口:原 desc 写的是"降级成 NULL",与下面的 from/to 不符(实际是
        # RAISE EXCEPTION → RAISE NOTICE)。desc 会被落盘产物原样带走,写错就等于
        # 给读产物的人一份错口径 —— 按代码改文案,不是按文案改代码。
        "desc": "把 DROP 守卫的 RAISE EXCEPTION 降级成 RAISE NOTICE"
                "(不中断 ⇒ 异表同名重新变成静默跳过)",
        "from": ("        RAISE EXCEPTION '[drop-index-guard] uniq_fund_recovery_open "
                 "不在 fund_recovery_orders 上"),
        "to": ("        RAISE NOTICE '[drop-index-guard] uniq_fund_recovery_open "
               "不在 fund_recovery_orders 上"),
        "expect": {
            q("test_axis_c_every_drop_guard_has_all_three_legs"),
            q("test_every_drop_guard_is_byte_identical_to_rewriting_the_base_version"),
        },
        "expect_note": "RAISE NOTICE 不中断 ⇒ 守卫走完什么都没做,诱饵表的索引保住了但目标表也没删成 ——"
                       "毒夹具断言「重放必须响亮失败」当场红",
    },
    {
        "id": "AXISC-03", "file": FUND6,
        "desc": "把 DROP 守卫的期望表改成另一张真表(绑错表 = 该删的删不掉)",
        "from": "i.indrelid = to_regclass('fund_recovery_orders')) THEN\n        DROP INDEX IF EXISTS uniq_fund_recovery_open;",
        "to": "i.indrelid = to_regclass('point_freezes')) THEN\n        DROP INDEX IF EXISTS uniq_fund_recovery_open;",
        "expect": {
            q("test_axis_c_every_drop_guard_has_all_three_legs"),
            q("test_every_drop_guard_is_byte_identical_to_rewriting_the_base_version"),
            q("test_every_declared_index_lands_on_its_target_table_after_cold_replay"),
            *_REPLAY_DEPENDENT,
            q("test_partial_unique_single_inflight_is_actually_enforced"),
            q("test_readiness_catches_index_definition_drift"),
            q("test_readiness_catches_missing_index_when_guard_is_neutered"),
            q("test_replay_twice_is_a_noop"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[idx_defgeo_qplan_brand_latest-defgeo_question_plans-False-"
              "db/migration_040_defgeo_question_plans_2026_08_21.sql]"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[idx_mhz_media_name_trgm-mhz_media-False-"
              "scripts/migration_mhz_media_name_trgm_2026_07_28.sql]"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[uq_defgeo_attempt_single_inflight-defgeo_monitoring_attempts-True-"
              "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql]"),
        },
        "expect_note": "锚注释仍写 fund_recovery_orders,而 IF 分支绑到了 point_freezes ⇒ "
                       "逐字模板锁与逐字等价锁各报各的",
    },
    {
        "id": "AXISC-04", "file": FUND6,
        "desc": "用逗号列表把第二个索引名藏进守卫体内的真删语句(轴C 扫描器必须整串枚举)",
        "from": "        DROP INDEX IF EXISTS uniq_fund_recovery_open;",
        "to": "        DROP INDEX IF EXISTS uniq_fund_recovery_open, ux_recharge_orders_idem;",
        "expect": {
            q("test_axis_c_no_naked_drop_index_remains"),
            q("test_axis_c_every_drop_guard_has_all_three_legs"),
            q("test_every_drop_guard_is_byte_identical_to_rewriting_the_base_version"),
        },
        "expect_note": "第二个名字既不在守卫的绑定里、也不在审定清单里 ⇒ 它是一条**裸** DROP",
    },
    {
        "id": "AXISC-05", "file": GEOOBS,
        "desc": "把动态 DROP 的 indrelid 过滤删掉(候选集从此不再限死在目标表上)",
        "from": "         WHERE i.indrelid='public.geo_observation_insight_jobs'::regclass\n",
        "to": "         WHERE TRUE\n",
        "expect": {
            q("test_dynamic_drop_site_is_frozen_and_already_table_bound"),
            # 过滤一去掉,那个游标就会看到**别的表**上的 UNIQUE 索引,
            # 撞上它自己原有的 `RAISE '存在未知额外 UNIQUE 索引'` ⇒ 整份重放当场炸,
            # 于是所有"自己现跑 replay"的判据跟着红。
            q("test_every_declared_index_lands_on_its_target_table_after_cold_replay"),
            *_REPLAY_DEPENDENT,
            q("test_partial_unique_single_inflight_is_actually_enforced"),
            q("test_readiness_catches_index_definition_drift"),
            q("test_readiness_catches_missing_index_when_guard_is_neutered"),
            q("test_replay_twice_is_a_noop"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[idx_defgeo_qplan_brand_latest-defgeo_question_plans-False-"
              "db/migration_040_defgeo_question_plans_2026_08_21.sql]"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[idx_mhz_media_name_trgm-mhz_media-False-"
              "scripts/migration_mhz_media_name_trgm_2026_07_28.sql]"),
            q("test_without_decoy_replay_is_clean_and_index_lands_on_target"
              "[uq_defgeo_attempt_single_inflight-defgeo_monitoring_attempts-True-"
              "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql]"),
        },
        "expect_note": "这一处不改写,靠「它本来就绑表」立住;那句过滤没了,理由就不成立",
    },
    {
        "id": "AXISC-06", "file": INVENTORY,
        "desc": "把审定清单里某个 M 档的期望表改错(SQL 一个字不动,只改清单)",
        "from": '"index": "uq_diag_refund_extref", "table": "diagnosis_refund_records",',
        "to": '"index": "uq_diag_refund_extref", "table": "diagnosis_runs",',
        "expect": {
            q("test_drop_guard_expected_table_matches_its_provenance[15M:uq_diag_refund_extref]"),
            q("test_every_drop_guard_is_byte_identical_to_rewriting_the_base_version"),
        },
        "expect_note": "清单是 SSOT,守卫是产物 —— 清单改了而守卫没改,双向锁必须抓到",
    },
    {
        "id": "AXISC-07", "file": M034,
        "desc": "把 strict 形态的不存在分支改成幂等跳过(与老形态口径不符)",
        "from": ("""            RAISE EXCEPTION 'index "uq_mhz_item_live_revision_root" does not exist'\n"""
                 "                USING ERRCODE = 'undefined_object';"),
        "to": "            NULL;",
        "expect": {
            q("test_axis_c_every_drop_guard_has_all_three_legs"),
            q("test_every_drop_guard_is_byte_identical_to_rewriting_the_base_version"),
        },
        "expect_note": "PG16 实测:老形态不带 IF EXISTS 时索引不存在会 ERROR;"
                       "改成 NULL 就把一条响亮失败变成了静默通过",
    },
    # ══════════════════════════════════════════════════════════════════
    # R2 收口新增(**交付方自选** —— 这一半没有独立审计方参与,报数时单列)
    # 守的是收口那 11 条新判据:兜底 DSN(3)· 运行期裸 DROP 机制(4)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "R2-01", "file": SELFRUNNER,
        "desc": "把陈旧的可执行 DSN 塞回 runner 文档串(行为不变,只看锁认不认字面量)",
        # 锚里带**真换行**:这样它跟本 dict 自己的源码文本(那里的换行是转义符,
        # 不是真换行)不同形,否则 runner 会在自己身上命中两处、把这一发判成"考错题"。
        "from": (r"    TEST_DATABASE_URL=postgresql://geo_admin:<pw>@localhost:<port>/<db>_test \\"
                 "\n        python scripts/mutation_runner_defgeo_index_guard.py"),
        "to": (r"    TEST_DATABASE_URL=postgresql://geo_admin:testpw@localhost:55620/geo_defgeo_w3c_test \\"
               "\n        python scripts/mutation_runner_defgeo_index_guard.py"),
        "expect": {q("test_mutation_runner_has_no_hardcoded_fallback_dsn")},
        "expect_note": "陈旧默认就是这么回来的 —— 哪怕只写在文档串里,照着粘贴的人一样打错库",
    },
    {
        "id": "R2-02", "file": SELFRUNNER,
        "desc": "把「缺 TEST_DATABASE_URL 就响亮失败」那条守卫摘掉",
        "from": 'DB_URL = os.environ.get("TEST_DATABASE_URL", "")\nif not DB_URL:',
        "to": 'DB_URL = os.environ.get("TEST_DATABASE_URL", "")\nif False:',
        "expect": {q("test_mutation_runner_fails_loud_without_an_explicit_test_dsn")},
        "expect_note": "摘掉之后会落到安全栓那条腿 —— 一样非零退出、文案里一样有 "
                       "TEST_DATABASE_URL 这个词。判据要是只断言「出现这个词」就抓不到,"
                       "所以断言收紧到点名『未设置』",
    },
    {
        "id": "R2-03", "file": SELFRUNNER,
        "desc": "把非一次性库名的安全栓文案降级(拦是拦了,但不再是安全栓拦的)",
        # 同 R2-01:锚里带真换行,免得在本 dict 自己的源码上又命中一次
        "from": '.lower():\n    raise SystemExit(f"安全栓:',
        "to": '.lower():\n    raise SystemExit(f"提示:',
        "expect": {q("test_mutation_runner_refuses_a_non_test_database_name")},
        "expect_note": "反向对照那一条必须认得出「是谁拦的」,不然它和上面那条是同一条",
    },
    {
        "id": "R2-04", "file": MONDB,
        "desc": "运行期裸 DROP 改个索引名(冻结集合 + 与审定清单的交集都必须当场红)",
        "from": 'cursor.execute("DROP INDEX IF EXISTS uniq_kms_keyword_active")',
        "to": 'cursor.execute("DROP INDEX IF EXISTS uniq_kms_keyword_active_v2")',
        "expect": {
            q("test_runtime_python_naked_drop_census_is_frozen"),
            q("test_runtime_naked_drops_hit_exactly_the_names_axisc_just_bound"),
        },
        "expect_note": "集合锁(不是条数锁)才抓得到「改名换一处」;第二条同时失守,"
                       "因为新名字不在轴C 审定清单里",
    },
    {
        "id": "R2-05", "file": SELFHEAL,
        "desc": "把死代码 ensure_selfserve_tables 接上线(它就从死代码变成第 7 个活隐患)",
        "from": ("        from db.monitoring_db import init_monitoring_tables\n"
                 "        init_monitoring_tables()"),
        "to": ("        from db.monitoring_db import init_monitoring_tables\n"
               "        from db.research_selfserve_db import ensure_selfserve_tables\n"
               "        init_monitoring_tables()\n"
               "        ensure_selfserve_tables()"),
        "expect": {q("test_selfserve_ensure_tables_is_still_dead_code")},
        "expect_note": "交付单说它是死代码、后续工单单独标注 —— 这句话必须有人守着",
    },
    {
        "id": "R2-06", "file": DIAGDB,
        "desc": "给 db.diagnosis_db 加一条模块级 import,把带裸 DROP 的模块拉进 prestart 闭包",
        "from": "import json\nimport logging",
        "to": "import json\nimport logging\nimport db.fund_recovery_db  # mutation",
        "expect": {q("test_prestart_bootstrap_does_not_reach_the_naked_drop_modules")},
        "expect_note": "§9.3-(b) 机制陈述的承重前提 —— 这条前提翻了,结论方向整个反过来",
    },
    {
        "id": "R2-07", "file": SERVERPY,
        "desc": "把 server.py 模块级的 init_wallet_tables() 调用摘成一个引用(不再执行)",
        "from": ("    from db.wallet_db import init_wallet_tables, seed_feature_pricing\n"
                 "    init_wallet_tables()"),
        "to": ("    from db.wallet_db import init_wallet_tables, seed_feature_pricing\n"
               "    _deferred = init_wallet_tables"),
        "expect": {q("test_the_real_trigger_path_is_module_level_in_server_py")},
        "expect_note": "「每次容器起都跑」这句话没人守的话,下一张工单会照着过期的严重性排优先级",
    },
    {
        "id": "R2-08", "file": SELFRUNNER,
        "desc": "把兜底 DSN 塞回取值处(默认值不再是空串)",
        # 与 R2-02 同锚不同改法:那一发摘守卫,这一发**把兜底默认塞回去**。
        # 刻意选一个库名不含 test 的 DSN —— 否则安全栓放行,子进程会在判据里
        # 套娃跑起一整轮撕锁(嵌套 runner 会同时改同一批源文件,后果不可控)。
        "from": 'DB_URL = os.environ.get("TEST_DATABASE_URL", "")\nif not DB_URL:',
        "to": ('DB_URL = os.environ.get("TEST_DATABASE_URL", '
               '"postgresql://geo_admin:pw@localhost:55620/geo_agentscope")\nif not DB_URL:'),
        "expect": {
            q("test_mutation_runner_has_no_hardcoded_fallback_dsn"),
            q("test_mutation_runner_fails_loud_without_an_explicit_test_dsn"),
        },
        "expect_note": "兜底一回来,取值处的 AST 锁与「缺环境变量必须响亮失败」同时失守;"
                       "反向对照(非 test 库名)仍应保持绿 —— 它守的是另一条腿",
    },
    # ══════════════════════════════════════════════════════════════════
    # R3 收口新增(**交付方自选**,单列不并总数)
    # 守的是:普查作用域(2)· 报错路径自己能报错(1)· 名字解析两个边角(1)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "R3-01", "file": SCANRT,
        "desc": "把 docs/ 放回普查作用域(= R2 那次让最终树基线必红的原样)",
        "from": '_OUT_OF_SCOPE_PREFIXES = ("tests/", "docs/")',
        "to": '_OUT_OF_SCOPE_PREFIXES = ("tests/",)',
        "expect": {
            q("test_runtime_python_naked_drop_census_is_frozen"),
            q("test_naked_drop_census_scope_is_the_executable_surface"),
        },
        "expect_note": "取证脚本为了演示这个病自带真 DROP INDEX,放回分母就是 22/10 —— "
                       "冻结锁与作用域样本必须同时红",
    },
    {
        "id": "R3-02", "file": SCANRT,
        "desc": "把作用域收成「只看 db/ + scripts/」(今天数字不变,但 api/ tools/ 成盲区)",
        "from": ("    if any(rel.startswith(pfx) for pfx in _OUT_OF_SCOPE_PREFIXES):\n"
                 "        return False\n"
                 '    return "/tests/" not in rel'),
        "to": ('    if not rel.startswith(("db/", "scripts/")):\n'
               "        return False\n"
               '    return "/tests/" not in rel'),
        "expect": {q("test_naked_drop_census_scope_is_the_executable_surface")},
        "expect_note": "🔴 这一发专门守「别把分母钉死在今天命中的两层上」——"
                       "冻结集合一条不变、条数一条不差,只有作用域样本会红",
    },
    {
        "id": "R3-03", "file": SUITEFILE,
        "desc": "把普查差集文案的排序键去掉(sorted 遇 (str, None) 自爆 TypeError)",
        "from": ("    extra = sorted(got - frozen, key=key)\n"
                 "    missing = sorted(frozen - got, key=key)"),
        "to": ("    extra = sorted(got - frozen)\n"
               "    missing = sorted(frozen - got)"),
        "expect": {q("test_census_failure_message_survives_a_none_named_entry")},
        "expect_note": "报错路径必须自己能跑通 —— 否则判据该红的那一次不是摆出 diff,"
                       "而是自爆在文案里把真差别盖掉",
    },
    {
        "id": "R3-04", "file": SCANRT,
        "desc": "把 `IF EXISTS` 那个可选组改成原子组(名字解析的边角行为翻面)",
        "from": r'    r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?"',
        "to": r'    r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?+"',
        "expect": {q("test_drop_index_name_edge_cases")},
        "expect_note": "回溯与否决定 `DROP INDEX IF EXISTS {}` 解出 'IF' 还是 None —— "
                       "冻结集合里 '{}' / 'IF' 这类条目的口径就挂在这上面",
    },
]


def main() -> int:
    originals = {f: f.read_bytes() for f in TARGETS}
    print("被测文件:")
    for f, b in sorted(originals.items()):
        print(f"  {f.relative_to(ROOT).as_posix()} sha={sha(b)}")

    print("\n=== 基线(不变异)===")
    red, green, code = run_criteria()
    base_red, base_green = set(red), green
    print(f"  绿 {green} · 红 {len(red)} · exit={code}")
    if red or code != 0:
        print("🔴 基线就不是全绿,撕锁结果无意义。先修判据。")
        for r in sorted(red):
            print("   " + r)
        return 1

    bad = 0
    tally = {"SELF": [0, 0], "EXT": [0, 0], "AXISC": [0, 0],
         "R2": [0, 0], "R3": [0, 0]}  # [杀死, 总数]
    verdicts: list[dict] = []
    for m in MUTATIONS:
        half = m["id"].split("-")[0]
        tally[half][1] += 1
        print(f"\n=== {m['id']} · {m['desc']} ===")
        with io.open(m["file"], encoding="utf-8", newline="") as fh:
            src = fh.read()
        hits = src.count(m["from"])
        if hits != 1:
            print(f"  🔴 变异锚点命中 {hits} 处(必须恰好 1)—— 这一发作废,不当『杀不掉』")
            bad += 1
            continue
        with io.open(m["file"], "w", encoding="utf-8", newline="") as fh:
            fh.write(src.replace(m["from"], m["to"], 1))
        try:
            red, green, code = run_criteria()
        finally:
            m["file"].write_bytes(originals[m["file"]])
            restored = m["file"].read_bytes() == originals[m["file"]]
        if not restored:
            print("  🔴🔴 还原失败!工作树已被污染,立即停手。")
            return 2

        expected: set[str] = m["expect"]
        missing = sorted(expected - red)
        extra = sorted(red - expected)
        verdicts.append({
            "id": m["id"], "desc": m["desc"],
            "file": m["file"].relative_to(ROOT).as_posix(),
            "expected_red": sorted(expected), "actual_red": sorted(red),
            "missing": missing, "extra": extra, "green": green,
            "killed": not missing and not extra and bool(expected),
            "note": m.get("expect_note", ""),
        })
        print(f"  期望 {len(expected)} 条 · 实测 {len(red)} 条 · 绿 {green}")
        if m.get("expect_note"):
            print(f"  说明:{m['expect_note']}")
        for r in sorted(red):
            print("     " + r)
        if missing:
            print(f"  🔴 **该红没红**({len(missing)}):{missing}")
        if extra:
            print(f"  🔴 **溢出红**({len(extra)}):{extra} —— "
                  "这发变异打到了它不该打到的判据,或判据之间有耦合")
        if not expected and not red:
            print("  ⚠️ 期望为空且实测为空 —— 这一发**当前没有判据在守**,按说明归因")
        elif missing or extra:
            bad += 1
        else:
            print("  ✅ 杀死,且红集合与期望**逐项相等**")
            tally[half][0] += 1

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "runner": "scripts/mutation_runner_defgeo_index_guard.py",
        "db_url_db": DB_URL.rsplit("/", 1)[-1],
        "suite": SUITE,
        "baseline": {"green": base_green, "red": sorted(base_red)},
        "totals": {k: {"killed": v[0], "total": v[1]} for k, v in tally.items()},
        "mutations": verdicts,
    }
    with io.open(ARTIFACT_JSON, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    with io.open(ARTIFACT_LOG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"基线:绿 {base_green} · 红 {len(base_red)}\n")
        for v in verdicts:
            fh.write(f"\n=== {v['id']} · {v['desc']} ===\n")
            fh.write(f"  文件 {v['file']}\n")
            fh.write(f"  期望红 {len(v['expected_red'])} · 实测红 {len(v['actual_red'])} "
                     f"· 绿 {v['green']} · {'KILLED' if v['killed'] else 'MISMATCH'}\n")
            for r in v["expected_red"]:
                fh.write(f"    期望 {r}\n")
            for r in v["actual_red"]:
                fh.write(f"    实测 {r}\n")
            if v["missing"]:
                fh.write(f"    🔴 该红没红 {v['missing']}\n")
            if v["extra"]:
                fh.write(f"    🔴 溢出红 {v['extra']}\n")
            if v["note"]:
                fh.write(f"    说明 {v['note']}\n")
        # 🔴 分半计数**遍历 tally**,不写死三半:R2 收口加了第四半 R2-*,
        #    第一版这里漏了它 ⇒ 落盘产物页脚只报三半,与正文 40 条自相矛盾。
        fh.write("\n" + " · ".join(f"{k} {v[0]}/{v[1]}" for k, v in tally.items()) + "\n")
    print(f"\n落盘产物:{ARTIFACT_JSON.relative_to(ROOT).as_posix()}")
    print(f"          {ARTIFACT_LOG.relative_to(ROOT).as_posix()}")

    print("\n还原核对:")
    dirty = False
    for f, b in sorted(originals.items()):
        now = f.read_bytes()
        same = now == b
        dirty = dirty or not same
        print(f"  {f.relative_to(ROOT).as_posix()} sha={sha(now)} "
              f"{'(逐字节一致)' if same else '(🔴 不一致)'}")
    if dirty:
        return 2

    print("=" * 64)
    print(" · ".join(f"{k} {v[0]}/{v[1]}" for k, v in tally.items()))
    print(f"✅ {len(MUTATIONS)} 发全部精确匹配" if bad == 0
          else f"🔴 {bad}/{len(MUTATIONS)} 发未达预期")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
