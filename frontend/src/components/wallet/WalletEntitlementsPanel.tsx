/**
 * Social Studio 钱包余量面板 V3.1 (F02)
 *
 * 来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md
 * 计划: .planning/phases/07-social-studio-subscription/PLAN.md F02
 *
 * 显示:
 *   - 当前订阅(套餐 + 到期日 + auto_renew)
 *   - 13 维 entitlements 余量进度条(只显示有 limit > 0 的维度)
 *   - 待清算佣金行(P1-5)
 *   - "管理订阅"按钮
 */
import { useEffect, useState } from 'react';
import { Loader2, Crown, AlertTriangle, ArrowRight } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';

interface QuotaItem {
  used: number;
  limit: number;
  unlimited: boolean;
  remaining: number;
}

interface SubscriptionMe {
  subscription_id?: number;
  plan_id: string | null;
  display_name: string;
  active: boolean;
  started_at?: string;
  expires_at?: string;
  auto_renew?: boolean;
  is_first_month?: boolean;
  monthly_yuan?: number;
  pending_clawback_points: number;
  quotas: Record<string, QuotaItem | number>;
}

const QUOTA_LABEL: Record<string, string> = {
  light_chat: '轻量写稿/对话',
  pro_write: '专业写稿',
  super_write: '超级写稿',
  web_search: '联网调用',
  video_minutes: '视频处理分钟',
  rewrite: '仿写',
  video_breakdown: '拆视频',
  author_breakdown: '拆博主',
  review: '数据复盘',
  monthly_plan: '本月计划',
  team_profile: '团队画像',
};

// 显示顺序(用户最关心的优先)
const DISPLAY_ORDER = [
  'light_chat', 'pro_write', 'super_write',
  'video_minutes', 'rewrite', 'video_breakdown', 'author_breakdown',
  'web_search', 'review', 'monthly_plan', 'team_profile',
];

export default function WalletEntitlementsPanel() {
  const [data, setData] = useState<SubscriptionMe | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await authFetch('/api/subscription/me');
        const json = await res.json();
        if (alive && json.success) setData(json.data);
      } catch {
        // 接口不在(B12 patch 未 apply)→ 隐藏面板
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  if (loading) {
    return (
      <Card>
        <CardContent className="flex h-32 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </CardContent>
      </Card>
    );
  }

  if (!data) return null;  // 接口未上线就不显示

  // 未订阅时
  if (!data.active) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-medium">订阅</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 pt-0">
          <p className="text-xs text-muted-foreground">还没订阅月卡 — 试试 ¥9.9 体验首月。</p>
          <Button
            size="sm"
            className="w-full"
            onClick={() => (window.location.href = '/pricing-plans')}
          >
            查看套餐 <ArrowRight className="ml-1.5 h-3.5 w-3.5" />
          </Button>
          {data.pending_clawback_points > 0 && (
            <ClawbackBanner points={data.pending_clawback_points} />
          )}
        </CardContent>
      </Card>
    );
  }

  // 套餐到期日
  const expiresAt = data.expires_at ? new Date(data.expires_at) : null;
  const daysLeft = expiresAt
    ? Math.max(0, Math.ceil((expiresAt.getTime() - Date.now()) / 86400000))
    : 0;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <Crown className="h-4 w-4 text-amber-500" />
            {data.display_name}
          </CardTitle>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 text-xs"
            onClick={() => (window.location.href = '/subscription/manage')}
          >
            管理订阅 <ArrowRight className="ml-1 h-3 w-3" />
          </Button>
        </div>
        <p className="text-[11px] text-muted-foreground">
          剩 {daysLeft} 天 · 到期 {expiresAt?.toLocaleDateString('zh-CN')} ·{' '}
          {data.auto_renew ? '自动续费' : '到期需手动续费'}
        </p>
      </CardHeader>

      <CardContent className="space-y-3 pt-0">
        {/* 13 维进度条(只显示 limit > 0 的) */}
        <div className="space-y-2">
          {DISPLAY_ORDER.map((key) => {
            const q = data.quotas[key] as QuotaItem | undefined;
            if (!q || typeof q === 'number') return null;
            if (q.limit === 0) return null;
            return <QuotaRow key={key} label={QUOTA_LABEL[key]} quota={q} />;
          })}
        </div>

        {/* 待清算佣金 */}
        {data.pending_clawback_points > 0 && (
          <ClawbackBanner points={data.pending_clawback_points} />
        )}

        {/* 加量入口 */}
        <Button
          variant="outline"
          size="sm"
          className="w-full"
          onClick={() => (window.location.href = '/wallet?tab=addon')}
        >
          算力不够? 加购扩展包
        </Button>
      </CardContent>
    </Card>
  );
}

function QuotaRow({ label, quota }: { label: string; quota: QuotaItem }) {
  if (quota.unlimited) {
    return (
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="text-foreground/80">已用 {quota.used} · 更高上限</span>
      </div>
    );
  }
  const pct = quota.limit > 0 ? Math.min(100, Math.round((quota.used / quota.limit) * 100)) : 0;
  const danger = pct >= 90;
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className={cn('font-medium', danger ? 'text-amber-500' : 'text-foreground/80')}>
          {quota.used} / {quota.limit}
        </span>
      </div>
      <Progress
        value={pct}
        className={cn('h-1.5', danger && '[&>div]:bg-amber-500')}
      />
    </div>
  );
}

function ClawbackBanner({ points }: { points: number }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-2.5 text-xs">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
      <div className="flex-1 leading-relaxed">
        <p className="font-medium text-amber-400">
          待清算收益: {points} 算力
        </p>
        <p className="mt-0.5 text-muted-foreground">
          有用户退款触发了收益回扣,余额不足部分将从后续收益抵扣。
          清算前提现暂时锁定。
        </p>
      </div>
    </div>
  );
}
