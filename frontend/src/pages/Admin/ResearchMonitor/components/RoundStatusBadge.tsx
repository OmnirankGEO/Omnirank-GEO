/**
 * 跑批状态徽章 · 与后端 round 状态机保持一致
 *
 * 状态映射(来源 services/research_monitor 状态机):
 *   - pending      待启动     · gray
 *   - running      运行中     · blue
 *   - completed    完成       · green
 *   - partial_success 部分成功 · amber
 *   - failed       失败       · red
 *   - cancelled    取消       · slate
 *   - failed_resumable 可续跑 · orange
 */
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { taskStatusLabel } from '@/lib/v35Terminology';

export interface RoundStatusBadgeProps {
    status: string;
    className?: string;
    /**
     * P14.3 C2 (老板 round_20260529_014854 反馈):
     * status='completed' 但 Stage 3 有 Jina/OSS 失败时 · 降级为 amber "完成 · 部分抓取失败"
     * 不要静悄悄绿色 "已完成" · 让 admin 一眼看到这轮有抓取损失
     */
    hasStageFailures?: boolean;
}

const STATUS_CONFIG: Record<string, { label: string; className: string }> = {
    pending:   { label: '待启动', className: 'bg-slate-100 text-slate-700 hover:bg-slate-100' },
    running:   { label: '运行中', className: 'bg-blue-100 text-blue-700 hover:bg-blue-100' },
    completed: { label: '已完成', className: 'bg-emerald-100 text-emerald-700 hover:bg-emerald-100' },
    partial_success: { label: '部分成功', className: 'bg-amber-100 text-amber-700 hover:bg-amber-100' },
    failed:    { label: '失败',   className: 'bg-red-100 text-red-700 hover:bg-red-100' },
    cancelled: { label: '已取消', className: 'bg-slate-200 text-slate-600 hover:bg-slate-200' },
    failed_resumable: { label: '可续跑', className: 'bg-orange-100 text-orange-700 hover:bg-orange-100' },
};

// P14.3 C2: completed + Stage 3 有失败时的降级显示 (跟 partial_success 同色但文案区分原因)
const COMPLETED_PARTIAL_CONFIG = {
    label: '完成 · 部分抓取失败',
    className: 'bg-amber-100 text-amber-700 hover:bg-amber-100',
};

export function RoundStatusBadge({ status, className, hasStageFailures }: RoundStatusBadgeProps) {
    // P14.3 C2 · completed + Stage 3 有 Jina/OSS 失败 → amber 降级 · 不要静悄悄绿色
    // r12 兜底:未知 status 走 taskStatusLabel 不直接 raw 渲染
    const fallback = STATUS_CONFIG[status] ?? {
        label: taskStatusLabel(status),
        className: 'bg-gray-100 text-gray-700',
    };
    const cfg = (status === 'completed' && hasStageFailures)
        ? COMPLETED_PARTIAL_CONFIG
        : fallback;
    return (
        <Badge variant="secondary" className={cn(cfg.className, className)}>
            {cfg.label}
        </Badge>
    );
}

export default RoundStatusBadge;
