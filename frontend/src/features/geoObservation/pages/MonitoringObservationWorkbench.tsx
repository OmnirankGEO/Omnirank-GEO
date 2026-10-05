/**
 * 服务商统一观测工作台（原地增强现役 /monitoring 或 /insights，不新建重复入口）。
 *
 * 首屏先回答：发生了什么（结果分布/稳定度）→ 为什么值得关注（口径断点/变化）→ 下一步（行动建议）。
 * 五 Tab：总览 | 平台表现 | 问题与证据 | 竞争格局 | 行动建议。
 * 所有数字来自 AI-3 真实 DTO；生成洞察是唯一会调 LLM 的动作，普通浏览零 LLM。
 * 统计粒度用 AI-3 真实 granularity（当日/本周/本月），真实时间范围由后端 window 决定并只读展示。
 */

import { useState, type ReactNode } from 'react';
import { LayoutDashboard, LayoutGrid, MessageSquareText, Swords, ListChecks, Sparkles, Download, Loader2 } from 'lucide-react';
import { useClientContext } from '@/context/ClientContext';
import { TabStrip, TabPanel, type TabDef } from '../components/TabStrip';
import { GranularityFilter } from '../components/GranularityFilter';
import { MetricTile, InfoTip } from '../components/shared';
import { StabilityInline } from '../components/badges';
import { SemanticInsightPanel } from '../components/SemanticInsightPanel';
import { EvidenceDrawer } from '../components/EvidenceDrawer';
import { ResourceStateView } from '../components/states';
import { OverviewTab } from './workbench/OverviewTab';
import { PlatformsTab } from './workbench/PlatformsTab';
import { QuestionsTab } from './workbench/QuestionsTab';
import { CompetitionTab } from './workbench/CompetitionTab';
import { ActionsTab, type NavTarget } from './workbench/ActionsTab';
import { resolveNavigate } from '../nav';
import { PAGE, METRIC_LABELS } from '../copy';
import { bpsToPercent, bpsChangeToPP, formatCount, formatUpdatedAt } from '../format';
import { useObservationResource } from '../hooks/useObservationResource';
import { useSemanticInsight } from '../hooks/useSemanticInsight';
import { observationClient, type ObservationClient } from '../client';
import type { BrandSummary, Granularity } from '../types';

type TabKey = 'overview' | 'platforms' | 'questions' | 'competition' | 'actions';

const TABS: TabDef<TabKey>[] = [
  { key: 'overview', label: PAGE.workbench.tabs.overview, icon: LayoutDashboard },
  { key: 'platforms', label: PAGE.workbench.tabs.platforms, icon: LayoutGrid },
  { key: 'questions', label: PAGE.workbench.tabs.questions, icon: MessageSquareText },
  { key: 'competition', label: PAGE.workbench.tabs.competition, icon: Swords },
  { key: 'actions', label: PAGE.workbench.tabs.actions, icon: ListChecks },
];

export interface MonitoringObservationWorkbenchProps {
  brandId?: number | null;
  brandName?: string | null;
  client?: ObservationClient;
  onNavigate?: (target: NavTarget, ctx: Record<string, unknown>) => void;
  initialGranularity?: Granularity;
  /** 暗部署或观测后端临时不可用时，保留现役洞察页面，不以 503 覆盖已有能力。 */
  unavailableFallback?: ReactNode;
}

