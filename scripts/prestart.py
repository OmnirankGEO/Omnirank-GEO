"""
ROLE=prestart 一次性预检(WORKERS=4 · SPEC D4 · FF6 硬化)
====================================================================
fleet(web/cron)服务前**单飞一次**,跑完退出。**任何依赖/配置/迁移失败 → 非零退出**
(让 deploy 编排在此 abort,不把坏 schema 的 fleet 放出去)。

FF6 硬化(boss 收口#3):
- **pg_advisory_lock 全局单飞**:多 prestart 容器/重试并发时,只有一个真正跑迁移,其余阻塞等它
  跑完(迁移幂等,等到后再跑一遍是快速 no-op)→ 消除并发建表/加约束竞争。statement_timeout 兜底
  防持有者卡死无限等。
- **非零退出**:DATABASE_URL 缺 / DB 连不上 / 迁移文件缺失 / 迁移 SQL 报错(含 migration 内 fail-closed
  RAISE)→ 一律非零退出码,deploy 脚本据此 abort。
- **两份迁移**:scheduling 三表 + diagnosis_runs 资金状态机,都在 fleet 起前建好。
"""
import logging
import os
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("prestart")

# 全局单飞 advisory lock key(distinct · 不与 service_fee_engine 0xC0DEAF / 业务锁冲突)
_PRESTART_LOCK_KEY = 907071313
_LOCK_WAIT_TIMEOUT_MS = 120000  # 拿锁最多等 120s,超时=异常=非零退出(防持有者卡死)

# ============================================================================
# [WO_PRESTART_LOCK_TIMEOUT 2026-08-16] 迁移期 lock_timeout / statement_timeout
# ============================================================================
# 🔴 为什么加(第 30 班两次中止实证,不是预防性洁癖):
#   prestart 无条件重放全部迁移,其中多条是飞轮表上的 `ALTER … ADD COLUMN`,要
#   **AccessExclusiveLock**。而 Admin 飞轮页每打开一次就发重查询,nginx 60s 已返 504
#   给浏览器,**后端 SELECT 却继续跑一个多小时**并持 AccessShareLock。
#   PostgreSQL 锁队列是 **FIFO**:ALTER 排在这些 SELECT 后面之后,
#   **所有后来的读又排在 ALTER 后面** → 整张表对全站不可读。
#   实测:第一次堵 20 分钟(11 条未授予锁),第二次堵 40 分钟;
#   两次分别撞 `geo_research_source_signals` 与 `geo_research_articles`
#   —— **两张不同的表 ⇒ 结构性,不是巧合**。
#
# 🔴 关键认识:**排队本身才是伤害,不是「迁移慢」。**
#   迁移自己等多久无所谓(切流之前,生产还跑在旧槽);
#   真正的事故是它站在队头把后来的读全堵死。
#   所以修法是**快速失败**(让出队头),不是「等久一点」。
#
# 行为:拿不到锁 → 10s 失败 → prestart 非零退出 → deploy 在 [2.5/8] abort(**切流之前**)
#      → 生产读**零影响**。把「全站排死 40 分钟」换成「部署失败,人来处理」。
#
# ⚠️ `statement_timeout` 原为 0(注释理由:建索引可能慢)。改成有限值有**真实风险**:
#   某条本来就慢的迁移会从「慢但成功」变成「超时失败」。故:
#     · 默认给到 300s(远大于现役全部迁移的实测耗时)
#     · **两个值都可用环境变量调**,不必改代码发版
#     · 判据 §2-3 要求全部现役迁移在干净库 + 生产形状库各重放一遍无新错
_MIGRATION_LOCK_TIMEOUT = os.getenv("PRESTART_MIGRATION_LOCK_TIMEOUT", "10s")
_MIGRATION_STMT_TIMEOUT = os.getenv("PRESTART_MIGRATION_STATEMENT_TIMEOUT", "300s")
#: PostgreSQL SQLSTATE 55P03 = lock_not_available(lock_timeout 命中时抛这个)
_SQLSTATE_LOCK_NOT_AVAILABLE = "55P03"

# [返工 P1-5] 跑**全量**迁移清单(单一权威源 db/migration_manifest)· 与 server.py 共用避免漂移。
#   生产 fleet(web/cron)导入期跳过迁移 → 全部 DDL/backfill 必须在此 prestart 单飞一次跑完(advisory-lock)。
# [修复净增量修] manifest 加载失败**不再静默降级最小集**(那样其余表不建 → fleet 起来炸,却 return 0 骗过 deploy):
#   loud logger.error + **fail-fast 非零退出**(prestart 的契约就是任何失败让 deploy abort · 不放坏 schema 出去)。
try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.migration_manifest import MIGRATIONS
except Exception as _mf_err:
    logger.error(f"[prestart] ❌ 迁移清单 db.migration_manifest 加载失败(部署完整性问题 · 非零退出让 deploy abort · "
                 f"绝不降级最小集导致 fleet 缺表): {_mf_err}", exc_info=True)
    sys.exit(5)


