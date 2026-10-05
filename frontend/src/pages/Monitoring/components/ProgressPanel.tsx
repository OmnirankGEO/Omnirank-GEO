/**
 * Real-time monitoring progress panel with SSE log display.
 */
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import SafeMarkdown from '@/components/SafeMarkdown';
import { RefreshCw, X } from 'lucide-react';
import type { MonitoringCell, MonitoringCellState, ProgressLog } from '../types';

interface ProgressPanelProps {
    loading: boolean;
    progressLogs: ProgressLog[];
    progressTotal: number;
    progressCompleted: number;
    cells: MonitoringCell[];
    retryingCellId?: number | null;
    onClose: () => void;
    onToggleExpand: (idx: number) => void;
    onRetryCell: (cell: MonitoringCell) => void;
}

const platformLabels: Record<MonitoringCell['platform'], string> = {
    dashscope: '千问', deepseek: 'DeepSeek', kimi: 'Kimi', doubao: '豆包',
};

const cellStatePresentation: Record<MonitoringCellState, { label: string; className: string }> = {
    queued: { label: '排队', className: 'border-slate-600 bg-slate-800 text-slate-300' },
    running: { label: '执行', className: 'border-cyan-500/50 bg-cyan-500/10 text-cyan-300' },
    succeeded: { label: '成功', className: 'border-green-500/50 bg-green-500/10 text-green-300' },
    failed: { label: '失败', className: 'border-amber-500/50 bg-amber-500/10 text-amber-300' },
    pending_identity: { label: '待身份确认', className: 'border-violet-500/50 bg-violet-500/10 text-violet-300' },
    unavailable: { label: '不可用', className: 'border-slate-700 bg-slate-900 text-slate-500' },
    pending_provider_confirmation: { label: '待人工核对', className: 'border-orange-500/40 bg-orange-500/10 text-orange-300' },
};

function monitoringErrorMessage(log: ProgressLog): string {
    switch (log.errorCode) {
        case 'brand_identity_retry_exhausted':
            return '系统已对同一条 AI 回答重新判定，仍无法确认其中的称呼是否属于当前品牌。本次结果不计入检出率，后续监测会继续使用已确认的品牌常用名。';
        case 'brand_identity_unresolved':
            // [工单 M-1 ④ 2026-07-28] 旧文案让用户以为整轮作废。事实是:仅此 1 条待
            // 人工确认,同轮其他结果都已正常计入;确认后本条立即重判并计入,零额外消耗。
            return '仅这 1 条待人工确认，同轮其他结果均已正常计入、不受影响。到上方「需要确认的品牌名称」卡完成确认后，本条会用确认结果对原回答立即重新判定并计入——不重复调用引擎、不产生额外消耗。';
        case 'brand_identity_data_unavailable':
            return '品牌资料暂时无法读取，本次结果不计入检出率。请稍后重试；如持续出现，请检查品牌资料中的“AI 搜索常用名”。';
        case 'platform_not_supported':
            return '当前平台暂未接入监测，本次结果不计入检出率。';
        case 'network_interrupted':
            return '监测连接已中断。本次未完成的结果不会计入检出率，请重新发起监测。';
        default:
            return '该平台本次未返回可用结果，可能是限流、超时或服务异常。本次结果不计入检出率，请稍后重试。';
    }
}

