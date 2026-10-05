/**
 * V3.5 Follow-up 批 4 · IA 合并 · 兼容老 bookmark
 *
 * 原"对账日报历史"独立页已合并到 InventoryAudit.tsx 的"历史记录" / "异常明细" tabs
 * 老路由 /admin/inventory-audit-history 继续保留 · 直接渲染新合并页
 * sidebar 入口已删除(2026-05-26 批 3)
 */
export { default } from './InventoryAudit';
