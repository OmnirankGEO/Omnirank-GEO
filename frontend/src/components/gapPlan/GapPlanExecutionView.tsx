/**
 * 交付计划 · 服务商执行面【纯展示层】
 * (返工 WO_GAPPLAN_REWORK_2026-08-11 · Review 裁定 963f7b27 假修复)
 *
 * 🔴 为什么把渲染从 `GapPlanExecution` 里拆出来:
 *    上一轮我声称"可选链防白屏",但守卫 `?.open` 之后分支体内 `{...execution_gate.hint}`
 *    仍是裸解引用 —— 崩溃点只是从 `.open` 挪到下一行,该 commit 唯一要防的场景净效果为零。
 *    **它能溜过去,根本原因是当时没有一条判据能真的把这个场景渲染一遍**:
 *    容器组件自己 fetch,`renderToString` 下 useEffect 不跑,快照恒 null,渲染不出任何东西。
 *    于是判据只能退化成正则扫写法,而"改了写法 + 变异杀得掉"证明的是门禁认得出写法,
 *    不是 bug 已修。
 *    拆出这个无副作用、快照由 props 注入的视图之后,
 *    `scripts/test-gap-plan-render.mjs` 可以直接构造「不含 execution_gate 键」的快照真渲染,
 *    断言①不抛 ②渲染的是执行面。判据从"扫源码"变成"跑一遍"。
 *
 * 本组件:不 fetch、不写状态到服务端、不认识 quoteId。只吃 props,只回调。
 */
import { useState } from 'react';
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import type {
  GapAction, GapPlanError, GapPlanItem, GapPlanSnapshot,
} from '@/lib/gapPlanApi';
import { GapTaskCard } from './GapTaskCard';
import { toneClasses } from './tone';

/** 分档决策的三态。导出是为了让判据能按语义断言,而不是按写法扫正则。 */
export type GapExecutionMode = 'loading' | 'error_only' | 'gate_hint' | 'execution';

/**
 * 🔴 三分支,不是二分支(返工要求 1)。三态的**失败方向**各不相同,必须分开写:
 *
 *   | snapshot.execution_gate | 行为 |
 *   |---|---|
 *   | `undefined`(旧后端 / 蓝绿回滚窗口) | **直接渲染执行面 = 旧行为原样** |
 *   | 有值且 open === false               | 只渲染一句提示 |
 *   | 有值且 open === true                | 渲染执行面 |
 *
 * 上一轮写的 `!snapshot.execution_gate?.open` 把第一行并进了第二行 —— 那等于
 * 让旧后端组合下**所有报价(含已付款)退化成一句提示**,是拿功能倒退换不崩。
 * 旧后端就该给旧行为:它本来也不知道有分档这回事。
 */
export function resolveExecutionMode(
  snapshot: GapPlanSnapshot | null,
  error: GapPlanError | null,
  loading: boolean,
): GapExecutionMode {
  if (!snapshot) return error ? 'error_only' : 'loading';
  const gate = snapshot.execution_gate;
  if (!gate) return 'execution';          // 旧后端不下发 → 旧行为
  return gate.open ? 'execution' : 'gate_hint';
}

interface Props {
  snapshot: GapPlanSnapshot | null;
  error: GapPlanError | null;
  loading: boolean;
  busy: boolean;
  onReload: () => void;
  onAction: (action: GapAction, item: GapPlanItem, payload?: { url?: string }) => void;
}

