"""空库冷启动引导(开源 E10 硬前置 · Review 09-28)。

病:本仓的表不全由迁移链建。一部分由**运行时代码**建(`init_auth_db` 建 users 等,web / cron 每次启动都跑),
一部分由**不在 migration_manifest 里的历史 SQL** 建(生产当年手工跑过,之后的迁移直接 ALTER 它们)。
生产库上这些表早就都在,所以没人发现;空库上 prestart 按 manifest 跑到第一个 ALTER 就 rc 4(第一个缺的就是 users)。

修法(照 prestart 里 `db.diagnosis_db` / `init_mhz_tables` 的先例,不往迁移里复制建表语句 —— 那会让同一张表有两份定义):
  在 prestart 跑 manifest **之前**,按下面 BOOTSTRAP 的固定顺序调现有的建表函数 / 跑那几份历史 SQL。
  · **只在冷库上跑**:哨兵表 `users` 不存在 ⇒ 冷库。生产库有 users ⇒ 整个引导一步都不走(空操作,不碰任何表)。
  · 顺序固定、每一步幂等(函数都是 CREATE … IF NOT EXISTS;SQL 在冷库上只跑一次),结果确定,可重跑。
  · 每一步都写明「谁需要它」:删掉任何一步,冷启动判据会红在点名的那个迁移上(tests/cold_start_2026_09_28)。
导出树(开源):本列表只引用导出后仍在的模块与 SQL;导出流水线若剥掉某个模块,对应步骤一并剥(导出树冷启动由 selfcheck 导出臂实跑判)。
"""
from __future__ import annotations

import importlib
import logging
import re
from pathlib import Path

logger = logging.getLogger("cold_start")

#: 冷库判据:这张表只由引导第一步(init_auth_db)建;生产库一定有它。
SENTINEL = "users"

#: (种类, 目标, 谁需要它)。种类 py = "模块:函数"(无参调用);pycur = 同上但传当前游标;sql = 仓内路径(剥 BEGIN / COMMIT / psql 反斜杠命令后整份执行);
#: ddl = 一条建表语句(只用于仓内没有任何定义、生产靠历史手工建出来的表,形状照生产 schema 快照);import = 导入即建表的模块。
BOOTSTRAP: list[tuple[str, str, str]] = [
    ("py", "db.auth_db:init_auth_db", "users / 角色权限等基表;manifest 第一份 migration_v3_2_c_end 就 FROM users"),
    ("py", "db.wallet_db:init_wallet_tables", "recharge_orders / feature_pricing;manifest 里多份迁移 ALTER 它们,v35 也 ALTER"),
    ("py", "db.publish_db:init_publish_tables", "publish_outcome_records 等发布表,也建 mhz_media(带 category);必须在 init_mhz_tables 之前 —— 运行时 server.py 也是这个顺序,反过来 mhz_media 缺 category、这里建索引就炸;manifest db/migration_036 publication_stage_strict_source 引用 publish_outcome_records"),
    ("py", "db.meijiehezi_db:init_mhz_tables", "mhz_media 等媒介盒子表;v35 ALTER mhz_media(prestart 之后还会再调一次,幂等)"),
    ("sql", "db/migration_008_pricing_llm_first.sql", "system_settings;v35 往里写默认配置(它在 manifest #11,本就每班重放、幂等,这里提前一次)"),
    ("sql", "scripts/migration_v35_factory_inventory_2026_05_26.sql",
     "customer_credit_transactions 等代理库存表;不在 manifest,manifest #9 pricing_dual_ssot 起直接引用它"),
    ("sql", "scripts/migration_v35_w2_2026_05_26.sql", "recharge_orders.order_type 等列;不在 manifest,manifest #10 agent_inventory_bigint_cutover 起引用"),
    ("sql", "scripts/migration_v35_w3_2026_05_26.sql", "agent_factory_agreements;v35 同一天第三份,不在 manifest"),
    ("sql", "scripts/migration_v35_w4_2026_05_26.sql", "binding_disputes / inventory_allocations / inventory_audit_*;v35 同一天第四份,不在 manifest"),
    ("ddl", "CREATE TABLE IF NOT EXISTS _migrations (name text PRIMARY KEY, applied_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP)",
     "_migrations(历史迁移自记账表);仓内没有任何代码建它,形状按生产 schema 快照(name 主键);下一步 migration_04 往里记账"),
    ("sql", "scripts/migration_04_geo_plan_tasks.sql", "geo_plan_tasks;不在 manifest,manifest #47 monitoring_yuanbao_default 起引用"),
    ("py", "db.migration_009_subscription_v2:run_migration",
     "user_social_subscriptions 等订阅表;manifest 里 direct_service_refund_agreements 只在它存在时加两条外键,"
     "而该迁移的精确约束合同(121 条)要求这两条在 —— 缺它就报 exact constraint contract mismatch"),
    ("pycur", "db.brands_schema:ensure_brands_schema", "brands(与生产同形的唯一出口);下一步 migration_001_teams 的 teams.brand_id 外键指向它"),
    ("import", "db.diagnosis_db", "quotes / topics / articles 等写作基表(导入即建,prestart 随后本来也导入它);下一步监测表引用 quotes"),
    ("sql", "db/migration_005_compliance.sql", "quotes.service_days / service_start_date + keyword_compliance_log;不在 manifest,manifest db/migration_028 service_period_ssot 引用 service_days"),
    ("sql", "db/migration_006_effective_rate.sql", "keyword_compliance_log 的后续列;005 的同组下一份,不在 manifest"),
    ("py", "db.monitoring_db:init_monitoring_tables", "client_keywords 等监测表(运行时 init_monitoring_api 也调它);manifest db/migration_043 FROM client_keywords"),
    ("py", "db.publish_records_schema:init_publish_records_table",
     "publish_records(运行时 init_publish_records_table 也调它);manifest db/migration_055 geo_douyin_post_quote_binding 引用"),
    ("py", "db.media_entity_flywheel_db:init_media_entity_flywheel_tables", "媒体实体飞轮表(运行时与下两步同一组、同一顺序调)"),
    ("py", "db.geo_source_signals_db:init_geo_source_signal_tables", "geo_research_source_signals;manifest db/migration_056 geo_douyin_post_batches 引用"),
    ("py", "db.writing_style_flywheel_db:init_writing_style_flywheel_tables", "写作风格飞轮表(运行时同组第三个)"),
    ("sql", "db/migration_001_teams.sql", "teams / team_members / user_notifications;不在 manifest,manifest #106 geo_image_note_contract 起引用 user_notifications"),
    ("ddl", "CREATE TABLE IF NOT EXISTS client_profiles (id text PRIMARY KEY, name text NOT NULL, brand_id integer, industry text, business text, "
            "products text, target_users text, pain_points text, competitors text, persona_positioning text, persona_tone text, "
            "persona_catchphrases text, persona_background text, persona_golden_quotes text, target_platforms text, successful_patterns text, "
            "negative_feedback text, brand_constraints text, is_deleted integer DEFAULT 0, deleted_at timestamp without time zone, "
            "created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP, updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP)",
     "client_profiles 基表;仓内唯一的建表在 SQLite 时代的 db/migrate_v3.py,PG 上生产靠历史建出;列与类型照该建表(生产快照同名同型),"
     "之后的列由运行时代码与迁移自己补;manifest db/migration_041 的报价 schema 守卫要求它是普通永久表"),
    ("sql", "scripts/migration_v36_agent_multi_package_2026_06_06.sql",
     "recharge_orders.override_id、agent_sku_overrides 排序列;不在 manifest,manifest #57 geo_article_closed_loop_v1 起引用"),
]


