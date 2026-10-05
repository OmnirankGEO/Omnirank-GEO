// 日报查看:按日期展示日报 Markdown + metrics + action items

import { useEffect, useState } from 'react'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { getReport } from '../api'
import type { AiOpsReport } from '../types'
import { MarkdownBlock } from './shared'

export function ReportViewer({ reportDate }: { reportDate: string }) {
  const [report, setReport] = useState<AiOpsReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(null)
    getReport(reportDate)
      .then((r) => { if (alive) setReport(r) })
      .catch((e) => { if (alive) setError(String((e as Error).message)) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [reportDate])

  return (
    <div className="space-y-4">
      <Link to="/admin/ai-ops/reports" className="inline-flex items-center gap-1 text-xs text-primary hover:underline">
        <ArrowLeft className="size-3.5" />返回日报列表
      </Link>
      {loading ? (
        <div className="flex min-h-40 items-center justify-center text-muted-foreground">
          <Loader2 className="mr-2 size-4 animate-spin" />加载中
        </div>
      ) : error ? (
        <div className="rounded-lg border border-dashed bg-muted/20 p-6 text-center text-sm text-destructive">{error}</div>
      ) : report ? (
        <div className="rounded-lg border bg-card p-5">
          {report.markdown ? (
            <MarkdownBlock text={report.markdown} />
          ) : (
            <p className="text-sm text-muted-foreground">（空）</p>
          )}
        </div>
      ) : null}
    </div>
  )
}
