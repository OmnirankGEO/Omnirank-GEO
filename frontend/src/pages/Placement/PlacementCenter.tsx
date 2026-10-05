import { useState, useEffect, useCallback } from 'react';
import { authFetch } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
    Loader2, Database, ShieldCheck, TrendingUp,
    ChevronRight, Newspaper, Link2, ArrowRight, Send
} from 'lucide-react';
import { useIsMobile } from '@/hooks/useIsMobile';
import PlacementRecommendation from '@/pages/Writing/PlacementRecommendation';
import { useClientContext } from '@/context/ClientContext';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface QuoteProject {
    id: number;
    brand_name: string;
    industry: string;
    keyword_count: number;
    total_required_articles: number;
    monthly_price: number;
    writing_status: string;
    confirmed_at: string;
}

interface PlacementStats {
    total_outlets: number;
    geo_confirmed: number;
}

interface TopSource {
    domain: string | null;
    url: string | null;
    title: string;
    count: number;
    platforms: string[];
    keyword_count: number;
    source: string;
}

interface PublishOutcome {
    id?: number;
    snapshot_id?: number;
    publish_status?: string | null;
    publish_url?: string | null;
    inclusion_status?: string | null;
    ai_citations_delta_30d?: number | null;
    monitoring_brand_score_delta_30d?: number | string | null;
    failed_reason?: string | null;
    synced_at?: string | null;
    confirmed_at?: string | null;
    article_ids?: unknown;
    selected_media?: unknown;
    selected_wemedia?: unknown;
    estimated_points?: number | null;
}

interface DecisionSnapshot {
    id: number;
    article_ids?: unknown;
    selected_media?: unknown;
    selected_wemedia?: unknown;
    estimated_points?: number | null;
    confirmed_at?: string | null;
    disclaimer_version?: string | null;
    terms_version?: string | null;
    recommendation_level?: number | null;
}

// Platform display name mapping
const PLATFORM_LABELS: Record<string, string> = {
    dashscope: '千问',
    kimi: 'Kimi',
    deepseek: 'DeepSeek',
    doubao: '豆包',
};

const STATUS_LABELS: Record<string, string> = {
    pending: '待处理',
    reviewing: '审核中',
    published: '已发布',
    rejected: '被拒',
    failed: '失败',
};

function countJsonList(value: unknown): number {
    if (Array.isArray(value)) return value.length;
    if (typeof value !== 'string' || value.trim() === '') return 0;
    try {
        const parsed = JSON.parse(value);
        return Array.isArray(parsed) ? parsed.length : 0;
    } catch {
        return 0;
    }
}

function selectedMediaCount(item: Pick<DecisionSnapshot, 'selected_media' | 'selected_wemedia'>): number {
    return countJsonList(item.selected_media) + countJsonList(item.selected_wemedia);
}

