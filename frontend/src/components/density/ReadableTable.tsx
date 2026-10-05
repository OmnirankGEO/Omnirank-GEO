/**
 * ReadableTable · 阅读密集表格 · CTO-15.23 Density System v1
 *
 * 解决:整站表格 8-11 列 + 行高 32px + 字号 11px 扁平 = "文字密密麻麻"
 *
 * 设计:
 * - 默认只露 primary + 至多 4 secondary 列(老板可见数据)
 * - "显示更多列" toggle 展开 tertiary 列(权重 / 出稿时间 / 新闻源等技术列)
 * - 行高 comfortable 48px(桌面)/ 56px(移动)· 字号 14-15 主 / 12-13 次 / 11 辅
 * - 列宽用 --table-* token · grid-template-columns 自动 1-N 切换
 * - 行点击可选 onRowClick(展开详情走 ExpandableMeta)
 *
 * 用法:
 *   <ReadableTable
 *     columns={[
 *       { key: 'name', label: '媒体名称', tier: 'primary', minWidth: 200 },
 *       { key: 'price', label: '价格', tier: 'secondary', minWidth: 80, align: 'right' },
 *       { key: 'rate', label: '收录率', tier: 'secondary', minWidth: 64 },
 *       { key: 'avgTime', label: '出稿', tier: 'tertiary', minWidth: 80 },
 *     ]}
 *     rows={data}
 *     renderCell={(row, col) => row[col.key]}
 *     onRowClick={(r) => ...}
 *   />
 */

import { useState, type ReactNode } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

export type ColumnTier = 'primary' | 'secondary' | 'tertiary';

export interface ReadableColumn<T> {
  key: string;
  label: string;
  tier: ColumnTier;
  minWidth?: number;
  align?: 'left' | 'right' | 'center';
  /** 隐藏 header label(checkbox 列 / 操作列等) */
  hideHeader?: boolean;
  /** 列自身的 cell 渲染覆盖(否则走外层 renderCell) */
  render?: (row: T) => ReactNode;
}

export interface ReadableTableProps<T> {
  columns: ReadableColumn<T>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  renderCell?: (row: T, col: ReadableColumn<T>) => ReactNode;
  /** 行点击(可选)· 例:导航 / 展开详情 */
  onRowClick?: (row: T) => void;
  /** 是否启用 "显示更多列" toggle(默认 true · 关掉则只示 primary+secondary) */
  showColumnToggle?: boolean;
  /** secondary 列默认最多显示几个 · 超出走 tertiary 一起折叠 · 默认 4 · 防"secondary 6 列密度回来"(Codex 1.1) */
  maxSecondaryVisible?: number;
  /** 行密度 · 默认 comfortable(阅读密集)· compact 给工作台 */
  density?: 'comfortable' | 'compact';
  /** 空数据文案 */
  emptyText?: string;
  className?: string;
}

function buildGridTemplate<T>(
  visibleCols: ReadableColumn<T>[],
): string {
  return visibleCols
    .map((c) => `minmax(${c.minWidth ?? 0}px, ${c.tier === 'primary' ? '2fr' : '1fr'})`)
    .join(' ');
}

function pickVisibleColumns<T>(
  columns: ReadableColumn<T>[],
  showHidden: boolean,
  maxSecondaryVisible: number,
): { visible: ReadableColumn<T>[]; totalHiddenCount: number } {
  const primary = columns.filter((c) => c.tier === 'primary');
  const secondary = columns.filter((c) => c.tier === 'secondary');
  const tertiary = columns.filter((c) => c.tier === 'tertiary');
  const secondaryVisible = showHidden ? secondary : secondary.slice(0, maxSecondaryVisible);
  const secondaryOverflow = secondary.slice(maxSecondaryVisible);
  const tertiaryVisible = showHidden ? tertiary : [];
  const visible = [...primary, ...secondaryVisible, ...tertiaryVisible];
  /* Codex Phase 1.2 修:totalHiddenCount 不随 showHidden 归零 · 防展开后按钮消失/labelf "收起 0 列" */
  const totalHiddenCount = secondaryOverflow.length + tertiary.length;
  return { visible, totalHiddenCount };
}

export function ReadableTable<T>({
  columns,
  rows,
  rowKey,
  renderCell,
  onRowClick,
  showColumnToggle = true,
  maxSecondaryVisible = 4,
  density = 'comfortable',
  emptyText = '暂无数据',
  className,
}: ReadableTableProps<T>) {
  const [showHidden, setShowHidden] = useState(false);

  const { visible: visibleCols, totalHiddenCount } = pickVisibleColumns(
    columns,
    showHidden,
    maxSecondaryVisible,
  );
  const gridTemplate = buildGridTemplate(visibleCols);
  const rowClass = density === 'comfortable' ? 'row-comfortable' : 'row-compact';

  return (
    <div className={cn('w-full', className)}>
      {/* "显示更多列" toggle · 始终用 totalHiddenCount 决定显隐(Codex Phase 1.2 修)*/}
      {showColumnToggle && totalHiddenCount > 0 && (
        <div className="mb-2 flex justify-end">
          <button
            type="button"
            onClick={() => setShowHidden(!showHidden)}
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors px-2 py-1 rounded hover:bg-muted/40"
          >
            {showHidden ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            {showHidden ? `收起 ${totalHiddenCount} 列详情` : `显示更多列(${totalHiddenCount})`}
          </button>
        </div>
      )}

      {/* Codex 1.1:外层 overflow-x-auto 防 fit-content 撑爆页面横向 */}
      <div className="w-full overflow-x-auto">
        {/* 表头 */}
        <div
          className="grid gap-3 border-b border-border bg-muted/30 px-4 py-2.5 text-info-tertiary font-semibold uppercase tracking-wider"
          style={{ gridTemplateColumns: gridTemplate, minWidth: 'fit-content' }}
        >
          {visibleCols.map((col) => (
            <div
              key={col.key}
              className={cn(
                'min-w-0',
                col.align === 'right' && 'text-right',
                col.align === 'center' && 'text-center',
              )}
            >
              {col.hideHeader ? <span className="sr-only">{col.label}</span> : col.label}
            </div>
          ))}
        </div>

        {/* 数据行 */}
        <div className="divide-y divide-border/50">
          {rows.length === 0 ? (
            <div className="py-12 text-center text-info-tertiary">{emptyText}</div>
          ) : (
            rows.map((row) => (
              <div
                key={rowKey(row)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={cn(
                  'grid gap-3 items-center',
                  rowClass,
                  onRowClick && 'cursor-pointer hover:bg-muted/30 transition-colors',
                )}
                style={{ gridTemplateColumns: gridTemplate, minWidth: 'fit-content' }}
              >
                {visibleCols.map((col) => (
                  <div
                    key={col.key}
                    className={cn(
                      'min-w-0',
                      col.tier === 'primary' && 'text-info-primary break-words',
                      col.tier === 'secondary' && 'text-info-secondary break-words',
                      col.tier === 'tertiary' && 'text-info-tertiary break-words',
                      col.align === 'right' && 'text-right',
                      col.align === 'center' && 'text-center',
                    )}
                  >
                    {col.render ? col.render(row) : renderCell ? renderCell(row, col) : null}
                  </div>
                ))}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
