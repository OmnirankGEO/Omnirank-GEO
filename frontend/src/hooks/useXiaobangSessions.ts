/**
 * useXiaobangSessions — 小榜会话列表管理
 *
 * 命名空间跟旧 useAgentSessions 独立(`omnirank_xiaobang_*`),
 * 避免和代理端 /agent 全屏聊天 / C 端聊天混在一起。
 * localStorage 持久化,按**已鉴权** user id 隔离(多账号共用浏览器不泄漏)。
 *
 * 🔴 H0(WORKORDER_XIAOBANG_SOLUTION_FIRST_AI_2026-08-20 §4 P0):
 *    这里原先自己解 JWT 取 user id:
 *      JSON.parse(atob(token.split('.')[1]))
 *    后端签的是**无 padding 的 base64url**(`auth/jwt_utils.py:56` urlsafe_b64encode
 *    + rstrip('='),且 `ensure_ascii=False` —— 中文显示名/角色名直接进 payload)。
 *    而 Web `atob()` 只吃标准 base64,遇到 `-` / `_` 抛 InvalidCharacterError
 *    → catch → 返回 `'anon'`。
 *
 *    🔴 触发是**内容相关的,不是必然**:本地实测 20 个真实风格中文显示名 × 400 组
 *    随机 uid/iat = 8000 样本,**37.7%** 的 payload 含 `-`/`_`,且按名字极度两极
 *    (「赵敏运营 / 杭州西子 / 广州鲸鸣」100%,「李明 / 张伟 / 陈静」0%);
 *    `len % 4 == 1` 那条路 0/8000,不是成因。
 *    后果:落在那 37.7% 里的账号全部共用同一个 `..._uanon` 池 —— 两个这样的账号
 *    在同一浏览器上先后登录,后者看得见前者的会话标题与消息正文,属 H0 权限/隐私边界。
 *
 *    现在的合同:命名空间**只**来自 `AuthContext.user.id`(已鉴权真值),
 *    拿不到已鉴权用户时**一律不读不写**(返回空、写入 no-op),没有 `anon` 兜底池。
 */
import { useState, useCallback, useEffect, useMemo } from 'react'
import { useAuth } from '@/context/AuthContext'

export const UNBOUND_XIAOBANG_CONTEXT = 'unscoped'

/**
 * 模块级命名空间。`null` = 尚无已鉴权用户(未登录 / /api/auth/me 还没回 / 已登出)。
 *
 * 为什么是模块级而不是纯 hook 状态:`loadSessionMessages` / `saveSessionMessages`
 * 是**普通导出函数**(useXiaobangChat 在 useState 初始化器里同步调用),
 * 不能变成 hook。由 `useXiaobangStorageNamespace()` 在**渲染期同步**发布,
 * 两个消费 hook 都调它 —— 谁先跑都行,不依赖 hook 之间的调用顺序。
 */
let currentNamespace: string | null = null

function namespaceForUserId(userId: unknown): string | null {
  const id = typeof userId === 'number' ? userId : Number(userId)
  return Number.isInteger(id) && id > 0 ? `u${id}` : null
}

/** 测试/接线用:直接发布命名空间。生产路径只由 `useXiaobangStorageNamespace` 调。 */
export function publishXiaobangStorageNamespace(namespace: string | null): string | null {
  currentNamespace = namespace
  return currentNamespace
}

export function getXiaobangStorageNamespace(): string | null {
  return currentNamespace
}

/**
 * 从已鉴权用户派生命名空间并同步发布。
 *
 * 🔴 必须在本文件/useXiaobangChat 里**任何 load / save 之前调用** ——
 *    useState 初始化器在同一次函数体里紧随其后执行。
 */
export function useXiaobangStorageNamespace(): string | null {
  const { user, isAuthenticated } = useAuth()
  const namespace = isAuthenticated && user ? namespaceForUserId(user.id) : null
  publishXiaobangStorageNamespace(namespace)
  return namespace
}

/**
 * 未认证时返回 null —— 调用方据此**完全跳过** localStorage,不落任何公共池。
 *
 * 🔴🔴 绊线(同上):这条 `null` 分支目前也是**纵深防御·非承重** —— 变异 M4
 *    (为空时回退 `uanon` 键)拆掉它,5 条判据全绿,因为认证未完成时抽屉根本不挂载
 *    (判据 4「前提检查·非锁」已把这个前提证成)。
 *    ⇒ **若抽屉哪天能在认证完成前挂载,这条立刻升为承重:先补锁,再改挂载行为。**
 */
