/**
 * Density System v1 · 组件桶 · CTO-15.23 2026-05-22
 *
 * 解决:整站"文字密密麻麻"病
 * - text-[10px]/text-[11px] 总数 ~1555 处
 * - rounded-full / Badge ~1473 处
 * - 硬编码 Tailwind 不会自动吃 CSS token · 必须通过组件取代
 *
 * 4 个组件:
 * 1. ReadableTable        · 表格列分级 + comfortable 行高 + "显示更多列" toggle
 * 2. BadgeOverflowGroup   · 一行最多 2 彩色 + "+N" overflow popover
 * 3. InfoLegendPopover    · 图例/规则默认收起 + button 触发
 * 4. ExpandableMeta       · 长 remark/reason/description 默认折叠 + 点开看全
 *
 * 配套 CSS token(src/styles/density.css):
 * - --info-primary/secondary/tertiary · 字号/字重/颜色三层
 * - --row-comfortable-* · 阅读密集页 48px 行高
 * - --row-compact-* · 工作台 36px 行高
 * - .text-info-primary/secondary/tertiary 工具类(直接 className 用)
 * - .row-comfortable / .row-compact 工具类
 */

export { ReadableTable } from './ReadableTable';
export type { ReadableColumn, ColumnTier, ReadableTableProps } from './ReadableTable';

export { BadgeOverflowGroup } from './BadgeOverflowGroup';
export type { BadgeItem, BadgeColor, BadgeOverflowGroupProps } from './BadgeOverflowGroup';

export { InfoLegendPopover } from './InfoLegendPopover';
export type { LegendItem, LegendGroup, InfoLegendPopoverProps } from './InfoLegendPopover';

export { InfoBadgeRow } from './InfoBadgeRow';
export type { InfoBadgeItem, InfoBadgeRowProps } from './InfoBadgeRow';

export { ExpandableMeta } from './ExpandableMeta';
export type { ExpandableMetaProps } from './ExpandableMeta';
