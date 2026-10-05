"""锁:公开端点必须真的在中间件白名单里,而不是"看着像公开的"。

生产实证(2026-08-05):
  refchain 包新加 `GET /api/auth/register/attribution`(扫码归因,注册**前**调用,
  此刻用户还没有 JWT)。端点本身写得对、Review 逐行核过安全面,
  但 `PUBLIC_PATHS` 是**精确匹配的 set**,里面只有 `/api/auth/register` ——
  多一段路径就不相等,于是被全局中间件 401,**端点代码一行都执行不到**。
  结果:扫码归因功能上线了等于没上线。

  🔴 同型事故本文件里已经躺着一次:
     `"/api/wallet/wechat-refund-callback"` —— 「原缺此白名单致回调 401 退款不到账」。
  这是第二次。所以立锁。

每条「必须命中」配一条成对的「必须不命中」——
只证"它在白名单里"是陈述现状,证不了"判据还能抓到下一个漏网的"。
"""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from auth.middleware import PUBLIC_PATHS, PUBLIC_PREFIXES  # noqa: E402


def _would_pass(path: str) -> bool:
    """复刻中间件 :108 的放行判据(同一套逻辑,不另写一份)。"""
    return path in PUBLIC_PATHS or path.startswith(tuple(PUBLIC_PREFIXES))


# ───────────────────────── 必须命中 ─────────────────────────

def test_A1_attribution_is_public__must_hit():
    """扫码归因端点必须放行 —— 它在用户拿到 JWT 之前就被调用。"""
    assert _would_pass("/api/auth/register/attribution"), (
        "归因端点不在白名单 → 中间件 401 → 端点代码一行都跑不到 → 扫码归因功能等于没上线"
    )


def test_A2_prelogin_endpoints_all_public__must_hit():
    """其余"登录前必须能调"的端点一并锁住,防下次改白名单时误删。"""
    for p in (
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/captcha",
        "/api/auth/send-sms",
    ):
        assert _would_pass(p), p


# ───────────────────── 必须不命中(反向对照) ─────────────────────

def test_B1_authed_endpoints_stay_protected__must_not_hit():
    """🔴 判别力自证:需要鉴权的端点必须仍被拦。

    没有这一条,上面那些"放行"可能只是因为白名单被写成了放行一切。
    """
    for p in (
        "/api/referral/team",
        "/api/referral/stats",
        "/api/admin/users",
        "/api/wallet/balance",
    ):
        assert not _would_pass(p), f"{p} 不该公开"


def test_B2_prefix_matching_is_not_too_loose__must_not_hit():
    """🔴 前缀不许宽到把兄弟路径一起放行。

    例:`/api/auth/register` 是精确项,不是前缀 —— 所以
    `/api/auth/register-admin` 这种编造的兄弟路径必须仍被拦。
    """
    assert not _would_pass("/api/auth/register-admin")
    assert not _would_pass("/api/authx/register")


def test_C1_exact_match_semantics_is_the_trap__must_hit():
    """🔴 把踩过的坑写成可执行的判据:精确匹配不覆盖子路径。

    这条不是在锁某个端点,是在锁**这个语义** ——
    谁哪天把 PUBLIC_PATHS 改成前缀匹配,这条会红,红了就得重新想清楚安全面。
    """
    assert "/api/auth/register" in PUBLIC_PATHS
    # 子路径与父路径是两个不同的字符串:这就是 401 的成因
    assert "/api/auth/register/attribution" != "/api/auth/register"
    # 而它必须**单独**登记过,不是靠前缀蒙混
    assert "/api/auth/register/attribution" in PUBLIC_PATHS, (
        "必须显式登记,不许依赖前缀匹配 —— 前缀会连兄弟路径一起放行"
    )
