/**
 * NotificationBanner — 底部浮出横幅（温和级别通知）
 *
 * 功能:
 *   - 轮询最近通知，发现新的 gentle 级别通知时从底部滑入
 *   - 3秒后自动滑出
 *   - 最多同时显示 1 条（新的替换旧的）
 *   - suppress_on_pages: 用户在指定页面时跳过
 *   - 静音模式下不显示
 *   - 点击横幅可跳转（如果有 link）
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { authFetch } from '@/lib/api';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';

interface BannerNotification {
  id: number;
  title: string;
  link: string;
  level: string;
  metadata?: {
    suppress_on_pages?: string[];
    [key: string]: unknown;
  };
}

const MUTE_KEY = 'notification_mute_until';

function isMuted(): boolean {
  const v = localStorage.getItem(MUTE_KEY);
  if (!v) return false;
  const until = parseInt(v, 10);
  if (until === -1) return true;
  return Date.now() < until;
}

export function NotificationBanner() {
  const navigate = useNavigate();
  const location = useLocation();
  const [banner, setBanner] = useState<BannerNotification | null>(null);
  const [visible, setVisible] = useState(false);
  const lastSeenId = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();
  const requestAbortRef = useRef<AbortController | null>(null);
  const requestInFlightRef = useRef<Promise<void> | null>(null);
  const locationPathRef = useRef(location.pathname);
  locationPathRef.current = location.pathname;

  const checkNewNotifications = useCallback((): Promise<void> => {
    if (isMuted() || document.visibilityState === 'hidden') return Promise.resolve();
    if (requestInFlightRef.current) return requestInFlightRef.current;
    const controller = new AbortController();
    requestAbortRef.current = controller;
    const request = authFetch('/api/user/notifications?limit=5', { signal: controller.signal })
      .then(async r => {
        if (!r.ok) throw new Error(`notification banner request failed: ${r.status}`);
        return r.json();
      })
      .then(d => {
        if (d.status !== 'success' || !d.notifications?.length) return;

        // 找最新的未读 gentle/important 通知
        const newNotif = d.notifications.find(
          (n: BannerNotification & { is_read: boolean }) =>
            !n.is_read &&
            (n.level === 'gentle' || n.level === 'important') &&
            n.id > lastSeenId.current
        );

        if (!newNotif) return;

        lastSeenId.current = newNotif.id;

        // suppress_on_pages 检查
        const suppressPages = newNotif.metadata?.suppress_on_pages || [];
        const currentPath = locationPathRef.current;
        if (suppressPages.some((p: string) => currentPath.startsWith(p))) return;

        // 显示横幅
        setBanner(newNotif);
        setVisible(true);

        // 3秒后自动消失
        if (timerRef.current) clearTimeout(timerRef.current);
        timerRef.current = setTimeout(() => {
          setVisible(false);
          setTimeout(() => setBanner(null), 300); // 等动画完成
        }, 3000);
      })
      .catch(error => { if (!controller.signal.aborted) console.error('通知横幅检查失败', error); })
      .finally(() => {
        if (requestAbortRef.current === controller) requestAbortRef.current = null;
      });
    requestInFlightRef.current = request;
    void request.finally(() => {
      if (requestInFlightRef.current === request) requestInFlightRef.current = null;
    });
    return request;
  }, []);

  useEffect(() => {
    // 初始化 lastSeenId（避免显示旧通知）
    const initController = new AbortController();
    // Initialization only records the latest id; it is not required to render or
    // operate the current page, so let critical route reads use the first slots.
    const initTimer = window.setTimeout(() => {
      void authFetch('/api/user/notifications?limit=1', { signal: initController.signal })
      .then(async r => {
        if (!r.ok) throw new Error(`notification banner init failed: ${r.status}`);
        return r.json();
      })
      .then(d => {
        if (d.status === 'success' && d.notifications?.length) {
          lastSeenId.current = d.notifications[0].id;
        }
      })
      .catch(error => { if (!initController.signal.aborted) console.error('通知横幅初始化失败', error); });
    }, 10_000);

    // 每 30 秒检查新通知
    const t = setInterval(() => { if (document.visibilityState === 'visible') void checkNewNotifications(); }, 30000);
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') requestAbortRef.current?.abort();
      else void checkNewNotifications();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.clearTimeout(initTimer);
      initController.abort();
      requestAbortRef.current?.abort();
      clearInterval(t);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [checkNewNotifications]);

  const dismiss = () => {
    setVisible(false);
    setTimeout(() => setBanner(null), 300);
  };

  if (!banner) return null;

  return (
    <div
      className={cn(
        'fixed bottom-6 left-1/2 -translate-x-1/2 z-50 max-w-md w-[calc(100%-2rem)]',
        'transition-all duration-300 ease-out',
        visible
          ? 'opacity-100 translate-y-0'
          : 'opacity-0 translate-y-4 pointer-events-none'
      )}
    >
      <div
        className={cn(
          'flex items-center gap-3 px-4 py-3 rounded-xl border shadow-lg',
          'bg-card/95 backdrop-blur-sm border-border',
          banner.link && 'cursor-pointer'
        )}
        onClick={() => {
          if (banner.link) {
            navigate(banner.link);
            dismiss();
          }
        }}
      >
        <span className="text-sm flex-1 line-clamp-2 break-words" title={banner.title}>{banner.title}</span>
        <button
          className="shrink-0 p-0.5 rounded hover:bg-secondary transition-colors"
          onClick={e => { e.stopPropagation(); dismiss(); }}
        >
          <X className="size-3.5 text-muted-foreground" />
        </button>
      </div>
    </div>
  );
}
