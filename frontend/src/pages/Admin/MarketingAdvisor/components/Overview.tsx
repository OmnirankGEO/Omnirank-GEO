// 营销军师 · 今日军情首页(对齐参考图 01 · 主题感知 · 只显真实事实数)
// 拍板口径:① 不做「预计今日可带来转化 ¥」预测式金额;② 「较昨日」无数据源不显示;
//          ③ 转化统计口径为「相关转化」(非因果归因),禁「带来收入」;④ 英文 key/status 零裸露。
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import {
  RefreshCw, Radar, Gem, TrendingUp, Moon, Tag, Plus, ArrowRight,
  Bell, Wallet, CheckCheck, Image as ImageIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import type {
  OverviewData, MarketingCase, MarketingEvent, OpportunityGroup, RiskLevel,
} from '../types'
import { GROUP_LABEL } from '../types'
import { fetchOverview, fetchCases, fetchEvents, runPatrol } from '../api'
import {
  FadeIn, Panel, LoadingState, EmptyState, ErrorState,
  CaseStatusBadge, PowerAndYuan,
} from './ui'

// —— 本地极简相对时间(iso → “x 分钟前”)——
function timeAgo(iso: string): string {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (s < 60) return '刚刚'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  return `${d} 天前`
}

// —— 数据更新时间(本次加载时刻 → “2026-07-04 10:30”)——
function fmtDateTime(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// —— 今日机会四卡(图 01):图标 + 彩色底片 ——
const GROUP_ICON: Record<OpportunityGroup, typeof Gem> = {
  high_value: Gem, convertible: TrendingUp, dormant: Moon, offer: Tag,
}
const GROUP_CHIP: Record<OpportunityGroup, string> = {
  high_value: 'bg-violet-500/10 text-violet-500',
  convertible: 'bg-sky-500/10 text-sky-500',
  dormant: 'bg-amber-500/10 text-amber-500',
  offer: 'bg-brand/10 text-brand',
}
const GROUP_ORDER: OpportunityGroup[] = ['high_value', 'convertible', 'dormant', 'offer']

// —— rule_key → 触达建议人话(英文零露出;兼容规格 key 与后端真实 key 两套写法)——
const RULE_HUMAN: Record<string, string> = {
  pending_orders: '挽回挂单', pending_recover: '挽回挂单',
  trial_exhausted_active: '推动首充',
  register_no_diagnosis: '引导首次体验',
  first_compliance: '达标祝贺', first_compliant: '达标祝贺',
  provider_restock: '提醒补货',
  no_recharge_streak: '充值促活',
  high_value_silent: '唤回大客户',
  written_not_published: '发布教育',
  low_feature_usage: '功能种草', feature_underused: '功能种草',
  conversion_drop: '转化诊断', conversion_low: '转化诊断',
}
const ruleHuman = (key: string): string => RULE_HUMAN[key] ?? '营销建议'

// —— event_type → 中文动态标签(规格 10 项 + 后端真实事件别名;未知显示「动态」)——
const EVENT_LABEL: Record<string, string> = {
  suggestion_generated: '建议生成',
  approved: '审批通过',
  rejected: '已驳回',
  executed: '执行完成',
  touch_sent: '触达发送',
  grant_applied: '算力发放',
  campaign_activated: '活动上线',
  campaign_ended: '活动下线',
  weekly_report: '周报',
  material_reported: '物料举报',
  // 后端真实存在的其余事件(避免英文裸露)
  approval_blocked: '审批拦截',
  changes_requested: '退回修改',
  campaign_drafted: '活动草拟',
  campaign_budget_exhausted: '预算用尽',
  grant_failed: '发放失败',
  grant_reversed: '发放回冲',
  grant_reconciled: '发放对账',
  grant_batch_over: '发放限额',
  grant_over_budget: '发放超限',
  kill_switch: '急停',
  measured: '效果测量',
  policy_changed: '策略变更',
  skill_pack_upserted: '技能包更新',
}
const eventLabel = (t: string): string => EVENT_LABEL[t] ?? '动态'

const EVENT_BADGE: Record<MarketingEvent['severity'], string> = {
  info: 'bg-muted text-muted-foreground',
  warn: 'bg-amber-500/10 text-amber-600',
  error: 'bg-rose-500/10 text-rose-600',
  security: 'bg-violet-500/10 text-violet-600',
}

// —— 优先级 badge(risk → 高/中/低)——
const PRIORITY: Record<RiskLevel, { label: string; cls: string }> = {
  high: { label: '高', cls: 'bg-rose-500/10 text-rose-600 border-rose-500/30' },
  med: { label: '中', cls: 'bg-amber-500/10 text-amber-600 border-amber-500/30' },
  low: { label: '低', cls: 'bg-muted text-muted-foreground border-border' },
}

// —— 客户列人话:数字指纹 → 「用户 #123」;否则用 audience 定义(真实数据,不造)——
function humanCustomer(c: MarketingCase): string {
  const fp = String(c.fingerprint || '')
  if (/^\d+$/.test(fp)) return `用户 #${fp}`
  const m = fp.match(/^u(?:ser)?[:_]?(\d+)$/)
  if (m) return `用户 #${m[1]}`
  return c.audience_jsonb?.definition || '目标客户群'
}

// —— 海报模板占位卡(静态展示 · brand 色系渐变)——
const POSTER_TEMPLATES: { name: string; grad: string }[] = [
  { name: '沙龙海报', grad: 'from-brand via-emerald-500 to-teal-500' },
  { name: '充值活动海报', grad: 'from-emerald-600 via-brand to-lime-400' },
  { name: '朋友圈文案', grad: 'from-teal-600 via-emerald-500 to-brand' },
]

// 转化环节顺序(注册 → 激活 → 首次消费 → 首充 → 复购)
const FUNNEL_STEPS: { key: string; label: string }[] = [
  { key: 'registered', label: '注册' },
  { key: 'activated', label: '激活' },
  { key: 'first_spend', label: '首次消费' },
  { key: 'first_charge', label: '首充' },
  { key: 'repurchase', label: '复购' },
]

// 「查看全部 →」小链接(跳分区路由)
function SeeAll({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline"
    >
      查看全部 <ArrowRight className="size-3.5" />
    </button>
  )
}

export function Overview({ onOpenCase, onGotoMaterials: _onGotoMaterials }: {
  onOpenCase: (id: number) => void
  onGotoMaterials: () => void // 保留现有 props 签名;跳转已统一改走 useNavigate 路由
}) {
  const navigate = useNavigate()
  const [overview, setOverview] = useState<OverviewData | null>(null)
  const [pending, setPending] = useState<MarketingCase[]>([])
  const [events, setEvents] = useState<MarketingEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [patrolling, setPatrolling] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [ov, cs, ev] = await Promise.all([
        fetchOverview(),
        fetchCases('pending'),
        fetchEvents(20),
      ])
      setOverview(ov)
      setPending(cs.cases)
      setEvents(ev.events)
      setLastUpdated(new Date())
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const onPatrol = useCallback(async () => {
    setPatrolling(true)
    try {
      const r = await runPatrol()
      toast.success(
        r.cases_opened > 0
          ? `巡逻完成 · 命中 ${r.signals_matched} 条信号,新立 ${r.cases_opened} 个待审批方案`
          : `巡逻完成 · 命中 ${r.signals_matched} 条信号,暂无需要新立的方案`,
      )
      await load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '巡逻失败')
    } finally {
      setPatrolling(false)
    }
  }, [load])

  const gotoCases = useCallback(() => navigate('/admin/marketing/cases'), [navigate])
  const gotoMaterials = useCallback(() => navigate('/admin/marketing/materials'), [navigate])

  if (loading) return <LoadingState text="正在汇总今日军情…" />
  if (error) return <ErrorState text={error} onRetry={load} />
  if (!overview) return <ErrorState text="暂无概览数据" onRetry={load} />

  const { facts, opportunity_cards, funnel } = overview
  const oppTotal = GROUP_ORDER.reduce((s, g) => s + (opportunity_cards[g] || 0), 0)
  const budget = facts.budget_month
  const budgetPct = budget.cap > 0 ? Math.min(100, Math.round((budget.spent / budget.cap) * 100)) : 0
  const funnelAllZero = FUNNEL_STEPS.every((s) => !(funnel[s.key] || 0))
  const funnelMax = Math.max(1, ...FUNNEL_STEPS.map((s) => funnel[s.key] || 0))

  return (
    <div className="space-y-5">
      {/* 1) 顶栏:问候 + 数据更新时间 + 刷新(图 01;「立即巡逻一轮」为现有功能保留) */}
      <FadeIn>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-foreground">您好,运营团队 👋</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              让每一次触达都有价值
              {overview.last_patrol && (
                <span> · 上次巡逻 {timeAgo(overview.last_patrol.ran_at)}</span>
              )}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {lastUpdated && (
              <span className="hidden rounded-md border border-border bg-card px-2.5 py-1.5 text-xs tabular-nums text-muted-foreground sm:inline-flex">
                数据更新时间:{fmtDateTime(lastUpdated)}
              </span>
            )}
            <Button variant="outline" size="sm" onClick={load} disabled={patrolling}>
              <RefreshCw className="mr-1.5 size-4" /> 刷新
            </Button>
            <Button size="sm" onClick={onPatrol} disabled={patrolling}>
              <Radar className={cn('mr-1.5 size-4', patrolling && 'animate-spin')} />
              {patrolling ? '巡逻中…' : '立即巡逻一轮'}
            </Button>
          </div>
        </div>
      </FadeIn>

      {/* 2) 今日机会(四卡 · 真实分组计数 · 无「较昨日」无预测金额) */}
      <Panel title="今日机会">
        {oppTotal === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-muted/30 px-4 py-6 text-center text-xs text-muted-foreground">
            数据积累中 · 已配置信号规则,等待第一条命中。可点右上「立即巡逻一轮」主动跑一遍。
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {GROUP_ORDER.map((g, i) => {
              const Icon = GROUP_ICON[g]
              const count = opportunity_cards[g] || 0
              return (
                <FadeIn key={g} delay={i * 0.04}>
                  <div className="rounded-xl border border-border bg-card p-4 transition-shadow hover:shadow-sm">
                    <div className="flex items-center gap-2.5">
                      <span className={cn('flex size-8 shrink-0 items-center justify-center rounded-lg', GROUP_CHIP[g])}>
                        <Icon className="size-4" />
                      </span>
                      <span className="truncate text-xs text-muted-foreground">{GROUP_LABEL[g]}</span>
                    </div>
                    <div className="mt-3 flex items-end justify-between gap-2">
                      <span className="text-3xl font-semibold leading-none tabular-nums text-foreground">
                        {count.toLocaleString()}
                      </span>
                      <Button
                        variant="ghost" size="sm"
                        className="h-7 px-2 text-xs text-brand hover:text-brand"
                        onClick={gotoCases}
                      >
                        看建议 <ArrowRight className="ml-1 size-3.5" />
                      </Button>
                    </div>
                  </div>
                </FadeIn>
              )
            })}
          </div>
        )}
      </Panel>

      {/* 3) 中部三列:待审批方案 / 海报素材 / 客户触达建议(图 01 中段) */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* (a) 待审批营销方案 */}
        <Panel
          title="待审批营销方案"
          className="lg:col-span-1"
          action={
            <div className="flex items-center gap-2">
              {pending.length > 0 && (
                <span className="inline-flex items-center rounded-full bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium tabular-nums text-rose-600">
                  {pending.length}
                </span>
              )}
              <SeeAll onClick={gotoCases} />
            </div>
          }
        >
          {pending.length === 0 ? (
            <EmptyState
              title="军师暂无待审建议"
              hint="每天 08:10 自动巡逻产出;也可点右上「立即巡逻一轮」马上跑一遍。"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border">
                    <th className="py-2 pr-2 font-medium">方案名</th>
                    <th className="py-2 pr-2 font-medium">目标人群</th>
                    <th className="py-2 pr-2 font-medium">预计影响</th>
                    <th className="py-2 font-medium">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {pending.slice(0, 6).map((c) => (
                    <tr
                      key={c.id}
                      onClick={() => onOpenCase(c.id)}
                      className="cursor-pointer border-b border-border/60 last:border-0 hover:bg-muted/40"
                    >
                      <td className="max-w-[150px] truncate py-2 pr-2 font-medium text-foreground" title={c.trigger_reason}>
                        {c.trigger_reason}
                      </td>
                      <td className="max-w-[110px] truncate py-2 pr-2 text-muted-foreground" title={c.audience_jsonb?.definition}>
                        {c.audience_jsonb?.definition || '—'}
                      </td>
                      <td className="max-w-[110px] truncate py-2 pr-2 text-muted-foreground" title={c.expected_impact}>
                        {c.expected_impact || '—'}
                      </td>
                      <td className="py-2"><CaseStatusBadge status={c.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        {/* (b) 可生成海报素材(静态模板占位 · brand 色系渐变) */}
        <Panel title="可生成海报素材" className="lg:col-span-1">
          <div className="grid grid-cols-3 gap-2">
            {POSTER_TEMPLATES.map((t, i) => (
              <FadeIn key={t.name} delay={i * 0.05}>
                <div className="relative aspect-[3/4] overflow-hidden rounded-lg">
                  <div className={cn('absolute inset-0 bg-gradient-to-br', t.grad)} />
                  <ImageIcon className="absolute left-1/2 top-[42%] size-6 -translate-x-1/2 -translate-y-1/2 text-white/70" />
                  <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/55 to-transparent px-2 pb-1.5 pt-6">
                    <span className="block truncate text-[11px] font-medium text-white">{t.name}</span>
                  </div>
                </div>
              </FadeIn>
            ))}
          </div>
          <Button className="mt-3 w-full" onClick={gotoMaterials}>
            <Plus className="mr-1.5 size-4" /> 生成营销素材
          </Button>
          <p className="mt-2 text-[11px] text-muted-foreground">
            素材仅用于代理触达客户,不含任何效果承诺文案。
          </p>
        </Panel>

        {/* (c) 客户触达建议(客户 / 触达建议人话 / 优先级) */}
        <Panel title="客户触达建议" className="lg:col-span-1" action={<SeeAll onClick={gotoCases} />}>
          {pending.length === 0 ? (
            <EmptyState
              title="暂无触达建议"
              hint="巡逻命中信号后,这里会给出按优先级排序的客户触达清单。"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border">
                    <th className="py-2 pr-2 font-medium">客户</th>
                    <th className="py-2 pr-2 font-medium">触达建议</th>
                    <th className="py-2 font-medium">优先级</th>
                  </tr>
                </thead>
                <tbody>
                  {pending.slice(0, 6).map((c) => {
                    const pr = PRIORITY[c.risk_level] || PRIORITY.low
                    return (
                      <tr
                        key={c.id}
                        onClick={() => onOpenCase(c.id)}
                        className="cursor-pointer border-b border-border/60 last:border-0 hover:bg-muted/40"
                      >
                        <td className="max-w-[130px] truncate py-2 pr-2 font-medium text-foreground" title={humanCustomer(c)}>
                          {humanCustomer(c)}
                        </td>
                        <td className="py-2 pr-2 text-muted-foreground">{ruleHuman(c.rule_key)}</td>
                        <td className="py-2">
                          <span className={cn('inline-flex items-center rounded-full border px-2 py-0.5 text-[11px]', pr.cls)}>
                            {pr.label}
                          </span>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>

      {/* 4) 底部两栏:本周转化效果(三事实数 + 相关转化)/ 近期营销动态 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* (a) 本周转化效果 · 只显真实事实数,口径为「相关转化」,不造折线 */}
        <Panel title="本周转化效果" className="lg:col-span-1">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <FactTile icon={<Bell className="size-4" />} label="待审批" value={facts.pending_approvals.toLocaleString()} />
            <FactTile icon={<CheckCheck className="size-4" />} label="今日已触达" value={facts.touched_today.toLocaleString()} delay={0.04} />
            <FadeIn delay={0.08}>
              <div className="rounded-xl border border-border bg-card p-3.5">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Wallet className="size-4" /> 本月预算水位
                </div>
                {budget.cap > 0 ? (
                  <>
                    <div className="mt-2 text-xl font-semibold tabular-nums text-foreground">{budgetPct}%</div>
                    <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
                      <motion.div
                        initial={{ width: 0 }}
                        animate={{ width: `${budgetPct}%` }}
                        transition={{ duration: 0.4 }}
                        className={cn('h-full rounded-full', budgetPct >= 90 ? 'bg-rose-500' : 'bg-brand')}
                      />
                    </div>
                    <div className="mt-1 text-[11px] text-muted-foreground">
                      已用 <PowerAndYuan points={budget.spent} /> / 上限 {budget.cap.toLocaleString()} 算力
                    </div>
                  </>
                ) : (
                  <>
                    <div className="mt-2 text-xl font-semibold text-foreground">—</div>
                    <div className="mt-1 text-[11px] text-muted-foreground">暂无进行中的活动预算</div>
                  </>
                )}
              </div>
            </FadeIn>
          </div>

          <div className="mt-4">
            <div className="mb-2 text-xs font-medium text-muted-foreground">相关转化(注册 → 复购)</div>
            {funnelAllZero ? (
              <EmptyState
                title="转化数据积累中"
                hint="发生注册、首充等真实行为后,这里按环节显示相关转化人数。"
              />
            ) : (
              <div className="space-y-2.5">
                {FUNNEL_STEPS.map((s, i) => {
                  const v = funnel[s.key] || 0
                  const pct = Math.round((v / funnelMax) * 100)
                  return (
                    <FadeIn key={s.key} delay={i * 0.03}>
                      <div>
                        <div className="mb-1 flex items-center justify-between text-xs">
                          <span className="text-muted-foreground">{s.label}</span>
                          <span className="font-medium tabular-nums text-foreground">{v.toLocaleString()}</span>
                        </div>
                        <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                          <motion.div
                            initial={{ width: 0 }}
                            animate={{ width: `${pct}%` }}
                            transition={{ duration: 0.4, delay: i * 0.03 }}
                            className="h-full rounded-full bg-brand"
                          />
                        </div>
                      </div>
                    </FadeIn>
                  )
                })}
              </div>
            )}
            <p className="mt-3 flex items-center gap-1 text-[11px] text-muted-foreground">
              <ArrowRight className="size-3" /> 各环节为相关转化人数,非因果归因。
            </p>
          </div>
        </Panel>

        {/* (b) 近期营销动态(时间 + 中文标签 + 人话消息) */}
        <Panel title="近期营销动态" className="lg:col-span-1">
          {events.length === 0 ? (
            <EmptyState title="暂无动态" hint="巡逻、审批、触达等操作都会在这里留痕。" />
          ) : (
            <ul className="space-y-3">
              {events.slice(0, 8).map((e, i) => (
                <FadeIn key={e.id} delay={Math.min(i, 8) * 0.02}>
                  <li className="flex items-start gap-2.5">
                    <span className="w-16 shrink-0 pt-0.5 text-[11px] tabular-nums text-muted-foreground">
                      {timeAgo(e.created_at)}
                    </span>
                    <span className={cn(
                      'inline-flex shrink-0 items-center rounded-md px-1.5 py-0.5 text-[10px] font-medium',
                      EVENT_BADGE[e.severity],
                    )}>
                      {eventLabel(e.event_type)}
                    </span>
                    <div
                      className={cn('min-w-0 flex-1 text-xs text-foreground', e.case_id && 'cursor-pointer hover:underline')}
                      onClick={() => e.case_id && onOpenCase(e.case_id)}
                    >
                      {e.message}
                    </div>
                  </li>
                </FadeIn>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  )
}

// 事实数字卡(小型 · 只显真实事实数)
function FactTile({ icon, label, value, delay = 0 }: {
  icon: ReactNode; label: string; value: ReactNode; delay?: number
}) {
  return (
    <FadeIn delay={delay}>
      <div className="rounded-xl border border-border bg-card p-3.5">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {icon} {label}
        </div>
        <div className="mt-2 text-xl font-semibold tabular-nums text-foreground">{value}</div>
      </div>
    </FadeIn>
  )
}
