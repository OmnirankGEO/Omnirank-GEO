"""营销中心(营销军师 + 物料工厂)服务层。

照抄 services/ai_ops 架构模式(巡逻→信号→立案→双闸→人审→执行 + 受限角色),
换规则库 / 换工具集 / 换数据白名单;审批/台账/双闸/审计复用同款。
红线:零改 middleware/billing.py / db/wallet_db.py / db/connection.py / auth/*。
"""
