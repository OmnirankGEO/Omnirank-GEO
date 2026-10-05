#!/usr/bin/env bash
# ============================================================================
# [P1-3/V3.5 refund final] 迁移升级矩阵 A–I(throwaway PG16 · 绝不指生产)
#   A 全新库迁移成功           B 连续 2× 幂等成功
#   C 旧表(f5b48142 结构)空表升级成功   D 旧表已补齐合法记录升级成功
#   E 旧表脏数据(NULL/非法/孤儿/重复凭证)→ 迁移 fail-closed 明确失败
#   F 人工订正后重跑成功        G 篡改 NOT NULL/convalidated → 反查失败(证明有齿)
#   H 流水符号脏行拒绝+人工订正重跑  I 符号约束缺失/篡改 → 反查失败(证明有齿)
# 用法:bash scripts/verify_diagnosis_migration_matrix.sh
# ============================================================================
set -uo pipefail
CID=diag-w4-mig-pg
PORT=5456
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MIG="$REPO/scripts/migration_diagnosis_runs_2026_07_13.sql"
export MSYS_NO_PATHCONV=1
PASS=0; FAIL=0
ok()   { echo "  OK  $1"; PASS=$((PASS+1)); }
bad()  { echo " FAIL $1"; FAIL=$((FAIL+1)); }

psqlc() { docker exec -i "$CID" psql -q -v ON_ERROR_STOP=1 -U geo_admin -d "$1"; }        # stdin SQL · 出错非零
psql1() { docker exec "$CID" psql -tAq -U geo_admin -d "$1" -c "$2"; }                     # 单条 · 取值
freshdb() { docker exec "$CID" psql -q -U geo_admin -d postgres -c "DROP DATABASE IF EXISTS $1 WITH (FORCE)" >/dev/null 2>&1
            docker exec "$CID" psql -q -U geo_admin -d postgres -c "CREATE DATABASE $1" >/dev/null
            printf '%s\n' "$CREDIT_SCHEMA" | psqlc "$1" >/dev/null; }
applymig() { docker exec -i "$CID" psql -v ON_ERROR_STOP=1 -U geo_admin -d "$1" < "$MIG" >/dev/null 2>"/tmp/mig_err_$1.txt"; }

