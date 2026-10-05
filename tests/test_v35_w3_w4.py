"""
V3.5 W3 + W4 验收 gate

W3:
1. 协议 schema · agent_factory_agreements 表存在 + 4 状态枚举
2. 协议 gate dependency · agreement_gate.py 提供 require_signed_agreement
3. agent_workbench_api W2 endpoint 已注入 Depends(/pricing/skus PUT + /promotion/qrcode)
4. customer_workbench_api 3 endpoints 注册 + DTO 字段铁律(不返 wholesale/raw_cost/tax/fee)
5. wallet_api recharge 接 W3 字段(agent_user_id / sku_template_id / binding_source / source_token)
6. L0 referral V3.5 防御 gate · process_recharge_commission_v3 检查 settlement_mode
7. 客户面禁词 rg: 新页 CreditWallet/BuyCredit/AgreementPage 不含禁词

W4:
8. W4 schema · 4 张表(disputes + allocations + audit_runs + audit_diffs)
9. customer_binding upsert 冲突时 INSERT disputes 表
10. inventory_audit service · run_audit + list_audit_runs 提供
11. admin_w4_api 4 endpoints 注册
12. scheduler.py 注册 v35_inventory_audit_daily cron
"""
import re
import os
import sys
import subprocess
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _strip_docstrings_and_comments(src: str) -> str:
    """对齐 test_v35_w2 helper"""
    import io, tokenize
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenizeError:
        return src
    lines = src.split("\n")
    in_block = False
    quote = None
    for i, line in enumerate(lines):
        if not in_block:
            for q in ('"""', "'''"):
                if line.count(q) >= 2:
                    start = line.find(q); end = line.find(q, start + 3) + 3
                    lines[i] = line[:start] + (" " * (end - start)) + line[end:]
                    break
                if q in line:
                    in_block = True; quote = q
                    start = line.find(q)
                    lines[i] = line[:start]
                    break
        else:
            if quote and quote in line:
                end = line.find(quote) + 3
                lines[i] = " " * end + line[end:]
                in_block = False; quote = None
            else:
                lines[i] = ""
    out = []
    for line in lines:
        if line.lstrip().startswith("#"): out.append("")
        else: out.append(line)
    return "\n".join(out)


# ============================================================
# W3 gates
# ============================================================

