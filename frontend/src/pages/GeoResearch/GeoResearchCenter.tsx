/**
 * GeoResearchCenter · 发布参谋页 [P11 · 2026-05-26 重设计]
 *
 * 角色定位变化:
 *   旧: CSV 调研数据管理后台 (代理也能看上传/模板/跑批 · 老板说不行)
 *   新: 投放决策视图 · 代理/销售看 "投哪儿" · 管理员额外有维护入口
 *
 * 严格按 P11 spec:
 *   1. 删除 "下载调研模板" "上传调研数据" 跑批入口 file input
 *   2. 保留 刷新; 权重配置/全局清理/调研后台 仅 admin 可见
 *   3. 全局清理收进 admin "更多/维护" 折叠菜单 · 二次确认
 *   4. 不显示 文章原文 / cleaned_content / raw_content / AI 原文
 *   5. 普通代理/销售看不到 上传/模板/跑批/清理/权重配置/导入记录/撤回批次
 *   6. 主体: Top10 → 矩阵 → 内容类型 → 题目预览 (先结论后依据)
 *
 * 数据安全:
 *   - 题目预览走 /research/industry/{id}/prompts-preview (后端保证不返回 AI 原文)
 *   - 内容类型分布走 /research/industry/{id}/content-type-stats
 *   - 矩阵复用 /sources 但只取 platform_engine_matrix (引用次数 · 不含 url/title/excerpt)
 *   - 历史 /details endpoint (会返回 answer_text) 本页不再调
 */
import { useState, useEffect, useCallback, useMemo, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { authFetch } from '@/lib/api';
import { useAuth } from '@/context/AuthContext';
import { WeightConfigDrawer } from '@/components/research/WeightConfigDrawer';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
    Loader2, FlaskConical, BarChart3,
    Database, Search, RefreshCw, TrendingUp, FileText,
    Settings, Wrench, ExternalLink, Star, Copy, ChevronDown,
    Trophy, LayoutGrid, HelpCircle, Radio,
    ClipboardCheck, CheckCircle2, Brain, Megaphone, History,
} from 'lucide-react';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

// ---------------------------------------------------------------------------
// Types (后端返回结构)
// ---------------------------------------------------------------------------

interface IndustryStats {
    industry: string;
    queries: number;
    engines: number;
    citations: number;
    batch_count: number;
    last_round_at: string | null;       // P11 新增 · ISO
}

interface ResearchSummary {
    total_records: number;
    total_industries: number;
    total_engines: number;               // P11 新增
    last_updated_at: string | null;      // P11 新增
    by_industry: IndustryStats[];
}

/**
 * 来自 /api/placement/research/industry/{industry} 的 platform_scores 单平台元数据
 *
 * 数据来源是 geo_engine_stats (聚合后 · month_weights 加权 · 老数据自然衰减)
 * 不是 geo_research_raw 原始 count · 这才是发布参谋应该看的"加权矩阵"
 *
 * score = Σ engine_weights[eng] × citation_rates[eng] × 100 (placement_service.py:2310)
 * citation_rates[eng] = 该平台在该引擎下的引用概率 (0~1)
 */
interface PlatformScore {
    engines: string[];                         // 命中过该平台的引擎列表
    citation_rates: Record<string, number>;    // engine → citation_rate (0~1)
    score: number;                             // 加权综合分 (越高越值得投放)
    type: string;                              // 平台类型 (UGC社区/门户/视频UGC...)
}

interface ContentTypeRow {
    content_type: string;
    count: number;
    ratio: number;
}

interface ContentTypeStats {
    industry: string;
    total: number;
    by_type: ContentTypeRow[];
    uncategorized_count: number;
}

interface PromptItem {
    prompt: string;
    engine_count: number;
    citation_count: number;
}

