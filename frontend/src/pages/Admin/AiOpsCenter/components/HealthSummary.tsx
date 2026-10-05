// 今日健康:6 张指标卡(全部映射真实 overview.health 字段)
// 诚实原则:后端只有 ai_ops.enabled / kill_switch / running / failed / pending / open bugs,
// 不伪造"整体健康分 / 服务可用性 / 较昨日 +N"等无数据支撑的指标。

import type { ReactNode } from 'react'
import {
  Activity, AlertTriangle, Bug, ClipboardCheck, Loader2, ShieldAlert,
} from 'lucide-react'
import type { AiOpsOverview } from '../types'

type Tone = 'ok' | 'warn' | 'danger' | 'muted'

const ICON_BG: Record<Tone, string> = {
  ok: 'bg-emerald-500/15 text-emerald-400',
  warn: 'bg-amber-500/15 text-amber-400',
  danger: 'bg-rose-500/15 text-rose-400',
  muted: 'bg-sky-500/15 text-sky-400',
}
const VALUE_COLOR: Record<Tone, string> = {
  ok: 'text-emerald-400',
  warn: 'text-amber-400',
  danger: 'text-rose-400',
  muted: 'text-foreground',
}

function Card({ icon, label, value, sub, tone }: {
  icon: ReactNode
  label: string
  value: ReactNode
  sub: string
  tone: Tone
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className={`grid size-8 shrink-0 place-items-center rounded-lg ${ICON_BG[tone]}`}>{icon}</span>
      </div>
      <div className={`mt-2 text-2xl font-semibold leading-tight ${VALUE_COLOR[tone]}`}>{value}</div>
      <div className="mt-1 text-[11px] text-muted-foreground">{sub}</div>
    </div>
  )
}

const CARD_LABELS = ['AI 运维总开关', 'Kill Switch', '进行中任务', '失败任务', '待审批', '未处理反馈']

export function HealthSummary({ overview }: { overview: AiOpsOverview | null }) {
  const h = overview?.health

  // 数据未到:显示「—」而不是假确定的「已停用/正常/0」(诚实原则)
  if (!h) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        {CARD_LABELS.map((label) => (
          <div key={label} className="rounded-xl border border-border bg-card p-4">
            <span className="text-xs text-muted-foreground">{label}</span>
            <div className="mt-2 text-2xl font-semibold text-muted-foreground">—</div>
            <div className="mt-1 text-[11px] text-muted-foreground">加载中…</div>
          </div>
        ))}
      </div>
    )
  }

  const enabled = Boolean(h.ai_ops_enabled)
  const kill = Boolean(h.kill_switch)
  const running = h.tasks_running ?? 0
  const failed = h.tasks_failed ?? 0
  const pending = h.pending_approvals ?? 0
  const openBugs = h.open_bug_feedback ?? 0

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
      <Card
        icon={<Activity className="size-4" />} label="AI 运维总开关"
        value={enabled ? '已启用' : '已停用'} tone={enabled ? 'ok' : 'muted'}
        sub={enabled ? 'Worker 可领取任务' : '默认停用 · 需授权开启'}
      />
      <Card
        icon={<ShieldAlert className="size-4" />} label="Kill Switch"
        value={kill ? '已急停' : '正常'} tone={kill ? 'danger' : 'ok'}
        sub={kill ? '所有执行已冻结' : '未触发急停'}
      />
      <Card
        icon={<Loader2 className={`size-4 ${running > 0 ? 'animate-spin' : ''}`} />} label="进行中任务"
        value={running} tone="muted"
        sub="running + 待审批"
      />
      <Card
        icon={<AlertTriangle className="size-4" />} label="失败任务"
        value={failed} tone={failed > 0 ? 'warn' : 'ok'}
        sub={failed > 0 ? '需查看事件流' : '暂无失败'}
      />
      <Card
        icon={<ClipboardCheck className="size-4" />} label="待审批"
        value={pending} tone={pending > 0 ? 'warn' : 'ok'}
        sub={pending > 0 ? 'L3/L4 待人工放行' : '无待办审批'}
      />
      <Card
        icon={<Bug className="size-4" />} label="未处理反馈"
        value={openBugs} tone={openBugs > 0 ? 'warn' : 'ok'}
        sub="bug 反馈 pending + read"
      />
    </div>
  )
}
