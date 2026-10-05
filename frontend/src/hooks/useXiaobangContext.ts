import { useEffect, useMemo, useState } from 'react'
import { authFetch } from '@/lib/api'
import type { XiaobangContextRefs } from './useXiaobangChat'

export interface XiaobangServerContext {
  brand_id: number | null
  brand_name: string | null
  quote_id: number | null
  current_page: string
  page_name: string
  data_updated_at: string | null
  has_customer: boolean
}

export interface CustomerOperationPlanSummary {
  plan_id?: string
  plan_version?: string
  context: XiaobangServerContext
  metrics?: {
    writing?: { pending?: number; in_progress?: number; completed?: number }
    publication?: { published?: number; indexed?: number; url_cited?: number }
  }
  primary_action?: { operation_id?: string; reason?: string }
  backup_action?: { operation_id?: string; reason?: string }
}

interface ContextResponse {
  success: boolean
  context: XiaobangServerContext
  operation_plan: CustomerOperationPlanSummary | null
  warning?: string
  exit?: { label: string; operation_id: string }
}

export function useXiaobangContext(currentPage: string, contextRefs?: XiaobangContextRefs) {
  const [data, setData] = useState<ContextResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const refsKey = useMemo(() => JSON.stringify(contextRefs || {}), [contextRefs])

  useEffect(() => {
    const controller = new AbortController()
    const stableRefs = JSON.parse(refsKey) as XiaobangContextRefs
    setData(null)
    setError(null)
    setLoading(true)
    void authFetch('/api/xiaobang/context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        current_page: currentPage,
        context_refs: Object.keys(stableRefs).length ? stableRefs : undefined,
      }),
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json() as Promise<ContextResponse>
      })
      .then((payload) => {
        if (!controller.signal.aborted) setData(payload)
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setError(reason instanceof Error ? reason.message : '上下文暂时不可用')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [currentPage, refsKey])

  return { data, loading, error }
}
