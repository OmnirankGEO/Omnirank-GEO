"""服务期唯一 SSOT —— 一口钟。

工单:`WO_SERVICE_PERIOD_SSOT_2026-08-06` §1(Review-CTO 出单 · Owner 报"倒计时说还有
一年,轮换闸说上月就到期")。生产实证:13 张 status='paid' 报价里 **11 张两钟打架**。

────────────────────────────────────────────────────────────────────────────
两个概念,单位不同,一个都不许混
────────────────────────────────────────────────────────────────────────────

1) **合同自然日历服务期**(本模块说的"服务期")
   SSOT = `quotes.service_start_date` + `quotes.service_end_date` 这一对 DATE 列。
   读它的人:监测页/门户倒计时、"服务期至"、自动监测轮换资格闸、续费提醒 scheduler、
            m3 `lq_service_end_date` / 交付队列 / 素材确认闸、门户 token 有效期。
   写它的人:**只有本模块的 `resolve_activation_period()`**,由付款激活/服务期调整
            端点调用。`service_months` 自此只是"算 end 的入参",不再是第二个真相。

2) **履约达标天数配额** = `quotes.service_days`
   单位是"累计达标天数"(Owner A 方案 2026-06-04 / 2026-06-23 两次拍板),
   **不是日历天**。服务完成 = `compliant_days >= service_days`,不设自然日历封顶。
   它**永远不许被加到某个日期上**得出"服务期至" —— 那正是本次事故的成因:
   `service_days` 的库默认值是 365,谁也没写过它,倒计时却拿它 `start + 365 天`
   算出"还有一年",而轮换闸读的 `service_end_date` 是 `start + service_months(默认 1)`
   = 一个月后就静默停轮换。

────────────────────────────────────────────────────────────────────────────
禁令(由 `tests/test_service_period_ssot_2026_08_06.py` 一致性锁守着)
────────────────────────────────────────────────────────────────────────────
- 禁 `service_months or 1`:月数缺失 → 抛 `ServicePeriodError` 进人工,**不许猜**。
  猜出来的是合同期,猜错就是错交付 / 错停服。
- 禁 `COALESCE(service_days, 365)` / `service_days or 365`:
  `migration_028` 已把该列置 NOT NULL(生产实测 392/392 行非空,回填 0 行),
  兜底从此是死代码 —— 留着只会再长出第三口钟。
- 禁在本模块之外用 `relativedelta(months=…)` 换算服务期;
  禁任何 `<某个日期> + service_days` 形式的"服务期至"。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional, Tuple

__all__ = [
    "MIN_SERVICE_MONTHS",
    "MAX_SERVICE_MONTHS",
    "ServicePeriodError",
    "coerce_date",
    "normalize_service_months",
    "compute_service_end",
    "resolve_activation_period",
    "read_calendar_period",
    "calendar_days_left",
    "is_within_calendar_period",
    "compliance_target_days",
    "service_period_missing_hint",
    "resolve_auto_monitoring_status",
]

MIN_SERVICE_MONTHS = 1
MAX_SERVICE_MONTHS = 24


class ServicePeriodError(ValueError):
    """服务期缺失 / 非法 —— fail-closed 进人工。

    `user_message` 是给代理看的人话(工程术语全站翻人话);`hint` 带解决路径
    (提示铁律:任何面向用户的提示必须自带解决方案快捷路径,否则不许出现)。
    """

    def __init__(self, user_message: str, *, hint: Optional[dict] = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.hint = hint or {}


def service_period_missing_hint(quote_id: Optional[int] = None) -> dict:
    """"没有服务期"这件事该怎么显示 —— 带出口,不只报数字。"""
    return {
        "code": "SERVICE_PERIOD_NOT_SET",
        "message": "这个客户还没定服务期 · 定了才有倒计时,自动监测也才会排班",
        "actions": [
            {
                "label": "去设服务期",
                "type": "open_service_period",
                "quote_id": quote_id,
            }
        ],
    }


def coerce_date(value: Any) -> Optional[date]:
    """把库里/请求里各种形态的日期收敛成 `date`;认不出来就返 None(不猜)。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text.split("T")[0].split(" ")[0])
    except (ValueError, TypeError):
        return None


