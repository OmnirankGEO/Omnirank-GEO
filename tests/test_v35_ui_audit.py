"""
V3.5 UI 英文/工程词审计 gate(2026-05-27)

老板 r1 报告 70+ 处违规 + 4 类根因 · 本次"全修"批量收口 · grep gate 防回归。

覆盖:
- P0 客户面 9 处违规
- P1 代理面 35 处违规(老历史页 + 协议正文 + V3.5 5 新页 toast 反模式)
- P2 admin 18 处违规
- P3 全局 8 处 + V3.5 8 error code 翻译覆盖率
- 词典 SSOT 扩展(errorCodeLabel + payoutMethodLabel + triggerByLabel + taskStatusLabel + activeStatusLabel)
- formatApiErrorForDisplay 升级(优先读 detail.code)

文档:docs/AI-CONTEXT/V35_UI_ENGLISH_AUDIT_2026-05-27.md
"""
import re
import sys
from pathlib import Path

import pytest

from tests.v35_visible_copy import visible_source

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend" / "src"
RM_DIR = FE / "pages" / "Admin" / "ResearchMonitor"


DEV_MARKER = re.compile(r"TODO|FIXME|待实现|未实现|待补|待接|Day\s*\d+", re.IGNORECASE)


def is_dev_placeholder_comment(line: str) -> bool:
    """注释里的「占位」是不是**开发周期标记**(而不是算法术语「占位符」)。

    抽成独立谓词,是为了能拿合成样本单独验它 —— 判定逻辑长在 for 循环深处时,
    只能靠「整条判据是绿的」间接推断,而那推断不出「它到底在不在分辨」。
    """
    return "占位" in line and bool(DEV_MARKER.search(line))


def rm_tsx_files() -> list:
    """ResearchMonitor 下**全部** .tsx —— 机械枚举,不维护硬编码清单。

    🔴 [#91] 原来这里是三处硬编码文件名(`PromptsPanel.tsx` / `ReviewPanel.tsx` /
       `ArticleDetail.tsx` / `IndustriesPanel.tsx` / `LockButton.tsx` / `ArticleCard.tsx`)。
       这些 panel 后来被合并/下线,文件没了,而判据分成两种坏法:
         · 不判存在的(`p3` / `r6`)⇒ **FileNotFoundError**,连本该检查的其它文件也没读到,
           而在 `-q` 输出里跟断言失败**同形**;
         · 判存在就 `continue` / `skip` 的(`r7` / `r9` / `r11` / `r12`)⇒ **静默少检**。
       两种都让分母悄悄缩水。硬编码清单必然继续烂 —— 换成枚举。
    """
    assert RM_DIR.is_dir(), f"ResearchMonitor 目录不在:{RM_DIR} —— 目录改名要同笔改本函数"
    files = sorted(RM_DIR.rglob("*.tsx"))
    assert files, "ResearchMonitor 下一个 .tsx 都没有 —— 分母塌了,下面所有绿都不算数"
    return files


def named_page(rel: str):
    """按名点到的页面:**文件不在 = 红**,不是 skip。

    具名清单是有意为之(那 5 个 admin V3.5 页),但「点名的文件不见了」意味着
    这条判据已经不在检查它 —— 那必须响,不能静默略过。
    """
    fp = FE / "pages" / rel
    assert fp.is_file(), (
        f"判据点名的页面不存在:{rel}。文件被改名/删除时,判据要么跟着改名,"
        f"要么按退役处理(写明继任者)—— 静默跳过等于这条判据从此不检查任何东西。")
    return fp


# ============================================================
# 词典 SSOT · errorCodeLabel + 4 新 API + V3.5 8 code 全覆盖
# ============================================================

def test_terminology_has_errorCodeLabel():
    """v35Terminology.ts 必须导出 errorCodeLabel"""
    src = (FE / "lib" / "v35Terminology.ts").read_text(encoding="utf-8")
    assert "export function errorCodeLabel(" in src, "缺 errorCodeLabel API"


def test_terminology_covers_8_v35_error_codes():
    """ERROR_CODE_DICT 覆盖 V3.5 后端 8 个 error code + legacy 兼容 4 个"""
    src = (FE / "lib" / "v35Terminology.ts").read_text(encoding="utf-8")
    required = [
        "AGENT_NOT_SIGNED", "AGENT_REQUIRED", "AGENT_MISMATCH", "NEED_BIND_AGENT",
        "INSUFFICIENT_CREDIT", "PUBLISH_REQUIRES_PAID_CREDIT",
        "V35_FACTORY_DISABLED", "V35_FREEZE_NOT_SUPPORTED",
    ]
    missing = [code for code in required if code not in src]
    assert not missing, f"errorCodeLabel 缺 V3.5 code: {missing}"


def test_terminology_has_payoutMethodLabel():
    """payoutMethodLabel 翻译 wechat/alipay/bank · admin 视角"""
    src = (FE / "lib" / "v35Terminology.ts").read_text(encoding="utf-8")
    assert "export function payoutMethodLabel(" in src
    # 覆盖 3 主要打款渠道
    for key in ["wechat", "alipay", "bank"]:
        assert f"'{key}'" in src or f'"{key}"' in src or f"{key}:" in src, f"payoutMethodLabel 缺 {key}"


def test_terminology_has_triggerByLabel():
    """triggerByLabel 翻译 cron/manual/admin · admin 对账日报"""
    src = (FE / "lib" / "v35Terminology.ts").read_text(encoding="utf-8")
    assert "export function triggerByLabel(" in src
    for key in ["cron", "manual", "admin"]:
        assert key in src, f"triggerByLabel 缺 {key}"


def test_terminology_has_taskStatusLabel():
    """taskStatusLabel 全局 raw status 兜底 · 覆盖 pending/running/completed/failed"""
    src = (FE / "lib" / "v35Terminology.ts").read_text(encoding="utf-8")
    assert "export function taskStatusLabel(" in src
    for key in ["pending", "running", "completed", "failed", "cancelled"]:
        assert key in src, f"taskStatusLabel 缺 {key}"


