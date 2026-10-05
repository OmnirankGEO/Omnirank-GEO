// 任务详情抽屉 · 人话版:顶部一句话讲清"现在什么状态、接下来会发生什么、要不要你做什么"
// 工程术语全部翻译(指令→要做什么 · 事件流→处理过程 · 产物→详细结果 · artifact_type→人话)。
// 数据全部真实(任务/事件/产物接口);2s 轮询,任务终态后停止。

import { useCallback, useEffect, useRef, useState } from 'react'
import { Ban, FileText, Loader2 } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cancelTask, getTask, getTaskArtifacts, getTaskEvents } from '../api'
import type { AiOpsArtifact, AiOpsEvent, AiOpsTask } from '../types'
import { KIND_LABEL } from '../types'
import { MarkdownBlock, RiskBadge, StatusBadge, timeAgo } from './shared'
import { TaskTimeline } from './TaskTimeline'

const ACTIVE = new Set(['queued', 'running', 'waiting_approval'])

// artifact_type → 人话(worker/ssh_runner 实际会写的类型)
const ARTIFACT_LABEL: Record<string, string> = {
  ops_context: '任务上下文包(交给 AI 的完整背景)',
  codex_output: 'AI 输出全文',
  patch: '代码改动(diff)',
  sql_result: 'SQL 查询结果',
  command_plan: '命令执行计划',
  glm_triage: 'GLM 一线分诊结果',
}

// 这些产物是给人读的 Markdown 散文 → 渲染排版;
// diff / SQL / JSON 类保持等宽原样(渲染反而破坏对齐与可复制性)
const MARKDOWN_ARTIFACTS = new Set(['codex_output', 'ops_context'])

// 日报任务直达:result(新)或 source_context(旧任务兜底)里的 report_date
function reportDateOf(task: AiOpsTask): string | null {
  const fromResult = (task.result_jsonb as { report_date?: string })?.report_date
  const fromCtx = (task.source_context_jsonb as { report_date?: string })?.report_date
  const d = fromResult || fromCtx || null
  return d && /^\d{4}-\d{2}-\d{2}$/.test(d) ? d : null
}

