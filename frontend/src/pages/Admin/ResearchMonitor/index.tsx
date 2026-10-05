/**
 * GEO 调研监测后台 · 顶层壳子页面
 *
 * Phase 9 (2026-05-25) · 重设计完整版 (P07):
 *   - 删审核 tab (LLM 评分 + top 30 推审核 整套下线)
 *   - 加文章库 tab (替代审核 · 管理员直接看所有抓取原文 + 编辑 + 加入参考库)
 *
 * P12 (2026-05-26):
 *   - 行业管理 + Prompts 管理 合并 → "行业 & Prompts" (左选行业 / 右编 prompts)
 *
 * P14 (2026-05-27):
 *   - 新增 "引用明细" tab (管理员审计层 · 数据源 geo_research_raw)
 *   - 顺序: 跑批 → 行业&Prompts → 引用明细 → 文章库 → 配置 (从查询词追溯证据 · 再钻到文章库管理)
 *   - 支持 URL deep link: ?tab=citations&industry=房地产 (来自 GeoResearchCenter admin 跳转)
 *
 * 5 tab:
 *   - 跑批管理
 *   - 行业 & Prompts
 *   - 引用明细 (P14 · 仅 admin · 后端 3 endpoint 已加 _require_admin)
 *   - 文章库
 *   - 系统配置
 */
import { useEffect, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Badge } from '@/components/ui/badge';
import { Activity, BookOpenText, Database, FileSearch, Settings, ShieldCheck } from 'lucide-react';
import RoundsPanel from './RoundsPanel';
import IndustriesPromptsPanel from './IndustriesPromptsPanel';
import ArticlesPanel from './ArticlesPanel';
import ConfigPanel from './ConfigPanel';
import CitationsPanel from './CitationsPanel';

const VALID_TABS = ['rounds', 'industries_prompts', 'citations', 'articles', 'config'] as const;
type TabKey = typeof VALID_TABS[number];

const TAB_META: Record<TabKey, { label: string; helper: string; icon: ReactNode }> = {
    rounds: {
        label: '运行总览',
        helper: '查看最近跑批、失败原因、续跑入口和调研进度。',
        icon: <Activity className="h-4 w-4" />,
    },
    industries_prompts: {
        label: '行业题目',
        helper: '维护每个行业要问 AI 的题目,下次跑批自动生效。',
        icon: <BookOpenText className="h-4 w-4" />,
    },
    citations: {
        label: '引用证据',
        helper: '追溯 AI 回答引用了哪些平台、文章和来源。',
        icon: <FileSearch className="h-4 w-4" />,
    },
    articles: {
        label: '文章库',
        helper: '查看抓取后的文章样本,作为飞轮学习材料。',
        icon: <Database className="h-4 w-4" />,
    },
    config: {
        label: '系统设置',
        helper: '配置自动跑批时间、行业范围和系统参数。',
        icon: <Settings className="h-4 w-4" />,
    },
};

