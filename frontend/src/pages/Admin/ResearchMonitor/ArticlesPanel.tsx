/**
 * GEO 调研监测后台 · 文章库面板 [P13 · 2026-05-26 重设计]
 *
 * 设计参考: 老板给的 AI回答爬虫本地工具的文章库视图
 *   - 顶部横向批次条 (前天 06:22 · 630 篇 · 当前选中黑底白字)
 *   - 左侧文章列表 · content_type chips 含数量 · 极简 card (标题+类型+来源+域名+字数+引擎chips+被N家引)
 *   - 右侧详情面板 (替代原弹窗 Dialog · 选中后直接展示 4 tab)
 *
 * 视觉优化 (在参考图基础上加):
 *   - 引擎 chip 4 色 (跟 GeoResearchCenter ENGINE_COLORS 同色: 豆包蓝/Kimi橙/DeepSeek绿/千问紫)
 *   - publisher 友好名 (cj.sina.cn → 新浪 · mp.weixin.qq.com → 微信公众号) · 前端 hash 50 域名
 *   - card hover + selected 边框 brand 色
 *   - 数字 tabular-nums + 圆角小 chip
 *
 * 后端依赖 (P13a · api/research_monitor_articles_api.py 已加):
 *   - GET /articles 返回 cited_by_platforms[] + counts_by_content_type{}
 *   - GET /articles/batches 返回 batches 列表
 */
'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Loader2, RefreshCw, Search, Trash2, Download, FileEdit, Check, X,
    Inbox, ChevronLeft, ChevronRight, ExternalLink, Layers, Tag, BarChart3,
} from 'lucide-react';
import { toast } from 'sonner';
import ReactMarkdown from '@/components/SafeMarkdown';
import { preprocessMarkdownForChinese } from '@/lib/markdownPreprocess';
// P13-v6 (2026-05-27): v4 API 用 Group/Panel/Separator (旧 v0 是 PanelGroup/PanelResizeHandle)
import { Group as PanelGroup, Panel, Separator as PanelResizeHandle } from 'react-resizable-panels';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
    Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
// P13-v13: 窄屏单列 + Dialog 装详情
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import {
    researchMonitorApi,
    type ArticleInLibrary,
    type ArticleDetailFull,
    type ArticleDaySummary,
    type ContentType,
    type ArticleIntentType,
    type ArticleIntentDistributionResponse,
    CONTENT_TYPE_LABEL,
    ARTICLE_INTENT_LABEL,
} from '@/lib/researchMonitorApi';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { formatApiErrorForDisplay } from '@/lib/api';

// =========================================================================
// 常量
// =========================================================================

const PAGE_SIZE = 30;

const CONTENT_TYPES: Array<ContentType | 'all'> = [
    'all', 'article', 'video', 'doc_tool', 'encyc', 'ecom', 'gov', 'other',
];

const CONTENT_TYPE_BADGE_COLOR: Record<ContentType, string> = {
    article:  'bg-blue-100 text-blue-700',
    video:    'bg-pink-100 text-pink-700',
    doc_tool: 'bg-cyan-100 text-cyan-700',
    encyc:    'bg-emerald-100 text-emerald-700',
    ecom:     'bg-purple-100 text-purple-700',
    gov:      'bg-slate-200 text-slate-700',
    other:    'bg-amber-100 text-amber-700',
};

const INTENT_TYPE_BADGE_COLOR: Record<ArticleIntentType, string> = {
    ranking:     'bg-amber-100 text-amber-800',
    tutorial:    'bg-emerald-100 text-emerald-800',
    long_form:   'bg-blue-100 text-blue-800',
    comparison:  'bg-purple-100 text-purple-800',
    data_report: 'bg-cyan-100 text-cyan-800',
    policy:      'bg-slate-200 text-slate-800',
    definition:  'bg-lime-100 text-lime-800',
    faq:         'bg-rose-100 text-rose-800',
};

// 后端 cited_by_platforms 元素值 → UI 友好名 + 颜色
// 跟 GeoResearchCenter ENGINE_COLORS 系列保持一致 · 兼容 lowercase / 中文混存
const ENGINE_DISPLAY: Record<string, { label: string; color: string }> = {
    doubao:   { label: 'doubao',   color: 'bg-blue-100 text-blue-700' },
    豆包:     { label: 'doubao',   color: 'bg-blue-100 text-blue-700' },
    kimi:     { label: 'kimi',     color: 'bg-orange-100 text-orange-700' },
    Kimi:     { label: 'kimi',     color: 'bg-orange-100 text-orange-700' },
    deepseek: { label: 'deepseek', color: 'bg-green-100 text-green-700' },
    DeepSeek: { label: 'deepseek', color: 'bg-green-100 text-green-700' },
    qwen:     { label: 'qwen',     color: 'bg-purple-100 text-purple-700' },
    千问:     { label: 'qwen',     color: 'bg-purple-100 text-purple-700' },
};

const ENGINE_FALLBACK_COLOR = 'bg-gray-100 text-gray-700';

// domain → publisher 友好名 (常见 50+ · 找不到就显 domain)
// 加新的: 直接往下加 entry · 后端不动
const PUBLISHER_MAP: Record<string, string> = {
    // 资讯门户
    'cj.sina.cn':           '新浪',
    'finance.sina.com.cn':  '新浪',
    'news.sina.com.cn':     '新浪',
    'sina.com.cn':          '新浪',
    'news.sohu.com':        '搜狐',
    'sohu.com':             '搜狐',
    'news.163.com':         '网易',
    '163.com':              '网易',
    'news.qq.com':          '腾讯',
    'tencent.com':          '腾讯',
    'thepaper.cn':          '澎湃新闻',
    'jiemian.com':          '界面新闻',
    '36kr.com':             '36 氪',
    // 社媒/UGC
    'zhihu.com':            '知乎',
    'baijiahao.baidu.com':  '百家号',
    'mp.weixin.qq.com':     '微信公众号',
    'xiaohongshu.com':      '小红书',
    'douyin.com':           '抖音',
    'iesdouyin.com':        '抖音',
    'iqiyi.com':            '爱奇艺',
    'bilibili.com':         'B 站',
    'b23.tv':               'B 站',
    'weibo.com':            '微博',
    // 头条系
    'toutiao.com':          '今日头条',
    'm.toutiao.com':        '今日头条',
    // 百科/工具
    'baike.baidu.com':      '百度百科',
    'wikipedia.org':        '维基百科',
    'zh.wikipedia.org':     '维基百科',
    'douban.com':           '豆瓣',
    // 行业
    'fang.com':             '搜房网',
    'fangtianxia.com':      '房天下',
    'lianjia.com':          '链家',
    'beike.com':            '贝壳',
    'autohome.com.cn':      '汽车之家',
    'pcauto.com.cn':        '太平洋汽车',
    'bitauto.com':          '易车',
    'dxy.com':              '丁香园',
    'haodf.com':            '好大夫',
    'ydyy.cn':              '医问医答',
    // 电商
    'jd.com':               '京东',
    'taobao.com':           '淘宝',
    'tmall.com':            '天猫',
    'pinduoduo.com':        '拼多多',
    'suning.com':           '苏宁',
    // 政府/官方
    'gov.cn':               '政府门户',
    // 技术社区
    'csdn.net':             'CSDN',
    'cnblogs.com':          '博客园',
    'juejin.cn':            '掘金',
    'segmentfault.com':     '思否',
};

function publisherOf(domain: string): string {
    if (!domain) return '—';
    // 精确匹配
    if (PUBLISHER_MAP[domain]) return PUBLISHER_MAP[domain];
    // 后缀匹配 (子域名 fallback)
    for (const key of Object.keys(PUBLISHER_MAP)) {
        if (domain.endsWith('.' + key) || domain === key) return PUBLISHER_MAP[key];
    }
    return domain;
}

