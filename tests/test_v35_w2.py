"""
V3.5 W2 验收 gate · 14 条(对齐 V6 终稿章九 W2 边界)

1. 前端 platform_cost_cents grep 期望 0
2. 后端 api/agent_*.py raw_cost / platform_cost_cents grep 期望 0
3. 后端 api/agent_*.py tax_rate_bps / tax_mode grep 期望 0
4. Endpoint response 黑名单递归扫描(DTO schema 层验证)
5. ownership 403 测试(非 owner 代理访问其他客户必拒)
6. 线下划拨流水正确(代理-N · 客户+N · revenue_ledger 0 新增)
7. 提现 ledger 锁定(同一 ledger 不重复锁 · 部分提现后余额准确)
8. mark_paid 凭证非空 check
9. 客户线上付款 bindings 自动写入(UPDATE 不 INSERT 冲突)
10. 代理预付资金分支隔离 test(agent_inventory_purchase 6 表 0 写入)
11. v32_legacy 双写保险丝 gate(record_legacy_commission_tx import/call 期望 0)
12. pytest 全 PASS(W1 6 P0 + 11 P1 不回归)— 委托现有 tests/test_v35_factory_inventory.py
13. py_compile + frontend build PASS
14. viewport 检查(人工)

大部分在 prod 才能真跑 · 这里仅做单元/语义级实证。
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import psycopg2
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]


def _connect_test_db():
    """只连接根 conftest 已校验过的隔离 TEST_DATABASE_URL。"""
    url = os.environ["TEST_DATABASE_URL"]
    assert "test" in url.rsplit("/", 1)[-1].lower()
    return psycopg2.connect(url)


# ============================================================
# Gate 1: 前端 platform_cost_cents grep 期望 0
# ============================================================

def test_gate_1_frontend_no_platform_cost_cents():
    """前端 Agent 页不得出现 platform_cost_cents"""
    agent_dir = ROOT / "frontend" / "src" / "pages" / "Agent"
    components_dir = ROOT / "frontend" / "src" / "components" / "agent"
    found = []
    for d in [agent_dir, components_dir]:
        if not d.exists():
            continue
        for path in d.rglob("*.tsx"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "platform_cost_cents" in text:
                found.append(str(path))
    assert not found, f"前端 Agent 路径出现 platform_cost_cents: {found}"


# ============================================================
# Gate 2: api/agent_*.py raw cost 期望 0
# ============================================================

def _strip_docstrings_and_comments(src: str) -> str:
    """删除 module/function docstring + 单行注释 · 用于 grep gate 防误伤"""
    import io, tokenize
    out = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenizeError:
        return src
    skip_strings = set()
    # 标记每个三引号字符串(docstring 候选)
    prev_meaningful = None
    for tok in tokens:
        tt, ts, *_ = tok
        if tt == tokenize.STRING and (ts.startswith('"""') or ts.startswith("'''")):
            # docstring 一般在 module top / function/class 头
            if prev_meaningful in (None, "NEWLINE", "INDENT", "DEDENT", "ENCODING") or \
               (prev_meaningful and prev_meaningful.endswith(":")):
                skip_strings.add((tok.start, tok.end))
        if tt not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT):
            prev_meaningful = ts
    lines = src.split("\n")
    # 简单粗暴:把所有三引号 block 内容替换为空(保留行数)
    in_block = False
    quote = None
    for i, line in enumerate(lines):
        if not in_block:
            for q in ('"""', "'''"):
                if line.count(q) >= 2:
                    # 单行 docstring
                    start = line.find(q)
                    end = line.find(q, start + 3) + 3
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
    # 删除单行注释
    out_lines = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out_lines.append("")
        else:
            # 行尾 # 注释也清(忽略字符串内 # · 简化:取第一个不在 [' "] 之后的 #)
            out_lines.append(line)
    return "\n".join(out_lines)


