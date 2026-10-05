/**
 * GEO 调研监测后台 · 跑批管理(rounds)
 *
 * 功能(后端 6 接口):
 *   - GET    /rounds                      列表(status / triggered_by 过滤 + 翻页)
 *   - GET    /rounds/{round_id}           详情(snapshot + 统计)
 *   - POST   /rounds/manual-trigger       手动触发(多选行业 + note)
 *   - POST   /rounds/{round_id}/cancel    取消(仅 running/pending)
 *   - POST   /rounds/{round_id}/resume    续跑(仅 failed_resumable)
 *   - GET    /rounds/{round_id}/cost      成本明细(by_item)
 *
 * UX(老板拍板):
 *   - 列表头:状态 dropdown / 触发方式 dropdown / 刷新 / "手动触发" 按钮
 *   - 表格:round_id · status badge · triggered_by · current_stage · 启动 · 完成 · 心跳 · 操作
 *   - 操作:详情 / 取消(running/pending)/ 续跑(failed_resumable)
 *   - 详情 Dialog:基础字段 + progress + snapshot + 统计 + 成本 tab
 *   - 手动触发 Dialog:多选行业 checkbox + note textarea
 *   - 错误码:409 month_budget_exhausted / not_cancellable / not_resumable / race_status_changed
 */
'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { formatApiErrorForDisplay } from '@/lib/api';
import { taskStatusLabel, stageLabel } from '@/lib/v35Terminology';
import {
    Loader2, RefreshCw, PlayCircle, XCircle, RotateCcw, Eye,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import RunningMonitorView from './RunningMonitorView';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
    Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
    Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import {
    Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
    researchMonitorApi,
    type Industry,
    type ResearchRound,
    type RoundDetail,
    type CostByItem,
} from '@/lib/researchMonitorApi';
import { RoundStatusBadge } from './components/RoundStatusBadge';
import {
    Stage3SummaryChips, Stage3SummaryCard, extractStage3FromSummary,
    stage3HasJinaFailures,
    summaryHasIntentFailures,
} from './components/Stage3SummaryView';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

// ==========================================
// 常量
// ==========================================

const PAGE_SIZE = 20;

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
    { value: '__all__', label: '全部状态' },
    { value: 'pending', label: '待启动' },
    { value: 'running', label: '运行中' },
    { value: 'completed', label: '已完成' },
    { value: 'partial_success', label: '部分成功' },
    { value: 'failed', label: '失败' },
    { value: 'failed_resumable', label: '可续跑' },
    { value: 'cancelled', label: '已取消' },
];

const TRIGGERED_BY_OPTIONS: Array<{ value: string; label: string }> = [
    { value: '__all__', label: '全部触发方式' },
    { value: 'cron', label: '半月定时' },
    { value: 'manual', label: '手动触发' },
    { value: 'missed_cron_recovery', label: '漏跑补跑' },
];

const CANCELLABLE = new Set(['running', 'pending']);
// P14-v9: cancelled 也可续跑 · 跟后端 RESUMABLE_STATUSES 对齐
//   admin cancel 通常因环境问题(网络/key)· 数据没污染 · 续跑会自动从断点继续 stage
const RESUMABLE = new Set(['failed_resumable', 'cancelled']);

// ==========================================
// 工具
// ==========================================

function fmtTime(s: string | null | undefined): string {
    if (!s) return '-';
    try {
        return new Date(s).toLocaleString('zh-CN', { hour12: false });
    } catch {
        return s;
    }
}

function fmtMoney(n: number | null | undefined): string {
    if (n === null || n === undefined || Number.isNaN(n)) return '-';
    return `¥${n.toFixed(2)}`;
}

// P14-v8b: stage 中文化 + 阶段编号 N/7
//   后端 stage 字段示例: stage_1_ai_fetch / stage_1_ai_fetch_done / stage_2_prefilter_done / ...
//   legacy 导入: legacy_imported (不带 stage_N 前缀 · idx=0 不显示编号)
const STAGE_LABEL_MAP: Record<string, string> = {
    'stage_1_ai_fetch': '1. AI 问答',
    'stage_1_ai_fetch_done': '1. AI 问答 (完成)',
    'stage_2_prefilter_done': '2. URL 过滤 (完成)',
    'stage_3_crawl': '3. 抓取原文',
    'stage_3_crawl_done': '3. 抓取原文 (完成)',
    'stage_4_clean': '4. 清洗内容',
    'stage_4_clean_done': '4. 清洗内容 (完成)',
    'stage_5_filter_done': '5. 入库筛选 (完成)',
    'stage_7_aggregate_done': '7. 聚合统计 (完成)',
    'stage_7_aggregate_failed': '7. 聚合统计 (失败)',
    'stage_8_notify_done': '8. 完成通知',
    'stage_8_notify_failed': '8. 完成通知 (失败)',
    'legacy_imported': '历史导入',
};
function parseRoundStage(stage: string | null): { label: string; idx: number } {
    if (!stage) return { label: '-', idx: 0 };
    const label = STAGE_LABEL_MAP[stage] || stage;
    const m = stage.match(/^stage_(\d+)/);
    const idx = m ? Number(m[1]) : 0;
    return { label, idx };
}

