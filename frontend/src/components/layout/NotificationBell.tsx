/**
 * NotificationBell — 站内信铃铛（三色分级 + 抖动 + 静音模式）
 *
 * 未读级别视觉:
 *   重要 → 红色数字 + 铃铛微抖
 *   普通(light/gentle) → 蓝色小圆点
 *   静默 → 无变化（通知中心可见）
 *   无未读 → 干干净净
 *
 * 点击通知：有 link 时整行跳转并标记已读；无 link 时仅标记已读
 * 静音模式：右键/长按铃铛弹出菜单
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { authFetch } from '@/lib/api';
import { AlertCircle, Bell, BellOff, Loader2, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useCEndPanel } from '@/context/CEndPanelContext';
import { WALLET_CHARGE_QUIET_EVENT } from '@/context/WalletContext';
import { useAuth } from '@/context/AuthContext';
import { NotificationDetailDialog } from './NotificationDetailDialog';
import {
  type FeedItem, type NotificationFilter, CATEGORY_LABELS, buildFeedUrl,
} from '@/lib/notificationFeed';

/** feed 条目 + C 端 geo_plan_ready 分屏用的 metadata */
type Notification = FeedItem & { metadata?: Record<string, unknown> };

interface UnreadByLevel {
  silent: number;
  light: number;
  gentle: number;
  important: number;
  total: number;
}

// 静音模式持久化 key
const MUTE_KEY = 'notification_mute_until';

function getMuteUntil(): number {
  const v = localStorage.getItem(MUTE_KEY);
  return v ? parseInt(v, 10) : 0;
}

function isMuted(): boolean {
  const until = getMuteUntil();
  if (until === -1) return true; // 永久静音
  if (until === 0) return false;
  return Date.now() < until;
}

function setMute(option: 'off' | '1h' | 'tomorrow' | 'forever') {
  if (option === 'off') {
    localStorage.removeItem(MUTE_KEY);
  } else if (option === '1h') {
    localStorage.setItem(MUTE_KEY, String(Date.now() + 3600000));
  } else if (option === 'tomorrow') {
    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    tomorrow.setHours(0, 0, 0, 0);
    localStorage.setItem(MUTE_KEY, String(tomorrow.getTime()));
  } else {
    localStorage.setItem(MUTE_KEY, '-1');
  }
}

export interface NotificationBellProps {
  /** 'agent' (默认,代理端) 走 navigate() 全屏跳 · 'c' (C 端) 走 openInPanel 分屏 · CTO-15.5 Phase 4 PLAN 05 Task 5.3 */
  userMode?: 'agent' | 'c';
}

