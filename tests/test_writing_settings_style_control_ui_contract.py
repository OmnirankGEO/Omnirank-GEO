"""Writing style flywheel control console UI contract.

These source-level tests guard the admin settings console shape without a
browser runner. Runtime behavior is covered by the API/manager tests.
"""

from __future__ import annotations

from pathlib import Path


COMPONENT = Path("frontend/src/components/WritingSettingsDialog.tsx")


def _source() -> str:
    return COMPONENT.read_text(encoding="utf-8")


def test_writing_settings_exposes_full_style_control_console():
    source = _source()

    assert "写作设置" in source
    assert "日常总览" in source
    assert "文体优化" in source
    assert "高级工具" in source
    assert 'value="versions"' in source
    assert 'value="flywheel"' in source
    assert 'value="align"' in source
    assert 'value="evidence"' in source
    assert 'value="release"' in source
    assert "文体版本" in source
    assert "飞轮资料" in source
    assert "一键对齐" in source
    assert "证据链" in source
    assert "启用或回退" in source
    assert "设为当前启用" in source
    assert "回退默认版本" in source
    assert "退役候选" in source
    assert "生成待审核候选" in source


def test_writing_settings_calls_style_control_api_surface():
    source = _source()

    assert '"/api/writing/style-control/versions"' in source
    assert '"/api/writing/style-control/align-draft"' in source
    assert '"/api/writing/style-control/activate"' in source
    assert '"/api/writing/style-control/rollback"' in source
    assert '"/api/writing/style-control/retire"' in source
    assert '"/api/writing/style-control/audit-log?limit=80"' in source
    assert "/api/writing/style-control/flywheel-summary" in source
    assert "/api/writing/style-control/evidence-chain/" in source


def test_writing_settings_style_control_shows_safety_contract():
    source = _source()

    assert "只生成待审核候选" in source
    assert "不直接启用" in source
    assert "不覆盖默认回退版本" in source
    assert "不会让实验内容给客户看" in source
    assert "实验内容给客户看始终关闭" in source
    assert "阻断" in source
    assert "需复核" in source
    assert "提醒" in source


def test_writing_settings_style_control_uses_operator_facing_copy():
    source = _source()

    assert "写作服务" in source
    assert "文章模板" in source
    assert "内部观测" in source
    assert "高级模板（慎用）" in source
    assert "实验内容给客户看" in source
    assert "当前启用" in source
    assert "待审核候选" in source
    assert "默认回退版本" in source
    assert "规则校验" in source
    assert "语义复核" in source
    assert "数据待接入" in source
    assert "采纳样本" in source
    assert "对照样本" in source
    assert "我们之前优化了什么" in source
    assert "旧问题" in source
    assert "现在怎么防住" in source
    assert "新旧模板对比" in source
    assert "一键流程" in source
    assert "一键生成候选" in source
    assert "查看测试分数" in source
    assert "查看内部测试记录" in source
    assert "不伪造测试通过" in source
    assert "新版内置模板" in source
    assert "当前可用模板" in source

    visible_forbidden = [
        "LLM 模型",
        "提示词模板",
        "Shadow 运行",
        ">Active<",
        ">Draft<",
        ">Stable<",
        ">Guard<",
        ">Judge<",
        ">not_run<",
        "active id",
        "回退 stable",
        "设为 active",
        ">stable_baseline<",
        ">prompt_overrides<",
        "customer output",
        ">with_evidence<",
        ">no_evidence<",
    ]
    for term in visible_forbidden:
        assert term not in source


def test_writing_settings_style_control_does_not_add_customer_output_actions():
    source = _source()

    assert "/api/writing/style-control/customer-output" not in source
    assert "/api/writing/style-control/publish-customer" not in source
    assert "发布到客户" not in source
    assert "开启客户可见输出" not in source
    assert "客户可见输出永久关闭" not in source
    assert "实验内容给客户看永久关闭" in source
    assert "当前展示的是系统默认模板" not in source
    assert "旧评分写法" not in source
    assert "未实际生效" not in source
    assert "customer_output_allowed: true" not in source
