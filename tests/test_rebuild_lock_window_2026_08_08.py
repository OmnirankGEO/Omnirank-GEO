"""重建脚本 `--apply` 自锁死 · 回归锁 · 返工单 §5.A ②(2026-08-08)。

🔴 **上一轮 dry-run 全绿,`--apply` 22 分钟零进展。**
`--apply` 与 dry-run 走**不同代码路径**:dry-run 不写库、不持锁,所以永远碰不到
死锁。所以本文件的每一条锁都跑 `apply=True`,并且都跑在**真 PostgreSQL** 上。

生产实测的死锁形态(`pg_blocking_pids`,两个 pid 都是它自己):

    488727 idle in transaction  ← 主事务已 UPDATE diagnosis_records(ROW EXCLUSIVE)
    488726 active / Lock        ← CREATE INDEX ... ON diagnosis_records,blocked_by {488727}

DDL 来自 `db/diagnosis_db.py:8467` 的**模块级** `init_db()` —— 报告装配链深处第一次
import 它就等于开一条新连接跑自愈 DDL,而那时主事务正持着锁。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
import tokenize
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")

BRAND_ID = 88001
DIAG_ID = 88001
OWNER_UID = 112

# 污染格:命中的是被拆出来的括号地名「深圳」,不是品牌本身 → 修复后必须掉下来
ANSWER_POLLUTED = "深圳夜宵推荐：大嘴龙虾、小龙坎。"
# 清单外、本地可重放且判定稳定的格
ANSWER_STABLE = "本地夜宵可以试试大嘴店和小龙坎火锅。"

SELF_HEAL_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_diag_records_run_token "
    "ON diagnosis_records (run_token)"
)

pytestmark = pytest.mark.skipif(not PG_URL, reason="real PostgreSQL URL is required")


# ══════════════════════════════════════════════════════════════════════
# 夹具:真库(真 schema · 走生产 init_db)+ 一份带污染格的诊断
# ══════════════════════════════════════════════════════════════════════

def _cell(verdict, matched, method, answer):
    return {
        "brand_verdict": verdict,
        "brand_detected": verdict == "YES",
        "matched_text": matched,
        "detection_reason": "fixture",
        "detection_method": method,
        "full_response": answer,
        "mentioned_brands": ["大嘴龙虾"],
        "engine": "dashscope",
    }


def _raw_payload() -> dict:
    detail = []
    for index in range(3):
        detail.append({
            "question": f"深圳小龙虾哪家好？{index}",
            "results": {"dashscope": _cell("YES", "深圳", "trusted_exact", ANSWER_POLLUTED)},
        })
    for index in range(2):
        detail.append({
            "question": f"深圳龙虾馆推荐{index}",
            "results": {"dashscope": _cell("UNKNOWN", None, "deterministic_local",
                                           ANSWER_STABLE)},
        })
    return {
        "data": {
            "ai_visibility": {
                "test_questions": [d["question"] for d in detail],
                "engines_tested": ["dashscope"],
                "detail_table": detail,
                "total_tests": len(detail),
            },
            "web_search": {"brand_direct_count": 0},
        },
        "keywords": "深圳小龙虾",
        "score_data": {},
    }


@pytest.fixture(scope="module")
def realdb():
    """一个**全新真库**:schema 由生产 `init_db()` 亲自建,不手搓。"""
    import psycopg2
    from psycopg2 import sql

    database_name = f"rebuildlock_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    admin.close()

    parsed = urlsplit(PG_URL)
    target_url = urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
    )
    os.environ["DATABASE_URL"] = target_url

    from db import connection as connection_db

    previous_url = connection_db.DATABASE_URL
    connection_db.DATABASE_URL = target_url
    connection_db._pool = None

    # 🔴 用生产 init_db() 建 schema —— 顺带证明"自愈 DDL 确实存在于建表链里"。
    from db.diagnosis_db import init_db

    init_db()

    conn = connection_db.get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS client_profiles (
            id BIGSERIAL PRIMARY KEY, brand_id INTEGER,
            is_deleted INTEGER DEFAULT 0)
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS brand_aliases (
            id BIGSERIAL PRIMARY KEY, brand_id INTEGER, canonical_name TEXT,
            alias TEXT, source TEXT)
    """)
    conn.commit()
    conn.close()

    yield target_url

    connection_db._pool = None
    connection_db.DATABASE_URL = previous_url
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database_name))
        )
    admin.close()


