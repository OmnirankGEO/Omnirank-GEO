/**
 * 财务中心 · GEO CTO-15.23 · 2026-05-30
 * 投资人/审计级财务报表 · 数据源 /api/admin/finance/*
 * 6 Tab:总览 / 利润表 / 负债与现金 / 成本中心 / 代理结算 / 对账审计
 * 双口径(权责/现金)+ 期间(月/季/年/自定义)+ 审计包导出
 */
import { useState, useEffect, useCallback } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
    Wallet, TrendingUp, Scale, Coins, Banknote, RefreshCw, Loader2,
    Download, Plus, Trash2, AlertTriangle, CheckCircle2, X,
} from 'lucide-react';
import { authApi } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { toast } from 'sonner';

const PERIODS = [
    { value: 'month', label: '本月' },
    { value: 'quarter', label: '本季' },
    { value: 'year', label: '本年' },
    { value: 'custom', label: '自定义' },
];

const fmtY = (n: number | null | undefined) =>
    '¥' + Number(n ?? 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const fmtCents = (n: number | null | undefined) => fmtY(Number(n ?? 0) / 100);

const fmtNum = (n: number | null | undefined) => Number(n ?? 0).toLocaleString('zh-CN');

const fmtTime = (s: string | null | undefined) => s ? new Date(s).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
}) : '—';

function Delta({ pct, goodUp = true }: { pct: number | null | undefined; goodUp?: boolean }) {
    if (pct == null) return <span className="text-muted-foreground">—</span>;
    const positive = pct > 0;
    const good = goodUp ? positive : !positive;
    const cls = pct === 0 ? 'text-muted-foreground' : good ? 'text-green-500' : 'text-orange-500';
    return <span className={cls}>{positive ? '+' : ''}{pct}%</span>;
}

function StatCard({ icon, label, value, delta, sub, highlight }: any) {
    return (
        <div className={`rounded-xl border p-4 ${highlight ? 'border-primary/30 bg-primary/5' : 'border-border bg-card'}`}>
            <div className="flex items-center gap-1.5 text-sm text-muted-foreground">{icon}{label}</div>
            <div className={`text-2xl font-bold mt-1 ${highlight ? 'text-primary' : ''}`}>{value}</div>
            {(delta !== undefined || sub) && (
                <div className="text-xs mt-1">{sub}{delta !== undefined && <> · 环比 {delta}</>}</div>
            )}
        </div>
    );
}

