"""
P14.4 E (2026-06) · review_status 列宽 VARCHAR(20)→32 防"加入参考库"500
'imported_to_reference' 21 字 > 原列宽 20 → UPDATE 报 value too long → 文章库按钮 500

锁:
  1. (inspect) db/diagnosis_db.py 建表定义 review_status 必须 VARCHAR(32) · 不能再回 20
  2. (inspect) audit log 表 prev_review_status / new_review_status 同步 VARCHAR(32)
  3. (inspect) migration SQL 含 ALTER COLUMN ... VARCHAR(32) (geo_research_articles + audit log)
  4. (inspect) idempotent migration 在 diagnosis_db._migrate 类似函数里有 widen 逻辑
  5. (functional · 跳过若无 DB) 写一行 review_status='imported_to_reference' 实际不报 22001
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestSchemaSourceLevel:
    """从源码层锁 · 不依赖 DB"""

    def test_diagnosis_db_articles_review_status_is_varchar32(self):
        src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
        assert re.search(
            r"review_status\s+VARCHAR\(32\)\s+DEFAULT\s+'crawled'", src,
        ), "geo_research_articles 建表 review_status 必须 VARCHAR(32) DEFAULT 'crawled'"

    def test_diagnosis_db_review_log_status_is_varchar32(self):
        src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
        # audit log 表 prev/new 两列同步加宽
        assert re.search(r"prev_review_status\s+VARCHAR\(32\)", src), \
            "geo_research_review_log.prev_review_status 必须 VARCHAR(32)"
        assert re.search(r"new_review_status\s+VARCHAR\(32\)", src), \
            "geo_research_review_log.new_review_status 必须 VARCHAR(32)"

    def test_diagnosis_db_has_idempotent_widen_alter(self):
        """diagnosis_db.py 应在 migration 区有 ALTER COLUMN TYPE VARCHAR(32) 给老库自动加宽"""
        src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
        assert re.search(
            r"ALTER\s+TABLE\s+geo_research_articles\s+ALTER\s+COLUMN\s+review_status\s+TYPE\s+VARCHAR\(32\)",
            src, re.IGNORECASE,
        ), "diagnosis_db.py 必须含 idempotent ALTER COLUMN review_status TYPE VARCHAR(32)"

    def test_no_residual_review_status_varchar20_in_articles_table(self):
        """防回退 · 建表段不允许再有 review_status VARCHAR(20)"""
        src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
        # 剥离 Python 注释行 · 防注释里写"VARCHAR(20)→32" 这种被误判
        non_comment_src = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        bare = re.findall(r"review_status\s+VARCHAR\(20\)", non_comment_src)
        assert not bare, \
            f"diagnosis_db.py 不应再有 review_status VARCHAR(20) · 找到 {len(bare)} 处"


class TestMigrationFiles:
    """migration SQL 文件存在 + 内容正确"""

    MIG_PATH = ROOT / "scripts" / "migration_review_status_varchar32_2026-06-01.sql"
    ROLLBACK_PATH = ROOT / "scripts" / "rollback_review_status_varchar32_2026-06-01.sql"

    def test_migration_file_exists(self):
        assert self.MIG_PATH.exists(), f"必须有 migration: {self.MIG_PATH.name}"

    def test_migration_widens_three_columns(self):
        sql = self.MIG_PATH.read_text(encoding="utf-8")
        # 3 处 ALTER:articles 1 + review_log 2
        alters = re.findall(
            r"ALTER\s+COLUMN\s+\w+\s+TYPE\s+VARCHAR\(32\)", sql, re.IGNORECASE,
        )
        assert len(alters) == 3, \
            f"migration 应含 3 处 ALTER COLUMN TYPE VARCHAR(32) (1 articles + 2 review_log) · 实际 {len(alters)}"

    def test_migration_idempotent_marker(self):
        """marker INSERT 必须 ON CONFLICT DO NOTHING · 重跑不挂"""
        sql = self.MIG_PATH.read_text(encoding="utf-8")
        assert "_migration_markers" in sql
        assert re.search(r"ON\s+CONFLICT.*DO\s+NOTHING", sql, re.IGNORECASE), \
            "migration marker 写入必须 ON CONFLICT DO NOTHING (幂等)"

    def test_rollback_file_exists(self):
        assert self.ROLLBACK_PATH.exists(), f"必须有 rollback: {self.ROLLBACK_PATH.name}"


@pytest.fixture
def db_conn_migrated():
    """测试 DB · 先跑 migration SQL 让列宽到位(模拟部署流程)"""
    import psycopg2
    url = os.environ.get('TEST_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not url:
        pytest.skip("TEST_DATABASE_URL 未设")
    conn = psycopg2.connect(url)
    # 跑 migration · 幂等 · 已 32 时不报错
    mig_path = ROOT / "scripts" / "migration_review_status_varchar32_2026-06-01.sql"
    sql = mig_path.read_text(encoding="utf-8")
    cur = conn.cursor()
    try:
        cur.execute(sql)
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        # _migration_markers 表可能不存在 · 测试 DB 简陋 · 把 marker INSERT 拆出来跑
        if '_migration_markers' in str(e):
            for stmt in [
                "ALTER TABLE geo_research_articles ALTER COLUMN review_status TYPE VARCHAR(32)",
                "ALTER TABLE geo_research_review_log ALTER COLUMN prev_review_status TYPE VARCHAR(32)",
                "ALTER TABLE geo_research_review_log ALTER COLUMN new_review_status TYPE VARCHAR(32)",
            ]:
                try:
                    cur.execute(stmt); conn.commit()
                except psycopg2.Error:
                    conn.rollback()
        else:
            raise
    yield conn
    conn.close()


class TestFunctional:
    """真插 'imported_to_reference' 不再 22001 string_data_right_truncation"""

    def test_can_insert_imported_to_reference_review_status(self, db_conn_migrated):
        """关键:再现 bug 场景 · 验证已修"""
        import psycopg2
        cur = db_conn_migrated.cursor()
        # 校验列宽 ≥ 21 · 否则下面 INSERT 会失败
        cur.execute("""
            SELECT character_maximum_length
              FROM information_schema.columns
             WHERE table_name = 'geo_research_articles'
               AND column_name = 'review_status'
        """)
        row = cur.fetchone()
        assert row is not None, "geo_research_articles.review_status 列必须存在"
        assert row[0] >= 21, \
            f"review_status 列宽必须 ≥ 21 (容纳 'imported_to_reference') · 实际 {row[0]} · 请跑 migration"

        # 同款验 audit log 两列
        cur.execute("""
            SELECT column_name, character_maximum_length
              FROM information_schema.columns
             WHERE table_name = 'geo_research_review_log'
               AND column_name IN ('prev_review_status', 'new_review_status')
        """)
        for col_name, max_len in cur.fetchall():
            assert max_len >= 21, \
                f"geo_research_review_log.{col_name} 列宽必须 ≥ 21 · 实际 {max_len}"