// 当前状态一句话:接下来会发生什么 / 要不要你做什么(全部基于真实 status + flag)
function statusHint(task: AiOpsTask, aiOpsEnabled: boolean | null): { text: string; cls: string } {
  switch (task.status) {
    case 'queued':
      if (aiOpsEnabled === false) {
        return {
          text: 'AI 运维总开关未启用:任务已排队保留,不会自动执行;开关启用后 AI 会自动领取处理。',
          cls: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
        }
      }
      return { text: '排队中,AI 稍后自动领取处理,不需要你操作。', cls: 'border-border bg-muted/30 text-muted-foreground' }
    case 'running':
      return { text: 'AI 正在处理,过程实时写在下面「处理过程」里。', cls: 'border-sky-500/40 bg-sky-500/10 text-sky-300' }
    case 'waiting_approval':
      return {
        text: 'AI 已给出改动/方案,正在等你到「审批中心」人工确认;不确认不会执行。',
        cls: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
      }
    case 'succeeded':
      return { text: '已完成。结论看「AI 结论」,依据在「详细结果」里。', cls: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300' }
    case 'failed':
      return { text: '处理失败了。看「处理过程」末尾的红色记录能找到原因,可重新创建任务再试。', cls: 'border-rose-500/40 bg-rose-500/10 text-rose-300' }
    case 'cancelled':
      return { text: '任务已取消,不会再执行。', cls: 'border-border bg-muted/30 text-muted-foreground' }
    default:
      return { text: '', cls: 'border-border bg-muted/30 text-muted-foreground' }
  }
}

export function TaskDetailDrawer({ taskId, onClose, aiOpsEnabled = null }: {
  taskId: number | null
  onClose: () => void
  aiOpsEnabled?: boolean | null
}) {
  const navigate = useNavigate()
  const [task, setTask] = useState<AiOpsTask | null>(null)
  const [events, setEvents] = useState<AiOpsEvent[]>([])
  const [artifacts, setArtifacts] = useState<AiOpsArtifact[]>([])
  const [busy, setBusy] = useState(false)
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    if (taskId == null) return
    try {
      const [t, ev, ar] = await Promise.all([
        getTask(taskId), getTaskEvents(taskId), getTaskArtifacts(taskId),
      ])
      setTask(t)
      setEvents(ev)
      setArtifacts(ar)
      // 任务已终态(succeeded/failed/cancelled):停止 2s 轮询,不再空转打接口
      if (!ACTIVE.has(t.status) && timer.current) {
        clearInterval(timer.current)
        timer.current = null
      }
    } catch {
      /* ignore transient */
    }
  }, [taskId])

  useEffect(() => {
    if (taskId == null) { setTask(null); setEvents([]); setArtifacts([]); return }
    void load()
    timer.current = setInterval(() => { void load() }, 2000)
    return () => { if (timer.current) clearInterval(timer.current) }
  }, [taskId, load])

  const doCancel = async () => {
    if (taskId == null) return
    setBusy(true)
    try {
      await cancelTask(taskId)
      toast.success('已取消任务')
      await load()
    } catch (err) {
      toast.error('取消失败', { description: String((err as Error).message) })
    } finally {
      setBusy(false)
    }
  }

  const hint = task ? statusHint(task, aiOpsEnabled) : null

  return (
    <Dialog open={taskId != null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="dark max-h-[88vh] max-w-3xl overflow-y-auto bg-[#0e121b] text-foreground">
        <DialogHeader>
          <DialogTitle className="text-base">
            {task ? (task.title || `${KIND_LABEL[task.kind]} #${task.id}`) : '任务详情'}
          </DialogTitle>
        </DialogHeader>

        {!task ? (
          <div className="flex min-h-40 items-center justify-center text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />加载中
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={task.status} />
              <RiskBadge risk={task.risk_level} />
              <span className="text-xs text-muted-foreground">优先级 {task.priority} · {KIND_LABEL[task.kind]}</span>
              <span className="ml-auto text-[11px] text-muted-foreground">创建 {timeAgo(task.created_at)}</span>
            </div>

            {/* 当前状态一句话(人话) */}
            {hint && hint.text && (
              <div className={`rounded-md border px-3 py-2 text-xs leading-5 ${hint.cls}`}>
                {hint.text}
              </div>
            )}

            {task.instruction && (
              <div className="rounded-md border bg-muted/20 p-3">
                <div className="mb-1 text-xs font-medium text-muted-foreground">要做什么</div>
                <p className="whitespace-pre-wrap text-sm leading-6 text-foreground">{task.instruction}</p>
              </div>
            )}

            {task.summary && (
              <div className="rounded-md border bg-muted/20 p-3">
                <div className="mb-1 text-xs font-medium text-muted-foreground">AI 结论</div>
                <MarkdownBlock text={task.summary} />
                {task.kind === 'report' && reportDateOf(task) && (
                  <Button size="sm" className="mt-2"
                    onClick={() => { onClose(); navigate(`/admin/ai-ops/reports/${reportDateOf(task)}`) }}>
                    <FileText className="mr-1 size-3.5" />打开日报正文
                  </Button>
                )}
              </div>
            )}

            <div>
              <div className="mb-2 text-xs font-medium text-muted-foreground">处理过程</div>
              <TaskTimeline events={events} />
            </div>

            {artifacts.length > 0 && (
              <div>
                <div className="mb-2 text-xs font-medium text-muted-foreground">详细结果</div>
                <div className="space-y-2">
                  {artifacts.map((a) => (
                    <details key={a.id} className="rounded-md border bg-background">
                      <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-foreground">
                        {ARTIFACT_LABEL[a.artifact_type] || a.artifact_type}{a.title ? ` · ${a.title}` : ''}
                      </summary>
                      {MARKDOWN_ARTIFACTS.has(a.artifact_type) && a.content_text ? (
                        <div className="max-h-80 overflow-auto border-t bg-muted/20 p-3">
                          <MarkdownBlock text={a.content_text} size="xs" />
                        </div>
                      ) : (
                        <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words border-t bg-muted/20 p-3 text-[11px] leading-4 text-muted-foreground">
                          {a.content_text || '（空）'}
                        </pre>
                      )}
                    </details>
                  ))}
                </div>
              </div>
            )}

            {ACTIVE.has(task.status) && (
              <div className="flex justify-end border-t pt-3">
                <Button size="sm" variant="ghost" className="text-muted-foreground"
                  onClick={() => void doCancel()} disabled={busy}>
                  <Ban className="mr-1 size-3.5" />取消任务
                </Button>
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
