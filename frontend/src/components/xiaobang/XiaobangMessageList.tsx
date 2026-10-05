/**
 * XiaobangMessageList — 小榜消息列表
 *
 * 跟旧 旧消息列表 的核心差异:
 * - 不渲染 11 种 cards · 只渲染 markdown 正文 + SourceCard + 链接卡
 * - 不渲染 旧上下文卡 / Reasoning / 工具进度 step
 * - 空状态 4 个统一系统/运营快捷问题(lucide 图标 · 无 emoji)
 * - 2026-05-25 删头像:assistant 消息直接出气泡 · 极简风
 */
import { useEffect, useRef } from 'react'
import ReactMarkdown from '@/components/SafeMarkdown'
import { CalendarCheck2, CircleHelp, Route, ListChecks } from 'lucide-react'
import { cn } from '@/lib/utils'
import { XiaobangSourceCard } from './XiaobangSourceCard'
import { XiaobangExitBar } from './XiaobangExitBar'
import { XiaobangLinkCard } from './XiaobangLinkCard'
import { XiaobangClarifyCard } from './XiaobangClarifyCard'
import { XiaobangGapCard } from './XiaobangGapCard'
import type { XiaobangMessage } from '@/hooks/useXiaobangChat'

interface XiaobangMessageListProps {
  messages: XiaobangMessage[]
  isStreaming?: boolean
  isThinking?: boolean
  onAction?: (message: string) => void
  onLinkNavigate?: () => void
  className?: string
  /** [包 D①] 转人工要带的当前路由 */
  currentPage?: string
  /** [包 D①] 出口条的重试入口(重发上一句) */
  onRetry?: (message: string) => void
}

const QUICK_QUESTIONS: { text: string; Icon: typeof CalendarCheck2 }[] = [
  { text: '今天先做什么', Icon: CalendarCheck2 },
  { text: '这个页面怎么用', Icon: CircleHelp },
  { text: '下一步去哪', Icon: Route },
  { text: '为什么这样安排', Icon: ListChecks },
]

/**
 * 过滤 LLM 误吐的 emoji + 内部标签 leak(借鉴旧 旧消息列表 但更严格)
 * 1. 标签行: [tool_call] / [system_prompt] 等整行干掉
 * 2. emoji: 用 unicode property 干掉所有 emoji(per "前端禁用 emoji" 约定)
 */
const TAG_LEAK_RE = /^\s*\[(suggest_next_action|system_prompt|tool_call|function_call|action_card|confirm_card|select_card|link_card)\][^\n]*$/gm
const EMOJI_RE = /\p{Extended_Pictographic}/gu
const INTERNAL_CODE_LABELS: Record<string, string> = {
  brand_fill: '品牌信息 AI 填写',
  autofill_brand: '客户资料 AI 补齐',
  topic_gen: '选题生成',
  article_gen: '文章生成',
  article_rewrite: '文章补发/重写',
  deep_analyze: '品牌深度行业解析',
  profile_polish: '档案字段润色',
  geo_diagnosis: 'GEO 品牌体检',
  media_proxy_publish: '媒体代发',
}
function cleanText(raw: string): string {
  if (!raw) return raw
  let text = raw.replace(TAG_LEAK_RE, '').replace(EMOJI_RE, '')
  for (const [code, label] of Object.entries(INTERNAL_CODE_LABELS)) {
    text = text.replace(new RegExp(`\\b${code}\\b`, 'g'), label)
  }
  return text.replace(/\n{3,}/g, '\n\n')
}

