/**
 * useXiaobangChat — 小榜纯问答聊天 hook
 *
 * 设计原则(对比旧 旧对话 hook):
 * - 没有工具调用 · 没有 11 种 cards · 没有意图分类 · 没有钱包扣费 diff
 * - 只接 `/api/xiaobang/chat` SSE 流:text delta + 一次性 meta(sources + link) + done
 * - 答案只有 3 种形态:正文 markdown / SourceCard(知识库出处)/ 链接卡(跳转按钮)
 * - 总超时 60s(纯检索 + LLM 远低于旧 agent 工具链的 170s)
 */
import { useState, useCallback, useRef, useEffect } from 'react'
import { useAuth } from '@/context/AuthContext'
import {
  saveSessionMessages,
  loadSessionMessages,
  useXiaobangStorageNamespace,
} from './useXiaobangSessions'

/**
 * 前端**能渲染**的注册表版本集合(协议协商,不是版本相等)。
 *
 * 🔴 债务 §4.1-7 的根因就是这里原本是一个常量 + 整版相等判断:后端一升
 *    operation-registry-v2 → v3,所有动作会被整体过滤掉 —— 用户看到的是
 *    "小榜答了,但一个按钮都没有",而且前后端各自都"没报错"。
 *
 * 现在的合同是两层:
 *   1. 服务端在 meta 里下发 `operation_map_compatible_versions`(权威兼容表);
 *   2. 前端声明自己认得哪些版本(动作卡形状没变的版本都认)。
 * 取两者交集。升版时服务端**只加不删**,旧 bundle 因此永远不会被一刀切掉。
 */
export const SUPPORTED_OPERATION_REGISTRY_VERSIONS: readonly string[] = [
  'operation-registry-v2',
  'operation-registry-v3',
]

/** @deprecated 保留仅为兼容旧 import;判版请用 SUPPORTED_OPERATION_REGISTRY_VERSIONS。 */
export const CURRENT_OPERATION_REGISTRY_VERSION = 'operation-registry-v3'

export interface XiaobangSource {
  /** 文档 slug 或 'faq_{id}' · 用于详情跳转 */
  source_slug: string
  /** 显示标题:《品牌体检》/《扣费失败会退款吗》 */
  source_title: string
  /** 文档内 H2 章节,FAQ 可空 */
  section_title?: string
  /** 'doc' | 'faq' · 渲染时用不同图标 */
  source_type: 'doc' | 'faq' | 'preset'
}

export interface XiaobangLink {
  /** 路由路径,如 /diagnosis/new */
  route: string
  /** 按钮文案,如 "去发起诊断" */
  label: string
  /**
   * [WO-B2 ① 2026-08-20] 这个路由背后**有合同的** operation id;
   * 服务端从注册表机械派生(`commandable_operation_for_route`),没有就不下发。
   *
   * 🔴 有它 = 这一跳可以先 prepare 一个 intent、带着 `?xiaobang_intent=` 过去,
   *    目标页就能预填而不是打开空白默认表单(规格 §12.2/§12.3)。
   *    没有它 = 老行为:直接 navigate。
   */
  operation_id?: string
}

export interface XiaobangMeta {
  /** 引用的 KB 来源(最多 3 个) */
  sources: XiaobangSource[]
  /** 推荐跳转(最多 1 个,top 来源的 route) */
  link?: XiaobangLink
  /** 检索置信度 · 影响 SourceCard 样式:high/medium 正常蓝,low 琥珀 */
  confidence: 'high' | 'medium' | 'low'
  /** [P4 缺口作战计划 2026-08-08 · C1] 服务端签发的结构化建议与动作。
   *  🔴 动作只能从这里来 —— 模型文本里的任何"路径"都不执行,
   *     `target_route` 由版本化操作地图签发,前端不拼、不猜、不兜底旧路径。 */
  gap_assistant?: XiaobangGapAssistant
  assistant_context?: XiaobangAssistantContext
  /**
   * [包 D① 2026-08-21 · 工单 §4 P1-4] 后端**一直在发**的人工接管信号。
   *
   * 🔴 后端 `api/xiaobang_api.py` 低置信兜底那支会下发 `handoff: true`
   *    并在正文里说「点下方把问题反馈给工作人员」——
   *    而这里的类型与下面的 SSE 解析器**两处都不保留它**,
   *    于是数据到了浏览器就被扔掉:用户看到「点下方」,下方什么都没有。
   *    这跟 gap_assistant 那次是同一种病(类型声明有、数据到了、然后被扔)。
   */
  handoff?: boolean
  should_escalate?: boolean
  /**
   * [包 D① 2026-08-21] 服务端签发的**出口**清单。
   * 每一轮失败/低置信至少有一条(后端 `assert_has_exit` 守着)。
   * 前端只按 `exit_id` 渲染,不猜文案、不自己造出口。
   */
  exits?: XiaobangExit[]
}