// ==========================================
// 主面板
// ==========================================

export default function RoundsPanel() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    // 列表
    const [rounds, setRounds] = useState<ResearchRound[]>([]);
    const [total, setTotal] = useState(0);
    const [offset, setOffset] = useState(0);
    const [loading, setLoading] = useState(false);

    // 筛选
    const [statusFilter, setStatusFilter] = useState<string>('__all__');
    const [triggeredByFilter, setTriggeredByFilter] = useState<string>('__all__');

    // 行业列表(给手动触发 dialog 用)
    const [industries, setIndustries] = useState<Industry[]>([]);

    // 详情 dialog
    const [detailOpen, setDetailOpen] = useState(false);
    const [activeRoundId, setActiveRoundId] = useState<string | null>(null);
    const [detailLoading, setDetailLoading] = useState(false);
    const [detailData, setDetailData] = useState<RoundDetail | null>(null);

    // 成本 tab(详情 dialog 内)
    const [costLoading, setCostLoading] = useState(false);
    const [costData, setCostData] = useState<{
        round_id: string;
        total_yuan: number;
        by_item: CostByItem[];
        month_budget_remaining: number;
    } | null>(null);

    // 手动触发 dialog
    const [triggerOpen, setTriggerOpen] = useState(false);
    const [triggerSelectedIds, setTriggerSelectedIds] = useState<Set<number>>(new Set());
    const [triggerNote, setTriggerNote] = useState('');
    const [triggerSubmitting, setTriggerSubmitting] = useState(false);

    // 行级动作 loading
    const [actionLoadingId, setActionLoadingId] = useState<string | null>(null);

    // 拉行业(给触发 dialog 用)
    useEffect(() => {
        let mounted = true;
        researchMonitorApi
            .listIndustries()
            .then((res) => {
                if (!mounted) return;
                setIndustries(res.industries.filter((i) => i.active));
            })
            .catch((e: Error) => {
                if (!mounted) return;
                toast.error(formatApiErrorForDisplay(e, '加载行业失败', 'admin'));
            });
        return () => {
            mounted = false;
        };
    }, []);

    // ===== 拉列表 =====
    const fetchList = useCallback(
        async (resetOffset = false) => {
            setLoading(true);
            const targetOffset = resetOffset ? 0 : offset;
            try {
                const res = await researchMonitorApi.listRounds({
                    status: statusFilter === '__all__' ? undefined : statusFilter,
                    triggered_by:
                        triggeredByFilter === '__all__' ? undefined : triggeredByFilter,
                    limit: PAGE_SIZE,
                    offset: targetOffset,
                });
                setRounds(res.rounds);
                setTotal(res.total);
                if (resetOffset) setOffset(0);
            } catch (e) {
                toast.error(formatApiErrorForDisplay(e, '加载列表失败 · 请重试', 'admin'));
            } finally {
                setLoading(false);
            }
        },
        [statusFilter, triggeredByFilter, offset],
    );

    // 筛选变化 → 重置到第 1 页
    useEffect(() => {
        void fetchList(true);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [statusFilter, triggeredByFilter]);

    // offset 变化 → 翻页
    useEffect(() => {
        if (offset === 0) return;
        void fetchList(false);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [offset]);

    // P14-v8 (2026-05-27 老板反馈): 列表里有 running/pending 时自动每 5s 轮询
    // 全部 completed/failed/cancelled 后停止 · 避免老板"不知道是不是在跑"
    const hasActiveRound = useMemo(
        () => rounds.some((r) => r.status === 'running' || r.status === 'pending'),
        [rounds],
    );
    // P15.1 (2026-05-28): 当前 active round (运行监控视图主对象 · 取第一个 running/pending)
    const activeRound = useMemo(
        () => rounds.find((r) => r.status === 'running' || r.status === 'pending') || null,
        [rounds],
    );
    // P15.1: 二级 tab · 默认: 有 active round → 运行监控 / 无 → 历史
    const [subTab, setSubTab] = useState<'monitor' | 'history'>('history');
    // 自动切 (mount 时 + active round 出现时)
    useEffect(() => {
        if (activeRound && subTab === 'history') setSubTab('monitor');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeRound?.round_id]);
    useEffect(() => {
        if (!hasActiveRound) return;
        const handle = setInterval(() => { void fetchList(false); }, 5000);
        return () => clearInterval(handle);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [hasActiveRound]);

    // ===== 详情 =====
    const openDetail = useCallback(async (round_id: string) => {
        setActiveRoundId(round_id);
        setDetailOpen(true);
        setDetailLoading(true);
        setDetailData(null);
        setCostData(null);
        try {
            const res = await researchMonitorApi.getRoundDetail(round_id);
            setDetailData(res);
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载详情失败 · 请重试', 'admin'));
        } finally {
            setDetailLoading(false);
        }
    }, []);

    const fetchCost = useCallback(async () => {
        if (!activeRoundId) return;
        setCostLoading(true);
        try {
            const res = await researchMonitorApi.getRoundCost(activeRoundId);
            setCostData(res);
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载成本失败 · 请重试', 'admin'));
        } finally {
            setCostLoading(false);
        }
    }, [activeRoundId]);

    // ===== 取消 =====
    const handleCancel = useCallback(
        async (round: ResearchRound) => {
            if (!(await askConfirm({ title: `确认取消跑批 ${round.round_id}?`, description: `注意:不会立即停止已发出的模型/接口请求；系统会在下一个阶段或任务边界停止，且不会再把状态改为已完成。已发出请求的成本仍会产生。`, danger: true }))) {
                return;
            }
            setActionLoadingId(round.round_id);
            try {
                const res = await researchMonitorApi.cancelRound(round.round_id);
                toast.success(`已取消 · 原状态 ${taskStatusLabel(res.cancelled_from)}`, {
                    description: '已停止后续阶段 · 已发出的请求成本不退',
                    duration: 8000,
                });
                // r11 r10 盲区修:RoundsPanel cancel/resume description 也去英文已确认
                void fetchList(false);
            } catch (e) {
                const err = e as Error & { status?: number };
                if (err.status === 409) toast.error(formatApiErrorForDisplay(err, '取消失败 · 请重试', 'admin'));
                else toast.error(formatApiErrorForDisplay(err, '取消失败 · 请重试', 'admin'));
            } finally {
                setActionLoadingId(null);
            }
        },
        [fetchList],
    );

    // ===== 续跑 =====
    const handleResume = useCallback(
        async (round: ResearchRound) => {
            setActionLoadingId(round.round_id);
            try {
                const res = await researchMonitorApi.resumeRound(round.round_id);
                toast.success(`已续跑 · 原状态 ${taskStatusLabel(res.resumed_from)}`);
                void fetchList(false);
            } catch (e) {
                const err = e as Error & { status?: number };
                toast.error(formatApiErrorForDisplay(err, '续跑失败 · 请重试', 'admin'));
            } finally {
                setActionLoadingId(null);
            }
        },
        [fetchList],
    );

    // ===== 手动触发 =====
    const openTrigger = () => {
        setTriggerSelectedIds(new Set());
        setTriggerNote('');
        setTriggerOpen(true);
    };

    const toggleTriggerIndustry = (id: number, checked: boolean) => {
        setTriggerSelectedIds((s) => {
            const next = new Set(s);
            if (checked) next.add(id);
            else next.delete(id);
            return next;
        });
    };

    const submitTrigger = async () => {
        setTriggerSubmitting(true);
        try {
            const ids = Array.from(triggerSelectedIds);
            const res = await researchMonitorApi.manualTriggerRound({
                industry_ids: ids.length === 0 ? undefined : ids,
                note: triggerNote.trim() || undefined,
            });
            toast.success(`已启动跑批 · 编号 ${res.round_id}`, {
                description: '后台异步执行 · 可在列表查看进度',
                duration: 8000,
            });
            setTriggerOpen(false);
            void fetchList(true);
        } catch (e) {
            const err = e as Error & { status?: number; detail?: unknown };
            if (err.status === 409) {
                const detail = err.detail as { spent?: number; limit?: number } | undefined;
                toast.error(
                    `本月预算已超 · 已用 ${detail?.spent ?? '?'} / ${detail?.limit ?? '?'} 元`,
                    { description: '请联系运维上调预算后重试', duration: 10000 },
                );
            } else {
                toast.error(formatApiErrorForDisplay(err, '触发失败 · 请重试', 'admin'));
            }
        } finally {
            setTriggerSubmitting(false);
        }
    };

    // P14-v6: 派生 · 只考虑 "有 active prompts" 的行业 (无 prompts 的不可勾选)
    // active_prompt_count 字段后端 /industries 返回 · 老数据兜底视为 0
    const triggerableIndustries = useMemo(
        () => industries.filter((i) => (i.active_prompt_count ?? 0) > 0),
        [industries],
    );
    const allTriggerableIds = useMemo(
        () => triggerableIndustries.map((i) => i.id),
        [triggerableIndustries],
    );
    const allTriggerSelected =
        allTriggerableIds.length > 0 &&
        allTriggerableIds.every((id) => triggerSelectedIds.has(id));

    return (
        <div className="space-y-4">
            {/* P14-v10: 自动跑批 cron 状态 banner · 显示下次触发时间 + 当前配置 */}
            <CronStatusBanner />

            {/* P15.1: 二级 tab · 运行监控 / 历史跑批 */}
            <Tabs value={subTab} onValueChange={(v) => setSubTab(v as 'monitor' | 'history')}>
                <TabsList>
                    <TabsTrigger value="monitor">
                        🟢 运行监控{hasActiveRound ? ` · ${rounds.filter(r => r.status === 'running' || r.status === 'pending').length}` : ''}
                    </TabsTrigger>
                    <TabsTrigger value="history">📋 历史跑批</TabsTrigger>
                </TabsList>

                <TabsContent value="monitor" className="mt-3">
                    <RunningMonitorView activeRound={activeRound} onCancel={async (rid) => {
                        // 复用现有 cancel · 简化版 (不走 confirm 因为 RunningMonitorView 自己 confirm)
                        await researchMonitorApi.cancelRound(rid);
                        await fetchList(false);
                    }} />
                </TabsContent>

                <TabsContent value="history" className="mt-3 space-y-4">
            {/* 顶部工具栏 */}
            <Card>
                <CardContent className="pt-4 pb-4">
                    <div className="flex flex-wrap items-center gap-3">
                        <div className="flex items-center gap-2">
                            <span className="text-sm text-muted-foreground">状态</span>
                            <Select value={statusFilter} onValueChange={setStatusFilter}>
                                <SelectTrigger className="w-36">
                                    <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                    {STATUS_OPTIONS.map((opt) => (
                                        <SelectItem key={opt.value} value={opt.value}>
                                            {opt.label}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                        <div className="flex items-center gap-2">
                            <span className="text-sm text-muted-foreground">触发方式</span>
                            <Select
                                value={triggeredByFilter}
                                onValueChange={setTriggeredByFilter}
                            >
                                <SelectTrigger className="w-36">
                                    <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                    {TRIGGERED_BY_OPTIONS.map((opt) => (
                                        <SelectItem key={opt.value} value={opt.value}>
                                            {opt.label}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                        <div className="ml-auto flex items-center gap-2">
                            <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => void fetchList(false)}
                                disabled={loading}
                            >
                                <RefreshCw
                                    className={`w-4 h-4 mr-1 ${loading ? 'animate-spin' : ''}`}
                                />
                                刷新
                            </Button>
                            <Button size="sm" onClick={openTrigger}>
                                <PlayCircle className="w-4 h-4 mr-1" />
                                手动触发
                            </Button>
                        </div>
                    </div>
                </CardContent>
            </Card>

            {/* 列表 */}
            <Card>
                <CardContent className="p-0">
                    <Table>
                        <TableHeader>
                            <TableRow>
                                <TableHead className="w-44">跑批编号</TableHead>
                                <TableHead className="w-24">状态</TableHead>
                                <TableHead className="w-24">触发</TableHead>
                                <TableHead className="w-28">当前阶段</TableHead>
                                <TableHead className="w-44">启动时间</TableHead>
                                <TableHead className="w-44">完成时间</TableHead>
                                <TableHead className="w-44">最近心跳</TableHead>
                                <TableHead className="w-72">操作</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {loading && rounds.length === 0 && (
                                <TableRow>
                                    <TableCell colSpan={8} className="text-center py-8">
                                        <Loader2 className="w-6 h-6 animate-spin inline" />
                                    </TableCell>
                                </TableRow>
                            )}
                            {!loading && rounds.length === 0 && (
                                <TableRow>
                                    <TableCell
                                        colSpan={8}
                                        className="text-center py-8 text-muted-foreground"
                                    >
                                        暂无跑批记录
                                    </TableCell>
                                </TableRow>
                            )}
                            {rounds.map((r) => {
                                const isActing = actionLoadingId === r.round_id;
                                const canCancel = CANCELLABLE.has(r.status);
                                const canResume = RESUMABLE.has(r.status);
                                return (
                                    <TableRow key={r.round_id}>
                                        <TableCell className="text-xs">
                                            {/* P14-v8: round_id 纯时间戳看不出行业 · 行业名作为主标识在上面 · round_id 缩小到次要位置 */}
                                            <div className="flex flex-col gap-1">
                                                {r.industries && r.industries.length > 0 && (
                                                    <div className="flex flex-wrap items-center gap-1">
                                                        {r.industries.slice(0, 3).map((ind) => (
                                                            <Badge
                                                                key={ind.id}
                                                                variant="secondary"
                                                                className="text-[10px] px-1.5 py-0.5"
                                                            >
                                                                {ind.name}
                                                            </Badge>
                                                        ))}
                                                        {r.industries.length > 3 && (
                                                            <span className="text-[10px] text-muted-foreground">
                                                                +{r.industries.length - 3}
                                                            </span>
                                                        )}
                                                    </div>
                                                )}
                                                <span className="font-mono text-[10px] text-muted-foreground" title={r.round_id}>
                                                    {r.round_id}
                                                </span>
                                                {/* P14.3 C1: stage 3 reasons chip · 历史 round 完成后从 summary_json.stage_3 读 */}
                                                <Stage3SummaryChips
                                                    s3={extractStage3FromSummary(r.summary_json as Record<string, unknown>)}
                                                />
                                            </div>
                                        </TableCell>
                                        <TableCell>
                                            {/* P14.3 C2: completed + Jina 失败时降级 amber · 不静悄悄绿色 */}
                                            <RoundStatusBadge
                                                status={r.status}
                                                hasStageFailures={stage3HasJinaFailures(
                                                    r.summary_json as Record<string, unknown>,
                                                ) || summaryHasIntentFailures(
                                                    r.summary_json as Record<string, unknown>,
                                                )}
                                            />
                                        </TableCell>
                                        <TableCell>
                                            <Badge variant="outline" className="text-xs">
                                                {r.triggered_by === 'cron'
                                                    ? '半月定时'
                                                    : r.triggered_by === 'missed_cron_recovery'
                                                        ? '漏跑补跑'
                                                        : '手动'}
                                            </Badge>
                                        </TableCell>
                                        <TableCell className="text-xs">
                                            {/* P14-v8b: 多阶段进度 · stage 中文化 + N/7 编号 + 各 stage 进度字段都识别 */}
                                            {(() => {
                                                const stageInfo = parseRoundStage(r.current_stage);
                                                const pj = (r.progress_json || {}) as Record<string, unknown>;
                                                // 各 stage 进度字段不同 · 兼容性映射
                                                let processed: number | null = null;
                                                let total: number | null = null;
                                                let progressLabel = '';
                                                if (pj.processed != null && pj.total != null) {
                                                    // stage_1 / 通用 N/M
                                                    processed = Number(pj.processed);
                                                    total = Number(pj.total);
                                                } else if (pj.kept_url_count != null && pj.raw_url_count != null) {
                                                    // stage_2 prefilter
                                                    processed = Number(pj.kept_url_count);
                                                    total = Number(pj.raw_url_count);
                                                    progressLabel = '保留';
                                                } else if (pj.crawled != null && (pj.total_urls != null || pj.total != null)) {
                                                    // stage_3 crawl · 后端字段 {total, crawled, skipped}
                                                    // processed = crawled + skipped (跳过的也算"处理完了" · 否则进度永远不到 100%)
                                                    processed = Number(pj.crawled) + Number(pj.skipped ?? 0);
                                                    total = Number(pj.total_urls ?? pj.total);
                                                    progressLabel = '抓取';
                                                }
                                                const pct = (total && total > 0 && processed != null)
                                                    ? Math.min(100, Math.round((processed / total) * 100)) : null;
                                                const isRunning = r.status === 'running';
                                                return (
                                                    <div className="flex flex-col gap-0.5">
                                                        <span className="font-medium">
                                                            {stageInfo.label}
                                                            {stageInfo.idx > 0 && (
                                                                <span className="text-muted-foreground ml-1">
                                                                    [{stageInfo.idx}/7]
                                                                </span>
                                                            )}
                                                        </span>
                                                        {isRunning && pct != null && (
                                                            <div className="flex items-center gap-1.5">
                                                                <div className="w-20 h-1 rounded-full bg-muted overflow-hidden">
                                                                    <div className="h-full bg-brand transition-all"
                                                                        style={{ width: `${pct}%` }} />
                                                                </div>
                                                                <span className="text-[10px] text-muted-foreground tabular-nums">
                                                                    {progressLabel && progressLabel + ' '}{processed}/{total} ({pct}%)
                                                                </span>
                                                            </div>
                                                        )}
                                                    </div>
                                                );
                                            })()}
                                        </TableCell>
                                        <TableCell className="text-xs text-muted-foreground">
                                            {fmtTime(r.started_at)}
                                        </TableCell>
                                        <TableCell className="text-xs text-muted-foreground">
                                            {fmtTime(r.finished_at)}
                                        </TableCell>
                                        <TableCell className="text-xs text-muted-foreground">
                                            {fmtTime(r.last_heartbeat_at)}
                                        </TableCell>
                                        <TableCell>
                                            <div className="flex items-center gap-1">
                                                <Button
                                                    size="sm"
                                                    variant="ghost"
                                                    onClick={() => void openDetail(r.round_id)}
                                                >
                                                    <Eye className="w-3 h-3 mr-1" />
                                                    详情
                                                </Button>
                                                {canCancel && (
                                                    <Button
                                                        size="sm"
                                                        variant="destructive"
                                                        disabled={isActing}
                                                        onClick={() => void handleCancel(r)}
                                                    >
                                                        {isActing ? (
                                                            <Loader2 className="w-3 h-3 animate-spin" />
                                                        ) : (
                                                            <XCircle className="w-3 h-3 mr-1" />
                                                        )}
                                                        取消
                                                    </Button>
                                                )}
                                                {canResume && (
                                                    <Button
                                                        size="sm"
                                                        variant="default"
                                                        disabled={isActing}
                                                        onClick={() => void handleResume(r)}
                                                    >
                                                        {isActing ? (
                                                            <Loader2 className="w-3 h-3 animate-spin" />
                                                        ) : (
                                                            <RotateCcw className="w-3 h-3 mr-1" />
                                                        )}
                                                        续跑
                                                    </Button>
                                                )}
                                            </div>
                                        </TableCell>
                                    </TableRow>
                                );
                            })}
                        </TableBody>
                    </Table>

                    {/* 翻页 */}
                    <div className="flex items-center justify-between p-3 border-t">
                        <div className="text-xs text-muted-foreground">
                            第 {Math.floor(offset / PAGE_SIZE) + 1} 页 · 共 {total} 条
                        </div>
                        <div className="flex items-center gap-2">
                            <Button
                                size="sm"
                                variant="outline"
                                disabled={offset === 0 || loading}
                                onClick={() =>
                                    setOffset((o) => Math.max(0, o - PAGE_SIZE))
                                }
                            >
                                上一页
                            </Button>
                            <Button
                                size="sm"
                                variant="outline"
                                disabled={offset + PAGE_SIZE >= total || loading}
                                onClick={() => setOffset((o) => o + PAGE_SIZE)}
                            >
                                下一页
                            </Button>
                        </div>
                    </div>
                </CardContent>
            </Card>

            {/* 详情 Dialog */}
            <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
                <DialogContent className="!max-w-[1100px] w-[90vw] h-[85vh] flex flex-col p-0 gap-0">
                    <DialogHeader className="px-6 py-4 border-b shrink-0">
                        <DialogTitle>跑批详情</DialogTitle>
                        <DialogDescription className="font-mono text-xs">
                            {activeRoundId}
                        </DialogDescription>
                    </DialogHeader>
                    <div className="flex-1 overflow-y-auto px-6 py-4">
                        {detailLoading ? (
                            <div className="flex items-center justify-center py-16">
                                <Loader2 className="w-8 h-8 animate-spin" />
                            </div>
                        ) : detailData ? (
                            <Tabs
                                defaultValue="overview"
                                onValueChange={(v) => {
                                    if (v === 'cost' && !costData && !costLoading) {
                                        void fetchCost();
                                    }
                                }}
                            >
                                <TabsList>
                                    <TabsTrigger value="overview">概览</TabsTrigger>
                                    <TabsTrigger value="progress">进度 / 快照</TabsTrigger>
                                    <TabsTrigger value="cost">成本明细</TabsTrigger>
                                </TabsList>

                                <TabsContent value="overview" className="space-y-3 mt-3">
                                    <div className="grid grid-cols-2 gap-3 text-sm">
                                        <KV label="状态">
                                            <RoundStatusBadge
                                                status={detailData.status}
                                                hasStageFailures={stage3HasJinaFailures(
                                                    detailData.summary_json as Record<string, unknown>,
                                                ) || summaryHasIntentFailures(
                                                    detailData.summary_json as Record<string, unknown>,
                                                )}
                                            />
                                        </KV>
                                        <KV label="当前阶段">
                                            {stageLabel(detailData.current_stage)}
                                        </KV>
                                        <KV label="触发方式">
                                            {detailData.triggered_by === 'cron'
                                                ? '半月定时'
                                                : detailData.triggered_by === 'missed_cron_recovery'
                                                    ? '漏跑补跑'
                                                : '手动'}
                                        </KV>
                                        <KV label="启动时间">
                                            {fmtTime(detailData.started_at)}
                                        </KV>
                                        <KV label="完成时间">
                                            {fmtTime(detailData.finished_at)}
                                        </KV>
                                        <KV label="最近心跳">
                                            {fmtTime(detailData.last_heartbeat_at)}
                                        </KV>
                                        <KV label="模型/接口调用次数">
                                            {(detailData.call_count as number | undefined) ?? '-'}
                                        </KV>
                                        <KV label="新增文章数">
                                            {(detailData.article_count as number | undefined) ?? '-'}
                                        </KV>
                                        <KV label="总成本">
                                            {fmtMoney(
                                                detailData.cost_total_yuan as number | undefined,
                                            )}
                                        </KV>
                                    </div>
                                    {/* P14.3 C1: stage 3 抓取分类卡 · 完成后历史回看也能解读为什么 0 新文章 */}
                                    <Stage3SummaryCard
                                        s3={extractStage3FromSummary(
                                            detailData.summary_json as Record<string, unknown>,
                                        )}
                                    />
                                </TabsContent>

                                <TabsContent value="progress" className="space-y-3 mt-3">
                                    <div>
                                        <Label className="text-xs text-muted-foreground">
                                            进度数据
                                        </Label>
                                        <pre className="mt-1 text-xs bg-muted/40 rounded p-3 overflow-auto max-h-[280px]">
                                            {JSON.stringify(
                                                detailData.progress_json || {},
                                                null,
                                                2,
                                            )}
                                        </pre>
                                    </div>
                                    <div>
                                        <Label className="text-xs text-muted-foreground">
                                            汇总数据
                                        </Label>
                                        <pre className="mt-1 text-xs bg-muted/40 rounded p-3 overflow-auto max-h-[280px]">
                                            {JSON.stringify(
                                                detailData.summary_json || {},
                                                null,
                                                2,
                                            )}
                                        </pre>
                                    </div>
                                    <div>
                                        <Label className="text-xs text-muted-foreground">
                                            快照数据
                                        </Label>
                                        <pre className="mt-1 text-xs bg-muted/40 rounded p-3 overflow-auto max-h-[280px]">
                                            {JSON.stringify(
                                                (detailData.snapshot_json as unknown) || {},
                                                null,
                                                2,
                                            )}
                                        </pre>
                                    </div>
                                </TabsContent>

                                <TabsContent value="cost" className="space-y-3 mt-3">
                                    {costLoading ? (
                                        <Loader2 className="w-6 h-6 animate-spin" />
                                    ) : costData ? (
                                        <>
                                            {/* legacy 跑批 by design 不灌 cost_log · 显示说明避免误以为 bug */}
                                            {(activeRoundId?.startsWith('legacy_') || activeRoundId?.startsWith('round_legacy_')) && (
                                                <div className="text-xs px-3 py-2 rounded-md bg-amber-50 text-amber-800 border border-amber-200">
                                                    历史导入数据(legacy) · 本地直跑后离线导入 · 未上传成本日志,所以本轮成本恒为 ¥0.00。月度预算剩余仍按真实跑批扣减。
                                                </div>
                                            )}
                                            <div className="grid grid-cols-2 gap-3 text-sm">
                                                <KV label="本轮总成本">
                                                    {fmtMoney(costData.total_yuan)}
                                                </KV>
                                                <KV label="月度预算剩余">
                                                    {fmtMoney(costData.month_budget_remaining)}
                                                </KV>
                                            </div>
                                            <Table>
                                                <TableHeader>
                                                    <TableRow>
                                                        <TableHead>项目</TableHead>
                                                        <TableHead className="w-24 text-right">
                                                            次数
                                                        </TableHead>
                                                        <TableHead className="w-32 text-right">
                                                            金额
                                                        </TableHead>
                                                    </TableRow>
                                                </TableHeader>
                                                <TableBody>
                                                    {costData.by_item.length === 0 && (
                                                        <TableRow>
                                                            <TableCell
                                                                colSpan={3}
                                                                className="text-center text-muted-foreground py-4"
                                                            >
                                                                暂无成本记录
                                                            </TableCell>
                                                        </TableRow>
                                                    )}
                                                    {costData.by_item.map((it, idx) => (
                                                        <TableRow key={idx}>
                                                            <TableCell className="text-xs">
                                                                {(it.item as string | undefined) ||
                                                                    it.platform ||
                                                                    '-'}
                                                            </TableCell>
                                                            <TableCell className="text-right text-xs">
                                                                {(it.records as number | undefined) ??
                                                                    it.calls ??
                                                                    '-'}
                                                            </TableCell>
                                                            <TableCell className="text-right text-xs">
                                                                {fmtMoney(
                                                                    (it.total as number | undefined) ??
                                                                        it.yuan,
                                                                )}
                                                            </TableCell>
                                                        </TableRow>
                                                    ))}
                                                </TableBody>
                                            </Table>
                                        </>
                                    ) : (
                                        <div className="text-sm text-muted-foreground">
                                            点击"成本明细"页签加载
                                        </div>
                                    )}
                                </TabsContent>
                            </Tabs>
                        ) : (
                            <div className="text-sm text-muted-foreground">
                                未加载到详情
                            </div>
                        )}
                    </div>
                </DialogContent>
            </Dialog>

            {/* 手动触发 Dialog */}
            <Dialog open={triggerOpen} onOpenChange={setTriggerOpen}>
                <DialogContent className="max-w-xl">
                    <DialogHeader>
                        <DialogTitle>手动触发跑批</DialogTitle>
                        <DialogDescription>
                            不勾任何行业 = 全部启用行业。实际预算阈值以系统配置为准。
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-4">
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <Label>选择行业(可空)</Label>
                                <Button
                                    size="sm"
                                    variant="ghost"
                                    onClick={() => {
                                        setTriggerSelectedIds(
                                            allTriggerSelected
                                                ? new Set()
                                                : new Set(allTriggerableIds),
                                        );
                                    }}
                                    disabled={allTriggerableIds.length === 0}
                                >
                                    {allTriggerSelected ? '取消全选' : '全选'}
                                </Button>
                            </div>
                            <div className="grid grid-cols-3 gap-2 max-h-[240px] overflow-y-auto p-2 border rounded">
                                {industries.length === 0 && (
                                    <div className="col-span-3 text-xs text-muted-foreground py-3 text-center">
                                        无启用行业
                                    </div>
                                )}
                                {/* P14-v6: 无 active prompts 的行业禁用勾选 + 灰显 + tooltip 提示 */}
                                {industries.map((ind) => {
                                    const promptCount = ind.active_prompt_count ?? 0;
                                    const disabled = promptCount === 0;
                                    return (
                                        <label
                                            key={ind.id}
                                            title={disabled ? '无 active Prompts · 请先在"行业 & Prompts" tab 配置' : `${promptCount} 个 active Prompts`}
                                            className={`flex items-center gap-2 text-sm p-1 rounded ${
                                                disabled
                                                    ? 'cursor-not-allowed opacity-50'
                                                    : 'cursor-pointer hover:bg-muted/30'
                                            }`}
                                        >
                                            <Checkbox
                                                checked={triggerSelectedIds.has(ind.id)}
                                                disabled={disabled}
                                                onCheckedChange={(v) =>
                                                    toggleTriggerIndustry(ind.id, Boolean(v))
                                                }
                                            />
                                            <span className="truncate">{ind.name}</span>
                                            <span className={`text-[10px] tabular-nums shrink-0 ${
                                                disabled ? 'text-amber-600' : 'text-muted-foreground'
                                            }`}>
                                                {disabled ? '无 Prompts' : `${promptCount}P`}
                                            </span>
                                        </label>
                                    );
                                })}
                            </div>
                        </div>
                        <div>
                            <Label htmlFor="trigger-note">备注(可空 · 写入 summary)</Label>
                            <Textarea
                                id="trigger-note"
                                value={triggerNote}
                                onChange={(e) => setTriggerNote(e.target.value)}
                                placeholder="例:CTO 测试新提示词集"
                                className="mt-1"
                                maxLength={500}
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button
                            variant="outline"
                            onClick={() => setTriggerOpen(false)}
                            disabled={triggerSubmitting}
                        >
                            取消
                        </Button>
                        <Button onClick={() => void submitTrigger()} disabled={triggerSubmitting}>
                            {triggerSubmitting ? (
                                <>
                                    <Loader2 className="w-3 h-3 animate-spin mr-1" />
                                    触发中
                                </>
                            ) : (
                                '确认触发'
                            )}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
                </TabsContent>
            </Tabs>
          {confirmDialog}
        </div>
    );
}

// ==========================================
// 内部小工具:KV 行
// ==========================================

function KV({ label, children }: { label: string; children: React.ReactNode }) {
    return (
        <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground w-24 shrink-0">{label}</span>
            <span className="flex-1">{children}</span>
        </div>
    );
}


// P14-v10: 自动跑批 cron 状态 banner
//   显示 "下次自动跑批: 2026-06-01 02:00 (CST · 每月 1,16 号)"
//   cron 禁用时显示 "自动跑批已关闭 · 仅手动触发"
//   配置修改需去 "系统配置" tab 改 cron_enabled/cron_days/cron_hour
function CronStatusBanner() {
    const [status, setStatus] = useState<{
        enabled: boolean; days: string; hour: number;
        next_run_at: string | null;
    } | null>(null);
    useEffect(() => {
        let mounted = true;
        researchMonitorApi.getCronStatus()
            .then(s => { if (mounted) setStatus(s); })
            .catch(() => { /* 静默 · 不阻塞主页面 */ });
        return () => { mounted = false; };
    }, []);
    if (!status) return null;

    const nextLabel = status.next_run_at
        ? new Date(status.next_run_at).toLocaleString('zh-CN', { hour12: false })
        : '-';
    const hourFmt = String(status.hour).padStart(2, '0');

    return (
        <Card>
            <CardContent className="py-3">
                <div className="flex items-center justify-between gap-3 text-sm">
                    <div className="flex items-center gap-2">
                        {status.enabled ? (
                            <>
                                <Badge variant="secondary" className="bg-green-100 text-green-800">自动跑批 已开启</Badge>
                                <span className="text-muted-foreground">
                                    下次触发: <span className="text-foreground font-medium">{nextLabel}</span>
                                </span>
                                <span className="text-muted-foreground text-xs">
                                    (每月 {status.days} 号 · {hourFmt}:00 北京时间)
                                </span>
                            </>
                        ) : (
                            <>
                                <Badge variant="secondary" className="bg-amber-100 text-amber-800">自动跑批 已关闭</Badge>
                                <span className="text-muted-foreground">仅可手动触发</span>
                            </>
                        )}
                    </div>
                    <span className="text-[10px] text-muted-foreground">修改: 系统配置 tab · cron_enabled / cron_days / cron_hour</span>
                </div>
            </CardContent>
        </Card>
    );
}
