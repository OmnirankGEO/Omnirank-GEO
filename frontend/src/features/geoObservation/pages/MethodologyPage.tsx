/**
 * 方法说明页（路由由集成 Gate 在 /methodology 或帮助中心注册）。
 *
 * AI-3 无方法说明端点，故本页为**静态业务说明**（不发请求、不 404、不维护第三份公式）：
 * 十类结果解释、何时不下结论、采样节奏、已知局限、口径版本常量。
 * 技术枚举/版本值只放"技术附录"（管理员可展开），不抢主叙事。
 */

import { useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Panel, SectionHeader } from '../components/shared';
import { OutcomeBadge } from '../components/badges';
import { OUTCOME_LABELS, OUTCOME_HINTS, METRIC_LABELS, PAGE } from '../copy';
import type { OutcomeKey } from '../types';

// 口径/合同版本：与 AI-3 常量一致（contract 冻结值），非手写公式。
const METRIC_VERSION = 'geo-observation-metrics-v1';
const CONTRACT_VERSION = 'geo-observation-contract-v1.4';
const UPDATED_AT = '2026-07-17';

const OUTCOME_ORDER: OutcomeKey[] = [
  'recommended',
  'conditionally_recommended',
  'candidate_only',
  'mentioned_only',
  'criteria_only',
  'refused_no_evidence',
  'refused_risk',
  'not_mentioned',
  'entity_ambiguous',
  'engine_error',
];

const METRIC_EXPLAIN: { label: string; desc: string }[] = [
  { label: METRIC_LABELS.presence_rate, desc: '在有效样本里，AI 明确提到目标品牌的比例。' },
  { label: METRIC_LABELS.explicit_recommendation_rate, desc: '在有效样本里，AI 把品牌作为推荐对象的比例。' },
  { label: METRIC_LABELS.share_of_voice, desc: '本次样本里目标品牌被提及的次数，占全部品牌被提及次数的比例。它反映样本内热度，不是市场份额。' },
  { label: METRIC_LABELS.citation_rate, desc: '回答里给出可查引用来源的比例。' },
  { label: METRIC_LABELS.evidence_coverage_rate, desc: '有足够可核验证据支撑的比例。' },
];

const SAMPLING_TIERS: { tier: string; frequency: string; purpose: string }[] = [
  { tier: '核心交易/推荐问题', frequency: '较高频', purpose: '跟踪真实获客与模型更新' },
  { tier: '重要品类/比较/风险问题', frequency: '中频', purpose: '观察竞争与候选池' },
  { tier: '长尾问题', frequency: '较低频', purpose: '覆盖与新机会' },
  { tier: '全行业基线', frequency: '定期', purpose: '行业对照' },
];

const LIMITS: string[] = [
  'AI 回答存在随机性，单次结果不代表趋势，需要多点数据。',
  '不同平台可能采用官方接口或搜索增强方式采集，口径变化期间趋势会分段展示，前后不宜直接比较。',
  '样本、独立来源或时间跨度不足时不展示具体结论。',
  '竞争视图只展示本品牌的样本内提及占比，不展示其他客户的品牌名单。',
];

export interface MethodologyPageProps {
  /** 管理员视角显示技术附录（版本值）。 */
  showTechnicalAppendix?: boolean;
}

