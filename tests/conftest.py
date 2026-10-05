"""
tests/conftest.py — 项目根级测试 fixture

关键职责：
  - 在任何业务代码 import 前，把 DATABASE_URL 切到 TEST_DATABASE_URL
    （services.placement_service / db.connection 模块加载时会缓存 DATABASE_URL）
  - 提供 db_with_clean_research：每个测试前清空研究相关表，并重置默认配置
"""
import os
from pathlib import Path

# ============================================================
# 在 import 任何业务模块前，把 DATABASE_URL 切成测试库
# ============================================================
from dotenv import load_dotenv

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

_test_url = os.getenv("TEST_DATABASE_URL")
if not _test_url:
    raise RuntimeError(
        "TEST_DATABASE_URL 未配置。请在 .env 中设置 TEST_DATABASE_URL "
        "指向独立的测试库（不要指生产）"
    )

# 保险栓：禁止测试 DB 名包含 'prod' 或缺 'test'
_dbname = _test_url.rsplit("/", 1)[-1].lower()
if "prod" in _dbname or "test" not in _dbname:
    if not os.getenv("ALLOW_NONTEST_DB"):
        raise RuntimeError(
            f"测试库名 '{_dbname}' 不安全（应包含 'test'）。"
            "如确认无误，可设 ALLOW_NONTEST_DB=1 跳过此检查。"
        )

# 把 DATABASE_URL 替换掉，让所有后续 import 看到测试库
os.environ["DATABASE_URL"] = _test_url

# ============================================================
# fixture
# ============================================================
import pytest
import psycopg2
import psycopg2.extras


@pytest.fixture
def db_with_clean_research():
    """每个测试：清空研究表 + 重置默认配置 + yield 一个 RealDictCursor 连接"""
    conn = psycopg2.connect(_test_url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    c = conn.cursor()
    # 清表（只清测试库的；保险栓在上面已校验）
    for t in ("geo_month_weights", "geo_engine_stats", "geo_research_raw"):
        c.execute(f"DELETE FROM {t}")
    # 重置默认配置
    c.execute(
        "UPDATE geo_aggregation_config SET value=%s WHERE key='decay_factor'",
        ("0.3",),
    )
    c.execute(
        "UPDATE geo_aggregation_config SET value=%s WHERE key='min_queries_per_month'",
        ("10",),
    )
    conn.commit()
    yield conn
    # 收尾再清一次
    for t in ("geo_month_weights", "geo_engine_stats", "geo_research_raw"):
        c.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
