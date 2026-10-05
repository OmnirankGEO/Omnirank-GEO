# -*- coding: utf-8 -*-
"""诊断资金状态机 throwaway-PG 判别验证(注入 fake billing · 生产同构资金表 · 真 SQL 真约束)。

前置(throwaway PG16 · 绝不指生产):
  1) 起容器:docker run -d --name diag-sm-pg -e POSTGRES_PASSWORD=test -e POSTGRES_USER=geo_admin \
       -e POSTGRES_DB=diag_sm_test -p 5434:5432 postgres:16
  2) 建辅助表(diagnosis_records[含生产 created_at] / point_freezes[id,user_id,task_ref,status,amount_total,
       amount_bonus/commission/paid] / point_transactions)，且三张 V3.5 资金表必须直接采用生产 DDL:
       customer_agent_credit_wallets + customer_credit_transactions = migration_v35_factory_inventory_2026_05_26.sql;
       customer_credit_freezes = migration_v35_v7_fund_chain_2026_06_08.sql。脚本会完整反查列/NULL/CHECK/FK/
       convalidated/partial UNIQUE，并做移除约束篡改测试；简化 schema 会立即失败。
  3) 跑迁移:psql < scripts/migration_diagnosis_runs_2026_07_13.sql
     (迁移会把 customer_credit_transactions.source CHECK 加 diagnosis_delivery_refund · v35 退款流水依赖)
运行:DATABASE_URL=postgresql://geo_admin:test@localhost:5434/diag_sm_test \
      REDIS_URL=redis://localhost:6399/0 python scripts/verify_diagnosis_runs_statemachine.py
覆盖:HC1 幂等(20 并发恰 1 run)/ R0 五分支 / R1 commit+可见性同事务 / release+withheld / exempt 零 billing /
     R2 ambiguous / R3 attempts≥5 / 幂等冲突守卫 / reaped CAS miss / sweeper 判死+收尸 / verify×2 /
     chk_freeze_handle 约束 / §3.7 人工处置 / 对抗审核修复净增量(#5/#6/#7/#13/#5-fu)。
"""
import os, sys, asyncio, types, json
from pathlib import Path

DB = os.environ["DATABASE_URL"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- 注入 fake middleware.billing(避免 import 真 billing · 受控返回)----
_billing_cfg = {"commit": {"success": True}, "release": {"success": True}, "commit_calls": [], "release_calls": []}
fake_billing = types.ModuleType("middleware.billing")
async def _fake_commit(**kw):
    _billing_cfg["commit_calls"].append(kw)
    return dict(_billing_cfg["commit"])
async def _fake_release(**kw):
    _billing_cfg["release_calls"].append(kw)
    return dict(_billing_cfg["release"])
fake_billing.commit_freeze = _fake_commit
fake_billing.release_freeze = _fake_release
sys.modules["middleware.billing"] = fake_billing
sys.modules.setdefault("middleware", types.ModuleType("middleware"))

# ---- 注入 fake cache.progress_bus(reconciler P0-1c 用 · 受控快照 + 记录 republish)----
_pb_cfg = {"snapshot": None, "published": []}
fake_pb = types.ModuleType("cache.progress_bus")
def _pb_get_snapshot_sync(sid):
    return _pb_cfg["snapshot"]
async def _pb_publish(sid, payload, terminal=False):
    _pb_cfg["published"].append({"sid": sid, "payload": payload, "terminal": terminal})
    return 1
fake_pb.get_snapshot_sync = _pb_get_snapshot_sync
fake_pb.publish = _pb_publish
sys.modules["cache.progress_bus"] = fake_pb

# [返工5 复审 P1] 不再注入 fake refund_work_order_db —— confirm_refund 已改为 **run-bound diagnosis_refund_records**,
#   彻底不引用 recharge_orders/refund_work_orders(见源码锁断言)。

import psycopg2, psycopg2.extras
from services import diagnosis_runs as dr

CHECKS = []
FAILS = []
def check(name, cond):
    CHECKS.append(name)
    print(("  OK  " if cond else " FAIL ") + name)
    if not cond:
        FAILS.append(name)

def q(sql, args=()):
    conn = psycopg2.connect(DB); conn.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        c = conn.cursor(); c.execute(sql, args); conn.commit()
        try: return c.fetchall()
        except Exception: return []
    finally: conn.close()

def _require_production_v35_fund_schema(record_checks=True):
    """硬门:三张资金表必须精确匹配生产列/默认值及完整已验证约束。"""
    required = {
        ("customer_credit_freezes", "id"): ("bigint", "NO", "nextval('customer_credit_freezes_id_seq'::regclass)"),
        ("customer_credit_freezes", "customer_user_id"): ("integer", "NO", None),
        ("customer_credit_freezes", "agent_user_id"): ("integer", "NO", None),
        ("customer_credit_freezes", "feature_code"): ("text", "NO", None),
        ("customer_credit_freezes", "amount_total"): ("integer", "NO", None),
        ("customer_credit_freezes", "amount_tool"): ("integer", "NO", "0"),
        ("customer_credit_freezes", "amount_publish"): ("integer", "NO", "0"),
        ("customer_credit_freezes", "amount_bonus"): ("integer", "NO", "0"),
        ("customer_credit_freezes", "status"): ("text", "NO", "'frozen'::text"),
        ("customer_credit_freezes", "task_ref"): ("text", "YES", None),
        ("customer_credit_freezes", "brand_id"): ("integer", "YES", None),
        ("customer_credit_freezes", "reason"): ("text", "YES", None),
        ("customer_credit_freezes", "created_at"): ("timestamp without time zone", "YES", "now()"),
        ("customer_credit_freezes", "committed_at"): ("timestamp without time zone", "YES", None),
        ("customer_credit_freezes", "released_at"): ("timestamp without time zone", "YES", None),
        ("customer_agent_credit_wallets", "customer_user_id"): ("integer", "NO", None),
        ("customer_agent_credit_wallets", "agent_user_id"): ("integer", "NO", None),
        ("customer_agent_credit_wallets", "tool_credit_points"): ("integer", "NO", "0"),
        ("customer_agent_credit_wallets", "publish_credit_points"): ("integer", "NO", "0"),
        ("customer_agent_credit_wallets", "bonus_credit_points"): ("integer", "NO", "0"),
        ("customer_agent_credit_wallets", "total_purchased_points"): ("bigint", "NO", "0"),
        ("customer_agent_credit_wallets", "total_consumed_points"): ("bigint", "NO", "0"),
        ("customer_agent_credit_wallets", "updated_at"): ("timestamp without time zone", "YES", "now()"),
        ("customer_credit_transactions", "id"): ("bigint", "NO", "nextval('customer_credit_transactions_id_seq'::regclass)"),
        ("customer_credit_transactions", "customer_user_id"): ("integer", "NO", None),
        ("customer_credit_transactions", "agent_user_id"): ("integer", "NO", None),
        ("customer_credit_transactions", "type"): ("text", "NO", None),
        ("customer_credit_transactions", "pool"): ("text", "NO", None),
        ("customer_credit_transactions", "points"): ("integer", "NO", None),
        ("customer_credit_transactions", "balance_tool_after"): ("integer", "NO", None),
        ("customer_credit_transactions", "balance_publish_after"): ("integer", "NO", None),
        ("customer_credit_transactions", "balance_bonus_after"): ("integer", "NO", None),
        ("customer_credit_transactions", "feature_code"): ("text", "YES", None),
        ("customer_credit_transactions", "related_order_id"): ("text", "YES", None),
        ("customer_credit_transactions", "source"): ("text", "YES", None),
        ("customer_credit_transactions", "description"): ("text", "YES", None),
        ("customer_credit_transactions", "created_at"): ("timestamp without time zone", "YES", "now()"),
    }
    rows = q(
        "SELECT table_name,column_name,data_type,is_nullable,column_default FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name = ANY(%s)",
        (["customer_credit_freezes", "customer_agent_credit_wallets", "customer_credit_transactions"],),
    )
    got = {
        (r["table_name"], r["column_name"]):
        (r["data_type"], r["is_nullable"], r.get("column_default"))
        for r in rows
    }
    columns_ok = got == required
    if record_checks:
        check("测试夹具使用生产真实 V3.5 三表完整列/NULL/DEFAULT 语义", columns_ok)

    constraints = q(
        "SELECT rel.relname AS table_name, con.conname, con.contype, con.convalidated, "
        "pg_get_constraintdef(con.oid) AS definition FROM pg_constraint con "
        "JOIN pg_class rel ON rel.oid=con.conrelid "
        "JOIN pg_namespace ns ON ns.oid=rel.relnamespace "
        "WHERE ns.nspname='public' AND rel.relname = ANY(%s)",
        (["customer_credit_freezes", "customer_agent_credit_wallets", "customer_credit_transactions"],),
    )
    def _canon_constraint(definition):
        # throwaway 与生产均为 PG16；直接比较 pg_get_constraintdef 的稳定输出。
        # 禁止 lower/去空白：它们会误改单引号内的 source 字面量，造成语义不同却“相等”的假绿。
        return str(definition)

    expected_constraints = {
        ("customer_agent_credit_wallets", "customer_agent_credit_wallets_pkey"):
            ("p", "PRIMARY KEY (customer_user_id)"),
        ("customer_agent_credit_wallets", "customer_agent_credit_wallets_tool_credit_points_check"):
            ("c", "CHECK ((tool_credit_points >= 0))"),
        ("customer_agent_credit_wallets", "customer_agent_credit_wallets_publish_credit_points_check"):
            ("c", "CHECK ((publish_credit_points >= 0))"),
        ("customer_agent_credit_wallets", "customer_agent_credit_wallets_bonus_credit_points_check"):
            ("c", "CHECK ((bonus_credit_points >= 0))"),
        ("customer_credit_freezes", "customer_credit_freezes_pkey"):
            ("p", "PRIMARY KEY (id)"),
        ("customer_credit_freezes", "customer_credit_freezes_customer_user_id_fkey"):
            ("f", "FOREIGN KEY (customer_user_id) REFERENCES users(id)"),
        ("customer_credit_freezes", "customer_credit_freezes_agent_user_id_fkey"):
            ("f", "FOREIGN KEY (agent_user_id) REFERENCES users(id)"),
        ("customer_credit_freezes", "customer_credit_freezes_amount_total_check"):
            ("c", "CHECK ((amount_total > 0))"),
        ("customer_credit_freezes", "customer_credit_freezes_status_check"):
            ("c", "CHECK ((status = ANY (ARRAY['frozen'::text, 'committed'::text, 'released'::text])))"),
        ("customer_credit_freezes", "chk_ccf_pool_sum"):
            ("c", "CHECK ((((amount_tool + amount_publish) + amount_bonus) = amount_total))"),
        ("customer_credit_transactions", "customer_credit_transactions_pkey"):
            ("p", "PRIMARY KEY (id)"),
        ("customer_credit_transactions", "customer_credit_transactions_type_check"):
            ("c", "CHECK ((type = ANY (ARRAY['allocate'::text, 'consume'::text, 'refund'::text, 'revoke'::text])))"),
        ("customer_credit_transactions", "customer_credit_transactions_pool_check"):
            ("c", "CHECK ((pool = ANY (ARRAY['tool'::text, 'publish'::text, 'bonus'::text])))"),
        ("customer_credit_transactions", "customer_credit_transactions_source_check"):
            ("c", "CHECK ((source = ANY (ARRAY['online_payment'::text, 'offline_allocation'::text, 'admin_adjust'::text, 'tool_consume'::text, 'refund_revoke'::text, 'agent_rebate'::text, 'tool_fail_refund'::text, 'diagnosis_delivery_refund'::text])))"),
        ("customer_credit_transactions", "customer_credit_transactions_consume_refund_points_sign_check"):
            ("c", "CHECK ((((type <> 'consume'::text) OR (points < 0)) AND ((type <> 'refund'::text) OR (points > 0))))"),
    }
    expected_constraints = {
        key: (contype, True, definition)
        for key, (contype, definition) in expected_constraints.items()
    }
    got_constraints = {
        (r["table_name"], r["conname"]):
        (r["contype"], r["convalidated"] is True, _canon_constraint(r["definition"]))
        for r in constraints
    }
    constraints_ok = got_constraints == expected_constraints
    if record_checks:
        check("测试夹具生产 PK/CHECK/FK 完整具名白名单精确匹配且 convalidated=true", constraints_ok)

    indexes = q(
        "SELECT tbl.relname AS tablename, idxrel.relname AS indexname, "
        "pg_get_indexdef(idx.indexrelid) AS indexdef, idx.indisunique, idx.indisvalid, "
        "idx.indisready, idx.indislive FROM pg_index idx "
        "JOIN pg_class idxrel ON idxrel.oid=idx.indexrelid "
        "JOIN pg_class tbl ON tbl.oid=idx.indrelid "
        "JOIN pg_namespace ns ON ns.oid=tbl.relnamespace "
        "WHERE ns.nspname='public' AND tbl.relname = ANY(%s)",
        (["customer_credit_freezes", "customer_agent_credit_wallets", "customer_credit_transactions"],),
    )
    expected_indexes = {
        ("customer_agent_credit_wallets", "customer_agent_credit_wallets_pkey"):
            "CREATE UNIQUE INDEX customer_agent_credit_wallets_pkey ON public.customer_agent_credit_wallets USING btree (customer_user_id)",
        ("customer_agent_credit_wallets", "idx_customer_agent_credit_agent"):
            "CREATE INDEX idx_customer_agent_credit_agent ON public.customer_agent_credit_wallets USING btree (agent_user_id)",
        ("customer_credit_freezes", "customer_credit_freezes_pkey"):
            "CREATE UNIQUE INDEX customer_credit_freezes_pkey ON public.customer_credit_freezes USING btree (id)",
        ("customer_credit_freezes", "idx_ccf_customer_status"):
            "CREATE INDEX idx_ccf_customer_status ON public.customer_credit_freezes USING btree (customer_user_id, status)",
        ("customer_credit_freezes", "idx_ccf_status_created"):
            "CREATE INDEX idx_ccf_status_created ON public.customer_credit_freezes USING btree (status, created_at)",
        ("customer_credit_freezes", "idx_ccf_task_ref"):
            "CREATE INDEX idx_ccf_task_ref ON public.customer_credit_freezes USING btree (task_ref) WHERE (task_ref IS NOT NULL)",
        ("customer_credit_freezes", "uq_ccf_active_task"):
            "CREATE UNIQUE INDEX uq_ccf_active_task ON public.customer_credit_freezes USING btree (customer_user_id, task_ref) WHERE ((task_ref IS NOT NULL) AND (status = 'frozen'::text))",
        ("customer_credit_transactions", "customer_credit_transactions_pkey"):
            "CREATE UNIQUE INDEX customer_credit_transactions_pkey ON public.customer_credit_transactions USING btree (id)",
        ("customer_credit_transactions", "idx_customer_credit_tx_customer"):
            "CREATE INDEX idx_customer_credit_tx_customer ON public.customer_credit_transactions USING btree (customer_user_id, created_at DESC)",
        ("customer_credit_transactions", "idx_customer_credit_tx_order"):
            "CREATE INDEX idx_customer_credit_tx_order ON public.customer_credit_transactions USING btree (related_order_id)",
    }
    expected_indexes = {
        key: (
            definition,
            key[1].endswith("_pkey") or key[1] == "uq_ccf_active_task",
            True, True, True,
        )
        for key, definition in expected_indexes.items()
    }
    got_indexes = {
        (r["tablename"], r["indexname"]): (
            str(r["indexdef"]), r["indisunique"] is True, r["indisvalid"] is True,
            r["indisready"] is True, r["indislive"] is True,
        )
        for r in indexes
    }
    index_ok = got_indexes == expected_indexes
    if record_checks:
        check("测试夹具生产 10 个索引（含 partial UNIQUE 谓词）PG16 原文精确匹配", index_ok)
    if not (columns_ok and constraints_ok and index_ok):
        column_diff = {
            str(k): {"expected": v, "actual": got.get(k)}
            for k, v in required.items() if got.get(k) != v
        }
        for extra in set(got) - set(required):
            column_diff[str(extra)] = {"expected": None, "actual": got[extra]}
        constraint_diff = {
            str(k): {"expected": v, "actual": got_constraints.get(k)}
            for k, v in expected_constraints.items() if got_constraints.get(k) != v
        }
        for extra in set(got_constraints) - set(expected_constraints):
            constraint_diff[str(extra)] = {"expected": None, "actual": got_constraints[extra]}
        index_diff = {
            str(k): {"expected": v, "actual": got_indexes.get(k)}
            for k, v in expected_indexes.items() if got_indexes.get(k) != v
        }
        for extra in set(got_indexes) - set(expected_indexes):
            index_diff[str(extra)] = {"expected": None, "actual": got_indexes[extra]}
        raise RuntimeError(
            f"throwaway V3.5 fund schema 与生产不一致:columns={column_diff},"
            f"constraints={constraint_diff},indexes={index_diff}")

def _create_v35_freeze_table_production_shape():
    """按 migration_v35_v7_fund_chain 重建被缺表测试删除的生产形状(仅 throwaway PG)。"""
    q("""
        CREATE TABLE customer_credit_freezes (
            id BIGSERIAL PRIMARY KEY,
            customer_user_id INTEGER NOT NULL REFERENCES users(id),
            agent_user_id INTEGER NOT NULL REFERENCES users(id),
            feature_code TEXT NOT NULL,
            amount_total INTEGER NOT NULL CHECK (amount_total > 0),
            amount_tool INTEGER NOT NULL DEFAULT 0,
            amount_publish INTEGER NOT NULL DEFAULT 0,
            amount_bonus INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'frozen' CHECK (status IN ('frozen','committed','released')),
            task_ref TEXT,
            brand_id INTEGER,
            reason TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            committed_at TIMESTAMP,
            released_at TIMESTAMP,
            CONSTRAINT chk_ccf_pool_sum CHECK (amount_tool + amount_publish + amount_bonus = amount_total)
        )
    """)
    q("CREATE INDEX idx_ccf_customer_status ON customer_credit_freezes(customer_user_id,status)")
    q("CREATE INDEX idx_ccf_task_ref ON customer_credit_freezes(task_ref) WHERE task_ref IS NOT NULL")
    q("CREATE INDEX idx_ccf_status_created ON customer_credit_freezes(status,created_at)")
    q("CREATE UNIQUE INDEX uq_ccf_active_task ON customer_credit_freezes(customer_user_id,task_ref) "
      "WHERE task_ref IS NOT NULL AND status='frozen'")

def _insert_v35_freeze(customer, agent, task_ref, status="frozen", tool=1, publish=0, bonus=0):
    total = int(tool) + int(publish) + int(bonus)
    if total <= 0:
        raise ValueError("V3.5 freeze fixture 总额必须 > 0")
    q("INSERT INTO users(id) VALUES (%s),(%s) ON CONFLICT (id) DO NOTHING", (customer, agent))
    rows = q(
        "INSERT INTO customer_credit_freezes "
        "(customer_user_id,agent_user_id,feature_code,task_ref,status,amount_total,amount_tool,amount_publish,amount_bonus) "
        "VALUES (%s,%s,'geo_diagnosis',%s,%s,%s,%s,%s,%s) RETURNING id",
        (customer, agent, task_ref, status, total, tool, publish, bonus),
    )
    return int(rows[0]["id"])

def _insert_credit_tx(customer, agent, type_, pool, points, related_order_id, source=None):
    source = source or ("tool_consume" if type_ == "consume" else "tool_fail_refund")
    q("INSERT INTO users(id) VALUES (%s),(%s) ON CONFLICT (id) DO NOTHING", (customer, agent))
    rows = q(
        "INSERT INTO customer_credit_transactions "
        "(customer_user_id,agent_user_id,type,pool,points,balance_tool_after,balance_publish_after,"
        "balance_bonus_after,related_order_id,source) VALUES (%s,%s,%s,%s,%s,0,0,0,%s,%s) RETURNING id",
        (customer, agent, type_, pool, points, str(related_order_id), source),
    )
    return int(rows[0]["id"])

_require_production_v35_fund_schema()

# 自检有齿:临时移除资金池守恒约束必须被 schema gate 捕获；恢复并 VALIDATE 后才继续。
q("ALTER TABLE customer_credit_freezes DROP CONSTRAINT chk_ccf_pool_sum")
_schema_tamper_detected = False
try:
    _require_production_v35_fund_schema(record_checks=False)
except RuntimeError:
    _schema_tamper_detected = True
finally:
    q("ALTER TABLE customer_credit_freezes ADD CONSTRAINT chk_ccf_pool_sum "
      "CHECK (amount_tool + amount_publish + amount_bonus = amount_total) NOT VALID")
    q("ALTER TABLE customer_credit_freezes VALIDATE CONSTRAINT chk_ccf_pool_sum")
check("测试夹具 schema 自检可检出约束篡改(非装饰性假门)", _schema_tamper_detected)
_require_production_v35_fund_schema()

# partial UNIQUE 的列、谓词和字符串字面量同样逐字符精确；大写 FROZEN 会让真实小写 frozen 行失去幂等保护。
q("DROP INDEX uq_ccf_active_task")
q("CREATE UNIQUE INDEX uq_ccf_active_task ON customer_credit_freezes(customer_user_id,task_ref) "
  "WHERE task_ref IS NOT NULL AND status='FROZEN'")
_index_predicate_tamper_detected = False
try:
    _require_production_v35_fund_schema(record_checks=False)
except RuntimeError:
    _index_predicate_tamper_detected = True
finally:
    q("DROP INDEX uq_ccf_active_task")
    q("CREATE UNIQUE INDEX uq_ccf_active_task ON customer_credit_freezes(customer_user_id,task_ref) "
      "WHERE task_ref IS NOT NULL AND status='frozen'")
check("测试夹具 schema 自检可检出 partial UNIQUE 谓词字面量篡改", _index_predicate_tamper_detected)
_require_production_v35_fund_schema()

# indexdef 不足以证明索引可用：invalid 壳仍会出现在 pg_indexes，必须核 pg_index 状态位。
q("UPDATE pg_index SET indisvalid=FALSE WHERE indexrelid='uq_ccf_active_task'::regclass")
_invalid_index_tamper_detected = False
try:
    _require_production_v35_fund_schema(record_checks=False)
except RuntimeError:
    _invalid_index_tamper_detected = True
finally:
    q("UPDATE pg_index SET indisvalid=TRUE WHERE indexrelid='uq_ccf_active_task'::regclass")
check("测试夹具 schema 自检可检出定义相同但 indisvalid=false 的索引壳", _invalid_index_tamper_detected)
_require_production_v35_fund_schema()

# 行为判别：同客户+同 task_ref 的第二笔 frozen 必须被 partial UNIQUE 真正拒绝。
_unique_probe_customer = 99001
_unique_probe_task = "diag_schema_unique_probe"
_insert_v35_freeze(_unique_probe_customer, 99002, _unique_probe_task, "frozen", tool=1)
_duplicate_frozen_rejected = False
try:
    _insert_v35_freeze(_unique_probe_customer, 99002, _unique_probe_task, "frozen", tool=1)
except psycopg2.errors.UniqueViolation:
    _duplicate_frozen_rejected = True
finally:
    q("DELETE FROM customer_credit_freezes WHERE customer_user_id=%s", (_unique_probe_customer,))
check("测试夹具 partial UNIQUE 行为有齿:第二笔同客户同 task_ref frozen 触发 UniqueViolation",
      _duplicate_frozen_rejected)

# 默认值同样是资金语义：错误 DEFAULT 会让 seed 省略列时假绿，必须被硬门检出。
q("ALTER TABLE customer_agent_credit_wallets ALTER COLUMN total_consumed_points SET DEFAULT 999")
_default_tamper_detected = False
try:
    _require_production_v35_fund_schema(record_checks=False)
except RuntimeError:
    _default_tamper_detected = True
finally:
    q("ALTER TABLE customer_agent_credit_wallets ALTER COLUMN total_consumed_points SET DEFAULT 0")
check("测试夹具 schema 自检可检出资金累计列 DEFAULT 篡改", _default_tamper_detected)
_require_production_v35_fund_schema()

# 字符串字面量必须逐字符精确：未被本脚本业务用例使用的 source 大小写漂移也要被 schema 门捕获。
q("DELETE FROM customer_credit_transactions")
q("ALTER TABLE customer_credit_transactions DROP CONSTRAINT customer_credit_transactions_source_check")
q("ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_source_check CHECK (source IN ("
  "'ONLINE_PAYMENT','offline_allocation','admin_adjust','tool_consume','refund_revoke',"
  "'agent_rebate','tool_fail_refund','diagnosis_delivery_refund'))")
_source_literal_tamper_detected = False
try:
    _require_production_v35_fund_schema(record_checks=False)
except RuntimeError:
    _source_literal_tamper_detected = True
finally:
    q("ALTER TABLE customer_credit_transactions DROP CONSTRAINT customer_credit_transactions_source_check")
    q("ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_source_check CHECK (source IN ("
      "'online_payment','offline_allocation','admin_adjust','tool_consume','refund_revoke',"
      "'agent_rebate','tool_fail_refund','diagnosis_delivery_refund')) NOT VALID")
    q("ALTER TABLE customer_credit_transactions VALIDATE CONSTRAINT customer_credit_transactions_source_check")
check("测试夹具 schema 自检可检出未使用 source 字面量大小写篡改", _source_literal_tamper_detected)
_require_production_v35_fund_schema()

# 额外 NOT VALID CHECK 仍会约束新写入，不能因 convalidated=false 被 gate 预过滤后隐身。
q("ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_extra_not_valid_check "
  "CHECK (source <> 'ONLINE_PAYMENT') NOT VALID")
_extra_unvalidated_tamper_detected = False
try:
    _require_production_v35_fund_schema(record_checks=False)
except RuntimeError:
    _extra_unvalidated_tamper_detected = True
finally:
    q("ALTER TABLE customer_credit_transactions DROP CONSTRAINT customer_credit_transactions_extra_not_valid_check")
check("测试夹具 schema 自检可检出额外 NOT VALID 约束", _extra_unvalidated_tamper_detected)
_require_production_v35_fund_schema()

# 精确约束定义判别：保留相同 token 但破坏布尔分组也必须失败，不能只凭名称/弱片段放行。
q("DELETE FROM customer_credit_transactions")
q("ALTER TABLE customer_credit_transactions DROP CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check")
q("ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check "
  "CHECK (type <> 'consume' OR points < 0 AND type <> 'refund' OR points > 0)")
_sign_grouping_tamper_detected = False
try:
    _require_production_v35_fund_schema(record_checks=False)
except RuntimeError:
    _sign_grouping_tamper_detected = True
finally:
    q("ALTER TABLE customer_credit_transactions DROP CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check")
    q("ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check "
      "CHECK ((type <> 'consume' OR points < 0) AND (type <> 'refund' OR points > 0)) NOT VALID")
    q("ALTER TABLE customer_credit_transactions VALIDATE CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check")
check("测试夹具 schema 自检可检出同名同 token 但布尔分组错误的符号 CHECK", _sign_grouping_tamper_detected)
_require_production_v35_fund_schema()

def wipe():
    q("DELETE FROM diagnosis_refund_records")   # [返工5 复审] 先删子表(FK→diagnosis_runs)
    q("DELETE FROM diagnosis_runs"); q("DELETE FROM diagnosis_records")
    q("DELETE FROM diagnosis_settlement_audit")
    q("DELETE FROM point_freezes"); q("DELETE FROM customer_credit_freezes")
    q("DELETE FROM point_transactions"); q("DELETE FROM customer_credit_transactions")   # [返工5 复审二] 钱包流水
    q("DELETE FROM customer_agent_credit_wallets")   # [P1-2] 客户额度钱包(退款到原池 · 每用例必清防余额串)

def mk_record(session_id, brand_id=None, run_token=None):
    q("INSERT INTO diagnosis_records (session_id, brand_name, brand_id, run_token, result_visibility, total_score) "
      "VALUES (%s,'B',%s,%s,'pending',88.0) ON CONFLICT (session_id) DO NOTHING",
      (session_id, brand_id, run_token))

# ============================ HC1 admission ============================
print("\n== HC1 admission ==")
wipe()
a1 = dr.admit_run(10, "req-1", 100, "sid-1", dr.mint_run_token(), "paid")
check("首请求 admitted", a1.admitted and a1.reason == "admitted")
rt1 = q("SELECT run_token FROM diagnosis_runs WHERE session_id='sid-1'")[0]["run_token"]
a2 = dr.admit_run(10, "req-1", 100, "sid-2", dr.mint_run_token(), "paid")
check("同 client_request_id → idempotent_retry 返既有", (not a2.admitted) and a2.reason == "idempotent_retry"
      and a2.run and a2.run["run_token"] == rt1)
a3 = dr.admit_run(10, "req-2", 100, "sid-3", dr.mint_run_token(), "paid")
check("同 brand 不同 request(既有活跃)→ brand_active 返活跃", (not a3.admitted) and a3.reason == "brand_active"
      and a3.run and a3.run["run_token"] == rt1)
# 20 并发同 request_id → 恰 1 run(唯一约束)
wipe()
import concurrent.futures as cf
def _try_admit(i): return dr.admit_run(11, "reqX", 101, f"c{i}", dr.mint_run_token(), "paid").admitted
with cf.ThreadPoolExecutor(max_workers=20) as ex:
    got = list(ex.map(_try_admit, range(20)))
n_runs = q("SELECT count(*) n FROM diagnosis_runs WHERE owner_user_id=11")[0]["n"]
check("20 并发同 request_id → 恰 1 admitted", sum(1 for x in got if x) == 1)
check("20 并发同 request_id → 恰 1 run 行", n_runs == 1)

# ============================ R0 activate ============================
print("\n== R0 activate_after_freeze ==")
wipe()
rt = dr.mint_run_token()
dr.admit_run(10, "r-paid", 100, "s-paid", rt, "paid")
st = dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 555, "legacy", "paid"))
row = q("SELECT run_status, freeze_id, freeze_backend FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("付费成功 → running + Handle 回填", st == "running" and row["run_status"] == "running"
      and row["freeze_id"] == 555 and row["freeze_backend"] == "legacy")
