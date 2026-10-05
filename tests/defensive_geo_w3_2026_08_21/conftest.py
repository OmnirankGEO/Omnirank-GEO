"""窗C(WP5 媒体五步 + WP6 发布资金全序)判据底座 —— 真 PG16 一次性库。

安全栓沿用窗A/窗B:库名必须**同时**含 ``defgeo`` 与 ``test``。
2026-08-15 实测过库名不含 ``test`` 会让安全栓整个失效、双臂 0 junit 假绿;
双树 A/B 又恰恰要给两臂各一个独立库,所以不能写死单个 URL。

schema 从哪来 —— **生产 pg_dump + 本包迁移**
--------------------------------------------
本包判据要打的是:

  · ``mhz_media`` 的私有列(CUR-10 毒串必须零命中)—— 手写 schema 会漏掉
    ``price/price1/price2/wholesale_*/provider_media_id`` 里的任意一列,
    而漏掉的那一列恰恰是判据要证明"没泄漏"的那一列 → **假绿**;
  · ``user_wallets`` / ``point_freezes`` —— 资金判据要数真实冻结行;
  · 迁移 044 的三条 **partial unique** 与 freeze trigger —— 并发/不可变判据
    全部承重在它们身上。

所以装生产 dump(与窗B 同一份快照),再按 manifest 现扫重放本包迁移。

🔴 pg_dump 的 ``search_path=''`` 是毒(2026-08-12 记过):
   dump 里 ``set_config('search_path','',false)`` 会让**之后**所有不带 schema
   限定的语句找不到表。装载前中和掉,并在连接上显式 ``SET search_path``。

🔴 本包迁移**不许跳过**:跑不起来 = 判据没有被测对象。宁可当场炸。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_THROWAWAY_URL = (
    "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_w3c_test"
)

_REQUIRED_DB_TOKENS = ("defgeo", "test")

#: 生产 schema dump(与窗B 同一份 —— 不是本包生成的,是真库快照)。
PROD_SCHEMA = ROOT / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"

#: 🔴 **本包拥有的**迁移。轴的作用域 = 本包的作用域。
#:    044 依赖生产 dump 里已有的表?**不依赖** —— 六张全新表之间只有内部 FK,
#:    对既有表零 FK。所以它在任何装了 dump 的库上都装得起来。
_PACKAGE_OWNED_MIGRATIONS = (
    "db/migration_044_defgeo_publish_decision_2026_08_21.sql",
)

#: 🔴 **运行期前置**(不是本包拥有,但本包的代码在运行时真的会读它们):
#:    · 042 给 ``keyword_selection_sessions`` 加 ``customer_confirmed_snapshot_id``
#:      —— ``service_milestone.read_facts`` 的第一条查询就按它定位;
#:    · 043 建 ``defgeo_activation_outbox`` —— 同一个函数的第三条查询读它。
#:    不装它们再去报「confirm 必 500」,是拿夹具的洞冤枉被测代码。
#:    生产 prestart 会按 manifest 跑它们,所以这里装上才是**同构**的。
#:    仍从 manifest 现扫(漏登记照样红),只是把范围从"本包拥有"扩到"运行期依赖"。
_PREREQ_MIGRATIONS = (
    "db/migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql",
    "db/migration_043_defgeo_activation_outbox_2026_08_21.sql",
    # [终审 P0-1 / 窗D P1-3 2026-08-23] 046 建 ``defgeo_monitoring_attempts``。
    # 进度端点的投影(``v2_service.project_run_progress`` → ``attempt_ledger``)
    # 运行时**真的会读它**:不装的话,P1-3 的正样本(存在的 task 仍 200)
    # 会以 UndefinedTable 炸掉,而那是拿夹具的洞冤枉被测代码。
    # 幂等实测:5 张表全 CREATE TABLE IF NOT EXISTS,两个 trigger 各自
    # 前置 DROP TRIGGER IF EXISTS ⇒ 已装载的库上重放安全。
    "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
    # [包E 2026-08-24] 047 给 ``defgeo_publish_commands`` 加两列供应商单据引用。
    # ``store.COMMAND_COLUMNS`` 现在含这两列,而
    # ``test_declared_columns_match_information_schema`` 是**逐列对账**的 ——
    # 不装 047 那条判据会红在"store 多写了两列"上,而那是夹具没装起来,
    # 不是被测代码错了。additive 且 IF NOT EXISTS ⇒ 已装载的库上重放安全。
    "db/migration_047_defgeo_publish_provider_ref_2026_08_24.sql",
    # [工单B 2026-08-25] 051 同一个理由:它给 ``defgeo_publish_commands`` 再加
    # ``settlement_attempts`` / ``last_settlement_error`` 两列(结算重试账),
    # ``store.COMMAND_COLUMNS`` 已经含它们 ⇒ 不装 051,
    # ``test_declared_columns_match_information_schema`` 会红在"store 多写了两列"上,
    # 而那是夹具没装起来,不是被测代码错了(与 047 那次同形)。
    # additive 且 IF NOT EXISTS ⇒ 已装载的库上重放安全。
    "db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql",
    # 🔴 [工单C 052 · 2026-08-25 · 由工单B 顺手补] C 把
    #    ``activation_materializer`` 改成读 ``defgeo_activation_outbox`` 的
    #    三列**冻结付款人身份**(payer_user_id / payer_funding_policy /
    #    payer_principal_kind)。本包的真链夹具会调 ``materialize_pending()``
    #    ⇒ 不装 052 就是 UndefinedColumn,整包 error ——
    #    那是"夹具没装起来",不是被测代码坏了(与 047/051 那两次同形)。
    "db/migration_052_defgeo_activation_frozen_payer_2026_08_25.sql",
)

#: 本包判据会数增量的既有表。缺一张,「零副作用」会退化成「零查询」全绿。
_REQUIRED_EXISTING_TABLES = ("mhz_media", "user_wallets", "point_freezes", "feature_pricing")

#: 044 建的六张表。反向自证:跑完必须一张不少。
_PACKAGE_TABLES = (
    "defgeo_publish_slots",
    "defgeo_publish_decision_snapshots",
    "defgeo_publish_commands",
    "defgeo_publish_outbox",
    "defgeo_settlement_review_entries",
    "defgeo_provider_execution_budgets",
)

_SEARCH_PATH_POISON = "SELECT pg_catalog.set_config('search_path', '', false);"


def _resolve_url() -> str:
    configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
    dbname = configured.rsplit("/", 1)[-1].lower()
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in dbname]
    if not configured or missing:
        raise RuntimeError(
            "窗C 判据锁死在本包一次性库上:库名必须同时含 {0};实得 {1!r}(缺 {2})。"
            "单跑用 {3}".format(
                _REQUIRED_DB_TOKENS, configured, missing, DEFAULT_THROWAWAY_URL
            )
        )
    return configured


EXACT_THROWAWAY_URL = _resolve_url()
os.environ["DATABASE_URL"] = EXACT_THROWAWAY_URL


def connect():
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    with conn.cursor() as cur:
        cur.execute("SET search_path TO public")
    return conn


def package_migrations() -> list[Path]:
    """从 manifest **现扫**。漏登记 = 上线后永远不会跑 —— 在这里就要红。"""
    from db.migration_manifest import MIGRATIONS

    registered = set(MIGRATIONS)
    wanted = (*_PREREQ_MIGRATIONS, *_PACKAGE_OWNED_MIGRATIONS)
    missing = [m for m in wanted if m not in registered]
    if missing:
        raise RuntimeError(
            f"本包迁移/前置没进 manifest:{missing}。"
            "prestart 只按 manifest 跑、不 glob 目录,漏登记 = 上线后永远不会跑。"
        )
    # 🔴 顺序按 manifest 原顺序,不按我这里的元组顺序 —— 042 依赖
    #    quote_pricing_snapshots,043 依赖 042,顺序错了装不上。
    #    依赖图不在我脑子里,在 manifest 的顺序里。
    return [ROOT / rel for rel in MIGRATIONS if rel in set(wanted)]


def _load_prod_schema(conn) -> None:
    if not PROD_SCHEMA.exists():              # pragma: no cover - 环境问题
        pytest.skip(f"缺生产 schema 快照:{PROD_SCHEMA}")
    sql = PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    if _SEARCH_PATH_POISON not in sql:
        # 形态变了要当场知道 —— 静默放过等于把毒又吃回去。
        raise AssertionError(
            f"生产 dump 里找不到已知的 search_path 毒行;形态可能变了,请重新核对:{PROD_SCHEMA}"
        )
    sql = sql.replace(_SEARCH_PATH_POISON, "-- [w3c conftest] search_path 毒行已中和")

    # 🔴 第二处毒:pg_dump 17+ 会在文件头尾写 psql **元命令**
    #    ``\restrict <token>`` / ``\unrestrict <token>``。它们不是 SQL,
    #    psycopg2 直送服务端会得到 `syntax error at or near "\"`。
    #    (窗B 的同名函数没有处理这一段 —— 它那边靠 `to_regclass(...) is not None`
    #     短路,库已经预装好时根本走不到装载分支,于是这条路径从没被真跑过。
    #     这正是「夹具没跑起来的绿不算绿」的又一形态,窗C 在这里补上。)
    stripped = 0
    lines: list[str] = []
    for ln in sql.splitlines():
        if ln.lstrip().startswith(("\\restrict", "\\unrestrict")):
            stripped += 1
            lines.append("-- [w3c conftest] psql 元命令已剥离(第 %d 行)" % (stripped,))
        else:
            lines.append(ln)
    if stripped == 0:
        # 形态变了要当场知道:dump 换了版本、不再写元命令,这条剥离就成了死代码。
        raise AssertionError(
            f"生产 dump 里一条 psql 元命令都没有 —— 剥离规则可能已经过时:{PROD_SCHEMA}"
        )
    sql = "\n".join(lines)
    if "\\restrict" in sql or "\\unrestrict" in sql:
        raise AssertionError("psql 元命令没有被完全剥离 —— 剥离规则与 dump 形态不匹配")

    # 🔴 第三处:``CREATE EXTENSION``。
    #    ⚠️ **这一段第一版是死代码**(2026-08-21 由并发跑判据的同窗 agent 抓到):
    #    它写在 ``sql = "\\n".join(lines)`` **之前**,而那一行会把整个 ``sql``
    #    覆盖回按原文攒的 ``lines`` —— 于是「加 IF NOT EXISTS」根本没进 execute。
    #    注释说在守、实际没在守,是最难查的一种假守卫。移到 join 之后。
    #
    #    ⚠️ 更重要的是:``CREATE EXTENSION IF NOT EXISTS`` **不是并发安全的** ——
    #    两个事务都看到"不存在"再各插一行,照样撞 ``pg_extension_name_index``。
    #    所以真正的修复是下面那把 advisory lock,这里的归一只是纵深防御。
    sql = sql.replace("CREATE EXTENSION IF NOT EXISTS", "CREATE EXTENSION")
    sql = sql.replace("CREATE EXTENSION ", "CREATE EXTENSION IF NOT EXISTS ")
    assert "CREATE EXTENSION IF NOT EXISTS IF NOT EXISTS" not in sql, "归一写重了"

    with conn.cursor() as cur:
        cur.execute(sql)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.autocommit = True
    try:
        # 🔴🔴 **整个装载过程持一把 pg advisory lock**。
        #    2026-08-21 实测:同一个一次性库上并发跑两个 pytest session 时,
        #    ``to_regclass`` 探针会落进另一个 session 的 drop→recreate 窗口,
        #    于是两边同时走进装载分支,``CREATE EXTENSION IF NOT EXISTS``
        #    在并发下**不安全**(两个事务都看到"不存在"再各插一行),
        #    错误签名是 `duplicate key ... pg_extension_name_index`。
        #    那种红与被测代码毫无关系 —— 正是「两边都红先比错误签名」要挡的噪声。
        #    锁 id 用固定常量:同库的所有 session 共用同一把,跨库互不影响
        #    (advisory lock 是**库级**的)。
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (0x64656667_00000044,))
        try:
            # 🔴 幂等装载:一次性库在同一次开发里会被反复复用。
            #    生产 dump 里的 CREATE TABLE **没有** IF NOT EXISTS,第二次装必炸。
            #    ⚠️ 探针不能只探一张随便的表:半装的库会让「探到了」=「装好了」。
            #    所以探完之后下面还有一段**逐表**反向自证
            #    (_REQUIRED_EXISTING_TABLES + _PACKAGE_TABLES),半装的库会在那里响亮地红。
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.mhz_media') AS r")
                already = cur.fetchone()[0] is not None
            if not already:
                _load_prod_schema(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (0x64656667_00000044,))
    finally:
        conn.close()

    # 🔴 **每条迁移一条新连接**:某条失败后连接进 aborted 事务,
    #    后续每条都变成 InFailedSqlTransaction 被"跳过" —— 那是假绿的老形态。
    for path in package_migrations():
        sql = path.read_text(encoding="utf-8", errors="replace")
        conn = psycopg2.connect(EXACT_THROWAWAY_URL)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(sql)
        except psycopg2.Error as exc:
            raise RuntimeError(
                f"本包迁移 {path.name} 执行失败,判据没有被测对象:{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            conn.close()

    # ── 反向自证:表真的在(覆盖「执行没抛错但也没建成」)──────────────────
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name = ANY(%s)",
                (list(_PACKAGE_TABLES),),
            )
            built = {r["table_name"] for r in cur.fetchall()}
        missing = sorted(set(_PACKAGE_TABLES) - built)
        if missing:
            raise RuntimeError(f"conftest 跑完后缺表 {missing} —— 判据会打在空气上")

        for t in _REQUIRED_EXISTING_TABLES:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) AS r", (f"public.{t}",))
                if cur.fetchone()["r"] is None:
                    raise RuntimeError(
                        f"计数表 {t} 缺失 —— 「零副作用 / 恰一次冻结」判据会失去被测对象"
                    )

        # ── 判据用的种子数据 ────────────────────────────────────────────────
        with conn.cursor() as cur:
            # 🔴 feature_pricing:freeze_points 查不到 feature_code 会直接
            #    ValueError —— 那**恰好证明** freeze 真的被调用了(不是被 mock 掉)。
            #    价格设 0(media_publish 现役就是 0),exact points 全走 extra_cost。
            cur.execute(
                "INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute) "
                "VALUES ('media_publish','媒体一键发布(判据夹具)',0,0) "
                "ON CONFLICT (feature_code) DO NOTHING")
            # 🔴 [窗G 段二③ · Owner 2026-08-24] 发布链的消耗代号已收敛到
            #    ``media_proxy_publish``(publish_funding.PUBLISH_FEATURE_CODE)。
            #    旧码那一行**保留**:组织路由的 legacy /api/meijiehezi/publish
            #    仍声明它,删掉会让另一条链的判据以无关理由变红。
            #    这一行是**数据不是 schema** —— 生产侧对应发车单里的幂等 ops 步骤,
            #    不进迁移(prestart 每次部署无条件重放全部迁移 + 体内禁 DML)。
            cur.execute(
                "INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute) "
                "VALUES ('media_proxy_publish','媒体代发(判据夹具)',0,0) "
                "ON CONFLICT (feature_code) DO NOTHING")
            for uid, name in ((9301, "w3c 服务商甲"), (9302, "w3c 服务商乙"),
                              (9303, "w3c 平台直营")):
                cur.execute(
                    "INSERT INTO users (id, username, password_hash, display_name) "
                    "VALUES (%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
                    (uid, f"w3c_test_{uid}", name))
                cur.execute(
                    "INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points) "
                    "VALUES (%s, 100000, 0, 0) ON CONFLICT (user_id) DO NOTHING", (uid,))
            for bid, owner in ((9401, 9301), (9402, 9302)):
                cur.execute(
                    "INSERT INTO brands (id, name, owner_user_id, industry) "
                    "VALUES (%s,%s,%s,'本地生活服务') ON CONFLICT (id) DO NOTHING",
                    (bid, f"w3c 判据品牌 {bid}", owner))
            # 🔴 媒体目录种子 **必须带私有列的真值**:CUR-10/POR-06 的毒串判据
            #    要证明「私有列有内容却没漏出去」。私有列全是 NULL 的夹具
            #    会让泄漏判据零判别力(什么都不写自然什么都不漏)。
            for mid, mname, domain, price_points in (
                (990001, "判据日报", "example-daily.com.cn", 120),
                (990002, "判据行业网", "trade-example.com", 80),
                (990003, "判据资讯站", "news.trade-example.com", 60),
                (990004, "判据地方网", "local-example.cn", 40),
                (990005, "判据长尾站", "tail-example.net", 20),
                (990006, "判据备选站", "alt-example.org", 30),
            ):
                cur.execute(
                    "INSERT INTO mhz_media (id, media_name, source_domain, our_price_points, "
                    "is_active, provider, provider_media_id) "
                    "VALUES (%s,%s,%s,%s,TRUE,'mhz',%s) ON CONFLICT (id) DO NOTHING",
                    (mid, mname, domain, price_points, mid + 500000))
            # 🔴 [工单B B-3 · 2026-08-25 回归夹具追加] 外发通道**已配置**这个前置事实。
            #    confirm 侧的 ``capability_available`` 从写死 True 改成真实
            #    ``provider_transport.readiness()`` 之后,这一行不在 ⇒ readiness False
            #    ⇒ 每一条 confirm 都 503(ADMISSION_UNAVAILABLE),本包 12 条真链臂全红。
            #    那不是"判据被削",是生产语义真的多了一个前置:通道从没配过就不许冻钱。
            #    夹具必须**真造这个前置事实**,而不是把探针 patch 掉
            #    (patch 掉 = 夹具替被测代码干活 ⇒ 那道闸恒绿)。
            #    反向对照判据(删掉这一行 ⇒ confirm 必 503 且零副作用)在
            #    tests/defgeo_wob_publish_funding_2026_08_25/test_b3_*.py。
            cur.execute(
                "INSERT INTO mhz_config (key, value) VALUES ('phpsessid','w3-fixture-session') "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture()
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
