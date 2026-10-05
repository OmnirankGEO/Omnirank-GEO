"""[v6 req4] answer_hash 迁移 · advisory lock + 唯一活动轮 + finalize/accepted + 经测试 restore · 行为测试。

判别性:去掉 uniq_active_forward_run 索引 → test_unique_active_forward_enforced 失败;
        去掉 restore 的重插 → test_restore_reinserts_deleted 失败。
"""
from __future__ import annotations
import sys
import threading
from pathlib import Path
import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dbsafe import resolve_test_db_url, require_destructive_allowed, _assert_safe_test_db  # noqa: E402

DB = resolve_test_db_url()
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL(本机 test/throwaway 库)")

ENG, BATCH = "V6MigEngine", "v6_mig_batch"
OLD = "MD5(COALESCE(r.answer_text,''))"
NEW = "MD5(COALESCE(r.query,'')||E'\\x1f'||COALESCE(r.industry,'')||E'\\x1f'||COALESCE(r.answer_text,''))"


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _clean():
    require_destructive_allowed(); _assert_safe_test_db(DB)
    from scripts.migrate_answer_hash_2026_07_12 import RUNS, SNAP, BAK, DEL
    with _conn() as c:
        cur = c.cursor()
        for t in ("geo_research_answer_entities", "geo_research_answer_facts", "geo_research_raw"):
            cur.execute(f"DELETE FROM {t}")
        for t in (DEL, BAK, SNAP, RUNS):
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        c.commit()
    yield
    with _conn() as c:
        cur = c.cursor()
        for t in ("geo_research_answer_entities", "geo_research_answer_facts", "geo_research_raw"):
            cur.execute(f"DELETE FROM {t}")
        for t in (DEL, BAK, SNAP, RUNS):
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        c.commit()


def _seed_old(cur, q, ind, ans):
    cur.execute("INSERT INTO geo_research_raw (query,industry,answer_text,engine,batch_id,cited_platform) "
                "VALUES (%s,%s,%s,%s,%s,'web') RETURNING id", (q, ind, ans, ENG, BATCH))
    rid = cur.fetchone()["id"]
    cur.execute(f"INSERT INTO geo_research_answer_facts (raw_id,engine,batch_id,answer_hash,industry,query) "
                f"SELECT r.id,%s,%s,{OLD},r.industry,r.query FROM geo_research_raw r WHERE r.id=%s RETURNING id",
                (ENG, BATCH, rid))
    return rid, cur.fetchone()["id"]


def _new_code_row(cur, q, ind, ans):
    cur.execute("INSERT INTO geo_research_raw (query,industry,answer_text,engine,batch_id,cited_platform) "
                "VALUES (%s,%s,%s,%s,%s,'web') RETURNING id", (q, ind, ans, ENG, BATCH))
    rid = cur.fetchone()["id"]
    cur.execute(f"INSERT INTO geo_research_answer_facts (raw_id,engine,batch_id,answer_hash,industry,query) "
                f"SELECT r.id,%s,%s,{NEW},r.industry,r.query FROM geo_research_raw r WHERE r.id=%s RETURNING id",
                (ENG, BATCH, rid))
    fid = cur.fetchone()["id"]
    cur.execute("INSERT INTO geo_research_answer_entities (answer_fact_id,entity_name,entity_key,engine) "
                "VALUES (%s,'e','ek',%s)", (fid, ENG))
    return fid


def test_finalize_forward_to_accepted():
    from scripts.migrate_answer_hash_2026_07_12 import forward, finalize, RUNS
    with _conn() as c:
        cur = c.cursor()
        _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        fr = forward(cur); c.commit()
        fin = finalize(cur); c.commit()
        assert fin["accepted"] is True and fin["run_id"] == fr["run_id"]
        cur.execute(f"SELECT phase FROM {RUNS} WHERE run_id=%s", (fr["run_id"],))
        assert cur.fetchone()["phase"] == "accepted"
        # accepted 后再 forward → 新 run(不复用 accepted)
        _seed_old(cur, "Q2", "IND", "ANS2"); c.commit()
        fr2 = forward(cur); c.commit()
        assert fr2["run_id"] != fr["run_id"] and fr2["reused"] is False