rt = dr.mint_run_token(); dr.admit_run(10, "r-cancel", 102, "s-cancel", rt, "paid")
st = dr.activate_after_freeze(rt, 10, dr.FreezeClassification("cancelled", None, None, "paid"))
check("确定性失败 → cancelled", st == "cancelled")
rt = dr.mint_run_token(); dr.admit_run(10, "r-unknown", 103, "s-unknown", rt, "paid")
st = dr.activate_after_freeze(rt, 10, dr.FreezeClassification("unknown", None, None, "paid"))
check("未知 + 双表查空 → 保持 pending_freeze(禁首次 cancelled)", st == "pending_freeze")
# 未知但双表有冻结 → release_pending
rt = dr.mint_run_token(); dr.admit_run(10, "r-unk2", 104, "s-unk2", rt, "paid")
q("INSERT INTO point_freezes (user_id, task_ref, status, amount_total) VALUES (10,%s,'frozen',650)", (dr.freeze_task_ref(rt),))
st = dr.activate_after_freeze(rt, 10, dr.FreezeClassification("unknown", None, None, "paid"))
row = q("SELECT run_status, freeze_backend FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("未知 + 双表定位到冻结 → release_pending + backend=legacy", st == "release_pending" and row["freeze_backend"] == "legacy")

# ============================ R1 commit 成功 + 可见性同事务 ============================
print("\n== R1 commit 成功 + 可见性 published(同事务)==")
wipe()
_billing_cfg["commit"] = {"success": True}; _billing_cfg["commit_calls"] = []
rt = dr.mint_run_token(); mk_record("s-c1", 100, rt)
dr.admit_run(10, "r-c1", 100, "s-c1", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 700, "legacy", "paid"))
out = asyncio.run(dr.commit_run(rt, {"type": "complete", "done": True, "message": "ok"}))
row = q("SELECT run_status, final_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-c1'")[0]["result_visibility"]
call = _billing_cfg["commit_calls"][-1]
check("commit → committed", out["ok"] and row["run_status"] == "committed")
check("可见性同事务 published", vis == "published")
check("final_snapshot 落库", row["final_snapshot_jsonb"] is not None)
check("commit 四参全传(freeze_id/task_ref/user_id/freeze_table)",
      call.get("freeze_id") == 700 and call.get("task_ref") == dr.freeze_task_ref(rt)
      and call.get("user_id") == 10 and call.get("freeze_table") == "legacy")

# ============================ 失败 release → withheld ============================
print("\n== release 成功 + 可见性 withheld ==")
_billing_cfg["release"] = {"success": True}; _billing_cfg["release_calls"] = []
rt = dr.mint_run_token(); mk_record("s-r1", 100, rt)
dr.admit_run(10, "r-r1", 105, "s-r1", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 701, "v35", "paid"))
out = asyncio.run(dr.release_run(rt, "task failed", {"type": "error", "done": True}))
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-r1'")[0]["result_visibility"]
check("release → released", out["ok"] and row["run_status"] == "released")
check("可见性 withheld", vis == "withheld")

# ============================ exempt 无 billing ============================
print("\n== exempt 双路零 billing ==")
_billing_cfg["commit_calls"] = []; _billing_cfg["release_calls"] = []
rt = dr.mint_run_token(); mk_record("s-ex", 100, rt)
dr.admit_run(9, "r-ex", 106, "s-ex", rt, "exempt")
dr.activate_after_freeze(rt, 9, dr.FreezeClassification("exempt", None, None, "exempt"))
out = asyncio.run(dr.commit_run(rt, {"done": True}))
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-ex'")[0]["result_visibility"]
check("exempt commit → completed_exempt + published + 零 billing 调用",
      out["ok"] and row["run_status"] == "completed_exempt" and vis == "published"
      and len(_billing_cfg["commit_calls"]) == 0)

# ============================ R2 ambiguous → settlement_manual ============================
print("\n== R2 ambiguous → settlement_manual ==")
_billing_cfg["commit"] = {"success": False, "ambiguous": True}
rt = dr.mint_run_token(); dr.admit_run(10, "r-amb", 107, "s-amb", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 702, "legacy", "paid"))
out = asyncio.run(dr.commit_run(rt, {"done": True}))
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("ambiguous → settlement_manual", row["run_status"] == "settlement_manual")

# ============================ R3 commit 5× fail → settlement_manual ============================
print("\n== R3 commit attempts>=5 → settlement_manual ==")
_billing_cfg["commit"] = {"success": False, "reason": "boom"}
rt = dr.mint_run_token(); dr.admit_run(10, "r-r3", 108, "s-r3", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 703, "legacy", "paid"))
asyncio.run(dr.commit_run(rt, {"done": True}))   # CAS running→commit_pending + 第 1 次失败
for _ in range(6):
    # 直接推 _do_settlement(已在 commit_pending)
    asyncio.run(dr._do_settlement(rt, "commit", {"done": True}))
row = q("SELECT run_status, settlement_attempts FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("commit 连续失败 attempts>=5 → settlement_manual", row["run_status"] == "settlement_manual" and row["settlement_attempts"] >= 5)

# ============================ 幂等冲突守卫:想 commit 但已 released ============================
print("\n== 幂等冲突守卫(想 commit 但 billing 幂等返回 released)==")
_billing_cfg["commit"] = {"success": True, "idempotent": True, "status": "released"}
rt = dr.mint_run_token(); mk_record("s-idem", 100, rt); dr.admit_run(10, "r-idem", 109, "s-idem", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 704, "legacy", "paid"))
out = asyncio.run(dr.commit_run(rt, {"done": True}))
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-idem'")[0]["result_visibility"]
check("想 commit 但已 released → settlement_manual(不伪造 committed)", row["run_status"] == "settlement_manual")
check("幂等冲突下产物**不**发布(仍 pending · 不白拿)", vis == "pending")

# ============================ commit_run 遇 reaped(running 已变)→ CAS miss 不 committed ============================
print("\n== commit_run 遇 reaped(不在 running)→ 放弃 commit ==")
_billing_cfg["commit"] = {"success": True}
rt = dr.mint_run_token(); dr.admit_run(10, "r-reap", 110, "s-reap", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 705, "legacy", "paid"))
q("UPDATE diagnosis_runs SET run_status='release_pending' WHERE run_token=%s", (rt,))  # 模拟被 reaper 抢走
out = asyncio.run(dr.commit_run(rt, {"done": True}))
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("running 已被 reaper 改 → commit_run CAS miss 放弃(不 committed)",
      (not out["ok"]) and row["run_status"] == "release_pending")

# ============================ sweeper:判死 running → release_pending ============================
print("\n== sweeper 判死 + 收尸 ==")
wipe()
_billing_cfg["release"] = {"success": True}
rt = dr.mint_run_token(); dr.admit_run(10, "r-dead", 111, "s-dead", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 706, "legacy", "paid"))
q("UPDATE diagnosis_runs SET heartbeat_at = NOW() - INTERVAL '10 minutes' WHERE run_token=%s", (rt,))
q("INSERT INTO point_freezes (user_id, task_ref, status, amount_total) VALUES (10,%s,'frozen',650)", (dr.freeze_task_ref(rt),))
stats = asyncio.run(dr.run_diagnosis_sweep())
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("running 心跳超 5min → 判死并结算(released 或 release_pending)",
      row["run_status"] in ("released", "release_pending") and stats["reaped_running"] >= 1)

# pending_freeze 收尸:双表查空 verify×2(间隔≥30s)→ cancelled_no_freeze
print("\n== pending_freeze 收尸 verify×2 → cancelled_no_freeze ==")
rt = dr.mint_run_token(); dr.admit_run(10, "r-corpse", 112, "s-corpse", rt, "paid")
# 无任何冻结记录 · pending_freeze 超 10min
q("UPDATE diagnosis_runs SET run_status='pending_freeze', status_changed_at = NOW() - INTERVAL '20 minutes' WHERE run_token=%s", (rt,))
asyncio.run(dr.run_diagnosis_sweep())   # 第 1 次查空 → verify_empty_count=1
q("UPDATE diagnosis_runs SET last_verify_at = NOW() - INTERVAL '40 seconds' WHERE run_token=%s", (rt,))  # 拉开≥30s
asyncio.run(dr.run_diagnosis_sweep())   # 第 2 次查空 → cancelled_no_freeze
row = q("SELECT run_status, verify_empty_count FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("双表查空连续 2 次(间隔≥30s)→ cancelled_no_freeze", row["run_status"] == "cancelled_no_freeze")

# 首次查空绝不 cancelled(单次不够)
rt = dr.mint_run_token(); dr.admit_run(10, "r-corpse2", 113, "s-corpse2", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='pending_freeze', status_changed_at = NOW() - INTERVAL '20 minutes' WHERE run_token=%s", (rt,))
asyncio.run(dr.run_diagnosis_sweep())   # 仅第 1 次
row = q("SELECT run_status, verify_empty_count FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("首次查空 → 仍 pending_freeze(禁首次 cancelled)", row["run_status"] == "pending_freeze" and row["verify_empty_count"] == 1)

# ============================ chk_freeze_handle DB 约束有齿 ============================
print("\n== chk_freeze_handle DB 约束(paid+running 无 freeze 被拒)==")
try:
    q("INSERT INTO diagnosis_runs (run_token, session_id, owner_user_id, client_request_id, billing_mode, freeze_task_ref, run_status) "
      "VALUES ('rt-bad','s-bad',10,'cr-bad','paid','diag_rt-bad','running')")
    check("chk_freeze_handle 拦 paid+running 无 freeze", False)
except Exception:
    check("chk_freeze_handle 拦 paid+running 无 freeze(约束有齿)", True)

# ============================ §3.7 人工处置 ============================
print("\n== §3.7 verify_and_resolve_manual ==")
rt = dr.mint_run_token(); dr.admit_run(10, "r-man", 114, "s-man", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='settlement_manual' WHERE run_token=%s", (rt,))
q("INSERT INTO point_freezes (user_id, task_ref, status, amount_total) VALUES (10,%s,'frozen',650) RETURNING id", (dr.freeze_task_ref(rt),))
fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (dr.freeze_task_ref(rt),))[0]["id"]
bad = dr.verify_and_resolve_manual(rt, "admin(uid=1)", "v35", fid, "commit", "note")  # backend 错(v35 表无此行)
check("Handle 三元组校验不过(选错 backend)→ 拒绝", not bad["ok"])
good = dr.verify_and_resolve_manual(rt, "admin(uid=1)", "legacy", fid, "release", "核对无交付,退款")
row = q("SELECT run_status, freeze_id, freeze_backend FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("正确 backend + 三元组匹配 → CAS 回 release_pending + 回填 Handle",
      good["ok"] and row["run_status"] == "release_pending" and row["freeze_id"] == fid and row["freeze_backend"] == "legacy")

# ============================ 对抗审核修复净增量 ============================
print("\n== [修复净增量] #6 sweeper commit 分支写 final_snapshot ==")
_billing_cfg["commit"] = {"success": True}
rt = dr.mint_run_token(); mk_record("s-fix6", 100, rt); dr.admit_run(10, "r-fix6", 200, "s-fix6", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 800, "legacy", "paid"))
q("UPDATE diagnosis_runs SET run_status='commit_pending', next_settlement_at=NOW()-INTERVAL '1 min' WHERE run_token=%s", (rt,))
asyncio.run(dr.run_diagnosis_sweep())
row = q("SELECT run_status, final_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("#6 sweeper commit → committed + final_snapshot NOT NULL(reconciler 可回补)",
      row["run_status"] == "committed" and row["final_snapshot_jsonb"] is not None)

print("\n== [修复净增量] #7 exempt pending_freeze 收尸 ==")
rt = dr.mint_run_token(); mk_record("s-fix7", 201, rt); dr.admit_run(9, "r-fix7", 201, "s-fix7", rt, "exempt")
q("UPDATE diagnosis_runs SET run_status='pending_freeze', status_changed_at=NOW()-INTERVAL '20 min' WHERE run_token=%s", (rt,))
asyncio.run(dr.run_diagnosis_sweep())
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("#7 exempt 卡 pending_freeze 超 10min → failed_exempt(不锁死品牌)", row["run_status"] == "failed_exempt")

print("\n== [修复净增量] #13 exempt 判死写 final_snapshot ==")
rt = dr.mint_run_token(); mk_record("s-fix13", 202, rt); dr.admit_run(9, "r-fix13", 202, "s-fix13", rt, "exempt")
dr.activate_after_freeze(rt, 9, dr.FreezeClassification("exempt", None, None, "exempt"))
q("UPDATE diagnosis_runs SET heartbeat_at=NOW()-INTERVAL '10 min' WHERE run_token=%s", (rt,))
asyncio.run(dr.run_diagnosis_sweep())
row = q("SELECT run_status, final_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-fix13'")[0]["result_visibility"]
check("#13 exempt 判死 → failed_exempt + final_snapshot NOT NULL + withheld",
      row["run_status"] == "failed_exempt" and row["final_snapshot_jsonb"] is not None and vis == "withheld")

print("\n== [修复净增量] #5 v35 表未建 → locate 不误判 error(安全查空)==")
q("DROP TABLE IF EXISTS customer_credit_freezes")
b, fid, st = dr._double_table_locate(10, "diag_nonexist")
check("#5 v35 表未建(UndefinedTable)→ 视作确定无 v35 冻结(None 非 error)", b is None)
# legacy 有冻结时仍能定位
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,'diag_leg1','frozen')")
b2, fid2, st2 = dr._double_table_locate(10, "diag_leg1")
check("#5 v35 表未建但 legacy 有 → 正常定位 legacy(不受影响)", b2 == "legacy")
_create_v35_freeze_table_production_shape()
_require_production_v35_fund_schema()

print("\n== [修复净增量审核 P2] #5-fu 列级 schema 漂(UndefinedColumn)→ 'error' 非误查空 ==")
q("ALTER TABLE point_freezes DROP COLUMN status")   # 制造 UndefinedColumn(SELECT id,status 会炸)
b_fu, _, _ = dr._double_table_locate(10, "diag_anytask")
check("#5-fu 列级 schema 漂(UndefinedColumn `column status does not exist`)→ 'error'(不假定无冻·不误 cancel 零退款)",
      b_fu == "error")
q("ALTER TABLE point_freezes ADD COLUMN status TEXT DEFAULT 'frozen'")
# 恢复后正常查空(表在列在无冻结)→ None(不是 error)
b_ok, _, _ = dr._double_table_locate(10, "diag_stillempty")
check("#5-fu 恢复后正常查空 → None(区分真查空 vs schema 漂)", b_ok is None)

# ============================ NO-GO 返工判别(P0-3/P1-2/P1-3)============================
print("\n== [NO-GO P0-3] 人工结算单事务:验证不过绝不半写 Handle + A/B 顺序恰一个成功 ==")
rt = dr.mint_run_token(); dr.admit_run(10, "r-ng3", 300, "s-ng3", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='settlement_manual' WHERE run_token=%s", (rt,))
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (dr.freeze_task_ref(rt),))
fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (dr.freeze_task_ref(rt),))[0]["id"]
# 验证不过(错 backend)→ 拒 + run 仍 settlement_manual + Handle 未写(freeze_id/backend 仍 NULL)
bad = dr.verify_and_resolve_manual(rt, "adminA(uid=1)", "v35", fid, "commit", "wrong")
row = q("SELECT run_status, freeze_id, freeze_backend FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("P0-3 验证不过 → 拒 + run 仍 settlement_manual + Handle 未半写(freeze_id/backend 仍 NULL)",
      (not bad["ok"]) and row["run_status"] == "settlement_manual"
      and row["freeze_id"] is None and row["freeze_backend"] is None)
