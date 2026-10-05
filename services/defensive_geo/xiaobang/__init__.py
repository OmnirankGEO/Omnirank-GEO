"""防御型 GEO · WP8 小榜。

🔴 §0.5.1 第 2 条:**不另设计第二套执行链**
------------------------------------------
「小榜五阶段执行链已在 39/40 班建成(``services/xiaobang_publish_execute.py``、
``xiaobang_command_contract.py``、``xiaobang_decision_ledger.py``、
``xiaobang_channel_eligibility.py``,幂等键 = confirmation receipt,
复用 ``geo_douyin/publish_batch_core.materialize_publish_batch``)。
WP8 一律改写为**在现役 execute 合同上增加 defensive 动作**。」

所以本包里**没有** prepare/confirm/execute 的第二份实现。有的是:

``frozen_reasons``    §9.6:discovery / prefill / 只读解释**只读冻结公开理由**,
                      不现场生成媒体理由或商业数字
``defensive_actions`` 往现役 ``services/gap_operation_map`` 注册表加 defensive 条目
                      (走同一个 CommandContract 形态、同一套五阶段)
``receipt_guard``     POR-13:七维绑定核验 —— 聊天「好的」零扣费
``kb_entries``        新增 KB 条目(零社媒),载两条 Owner 铁律
"""