function getSessionsKey(): string | null {
  return currentNamespace ? `omnirank_xiaobang_sessions_${currentNamespace}` : null
}
function getMessagesPrefix(): string | null {
  return currentNamespace ? `omnirank_xiaobang_msgs_${currentNamespace}_` : null
}

export interface XiaobangSessionMeta {
  id: string
  title: string
  updatedAt: number
  messageCount: number
  /** 不存客户名，只存本账号内稳定上下文键；旧会话统一留在 unscoped。 */
  contextKey: string
}

function normalizeContextKey(value?: string): string {
  const clean = String(value || '').trim()
  return clean || UNBOUND_XIAOBANG_CONTEXT
}

export function xiaobangSessionContextForBrand(brandId?: number | null): string {
  return brandId && brandId > 0 ? `brand:${brandId}` : UNBOUND_XIAOBANG_CONTEXT
}

export function sessionsForXiaobangContext(
  sessions: XiaobangSessionMeta[],
  contextKey: string,
): XiaobangSessionMeta[] {
  const expected = normalizeContextKey(contextKey)
  return sessions.filter((session) => normalizeContextKey(session.contextKey) === expected)
}

function loadSessions(): XiaobangSessionMeta[] {
  const key = getSessionsKey()
  if (!key) return []
  try {
    const raw = localStorage.getItem(key)
    const parsed = raw ? JSON.parse(raw) : []
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((item) => item && typeof item.id === 'string')
      .map((item) => ({
        id: item.id,
        title: String(item.title || '历史对话'),
        updatedAt: Number(item.updatedAt || 0),
        messageCount: Number(item.messageCount || 0),
        // 无法证明客户归属的历史记录绝不自动绑定到当前客户。
        contextKey: normalizeContextKey(item.contextKey),
      }))
  } catch {
    return []
  }
}

function saveSessions(sessions: XiaobangSessionMeta[]) {
  const key = getSessionsKey()
  if (!key) return
  localStorage.setItem(key, JSON.stringify(sessions))
}