// P13-v14 (2026-05-27 review fix): 监听窄屏断点 · 跟 Tailwind `lg:` (1024px) 对齐
// 老板第 6 轮 review 指出 Radix Dialog 用 Portal 到 body · `lg:hidden` 容器关不住它
// 桌面端点击文章会同时显: PanelGroup 详情 + Dialog 弹窗遮罩
// 修: Dialog open 必须用 useIsNarrowScreen() 真实 viewport 判断 · 而不是依赖 CSS class
function useIsNarrowScreen(): boolean {
    const [isNarrow, setIsNarrow] = useState(false);
    useEffect(() => {
        const mql = window.matchMedia('(max-width: 1023px)');  // < lg
        const update = () => setIsNarrow(mql.matches);
        update();
        mql.addEventListener('change', update);
        return () => mql.removeEventListener('change', update);
    }, []);
    return isNarrow;
}


// P13-v9 (2026-05-27 老板): 中文 markdown 预处理
// CommonMark 规范要求 ** 两侧有 ASCII boundary (left/right flanking) 才识别为 emphasis
// 中文字符紧贴 ** 时常不被识别 · 显示成字面的 `**共识1：**`
// 修: 用 regex 在 **X** / *X* 两侧插入零宽空格 ​ · 强制 boundary 识别
// 也修 # / ## 后没空格的情况 (CommonMark 要求 # 后必须空格才是 heading)
//
// P13-v10 追加 (2026-05-27): cleaned 内容有 '1****肺结节' 这种 4+ 连续星号
//   原始可能是 '1. **肺结节**' 清洗后被改成 '1****肺结节**'
//   修: 先把 *{3,} 折叠成 ** · 让 emphasis regex 接管
// P13-v11 (2026-05-27 老板): cleaned 显示要"跟原网址一样漂亮" · 编辑才显 raw
// 核心: 用占位符 swap 法保护配对 ** 再删孤立 ** · 让残留字面 ** 不再出现
//
// 流程:
//   step -1: 行首"数字 + 多星 + 内容"整段转 H3 标题 (1****X / 2.1****<6 mm)
//   step  0: 3+ 连续星号折叠为 ** (****X**** → **X**)
//   step  1: 配对 **X** 转占位符 (保护) → SOH X STX
//   step  2: 单星 *X* 同样占位符保护 (避免被 step 3 误删)
//   step  3: 删所有残留孤立的 ** 和 * (没匹配上的)
//   step  4: 占位符还原为 **​X​** (零宽空格强制 CommonMark 识别中文边界)
//   step  5: heading # 后补空格 (#标题 → # 标题)
//
// SOH(\x01) / STX(\x02) 是 ASCII 控制字符 · 正文几乎不会出现 · 用作临时占位
// 2026-07-22 sink census F6: 实现已收口到 @/lib/markdownPreprocess(与 CitationsPanel 共享,语义未改)


// P13-v3 (2026-05-26 老板): 顶部按天 · 不再显示时分
// 输入 YYYY-MM-DD · 输出 '今天' / '昨天' / '前天' / '5/23' / '4/30'
function formatDayLabel(dayStr: string): string {
    const [y, m, d] = dayStr.split('-').map(Number);
    if (!y || !m || !d) return dayStr;
    const target = new Date(y, m - 1, d);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const dayAgo = (n: number) => new Date(today.getTime() - n * 86400e3);
    const sameDay = (a: Date, b: Date) =>
        a.getFullYear() === b.getFullYear()
        && a.getMonth() === b.getMonth()
        && a.getDate() === b.getDate();
    if (sameDay(target, today)) return '今天';
    if (sameDay(target, dayAgo(1))) return '昨天';
    if (sameDay(target, dayAgo(2))) return '前天';
    // 跨年显年份 · 同年只显 M/D
    if (target.getFullYear() === today.getFullYear()) return `${m}/${d}`;
    return `${y}/${m}/${d}`;
}