# A 成功(settlement_manual→release_pending)
gA = dr.verify_and_resolve_manual(rt, "adminA(uid=1)", "legacy", fid, "release", "核对退款")
# B 再处置(已非 settlement_manual)→ 拒(恰一个成功语义 · FOR UPDATE 锁内状态复核)
gB = dr.verify_and_resolve_manual(rt, "adminB(uid=2)", "legacy", fid, "commit", "抢")
row = q("SELECT run_status, freeze_id, freeze_backend FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("P0-3 A 成功→release_pending+Handle 原子回填 · B 复核状态已变→拒(恰一个成功)",
      gA["ok"] and (not gB["ok"]) and row["run_status"] == "release_pending"
      and row["freeze_id"] == fid and row["freeze_backend"] == "legacy")

print("\n== [NO-GO P1-2] 终态 final_snapshot 富化 diagnosis_id ==")
_billing_cfg["commit"] = {"success": True}
rt = dr.mint_run_token(); mk_record("s-ng2", 301, rt); dr.admit_run(10, "r-ng2", 301, "s-ng2", rt, "paid")
did = q("SELECT id FROM diagnosis_records WHERE session_id='s-ng2'")[0]["id"]
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 900, "legacy", "paid"))
asyncio.run(dr.commit_run(rt, {"type": "complete", "done": True, "terminal": True, "message": "ok"}))
import json as _json2
snap = q("SELECT final_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]["final_snapshot_jsonb"]
snap = _json2.loads(snap) if isinstance(snap, str) else snap
check("P1-2 commit 终态 final_snapshot 含 diagnosis_id(前端跳转/reconciler 回补)",
      isinstance(snap, dict) and snap.get("diagnosis_id") == did)

print("\n== [NO-GO P1-3] cancelled/cancelled_no_freeze 同步 withheld + final_snapshot ==")
rt = dr.mint_run_token(); mk_record("s-ng1a", 302, rt); dr.admit_run(10, "r-ng1a", 302, "s-ng1a", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("cancelled", None, None, "paid"))
row = q("SELECT run_status, final_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-ng1a'")[0]["result_visibility"]
check("P1-3 cancelled → withheld + final_snapshot 非 NULL(不留旧成功态回退窗口)",
      row["run_status"] == "cancelled" and vis == "withheld" and row["final_snapshot_jsonb"] is not None)
# cancelled_no_freeze via sweeper
rt = dr.mint_run_token(); mk_record("s-ng1b", 303, rt); dr.admit_run(10, "r-ng1b", 303, "s-ng1b", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='pending_freeze', status_changed_at=NOW()-INTERVAL '20 min' WHERE run_token=%s", (rt,))
asyncio.run(dr.run_diagnosis_sweep())
q("UPDATE diagnosis_runs SET last_verify_at=NOW()-INTERVAL '40 sec' WHERE run_token=%s", (rt,))
asyncio.run(dr.run_diagnosis_sweep())
row = q("SELECT run_status, final_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-ng1b'")[0]["result_visibility"]
check("P1-3 cancelled_no_freeze → withheld + final_snapshot(Redis 过期不回退成功态)",
      row["run_status"] == "cancelled_no_freeze" and vis == "withheld" and row["final_snapshot_jsonb"] is not None)

print("\n== [NO-GO P0-2b] 列表过滤 withheld 谓词(直接 SQL · 同 get_brand_diagnoses WHERE)==")
q("INSERT INTO diagnosis_records (session_id, brand_name, brand_id, result_visibility, total_score) "
  "VALUES ('vis-pub', 'B', 999, 'published', 80), ('vis-wh', 'B', 999, 'withheld', 70), "
  "('vis-null', 'B', 999, NULL, 60) ON CONFLICT (session_id) DO NOTHING")
# 复用 get_brand_diagnoses 的 WHERE 谓词(避免 import 全 db 模块触发 init_db 撞最小 schema)
_rows = q("SELECT session_id FROM diagnosis_records WHERE brand_id=999 "
          "AND (result_visibility IS NULL OR result_visibility <> 'withheld')")
_sids = {r["session_id"] for r in _rows}
check("P0-2b 列表谓词含 published + NULL(兼容)· 排除 withheld",
      "vis-pub" in _sids and "vis-null" in _sids and "vis-wh" not in _sids)

# ============================================================================
# ================== NO-GO 返工2 判别(3P0 + 4P1 + item8)==================
# ============================================================================

print("\n== [返工2 P1-2] 客户读取谓词 published-only(排 withheld **且排 pending**)==")
q("INSERT INTO diagnosis_records (session_id, brand_name, brand_id, result_visibility, total_score) "
  "VALUES ('v2-pub','B',888,'published',80),('v2-wh','B',888,'withheld',70),"
  "('v2-pend','B',888,'pending',NULL),('v2-null','B',888,NULL,60) ON CONFLICT (session_id) DO NOTHING")
_rows2 = q("SELECT session_id FROM diagnosis_records WHERE brand_id=888 "
           "AND (result_visibility IS NULL OR result_visibility = 'published')")
_sids2 = {r["session_id"] for r in _rows2}
check("返工2 P1-2 published-only:含 published+NULL · 排 withheld **和 pending**",
      _sids2 == {"v2-pub", "v2-null"})

print("\n== [返工2 P0-2] commit 成功但 0 产物行 → delivery_repair_pending(非成功 · 禁 complete)==")
_billing_cfg["commit"] = {"success": True}
rt = dr.mint_run_token()
# 故意**不** mk_record(该 session 无产物行)→ 可见性 UPDATE 命中 0 行
dr.admit_run(10, "r-dr", 401, "s-dr-noprod", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 950, "legacy", "paid"))
out = asyncio.run(dr.commit_run(rt, {"type": "complete", "done": True, "terminal": True}))
row = q("SELECT run_status, final_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
import json as _json3
_snapdr = row["final_snapshot_jsonb"]
_snapdr = _json3.loads(_snapdr) if isinstance(_snapdr, str) else _snapdr
check("返工2 P0-2 commit 成功但 0 产物 → delivery_repair_pending(非 committed)",
      row["run_status"] == "delivery_repair_pending")
check("返工2 P0-2 commit_run 返回 ok=False + terminal=delivery_repair_pending(调用方禁发 complete)",
      (not out["ok"]) and out.get("terminal") == "delivery_repair_pending")
check("返工2 P0-2 final_snapshot 是异常/repair(type=error · 非 complete)",
      isinstance(_snapdr, dict) and _snapdr.get("type") == "error")

print("\n== [返工2 P0-1c] reconciler:Redis 陈旧'成功态'但 DB 是 withheld → 用 DB 终态纠正 ==")
_billing_cfg["release"] = {"success": True}
rt = dr.mint_run_token(); mk_record("s-recon", 402, rt); dr.admit_run(10, "r-recon", 402, "s-recon", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 951, "v35", "paid"))
asyncio.run(dr.release_run(rt, "task failed", {"type": "error", "done": True, "terminal": True, "message": "退款"}))
# Redis 里是**陈旧成功态**(impl 早发/翻案前)
_pb_cfg["snapshot"] = {"type": "complete", "done": True, "terminal": True, "message": "已完成"}
_pb_cfg["published"] = []
asyncio.run(dr.run_diagnosis_reconciler())
_repub = [p for p in _pb_cfg["published"] if p["sid"] == "s-recon"]
check("返工2 P0-1c Redis 显 complete 但 DB released → reconciler 重发 DB 终态(纠正陈旧成功态)",
      len(_repub) == 1 and _repub[0]["payload"].get("type") == "error")
# 一致时(Redis 也是 error 终态)→ 不重发
_pb_cfg["snapshot"] = {"type": "error", "done": True, "terminal": True}
_pb_cfg["published"] = []
asyncio.run(dr.run_diagnosis_reconciler())
check("返工2 P0-1c Redis 与 DB 终态一致(都失败)→ 不重复回补",
      len([p for p in _pb_cfg["published"] if p["sid"] == "s-recon"]) == 0)
_pb_cfg["snapshot"] = None  # 复位

print("\n== [返工2 P0-3] 双表都冻结:单表处置被拒 + 双表逐笔核清(keeper 进态 · 重复笔退款)==")
rt = dr.mint_run_token(); dr.admit_run(10, "r-df", 403, "s-df", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='settlement_manual' WHERE run_token=%s", (rt,))
_tref = dr.freeze_task_ref(rt)
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (_tref,))
_insert_v35_freeze(10, 9, _tref, "frozen")
leg_fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
v35_fid = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
# 单表处置 → 被拒(double_frozen)
single = dr.verify_and_resolve_manual(rt, "admin(uid=1)", "legacy", leg_fid, "commit", "只处置一笔")
check("返工2 P0-3 双表都冻结时单表处置被拒(double_frozen)",
      (not single["ok"]) and single.get("double_frozen") is True)
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工2 P0-3 单表被拒后 run 仍 settlement_manual(未半处置)", row["run_status"] == "settlement_manual")
# 双表处置:keeper=legacy commit · 重复笔=v35 release
_billing_cfg["release_calls"] = []; _billing_cfg["release"] = {"success": True}
dbl = asyncio.run(dr.resolve_double_frozen(rt, "admin(uid=1)", "legacy", leg_fid, "commit", "v35", v35_fid, "双表核清"))
row = q("SELECT run_status, freeze_id, freeze_backend FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工2 P0-3 双表处置 → keeper(legacy)进 commit_pending + Handle=legacy",
      dbl["ok"] and row["run_status"] == "commit_pending" and row["freeze_id"] == leg_fid and row["freeze_backend"] == "legacy")
_rel_v35 = [c for c in _billing_cfg["release_calls"] if c.get("freeze_id") == v35_fid and c.get("freeze_table") == "v35"]
check("返工2 P0-3 重复笔(v35)被 release_freeze 退款(四参 · freeze_id 精确)", len(_rel_v35) == 1)

print("\n== [返工2 item8] 持久审计:双表处置写 manual_resolve_double + dup_release ==")
_aud = dr.list_settlement_audit(rt)
_actions = {a["action"] for a in _aud}
check("返工2 item8 双表处置持久审计含 manual_resolve_double + dup_release",
      "manual_resolve_double" in _actions and "dup_release" in _actions)

print("\n== [返工5 P0] 重复笔 release 返 idempotent committed(已消费未退)→ 禁推进 keeper · token-gated 回退 settlement_manual ==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-p0dbl", 407, "s-p0dbl", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='settlement_manual' WHERE run_token=%s", (rt,))
_tref = dr.freeze_task_ref(rt)
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (_tref,))
_insert_v35_freeze(10, 9, _tref, "committed")
leg_fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
v35_fid = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
# 🔴 判别核心:重复笔(v35)其实**已 committed(已消费)** · fake release 返 idempotent committed(钱没退)。
#   修复前只判 success=True → 记成已退 + 推进 keeper commit_pending = 双扣。修复后 → 拒推进 + 回退 settlement_manual。
_billing_cfg["release_calls"] = []
_billing_cfg["release"] = {"success": True, "idempotent": True, "status": "committed"}
_dbl = asyncio.run(dr.resolve_double_frozen(rt, "admin(uid=1)", "legacy", leg_fid, "commit", "v35", v35_fid, "双表核清"))
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工5 P0 重复笔 idempotent committed(未真实退)→ resolve **不 ok**(禁把已消费笔当已退)", not _dbl["ok"])
check("返工5 P0 → keeper **未推进**(回退 settlement_manual · 非 commit_pending 防双扣)", row["run_status"] == "settlement_manual")
# 对照:重复笔真实 released(idempotent released)→ 正常推进 keeper
_billing_cfg["release"] = {"success": True, "idempotent": True, "status": "released"}
_dbl2 = asyncio.run(dr.resolve_double_frozen(rt, "admin(uid=1)", "legacy", leg_fid, "commit", "v35", v35_fid, "双表核清2"))
row2 = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工5 P0 对照:重复笔 idempotent **released**(前次已退)→ 正常推进 keeper commit_pending",
      _dbl2["ok"] and row2["run_status"] == "commit_pending")
_billing_cfg["release"] = {"success": True}   # 复位 · 不影响下游

print("\n== [返工2 item8] auto→settlement_manual 写 auto_to_manual 审计 ==")
_billing_cfg["commit"] = {"success": False, "ambiguous": True}
rt = dr.mint_run_token(); dr.admit_run(10, "r-a2m", 404, "s-a2m", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 952, "legacy", "paid"))
asyncio.run(dr.commit_run(rt, {"done": True}))
_aud2 = dr.list_settlement_audit(rt)
check("返工2 item8 ambiguous 自动转人工 → 持久审计 auto_to_manual",
      any(a["action"] == "auto_to_manual" for a in _aud2))

print("\n== [返工2 item8] settlement_manual 超时(>1h)→ sweeper 限频告警 ==")
rt = dr.mint_run_token(); dr.admit_run(10, "r-stuck", 405, "s-stuck", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='settlement_manual', status_changed_at=NOW()-INTERVAL '2 hours' WHERE run_token=%s", (rt,))
_stats_stuck = asyncio.run(dr.run_diagnosis_sweep())
check("返工2 item8 settlement_manual 卡超 1h → sweeper stuck_alerted≥1", _stats_stuck.get("stuck_alerted", 0) >= 1)

print("\n== [返工2 P1-4] run_lease_lost DB fencing(确定非 running 才 True · 抖动/查无 fail-open)==")
rt = dr.mint_run_token(); dr.admit_run(10, "r-fence", 406, "s-fence", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 953, "legacy", "paid"))  # → running
check("返工2 P1-4 running → run_lease_lost=False(不中止落库)", dr.run_lease_lost(rt) is False)
q("UPDATE diagnosis_runs SET run_status='release_pending' WHERE run_token=%s", (rt,))
check("返工2 P1-4 release_pending(被收尸)→ run_lease_lost=True(中止落库)", dr.run_lease_lost(rt) is True)
check("返工2 P1-4 run 不存在 → run_lease_lost=False(不确定 · fail-open 交下游兜底)",
      dr.run_lease_lost("run_nonexistent_xyz") is False)

print("\n== [返工2 P1-1] admit 幂等命中的终态旧 run 不属活跃态(server 据此返 resolved)==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(12, "req-term", 500, "s-term", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='cancelled', finished_at=NOW() WHERE run_token=%s", (rt,))
a_term = dr.admit_run(12, "req-term", 500, "s-term2", dr.mint_run_token(), "paid")
check("返工2 P1-1 同 client_request_id 幂等命中旧 run",
      (not a_term.admitted) and a_term.reason == "idempotent_retry" and a_term.run is not None)
check("返工2 P1-1 旧 run 是终态(cancelled not-in ACTIVE_STATUSES)→ server 端返 status=resolved 不进死任务",
      a_term.run["run_status"] == "cancelled" and a_term.run["run_status"] not in dr.ACTIVE_STATUSES)

print("\n== [返工2 P1-2] withheld 结算回退 brands.latest_score 冗余列 ==")
_billing_cfg["release"] = {"success": True}
# 造 brand + 两诊断(老 published + 新 pending→将 withheld)· brands.latest 指向新诊断
q("INSERT INTO brands (id, name, latest_score, latest_diagnosis_id) VALUES (600,'BR',88,NULL) ON CONFLICT (id) DO NOTHING")
q("INSERT INTO diagnosis_records (session_id, brand_name, brand_id, result_visibility, total_score) "
  "VALUES ('br-old','BR',600,'published',72) ON CONFLICT (session_id) DO NOTHING")
old_did = q("SELECT id FROM diagnosis_records WHERE session_id='br-old'")[0]["id"]
rt = dr.mint_run_token()
q("INSERT INTO diagnosis_records (session_id, brand_name, brand_id, run_token, result_visibility, total_score) "
  "VALUES ('br-new','BR',600,%s,'pending',95) ON CONFLICT (session_id) DO NOTHING", (rt,))
new_did = q("SELECT id FROM diagnosis_records WHERE session_id='br-new'")[0]["id"]
q("UPDATE brands SET latest_score=95, latest_diagnosis_id=%s WHERE id=600", (new_did,))
dr.admit_run(10, "r-brrev", 600, "br-new", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 954, "legacy", "paid"))
asyncio.run(dr.release_run(rt, "failed", {"type": "error", "done": True}))
brow = q("SELECT latest_score, latest_diagnosis_id FROM brands WHERE id=600")[0]
check("返工2 P1-2 withheld 后 brands.latest 回退到最近 published 诊断(不泄露退款分)",
      brow["latest_diagnosis_id"] == old_did and int(brow["latest_score"]) == 72)

