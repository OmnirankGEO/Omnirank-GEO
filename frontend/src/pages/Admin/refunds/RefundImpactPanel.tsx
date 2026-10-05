import {
  AlertTriangle,
  Banknote,
  CheckCircle2,
  Clock3,
  FileWarning,
  ReceiptText,
  Users,
  Wallet,
  XCircle,
  Zap,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import type { RefundImpact, RefundOrderPreview, RefundWorkOrder } from './types';
import { RefundTimeline } from './RefundTimeline';

function money(cents?: number) {
  return `¥${((cents || 0) / 100).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function power(value?: number) {
  return (value || 0).toLocaleString('zh-CN');
}

function getImpact(preview: RefundOrderPreview | null, workOrder?: RefundWorkOrder | null): RefundImpact | null {
  if (workOrder?.impact_snapshot?.impact) return workOrder.impact_snapshot.impact;
  if (preview?.impact) return preview.impact;
  return null;
}

function getChecks(preview: RefundOrderPreview | null, workOrder?: RefundWorkOrder | null) {
  if (workOrder?.impact_snapshot?.checks) return workOrder.impact_snapshot.checks;
  if (preview?.checks) return preview.checks;
  return [];
}

const statusIcon = {
  pass: CheckCircle2,
  warn: AlertTriangle,
  fail: XCircle,
};

export function RefundImpactPanel({
  preview,
  workOrder,
}: {
  preview: RefundOrderPreview | null;
  workOrder?: RefundWorkOrder | null;
}) {
  const impact = getImpact(preview, workOrder);
  const checks = getChecks(preview, workOrder);
  const risks = impact?.risk_tips?.length ? impact.risk_tips : ['请先查询订单，系统会在这里展示退款影响。'];

  const cards = [
    { label: '原订单金额', value: money(impact?.original_amount_cents), icon: ReceiptText, tone: 'blue' },
    { label: '预计退款', value: money(impact?.estimated_refund_cents), icon: Banknote, tone: 'green' },
    { label: '已使用算力', value: power(impact?.used_power), icon: Zap, tone: 'violet' },
    { label: '客户钱包扣减', value: `${power(impact?.customer_wallet_deduct_power)} 算力`, icon: Wallet, tone: 'amber' },
    { label: '服务商收益影响', value: money(impact?.agent_revenue_reversal_cents), icon: Users, tone: 'yellow' },
    { label: '人工打款', value: impact?.needs_manual_payout ? '待处理' : '无需打款', icon: Clock3, tone: 'indigo' },
  ];

  return (
    <aside className="lg:sticky lg:top-5 space-y-4">
      <section className="rounded-lg border border-white/10 bg-card/70 p-4 shadow-sm">
        <div className="mb-4 flex items-center gap-2">
          <div className="grid h-8 w-8 place-items-center rounded-md bg-blue-500/12 text-blue-300">
            <FileWarning className="h-4 w-4" />
          </div>
          <h2 className="text-base font-semibold text-foreground">退款影响预览</h2>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {cards.map((card) => {
            const Icon = card.icon;
            return (
              <div
                key={card.label}
                className="rounded-md border border-white/10 bg-background/45 p-3"
              >
                <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
                  <span className={cn(
                    'grid h-7 w-7 place-items-center rounded-md',
                    card.tone === 'green' && 'bg-emerald-500/12 text-emerald-300',
                    card.tone === 'blue' && 'bg-blue-500/12 text-blue-300',
                    card.tone === 'violet' && 'bg-violet-500/12 text-violet-300',
                    card.tone === 'amber' && 'bg-amber-500/12 text-amber-300',
                    card.tone === 'yellow' && 'bg-yellow-500/12 text-yellow-300',
                    card.tone === 'indigo' && 'bg-indigo-500/12 text-indigo-300',
                  )}>
                    <Icon className="h-4 w-4" />
                  </span>
                  {card.label}
                </div>
                <div className="text-lg font-semibold leading-tight text-foreground">{card.value}</div>
              </div>
            );
          })}
        </div>
      </section>

      <section className="rounded-lg border border-white/10 bg-card/70 p-4">
        <h2 className="mb-3 text-base font-semibold text-foreground">系统检查</h2>
        <div className="space-y-2">
          {checks.length === 0 ? (
            <div className="text-sm text-muted-foreground">查询订单后显示检查结果</div>
          ) : checks.map((check) => {
            const Icon = statusIcon[check.status as keyof typeof statusIcon] || AlertTriangle;
            return (
              <div key={check.key} className="flex items-center gap-2 text-sm">
                <Icon className={cn(
                  'h-4 w-4',
                  check.status === 'pass' && 'text-emerald-400',
                  check.status === 'warn' && 'text-amber-400',
                  check.status === 'fail' && 'text-red-400',
                )} />
                <span className="text-muted-foreground">{check.label}</span>
              </div>
            );
          })}
        </div>
        <div className="mt-4 rounded-md border border-amber-500/25 bg-amber-500/8 p-3">
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-amber-200">
            <AlertTriangle className="h-4 w-4" />
            当前风险提示
          </div>
          <div className="space-y-1 text-xs leading-relaxed text-amber-100/80">
            {risks.map((risk) => <p key={risk}>{risk}</p>)}
          </div>
        </div>
      </section>

      <RefundTimeline workOrder={workOrder || null} preview={preview} />
    </aside>
  );
}
