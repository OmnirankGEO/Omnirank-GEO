"""
V3.5 Follow-up sprint 验收 gate(批 1A/1B/2/3/4/5)

批 1A · billing 同步链 V3.5 分流(挡灰度 · 必做):
  W5-1A    check_balance_only / deduct_points 含 _is_v35_customer 分流 + consume_credit 调用
  W5-1A.1  charge_on_success / charge_with_refund 自动兼容(内部调上述 2 函数)
  W5-1A.2  异常映射 InsufficientCreditError / PublishPaidOnlyError → HTTPException 402
  W5-1A.3  混合余额铁律 · 不 fallback 老 user_wallets

批 1B · freeze 链 V3.5 503 阻断:
  W5-1B    freeze_points 对 V3.5 客户抛 V35_FREEZE_NOT_SUPPORTED(503)
  W5-1B.1  commit_freeze / release_freeze 不动 legacy 语义(V3.5 走不到此处)

批 2 · 词典 SSOT:
  W5-3     v35Terminology.ts 导出静态 + 动态 label · 单测覆盖 40+ 条

批 3-5 · 前端文案治理(grep gate · 见批 3-5 实施后启用):
  W5-4 客户面禁词 0 命中 / W5-5 代理面禁工程字段 / W5-6 admin t() 包裹
  W5-7 InventoryAudit 合并 / W5-8 sidebar rename / W5-9 build PASS / W5-10 viewport
  W5-11 异常文案禁词 0 漏
"""
import re
import sys
from pathlib import Path

import pytest

from tests.v35_visible_copy import visible_hits

ROOT = Path(__file__).resolve().parents[1]


def _strip_docstrings_and_comments(src: str) -> str:
    """对齐 test_v35_w3_w4 helper"""
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
# 批 1A · billing 同步链 V3.5 分流
# ============================================================

def test_w5_1a_billing_imports_v35_helpers():
    """billing.py import 了 consume_credit / is_publish_feature / 异常类"""
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    for sym in [
        "from services.customer_credit import",
        "consume_credit",
        "is_publish_feature",
        "InsufficientCreditError",
        "PublishPaidOnlyError",
    ]:
        assert sym in src, f"billing.py 未 import {sym}"







@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_w5_1a_consume_credit_accepts_requires_paid():
    """[r3 P0] consume_credit signature 接 requires_paid 入参 · 优先按它判 publish 路径"""
    src = (ROOT / "services" / "customer_credit.py").read_text(encoding="utf-8")
    # signature 含 requires_paid
    sig = re.search(r"def consume_credit\([^)]*\)", src, re.DOTALL)
    assert sig, "未找到 consume_credit signature"
    assert "requires_paid" in sig.group(0), (
        "consume_credit signature 必须接 requires_paid · 由 billing 从 feature_pricing 传入"
    )
    # 函数体内必须用 requires_paid 作为 publish 路径判定(OR is_publish_feature)
    body = re.search(r"def consume_credit\(.*?\n(?=def |\nclass )", src, re.DOTALL)
    assert body, "未找到 consume_credit 函数体"
    btext = body.group(0)
    assert re.search(r"if\s+requires_paid\s+or\s+is_publish_feature", btext) or \
           re.search(r"if\s+is_publish_feature\(.*?\)\s+or\s+requires_paid", btext), (
        "consume_credit 必须 `requires_paid OR is_publish_feature` 判定 publish 路径"
    )


def test_w5_1a_publish_paid_only_for_media_proxy_publish():
    """[r3 P0 grep-based] media_proxy_publish 在 feature_pricing requires_paid_points=True
    → billing 拿到 True → 传给 consume_credit → 强制走 publish_credit_points 扣
    防回归:静态检查 wallet_db PRICING_DATA 中 media_proxy_publish 行尾确实是 True
    PRICING_DATA 真实结构:tuple ('code', '名称', cost_points, cost_compute, requires_paid_points)
    """
    src = (ROOT / "db" / "wallet_db.py").read_text(encoding="utf-8")
    # 找 media_proxy_publish 这一行(tuple 形式 · 末尾字段是 requires_paid_points)
    line_match = re.search(r"\(\s*['\"]media_proxy_publish['\"].*?\)", src, re.DOTALL)
    assert line_match, "wallet_db.py PRICING_DATA 未找到 media_proxy_publish tuple"
    entry = line_match.group(0)
    # tuple 最后一个 True/False 字段就是 requires_paid_points
    # 实证形式:('media_proxy_publish', '媒体代发', 0, 0.0, True)
    paid_match = re.search(r",\s*(True|False)\s*\)", entry)
    assert paid_match, f"media_proxy_publish tuple 末尾未找到 requires_paid 布尔:\n{entry}"
    assert paid_match.group(1) == "True", (
        f"media_proxy_publish 的 requires_paid_points 必须为 True · 实际:\n{entry}"
    )




