"""metaso api_key 延迟读取 + regenerating 看门狗 · 判据锁(WO R2 2026-08-09)

§1 `metaso_mcp` 的 `api_key` 原来在 import 时读死 —— **第三次咬人**:
    admin 后台改 key 不重启不生效、两个测试的 `setenv` 全是死代码。
§2 `regenerating` 是三方都不管的死胡同态(孤儿 6122-6133 的根因)。
    纳入超时看门狗,恢复语义必须保持 charge-on-success。
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager

import psycopg2
import pytest

# ══════════════════════════════════════════════════════════════════════════
# §1 api_key 现取 + 官方接缝
# ══════════════════════════════════════════════════════════════════════════

def test_env_change_after_import_takes_effect(monkeypatch):
    """【必须命中 · 受害场景 1】admin 后台改 key(写回 os.environ)当场生效。

    `config/settings_manager` 把设置写回 `os.environ["METASO_API_KEY"]`。
    修复前:本模块 import 时读死 → **必须重启进程**才认。
    """
    import tools.search.metaso_mcp as mm

    monkeypatch.setitem(mm.METASO_MCP_CONFIG, "api_key", None)   # 无显式覆盖
    monkeypatch.setenv("METASO_API_KEY", "key-set-after-import")
    assert mm._metaso_api_key() == "key-set-after-import"

    monkeypatch.setenv("METASO_API_KEY", "key-changed-again")
    assert mm._metaso_api_key() == "key-changed-again", "改一次生效一次,不缓存"


def test_config_override_is_the_official_test_seam(monkeypatch):
    """【必须命中 · 受害场景 2/3】`setitem` 覆盖优先于 env —— 测试的官方接缝。"""
    import tools.search.metaso_mcp as mm

    monkeypatch.setenv("METASO_API_KEY", "env-key")
    monkeypatch.setitem(mm.METASO_MCP_CONFIG, "api_key", "seam-key")
    assert mm._metaso_api_key() == "seam-key"


def test_missing_key_is_empty_string_not_none(monkeypatch):
    """【必须不命中】两边都没有时返回空串 —— 调用点用 `if not api_key` 判,
    返回 None 也能过,但空串让类型稳定、不会把 "None" 拼进 header。"""
    import tools.search.metaso_mcp as mm

    monkeypatch.setitem(mm.METASO_MCP_CONFIG, "api_key", None)
    monkeypatch.delenv("METASO_API_KEY", raising=False)
    assert mm._metaso_api_key() == ""


def test_module_no_longer_reads_env_at_import_time():
    """【必须不命中 · 打在源码上】配置字典里不许再有 import 期的 env 读取。

    这就是"第三次咬人"的那一行。它回来了,上面三条会以别的方式绿,
    所以要单独钉住形状。
    """
    with open("tools/search/metaso_mcp.py", encoding="utf-8") as handle:
        raw = handle.read()
    code = "\n".join(l for l in raw.split("\n") if not l.strip().startswith("#"))
    head = code.split("def _metaso_api_key")[0]
    assert 'os.environ.get("METASO_API_KEY")' not in head, (
        "配置字典又在 import 期读 env 了"
    )
    assert code.count("api_key = _metaso_api_key()") == 3, (
        "三个读取点必须都走现取(少一个就是漏一条链)"
    )
    assert 'METASO_MCP_CONFIG["api_key"]' not in code.replace(
        'METASO_MCP_CONFIG.get("api_key")', ""
    ), "不许再有直接下标读取"


def test_all_three_historical_victims_no_longer_need_a_workaround(monkeypatch):
    """【必须命中 · 验证"三个历史受害场景不再需要各自绕"】

    三个场景现在**用同一种方式**就能满足,不再各自想办法:
      1. admin 改 env → 现取生效;
      2/3. 两个测试 → setenv **也**有效了(不再是死代码),setitem 是更稳的接缝。
    """
    import tools.search.metaso_mcp as mm

    monkeypatch.setitem(mm.METASO_MCP_CONFIG, "api_key", None)
    # 场景 1 与 场景 2/3 走的是同一条现取路径 —— 一个修法覆盖三处
    monkeypatch.setenv("METASO_API_KEY", "victim-scenario-key")
    assert mm._metaso_api_key() == "victim-scenario-key"
    # 而且**不需要** reload 模块(以前唯一的绕法就是 importlib.reload / 重启)
    assert "importlib" not in open(
        "tests/test_undelivered_not_billed_2026_08_08.py", encoding="utf-8"
    ).read(), "测试里不该再需要 reload 这种绕法"


# ══════════════════════════════════════════════════════════════════════════
# §2 regenerating 看门狗
# ══════════════════════════════════════════════════════════════════════════

SCHEMA = "w3_regen_watchdog_probe"

# 🔴 列集按**真 sink 需要**建,不是按判据方便建:server.py 智能补足那条 INSERT
#    要 keyword_id/original_keyword/user_choice/... 全套 —— 少一列,"把真语句拉过来
#    执行"这条锁就退化成只能看源码。
# 🔴 regenerate_started_at 故意**不写进这段 DDL** —— 它由 `db/diagnosis_db` 的
#    建表兜底注册表自己加(见 test_migration_registers_the_column),那条锁才有判别力。
TOPICS_DDL = """
CREATE TABLE IF NOT EXISTS topics (
    id SERIAL PRIMARY KEY,
    keyword_id INTEGER,
    quote_id INTEGER,
    original_keyword TEXT,
    status TEXT,
    article_id INTEGER,
    is_optimize BOOLEAN DEFAULT FALSE,
    optimized_title TEXT,
    user_choice VARCHAR(32),
    user_choice_source VARCHAR(32),
    style_code VARCHAR(64),
    article_style TEXT,
    regenerate_count INTEGER DEFAULT 0,
    fail_reason TEXT,
    writing_started_at TIMESTAMP,
    -- [WO_225-c1 §8.3] 媒体桶(迁移 061)。与迁移 / 自愈 DDL / 其他夹具同名同型。
    --   本判据自带一份 topics DDL,所以它是全仓第 4 套定义;不补这一列,
    --   拉 server.py 原文去执行的那条判据会 UndefinedColumn。
    media_bucket VARCHAR(40),
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS quotes (
    id SERIAL PRIMARY KEY,
    writing_status TEXT
);
"""

# 🔴 计费表建在 **public**(不在探针 schema 里)——「看门狗不产生扣费」那条判据
#    要数的是真表行数,而看门狗若真去写它们,写的就是 public 那张。
#    只列本判据用得到的列;第一版我没建它们,直接报 relation does not exist。
BILLING_DDL = """
CREATE TABLE IF NOT EXISTS public.llm_call_log (
    id SERIAL PRIMARY KEY, caller TEXT NOT NULL, platform TEXT NOT NULL,
    estimated_cost NUMERIC(10,6) DEFAULT 0, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS public.point_transactions (
    id BIGSERIAL PRIMARY KEY, user_id INTEGER, feature_code TEXT,
    amount BIGINT, created_at TIMESTAMP DEFAULT NOW()
);
"""


# 🔴 连接是**池化**的(`db/connection.ThreadedConnectionPool`),`putconn` 只 rollback、
#    不复位 `search_path`;而 `SET search_path` 一旦随业务 commit 落定就会跟着连接回池,
#    被后面任何一个测试拿到 → `no schema has been selected`。
#    (实测:本文件与 `test_undelivered_r2_*` 同批跑时,后者 17 个 setup 全炸。)
#    所以每一处用完都必须显式还原,不是"反正测试跑完就没了"。
@contextmanager
def _probe_cursor(schema: str = SCHEMA):
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SET search_path TO {schema}")
        try:
            yield cur
        finally:
            try:
                cur.execute("SET search_path TO public")
            except Exception:
                pass


class _ResetOnCloseConn:
    """看门狗自己 `conn.close()` —— 归还前把 search_path 还原掉。"""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        # 🔴 只转发读、不转发写 = 写全落在壳上:`init_db()` 的 `conn.autocommit = True`
        #    就这么被吞掉,真连接仍是事务模式 → 第一条失败 DDL 直接把整条自愈链带成
        #    `InFailedSqlTransaction`。壳类必须两头都转发。
        setattr(self._conn, name, value)

    def close(self):
        try:
            self._conn.cursor().execute("SET search_path TO public")
            self._conn.commit()
        except Exception:
            pass
        self._conn.close()


def _registered_topics_columns() -> dict[str, str]:
    """把 `db/diagnosis_db` 建表兜底里登记的 topics 列**原样读出来**。

    不手抄一份 —— 手抄的话删掉源码那行,判据照样绿(第二套的老毛病)。
    """
    with open("db/diagnosis_db.py", encoding="utf-8") as handle:
        src = handle.read()
    pairs = re.findall(
        r'_safe_add_column\(\s*cursor\s*,\s*"topics"\s*,\s*"(\w+)"\s*,\s*"([^"]+)"\s*\)',
        src,
    )
    # 只认字面量那种写法(源码里还有一个 for 循环批量登记 4 列,与本判据无关)
    assert len(pairs) >= 15, f"没解析到 topics 列登记表(只有 {len(pairs)} 条),正则该修了"
    return dict(pairs)


def _apply_topics_column_migrations(cur) -> None:
    for col, col_type in _registered_topics_columns().items():
        # 探针 schema 里没有被外键指向的表,剥掉 REFERENCES 只留列本身(FK 不是本判据的对象)
        col_type = re.sub(r"\s+REFERENCES\s+\S+", "", col_type)
        cur.execute(f"ALTER TABLE topics ADD COLUMN IF NOT EXISTS {col} {col_type}")


BOOT_SCHEMA = f"{SCHEMA}_boot"
_diag_module = None


def _import_diagnosis_db_isolated():
    """`db/diagnosis_db.py` 末尾是**模块级** `init_db()` —— 第一次 import 它就会跑
    一整条建表/自愈 DDL 链。共享 throwaway 库的 `public` 里有别的套件留下的**残缺表**
    (`diagnosis_records` / `brands` 都缺列),自愈链撞上就抛 `UndefinedColumn`,
    连带本套件全部 setup 失败。

    🔴 这不是"绕过判据":本套件要的只是 `diag.get_connection`;`init_db` 去自愈
    `public` 与本包判据无关。所以把这一次 import 期的自举**关进一个独立 schema**,
    让它在那里建一套完整的表,`public` 一个字不动。

    (顺带说明为什么以前"看起来没事":改判据前夹具泄漏 `search_path`,
    init_db 恰好也跑在探针 schema 里 —— **绿是靠那个泄漏撑着的**。修掉泄漏才暴露。)
    """
    global _diag_module
    if _diag_module is not None:
        return _diag_module

    import db.connection as dbconn

    with dbconn.get_db() as conn:
        conn.cursor().execute(f"CREATE SCHEMA IF NOT EXISTS {BOOT_SCHEMA}")

    real = dbconn.get_connection

    def _boot_scoped():
        conn = real()
        # 顺序不能反:`SET` 会开一个事务,而 psycopg2 的
        # `set_session cannot be used inside a transaction` —— 之后 init_db 再置
        # autocommit 就会抛。先置 autocommit,`SET` 再作为独立语句落到会话上。
        conn.autocommit = True
        conn.cursor().execute(f"SET search_path TO {BOOT_SCHEMA}")
        return _ResetOnCloseConn(conn)

    dbconn.get_connection = _boot_scoped
    try:
        import db.diagnosis_db as diag
    finally:
        dbconn.get_connection = real

    _diag_module = diag
    return diag


@pytest.fixture
def topics_db(monkeypatch):
    """把看门狗指到一个干净 schema 上跑 —— 真 SQL、真表、真事务。"""
    from db.connection import get_db

    diag = _import_diagnosis_db_isolated()

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(BILLING_DDL)
    with _probe_cursor() as cur:
        cur.execute(TOPICS_DDL)
        _apply_topics_column_migrations(cur)

    real_get_connection = diag.get_connection

    def _scoped():
        conn = real_get_connection()
        conn.cursor().execute(f"SET search_path TO {SCHEMA}")
        return _ResetOnCloseConn(conn)

    monkeypatch.setattr(diag, "get_connection", _scoped)
    yield
    with get_db() as conn:
        conn.cursor().execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


# 这两列的值是 SQL 表达式(NOW() - INTERVAL ...),按字面拼进语句,不走参数
_RAW_COLS = ("created_at", "regenerate_started_at")


def _seed_topic(**kw):
    cols = {"quote_id": 386, "status": "regenerating", "article_id": None,
            "is_optimize": False, "optimized_title": "某个已经生成好的标题",
            "regenerate_count": 9, "created_at": "NOW() - INTERVAL '30 days'",
            "regenerate_started_at": "NULL"}
    cols.update(kw)
    raw = {k: cols.pop(k) for k in _RAW_COLS if k in cols}
    keys = ", ".join([*cols, *raw])
    ph = ", ".join(["%s"] * len(cols) + list(raw.values()))
    with _probe_cursor() as cur:
        cur.execute(f"INSERT INTO topics ({keys}) VALUES ({ph}) RETURNING id",
                    tuple(cols.values()))
        return int(cur.fetchone()["id"])


def _row(tid):
    with _probe_cursor() as cur:
        cur.execute(
            "SELECT status, fail_reason, article_id, regenerate_started_at "
            "FROM topics WHERE id=%s", (tid,))
        return dict(cur.fetchone())


def _run_watchdog():
    from api.scheduler import _topic_writing_watchdog

    _topic_writing_watchdog()


def test_stuck_24h_regenerating_is_picked_up(topics_db):
    """【必须命中】卡死超过 24h 的 regenerating 必须被捡起 → `failed`。

    生产实证形态:quote 386 的 6122-6133 —— is_optimize=false、
    optimized_title 是真标题(不是占位符)、article_id 空、
    `regenerate_started_at` 为 NULL(上线前入的态)→ 回落 created_at。
    这几条正是让既有恢复任务**捡不到**它们的原因。
    """
    tid = _seed_topic()
    _run_watchdog()
    row = _row(tid)
    assert row["status"] == "failed", "24h+ 的死胡同行必须被回收"
    assert row["fail_reason"], "必须写清原因,不留空让人猜"


def test_pickup_does_not_charge_anything(topics_db):
    """【必须命中 · 打在钱上】捡起动作**不产生任何扣费**。

    看门狗只改 `topics.status/fail_reason` —— 不碰钱包、不碰 freeze、不碰
    `llm_call_log`。判据:跑完之后 `point_transactions` 与 `llm_call_log`
    行数逐字不变(而不是"我读了代码觉得它不扣费")。
    """
    from db.connection import get_db

    def _counts():
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM public.llm_call_log")
            a = int(cur.fetchone()["n"])
            cur.execute("SELECT count(*) AS n FROM public.point_transactions")
            b = int(cur.fetchone()["n"])
            return a, b

    _seed_topic()
    before = _counts()
    _run_watchdog()
    assert _counts() == before, "看门狗不许产生任何计费/扣费行"


def test_recovered_state_is_failed_not_draft(topics_db):
    """【必须不命中 · charge-on-success 铁律】不许推回 draft/pending。

    看门狗自己的注释写死了这条:回 draft 用户重选会**二次扣费**。
    `failed` 是可恢复态(派发输入集含 failed、全量重置也不拒绝),
    客户能自己走正常路重来,而那一次才按 charge-on-success 正常计费。
    """
    tid = _seed_topic()
    _run_watchdog()
    assert _row(tid)["status"] not in ("draft", "pending"), "推回 draft = 双扣"

    src = open("api/scheduler.py", encoding="utf-8").read()
    body = src.split("def _topic_writing_watchdog")[1].split("\ndef ")[0]
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    assert "SET status='failed'" in code
    assert "status='draft'" not in code and "status='pending'" not in code


@pytest.mark.parametrize("kw, why", [
    ({"created_at": "NOW() - INTERVAL '2 hours'"}, "才 2 小时 —— 可能正在跑"),
    ({"article_id": 4242}, "已经出稿了 —— 护栏"),
    ({"status": "completed"}, "不是 regenerating"),
    ({"status": "writing", "created_at": "NOW() - INTERVAL '30 minutes'"},
     "writing 且未到它自己的 90min 阈值 —— 两支都不该碰"),
])
def test_watchdog_does_not_touch_these(topics_db, kw, why):
    """【必须不命中】反向对照集:一条都不许被误伤。"""
    tid = _seed_topic(**kw)
    before = _row(tid)["status"]
    _run_watchdog()
    assert _row(tid)["status"] == before, why


def test_watchdog_is_idempotent(topics_db):
    """【必须不命中】跑两遍与跑一遍结果相同(定时任务每 5 分钟一次)。"""
    tid = _seed_topic()
    _run_watchdog()
    first = _row(tid)
    _run_watchdog()
    assert _row(tid) == first


def test_writing_branch_is_untouched(topics_db):
    """【必须命中 · 反向对照】原来那支 writing→write_timeout 一个字没改。

    没有这条,"把两支合并成一支"这种实现会让 writing 也被推成 failed ——
    那会绕过 write_timeout 的防双扣设计。
    """
    tid = _seed_topic(status="writing",
                      created_at="NOW() - INTERVAL '5 hours'")
    _run_watchdog()
    assert _row(tid)["status"] == "write_timeout", "writing 仍走 write_timeout,不是 failed"


# ──────────────────────────────────────────────────────────────────────────
# §2.1 [返工 2026-08-09] 判据锚 regenerate_started_at,不是 created_at
#
# 误杀形态:老选题(created_at 很早)今天刚点"重新生成" —— 用 created_at 判,
# 下一个 5 分钟 tick 就被推 failed。用户看到的是"点了重新生成,几分钟后变失败"。
# ──────────────────────────────────────────────────────────────────────────

def test_old_topic_freshly_regenerating_is_not_killed(topics_db):
    """【必须不命中 · 返工核心】30 天前建的老选题,刚刚进入 regenerating → 不许杀。

    这是真实形态:选题在 quote 里躺了一个月,代理今天点了"重新生成"。
    改锚之前这一行 100% 被误杀(created_at 早就过 24h 了)。
    """
    tid = _seed_topic(created_at="NOW() - INTERVAL '30 days'",
                      regenerate_started_at="NOW()")
    _run_watchdog()
    assert _row(tid)["status"] == "regenerating", "刚开始重生成的老选题被误杀了"


def test_same_created_at_different_regen_start_diverge(topics_db):
    """【判别力对照】created_at 完全相同、只有 regenerate_started_at 不同 →
    一个被捡、一个不被捡。

    没有这条,上一条可能只是"某个别的条件恰好放过它"。
    """
    fresh = _seed_topic(created_at="NOW() - INTERVAL '40 days'",
                        regenerate_started_at="NOW() - INTERVAL '10 minutes'")
    stale = _seed_topic(created_at="NOW() - INTERVAL '40 days'",
                        regenerate_started_at="NOW() - INTERVAL '25 hours'")
    _run_watchdog()
    assert _row(fresh)["status"] == "regenerating"
    assert _row(stale)["status"] == "failed"


def test_legacy_null_falls_back_to_created_at(topics_db):
    """【必须命中】上线前就在途的存量行(列为 NULL)仍按 created_at 判。

    去掉 COALESCE 回落,6122-6133 那批(卡了 20+ 天、列必为 NULL)就永远捡不起来,
    根治退化成"只对以后的行有效"。
    """
    tid = _seed_topic(created_at="NOW() - INTERVAL '25 days'",
                      regenerate_started_at="NULL")
    _run_watchdog()
    assert _row(tid)["status"] == "failed"


def test_regenerate_topic_stamps_the_column_end_to_end(topics_db):
    """【必须命中 · 打在接线上】走**真的** `regenerate_topic()`,不是自己写 SQL。

    列加了、看门狗改锚了,但入态点忘了写 —— 那就是第 N 次"接线没接":
    每一行的 `regenerate_started_at` 恒 NULL、恒回落 created_at、误杀原样。
    所以这条端到端跑:老选题 → 真函数置态 → 列必须有值 → 看门狗必须放过。
    """
    from db.diagnosis_db import regenerate_topic

    tid = _seed_topic(status="pending", created_at="NOW() - INTERVAL '30 days'")
    assert regenerate_topic(tid) is True

    row = _row(tid)
    assert row["status"] == "regenerating"
    assert row["regenerate_started_at"] is not None, "入态点没盖时间戳"

    _run_watchdog()
    assert _row(tid)["status"] == "regenerating", "真路径进来的新态被误杀"


def _sole_match(pattern: str, text: str, what: str) -> str:
    hits = re.findall(pattern, text, re.S)
    assert len(hits) == 1, f"{what} 在 server.py 命中 {len(hits)} 次(应为 1),正则该修了"
    return hits[0]


def _server_src() -> str:
    with open("server.py", encoding="utf-8") as handle:
        return handle.read()


def test_optimize_insert_sink_stamps_the_column(topics_db):
    """【必须命中 · 第 2 个 sink】智能补足**建行**那条 INSERT 也要盖戳。

    把 server.py 里那条语句**原样拉出来执行**(不是抄一份),执行完查真列。
    """
    sql = _sole_match(
        r"INSERT INTO topics \([^\"]*?'regenerating', TRUE[^\"]*?RETURNING id",
        _server_src(), "智能补足建行 INSERT")
    with _probe_cursor() as cur:
        # [WO_225-c1 §8.3] 第 7 个参数是 `media_bucket`(迁移 061 新列)。
        #   本判据**原样拉 server.py 的语句去真执行**,所以语句多一个占位符,
        #   参数元组就必须跟着多一个 —— 这正是它作为接线判据的价值:
        #   抄一份 SQL 的判据不会红,拉原文执行的会。传 None = 老行为(桶留空)。
        cur.execute(sql, (11, 386, "关键词", "guide", "guide", "指南体", None))
        tid = int(cur.fetchone()["id"])
    assert _row(tid)["regenerate_started_at"] is not None


def test_optimize_retry_sink_stamps_the_column(topics_db):
    """【必须命中 · 第 3 个 sink】智能补足**重试**那条 UPDATE 也要盖戳。

    这个 sink 与 `regenerate_topic` 同类:改的是**已存在的老行**,
    created_at 可以很早 —— 不盖戳就是又一条误杀路径。
    """
    sql = _sole_match(
        r"UPDATE topics SET status='regenerating'.*?status='failed'\s*\n",
        _server_src(), "智能补足重试 UPDATE")
    tid = _seed_topic(status="failed", is_optimize=True,
                      created_at="NOW() - INTERVAL '30 days'")
    with _probe_cursor() as cur:
        cur.execute(sql, (tid,))
        assert cur.rowcount == 1

    assert _row(tid)["regenerate_started_at"] is not None
    _run_watchdog()
    assert _row(tid)["status"] == "regenerating", "重试刚置的态被误杀"


def test_every_regenerating_sink_stamps_the_column():
    """【必须不命中 · 枚举全部 sink】不许有第 4 个入态点绕过盖戳。

    今天是 3 个:`regenerate_topic` / 智能补足建行 / 智能补足重试。
    以后谁再加一个只写 status 的,这条当场红。
    """
    sources = {"server.py": _server_src()}
    with open("db/diagnosis_db.py", encoding="utf-8") as handle:
        sources["db/diagnosis_db.py"] = handle.read()

    sinks = []
    for path, src in sources.items():
        code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
        # 写入型:SET status = 'regenerating'  或  INSERT ... VALUES 里的 'regenerating'
        for m in re.finditer(r"SET\s+status\s*=\s*'regenerating'|SET\s*\n\s*status\s*=\s*'regenerating'",
                             code):
            sinks.append((path, code[m.start():m.start() + 400]))
        # `[^"]` 把匹配夹在**同一段三引号 SQL** 内 —— 否则非贪婪会从上一条
        # INSERT 一路走到下一条里的 'regenerating',凭空多出两个"sink"。
        for m in re.finditer(r"INSERT INTO topics \([^\"]*?'regenerating',", code, re.S):
            sinks.append((path, code[m.start():m.end() + 200]))

    assert len(sinks) == 3, f"入态点数量变了({len(sinks)} 个)—— 新增的那个盖戳了吗?"
    for path, chunk in sinks:
        assert "regenerate_started_at" in chunk, f"{path} 有一个入态点没盖 regenerate_started_at"


def test_migration_registers_the_column():
    """【必须命中 · 打在迁移上】建表兜底登记表里必须有这一列。

    只改代码不加列 = 生产 `column ... does not exist`,看门狗整支静默失败
    (它包在 try/except 里)。判据不看源码字符串,而是**把登记表应用到一张
    没有这列的表上**,再查 information_schema —— 删掉那行注册,这条就红。
    """
    from db.connection import get_db

    probe = f"{SCHEMA}_migrate"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"DROP SCHEMA IF EXISTS {probe} CASCADE")
        cur.execute(f"CREATE SCHEMA {probe}")
    with _probe_cursor(probe) as cur:
        cur.execute("CREATE TABLE topics (id SERIAL PRIMARY KEY)")
        cur.execute(
            "SELECT count(*) AS n FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='topics' "
            "AND column_name='regenerate_started_at'", (probe,))
        assert int(cur.fetchone()["n"]) == 0, "夹具自己就带了这列,这条锁没判别力"

        _apply_topics_column_migrations(cur)

        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='topics' "
            "AND column_name='regenerate_started_at'", (probe,))
        row = cur.fetchone()
        assert row, "建表兜底没登记 regenerate_started_at"
        assert row["data_type"].startswith("timestamp"), row["data_type"]
        cur.execute(f"DROP SCHEMA IF EXISTS {probe} CASCADE")
