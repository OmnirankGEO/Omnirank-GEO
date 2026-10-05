import { lazy, Suspense, useCallback, useEffect, useState, useRef } from "react";
import { DefensiveReportView, type ReportPresentation } from '@/components/defensiveGeo/DefensiveReportView';
import { DEFGEO_COPY } from '@/lib/defensiveGeoCopy';
import { trackEvent } from '@/lib/analytics';
import { reportHeadline } from './report/reportHeadline';
import {
    loadWithOneRetry, describeReportLoadFailure, type ReportLoadFailure,
} from './report/reportLoadFailure';
import { useParams, useNavigate, Link } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button, buttonVariants } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { ArrowLeft, FileText, Download, ExternalLink, Loader2, Building2, RefreshCw, BarChart3, Share2, Calculator, MoreHorizontal, Sparkles } from "lucide-react";
import { Dialog, DialogContent, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { ExportReportModal } from "./ExportReportModal";
import { diagnosisApi, type DiagnosisRecord, authFetch, formatApiErrorForDisplay } from "@/lib/api";
import { useClientContext } from "@/context/ClientContext";  // [CTO-15.23 2026-05-11 P0-1] 报告页用错 brand_id 修
import ReactMarkdown from "@/components/SafeMarkdown"; // [2026-07-22 板块B] 全站 Markdown SSOT（verify-markdown-ssot 门禁）
import { Badge } from "@/components/ui/badge";
import { toast } from 'sonner';
import ClientMaterialsEditor from "./ClientMaterialsEditor";
import { ReportV2View, isReportV2 } from "./ReportV2View";  // A.5 (CTO-15.9 session 3)
// C3.2 (CTO-15.9 session 3 · 2026-04-25 · M2 §P1.4) 诊断 19 次趋势图
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import { SharePosterDialog } from "@/components/share/SharePosterDialog";
import { useIsCEndContext } from '@/hooks/useIsCEndContext';
// 2026-05-25 删:FeatureCostBadge 原本只在"生成 GEO 报价方案"按钮下用 · 按钮已下架
// CTO-15.20 v2 · C.2 桥接:诊断报告页顶部加 BridgeBanner(主按钮 + 回 M3)
import { BridgeBanner } from '@/components/workbench/BridgeBanner';
import { isSandboxActive } from '@/sandbox/sandboxState';
import { setTutorialStage } from '@/sandbox/tutorialStage';
import { useMarkStepCompleted } from '@/hooks/useMarkStepCompleted';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { copyAsyncText } from '@/lib/copyUtils';
import { openAsyncUrl } from '@/lib/openAsyncUrl';
import { ManualCopyDialog } from '@/components/common/ManualCopyDialog';
import { ManualOpenDialog } from '@/components/common/ManualOpenDialog';

const DiagnosisRecommendationBehavior = lazy(() => import('@/features/geoObservation').then(m => ({
    default: m.DiagnosisRecommendationBehavior,
})));

export function DiagnosisReport() {
    const { id } = useParams<{ id: string }>();
    const navigate = useNavigate();
    const isCEnd = useIsCEndContext();  // v3.2: C 端隐藏"导出报告"按钮
    // [CTO-15.23 2026-05-11 P0-1] 报告页 mount 时强制把全局 currentBrandId 切到 diagnosis.brand_id
    //   原 bug:进 QZQZ(brand_id=428) 诊断报告 · 旁边 5+ 个 API 用 useClientContext 全局 currentBrandId(老板 default=10)
    //   → /api/quotes?brand_id=10 / /api/client-context/10 / /api/portal/tokens/109 / /api/monitoring/clients/109/keywords
    //   = 用户看 QZQZ 报告页 · 旁边显示别人(brand 10)的报价/监测/完整度 = "报价逻辑混乱"
    const { switchClient } = useClientContext();
    const [loading, setLoading] = useState(true);
    // [门四 UX 返修 2026-08-22] 失败态从裸字符串换成结构化的三件套
    //   (人话标题 / 怎么办 / 是不是瞬时)。理由见 report/reportLoadFailure.ts 顶部。
    const [error, setError] = useState<ReportLoadFailure | null>(null);
    const [exportModalOpen, setExportModalOpen] = useState(false);
    const [showSharePoster, setShowSharePoster] = useState(false);
    // [WO_WHITELABEL_COPY_UX 项2] 剪贴板被拒但链接已拿到 → 弹可选中链接框
    const [manualCopyText, setManualCopyText] = useState<string | null>(null);
    // [WO_IOS_TOUCH_UX 2026-08-05] 报告窗口被拦但链接已拿到 → 给可点链接
    const [manualOpenUrl, setManualOpenUrl] = useState<string | null>(null);
    const [regenerating, setRegenerating] = useState(false);
    // [P1-9] 「打开报告」按钮的在途态（拿 share-link 期间禁重复点，防开多个标签页）
    const [openingReport, setOpeningReport] = useState(false);
    // 2026-05-25 删:generatingQuote · 报告页"生成 GEO 报价方案"一键入口已下架
    const [showMaterialsEditor, setShowMaterialsEditor] = useState(false);
    const reportRef = useRef<HTMLDivElement>(null);
    const [diagnosisScope, setDiagnosisScope] = useState<string>("");  // geo | legacy; UI 只保留 GEO 主线入口
    const [isLegacy, setIsLegacy] = useState(false);
    const [report, setReport] = useState<{ detail: DiagnosisRecord | null; content: string }>({
        detail: null,
        content: "",
    });
    // 板块 A 收尾(2026-07-22)：逐格确认后页头徽章已就地更新，但 v2 正文内嵌旧分数文本不更新
    // → 记录本次会话最近一次重算时间，页头出"已于 HH:mm 重算"角标 + "重新加载报告"按钮，
    //   点击重拉诊断详情刷新全文；角标持续可见直到刷新完成。
    // [CUR-01 2026-08-21] 报告绑定:这份诊断是不是 v2 defensive/hybrid 运行。
    // 🔴 判据来自**服务端**(run_token → v2 preview → campaign_mode),
    //    前端不靠 content 里的 marker 猜 —— 旧 v2 Markdown 与本规格的
    //    Presentation v2 是同名异物,靠 marker 判会把所有旧报告送进新组件。
    const [defgeoPresentation, setDefgeoPresentation] = useState<ReportPresentation | null>(null);
    const [scoreRecalculatedAt, setScoreRecalculatedAt] = useState<Date | null>(null);

    // 🔴 [#63 2026-09-05] 五卡投影为空 ⇒ 回落完整报告,并**上报一次**(供数据侧统计发生面)。
    //    用 ref 去重:这个 effect 会随 report 的每次重渲染再跑,不去重就把**一次**回落
    //    报成 N 次 —— 那样统计出来的是渲染次数,不是「有多少份报告踩到」。
    //    两个数在图表上长得一样,而只有后者能回答「这个缺陷影响面多大」。
    const fiveCardFallbackReported = useRef(false);
    useEffect(() => {
        if (!defgeoPresentation?.isV2) return;
        if (defgeoPresentation.cards.length > 0) return;
        if (fiveCardFallbackReported.current) return;
        fiveCardFallbackReported.current = true;
        trackEvent('defgeo_five_card_fallback', {
            diagnosisId: defgeoPresentation.diagnosisId,
            questionPlanId: defgeoPresentation.questionPlanId ?? null,
            questionPlanRevision: defgeoPresentation.questionPlanRevision ?? null,
            projectionVersion: defgeoPresentation.projectionVersion ?? null,
        });
    }, [defgeoPresentation]);

    useEffect(() => {
        if (!id) return;
        let alive = true;
        (async () => {
            try {
                const token = localStorage.getItem('omnirank_token');
                const res = await fetch(`/api/defensive-geo/reports/${id}/presentation`, {
                    headers: token ? { Authorization: `Bearer ${token}` } : {},
                });
                if (!res.ok) return;            // 拿不到就走 legacy,不打断阅读
                const data = (await res.json()) as ReportPresentation;
                if (alive) setDefgeoPresentation(data);
            } catch {
                /* 绑定查询失败不该连带把报告页拖垮 —— 静默回落 legacy */
            }
        })();
        return () => { alive = false; };
    }, [id]);
    const [reloadingReport, setReloadingReport] = useState(false);
    // C3.2 (CTO-15.9 session 3 · 2026-04-25 · M2 §P1.4) 19 次诊断 score 趋势
    const [scoreHistory, setScoreHistory] = useState<{ date: string; total_score: number; level: string }[]>([]);
    const markStep = useMarkStepCompleted();

    // 沙盒: 落地诊断报告页 = 第一步真正完成 · 标记完成(触发"第一步完成"庆祝)+ 过场话术衔接第二步
    const [showStep2Bridge, setShowStep2Bridge] = useState(false);
    const [step1Triggered, setStep1Triggered] = useState(false);
    const sandboxStep1DoneRef = useRef(false);
    // (1) 落地报告页 · 标记第一步完成 + 触发后续
    useEffect(() => {
        if (!isSandboxActive() || sandboxStep1DoneRef.current) return;
        sandboxStep1DoneRef.current = true;
        markStep('first_diagnosis');   // → SandboxBanner 弹"第一步完成"小庆祝(撒花)
        setStep1Triggered(true);
    }, [markStep]);
    // (2) 庆祝先播 ~2.6s, 再弹过场话术窗(依赖 state · StrictMode 双跑也能正确重设定时器)
    useEffect(() => {
        if (!step1Triggered) return;
        const t = window.setTimeout(() => setShowStep2Bridge(true), 2600);
        return () => window.clearTimeout(t);
    }, [step1Triggered]);

    const loadReport = useCallback(async () => {
        if (!id) return;
        try {
            // [门四] 首屏两条主请求对瞬时 5xx/网络错**自动重试一次**(短退避)。
            // 两条都是只读 GET,重试无副作用。终态 4xx 不重试(见 isTransientLoadFailure)。
            const [detailRes, contentRes] = await loadWithOneRetry(() => Promise.all([
                diagnosisApi.getDetail(parseInt(id)),
                diagnosisApi.getContent(parseInt(id)),
            ]));
            setReport({
                detail: detailRes.data,
                content: contentRes.data.content || "",
            });

            // 获取诊断scope
            try {
                const typeRes = await authFetch(`/api/diagnosis/${id}/type`);
                const typeData = await typeRes.json();
                if (typeData.scope) {
                    setDiagnosisScope(typeData.scope);
                    setIsLegacy(typeData.is_legacy || false);
                }
            } catch (e) {
                console.warn("获取诊断类型失败:", e);
            }

            // C3.2 · 拉 19 次 score 趋势
            const bid = (detailRes.data as any)?.brand_id;
            if (bid) {
                // [CTO-15.23 2026-05-11 P0-1] 切全局 currentBrandId 到诊断对应的 brand_id
                //   让本页 + 后续跳转(报价 / 监测 / 客户工作台)继承正确 brand 上下文
                //   不 reset:用户离开报告页后保留新 brand · 大概率接下来的操作都跟这个客户相关
                switchClient(bid);
                try {
                    const histRes = await authFetch(`/api/brands/${bid}/diagnosis-history?limit=19`);
                    const histData = await histRes.json();
                    if (histData?.success && Array.isArray(histData.items)) {
                        setScoreHistory(histData.items);
                    }
                } catch (e) {
                    console.warn("获取诊断趋势失败:", e);
                }
            }
        } catch (err: any) {
            console.error("Failed to fetch report:", err);
            setError(describeReportLoadFailure(err, formatApiErrorForDisplay(err, "加载报告失败")));
        } finally {
            setLoading(false);
        }
    }, [id, switchClient]);

    useEffect(() => {
        void loadReport();
    }, [loadReport]);

    /**
     * [门四] 失败态里的「重试」——**就地**重拉,不动路由。
     * 先清失败态并回到 loading,骨架屏那段文案本身就在说"正在加载内容",
     * 她点完立刻看得见系统在动(而不是按钮点下去没反应)。
     */
    const retryLoadReport = useCallback(() => {
        setError(null);
        setLoading(true);
        void loadReport();
    }, [loadReport]);

    const getLevelColor = (level: string) => {
        if (!level) return 'bg-muted-foreground text-white';
        if (level.includes('领先')) return 'bg-emerald-500 text-white';
        if (level.includes('成熟')) return 'bg-green-500 text-white';
        if (level.includes('成长')) return 'bg-brand text-white';
        if (level.includes('起步')) return 'bg-amber-500 text-white';
        if (level.includes('待提升')) return 'bg-orange-500 text-white';
        if (level.includes('空白')) return 'bg-muted-foreground text-white';
        return 'bg-muted-foreground text-white';
    };

    // 2026-05-25 删:handleGenerateQuote · 报告页"生成 GEO 报价方案"一键入口已下架
    // 用户改走 sidebar → /pricing 单一在线报价入口
    // (后端 /api/diagnosis/{id}/generate-quote 端点暂留 · UI 不再调 · 等后端那边一起清)

    // WJ-11 复制报告分享链接(顶部 CTA + 下拉菜单复用)
    // [WO_WHITELABEL_COPY_UX 项2 2026-08-05] iOS Safari / 微信 webview:剪贴板写入必须在
    // 用户手势的同步栈里发起 —— handler 不能是 async、复制不能排在 await 接口之后。
    // 走 copyAsyncText(同步构造 ClipboardItem,文本 Promise 晚到没关系);
    // 复制被拒但链接已拿到 → 弹 ManualCopyDialog 给可长按选中的链接,不许只报「复制失败」。
    const handleCopyShareLink = () => {
        if (!id) return;
        void copyAsyncText(async () => {
            const res = await authFetch(`/api/diagnosis/${id}/share-link`, { method: 'POST' });
            const data = await res.json();
            if (!data.share_url) throw new Error('no share_url');
            return data.share_url as string;
        }).then(({ ok, text }) => {
            if (ok) {
                toast.success('链接已复制，发给客户即可查看');
            } else if (text) {
                setManualCopyText(text);
            } else {
                toast.error('生成分享链接失败');
            }
        });
    };

    // [P1-9] 打开报告：复用 share-link 接口，拿到的就是客户点开看到的那份公开报告。
    //   不自己拼 URL（token 由后端 baked 进 target_url，前端拼会产生坏链）。
    // [WO_IOS_TOUCH_UX 2026-08-05] 🔴 handler 不能是 async。
    //   本页「复制报告链接」与「打开报告」是并排两个主按钮,**同一个病**:
    //   都先 await share-link 再动作,await 之后手势凭证就没了。
    //   复制侧已由 WO_WHITELABEL_COPY_UX 项2 改走 copyAsyncText;这里是剩下的另一半 ——
    //   服务商在手机上此前既复制不了链接、也打不开报告。
    //   走 openAsyncUrl:手势同步栈里先占住窗口,URL 到手再导航过去。
    const handleOpenReport = () => {
        if (!id) return;
        setOpeningReport(true);
        void openAsyncUrl(async () => {
            const res = await authFetch(`/api/diagnosis/${id}/share-link`, { method: 'POST' });
            const data = await res.json();
            if (!data.share_url) throw new Error('no share_url');
            return data.share_url as string;
        })
            .then(({ ok, url, blocked }) => {
                if (ok) return;
                // 链接已拿到、只是窗口被拦 → 给可点的链接,不许只丢一句"打开失败"
                if (blocked && url) setManualOpenUrl(url);
                else toast.error('打开报告失败，请稍后重试');
            })
            .finally(() => setOpeningReport(false));
    };

    // 重新生成 GEO 报告
    const handleRegenerateReport = async () => {
        if (!id) return;

        setRegenerating(true);
        try {
            const response = await authFetch(`/api/diagnosis/${id}/regenerate-report`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ diagnosis_scope: 'geo' })
            });

            const data = await response.json();

            if (data.success && data.report) {
                setReport(prev => ({ ...prev, content: data.report }));
                setDiagnosisScope('geo');
                toast.success('GEO诊断报告已生成');
            } else {
                toast.error(data.error || '生成失败，请重试');
            }
        } catch (err) {
            console.error('重新生成报告失败:', err);
            toast.error('生成失败，请重试');
        } finally {
            setRegenerating(false);
        }
    };


    if (loading) {
        // 99% 完成后跳到这里。后端已经跑完分析，此时在拉报告 Markdown 内容
        // (几十~几百 KB)。下载和解析需要 1~2 分钟,不是卡死。
        // 用骨架屏 + 明确文案让用户安心。
        return (
            <div className="p-4 md:p-6 space-y-6 pb-24 lg:pb-0 animate-pulse">
                {/* 顶部状态条:文案 + spinner,告诉用户系统在干嘛、要等多久 */}
                <div className="flex items-center justify-center gap-3 rounded-xl border border-brand/20 bg-brand/5 px-4 py-3">
                    <Loader2 className="h-5 w-5 animate-spin text-brand shrink-0" />
                    <div className="text-sm">
                        <div className="font-medium text-foreground">报告生成完成,正在加载内容</div>
                        <div className="text-xs text-muted-foreground mt-0.5">报告内容较大,通常需要 1-2 分钟,请稍候不要刷新页面</div>
                    </div>
                </div>

                {/* 标题骨架 */}
                <div className="flex items-center gap-4">
                    <div className="h-10 w-10 rounded-xl bg-muted shrink-0" />
                    <div className="flex-1 space-y-2">
                        <div className="h-5 w-2/3 rounded bg-muted" />
                        <div className="h-3 w-1/3 rounded bg-muted" />
                    </div>
                </div>

                {/* 评分卡片骨架 */}
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    {Array.from({ length: 4 }).map((_, i) => (
                        <div key={i} className="rounded-xl border border-border p-4 space-y-2">
                            <div className="h-3 w-1/2 rounded bg-muted" />
                            <div className="h-7 w-3/4 rounded bg-muted" />
                        </div>
                    ))}
                </div>

                {/* 报告正文骨架 */}
                <div className="rounded-xl border border-border p-6 space-y-3">
                    <div className="h-4 w-1/4 rounded bg-muted" />
                    <div className="space-y-2 pt-2">
                        {Array.from({ length: 8 }).map((_, i) => (
                            <div key={i} className="h-3 rounded bg-muted" style={{ width: `${60 + (i * 7) % 35}%` }} />
                        ))}
                    </div>
                    <div className="h-32 rounded bg-muted mt-4" />
                    <div className="space-y-2 pt-2">
                        {Array.from({ length: 5 }).map((_, i) => (
                            <div key={i} className="h-3 rounded bg-muted" style={{ width: `${55 + (i * 11) % 40}%` }} />
                        ))}
                    </div>
                </div>
            </div>
        );
    }

    if (error) {
        // [门四 UX 返修] 三处变化,每一处都对着实录里那个场景:
        //   ① 主行动是「重试」,**留在本页原地重拉**(不丢路由 —— 她是从客户工作台
        //      点进来的,`navigate("/")` 等于把她赶回起点重走一遍);
        //   ② 「返回首页」降为次要动作;
        //   ③ 底色从红换成琥珀:红色是"坏了"的信号,而这多半只是忙了一下。
        return (
            <div className="p-4 md:p-6 space-y-4">
                <div
                    role="alert"
                    data-testid="report-load-failure"
                    className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 space-y-1"
                >
                    <p className="text-[15px] font-medium text-amber-900">{error.headline}</p>
                    <p className="text-[13px] text-amber-800">{error.body}</p>
                </div>
                <div className="flex flex-wrap gap-2">
                    <Button data-testid="report-retry" onClick={retryLoadReport}>重试</Button>
                    <Button variant="outline" onClick={() => navigate("/")}>返回首页</Button>
                </div>
            </div>
        );
    }

    return (
        <div className="p-4 md:p-6 space-y-6 pb-24 lg:pb-0">
            {/* CTO-15.20 v2 · C.2 桥接 banner · 顶部主按钮 + 回 M3 客户工作台 */}
            <BridgeBanner brandId={(report.detail as { brand_id?: number } | null)?.brand_id ?? null} />
            {/* 🔴 [包三 · 报告页] 顶部原来只有分数和等级徽标 —— 那是**读数**不是结论。
                她要的是"这意味着什么、接下来做哪一步";没有这句,她得自己把 62 分 +「成长」
                翻译成一个动作,而这正是「3 秒不知该点哪」的来源。
                映射在 report/reportHeadline.ts(纯函数、总覆盖、未知档给笼统但为真的一句)。 */}
            {(() => {
                const h = reportHeadline(report.detail?.level, report.detail?.total_score);
                const toneClass = h.tone === 'urgent' ? 'border-amber-500/40 bg-amber-500/10'
                    : h.tone === 'attention' ? 'border-brand/40 bg-brand/5'
                    : h.tone === 'steady' ? 'border-emerald-500/30 bg-emerald-500/5'
                    : 'border-border bg-muted/30';
                return (
                    <div data-testid="report-headline"
                        className={`rounded-xl border p-3 text-[13px] leading-relaxed ${toneClass}`}>
                        <p className="font-medium text-foreground">{h.conclusion}</p>
                        <p className="text-muted-foreground mt-1">下一步:{h.nextStep}</p>
                    </div>
                );
            })()}
            <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                <div className="flex items-center gap-4 min-w-0">
                    <Button variant="ghost" size="icon" className="shrink-0" onClick={() => navigate(-1)}>
                        <ArrowLeft className="h-4 w-4" />
                    </Button>
                    <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center shrink-0">
                        <BarChart3 className="h-5 w-5 text-brand" />
                    </div>
                    <div className="min-w-0 flex-1">
                        {/* [CTO-15.23 2026-05-06 P1 修复] 加 title tooltip · hover 看完整品牌名 · 防 8 字截断 "e2e-tes..." */}
                        <h2 className="text-xl sm:text-2xl font-bold text-foreground break-words leading-tight" title={report.detail?.brand_name || "诊断报告"}>
                            {report.detail?.brand_name || "诊断报告"}
                        </h2>
                        <div className="flex items-center gap-3 mt-1 flex-wrap">
                            <span className="text-muted-foreground">{report.detail?.industry}</span>
                            {report.detail?.level && (
                                <Badge className={getLevelColor(report.detail.level)}>
                                    {report.detail.level}
                                </Badge>
                            )}
                            <span className="text-xl sm:text-2xl font-bold text-foreground inline-flex items-center gap-1">
                                {report.detail?.total_score ?? '—'}/100分
                                <HelpHint title="这个分数和等级怎么来的?">
                                    分数 = 我们在主流 AI 平台里, 用这个品牌的行业关键词搜一圈,
                                    综合<b>被提到的次数、来源证据、内容质量</b>等算出的 0-100 分, 反映品牌当前在 AI 里的<b>可见度</b>。
                                    <br />等级(危急 / 成长 / 成熟 / 领先)是分数对应的档位。<b>分越低 = 越值得做 GEO</b> —— 正好拿这份报告去跟客户谈单。
                                </HelpHint>
                            </span>
                            {scoreRecalculatedAt && (
                                <span className="inline-flex items-center gap-1.5 text-xs">
                                    <Badge variant="outline" className="border-brand/40 text-brand font-normal">
                                        已于 {scoreRecalculatedAt.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })} 重算
                                    </Badge>
                                    <Button
                                        variant="ghost"
                                        size="sm"
                                        className="h-6 px-2 text-xs"
                                        disabled={reloadingReport}
                                        onClick={async () => {
                                            setReloadingReport(true);
                                            try {
                                                await loadReport();
                                                setScoreRecalculatedAt(null);
                                            } finally {
                                                setReloadingReport(false);
                                            }
                                        }}
                                    >
                                        <RefreshCw className={`mr-1 h-3 w-3 ${reloadingReport ? 'animate-spin' : ''}`} />
                                        重新加载报告
                                    </Button>
                                </span>
                            )}
                        </div>
                    </div>
                </div>

                {/* CTO-15.23 Phase 6 (2026-05-22) · action row 收口
                 * 老板 5/22 报"密密麻麻" + audit 标 8 button 挤一行 mobile 多行
                 * 改:主 action 1 个 visible(生成 GEO 报价方案) + 其余收 DropdownMenu
                 * 例外:GEO 诊断标识 Badge 保留 inline · 是状态指示不是 action */}
                <div className="flex items-center gap-1.5 sm:gap-2 flex-wrap [&>*]:text-xs sm:[&>*]:text-sm [&_a>button]:text-xs sm:[&_a>button]:text-sm">
                    {/* 显示当前 GEO 主线标识 · 状态 badge · 保留 inline */}
                    {diagnosisScope && (
                        <Badge className="bg-blue-500 text-white">
                            GEO诊断
                        </Badge>
                    )}

                    {/* 2026-05-25 删:"生成 GEO 报价方案"主按钮已下架 · 走 sidebar → /pricing 单一在线报价 */}
                    {/* 次要 actions 收 DropdownMenu · 减首屏视觉过载
                     * Phase 6.1(Codex 十二审修):不用 asChild + Button · 项目 Button 非 forwardRef
                     *   Radix 拿不到 ref · popper 定位失败 · 菜单 transform translate(0,-200%) 不可见
                     *   修:DropdownMenuTrigger 自己渲染 <button> · 复用 buttonVariants 保样式一致 */}
                    <DropdownMenu>
                        <DropdownMenuTrigger
                            className={buttonVariants({ variant: 'outline' })}
                            aria-label="更多操作"
                        >
                            <MoreHorizontal className="mr-2 h-4 w-4" />
                            更多操作
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" className="w-56">
                            <div className="px-2 py-1.5 text-info-tertiary uppercase tracking-wider font-semibold">报告操作</div>

                            {!isLegacy && diagnosisScope !== 'geo' && (
                                <DropdownMenuItem onClick={handleRegenerateReport} disabled={regenerating}>
                                    {regenerating ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
                                    生成 GEO 诊断
                                </DropdownMenuItem>
                            )}

                            <DropdownMenuItem onClick={() => setShowMaterialsEditor(!showMaterialsEditor)}>
                                <Building2 className="mr-2 h-4 w-4" />
                                {showMaterialsEditor ? '收起资料' : '补充企业资料'}
                            </DropdownMenuItem>

                            <DropdownMenuItem asChild>
                                <Link
                                    to={`/diagnosis/new?retest=1&original_id=${id}&brand_name=${encodeURIComponent(report.detail?.brand_name || '')}&original_score=${report.detail?.total_score || 0}&industry=${encodeURIComponent(report.detail?.industry || '')}`}
                                    className="flex items-center cursor-pointer"
                                >
                                    <RefreshCw className="mr-2 h-4 w-4" />
                                    复测
                                </Link>
                            </DropdownMenuItem>

                            <DropdownMenuSeparator />
                            <div className="px-2 py-1.5 text-info-tertiary uppercase tracking-wider font-semibold">分享 / 导出</div>

                            <DropdownMenuItem onClick={handleCopyShareLink}>
                                <Share2 className="mr-2 h-4 w-4" />
                                复制分享链接
                            </DropdownMenuItem>

                            <DropdownMenuItem onClick={() => setShowSharePoster(true)}>
                                <Share2 className="mr-2 h-4 w-4" />
                                分享海报
                            </DropdownMenuItem>

                            {!isCEnd && (
                                <DropdownMenuItem onClick={() => setExportModalOpen(true)}>
                                    <Download className="mr-2 h-4 w-4" />
                                    导出报告
                                </DropdownMenuItem>
                            )}
                        </DropdownMenuContent>
                    </DropdownMenu>
                    {/* HelpHint「复测」放在 DropdownMenu 外面同一行 · self-center 居中 */}
                    <HelpHint title="「复测」是什么?啥时候用?" className="self-center">
                        复测 = 用相同设置<b>再跑一次诊断</b>, 跟上次分数对比, 看做了 GEO 之后品牌在 AI 里的可见度有没有提升。
                        <br />一般在<b>发文一段时间后</b>(几周 / 一个月)做一次, 拿"分数从 X 涨到 Y"的对比给客户看效果、促续费。
                    </HelpHint>
                </div>
            </div>

            {/*
              * [P1-9 · 2026-07-26] 报告按钮主次重排。
              *  - 主按钮两个：「复制报告链接」+「打开报告」（发客户 / 自己先看一遍）
              *  - 「生成客户报价单」降为次要（ghost）
              *  - 删除「用报告写第一篇文章」：那条路是死路 —— /articles 是只读的
              *    历史文章库，写文章要报价完成后才能进，从诊断报告点过去只会看到空列表。
              *    连带删掉「更多操作」里指向同一路径的「生成文章」，不留悬空入口。
              */}
            {!isCEnd && (
                <>
                {/* 🔴 [包三 · 报告页]「给客户看的版本」入口。这排按钮原来只说动作
                    (复制链接 / 打开报告),没说**哪一个是对外的、客户点开会看到什么**。
                    她不知道对方看到什么就不敢发;不敢发,这条链路就断在报告页。 */}
                <div data-testid="report-customer-version"
                    className="mb-2 rounded-xl border border-border bg-muted/30 p-3 text-[13px] leading-relaxed">
                    <p className="font-medium text-foreground">下面这条链接就是给客户看的版本。</p>
                    <p className="text-muted-foreground mt-1">
                        客户点开后看到:得分、每一项的结论和依据。
                        <span className="text-foreground">不会看到</span>你的内部备注、成本和这一页的操作按钮;也不需要注册或登录。
                    </p>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row">
                    <Button className="flex-1 justify-center" onClick={handleCopyShareLink}>
                        <Share2 className="h-4 w-4 mr-1.5" />
                        复制报告链接
                    </Button>
                    <Button className="flex-1 justify-center" onClick={handleOpenReport} disabled={openingReport}>
                        {openingReport
                            ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                            : <ExternalLink className="h-4 w-4 mr-1.5" />}
                        打开报告
                    </Button>
                    <Button
                        variant="ghost"
                        className="justify-center text-muted-foreground hover:text-foreground sm:flex-none"
                        onClick={() => navigate('/pricing')}
                    >
                        <Calculator className="h-4 w-4 mr-1.5" />
                        生成客户报价单
                    </Button>
                </div>
                </>
            )}

            {/* 企业资料编辑器 */}
            {showMaterialsEditor && id && (
                <ClientMaterialsEditor
                    diagnosisId={parseInt(id)}
                    onSaved={() => { }}
                />
            )}

            {report.detail?.brand_id ? (
                <Suspense fallback={<div className="flex justify-center py-8"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>}>
                    <DiagnosisRecommendationBehavior
                        brandId={report.detail.brand_id}
                        hideWhenUnavailable
                    />
                </Suspense>
            ) : null}

            {id && (
                <>
                <ExportReportModal
                    open={exportModalOpen}
                    onOpenChange={setExportModalOpen}
                    diagnosisId={parseInt(id)}
                    brandName={report.detail?.brand_name || '诊断报告'}
                />
                <SharePosterDialog
                    open={showSharePoster}
                    onClose={() => setShowSharePoster(false)}
                    type="report"
                    params={{ diagnosis_id: parseInt(id) }}
                />
                </>
            )}

            <ManualCopyDialog text={manualCopyText} onClose={() => setManualCopyText(null)} />
            <ManualOpenDialog
                url={manualOpenUrl}
                onClose={() => setManualOpenUrl(null)}
                title="报告没能自动打开"
                hint="这就是客户点开看到的那份报告。点下面的按钮打开，或长按链接复制发给客户。"
            />

            <Card className="border border-border rounded-xl shadow-none">
                <CardHeader className="p-5 pb-3">
                    <CardTitle>诊断报告详情</CardTitle>
                </CardHeader>
                <CardContent className="p-5 pt-0">
                    {report.content ? (
                        <div ref={reportRef}>
                            {/* 🔴 [#63 2026-09-05] 闸多一个条件:**有卡才走五卡视图**。
                                CUR-01 定义的是「**有**投影时走五卡、不进 legacy view」,
                                它对「投影为空」这一档从未定义 —— 这是补档,不是推翻。
                                原来只判 isV2:cards=[] 时渲染五个空壳、每张写「暂无结论」,
                                而且**永远走不到 ReportV2View** —— 331KB 完整内容一直在,
                                只是被这一行挡住了。诊断 692 的「五卡全暂无结论 + 零出口」就是它。 */}
                            {defgeoPresentation?.isV2 && defgeoPresentation.cards.length === 0 && (
                                <p role="note" className="mb-3 rounded-md border border-border bg-muted/50 p-3 text-[13px] text-muted-foreground">
                                    {DEFGEO_COPY.fiveCardSummaryUnavailable}
                                </p>
                            )}
                            {defgeoPresentation?.isV2 && defgeoPresentation.cards.length > 0 ? (
                                <DefensiveReportView presentation={defgeoPresentation} />
                            ) : isReportV2(report.content) ? (
                                // A.5 (CTO-15.9 session 3) · v2 8 模块装配版 → Card 拆分
                                // 板块 A(2026-07-22) · 传 diagnosisId:Module 3 改结构化逐格判定 + 人工确认,
                                // 确认后总分/等级徽章就地更新(评分 SSOT 重算结果)
                                <ReportV2View
                                    content={report.content}
                                    diagnosisId={id ? parseInt(id) : undefined}
                                    onScoreUpdated={(score, level) => {
                                        setReport(prev => prev.detail ? {
                                            ...prev,
                                            detail: {
                                                ...prev.detail,
                                                ...(score !== null ? { total_score: score } : {}),
                                                ...(level !== null ? { level } : {}),
                                            },
                                        } : prev);
                                        // 页头徽章已就地更新；正文 v2 模块内嵌旧分数文本 → 出角标提示重新加载
                                        setScoreRecalculatedAt(new Date());
                                    }}
                                />
                            ) : (
                                <div className="prose prose-neutral max-w-none dark:prose-invert prose-table:border-collapse prose-th:border prose-th:border-border prose-th:p-2 prose-th:bg-muted prose-td:border prose-td:border-border prose-td:p-2 overflow-x-auto">
                                    <ReactMarkdown>{report.content}</ReactMarkdown>
                                </div>
                            )}
                        </div>
                    ) : (
                        <div className="text-center py-12 text-muted-foreground">
                            <FileText className="h-12 w-12 mx-auto mb-3 opacity-40" />
                            <p className="text-lg font-medium">报告内容暂未生成</p>
                            <p className="text-sm mt-1">可能诊断仍在进行中，或报告生成失败。请尝试重新生成。</p>
                            <Button
                                variant="outline"
                                className="mt-4"
                                disabled={regenerating}
                                onClick={async () => {
                                    setRegenerating(true);
                                    try {
                                        await authFetch(`/api/diagnosis/${id}/regenerate-report`, { method: "POST" });
                                        window.location.reload();
                                    } catch {
                                        toast.error("重新生成失败，请稍后重试");
                                    } finally {
                                        setRegenerating(false);
                                    }
                                }}
                            >
                                {regenerating ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" />生成中...</> : "重新生成报告"}
                            </Button>
                        </div>
                    )}
                </CardContent>
            </Card>

            {/* C3.2 (CTO-15.9 session 3 · M2 §P1.4) 19 次诊断 score 趋势 */}
            {scoreHistory.length >= 2 && (
                <Card className="border border-border rounded-xl shadow-none mt-4">
                    <CardHeader className="p-5 pb-3">
                        <CardTitle className="flex items-center gap-2">
                            <BarChart3 className="h-5 w-5 text-brand" />
                            诊断 score 趋势(近 {scoreHistory.length} 次)
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="p-5 pt-0">
                        <ResponsiveContainer width="100%" height={240}>
                            <LineChart data={scoreHistory.map((it, i) => ({
                                idx: i + 1,
                                date: it.date ? new Date(it.date).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' }) : `#${i + 1}`,
                                score: it.total_score,
                            }))}>
                                <CartesianGrid strokeDasharray="3 3" />
                                <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                                <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
                                <Tooltip />
                                <Line type="monotone" dataKey="score" stroke="hsl(var(--primary))" strokeWidth={2} dot={{ r: 3 }} />
                            </LineChart>
                        </ResponsiveContainer>
                        <p className="text-[11px] text-muted-foreground mt-2 leading-relaxed">
                            * 第 1 次 score: <strong>{scoreHistory[0].total_score}</strong> ·
                            最新 score: <strong>{scoreHistory[scoreHistory.length - 1].total_score}</strong> ·
                            趋势 <strong className={
                                scoreHistory[scoreHistory.length - 1].total_score - scoreHistory[0].total_score > 0
                                    ? 'text-emerald-600' : 'text-rose-600'
                            }>
                                {scoreHistory[scoreHistory.length - 1].total_score - scoreHistory[0].total_score >= 0 ? '+' : ''}
                                {scoreHistory[scoreHistory.length - 1].total_score - scoreHistory[0].total_score}
                            </strong> 分
                        </p>
                    </CardContent>
                </Card>
            )}

            {/* ===== 沙盒 第一步→第二步 过场话术窗 ===== */}
            <Dialog open={showStep2Bridge} onOpenChange={() => { /* 锁住 · 只能点按钮继续 */ }}>
                <DialogContent className="max-w-lg [&>button]:hidden">
                    <DialogTitle className="text-lg flex items-center gap-2">
                        <Sparkles className="h-5 w-5 text-amber-500" />
                        第一步完成!体检报告出来了
                    </DialogTitle>
                    <DialogDescription className="text-sm mt-2 leading-6">
                        客户看到自己在 AI 里几乎搜不到(26 分 · 危急级),很认可这次诊断——这正是签约的最好时机。
                        <br /><br />
                        <strong className="text-foreground">下一步:</strong> 趁热打铁, 基于这份诊断给客户出一份报价方案, 促成签约收款。
                    </DialogDescription>
                    <DialogFooter className="mt-4">
                        <Button
                            className="bg-amber-500 hover:bg-amber-600 text-white"
                            onClick={() => {
                                setShowStep2Bridge(false);
                                setTutorialStage('step2-sidebar'); // 点亮侧边栏「报价方案」做强制衔接
                            }}
                        >
                            <Calculator className="h-4 w-4 mr-1.5" />
                            去生成报价方案 (第二步) →
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    );
}
