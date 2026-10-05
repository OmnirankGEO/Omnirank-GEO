/**
 * TodayPriorityWidget · 旧版 / Dashboard 顶部"今日跟进"轻量版
 *
 * CTO-15.21 v5 收口(2026-04-28 老板拍板):
 *   - 拉 m3Api.getSalesToday() 拿到 sorted by priority 客户列表
 *   - 显示 primary 1 + secondary top 4 共 5 卡
 *   - 每卡点击跳 /my-clients/:brand_id(旧版客户详情)
 *   - 顶部「全部」跳 /dashboard（旧版今日看板；/dashboard/today 自 05-05 只是一条重定向，#165 F5 改直达）
 *
 * 元指令:Refactor Not Rewrite · 不重写 M3 今日销售页 · 复用 m3Api + 简化展示。
 */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, Sun, Loader2 } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { StagePill } from './parts';
import { m3Api, type SalesClientListItem } from '@/services/m3';
import { cn } from '@/lib/utils';

interface TodayPriorityWidgetProps {
  className?: string;
  /** 显示几个 secondary 客户 · 默认 4(配合 primary 共 5 卡) */
  maxSecondary?: number;
}

export function TodayPriorityWidget({
  className,
  maxSecondary = 4,
}: TodayPriorityWidgetProps) {
  const navigate = useNavigate();
  const [primary, setPrimary] = useState<SalesClientListItem | undefined>(undefined);
  const [secondary, setSecondary] = useState<SalesClientListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    m3Api
      .getSalesToday()
      .then((snap) => {
        if (!mounted) return;
        setPrimary(snap.primary);
        setSecondary(snap.secondary.slice(0, maxSecondary));
        setTotal(snap.total);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!mounted) return;
        setError(e instanceof Error ? e.message : '加载失败');
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [maxSecondary]);

  // Keep the grid cell mounted across loading, empty, and error states. Removing
  // it after the first paint makes the adjacent business card jump columns.
  if (loading) {
    return (
      <Card className={cn('min-w-0 overflow-hidden border-primary/20 bg-primary/5', className)}>
        <CardContent className="flex min-w-0 items-center gap-2 p-4 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          <span className="min-w-0 truncate">加载今日跟进...</span>
        </CardContent>
      </Card>
    );
  }

  if (error || (!primary && secondary.length === 0)) {
    return (
      <Card className={cn('min-w-0 overflow-hidden border-primary/20 bg-primary/5', className)}>
        <CardContent className="flex min-h-[112px] min-w-0 flex-col justify-center gap-1 p-4">
          <div className="flex items-center gap-2 text-sm font-medium text-foreground">
            <Sun className="h-4 w-4 shrink-0 text-primary" aria-hidden />
            今日跟进
          </div>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {error ? '今日跟进暂时无法更新，不影响其他业务数据。' : '今天暂无需要优先跟进的客户。'}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className={cn('min-w-0 overflow-hidden border-primary/20 bg-primary/5', className)}>
      <CardContent className="min-w-0 space-y-3 p-3 sm:p-4">
        {/* 头部 · 标题 + 跳完整今日跟进 */}
        <div className="flex min-w-0 items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <Sun className="h-4 w-4 shrink-0 text-primary" aria-hidden />
            <h2 className="shrink-0 text-sm font-semibold text-foreground">今日跟进</h2>
            {total > 0 && (
              <Badge variant="outline" className="shrink-0 whitespace-nowrap px-1.5 py-0 text-[10px]">
                共 {total} 客户
              </Badge>
            )}
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 shrink-0 gap-1 text-xs"
            onClick={() => navigate('/dashboard')}
            aria-label="查看完整今日看板"
          >
            全部
            <ArrowRight className="h-3 w-3" aria-hidden />
          </Button>
        </div>

        {/* primary · 现在最紧急 */}
        {primary && (
          <button
            type="button"
            onClick={() => navigate(`/my-clients/${primary.id}`)}
            className="min-h-[64px] w-full min-w-0 overflow-hidden rounded-lg border border-primary/30 bg-card p-3 text-left transition-colors hover:bg-primary/5"
            aria-label={`查看客户详情 · ${primary.name}`}
          >
            <div className="mb-1 flex min-w-0 items-center justify-between gap-2">
              <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider text-primary">
                现在最紧急
              </span>
              {primary.timer && (
                <span className="min-w-0 truncate text-[10px] text-muted-foreground">{primary.timer}</span>
              )}
            </div>
            <div className="flex min-w-0 items-start gap-2">
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold text-foreground break-words leading-snug" title={primary.name}>{primary.name}</div>
                <div className="text-xs text-muted-foreground break-words leading-snug">
                  {primary.industry || '—'} · {primary.city || '—'}
                </div>
              </div>
              <div className="flex max-w-[42%] shrink-0 items-center justify-end gap-1.5 overflow-hidden">
                <StagePill stage={primary.stage} className="truncate" />
                <ArrowRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
              </div>
            </div>
          </button>
        )}

        {/* secondary · 稍后处理 */}
        {secondary.length > 0 && (
          <div className="min-w-0 space-y-1.5">
            <div className="px-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              稍后处理
            </div>
            <div className="grid min-w-0 grid-cols-1 gap-1.5 sm:grid-cols-2">
              {secondary.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => navigate(`/my-clients/${c.id}`)}
                  className="min-h-[44px] min-w-0 overflow-hidden rounded-md border border-border bg-card/60 px-2.5 py-2 text-left transition-colors hover:bg-muted/40"
                  aria-label={`查看客户详情 · ${c.name} · ${c.stage_label}`}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="min-w-0 flex-1 text-xs font-medium text-foreground break-words leading-snug" title={c.name}>
                      {c.name}
                    </span>
                    <StagePill stage={c.stage} className="max-w-[45%] truncate" />
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
