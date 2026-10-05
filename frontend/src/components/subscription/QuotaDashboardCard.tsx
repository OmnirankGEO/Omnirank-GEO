/**
 * 本月配额仪表盘 - SubscriptionManagementPage 内嵌组件
 *
 * 来源: .planning/phases/07-social-studio-subscription/ADDON_OPTIMIZATION_V1.md T5
 *
 * 用户主动管理配额的入口:
 *   - 拉 GET /api/subscription/quota-usage 显示 11 维 used / limit
 *   - 使用率 > 70% 显黄色 / 100% 显红色
 *   - 可加包维度旁边显"加包"按钮 → inline 调起 SubscriptionOverrunDialog
 *
 * 设计语言: ss-* (跟随 SubscriptionManagementPage 主题)
 */
import { useEffect, useState, type CSSProperties } from 'react';
import { Loader2, Plus, AlertTriangle } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import SubscriptionOverrunDialog from './SubscriptionOverrunDialog';

interface QuotaItem {
  used: number;
  limit: number;
  remaining: number;
  addon_addable: boolean;
  is_unlimited: boolean;
  usage_pct: number;
}

interface QuotaUsageData {
  plan_id: string | null;
  plan_name: string;
  period_end: string | null;
  quotas: Record<string, QuotaItem>;
  no_active_subscription?: boolean;
}

// 维度 → 友好标签 + feature_code (OverrunDialog 用 feature_code 查 quota-check)
const QUOTA_META: Record<string, { label: string; featureCode: string }> = {
  light_chat: { label: '轻量写稿', featureCode: 'light_chat' },
  pro_write: { label: '专业写稿', featureCode: 'pro_write' },
  super_write: { label: '超级写稿', featureCode: 'super_write' },
  web_search: { label: '联网检索', featureCode: 'web_search' },
  video_minutes: { label: '视频处理分钟', featureCode: 'video_asr' },
  rewrite: { label: '仿写', featureCode: 'rewrite' },
  video_breakdown: { label: '拆视频', featureCode: 'single_video' },
  author_breakdown: { label: '拆博主', featureCode: 'author_breakdown' },
  review: { label: '内容复盘', featureCode: 'review' },
  monthly_plan: { label: '本月计划', featureCode: 'monthly_plan' },
  team_profile: { label: '团队画像', featureCode: 'team_profile' },
};

// 显示顺序: 优先展示可加包的 6 维
const DISPLAY_ORDER = [
  'video_minutes', 'pro_write', 'rewrite',
  'video_breakdown', 'author_breakdown', 'review',
  'light_chat', 'monthly_plan', 'team_profile',
  'super_write', 'web_search',
];

interface Props {
  themeStyle: CSSProperties;
  /** 父组件刷新触发器(加包成功后让父也重新拉 me) */
  onAddonPaid?: () => void;
}