def test_w3_gate_1_agreement_schema():
    """agent_factory_agreements 表 schema 包含 4 状态枚举"""
    sql = (ROOT / "scripts" / "migration_v35_w3_2026_05_26.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS agent_factory_agreements" in sql
    for state in ["unsigned", "signed", "rejected", "expired"]:
        assert f"'{state}'" in sql, f"4 状态枚举少 {state}"


def test_w3_gate_2_agreement_gate_provided():
    """auth/agreement_gate.py 提供 require_signed_agreement dependency"""
    p = ROOT / "auth" / "agreement_gate.py"
    assert p.exists()
    src = p.read_text(encoding="utf-8")
    assert "def require_signed_agreement" in src
    assert "AGREEMENT_NOT_SIGNED" in src


def test_w3_gate_3_w2_endpoint_gated():
    """W2 /pricing/skus PUT 和 /promotion/qrcode 加了 Depends(require_signed_agreement)"""
    src = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    # 检查 import
    assert "require_signed_agreement" in src
    # 至少 2 处 Depends 注入
    assert src.count("Depends(require_signed_agreement)") >= 2


def test_w3_gate_4_customer_api_dto_blacklist():
    """customer_workbench_api 不返 wholesale/raw_cost/tax/fee 字段"""
    src = _strip_docstrings_and_comments(
        (ROOT / "api" / "customer_workbench_api.py").read_text(encoding="utf-8")
    )
    forbidden = ["wholesale_cents", "platform_cost_cents", "raw_cost",
                 "tax_rate_bps", "tax_mode", "gateway_fee_bps",
                 "settlement_service_fee_bps", "agent_revenue_ledger"]
    violations = []
    for kw in forbidden:
        if kw in src:
            violations.append(kw)
    assert not violations, f"customer_workbench_api 含禁字段: {violations}"


def test_w3_gate_5_wallet_recharge_accepts_w3_fields():
    """公开充值请求只接商品/报价引用，不接商业关系主体。"""
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    start = src.index("class RechargeRequest")
    end = src.index("class PaymentCallbackRequest")
    request_model = src[start:end]
    assert "sku_template_id:" in request_model
    assert "price_quote_id:" in request_model
    for private_field in ("agent_user_id:", "binding_source:", "source_token:"):
        assert private_field not in request_model
    assert '"order_type": v35_order_type' in src
    assert "create_recharge_order(**_order_create_kwargs)" in src


def test_w3_gate_5b_sku_amount_ssot():
    """[boss r5 P0-3] wallet/recharge SKU 路径 amount_cents 必须由 SKU snapshot 强制
    · 不能用 client custom_amount_yuan / package_id 篡改"""
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    # Path SKU 必须覆盖新 override_id 与老 sku_template_id 两种入口
    assert "if req.override_id or req.sku_template_id:" in src
    # amount_cents 在 SKU 路径必须从 _retail / retail_cents 取(不能从 custom_amount_yuan)
    assert "amount_cents = int(_retail)" in src, "SKU 路径 amount_cents 必须 = sku.retail_cents"
    # 必须显式标 amount_source='sku_snapshot' 给审计
    assert "amount_source" in src and "sku_snapshot" in src
    # agent_user_id 必须从服务端推导(bound_agent_id_for_v35)· 不直接信任 req.agent_user_id
    assert "bound_agent_id_for_v35" in src
    assert '"agent_user_id": bound_agent_id_for_v35' in src
    # 商业服务主体必须从服务端 canonical resolver 推导，不再校验客户端主体。
    assert "resolve_commercial_relationship" in src
    assert "commercial_resolution" in src


def test_w3_gate_5c_binding_dispute_no_tx_abort():
    """[boss r5 P1] customer_binding W4 disputes INSERT 用 to_regclass 防 tx abort"""
    src = (ROOT / "services" / "customer_binding.py").read_text(encoding="utf-8")
    # 不能再用裸 try/except 包 INSERT(会污染事务)
    # 必须用 to_regclass 先查表
    assert "to_regclass" in src, "必须 to_regclass 检测表存在 · 防 UndefinedTable 污染事务"
    # 检查 INSERT 在 to_regclass != None 的分支内
    assert "if _reg is not None:" in src


def test_w3_gate_5d_v35_flag_gate_in_sku_path():
    """[boss r6 P0] wallet/recharge SKU 路径必须 check V35_FACTORY_INVENTORY_ENABLED
    · flag=false 拒绝下单 · 防付款落 direct/v32 错账"""
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    # 必须 import is_v35_factory_enabled
    assert "is_v35_factory_enabled" in src
    # 必须有 V35_FACTORY_DISABLED 503 错误
    assert "V35_FACTORY_DISABLED" in src
    # gate 必须在统一 SKU 路径(override_id 或 sku_template_id)内
    sku_branch_idx = src.find("if req.override_id or req.sku_template_id:")
    assert sku_branch_idx > 0
    snippet = src[sku_branch_idx:sku_branch_idx + 2500]
    assert "is_v35_factory_enabled" in snippet, "V3.5 flag check 必须在 SKU 分支内"


def test_w3_gate_5e_direct_fallback_not_offered():
    """无显式绑定时使用合格平台直营，不要求客户先绑定服务商。"""
    src = (ROOT / "api" / "customer_workbench_api.py").read_text(encoding="utf-8")
    assert 'source": "direct_fallback"' not in src
    assert "needs_binding" not in src
    assert "resolve_commercial_relationship" in src
    assert "PLATFORM_CONFIGURATION_MANAGED" in src


def test_w3_gate_5f_buycredit_wechat_force_xunhupay():
    """[boss r6 P1-2] BuyCredit 微信内 UA 检测 + 强制 xunhupay 绕 JSAPI 死路"""
    src = (ROOT / "frontend" / "src" / "pages" / "Customer" / "BuyCredit.tsx").read_text(encoding="utf-8")
    # 必须有 MicroMessenger UA 检测 helper
    assert "MicroMessenger" in src or "_isWeChatBrowser" in src
    # handleBuy 必须根据 wechat 与否传 channel
    assert "xunhupay" in src, "微信内必须强制 xunhupay channel"
    # 必须把 channel 传给 createSKURecharge
    assert "channel" in src


def test_w3_gate_5g_orchestrator_snapshot_locked_v35():
    """[boss r7 P0-1] Orchestrator 已创建 SKU 订单 · 必须强制走 V3.5
    · 不让 flag 中途切换改路径(防资金错账)"""
    src = (ROOT / "services" / "settlement_orchestrator.py").read_text(encoding="utf-8")
    # is_v35_locked_order 判断必须存在
    assert "is_v35_locked_order" in src
    # 条件:order_type=customer_recharge + sku_template_id + agent_user_id
    assert 'order_type == "customer_recharge"' in src
    assert "order_sku_id is not None" in src and "order_agent_id is not None" in src
    # 双保险:pricing_snapshot.amount_source='sku_snapshot'
    assert 'amount_source"' in src and "sku_snapshot" in src
    # 平台直营订单按不可变快照结算，不能补造商业绑定。
    assert "v35_platform_direct_settlement" in src
    assert "commercial_resolution" in src
    assert "from services.customer_binding import upsert_customer_agent_binding" not in src
    assert "upsert_customer_agent_binding(" not in src
    # log 标 locked by order snapshot
    assert "locked by order snapshot" in src


def test_w3_gate_5h_customer_bind_agent_endpoint():
    """旧客户自助商业绑定入口保留兼容路由但 fail-closed。"""
    src = (ROOT / "api" / "customer_workbench_api.py").read_text(encoding="utf-8")
    assert "@router.post(\"/bind-agent\")" in src
    block = src[src.index("async def customer_bind_agent"):]
    assert "PLATFORM_CONFIGURATION_MANAGED" in block
    assert "410" in block
    assert "upsert_customer_agent_binding" not in block
    assert "FROM referral_codes WHERE code" not in block


def test_w3_gate_5i_promotion_link_v35_binding_bridge():
    """推广链接只记录邀请归属，不桥接商业绑定。"""
    backend = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    invite = (ROOT / "frontend" / "src" / "pages" / "Invite" / "InvitePage.tsx").read_text(encoding="utf-8")
    assert 'ref_link = f"{public_origin}/api/sl/{short_code}"' in backend
    assert "omnirank_pending_bind_agent_ref" not in invite
    assert "bind_agent_ref" not in invite


def test_w3_gate_5j_buycredit_auto_bind_from_url():
    """购买页不得从 URL、缓存或输入框建立商业绑定。"""
    src = (ROOT / "frontend" / "src" / "pages" / "Customer" / "BuyCredit.tsx").read_text(encoding="utf-8")
    for token in ("_getBindRefFromURL", "bind_agent_ref", "bindAgent", "BindAgentInput"):
        assert token not in src


def test_w3_gate_5k_referral_code_from_correct_table():
    """[boss r8 P0-1] 推广码查 referral_codes 表(user_wallets 无 referral_code 列)
    · agent promotion/qrcode + customer/bind-agent 都必须用 referral_codes"""
    agent_src = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    customer_src = (ROOT / "api" / "customer_workbench_api.py").read_text(encoding="utf-8")
    # 严禁查 user_wallets.referral_code
    assert "user_wallets WHERE referral_code" not in agent_src, \
        "agent_workbench 不应再查 user_wallets.referral_code(列不存在)"
    assert "user_wallets WHERE referral_code" not in customer_src, \
        "customer_workbench 不应再查 user_wallets.referral_code"
    # 必须查 referral_codes 表
    assert "referral_codes WHERE user_id" in agent_src or "FROM referral_codes" in agent_src
    assert "FROM referral_codes WHERE code" not in customer_src
    # promotion/qrcode 必须 get-or-create(INSERT ON CONFLICT)
    assert "INSERT INTO referral_codes" in agent_src


def test_w3_gate_5l_promotion_link_to_public_invite():
    """推广链使用服务端不透明短链，不把推荐码放进 URL。"""
    src = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    assert "/invite?ref=" not in src
    assert 'ref_link = f"{public_origin}/api/sl/{short_code}"' in src


def test_w3_gate_5m_login_page_preserve_bind_ref():
    """登录页可保留注册邀请归属，但不得生成商业绑定参数或缓存。"""
    src = (ROOT / "frontend" / "src" / "pages" / "Login" / "LoginPage.tsx").read_text(encoding="utf-8")
    assert "referral_code" in src
    assert "bind_agent_ref" not in src
    assert "omnirank_pending_bind_agent_ref" not in src


def test_w3_gate_5n_buycredit_localStorage_fallback():
    """购买页不读写历史商业绑定缓存。"""
    src = (ROOT / "frontend" / "src" / "pages" / "Customer" / "BuyCredit.tsx").read_text(encoding="utf-8")
    assert "omnirank_pending_bind_agent_ref" not in src
    assert "_clearPendingBindRef" not in src


def test_w3_gate_5o_login_page_reads_state_from_search():
    """登录页的历史 from.search 只可解析注册 referral，不可解析商业绑定。"""
    src = (ROOT / "frontend" / "src" / "pages" / "Login" / "LoginPage.tsx").read_text(encoding="utf-8")
    assert "state?.from?.search" in src or "from?.search" in src, \
        "LoginPage 必须保留登录前页面的注册来源上下文"
    assert "_fromSearchParams" in src or "fromSearchParams" in src
    assert "_fromSearchParams.get('ref')" in src or "fromSearchParams.get('ref')" in src
    assert "bind_agent_ref" not in src


def test_w3_gate_5q_rollback_no_drop_w1_column():
    """[boss r12 P0] W3 rollback 严禁 DROP COLUMN sku_template_id(W1 资产)"""
    src = (ROOT / "scripts" / "rollback_v35_w3_2026_05_26.sql").read_text(encoding="utf-8")
    # 不能含 DROP COLUMN sku_template_id(无论 IF EXISTS 与否)
    import re as _re
    bad = _re.search(r"DROP\s+COLUMN.*sku_template_id", src, _re.IGNORECASE)
    assert not bad, "W3 rollback 严禁 DROP recharge_orders.sku_template_id(W1 拥)"
    # W3 自己的资产必须仍 drop
    assert "DROP INDEX IF EXISTS idx_recharge_sku" in src
    assert "DROP TABLE IF EXISTS agent_factory_agreements" in src


def test_w3_gate_5r_migration_column_level_verify():
    """[boss r12 P1] W3 migration verify 必须做列级断言(5 关键列)"""
    src = (ROOT / "scripts" / "migration_v35_w3_2026_05_26.sql").read_text(encoding="utf-8")
    # DO 块内必须查 information_schema.columns + 5 关键列
    for col in ["agent_user_id", "status", "content_hash", "signed_ip", "signed_ua"]:
        assert f"'{col}'" in src, f"W3 migration DO verify 缺列名 {col}"
    # 必须断言 5 列(防 partner 撞名时只查表名导致漏检)
    assert "v_col_count" in src or "<> 5" in src
    # Python wrapper VERIFY_QUERIES 也要列级
    wrap = (ROOT / "db" / "migrate_v35_w3.py").read_text(encoding="utf-8")
    assert "5 关键列" in wrap or "agent_user_id','status','content_hash'" in wrap


def test_w3_gate_5p_referral_code_race_safe():
    """[boss r9 P1] /promotion/qrcode get-or-create ON CONFLICT 并发后 re-SELECT
    · 防返未入库本地码"""
    src = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    # 必须有 fallback re-SELECT(在 ON CONFLICT RETURNING 之后)
    # 简化检查:INSERT 块之后至少出现 2 次 SELECT code FROM referral_codes
    insert_idx = src.find("INSERT INTO referral_codes")
    assert insert_idx > 0
    after_insert = src[insert_idx:]
    # ON CONFLICT 之后必须再 SELECT(race fallback)
    after_conflict = after_insert.find("ON CONFLICT")
    assert after_conflict > 0
    after_conflict_snippet = after_insert[after_conflict:after_conflict + 1500]
    assert "SELECT code FROM referral_codes" in after_conflict_snippet, \
        "并发兜底:ON CONFLICT 之后必须 re-SELECT code"


def test_w3_gate_6_l0_referral_v35_gate():
    """process_recharge_commission_v3 含 V3.5 settlement_mode 防御 gate"""
    src = (ROOT / "api" / "referral_api.py").read_text(encoding="utf-8")
    # 必须 grep 到 V3.5 防御逻辑
    assert "v35_inventory_settlement" in src
    assert "agent_inventory_purchase" in src
    assert "拦截 V3.5 路径调用" in src or "V3.5 防御 gate" in src or "V3.5 路径" in src


def _strip_jsx_comments(src: str) -> str:
    """删 JSX/TS 注释:/* */ + // + JSDoc 顶部 · 防误伤禁词说明本身"""
    # 删 /* ... */ 块注释(含 JSDoc)
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    # 删 // 行注释
    out_lines = []
    for line in src.split("\n"):
        s = line
        # 简单粗暴:跳过 // 之后(忽略字符串内 // · 客户面页用 // 几率低)
        if "//" in s:
            s = s[:s.find("//")]
        out_lines.append(s)
    return "\n".join(out_lines)


def test_w3_gate_7_customer_frontend_no_forbidden_words():
    """W3 新建客户面页 · 禁词 rg 期望 0(排除 JSX/TS 注释)"""
    pages = [
        ROOT / "frontend" / "src" / "pages" / "Customer" / "CreditWallet.tsx",
        ROOT / "frontend" / "src" / "pages" / "Customer" / "BuyCredit.tsx",
    ]
    forbidden = ["目标出现率", "出现率目标", "已上榜", "冲刺中", "声量份额"]
    # SOV / share of voice 排除 · 因为禁词说明本身需要 mention(代码注释 strip 后无)
    violations = []
    for p in pages:
        if not p.exists(): continue
        text = _strip_jsx_comments(p.read_text(encoding="utf-8"))
        for kw in forbidden:
            if kw in text:
                violations.append(f"{p.name} contains '{kw}'")
        # SOV 单独严格 grep:不能作为整词出现
        for sov_word in ["SOV", "share of voice"]:
            if re.search(rf"\b{re.escape(sov_word)}\b", text):
                violations.append(f"{p.name} contains '{sov_word}'(strict word boundary)")
    assert not violations, f"客户面禁词: {violations}"


# ============================================================
# W4 gates
# ============================================================

def test_w4_gate_8_w4_schema_4_tables():
    """W4 migration 建 4 张表"""
    sql = (ROOT / "scripts" / "migration_v35_w4_2026_05_26.sql").read_text(encoding="utf-8")
    for table in ["customer_agent_binding_disputes", "agent_inventory_allocations",
                  "inventory_audit_runs", "inventory_audit_diffs"]:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, f"缺表 {table}"


def test_w4_gate_9_binding_dispute_history_insert():
    """customer_binding upsert 冲突分支 INSERT 到 disputes 表"""
    src = (ROOT / "services" / "customer_binding.py").read_text(encoding="utf-8")
    assert "customer_agent_binding_disputes" in src
    assert "INSERT INTO customer_agent_binding_disputes" in src


def test_w4_gate_10_audit_service_provided():
    """services/inventory_audit.py 提供 run_audit + list_audit_runs"""
    p = ROOT / "services" / "inventory_audit.py"
    assert p.exists()
    src = p.read_text(encoding="utf-8")
    assert "def run_audit" in src
    assert "def list_audit_runs" in src
    assert "DRIFT_THRESHOLD" in src


def test_w4_gate_11_admin_w4_api_endpoints():
    """admin_w4_api 4 个 endpoint:disputes list + patch + audit run + audit history"""
    src = (ROOT / "api" / "admin_w4_api.py").read_text(encoding="utf-8")
    assert "@router.get(\"/binding-disputes\")" in src
    assert "@router.patch(\"/binding-disputes/{dispute_id}\")" in src
    assert "@router.post(\"/inventory-audit/run\")" in src
    assert "@router.get(\"/inventory-audit/history\")" in src


def test_w4_gate_12_scheduler_audit_cron():
    """scheduler.py 注册 v35_inventory_audit_daily"""
    src = (ROOT / "api" / "scheduler.py").read_text(encoding="utf-8")
    assert "v35_inventory_audit_daily" in src
    assert "_v35_inventory_audit_daily" in src
    # 02:45
    assert "minute=45" in src and "hour=2" in src


# ============================================================
# Cross-cutting
# ============================================================

def test_all_w3_w4_py_compile():
    """W3 + W4 后端文件 py_compile PASS"""
    files = [
        "server.py",
        "api/agent_agreement_api.py",
        "api/customer_workbench_api.py",
        "api/admin_w4_api.py",
        "api/wallet_api.py",
        "api/referral_api.py",
        "api/scheduler.py",
        "services/agent_agreement.py",
        "services/customer_binding.py",
        "services/inventory_audit.py",
        "auth/agreement_gate.py",
        "db/wallet_db.py",
        "db/migrate_v35_w3.py",
        "db/migrate_v35_w4.py",
    ]
    r = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "py_compile"] + files,
        cwd=ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"py_compile failed: {r.stderr}"


