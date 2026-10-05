/**
 * FullHome — 全量模式工作台
 * 双栏布局：左 GEO 摘要 + 右社媒摘要
 * 移动端变为上下堆叠 + Tab 切换
 */

import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/context/AuthContext';
import { useHomeStats } from '@/hooks/useHomeStats';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  Stethoscope, BookOpen, Send, Loader2, RefreshCw,
} from 'lucide-react';
// CTO-15.20 v2 · C.3 · 顶部加"今日跟进"优先级 widget(代理日常入口)
import { TodayPriorityWidget } from '@/components/workbench/TodayPriorityWidget';

export function FullHome() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const { data, loading, stale, error, retry } = useHomeStats<any>();

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="size-6 animate-spin text-muted-foreground" /></div>;
  if (!data && error) return <HomeStatsError message={error} onRetry={retry} />;

  const geo = data?.geo || {};
  const balance = data?.balance || {};

  const GeoSection = () => (
    <Card className="h-full min-w-0 overflow-hidden">
      <CardContent className="min-w-0 p-4 sm:p-5 space-y-4">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-foreground">GEO 业务</h2>
          <p className="text-xs text-muted-foreground mt-0.5">诊断、写作、发布都从这里进</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button data-testid="home-refresh" aria-label="刷新首页" variant="ghost" size="icon" className="size-9" onClick={() => void retry()}>
            <RefreshCw className="size-4" />
          </Button>
          <Button variant="ghost" size="sm" className="h-9 px-2" onClick={() => navigate('/dashboard')}>
            今日看板 →
          </Button>
        </div>
      </div>
      <div className="grid min-w-0 grid-cols-2 gap-2.5">
        {[
          { label: '品牌', value: geo.brand_count || 0 },
          { label: '文章', value: geo.article_count || 0 },
          { label: '已发布', value: geo.published_count || 0 },
          { label: '本月诊断', value: geo.diagnosis_count_month || 0 },
        ].map(s => (
          <div key={s.label} className="flex min-h-[76px] min-w-0 flex-col justify-center overflow-hidden rounded-xl border bg-secondary/20 p-3 text-center">
            <div className="truncate text-2xl font-bold leading-none">{s.value}</div>
            <div className="mt-1.5 truncate text-xs text-muted-foreground">{s.label}</div>
          </div>
        ))}
      </div>
      <div className="grid min-w-0 grid-cols-1 gap-2.5 sm:grid-cols-3 lg:grid-cols-1">
        {[
          { label: '新建诊断', desc: '给客户做 AI 搜索体检', icon: Stethoscope, link: '/diagnosis/new' },
          { label: '写作大厅', desc: '根据方案生成文章', icon: BookOpen, link: '/writing' },
          { label: '发布中心', desc: '发布内容和证据', icon: Send, link: '/publish' },
        ].map(a => (
          <Button
            key={a.label}
            variant="outline"
            className="h-auto min-h-[58px] min-w-0 justify-start gap-3 overflow-hidden px-3 py-3"
            onClick={() => navigate(a.link)}
          >
            <span className="size-9 rounded-lg bg-secondary flex items-center justify-center shrink-0">
              <a.icon className="size-4" />
            </span>
            <span className="min-w-0 flex-1 text-left">
              <span className="block text-sm font-medium">{a.label}</span>
              <span className="block text-xs text-muted-foreground font-normal truncate">{a.desc}</span>
            </span>
          </Button>
        ))}
      </div>
      </CardContent>
    </Card>
  );

  return (
    <div className="mx-auto max-w-7xl min-w-0 overflow-x-hidden p-3 sm:p-6 space-y-6">
      {(stale || error) && (
        <div role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-amber-700 dark:text-amber-300">
          首页数据暂时无法更新，当前保留上次成功结果。
        </div>
      )}
      {/* 问候 */}
      <div className="min-w-0 px-0 sm:px-1">
        <h1 className="break-words text-xl font-bold leading-snug">
          {new Date().getHours() < 12 ? '早上好' : new Date().getHours() < 18 ? '下午好' : '晚上好'}，{data?.nickname || user?.display_name || '你好'}
        </h1>
        <p className="text-sm text-muted-foreground mt-0.5">余额 {balance.total?.toLocaleString() || 0} 算力</p>
      </div>

      <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1.55fr)_minmax(360px,0.8fr)] items-stretch">
        {/* GEO 代理工作台只保留 GEO 主线；社媒已拆成独立产品入口，不在这里混排。 */}
        <TodayPriorityWidget className="h-full min-w-0" />
        <GeoSection />
      </div>

      {/* 最近活动 */}
      {data?.recent_activities?.length > 0 && (
        <Card>
          <CardContent className="p-4">
            <h3 className="text-sm font-medium mb-3">最近动态</h3>
            <div className="space-y-2">
              {data.recent_activities.map((a: any, i: number) => (
                <div key={i} className="flex items-center gap-2 text-sm">
                  <div className="size-1.5 rounded-full bg-muted-foreground shrink-0" />
                  <span className="flex-1">{a.title}</span>
                  <span className="text-xs text-muted-foreground">{a.time}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function HomeStatsError({ message, onRetry }: { message: string; onRetry: () => Promise<void> }) {
  return (
    <div role="alert" className="mx-auto max-w-lg space-y-3 px-4 py-20 text-center">
      <p className="text-sm text-destructive">{message} · 这不代表业务数据为 0。</p>
      <Button variant="outline" onClick={() => void onRetry()}>重新读取</Button>
    </div>
  );
}
