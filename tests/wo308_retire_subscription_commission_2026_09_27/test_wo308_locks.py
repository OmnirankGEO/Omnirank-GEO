"""WO_308 · 停掉订阅比例佣金 + 订阅下单返回 410 的锁(2026-09-27)。

背景:08_billing 定 18%/3% 类比例佣金 05-30 已停,活着的推荐激励只有 L0 即时 bonus;
原订阅佣金模块是独立的订阅佣金(28/12/3% 阶梯,T+3 进 commission_points),没有任何开关管;
[开源 E3 · B4 · 2026-09-28] 订阅佣金 / 订阅支付两个模块与下单端点已整删,①②④⑤ 四格随之退役(对象不在了),
只剩 ③「调度表里不再有 3 个佣金 cron 与自动续费 cron」;「这些模块不许回来」由 tests/oss_e3a_routers_2026_09_28 接管。
`/api/subscription/checkout` 原样接受客户端传的 referrer_user_id(代码事实见 开源/E3_SUBSCRIPTION_REFERRAL_FACTS_2026-09-27.md)。
订阅支付回调不在鉴权白名单 ⇒ 付了款也开通不了(开源/E3_SUBSCRIPTION_RENEWAL_FACTS_2026-09-27.md §2)。

守五件事(全部进程内打桩,不连库、不联网):
  ① checkout 带 referrer_user_id ⇒ 410「该套餐已下线」,下单函数与数据库连接一次都不碰(不建订单行、不建支付单);
  ② 下单函数不论谁传推荐人,都不往下记(免费分支交给 grant_subscription 的推荐人是 None;全程 SQL 不碰佣金账本);
  ③ 调度表里不再有 3 个订阅佣金 cron 和 03:00 自动续费 cron,月度重置还在(对照臂);
  ④ 佣金入账 enqueue 是 no-op:数据库连接一碰就抛,它照样返回 noop;
  ⑤ 03:00 那个函数与 _try_auto_renew 直接调用也不碰数据库、不碰钱包(不扣 paid_points)。
牙证:在真文件上注毒(去掉 410 / 去掉置空 / 加回 cron / 去掉 no-op / 去掉自动续费 no-op),见交付单。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


RETIRED_CRONS = ("subscription_commission_settle", "subscription_clawback_resolve", "subscription_commission_reconcile",
                 "subscription_grace_check")
KEPT_CRONS = ("subscription_monthly_reset",)


class _Req:
    def __init__(self):
        self.state = SimpleNamespace(user={"user_id": 4242, "is_admin": False})
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers = {"user-agent": "pytest", "X-Device-Fingerprint": "fp"}


# ---------- ① checkout ⇒ 410,不建订单、不建支付单 ----------

# ---------- ② 下单函数不记推荐人 ----------

class _FakeCursor:
    def __init__(self, log):
        self.log = log
        self._one = None

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        if "FROM subscription_plans" in sql:
            self._one = {"plan_id": "free", "monthly_yuan": 0, "yearly_yuan": None, "first_month_yuan": None,
                         "is_active": True}
        else:
            self._one = None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, log):
        self.log = log
        self.autocommit = True

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ---------- ③ 调度表:4 个退役 cron 不在,月度重置在 ----------

def test_commission_crons_are_not_scheduled() -> None:
    from api import subscription_scheduler

    added = []

    class _FakeScheduler:
        def add_job(self, **kw):
            added.append(kw["id"])

        def get_jobs(self):
            return []

    with patch("api.scheduler.get_scheduler", lambda: _FakeScheduler()):
        subscription_scheduler.register_subscription_jobs()
    assert not (set(RETIRED_CRONS) & set(added)), added
    # 对照臂:没动过的月度重置必须还在(防止「全摘光」也算绿)
    assert set(KEPT_CRONS) <= set(added), added


# ---------- ④ enqueue 是 no-op,不连库 ----------

# ---------- ⑤ 自动续费不扣钱包 ----------