def test_unique_active_forward_enforced():
    """🔴 唯一活动轮:同时至多一个 phase='forward' 的 run(部分唯一索引)。"""
    from scripts.migrate_answer_hash_2026_07_12 import forward, RUNS
    with _conn() as c:
        cur = c.cursor()
        _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        forward(cur); c.commit()   # 建 active forward run + 索引
        # 手工再插一条 phase='forward' → 唯一活动轮索引应拒
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(f"INSERT INTO {RUNS} (phase) VALUES ('forward')")
        c.rollback()


def test_restore_reinserts_deleted():
    """🔴 rollback 备份后删的行,restore 能整行重插回来(可逆 · 非只删)。"""
    from scripts.migrate_answer_hash_2026_07_12 import forward, rollback, restore
    with _conn() as c:
        cur = c.cursor()
        _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        fr = forward(cur); c.commit()
        new_fid = _new_code_row(cur, "Q2", "IND", "ANS2"); c.commit()
        rb = rollback(cur); c.commit()
        assert rb["purged_facts"] == 1 and rb["purged_entities"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM geo_research_answer_facts WHERE id=%s", (new_fid,))
        assert cur.fetchone()["n"] == 0, "rollback 已删新增行"
        # restore 该轮 → 被删的 fact + entity 回来
        res = restore(cur, rb["run_id"]); c.commit()
        assert res["restored_facts"] == 1 and res["restored_entities"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM geo_research_answer_facts WHERE id=%s", (new_fid,))
        assert cur.fetchone()["n"] == 1, "🔴 restore 必须把删掉的 fact 重插回来"
        cur.execute("SELECT COUNT(*) AS n FROM geo_research_answer_entities WHERE answer_fact_id=%s", (new_fid,))
        assert cur.fetchone()["n"] == 1, "🔴 restore 必须把删掉的 entity 重插回来"


def test_concurrent_forward_advisory_lock_fast_fail_single_run():
    """🔴 [v7 finding7] 双迁移并发:两线程同时 forward → try-advisory-lock【快速失败】· 不阻塞。

    不变量:至多一个 phase='forward' run;绝不产生两个不同新 run。
    行为:抢到锁者建 run;抢不到者【立即 raise】(不阻塞等待)—— 消除旧阻塞版"锁竞争不快速失败"。
    """
    from scripts.migrate_answer_hash_2026_07_12 import forward, RUNS
    with _conn() as c:
        cur = c.cursor(); _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
    results = []
    barrier = threading.Barrier(2)

    def _run():
        barrier.wait()
        try:
            with _conn() as c:
                cur = c.cursor()
                r = forward(cur); c.commit()
                results.append(("ok", r))
        except Exception as e:  # noqa: BLE001
            results.append(("err", repr(e)))

    ts = [threading.Thread(target=_run) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]

    oks = [r for tag, r in results if tag == "ok"]
    errs = [r for tag, r in results if tag == "err"]
    assert len(oks) >= 1, f"至少一个线程应成功建 run · results={results}"
    run_ids = {r["run_id"] for r in oks}
    assert len(run_ids) == 1, f"🔴 并发 forward 绝不产生两个不同新 run · 实际 {run_ids}"
    # 抢不到锁的线程必须是【快速失败】(advisory lock 报错),不是别的崩溃 / 不是阻塞
    for e in errs:
        assert "advisory lock" in e, f"🔴 并发失败必须是 advisory lock 快速失败 · 实际 {e}"
    with _conn() as c:
        cur = c.cursor(); cur.execute(f"SELECT COUNT(*) AS n FROM {RUNS} WHERE phase='forward'")
        assert cur.fetchone()["n"] == 1, "🔴 不变量:至多一个 active forward run"