print("\n== [返工2 修复净增量 P2] SAVEPOINT 隔离:brands 回退失败不毒化终态事务(终态+可见性仍落)==")
_billing_cfg["release"] = {"success": True}
# 强制 brands 回退 UPDATE 失败:DROP brands → withheld 分支的 UPDATE brands 抛 UndefinedTable。
#   修复前(无 SAVEPOINT):事务毒化 → 紧随 final_snapshot 写抛 InFailedSqlTransaction → 整个终态事务回滚
#   → run 卡 release_pending、可见性卡 pending(已退款却卡住)。修复后(SAVEPOINT):回退隔离,终态照落。
q("DROP TABLE IF EXISTS brands")
rt = dr.mint_run_token(); mk_record("s-sp", 601, rt); dr.admit_run(10, "r-sp", 601, "s-sp", rt, "paid")
dr.activate_after_freeze(rt, 10, dr.FreezeClassification("paid", 960, "legacy", "paid"))
out = asyncio.run(dr.release_run(rt, "failed", {"type": "error", "done": True}))
row = q("SELECT run_status, final_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-sp'")[0]["result_visibility"]
check("返工2 P2 brands 回退失败(表缺)但 SAVEPOINT 隔离 → 终态 released 仍落", out["ok"] and row["run_status"] == "released")
check("返工2 P2 brands 回退失败但可见性 withheld + final_snapshot 仍写(终态事务未被毒化回滚)",
      vis == "withheld" and row["final_snapshot_jsonb"] is not None)
q("CREATE TABLE IF NOT EXISTS brands (id INTEGER PRIMARY KEY, name TEXT, latest_score REAL, latest_diagnosis_id INTEGER, updated_at TIMESTAMPTZ DEFAULT NOW())")

# ============================ [返工3 P0] 双表并发相反 keeper(claim-before-money · 判别式)============================
print("\n== [返工3 P0] 双表并发相反 keeper:恰一个 claim 成功 · **只退一笔** · keeper 与释放笔相反 ==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-cc", 700, "s-cc", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='settlement_manual' WHERE run_token=%s", (rt,))
_tref = dr.freeze_task_ref(rt)
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (_tref,))
_insert_v35_freeze(10, 9, _tref, "frozen")
leg_fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
v35_fid = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
_billing_cfg["release_calls"] = []; _billing_cfg["release"] = {"success": True}
def _resolve_A():
    return asyncio.run(dr.resolve_double_frozen(rt, "adminA(uid=1)", "legacy", leg_fid, "commit", "v35", v35_fid, "A核清"))
def _resolve_B():
    return asyncio.run(dr.resolve_double_frozen(rt, "adminB(uid=2)", "v35", v35_fid, "commit", "legacy", leg_fid, "B核清"))
with cf.ThreadPoolExecutor(max_workers=2) as ex:
    fa = ex.submit(_resolve_A); fb = ex.submit(_resolve_B)
    ra, rb = fa.result(), fb.result()
_oks = [r for r in (ra, rb) if r.get("ok")]
check("返工3 P0 并发相反 keeper → 恰一个 claim 成功", len(_oks) == 1)
row = q("SELECT run_status, freeze_id, freeze_backend, manual_resolution FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工3 P0 run 进 keeper commit_pending(单一处置生效)", row["run_status"] == "commit_pending")
# 🔴 判别核心:修复前(release-before-claim)两 admin 都过 settlement_manual 读检查 → **各退一笔=2 次 release**;
#    修复后 claim 原子占用 → 只有获胜者退一笔。
check("返工3 P0 **只退一笔**(修复前会两笔都退=双退竞态)", len(_billing_cfg["release_calls"]) == 1)
_kb = row["freeze_backend"]
_opp = "v35" if _kb == "legacy" else "legacy"
_opp_fid = v35_fid if _kb == "legacy" else leg_fid
_rel0 = _billing_cfg["release_calls"][0]
check("返工3 P0 释放笔=keeper 相反表(绝不误退 keeper 本身)",
      _rel0.get("freeze_table") == _opp and _rel0.get("freeze_id") == _opp_fid)
check("返工3 P0 keeper 推进后 manual_resolution 已清空", row["manual_resolution"] is None)
# 落败者被拒 · run 未被二次处置
_rej = [r for r in (ra, rb) if not r.get("ok")]
check("返工3 P0 落败者被拒(另一处置意图占用/已推进)", len(_rej) == 1)

# ============================ [返工3 P0] claim 后校验不过 → 回退 settlement_manual(未动钱)============================
print("\n== [返工3 P0] claim 后 keeper 三元组不匹配 → 拒 + 回退 settlement_manual + 零退款 ==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-rev", 703, "s-rev", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='settlement_manual' WHERE run_token=%s", (rt,))
_tref = dr.freeze_task_ref(rt)
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (_tref,))
_insert_v35_freeze(10, 9, _tref, "frozen")
v35_fid = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
_billing_cfg["release_calls"] = []
_bad = asyncio.run(dr.resolve_double_frozen(rt, "adminA(uid=1)", "legacy", 9999999, "commit", "v35", v35_fid, "坏keeper"))
row = q("SELECT run_status, manual_resolution FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工3 P0 keeper 三元组不匹配 → 拒 + 回退 settlement_manual + 意图清 + 零退款",
      (not _bad["ok"]) and row["run_status"] == "settlement_manual"
      and row["manual_resolution"] is None and len(_billing_cfg["release_calls"]) == 0)

# ============================ [返工3 P0] resume_double_frozen 崩溃续跑 ============================
print("\n== [返工3 P0] resume_double_frozen(manual_resolving+持久意图 → 退重复笔+推进 keeper+清意图)==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-rs", 701, "s-rs", rt, "paid")
_tref = dr.freeze_task_ref(rt)
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (_tref,))
_insert_v35_freeze(10, 9, _tref, "frozen")
leg_fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
v35_fid = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
_intent = json.dumps({"keeper_backend": "legacy", "keeper_freeze_id": leg_fid, "keeper_decision": "release",
                      "other_backend": "v35", "other_freeze_id": v35_fid, "operator": "adminA"}, sort_keys=True)
q("UPDATE diagnosis_runs SET run_status='manual_resolving', manual_resolution=%s WHERE run_token=%s", (_intent, rt))
_billing_cfg["release_calls"] = []; _billing_cfg["release"] = {"success": True}
_rs = asyncio.run(dr.resume_double_frozen(rt))
row = q("SELECT run_status, freeze_backend, manual_resolution FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工3 P0 resume → keeper(legacy release)进 release_pending + 意图清空",
      _rs["ok"] and row["run_status"] == "release_pending" and row["freeze_backend"] == "legacy" and row["manual_resolution"] is None)
check("返工3 P0 resume → 重复笔 v35 幂等退款",
      any(c.get("freeze_id") == v35_fid and c.get("freeze_table") == "v35" for c in _billing_cfg["release_calls"]))

# ============ [返工3 修复净增量] resume/动钱前 task_ref-bound 校验(判别:误填别任务冻结不退错钱)============
print("\n== [返工3 修复净增量] resume 前 task_ref-bound 校验:意图 other 误填别任务冻结 → 拒动钱 + 回退 settlement_manual ==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-xt", 704, "s-xt", rt, "paid")
_tref = dr.freeze_task_ref(rt)
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (_tref,))
leg_fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
# 同 user 另一任务(不同 task_ref)的 v35 冻结 → 被误填成 other_freeze_id(billing 按 id+user 定位不含 task_ref → 会退错任务)
_insert_v35_freeze(10, 9, "diag_OTHER_TASK", "frozen")
xtask_fid = q("SELECT id FROM customer_credit_freezes WHERE task_ref='diag_OTHER_TASK'")[0]["id"]
_bad_intent = json.dumps({"keeper_backend": "legacy", "keeper_freeze_id": leg_fid, "keeper_decision": "release",
                          "other_backend": "v35", "other_freeze_id": xtask_fid, "operator": "adminA"}, sort_keys=True)
q("UPDATE diagnosis_runs SET run_status='manual_resolving', manual_resolution=%s WHERE run_token=%s", (_bad_intent, rt))
_billing_cfg["release_calls"] = []
_xr = asyncio.run(dr.resume_double_frozen(rt))
row = q("SELECT run_status, manual_resolution FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
# 🔴 判别核心:修复前 resume 不校验 → release_freeze(xtask_fid) 退掉别任务的冻结(len(release_calls)==1);
#    修复后 task_ref-bound 校验拦下 → 零退款 + 回退 settlement_manual。
check("返工3 修复净增量 resume:other 误填别任务冻结(task_ref 不匹配)→ 拒动钱 + 回退 settlement_manual + **零退款**",
      (not _xr["ok"]) and row["run_status"] == "settlement_manual" and row["manual_resolution"] is None
      and len(_billing_cfg["release_calls"]) == 0)

# ============================ [返工3 P0] sweeper 自动续跑 manual_resolving(卡>2min)============================
print("\n== [返工3 P0] sweeper 自动续跑 manual_resolving(卡>2min · 按持久意图幂等)==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-sr", 702, "s-sr", rt, "paid")
_tref = dr.freeze_task_ref(rt)
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (_tref,))
_insert_v35_freeze(10, 9, _tref, "frozen")
leg_fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
v35_fid = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
_intent = json.dumps({"keeper_backend": "legacy", "keeper_freeze_id": leg_fid, "keeper_decision": "commit",
                      "other_backend": "v35", "other_freeze_id": v35_fid, "operator": "adminA"}, sort_keys=True)
q("UPDATE diagnosis_runs SET run_status='manual_resolving', manual_resolution=%s, "
  "status_changed_at=NOW()-INTERVAL '5 minutes' WHERE run_token=%s", (_intent, rt))
_billing_cfg["release_calls"] = []
_st_sr = asyncio.run(dr.run_diagnosis_sweep())
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工3 P0 sweeper 自动续跑 manual_resolving → keeper *_pending",
      _st_sr.get("manual_resumed", 0) >= 1 and row["run_status"] in ("commit_pending", "release_pending"))

# ============================ [返工3 P1] delivery_repair 处置闭环 ============================
print("\n== [返工3 P1] delivery_repair republish(产物已恢复 → committed + published · 幂等)==")
wipe()
rt = dr.mint_run_token(); mk_record("s-dr1", 800, rt); dr.admit_run(10, "r-dr1", 800, "s-dr1", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending' WHERE run_token=%s", (rt,))
_rp = dr.repair_delivery(rt, "admin(uid=1)", "republish", "产物已恢复")
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-dr1'")[0]["result_visibility"]
check("返工3 P1 republish → run committed + 产物 published + 审计", _rp["ok"] and row["run_status"] == "committed"
      and vis == "published" and "delivery_republish" in {a["action"] for a in dr.list_settlement_audit(rt)})
_rp2 = dr.repair_delivery(rt, "admin(uid=1)", "republish", "重复")
check("返工3 P1 republish 幂等(已 committed → idempotent ok)", _rp2["ok"] and _rp2.get("idempotent"))

print("\n== [返工3 P1] delivery_repair:无产物 republish 拒 · writeoff 核销(ops_resolved 抑制告警 · 幂等)==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-dr2", 801, "s-dr2", rt, "paid")   # 无 diagnosis_records
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', status_changed_at=NOW()-INTERVAL '2 hours' WHERE run_token=%s", (rt,))
_rpx = dr.repair_delivery(rt, "admin(uid=1)", "republish", "无产物")
check("返工3 P1 无产物 republish 被拒(导向 writeoff)", not _rpx["ok"])
_st_dr1 = asyncio.run(dr.run_diagnosis_sweep())
check("返工3 P1 未核销 delivery_repair 卡>1h → sweeper 告警", _st_dr1.get("stuck_alerted", 0) >= 1)
# [返工4 P1] writeoff 改为进 refund_pending(不再 ops_resolved 抑告警 · 完整 writeoff→confirm_refund 流见下方返工4 段)
_wo = dr.repair_delivery(rt, "admin(uid=1)", "writeoff", "工单#123 退款")
lse = q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]["last_settlement_error"]
check("返工4 P1 writeoff → refund_pending 标记 + 审计 delivery_writeoff_intent",
      _wo["ok"] and str(lse).startswith("refund_pending:")
      and "delivery_writeoff_intent" in {a["action"] for a in dr.list_settlement_audit(rt)})
_wo2 = dr.repair_delivery(rt, "admin(uid=1)", "writeoff", "重复")
check("返工4 P1 writeoff 幂等(已 refund_pending → idempotent ok)", _wo2["ok"] and _wo2.get("idempotent"))
q("DELETE FROM diagnosis_runs"); q("DELETE FROM diagnosis_records")

# ============================ [返工4 P0] 租约 token/lease(判别:相同意图有效租约不并发 · token-gated 回退/接管)============================
print("\n== [返工4 P0] 有效租约内相同意图/相反 keeper claim → in_progress(不放第二执行者并发 · 消除双退根因)==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-lease", 710, "s-lease", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='settlement_manual' WHERE run_token=%s", (rt,))
_tref = dr.freeze_task_ref(rt)
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (_tref,))
_insert_v35_freeze(10, 9, _tref, "frozen")
leg_fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
v35_fid = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
claimA = dr._claim_double_frozen(rt, "adminA", "legacy", leg_fid, "release", "v35", v35_fid)
check("返工4 P0 A fresh claim → 成功 + 拿 token(非 resumed)", claimA["ok"] and claimA.get("token") and not claimA.get("resumed"))
claimB = dr._claim_double_frozen(rt, "adminB", "legacy", leg_fid, "release", "v35", v35_fid)
check("返工4 P0 有效租约内**相同意图**第二 claim → in_progress(不并发续跑)", (not claimB["ok"]) and claimB.get("in_progress"))
claimC = dr._claim_double_frozen(rt, "adminC", "v35", v35_fid, "release", "legacy", leg_fid)
check("返工4 P0 有效租约内**相反 keeper** claim → in_progress(杜绝反向 claim 窗口)", (not claimC["ok"]) and claimC.get("in_progress"))

print("\n== [返工4 P0] token-gated 回退:错 token 不能回退(防被接管者误回退打开反向 claim)==")
dr._revert_claim(rt, "res_WRONGTOKEN")
row = q("SELECT run_status, manual_resolution_token FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工4 P0 错 token 回退 → no-op(run 仍 manual_resolving · A token 未动)",
      row["run_status"] == "manual_resolving" and row["manual_resolution_token"] == claimA["token"])
dr._revert_claim(rt, claimA["token"])
row = q("SELECT run_status, manual_resolution_token, manual_lease_until FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
check("返工4 P0 正 token 回退 → settlement_manual + 清 token/lease",
      row["run_status"] == "settlement_manual" and row["manual_resolution_token"] is None and row["manual_lease_until"] is None)

print("\n== [返工4 P0] sweeper 仅接管**租约过期**的 manual_resolving(有效租约不抢在途处理)==")
wipe()
rt1 = dr.mint_run_token(); dr.admit_run(10, "r-lv", 711, "s-lv", rt1, "paid")
_t1 = dr.freeze_task_ref(rt1)
q("INSERT INTO point_freezes (user_id, task_ref, status) VALUES (10,%s,'frozen')", (_t1,))
_insert_v35_freeze(10, 9, _t1, "frozen")
lf1 = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_t1,))[0]["id"]
vf1 = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_t1,))[0]["id"]
_it1 = json.dumps({"keeper_backend": "legacy", "keeper_freeze_id": lf1, "keeper_decision": "release",
                   "other_backend": "v35", "other_freeze_id": vf1, "operator": "a"}, sort_keys=True)
q("UPDATE diagnosis_runs SET run_status='manual_resolving', manual_resolution=%s, manual_resolution_token='res_valid', "
  "manual_lease_until=NOW()+INTERVAL '90 seconds' WHERE run_token=%s", (_it1, rt1))
_billing_cfg["release_calls"] = []; _billing_cfg["release"] = {"success": True}
_stv = asyncio.run(dr.run_diagnosis_sweep())
row1 = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt1,))[0]
check("返工4 P0 有效租约 manual_resolving → sweeper **不接管**(仍 manual_resolving)", row1["run_status"] == "manual_resolving")
q("UPDATE diagnosis_runs SET manual_lease_until=NOW()-INTERVAL '10 seconds' WHERE run_token=%s", (rt1,))
_ste = asyncio.run(dr.run_diagnosis_sweep())
row1b = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt1,))[0]
check("返工4 P0 租约过期 manual_resolving → sweeper 接管续跑 → keeper *_pending",
      _ste.get("manual_resumed", 0) >= 1 and row1b["run_status"] in ("commit_pending", "release_pending"))

# ============================ [返工4 P1] republish 完整性 + writeoff→refund_pending→confirm_refund ============================
print("\n== [返工4 P1] republish 完整性:空壳(total_score NULL)拒 · 完整记录只发布该条 rowcount==1 ==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-rp2", 720, "s-rp2", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending' WHERE run_token=%s", (rt,))
q("INSERT INTO diagnosis_records (session_id, brand_name, brand_id, result_visibility, total_score) "
  "VALUES ('s-rp2','B',720,'pending',NULL) ON CONFLICT (session_id) DO NOTHING")   # 空壳占位(total_score NULL)
_rp_empty = dr.repair_delivery(rt, "admin(uid=1)", "republish", "试图发空壳")
check("返工4 P1 republish 空壳(total_score NULL)→ **拒**(不发空壳)", not _rp_empty["ok"])
q("UPDATE diagnosis_records SET total_score=88 WHERE session_id='s-rp2'")   # 补完整分
_rp_ok = dr.repair_delivery(rt, "admin(uid=1)", "republish", "产物已恢复")
row = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]
vis = q("SELECT result_visibility FROM diagnosis_records WHERE session_id='s-rp2'")[0]["result_visibility"]
check("返工4 P1 完整记录 republish → committed + 该条 published", _rp_ok["ok"] and row["run_status"] == "committed" and vis == "published")

print("\n== [返工5 复审二 P1] confirm_refund 只读核验**真实钱包退款流水**(type=refund · 逐资金池 · 无真退款保持 refund_pending)==")
wipe()
# legacy 冻结(committed · 拆分 bonus=100 + paid=550 = 650)· owner=10 · consume 流水(CMT{fid}-)
rt = dr.mint_run_token(); dr.admit_run(10, "r-wf2", 721, "s-wf2", rt, "paid")
_tref = dr.freeze_task_ref(rt)
q("INSERT INTO point_freezes (user_id, task_ref, status, amount_total, amount_bonus, amount_commission, amount_paid) "
  "VALUES (10,%s,'committed',650,100,0,550)", (_tref,))