@pytest.fixture()
def seeded(realdb):
    """每个用例重新种一份干净的诊断(用例之间不互相污染)。"""
    from db.connection import get_connection

    conn = get_connection()
    cur = conn.cursor()
    for statement, params in (
        ("DELETE FROM diagnosis_records WHERE id = %s", (DIAG_ID,)),
        ("DELETE FROM brands WHERE id = %s", (BRAND_ID,)),
        ("DELETE FROM client_profiles WHERE brand_id = %s", (BRAND_ID,)),
        ("DELETE FROM brand_aliases WHERE brand_id = %s", (BRAND_ID,)),
    ):
        cur.execute(statement, params)
    cur.execute(
        "INSERT INTO brands (id, name, industry, owner_user_id, latest_score,"
        " latest_diagnosis_id) VALUES (%s,%s,%s,%s,%s,%s)",
        (BRAND_ID, "阿强小龙虾（深圳）", "餐饮", OWNER_UID, 58, DIAG_ID),
    )
    cur.execute("INSERT INTO client_profiles (brand_id) VALUES (%s)", (BRAND_ID,))
    cur.execute(
        "INSERT INTO diagnosis_records (id, session_id, brand_name, industry, brand_id,"
        " keywords, total_score, level, raw_data_json, result_visibility)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'published')",
        (DIAG_ID, f"fixture-{uuid.uuid4().hex[:8]}", "阿强小龙虾（深圳）", "餐饮",
         BRAND_ID, "深圳小龙虾", 58, "成长型",
         json.dumps(_raw_payload(), ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return DIAG_ID


def _answers_digest() -> str:
    """所有 `full_response` 的指纹 —— 用来证明原文一字节没改。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT raw_data_json FROM diagnosis_records WHERE id = %s", (DIAG_ID,))
        raw = cur.fetchone()["raw_data_json"]
    finally:
        conn.close()
    if isinstance(raw, str):
        raw = json.loads(raw)
    answers = [
        cell.get("full_response")
        for row in raw["data"]["ai_visibility"]["detail_table"]
        for cell in row["results"].values()
    ]
    return hashlib.sha256(json.dumps(answers, ensure_ascii=False).encode("utf-8")).hexdigest()


def _cells():
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT raw_data_json, total_score, level FROM diagnosis_records"
                    " WHERE id = %s", (DIAG_ID,))
        row = dict(cur.fetchone())
    finally:
        conn.close()
    raw = row["raw_data_json"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    cells = [
        cell
        for item in raw["data"]["ai_visibility"]["detail_table"]
        for cell in item["results"].values()
    ]
    return row, cells


# ══════════════════════════════════════════════════════════════════════
# 判别力前提:这个坑在这台 PG 上**是真的**
# ══════════════════════════════════════════════════════════════════════

def test_the_self_heal_ddl_really_blocks_on_an_open_update(realdb, seeded):
    """🔴【正向对照 · 证明危险是真的】主事务 UPDATE 未提交时,自愈 DDL 必然被挡住。

    没有这条,下面"修好了"的断言就没有参照物 —— 万一这台 PG 根本不冲突,
    那些绿全是恒真。这里用 `statement_timeout` 把"死等"变成"可断言的失败",
    🔴 只用在**这条对照**上;修复本身一处超时/重试/NOWAIT 都没用(§4.2 明令)。
    """
    import psycopg2

    holder = psycopg2.connect(realdb)
    blocked = psycopg2.connect(realdb)
    try:
        with holder.cursor() as cur:
            cur.execute("UPDATE diagnosis_records SET total_score = total_score"
                        " WHERE id = %s", (DIAG_ID,))
        # 不 commit:锁一直握着
        blocked.autocommit = True
        with blocked.cursor() as cur:
            cur.execute("SET statement_timeout = '2s'")
            with pytest.raises(psycopg2.errors.QueryCanceled):
                cur.execute(SELF_HEAL_DDL)
    finally:
        holder.rollback()
        holder.close()
        blocked.close()

    # 反向对照:主事务提交后,同一条 DDL 秒过 —— 证明上面挡住的原因就是那把锁,
    # 不是 DDL 自己慢或者语法有问题。
    freed = psycopg2.connect(realdb)
    freed.autocommit = True
    try:
        with freed.cursor() as cur:
            cur.execute("SET statement_timeout = '5s'")
            cur.execute(SELF_HEAL_DDL)      # 不许抛
    finally:
        freed.close()


# ══════════════════════════════════════════════════════════════════════
# ②-1 死锁回归锁:写窗口内**零**新连接
# ══════════════════════════════════════════════════════════════════════

class _ConnProbe:
    """数连接池取连接的次数,并区分"第一条写语句之前/之后"。

    🔴 探针打在**连接池对象**上。打在 `db.connection.get_connection` 这个名字上
    是无效判据:装配链里大量模块是 `from db.connection import get_connection`
    (import 期就绑定了函数对象),patch 模块属性对它们一个都拦不到。
    """

    def __init__(self):
        self.before = 0
        self.after = 0
        self.wrote = False
        self.stacks: list[str] = []

    def install(self, monkeypatch):
        import traceback

        from db import connection as connection_db

        pool = connection_db._get_pool()
        original = pool.getconn

        def traced(*args, **kwargs):
            if self.wrote:
                self.after += 1
                self.stacks.append("".join(traceback.format_stack()[-8:-1]))
            else:
                self.before += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(pool, "getconn", traced)

    def watch(self, conn):
        """包住主连接的 cursor,记下第一条写 diagnosis_records 的语句。"""
        real_cursor = conn.cursor

        def cursor(*args, **kwargs):
            cur = real_cursor(*args, **kwargs)
            real_execute = cur.execute

            def execute(sql, params=None):
                text = str(sql).strip().upper()
                if text.startswith("UPDATE") and "DIAGNOSIS_RECORDS" in text:
                    self.wrote = True
                return real_execute(sql, params) if params is not None else real_execute(sql)

            cur.execute = execute
            return cur

        conn.cursor = cursor
        return conn


def test_no_new_connection_is_opened_inside_the_write_window(realdb, seeded, monkeypatch):
    """🔴🔴【②-1 本单核心】`apply=True` 全程,第一条写语句之后**零**新连接。

    这就是死锁的充要条件:写窗口内不开新连接 → 不可能有第二个会话去等主事务的锁。

    反向对照写在同一条断言里:`probe.before` 必须 > 0 —— 装配链本来就会开
    近十条连接,只是全被挪到了写之前。若 before 也是 0,说明探针根本没看见
    连接(判别力=0),这条绿毫无意义。
    """
    import scripts.rebuild_parenthetical_false_mentions_2026_08_06 as rebuild
    from db.connection import get_connection

    rebuild._preload_report_chain()

    probe = _ConnProbe()
    probe.install(monkeypatch)

    conn = probe.watch(get_connection())
    started = time.monotonic()
    try:
        result = rebuild.rebuild_one(conn, DIAG_ID, apply=True)
        conn.commit()
    finally:
        conn.close()
    elapsed = time.monotonic() - started

    assert result["status"] == "rebuilt"
    assert probe.wrote, "一条写 diagnosis_records 的语句都没发 → 判据没打到写窗口上"
    assert probe.before > 0, (
        "写之前一条连接都没开 → 探针没有判别力(装配链实测会开近十条)"
    )
    assert probe.after == 0, (
        f"写窗口内又开了 {probe.after} 条连接 —— 这正是自锁死的形态:\n"
        + "\n".join(probe.stacks)
    )
    assert elapsed < 120, f"{elapsed:.1f}s 未完成 —— 疑似又卡在锁上"


def test_the_probe_would_catch_the_old_order(realdb, seeded, monkeypatch):
    """🔴【探针的反向对照】把旧顺序演一遍,探针必须**数得出来**。

    先 UPDATE 再去开连接 —— 也就是修复之前的形态。若这条数出 0,
    说明上面那条 `after == 0` 是探针瞎了,不是代码好了。
    """
    from db.connection import get_connection

    probe = _ConnProbe()
    probe.install(monkeypatch)

    conn = probe.watch(get_connection())
    try:
        cur = conn.cursor()
        cur.execute("UPDATE diagnosis_records SET total_score = total_score WHERE id = %s",
                    (DIAG_ID,))
        # 旧顺序:持锁之后才去装配 → 装配自己开连接
        from services.public_whitelabel import get_public_whitelabel_data

        get_public_whitelabel_data(brand_owner_user_id=OWNER_UID, brand_id=BRAND_ID)
    finally:
        conn.rollback()
        conn.close()

    assert probe.after > 0, "探针数不出'持锁后开的连接' → 它没有判别力"


def test_forbid_guard_denies_even_import_bound_get_connection(realdb, seeded):
    """🔴 守卫必须拦得住 `from db.connection import get_connection` 那种写法。

    这是判据形态的核心:拦模块属性只能拦住 `db.connection.get_connection(...)`
    这一种调用方式,而装配链里几乎全是 import 期绑定的那种。
    """
    import scripts.rebuild_parenthetical_false_mentions_2026_08_06 as rebuild
    # 与装配链同款:import 期就把函数对象绑到本地名字上
    from db.connection import get_connection as bound_at_import

    # 窗口外:正常
    outside = bound_at_import()
    outside.close()

    with pytest.raises(rebuild.ConnectionInsideLockWindow):
        with rebuild._forbid_new_connections("单测"):
            bound_at_import()

    # 窗口退出后必须复原 —— 否则守卫会把整个进程的取连接能力永久掐断
    restored = bound_at_import()
    restored.close()


def test_preload_covers_the_module_that_actually_deadlocks():
    """🔴 预加载清单必须**点名** `db.diagnosis_db` —— 它才是模块级 init_db() 那个。

    反向对照:同时断言它真的是"模块级调用"(源码面 · 顶层 `init_db()`),
    否则清单里列着它也只是摆设。
    """
    import scripts.rebuild_parenthetical_false_mentions_2026_08_06 as rebuild

    source = (ROOT / "scripts"
              / "rebuild_parenthetical_false_mentions_2026_08_06.py").read_text(encoding="utf-8")
    preload_src = source.split("def _preload_report_chain")[1].split("\ndef ")[0]
    assert '"db.diagnosis_db"' in preload_src

    diagnosis_db_src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    top_level_calls = [
        line for line in diagnosis_db_src.splitlines()
        if line.strip() == "init_db()" and not line.startswith((" ", "\t"))
    ]
    assert top_level_calls, (
        "db/diagnosis_db.py 不再是模块级 init_db() —— 病因变了,本包的前提要重审"
    )

    rebuild._preload_report_chain()
    assert "db.diagnosis_db" in sys.modules


# ══════════════════════════════════════════════════════════════════════
# ②-2 `--apply` 真库端到端 · 必须真 commit
# ══════════════════════════════════════════════════════════════════════

def test_apply_end_to_end_really_commits_and_never_touches_full_response(
    realdb, seeded, monkeypatch, capsys
):
    """🔴🔴【②-2】走 `main()` 的 `--apply` 全路径,真库、真提交、真改值。

    dry-run 全绿证明不了 apply 能跑 —— 这正是本次翻车点,所以这条必须存在。
    """
    import scripts.rebuild_parenthetical_false_mentions_2026_08_06 as rebuild

    before_row, before_cells = _cells()
    before_answers = _answers_digest()
    assert sum(1 for c in before_cells if c["brand_verdict"] == "YES") == 3
    assert before_row["total_score"] == 58

    monkeypatch.setattr(sys, "argv", ["rebuild", "--ids", str(DIAG_ID), "--apply"])
    started = time.monotonic()
    exit_code = rebuild.main()
    elapsed = time.monotonic() - started

    assert exit_code == 0
    assert elapsed < 120, f"{elapsed:.1f}s 未完成 —— 疑似又卡在锁上"
    assert "[APPLIED] committed=1" in capsys.readouterr().out

    after_row, after_cells = _cells()
    # 真落库了(不是"跑完了但没写")
    assert sum(1 for c in after_cells if c["brand_verdict"] == "YES") == 0
    assert all(c["matched_text"] is None for c in after_cells), (
        "matched_text 还留着「深圳」= 真凶继续挂在报告上"
    )
    assert after_row["total_score"] != before_row["total_score"]

    # 🔴 原文一字节没改
    assert _answers_digest() == before_answers, "full_response 被改动了 —— 红线⑤"

    # 🔴 零模型调用的旁证:全部落库 detection_method 都是本地确定性层产出的
    from scripts.rebuild_parenthetical_false_mentions_2026_08_06 import (
        _LOCAL_DECISION_METHODS,
    )
    rewritten = [c for c in after_cells if c["detection_reason"] != "fixture"]
    assert rewritten, "一格都没重写 → 断言失去判别力"
    assert all(c["detection_method"] in _LOCAL_DECISION_METHODS for c in rewritten)


def test_apply_is_idempotent_second_run_changes_nothing(realdb, seeded, monkeypatch, capsys):
    """重跑一次不应再改任何格(污染格已经不在清单里了)。"""
    import scripts.rebuild_parenthetical_false_mentions_2026_08_06 as rebuild

    monkeypatch.setattr(sys, "argv", ["rebuild", "--ids", str(DIAG_ID), "--apply"])
    rebuild.main()
    capsys.readouterr()
    first_digest = _answers_digest()
    _, first_cells = _cells()

    rebuild.main()
    output = capsys.readouterr().out
    assert "rewritten=0" in output, "第二遍还在改格 —— 不幂等"
    assert _answers_digest() == first_digest
    _, second_cells = _cells()
    assert [c["brand_verdict"] for c in second_cells] == \
           [c["brand_verdict"] for c in first_cells]


def test_dry_run_writes_nothing(realdb, seeded, monkeypatch, capsys):
    """dry-run 一个字都不写 —— 逐份提交改造之后这条尤其要钉住。"""
    import scripts.rebuild_parenthetical_false_mentions_2026_08_06 as rebuild

    before_row, before_cells = _cells()
    monkeypatch.setattr(sys, "argv", ["rebuild", "--ids", str(DIAG_ID)])
    assert rebuild.main() == 0
    assert "[DRY-RUN] no writes" in capsys.readouterr().out

    after_row, after_cells = _cells()
    assert after_row["total_score"] == before_row["total_score"]
    assert [c["brand_verdict"] for c in after_cells] == \
           [c["brand_verdict"] for c in before_cells]


def test_one_diagnosis_failing_does_not_take_down_the_others(realdb, seeded, monkeypatch,
                                                             capsys):
    """🔴 逐份独立:清单里混一个不存在的 id,已成功那份必须仍然落库。

    逐份提交改造之后,这条是必须的回归 —— 原来是共享事务 + SAVEPOINT。
    """
    import scripts.rebuild_parenthetical_false_mentions_2026_08_06 as rebuild

    monkeypatch.setattr(sys, "argv",
                        ["rebuild", "--ids", f"{DIAG_ID},999999", "--apply"])
    rebuild.main()
    output = capsys.readouterr().out
    assert "not_found" in output
    assert "[APPLIED] committed=1" in output

    _, cells = _cells()
    assert sum(1 for c in cells if c["brand_verdict"] == "YES") == 0, (
        "同批里有一份 not_found,已重判那份竟被回滚了"
    )


# ══════════════════════════════════════════════════════════════════════
# 源码面:三种"糊法"一处都不许有
# ══════════════════════════════════════════════════════════════════════

def test_no_timeout_retry_or_nowait_papering_over_the_deadlock():
    """🔴【§4.2 明令】不许用超时 / 重试 / NOWAIT 糊死锁 —— 那只是把死锁变成随机失败。

    判据剥掉注释和字符串再看,否则"注释里解释了为什么不用"会被判成"用了"
    (剥注释≠剥字符串,两样都要剥)。
    """
    source = (ROOT / "scripts"
              / "rebuild_parenthetical_false_mentions_2026_08_06.py").read_text(encoding="utf-8")
    code = "".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    ).lower()

    # 反向对照:判据得先证明自己抓得住这些形态
    probe = "".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(
            "statement_timeout = 1\nfor attempt in range(3):\n    pass\nnowait = True\n"
        ).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    ).lower()
    for needle in ("statement_timeout", "nowait", "attempt"):
        assert needle in probe, "判据抓不住已知形态 → 没有判别力"

    for needle in ("statement_timeout", "lock_timeout", "nowait", "skip locked", "time.sleep"):
        assert needle not in code, f"重建脚本里出现了糊死锁的形态:{needle}"