export function NotificationBell({ userMode = 'agent' }: NotificationBellProps = {}) {
  const navigate = useNavigate();
  const location = useLocation();
  // C 端时有 panel(CEndPanelProvider) · 代理端 panel=null
  const panel = useCEndPanel();
  const [open, setOpen] = useState(false);
  const [muteMenu, setMuteMenu] = useState(false);
  const [muted, setMutedState] = useState(isMuted);
  const [unread, setUnread] = useState(0);
  const [byLevel, setByLevel] = useState<UnreadByLevel>({ silent: 0, light: 0, gentle: 0, important: 0, total: 0 });
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [unreadError, setUnreadError] = useState(false);
  const [actionError, setActionError] = useState('');
  // [P5b] 安静模式("安静一点"扣费偏好):扣费时不弹 toast,只在铃铛亮一个蓝点提示
  const [quietPing, setQuietPing] = useState(false);
  // [工单 2026-07-29 T3] 两类分组 + 详情弹窗
  const { user } = useAuth();
  const [category, setCategory] = useState<NotificationFilter>('all');
  const [byCategory, setByCategory] = useState<{ system: number; billing: number }>({ system: 0, billing: 0 });
  const [detail, setDetail] = useState<Notification | null>(null);
  const dropRef = useRef<HTMLDivElement>(null);
  const muteRef = useRef<HTMLDivElement>(null);
  const unreadAbortRef = useRef<AbortController | null>(null);
  const unreadInFlightRef = useRef<Promise<void> | null>(null);

  // 轮询未读数（60秒一次）
  const fetchUnread = useCallback((): Promise<void> => {
    if (document.visibilityState === 'hidden') return Promise.resolve();
    if (unreadInFlightRef.current) return unreadInFlightRef.current;
    const controller = new AbortController();
    unreadAbortRef.current = controller;
    const request = (async () => {
    try {
      const r = await authFetch('/api/user/notifications/unread-count', { signal: controller.signal });
      if (!r.ok) throw new Error(`notification unread request failed: ${r.status}`);
      const d = await r.json();
      if (d.status !== 'success') throw new Error('notification unread response was not successful');
      setUnreadError(false);
      setUnread(d.count);
      if (d.by_level) setByLevel(d.by_level);
      if (d.by_category) setByCategory(d.by_category);
    } catch (error) {
      if (controller.signal.aborted) return;
      console.error('通知未读数加载失败', error);
      setUnreadError(true);
    } finally {
      if (unreadAbortRef.current === controller) unreadAbortRef.current = null;
    }
    })();
    unreadInFlightRef.current = request;
    void request.finally(() => {
      if (unreadInFlightRef.current === request) unreadInFlightRef.current = null;
    });
    return request;
  }, []);

  useEffect(() => {
    // The badge is auxiliary UI. Keep its behavior, but yield the initial
    // connection slots to the current route chunk and page-critical reads.
    const initialTimer = window.setTimeout(() => { void fetchUnread(); }, 10_000);
    const t = setInterval(() => { if (document.visibilityState === 'visible') void fetchUnread(); }, 60000);
    // 静音状态定时刷新
    const mt = setInterval(() => setMutedState(isMuted()), 30000);
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') unreadAbortRef.current?.abort();
      else void fetchUnread();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      unreadAbortRef.current?.abort();
      window.clearTimeout(initialTimer);
      clearInterval(t);
      clearInterval(mt);
    };
  }, [fetchUnread]);

  // [P5b] "安静一点"偏好:扣费发生时只亮蓝点(不弹 toast)· 打开铃铛即消点
  useEffect(() => {
    const onQuietCharge = () => setQuietPing(true);
    window.addEventListener(WALLET_CHARGE_QUIET_EVENT, onQuietCharge);
    return () => window.removeEventListener(WALLET_CHARGE_QUIET_EVENT, onQuietCharge);
  }, []);

  // 打开下拉时加载列表(feed:系统消息 + 扣费通知两路合并,同业务单号终态已在后端去重)
  const loadList = useCallback((next: NotificationFilter = category) => {
    setLoading(true);
    setLoadError(false);
    setActionError('');
    authFetch(buildFeedUrl({ category: next, limit: 10, offset: 0 }))
      .then(async r => {
        if (!r.ok) throw new Error(`notification list request failed: ${r.status}`);
        const d = await r.json();
        if (d.status !== 'success') throw new Error('notification list response was not successful');
        setNotifications(d.items || []);
      })
      .catch(error => {
        console.error('通知列表加载失败', error);
        setLoadError(true);
      })
      .finally(() => setLoading(false));
  }, [category]);

  const switchCategory = (next: NotificationFilter) => {
    setCategory(next);
    loadList(next);
  };

  const toggle = () => {
    if (!open) loadList(category);
    setOpen(!open);
    setMuteMenu(false);
    setQuietPing(false); // [P5b] 点开铃铛即消"安静扣费"蓝点
  };

  // 点击外部关闭
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropRef.current && !dropRef.current.contains(e.target as Node)) {
        setOpen(false);
        setMuteMenu(false);
      }
    };
    if (open || muteMenu) document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open, muteMenu]);

  const markRead = async (id: string) => {
    setActionError('');
    try {
      // 扣费类走同一端点,后端只推"已读水位"这张纯 UI 状态表,资金表零写入。
      const response = await authFetch('/api/user/notifications/feed/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: id }),
      });
      if (!response.ok) throw new Error(`notification read request failed: ${response.status}`);
      const body = await response.json();
      if (body.status !== 'success') throw new Error('notification read response was not successful');
      setNotifications(prev => prev.map(n => n.id === id ? { ...n, is_read: true } : n));
      await fetchUnread();
    } catch (error) {
      console.error('通知已读状态更新失败', error);
      setActionError('通知状态更新失败，请重试');
    }
  };

  const markAllRead = async () => {
    setActionError('');
    try {
      const response = await authFetch(`/api/user/notifications/read-all?category=${category}`, { method: 'POST' });
      if (!response.ok) throw new Error(`notification read-all request failed: ${response.status}`);
      const body = await response.json();
      if (body.status !== 'success') throw new Error('notification read-all response was not successful');
      setNotifications(prev => prev.map(n => ({ ...n, is_read: true })));
      await fetchUnread();
    } catch (error) {
      console.error('全部已读状态更新失败', error);
      setActionError('通知状态更新失败，请重试');
    }
  };

  // [工单 2026-07-29 T3 §3.2] 点击一律先开详情弹窗(展示完整正文),跳转收进弹窗底部。
  // 旧行为:有 link 直接跳走 / 无 link 完全无响应 —— 两者都让用户看不到被
  // line-clamp-3 截掉的正文。
  const handleNotificationClick = (n: Notification) => {
    setDetail(n);
    if (!n.is_read) void markRead(n.id);
  };

  const handleNotificationKeyDown = (e: React.KeyboardEvent, n: Notification) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    handleNotificationClick(n);
  };

  const handleNavigate = (link: string, n?: Notification) => {
    if (n && !n.is_read) void markRead(n.id);
    // [CTO-15.5 PLAN 05 Task 5.3] C 端 geo_plan_ready 类型走 panel.openInPanel
    // · 其他类型走原 navigate 保持兼容
    // · 代理端 userMode='agent' 始终走 navigate (默认行为不变)
    if (userMode === 'c') {
      // [WO_260] 原 geo_plan_ready 通知 → 在 C 端分屏里开老 C 端方案页的特判已删:那条路由随 E3 删域;
      //   本分支只在 userMode='c'(废弃的 C 端壳才传)时走,在役页头默认 'agent',不受影响。
      // 其他 C 端通知: 有 panel 就 openInPanel,否则 fallback navigate
      if (panel && link && link.startsWith('/')) {
        panel.openInPanel(link);
        setDetail(null);
        setOpen(false);
        return;
      }
    }
    navigate(link);
    setDetail(null);
    setOpen(false);
  };

  // 右键打开静音菜单
  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setMuteMenu(!muteMenu);
    setOpen(false);
  };

  const handleMuteOption = (option: 'off' | '1h' | 'tomorrow' | 'forever') => {
    setMute(option);
    setMutedState(isMuted());
    setMuteMenu(false);
  };

  const formatTime = (t: string) => {
    if (!t) return '';
    const d = new Date(t);
    const now = new Date();
    const diff = now.getTime() - d.getTime();
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return `${Math.floor(diff / 60000)}分钟前`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}小时前`;
    return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
  };

  // 计算铃铛状态
  const hasImportant = byLevel.important > 0;
  const hasNormal = byLevel.light + byLevel.gentle > 0;
  const displayUnread = byLevel.total - byLevel.silent; // 静默不计入显示

  // 未读圆点颜色（按通知级别）
  const getLevelDotColor = (level: string) => {
    switch (level) {
      case 'important': return 'bg-red-500';
      case 'gentle': return 'bg-blue-400';
      case 'light': return 'bg-zinc-400';
      default: return 'bg-zinc-600';
    }
  };

  return (
    <div className="relative" ref={dropRef}>
      {/* 铃铛按钮 */}
      <button
        onClick={toggle}
        onContextMenu={handleContextMenu}
        className="relative inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg hover:bg-secondary transition-colors sm:min-h-0 sm:min-w-0 sm:p-2"
        aria-label="打开通知中心"
        title={unreadError ? '通知状态暂时无法加载，点击重试' : muted ? '通知已静音（右键取消）' : '站内信（右键静音）'}
      >
        {muted ? (
          <BellOff className="size-4 text-muted-foreground/50" />
        ) : (
          <Bell className={cn(
            'size-4 text-muted-foreground transition-transform',
            hasImportant && !muted && 'animate-[bell-shake_0.5s_ease-in-out_3]'
          )} />
        )}

        {/* important 红点不受静音影响；静音只抑制横幅，不隐藏未读事实。 */}
        {(hasImportant || (!muted && (displayUnread > 0 || quietPing))) && (
          hasImportant ? (
            // 红色数字（重要）
            <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 rounded-full bg-red-500 text-[9px] text-white flex items-center justify-center font-bold px-1">
              {displayUnread > 9 ? '9+' : displayUnread}
            </span>
          ) : (
            // 蓝色小圆点（普通未读 / P5b 安静扣费提示）
            <span className="absolute top-1 right-1 size-2 rounded-full bg-blue-400" />
          )
        )}
        {unreadError && !hasImportant && (
          <AlertCircle className="absolute -right-1 -top-1 size-3.5 text-amber-500" aria-hidden="true" />
        )}
      </button>

      {/* 静音菜单 */}
      {muteMenu && (
        <div ref={muteRef} className="absolute right-0 top-full mt-2 w-44 rounded-xl border border-border bg-card shadow-2xl z-50 py-1 overflow-hidden">
          {[
            { key: 'off' as const, label: '正常接收', icon: '🔔', active: !muted },
            { key: '1h' as const, label: '静音 1 小时', icon: '🔕', active: false },
            { key: 'tomorrow' as const, label: '静音到明天', icon: '🔕', active: false },
            { key: 'forever' as const, label: '永久静音', icon: '🔕', active: false },
          ].map(opt => (
            <button
              key={opt.key}
              className={cn(
                'w-full text-left px-3 py-2 text-sm hover:bg-secondary/50 transition-colors flex items-center gap-2',
                opt.active && 'text-primary font-medium'
              )}
              onClick={() => handleMuteOption(opt.key)}
            >
              <span className="text-xs">{opt.icon}</span>
              {opt.label}
              {opt.active && <span className="ml-auto text-xs text-primary">←</span>}
            </button>
          ))}
        </div>
      )}

      {/* 下拉面板 */}
      {open && (
        <div className="fixed inset-x-2 top-14 max-h-[calc(100dvh-4rem)] rounded-xl border border-border bg-card shadow-2xl z-50 flex flex-col overflow-hidden sm:absolute sm:inset-x-auto sm:right-0 sm:top-full sm:mt-2 sm:w-80 sm:max-h-[420px]">
          {/* 头部 */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
            <span className="text-sm font-medium">通知</span>
            {unread > 0 && (
              <button onClick={markAllRead} className="text-[11px] text-primary hover:underline">
                全部已读
              </button>
            )}
          </div>

          {/* [工单 2026-07-29 T3 §3.1] 两类分组 tab · 各自未读数 */}
          <div className="flex items-center gap-1 border-b border-border px-2 py-1.5">
            {(['all', 'system', 'billing'] as NotificationFilter[]).map(key => {
              const count = key === 'all'
                ? byCategory.system + byCategory.billing
                : byCategory[key];
              return (
                <button
                  key={key}
                  onClick={() => switchCategory(key)}
                  aria-pressed={category === key}
                  className={cn(
                    'rounded-md px-2 py-1 text-[11px] transition-colors',
                    category === key ? 'bg-secondary font-medium' : 'text-muted-foreground hover:bg-secondary/50',
                  )}
                >
                  {CATEGORY_LABELS[key]}
                  {count > 0 && <span className="ml-1 text-primary">{count > 99 ? '99+' : count}</span>}
                </button>
              );
            })}
          </div>

          {/* 列表 */}
          <div className="flex-1 overflow-y-auto">
            {actionError && (
              <div role="alert" className="border-b border-border bg-destructive/10 px-4 py-2 text-xs text-destructive">
                {actionError}
              </div>
            )}
            {loading ? (
              <div className="flex justify-center py-8"><Loader2 className="size-4 animate-spin text-muted-foreground" /></div>
            ) : loadError ? (
              <div className="space-y-3 px-4 py-7 text-center">
                <div className="text-sm text-muted-foreground">通知暂时无法加载</div>
                <button className="text-xs text-primary hover:underline" onClick={() => loadList(category)}>重试</button>
              </div>
            ) : notifications.length === 0 ? (
              <div className="text-center py-8 text-sm text-muted-foreground">暂无通知</div>
            ) : (
              notifications.map(n => (
                <div
                  key={n.id}
                  className={cn(
                    'flex cursor-pointer items-start gap-3 px-4 py-3 border-b border-border/50 transition-colors hover:bg-secondary/30',
                    !n.is_read && 'bg-primary/[0.03]',
                  )}
                  onClick={() => handleNotificationClick(n)}
                  onKeyDown={e => handleNotificationKeyDown(e, n)}
                  role="button"
                  tabIndex={0}
                  aria-label={`查看通知详情: ${n.title}`}
                >
                  {/* 未读圆点 — 按级别着色 */}
                  <div className="mt-1.5 shrink-0">
                    {!n.is_read ? (
                      <div className={cn('size-2 rounded-full', getLevelDotColor(n.level))} />
                    ) : (
                      <div className="size-2" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm leading-snug">{n.title}</div>
                    {n.content && <div className="mt-1 line-clamp-3 break-words text-xs text-muted-foreground">{n.content}</div>}
                    <div className="text-[10px] text-muted-foreground mt-1">{formatTime(n.created_at)}</div>
                  </div>
                  {(n.link || n.type === 'geo_plan_ready') && (
                    <button
                      className="-my-2 -mr-2 mt-[-7px] inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-lg hover:bg-secondary transition-colors"
                      aria-label={`打开通知链接: ${n.title}`}
                      onClick={e => { e.stopPropagation(); handleNavigate(n.link || '', n); }}
                    >
                      <ChevronRight className="size-3.5 text-muted-foreground" />
                    </button>
                  )}
                </div>
              ))
            )}
          </div>

          {/* 底部 */}
          <button
            className="px-4 py-2.5 text-xs text-primary hover:bg-secondary/50 border-t border-border text-center transition-colors"
            onClick={() => { navigate('/notifications'); setOpen(false); }}
          >
            查看全部通知
          </button>
        </div>
      )}

      {/* [工单 2026-07-29 T3 §3.2] 详情弹窗:完整正文 + 跳转入口 */}
      <NotificationDetailDialog
        item={detail}
        isAdmin={user?.is_admin === true}
        onClose={() => setDetail(null)}
        onNavigate={(link, item) => handleNavigate(link, item as Notification)}
      />
    </div>
  );
}
