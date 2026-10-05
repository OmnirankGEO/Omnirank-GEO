/**
 * 页面教程视频 · Layer 2 (三层新手教程的第 2 层:页面内置视频)
 *
 * HeaderTutorialButton: 挂在全局顶栏的统一入口 · 每个页面位置都一样、好找、显眼。
 * - 按当前路由自动切对应视频 · 没有教程的页面自动隐藏按钮
 * - 报价方案页有两个视频(在线报价 / 快速录单)· 弹窗里用小 tab 切换
 * - 点开 → 居中弹窗播放 · 不自动弹, 只在用户点时看
 * - 视频文件放 frontend/public/tutorials/ · 没录好(404)显示"准备中"占位
 */
import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { PlayCircle, Video, ArrowRight, BookOpen } from 'lucide-react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useScreenshotMode } from '@/sandbox/screenshotMode';
import { tutorialUrl } from '@/components/help/videos-data';

export interface TutorialVideo {
    /** 视频路径 · 放 public/tutorials/ 下 */
    src: string;
    /** 弹窗标题 */
    title: string;
    /** 多视频时的 tab 文字(如"在线报价")· 单视频可不填 */
    tab?: string;
}

/** 各业务页对应的教程视频 · src 走 OSS(videos-data.ts 统一 CDN base) */
export const PAGE_TUTORIALS = {
    diagnosis:          { src: tutorialUrl('v1-diagnosis.mp4'),         title: '如何生成诊断报告 · 30 秒发起品牌体检' },
    diagnosisReport:    { src: tutorialUrl('v1b-diagnosis-report.mp4'), title: '如何看懂诊断报告' },
    onlineQuote:        { src: tutorialUrl('v2a-online-quote.mp4'),     title: '在线报价 · 让客户线上选档位' },
    // 2026-05-25 删:quickQuote (V2b 快速录单) 已废弃
    writing:            { src: tutorialUrl('v3a-writing.mp4'),          title: 'AI 写文章 · 从报价到一篇成稿' },
    // 2026-05-26 发布拆 2 个 tab(代发 / 自助发布)· 弹窗内切换
    // [WO_273] 浏览器插件自助发布退役 ⇒ 发布页教程只剩「代发」一条(原「自助发布」那条教的是已删掉的 tab)。
    publishProxy:       { src: tutorialUrl('v3b-publish.mp4'),          title: '代发 · 加购物车 → 选平台 → 投放',   tab: '代发' },
    // 2026-05-26 监测加 V4b(优化重投)· 3 个 tab
    monitoring:         { src: tutorialUrl('v4-monitoring.mp4'),        title: '加监测 · 追踪 AI 推荐',              tab: '加监测' },
    monitoringOptimize: { src: tutorialUrl('v4b-optimize.mp4'),         title: '词没达标? 选词优化、补文章重新投放',   tab: '优化重投' },
    deliver:            { src: tutorialUrl('v4c-deliver.mp4'),          title: '把效果交付给客户 · 白标链接 + 月报',   tab: '交付客户' },
} as const;

/** 路由 → 页面教程组(label 短名显示在按钮上 + 该页所有视频)
 *  顺序敏感: /diagnosis/report 必须排在 /diagnosis 前面 */
const TUTORIAL_GROUPS: { match: string; label: string; videos: TutorialVideo[] }[] = [
    { match: '/diagnosis/report', label: '看懂报告',   videos: [PAGE_TUTORIALS.diagnosisReport] },
    { match: '/diagnosis',        label: '发起诊断',   videos: [PAGE_TUTORIALS.diagnosis] },
    { match: '/pricing',          label: '报价开单',   videos: [PAGE_TUTORIALS.onlineQuote] },
    { match: '/writing',          label: 'AI 写文章',  videos: [PAGE_TUTORIALS.writing] },
    { match: '/publish',          label: '发布投放',   videos: [PAGE_TUTORIALS.publishProxy] },
    { match: '/monitoring',       label: '监测全流程', videos: [PAGE_TUTORIALS.monitoring, PAGE_TUTORIALS.monitoringOptimize, PAGE_TUTORIALS.deliver] },
];

/** 当前路由对应的教程组 · 没有则 null(按钮不显示) */
function groupForPath(pathname: string) {
    return TUTORIAL_GROUPS.find((g) => pathname.startsWith(g.match)) ?? null;
}

