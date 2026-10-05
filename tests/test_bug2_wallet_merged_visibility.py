"""[BUG-2 · 2026-07-27] 客户必须看得见自己全部的钱(两个钱包合并 + 迁移流水说人话)

Owner 原话:"关于钱的事情怎么能让用户看不到？简单直接的让客户看到数字不就好了吗？"

覆盖两层:
A. 余额层 —— 可用合计 = user_wallets(paid+bonus+commission) + credit(tool+publish+bonus)
   后端 get_wallet_balance 已给出合并子池;前端消费点【不得】自己用未合并字段重算。
B. 流水层 —— v35 迁移流水不再对客户整条消失,改写成"转入可用额度"的人话,
   金额归 0(不吓人),后台 include_internal_migrations=True 仍拿到原始 -19,500(不篡改账本)。

生产实证(RO 2026-07-27 · 5 个信用钱包客户):
  user 149 信用 9,663 + 平台 paid 36,516 → 只显示平台那半就等于对客户少报 9,663
  user 93/127 平台 paid 全 0,钱 100% 在信用钱包 → 老口径下客户看到的余额是 0

变异守卫:任一前端消费点改回 `wallet.paidPoints + ...` 自算合计 / 后端改回整条隐藏 → 转红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend" / "src"


# ============================================================
# A. 余额合并
# ============================================================

def test_backend_balance_exposes_merged_subpools():
    """后端必须给出合并子池,前端才有得用。"""
    import db.wallet_db as wallet_db

    class _Cursor:
        def __init__(self):
            self.q = ""

        def execute(self, query, _params=None):
            self.q = " ".join(str(query).split())

        def fetchone(self):
            if "user_social_subscriptions" in self.q:
                return None
            if "commission_clawback_pending" in self.q:
                return {"pending": 0}
            if "customer_agent_credit_wallets" in self.q:
                return {
                    "tool_credit_points": 9663,
                    "publish_credit_points": 500,
                    "bonus_credit_points": 200,
                    "total_purchased_points": 50818,
                    "total_consumed_points": 41155,
                    "agent_user_id": 7,
                }
            return None

    class _Conn:
        def cursor(self):
            return _Cursor()

        def close(self):
            return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wallet_db, "get_or_create_wallet", lambda _uid: {
            "paid_points": 36516, "commission_points": 0,
            "bonus_points": 2078, "frozen_points": 0, "total_recharged": 50818,
        })
        mp.setattr(wallet_db, "get_connection", _Conn)
        r = wallet_db.get_wallet_balance(149)

    assert r["customer_credit_status"] == "ready"
    # 客户可用合计 = 两个钱包全部可用池之和
    merged = r["paid_points_total"] + r["bonus_points_total"] + r["commission_points"]
    assert merged == (36516 + 2078 + 0) + (9663 + 500 + 200)
    # 老 total 字段只统计 user_wallets —— 保留兼容,但前端不得拿它当"可用合计"展示
    assert r["total"] == 36516 + 2078 + 0


def _read(rel: str) -> str:
    return (FE / rel).read_text(encoding="utf-8")


MERGED_BALANCE_CONSUMERS = [
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] 另两处合并余额消费方(M3 收入页 · 社媒工作台钱包页)随宿主整删
    "pages/Wallet/WalletPage.tsx",
]


@pytest.mark.parametrize("rel", MERGED_BALANCE_CONSUMERS)
def test_frontend_total_never_recomputed_from_unmerged_fields(rel):
    """禁止 `paidPoints + commissionPoints + bonusPoints` 这种自算合计 —— 它漏掉信用钱包。"""
    src = _read(rel)
    assert "wallet.paidPoints + wallet.commissionPoints + wallet.bonusPoints" not in src, \
        f"{rel} 自算合计会漏掉 customer_agent_credit_wallets,必须用 WalletContext 的 totalPoints"
    assert "paidPoints + commissionPoints + bonusPoints" not in src, rel


@pytest.mark.parametrize("rel", MERGED_BALANCE_CONSUMERS)
def test_frontend_uses_merged_display_fields(rel):
    """展示必须用合并字段(totalPoints / paidPointsDisplayed / bonusPointsDisplayed)。"""
    src = _read(rel)
    assert "totalPoints" in src, rel
    assert "paidPointsDisplayed" in src, rel


BALANCE_DISPLAY_WIDGETS = [
    "components/managed/QuickRechargeDialog.tsx",
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] 社媒充值弹窗随社媒组件目录整删
]


@pytest.mark.parametrize("rel", BALANCE_DISPLAY_WIDGETS)
def test_recharge_dialogs_show_merged_balance(rel):
    """充值弹窗里的"当前余额"必须是合并值,否则 V3.5 客户看到 0 会重复充值。"""
    src = _read(rel)
    assert "paidPointsDisplayed" in src, rel
    # 未合并的 paidPoints 不得再出现在充值弹窗里
    import re
    bare = re.findall(r"(?<![A-Za-z])paidPoints(?![A-Za-z])", src)
    assert not bare, f"{rel} 仍在用未合并的 paidPoints: {len(bare)} 处"


def test_wordplan_precheck_uses_full_recharge_pool():
    """前端预检不得只看平台 paid —— V3.5 客户的钱在信用钱包,会被恒判"余额不足"拦死。"""
    src = _read("components/managed/WordPlanDialog.tsx")
    assert "publishPointsAvailable < requiredPoints" in src
    assert "paidPoints < requiredPoints" not in src


# ============================================================
# B. 迁移流水说人话(不再整条消失)
# ============================================================

def test_migration_transaction_rewritten_not_hidden():
    from api.wallet_api import _decorate_wallet_transaction_for_display

    tx = {
        "id": 1,
        "type": "v35_migrated_to_customer_credit",
        "amount": -19500,
        "description": "V3.5 平台统一代收迁移",
        "feature_code": None,
    }
    out = _decorate_wallet_transaction_for_display(dict(tx))

    # 不吓人:不再对客户暴露 -19,500
    assert out["amount"] == 0
    # 说人话:客户能对上"我充了 150 块"
    assert "¥150" in out["description"]
    assert "到账" in out["description"]
    assert "可用额度" in out["description"]
    # 内部术语不得漏出
    assert "v35" not in out["description"].lower()
    assert "迁移" not in out["description"]
    # 给前端的中性渲染标记 + 真实转入额度仍可查
    assert out["display_kind"] == "credit_transfer"
    assert out["transferred_points"] == 19500


def test_migration_rewrite_uses_pricing_ssot_rate():
    """元值换算走 config.pricing_config SSOT,不得硬编码 130。"""
    import api.wallet_api as wallet_api
    import config.pricing_config as pc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pc, "get_points_per_yuan", lambda: 100)
        out = wallet_api._decorate_wallet_transaction_for_display({
            "type": "v35_migrated_to_customer_credit", "amount": -19500,
        })
    assert "¥195" in out["description"]


def test_other_transaction_types_untouched():
    """只改这一种类型,别的流水一个字都不能动。"""
    from api.wallet_api import _decorate_wallet_transaction_for_display

    tx = {"type": "consume", "amount": -650, "description": "GEO 诊断", "feature_code": "geo_diagnosis"}
    out = _decorate_wallet_transaction_for_display(dict(tx))
    assert out["amount"] == -650
    assert out["description"] == "GEO 诊断"
    assert "display_kind" not in out


def test_customer_transaction_api_no_longer_filters_migration():
    """客户流水接口不得再整条过滤掉迁移流水(过滤 = 客户对不上账)。"""
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    head = src.index("async def list_transactions(")
    body = src[head:src.index("@router.get(\"/packages\")", head)]
    assert "include_internal_migrations=False" not in body, \
        "客户流水改为展示 + 人话改写,不再整条隐藏"


def test_admin_view_keeps_raw_ledger():
    """后台/审计视角必须仍能拿到原始未改写的流水(不篡改账本)。"""
    import db.wallet_db as wallet_db
    where_admin, _ = wallet_db._build_transaction_where(149, None, include_internal_migrations=True)
    assert "type <> " not in where_admin
    # 参数仍保留隐藏能力,供其它需要的场景使用
    where_hidden, params = wallet_db._build_transaction_where(
        149, None, include_internal_migrations=False)
    assert "type <> " in where_hidden
    assert "v35_migrated_to_customer_credit" in params


def test_frontend_no_longer_hides_migration_rows():
    """前端也不得再藏 —— 后端翻好了前端再藏一层等于白改。"""
    src = _read("pages/Wallet/WalletPage.tsx")
    assert "CUSTOMER_HIDDEN_TRANSACTION_TYPES" not in src
    # 该类型要有中性渲染配置(否则落到 amount 正负兜底,0 会显示 '--' 但标签缺失)
    assert "v35_migrated_to_customer_credit: {" in src
    assert "转入可用额度" in src
    # committed freeze 的隐藏是另一回事,必须保留
    assert "freeze_status === 'committed'" in src
