import { useState, useEffect } from 'react';
import { useSimulatedThinking } from '@/hooks/useSimulatedThinking';
import { SimulatedThinking } from '@/components/SimulatedThinking';
import { authFetch } from '@/lib/api';
import { useClientContext } from '@/context/ClientContext';
import {
    BarChart3, TrendingUp, Link2, Target, Sparkles, Loader2,
    Brain, ChevronDown, ThumbsUp, ThumbsDown, Globe, Clock
} from 'lucide-react';

// =============================================
// 类型 V4.2
// =============================================
interface CompetitorBrand {
    brand_name: string;
    mention_count: number;
    share_pct: number;
    avg_rank: number | null;
    top1_count: number;
    top3_count: number;
    platform_count?: number;
}

interface SourceItem {
    url: string | null;
    domain?: string | null;
    title?: string;
    content_type?: string;
    count: number;
    platforms: string[];
    keyword_count?: number;
    source?: string;
}

interface ROIData {
    baseline: { total_keywords: number; mentioned_count: number; detection_rate: number; avg_best_rank: number | null };
    current: { total_keywords: number; mentioned_count: number; detection_rate: number; avg_best_rank: number | null };
    delta: { detection_rate: number; avg_best_rank?: number };
}

interface KeywordInsight {
    id: number;
    keyword: string;
    platforms_analyzed: string[];
    brands_found: any[];
    client_position: {
        mentioned_in: number;
        total_platforms: number;
        best_rank: number | null;
        worst_rank: number | null;
        cited_strengths: string[];
        gaps_vs_competitors: string[];
    };
    optimization_hints: string[];
    quality_flag: string;
    distilled_at: string;
}

// =============================================
// 主组件
// =============================================
interface InsightsCenterProps {
    brandId?: number | null;
    brandName?: string | null;
}