_fid = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_tref,))[0]["id"]
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=%s, freeze_backend='legacy', "
  "freeze_task_ref=%s WHERE run_token=%s", (_fid, _tref, rt))
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (10,'consume','bonus',-100,0,%s)", (f"CMT{_fid}-x",))
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (10,'consume','paid',-550,0,%s)", (f"CMT{_fid}-x",))
_cb = q("SELECT id FROM point_transactions WHERE type='consume' AND point_type='bonus' AND order_id=%s", (f"CMT{_fid}-x",))[0]["id"]
_cp = q("SELECT id FROM point_transactions WHERE type='consume' AND point_type='paid' AND order_id=%s", (f"CMT{_fid}-x",))[0]["id"]
dr.repair_delivery(rt, "admin(uid=1)", "writeoff", "产物不可恢复·走退款")
check("返工4 P1 writeoff → refund_pending",
      str(q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s",(rt,))[0]["last_settlement_error"]).startswith("refund_pending:"))
# 🔴 判别1:无真实退款流水 → confirm 拒 · 保持 refund_pending(禁手填凭证标记已退)
check("返工5 复审二 P1 无真实退款流水 → confirm 拒(保持 refund_pending)",
      not dr.repair_delivery(rt, "admin(uid=1)", "confirm_refund", "手填凭证但无真退款", refund_tx_id="999999")["ok"])
# 🔴 判别2:consume(CMT)不是退款证据 → 拒
check("返工5 复审二 P1 仅 consume(CMT)当退款 → 拒(consume 只证扣费)",
      not dr.repair_delivery(rt, "admin(uid=1)", "confirm_refund", "拿consume当退款", refund_tx_id=str(_cb))["ok"])
# 🔴 判别3:release(RLS)不是已提交扣费的退款证据 → 拒
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (10,'release','bonus',100,0,%s)", (f"RLS{_fid}-x",))
_rls = q("SELECT id FROM point_transactions WHERE type='release' AND order_id=%s", (f"RLS{_fid}-x",))[0]["id"]
check("返工5 复审二 P1 release(RLS)当退款 → 拒(release 只适用未提交冻结)",
      not dr.repair_delivery(rt, "admin(uid=1)", "confirm_refund", "拿release当退款", refund_tx_id=str(_rls))["ok"])
# 🔴 判别4:真实 refund 流水但**资金池拆分错**(总额650对 · 全退 paid)→ 拒
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (10,'refund','paid',650,0,%s)", (str(_cp),))
_bad = q("SELECT id FROM point_transactions WHERE type='refund' AND point_type='paid' AND amount=650 AND order_id=%s", (str(_cp),))[0]["id"]
check("返工5 复审二 P1 退款总额对但资金池拆分错(全退paid 650 ≠ bonus100+paid550)→ 拒",
      not dr.repair_delivery(rt, "admin(uid=1)", "confirm_refund", "退错池", refund_tx_id=str(_bad))["ok"])
q("DELETE FROM point_transactions WHERE id=%s", (_bad,))
# 全拒后:零退款记录 + run 仍 refund_pending(原子 · 未误 resolved)
check("返工5 复审二 P1 全拒后零退款记录 + run 仍 refund_pending(未误 resolved)",
      len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (rt,)))==0
      and str(q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s",(rt,))[0]["last_settlement_error"]).startswith("refund_pending:"))
# 🔴 判别5:真实 refund 流水(**admin_refund_order 格式:order_id=被退 consume 的 order_id 串 'CMT{fid}-x'** · 逐池对+总额对)
#   → confirm 通过 → resolved + 绑定记录 + 审计同事务(证在线唯一 admin 退款工具产出可达 · 修复净增量抓到的 P2)
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (10,'refund','bonus',100,0,%s)", (f"CMT{_fid}-x",))
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (10,'refund','paid',550,0,%s)", (f"CMT{_fid}-x",))
_rb = q("SELECT id FROM point_transactions WHERE type='refund' AND point_type='bonus' AND order_id=%s", (f"CMT{_fid}-x",))[0]["id"]
_cf = dr.repair_delivery(rt, "admin(uid=1)", "confirm_refund", "已核对真实退款流水", refund_tx_id=str(_rb))
_rec = q("SELECT freeze_id, freeze_backend, owner_user_id, points, refund_tx_ref FROM diagnosis_refund_records WHERE run_token=%s", (rt,))
check("返工5 复审二 P1 真实 refund 流水(逐池对+总额对)→ confirm 通过 + 建绑定记录(refund_tx_ref/points/freeze)",
      _cf["ok"] and len(_rec)==1 and _rec[0]["points"]==650 and _rec[0]["freeze_id"]==_fid and _rec[0]["refund_tx_ref"]==str(_rb))
check("返工5 复审二 P1 → run resolved(ops_resolved:refund_confirmed 含 refund_tx)",
      str(q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s",(rt,))[0]["last_settlement_error"]).startswith("ops_resolved:refund_confirmed:"))
check("返工5 复审二 req#4 处置与持久审计同事务(审计行已写)",
      any(a["action"]=="delivery_refund_confirmed" for a in dr.list_settlement_audit(rt)))
q("UPDATE diagnosis_runs SET status_changed_at=NOW()-INTERVAL '2 hours' WHERE run_token=%s", (rt,))
check("返工5 复审二 P1 confirm 后 → 抑告警", asyncio.run(dr.run_diagnosis_sweep()).get("stuck_alerted",0)==0)
# 兼容 refund_points 格式(order_id = 被退 consume 的**数字 id**)· 也应通过(两真实生产格式都接受)
rtp = dr.mint_run_token(); dr.admit_run(11, "r-rp", 731, "s-rp", rtp, "paid")
_trefp = dr.freeze_task_ref(rtp)
q("INSERT INTO point_freezes (user_id, task_ref, status, amount_total, amount_bonus, amount_commission, amount_paid) VALUES (11,%s,'committed',300,0,0,300)", (_trefp,))
_fidp = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_trefp,))[0]["id"]
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=%s, freeze_backend='legacy', freeze_task_ref=%s WHERE run_token=%s", (_fidp, _trefp, rtp))
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (11,'consume','paid',-300,0,%s)", (f"CMT{_fidp}-y",))
_cpp = q("SELECT id FROM point_transactions WHERE type='consume' AND user_id=11 AND order_id=%s", (f"CMT{_fidp}-y",))[0]["id"]
dr.repair_delivery(rtp, "admin(uid=1)", "writeoff", "走退款")
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (11,'refund','paid',300,0,%s)", (str(_cpp),))
_rpp = q("SELECT id FROM point_transactions WHERE type='refund' AND user_id=11 AND order_id=%s", (str(_cpp),))[0]["id"]
check("返工5 复审二 P1 refund_points 格式(order_id=consume 数字 id)也接受 → confirm 通过(两真实格式兼容)",
      dr.repair_delivery(rtp, "admin(uid=1)", "confirm_refund", "refund_points格式", refund_tx_id=str(_rpp))["ok"])
# 🔴 判别6:一退款流水只核销一个 run —— 他 run 引用本 run 的 refund_tx → 拒(run 域查不到 · 且 UNIQUE 兜底)
rt2 = dr.mint_run_token(); dr.admit_run(20, "r-wf3", 722, "s-wf3", rt2, "paid")
_tref2 = dr.freeze_task_ref(rt2)
q("INSERT INTO point_freezes (user_id, task_ref, status, amount_total, amount_bonus, amount_commission, amount_paid) VALUES (20,%s,'committed',650,100,0,550)", (_tref2,))
_fid2 = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_tref2,))[0]["id"]
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=%s, freeze_backend='legacy', freeze_task_ref=%s WHERE run_token=%s", (_fid2, _tref2, rt2))
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (20,'consume','bonus',-100,0,%s)", (f"CMT{_fid2}-x",))
q("INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,order_id) VALUES (20,'consume','paid',-550,0,%s)", (f"CMT{_fid2}-x",))
dr.repair_delivery(rt2, "admin(uid=1)", "writeoff", "第二 run")
check("返工5 复审二 P1 他 run 引用本 run 的 refund_tx → 拒(不属本 run 冻结 · 防一证多用)",
      (not dr.repair_delivery(rt2, "admin(uid=1)", "confirm_refund", "盗用他run退款", refund_tx_id=str(_rb))["ok"])
      and len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (rt2,)))==0)
# v35 backend:customer_credit_freezes(pool tool/publish/bonus)+ customer_credit_transactions
rtv = dr.mint_run_token(); dr.admit_run(30, "r-v35", 730, "s-v35", rtv, "paid")
_trefv = dr.freeze_task_ref(rtv)
_insert_v35_freeze(30, 9, _trefv, "committed", tool=120, publish=80)
_fidv = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_trefv,))[0]["id"]
q("INSERT INTO customer_agent_credit_wallets (customer_user_id,agent_user_id) VALUES (30,9)")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=%s, freeze_backend='v35', freeze_task_ref=%s WHERE run_token=%s", (_fidv, _trefv, rtv))
_ccT = _insert_credit_tx(30, 9, "consume", "tool", -120, _trefv)
_ccP = _insert_credit_tx(30, 9, "consume", "publish", -80, _trefv)
dr.repair_delivery(rtv, "admin(uid=1)", "writeoff", "v35 走退款")
check("返工5 复审二 P1 v35 无真实退款流水 → confirm 拒(保持 refund_pending)",
      not dr.repair_delivery(rtv, "admin(uid=1)", "confirm_refund", "v35无退款", refund_tx_id="888888")["ok"])
# 🔴 判别(复审二2):v35 release 格式退款(related_order_id=task_ref · RLS)→ 拒(非已提交扣费退款 · 不违反拒 RLS 不变式)
_rlsv = _insert_credit_tx(30, 9, "refund", "tool", 120, _trefv)
check("返工5 复审二2 P1 v35 release 格式退款(related_order_id=task_ref)→ 拒(RLS 非已提交扣费退款)",
      not dr.repair_delivery(rtv, "admin(uid=1)", "confirm_refund", "release格式", refund_tx_id=str(_rlsv))["ok"])
q("DELETE FROM customer_credit_transactions WHERE id=%s", (_rlsv,))
_rvt = _insert_credit_tx(30, 9, "refund", "tool", 120, _ccT)
_insert_credit_tx(30, 9, "refund", "publish", 80, _ccP)
_cfv = dr.repair_delivery(rtv, "admin(uid=1)", "confirm_refund", "v35 已核对真实退款", refund_tx_id=str(_rvt))
_recv = q("SELECT points, freeze_backend FROM diagnosis_refund_records WHERE run_token=%s", (rtv,))
check("返工5 复审二 P1 v35 真实 refund 流水(逐池对 tool120+publish80)→ confirm 通过 + 绑定(backend=v35)",
      _cfv["ok"] and len(_recv)==1 and _recv[0]["points"]==200 and _recv[0]["freeze_backend"]=="v35")
# 🔴 判别(复审二2):v35 同 task_ref 多笔 committed/released 冻结 → 退款无法唯一绑定 → fail-closed 拒
#   **隔离歧义闸**:本 run 有**完整 consume + numeric refund**(无歧义闸时 confirm 本会通过)· 拒因必须是歧义闸(校验 error 文案),
#   否则删掉歧义闸此用例仍会因 consume-empty 假绿(审核抓到的测试缺陷)。
rtamb = dr.mint_run_token(); dr.admit_run(31, "r-amb", 732, "s-amb", rtamb, "paid")
_trefa = dr.freeze_task_ref(rtamb)
_insert_v35_freeze(31, 9, _trefa, "released", tool=120, publish=80)
_insert_v35_freeze(31, 9, _trefa, "committed", tool=120, publish=80)
_fida2 = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s AND status='committed'", (_trefa,))[0]["id"]
q("INSERT INTO customer_agent_credit_wallets (customer_user_id,agent_user_id) VALUES (31,9)")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=%s, freeze_backend='v35', freeze_task_ref=%s WHERE run_token=%s", (_fida2, _trefa, rtamb))
_caT = _insert_credit_tx(31, 9, "consume", "tool", -120, _trefa)
_caP = _insert_credit_tx(31, 9, "consume", "publish", -80, _trefa)
_rva = _insert_credit_tx(31, 9, "refund", "tool", 120, _caT)
_insert_credit_tx(31, 9, "refund", "publish", 80, _caP)
dr.repair_delivery(rtamb, "admin(uid=1)", "writeoff", "amb")
_amb = dr.repair_delivery(rtamb, "admin(uid=1)", "confirm_refund", "多冻结歧义", refund_tx_id=str(_rva))
check("返工5 复审二2 P1 v35 同 task_ref 多笔 committed/released 冻结 → fail-closed 拒(**歧义闸隔离**:有完整 consume+refund 本可通过·拒因=歧义闸)",
      (not _amb["ok"]) and ("多笔 committed/released" in str(_amb.get("error", ""))))
# 未 writeoff 直接 confirm → 拒
rt3 = dr.mint_run_token(); dr.admit_run(10, "r-cf3", 723, "s-cf3", rt3, "paid")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending' WHERE run_token=%s", (rt3,))
check("返工4 P1 未 writeoff 直接 confirm → 拒(必先 refund_pending)",
      not dr.repair_delivery(rt3, "admin(uid=1)", "confirm_refund", "直接确认", refund_tx_id="1")["ok"])
# 无冻结绑定 → fail-closed 拒
rt4 = dr.mint_run_token(); dr.admit_run(10, "r-nofz", 724, "s-nofz", rt4, "paid")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=NULL, freeze_backend=NULL WHERE run_token=%s", (rt4,))
dr.repair_delivery(rt4, "admin(uid=1)", "writeoff", "无冻结")
check("返工5 复审二 P1 confirm 无冻结绑定 → fail-closed 拒",
      not dr.repair_delivery(rt4, "admin(uid=1)", "confirm_refund", "无冻结", refund_tx_id="1")["ok"])
# 源码锁:confirm 零 recharge_orders/refund_work_orders 耦合 + 不写 billing(只读核验)
_dr_src = (Path(__file__).resolve().parent.parent / "services" / "diagnosis_runs.py").read_text(encoding="utf-8")
check("返工5 复审二 req#3 confirm 零 recharge_orders 耦合(源码不 import/调 get_refund_work_order)",
      ("refund_work_order_db import get_refund_work_order" not in _dr_src) and ("get_refund_work_order(" not in _dr_src))

print("\n== [返工5-复审 修复净增量] exempt(免单)run delivery_repair_pending → writeoff 直接 ops_resolved(抑告警)· paid 异常不放宽 ==")
wipe()
# 🔴 判别:exempt(freeze_id NULL · 无扣费)run 卡 delivery_repair_pending —— confirm 金额核验恒 fail-closed · 唯一收口=writeoff 直达 resolved
rt = dr.mint_run_token(); dr.admit_run(30, "r-exm", 725, "s-exm", rt, "exempt")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=NULL, freeze_backend=NULL WHERE run_token=%s", (rt,))
_woe = dr.repair_delivery(rt, "admin(uid=1)", "writeoff", "免单无产物·无需退款")
_lse = q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]["last_settlement_error"]
check("返工5-复审 exempt writeoff → ops_resolved:writeoff_exempt(抑告警 · 不进 refund_pending)",
      _woe["ok"] and str(_lse).startswith("ops_resolved:writeoff_exempt:"))
q("UPDATE diagnosis_runs SET status_changed_at=NOW()-INTERVAL '2 hours' WHERE run_token=%s", (rt,))
check("返工5-复审 exempt 收口后 → 抑告警(sweeper 不再 stuck · 消除告警风暴)",
      asyncio.run(dr.run_diagnosis_sweep()).get("stuck_alerted", 0) == 0)
# 已卡 refund_pending 的 exempt run → writeoff 救回(rescue · 置于幂等短路前)
rt2 = dr.mint_run_token(); dr.admit_run(30, "r-exm2", 726, "s-exm2", rt2, "exempt")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=NULL, "
  "last_settlement_error='refund_pending:writeoff:by=old' WHERE run_token=%s", (rt2,))
