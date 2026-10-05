/**
 * ReportPage — 公开 GEO 诊断报告 · 赛马候选 A 主组件
 *
 * 状态机:loading → ready / not_ready / invalid_link / error
 * 数据纪律：全部经 transport 进入；生产接线传 createHttpTransport(),
 *          harness/测试传 createFixtureTransport()；本组件不含任何 fixture。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { MotionConfig, motion } from 'framer-motion';
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
} from '@/components/ui/dialog';
import type { PublicReportPageState } from '../contract/types';
import type { PublicReportTransport } from '../transport/transport';
import { useSectionFadeUp } from './motion/presets';
import { TopNav } from './sections/TopNav';
import { HeroSummary } from './sections/HeroSummary';
import { DecisionFunnel } from './sections/DecisionFunnel';
import { IdentityCalibration } from './sections/IdentityCalibration';
import { PlatformPerformance } from './sections/PlatformPerformance';
import { KeyFindings } from './sections/KeyFindings';
import { EvidenceMatrix } from './sections/EvidenceMatrix';
import { CompetitiveLandscape } from './sections/CompetitiveLandscape';
import { PriorityActions } from './sections/PriorityActions';
import { ThirtyDayPlan } from './sections/ThirtyDayPlan';
import { Narrative } from './sections/Narrative';
import { Methodology } from './sections/Methodology';
import { ReportFooter } from './sections/ReportFooter';
import {
    ErrorState,
    InvalidLinkState,
    LoadingSkeleton,
    NotReadyState,
} from './states/PageStates';
import { mountOpened } from '@/lib/customerEvents';
import { OssAttribution } from '@/components/common/OssAttribution';
import '../theme.css';

export type PublicReportTheme = 'light' | 'dark';

const REPORT_THEME_STORAGE_KEY = 'omnirank-public-report-theme';

function initialReportTheme(): PublicReportTheme {
    try {
        return window.localStorage.getItem(REPORT_THEME_STORAGE_KEY) === 'dark' ? 'dark' : 'light';
    } catch {
        return 'light';
    }
}

export interface PublicReportPageProps {
    readonly transport: PublicReportTransport;
    readonly reportId: string;
    readonly shareToken: string | null;
}

/** "数据怎么看" 静态说明(不含业务数据) */
function HelpDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
    return (
        <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
            <DialogContent className="max-w-lg">
                <DialogHeader>
                    <DialogTitle>数据怎么看</DialogTitle>
                    <DialogDescription>一分钟读懂这份报告</DialogDescription>
                </DialogHeader>
                <div className="space-y-3 text-sm leading-6 text-slate-600">
                    <p>
                        <strong className="text-slate-800">GEO 综合评分</strong>
                        :AI 问答平台在实测问题中识别到你、把你说进选项的综合表现(0-100)，等级由统一阈值映射。
                    </p>
                    <p>
                        <strong className="text-slate-800">资料完整度</strong>
                        :本次判断依据是否充足，与 GEO 评分是两个独立指标，互不代表。
                    </p>
                    <p>
                        <strong className="text-slate-800">决策漏斗</strong>
                        :从"客户搜你品牌名时 AI 答得对"到"高决策问题里 AI 引用你"的逐层留存；哪一层掉得多，就先补哪一层。
                    </p>
                    <p>
                        <strong className="text-slate-800">证据等级</strong>:A=直接证据(可下结论)、B=参考证据(倾向性参考)、C=待验证线索(只表示可能或推测，需进一步验证)。
                    </p>
                    <p>
                        <strong className="text-slate-800">"—" 与 0 的区别</strong>:0 是测出来的结果，"—" 是本次没有有效测量，两者不能混为一谈。
                    </p>
                </div>
            </DialogContent>
        </Dialog>
    );
}