/** 顶栏统一"看教程"入口 · 全站固定位置 · 按路由自动切视频 */
export function HeaderTutorialButton() {
    const { pathname } = useLocation();
    const navigate = useNavigate();
    const group = groupForPath(pathname);
    const screenshotMode = useScreenshotMode();
    const [open, setOpen] = useState(false);
    const [active, setActive] = useState(0);
    const [failed, setFailed] = useState(false);

    if (!group) return null;
    // 2026-05-23 截图模式 · 顶栏"看教程"绿色按钮藏起来
    if (screenshotMode) return null;
    const videos = group.videos;
    const current = videos[Math.min(active, videos.length - 1)];

    const openModal = () => { setActive(0); setFailed(false); setOpen(true); };

    return (
        <>
            <button
                type="button"
                onClick={openModal}
                className="flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border border-emerald-300/80 bg-emerald-300 px-3 py-1.5 text-xs font-bold text-emerald-950 shadow-[0_8px_22px_rgba(16,185,129,0.25)] transition-colors hover:bg-emerald-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300/80"
            >
                <PlayCircle className="h-4 w-4 stroke-[2.4] text-emerald-950" />
                看教程 · {group.label}
            </button>
            <Dialog open={open} onOpenChange={setOpen}>
                <DialogContent className="max-w-3xl">
                    <DialogHeader>
                        <DialogTitle className="flex items-center gap-2 text-base">
                            <Video className="h-4 w-4 text-brand" />
                            {current.title}
                        </DialogTitle>
                    </DialogHeader>

                    {/* 多视频(报价页:在线报价 / 快速录单)· 占满宽度的分段切换器, 让"有多个视频"一眼可见 */}
                    {videos.length > 1 && (
                        <div className="flex flex-col gap-2">
                            <span className="text-xs text-muted-foreground">
                                本页有 {videos.length} 个教程视频, 点下面切换
                            </span>
                            <div className="inline-flex w-full gap-1 rounded-xl border bg-muted/60 p-1">
                                {videos.map((v, i) => (
                                    <button
                                        key={v.src}
                                        type="button"
                                        onClick={() => { setActive(i); setFailed(false); }}
                                        className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium transition-all ${
                                            i === active
                                                ? 'bg-background text-foreground shadow-sm ring-1 ring-border'
                                                : 'text-muted-foreground hover:text-foreground'
                                        }`}
                                    >
                                        <span className={`flex size-4 shrink-0 items-center justify-center rounded-full text-[10px] font-bold ${
                                            i === active ? 'bg-brand text-white' : 'bg-muted-foreground/20 text-muted-foreground'
                                        }`}>
                                            {i + 1}
                                        </span>
                                        {v.tab ?? `视频 ${i + 1}`}
                                    </button>
                                ))}
                            </div>
                        </div>
                    )}

                    {!failed ? (
                        <video
                            key={current.src}
                            src={current.src}
                            controls
                            autoPlay
                            playsInline
                            className="aspect-video w-full rounded-lg bg-black"
                            onError={() => setFailed(true)}
                        />
                    ) : (
                        <div className="flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-amber-400/40 bg-amber-50/40 px-6 py-16 text-center dark:bg-amber-950/20">
                            <Video className="h-8 w-8 text-amber-500" />
                            <div className="text-sm font-medium text-amber-700 dark:text-amber-300">教程视频准备中</div>
                            <div className="text-[11px] text-muted-foreground/70">
                                视频放到 <span className="font-mono">public{current.src}</span> 后刷新即可播放
                            </div>
                        </div>
                    )}

                    {/* 引导:全部教程都在帮助中心 · 一键跳转 */}
                    <div className="flex flex-col gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between">
                        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                            <BookOpen className="h-3.5 w-3.5 text-brand" />
                            所有功能的教程视频都在「帮助中心」, 随时可以回看
                        </span>
                        <button
                            type="button"
                            onClick={() => { setOpen(false); navigate('/help'); }}
                            className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90"
                        >
                            去帮助中心
                            <ArrowRight className="h-3.5 w-3.5" />
                        </button>
                    </div>
                </DialogContent>
            </Dialog>
        </>
    );
}

export default HeaderTutorialButton;
