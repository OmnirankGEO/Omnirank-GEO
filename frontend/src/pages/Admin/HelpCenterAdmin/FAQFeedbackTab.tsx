// [2026-05-18 帮助中心 Phase 3 v2] 反馈收件箱 tab
// 状态 chip + 紧急度筛选 + 卡片列表 + 状态切换
// "改 FAQ" 直接 navigate 到独立编辑页(不再 setActiveTab + preselectId)

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import {
  AlertCircle,
  ArrowRight,
  Bug,
  Clock,
  ExternalLink,
  Image as ImageIcon,
  Loader2,
  MailOpen,
  Save,
  User,
  Bot,
  Wrench,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import SafeMarkdown from '@/components/SafeMarkdown'
import { Textarea } from '@/components/ui/textarea'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { listFeedback, updateFeedback, diagnoseFeedback, fixFeedback } from './api'
import {
  STATUS_LABEL,
  URGENCY_LABEL,
  FEEDBACK_KIND_LABEL,
  type FAQAdminFeedback,
  type FeedbackStatus,
  type Urgency,
} from './types'

type Props = {
  /** 父级想知道当前 pending 数量(用来在 tab badge 显示) */
  onCountsLoaded?: (pending: number) => void
}

const STATUS_FILTERS: { value: FeedbackStatus | 'all'; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'pending', label: '待处理' },
  { value: 'read', label: '已读' },
  { value: 'done', label: '已处理' },
  { value: 'closed', label: '关闭' },
]

const URGENCY_BADGE_COLOR: Record<Urgency, string> = {
  high: 'bg-rose-500/10 text-rose-600 border-rose-500/30',
  mid: 'bg-amber-500/10 text-amber-600 border-amber-500/30',
  low: 'bg-muted text-muted-foreground border-border',
}

const STATUS_BADGE_VARIANT: Record<FeedbackStatus, 'default' | 'secondary' | 'outline'> = {
  pending: 'default',
  read: 'secondary',
  done: 'outline',
  closed: 'outline',
}

function timeAgo(iso: string): string {
  const t = new Date(iso).getTime()
  const diff = Date.now() - t
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  return `${d} 天前`
}

function submitterIdentityLabel(fb: FAQAdminFeedback): string {
  if (fb.submitter_identity === 'admin') return '管理员'
  if (fb.submitter_identity === 'l2') return '二级代理'
  if (fb.submitter_identity === 'agent') return '一级代理'
  if (fb.submitter_identity === 'normal_user') return '普通用户'
  const level = Number(fb.submitter_agent_level || 0)
  if (level >= 2) return '二级代理'
  if (level >= 1) return '一级代理'
  return '普通用户'
}

