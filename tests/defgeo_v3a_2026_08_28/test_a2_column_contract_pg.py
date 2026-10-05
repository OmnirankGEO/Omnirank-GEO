"""【A-2 = Codex 三审 P1-3 + P2-4】050/051/052/054 的列合同不是 exact。

Codex 亲验的三个 poison(我逐条复现过,见交付文 §A-2)
----------------------------------------------------
1. 050 接受 ``payer_user_id INTEGER DEFAULT 777`` ⇒ 省略该列的旧 INSERT
   静默得到 payer 777;
2. 051 接受 ``settlement_attempts BIGINT`` / ``last_settlement_error INTEGER``
   ⇒ 真实 writer 写错误文本时 ``InvalidTextRepresentation``;
3. 052 接受 ``payer_user_id BIGINT DEFAULT 777`` 及身份文本默认值
   ⇒ 旧形状 INSERT 把租户 123 解析成 payer 777。

病根是同一个:``ADD COLUMN IF NOT EXISTS`` **按列名判存** —— 库上已有同名列
就直接 no-op,类型 / 长度 / 默认值一律不纠正。而 050 的手写自证核了
type 与 nullable **两轴**,default 轴**零核验**。那不是作者疏忽的极限,
是「手写守卫只守作者当时想到的那几维」的必然结果:
漏掉的那一维不会让任何判据变红。

修法(见 ``scripts/defgeo_readiness_gen.py`` 的列合同轴)
------------------------------------------------------
分母 = 文件里每一条 ``ADD COLUMN``(机械扫,走 ``strip_sql_noise``);
期望值 = 真 PG16 现读的三样,拼成一个串逐字比:

    format_type(atttypid, atttypmod) | notnull=<t/f> | default=<expr 或 ->

  · ``format_type`` **带 typmod** —— ``varchar(64)`` 与 ``varchar(255)`` 在
    ``information_schema.data_type`` 里都是 ``character varying``:
    只核 data_type 的守卫对「错长度」零判别;
  · ``default=-`` 用字面量而不是 NULL —— NULL 参与 ``<>`` 结果是 NULL 而不是
    true,整条 IF 会静默不成立,那是一个**恒绿**的守卫。

轴的作用域顺带覆盖到 046(它也有一条 ADD COLUMN)—— 加轴不是只为工单点名的
那四个文件,是为**这条轴上的每一个**。
"""
from __future__ import annotations

import psycopg2
import pytest

from tests.defgeo_v3a_2026_08_28 import conftest as CT

pytestmark = pytest.mark.integration

M050 = "db/migration_050_diagnosis_payer_identity_2026_08_25.sql"
M051 = "db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql"
M052 = "db/migration_052_defgeo_activation_frozen_payer_2026_08_25.sql"
M054 = "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql"


def _conn(dsn):
    c = psycopg2.connect(dsn)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