export function MonitoringObservationWorkbench({
  brandId: providedBrandId,
  brandName,
  client = observationClient,
  onNavigate,
  initialGranularity = 'day',
  unavailableFallback,
}: MonitoringObservationWorkbenchProps) {
  const { currentBrandId } = useClientContext();
  const brandId = providedBrandId ?? currentBrandId ?? null;
  const nav = resolveNavigate(onNavigate);
  const [granularity, setGranularity] = useState<Granularity>(initialGranularity);
  const [activeTab, setActiveTab] = useState<TabKey>('overview');
  // 证据抽屉提升到工作台层：问题列表与洞察面板的"查看证据"共用。
  const [evidenceId, setEvidenceId] = useState<string | null>(null);

  const summaryRes = useObservationResource(
    (signal) => client.brandSummary(brandId as number, granularity, signal),
    [brandId, granularity],
    brandId !== null,
  );
  const summary = summaryRes.data;

  const insight = useSemanticInsight(brandId, granularity);

  const displayName = brandName || summary?.brand.display_name || '';

  const summaryState = ResourceStateView({
    status: summaryRes.status,
    errorCode: summaryRes.errorCode,
    httpStatus: summaryRes.httpStatus,
    message: summaryRes.errorMessage,
    isEmpty: summaryRes.status === 'success' && !summary,
    onRetry: summaryRes.reload,
  });

  if (summaryRes.status === 'error' && summaryRes.httpStatus === 503 && unavailableFallback) {
    return <>{unavailableFallback}</>;
  }

  function handleExport() {
    if (!summary) return;
    const md = buildExportMarkdown(summary);
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `观测概览-${summary.brand.display_name}-${summary.window.start}_${summary.window.end}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return (
    <div className="space-y-5">
      {/* 页头 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-xl font-bold text-foreground sm:text-2xl">{PAGE.workbench.title}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {displayName ? `${displayName} · ` : ''}
            {PAGE.workbench.subtitle}
          </p>
          {summary ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {summary.window.label}（{summary.window.start} ~ {summary.window.end}） · {METRIC_LABELS.data_updated_at}{' '}
              {formatUpdatedAt(summary.data_updated_at)}
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <GranularityFilter value={granularity} onChange={setGranularity} />
          <button
            type="button"
            onClick={insight.generate}
            disabled={brandId === null || insight.phase === 'starting' || insight.phase === 'running'}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-brand px-3.5 py-2 text-xs font-semibold text-white transition hover:bg-brand-hover disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
          >
            {insight.phase === 'starting' || insight.phase === 'running' ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Sparkles className="h-3.5 w-3.5" />
            )}
            {insight.phase === 'completed' ? PAGE.workbench.regenerateInsight : PAGE.workbench.generateInsight}
          </button>
          <button
            type="button"
            onClick={handleExport}
            disabled={!summary}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-border bg-card px-3.5 py-2 text-xs font-medium text-foreground transition hover:bg-muted disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
          >
            <Download className="h-3.5 w-3.5" />
            {PAGE.workbench.export}
          </button>
        </div>
      </div>

      {/* 未选客户 */}
      {brandId === null ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-border bg-card px-6 py-14 text-center">
          <p className="text-sm font-medium text-foreground">{PAGE.workbench.pickBrand}</p>
          <p className="mt-1.5 max-w-md text-xs text-muted-foreground">{PAGE.workbench.pickBrandHint}</p>
        </div>
      ) : summaryState ? (
        summaryState
      ) : summary ? (
        <>
          {/* KPI 概览（比例可空 → 显示"—"，绝不当 0%） */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <MetricTile
              label={METRIC_LABELS.valid_observations}
              value={formatCount(summary.summary.valid_observations)}
              info="本周期回答有效、实体判定明确的观测数；品牌可能混淆或本次检测未完成的不计入。"
            />
            <MetricTile
              label={METRIC_LABELS.presence_rate}
              value={bpsToPercent(summary.summary.presence_rate_bps)}
              sub={changeSub(summary, 'presence')}
            />
            <MetricTile
              label={METRIC_LABELS.explicit_recommendation_rate}
              value={bpsToPercent(summary.summary.explicit_recommendation_rate_bps)}
              sub={changeSub(summary, 'recommendation')}
            />
            <div className="rounded-lg border border-border bg-card p-3.5 sm:p-4">
              <div className="flex items-center gap-1">
                <span className="text-xs text-muted-foreground">结果稳定度</span>
                <InfoTip label={summary.summary.stability_explanation} />
              </div>
              <div className="mt-2">
                <StabilityInline status={summary.summary.stability_status} />
              </div>
            </div>
          </div>

          {/* 洞察面板（仅显式生成后出现）；证据引用可打开证据抽屉 */}
          <SemanticInsightPanel state={insight} onEvidenceClick={setEvidenceId} />

          {/* 主 Tab */}
          <TabStrip tabs={TABS} active={activeTab} onChange={setActiveTab} idBase="geoobs-wb" />

          <div className="mt-4">
            <TabPanel idBase="geoobs-wb" tabKey="overview" active={activeTab}>
              <OverviewTab client={client} brandId={brandId} granularity={granularity} summary={summary} />
            </TabPanel>
            <TabPanel idBase="geoobs-wb" tabKey="platforms" active={activeTab}>
              <PlatformsTab client={client} brandId={brandId} granularity={granularity} />
            </TabPanel>
            <TabPanel idBase="geoobs-wb" tabKey="questions" active={activeTab}>
              <QuestionsTab client={client} brandId={brandId} onOpenEvidence={setEvidenceId} />
            </TabPanel>
            <TabPanel idBase="geoobs-wb" tabKey="competition" active={activeTab}>
              <CompetitionTab client={client} brandId={brandId} summary={summary} />
            </TabPanel>
            <TabPanel idBase="geoobs-wb" tabKey="actions" active={activeTab}>
              <ActionsTab
                client={client}
                brandId={brandId}
                granularity={granularity}
                nextActions={summary.next_actions}
                onNavigate={nav}
              />
            </TabPanel>
          </div>

          {/* 证据抽屉（工作台层，问题列表 + 洞察面板共用） */}
          <EvidenceDrawer
            client={client}
            brandId={brandId}
            observationId={evidenceId}
            open={evidenceId !== null}
            onClose={() => setEvidenceId(null)}
            onAddEvidence={() => {
              setEvidenceId(null);
              nav('writing', { brandId, action: 'add_evidence' });
            }}
          />
        </>
      ) : null}
    </div>
  );
}

function changeSub(summary: BrandSummary, kind: 'presence' | 'recommendation'): string {
  if (!summary.comparison.comparison_allowed) return '本周期不做跨期对比';
  const bps = kind === 'presence' ? summary.comparison.presence_change_bps : summary.comparison.recommendation_change_bps;
  if (bps === null) return '暂无对比数据';
  return `较上周期 ${bpsChangeToPP(bps)}`;
}

function buildExportMarkdown(s: BrandSummary): string {
  const lines: string[] = [];
  lines.push(`# 统一观测概览 · ${s.brand.display_name}`);
  lines.push('');
  lines.push(`- 时间范围：${s.window.label}（${s.window.start} ~ ${s.window.end}）`);
  lines.push(`- ${METRIC_LABELS.data_updated_at}：${formatUpdatedAt(s.data_updated_at)}`);
  lines.push('');
  lines.push('## 概览');
  lines.push(`- ${METRIC_LABELS.valid_observations}：${formatCount(s.summary.valid_observations)}`);
  lines.push(`- ${METRIC_LABELS.presence_rate}：${bpsToPercent(s.summary.presence_rate_bps)}`);
  lines.push(`- ${METRIC_LABELS.explicit_recommendation_rate}：${bpsToPercent(s.summary.explicit_recommendation_rate_bps)}`);
  lines.push(`- 结果稳定度：${s.summary.stability_explanation}`);
  if (!s.comparison.comparison_allowed) lines.push('- 本周期不与上周期直接比较（口径断点）。');
  lines.push('');
  lines.push('## 下一步行动');
  for (const a of s.next_actions) {
    lines.push(`- ${a.title}：${a.reason}${a.may_charge ? '' : '（不额外扣费）'}`);
  }
  lines.push('');
  lines.push('> 本概览仅包含总览与下一步行动，与页面使用同一数据口径；平台/问题证据/竞争/趋势请在页面内查看。');
  return lines.join('\n');
}

export default MonitoringObservationWorkbench;
