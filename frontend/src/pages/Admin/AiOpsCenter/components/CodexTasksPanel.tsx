// Codex 修复任务:running / succeeded / failed 列表

import { Loader2 } from 'lucide-react'
import type { AiOpsTask } from '../types'
import { KIND_LABEL } from '../types'
import { EmptyHint, Panel, RiskBadge, StatusBadge, timeAgo } from './shared'

export function CodexTasksPanel({ tasks, onOpen }: {
  tasks: AiOpsTask[]
  onOpen: (taskId: number) => void
}) {
  const codex = tasks.filter((t) => t.kind === 'diagnose' || t.kind === 'fix' || t.kind === 'code_review')
  return (
    <Panel title="Codex 诊断 / 修复任务">
      {codex.length === 0 ? (
        <EmptyHint text="暂无 Codex 任务" />
      ) : (
        <ul className="space-y-2">
          {codex.slice(0, 8).map((t) => (
            <li key={t.id}>
              <button
                type="button"
                onClick={() => onOpen(t.id)}
                className="flex w-full items-center gap-2 rounded-md border bg-background px-3 py-2 text-left hover:bg-muted/50"
              >
                {t.status === 'running' && <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" />}
                <span className="min-w-0 flex-1 truncate text-sm">{t.title || `${KIND_LABEL[t.kind]} #${t.id}`}</span>
                <RiskBadge risk={t.risk_level} />
                <StatusBadge status={t.status} />
                <span className="shrink-0 text-[11px] text-muted-foreground">{timeAgo(t.created_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}