export default function FinanceCenter() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const navigate = useNavigate();
    const [searchParams, setSearchParams] = useSearchParams();
    const [period, setPeriod] = useState(searchParams.get('period') || 'year');
    const [basis, setBasis] = useState(searchParams.get('basis') || 'accrual');
    const [activeTab, setActiveTab] = useState(searchParams.get('tab') || 'overview');
    const [customStart, setCustomStart] = useState('');
    const [customEnd, setCustomEnd] = useState('');
    const [d, setD] = useState<any>({});
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [flow, setFlow] = useState<any>(null);
    const [flowLoading, setFlowLoading] = useState(false);

    // opex 新增表单
    const [opexForm, setOpexForm] = useState<any>({ period_month: '2026-06', category: 'server', amount_yuan: '', note: '' });
    const [savingOpex, setSavingOpex] = useState(false);

    const buildParams = useCallback(() => {
        const pr: any = { period, basis };
        if (period === 'custom' && customStart && customEnd) {
            pr.start = customStart;
            pr.end = customEnd;
        }
        return pr;
    }, [period, basis, customStart, customEnd]);

    const fetchAll = useCallback(async () => {
        if (period === 'custom' && (!customStart || !customEnd)) return;
        setLoading(true);
        setError('');
        try {
            const pr = buildParams();
            const r = await Promise.allSettled([
                authApi.get('/api/admin/finance/overview', { params: pr }),
                authApi.get('/api/admin/finance/pnl', { params: pr }),
                authApi.get('/api/admin/finance/liabilities'),
                authApi.get('/api/admin/finance/cashflow', { params: pr }),
                authApi.get('/api/admin/finance/cost-center', { params: pr }),
                authApi.get('/api/admin/finance/agent-settlement', { params: pr }),
                authApi.get('/api/admin/finance/reconciliation'),
                authApi.get('/api/admin/finance/unit-economics', { params: pr }),
                authApi.get('/api/admin/finance/opex'),
                authApi.get('/api/admin/finance/ledger', { params: { ...pr, limit: 100 } }),
                authApi.get('/api/admin/finance/recharge-orders', { params: { ...pr, status: 'paid', limit: 50 } }),
            ]);
            const val = (i: number) => {
                const x = r[i];
                return x.status === 'fulfilled' ? x.value.data : null;
            };
            setD({
                overview: val(0), pnl: val(1), liabilities: val(2), cashflow: val(3),
                costCenter: val(4), agentSettlement: val(5), reconciliation: val(6),
                unitEconomics: val(7), opex: val(8), ledger: val(9),
                recharges: val(10),
            });
            // [复审 r3] 单端点失败不白屏整页(各 Tab 用可选链兜底)· 仅全部失败(未登录/网络全挂)才整页 error
            if (r.every(x => x.status === 'rejected')) {
                const firstRej = r.find(x => x.status === 'rejected');
                const reason: any = firstRej && firstRej.status === 'rejected' ? firstRej.reason : null;
                setError(reason?.response?.data?.detail || reason?.message || '加载失败');
            }
        } catch (e: any) {
            setError(e.response?.data?.detail || e.message || '加载失败');
        } finally {
            setLoading(false);
        }
    }, [period, customStart, customEnd, buildParams]);

    useEffect(() => { fetchAll(); }, [fetchAll]);

    const syncUrlState = (patch: Record<string, string | null>) => {
        const next = new URLSearchParams(searchParams);
        Object.entries(patch).forEach(([key, value]) => {
            if (value == null || value === '') next.delete(key);
            else next.set(key, value);
        });
        setSearchParams(next);
    };

    const changeBasis = (nextBasis: string) => {
        setBasis(nextBasis);
        syncUrlState({ basis: nextBasis, period, tab: activeTab });
    };

    const changePeriod = (nextPeriod: string) => {
        setPeriod(nextPeriod);
        syncUrlState({ period: nextPeriod, basis, tab: activeTab });
    };

    const changeTab = (tab: string) => {
        setActiveTab(tab);
        syncUrlState({ tab, period, basis });
    };

    const openRechargeFlow = useCallback(async (orderId: string) => {
        setFlowLoading(true);
        try {
            const res = await authApi.get(`/api/admin/finance/recharge-orders/${orderId}/flow`);
            setFlow(res.data);
            const next = new URLSearchParams(searchParams);
            next.set('tab', 'recharges');
            next.set('order_id', orderId);
            setSearchParams(next);
        } catch (e: any) {
            toast.error('加载资金链路失败:' + (e.response?.data?.detail || e.message));
        } finally {
            setFlowLoading(false);
        }
    }, [searchParams, setSearchParams]);

    useEffect(() => {
        const orderId = searchParams.get('order_id');
        if (!orderId || String(flow?.order?.id || '') === orderId) return;
        setActiveTab('recharges');
        openRechargeFlow(orderId);
    }, [searchParams, flow?.order?.id, openRechargeFlow]);

    const entryNotice = (() => {
        if (searchParams.get('from') !== 'dashboard') return null;
        if (activeTab === 'recharges') return '来自运营控制台:本月营收(现金)';
        if (activeTab === 'cost' && searchParams.get('cost') === 'media') return '来自运营控制台:代发采购成本';
        if (activeTab === 'cost') return '来自运营控制台:本月总成本';
        if (activeTab === 'pnl') return '来自运营控制台:本月毛利 / 毛利率';
        if (activeTab === 'balance') return '来自运营控制台:ARPU / 单位经济';
        return '来自运营控制台:财务总览';
    })();

    const clearEntryFilter = () => {
        setFlow(null);
        setPeriod('year');
        setBasis('accrual');
        setActiveTab('overview');
        setSearchParams(new URLSearchParams());
    };

    const exportAudit = async () => {
        try {
            const res = await authApi.get('/api/admin/finance/export', { params: buildParams(), responseType: 'blob' });
            const blob = new Blob([res.data], { type: 'text/csv' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `finance_audit_${d.overview?.label || 'export'}.csv`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (e: any) {
            toast.error('导出失败:' + (e.response?.data?.detail || e.message));
        }
    };

    const saveOpex = async () => {
        const cents = Math.round(parseFloat(opexForm.amount_yuan || '0') * 100);
        if (!cents || cents < 0) { toast.error('请输入有效金额'); return; }
        setSavingOpex(true);
        try {
            await authApi.post('/api/admin/finance/opex', {
                period_month: opexForm.period_month,
                category: opexForm.category,
                amount_cents: cents,
                note: opexForm.note || null,
            });
            setOpexForm({ ...opexForm, amount_yuan: '', note: '' });
            await fetchAll();
        } catch (e: any) {
            toast.error('保存失败:' + (e.response?.data?.detail || e.message));
        } finally {
            setSavingOpex(false);
        }
    };

    const deleteOpex = async (id: number) => {
        if (!(await askConfirm({ title: '确认删除这条运营成本?', danger: true }))) return;
        try {
            await authApi.delete(`/api/admin/finance/opex/${id}`);
            await fetchAll();
        } catch (e: any) {
            toast.error('删除失败:' + (e.response?.data?.detail || e.message));
        }
    };

    if (error && !d.overview) {
        return <div className="text-center py-20 text-destructive">❌ {error}</div>;
    }

    const ov = d.overview;
    const cards = ov?.cards;

    return (
        <div className="container mx-auto py-6 px-4 space-y-6 max-w-7xl">
            {/* 标题 + 期间 + 口径 + 导出 */}
            <div className="flex items-center justify-between flex-wrap gap-3">
                <div className="flex items-center gap-2">
                    <Wallet className="h-6 w-6 text-primary" />
                    <h1 className="text-2xl font-bold">财务中心</h1>
                    <span className="text-sm text-muted-foreground">{ov?.label} · {basis === 'cash' ? '现金口径' : '权责口径'}</span>
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                    {/* 口径 */}
                    <div className="flex rounded-lg border border-border bg-card overflow-hidden">
                        {[{ v: 'accrual', l: '权责制' }, { v: 'cash', l: '现金制' }].map(o => (
                            <button key={o.v} onClick={() => changeBasis(o.v)}
                                className={`px-3 py-1.5 text-sm ${basis === o.v ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}>
                                {o.l}
                            </button>
                        ))}
                    </div>
                    {/* 期间 */}
                    <div className="flex rounded-lg border border-border bg-card overflow-hidden">
                        {PERIODS.map(o => (
                            <button key={o.value} onClick={() => changePeriod(o.value)}
                                className={`px-3 py-1.5 text-sm ${period === o.value ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}>
                                {o.label}
                            </button>
                        ))}
                    </div>
                    {period === 'custom' && (
                        <div className="flex items-center gap-1">
                            <input type="date" value={customStart} onChange={e => setCustomStart(e.target.value)}
                                className="px-2 py-1 text-sm rounded border border-border bg-card" />
                            <span className="text-muted-foreground">~</span>
                            <input type="date" value={customEnd} onChange={e => setCustomEnd(e.target.value)}
                                className="px-2 py-1 text-sm rounded border border-border bg-card" />
                        </div>
                    )}
                    <Button variant="outline" size="sm" onClick={exportAudit} disabled={loading}>
                        <Download className="h-4 w-4 mr-1" />导出审计包
                    </Button>
                    <Button variant="outline" size="sm" onClick={fetchAll} disabled={loading}>
                        {loading ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <RefreshCw className="h-4 w-4 mr-1" />}刷新
                    </Button>
                </div>
            </div>

            {/* 口径说明条 */}
            <div className="text-xs text-muted-foreground bg-muted/40 rounded-lg px-3 py-2">
                <strong>权责制</strong>(审计主口径):消费/交付时确认收入,未消费充值计递延负债。
                <strong className="ml-2">现金制</strong>:充值到账即入账。
                赠送/佣金算力消费不计收入(防虚增) · 服务方渠道只认出厂价(玩法B 客户款不过平台)。
            </div>

            {entryNotice && (
                <div className="rounded-lg border border-primary/20 bg-primary/5 px-3 py-2 flex items-center justify-between gap-3">
                    <div className="text-xs text-muted-foreground">
                        <span className="text-foreground font-medium">{entryNotice}</span>
                        <span className="ml-2">已自动应用期间、口径和板块筛选，可继续切换或清除。</span>
                    </div>
                    <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={clearEntryFilter}>清除筛选</Button>
                </div>
            )}

            {/* 总览卡片 */}
            {cards && (
                <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
                    <StatCard icon={<TrendingUp className="h-3.5 w-3.5" />} label="确认收入" value={fmtY(cards.revenue?.value)}
                        delta={<Delta pct={cards.revenue?.delta_pct} goodUp />} highlight />
                    <StatCard icon={<Coins className="h-3.5 w-3.5" />} label="成本 COGS" value={fmtY(cards.cogs?.value)}
                        delta={<Delta pct={cards.cogs?.delta_pct} goodUp={false} />} />
                    <StatCard icon={<TrendingUp className="h-3.5 w-3.5" />} label="毛利" value={fmtY(cards.gross?.value)}
                        delta={<Delta pct={cards.gross?.delta_pct} goodUp />} sub={`毛利率 ${cards.gross?.margin ?? '—'}%`} />
                    <StatCard icon={<Scale className="h-3.5 w-3.5" />} label="递延负债(预收)" value={fmtY(cards.deferred?.value)}
                        sub="未交付预收款" />
                    <StatCard icon={<Banknote className="h-3.5 w-3.5" />} label="本期净现金" value={fmtY(cards.net_cash?.value)}
                        sub="充值−采购−提现−退款" />
                </div>
            )}

            <Tabs value={activeTab} onValueChange={changeTab}>
                <TabsList className="flex-wrap h-auto">
                    <TabsTrigger value="overview">总览</TabsTrigger>
                    <TabsTrigger value="recharges">充值账单</TabsTrigger>
                    <TabsTrigger value="pnl">利润表</TabsTrigger>
                    <TabsTrigger value="balance">负债与现金</TabsTrigger>
                    <TabsTrigger value="cost">成本中心</TabsTrigger>
                    <TabsTrigger value="agent">服务收益结算</TabsTrigger>
                    <TabsTrigger value="audit">对账审计</TabsTrigger>
                </TabsList>

                {/* ===== 总览 ===== */}
                <TabsContent value="overview" className="space-y-4">
                    <div className="grid md:grid-cols-2 gap-4">
                        <div className="rounded-xl border border-border bg-card p-4">
                            <h2 className="font-semibold mb-3">收入构成(权责)</h2>
                            {ov?.revenue_breakdown && (
                                <table className="w-full text-sm">
                                    <tbody>
                                        <Row k="直营消费(付费算力)" v={fmtY(ov.revenue_breakdown.direct_consume)} />
                                        <Row k="服务方渠道(出厂价)" v={fmtY(ov.revenue_breakdown.agent_factory)} />
                                        <Row k="社媒订阅" v={fmtY(ov.revenue_breakdown.subscription)} />
                                        <Row k="退款冲减" v={'-' + fmtY(ov.revenue_breakdown.refund)} />
                                        <Row k="确认收入合计" v={fmtY(ov.revenue_breakdown.confirmed)} bold />
                                    </tbody>
                                </table>
                            )}
                            <div className="text-xs text-muted-foreground mt-2">
                                现金口径收入(充值到账):{fmtY(ov?.cash_revenue)}
                            </div>
                        </div>
                        <div className="rounded-xl border border-border bg-card p-4">
                            <h2 className="font-semibold mb-3">成本构成 COGS</h2>
                            {ov?.cogs_breakdown && (
                                <table className="w-full text-sm">
                                    <tbody>
                                        <Row k="LLM + 监测(AI 引擎)" v={fmtY(ov.cogs_breakdown.llm)} />
                                        <Row k="媒体采购(代发)" v={fmtY(ov.cogs_breakdown.media)} />
                                        <Row k="COGS 合计" v={fmtY(ov.cogs_breakdown.total)} bold />
                                    </tbody>
                                </table>
                            )}
                        </div>
                    </div>
                </TabsContent>

                {/* ===== 充值账单 ===== */}
                <TabsContent value="recharges" className="space-y-4">
                    <div className="rounded-xl border border-border bg-card p-4">
                        <div className="flex items-start justify-between gap-3 mb-4">
                            <div>
                                <h2 className="font-semibold">充值账单</h2>
                                <p className="text-xs text-muted-foreground mt-1">
                                    与「本月营收(现金)」同一口径:已支付充值订单,金额用分保存、前端格式化。
                                </p>
                            </div>
                            <div className="text-right text-xs text-muted-foreground">
                                <div>合计 {fmtCents(d.recharges?.totals?.amount_cents)}</div>
                                <div>{fmtNum(d.recharges?.totals?.points_granted)} 算力</div>
                            </div>
                        </div>
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead className="text-muted-foreground text-xs">
                                    <tr className="border-b border-border">
                                        <th className="text-left py-2">支付时间</th>
                                        <th className="text-left">付款用户</th>
                                        <th className="text-left">资金路径</th>
                                        <th className="text-right">充值金额</th>
                                        <th className="text-right">入账算力</th>
                                        <th className="text-left pl-3">绑定服务商</th>
                                        <th className="text-left">状态</th>
                                        <th className="text-right">操作</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {(d.recharges?.items || []).map((o: any) => (
                                        <tr key={o.id} className="border-b border-border/40 hover:bg-muted/20">
                                            <td className="py-2 text-muted-foreground tabular-nums">{fmtTime(o.paid_at || o.created_at)}</td>
                                            <td>
                                                <button className="text-left hover:text-primary" onClick={() => navigate(`/admin/users?q=${o.user_id}&user_id=${o.user_id}`)}>
                                                    <div className="font-medium">{o.user_display_name}</div>
                                                    <div className="text-[11px] text-muted-foreground">{o.user_role_label} · #{o.user_id}</div>
                                                </button>
                                            </td>
                                            <td><span className="text-xs px-2 py-0.5 rounded-full bg-muted">{o.path_label}</span></td>
                                            <td className="text-right font-mono">{fmtCents(o.amount_cents)}</td>
                                            <td className="text-right font-mono">{fmtNum(o.points_granted)}</td>
                                            <td className="pl-3 text-muted-foreground">{o.agent_display_name || (o.agent_user_id ? `#${o.agent_user_id}` : '—')}</td>
                                            <td>
                                                <div className="flex items-center gap-1.5">
                                                    {o.has_flow_warning ? <AlertTriangle className="h-3.5 w-3.5 text-orange-500" /> : <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />}
                                                    <span className="text-xs">{o.refund_status !== 'none' ? o.refund_status : o.payment_status}</span>
                                                </div>
                                            </td>
                                            <td className="text-right">
                                                <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => openRechargeFlow(o.id)}>
                                                    查看链路
                                                </Button>
                                            </td>
                                        </tr>
                                    ))}
                                    {(!d.recharges?.items || d.recharges.items.length === 0) && (
                                        <tr><td colSpan={8} className="text-center py-10 text-muted-foreground">当前筛选下暂无充值订单</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </TabsContent>

                {/* ===== 利润表 ===== */}
                <TabsContent value="pnl" className="space-y-4">
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">利润表 P&amp;L（{d.pnl?.label} · {basis === 'cash' ? '现金' : '权责'}）</h2>
                        <table className="w-full text-sm">
                            <tbody>
                                <Row k="确认收入" v={fmtY(d.pnl?.revenue)} bold />
                                <Row k="COGS:LLM+监测" v={'-' + fmtY(d.pnl?.cogs?.llm)} indent />
                                <Row k="COGS:媒体采购" v={'-' + fmtY(d.pnl?.cogs?.media)} indent />
                                <Row k="毛利" v={fmtY(d.pnl?.gross)} bold sub={`毛利率 ${d.pnl?.gross_margin ?? '—'}%`} />
                                {(d.pnl?.opex_breakdown || []).map((o: any) => (
                                    <Row key={o.category} k={`OpEx:${o.label}`} v={'-' + fmtY(o.amount)} indent />
                                ))}
                                <Row k="OpEx 合计" v={'-' + fmtY(d.pnl?.opex_total)} indent />
                                <Row k="支付渠道费" v={'-' + fmtY(d.pnl?.gateway_fee)} indent />
                                <Row k="营业利润" v={fmtY(d.pnl?.operating_profit)} bold sub={`营业利润率 ${d.pnl?.operating_margin ?? '—'}%`} />
                            </tbody>
                        </table>
                    </div>

                    {/* 分功能收入 */}
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">分功能收入(付费算力消费 Top 30)</h2>
                        <div className="overflow-x-auto max-h-80 overflow-y-auto">
                            <table className="w-full text-sm">
                                <thead className="text-muted-foreground text-xs sticky top-0 bg-card">
                                    <tr className="border-b border-border"><th className="text-left py-1">功能</th><th className="text-right">收入</th><th className="text-right">次数</th></tr>
                                </thead>
                                <tbody>
                                    {(d.pnl?.revenue_by_feature || []).map((f: any) => (
                                        <tr key={f.feature_code} className="border-b border-border/40">
                                            <td className="py-1 font-mono text-xs">{f.feature_code}</td>
                                            <td className="text-right">{fmtY(f.revenue)}</td>
                                            <td className="text-right text-muted-foreground">{fmtNum(f.count)}</td>
                                        </tr>
                                    ))}
                                    {(!d.pnl?.revenue_by_feature || d.pnl.revenue_by_feature.length === 0) && (
                                        <tr><td colSpan={3} className="text-center py-6 text-muted-foreground">本期暂无付费消费</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>

                    {/* OpEx 录入(D4) */}
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">运营成本录入(服务器/带宽/域名/人力/其它 · 按月)</h2>
                        <div className="flex flex-wrap items-end gap-2 mb-4 pb-4 border-b border-border">
                            <label className="text-xs">归属月
                                <input type="month" value={opexForm.period_month}
                                    onChange={e => setOpexForm({ ...opexForm, period_month: e.target.value })}
                                    className="block mt-0.5 px-2 py-1 text-sm rounded border border-border bg-card" />
                            </label>
                            <label className="text-xs">类别
                                <select value={opexForm.category}
                                    onChange={e => setOpexForm({ ...opexForm, category: e.target.value })}
                                    className="block mt-0.5 px-2 py-1 text-sm rounded border border-border bg-card">
                                    {(d.opex?.categories || []).map((c: any) => <option key={c.value} value={c.value}>{c.label}</option>)}
                                </select>
                            </label>
                            <label className="text-xs">金额(元)
                                <input type="number" value={opexForm.amount_yuan} placeholder="0.00"
                                    onChange={e => setOpexForm({ ...opexForm, amount_yuan: e.target.value })}
                                    className="block mt-0.5 px-2 py-1 text-sm rounded border border-border bg-card w-28" />
                            </label>
                            <label className="text-xs flex-1 min-w-32">备注
                                <input type="text" value={opexForm.note} placeholder="可选"
                                    onChange={e => setOpexForm({ ...opexForm, note: e.target.value })}
                                    className="block mt-0.5 px-2 py-1 text-sm rounded border border-border bg-card w-full" />
                            </label>
                            <Button size="sm" onClick={saveOpex} disabled={savingOpex}>
                                {savingOpex ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Plus className="h-4 w-4 mr-1" />}新增
                            </Button>
                        </div>
                        <div className="overflow-x-auto max-h-72 overflow-y-auto">
                            <table className="w-full text-sm">
                                <thead className="text-muted-foreground text-xs sticky top-0 bg-card">
                                    <tr className="border-b border-border"><th className="text-left py-1">归属月</th><th className="text-left">类别</th><th className="text-right">金额</th><th className="text-left pl-3">备注</th><th></th></tr>
                                </thead>
                                <tbody>
                                    {(d.opex?.items || []).map((o: any) => (
                                        <tr key={o.id} className="border-b border-border/40">
                                            <td className="py-1">{String(o.period_month).slice(0, 7)}</td>
                                            <td>{o.label}</td>
                                            <td className="text-right font-mono">{fmtY(o.amount_yuan)}</td>
                                            <td className="pl-3 text-muted-foreground text-xs">{o.note}</td>
                                            <td className="text-right"><button onClick={() => deleteOpex(o.id)} className="text-muted-foreground hover:text-destructive"><Trash2 className="h-3.5 w-3.5" /></button></td>
                                        </tr>
                                    ))}
                                    {(!d.opex?.items || d.opex.items.length === 0) && (
                                        <tr><td colSpan={5} className="text-center py-6 text-muted-foreground">暂无运营成本记录 · 上方录入</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </TabsContent>

                {/* ===== 负债与现金 ===== */}
                <TabsContent value="balance" className="space-y-4">
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">负债与递延表(时点)</h2>
                        <table className="w-full text-sm">
                            <thead className="text-muted-foreground text-xs"><tr className="border-b border-border"><th className="text-left py-1">科目</th><th className="text-left">性质</th><th className="text-right">金额</th></tr></thead>
                            <tbody>
                                {(d.liabilities?.items || []).map((it: any) => (
                                    <tr key={it.key} className="border-b border-border/40">
                                        <td className="py-1">{it.label}</td>
                                        <td className="text-muted-foreground text-xs">{it.nature}</td>
                                        <td className="text-right font-mono">{fmtY(it.amount)}</td>
                                    </tr>
                                ))}
                                <tr className="font-bold"><td className="py-1.5">负债/代持合计</td><td></td><td className="text-right font-mono">{fmtY(d.liabilities?.total)}</td></tr>
                            </tbody>
                        </table>
                    </div>
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">现金流量表({d.cashflow?.label})</h2>
                        <div className="grid md:grid-cols-2 gap-6">
                            <div>
                                <div className="text-sm font-medium text-green-600 mb-1">现金流入</div>
                                <table className="w-full text-sm">
                                    <tbody>
                                        {(d.cashflow?.inflow || []).map((i: any) => <Row key={i.method} k={`充值·${i.method}`} v={fmtY(i.amount)} />)}
                                        <Row k="流入合计" v={fmtY(d.cashflow?.total_inflow)} bold />
                                    </tbody>
                                </table>
                            </div>
                            <div>
                                <div className="text-sm font-medium text-orange-600 mb-1">现金流出</div>
                                <table className="w-full text-sm">
                                    <tbody>
                                        {(d.cashflow?.outflow || []).map((o: any) => <Row key={o.key} k={o.label} v={fmtY(o.amount)} />)}
                                        <Row k="流出合计" v={fmtY(d.cashflow?.total_outflow)} bold />
                                    </tbody>
                                </table>
                            </div>
                        </div>
                        <div className="mt-3 pt-3 border-t border-border flex justify-between font-bold">
                            <span>净现金流</span><span className="font-mono">{fmtY(d.cashflow?.net_cash_flow)}</span>
                        </div>
                    </div>
                    {/* 单位经济 */}
                    {d.unitEconomics && (
                        <div className="rounded-xl border border-border bg-card p-4">
                            <h2 className="font-semibold mb-3">单位经济</h2>
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                                <div><div className="text-muted-foreground text-xs">ARPU</div><div className="font-bold">{fmtY(d.unitEconomics.arpu)}</div></div>
                                <div><div className="text-muted-foreground text-xs">ARPPU(付费)</div><div className="font-bold">{fmtY(d.unitEconomics.arppu)}</div></div>
                                <div><div className="text-muted-foreground text-xs">活跃/付费用户</div><div className="font-bold">{fmtNum(d.unitEconomics.active_users)} / {fmtNum(d.unitEconomics.paying_users)}</div></div>
                                <div><div className="text-muted-foreground text-xs">复购用户</div><div className="font-bold">{fmtNum(d.unitEconomics.repeat_buyers)}</div></div>
                            </div>
                            <div className="text-xs text-muted-foreground mt-2">{d.unitEconomics.placeholder_note}</div>
                        </div>
                    )}
                </TabsContent>

                {/* ===== 成本中心 ===== */}
                <TabsContent value="cost" className="space-y-4">
                    <div className="grid grid-cols-3 gap-4">
                        <StatCard label="LLM+监测" value={fmtY(d.costCenter?.llm?.total)} sub={`${fmtNum(d.costCenter?.llm?.calls)} 次调用`} />
                        <StatCard label="媒体采购" value={fmtY(d.costCenter?.media?.total)} />
                        <StatCard label="成本总计" value={fmtY(d.costCenter?.grand_total)} highlight />
                    </div>
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">LLM 成本分平台</h2>
                        <table className="w-full text-sm">
                            <thead className="text-muted-foreground text-xs"><tr className="border-b border-border"><th className="text-left py-1">平台</th><th className="text-right">成本</th><th className="text-right">调用</th><th className="text-right">输入Tok</th><th className="text-right">输出Tok</th></tr></thead>
                            <tbody>
                                {(d.costCenter?.llm?.by_platform || []).map((p: any) => (
                                    <tr key={p.platform} className="border-b border-border/40">
                                        <td className="py-1 font-medium">{p.platform}</td>
                                        <td className="text-right font-mono">{fmtY(p.cost)}</td>
                                        <td className="text-right text-muted-foreground">{fmtNum(p.calls)}</td>
                                        <td className="text-right text-muted-foreground">{fmtNum(p.input_tokens)}</td>
                                        <td className="text-right text-muted-foreground">{fmtNum(p.output_tokens)}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                    <div className="grid md:grid-cols-2 gap-4">
                        <div className="rounded-xl border border-border bg-card p-4">
                            <h2 className="font-semibold mb-3">LLM 成本分业务</h2>
                            <table className="w-full text-sm">
                                <tbody>
                                    {(d.costCenter?.llm?.by_caller || []).slice(0, 15).map((c: any) => (
                                        <tr key={c.caller} className="border-b border-border/40"><td className="py-1 text-xs font-mono">{c.caller}</td><td className="text-right font-mono">{fmtY(c.cost)}</td></tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                        <div className="rounded-xl border border-border bg-card p-4">
                            <h2 className="font-semibold mb-3">媒体采购分渠道</h2>
                            <table className="w-full text-sm">
                                <tbody>
                                    {(d.costCenter?.media?.by_outlet || []).map((m: any) => (
                                        <tr key={m.media_name} className="border-b border-border/40"><td className="py-1">{m.media_name}</td><td className="text-right font-mono">{fmtY(m.cost)}</td><td className="text-right text-muted-foreground text-xs">{m.count}篇</td></tr>
                                    ))}
                                    {(!d.costCenter?.media?.by_outlet || d.costCenter.media.by_outlet.length === 0) && (
                                        <tr><td colSpan={3} className="text-center py-6 text-muted-foreground">本期无媒体采购</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </TabsContent>

                {/* ===== 代理结算 ===== */}
                <TabsContent value="agent" className="space-y-4">
                    <StatCard label="应付服务方(已结算待提现)" value={fmtY(d.agentSettlement?.payable_to_agents)} highlight />
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">服务收益台账(4 层 · 分状态)</h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead className="text-muted-foreground text-xs"><tr className="border-b border-border"><th className="text-left py-1">状态</th><th className="text-right">笔数</th><th className="text-right">客户付</th><th className="text-right">出厂价</th><th className="text-right">渠道费</th><th className="text-right">服务费</th><th className="text-right">税</th><th className="text-right">应付服务方</th></tr></thead>
                                <tbody>
                                    {(d.agentSettlement?.pipeline || []).map((p: any) => (
                                        <tr key={p.status} className="border-b border-border/40">
                                            <td className="py-1 font-medium">{p.status}</td>
                                            <td className="text-right">{fmtNum(p.count)}</td>
                                            <td className="text-right font-mono">{fmtY(p.customer_paid)}</td>
                                            <td className="text-right font-mono">{fmtY(p.factory)}</td>
                                            <td className="text-right font-mono">{fmtY(p.gateway_fee)}</td>
                                            <td className="text-right font-mono">{fmtY(p.service_fee)}</td>
                                            <td className="text-right font-mono">{fmtY(p.tax)}</td>
                                            <td className="text-right font-mono">{fmtY(p.settlement)}</td>
                                        </tr>
                                    ))}
                                    {(!d.agentSettlement?.pipeline || d.agentSettlement.pipeline.length === 0) && (
                                        <tr><td colSpan={8} className="text-center py-6 text-muted-foreground">本期无服务方渠道结算</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">提现申请管道</h2>
                        <table className="w-full text-sm">
                            <tbody>
                                {(d.agentSettlement?.withdrawals || []).map((w: any) => (
                                    <tr key={w.status} className="border-b border-border/40"><td className="py-1">{w.status}</td><td className="text-right">{fmtNum(w.count)} 笔</td><td className="text-right font-mono">{fmtY(w.amount)}</td></tr>
                                ))}
                                {(!d.agentSettlement?.withdrawals || d.agentSettlement.withdrawals.length === 0) && (
                                    <tr><td colSpan={3} className="text-center py-6 text-muted-foreground">无提现申请</td></tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                </TabsContent>

                {/* ===== 对账审计 ===== */}
                <TabsContent value="audit" className="space-y-4">
                    <div className="rounded-xl border border-border bg-card p-4">
                        <div className="flex items-center gap-2 mb-3">
                            {d.reconciliation?.has_recent_drift
                                ? <AlertTriangle className="h-4 w-4 text-orange-500" />
                                : <CheckCircle2 className="h-4 w-4 text-green-500" />}
                            <h2 className="font-semibold">库存守恒对账(每日 cron · 最近 10 次)</h2>
                        </div>
                        <table className="w-full text-sm">
                            <thead className="text-muted-foreground text-xs"><tr className="border-b border-border"><th className="text-left py-1">时间</th><th className="text-left">触发</th><th className="text-right">差异(付费)</th><th className="text-right">差异(赠送)</th><th className="text-right">差异(发布)</th><th className="text-center">漂移</th></tr></thead>
                            <tbody>
                                {(d.reconciliation?.latest_runs || []).map((r: any) => (
                                    <tr key={r.id} className="border-b border-border/40">
                                        <td className="py-1 text-xs">{r.run_at ? new Date(r.run_at).toLocaleString('zh-CN') : '—'}</td>
                                        <td className="text-xs text-muted-foreground">{r.triggered_by}</td>
                                        <td className="text-right font-mono">{r.diff_paid}</td>
                                        <td className="text-right font-mono">{r.diff_bonus}</td>
                                        <td className="text-right font-mono">{r.diff_publish}</td>
                                        <td className="text-center">{r.has_drift ? <span className="text-orange-500">⚠</span> : <span className="text-green-500">✓</span>}</td>
                                    </tr>
                                ))}
                                {(!d.reconciliation?.latest_runs || d.reconciliation.latest_runs.length === 0) && (
                                    <tr><td colSpan={6} className="text-center py-6 text-muted-foreground">暂无对账记录</td></tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">异常清单</h2>
                        <div className="grid grid-cols-3 gap-3">
                            {(d.reconciliation?.anomalies || []).map((a: any) => (
                                <div key={a.key} className={`rounded-lg border p-3 ${a.count > 0 ? 'border-orange-500/30 bg-orange-500/5' : 'border-border'}`}>
                                    <div className="text-xs text-muted-foreground">{a.label}</div>
                                    <div className={`text-2xl font-bold ${a.count > 0 ? 'text-orange-500' : ''}`}>{a.count}</div>
                                </div>
                            ))}
                        </div>
                    </div>
                    {/* 逐笔账本钻取(端到端可追溯 · 内部审计可露算力明细 · balance_after 守恒列) */}
                    <div className="rounded-xl border border-border bg-card p-4">
                        <h2 className="font-semibold mb-3">逐笔账本({d.ledger?.label} · 最近 {d.ledger?.items?.length || 0} / 共 {fmtNum(d.ledger?.total)} 笔)</h2>
                        <div className="overflow-x-auto max-h-96 overflow-y-auto">
                            <table className="w-full text-sm">
                                <thead className="text-muted-foreground text-xs sticky top-0 bg-card">
                                    <tr className="border-b border-border">
                                        <th className="text-left py-1">时间</th><th className="text-left">用户</th><th className="text-left">类型</th><th className="text-left">轨道</th>
                                        <th className="text-right">变动(算力)</th><th className="text-right">余额</th><th className="text-left pl-2">功能/说明</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {(d.ledger?.items || []).map((t: any) => (
                                        <tr key={t.id} className="border-b border-border/40">
                                            <td className="py-1 text-xs whitespace-nowrap">{t.created_at ? new Date(t.created_at).toLocaleString('zh-CN') : '—'}</td>
                                            <td className="text-xs">{t.user_id}</td>
                                            <td className="text-xs">{t.type}</td>
                                            <td className="text-xs">{t.point_type}</td>
                                            <td className={`text-right font-mono ${t.amount < 0 ? 'text-orange-500' : 'text-green-600'}`}>{t.amount > 0 ? '+' : ''}{fmtNum(t.amount)}</td>
                                            <td className="text-right font-mono text-muted-foreground">{fmtNum(t.balance_after)}</td>
                                            <td className="pl-2 text-xs text-muted-foreground">{t.feature_code || t.description || '—'}</td>
                                        </tr>
                                    ))}
                                    {(!d.ledger?.items || d.ledger.items.length === 0) && (
                                        <tr><td colSpan={7} className="text-center py-6 text-muted-foreground">本期无算力流水</td></tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </TabsContent>
            </Tabs>
            {(flow || flowLoading) && (
                <RechargeFlowDrawer flow={flow} loading={flowLoading} onClose={() => setFlow(null)} />
            )}
          {confirmDialog}
        </div>
    );
}

function RechargeFlowDrawer({ flow, loading, onClose }: { flow: any; loading: boolean; onClose: () => void }) {
    const order = flow?.order;
    const credit = flow?.customer_credit;
    const ledger = flow?.revenue_ledger || [];
    const creditTxs = flow?.customer_credit_transactions || [];
    const pointTxs = flow?.point_transactions || [];
    return (
        <div className="fixed inset-0 z-50 bg-background/70 backdrop-blur-sm flex justify-end">
            <div className="w-full max-w-2xl h-full bg-card border-l border-border shadow-xl overflow-y-auto">
                <div className="sticky top-0 z-10 bg-card/95 border-b border-border p-4 flex items-start justify-between gap-3">
                    <div>
                        <h2 className="font-semibold text-lg">资金链路</h2>
                        <p className="text-xs text-muted-foreground mt-1">{order?.id || '加载中'}</p>
                    </div>
                    <Button variant="ghost" size="sm" className="h-8 w-8 p-0" onClick={onClose}><X className="h-4 w-4" /></Button>
                </div>
                {loading && !flow ? (
                    <div className="py-20 flex justify-center text-muted-foreground"><Loader2 className="h-5 w-5 animate-spin" /></div>
                ) : (
                    <div className="p-4 space-y-4">
                        <div className="rounded-xl border border-border p-4">
                            <div className="grid grid-cols-2 gap-3 text-sm">
                                <div><div className="text-xs text-muted-foreground">付款用户</div><div className="font-medium">{order?.user_display_name}</div></div>
                                <div><div className="text-xs text-muted-foreground">路径</div><div className="font-medium">{order?.path_label}</div></div>
                                <div><div className="text-xs text-muted-foreground">客户支付</div><div className="font-mono font-bold">{fmtCents(order?.amount_cents)}</div></div>
                                <div><div className="text-xs text-muted-foreground">入账算力</div><div className="font-mono font-bold">{fmtNum(order?.points_granted)}</div></div>
                                <div><div className="text-xs text-muted-foreground">服务商</div><div>{order?.agent_display_name || (order?.agent_user_id ? `#${order.agent_user_id}` : '—')}</div></div>
                                <div><div className="text-xs text-muted-foreground">状态</div><div>{order?.refund_status !== 'none' ? order?.refund_status : order?.payment_status}</div></div>
                            </div>
                        </div>

                        {(flow?.warnings || []).length > 0 && (
                            <div className="rounded-xl border border-orange-500/30 bg-orange-500/5 p-3 space-y-1">
                                {(flow?.warnings || []).map((w: string, i: number) => (
                                    <div key={i} className="flex items-start gap-2 text-sm text-orange-300">
                                        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />{w}
                                    </div>
                                ))}
                            </div>
                        )}

                        <div className="rounded-xl border border-border p-4">
                            <h3 className="font-medium mb-3">链路步骤</h3>
                            <div className="space-y-2">
                                {(flow?.steps || []).map((s: any) => (
                                    <div key={s.key} className="flex items-center justify-between text-sm border-b border-border/30 pb-2 last:border-0 last:pb-0">
                                        <span>{s.label}</span>
                                        <span className="text-muted-foreground">{s.amount_cents != null ? fmtCents(s.amount_cents) : `${s.count ?? 0} 条`}</span>
                                    </div>
                                ))}
                            </div>
                        </div>

                        {credit && (
                            <div className="rounded-xl border border-border p-4">
                                <h3 className="font-medium mb-3">服务方账户算力</h3>
                                <div className="grid grid-cols-2 gap-3 text-sm">
                                    <div><div className="text-xs text-muted-foreground">服务方</div><div>{credit.agent_display_name || `#${credit.agent_user_id}`}</div></div>
                                    <div><div className="text-xs text-muted-foreground">剩余算力</div><div className="font-mono font-bold">{fmtNum((credit.tool_credit_points || 0) + (credit.publish_credit_points || 0) + (credit.bonus_credit_points || 0))}</div></div>
                                    <div><div className="text-xs text-muted-foreground">累计购买</div><div className="font-mono">{fmtNum(credit.total_purchased_points)}</div></div>
                                    <div><div className="text-xs text-muted-foreground">累计消耗</div><div className="font-mono">{fmtNum(credit.total_consumed_points)}</div></div>
                                </div>
                            </div>
                        )}

                        <FlowTable title="普通钱包流水" rows={pointTxs} amountField="amount" />
                        <FlowTable title="服务方算力流水" rows={creditTxs} amountField="points" />
                        <FlowTable title="服务商收益台账" rows={ledger} amountField="agent_settlement_cents" cents />
                    </div>
                )}
            </div>
        </div>
    );
}

function FlowTable({ title, rows, amountField, cents }: { title: string; rows: any[]; amountField: string; cents?: boolean }) {
    return (
        <div className="rounded-xl border border-border p-4">
            <h3 className="font-medium mb-3">{title}</h3>
            {rows.length ? (
                <div className="space-y-0 rounded-lg border border-border/40 overflow-hidden">
                    {rows.map((r, i) => {
                        const amount = Number(r[amountField] || 0);
                        return (
                            <div key={r.id ?? i} className="grid grid-cols-[96px_1fr_auto] gap-2 px-3 py-2 text-xs border-b border-border/30 last:border-0">
                                <span className="text-muted-foreground tabular-nums">{fmtTime(r.created_at)}</span>
                                <span className="min-w-0 truncate">{r.description || r.source || r.type || r.status || '—'}</span>
                                <span className={`font-mono ${amount < 0 ? 'text-red-400' : 'text-green-400'}`}>
                                    {cents ? fmtCents(amount) : `${amount > 0 ? '+' : ''}${fmtNum(amount)}`}
                                </span>
                            </div>
                        );
                    })}
                </div>
            ) : <div className="text-sm text-muted-foreground">暂无记录</div>}
        </div>
    );
}

function Row({ k, v, bold, indent, sub }: any) {
    return (
        <tr className={`border-b border-border/40 ${bold ? 'font-bold' : ''}`}>
            <td className={`py-1.5 ${indent ? 'pl-4 text-muted-foreground' : ''}`}>{k}{sub && <span className="ml-2 text-xs text-muted-foreground font-normal">{sub}</span>}</td>
            <td className="text-right font-mono">{v}</td>
        </tr>
    );
}
