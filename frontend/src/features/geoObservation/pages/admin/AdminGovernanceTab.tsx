/**
 * 管理员 · 样本治理。
 *
 * AI-3 是只读产品 API；晋升审核/撤回等**写**操作属 AI-2 治理接口，尚未交付。
 * 本页因此为 capability-pending：
 *  - 只读展示各状态数量（来自 AI-3 overview，真实）；
 *  - 明确说明写操作接口就绪后接入；
 *  - 绝不显示会 404 的审核按钮，不伪造队列/理由表单。
 */

import { ClipboardList, Lock } from 'lucide-react';
import { Panel, SectionHeader } from '../../components/shared';
import { OBSERVATION_CAPABILITIES } from '../../capabilities';
import { PAGE } from '../../copy';
import { formatCount } from '../../format';
import type { AdminOverview } from '../../types';

export function AdminGovernanceTab({ overview }: { overview: AdminOverview }) {
  const c = overview.counts;
  const rows: { label: string; value: number; hint: string }[] = [
    { label: PAGE.admin.kpi.pending_review, value: c.pending_review, hint: '等待人工审核是否进入匿名公共层的来源。' },
    { label: PAGE.admin.kpi.private_only, value: c.private_only, hint: '只服务该客户、不进入公共层的来源。' },
    { label: PAGE.admin.kpi.rejected, value: c.rejected, hint: '因隐私或质量原因被拒绝晋升的来源。' },
    { label: PAGE.admin.kpi.withdrawn, value: c.withdrawn, hint: '已撤回来源；对公开聚合贡献严格为零。' },
    { label: PAGE.admin.kpi.stuck_claims, value: c.stuck_claims, hint: '处理中长时间未完成的任务，需人工介入。' },
  ];

  return (
    <div className="space-y-4">
      <SectionHeader
        title={
          <span className="inline-flex items-center gap-1.5">
            <ClipboardList className="h-4 w-4 text-muted-foreground" />
            样本治理概况
          </span>
        }
        hint="按状态查看样本来源数量。"
      />

      {/* capability 未接入：只读 + 明确说明，无审核写按钮 */}
      {!OBSERVATION_CAPABILITIES.sampleGovernanceWrite ? (
        <div className="flex items-start gap-2 rounded-lg border border-border bg-muted/40 px-3.5 py-2.5">
          <Lock className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <p className="text-xs leading-relaxed text-muted-foreground">{PAGE.admin.governancePending}</p>
        </div>
      ) : null}

      <Panel className="p-2">
        <ul className="divide-y divide-border">
          {rows.map((r) => (
            <li key={r.label} className="flex items-center justify-between gap-3 px-3 py-3">
              <div className="min-w-0">
                <div className="text-sm text-foreground">{r.label}</div>
                <div className="mt-0.5 text-xs text-muted-foreground">{r.hint}</div>
              </div>
              <span className="shrink-0 text-lg font-semibold tabular-nums text-foreground">{formatCount(r.value)}</span>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
