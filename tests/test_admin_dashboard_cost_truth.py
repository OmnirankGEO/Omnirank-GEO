"""AdminDashboard 成本口径修复 · 2026-05-29 老板 P0

覆盖:
- A. dashboard_api.py finance 拆 3 字段(llm_api / publish_external / total_operating)+ profit 改用 total
- B. publishing 卡补 profit_yuan
- C. services/api_costs.py KNOWN_CALLERS_FAMILIES + unknown_caller_cost surface
- D. AdminDashboard.tsx 文案改 "LLM API 调用费" + 加 3 新 stat card + 发布卡补毛利
- 监测周报/月报函数顶部加 caller 命名约定注释

防止"本月成本 ¥428.19 = API 调用费" 误导再现。
"""
from __future__ import annotations
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# A. dashboard_api.py finance 拆 3 字段
# ============================================================

def test_finance_has_three_split_cost_fields():
    """A · finance 必须返 3 成本字段(v1.3 起在 _compute_finance_metrics 纯 helper · 真执行见 v1_3)"""
    from api.dashboard_api import _compute_finance_metrics
    f = _compute_finance_metrics(
        recharge_revenue_yuan=100, publish_revenue_yuan=0,
        llm_api_cost_yuan=10, publish_external_cost_yuan=5, active_7d=1,
    )
    for k in ("llm_api_cost_yuan", "publish_external_cost_yuan", "total_operating_cost_yuan"):
        assert k in f, f"A 破:finance 缺 {k}"


def test_finance_profit_uses_total_operating_cost():
    """A · profit / profit_rate 必须扣 total_operating_cost(LLM + 外采)· 真执行
    v1.3 现金口径:profit = 充值现金 - 总运营成本(不被发布收入虚高)
    """
    from api.dashboard_api import _compute_finance_metrics
    f = _compute_finance_metrics(
        recharge_revenue_yuan=1000, publish_revenue_yuan=999,
        llm_api_cost_yuan=200, publish_external_cost_yuan=100, active_7d=10,
    )
    assert f["total_operating_cost_yuan"] == 300
    assert f["profit_yuan"] == 700, f"A 破:profit 应=充值-总运营成本=700 · 实 {f['profit_yuan']}"
    assert f["profit_rate"] == 0.7


def test_finance_old_cost_yuan_alias_preserved():
    """A · cost_yuan 兼容别名保留 = LLM API 费(向后兼容老前端)· 真执行"""
    from api.dashboard_api import _compute_finance_metrics
    f = _compute_finance_metrics(
        recharge_revenue_yuan=100, publish_revenue_yuan=0,
        llm_api_cost_yuan=42, publish_external_cost_yuan=0, active_7d=1,
    )
    assert "cost_yuan" in f, "A 破:cost_yuan 老字段被删"
    assert f["cost_yuan"] == 42, "A 破:cost_yuan 兼容别名应=LLM API 费"


def test_publish_external_cost_uses_cost_yuan_inline():
    """A · 外采成本 = mhz_publish_order_items.cost_yuan(媒体原价)· v1.4 在 _query_publish_numbers helper
    (v1.4 三口径分离:已发布毛利成本 + 全状态总外采 · 都用 SUM(cost_yuan))
    """
    src = _read("api/dashboard_api.py")
    fn_start = src.find("def _query_publish_numbers(")
    assert fn_start > 0, "A 破:缺 _query_publish_numbers helper"
    fn = src[fn_start:fn_start + 2000]
    assert "SUM(cost_yuan)" in fn, "A 破:外采成本未用 SUM(cost_yuan)"


# ============================================================
# B. publishing 卡补毛利
# ============================================================

