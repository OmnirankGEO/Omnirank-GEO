/**
 * 管理员 · 平台与模型：
 * - 平台启用状态 + 只读运行健康（AI-3 只给 platform/enabled/health；无 provider/surface/model 明细则不造）。
 * - 模型版本漂移（AI-3 /model-shifts，真实）：解释趋势分段，不解释为客户涨跌。
 * - 平台启停写属 AI-2 治理接口，未交付 → 只读展示 + 明确说明，不显示会 404 的写按钮。
 */

import { Lock, Activity } from 'lucide-react';
import { Panel, SectionHeader, Pill, InfoTip } from '../../components/shared';
import { StabilityTag } from '../../components/badges';
import { ResourceStateView } from '../../components/states';
import { OBSERVATION_CAPABILITIES } from '../../capabilities';
import { PAGE } from '../../copy';
import { bpsToPercent, formatDate } from '../../format';
import { useObservationResource } from '../../hooks/useObservationResource';
import type { ObservationClient } from '../../client';
import type { AdminOverview, RuntimeHealth } from '../../types';

const HEALTH_LABEL: Record<RuntimeHealth, { label: string; tone: Parameters<typeof Pill>[0]['tone'] }> = {
  healthy: { label: '正常', tone: 'good' },
  degraded: { label: '降级', tone: 'warn' },
  unavailable: { label: '不可用', tone: 'bad' },
  unknown: { label: '未知', tone: 'muted' },
};

const PLATFORM_NAMES: Record<string, string> = {
  doubao: '豆包',
  qwen: '千问',
  deepseek: 'DeepSeek',
  yuanbao: '元宝',
  kimi: 'Kimi',
};

export function AdminPlatformsTab({
  overview,
  client,
}: {
  overview: AdminOverview;
  client: ObservationClient;
}) {
  const shifts = useObservationResource((signal) => client.adminModelShifts(signal), [], true);

  const shiftState = ResourceStateView({
    status: shifts.status,
    errorCode: shifts.errorCode,
    httpStatus: shifts.httpStatus,
    message: shifts.errorMessage,
    isEmpty: shifts.status === 'success' && (shifts.data?.items.length ?? 0) === 0,
    onRetry: shifts.reload,
    compact: true,
    emptyTitle: '暂无模型版本漂移',
    emptyDesc: '各平台模型版本稳定时，这里为空。',
  });

  return (
    <div className="space-y-4">
      <SectionHeader title="平台与模型" hint="各平台的启用状态与运行健康。" info={PAGE.admin.healthReadOnly} />

      {/* 只读健康 */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {overview.platform_health.map((p) => {
          const hl = HEALTH_LABEL[p.runtime_health];
          return (
            <Panel key={p.platform} className="p-4">
              <div className="flex items-start justify-between gap-2">
                <h4 className="text-sm font-semibold text-foreground">{p.platform}</h4>
                <span
                  className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                    p.enabled_by_policy ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' : 'bg-muted text-muted-foreground'
                  }`}
                >
                  {p.enabled_by_policy ? '已启用' : '已停用'}
                </span>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                  <Activity className="h-3.5 w-3.5" />
                  运行健康
                </span>
                <Pill tone={hl.tone}>{hl.label}</Pill>
              </div>
            </Panel>
          );
        })}
      </div>

      {/* 平台启停写：capability 未接入 → 只读说明，无写按钮 */}
      {!OBSERVATION_CAPABILITIES.platformPolicyWrite ? (
        <div className="flex items-start gap-2 rounded-lg border border-border bg-muted/40 px-3.5 py-2.5">
          <Lock className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <p className="text-xs leading-relaxed text-muted-foreground">{PAGE.admin.platformWritePending}</p>
        </div>
      ) : null}

      {/* 模型版本漂移（真实 /model-shifts） */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader title={PAGE.admin.modelShiftTitle} hint={PAGE.admin.modelShiftHint} />
        <div className="mt-3">
          {shiftState ? (
            shiftState
          ) : (
            <ul className="space-y-2">
              {shifts.data!.items.map((s, i) => (
                <li key={i} className="flex flex-wrap items-center gap-2 rounded-lg bg-muted/50 px-3 py-2 text-sm">
                  <span className="font-medium text-foreground">{s.industry_key}</span>
                  {s.platform_key ? <Pill tone="neutral">{PLATFORM_NAMES[s.platform_key] || s.platform_key}</Pill> : null}
                  {s.model_revision ? (
                    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                      模型版本 {s.model_revision}
                    </span>
                  ) : null}
                  <span className="text-xs text-muted-foreground">断点 {formatDate(s.bucket_start)}</span>
                  <StabilityTag status={s.stability_status} />
                  <span className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground">
                    漂移强度 {bpsToPercent(s.model_shift_index_bps)}
                    <InfoTip label="模型/表面版本变化前后的结构差异，只用来标记口径断点。" />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Panel>
    </div>
  );
}
