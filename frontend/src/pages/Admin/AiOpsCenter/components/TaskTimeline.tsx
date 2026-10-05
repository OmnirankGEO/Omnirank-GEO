// 任务事件流时间线

import type { AiOpsEvent } from '../types'
import { EmptyHint, SeverityDot, timeAgo } from './shared'

export function TaskTimeline({ events }: { events: AiOpsEvent[] }) {
  if (events.length === 0) return <EmptyHint text="还没有事件" />
  return (
    <ol className="space-y-2">
      {events.map((e) => (
        <li key={e.id} className="flex gap-2">
          <SeverityDot severity={e.severity} />
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-xs font-medium text-foreground">{e.event_type}</span>
              <span className="shrink-0 text-[11px] text-muted-foreground">{timeAgo(e.created_at)}</span>
            </div>
            {e.message && (
              <p className="whitespace-pre-wrap break-words text-xs leading-5 text-muted-foreground">{e.message}</p>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}