function safeMarkdownFilename(title: string | null | undefined, id: number): string {
    let name = (title || `article_${id}`)
        .replace(/[\x00-\x1f<>:"/\\|?*]/g, '_')
        .replace(/[. ]+$/g, '')
        .trim();
    if (!name) name = `article_${id}`;
    if (name.length > 80) name = name.slice(0, 80);
    return `${name}.md`;
}

function triggerBlobDownload(content: BlobPart | Blob, filename: string, mimeType: string): void {
    const blob = content instanceof Blob ? content : new Blob([content], { type: mimeType });
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(href);
}

function buildArticleMarkdown(d: ArticleDetailFull): string | null {
    const a = d.article;
    const body = (d.cleaned_content || '').trim() || (d.raw_content || '').trim();
    if (!body) return null;

    const engines = Array.from(new Set([
        ...(a.cited_by_platforms ?? []),
        ...(d.citations ?? []).map(c => c.platform).filter(Boolean),
    ])).join(' / ') || '—';

    const contentType = a.content_type ? CONTENT_TYPE_LABEL[a.content_type] : '—';
    const intentType = a.intent_type ? ARTICLE_INTENT_LABEL[a.intent_type] : '—';
    const meta = [
        `来源：${a.url || '—'}`,
        `域名：${a.domain || '—'}`,
        `行业：${a.primary_industry || '—'}`,
        `内容类型：${contentType}`,
        `文章意图：${intentType}`,
        `字数：${a.cleaned_char_count ?? a.raw_char_count ?? '—'}`,
        `抓取时间：${a.fetched_at || '—'}`,
        `引用引擎：${engines}`,
    ].join('\n');

    return `# ${a.title || '未命名文章'}\n\n${meta}\n\n---\n\n${body}\n`;
}
// =========================================================================
// 主组件
// =========================================================================
interface ArticlesPanelProps {
    /** P14 v3 (MEDIUM fix): 来自 ?article_id=N · 引用明细跳过来时自动打开该文章详情 */
    initialArticleId?: number;
}

export default function ArticlesPanel({ initialArticleId }: ArticlesPanelProps = {}) {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    // P13-v14: 窄屏检测 (跟 Tailwind `lg:` 1024px 对齐 · 修 Dialog portal 桌面误显)
    const isNarrow = useIsNarrowScreen();

    // ---- 列表 + 天 (P13-v3 老板拍板: 按天聚合 替代单 batch 视图) ----
    const [days, setDays] = useState<ArticleDaySummary[]>([]);
    const [loadingDays, setLoadingDays] = useState(false);
    const [selectedDay, setSelectedDay] = useState<string | null>(null);   // YYYY-MM-DD · null=全部天

    const [articles, setArticles] = useState<ArticleInLibrary[]>([]);
    const [total, setTotal] = useState(0);
    const [countsByCT, setCountsByCT] = useState<Record<string, number>>({});
    const [intentDistribution, setIntentDistribution] = useState<ArticleIntentDistributionResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [loadingIntentDistribution, setLoadingIntentDistribution] = useState(false);

    // ---- 筛选 ----
    const [contentTypeFilter, setContentTypeFilter] = useState<ContentType | 'all'>('all');
    // P13-v2 (2026-05-26 老板): 行业筛选改成左栏 chip 选择 (不是手输框)
    // null = 全部行业 · string = 该批次下某具体行业
    // 选批次时自动重置 · 切批次重选行业
    const [selectedIndustry, setSelectedIndustry] = useState<string | null>(null);
    const [minChars, setMinChars] = useState<string>('');
    const [keyword, setKeyword] = useState<string>('');
    const [showSkipped, setShowSkipped] = useState(false);
    const [showImported, setShowImported] = useState(true);
    const [sort, setSort] = useState<'citations_desc' | 'chars_desc' | 'fetched_desc'>('citations_desc');
    const [offset, setOffset] = useState(0);

    // ---- 选中(批量) ----
    const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

    // ---- 右侧详情 ----
    // P14 v3 (MEDIUM fix): 初始化时从 prop 接住引用明细跳来的 article_id
    const [selectedArticleId, setSelectedArticleId] = useState<number | null>(initialArticleId ?? null);
    const [detail, setDetail] = useState<ArticleDetailFull | null>(null);
    const [detailLoading, setDetailLoading] = useState(false);

    // P14 v3 (MEDIUM fix): initialArticleId 变化时跟随
    //   - 用户从引用明细 #N 跳进来 → 自动打开 N 的详情
    //   - 后续在引用明细再点 #M → URL article_id 变 → prop 变 → 自动打开 M
    //   - 同时清掉 day/industry/content_type 过滤 · 避免 "该 article 不在当前过滤下 · 列表空" 的误解
    //     (right pane 详情走 getArticleDetail 不依赖列表 · 但列表显示 0 篇会让管理员迷惑)
    useEffect(() => {
        if (initialArticleId && initialArticleId !== selectedArticleId) {
            setSelectedArticleId(initialArticleId);
            setSelectedDay(null);
            setSelectedIndustry(null);
            setContentTypeFilter('all');
            setKeyword('');
            setMinChars('');
            setOffset(0);
            setShowSkipped(true);
            setShowImported(true);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [initialArticleId]);

    // ---- 编辑态 ----
    const [editing, setEditing] = useState(false);
    const [editedContent, setEditedContent] = useState('');
    const [editNote, setEditNote] = useState('');
    const [savingEdit, setSavingEdit] = useState(false);
    // ---- API loaders ----
    const loadDays = useCallback(async () => {
        setLoadingDays(true);
        try {
            // P13-v13 (review fix): limit 30 → 365 · 让"全部天" 行业聚合跟全库文章列表口径一致
            // 覆盖一年数据 · 防 30 天后两边对不上
            const res = await researchMonitorApi.listArticleDays({ limit: 365 });
            setDays(res.days ?? []);
        } catch (e) {
            console.error('加载天列表失败', e);
        } finally {
            setLoadingDays(false);
        }
    }, []);

    const buildArticleFilterParams = useCallback((includePaging: boolean) => {
        const status_values: string[] = ['in_library'];
        if (showSkipped) status_values.push('auto_skipped');
        if (showImported) status_values.push('imported_to_reference');

        const params: Parameters<typeof researchMonitorApi.listArticles>[0] = {
            review_status: status_values.join(','),
        };
        if (includePaging) {
            params.sort = sort;
            params.limit = PAGE_SIZE;
            params.offset = offset;
        }
        if (selectedDay) {
            const day = days.find(d => d.day === selectedDay);
            if (day && day.batch_ids.length > 0) {
                params.batch_ids = day.batch_ids.join(',');
            }
        }
        if (contentTypeFilter !== 'all') params.content_type = contentTypeFilter;
        if (selectedIndustry) params.industry = selectedIndustry;
        if (minChars && Number(minChars) > 0) params.min_chars = Number(minChars);
        if (keyword.trim()) params.keyword = keyword.trim();
        return params;
    }, [selectedDay, days, contentTypeFilter, selectedIndustry, minChars, keyword,
        showSkipped, showImported, sort, offset]);

    const loadArticles = useCallback(async () => {
        setLoading(true);
        try {
            const res = await researchMonitorApi.listArticles(buildArticleFilterParams(true));
            setArticles(res.articles);
            setTotal(res.total);
            setCountsByCT(res.counts_by_content_type ?? {});
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载文章库失败', 'admin'));
        } finally {
            setLoading(false);
        }
    }, [buildArticleFilterParams]);

    const loadIntentDistribution = useCallback(async () => {
        setLoadingIntentDistribution(true);
        try {
            const res = await researchMonitorApi.getArticleIntentDistribution(buildArticleFilterParams(false));
            setIntentDistribution(res);
        } catch (e) {
            console.error('加载文章意图分布失败', e);
            setIntentDistribution(null);
        } finally {
            setLoadingIntentDistribution(false);
        }
    }, [buildArticleFilterParams]);

    const loadDetail = useCallback(async (id: number) => {
        setDetailLoading(true);
        setDetail(null);
        setEditing(false);
        try {
            const d = await researchMonitorApi.getArticleDetail(id);
            setDetail(d);
            setEditedContent(d.cleaned_content);
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载详情失败', 'admin'));
            setSelectedArticleId(null);
        } finally {
            setDetailLoading(false);
        }
    }, []);

    useEffect(() => { void loadDays(); }, [loadDays]);
    useEffect(() => { void loadArticles(); }, [loadArticles]);
    useEffect(() => { void loadIntentDistribution(); }, [loadIntentDistribution]);
    useEffect(() => {
        if (selectedArticleId == null) {
            setDetail(null);
            return;
        }
        void loadDetail(selectedArticleId);
    }, [selectedArticleId, loadDetail]);

    // ---- Actions ----
    const saveEdit = async () => {
        if (!detail) return;
        setSavingEdit(true);
        try {
            const res = await researchMonitorApi.editArticle(detail.article.id, {
                cleaned_content: editedContent,
                edit_note: editNote || undefined,
            });
            toast.success(`保存成功 · 新字数 ${res.cleaned_char_count}`);
            setEditing(false);
            setEditNote('');
            await loadDetail(detail.article.id);
            void loadArticles();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '保存失败', 'admin'));
        } finally {
            setSavingEdit(false);
        }
    };

    const updateIntentType = async (intentType: ArticleIntentType | null) => {
        if (!detail) return;
        try {
            await researchMonitorApi.updateArticleIntent(detail.article.id, {
                intent_type: intentType,
                note: intentType ? '文章库详情手动修正' : '文章库详情清空文章意图',
            });
            toast.success(intentType ? `已标为 ${ARTICLE_INTENT_LABEL[intentType]}` : '已清空文章意图');
            await loadDetail(detail.article.id);
            void loadArticles();
            void loadIntentDistribution();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '修正失败', 'admin'));
        }
    };

    // P14.5 (2026-06-01 老板): 下载文章为 Markdown
    // 优先用清洗后内容 · 没有清洗就用原文 · 都没就跳过 (单篇时 toast error)
    const downloadMarkdown = (id: number) => {
        if (!detail || detail.article.id !== id) {
            toast.error('详情未加载 · 先在列表选中文章');
            return;
        }
        const md = buildArticleMarkdown(detail);
        if (md == null) {
            toast.error('暂无可下载内容 · 清洗后和原文都为空');
            return;
        }
        const filename = safeMarkdownFilename(detail.article.title, detail.article.id);
        triggerBlobDownload(md, filename, 'text/markdown;charset=utf-8');
        toast.success(`已下载 ${filename}`);
    };

    // P14.5: 批量下载 N 篇为 zip · 并发 4 拉详情防打爆后端 · 文件名冲突 _2/_3
    // 后端列表 API 不带正文 → 必须逐个 GET /articles/{id} · N 大时进度 toast 必备
    const bulkDownloadMarkdown = async () => {
        if (selectedIds.size === 0) return;
        const ids = [...selectedIds];
        const total = ids.length;
        const toastId = toast.loading(`准备下载 ${total} 篇 · 0/${total}`);
        try {
            const { default: JSZip } = await import('jszip');
            const zip = new JSZip();
            const usedNames = new Map<string, number>();
            let ok = 0, skipped = 0, failed = 0;
            let nextIdx = 0;
            const concurrency = 4;

            await Promise.all(
                Array.from({ length: concurrency }, async () => {
                    while (true) {
                        const i = nextIdx++;
                        if (i >= ids.length) return;
                        const articleId = ids[i];
                        try {
                            const d = await researchMonitorApi.getArticleDetail(articleId);
                            const md = buildArticleMarkdown(d);
                            if (md == null) { skipped++; continue; }
                            const base = safeMarkdownFilename(d.article.title, d.article.id).replace(/\.md$/, '');
                            const used = usedNames.get(base) ?? 0;
                            const filename = used === 0 ? `${base}.md` : `${base}_${used + 1}.md`;
                            usedNames.set(base, used + 1);
                            zip.file(filename, md);
                            ok++;
                        } catch {
                            failed++;
                        } finally {
                            const done = ok + skipped + failed;
                            toast.loading(
                                `下载中 · ${done}/${total} (成功 ${ok} 跳过 ${skipped} 失败 ${failed})`,
                                { id: toastId },
                            );
                        }
                    }
                }),
            );

            if (ok === 0) {
                toast.error(`无可下载内容 · 跳过 ${skipped} 失败 ${failed}`, { id: toastId });
                return;
            }
            const blob = await zip.generateAsync({ type: 'blob' });
            const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
            triggerBlobDownload(blob, `articles_${ts}.zip`, 'application/zip');
            toast.success(`已下载 ${ok} 篇 (zip) · 跳过 ${skipped} · 失败 ${failed}`, { id: toastId });
            setSelectedIds(new Set());
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '批量下载失败', 'admin'), { id: toastId });
        }
    };
    const deleteOne = async (id: number) => {
        if (!(await askConfirm({ title: `确认软删 article #${id}?`, danger: true }))) return;
        try {
            await researchMonitorApi.deleteArticle(id);
            toast.success(`已删除 #${id}`);
            if (selectedArticleId === id) setSelectedArticleId(null);
            void loadArticles();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '删除失败', 'admin'));
        }
    };

    const toggleSelected = (id: number) => {
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id); else next.add(id);
            return next;
        });
    };

    const bulkDelete = async () => {
        if (selectedIds.size === 0) return;
        if (!(await askConfirm({ title: `批量软删 ${selectedIds.size} 篇?`, danger: true }))) return;
        try {
            const res = await researchMonitorApi.bulkDeleteArticles([...selectedIds]);
            toast.success(`批量删除完成 · 删 ${res.deleted ?? 0} · 跳过 ${res.skipped ?? 0}`);
            setSelectedIds(new Set());
            void loadArticles();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '批量删除失败', 'admin'));
        }
    };


    // ---- Derived ----
    const pageStart = offset + 1;
    const pageEnd = Math.min(offset + articles.length, total);

    // ChipCount for content_type tabs (后端 counts_by_content_type · '__all__' = 总数 · '__null__' = 未分类)
    const countFor = (ct: ContentType | 'all'): number => {
        if (ct === 'all') return countsByCT['__all__'] ?? total;
        return countsByCT[ct] ?? 0;
    };

    return (
        <div className="space-y-3">
            {/* ============ 顶部 1 · 天条 (P13-v3 老板拍板按天聚合) ============ */}
            <DayStrip
                days={days}
                loading={loadingDays}
                selectedDay={selectedDay}
                onSelect={(d) => {
                    setSelectedDay(d);
                    // P13-v3: 切天时重置 industry + content_type · 漏斗式 cascade 重选
                    setSelectedIndustry(null);
                    setContentTypeFilter('all');
                    setOffset(0);
                    setSelectedArticleId(null);
                }}
                onRefresh={loadDays}
            />

            {/* ============ 顶部 2 · 行业横向条 (P13-v5 2026-05-27 老板: 文章列表太窄 · 行业从左栏移顶部) ============ */}
            <IndustryStrip
                days={days}
                selectedDay={selectedDay}
                selectedIndustry={selectedIndustry}
                onSelectIndustry={(name) => {
                    setSelectedIndustry(name);
                    setContentTypeFilter('all');
                    setOffset(0);
                    setSelectedArticleId(null);
                }}
            />

            <IntentDistributionPanel
                data={intentDistribution}
                loading={loadingIntentDistribution}
                selectedIndustry={selectedIndustry}
            />

            {/* ============ 批量 actions bar (有选中才显示) ============ */}
            {selectedIds.size > 0 && (
                <div className="flex items-center gap-2 bg-blue-50 px-3 py-2 rounded-md text-sm">
                    <span className="text-blue-900 font-medium">已选 {selectedIds.size} 篇</span>
                    <Button size="sm" variant="outline" onClick={bulkDownloadMarkdown}>
                        <Download className="h-3.5 w-3.5 mr-1.5" />批量下载 MD
                    </Button>
                    <Button size="sm" variant="destructive" onClick={bulkDelete}>
                        <Trash2 className="h-3.5 w-3.5 mr-1.5" />批量删除
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setSelectedIds(new Set())}>
                        清空
                    </Button>
                </div>
            )}

            {/* ============ 两栏可拖拽 · P13-v6 (2026-05-27 老板: 中线可拖) ============
                 LEFT  (默认 65% · 最小 30%) · 文章列表
                 SPLITTER · 可拖拽
                 RIGHT (默认 35% · 最小 20%) · 详情 panel
                 autoSaveId="articles-split" · 用户拖完 localStorage 持久化
                 lg 以下: 单列文章列表 + 详情 Dialog (P13-v13 修 review medium 窄屏不可用)
            */}
            {(() => {
                const articleListCard = (<Card>
                        <CardContent className="p-3 space-y-3">
                            {/* content_type chips with counts */}
                            <div className="flex flex-wrap gap-1.5">
                                {CONTENT_TYPES.map(t => (
                                    <ContentTypeChip
                                        key={t}
                                        type={t}
                                        count={countFor(t)}
                                        active={contentTypeFilter === t}
                                        onClick={() => { setContentTypeFilter(t); setOffset(0); }}
                                    />
                                ))}
                            </div>

                            {/* 筛选行 (P13-v2: 删了"行业精确匹配"输入 · 改成左栏 chip 选) */}
                            <div className="flex items-center gap-1.5">
                                <Input
                                    placeholder="搜索标题/域名/URL"
                                    value={keyword}
                                    onChange={e => setKeyword(e.target.value)}
                                    onKeyDown={e => { if (e.key === 'Enter') { setOffset(0); void loadArticles(); }}}
                                    className="h-8 text-xs"
                                />
                                <Select value={sort} onValueChange={(v) => { setSort(v as typeof sort); setOffset(0); }}>
                                    <SelectTrigger className="h-8 w-32 text-xs shrink-0">
                                        <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
                                        <SelectItem value="citations_desc">引用次数 ↓</SelectItem>
                                        <SelectItem value="chars_desc">字数 ↓</SelectItem>
                                        <SelectItem value="fetched_desc">抓取时间 ↓</SelectItem>
                                    </SelectContent>
                                </Select>
                            </div>

                            {/* 二级筛选 */}
                            <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                                <Input
                                    placeholder="最小字数"
                                    type="number"
                                    value={minChars}
                                    onChange={e => setMinChars(e.target.value)}
                                    onKeyDown={e => { if (e.key === 'Enter') { setOffset(0); void loadArticles(); }}}
                                    className="h-7 w-20 text-xs"
                                />
                                <div className="flex items-center gap-1">
                                    <Checkbox
                                        id="show-skipped"
                                        checked={showSkipped}
                                        onCheckedChange={(v) => { setShowSkipped(!!v); setOffset(0); }}
                                    />
                                    <Label htmlFor="show-skipped" className="cursor-pointer text-[11px]">含已跳过</Label>
                                </div>
                                <div className="flex items-center gap-1">
                                    <Checkbox
                                        id="show-imported"
                                        checked={showImported}
                                        onCheckedChange={(v) => { setShowImported(!!v); setOffset(0); }}
                                    />
                                    <Label htmlFor="show-imported" className="cursor-pointer text-[11px]">含已入库</Label>
                                </div>
                                <Button variant="ghost" size="sm" className="h-7 px-2 text-[11px] ml-auto"
                                    onClick={() => { setOffset(0); void loadArticles(); }} disabled={loading}>
                                    {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                                </Button>
                            </div>

                            {/* 统计行 + 漏斗面包屑 */}
                            <div className="text-[11px] text-muted-foreground border-t pt-2 flex items-center flex-wrap gap-1.5">
                                {selectedDay && (
                                    <Badge variant="outline" className="text-[10px] gap-1">
                                        <Layers className="h-2.5 w-2.5" />
                                        {formatDayLabel(selectedDay)}
                                        <button
                                            type="button"
                                            onClick={() => { setSelectedDay(null); setSelectedIndustry(null); setOffset(0); }}
                                            className="ml-1 hover:text-foreground"
                                        >×</button>
                                    </Badge>
                                )}
                                {selectedIndustry && (
                                    <Badge variant="outline" className="text-[10px] gap-1">
                                        <Tag className="h-2.5 w-2.5" />
                                        {selectedIndustry}
                                        <button
                                            type="button"
                                            onClick={() => { setSelectedIndustry(null); setOffset(0); }}
                                            className="ml-1 hover:text-foreground"
                                        >×</button>
                                    </Badge>
                                )}
                                <span className="ml-auto tabular-nums">
                                    共 {total.toLocaleString()} 篇
                                    {articles.length > 0 && ` · ${pageStart}-${pageEnd}`}
                                </span>
                            </div>

                            {/* 列表 */}
                            <div className="space-y-1.5 max-h-[60vh] overflow-y-auto pr-1">
                                {loading && articles.length === 0 && (
                                    <div className="py-12 text-center">
                                        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground mx-auto" />
                                    </div>
                                )}
                                {!loading && articles.length === 0 && (
                                    <div className="py-12 text-center text-sm text-muted-foreground">
                                        <Inbox className="h-8 w-8 mx-auto mb-2 text-muted-foreground/30" />
                                        无匹配文章
                                    </div>
                                )}
                                {articles.map(a => (
                                    <ArticleListItem
                                        key={a.id}
                                        article={a}
                                        selected={selectedArticleId === a.id}
                                        checked={selectedIds.has(a.id)}
                                        onSelect={() => setSelectedArticleId(a.id)}
                                        onToggleCheck={() => toggleSelected(a.id)}
                                    />
                                ))}
                            </div>

                            {/* 翻页 */}
                            {total > PAGE_SIZE && (
                                <div className="flex items-center justify-between border-t pt-2">
                                    <Button
                                        size="sm" variant="ghost"
                                        disabled={offset === 0}
                                        onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                                    >
                                        <ChevronLeft className="h-3.5 w-3.5 mr-1" />上一页
                                    </Button>
                                    <span className="text-xs text-muted-foreground tabular-nums">
                                        {Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(total / PAGE_SIZE)}
                                    </span>
                                    <Button
                                        size="sm" variant="ghost"
                                        disabled={pageEnd >= total}
                                        onClick={() => setOffset(offset + PAGE_SIZE)}
                                    >
                                        下一页<ChevronRight className="h-3.5 w-3.5 ml-1" />
                                    </Button>
                                </div>
                            )}
                        </CardContent>
                    </Card>);
                const detailPaneEl = (<DetailPane
                    articleId={selectedArticleId}
                    detail={detail}
                    loading={detailLoading}
                    editing={editing}
                    editedContent={editedContent}
                    editNote={editNote}
                    savingEdit={savingEdit}
                    onClose={() => setSelectedArticleId(null)}
                    onEditStart={() => setEditing(true)}
                    onEditCancel={() => {
                        setEditing(false);
                        setEditedContent(detail?.cleaned_content ?? '');
                        setEditNote('');
                    }}
                    onEditChange={setEditedContent}
                    onEditNoteChange={setEditNote}
                    onSaveEdit={saveEdit}
                    onUpdateIntent={updateIntentType}
                    onDownloadMarkdown={downloadMarkdown}
                    onDelete={deleteOne}
                />);
                return (<>
                    {/* lg+ · 拖拽 PanelGroup */}
                    <div className="hidden lg:block" style={{ height: 'calc(100vh - 280px)', minHeight: '500px' }}>
                        <PanelGroup orientation="horizontal" id="articles-panel-split" className="h-full">
                            <Panel defaultSize="65%" minSize="30%" className="space-y-3 overflow-auto pr-1.5">
                                {articleListCard}
                            </Panel>
                            <PanelResizeHandle className="group relative w-1.5 mx-0.5 bg-transparent hover:bg-brand/40 data-[resize-handle-state=drag]:bg-brand transition-colors">
                                <div className="absolute inset-y-0 left-1/2 -translate-x-1/2 w-px bg-border group-hover:bg-brand group-data-[resize-handle-state=drag]:bg-brand" />
                            </PanelResizeHandle>
                            <Panel defaultSize="35%" minSize="20%" className="space-y-3 overflow-auto pl-1.5">
                                {detailPaneEl}
                            </Panel>
                        </PanelGroup>
                    </div>

                    {/* P13-v13 · lg 以下窄屏 · 单列文章列表 + Dialog 详情
                        P13-v14 修 (review v6 MEDIUM-HIGH): Radix Dialog 用 Portal 到 body
                          `lg:hidden` 容器关不住 portal 出的 Dialog
                          桌面端点文章会同时显: PanelGroup 详情 + Dialog 遮罩
                        修:
                          1. lg+ 不 mount Dialog 整个 (用 isNarrow 短路 · 真 viewport 判断)
                          2. open 也带 isNarrow 双保险 · 防视口切换瞬态 */}
                    <div className="lg:hidden space-y-3">
                        {articleListCard}
                    </div>
                    {isNarrow && (
                        <Dialog
                            open={isNarrow && selectedArticleId !== null}
                            onOpenChange={(v) => { if (!v) setSelectedArticleId(null); }}
                        >
                            <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
                                <DialogHeader>
                                    <DialogTitle className="text-sm">文章详情</DialogTitle>
                                </DialogHeader>
                                {detailPaneEl}
                            </DialogContent>
                        </Dialog>
                    )}
                </>);
            })()}
          {confirmDialog}
        </div>
    );
}

