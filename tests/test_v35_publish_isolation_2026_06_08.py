"""V3.5 v8 publish_credit 隔离单测(P0 2026-06-08 Codex 复审抓真错)

铁律:V3.5 客户三池语义:
- tool_credit:工具类(诊断/写作/监测/advisor)· 可消费
- publish_credit:发布类(media_publish)· 独立 · 不可用工具
- bonus_credit:赠送 · 仅工具

后端 get_wallet_balance 必须拆字段:
- tool_credit_total = paid + tool_credit(工具预检用)
- publish_credit_total = publish_credit(发布预检用)
- bonus_points_total = bonus + bonus_credit(工具预检用)
- paid_points_total = paid + tool + publish(顶部展示用 · 不可预检)
"""
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_v35_customer_publish_heavy():
    """V3.5 客户 paid=0 / tool=0 / publish=10000 / bonus=0(Codex 抓真错的场景)"""
    return {
        "tool_credit_points": 0,
        "publish_credit_points": 10000,
        "bonus_credit_points": 0,
        "total_purchased_points": 10000,
        "total_consumed_points": 0,
        "agent_user_id": 28,
    }


@pytest.fixture
def mock_v35_customer_tool_heavy():
    """V3.5 客户 paid=0 / tool=10000 / publish=0 / bonus=0(对照组)"""
    return {
        "tool_credit_points": 10000,
        "publish_credit_points": 0,
        "bonus_credit_points": 0,
        "total_purchased_points": 10000,
        "total_consumed_points": 0,
        "agent_user_id": 28,
    }


@pytest.fixture
def mock_legacy_user():
    """legacy 用户 paid=5000 / bonus=2000 / commission=1000 · 无 customer_credit"""
    return {
        "tool_credit_points": 0,
        "publish_credit_points": 0,
        "bonus_credit_points": 0,
        "total_purchased_points": 0,
        "total_consumed_points": 0,
        "agent_user_id": None,
    }


def _build_balance(paid, bonus, commission, customer_credit):
    """重现 get_wallet_balance 关键返回字段算法(v8 P0 修后)"""
    credit_tool = customer_credit["tool_credit_points"]
    credit_publish = customer_credit["publish_credit_points"]
    credit_bonus = customer_credit["bonus_credit_points"]
    return {
        "paid_points": paid,
        "commission_points": commission,
        "bonus_points": bonus,
        "customer_credit": customer_credit,
        # v8 三池语义拆分
        "tool_credit_total": paid + credit_tool,
        "publish_credit_total": credit_publish,
        "bonus_points_total": bonus + credit_bonus,
        # 展示用(向后兼容)
        "paid_points_total": paid + credit_tool + credit_publish,
    }


def test_v35_publish_heavy_tool_isolation(mock_v35_customer_publish_heavy):
    """🔴 Codex 抓真错的场景:V3.5 客户 publish=10000/tool=0 · 跑诊断(工具)预检必须用 tool_credit_total 不并 publish"""
    balance = _build_balance(paid=0, bonus=0, commission=0, customer_credit=mock_v35_customer_publish_heavy)
    # 关键铁律:tool_credit_total 不含 publish · 否则前端误判"够"后端 402
    assert balance["tool_credit_total"] == 0, "tool_credit_total 必须不含 publish_credit"
    assert balance["publish_credit_total"] == 10000
    # 工具预检可用合计 = tool + bonus(不算 publish)
    tool_available = balance["tool_credit_total"] + balance["bonus_points_total"]
    assert tool_available == 0, f"V3.5 客户 publish=10000/tool=0 跑工具预检必须 0(实际 {tool_available})"
    # 顶部展示用 paid_points_total 含 publish(向后兼容 · 仅展示)
    assert balance["paid_points_total"] == 10000


def test_v35_tool_heavy_tool_predicate(mock_v35_customer_tool_heavy):
    """V3.5 客户 tool=10000/publish=0 跑工具 · 预检必须够"""
    balance = _build_balance(paid=0, bonus=0, commission=0, customer_credit=mock_v35_customer_tool_heavy)
    assert balance["tool_credit_total"] == 10000
    assert balance["publish_credit_total"] == 0
    tool_available = balance["tool_credit_total"] + balance["bonus_points_total"]
    assert tool_available == 10000


