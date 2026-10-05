/**
 * XiaobangDrawer — 小榜抽屉(右侧 Sheet)+ FAB 入口
 *
 * 替代旧 旧 AI 助手抽屉 · 走纯问答 /api/xiaobang/chat 后端。
 * 跟旧的差异:
 * - 顶栏副标题改成"问我系统怎么用,我带你找答案"
 * - 不挂 旧上下文卡 / Reasoning · 不显示工具进度
 * - 截图模式自动隐藏(同旧 旧 AI 助手抽屉 · 复用 useScreenshotMode)
 * - 历史会话面板用 XiaobangHistoryPanel(命名空间独立 · 不和旧 agent 混)
 */
import { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { X, Plus, Trash2, History, MessageCircle } from 'lucide-react'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { cn } from '@/lib/utils'
import { useScreenshotMode } from '@/sandbox/screenshotMode'
import { useXiaobangChat, type XiaobangContextRefs } from '@/hooks/useXiaobangChat'
import { useXiaobangContext } from '@/hooks/useXiaobangContext'
import {
  useXiaobangSessions,
  xiaobangSessionContextForBrand,
  type XiaobangSessionMeta,
} from '@/hooks/useXiaobangSessions'
import { useClientContext } from '@/context/ClientContext'
import { AgentFAB } from '@/components/agent/AgentFAB'
import { XiaobangMessageList } from './XiaobangMessageList'
import { XiaobangChatInput } from './XiaobangChatInput'
import { XiaobangContextBar } from './XiaobangContextBar'

function positiveParam(params: URLSearchParams, ...names: string[]): number | undefined {
  for (const name of names) {
    const value = params.get(name)
    if (value && /^[1-9][0-9]*$/.test(value)) return Number(value)
  }
  return undefined
}

export function helpTargetForRoute(route: string): string {
  const path = route.split(/[?#]/, 1)[0].toLowerCase()
  const slug = path.replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'root'
  return `route-${slug}`
}

function formatTime(ts: number): string {
  const diff = Date.now() - ts
  if (diff < 60_000) return '刚刚'
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)}分钟前`
  if (diff < 86400_000) return `${Math.floor(diff / 3600_000)}小时前`
  const days = Math.floor(diff / 86400_000)
  if (days === 1) return '昨天'
  if (days < 7) return `${days}天前`
  return new Date(ts).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

export function XiaobangDrawer({ initialOpen = false }: { initialOpen?: boolean }) {
  const [open, setOpen] = useState(initialOpen)
  const [showHistory, setShowHistory] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()
  const screenshotMode = useScreenshotMode()
  const { currentBrandId } = useClientContext()

  // 浏览器只提交引用提示；服务端逐项重验客户、报价和任务归属。
  // 刻意不从 relatedQuoteIds 猜报价：仅有当前品牌时由服务端选择并重新鉴权。
  const contextRefs = useMemo<XiaobangContextRefs | undefined>(() => {
    const params = new URLSearchParams(location.search)
    const refs: XiaobangContextRefs = {
      brand_id: currentBrandId || positiveParam(params, 'brand_id', 'client_id'),
      quote_id: positiveParam(params, 'quote_id'),
      article_id: positiveParam(params, 'article_id'),
      publication_id: positiveParam(params, 'publication_id', 'publication_task_id'),
      monitoring_task_id: positiveParam(params, 'monitoring_task_id', 'task_id'),
    }
    return Object.values(refs).some((value) => value !== undefined) ? refs : undefined
  }, [currentBrandId, location.search])
  const currentPage = `${location.pathname}${location.search}`
  const serverContext = useXiaobangContext(currentPage, contextRefs)
  const sessionContextKey = xiaobangSessionContextForBrand(
    // 客户切换事件先于 context API 回包；优先使用刚选择的 brand hint 立即隔离
    // 本地历史，服务端仍会在 /context 与 /chat 逐项重验归属。
    contextRefs?.brand_id || serverContext.data?.context?.brand_id,
  )
  const {
    sessions,
    activeSessionId,
    createSession,
    selectSession,
    deleteSession,
    upsertSession,
  } = useXiaobangSessions(sessionContextKey)

  const chat = useXiaobangChat({
    sessionId: activeSessionId,
    currentPage,
    contextRefs,
    onMessagesChange: upsertSession,
  })

  // 客户一变立刻切到空会话，旧客户回答不继续参与当前上下文。
  const previousSessionContextRef = useRef(sessionContextKey)
  useEffect(() => {
    if (previousSessionContextRef.current === sessionContextKey) return
    previousSessionContextRef.current = sessionContextKey
    chat.cancelStreaming()
    setShowHistory(false)
  }, [chat.cancelStreaming, sessionContextKey])

  // 不修改共享 sidebar/page 文件：在运行时把注册表的稳定 target 绑定到现役真实链接；
  // 当前路由没有侧栏入口时再绑定真实 page main 容器。
  useEffect(() => {
    const restored = new Map<HTMLElement, string | null>()
    const bindLinks = () => {
      document.querySelectorAll<HTMLAnchorElement>('a[href]').forEach((anchor) => {
        if (restored.has(anchor)) return
        let path = ''
        try {
          path = new URL(anchor.href, window.location.origin).pathname
        } catch {
          return
        }
        if (!path.startsWith('/')) return
        restored.set(anchor, anchor.getAttribute('data-help-target'))
        anchor.setAttribute('data-help-target', helpTargetForRoute(path))
      })
    }
    bindLinks()
    const observer = new MutationObserver(bindLinks)
    observer.observe(document.body, { childList: true, subtree: true })
    const main = document.querySelector<HTMLElement>('main')
    if (main) {
      restored.set(main, main.getAttribute('data-help-target'))
      main.setAttribute('data-help-target', helpTargetForRoute(location.pathname))
    }
    return () => {
      observer.disconnect()
      restored.forEach((previous, element) => {
        if (previous === null) element.removeAttribute('data-help-target')
        else element.setAttribute('data-help-target', previous)
      })
    }
  }, [location.pathname])

  // 导航落地后定位服务端签发的 target。优先真实链接/按钮，再退到页面区域。
  useEffect(() => {
    const state = location.state as { xiaobangHelpTarget?: string } | null
    const target = state?.xiaobangHelpTarget
    if (!target || !/^[a-z0-9-]+$/i.test(target)) return
    let attempts = 0
    let cleanupTimer: number | undefined
    let retryTimer: number | undefined
    let highlighted: HTMLElement | undefined
    let injectedTarget = false
    const findAndHighlight = () => {
      const matches = Array.from(
        document.querySelectorAll<HTMLElement>(`[data-help-target="${target}"]`),
      )
      const visible = matches.filter((item) => {
        const rect = item.getBoundingClientRect()
        return rect.width > 0 && rect.height > 0
      })
      let element = visible.find((item) => item.matches('a,button,[role="button"]')) || visible[0]
      if (element?.matches('main')) {
        const control = Array.from(
          element.querySelectorAll<HTMLElement>('button,a,[role="button"],input,select'),
        ).find((item) => {
          const rect = item.getBoundingClientRect()
          return rect.width > 0 && rect.height > 0 && !item.hasAttribute('disabled')
        })
        if (control) {
          element = control
          injectedTarget = !element.hasAttribute('data-help-target')
          element.setAttribute('data-help-target', target)
        }
      }
      if (!element && attempts++ < 20) {
        retryTimer = window.setTimeout(findAndHighlight, 100)
        return
      }
      if (!element) return
      highlighted = element
      element.dataset.xiaobangHighlighted = 'true'
      element.classList.add('ring-2', 'ring-brand', 'ring-offset-2', 'ring-offset-background')
      element.scrollIntoView({ block: 'center', behavior: 'smooth' })
      if (element.matches('a,button,[tabindex]')) element.focus({ preventScroll: true })
      cleanupTimer = window.setTimeout(() => {
        element.classList.remove('ring-2', 'ring-brand', 'ring-offset-2', 'ring-offset-background')
        delete element.dataset.xiaobangHighlighted
      }, 3000)
    }
    findAndHighlight()
    return () => {
      if (retryTimer) window.clearTimeout(retryTimer)
      if (cleanupTimer) window.clearTimeout(cleanupTimer)
      if (highlighted) {
        highlighted.classList.remove('ring-2', 'ring-brand', 'ring-offset-2', 'ring-offset-background')
        delete highlighted.dataset.xiaobangHighlighted
        if (injectedTarget) highlighted.removeAttribute('data-help-target')
      }
    }
  }, [location.key, location.state])

  const handleSend = useCallback(
    (
      text: string,
      options?: { attachmentText?: string; attachmentTitle?: string; attachmentPreviewUrl?: string },
    ) => {
      chat.sendMessage(text, options)
    },
    [chat],
  )

  const handleNewSession = () => {
    createSession()
    setShowHistory(false)
  }

  const handleSelectSession = (id: string) => {
    selectSession(id)
    setShowHistory(false)
  }

  // 截图模式时不渲染(不弹 FAB / 不出现在截图里)
  if (screenshotMode) return null

  return (
    <>
      <AgentFAB onClick={() => setOpen(true)} />
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent
          showCloseButton={false}
          className={cn(
            'h-dvh w-full p-0 sm:h-full sm:w-[420px] sm:max-w-[420px]',
            'flex flex-col border-l border-border/50 bg-background',
          )}
        >
          {/* Header */}
          <SheetHeader className="shrink-0 border-b border-border/30 px-3 py-2.5">
            <div className="flex items-center justify-between">
              <div className="min-w-0">
                <SheetTitle className="text-sm font-medium text-foreground">
                  小榜 · GEO 助手
                </SheetTitle>
                <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
                  问我系统怎么用,我带你找答案
                </p>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={handleNewSession}
                  title="新对话"
                  className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
                >
                  <Plus className="size-4" />
                </button>
                <button
                  onClick={() => setShowHistory(!showHistory)}
                  title="历史记录"
                  className={cn(
                    'rounded-md p-1.5 transition-colors',
                    showHistory
                      ? 'bg-muted/60 text-foreground'
                      : 'text-muted-foreground hover:bg-muted/50 hover:text-foreground',
                  )}
                >
                  <History className="size-4" />
                </button>
                <button
                  onClick={() => setOpen(false)}
                  title="关闭"
                  className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
                >
                  <X className="size-4" />
                </button>
              </div>
            </div>
          </SheetHeader>

          {!showHistory && (
            <XiaobangContextBar
              context={serverContext.data?.context}
              operationPlan={serverContext.data?.operation_plan}
              loading={serverContext.loading}
              onChooseCustomer={() => {
                setOpen(false)
                navigate('/my-clients', {
                  state: { xiaobangHelpTarget: helpTargetForRoute('/my-clients') },
                })
              }}
            />
          )}

          {showHistory ? (
            <HistoryPanel
              sessions={sessions}
              activeSessionId={activeSessionId}
              onSelect={handleSelectSession}
              onDelete={deleteSession}
              onNew={handleNewSession}
            />
          ) : (
            <>
              <XiaobangMessageList
                messages={chat.messages}
                isStreaming={chat.isStreaming}
                isThinking={chat.isThinking}
                onAction={handleSend}
                onLinkNavigate={() => setOpen(false)}
                currentPage={currentPage}
                onRetry={handleSend}
                className="flex-1 min-h-0"
              />
              <XiaobangChatInput
                onSend={handleSend}
                onStop={chat.cancelStreaming}
                disabled={chat.isStreaming}
                isStreaming={chat.isStreaming}
                className="shrink-0"
              />
            </>
          )}
        </SheetContent>
      </Sheet>
    </>
  )
}

function HistoryPanel({
  sessions,
  activeSessionId,
  onSelect,
  onDelete,
  onNew,
}: {
  sessions: XiaobangSessionMeta[]
  activeSessionId: string
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  onNew: () => void
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 px-3 py-2">
        <button
          onClick={onNew}
          className={cn(
            'flex w-full items-center gap-2 rounded-lg border border-border/40 px-3 py-2 text-sm',
            'text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground',
          )}
        >
          <Plus className="size-4" />
          <span>新对话</span>
        </button>
      </div>
      <div className="flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
        {sessions.length === 0 && (
          <p className="py-8 text-center text-xs text-muted-foreground/40">暂无历史对话</p>
        )}
        {sessions.map((session) => (
          <div
            key={session.id}
            className={cn(
              'group flex cursor-pointer items-start gap-2 rounded-lg px-3 py-2.5 transition-colors',
              session.id === activeSessionId
                ? 'bg-muted/60 text-foreground'
                : 'text-muted-foreground hover:bg-muted/30 hover:text-foreground',
            )}
            onClick={() => onSelect(session.id)}
          >
            <MessageCircle className="mt-0.5 size-4 shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm">{session.title}</p>
              <p className="mt-0.5 text-[11px] text-muted-foreground/60">
                {formatTime(session.updatedAt)}
                {session.messageCount > 0 && ` · ${session.messageCount}条`}
              </p>
            </div>
            <button
              onClick={(e) => {
                e.stopPropagation()
                onDelete(session.id)
              }}
              className={cn(
                'shrink-0 rounded p-1 text-muted-foreground/40 opacity-0 transition-opacity',
                'hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100',
              )}
            >
              <Trash2 className="size-3.5" />
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

export default XiaobangDrawer
