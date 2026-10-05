/**
 * 写作中心顶层分区(工单 §1):写文章 | 制作 GEO 图文
 *
 * 🔴 刻意做成【薄壳】:WritingHall.tsx 有 8000 行,直接往里插 tab 回归面太大。
 *    本壳只负责顶层两 tab,写文章那一侧原样挂载 WritingHall,零改动零回归。
 *
 * 🔴 2026-08-03 · 客户是唯一作用域(SSOT《GEO 内容生产是一条流水线》铁律 1):
 *    进创作中心的第一件事是"这是给哪个客户做的",两个 tab 共用这一个答案。
 *      - 左上角已经选了客户 → 直接进,默认「写文章」;
 *      - 左上角没选         → 本页先选,选完 **switchClient() 回写左上角**,
 *                              于是左上角仍然是唯一的客户同步点,不是第二个。
 *    在此之前,图文 tab 自己还有一个客户选择器 —— 同一件事两个地方选,
 *    这正是 Owner 说的"割裂"。那个已删除,本页是唯一入口。
 */
import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Loader2, Users } from 'lucide-react';
import { useClientContext } from '@/context/ClientContext';
// [WO-B ① 2026-08-20 · 规格 §12.3]「WritingHall 已能接收 quote;GEO 图文页需补
// 真实 intent 解析,不能继续打开空白默认表单」。本壳是 `/writing` 的路由组件
// (WritingHall 8000 行、图文 tab 也挂在这里),所以 intent 解析接在**壳**上:
// 一处解析,两个 tab 都受益,且不动那 8000 行。
import { XiaobangPrefillRegion } from '@/components/xiaobang/XiaobangPrefillRegion';
import { useXiaobangPrefill } from '@/hooks/useXiaobangPrefill';

const WritingHall = lazy(() =>
    import('@/pages/Writing/WritingHall').then(m => ({ default: m.WritingHall })));
/*
 * 🔴 [#204 a2] 「制作 GEO 图文」这一 tab 已撤,`ImageNoteStudio` 整页已删。
 *    图文现在只住一条路由:`/writing/image-note[/:postId]` —— Owner「唯一一屏」。
 *    左栏就是列表(选题 + 三态),所以不再有"列表页 / 制作台"这层。
 *    老链 `?tab=douyin` 在下面做一次 replace 重定向:存量收藏夹不落空。
 */

const Fallback = () => (
    <div className="flex items-center justify-center py-16 text-muted-foreground">
        <Loader2 className="mr-2 h-5 w-5 animate-spin" />加载中
    </div>
);

/**
 * 没选客户时的第一屏。
 *
 * 🔴 它**不是**第二个客户选择器 —— 选中后立刻 `switchClient()` 写回全局,
 *    左上角随即显示同一个客户。这里只是"左上角还空着时"的补选入口。
 */
function ClientPicker() {
    const { clients, listReady, listLoading, listError, switchClient } = useClientContext();
    const [query, setQuery] = useState('');

    const filtered = useMemo(() => {
        const kw = query.trim().toLowerCase();
        if (!kw) return clients;
        return clients.filter(c =>
            (c.name || '').toLowerCase().includes(kw)
            || (c.industry || '').toLowerCase().includes(kw)
            || (c.company_name || '').toLowerCase().includes(kw));
    }, [clients, query]);

    return (
        <Card data-testid="writing-client-picker">
            <CardContent className="space-y-3 p-4">
                <div className="flex items-center gap-2">
                    <Users className="h-4 w-4 text-primary" />
                    <h2 className="text-sm font-semibold">先选一个客户</h2>
                    <span className="text-[11px] text-muted-foreground">
                        写文章和做图文用的是同一份客户资料，选完两边都通
                    </span>
                </div>
                <Input
                    value={query}
                    onChange={e => setQuery(e.target.value)}
                    placeholder="搜客户名 / 行业"
                    className="h-9 text-sm"
                    data-testid="writing-client-search"
                />
                {listLoading && !listReady && (
                    <div className="flex items-center gap-2 py-6 text-xs text-muted-foreground">
                        <Loader2 className="h-4 w-4 animate-spin" />正在读客户列表
                    </div>
                )}
                {listError && (
                    <p className="text-xs text-destructive">{listError}</p>
                )}
                {listReady && filtered.length === 0 && (
                    <p className="py-6 text-xs text-muted-foreground">
                        {clients.length === 0
                            ? '还没有客户，先去「我的客户」建一个'
                            : '没有匹配的客户，换个词试试'}
                    </p>
                )}
                <div className="flex flex-wrap gap-1.5">
                    {filtered.map(c => (
                        <Button
                            key={c.id}
                            variant="outline"
                            size="sm"
                            className="h-7 px-2.5 text-xs"
                            onClick={() => switchClient(c.id)}
                            data-testid={`writing-pick-client-${c.id}`}
                        >
                            {c.name}
                        </Button>
                    ))}
                </div>
            </CardContent>
        </Card>
    );
}

