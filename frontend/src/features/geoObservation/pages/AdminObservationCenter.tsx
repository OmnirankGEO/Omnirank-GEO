/**
 * 管理员观测治理中心（建议路由 /admin/geo-observation-center，归入 GEO 调研/监测分组）。
 * 五 Tab：运行概览 | 平台与模型 | 样本治理 | 行业趋势 | 内容与媒体机会。
 * 首屏先看待审、撤回未对账、平台漂移、调度健康；写操作展示影响预览与 reason。
 */

import { useState } from 'react';
import { Gauge, Server, ClipboardCheck, TrendingUp, Lightbulb } from 'lucide-react';
import { TabStrip, TabPanel, type TabDef } from '../components/TabStrip';
import { Pill } from '../components/shared';
import { ResourceStateView } from '../components/states';
import { AdminOverviewTab } from './admin/AdminOverviewTab';
import { AdminPlatformsTab } from './admin/AdminPlatformsTab';
import { AdminGovernanceTab } from './admin/AdminGovernanceTab';
import { AdminIndustryTab } from './admin/AdminIndustryTab';
import { AdminOpportunitiesTab } from './admin/AdminOpportunitiesTab';
import { PAGE } from '../copy';
import { formatUpdatedAt } from '../format';
import { useObservationResource } from '../hooks/useObservationResource';
import { observationClient, type ObservationClient } from '../client';
import type { NavTarget } from './workbench/ActionsTab';

type TabKey = 'overview' | 'platforms' | 'governance' | 'industry' | 'opportunities';

const TABS: TabDef<TabKey>[] = [
  { key: 'overview', label: PAGE.admin.tabs.overview, icon: Gauge },
  { key: 'platforms', label: PAGE.admin.tabs.platforms, icon: Server },
  { key: 'governance', label: PAGE.admin.tabs.governance, icon: ClipboardCheck },
  { key: 'industry', label: PAGE.admin.tabs.industry, icon: TrendingUp },
  { key: 'opportunities', label: PAGE.admin.tabs.opportunities, icon: Lightbulb },
];

const READINESS_TONE: Record<string, Parameters<typeof Pill>[0]['tone']> = {
  ready: 'good',
  attention_required: 'warn',
  unavailable: 'bad',
};

export interface AdminObservationCenterProps {
  client?: ObservationClient;
  onNavigate?: (target: NavTarget, ctx: Record<string, unknown>) => void;
}

export function AdminObservationCenter({
  client = observationClient,
  onNavigate,
}: AdminObservationCenterProps) {
  const [activeTab, setActiveTab] = useState<TabKey>('overview');

  const overviewRes = useObservationResource((signal) => client.adminOverview(signal), [], true);
  const overview = overviewRes.data;

  const overviewState = ResourceStateView({
    status: overviewRes.status,
    errorCode: overviewRes.errorCode,
    httpStatus: overviewRes.httpStatus,
    message: overviewRes.errorMessage,
    isEmpty: overviewRes.status === 'success' && !overview,
    onRetry: overviewRes.reload,
  });

  const readinessLabel =
    overview
      ? PAGE.admin.readiness[overview.readiness as keyof typeof PAGE.admin.readiness] || overview.readiness
      : '';

  return (
    <div className="space-y-5">
      {/* 页头 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-xl font-bold text-foreground sm:text-2xl">{PAGE.admin.title}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{PAGE.admin.subtitle}</p>
        </div>
        {overview ? (
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              系统就绪度
              <Pill tone={READINESS_TONE[overview.readiness] || 'muted'}>{readinessLabel}</Pill>
            </span>
            <span className="text-xs text-muted-foreground">
              聚合更新于 {formatUpdatedAt(overview.aggregate_updated_at)}
            </span>
          </div>
        ) : null}
      </div>

      {overviewState && activeTab === 'overview' ? (
        overviewState
      ) : (
        <>
          <TabStrip tabs={TABS} active={activeTab} onChange={setActiveTab} idBase="geoobs-admin" />
          <div className="mt-4">
            <TabPanel idBase="geoobs-admin" tabKey="overview" active={activeTab}>
              {overviewState ? overviewState : overview ? <AdminOverviewTab data={overview} /> : null}
            </TabPanel>
            <TabPanel idBase="geoobs-admin" tabKey="platforms" active={activeTab}>
              {overviewState ? overviewState : overview ? <AdminPlatformsTab overview={overview} client={client} /> : null}
            </TabPanel>
            <TabPanel idBase="geoobs-admin" tabKey="governance" active={activeTab}>
              {overviewState ? overviewState : overview ? <AdminGovernanceTab overview={overview} /> : null}
            </TabPanel>
            <TabPanel idBase="geoobs-admin" tabKey="industry" active={activeTab}>
              <AdminIndustryTab client={client} />
            </TabPanel>
            <TabPanel idBase="geoobs-admin" tabKey="opportunities" active={activeTab}>
              <AdminOpportunitiesTab client={client} onNavigate={onNavigate} />
            </TabPanel>
          </div>
        </>
      )}
    </div>
  );
}

export default AdminObservationCenter;