export function FAQFeedbackTab({ onCountsLoaded }: Props) {
  const navigate = useNavigate()
  const [items, setItems] = useState<FAQAdminFeedback[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<FeedbackStatus | 'all'>('pending')
  const [urgencyFilter, setUrgencyFilter] = useState<Urgency | 'all'>('all')
  const [busyId, setBusyId] = useState<number | null>(null)
  const [noteDrafts, setNoteDrafts] = useState<Record<number, string>>({})
  const [previewImage, setPreviewImage] = useState<{ url: string; key?: string } | null>(null)

  const reload = async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await listFeedback({
        kind: 'bug',
        status: statusFilter !== 'all' ? statusFilter : undefined,
        urgency: urgencyFilter !== 'all' ? urgencyFilter : undefined,
      })
      setItems(data)
      setNoteDrafts((prev) => {
        const next = { ...prev }
        data.forEach((item) => {
          if (next[item.id] == null) next[item.id] = item.admin_note || ''
        })
        return next
      })
      if (onCountsLoaded && statusFilter === 'pending') {
        onCountsLoaded(data.length)
      }
    } catch (err) {
      setError(String((err as Error).message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, urgencyFilter])

  const handleStatus = async (id: number, status: FeedbackStatus) => {
    setBusyId(id)
    try {
      await updateFeedback(id, { status })
      toast(`已标记为「${STATUS_LABEL[status]}」`)
      await reload()
    } catch (err) {
      toast.error('改状态失败', { description: String((err as Error).message) })
    } finally {
      setBusyId(null)
    }
  }

  const handleSaveNote = async (id: number) => {
    setBusyId(id)
    try {
      await updateFeedback(id, { admin_note: noteDrafts[id] || '' })
      toast.success('处理备注已保存')
      await reload()
    } catch (err) {
      toast.error('保存备注失败', { description: String((err as Error).message) })
    } finally {
      setBusyId(null)
    }
  }

  const editFAQ = (faqId: number) => {
    navigate(`/admin/help-center/faq/${faqId}`)
  }

  // 把 bug 反馈交给 AI 运维控制塔:创建任务后跳到任务详情看进展。
  const handleAiTask = async (id: number, action: 'diagnose' | 'fix') => {
    setBusyId(id)
    try {
      const { task } = action === 'fix' ? await fixFeedback(id) : await diagnoseFeedback(id)
      toast.success(action === 'fix' ? '已交给 Codex 修复' : '已创建 AI 诊断任务')
      navigate(`/admin/ai-ops/tasks/${task.id}`)
    } catch (err) {
      toast.error('创建 AI 任务失败', { description: String((err as Error).message) })
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-4">
      {/* 筛选 */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex gap-1">
          {STATUS_FILTERS.map((s) => (
            <button
              key={s.value}
              type="button"
              onClick={() => setStatusFilter(s.value)}
              className={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                statusFilter === s.value
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'border-border bg-card text-muted-foreground hover:bg-muted'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <Select value={urgencyFilter} onValueChange={(v) => setUrgencyFilter(v as Urgency | 'all')}>
          <SelectTrigger className="h-9 w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部紧急度</SelectItem>
            <SelectItem value="high">{URGENCY_LABEL.high}</SelectItem>
            <SelectItem value="mid">{URGENCY_LABEL.mid}</SelectItem>
            <SelectItem value="low">{URGENCY_LABEL.low}</SelectItem>
          </SelectContent>
        </Select>
        <div className="ml-auto text-xs text-muted-foreground">共 {items.length} 条</div>
      </div>

      {/* 列表 */}
      {loading ? (
        <div className="flex min-h-[200px] items-center justify-center text-muted-foreground">
          <Loader2 className="mr-2 size-4 animate-spin" />
          加载中
        </div>
      ) : error ? (
        <div className="flex min-h-[200px] flex-col items-center justify-center rounded-lg border border-dashed bg-muted/20 p-6 text-center">
          <AlertCircle className="mb-2 size-6 text-destructive" />
          <p className="text-sm">{error}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={reload}>
            重试
          </Button>
        </div>
      ) : items.length === 0 ? (
        <div className="flex min-h-[200px] flex-col items-center justify-center rounded-lg border border-dashed bg-muted/20 p-6 text-center">
          <MailOpen className="mb-2 size-6 text-muted-foreground" />
          <p className="text-sm text-foreground">没有反馈</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {statusFilter === 'pending' ? '太棒了, 所有反馈都处理过了' : '换个筛选试试'}
          </p>
        </div>
      ) : (
        <ul className="space-y-3">
          {items.map((fb) => (
            <li key={fb.id} className="rounded-lg border bg-card p-4">
              {/* 头部 · 状态 + 紧急度 + 时间 */}
              <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
                <Badge variant={STATUS_BADGE_VARIANT[fb.status]}>{STATUS_LABEL[fb.status]}</Badge>
                <Badge variant={fb.kind === 'bug' ? 'default' : 'outline'} className="gap-1">
                  {fb.kind === 'bug' && <Bug className="size-3" />}
                  {FEEDBACK_KIND_LABEL[fb.kind]}
                </Badge>
                <span
                  className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 ${URGENCY_BADGE_COLOR[fb.urgency]}`}
                >
                  {URGENCY_LABEL[fb.urgency]}
                </span>
                <span className="ml-auto inline-flex items-center gap-1 text-muted-foreground">
                  <Clock className="size-3" />
                  {timeAgo(fb.created_at)}
                </span>
              </div>

              <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_280px]">
                <div className="min-w-0 space-y-3">
                  <div>
                    <div className="mb-1 text-xs font-medium text-muted-foreground">用户描述</div>
                    <p className="whitespace-pre-wrap text-sm leading-6 text-foreground">{fb.message}</p>
                  </div>

                  {fb.ai_answer && (
                    <div className="rounded-md border bg-muted/30 p-3">
                      <div className="mb-1 text-xs font-medium text-muted-foreground">小榜先答</div>
                      <SafeMarkdown className="text-sm leading-6 text-foreground">{fb.ai_answer}</SafeMarkdown>
                    </div>
                  )}

                  {fb.screenshot_url && (
                    <div className="rounded-md border bg-muted/20 p-3">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <div className="inline-flex items-center gap-2 text-xs font-medium text-muted-foreground">
                          <ImageIcon className="size-4" />
                          用户截图
                        </div>
                        <button
                          type="button"
                          onClick={() =>
                            setPreviewImage({
                              url: fb.screenshot_url,
                              key: fb.screenshot_key,
                            })
                          }
                          className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                        >
                          预览原图
                          <ExternalLink className="size-3" />
                        </button>
                      </div>
                      <button
                        type="button"
                        onClick={() =>
                          setPreviewImage({
                            url: fb.screenshot_url,
                            key: fb.screenshot_key,
                          })
                        }
                        className="block w-full cursor-zoom-in rounded-md focus:outline-none focus:ring-2 focus:ring-primary/40"
                      >
                        <img
                          src={fb.screenshot_url}
                          alt="用户反馈截图"
                          className="max-h-72 w-full rounded-md border bg-background object-contain"
                        />
                      </button>
                      {fb.screenshot_key && (
                        <div className="mt-2 truncate text-[11px] text-muted-foreground">
                          OSS: {fb.screenshot_key}
                        </div>
                      )}
                    </div>
                  )}
                </div>

                <aside className="space-y-3 rounded-md border bg-muted/20 p-3">
                <div className="space-y-1 text-xs text-muted-foreground">
                  <div className="inline-flex items-center gap-1">
                    <User className="size-3" />
                    {fb.user_display_name || fb.user_username || `用户 #${fb.user_id}`}
                  </div>
                  <div>
                    身份:{' '}
                    <Badge variant="outline" className="h-5 rounded-full px-1.5 text-[11px] font-normal">
                      {submitterIdentityLabel(fb)}
                    </Badge>
                  </div>
                  {fb.contact && <div>联系: {fb.contact}</div>}
                    <div>提交时间: {new Date(fb.created_at).toLocaleString()}</div>
                    {fb.faq_id != null && fb.faq_question && (
                      <div>
                        关联 FAQ:{' '}
                        <button
                          type="button"
                          onClick={() => editFAQ(fb.faq_id!)}
                          className="text-primary hover:underline"
                        >
                          {fb.faq_question} <ArrowRight className="inline size-3" />
                        </button>
                      </div>
                    )}
                  </div>

                  <div>
                    <label className="mb-1 block text-xs font-medium text-muted-foreground">处理备注</label>
                    <Textarea
                      value={noteDrafts[fb.id] ?? ''}
                      onChange={(event) =>
                        setNoteDrafts((prev) => ({ ...prev, [fb.id]: event.target.value }))
                      }
                      placeholder="记录排查结果、处理方式或需要跟进的人"
                      className="min-h-24 resize-y text-xs leading-5"
                      disabled={busyId === fb.id}
                    />
                    <Button
                      size="sm"
                      variant="outline"
                      className="mt-2 w-full"
                      onClick={() => void handleSaveNote(fb.id)}
                      disabled={busyId === fb.id || (noteDrafts[fb.id] ?? '') === (fb.admin_note || '')}
                    >
                      {busyId === fb.id ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : <Save className="mr-1 size-3.5" />}
                      保存备注
                    </Button>
                  </div>
                </aside>
              </div>

              {/* 操作 */}
              <div className="mt-3 flex flex-wrap gap-2 border-t pt-3">
                {fb.kind === 'bug' && (
                  <>
                    <Button
                      size="sm"
                      variant="default"
                      onClick={() => void handleAiTask(fb.id, 'diagnose')}
                      disabled={busyId === fb.id}
                    >
                      <Bot className="mr-1 size-3.5" />
                      AI 诊断
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => void handleAiTask(fb.id, 'fix')}
                      disabled={busyId === fb.id}
                    >
                      <Wrench className="mr-1 size-3.5" />
                      交给 Codex 修复
                    </Button>
                  </>
                )}
                {fb.status === 'pending' && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => handleStatus(fb.id, 'read')}
                    disabled={busyId === fb.id}
                  >
                    标记已读
                  </Button>
                )}
                {fb.faq_id != null && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => editFAQ(fb.faq_id!)}
                    disabled={busyId === fb.id}
                  >
                    改 FAQ
                  </Button>
                )}
                {(fb.status === 'pending' || fb.status === 'read') && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => handleStatus(fb.id, 'done')}
                    disabled={busyId === fb.id}
                  >
                    标记已处理
                  </Button>
                )}
                {fb.status !== 'closed' && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => handleStatus(fb.id, 'closed')}
                    disabled={busyId === fb.id}
                    className="text-muted-foreground"
                  >
                    关闭
                  </Button>
                )}
                {fb.status === 'closed' && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => handleStatus(fb.id, 'pending')}
                    disabled={busyId === fb.id}
                  >
                    重开
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <Dialog open={previewImage != null} onOpenChange={(open) => !open && setPreviewImage(null)}>
        <DialogContent className="max-w-[92vw] gap-3 p-4 sm:max-w-5xl">
          <DialogHeader>
            <DialogTitle className="text-base">用户截图预览</DialogTitle>
          </DialogHeader>
          {previewImage && (
            <div className="space-y-2">
              <div className="flex max-h-[78vh] items-center justify-center overflow-auto rounded-lg border bg-muted/20 p-2">
                <img
                  src={previewImage.url}
                  alt="用户反馈截图大图"
                  className="max-h-[74vh] max-w-full object-contain"
                />
              </div>
              {previewImage.key && (
                <div className="truncate text-xs text-muted-foreground">
                  OSS: {previewImage.key}
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
