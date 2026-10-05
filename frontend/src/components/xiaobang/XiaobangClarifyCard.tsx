/**
 * XiaobangClarifyCard — 反问澄清卡片
 *
 * 后端 BM25 检索到 top1/top2 分数接近且来自不同文档时,不猜,
 * 让用户选具体想问的方向。点击候选项 → 自动追问对应问题。
 *
 * 视觉:在 assistant 消息体内显示一段提示 + 2-3 个候选 chip(按钮)
 * 跟答案、SourceCard、链接卡 是互斥关系(命中反问时不显示这三个)
 */
import { HelpCircle, ArrowRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { XiaobangClarify } from '@/hooks/useXiaobangChat'

interface XiaobangClarifyCardProps {
  clarify: XiaobangClarify
  onPick: (query: string) => void
  disabled?: boolean
}

export function XiaobangClarifyCard({ clarify, onPick, disabled = false }: XiaobangClarifyCardProps) {
  if (!clarify.options || clarify.options.length === 0) return null

  return (
    <div className="space-y-2">
      <div className="flex items-start gap-2 rounded-2xl bg-card px-3.5 py-2 text-sm text-foreground">
        <HelpCircle className="mt-0.5 size-4 shrink-0 text-brand" />
        <p className="leading-relaxed">{clarify.prompt}</p>
      </div>
      <div className="flex flex-col gap-2">
        {clarify.options.map((opt) => (
          <button
            key={opt.source_slug + '|' + opt.section_title}
            type="button"
            onClick={() => onPick(opt.query)}
            disabled={disabled}
            className={cn(
              'group flex w-full items-center justify-between gap-2 rounded-lg border border-border/40',
              'bg-muted/30 px-3 py-2 text-left text-xs text-foreground transition-all',
              disabled
                ? 'cursor-not-allowed opacity-50'
                : 'hover:border-brand/40 hover:bg-muted hover:shadow-sm',
            )}
          >
            <div className="min-w-0 flex-1">
              <div className="truncate font-medium">{opt.label}</div>
              {opt.section_title && opt.section_title.length > 16 && (
                <div className="mt-0.5 truncate text-[11px] text-muted-foreground/70">
                  · {opt.section_title}
                </div>
              )}
            </div>
            <ArrowRight className="size-3.5 shrink-0 text-muted-foreground/60 group-hover:text-brand" />
          </button>
        ))}
      </div>
    </div>
  )
}