export interface XiaobangExit {
  exit_id: 'clarify' | 'retry' | 'manual_path' | 'handoff' | 'resolved' | 'unresolved'
  label: string
  /** handoff 专有:告诉用户**会提交什么**,不许悄悄外发 */
  submits?: string[]
}

export interface XiaobangAssistantContext {
  brand_id?: number | null
  brand_name?: string | null
  quote_id?: number | null
  current_page?: string
  page_name?: string
  data_updated_at?: string | null
  has_customer?: boolean
}

export interface XiaobangEvidence {
  label: string
  value: string
}

export interface XiaobangGapAction {
  action_id: string
  operation_id: string
  label: string
  icon?: string
  confirmation: boolean
  enabled: boolean
  /** 服务端签发的现役路由。没有它就不渲染跳转按钮。 */
  target_route?: string
  /** 页面内 data-help-target 控件标识,用于"高亮入口" */
  help_target?: string
  hint?: string
  registry_version?: string
  primary: boolean
}

export interface XiaobangGapAssistant {
  headline: string
  reasons: string[]
  actions: XiaobangGapAction[]
  breadcrumb: string[]
  plan_snapshot_id?: string | null
  snapshot_version?: string | null
  authority_generation?: number | null
  operation_map_version: string
  operation_map_compatible_versions?: string[]
  /** fail-closed 降级态:只影响解释,页面数据不受影响 */
  degraded: boolean
  context?: XiaobangAssistantContext
  plan_id?: string | null
  evidence: XiaobangEvidence[]
}

export interface XiaobangContextRefs {
  brand_id?: number
  client_id?: number
  quote_id?: number
  article_id?: number
  publication_id?: number
  publication_task_id?: number
  monitoring_task_id?: number
  plan_item_id?: string
  authority_generation?: number
}

/** 反问澄清的候选项 · 命中时不调 LLM,让用户先选意图 */
export interface XiaobangClarifyOption {
  label: string
  query: string
  source_slug: string
  source_title: string
  section_title?: string
  source_type: 'doc' | 'faq' | 'preset'
}

export interface XiaobangClarify {
  prompt: string
  options: XiaobangClarifyOption[]
}

export interface XiaobangMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  attachment?: {
    title: string
    previewUrl?: string
  }
  meta?: XiaobangMeta
  /** 反问澄清(后端检测到歧义时 · 跟 content 互斥) */
  clarify?: XiaobangClarify
  isStreaming: boolean
  timestamp: number
}

export interface XiaobangSendOptions {
  attachmentText?: string
  attachmentTitle?: string
  attachmentPreviewUrl?: string
}

interface UseXiaobangChatOptions {
  sessionId?: string
  currentPage?: string
  /** [P4 · C1] 引用式客户上下文。🔴 只传引用不传事实 ——
   *  浏览器手里没有采购系数/成本/快照正文,改不了服务端的判断。 */
  contextRefs?: XiaobangContextRefs
  onMessagesChange?: (sessionId: string, firstUserMessage: string, count: number) => void
}

let msgCounter = 0
function genId() {
  return `xb_msg_${Date.now()}_${++msgCounter}`
}

/**
 * [P4 · C1] 只保留服务端签发的字段,形状不对就当没有。
 *
 * 🔴 `target_route` **原样带过来,前端一个字符都不改** —— 它由版本化操作地图签发。
 *    这里既不补前缀、也不在缺失时回落到任何默认路由:猜错路径比不给按钮更糟
 *    (运营点进去看到别的客户/别的页面,比"没按钮"难查得多)。
 */