def test_gate_2_agent_api_no_raw_cost():
    api_dir = ROOT / "api"
    pattern = re.compile(r"\b(platform_cost_cents|raw_cost_cents|raw_llm_cost)\b")
    violations = []
    for path in api_dir.glob("agent_*.py"):
        text = _strip_docstrings_and_comments(path.read_text(encoding="utf-8", errors="ignore"))
        for m in pattern.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            violations.append(f"{path.name}:{line_no} → {m.group()}")
    assert not violations, f"api/agent_*.py 出现 raw cost: {violations}"


# ============================================================
# Gate 3: api/agent_*.py tax 规则字段期望 0
# ============================================================

def test_gate_3_agent_api_no_tax_rules():
    """代理 API 不得返回 tax_rate_bps / tax_mode / gateway_fee_bps / settlement_service_fee_bps"""
    api_dir = ROOT / "api"
    pattern = re.compile(r"\b(tax_rate_bps|tax_mode|gateway_fee_bps|settlement_service_fee_bps)\b")
    violations = []
    for path in api_dir.glob("agent_*.py"):
        text = _strip_docstrings_and_comments(path.read_text(encoding="utf-8", errors="ignore"))
        for m in pattern.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            line = text.split("\n")[line_no - 1]
            violations.append(f"{path.name}:{line_no} → {m.group()} :: {line.strip()[:80]}")
    assert not violations, f"api/agent_*.py 出现 tax 规则字段: {violations}"


# ============================================================
# Gate 4: pydantic DTO 黑名单递归扫描
# ============================================================

def test_gate_4_dto_response_blacklist():
    """Agent*Response 不得含黑名单字段"""
    from schemas import v35_w2_dto as dto
    forbidden = {
        "platform_cost_cents", "raw_cost", "raw_llm_cost",
        "tax_rate_bps", "tax_mode",
        "gateway_fee_bps", "settlement_service_fee_bps",
    }
    leaks = []
    for name in dir(dto):
        if not name.startswith("Agent") or not name.endswith("Response"):
            continue
        cls = getattr(dto, name)
        if not hasattr(cls, "model_fields"):
            continue
        for field_name in cls.model_fields.keys():
            if field_name in forbidden:
                leaks.append(f"{name}.{field_name}")
    assert not leaks, f"Agent*Response DTO 漏字段: {leaks}"


def test_gate_4_admin_dto_has_raw_fields():
    """Admin*Response 应能含 admin-only 规则字段(验证 DTO 分离方向正确)"""
    from schemas.v35_w2_dto import AdminTaxProfileResponse
    # tax 规则字段 admin 可见 · agent 不可见
    admin_fields = set(AdminTaxProfileResponse.model_fields.keys())
    assert "default_tax_rate_bps" in admin_fields, (
        "AdminTaxProfileResponse 应保留 default_tax_rate_bps · admin 可见 admin-only 规则"
    )
    assert "default_tax_mode" in admin_fields


# ============================================================
# Gate 5-8: 业务逻辑判别
# ============================================================

