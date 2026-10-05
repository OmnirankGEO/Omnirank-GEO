/**
 * GEO 调研监测后台 · 引用明细面板 [P14 · 2026-05-27]
 *
 * 定位:
 *   - 管理员审计层 · 不是代理决策视图
 *   - 三层漏斗: 行业 → query → answer(按 engine 分组) → cites[]
 *
 * 严格 admin-only · 后端 3 个 endpoint 已加 _require_admin
 *
 * 数据流:
 *   1. 进页面拉 /citations/industries-summary (一次)
 *   2. 选行业后拉 /citations/queries?industry=xxx (带搜索 + 分页)
 *   3. 展开 query 后拉 /citations/query-detail (按 engine 分组 · 每组默认 5 个 answer)
 *
 * 决策点 (P14 拍板):
 *   - engine 归一显示 (豆包/Kimi/DeepSeek/千问) · 原始名放 tooltip
 *   - 每 engine 5 个 answer · "加载更多" 加 offset
 *   - answer_text 复用 ArticlesPanel 的 preprocessMarkdownForChinese (本文件 copy 一份 · 不做跨文件重构)
 *   - URL deep link: ?tab=citations&industry=xxx (encodeURIComponent)
 */
'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    Loader2, Search, ChevronDown, ChevronRight, ExternalLink, RefreshCw, X,
    Inbox, BookOpen,
} from 'lucide-react';
import ReactMarkdown from '@/components/SafeMarkdown';
import { preprocessMarkdownForChinese } from '@/lib/markdownPreprocess';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import {
    researchMonitorApi,
    CITATION_ENGINES,
    type CitationEngine,
    type CitationIndustrySummary,
    type CitationQuerySummary,
    type CitationQueryDetailResp,
    type CitationEngineGroup,
} from '@/lib/researchMonitorApi';
import { formatApiErrorForDisplay } from '@/lib/api';

// =========================================================================
// engine 配色 · 跟 GeoResearchCenter / ArticlesPanel 一致
// =========================================================================

const ENGINE_COLOR: Record<CitationEngine, string> = {
    '豆包':     'bg-blue-100 text-blue-800 border-blue-200',
    'Kimi':     'bg-orange-100 text-orange-800 border-orange-200',
    'DeepSeek': 'bg-green-100 text-green-800 border-green-200',
    '千问':     'bg-purple-100 text-purple-800 border-purple-200',
};

const QUERY_PAGE_SIZE = 50;

// =========================================================================
// preprocessMarkdownForChinese (2026-07-22 sink census F7: 已收口到
// @/lib/markdownPreprocess 与 ArticlesPanel 共享 · 语义未改 · 原 SOH/STX
// 占位符 swap 法 + 零宽空格修中文边界的实现见该文件)
// =========================================================================

// =========================================================================
// 工具
// =========================================================================

