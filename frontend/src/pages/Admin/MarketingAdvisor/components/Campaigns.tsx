// 营销军师 · 活动与台账
// 台账快照 + 上下线控制:价格与预算的修改到定价中心完成,此处不改数字。
// 人话化:界面不露英文活动代号/类型码;每个活动配一句"到底送什么"的参数摘要
// (取自后端 SELECT * 带出的 params_jsonb,字段缺失时优雅降级不显示,不伪造)。
import { useCallback, useEffect, useState } from 'react'
import { Megaphone, Power, PowerOff, Info } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useConfirmDialog } from '@/components/ui/confirm-dialog'
import type { Campaign } from '../types'
import { fetchCampaigns, activateCampaign, endCampaign } from '../api'
import { FadeIn, Panel, LoadingState, EmptyState, ErrorState, PowerAndYuan } from './ui'

// types.ts 未声明 params_jsonb(后端 SELECT * 会带出)· 本地扩展,缺失时摘要不显示
type CampaignRow = Campaign & { params_jsonb?: unknown }

const CAMPAIGN_TYPE_LABEL: Record<string, string> = {
  first_charge_double: '首充双倍',
  milestone: '累充成长',
  adhoc: '临时活动',
}

const STATUS_META: Record<string, { label: string; className: string }> = {
  draft: { label: '草稿 · 未上线', className: 'bg-muted text-muted-foreground border-border' },
  active: { label: '进行中', className: 'bg-brand/10 text-brand border-brand/30' },
  paused: { label: '已暂停', className: 'bg-amber-500/10 text-amber-600 border-amber-500/30' },
  ended: { label: '已结束', className: 'bg-muted text-muted-foreground border-border' },
}

function CampaignStatusBadge({ status }: { status: Campaign['status'] }) {
  // 未知状态兜底中文,原始值放 title(悬停可查),不露英文
  const meta = STATUS_META[status] ?? { label: '状态待确认', className: 'bg-muted text-muted-foreground border-border' }
  return (
    <span
      title={status}
      className={cn('inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px]', meta.className)}
    >
      {meta.label}
    </span>
  )
}

const numOrNull = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

// 人话参数摘要:回答"这个活动到底送什么"。字段缺失返回 null(不显示,不伪造)。
function paramsSummary(c: CampaignRow): string | null {
  const p = c.params_jsonb
  if (!p || typeof p !== 'object' || Array.isArray(p)) return null
  const o = p as Record<string, unknown>
  if (c.campaign_type === 'first_charge_double') {
    const rate = numOrNull(o.bonus_rate)
    const cap = numOrNull(o.max_grant_points)
    const parts: string[] = []
    if (rate !== null) parts.push(`首充赠 ${Math.round(rate * 100)}%`)
    if (cap !== null) parts.push(`单笔最高赠 ${cap.toLocaleString()} 算力`)
    return parts.length ? parts.join(' · ') : null
  }
  if (c.campaign_type === 'milestone') {
    const tiers = Array.isArray(o.tiers) ? o.tiers : []
    const parts: string[] = []
    for (const t of tiers) {
      if (!t || typeof t !== 'object' || Array.isArray(t)) continue
      const tier = t as Record<string, unknown>
      const threshold = numOrNull(tier.threshold_cents)
      const bonus = numOrNull(tier.bonus_points)
      if (threshold === null || bonus === null) continue
      parts.push(`累计满 ¥${(threshold / 100).toLocaleString()} 赠 ${bonus.toLocaleString()} 算力`)
    }
    return parts.length ? parts.join(',') : null
  }
  return null
}

// 静态宽度档(避免动态 w-[..%] 被 Tailwind JIT purge · 也避免 inline style)
const WIDTH_STEP: string[] = [
  'w-0', 'w-[10%]', 'w-1/5', 'w-[30%]', 'w-2/5', 'w-1/2',
  'w-3/5', 'w-[70%]', 'w-4/5', 'w-[90%]', 'w-full',
]

function BudgetGauge({ spent, cap }: { spent: number; cap: number }) {
  const pct = cap > 0 ? Math.min(100, Math.max(0, Math.round((spent / cap) * 100))) : 0
  const nearFull = pct >= 90
  const widthClass = WIDTH_STEP[Math.round(pct / 10)]
  return (
    <div className="min-w-[9rem] space-y-1">
      <div className="text-xs text-foreground">
        <PowerAndYuan points={spent} />
        <span className="text-muted-foreground"> / </span>
        <span className="text-muted-foreground tabular-nums">{cap.toLocaleString()} 算力</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div className={cn('h-full rounded-full transition-[width] duration-300', widthClass, nearFull ? 'bg-amber-500' : 'bg-brand')} />
      </div>
      <div className="text-[11px] text-muted-foreground tabular-nums">水位 {pct}%</div>
    </div>
  )
}

