// 右侧固定 AI 命令台 · v2 真对话流
// 每次发送后展示后端 reply 人话回复(确定性生成,基于真实 DB/flag 状态,不是假 LLM):
//   - 问数类短问题 → 直接回答(不建任务)
//   - 日报 → 同步生成 + 查看按钮
//   - 其余 → 创建 diagnose/fix 任务 + 「查看任务」按钮(不再自动甩进抽屉)
// 高危动作永不在此直接执行。

import { useEffect, useRef, useState } from 'react'
import type { LucideIcon } from 'lucide-react'
import { ArrowRight, FileText, Loader2, ScrollText, Send, Sparkles, Wallet, Wrench } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { chatCommand } from '../api'
import type { AiOpsTask } from '../types'
import { KIND_LABEL } from '../types'
import { MarkdownBlock, timeAgo } from './shared'

type Quick = { icon: LucideIcon; label: string; command: string }

// 文案诚实:除「日报」真生成外,其余是「创建任务」(修复意图后端建 fix,其余 diagnose)
const QUICK: Quick[] = [
  { icon: FileText, label: '生成运维日报', command: '生成今天运营日报' },
  { icon: ScrollText, label: '创建 500 诊断任务', command: '检查今天 500 最多的接口并给出可能原因' },
  { icon: Wallet, label: '创建财务核对任务', command: '检查财务中心本月营收和充值账单是否一致' },
  { icon: Wrench, label: '创建 Codex 修复任务', command: '帮我修复监测趋势最新点不一致的问题' },
]

type ChatMsg = {
  role: 'user' | 'ai'
  text: string
  taskId?: number
  reportDate?: string   // 有值 → 主按钮直达日报正文(任务进度退为次按钮)
  isError?: boolean
}

