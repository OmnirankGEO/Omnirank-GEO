// 营销军师 · 杠杆面板(看清现状 + 一键跳到能动手的地方)
// 收编平台散落的价格杠杆:充值阶梯 / SKU 货架 / 内置活动(首充双倍)。
// 价格类修改在定价中心完成;军师不直接改价,此处负责"看懂现状 + 引路去动手"。
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Layers, Package, Sparkles, Info } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { fetchLevers } from '../api'
import { FadeIn, Panel, LoadingState, EmptyState, ErrorState, PowerAndYuan } from './ui'

type LeversData = Awaited<ReturnType<typeof fetchLevers>>
type SkuItem = { default_name?: string; points_granted?: number; suggested_retail_yuan?: number }

// 内置活动(首充双倍)状态的人话:是什么状态 + 意味着什么
const FCD_STATUS_META: Record<string, { label: string; cls: string; text: string }> = {
  draft: {
    label: '未上线 · 不会发放',
    cls: 'border-border bg-muted text-muted-foreground',
    text: '首充双倍已配置但还没上线,现在用户充值不会获赠。',
  },
  active: {
    label: '真实生效中',
    cls: 'border-brand/30 bg-brand/10 text-brand',
    text: '首充双倍正在真实生效,满足条件的首充会自动获赠算力。',
  },
  paused: {
    label: '已暂停',
    cls: 'border-amber-500/30 bg-amber-500/10 text-amber-600',
    text: '首充双倍已暂停,恢复上线前不会发放。',
  },
  ended: {
    label: '已结束',
    cls: 'border-border bg-muted text-muted-foreground',
    text: '首充双倍已结束,不再发放。',
  },
}

// 充值阶梯 item 结构未定 —— 泛化渲染:优先取常见键;完全无法识别时给人话指引(不倒英文 JSON)。
function ladderFields(raw: unknown): { title: string; lines: { label: string; node: ReactNode }[] } {
  if (!raw || typeof raw !== 'object') {
    return { title: String(raw ?? '—'), lines: [] }
  }
  const o = raw as Record<string, unknown>
  const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  const amount = num(o.amount ?? o.amount_yuan ?? o.price ?? o.rmb)
  const points = num(o.points ?? o.points_granted ?? o.base_points)
  const bonus = num(o.bonus ?? o.bonus_points ?? o.gift_points)

  const lines: { label: string; node: ReactNode }[] = []
  if (amount !== null) lines.push({ label: '充值金额', node: <span className="tabular-nums">¥{amount.toLocaleString()}</span> })
  if (points !== null) lines.push({ label: '到账算力', node: <PowerAndYuan points={points} /> })
  if (bonus !== null && bonus > 0) lines.push({ label: '额外赠送', node: <PowerAndYuan points={bonus} /> })

  const title = String(o.name ?? o.title ?? o.label ?? (amount !== null ? `¥${amount} 档` : '充值档位'))
  // 完全无法识别常见键 —— 人话指引,不在界面倒英文字段名。
  if (lines.length === 0) {
    return {
      title,
      lines: [{
        label: '配置说明',
        node: <span className="text-[11px] text-muted-foreground">该档位的配置格式暂无法直接解读,点右上「去定价中心调整」查看原始配置。</span>,
      }],
    }
  }
  return { title, lines }
}