def test_gate_5_ownership_403(monkeypatch):
    """非 owner 代理调 /api/agent/customers/{other}/credit 必须 403"""
    from api import agent_workbench_api

    monkeypatch.setattr(
        agent_workbench_api,
        "get_customer_binding",
        lambda _cursor, _customer_user_id: {"agent_user_id": 202},
    )
    with pytest.raises(HTTPException) as exc:
        agent_workbench_api._require_customer_owned_by_agent(object(), 101, 303)
    assert exc.value.status_code == 403


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_gate_6_offline_allocation_balance():
    """线下划拨:代理 paid -N · 客户 tool +N · agent_revenue_ledger 0 新增"""
    from services.agent_inventory import allocate_offline
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import allocate_credit 已删除。

    with _connect_test_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TEMP TABLE agent_inventory_wallets (
                agent_user_id INTEGER PRIMARY KEY,
                paid_inventory_points BIGINT NOT NULL DEFAULT 0,
                bonus_inventory_points BIGINT NOT NULL DEFAULT 0,
                frozen_inventory_points BIGINT NOT NULL DEFAULT 0,
                total_purchased_points BIGINT NOT NULL DEFAULT 0,
                total_allocated_points BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT NOW()
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_inventory_transactions (
                id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL,
                type TEXT NOT NULL, pool TEXT NOT NULL, points BIGINT NOT NULL,
                balance_paid_after BIGINT NOT NULL, balance_bonus_after BIGINT NOT NULL,
                related_customer_user_id INTEGER, related_order_id TEXT,
                description TEXT, created_at TIMESTAMP DEFAULT NOW()
            ) ON COMMIT DROP;
            CREATE TEMP TABLE customer_agent_credit_wallets (
                customer_user_id INTEGER PRIMARY KEY, agent_user_id INTEGER NOT NULL,
                tool_credit_points BIGINT NOT NULL DEFAULT 0,
                publish_credit_points BIGINT NOT NULL DEFAULT 0,
                bonus_credit_points BIGINT NOT NULL DEFAULT 0,
                total_purchased_points BIGINT NOT NULL DEFAULT 0,
                total_consumed_points BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT NOW()
            ) ON COMMIT DROP;
            CREATE TEMP TABLE customer_credit_transactions (
                id BIGSERIAL PRIMARY KEY, customer_user_id INTEGER NOT NULL,
                agent_user_id INTEGER NOT NULL, type TEXT NOT NULL, pool TEXT NOT NULL,
                points BIGINT NOT NULL, balance_tool_after BIGINT NOT NULL,
                balance_publish_after BIGINT NOT NULL, balance_bonus_after BIGINT NOT NULL,
                feature_code TEXT, related_order_id TEXT, source TEXT, description TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_revenue_ledger (id BIGSERIAL PRIMARY KEY) ON COMMIT DROP;
            INSERT INTO agent_inventory_wallets
                (agent_user_id, paid_inventory_points, bonus_inventory_points)
            VALUES (101, 1000, 200);
        """)

        agent_after = allocate_offline(cur, 101, 303, paid_points=300, bonus_points=50)
        customer_after = allocate_credit(
            cur,
            customer_user_id=303,
            agent_user_id=101,
            tool_points=200,
            publish_points=100,
            bonus_points=50,
            related_order_id="offline_w2_gate",
            source="offline_allocation",
            record_bonus_grant=False,
        )
        cur.execute("SELECT COUNT(*) FROM agent_revenue_ledger")
        assert cur.fetchone()[0] == 0
        assert agent_after["paid_inventory_points"] == 700
        assert agent_after["bonus_inventory_points"] == 150
        assert customer_after == {
            "tool_credit_points": 200,
            "publish_credit_points": 100,
            "bonus_credit_points": 50,
            "total_allocated": 350,
        }


def test_gate_7_settlement_request_lock(monkeypatch):
    """同一 ledger 不重复锁定 · 部分提现后余额准确"""
    from services import agent_pricing
    from services.agent_revenue import create_settlement_request, get_agent_balance

    monkeypatch.setattr(
        agent_pricing,
        "calc_withdrawal_fees",
        lambda _cursor, _agent_id, gross: {
            "gateway_fee_cents": 0,
            "settlement_fee_cents": 0,
            "tax_cents": 0,
            "platform_fee_cents": 0,
            "total_fee_cents": 0,
            "net_cents": gross,
        },
    )
    with _connect_test_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TEMP TABLE agent_revenue_ledger (
                id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL,
                agent_settlement_cents BIGINT NOT NULL, status TEXT NOT NULL,
                reversed_at TIMESTAMP, settled_at TIMESTAMP
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_settlement_requests (
                id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL,
                request_amount_cents BIGINT NOT NULL, bank_name TEXT,
                bank_account TEXT, account_holder TEXT, invoice_required BOOLEAN,
                status TEXT NOT NULL, gateway_fee_cents BIGINT,
                settlement_fee_cents BIGINT, tax_cents BIGINT, net_amount_cents BIGINT
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_settlement_request_items (
                settlement_request_id BIGINT NOT NULL, ledger_id BIGINT NOT NULL,
                locked_amount_cents BIGINT NOT NULL
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_commission_redemption_requests (
                id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL, status TEXT NOT NULL
            ) ON COMMIT DROP;
            CREATE TEMP TABLE agent_commission_redemption_items (
                redemption_request_id BIGINT NOT NULL, ledger_id BIGINT NOT NULL,
                locked_amount_cents BIGINT NOT NULL
            ) ON COMMIT DROP;
            INSERT INTO agent_revenue_ledger
                (agent_user_id, agent_settlement_cents, status, settled_at)
            VALUES (101, 10000, 'settled', NOW());
        """)

        first = create_settlement_request(cur, 101, 3000, "bank", "acct", "holder")
        second = create_settlement_request(cur, 101, 4000, "bank", "acct", "holder")
        assert first["total_locked_cents"] == 3000
        assert second["total_locked_cents"] == 4000
        cur.execute("SELECT COUNT(*), SUM(locked_amount_cents) FROM agent_settlement_request_items")
        assert cur.fetchone() == (2, 7000)
        assert get_agent_balance(cur, 101)["available_cents"] == 3000


def test_gate_8_mark_paid_proof_required():
    """mark_paid 必须填 transfer_proof_url 或 wire_transfer_no"""
    # 通过读 admin_factory_api.py 源码检查防御性 check 存在
    src = (ROOT / "api" / "admin_factory_api.py").read_text(encoding="utf-8")
    assert "transfer_proof_url and not req.wire_transfer_no" in src or \
           ("transfer_proof_url" in src and "wire_transfer_no" in src and "raise HTTPException" in src), \
        "admin_factory_api.py mark_paid 缺凭证非空 check"


# ============================================================
# Gate 9: bindings UNIQUE 冲突走 UPDATE 不 INSERT
# ============================================================

def test_gate_9_binding_dispute_on_conflict():
    """customer_binding.upsert 冲突时走 UPDATE dispute_status='pending' 不 INSERT"""
    src = (ROOT / "services" / "customer_binding.py").read_text(encoding="utf-8")
    # 冲突分支必须用 UPDATE + dispute_status='pending'
    assert "dispute_status = 'pending'" in src or "dispute_status='pending'" in src, \
        "customer_binding 冲突分支应 UPDATE dispute_status"
    # 严禁第二次 INSERT
    insert_count = src.count("INSERT INTO customer_agent_bindings")
    assert insert_count <= 1, f"customer_agent_bindings 应至多 1 处 INSERT(ON CONFLICT)· 实际 {insert_count}"


# ============================================================
# Gate 10: 代理预付资金分支隔离
# ============================================================

def test_gate_10_agent_inventory_purchase_isolation():
    """complete_recharge 早分支:6 表不写 + 不进 Orchestrator"""
    src = (ROOT / "db" / "wallet_db.py").read_text(encoding="utf-8")
    # 早分支必须存在
    assert "order_type == \"agent_inventory_purchase\"" in src or \
           "order_type == 'agent_inventory_purchase'" in src, \
        "complete_recharge 缺 agent_inventory_purchase 早分支"
    # 早分支必须 return 不进 Orchestrator(找早分支 block 内 return · 限定在 if 块缩进内)
    early_branch_idx = src.find("'agent_inventory_purchase'")
    if early_branch_idx < 0:
        early_branch_idx = src.find('"agent_inventory_purchase"')
    assert early_branch_idx > 0
    # 找紧随其后的下一个独立 return 语句(必须在早分支内 · 不能是其他 client recharge 路径的 return)
    # 用更宽阈值 + 限定首个 return 出现在 if/elif 之前
    normal_wallet_idx = src.index("# 入账(user_wallets", early_branch_idx)
    snippet = src[early_branch_idx:normal_wallet_idx]
    return_idx = snippet.find("return dict(order)")
    next_else_or_branch = re.search(r"\n        # =+\n        # 4\.", snippet)
    assert return_idx > 0, "agent_inventory_purchase 早分支必须有 return"
    if next_else_or_branch:
        assert return_idx < next_else_or_branch.start(), \
            "return 必须在客户充值正常路径分隔注释之前(否则不是早分支 return)"


# ============================================================
# Gate 11: v32_legacy 双写保险丝
# ============================================================

def test_gate_11_v32_legacy_no_double_write():
    """rg `record_legacy_commission_tx` import/call/def 期望 0(commit/non-comment)"""
    for path_str in ["api/referral_api.py", "services/settlement_orchestrator.py"]:
        path = ROOT / path_str
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # 检查非注释行
        for i, line in enumerate(text.split("\n"), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # docstring 内禁令文字允许出现
            if '"""' in line or "'''" in line:
                continue
            # 匹配 import 或 call · 排除 docstring 引号包裹的引用
            if re.search(r"\bfrom\s+.*\s+import\s+.*record_legacy_commission_tx\b", line):
                pytest.fail(f"{path.name}:{i} import record_legacy_commission_tx · 双写保险丝违反")
            if re.search(r"^\s*record_legacy_commission_tx\(", line):
                pytest.fail(f"{path.name}:{i} 调用 record_legacy_commission_tx · 双写保险丝违反")
    # api/referral_api.py 不得定义
    referral = ROOT / "api" / "referral_api.py"
    if referral.exists():
        text = referral.read_text(encoding="utf-8")
        assert not re.search(r"^def record_legacy_commission_tx", text, re.MULTILINE), \
            "api/referral_api.py 定义了 record_legacy_commission_tx · 双写保险丝违反"


# ============================================================
# Gate 12: W1 集成测试不回归(委托 tests/test_v35_factory_inventory.py)
# ============================================================

def test_gate_12_w1_no_regression_pointer():
    """指针测试:确认 W1 集成测试文件存在 · 实际跑由 pytest -k pattern 全跑"""
    assert (ROOT / "tests" / "test_v35_factory_inventory.py").exists(), \
        "W1 集成测试文件缺失"


# ============================================================
# Gate 13: py_compile + frontend build
# ============================================================

def test_gate_13_backend_py_compile():
    """所有 W2 后端文件 py_compile PASS"""
    files = [
        "server.py",
        "api/agent_workbench_api.py",
        "api/admin_factory_api.py",
        "schemas/v35_w2_dto.py",
        "services/customer_binding.py",
        "db/wallet_db.py",
        "db/migrate_v35_w2.py",
    ]
    r = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "py_compile"] + files,
        cwd=ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"py_compile failed: {r.stderr}"


def test_gate_13_frontend_build():
    """审核包本机必须能完成真实 TypeScript/Vite 构建。"""
    npm = "npm.cmd" if os.name == "nt" else "npm"
    r = subprocess.run(
        [npm, "run", "build"],
        cwd=ROOT / "frontend",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert r.returncode == 0, f"frontend build failed:\n{r.stdout}\n{r.stderr}"


# ============================================================
# Gate 14: viewport 静态回归
# ============================================================

def test_gate_14_viewport():
    """库存、客户钱包和结算关键布局必须保留窄屏单列及表格横向滚动。"""
    inventory = (ROOT / "frontend/src/pages/Agent/InventoryCenter.tsx").read_text(encoding="utf-8")
    wallet = (ROOT / "frontend/src/pages/Customer/CreditWallet.tsx").read_text(encoding="utf-8")
    settlement = (ROOT / "frontend/src/pages/Agent/SettlementCenter.tsx").read_text(encoding="utf-8")
    assert "grid-cols-1" in inventory and "sm:grid-cols-" in inventory
    assert "overflow-x-auto" in inventory
    assert "grid-cols-1" in wallet and "overflow-x-auto" in wallet
    assert "grid-cols-1" in settlement and "overflow-x-auto" in settlement