OLD_SCHEMA="
CREATE TABLE diagnosis_refund_records (
    id BIGSERIAL PRIMARY KEY, run_token TEXT, freeze_task_ref TEXT, freeze_id BIGINT,
    freeze_backend TEXT, owner_user_id INTEGER, points INTEGER NOT NULL,
    external_ref TEXT, operator TEXT NOT NULL, note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE UNIQUE INDEX uq_diag_refund_run ON diagnosis_refund_records (run_token);
CREATE UNIQUE INDEX uq_diag_refund_extref ON diagnosis_refund_records (external_ref) WHERE external_ref IS NOT NULL;
"
CREDIT_SCHEMA="
CREATE TABLE customer_credit_transactions (
    id BIGSERIAL PRIMARY KEY,
    type TEXT NOT NULL,
    points INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'admin_adjust',
    CONSTRAINT customer_credit_transactions_source_check CHECK (source IN (
        'online_payment','offline_allocation','admin_adjust','tool_consume','refund_revoke','agent_rebate','tool_fail_refund'))
);
"
# 造两条 diagnosis_runs 供 FK 引用(先建 runs · 再建旧退款表)
seed_runs() { psqlc "$1" <<SQL
INSERT INTO diagnosis_runs (run_token, session_id, owner_user_id, client_request_id, billing_mode, freeze_task_ref, run_status)
VALUES ('run_ok','sid1',30,'cr1','paid','diag_run_ok','delivery_repair_pending')
ON CONFLICT DO NOTHING;
SQL
}

echo "=== 起 throwaway PG16(port $PORT)==="
docker rm -f "$CID" >/dev/null 2>&1 || true
docker run -d --name "$CID" -e POSTGRES_PASSWORD=test -e POSTGRES_USER=geo_admin -e POSTGRES_DB=postgres -p ${PORT}:5432 postgres:16 >/dev/null
for i in $(seq 1 90); do docker exec "$CID" psql -U geo_admin -d postgres -c "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
docker exec "$CID" psql -U geo_admin -d postgres -c "SELECT 1" >/dev/null 2>&1 || { echo "PG 未起"; docker rm -f "$CID" >/dev/null 2>&1; exit 2; }

# ── A 全新库迁移成功 ─────────────────────────────────────────────
echo "== A 全新库迁移 =="
freshdb dba
if applymig dba; then ok "A 全新库迁移成功(含反查断言全过)"; else bad "A 全新库迁移失败:$(tail -3 /tmp/mig_err_dba.txt)"; fi
[ "$(psql1 dba "SELECT data_type FROM information_schema.columns WHERE table_name='diagnosis_refund_records' AND column_name='points'")" = "bigint" ] \
  && ok "A points=bigint" || bad "A points 非 bigint"
[ "$(psql1 dba "SELECT convalidated FROM pg_constraint WHERE conname='fk_diag_refund_run'")" = "t" ] \
  && ok "A FK convalidated=true" || bad "A FK 未 validate"
[ "$(psql1 dba "SELECT convalidated FROM pg_constraint WHERE conname='customer_credit_transactions_consume_refund_points_sign_check'")" = "t" ] \
  && ok "A consume/refund 符号 CHECK convalidated=true" || bad "A 流水符号 CHECK 缺失/未 validate"

# ── B 连续 2× 幂等 ────────────────────────────────────────────────
echo "== B 2× 幂等 =="
if applymig dba; then ok "B 同库第 2 次迁移成功(幂等)"; else bad "B 2× 迁移失败:$(tail -3 /tmp/mig_err_dba.txt)"; fi

# ── C 旧表(f5b48142)空表升级 ─────────────────────────────────────
echo "== C 旧表空表升级 =="
freshdb dbc
# 先跑迁移建 diagnosis_runs（并建新表）→ 删新表换旧表(空)→ 再跑迁移走升级路径
applymig dbc >/dev/null 2>&1
psqlc dbc <<< "DROP TABLE diagnosis_refund_records; $OLD_SCHEMA"
if applymig dbc; then ok "C 旧表空表升级成功"; else bad "C 旧表空表升级失败:$(tail -3 /tmp/mig_err_dbc.txt)"; fi
[ "$(psql1 dbc "SELECT is_nullable FROM information_schema.columns WHERE table_name='diagnosis_refund_records' AND column_name='refund_tx_ref'")" = "NO" ] \
  && ok "C refund_tx_ref 升级为 NOT NULL" || bad "C refund_tx_ref 未收紧"

# ── D 旧表已补齐合法记录升级 ──────────────────────────────────────
echo "== D 旧表已补齐合法记录升级 =="
freshdb dbd
applymig dbd >/dev/null 2>&1
seed_runs dbd
psqlc dbd <<SQL
DROP TABLE diagnosis_refund_records;
$OLD_SCHEMA
ALTER TABLE diagnosis_refund_records ADD COLUMN refund_tx_ref TEXT;  -- 模拟运维已补列
INSERT INTO diagnosis_refund_records (run_token, freeze_task_ref, freeze_id, freeze_backend, owner_user_id, points, external_ref, refund_tx_ref, operator, note)
VALUES ('run_ok','diag_run_ok',77,'v35',30,200,'old-ext','555','opX','已补齐');
SQL
if applymig dbd; then ok "D 旧表已补齐合法记录升级成功"; else bad "D 升级失败:$(tail -3 /tmp/mig_err_dbd.txt)"; fi
[ "$(psql1 dbd "SELECT points FROM diagnosis_refund_records WHERE run_token='run_ok'")" = "200" ] \
  && ok "D 合法记录保留(points=200)" || bad "D 记录丢失"

# ── E 旧表脏数据 → fail-closed 明确失败 ───────────────────────────
echo "== E 旧表脏数据 fail-closed =="
freshdb dbe
applymig dbe >/dev/null 2>&1
seed_runs dbe
# 脏:refund_tx_ref NULL(未补齐)+ 孤儿 run_token + points≤0
psqlc dbe <<SQL
DROP TABLE diagnosis_refund_records;
$OLD_SCHEMA
INSERT INTO diagnosis_refund_records (run_token, freeze_task_ref, freeze_id, freeze_backend, owner_user_id, points, external_ref, operator)
VALUES ('run_ok','diag_run_ok',77,'v35',30,200,'e1','opX'),          -- refund_tx_ref NULL(不可证明)
       ('run_orphan','diag_x',88,'legacy',31,100,'e2','opX');         -- 孤儿 run_token
SQL
if applymig dbe; then bad "E 脏数据竟迁移成功(应 fail-closed)"; else
  if grep -q "迁移 fail-closed" "/tmp/mig_err_dbe.txt"; then ok "E 脏数据 → 迁移 fail-closed RAISE(明确失败 · 输出待订正)"; else bad "E 失败但非 fail-closed RAISE:$(tail -2 /tmp/mig_err_dbe.txt)"; fi
fi
[ -z "$(psql1 dbe "SELECT is_nullable FROM information_schema.columns WHERE table_name='diagnosis_refund_records' AND column_name='refund_tx_ref' AND is_nullable='NO'")" ] \
  && ok "E fail-closed 后未误收紧(refund_tx_ref 仍可空 · 数据未破坏)" || bad "E 竟已收紧(不该)"

# ── F 人工订正后重跑成功 ──────────────────────────────────────────
#   注:E 的 fail-closed 迁移(DO 块原子)回滚了 ADD COLUMN refund_tx_ref → 订正需重新补列(真实运维流程)
echo "== F 人工订正后重跑 =="
psqlc dbe <<SQL
ALTER TABLE diagnosis_refund_records ADD COLUMN IF NOT EXISTS refund_tx_ref TEXT;   -- 运维手动补列
DELETE FROM diagnosis_refund_records WHERE run_token='run_orphan';                   -- 删孤儿
UPDATE diagnosis_refund_records SET refund_tx_ref='777' WHERE run_token='run_ok';    -- 逐行核实真实退款流水后回填
SQL
if applymig dbe; then ok "F 人工订正(补列 + 删孤儿 + 回填真实 refund_tx_ref)后重跑成功"; else bad "F 订正后仍失败:$(tail -3 /tmp/mig_err_dbe.txt)"; fi

# ── G 篡改 NOT NULL / convalidated → 反查失败(证明有齿)──────────────
echo "== G 篡改反查有齿 =="
# 抽取迁移末尾"可执行反查断言"DO 块单独跑
awk '/可执行反查断言/{f=1} f{print}' "$MIG" > /tmp/revcheck.sql
# G1 篡改 NOT NULL:refund_tx_ref DROP NOT NULL → 反查须 RAISE
freshdb dbg
applymig dbg >/dev/null 2>&1
psql1 dbg "ALTER TABLE diagnosis_refund_records ALTER COLUMN refund_tx_ref DROP NOT NULL" >/dev/null
if docker exec -i "$CID" psql -v ON_ERROR_STOP=1 -U geo_admin -d dbg < /tmp/revcheck.sql >/dev/null 2>/tmp/g1.txt; then
  bad "G1 篡改 NOT NULL 后反查竟通过(反查无齿)"
else
  grep -q "反查" /tmp/g1.txt && ok "G1 篡改 refund_tx_ref NOT NULL → 反查 RAISE(有齿)" || bad "G1 失败但非反查 RAISE:$(tail -2 /tmp/g1.txt)"
fi
# G2 篡改 convalidated:FK 换 NOT VALID(未 validate)→ 反查须 RAISE
freshdb dbg2
applymig dbg2 >/dev/null 2>&1
psqlc dbg2 <<SQL
ALTER TABLE diagnosis_refund_records DROP CONSTRAINT fk_diag_refund_run;
ALTER TABLE diagnosis_refund_records ADD CONSTRAINT fk_diag_refund_run FOREIGN KEY (run_token) REFERENCES diagnosis_runs(run_token) NOT VALID;
SQL
if docker exec -i "$CID" psql -v ON_ERROR_STOP=1 -U geo_admin -d dbg2 < /tmp/revcheck.sql >/dev/null 2>/tmp/g2.txt; then
  bad "G2 FK 未 validate(convalidated=false)反查竟通过(反查无齿)"
else
  grep -q "反查" /tmp/g2.txt && ok "G2 FK convalidated=false → 反查 RAISE(有齿)" || bad "G2 失败但非反查 RAISE:$(tail -2 /tmp/g2.txt)"
fi

# ── H 流水符号脏行 fail-closed + 人工订正重跑 ─────────────────────
echo "== H customer_credit_transactions 符号脏行拒绝/订正 =="
freshdb dbh
psqlc dbh <<SQL
INSERT INTO customer_credit_transactions (type, points) VALUES ('consume', 0), ('refund', -9);
SQL
if applymig dbh; then bad "H 符号脏行竟迁移成功(应 fail-closed)"; else
  grep -q "符号迁移 fail-closed" /tmp/mig_err_dbh.txt \
    && ok "H consume=0/refund=-9 → 迁移 fail-closed RAISE(列出违规行)" \
    || bad "H 失败但非符号 fail-closed RAISE:$(tail -3 /tmp/mig_err_dbh.txt)"
fi
[ "$(psql1 dbh "SELECT count(*) FROM customer_credit_transactions WHERE (type='consume' AND points>=0) OR (type='refund' AND points<=0)")" = "2" ] \
  && ok "H 脏行原样保留(迁移未自动改历史)" || bad "H 脏行被迁移修改/删除"
[ -z "$(psql1 dbh "SELECT 1 FROM pg_constraint WHERE conname='customer_credit_transactions_consume_refund_points_sign_check'")" ] \
  && ok "H fail-closed 后未误加符号约束" || bad "H 脏行失败后竟留下符号约束"
psqlc dbh <<SQL
UPDATE customer_credit_transactions SET points=-9 WHERE type='consume';
UPDATE customer_credit_transactions SET points=9 WHERE type='refund';
SQL
if applymig dbh; then ok "H 人工核对订正符号后重跑成功"; else bad "H 订正后仍失败:$(tail -3 /tmp/mig_err_dbh.txt)"; fi
[ "$(psql1 dbh "SELECT convalidated FROM pg_constraint WHERE conname='customer_credit_transactions_consume_refund_points_sign_check'")" = "t" ] \
  && ok "H 订正重跑后符号 CHECK convalidated=true" || bad "H 订正后约束未 validate"

# ── I 缺约束/篡改约束反查有齿 ────────────────────────────────────
echo "== I 流水符号约束反查有齿 =="
freshdb dbi
applymig dbi >/dev/null 2>&1
psql1 dbi "ALTER TABLE customer_credit_transactions DROP CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check" >/dev/null
if docker exec -i "$CID" psql -v ON_ERROR_STOP=1 -U geo_admin -d dbi < /tmp/revcheck.sql >/dev/null 2>/tmp/i1.txt; then
  bad "I1 删除流水符号约束后反查竟通过(反查无齿)"
else
  grep -q "符号 CHECK" /tmp/i1.txt && ok "I1 删除流水符号约束 → 反查 RAISE(有齿)" || bad "I1 失败但未命中符号反查:$(tail -2 /tmp/i1.txt)"
fi
freshdb dbi2
applymig dbi2 >/dev/null 2>&1
psqlc dbi2 <<SQL
ALTER TABLE customer_credit_transactions DROP CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check;
ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check CHECK (points <> 0) NOT VALID;
SQL
if docker exec -i "$CID" psql -v ON_ERROR_STOP=1 -U geo_admin -d dbi2 < /tmp/revcheck.sql >/dev/null 2>/tmp/i2.txt; then
  bad "I2 篡改为错误/未验证符号约束后反查竟通过(反查无齿)"
else
  grep -q "符号 CHECK" /tmp/i2.txt && ok "I2 错误定义+convalidated=false → 反查 RAISE(有齿)" || bad "I2 失败但未命中符号反查:$(tail -2 /tmp/i2.txt)"
fi

echo ""
echo "================= 迁移矩阵 A–I ================="
echo "PASS=$PASS  FAIL=$FAIL"
docker rm -f "$CID" >/dev/null 2>&1 || true
[ "$FAIL" = 0 ] && exit 0 || exit 1
