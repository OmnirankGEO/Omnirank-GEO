/**
 * 付费诊断结果增强模块："AI 推荐行为"（嵌入现役诊断报告，不另造第二份报告）。
 *
 * AI-3 无诊断专属端点，故按诊断的品牌复用真实 `/brands/{brand_id}/summary`（+ 可选行业基线对照），
 * 展示该品牌当前的 AI 推荐行为：提及/推荐/条件/拒绝分布、比例、稳定度、可执行下一步。
 * 禁止：公共飞轮内部过程、其他客户答案、内部成本/平台 API/模型路由、"买了就上榜"暗示。
 */

import { Stethoscope, PenLine } from 'lucide-react';
import { Panel, SectionHeader, MetricTile, Pill } from '../components/shared';
import { OutcomeDistribution } from '../components/OutcomeDistribution';
import { StabilityInline } from '../components/badges';
import { ModelShiftBanner, ResourceStateView } from '../components/states';
import { PAGE, METRIC_LABELS } from '../copy';
import { bpsToPercent, formatCount, formatUpdatedAt } from '../format';
import { useObservationResource } from '../hooks/useObservationResource';
import { observationClient, type ObservationClient } from '../client';
import { resolveNavigate, type NavTarget } from '../nav';
import type { PublicBaselineResponse } from '../types';

export interface DiagnosisRecommendationBehaviorProps {
  /** 诊断所属品牌（诊断报告已知）。AI-3 无诊断端点，故按品牌复用真实 summary。 */
  brandId: number;
  /** 可选：诊断的行业键，用于与匿名行业基线对照（真实公共端点）。 */
  industryKey?: string | null;
  client?: ObservationClient;
  onNavigate?: (target: NavTarget, ctx: Record<string, unknown>) => void;
  /** 暗部署阶段不在现役诊断报告中显示一块“暂不可用”的新模块。 */
  hideWhenUnavailable?: boolean;
}

