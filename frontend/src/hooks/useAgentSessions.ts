/**
 * useAgentSessions — 管理 Agent 会话列表
 * localStorage 持久化，按 user_id 隔离（多账号共用浏览器不泄漏）
 */

import { useState, useCallback, useEffect } from 'react'

// 从 JWT 解出 user_id，匿名或无 token 返回 'anon'
function getCurrentUserId(): string {
  const token = typeof window !== 'undefined' ? localStorage.getItem('omnirank_token') : null
  if (!token) return 'anon'
  try {
    const payload = JSON.parse(atob(token.split('.')[1]))
    return String(payload.user_id || payload.sub || 'anon')
  } catch {
    return 'anon'
  }
}

function getStorageKey(): string {
  return `omnirank_agent_sessions_u${getCurrentUserId()}`
}
function getMessagesPrefix(): string {
  return `omnirank_agent_msgs_u${getCurrentUserId()}_`
}

export interface SessionMeta {
  id: string
  title: string
  updatedAt: number
  messageCount: number
  hasStarred?: boolean
}

function loadSessions(): SessionMeta[] {
  try {
    const raw = localStorage.getItem(getStorageKey())
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function saveSessions(sessions: SessionMeta[]) {
  localStorage.setItem(getStorageKey(), JSON.stringify(sessions))
}

function generateSessionId() {
  return `s_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
}

/** 从第一条用户消息提取标题，最多 20 字 */
function extractTitle(firstUserMessage: string): string {
  const clean = firstUserMessage.replace(/\n/g, ' ').trim()
  return clean.length > 20 ? clean.slice(0, 20) + '…' : clean
}

export function useAgentSessions() {
  const [sessions, setSessions] = useState<SessionMeta[]>(loadSessions)
  const [activeSessionId, setActiveSessionId] = useState<string>(() => {
    const list = loadSessions()
    return list.length > 0 ? list[0].id : generateSessionId()
  })

  // 用户切换（登出/登录）时重新加载会话列表
  // 监听 localStorage 的 omnirank_token 变化
  useEffect(() => {
    const handler = () => {
      setSessions(loadSessions())
      const list = loadSessions()
      setActiveSessionId(list.length > 0 ? list[0].id : generateSessionId())
    }
    // storage 事件跨 tab 触发；同 tab 内用 custom event
    window.addEventListener('storage', handler)
    window.addEventListener('omnirank-user-changed', handler)
    return () => {
      window.removeEventListener('storage', handler)
      window.removeEventListener('omnirank-user-changed', handler)
    }
  }, [])

  /** 创建新会话并激活 */
  const createSession = useCallback(() => {
    const id = generateSessionId()
    setActiveSessionId(id)
    // 不立即添加到列表 — 等第一条消息发出后再添加
    return id
  }, [])

  /** 切换到已有会话 */
  const selectSession = useCallback((id: string) => {
    setActiveSessionId(id)
  }, [])

  /** 删除会话 */
  const deleteSession = useCallback((id: string) => {
    setSessions(prev => {
      const next = prev.filter(s => s.id !== id)
      saveSessions(next)
      return next
    })
    // 清除消息数据
    localStorage.removeItem(getMessagesPrefix() + id)
    // 如果删的是当前会话，切到最近的或新建
    setActiveSessionId(prev => {
      if (prev !== id) return prev
      const remaining = loadSessions().filter(s => s.id !== id)
      return remaining.length > 0 ? remaining[0].id : generateSessionId()
    })
  }, [])

  /** 更新会话元数据（发消息后调用） */
  const upsertSession = useCallback((id: string, firstMessage: string, messageCount: number, hasStarred?: boolean) => {
    setSessions(prev => {
      const existing = prev.find(s => s.id === id)
      let next: SessionMeta[]
      if (existing) {
        next = prev.map(s =>
          s.id === id ? { ...s, updatedAt: Date.now(), messageCount, hasStarred: hasStarred ?? s.hasStarred } : s
        )
      } else {
        next = [
          { id, title: extractTitle(firstMessage), updatedAt: Date.now(), messageCount, hasStarred },
          ...prev,
        ]
      }
      // 按 updatedAt 降序排列
      next.sort((a, b) => b.updatedAt - a.updatedAt)
      saveSessions(next)
      return next
    })
  }, [])

  return {
    sessions,
    activeSessionId,
    createSession,
    selectSession,
    deleteSession,
    upsertSession,
  }
}

// 工具函数：保存/加载消息到 localStorage（按 user_id 隔离）
export function saveSessionMessages(sessionId: string, messages: unknown[]) {
  try {
    localStorage.setItem(getMessagesPrefix() + sessionId, JSON.stringify(messages))
  } catch {
    // quota exceeded — 静默失败
  }
}

export function loadSessionMessages(sessionId: string): unknown[] {
  try {
    const raw = localStorage.getItem(getMessagesPrefix() + sessionId)
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

// 清理旧格式的全局 key（2026-04-16 前注册的用户可能残留）
// 在 AuthContext logout 时调用
export function clearLegacyAgentStorage() {
  try {
    localStorage.removeItem('omnirank_agent_sessions')
    // 清理旧格式的 omnirank_agent_msgs_s_xxx
    const keysToRemove: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key && key.startsWith('omnirank_agent_msgs_s_')) {
        keysToRemove.push(key)
      }
    }
    keysToRemove.forEach(k => localStorage.removeItem(k))
  } catch {
    // 忽略
  }
}