export function AiCommandConsole({ onOpenTask, recentTasks, adminName }: {
  onOpenTask: (taskId: number) => void
  recentTasks: AiOpsTask[]
  adminName: string
}) {
  const navigate = useNavigate()
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [msgs, setMsgs] = useState<ChatMsg[]>([])
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // 新消息自动滚到底
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [msgs, busy])

  const send = async (text: string) => {
    const msg = text.trim()
    if (!msg || busy) return
    setBusy(true)
    setMessage('')
    // v2 总管:带上最近 10 轮对话(截 500 字/条),后端生成式回答能接住追问
    const history = msgs
      .filter((m) => !m.isError)
      .slice(-10)
      .map((m) => ({ role: m.role, text: m.text.slice(0, 500) }))
    setMsgs((m) => [...m, { role: 'user', text: msg }])
    try {
      const res = await chatCommand(msg, { page: '/admin/ai-ops' }, history)
      const fallback = res.task ? `已创建任务 #${res.task.id}「${res.task.title}」。` : '已收到。'
      setMsgs((m) => [...m, {
        role: 'ai', text: res.reply || fallback,
        taskId: res.task?.id, reportDate: res.report_date,
      }])
    } catch (err) {
      setMsgs((m) => [...m, {
        role: 'ai', isError: true,
        text: `没弄成:${String((err as Error).message)}。可以换个说法再试。`,
      }])
      toast.error('命令台请求失败', { description: String((err as Error).message) })
    } finally {
      setBusy(false)
    }
  }

  const chatTasks = recentTasks.filter((t) => t.source_type === 'chat').slice(0, 5)

  return (
    <div className="flex h-full min-h-0 flex-col bg-[#0e121b]">
      {/* 标题 */}
      <div className="flex h-14 shrink-0 items-center gap-2 border-b border-border px-4">
        <Sparkles className="size-4 text-primary" />
        <span className="text-sm font-semibold text-foreground">AI 助手 · 命令台</span>
      </div>

      {/* 对话区 */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {/* 问候(常驻第一条) */}
        <div className="flex gap-2.5 rounded-lg border border-border bg-card p-3">
          <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-primary/15 text-primary">
            <Sparkles className="size-4" />
          </span>
          <div className="min-w-0">
            <p className="text-sm text-foreground">你好,{adminName}</p>
            <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
              直接问我「现在系统状态怎么样」「有什么待审批的」,或用一句话交代要查/要修的事,
              我会答数或创建<b className="text-foreground">可审计任务</b>。高危动作只出方案、必须你审批。
            </p>
          </div>
        </div>

        {/* 快速操作 */}
        <div className="mt-3">
          <div className="mb-1.5 text-xs font-medium text-muted-foreground">快速操作</div>
          <div className="grid grid-cols-2 gap-1.5">
            {QUICK.map((q) => {
              const Icon = q.icon
              return (
                <button
                  key={q.label} type="button" onClick={() => void send(q.command)} disabled={busy}
                  className="flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-2 text-left text-xs text-muted-foreground hover:bg-muted/60 hover:text-foreground disabled:opacity-50"
                >
                  <Icon className="size-3.5 shrink-0 text-primary" />
                  <span className="min-w-0 truncate">{q.label}</span>
                </button>
              )
            })}
          </div>
        </div>

        {/* 消息气泡 */}
        <div className="mt-3 space-y-2">
          {msgs.map((m, i) => (
            m.role === 'user' ? (
              <div key={i} className="flex justify-end">
                <div className="max-w-[85%] rounded-lg rounded-br-sm bg-primary/15 px-3 py-2 text-xs leading-5 text-foreground">
                  {m.text}
                </div>
              </div>
            ) : (
              <div key={i} className="flex justify-start">
                <div className={`max-w-[92%] rounded-lg rounded-bl-sm border px-3 py-2 text-xs leading-5 ${
                  m.isError ? 'border-rose-500/40 bg-rose-500/10 text-rose-300' : 'border-border bg-card text-foreground'
                }`}>
                  <MarkdownBlock text={m.text} size="xs" />
                  <div className="flex flex-wrap gap-1.5">
                    {m.reportDate && (
                      <button
                        type="button" onClick={() => navigate(`/admin/ai-ops/reports/${m.reportDate}`)}
                        className="mt-1.5 inline-flex items-center gap-1 rounded border border-primary/40 bg-primary/15 px-2 py-1 text-[11px] font-medium text-foreground hover:bg-primary/25"
                      >
                        <FileText className="size-3" />看日报正文 <ArrowRight className="size-3" />
                      </button>
                    )}
                    {m.taskId != null && (
                      <button
                        type="button" onClick={() => onOpenTask(m.taskId!)}
                        className="mt-1.5 inline-flex items-center gap-1 rounded border border-border bg-background px-2 py-1 text-[11px] text-foreground hover:bg-muted/60"
                      >
                        {m.reportDate ? '生成过程' : '查看任务进度'} <ArrowRight className="size-3" />
                      </button>
                    )}
                  </div>
                </div>
              </div>
            )
          ))}
          {busy && (
            <div className="flex items-center gap-2 px-1 text-[11px] text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />处理中…
            </div>
          )}
        </div>

        {/* 最近命令台任务(刷新页面后仍可追溯) */}
        {chatTasks.length > 0 && (
          <div className="mt-4">
            <div className="mb-1.5 text-xs font-medium text-muted-foreground">最近命令台任务</div>
            <ul className="space-y-1">
              {chatTasks.map((t) => (
                <li key={t.id}>
                  <button
                    type="button" onClick={() => onOpenTask(t.id)}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-muted/60"
                  >
                    <span className="min-w-0 flex-1 truncate text-xs text-foreground">{t.title || KIND_LABEL[t.kind]}</span>
                    <span className="shrink-0 text-[10px] text-muted-foreground">{timeAgo(t.created_at)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* 输入 */}
      <div className="shrink-0 border-t border-border p-3">
        <Textarea
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder="问我系统情况,或说要查/要修什么…"
          className="min-h-16 resize-none bg-card text-sm"
          disabled={busy}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              // 中文 IME 组词中的 Enter 是选字,不是发送(isComposing 守卫)
              if (e.nativeEvent.isComposing) return
              e.preventDefault()
              void send(message)
            }
          }}
        />
        <div className="mt-2 flex items-center justify-between">
          <span className="text-[10px] text-muted-foreground">Enter 发送 · Shift+Enter 换行</span>
          <Button size="sm" onClick={() => void send(message)} disabled={busy || !message.trim()}>
            {busy ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : <Send className="mr-1 size-3.5" />}
            发送
          </Button>
        </div>
      </div>
    </div>
  )
}
