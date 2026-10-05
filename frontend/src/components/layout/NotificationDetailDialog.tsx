/**
 * NotificationDetailDialog — 通知详情弹窗(工单 2026-07-29 T3 §3.2)
 *
 * 修的真因(不是"没做弹窗"这么简单):
 *   旧 NotificationBell 点击列表项时,**有 link 就直接跳走、没 link 就只标已读什么都不发生**
 *   —— 所以"点了没反应"和"跳走了但没看到完整内容"是同一个缺口的两面;
 *   而正文在列表里被 `line-clamp-3` 截断,Owner 截图里"…请在效果监测页面查看…"后面
 *   看不到的就是这一刀。
 *
 * 现在:点击一律先开弹窗展示**完整正文**(不截断),跳转收进弹窗底部的按钮里。
 *
 * 🔴 扣费类通知在本组件内只读渲染 + 跳转,**不发起任何写请求**。
 */

import { createPortal } from 'react-dom';
import { ExternalLink, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  type FeedItem, CATEGORY_LABELS, formatFeedFullTime, jumpLabel, mayOpenLink,
} from '@/lib/notificationFeed';

interface Props {
  item: FeedItem | null;
  isAdmin: boolean;
  onClose: () => void;
  onNavigate: (link: string, item: FeedItem) => void;
}

export function NotificationDetailDialog({ item, isAdmin, onClose, onNavigate }: Props) {
  if (!item) return null;
  const canJump = mayOpenLink(item.link, isAdmin);

  return createPortal(
    <div
      className="fixed inset-0 z-[300] flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label={`通知详情: ${item.title}`}
    >
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div className="relative mx-4 w-full max-w-2xl overflow-hidden rounded-2xl border border-border bg-card shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 className="text-base font-semibold break-words">{item.title}</h2>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="rounded bg-secondary px-1.5 py-0.5">
                {CATEGORY_LABELS[item.category]}
              </span>
              {item.business_no && <span>业务单号 {item.business_no}</span>}
              <span>{formatFeedFullTime(item.created_at)}</span>
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="关闭通知详情"
            className="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-lg hover:bg-secondary"
          >
            <X className="size-4" />
          </button>
        </div>

        {/* 正文:完整落库文案,whitespace-pre-wrap 不截断、不 line-clamp */}
        <div className="max-h-[60vh] overflow-y-auto px-5 py-4">
          {item.content ? (
            <p
              data-testid="notification-detail-content"
              className="whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground/90"
            >
              {item.content}
            </p>
          ) : (
            <p className="py-6 text-center text-sm text-muted-foreground">该通知没有更多内容</p>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
          {canJump && (
            <Button size="sm" onClick={() => onNavigate(item.link, item)}>
              <ExternalLink className="mr-1 size-3.5" />
              {jumpLabel(item)}
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={onClose}>关闭</Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