function generateSessionId() {
  return `xb_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
}

function extractTitle(firstUserMessage: string): string {
  const clean = firstUserMessage.replace(/\n/g, ' ').trim()
  return clean.length > 20 ? clean.slice(0, 20) + '…' : clean
}

/** 读 / 写一段会话的消息列表(localStorage 缓存) */
export function loadSessionMessages<T = unknown>(sessionId: string): T[] {
  const prefix = getMessagesPrefix()
  if (!prefix) return []
  try {
    const raw = localStorage.getItem(prefix + sessionId)
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

export function saveSessionMessages<T = unknown>(sessionId: string, messages: T[]) {
  const prefix = getMessagesPrefix()
  if (!prefix) return
  try {
    localStorage.setItem(prefix + sessionId, JSON.stringify(messages))
  } catch {
    // localStorage 满了 · 静默失败,不影响当前会话
  }
}

export function useXiaobangSessions(contextKey = UNBOUND_XIAOBANG_CONTEXT) {
  // 🔴 必须在下面两个 useState 初始化器之前 —— 它们同步读 localStorage。
  const namespace = useXiaobangStorageNamespace()
  const scopedContextKey = normalizeContextKey(contextKey)
  const [allSessions, setAllSessions] = useState<XiaobangSessionMeta[]>(loadSessions)
  const [activeSessionId, setActiveSessionId] = useState<string>(() => {
    const list = sessionsForXiaobangContext(loadSessions(), scopedContextKey)
    return list.length > 0 ? list[0].id : generateSessionId()
  })

  // 🔴🔴 绊线(Review 2026-08-21 裁定 · 给下一个改挂载行为的人):
  //    下面这段【渲染期原子换池】目前是**纵深防御·非承重** —— 变异 M3 拆掉它,
  //    5 条隔离判据全绿。原因是两条真实换号路径(page.reload / 跨标签页 storage 事件)
  //    **都以抽屉被卸载重挂收场**(AuthContext.reconcileExternalSession 会
  //    setUser(null)+setIsLoading(true) → ProtectedRoute 进 loading → Layout 连抽屉一起卸)。
  //    ⇒ **若换号不再以抽屉重挂收场,这一段立刻升为承重:先补锁,再改挂载行为。**
  //    顺序反了 = 上一个账号的会话列表会在下一个账号屏上留一帧,而没有任何判据会红。
  // 🔴 账号切换必须**原子**:在渲染期换池,而不是等 useEffect ——
  //    effect 要等本次渲染提交后才跑,那一帧里 B 的界面上挂的还是 A 的会话列表。
  //    React 允许「渲染期给自己 setState」:它会丢弃本次输出、立刻用新 state 重渲,
  //    所以 A 的池一帧都不会被 B 看到。
  const [namespaceSnapshot, setNamespaceSnapshot] = useState<string | null>(namespace)
  if (namespaceSnapshot !== namespace) {
    setNamespaceSnapshot(namespace)
    const nextAll = loadSessions()
    const scoped = sessionsForXiaobangContext(nextAll, scopedContextKey)
    setAllSessions(nextAll)
    setActiveSessionId(scoped[0]?.id || generateSessionId())
  }

  const sessions = useMemo(
    () => sessionsForXiaobangContext(allSessions, scopedContextKey),
    [allSessions, scopedContextKey],
  )

  useEffect(() => {
    const handler = () => {
      const list = loadSessions()
      setAllSessions(list)
    }
    window.addEventListener('storage', handler)
    window.addEventListener('omnirank-user-changed', handler)
    return () => {
      window.removeEventListener('storage', handler)
      window.removeEventListener('omnirank-user-changed', handler)
    }
  }, [])

  // 客户上下文变化时，只能激活同一 contextKey 的会话；没有就开一个空会话。
  useEffect(() => {
    setActiveSessionId((current) => {
      const currentMeta = allSessions.find((session) => session.id === current)
      if (currentMeta && normalizeContextKey(currentMeta.contextKey) === scopedContextKey) {
        return current
      }
      const scoped = sessionsForXiaobangContext(allSessions, scopedContextKey)
      return scoped[0]?.id || generateSessionId()
    })
  }, [allSessions, scopedContextKey])

  /** 新建会话并激活(不立即写列表 · 等第一条消息再 upsert) */
  const createSession = useCallback(() => {
    const id = generateSessionId()
    setActiveSessionId(id)
    return id
  }, [])

  const selectSession = useCallback((id: string) => {
    const candidate = allSessions.find((session) => session.id === id)
    if (!candidate || normalizeContextKey(candidate.contextKey) !== scopedContextKey) return false
    setActiveSessionId(id)
    return true
  }, [allSessions, scopedContextKey])

  const deleteSession = useCallback((id: string) => {
    setAllSessions((prev) => {
      const next = prev.filter((s) => s.id !== id)
      saveSessions(next)
      return next
    })
    const prefix = getMessagesPrefix()
    if (prefix) {
      try {
        localStorage.removeItem(prefix + id)
      } catch {
        // ignore
      }
    }
    setActiveSessionId((curr) => {
      if (curr !== id) return curr
      const remaining = sessionsForXiaobangContext(
        loadSessions().filter((s) => s.id !== id),
        scopedContextKey,
      )
      return remaining.length > 0 ? remaining[0].id : generateSessionId()
    })
  }, [scopedContextKey])

  /** 首条消息发完后调用 · 把会话加入列表 / 更新标题 + 计数 */
  const upsertSession = useCallback(
    (sessionId: string, firstUserMessage: string, messageCount: number) => {
      // 未认证时不落任何历史(否则会写进上一个账号的池或公共池)。
      if (!getSessionsKey()) return
      setAllSessions((prev) => {
        const idx = prev.findIndex((s) => s.id === sessionId)
        // 会话 ID 若已属于另一客户，拒绝把它“改绑”到当前客户。
        if (idx >= 0 && normalizeContextKey(prev[idx].contextKey) !== scopedContextKey) {
          return prev
        }
        const meta: XiaobangSessionMeta = {
          id: sessionId,
          title: idx >= 0 ? prev[idx].title : extractTitle(firstUserMessage),
          updatedAt: Date.now(),
          messageCount,
          contextKey: scopedContextKey,
        }
        const next =
          idx >= 0
            ? [meta, ...prev.filter((s) => s.id !== sessionId)]
            : [meta, ...prev]
        saveSessions(next)
        return next
      })
    },
    [scopedContextKey],
  )

  return {
    sessions,
    activeSessionId,
    createSession,
    selectSession,
    deleteSession,
    upsertSession,
  }
}