def test_w4_audit_run_integration():
    """真实 PostgreSQL 上执行 W4 对账，并原子写入一次无漂移审计。"""
    from services.inventory_audit import run_audit

    url = os.environ["TEST_DATABASE_URL"]
    assert "test" in url.rsplit("/", 1)[-1].lower()
    with psycopg2.connect(url) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TEMP TABLE agent_inventory_wallets (
                paid_inventory_points BIGINT NOT NULL DEFAULT 0,
                bonus_inventory_points BIGINT NOT NULL DEFAULT 0,
                -- [2026-07-29 返修] 账实相符式把冻结池计入
                frozen_inventory_points BIGINT NOT NULL DEFAULT 0
            ) ON COMMIT DROP;
            CREATE TEMP TABLE customer_agent_credit_wallets (
                tool_credit_points BIGINT NOT NULL DEFAULT 0,
                publish_credit_points BIGINT NOT NULL DEFAULT 0,
                bonus_credit_points BIGINT NOT NULL DEFAULT 0
            ) ON COMMIT DROP;
            CREATE TEMP TABLE customer_credit_transactions (
                type TEXT NOT NULL, pool TEXT NOT NULL, points BIGINT NOT NULL,
                related_order_id TEXT
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_inventory_transactions (
                type TEXT NOT NULL, pool TEXT NOT NULL, points BIGINT NOT NULL,
                related_order_id TEXT
            ) ON COMMIT DROP;
            CREATE TEMP TABLE inventory_audit_runs (
                id BIGSERIAL PRIMARY KEY, run_at TIMESTAMP NOT NULL DEFAULT NOW(),
                triggered_by TEXT, triggered_by_user_id INTEGER,
                agent_total_paid BIGINT, agent_total_bonus BIGINT,
                customer_total_tool BIGINT, customer_total_publish BIGINT,
                customer_total_bonus BIGINT, platform_consumed BIGINT,
                historical_purchased BIGINT, historical_admin_adjust BIGINT,
                refunded_or_revoked BIGINT, diff_paid BIGINT, diff_bonus BIGINT,
                diff_publish BIGINT, has_drift BOOLEAN, notes TEXT,
                -- [2026-07-29 返修] 失败态 + 账实相符式两个量
                agent_total_frozen BIGINT DEFAULT 0,
                wallet_total BIGINT, ledger_total BIGINT,
                status TEXT DEFAULT 'ok', error_message TEXT
            ) ON COMMIT DROP;
            CREATE TEMP TABLE inventory_audit_diffs (
                id BIGSERIAL PRIMARY KEY, run_id BIGINT, track TEXT,
                diff_points BIGINT, detail JSONB
            ) ON COMMIT DROP;
        """)
        result = run_audit(cur, triggered_by="pytest", triggered_by_user_id=999)
        assert result["has_drift"] is False
        assert result["diff_paid"] == 0
        cur.execute(
            "SELECT triggered_by, triggered_by_user_id, has_drift "
            "FROM inventory_audit_runs WHERE id = %s",
            (result["run_id"],),
        )
        assert cur.fetchone() == ("pytest", 999, False)
        cur.execute("SELECT COUNT(*) FROM inventory_audit_diffs")
        assert cur.fetchone()[0] == 0
