"""Fleet 启动期 fail-closed schema 守卫 —— **单一来源清单**(2026-07-30)。

来历(工单 `WORKORDER_PRESTART_GUARD_COVERAGE_AND_ROLLBACK_PROBE_2026-07-30.md` §0/§2):
`server.py` 的 `ROLE=web|cron` 分支每次容器启动验**六道**守卫,而部署路径真正跑的
`scripts/prestart.py` 只验**三道** → 两处清单各写一份 → 漂移。后果:prestart 这道
"碰容器之前"的天然拦截点对 `dealer_resale` / `geo_observation` / `monitoring_product` /
`diagnosis` 四道形同虚设(实证:pack123 部署日志里 grep `DealerResale` 零命中 ——
该守卫成功/早退两个分支都会 log,一条都没出)。于是"迁移已跑完 + 现役镜像失去可重启性"
这种坏窗口只能等容器崩了才暴露。

本模块把那六道守卫连同**顺序**做成唯一清单(`FLEET_SCHEMA_GUARDS`),
`server.py`(web/cron)与 `scripts/prestart.py` 都只能经 `run_fleet_schema_guards()` 取用
→ 想漏一道必须改这一处,不可能再单侧漂移。

🔴🔴 **给"server.py 怎么少了 235 行守卫"的下一个人**:守卫**没被删,是搬到了这里**。
`80132d5f:server.py` 第 541-748 行那六个 `_verify_*_fail_closed` 与本文件对应段落
**字符串全等**(仅去掉函数名前导下划线 · 迁移时用脚本机械核对过)。
web/cron 分支不再逐行列六次调用,改成一次 `run_fleet_schema_guards(log=logger)` ——
**覆盖面没减,顺序没变**(见下方清单的注释)。要改守卫集,改这里的清单,别再在
`server.py` 或 `scripts/prestart.py` 里另列一份 —— 那两份清单漂移正是本次事故的根因。

本次(2026-07-30)覆盖面变化,**只增不减**:

| 守卫 | 迁移前 web/cron | 迁移前 prestart(部署路径) | 现在 |
|---|---|---|---|
| `diagnosis` | ✅ | ❌ **漏** | ✅ 清单 |
| `geo_article_v14` | ✅ | ✅ | ✅ 清单 |
| `article_closed_loop` | ✅ | ✅ | ✅ 清单 |
| `dealer_resale` | ✅ | ❌ **漏** | ✅ 清单 |
| `geo_observation` | ✅ | ❌ **漏** | ✅ 清单 |
| `monitoring_product` | ✅ | ❌ **漏** | ✅ 清单 |
| `whitelabel_backoffice` | ❌(运行时不验) | ✅ | ✅ **仍留在 prestart · 不进清单** |

- **白标那道为什么不并进来**:它要用 `scripts/prestart.py` 那条**正在跑迁移的连接游标**
  (`assert_whitelabel_backoffice_schema_ready(cur, require_settings=False)`),
  而清单里的守卫各自开连接;而且 web/cron 启动期本来就不验它。
  硬并进清单 = 给 web/cron 加一道它从来没跑过的门 = 超出本单范围的运行时收紧。
  → 保留原样,`tests/test_whitelabel_backoffice_scope_pg_2026_07_23.py` 的四调用点静态闸继续守它。

设计约定:
- 🔴 **本模块禁止 import server**:`scripts/prestart.py` 要用它,而 `import server`
  在 ROLE 未设时会走 `_run_sql_migrations(fail_fast=False)` = 在容器里跑迁移。
- 清单项存的是**属性名字符串**,由 `run_fleet_schema_guards` 动态解析 →
  清单本身是可断言的数据(锁能核对"清单 == 本模块定义的守卫全集"),
  且解析失败(清单指向不存在的守卫)= fail-closed 抛错,不静默跳过。
  这样"清单 == 实现全集"是断言出来的,不靠人去对齐两份列表。
- `dealer_resale` 一律以**不带 `require_full`** 的形态调用:prestart 不得比运行时更严
  (过度收紧同样是回归 —— 会让部署在 `[2.5/8]` 因运行时本来就放行的状态而 halt)。
- 🔴 **调用位置必须在迁移之后**:清单里的守卫验的是"迁移建完的对象",排在迁移前必然误判。
  `scripts/prestart.py` 里 `run_fleet_schema_guards()` 在 `for rel in MIGRATIONS` 循环**之后**,
  由 `tests/prestart/test_prestart_fleet_guard_coverage.py` 的顺序锁(AST)钉住。
- 🔴 **凡"某处必须真的调用了本模块"这类锁,用 AST 不要用源码串**:
  把调用注释掉,`"run_fleet_schema_guards(...)" in source` 照样命中 →
  变异存活(本单实测:server.py web/cron 那次调用的变异就是这么活下来的,改 AST 后才转红)。
"""