interface PromptsPreview {
    industry: string;
    source: 'prompts_table' | 'raw_fallback';
    total: number;
    items: PromptItem[];
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ENGINE_NAMES = ['豆包', 'Kimi', 'DeepSeek', '千问'];

// 内容类型 7 分类 (P09 stage 4 启发式产出) · UI 友好名 + 代理建议
const CONTENT_TYPE_LABEL: Record<string, string> = {
    article: '资讯文章',
    video: '视频内容',
    doc_tool: '文档/工具',
    encyc: '百科/知识库',
    ecom: '电商页面',
    gov: '政府/官方',
    other: '其他',
};

// 内容类型分布 → 代理投放建议 (本页静态文案 · 不入 DB)
function suggestFromContentTypes(top: ContentTypeRow | undefined): string {
    if (!top) return '该行业暂无文章库样本 · 建议先让管理员跑一轮调研收集样本';
    const ct = top.content_type;
    const pct = (top.ratio * 100).toFixed(0);
    if (ct === 'article') return `资讯文章占 ${pct}% · 建议优先做长文投放(深度 1500+ 字)`;
    if (ct === 'video') return `视频内容占 ${pct}% · 补短视频脚本 + 种草文案(B站/抖音/小红书)`;
    if (ct === 'doc_tool') return `文档/工具占 ${pct}% · 做教程、清单、工具型内容(How-to / Checklist)`;
    if (ct === 'encyc') return `百科条目占 ${pct}% · 投百科类深度专题(知乎/百度知道/维基风格)`;
    if (ct === 'ecom') return `电商页面占 ${pct}% · 重点维护商品详情页 + KOC 测评`;
    if (ct === 'gov') return `官方信源占 ${pct}% · 内容需引用政策文件 / 行业白皮书提升权威性`;
    return `主要内容类型: ${CONTENT_TYPE_LABEL[ct] ?? ct} (${pct}%) · 视具体行业语境定投放策略`;
}

// 推荐分 → 星级 (4 档 · 跟 spec 一致)
function scoreToStars(score: number): number {
    if (score >= 30) return 4;
    if (score >= 15) return 3;
    if (score >= 5) return 2;
    return 1;
}

// 推荐分 → 一句 hint
function scoreToHint(score: number, platform: string): string {
    const stars = scoreToStars(score);
    if (stars === 4) return `${platform} 是该行业强势平台 · 优先重投`;
    if (stars === 3) return `${platform} 表现稳定 · 值得持续投放`;
    if (stars === 2) return `${platform} 有一定权重 · 可作辅助补充`;
    return `${platform} 当前曝光较低 · 投入需谨慎`;
}

function formatRelative(iso: string | null): string {
    if (!iso) return '从未';
    const d = new Date(iso);
    const diffH = (Date.now() - d.getTime()) / 36e5;
    if (diffH < 1) return '刚刚';
    if (diffH < 24) return `${Math.floor(diffH)}h 前`;
    const diffD = diffH / 24;
    if (diffD < 30) return `${Math.floor(diffD)}天前`;
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function freshnessBadge(iso: string | null): { label: string; tone: 'green' | 'amber' | 'red' | 'gray' } {
    if (!iso) return { label: '无数据', tone: 'gray' };
    const diffD = (Date.now() - new Date(iso).getTime()) / 86400e3;
    if (diffD < 7) return { label: '本周', tone: 'green' };
    if (diffD < 30) return { label: '本月', tone: 'amber' };
    return { label: `${Math.floor(diffD / 30)} 月前`, tone: 'red' };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function GeoResearchCenter() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const { user } = useAuth();
    const navigate = useNavigate();
    const isAdmin = user?.is_admin === true;

    // ---- 全局状态 ----
    const [summary, setSummary] = useState<ResearchSummary | null>(null);
    const [loading, setLoading] = useState(true);
    const [search, setSearch] = useState('');
    const [workbenchTab, setWorkbenchTab] = useState('overview');

    // ---- 行业详情状态 ----
    // P11 fix (2026-05-26 v2): 删 sourceAnalysis state
    //   理由 1 (HIGH 安全): /sources endpoint 同时返回 cross_engine_sources / engine_unique_sources
    //     里有 url/title · 前端不渲染不等于不下载 · 浏览器 Network 仍可见 · 代理/销售边界被破
    //   理由 2 (HIGH 数据): /sources 走 geo_research_raw 原始 count · 没 month_weights 衰减
    //     发布参谋 spec 要的是加权矩阵 · 应走 /industry/{id} 的 platform_scores (geo_engine_stats)
    //   修法: Top10 + Matrix 都从 industryScores (platform_scores) 派生
    const [selectedIndustry, setSelectedIndustry] = useState<string | null>(null);
    const [industryScores, setIndustryScores] = useState<Record<string, PlatformScore> | null>(null);
    const [loadingScores, setLoadingScores] = useState(false);
    const [contentStats, setContentStats] = useState<ContentTypeStats | null>(null);
    const [loadingContent, setLoadingContent] = useState(false);
    const [promptsPreview, setPromptsPreview] = useState<PromptsPreview | null>(null);
    const [loadingPrompts, setLoadingPrompts] = useState(false);
    const [showAllPrompts, setShowAllPrompts] = useState(false);

    // ---- Admin UI 状态 ----
    const [weightConfigOpen, setWeightConfigOpen] = useState(false);
    const [maintMenuOpen, setMaintMenuOpen] = useState(false);
    const [cleaning, setCleaning] = useState(false);
    const [cleanResult, setCleanResult] = useState<{ success: boolean; removed_bad?: number; removed_dup?: number; remaining?: number; error?: string } | null>(null);

    // ---- Data loaders ----
    const loadSummary = useCallback(async () => {
        try {
            const res = await authFetch('/api/placement/research/summary');
            if (res.ok) setSummary(await res.json());
        } catch (err) {
            console.error('Failed to load summary:', err);
        } finally {
            setLoading(false);
        }
    }, []);

    const loadIndustryScores = useCallback(async (industry: string) => {
        setLoadingScores(true);
        setIndustryScores(null);
        try {
            const res = await authFetch(`/api/placement/research/industry/${encodeURIComponent(industry)}`);
            if (res.ok) {
                const data = await res.json();
                setIndustryScores(data.platform_scores ?? {});
            }
        } finally {
            setLoadingScores(false);
        }
    }, []);

    // P11 fix (2026-05-26 v2): 删 loadSourceAnalysis · 见 selectedIndustry state 注释
    // /sources 不再调 · 防止 url/title 在浏览器 Network 暴露
    // Top10 + Matrix 都从 industryScores (platform_scores · geo_engine_stats 加权) 派生

    const loadContentStats = useCallback(async (industry: string) => {
        setLoadingContent(true);
        setContentStats(null);
        try {
            const res = await authFetch(`/api/placement/research/industry/${encodeURIComponent(industry)}/content-type-stats`);
            if (res.ok) setContentStats(await res.json());
        } finally {
            setLoadingContent(false);
        }
    }, []);

    const loadPromptsPreview = useCallback(async (industry: string) => {
        setLoadingPrompts(true);
        setPromptsPreview(null);
        setShowAllPrompts(false);
        try {
            const res = await authFetch(`/api/placement/research/industry/${encodeURIComponent(industry)}/prompts-preview?limit=50`);
            if (res.ok) setPromptsPreview(await res.json());
        } finally {
            setLoadingPrompts(false);
        }
    }, []);

    useEffect(() => { loadSummary(); }, [loadSummary]);

    useEffect(() => {
        if (!selectedIndustry) {
            setIndustryScores(null);
            setContentStats(null);
            setPromptsPreview(null);
            return;
        }
        // 并发拉 3 个 endpoint · 都是安全 endpoint (不含 url/title/answer_text)
        // industry → platform_scores (加权 matrix · Top10 + 矩阵都靠它)
        // content-type-stats / prompts-preview → P11 新加 · 已锁安全
        loadIndustryScores(selectedIndustry);
        loadContentStats(selectedIndustry);
        loadPromptsPreview(selectedIndustry);
    }, [selectedIndustry, loadIndustryScores, loadContentStats, loadPromptsPreview]);

    // ---- Admin only: 全局清理 (放维护菜单 · 二次确认) ----
    const handleGlobalCleanup = async () => {
        const msg =
            '将扫描所有行业的调研原始数据,移除:1) 平台名异常超长(脏数据);' +
            '2) 同行业 + query + engine + url 完全重复的引用。' +
            '影响范围: 全部 ' + (summary?.total_records ?? 0) + ' 条引用记录。' +
            '操作不可撤销 · 建议先确认这是预期操作。';
        if (!(await askConfirm({ title: '【全局清理】确认执行?', description: msg, confirmLabel: '确认清理', danger: true }))) return;
        // 二次确认
        if (!(await askConfirm({ title: '再次确认: 全局清理会立即重写 geo_research_raw 表 · 真的执行?', danger: true }))) return;
        setCleaning(true);
        setCleanResult(null);
        try {
            const formData = new FormData();
            const res = await authFetch('/api/placement/research/cleanup', { method: 'POST', body: formData });
            const data = await res.json();
            setCleanResult(data);
            if (data.success) {
                await loadSummary();
                if (selectedIndustry) {
                    await loadIndustryScores(selectedIndustry);
                }
            }
        } catch (err) {
            setCleanResult({ success: false, error: String(err) });
        } finally {
            setCleaning(false);
        }
    };

    // ---- Derived: 行业列表 (过滤 + 排序) ----
    const filteredIndustries = useMemo(() => {
        let list = summary?.by_industry ?? [];
        // [BUG4 2026-06-05] geo服务 = 平台内部 GEO 业务分类 · 仅 admin 可见(防暴露内部口径 · 搜索也搜不到)
        if (!isAdmin) {
            list = list.filter(s => !s.industry.replace(/\s/g, '').toLowerCase().includes('geo服务'));
        }
        const q = search.trim().toLowerCase();
        return q
            ? list.filter(s => s.industry.toLowerCase().includes(q))
            : list;
    }, [summary, search, isAdmin]);

    const selectedStats = useMemo(() => {
        if (!selectedIndustry) return null;
        return (summary?.by_industry ?? []).find(s => s.industry === selectedIndustry) ?? null;
    }, [summary, selectedIndustry]);

    useEffect(() => {
        if (filteredIndustries.length === 0) {
            if (selectedIndustry) setSelectedIndustry(null);
            return;
        }
        if (!selectedIndustry || !filteredIndustries.some(s => s.industry === selectedIndustry)) {
            setSelectedIndustry(filteredIndustries[0].industry);
        }
    }, [filteredIndustries, selectedIndustry]);

    // ---- Derived: Top 10 投放优先级 (P11 v2 改走 platform_scores · geo_engine_stats 加权) ----
    //
    // 之前版本走 /sources 的 platform_engine_matrix [HIGH bug]:
    //   - shape 误读 (后端是 platform→engine·我当 engine→platform 用) · 把 4 个引擎当投放平台
    //   - /sources 基于 geo_research_raw 原始 count · 没 month_weights 衰减
    //
    // 现版本走 industryScores (platform_scores · placement_service.py:2261):
    //   - score 已是 Σ engine_weights × citation_rates × 100 加权综合分
    //   - geo_engine_stats 经 _recompute_industry_aggregates 时叠加 month_weights
    //   - 平台是真平台名 (知乎/抖音/头条等) · 不会再错把豆包/Kimi 当投放目标
    const top10 = useMemo(() => {
        if (!industryScores) return [];
        return Object.entries(industryScores)
            .map(([platform, ps]) => ({
                platform,
                // citation_count: 该平台被 4 个引擎引用的总次数估算
                // 用 citation_rates 求和(各引擎的 rate × 100) · 直观且不依赖额外字段
                citation_count: Object.values(ps.citation_rates).reduce((s, r) => s + Math.round(r * 100), 0),
                score: ps.score,
                type: ps.type,
                engines: ps.engines,
                citation_rates: ps.citation_rates,
            }))
            .sort((a, b) => b.score - a.score)
            .slice(0, 10);
    }, [industryScores]);

    // ---- Loading 全屏 ----
    if (loading) {
        return (
            <div className="flex items-center justify-center h-64">
                <Loader2 className="h-8 w-8 animate-spin text-brand" />
            </div>
        );
    }

    return (
        <div className="space-y-5 p-0 sm:p-2 lg:p-4">

            {/* ============== 顶部: 人话化飞轮入口 ============== */}
            <section className="rounded-2xl border bg-card/70 p-4 shadow-sm sm:p-5">
                <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                    <div className="flex items-start gap-3">
                        <div className="hidden h-11 w-11 rounded-2xl bg-brand/10 sm:flex items-center justify-center shrink-0">
                            <FlaskConical className="h-5 w-5 text-brand" />
                        </div>
                        <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                                <h1 className="text-xl font-bold text-foreground sm:text-2xl">GEO 数据飞轮</h1>
                                <Badge variant="secondary" className="bg-emerald-500/10 text-emerald-700">内部顾问</Badge>
                            </div>
                            <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">
                                把 AI 调研结果整理成媒体推荐、内容策略和证据依据。这里只给运营做决策参考,启用任何策略都需要人工审核,不会自动接管写作、投放、报价或扣费。
                            </p>
                        </div>
                    </div>
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-center xl:justify-end">
                        <div className="relative min-w-[180px] sm:min-w-[220px]">
                            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                            <input
                                type="text"
                                value={search}
                                onChange={e => setSearch(e.target.value)}
                                placeholder="搜索行业"
                                className="h-10 w-full rounded-xl border bg-background pl-9 pr-3 text-sm"
                            />
                        </div>
                        <select
                            value={selectedIndustry ?? ''}
                            onChange={e => setSelectedIndustry(e.target.value || null)}
                            className="h-10 min-w-[180px] rounded-xl border bg-background px-3 text-sm"
                        >
                            {filteredIndustries.length === 0 ? (
                                <option value="">暂无行业数据</option>
                            ) : filteredIndustries.map(stats => (
                                <option key={stats.industry} value={stats.industry}>{stats.industry}</option>
                            ))}
                        </select>
                        <Button variant="ghost" size="sm" onClick={loadSummary}>
                            <RefreshCw className="h-3.5 w-3.5 mr-1.5" />刷新
                        </Button>
                        {isAdmin && (
                            <Button variant="outline" size="sm" onClick={() => navigate('/admin/research-monitor')}>
                                <ExternalLink className="h-3.5 w-3.5 mr-1.5" />调研后台
                            </Button>
                        )}
                    </div>
                </div>
            </section>

            {/* 清理结果反馈 (admin only) */}
            {isAdmin && cleanResult && (
                <div className={`text-xs px-3 py-2 rounded-md ${cleanResult.success ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}`}>
                    {cleanResult.success
                        ? `清理完成: 移除坏数据 ${cleanResult.removed_bad ?? 0} 条,去重 ${cleanResult.removed_dup ?? 0} 条,剩余 ${cleanResult.remaining ?? 0} 条`
                        : `清理失败: ${cleanResult.error ?? '未知错误'}`}
                </div>
            )}

            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm leading-relaxed text-amber-900 dark:text-amber-100">
                真实第一: 答案采纳和明确引用权重最高,网页抓取与搜索曝光只作参考。当前页面不会直接改线上策略,所有启用都需要人工复核。
            </div>

            {/* ============== 运营工作台: 一个容器 + 顶部小标签 ============== */}
            <section className="rounded-2xl border bg-card/70 p-3 shadow-sm sm:p-4">
                <div className="mb-4 flex flex-col gap-3 border-b pb-4 lg:flex-row lg:items-center lg:justify-between">
                    <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                            <h2 className="text-lg font-bold sm:text-xl">飞轮顾问工作台</h2>
                            <Badge variant="secondary" className="bg-emerald-500/10 text-emerald-700">只给建议</Badge>
                            <Badge variant="secondary" className="bg-amber-500/10 text-amber-700">人工审核后启用</Badge>
                        </div>
                        <p className="mt-2 max-w-4xl text-sm leading-relaxed text-muted-foreground">
                            这里把调研数据翻译成运营能看的四件事:数据是否够用、哪些媒体值得做、文章应该怎么写、是否可以进入人工审核。飞轮仍是内部顾问,不会自动改线上写作、投放、报价或扣费。
                        </p>
                    </div>
                    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:min-w-[520px]">
                        <MiniStatus label="线上影响" value="关闭" tone="green" />
                        <MiniStatus label="当前行业" value={selectedIndustry ?? '暂无'} />
                        <MiniStatus label="候选媒体" value={top10.length ? `${top10.length} 个` : '待聚合'} />
                        <MiniStatus label="最后更新" value={formatRelative(selectedStats?.last_round_at ?? summary?.last_updated_at ?? null)} />
                    </div>
                </div>
                <Tabs value={workbenchTab} onValueChange={setWorkbenchTab} className="w-full">
                    <TabsList className="flex h-auto w-full flex-wrap justify-start gap-1 rounded-xl bg-muted/40 p-1">
                        <TabsTrigger value="overview" className="gap-1.5"><BarChart3 className="h-3.5 w-3.5" />总览</TabsTrigger>
                        <TabsTrigger value="media" className="gap-1.5"><Radio className="h-3.5 w-3.5" />媒体推荐</TabsTrigger>
                        <TabsTrigger value="strategy" className="gap-1.5"><Brain className="h-3.5 w-3.5" />内容策略</TabsTrigger>
                        <TabsTrigger value="evidence" className="gap-1.5"><Search className="h-3.5 w-3.5" />证据说明</TabsTrigger>
                        <TabsTrigger value="review" className="gap-1.5"><ClipboardCheck className="h-3.5 w-3.5" />人工审核</TabsTrigger>
                        <TabsTrigger value="history" className="gap-1.5"><History className="h-3.5 w-3.5" />历史记录</TabsTrigger>
                    </TabsList>

                    <TabsContent value="overview" className="mt-4 space-y-4">
                        <div className="grid grid-cols-2 gap-2 md:grid-cols-4 md:gap-4">
                            <StatCard icon={<FlaskConical className="h-4 w-4 text-green-600" />} bg="bg-green-100"
                                value={`${summary?.total_industries ?? 0}`} label="已覆盖行业" />
                            <StatCard icon={<Database className="h-4 w-4 text-blue-600" />} bg="bg-blue-100"
                                value={(summary?.total_records ?? 0).toLocaleString()} label="AI 引用记录" />
                            <StatCard icon={<BarChart3 className="h-4 w-4 text-orange-600" />} bg="bg-orange-100"
                                value={`${summary?.total_engines ?? 0} / ${ENGINE_NAMES.length}`} label="AI 平台覆盖" />
                            <StatCard icon={<TrendingUp className="h-4 w-4 text-purple-600" />} bg="bg-purple-100"
                                value={formatRelative(summary?.last_updated_at ?? null)} label="最近更新" small />
                        </div>
                        <div className="grid gap-3 lg:grid-cols-3">
                            <InsightCard title="当前行业" value={selectedIndustry ?? '暂无行业'} description={selectedStats ? `${selectedStats.citations.toLocaleString()} 条引用 · ${selectedStats.queries} 个调研题 · ${selectedStats.batch_count} 个批次` : '还没有可展示的行业样本'} />
                            <InsightCard title="推荐依据" value={top10.length ? `${top10.length} 个候选平台` : '待聚合'} description="优先看答案采纳与明确引用,再看文章库和搜索曝光。" />
                            <InsightCard title="上线边界" value="人工审核" description="飞轮只给建议。启用前必须由管理员复核风险、白名单和适用行业。" />
                        </div>
                    </TabsContent>

                    <TabsContent value="media" className="mt-4 space-y-4">
                        {selectedIndustry ? (
                            <>
                                <Top10Panel items={top10} loading={loadingScores} industry={selectedIndustry} />
                                <MatrixPanel scores={industryScores} loading={loadingScores} />
                            </>
                        ) : <EmptyWorkbench message="暂无行业可查看媒体推荐。请先完成一轮调研聚合。" />}
                    </TabsContent>

                    <TabsContent value="strategy" className="mt-4 space-y-4">
                        <ContentTypePanel stats={contentStats} loading={loadingContent} />
                        <PromptsPanel
                            preview={promptsPreview}
                            loading={loadingPrompts}
                            showAll={showAllPrompts}
                            onToggleShowAll={() => setShowAllPrompts(s => !s)}
                        />
                    </TabsContent>

                    <TabsContent value="evidence" className="mt-4 space-y-4">
                        <div className="grid gap-3 md:grid-cols-2">
                            <InsightCard title="证据口径" value="引用优先" description="答案采纳、明确引用和文章库样本是主证据;搜索曝光和网页抓取只作低权重参考。" />
                            <InsightCard title="数据新鲜度" value={formatRelative(selectedStats?.last_round_at ?? summary?.last_updated_at ?? null)} description="过旧行业需要先跑调研,再看媒体和策略建议。" />
                        </div>
                        {isAdmin && selectedIndustry && (
                            <Button
                                size="sm"
                                variant="outline"
                                onClick={() => {
                                    const ind = encodeURIComponent(selectedIndustry);
                                    navigate(`/admin/research-monitor?tab=citations&industry=${ind}`);
                                }}
                            >
                                <Search className="w-3.5 h-3.5 mr-1" />
                                查看引用明细
                            </Button>
                        )}
                    </TabsContent>

                    <TabsContent value="review" className="mt-4 space-y-4">
                        <div className="grid gap-3 md:grid-cols-2">
                            <ReviewCard title="接管前闸门" status="等待人工审核" description="当前不会接管线上。管理员需要确认行业、媒体白名单、风险边界和客户适用范围。" />
                            <ReviewCard title="策略启用" status="手动启用" description="飞轮输出只能进入待审核候选。启用后也应先灰度观察,不能直接对客承诺效果。" />
                        </div>
                        {isAdmin && (
                            <div className="flex flex-wrap gap-2">
                                <Button variant="outline" size="sm" onClick={() => setWeightConfigOpen(true)}>
                                    <Settings className="h-3.5 w-3.5 mr-1.5" />权重配置
                                </Button>
                                <div className="relative">
                                    <Button variant="outline" size="sm" onClick={() => setMaintMenuOpen(o => !o)}>
                                        <Wrench className="h-3.5 w-3.5 mr-1.5" />维护<ChevronDown className="h-3 w-3 ml-1" />
                                    </Button>
                                    {maintMenuOpen && (
                                        <div className="absolute left-0 top-full z-10 mt-1 w-72 rounded-md border bg-popover p-1 shadow-lg">
                                            <div className="px-2 py-1.5 text-xs font-medium text-muted-foreground border-b mb-1">
                                                危险操作 · 操作不可撤销
                                            </div>
                                            <button
                                                type="button"
                                                disabled={cleaning}
                                                onClick={() => { setMaintMenuOpen(false); handleGlobalCleanup(); }}
                                                className="w-full text-left text-sm px-3 py-2 rounded hover:bg-red-50 text-red-700 flex items-center gap-2 disabled:opacity-50"
                                            >
                                                {cleaning ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Database className="h-3.5 w-3.5" />}
                                                <div className="min-w-0 flex-1">
                                                    <div className="font-medium">全局清理调研数据</div>
                                                    <div className="text-xs text-red-600/70 mt-0.5">移除脏平台名 + 完全重复引用 · 需二次确认</div>
                                                </div>
                                            </button>
                                        </div>
                                    )}
                                </div>
                            </div>
                        )}
                    </TabsContent>

                    <TabsContent value="history" className="mt-4 space-y-4">
                        <div className="rounded-xl border bg-background/60 p-4">
                            <div className="mb-3 flex items-center gap-2">
                                <History className="h-4 w-4 text-muted-foreground" />
                                <h3 className="text-sm font-semibold">近期行业样本</h3>
                            </div>
                            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                                {(summary?.by_industry ?? []).slice(0, 9).map(stats => {
                                    const fresh = freshnessBadge(stats.last_round_at);
                                    return (
                                        <button
                                            key={stats.industry}
                                            type="button"
                                            onClick={() => {
                                                setSelectedIndustry(stats.industry);
                                                setWorkbenchTab('overview');
                                            }}
                                            className="rounded-lg border bg-muted/20 px-3 py-2 text-left hover:bg-muted/40"
                                        >
                                            <div className="flex items-center justify-between gap-2">
                                                <span className="truncate text-sm font-medium">{stats.industry}</span>
                                                <Badge variant="secondary" className="text-[10px]">{fresh.label}</Badge>
                                            </div>
                                            <p className="mt-1 text-xs text-muted-foreground">
                                                {stats.citations.toLocaleString()} 引用 · {stats.engines} 个 AI 平台
                                            </p>
                                        </button>
                                    );
                                })}
                            </div>
                        </div>
                    </TabsContent>
                </Tabs>
            </section>

            {/* Weight Config Drawer (admin only)
                Drawer 内部维护 industry 选择器 · 它需要候选行业列表(来自 summary.by_industry)
                onAggregated 回调: 权重改动后自动刷新 summary 让左侧列表的 last_round_at 同步 */}
            {isAdmin && (
                <WeightConfigDrawer
                    open={weightConfigOpen}
                    onOpenChange={setWeightConfigOpen}
                    industries={(summary?.by_industry ?? []).map(s => s.industry)}
                    onAggregated={loadSummary}
                />
            )}
          {confirmDialog}
        </div>
    );
}

// =========================================================================
// 子组件
// =========================================================================

function StatCard(props: {
    icon: ReactNode;
    bg: string;
    value: string;
    label: string;
    small?: boolean;
}) {
    return (
        <Card>
            <CardContent className="p-3 sm:pt-5 sm:pb-4">
                <div className="flex items-center gap-2 sm:gap-3">
                    <div className={`h-8 w-8 rounded-lg ${props.bg} flex items-center justify-center sm:h-10 sm:w-10 shrink-0`}>
                        {props.icon}
                    </div>
                    <div className="min-w-0">
                        <p className={`font-bold tabular-nums truncate ${props.small ? 'text-sm sm:text-base' : 'text-lg sm:text-2xl'}`}>
                            {props.value}
                        </p>
                        <p className="text-xs text-muted-foreground">{props.label}</p>
                    </div>
                </div>
            </CardContent>
        </Card>
    );
}

function MiniStatus(props: { label: string; value: string; tone?: 'green' }) {
    const isGreen = props.tone === 'green';
    return (
        <div className={`rounded-xl border px-3 py-2 ${isGreen ? 'border-emerald-500/30 bg-emerald-500/10' : 'bg-background/60'}`}>
            <p className="text-[11px] text-muted-foreground">{props.label}</p>
            <p className={`mt-1 truncate text-sm font-semibold ${isGreen ? 'text-emerald-700 dark:text-emerald-300' : ''}`}>
                {props.value}
            </p>
        </div>
    );
}

function InsightCard(props: { title: string; value: string; description: string }) {
    return (
        <div className="rounded-xl border bg-background/60 p-4">
            <p className="text-xs font-medium text-muted-foreground">{props.title}</p>
            <p className="mt-1 text-xl font-bold">{props.value}</p>
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{props.description}</p>
        </div>
    );
}

function ReviewCard(props: { title: string; status: string; description: string }) {
    return (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
            <div className="flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-amber-600" />
                <h3 className="font-semibold">{props.title}</h3>
                <Badge variant="secondary" className="ml-auto bg-amber-500/10 text-amber-700">{props.status}</Badge>
            </div>
            <p className="mt-3 text-sm leading-relaxed text-muted-foreground">{props.description}</p>
        </div>
    );
}

function EmptyWorkbench({ message }: { message: string }) {
    return (
        <div className="rounded-xl border bg-background/60 py-12 text-center">
            <Megaphone className="mx-auto mb-3 h-8 w-8 text-muted-foreground/40" />
            <p className="text-sm text-muted-foreground">{message}</p>
        </div>
    );
}

function StarRow({ stars }: { stars: number }) {
    return (
        <div className="flex items-center gap-0.5">
            {[1, 2, 3, 4].map(i => (
                <Star
                    key={i}
                    className={`h-3.5 w-3.5 ${i <= stars ? 'fill-amber-400 text-amber-400' : 'text-muted-foreground/30'}`}
                />
            ))}
        </div>
    );
}

interface Top10Item {
    platform: string;
    citation_count: number;
    score: number;
    type: string;
    engines: string[];
    citation_rates: Record<string, number>;
}

function Top10Panel(props: {
    items: Top10Item[];
    loading: boolean;
    industry: string;
}) {
    return (
        <Card>
            <CardContent className="p-4 sm:p-5">
                <div className="flex items-center gap-2 mb-3">
                    <Trophy className="h-4 w-4 text-amber-500" />
                    <h3 className="font-semibold text-sm sm:text-base">投放优先级 Top 10</h3>
                    {/* score 已含 month_weights 衰减 · 不是 raw count */}
                    <Badge variant="secondary" className="text-[10px] ml-auto">加权综合分(月度衰减)</Badge>
                </div>
                {props.loading ? (
                    <div className="py-8 text-center">
                        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground mx-auto" />
                    </div>
                ) : props.items.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-6 text-center">
                        「{props.industry}」 暂无加权数据 · 调研后台跑一轮 + 等聚合脚本算 stats
                    </p>
                ) : (
                    <div className="space-y-1.5">
                        {props.items.map((it, idx) => {
                            const stars = scoreToStars(it.score);
                            return (
                                <div
                                    key={it.platform}
                                    className="flex items-center gap-3 p-2 rounded-md border bg-muted/20"
                                >
                                    <span className="w-6 text-center text-xs font-bold text-muted-foreground tabular-nums">
                                        #{idx + 1}
                                    </span>
                                    <div className="flex-1 min-w-0">
                                        <span className="font-medium text-sm truncate">{it.platform}</span>
                                        <span className="text-[10px] text-muted-foreground ml-1.5">{it.type}</span>
                                    </div>
                                    <span className="hidden sm:inline text-xs text-muted-foreground tabular-nums">
                                        命中 {it.engines.length}/{ENGINE_NAMES.length}
                                    </span>
                                    <span className="text-sm font-semibold tabular-nums text-brand w-14 text-right">
                                        {it.score.toFixed(1)}
                                    </span>
                                    <StarRow stars={stars} />
                                    <span className="hidden md:inline text-xs text-muted-foreground max-w-[180px] truncate">
                                        {scoreToHint(it.score, it.platform)}
                                    </span>
                                </div>
                            );
                        })}
                    </div>
                )}
            </CardContent>
        </Card>
    );
}

/**
 * Matrix Panel (P11 v2 改走加权 platform_scores · 不再调 /sources)
 *
 * 行 = 平台 (按 score DESC 排前 20)
 * 列 = 4 个引擎 (固定顺序 ENGINE_NAMES)
 * 值 = citation_rates[engine] × 100 = 该平台在该引擎下的引用概率%
 * heatmap 等级按 rate 分档 (强 ≥40% / 中 15~40% / 弱 <15%)
 *
 * 跟前版 [HIGH bug] 的区别:
 *   - 前版数据源 /sources platform_engine_matrix [plat][eng] · 走 raw count 无加权
 *   - 前版数据 shape 误读 (eng 当 outer key) · Top10 把"豆包"当投放平台
 *   - 现版数据源 /industry/{id} platform_scores · 走 geo_engine_stats (含 month_weights)
 *   - 现版 shape 正确: 平台→{engines, citation_rates[eng], score, type}
 */
function MatrixPanel(props: {
    scores: Record<string, PlatformScore> | null;
    loading: boolean;
}) {
    const engines = ENGINE_NAMES;  // 固定 4 列 · 不依赖 scores 数据
    const { platformsSorted, getRate, getScore } = useMemo(() => {
        if (!props.scores) {
            return {
                platformsSorted: [] as string[],
                getRate: () => 0,
                getScore: () => 0,
            };
        }
        const entries = Object.entries(props.scores).sort((a, b) => b[1].score - a[1].score);
        return {
            platformsSorted: entries.map(([p]) => p),
            getRate: (plat: string, eng: string) => props.scores?.[plat]?.citation_rates?.[eng] ?? 0,
            getScore: (plat: string) => props.scores?.[plat]?.score ?? 0,
        };
    }, [props.scores]);

    // heatmap 颜色: 单元格 = 该平台在该引擎下的引用概率 (0~1)
    const cellTone = (rate: number): string => {
        if (rate === 0) return 'bg-muted/20';
        if (rate >= 0.40) return 'bg-emerald-500/80 text-white';
        if (rate >= 0.25) return 'bg-emerald-400/70 text-white';
        if (rate >= 0.15) return 'bg-emerald-300/60';
        if (rate >= 0.05) return 'bg-emerald-200/50';
        return 'bg-emerald-100/40';
    };

    return (
        <Card>
            <CardContent className="p-4 sm:p-5">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <LayoutGrid className="h-4 w-4 text-emerald-600" />
                    <h3 className="font-semibold text-sm sm:text-base">平台 × 引擎引用矩阵</h3>
                    {/* 来自 geo_engine_stats · 已加权 · 不是 raw count */}
                    <Badge variant="secondary" className="text-[10px] ml-auto">
                        覆盖率 · 该引擎在 N% 的题里引用了该平台
                    </Badge>
                </div>
                {/* 文案修(2026-06): 老板反馈"横竖加起来不是 100%"感觉违反常识 · 实际是覆盖率不是分布 ·
                    每行每列独立 · 单题可引多平台 → 加起来本就不应该 = 100% · 解释清楚就不冲突 */}
                <div className="flex items-start gap-1.5 text-[11px] text-muted-foreground mb-3 leading-relaxed">
                    <HelpCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                    <span>
                        每格独立看 ·
                        例:豆包 × CSDN博客 = <strong>95%</strong> 意思是"豆包在本行业的所有题里 · 95% 的回答引用了 CSDN博客"。
                        <strong>横向不互补 100%</strong>(各引擎题集独立)·
                        <strong>纵向加可超 100%</strong>(单题回答常引用多个平台)。
                    </span>
                </div>
                {props.loading ? (
                    <div className="py-8 text-center">
                        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground mx-auto" />
                    </div>
                ) : platformsSorted.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-6 text-center">
                        暂无矩阵数据 · 调研后台跑一轮 + 等 aggregate_research_stats 算 stats
                    </p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-xs">
                            <thead>
                                <tr>
                                    <th className="text-left font-medium px-2 py-1.5 sticky left-0 bg-background">平台</th>
                                    {engines.map(eng => (
                                        <th key={eng} className="text-center font-medium px-2 py-1.5">{eng}</th>
                                    ))}
                                    <th className="text-right font-medium px-2 py-1.5">综合分</th>
                                </tr>
                            </thead>
                            <tbody>
                                {platformsSorted.slice(0, 20).map(plat => (
                                    <tr key={plat}>
                                        <td className="font-medium px-2 py-1.5 sticky left-0 bg-background truncate max-w-[140px]">{plat}</td>
                                        {engines.map(eng => {
                                            const rate = getRate(plat, eng);
                                            const pct = Math.round(rate * 100);
                                            // 文案修(2026-06): cell tooltip 说人话 · 防误读成"分布占比"
                                            const tip = rate > 0
                                                ? `${eng} 在本行业的题里 · ${pct}% 的回答引用了"${plat}"`
                                                : `${eng} 在本行业的题里 · 几乎没有回答引用"${plat}"`;
                                            return (
                                                <td key={eng} className="px-1 py-1">
                                                    <div className={`rounded px-1.5 py-1 text-center ${cellTone(rate)}`} title={tip}>
                                                        {rate > 0 ? `${pct}%` : '·'}
                                                    </div>
                                                </td>
                                            );
                                        })}
                                        <td className="text-right tabular-nums px-2 py-1.5 font-semibold text-brand">
                                            {getScore(plat).toFixed(1)}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                        {platformsSorted.length > 20 && (
                            <p className="text-xs text-muted-foreground mt-2 text-center">
                                只显示 Top 20 平台 (按综合分) · 完整数据共 {platformsSorted.length} 个
                            </p>
                        )}
                        {/* 推荐等级 legend */}
                        <div className="flex flex-wrap items-center gap-3 mt-3 text-xs text-muted-foreground">
                            <span>推荐等级:</span>
                            <span className="flex items-center gap-1">
                                <span className="w-3 h-3 rounded bg-emerald-500/80 inline-block" />强(≥40%)
                            </span>
                            <span className="flex items-center gap-1">
                                <span className="w-3 h-3 rounded bg-emerald-300/60 inline-block" />中(15~40%)
                            </span>
                            <span className="flex items-center gap-1">
                                <span className="w-3 h-3 rounded bg-emerald-100/40 inline-block" />弱(&lt;15%)
                            </span>
                        </div>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}

function ContentTypePanel(props: { stats: ContentTypeStats | null; loading: boolean }) {
    const top = props.stats?.by_type?.[0];
    return (
        <Card>
            <CardContent className="p-4 sm:p-5">
                <div className="flex items-center gap-2 mb-3">
                    <FileText className="h-4 w-4 text-indigo-600" />
                    <h3 className="font-semibold text-sm sm:text-base">内容类型分布</h3>
                    {props.stats && props.stats.total > 0 && (
                        <Badge variant="secondary" className="text-[10px] ml-auto">
                            样本: {props.stats.total} 篇入库文章
                        </Badge>
                    )}
                </div>
                {props.loading ? (
                    <div className="py-8 text-center"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground mx-auto" /></div>
                ) : !props.stats || props.stats.total === 0 ? (
                    <p className="text-sm text-muted-foreground py-6 text-center">
                        暂无入库文章样本 · 等下次调研收集后此处自动出现内容类型饼图
                    </p>
                ) : (
                    <div className="space-y-2">
                        {props.stats.by_type.map(t => (
                            <div key={t.content_type} className="flex items-center gap-3">
                                <span className="text-sm font-medium w-24 shrink-0">
                                    {CONTENT_TYPE_LABEL[t.content_type] ?? t.content_type}
                                </span>
                                <div className="flex-1 h-5 rounded bg-muted overflow-hidden relative">
                                    <div
                                        className="h-full bg-indigo-400 transition-all"
                                        style={{ width: `${t.ratio * 100}%` }}
                                    />
                                    <span className="absolute inset-0 flex items-center px-2 text-xs font-medium">
                                        {(t.ratio * 100).toFixed(0)}% · {t.count} 篇
                                    </span>
                                </div>
                            </div>
                        ))}
                        {props.stats.uncategorized_count > 0 && (
                            <p className="text-xs text-muted-foreground pt-1">
                                + {props.stats.uncategorized_count} 篇未分类(老数据 · 内容类型字段为空)
                            </p>
                        )}
                        {/* 代理建议 */}
                        <div className="mt-3 p-3 rounded-md bg-indigo-50 border border-indigo-100 text-xs text-indigo-900 leading-relaxed">
                            <span className="font-semibold">💡 投放建议: </span>
                            {suggestFromContentTypes(top)}
                        </div>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}

function PromptsPanel(props: {
    preview: PromptsPreview | null;
    loading: boolean;
    showAll: boolean;
    onToggleShowAll: () => void;
}) {
    const items = props.preview?.items ?? [];
    const visible = props.showAll ? items : items.slice(0, 5);

    const handleCopy = (text: string) => {
        try {
            navigator.clipboard?.writeText(text);
        } catch (e) {
            console.error('copy failed', e);
        }
    };

    return (
        <Card>
            <CardContent className="p-4 sm:p-5">
                <div className="flex items-center gap-2 mb-3">
                    <Search className="h-4 w-4 text-cyan-600" />
                    <h3 className="font-semibold text-sm sm:text-base">调研题目预览</h3>
                    {props.preview && (
                        <Badge variant="secondary" className="text-[10px] ml-auto">
                            {props.preview.source === 'prompts_table' ? '管理员配置' : '老 CSV 数据'} ·
                            共 {props.preview.total} 条
                        </Badge>
                    )}
                </div>
                {props.loading ? (
                    <div className="py-8 text-center"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground mx-auto" /></div>
                ) : items.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-6 text-center">该行业暂无调研题目</p>
                ) : (
                    <>
                        <div className="space-y-1.5">
                            {visible.map((p, idx) => (
                                <div key={`${p.prompt}-${idx}`} className="flex items-start gap-2 p-2 rounded-md border bg-muted/20">
                                    <span className="w-5 text-xs font-bold text-muted-foreground tabular-nums shrink-0 pt-0.5">
                                        {idx + 1}
                                    </span>
                                    <div className="flex-1 min-w-0">
                                        <p className="text-sm break-words">{p.prompt}</p>
                                        <p className="text-[11px] text-muted-foreground mt-0.5">
                                            {p.engine_count} 引擎命中 · {p.citation_count.toLocaleString()} 条引用
                                        </p>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => handleCopy(p.prompt)}
                                        className="shrink-0 text-xs text-muted-foreground hover:text-foreground p-1 rounded hover:bg-muted"
                                        title="复制 prompt"
                                    >
                                        <Copy className="h-3.5 w-3.5" />
                                    </button>
                                </div>
                            ))}
                        </div>
                        {items.length > 5 && (
                            <Button
                                variant="ghost"
                                size="sm"
                                className="w-full mt-2"
                                onClick={props.onToggleShowAll}
                            >
                                {props.showAll ? '收起' : `展开全部 (${items.length} 条)`}
                            </Button>
                        )}
                    </>
                )}
            </CardContent>
        </Card>
    );
}