_woe2 = dr.repair_delivery(rt2, "admin(uid=1)", "writeoff", "救回已卡 exempt")
check("返工5-复审 已卡 refund_pending 的 exempt run → writeoff 救回 ops_resolved(rescue)",
      _woe2["ok"] and str(q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s", (rt2,))[0]["last_settlement_error"]).startswith("ops_resolved:writeoff_exempt:"))
# 🔴 paid 异常(freeze_id NULL)**不放宽**:writeoff→refund_pending · confirm 仍 fail-closed(强制人工核对冻结)
rt3 = dr.mint_run_token(); dr.admit_run(30, "r-paidanom", 727, "s-paidanom", rt3, "paid")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=NULL, freeze_backend=NULL WHERE run_token=%s", (rt3,))
dr.repair_delivery(rt3, "admin(uid=1)", "writeoff", "paid异常")
check("返工5-复审 paid 异常(freeze NULL)writeoff → 仍 refund_pending(不放宽 · 仅 exempt 直达 resolved)",
      str(q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s", (rt3,))[0]["last_settlement_error"]).startswith("refund_pending:"))
check("返工5-复审 paid 异常 confirm → 仍 fail-closed 拒(强制人工核对冻结 · 不误 resolved)",
      not dr.repair_delivery(rt3, "admin(uid=1)", "confirm_refund", "试图确认", refund_tx_id="1")["ok"])

print("\n== [返工4 修复净增量] republish 与退款互斥:refund_pending/confirmed 后即便产物完整也拒补发(防双重受益)==")
rt3 = dr.mint_run_token(); dr.admit_run(10, "r-mx", 723, "s-mx", rt3, "paid")
# 完整产物(total_score 非空)· 若无 refund 互斥守卫 republish 会成功 → 退款+交付双重受益
q("INSERT INTO diagnosis_records (session_id, brand_name, brand_id, result_visibility, total_score) "
  "VALUES ('s-mx','B',723,'pending',88) ON CONFLICT (session_id) DO NOTHING")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending' WHERE run_token=%s", (rt3,))
dr.repair_delivery(rt3, "admin(uid=1)", "writeoff", "已开退款工单")   # → refund_pending
_rp_mx = dr.repair_delivery(rt3, "admin(uid=1)", "republish", "退款后想补发(产物完整)")
row_mx = q("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (rt3,))[0]
check("返工4 修复净增量 refund_pending 后产物完整 republish → **拒**(退款与补发互斥·防双重受益·run 未被 committed)",
      (not _rp_mx["ok"]) and row_mx["run_status"] == "delivery_repair_pending")
q("DELETE FROM diagnosis_runs"); q("DELETE FROM diagnosis_records")

print("\n== [返工5 P1] list_delivery_repair 活跃队列排除已核销(resolved 不挤占 LIMIT · 新 awaiting 必现)==")
wipe()
# 2 条已核销(老时间)+ 1 条 awaiting(新时间)· limit=2 活跃查询:老代码返 2 条 resolved(awaiting 消失);新代码排除 resolved
for tk_suffix, uid in [("res1", 801), ("res2", 802)]:
    _r = dr.mint_run_token(); dr.admit_run(10, f"r-{tk_suffix}", uid, f"s-{tk_suffix}", _r, "paid")
    q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', "
      "last_settlement_error='ops_resolved:refund_confirmed:ticket=1:by=op', "
      "status_changed_at=NOW()-INTERVAL '10 hours' WHERE run_token=%s", (_r,))
_await = dr.mint_run_token(); dr.admit_run(10, "r-await", 803, "s-await", _await, "paid")
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', status_changed_at=NOW() WHERE run_token=%s", (_await,))
_active = dr.list_delivery_repair(limit=2)
_active_tokens = {r["run_token"] for r in _active}
check("返工5 P1 活跃队列(limit=2)含新 awaiting(未被 2 条老 resolved 挤出)", _await in _active_tokens)
check("返工5 P1 活跃队列排除已核销 resolved", all(not r.get("resolved") for r in _active))
_hist = dr.list_delivery_repair(limit=10, resolved_only=True)
check("返工5 P1 历史分页(resolved_only)返已核销 2 条", len([r for r in _hist if r.get("resolved")]) == 2)

print("\n== [返工5 P2] settlement 队列纳入 manual_resolving(只读 · is_resolving/lease_state · resolvable=False)==")
wipe()
rt = dr.mint_run_token(); dr.admit_run(10, "r-mr", 810, "s-mr", rt, "paid")
q("UPDATE diagnosis_runs SET run_status='manual_resolving', manual_resolution_token='res_x', "
  "manual_lease_until=NOW()+INTERVAL '60 seconds' WHERE run_token=%s", (rt,))
_sm = dr.list_settlement_manual(limit=100)
_mr = [r for r in _sm if r["run_token"] == rt]
check("返工5 P2 manual_resolving run 在 settlement 队列可见(否则后台盲区)", len(_mr) == 1)
check("返工5 P2 manual_resolving 标 is_resolving=True · resolvable=False(前端禁直接 resolve)",
      bool(_mr) and _mr[0].get("is_resolving") is True and _mr[0].get("resolvable") is False)
check("返工5 P2 有效租约 → lease_state=processing", bool(_mr) and _mr[0].get("lease_state") == "processing")
q("UPDATE diagnosis_runs SET manual_lease_until=NOW()-INTERVAL '10 seconds' WHERE run_token=%s", (rt,))
_mr2 = [r for r in dr.list_settlement_manual(limit=100) if r["run_token"] == rt]
check("返工5 P2 租约过期 → lease_state=expired_await_takeover", bool(_mr2) and _mr2[0].get("lease_state") == "expired_await_takeover")

print("\n== [返工5 P0] 占位 INSERT 失败 fail-closed(源码锁 · 锚定唯一 503 detail 码 · 防回退 fail-open 泄露)==")
_srv_src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
check("返工5 P0 占位失败 → raise HTTPException 503(唯一 detail 码 · 真实 raise 非注释)",
      '诊断初始化失败,请稍后再试' in _srv_src)
check("返工5 P0 旧 fail-open 吞异常继续路径已移除(不再 '产物由 save_diagnosis 建')",
      'fail-open · 产物由 save_diagnosis 建' not in _srv_src)

# ============================================================================
# [P1-1 + P1-2] v35 owner 精确绑定 + v35 交付缺失精确退款执行闭环
# ============================================================================
def _seed_v35_repair(owner, agent, tool, publish, bonus, freeze_owner=None, freeze_status='committed',
                     freeze_agent=None, wallet_agent=None, consume_agents=None):
    """建 v35 冻结 + 钱包 + consume 流水 → delivery_repair_pending → writeoff → refund_pending。
    freeze_owner!=None → 冻结 customer_user_id 用 freeze_owner(测 owner 错绑 · run.owner≠冻结 owner)。
    freeze_agent/wallet_agent/consume_agents 仅用于服务商归属漂移判别;默认全部等于 agent。
    返回 (run_token, freeze_id, task_ref, {pool: consume_id})。"""
    fowner = freeze_owner if freeze_owner is not None else owner
    fagent = agent if freeze_agent is None else freeze_agent
    wagent = agent if wallet_agent is None else wallet_agent
    consume_agents = consume_agents or {}
    rt = dr.mint_run_token(); dr.admit_run(owner, "r-" + rt[-8:], None, "s-" + rt[-8:], rt, "paid")
    tref = dr.freeze_task_ref(rt)
    for uid in {int(fowner), int(agent), int(fagent), int(wagent), *(int(v) for v in consume_agents.values())}:
        q("INSERT INTO users(id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (uid,))
    q("INSERT INTO customer_agent_credit_wallets (customer_user_id, agent_user_id, tool_credit_points, publish_credit_points, bonus_credit_points) "
      "VALUES (%s,%s,0,0,0) ON CONFLICT (customer_user_id) DO NOTHING", (fowner, wagent))
    fid = _insert_v35_freeze(fowner, fagent, tref, freeze_status, tool=tool, publish=publish, bonus=bonus)
    cmap = {}
    for pool, amt in (("tool", tool), ("publish", publish), ("bonus", bonus)):
        if amt <= 0:
            continue
        cmap[pool] = _insert_credit_tx(
            fowner, consume_agents.get(pool, agent), "consume", pool, -amt, tref, "tool_consume")
    q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=%s, freeze_backend='v35', freeze_task_ref=%s WHERE run_token=%s", (fid, tref, rt))
    writeoff = dr.repair_delivery(rt, "admin(uid=1)", "writeoff", "v35 走退款")
    if not writeoff.get("ok"):
        raise RuntimeError(f"v35 fixture writeoff 失败:{writeoff}")
    return rt, fid, tref, cmap

def _wallet(owner):
    r = q("SELECT customer_user_id,agent_user_id,tool_credit_points,publish_credit_points,bonus_credit_points,"
          "total_purchased_points,total_consumed_points FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (owner,))
    return r[0] if r else None

def _v35_refund_rows(owner):
    return q("SELECT count(*) n FROM customer_credit_transactions WHERE type='refund' AND source='diagnosis_delivery_refund' AND customer_user_id=%s", (owner,))[0]["n"]

def _v35_refund_snapshot(owner, run_token):
    """自动退款调用前后逐表全列快照(含归属/时间/描述,防只比余额或 count 假绿)。"""
    return {
        # 每个判别用例先 wipe；抓三张资金表全量，确保错 owner 候选也纳入“零变化”证明。
        "wallet": q("SELECT * FROM customer_agent_credit_wallets ORDER BY customer_user_id"),
        "freeze": q("SELECT * FROM customer_credit_freezes ORDER BY id"),
        "transactions": q("SELECT * FROM customer_credit_transactions ORDER BY id"),
        "runs": q("SELECT * FROM diagnosis_runs ORDER BY run_token"),
        "records": q("SELECT * FROM diagnosis_refund_records ORDER BY id"),
        "audit": q("SELECT * FROM diagnosis_settlement_audit ORDER BY id"),
    }

def _refund_agents(owner):
    return q("SELECT id,agent_user_id,pool,points,related_order_id FROM customer_credit_transactions "
             "WHERE customer_user_id=%s AND type='refund' AND source='diagnosis_delivery_refund' ORDER BY id", (owner,))

def _call_v35_with_writer_count(run_token, note):
    """调用真实执行边界并计数 refund_credit；归属拒绝用例必须在 writer 调用 0 次时返回。"""
    from services import customer_credit as customer_credit_service
    calls = {"n": 0}
    original = customer_credit_service.refund_credit

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    customer_credit_service.refund_credit = counted
    try:
        out = dr.refund_and_confirm_v35_delivery(run_token, "admin(uid=1)", note)
    finally:
        customer_credit_service.refund_credit = original
    return out, calls["n"]

def _is_safe_agent_ownership_error(out):
    """结构化码供程序识别；运营文案只讲处置，不泄露工程字段、内部 ID 或底层实现。"""
    return (
        (not out.get("ok"))
        and out.get("error_code") == "V35_AGENT_OWNERSHIP_MISMATCH"
        and out.get("error") == "客户额度账户的服务商归属信息不一致，退款确认未完成，请人工核对"
        and not any(term in str(out.get("error", "")) for term in ("agent_user_id", "v35", "CAS", "ledger"))
    )

def _is_safe_customer_ownership_error(out):
    """错客户归属也必须结构化且仅展示运营可理解的处置说明。"""
    return (
        (not out.get("ok"))
        and out.get("error_code") == "V35_CUSTOMER_OWNERSHIP_MISMATCH"
        and out.get("error") == "资金记录的客户归属信息不一致，退款确认未完成，请人工核对"
        and not any(term in str(out.get("error", "")) for term in ("customer_user_id", "v35", "CAS", "ledger"))
    )

print("\n== [P1-2] v35 交付缺失精确退款执行:正确 owner 完整退款成功(退回原池 + 建记录 + resolved)==")
wipe()
rt, fid, tref, cmap = _seed_v35_repair(40, 9, 120, 80, 0)
_b0 = _wallet(40)
_out = dr.refund_and_confirm_v35_delivery(rt, "admin(uid=1)", "核对无交付·执行退款")
check("P1-2 v35 精确退款成功(total=200)", _out.get("ok") and _out.get("total") == 200)
_b1 = _wallet(40)
check("P1-2 v35 退回原池(tool +120 · publish +80)",
      _b1["tool_credit_points"] == _b0["tool_credit_points"] + 120 and _b1["publish_credit_points"] == _b0["publish_credit_points"] + 80)
_rec = q("SELECT freeze_backend, points, pool_split, refund_tx_ref FROM diagnosis_refund_records WHERE run_token=%s", (rt,))
check("P1-2 v35 建 run-bound 退款记录(backend=v35 · points=200)", len(_rec) == 1 and _rec[0]["freeze_backend"] == "v35" and _rec[0]["points"] == 200)
check("P1-2 v35 refund 流水 source=diagnosis_delivery_refund 逐池绑 consume.id",
      len(q("SELECT 1 FROM customer_credit_transactions WHERE type='refund' AND source='diagnosis_delivery_refund' AND customer_user_id=40 AND pool='tool' AND related_order_id=%s", (str(cmap["tool"]),))) == 1
      and len(q("SELECT 1 FROM customer_credit_transactions WHERE type='refund' AND source='diagnosis_delivery_refund' AND customer_user_id=40 AND pool='publish' AND related_order_id=%s", (str(cmap["publish"]),))) == 1)
_success_refunds = _refund_agents(40)
check("P1-2 v35 成功退款逐条继承原冻结服务商(agent=9)",
      len(_success_refunds) == 2 and all(int(row["agent_user_id"]) == 9 for row in _success_refunds))
check("P1-2 v35 run → ops_resolved:refund_confirmed",
      str(q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s", (rt,))[0]["last_settlement_error"]).startswith("ops_resolved:refund_confirmed:"))
check("P1-2 v35 同事务审计写入(delivery_v35_refund_executed)",
      any(a["action"] == "delivery_v35_refund_executed" for a in dr.list_settlement_audit(rt)))
# 重试幂等(不重复退款)
_out2 = dr.refund_and_confirm_v35_delivery(rt, "admin(uid=1)", "重试")
check("P1-2 v35 重试 → 幂等(不重复退款)", _out2.get("idempotent") is True)
check("P1-2 v35 重试后钱包零变化(零双退)", _wallet(40) == _b1 and _v35_refund_rows(40) == 2)

# publish-only / tool-only 与 tool+publish 同用例组，证明每个受支持资金组合可达
wipe()
rt_pub, _, _, _ = _seed_v35_repair(401, 9, 0, 75, 0)
_pub = dr.refund_and_confirm_v35_delivery(rt_pub, "admin(uid=1)", "仅发布额度退款")
check("V3.5 publish-only 自动退款成功", _pub.get("ok") and _wallet(401)["publish_credit_points"] == 75
      and _wallet(401)["tool_credit_points"] == 0)
wipe()
rt_tool, _, _, _ = _seed_v35_repair(402, 9, 60, 0, 0)
_tool = dr.refund_and_confirm_v35_delivery(rt_tool, "admin(uid=1)", "仅工具额度退款")
check("V3.5 tool-only 自动退款成功", _tool.get("ok") and _wallet(402)["tool_credit_points"] == 60
      and _wallet(402)["publish_credit_points"] == 0)

print("\n== [V3.5 refund final] bonus>0 首个写操作前 fail-closed(所有资金/状态/记录/审计零变化)==")
wipe()
rt_bonus, _, _, _bonus_cmap = _seed_v35_repair(403, 9, 40, 20, 30)
_bonus_before = _v35_refund_snapshot(403, rt_bonus)
_bonus_row = [x for x in dr.list_delivery_repair(limit=100) if x["run_token"] == rt_bonus][0]
check("bonus>0 列表明确标人工核对且 can_execute_v35_refund=false",
      _bonus_row.get("bonus_refund_requires_manual") is True and _bonus_row.get("can_execute_v35_refund") is False)
from services import customer_credit as _bonus_cc
_bonus_refund_calls = {"n": 0}
_bonus_orig_refund_credit = _bonus_cc.refund_credit
def _bonus_forbidden_refund(*a, **k):
    _bonus_refund_calls["n"] += 1
    return _bonus_orig_refund_credit(*a, **k)
_bonus_cc.refund_credit = _bonus_forbidden_refund
try:
    _bonus_out = dr.refund_and_confirm_v35_delivery(rt_bonus, "admin(uid=1)", "赠送额度退款测试")
finally:
    _bonus_cc.refund_credit = _bonus_orig_refund_credit
_bonus_after = _v35_refund_snapshot(403, rt_bonus)
check("bonus>0 返回结构化 BONUS_REFUND_REQUIRES_MANUAL",
      (not _bonus_out.get("ok")) and _bonus_out.get("error_code") == "BONUS_REFUND_REQUIRES_MANUAL")
check("bonus>0 在首个钱包 writer 前拒绝(refund_credit 调用 0 次)", _bonus_refund_calls["n"] == 0)
check("bonus>0 拒绝后钱包/流水/run/refund record/审计全部零变化", _bonus_after == _bonus_before)

# 财务人工退款完成后，复用 confirm_refund 的真实流水只读核验闭环；该路径不得调用自动 refund_credit。
# 先只造 tool/publish、故意缺 bonus，证明三池不完整仍保持 pending；再补齐 bonus 走成功路径。
_bonus_manual_refunds = {}
for _pool, _amount in (("tool", 40), ("publish", 20)):
    _bonus_manual_refunds[_pool] = _insert_credit_tx(
        403, 9, "refund", _pool, _amount, _bonus_cmap[_pool], "admin_adjust")
q("UPDATE customer_agent_credit_wallets SET tool_credit_points=40, publish_credit_points=20 "
  "WHERE customer_user_id=403")
_bonus_missing_before = _v35_refund_snapshot(403, rt_bonus)
_bonus_missing_out = dr.repair_delivery(
    rt_bonus, "admin(uid=1)", "confirm_refund", "财务退款仍缺赠送额度流水",
    refund_tx_id=str(_bonus_manual_refunds["tool"]),
)
check("bonus>0 人工 confirm 缺 bonus refund 流水 → 拒且保持 refund_pending/全表零变化",
      (not _bonus_missing_out.get("ok"))
      and _v35_refund_snapshot(403, rt_bonus) == _bonus_missing_before
      and str(q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s", (rt_bonus,))[0]["last_settlement_error"])
      .startswith("refund_pending:"))

_bonus_manual_refunds["bonus"] = _insert_credit_tx(
    403, 9, "refund", "bonus", 30, _bonus_cmap["bonus"], "admin_adjust")
q("UPDATE customer_agent_credit_wallets SET bonus_credit_points=30 WHERE customer_user_id=403")
_bonus_manual_wallet_before = _wallet(403)
_bonus_manual_tx_before = q("SELECT * FROM customer_credit_transactions ORDER BY id")
_bonus_manual_writer_calls = {"n": 0}
def _bonus_manual_forbidden_writer(*a, **k):
    _bonus_manual_writer_calls["n"] += 1
    raise AssertionError("confirm_refund 不得调用 refund_credit")
_bonus_cc.refund_credit = _bonus_manual_forbidden_writer
try:
    _bonus_manual_out = dr.repair_delivery(
        rt_bonus, "admin(uid=1)", "confirm_refund", "财务已人工核对并完成赠送额度退款",
        refund_tx_id=str(_bonus_manual_refunds["bonus"]),
    )
finally:
    _bonus_cc.refund_credit = _bonus_orig_refund_credit
_bonus_manual_record = q(
    "SELECT freeze_backend, points, refund_tx_ref FROM diagnosis_refund_records WHERE run_token=%s",
    (rt_bonus,),
)
_bonus_manual_run = q(
    "SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s", (rt_bonus,),
)[0]
_bonus_manual_audit = q(
    "SELECT action FROM diagnosis_settlement_audit WHERE run_token=%s ORDER BY id", (rt_bonus,),
)
check("bonus>0 财务真实 tool/publish/bonus refund 流水 → 人工 confirm_refund 可核销",
      _bonus_manual_out.get("ok") and len(_bonus_manual_record) == 1
      and _bonus_manual_record[0]["freeze_backend"] == "v35"
      and int(_bonus_manual_record[0]["points"]) == 90
      and str(_bonus_manual_record[0]["refund_tx_ref"]) == str(_bonus_manual_refunds["bonus"])
      and str(_bonus_manual_run["last_settlement_error"]).startswith("ops_resolved:refund_confirmed:")
      and [r["action"] for r in _bonus_manual_audit].count("delivery_refund_confirmed") == 1)
check("bonus>0 人工 confirm 只读核验:不再改钱包/流水且自动 refund_credit 仍 0 次",
      _wallet(403) == _bonus_manual_wallet_before
      and q("SELECT * FROM customer_credit_transactions ORDER BY id") == _bonus_manual_tx_before
      and _bonus_manual_writer_calls["n"] == 0)
_bonus_retry_before = _v35_refund_snapshot(403, rt_bonus)
_bonus_retry = dr.repair_delivery(
    rt_bonus, "admin(uid=1)", "confirm_refund", "重复确认人工赠送额度退款",
    refund_tx_id=str(_bonus_manual_refunds["bonus"]),
)
check("bonus>0 人工 confirm 重试幂等且钱包/流水/run/记录/审计零新增",
      _bonus_retry.get("ok") and _bonus_retry.get("idempotent") is True
      and _v35_refund_snapshot(403, rt_bonus) == _bonus_retry_before)

print("\n== [V3.5 bonus 人工确认] 20 并发只核销一次且绝不调用自动 writer ==")
wipe()
_rt_manual_c20, _, _, _manual_c20_cmap = _seed_v35_repair(492, 9, 40, 20, 30)
_manual_c20_ref = None
for _pool, _amount in (("tool", 40), ("publish", 20), ("bonus", 30)):
    _rid = _insert_credit_tx(492, 9, "refund", _pool, _amount, _manual_c20_cmap[_pool], "admin_adjust")
    _manual_c20_ref = _manual_c20_ref or _rid
q("UPDATE customer_agent_credit_wallets SET tool_credit_points=40, publish_credit_points=20, "
  "bonus_credit_points=30 WHERE customer_user_id=492")
_manual_c20_wallet_before = _wallet(492)
_manual_c20_tx_before = q("SELECT * FROM customer_credit_transactions ORDER BY id")
import threading as _manual_threading
_manual_c20_barrier = _manual_threading.Barrier(20)
_manual_c20_writer_calls = {"n": 0}
def _manual_c20_forbidden_writer(*a, **k):
    _manual_c20_writer_calls["n"] += 1
    raise AssertionError("人工 confirm 并发不得调用 refund_credit")
def _manual_c20_confirm(i):
    _manual_c20_barrier.wait(timeout=30)
    return dr.repair_delivery(
        _rt_manual_c20, f"admin-manual-{i}(uid={i})", "confirm_refund", "并发确认人工赠送额度退款",
        refund_tx_id=str(_manual_c20_ref),
    )
_bonus_cc.refund_credit = _manual_c20_forbidden_writer
try:
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        _manual_c20_results = list(ex.map(_manual_c20_confirm, range(20)))
finally:
    _bonus_cc.refund_credit = _bonus_orig_refund_credit
_manual_c20_real = [r for r in _manual_c20_results if r.get("ok") and not r.get("idempotent")]
_manual_c20_idem = [r for r in _manual_c20_results if r.get("ok") and r.get("idempotent")]
check("V3.5 bonus 人工 confirm 并发 20 → 恰 1 次真实核销 + 19 次幂等",
      len(_manual_c20_real) == 1 and len(_manual_c20_idem) == 19)
check("V3.5 bonus 人工 confirm 并发 20 → 一条 record/一条完成审计",
      len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (_rt_manual_c20,))) == 1
      and len(q("SELECT 1 FROM diagnosis_settlement_audit WHERE run_token=%s "
                "AND action='delivery_refund_confirmed'", (_rt_manual_c20,))) == 1)
check("V3.5 bonus 人工 confirm 并发 20 → 钱包/退款流水不变且自动 writer 0 次",
      _wallet(492) == _manual_c20_wallet_before
      and q("SELECT * FROM customer_credit_transactions ORDER BY id") == _manual_c20_tx_before
      and _manual_c20_writer_calls["n"] == 0)

print("\n== [V3.5 bonus 人工确认] 服务端 paid+committed 硬门(禁止仅信前端) ==")
for _owner, _freeze_status in ((493, "frozen"), (494, "released")):
    wipe()
    _rt_state, _, _, _state_cmap = _seed_v35_repair(
        _owner, 9, 40, 20, 30, freeze_status=_freeze_status)
    _state_ref = None
    for _pool, _amount in (("tool", 40), ("publish", 20), ("bonus", 30)):
        _rid = _insert_credit_tx(_owner, 9, "refund", _pool, _amount, _state_cmap[_pool], "admin_adjust")
        _state_ref = _state_ref or _rid
    _state_before = _v35_refund_snapshot(_owner, _rt_state)
    _state_out = dr.repair_delivery(
        _rt_state, "admin(uid=1)", "confirm_refund", f"拒绝 {_freeze_status} 冻结人工确认",
        refund_tx_id=str(_state_ref),
    )
    check(f"V3.5 bonus 人工 confirm freeze={_freeze_status} → 结构化拒绝且全表零变化",
          (not _state_out.get("ok"))
          and _state_out.get("error_code") == "V35_REFUND_FREEZE_NOT_COMMITTED"
          and _v35_refund_snapshot(_owner, _rt_state) == _state_before)

wipe()
_rt_exempt_confirm, _, _, _exempt_cmap = _seed_v35_repair(495, 9, 40, 20, 30)
for _pool, _amount in (("tool", 40), ("publish", 20), ("bonus", 30)):
    _insert_credit_tx(495, 9, "refund", _pool, _amount, _exempt_cmap[_pool], "admin_adjust")
q("UPDATE diagnosis_runs SET billing_mode='exempt' WHERE run_token=%s", (_rt_exempt_confirm,))
_exempt_before = _v35_refund_snapshot(495, _rt_exempt_confirm)
_exempt_out = dr.repair_delivery(
    _rt_exempt_confirm, "admin(uid=1)", "confirm_refund", "拒绝非付费 run 人工确认",
    refund_tx_id=str(q("SELECT id FROM customer_credit_transactions WHERE customer_user_id=495 "
                     "AND type='refund' ORDER BY id LIMIT 1")[0]["id"]),
)
check("V3.5 bonus 人工 confirm billing_mode!=paid → 结构化拒绝且全表零变化",
      (not _exempt_out.get("ok"))
      and _exempt_out.get("error_code") == "REFUND_CONFIRM_REQUIRES_PAID"
      and _v35_refund_snapshot(495, _rt_exempt_confirm) == _exempt_before)

print("\n== [V3.5 refund ownership] freeze/wallet/consume/refund 服务商漂移在首个 writer 前 fail-closed ==")
for _case, _owner, _tool, _publish, _seed_kwargs in (
    ("冻结 agent 漂移", 404, 100, 0, {"freeze_agent": 8}),
    ("钱包 agent 漂移", 405, 100, 0, {"wallet_agent": 8}),
    ("第二池 consume agent 漂移", 406, 60, 40, {"consume_agents": {"publish": 8}}),
):
    wipe()
    _rt_bad, _, _, _ = _seed_v35_repair(_owner, 9, _tool, _publish, 0, **_seed_kwargs)
    _bad_before = _v35_refund_snapshot(_owner, _rt_bad)
    _bad_out, _bad_writer_calls = _call_v35_with_writer_count(_rt_bad, f"{_case}拒绝测试")
    check(f"{_case} → V35_AGENT_OWNERSHIP_MISMATCH",
          _is_safe_agent_ownership_error(_bad_out))
    check(f"{_case} → refund_credit 0 次且钱包/全流水/run/记录/审计零变化",
          _bad_writer_calls == 0 and _v35_refund_snapshot(_owner, _rt_bad) == _bad_before)

wipe()
_rt_bad_refund, _, _, _bad_refund_map = _seed_v35_repair(407, 9, 60, 40, 0)
_insert_credit_tx(407, 9, "refund", "tool", 60, str(_bad_refund_map["tool"]), "tool_fail_refund")
_insert_credit_tx(407, 8, "refund", "publish", 40, str(_bad_refund_map["publish"]), "tool_fail_refund")
_bad_refund_before = _v35_refund_snapshot(407, _rt_bad_refund)
_bad_refund_out, _bad_refund_writer_calls = _call_v35_with_writer_count(
    _rt_bad_refund, "既存退款服务商漂移拒绝测试")
check("既存绑定 refund agent 漂移 → V35_AGENT_OWNERSHIP_MISMATCH",
      _is_safe_agent_ownership_error(_bad_refund_out))
check("既存绑定 refund agent 漂移 → writer 0 次且所有表/钱包零变化",
      _bad_refund_writer_calls == 0
      and _v35_refund_snapshot(407, _rt_bad_refund) == _bad_refund_before)

wipe()
_rt_existing_valid, _, _, _existing_valid_map = _seed_v35_repair(414, 9, 60, 40, 0)
_insert_credit_tx(
    414, 9, "refund", "tool", 60, str(_existing_valid_map["tool"]), "tool_fail_refund")
_insert_credit_tx(
    414, 9, "refund", "publish", 40, str(_existing_valid_map["publish"]), "tool_fail_refund")
_existing_valid_before = _v35_refund_snapshot(414, _rt_existing_valid)
_existing_valid_out, _existing_valid_writer_calls = _call_v35_with_writer_count(
    _rt_existing_valid, "已有合法退款防重复测试")
check("既存 tool+publish refund 均归属/正号/池合法 → EXISTING_REFUND_REQUIRES_MANUAL",
      (not _existing_valid_out.get("ok"))
      and _existing_valid_out.get("error_code") == "EXISTING_REFUND_REQUIRES_MANUAL")
check("既存合法 refund → 首个 writer 前拒绝且全量资金/状态/记录/审计零变化",
      _existing_valid_writer_calls == 0
      and _v35_refund_snapshot(414, _rt_existing_valid) == _existing_valid_before)

# 人工 confirm_refund 共用只读核验也必须逐条拒绝服务商漂移，不能只保护自动执行端点。
wipe()
_rt_manual_agent, _, _, _manual_agent_map = _seed_v35_repair(408, 9, 60, 40, 0)
_manual_good_refund_id = _insert_credit_tx(
    408, 9, "refund", "tool", 60, str(_manual_agent_map["tool"]), "tool_fail_refund")
_insert_credit_tx(
    408, 8, "refund", "publish", 40, str(_manual_agent_map["publish"]), "tool_fail_refund")
_manual_agent_before = _v35_refund_snapshot(408, _rt_manual_agent)
_manual_agent_out = dr.repair_delivery(
    _rt_manual_agent, "admin(uid=1)", "confirm_refund", "人工退款服务商漂移",
    refund_tx_id=str(_manual_good_refund_id))
check("人工 confirm_refund 的 refund agent 漂移 → 同一结构化归属错误",
      _is_safe_agent_ownership_error(_manual_agent_out))
check("人工 confirm_refund 的 refund agent 漂移 → 钱包/全流水/run/记录/审计零变化",
      _v35_refund_snapshot(408, _rt_manual_agent) == _manual_agent_before)

# 混合证据判别：正确 owner/agent 的完整证据与错 owner+agent 坏行并存时，SQL 不得先按 owner 筛掉坏行。
wipe()
_rt_mixed_consume, _, _tref_mixed_consume, _ = _seed_v35_repair(411, 9, 100, 0, 0)
_insert_credit_tx(9411, 9, "consume", "tool", -100, _tref_mixed_consume, "tool_consume")
_mixed_consume_before = _v35_refund_snapshot(411, _rt_mixed_consume)
_mixed_consume_out, _mixed_consume_writer_calls = _call_v35_with_writer_count(
    _rt_mixed_consume, "混合客户扣费证据拒绝测试")
check("正确 consume + 仅错 owner（agent 正确）consume 混合存在 → 全量取证后结构化拒绝",
      _is_safe_customer_ownership_error(_mixed_consume_out))
check("混合 consume 错归属 → writer 0 次且三张资金表/run/记录/审计全量零变化",
      _mixed_consume_writer_calls == 0
      and _v35_refund_snapshot(411, _rt_mixed_consume) == _mixed_consume_before)

wipe()
_rt_mixed_refund, _, _, _mixed_refund_map = _seed_v35_repair(412, 9, 60, 40, 0)
_insert_credit_tx(412, 9, "refund", "tool", 60, str(_mixed_refund_map["tool"]), "tool_fail_refund")
_insert_credit_tx(9412, 9, "refund", "publish", 40, str(_mixed_refund_map["publish"]), "tool_fail_refund")
_mixed_refund_before = _v35_refund_snapshot(412, _rt_mixed_refund)
_mixed_refund_out, _mixed_refund_writer_calls = _call_v35_with_writer_count(
    _rt_mixed_refund, "混合客户退款证据拒绝测试")
check("正确 refund + 仅错 owner（agent 正确）refund 混合存在 → 全量取证后结构化拒绝",
      _is_safe_customer_ownership_error(_mixed_refund_out))
check("混合 refund 错归属 → writer 0 次且三张资金表/run/记录/审计全量零变化",
      _mixed_refund_writer_calls == 0
      and _v35_refund_snapshot(412, _rt_mixed_refund) == _mixed_refund_before)

print("\n== [P1-1] v35 owner 精确绑定:run.owner ≠ 冻结 customer_user_id → fail-closed(证据属他人绝不误当本 run)==")
wipe()
# run.owner=111 · 冻结/consume/refund 全属 222(完整) · 无 owner 约束时旧代码会覆盖 owner=222 核过 → 假闭环
rtm, fidm, trefm, cmapm = _seed_v35_repair(111, 9, 120, 80, 0, freeze_owner=222)
# 系统退款端点:owner=111 锁冻结查空 → 拒
_om = dr.refund_and_confirm_v35_delivery(rtm, "admin(uid=1)", "owner错绑系统退款")
check("P1-1 v35 系统退款 owner 错绑(run=111 冻结=222)→ 结构化 fail-closed 拒",
      _is_safe_customer_ownership_error(_om))
check("P1-1 owner 错绑 → 零退款记录 + 222 钱包零变化(未误退)",
      len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (rtm,))) == 0
      and _wallet(222)["tool_credit_points"] == 0)
# 人工 confirm_refund 路径(_verify_ledger_refund):即便 222 有完整真实退款流水,owner=111 也拒
_rbm = _insert_credit_tx(222, 9, "refund", "tool", 120, str(cmapm["tool"]), "tool_fail_refund")
_insert_credit_tx(222, 9, "refund", "publish", 80, str(cmapm["publish"]), "tool_fail_refund")
check("P1-1 人工 confirm_refund：222 完整真实退款流水 + run.owner=111 → 拒(_verify_ledger_refund owner 约束)",
      not dr.repair_delivery(rtm, "admin(uid=1)", "confirm_refund", "盗用他客户退款流水", refund_tx_id=str(_rbm)).get("ok"))

print("\n== [P1-2] v35 退款拒绝矩阵(错状态/池不符/歧义/backend/未writeoff · 钱包零变化)==")
wipe()
# 非 committed 冻结(released) → 拒
rt_rel, fid_rel, tref_rel, _ = _seed_v35_repair(41, 9, 100, 0, 0, freeze_status='released')
_or = dr.refund_and_confirm_v35_delivery(rt_rel, "admin(uid=1)", "released")
check("P1-2 v35 冻结非 committed(released)→ 拒 + 钱包零变化", (not _or.get("ok")) and _wallet(41)["tool_credit_points"] == 0)
# 资金池不符(consume tool=100 · 篡改冻结 amount_tool=120)→ 拒
rt_p, fid_p, tref_p, _ = _seed_v35_repair(42, 9, 100, 0, 0)
q("UPDATE customer_credit_freezes SET amount_tool=120, amount_total=120 WHERE id=%s", (fid_p,))
_op = dr.refund_and_confirm_v35_delivery(rt_p, "admin(uid=1)", "池不符")
check("P1-2 v35 consume 池 ≠ 冻结拆分 → fail-closed 拒 + 钱包零变化", (not _op.get("ok")) and _wallet(42)["tool_credit_points"] == 0)
# 同 task_ref 多 committed 冻结(歧义)→ 拒
rt_a, fid_a, tref_a, _ = _seed_v35_repair(43, 9, 100, 0, 0)
_insert_v35_freeze(43, 9, tref_a, "committed", tool=100)
_oa = dr.refund_and_confirm_v35_delivery(rt_a, "admin(uid=1)", "歧义")
check("P1-2 v35 同 task_ref 多 committed 冻结 → fail-closed 拒(多笔 committed)+ 钱包零变化",
      (not _oa.get("ok")) and ("多笔 committed" in str(_oa.get("error", ""))) and _wallet(43)["tool_credit_points"] == 0)
# legacy run 走 v35 端点 → 拒
rt_l = dr.mint_run_token(); dr.admit_run(44, "r-lg", None, "s-lg", rt_l, "paid")
_trefl = dr.freeze_task_ref(rt_l)
q("INSERT INTO point_freezes (user_id, task_ref, status, amount_total, amount_bonus, amount_commission, amount_paid) VALUES (44,%s,'committed',100,0,0,100)", (_trefl,))
_fidl = q("SELECT id FROM point_freezes WHERE task_ref=%s", (_trefl,))[0]["id"]
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=%s, freeze_backend='legacy', freeze_task_ref=%s WHERE run_token=%s", (_fidl, _trefl, rt_l))
dr.repair_delivery(rt_l, "admin(uid=1)", "writeoff", "legacy")
_ol = dr.refund_and_confirm_v35_delivery(rt_l, "admin(uid=1)", "legacy走v35端点")
check("P1-2 legacy run 走 v35 退款端点 → 拒(backend != v35)", (not _ol.get("ok")) and ("v35" in str(_ol.get("error", ""))))
# 未 writeoff(awaiting)直接执行 → 拒(必先 refund_pending)
rt_w = dr.mint_run_token(); dr.admit_run(45, "r-w", None, "s-w", rt_w, "paid")
_trefw = dr.freeze_task_ref(rt_w)
q("INSERT INTO customer_agent_credit_wallets (customer_user_id, agent_user_id) VALUES (45,9) ON CONFLICT DO NOTHING")
_insert_v35_freeze(45, 9, _trefw, "committed", tool=100)
_fidw = q("SELECT id FROM customer_credit_freezes WHERE task_ref=%s", (_trefw,))[0]["id"]
q("UPDATE diagnosis_runs SET run_status='delivery_repair_pending', freeze_id=%s, freeze_backend='v35', freeze_task_ref=%s WHERE run_token=%s", (_fidw, _trefw, rt_w))
_ow = dr.refund_and_confirm_v35_delivery(rt_w, "admin(uid=1)", "未writeoff")
check("P1-2 v35 未 writeoff(非 refund_pending)直接执行 → 拒(必先 writeoff)", (not _ow.get("ok")) and _wallet(45)["tool_credit_points"] == 0)
# 错 run_token / freeze_id / task_ref 均须 fail-closed
check("V3.5 错 run_token → fail-closed 拒", not dr.refund_and_confirm_v35_delivery("missing-run-token", "admin(uid=1)", "错误 run").get("ok"))
rt_run, _, tref_run, _ = _seed_v35_repair(461, 9, 100, 0, 0)
tampered_rt = rt_run + "-mismatch"
q("UPDATE diagnosis_runs SET run_token=%s WHERE run_token=%s", (tampered_rt, rt_run))
_wrong_run = dr.refund_and_confirm_v35_delivery(tampered_rt, "admin(uid=1)", "错误 run 绑定")
_wrong_run_row = [x for x in dr.list_delivery_repair(limit=100) if x["run_token"] == tampered_rt][0]
check("V3.5 run_token 与 freeze_task_ref 错绑 → 结构化 fail-closed + 钱包零变化",
      (not _wrong_run.get("ok")) and _wrong_run.get("error_code") == "RUN_FREEZE_TASK_REF_MISMATCH"
      and tref_run != dr.freeze_task_ref(tampered_rt) and _wallet(461)["tool_credit_points"] == 0
      and _wrong_run_row.get("can_execute_v35_refund") is False)
rt_fid, _, _, _ = _seed_v35_repair(46, 9, 100, 0, 0)
q("UPDATE diagnosis_runs SET freeze_id=999999999 WHERE run_token=%s", (rt_fid,))
check("V3.5 错 freeze_id → fail-closed + 钱包零变化",
      (not dr.refund_and_confirm_v35_delivery(rt_fid, "admin(uid=1)", "错误资金记录").get("ok"))
      and _wallet(46)["tool_credit_points"] == 0)
rt_tref, _, _, _ = _seed_v35_repair(47, 9, 100, 0, 0)
q("UPDATE diagnosis_runs SET freeze_task_ref='diag_wrong_task_ref' WHERE run_token=%s", (rt_tref,))
check("V3.5 错 task_ref → fail-closed + 钱包零变化",
      (not dr.refund_and_confirm_v35_delivery(rt_tref, "admin(uid=1)", "错误任务绑定").get("ok"))
      and _wallet(47)["tool_credit_points"] == 0)

print("\n== [V3.5 refund final] 应用层符号守卫(临时移除 DB CHECK，防止约束掩盖代码缺陷/假绿)==")
wipe()
q("ALTER TABLE customer_credit_transactions DROP CONSTRAINT IF EXISTS customer_credit_transactions_consume_refund_points_sign_check")
# 正数 consume：旧 abs(points) 会误当 100 实扣并执行退款；新守卫必须在动钱前拒绝
rt_pos, _, tref_pos, _ = _seed_v35_repair(480, 9, 100, 0, 0)
q("UPDATE customer_credit_transactions SET points=100 WHERE customer_user_id=480 AND type='consume' AND related_order_id=%s", (tref_pos,))
_pos = dr.refund_and_confirm_v35_delivery(rt_pos, "admin(uid=1)", "正数扣费证据")
check("正数 consume 明确拒绝(移除符号守卫会误退款，判别测试非假绿)",
      (not _pos.get("ok")) and _pos.get("error_code") == "INVALID_CONSUME_LEDGER_SIGN" and _wallet(480)["tool_credit_points"] == 0)
# 零 consume 放在冻结期望为 0 的池：旧 abs(0) 会静默通过；新守卫逐条拒绝
wipe()
rt_zero, _, tref_zero, _ = _seed_v35_repair(481, 9, 100, 0, 0)
_insert_credit_tx(481, 9, "consume", "publish", 0, tref_zero, "tool_consume")
_zero = dr.refund_and_confirm_v35_delivery(rt_zero, "admin(uid=1)", "零值扣费证据")
check("零 consume 明确拒绝(零期望池隔离符号闸)",
      (not _zero.get("ok")) and _zero.get("error_code") == "INVALID_CONSUME_LEDGER_SIGN" and _wallet(481)["tool_credit_points"] == 0)
# 负数 refund：旧 abs(amount) 会误当真退款并 resolved；新只读核验必须拒绝
wipe()
rt_neg_ref, _, _, cmap_neg = _seed_v35_repair(482, 9, 100, 0, 0)
_neg_ref_id = _insert_credit_tx(
    482, 9, "refund", "tool", -100, str(cmap_neg["tool"]), "tool_fail_refund")
_neg_ref = dr.repair_delivery(rt_neg_ref, "admin(uid=1)", "confirm_refund", "负数退款证据", refund_tx_id=str(_neg_ref_id))
check("负数 refund 明确拒绝且 run 保持 refund_pending",
      (not _neg_ref.get("ok")) and "必须 > 0" in str(_neg_ref.get("error", ""))
      and str(q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s", (rt_neg_ref,))[0]["last_settlement_error"]).startswith("refund_pending:")
      and len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (rt_neg_ref,))) == 0)
# 零 refund 与完整正数退款并存、落在冻结期望为 0 的 bonus 池：旧 abs(0) 会静默通过
wipe()
rt_zero_ref, _, _, cmap_zr = _seed_v35_repair(483, 9, 100, 0, 0)
_valid_ref_id = _insert_credit_tx(
    483, 9, "refund", "tool", 100, str(cmap_zr["tool"]), "tool_fail_refund")
_insert_credit_tx(483, 9, "refund", "bonus", 0, str(cmap_zr["tool"]), "tool_fail_refund")
_zero_ref = dr.repair_delivery(rt_zero_ref, "admin(uid=1)", "confirm_refund", "零值退款证据", refund_tx_id=str(_valid_ref_id))
check("零 refund 明确拒绝(完整正数退款并存，隔离零符号闸)且不 resolved",
      (not _zero_ref.get("ok")) and "必须 > 0" in str(_zero_ref.get("error", ""))
      and len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (rt_zero_ref,))) == 0)
# 自动执行路径也必须在 refund_credit 前逐条验证既存 refund 符号，并保留结构化 409 业务码。
wipe()
rt_auto_neg_ref, _, _, cmap_auto_neg = _seed_v35_repair(484, 9, 100, 0, 0)
_insert_credit_tx(
    484, 9, "refund", "tool", -100, str(cmap_auto_neg["tool"]), "tool_fail_refund")
_auto_neg_before = _v35_refund_snapshot(484, rt_auto_neg_ref)
_auto_neg_out, _auto_neg_writer_calls = _call_v35_with_writer_count(
    rt_auto_neg_ref, "自动路径负数退款证据")
check("自动路径负数 refund → 首写前 INVALID_REFUND_LEDGER_SIGN",
      (not _auto_neg_out.get("ok"))
      and _auto_neg_out.get("error_code") == "INVALID_REFUND_LEDGER_SIGN")
check("自动路径负数 refund → writer 0 次且三张资金表/run/记录/审计全量零变化",
      _auto_neg_writer_calls == 0
      and _v35_refund_snapshot(484, rt_auto_neg_ref) == _auto_neg_before)

wipe()
rt_auto_zero_ref, _, _, cmap_auto_zero = _seed_v35_repair(485, 9, 100, 0, 0)
_insert_credit_tx(
    485, 9, "refund", "tool", 100, str(cmap_auto_zero["tool"]), "tool_fail_refund")
_insert_credit_tx(
    485, 9, "refund", "bonus", 0, str(cmap_auto_zero["tool"]), "tool_fail_refund")
_auto_zero_before = _v35_refund_snapshot(485, rt_auto_zero_ref)
_auto_zero_out, _auto_zero_writer_calls = _call_v35_with_writer_count(
    rt_auto_zero_ref, "自动路径零值退款证据")
check("自动路径零 refund（与完整正数证据并存）→ 首写前 INVALID_REFUND_LEDGER_SIGN",
      (not _auto_zero_out.get("ok"))
      and _auto_zero_out.get("error_code") == "INVALID_REFUND_LEDGER_SIGN")
check("自动路径零 refund → writer 0 次且三张资金表/run/记录/审计全量零变化",
      _auto_zero_writer_calls == 0
      and _v35_refund_snapshot(485, rt_auto_zero_ref) == _auto_zero_before)
# 清掉故意脏数据并恢复 DB SSOT，后续用例继续在正式约束下执行
wipe()
q("ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check "
  "CHECK ((type <> 'consume' OR points < 0) AND (type <> 'refund' OR points > 0)) NOT VALID")
q("ALTER TABLE customer_credit_transactions VALIDATE CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check")

print("\n== [P1-2] v35 退款原子性:注入 记录/审计 失败 → 整事务回滚(含钱包退款)==")
wipe()
def _boom(*a, **k):
    raise RuntimeError("注入失败(测回滚)")
# 记录/CAS 失败 → 回滚含钱包退款
rt_r, fid_r, tref_r, _ = _seed_v35_repair(60, 9, 100, 50, 0)
_b0r = _wallet(60)
_orig_rec = dr._record_refund_and_cas_resolved
dr._record_refund_and_cas_resolved = _boom
try:
    _orr = dr.refund_and_confirm_v35_delivery(rt_r, "admin(uid=1)", "注入记录失败")
finally:
    dr._record_refund_and_cas_resolved = _orig_rec
check("P1-2 v35 记录/CAS 失败 → 返回失败", not _orr.get("ok"))
check("P1-2 v35 记录失败 → 钱包零变化(回滚含退款)", _wallet(60) == _b0r)
check("P1-2 v35 记录失败 → 零退款流水 + run 仍 refund_pending + 零退款记录",
      _v35_refund_rows(60) == 0
      and str(q("SELECT last_settlement_error FROM diagnosis_runs WHERE run_token=%s", (rt_r,))[0]["last_settlement_error"]).startswith("refund_pending:")
      and len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (rt_r,))) == 0)