// =========================================================================
// 子组件 · 顶部横向批次条
// =========================================================================
function IntentDistributionPanel(props: {
    data: ArticleIntentDistributionResponse | null;
    loading: boolean;
    selectedIndustry: string | null;
}) {
    const items = props.data?.items ?? [];
    const total = props.data?.total ?? 0;
    const topItems = [...items]
        .filter(i => i.count > 0)
        .sort((a, b) => b.count - a.count);
    const topItem = topItems[0] ?? null;

    if (props.loading && !props.data) {
        return (
            <Card>
                <CardContent className="py-3 text-xs text-muted-foreground flex items-center gap-2">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    正在计算文章意图占比
                </CardContent>
            </Card>
        );
    }

    if (!props.loading && total === 0) return null;

    return (
        <Card>
            <CardContent className="p-3 space-y-2">
                <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                        <BarChart3 className="h-4 w-4 text-muted-foreground" />
                        <span className="text-sm font-medium">
                            {props.selectedIndustry ? `${props.selectedIndustry} · ` : ''}文章意图占比
                        </span>
                        <span className="text-xs text-muted-foreground tabular-nums">
                            共 {total.toLocaleString()} 篇
                        </span>
                    </div>
                    <div className="flex items-center gap-3 text-xs text-muted-foreground tabular-nums">
                        {topItem ? (
                            <span>占比最高 {topItem.label} {topItem.percent.toFixed(1)}%</span>
                        ) : null}
                        {props.data?.unclassified_count ? (
                            <span>未分类 {props.data.unclassified_count.toLocaleString()} 篇</span>
                        ) : null}
                    </div>
                </div>
                {topItems.length === 0 ? (
                    <div className="text-xs text-muted-foreground py-2">当前筛选下暂无已分类文章</div>
                ) : (
                    <div data-intent-distribution-list className="space-y-1.5">
                        {topItems.map(item => {
                            const color = INTENT_TYPE_BADGE_COLOR[item.intent_type] ?? 'bg-muted text-foreground';
                            return (
                                <div key={item.intent_type} className="grid grid-cols-[112px_1fr_126px] items-center gap-3 text-xs">
                                    <span className="font-medium truncate">{item.label}</span>
                                    <div className="h-2.5 rounded-full bg-muted overflow-hidden">
                                        <div
                                            className={`h-full rounded-full ${color.split(' ')[0]}`}
                                            style={{ width: `${Math.max(2, item.percent)}%` }}
                                        />
                                    </div>
                                    <span className="text-right text-muted-foreground tabular-nums">
                                        {item.percent.toFixed(1)}% · {item.count.toLocaleString()} 篇
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

// =========================================================================
// 子组件 · 顶部行业横向条 (P13-v5 · 2026-05-27 老板: 行业从左栏移顶部)
// 替代 IndustryList · 跟 DayStrip 同款视觉 · 横向 chip · 节省宽度让文章列表 +60%
// =========================================================================
function IndustryStrip(props: {
    days: ArticleDaySummary[];
    selectedDay: string | null;
    selectedIndustry: string | null;
    onSelectIndustry: (name: string | null) => void;
}) {
    // 当前 day 的 industries · 若"全部天"则 union 所有 day
    const industries = useMemo(() => {
        if (props.selectedDay) {
            const d = props.days.find(x => x.day === props.selectedDay);
            return d?.industries ?? [];
        }
        const agg: Record<string, number> = {};
        for (const d of props.days) {
            for (const ind of (d.industries ?? [])) {
                agg[ind.name] = (agg[ind.name] ?? 0) + ind.articles_count;
            }
        }
        return Object.entries(agg)
            .map(([name, articles_count]) => ({ name, articles_count }))
            .sort((a, b) => b.articles_count - a.articles_count);
    }, [props.days, props.selectedDay]);

    const totalCount = industries.reduce((s, i) => s + i.articles_count, 0);

    if (industries.length === 0 && !props.selectedDay) {
        return null;  // 还没数据时不显示空条
    }

    return (
        <div className="flex items-center gap-2">
            <div className="shrink-0 flex items-center gap-1 text-xs text-muted-foreground pr-1">
                <Tag className="h-3 w-3" />
                行业
            </div>
            {/* 全部行业 */}
            <button
                type="button"
                onClick={() => props.onSelectIndustry(null)}
                className={`shrink-0 px-2.5 py-1 rounded-md text-xs transition-colors ${
                    props.selectedIndustry === null
                        ? 'bg-foreground text-background'
                        : 'border bg-muted/30 hover:bg-muted/50'
                }`}
            >
                全部
                <span className={`ml-1 tabular-nums text-[10px] ${
                    props.selectedIndustry === null ? 'text-background/70' : 'text-muted-foreground'
                }`}>
                    {totalCount.toLocaleString()}
                </span>
            </button>
            <div className="flex-1 flex items-center gap-1.5 overflow-x-auto pb-1">
                {industries.length === 0 && (
                    <span className="text-xs text-muted-foreground">该天无行业数据</span>
                )}
                {industries.map(ind => {
                    const active = props.selectedIndustry === ind.name;
                    return (
                        <button
                            key={ind.name}
                            type="button"
                            onClick={() => props.onSelectIndustry(ind.name)}
                            className={`shrink-0 px-2.5 py-1 rounded-md text-xs transition-colors ${
                                active
                                    ? 'bg-foreground text-background'
                                    : 'border bg-muted/30 hover:bg-muted/50'
                            }`}
                            title={`${ind.name} · ${ind.articles_count.toLocaleString()} 篇`}
                        >
                            {ind.name}
                            <span className={`ml-1 tabular-nums text-[10px] ${
                                active ? 'text-background/70' : 'text-muted-foreground'
                            }`}>
                                {ind.articles_count.toLocaleString()}
                            </span>
                        </button>
                    );
                })}
            </div>
        </div>
    );
}


// P13-v3 (2026-05-26 老板): 顶部按天聚合 · 替代之前的 BatchStrip
// 一天可能 union 多个 batch · 选 day 后传该天所有 batch_ids 给 /articles
function DayStrip(props: {
    days: ArticleDaySummary[];
    loading: boolean;
    selectedDay: string | null;
    onSelect: (day: string | null) => void;
    onRefresh: () => void;
}) {
    return (
        <div className="flex items-center gap-2">
            <button
                type="button"
                onClick={() => props.onSelect(null)}
                className={`shrink-0 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                    props.selectedDay === null
                        ? 'bg-foreground text-background'
                        : 'border bg-muted/30 hover:bg-muted/50'
                }`}
            >
                <Layers className="h-3 w-3 inline mr-1 -mt-0.5" />
                全部天
            </button>
            <div className="flex-1 flex items-center gap-2 overflow-x-auto pb-1">
                {props.loading && props.days.length === 0 && (
                    <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                )}
                {props.days.map(d => {
                    const selected = d.day === props.selectedDay;
                    const indNames = d.industries.slice(0, 5).map(i => i.name).join(' / ');
                    return (
                        <button
                            key={d.day}
                            type="button"
                            onClick={() => props.onSelect(d.day)}
                            className={`shrink-0 px-3 py-1.5 rounded-md text-xs transition-colors ${
                                selected
                                    ? 'bg-foreground text-background'
                                    : 'border bg-muted/30 hover:bg-muted/50'
                            }`}
                            title={
                                `${d.day} · ${d.batch_ids.length} 批次`
                                + (indNames ? ` · ${indNames}${d.industries.length > 5 ? ' / ...' : ''}` : '')
                            }
                        >
                            <div className="font-medium">{formatDayLabel(d.day)}</div>
                            <div className={`text-[10px] mt-0.5 tabular-nums ${selected ? 'text-background/70' : 'text-muted-foreground'}`}>
                                {d.articles_count.toLocaleString()} 篇 · {d.batch_ids.length} 批
                            </div>
                        </button>
                    );
                })}
            </div>
            <Button size="sm" variant="ghost" onClick={props.onRefresh} disabled={props.loading} className="shrink-0">
                {props.loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            </Button>
        </div>
    );
}

// =========================================================================
// 子组件 · content_type chip with count
// =========================================================================
function ContentTypeChip(props: {
    type: ContentType | 'all';
    count: number;
    active: boolean;
    onClick: () => void;
}) {
    const label = props.type === 'all' ? '全部' : CONTENT_TYPE_LABEL[props.type as ContentType];
    return (
        <button
            type="button"
            onClick={props.onClick}
            className={`px-2.5 py-1 rounded-full text-xs transition-colors ${
                props.active
                    ? 'bg-foreground text-background'
                    : 'bg-muted/40 hover:bg-muted/60 text-foreground/80'
            }`}
        >
            {label}
            <span className={`ml-1 tabular-nums ${props.active ? 'text-background/70' : 'text-muted-foreground'}`}>
                ({props.count.toLocaleString()})
            </span>
        </button>
    );
}

// =========================================================================
// 子组件 · 文章列表 item (设计图同款 · 极简单行)
// =========================================================================
function ArticleListItem(props: {
    article: ArticleInLibrary;
    selected: boolean;
    checked: boolean;
    onSelect: () => void;
    onToggleCheck: () => void;
}) {
    const a = props.article;
    const ctColor = a.content_type ? CONTENT_TYPE_BADGE_COLOR[a.content_type] : 'bg-gray-100 text-gray-600';
    const ctLabel = a.content_type ? CONTENT_TYPE_LABEL[a.content_type] : '未分类';
    const intentColor = a.intent_type ? INTENT_TYPE_BADGE_COLOR[a.intent_type] : null;
    const intentLabel = a.intent_type ? ARTICLE_INTENT_LABEL[a.intent_type] : null;
    const publisher = publisherOf(a.domain);

    return (
        <div
            className={`group rounded-md border px-3 py-2 cursor-pointer transition-colors ${
                props.selected ? 'border-brand bg-brand/5' : 'hover:bg-muted/30'
            }`}
            onClick={props.onSelect}
        >
            <div className="flex items-start gap-2">
                <Checkbox
                    checked={props.checked}
                    onCheckedChange={() => props.onToggleCheck()}
                    onClick={e => e.stopPropagation()}
                    className="mt-0.5 shrink-0"
                />
                <div className="flex-1 min-w-0">
                    {/* 标题 */}
                    <p className="text-sm font-medium truncate" title={a.title || '(无标题)'}>
                        {a.title || '(无标题)'}
                    </p>
                    {/* 类型 + 来源 + 域名 + 字数 */}
                    <div className="flex items-center gap-1.5 mt-1 text-[11px] text-muted-foreground flex-wrap">
                        <span className={`px-1.5 py-0.5 rounded ${ctColor}`}>{ctLabel}</span>
                        {intentLabel && intentColor && (
                            <span className={`px-1.5 py-0.5 rounded ${intentColor}`}>{intentLabel}</span>
                        )}
                        <span>{publisher}</span>
                        {publisher !== a.domain && (
                            <span className="text-muted-foreground/60">{a.domain}</span>
                        )}
                        {a.cleaned_char_count != null && (
                            <span className="tabular-nums">· {a.cleaned_char_count.toLocaleString()} 字</span>
                        )}
                    </div>
                    {/* 引擎 chips + 被 N 家引 */}
                    <div className="flex items-center gap-1 mt-1.5">
                        {(a.cited_by_platforms ?? []).map(p => {
                            const meta = ENGINE_DISPLAY[p] ?? { label: p, color: ENGINE_FALLBACK_COLOR };
                            return (
                                <span
                                    key={p}
                                    className={`px-1.5 py-0.5 rounded text-[10px] tabular-nums ${meta.color}`}
                                >
                                    {meta.label}
                                </span>
                            );
                        })}
                        {(a.cited_by_platforms?.length ?? 0) > 0 && (
                            <Badge variant="secondary" className="ml-auto text-[10px] tabular-nums shrink-0">
                                被 {a.cited_by_platforms.length} 家引
                            </Badge>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}

// =========================================================================
// 子组件 · 右侧详情 panel (替代原弹窗 · 4 tabs)
// =========================================================================
function DetailPane(props: {
    articleId: number | null;
    detail: ArticleDetailFull | null;
    loading: boolean;
    editing: boolean;
    editedContent: string;
    editNote: string;
    savingEdit: boolean;
    onClose: () => void;
    onEditStart: () => void;
    onEditCancel: () => void;
    onEditChange: (v: string) => void;
    onEditNoteChange: (v: string) => void;
    onSaveEdit: () => void;
    onUpdateIntent: (v: ArticleIntentType | null) => void;
    onDownloadMarkdown: (id: number) => void;
    onDelete: (id: number) => void;
}) {
    // 空态
    if (props.articleId == null) {
        return (
            <Card>
                <CardContent className="py-24 text-center">
                    <Inbox className="h-10 w-10 text-muted-foreground/30 mx-auto mb-3" />
                    <p className="text-sm text-muted-foreground">← 从左侧选一篇文章查看</p>
                </CardContent>
            </Card>
        );
    }

    // 加载中
    if (props.loading || !props.detail) {
        return (
            <Card>
                <CardContent className="py-24 text-center">
                    <Loader2 className="h-6 w-6 animate-spin text-muted-foreground mx-auto" />
                </CardContent>
            </Card>
        );
    }

    const d = props.detail;
    const a = d.article;
    const publisher = publisherOf(a.domain);
    const intentLabel = a.intent_type ? ARTICLE_INTENT_LABEL[a.intent_type] : null;
    const intentColor = a.intent_type ? INTENT_TYPE_BADGE_COLOR[a.intent_type] : null;

    return (
        <Card>
            <CardContent className="p-4 space-y-3">
                {/* 头部 · 标题 + 关闭 */}
                <div className="flex items-start gap-2">
                    <div className="flex-1 min-w-0">
                        <h3 className="text-base font-semibold leading-tight">{a.title || '(无标题)'}</h3>
                        <div className="flex items-center gap-1.5 mt-1 text-[11px] text-muted-foreground flex-wrap">
                            <span>{publisher}</span>
                            {publisher !== a.domain && <span>· {a.domain}</span>}
                            {a.cleaned_char_count != null && <span>· {a.cleaned_char_count.toLocaleString()} 字</span>}
                            <a
                                href={a.url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center text-brand hover:underline ml-1"
                            >
                                <ExternalLink className="h-3 w-3 mr-0.5" />原文
                            </a>
                            {intentLabel && intentColor && (
                                <span className={`px-1.5 py-0.5 rounded ${intentColor}`}>{intentLabel}</span>
                            )}
                        </div>
                    </div>
                    <Button variant="ghost" size="sm" onClick={props.onClose}>
                        <X className="h-3.5 w-3.5" />
                    </Button>
                </div>

                {/* Action 按钮组 · P14.5 (2026-06-01): 管理员只需稳定查看/编辑/下载 */}
                <div className="flex items-center flex-wrap justify-between gap-2">
                    {/* 第 1 组 · 编辑相关 */}
                    <div className="flex items-center gap-2">
                        {!props.editing ? (
                            <Button size="sm" variant="outline" onClick={props.onEditStart}>
                                <FileEdit className="h-3.5 w-3.5 mr-1.5" />编辑清洗内容
                            </Button>
                        ) : (
                            <>
                                <Button size="sm" onClick={props.onSaveEdit} disabled={props.savingEdit}>
                                    {props.savingEdit
                                        ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />
                                        : <Check className="h-3.5 w-3.5 mr-1.5" />}
                                    保存
                                </Button>
                                <Button size="sm" variant="ghost" onClick={props.onEditCancel} disabled={props.savingEdit}>
                                    取消
                                </Button>
                            </>
                        )}
                    </div>
                    {/* 第 2 组 · 文章操作 */}
                    <div className="flex items-center gap-2">
                        <Button size="sm" onClick={() => props.onDownloadMarkdown(a.id)}>
                            <Download className="h-3.5 w-3.5 mr-1.5" />
                            下载 Markdown
                        </Button>
                        <Button size="sm" variant="ghost" className="text-red-600 hover:text-red-700"
                            onClick={() => props.onDelete(a.id)}>
                            <Trash2 className="h-3.5 w-3.5 mr-1.5" />删除
                        </Button>
                    </div>
                </div>

                {/* edit note */}
                {props.editing && (
                    <Input
                        placeholder="编辑备注 (可选 · 写入审计日志)"
                        value={props.editNote}
                        onChange={e => props.onEditNoteChange(e.target.value)}
                        className="h-8 text-xs"
                    />
                )}

                {/* Tabs · cleaned / raw / citations / meta */}
                <Tabs defaultValue="cleaned" className="space-y-2">
                    <TabsList className="h-8">
                        <TabsTrigger value="cleaned" className="text-xs px-3">清洗后</TabsTrigger>
                        <TabsTrigger value="raw" className="text-xs px-3">原文</TabsTrigger>
                        <TabsTrigger value="citations" className="text-xs px-3">
                            被引 ({d.citations_count})
                        </TabsTrigger>
                        <TabsTrigger value="meta" className="text-xs px-3">元数据</TabsTrigger>
                    </TabsList>

                    <TabsContent value="cleaned">
                        {props.editing ? (
                            // 编辑态: textarea 保留 raw md · 让管理员能改 markdown 源码
                            // P13-v8 (2026-05-27 老板): 编辑态高度跟只读态一致 (旧 rows=24 ≈576px 跟 60vh 不匹配)
                            // 改 min-h + max-h 都 60vh · 跟下面 div 完全对齐
                            <Textarea
                                value={props.editedContent}
                                onChange={e => props.onEditChange(e.target.value)}
                                className="font-mono text-xs min-h-[60vh] max-h-[60vh] resize-none"
                            />
                        ) : (
                            // P13-v2 (2026-05-26 老板): 只读态用 ReactMarkdown 渲染成 HTML
                            // 不再显示 raw md 字面 (# / ## / ** **) · 显大标题 + 段落 + 列表
                            // prose 来自 @tailwindcss/typography 插件 · 跟 DiagnosisReport 同款风格
                            // P13-v8: min-h 也加 · 跟 textarea 同高 · 无论内容多少高度一致
                            <div className="min-h-[60vh] max-h-[60vh] overflow-y-auto rounded-md border bg-background p-4 text-sm">
                                {d.cleaned_content ? (
                                    <div className="prose prose-sm prose-neutral max-w-none dark:prose-invert
                                                    prose-headings:font-semibold prose-headings:scroll-mt-4
                                                    prose-h1:text-lg prose-h2:text-base prose-h3:text-sm
                                                    prose-p:leading-relaxed prose-p:my-2
                                                    prose-li:my-0.5
                                                    prose-table:border-collapse prose-th:border prose-th:border-border prose-th:p-2 prose-th:bg-muted
                                                    prose-td:border prose-td:border-border prose-td:p-2
                                                    prose-code:text-xs prose-code:bg-muted prose-code:px-1 prose-code:rounded
                                                    prose-a:text-brand prose-a:no-underline hover:prose-a:underline
                                                    overflow-x-auto">
                                        {/* P13-v9: preprocessMarkdownForChinese 修中文边界 ** / # 识别 */}
                                        <ReactMarkdown>
                                            {preprocessMarkdownForChinese(d.cleaned_content)}
                                        </ReactMarkdown>
                                    </div>
                                ) : (
                                    <span className="text-muted-foreground">(无清洗内容)</span>
                                )}
                                {d.cleaned_truncated && (
                                    <p className="mt-2 text-amber-600 text-[11px]">· 内容已截断 (超长)</p>
                                )}
                            </div>
                        )}
                    </TabsContent>

                    <TabsContent value="raw">
                        {/* 原文 tab · 保留 raw 显示 (抓取的可能不是 markdown · 可能是 HTML 或乱码 · 不强渲染) */}
                        {/* P13-v8: min-h 加 · 跟 cleaned tab 一致 */}
                        <div className="min-h-[60vh] max-h-[60vh] overflow-y-auto rounded-md border bg-muted/20 p-3 text-xs whitespace-pre-wrap font-mono">
                            {d.raw_content || <span className="text-muted-foreground">(无原文)</span>}
                        </div>
                    </TabsContent>

                    <TabsContent value="citations">
                        {/* P13-v8: min-h 加 · 跟 cleaned/raw 同高 */}
                        <div className="min-h-[60vh] max-h-[60vh] overflow-y-auto space-y-1.5">
                            {d.citations.length === 0 && (
                                <p className="text-sm text-muted-foreground py-6 text-center">无引用记录</p>
                            )}
                            {d.citations.map((c, idx) => {
                                const meta = ENGINE_DISPLAY[c.platform] ?? { label: c.platform, color: ENGINE_FALLBACK_COLOR };
                                return (
                                    <div key={idx} className="border rounded-md p-2 text-xs">
                                        <div className="flex items-center gap-2 mb-1">
                                            <span className={`px-1.5 py-0.5 rounded text-[10px] ${meta.color}`}>{meta.label}</span>
                                            <span className="text-muted-foreground tabular-nums">round={c.round_id?.slice(0, 30) ?? '?'}</span>
                                            {c.rank != null && <span className="text-muted-foreground">rank=#{c.rank}</span>}
                                            <span className="text-muted-foreground ml-auto">{c.cited_at?.slice(0, 16).replace('T', ' ')}</span>
                                        </div>
                                        {c.prompt_text && (
                                            <p className="text-muted-foreground italic">「{c.prompt_text}」</p>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    </TabsContent>

                    <TabsContent value="meta">
                        {/* P13-v8: min-h 加 · 跟其他 tab 同高 · 防 tab 切换页面跳动 */}
                        <div className="min-h-[60vh] max-h-[60vh] overflow-y-auto space-y-1.5 text-xs">
                            <MetaRow label="ID" value={String(a.id)} />
                            <MetaRow label="URL" value={a.url} link />
                            <MetaRow label="域名层级" value={a.domain_tier} />
                            <MetaRow label="行业" value={a.primary_industry ?? '—'} />
                            <MetaRow label="内容类型" value={a.content_type ?? '—'} />
                            <div className="flex items-center gap-2">
                                <span className="w-20 shrink-0 text-muted-foreground">文章意图</span>
                                <Select
                                    value={a.intent_type ?? 'unset'}
                                    onValueChange={(v) => props.onUpdateIntent(v === 'unset' ? null : v as ArticleIntentType)}
                                >
                                    <SelectTrigger className="h-8 w-44 text-xs">
                                        <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
                                        <SelectItem value="unset">未分类</SelectItem>
                                        {Object.entries(ARTICLE_INTENT_LABEL).map(([key, label]) => (
                                            <SelectItem key={key} value={key}>{label}</SelectItem>
                                        ))}
                                    </SelectContent>
                                </Select>
                            </div>
                            <MetaRow
                                label="分类置信"
                                value={a.intent_confidence == null ? '—' : `${Math.round(a.intent_confidence * 100)}%`}
                            />
                            <MetaRow label="分类理由" value={a.intent_reason ?? '—'} />
                            <MetaRow label="分类模型" value={a.intent_model ?? '—'} />
                            <MetaRow label="分类时间" value={a.intent_classified_at ?? '—'} />
                            <MetaRow label="清洗状态" value={a.clean_status} />
                            <MetaRow label="审核状态" value={a.review_status} />
                            <MetaRow label="首见批次" value={a.first_seen_round_id ?? '—'} />
                            <MetaRow label="抓取时间" value={a.fetched_at ?? '—'} />
                            <MetaRow label="字数 (清洗/原)" value={`${a.cleaned_char_count ?? '?'} / ${a.raw_char_count ?? '?'}`} />
                            <MetaRow label="被引总数" value={String(a.total_citation_count)} />
                            {a.is_duplicate && <MetaRow label="重复" value="是" />}
                        </div>
                    </TabsContent>
                </Tabs>
            </CardContent>
        </Card>
    );
}

function MetaRow({ label, value, link }: { label: string; value: string; link?: boolean }) {
    return (
        <div className="flex items-start gap-2">
            <span className="w-20 shrink-0 text-muted-foreground">{label}</span>
            {link ? (
                <a href={value} target="_blank" rel="noopener noreferrer"
                   className="text-brand hover:underline break-all font-mono">{value}</a>
            ) : (
                <span className="break-all font-mono">{value}</span>
            )}
        </div>
    );
}