import logging

logger = logging.getLogger("GEO-SchemaGuard")

# 每道守卫通过后打的稳定标记(部署日志 grep 锚点 · 只验退出码 = 假绿,见工单 §2.3 锁 2)
FLEET_GUARD_PASS_MARKER = "[FleetSchemaGuard]"


def verify_diagnosis_schema_fail_closed():
    """[返工2 P1-3] web/cron worker 启动期 **fail-closed schema 反查**(不跑迁移 · prestart 已唯一执行)。
    资金状态机 + 可见性闸所依赖的关键 schema(diagnosis_runs 表 + 核心列 + diagnosis_records.result_visibility)
    缺失 → **raise 中止启动**(缺表下资金结算/可见性全失效 · 宁可 crash-loop 逼 prestart 先跑,绝不带残缺 schema 服务)。
    DATABASE_URL 未配置 → 跳过(本地无 DB 场景不阻断)。"""
    import os
    import psycopg2
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        logger.warning("[SchemaCheck] DATABASE_URL 未配置,跳过 fail-closed schema 反查")
        return
    conn = psycopg2.connect(database_url)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('diagnosis_runs')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("[SchemaCheck fail-closed] diagnosis_runs 表不存在 · prestart 迁移未跑 · 拒绝启动 web/cron")
        # 关键列(资金状态机 + HC1 幂等 + reconciler 回补 · 缺任一即残缺)。
        #
        # [E2-4 · Codex 二审 · 2026-08-26] 从「只核列**名**」升级为「名 + 类型 + 可空性」。
        #   原来只查 column_name 在不在 —— 一个**同名错类型**的列照样放行,而
        #   `ADD COLUMN IF NOT EXISTS` 见到同名列就是 no-op,于是错类型会一直留着,
        #   prestart 每次重放都"成功",守卫每次都放行。下面每一条的静默后果都不一样:
        #     · final_snapshot_jsonb 若是 text  → jsonb 取值语义变样,终态快照读不出来;
        #     · manual_lease_until 若丢时区     → 租约到期比较按本地时区算,双表处置抢锁;
        #     · freeze_id 若是 integer 而非 bigint → 到量级后溢出,冻结句柄对不上;
        #     · billing_mode / run_status 若可空 → 状态机多出一个没人处理的 NULL 态;
        #     · 🔴 payer_user_id 若被建成 **NOT NULL** → `NULL = payer 就是 owner`
        #       这个语义直接死掉:存量每一行都得有值,而它们本来就该是 NULL。
        #       (所以这一列要核的不是"非空",恰恰相反 —— 必须**可空**。)
        #
        # 🔴 规格是照**真 schema 实测**写的(生产 dump + 050 重放后逐列查出来的),
        #    不是照"看起来该是什么"写的。写窄了会让正确的库起不来,那比原来的洞更贵。
        _need = {
            "run_status":              ("text", "NO"),
            "client_request_id":       ("text", "NO"),
            "freeze_id":               ("bigint", "YES"),
            "freeze_backend":          ("text", "YES"),
            "final_snapshot_jsonb":    ("jsonb", "YES"),
            "billing_mode":            ("text", "NO"),
            # [返工3/4 P0] 双表 claim 持久意图 + 租约 token/到期 · 缺则双表处置崩
            "manual_resolution":       ("text", "YES"),
            "manual_resolution_token": ("text", "YES"),
            "manual_lease_until":      ("timestamp with time zone", "YES"),
            # [A-1 · 迁移 050] payer_user_id:冻结行长在谁名下。缺列的静默后果是
            #   结算又回到「拿租户 id 定位平台的冻结」—— 永远结不掉且零判据会红,
            #   所以这一列**必须**做成响亮的启动期 fail-closed。
            "payer_user_id":           ("integer", "YES"),
        }
        # 🔴 `table_schema='public'` 不是装饰:information_schema 是**跨 schema** 的,
        #    不带它就等于问"任何 schema 里有没有一张叫 diagnosis_runs 的表" ——
        #    search_path 上换一个 schema,守卫就会拿别人的表来给自己发通行证。
        #    (同族教训:DROP INDEX 语法上不绑表,撞名会静默删掉无辜表的索引。)
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='diagnosis_runs'")
        _have = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        _missing = sorted(set(_need) - set(_have))
        if _missing:
            raise RuntimeError(f"[SchemaCheck fail-closed] diagnosis_runs 缺列 {_missing} · prestart 迁移不完整 · 拒绝启动")
        _wrong = sorted(
            "%s(实际 %s/%s,要求 %s/%s)" % (col, _have[col][0], _have[col][1], want[0], want[1])
            for col, want in _need.items() if _have[col] != want)
        if _wrong:
            raise RuntimeError(
                "[SchemaCheck fail-closed] diagnosis_runs 列类型/可空性不符 " + str(_wrong)
                + " · 同名错类型的列不会被 ADD COLUMN IF NOT EXISTS 纠正 · 拒绝启动")
        # 可见性闸依赖列(全读取链 fail-closed 的基础)
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='diagnosis_records' "
                    "AND column_name='result_visibility'")
        if cur.fetchone() is None:
            raise RuntimeError("[SchemaCheck fail-closed] diagnosis_records.result_visibility 缺失 · 可见性闸失效 · 拒绝启动")
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='marketing_material_generation_attempts'"
        )
        _geo_have = {row[0] for row in cur.fetchall()}
        _geo_need = {
            "component_id", "submit_state", "poll_state", "submit_guard_token",
            "last_heartbeat_at", "provider_result_jsonb", "materialization_state",
            "resolution_state", "resolved_at",
        }
        _geo_missing = _geo_need - _geo_have
        if _geo_missing:
            raise RuntimeError(
                f"[SchemaCheck fail-closed] GEO provider attempts 缺列 {_geo_missing} · "
                "prestart 迁移不完整 · 拒绝启动"
            )
        from services.admin_user_governance_schema import verify_admin_user_governance_schema

        verify_admin_user_governance_schema(cur)
        from services.admin_cross_tenant_schema import verify_admin_cross_tenant_schema

        verify_admin_cross_tenant_schema(cur)
        from services.agent_retail_sku_schema import assert_schema_ready as assert_retail_sku_schema_ready

        assert_retail_sku_schema_ready(cur)
        logger.info("✅ [SchemaCheck] 诊断资金/可见性关键 schema 齐全")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def verify_dealer_resale_schema_fail_closed(*, require_full: bool = False):
    """web/cron read-only guard; DDL remains prestart-only.

    The independent flag defaults off.  A closed writer gate keeps legacy orders
    available even before this additive schema exists; an open gate with any
    missing table/constraint must stop the process before it can accept money.
    """
    import os
    import psycopg2
    import psycopg2.extras

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return
    # The resale schema contract intentionally reads rows by SQL aliases. Keep
    # this startup-only connection aligned with the application/test cursors;
    # psycopg2's default tuple cursor would turn every contract row into {}.
    conn = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        from services.dealer_inventory_resale import purchase_agreement_schema_status

        legal_status = purchase_agreement_schema_status(cur)
        if not legal_status["ready"]:
            raise RuntimeError(
                "[PurchaseAgreement SchemaCheck fail-closed] "
                + "；".join(legal_status["blockers"])
            )
        cur.execute(
            "SELECT value FROM system_settings WHERE key='DEALER_INVENTORY_RESALE_ENABLED'"
        )
        row = cur.fetchone()
        flag_enabled = bool(row) and str(row.get("value")).strip().lower() in {"true", "1", "yes", "on"}
        if not flag_enabled and not require_full:
            logger.info("[DealerResale SchemaCheck] 协议购买 schema 已验证；转售 flag 关闭")
            return
        from services.dealer_inventory_resale import schema_status

        status = schema_status(cur)
        if not status["ready"]:
            raise RuntimeError(
                "[DealerResale SchemaCheck fail-closed] " + "；".join(status["blockers"])
            )
        logger.info("✅ [DealerResale SchemaCheck] 逐级库存转售 schema/约束齐全")
    finally:
        conn.close()


