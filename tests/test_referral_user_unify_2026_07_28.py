"""推广获客页统一改造判别锁(2026-07-28)。

① /referral 按新推广中心骨架重做普通用户版(InviteCenter:推广码/海报/推荐列表);
② 身份路线铁律:普通用户页零"佣金/提现/28%",只见 15% 即时路线,保留升级不补差提示;
③ 分身份取数:普通用户页零 agent 专属 API;服务商佣金体系一字不动;
④ 路由保留 /referral,老页面归档不删。
变异:塞佣金字样/换 agent API/路由回退/删提示 → 转红。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend" / "src"

_INVITE = FE / "pages" / "Referral" / "InviteCenter.tsx"
_LEGACY = FE / "pages" / "Referral" / "ReferralCenter.tsx"
_APP = FE / "App.tsx"
_POSTER = FE / "components" / "promotion" / "PromotionPoster.tsx"
_AGENT_PROMO = FE / "pages" / "Agent" / "PromotionCenter.tsx"


# ===========================================================================
# ② 身份文案铁律
# ===========================================================================
def test_invite_center_copy_has_no_commission_withdraw_or_28():
    """普通用户页零"佣金/提现/18%"。变异(塞任一字样)→ 本锁转红。"""
    src = _INVITE.read_text(encoding="utf-8")
    assert "佣金" not in src
    assert "提现" not in src
    assert "28%" not in src
    assert "间推" not in src and "直推" not in src


def test_invite_center_shows_15_percent_instant_route():
    """15% 即时路线可见(页头 Badge + 说明 + 明细列),不是藏在接口字段里。"""
    src = _INVITE.read_text(encoding="utf-8")
    assert "15%" in src
    assert "即时" in src
    assert 'data-testid="bonus-rate"' in src  # 明细里逐笔呈现比例


def test_invite_center_keeps_upgrade_no_backfill_notice():
    """升级服务商不补差提示保留,且该句不携带资金类字样。"""
    src = _INVITE.read_text(encoding="utf-8")
    # 钉 UI 原句(文件头注释另有"不补差"三字,不能替 UI 文案背书)
    assert "按当时身份结算,不补差额" in src
    assert "升级服务商" in src
    assert "合作伙伴计划" in src


# ===========================================================================
# ③ 分身份取数
# ===========================================================================
def test_invite_center_uses_zero_agent_apis():
    """普通用户页零 agent 专属 API(硬约束)。变异(调 promotionQR)→ 转红。"""
    src = _INVITE.read_text(encoding="utf-8")
    assert "/api/agent/" not in src
    assert "agentApi" not in src
    assert "promotionQR" not in src and "promotionCustomers" not in src
    # 正确数据源三件套(登录即可用的 /api/referral/*)
    assert "/api/referral/code" in src
    assert "/api/referral/stats" in src
    assert "/api/referral/bonus-history" in src
    # stats 只取人数,不消费任何 commission 字段
    assert "commission" not in src


def test_provider_promotion_data_sources_untouched():
    """服务商侧数据源与佣金体系一字不动。"""
    src = _AGENT_PROMO.read_text(encoding="utf-8")
    assert "agentApi.promotionQR()" in src
    assert "agentApi.promotionCustomers(" in src
    app = _APP.read_text(encoding="utf-8")
    assert 'path="agent/promotion"' in app  # 服务商推广路由原样
    # 佣金面板文件在库且被归档页引用(v35 审计锁也 read_text 它们)
    assert (FE / "pages" / "Referral" / "CommissionPanel.tsx").exists()
    assert (FE / "pages" / "Referral" / "CommissionPanelV32.tsx").exists()


# ===========================================================================
# ① 骨架 + ④ 路由与归档
# ===========================================================================
def test_invite_center_carries_promotion_skeleton():
    """新版三段式:推广码(+二维码)/海报(复用 PromotionPoster)/推荐列表。"""
    src = _INVITE.read_text(encoding="utf-8")
    assert "我的推荐码" in src
    assert "QRCode.toDataURL" in src          # 客户端二维码,与新推广中心同法
    assert "PromotionPoster" in src           # 复用同一海报组件(邀请口吻 props)
    assert "html2canvas" in src
    assert "我的推荐(" in src                  # 推荐列表卡
    assert "formatApiErrorForDisplay" in src  # v35 错误文案收口口径


def test_route_switched_and_legacy_archived_not_deleted():
    """/referral 路由切到 InviteCenter;老页归档保留(不删),App 不再引用。

    变异(路由回退 ReferralCenter)→ 本锁转红。
    """
    app = _APP.read_text(encoding="utf-8")
    assert 'path="referral" element={<InviteCenter />}' in app
    # 归档:App 对老页零 import(注释提及路径不算引用,锁钉 import 语句)
    assert "import('@/pages/Referral/ReferralCenter')" not in app
    assert "<ReferralCenter />" not in app
    legacy = _LEGACY.read_text(encoding="utf-8")
    assert "归档 2026-07-28" in legacy  # 归档标注在场,文件保留


def test_poster_defaults_keep_agent_page_unchanged():
    """海报组件新文案 props 全 optional,默认=服务商原文案(agent 页零感知)。"""
    src = _POSTER.read_text(encoding="utf-8")
    assert "title?:" in src and "footerText?:" in src and "ctaText?:" in src
    # 默认标题两行都必须完整兜底(单行被掏空=服务商海报出空行,也要抓)
    assert src.count("['让客户在 AI 里', '更容易被找到']") >= 2
    assert "'扫码了解服务'" in src
    assert "`由${footerName}提供服务`" in src
    agent = _AGENT_PROMO.read_text(encoding="utf-8")
    assert "title=" not in agent.split("PromotionPoster")[1][:400], (
        "服务商页不传新 props,走默认 —— 保持零改动"
    )