export function DiagnosisRecommendationBehavior({
  brandId,
  industryKey,
  client = observationClient,
  onNavigate,
  hideWhenUnavailable = false,
}: DiagnosisRecommendationBehaviorProps) {
  const nav = resolveNavigate(onNavigate);
  const res = useObservationResource(
    (signal) => client.brandSummary(brandId, 'day', signal),
    [brandId],
    true,
  );
  const data = res.data;

  // 行业基线对照（真实公共端点，样本不足由 status 字段表达）
  const baselineRes = useObservationResource<PublicBaselineResponse>(
    (signal) => client.industryBaseline(industryKey as string, 'day', signal),
    [industryKey],
    !!industryKey,
  );

  const stateView = ResourceStateView({
    status: res.status,
    errorCode: res.errorCode,
    httpStatus: res.httpStatus,
    message: res.errorMessage,
    isEmpty: res.status === 'success' && !data,
    onRetry: res.reload,
    compact: true,
    emptyTitle: '本次诊断暂无 AI 推荐行为数据',
    emptyDesc: '完成一次带 AI 搜索的诊断后，这里会显示模型对该品牌的推荐行为。',
  });

  const baselineOk =
    baselineRes.status === 'success' && baselineRes.data?.status === 'ok' && baselineRes.data.baseline;

  /**
   * 🔴 [#87 2026-09-05] 接口少给一个字段 ⇒ **整份诊断报告页**进错误边界「页面出错了」。
   *
   * 原因:summary 的六个字段与 next_actions 的长度共八处,全部是不带守卫的直接取值。
   * `data` 本身判过(`isEmpty: !data`),但**"有 data" 不等于 "data 里每个字段都在"**。
   * 这个模块是**嵌进现役诊断报告**的 —— 它崩一块,用户失去的是整份报告。
   *
   * 处置:缺字段 ⇒ 该子块自己显示"暂无数据",其余照常渲染,不向上抛。
   */
  const summary = data?.summary ?? null;
  const rawOutcomes = data?.outcomes;
  const rawNextActions = data?.next_actions;
  // 不只判 null/undefined —— 字段在但不是数组时 .map 一样崩。
  const outcomes = Array.isArray(rawOutcomes) ? rawOutcomes : [];
  const nextActions = Array.isArray(rawNextActions) ? rawNextActions : [];

  if (hideWhenUnavailable && res.status === 'error' && res.httpStatus === 503) {
    return null;
  }

  return (
    <Panel className="p-4 sm:p-5">
      <SectionHeader
        title={
          <span className="inline-flex items-center gap-1.5">
            <Stethoscope className="h-4 w-4 text-brand" />
            AI 推荐行为
          </span>
        }
        hint="这个品牌当前在 AI 搜索里是被直接推荐、进候选，还是因证据/风险不推荐。"
      />

      <div className="mt-4">
        {stateView ? (
          stateView
        ) : data ? (
          <div className="space-y-4">
            {summary ? (
              summary.stability_status === 'shifted' ? (
                <ModelShiftBanner explanation={summary.stability_explanation} />
              ) : null
            ) : (
              <p data-testid="dxrb-summary-missing" className="text-xs text-muted-foreground">
                这一块的统计暂时取不到 · 报告其余内容不受影响
              </p>
            )}

            {summary ? (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <MetricTile label={METRIC_LABELS.valid_observations} value={formatCount(summary.valid_observations)} />
                <MetricTile label={METRIC_LABELS.presence_rate} value={bpsToPercent(summary.presence_rate_bps)} />
                <MetricTile
                  label={METRIC_LABELS.explicit_recommendation_rate}
                  value={bpsToPercent(summary.explicit_recommendation_rate_bps)}
                />
                <div className="rounded-lg border border-border bg-card p-3.5 sm:p-4">
                  <span className="text-xs text-muted-foreground">结果稳定度</span>
                  <div className="mt-2">
                    <StabilityInline status={summary.stability_status} />
                  </div>
                </div>
              </div>
            ) : null}

            <div>
              <h4 className="mb-3 text-sm font-semibold text-foreground">推荐行为分布</h4>
              <OutcomeDistribution outcomes={outcomes} />
            </div>

            {/* 行业匿名基线对比（真实端点；样本不足明确说明，不 fallback） */}
            {industryKey ? (
              <Panel className="p-3.5">
                <h4 className="mb-2 text-sm font-semibold text-foreground">与行业基线对比</h4>
                {summary && baselineOk ? (
                  <div className="flex flex-wrap items-center gap-4 text-sm">
                    <span className="text-foreground">
                      本品牌 {METRIC_LABELS.presence_rate}：
                      <span className="font-semibold">{bpsToPercent(summary.presence_rate_bps)}</span>
                    </span>
                    <span className="text-muted-foreground">
                      行业匿名基线：{bpsToPercent(baselineRes.data!.baseline!.presence_rate_bps)}
                    </span>
                    <span className="text-[11px] text-muted-foreground">{baselineRes.data!.sample_scope}</span>
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    {baselineRes.data?.message || '行业匿名基线样本不足，本次暂不做对比。'}
                  </p>
                )}
              </Panel>
            ) : null}

            {/* 可执行下一步（来自 summary.next_actions，真实） */}
            {nextActions.length > 0 ? (
              <div>
                <h4 className="mb-2 text-sm font-semibold text-foreground">可执行的下一步</h4>
                <ul className="space-y-2">
                  {nextActions.map((a, i) => (
                    <li
                      key={`${a.action}-${i}`}
                      className="flex flex-col gap-2 rounded-lg bg-muted/50 p-3 sm:flex-row sm:items-center sm:justify-between"
                    >
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-medium text-foreground">{a.title}</span>
                          {!a.may_charge ? <Pill tone="good">{PAGE.actions.noCharge}</Pill> : null}
                        </div>
                        <p className="mt-0.5 text-xs text-muted-foreground break-words">{a.reason}</p>
                      </div>
                      {a.action === 'add_evidence' || a.action === 'add_content' ? (
                        <button
                          type="button"
                          onClick={() => nav('writing', { brandId, action: a.action })}
                          className="inline-flex min-h-[34px] shrink-0 items-center gap-1.5 self-start rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-card/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                        >
                          <PenLine className="h-3.5 w-3.5" />
                          {PAGE.actions.goWriting}
                        </button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

export default DiagnosisRecommendationBehavior;
