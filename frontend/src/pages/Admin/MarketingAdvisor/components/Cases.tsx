// 营销军师 · 「营销方案」审批队列(自取数 · 状态筛选 · 三态 · 一句话新建)
import { useCallback, useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Eye, Loader2, PlusCircle, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import { authFetch } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import type { MarketingCase } from '../types'
import { fetchCases } from '../api'
import {
  FadeIn, Panel, LoadingState, EmptyState, ErrorState, CaseStatusBadge, RiskBadge,
} from './ui'

// 状态筛选页签(全部 = undefined · 其余对应 status 查询参数)
const STATUS_TABS: { key: string; value: string | undefined; label: string }[] = [
  { key: 'all', value: undefined, label: '全部' },
  { key: 'pending', value: 'pending', label: '待审批' },
  { key: 'approved', value: 'approved', label: '已通过' },
  { key: 'executed', value: 'executed', label: '已执行' },
  { key: 'rejected', value: 'rejected', label: '已结束' },
]

function formatExpires(iso?: string | null): string {
  if (!iso) return '长期有效'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
}

export function Cases({ onOpenCase }: { onOpenCase: (id: number) => void }) {
  const [activeTab, setActiveTab] = useState<string>('all')
  const [cases, setCases] = useState<MarketingCase[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (tabKey: string) => {
    const status = STATUS_TABS.find((t) => t.key === tabKey)?.value
    setLoading(true)
    setError(null)
    try {
      const data = await fetchCases(status)
      setCases(Array.isArray(data.cases) ? data.cases : [])
      setCounts(data.status_counts || {})
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载营销方案失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load(activeTab) }, [activeTab, load])

  const countFor = (tab: { key: string; value: string | undefined }): number => {
    if (tab.value === undefined) {
      return Object.values(counts).reduce((sum, n) => sum + (Number(n) || 0), 0)
    }
    return Number(counts[tab.value]) || 0
  }

  // 「新建营销方案」:一句话方向 → AI 起草完整方案进待审(老板拍板:不只等巡逻)
  const [draftOpen, setDraftOpen] = useState(false)
  const [direction, setDirection] = useState('')
  const [drafting, setDrafting] = useState(false)
  const submitDraft = useCallback(async () => {
    const d = direction.trim()
    if (d.length < 4) { toast.error('用一句话说清方向,比如:给沉默的高价值客户做一场唤回'); return }
    setDrafting(true)
    try {
      const res = await authFetch('/api/admin/marketing/cases/draft', {
        method: 'POST', body: JSON.stringify({ direction: d }),
      })
      const data = (await res.json().catch(() => null)) as { ok?: boolean; created?: boolean; note?: string; detail?: string; copy_source?: string; gate?: string; audience_count?: number } | null
      if (res.ok && data?.ok) {
        // [P0-C 质量闸] 圈不到人 = 不建案,如实告知(不是错误,是军师不出空壳案)
        if (data.gate === 'no_audience') {
          toast(data.note || '这个方向当前圈不到人,方案未创建', { description: '换个方向再试,或等信号积累。' })
          return
        }
        // copy_source 如实透传:老板要能一眼看出 LLM 有没有真的动笔
        toast.success(!data.created
          ? (data.note || '今天已建过同方向方案')
          : data.copy_source === 'ai'
            ? `军师已起草(AI 撰写 · 实时圈选 ${data.audience_count ?? '?'} 人),进入待审批`
            : '已按默认模板起草(AI 暂不可用,文案可在详情页手改),进入待审批')
        setDraftOpen(false); setDirection('')
        setActiveTab('pending'); void load('pending')
      } else {
        toast.error(typeof data?.detail === 'string' ? data.detail : '起草失败,请稍后再试')
      }
    } catch {
      toast.error('网络异常,请稍后再试')
    } finally {
      setDrafting(false)
    }
  }, [direction, load])

  return (
    <Panel
      title="营销方案"
      action={
        <div className="flex flex-wrap items-center gap-1">
          <Button size="sm" className="mr-1 min-h-9" onClick={() => setDraftOpen((v) => !v)}>
            <PlusCircle className="size-3.5" /> 新建营销方案
          </Button>
          {STATUS_TABS.map((tab) => {
            const isActive = activeTab === tab.key
            const c = countFor(tab)
            return (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors',
                  isActive
                    ? 'border-brand bg-brand/10 text-foreground'
                    : 'border-border text-muted-foreground hover:bg-muted',
                )}
              >
                <span>{tab.label}</span>
                <span className="tabular-nums opacity-70">{c}</span>
              </button>
            )
          })}
        </div>
      }
    >
      {/* 一句话起草面板 */}
      {draftOpen && (
        <FadeIn>
          <div className="mb-4 rounded-xl border border-brand/30 bg-brand/5 p-3">
            <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
              <Sparkles className="size-4 text-brand" /> 用一句话告诉军师你想做什么,它来起草完整方案
            </div>
            <div className="flex flex-col gap-2 sm:flex-row">
              <input
                value={direction}
                maxLength={200}
                onChange={(e) => setDirection(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') void submitDraft() }}
                placeholder="如:给沉默的高价值客户做一场唤回 / 月底冲一波首充"
                className="min-h-11 w-full flex-1 rounded-lg border border-border bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-brand"
              />
              <Button className="min-h-11" disabled={drafting} onClick={() => void submitDraft()}>
                {drafting ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
                让军师起草
              </Button>
            </div>
            <p className="mt-2 text-[11px] text-muted-foreground">起草的方案会进入待审批队列,过五项合规检查、你批准后才会执行;不会自动打扰任何用户。</p>
          </div>
        </FadeIn>
      )}

      {loading ? (
        <LoadingState text="加载营销方案…" />
      ) : error ? (
        <ErrorState text={error} onRetry={() => void load(activeTab)} />
      ) : cases.length === 0 ? (
        <div>
          <EmptyState
            title="暂无营销方案"
            hint="军师每天 08:10 自动巡逻起草;也可以现在就用一句话让它起草一个。"
          />
          <div className="mt-3 flex justify-center">
            <Button size="sm" className="min-h-9" onClick={() => setDraftOpen(true)}>
              <PlusCircle className="size-3.5" /> 新建营销方案
            </Button>
          </div>
        </div>
      ) : (
        <FadeIn>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">方案号</th>
                  <th className="py-2 pr-3 font-medium">方案名</th>
                  <th className="py-2 pr-3 font-medium">目标人群</th>
                  <th className="py-2 pr-3 font-medium">风险</th>
                  <th className="py-2 pr-3 font-medium">状态</th>
                  <th className="py-2 pr-3 font-medium">有效期</th>
                  <th className="py-2 pr-0 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                <AnimatePresence initial={false}>
                  {cases.map((c, i) => (
                    <motion.tr
                      key={c.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0 }}
                      transition={{ duration: 0.18, delay: Math.min(i * 0.015, 0.2) }}
                      className="border-b border-border/60 transition-colors hover:bg-muted/50"
                    >
                      <td className="py-2.5 pr-3 align-top font-mono text-xs tabular-nums text-muted-foreground">
                        {c.case_no}
                      </td>
                      <td className="max-w-[220px] py-2.5 pr-3 align-top">
                        <div className="truncate font-medium text-foreground" title={c.trigger_reason}>
                          {c.trigger_reason || '—'}
                        </div>
                      </td>
                      <td className="max-w-[200px] py-2.5 pr-3 align-top text-muted-foreground">
                        <div className="truncate" title={c.audience_jsonb?.definition}>
                          {c.audience_jsonb?.definition || '—'}
                        </div>
                      </td>
                      <td className="py-2.5 pr-3 align-top">
                        <RiskBadge risk={c.risk_level} />
                      </td>
                      <td className="py-2.5 pr-3 align-top">
                        <CaseStatusBadge status={c.status} />
                      </td>
                      <td className="py-2.5 pr-3 align-top text-xs tabular-nums text-muted-foreground">
                        {formatExpires(c.expires_at)}
                      </td>
                      <td className="py-2.5 pr-0 text-right align-top">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 gap-1 px-2 text-xs"
                          onClick={() => onOpenCase(c.id)}
                        >
                          <Eye className="size-3.5" /> 查看
                        </Button>
                      </td>
                    </motion.tr>
                  ))}
                </AnimatePresence>
              </tbody>
            </table>
          </div>
        </FadeIn>
      )}
    </Panel>
  )
}
