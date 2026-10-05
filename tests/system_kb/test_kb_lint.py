from pathlib import Path

from tools.xiaobang_kb_lint import lint_pages


def _write_page(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_kb_lint_flags_normal_user_proxy_leak(tmp_path):
    page = tmp_path / "wallet.md"
    _write_page(page, """---
route: /wallet
page_name: 我的钱包
is_admin_only: false
visible_to: both
---

## 用途
查看余额。

## 字段
- 提现进度（可选）：查看佣金提现状态。
""")

    issues = lint_pages(str(tmp_path / "*.md"))

    assert any(i.check == "normal_user_leak" and "佣金" in i.detail for i in issues)


def test_kb_lint_accepts_line_level_agent_gate(tmp_path):
    page = tmp_path / "wallet.md"
    _write_page(page, """---
route: /wallet
page_name: 我的钱包
is_admin_only: false
visible_to: both
---

## 用途
查看余额。

## 字段
- [仅代理] 提现进度（可选）：查看佣金提现状态。
""")

    issues = lint_pages(str(tmp_path / "*.md"))

    assert issues == []


def test_kb_lint_flags_internal_feature_codes(tmp_path):
    page = tmp_path / "diagnosis.md"
    _write_page(page, """---
route: /diagnosis/new
page_name: 新建诊断
is_admin_only: false
visible_to: both
---

## 用户常问
- AI 填写扣什么？ → 成功后扣 brand_fill。
""")

    issues = lint_pages(str(tmp_path / "*.md"))

    assert any(i.check == "internal_code" and "brand_fill" in i.detail for i in issues)


def test_kb_lint_flags_agent_pool_l1_overreach(tmp_path):
    # 代理可见(visible_to=agent)的内容含 L2 专属词 → 普通代理(L1)会越权看到。
    page = tmp_path / "wallet.md"
    _write_page(page, """---
route: /wallet
page_name: 我的钱包
is_admin_only: false
visible_to: agent
---

## 字段
- 渠道服务费（可选）：L2 服务费分账卡。
""")

    issues = lint_pages(str(tmp_path / "*.md"))

    assert any(i.check == "agent_pool_l1_overreach" and "渠道服务费" in i.detail for i in issues)


def test_kb_lint_exempts_agent_agreement(tmp_path):
    # agent-agreement 是协议正文,合法含追索/L2 分层条款,豁免代理池越权扫描。
    page = tmp_path / "agent-agreement.md"
    _write_page(page, """---
route: /agent/agreement
page_name: 工厂模式协议
is_admin_only: false
visible_to: agent
---

## 用户常问
- 退款怎么办？ → 走三级追索；L2 分层另有约定。
""")

    issues = lint_pages(str(tmp_path / "*.md"))

    assert not any(i.check == "agent_pool_l1_overreach" for i in issues)