export default function QuotaDashboardCard({ themeStyle, onAddonPaid }: Props) {
  const [data, setData] = useState<QuotaUsageData | null>(null);
  const [loading, setLoading] = useState(true);
  const [overrunFeature, setOverrunFeature] = useState<{ code: string; minutes: number } | null>(null);

  const reload = async () => {
    setLoading(true);
    try {
      const res = await authFetch('/api/subscription/quota-usage');
      const json = await res.json();
      if (json.success) setData(json.data);
    } catch {
      // 静默 · 仪表盘失败不阻断主页面
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    reload();
  }, []);

  const handleAddonPaid = () => {
    setOverrunFeature(null);
    reload();
    onAddonPaid?.();
  };

  if (loading) {
    return (
      <section className="rounded-2xl border border-[var(--ss-line)] bg-[var(--ss-panel)] p-5 shadow-[var(--ss-soft-shadow)]">
        <h2 className="text-base font-medium text-[var(--ss-text-soft)]">本月配额</h2>
        <div className="mt-4 flex h-24 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-[var(--ss-muted)]" />
        </div>
      </section>
    );
  }

  if (!data || data.no_active_subscription) {
    return (
      <section className="rounded-2xl border border-[var(--ss-line)] bg-[var(--ss-panel)] p-5 shadow-[var(--ss-soft-shadow)]">
        <h2 className="text-base font-medium text-[var(--ss-text-soft)]">本月配额</h2>
        <p className="mt-3 text-xs text-[var(--ss-muted)]">订阅月卡后查看 11 维配额使用情况</p>
      </section>
    );
  }

  const quotas = data.quotas || {};
  const orderedKeys = DISPLAY_ORDER.filter((k) => quotas[k] && (quotas[k].limit !== 0 || quotas[k].used > 0));

  return (
    <>
      <section className="rounded-2xl border border-[var(--ss-line)] bg-[var(--ss-panel)] p-5 shadow-[var(--ss-soft-shadow)]">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-medium text-[var(--ss-text-soft)]">本月配额使用</h2>
          {(() => {
            // Bug D 修: period_end 可能 null / 无效 ISO · 防 "Invalid Date"
            if (!data.period_end) return null;
            const d = new Date(data.period_end);
            if (Number.isNaN(d.getTime())) return null;
            return (
              <span className="text-xs text-[var(--ss-quiet)]">
                到期 {d.toLocaleDateString('zh-CN')}
              </span>
            );
          })()}
        </div>
        <p className="mt-1 text-xs text-[var(--ss-muted)]">
          月卡 quotas 用尽后自动按算力扣兜底 · 想加配额选下方"加包"按钮
        </p>

        <div className="mt-4 grid gap-2 sm:grid-cols-2">
          {orderedKeys.map((key) => {
            const q = quotas[key];
            const meta = QUOTA_META[key];
            if (!meta) return null;
            const isUnlimited = q.is_unlimited;
            const pct = isUnlimited ? 0 : q.usage_pct;
            const tone =
              isUnlimited ? 'unlimited' :
              pct >= 100 ? 'critical' :
              pct >= 70 ? 'warning' :
              'normal';

            return (
              <div
                key={key}
                className={cn(
                  'rounded-xl border bg-[var(--ss-panel-soft)] px-3 py-2.5 transition',
                  tone === 'critical'
                    ? 'border-[var(--ss-danger-line)] bg-[var(--ss-danger-bg)]'
                    : tone === 'warning'
                      ? 'border-amber-500/40'
                      : 'border-[var(--ss-line-soft)]',
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium text-[var(--ss-text)]">{meta.label}</span>
                  <span className="shrink-0 text-xs tabular-nums text-[var(--ss-muted)]">
                    {isUnlimited ? '∞' : `${q.used} / ${q.limit}`}
                  </span>
                </div>

                {/* progress bar */}
                {!isUnlimited && (
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--ss-panel-softer)]">
                    <div
                      className={cn(
                        'h-full rounded-full transition-all',
                        tone === 'critical'
                          ? 'bg-[var(--ss-danger)]'
                          : tone === 'warning'
                            ? 'bg-amber-500'
                            : 'bg-[var(--ss-primary)]',
                      )}
                      style={{ width: `${Math.min(100, pct)}%` }}
                    />
                  </div>
                )}

                {/* 加包按钮 (仅可加包 + 用率 > 50% 时显) */}
                {q.addon_addable && !isUnlimited && pct >= 50 && (
                  <button
                    type="button"
                    onClick={() => setOverrunFeature({ code: meta.featureCode, minutes: 0 })}
                    className={cn(
                      'mt-2 flex h-7 items-center gap-1 rounded-full px-2.5 text-[11px] font-medium transition',
                      tone === 'critical'
                        ? 'bg-[var(--ss-primary)] text-[var(--ss-primary-text)] hover:bg-[var(--ss-primary-hover)]'
                        : 'border border-[var(--ss-line)] text-[var(--ss-text-soft)] hover:bg-[var(--ss-hover)]',
                    )}
                  >
                    <Plus className="h-3 w-3" aria-hidden="true" />
                    加包
                    {tone === 'critical' && (
                      <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                    )}
                  </button>
                )}
              </div>
            );
          })}
        </div>

        {orderedKeys.length === 0 && (
          <p className="mt-3 text-xs text-[var(--ss-quiet)]">本周期还未使用任何额度</p>
        )}
      </section>

      {/* 加包 OverrunDialog (mode='manual' · 主动加包文案 + 隐藏"继续扣积分") */}
      {overrunFeature && (
        <SubscriptionOverrunDialog
          open={!!overrunFeature}
          onOpenChange={(open) => !open && setOverrunFeature(null)}
          featureCode={overrunFeature.code}
          videoMinutes={overrunFeature.minutes}
          onPaidAddon={handleAddonPaid}
          onSkip={() => setOverrunFeature(null)}
          themeStyle={themeStyle}
          mode="manual"
        />
      )}
    </>
  );
}
