"""[Deploy-CTO NO-GO v3 finding 5/8] answer_hash 迁移预检 + collision-safe UPDATE · DB 行为测试。

复现 Deploy-CTO 反证:v2 预检只查【待迁行彼此】冲突,漏查【待迁行 vs 已存在新哈希行】→ 预检 0 冲突
但 UPDATE 撞 UNIQUE 抛 UniqueViolation。
v3 修复:预检 0b-ii 检查待迁 vs 已存在;UPDATE 带 NOT EXISTS 守卫(撞已存在行则跳过,不报错)。

判别性:去掉 UPDATE 的 NOT EXISTS 守卫 → test_migration_update_no_unique_violation 抛 UniqueViolation。
需 throwaway PG(geo_research_raw + geo_research_answer_facts)。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
import pytest
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL")

ENG = "TestEngine"
BATCH = "nogo_mig_batch"
NEWHASH_SQL = "MD5(COALESCE(r.query,'')||E'\\x1f'||COALESCE(r.industry,'')||E'\\x1f'||COALESCE(r.answer_text,''))"


def _conn():
    c = psycopg2.connect(DB)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(autouse=True)
def _schema_and_seed():
    with _conn() as c:
        cur = c.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS geo_research_raw (
            id SERIAL PRIMARY KEY, query TEXT, industry TEXT, answer_text TEXT, engine TEXT, batch_id TEXT)""")
        # 清理本用例范围
        cur.execute("DELETE FROM geo_research_answer_facts WHERE batch_id=%s", (BATCH,))
        cur.execute("DELETE FROM geo_research_raw WHERE batch_id=%s", (BATCH,))
        # raw 行:一个撞组(R1)+ 一个非撞组(R2)
        cur.execute("INSERT INTO geo_research_raw (query, industry, answer_text, engine, batch_id, cited_platform) "
                    "VALUES ('QA','IND','ANS_COLLIDE',%s,%s,'web') RETURNING id", (ENG, BATCH))
        r1 = cur.fetchone()["id"]
        cur.execute("INSERT INTO geo_research_raw (query, industry, answer_text, engine, batch_id, cited_platform) "
                    "VALUES ('QB','IND','ANS_CLEAN',%s,%s,'web') RETURNING id", (ENG, BATCH))
        r2 = cur.fetchone()["id"]
        # R1 组:① 旧哈希 fact(MD5(answer_text)) ② 已存在的新哈希 canonical fact(MD5(q⊕i⊕a)) —— 二者同 engine/batch → 迁移会撞
        cur.execute(f"INSERT INTO geo_research_answer_facts (raw_id, engine, batch_id, answer_hash, industry, query) "
                    f"SELECT r.id, %s, %s, MD5(COALESCE(r.answer_text,'')), r.industry, r.query FROM geo_research_raw r WHERE r.id=%s",
                    (ENG, BATCH, r1))
        cur.execute(f"INSERT INTO geo_research_answer_facts (raw_id, engine, batch_id, answer_hash, industry, query) "
                    f"SELECT r.id, %s, %s, {NEWHASH_SQL}, r.industry, r.query FROM geo_research_raw r WHERE r.id=%s",
                    (ENG, BATCH, r1))
        # R2 组:只有旧哈希 fact(无撞)→ 应被正常迁移
        cur.execute(f"INSERT INTO geo_research_answer_facts (raw_id, engine, batch_id, answer_hash, industry, query) "
                    f"SELECT r.id, %s, %s, MD5(COALESCE(r.answer_text,'')), r.industry, r.query FROM geo_research_raw r WHERE r.id=%s",
                    (ENG, BATCH, r2))
        c.commit()
        cur.execute("SELECT id FROM geo_research_raw WHERE batch_id=%s ORDER BY id", (BATCH,))
        pytest._nogo_r1r2 = [row["id"] for row in cur.fetchall()]
    yield
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM geo_research_answer_facts WHERE batch_id=%s", (BATCH,))
        cur.execute("DELETE FROM geo_research_raw WHERE batch_id=%s", (BATCH,))
        c.commit()


def test_precheck_0bii_detects_existing_collision():
    """0b-ii:待迁旧哈希行(R1)的目标新哈希撞【已存在】canonical 行 → 预检必须报 >=1(v2 会报 0)。"""
    with _conn() as c:
        cur = c.cursor()
        cur.execute(f"""
            WITH mig AS (
              SELECT f.id, f.engine, f.batch_id, {NEWHASH_SQL} AS new_hash
                FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id = r.id
               WHERE f.batch_id=%s AND f.answer_hash = MD5(COALESCE(r.answer_text,''))
            )
            SELECT COUNT(*) AS n FROM mig
             WHERE EXISTS (SELECT 1 FROM geo_research_answer_facts e
                            WHERE e.engine=mig.engine AND e.batch_id=mig.batch_id
                              AND e.answer_hash=mig.new_hash AND e.id<>mig.id)
        """, (BATCH,))
        n = cur.fetchone()["n"]
    assert n >= 1, "🔴 预检必须检出【待迁 vs 已存在新哈希行】撞(v2 漏此类 → 迁移崩)"


def test_migration_update_no_unique_violation_and_skips_collider():
    """带 NOT EXISTS 守卫的 UPDATE:不抛 UniqueViolation;撞组 R1 的旧行被跳过、非撞组 R2 被迁移。"""
    with _conn() as c:
        cur = c.cursor()
        # v3 STEP2 UPDATE(带 NOT EXISTS 守卫)· 只作用本 batch
        try:
            cur.execute(f"""
                UPDATE geo_research_answer_facts f
                   SET answer_hash = {NEWHASH_SQL}, updated_at = NOW()
                  FROM geo_research_raw r
                 WHERE f.raw_id = r.id AND f.batch_id=%s
                   AND f.answer_hash = MD5(COALESCE(r.answer_text,''))
                   AND NOT EXISTS (
                       SELECT 1 FROM geo_research_answer_facts e
                        WHERE e.engine=f.engine AND e.batch_id=f.batch_id
                          AND e.answer_hash = {NEWHASH_SQL} AND e.id<>f.id)
            """, (BATCH,))
            c.commit()
        except psycopg2.errors.UniqueViolation:
            c.rollback()
            pytest.fail("🔴 带守卫的 UPDATE 不应抛 UniqueViolation(去掉 NOT EXISTS 守卫才会抛)")
        # R1 撞组的旧行仍是旧哈希(被跳过);R2 非撞行已迁成新哈希
        r1, r2 = pytest._nogo_r1r2[0], pytest._nogo_r1r2[1]
        cur.execute(f"""SELECT
            (SELECT COUNT(*) FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id=r.id
              WHERE f.raw_id=%s AND f.answer_hash = MD5(COALESCE(r.answer_text,''))) AS r1_old_remaining,
            (SELECT COUNT(*) FROM geo_research_answer_facts f JOIN geo_research_raw r ON f.raw_id=r.id
              WHERE f.raw_id=%s AND f.answer_hash = {NEWHASH_SQL}) AS r2_migrated
        """, (r1, r2))
        row = cur.fetchone()
    assert row["r1_old_remaining"] == 1, "撞组旧行应被跳过(仍旧哈希),交人工清理"
    assert row["r2_migrated"] == 1, "非撞组应被正常迁移成新哈希"
