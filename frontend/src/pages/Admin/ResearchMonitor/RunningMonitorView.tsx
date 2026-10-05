/**
 * GEO 调研监测 · 运行监控视图 [P15.1 · 2026-05-28]
 *
 * 给跑批管理 → 运行监控 二级 tab 用 · 显示当前 active round 的:
 *   - round 元信息 (行业 / 状态 / 心跳健康)
 *   - 总进度 (1/7 ~ 8/8)
 *   - 4 平台成功/失败/引用数 + 最新错误
 *   - 阶段进度条
 *   - 失败错误聚合
 *
 * 轮询频率: 3s (有 active round 时)
 *
 * 不实现 (P15.2/P15.3 backlog):
 *   - 失败明细 per-prompt 展开
 *   - 事件日志
 *   - 单平台重跑
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Loader2, RefreshCw, AlertTriangle, Activity, XCircle, CheckCircle2, Clock,
    ChevronDown, ChevronRight,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { RoundStatusBadge } from './components/RoundStatusBadge';
import { stage3HasJinaFailures } from './components/Stage3SummaryView';
import {
    researchMonitorApi, type ResearchRound, type LiveStatus, type LiveStatusPlatform,
} from '@/lib/researchMonitorApi';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { formatApiErrorForDisplay } from '@/lib/api';

const PLATFORM_COLOR: Record<string, string> = {
    豆包: 'bg-blue-100 text-blue-800 border-blue-200',
    DeepSeek: 'bg-green-100 text-green-800 border-green-200',
    Qwen: 'bg-purple-100 text-purple-800 border-purple-200',
    Kimi: 'bg-orange-100 text-orange-800 border-orange-200',
};
const STATUS_COLOR: Record<string, string> = {
    success: 'bg-green-50 border-green-300',
    failed: 'bg-red-50 border-red-300',
    partial: 'bg-amber-50 border-amber-300',
    running: 'bg-blue-50 border-blue-300',
    unknown: 'bg-gray-50 border-gray-300',
};
const STATUS_LABEL: Record<string, string> = {
    success: '全部成功',
    failed: '全部失败',
    partial: '部分失败',
    running: '运行中',
    unknown: '等待中',
};

function fmtDuration(seconds: number | null): string {
    if (seconds === null) return '-';
    if (seconds < 60) return `${seconds}秒`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return `${m}分${s ? ` ${s}秒` : ''}`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
}

function heartbeatHealth(ageSec: number | null): { color: string; label: string } {
    if (ageSec === null) return { color: 'text-muted-foreground', label: '未知' };
    if (ageSec < 120) return { color: 'text-green-700', label: `${ageSec}秒前` };
    if (ageSec < 300) return { color: 'text-blue-700', label: `${fmtDuration(ageSec)} · 处理中` };
    if (ageSec < 600) return { color: 'text-amber-700', label: `${fmtDuration(ageSec)} · 疑似卡住` };
    return { color: 'text-red-700', label: `${fmtDuration(ageSec)} · 可能卡死` };
}

interface Props {
    activeRound: ResearchRound | null;     // 当前要监控的 round (父组件传入)
    onCancel: (round_id: string) => Promise<void>;
}

export default function RunningMonitorView({ activeRound, onCancel }: Props) {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const [data, setData] = useState<LiveStatus | null>(null);
    const [loading, setLoading] = useState(false);
    const [cancelLoading, setCancelLoading] = useState(false);
    const [errorsOpen, setErrorsOpen] = useState(true);

    const fetchLive = useCallback(async () => {
        if (!activeRound) { setData(null); return; }
        setLoading(true);
        try {
            const res = await researchMonitorApi.getRoundLiveStatus(activeRound.round_id);
            setData(res);
        } catch (e) {
            // 静默 · 不弹 toast 避免轮询时刷屏
            console.warn('live-status 拉取失败', e);
        } finally {
            setLoading(false);
        }
    }, [activeRound]);

    // 初次拉 + 3s 轮询 (有 active round 时)
    useEffect(() => { void fetchLive(); }, [fetchLive]);
    useEffect(() => {
        if (!activeRound) return;
        const isActive = activeRound.status === 'running' || activeRound.status === 'pending';
        const interval = isActive ? 3000 : 15000;
        const h = setInterval(() => { void fetchLive(); }, interval);
        return () => clearInterval(h);
    }, [activeRound, fetchLive]);

    const handleCancel = async () => {
        if (!activeRound) return;
        if (!(await askConfirm({ title: `确认取消 round ${activeRound.round_id}?`, danger: true }))) return;
        setCancelLoading(true);
        try {
            await onCancel(activeRound.round_id);
            toast.success('已取消');
            await fetchLive();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '取消失败', 'admin'));
        } finally {
            setCancelLoading(false);
        }
    };

    if (!activeRound) {
        return (
            <Card>
                <CardContent className="py-16 text-center text-sm text-muted-foreground">
                    <Activity className="w-10 h-10 mx-auto mb-3 opacity-30" />
                    当前无运行中的跑批 · 切到 <span className="font-medium">历史跑批</span> tab 看过去的记录
                </CardContent>
            </Card>
        );
    }
    if (!data) {
        return (
            <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 animate-spin" /></div>
        );
    }

    const { round, overall, platforms, stage_progress, errors, articles_done, stage_3_summary, intent_summary } = data;
    const hb = heartbeatHealth(round.heartbeat_age_seconds);
    const elapsedSec = round.started_at
        ? Math.floor((Date.now() - new Date(round.started_at).getTime()) / 1000)
        : null;

    const canCancel = round.status === 'running' || round.status === 'pending';

    return (
        <div className="space-y-4">
            {/* 1. round 元信息 */}
            <Card>
                <CardContent className="pt-4 pb-4">
                    <div className="flex items-start justify-between gap-3">
                        <div>
                            <div className="flex items-center gap-2 mb-1">
                                {round.industry_names.length > 0 ? (
                                    round.industry_names.slice(0, 3).map(n => (
                                        <Badge key={n} variant="secondary" className="text-xs">{n}</Badge>
                                    ))
                                ) : (
                                    <Badge variant="secondary" className="text-xs">未知行业</Badge>
                                )}
                                <span className="font-mono text-[10px] text-muted-foreground">{round.round_id}</span>
                            </div>
                            <div className="flex items-center gap-3 text-sm">
                                {/* P14.3 C2: 复用 RoundStatusBadge · completed + Jina 失败时降级 amber · 跟历史列表口径一致 */}
                                <RoundStatusBadge
                                    status={round.status}
                                    hasStageFailures={stage3HasJinaFailures(stage_3_summary) || ((intent_summary?.failed ?? 0) > 0)}
                                />
                                {elapsedSec !== null && (
                                    <span className="text-muted-foreground text-xs">
                                        <Clock className="inline w-3 h-3 mr-0.5" />
                                        已耗时 {fmtDuration(elapsedSec)}
                                    </span>
                                )}
                                <span className={`text-xs ${hb.color}`}>
                                    心跳: {hb.label}
                                </span>
                            </div>
                            {round.error_message && (
                                <div className="text-xs text-red-700 mt-1 bg-red-50 rounded px-2 py-1">
                                    {round.error_message}
                                </div>
                            )}
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                            <Button size="sm" variant="ghost" onClick={() => void fetchLive()} disabled={loading}>
                                <RefreshCw className={`w-3.5 h-3.5 mr-1 ${loading ? 'animate-spin' : ''}`} />
                                刷新
                            </Button>
                            {canCancel && (
                                <Button size="sm" variant="destructive"
                                    onClick={() => void handleCancel()} disabled={cancelLoading}>
                                    {cancelLoading ? <Loader2 className="w-3 h-3 animate-spin mr-1" />
                                                  : <XCircle className="w-3 h-3 mr-1" />}
                                    取消任务
                                </Button>
                            )}
                        </div>
                    </div>
                </CardContent>
            </Card>

            {/* 2. 总进度 */}
            <Card>
                <CardContent className="pt-4 pb-4">
                    <div className="flex items-center justify-between text-sm mb-2">
                        <span className="font-medium">总进度</span>
                        <span className="text-muted-foreground tabular-nums">
                            {overall.label} · {overall.percent}%
                        </span>
                    </div>
                    <div className="h-2 rounded-full bg-muted overflow-hidden">
                        <div className="h-full bg-brand transition-all"
                            style={{ width: `${overall.percent}%` }} />
                    </div>
                </CardContent>
            </Card>

            {/* 3. 4 平台 AI 回答 */}
            <Card>
                <CardContent className="pt-4 pb-4">
                    <h3 className="text-sm font-semibold mb-3">四平台 AI 回答</h3>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                        {platforms.length === 0 && (
                            <div className="col-span-full text-xs text-muted-foreground text-center py-4">
                                还没有任何调用记录(stage_1 未开始)
                            </div>
                        )}
                        {platforms.map(p => <PlatformCard key={p.platform} p={p} />)}
                    </div>
                </CardContent>
            </Card>

            {/* 4. 阶段进度 */}
            <Card>
                <CardContent className="pt-4 pb-4">
                    <h3 className="text-sm font-semibold mb-3">阶段进度</h3>
                    <div className="space-y-1.5">
                        {stage_progress.map(s => <StageRow key={s.stage} s={s} />)}
                    </div>
                    <div className="text-[10px] text-muted-foreground mt-2 pl-2">
                        入文章库数: {articles_done}
                    </div>
                </CardContent>
            </Card>

            {/* 4b. P14.1 C3+ · Stage 3 reasons + error_samples (running 实时 / 完成态从 summary 兜底) */}
            {stage_3_summary && (
                <Card>
                    <CardContent className="pt-4 pb-4 space-y-3">
                        <div className="flex items-center justify-between text-sm">
                            <span className="font-semibold">Stage 3 · 抓取分类</span>
                            <span className="text-[10px] text-muted-foreground tabular-nums">
                                processed {stage_3_summary.processed ?? 0} / {stage_3_summary.total_urls ?? 0}
                                {typeof stage_3_summary.jina_requests === 'number' &&
                                    <> · Jina 请求 {stage_3_summary.jina_requests}</>}
                            </span>
                        </div>
                        {/* P14.2 C1: 7 类 reasons · 自适应 grid 防第 7 项挤坏 */}
                        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2 text-[11px]">
                            {([
                                ['crawled_new', '新抓', 'text-green-700 bg-green-50'],
                                ['crawled_dup', '跨行业复用', 'text-indigo-700 bg-indigo-50'],
                                ['reused_existing', '复用旧文章', 'text-blue-700 bg-blue-50'],
                                ['skipped_short', '内容太短', 'text-gray-700 bg-gray-50'],
                                ['skipped_jina_failed', 'Jina 失败', 'text-red-700 bg-red-50'],
                                ['skipped_oss_failed', 'OSS 失败', 'text-orange-700 bg-orange-50'],
                                ['failed_unknown', '未分类', 'text-purple-700 bg-purple-50'],
                            ] as const).map(([k, label, cls]) => (
                                <div key={k} className={`rounded px-2 py-1 ${cls} flex items-center justify-between`}>
                                    <span>{label}</span>
                                    <span className="tabular-nums font-medium">{stage_3_summary[k] ?? 0}</span>
                                </div>
                            ))}
                        </div>
                        {(stage_3_summary.error_samples && stage_3_summary.error_samples.length > 0) && (
                            <div className="border-t pt-2">
                                <div className="text-[11px] text-muted-foreground mb-1">
                                    失败样本(前 {stage_3_summary.error_samples.length} 条):
                                </div>
                                <div className="space-y-1">
                                    {stage_3_summary.error_samples.map((e, idx) => (
                                        <div key={idx} className="flex items-start gap-2 text-[11px] border-l-2 border-red-300 pl-2 py-0.5">
                                            <Badge variant="outline" className="shrink-0 text-[10px]">
                                                {e.error_type}
                                            </Badge>
                                            <span className="font-mono text-muted-foreground truncate flex-1" title={e.url}>
                                                {e.url}
                                            </span>
                                            <span className="text-red-700 truncate max-w-[40%]" title={e.error_message}>
                                                {e.error_message}
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                    </CardContent>
                </Card>
            )}


            {/* 4c. P15 · 文章意图分类进度 */}
            {intent_summary && (
                <Card>
                    <CardContent className="pt-4 pb-4 space-y-3">
                        <div className="flex items-center justify-between text-sm">
                            <span className="font-semibold">文章意图分类</span>
                            <span className="text-[10px] text-muted-foreground tabular-nums">
                                已分类 {intent_summary.classified ?? 0} / {intent_summary.total ?? 0}
                                {typeof intent_summary.failed === 'number' && intent_summary.failed > 0 &&
                                    <> · 失败 {intent_summary.failed}</>}
                                {intent_summary.model && <> · {intent_summary.model}</>}
                            </span>
                        </div>
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px]">
                            <div className="rounded px-2 py-1 text-emerald-700 bg-emerald-50 flex items-center justify-between">
                                <span>已分类</span>
                                <span className="tabular-nums font-medium">{intent_summary.classified ?? 0}</span>
                            </div>
                            <div className="rounded px-2 py-1 text-red-700 bg-red-50 flex items-center justify-between">
                                <span>失败</span>
                                <span className="tabular-nums font-medium">{intent_summary.failed ?? 0}</span>
                            </div>
                            <div className="rounded px-2 py-1 text-slate-700 bg-slate-50 flex items-center justify-between">
                                <span>总数</span>
                                <span className="tabular-nums font-medium">{intent_summary.total ?? 0}</span>
                            </div>
                            <div className="rounded px-2 py-1 text-blue-700 bg-blue-50 flex items-center justify-between">
                                <span>模型</span>
                                <span className="tabular-nums font-medium truncate max-w-[110px]" title={intent_summary.model || '-'}>{intent_summary.model || '-'}</span>
                            </div>
                        </div>
                        <div className="h-2 rounded-full bg-muted overflow-hidden">
                            <div className="h-full bg-emerald-500 transition-all"
                                style={{ width: `${intent_summary.total ? Math.min(100, Math.round(((intent_summary.classified ?? 0) + (intent_summary.failed ?? 0)) / intent_summary.total * 100)) : 0}%` }} />
                        </div>
                        {(intent_summary.error_samples && intent_summary.error_samples.length > 0) && (
                            <div className="border-t pt-2">
                                <div className="text-[11px] text-muted-foreground mb-1">
                                    失败样本(前 {intent_summary.error_samples.length} 条):
                                </div>
                                <div className="space-y-1">
                                    {intent_summary.error_samples.map((e, idx) => (
                                        <div key={idx} className="flex items-start gap-2 text-[11px] border-l-2 border-red-300 pl-2 py-0.5">
                                            <Badge variant="outline" className="shrink-0 text-[10px]">
                                                {e.error_type || e.reason || 'error'}
                                            </Badge>
                                            <span className="font-mono text-muted-foreground truncate flex-1" title={e.url || e.title}>
                                                {e.title || e.url || `article#${e.article_id ?? '-'}`}
                                            </span>
                                            <span className="text-red-700 truncate max-w-[40%]" title={e.error_message}>
                                                {e.error_message}
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                    </CardContent>
                </Card>
            )}

            {/* 5. 失败摘要 */}
            {errors.length > 0 && (
                <Card>
                    <CardContent className="pt-4 pb-4">
                        <button type="button" onClick={() => setErrorsOpen(o => !o)}
                            className="w-full flex items-center justify-between text-sm">
                            <span className="font-semibold flex items-center gap-2">
                                <AlertTriangle className="w-4 h-4 text-red-600" />
                                失败摘要 ({errors.length} 种 · 共 {errors.reduce((s, e) => s + e.count, 0)} 条)
                            </span>
                            {errorsOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                        </button>
                        {errorsOpen && (
                            <div className="mt-3 space-y-2">
                                {errors.map((e, idx) => (
                                    <div key={idx} className="flex items-start gap-2 text-xs border-l-2 border-red-300 pl-2 py-1">
                                        <Badge variant="outline" className="shrink-0 text-[10px]">
                                            {e.label}
                                        </Badge>
                                        <span className="text-red-700 font-medium shrink-0">× {e.count}</span>
                                        <span className="text-muted-foreground truncate">{e.sample}</span>
                                    </div>
                                ))}
                            </div>
                        )}
                    </CardContent>
                </Card>
            )}
          {confirmDialog}
        </div>
    );
}

// ==========================================
// 子组件
// ==========================================

function PlatformCard({ p }: { p: LiveStatusPlatform }) {
    const cardColor = STATUS_COLOR[p.status] || 'bg-gray-50';
    const labelColor = PLATFORM_COLOR[p.label] || 'bg-gray-100 text-gray-800';
    return (
        <div className={`border rounded-lg p-3 ${cardColor}`}>
            <div className="flex items-center justify-between mb-2">
                <Badge className={`text-xs ${labelColor}`}>{p.label}</Badge>
                <span className="text-[10px] text-muted-foreground">{STATUS_LABEL[p.status] || p.status}</span>
            </div>
            <div className="space-y-1 text-xs">
                <div className="flex items-center gap-2">
                    <CheckCircle2 className="w-3 h-3 text-green-600" />
                    <span className="tabular-nums">成功 {p.success} / 失败 {p.failed}</span>
                </div>
                <div className="text-muted-foreground">引用 {p.citations}</div>
                {p.latest_error && (
                    <div className="text-red-700 text-[10px] truncate" title={p.latest_error}>
                        {p.latest_error}
                    </div>
                )}
            </div>
        </div>
    );
}

function StageRow({ s }: { s: { stage: string; label: string; status: string; done: number; total: number } }) {
    const statusBadge = s.status === 'done' ? 'bg-green-100 text-green-800' :
                        s.status === 'running' ? 'bg-blue-100 text-blue-800' :
                                                  'bg-gray-100 text-gray-600';
    const statusLabel = s.status === 'done' ? '完成' :
                        s.status === 'running' ? '进行中' :
                                                  '未开始';
    const pct = s.total > 0 ? Math.min(100, Math.round((s.done / s.total) * 100)) : 0;
    return (
        <div className="flex items-center gap-3 text-xs">
            <Badge variant="secondary" className={`shrink-0 text-[10px] ${statusBadge}`}>{statusLabel}</Badge>
            <span className="font-medium w-28 shrink-0">{s.label}</span>
            {s.total > 0 && (
                <>
                    <div className="flex-1 h-1 rounded-full bg-muted overflow-hidden max-w-xs">
                        <div className={`h-full transition-all ${
                            s.status === 'done' ? 'bg-green-600' :
                            s.status === 'running' ? 'bg-blue-500' : 'bg-muted'
                        }`} style={{ width: `${pct}%` }} />
                    </div>
                    <span className="text-muted-foreground tabular-nums w-24 shrink-0">
                        {s.done} / {s.total} ({pct}%)
                    </span>
                </>
            )}
            {s.total === 0 && (
                <span className="text-muted-foreground">-</span>
            )}
        </div>
    );
}

// noop · 防 unused import
const _ = useMemo;
void _;
