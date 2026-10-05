/**
 * GeoHome — GEO 用户工作台
 * 品牌表现 + 快捷操作 + 最近发布 + 最近活动
 * 空状态引导（brand_count=0 时显示极速引导）
 */

import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/context/AuthContext';
import { useHomeStats } from '@/hooks/useHomeStats';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Sparkles, Stethoscope, BookOpen, Send, Activity,
  CheckCircle2, Clock, XCircle, ExternalLink, Loader2,
  PauseCircle, TrendingUp, Building2, Calculator,
} from 'lucide-react';
// CTO-15.20 v2 · C.3 · 顶部加"今日跟进"优先级 widget(代理日常入口)
import { TodayPriorityWidget } from '@/components/workbench/TodayPriorityWidget';
// WJ-45 玩法B 说明卡(新代理首次进工作台讲清生意闭环 · 可关闭)
import { PlayBookCard } from '@/components/PlayBookCard';

const STATUS_ICON: Record<string, any> = {
  published: { icon: CheckCircle2, color: 'text-green-500', label: '已发布' },
  submitted: { icon: Clock, color: 'text-blue-500', label: '审核中' },
  rejected: { icon: XCircle, color: 'text-red-500', label: '已拒稿' },
  pending: { icon: Clock, color: 'text-muted-foreground', label: '准备中' },
  queued: { icon: PauseCircle, color: 'text-amber-500', label: '排队中' },
};

// WJ-01 今日路线 4 步(新代理首屏知道第一步点哪)
const FOUR_STEPS = [
  { label: '录客户', link: '/my-clients', icon: Building2 },
  { label: 'AI 体检', link: '/diagnosis/new', icon: Stethoscope },
  { label: '生成报价', link: '/pricing', icon: Calculator },
  { label: '写文章', link: '/writing', icon: BookOpen },
];