#: 逐发 poison。每个迁移至少三发:**错类型 / 错 typmod(长度)/ 错 default**。
#: 没有 typmod 可错的整数列(050/054)用**错可空性**补第三维 ——
#: 三维不是凑数:Codex 的三个亲验 poison 恰好分布在这三维上。
POISONS = [
    # ── 050 diagnosis_runs.payer_user_id ────────────────────────────────
    ("050-错default(Codex 亲验)", M050,
     ["ALTER TABLE diagnosis_runs ALTER COLUMN payer_user_id SET DEFAULT 777"],
     "payer_user_id"),
    ("050-错类型", M050,
     ["ALTER TABLE diagnosis_runs ALTER COLUMN payer_user_id TYPE BIGINT"],
     "payer_user_id"),
    ("050-错可空性", M050,
     ["UPDATE diagnosis_runs SET payer_user_id = owner_user_id WHERE payer_user_id IS NULL",
      "ALTER TABLE diagnosis_runs ALTER COLUMN payer_user_id SET NOT NULL"],
     "payer_user_id"),
    # ── 051 defgeo_publish_commands 两列 ────────────────────────────────
    ("051-错类型 attempts BIGINT(Codex 亲验)", M051,
     ["ALTER TABLE defgeo_publish_commands "
      "ALTER COLUMN settlement_attempts TYPE BIGINT"],
     "settlement_attempts"),
    ("051-错类型 error INTEGER(Codex 亲验)", M051,
     ["ALTER TABLE defgeo_publish_commands "
      "ALTER COLUMN last_settlement_error TYPE INTEGER USING NULL"],
     "last_settlement_error"),
    ("051-错default", M051,
     ["ALTER TABLE defgeo_publish_commands "
      "ALTER COLUMN settlement_attempts SET DEFAULT 7"],
     "settlement_attempts"),
    # ── 052 defgeo_activation_outbox 三列 ───────────────────────────────
    ("052-错类型+错default(Codex 亲验)", M052,
     ["ALTER TABLE defgeo_activation_outbox ALTER COLUMN payer_user_id TYPE BIGINT",
      "ALTER TABLE defgeo_activation_outbox ALTER COLUMN payer_user_id SET DEFAULT 777"],
     "payer_user_id"),
    ("052-错 typmod 64→255", M052,
     ["ALTER TABLE defgeo_activation_outbox "
      "ALTER COLUMN payer_funding_policy TYPE VARCHAR(255)"],
     "payer_funding_policy"),
    ("052-身份文本默认值(Codex 亲验)", M052,
     ["ALTER TABLE defgeo_activation_outbox "
      "ALTER COLUMN payer_principal_kind SET DEFAULT 'agency'"],
     "payer_principal_kind"),
    # ── 054 monitoring_run_cells.tenant_owner_user_id(P2-4)─────────────
    ("054-错default(P2-4 点名)", M054,
     ["ALTER TABLE monitoring_run_cells "
      "ALTER COLUMN tenant_owner_user_id SET DEFAULT 777"],
     "tenant_owner_user_id"),
    ("054-错类型", M054,
     ["ALTER TABLE monitoring_run_cells "
      "ALTER COLUMN tenant_owner_user_id TYPE BIGINT"],
     "tenant_owner_user_id"),
    ("054-错可空性", M054,
     ["UPDATE monitoring_run_cells SET tenant_owner_user_id = 1 "
      "WHERE tenant_owner_user_id IS NULL",
      "ALTER TABLE monitoring_run_cells "
      "ALTER COLUMN tenant_owner_user_id SET NOT NULL"],
     "tenant_owner_user_id"),
]


@pytest.mark.parametrize("label,rel,stmts,needle",
                         POISONS, ids=[p[0] for p in POISONS])
def test_a2_a_wrong_shaped_column_makes_the_migration_raise(
        chain_db, label, rel, stmts, needle):
    """🔴 列被做成错形状之后,重放该迁移必须 RAISE 并**点名是哪一列**。

    形态统一:装齐 → 把某一列 ALTER 成错形状 → 重放。
    这正是 Codex 复现 P1-3 的路径,也是生产上真实会出现的形态
    (手工建的 / 别的分支建的 / 回滚残留 / 迁移曾经跑到一半)。

    不点名列的报错等于让运维在几十列里猜 —— 所以断言里核了 needle。
    """
    conn = _conn(chain_db("a2p"))
    try:
        cur = conn.cursor()
        for s in stmts:
            cur.execute(s)
        with pytest.raises(psycopg2.Error) as err:
            CT.run_migration(conn, rel)
        assert needle in str(err.value), (
            "RAISE 了,但没点名是 %s 那一列:%s" % (needle, err.value))
    finally:
        conn.close()


@pytest.mark.parametrize("rel", [M050, M051, M052, M054])
def test_a2_a_correctly_shaped_database_replays_clean(chain_db, rel):
    """配对的必须不命中:形状**对**的库上,迁移必须能反复重放且不抛。

    prestart 每次部署无条件重放全部迁移 —— 列合同守卫要是把正确的库也拒了,
    每一次部署都会卡在迁移这一步。写窄比写漏更贵,这一条守的是那一侧。
    """
    conn = _conn(chain_db("a2ok"))
    try:
        for _ in range(2):        # 跑两遍,证明幂等不是"第一遍碰巧过"
            CT.run_migration(conn, rel)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 行为 poison —— Codex 说的「真实 writer 立即出错」,拿真 writer 打一发
