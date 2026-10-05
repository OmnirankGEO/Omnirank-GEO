/**
 * XiaobangGapCard — 小榜里的「缺口作战计划」结构化建议卡
 *
 * 🔴 存在的理由(2026-08-08 自查发现):后端 gap_assistant 一直在 SSE 的 meta 里下发,
 *    但 useXiaobangChat 组装 meta 时只挑 sources/link/confidence,把它整个丢了,
 *    前端也没有任何组件读它 —— 后端算了、审计也写了,用户一个字看不见。
 *    这是本仓「判据打在函数上、接线没接」的第四例,前三例是别人的包,这一例是我自己写的。
 *
 * 本组件只做一件事:把**服务端已经签发的**东西显示出来。
 *
 * 🔴 三条硬约束:
 *   1. **路由只认 `target_route`** —— 服务端由版本化操作地图签发。
 *      前端不拼、不猜、不兜底旧路径;没有 target_route 的动作就**不渲染跳转按钮**。
 *      (所以本文件里不允许出现任何路由字面量,门禁会扫。)
 *   2. **不渲染未登记动作** —— 服务端按 OperationRegistry 的权限与版本签发,
 *      hook 再要求动作版本与当前注册表版本完全一致；本组件只接本地绝对路径。
 *   3. **降级态不是故障** —— degraded 只说明"这次建议取不到",页面数据不受影响,
 *      用一行弱化文案说清楚,不用报错色、不做重试循环。
 */
import { useNavigate } from 'react-router-dom'
import { ArrowRight, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { XiaobangGapAction, XiaobangGapAssistant } from '@/hooks/useXiaobangChat'

interface XiaobangGapCardProps {
  assistant: XiaobangGapAssistant
  /** 跳转前先关 drawer(否则抽屉盖住目标页),与 XiaobangLinkCard 同款 */
  onNavigate?: () => void
  className?: string
}

/** 能跳的动作 = 服务端签发了现役路由 且 服务端说它可用。两个条件都要。 */
export function navigableActions(actions: XiaobangGapAction[]): XiaobangGapAction[] {
  return (actions || []).filter((a) =>
    !!a
    && a.enabled === true
    && !!a.registry_version
    && typeof a.target_route === 'string'
    && a.target_route.startsWith('/')
    && !a.target_route.startsWith('//'),
  ).slice(0, 2)
}

export function XiaobangGapCard({ assistant, onNavigate, className }: XiaobangGapCardProps) {
  const navigate = useNavigate()
  const actions = navigableActions(assistant.actions)
  const trail = (assistant.breadcrumb || []).join(' → ')
  // 导航类回答的 headline 里已经带了面包屑,再显示一遍是噪音
  const showTrail = !!trail && !assistant.headline.includes(trail)

  return (
    <div
      data-testid="xiaobang-gap-card"
      className={cn(
        'rounded-2xl border border-border bg-card px-3.5 py-3 text-sm',
        className,
      )}
    >
      <p className="font-medium leading-relaxed text-foreground">{assistant.headline}</p>

      {showTrail && (
        <p className="mt-1 text-xs text-muted-foreground">路径：{trail}</p>
      )}

      {assistant.reasons?.length > 0 && (
        <ul className="mt-2 space-y-1">
          {assistant.reasons.map((reason, i) => (
            <li key={i} className="flex gap-1.5 text-xs leading-relaxed text-muted-foreground">
              <span className="mt-1.5 size-1 shrink-0 rounded-full bg-muted-foreground/60" />
              <span className="min-w-0">{reason}</span>
            </li>
          ))}
        </ul>
      )}

      {actions.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {actions.map((action) => (
            <button
              key={action.action_id}
              type="button"
              data-action-id={action.action_id}
              title={action.hint || undefined}
              onClick={() => {
                onNavigate?.()
                navigate(action.target_route as string, {
                  state: {
                    xiaobangHelpTarget: action.help_target,
                    xiaobangOperationId: action.operation_id,
                  },
                })
              }}
              className={cn(
                'inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5',
                'text-xs font-medium shadow-sm transition-colors',
                action.primary
                  ? 'bg-brand text-white hover:opacity-90'
                  : 'border border-border bg-background text-foreground hover:bg-muted',
              )}
            >
              <span className="truncate">{action.label}</span>
              <ArrowRight className="size-3.5 shrink-0" />
            </button>
          ))}
        </div>
      )}

      {assistant.evidence.length > 0 && (
        <details className="group mt-3 border-t border-border/50 pt-2">
          <summary className="flex cursor-pointer list-none items-center gap-1 text-xs text-muted-foreground">
            <ChevronDown className="size-3 transition-transform group-open:rotate-180" />
            查看安排依据
          </summary>
          <dl className="mt-2 space-y-1.5">
            {assistant.evidence.map((item) => (
              <div key={`${item.label}:${item.value}`} className="flex gap-2 text-xs">
                <dt className="shrink-0 text-muted-foreground">{item.label}</dt>
                <dd className="min-w-0 text-foreground">{item.value}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}

      {assistant.degraded && (
        <p className="mt-2 text-xs text-muted-foreground">
          这条建议这次没取到，页面上的任务和状态不受影响。
        </p>
      )}
    </div>
  )
}