export function GeoHome() {
  const { user } = useAuth();
  const navigate = useNavigate();
  // WJ-45 修(Codex P1):玩法B 卡仅代理(L1/L2/paid · 非 admin)看 · 普通用户不看经营话术
  const isAgent = (user?.agent_level ?? 0) >= 1;
  const isAdmin = !!(user as { is_admin?: boolean } | null)?.is_admin;
  const { data, loading, stale, error, retry } = useHomeStats<any>();

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="size-6 animate-spin text-muted-foreground" /></div>;
  if (!data && error) {
    return (
      <div role="alert" className="mx-auto max-w-lg space-y-3 px-4 py-20 text-center">
        <p className="text-sm text-destructive">{error} · 这不代表业务数据为 0。</p>
        <Button variant="outline" onClick={() => void retry()}>重新读取</Button>
      </div>
    );
  }

  const geo = data?.geo || {};
  const balance = data?.balance || {};
  const activities = data?.recent_activities || [];

  // 空状态引导
  if (geo.brand_count === 0) {
    return (
      <div className="max-w-lg mx-auto text-center py-16 px-4 space-y-6">
        <div className="size-20 rounded-2xl bg-primary/10 flex items-center justify-center mx-auto">
          <Sparkles className="size-10 text-primary" />
        </div>
        <h2 className="text-2xl font-bold">开启你的 AI 搜索占位之旅</h2>
        <p className="text-muted-foreground">
          添加你的品牌，AI 会自动分析你在豆包、Kimi、DeepSeek、千问中的可见度，
          并为你量身定制优化方案。
        </p>
        <Button size="lg" onClick={() => navigate('/diagnosis/new')}>
          + 开始第一次品牌体检
        </Button>
      </div>
    );
  }

  // WJ-01 当前步(空状态已在上方拦截 · 此处 brand_count > 0,录客户步已完成)
  const hasDiag = geo.latest_score != null || (geo.diagnosis_count_month || 0) > 0;
  const hasArticle = (geo.article_count || 0) > 0;
  const currentStep = !hasDiag ? 1 : !hasArticle ? 2 : 3;

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
          {new Date().getHours() < 12 ? '早上好' : new Date().getHours() < 18 ? '下午好' : '晚上好'}，{(user as { nickname?: string; display_name?: string } | null)?.nickname || (user as { display_name?: string } | null)?.display_name || '你好'}
        </h1>
        <p className="text-sm text-muted-foreground mt-0.5">
          余额 {balance.total?.toLocaleString() || 0} 算力
        </p>
      </div>

      {/* WJ-01 今日路线 · 4 步引导(高亮当前步 · 点击直达) */}
      <Card className="min-w-0 overflow-hidden border-primary/20 bg-primary/5">
        <CardContent className="p-4 sm:p-5">
          <div className="mb-3">
            <h2 className="text-base font-semibold text-foreground">今日路线</h2>
            <p className="text-xs text-muted-foreground mt-0.5">先做 4 步,拿方案见客户</p>
          </div>
          <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
            {FOUR_STEPS.map((step, i) => {
              const done = i < currentStep;
              const current = i === currentStep;
              const Icon = step.icon;
              return (
                <button
                  key={i}
                  type="button"
                  onClick={() => navigate(step.link)}
                  className={`min-h-[72px] min-w-0 rounded-xl border p-3 text-left transition-colors ${
                    current
                      ? 'border-primary bg-primary/10 ring-1 ring-primary/30'
                      : done
                        ? 'border-border bg-secondary/30'
                        : 'border-border bg-card hover:bg-secondary/20'
                  }`}
                >
                  <div className="flex items-center gap-1.5">
                    <span className={`flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold ${
                      done ? 'bg-green-500 text-white' : current ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'
                    }`}>
                      {done ? '✓' : i + 1}
                    </span>
                    <Icon className="size-4 text-muted-foreground" />
                  </div>
                  <div className="mt-2 truncate text-sm font-medium text-foreground">{step.label}</div>
                  {current && <div className="mt-0.5 text-[11px] text-primary">现在做这步 →</div>}
                </button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      {/* WJ-45 玩法B 说明卡 · 仅代理(L1/L2/paid · 非 admin)· 今日路线下方(未关闭时) */}
      {isAgent && !isAdmin && (
        <PlayBookCard className="min-w-0" userId={(user as { id?: number | string } | null)?.id} />
      )}

      <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1.55fr)_minmax(360px,0.8fr)] items-start">
        {/* CTO-15.20 v2 · C.3 · 今日跟进优先级 widget(primary 1 + secondary 4 紧急客户) */}
        {/* 🔴 [包三 · 首页看板] 「今天 3 件事一眼看到」——这里只给 3 张。
            组件默认 1+4=5 张,而 5 张同类卡片不是"三件事",是一张列表的前 5 行:
            她仍然要自己挑。给 3 张、其余留在「全部」后面,才是"一眼看到"。
            🔴 改的是**调用点的参数**,不是组件默认值 —— M3 工作台那边按 5 张排版,
            改默认值会顺手改掉另一个页面的布局(那是别人的 territory)。 */}
        <TodayPriorityWidget className="h-full min-w-0" maxSecondary={2} />

        <Card className="h-full min-w-0 overflow-hidden">
          <CardContent className="min-w-0 p-4 sm:p-5 space-y-4">
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-foreground">GEO 业务</h2>
              <p className="text-xs text-muted-foreground mt-0.5">每天常用工具和关键数字</p>
            </div>

            {/* 核心指标 */}
            <div className="grid min-w-0 grid-cols-2 gap-2.5">
              {[
                { label: '品牌可见度', value: geo.latest_score ? `${geo.latest_score}分` : '—', sub: '最新评分', link: '/history' },
                { label: '已写文章', value: geo.article_count, sub: `本月诊断 ${geo.diagnosis_count_month} 次`, link: '/writing' },
                { label: '已发布', value: geo.published_count, sub: '代发成功', link: '/publish/history' },
                { label: '管理品牌', value: geo.brand_count, sub: '客户资产', link: '/my-clients' },
              ].map((s, i) => (
                <button
                  key={i}
                  type="button"
                  className="min-h-[80px] min-w-0 overflow-hidden rounded-xl border bg-secondary/20 p-3 text-left transition-colors hover:bg-secondary/40"
                  onClick={() => navigate(s.link)}
                >
                  <div className="truncate text-2xl font-bold leading-none">{s.value}</div>
                  <div className="mt-2 truncate text-xs text-foreground">{s.label}</div>
                  {s.sub && <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{s.sub}</div>}
                </button>
              ))}
            </div>

            {/* 快捷操作 */}
            <div className="grid min-w-0 grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-1">
              {[
                { label: '新建诊断', desc: '给客户做 AI 搜索体检', icon: Stethoscope, link: '/diagnosis/new' },
                { label: '写作大厅', desc: '根据方案生成文章', icon: BookOpen, link: '/writing' },
                { label: '发布中心', desc: '发布内容和证据', icon: Send, link: '/publish' },
                { label: '监测中心', desc: '追踪 AI 推荐变化', icon: Activity, link: '/monitoring' },
              ].map((a, i) => (
                <button
                  key={i}
                  type="button"
                  className="flex min-h-[58px] min-w-0 items-center gap-3 overflow-hidden rounded-xl border bg-card px-3 py-3 text-left transition-colors hover:bg-secondary/30"
                  onClick={() => navigate(a.link)}
                >
                  <span className="size-9 rounded-lg bg-secondary flex items-center justify-center shrink-0">
                    <a.icon className="size-4 text-muted-foreground" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-medium">{a.label}</span>
                    <span className="block text-xs text-muted-foreground truncate">{a.desc}</span>
                  </span>
                </button>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* 最近发布 */}
      {geo.recent_publishes && geo.recent_publishes.length > 0 && (
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium">最近发布</h3>
              <Button variant="ghost" size="sm" onClick={() => navigate('/publish/history')}>
                查看全部 <ExternalLink className="size-3 ml-1" />
              </Button>
            </div>
            <div className="space-y-2">
              {geo.recent_publishes.map((p: any, i: number) => {
                const cfg = STATUS_ICON[p.status] || STATUS_ICON.pending;
                const Icon = cfg.icon;
                return (
                  <div key={i} className="flex items-center gap-3 text-sm">
                    <Icon className={`size-4 shrink-0 ${cfg.color}`} />
                    <span className="font-medium truncate flex-1">{p.media_name}</span>
                    <span className="text-muted-foreground truncate max-w-[200px]">{p.title}</span>
                    <span className={`text-xs shrink-0 ${cfg.color}`}>{cfg.label}</span>
                    <span className="text-xs text-muted-foreground shrink-0">{p.time}</span>
                    {p.url && (
                      <a href={p.url} target="_blank" rel="noopener noreferrer" className="text-primary" onClick={e => e.stopPropagation()}>
                        <ExternalLink className="size-3" />
                      </a>
                    )}
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}

      {/* 最近活动 */}
      {activities.length > 0 && (
        <Card>
          <CardContent className="p-4">
            <h3 className="text-sm font-medium mb-3">最近活动</h3>
            <div className="space-y-2">
              {activities.map((a: any, i: number) => (
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