# ══════════════════════════════════════════════════════════════════════════
def test_a2_the_real_settlement_writer_breaks_on_the_wrong_error_column_type(chain_db):
    """🔴 051 那一发的**行为**面:``last_settlement_error`` 被做成 INTEGER 之后,
    真实单写点 ``store.bump_settlement_attempt`` 当场 ``InvalidTextRepresentation``。

    为什么不先塞一行再打:这条语句的失败发生在**解析期** ——
    ``last_settlement_error = 'boom'`` 对着 integer 列,PG 在还没碰任何行之前
    就把字面量往 integer 上强转失败了。所以有没有匹配行都一样炸,
    而且是**确定性**的(不吃"哪一行先被扫到"的运气)。
    这也正是生产会看到的那一个错误。
    """
    from services.defensive_geo.publish import store as _store

    dsn = chain_db("a2w51")
    conn = _conn(dsn)
    try:
        conn.cursor().execute(
            "ALTER TABLE defgeo_publish_commands "
            "ALTER COLUMN last_settlement_error TYPE INTEGER USING NULL")
        cur = conn.cursor()
        with pytest.raises(psycopg2.errors.InvalidTextRepresentation):
            _store.bump_settlement_attempt(
                cur, publish_command_id="pcmd-does-not-exist", error="boom")
    finally:
        conn.close()


def test_a2_the_real_settlement_writer_is_fine_on_the_contracted_shape(chain_db):
    """配对的必须不命中:形状对的时候,同一个 writer 同一句调用必须**不抛**。

    少了它,上面那条证明不了是"形状"造成的 —— 也可能是这个调用本身就会炸。
    """
    from services.defensive_geo.publish import store as _store

    conn = _conn(chain_db("a2w51ok"))
    try:
        cur = conn.cursor()
        assert _store.bump_settlement_attempt(
            cur, publish_command_id="pcmd-does-not-exist", error="boom") == 0
    finally:
        conn.close()


def test_a2_an_old_shaped_insert_silently_gets_the_poisoned_payer(chain_db):
    """🔴 052 那一发的**行为**面,Codex 原话:「旧形状 INSERT 会把租户 123 解析成 payer 777」。

    这一条不是在验被测代码 —— 它在把「为什么错 default 算资损」变成可观测的事实:
    同一条**不提 payer_user_id** 的 INSERT,
      · 合同形状下 ⇒ ``payer_user_id IS NULL``(语义 = payer 就是 owner);
      · 被下了 ``DEFAULT 777`` 之后 ⇒ 静默变成 777,而 tenant 还是 123。
    钱的方向就此改道,没有任何一条报错、没有任何一条判据会红 —— 除非有人核 default。
    """
    # 🔴 复现时的一处订正,值得写下来:只给 ``payer_user_id`` 下 DEFAULT 777,
    #    旧形状 INSERT **不会**静默拿到 777 —— 它会撞
    #    ``defgeo_activation_outbox_payer_group``(三列必须同为 NULL 或同不为 NULL)
    #    而当场 CheckViolation。要复现 Codex 描述的那一幕,必须**三列一起**下默认值,
    #    而 Codex 的 poison 原文写的正是「payer_user_id BIGINT DEFAULT 777 **及身份文本默认值**」。
    #    这一处订正反过来加强了结论:群组 CHECK 只保证「三列同进同出」,
    #    它对「进来的是不是**对**的那三个值」零判别 —— 所以三列都得进列合同轴。
    def _old_shape_insert(dsn, poison=False):
        """真外键、真前置行。``defgeo_activation_outbox`` 对
        ``quote_pricing_snapshots(id, quote_id)`` 有真 FK —— 拿 (1,1) 硬塞会被它拦下,
        那样红的是夹具不是被测形态。所以先造一条真快照,再用它的 id 发 INSERT。
        """
        conn = _conn(dsn)
        try:
            cur = conn.cursor()
            if poison:
                for col, val in (("payer_user_id", "777"),
                                 ("payer_funding_policy", "'platform_underwrite'"),
                                 ("payer_principal_kind", "'agency'")):
                    cur.execute("ALTER TABLE defgeo_activation_outbox "
                                "ALTER COLUMN %s SET DEFAULT %s" % (col, val))
            cur.execute(
                "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
                "VALUES (9123,'v3a_owner','v3a_owner','x','v3a@example.com',1) "
                "ON CONFLICT (id) DO NOTHING")
            cur.execute("INSERT INTO brands (name, owner_user_id) VALUES ('V3A_A2', 9123) "
                        "RETURNING id")
            brand_id = cur.fetchone()[0]
            cur.execute("INSERT INTO quotes (brand_id) VALUES (%s) RETURNING id", (brand_id,))
            quote_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO keyword_selection_sessions "
                "(token, quote_id, brand_id, keywords_snapshot, expires_at) "
                "VALUES ('v3a-tok', %s, %s, '[]', '2099-01-01T00:00:00') RETURNING id",
                (quote_id, brand_id))
            sess_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO quote_pricing_snapshots "
                "(quote_id, brand_id, selection_session_id, version, reason, "
                " calculation_version, pricing_snapshot, snapshot_hash) "
                "VALUES (%s, %s, %s, 1, 'v3a', 'v1', '{}'::jsonb, repeat('c', 64)) "
                "RETURNING id", (quote_id, brand_id, sess_id))
            snap_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO defgeo_activation_outbox "
                "(accepted_snapshot_id, quote_id, brand_id, event_kind, "
                " accepted_snapshot_hash, tenant_owner_id) "
                "VALUES (%s, %s, %s, 'quote_accepted', repeat('a', 64), 123) "
                "RETURNING payer_user_id, tenant_owner_id", (snap_id, quote_id, brand_id))
            return cur.fetchone()
        finally:
            conn.close()

    payer, tenant = _old_shape_insert(chain_db("a2ok52"))
    assert tenant == 123 and payer is None, (
        "合同形状下 payer_user_id 就该是 NULL(= payer 即 owner),实得 %r/%r —— "
        "世界没造对,下面那一半的对照就没有意义" % (payer, tenant))

    payer, tenant = _old_shape_insert(chain_db("a2bad52"), poison=True)
    assert tenant == 123 and payer == 777, (
        "下了 DEFAULT 777 之后旧形状 INSERT 竟然没拿到 777(实得 %r/%r)—— "
        "那这条毒不成立,上面那一堆 default 判据守的东西要重新想" % (payer, tenant))


