"""`brands.latest_*` 双闸判据 · 2026-08-08

对应两张单(**必须一起修,缺任一条冗余列照样脏**):
- `WO_BRAND_LATEST_CROSS_TENANT_WRITE_2026-08-08` —— 写进去的得是**自己家的**
- `WO_V2_REGEN_POLLUTES_BRAND_LATEST_2026-08-08`  —— 写进去的得是**最新那份**

## 🔴 本文件的三条纪律(踩过的坑,不是教条)

1. **打真库真事务**。守卫整个逻辑都在 SQL 里 —— 用假连接测等于什么都没测。
   需要 `TEST_DATABASE_URL`,缺了直接 skip,**不静默降级成假绿**。
2. **断言打在落库后的 `brands` 行上**,不是打在函数返回值上。
   「函数对、接线缺」这一形状 2026-08 第一周已出现四次。
   每个反例都额外跑一遍常态判据 SQL(跨品牌指向必须恒 0)。
3. **每条"必须挡下"都配一条"必须写成功"**。只有前者的话,
   把函数体改成 `return` 也能拿满分 —— 那是零判别力。

生产实测口径(2026-08-08 只读取证):`result_visibility` 取值 NULL 278 / published 56 /
withheld 4。NULL 语义 = published(历史兼容)。`diagnosis_records.created_at`
默认 `CURRENT_TIMESTAMP`。判据照这个形态构造,不自己发明。
"""

from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

psycopg2 = pytest.importorskip("psycopg2")

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL(真库判据 · 守卫逻辑全在 SQL 里,假连接测不出任何东西)",
)


# 常态判据:跨品牌指向数。任何写入路径跑完之后它都必须是 0。
CROSS_TENANT_INVARIANT_SQL = """
    SELECT count(*) FROM brands b
      JOIN diagnosis_records d ON d.id = b.latest_diagnosis_id
     WHERE d.brand_id IS DISTINCT FROM b.id
"""

# 列类型照生产实测(SQL 4 维核验第 2 维:latest_score/total_score 都是 integer,
# created_at 是 timestamp without time zone 且默认 CURRENT_TIMESTAMP)。
SCHEMA_SQL = """
DROP TABLE IF EXISTS diagnosis_records CASCADE;
DROP TABLE IF EXISTS brands CASCADE;

CREATE TABLE brands (
    id                  SERIAL PRIMARY KEY,
    name                TEXT,
    owner_user_id       INTEGER,
    diagnosis_count     INTEGER DEFAULT 0,
    latest_score        INTEGER,
    latest_diagnosis_id INTEGER,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE diagnosis_records (
    id                          SERIAL PRIMARY KEY,
    brand_id                    INTEGER,
    brand_name                  TEXT,
    total_score                 INTEGER,
    level                       TEXT,
    result_visibility           TEXT
        CHECK (result_visibility IS NULL
               OR result_visibility IN ('pending','published','withheld')),
    created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    report_v2_version           TEXT,
    report_v2_internal_md       TEXT,
    report_v2_client_md         TEXT,
    report_v2_modules_jsonb     JSONB,
    report_v2_generated_at      TIMESTAMP,
    report_v2_error             TEXT,
    data_completeness_score     INTEGER,
    data_completeness_breakdown JSONB
);
"""


def _guard_db_name(url: str) -> None:
    """别把判据跑到真库上。库名不带 test 就拒跑 —— 照 admin_user_governance 的既有约定。"""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if "test" not in name:
        raise RuntimeError(f"unsafe TEST_DATABASE_URL database name: {name!r}")


@pytest.fixture(scope="session")
def real_schema():
    """🔴 用 `init_db()` 建**生产真实 schema**,不用我手写的最小表。

    第一版我手写了 brands / diagnosis_records 两张最小表,结果 `import db.diagnosis_db`
    时模块级 `init_db()` 撞 `column "industry" does not exist` 直接挂。
    那个报错其实是在提示:**fixture 的字段形态与生产不一致**。
    改成让 init_db 自己建 —— 判据从此跑在真列、真类型、真 CHECK 约束上。
    `SCHEMA_SQL` 保留作为 init_db 不可用时的降级形态说明,但不再默认使用。
    """
    _guard_db_name(TEST_DATABASE_URL)
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    import db.diagnosis_db  # noqa: F401  —— 模块级 init_db() 建全量 schema
    return True