export function ProgressPanel({
    loading,
    progressLogs,
    progressTotal,
    progressCompleted,
    cells,
    retryingCellId,
    onClose,
    onToggleExpand,
    onRetryCell,
}: ProgressPanelProps) {
    const groupedCells = cells.reduce<Array<{ key: string; keyword: string; cells: MonitoringCell[] }>>((groups, cell) => {
        const keyword = cell.keyword_snapshot || cell.keyword || `关键词 ${cell.keyword_id}`;
        const key = `${cell.keyword_source}:${cell.keyword_id}`;
        const existing = groups.find(group => group.key === key);
        if (existing) existing.cells.push(cell);
        else groups.push({ key, keyword, cells: [cell] });
        return groups;
    }, []);

    return (
        <Card className="border-2 border-brand/30 bg-slate-900 text-white">
            <CardHeader className="pb-2">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <CardTitle className="flex shrink-0 items-center gap-2 whitespace-nowrap text-lg text-brand">
                        <RefreshCw className={`h-5 w-5 ${loading ? 'animate-spin' : ''}`} />
                        监测进度
                    </CardTitle>
                    <div className="flex w-full items-center gap-2 sm:w-auto sm:gap-4">
                        <span className="shrink-0 text-sm text-slate-300">
                            {progressCompleted}/{progressTotal}
                        </span>
                        <div className="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-slate-700 sm:w-48 sm:flex-none">
                            <div
                                className="h-full bg-brand transition-all duration-300"
                                style={{ width: progressTotal > 0 ? `${(progressCompleted / progressTotal) * 100}%` : '0%' }}
                            />
                        </div>
                        {!loading && (
                            <Button
                                size="sm"
                                variant="ghost"
                                className="h-8 w-8 shrink-0 p-0 text-slate-300 hover:text-white"
                                onClick={onClose}
                                aria-label="关闭监测进度"
                                title="关闭"
                            >
                                <X className="h-4 w-4" />
                            </Button>
                        )}
                    </div>
                </div>
            </CardHeader>
            <CardContent className="p-3 sm:p-5 pt-0">
                {groupedCells.length > 0 && (
                    <div className="mb-3 max-h-80 space-y-3 overflow-y-auto rounded bg-slate-950 p-3" data-testid="monitoring-planned-matrix">
                        {groupedCells.map(group => (
                            <div key={group.key} className="min-w-0 rounded border border-slate-800 p-2">
                                <div className="mb-2 break-words text-sm text-white">{group.keyword}</div>
                                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                                    {(['dashscope', 'deepseek', 'kimi', 'doubao'] as const).map(platform => {
                                        const cell = group.cells.find(item => item.platform === platform);
                                        const state = cell?.state || 'unavailable';
                                        const presentation = cellStatePresentation[state];
                                        const canRetry = Boolean(
                                            cell
                                             && state === 'failed'
                                             && ['covered', 'admin_covered'].includes(cell.fulfillment_state || '')
                                             && cell.retry_coverage === 'included'
                                             && Number(cell.retry_count || 0) < Number(cell.retry_max_attempts || 0)
                                         );
                                        return (
                                            <div
                                                key={platform}
                                                className={`min-w-0 rounded border px-2 py-2 ${presentation.className}`}
                                                title={cell?.error_message || (state === 'pending_provider_confirmation' ? '供应商结果待核对，禁止自动再次请求' : '')}
                                            >
                                                <div className="truncate text-xs text-slate-400">{platformLabels[platform]}</div>
                                                <div className="mt-1 text-xs font-medium">{presentation.label}</div>
                                                {state === 'running' && <div className="mt-1 h-1 w-full animate-pulse rounded bg-cyan-400/50" />}
                                                {canRetry && cell && (
                                                    <button
                                                        type="button"
                                                        className="mt-1 min-h-8 w-full rounded border border-amber-400/40 px-1 text-xs hover:bg-amber-400/10 disabled:opacity-50"
                                                        disabled={retryingCellId === cell.id}
                                                        onClick={() => onRetryCell(cell)}
                                                        aria-label={`重试 ${group.keyword} ${platformLabels[platform]}`}
                                                    >
                                                        {retryingCellId === cell.id ? '重试中' : '重试此格'}
                                                    </button>
                                                )}
                                            </div>
                                        );
                                    })}
                                </div>
                            </div>
                        ))}
                    </div>
                )}
                {(() => {
                    // [工单 M-1 ④] 待确认摘要:让用户一眼看到"仅 N 条待确认,其余 M 条已计入"
                    const pendingIdentity = progressLogs.filter(log => log.errorCode === 'brand_identity_unresolved').length;
                    const counted = progressLogs.filter(log => log.status === 'success').length;
                    if (pendingIdentity === 0) return null;
                    return (
                        <div
                            data-testid="identity-pending-summary"
                            className="mb-2 rounded border border-violet-500/40 bg-violet-500/10 px-3 py-2 text-xs leading-5 text-violet-200"
                            role="status"
                        >
                            仅 {pendingIdentity} 条待确认品牌名称，其余 {counted} 条已正常计入。
                            到上方「需要确认的品牌名称」卡完成确认后，该条会立即重新判定并计入。
                        </div>
                    );
                })()}
                <div className="font-mono text-sm h-64 overflow-y-auto bg-slate-950 rounded p-3 space-y-1">
                    {progressLogs.length === 0 ? (
                        <div className="text-slate-400 animate-pulse">
                            {loading ? '正在连接监测服务...' : '已连接 · 等待引擎返回结果(单引擎可能 7-25 秒)...'}
                        </div>
                    ) : (
                        progressLogs.map((log, idx) => (
                            <div key={idx} className="mb-2">
                                <button
                                    type="button"
                                    className="flex w-full min-w-0 flex-col gap-1 rounded px-1 py-1 text-left hover:bg-slate-800 sm:grid sm:grid-cols-[auto_auto_minmax(0,1fr)_auto_auto] sm:items-start sm:gap-x-2 sm:gap-y-0"
                                    onClick={() => onToggleExpand(idx)}
                                    aria-expanded={Boolean(log.expanded)}
                                >
                                    <span className="flex w-full items-center gap-2 sm:contents">
                                        <span className="shrink-0 text-slate-500">{log.time}</span>
                                        <span className="shrink-0 text-cyan-400">[{log.platform}]</span>
                                        <span className="ml-auto shrink-0 text-xs text-slate-500 sm:order-5 sm:ml-0">
                                            {log.expanded ? '收起' : '查看详情'}
                                        </span>
                                    </span>
                                    <span className="min-w-0 break-words text-white sm:order-3">{log.keyword}</span>
                                    {log.status === 'error' ? (
                                        <span className="shrink-0 text-amber-300 sm:order-4">
                                            {log.errorCode === 'brand_identity_unresolved' ? '仅此条待确认 · 暂未计入' : '本次未计入'}
                                        </span>
                                    ) : (
                                        <span className={`shrink-0 sm:order-4 ${log.detected ? 'text-green-400' : 'text-red-400'}`}>
                                            {/* [M-1 ①] 人工确认后的行如实回写"已确认 · 已计入" */}
                                            {log.identityConfirmed
                                                ? (log.detected ? '已确认 · 已计入(检出)' : '已确认 · 已计入(未提及)')
                                                : (log.detected ? '已检出' : '未检出')}
                                        </span>
                                    )}
                                </button>
                                {log.expanded && (
                                    <div className="mt-1 max-h-48 overflow-y-auto break-words rounded bg-slate-800/50 p-2 text-xs leading-5 text-slate-300 sm:ml-4">
                                        {log.status === 'error' ? (
                                            <p className="whitespace-pre-wrap">{monitoringErrorMessage(log)}</p>
                                        ) : log.fullResponse ? (
                                            <SafeMarkdown>{log.fullResponse}</SafeMarkdown>
                                        ) : (
                                            <p>(AI回复内容为空，可能是API返回格式变更)</p>
                                        )}
                                    </div>
                                )}
                            </div>
                        ))
                    )}
                    {loading && progressLogs.length > 0 && (
                        <div className="text-yellow-400 animate-pulse">▌ 检测中...</div>
                    )}
                </div>
            </CardContent>
        </Card>
    );
}