# ══════════════════════════════════════════════════════════════════════════
# 机械 census —— 分母是扫出来的,不是抄出来的
# ══════════════════════════════════════════════════════════════════════════
def test_a2_every_declared_column_is_under_exact_contract():
    """文件里声明了几条 ADD COLUMN,readiness 里就得有几条,且三样都核。

    双向:声明了却没进 readiness ⇒ 那一列从上线起没人守(P1-3 的病);
    readiness 里有个文件没声明的列 ⇒ 幽灵对象,同样要红。
    """
    from scripts.defgeo_readiness_gen import READINESS_FILES, declared_columns

    total = 0
    for tag, rel in sorted(READINESS_FILES.items()):
        sql = (CT.REPO / rel).read_text(encoding="utf-8", errors="replace")
        declared = declared_columns(sql)
        total += len(declared)
        block = sql.split("-- @readiness-begin %s" % tag)[1].split(
            "-- @readiness-end %s" % tag)[0]
        for col, tbl in declared:
            assert "('%s', 'public.%s'," % (col, tbl) in block, (
                "[%s] 声明了列 %s.%s,readiness 里却没有它" % (tag, tbl, col))
        if declared:
            assert "format_type(a.atttypid, a.atttypmod)" in block, (
                "[%s] 列合同没带 typmod —— varchar(64) 与 varchar(255) 的 "
                "data_type 都是 character varying,错长度零判别" % tag)
            assert "a.attnotnull" in block, "[%s] 列合同没核可空性" % tag
            assert "pg_get_expr(d.adbin, d.adrelid)" in block, (
                "[%s] 列合同**没核 default** —— 这正是 050 原来零核验的那一轴,"
                "Codex 三个亲验 poison 有两个走的就是它" % tag)
    assert total >= 8, "整条列轴只扫到 %d 列 —— 分母塌了,这些判据在守空气" % total


def test_a2_the_column_axis_has_a_real_denominator_in_each_named_migration():
    """工单点名的四个迁移,每一个都必须真的有列进了这条轴。

    没有这一条,「列轴」可以在只覆盖 046 一条列的世界里全绿,
    而 050/051/052/054 —— 也就是 Codex 打穿的那四个 —— 一条都没进来。
    """
    from scripts.defgeo_readiness_gen import declared_columns

    expected = {M050: 1, M051: 2, M052: 3, M054: 1}
    for rel, count in expected.items():
        sql = (CT.REPO / rel).read_text(encoding="utf-8", errors="replace")
        got = declared_columns(sql)
        assert len(got) == count, (
            "%s 应当声明 %d 列,机械扫到 %d 列:%r —— "
            "对不上就说明扫描器或文件变了,两边都要人看一眼" % (rel, count, len(got), got))
