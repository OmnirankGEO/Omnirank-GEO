import { CheckCircle2, Circle, Clock3 } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { RefundOrderPreview, RefundWorkOrder } from './types';

const eventLabel: Record<string, string> = {
  created: '创建工单',
  submitted: '提交审核',
  approved: '审核通过',
  executed: '系统冲账',
  payout_proof: '上传打款凭证',
  completed: '标记完成',
  rejected: '驳回工单',
  attachment_uploaded: '上传凭证',
};

export function RefundTimeline({
  workOrder,
  preview,
}: {
  workOrder?: RefundWorkOrder | null;
  preview?: RefundOrderPreview | null;
}) {
  const eventItems = workOrder?.events?.length
    ? workOrder.events.map((event) => ({
      key: `${event.id}`,
      label: eventLabel[event.event_type] || event.event_type,
      note: event.note,
      time: event.created_at,
      status: 'done',
    }))
    : (preview?.timeline || [
      { key: 'created', label: '创建工单', status: 'pending' },
      { key: 'approved', label: '审核通过', status: 'pending' },
      { key: 'executed', label: '系统冲账', status: 'pending' },
      { key: 'payout_proof', label: '上传打款凭证', status: 'pending' },
      { key: 'completed', label: '标记完成', status: 'pending' },
    ]).map((item) => ({ ...item, note: '', time: '' }));

  return (
    <section className="rounded-lg border border-white/10 bg-card/70 p-4">
      <h2 className="mb-4 text-base font-semibold text-foreground">工单时间线</h2>
      <div className="space-y-0">
        {eventItems.map((item, index) => {
          const done = item.status === 'done';
          const Icon = done ? CheckCircle2 : index === 0 ? Clock3 : Circle;
          return (
            <div key={item.key} className="relative flex gap-3 pb-5 last:pb-0">
              {index < eventItems.length - 1 && (
                <div className="absolute left-[7px] top-5 h-full w-px bg-border" />
              )}
              <Icon className={cn('relative z-10 mt-0.5 h-4 w-4 bg-card', done ? 'text-emerald-400' : 'text-muted-foreground')} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-3">
                  <p className={cn('text-sm font-medium', done ? 'text-foreground' : 'text-muted-foreground')}>
                    {item.label}
                  </p>
                  {item.time && <span className="text-xs text-muted-foreground">{String(item.time).slice(0, 16).replace('T', ' ')}</span>}
                </div>
                {item.note && <p className="mt-1 text-xs text-muted-foreground">{item.note}</p>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
