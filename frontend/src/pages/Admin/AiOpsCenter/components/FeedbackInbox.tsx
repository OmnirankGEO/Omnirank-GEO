// 问题反馈收件箱:未结案 bug 反馈,一键 AI 诊断 / 交给 Codex 修复
// fix 任务同样只创建不执行(L1 · 合并必审批 · 执行受总开关/Runner 管控)

import { useEffect, useState } from 'react'
import { Bot, Loader2, Wrench } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { listFeedback } from '@/pages/Admin/HelpCenterAdmin/api'
import type { FAQAdminFeedback } from '@/pages/Admin/HelpCenterAdmin/types'
import { diagnoseFeedback, fixFeedback } from '../api'
import { EmptyHint, Panel, timeAgo } from './shared'

const URGENCY_LABEL: Record<string, string> = { high: '卡死', mid: '影响业务', low: '不急' }

export function FeedbackInbox({ onOpenTask, max = 6 }: { onOpenTask: (taskId: number) => void; max?: number }) {
  const [items, setItems] = useState<FAQAdminFeedback[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)

  const reload = async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await listFeedback({ kind: 'bug', status: 'pending', limit: 20 })
      setItems(data)
    } catch {
      setError('反馈加载失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void reload() }, [])

  const act = async (id: number, kind: 'diagnose' | 'fix') => {
    setBusyId(id)
    try {
      const { task } = kind === 'diagnose' ? await diagnoseFeedback(id) : await fixFeedback(id)
      toast.success(kind === 'diagnose' ? '已创建 AI 诊断任务' : '已创建 Codex 修复任务(合并需人工审批)')
      onOpenTask(task.id)
    } catch (err) {
      toast.error('创建失败', { description: String((err as Error).message) })
    } finally {
      setBusyId(null)
    }
  }

  return (
    <Panel title="问题反馈收件箱" action={<span className="text-xs text-muted-foreground">待处理 {items.length}</span>}>
      {loading ? (
        <EmptyHint text="加载中" />
      ) : error ? (
        <EmptyHint text={error} />
      ) : items.length === 0 ? (
        <EmptyHint text="没有未处理的 bug 反馈" />
      ) : (
        <ul className="space-y-2">
          {items.slice(0, max).map((fb) => (
            <li key={fb.id} className="rounded-md border bg-background p-2.5">
              <div className="mb-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                <span>#{fb.id}</span>
                <span className="rounded-full border px-1.5">{URGENCY_LABEL[fb.urgency] || fb.urgency}</span>
                <span className="ml-auto">{timeAgo(fb.created_at)}</span>
              </div>
              <p className="line-clamp-2 text-xs text-foreground">{fb.message}</p>
              <div className="mt-2 flex justify-end gap-2">
                <Button size="sm" variant="outline" onClick={() => void act(fb.id, 'diagnose')} disabled={busyId === fb.id}>
                  {busyId === fb.id ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : <Bot className="mr-1 size-3.5" />}
                  AI 诊断
                </Button>
                <Button size="sm" variant="outline" onClick={() => void act(fb.id, 'fix')} disabled={busyId === fb.id}>
                  <Wrench className="mr-1 size-3.5" />
                  交给 Codex 修复
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}
