import { useEffect, useState } from 'react';
import { Skeleton } from '@/components/ui/skeleton';

/**
 * PageLoading — 路由 lazy chunk 加载占位骨架屏
 *
 * Phase E.5 · v2 §10.2:
 *   - 默认显示骨架屏(无文字 · 不焦虑)
 *   - 超过 3 秒还没渲染完 → 顶部加一行"AI 在准备 · 通常 5 秒内完成"
 *   - 超过 8 秒 → 改"加载有点慢 · 网络可能不稳"
 *   - 用户感知"系统活着" · 不像卡死
 */
export function PageLoading() {
  const [phase, setPhase] = useState<'fast' | 'slow' | 'very_slow'>('fast');

  useEffect(() => {
    const t1 = setTimeout(() => setPhase('slow'), 3000);
    const t2 = setTimeout(() => setPhase('very_slow'), 8000);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, []);

  return (
    <div className="fixed inset-0 z-40 overflow-hidden bg-background p-4 md:px-6 space-y-4">
      {phase !== 'fast' && (
        <div
          role="status"
          aria-live="polite"
          className="text-xs text-muted-foreground italic animate-in fade-in duration-200"
        >
          {phase === 'slow' ? '⚙️ 准备中 · 通常 5 秒内完成' : '🐢 加载有点慢 · 网络可能不稳 · 再等等'}
        </div>
      )}
      <Skeleton className="h-8 w-48" />
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <Skeleton className="h-32 rounded-xl" />
        <Skeleton className="h-32 rounded-xl" />
        <Skeleton className="h-32 rounded-xl" />
      </div>
      <Skeleton className="h-64 rounded-xl" />
    </div>
  );
}