export default function ResearchMonitor() {
    const [searchParams, setSearchParams] = useSearchParams();

    // URL deep link: ?tab=citations&industry=房地产&query=xxx
    const urlTab = searchParams.get('tab');
    const initialTab: TabKey = (VALID_TABS as readonly string[]).includes(urlTab || '')
        ? (urlTab as TabKey)
        : 'rounds';
    const [tab, setTab] = useState<TabKey>(initialTab);

    const urlIndustry = searchParams.get('industry') || undefined;
    const urlQuery = searchParams.get('query') || undefined;
    // P14 v3 (MEDIUM fix): articles tab 支持 article_id deep link 从引用明细跳进来
    const urlArticleIdRaw = searchParams.get('article_id');
    const urlArticleId = urlArticleIdRaw && /^\d+$/.test(urlArticleIdRaw)
        ? Number(urlArticleIdRaw) : undefined;

    // P14 v2 (LOW1 fix): URL → state 双向同步
    //   - 前进/后退 / 外部 navigate / 重复点跳转按钮 都让 tab+industry+query 跟 URL 走
    //   - 不只是 mount 一次
    useEffect(() => {
        const urlTabNow = searchParams.get('tab');
        const nextTab: TabKey = (VALID_TABS as readonly string[]).includes(urlTabNow || '')
            ? (urlTabNow as TabKey)
            : 'rounds';
        if (nextTab !== tab) {
            setTab(nextTab);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [searchParams]);

    // tab 切换时同步 URL · 各 tab 的 deep link 参数仅在自己 tab 时保留
    useEffect(() => {
        const next = new URLSearchParams(searchParams);
        if (tab === 'rounds') {
            next.delete('tab');
        } else {
            next.set('tab', tab);
        }
        // 离开 citations tab 清掉 industry/query
        if (tab !== 'citations') {
            next.delete('industry');
            next.delete('query');
        }
        // 离开 articles tab 清掉 article_id (P14 v3)
        if (tab !== 'articles') {
            next.delete('article_id');
        }
        if (next.toString() !== searchParams.toString()) {
            setSearchParams(next, { replace: true });
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tab]);

    const current = TAB_META[tab];

    return (
        <div className="container mx-auto max-w-7xl space-y-5 p-4 sm:p-6">
            <section className="rounded-2xl border bg-card/70 p-5 shadow-sm">
                <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                    <div className="flex items-start gap-3">
                        <div className="hidden h-11 w-11 items-center justify-center rounded-2xl bg-emerald-500/10 text-emerald-600 sm:flex">
                            <Activity className="h-5 w-5" />
                        </div>
                        <div>
                            <div className="flex flex-wrap items-center gap-2">
                                <h1 className="text-2xl font-bold">GEO 调研监测</h1>
                                <Badge variant="secondary" className="bg-emerald-500/10 text-emerald-700">内部数据管线</Badge>
                            </div>
                            <p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">
                                管理 AI 调研跑批、行业题目、引用证据和文章样本。这里产出的都是飞轮学习材料,不会自动接管客户写作、投放、报价或扣费。
                            </p>
                        </div>
                    </div>
                    <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-900 dark:text-amber-100">
                        启用任何线上策略前必须人工审核。
                    </div>
                </div>
            </section>

            <Tabs value={tab} onValueChange={v => setTab(v as TabKey)} className="w-full">
                <section className="rounded-2xl border bg-card/70 p-3 shadow-sm sm:p-4">
                    <TabsList className="flex h-auto w-full flex-wrap justify-start gap-1 rounded-xl bg-muted/40 p-1">
                        {VALID_TABS.map(key => (
                            <TabsTrigger key={key} value={key} className="gap-1.5">
                                {TAB_META[key].icon}
                                {TAB_META[key].label}
                            </TabsTrigger>
                        ))}
                    </TabsList>

                    <div className="mt-4 rounded-xl border bg-background/60 p-4">
                        <div className="flex items-center gap-2">
                            <ShieldCheck className="h-4 w-4 text-emerald-600" />
                            <h2 className="font-semibold">{current.label}</h2>
                        </div>
                        <p className="mt-1 text-sm text-muted-foreground">{current.helper}</p>
                    </div>

                    <TabsContent value="rounds" className="mt-4">
                        <RoundsPanel />
                    </TabsContent>

                    <TabsContent value="industries_prompts" className="mt-4">
                        <IndustriesPromptsPanel />
                    </TabsContent>

                    <TabsContent value="citations" className="mt-4">
                        <CitationsPanel initialIndustry={urlIndustry} initialQuery={urlQuery} />
                    </TabsContent>

                    <TabsContent value="articles" className="mt-4">
                        <ArticlesPanel initialArticleId={urlArticleId} />
                    </TabsContent>

                    <TabsContent value="config" className="mt-4">
                        <ConfigPanel />
                    </TabsContent>
                </section>
            </Tabs>
        </div>
    );
}
