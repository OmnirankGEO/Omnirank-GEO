/**
 * 管理员 · 运行概览：晋升/私有/拒绝/撤回、对账延迟、卡住任务、聚合更新、配置版本、平台健康摘要。
 * 严格按 AI-3 AdminOverviewDTO；不发明 source_events/feature_flags 等后端未提供的字段。
 */

import { AlertTriangle } from 'lucide-react';
import { Panel, SectionHeader, MetricTile, InfoTip, Pill } from '../../components/shared';
import { PAGE } from '../../copy';
import { formatCount, formatDelaySeconds, formatUpdatedAt } from '../../format';
import type { AdminOverview, RuntimeHealth } from '../../types';

const HEALTH_LABEL: Record<RuntimeHealth, { label: string; tone: Parameters<typeof Pill>[0]['tone'] }> = {
  healthy: { label: '正常', tone: 'good' },
  degraded: { label: '降级', tone: 'warn' },
  unavailable: { label: '不可用', tone: 'bad' },
  unknown: { label: '未知', tone: 'muted' },
};

export function AdminOverviewTab({ data }: { data: AdminOverview }) {
  const k = PAGE.admin.kpi;
  const needsReconcile = data.counts.withdrawn > 0 || data.counts.stuck_claims > 0;

  return (
    <div className="space-y-4">
      {needsReconcile ? (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3.5 py-2.5">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <p className="text-xs leading-relaxed text-foreground">
            存在已撤回或卡住的来源，需确认它们对公开聚合的贡献为零。撤回来源不会进入任何公开统计。
          </p>
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricTile label={k.pending_review} value={formatCount(data.counts.pending_review)} info="等待人工审核是否可进入匿名公共学习层的来源数量。" />
        <MetricTile label={k.private_only} value={formatCount(data.counts.private_only)} info="只服务该客户、不进入公共层的来源数量。" />
        <MetricTile label={k.rejected} value={formatCount(data.counts.rejected)} info="因隐私或质量原因被拒绝晋升的来源数量。" />
        <MetricTile label={k.withdrawn} value={formatCount(data.counts.withdrawn)} info="因注销/撤回/退款等被撤回的来源；对公开聚合贡献严格为零。" />
        <MetricTile
          label={k.stuck_claims}
          value={formatCount(data.counts.stuck_claims)}
          tone={data.counts.stuck_claims > 0 ? 'warn' : 'default'}
          info="处理中但长时间未完成的任务，需要人工介入。"
        />
        <MetricTile
          label={k.reconciler_delay}
          value={formatDelaySeconds(data.reconciler_delay_seconds)}
          tone={data.reconciler_delay_seconds > 300 ? 'warn' : 'default'}
          info="来源登记与聚合对账之间的延迟。"
        />
        <div className="rounded-lg border border-border bg-card p-3.5 sm:p-4">
          <span className="text-xs text-muted-foreground">{k.aggregate_updated}</span>
          <div className="mt-1.5 text-sm font-medium text-foreground">{formatUpdatedAt(data.aggregate_updated_at)}</div>
        </div>
        <div className="rounded-lg border border-border bg-card p-3.5 sm:p-4">
          <div className="flex items-center gap-1">
            <span className="text-xs text-muted-foreground">{k.policy_version}</span>
            <InfoTip label="当前生效的观测策略配置版本。" />
          </div>
          <div className="mt-1.5 text-sm font-medium tabular-nums text-foreground">
            {data.policy_version === null ? '未接入' : `v${data.policy_version}`}
          </div>
        </div>
      </div>

      {/* 平台健康摘要（AI-3 只给 platform/enabled/health） */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader title="平台健康摘要" hint="平台的启用状态与运行健康（只读，系统自动探测）。" />
        <ul className="mt-3 space-y-2">
          {data.platform_health.map((h) => {
            const hl = HEALTH_LABEL[h.runtime_health];
            return (
              <li key={h.platform} className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2">
                <span className="text-sm text-foreground">{h.platform}</span>
                <span className="flex items-center gap-2">
                  <span
                    className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      h.enabled_by_policy ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' : 'bg-muted text-muted-foreground'
                    }`}
                  >
                    {h.enabled_by_policy ? '已启用' : '已停用'}
                  </span>
                  <Pill tone={hl.tone}>{hl.label}</Pill>
                </span>
              </li>
            );
          })}
        </ul>
      </Panel>
    </div>
  );
}
