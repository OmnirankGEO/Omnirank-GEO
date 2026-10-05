/**
 * NotificationCenter — 通知历史页(工单 2026-07-29 T3 §3.3)
 *
 * 变更:
 *   · 数据源换成 /api/user/notifications/feed —— 系统消息 + 扣费通知两类;
 *   · 按时间倒序 + **滚动加载更多**(不再只取最近 100 条);
 *   · 按类别筛选(系统消息 / 扣费通知)+ 未读筛选;
 *   · 点击开详情弹窗,正文完整不截断,跳转在弹窗里;
 *   · **不做删除**(通知是记录,尤其扣费类要可追溯)—— 要清爽用"全部已读"。
 *
 * 🔴 扣费通知在本页只读渲染与跳转,不触发任何扣费/退费写操作。
 */

import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { authFetch } from '@/lib/api';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Bell, Check, CheckCheck, ChevronRight, Loader2, Inbox } from 'lucide-react';
import { cn } from '@/lib/utils';
import { ActionableAlert } from '@/components/ui/actionable-alert';
import { useAuth } from '@/context/AuthContext';
import { NotificationDetailDialog } from '@/components/layout/NotificationDetailDialog';
import {
  type FeedItem, type NotificationFilter, CATEGORY_LABELS, buildFeedUrl,
  formatFeedFullTime, mayOpenLink,
} from '@/lib/notificationFeed';

const PAGE_SIZE = 20;

const LEVEL_DOT_COLOR: Record<string, string> = {
  important: 'bg-red-500',
  gentle: 'bg-blue-400',
  light: 'bg-zinc-400',
  silent: 'bg-zinc-600',
};