def normalize_service_months(value: Any) -> int:
    """把服务月数收敛成 1..24 的整数;缺失/非法 → 抛错进人工。

    🔴 这里**刻意没有** `or 1`。旧代码六处 `req.service_months or 1` 把"销售没填"
    和"销售填了 1 个月"压成同一个值 —— 一份多月合同会被激活成 1 个月,
    一个月后自动监测静默停轮换,而客户界面还显示服务充足。
    """
    if value is None or value == "":
        raise ServicePeriodError(
            "这单还没填服务期(几个月)· 填了才能激活,系统不替你猜",
            hint=service_period_missing_hint(),
        )
    try:
        months = int(value)
    except (TypeError, ValueError):
        raise ServicePeriodError(
            f"服务期(月数)填的不是数字:{value!r}",
            hint=service_period_missing_hint(),
        ) from None
    if months < MIN_SERVICE_MONTHS or months > MAX_SERVICE_MONTHS:
        raise ServicePeriodError(
            f"服务期只支持 {MIN_SERVICE_MONTHS}-{MAX_SERVICE_MONTHS} 个月,收到 {months}",
            hint=service_period_missing_hint(),
        )
    return months


def compute_service_end(start: date, months: int) -> date:
    """`start + months 个自然月` —— 全站**唯一**一处 months → end 的换算。

    别处再出现 `relativedelta(months=…)` 算服务期,一致性锁会转红。
    """
    from dateutil.relativedelta import relativedelta

    if not isinstance(start, date) or isinstance(start, datetime):
        start = coerce_date(start)
    if start is None:
        raise ServicePeriodError(
            "服务期起始日无效 · 请重新选起始日",
            hint=service_period_missing_hint(),
        )
    return start + relativedelta(months=int(months))


def resolve_activation_period(
    *,
    start_date: Any = None,
    months: Any,
    today: Optional[date] = None,
) -> Tuple[date, date]:
    """**唯一写入换算点**:给激活/调整端点算 (service_start_date, service_end_date)。

    - `start_date` 缺省 = 今天(起始日缺省是无害的,合同都从"今天开始"起算);
    - `months` **不缺省** —— 缺失/非法一律抛 `ServicePeriodError`(§1.2 不许猜)。

    返回的一对值必须**同一条 UPDATE 一起落库**,任何只写其中一个的路径都是新的分裂点。
    """
    months_int = normalize_service_months(months)
    start = coerce_date(start_date) or (today or date.today())
    end = compute_service_end(start, months_int)
    return start, end


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(key)
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(key)
    return getattr(row, key, None)


def read_calendar_period(row: Any) -> Tuple[Optional[date], Optional[date]]:
    """**唯一读点**:从一行 quotes 里取 (start, end)。

    只认 `service_start_date` / `service_end_date`。
    绝不从 `service_days` 或 `service_months` 反推 —— 反推就是在造第二口钟。
    """
    return coerce_date(_row_get(row, "service_start_date")), coerce_date(
        _row_get(row, "service_end_date")
    )


def calendar_days_left(end: Any, today: Optional[date] = None) -> Optional[int]:
    """离服务期结束还有几天 · **带符号**(负数 = 已过期 N 天)· 没有服务期返 None。

    不 clamp 到 0:UI 要能说"已过期 N 天"(老板 v1.7 复审 P1),
    clamp 掉就又变成"看起来还在期"。
    """
    end_date = coerce_date(end)
    if end_date is None:
        return None
    return (end_date - (today or date.today())).days


def is_within_calendar_period(end: Any, today: Optional[date] = None) -> bool:
    """今天还在服务期内吗 · 没有服务期 = False(fail-closed,不当作永久有效)。"""
    left = calendar_days_left(end, today=today)
    return left is not None and left >= 0


def compliance_target_days(row: Any) -> Optional[int]:
    """履约达标天数配额(`quotes.service_days`)· **不是日历天,永远别加到日期上**。

    NULL / ≤0 → 返 None,由调用方显式处理。**这里没有 365 兜底**:
    `migration_028` 把该列置了 NOT NULL,兜底是死代码;
    真要是 NULL,那说明有人绕过了约束,应当被看见,而不是被 365 糊过去。
    """
    raw = _row_get(row, "service_days")
    if raw is None:
        return None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None
    return days if days > 0 else None


def calendar_span_days(start: Any, end: Any) -> Optional[int]:
    """服务期一共多少个自然日 —— 展示用派生值(§1.1:天数从此是派生的,不是源)。"""
    s, e = coerce_date(start), coerce_date(end)
    if s is None or e is None:
        return None
    return (e - s).days