const mdComponents = {
  table: ({ children, ...props }: React.ComponentPropsWithoutRef<'table'>) => (
    <table className="my-2 w-full border-collapse text-xs" {...props}>{children}</table>
  ),
  th: ({ children, ...props }: React.ComponentPropsWithoutRef<'th'>) => (
    <th className="border-b border-border px-2 py-1 text-left font-medium text-muted-foreground" {...props}>{children}</th>
  ),
  td: ({ children, ...props }: React.ComponentPropsWithoutRef<'td'>) => (
    <td className="border-b border-border/50 px-2 py-1" {...props}>{children}</td>
  ),
  // 模型/FAQ 正文里的 URL 永不拥有动作权限；可点击动作只来自签名 meta。
  a: ({ children }: React.ComponentPropsWithoutRef<'a'>) => (
    <span className="text-brand">{children}</span>
  ),
  code: ({ children, ...props }: React.ComponentPropsWithoutRef<'code'>) => (
    <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs" {...props}>{children}</code>
  ),
  ul: ({ children, ...props }: React.ComponentPropsWithoutRef<'ul'>) => (
    <ul className="list-inside list-disc space-y-1" {...props}>{children}</ul>
  ),
  ol: ({ children, ...props }: React.ComponentPropsWithoutRef<'ol'>) => (
    <ol className="list-inside list-decimal space-y-1" {...props}>{children}</ol>
  ),
  p: ({ children, ...props }: React.ComponentPropsWithoutRef<'p'>) => (
    <p className="my-1" {...props}>{children}</p>
  ),
  h3: ({ children, ...props }: React.ComponentPropsWithoutRef<'h3'>) => (
    <h3 className="mt-3 mb-1 text-sm font-semibold" {...props}>{children}</h3>
  ),
  h4: ({ children, ...props }: React.ComponentPropsWithoutRef<'h4'>) => (
    <h4 className="mt-2 mb-1 text-sm font-medium" {...props}>{children}</h4>
  ),
}

