"""
tests/ai_ops conftest · 2026-07-01

参考 tests/research_monitor/conftest.py 的 setup_test_db 模式:
  - 依赖根 tests/conftest.py 在 import 前已把 DATABASE_URL 切成 TEST_DATABASE_URL。
  - session 级 setup_ai_ops_db:在测试库建最小 faq_feedback 依赖表 + 跑 AI Ops migration。
  - pg_conn:每测试独立连接(RealDictCursor)。
  - clean_ai_ops:每测试前后 TRUNCATE ai_ops_* + faq_feedback,并复位默认策略种子。

防误连生产:测试库 URL 必须含 'test'(与 research_monitor 一致)。
"""
import os
from pathlib import Path

import pytest
import psycopg2
from psycopg2.extras import RealDictCursor

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_SQL = _PROJECT_ROOT / "scripts" / "migration_ai_ops_center_2026_07_01.sql"

# 依赖顺序:先删引用 ai_ops_tasks 的子表,再删主表(TRUNCATE ... CASCADE 已足够,但显式列全)
AI_OPS_TABLES = [
    "ai_ops_task_events",
    "ai_ops_artifacts",
    "ai_ops_approvals",
    "ai_ops_reports",
    "ai_ops_policies",
    "ai_ops_worker_heartbeats",  # P1-B · 独立表(无 FK),TRUNCATE 复位
    "ai_ops_alerts",             # 包B · 巡逻告警(FK→tasks)
    "ai_ops_patrol_runs",        # 包B · 巡逻打卡
    "ai_ops_tasks",
]

_POLICY_SEED_SQL = """
INSERT INTO ai_ops_policies (key, value_jsonb) VALUES
  ('ai_ops.enabled',        '{"enabled": false}'::jsonb),
  ('codex.diagnose.enabled','{"enabled": true}'::jsonb),
  ('codex.fix.enabled',     '{"enabled": false}'::jsonb),
  ('ssh_runner.enabled',    '{"enabled": false}'::jsonb),
  ('ai_ops.kill_switch',    '{"enabled": false}'::jsonb),
  ('ai_ops.chat_llm.enabled','{"enabled": false}'::jsonb),
  ('ai_ops.glm_triage.enabled','{"enabled": false}'::jsonb),
  ('auto_deploy.enabled',   '{"enabled": false}'::jsonb),
  ('ai_ops.auto_create_from_feedback','{"enabled": false}'::jsonb),
  ('ai_ops.patrol.enabled', '{"enabled": false}'::jsonb)
ON CONFLICT (key) DO NOTHING;
"""

# faq_feedback 依赖(task_service.get_feedback_row / overview 反馈计数读它)。
# 只建 AI Ops 用到的列子集,跟生产 db/faq_db.py 的 faq_feedback 同名同义。
_FAQ_FEEDBACK_MIN_SQL = """
CREATE TABLE IF NOT EXISTS faq_feedback (
  id                 SERIAL PRIMARY KEY,
  client_id          VARCHAR(64) UNIQUE,
  faq_id             INTEGER,
  message            TEXT NOT NULL DEFAULT '',
  urgency            VARCHAR(10) NOT NULL DEFAULT 'low',
  contact            TEXT NOT NULL DEFAULT '',
  user_id            INTEGER NOT NULL DEFAULT 0,
  kind               VARCHAR(10) NOT NULL DEFAULT 'faq',
  status             VARCHAR(10) NOT NULL DEFAULT 'pending',
  screenshot_url     TEXT NOT NULL DEFAULT '',
  ai_answer          TEXT NOT NULL DEFAULT '',
  admin_note         TEXT NOT NULL DEFAULT '',
  submitter_identity VARCHAR(20) NOT NULL DEFAULT '',
  submitter_agent_level INTEGER NOT NULL DEFAULT 0,
  created_at         TIMESTAMP DEFAULT NOW(),
  handled_at         TIMESTAMP,
  handled_by         INTEGER
);
"""


def _test_url() -> str:
    url = os.environ.get("DATABASE_URL", "") or os.environ.get("TEST_DATABASE_URL", "")
    if "test" not in url.lower():
        raise RuntimeError(f"测试库 URL 必须含 'test' 防误连生产,当前: {url[:50]}...")
    return url


@pytest.fixture(scope="session")
def setup_ai_ops_db():
    """在测试库建 faq_feedback 依赖 + 跑 AI Ops migration(等价 server._run_sql_migrations 建表)。"""
    url = _test_url()
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(_FAQ_FEEDBACK_MIN_SQL)
    sql = _MIGRATION_SQL.read_text(encoding="utf-8")
    cur.execute(sql)
    conn.close()
    yield


@pytest.fixture
def pg_conn(setup_ai_ops_db):
    url = _test_url()
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = False
    yield conn
    try:
        conn.rollback()
    except Exception:
        pass
    conn.close()


@pytest.fixture
def clean_ai_ops(setup_ai_ops_db):
    """每测试前后 TRUNCATE ai_ops_* + faq_feedback,并复位默认策略种子。"""
    url = _test_url()
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()

    def _reset():
        cur.execute(
            "TRUNCATE TABLE " + ", ".join(AI_OPS_TABLES) + " RESTART IDENTITY CASCADE"
        )
        cur.execute("TRUNCATE TABLE faq_feedback RESTART IDENTITY CASCADE")
        cur.execute(_POLICY_SEED_SQL)  # 复位默认策略(TRUNCATE 会清掉 migration 的种子)

    _reset()
    yield
    _reset()
    conn.close()


# 在 autouse patch 之前捕获三个 seam 的真实函数体——
# 需要测真实函数逻辑(如"无 key 早退")的用例用 real_llm_seams 拿原函数,
# 否则测的是 autouse 假函数 = 假绿(包B.2 复审 P2-2)。
from services.ai_ops import chat_agent as _ca, chat_intent as _ci, chat_tools as _ct  # noqa: E402

_REAL_LLM_SEAMS = {
    "chat_intent._call_llm": _ci._call_llm,
    "chat_agent._call_llm_generate": _ca._call_llm_generate,
    "chat_tools._call_llm_tools": _ct._call_llm_tools,
}


@pytest.fixture
def real_llm_seams():
    return dict(_REAL_LLM_SEAMS)


@pytest.fixture(autouse=True)
def _no_real_llm_calls(monkeypatch):
    """
    G-8 加固(包B.2):三个 LLM seam 默认置 None,测试进程绝不真调外部 API
    (conftest load_dotenv 会带进真实 DEEPSEEK_API_KEY,不能靠"没 key"兜底)。
    需要脚本化 LLM 行为的测试,在测试体内再次 monkeypatch 覆盖即可。
    """
    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(_ci, "_call_llm", _none)
    monkeypatch.setattr(_ca, "_call_llm_generate", _none)
    monkeypatch.setattr(_ct, "_call_llm_tools", _none)


@pytest.fixture
def make_bug_feedback(pg_conn):
    """工厂:插一条 bug 反馈,返回 id。"""
    counter = [0]

    def _make(message="页面按钮点不动", urgency="high", kind="bug", status="pending"):
        counter[0] += 1
        cur = pg_conn.cursor()
        cur.execute(
            """
            INSERT INTO faq_feedback (client_id, message, urgency, kind, status, user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (f"cid_{counter[0]}", message, urgency, kind, status, 10),
        )
        fid = cur.fetchone()["id"]
        pg_conn.commit()
        return fid

    return _make
