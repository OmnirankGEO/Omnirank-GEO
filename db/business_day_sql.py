"""业务日边界的 SQL SSOT(WO-LATENT-TRAPS §2 · 2026-08-17)

## 这条轴到底是什么(真 PG 实测,不是推理)

`scripts/research/tz_axis_evidence_probe.py` 在同一个 PG 上把三个会话时区各跑一遍,
夹具刻意取 `2026-08-17 16:30 UTC == 2026-08-18 00:30 Asia/Shanghai`(**跨日界**,
第一版夹具用 `NOW()`,三个时区碰巧同一天 → 零判别力,差点得出"全都不敏感"的假结论):

| 表达式                                          | Asia/Shanghai | UTC        | New_York   | 结论       |
|-------------------------------------------------|---------------|------------|------------|------------|
| `naive::date`                                   | 2026-08-18    | 2026-08-18 | 2026-08-18 | 时区无关   |
| `date_trunc('day', naive)`                      | 08-18 00:00   | 08-18 00:00| 08-18 00:00| 时区无关   |
| `timestamptz::date`                             | 2026-08-18    | 2026-08-17 | 2026-08-17 | **敏感**   |
| `naive::date = timestamptz::date`               | true          | false      | false      | **敏感**   |
| `naive::date = (tstz AT TIME ZONE 'Asia/Shanghai')::date` | true | true    | true       | 修好了     |

所以**不是** `created_at::date` 本身有问题 —— 它只是墙钟截断,时区无关。
有问题的是它**对上** `CURRENT_DATE` / `NOW()` 这类**会话派生的今天**:
一边是「按 DB 写入时区存下来的墙钟」,一边是「按会话时区算出来的今天」,两把尺子。
生产现在 `SHOW TimeZone = Asia/Shanghai`,两把尺子恰好重合 —— **碰巧对,不是设计对**。
DB 参数改一次 / 起一个 UTC 的副本库 / 换云厂商默认值,日限额、服务窗口、监测调度当场平移 8 小时,
而且**不报错**。

## 修法

把那个「今天」显式钉到业务时区,不让它跟着会话跑:

    created_at::date = CURRENT_DATE
    ↓
    created_at::date = (NOW() AT TIME ZONE 'Asia/Shanghai')::date

生产 TimeZone 本来就是 Asia/Shanghai,所以**今天的行为逐字节不变**(既有用例照常绿);
换成 UTC 库跑,旧写法结果会漂,新写法不漂 —— 判据就打在这个差上
(`tests/test_business_day_boundary_tz_2026_08_17.py`)。

## 用法

    from db.business_day_sql import BIZ_TODAY_SQL
    sql = f"... WHERE created_at::date = {BIZ_TODAY_SQL}"

既有 7 处修复点为了把 diff 压到最小、避免把普通字符串改成 f-string,
直接内联了同一段文本;判据 `test_inlined_text_matches_ssot` 逐处比对它与本模块常量**逐字相同**,
所以内联不会跟 SSOT 漂开。
"""

from __future__ import annotations

# 业务时区 —— 与生产 DB TimeZone 一致(2026-08-17 `SHOW TimeZone` 只读取证 = Asia/Shanghai)。
# 这里写死是**故意的**:业务日就该由业务定义,不该由 DB 参数定义。
BUSINESS_TIMEZONE = "Asia/Shanghai"

# 「业务时区的今天」—— 会话时区无关。替代裸 CURRENT_DATE / NOW()::date。
BIZ_TODAY_SQL = "(NOW() AT TIME ZONE 'Asia/Shanghai')::date"

# 「业务时区的此刻(墙钟)」—— 与 naive 列同一把尺子。替代裸 NOW() 做边界比较时用。
BIZ_NOW_SQL = "(NOW() AT TIME ZONE 'Asia/Shanghai')"

__all__ = ["BUSINESS_TIMEZONE", "BIZ_TODAY_SQL", "BIZ_NOW_SQL"]