export function XiaobangMessageList({
  messages,
  isStreaming = false,
  isThinking = false,
  onAction,
  onLinkNavigate,
  className,
  currentPage,
  onRetry,
}: XiaobangMessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, messages[messages.length - 1]?.content])

  // ---- 空状态:4 个统一系统/运营快捷问题 ----
  if (messages.length === 0) {
    return (
      <div className={cn('flex flex-1 flex-col items-center justify-center px-4 py-6', className)}>
        <div className="w-full max-w-md space-y-4">
          <div className="flex flex-col items-center gap-2 text-center">
            <p className="text-base font-medium text-foreground">你好,我是小榜</p>
            <p className="text-xs leading-5 text-muted-foreground">
              不知道怎么开始?试试问我:
            </p>
          </div>
          <div className="grid gap-2">
            {QUICK_QUESTIONS.map(({ text, Icon }) => (
              <button
                key={text}
                type="button"
                onClick={() => onAction?.(text)}
                className={cn(
                  'flex min-h-11 items-center gap-2.5 rounded-lg border border-border/40 bg-muted/30 px-3 py-2',
                  'text-left text-xs text-muted-foreground transition-colors',
                  'hover:border-brand/30 hover:bg-muted hover:text-foreground',
                )}
              >
                <Icon className="size-4 shrink-0 text-brand" />
                <span>{text}</span>
              </button>
            ))}
          </div>
          <p className="text-center text-[11px] text-muted-foreground/60">
            找不到想问的?直接在下面打字
          </p>
        </div>
      </div>
    )
  }

  // ---- 正常对话流 ----
  const lastAssistantIdx = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') return i
    }
    return -1
  })()

  return (
    <div className={cn('flex-1 overflow-y-auto px-3 py-3 md:px-4 md:py-4', className)}>
      <div className="space-y-3">
        {messages.map((msg, idx) => (
          <MessageBubble
            key={msg.id}
            message={msg}
            isLast={idx === lastAssistantIdx}
            isThinking={isThinking && idx === lastAssistantIdx}
            onAction={onAction}
            onLinkNavigate={onLinkNavigate}
            currentPage={currentPage}
            onRetry={onRetry}
            precedingUserText={
              [...messages.slice(0, idx)].reverse()
                .find((m) => m.role === 'user')?.content || ''
            }
            priorTurns={messages.slice(0, idx)
              .filter((m) => !m.isStreaming && m.content.trim())
              .slice(-6)
              .map((m) => ({ role: m.role, content: m.content }))}
          />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

function MessageBubble({
  message,
  isLast,
  isThinking,
  onAction,
  onLinkNavigate,
  currentPage,
  onRetry,
  precedingUserText,
  priorTurns,
}: {
  message: XiaobangMessage
  isLast: boolean
  isThinking?: boolean
  onAction?: (msg: string) => void
  onLinkNavigate?: () => void
  currentPage?: string
  onRetry?: (msg: string) => void
  /** 这条回答对应的那句用户提问(转人工时人工要先看它) */
  precedingUserText: string
  /** 这条回答**之前**的有界对话 —— 不含它自己,也不含当前这句提问 */
  priorTurns: Array<{ role: string; content: string }>
}) {
  const isUser = message.role === 'user'

  // assistant 空内容且 streaming · 显示"思考中"占位
  if (!isUser && !message.content && !message.meta && message.isStreaming) {
    return (
      <div className="flex items-start">
        <div className="rounded-2xl bg-card px-3 py-2 text-sm text-muted-foreground">
          正在查资料
          <span className="ml-1 inline-flex">
            <span className="size-1 animate-pulse rounded-full bg-current" />
            <span className="size-1 animate-pulse rounded-full bg-current [animation-delay:200ms] ml-1" />
            <span className="size-1 animate-pulse rounded-full bg-current [animation-delay:400ms] ml-1" />
          </span>
        </div>
      </div>
    )
  }

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl bg-foreground px-3.5 py-2 text-sm text-background">
          {message.attachment && (
            <div className="mb-2 overflow-hidden rounded-xl border border-background/20 bg-background/10">
              {message.attachment.previewUrl ? (
                <img
                  src={message.attachment.previewUrl}
                  alt={message.attachment.title || '已附截图'}
                  className="max-h-48 w-full object-cover"
                />
              ) : (
                <div className="px-3 py-2 text-xs text-background/70">
                  已附截图（预览仅本次会话可见）
                </div>
              )}
              <div className="truncate border-t border-background/15 px-2 py-1 text-[11px] text-background/70">
                {message.attachment.title || '截图'}
              </div>
            </div>
          )}
          <p className="whitespace-pre-wrap leading-relaxed">{message.content}</p>
        </div>
      </div>
    )
  }

  // assistant 消息 · 正文 + (ClarifyCard 或 SourceCard+链接卡)
  // 反问澄清命中时只显示 ClarifyCard,不显示 content / source / link
  const cleaned = cleanText(message.content)
  const hasClarify = !!message.clarify && message.clarify.options.length > 0

  return (
    <div className="min-w-0 space-y-2">
      {hasClarify ? (
        <XiaobangClarifyCard
          clarify={message.clarify!}
          onPick={(q) => onAction?.(q)}
          disabled={!isLast}
        />
      ) : (
        <>
          {cleaned && (
            <div className="rounded-2xl bg-card px-3.5 py-2 text-sm text-foreground">
              <div className="leading-relaxed prose-sm max-w-none">
                <ReactMarkdown components={mdComponents}>
                  {cleaned}
                </ReactMarkdown>
                {message.isStreaming && (
                  <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-foreground/60 align-middle" />
                )}
              </div>
            </div>
          )}
          {/* [P4 · C1] 结构化建议排在来源之前:它是"接下来干什么",
              比"这句话出自哪篇文档"更该先被看到。 */}
          {message.meta?.gap_assistant && (
            <XiaobangGapCard
              assistant={message.meta.gap_assistant}
              onNavigate={onLinkNavigate}
            />
          )}
          {message.meta?.sources && message.meta.sources.length > 0 && (
            <XiaobangSourceCard meta={message.meta} />
          )}
          {message.meta?.link && (
            <XiaobangLinkCard link={message.meta.link} onNavigate={onLinkNavigate} />
          )}
          {/* [包 D①③④] 出口条排在**最后**:它是「这一轮没解决怎么办」,
              先让用户看完答案与依据,再给兜底出口。 */}
          {message.meta?.exits && message.meta.exits.length > 0 && (
            <XiaobangExitBar
              exits={message.meta.exits}
              question={precedingUserText}
              aiAnswer={message.content}
              currentPage={currentPage || ''}
              recentTurns={priorTurns}
              onRetry={() => onRetry?.(precedingUserText)}
              onClarify={() => onAction?.(precedingUserText)}
            />
          )}
        </>
      )}
    </div>
  )
}