export function normalizeGapAssistant(raw: unknown): XiaobangGapAssistant | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const src = raw as Record<string, unknown>
  const headline = typeof src.headline === 'string' ? src.headline.trim() : ''
  if (!headline) return undefined
  const operationMapVersion = typeof src.operation_map_version === 'string'
    ? src.operation_map_version
    : ''
  const serverCompatible = Array.isArray(src.operation_map_compatible_versions)
    ? (src.operation_map_compatible_versions as unknown[]).filter(
        (v): v is string => typeof v === 'string',
      )
    : []
  const actions = (Array.isArray(src.actions) ? src.actions : [])
    .filter((a): a is Record<string, unknown> => !!a && typeof a === 'object')
    .filter((a) =>
      typeof a.action_id === 'string'
      && typeof a.operation_id === 'string'
      && typeof a.label === 'string',
    )
    .map((a) => ({
      action_id: a.action_id as string,
      operation_id: a.operation_id as string,
      label: a.label as string,
      icon: typeof a.icon === 'string' ? a.icon : undefined,
      confirmation: a.confirmation === true,
      enabled: a.enabled !== false,
      target_route: typeof a.target_route === 'string' ? a.target_route : undefined,
      help_target: typeof a.help_target === 'string' ? a.help_target : undefined,
      hint: typeof a.hint === 'string' ? a.hint : undefined,
      registry_version: typeof a.registry_version === 'string' ? a.registry_version : undefined,
      primary: a.primary === true,
    }))
    .filter((a) => {
      // 动作必须自报版本,且该版本同时被服务端兼容表与本 bundle 认可。
      // 服务端没下发兼容表(旧后端)时退回"等于本次 payload 版本",
      // 与升级前行为一致 —— 降级要能说清降到哪一档,不能悄悄放行任意版本。
      const v = a.registry_version
      if (!v) return false
      if (!SUPPORTED_OPERATION_REGISTRY_VERSIONS.includes(v)) return false
      if (serverCompatible.length > 0) return serverCompatible.includes(v)
      return v === operationMapVersion
    })
    .slice(0, 2)
  return {
    headline,
    reasons: (Array.isArray(src.reasons) ? src.reasons : []).filter(
      (r): r is string => typeof r === 'string' && !!r.trim(),
    ).slice(0, 3),
    actions,
    breadcrumb: (Array.isArray(src.breadcrumb) ? src.breadcrumb : []).filter(
      (b): b is string => typeof b === 'string',
    ),
    plan_snapshot_id: typeof src.plan_snapshot_id === 'string' ? src.plan_snapshot_id : null,
    snapshot_version: typeof src.snapshot_version === 'string' ? src.snapshot_version : null,
    authority_generation:
      typeof src.authority_generation === 'number' ? src.authority_generation : null,
    operation_map_version: operationMapVersion,
    degraded: src.degraded === true,
    context: src.context && typeof src.context === 'object'
      ? src.context as XiaobangAssistantContext
      : undefined,
    plan_id: typeof src.plan_id === 'string' ? src.plan_id : null,
    evidence: (Array.isArray(src.evidence) ? src.evidence : [])
      .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
      .filter((item) => typeof item.label === 'string' && typeof item.value === 'string')
      .map((item) => ({ label: item.label as string, value: item.value as string })),
  }
}

/**
 * [包 A③④ 2026-08-21] 浏览器侧挑出**有界最近对话**随请求带上去。
 *
 * 🔴 这里**不做「什么算合法」的判断** —— 那个谓词的唯一权威在服务端
 *    `services/xiaobang_recent_turns.py`。同一个谓词写两处必有一处没人验;
 *    浏览器这头只负责「少发点」(省带宽 + 别把正在流的那条发出去)。
 *
 * 🔴 只取 role/content 两个字段:
 *    · `attachment` 一律不进历史(§6 包 A④ —— 旧截图的二进制/OCR 不塞进上文;
 *      本轮的截图走 `attachment_text`,只作用于本轮);
 *    · `meta` 不进(里面有结构化动作/来源,不是用户说过的话);
 *    · 还在流的那条(isStreaming)不进 —— 它内容还没写完。
 */
const RECENT_TURNS_MAX = 6

function toRecentTurns(messages: XiaobangMessage[]): Array<{ role: string; content: string }> {
  return messages
    .filter((m) => !m.isStreaming)
    .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.content.trim())
    .slice(-RECENT_TURNS_MAX)
    .map((m) => ({ role: m.role, content: m.content }))
}