function formatRelative(iso: string | null): string {
    if (!iso) return '-';
    const t = new Date(iso).getTime();
    const d = Date.now() - t;
    if (d < 0) return '刚刚';
    const min = Math.floor(d / 60_000);
    if (min < 1) return '刚刚';
    if (min < 60) return `${min}分前`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h前`;
    const day = Math.floor(hr / 24);
    if (day < 30) return `${day}天前`;
    const mon = Math.floor(day / 30);
    if (mon < 12) return `${mon}月前`;
    return `${Math.floor(mon / 12)}年前`;
}

function safeDomain(url: string): string {
    try {
        const u = new URL(url);
        return u.hostname.replace(/^www\./, '');
    } catch { return url.slice(0, 40); }
}

// =========================================================================
// 接收 props: 支持 URL deep link 进来 (预选 industry / query)
// =========================================================================

interface CitationsPanelProps {
    initialIndustry?: string;
    initialQuery?: string;
}

export default function CitationsPanel({ initialIndustry, initialQuery }: CitationsPanelProps) {
    // ---- 行业列表 ----
    const [industries, setIndustries] = useState<CitationIndustrySummary[]>([]);
    const [industriesLoading, setIndustriesLoading] = useState(false);
    const [selectedIndustry, setSelectedIndustry] = useState<string | null>(initialIndustry ?? null);
    const [industrySearch, setIndustrySearch] = useState('');

    // P14 v2 (LOW1 fix): URL props 变化时同步 state · 支持前进/后退/重复 navigate
    useEffect(() => {
        if (initialIndustry && initialIndustry !== selectedIndustry) {
            setSelectedIndustry(initialIndustry);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [initialIndustry]);

    // ---- query 列表 ----
    const [queries, setQueries] = useState<CitationQuerySummary[]>([]);
    const [queriesTotal, setQueriesTotal] = useState(0);
    const [queriesLoading, setQueriesLoading] = useState(false);
    const [querySearch, setQuerySearch] = useState('');
    const [queryEngineFilter, setQueryEngineFilter] = useState<CitationEngine | ''>('');
    const [queryOffset, setQueryOffset] = useState(0);

    // ---- 展开的 query → 详情 ----
    const [expandedQuery, setExpandedQuery] = useState<string | null>(initialQuery ?? null);
    const [detailMap, setDetailMap] = useState<Record<string, CitationQueryDetailResp>>({});
    const [detailLoadingKey, setDetailLoadingKey] = useState<string | null>(null);

    // ============== load industries ==============
    const loadIndustries = useCallback(async () => {
        setIndustriesLoading(true);
        try {
            const resp = await researchMonitorApi.listCitationIndustries();
            setIndustries(resp.industries);
            // 如果 deep link 带的 industry 不存在 · 自动选第一个
            if (initialIndustry && !resp.industries.some(i => i.industry === initialIndustry)) {
                setSelectedIndustry(resp.industries[0]?.industry ?? null);
            } else if (!selectedIndustry && resp.industries.length > 0) {
                setSelectedIndustry(resp.industries[0].industry);
            }
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载行业失败', 'admin'));
        } finally {
            setIndustriesLoading(false);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    useEffect(() => { void loadIndustries(); }, [loadIndustries]);

    // ============== load queries ==============
    const loadQueries = useCallback(async () => {
        if (!selectedIndustry) { setQueries([]); setQueriesTotal(0); return; }
        setQueriesLoading(true);
        try {
            const resp = await researchMonitorApi.listCitationQueries({
                industry: selectedIndustry,
                q: querySearch || undefined,
                engine: queryEngineFilter || undefined,
                limit: QUERY_PAGE_SIZE,
                offset: queryOffset,
            });
            setQueries(resp.queries);
            setQueriesTotal(resp.total);
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载 query 失败', 'admin'));
            setQueries([]);
            setQueriesTotal(0);
        } finally {
            setQueriesLoading(false);
        }
    }, [selectedIndustry, querySearch, queryEngineFilter, queryOffset]);

    // 切行业重置分页 + 关展开
    useEffect(() => {
        setQueryOffset(0);
        setExpandedQuery(null);
        setDetailMap({});
    }, [selectedIndustry, querySearch, queryEngineFilter]);

    useEffect(() => { void loadQueries(); }, [loadQueries]);

    // ============== load query detail ==============
    const loadDetail = useCallback(async (q: string, engineFilter?: CitationEngine, append?: boolean) => {
        const key = q;  // 每个 query 缓存一份 detail
        setDetailLoadingKey(key);
        try {
            const existing = detailMap[key];
            const offset = append && existing && engineFilter
                ? (existing.engines.find(g => g.engine === engineFilter)?.answers.length ?? 0)
                : 0;
            const resp = await researchMonitorApi.getCitationQueryDetail({
                industry: selectedIndustry!,
                query: q,
                engine: engineFilter,
                engine_offset: offset,
                answers_per_engine: 5,
            });
            setDetailMap(prev => {
                // 全量替换或合并某个 engine 的 answers
                if (append && existing && engineFilter) {
                    const merged: CitationQueryDetailResp = {
                        ...existing,
                        engines: existing.engines.map(g => {
                            if (g.engine !== engineFilter) return g;
                            const fresh = resp.engines.find(x => x.engine === engineFilter);
                            if (!fresh) return g;
                            return {
                                ...g,
                                answers: [...g.answers, ...fresh.answers],
                                has_more: fresh.has_more,
                                total_answers: fresh.total_answers,
                            };
                        }),
                    };
                    return { ...prev, [key]: merged };
                }
                return { ...prev, [key]: resp };
            });
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载详情失败', 'admin'));
        } finally {
            setDetailLoadingKey(null);
        }
    }, [selectedIndustry, detailMap]);

    const toggleExpand = useCallback((q: string) => {
        if (expandedQuery === q) {
            setExpandedQuery(null);
        } else {
            setExpandedQuery(q);
            if (!detailMap[q]) void loadDetail(q);
        }
    }, [expandedQuery, detailMap, loadDetail]);

    // deep link initialQuery 自动展开
    // P14 v2 (LOW1 fix): initialQuery 变化时也重新跟随 · 不只是 mount 一次
    useEffect(() => {
        if (initialQuery && selectedIndustry && !detailMap[initialQuery] && !queriesLoading) {
            // 等 queries 加载完后自动展开
            if (queries.some(q => q.query === initialQuery)) {
                setExpandedQuery(initialQuery);
                void loadDetail(initialQuery);
            }
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [initialQuery, selectedIndustry, queries.length]);

    // ============== derived ==============
    const filteredIndustries = useMemo(() => {
        if (!industrySearch.trim()) return industries;
        const k = industrySearch.trim().toLowerCase();
        return industries.filter(i => i.industry.toLowerCase().includes(k));
    }, [industries, industrySearch]);

    const selectedSummary = useMemo(
        () => industries.find(i => i.industry === selectedIndustry) ?? null,
        [industries, selectedIndustry],
    );

    // =========================================================================
    // render
    // =========================================================================

    return (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12 lg:gap-5 items-start">
            {/* ============== LEFT · 行业列表 (3/12) · sticky ============== */}
            <div className="lg:col-span-3 space-y-2 lg:sticky lg:top-4 lg:self-start">
                <div className="flex items-center justify-between">
                    <h3 className="text-sm font-semibold">行业列表</h3>
                    <Button size="sm" variant="ghost" onClick={() => void loadIndustries()} disabled={industriesLoading}>
                        <RefreshCw className={`w-3.5 h-3.5 ${industriesLoading ? 'animate-spin' : ''}`} />
                    </Button>
                </div>
                <div className="relative">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
                    <Input
                        value={industrySearch}
                        onChange={e => setIndustrySearch(e.target.value)}
                        placeholder="搜索行业..."
                        className="pl-9 h-8 text-xs"
                    />
                </div>
                <div className="space-y-1 max-h-[calc(100vh-220px)] overflow-y-auto pr-1">
                    {industriesLoading && industries.length === 0 && (
                        <div className="flex justify-center py-6"><Loader2 className="w-4 h-4 animate-spin" /></div>
                    )}
                    {!industriesLoading && filteredIndustries.length === 0 && (
                        <div className="text-center py-6 text-xs text-muted-foreground">
                            {industrySearch ? '无匹配行业' : '暂无数据'}
                        </div>
                    )}
                    {filteredIndustries.map(stats => {
                        const selected = selectedIndustry === stats.industry;
                        return (
                            <button
                                key={stats.industry}
                                type="button"
                                onClick={() => setSelectedIndustry(stats.industry)}
                                className={`w-full text-left rounded-md border px-2.5 py-2 transition-colors ${
                                    selected ? 'border-brand bg-brand/5' : 'hover:bg-muted/40'
                                }`}
                            >
                                <div className="flex items-center justify-between gap-2">
                                    <span className="font-medium text-xs truncate">{stats.industry}</span>
                                    <span className="text-[10px] text-muted-foreground tabular-nums shrink-0">
                                        {stats.citation_count.toLocaleString()}
                                    </span>
                                </div>
                                <div className="text-[10px] text-muted-foreground mt-0.5">
                                    {stats.query_count} 词 · {stats.engine_count}/4 引擎 · {formatRelative(stats.last_cited_at)}
                                </div>
                            </button>
                        );
                    })}
                </div>
            </div>

            {/* ============== RIGHT · query 列表 + 展开详情 (9/12) ============== */}
            <div className="lg:col-span-9 space-y-3">
                {!selectedIndustry ? (
                    <div className="border rounded-lg py-16 text-center text-sm text-muted-foreground">
                        左侧选择行业
                    </div>
                ) : (
                    <>
                        {/* 行业 header */}
                        <div className="border rounded-lg p-4 bg-muted/20">
                            <div className="flex items-baseline justify-between">
                                <div>
                                    <h2 className="text-lg font-bold">{selectedIndustry}</h2>
                                    {selectedSummary && (
                                        <div className="text-xs text-muted-foreground mt-1">
                                            {selectedSummary.query_count} 个测试词 · {selectedSummary.engine_count}/4 引擎 · {selectedSummary.citation_count.toLocaleString()} 条引用
                                        </div>
                                    )}
                                </div>
                                <Button size="sm" variant="outline" onClick={() => void loadQueries()} disabled={queriesLoading}>
                                    <RefreshCw className={`w-3.5 h-3.5 mr-1 ${queriesLoading ? 'animate-spin' : ''}`} />
                                    刷新
                                </Button>
                            </div>
                        </div>

                        {/* 搜索 + engine 过滤 */}
                        <div className="flex items-center gap-2">
                            <div className="relative flex-1">
                                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
                                <Input
                                    value={querySearch}
                                    onChange={e => setQuerySearch(e.target.value)}
                                    placeholder="搜索查询词..."
                                    className="pl-9 h-9 text-sm"
                                />
                            </div>
                            <div className="flex items-center gap-1 text-xs">
                                <span className="text-muted-foreground mr-1">引擎:</span>
                                <button
                                    type="button"
                                    onClick={() => setQueryEngineFilter('')}
                                    className={`px-2 py-1 rounded border text-xs ${
                                        queryEngineFilter === ''
                                            ? 'bg-brand text-white border-brand'
                                            : 'hover:bg-muted/40'
                                    }`}
                                >
                                    全部
                                </button>
                                {CITATION_ENGINES.map(eng => (
                                    <button
                                        key={eng}
                                        type="button"
                                        onClick={() => setQueryEngineFilter(queryEngineFilter === eng ? '' : eng)}
                                        className={`px-2 py-1 rounded border text-xs ${
                                            queryEngineFilter === eng
                                                ? ENGINE_COLOR[eng] + ' ring-1 ring-current'
                                                : 'hover:bg-muted/40'
                                        }`}
                                    >
                                        {eng}
                                    </button>
                                ))}
                            </div>
                        </div>

                        {/* query 列表 */}
                        <div className="border rounded-lg divide-y">
                            {queriesLoading && queries.length === 0 && (
                                <div className="flex justify-center py-10"><Loader2 className="w-5 h-5 animate-spin" /></div>
                            )}
                            {!queriesLoading && queries.length === 0 && (
                                <div className="text-center py-10 text-sm text-muted-foreground flex flex-col items-center gap-2">
                                    <Inbox className="w-8 h-8 opacity-30" />
                                    {querySearch || queryEngineFilter ? '无匹配' : '该行业暂无引用记录'}
                                </div>
                            )}
                            {queries.map(qSum => {
                                const isExpanded = expandedQuery === qSum.query;
                                const detail = detailMap[qSum.query];
                                return (
                                    <div key={qSum.query} className="hover:bg-muted/30 transition-colors">
                                        {/* 行首: 点开展开 */}
                                        <button
                                            type="button"
                                            onClick={() => toggleExpand(qSum.query)}
                                            className="w-full text-left px-4 py-3 flex items-start gap-3"
                                        >
                                            <div className="mt-0.5 shrink-0">
                                                {isExpanded
                                                    ? <ChevronDown className="w-4 h-4 text-muted-foreground" />
                                                    : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
                                            </div>
                                            <div className="flex-1 min-w-0">
                                                <div className="font-medium text-sm">{qSum.query}</div>
                                                <div className="text-xs text-muted-foreground mt-1">
                                                    {qSum.engine_count} 引擎 · {qSum.citation_count} 条引用
                                                </div>
                                            </div>
                                            <div className="flex items-center gap-1.5 shrink-0">
                                                {CITATION_ENGINES.map(eng => {
                                                    const cnt = qSum.engines[eng];
                                                    if (!cnt) return null;
                                                    return (
                                                        <Badge
                                                            key={eng}
                                                            variant="outline"
                                                            className={`text-[10px] px-1.5 py-0.5 ${ENGINE_COLOR[eng]}`}
                                                        >
                                                            {eng} ({cnt})
                                                        </Badge>
                                                    );
                                                })}
                                            </div>
                                        </button>

                                        {/* 展开后 · detail */}
                                        {isExpanded && (
                                            <div className="px-4 pb-4 pt-1 border-t bg-muted/10">
                                                {detailLoadingKey === qSum.query && !detail && (
                                                    <div className="flex justify-center py-6">
                                                        <Loader2 className="w-4 h-4 animate-spin" />
                                                    </div>
                                                )}
                                                {detail && (
                                                    <div className="space-y-4">
                                                        {detail.engines.filter(g => g.total_answers > 0).map(group => (
                                                            <EngineGroupBlock
                                                                key={group.engine}
                                                                group={group}
                                                                onLoadMore={() => void loadDetail(qSum.query, group.engine, true)}
                                                                loading={detailLoadingKey === qSum.query}
                                                            />
                                                        ))}
                                                        {detail.engines.every(g => g.total_answers === 0) && (
                                                            <div className="text-xs text-muted-foreground text-center py-4">
                                                                无回答数据
                                                            </div>
                                                        )}
                                                    </div>
                                                )}
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                        </div>

                        {/* 分页 */}
                        {queriesTotal > QUERY_PAGE_SIZE && (
                            <div className="flex items-center justify-between text-xs text-muted-foreground">
                                <div>共 {queriesTotal} 个查询词 · 当前 {queryOffset + 1} ~ {Math.min(queryOffset + QUERY_PAGE_SIZE, queriesTotal)}</div>
                                <div className="flex gap-2">
                                    <Button size="sm" variant="outline" disabled={queryOffset === 0 || queriesLoading}
                                        onClick={() => setQueryOffset(Math.max(0, queryOffset - QUERY_PAGE_SIZE))}>
                                        上一页
                                    </Button>
                                    <Button size="sm" variant="outline"
                                        disabled={queryOffset + QUERY_PAGE_SIZE >= queriesTotal || queriesLoading}
                                        onClick={() => setQueryOffset(queryOffset + QUERY_PAGE_SIZE)}>
                                        下一页
                                    </Button>
                                </div>
                            </div>
                        )}
                    </>
                )}
            </div>
        </div>
    );
}

// =========================================================================
// 子组件: 单 engine 分组 (answers + cites)
// =========================================================================

function EngineGroupBlock({
    group, onLoadMore, loading,
}: {
    group: CitationEngineGroup;
    onLoadMore: () => void;
    loading: boolean;
}) {
    return (
        <div className="border rounded-md bg-background">
            <div className={`px-3 py-2 flex items-center justify-between border-b ${ENGINE_COLOR[group.engine]} bg-opacity-30`}>
                <div className="flex items-center gap-2">
                    <span className="font-semibold text-sm">{group.engine}</span>
                    <span className="text-xs">共 {group.total_answers} 个回答 · 显示 {group.answers.length}</span>
                </div>
            </div>
            <div className="divide-y">
                {group.answers.map((ans, idx) => (
                    <AnswerCard key={`${ans.answer_md5}_${idx}`} answer={ans} />
                ))}
            </div>
            {group.has_more && (
                <div className="px-3 py-2 border-t flex justify-center">
                    <Button size="sm" variant="ghost" onClick={onLoadMore} disabled={loading}>
                        {loading
                            ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1" />
                            : null}
                        加载更多 ({group.total_answers - group.answers.length} 个)
                    </Button>
                </div>
            )}
        </div>
    );
}

function AnswerCard({ answer }: { answer: import('@/lib/researchMonitorApi').CitationAnswer }) {
    const navigate = useNavigate();
    const [citesOpen, setCitesOpen] = useState(false);
    // P14 v3 (老板反馈): 默认收起回答 · 一个 query 下 N 个回答展平太挤 · 想看再展开
    const [textOpen, setTextOpen] = useState(false);

    // 时间作主标识 · 中文友好格式 'YYYY-MM-DD HH:mm' · batch_id / raw_engine 收到 hover tooltip 排查用
    const dt = answer.created_at ? new Date(answer.created_at) : null;
    const dateLabel = dt
        ? `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')} ${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`
        : '时间未知';
    const debugTip = `批次 batch_id: ${answer.batch_id ?? '-'}\n原始 engine 字段: ${answer.raw_engine ?? '-'}`;

    return (
        <div className="px-3 py-3 space-y-2">
            <div className="flex items-center justify-between text-xs">
                <span
                    className="text-foreground/80 tabular-nums cursor-help"
                    title={debugTip}
                >
                    {dateLabel}
                </span>
                <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]"
                    onClick={() => setTextOpen(o => !o)}>
                    {textOpen ? <X className="w-3 h-3 mr-1" /> : null}
                    {textOpen ? '收起回答' : '展开回答'}
                </Button>
            </div>

            {textOpen && (
                <div className="prose prose-sm max-w-none p-3 bg-muted/30 rounded text-sm leading-relaxed
                    [&_h1]:text-base [&_h2]:text-sm [&_h3]:text-sm [&_h4]:text-xs
                    [&_p]:my-1.5 [&_ul]:my-1.5 [&_ol]:my-1.5 [&_li]:my-0.5">
                    <ReactMarkdown>
                        {preprocessMarkdownForChinese(answer.answer_text || '*(空回答)*')}
                    </ReactMarkdown>
                </div>
            )}

            <div>
                <button
                    type="button"
                    onClick={() => setCitesOpen(o => !o)}
                    className="text-xs text-brand hover:underline flex items-center gap-1"
                >
                    {citesOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                    {answer.cite_count} 条引用来源
                </button>
                {citesOpen && (
                    <div className="mt-2 space-y-1.5">
                        {answer.cites.length === 0 && (
                            <div className="text-xs text-muted-foreground">无引用来源</div>
                        )}
                        {answer.cites.map(c => (
                            <div key={c.raw_id} className="text-xs border-l-2 border-muted pl-2 py-1">
                                <div className="flex items-start gap-2">
                                    <span className="text-muted-foreground tabular-nums shrink-0">#{c.position ?? '?'}</span>
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center gap-1.5 flex-wrap">
                                            {c.platform && (
                                                <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{c.platform}</Badge>
                                            )}
                                            <span className="text-muted-foreground text-[10px]">{safeDomain(c.url)}</span>
                                            {/* P14 v2 · 有 article_id 时显示跳文章库按钮 (deep link 由 ArticlesPanel 后续消费) */}
                                            {c.article_id != null && (
                                                <button
                                                    type="button"
                                                    onClick={() => navigate(`/admin/research-monitor?tab=articles&article_id=${c.article_id}`)}
                                                    className="inline-flex items-center gap-0.5 text-[10px] px-1.5 py-0 rounded
                                                        bg-brand/10 text-brand hover:bg-brand/20 transition-colors"
                                                    title={`跳到文章库查看文章 #${c.article_id}`}
                                                >
                                                    <BookOpen className="w-2.5 h-2.5" />
                                                    文章库 #{c.article_id}
                                                </button>
                                            )}
                                        </div>
                                        {c.title && (
                                            <a href={c.url} target="_blank" rel="noopener noreferrer"
                                                className="text-foreground hover:text-brand hover:underline mt-0.5 block">
                                                {c.title}
                                                <ExternalLink className="inline w-3 h-3 ml-1" />
                                            </a>
                                        )}
                                        {!c.title && (
                                            <a href={c.url} target="_blank" rel="noopener noreferrer"
                                                className="text-muted-foreground hover:text-brand hover:underline mt-0.5 block text-[11px] truncate">
                                                {c.url}
                                            </a>
                                        )}
                                        {c.excerpt && (
                                            <div className="text-muted-foreground text-[11px] mt-1 line-clamp-3">
                                                {c.excerpt}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}
