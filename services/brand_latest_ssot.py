"""`brands.latest_score` / `brands.latest_diagnosis_id` 的**唯一写入口径**(SSOT)。

## 为什么需要这个文件

2026-08-08 Deploy-CTO 收盘清理扫出两个独立缺陷,同一张表同一列:

1. **写成别人家的**(`WO_BRAND_LATEST_CROSS_TENANT_WRITE_2026-08-08`)——
   42 个品牌的 `latest_diagnosis_id` 指向别的品牌的诊断(190 个可 JOIN 品牌里占 22%)。
   根因:三个写入点是 `UPDATE brands ... WHERE id = %s` 这种**两个自由入参直接配对、零校验**的形态,
   调用方传错 → 当场串台,无任何报错。
2. **写成旧的那份**(`WO_V2_REGEN_POLLUTES_BRAND_LATEST_2026-08-08`)——
   重生一份**旧**诊断的 v2 报告会把 `latest_*` 拽回那份旧诊断。
   实测:浙江岱林生物客户列表分 69 → 50。触发路径含「服务商对旧诊断做人工身份确认」,
   **那是日常操作,不是运维动作。**

缺任一条,冗余列照样脏 —— 所以两条守卫必须在**同一个出口**上一起生效。

## 三重约束,全部落在 SQL 里

刻意**不在 Python 里判**,原因有二:少一次 check-then-act 竞态;调用方无法绕过。

| # | 约束 | SQL 形态 |
|---|---|---|
| ① | **归属** —— 诊断必须真属于被写的品牌 | `FROM diagnosis_records d ... AND d.brand_id = b.id` |
| ② | **配对** —— 调用方声明的 brand_id(若给)必须与 ① 一致 | `AND (CAST(%(brand_id)s AS INTEGER) IS NULL OR b.id = CAST(...))` |
| ③ | **最新** —— 必须是该品牌 published-only 口径下最新那份 | `AND d.id = (SELECT ... ORDER BY x.created_at DESC LIMIT 1)` |

`latest_diagnosis_id` 取 `d.id` 而不是再收一个入参 —— 参数配对这个危险面**直接消失**,
不存在「校验用 A、写入用 B」的可能。

## 口径来源(不是新造第四种写法)

`result_visibility` 的 published-only 谓词与「取最新那份」的子查询形态,逐字取自
`services/diagnosis_runs.py` 那条退款/中断回退 SQL —— 全仓已有的**正确**样板。
归属 JOIN 形态取自 `services/diagnosis_report_v2.py`。本文件是把这两半合起来收成一个出口,
**减少写法数量,不是增加**。

`result_visibility` 生产实测取值(2026-08-08 只读取证):NULL 278 / published 56 / withheld 4。
NULL 语义 = published(历史兼容),故谓词必须写成 `IS NULL OR = 'published'`。

## 🔴 不要做的事

- **不要改成「永不写」**。CTO-15.23 2026-05-06 P0-2 修的正是「忘写 `latest_score` 导致
  报告页 13/100 与客户横幅 33/100 撕裂」。要的是**加条件,不是删功能**。
- **不要把 `diagnosis_count` 并进来**。计数语义与 `latest_*` 无关(一份被守卫挡下的诊断
  照样算跑过一次),并进来会静默改变计数行为。各调用点的计数语句保持原样。
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["sync_brand_latest", "BRAND_LATEST_SYNC_SQL"]


# 三重约束一次写清。调用方只能通过这一条 SQL 写 latest_*。
BRAND_LATEST_SYNC_SQL = """
    UPDATE brands b
       SET latest_score        = %(score)s,
           latest_diagnosis_id = d.id,
           updated_at          = CURRENT_TIMESTAMP
      FROM diagnosis_records d
     WHERE d.id = %(diagnosis_id)s
       AND d.brand_id = b.id
       AND (CAST(%(brand_id)s AS INTEGER) IS NULL
            OR b.id = CAST(%(brand_id)s AS INTEGER))
       AND (d.result_visibility IS NULL OR d.result_visibility = 'published')
       AND d.id = (
             SELECT x.id
               FROM diagnosis_records x
              WHERE x.brand_id = b.id
                AND (x.result_visibility IS NULL OR x.result_visibility = 'published')
              ORDER BY x.created_at DESC, x.id DESC
              LIMIT 1
           )
"""


def sync_brand_latest(
    cur: Any,
    *,
    diagnosis_id: int,
    score: Optional[int],
    brand_id: Optional[int] = None,
) -> int:
    """把 `brands.latest_score` / `latest_diagnosis_id` 同步到这份诊断上。

    在**调用方已有的游标与事务**里执行 —— 不自己开连接,否则会脱离调用方事务,
    出现「诊断回滚了但冗余列留下」这种半写状态。

    Args:
        cur: 调用方的 DB 游标(调用方负责事务与 commit)。
        diagnosis_id: 要同步的诊断 id。
        score: 要写入的分数。显式传入而不取 `d.total_score` —— v2 漏斗分与
            `diagnosis_records.total_score` 在同一事务内的写入时序不保证,取列可能读到旧值。
        brand_id: 调用方声明的品牌 id。传了就参与校验(不配对 → 一行都不写);
            传 None 表示「归属以诊断记录为准」(`diagnosis_report_v2` 的既有语义)。

    Returns:
        受影响行数。**0 表示被守卫挡下**(不配对 / 不是最新 / 非 published),
        这是正常的防御结果,不是错误 —— 调用方按需决定要不要记日志。
    """
    cur.execute(
        BRAND_LATEST_SYNC_SQL,
        {
            "score": int(score or 0),
            "diagnosis_id": int(diagnosis_id),
            "brand_id": None if brand_id is None else int(brand_id),
        },
    )
    return int(getattr(cur, "rowcount", 0) or 0)
