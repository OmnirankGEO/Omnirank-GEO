// 营销军师 · 营销效果复盘(相关口径 · 非因果宣称 · 主题感知亮暗)
// 自取 fetchEffect() + fetchWeeklyReport();生成复盘报告走 generateWeeklyReport()。
// 三指标均为「相关转化」,不做因果宣称,不出承诺词。
import { useCallback, useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { Users, Activity, Wallet, RefreshCw, FileText, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import ReactMarkdown from '@/components/SafeMarkdown'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { fetchEffect, fetchWeeklyReport, generateWeeklyReport } from '../api'
import {
  FadeIn, Panel, StatCard, LoadingState, EmptyState, ErrorState,
} from './ui'

// —— fetchEffect() 返回结构(与 api.ts 内联签名保持一致) ——
interface EffectData {
  funnel_4level: Record<string, number>
  kpi: Record<string, number>
  attribution: { skill_pack: string; measured: number; converted: number; conv_rate_pct: number }[]
  ai_review: { markdown?: string; summary?: string }
  control_group: { enabled: boolean; note: string }
}

// 漏斗 4 级配置(相关口径 · 从 targeted 起算比例)
const FUNNEL_STEPS: { key: string; label: string }[] = [
  { key: 'targeted', label: '目标人数' },
  { key: 'reached', label: '成功触达' },
  { key: 'revisit', label: 'N 日回访' },
  { key: 'recharge', label: 'N 日充值' },
]

function num(v: number | undefined): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

function pct(part: number, whole: number): string {
  if (whole <= 0) return '0%'
  return `${((part / whole) * 100).toFixed(1)}%`
}

export function Effect() {
  const [data, setData] = useState<EffectData | null>(null)
  const [report, setReport] = useState<{ markdown?: string } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [eff, rep] = await Promise.all([fetchEffect(), fetchWeeklyReport()])
      setData(eff as EffectData)
      setReport(rep.report || null)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const onGenerate = useCallback(async () => {
    setGenerating(true)
    try {
      const r = await generateWeeklyReport()
      toast.success(`已生成复盘报告 · ${r.week}`)
      await load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '生成失败')
    } finally {
      setGenerating(false)
    }
  }, [load])

  const onDistill = useCallback(() => {
    // v1 占位:沉淀为策略规则(暂无副作用)
    toast.success('已记录,可在设置→技能包查看')
  }, [])

  if (loading) return <LoadingState text="复盘数据加载中…" />
  if (error) return <ErrorState text={error} onRetry={() => void load()} />
  if (!data) return <EmptyState title="数据积累中,暂无复盘" hint="投放触达累积到一定样本后,这里会展示相关转化漏斗与 AI 复盘。" />

  const kpi = data.kpi || {}
  const funnel = data.funnel_4level || {}
  const targeted = num(funnel.targeted)
  const reviewMd = data.ai_review?.markdown
  const weeklyMd = report?.markdown

  return (
    <div className="space-y-5">
      {/* 顶部动作 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-foreground">营销效果复盘</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            以下均为触达后的<span className="text-foreground">相关转化</span>口径,非因果宣称。
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={() => void onGenerate()} disabled={generating}>
          <RefreshCw className={cn('mr-1.5 size-3.5', generating && 'animate-spin')} />
          {generating ? '生成中…' : '生成复盘报告'}
        </Button>
      </div>

      {/* 1) KPI 行 */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatCard label="触达人数" value={num(kpi.reached).toLocaleString()} icon={<Users className="size-3.5" />} delay={0} />
        <StatCard label="相关转化" value={num(kpi.correlated_conversion).toLocaleString()} icon={<Activity className="size-3.5" />} sub="触达后发生的相关行为(非因果)" delay={0.05} />
        <StatCard label="充值相关转化" value={num(kpi.recharge_conversion).toLocaleString()} icon={<Wallet className="size-3.5" />} sub="触达后发生的充值(相关口径)" delay={0.1} accent />
      </div>

      {/* 2) 转化漏斗 4 级 */}
      <Panel title="转化漏斗(4 级 · 相关口径)">
        {targeted <= 0 ? (
          <EmptyState title="暂无漏斗数据" hint="目标人群产生触达后,这里会显示各级相关转化。" />
        ) : (
          <div className="space-y-2.5">
            {FUNNEL_STEPS.map((step, i) => {
              const v = num(funnel[step.key])
              const ratio = targeted > 0 ? Math.min(1, v / targeted) : 0
              return (
                <FadeIn key={step.key} delay={i * 0.05}>
                  <div className="flex items-center gap-3">
                    <div className="w-20 shrink-0 text-xs text-muted-foreground">{step.label}</div>
                    <div className="relative h-7 flex-1 overflow-hidden rounded-md bg-muted">
                      <motion.div
                        initial={{ width: 0 }}
                        animate={{ width: `${Math.max(ratio * 100, v > 0 ? 4 : 0)}%` }}
                        transition={{ duration: 0.4, delay: i * 0.05 }}
                        className={cn('h-full rounded-md', i === FUNNEL_STEPS.length - 1 ? 'bg-brand' : 'bg-primary/70')}
                      />
                      <div className="absolute inset-0 flex items-center px-2.5 text-[11px] font-medium tabular-nums text-foreground">
                        {v.toLocaleString()}
                      </div>
                    </div>
                    <div className="w-14 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                      {pct(v, targeted)}
                    </div>
                  </div>
                </FadeIn>
              )
            })}
          </div>
        )}
        <p className="mt-3 text-[11px] text-muted-foreground">各环节为触达后相关转化,非因果宣称。</p>
      </Panel>

      {/* 3) AI 复盘总结 */}
      <Panel
        title="AI 复盘总结"
        action={
          <Button size="sm" variant="secondary" onClick={onDistill}>
            <Sparkles className="mr-1.5 size-3.5" />
            沉淀为策略规则
          </Button>
        }
      >
        {reviewMd || weeklyMd ? (
          <FadeIn>
            <div className="prose prose-sm max-w-none dark:prose-invert">
              <ReactMarkdown>
                {reviewMd || weeklyMd || ''}
              </ReactMarkdown>
            </div>
          </FadeIn>
        ) : (
          <EmptyState title="数据积累中,暂无复盘" hint="点击右上角「生成复盘报告」,或等待样本积累后自动生成。" />
        )}
        <p className="mt-3 border-t border-border pt-2 text-[11px] text-muted-foreground">
          AI 分析仅供参考,请结合业务实际判断。
        </p>
      </Panel>

      {/* 4) 框架标签归因 */}
      <Panel title="框架标签归因">
        {data.attribution && data.attribution.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">技能包</th>
                  <th className="py-2 pr-3 text-right font-medium">已测度</th>
                  <th className="py-2 pr-3 text-right font-medium">相关转化</th>
                  <th className="py-2 text-right font-medium">相关转化率</th>
                </tr>
              </thead>
              <tbody>
                {data.attribution.map((row, i) => (
                  <tr key={`${row.skill_pack}-${i}`} className="border-b border-border/60 last:border-0">
                    <td className="py-2 pr-3 text-foreground">{row.skill_pack}</td>
                    <td className="py-2 pr-3 text-right tabular-nums text-muted-foreground">{num(row.measured).toLocaleString()}</td>
                    <td className="py-2 pr-3 text-right tabular-nums text-muted-foreground">{num(row.converted).toLocaleString()}</td>
                    <td className="py-2 text-right tabular-nums text-foreground">
                      {/* 样本 < 10 隐藏百分比,避免小样本误导 */}
                      {num(row.measured) < 10 ? <span className="text-muted-foreground">样本不足</span> : `${num(row.conv_rate_pct).toFixed(1)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="样本不足" hint="技能包投放测度累积后展示归因,当前样本不足以呈现。" />
        )}
      </Panel>

      {/* 5) 对照组 / 实验组对比(功能开关) */}
      <Panel title="对照组 / 实验组对比">
        {data.control_group?.enabled ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {(['对照组', '实验组'] as const).map((g, i) => (
              <FadeIn key={g} delay={i * 0.05}>
                <div className="rounded-lg border border-border bg-muted/30 p-4">
                  <div className="text-xs font-medium text-muted-foreground">{g}</div>
                  <div className="mt-2 text-sm text-foreground">相关转化对比展示区</div>
                  <div className="mt-1 text-[11px] text-muted-foreground">分组样本累积后填充三指标对比。</div>
                </div>
              </FadeIn>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-border bg-muted/30 p-4">
            <p className="text-xs text-muted-foreground">
              {data.control_group?.note || '对照组模块未启用。'}
            </p>
            <p className="mt-1.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <FileText className="size-3.5" />
              当前展示相关转化三指标。
            </p>
          </div>
        )}
      </Panel>
    </div>
  )
}
