/**
 * CTO-15.23 2026-05-09 · Admin LLM 成本监控
 * 数据源: /api/admin/llm-cost/* 6 endpoint
 * 给老板看 4 引擎 + N callsite 全量调用 · 跟控制台对账
 */
import { useState, useEffect, useCallback } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Activity, AlertTriangle, BarChart3, Loader2, RefreshCw, Wallet } from 'lucide-react';
import { authApi } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';

interface SummaryData {
    days: number;
    current: {
        total_calls: number;
        total_cost: number;  // LLM-only(对账控制台用)
        avg_cost: number;
        failed_calls: number;
        success_rate: number | null;
        avg_duration_ms: number;
        // 2026-05-22 老板拍板:发布外采(1x 真实价 · 用户扣 1.5x markup 不计)合并入总成本
        external_publish_cost?: number;
        external_publish_orders?: number;
        external_publish_items?: number;
        grand_total_cost?: number;  // = total_cost + external_publish_cost
    };
    previous: {
        total_calls: number;
        total_cost: number;
        external_publish_cost?: number;
        grand_total_cost?: number;
    };
    delta: {
        calls_pct: number | null;
        cost_pct: number | null;
        grand_total_pct?: number | null;
    };
}

interface PlatformItem {
    platform: string;
    calls: number;
    cost: number;
    input_tokens: number;
    output_tokens: number;
    avg_duration_ms: number;
    failed: number;
    share_pct: number;
}

interface CallerItem {
    caller: string;
    calls: number;
    cost: number;
    avg_cost: number;
    failed: number;
    share_pct: number;
}

interface ClientItem {
    brand_id: number;
    brand_name: string | null;
    calls: number;
    cost: number;
    last_quote_status: string | null;
    last_paid_amount: number;
}

interface RecentCall {
    id: number;
    created_at: string;
    caller: string;
    platform: string;
    model: string | null;
    input_tokens: number;
    output_tokens: number;
    estimated_cost: number;
    duration_ms: number;
    brand_id: number | null;
    success: boolean;
    error_msg: string | null;
}

const DAYS_OPTIONS = [
    { value: 1, label: '24小时' },
    { value: 7, label: '7天' },
    { value: 30, label: '30天' },
];

const daysFromPeriod = (period: string | null) => {
    if (period === 'month') return 30;
    if (period === 'quarter') return 90;
    if (period === 'year') return 365;
    return null;
};