export function useXiaobangChat(options: UseXiaobangChatOptions = {}) {
  const { token } = useAuth()
  // 🔴 H0:必须在下面的 useState 初始化器之前发布命名空间 —— `loadSessionMessages`
  //    是同步读 localStorage 的普通函数,命名空间没就位它读到的就是别人的池。
  const storageNamespace = useXiaobangStorageNamespace()
  const sessionId = options.sessionId || 'default'
  const [messages, setMessages] = useState<XiaobangMessage[]>(
    () => loadSessionMessages(sessionId) as XiaobangMessage[],
  )
  const [isStreaming, setIsStreaming] = useState(false)
  const [isThinking, setIsThinking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const transientPreviewUrlsRef = useRef<string[]>([])

  // 🔴 H0:消息池的作用域是「账号 × 会话」,不只是会话。
  //    换账号时若只按 sessionId 判,B 会继续挂着 A 的 messages(sessionId 没变)。
  //    与 useXiaobangSessions 同理走**渲染期换池**,不让 A 的正文在 B 的屏上留一帧。
  const messageScopeKey = `${storageNamespace ?? 'unauthenticated'}::${sessionId}`
  const [messageScopeSnapshot, setMessageScopeSnapshot] = useState(messageScopeKey)
  if (messageScopeSnapshot !== messageScopeKey) {
    setMessageScopeSnapshot(messageScopeKey)
    setMessages(loadSessionMessages(sessionId) as XiaobangMessage[])
    setError(null)
    setIsStreaming(false)
    setIsThinking(false)
  }

  const persistMessages = useCallback((items: XiaobangMessage[]) => {
    const serializable = items.map((m) =>
      m.attachment?.previewUrl
        ? { ...m, attachment: { title: m.attachment.title } }
        : m,
    )
    saveSessionMessages(sessionId, serializable)
  }, [sessionId])

  useEffect(() => {
    return () => {
      transientPreviewUrlsRef.current.forEach((url) => {
        if (url.startsWith('blob:')) URL.revokeObjectURL(url)
      })
      transientPreviewUrlsRef.current = []
    }
  }, [])

  // 账号 / session 切换 · 掐掉仍在流的上一轮(state 已在渲染期换过,这里只管副作用)
  useEffect(() => {
    return () => {
      abortRef.current?.abort()
      abortRef.current = null
    }
  }, [messageScopeKey])

  const sendMessage = useCallback(
    async (text: string, sendOptions: XiaobangSendOptions = {}) => {
      const trimmed = text.trim()
      const attachmentText = (sendOptions.attachmentText || '').trim()
      const attachmentTitle = (sendOptions.attachmentTitle || '').trim()
      const attachmentPreviewUrl = (sendOptions.attachmentPreviewUrl || '').trim()
      if ((!trimmed && !attachmentText) || isStreaming) return
      const displayText = trimmed || '请帮我看一下这张截图，我该怎么操作？'
      if (attachmentPreviewUrl.startsWith('blob:')) {
        transientPreviewUrlsRef.current.push(attachmentPreviewUrl)
      }

      const userMsg: XiaobangMessage = {
        id: genId(),
        role: 'user',
        content: displayText,
        attachment: attachmentTitle
          ? { title: attachmentTitle, previewUrl: attachmentPreviewUrl || undefined }
          : undefined,
        isStreaming: false,
        timestamp: Date.now(),
      }
      const assistantId = genId()
      const assistantMsg: XiaobangMessage = {
        id: assistantId,
        role: 'assistant',
        content: '',
        isStreaming: true,
        timestamp: Date.now(),
      }

      // 🔴 必须在 setMessages 之前取:取完之后 messages 里就多了当前这一轮,
      //    当前问题会**重复**出现在 history 和 message 两处。
      const recentTurns = toRecentTurns(messages)

      setMessages((prev) => [...prev, userMsg, assistantMsg])
      setIsStreaming(true)
      setIsThinking(true)
      setError(null)

      const controller = new AbortController()
      abortRef.current = controller

      // 60s 总超时(纯检索 + LLM 一般 3-8s · 60s 给足缓冲)
      const timeoutId = setTimeout(() => {
        controller.abort()
      }, 60_000)

      const finishStream = (errMsg?: string) => {
        clearTimeout(timeoutId)
        setIsStreaming(false)
        setIsThinking(false)
        setMessages((prev) => {
          const updated = prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content: errMsg ? m.content || `出错了:${errMsg}` : m.content,
                  isStreaming: false,
                }
              : m,
          )
          persistMessages(updated)
          const firstUser = updated.find((m) => m.role === 'user')
          if (firstUser) {
            options.onMessagesChange?.(sessionId, firstUser.content, updated.length)
          }
          return updated
        })
      }

      try {
        const resp = await fetch('/api/xiaobang/chat', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'text/event-stream',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            message: displayText,
            session_id: sessionId,
            current_page: options.currentPage || '',
            attachment_text: attachmentText || undefined,
            // [P4 · C1] 两头改的"前端这头"(工单 R7):
            // 只加引用,不加事实;没有上下文时字段整个不出现,后端行为与本包上线前一致。
            context_refs: options.contextRefs && Object.keys(options.contextRefs).length
              ? options.contextRefs : undefined,
            // [包 A③] 有界最近对话。`messages` 是本次 send 之前的快照,
            // 因此**不含**刚 push 进去的 userMsg/assistantMsg(当前这轮由 message 字段承载)。
            recent_turns: recentTurns.length ? recentTurns : undefined,
          }),
          signal: controller.signal,
        })

        if (!resp.ok || !resp.body) {
          throw new Error(`HTTP ${resp.status}`)
        }

        const reader = resp.body.getReader()
        const decoder = new TextDecoder('utf-8')
        let buffer = ''
        let lastEvent = 'message'

        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })

          // SSE 按 \n\n 切帧
          let idx: number
          while ((idx = buffer.indexOf('\n\n')) >= 0) {
            const rawFrame = buffer.slice(0, idx)
            buffer = buffer.slice(idx + 2)
            if (!rawFrame.trim() || rawFrame.startsWith(':')) continue

            const lines = rawFrame.split('\n')
            let eventName = lastEvent
            const dataLines: string[] = []
            for (const line of lines) {
              if (line.startsWith('event:')) eventName = line.slice(6).trim()
              else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
            }
            lastEvent = eventName
            const dataStr = dataLines.join('\n')
            if (!dataStr) continue

            // JSON 解析单独 try · 失败仅跳过本帧(防一帧错断全流)
            // 之前曾把事件处理也包在同一个 try 里 → event:error 的 throw 被
            // catch (parseErr) 吞掉 · 前端不显示后端错误。
            let data: any
            try {
              data = JSON.parse(dataStr)
            } catch {
              continue
            }

            // 事件分发在 try 外:任何 throw(尤其 error 事件)都能被外层 catch 接住
            if (eventName === 'text') {
              const delta = String(data.delta || '')
              if (delta) {
                setIsThinking(false)
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantId ? { ...m, content: m.content + delta } : m,
                  ),
                )
              }
            } else if (eventName === 'meta') {
              const meta: XiaobangMeta = {
                sources: Array.isArray(data.sources) ? data.sources : [],
                link: data.link || undefined,
                confidence: data.confidence || 'medium',
                // [P4 · C1 接线补齐 2026-08-08] 后端一直在 meta 里下发 gap_assistant,
                // 这里原先只挑上面三个键,把它整个丢掉 —— 类型声明有、数据到了浏览器、
                // 然后被扔了,谁也没发现。接上之后才轮得到 XiaobangGapCard 渲染。
                gap_assistant: normalizeGapAssistant(data.gap_assistant),
                assistant_context: data.assistant_context && typeof data.assistant_context === 'object'
                  ? data.assistant_context as XiaobangAssistantContext
                  : undefined,
                // [包 D①] 接管信号必须留住 —— 后端发了、前端扔了,
                // 用户就会看到「点下方反馈」而下方没有任何按钮。
                handoff: data.handoff === true,
                should_escalate: data.should_escalate === true,
                // [包 D①] 出口只认服务端签发的形状;形状不对就当没有,
                // 绝不在前端兜底造一个出口出来(造出来的按钮点下去没有合同)。
                exits: Array.isArray(data.exits)
                  ? (data.exits as unknown[])
                      .filter((e): e is Record<string, unknown> => !!e && typeof e === 'object')
                      .filter((e) => typeof e.exit_id === 'string' && typeof e.label === 'string')
                      .map((e) => ({
                        exit_id: e.exit_id as XiaobangExit['exit_id'],
                        label: e.label as string,
                        submits: Array.isArray(e.submits)
                          ? (e.submits as unknown[]).filter((x): x is string => typeof x === 'string')
                          : undefined,
                      }))
                  : undefined,
              }
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantId ? { ...m, meta } : m)),
              )
            } else if (eventName === 'clarify') {
              // 反问澄清:直接写到当前 assistant 消息上 · 关闭流式
              const clarify: XiaobangClarify = {
                prompt: String(data.prompt || '你问的具体是哪种?'),
                options: Array.isArray(data.options) ? data.options : [],
              }
              setIsThinking(false)
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId ? { ...m, clarify, isStreaming: false } : m,
                ),
              )
            } else if (eventName === 'error') {
              throw new Error(String(data.message || '未知错误'))
            } else if (eventName === 'done') {
              finishStream()
              return
            }
          }
        }
        finishStream()
      } catch (e) {
        if ((e as Error).name === 'AbortError') {
          finishStream('请求超时,请稍后再试')
        } else {
          const msg = (e as Error).message || '网络异常'
          setError(msg)
          finishStream(msg)
        }
      } finally {
        clearTimeout(timeoutId)
        abortRef.current = null
      }
    },
    [isStreaming, sessionId, token, options, persistMessages],
  )

  const cancelStreaming = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return {
    messages,
    isStreaming,
    isThinking,
    error,
    sendMessage,
    cancelStreaming,
  }
}