function formatLedgerDate(value?: string | null): string {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value.slice(0, 10);
    return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

function formatCitationDelta(value?: number | null): string {
    const delta = Number(value ?? 0);
    return delta > 0 ? `+${delta}` : `${delta}`;
}

function statusBadgeClass(status?: string | null): string {
    if (status === 'published') return 'bg-emerald-100 text-emerald-700 border-emerald-200 hover:bg-emerald-100';
    if (status === 'failed' || status === 'rejected') return 'bg-red-100 text-red-700 border-red-200 hover:bg-red-100';
    if (status === 'reviewing') return 'bg-amber-100 text-amber-700 border-amber-200 hover:bg-amber-100';
    return 'bg-muted text-muted-foreground border-border hover:bg-muted';
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function PlacementCenter() {
    const { currentBrandId, clientContext } = useClientContext();
    const globalBrandName = clientContext?.brand?.name || '';
    const isMobile = useIsMobile();

    const [projects, setProjects] = useState<QuoteProject[]>([]);
    const [selectedProject, setSelectedProject] = useState<QuoteProject | null>(null);
    const [loading, setLoading] = useState(true);
    const [stats, setStats] = useState<PlacementStats | null>(null);
    const [topSources, setTopSources] = useState<TopSource[]>([]);
    const [loadingSources, setLoadingSources] = useState(false);
    const [publishOutcomes, setPublishOutcomes] = useState<PublishOutcome[]>([]);
    const [decisionSnapshots, setDecisionSnapshots] = useState<DecisionSnapshot[]>([]);
    const [loadingPublishLedger, setLoadingPublishLedger] = useState(false);

    // -------------------------------------------------------------------
    // Data loading
    // -------------------------------------------------------------------

    const loadProjects = useCallback(async () => {
        try {
            let url = '/api/writing/projects';
            if (globalBrandName) {
                url += `?brand_name=${encodeURIComponent(globalBrandName)}`;
            }
            const res = await authFetch(url);
            const data = await res.json();
            const list = data.projects || [];
            setProjects(list);
            // Auto-select first project if none selected
            if (list.length > 0 && !selectedProject) {
                setSelectedProject(list[0]);
            }
        } catch (e) {
            console.error('Failed to load projects:', e);
        } finally {
            setLoading(false);
        }
    }, [globalBrandName]);

    const loadStats = useCallback(async () => {
        try {
            const res = await authFetch('/api/placement/stats');
            if (res.ok) {
                const data = await res.json();
                setStats(data);
            }
        } catch (e) {
            console.error('Failed to load stats:', e);
        }
    }, []);

    const loadTopSources = useCallback(async () => {
        if (!currentBrandId) return;
        setLoadingSources(true);
        try {
            const res = await authFetch(`/api/insights/source-analysis?brand_id=${currentBrandId}&days=90`);
            if (res.ok) {
                const data = await res.json();
                setTopSources(data.data?.top_sources?.slice(0, 10) || []);
            }
        } catch (e) {
            console.error('Failed to load sources:', e);
        } finally {
            setLoadingSources(false);
        }
    }, [currentBrandId]);

    const loadPublishLedger = useCallback(async () => {
        if (!currentBrandId) {
            setPublishOutcomes([]);
            setDecisionSnapshots([]);
            return;
        }

        setLoadingPublishLedger(true);
        try {
            const params = new URLSearchParams({
                brand_id: String(currentBrandId),
                limit: '8',
            });
            const query = params.toString();
            const [outcomesRes, snapshotsRes] = await Promise.all([
                authFetch(`/api/publish/outcomes?${query}`),
                authFetch(`/api/publish/decision-snapshots?${query}`),
            ]);

            if (outcomesRes.ok) {
                const data = await outcomesRes.json();
                setPublishOutcomes(Array.isArray(data.outcomes) ? data.outcomes : []);
            }
            if (snapshotsRes.ok) {
                const data = await snapshotsRes.json();
                setDecisionSnapshots(Array.isArray(data.snapshots) ? data.snapshots : []);
            }
        } catch (e) {
            console.error('Failed to load publish ledger:', e);
        } finally {
            setLoadingPublishLedger(false);
        }
    }, [currentBrandId]);

    useEffect(() => {
        loadProjects();
        loadStats();
    }, [loadProjects, loadStats]);

    useEffect(() => {
        loadTopSources();
    }, [loadTopSources]);

    useEffect(() => {
        loadPublishLedger();
    }, [loadPublishLedger]);

    // -------------------------------------------------------------------
    // Render
    // -------------------------------------------------------------------

    if (loading) {
        return (
            <div className="flex items-center justify-center h-64">
                <Loader2 className="h-8 w-8 animate-spin text-brand" />
            </div>
        );
    }

    const publishLedgerPanel = (
        <Card className="border border-border rounded-xl">
            <CardHeader className="pb-2">
                <CardTitle className="text-sm flex items-center gap-1.5">
                    <ShieldCheck className="h-3.5 w-3.5" />
                    投放台账
                </CardTitle>
                <CardDescription className="text-xs">结果回流与历史快照</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
                <div className="space-y-2">
                    <div className="flex items-center justify-between">
                        <span className="text-xs font-medium text-muted-foreground">投放结果</span>
                        {loadingPublishLedger && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
                    </div>
                    {publishOutcomes.length === 0 ? (
                        <div className="text-xs text-muted-foreground py-2">暂无投放结果</div>
                    ) : (
                        publishOutcomes.slice(0, 4).map((outcome, idx) => {
                            const status = outcome.publish_status || 'pending';
                            return (
                                <div key={outcome.id ?? outcome.snapshot_id ?? idx} className="rounded-lg border bg-muted/20 p-2 space-y-1.5">
                                    <div className="flex items-center justify-between gap-2">
                                        <Badge className={`text-[10px] px-1.5 py-0 leading-4 border ${statusBadgeClass(status)}`}>
                                            {STATUS_LABELS[status] || status}
                                        </Badge>
                                        <span className="text-[10px] text-muted-foreground">{formatLedgerDate(outcome.synced_at || outcome.confirmed_at)}</span>
                                    </div>
                                    <div className="flex items-center justify-between gap-2 text-xs">
                                        <span className="text-muted-foreground">30天引用变化</span>
                                        <span className="font-medium tabular-nums">{formatCitationDelta(outcome.ai_citations_delta_30d)}</span>
                                    </div>
                                    {outcome.publish_url && (
                                        <a
                                            href={outcome.publish_url}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="text-xs text-primary hover:underline flex items-center gap-1 min-w-0"
                                        >
                                            <Link2 className="h-3 w-3 shrink-0" />
                                            <span className="truncate">发布URL</span>
                                        </a>
                                    )}
                                    {outcome.failed_reason && (
                                        <div className="text-xs text-red-600 truncate" title={outcome.failed_reason}>
                                            失败原因: {outcome.failed_reason}
                                        </div>
                                    )}
                                </div>
                            );
                        })
                    )}
                </div>

                <div className="space-y-2 border-t pt-3">
                    <div className="flex items-center justify-between">
                        <span className="text-xs font-medium text-muted-foreground">历史投放快照</span>
                        <span className="text-[10px] text-muted-foreground">近3个月可回查</span>
                    </div>
                    {decisionSnapshots.length === 0 ? (
                        <div className="text-xs text-muted-foreground py-2">暂无确认快照</div>
                    ) : (
                        decisionSnapshots.slice(0, 4).map(snapshot => (
                            <div key={snapshot.id} className="rounded-lg border bg-muted/20 p-2 space-y-1">
                                <div className="flex items-center justify-between gap-2">
                                    <span className="text-xs font-medium truncate">快照 #{snapshot.id}</span>
                                    <span className="text-[10px] text-muted-foreground">{formatLedgerDate(snapshot.confirmed_at)}</span>
                                </div>
                                <div className="flex items-center gap-2 text-[11px] text-muted-foreground flex-wrap">
                                    <span>{countJsonList(snapshot.article_ids)}篇文章</span>
                                    <span>{selectedMediaCount(snapshot)}家媒体</span>
                                    {snapshot.estimated_points != null && <span>{snapshot.estimated_points}点</span>}
                                </div>
                                <div className="text-[10px] text-muted-foreground truncate">
                                    {snapshot.terms_version || 'terms'} / {snapshot.disclaimer_version || 'disclaimer'}
                                </div>
                            </div>
                        ))
                    )}
                </div>
            </CardContent>
        </Card>
    );

    return (
        <div className="space-y-4 md:space-y-6 p-4 sm:p-6">
            {/* Page header */}
            <div className="flex items-center gap-3">
                <div className="h-9 w-9 rounded-xl bg-brand/10 flex items-center justify-center shrink-0">
                    <Send className="h-4 w-4 text-brand" />
                </div>
                <div className="min-w-0">
                    <h1 className="text-lg md:text-2xl font-bold text-foreground">投放管理中心</h1>
                    <p className="text-xs md:text-sm text-muted-foreground truncate">AI 根据文章内容、行业样本和当前媒体资源给出优先投放建议</p>
                </div>
            </div>

            {/* Overview cards — 2x2 on mobile */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {[
                    { icon: Database, color: 'text-blue-500', bg: 'bg-blue-500/10', value: stats?.total_outlets?.toLocaleString() ?? '—', label: '媒体资源' },
                    { icon: ShieldCheck, color: 'text-emerald-500', bg: 'bg-emerald-500/10', value: stats?.geo_confirmed ?? 0, label: 'GEO 收录' },
                    { icon: Newspaper, color: 'text-orange-500', bg: 'bg-orange-500/10', value: projects.length, label: '进行中项目' },
                    { icon: Link2, color: 'text-purple-500', bg: 'bg-purple-500/10', value: topSources.length > 0 ? topSources.length + '+' : '—', label: '监测信源' },
                ].map(({ icon: Icon, color, bg, value, label }) => (
                    <Card key={label} className="border border-border rounded-xl">
                        <CardContent className="p-3 md:p-5">
                            <div className="flex items-center gap-2.5">
                                <div className={`h-8 w-8 md:h-10 md:w-10 rounded-lg flex items-center justify-center shrink-0 ${bg}`}>
                                    <Icon className={`h-4 w-4 md:h-5 md:w-5 ${color}`} />
                                </div>
                                <div className="min-w-0">
                                    <p className="text-lg md:text-2xl font-bold tabular-nums leading-tight">{value}</p>
                                    <p className="text-[10px] md:text-xs text-muted-foreground">{label}</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                ))}
            </div>

            {/* Mobile: project selector as dropdown */}
            {isMobile && (
                <div className="space-y-2">
                    <label className="text-sm font-medium text-muted-foreground">选择项目</label>
                    <select
                        value={selectedProject?.id || ''}
                        onChange={e => {
                            const p = projects.find(proj => proj.id === Number(e.target.value));
                            setSelectedProject(p || null);
                        }}
                        className="w-full px-3 py-2.5 rounded-lg border border-border bg-card text-sm"
                    >
                        <option value="">选择报价单项目</option>
                        {projects.map(proj => (
                            <option key={proj.id} value={proj.id}>
                                {proj.brand_name} ({proj.keyword_count}词 / {proj.total_required_articles}篇)
                            </option>
                        ))}
                    </select>
                </div>
            )}

            {isMobile && publishLedgerPanel}

            {/* Main layout: side-by-side on desktop, stacked on mobile */}
            <div className="flex flex-col md:grid md:grid-cols-12 gap-4 md:gap-5">
                {/* LEFT: Project selector + top sources (desktop only) */}
                {!isMobile && (
                    <div className="col-span-3 space-y-4">
                        <div>
                            <h3 className="text-sm font-medium text-muted-foreground px-1 mb-3">选择项目</h3>
                            {projects.length === 0 ? (
                                <Card className="border border-border rounded-xl">
                                    <CardContent className="py-8 text-center text-sm text-muted-foreground">
                                        暂无已确认的报价单
                                    </CardContent>
                                </Card>
                            ) : (
                                <div className="space-y-2">
                                    {projects.map(proj => {
                                        const isActive = selectedProject?.id === proj.id;
                                        return (
                                            <div
                                                key={proj.id}
                                                onClick={() => setSelectedProject(proj)}
                                                className={`flex items-center gap-3 px-4 py-3 rounded-lg cursor-pointer transition-all ${
                                                    isActive
                                                        ? 'bg-primary text-primary-foreground shadow-xs'
                                                        : 'bg-card hover:bg-muted/60 border'
                                                }`}
                                            >
                                                <div className="flex-1 min-w-0">
                                                    <div className="text-sm font-medium truncate">{proj.brand_name}</div>
                                                    <div className={`text-xs mt-0.5 ${isActive ? 'text-primary-foreground/70' : 'text-muted-foreground'}`}>
                                                        {proj.keyword_count}词 / {proj.total_required_articles}篇
                                                    </div>
                                                </div>
                                                <ChevronRight className={`h-4 w-4 shrink-0 ${isActive ? 'text-primary-foreground/70' : 'text-muted-foreground/40'}`} />
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>

                        {topSources.length > 0 && (
                            <Card className="border border-border rounded-xl">
                                <CardHeader className="pb-2">
                                    <CardTitle className="text-sm flex items-center gap-1.5">
                                        <TrendingUp className="h-3.5 w-3.5" />
                                        AI 高频引用信源
                                    </CardTitle>
                                    <CardDescription className="text-xs">近90天监测数据</CardDescription>
                                </CardHeader>
                                <CardContent className="space-y-1.5">
                                    {topSources.map((src, idx) => (
                                        <div key={idx} className="flex items-center gap-2 text-xs py-1">
                                            <span className="text-muted-foreground tabular-nums w-4 shrink-0">{idx + 1}</span>
                                            <span className="flex-1 truncate font-medium" title={src.domain || src.title}>
                                                {src.domain || src.title}
                                            </span>
                                            <div className="flex items-center gap-1 shrink-0">
                                                {src.platforms.slice(0, 2).map(p => (
                                                    <Badge key={p} variant="outline" className="text-[9px] px-1 py-0 leading-tight">
                                                        {PLATFORM_LABELS[p] || p}
                                                    </Badge>
                                                ))}
                                            </div>
                                            <span className="text-muted-foreground tabular-nums shrink-0">{src.count}次</span>
                                        </div>
                                    ))}
                                    <div className="pt-2 border-t mt-2">
                                        <a href="/monitoring" className="text-xs text-primary hover:underline flex items-center gap-1">
                                            查看完整信源分析 <ArrowRight className="h-3 w-3" />
                                        </a>
                                    </div>
                                </CardContent>
                            </Card>
                        )}

                        {publishLedgerPanel}
                    </div>
                )}

                {/* RIGHT (or full-width on mobile): Placement recommendation */}
                <div className="md:col-span-9">
                    {!selectedProject ? (
                        <Card className="border border-border rounded-xl">
                            <CardContent className="py-12 md:py-20 text-center">
                                <Newspaper className="h-10 w-10 text-muted-foreground/30 mx-auto mb-3" />
                                <p className="text-sm text-muted-foreground">
                                    {isMobile ? '请从上方选择项目' : '从左侧选择报价单项目，查看投放建议'}
                                </p>
                            </CardContent>
                        </Card>
                    ) : (
                        <PlacementRecommendation
                            key={selectedProject.id}
                            quoteId={selectedProject.id}
                            brandName={selectedProject.brand_name}
                        />
                    )}
                </div>
            </div>
        </div>
    );
}