export default function LLMCostDashboard() {
    const [searchParams, setSearchParams] = useSearchParams();
    const [days, setDays] = useState(() => daysFromPeriod(searchParams.get('period')) || Number(searchParams.get('days') || 7));
    const [summary, setSummary] = useState<SummaryData | null>(null);
    const [byPlatform, setByPlatform] = useState<PlatformItem[]>([]);
    const [byCaller, setByCaller] = useState<CallerItem[]>([]);
    const [byClient, setByClient] = useState<ClientItem[]>([]);
    const [recentCalls, setRecentCalls] = useState<RecentCall[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    const fetchAll = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            const [s, p, c, cl, r] = await Promise.all([
                authApi.get('/api/admin/llm-cost/summary', { params: { days } }),
                authApi.get('/api/admin/llm-cost/by-platform', { params: { days } }),
                authApi.get('/api/admin/llm-cost/by-caller', { params: { days } }),
                authApi.get('/api/admin/llm-cost/by-client', { params: { days, top: 20 } }),
                authApi.get('/api/admin/llm-cost/recent-calls', { params: { limit: 100 } }),
            ]);
            setSummary(s.data);
            setByPlatform(p.data.items || []);
            setByCaller(c.data.items || []);
            setByClient(cl.data.items || []);
            setRecentCalls(r.data.items || []);
        } catch (e: any) {
            setError(e.response?.data?.detail || e.message || '加载失败');
        } finally {
            setLoading(false);
        }
    }, [days]);

    useEffect(() => {
        fetchAll();
    }, [fetchAll]);

    useEffect(() => {
        const periodDays = daysFromPeriod(searchParams.get('period'));
        const queryDays = Number(searchParams.get('days') || '');
        const nextDays = periodDays || (Number.isFinite(queryDays) && queryDays > 0 ? queryDays : null);
        if (nextDays && nextDays !== days) setDays(nextDays);
    }, [searchParams, days]);

    const changeDays = (nextDays: number) => {
        setDays(nextDays);
        const next = new URLSearchParams(searchParams);
        next.delete('period');
        next.set('days', String(nextDays));
        setSearchParams(next);
    };

    const fmt = (n: number, decimals = 2) =>
        n.toLocaleString('zh-CN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });

    const fmtDelta = (pct: number | null) => {
        if (pct == null) return '—';
        const cls = pct > 0 ? 'text-orange-500' : pct < 0 ? 'text-green-500' : 'text-muted-foreground';
        const sign = pct > 0 ? '+' : '';
        return <span className={cls}>{sign}{pct}%</span>;
    };

    if (error && !summary) {
        return <div className="text-center py-20 text-destructive">❌ {error}</div>;
    }

    return (
        <div className="container mx-auto py-6 px-4 space-y-6 max-w-7xl">
            {/* 标题 + 时间选择 */}
            <div className="flex items-center justify-between flex-wrap gap-3">
                <div className="flex items-center gap-2">
                    <Activity className="h-6 w-6 text-blue-500" />
                    <h1 className="text-2xl font-bold">LLM 成本监控</h1>
                    <span className="text-sm text-muted-foreground">
                        监控 4 引擎 + 多 callsite 调用 · 对账控制台
                    </span>
                    <Link to="/admin/finance" className="ml-1 text-sm text-primary hover:underline flex items-center gap-1">
                        <Wallet className="h-3.5 w-3.5" />完整财务中心 →
                    </Link>
                </div>
                <div className="flex items-center gap-2">
                    <div className="flex rounded-lg border border-border bg-card overflow-hidden">
                        {DAYS_OPTIONS.map(opt => (
                            <button
                                key={opt.value}
                                onClick={() => changeDays(opt.value)}
                                className={`px-3 py-1.5 text-sm transition-colors ${
                                    days === opt.value
                                        ? 'bg-primary text-primary-foreground'
                                        : 'hover:bg-muted'
                                }`}
                            >
                                {opt.label}
                            </button>
                        ))}
                    </div>
                    <Button variant="outline" size="sm" onClick={fetchAll} disabled={loading}>
                        {loading ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                        刷新
                    </Button>
                </div>
            </div>

            {/* 卡片(2026-05-22 总成本卡 + 新增 发布外采 + 总账 grand total)*/}
            {summary && (
                <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
                    <div className="rounded-xl border border-border bg-card p-4">
                        <div className="text-sm text-muted-foreground">总调用量</div>
                        <div className="text-3xl font-bold mt-1">
                            {summary.current.total_calls.toLocaleString()}
                        </div>
                        <div className="text-xs mt-1">vs 上期 {fmtDelta(summary.delta.calls_pct)}</div>
                    </div>
                    <div className="rounded-xl border border-border bg-card p-4">
                        <div className="text-sm text-muted-foreground">LLM 成本(对账)</div>
                        <div className="text-3xl font-bold mt-1">¥{fmt(summary.current.total_cost)}</div>
                        <div className="text-xs mt-1">vs 上期 {fmtDelta(summary.delta.cost_pct)}</div>
                    </div>
                    {/* 2026-05-22 老板拍板:发布外采纳入总成本(1x 真价 · markup 1.5x 不计) */}
                    <div className="rounded-xl border border-border bg-card p-4">
                        <div className="text-sm text-muted-foreground">发布外采(媒体)</div>
                        <div className="text-3xl font-bold mt-1">¥{fmt(summary.current.external_publish_cost ?? 0)}</div>
                        <div className="text-xs text-muted-foreground mt-1">
                            {summary.current.external_publish_orders ?? 0} 订单 · {summary.current.external_publish_items ?? 0} 篇
                        </div>
                    </div>
                    {/* 2026-05-22 新增:总账 = LLM + 发布外采 */}
                    <div className="rounded-xl border border-primary/30 bg-primary/5 p-4">
                        <div className="text-sm text-muted-foreground">总账(全开销)</div>
                        <div className="text-3xl font-bold mt-1 text-primary">
                            ¥{fmt(summary.current.grand_total_cost ?? summary.current.total_cost)}
                        </div>
                        <div className="text-xs mt-1">
                            vs 上期 {fmtDelta(summary.delta.grand_total_pct ?? summary.delta.cost_pct)}
                        </div>
                    </div>
                    <div className={`rounded-xl border p-4 ${
                        summary.current.failed_calls > 0
                            ? 'border-red-500/30 bg-red-500/5'
                            : 'border-border bg-card'
                    }`}>
                        <div className="flex items-center gap-1 text-sm text-muted-foreground">
                            {summary.current.failed_calls > 0 && <AlertTriangle className="h-3.5 w-3.5 text-red-500" />}
                            异常 · 成功率
                        </div>
                        <div className={`text-3xl font-bold mt-1 ${
                            summary.current.failed_calls > 0 ? 'text-red-500' : ''
                        }`}>
                            {summary.current.failed_calls}
                        </div>
                        <div className="text-xs mt-1">
                            成功率 {summary.current.success_rate ?? '—'}%
                        </div>
                    </div>
                </div>
            )}

            {/* 主要内容 Tabs */}
            <Tabs defaultValue="platform">
                <TabsList>
                    <TabsTrigger value="platform">按平台(对账控制台)</TabsTrigger>
                    <TabsTrigger value="caller">按调用源</TabsTrigger>
                    <TabsTrigger value="client">客户排行</TabsTrigger>
                    <TabsTrigger value="recent">最近调用</TabsTrigger>
                </TabsList>

                <TabsContent value="platform">
                    <div className="rounded-xl border border-border bg-card p-4">
                        <div className="flex items-center gap-2 mb-3">
                            <BarChart3 className="h-4 w-4" />
                            <h2 className="font-semibold">按平台分组(对账 Moonshot/火山/阿里云/DeepSeek 控制台 · 误差应 &lt; 5%)</h2>
                        </div>
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead className="text-muted-foreground text-xs">
                                    <tr className="border-b border-border">
                                        <th className="text-left py-2">平台</th>
                                        <th className="text-right">调用次数</th>
                                        <th className="text-right">成本</th>
                                        <th className="text-right">输入Tok</th>
                                        <th className="text-right">输出Tok</th>
                                        <th className="text-right">平均时长</th>
                                        <th className="text-right">失败</th>
                                        <th className="text-right">占比</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {byPlatform.map(p => (
                                        <tr key={p.platform} className="border-b border-border/40">
                                            <td className="py-2 font-medium">{p.platform}</td>
                                            <td className="text-right">{p.calls.toLocaleString()}</td>
                                            <td className="text-right font-mono">¥{fmt(p.cost)}</td>
                                            <td className="text-right text-muted-foreground">{p.input_tokens.toLocaleString()}</td>
                                            <td className="text-right text-muted-foreground">{p.output_tokens.toLocaleString()}</td>
                                            <td className="text-right text-muted-foreground">{p.avg_duration_ms} ms</td>
                                            <td className={`text-right ${p.failed > 0 ? 'text-red-500' : ''}`}>{p.failed}</td>
                                            <td className="text-right">{p.share_pct}%</td>
                                        </tr>
                                    ))}
                                    {byPlatform.length === 0 && (
                                        <tr><td colSpan={8} className="text-center py-8 text-muted-foreground">暂无数据</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </TabsContent>

                <TabsContent value="caller">
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">按调用源分组(monitoring/autofill/竞品/写文章/...)</h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead className="text-muted-foreground text-xs">
                                    <tr className="border-b border-border">
                                        <th className="text-left py-2">调用源</th>
                                        <th className="text-right">调用次数</th>
                                        <th className="text-right">成本</th>
                                        <th className="text-right">平均</th>
                                        <th className="text-right">失败</th>
                                        <th className="text-right">占比</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {byCaller.map(c => (
                                        <tr key={c.caller} className="border-b border-border/40">
                                            <td className="py-2 font-medium">{c.caller}</td>
                                            <td className="text-right">{c.calls.toLocaleString()}</td>
                                            <td className="text-right font-mono">¥{fmt(c.cost)}</td>
                                            <td className="text-right text-muted-foreground">¥{fmt(c.avg_cost, 4)}</td>
                                            <td className={`text-right ${c.failed > 0 ? 'text-red-500' : ''}`}>{c.failed}</td>
                                            <td className="text-right">{c.share_pct}%</td>
                                        </tr>
                                    ))}
                                    {byCaller.length === 0 && (
                                        <tr><td colSpan={6} className="text-center py-8 text-muted-foreground">暂无数据</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </TabsContent>

                <TabsContent value="client">
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">Top 20 烧钱客户(对照 paid_amount 看是否白嫖)</h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead className="text-muted-foreground text-xs">
                                    <tr className="border-b border-border">
                                        <th className="text-left py-2">排名</th>
                                        <th className="text-left">客户</th>
                                        <th className="text-right">调用次数</th>
                                        <th className="text-right">成本</th>
                                        <th className="text-left pl-4">最近 quote</th>
                                        <th className="text-right">付费金额</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {byClient.map((c, idx) => (
                                        <tr key={c.brand_id} className="border-b border-border/40">
                                            <td className="py-2 text-muted-foreground">{idx + 1}</td>
                                            <td className="font-medium">{c.brand_name || `brand#${c.brand_id}`}</td>
                                            <td className="text-right">{c.calls.toLocaleString()}</td>
                                            <td className="text-right font-mono">¥{fmt(c.cost)}</td>
                                            <td className={`pl-4 ${c.last_quote_status === 'paid' ? 'text-green-500' : 'text-orange-500'}`}>
                                                {c.last_quote_status || '—'}
                                            </td>
                                            <td className="text-right">¥{fmt(c.last_paid_amount)}</td>
                                        </tr>
                                    ))}
                                    {byClient.length === 0 && (
                                        <tr><td colSpan={6} className="text-center py-8 text-muted-foreground">暂无数据</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </TabsContent>

                <TabsContent value="recent">
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">最近 100 次调用</h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-xs">
                                <thead className="text-muted-foreground">
                                    <tr className="border-b border-border">
                                        <th className="text-left py-2">时间</th>
                                        <th className="text-left">caller</th>
                                        <th className="text-left">platform</th>
                                        <th className="text-left">model</th>
                                        <th className="text-right">in/out tok</th>
                                        <th className="text-right">成本</th>
                                        <th className="text-right">时长</th>
                                        <th className="text-right">brand</th>
                                        <th className="text-left">状态</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recentCalls.map(r => (
                                        <tr key={r.id} className="border-b border-border/40 font-mono">
                                            <td className="py-1.5 whitespace-nowrap">
                                                {new Date(r.created_at).toLocaleString('zh-CN', { hour12: false })}
                                            </td>
                                            <td>{r.caller}</td>
                                            <td>{r.platform}</td>
                                            <td className="text-muted-foreground truncate max-w-[180px]">{r.model || '—'}</td>
                                            <td className="text-right text-muted-foreground">
                                                {r.input_tokens}/{r.output_tokens}
                                            </td>
                                            <td className="text-right">¥{fmt(r.estimated_cost, 4)}</td>
                                            <td className="text-right text-muted-foreground">{r.duration_ms}ms</td>
                                            <td className="text-right text-muted-foreground">{r.brand_id || '—'}</td>
                                            <td className={r.success ? 'text-green-500' : 'text-red-500'}>
                                                {r.success ? 'OK' : (r.error_msg || 'FAIL').slice(0, 40)}
                                            </td>
                                        </tr>
                                    ))}
                                    {recentCalls.length === 0 && (
                                        <tr><td colSpan={9} className="text-center py-8 text-muted-foreground">暂无数据</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </TabsContent>
            </Tabs>
        </div>
    );
}