export function Campaigns() {
  const [confirmDialog, askConfirm] = useConfirmDialog()  // askConfirm 防遮蔽 window.confirm(第53轮教训)
  const [campaigns, setCampaigns] = useState<CampaignRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const { campaigns } = await fetchCampaigns()
      setCampaigns(campaigns)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载活动失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const handleActivate = useCallback(async (c: Campaign) => {
    const ok = await askConfirm({
      title: `上线活动「${c.name}」?`,
      description: '上线后该活动立即对用户生效。预算/价格类改动请到定价中心操作,此处仅控制上下线。',
      confirmLabel: '确认上线',
    })
    if (!ok) return
    setBusyId(c.id)
    try {
      await activateCampaign(c.id)
      toast.success(`已上线:${c.name}`)
      await load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '上线失败')
    } finally {
      setBusyId(null)
    }
  }, [askConfirm, load])

  const handleEnd = useCallback(async (c: Campaign) => {
    const ok = await askConfirm({
      title: `即时下线「${c.name}」?`,
      description: '下线后活动立即停止对用户生效,已发生的算力消耗不可回滚。此操作不可撤销。',
      confirmLabel: '确认下线',
      danger: true,
    })
    if (!ok) return
    setBusyId(c.id)
    try {
      await endCampaign(c.id)
      toast.success(`已下线:${c.name}`)
      await load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '下线失败')
    } finally {
      setBusyId(null)
    }
  }, [askConfirm, load])

  return (
    <>
      {confirmDialog}
      <FadeIn>
        <Panel
          title="活动与台账"
          action={
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Info className="size-3.5 shrink-0" />
              <span>价格与预算的修改到定价中心完成;这里看台账、管上下线。</span>
            </div>
          }
        >
          {loading ? (
            <LoadingState text="加载活动…" />
          ) : error ? (
            <ErrorState text={error} onRetry={load} />
          ) : campaigns.length === 0 ? (
            <EmptyState
              title="还没有营销活动"
              hint="活动(首充双倍 / 累充成长 / 临时活动)由营销军师立案后在此登记。当前无进行中或草稿活动。"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[42rem] border-collapse text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    <th className="py-2 pr-3 font-medium">活动名</th>
                    <th className="py-2 pr-3 font-medium">类型</th>
                    <th className="py-2 pr-3 font-medium">状态</th>
                    <th className="py-2 pr-3 font-medium">预算水位</th>
                    <th className="py-2 pr-3 font-medium">常驻</th>
                    <th className="py-2 pl-3 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {campaigns.map((c) => {
                    const busy = busyId === c.id
                    const canActivate = c.status === 'draft' || c.status === 'paused'
                    const canEnd = c.status === 'active'
                    const summary = paramsSummary(c)
                    return (
                      <tr key={c.id} className="border-b border-border/60 last:border-0 align-top">
                        <td className="py-3 pr-3">
                          <div className="flex items-center gap-2 font-medium text-foreground">
                            <Megaphone className="size-3.5 shrink-0 text-muted-foreground" />
                            {c.name}
                          </div>
                          {summary && (
                            <div className="mt-1 max-w-[24rem] text-[11px] leading-relaxed text-muted-foreground">
                              {summary}
                            </div>
                          )}
                        </td>
                        <td className="py-3 pr-3">
                          {/* 类型中文 badge · 未知类型兜底中文,原始值 title 可查 */}
                          <Badge variant="secondary" title={c.campaign_type}>
                            {CAMPAIGN_TYPE_LABEL[c.campaign_type] ?? '其他活动'}
                          </Badge>
                        </td>
                        <td className="py-3 pr-3">
                          <CampaignStatusBadge status={c.status} />
                        </td>
                        <td className="py-3 pr-3">
                          <BudgetGauge spent={c.spent_points} cap={c.budget_cap_points} />
                        </td>
                        <td className="py-3 pr-3">
                          {c.is_resident ? (
                            <Badge variant="secondary">常驻</Badge>
                          ) : (
                            <span className="text-xs text-muted-foreground">—</span>
                          )}
                        </td>
                        <td className="py-3 pl-3 text-right">
                          {canActivate && (
                            <div className="flex flex-col items-end gap-1">
                              <Button size="sm" disabled={busy} onClick={() => handleActivate(c)}>
                                <Power className="size-3.5" /> 上线
                              </Button>
                              {c.status === 'draft' && (
                                <div className="max-w-[15rem] text-right text-[11px] leading-relaxed text-muted-foreground">
                                  上线后,满足条件的充值才会真实获赠(还需在设置页打开"赠送算力入账")
                                </div>
                              )}
                            </div>
                          )}
                          {canEnd && (
                            <Button size="sm" variant="destructive" disabled={busy} onClick={() => handleEnd(c)}>
                              <PowerOff className="size-3.5" /> 即时下线
                            </Button>
                          )}
                          {!canActivate && !canEnd && (
                            <span className="text-xs text-muted-foreground">无可用操作</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </FadeIn>
    </>
  )
}
