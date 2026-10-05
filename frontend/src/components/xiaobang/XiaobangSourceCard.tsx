/**
 * XiaobangSourceCard — 答案下方的"来自《XXX》文档"引用块
 *
 * 3 种样式(对应 confidence: high/medium/low):
 * - high/medium · 浅灰底 + BookOpen 图标 · "来自《X》文档"
 * - low         · 浅琥珀底 + Info 图标   · "我没有直接的答案,以上是根据《X》推测的"
 *
 * 多来源时显示 "综合了以下文档" + 列表,跳转链接走第一个来源。
 */
import { BookOpen, Info, ArrowRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { XiaobangSource, XiaobangMeta } from '@/hooks/useXiaobangChat'

interface XiaobangSourceCardProps {
  meta: XiaobangMeta
}

function buildDocUrl(source: XiaobangSource): string {
  // doc → /help/docs/{slug}#section · faq → /help/docs/{slug}(faq 单独页暂无,先指 help)
  if (source.source_type === 'faq') return `/help#faq-${source.source_slug.replace(/^faq_/, '')}`
  if (source.source_type === 'preset') return '/help'
  // 🔴 [包 D⑥ 2026-08-21 · 工单 §4 P1-7] 系统知识库(sys)的 chunk 用**路由本身**
  //    当 source_slug(`tools/xiaobang_system_kb.py`:`"source_slug": route` /
  //    `f"{route}#btn-..."`),所以它以 `/` 开头。
  //    原来这里无脑拼 `/help/docs/${slug}` ⇒ 生成 `/help/docs//monitoring`
  //    —— 双斜杠 + 一个根本不存在的文档页。
  //    这类来源指的就是**那个页面**,直接给页面路由(带上 #锚点)。
  if (source.source_slug.startsWith('/')) return source.source_slug
  return `/help/docs/${source.source_slug}`
}

export function XiaobangSourceCard({ meta }: XiaobangSourceCardProps) {
  const { sources, confidence } = meta
  if (!sources || sources.length === 0) return null

  const isLow = confidence === 'low'
  const Icon = isLow ? Info : BookOpen
  const top = sources[0]
  const isMulti = sources.length > 1

  return (
    <div
      className={cn(
        'rounded-lg border px-3 py-2 text-xs',
        isLow
          ? 'border-amber-200/60 bg-amber-50/60 text-amber-900 dark:border-amber-800/40 dark:bg-amber-950/30 dark:text-amber-200'
          : 'border-border/40 bg-muted/40 text-muted-foreground',
      )}
    >
      <div className="flex items-start gap-2">
        <Icon className={cn('mt-0.5 size-3.5 shrink-0', isLow ? 'text-amber-600' : 'text-brand')} />
        <div className="min-w-0 flex-1">
          {isLow ? (
            <p className="leading-relaxed">
              我没有直接的答案,以上是根据《{top.source_title}》
              {top.section_title && `· ${top.section_title}`}的内容推测的。建议点下面去看原文核对。
            </p>
          ) : isMulti ? (
            <>
              <p>综合了以下文档:</p>
              <ul className="mt-1 space-y-0.5">
                {sources.slice(0, 3).map((s) => (
                  <li key={s.source_slug} className="truncate">
                    · 《{s.source_title}》{s.section_title && (
                      <span className="opacity-70"> · {s.section_title}</span>
                    )}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="truncate">
              来自《{top.source_title}》
              {top.section_title && (
                <span className="opacity-70"> · {top.section_title}</span>
              )}
            </p>
          )}
          <a
            href={buildDocUrl(top)}
            target="_blank"
            rel="noopener noreferrer"
            className={cn(
              'mt-1.5 inline-flex items-center gap-0.5 font-medium hover:underline',
              isLow ? 'text-amber-700 dark:text-amber-300' : 'text-brand',
            )}
          >
            看{isMulti ? `《${top.source_title}》` : '完整文档'}
            <ArrowRight className="size-3" />
          </a>
        </div>
      </div>
    </div>
  )
}