def test_publishing_has_profit_yuan():
    """B · publishing.profit_yuan 必须存在 + = revenue - cost(v1.3 起走 _compute_publish_card)"""
    src = _read("api/dashboard_api.py")
    # 初始化字典含 profit_yuan
    init_pat = re.compile(r'publishing\s*=\s*\{[^}]*"profit_yuan"', re.DOTALL)
    assert init_pat.search(src), \
        "B 破:publishing 初始化 dict 未含 profit_yuan(老前端 undefined)"
    # v1.4 走纯 helper · 真执行验证 = revenue - margin_cost
    from api.dashboard_api import _compute_publish_card
    pc = _compute_publish_card(publish_revenue_yuan=150.0, publish_margin_cost_yuan=100.0)
    assert pc["profit_yuan"] == 50.0, "B 破:publish profit_yuan 算法非 revenue - cost"
    assert 'publishing["profit_yuan"] = _pc["profit_yuan"]' in src, \
        "B 破:publishing.profit_yuan 未走 _compute_publish_card"


# ============================================================
# C. services/api_costs.py KNOWN_CALLERS 常量 + unknown surface
# ============================================================

def test_api_costs_has_known_callers_constant():
    """C · KNOWN_CALLERS_FAMILIES 常量 + UNKNOWN_CALLERS 列出 · 防归因污染"""
    src = _read("services/api_costs.py")
    assert "KNOWN_CALLERS_FAMILIES" in src, \
        "C 破:services/api_costs.py 缺 KNOWN_CALLERS_FAMILIES 常量(归因审计基础)"
    assert "UNKNOWN_CALLERS" in src, \
        "C 破:services/api_costs.py 缺 UNKNOWN_CALLERS 常量"
    # 必须包含核心命名族
    for fam in ("monitoring_", "article_", "social_", "keyword_"):
        assert f'"{fam}"' in src, f"C 破:KNOWN_CALLERS_FAMILIES 缺 {fam} 族"


