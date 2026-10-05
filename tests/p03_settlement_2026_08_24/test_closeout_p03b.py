"""【P0-3b】结算收尾:冻结拆分快照(A)· 识别失败守卫接线(B)· 两条竞态残余(C)。

判据仍是真 PG16 + 真 `middleware.billing` 原语,零 mock —— 唯一被替身的是
**协作方**(`_run_diagnosis_impl` / `settle_charge`),被测的那几行(结算判定、except 路由、
快照覆盖)全程跑真身。替掉被测代码本身的那种夹具在本仓叫「夹具替被测代码干活」,恒绿,不算数。

夹具库在**生产 schema 之上叠加迁移 048** —— 顺带证明这条迁移能装进真实形状的库。
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import pathlib
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from tests.p03_settlement_2026_08_24.test_settlement_real_pg import (  # noqa: E402
    ADMIN_DSN, ALL_LEVEL_WORDS, PROD_SCHEMA, _admin_dsn, _conn, _verdict, _world,
)

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 本包自己那条迁移的文件名 —— 下面 ``test_this_package_migration_is_in_the_manifest``
#: 拿它当"必须在清单里"的锚。它是**本包的身份**,与上面那个机械枚举的建库清单
#: 是两件事:枚举决定"建库要装哪些",这个常量决定"本包自己那条有没有被登记"。
MIGRATION_048_NAME = "migration_048_diagnosis_reserved_split_2026_08_24.sql"


def _diagnosis_runs_migrations():
    """从 **manifest 现扫** 所有 ALTER diagnosis_runs 的 ``db/migration_0*`` 文件,保持清单顺序。

    🔴 [A-1 · 2026-08-25] 原来这里是一个手抄的常量 ``MIGRATION_048``。
       迁移 050 给 diagnosis_runs 加了 ``payer_user_id``,而 ``_persist_freeze_handle``
       同一条 UPDATE 会写它 —— 手抄清单不会跟着变,于是这个夹具建出来的库缺列,
       本文件当场 UndefinedColumn 红,红因与被测行为无关。
       手抄的分母漏掉的那一项不会让任何判据变红(本仓 2026-08-19 实证),
       所以改成从 manifest 机械枚举:以后谁再给这张表加列,这里自动跟上。
    """
    from db.migration_manifest import MIGRATIONS

    out = []
    for rel in MIGRATIONS:
        if not rel.startswith("db/migration_0"):
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "ALTER TABLE diagnosis_runs" in text:
            out.append(path)
    if not out:
        raise RuntimeError(
            "manifest 里一条 ALTER diagnosis_runs 的迁移都扫不到 —— 扫描面坏了。"
            "宁可当场炸,也不要给判据一个缺列的库(那会红得莫名其妙)。")
    return out


@pytest.fixture(scope="module")
def live_db_048():
    """生产 schema + **全部** ALTER diagnosis_runs 的迁移。库名含 test(安全栓),用完 DROP。"""
    if not PROD_SCHEMA.is_file():
        pytest.skip("需要生产 schema 夹具")
    name = "p03b_" + uuid.uuid4().hex[:8] + "_test"
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "' + name + '"')
    admin.close()
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name

    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    cur.execute("SET search_path = public")
    for _migration in _diagnosis_runs_migrations():            # 迁移装进真实形状的库
        cur.execute(_migration.read_text(encoding="utf-8"))
    conn.close()

    import db.connection as dbconn
    old_url, old_pool = dbconn.DATABASE_URL, dbconn._pool
    dbconn.DATABASE_URL = dsn
    dbconn._pool = None
    try:
        yield dsn
    finally:
        try:
            dbconn.close_pool()
        except Exception:  # noqa: BLE001
            pass
        dbconn.DATABASE_URL, dbconn._pool = old_url, old_pool
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
        admin.close()


def _set_split_snapshot(dsn, run_token, snapshot):
    c = _conn(dsn)
    c.cursor().execute(
        "UPDATE diagnosis_runs SET reserved_split_snapshot_jsonb=%s WHERE run_token=%s",
        (json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None, run_token))
    c.close()


def _settle(world, snapshot=None):
    from services.diagnosis_runs import commit_run
    return asyncio.run(commit_run(world["run_token"], snapshot or world["snapshot"]))


def _after(dsn, world):
    c = _conn(dsn)
    cur = c.cursor()
    cur.execute("SELECT status FROM point_freezes WHERE id=%s", (world["freeze_id"],))
    freeze = cur.fetchone()
    cur.execute("SELECT run_status, last_settlement_error, final_snapshot_jsonb "
                "FROM diagnosis_runs WHERE run_token=%s", (world["run_token"],))
    run = cur.fetchone()
    cur.execute("SELECT paid_points, bonus_points, commission_points, frozen_points "
                "FROM user_wallets WHERE user_id=%s", (world["uid"],))
    wallet = cur.fetchone()
    c.close()
    return {"freeze": freeze, "run": run, "wallet": wallet}


# ===========================================================================
# A · 冻结拆分快照:多池也能自动按比例,且**逐池金额 + order 都对**
# ===========================================================================

def test_multi_pool_with_snapshot_settles_per_pool_in_the_frozen_order(live_db_048):
    """650 = bonus 300 + paid 350,order=[bonus,commission,paid],ratio 0.25 → 应扣 162。

    按 order 走:bonus 先出 min(300,162)=162 → 退回 bonus 138 / commission 0 / paid 350。
    **逐池断言**才有区分力:如果 order 被反过来(paid 先),数字会是 paid 退 188 / bonus 退 300 ——
    只断言"总额对"是分不出这两种的,而它们对客户是两回事(赠送池和现金池不等价)。
    """
    w = _world(live_db_048, engines=["qwen"], successful_tests=8, frozen=650,
               split={"bonus": 300, "commission": 0, "paid": 350})
    _set_split_snapshot(live_db_048, w["run_token"],
                        {"bonus": 300, "commission": 0, "paid": 350,
                         "order": ["bonus", "commission", "paid"],
                         "requires_paid_points": False, "deduction_preference": "default"})
    out = _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    a = _after(live_db_048, w)

    assert a["freeze"]["status"] == "committed", a["run"]["last_settlement_error"]
    assert out.get("ok") is True
    assert a["wallet"]["frozen_points"] == 0
    assert a["wallet"]["bonus_points"] == w["paid"] + 138, "bonus 池未按 order 首位承担实扣"
    assert a["wallet"]["commission_points"] == w["paid"]
    assert a["wallet"]["paid_points"] == w["paid"] + 350, "paid 池应整池退回(order 里排在 bonus 之后)"


def test_reversed_order_in_snapshot_moves_the_money_differently(live_db_048):
    """区分力自证:同一笔冻结,只把快照 order 换成 paid 优先 → 逐池数字必须**不同**。
    (两种排法结果一样的话,上一条"按 order 结算"就没在验任何东西。)
    """
    w = _world(live_db_048, engines=["qwen"], successful_tests=8, frozen=650,
               split={"bonus": 300, "commission": 0, "paid": 350})
    _set_split_snapshot(live_db_048, w["run_token"],
                        {"bonus": 300, "commission": 0, "paid": 350,
                         "order": ["paid", "commission", "bonus"],
                         "requires_paid_points": False, "deduction_preference": "agent_friendly"})
    _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    a = _after(live_db_048, w)
    assert a["freeze"]["status"] == "committed", a["run"]["last_settlement_error"]
    assert a["wallet"]["paid_points"] == w["paid"] + (350 - 162), "paid 池应作为 order 首位承担实扣"
    assert a["wallet"]["bonus_points"] == w["paid"] + 300, "bonus 池应整池退回"


def test_legacy_run_without_snapshot_multi_pool_still_goes_manual(live_db_048):
    """存量锁:冻结时还没这一列(快照 NULL)+ 多池 → 维持 P0-3 行为,转人工,不猜。"""
    w = _world(live_db_048, engines=["qwen"], successful_tests=8, frozen=650,
               split={"bonus": 300, "commission": 0, "paid": 350})
    _set_split_snapshot(live_db_048, w["run_token"], None)
    _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    a = _after(live_db_048, w)
    assert a["run"]["run_status"] == "settlement_manual"
    assert a["freeze"]["status"] == "frozen", "转人工时绝不能已经动过钱"
    assert "reserved_split_order_unknown" in (a["run"]["last_settlement_error"] or "")


def test_snapshot_disagreeing_with_the_freeze_row_goes_manual(live_db_048):
    """快照三池与冻结行对不上(列被改过/冻结行被动过)→ 不可信 → 转人工,不猜。"""
    w = _world(live_db_048, engines=["qwen"], successful_tests=8, frozen=650,
               split={"bonus": 300, "commission": 0, "paid": 350})
    _set_split_snapshot(live_db_048, w["run_token"],
                        {"bonus": 400, "commission": 0, "paid": 250,      # ← 和冻结行不符
                         "order": ["bonus", "commission", "paid"]})
    _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    a = _after(live_db_048, w)
    assert a["run"]["run_status"] == "settlement_manual"
    assert a["freeze"]["status"] == "frozen"
    assert "reserved_split_sum_mismatch" in (a["run"]["last_settlement_error"] or "")


def test_snapshot_with_order_billing_would_reject_goes_manual(live_db_048):
    """order 不是 billing 认的两种形态 → 自己先判死,别把必 raise 的 split 送进资金原语。"""
    w = _world(live_db_048, engines=["qwen"], successful_tests=8, frozen=650,
               split={"bonus": 0, "commission": 0, "paid": 650})
    _set_split_snapshot(live_db_048, w["run_token"],
                        {"bonus": 0, "commission": 0, "paid": 650, "order": ["paid"]})
    _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    a = _after(live_db_048, w)
    assert a["run"]["run_status"] == "settlement_manual"
    assert a["freeze"]["status"] == "frozen"
    assert "reserved_split_order_invalid" in (a["run"]["last_settlement_error"] or "")


def test_single_pool_without_snapshot_still_auto_prorates(live_db_048):
    """配对的必须不命中:单池 + 无快照 → P0-3 的自动按比例照旧(不因新列而回退)。"""
    w = _world(live_db_048, engines=["qwen"], successful_tests=8, frozen=650)
    _set_split_snapshot(live_db_048, w["run_token"], None)
    _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    a = _after(live_db_048, w)
    assert a["freeze"]["status"] == "committed", a["run"]["last_settlement_error"]
    assert a["wallet"]["paid_points"] == w["paid"] + (650 - 162)


# --- A 的接线:光有列没用,冻结当时必须真的写进去 ---------------------------

def test_classify_freeze_result_carries_the_billing_split_snapshot():
    """`freeze_points` 返回的 physical_split_snapshot 必须被分类器原样带出来。"""
    from services.diagnosis_runs import classify_freeze_result
    snap = {"bonus": 1, "commission": 0, "paid": 2, "order": ["bonus", "commission", "paid"]}
    cls = classify_freeze_result(
        {"freeze_id": 7, "freeze_table": "legacy", "physical_split_snapshot": snap}, None)
    assert cls.branch == "paid" and cls.split_snapshot == snap


def test_persist_freeze_handle_actually_writes_the_column(live_db_048):
    """接线锁:分类器带出来了,还得真落到列上 —— 中间断一环,多池照样转人工。"""
    from services.diagnosis_runs import _persist_freeze_handle
    w = _world(live_db_048, engines=["qwen"], successful_tests=8)
    _set_split_snapshot(live_db_048, w["run_token"], None)
    snap = {"bonus": 0, "commission": 0, "paid": 650, "order": ["bonus", "commission", "paid"]}
    _persist_freeze_handle(w["run_token"], w["freeze_id"], "legacy", snap)
    c = _conn(live_db_048)
    cur = c.cursor()
    cur.execute("SELECT reserved_split_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s",
                (w["run_token"],))
    stored = cur.fetchone()["reserved_split_snapshot_jsonb"]
    c.close()
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored == snap


def test_exempt_four_arg_construction_still_works():
    """向后兼容锁:server.py 的 exempt 分支用四参构造 FreezeClassification,不能被新字段打断。"""
    from services.diagnosis_runs import FreezeClassification
    cls = FreezeClassification("exempt", None, None, "exempt")
    assert cls.split_snapshot is None


def test_activate_after_freeze_passes_the_snapshot_through(live_db_048):
    """端到端接线:分类器 → activate_after_freeze → 列。断哪一环这条都红。"""
    from services.diagnosis_runs import (
        FreezeClassification, activate_after_freeze, get_run,
    )
    w = _world(live_db_048, engines=["qwen"], successful_tests=8)
    c = _conn(live_db_048)
    c.cursor().execute("UPDATE diagnosis_runs SET run_status='pending_freeze', "
                       "reserved_split_snapshot_jsonb=NULL WHERE run_token=%s", (w["run_token"],))
    c.close()
    snap = {"bonus": 0, "commission": 650, "paid": 0, "order": ["commission", "paid"]}
    activate_after_freeze(w["run_token"], w["uid"],
                          FreezeClassification("paid", w["freeze_id"], "legacy", "paid", snap))
    stored = get_run(w["run_token"])["reserved_split_snapshot_jsonb"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored == snap


# ===========================================================================
# B · 识别失败守卫接线
# ===========================================================================

SUSPECT = {"suspected": True, "verdict": "suspected_identity_failure",
           "signals": ["brand_name_malformed"]}


def test_suspected_identity_goes_manual_and_never_touches_the_money(live_db_048):
    """疑似识别失败 → 转人工;冻结**原样挂着**(不自动扣也不自动退,由人裁)。"""
    w = _world(live_db_048, engines=["qwen", "kimi"], successful_tests=8, total_score=0)
    out = _settle(w, dict(w["snapshot"], identity_suspicion=SUSPECT))
    a = _after(live_db_048, w)
    assert a["run"]["run_status"] == "settlement_manual"
    assert a["freeze"]["status"] == "frozen", "人裁之前绝不能动钱"
    assert a["wallet"]["frozen_points"] == w["frozen"], "冻结额必须原样挂着"
    from services.diagnosis_runs import IDENTITY_REVIEW_REASON
    assert out.get("reason") == IDENTITY_REVIEW_REASON


def test_not_suspected_is_byte_for_byte_unchanged(live_db_048):
    """配对的必须不命中:suspected=False → 行为与没这个字段时一致(正常成交)。"""
    w = _world(live_db_048, engines=["qwen"], successful_tests=32, total_score=72, level="健康级")
    _settle(w, dict(w["snapshot"], identity_suspicion={"suspected": False, "verdict": "ok"},
                    delivery_verdict=_verdict("sufficient", 1.0)))
    a = _after(live_db_048, w)
    assert a["freeze"]["status"] == "committed"
    assert a["wallet"]["paid_points"] == w["paid"]


def test_identity_review_message_carries_no_level_word_and_no_refund_claim():
    """文案锁:这一档钱还没退,不许说"已退回";也不许出等级词。"""
    from services.diagnosis_runs import IDENTITY_REVIEW_MESSAGE
    hits = [x for x in ALL_LEVEL_WORDS if x in IDENTITY_REVIEW_MESSAGE]
    assert not hits, hits
    assert "已退回" not in IDENTITY_REVIEW_MESSAGE, "钱还挂着,不能说已退回"
    assert "人工" in IDENTITY_REVIEW_MESSAGE


def test_completion_payload_splats_the_settlement_signal_assembler():
    """接线锁(R2-② 升级版):完成 payload 必须 `**settlement_signals_from_result(result)`。

    **这条锁上一版是错的**,Review 亲手注毒抓到:它只断言键名 `identity_suspicion`
    出现在 payload 的 dict 字面量里 —— 留着键、把值钉成 None,52 条判据照样全绿,
    而两道守卫其实已经失效。结构对了 ≠ 值对了,是两道缝。

    现在这条只负责**结构那道缝**(有没有走那个组装函数);
    **值那道缝**交给下面 test_settlement_signals_* 两条直接驱动函数的判据。

    ⚠️⚠️ **这条仍然是 `ast.dump()` 的子串匹配 —— 别只看名字以为调用点由它在守。**
    它只答得了"那个函数名有没有出现在某个 `**` 展开的 dump 里",于是这两种写法它**恒绿**:

        **settlement_signals_from_result({})                       # 入参被换掉
        **{k: None for k in settlement_signals_from_result(result)}  # 值被推平

    两种都让 delivery_verdict / identity_suspicion 在**每一单**里恒 None
    (= 降级交付全额扣 + 疑似识别失败永不转人工),而这条判据一声不吭。

    R3 实测(`scratchpad/p03b_attribution.py`,四发变异取完整 FAILED 全集):
      · 调用处**整体换成手写 None dict** → 本条 + contract 那条 + 节点级锁,3 条齐红;
      · 调用处**入参换成 `{}`**           → **只有节点级锁红,本条全绿**。

    真正把调用点锁到节点的是:
      `test_r3_gap_closure.py::test_payload_splat_is_the_assembler_called_with_the_real_result`
    (逐节点判 `ast.Call` + func 解析 + 实参恰好一个 `Name('result')` + 无关键字参数)。
    本条保留的意义只是"连 `**` 展开都没了"这种更粗的破法能早一步报出来;
    **要改调用点的行为,去看那条节点级锁,不是这条。**
    """
    tree = ast.parse(io.open(REPO / "server.py", encoding="utf-8").read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if not {"diagnosis_id", "share_token", "terminal"} <= keys:
            continue
        # dict 字面量里的 `**x` 在 AST 里是 key=None、value=x
        splats = [ast.dump(v) for k, v in zip(node.keys, node.values) if k is None]
        found.append((node.lineno, any("settlement_signals_from_result" in d for d in splats)))
    assert found, "没定位到诊断完成 payload,分母取错了"
    bad = [ln for ln, ok in found if not ok]
    assert not bad, (
        "server.py:%s 的完成 payload 没有 **settlement_signals_from_result(result) —— "
        "结算侧读不到判定,两道守卫都开不了火" % bad)


def test_settlement_signals_carry_the_real_values_not_just_the_keys():
    """值级判据 ①:workflow 结果里的**值**必须原样穿透。

    把任一路取值钉成 None(键还在)这条就红 —— 那正是 Review 注的那发毒。
    """
    from services.diagnosis_runs import SETTLEMENT_SIGNAL_KEYS, settlement_signals_from_result

    verdict = {"version": "v-test", "outcome": "degraded", "billable_ratio": 0.5}
    suspicion = {"suspected": True, "verdict": "suspected_identity_failure"}
    out = settlement_signals_from_result(
        {"data": {"delivery_verdict": verdict,
                  "ai_visibility": {"identity_suspicion": suspicion, "total_tests": 8}}})

    assert set(SETTLEMENT_SIGNAL_KEYS) == set(out), out
    assert out["delivery_verdict"] == verdict, "delivery_verdict 的值没穿透"
    assert out["identity_suspicion"] == suspicion, "identity_suspicion 的值没穿透"


@pytest.mark.parametrize("result", [
    {},                                              # 什么都没有
    {"data": {}},                                    # 有 data 没信号
    {"data": {"ai_visibility": "not-a-dict"}},       # 形状不对
    None, "not-a-dict",                              # 压根不是 dict
])
def test_settlement_signals_are_none_when_absent_but_keys_always_present(result):
    """值级判据 ②(配对的必须不命中):取不到就给 None,但**键始终在**。

    键始终在,是为了让下游分得清"没算"和"算了是空";
    给 None 而不是省略,与两个消费方"拿不到就按没有处理"的既有语义一致。
    """
    from services.diagnosis_runs import SETTLEMENT_SIGNAL_KEYS, settlement_signals_from_result

    out = settlement_signals_from_result(result)
    assert set(SETTLEMENT_SIGNAL_KEYS) == set(out)
    assert all(out[k] is None for k in SETTLEMENT_SIGNAL_KEYS), out


def test_signal_values_actually_drive_the_identity_guard_end_to_end(live_db_048):
    """值级判据 ③(最硬的一条):走**真组装函数**产出的 payload,守卫必须真的开火。

    这条不看结构、不看键名 —— 它把 workflow 形状的 result 交给组装函数,
    拿产出物当结算快照跑真链。值被钉成 None 时守卫不会开火,这条立刻红。
    """
    from services.diagnosis_runs import IDENTITY_REVIEW_REASON, settlement_signals_from_result

    w = _world(live_db_048, engines=["qwen", "kimi"], successful_tests=8, total_score=0)
    workflow_result = {
        "data": {"ai_visibility": {"identity_suspicion": SUSPECT, "total_tests": 8}},
        "scores": {"total_score": 0, "level": "隐形级"},
    }
    snap = dict(w["snapshot"], **settlement_signals_from_result(workflow_result))
    out = _settle(w, snap)
    a = _after(live_db_048, w)

    assert out.get("reason") == IDENTITY_REVIEW_REASON, out
    assert a["run"]["run_status"] == "settlement_manual"
    assert a["freeze"]["status"] == "frozen", "人裁之前绝不能动钱"


def test_signal_values_actually_drive_the_partial_charge_end_to_end(live_db_048):
    """值级判据 ④:delivery_verdict 同款 —— 走真组装函数,按比例扣必须真的发生。"""
    from services.diagnosis_runs import settlement_signals_from_result

    w = _world(live_db_048, engines=["qwen"], successful_tests=8, frozen=650)
    workflow_result = {"data": {"delivery_verdict": _verdict("degraded", 0.25),
                                "ai_visibility": {"total_tests": 8}}}
    snap = dict(w["snapshot"], **settlement_signals_from_result(workflow_result))
    _settle(w, snap)
    a = _after(live_db_048, w)

    assert a["freeze"]["status"] == "committed", a["run"]["last_settlement_error"]
    assert a["wallet"]["paid_points"] == w["paid"] + (650 - 162), (
        "按比例扣没发生 —— delivery_verdict 的值没穿透到结算")


def test_identity_predicate_reads_the_shape_the_producer_actually_sends(live_db_048):
    """形状锁:守卫必须认**顶层** identity_suspicion(server.py 就是这么发的)。

    夹具刻意**不用** `data.ai_visibility` 那种嵌套写法 —— 发送方从不发那个形状,
    拿它当夹具正是本病(读的键生产从来不会发)。
    """
    from services.diagnosis_runs import _identity_suspected
    assert _identity_suspected({"identity_suspicion": SUSPECT}) is True
    assert _identity_suspected({"identity_suspicion": {"suspected": False}}) is False
    assert _identity_suspected({}) is False


# ===========================================================================
# C · 两条竞态残余
# ===========================================================================

def test_rb_other_invalid_product_refund_also_covers_the_four_keys(live_db_048):
    """R-b:非"没测成"的产物证明失败(这里用报告未 ready)也是退款 →
    终态快照同样不许漏 result/level/score/share_token。
    """
    w = _world(live_db_048, engines=["qwen"], successful_tests=8, total_score=30, level="危急级")
    c = _conn(live_db_048)
    # 让 is_client_report_ready 判假 → 走 commit_product_invalid 那条**兄弟**分支
    c.cursor().execute("UPDATE diagnosis_records SET report_v2_modules_jsonb=%s WHERE id=%s",
                       (json.dumps({"client": {"modules": {}}}), w["diagnosis_id"]))
    c.close()
    assert w["snapshot"]["result"]["level"] == "危急级"      # 正样本自证:毒下进去了
    _settle(w)
    a = _after(live_db_048, w)
    assert a["freeze"]["status"] == "released"
    snap = a["run"]["final_snapshot_jsonb"]
    if isinstance(snap, str):
        snap = json.loads(snap)
    blob = json.dumps(snap, ensure_ascii=False)
    hits = [x for x in ALL_LEVEL_WORDS if x in blob]
    assert not hits, "commit_product_invalid 退款快照漏了等级词: %s · %s" % (hits, blob)
    assert snap.get("share_token") is None
    # 与 not_measured 区分:这一档不该打 not_measured 标(原因不同,运营要分得开)
    assert snap.get("not_measured") is not True


def test_rb_both_refund_branches_share_one_key_covering_helper():
    """单点锁:两个退款分支必须共用同一个覆盖清单,别各写一遍(R-b 就是各写一遍漏的)。"""
    from services.diagnosis_runs import (
        _SUCCESS_LEAK_KEYS, _not_measured_snapshot, _refunded_snapshot,
    )
    assert set(_SUCCESS_LEAK_KEYS) == {"result", "share_token", "score", "level"}
    for snap in (_refunded_snapshot("x"), _not_measured_snapshot()):
        for key in _SUCCESS_LEAK_KEYS:
            assert key in snap and snap[key] is None, key


def test_ra_inner_commit_exception_releases_the_org_charge():
    """R-a:内层 `except Exception as cex` 吞掉异常后,org 的 charge link 必须被释放。

    构造中间态而不是真赛跑:让 `settle_charge` 直接抛(竞态窗里它确实可能抛),
    然后看被测的那段 except 有没有把 release 路由起来。被替身的是**协作方**,
    被测的 except 路由跑的是真身。
    """
    tree = ast.parse(io.open(REPO / "server.py", encoding="utf-8").read())
    handlers = [n for n in ast.walk(tree)
                if isinstance(n, ast.ExceptHandler) and n.name == "cex"]
    assert handlers, "没定位到内层 commit except,分母取错了"
    dumped = "\n".join(ast.dump(h) for h in handlers)
    assert "release_charge" in dumped, (
        "内层 commit except 没有 org release 路由 —— settle_charge 中途抛时 "
        "charge link 会一直冻着,外层 except 够不到它")
    assert "organization_charge_id" in dumped, "org release 必须只对 org 计费的 run 触发"


def test_ra_release_is_guarded_so_legacy_runs_are_not_double_released():
    """配对的必须不命中:非 org 的 legacy 冻结**不能**在这里被顺手 release
    (它有 sweeper 按 run_status 兜底重试;这里再退一次就是双重处置)。
    """
    tree = ast.parse(io.open(REPO / "server.py", encoding="utf-8").read())
    checked = 0                      # ← 分母:没数到任何一个调用点 = 这条判据什么都没验
    for h in [n for n in ast.walk(tree)
              if isinstance(n, ast.ExceptHandler) and n.name == "cex"]:
        for node in ast.walk(h):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "_release_org_cex":
                checked += 1
                enclosing = [ast.dump(p.test) for p in ast.walk(h)
                             if isinstance(p, ast.If) and node in list(ast.walk(p))]
                assert any("organization_charge_id" in t for t in enclosing), (
                    "org release 没有被 organization_charge_id 守住")
    # 零分母自证:上面那两层 for 一旦匹配不到(改了调用写法/换了名字),循环体不执行,
    # 这条判据会**空过**而不是判红 —— 空过和"验过了"在报告里长得一模一样。
    assert checked == 1, "预期正好 1 处 org release 调用,实际数到 %d 处" % checked


def test_ra_release_call_kwargs_actually_bind_to_release_charge():
    """R-a 的 AST 锁只能证明"那儿写了 release_charge",证明不了**参数对不对**。

    写错一个关键字 → 运行时 TypeError → 被我自己那层 `except Exception as _rex2` 接住
    → 只留一条问题单,charge link 照样冻着,而所有结构锁**照样绿**。
    所以这里把 server.py 里那次调用的关键字集合抽出来,拿真签名 `bind` 一次。
    """
    import inspect

    from services.organization_billing import release_charge

    tree = ast.parse(io.open(REPO / "server.py", encoding="utf-8").read())
    calls = []
    for handler in [n for n in ast.walk(tree)
                    if isinstance(n, ast.ExceptHandler) and n.name == "cex"]:
        for node in ast.walk(handler):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "_release_org_cex":
                calls.append({kw.arg for kw in node.keywords if kw.arg})
    assert calls, "没定位到内层 except 里的 org release 调用,分母取错了"

    sig = inspect.signature(release_charge)
    for kwargs in calls:
        # 用真签名绑定:少必填 / 多不存在的键 都会在这里炸,而不是等生产 TypeError。
        sig.bind(**{name: None for name in kwargs})


# ===========================================================================
# 迁移接线 —— 光写 .sql 文件没用,prestart 只跑 manifest 里列出来的
# ===========================================================================
#
# 变异 N12(把 048 从 manifest 里删掉)第一轮**存活**:文件在、判据全绿,
# 而生产根本不会执行它 → 列不存在 → 多池永远转人工,新功能静默不生效。
# 本仓这类「接线没接」已经复发过好几次,所以这里同 commit 补上接线锁。

#: 017 之前的历史迁移已被 scripts/ 下的整合脚本与 init_db 接管,**不在** manifest 里。
#: 冻结成显式集合(今天机械枚举得到),作用是让**新**迁移无法悄悄混进这个豁免区 ——
#: 下面那条断言钉死它的大小和上界,想往里加一个新的就会判红。
LEGACY_UNLISTED_MIGRATIONS = frozenset({
    "db/migration_001_teams.sql",
    "db/migration_002_corpus.sql",
    "db/migration_003_inspirations.sql",
    "db/migration_004_intelligence.sql",
    "db/migration_004_material_cards.sql",
    "db/migration_005_activity.sql",
    "db/migration_005_compliance.sql",
    "db/migration_006_effective_rate.sql",
    "db/migration_007_identity_v3_3_1.sql",
    "db/migration_011_social_agent_plans.sql",
    "db/migration_012_social_agent_tool_audit.sql",
    "db/migration_013_social_agent_billing.sql",
    "db/migration_014_social_agent_metrics.sql",
    "db/migration_016_l0_quote_markup_self_edit_2026_06_07.sql",
})


def test_every_modern_migration_is_wired_into_the_manifest():
    """磁盘上每一个编号迁移,要么在 manifest 里,要么在冻结的历史豁免集里。"""
    import re as _re

    from db.migration_manifest import MIGRATIONS

    listed = set(MIGRATIONS)
    on_disk = sorted(p.as_posix() for p in (REPO / "db").glob("migration_[0-9][0-9][0-9]_*.sql"))
    on_disk = [p[p.index("db/"):] if "db/" in p else p for p in on_disk]
    assert len(on_disk) >= 30, "分母取错了:只数到 %d 个编号迁移" % len(on_disk)

    orphans = [p for p in on_disk
               if p not in listed and p not in LEGACY_UNLISTED_MIGRATIONS]
    assert not orphans, (
        "这些迁移文件没进 db/migration_manifest.MIGRATIONS,prestart 不会执行它们:%s" % orphans)


def test_the_legacy_exemption_set_cannot_absorb_new_migrations():
    """豁免集必须保持冻结 —— 它是历史事实,不是给新迁移开的后门。"""
    import re as _re

    nums = [int(_re.match(r"db/migration_(\d{3})_", p).group(1))
            for p in LEGACY_UNLISTED_MIGRATIONS]
    assert len(LEGACY_UNLISTED_MIGRATIONS) == 14, "豁免集大小变了 —— 有新迁移混进来了"
    assert max(nums) <= 16, "豁免集里出现了 017 及以后的迁移:%s" % sorted(nums)


def test_this_package_migration_is_in_the_manifest():
    """直给锁:本包这条 048 必须被 prestart 重放,否则列建不出来。"""
    from db.migration_manifest import MIGRATIONS

    assert MIGRATION_048_NAME in " ".join(MIGRATIONS), MIGRATION_048_NAME


def test_no_two_modern_migrations_claim_the_same_number():
    """号段撞车锁 —— 把 R2-① 那两轮返修变成一条会自己报错的判据。

    本包最初取 040,而 040-047 已被冻结的防御 GEO 班列认领(其中 047 是**同一天**
    被包E 拿走的),两轮裁定才收敛到 048。号段登记制是流程侧的解法;
    这条是工程侧的兜底:**同一棵树里两个现代迁移撞号就判红**。

    什么时候会救命:防御线合进来时两边的文件会同时落在这棵树上 ——
    撞号在那一刻变成一条红判据,而不是 prestart 按字典序跑出个谁也没想过的顺序。

    ≤016 的历史重号(004/005 各两份)是既成事实,走已冻结的 LEGACY_UNLISTED_MIGRATIONS 豁免;
    豁免集的大小与上界另有判据钉死,新号混不进来。
    """
    import collections
    import re as _re

    by_num = collections.defaultdict(list)
    for path in sorted((REPO / "db").glob("migration_[0-9][0-9][0-9]_*.sql")):
        num = _re.match(r"migration_(\d{3})_", path.name).group(1)
        by_num[num].append("db/" + path.name)
    assert len(by_num) >= 30, "分母取错了:只数到 %d 个编号" % len(by_num)

    collisions = {
        num: files for num, files in by_num.items()
        if len(files) > 1 and any(f not in LEGACY_UNLISTED_MIGRATIONS for f in files)
    }
    assert not collisions, (
        "迁移号撞车(prestart 会按字典序跑出不确定的顺序):%s" % collisions)