def verify_geo_observation_schema_fail_closed():
    """Verify AI-2 governance plus AI-1/AI-3 integration schema at startup."""
    import os

    if not os.getenv("DATABASE_URL"):
        return
    from services.geo_observation.integration import verify_integration_schema_on_startup
    from services.geo_observation.wiring import verify_schema_on_startup

    verify_schema_on_startup()
    verify_integration_schema_on_startup()
    logger.info("✅ [SchemaCheck] GEO 统一观测治理/采集账本/洞察任务 schema 齐全")


def verify_geo_article_v14_schema_fail_closed():
    """Verify the exact v1.4 object definitions without running DDL."""
    import os

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return
    import psycopg2

    conn = psycopg2.connect(database_url)
    try:
        cur = conn.cursor()
        from services.geo_article_v14_schema_contract import assert_schema_ready

        assert_schema_ready(cur)
        logger.info("✅ [SchemaCheck] GEO article v1.4 exact schema contract ready")
    finally:
        conn.close()


def verify_monitoring_product_schema_fail_closed():
    """Verify monitoring entitlement objects without running DDL."""
    import os

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(
        database_url,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        cur = conn.cursor()
        from db.monitoring_db import (
            assert_monitoring_cell_retry_ready,
            assert_monitoring_identity_review_ready,
            assert_monitoring_product_matrix_ready,
        )

        assert_monitoring_product_matrix_ready(cur)
        assert_monitoring_identity_review_ready(cur)
        assert_monitoring_cell_retry_ready(cur)
        logger.info("✅ [SchemaCheck] 持续监测商品矩阵、身份确认及单格重试定义齐全")

    finally:
        conn.close()


def verify_article_closed_loop_schema_fail_closed():
    """Verify the additive sidecar definition without activating any feature."""
    import os

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return
    import psycopg2

    conn = psycopg2.connect(database_url)
    try:
        cur = conn.cursor()
        from services.article_closed_loop_schema_contract import assert_schema_ready

        assert_schema_ready(cur)
        from services.article_closed_loop_contract import (
            CANARY_THRESHOLD_POLICY_VERSION,
            feature_flag_blockers,
        )

        flag_blockers = feature_flag_blockers(
            schema_ready=True,
            compiler_policy_signed=CANARY_THRESHOLD_POLICY_VERSION != "UNSIGNED",
        )
        if flag_blockers:
            raise RuntimeError(f"article_closed_loop_flag_contract_blocked:{','.join(flag_blockers)}")
        logger.info("✅ [SchemaCheck] GEO article closed-loop sidecar exact schema contract ready")
    finally:
        conn.close()


# ========== 单一来源清单(server.py web/cron 与 scripts/prestart.py 共用) ==========
# 顺序与迁移前 `server.py:764-770` 的 web/cron 分支逐行一致(行为零变化)。
# (guard_key 用于日志/断言 · attr_name 由 run_fleet_schema_guards 动态解析)
FLEET_SCHEMA_GUARDS: tuple[tuple[str, str], ...] = (
    ("diagnosis", "verify_diagnosis_schema_fail_closed"),
    ("geo_article_v14", "verify_geo_article_v14_schema_fail_closed"),
    ("article_closed_loop", "verify_article_closed_loop_schema_fail_closed"),
    ("dealer_resale", "verify_dealer_resale_schema_fail_closed"),
    ("geo_observation", "verify_geo_observation_schema_fail_closed"),
    ("monitoring_product", "verify_monitoring_product_schema_fail_closed"),
)


def resolve_fleet_guard(attr_name: str):
    """按名解析守卫;清单指向不存在/不可调用的名字 → **抛错**(绝不静默跳过一道)。"""
    guard = globals().get(attr_name)
    if not callable(guard):
        raise RuntimeError(
            f"{FLEET_GUARD_PASS_MARKER} 清单项 {attr_name} 未定义或不可调用 · "
            "守卫清单与实现漂移 · fail-closed"
        )
    return guard


def run_fleet_schema_guards(*, log=None, prefix: str = "") -> list[str]:
    """按清单顺序跑全部 fleet 守卫;任一失败 → 异常上抛(调用方负责非零退出/拒绝启动)。

    返回已通过的 guard_key 列表(调用方可据此断言覆盖度)。
    每道单独打一行 PASS(`✅ [FleetSchemaGuard] <key> 契约通过`)——
    只验进程退出码的话,谁把某道调用删了照样绿。
    """
    log = log or logger
    passed: list[str] = []
    for guard_key, attr_name in FLEET_SCHEMA_GUARDS:
        resolve_fleet_guard(attr_name)()
        log.info(f"{prefix}✅ {FLEET_GUARD_PASS_MARKER} {guard_key} 契约通过")
        passed.append(guard_key)
    return passed
