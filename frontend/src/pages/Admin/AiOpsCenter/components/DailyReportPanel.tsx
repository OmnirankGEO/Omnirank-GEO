// 每日运营报告:最新日报摘要 + 老板待办 + 生成/查看入口

import { FileText, Loader2, RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import type { AiOpsReport } from '../types'
import { EmptyHint, Panel, timeAgo } from './shared'

export function DailyReportPanel({ latestReport, onGenerate, generating }: {
  latestReport: AiOpsReport | null
  onGenerate: () => void
  generating: boolean
}) {
  const actionItems = latestReport?.action_items_jsonb || []
  return (
    <Panel
      title="每日运营报告"
      action={
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={onGenerate} disabled={generating}>
            {generating ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : <RefreshCw className="mr-1 size-3.5" />}
            生成日报
          </Button>
          <Link to="/admin/ai-ops/reports" className="text-xs text-primary hover:underline">查看全部</Link>
        </div>
      }
    >
      {!latestReport ? (
        <EmptyHint text="还没有日报,点「生成日报」" />
      ) : (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <FileText className="size-3.5" />
            {latestReport.report_date} · {timeAgo(latestReport.updated_at)}
          </div>
          <p className="text-sm text-foreground">{latestReport.summary || '（无摘要）'}</p>
          {actionItems.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-medium text-muted-foreground">今日需老板处理</div>
              <ol className="list-decimal space-y-1 pl-5 text-xs text-foreground">
                {actionItems.map((x, i) => <li key={i}>{x}</li>)}
              </ol>
            </div>
          )}
          <Link to={`/admin/ai-ops/reports/${latestReport.report_date}`}
            className="inline-block text-xs text-primary hover:underline">
            看完整日报 →
          </Link>
        </div>
      )}
    </Panel>
  )
}
