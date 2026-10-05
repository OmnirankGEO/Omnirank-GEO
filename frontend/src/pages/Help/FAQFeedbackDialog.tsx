// [2026-05-18 帮助中心 Phase 2] FAQ 页面级反馈 Dialog
// 提交走 submitFeedback(): POST 优先, 失败落 localStorage 队列

import { useState } from 'react'
import { toast } from 'sonner'
import { MessageSquarePlus } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { submitFeedback, type Urgency } from './faq-feedback'
import type { FAQItemAPI } from './faq-api-client'

const NO_FAQ_VALUE = '__none__'

const urgencyOptions: { value: Urgency; title: string; desc: string }[] = [
  { value: 'low', title: '不急', desc: '想到就提' },
  { value: 'mid', title: '影响业务', desc: '客户单受影响' },
  { value: 'high', title: '卡死了', desc: '等不了, 急' },
]

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 当前可选关联的 FAQ 列表 · 由父级 HelpFAQ 拉好传进来 */
  items: FAQItemAPI[]
  /** 可选 · 预填关联 FAQ */
  preselectFaqId?: number
}

export function FAQFeedbackDialog({ open, onOpenChange, items, preselectFaqId }: Props) {
  const [message, setMessage] = useState('')
  const [faqIdStr, setFaqIdStr] = useState<string>(
    preselectFaqId ? String(preselectFaqId) : NO_FAQ_VALUE,
  )
  const [urgency, setUrgency] = useState<Urgency>('low')
  const [contact, setContact] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const reset = () => {
    setMessage('')
    setFaqIdStr(preselectFaqId ? String(preselectFaqId) : NO_FAQ_VALUE)
    setUrgency('low')
    setContact('')
  }

  const canSubmit = message.trim().length >= 5 && !submitting

  const handleSubmit = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    const faqId = faqIdStr !== NO_FAQ_VALUE ? Number(faqIdStr) : null
    const result = await submitFeedback({
      faqId,
      message: message.trim(),
      urgency,
      contact: contact.trim(),
    })
    if (result.ok) {
      toast(result.message, {
        description: '管理员会跟进 · 答案补好后系统消息通知你',
      })
    } else {
      toast.warning(result.message, {
        description: '下次打开 FAQ 页时会自动重发',
      })
    }
    reset()
    setSubmitting(false)
    onOpenChange(false)
  }

  const handleOpenChange = (next: boolean) => {
    if (!next) reset()
    onOpenChange(next)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <MessageSquarePlus className="size-4" />
            给管理员提反馈
          </DialogTitle>
          <DialogDescription>
            没找到答案 / 想补条 FAQ / 答案不准, 都可以提。管理员会跟进。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* 反馈内容 */}
          <div className="space-y-1.5">
            <Label htmlFor="faq-feedback-message" className="text-sm">
              你想反馈啥? <span className="text-destructive">*</span>
            </Label>
            <Textarea
              id="faq-feedback-message"
              value={message}
              onChange={(e) => setMessage(e.target.value.slice(0, 500))}
              placeholder="例: 多次消耗算力叠加退款怎么算 / 监测一直没数据 / 想加个夜间模式 FAQ"
              rows={4}
              className="resize-none text-sm"
              autoFocus
            />
            <p className="text-right text-xs text-muted-foreground">{message.length} / 500</p>
          </div>

          {/* 关联 FAQ */}
          <div className="space-y-1.5">
            <Label className="text-sm">跟哪条问题相关?</Label>
            <Select value={faqIdStr} onValueChange={setFaqIdStr}>
              <SelectTrigger className="h-10">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_FAQ_VALUE}>无 · 通用反馈</SelectItem>
                {items.map((item) => (
                  <SelectItem key={item.id} value={String(item.id)}>
                    {item.question}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* 紧急度 */}
          <div className="space-y-1.5">
            <Label className="text-sm">这事多紧急?</Label>
            <div className="grid grid-cols-3 gap-2">
              {urgencyOptions.map((opt) => {
                const active = urgency === opt.value
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setUrgency(opt.value)}
                    className={`rounded-md border px-3 py-2 text-left transition-colors ${
                      active
                        ? 'border-primary bg-primary/10'
                        : 'border-border bg-card hover:bg-muted'
                    }`}
                  >
                    <div
                      className={`text-sm font-medium ${
                        active ? 'text-primary' : 'text-foreground'
                      }`}
                    >
                      {opt.title}
                    </div>
                    <div className="text-xs text-muted-foreground">{opt.desc}</div>
                  </button>
                )
              })}
            </div>
          </div>

          {/* 联系方式 */}
          <div className="space-y-1.5">
            <Label htmlFor="faq-feedback-contact" className="text-sm">
              联系方式(可选)
            </Label>
            <Input
              id="faq-feedback-contact"
              value={contact}
              onChange={(e) => setContact(e.target.value.slice(0, 50))}
              placeholder="默认通过系统消息回复你 · 想留手机/微信也行"
              className="h-10 text-sm"
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)}>
            取消
          </Button>
          <Button onClick={handleSubmit} disabled={!canSubmit}>
            {submitting ? '提交中...' : '提交反馈'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