# 审计 失败 → 回滚含钱包退款
rt_au, fid_au, tref_au, _ = _seed_v35_repair(61, 9, 100, 0, 0)
_b0au = _wallet(61)
_orig_aud = dr._write_settlement_audit
dr._write_settlement_audit = _boom
try:
    _oau = dr.refund_and_confirm_v35_delivery(rt_au, "admin(uid=1)", "注入审计失败")
finally:
    dr._write_settlement_audit = _orig_aud
check("P1-2 v35 审计失败 → 返回失败 + 钱包零变化(回滚) + 零退款记录",
      (not _oau.get("ok")) and _wallet(61) == _b0au and _v35_refund_rows(61) == 0
      and len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (rt_au,))) == 0)
# 第二池 refund_credit 前注入异常：第一池已更新钱包/写 refund，整事务仍须全部回滚
rt_mid, _, _, _ = _seed_v35_repair(62, 9, 100, 50, 0)
_mid_before = _v35_refund_snapshot(62, rt_mid)
from services import customer_credit as _cc
_orig_refund_credit = _cc.refund_credit
_refund_calls = {"n": 0}
def _refund_then_boom(*a, **k):
    _refund_calls["n"] += 1
    if _refund_calls["n"] == 2:
        raise RuntimeError("注入第二池退款失败")
    return _orig_refund_credit(*a, **k)
