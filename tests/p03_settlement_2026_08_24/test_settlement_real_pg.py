"""【P0-3】诊断结算判定 —— 真 PG16 + 真 billing 原语,零 mock 的端到端资金判据。

## 为什么必须真库 + 真 billing

被测缺陷的全部危险性在于「每一层看上去都通过了」:

* `require_complete_product` 只验 `total_score IS NOT NULL` —— 四引擎全挂时评分链
  fallback 出 0 分,这一条**恒真**;
* `is_client_report_ready` 只验模块 1 有非空 insight —— 而 `services/report_writer_v2.py:348`
  的 `insight` 有无条件默认串,这一条**结构上不可能为假**;
* 于是 `commit_freeze` 照常全额扣款,客户拿到一份满篇「暂无结论」的 0 分「隐形级」报告。

所以判据必须**真的把钱走一遍**:真 `point_freezes` 行、真 `user_wallets` 三池、
真 `middleware.billing.commit_freeze/release_freeze`(五保护文件,只调不改)。
把 billing mock 掉就等于把被测的那一行换成夹具自己 —— 本仓管这叫「夹具替被测代码干活」,
那样写出来的判据恒绿。

## 判据成对(每个"必须命中"配一个"必须不命中")

| # | 必须命中 | 配对的必须不命中 |
|---|---------|----------------|
| ① | 零成功观测 → freeze 落 `released`、钱包复原 | 有成功观测 → 落 `committed`(不误杀) |
| ② | 降级交付 → 按比例扣(0.25 / 0.75 两档) | 判定为 sufficient → 全额扣 |
| ③ | 零成功的终态快照里**没有任何等级词** | 正常成交的快照里**有**分数/等级(证明探针打得进正样本) |
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
PROD_SCHEMA = pathlib.Path(os.getenv(
    "P03_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
ADMIN_DSN = os.getenv(
    "P03_TEST_DSN", "postgresql://geo_admin:p03pass@localhost:55433/p03_test")

OWNER_UID = 770301
FEATURE = "geo_diagnosis"

#: 全站等级词全集 —— 两套评分 SSOT 各 6 档,一个都不许出现在"没测成"的响应里。
#: 分母不是我手写的:漏斗档来自 tools/scoring/funnel_score.FUNNEL_LEVEL_META,
#: 总分档来自 tools/scoring/scoring_levels.LEVEL_META(见 test_level_vocabulary_is_the_whole_ssot
#: 那条锁 —— 它保证这张表跟着 SSOT 走,SSOT 加一档而这里没加就判红)。
FUNNEL_LEVELS = ["主导级", "健康级", "成长级", "边缘级", "危急级", "隐形级"]
SCORE_LEVELS = ["领先", "成熟", "成长", "起步", "待提升", "空白"]
ALL_LEVEL_WORDS = FUNNEL_LEVELS + SCORE_LEVELS


def _admin_dsn() -> str:
    return ADMIN_DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def live_db():
    if not PROD_SCHEMA.is_file():
        pytest.skip("需要生产 schema 夹具 " + str(PROD_SCHEMA))
    name = "p03_" + uuid.uuid4().hex[:8] + "_test"
    if "test" not in name:                       # 安全栓:库名必须含 test
        raise RuntimeError("unsafe test db name " + name)
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
    # pg_dump 尾部会把 search_path 设成 '' —— 不复位的话后续所有无前缀 SQL 全找不到表。
    cur.execute("SET search_path = public")
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


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


# 客户报告的真实形态是 {"client": {"modules": {"1": {...}}}}(services/report_html_renderer.py:447)。
# 写错这层包装 → is_client_report_ready 返 False → 结算因"报告没就绪"改道退款,
# ① 那条判据就会**因为错误的原因**变绿(排障探针当场抓到过这一次假绿)。
# 模块 1 的 insight 用生产的无条件默认串,level 故意塞「隐形级」当毒。
REPORT_READY = json.dumps(
    {"client": {"modules": {"1": {"insight": "你的 GEO 现状如下。",
                                  "level": "隐形级"}}}},
    ensure_ascii=False)


def _world(dsn, *, engines, successful_tests, frozen=650, paid=10_000,
           total_score=0, level="隐形级", split=None):
    """造一单**真**的待结算诊断:钱包 + 冻结行 + run 行 + 产物行。

    engines / successful_tests 直接落 diagnosis_records 的耐久列
    (`ai_engines_tested` / `ai_total_tests`)—— 判据要驱动的就是被测代码从这两列
    读出来的那个判断,不在测试里替它算好结论。
    """
    from services.diagnosis_runs import freeze_task_ref, mint_run_token
    c = _conn(dsn)
    cur = c.cursor()
    uid = OWNER_UID
    cur.execute("INSERT INTO users (id, username, display_name, password_hash, email) "
                "VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
                (uid, "p03_owner", "p03_owner", "p03@example.com"))
    pools = split or {"bonus": 0, "commission": 0, "paid": frozen}
    cur.execute(
        "INSERT INTO user_wallets (user_id, paid_points, bonus_points, commission_points, frozen_points) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id) DO UPDATE SET "
        "paid_points=EXCLUDED.paid_points, bonus_points=EXCLUDED.bonus_points, "
        "commission_points=EXCLUDED.commission_points, frozen_points=EXCLUDED.frozen_points",
        (uid, paid, paid, paid, frozen))
    cur.execute(
        "INSERT INTO feature_pricing (feature_code, feature_name, cost_points) "
        "VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO NOTHING",
        (FEATURE, "GEO 诊断", 650))
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                ("P03客户_" + uuid.uuid4().hex[:6], uid))
    brand_id = cur.fetchone()["id"]

    run_token = mint_run_token()
    session_id = "sess_" + uuid.uuid4().hex[:12]
    task_ref = freeze_task_ref(run_token)
    cur.execute(
        "INSERT INTO point_freezes (user_id, feature_code, amount_total, amount_bonus, "
        "amount_commission, amount_paid, status, task_ref, brand_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,'frozen',%s,%s) RETURNING id",
        (uid, FEATURE, frozen, pools["bonus"], pools["commission"], pools["paid"],
         task_ref, brand_id))
    freeze_id = cur.fetchone()["id"]

    cur.execute(
        "INSERT INTO diagnosis_records (session_id, brand_name, industry, brand_id, run_token, "
        "result_visibility, total_score, level, report_v2_modules_jsonb, "
        "ai_total_tests, ai_engines_tested, total_questions_tested) "
        "VALUES (%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s) RETURNING id",
        (session_id, "P03客户", "测试行业", brand_id, run_token, total_score, level, REPORT_READY,
         successful_tests, json.dumps(engines, ensure_ascii=False), 8))
    diagnosis_id = cur.fetchone()["id"]

    complete_snap = {
        "type": "complete", "stage": "done", "progress": 100, "done": True, "terminal": True,
        "diagnosis_id": diagnosis_id, "share_token": "shr_" + uuid.uuid4().hex[:8],
        "message": "诊断完成",
        "result": {"total_score": total_score, "level": level},
    }
    cur.execute(
        "INSERT INTO diagnosis_runs (run_token, session_id, owner_user_id, brand_id, "
        "client_request_id, billing_mode, freeze_task_ref, freeze_id, freeze_backend, "
        "run_status, final_snapshot_jsonb) "
        "VALUES (%s,%s,%s,%s,%s,'paid',%s,%s,'legacy','running',%s)",
        (run_token, session_id, uid, brand_id, "req_" + uuid.uuid4().hex[:10],
         task_ref, freeze_id, json.dumps(complete_snap, ensure_ascii=False)))
    c.close()
    return {"run_token": run_token, "session_id": session_id, "freeze_id": freeze_id,
            "diagnosis_id": diagnosis_id, "uid": uid, "frozen": frozen, "paid": paid,
            "snapshot": complete_snap}


def _settle(world, snapshot=None):
    from services.diagnosis_runs import commit_run
    return asyncio.run(commit_run(world["run_token"], snapshot or world["snapshot"]))


def _after(dsn, world):
    c = _conn(dsn)
    cur = c.cursor()
    cur.execute("SELECT status, amount_total FROM point_freezes WHERE id=%s", (world["freeze_id"],))
    freeze = cur.fetchone()
    cur.execute("SELECT run_status, final_snapshot_jsonb, last_settlement_error "
                "FROM diagnosis_runs WHERE run_token=%s", (world["run_token"],))
    run = cur.fetchone()
    cur.execute("SELECT result_visibility FROM diagnosis_records WHERE id=%s", (world["diagnosis_id"],))
    rec = cur.fetchone()
    cur.execute("SELECT paid_points, bonus_points, commission_points, frozen_points "
                "FROM user_wallets WHERE user_id=%s", (world["uid"],))
    wallet = cur.fetchone()
    c.close()
    return {"freeze": freeze, "run": run, "record": rec, "wallet": wallet}


def _verdict(outcome="degraded", ratio=0.75):
    from services.diagnosis_sample_contract import SAMPLE_CONTRACT_VERSION
    return {"version": SAMPLE_CONTRACT_VERSION, "outcome": outcome,
            "planned": 32, "succeeded": int(32 * ratio),
            "coverage_ratio": ratio, "billable_ratio": ratio,
            "failed_platforms": [], "message": ""}


# ===========================================================================
# ① 零成功观测 → 必 release(全额退),绝不 commit
# ===========================================================================

def test_zero_engine_success_releases_and_never_commits(live_db):
    w = _world(live_db, engines=["qwen", "deepseek", "kimi", "doubao"], successful_tests=0)
    out = _settle(w)
    a = _after(live_db, w)

    assert a["freeze"]["status"] == "released", (
        "四引擎零成功仍然扣了钱:freeze=%s" % a["freeze"]["status"])
    assert a["run"]["run_status"] == "released"
    assert a["record"]["result_visibility"] == "withheld"
    # 钱包必须**复原**:frozen 清零,三池一分不少(冻结时钱已从 paid 搬进 frozen,
    # release 把它搬回来 → paid 回到 10000+650)。
    assert a["wallet"]["frozen_points"] == 0
    assert a["wallet"]["paid_points"] == w["paid"] + w["frozen"]
    assert out.get("terminal") == "released"


def test_zero_success_is_recorded_as_not_measured_not_generic_failure(live_db):
    """退款理由必须写明「没测成」—— 运营据此区分我们的故障与客户的业务结论。"""
    w = _world(live_db, engines=["qwen"], successful_tests=0)
    _settle(w)
    err = (_after(live_db, w)["run"]["last_settlement_error"] or "")
    assert "commit_not_measured" in err, err


# --- 配对的必须不命中:有成功观测就绝不能被误杀 -------------------------------

def test_engine_success_still_commits_in_full(live_db):
    """反向锁:同一条链,只把成功数从 0 改成 24 → 必须照常全额成交。"""
    w = _world(live_db, engines=["qwen", "deepseek", "kimi", "doubao"], successful_tests=24,
               total_score=42, level="边缘级")
    out = _settle(w)
    a = _after(live_db, w)
    assert a["freeze"]["status"] == "committed", a["run"]["last_settlement_error"]
    assert a["run"]["run_status"] == "committed"
    assert a["record"]["result_visibility"] == "published"
    assert a["wallet"]["frozen_points"] == 0
    assert a["wallet"]["paid_points"] == w["paid"]        # 全额扣:冻结的钱没退回来
    assert out.get("ok") is True


def test_social_diagnosis_without_engines_is_untouched(live_db):
    """向后兼容锁:社媒/老行没跑引擎(ai_engines_tested 空)→ 判定不介入,行为不变。"""
    w = _world(live_db, engines=[], successful_tests=0, total_score=61, level="成长级")
    _settle(w)
    a = _after(live_db, w)
    assert a["freeze"]["status"] == "committed"
    assert a["run"]["run_status"] == "committed"


def test_legacy_row_with_null_columns_is_untouched(live_db):
    """向后兼容锁:老行两列都是 NULL → 不改判(绝不因为读不到就去动钱)。"""
    w = _world(live_db, engines=["qwen"], successful_tests=0)
    c = _conn(live_db)
    c.cursor().execute("UPDATE diagnosis_records SET ai_engines_tested=NULL, ai_total_tests=NULL "
                       "WHERE id=%s", (w["diagnosis_id"],))
    c.close()
    _settle(w)
    a = _after(live_db, w)
    assert a["freeze"]["status"] == "committed"


# ===========================================================================
# ② 部分成功 → 按已履约比例扣(两档)
# ===========================================================================

@pytest.mark.parametrize("ratio", [0.25, 0.75])
def test_degraded_delivery_charges_only_the_delivered_share(live_db, ratio):
    w = _world(live_db, engines=["qwen", "deepseek", "kimi", "doubao"],
               successful_tests=int(32 * ratio), total_score=30, level="危急级")
    snap = dict(w["snapshot"], delivery_verdict=_verdict("degraded", ratio))
    out = _settle(w, snap)
    a = _after(live_db, w)

    expected_charge = int(w["frozen"] * ratio)
    expected_refund = w["frozen"] - expected_charge
    assert a["freeze"]["status"] == "committed", a["run"]["last_settlement_error"]
    assert a["wallet"]["frozen_points"] == 0
    assert a["wallet"]["paid_points"] == w["paid"] + expected_refund, (
        "降级交付没有按比例退回未履约部分:ratio=%s 期望退 %s" % (ratio, expected_refund))
    assert out.get("ok") is True


def test_two_tiers_charge_different_amounts(live_db):
    """两档必须**真的不同** —— 否则"按比例"这条判据没有区分力(都退一样等于没按比例)。"""
    low = _world(live_db, engines=["qwen"], successful_tests=8)
    _settle(low, dict(low["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    refund_low = _after(live_db, low)["wallet"]["paid_points"] - low["paid"]

    high = _world(live_db, engines=["qwen"], successful_tests=24)
    _settle(high, dict(high["snapshot"], delivery_verdict=_verdict("degraded", 0.75)))
    refund_high = _after(live_db, high)["wallet"]["paid_points"] - high["paid"]

    assert refund_low > refund_high > 0, (refund_low, refund_high)


def test_sufficient_verdict_still_charges_full(live_db):
    """配对的必须不命中:判定为全履约 → 一分不退。"""
    w = _world(live_db, engines=["qwen"], successful_tests=32, total_score=72, level="健康级")
    _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("sufficient", 1.0)))
    a = _after(live_db, w)
    assert a["freeze"]["status"] == "committed"
    assert a["wallet"]["paid_points"] == w["paid"]


def test_multi_pool_freeze_goes_manual_instead_of_guessing_the_order(live_db):
    """两个池都出了钱 → 先扣哪个池无从得知 → 转人工,**不猜**(既不多收也不少收)。"""
    w = _world(live_db, engines=["qwen"], successful_tests=8, frozen=650,
               split={"bonus": 300, "commission": 0, "paid": 350})
    out = _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("degraded", 0.25)))
    a = _after(live_db, w)
    assert a["run"]["run_status"] == "settlement_manual"
    assert a["freeze"]["status"] == "frozen", "转人工时绝不能已经动过钱"
    assert "reserved_split_order_unknown" in (a["run"]["last_settlement_error"] or "")
    assert out.get("terminal") == "settlement_manual"


# ===========================================================================
# ③ 呈现:没测成时,用户拿到的东西里不许有任何等级词
# ===========================================================================

def test_not_measured_terminal_snapshot_carries_no_level_word(live_db):
    """终态快照就是用户看到的东西 —— server.py:4401 轮询兜底是
    `return {"found": True, **_snap}`,整份原样回前端。
    造世界时特意把 `result.level="隐形级"` 和 share_token 塞进了成功快照
    (生产就是这么写的),所以这条锁验的是**我们真的把它盖掉了**,不是"本来就没有"。
    """
    w = _world(live_db, engines=["qwen", "deepseek"], successful_tests=0)
    assert w["snapshot"]["result"]["level"] == "隐形级"      # 正样本自证:毒确实下进去了
    _settle(w)
    snap = _after(live_db, w)["run"]["final_snapshot_jsonb"]
    if isinstance(snap, str):
        snap = json.loads(snap)
    blob = json.dumps(snap, ensure_ascii=False)
    hits = [w_ for w_ in ALL_LEVEL_WORDS if w_ in blob]
    assert not hits, "没测成的终态快照里漏出了等级词: %s · snapshot=%s" % (hits, blob)
    assert snap.get("not_measured") is True
    assert snap.get("share_token") is None
    assert "算力已退回" in (snap.get("message") or "")


def test_probe_can_see_a_level_word_when_one_is_present(live_db):
    """探针活性自证:正常成交的终态快照里**必须**能被这套词表抓到等级词。
    否则上一条"没抓到"可能只是尺子坏了(词表拼错/blob 是空的),不是真的干净。
    """
    w = _world(live_db, engines=["qwen"], successful_tests=32, total_score=72, level="健康级")
    _settle(w, dict(w["snapshot"], delivery_verdict=_verdict("sufficient", 1.0)))
    snap = _after(live_db, w)["run"]["final_snapshot_jsonb"]
    if isinstance(snap, str):
        snap = json.loads(snap)
    blob = json.dumps(snap, ensure_ascii=False)
    assert [x for x in ALL_LEVEL_WORDS if x in blob], "探针打不进正样本,词表是坏的:" + blob


def test_level_vocabulary_is_the_whole_ssot(live_db):
    """词表分母锁:等级词全集必须**等于**两套 SSOT 的全部档位,不许手写漏档。
    (手写分母漏掉的那一档不会让任何判据变红 —— 所以这里从 SSOT 机械取。)
    """
    from tools.scoring.funnel_score import FUNNEL_LEVEL_META
    from tools.scoring.scoring_levels import LEVEL_META
    assert set(FUNNEL_LEVELS) == set(FUNNEL_LEVEL_META), set(FUNNEL_LEVEL_META)
    assert set(SCORE_LEVELS) == set(LEVEL_META), set(LEVEL_META)
