"""防御型 GEO · WP7 监测 / D0-D30 / 续费。

本包**不接管**现役监测链。规格 §13.1 逐字:「保留现役 raw result 与
``target_outcome``。防御事实准确性是**额外版本化投影**」。所以这里全部是
读侧投影 + 一张追加式 attempt 账本,现役 ``monitoring_run_cells`` /
``monitoring_results`` 的写语义一行不改。

模块分工
--------
``lineage``              §6.1 六边界身份(plan_cell_id / attempt_id / projection_id)
``attempt_ledger``       追加式 attempt 账本 —— 现役 cell 行会被重试**覆盖**,
                         MON-03/MON-10 要的"保留原始失败"只能另存
``denominators``         §6.3 版本化 denominator registry(20 项,逐项冻结计量单位)
``defensive_projection`` §13.1 DefensiveOutcomeProjection + R-1 AI 复判
``scenario_coverage``    §7.3 题族覆盖(planned/measured/covered 三分母)
``source_consistency``   §7.4 canonical facts 与实体状态五分
``attribution``          §7.5 显式来源归因 / 陈述支持 两条**正交**轴
``snapshot``             §13.3 冻结诊断快照 vs 实时监测(as_of)
``comparability_feed``   把真实 cell 喂进窗B 的 ``presentation.comparability``
``renewal``              §13.4 续费建议(**不自动扣费**)
``enrollment``           §15.9.3 v2 admission gate(未激活/未拨预算 ⇒ 零副作用)
"""