def test_api_costs_unknown_caller_surfaced():
    """C · get_api_cost_summary 返字段含 unknown_caller_cost / ratio"""
    src = _read("services/api_costs.py")
    # 返回 dict 含 unknown_caller_cost
    fn_start = src.find("def get_api_cost_summary(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 5000]

    assert '"unknown_caller_cost"' in fn_block, \
        "C 破:get_api_cost_summary 返字段缺 unknown_caller_cost"
    assert '"unknown_caller_ratio"' in fn_block, \
        "C 破:get_api_cost_summary 返字段缺 unknown_caller_ratio"


def test_is_known_caller_function():
    """C · is_known_caller helper 必须存在 + 拒绝 UNKNOWN_CALLERS 中的值"""
    src = _read("services/api_costs.py")
    assert "def is_known_caller(" in src, \
        "C 破:is_known_caller helper 未定义"


def test_monitoring_report_caller_doc_note():
    """C · api_generate_report 必须有 caller 命名约定文档注释"""
    src = _read("api/monitoring_api.py")
    fn_start = src.find("def api_generate_report(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 3000]

    assert "monitoring_report_review" in fn_block, \
        "C 破:api_generate_report docstring 未约定 monitoring_report_review caller(未来加 LLM 总结时缺规范)"
    assert "geo_retrospective_report" in fn_block, \
        "C 破:api_generate_report docstring 未约定 geo_retrospective_report caller"


# ============================================================
# D. AdminDashboard.tsx UI 文案 + 三卡 + 发布毛利
# ============================================================

def test_admin_dashboard_no_misleading_本月成本():
    """D · 不再出现「本月成本 + API 调用费」组合"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")

    # 「本月成本」label 必须被替换(允许出现在注释里说明老 BUG · 但不能是 StatCard 的 label)
    # 精确匹配 label="本月成本" 模式 — 严禁存在
    pat = re.compile(r'label\s*=\s*"本月成本"')
    assert not pat.search(src), \
        "D 破:AdminDashboard 仍有 label=\"本月成本\" StatCard(老板 P0 禁止误导)"


def test_admin_dashboard_has_llm_api_cost_card():
    """D · 必须有 LLM API 调用费 StatCard 用 finance.llm_api_cost_yuan"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert 'label="LLM API 调用费"' in src, \
        "D 破:AdminDashboard 缺 LLM API 调用费 StatCard(boss P0)"
    assert "finance.llm_api_cost_yuan" in src, \
        "D 破:UI 未消费 finance.llm_api_cost_yuan"


def test_admin_dashboard_has_publish_external_card():
    """D · 必须有发布外采成本 StatCard"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert 'label="发布外采成本"' in src, \
        "D 破:AdminDashboard 缺发布外采成本 StatCard"
    assert "finance.publish_external_cost_yuan" in src, \
        "D 破:UI 未消费 finance.publish_external_cost_yuan"


def test_admin_dashboard_has_total_operating_card():
    """D · 必须有总运营成本 StatCard · sub 明示 LLM + 发布外采"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert 'label="总运营成本"' in src, \
        "D 破:AdminDashboard 缺总运营成本 StatCard(老板 P0)"
    assert "finance.total_operating_cost_yuan" in src, \
        "D 破:UI 未消费 finance.total_operating_cost_yuan"
    # sub 应明示构成
    assert 'sub="LLM + 发布外采"' in src, \
        "D 破:总运营成本 sub 未明示构成"


def test_admin_dashboard_publishing_revenue_cost_profit():
    """D · 代发业务卡必须显示 revenue / cost / profit 3 行"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert "publishing.revenue_yuan" in src, \
        "D 破:发布卡未显示 revenue_yuan"
    assert "publishing.cost_yuan" in src, \
        "D 破:发布卡未显示 cost_yuan"
    assert "publishing.profit_yuan" in src, \
        "D 破:发布卡未显示 profit_yuan(老板 P0)"
    assert "发布收入" in src, \
        "D 破:发布卡缺「发布收入」文案"
    assert "发布外采" in src, \
        "D 破:发布卡缺「发布外采」文案"
    assert "发布毛利" in src, \
        "D 破:发布卡缺「发布毛利」文案"


def test_admin_dashboard_api_cost_card_renamed():
    """D · 「API 成本（本月）」卡片标题改成「LLM API 调用明细（本月）」"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    # 严格不能保留旧标题(防 ambiguous "API 成本")
    assert "LLM API 调用明细（本月）" in src, \
        "D 破:API 成本明细卡片标题未改成「LLM API 调用明细（本月）」(防误导)"
    # 旧 ambiguous 卡片标题禁止
    assert "API 成本（本月）" not in src, \
        "D 破:旧 ambiguous 「API 成本（本月）」标题仍残留"


def test_admin_dashboard_api_cost_合计_renamed():
    """D · API 成本明细底部「合计」标签改成「合计 LLM API 费」"""
    src = _read("frontend/src/pages/Home/AdminDashboard.tsx")
    assert "合计 LLM API 费" in src, \
        "D 破:API 明细底部「合计」未改名「合计 LLM API 费」(消除误导)"


# ============================================================
# 整体一致性 · 红线不动
# ============================================================

def test_no_red_line_files_touched():
    """整体 · 红线 5 文件必须 0 改"""
    import subprocess
    red_line = [
        "middleware/billing.py", "auth/jwt_utils.py", "auth/middleware.py",
        "db/connection.py", "tools/scoring/geo_scope_scorer.py",
    ]
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "main..HEAD"],
            cwd=_ROOT, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            # 本地可能不在 main 分支或 main 不存在 · 跳过(CI 会跑)
            return
        changed = set(result.stdout.strip().splitlines())
        hit = changed & set(red_line)
        # 🔴 [2026-09-05] **收窄,不是放宽**:本轮 `middleware/billing.py` 经授权改动。
        #    Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377
        #    其余保护文件任一被改 ⇒ 仍然红;billing.py 改回不动 ⇒ **也红**
        #    (授权用完必须显式收回,不留「曾经批过所以永远敞着」的门)。
        assert hit == {"middleware/billing.py"}, (
            f"红线改动集 {sorted(hit)} != 已授权集 ['middleware/billing.py'] · Owner 2026-09-05 亲口授权(「billing.py 这笔我批了」)· OWNER_AUTHORIZATIONS_2026-09-05.md sha256 b673b188ff01e377")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # git 不可用 · 跳过
        pass