def test_api_formatApiErrorForDisplay_reads_detail_code():
    """formatApiErrorForDisplay 必须优先读 detail.code 走 errorCodeLabel · 不再裸露后端 message"""
    src = (FE / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "errorCodeLabel" in src, "formatApiErrorForDisplay 未接入 errorCodeLabel"
    # 优先级:detail.code lookup 必须在 detail.message 之前
    body = re.search(r"export function formatApiErrorForDisplay.*?\n(?=export |function |\Z)",
                     src, re.DOTALL)
    assert body, "未找到 formatApiErrorForDisplay 函数体"
    btext = body.group(0)
    code_pos = btext.find("errorCodeLabel")
    msg_pos = btext.find("detail.message" if "detail.message" in btext else ".message")
    # detail.code 必须在 message 之前(优先级)
    assert code_pos > 0 and code_pos < (msg_pos if msg_pos > 0 else 999999), (
        "detail.code 必须优先于 detail.message 处理"
    )


# ============================================================
# 后端 message 脱敏
# ============================================================

def test_backend_V35_FACTORY_DISABLED_message_no_engineering_terms():
    """V35_FACTORY_DISABLED 后端 message 不再含 'V3.5' / 'SKU'"""
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    m = re.search(r'"code":\s*"V35_FACTORY_DISABLED",\s*"message":\s*"([^"]+)"', src)
    assert m, "未找到 V35_FACTORY_DISABLED message"
    msg = m.group(1)
    for forbidden in ["V3.5", "SKU"]:
        assert forbidden not in msg, f"V35_FACTORY_DISABLED message 仍含 '{forbidden}': {msg}"


# ============================================================
# P0 客户面违规 · 0 残留
# ============================================================

def test_p0_buycredit_no_agent_user_id_toast():
    """BuyCredit · agent_user_id 不再裸露给客户 toast"""
    src = (FE / "pages" / "Customer" / "BuyCredit.tsx").read_text(encoding="utf-8")
    # 不能含 "${r.agent_user_id}" 直接拼 toast
    assert "${r.agent_user_id}" not in src, "BuyCredit 仍在 toast 暴露 agent_user_id"
    # 不能含 "代理 #" / "服务方 · 编号 N"
    assert "服务方 · 编号" not in src and "服务方代理 #" not in src


def test_p0_buycredit_no_token_placeholder():
    """BuyCredit BindAgentInput placeholder 不含 'token'"""
    src = (FE / "pages" / "Customer" / "BuyCredit.tsx").read_text(encoding="utf-8")
    placeholders = re.findall(r'placeholder="([^"]+)"', src)
    for p in placeholders:
        assert "token" not in p.lower(), f"placeholder 含 token: {p}"


def test_p0_buycredit_order_id_not_in_dialog_title():
    """BuyCredit DialogTitle 不再直接拼 order_id"""
    src = (FE / "pages" / "Customer" / "BuyCredit.tsx").read_text(encoding="utf-8")
    # 不能含 `完成支付 · ${info.order_id}`
    assert "完成支付 · ${info.order_id}" not in src


def test_p0_buycredit_uses_formatApiErrorForDisplay():
    """BuyCredit catch block 走 formatApiErrorForDisplay · 不裸拼 e.message"""
    src = (FE / "pages" / "Customer" / "BuyCredit.tsx").read_text(encoding="utf-8")
    assert "formatApiErrorForDisplay" in src
    assert "from '@/lib/api'" in src
    # 不能再有 toast.error(`下单失败: ${e.message
    assert "下单失败: ${e.message" not in src
    assert "绑定失败: ${e.message" not in src


def test_p0_credit_wallet_uses_service_provider_fallback():
    """CreditWallet · fallback '已绑定代理' → '已绑定服务方'"""
    src = (FE / "pages" / "Customer" / "CreditWallet.tsx").read_text(encoding="utf-8")
    assert "已绑定代理" not in src or "已绑定服务方" in src


def test_p0_deduct_insufficient_dialog_no_jifen():
    """DeductDialog / InsufficientDialog '积分' → '额度'(客户铁律)"""
    for fn in ["DeductDialog.tsx", "InsufficientDialog.tsx"]:
        src = (FE / "components" / fn).read_text(encoding="utf-8")
        # 不再含"积分"(只允许在 import / 注释 / variable name)
        # 简化检查:JSX 文本里不应有 "积分扣除" "积分不足" "所需积分"
        for forbid in ["积分扣除", "积分不足", "所需积分", "消费积分", "当前积分余额"]:
            assert forbid not in src, f"{fn} 仍含 visible 文案 '{forbid}'"


def test_p0_header_chip_uses_e_du():
    """Header 顶部余额 chip 不再用"积分"裸字"""
    src = (FE / "components" / "layout" / "Header.tsx").read_text(encoding="utf-8")
    # 找 chip 内 <span>...积分</span> 模式
    chip_jifen = re.search(r'<span[^>]*>\s*积分\s*</span>', src)
    assert not chip_jifen, "Header chip 仍裸露 '积分' span"


# ============================================================
# P1 代理面违规 · 0 残留
# ============================================================

def test_p1_profitdashboard_no_paid_bonus_raw():
    """ProfitDashboard 不再有 'paid: X / bonus: X' 等 raw 字面"""
    src = (FE / "pages" / "Agent" / "ProfitDashboard.tsx").read_text(encoding="utf-8")
    # JSX 文案不应含 "paid:" / "bonus:" / "(paid)" / "(bonus)" visible 字面
    for forbid in ["paid: ${", "bonus: ${", "(paid)", "(bonus)", "(paid 部分)"]:
        assert forbid not in src, f"ProfitDashboard 仍有 visible '{forbid}'"


def test_p1_commissionpanel_no_paid_bonus_raw():
    """CommissionPanel paid/bonus 字面替换 · table head + 月度统计 + dialog 描述"""
    src = (FE / "pages" / "Referral" / "CommissionPanel.tsx").read_text(encoding="utf-8")
    for forbid in ["佣金 (paid)", "膨胀 (bonus)", "本金 (paid)", "膨胀 20% (bonus)",
                   "转为 paid 积分", "转为 bonus", ">paid<", ">bonus<"]:
        assert forbid not in src, f"CommissionPanel 仍有 visible '{forbid}'"
    # "用户#" → "用户编号"
    assert "用户#" not in src, "CommissionPanel 仍用 '用户#' 半中半英"


def test_p1_commissionpanelv32_default_status_translated():
    """CommissionPanelV32 default switch 不再裸露 {r.status}"""
    src = (FE / "pages" / "Referral" / "CommissionPanelV32.tsx").read_text(encoding="utf-8")
    # default branch 不应直接 {r.status}
    assert ">{r.status}</Badge>" not in src, "CommissionPanelV32 default 仍裸露 raw status"


def test_p1_agreement_page_section8_chinese():
    """AgreementPage §8 协议正文 enum 中文化"""
    src = (FE / "pages" / "Agent" / "AgreementPage.tsx").read_text(encoding="utf-8")
    # 不再含 4 税务身份英文 enum
    for forbid in ["individual_business / company / partnership",
                   "withholding · ",
                   "invoice_provided",
                   "exempt_manual",
                   "settled 已提走",
                   "0 损失 in A/B/C"]:
        assert forbid not in src, f"AgreementPage 协议正文仍含 '{forbid}'"


def test_p1_quotepreview_no_H5():
    """QuotePreview · H5 链接 → 网页链接"""
    src = (FE / "pages" / "Agent" / "QuotePreview.tsx").read_text(encoding="utf-8")
    assert "H5 链接" not in src


def test_p1_v35_5pages_use_formatApiErrorForDisplay():
    """V3.5 5 新页 toast.error 收口 · 走 formatApiErrorForDisplay · 不裸拼 e.message"""
    pages = [
        "Agent/InventoryCenter.tsx",
        "Agent/PricingCenter.tsx",
        "Agent/SettlementCenter.tsx",
        "Agent/PromotionCenter.tsx",
        "Agent/AgreementPage.tsx",
    ]
    for p in pages:
        src = (FE / "pages" / p).read_text(encoding="utf-8")
        assert "formatApiErrorForDisplay" in src, f"{p} 未引入 formatApiErrorForDisplay"
        # 不再裸拼 ${e.message || e}
        assert "${e.message || e}" not in src, f"{p} 仍裸拼 e.message"


# ============================================================
# P2 admin 违规 · 0 残留
# ============================================================

def test_p2_usermanagement_wallet_tab_chinese():
    """UserManagement 钱包 tab · Paid/Bonus/Total/ADMIN 全中文化"""
    src = (FE / "pages" / "Admin" / "UserManagement.tsx").read_text(encoding="utf-8")
    # JSX 文本里不应有 ">Paid<" / ">Bonus<" / ">Total<" / ">ADMIN<"
    for forbid in [">Paid<", ">Bonus<", ">Total<", ">ADMIN<"]:
        assert forbid not in src, f"UserManagement 仍有 visible '{forbid}'"
    # paid/bonus dropdown 不再 "bonus — 赠送积分" 半中半英
    assert "bonus — 赠送积分" not in src and "paid — 充值积分" not in src


def test_p2_settlements_payout_method_translated():
    """SettlementsReview · payout_method 走 payoutMethodLabel 翻译"""
    src = (FE / "pages" / "Admin" / "SettlementsReview.tsx").read_text(encoding="utf-8")
    assert "payoutMethodLabel" in src, "SettlementsReview 未引入 payoutMethodLabel"
    # 不再 raw {it.payout_method}
    assert "{it.payout_method}" not in src, "SettlementsReview 仍 raw 渲染 payout_method"
    # 表头 "ID" → "编号"
    assert ">ID</th>" not in src
    # "凭证 URL" → "凭证链接"
    assert "凭证 URL" not in src


def test_p2_inventoryaudit_triggered_by_translated():
    """InventoryAudit · triggered_by 走 triggerByLabel"""
    src = (FE / "pages" / "Admin" / "InventoryAudit.tsx").read_text(encoding="utf-8")
    assert "triggerByLabel" in src
    assert "{r.triggered_by}" not in src, "InventoryAudit 仍 raw 渲染 triggered_by"


def test_p2_binding_disputes_id_and_token_hidden():
    """BindingDisputes · 表头 ID → 编号 · source_token 不再 8 字符前缀"""
    src = (FE / "pages" / "Admin" / "BindingDisputes.tsx").read_text(encoding="utf-8")
    assert ">ID</th>" not in src
    # source_token slice 不应再露
    assert "source_token.slice(0, 8)" not in src
    # 用户#N → 客户编号 N
    assert "用户#${d.customer_user_id}" not in src


def test_p2_taxprofiles_bps_chinese():
    """TaxProfiles · '单位 0.01%' → '万分位'"""
    src = (FE / "pages" / "Admin" / "TaxProfiles.tsx").read_text(encoding="utf-8")
    assert "单位 0.01%" not in src
    assert "万分位" in src


# ============================================================
# P3 全局违规 · 0 残留
# ============================================================

def test_p3_research_monitor_active_chinese():
    """Admin/ResearchMonitor · 'active' 表头中文化"""
    for fp in rm_tsx_files():
        src = fp.read_text(encoding="utf-8")
        assert ">active</TableHead>" not in src, f"{fp.name} 仍 raw 'active' 表头"


def test_p3_workspace_status_translated():
    """Workspace · raw {result.status} 走 taskStatusLabel"""
    src = (FE / "pages" / "Workspace" / "Workspace.tsx").read_text(encoding="utf-8")
    assert "taskStatusLabel" in src
    assert ">{result.status}</Badge>" not in src


def test_p3_rollback_dialog_status_translated():
    """RollbackDialog · raw {task.status} 走 taskStatusLabel"""
    src = (FE / "pages" / "Monitoring" / "components" / "RollbackDialog.tsx").read_text(encoding="utf-8")
    assert "taskStatusLabel" in src
    assert ">{task.status}</Badge>" not in src


def test_p3_agent_trace_audit_status_translated():
    """AgentTrace / AgentAudit · raw {row.status} 走 taskStatusLabel"""
    for f in ["AgentTrace.tsx", "AgentAudit.tsx"]:
        src = (FE / "pages" / "Admin" / f).read_text(encoding="utf-8")
        assert "taskStatusLabel" in src, f"{f} 缺 taskStatusLabel import"
        # 不再 raw {row.status} 直渲染
        assert ">{row.status}</div>" not in src


# ============================================================
# 总闸 · py_compile + 防止反模式残留
# ============================================================

def test_v35_5pages_zero_raw_emessage_toast():
    """V3.5 5 新页 + BuyCredit + CreditWallet + admin V3.5 5 页 共 12 页 0 处 toast.error 模板拼 e.message
    r6 强化:加入 CreditWallet + admin 5 页(InventoryAudit / BindingDisputes / SettlementsReview / TaxProfiles / PricingMgmt)
    """
    pages = [
        # 客户面
        "Customer/BuyCredit.tsx",
        "Customer/CreditWallet.tsx",   # r6 新加
        # 代理 V3.5 5 新页
        "Agent/InventoryCenter.tsx",
        "Agent/PricingCenter.tsx",
        "Agent/SettlementCenter.tsx",
        "Agent/PromotionCenter.tsx",
        "Agent/AgreementPage.tsx",
        # admin V3.5 5 页(r6 新加)
        "Admin/InventoryAudit.tsx",
        "Admin/BindingDisputes.tsx",
        "Admin/SettlementsReview.tsx",
        "Admin/TaxProfiles.tsx",
        "Admin/PricingMgmt.tsx",
    ]
    for p in pages:
        src = (FE / "pages" / p).read_text(encoding="utf-8")
        # 模板字符串 toast.error 拼 e.message · 全删
        hits = re.findall(r"toast\.error\(`[^`]*\$\{e\.message[^`]*`\)", src)
        assert not hits, f"{p} 仍有 raw toast.error 模板字符串 e.message: {hits}"


def test_r6_customer_no_jifen_visible():
    """[r6 P0] 客户面 visible '积分' 0 残留(Customer/*;旧 C 端对话页随开源 E3 删)
    interface / type / comment / import / variable name 不算违规
    """
    targets = [
        FE / "pages" / "Customer" / "BuyCredit.tsx",
        FE / "pages" / "Customer" / "CreditWallet.tsx",
    ]
    for fp in targets:
        if not fp.exists():
            continue
        src = fp.read_text(encoding="utf-8")
        # 提取 JSX 字符串字面量(单 / 双 / 模板字符串)+ toast 文案 + placeholder
        # 简化:任意行含 "积分" 且不是 注释 / interface / type / import
        lines = src.split("\n")
        violations = []
        for i, line in enumerate(lines, 1):
            if "积分" not in line:
                continue
            stripped = line.strip()
            # allowlist · 命中跳过(注释 / docstring / import / 类型定义)
            if (stripped.startswith("//") or stripped.startswith("*") or
                    stripped.startswith("/*") or stripped.startswith("*/") or
                    stripped.startswith("import ") or
                    stripped.startswith("type ") or stripped.startswith("interface ")):
                continue
            # 类型字段定义跳过(e.g. paid_points: number,)
            if re.match(r"^\s*\w+_?(points|jifen)\s*[?:]?:\s*\w", stripped):
                continue
            violations.append((i, line.rstrip()))
        # 允许 1-2 处合法位置(如 PRICING_DATA 类型字段)· 严格 0 要看具体
        # 客户面铁律:visible JSX / toast / placeholder 不应含"积分"
        # 用更宽松规则:命中 >0 但允许在 lines containing only field/comment
        # 简化:直接 0
        assert not violations, (
            f"{fp.name} visible '积分' 残留:\n" +
            "\n".join(f"  L{i}: {line}" for i, line in violations[:5])
        )


def test_r6_admin_v35_pages_use_formatApiErrorForDisplay():
    """[r6 P2] admin V3.5 5 页 + ResearchMonitor 3 panel 必须 import formatApiErrorForDisplay"""
    missing = []
    for p in ("Admin/InventoryAudit.tsx", "Admin/BindingDisputes.tsx",
              "Admin/SettlementsReview.tsx", "Admin/TaxProfiles.tsx",
              "Admin/PricingMgmt.tsx"):
        if "formatApiErrorForDisplay" not in named_page(p).read_text(encoding="utf-8"):
            missing.append(p)
    # ResearchMonitor 侧:分母**派生**自「这个文件会不会弹 toast」,
    # 不是全目录一刀切 —— `components/RoundStatusBadge.tsx` 之类不弹 toast,
    # 要求它 import 是无理由的过严。
    for fp in rm_tsx_files():
        src = fp.read_text(encoding="utf-8")
        if "toast." not in src:
            continue
        if "formatApiErrorForDisplay" not in src:
            missing.append(fp.relative_to(FE / "pages").as_posix())
    assert not missing, (
        "这些会弹 toast 的页面没有 formatApiErrorForDisplay:\n  "
        + "\n  ".join(missing)
        + "\n  ⇒ 后端 detail / 堆栈会原样显示给使用者(v35「工程术语不外泄」)。")


def test_r7_research_monitor_zero_raw_err_message_toast():
    """[r7 老板 r6 盲区修] ResearchMonitor 目录 0 处 toast.error 模板拼任意变量名 .message
    覆盖 err.message / e.message / error.message / exc.message / 等
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    violations = []
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        hits = re.findall(
            r"toast\.error\(`[^`]*\$\{[a-zA-Z_]\w*\.message[^`]*`\)",
            src,
        )
        if hits:
            violations.append((tsx.name, hits))
    assert not violations, (
        f"ResearchMonitor 仍有 raw toast.error 模板拼 .message:\n" +
        "\n".join(f"  {n}: {h}" for n, h in violations)
    )


def test_r7_admin_v35_zero_raw_message_toast():
    """admin V3.5 + ResearchMonitor 全部 9 文件 0 raw toast.error .message 模板"""
    targets = [named_page(p) for p in (
        "Admin/InventoryAudit.tsx", "Admin/BindingDisputes.tsx",
        "Admin/SettlementsReview.tsx", "Admin/TaxProfiles.tsx",
        "Admin/PricingMgmt.tsx")] + rm_tsx_files()
    violations = {}
    for fp in targets:
        p = fp.relative_to(FE / "pages").as_posix()
        src = fp.read_text(encoding="utf-8")
        hits = re.findall(
            r"toast\.error\(`[^`]*\$\{[a-zA-Z_]\w*\.message[^`]*`\)",
            src,
        )
        if hits:
            violations[p] = hits
    assert not violations, (
        "admin V3.5 + ResearchMonitor 仍有 raw toast.error .message:\n" +
        "\n".join(f"  {p}: {h}" for p, h in violations.items())
    )


def test_r8_research_monitor_no_type_cast_message_in_toast():
    """[r8 老板 r7 盲区修] ResearchMonitor 0 处 toast.error 含 `(X as Error).message` 类型断言
    覆盖 (e as Error).message / (err as any).message 等
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    violations = []
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        # toast.error 调用范围内含 (X as Y).message 类型断言模式
        # 简化:全行扫 `toast.error(...(... as ...).message ...)`
        # 多行扫:整体 source 内 toast.error 调用块内含 type-cast .message
        # 用单行近似:任一 toast.error(...) 单行含 `as Error).message` / `as any).message`
        for line_num, line in enumerate(src.split("\n"), 1):
            if "toast.error" not in line:
                continue
            if re.search(r"\(\s*\w+\s+as\s+\w+\s*\)\.message", line):
                violations.append((tsx.name, line_num, line.strip()[:120]))
    assert not violations, (
        "ResearchMonitor 仍有 (X as Error).message 进 toast.error:\n" +
        "\n".join(f"  {n}:{l}: {s}" for n, l, s in violations)
    )


def test_r8_research_monitor_no_msg_variable_in_toast():
    """[r8 老板 r7 盲区修] ResearchMonitor 0 处 `const msg = ?.message; toast.error(...msg...)` 中间变量泄露
    覆盖:
      const msg = e instanceof Error ? e.message : '加载失败';
      toast.error(`xxx: ${msg}`);  或 toast.error(msg);
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    violations = []
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        # 找所有 `const msg = ?.message` / `let msg = ?.message` 等 · 然后看后续是否 toast.error 用 msg
        # 简化:任一行含 `const msg = ` + `.message` · 后续 5 行内含 `toast.error(...msg...)`
        lines = src.split("\n")
        for i, line in enumerate(lines):
            if not re.search(r"(const|let)\s+msg\s*=.*\.message", line):
                continue
            # 看接下来 5 行
            scope = "\n".join(lines[i:i + 8])
            if re.search(r"toast\.error\([^)]*\bmsg\b", scope):
                violations.append((tsx.name, i + 1, line.strip()[:120]))
    assert not violations, (
        "ResearchMonitor 仍有 const msg = .message → toast.error(...msg...):\n" +
        "\n".join(f"  {n}:{l}: {s}" for n, l, s in violations)
    )


def test_r8_research_monitor_no_description_message_in_toast():
    """[r8 老板 r7 盲区修] ResearchMonitor 0 处 toast.error 的 description option 含 `.message` 后端原文泄露
    覆盖:
      toast.error('xxx', { description: detail?.message || err.message })
    后端 res.message 类成功消息(toast.success 走 description: res.message)允许 · 仅查 toast.error
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    violations = []
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        # 找 toast.error 多行调用块(到 `;` 结束)· 含 description: ...?.message
        # 简化:扫每个 toast.error(...) 后续 10 行内含 `description:` + `.message`
        # 排除走 formatApiErrorForDisplay 的(它已 wrapped)
        lines = src.split("\n")
        for i, line in enumerate(lines):
            if not re.search(r"\btoast\.error\(", line):
                continue
            block = "\n".join(lines[i:i + 6])
            # 同 block 内 description: 且含 .message · 且不含 formatApiErrorForDisplay
            if re.search(r"description:\s*[^,}]*\.message", block):
                # 允许 formatApiErrorForDisplay wrap
                if "formatApiErrorForDisplay" not in block:
                    violations.append((tsx.name, i + 1, lines[i].strip()[:120]))
    assert not violations, (
        "ResearchMonitor 仍有 toast.error description 内 .message 后端原文:\n" +
        "\n".join(f"  {n}:{l}: {s}" for n, l, s in violations)
    )


def test_r9_research_monitor_no_message_in_toast_options():
    """[r9 老板 r8 盲区修] ResearchMonitor 0 处 toast.{success,error}(..., { description: ...message })
    覆盖 res.message / detail?.message / err.message 出现在 toast 选项块内
    r8 漏:r8 gate 排除 res.message · 但 res.message 一样会冒后端原文/英文/工程词
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    violations = []
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        lines = src.split("\n")
        for i, line in enumerate(lines):
            if not re.search(r"\btoast\.(success|error|info|warning)\(", line):
                continue
            # 后续 6 行块内找 `description: ...message`
            block = "\n".join(lines[i:i + 8])
            # 排除 formatApiErrorForDisplay wrap
            if re.search(r"description\s*:\s*[^,}]*\.message", block):
                # block 含 formatApiErrorForDisplay 表示已 wrap · 仍允许
                if "formatApiErrorForDisplay" not in block:
                    violations.append((tsx.name, i + 1, lines[i].strip()[:120]))
    assert not violations, (
        "ResearchMonitor toast 选项块 description 含 raw .message:\n" +
        "\n".join(f"  {n}:L{l}: {s}" for n, l, s in violations)
    )


def test_r9_research_monitor_no_intermediate_message_var_in_toast():
    """[r9 老板 r8 盲区修] ResearchMonitor 0 处 const/let detail|msg|friendly = ?.message → toast.{success,error}(...该变量...)
    覆盖中间变量绕过 r8 gate(r8 只查 const msg = · 没查 const detail = / const friendly = 等任意名)
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    violations = []
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        lines = src.split("\n")
        for i, line in enumerate(lines):
            # 任意 const/let 变量名 = ?.message
            m = re.search(r"(const|let)\s+(\w+)\s*=.*\.message", line)
            if not m:
                continue
            var_name = m.group(2)
            # 排除走 formatApiErrorForDisplay 的赋值(如 const friendly = formatApiErrorForDisplay(...))
            if "formatApiErrorForDisplay" in line:
                continue
            # 后续 10 行内 toast.{success,error}(...var_name...)
            scope = "\n".join(lines[i:i + 12])
            if re.search(rf"toast\.(success|error|info|warning)\([^)]*\b{re.escape(var_name)}\b", scope):
                violations.append((tsx.name, i + 1, line.strip()[:120]))
    assert not violations, (
        "ResearchMonitor 中间变量 const X = ?.message → toast(...X...):\n" +
        "\n".join(f"  {n}:L{l}: {s}" for n, l, s in violations)
    )


def test_r9_lockbutton_no_message_chain():
    """[r9 老板 r8 直点] LockButton.tsx 不允许 err.message → detail → toast 链路
    必须统一走 formatApiErrorForDisplay
    """
    # [#91 · 2026-09-05] **已退役**:LockButton.tsx 全树已无(整块下线)。
    # 原来是 `if not fp.exists(): pytest.skip(...)` —— 静默出口:判据从此不检查
    # 任何东西,而 -q 汇总里 skip 不进 failed 计数,没人会发现。
    # 退役 = **换人守**,不是不守了。继任者是同文件的目录级闸
    # test_r8_research_monitor_toast_no_message_near_call(扫整个目录的
    # toast.error 块内 raw .message,覆盖面严格大于原来那一个文件)。
    # 写成**肯定式**:断言继任者还活着 —— 缺席式「LockButton 不见了就算过」
    # 会被『正确下线』与『继任者也被删』同时满足,而两者含义相反。
    _successor = "test_r8_research_monitor_toast_no_message_near_call"
    assert callable(globals().get(_successor)), (
        f"继任者 {_successor} 不在了 —— LockButton 退役的前提是「有人接着守」。")
    fp = FE / "pages" / "Admin" / "ResearchMonitor" / "LockButton.tsx"
    assert not fp.exists(), "LockButton.tsx 又回来了 —— 退役前提消失,把这条改回真检查。"
    return
    src = fp.read_text(encoding="utf-8")
    # 不允许 err.message 出现(整个文件 · 因为 LockButton 全部 toast 都该走 formatApi)
    assert "err.message" not in src, "LockButton 仍有 err.message · 必须走 formatApiErrorForDisplay"
    # 不允许 `typeof err.detail === 'string' ? err.detail : err.message` 链路
    assert "? err.detail : err.message" not in src
    assert "typeof err.detail === 'string'" not in src or "formatApiErrorForDisplay" in src


def test_r8_research_monitor_toast_no_message_near_call():
    """[r8 总闸] ResearchMonitor 任一 toast.error 调用块(后续 6 行)0 出现 `.message`
    彻底覆盖上面 4 种盲区 · 含 (X as Y).message / msg 变量 / description / 直接传入
    允许 formatApiErrorForDisplay 内部用 .message(已包装)
    允许 res.message / response.message 等后端成功返回(toast.success 用 · 不算违规)
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    violations = []
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        lines = src.split("\n")
        for i, line in enumerate(lines):
            if not re.search(r"\btoast\.error\(", line):
                continue
            block = "\n".join(lines[i:i + 8])
            # 在 toast.error 块内找 .message 出现 · 且不走 formatApiErrorForDisplay 包装
            # 同一 toast.error 调用块内 · 不允许 .message 出现 unless formatApi 包了它
            # 简化判定:block 内若 .message 出现 · 必须同 block 也有 formatApiErrorForDisplay
            has_message = re.search(r"\.message\b", block)
            has_format = "formatApiErrorForDisplay" in block
            if has_message and not has_format:
                # 进一步排除 res.message(success path · 但我们扫的就是 toast.error)
                # 仅 toast.error 块内 .message 都禁
                violations.append((tsx.name, i + 1, lines[i].strip()[:120]))
    assert not violations, (
        "ResearchMonitor toast.error 块内仍有 raw .message(未走 formatApiErrorForDisplay):\n" +
        "\n".join(f"  {n}:L{l}: {s}" for n, l, s in violations)
    )


def test_followup_pyfiles_compile():
    """py_compile 验证后端无 syntax error"""
    import py_compile
    files = [
        ROOT / "api" / "wallet_api.py",
        ROOT / "middleware" / "billing.py",
        ROOT / "services" / "customer_credit.py",
    ]
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            pytest.fail(f"{f.name} 编译失败: {e}")


# ============================================================
# r10 · 老板 r9 复核盲区(2 类)
#   1. 客户面 scope 漏 components/c_end · 仍有"积分/返利/¥/平台运营成本"
#   2. ResearchMonitor 不止 .message · TableHead/Label/placeholder/toast
#      仍露 round_id / stage / prompt_text / confirm_code / slug / name /
#      value(JSON) / active / sort_order / prompts / from / item
# ============================================================

def test_r10_c_end_components_no_jifen_visible():
    """[r10 老板 r9 盲区修] components/c_end 任意 .tsx 不再 visible '积分'
    interface / type / comment / import / variable name allowlist
    """
    c_end_dir = FE / "components" / "c_end"
    if not c_end_dir.exists():
        pytest.skip("components/c_end 目录不存在")
    violations = []
    for tsx in c_end_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        for i, line in enumerate(src.split("\n"), 1):
            if "积分" not in line:
                continue
            stripped = line.strip()
            # 注释/import/type/interface allowlist
            if (stripped.startswith("//") or stripped.startswith("*") or
                    stripped.startswith("/*") or stripped.startswith("*/") or
                    stripped.startswith("import ") or
                    stripped.startswith("type ") or stripped.startswith("interface ")):
                continue
            # 类型字段定义 allowlist
            if re.match(r"^\s*\w+_?(points|jifen)\s*[?:]?:\s*\w", stripped):
                continue
            violations.append((tsx.name, i, line.rstrip()[:120]))
    assert not violations, (
        "components/c_end visible '积分' 残留(应改为'额度'):\n" +
        "\n".join(f"  {n}:L{i}: {s}" for n, i, s in violations)
    )


def test_r10_c_end_components_no_cost_breakdown():
    """[r10 老板 r9 盲区修] components/c_end 不再 visible "约 ¥X" / "平台运营成本" / "带看奖励"
    成本拆解客户面隐藏(boss r10:"约 ¥2 / 平台运营成本 客户面隐藏或改成'本次占用额度'")
    """
    c_end_dir = FE / "components" / "c_end"
    if not c_end_dir.exists():
        pytest.skip("components/c_end 目录不存在")
    violations = []
    forbidden = ["平台运营成本", "约 ¥", "约¥", "带看奖励", "分销返点"]
    for tsx in c_end_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        for i, line in enumerate(src.split("\n"), 1):
            stripped = line.strip()
            # 注释 allowlist
            if (stripped.startswith("//") or stripped.startswith("*") or
                    stripped.startswith("/*") or stripped.startswith("*/")):
                continue
            for term in forbidden:
                if term in line:
                    violations.append((tsx.name, i, term, line.rstrip()[:120]))
                    break
    assert not violations, (
        "components/c_end 仍有成本拆解客户面泄露:\n" +
        "\n".join(f"  {n}:L{i} [{t}]: {s}" for n, i, t, s in violations)
    )


# 工程英文词白名单(JSX 文案中禁出现)
RM_FORBIDDEN_ENG_TERMS = [
    "round_id", "prompt_text", "sort_order", "confirm_code",
    "value(JSON)", "value (JSON)",
    "snapshot_json", "progress_json", "summary_json",
]

def test_r10_research_monitor_visible_jsx_no_eng_terms():
    """[r10 老板 r9 盲区修] ResearchMonitor 可见 JSX(TableHead/Label/placeholder/toast/DialogTitle/DialogDescription)
    0 处暴露 round_id / prompt_text / sort_order / confirm_code / value(JSON) / progress_json 等工程英文词
    注释/类型/import/API 字段访问 allowlist
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    violations = []
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        for i, line in enumerate(src.split("\n"), 1):
            stripped = line.strip()
            # allowlist · 注释/import/type/interface/API 字段访问(.foo_bar)
            if (stripped.startswith("//") or stripped.startswith("*") or
                    stripped.startswith("/*") or stripped.startswith("*/") or
                    stripped.startswith("import ") or
                    stripped.startswith("type ") or stripped.startswith("interface ") or
                    stripped.startswith("const PAGE_SIZE") or
                    stripped.startswith("export ") or
                    re.match(r"^\s*\w+\s*[?:]\s*\w+", stripped)):
                continue
            # API 字段访问 allowlist:`r.round_id` / `setRound_id` 内部变量名
            # 我们只查 JSX-like 上下文(>X< / label="X" / placeholder="X" / 中文字符串内拼)
            # 简化:命中工程词 + 在 JSX 文本位置(<...>X</...> 或 label="X" 等)
            for term in RM_FORBIDDEN_ENG_TERMS:
                # JSX 直接渲染:>round_id<
                if f">{term}<" in line:
                    violations.append((tsx.name, i, term, "JSX text"))
                # label/placeholder/title attribute
                m = re.search(rf'(label|placeholder|title)=["\']{re.escape(term)}["\']', line)
                if m:
                    violations.append((tsx.name, i, term, f"{m.group(1)} attr"))
                # toast 文案首参或文案模板含工程词(简化:含工程词 + toast.success/error/info/warning)
                # 这一类已被 r6-r9 间接覆盖 · 这里只做 JSX 表层
    assert not violations, (
        "ResearchMonitor 可见 JSX 仍露工程英文词:\n" +
        "\n".join(f"  {n}:L{i} [{t}] · {ctx}" for n, i, t, ctx in violations)
    )


def test_r10_research_monitor_visible_no_active_jsx():
    """[r10 老板 r9 盲区修] ResearchMonitor JSX 文案不再 visible 'active' / 'slug' / 'value' / 'key' / 'description' / 'item' / 'prompt' / 'prompts'
    仅检 JSX 直接渲染(>word<)和 label/placeholder attribute · API 字段访问不查
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    forbidden_words = ["active", "slug", "value", "key", "description", "item", "prompt", "prompts"]
    violations = []
    for tsx in rm_tsx_files():
        # [#91] 走共用抽取器:注释({/* */} 含在内)与 ${} 插值内容被挖空,
        # 但保行保列 —— 下面的行号仍对得上原文。原来只用 stripped.startswith("//")
        # 判注释,漏掉 {/* JSX 注释 */},实测三处命中全在那里面。
        src = visible_source(tsx.read_text(encoding="utf-8"))
        for i, line in enumerate(src.split("\n"), 1):
            stripped = line.strip()
            # allowlist 注释
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*") or stripped.startswith("*/"):
                continue
            for word in forbidden_words:
                # >word< 直接 JSX 渲染
                if re.search(rf">\s*{word}\s*<", line):
                    violations.append((tsx.name, i, word, "JSX text"))
                # label="word"
                if re.search(rf'label=["\']{word}["\']', line):
                    violations.append((tsx.name, i, word, "label attr"))
                # placeholder="word"
                if re.search(rf'placeholder=["\']{word}["\']', line):
                    violations.append((tsx.name, i, word, "placeholder attr"))
                # "中文 active 中文" 中文夹英(检"全 active 行业"类)
                # 用 \u 转义码点避免 Windows gbk 编码问题
                pat = rf"[一-鿿]\s+{word}\s+[一-鿿]"
                if re.search(pat, line):
                    violations.append((tsx.name, i, word, "中文夹英"))
    assert not violations, (
        "ResearchMonitor JSX 仍 visible 工程英文单词:\n" +
        "\n".join(f"  {n}:L{i} [{t}] · {ctx}" for n, i, t, ctx in violations[:30])
    )


def test_r11_research_monitor_visible_no_runner_llm_http():
    """[r11 老板 r10 盲区修] ResearchMonitor 可见文案(confirm / DialogDescription / toast / TableCell / KV value)
    0 处出现 LLM / HTTP / runner / completed / current_stage / JSON
    注释/import/类型/API 字段访问 allowlist · 但可见字符串(JSX text / confirm()/toast 文案/option label)不 allowlist
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    # 这些工程英文词作为 visible 文案时违规
    forbidden = ["LLM", "HTTP", "runner", "current_stage"]
    # 允许出现在注释/import 行(allowlist)
    violations = []
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        for i, line in enumerate(src.split("\n"), 1):
            stripped = line.strip()
            # allowlist 注释/import/type/interface
            if (stripped.startswith("//") or stripped.startswith("*") or
                    stripped.startswith("/*") or stripped.startswith("*/") or
                    stripped.startswith("import ") or
                    stripped.startswith("type ") or stripped.startswith("interface ") or
                    stripped.startswith("export interface ") or stripped.startswith("export type ")):
                continue
            for term in forbidden:
                # 命中 + 不是 API 字段访问(`r.current_stage` / `detailData.current_stage`)
                # · 也不是参数定义(`current_stage:` / `current_stage?:`)
                if term not in line:
                    continue
                # API 字段访问 allowlist:`.current_stage` 形式(后面接 [/}/)/.)
                if term == "current_stage":
                    # 排除 API 字段访问:`.current_stage` 或 ` current_stage:` (TS 类型)
                    # 我们查:出现在 backtick / double-quote / single-quote 中文字符串内
                    if re.search(r'["\'`][^"\'`]*current_stage[^"\'`]*["\'`]', line):
                        # 进一步排除 setActiveRoundId(round_id: string) 类
                        if not re.search(r":\s*string", line):
                            violations.append((tsx.name, i, term, line.strip()[:120]))
                else:
                    # LLM / HTTP / runner / completed:命中即违规(除非已 allowlist)
                    # 排除变量名/常量名(如 `const HTTP_STATUS = ...`)
                    if re.search(rf"\b{term}\b", line):
                        # 排除"如果在 string/comment 之外的合法位置"
                        # 简化:只查 JSX 文本/字符串/模板字符串/confirm()/toast() 文案
                        # 检测是否在引号或反引号字符串内
                        if re.search(rf'["\'`][^"\'`]*\b{term}\b[^"\'`]*["\'`]', line):
                            # 排除 ts-eslint allowlist style:`// @ts-ignore HTTP ...`
                            if not stripped.startswith("//"):
                                violations.append((tsx.name, i, term, line.strip()[:120]))
    assert not violations, (
        "ResearchMonitor 可见文案仍露 LLM/HTTP/runner/current_stage:\n" +
        "\n".join(f"  {n}:L{i} [{t}] · {s}" for n, i, t, s in violations[:30])
    )


def test_r11_research_monitor_no_completed_in_visible_string():
    """[r11 老板 r10 盲区修] ResearchMonitor confirm/toast/DialogDescription 0 处含 visible "completed" / "cancelled" / "running" / "failed" 等英文 enum 字面
    后端 status enum 必须通过 taskStatusLabel / stageLabel 翻译 · 不直接拼到中文文案
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    forbidden = ["completed", "cancelled", "canceled", "running", "failed"]
    violations = []
    for tsx in rm_tsx_files():
        # [#91] `跳过 ${skipped} 失败 ${failed}` 的可见文本只有中文;不挖空 ${}
        # 的话 failed 会被判成「文案里露了 status enum」(实测 3 处假阳)。
        raw = tsx.read_text(encoding="utf-8")
        raw_lines = raw.split("\n")
        src = visible_source(raw)
        for i, line in enumerate(src.split("\n"), 1):
            stripped = line.strip()
            if (stripped.startswith("//") or stripped.startswith("*") or
                    stripped.startswith("/*") or stripped.startswith("*/") or
                    stripped.startswith("import ") or
                    stripped.startswith("type ") or stripped.startswith("interface ")):
                continue
            # STATUS_OPTIONS array 是必要的枚举映射 · allowlist
            if re.search(r"value:\s*['\"](completed|cancelled|canceled|running|failed)['\"]", line):
                continue
            # ts type/enum
            if re.search(r"\bstatus:\s*['\"]?\s*(completed|cancelled|canceled|running|failed)", line):
                continue
            # state 比较 / 集合定义 allowlist:`CANCELLABLE = new Set(['running', 'pending'])`
            if "new Set(" in line:
                continue
            # 排除 .status === 'completed' / status === ... 类比较
            if re.search(r"===\s*['\"](completed|cancelled|canceled|running|failed)", line):
                continue
            # 排除 status filter 选项 array { value: 'completed', label: '已完成' }
            for term in forbidden:
                # 中文夹英 e.g. "覆盖为 completed 。"
                # 必须独立单词形式出现(两侧空格/标点/中文 · 而非 ${res.failed} 类字段访问)
                pat = rf"[一-鿿][^'\"`{{}}]*?(^|[\s,。：·;:])({term})([\s,。：·;:]|$)[^'\"`{{}}]*?[一-鿿]"
                if re.search(pat, line):
                    violations.append((tsx.name, i, term, raw_lines[i - 1].strip()[:120]))
                    break
                # confirm/toast/DialogDescription 文案内独立单词
                # 排除 ${X.term} / ${X.term ?? 0} 类字段访问(`.term` 前置 dot)
                # 用正则:term 前面不是 `.` 也不是 `${X.`
                m = re.search(rf"[`'\"][^`'\"]*?(?<![.\w]){term}(?![\w]).*?[`'\"]", line)
                if m:
                    seg = m.group(0)
                    if re.search(r"[一-鿿]", seg):
                        violations.append((tsx.name, i, term, raw_lines[i - 1].strip()[:120]))
                        break
    assert not violations, (
        "ResearchMonitor 文案仍含 status 英文 enum:\n" +
        "\n".join(f"  {n}:L{i} [{t}] · {s}" for n, i, t, s in violations[:30])
    )


def test_r11_research_monitor_current_stage_uses_stageLabel():
    """[r11 老板 r10 直点] RoundsPanel.tsx 不再直接 {r.current_stage} / {detailData.current_stage} 裸渲染
    必须走 stageLabel() 包装
    """
    fp = FE / "pages" / "Admin" / "ResearchMonitor" / "RoundsPanel.tsx"
    if not fp.exists():
        pytest.skip("RoundsPanel.tsx 不存在")
    src = fp.read_text(encoding="utf-8")
    # 必须 import stageLabel
    assert "stageLabel" in src, "RoundsPanel 未引入 stageLabel"
    # 不允许 raw {r.current_stage || '-'} / {detailData.current_stage || '-'}
    # JSX 直接 {X.current_stage ...} 没走 stageLabel 包装
    lines = src.split("\n")
    violations = []
    for i, line in enumerate(lines, 1):
        # 找 {X.current_stage 模式
        if re.search(r"\{[^}]*\bcurrent_stage\b", line):
            # 必须 stageLabel( 包裹
            if "stageLabel(" not in line:
                # 排除非 JSX 上下文(`useState<...current_stage...>` / type 定义)
                if re.search(r"<[^>]*>", line) and ("useState" in line or "interface" in line.lower()):
                    continue
                violations.append((i, line.strip()[:120]))
    assert not violations, (
        "RoundsPanel current_stage 仍裸渲染(未走 stageLabel):\n" +
        "\n".join(f"  L{i}: {s}" for i, s in violations)
    )


def test_r11_v35Terminology_has_stageLabel():
    """[r11] v35Terminology.ts 必须导出 stageLabel · 覆盖 8 核心 stage"""
    src = (FE / "lib" / "v35Terminology.ts").read_text(encoding="utf-8")
    assert "export function stageLabel(" in src
    # 覆盖核心 stage
    for key in ["crawl", "clean", "score", "review", "publish", "completed", "failed", "cancelled"]:
        assert key in src, f"stageLabel 缺 {key}"


def test_r11_articledetail_uses_model_not_llm():
    """[r11 老板 r10 直点] ArticleDetail.tsx 不再 visible 'LLM 评分理由' · 改'模型评分理由'
    """
    # [#91 · 2026-09-05] **已退役**:ArticleDetail.tsx 全树已无,「LLM 评分」功能
    # 整块下线 —— index.tsx 顶部注释白纸黑字:「Phase 9 (2026-05-25) 重设计完整版
    # (P07): 删审核 tab (LLM 评分 + top 30 推审核 整套下线)」。
    # 🔴 它的正向断言 `"模型评分" in src` **无法**改成目录级:全目录实测 0 处
    #    「模型评分」—— 功能没了,不是改名了。硬改目录级会要求一个已下线的功能
    #    必须存在,那是把判据钉在幻觉上。
    # 继任者:test_r11_research_monitor_visible_no_runner_llm_http(目录级,
    # 禁 visible 文案出现 LLM / HTTP / runner)。
    _successor = "test_r11_research_monitor_visible_no_runner_llm_http"
    assert callable(globals().get(_successor)), (
        f"继任者 {_successor} 不在了 —— 「LLM 不许露给用户」从此无人守。")
    fp = FE / "pages" / "Admin" / "ResearchMonitor" / "ArticleDetail.tsx"
    assert not fp.exists(), "ArticleDetail.tsx 又回来了 —— 退役前提消失,把这条改回真检查。"
    return
    src = fp.read_text(encoding="utf-8")
    # JSX 文本不应再有 ">LLM 评分理由<" / "LLM 评分"
    assert "LLM 评分" not in src, "ArticleDetail 仍含 'LLM 评分'(应改'模型评分')"
    assert "模型评分" in src, "ArticleDetail 缺中文化"


def test_r12_reject_reasons_no_llm():
    """[r12 自检 r11 盲区] REJECT_REASONS 不含 'LLM' 工程英文 · 老板 r11 LLM→模型 同模式扩展
    `'LLM 清洗失败'` → `'清洗失败'`
    """
    src = (FE / "lib" / "researchMonitorApi.ts").read_text(encoding="utf-8")
    # REJECT_REASONS 块内不应含 'LLM'
    m = re.search(r"export const REJECT_REASONS\s*=\s*\{([^}]+)\}", src, re.DOTALL)
    assert m, "未找到 REJECT_REASONS"
    body = m.group(1)
    assert "LLM" not in body, f"REJECT_REASONS 仍含 LLM:\n{body}"


def test_r12_domain_tier_uses_label_function():
    """[r12 自检 r11 盲区] ArticleCard / ArticleDetail 不再 raw {article.domain_tier}
    必须走 domainTierLabel() 包装
    """
    for fname in ["components/ArticleCard.tsx", "ArticleDetail.tsx"]:
        fp = FE / "pages" / "Admin" / "ResearchMonitor" / fname
        if not fp.exists():
            continue
        src = fp.read_text(encoding="utf-8")
        # 不允许 {article.domain_tier} 或 {<X>.domain_tier} 裸渲染
        raw_renders = re.findall(r"\{[^}]*\.domain_tier[^}]*\}", src)
        for snippet in raw_renders:
            assert "domainTierLabel" in snippet or "domainTierLabel" in src, (
                f"{fname} 仍 raw 渲染 {snippet} · 必须走 domainTierLabel"
            )
        # 必须 import domainTierLabel
        assert "domainTierLabel" in src, f"{fname} 未引入 domainTierLabel"


def test_r12_v35Terminology_has_domainTierLabel():
    """[r12] v35Terminology.ts 必须导出 domainTierLabel · 覆盖 whitelist/gray/blacklist"""
    src = (FE / "lib" / "v35Terminology.ts").read_text(encoding="utf-8")
    assert "export function domainTierLabel(" in src
    for key in ["whitelist", "gray", "blacklist"]:
        assert key in src, f"domainTierLabel 缺 {key}"


def test_r12_round_status_badge_fallback_no_raw():
    """[r12 自检 r11 盲区] RoundStatusBadge 未知 status 不能直接显示 raw enum
    fallback 必须走 taskStatusLabel(status) · 不允许 { label: status, ... } 裸 fallback
    """
    fp = FE / "pages" / "Admin" / "ResearchMonitor" / "components" / "RoundStatusBadge.tsx"
    if not fp.exists():
        pytest.skip("RoundStatusBadge.tsx 不存在")
    src = fp.read_text(encoding="utf-8")
    # 不允许 `?? { label: status,` 直接 raw fallback
    assert re.search(r"\?\?\s*\{\s*label:\s*status\s*,", src) is None, (
        "RoundStatusBadge fallback 仍直接用 raw status · 必须走 taskStatusLabel(status)"
    )
    # 必须引入 taskStatusLabel
    assert "taskStatusLabel" in src, "RoundStatusBadge 未引入 taskStatusLabel"


def test_r12_article_card_no_dev_placeholder():
    """[r12 自检 r11 盲区] ArticleCard 不再显示 'Day X-Y XX 实施' dev placeholder"""
    # [#91] 原来只看 components/ArticleCard.tsx,而该文件全树已无 ⇒ 静默 skip,
    # 这条判据其实什么都没在检查。「dev 占位文案不许上线」是**目录级**关切,
    # 与某一个组件文件在不在无关 ⇒ 扫整个 ResearchMonitor(实测 0 命中)。
    src = "\n".join(fp.read_text(encoding="utf-8") for fp in rm_tsx_files())
    # JSX 文本不应含 'Day X-Y XX 实施' / 'Day X XX 实施' / 'placeholder' 类 dev 占位
    # 注释内可以保留 dev 周期标记
    # 简化:扫所有非注释行
    violations = []
    for i, line in enumerate(src.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*") or stripped.startswith("*/"):
            continue
        # 'Day X-Y 审核界面实施' / 'Day X 审核界面实施' 等 dev 占位
        if re.search(r"Day\s*\d+(-\d+)?\s*[^<>]{0,15}实施", line):
            violations.append((i, line.strip()[:120]))
    assert not violations, (
        "ArticleCard 仍含 'Day X-Y XX 实施' dev placeholder:\n" +
        "\n".join(f"  L{i}: {s}" for i, s in violations)
    )


def test_r13_research_monitor_no_dev_jargon():
    """[r13 老板 r12 复核盲区 · r14 扩展 researchMonitorApi.ts]
    ResearchMonitor 相关源码不允许出现:
       1. `Day\\s*\\d+`(开发周期编号 · 全文件禁 · 注释也不 allowlist)
       2. 注释行内的 'placeholder' / '占位' 词(JSX `placeholder=` prop 允许)
    覆盖范围:frontend/src/pages/Admin/ResearchMonitor/**/*.tsx
            + frontend/src/lib/researchMonitorApi.ts(r14 扩 · 老板 r13 抓注释残留)
    """
    # [#91] 用模块级 RM_DIR(可被判据 monkeypatch),并去掉 `不存在就 skip` 那个静默出口。
    targets = list(rm_tsx_files())
    api_lib = FE / "lib" / "researchMonitorApi.ts"
    if api_lib.exists():
        targets.append(api_lib)
    violations = []
    for tsx in targets:
        src = tsx.read_text(encoding="utf-8")
        for i, line in enumerate(src.split("\n"), 1):
            # 1. Day X 全文件禁(注释/代码/字符串都禁)
            if re.search(r"\bDay\s*\d+", line):
                violations.append((tsx.name, i, "Day X", line.strip()[:120]))
            # 2. placeholder/占位 仅在注释行内禁(JSX prop placeholder= 允许)
            stripped = line.strip()
            is_comment_line = (
                stripped.startswith("//") or stripped.startswith("*") or
                stripped.startswith("/*") or stripped.startswith("*/")
            )
            if is_comment_line:
                if re.search(r"\bplaceholder\b", line, re.IGNORECASE):
                    violations.append((tsx.name, i, "placeholder", line.strip()[:120]))
                # 🔴 [#91 · 2026-09-05] 「占位」必须带**开发标记**才算违规。
                #    本条要抓的是「开发周期占位文案没清干净」。而 2026-05 起
                #    ArticlesPanel / CitationsPanel 用「占位符」作**算法术语**
                #    (`**` 配对保护的 placeholder-swap 法),实测 6 处命中**全是**它,
                #    没有一处是开发标记 —— 裸词「占位」已经退化成纯噪声源。
                #    ⚠️ 这条**不能**像 r10/r11 那样套「去注释抽取器」:r13 是**故意**
                #       只扫注释的。给它剥注释 = 扫无可扫 = 恒绿,比现在的假阳更坏。
                #       修法是收窄**词面**,不是换扫描面。
                if is_dev_placeholder_comment(line):
                    violations.append((tsx.name, i, "占位(带开发标记)", line.strip()[:120]))
    assert not violations, (
        "ResearchMonitor 源码仍含开发占位词:\n" +
        "\n".join(f"  {n}:L{i} [{t}] · {s}" for n, i, t, s in violations[:30])
    )


def test_r10_research_monitor_toast_no_eng_term():
    """[r10 老板 r9 盲区修] ResearchMonitor toast 文案 0 处含 round_id / prompt_text / sort_order / confirm_code / cancelled / from + ${
    覆盖 toast.{success,error,info,warning}(`...英文工程词...`)
    """
    rm_dir = FE / "pages" / "Admin" / "ResearchMonitor"
    if not rm_dir.exists():
        pytest.skip("ResearchMonitor 目录不存在")
    violations = []
    forbidden_in_toast = [
        "round_id ${", "round_id $", "prompt_text ${", "sort_order ${",
        "confirm_code ${", "from ${", "→ cancelled",
    ]
    for tsx in rm_dir.rglob("*.tsx"):
        src = tsx.read_text(encoding="utf-8")
        lines = src.split("\n")
        for i, line in enumerate(lines):
            if not re.search(r"\btoast\.(success|error|info|warning)\(", line):
                continue
            block = "\n".join(lines[i:i + 6])
            for term in forbidden_in_toast:
                if term in block:
                    violations.append((tsx.name, i + 1, term, lines[i].strip()[:120]))
    assert not violations, (
        "ResearchMonitor toast 文案仍露工程英文:\n" +
        "\n".join(f"  {n}:L{i} [{t}] · {s}" for n, i, t, s in violations)
    )