# ─────────────────────────────────────────────────────────────────────────────
# [客户反馈⑥ 2026-08-09] 自动监测**真实**排班资格 —— 界面说"已暂停"之前先问它
# ─────────────────────────────────────────────────────────────────────────────
#
# 🔴 事实(2026-08-09 生产快照只读实证 + 源码双证,写在这里免得下一个人再猜一遍):
#   自动监测**有两条互斥的排班链**,日历到期只卡其中一条:
#
#   链 A · 逐词订阅(`keyword_monitor_subscriptions`,`list_active_subscriptions`)
#     WHERE 里**没有** service_end_date 这一条,停机条件是
#     `compliant_days >= service_days`(Owner 2026-06-04 A 方案:纯履约,不设日历封顶)。
#     → 日历到期**不停**。实测:quote 287(雅栖)service_end_date=2026-06-10 已过期 60 天,
#       3 条订阅全 active,`last_charged_at` = 2026-08-08(前一天还在扣费跑)。
#       全库同形态(日历过期 + 有 active 订阅)共 4 张报价。
#
#   链 B · 报价级轮换(`get_monitoring_enabled_clients`)
#     只接**没建过逐词订阅**的核心词,WHERE 里明写
#     `service_end_date >= CURRENT_DATE` → 日历到期**确实停**。
#     (2026-08-06 那次改动刻意没动这条闸,只是给它补了内部通知。)
#
#   所以前端那句写死的"(已到期 N 天 · 自动监测已暂停)"对链 A 的客户是**假的**,
#   对链 B 的客户是真的。判据不是日期,是"这个客户挂在哪条链上"。
def resolve_auto_monitoring_status(
    *,
    monitoring_enabled: Any,
    service_end: Any,
    active_subscription_count: Any,
    today: Optional[date] = None,
) -> dict:
    """自动监测此刻**真的**在跑吗 —— 返回 (active, code, 人话)。

    参数全部是**事实**,不是判断:开关 / 服务期结束日 / 还有几条 active 逐词订阅。
    调用方负责把这三个事实查出来;是与不是的解释只在这一处。
    """
    enabled = bool(monitoring_enabled)
    try:
        sub_count = int(active_subscription_count or 0)
    except (TypeError, ValueError):
        sub_count = 0
    end = coerce_date(service_end)
    left = calendar_days_left(end, today=today)
    expired = left is not None and left < 0

    # 🔴 顺序不能反(Review 2026-08-09 P1-1 判红,生产快照实证)。
    #   `monitoring_enabled` 是 **quotes 级开关**,它只出现在链 B
    #   (`get_monitoring_enabled_clients` 的 `AND q.monitoring_enabled = TRUE`);
    #   链 A(`list_active_subscriptions`)的 WHERE 里**根本没有这一列** ——
    #   逐词订阅一旦建立就由订阅状态机管,与报价级开关解耦。
    #   把 `not enabled` 放在最前面,等于用链 B 的闸去否定链 A 的事实:
    #   生产快照实测 quote 366(浙江岱林生物)`monitoring_enabled=false`
    #   却有 **2 条真实可排班订阅** —— 界面会说"开关没开",而 daily job 照跑照扣费。
    #   那正是本条修复要消灭的那类假话,只是换了个方向。
    if sub_count > 0:
        # 链 A:按达标天数履约。日历到期不停,报价级开关也管不着它。
        return {
            "active": True,
            "code": "compliance_driven",
            "message": ("服务期日历已过,自动监测仍在按达标天数继续跑"
                        if expired else "自动监测按达标天数正常进行"),
            "calendar_expired": expired,
        }
    # 以下都是"没有可排班的逐词订阅"→ 只剩链 B,这时报价级开关才说了算。
    if not enabled:
        return {
            "active": False,
            "code": "monitoring_disabled",
            "message": "自动监测开关没开",
            "calendar_expired": expired,
        }
    if end is None:
        return {
            "active": False,
            "code": "service_period_not_set",
            "message": "还没设服务期,自动监测排不上班",
            "calendar_expired": False,
        }
    if expired:
        # 链 B 且日历过期 —— 这才是唯一"已暂停"为真的情形。
        return {
            "active": False,
            "code": "rotation_blocked_calendar",
            "message": "服务期已到,自动监测已暂停,续费后自动恢复",
            "calendar_expired": True,
        }
    return {
        "active": True,
        "code": "rotation_active",
        "message": "自动监测正常进行",
        "calendar_expired": False,
    }


def portal_token_cap(end: Any, grace_days: int) -> Optional[date]:
    """门户 token 最远能续到哪天 = 服务期结束 + 宽限期。没有服务期 → None(不续)。"""
    e = coerce_date(end)
    if e is None:
        return None
    return e + timedelta(days=int(grace_days))