export function Levers() {
  const navigate = useNavigate()
  const [data, setData] = useState<LeversData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetchLevers()
      setData(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : '杠杆面板加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const gotoPricing = useCallback(() => navigate('/admin/pricing-center'), [navigate])

  if (loading) return <LoadingState text="加载杠杆快照…" />
  if (error) return <ErrorState text={error} onRetry={load} />
  if (!data) {
    return (
      <EmptyState
        title="暂无杠杆数据"
        hint="定价中心尚未配置任何价格杠杆。"
        action={<Button variant="outline" size="sm" onClick={gotoPricing}>去定价中心</Button>}
      />
    )
  }

  const ladders = data.recharge_ladders ?? []
  const skus = (data.sku_shelf ?? []) as SkuItem[]
  const fcd = data.first_charge_double

  // 状态人话映射;未知状态兜底中文,原始值放 title 悬停可查
  const fcdMeta = fcd?.exists
    ? (FCD_STATUS_META[fcd.status ?? ''] ?? {
        label: '状态待确认',
        cls: 'border-border bg-muted text-muted-foreground',
        text: '首充双倍已配置,状态待确认(悬停右侧徽章查看)。',
      })
    : {
        label: '未配置',
        cls: 'border-border bg-muted text-muted-foreground',
        text: '当前未配置首充双倍活动。',
      }

  return (
    <div className="space-y-4">
      {/* 行动横幅:看完就能动手 */}
      <FadeIn>
        <div className="flex flex-col gap-2 rounded-lg border border-brand/40 bg-brand/10 p-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-2 text-sm text-foreground">
            <Sparkles className="mt-0.5 size-4 shrink-0 text-brand" />
            <span>想搞一场营销活动?让军师根据数据起草方案 →</span>
          </div>
          <Button
            size="sm"
            className="shrink-0 self-start sm:self-auto"
            onClick={() => navigate('/admin/marketing/cases')}
          >
            去看今日建议
          </Button>
        </div>
      </FadeIn>

      <FadeIn delay={0.02}>
        <div className="flex items-start gap-2 rounded-lg border border-border bg-muted/40 p-3 text-xs text-muted-foreground">
          <Info className="mt-0.5 size-4 shrink-0" />
          <span>这里是营销杠杆总览:看清现状,一键跳到能动手的地方。价格类修改在定价中心完成,军师不直接改价。</span>
        </div>
      </FadeIn>

      {/* 1. 充值阶梯 */}
      <FadeIn delay={0.04}>
        <Panel
          title="充值阶梯"
          action={
            <div className="flex items-center gap-2">
              <Layers className="size-4 text-muted-foreground" />
              <Button variant="outline" size="sm" onClick={gotoPricing}>去定价中心调整</Button>
            </div>
          }
        >
          {ladders.length === 0 ? (
            <EmptyState
              title="尚未配置充值阶梯"
              hint="到定价中心设置充值档位后,这里会显示各档的金额与到账算力。"
            />
          ) : (
            <>
              <p className="mb-3 text-xs text-muted-foreground">
                充得越多送得越多,当前 {ladders.length} 档。这里只看现状,调整档位去定价中心。
              </p>
              <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {ladders.map((raw, i) => {
                  const f = ladderFields(raw)
                  return (
                    <li key={i} className="rounded-lg border border-border bg-background p-3">
                      <div className="mb-2 text-sm font-medium text-foreground">{f.title}</div>
                      <dl className="space-y-1">
                        {f.lines.map((ln, j) => (
                          <div key={j} className="flex items-baseline justify-between gap-2 text-xs">
                            <dt className="shrink-0 text-muted-foreground">{ln.label}</dt>
                            <dd className="text-right text-foreground">{ln.node}</dd>
                          </div>
                        ))}
                      </dl>
                    </li>
                  )
                })}
              </ul>
            </>
          )}
        </Panel>
      </FadeIn>

      {/* 2. SKU 货架(算力包) */}
      <FadeIn delay={0.08}>
        <Panel
          title="算力包货架"
          action={
            <div className="flex items-center gap-2">
              <Package className="size-4 text-muted-foreground" />
              <Button variant="outline" size="sm" onClick={gotoPricing}>去定价中心调整</Button>
            </div>
          }
        >
          {skus.length === 0 ? (
            <EmptyState
              title="货架为空"
              hint="定价中心尚未上架任何算力包。"
            />
          ) : (
            <>
              <p className="mb-3 text-xs text-muted-foreground">
                当前上架 {skus.length} 款算力包,每款的到账算力与建议零售价如下。
              </p>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[420px] text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs text-muted-foreground">
                      <th className="py-2 pr-3 font-medium">名称</th>
                      <th className="py-2 pr-3 font-medium">到账算力</th>
                      <th className="py-2 font-medium">建议零售价</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {skus.map((s, i) => (
                      <tr key={i}>
                        <td className="py-2 pr-3 text-foreground">{s.default_name ?? '—'}</td>
                        <td className="py-2 pr-3">
                          {typeof s.points_granted === 'number'
                            ? <PowerAndYuan points={s.points_granted} />
                            : <span className="text-muted-foreground">—</span>}
                        </td>
                        <td className="py-2 tabular-nums text-foreground">
                          {typeof s.suggested_retail_yuan === 'number'
                            ? `¥${s.suggested_retail_yuan.toLocaleString()}`
                            : <span className="text-muted-foreground">—</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </Panel>
      </FadeIn>

      {/* 3. 内置活动 · 首充双倍 */}
      <FadeIn delay={0.12}>
        <Panel
          title="内置活动 · 首充双倍"
          action={
            <div className="flex items-center gap-2">
              <Sparkles className="size-4 text-muted-foreground" />
              <Button variant="outline" size="sm" onClick={() => navigate('/admin/marketing/campaigns')}>去活动管理</Button>
            </div>
          }
        >
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-muted-foreground">{fcdMeta.text}</div>
            <span
              title={fcd?.status}
              className={'inline-flex shrink-0 items-center whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs ' + fcdMeta.cls}
            >
              {fcdMeta.label}
            </span>
          </div>
        </Panel>
      </FadeIn>
    </div>
  )
}