export function WritingWorkspace() {
    /*
     * 🔴 [#203] tab 认 URL 参数。改前它只是本地 state,于是图文详情页那颗
     *    「返回列表」无论怎么跳都会落在**写文章**那一侧 ——
     *    按钮写着返回列表却回到另一条产线,和坏链接没区别。
     *    取值与下面 TabsTrigger 的 value 同源('article' / 'douyin'),
     *    认不出的值一律回默认,不让 URL 决定出现一个不存在的 tab。
     */
    const [searchParams] = useSearchParams();
    const tabFromUrl = searchParams.get('tab');
    const [tab, setTab] = useState('article');
    const navigate = useNavigate();
    /*
     * 🔴 [#204 a2 · 条件①] 老链 `?tab=douyin` **重定向**到图文那条路由。
     *    那个 tab 本单撤了,但这条链已经发出去过(#186 那批、以及用户自己的收藏夹)。
     *    不重定向的话它会静默落在「写文章」上 —— 页面正常、内容不是他要的那一条,
     *    这比 404 更难发现。`replace` 是为了后退不弹回一个已经不存在的 tab。
     */
    useEffect(() => {
        if (tabFromUrl === 'douyin') navigate('/writing/image-note', { replace: true });
    }, [tabFromUrl, navigate]);
    // 🔴 键必须是后端 `_FORM_PREFILL_FIELDS` 真会写的那几个。
    //    `article_id` 也落:文章 tab 里的既有链路按 URL 参数选稿。
    const { prefill: xiaobangPrefill, error: xiaobangPrefillError } = useXiaobangPrefill({
        numbers: {
            brand_id: 'brand_id',
            quote_id: 'quote_id',
            article_id: 'article_id',
            geo_post_id: 'geo_post_id',
        },
    });
    // 临时制作(规格 01 §3.1 「手工创作入口单列」)。默认 false = 走合同批量工作台。
    const { currentBrandId, isAllClientsMode, relatedQuoteIds, switchClient } = useClientContext();
    /**
     * 🔴 [#150 2026-09-08] 深链带来的 `quote_id` **不再用来选报价**(选择卡已随合同产线撤下),
     *    改为**落成词表高亮**:新页把属于这张报价的词标出来,用户一眼看到"小榜让我做的是这几个"。
     *    `brand_id` 的落法一字不动(切客户的时序竞态那段推理仍然成立)。
     */
    /*
     * 🔴 [#204 a2] `highlightQuoteId` 已删:它的**唯一消费者**是制作台的词表高亮,
     *    那块屏本单退役。留着就是一个算出来没人用的值 ——
     *    而判据 G4 恰好钉着"它被传下去了",那条判据也随之失去指称对象(按「位置」重判)。
     */
    // 🔴 对象是 GEO 图文时,深链要落在**图文 tab**,不是默认的写文章 tab。
    //    落错 tab 的观感与"打开空白默认表单"没有区别 —— 用户看到的仍然不是他要的那一页。
    /*
     * 🔴 对象是 GEO 图文时,深链要落在**图文那一屏**。
     *    改前是 `setTab('douyin')`;tab 撤了之后那句话会变成一次**静默空转** ——
     *    不报错、不跳转,用户停在写文章页,看到的仍然不是他要的东西。
     *    能拿到 `geo_post_id` 就直接开到那一条,拿不到就落在图文首屏。
     */
    useEffect(() => {
        const kinds = (xiaobangPrefill?.object?.items || []).map(i => i.resource_kind);
        if (!kinds.includes('geo_image_post')) return;
        const pid = Number(xiaobangPrefill?.form_prefill?.geo_post_id);
        navigate(Number.isFinite(pid) && pid > 0
            ? `/writing/image-note/${pid}` : '/writing/image-note', { replace: true });
    }, [xiaobangPrefill, navigate]);
    const quoteChoices = useMemo(
        () => (relatedQuoteIds || []).map(id => Number(id)).filter(n => Number.isFinite(n)),
        [relatedQuoteIds]);

    // 🔴 admin 默认走「全部客户」模式(ClientContext 的 pickDefault),
    //    它的使用场景本来就是跨客户巡检 —— 不逼它每次先选一个客户。
    //    非 admin 没选客户时才拦。
    const needPickClient = currentBrandId === null && !isAllClientsMode;

    return (
        <div className="p-4 md:p-6">
            {/* 页面标题:AI 创作中心(路由仍是 /writing,不改 URL 保持兼容) */}
            <XiaobangPrefillRegion
                prefill={xiaobangPrefill}
                error={xiaobangPrefillError}
                onBackToAssistant={() => navigate('/dashboard')}
            />
            <div className="mb-4">
                <h1 className="text-lg font-bold text-foreground sm:text-2xl">AI 创作中心</h1>
                <p className="text-xs text-muted-foreground sm:text-sm">
                    写文章投媒体，或做图文帖投抖音 —— 都是为了让 AI 搜索在回答里引用到你
                </p>
            </div>

            {needPickClient ? <ClientPicker /> : (
                <Tabs value={tab} onValueChange={setTab} className="w-full">
                    <TabsList>
                        {/* testid 是给判据的稳定抓手:两个 trigger 的可访问名
                            由文本节点 + Badge 拼出来,快照里算不出稳定的 name。 */}
                        <TabsTrigger value="article" data-testid="writing-tab-article">写文章</TabsTrigger>
                    </TabsList>
                    {/* 🔴 [#204 a2 · 条件②] 图文的 tab 撤了,**入口不能跟着撤**:
                        否则从 `/writing` 一步到不了图文,只剩深链和空态 CTA 能进去。
                        这一条是"一步可达"的那处入口,判据钉住它。 */}
                    <Link to="/writing/image-note"
                        data-testid="writing-goto-image-note"
                        className="ml-auto inline-flex items-center gap-1.5 text-sm text-muted-foreground underline underline-offset-2 hover:text-foreground">
                        制作 GEO 图文
                        <Badge variant="secondary" className="px-1.5 py-0 text-[10px]">试点</Badge>
                    </Link>

                    {/* 原写作链原样挂载,不改一行 */}
                    <TabsContent value="article" className="mt-0">
                        <Suspense fallback={<Fallback />}>
                            <WritingHall />
                        </Suspense>
                    </TabsContent>


                </Tabs>
            )}
        </div>
    );
}

export default WritingWorkspace;