_cc.refund_credit = _refund_then_boom
try:
    _mid_out = dr.refund_and_confirm_v35_delivery(rt_mid, "admin(uid=1)", "第二池异常回滚")
finally:
    _cc.refund_credit = _orig_refund_credit
check("首池已写后第二池异常 → 整事务回滚所有资金/状态/记录/审计",
      (not _mid_out.get("ok")) and _refund_calls["n"] == 2 and _v35_refund_snapshot(62, rt_mid) == _mid_before)

# refund_credit 已写钱包/退款流水后篡改新流水 agent：post-writer 复核必须抛异常，使整个事务回滚。
wipe()
rt_post_agent, _, _, _ = _seed_v35_repair(409, 9, 100, 0, 0)
_post_agent_before = _v35_refund_snapshot(409, rt_post_agent)
q("INSERT INTO users(id) VALUES (8) ON CONFLICT (id) DO NOTHING")
from services import customer_credit as _post_agent_cc
_post_agent_original = _post_agent_cc.refund_credit
_post_agent_calls = {"n": 0}
def _refund_then_drift_agent(cur, customer_user_id, **kwargs):
    _post_agent_calls["n"] += 1
    result = _post_agent_original(cur, customer_user_id, **kwargs)
    cur.execute(
        "UPDATE customer_credit_transactions SET agent_user_id=8 "
        "WHERE customer_user_id=%s AND type='refund' AND source='diagnosis_delivery_refund' "
        "AND related_order_id=%s",
        (customer_user_id, str(kwargs.get("related_order_id"))),
    )
    return result
_post_agent_cc.refund_credit = _refund_then_drift_agent
try:
    _post_agent_out = dr.refund_and_confirm_v35_delivery(
        rt_post_agent, "admin(uid=1)", "退款写入后服务商漂移")
finally:
    _post_agent_cc.refund_credit = _post_agent_original
check("新 refund 写入后 agent 漂移 → post-writer 复核返回结构化归属错误",
      _is_safe_agent_ownership_error(_post_agent_out)
      and _post_agent_calls["n"] == 1)
check("新 refund 写入后 agent 漂移 → 钱包/全流水/run/记录/审计整事务零变化",
      _v35_refund_snapshot(409, rt_post_agent) == _post_agent_before)

# post-writer 金额漂移没有下游专属业务码：仍须通过 fallback 结构化 409 退出并回滚整个事务。
wipe()
rt_post_amount, _, _, _ = _seed_v35_repair(415, 9, 100, 0, 0)
_post_amount_before = _v35_refund_snapshot(415, rt_post_amount)
from services import customer_credit as _post_amount_cc
_post_amount_original = _post_amount_cc.refund_credit
_post_amount_calls = {"n": 0}
def _refund_then_drift_amount(cur, customer_user_id, **kwargs):
    _post_amount_calls["n"] += 1
    result = _post_amount_original(cur, customer_user_id, **kwargs)
    cur.execute(
        "UPDATE customer_credit_transactions SET points=points-1 "
        "WHERE customer_user_id=%s AND type='refund' AND source='diagnosis_delivery_refund' "
        "AND related_order_id=%s",
        (customer_user_id, str(kwargs.get("related_order_id"))),
    )
    return result
_post_amount_cc.refund_credit = _refund_then_drift_amount
try:
    _post_amount_out = dr.refund_and_confirm_v35_delivery(
        rt_post_amount, "admin(uid=1)", "退款写入后金额漂移")
finally:
    _post_amount_cc.refund_credit = _post_amount_original
check("新 refund 写入后正金额漂移 → V35_REFUND_VERIFICATION_FAILED（非模糊 500）",
      (not _post_amount_out.get("ok"))
      and _post_amount_out.get("error_code") == "V35_REFUND_VERIFICATION_FAILED"
      and _post_amount_calls["n"] == 1)
check("新 refund 写入后金额漂移 → 三张资金表/run/记录/审计整事务全量零变化",
      _v35_refund_snapshot(415, rt_post_amount) == _post_amount_before)

print("\n== [V3.5 refund ownership] 20 并发面对归属漂移全部拒绝且 writer 0 次 ==")
wipe()
rt_agent_c20, _, _, _ = _seed_v35_repair(410, 9, 70, 30, 0, wallet_agent=8)
_agent_c20_before = _v35_refund_snapshot(410, rt_agent_c20)
from services import customer_credit as _agent_c20_cc
_agent_c20_original = _agent_c20_cc.refund_credit
_agent_c20_calls = {"n": 0}
def _agent_c20_counted(*args, **kwargs):
    _agent_c20_calls["n"] += 1
    return _agent_c20_original(*args, **kwargs)
_agent_c20_cc.refund_credit = _agent_c20_counted
import threading as _threading
_agent_c20_barrier = _threading.Barrier(20)
def _exec_bad_agent_v35(i):
    _agent_c20_barrier.wait(timeout=30)
    return dr.refund_and_confirm_v35_delivery(
        rt_agent_c20, f"admin-agent-{i}(uid={i})", "并发归属漂移拒绝")
try:
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        _agent_c20_results = list(ex.map(_exec_bad_agent_v35, range(20)))
finally:
    _agent_c20_cc.refund_credit = _agent_c20_original
check("服务商归属漂移 20 并发 → 20 次均结构化拒绝",
      len(_agent_c20_results) == 20
      and all(_is_safe_agent_ownership_error(row) for row in _agent_c20_results))
check("服务商归属漂移 20 并发 → refund_credit 0 次且所有资金/状态表零变化",
      _agent_c20_calls["n"] == 0
      and _v35_refund_snapshot(410, rt_agent_c20) == _agent_c20_before)

print("\n== [V3.5 refund ownership] 混合仅错 owner consume 的 20 并发全部拒绝且 writer 0 次 ==")
wipe()
rt_owner_c20, _, tref_owner_c20, _ = _seed_v35_repair(413, 9, 70, 30, 0)
_insert_credit_tx(9413, 9, "consume", "publish", -30, tref_owner_c20, "tool_consume")
_owner_c20_before = _v35_refund_snapshot(413, rt_owner_c20)
from services import customer_credit as _owner_c20_cc
_owner_c20_original = _owner_c20_cc.refund_credit
_owner_c20_calls = {"n": 0}
def _owner_c20_counted(*args, **kwargs):
    _owner_c20_calls["n"] += 1
    return _owner_c20_original(*args, **kwargs)
_owner_c20_cc.refund_credit = _owner_c20_counted
_owner_c20_barrier = _threading.Barrier(20)
def _exec_bad_owner_v35(i):
    _owner_c20_barrier.wait(timeout=30)
    return dr.refund_and_confirm_v35_delivery(
        rt_owner_c20, f"admin-owner-{i}(uid={i})", "并发客户归属漂移拒绝")
try:
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        _owner_c20_results = list(ex.map(_exec_bad_owner_v35, range(20)))
finally:
    _owner_c20_cc.refund_credit = _owner_c20_original
check("混合仅错 owner（agent 正确）consume 20 并发 → 20 次均结构化拒绝",
      len(_owner_c20_results) == 20
      and all(_is_safe_customer_ownership_error(row) for row in _owner_c20_results))
check("混合仅错 owner（agent 正确）consume 20 并发 → writer 0 次且所有资金/状态表零变化",
      _owner_c20_calls["n"] == 0
      and _v35_refund_snapshot(413, rt_owner_c20) == _owner_c20_before)

print("\n== [P1-2] v35 退款并发:5 并发恰 1 真实退款(钱包恰 +total · 零双退)==")
wipe()
rt_c, fid_c, tref_c, _ = _seed_v35_repair(50, 9, 100, 0, 0)
from services import customer_credit as _c5_cc
_c5_original = _c5_cc.refund_credit
_c5_writer_calls = {"n": 0}
_c5_writer_lock = _threading.Lock()
def _c5_counted(*args, **kwargs):
    with _c5_writer_lock:
        _c5_writer_calls["n"] += 1
    return _c5_original(*args, **kwargs)
_c5_cc.refund_credit = _c5_counted
_c5_barrier = _threading.Barrier(5)
def _exec_v35(i):
    _c5_barrier.wait(timeout=30)
    return dr.refund_and_confirm_v35_delivery(rt_c, f"admin{i}(uid={i})", "并发执行")
try:
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        _cres = list(ex.map(_exec_v35, range(5)))
finally:
    _c5_cc.refund_credit = _c5_original
_real = [r for r in _cres if r.get("ok") and not r.get("idempotent")]
_idem = [r for r in _cres if r.get("ok") and r.get("idempotent")]
check("P1-2 v35 并发 5 → 恰 1 真实退款", len(_real) == 1)
check("P1-2 v35 并发 5 → 其余 4 次均幂等成功且 writer 恰调用 1 次",
      len(_idem) == 4 and _c5_writer_calls["n"] == 1)
check("P1-2 v35 并发 → 只 1 笔退款流水 + 钱包恰 +100(零双退)",
      _v35_refund_rows(50) == 1 and _wallet(50)["tool_credit_points"] == 100
      and all(int(row["agent_user_id"]) == 9 for row in _refund_agents(50)))
check("P1-2 v35 并发 → 恰 1 条退款记录", len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (rt_c,))) == 1)

wipe()
rt_c20, _, _, _ = _seed_v35_repair(51, 9, 70, 30, 0)
from services import customer_credit as _c20_cc
_c20_original = _c20_cc.refund_credit
_c20_writer_calls = {"n": 0}
_c20_writer_lock = _threading.Lock()
def _c20_counted(*args, **kwargs):
    with _c20_writer_lock:
        _c20_writer_calls["n"] += 1
    return _c20_original(*args, **kwargs)
_c20_cc.refund_credit = _c20_counted
_c20_barrier = _threading.Barrier(20)
def _exec_v35_20(i):
    _c20_barrier.wait(timeout=30)
    return dr.refund_and_confirm_v35_delivery(rt_c20, f"admin20-{i}(uid={i})", "二十并发执行")
try:
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        _cres20 = list(ex.map(_exec_v35_20, range(20)))
finally:
    _c20_cc.refund_credit = _c20_original
_real20 = [r for r in _cres20 if r.get("ok") and not r.get("idempotent")]
_idem20 = [r for r in _cres20 if r.get("ok") and r.get("idempotent")]
check("V3.5 并发 20 → 恰 1 真实退款", len(_real20) == 1)
check("V3.5 并发 20 → 其余 19 次均幂等成功且两池 writer 恰调用 2 次",
      len(_idem20) == 19 and _c20_writer_calls["n"] == 2)
check("V3.5 并发 20 → 两池各一笔退款 + 钱包恰 +100 + 一条 run-bound 记录",
      _v35_refund_rows(51) == 2 and _wallet(51)["tool_credit_points"] == 70
      and _wallet(51)["publish_credit_points"] == 30
      and all(int(row["agent_user_id"]) == 9 for row in _refund_agents(51))
      and len(q("SELECT 1 FROM diagnosis_refund_records WHERE run_token=%s", (rt_c20,))) == 1)

print("\n== [P1-2] 源码锁:v35 退款不调 refund_points · 不写 recharge_orders/refund_work_orders · 用 refund_credit+专用 source ==")
import io as _io, tokenize as _tk, re as _re
_dr_src2 = (Path(__file__).resolve().parent.parent / "services" / "diagnosis_runs.py").read_text(encoding="utf-8")
def _code_names(src):
    """提取所有 NAME token(排除注释/字符串/文档串)· 精确判断标识符是否作为代码调用/导入(refund_points 仅在注释/docstring 则不入)。"""
    names = set()
    try:
        for tok in _tk.generate_tokens(_io.StringIO(src).readline):
            if tok.type == _tk.NAME:
                names.add(tok.string)
    except Exception:
        pass
    return names
_dr_names = _code_names(_dr_src2)
check("P1-2 源码锁:diagnosis_runs 不导入/不调 refund_points(NAME token 不含 · v35 退款用 refund_credit 退原池)",
      "refund_points" not in _dr_names)
check("P1-2 源码锁:diagnosis_runs 无 SQL 触碰 recharge_orders/refund_work_orders(FROM/INTO/UPDATE/JOIN 后无该表 · 仅注释提及不算)",
      (not _re.search(r'(?i)(from|into|update|join|table)\s+recharge_orders', _dr_src2))
      and (not _re.search(r'(?i)(from|into|update|join|table)\s+refund_work_orders', _dr_src2))
      and ("get_refund_work_order(" not in _dr_src2))
check("P1-2 源码锁:v35 退款用 customer_credit.refund_credit + source=diagnosis_delivery_refund",
      ("from services.customer_credit import refund_credit" in _dr_src2) and ("diagnosis_delivery_refund" in _dr_src2))
check("V3.5 refund final 源码锁:自动路径显式 bonus_points=0 + structured manual code",
      '"bonus_points": 0' in _dr_src2 and "BONUS_REFUND_REQUIRES_MANUAL" in _dr_src2)
_server_src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
check("V3.5 refund final API 锁:结构化业务拒绝映射 HTTP 409",
      'status_code = 500 if code == "V35_REFUND_TRANSACTION_FAILED" else 409' in _server_src
      and 'detail={"code": code, "message":' in _server_src)
_front_src = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "pages" / "Admin" / "DiagnosisFundExceptions.tsx").read_text(encoding="utf-8")
check("V3.5 refund final 前端锁:本地重算 paid/v35/committed/refund_pending/bonus=0 且打开+提交双重 fail-closed",
      "function canExecuteV35Refund" in _front_src
      and "item.billing_mode === 'paid'" in _front_src
      and "item.run_status === 'delivery_repair_pending'" in _front_src
      and "item.freeze_backend === 'v35'" in _front_src
      and "item.repair_stage === 'refund_pending'" in _front_src
      and "item.v35_refund_preview?.freeze_status === 'committed'" in _front_src
      and "Number(item.v35_refund_preview?.bonus || 0) === 0" in _front_src
      and _front_src.count("if (!canExecuteV35Refund(") >= 2)
def _manual_bonus_ui_contract(src):
    """局部绑定 helper 与新增按钮；禁止靠 legacy 按钮/弹窗标题的同名字符串假绿。"""
    gate_match = _re.search(
        r"function canConfirmManualV35BonusRefund\(item: DeliveryRepairItem\): boolean \{(?P<body>.*?)\n\}",
        src,
        _re.S,
    )
    gate = gate_match.group("body") if gate_match else ""
    gate_compact = _re.sub(r"\s+", " ", gate).strip()
    expected_gate = (
        "return item.billing_mode === 'paid' "
        "&& item.run_status === 'delivery_repair_pending' "
        "&& item.freeze_backend === 'v35' "
        "&& item.repair_stage === 'refund_pending' "
        "&& item.v35_refund_preview?.freeze_status === 'committed' "
        "&& Number(item.v35_refund_preview?.bonus || 0) > 0 "
        "&& item.can_execute_v35_refund === false;"
    )
    gate_ok = (
        bool(gate_match)
        and gate_compact == expected_gate
    )
    button_match = _re.search(
        r"\{canConfirmManualV35BonusRefund\(item\) && \((?P<body>.*?)\n\s*\)\}",
        src,
        _re.S,
    )
    button = button_match.group("body") if button_match else ""
    action_ok = (
        bool(button_match)
        and "确认人工退款完成" in button
        and "onClick={() => openRepair(item, 'confirm_refund')}" in button
        and "openV35Refund(item)" not in button
        and "Number.isInteger(Number(refundTxId))" in src
        and "body.refund_tx_id = refundTxId.trim()" in src
        and "decision === 'confirm_refund' && note.trim().length < 4" in src
        and "Number(item.v35_refund_preview?.bonus || 0) === 0" in src
    )
    return gate_ok, action_ok

_manual_gate_ok, _manual_action_ok = _manual_bonus_ui_contract(_front_src)
check("V3.5 bonus 人工收口前端锁:helper 六项全 AND 且仅 paid/v35/committed/refund_pending/bonus>0 可见",
      _manual_gate_ok)
check("V3.5 bonus 人工收口前端锁:新增按钮局部绑定 confirm_refund + 真实流水/说明，不开放自动退款",
      _manual_action_ok)

# 反假绿：旧实现只查全局字符串存在，AND→OR、人工按钮误接自动端点、删按钮文案仍会通过。
_manual_gate_block_match = _re.search(
    r"function canConfirmManualV35BonusRefund\(item: DeliveryRepairItem\): boolean \{.*?\n\}",
    _front_src,
    _re.S,
)
_manual_gate_block = _manual_gate_block_match.group(0) if _manual_gate_block_match else ""
_manual_gate_or_mutant = _front_src.replace(
    _manual_gate_block,
    _manual_gate_block.replace(
        "&& item.run_status === 'delivery_repair_pending'",
        "|| item.run_status === 'delivery_repair_pending'",
        1,
    ),
    1,
) if _manual_gate_block else _front_src
_manual_gate_inline_or_mutant = _front_src.replace(
    _manual_gate_block,
    _manual_gate_block.replace(
        "&& item.can_execute_v35_refund === false;",
        "&& item.can_execute_v35_refund === false || true;",
        1,
    ),
    1,
) if _manual_gate_block else _front_src
_manual_gate_same_line_control = _front_src.replace(
    _manual_gate_block,
    _manual_gate_block.replace(
        "&& item.run_status === 'delivery_repair_pending'\n    && item.freeze_backend === 'v35'",
        "&& item.run_status === 'delivery_repair_pending' && item.freeze_backend === 'v35'",
        1,
    ),
    1,
) if _manual_gate_block else _front_src
_manual_button_match = _re.search(
    r"\{canConfirmManualV35BonusRefund\(item\) && \(.*?\n\s*\)\}",
    _front_src,
    _re.S,
)
_manual_button_block = _manual_button_match.group(0) if _manual_button_match else ""
_manual_handler_mutant = _front_src.replace(
    _manual_button_block,
    _manual_button_block.replace("openRepair(item, 'confirm_refund')", "openV35Refund(item)", 1),
    1,
) if _manual_button_block else _front_src
_manual_label_mutant = _front_src.replace(
    _manual_button_block,
    _manual_button_block.replace("确认人工退款完成", "", 1),
    1,
) if _manual_button_block else _front_src
check("V3.5 bonus 人工收口前端锁有齿:行首/行内 OR、handler→自动端点、删除文案转红；等价单行 AND 保持绿",
      _manual_gate_or_mutant != _front_src
      and _manual_gate_inline_or_mutant != _front_src
      and _manual_gate_same_line_control != _front_src
      and _manual_handler_mutant != _front_src
      and _manual_label_mutant != _front_src
      and not _manual_bonus_ui_contract(_manual_gate_or_mutant)[0]
      and not _manual_bonus_ui_contract(_manual_gate_inline_or_mutant)[0]
      and _manual_bonus_ui_contract(_manual_gate_same_line_control)[0]
      and not _manual_bonus_ui_contract(_manual_handler_mutant)[1]
      and not _manual_bonus_ui_contract(_manual_label_mutant)[1])
check("V3.5 refund final 前端锁:普通运营文案不暴露 finance/type=refund/run/双表 等工程术语",
      all(term not in _front_src for term in (
          "finance 退款路径", "type=refund", "绑定本 run", "双表逐笔核清", "admin 退款",
          "真实钱包退款流水 id",
      )))
check("V3.5 refund final 前端锁:结构化错误按 code 映射运营文案且不直出服务端 message",
      "function operatorSafeActionError" in _front_src
      and "ACTION_ERROR_MESSAGES" in _front_src
      and all(code in _front_src for code in (
          "INVALID_CONSUME_LEDGER_SIGN", "INVALID_REFUND_LEDGER_SIGN",
          "V35_AGENT_OWNERSHIP_MISMATCH", "V35_CUSTOMER_OWNERSHIP_MISMATCH",
          "EXISTING_REFUND_REQUIRES_MANUAL", "V35_REFUND_VERIFICATION_FAILED",
      ))
      and "msg = structured.message" not in _front_src
      and "setActionError(operatorSafeActionError(detail))" in _front_src)

print("\n" + ("=" * 50))
print(f"TOTAL CHECKS: {len(CHECKS)}")
print(f"TOTAL FAILS: {len(FAILS)}")
for f in FAILS:
    print("  FAIL: " + f)
sys.exit(1 if FAILS else 0)
