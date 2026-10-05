import { Button } from '@/components/ui/button';
import {
  CONFLICT_REASON_HINTS, CONFLICT_REASON_LABELS, groupConflictsByReason,
  type DuplicateOrderBlock,
} from '@/contracts/duplicateOrderConflict';

interface Props {
  block: DuplicateOrderBlock;
  /** 剔除后还剩几个「文章×媒体」组合。0 = 空单,不给剔除按钮。 */
  remainingAfterRemoval: number;
  disabled?: boolean;
  onRemoveConflicts?: () => void;
}

/**
 * 批量投放冲突「处理窗口」。
 *
 * 🔴 刻意抽成**独立组件**而不是写在 `PublishRiskConfirmDialog` 里:
 *    Radix `Dialog` 走 Portal,`renderToStaticMarkup` 渲不出内容 → 断言只能退回
 *    去打源码字符串,而源码串断言换皮即绕过、重构就误伤。抽出来之后锁能断在
 *    **真实渲染结果**上(与既有 `StrictPresubmitPanel` 同一先例)。
 *
 * 本组件不做任何判定:该拦谁由后端预检决定,这里只呈现 + 提供出口。
 */
export function DuplicateConflictPanel({
  block,
  remainingAfterRemoval,
  disabled = false,
  onRemoveConflicts,
}: Props) {
  const conflicts = block?.conflicts ?? [];
  if (conflicts.length === 0) return null;
  const wouldBeEmpty = remainingAfterRemoval <= 0;

  return (
    <div
      data-testid="duplicate-conflict-panel"
      data-conflict-count={conflicts.length}
      data-remaining-after-removal={remainingAfterRemoval}
      className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm"
    >
      <div className="font-medium text-foreground">
        这批里有 {conflicts.length} 个组合没法提交，本次未产生任何费用。
      </div>

      {groupConflictsByReason(conflicts).map(group => (
        <div key={group.reason} className="mt-2.5">
          <div className="text-xs font-medium text-foreground">
            {CONFLICT_REASON_LABELS[group.reason]}（{group.items.length}）
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {CONFLICT_REASON_HINTS[group.reason]}
          </p>
          <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
            {group.items.slice(0, 5).map(c => (
              <li key={`${c.articleId}:${c.mediaId}`}>
                · {c.articleTitle || `文章 ${c.articleId}`}
                <span className="mx-1">×</span>
                {c.mediaName || `媒体 ${c.mediaId}`}
              </li>
            ))}
            {group.items.length > 5 && (
              <li>· 其余 {group.items.length - 5} 个…</li>
            )}
          </ul>
        </div>
      ))}

      {wouldBeEmpty ? (
        <p className="mt-3 text-xs font-medium text-destructive">
          这批里所有组合都发不出去，没有可以继续投放的内容。请返回重新选择。
        </p>
      ) : (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-3 w-full"
          data-testid="remove-conflicts-button"
          disabled={disabled}
          onClick={onRemoveConflicts}
        >
          去掉这 {conflicts.length} 个冲突组合，继续投放其余 {remainingAfterRemoval} 个
        </Button>
      )}
    </div>
  );
}