def test_legacy_user_no_customer_credit(mock_legacy_user):
    """legacy 用户 paid=5000 / bonus=2000 / commission=1000 · 工具预检 = paid+bonus+commission"""
    balance = _build_balance(paid=5000, bonus=2000, commission=1000, customer_credit=mock_legacy_user)
    assert balance["tool_credit_total"] == 5000  # paid + 0(无 customer_credit.tool)
    assert balance["publish_credit_total"] == 0
    assert balance["bonus_points_total"] == 2000
    assert balance["paid_points_total"] == 5000
    # legacy 工具预检 = paid + bonus + commission = 8000
    tool_available = balance["tool_credit_total"] + balance["bonus_points_total"] + balance["commission_points"]
    assert tool_available == 8000


def test_v35_publish_predicate_with_publish_credit(mock_v35_customer_publish_heavy):
    """[V3.5 v8 P0 老板 2026-06-08 拍板]V3.5 客户 publish=10000/tool=0 跑发布 · 预检走 publish+tool"""
    balance = _build_balance(paid=0, bonus=0, commission=0, customer_credit=mock_v35_customer_publish_heavy)
    # 新公式:paid + tool + publish + commission(充值算力都可发布)
    publish_available = (
        balance["paid_points"]
        + balance["customer_credit"]["tool_credit_points"]
        + balance["customer_credit"]["publish_credit_points"]
        + balance["commission_points"]
    )
    assert publish_available == 10000, "V3.5 客户 publish=10000 跑发布·publish 池兜底·够"


def test_v35_publish_fallback_to_tool_credit(mock_v35_customer_tool_heavy):
    """[V3.5 v8 P0 老板 2026-06-08 拍板]V3.5 客户 publish=0/tool=10000 跑发布(media_publish)·tool 兜底必过

    铁律:充值算力(tool_credit + publish_credit)都可换全功能(含发布)·赠送 (bonus_credit) 严禁发布
    场景:代理给客户充工具额度 10000 · 没单独配 publish 额度 · 客户依然能跑 media_publish(因为 tool 也是充值性质)
    """
    balance = _build_balance(paid=0, bonus=0, commission=0, customer_credit=mock_v35_customer_tool_heavy)
    publish_available = (
        balance["paid_points"]
        + balance["customer_credit"]["tool_credit_points"]
        + balance["customer_credit"]["publish_credit_points"]
        + balance["commission_points"]
    )
    MEDIA_PUBLISH_COST = 1300  # 假设 media_publish 成本(实际 0 在 feature_pricing 但概念测试)
    assert publish_available >= MEDIA_PUBLISH_COST, \
        f"V3.5 客户 tool=10000 跑发布·tool 必须兜底·实际可用 {publish_available}"


def test_v35_bonus_credit_cannot_publish():
    """[V3.5 v8 P0 老板 2026-06-08 铁律]赠送算力 (bonus_credit) 严禁发布·跑发布预检必须不算 bonus"""
    cc = {
        "tool_credit_points": 0,
        "publish_credit_points": 0,
        "bonus_credit_points": 10000,  # 只有赠送
        "total_purchased_points": 0,
        "total_consumed_points": 0,
        "agent_user_id": 28,
    }
    balance = _build_balance(paid=0, bonus=0, commission=0, customer_credit=cc)
    # 发布预检不含 bonus(赠送禁发布)
    publish_available = (
        balance["paid_points"]
        + balance["customer_credit"]["tool_credit_points"]
        + balance["customer_credit"]["publish_credit_points"]
        + balance["commission_points"]
    )
    assert publish_available == 0, "赠送算力禁发布·publish_available 必须不含 bonus_credit"
    # 工具预检 OK(赠送可工具)
    tool_available = (
        balance["paid_points"]
        + balance["customer_credit"]["tool_credit_points"]
        + balance["bonus_points"]
        + balance["customer_credit"]["bonus_credit_points"]
        + balance["commission_points"]
    )
    assert tool_available == 10000, "赠送可工具·tool_available 含 bonus_credit"