export function NotificationCenter() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const [items, setItems] = useState<FeedItem[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [actionError, setActionError] = useState('');
  const [category, setCategory] = useState<NotificationFilter>('all');
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [detail, setDetail] = useState<FeedItem | null>(null);

  const fetchPage = useCallback(async (offset: number) => {
    const response = await authFetch(
      buildFeedUrl({ category, limit: PAGE_SIZE, offset, unreadOnly }),
    );
    if (!response.ok) throw new Error(`notification feed request failed: ${response.status}`);
    const body = await response.json();
    if (body.status !== 'success') throw new Error('notification feed response was not successful');
    return body as { items: FeedItem[]; total: number; has_more: boolean };
  }, [category, unreadOnly]);

  const load = useCallback(() => {
    setLoading(true);
    setLoadError(false);
    setActionError('');
    fetchPage(0)
      .then(page => {
        setItems(page.items || []);
        setTotal(page.total || 0);
        setHasMore(Boolean(page.has_more));
      })
      .catch(error => {
        console.error('通知中心加载失败', error);
        setLoadError(true);
      })
      .finally(() => setLoading(false));
  }, [fetchPage]);

  useEffect(load, [load]);

  const loadMore = async () => {
    setLoadingMore(true);
    try {
      const page = await fetchPage(items.length);
      setItems(prev => [...prev, ...(page.items || [])]);
      setTotal(page.total || 0);
      setHasMore(Boolean(page.has_more));
    } catch (error) {
      console.error('加载更多通知失败', error);
      setActionError('加载更多失败，请重试');
    } finally {
      setLoadingMore(false);
    }
  };

  const markRead = async (id: string) => {
    setActionError('');
    try {
      const response = await authFetch('/api/user/notifications/feed/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: id }),
      });
      if (!response.ok) throw new Error(`notification read request failed: ${response.status}`);
      const body = await response.json();
      if (body.status !== 'success') throw new Error('notification read response was not successful');
      setItems(prev => prev.map(n => (n.id === id ? { ...n, is_read: true } : n)));
    } catch (error) {
      console.error('通知已读状态更新失败', error);
      setActionError('通知状态更新失败，请重试');
    }
  };

  const markAllRead = async () => {
    setActionError('');
    try {
      const response = await authFetch(
        `/api/user/notifications/read-all?category=${category}`, { method: 'POST' },
      );
      if (!response.ok) throw new Error(`notification read-all request failed: ${response.status}`);
      const body = await response.json();
      if (body.status !== 'success') throw new Error('notification read-all response was not successful');
      setItems(prev => prev.map(n => ({ ...n, is_read: true })));
    } catch (error) {
      console.error('全部已读状态更新失败', error);
      setActionError('通知状态更新失败，请重试');
    }
  };

  const openDetail = (item: FeedItem) => {
    setDetail(item);
    if (!item.is_read) void markRead(item.id);
  };

  const unreadCount = items.filter(n => !n.is_read).length;

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4 sm:p-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Bell className="size-5" />
          <h1 className="text-xl font-bold">通知中心</h1>
          {unreadCount > 0 && <Badge>{unreadCount} 未读</Badge>}
        </div>
        {unreadCount > 0 && (
          <Button variant="outline" size="sm" onClick={markAllRead}>
            <CheckCheck className="mr-1 size-3.5" /> 全部已读
          </Button>
        )}
      </div>

      {/* 筛选:类别 + 未读 */}
      <div className="flex flex-wrap gap-2">
        {(['all', 'system', 'billing'] as NotificationFilter[]).map(key => (
          <Button
            key={key}
            variant={category === key ? 'default' : 'outline'}
            size="sm"
            aria-pressed={category === key}
            onClick={() => setCategory(key)}
          >
            {CATEGORY_LABELS[key]}
          </Button>
        ))}
        <Button
          variant={unreadOnly ? 'default' : 'outline'}
          size="sm"
          aria-pressed={unreadOnly}
          onClick={() => setUnreadOnly(v => !v)}
        >
          只看未读
        </Button>
      </div>

      {actionError && (
        <ActionableAlert
          contract={{ confirmStatus: '/api/user/notifications/feed/read' }}
          title={actionError}
          description="通知本身未丢失；刷新状态后可再次确认。"
          className="border-destructive/30 bg-destructive/10 text-destructive"
        />
      )}

      {loading ? (
        <div className="flex justify-center py-16"><Loader2 className="size-6 animate-spin text-muted-foreground" /></div>
      ) : loadError ? (
        <ActionableAlert
          contract={{ action: 'reload_notifications', target: '/api/user/notifications/feed', permission: 'authenticated', recovery: 'retry_after_network_recovers' }}
          title="通知暂时无法加载"
          description="请检查网络后重试，已有通知不会丢失。"
          className="border-amber-500/30 bg-amber-500/10"
          actionLabel="重新加载"
          onAction={load}
        />
      ) : items.length === 0 ? (
        <Card>
          <CardContent className="py-16 text-center">
            <Inbox className="mx-auto mb-4 size-12 text-muted-foreground/30" />
            <h3 className="mb-1 text-lg font-medium">{unreadOnly ? '没有未读通知' : '暂无通知'}</h3>
            <p className="text-sm text-muted-foreground">系统通知、发布状态、算力变动都会出现在这里</p>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="space-y-1">
            {items.map(n => (
              <div
                key={n.id}
                role="button"
                tabIndex={0}
                aria-label={`查看通知详情: ${n.title}`}
                className={cn(
                  'flex cursor-pointer items-start gap-3 rounded-lg border p-4 transition-colors',
                  !n.is_read ? 'border-primary/20 bg-primary/[0.03]' : 'border-border hover:bg-secondary/30',
                )}
                onClick={() => openDetail(n)}
                onKeyDown={e => {
                  if (e.key !== 'Enter' && e.key !== ' ') return;
                  e.preventDefault();
                  openDetail(n);
                }}
              >
                <div className="mt-1 shrink-0">
                  {!n.is_read ? (
                    <div className={cn('size-2.5 rounded-full', LEVEL_DOT_COLOR[n.level] || 'bg-blue-400')} />
                  ) : (
                    <Check className="size-3.5 text-muted-foreground/30" />
                  )}
                </div>

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{n.title}</span>
                    <Badge variant="secondary" className="px-1.5 py-0 text-[10px]">
                      {CATEGORY_LABELS[n.category]}
                    </Badge>
                  </div>
                  <span className="mt-1.5 block text-[11px] text-muted-foreground">
                    {formatFeedFullTime(n.created_at)}
                  </span>
                  {n.content && (
                    <p className="mt-2 line-clamp-2 break-words text-sm text-muted-foreground">{n.content}</p>
                  )}
                </div>

                {mayOpenLink(n.link, user?.is_admin === true) && (
                  <button
                    className="mt-0 inline-flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-lg transition-colors hover:bg-secondary"
                    aria-label={`打开通知链接: ${n.title}`}
                    onClick={e => {
                      e.stopPropagation();
                      if (!n.is_read) void markRead(n.id);
                      navigate(n.link);
                    }}
                  >
                    <ChevronRight className="size-4 text-muted-foreground" />
                  </button>
                )}
              </div>
            ))}
          </div>

          <div className="pt-2 text-center">
            {hasMore ? (
              <Button variant="outline" size="sm" onClick={loadMore} disabled={loadingMore}>
                {loadingMore ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : null}
                加载更多
              </Button>
            ) : (
              <span className="text-xs text-muted-foreground">共 {total} 条 · 已到底</span>
            )}
          </div>
        </>
      )}

      <NotificationDetailDialog
        item={detail}
        isAdmin={user?.is_admin === true}
        onClose={() => setDetail(null)}
        onNavigate={(link, item) => {
          setDetail(null);
          if (!item.is_read) void markRead(item.id);
          navigate(link);
        }}
      />
    </div>
  );
}
