"""[R5 · Owner 裁定 2] init_db 建索引守卫 `execute_index_guarded` 的判据。

守的病(2026-08-20 · R2/R4/R5 三轮遇到同一族):
    init_db 的建索引段假设「表和列都是我自己刚建的」。可**表已经存在且比生产窄**的库
    (一次性测试库 / 老 CI / 老 staging)里,`CREATE TABLE IF NOT EXISTS` 是 no-op,
    于是 `CREATE INDEX idx_brand_name ON diagnosis_records(brand_name)` 当场
    UndefinedColumn ⇒ **整个 init_db 中断**,后面几十张表全没建成、且没人看得出来。

🔴 本文件最重要的是**防伪判据** test_fresh_empty_db_skips_nothing:
    守卫的危险在于它可能替一个真的建表顺序缺陷背锅(把「炸」变成「静静地少了个索引」)。
    全新空库上一张表都不是既存的 ⇒ 任何跳过都只能是 init_db 自己的顺序错了 ⇒
    跳过集必须为**空**。这条绿,守卫才只在「别人的窄表」上生效。
"""
import os
import pathlib
import subprocess
import sys

import psycopg2

REPO = pathlib.Path(__file__).resolve().parents[2]

PROBE = (
    "import db.diagnosis_db as d;"
    " print('INDEX_SKIPPED=%r' % (sorted(set((t, tuple(c)) for t, c in d._INDEX_SKIPPED)),))"
)


def _fresh_db(dbname: str) -> str:
    admin = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"
    con = psycopg2.connect(admin)
    con.autocommit = True
    con.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % dbname)
    con.cursor().execute('CREATE DATABASE "%s"' % dbname)
    con.close()
    dsn = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0] + "/" + dbname
    c = psycopg2.connect(dsn)
    c.autocommit = True
    c.cursor().execute("CREATE EXTENSION IF NOT EXISTS vector")
    c.close()
    return dsn


def _run_init_db(dsn: str):
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", PROBE],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900,
        env={**os.environ, "PYTHONPATH": str(REPO), "DATABASE_URL": dsn},
    )


def test_fresh_empty_db_skips_nothing():
    """🔴 防伪判据:全新空库跑完 init_db,建索引**一条都不许跳**。

    跳了就说明守卫正在替一个真的建表顺序缺陷背锅 —— 那是把「响亮地炸」
    换成「静静地少个索引」,比原病更难查。
    """
    dsn = _fresh_db("geo_test_r5_idxguard_fresh")
    proc = _run_init_db(dsn)
    assert proc.returncode == 0, "空库上 init_db 没跑通:\n" + (proc.stdout + proc.stderr)[-2500:]
    line = [l for l in proc.stdout.splitlines() if l.startswith("INDEX_SKIPPED=")]
    assert line, "探针没打出 INDEX_SKIPPED —— 零分母,判据无效:\n" + proc.stdout[-1500:]
    assert line[-1] == "INDEX_SKIPPED=[]", (
        "全新空库上竟然跳过了索引,守卫正在掩盖真缺陷:" + line[-1])


def test_fresh_empty_db_still_actually_builds_the_indexes():
    """配对反向:守卫不许把**合法**的索引也跳掉(否则「跳过集为空」可以靠不建来满足)。"""
    dsn = _fresh_db("geo_test_r5_idxguard_built")
    proc = _run_init_db(dsn)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
    have = {r[0] for r in cur.fetchall()}
    conn.close()
    want = {"idx_brands_company", "idx_brands_industry", "idx_brands_owner_user_id",
            "brands_name_owner_key", "idx_brand_name", "idx_diagnosis_brand_id"}
    assert not (want - have), "守卫把合法索引也跳了,缺 %s" % sorted(want - have)


def test_preexisting_narrow_table_no_longer_aborts_init_db():
    """既存窄表:修前 init_db 当场 UndefinedColumn 中断,修后跑完并**记账**。

    夹具最常见的形态 —— 自己手搓了一张只有两列的 diagnosis_records,
    然后 init_db 想给它建 idx_brand_name(brand_name)。
    """
    dsn = _fresh_db("geo_test_r5_idxguard_narrow")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    conn.cursor().execute("CREATE TABLE diagnosis_records (id SERIAL PRIMARY KEY)")
    conn.close()

    proc = _run_init_db(dsn)
    assert proc.returncode == 0, (
        "既存窄表上 init_db 仍然炸了 —— 守卫没接上:\n"
        + (proc.stdout + proc.stderr)[-2500:])
    line = [l for l in proc.stdout.splitlines() if l.startswith("INDEX_SKIPPED=")]
    assert line, "探针没打出 INDEX_SKIPPED:\n" + proc.stdout[-1500:]
    assert "diagnosis_records" in line[-1], (
        "跳过了却没记账(或根本没跳到那张窄表):" + line[-1])
    assert "brand_name" in line[-1], "记账里没写清缺的是哪一列:" + line[-1]
    # 🔴 跳过必须**留痕**到启动日志,不许只存在内存里
    assert "建索引跳过" in (proc.stdout + proc.stderr), (
        "跳过没有打印到启动日志 —— 又变回静默黑洞")