export function MethodologyPage({ showTechnicalAppendix = false }: MethodologyPageProps) {
  const [appendixOpen, setAppendixOpen] = useState(false);

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <div>
        <h1 className="text-xl font-bold text-foreground sm:text-2xl">{PAGE.methodology.title}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{PAGE.methodology.subtitle}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {PAGE.methodology.updatedAt} {UPDATED_AT}
        </p>
      </div>

      {/* 指标怎么算 */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader title="指标怎么算" />
        <ul className="mt-3 space-y-3">
          {METRIC_EXPLAIN.map((m) => (
            <li key={m.label}>
              <p className="text-sm font-medium text-foreground">{m.label}</p>
              <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">{m.desc}</p>
            </li>
          ))}
        </ul>
        <p className="mt-3 rounded-lg bg-muted/50 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
          有效样本不包含"品牌可能混淆"和"本次检测未完成"两类；它们不计入任何比例的分子或分母。无数据时显示"—"，不当作 0%。
        </p>
      </Panel>

      {/* 十类回答结果 */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader title={PAGE.methodology.outcomesTitle} />
        <ul className="mt-3 space-y-2.5">
          {OUTCOME_ORDER.map((k) => (
            <li key={k} className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-3">
              <span className="shrink-0 sm:w-40">
                <OutcomeBadge outcome={k} />
              </span>
              <span className="text-xs leading-relaxed text-muted-foreground">{OUTCOME_HINTS[k]}</span>
              <span className="sr-only">{OUTCOME_LABELS[k]}</span>
            </li>
          ))}
        </ul>
      </Panel>

      {/* 采样节奏 */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader title={PAGE.methodology.samplingTitle} hint="不同重要程度的问题采样频率不同，避免每天全量烧钱。" />
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="pb-2 pr-3 font-medium">问题类型</th>
                <th className="pb-2 pr-3 font-medium">频率</th>
                <th className="pb-2 font-medium">用途</th>
              </tr>
            </thead>
            <tbody>
              {SAMPLING_TIERS.map((t, i) => (
                <tr key={i} className="border-b border-border/50">
                  <td className="py-2 pr-3 text-foreground">{t.tier}</td>
                  <td className="py-2 pr-3 text-foreground">{t.frequency}</td>
                  <td className="py-2 text-muted-foreground">{t.purpose}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {/* 什么时候不下结论 */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader title="什么时候不下结论" />
        <ul className="mt-3 space-y-2 text-sm text-foreground">
          <li className="flex items-start gap-1.5">
            <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-muted-foreground" />
            <span>样本、独立来源或时间跨度不够时，显示"{PAGE.states.insufficient}"，不硬给趋势。</span>
          </li>
          <li className="flex items-start gap-1.5">
            <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-muted-foreground" />
            <span>口径断点（如平台模型升级）前后的结果不宜直接比较，页面会标注"数据口径已更新"并分段展示。</span>
          </li>
          <li className="flex items-start gap-1.5">
            <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-muted-foreground" />
            <span>单次的一次变化只显示"波动中"，需要复测确认后才算真实变化。</span>
          </li>
        </ul>
      </Panel>

      {/* 已知局限 */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader title={PAGE.methodology.limitsTitle} />
        <ul className="mt-3 space-y-2">
          {LIMITS.map((l, i) => (
            <li key={i} className="flex items-start gap-1.5 text-sm text-foreground">
              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-amber-500" />
              <span className="break-words">{l}</span>
            </li>
          ))}
        </ul>
      </Panel>

      {/* 技术附录（仅管理员） */}
      {showTechnicalAppendix ? (
        <Panel className="overflow-hidden">
          <button
            type="button"
            aria-expanded={appendixOpen}
            onClick={() => setAppendixOpen((v) => !v)}
            className="flex w-full items-center justify-between px-4 py-3 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
          >
            <span className="text-sm font-medium text-foreground">技术附录（管理员）</span>
            <ChevronDown className={`h-4 w-4 text-muted-foreground transition ${appendixOpen ? 'rotate-180' : ''}`} />
          </button>
          {appendixOpen ? (
            <dl className="grid grid-cols-1 gap-2 border-t border-border px-4 py-3 text-xs sm:grid-cols-2">
              <div className="flex justify-between gap-2">
                <dt className="text-muted-foreground">合同版本</dt>
                <dd className="text-foreground">{CONTRACT_VERSION}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted-foreground">{PAGE.methodology.version}</dt>
                <dd className="text-foreground">{METRIC_VERSION}</dd>
              </div>
            </dl>
          ) : null}
        </Panel>
      ) : null}
    </div>
  );
}

export default MethodologyPage;
