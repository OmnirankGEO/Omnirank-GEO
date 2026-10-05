// 监控告警(包B · 主动巡逻)· 数据全部真实:ai_ops_alerts + 巡逻打卡
// 原则:告警是"保安的哨声"——firing 置顶红黄分级;恢复的收进历史;绝不伪造健康度。

import { useCallback, useEffect, useState } from 'react'
import { BellRing, CheckCircle2, RefreshCw, Radar } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { listAlerts, resolveAlert, runPatrol } from '../api'
import type { AiOpsAlert, AiOpsOverview, AlertSeverity } from '../types'
import { EmptyHint, Panel } from './shared'

const SEVERITY_STYLE: Record<AlertSeverity, { label: string; cls: string }> = {
  critical: { label: '严重', cls: 'bg-rose-500/15 text-rose-400 border-rose-500/40' },
  warn: { label: '警告', cls: 'bg-amber-500/15 text-amber-400 border-amber-500/40' },
  info: { label: '提醒', cls: 'bg-sky-500/15 text-sky-400 border-sky-500/40' },
}

const RULE_LABEL: Record<string, string> = {
  runner_offline: '本机 Codex 离线',
  task_failed_recent: '任务失败',
  task_stuck_running: '任务卡死',
  approval_backlog: '审批积压',
  queue_pileup: '队列堆积',
  report_missing: '日报异常',
  kill_switch_on: '急停提醒',
}

// 相对时间(服务端算好的秒数 · 不用 new Date 解 naive TIMESTAMP,防时区错位)
function formatAge(sec?: number | null): string {
  if (sec == null || sec < 0) return '—'
  if (sec < 60) return '刚刚'
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`
  return `${Math.floor(sec / 86400)} 天前`
}

type AlertRow = AiOpsAlert & { first_seen_age_seconds?: number; last_seen_age_seconds?: number }

export function MonitoringPanel({ overview, onOpenTask, refreshKey }: {
  overview: AiOpsOverview | null
  onOpenTask: (id: number) => void
  refreshKey: number
}) {
  const [alerts, setAlerts] = useState<AlertRow[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const data = await listAlerts(undefined, 100)
      setAlerts(data.alerts as AlertRow[])
    } catch { /* transient */ } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    const t = setInterval(() => { void load() }, 30000)
    return () => clearInterval(t)
  }, [load, refreshKey])

  const patrol = overview?.patrol
  const patrolOn = Boolean(patrol?.enabled)
  const lastRun = patrol?.last_run ?? null

  const doPatrol = async () => {
    setBusy(true)
    try {
      const r = await runPatrol()
      toast.success(`巡逻完成:${r.firing} 条告警在响 · 新增 ${r.opened} · 恢复 ${r.resolved}`)
      await load()
    } catch (err) {
      toast.error('巡逻失败', { description: String((err as Error).message) })
    } finally {
      setBusy(false)
    }
  }

  const doResolve = async (id: number) => {
    setBusy(true)
    try {
      await resolveAlert(id)
      toast.success('已标记恢复')
      await load()
    } catch (err) {
      toast.error('操作失败', { description: String((err as Error).message) })
    } finally {
      setBusy(false)
    }
  }

  const firing = alerts.filter((a) => a.status === 'firing')
  const resolved = alerts.filter((a) => a.status === 'resolved').slice(0, 10)

  return (
    <div className="max-w-3xl space-y-3">
      {/* 巡逻状态(保安打卡) */}
      <Panel title="主动巡逻">
        <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-background px-3 py-2 text-sm">
          <div className="flex min-w-0 items-center gap-2.5">
            <Radar className={`size-5 shrink-0 ${patrolOn ? 'text-emerald-400' : 'text-muted-foreground'}`} />
            <div className="min-w-0">
              <div className="text-foreground">
                {patrolOn ? '定时巡逻开启 · 每 5 分钟一轮' : '定时巡逻关闭(可在「系统设置」开启)'}
              </div>
              <div className="text-[11px] text-muted-foreground">
                {lastRun
                  ? `上次巡逻 ${formatAge(lastRun.age_seconds)} · 在响 ${lastRun.firing_count} · 新增 ${lastRun.opened_count} · 恢复 ${lastRun.resolved_count}`
                  : '还没有巡逻记录(点右侧按钮立即巡一轮)'}
              </div>
            </div>
          </div>
          <Button size="sm" variant="outline" className="shrink-0" disabled={busy} onClick={() => void doPatrol()}>
            <RefreshCw className={`mr-1 size-3.5 ${busy ? 'animate-spin' : ''}`} />立即巡逻
          </Button>
        </div>
        <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
          巡逻只读检查:Runner 心跳(离线超 5 分钟才告警)/ 失败任务 / 卡死任务 / 审批积压 / 队列堆积 / 日报异常 / 急停遗忘。
          异常拉响告警、恢复自动消警;失败任务积累到阈值且总开关开着时,自动立一条只读诊断案(急停时不立)。
          手动「标记恢复」后若异常仍在,下一轮会重新拉响——这是保安在坚持,不是 bug。
        </p>
      </Panel>

      {/* 在响告警 */}
      <Panel title={`在响告警${firing.length ? ` · ${firing.length}` : ''}`}>
        {loading ? <EmptyHint text="加载中" /> : firing.length === 0 ? (
          <div className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
            <CheckCircle2 className="size-4 text-emerald-400" />一切正常,没有在响的告警
          </div>
        ) : (
          <ul className="space-y-2">
            {firing.map((a) => {
              const sev = SEVERITY_STYLE[a.severity] ?? SEVERITY_STYLE.warn
              return (
                <li key={a.id} className="rounded-md border border-border bg-background px-3 py-2.5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium ${sev.cls}`}>
                          {sev.label}
                        </span>
                        <span className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {RULE_LABEL[a.rule_key] ?? a.rule_key}
                        </span>
                        <span className="text-sm font-medium text-foreground">{a.title}</span>
                      </div>
                      {a.detail && <p className="mt-1 text-xs leading-5 text-muted-foreground">{a.detail}</p>}
                      <div className="mt-1 text-[11px] text-muted-foreground/80">
                        首次 {formatAge(a.first_seen_age_seconds)} · 最近确认 {formatAge(a.last_seen_age_seconds)}
                      </div>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1.5">
                      {a.task_id != null && (
                        <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => onOpenTask(a.task_id as number)}>
                          看诊断任务 #{a.task_id}
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" className="h-7 text-xs text-muted-foreground" disabled={busy}
                        onClick={() => void doResolve(a.id)}>
                        标记恢复
                      </Button>
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </Panel>

      {/* 最近恢复 */}
      {resolved.length > 0 && (
        <Panel title="最近恢复">
          <ul className="divide-y divide-border">
            {resolved.map((a) => (
              <li key={a.id} className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
                <BellRing className="size-3.5 shrink-0 opacity-50" />
                <span className="min-w-0 flex-1 truncate">{a.title}</span>
                <span className="shrink-0">{RULE_LABEL[a.rule_key] ?? a.rule_key}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  )
}
