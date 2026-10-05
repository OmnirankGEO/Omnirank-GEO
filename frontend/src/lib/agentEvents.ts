/**
 * Agent Event Bridge — 页面操作 → AI 助手感知
 *
 * 用法：
 *   import { emitAgentEvent } from '@/lib/agentEvents'
 *   emitAgentEvent('diagnosis_completed', { brand_name: '张姐美容院', score: 67 })
 *
 * AI 助手的 旧对话 hook hook 会监听 'agent-event' 事件并自动转发给 AI。
 */

export function emitAgentEvent(action: string, data?: Record<string, unknown>) {
  try {
    window.dispatchEvent(new CustomEvent('agent-event', {
      detail: { action, data: data || {} },
    }))
  } catch {
    // 静默失败（SSR 环境没有 window）
  }
}
