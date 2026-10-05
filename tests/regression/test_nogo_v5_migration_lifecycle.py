"""[v5 req3] answer_hash 迁移 run 生命周期 · PG 判别性行为测试(取代 v4 单轮测试)。

覆盖:
- forward 起 run(独立 run_id)+ 拍全量快照 + 迁移;
- rollback ① 还原被迁行旧哈希 ②【删迁移后新增行前先完整备份(不得无备份 DELETE)】③ 标 run rolled_back;
- rollback 后再次 forward → 【新 run_id + 新轮快照】;
- 【连续两轮 forward→rollback 数据零丢失】:原始 fact 两轮后仍在且旧哈希恢复,被删新增行全部有备份;
- precheck 在【存在未结束 forward run(非预期 phase)】/ 碰撞 / 孤儿时 ok=False;
- 无 active run 时 rollback 是 no-op(不误删)。

判别性:去掉 rollback 的 R3 备份→删 → test_rollback_backs_up_before_delete 失败;
        去掉 forward 的"active run 复用快照 / 新 run 新快照"分支 → 两轮/新轮测试失败。
"""
from __future__ import annotations
import sys
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

ENG, BATCH = "V5MigEngine", "v5_mig_batch"
OLD_HASH_SQL = "MD5(COALESCE(r.answer_text,''))"
NEW_HASH_SQL = "MD5(COALESCE(r.query,'')||E'\\x1f'||COALESCE(r.industry,'')||E'\\x1f'||COALESCE(r.answer_text,''))"


def _conn():
    c = psycopg2.connect(DB); c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _clean():
    # [v5 req5] 清表前显式授权 + 二次校验目标确是本机 test/throwaway 库
    require_destructive_allowed()
    _assert_safe_test_db(DB)
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
    cur.execute("INSERT INTO geo_research_raw (query, industry, answer_text, engine, batch_id, cited_platform) "
                "VALUES (%s,%s,%s,%s,%s,'web') RETURNING id", (q, ind, ans, ENG, BATCH))
    rid = cur.fetchone()["id"]
    cur.execute(f"INSERT INTO geo_research_answer_facts (raw_id, engine, batch_id, answer_hash, industry, query) "
                f"SELECT r.id, %s, %s, {OLD_HASH_SQL}, r.industry, r.query FROM geo_research_raw r WHERE r.id=%s "
                f"RETURNING id", (ENG, BATCH, rid))
    return rid, cur.fetchone()["id"]


def _insert_new_code_row(cur, q, ind, ans):
    """模拟迁移后【新代码】按新哈希插入(不在本轮快照)+ 一条 entity。返回 new_fid。"""
    cur.execute("INSERT INTO geo_research_raw (query, industry, answer_text, engine, batch_id, cited_platform) "
                "VALUES (%s,%s,%s,%s,%s,'web') RETURNING id", (q, ind, ans, ENG, BATCH))
    rid2 = cur.fetchone()["id"]
    cur.execute(f"INSERT INTO geo_research_answer_facts (raw_id, engine, batch_id, answer_hash, industry, query) "
                f"SELECT r.id, %s, %s, {NEW_HASH_SQL}, r.industry, r.query FROM geo_research_raw r WHERE r.id=%s "
                f"RETURNING id", (ENG, BATCH, rid2))
    new_fid = cur.fetchone()["id"]
    cur.execute("INSERT INTO geo_research_answer_entities (answer_fact_id, entity_name, entity_key, engine) "
                "VALUES (%s,'e','ek',%s)", (new_fid, ENG))
    return new_fid


def _hash_is_old(cur, fid) -> bool:
    cur.execute(f"SELECT COUNT(*) AS n FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id=r.id "
                f"WHERE f.id=%s AND f.answer_hash={OLD_HASH_SQL}", (fid,))
    return cur.fetchone()["n"] == 1


def test_forward_creates_run_and_snapshot():
    from scripts.migrate_answer_hash_2026_07_12 import forward, SNAP
    with _conn() as c:
        cur = c.cursor()
        _rid, fid = _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        fr = forward(cur); c.commit()
        assert fr["reused"] is False and fr["run_id"] is not None, f"首次 forward 应起新 run: {fr}"
        assert fr["updated"] == 1 and fr["snapshot"] == 1, fr
        cur.execute(f"SELECT COUNT(*) AS n FROM {SNAP} WHERE run_id=%s AND fact_id=%s", (fr["run_id"], fid))
        assert cur.fetchone()["n"] == 1, "快照必须含被迁 fact"


def test_rollback_backs_up_before_delete():
    """rollback 删迁移后新增行【前】必须完整备份(facts+entities)→ deleted 表可审计。"""
    from scripts.migrate_answer_hash_2026_07_12 import forward, rollback, DEL
    with _conn() as c:
        cur = c.cursor()
        _rid, fid = _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        fr = forward(cur); c.commit()
        new_fid = _insert_new_code_row(cur, "Q2", "IND", "ANS2"); c.commit()
        rb = rollback(cur); c.commit()
        assert rb["purged_facts"] == 1 and rb["purged_entities"] == 1, rb
        # 🔴 删除前必须已备份:deleted 表有 fact + entity 各一条(run_id 对应)
        cur.execute(f"SELECT kind, COUNT(*) AS n FROM {DEL} WHERE run_id=%s GROUP BY kind", (rb["run_id"],))
        by_kind = {row["kind"]: row["n"] for row in cur.fetchall()}
        assert by_kind.get("fact") == 1, f"删的 fact 必须先备份进 deleted 表: {by_kind}"
        assert by_kind.get("entity") == 1, f"删的 entity 必须先备份进 deleted 表: {by_kind}"
        # 备份内容为整行 to_jsonb(含 answer_hash / id)
        cur.execute(f"SELECT row_json FROM {DEL} WHERE run_id=%s AND kind='fact'", (rb["run_id"],))
        rj = cur.fetchone()["row_json"]
        assert int(rj["id"]) == int(new_fid), "备份的整行必须是被删的那条 fact"
        # 原始行还原旧哈希
        assert _hash_is_old(cur, fid), "被迁 fact 必须还原旧哈希"