def is_cold(cur) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NULL", (f"public.{SENTINEL}",))
    return bool(cur.fetchone()[0])


def _sql_body(path: Path) -> str:
    sql = path.read_text(encoding="utf-8")
    sql = re.sub(r"^\s*\\[a-zA-Z_]+.*$", "", sql, flags=re.MULTILINE)
    return re.sub(r"^\s*(BEGIN|COMMIT)\s*;\s*$", "", sql, flags=re.MULTILINE | re.IGNORECASE)


def bootstrap(cur, root: Path, log: logging.Logger | None = None, prefix: str = "") -> bool:
    """冷库 ⇒ 按 BOOTSTRAP 顺序建基表并返回 True;热库 ⇒ 什么都不做,返回 False。任何一步失败向上抛(prestart 非零退出)。"""
    log = log or logger
    if not is_cold(cur):
        log.info(f"{prefix}冷启动引导:哨兵表 {SENTINEL} 已在 ⇒ 不是空库,跳过(生产库走这里,空操作)")
        return False
    log.info(f"{prefix}冷启动引导:空库 ⇒ 按固定顺序建 {len(BOOTSTRAP)} 步基表")
    for kind, target, why in BOOTSTRAP:
        if kind in ("py", "pycur"):
            mod, fn = target.split(":")
            f = getattr(importlib.import_module(mod), fn)
            f(cur) if kind == "pycur" else f()
        elif kind == "sql":
            path = root / target
            if not path.exists():
                raise RuntimeError(f"冷启动引导缺文件:{target}")
            cur.execute(_sql_body(path))
        elif kind == "ddl":
            cur.execute(target)
        elif kind == "import":
            importlib.import_module(target)
        else:
            raise ValueError(f"未知引导步骤种类 {kind!r}")
        log.info(f"{prefix}冷启动引导 ✅ {kind} {target} —— {why}")
    return True


#: 冷启动与生产 schema 的对齐基线(由 scripts/gen_cold_start_parity.py 从生产 schema 快照生成)。
PARITY_SQL = "db/cold_start_parity.sql"


def apply_parity(cur, root: Path, log: logging.Logger | None = None, prefix: str = "") -> None:
    """[E10 · 0913AO] 空库冷启动跑完 manifest 之后,补齐只由历史 SQL / 手工 ALTER 建出来、在役代码读写的表与列。

    调用方(scripts/prestart.py)只在开跑时是空库(本模块 bootstrap 返回 True)时调用 ⇒ 生产库永不执行。
    文件由 scripts/gen_cold_start_parity.py 生成:缺表只收在役代码有 SQL 引用的(DDL 取自生产快照,
    去掉 OWNER / GRANT / COMMENT),缺列全收(ADD COLUMN IF NOT EXISTS)。失败向上抛(prestart 非零退出)。
    """
    log = log or logger
    path = root / PARITY_SQL
    if not path.exists():
        raise RuntimeError(f"冷启动对齐基线缺文件:{PARITY_SQL}")
    cur.execute(_sql_body(path))
    log.info(f"{prefix}冷启动对齐 ✅ {PARITY_SQL}(缺表 / 缺列按生产快照补齐)")