@pytest.fixture()
def conn(real_schema):
    from psycopg2.extras import RealDictCursor

    c = psycopg2.connect(TEST_DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        with c.cursor() as cur:
            cur.execute(
                "TRUNCATE diagnosis_records, brands RESTART IDENTITY CASCADE"
            )
        c.commit()
        yield c
    finally:
        c.close()


@pytest.fixture()
def data(conn):
    """两个品牌,各自的诊断。刻意造出三种反例形态。

    A(brand 1):published 旧(diag 1)→ published 新(diag 2)
    B(brand 2):published(diag 3)  → withheld 更晚(diag 4)
    """
    with conn.cursor() as cur:
        # 🔴 NOT NULL 且无默认的列必须给值:brands.name / diagnosis_records
        # (session_id, brand_name, industry) —— 由真 schema 实查得出,不是猜的。
        cur.execute("INSERT INTO brands (id, name) VALUES (1,'甲家'),(2,'乙家')")
        cur.execute("SELECT setval(pg_get_serial_sequence('brands','id'), 2)")
        cur.execute(
            """
            INSERT INTO diagnosis_records
                (id, session_id, brand_name, industry,
                 brand_id, total_score, result_visibility, created_at)
            VALUES
                (1, 's1', '甲家', '制造', 1, 50, 'published', NOW() - INTERVAL '10 days'),
                (2, 's2', '甲家', '制造', 1, 69, 'published', NOW() - INTERVAL '1 day'),
                (3, 's3', '乙家', '服务', 2, 31, NULL,        NOW() - INTERVAL '5 days'),
                (4, 's4', '乙家', '服务', 2, 23, 'withheld',  NOW())
            """
        )
        cur.execute("SELECT setval(pg_get_serial_sequence('diagnosis_records','id'), 4)")
        # 起点:两家都指向各自真最新的 published 那份
        cur.execute("UPDATE brands SET latest_score=69, latest_diagnosis_id=2 WHERE id=1")
        cur.execute("UPDATE brands SET latest_score=31, latest_diagnosis_id=3 WHERE id=2")
    conn.commit()
    return conn


def _brand_row(conn, brand_id: int) -> dict:
    """读**落库后**的行 —— 判据只认这个,不认函数返回值。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, latest_score, latest_diagnosis_id, diagnosis_count "
            "FROM brands WHERE id=%s",
            (brand_id,),
        )
        return dict(cur.fetchone())


class _NoCloseConn:
    """把测试连接喂给 `update_brand_stats` 用的代理。

    该函数自开连接、自 `commit()`、`finally` 里 `close()`。判据要看它**落库后**的效果,
    所以只拦 `close`(psycopg2 连接的 `close` 是只读属性,monkeypatch 不上),
    `commit` 与 `cursor` 全部**真的透传** —— 不能把 commit 也 stub 掉,
    否则测的就不是"真的写进去了"而只是"函数跑完没报错"。
    """

    def __init__(self, real):
        self._real = real

    def close(self):  # 唯一被拦的行为
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


def _cross_tenant_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(CROSS_TENANT_INVARIANT_SQL)
        return int(list(dict(cur.fetchone()).values())[0])


# ════════════════════════════════════════════════════════════════════
# 一、SSOT 单点本身的行为(五条,全部成对)
# ════════════════════════════════════════════════════════════════════


def test_a1_正例_配对正确且是最新那份_必须写成功(data):
    """反向对照的正向那一半。没有这条,'永不写' 的错误实现也能拿满分。"""
    from services.brand_latest_ssot import sync_brand_latest

    conn = data
    with conn.cursor() as cur:
        rows = sync_brand_latest(cur, diagnosis_id=2, score=77, brand_id=1)
    conn.commit()

    assert rows == 1
    after = _brand_row(conn, 1)
    assert after["latest_score"] == 77, "配对正确且是最新那份 → 必须写进去(不许砍功能)"
    assert after["latest_diagnosis_id"] == 2
    assert _cross_tenant_count(conn) == 0


def test_a2_跨品牌_诊断属于乙家却声明甲家_必须一行都不改(data):
    """`WO_BRAND_LATEST_CROSS_TENANT_WRITE` §7①。"""
    from services.brand_latest_ssot import sync_brand_latest

    conn = data
    before = _brand_row(conn, 1)
    with conn.cursor() as cur:
        rows = sync_brand_latest(cur, diagnosis_id=3, score=999, brand_id=1)  # diag 3 属乙家
    conn.commit()

    assert rows == 0
    assert _brand_row(conn, 1) == before, "跨品牌配对 → brands 必须逐字不变"
    assert _brand_row(conn, 2) == _brand_row(conn, 2)  # 乙家也不该被顺手写
    assert _cross_tenant_count(conn) == 0


def test_a2b_反向对照_同一份诊断配它真正的品牌_必须写成功(data):
    """证明 a2 的 0 行是被**归属校验**挡的,不是这条 SQL 恒不写。"""
    from services.brand_latest_ssot import sync_brand_latest

    conn = data
    with conn.cursor() as cur:
        rows = sync_brand_latest(cur, diagnosis_id=3, score=888, brand_id=2)
    conn.commit()

    assert rows == 1
    assert _brand_row(conn, 2)["latest_score"] == 888
    assert _cross_tenant_count(conn) == 0


def test_a3_自己家但是旧那份_必须一行都不改(data):
    """`WO_V2_REGEN_POLLUTES_BRAND_LATEST` §6①。

    实测事故就是这条:重生 diag 1(旧)的 v2 报告 → 客户列表分 69 被拽回 50。
    """
    from services.brand_latest_ssot import sync_brand_latest

    conn = data
    before = _brand_row(conn, 1)
    with conn.cursor() as cur:
        rows = sync_brand_latest(cur, diagnosis_id=1, score=50, brand_id=1)
    conn.commit()

    assert rows == 0
    after = _brand_row(conn, 1)
    assert after == before, "重生旧诊断 → latest_* 必须逐字不变(69 不许被拽回 50)"
    assert after["latest_score"] == 69
    assert after["latest_diagnosis_id"] == 2


def test_a4_withheld_虽然日期最晚也不算最新_且不挡住真最新那份(data):
    """`WO_V2_REGEN_POLLUTES_BRAND_LATEST` §6③ —— result_visibility 维度别丢。

    乙家的 diag 4 是 withheld 且 created_at 最晚。两件事都要成立:
      ① 它自己不能被当成"最新"写进去;
      ② 它也**不能挡住** diag 3(真正最新的 published)的同步。
    只验 ① 的话,"published-only 谓词写反了" 这种错也能蒙混过关。
    """
    from services.brand_latest_ssot import sync_brand_latest

    conn = data
    before = _brand_row(conn, 2)

    with conn.cursor() as cur:
        rows_withheld = sync_brand_latest(cur, diagnosis_id=4, score=23, brand_id=2)
    conn.commit()
    assert rows_withheld == 0, "withheld 不进 latest_*"
    assert _brand_row(conn, 2) == before

    with conn.cursor() as cur:
        rows_published = sync_brand_latest(cur, diagnosis_id=3, score=31, brand_id=2)
    conn.commit()
    assert rows_published == 1, "withheld 存在不得挡住真正最新那份 published 的同步"
    assert _cross_tenant_count(conn) == 0


def test_a5_brand_id_为_None_时归属以诊断记录为准(data):
    """W1(`diagnosis_report_v2`)的既有语义:不依赖外部 brand_id 参数。

    成对:传 None + 是最新 → 写成功;传 None + 是旧那份 → 挡下。
    """
    from services.brand_latest_ssot import sync_brand_latest

    conn = data
    with conn.cursor() as cur:
        ok = sync_brand_latest(cur, diagnosis_id=2, score=70, brand_id=None)
    conn.commit()
    assert ok == 1
    assert _brand_row(conn, 1)["latest_score"] == 70

    with conn.cursor() as cur:
        blocked = sync_brand_latest(cur, diagnosis_id=1, score=50, brand_id=None)
    conn.commit()
    assert blocked == 0, "brand_id=None 只放宽②配对,不放宽③最新"
    assert _brand_row(conn, 1)["latest_score"] == 70


# ════════════════════════════════════════════════════════════════════
# 二、接线判据 —— 打在真调用点上,不是打在 helper 上
#     (「函数对、接线缺」本周第五次,不能只测 helper)
# ════════════════════════════════════════════════════════════════════


def _v2_result(total: int) -> dict:
    """喂给 `update_diagnosis_v2_in_db` 的最小**生产真实形态**。

    🔴 键必须是 `funnel_score`(`diagnosis_report_v2.py:365` 实读),不是 `funnel`。
    我第一版写成 `funnel` —— `write_funnel_columns` 恒 False,整个 latest_* 分支根本没执行,
    于是 b1「断言不变」空过、b2「断言写入」转红。**成对判据把这个假绿逼出来了**;
    只写 b1 的话,这份判据会一路绿到上线,而它什么都没测。
    """
    return {
        "internal": {"full_markdown": "内部版正文"},
        "client": {"full_markdown": "客户版正文", "modules": {}},
        "completeness": {"score": 88, "breakdown": {}},
        "funnel_score": {"total_score": total, "level": "B"},
    }


def test_b1_接线_重生旧诊断的v2报告_不得覆盖latest(data, monkeypatch):
    """🔴 本包最重要的一条:走**真函数真事务**,不是调 helper。

    `update_diagnosis_v2_in_db(conn=...)` 是服务商对旧诊断做人工身份确认时真正跑的那条路径。
    """
    from services import diagnosis_report_v2 as m

    conn = data
    before = _brand_row(conn, 1)

    ok = m.update_diagnosis_v2_in_db(1, _v2_result(50), conn=conn)  # diag 1 = 旧那份
    conn.commit()

    assert ok is True, "报告本体照常写入(不许因为守卫把整个重生功能弄挂)"
    with conn.cursor() as cur:
        cur.execute("SELECT report_v2_version, total_score FROM diagnosis_records WHERE id=1")
        rec = dict(cur.fetchone())
    assert rec["report_v2_version"] == "v2", "diagnosis_records 该写的照写"
    assert rec["total_score"] == 50

    after = _brand_row(conn, 1)
    assert after == before, "但 brands.latest_* 必须逐字不变 —— 69 不许被拽回 50"
    assert _cross_tenant_count(conn) == 0


def test_b2_接线反向对照_重生最新那份的v2报告_必须照常同步(data):
    """没有这条,把 W1 改成"永不写"也能让 b1 通过 —— 那正是 CTO-15.23 修过的撕裂 bug。"""
    from services import diagnosis_report_v2 as m

    conn = data
    ok = m.update_diagnosis_v2_in_db(2, _v2_result(72), conn=conn)  # diag 2 = 最新那份
    conn.commit()

    assert ok is True
    after = _brand_row(conn, 1)
    assert after["latest_score"] == 72, "最新那份 → latest_score 必须被写入(功能没被砍)"
    assert after["latest_diagnosis_id"] == 2
    assert _cross_tenant_count(conn) == 0


def test_b3_接线_update_brand_stats_计数照加而latest被守卫挡下(data, monkeypatch):
    """W3。它当前是死函数(零调用点),但签名随时可能被重新接进调用链。

    成对验两件事:①不配对时 latest_* 不写;②`diagnosis_count` 语义**未被改变**(照常 +1)。
    第②条是防我自己:把计数并进守卫会静默改变计数行为。
    """
    import db.diagnosis_db as m

    conn = data
    monkeypatch.setattr(m, "get_connection", lambda: _NoCloseConn(conn))

    before = _brand_row(conn, 1)
    synced = m.update_brand_stats(brand_id=1, diagnosis_id=3, score=999)  # diag 3 属乙家
    after = _brand_row(conn, 1)

    assert synced == 0
    assert after["latest_score"] == before["latest_score"], "跨品牌 → latest_* 不写"
    assert after["latest_diagnosis_id"] == before["latest_diagnosis_id"]
    assert after["diagnosis_count"] == (before["diagnosis_count"] or 0) + 1, \
        "计数语义不许因为加守卫而改变"
    assert _cross_tenant_count(conn) == 0


def test_b4_接线反向对照_update_brand_stats_配对正确必须写成功(data, monkeypatch):
    import db.diagnosis_db as m

    conn = data
    monkeypatch.setattr(m, "get_connection", lambda: _NoCloseConn(conn))

    synced = m.update_brand_stats(brand_id=1, diagnosis_id=2, score=81)
    after = _brand_row(conn, 1)

    assert synced == 1
    assert after["latest_score"] == 81
    assert after["latest_diagnosis_id"] == 2
    assert _cross_tenant_count(conn) == 0
