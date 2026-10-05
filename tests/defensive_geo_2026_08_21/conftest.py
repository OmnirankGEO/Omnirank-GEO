"""防御型 GEO 判据底座(真 PG · 一次性库)。

安全栓形态照抄本仓既有房屋风格(``tests/xiaobang_vnext_2026_08_18/conftest.py``):
库名必须**同时**含 ``defgeo`` 与 ``test`` 两个词 —— 不是写死单个 URL,
因为双树 A/B 要给两臂各一个独立库,而 A/B 恰恰是用来抓
「两边都没跑起来却报无新增红」的那件工具。
2026-08-15 实测过:库名不含 ``test`` 会把 conftest 的安全栓全灭,双臂 0 junit 假绿。

schema 怎么来 —— **不手写**
---------------------------
手写一份精简 schema 就是第二套表定义:列一漏、CHECK 一少,判据全绿而生产照样炸
(本仓 2026-08-09 记过「测试 schema 类型不同构照样全绿」)。所以这里跑**真代码**:

  ① ``db.diagnosis_db.init_db()``          —— 基表(brands / quotes / confirmed_keywords …)
  ② ``db.monitoring_db.init_monitoring_tables()`` —— monitoring_tasks / monitoring_results
                                              + 全部 ``_safe_add_column`` 增列
  ③ 已登记迁移里**触及 monitoring_results 或 diagnosis_runs 的那几条**,按 manifest 原顺序重放;
  ④ 本包新增的 040/041(同样从 manifest 现扫 —— 漏登记会在这里当场炸)。

③④ 的清单都不是手抄的:两个 ``_*_migrations()`` 从 ``db.migration_manifest.MIGRATIONS``
现扫,谁改了 manifest 这里就跟着变。为什么只跑这 8 条而不是全部 110 条 ——
manifest 里其余迁移依赖 ``users`` / ``publish_records`` / ``mhz_*`` 等由别的
init 模块建的基表,在本包范围外;跑全量只会得到 50 条 UndefinedTable,
那不是「验过了」,是噪声。范围写在这里,谁都能复算。

🔴 为什么必须打真库:CUR-09 的修复改的是一条 **SQL SELECT 的列清单**。
   mock 一个 cursor 只能证明「我写的 Python 会读那两个键」,证明不了
   「这两列在真表上存在且类型吃得下」—— 本仓 2026-08-18 明确记过
   「真 HTTP 判据必须打真库」,同一个道理。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_THROWAWAY_URL = "postgresql://geo_admin:testpw@localhost:55472/geo_defgeo_test"

_REQUIRED_DB_TOKENS = ("defgeo", "test")

_configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
_dbname = _configured.rsplit("/", 1)[-1].lower()
_missing = [t for t in _REQUIRED_DB_TOKENS if t not in _dbname]
if not _configured or _missing:
    raise RuntimeError(
        "defensive_geo 判据锁死在本包一次性库上:库名必须同时含 {0};"
        "实得 {1!r}(缺 {2})。单跑用 {3}".format(
            _REQUIRED_DB_TOKENS, _configured, _missing, DEFAULT_THROWAWAY_URL
        )
    )

EXACT_THROWAWAY_URL = _configured
os.environ["DATABASE_URL"] = EXACT_THROWAWAY_URL


def _connect():
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def _monitoring_migrations() -> list[Path]:
    """从 manifest 现扫「触及 monitoring_results」的迁移,保持 manifest 原顺序。

    清单**不手抄**:manifest 变了这里自动跟着变。手抄清单漏掉的那一条不会让
    任何判据变红(本仓 2026-08-19 实证)。
    """
    from db.migration_manifest import MIGRATIONS

    out: list[Path] = []
    for rel in MIGRATIONS:
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # 扫描面 = 本包判据真正会读到的既有表。
        # ``diagnosis_runs`` 是后加的:端点判据要证明「preview 不创建 diagnosis_run」,
        # 那条断言需要那张表真的存在 —— 表不存在时判据会红成 UndefinedTable,
        # 而那种红跟被测代码毫无关系(与「两边都红先比错误签名」同源)。
        # notification_outbox 是后加的:confirm 判据要数它的增量来证明
        # 「四条腿同事务」和「失败整体回滚」。
        # 🔴 **不再按表名筛,整份 manifest 都跑**。
        #
        # 原来按表名筛(monitoring_results / diagnosis_runs / …),每加一条判据
        # 就要往正则里补一个表名;而表之间有依赖:outbox 建在 user_notifications 上,
        # user_notifications 又建在别处 —— 追了三层还没到底。追不完的原因是
        # **依赖图不在我脑子里,在 manifest 的顺序里**。既然如此就按 manifest 顺序
        # 全跑一遍,跑不起来的自然跳过(它们依赖的是本包范围外、由别的 init 模块
        # 建的基表)。代价是慢几秒,换来的是「判据要数哪张表就有哪张表」。
        #
        # 安全性不变:每条独立连接、失败即跳过并留痕;本包自己的迁移仍**不许跳过**。
        del text  # 不再用于筛选,留个名字提醒这里曾经按表名筛过
        out.append(path)
    if not out:
        raise RuntimeError(
            "manifest 里扫不到任何触及 monitoring_results / diagnosis_runs 的迁移 —— "
            "扫描面坏了。宁可当场炸,也不要给判据一个缺表缺列的库(那会红得莫名其妙)。"
        )
    return out


#: 🔴 **本包拥有的**迁移(窗A / WP0-WP2)。轴的作用域 = 本包的作用域。
#:
#: 为什么点名而不是 `if "defgeo" in rel`:窗B 合流后,manifest 里出现了
#: 042/043(窗B 的 WP4 迁移),它们也含 "defgeo"。宽匹配会把**别人的**迁移
#: 划进"本包自己的、不许跳过"里 —— 而 042 依赖一长串生产表
#: (pricing_catalog_versions → 组织席位 → quote_pricing_snapshots),
#: 在本包这个从 init_db 起建的最小库里注定装不上,于是整套判据全部 error。
#:
#: 042/043 有它们自己的判据底座:`tests/defensive_geo_w2_2026_08_21/conftest.py`
#: 装的是**生产 pg_dump**,那里才有它们的依赖。窗B 的 conftest 守它们,本包守 040/041。
#: 收窄不是放宽:分母仍然从 manifest 现扫(漏登记照样红),只是范围回到本包。
_PACKAGE_OWNED_MIGRATIONS = (
    "db/migration_040_defgeo_question_plans_2026_08_21.sql",
    "db/migration_041_defgeo_run_previews_2026_08_21.sql",
    # 门三 G9:diagnosis_runs 上的执行派发标记(additive 两列 + partial index)
    "db/migration_045_defgeo_run_dispatch_2026_08_22.sql",
)


def _defgeo_migrations() -> list[Path]:
    """本包新增迁移 —— 是否**已登记 manifest** 从 manifest 现扫,不手抄。

    这样「迁移没进 manifest」会在这里直接表现为「判据库里没有那两张表」,
    而不是等上线后才发现它永远不会跑。
    """
    from db.migration_manifest import MIGRATIONS

    registered = set(MIGRATIONS)
    missing = [m for m in _PACKAGE_OWNED_MIGRATIONS if m not in registered]
    if missing:
        raise RuntimeError(
            f"本包迁移没进 manifest:{missing}。"
            "prestart 只按 manifest 跑、不 glob 目录,漏登记 = 上线后永远不会跑。"
        )
    return [ROOT / rel for rel in MIGRATIONS if rel in set(_PACKAGE_OWNED_MIGRATIONS)]


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from db.diagnosis_db import init_db
    from db.monitoring_db import init_monitoring_tables

    init_db()
    init_monitoring_tables()
    # 🔴 钱包表(point_freezes 等)由 `db/wallet_db.py` 建 —— 那是**受保护文件**,
    #    这里只 import 调用它的建表入口,一行都不改。
    #    confirm 判据要数 point_freezes 的增量来证明「零 freeze」;表不存在时
    #    计数会抛,而一旦有人给计数加了 try/except,「零副作用」就退化成
    #    「零查询」全绿 —— 所以宁可在这里建好,让计数真的去数。
    #    ⚠️ 顺序不是猜的:钱包表建在 users 之上,第一次只调 init_wallet_tables()
    #       直接得到 `relation "users" does not exist` —— 依赖关系是被判据逼出来的。
    from db.auth_db import init_auth_db
    from db.wallet_db import init_wallet_tables

    init_auth_db()
    init_wallet_tables()

    # 🔴 **每条迁移一条新连接**。共用一条连接是个已经咬过我一次的坑:
    #    某条迁移因缺表失败后,连接进入 aborted 事务,**后续每一条**都变成
    #    `InFailedSqlTransaction` 被"跳过" —— 包括本包自己的 040/041。
    #    于是判据打在别处(手工 psql)建好的同名表上,全绿,而 conftest 其实
    #    一张表都没建成。这就是本仓「夹具没跑起来的绿不算绿」的又一形态。
    #    autocommit=True 挡不住它:迁移文件里的 DO 块/显式事务照样能把连接带进事务。
    # 🔴 具名前置:``user_notifications`` 由 ``db/migration_001_teams.sql`` 建,
    #    而那条 **不在 manifest 里**(生产由别的 init 路径跑)。
    #    ``notification_outbox`` 又建在它之上 —— 少这一条,confirm 的
    #    「outbox 恰一条 / 失败整体回滚」两组判据就没有被测对象。
    #    这里显式点名跑它,不 glob、不猜:清单短且理由写在旁边,谁都能复算。
    _PREREQ = [ROOT / "db" / "migration_001_teams.sql"]

    required = _defgeo_migrations()
    for path in [*_PREREQ, *_monitoring_migrations(), *required]:
        sql = path.read_text(encoding="utf-8", errors="replace")
        conn = psycopg2.connect(EXACT_THROWAWAY_URL)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        except psycopg2.Error as exc:
            if path in required:
                # 🔴 本包自己的迁移**不许跳过**。它跑不起来就等于判据没有被测对象,
                #    继续跑只会得到一堆「全绿但什么都没验」。宁可当场炸。
                raise RuntimeError(
                    f"本包迁移 {path.name} 执行失败,判据没有被测对象:{type(exc).__name__}: {exc}"
                ) from exc
            # 既有迁移里有几条同时触及本包范围外的表(publish_* / mhz_*)。
            # 缺表就跳过该条,但**必须留痕** —— 静默跳过会让「列没补上」
            # 表现成判据里一句莫名其妙的 UndefinedColumn。
            print(f"[defgeo conftest] 跳过 {path.name}: {type(exc).__name__}: {exc}")
        finally:
            conn.close()

    # 反向自证:本包两张表必须真的在。没有这一步,上面的 raise 只覆盖"执行抛错",
    # 覆盖不了"执行没抛错但也没建成"。
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS c FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN "
                "('defgeo_question_plans','defgeo_diagnosis_run_previews')"
            )
            built = cur.fetchone()[0]
        if built != 2:
            raise RuntimeError(
                f"conftest 跑完后本包只有 {built}/2 张表 —— 判据会打在空气上"
            )

        # 🔴 confirm 判据靠数这三张表的增量证明「零副作用 / 恰一次」。
        #    缺一张,那些计数要么抛、要么(万一有人给它加了 try)静默变成 0 增量全绿。
        #    所以在这里**响亮**地要求它们存在,而不是等判据里报一句
        #    莫名其妙的 UndefinedTable。
        missing = []
        for _t in ("diagnosis_runs", "point_freezes", "notification_outbox"):
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) AS r", (_t,))
                if cur.fetchone()[0] is None:
                    missing.append(_t)
        if missing:
            raise RuntimeError(
                f"计数表缺失 {missing} —— confirm 的「零副作用」判据会失去被测对象。"
                "检查 manifest 重放是否被更早的依赖失败卡住。"
            )

        # 🔴 种一行 feature_pricing。
        #    ``middleware/billing.freeze_points`` 会查 ``get_feature_pricing(feature_code)``,
        #    查不到直接 ``ValueError: 未知的功能编码`` —— confirm 判据第一次跑就死在这里。
        #    **这恰好证明了 freeze 真的被调用了**(不是空转、没被 mock 掉),
        #    所以补的是**数据**不是**桩**:价格设 0,与本包当前 shadow 定价一致
        #    价格设 **非 0**(7):0 价不产生 point_freezes 行,那会让
        #    「freeze 增量为 0」既像对也像错 —— 零判别力。非 0 才能真的证明
        #    「一次 confirm ⇒ 恰一条冻结行」。
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feature_pricing "
                "(feature_code, feature_name, cost_points, cost_compute) "
                "VALUES ('geo_diagnosis','GEO 诊断(判据夹具)',7,0) "
                "ON CONFLICT (feature_code) DO NOTHING"
            )
        # 🔴 给判据用的租户充值。
        #    不充值时 ``freeze_points`` 抛 HTTPException(402) —— 那条路**也要验**,
        #    但成功路径需要真有钱,否则「恰一条冻结行」永远验不到。
        # 判据用的品牌。confirm 会把 brand_id 写进 diagnosis_runs;
        # 品牌不存在时那条 INSERT 让**整个事务中止**,后续每一步都变成
        # InFailedSqlTransaction —— 报错点在 get_user,真因却在三步之前。
        # (排查它花的时间,正好是"级联噪声先找第一层真因"那条规矩的价钱。)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brands (id, name, owner_user_id, industry) "
                "VALUES (901, '判据品牌-defgeo', 8401, '本地生活服务') "
                "ON CONFLICT (id) DO NOTHING")

        for _uid in (8401, 8402, 7301, 7302):
            with conn.cursor() as cur:
                # 列名逐个从 information_schema 核过(users 没有 role 列 ——
                # 第一版按记忆写了 role,当场 UndefinedColumn。SQL 四维核验,
                # 「列名」那一维不是可选项)。
                cur.execute(
                    "INSERT INTO users (id, username, password_hash, display_name) "
                    "VALUES (%s, %s, 'x', %s) ON CONFLICT (id) DO NOTHING",
                    (_uid, "defgeo_test_" + str(_uid), "判据用户" + str(_uid)))
                cur.execute(
                    "INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points) "
                    "VALUES (%s, 100000, 0, 0) ON CONFLICT (user_id) DO NOTHING",
                    (_uid,))
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture()
def db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