export function GapPlanExecutionView({
  snapshot, error, loading, busy, onReload, onAction,
}: Props) {
  const [showEvidence, setShowEvidence] = useState(false);

  const mode = resolveExecutionMode(snapshot, error, loading);

  if (mode === 'loading') {
    if (!loading) return null;
    return (
      // 🔴 加载态也带锚点(返工订正 2):原来这一态没有,小榜「交付计划在哪」在
      //    加载那一瞬间高亮不到任何元素。四态齐了,向导才是全程有靶子的。
      <Card data-help-target="gap-plan-section">
        <CardHeader className="pb-3"><CardTitle className="text-base">交付计划</CardTitle></CardHeader>
        <CardContent><p className="text-sm text-muted-foreground">正在整理这个问题的真实回答…</p></CardContent>
      </Card>
    );
  }

  if (mode === 'error_only') {
    return (
      <Card data-help-target="gap-plan-section">
        <CardHeader className="pb-3"><CardTitle className="text-base">交付计划</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-foreground">{error?.label}</p>
          <p className="text-xs text-muted-foreground">{error?.message}</p>
          <Button size="sm" variant="outline" onClick={onReload} disabled={loading}>
            <RefreshCw className="mr-1 h-3 w-3" />{error?.primary_action?.label || '重新获取'}
          </Button>
        </CardContent>
      </Card>
    );
  }

  if (!snapshot) return null;                 // 类型收窄用,mode 已排除

  if (mode === 'gate_hint') {
    // 🔴 只有到了这一支,`execution_gate` 才被证明存在(resolveExecutionMode 已排除
    //    undefined)。可选链在这里是**冗余而非防护** —— 真正的防护是上面那次分流。
    return (
      <Card data-help-target="gap-plan-section">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">交付计划</CardTitle>
        </CardHeader>
        <CardContent>
          {/* 文案由服务端字典下发,前端不写第二份中文串 */}
          <p className="text-sm text-muted-foreground leading-relaxed">
            {snapshot.execution_gate?.hint}
          </p>
        </CardContent>
      </Card>
    );
  }

  const capTone = toneClasses(snapshot.capacity.tone);

  return (
    <Card data-help-target="gap-plan-section">
      <CardHeader className="pb-3 flex-row items-center justify-between space-y-0">
        <CardTitle className="text-base">交付计划</CardTitle>
        <Button size="sm" variant="ghost" onClick={onReload} disabled={loading}
                aria-label="重新获取交付计划">
          <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
        </Button>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* 快照过期但有旧结果:提示而不清空 */}
        {error && (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2">
            <p className="text-xs text-amber-700 dark:text-amber-300">
              {error.message}（下面显示的是上一次的有效结果）
            </p>
          </div>
        )}

        {/* ── 第一层:一句话诊断 + 容量 ── */}
        <div className="space-y-2">
          <p className="text-sm text-foreground leading-relaxed">{snapshot.summary.headline}</p>
          <p className="text-sm text-muted-foreground leading-relaxed">{snapshot.summary.next_step}</p>

          <div className="flex flex-wrap items-center gap-2 pt-1">
            <span className={cn('inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs',
                                capTone.badge)}>
              <span className={cn('h-1.5 w-1.5 rounded-full', capTone.dot)} aria-hidden="true" />
              {snapshot.capacity.label}
            </span>
            {snapshot.query_family.display_query && (
              <span className="text-xs text-muted-foreground">
                目标问题：{snapshot.query_family.display_query}
              </span>
            )}
          </div>

          {snapshot.summary.capacity_notice && (
            <p className="text-xs text-muted-foreground">{snapshot.summary.capacity_notice}</p>
          )}

          {/* 历史超额如实显示,不假装守恒(合同:12/137 张单的交付槽真的超过冻结容量) */}
          {snapshot.capacity.over_delivered_articles > 0 && (
            <p className="text-xs text-muted-foreground">
              这个词包已交付 {snapshot.capacity.consumed_articles} 篇，超出当时冻结的{' '}
              {snapshot.capacity.authorized_articles} 篇。历史记录保持原样，不影响后续安排。
            </p>
          )}

          {/* 🔴 用不满**不是失败**,但原因必须露出来 ——
              说不出为什么会带一条「原因待确认」,那正是要让人看见的东西。 */}
          {snapshot.shortfall?.shortfall_articles > 0 && (
            <div className="rounded-md border border-border bg-muted/30 px-3 py-2">
              <p className="text-xs text-foreground">
                还有 {snapshot.shortfall.shortfall_articles} 篇没有安排。条数保留，不计作未完成。
              </p>
              <ul className="mt-1 space-y-0.5">
                {snapshot.shortfall.reasons.map((r) => (
                  <li key={r.code} className="text-[11px] text-muted-foreground">
                    · {r.label}：{r.explanation}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {/* ── 第二层:任务卡流 ── */}
        <div className="space-y-3">
          {snapshot.items.map((item) => (
            <GapTaskCard
              key={item.plan_item_id}
              item={item}
              evidence={snapshot.evidence?.[item.plan_item_id]}
              busy={busy}
              onAction={onAction}
            />
          ))}
          {!snapshot.items.length && (
            <p className="text-sm text-muted-foreground">
              眼下没有值得写的新缺口。条数保留，30 天回查后再评估。
            </p>
          )}
        </div>

        {/* ── 第三层:依据抽屉(默认折叠) ── */}
        <div className="border-t border-border pt-3">
          <button
            type="button"
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            aria-expanded={showEvidence}
            onClick={() => setShowEvidence((v) => !v)}
          >
            {showEvidence ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
            为什么这样安排
          </button>

          {showEvidence && (
            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-live="polite">
              {snapshot.evidence_platforms.map((p) => (
                <div key={p.platform_label} className="rounded-md border border-border bg-muted/20 p-3">
                  <p className="text-xs font-medium text-foreground">{p.platform_label}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    当前候选位置 {p.candidate_count} 个
                  </p>
                  <p className="text-xs text-muted-foreground">
                    客户是否出现：{snapshot.summary.customer_present ? '已出现' : '尚未出现'}
                  </p>
                  {p.last_observed_at && (
                    <p className="text-xs text-muted-foreground">
                      最近观察：{String(p.last_observed_at).slice(0, 10)}
                    </p>
                  )}
                  {p.citation_domains.length > 0 && (
                    <p className="mt-1 text-[11px] text-muted-foreground break-words">
                      常被引用：{p.citation_domains.slice(0, 4).join('、')}
                    </p>
                  )}
                </div>
              ))}
              {!snapshot.evidence_platforms.length && (
                <p className="text-xs text-muted-foreground">
                  还在整理这个问题的真实回答，完成后会显示各平台的观察结果。
                </p>
              )}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export default GapPlanExecutionView;