def _apply(cur, root: Path, rel: str) -> None:
    path = root / rel
    if not path.exists():
        raise RuntimeError(f"迁移文件缺失(依赖失败): {rel}")
    sql = path.read_text(encoding="utf-8")
    sql = re.sub(r'^\s*\\[a-zA-Z_]+.*$', '', sql, flags=re.MULTILINE)         # 剥 psql \命令
    sql = re.sub(r'^\s*(BEGIN|COMMIT)\s*;\s*$', '', sql, flags=re.MULTILINE | re.IGNORECASE)
    cur.execute(sql)   # 任何 SQL 错误(含 migration fail-closed RAISE)向上抛 → 非零退出
    logger.info(f"[prestart] ✅ 迁移完成: {rel}")


def _report_blockers(dsn: str) -> None:
    """[WO_PRESTART_LOCK_TIMEOUT §1-配套1] lock_timeout 失败时打印**被谁挡**。

    🔴 目的很具体:让下一个人 **10 秒内知道该 cancel 谁**,而不是自己再从
    `pg_stat_activity` 摸一遍(第 30 班我摸了两轮才定位到 blocker)。

    🔴 **另开一条连接**:失败那条连接的会话状态不可信(且它马上要被 finally 关掉),
    诊断信息不能依赖它。这条连接自己也设短超时,**绝不能让诊断动作变成第二个坝**。
    """
    conn = None
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=5)
        conn.autocommit = True
        c = conn.cursor()
        c.execute("SET lock_timeout = '2s'")
        c.execute("SET statement_timeout = '10s'")
        # 🔴 **不能只筛 `state='active'`** —— 第一版就是那么写的,判据当场打回:
        #   真正攥着锁不放的往往是 **`idle in transaction`**(查询跑完了,事务没关,
        #   ACCESS SHARE 一直握着)。这正是生产上血缘三报的形态:
        #   nginx 60s 已 504,后端事务还开着。只看 active 会漏掉它,
        #   打出「当前已无活动会话」这种**误导性结论**。
        #   现在改为:**直接从 `pg_locks` 找持锁者**(谁持锁是事实,不是状态推测),
        #   按事务年龄排序 —— 队头那个就是要 cancel 的。
        c.execute(
            """
            SELECT a.pid,
                   coalesce(a.client_addr::text, 'local'),
                   coalesce(a.state, '?'),
                   date_trunc('second',
                       now() - coalesce(a.xact_start, a.query_start, a.backend_start))::text,
                   coalesce(string_agg(DISTINCT c.relname, ',' ORDER BY c.relname), '-'),
                   left(regexp_replace(coalesce(a.query, ''), '\\s+', ' ', 'g'), 80)
              FROM pg_stat_activity a
              JOIN pg_locks l ON l.pid = a.pid AND l.granted
              LEFT JOIN pg_class c ON c.oid = l.relation
                     AND c.relnamespace = 'public'::regnamespace
             WHERE a.pid <> pg_backend_pid()
               AND a.state IN ('active', 'idle in transaction',
                               'idle in transaction (aborted)')
             GROUP BY a.pid, a.client_addr, a.state, a.xact_start, a.query_start,
                      a.backend_start, a.query
             ORDER BY 4 DESC
             LIMIT 10
            """
        )
        rows = c.fetchall()
        if not rows:
            logger.error("[prestart] 🔴 lock_timeout:但当前查不到任何持锁的活动/挂起事务 "
                         "—— 阻塞者可能刚结束;重跑一次部署即可")
            return
        logger.error("[prestart] 🔴 lock_timeout —— 下面是**当前持锁**的会话(按事务年龄倒序),"
                     "队头那几个就是要 cancel 的对象:")
        for pid, addr, state, age, rels, q in rows:
            logger.error(f"[prestart]    pid={pid} 来自={addr} 状态={state} 事务年龄={age} "
                         f"持锁表={rels} :: {q}")
        logger.error("[prestart]    处置(人工判断后执行,**本脚本不自动重试** —— "
                     "自动重试会在长查询没清干净时反复插队,每次插队本身又是一个短暂的坝):")
        logger.error("[prestart]      docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope "
                     "-c \"SELECT pg_cancel_backend(<pid>);\"")
    except Exception as diag_err:  # 诊断失败绝不能盖掉原始错误
        logger.error(f"[prestart] (阻塞者诊断本身失败,不影响上面的原始错误): {diag_err}")
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def main() -> int:
    try:
        import psycopg2
    except Exception as e:
        logger.error(f"[prestart] psycopg2 不可用(依赖失败): {e}")
        return 2
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        logger.error("[prestart] DATABASE_URL 未配置(配置失败)")
        return 3
    root = Path(__file__).resolve().parent.parent

    conn = None
    locked = False
    # 🔴 [WO_PRESTART_LOCK_TIMEOUT · 实测修正] 两个闸必须走 **PGOPTIONS**,不能只 `SET` 在
    #   prestart 自己那条连接上 —— 第一版就是那么写的,判据当场把它打回来了:
    #   下面 `import db.diagnosis_db` 会**另开一整个连接池**(日志 `[DB] 连接池已初始化 min=5,max=25`),
    #   它的 DDL 跑在**池里的连接**上,压根不带我 SET 的会话参数 ⇒ 照样无限排队。
    #   实测:只 SET 会话参数时,有阻塞者的场景 prestart **挂 240s 未返回**(判据 §2-1 判红)。
    #   PGOPTIONS 是进程级的,该进程之后建的**每一条 libpq 连接**(含池)都继承 ⇒ 全覆盖。
    _pgopts = os.environ.get("PGOPTIONS", "").strip()
    os.environ["PGOPTIONS"] = (
        f"{_pgopts} -c lock_timeout={_MIGRATION_LOCK_TIMEOUT} "
        f"-c statement_timeout={_MIGRATION_STMT_TIMEOUT}"
    ).strip()
    logger.info(f"[prestart] 迁移期闸门(PGOPTIONS · 覆盖本进程全部连接含连接池):"
                f"lock_timeout={_MIGRATION_LOCK_TIMEOUT} · "
                f"statement_timeout={_MIGRATION_STMT_TIMEOUT} "
                f"(可用 PRESTART_MIGRATION_LOCK_TIMEOUT / "
                f"PRESTART_MIGRATION_STATEMENT_TIMEOUT 覆盖)")

    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        # 全局单飞:阻塞获取 advisory lock(statement_timeout 兜底防无限等)
        # 🔴 advisory lock 也受 lock_timeout 管辖(PG 文档:table/index/row/**其他数据库对象**),
        #   而单飞锁本来就该等满 120s(等的是**另一个 prestart**,不是业务查询,不占谁的队头)。
        #   所以这条连接上先把 lock_timeout 关掉,拿到锁之后再按配置值打开。
        cur.execute("SET lock_timeout = 0")
        cur.execute(f"SET statement_timeout = {_LOCK_WAIT_TIMEOUT_MS}")
        cur.execute("SELECT pg_advisory_lock(%s)", (_PRESTART_LOCK_KEY,))
        locked = True
        logger.info("[prestart] 已获全局单飞锁 · 开始迁移")
        # [WO_PRESTART_LOCK_TIMEOUT 2026-08-16] 原为 `SET statement_timeout = 0`
        #   —— 迁移在「无 statement_timeout + 无 lock_timeout」下跑,拿不到锁就**无限排队**,
        #   而排在队头的 ALTER 会把后来的读全堵死(第 30 班实测 20 / 40 分钟)。
        #   现在两个闸都上:拿不到锁 10s 快速失败,让出队头。理由详见文件头常量处。
        #   拿到单飞锁后,把这条连接也拉回配置值(PGOPTIONS 已覆盖后续新连接,
        #   但**这条连接**刚才为了等单飞锁把 lock_timeout 关过,必须显式恢复)。
        cur.execute(f"SET lock_timeout = '{_MIGRATION_LOCK_TIMEOUT}'")
        cur.execute(f"SET statement_timeout = '{_MIGRATION_STMT_TIMEOUT}'")
        # [开源 E10 · 2026-09-28] 空库冷启动:先按 db/cold_start.BOOTSTRAP 的固定顺序建「运行时代码建的表」
        #   与「不在 manifest 里的历史 SQL 建的表」;哨兵表 users 已在(生产库)⇒ 整段跳过,空操作。
        from db.cold_start import bootstrap as _cold_start_bootstrap

        _was_cold = _cold_start_bootstrap(cur, root, log=logger, prefix="[prestart] ")

        # Disaster-recovery/fresh databases need the legacy application base
        # tables before additive SQL migrations run.  diagnosis_db's init is
        # the existing SSOT for quotes/topics/articles/article_generations; its
        # corrected order creates topics without the legacy FK, creates
        # articles, then adds the canonical topics -> articles FK.
        import db.diagnosis_db  # noqa: F401  # intentional base-schema bootstrap

        # [#156 · 2026-09-08] 同一个理由、同一种做法:媒介盒子那组表也由**运行时代码**
        #   (`db/meijiehezi_db.init_mhz_tables`)建,而 migration_034 直接 ALTER 它们。
        #   干净库上 manifest 先跑 ⇒ 034 整支失败(实测报 publish_idempotency_keys、
        #   修掉第一张后又报 mhz_short_video_drafts),而失败被记日志后**继续往下走**,
        #   于是灾备重建会得到一个少了一批列的库,**没有任何东西喊**。
        #   🔴 修法不是往迁移里复制建表语句 —— 那会让同一张表有两份定义,
        #      正是"schema 由运行时代码建"这个病的加重版。照上面那行的先例引导即可。
        from db.meijiehezi_db import init_mhz_tables

        init_mhz_tables()
        logger.info("[prestart] ✅ 媒介盒子基础 schema 已引导(034 依赖它)")

        logger.info("[prestart] ✅ 写作基础 schema 已按现役 init_db 顺序引导")
        for rel in MIGRATIONS:
            _apply(cur, root, rel)
        # [E10 · 0913AO · Review 09-28] 空库冷启动与生产 schema 对齐:只由历史 SQL / 手工 ALTER 建出来、
        #   在役代码读写的表与列(例:confirmed_keywords.brand_id,确认报价 RETURNING 它)。
        #   只在**开跑时是空库**时执行(_was_cold);生产库一步不走。生成方法见 scripts/gen_cold_start_parity.py。
        if _was_cold:
            from db.cold_start import apply_parity as _cold_start_parity

            _cold_start_parity(cur, root, log=logger, prefix="[prestart] ")
        # [守卫单一来源 2026-07-30 · 工单 §2] fleet 全量 fail-closed 守卫。
        #   历史:此处只验 geo_article_v14 / article_closed_loop 两道 + 下面的白标一道,
        #   而 `server.py` 的 web/cron 分支每次容器启动验**六道** → 两份清单漂移 →
        #   prestart(**碰容器之前**的唯一拦截点)对 dealer_resale / geo_observation /
        #   monitoring_product / diagnosis 四道形同虚设(pack123 部署日志 grep DealerResale 零命中)。
        #   现在两边共用 services/startup_schema_guards.FLEET_SCHEMA_GUARDS 这一份清单。
        #   任一守卫抛错 → 落到下面 except → return 4(非零)→ deploy 在 [2.5/8] halt,候选不启动。
        #   dealer_resale 用**不带 require_full** 的形态(= web/cron 口径):
        #   prestart 不得比运行时更严,否则运行时本来放行的状态会把部署卡死。
        from services.startup_schema_guards import run_fleet_schema_guards

        run_fleet_schema_guards(log=logger, prefix="[prestart] ")
        # [统一 R3 §七] 白标 backoffice 作用域：与 migration 自验/runtime/统一 release
        # 同一 DB 合同函数。**prestart 独有**（不在 fleet 清单里：web/cron 启动期不验它，
        # 且它要用 prestart 这条跑迁移的连接游标）——保留在此，不因"清单统一"而删掉覆盖面。
        # require_settings=False：fresh 库 whitelabel_settings 基表
        # 由运行时 init_referral_tables 兜底收敛（migration fresh 守卫跳过 §1），
        # 表一旦存在即被合同全量核验。
        from services.whitelabel_backoffice_schema_contract import (
            assert_whitelabel_backoffice_schema_ready,
        )

        assert_whitelabel_backoffice_schema_ready(cur, require_settings=False)
        logger.info("[prestart] ✅ 白标 backoffice 作用域 schema contract exact match")
        logger.info("[prestart] 预检完成 · 全部迁移就绪")
        return 0
    except Exception as e:
        logger.error(f"[prestart] ❌ 失败,非零退出(deploy 应 abort): {e}", exc_info=True)
        # [WO_PRESTART_LOCK_TIMEOUT §1-配套1] 只有 lock_timeout(55P03)才打阻塞者清单 ——
        #   其他失败(SQL 语法 / fail-closed RAISE / 缺表)跟锁无关,打了只会淹没真错误。
        if getattr(e, "pgcode", None) == _SQLSTATE_LOCK_NOT_AVAILABLE:
            logger.error("[prestart] 🔴 这是 **lock_timeout** 失败(SQLSTATE 55P03),"
                         "不是迁移本身出错 —— 迁移一行都没执行,库未被改动。"
                         "部署已在切流【之前】中止,生产读零影响。")
            _report_blockers(dsn)
        return 4
    finally:
        try:
            if conn is not None and locked:
                c2 = conn.cursor()
                c2.execute("SET statement_timeout = 0")
                c2.execute("SELECT pg_advisory_unlock(%s)", (_PRESTART_LOCK_KEY,))
        except Exception:
            pass  # 断连时 advisory lock 自动释放
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