export default function InsightsCenter({ brandId: providedBrandId, brandName }: InsightsCenterProps = {}) {
    const { currentBrandId } = useClientContext();
    const [activeTab, setActiveTab] = useState<'radar' | 'patterns' | 'sources' | 'roi'>('radar');
    const brandId = providedBrandId ?? currentBrandId ?? null;
    const [days, setDays] = useState(30);
    const [loading, setLoading] = useState(false);

    // 数据
    const [radarData, setRadarData] = useState<{ brands: CompetitorBrand[]; total_mentions: number; total_keywords: number } | null>(null);
    const [patternsData, setPatternsData] = useState<any>(null);
    const [sourcesData, setSourcesData] = useState<{ top_sources: SourceItem[]; total_unique_sources: number } | null>(null);
    const [roiData, setRoiData] = useState<ROIData | null>(null);
    const [aiSummary, setAiSummary] = useState<string>('');
    const [synthesizing, setSynthesizing] = useState(false);
    const { thinkingSteps: insightThinking, isThinking: isInsightThinking } = useSimulatedThinking(synthesizing, {
      steps: ['正在分析监测数据...', '识别关键趋势...', '生成 AI 洞察...'],
    });

    const [error, setError] = useState<string>('');

    useEffect(() => {
        if (!brandId) return;
        loadTabData(activeTab);
    }, [activeTab, brandId, days]);

    async function loadTabData(tab: string) {
        if (!brandId) {
            setError('请先在监测中心选择一个客户，再查看数据洞察。');
            return;
        }
        setLoading(true);
        setError('');
        try {
            const endpoints: Record<string, string> = {
                radar: `/api/insights/competitor-radar?brand_id=${brandId}&days=${days}`,
                patterns: `/api/insights/success-patterns?brand_id=${brandId}&days=${days}`,
                sources: `/api/insights/source-analysis?brand_id=${brandId}&days=${days}`,
                roi: `/api/insights/roi-verification?brand_id=${brandId}`,
            };

            const url = endpoints[tab];
            if (!url) return;

            const res = await authFetch(url);

            if (!res.ok) {
                if (res.status === 401 || res.status === 403) {
                    setError('这个客户的数据洞察暂时没有权限查看。请选择你负责的客户，或让管理员确认客户归属。');
                } else if (res.status === 404) {
                    setError('这个客户还没有可分析的监测数据。先跑一次监测，数据沉淀后再看洞察。');
                } else {
                    setError('这份洞察暂时没拿到。监测主流程不受影响，可以稍后再试。');
                }
                setLoading(false);
                return;
            }

            const json = await res.json();
            if (json.status === 'success') {
                switch (tab) {
                    case 'radar': setRadarData(json.data); break;
                    case 'patterns': setPatternsData(json.data); break;
                    case 'sources': setSourcesData(json.data); break;
                    case 'roi': setRoiData(json.data); break;
                }
            }
        } catch (err) {
            console.error('加载数据失败:', err);
            setError('网络暂时不稳定，这份洞察没有加载出来。监测主流程不受影响。');
        }
        setLoading(false);
    }

    async function handleSynthesize() {
        if (!brandId) {
            setError('请先选择客户，再生成 AI 洞察。');
            return;
        }
        setSynthesizing(true);
        try {
            const res = await authFetch(`/api/insights/synthesize?brand_id=${brandId}&days=${days}`, { method: 'POST' });
            const json = await res.json();
            if (json.status === 'success') {
                setAiSummary(json.data.summary_text || '');
            }
        } catch (err) {
            console.error('AI合成失败:', err);
            setAiSummary('AI 洞察这次没有生成成功，请稍后重试。');
        }
        setSynthesizing(false);
    }

    const tabs = [
        { id: 'radar' as const, label: '竞品雷达', icon: BarChart3 },
        { id: 'patterns' as const, label: '成功模式', icon: TrendingUp },
        { id: 'sources' as const, label: '信源分析', icon: Link2 },
        { id: 'roi' as const, label: '效果验证', icon: Target },
    ];

    return (
        <div className="space-y-6">
            {/* 页头 */}
            <div>
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                    <div className="flex items-center gap-2 sm:gap-4">
                        <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center">
                            <Brain className="h-5 w-5 text-brand" />
                        </div>
                        <div>
                            <h1 className="text-2xl font-bold text-foreground">
                                数据洞察中心
                            </h1>
                            <p className="text-sm text-muted-foreground mt-1">
                                {brandName ? `${brandName} 的 AI 搜索竞争情报` : '先选择客户，再查看 AI 搜索竞争情报'}
                            </p>
                        </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 sm:gap-3">
                        {/* 天数选择 */}
                        <div className="relative">
                            <select
                                value={days}
                                onChange={(e) => setDays(Number(e.target.value))}
                                className="appearance-none bg-card text-foreground text-sm px-3 py-2 pr-8 rounded-lg border border-border focus:border-brand focus:ring-2 focus:ring-brand/20 focus:outline-hidden"
                            >
                                <option value={7}>最近 7 天</option>
                                <option value={14}>最近 14 天</option>
                                <option value={30}>最近 30 天</option>
                                <option value={60}>最近 60 天</option>
                                <option value={90}>最近 90 天</option>
                            </select>
                            <ChevronDown className="absolute right-2 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
                        </div>
                        {/* AI 洞察按钮 */}
                        <button
                            onClick={handleSynthesize}
                            disabled={synthesizing}
                            className="flex items-center gap-2 px-4 py-2 bg-linear-to-r from-indigo-500 to-purple-500 text-white text-sm font-medium rounded-lg shadow-xs hover:shadow-md hover:opacity-95 transition disabled:opacity-50"
                        >
                            {synthesizing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                            {synthesizing ? '生成中...' : 'AI 洞察'}
                        </button>
                    </div>
                </div>

                {isInsightThinking && (
                    <SimulatedThinking steps={insightThinking} isVisible={true} className="mt-3" waitScene="research" />
                )}

                {/* AI 摘要面板 */}
                {aiSummary && (
                    <div className="mt-4 p-4 bg-linear-to-r from-indigo-50 to-purple-50 border border-indigo-100 rounded-xl">
                        <div className="flex items-center gap-2 mb-2">
                            <Sparkles className="h-4 w-4 text-indigo-500" />
                            <span className="text-sm font-semibold text-indigo-700">AI 洞察摘要</span>
                        </div>
                        <div className="text-sm text-foreground whitespace-pre-line leading-relaxed">
                            {aiSummary}
                        </div>
                    </div>
                )}
            </div>

            {/* Tab 切换 */}
            <div className="flex gap-1 bg-muted p-1 rounded-xl w-fit">
                {tabs.map(tab => {
                    const Icon = tab.icon;
                    return (
                        <button
                            key={tab.id}
                            onClick={() => setActiveTab(tab.id)}
                            className={`flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium transition-all ${activeTab === tab.id
                                ? 'bg-card text-brand shadow-xs'
                                : 'text-muted-foreground hover:text-foreground hover:bg-card/50'
                                }`}
                        >
                            <Icon className="h-4 w-4" />
                            {tab.label}
                        </button>
                    );
                })}
            </div>

            {/* 内容区 */}
            {!brandId ? (
                <div className="flex flex-col items-center justify-center h-64 bg-card border border-border rounded-xl px-6 text-center">
                    <Clock className="h-8 w-8 text-muted-foreground mb-3" />
                    <p className="text-sm font-medium text-foreground mb-2">先选择一个客户</p>
                    <p className="text-xs text-muted-foreground max-w-md">
                        数据洞察会按客户读取监测记录。请选择你负责的客户，避免误看其他客户数据。
                    </p>
                </div>
            ) : loading ? (
                <div className="flex items-center justify-center h-64">
                    <Loader2 className="h-8 w-8 animate-spin text-brand" />
                </div>
            ) : error ? (
                <div className="flex flex-col items-center justify-center h-64 bg-card border border-red-200 rounded-xl">
                    <div className="text-4xl mb-4">⚠️</div>
                    <p className="text-sm text-red-600 font-medium mb-2">数据加载失败</p>
                    <p className="text-xs text-muted-foreground max-w-md text-center">{error}</p>
                    <button
                        onClick={() => loadTabData(activeTab)}
                        className="mt-4 px-4 py-2 bg-brand text-white text-sm rounded-lg hover:bg-brand/90 transition"
                    >
                        重试
                    </button>
                </div>
            ) : (
                <div className="animate-in fade-in duration-300">
                    {activeTab === 'radar' && (radarData ? <CompetitorRadarPanel data={radarData} /> : <EmptyState />)}
                    {activeTab === 'patterns' && (patternsData ? <SuccessPatternsPanel data={patternsData} brandId={brandId} days={days} /> : <EmptyState />)}
                    {activeTab === 'sources' && (sourcesData ? <SourceAnalysisPanel data={sourcesData} /> : <EmptyState />)}
                    {activeTab === 'roi' && (roiData ? <ROIVerificationPanel data={roiData} /> : <EmptyState />)}
                </div>
            )}
        </div>
    );
}

// =============================================
// 子面板：竞品雷达
// =============================================
function CompetitorRadarPanel({ data }: { data: { brands: CompetitorBrand[]; total_mentions: number; total_keywords?: number } }) {
    const cleanBrands = data.brands.filter(b => {
        const noise = ['核心优势', '--', '有限公司', '综合评分', '服务内容', '注意事项', '主要优势', '总结'];
        return !noise.includes(b.brand_name) && b.brand_name.length >= 2 && b.brand_name.length <= 20;
    });

    const maxMention = Math.max(...cleanBrands.map(b => b.mention_count), 1);

    return (
        <div className="space-y-4">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
                <StatCard label="总提及数" value={data.total_mentions} icon={BarChart3} color="indigo" />
                <StatCard label="识别品牌" value={cleanBrands.length} icon={TrendingUp} color="blue" />
                <StatCard label="覆盖关键词" value={data.total_keywords || 0} icon={Globe} color="emerald" />
                <StatCard label="Top 1 品牌" value={cleanBrands[0]?.brand_name || '-'} icon={Target} color="amber" isText />
            </div>

            <div className="bg-card border border-border rounded-xl p-5">
                <h3 className="text-base font-semibold text-foreground mb-4">心智份额排名</h3>
                <div className="space-y-3">
                    {cleanBrands.slice(0, 15).map((brand, i) => (
                        <div key={i} className="flex items-center gap-3">
                            <span className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold ${i < 3 ? 'bg-indigo-500 text-white' : 'bg-muted text-muted-foreground'
                                }`}>
                                {i + 1}
                            </span>
                            <span className="w-32 text-sm text-foreground truncate font-medium">{brand.brand_name}</span>
                            <div className="flex-1 h-6 bg-muted rounded-full overflow-hidden">
                                <div
                                    className={`h-full rounded-full transition-all duration-500 ${i === 0 ? 'bg-linear-to-r from-indigo-500 to-indigo-400' :
                                        i === 1 ? 'bg-linear-to-r from-blue-500 to-blue-400' :
                                            i === 2 ? 'bg-linear-to-r from-cyan-500 to-cyan-400' :
                                                'bg-muted-foreground/40'
                                        }`}
                                    style={{ width: `${(brand.mention_count / maxMention) * 100}%` }}
                                />
                            </div>
                            <span className="w-20 text-right text-sm text-foreground">
                                {brand.share_pct}%
                                <span className="text-muted-foreground"> ({brand.mention_count})</span>
                            </span>
                            {brand.avg_rank && (
                                <span className="w-16 text-right text-xs text-muted-foreground">
                                    均排 {brand.avg_rank}
                                </span>
                            )}
                            {brand.platform_count !== undefined && (
                                <span className="w-12 text-right text-xs text-muted-foreground">
                                    {brand.platform_count}平台
                                </span>
                            )}
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}

// =============================================
// 子面板：成功模式 V4.2（BUG-1 + ISSUE-2 修复版）
// =============================================
function SuccessPatternsPanel({ data, brandId, days }: { data: any; brandId: number; days: number }) {
    const rankDist = data.rank_distribution || {};
    const platformStats = data.platform_stats || {};
    const structureSummary = data.structure_summary || {};
    const [keywordInsights, setKeywordInsights] = useState<KeywordInsight[]>([]);
    const [trendKeyword, setTrendKeyword] = useState<string | null>(null);
    const [trendData, setTrendData] = useState<any[]>([]);
    const [trendLoading, setTrendLoading] = useState(false);

    // 加载最新关键词蒸馏列表（BUG-1 修复：使用正确的 API）
    useEffect(() => {
        loadKeywordInsights();
    }, [brandId, days]);

    async function loadKeywordInsights() {
        try {
            const res = await authFetch(`/api/insights/keyword-insights-list?brand_id=${brandId}&days=${days}&limit=100`);
            const json = await res.json();
            if (json.status === 'success' && json.data?.insights) {
                setKeywordInsights(json.data.insights);
            }
        } catch (err) {
            console.error('加载关键词蒸馏列表失败:', err);
        }
    }

    // ISSUE-2 修复：加载关键词时序趋势
    async function loadKeywordTrend(keyword: string) {
        if (trendKeyword === keyword) {
            setTrendKeyword(null);
            return;
        }
        setTrendKeyword(keyword);
        setTrendLoading(true);
        try {
            const res = await authFetch(`/api/insights/keyword-trend?brand_id=${brandId}&keyword=${encodeURIComponent(keyword)}&days=90`);
            const json = await res.json();
            if (json.status === 'success') {
                setTrendData(json.data.trend || []);
            }
        } catch (err) {
            console.error('加载趋势失败:', err);
            setTrendData([]);
        }
        setTrendLoading(false);
    }

    async function handleFeedback(insightId: number, flag: 'verified' | 'rejected') {
        try {
            const res = await authFetch('/api/insights/feedback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ insight_id: insightId, quality_flag: flag }),
            });
            const json = await res.json();
            if (json.status === 'success') {
                setKeywordInsights(prev =>
                    prev.map(ki => ki.id === insightId ? { ...ki, quality_flag: flag } : ki)
                );
            }
        } catch (err) {
            console.error('反馈提交失败:', err);
        }
    }

    return (
        <div className="space-y-4">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
                <StatCard label="分析关键词" value={data.total_insights || 0} icon={BarChart3} color="indigo" />
                <StatCard label="Top 1 次数" value={rankDist.top1 || 0} icon={Target} color="emerald" />
                <StatCard label="Top 3 次数" value={rankDist.top3 || 0} icon={TrendingUp} color="blue" />
                <StatCard label="未出现" value={rankDist.absent || 0} icon={Clock} color="slate" />
            </div>

            <div className="grid grid-cols-2 gap-4">
                {/* 平台表现对比 */}
                <div className="bg-card border border-border rounded-xl p-5">
                    <h3 className="text-base font-semibold text-foreground mb-4">平台表现对比</h3>
                    <div className="space-y-2">
                        {Object.entries(platformStats).map(([platform, stats]: [string, any]) => (
                            <div key={platform} className="flex flex-wrap items-center justify-between p-3 bg-muted rounded-lg gap-2">
                                <span className="text-sm font-medium text-foreground capitalize">{platform}</span>
                                <div className="flex flex-wrap items-center gap-2 sm:gap-4 text-sm">
                                    <span className="text-muted-foreground">覆盖 {stats.total}</span>
                                    <span className={`font-semibold ${stats.detection_rate >= 50 ? 'text-emerald-600' : 'text-amber-600'}`}>
                                        检出 {stats.detection_rate}%
                                    </span>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>

                {/* 回复结构偏好 */}
                <div className="bg-card border border-border rounded-xl p-5">
                    <h3 className="text-base font-semibold text-foreground mb-4">回复结构偏好</h3>
                    {Object.keys(structureSummary).length === 0 ? (
                        <p className="text-muted-foreground text-sm py-4 text-center">暂无结构分析数据</p>
                    ) : (
                        <div className="space-y-2">
                            {Object.entries(structureSummary).map(([platform, descs]: [string, any]) => (
                                <div key={platform} className="p-3 bg-muted rounded-lg">
                                    <span className="text-sm font-medium text-brand capitalize">{platform}</span>
                                    <div className="mt-1 space-y-1">
                                        {(descs as string[]).map((desc: string, i: number) => (
                                            <p key={i} className="text-xs text-muted-foreground">• {desc}</p>
                                        ))}
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>

            {/* 关键词蒸馏详情（BUG-1 修复：始终渲染） */}
            <div className="bg-card border border-border rounded-xl p-5">
                <h3 className="text-base font-semibold text-foreground mb-4">
                    关键词蒸馏详情
                    <span className="text-sm font-normal text-muted-foreground ml-2">
                        共 {keywordInsights.length} 条 · 点击关键词查看趋势
                    </span>
                </h3>
                {keywordInsights.length === 0 ? (
                    <p className="text-muted-foreground text-sm py-8 text-center">暂无蒸馏数据，执行监测任务后会自动生成</p>
                ) : (
                    <div className="space-y-2">
                        {keywordInsights.map((ki) => (
                            <div key={ki.id}>
                                <div className="p-3 bg-muted rounded-lg hover:bg-muted/80 transition cursor-pointer"
                                    onClick={() => loadKeywordTrend(ki.keyword)}>
                                    <div className="flex items-center justify-between">
                                        <div className="flex items-center gap-2">
                                            <TrendingUp className={`h-3.5 w-3.5 ${trendKeyword === ki.keyword ? 'text-brand' : 'text-muted-foreground/30'}`} />
                                            <span className="text-sm font-medium text-foreground">{ki.keyword}</span>
                                            <span className={`px-2 py-0.5 text-xs rounded-full ${ki.quality_flag === 'verified' ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' :
                                                ki.quality_flag === 'rejected' ? 'bg-red-50 text-red-700 border border-red-200' :
                                                    ki.quality_flag === 'degraded' ? 'bg-amber-50 text-amber-700 border border-amber-200' :
                                                        'bg-muted text-muted-foreground border border-border'
                                                }`}>
                                                {ki.quality_flag === 'verified' ? '✅ 已验证' :
                                                    ki.quality_flag === 'rejected' ? '❌ 已拒绝' :
                                                        ki.quality_flag === 'degraded' ? '⚠️ 降级' :
                                                            '🤖 自动'}
                                            </span>
                                        </div>
                                        <div className="flex items-center gap-2">
                                            <span className="text-xs text-muted-foreground">
                                                {ki.brands_found?.length || 0} 品牌 · {ki.platforms_analyzed?.length || 0} 平台
                                            </span>
                                            <button
                                                onClick={(e) => { e.stopPropagation(); handleFeedback(ki.id, 'verified'); }}
                                                className="p-1 text-muted-foreground hover:text-emerald-500 transition"
                                                title="标记为准确"
                                            >
                                                <ThumbsUp className="h-3.5 w-3.5" />
                                            </button>
                                            <button
                                                onClick={(e) => { e.stopPropagation(); handleFeedback(ki.id, 'rejected'); }}
                                                className="p-1 text-muted-foreground hover:text-red-500 transition"
                                                title="标记为不准确"
                                            >
                                                <ThumbsDown className="h-3.5 w-3.5" />
                                            </button>
                                        </div>
                                    </div>
                                    {ki.optimization_hints?.length > 0 && (
                                        <div className="mt-2 space-y-1">
                                            {ki.optimization_hints.slice(0, 2).map((hint, i) => (
                                                <p key={i} className="text-xs text-muted-foreground">💡 {hint}</p>
                                            ))}
                                        </div>
                                    )}
                                </div>

                                {/* ISSUE-2 修复：关键词时序趋势展开面板 */}
                                {trendKeyword === ki.keyword && (
                                    <div className="ml-6 mt-1 mb-2 p-4 bg-indigo-50 border border-indigo-100 rounded-lg">
                                        <div className="flex items-center gap-2 mb-3">
                                            <TrendingUp className="h-4 w-4 text-indigo-500" />
                                            <span className="text-sm font-semibold text-indigo-700">
                                                「{ki.keyword}」90天趋势
                                            </span>
                                        </div>
                                        {trendLoading ? (
                                            <div className="flex items-center gap-2 py-4 justify-center">
                                                <Loader2 className="h-4 w-4 animate-spin text-indigo-400" />
                                                <span className="text-sm text-muted-foreground">加载趋势...</span>
                                            </div>
                                        ) : trendData.length === 0 ? (
                                            <p className="text-sm text-muted-foreground text-center py-4">暂无历史趋势数据（需监测≥2次）</p>
                                        ) : (
                                            <div className="space-y-2">
                                                <table className="w-full text-xs">
                                                    <thead>
                                                        <tr className="border-b border-indigo-200">
                                                            <th className="text-left text-indigo-600 pb-2 font-medium">日期</th>
                                                            <th className="text-right text-indigo-600 pb-2 font-medium">最佳排名</th>
                                                            <th className="text-right text-indigo-600 pb-2 font-medium">提及平台</th>
                                                            <th className="text-right text-indigo-600 pb-2 font-medium">平台数</th>
                                                        </tr>
                                                    </thead>
                                                    <tbody>
                                                        {trendData.map((t: any, idx: number) => {
                                                            const cp = t.client_position || {};
                                                            return (
                                                                <tr key={idx} className="border-b border-indigo-50">
                                                                    <td className="py-1.5 text-foreground">
                                                                        {t.distilled_at ? new Date(t.distilled_at).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' }) : '-'}
                                                                    </td>
                                                                    <td className="text-right">
                                                                        <span className={`font-semibold ${cp.best_rank === 1 ? 'text-emerald-600' :
                                                                                cp.best_rank && cp.best_rank <= 3 ? 'text-blue-600' :
                                                                                    cp.best_rank ? 'text-foreground' : 'text-muted-foreground/30'
                                                                            }`}>
                                                                            {cp.best_rank || '-'}
                                                                        </span>
                                                                    </td>
                                                                    <td className="text-right text-muted-foreground">{cp.mentioned_in ?? 0}/{cp.total_platforms ?? 0}</td>
                                                                    <td className="text-right text-muted-foreground">
                                                                        {(t.platforms_analyzed || []).length}
                                                                    </td>
                                                                </tr>
                                                            );
                                                        })}
                                                    </tbody>
                                                </table>
                                                <p className="text-xs text-indigo-400 text-right">共 {trendData.length} 次快照</p>
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}

// =============================================
// 子面板：信源分析 V4.2
// =============================================
function SourceAnalysisPanel({ data }: { data: { top_sources: SourceItem[]; total_unique_sources: number; has_real_citations?: boolean } }) {
    return (
        <div className="bg-card border border-border rounded-xl p-5">
            <h3 className="text-base font-semibold text-foreground mb-4">
                AI 引用信源排名
                <span className="text-sm font-normal text-muted-foreground ml-2">
                    共 {data.total_unique_sources || 0} 个唯一来源
                </span>
            </h3>
            {(data.top_sources?.length || 0) === 0 ? (
                <p className="text-muted-foreground text-sm py-8 text-center">暂无信源数据</p>
            ) : (
                <div className="space-y-2">
                    {data.has_real_citations === false && (
                        <div className="text-xs text-amber-600 bg-amber-50 border border-amber-200 rounded-md px-3 py-2 mb-3">
                            当前显示的是LLM蒸馏提取的信源（精度有限）。下次监测运行后将自动切换为AI引擎实际返回的来源URL。
                        </div>
                    )}
                    {data.top_sources.map((source, i) => {
                        const displayName = source.domain || source.title || source.url || '未知';
                        const linkUrl = source.url && source.url.startsWith('http') ? source.url : undefined;
                        return (
                            <div key={i} className="flex items-center gap-3 p-3 bg-muted rounded-lg hover:bg-muted/80 transition">
                                <span className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold ${i < 3 ? 'bg-indigo-500 text-white' : 'bg-muted text-muted-foreground'
                                    }`}>{i + 1}</span>
                                <div className="flex-1 min-w-0">
                                    {linkUrl ? (
                                        <a href={linkUrl} target="_blank" rel="noopener noreferrer"
                                            className="text-sm text-indigo-600 hover:text-indigo-700 truncate block">
                                            {displayName}
                                        </a>
                                    ) : (
                                        <span className="text-sm text-foreground truncate block">{displayName}</span>
                                    )}
                                    {source.domain && source.title && source.domain !== source.title && (
                                        <span className="text-xs text-muted-foreground">{source.domain}</span>
                                    )}
                                </div>
                                {source.source === 'search_citations' && (
                                    <span className="px-2 py-0.5 text-xs bg-green-50 text-green-600 border border-green-200 rounded-full">
                                        引擎来源
                                    </span>
                                )}
                                {source.content_type && source.content_type !== '未知' && (
                                    <span className="px-2 py-0.5 text-xs bg-indigo-50 text-indigo-600 border border-indigo-100 rounded-full">
                                        {source.content_type}
                                    </span>
                                )}
                                <span className="text-sm text-muted-foreground whitespace-nowrap">{source.count} 次来源命中</span>
                                <div className="flex gap-1">
                                    {source.platforms.map(p => (
                                        <span key={p} className="px-2 py-0.5 text-xs bg-muted text-muted-foreground border border-border rounded-full capitalize">{p}</span>
                                    ))}
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

// =============================================
// 子面板：效果验证 V4.2
// =============================================
function ROIVerificationPanel({ data }: { data: ROIData }) {
    const { baseline, current, delta } = data;

    function DeltaBadge({ value, suffix = '%', inverted = false }: { value: number | undefined; suffix?: string; inverted?: boolean }) {
        if (value === undefined || value === null) return <span className="text-muted-foreground">-</span>;
        const isGood = inverted ? value < 0 : value > 0;
        return (
            <span className={`text-sm font-semibold ${isGood ? 'text-emerald-600' : value === 0 ? 'text-muted-foreground' : 'text-red-500'}`}>
                {value > 0 ? '+' : ''}{value}{suffix}
            </span>
        );
    }

    return (
        <div className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
                <div className="bg-card border border-border rounded-xl p-5 text-center">
                    <p className="text-sm text-muted-foreground mb-1">检出率变化</p>
                    <div className="text-3xl font-bold">
                        <DeltaBadge value={delta.detection_rate} />
                    </div>
                    <p className="text-xs text-muted-foreground mt-1">对比基准期</p>
                </div>
                <div className="bg-card border border-border rounded-xl p-5 text-center">
                    <p className="text-sm text-muted-foreground mb-1">最佳排名变化</p>
                    <div className="text-3xl font-bold">
                        <DeltaBadge value={delta.avg_best_rank} suffix="" inverted />
                    </div>
                    <p className="text-xs text-muted-foreground mt-1">越低越好</p>
                </div>
            </div>

            <div className="bg-card border border-border rounded-xl p-5">
                <h3 className="text-base font-semibold text-foreground mb-4">基准期 vs 当前期</h3>
                <table className="w-full text-sm">
                    <thead>
                        <tr className="border-b border-border">
                            <th className="text-left text-muted-foreground pb-3 font-medium">指标</th>
                            <th className="text-right text-muted-foreground pb-3 font-medium">基准期 (30-60天前)</th>
                            <th className="text-right text-muted-foreground pb-3 font-medium">当前期 (最近30天)</th>
                            <th className="text-right text-muted-foreground pb-3 font-medium">变化</th>
                        </tr>
                    </thead>
                    <tbody className="text-foreground">
                        <tr className="border-b border-border/50">
                            <td className="py-3">蒸馏关键词数</td>
                            <td className="text-right">{baseline.total_keywords}</td>
                            <td className="text-right">{current.total_keywords}</td>
                            <td className="text-right"><DeltaBadge value={current.total_keywords - baseline.total_keywords} suffix="" /></td>
                        </tr>
                        <tr className="border-b border-border/50">
                            <td className="py-3">品牌被提及数</td>
                            <td className="text-right">{baseline.mentioned_count}</td>
                            <td className="text-right">{current.mentioned_count}</td>
                            <td className="text-right"><DeltaBadge value={current.mentioned_count - baseline.mentioned_count} suffix="" /></td>
                        </tr>
                        <tr className="border-b border-border/50">
                            <td className="py-3">检出率</td>
                            <td className="text-right">{baseline.detection_rate}%</td>
                            <td className="text-right">{current.detection_rate}%</td>
                            <td className="text-right"><DeltaBadge value={delta.detection_rate} /></td>
                        </tr>
                        <tr>
                            <td className="py-3">最佳平均排名</td>
                            <td className="text-right">{baseline.avg_best_rank ?? '-'}</td>
                            <td className="text-right">{current.avg_best_rank ?? '-'}</td>
                            <td className="text-right"><DeltaBadge value={delta.avg_best_rank} suffix="" inverted /></td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    );
}

// =============================================
// 复用组件：空状态
// =============================================
function EmptyState() {
    return (
        <div className="flex flex-col items-center justify-center h-48 bg-card border border-border rounded-xl">
            <BarChart3 className="h-10 w-10 text-muted-foreground/30 mb-3" />
            <p className="text-sm text-muted-foreground">暂无蒸馏数据</p>
            <p className="text-xs text-muted-foreground mt-1">执行监测任务后会自动生成洞察</p>
        </div>
    );
}

// =============================================
// 复用组件：统计卡片
// =============================================
function StatCard({
    label, value, icon: Icon, color, isText = false
}: {
    label: string;
    value: number | string;
    icon: any;
    color: string;
    isText?: boolean;
}) {
    const colorMap: Record<string, string> = {
        indigo: 'bg-indigo-50 border-indigo-100',
        blue: 'bg-blue-50 border-blue-100',
        emerald: 'bg-emerald-50 border-emerald-100',
        amber: 'bg-amber-50 border-amber-100',
        slate: 'bg-muted border-border',
    };
    const iconColor: Record<string, string> = {
        indigo: 'text-indigo-500',
        blue: 'text-blue-500',
        emerald: 'text-emerald-500',
        amber: 'text-amber-500',
        slate: 'text-muted-foreground',
    };

    return (
        <div className={`${colorMap[color]} border rounded-xl p-4`}>
            <div className="flex items-center gap-2 mb-2">
                <Icon className={`h-4 w-4 ${iconColor[color]}`} />
                <span className="text-xs text-muted-foreground">{label}</span>
            </div>
            <div className={`${isText ? 'text-lg' : 'text-2xl'} font-bold text-foreground`}>
                {typeof value === 'number' ? value.toLocaleString() : value}
            </div>
        </div>
    );
}