def test_paid_points_total_backward_compat(mock_v35_customer_publish_heavy):
    """paid_points_total 向后兼容 = paid + tool + publish · 仅顶部展示用(不可预检)"""
    balance = _build_balance(paid=500, bonus=0, commission=0, customer_credit=mock_v35_customer_publish_heavy)
    # paid_points_total 是历史字段 · 含 publish · 不可用于工具预检 · 仅"充值算力总额"展示
    assert balance["paid_points_total"] == 500 + 0 + 10000
    # 工具预检不能用 paid_points_total · 必须 tool_credit_total + bonus_points_total
    assert balance["tool_credit_total"] != balance["paid_points_total"], "tool_credit_total 与 paid_points_total 必须不同 · 防误用"


def test_no_publish_pollution_in_bonus():
    """bonus_points_total 必须只含 bonus + bonus_credit · 不能误算 publish/tool"""
    cc = {
        "tool_credit_points": 100,
        "publish_credit_points": 200,
        "bonus_credit_points": 300,
        "total_purchased_points": 600,
        "total_consumed_points": 0,
        "agent_user_id": 28,
    }
    balance = _build_balance(paid=50, bonus=70, commission=0, customer_credit=cc)
    assert balance["bonus_points_total"] == 70 + 300  # 不含 tool/publish


# ============================================================
# [Codex 复审 P0 2 轮] 社媒研究工具 feature_code 归类
# single_video / author_breakdown 必须是工具类 · 不是发布类
# V3.5 客户预检走 tool_credit_total + bonus 不走 publish_credit
# ============================================================

@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_social_research_features_are_tool_not_publish():
    """[Codex 复审 P0 轮 2]single_video / author_breakdown 是社媒研究工具 · 必须不在 publish 白名单。
    V3.5 客户跑这些·后端 _v35_check_credit_only 走工具池(bonus+tool)·
    前端两个社媒研究页(采集 / 拆解,已随开源 E3 删)当年必须用 toolPointsAvailable 不用 totalPoints"""
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import is_publish_feature 已删除。
    # 关键铁律:这些 feature 必须返 False(否则 V3.5 客户预检走错池)
    assert is_publish_feature("single_video") is False, "single_video 是工具类·不能算 publish"
    assert is_publish_feature("author_breakdown") is False, "author_breakdown 是工具类·不能算 publish"
    # 对照组:确认 publish 类正确识别
    assert is_publish_feature("publish_single") is True
    assert is_publish_feature("publish_mhz_media") is True
    assert is_publish_feature("publish_anything_with_prefix") is True


def test_v35_publish_heavy_blocks_social_research_predicate(mock_v35_customer_publish_heavy):
    """[Codex 复审 P0 轮 2 回归]V3.5 客户 publish=10000/tool=0 跑 single_video/author_breakdown 工具预检必须 0。
    前端用 toolPointsAvailable(不含 publish)→ 拒绝进入 executeCollect/executeBreakdown 路径。"""
    balance = _build_balance(paid=0, bonus=0, commission=0, customer_credit=mock_v35_customer_publish_heavy)
    # 前端 toolPointsAvailable 等效算法
    tool_points_available = (
        balance["paid_points"]
        + balance["customer_credit"]["tool_credit_points"]
        + balance["bonus_points"]
        + balance["customer_credit"]["bonus_credit_points"]
        + balance["commission_points"]
    )
    # 真实成本对齐 feature_pricing(老板拍 2026-06-08)
    SINGLE_VIDEO_COST = 390
    AUTHOR_BREAKDOWN_COST = 1950
    # 关键铁证:用 toolPointsAvailable·不够·前端必须挡(进 DeductDialog 走 InsufficientDialog)
    assert tool_points_available < SINGLE_VIDEO_COST, "V3.5 publish 重客户 工具预检必须不够"
    assert tool_points_available < AUTHOR_BREAKDOWN_COST, "V3.5 publish 重客户 工具预检必须不够"
    # 反证:旧 totalPoints 含 publish 会误判"够"
    total_points_old = balance["paid_points_total"] + balance["bonus_points_total"] + balance["commission_points"]
    assert total_points_old >= SINGLE_VIDEO_COST, "旧 totalPoints 会误判够 · 修后必须用 toolPointsAvailable"