def test_forward_after_rollback_creates_new_run_and_snapshot():
    """rollback 后再次 forward → 新 run_id + 新轮快照(不复用旧 run)。"""
    from scripts.migrate_answer_hash_2026_07_12 import forward, rollback, SNAP
    with _conn() as c:
        cur = c.cursor()
        _rid, fid = _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        fr1 = forward(cur); c.commit()
        rollback(cur); c.commit()
        fr2 = forward(cur); c.commit()
        assert fr2["run_id"] != fr1["run_id"], f"rollback 后 forward 必须新 run: {fr1['run_id']} vs {fr2['run_id']}"
        assert fr2["reused"] is False, "rollback 后不得复用旧 run"
        # 新轮快照独立存在
        cur.execute(f"SELECT COUNT(*) AS n FROM {SNAP} WHERE run_id=%s", (fr2["run_id"],))
        assert cur.fetchone()["n"] == 1, "新轮必须有独立快照"


def test_forward_rerun_same_run_reuses_snapshot():
    """同轮内重跑 forward(未 rollback)→ 复用 run + 快照(幂等 · 不新拍不新 run)。"""
    from scripts.migrate_answer_hash_2026_07_12 import forward, RUNS
    with _conn() as c:
        cur = c.cursor()
        _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        fr1 = forward(cur); c.commit()
        fr2 = forward(cur); c.commit()
        assert fr2["run_id"] == fr1["run_id"] and fr2["reused"] is True, "同轮重跑应复用 run"
        assert fr2["updated"] == 0, "已迁行第二次 forward 迁 0(幂等)"
        cur.execute(f"SELECT COUNT(*) AS n FROM {RUNS} WHERE phase='forward'")
        assert cur.fetchone()["n"] == 1, "同轮重跑不得新建 run"


def test_two_rounds_forward_rollback_zero_data_loss():
    """🔴 连续两轮 forward→rollback:原始 fact 零丢失(两轮后仍在 + 旧哈希恢复),被删新增行全部有备份。"""
    from scripts.migrate_answer_hash_2026_07_12 import forward, rollback, DEL
    with _conn() as c:
        cur = c.cursor()
        _rid, fid = _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()

        # ---- 轮 1 ----
        r1 = forward(cur); c.commit()
        n1 = _insert_new_code_row(cur, "Q2", "IND", "ANS2"); c.commit()
        rb1 = rollback(cur); c.commit()
        assert _hash_is_old(cur, fid), "轮1后原始 fact 必须还原旧哈希"
        cur.execute("SELECT COUNT(*) AS n FROM geo_research_answer_facts WHERE id=%s", (n1,))
        assert cur.fetchone()["n"] == 0, "轮1新增行已清"

        # ---- 轮 2 ----(原始 fact 现为旧哈希 → 可再迁)
        r2 = forward(cur); c.commit()
        assert r2["run_id"] != r1["run_id"], "轮2必须新 run"
        assert r2["updated"] == 1, "轮2原始 fact 可再迁"
        n2 = _insert_new_code_row(cur, "Q3", "IND", "ANS3"); c.commit()
        rb2 = rollback(cur); c.commit()

        # 零丢失:原始 fact 仍在 + 旧哈希恢复
        cur.execute("SELECT COUNT(*) AS n FROM geo_research_answer_facts WHERE id=%s", (fid,))
        assert cur.fetchone()["n"] == 1, "🔴 两轮后原始 fact 必须仍在(零丢失)"
        assert _hash_is_old(cur, fid), "🔴 两轮后原始 fact 旧哈希恢复"
        # 两轮被删新增行都有备份(不得无备份 DELETE)
        cur.execute(f"SELECT COUNT(*) AS n FROM {DEL} WHERE kind='fact'")
        assert cur.fetchone()["n"] == 2, "两轮各删 1 新增 fact,均应有备份"


def test_precheck_nonzero_on_active_forward_run():
    """存在未结束 forward run(非预期 phase)→ precheck ok=False → _main 非零退出。"""
    from scripts.migrate_answer_hash_2026_07_12 import forward, precheck
    with _conn() as c:
        cur = c.cursor()
        _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        forward(cur); c.commit()  # 留下 active run
        pc = precheck(cur); c.commit()
        assert pc["ok"] is False, f"存在 active forward run 时 precheck 必须非零(ok=False): {pc}"
        assert pc["active_forward_run_id"] is not None
        assert any("未结束" in p for p in pc["problems"]), pc["problems"]


def test_precheck_ok_when_clean():
    from scripts.migrate_answer_hash_2026_07_12 import precheck
    with _conn() as c:
        cur = c.cursor()
        _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        pc = precheck(cur); c.commit()
        assert pc["ok"] is True, f"干净库 precheck 应 ok: {pc}"


def test_rollback_no_active_run_is_noop():
    from scripts.migrate_answer_hash_2026_07_12 import rollback
    with _conn() as c:
        cur = c.cursor()
        _rid, fid = _seed_old(cur, "Q1", "IND", "ANS1"); c.commit()
        rb = rollback(cur); c.commit()
        assert rb["run_id"] is None and rb["purged_facts"] == 0, f"无 active run rollback 必须 no-op: {rb}"
        cur.execute("SELECT COUNT(*) AS n FROM geo_research_answer_facts WHERE id=%s", (fid,))
        assert cur.fetchone()["n"] == 1, "no-op 不得误删原始 fact"