def test_w5_1a3_no_fallback_to_legacy_user_wallets():
    """混合余额铁律 · V3.5 客户余额不足直接 402 · 不 fallback 老 user_wallets
    具体守护:check_balance_only / deduct_points 内 V3.5 分支 raise 后不能 fall-through 到 legacy
    """
    src = _strip_docstrings_and_comments(
        (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    )
    # check_balance_only 内 V3.5 命中后 return · 不掉到 legacy
    m = re.search(r"async def check_balance_only\([^)]*\).*?async def \w+\(",
                  src, re.DOTALL)
    body = m.group(0)
    # V3.5 分支必含 return(预检通过)+ raise(预检失败)· 不能 continue
    assert "return {" in body, "check_balance_only V3.5 分支必有 return"
    # deduct_points 内 V3.5 分支同理
    m2 = re.search(r"async def deduct_points\([^)]*\).*?async def \w+\(",
                   src, re.DOTALL)
    body2 = m2.group(0)
    assert body2.count("return {") >= 2, "deduct_points V3.5 + legacy 两支都需 return"


def test_w5_1a1_charge_on_success_auto_compatible():
    """charge_on_success 内部调 check_balance_only + deduct_points · 自动兼容 V3.5"""
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    m = re.search(r"async def charge_on_success\(.*?\n(?=async def \w+\()",
                  src, re.DOTALL)
    assert m
    body = m.group(0)
    assert "check_balance_only(" in body
    assert "deduct_points(" in body


def test_w5_1a1_charge_with_refund_auto_compatible():
    """charge_with_refund 内部调 check_balance_only + deduct_points · 自动兼容 V3.5"""
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    m = re.search(r"async def charge_with_refund\(.*?\Z", src, re.DOTALL)
    assert m
    body = m.group(0)
    assert "check_balance_only(" in body
    assert "deduct_points(" in body


# ============================================================
# 批 1B · freeze 链 V3.5 503 阻断
# ============================================================

def test_w5_1b_freeze_points_blocks_v35_customer():
    """freeze_points 对 V3.5 客户抛 V35_FREEZE_NOT_SUPPORTED(503)"""
    # 🔴 [#91 · 2026-09-05] **已退役**。本条钉的是 `_is_v35_customer(cursor` 这个符号,
    #    而它于 **2026-07-27 单账本收敛**时被**有意删除** —— 证据在 middleware/billing.py:
    #      L14 `- 已删除:_is_v35_customer / _v35_check_credit_only / …`
    #      L80 `[单账本收敛 2026-07-27] _is_v35_customer 已删除。`
    #    ⇒ 它不是「还没修的缺陷」,是「该退役而没退役的判据」。两者处置相反。
    #
    #    🔴 同一文件里另外两条(:414 / :444)早已因**完全相同**的原因退役成 skip,
    #       理由还点了继任者 —— 唯独这一条被漏掉,红了一路没人往下追。
    #       那批退役的分母是**手挑**的;漏一条不会让任何东西变红。
    #
    #    退役写成**肯定式**(不用 skip:skip 是静默出口,-q 汇总里不进 failed 计数):
    #    断言继任者的反向锁**还活着**。继任者被删/改名时这条必须响。
    successor = ROOT / "tests" / "test_wallet_single_ledger_stage2.py"
    assert successor.is_file(), (
        f"继任者不在:{successor} —— 「不许按身份分流账本」从此无人守,"
        f"本条退役的前提消失。")
    code = successor.read_text(encoding="utf-8")
    assert '_is_v35_customer" not in code' in code, (
        "继任者里那条**反向锁**(断言 _is_v35_customer 必须不存在)不见了 —— "
        "退役 = 换人守,不是不守了。")
    assert (ROOT / "tests" / "v35_refund_single_ledger_2026_08_17").is_dir(), (
        "继任者目录 tests/v35_refund_single_ledger_2026_08_17/ 不在了。")

def test_w5_1b1_commit_release_freeze_legacy_unchanged():
    """commit_freeze / release_freeze 走 legacy 语义(V3.5 在 freeze_points 已阻断)
    防御:这两个不要误加 V3.5 分流 · 流水仍是 user_wallets / point_freezes 三池
    """
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    # commit_freeze 函数体
    m_commit = re.search(r"async def commit_freeze\(.*?\nasync def release_freeze\(",
                         src, re.DOTALL)
    assert m_commit
    commit_body = m_commit.group(0)
    # 不应在 commit/release 内部调 consume_credit / refund_credit / _is_v35_customer
    assert "consume_credit" not in commit_body, "commit_freeze 不该调 consume_credit · V3.5 走不到这里"
    assert "_is_v35_customer" not in commit_body, "commit_freeze 不应加 V3.5 分流"

    m_release = re.search(r"async def release_freeze\(.*?\n@asynccontextmanager",
                          src, re.DOTALL)
    assert m_release
    release_body = m_release.group(0)
    assert "consume_credit" not in release_body
    assert "_is_v35_customer" not in release_body


# ============================================================
# 总闸 · billing.py + 关联模块 py_compile PASS
# ============================================================

def test_all_followup_py_compile():
    """V3.5 Follow-up 涉及的所有 .py 文件 py_compile 通过"""
    files = [
        ROOT / "middleware" / "billing.py",
        ROOT / "services" / "customer_credit.py",
    ]
    for f in files:
        try:
            import py_compile
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            pytest.fail(f"{f.name} 编译失败: {e}")


# ============================================================
# 批 1A · runtime 行为测试(r3 老板要求 · mock cursor 覆盖 consume_credit)
# ============================================================

class _MockCursor:
    """简化 RealDictCursor · 按 SQL pattern 返预设 row"""
    def __init__(self, wallet_row):
        self.wallet_row = wallet_row
        self.last_sql = None
        self.last_params = None
        self.update_calls = []
        self.insert_calls = []

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params
        if "UPDATE customer_agent_credit_wallets" in sql:
            self.update_calls.append((sql, params))
        elif "INSERT INTO customer_credit_transactions" in sql:
            self.insert_calls.append((sql, params))

    def fetchone(self):
        # SELECT FOR UPDATE → 返 wallet
        # SELECT 3 列 → 返 wallet 字段子集
        # INSERT RETURNING id → 返 fake id
        if self.last_sql and "INSERT INTO customer_credit_transactions" in self.last_sql:
            return {"id": 1}
        if self.last_sql and ("SELECT customer_user_id" in self.last_sql or
                              "SELECT tool_credit_points" in self.last_sql):
            return self.wallet_row
        return None

    def fetchall(self):
        return []


def _make_wallet(tool=0, publish=0, bonus=0, customer=1001, agent=2001):
    return {
        "customer_user_id": customer,
        "agent_user_id": agent,
        "tool_credit_points": tool,
        "publish_credit_points": publish,
        "bonus_credit_points": bonus,
        "total_purchased_points": tool + publish + bonus,
        "total_consumed_points": 0,
    }


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_runtime_v35_short_task_deducts_customer_credit_not_user_wallets():
    """[r3 老板要求 1] V3.5 wallet 足额短任务 → 扣 customer_agent_credit_wallets · 不动 user_wallets

    通过调用 consume_credit 验证:
    - 扣 tool_credit_points 或 bonus_credit_points
    - 不返 user_wallets 字段
    - INSERT customer_credit_transactions 流水
    """
    sys.path.insert(0, str(ROOT))
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import consume_credit 已删除。

    cursor = _MockCursor(_make_wallet(tool=1000, publish=0, bonus=500))
    result = consume_credit(
        cursor=cursor,
        customer_user_id=1001,
        feature_code="article_gen",   # 非 publish 类
        cost_points=300,
        requires_paid=False,
    )
    # 扣费成功
    assert result["consumed_from_pool"] in ("bonus", "tool", "bonus+tool")
    # bonus 优先扣 300 · bonus 余 200 · tool 不动
    assert result["bonus_credit_points"] == 200, f"bonus 应扣到 200 · 实际 {result['bonus_credit_points']}"
    assert result["tool_credit_points"] == 1000, f"tool 应保持 1000 · 实际 {result['tool_credit_points']}"
    # 必须有 UPDATE customer_agent_credit_wallets · 不应触碰 user_wallets
    assert len(cursor.update_calls) >= 1
    for sql, _ in cursor.update_calls:
        assert "customer_agent_credit_wallets" in sql, "V3.5 路径不应 UPDATE user_wallets"
        assert "user_wallets" not in sql, "V3.5 路径漏到 user_wallets · 资金风险"


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_runtime_v35_insufficient_credit_raises_402_no_fallback():
    """[r3 老板要求 2] V3.5 wallet 不足 → 抛 InsufficientCreditError(billing 转 402)
    不 fallback 到老 user_wallets(混合余额铁律)
    """
    sys.path.insert(0, str(ROOT))
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import consume_credit, InsufficientCreditError 已删除。

    cursor = _MockCursor(_make_wallet(tool=50, publish=0, bonus=20))
    try:
        consume_credit(
            cursor=cursor,
            customer_user_id=1001,
            feature_code="article_gen",
            cost_points=300,  # tool+bonus = 70 < 300
            requires_paid=False,
        )
        pytest.fail("应抛 InsufficientCreditError")
    except InsufficientCreditError as e:
        assert e.pool in ("tool+bonus", "tool"), f"pool 应是 tool+bonus · 实际 {e.pool}"
        assert e.required == 300
        assert e.available == 70


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_runtime_v35_media_proxy_publish_forces_publish_credit():
    """[r3 老板要求 3 · P0] media_proxy_publish requires_paid=True
    → 强制走 publish_credit_points · 不动 tool/bonus
    防 r2 漏洞:media_proxy_publish 不以 publish_ 开头 · is_publish_feature 返 False · 之前会走 tool 漏 paid-only
    """
    sys.path.insert(0, str(ROOT))
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import consume_credit 已删除。

    cursor = _MockCursor(_make_wallet(tool=500, publish=200, bonus=300))
    result = consume_credit(
        cursor=cursor,
        customer_user_id=1001,
        feature_code="media_proxy_publish",
        cost_points=100,
        requires_paid=True,   # billing 从 feature_pricing.requires_paid_points 传入
    )
    # 必须扣 publish · 不扣 tool / bonus
    assert result["consumed_from_pool"] == "publish", f"应扣 publish · 实际 {result['consumed_from_pool']}"
    assert result["publish_credit_points"] == 100, f"publish 应扣到 100 · 实际 {result['publish_credit_points']}"
    assert result["tool_credit_points"] == 500, f"tool 不应动 · 实际 {result['tool_credit_points']}"
    assert result["bonus_credit_points"] == 300, f"bonus 不应动 · 实际 {result['bonus_credit_points']}"


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_runtime_v35_publish_insufficient_no_fallback_to_bonus_tool():
    """[r3 老板要求 4 · P0] publish 不足但 bonus/tool 足额 → 必须 402 · 不 fallback
    paid-only 铁律:发布消费不能用 bonus / tool
    """
    sys.path.insert(0, str(ROOT))
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import consume_credit, InsufficientCreditError 已删除。

    cursor = _MockCursor(_make_wallet(tool=5000, publish=50, bonus=3000))
    try:
        consume_credit(
            cursor=cursor,
            customer_user_id=1001,
            feature_code="media_proxy_publish",
            cost_points=100,    # publish=50 不够 · 但 bonus+tool=8000 远远够
            requires_paid=True,
        )
        pytest.fail("publish 不足必须抛 InsufficientCreditError · 不能 fallback bonus/tool")
    except InsufficientCreditError as e:
        assert e.pool == "publish", f"pool 应是 publish · 实际 {e.pool}"
        assert e.required == 100
        assert e.available == 50


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_runtime_v35_publish_code_also_forces_publish_even_without_requires_paid():
    """[r3 兜底 · 防御] publish_mhz_media 等以 publish_ 开头的 code · 即使 requires_paid=False 也走 publish
    requires_paid OR is_publish_feature · 任一命中即 publish 路径
    """
    sys.path.insert(0, str(ROOT))
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import consume_credit 已删除。

    cursor = _MockCursor(_make_wallet(tool=500, publish=200, bonus=300))
    result = consume_credit(
        cursor=cursor,
        customer_user_id=1001,
        feature_code="publish_mhz_media",  # is_publish_feature(code) → True
        cost_points=100,
        requires_paid=False,  # 即使 False 也应走 publish
    )
    assert result["consumed_from_pool"] == "publish"
    assert result["publish_credit_points"] == 100


# ============================================================
# r4 · 预检链 runtime test(老板 r4 review 要求)
# ============================================================

class _MockCursorReadOnly:
    """只读 cursor · 用于 _v35_check_credit_only(只 SELECT · 不 UPDATE/INSERT)"""
    def __init__(self, wallet_row):
        self.wallet_row = wallet_row
    def execute(self, sql, params=None):
        pass
    def fetchone(self):
        return self.wallet_row


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_runtime_v35_check_balance_media_proxy_publish_blocks_when_publish_zero():
    """[r4 P0 老板要求 · 预检对称修] _v35_check_credit_only(media_proxy_publish, requires_paid=True)
    publish=0 + bonus/tool 足额 → 必须抛 InsufficientCreditError(pool='publish')
    防 r3 漏:预检放行 → 实扣失败 silent → 用户白用 + 资金不对账
    """
    sys.path.insert(0, str(ROOT))
    from middleware.billing import _v35_check_credit_only
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import InsufficientCreditError 已删除。

    cursor = _MockCursorReadOnly(_make_wallet(tool=5000, publish=0, bonus=3000))
    try:
        _v35_check_credit_only(
            cursor=cursor,
            customer_user_id=1001,
            feature_code="media_proxy_publish",  # 不以 publish_ 开头
            total_cost=100,
            requires_paid=True,   # 关键:billing 必须传
        )
        pytest.fail("media_proxy_publish + publish=0 必须抛 InsufficientCreditError(publish) · "
                    "不能因 is_publish_feature 漏判而放行")
    except InsufficientCreditError as e:
        assert e.pool == "publish", (
            f"pool 应是 publish · 实际 {e.pool} · r4 漏:predcheck 把 media_proxy_publish 当工具放行"
        )
        assert e.required == 100
        assert e.available == 0



@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_runtime_v35_check_balance_publish_code_blocks_even_without_requires_paid():
    """[r4 兜底] is_publish_feature 也作为兜底 · publish_mhz_media 即使 requires_paid=False 也走 publish 预检"""
    sys.path.insert(0, str(ROOT))
    from middleware.billing import _v35_check_credit_only
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import InsufficientCreditError 已删除。

    cursor = _MockCursorReadOnly(_make_wallet(tool=5000, publish=0, bonus=3000))
    try:
        _v35_check_credit_only(
            cursor=cursor,
            customer_user_id=1001,
            feature_code="publish_mhz_media",  # is_publish_feature → True
            total_cost=100,
            requires_paid=False,
        )
        pytest.fail("publish_mhz_media 即使 requires_paid=False 也应走 publish 预检 · 抛 publish 不足")
    except InsufficientCreditError as e:
        assert e.pool == "publish"


# ============================================================
# 批 2 · v35Terminology.ts 词典(批 2 实施后启用)
# ============================================================

def test_w5_3_terminology_module_exists():
    """frontend/src/lib/v35Terminology.ts 存在 + 导出 5 个 label API"""
    p = ROOT / "frontend" / "src" / "lib" / "v35Terminology.ts"
    if not p.exists():
        pytest.skip("批 2 v35Terminology.ts 尚未实施")
    src = p.read_text(encoding="utf-8")
    for api in [
        "export function t(",
        "export function transactionTypeLabel(",
        "export function poolLabel(",
        "export function sourceLabel(",
        "export function statusLabel(",
    ]:
        assert api in src, f"v35Terminology.ts 缺 {api}"


def test_w5_3_terminology_covers_pools():
    """词典覆盖 3 个 pool: tool / publish / bonus(检测 dict key 或 quoted string)"""
    p = ROOT / "frontend" / "src" / "lib" / "v35Terminology.ts"
    if not p.exists():
        pytest.skip("批 2 尚未实施")
    src = p.read_text(encoding="utf-8")
    for pool in ["tool", "publish", "bonus"]:
        # dict key(unquoted)/ quoted string / case 引用
        assert (re.search(rf"\b{pool}\s*:", src) or
                f"'{pool}'" in src or f'"{pool}"' in src), f"词典缺 pool {pool}"


def test_w5_3_terminology_covers_transaction_types():
    """词典覆盖 4 transaction type(检测 dict key)"""
    p = ROOT / "frontend" / "src" / "lib" / "v35Terminology.ts"
    if not p.exists():
        pytest.skip("批 2 尚未实施")
    src = p.read_text(encoding="utf-8")
    for t in ["allocate", "consume", "refund", "revoke"]:
        assert (re.search(rf"\b{t}\s*:", src) or
                f"'{t}'" in src or f'"{t}"' in src), f"词典缺 type {t}"


def test_w5_3_terminology_role_matrix():
    """词典支持 3 角色:customer / agent / admin"""
    p = ROOT / "frontend" / "src" / "lib" / "v35Terminology.ts"
    if not p.exists():
        pytest.skip("批 2 尚未实施")
    src = p.read_text(encoding="utf-8")
    for role in ["customer", "agent", "admin"]:
        assert role in src, f"词典缺角色 {role}"


# ============================================================
# 批 3-5 · 前端 grep gate(批 3-5 实施后启用)
# ============================================================

CUSTOMER_FORBIDDEN_WORDS = [
    # 工程词
    "SKU", "V3.5", "V3\\.5", "drift", "diff_",
    "ledger", "clawback", "paid_inventory", "bonus_inventory",
    "pending_payout", "bps", "FIFO",
    # pool 字段名
    "tool_credit", "publish_credit", "bonus_credit",
]

AGENT_FORBIDDEN_WORDS = [
    "ledger", "FIFO", "paid_inventory", "drift",
    "clawback", "bps", "diff_",
]


def _is_visible_jsx_text(src: str, word: str) -> list:
    """筛出**可见**文案命中 —— 薄委托到共用抽取器 `tests/v35_visible_copy`。

    🔴 [#91 · 2026-09-05] 原来这里是本文件私有的一套近似,和
       `test_v35_ui_audit.py` 里 r10 / r11 各自的一套,是**同一个谓词的三份实现**。
       三份各漏各的,实测四条判据全是假阳。同一个谓词写三处,必有两处没人验。

       本文件这份漏的是**返回类型注解**:`function toQuotedSKU(...): QuotedSKU {`
       —— 它挡了 `.X` / `X:` / `X(` / `<X` 四种位置,唯独没挡「词是更长标识符的
       一部分」。共用版补的就是这一条(只认 **ASCII** 词字符相邻,
       否则 `'您的SKU额度'` 这种真违规会被一起滤掉)。
    """
    return visible_hits(src, word)
def test_w5_4_customer_pages_no_forbidden_words():
    """客户面 8 文件不漏禁词(批 3 实施后)"""
    targets_root = ROOT / "frontend" / "src" / "pages" / "Customer"
    if not targets_root.exists():
        pytest.skip("Customer 目录不存在")
    # 批 3 标记:词典已落地 + 客户面已治理 → terminology import 应见
    sample = targets_root / "CreditWallet.tsx"
    if sample.exists() and "v35Terminology" not in sample.read_text(encoding="utf-8"):
        pytest.skip("批 3 客户面治理尚未落地 v35Terminology import")

    violations = {}
    for tsx in targets_root.rglob("*.tsx"):
        if tsx.name.endswith(".test.tsx"):
            continue
        src = tsx.read_text(encoding="utf-8")
        for word in CUSTOMER_FORBIDDEN_WORDS:
            hits = _is_visible_jsx_text(src, word.replace("\\.", "."))
            if hits:
                violations.setdefault(str(tsx.relative_to(ROOT)), []).extend(
                    [(word, *h) for h in hits[:3]]
                )
    assert not violations, "客户面禁词命中:\n" + "\n".join(
        f"  {f}: {v}" for f, v in violations.items()
    )


def test_w5_5_agent_pages_no_engineering_words():
    """代理面禁工程字段(批 3 实施后)"""
    targets_root = ROOT / "frontend" / "src" / "pages" / "Agent"
    if not targets_root.exists():
        pytest.skip("Agent 目录不存在")
    sample = targets_root / "SettlementCenter.tsx"
    if sample.exists() and "v35Terminology" not in sample.read_text(encoding="utf-8"):
        pytest.skip("批 3 代理面治理尚未落地 v35Terminology import")
    violations = {}
    for tsx in targets_root.rglob("*.tsx"):
        if tsx.name.endswith(".test.tsx"):
            continue
        src = tsx.read_text(encoding="utf-8")
        for word in AGENT_FORBIDDEN_WORDS:
            hits = _is_visible_jsx_text(src, word)
            if hits:
                violations.setdefault(str(tsx.relative_to(ROOT)), []).extend(
                    [(word, *h) for h in hits[:3]]
                )
    assert not violations, "代理面工程词命中:\n" + "\n".join(
        f"  {f}: {v}" for f, v in violations.items()
    )


def test_w5_7_inventory_audit_merged():
    """批 4 · InventoryAudit + InventoryAuditHistory 合并 3 tabs"""
    audit = ROOT / "frontend" / "src" / "pages" / "Admin" / "InventoryAudit.tsx"
    if not audit.exists():
        pytest.skip("InventoryAudit.tsx 不存在")
    src = audit.read_text(encoding="utf-8")
    # 批 4 实施后:含 Tabs + 3 tab 标识
    if "Tabs" not in src or "history" not in src.lower():
        pytest.skip("批 4 InventoryAudit 尚未合并")
    # 合并后 History 文件可能保留但不再走路由 / 或被删
    assert "Tabs" in src
    # 3 tabs: 当前对账 / 历史记录 / 异常明细
    assert "当前对账" in src or "立即核对" in src
    assert "历史记录" in src or "对账历史" in src


def test_w5_8_sidebar_rename():
    """[#91 · 2026-09-06 Review 重锚] **已退役** —— 继任者见 tests/test_v35_sidebar_ssot_lock.py。

    原锁问「八个字面串在不在 AppSidebar.tsx」,而正确做法是标签从 SSOT
    (v35Terminology.SIDEBAR_LABELS + sidebarLabel())取 —— **做对了字面量就不在
    那个文件**:基线(没做)2/8 红,接完 SSOT(做对了)1/8 **更红**。
    锁与正确做法**互斥**,这不是阈值松紧问题,是锚错了。
    另外原来 `found == 0 ⇒ pytest.skip` 让「完全没做」与「做对了」同落 skip,
    只罚「做一半」—— 三档压成一档,压掉的正是两个极端。

    退役写成**肯定式**(不 skip:skip 是静默出口,-q 汇总里不进 failed 计数):
    断言继任者文件在,且四道闸一条不少。
    """
    successor = ROOT / "tests" / "test_v35_sidebar_ssot_lock.py"
    assert successor.is_file(), (
        f"继任者不在:{successor} —— 侧栏标签「必须走 SSOT」从此无人守,"
        f"本条退役的前提消失。")
    code = successor.read_text(encoding="utf-8")
    for gate in ("test_ssot_has_exactly_eight_labels",
                 "test_ssot_labels_contain_no_forbidden_terms",
                 "test_every_ssot_key_is_actually_called_in_the_sidebar",
                 "test_sidebar_hardcodes_no_label_literal",
                 "test_missing_ssot_file_is_red_not_skip"):
        assert f"def {gate}(" in code, (
            f"继任者里少了闸 {gate} —— 退役 = 换人守,不是换个人少守几样。")

# ============================================================================
# [单账本收敛 2026-07-27] 本文件原有 8 条用例断言 billing.py 里【存在】V3.5 分流
# (_is_v35_customer / _v35_check_credit_only / check_balance_only 与 deduct_points
#  的 v35 分支 / 402 异常映射等)。Owner 已决定拆除双账本,那些分支不再存在,
# 断言前提消失 —— 已删除,不是"跳过"也不是改成宽松断言(那是假绿)。
#
# 反向的锁写在 tests/test_wallet_single_ledger_stage2.py:
# 断言这些分支【必须不存在】,把分支加回来就转红。
# ============================================================================