export function PublicReportPage({ transport, reportId, shareToken }: PublicReportPageProps) {
    const [state, setState] = useState<PublicReportPageState>({ kind: 'loading' });
    const [helpOpen, setHelpOpen] = useState(false);
    const [theme, setTheme] = useState<PublicReportTheme>(initialReportTheme);
    // 帮助对话框焦点还原(受控 Dialog 无 DialogTrigger,手动归还焦点)
    const helpInvokerRef = useRef<HTMLElement | null>(null);
    const mainVariants = useSectionFadeUp();
    // 请求竞态守卫:代际序号 + 卸载保护 + AbortController
    // 报告 A→B 切换/连续重试时,旧响应(后到的 A/上一次重试)一律丢弃,绝不串报告
    const requestSeqRef = useRef(0);
    const abortRef = useRef<AbortController | null>(null);
    const mountedRef = useRef(true);

    useEffect(() => {
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
            abortRef.current?.abort();
        };
    }, []);

    // Public reports own their presentation theme.  The authenticated app's
    // global `.dark` class must not half-invert a light report.  Restore the
    // previous app theme when leaving this standalone route.
    useEffect(() => {
        const root = document.documentElement;
        const previousDark = root.classList.contains('dark');
        const previousColorScheme = root.style.colorScheme;
        return () => {
            root.classList.toggle('dark', previousDark);
            root.style.colorScheme = previousColorScheme;
        };
    }, []);

    useEffect(() => {
        const root = document.documentElement;
        root.classList.toggle('dark', theme === 'dark');
        root.style.colorScheme = theme;
        try {
            window.localStorage.setItem(REPORT_THEME_STORAGE_KEY, theme);
        } catch {
            // Storage can be disabled in privacy mode; the in-memory choice
            // still works for the current report.
        }
    }, [theme]);

    const load = useCallback(async () => {
        // Invalidate and cancel the previous generation before validating the next route.
        // Otherwise a slow valid report can overwrite a newly selected invalid-link state.
        const seq = ++requestSeqRef.current;
        abortRef.current?.abort();
        abortRef.current = null;
        if (!/^\d+$/.test(reportId)) {
            setState({ kind: 'invalid_link', message: '链接已失效。请联系发你链接的人重新发送。' });
            return;
        }
        const controller = new AbortController();
        abortRef.current = controller;
        setState({ kind: 'loading' });
        let result: Awaited<ReturnType<PublicReportTransport['loadReport']>>;
        try {
            result = await transport.loadReport({ reportId, shareToken, signal: controller.signal });
        } catch {
            return; // transport 自身已吞错;这里防御取消/异常,不落任何状态
        }
        // 代际过期(已有更新的请求)或已卸载 → 丢弃旧响应
        if (seq !== requestSeqRef.current || !mountedRef.current) return;
        setState(result);
    }, [transport, reportId, shareToken]);

    useEffect(() => {
        void load();
    }, [load]);

    useEffect(() => {
        if (state.kind !== 'ready') return;
        const diagnosisId = Number(reportId);
        if (!Number.isSafeInteger(diagnosisId) || diagnosisId <= 0) return;
        return mountOpened({ source: 'public_report', diagnosisId });
    }, [reportId, state.kind]);

    useEffect(() => {
        if (state.kind !== 'ready') return;
        const previousTitle = document.title;
        let description = document.querySelector<HTMLMetaElement>('meta[name="description"]');
        const createdDescription = !description;
        if (!description) {
            description = document.createElement('meta');
            description.name = 'description';
            document.head.appendChild(description);
        }
        const previousDescription = description.content;
        const brandName = state.report.identity.brandName || '品牌';
        const score = state.report.summary.geoScore;
        document.title = `${brandName} GEO 诊断报告 · AI 搜索表现`;
        description.content = score === null
            ? `${brandName} 的 GEO 诊断报告，展示本次 AI 实测结果与证据。`
            : `${brandName} 的 GEO 综合评分为 ${score}/100，查看本次 AI 实测结果与证据。`;
        return () => {
            document.title = previousTitle;
            if (createdDescription) description?.remove();
            else if (description) description.content = previousDescription;
        };
    }, [state]);

    const openHelp = useCallback((invoker?: HTMLElement) => {
        helpInvokerRef.current = invoker ?? null;
        setHelpOpen(true);
    }, []);

    const closeHelp = useCallback(() => {
        setHelpOpen(false);
        // 关闭后把焦点还给触发按钮(若仍在文档中)
        window.setTimeout(() => {
            const invoker = helpInvokerRef.current;
            if (invoker && document.contains(invoker)) invoker.focus();
        }, 0);
    }, []);

    return (
        <MotionConfig reducedMotion="user">
            <div
                className="prp-report min-h-screen bg-slate-50 text-slate-900 antialiased"
                data-public-report-theme={theme}
            >
                {state.kind === 'loading' && <LoadingSkeleton />}

                {state.kind === 'not_ready' && (
                    <NotReadyState message={state.message} onRetry={() => void load()} />
                )}

                {state.kind === 'invalid_link' && <InvalidLinkState message={state.message} />}

                {state.kind === 'error' && (
                    <ErrorState message={state.message} onRetry={() => void load()} />
                )}

                {state.kind === 'ready' && (
                    <>
                        <TopNav
                            whitelabel={state.report.identity.whitelabel}
                            brandingStatus={state.report.identity.brandingStatus}
                            onOpenHelp={openHelp}
                            theme={theme}
                            onThemeChange={setTheme}
                        />
                        <HeroSummary report={state.report} />
                        {/*
                          * [WO 2026-08-07] 服务商视角的校准块。
                          * 🔴 渲染条件就是数据在不在 —— 匿名访客的响应体里服务端
                          * 已经把 calibration 整段裁掉了,前端**不再判一次身份**
                          * (那会成为第二处口径)。放在页顶:软引导"校准后再发给客户"。
                          */}
                        {state.report.calibration ? (
                            <IdentityCalibration
                                calibration={state.report.calibration}
                                reportId={reportId}
                                shareToken={shareToken}
                            />
                        ) : null}
                        <motion.main
                            variants={mainVariants}
                            initial="hidden"
                            animate="show"
                            className="mx-auto w-full max-w-[1320px] px-4 sm:px-6 lg:px-10"
                        >
                            <DecisionFunnel section={state.report.funnel} />
                            <PlatformPerformance
                                section={state.report.platforms}
                                /* [#238 甲] 与竞争格局同源的那一个数;该节取不到就传 null ⇒ 不说那句 */
                                excludedBrandDirectedCount={
                                    state.report.competitive.status === 'ready'
                                        ? (state.report.competitive.data.excludedBrandDirectedCount ?? null)
                                        : null
                                }
                                evidenceItems={
                                    state.report.evidence.status === 'ready'
                                        ? state.report.evidence.data.items
                                        : null
                                }
                            />
                            <KeyFindings section={state.report.findings} />
                            <EvidenceMatrix section={state.report.evidence} />
                            <CompetitiveLandscape section={state.report.competitive} />
                            <PriorityActions
                                section={state.report.actions}
                                evidenceRowKeys={
                                    state.report.evidence.status === 'ready'
                                        ? state.report.evidence.data.items.map((e) => e.rowKey)
                                        : []
                                }
                            />
                            <ThirtyDayPlan section={state.report.thirtyDayPlan} />
                            <Narrative markdown={state.report.narrativeMd} />
                            <Methodology notes={state.report.methodology} />
                        </motion.main>
                        <ReportFooter
                            generatedAt={state.report.identity.dataUpdatedAt ?? state.report.identity.diagnosedAt}
                            versionLabel={state.report.identity.reportVersionLabel}
                            whitelabel={
                                // 白标门控：仅后端批准才展示，与 TopNav 一致(未批准 → 中性页尾)
                                state.report.identity.brandingStatus === 'approved_whitelabel'
                                    ? state.report.identity.whitelabel
                                    : null
                            }
                        />
                        {/* WO_329 开源版署名位:独立组件(ReportFooter 不写品牌字);白标与否都出;开关关 ⇒ 不渲染 */}
                        <OssAttribution />
                        <HelpDialog open={helpOpen} onClose={closeHelp} />
                    </>
                )}
            </div>
        </MotionConfig>
    );
}
