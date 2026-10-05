/**
 * Monitoring page — orchestrator that manages state and delegates rendering
 * to sub-components under ./components/.
 */
import { authFetch } from '@/lib/api';
import { copyToClipboard } from '@/lib/copyUtils';
import { lazyToast } from '@/lib/lazyToast';
import { useAuth } from '@/context/AuthContext';
import { useBranding } from '@/hooks/useWhitelabel';
import { useWallet } from '@/context/WalletContext';
import { useOrganization } from '@/context/OrganizationContext';
// v1_3 (CTO-15.1 2026-04-19): 动态价目表(全站静默扣费 · 不前置展示价格 · useFeatureCost 仍用于余额/freeze)
import { usePricing } from '@/context/PricingContext';
import {
    PRICING_UNAVAILABLE_LABEL, PricingUnavailableHint,
} from '@/components/pricing/PricingUnavailableHint';
import { useConfirmLargeDeduction } from '@/hooks/useConfirmLargeDeduction';
import { useState, useEffect, useLayoutEffect, useRef, useMemo, lazy, Suspense } from 'react';
import { useDirtyForm } from '@/hooks/useDirtyForm';
import { useClientContext } from '@/context/ClientContext';
import { useActiveDemoSelection } from '@/lib/demoMode';
import { useLocation, useSearchParams, useNavigate, useNavigationType } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
/* [WO_251 §4] 客户行的显示口径单点 —— 现值优先、缺字段才回落快照、都没有给显示默认值。 */
import { clientDisplayIndustry, clientDisplayName } from '@/lib/clientDisplay';
import { Activity, RefreshCw, FileText, Loader2, BarChart3, TrendingUp, CheckCircle2, Circle, ArrowRight, Clock, AlertCircle, FileBarChart } from 'lucide-react';
// CTO-15.20 v2 · C.2:旧版顶部 BridgeBanner(决策条 + 主按钮 + 回 M3)
// (替换 v1 仅 BackToM3Button · 补 M3 决策条)
import { BridgeBanner } from '@/components/workbench/BridgeBanner';
// CTO-15.20 v2 · D.1:测试客户隔离 hook + toggle
import { useTestClientFilter, TestClientFilterToggle } from '@/components/workbench/TestClientFilterToggle';
import { useBrandsTestMap } from '@/hooks/useBrandsTestMap';
import { useMarkStepCompleted } from '@/hooks/useMarkStepCompleted';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { isSandboxActive, exitSandbox } from '@/sandbox/sandboxState';
import { useTutorialStage, setTutorialStage } from '@/sandbox/tutorialStage';
import { FastForward, Sparkles, Trophy } from 'lucide-react';
import { LazyFeatureTooltip as FeatureTooltip } from '@/components/onboarding/LazyFeatureTooltip';
import { Dialog, DialogContent, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';


const ObservationWorkbench = lazy(() => import('@/features/geoObservation').then(m => ({
    default: m.MonitoringObservationWorkbench,
})));
const LegacyInsightsCenter = lazy(() => import('@/pages/Insights/InsightsCenter'));

const AddKeywordDialog = lazy(() => import('./components/AddKeywordDialog').then(m => ({ default: m.AddKeywordDialog })));
const TokenDialog = lazy(() => import('./components/TokenDialog').then(m => ({ default: m.TokenDialog })));
const PublicationDialog = lazy(() => import('./components/PublicationDialog').then(m => ({ default: m.PublicationDialog })));
const TrendChartDialog = lazy(() => import('./components/TrendChartDialog').then(m => ({ default: m.TrendChartDialog })));
const OperationLogsDialog = lazy(() => import('./components/OperationLogsDialog').then(m => ({ default: m.OperationLogsDialog })));
const ReportGenDialog = lazy(() => import('./components/ReportGenDialog').then(m => ({ default: m.ReportGenDialog })));
const MonitoringResultsDialog = lazy(() => import('./components/MonitoringResultsDialog').then(m => ({ default: m.MonitoringResultsDialog })));
const RollbackDialog = lazy(() => import('./components/RollbackDialog').then(m => ({ default: m.RollbackDialog })));
const ClearDataDialog = lazy(() => import('./components/ClearDataDialog').then(m => ({ default: m.ClearDataDialog })));
const ScheduleDialog = lazy(() => import('./components/ScheduleDialog').then(m => ({ default: m.ScheduleDialog })));
const ArchivesDialog = lazy(() => import('./components/ArchivesDialog').then(m => ({ default: m.ArchivesDialog })));
const ServiceConfigDialog = lazy(() => import('./components/ServiceConfigDialog').then(m => ({ default: m.ServiceConfigDialog })));
const PlatformWeightsDialog = lazy(() => import('./components/PlatformWeightsDialog').then(m => ({ default: m.PlatformWeightsDialog })));


import type { Client, Keyword, Publication, TokenInfo, TrendData, OperationLog, MonitoringResult, MonitoringCell, ProgressLog } from './types';
import { ActionCards } from './components/ActionCards';
import { ProgressPanel } from './components/ProgressPanel';
import { STREAM_STALL_MS } from './monitoringPollSchedule';
import { KeywordTable } from './components/KeywordTable';
import { ArchivedKeywordsList } from './components/ArchivedKeywordsList';
import { IdentityReviewPanel } from './components/IdentityReviewPanel';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { safeRandomUUID } from '@/lib/safeRandomUUID';
import { NextStepBar } from '@/components/nav/NextStepBar';

// CTO-15.20 v2 · C.2:BridgeBanner 内部自动读 URL ?brand_id=X · 此处不需要 helper

// [2026-06-04 视觉] 趋势快照小折线(无依赖内联 SVG · 喂现有 trendData · 纯展示)
function MiniSparkline({ data }: { data: (number | null | undefined)[] }) {
    const pts = data.filter((d): d is number => typeof d === 'number');
    if (pts.length < 2) return null;
    const max = Math.max(...pts), min = Math.min(...pts);
    const range = max - min || 1;
    const w = 132, h = 40;
    const coords = pts.map((v, i) => `${(i / (pts.length - 1)) * w},${h - ((v - min) / range) * (h - 4) - 2}`);
    const up = pts[pts.length - 1] >= pts[0];
    return (
        <svg width={w} height={h} className="overflow-visible" aria-hidden>
            <polyline points={coords.join(' ')} fill="none" stroke={up ? 'hsl(var(--brand))' : 'hsl(0 70% 60%)'} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx={coords[coords.length - 1].split(',')[0]} cy={coords[coords.length - 1].split(',')[1]} r="2.5" fill={up ? 'hsl(var(--brand))' : 'hsl(0 70% 60%)'} />
        </svg>
    );
}

export default function MonitoringPage() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const {
        clientContext,
        clients: authorizedClients,
        currentBrandId,
        error: clientContextError,
        listReady,
        switchClient,
    } = useClientContext();
    const activeDemoSelection = useActiveDemoSelection();
    const { isMember } = useOrganization();
    const location = useLocation();
    const navigationType = useNavigationType();
    const navigate = useNavigate();
    const [, setSearchParams] = useSearchParams();
    const urlBrandId = useMemo(() => {
        const raw = new URLSearchParams(location.search).get('brand_id');
        const parsed = raw ? parseInt(raw, 10) : null;
        return parsed && Number.isFinite(parsed) && parsed > 0 ? parsed : null;
    }, [location.search]);
    const urlBrandRejected = listReady
        && urlBrandId !== null
        && !authorizedClients.some(client => client.id === urlBrandId);
    const urlSelectionUnresolved = urlBrandId !== null && currentBrandId !== urlBrandId;
    // [P0 fix 2026-05-25 r14] sidebar 切换客户 currentBrandId 立即生效 · URL 兜底
    // 旧 BUG:urlBrandId ?? currentBrandId · 进入时 URL 带 ?brand_id 锁死 sidebar 切不动
    // 新:currentBrandId(sidebar 主动)优先 · URL 仅在 sidebar 未选时兜底
    const activeBrandId = currentBrandId ?? urlBrandId ?? null;
    const hasResolvedActiveContext = activeBrandId === null
        || clientContext?.brand.id === activeBrandId;
    const isDemoAccess = Boolean(
        activeBrandId !== null
        && hasResolvedActiveContext
        && clientContext?.access_mode === 'demo'
        && activeDemoSelection?.brandId === activeBrandId
        && activeDemoSelection.caseId === clientContext.demo_case_id
    );
    const activeAccessMode: 'live' | 'demo' | 'pending' = urlBrandRejected || urlSelectionUnresolved
        ? 'pending'
        : isDemoAccess ? 'demo'
            : hasResolvedActiveContext ? 'live' : 'pending';
    const accessScopeKey = `${activeAccessMode}:${activeBrandId ?? 'none'}:${activeDemoSelection?.caseId ?? 'live'}`;
    const urlHydratedRef = useRef(false);
    const urlSelectionPendingRef = useRef<number | null | undefined>(undefined);
    const previousLocationKeyRef = useRef<string | null>(null);

    useEffect(() => {
        if (!listReady) return;
        const urlClient = urlBrandId === null
            ? null
            : authorizedClients.find(client => client.id === urlBrandId);
        const locationChanged = previousLocationKeyRef.current !== location.key;
        const shouldApplyUrl = !urlHydratedRef.current
            || (locationChanged && navigationType === 'POP');
        urlHydratedRef.current = true;
        previousLocationKeyRef.current = location.key;
        if (!shouldApplyUrl) return;
        // [M-2 2026-07-28] 第二条清空路径修复:URL 没带 brand_id = 对选择"无主张"。
        // 旧逻辑把裸 /monitoring 当成"选择应清空"去执行空切换——从侧栏点
        // "效果监测"进来必然清掉已选客户,随后默认重选 + 上下文异步加载让
        // accessScopeKey 连续翻转,整页数据反复清空重拉(闪断/未成形容器)。
        // 选择是 UI 状态不是凭证:只有 URL 明确点名品牌时才应用 URL;
        // 裸 URL 由下方同步 effect 把当前选择吸附进 URL(replace,不制造历史陷阱)。
        if (urlBrandId === null) return;
        if (!urlClient) return;
        if (currentBrandId !== urlBrandId) {
            urlSelectionPendingRef.current = urlBrandId;
            switchClient(urlBrandId);
        }
    }, [authorizedClients, currentBrandId, listReady, location.key, navigationType, switchClient, urlBrandId]);

    // Sidebar changes create a history entry; popstate is applied above through
    // the same authorized switchClient path, so back/forward cannot bypass scope.
    useEffect(() => {
        if (!urlHydratedRef.current || urlBrandRejected) return;
        if (urlSelectionPendingRef.current !== undefined) {
            if (currentBrandId !== urlSelectionPendingRef.current) return;
            urlSelectionPendingRef.current = undefined;
        }
        if (currentBrandId !== null && currentBrandId !== urlBrandId) {
            setSearchParams((p) => {
                const np = new URLSearchParams(p);
                np.set('brand_id', String(currentBrandId));
                return np;
                // 裸 URL 吸附当前选择用 replace(进页即写,不该多出一层历史);
                // URL 已有别的品牌(sidebar 主动切换)保持 push,回退仍可回到上一客户。
            }, { replace: urlBrandId === null });
        }
    }, [currentBrandId, setSearchParams, urlBrandId, urlBrandRejected]);
    const globalQuoteIds = clientContext?.relatedQuoteIds || [];
    const { user } = useAuth();
    // /api/auth/me 是权威身份来源；只有真实布尔值 true 才开放管理员控制。
    const isAdmin = user?.is_admin === true;
    // v3.6 白标(oem)：新手引导完成弹窗标题用代理品牌名。
    // D2（Owner 2026-07-22）：product_name 属后台换肤字段 · 仅 backoffice 授权才出；
    // 未授权时 hook 的 brand 已解析为平台默认(SSOT · company_name=OmniRank · 全域上榜)，fail-closed。
    const { brand: monitoringBrand, backofficeBrandAllowed } = useBranding({ userId: user?.id });
    const monitoringBrandName = (backofficeBrandAllowed ? monitoringBrand?.product_name : undefined) || monitoringBrand?.company_name || '';
    // CTO-13.0: 三级可见 — admin / 代理 / 普通用户
    const { notifyFreezeHold } = useWallet();
    // v1_3 (CTO-15.1 2026-04-19): 动态价目表 + 大额 toast 确认
    const { getTrustedCost, loading: pricingLoading, error: pricingError,
        refresh: pricingRefresh, retryNotBefore: pricingRetryNotBefore } = usePricing();
    const monitorSingleCost = getTrustedCost('monitor_single');
    const confirmLarge = useConfirmLargeDeduction(1000);
    const markStep = useMarkStepCompleted();
    /* 🔴 [#222 a1b'] 这里原有 `isAgent` / `agentOrAdmin`,只喂两处:ActionCards 的身份 prop
       与页面标题的身份分叉。两者都已撤,变量随之作废 —— 留着会让下一个人以为本页仍按身份分流。 */

    // 沙盒态 · step 4 教程引导
    const sandboxActive = isSandboxActive();
    const tutorialStage = useTutorialStage();
    const inStep4 = sandboxActive && tutorialStage.startsWith('step4-');

    // "时间快进"柔和过渡 · 两个时机: 进第四步(快进 10 天) / 补发后(又过 8 天)
    const [showTimeSkip, setShowTimeSkip] = useState(false);
    const [timeSkipFading, setTimeSkipFading] = useState(false);
    const [timeSkipVariant, setTimeSkipVariant] = useState<'intro' | 'recovered'>('intro');
    const timeSkipShownRef = useRef<{ intro: boolean; recovered: boolean }>({ intro: false, recovered: false });
    // (1) 进 step4-intro / step4-recovered 时各触发一次
    useEffect(() => {
        if (!sandboxActive) return;
        if (tutorialStage === 'step4-intro' && !timeSkipShownRef.current.intro) {
            timeSkipShownRef.current.intro = true;
            setTimeSkipVariant('intro');
            setTimeSkipFading(false);
            setShowTimeSkip(true);
        } else if (tutorialStage === 'step4-recovered' && !timeSkipShownRef.current.recovered) {
            timeSkipShownRef.current.recovered = true;
            setTimeSkipVariant('recovered');
            setTimeSkipFading(false);
            setShowTimeSkip(true);
        }
    }, [sandboxActive, tutorialStage]);
    // (2) 显示后自动淡出+卸载 (依赖 showTimeSkip · StrictMode 双跑也能正确重设定时器)
    useEffect(() => {
        if (!showTimeSkip) return;
        const tFade = window.setTimeout(() => setTimeSkipFading(true), 2400);
        const tHide = window.setTimeout(() => setShowTimeSkip(false), 2900);
        return () => { window.clearTimeout(tFade); window.clearTimeout(tHide); };
    }, [showTimeSkip]);
    const dismissTimeSkip = () => { setTimeSkipFading(true); window.setTimeout(() => setShowTimeSkip(false), 300); };
    // 收尾窗打开(step4-finish)→ 撒花庆祝
    const finishConfettiRef = useRef(false);
    useEffect(() => {
        if (sandboxActive && tutorialStage === 'step4-finish' && !finishConfettiRef.current) {
            finishConfettiRef.current = true;
            window.setTimeout(() => { void import('@/lib/confetti').then(({ fireBigCelebration }) => fireBigCelebration()); }, 250);
        }
    }, [sandboxActive, tutorialStage]);
    const timeSkipText = timeSkipVariant === 'intro'
        ? { sub: '签约发文后, 教程帮你快进', big: '10 天后', tip: '看这个客户持续发文 10 天的真实效果(演示)' }
        : { sub: '补发的文章已发出 · 又过了', big: '8 天', tip: '看看那个词的出现率追上来没有' };

    // CTO-15.20 v2 · D.1:测试客户隔离(默认 ON · admin 全部客户模式下需要)
    const [includeTest, setIncludeTest] = useTestClientFilter();
    const { isTest, testCount } = useBrandsTestMap();

    // Core state
    const [clients, setClients] = useState<Client[]>([]);
    const [clientsLoading, setClientsLoading] = useState(true);
    const [clientsError, setClientsError] = useState<string | null>(null);
    const [snapshotMissing, setSnapshotMissing] = useState(false);
    const [selectedClient, setSelectedClient] = useState<string>('');
    const [keywords, setKeywords] = useState<Keyword[]>([]);
    const [clientAppearanceRate, setClientAppearanceRate] = useState<number | null>(null);  // [2026-06-30 kou-jing] backend authority top-avg
    // [§4.2 闭环] 超红海词单独区(高竞争·需单独报价·不计达标·不混普通监测列表)
    const [superRedOceanKws, setSuperRedOceanKws] = useState<Array<{ id: number; keyword: string; competition_ratio?: number; note?: string; detection_details?: Array<{ platform: string; is_detected: boolean; snippet?: string; tested_at?: string; pending?: boolean }> }>>([]);
    const [publications, setPublications] = useState<Publication[]>([]);
    const [tokenInfo, setTokenInfo] = useState<TokenInfo | null>(null);
    const [trendData, setTrendData] = useState<TrendData[]>([]);
    const [trendError, setTrendError] = useState<string | null>(null);
    const [operationLogs, setOperationLogs] = useState<OperationLog[]>([]);
    const [loading, setLoading] = useState(false);
    const [selectedKeywords, setSelectedKeywords] = useState<Set<string>>(new Set());

    // 监测执行是购买短句后的主工作流；数据洞察是按需查看的分析视图。
    // 身份异步刷新不得替用户切换页面，避免演示和日常操作突然跳走。
    const [showInsights, setShowInsights] = useState(false);
    const selectMonitoringView = (showObservationWorkbench: boolean) => {
        setShowInsights(showObservationWorkbench);
    };

    // Dialog visibility
    const [showAddDialog, setShowAddDialog] = useState(false);
    const [showPublicationDialog, setShowPublicationDialog] = useState(false);
    const [showTokenDialog, setShowTokenDialog] = useState(false);
    const [showTrendDialog, setShowTrendDialog] = useState(false);
    const [showLogsDialog, setShowLogsDialog] = useState(false);
    const [showResultsDialog, setShowResultsDialog] = useState(false);
    const [showRollbackDialog, setShowRollbackDialog] = useState(false);
    const [showClearDialog, setShowClearDialog] = useState(false);
    const [showArchivesDialog, setShowArchivesDialog] = useState(false);
    const [showScheduleDialog, setShowScheduleDialog] = useState(false);
    const [showReportGenDialog, setShowReportGenDialog] = useState(false);
    const [showServiceConfigDialog, setShowServiceConfigDialog] = useState(false);
    const [showWeightsDialog, setShowWeightsDialog] = useState(false);

    // Monitoring results & rollback
    const [monitoringResults, setMonitoringResults] = useState<MonitoringResult[]>([]);
    const [rollbackTasks, setRollbackTasks] = useState<any[]>([]);
    const [selectedRollbackIds, setSelectedRollbackIds] = useState<Set<number>>(new Set());
    const [rollbackLoading, setRollbackLoading] = useState(false);
    const [syncingTaskId, setSyncingTaskId] = useState<number | null>(null);
    const [latestUnsyncedTaskId, setLatestUnsyncedTaskId] = useState<number | null>(null);
    const [copied, setCopied] = useState(false);

    // Clear data
    const [clearPassword, setClearPassword] = useState('');
    const [clearReason, setClearReason] = useState('');
    const [clearLoading, setClearLoading] = useState(false);
    const [archives, setArchives] = useState<any[]>([]);
    const [restoreLoading, setRestoreLoading] = useState<string | null>(null);

    // Schedule
    const [scheduleHour, setScheduleHour] = useState(16);
    const [scheduleMinute, setScheduleMinute] = useState(0);
    const [scheduleEnabled, setScheduleEnabled] = useState(false);
    const [scheduleStatus, setScheduleStatus] = useState<any>(null);
    const [scheduleLoading, setScheduleLoading] = useState(false);
    const [clientMonitoringEnabled, setClientMonitoringEnabled] = useState(false);
    const [monitoringIntervalHours, setMonitoringIntervalHours] = useState(24);
    const [monitoringStartHour, setMonitoringStartHour] = useState(8);
    const [clientMonitoringConfig, setClientMonitoringConfig] = useState<any>(null);

    // Report generation
    const [reportGenType, setReportGenType] = useState<string>('weekly');
    const [reportGenLoading, setReportGenLoading] = useState(false);
    const [reportGenResult, setReportGenResult] = useState<string>('');

    // Service config
    const [serviceConfigDays, setServiceConfigDays] = useState<number>(365);
    const [serviceConfigCustom, setServiceConfigCustom] = useState<string>('');
    const [serviceConfigLoading, setServiceConfigLoading] = useState(false);
    const [currentServiceDays, setCurrentServiceDays] = useState<number | null>(null);
    const [currentServiceStart, setCurrentServiceStart] = useState<string | null>(null);
    // [服务期 SSOT 2026-08-06] 日历服务期只从后端 contract_end_date 拿（前端不自算）
    const [currentServiceEnd, setCurrentServiceEnd] = useState<string | null>(null);
    const [currentServiceDaysLeft, setCurrentServiceDaysLeft] = useState<number | null>(null);
    // [客户反馈⑥ 2026-08-09] 自动监测**真实**排班资格(后端算,前端不许自己按日期推)
    const [autoMonitoring, setAutoMonitoring] = useState<{ active: boolean; code: string; message: string } | null>(null);

    // Platform weights · manual/auto monitoring always uses enhanced search mode.
    const [weightsLoading, setWeightsLoading] = useState(false);
    const [weightsSearching, setWeightsSearching] = useState(false);
    const [platformWeights, setPlatformWeights] = useState<Record<string, number>>({});
    const [platformMau, setPlatformMau] = useState<Record<string, string>>({});
    const [weightsSource, setWeightsSource] = useState('');
    const [weightsUpdatedAt, setWeightsUpdatedAt] = useState('');

    // Real-time progress
    const [showProgressPanel, setShowProgressPanel] = useState(false);
    /**
     * [#182 §1.4] 监测流停滞。
     * 🔴 `reader.read()` 此前**没有客户端超时**:后端某格卡在 running 不再发帧,
     *    cell 就永远 pulse(`ProgressPanel.tsx:134`)—— 屏幕说"还在跑",
     *    真相是"没人再说话了"。这是最难自查的一种"看起来没反应"。
     */
    const [streamStalled, setStreamStalled] = useState(false);
    const [progressLogs, setProgressLogs] = useState<ProgressLog[]>([]);
    const [progressTotal, setProgressTotal] = useState(0);
    const [progressCompleted, setProgressCompleted] = useState(0);
    const [progressCells, setProgressCells] = useState<MonitoringCell[]>([]);
    const [progressTaskId, setProgressTaskId] = useState<number | null>(null);
    const [retryingCellId, setRetryingCellId] = useState<number | null>(null);

    // Form data
    const [newKeyword, setNewKeyword] = useState({ keyword: '', target_brand: '' });

    // 🔴 [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16 ①] 接脏表单守卫。
    //   这就是 Owner 报的那个场景本身:在监测中心填品牌/词条,切出去查资料再切回来,
    //   bfcache 复活触发版本比对 → 静默 hardReload → 一屏输入没了还跳首页。
    //   注册之后,静默那条路会先问这里;有内容就不刷,改弹 banner 让用户自己点。
    //   探针传函数不传布尔:要在"该不该刷"那一刻现场求值,不能冻住注册时的旧值。
    useDirtyForm(
        'monitoring.add-keyword',
        () => Boolean(newKeyword.keyword.trim() || newKeyword.target_brand.trim()),
    );
    useDirtyForm(
        'monitoring.clear-data',
        () => Boolean(clearPassword.trim() || clearReason.trim()),
    );
    useDirtyForm(
        'monitoring.service-config',
        () => Boolean(serviceConfigCustom.trim()),
    );
    const [newPublication, setNewPublication] = useState({
        platform_name: '', platform_url: '', article_title: '',
        publish_date: new Date().toISOString().split('T')[0]
    });

    // SSE abort controller
    const monitorAbortRef = useRef<AbortController | null>(null);
    const cellRetryAbortRef = useRef<AbortController | null>(null);
    const cellRetryRequestRef = useRef<Map<number, { planHash: string; requestId: string }>>(new Map());
    const taskHydrateAbortRef = useRef<AbortController | null>(null);
    const monitoringGenerationRef = useRef(0);
    const memberMonitorRequestRef = useRef<{ fingerprint: string; requestId: string } | null>(null);
    const clientCoreReadAbortRef = useRef<AbortController | null>(null);
    const clientBrandReadAbortRef = useRef<AbortController | null>(null);
    const clientsReadAbortRef = useRef<AbortController | null>(null);
    const accessScopeRef = useRef(accessScopeKey);
    useEffect(() => { return () => {
        monitorAbortRef.current?.abort();
        cellRetryAbortRef.current?.abort();
        clientCoreReadAbortRef.current?.abort();
        clientBrandReadAbortRef.current?.abort();
        clientsReadAbortRef.current?.abort();
        taskHydrateAbortRef.current?.abort();
    }; }, []);

    // [客户反馈⑤ 机制B 2026-08-09] 清空判据从 accessScopeKey(含 activeAccessMode)
    //   改成"**看的是不是同一个客户**"。
    //   旧判据里 activeAccessMode 会在 clientContext 短暂为空时从 live 翻成 pending
    //   再翻回来 —— 而任何一次**成功的**写操作(编辑品牌资料)都会触发 ClientContext
    //   force 重载 → 于是整页 state 被清空又重填,用户看到关键词表"短暂清空又恢复"。
    //   真正需要清空的只有"换了客户 / 换了演示案例"这两件事,它们都在下面这个键里。
    //   🔴 accessScopeRef / monitoringGenerationRef 仍**每次 scope 变化都更新** ——
    //     它们是"丢弃过期响应"的判据,与"要不要清空屏幕"是两件事,合在一起才是原 bug。
    const clientIdentityKey = `${activeBrandId ?? 'none'}:${activeDemoSelection?.caseId ?? 'live'}`;
    const clearedIdentityRef = useRef<string | null>(null);
    useLayoutEffect(() => {
        accessScopeRef.current = accessScopeKey;
        monitoringGenerationRef.current += 1;
        clientsReadAbortRef.current?.abort();
        clientCoreReadAbortRef.current?.abort();
        clientBrandReadAbortRef.current?.abort();
        taskHydrateAbortRef.current?.abort();
        if (clearedIdentityRef.current !== clientIdentityKey) {
            clearedIdentityRef.current = clientIdentityKey;
            setClients([]);
            setSelectedClient('');
            setKeywords([]);
            setSuperRedOceanKws([]);
            setPublications([]);
            setTokenInfo(null);
            setTrendData([]);
            setOperationLogs([]);
            setMonitoringResults([]);
            setRollbackTasks([]);
            setArchives([]);
            setSnapshotMissing(false);
        }
        setClientsLoading(activeAccessMode !== 'pending');
    }, [accessScopeKey, activeAccessMode, clientIdentityKey]);

    const hydrateMonitoringTask = async (
        taskId: number,
        show = true,
        expectedGeneration = monitoringGenerationRef.current,
        expectedBrandId = activeBrandId,
    ) => {
        if (expectedGeneration !== monitoringGenerationRef.current) return;
        taskHydrateAbortRef.current?.abort();
        const controller = new AbortController();
        taskHydrateAbortRef.current = controller;
        const response = await authFetch(`/api/monitoring/tasks/${taskId}`, {
            signal: controller.signal,
        });
        const data = await response.json().catch(() => ({}));
        if (
            controller.signal.aborted
            || expectedGeneration !== monitoringGenerationRef.current
            || !response.ok
            || data.status !== 'success'
            || Number(data.task?.brand_id || 0) !== Number(expectedBrandId || 0)
        ) return;
        const cells = (data.cells || []) as MonitoringCell[];
        setProgressTaskId(taskId);
        setProgressCells(cells);
        setProgressTotal(Number(data.task?.total_tests || cells.filter(cell => cell.is_planned).length));
        setProgressCompleted(Number(data.task?.completed_tests || 0));
        setProgressLogs((data.results || []).map((result: any) => ({
            time: result.tested_at
                ? new Date(result.tested_at).toLocaleTimeString('zh-CN')
                : '',
            keyword: result.keyword || '监测结果',
            platform: result.platform || 'unknown',
            status: result.identity_review_state === 'pending' ? 'error' : 'success',
            error: result.identity_review_state === 'pending' ? '品牌身份待确认' : '',
            errorCode: result.identity_review_state === 'pending' ? 'brand_identity_unresolved' : '',
            detected: Boolean(result.is_detected),
            // [M-1 ①] 人工确认过的行回写"已确认 · 已计入"(confirmed/rejected 都是终态)
            identityConfirmed: ['confirmed', 'rejected'].includes(result.identity_review_state),
            snippet: result.response_snippet || '',
            fullResponse: result.full_response || '',
        })));
        if (show) setShowProgressPanel(true);
    };

    useEffect(() => {
        const generation = ++monitoringGenerationRef.current;
        monitorAbortRef.current?.abort();
        cellRetryAbortRef.current?.abort();
        taskHydrateAbortRef.current?.abort();
        cellRetryRequestRef.current.clear();
        setProgressTaskId(null);
        setProgressCells([]);
        setProgressLogs([]);
        setProgressTotal(0);
        setProgressCompleted(0);
        setRetryingCellId(null);
        setShowProgressPanel(false);
        if (!activeBrandId || activeAccessMode !== 'live') return;
        const stored = window.localStorage.getItem(`omnirank_monitoring_active_task_${activeBrandId}`);
        const taskId = stored ? Number(stored) : 0;
        if (Number.isInteger(taskId) && taskId > 0) {
            void hydrateMonitoringTask(taskId, true, generation, activeBrandId)
                .catch(() => undefined);
        }
    }, [activeAccessMode, activeBrandId]);

    useLayoutEffect(() => {
        clientCoreReadAbortRef.current?.abort();
        clientBrandReadAbortRef.current?.abort();
        setKeywords([]);
        setCurrentServiceDays(null);
        setCurrentServiceStart(null);
        setCurrentServiceEnd(null);
        setCurrentServiceDaysLeft(null);
        setClientMonitoringConfig(null);
        setClientMonitoringEnabled(false);
    }, [selectedClient]);

    useLayoutEffect(() => {
        clientBrandReadAbortRef.current?.abort();
        setTrendData([]);
        setTrendError(null);
        setLatestUnsyncedTaskId(null);
    }, [selectedClient, activeBrandId]);

    // Global client sync: globalQuoteIds 变化时同步选中
    useEffect(() => {
        if (activeAccessMode === 'pending') {
            setSelectedClient('');
            return;
        }
        if (activeBrandId) return;
        if (globalQuoteIds.length > 0) {
            const quoteId = globalQuoteIds[0].toString();
            if (quoteId !== selectedClient) setSelectedClient(quoteId);
        }
    }, [activeAccessMode, globalQuoteIds, activeBrandId, selectedClient]);

    // activeBrandId(URL ?brand_id 优先)或 clients 变化时，同步 selectedClient
    useEffect(() => {
        if (activeAccessMode === 'pending') {
            setSelectedClient('');
            return;
        }
        if (clients.length === 0) {
            setSelectedClient('');
            return;
        }
        if (activeBrandId) {
            const match = clients.find(c => c.brand_id === activeBrandId);
            if (match) {
                setSelectedClient(match.quote_id.toString());
            } else {
                // 当前 URL/上下文指定的品牌没有监测报价，不能 fallback 到其他客户。
                setSelectedClient('');
            }
            return;
        }
        // 没有选中品牌时选第一个
        if (!selectedClient) {
            setSelectedClient(clients[0].quote_id.toString());
        }
    }, [activeAccessMode, clients, activeBrandId, selectedClient]);

    useEffect(() => {
        if (activeAccessMode === 'pending') return;
        const controller = new AbortController();
        clientsReadAbortRef.current = controller;
        void fetchClients(controller.signal, accessScopeKey);
        void fetchScheduleStatus(controller.signal, accessScopeKey);
        return () => controller.abort();
    }, [activeAccessMode, accessScopeKey]);

    useEffect(() => {
        if (activeAccessMode === 'pending') return;
        if (selectedClient) {
            const quoteId = parseInt(selectedClient);
            const controller = new AbortController();
            clientCoreReadAbortRef.current = controller;
            void fetchKeywords(quoteId, controller.signal);
            void fetchClientMonitoringConfig(quoteId, controller.signal);
            return () => controller.abort();
        } else {
            setKeywords([]);
            setCurrentServiceDays(null);
            setCurrentServiceStart(null);
            setCurrentServiceEnd(null);
            setCurrentServiceDaysLeft(null);
            setLatestUnsyncedTaskId(null);
            setClientMonitoringConfig(null);
            setClientMonitoringEnabled(false);
        }
    }, [activeAccessMode, selectedClient]);

    useEffect(() => {
        if (activeAccessMode === 'pending' || !selectedClient || !activeBrandId) return;
        const controller = new AbortController();
        clientBrandReadAbortRef.current = controller;
        const quoteId = parseInt(selectedClient);
        void fetchTrendData(quoteId, activeBrandId, controller.signal);
        void fetchArchives(activeBrandId, controller.signal);
        void fetchLatestUnsyncedTask(activeBrandId, controller.signal);
        return () => controller.abort();
    }, [activeAccessMode, selectedClient, activeBrandId]);

    // 沙盒 step 4: 用户点过 sidebar 导航并进入监测页后，词加载好再推进到第一个 spotlight。
    // step4-sidebar 表示移动端还需要先打开菜单并点击“效果监测”，不能仅因当前 URL
    // 已经是 /monitoring 就提前吞掉这个交互步骤。
    // 2026-05-24 BUGFIX: 推进条件鲁棒化 · 防 stage 卡在 step3 末段就来到 /monitoring 不推进
    // 用户从 /publish 直接 URL 跳来 / 刷新 / 返回 等场景 stage 可能停留在 step3-* 但其实已经过了
    // 任何 step3-* 后期 + step4-page + keywords 已加载 → 都推到 step4-intro
    useEffect(() => {
        if (!sandboxActive) return;
        const stageOkToBoot = (
            tutorialStage === 'step4-page' ||
            // 兜底:step3 末段还没切到 step4 但用户已经进监测页 + 有词 → 视作已就绪
            tutorialStage === 'step3-pub-batch-send' ||
            /* [#191] 原先这里还兜 step3-pub-await-process/edit/confirm 三个阶段。
               它们自 88134ebd7(2026-07-27)起不可达,已从阶段机退役
               ⇒ 兜底也一起去掉,否则是三条永不成立的条件冒充覆盖面。 */
            tutorialStage === 'step3-go-publish' ||
            tutorialStage === 'step3-show-articles'
        );
        if (stageOkToBoot && keywords.length > 0) {
            setTutorialStage('step4-intro');
        }
    }, [sandboxActive, tutorialStage, keywords.length]);

    // ========== Fetch functions ==========

    const fetchClients = async (signal?: AbortSignal, expectedScope = accessScopeRef.current) => {
        setClientsLoading(true);
        try {
            const res = await authFetch('/api/monitoring/clients', { signal });
            const data = await res.json();
            if (!res.ok || data.status !== 'success') throw new Error(data?.detail || data?.error || `HTTP ${res.status}`);
            if (signal?.aborted || expectedScope !== accessScopeRef.current) return;
            setClients(data.clients || []);
            if (data.snapshot_missing) setSnapshotMissing(true);
            setClientsError(null);
        } catch (e) {
            if (signal?.aborted) return;
            console.error('获取客户列表失败', e);
            setClientsError('客户列表更新失败；若有上次成功数据将继续保留');
        } finally {
            if (!signal?.aborted && expectedScope === accessScopeRef.current) setClientsLoading(false);
        }
    };

    const fetchKeywords = async (quoteId: number, signal?: AbortSignal) => {
        setLoading(true);
        try {
            const res = await authFetch(`/api/monitoring/clients/${quoteId}/keywords`, { signal });
            const data = await res.json();
            if (!signal?.aborted && data.status === 'success') {
                setKeywords(data.keywords);
                if (data.snapshot_missing) setSnapshotMissing(true);
                setClientAppearanceRate((data?.client_appearance_rate ?? null) as number | null);  // [2026-06-30] backend authority (same source as trend latest)
                setSuperRedOceanKws(data.super_red_ocean_keywords || []);  // [§4.2 闭环] 超红海单独区·不混普通监测
                if (data.service_days !== undefined) setCurrentServiceDays(data.service_days);
                if (data.service_start_date !== undefined) setCurrentServiceStart(data.service_start_date);
                // 服务期唯一 SSOT：contract_end_date = quotes.service_end_date
                if (data.contract_end_date !== undefined) setCurrentServiceEnd(data.contract_end_date);
                if (data.service_remaining_days_natural_signed !== undefined) setCurrentServiceDaysLeft(data.service_remaining_days_natural_signed);
                // [客户反馈⑥] 后端取不到(老后端 / 查库失败)时保持 null → 横幅退回中性陈述
                setAutoMonitoring(data.auto_monitoring ?? null);
            }
        } catch (e) { if (!signal?.aborted) console.error('获取词条失败', e); }
        finally { if (!signal?.aborted) setLoading(false); }
    };

    const fetchPublications = async (quoteId: number, expectedScope = accessScopeRef.current) => {
        try {
            const res = await authFetch(`/api/publications/${quoteId}`);
            const data = await res.json();
            if (expectedScope !== accessScopeRef.current) return;
            if (data.snapshot_missing) setSnapshotMissing(true);
            if (data.status === 'success') setPublications(data.publications);
        } catch (e) { console.error('获取投放列表失败', e); }
    };

    const fetchToken = async (quoteId: number, expectedScope = accessScopeRef.current) => {
        try {
            const res = await authFetch(`/api/portal/tokens/${quoteId}`);
            const data = await res.json();
            if (expectedScope !== accessScopeRef.current) return;
            if (data.snapshot_missing) setSnapshotMissing(true);
            setTokenInfo(data.status === 'success' && data.token ? data.token : null);
        } catch (e) { console.error('获取Token失败', e); }
    };

    const fetchTrendData = async (quoteId?: number, brandId?: number | null, signal?: AbortSignal) => {
        const currentQuoteId = quoteId ?? (selectedClient ? parseInt(selectedClient) : null);
        const currentBrandId = brandId ?? activeBrandId;
        if (!currentQuoteId || !currentBrandId) return;
        try {
            // [Deploy-CTO 2026-05-30 老板拍板] 趋势对齐当前客户:按 client_id(当前 quote)查 · 不按 brand 聚合
            // 旧 brand_id 会把同品牌其他 campaign(0 词/陈旧 quote)混进来稀释当前客户出现率
            const res = await authFetch(`/api/monitoring/trend?client_id=${currentQuoteId}&days=7`, { signal });
            const data = await res.json();
            if (signal?.aborted) return;
            if (data.snapshot_missing) setSnapshotMissing(true);
            if (data.status === "success" && data.trend) {
                setTrendData(data.trend.map((item: { date: string; rate: number | null }) => ({ date: item.date, rate: item.rate })));
                setTrendError(null);
            } else { setTrendError('趋势响应不完整 · 这不代表趋势为 0'); }
        } catch (e) { if (!signal?.aborted) { console.error("获取趋势数据失败", e); setTrendError('趋势读取失败 · 已保留上次成功数据'); } }
    };

    const fetchOperationLogs = async (expectedScope = accessScopeRef.current) => {
        // 2026-06-26 · 日志按当前客户 brand_id 二次隔离，避免同一代理名下多客户日志串台。
        try {
            const params = new URLSearchParams({ limit: '50' });
            if (activeBrandId) params.set('brand_id', String(activeBrandId));
            const res = await authFetch(`/api/logs?${params.toString()}`);
            const data = await res.json();
            if (expectedScope !== accessScopeRef.current) return;
            if (data.snapshot_missing) setSnapshotMissing(true);
            if (data.status === 'success') setOperationLogs(data.logs || []);
        } catch (e) { console.error('获取操作日志失败', e); }
    };

    const fetchRollbackTasks = async (expectedScope = accessScopeRef.current) => {
        if (!activeBrandId) return;
        try {
            const res = await authFetch(`/api/monitoring/rollback/tasks?brand_id=${activeBrandId}&limit=20`);
            const data = await res.json();
            if (expectedScope !== accessScopeRef.current) return;
            if (data.snapshot_missing) setSnapshotMissing(true);
            if (data.status === 'success') setRollbackTasks(data.tasks || []);
        } catch (e) { console.error('获取回退任务失败', e); }
    };

    const fetchLatestUnsyncedTask = async (brandId?: number | null, signal?: AbortSignal) => {
        const currentBrandId = brandId ?? activeBrandId;
        if (!currentBrandId) return;
        try {
            const res = await authFetch(`/api/monitoring/rollback/tasks?brand_id=${currentBrandId}&limit=5`, { signal });
            const data = await res.json();
            if (!signal?.aborted && data.status === 'success' && data.tasks) {
                if (data.snapshot_missing) setSnapshotMissing(true);
                const unsynced = data.tasks.find((t: any) => t.status === 'completed' && !t.trend_synced && t.trigger_type !== 'scheduled');
                setLatestUnsyncedTaskId(unsynced ? unsynced.id : null);
            }
        } catch (e) { if (!signal?.aborted) console.error('获取未同步任务失败', e); }
    };

    const fetchArchives = async (brandId?: number | null, signal?: AbortSignal) => {
        const currentBrandId = brandId ?? activeBrandId;
        if (!currentBrandId) return;
        try {
            const res = await authFetch(`/api/monitoring/archives?brand_id=${currentBrandId}`, { signal });
            const data = await res.json();
            if (!signal?.aborted) {
                if (data.snapshot_missing) setSnapshotMissing(true);
                setArchives(data.archives || []);
            }
        } catch (e) { if (!signal?.aborted) console.error('获取归档列表失败', e); }
    };

    const fetchScheduleStatus = async (signal?: AbortSignal, expectedScope = accessScopeRef.current) => {
        try {
            const res = await authFetch('/api/monitoring/schedule', { signal });
            const data = await res.json();
            if (!signal?.aborted && expectedScope === accessScopeRef.current && data.status === 'success') {
                if (data.snapshot_missing) setSnapshotMissing(true);
                setScheduleStatus(data);
                setScheduleEnabled(!!data.monitoring_enabled);
                // 从 daily_monitoring job 的 trigger 中提取时间
                const monJob = data.jobs?.find((j: any) => j.id === 'daily_monitoring');
                if (monJob) {
                    const trigger = monJob.trigger || '';
                    const hourMatch = trigger.match(/hour='(\d+)'/);
                    const minMatch = trigger.match(/minute='(\d+)'/);
                    if (hourMatch) setScheduleHour(parseInt(hourMatch[1]));
                    if (minMatch) setScheduleMinute(parseInt(minMatch[1]));
                }
            }
        } catch (e) { if (!signal?.aborted) console.error('获取定时状态失败', e); }
    };

    const fetchClientMonitoringConfig = async (quoteId?: number, signal?: AbortSignal) => {
        const qid = quoteId ?? (selectedClient ? parseInt(selectedClient) : null);
        if (!qid) {
            setClientMonitoringConfig(null);
            setClientMonitoringEnabled(false);
            setMonitoringIntervalHours(24);
            setMonitoringStartHour(8);
            return;
        }
        try {
            const res = await authFetch(`/api/monitoring/client/${qid}/monitoring-config`, { signal });
            const data = await res.json();
            if (!signal?.aborted && data.status === 'success') {
                if (data.snapshot_missing) setSnapshotMissing(true);
                setClientMonitoringConfig(data);
                setClientMonitoringEnabled(!!data.monitoring_enabled);
                setMonitoringIntervalHours(Number(data.monitoring_interval_hours || 24));
                setMonitoringStartHour(Number(data.monitoring_start_hour ?? 8));
            }
        } catch (e) { if (!signal?.aborted) console.error('获取客户定时监测配置失败', e); }
    };

    const fetchLatestResults = async (expectedScope = accessScopeRef.current) => {
        if (!selectedClient || !activeBrandId) return;
        try {
            const res = await authFetch(`/api/monitoring/tasks?brand_id=${activeBrandId}&limit=1`);
            const data = await res.json();
            if (expectedScope !== accessScopeRef.current) return;
            if (data.snapshot_missing) setSnapshotMissing(true);
            if (data.status === 'success' && data.tasks?.length > 0) {
                const detailRes = await authFetch(`/api/monitoring/tasks/${data.tasks[0].id}`);
                const detailData = await detailRes.json();
                if (expectedScope !== accessScopeRef.current) return;
                if (detailData.snapshot_missing) setSnapshotMissing(true);
                if (detailData.status === 'success') { setMonitoringResults(detailData.results || []); setShowResultsDialog(true); }
            } else { lazyToast.message(data.snapshot_missing ? '快照未包含' : '暂无检测记录'); }
        } catch (e) { console.error('获取结果失败', e); }
    };

    // ========== Action handlers ==========

    const handleAddKeyword = async () => {
        // CTO-15.23 2026-05-25 · 老板报"添加关键词没有用" 根因:
        //   brand 已创建但 quotes 表无对应行(套餐已选但 quote 未生成)→ clients.find 无 match
        //   → setSelectedClient('') → 原代码 if(!selectedClient) return silent fail · 用户感知"无效"
        // 修法:
        //   1. keyword 空 → toast 提示(不再 silent)
        //   2. selectedClient 空但 activeBrandId 有 → 仍允许提交(后端 add_keyword 支持 brand_id-only · monitoring_db.py:657 _resolve_id)
        //   3. 都空 → toast 提示需选客户
        if (!newKeyword.keyword) {
            lazyToast.error('请输入关键词');
            return;
        }
        if (!selectedClient && !activeBrandId) {
            lazyToast.error('请先选择客户');
            return;
        }
        try {
            const payload: Record<string, unknown> = {
                brand_id: activeBrandId,
                keyword: newKeyword.keyword,
                target_brand: newKeyword.target_brand,
            };
            if (selectedClient) payload.quote_id = parseInt(selectedClient);
            const res = await authFetch('/api/monitoring/keywords', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            // [Deploy-CTO 2026-05-26 根治 B] 后端返 resolved_quote_id · 自动跳到 keyword 真实所在的 quote
            // 老板真根因:同 brand 多 quote 时 add 写到 A · selectedClient 是 B · 看不到 row
            // 修:拿后端 resolved_quote_id · setSelectedClient + fetchKeywords 跳过去
            const resolvedQuoteId: number | null = (typeof data.quote_id === 'number' && data.quote_id > 0) ? data.quote_id : null;
            const targetQuoteId = resolvedQuoteId || (selectedClient ? parseInt(selectedClient) : null);

            if (data.status === 'success' || data.keyword_id) {
                setShowAddDialog(false);
                // [onboarding] 新手教程 step4 完成钩子 · 加监测算完成 first_monitoring
                markStep('first_monitoring');
                setNewKeyword({ keyword: '', target_brand: '' });
                if (targetQuoteId) {
                    // 跳到 resolved quote(可能 ≠ 原 selectedClient)
                    if (resolvedQuoteId && String(resolvedQuoteId) !== selectedClient) {
                        setSelectedClient(String(resolvedQuoteId));
                    }
                    fetchKeywords(targetQuoteId);
                    lazyToast.success('关键词已添加');
                } else {
                    lazyToast.success('关键词已添加 · 请刷新页面查看');
                }
            } else if (data.error) {
                // CTO-15.23 2026-05-25 · 后端 error 已是中文友好 msg(如 duplicate_keyword)· 不重复加"添加失败:" 前缀
                lazyToast.error(data.error);
                // [Deploy-CTO 2026-05-26 根治 B+] duplicate 也跳到 resolved quote · 让用户看到已存在 row
                if (targetQuoteId) {
                    if (resolvedQuoteId && String(resolvedQuoteId) !== selectedClient) {
                        setSelectedClient(String(resolvedQuoteId));
                    }
                    fetchKeywords(targetQuoteId);
                }
                if (data.code === 'duplicate_keyword') {
                    setShowAddDialog(false);
                    setNewKeyword({ keyword: '', target_brand: '' });
                }
            } else {
                lazyToast.error('添加失败 · 返回数据异常');
            }
        } catch (e) {
            console.error('添加词条失败', e);
            lazyToast.error('添加词条失败: ' + (e instanceof Error ? e.message : String(e)));
        }
    };

    const handleRunMonitoring = async () => {
        if (!selectedClient || selectedKeywords.size === 0) { lazyToast.message('请先选择要监测的词条'); return; }
        if (monitorSingleCost == null) {
            lazyToast.error(pricingError || '动态价目尚未确认，本次没有启动监测');
            return;
        }
        // v1_3 (CTO-15.1 2026-04-19): 按钮已标价 + 大额 toast 5s 反悔
        // 替代 window.confirm 强阻断（老板元指令：按钮级确认，不弹窗）
        // admin 因高频调试直接执行（totalCost=0 走 confirmLarge 也是立即执行）
        if (!isAdmin) {
            const kwCount = selectedKeywords.size;
            const totalCost = monitorSingleCost * kwCount;
            // [2026-06-03 全站静默扣费] confirmLarge 仍保留(>1000 额度大额误触保护)· label 去金额改纯动作
            confirmLarge(
                totalCost,
                () => { void doRunMonitoring(); },
                `将开始监测 ${kwCount} 个关键词`,
            );
            return;
        }
        void doRunMonitoring();
    };

    const doRunMonitoring = async () => {
        if (monitorSingleCost == null) {
            lazyToast.error(pricingError || '动态价目尚未确认，本次没有启动监测');
            return;
        }
        monitorAbortRef.current?.abort();
        const abortController = new AbortController();
        monitorAbortRef.current = abortController;
        const operationGeneration = monitoringGenerationRef.current;
        const operationBrandId = activeBrandId;
        // [P5b] 长任务启动:先占用额度提示(做成才正式扣,失败自动退回)· 管理员免扣不提示
        if (!isAdmin) {
            notifyFreezeHold(monitorSingleCost * selectedKeywords.size);
        }
        setShowProgressPanel(true); setProgressLogs([]); setProgressCells([]); setProgressTaskId(null); setProgressCompleted(0); setLoading(true); setStreamStalled(false);
        let activeTaskId: number | null = null;
        try {
            if (isMember) {
                const fingerprint = JSON.stringify({
                    brandId: activeBrandId,
                    quoteId: parseInt(selectedClient),
                    keywordKeys: Array.from(selectedKeywords).sort(),
                });
                if (memberMonitorRequestRef.current?.fingerprint !== fingerprint) {
                    memberMonitorRequestRef.current = {
                        fingerprint,
                        requestId: `member-monitor-${safeRandomUUID()}`,
                    };
                }
                const response = await authFetch('/api/monitoring/run', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Request-ID': memberMonitorRequestRef.current.requestId,
                    },
                    body: JSON.stringify({
                        brand_id: activeBrandId,
                        quote_id: parseInt(selectedClient),
                        keyword_keys: Array.from(selectedKeywords),
                        search_mode: 'enhanced',
                    }),
                    signal: abortController.signal,
                });
                const data = await response.json().catch(() => ({}));
                if (operationGeneration !== monitoringGenerationRef.current) return;
                if (!response.ok || data.status === 'error') {
                    const detail = data?.detail?.message || data?.detail || data?.error || '监测失败';
                    throw new Error(typeof detail === 'string' ? detail : '监测失败');
                }
                setProgressTotal(selectedKeywords.size);
                setProgressCompleted(selectedKeywords.size);
                setProgressLogs([{
                    time: new Date().toLocaleTimeString('zh-CN'),
                    keyword: '本次监测', platform: '全部平台', status: 'success', error: '',
                    errorCode: '', detected: true, snippet: '监测完成，结果已按员工产物隔离保存', fullResponse: '',
                }]);
                setSelectedKeywords(new Set());
                memberMonitorRequestRef.current = null;
                fetchKeywords(parseInt(selectedClient));
                if (data.task_id) {
                    setLatestUnsyncedTaskId(data.task_id);
                    setProgressTaskId(data.task_id);
                    window.localStorage.setItem(`omnirank_monitoring_active_task_${operationBrandId}`, String(data.task_id));
                    await hydrateMonitoringTask(data.task_id, true, operationGeneration, operationBrandId);
                }
                return;
            }
            const response = await authFetch('/api/monitoring/run-stream', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ brand_id: activeBrandId, quote_id: parseInt(selectedClient), keyword_keys: Array.from(selectedKeywords), search_mode: 'enhanced' }),
                signal: abortController.signal,
            });
            if (!response.body) throw new Error('ReadableStream not supported');
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (true) {
                /**
                 * 🔴 [#182 §1.4] 每一次读都配一个 120 秒的闸。超时不是"出错",
                 *    是**没有人再说话了** —— 照实说成「等待超时」并给重试,
                 *    而不是让格子无限 pulse 下去。
                 */
                let stalled = false;
                let stallTimer: number | undefined;
                const raced = await Promise.race([
                    reader.read(),
                    new Promise<{ done: boolean; value?: Uint8Array }>((resolve) => {
                        stallTimer = window.setTimeout(() => {
                            stalled = true;
                            resolve({ done: true });
                        }, STREAM_STALL_MS);
                    }),
                ]);
                if (stallTimer !== undefined) window.clearTimeout(stallTimer);
                const { done, value } = raced;
                if (stalled) {
                    setStreamStalled(true);
                    void reader.cancel().catch(() => undefined);
                    break;
                }
                if (
                    done
                    || abortController.signal.aborted
                    || operationGeneration !== monitoringGenerationRef.current
                ) break;
                setStreamStalled(false);   // 又有帧了 ⇒ 不再是停滞
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        try {
                            const data = JSON.parse(line.slice(6));
                            if (data.type === 'start') {
                                setProgressTotal(data.total);
                                activeTaskId = Number(data.task_id) || null;
                                setProgressTaskId(activeTaskId);
                                setProgressCells((data.cells || []) as MonitoringCell[]);
                                if (activeTaskId) {
                                    window.localStorage.setItem(`omnirank_monitoring_active_task_${operationBrandId}`, String(activeTaskId));
                                }
                                setLoading(false);  // [2026-06-07 P0-2a] 收到 start 立刻关 loading → ProgressPanel 显「已连接·等待引擎」· 不再卡「正在连接监测服务」7-25s
                            }
                            else if (data.type === 'cell' && data.cell_id) {
                                setProgressCells(prev => prev.map(cell => cell.id === data.cell_id ? {
                                    ...cell,
                                    state: data.state || cell.state,
                                } : cell));
                            }
                            else if (data.type === 'detail') {
                                setProgressCompleted(data.completed);
                                if (data.cell_id) {
                                    setProgressCells(prev => prev.map(cell => cell.id === data.cell_id ? {
                                        ...cell,
                                        state: data.cell_state || cell.state,
                                        error_code: data.error_code || null,
                                        error_message: data.error || null,
                                    } : cell));
                                }
                                setProgressLogs(prev => [...prev, {
                                    time: new Date().toLocaleTimeString('zh-CN'),
                                    keyword: data.keyword,
                                    platform: data.platform,
                                    status: data.status || 'success',
                                    error: data.error || '',
                                    errorCode: data.error_code || '',
                                    detected: data.detected,
                                    snippet: data.snippet,
                                    fullResponse: data.full_response || '',
                                    cellId: data.cell_id,
                                    cellState: data.cell_state,
                                    planHash: data.plan_hash,
                                }]);
                            } else if (data.type === 'complete') {
                                setSelectedKeywords(new Set()); fetchKeywords(parseInt(selectedClient));
                                if (data.cells) setProgressCells(data.cells as MonitoringCell[]);
                                if (data.task_id) setLatestUnsyncedTaskId(data.task_id);
                            } else if (data.type === 'error') { lazyToast.error(data.error || '监测失败'); }
                        } catch (e) { console.warn('Parse SSE data error:', e); }
                    }
                }
            }
            if (activeTaskId) {
                await hydrateMonitoringTask(activeTaskId, true, operationGeneration, operationBrandId);
            }
        } catch (e: any) {
            if (
                e?.name === 'AbortError'
                || operationGeneration !== monitoringGenerationRef.current
            ) return;
            if (activeTaskId) {
                await hydrateMonitoringTask(activeTaskId, true, operationGeneration, operationBrandId)
                    .catch(() => undefined);
            }
            console.error('监测失败', e);
            lazyToast.error('监测请求失败，请检查网络后重试');
            setProgressLogs(prev => [...prev, { time: new Date().toLocaleTimeString('zh-CN'), keyword: '系统', platform: 'error', status: 'error', error: '监测连接中断', errorCode: 'network_interrupted', detected: false, snippet: '监测连接中断', fullResponse: '' }]);
        } finally { setLoading(false); }
    };

    const handleRetryMonitoringCell = async (cell: MonitoringCell) => {
        if (!progressTaskId || !activeBrandId || retryingCellId) return;
        const retryTaskId = progressTaskId;
        const retryBrandId = activeBrandId;
        const retryGeneration = monitoringGenerationRef.current;
        cellRetryAbortRef.current?.abort();
        const retryController = new AbortController();
        cellRetryAbortRef.current = retryController;
        const retryStorageKey = `omnirank_monitoring_cell_retry_${retryTaskId}_${cell.id}`;
        let retryRequest = cellRetryRequestRef.current.get(cell.id);
        if (!retryRequest) {
            try {
                const stored = JSON.parse(window.localStorage.getItem(retryStorageKey) || 'null');
                if (stored?.planHash === cell.plan_hash && typeof stored?.requestId === 'string') {
                    const restoredRequest = { planHash: cell.plan_hash, requestId: stored.requestId };
                    retryRequest = restoredRequest;
                    cellRetryRequestRef.current.set(cell.id, restoredRequest);
                }
            } catch { /* ignore corrupt retry cache */ }
        }
        if (!retryRequest || retryRequest.planHash !== cell.plan_hash) {
            retryRequest = { planHash: cell.plan_hash, requestId: safeRandomUUID() };
            cellRetryRequestRef.current.set(cell.id, retryRequest);
            window.localStorage.setItem(retryStorageKey, JSON.stringify(retryRequest));
        }
        setRetryingCellId(cell.id);
        setProgressCells(prev => prev.map(item => item.id === cell.id ? { ...item, state: 'running' } : item));
        try {
            const response = await authFetch(`/api/monitoring/tasks/${retryTaskId}/cells/${cell.id}/retry`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    brand_id: retryBrandId,
                    request_id: retryRequest.requestId,
                    expected_plan_hash: cell.plan_hash,
                }),
                signal: retryController.signal,
            });
            const data = await response.json().catch(() => ({}));
            if (retryGeneration !== monitoringGenerationRef.current) return;
            if (!response.ok) {
                const outcomeMayBeUnknown = response.status >= 500
                    || [408, 425, 429].includes(response.status);
                if (!outcomeMayBeUnknown) {
                    cellRetryRequestRef.current.delete(cell.id);
                    window.localStorage.removeItem(retryStorageKey);
                }
                const detail = data?.detail?.message || data?.detail || '单格重试未启动';
                throw new Error(typeof detail === 'string' ? detail : '单格重试未启动');
            }
            if (data.cell) {
                setProgressCells(prev => prev.map(item => item.id === cell.id ? data.cell : item));
            }
            await hydrateMonitoringTask(retryTaskId, true, retryGeneration, retryBrandId);
            if (data.status === 'success' || (
                data.status === 'replayed'
                && ['succeeded', 'pending_identity'].includes(data?.data?.state)
            )) {
                cellRetryRequestRef.current.delete(cell.id);
                window.localStorage.removeItem(retryStorageKey);
                lazyToast.success('该单格已完成，不会重跑其他成功结果');
            } else if (data.status === 'in_progress') {
                lazyToast.message('同一单格已在执行，请稍候');
            } else {
                cellRetryRequestRef.current.delete(cell.id);
                window.localStorage.removeItem(retryStorageKey);
                lazyToast.error(data?.data?.error || '该单格仍未返回可用结果');
            }
        } catch (error) {
            if (
                (error instanceof DOMException && error.name === 'AbortError')
                || retryGeneration !== monitoringGenerationRef.current
            ) return;
            lazyToast.error(error instanceof Error ? error.message : '单格重试连接中断');
            await hydrateMonitoringTask(retryTaskId, true, retryGeneration, retryBrandId)
                .catch(() => undefined);
        } finally {
            if (cellRetryAbortRef.current === retryController) {
                cellRetryAbortRef.current = null;
            }
            if (retryGeneration === monitoringGenerationRef.current) {
                setRetryingCellId(null);
            }
        }
    };

    const handleAddPublication = async () => {
        if (!newPublication.platform_name || !selectedClient) { lazyToast.error('请填写投放平台'); return; }
        try {
            const res = await authFetch('/api/publications', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ quote_id: parseInt(selectedClient), ...newPublication, operator_id: 'admin' })
            });
            const data = await res.json();
            if (data.status === 'success') {
                lazyToast.success('投放记录已添加');
                setNewPublication({ platform_name: '', platform_url: '', article_title: '', publish_date: new Date().toISOString().split('T')[0] });
                fetchPublications(parseInt(selectedClient));
            } else {
                lazyToast.error(data.error || '添加失败');
            }
        } catch (e) { console.error('添加投放失败', e); lazyToast.error('添加失败'); }
    };

    const handleGenerateToken = async () => {
        if (!selectedClient) return;
        try {
            const clientInfo = clients.find(c => c.quote_id.toString() === selectedClient);
            const res = await authFetch('/api/portal/tokens', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ quote_id: parseInt(selectedClient), brand_name: clientInfo?.brand_name, days_valid: parseInt(import.meta.env.VITE_TOKEN_DAYS_VALID || '30') })
            });
            const data = await res.json();
            if (res.ok && data.status === 'success' && data.data) {
                setTokenInfo(data.data);
                setCopied(false);
                lazyToast.success(tokenInfo ? '客户门户链接已重新生成' : '客户门户链接已生成');
                return;
            }
            const detail = data?.detail?.message || data?.detail || data?.error || '客户门户链接生成失败';
            lazyToast.error(typeof detail === 'string' ? detail : '客户门户链接生成失败');
        } catch (e) {
            console.error('生成Token失败', e);
            lazyToast.error('客户门户链接生成失败，请稍后重试');
        }
    };

    const copyToken = async () => {
        if (tokenInfo?.token) {
            const ok = await copyToClipboard(tokenInfo.token);
            if (ok) { setCopied(true); setTimeout(() => setCopied(false), 2000); }
            else { lazyToast.error('复制失败，请手动复制'); }
        }
    };

    const handleGenerateReport = async () => {
        if (!activeBrandId) return;
        setReportGenLoading(true); setReportGenResult('');
        try {
            const reportPath = isMember
                ? `/api/reports/generate?brand_id=${activeBrandId}&report_type=${encodeURIComponent(reportGenType)}`
                : `/api/reports/generate/${reportGenType}?brand_id=${activeBrandId}`;
            const res = await authFetch(reportPath, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') setReportGenResult(`报告生成成功！报告ID: ${data.report_id || data.id || '-'}`);
            else if (data.status === 'warning') setReportGenResult(data.message || '当前周期无监测数据');
            else setReportGenResult(`生成失败: ${data.error || data.message || '未知错误'}`);
        } catch (e) { console.error('生成报告失败', e); setReportGenResult('生成报告失败，请稍后重试'); }
        finally { setReportGenLoading(false); }
    };

    const handleRollback = async () => {
        if (selectedRollbackIds.size === 0) return;
        setRollbackLoading(true);
        try {
            const res = await authFetch('/api/monitoring/rollback-batch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task_ids: Array.from(selectedRollbackIds) }) });
            const data = await res.json();
            if (data.status === 'success') {
                lazyToast.success(`回退成功：已删除 ${data.deleted_results} 条监测结果`);
                setSelectedRollbackIds(new Set()); fetchRollbackTasks();
                if (selectedClient) fetchKeywords(parseInt(selectedClient));
            } else { lazyToast.error('回退失败: ' + (data.error || '未知错误')); }
        } catch (e) { console.error('回退失败', e); lazyToast.error('回退失败'); }
        finally { setRollbackLoading(false); }
    };

    const handleSyncTrends = async (taskId: number) => {
        setSyncingTaskId(taskId);
        try {
            const res = await authFetch(`/api/monitoring/sync-trends/${taskId}`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') {
                lazyToast.success(`同步成功：已同步 ${data.synced_keywords} 个词条的趋势数据`);
                setLatestUnsyncedTaskId(null); fetchRollbackTasks();
                if (selectedClient) fetchKeywords(parseInt(selectedClient));
            } else { lazyToast.error('同步失败: ' + (data.error || '未知错误')); }
        } catch (e) { console.error('同步失败', e); lazyToast.error('同步趋势数据失败'); }
        finally { setSyncingTaskId(null); }
    };

    const handleClearData = async () => {
        if (!clearPassword) { lazyToast.message('请输入操作密码'); return; }
        if (!selectedClient || !activeBrandId) { lazyToast.message('请先选择客户'); return; }
        setClearLoading(true);
        try {
            const res = await authFetch('/api/monitoring/clear-data', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: clearPassword, brand_id: activeBrandId, quote_id: parseInt(selectedClient), reason: clearReason || '手动清除' }) });
            const data = await res.json();
            if (data.status === 'success') {
                lazyToast.success(`清除成功！已归档 ${data.deleted_results} 条监测结果、${data.deleted_tasks} 个任务`, { description: `归档编号: ${data.archive_key}，如需恢复点击"数据归档"按钮` });
                setShowClearDialog(false); setClearPassword(''); setClearReason('');
                if (selectedClient) fetchKeywords(parseInt(selectedClient));
            } else { lazyToast.error('清除失败: ' + (data.error || '未知错误')); }
        } catch (e) { console.error('清除失败', e); lazyToast.error('清除请求失败'); }
        finally { setClearLoading(false); }
    };

    const handleRestore = async (archiveKey: string) => {
        if (!(await askConfirm({ title: '确认恢复此归档的监测数据？', confirmLabel: '恢复' }))) return;
        setRestoreLoading(archiveKey);
        try {
            const res = await authFetch('/api/monitoring/restore-data', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ archive_key: archiveKey }) });
            const data = await res.json();
            if (data.status === 'success') {
                lazyToast.success(`恢复成功！已恢复 ${data.restored_tasks} 个任务、${data.restored_results} 条结果`);
                fetchArchives(); if (selectedClient) fetchKeywords(parseInt(selectedClient));
            } else { lazyToast.error('恢复失败: ' + (data.error || '未知错误')); }
        } catch (e) { lazyToast.error('恢复请求失败'); }
        finally { setRestoreLoading(null); }
    };

    const handleSaveSchedule = async () => {
        setScheduleLoading(true);
        try {
            const globalRes = await authFetch('/api/monitoring/schedule', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hour: scheduleHour, minute: scheduleMinute, enabled: scheduleEnabled }) });
            const globalData = await globalRes.json();
            if (!(globalData.status === 'success' || globalData.success === true)) {
                lazyToast.error('设置失败: ' + (globalData.detail || globalData.message || globalData.error || '未知错误'));
                return;
            }

            let clientSaved = true;
            let clientData: any = null;
            if (selectedClient) {
                const safeInterval = Math.max(1, Math.min(Number(monitoringIntervalHours) || 24, 720));
                const safeStartHour = Math.max(0, Math.min(Number(monitoringStartHour) || 8, 23));
                const clientRes = await authFetch(`/api/monitoring/client/${selectedClient}/monitoring-config`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        enabled: clientMonitoringEnabled,
                        interval_hours: safeInterval,
                        start_hour: safeStartHour,
                    }),
                });
                clientData = await clientRes.json();
                clientSaved = clientData.status === 'success' || clientData.success === true;
                if (!clientSaved) {
                    lazyToast.error('客户自动监测保存失败: ' + (clientData.detail || clientData.message || clientData.error || '未知错误'));
                    return;
                }
            }

            setShowScheduleDialog(false);
            const interval = clientData?.interval_hours || monitoringIntervalHours;
            lazyToast.success(scheduleEnabled
                ? `定时监测已保存 · 当前客户每 ${interval} 小时执行一次`
                : '全局定时巡检已关闭',
                scheduleEnabled ? { description: `系统每小时巡检到点客户，不会每小时消耗算力` } : undefined);
            fetchScheduleStatus();
            if (selectedClient) fetchClientMonitoringConfig(parseInt(selectedClient));
        } catch (e) { lazyToast.error('设置请求失败'); }
        finally { setScheduleLoading(false); }
    };

    const handleRunNow = async () => {
        if (!(await askConfirm({ title: '立即执行全部品牌的监测任务？（后台运行，不影响页面操作）' }))) return;
        try {
            const res = await authFetch('/api/monitoring/schedule/run-now', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'started') { lazyToast.success('全品牌监测已在后台启动！完成后会发送通知。'); fetchScheduleStatus(); }
            else lazyToast.error(data.error || '启动失败');
        } catch (e) { lazyToast.error('请求失败'); }
    };

    const handleStopMonitoring = async () => {
        if (!(await askConfirm({ title: '确认停止当前正在执行的监测任务？（当前品牌完成后停止）', danger: true }))) return;
        try {
            const res = await authFetch('/api/monitoring/schedule/stop', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'stopping') { lazyToast.success('正在停止，当前品牌完成后会中断'); fetchScheduleStatus(); }
            else lazyToast.error(data.error || '停止失败');
        } catch (e) { lazyToast.error('请求失败'); }
    };

    const handleSaveServiceConfig = async () => {
        if (!selectedClient) return;
        setServiceConfigLoading(true);
        try {
            const days = serviceConfigCustom ? parseInt(serviceConfigCustom) : serviceConfigDays;
            if (!days || days < 1) { lazyToast.message('服务天数必须大于0'); setServiceConfigLoading(false); return; }
            // [服务期 SSOT 2026-08-06] 本对话框只改【累计达标天数配额】。
            //   合同服务期（起止日）在报价单里改 —— 旧版在这里改起始日却不重算结束日，
            //   是两钟错开的写入侧成因（晨光富士 #286 就是这个形状）。
            const body: any = { service_days: days };
            const res = await authFetch(`/api/monitoring/clients/${selectedClient}/service-config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            if (!res.ok) { const errData = await res.json().catch(() => ({})); lazyToast.error('保存失败: ' + (errData.detail || errData.error || `HTTP ${res.status}`)); return; }
            const data = await res.json();
            if (data.status === 'success') { setCurrentServiceDays(data.service_days); setShowServiceConfigDialog(false); fetchKeywords(parseInt(selectedClient)); }
            else { lazyToast.error('保存失败: ' + (data.error || JSON.stringify(data))); }
        } catch (e: any) { console.error('保存服务期配置失败', e); lazyToast.error('保存失败: ' + (e.message || '网络错误')); }
        finally { setServiceConfigLoading(false); }
    };

    const defaultWeights = { doubao: 0.25, dashscope: 0.25, deepseek: 0.25, yuanbao: 0.25 };

    const handleOpenWeights = async () => {
        setPlatformWeights(defaultWeights); setPlatformMau({}); setWeightsSource('default'); setWeightsUpdatedAt(''); setShowWeightsDialog(true); setWeightsLoading(true);
        try {
            const res = await authFetch('/api/monitoring/platform-weights');
            const data = await res.json();
            if (data.status === 'success' && data.weights && Object.keys(data.weights).length > 0) {
                setPlatformWeights(data.weights); setPlatformMau(data.mau_data || {}); setWeightsSource(data.source || 'default'); setWeightsUpdatedAt(data.updated_at || '');
            }
        } catch (e) { console.error('获取权重失败', e); }
        finally { setWeightsLoading(false); }
    };

    const handleSearchMau = async () => {
        setWeightsSearching(true);
        try {
            const res = await authFetch('/api/monitoring/platform-weights/search', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') {
                setPlatformWeights(data.suggested_weights || {});
                const mauMap: Record<string, string> = {};
                if (data.mau_result) { for (const [k, v] of Object.entries(data.mau_result as Record<string, any>)) { mauMap[k] = `${v.name}: ${v.mau}`; } }
                setPlatformMau(mauMap); setWeightsSource('ai_search');
            } else { lazyToast.error('搜索失败: ' + (data.error || '未知错误')); }
        } catch (e) { console.error('搜索MAU失败', e); lazyToast.error('搜索最新数据失败，请稍后重试'); }
        finally { setWeightsSearching(false); }
    };

    const handleSaveWeights = async () => {
        const total = Object.values(platformWeights).reduce((a, b) => a + b, 0);
        if (Math.abs(total - 1.0) > 0.05) { lazyToast.message(`权重总和为 ${total.toFixed(2)}，应接近 1.0，请调整`); return; }
        setWeightsLoading(true);
        try {
            const res = await authFetch('/api/monitoring/platform-weights', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ weights: platformWeights, mau_data: platformMau }) });
            const data = await res.json();
            if (data.status === 'success') {
                lazyToast.success('平台权重已更新');
                setShowWeightsDialog(false);
                if (selectedClient) {
                    await fetchKeywords(parseInt(selectedClient));
                    fetchTrendData();
                }
            }
            else { lazyToast.error('保存失败: ' + (data.error || '未知错误')); }
        } catch (e) { console.error('保存权重失败', e); lazyToast.error('保存失败'); }
        finally { setWeightsLoading(false); }
    };

    const refreshKeywordState = () => {
        if (selectedClient) fetchKeywords(parseInt(selectedClient));
        fetchClients();
        fetchArchives();
    };

    const handleArchiveKeywords = async (items: Keyword[]) => {
        if (items.length === 0) return;
        const failures: string[] = [];
        for (const kw of items) {
            try {
                const params = new URLSearchParams({
                    reason: 'manual',
                    source: kw.source || 'confirmed',
                });
                const res = await authFetch(`/api/monitoring/keywords/${kw.id}/archive?${params}`, { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (!res.ok || !data.success) {
                    failures.push(`${kw.keyword}: ${data?.detail || data?.error || `HTTP ${res.status}`}`);
                }
            } catch (e: any) {
                failures.push(`${kw.keyword}: ${e.message || '请求失败'}`);
            }
        }
        if (failures.length > 0) {
            lazyToast.error(`有 ${failures.length} 个词条归档失败`, { description: failures.slice(0, 2).join('；') });
        } else {
            lazyToast.success(items.length === 1 ? `已归档「${items[0].keyword}」` : `已归档 ${items.length} 个词条`);
        }
        setSelectedKeywords(new Set());
        refreshKeywordState();
    };

    const handleDeleteKeyword = async (kw: Keyword) => {
        if (!(await askConfirm({ title: '确定删除该词条？', danger: true }))) return;
        try {
            await authFetch(`/api/monitoring/keywords/${kw.id}?source=extra`, { method: 'DELETE' });
            fetchKeywords(parseInt(selectedClient));
        } catch (e) { console.error('删除失败', e); }
    };

    // CTO-15.20 v2 · D.1:测试客户隔离 · 默认隐藏 is_test=true 的 brand_id
    const visibleClients = useMemo(() => {
        if (includeTest) return clients;
        return clients.filter(c => !isTest(c.brand_id));
    }, [clients, includeTest, isTest]);
    const selectedClientInfo = visibleClients.find(c => c?.quote_id?.toString() === selectedClient)
        || (isAdmin ? clients.find(c => c?.quote_id?.toString() === selectedClient) : undefined);

    // [WJ-23/32 2026-06-01] 监测首屏"看效果"概览 · 用已有 keywords 算 · 不造假数据
    // 口径对齐 KeywordTable(L730-734):只统计真正在监测中的词(lifecycle==='monitoring')·
    //   出现率主口径 effective_rate ?? detection_rate(达标前瞬时 / 达标后历史平滑)· 达标 = is_compliant===true
    const effectOverview = useMemo(() => {
        const monitoringKws = keywords.filter(k => k.lifecycle === 'monitoring');
        const total = monitoringKws.length;
        const compliant = monitoringKws.filter(k => k.is_compliant === true).length;
        const rated = monitoringKws.filter(k => (k.effective_rate ?? k.detection_rate) != null);
        const clientSideAvg = rated.length > 0
            ? Math.round(rated.reduce((sum, k) => sum + (k.effective_rate ?? k.detection_rate ?? 0), 0) / rated.length)
            : null;
        // [2026-06-30 kou-jing] backend authority client_appearance_rate first (same source as trend latest); fallback to client-side
        const avgRate = (clientAppearanceRate !== null && clientAppearanceRate !== undefined)
            ? Math.round(Number(clientAppearanceRate))
            : clientSideAvg;
        return { total, compliant, avgRate, hasMonitoring: total > 0 };
    }, [keywords, clientAppearanceRate]);

    // [2026-06-04 视觉] 顶部健康度 + 本周重点派生(纯用现有 keywords/trendData · 不调接口)
    const trendChange = useMemo(() => {
        const pts = trendData.filter(d => d.rate != null);
        if (pts.length < 2) return null;
        return Math.round(((pts[pts.length - 1].rate as number) - (pts[0].rate as number)) * 10) / 10;
    }, [trendData]);

    // 一句话总结(面向王姐:看客户在 AI 里排得怎么样)
    const effectSummary = useMemo(() => {
        const { total, compliant, avgRate } = effectOverview;
        if (total === 0) return '';
        const trendSuffix = trendChange != null && trendChange < 0 ? '，但近 7 天有回落，需要继续补内容' : '';
        if (compliant === total) return `${total} 个词全部达标${trendSuffix || ',客户在 AI 里排得很稳'}`;
        if (compliant > 0) return `${compliant}/${total} 个词已达标${avgRate != null ? `,平均出现率约 ${avgRate}%` : ''}${trendSuffix}`;
        if (avgRate != null && avgRate > 0) return `还没有词达标,平均出现率约 ${avgRate}%,持续发文会慢慢涨上来`;
        return '词刚进监测,还没出排名数据,持续发文几天后再看';
    }, [effectOverview, trendChange]);
    const failCount = useMemo(
        () => keywords.filter(k => k.lifecycle === 'monitoring' && k.is_compliant === false).length,
        [keywords],
    );
    const minRemainingDays = useMemo(() => {
        const days = keywords.map(k => k.remaining_days).filter((d): d is number => typeof d === 'number');
        return days.length ? Math.min(...days) : null;
    }, [keywords]);

    // ========== Render ==========

    // CTO-15.20 v2 · C.2:URL ?brand_id 优先 · fallback ClientContext.currentBrandId
    const bridgeBrandId = activeBrandId;
    const selectedBrandId = selectedClientInfo?.brand_id && Number.isFinite(selectedClientInfo.brand_id)
        ? selectedClientInfo.brand_id
        : null;
    const insightsBrandId = selectedBrandId ?? bridgeBrandId;
    const canOpenInsights = Boolean(insightsBrandId);
    if (activeAccessMode === 'pending') {
        if (urlBrandRejected) {
            return (
                <div className="space-y-4 p-3 md:p-6" role="alert">
                    <div className="border-y border-destructive/30 bg-destructive/5 px-4 py-6 text-sm text-destructive">
                        当前账号无权访问该客户，或客户已不存在。
                    </div>
                </div>
            );
        }
        return (
            <div className="space-y-4 p-3 md:p-6" aria-label={clientContextError ? '客户访问加载失败' : '正在确认客户访问权限'}>
                {clientContextError ? (
                    <div className="border-y border-amber-500/30 bg-amber-500/5 px-4 py-6" role="alert" data-testid="client-access-error">
                        {/* [M-2 自审 2026-07-28] 这句原先写死"演示案例":403 不再清选择后本路径
                            暴露度大增,真实客户遇到权限抖动会看到一句与自己无关的话。按当前
                            选择是否为演示案例分流,且明说选择仍在、可重试。 */}
                        <p className="font-medium text-foreground">
                            {activeDemoSelection?.brandId === activeBrandId
                                ? '演示案例暂时无法加载'
                                : '这个客户的资料暂时读取不到'}
                        </p>
                        <p className="mt-1 text-sm text-muted-foreground">{clientContextError}。页面已停止自动重试，避免触发风控。</p>
                        <p className="mt-1 text-sm text-muted-foreground">你选中的客户没有被清空，点下方按钮或刷新即可重试。</p>
                        {/* 选择保留后同步 effect 一定已把 brand_id 吸附进 URL,故这里
                            用 urlBrandId 即可;不加"到不了"的兜底分支(无锁的代码不留)。 */}
                        {urlBrandId !== null ? (
                            <Button className="mt-4" variant="outline" onClick={() => switchClient(urlBrandId)}>
                                <RefreshCw className="mr-2 h-4 w-4" />重新验证访问权限
                            </Button>
                        ) : null}
                    </div>
                ) : (
                    <>
                        <div className="h-24 animate-pulse bg-muted/60" />
                        <div className="h-64 animate-pulse bg-muted/60" />
                    </>
                )}
            </div>
        );
    }
    return (
        <div className="p-3 md:p-6 space-y-4 md:space-y-6" data-access-mode={activeAccessMode}>
            {/* CTO-15.20 v2 · C.2 桥接 banner · 显示决策条 + 主按钮 + 回 M3(brandId 来自 URL 或 ClientContext) */}
            <BridgeBanner brandId={bridgeBrandId} showMissingState />

            {snapshotMissing && (
                <div className="border-y border-amber-500/30 bg-amber-500/5 px-3 py-2 text-sm text-amber-700 dark:text-amber-300" role="status">
                    快照未包含部分历史数据；对应区域保留正常页面布局并显示原生空态。
                </div>
            )}

            {/* Page header */}
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                <div className="flex items-center gap-3">
                    <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center shrink-0">
                        <Activity className="h-5 w-5 text-brand" />
                    </div>
                    <div>
                        <h1 className="text-xl md:text-2xl font-bold text-foreground">
                            {/* [#222 a1b'] 两身份同文案:文案按身份分叉与「权限一致」相悖,且不涉能力。 */}
                            监测中心
                        </h1>
                        <p className="text-xs md:text-sm text-muted-foreground">
                            AI 搜索出现率监测与趋势分析
                        </p>
                    </div>
                </div>
                <div className="grid w-full grid-cols-2 items-center gap-2 sm:flex sm:w-auto sm:flex-wrap">
                    <div
                        role="tablist"
                        aria-label="监测中心视图"
                        className="col-span-2 flex h-10 items-center gap-1 rounded-lg border border-border bg-muted/40 p-1 shadow-sm sm:col-auto"
                    >
                        <button
                            type="button"
                            role="tab"
                            aria-selected={!showInsights}
                            onClick={() => selectMonitoringView(false)}
                            className={`flex h-8 items-center gap-1.5 rounded-md px-3 text-sm font-medium whitespace-nowrap transition-[background-color,color,box-shadow] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${!showInsights ? 'bg-background text-foreground shadow-sm ring-1 ring-border/70' : 'text-muted-foreground hover:bg-background/60 hover:text-foreground'}`}
                        >
                            <Activity className={`h-4 w-4 ${!showInsights ? 'text-brand' : ''}`} />
                            监测执行
                        </button>
                        <button
                            type="button"
                            role="tab"
                            aria-selected={showInsights}
                            onClick={() => {
                                if (!canOpenInsights) {
                                    lazyToast.info('先选择客户，再查看数据洞察');
                                    return;
                                }
                                selectMonitoringView(true);
                            }}
                            disabled={!canOpenInsights}
                            title={canOpenInsights ? '查看当前客户的数据洞察' : '先选择客户，再查看数据洞察'}
                            className={`flex h-8 items-center gap-1.5 rounded-md px-3 text-sm font-medium whitespace-nowrap transition-[background-color,color,box-shadow] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-45 ${showInsights ? 'bg-background text-foreground shadow-sm ring-1 ring-border/70' : 'text-muted-foreground hover:bg-background/60 hover:text-foreground'}`}
                        >
                            <BarChart3 className={`h-4 w-4 ${showInsights ? 'text-brand' : ''}`} />
                            数据洞察
                        </button>
                    </div>
                    {isAdmin && (
                        <TestClientFilterToggle
                            value={includeTest}
                            onChange={setIncludeTest}
                            hiddenCount={testCount}
                        />
                    )}
                    {!showInsights && <Button className="w-full sm:w-auto" variant="outline" disabled={!selectedClient} onClick={() => fetchKeywords(parseInt(selectedClient))}>
                        <RefreshCw className="h-4 w-4 mr-2" />刷新
                    </Button>}
                    {showInsights && (
                        <Button className="w-full sm:w-auto" variant="outline" onClick={() => navigate('/monitoring/methodology')}>
                            <FileBarChart className="h-4 w-4 mr-2" />方法说明
                        </Button>
                    )}
                    {/* v1_3 CTO-15.1: 开始监测按钮 · 2026-06-03 全站静默扣费去价格角标 · 仅留动作 + 大额 toast 反悔 */}
                    {!showInsights && (
                        <Button className="w-full sm:w-auto" onClick={() => {
                            if (loading) {
                                monitorAbortRef.current?.abort();
                                setLoading(false);
                                lazyToast.message('已停止等待；后台任务可能仍在结算，重试会沿用同一请求');
                                return;
                            }
                            handleRunMonitoring();
                        }} disabled={!selectedClient || selectedKeywords.size === 0 || monitorSingleCost == null}
                        title={monitorSingleCost == null ? (pricingError || '正在读取动态价目') : undefined}>
                            {monitorSingleCost == null ? (
                                pricingLoading ? '价目读取中…' : PRICING_UNAVAILABLE_LABEL
                            ) : loading ? (
                                <><RefreshCw className="h-4 w-4 mr-2 animate-spin" />监测中（停止等待）</>
                            ) : selectedKeywords.size === 0 ? (
                                '请先勾选词条'
                            ) : (
                                <span className="inline-flex items-center gap-1.5">
                                    开始监测 ({selectedKeywords.size})
                                </span>
                            )}
                        </Button>
                    )}
                    {/* 🔴 [#199] 价目读不到时:原因画在按钮**外面**(手机没有 hover)+ 一个真能点的出口。 */}
                    {!showInsights && monitorSingleCost == null && !pricingLoading && (
                        <PricingUnavailableHint
                            error={pricingError}
                            retryNotBefore={pricingRetryNotBefore}
                            onRetry={() => { void pricingRefresh(); }}
                        />
                    )}
                    {/* CTO-13.0: 查看报告全员可用 */}
                    {!showInsights && (
                        <Button className="w-full sm:w-auto" variant="outline" onClick={() => { void fetchLatestResults(); }}>
                            <FileText className="h-4 w-4 mr-2" />查看报告
                        </Button>
                    )}
                </div>
            </div>

            {showInsights ? (
                <Suspense fallback={<div className="flex justify-center py-12"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>}>
                    <ObservationWorkbench
                        brandId={insightsBrandId}
                        brandName={selectedClientInfo?.brand_name}
                        unavailableFallback={(
                            <LegacyInsightsCenter
                                brandId={insightsBrandId}
                                brandName={selectedClientInfo?.brand_name}
                            />
                        )}
                    />
                </Suspense>
            ) : (<>
            {/* Client info bar */}
            {selectedClientInfo && (
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2.5 bg-muted border border-border rounded-lg text-sm text-muted-foreground">
                    {/* 🔴 [WO_251 §4] 显示**品牌现值**,不是下单那一刻的快照 ——
                        客户改名 / 当初分类选错时,快照不会变(Owner 截图:同一个客户
                        报价上是旧名 +「餐饮食品」,品牌现值是酒店)。
                        口径收在 `@/lib/clientDisplay` 一处,两个消费点不各写一份。 */}
                    <span data-testid="monitor-client-name" className="font-medium text-foreground whitespace-nowrap">{clientDisplayName(selectedClientInfo)}</span>
                    <span className="hidden sm:inline">·</span>
                    <span data-testid="monitor-client-industry" className="whitespace-nowrap">行业: {clientDisplayIndustry(selectedClientInfo)}</span>
                    <span className="hidden sm:inline">·</span>
                    {isAdmin ? (
                        <div className="flex items-center gap-1 whitespace-nowrap">
                            <span>套餐:</span>
                            <Select value={selectedClientInfo.tier || 'standard'} onValueChange={async (val) => {
                                try {
                                    const res = await authFetch(`/api/monitoring/tier?brand_id=${activeBrandId}&tier=${val}`, { method: 'PUT' });
                                    const data = await res.json();
                                    if (data.status === 'success') {
                                        setClients(prev => prev.map(c => c.quote_id === selectedClientInfo.quote_id ? { ...c, tier: val } : c));
                                        // 刷关键词表 · target_rate / is_compliant / effective_rate 全按新 tier 重算
                                        if (selectedClient) fetchKeywords(parseInt(selectedClient));
                                        const tierName = ({entry:'入门版', standard:'标准版', premium:'旗舰版'} as Record<string, string>)[val] || val;
                                        lazyToast.success(`已切换到${tierName}`);
                                    } else {
                                        lazyToast.error(data.error || '更新套餐失败');
                                    }
                                } catch (e) {
                                    console.error('更新套餐失败', e);
                                    lazyToast.error('更新套餐失败');
                                }
                            }}>
                                <SelectTrigger className="h-7 w-[120px] text-xs"><SelectValue /></SelectTrigger>
                                <SelectContent>
                                    <SelectItem value="entry">入门版 (50%)</SelectItem>
                                    <SelectItem value="standard">标准版 (65%)</SelectItem>
                                    <SelectItem value="premium">旗舰版 (75%)</SelectItem>
                                </SelectContent>
                            </Select>
                        </div>
                    ) : (
                        <span className="whitespace-nowrap">套餐: {{ entry: '入门版', standard: '标准版', premium: '旗舰版' }[selectedClientInfo.tier || 'standard'] || '标准版'}</span>
                    )}
                    {/* [服务期 SSOT 2026-08-06] 旧版这里把【累计达标天数配额】写成"服务期: N 天",
                        而真正决定自动监测排不排班的是 quotes.service_end_date —— 两者在 11/13 张
                        paid 报价上差了近一年,老板看到的"还有一年"就是这一行。
                        现在:服务期显真实起止日(后端 contract_end_date),达标配额单独一项、单独命名。 */}
                    <span className="whitespace-nowrap inline-flex items-center gap-1">
                        {currentServiceEnd ? (
                            <>
                                服务期至: {currentServiceEnd}
                                {currentServiceDaysLeft != null && (
                                    currentServiceDaysLeft >= 0
                                        ? <span className="text-muted-foreground">(剩 {currentServiceDaysLeft} 天)</span>
                                        : (
                                            /* [客户反馈⑥ 2026-08-09] 旧文案写死"(已到期 N 天 · 自动监测已暂停)"。
                                               那是**假的**:日历到期只卡"报价级轮换"那条排班链;
                                               建过逐词订阅的客户走"按达标天数履约"那条,到期照跑
                                               (实证 quote 287 过期 60 天、3 条订阅 active、前一天还在扣费)。
                                               现在只显示后端算出来的真实资格;后端没给就只报到期天数,
                                               **不做任何因果断言**。 */
                                            <span className="text-red-500 font-medium">
                                                (已到期 {-currentServiceDaysLeft} 天
                                                {autoMonitoring ? ` · ${autoMonitoring.message}` : ''})
                                            </span>
                                        )
                                )}
                            </>
                        ) : (
                            <span className="text-amber-600">
                                服务期未设{autoMonitoring ? ` · ${autoMonitoring.message}` : ''}
                            </span>
                        )}
                        {currentServiceEnd && currentServiceDaysLeft != null && currentServiceDaysLeft < 0 && (
                            <Button size="sm" variant="link" className="h-auto p-0 text-xs"
                                onClick={() => navigate('/pricing')}>
                                去续费 →
                            </Button>
                        )}
                        {!currentServiceEnd && (
                            <Button size="sm" variant="link" className="h-auto p-0 text-xs"
                                onClick={() => navigate('/pricing')}>
                                去设服务期 →
                            </Button>
                        )}
                        {/* [客户反馈⑥ 2026-08-09 · Review P1-1] 这段原本写着"自动监测只在服务期内排班,
                            到期就停排班" —— 横幅那句假话删掉了,它却还住在 tooltip 里。
                            真相是两条链:已建监测订阅的词按"累计达标天数"履约,日历到期照跑;
                            没建订阅的词才由服务期日历决定排不排班。 */}
                        <HelpHint title="服务期 / 达标天数 是两回事">
                            <b>服务期</b>(上面这个日期)= 合同买到哪天为止,到期该提醒续费。
                            <br /><b>累计达标天数</b>(下面那个)= 这一单承诺"每个词累计达标多少天"才算交付完成,
                            单位是达标天数,<b>不是日历天</b>,不会因为日历到期就归零。
                            <br />到期之后监测还跑不跑,分两种情况:<b>已建监测订阅的词</b>按累计达标天数继续跑,
                            达标满了才停;<b>没建订阅的词</b>由服务期日历决定,到期就排不上班,续费后恢复。
                            上面那行显示的就是这个客户<b>此刻的真实情况</b>,不用自己按日期推。
                        </HelpHint>
                    </span>
                    {currentServiceDays && (
                        <span className="whitespace-nowrap inline-flex items-center gap-1">
                            累计达标天数: {currentServiceDays}天
                        </span>
                    )}
                    <span className="sm:ml-auto text-xs text-muted-foreground whitespace-nowrap">{selectedClientInfo.keyword_count} 词条</span>
                </div>
            )}

            {/* [2026-06-04 视觉] 监测健康度 + 本周重点(把"现在好不好 / 下一步处理什么"提到第一屏)· 仅选中有监测数据客户时显示 */}
            {selectedClientInfo && effectOverview.hasMonitoring && (
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                    {/* 监测健康度 */}
                    <div className="lg:col-span-2 rounded-xl border border-brand/30 bg-brand/5 p-4 md:p-5">
                        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                            <div className="min-w-0">
                                <p className="flex items-center gap-1.5 text-xs text-muted-foreground"><TrendingUp className="h-3.5 w-3.5 text-brand" />近 7 天平均出现率</p>
                                <p className="mt-1 text-3xl font-bold tabular-nums text-brand">
                                    {effectOverview.avgRate != null ? `${effectOverview.avgRate}%` : '—'}
                                </p>
                                <p className="mt-1 text-sm font-medium text-foreground leading-snug">{effectSummary}</p>
                            </div>
                            {/* 趋势快照 */}
                            <div className="flex flex-col items-start gap-1 shrink-0 sm:items-end">
                                <MiniSparkline data={trendData.map(d => d.rate)} />
                                {trendError && <span role="status" className="max-w-52 text-right text-[11px] text-amber-600">{trendError}</span>}
                                {trendChange != null && (
                                    <span className={`text-xs font-medium tabular-nums ${trendChange >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
                                        近 7 天 {trendChange >= 0 ? '+' : ''}{trendChange}%
                                    </span>
                                )}
                            </div>
                        </div>
                        {/* KPI 四宫格 */}
                        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                            <div className="rounded-lg border border-border bg-card p-2.5">
                                <p className="text-xs text-muted-foreground">监测词条</p>
                                <p className="mt-0.5 text-lg font-bold tabular-nums">{effectOverview.total}</p>
                            </div>
                            <div className="rounded-lg border border-border bg-card p-2.5">
                                <p className="text-xs text-muted-foreground">已达标</p>
                                <p className="mt-0.5 text-lg font-bold tabular-nums text-emerald-500">{effectOverview.compliant}</p>
                            </div>
                            <div className="rounded-lg border border-border bg-card p-2.5">
                                <p className="text-xs text-muted-foreground">未达标</p>
                                <p className={`mt-0.5 text-lg font-bold tabular-nums ${(effectOverview.total - effectOverview.compliant) > 0 ? 'text-red-500' : 'text-muted-foreground'}`}>{effectOverview.total - effectOverview.compliant}</p>
                            </div>
                            <div className="rounded-lg border border-border bg-card p-2.5">
                                <p className="text-xs text-muted-foreground">平均变化</p>
                                <p className={`mt-0.5 text-lg font-bold tabular-nums ${trendChange == null ? 'text-muted-foreground' : trendChange >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
                                    {trendChange == null ? '—' : `${trendChange >= 0 ? '+' : ''}${trendChange}%`}
                                </p>
                            </div>
                        </div>
                        {/* 主按钮:查看效果报告 → 复用现有数据洞察(InsightsCenter)入口 */}
                        <Button
                            className="mt-4 w-full sm:w-auto"
                            onClick={() => {
                                if (!canOpenInsights) { lazyToast.info('先选择客户，再查看效果报告'); return; }
                                selectMonitoringView(true);
                            }}
                            disabled={!canOpenInsights}
                        >
                            <BarChart3 className="h-4 w-4 mr-2" />查看效果报告<ArrowRight className="h-4 w-4 ml-1.5" />
                        </Button>
                    </div>

                    {/* 本周重点(派生 · 不抢主内容) */}
                    <div className="lg:col-span-1 rounded-xl border border-border bg-card p-4 md:p-5">
                        <p className="text-sm font-semibold text-foreground">本周重点</p>
                        <div className="mt-3 space-y-2.5 text-sm">
                            <div className="flex items-center gap-2">
                                {failCount > 0 ? <AlertCircle className="h-4 w-4 shrink-0 text-red-500" /> : <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />}
                                <span className="text-foreground/90">{failCount > 0 ? `${failCount} 个词未达标 · 需补内容` : '暂无未达标词'}</span>
                            </div>
                            <div className="flex items-center gap-2">
                                <Clock className={`h-4 w-4 shrink-0 ${minRemainingDays != null && minRemainingDays <= 7 ? 'text-orange-500' : 'text-muted-foreground'}`} />
                                <span className="text-foreground/90">
                                    {minRemainingDays == null ? '服务期充足' : minRemainingDays <= 7 ? `最近一个词 ${minRemainingDays} 天后到期 · 关注续费` : `服务期最近到期还有 ${minRemainingDays} 天`}
                                </span>
                            </div>
                            <div className="flex items-center gap-2">
                                <FileBarChart className="h-4 w-4 shrink-0 text-brand" />
                                <span className="text-foreground/90">可生成本期效果报告交付客户</span>
                            </div>
                        </div>
                        {/* 轻量建议顺序 */}
                        <div className="mt-3 rounded-lg border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                            建议顺序:未达标词 → 补内容 → 生成报告
                        </div>
                    </div>
                </div>
            )}

            {/* 空状态引导 */}
            {clientsLoading && clients.length === 0 && (
                <div aria-label="正在读取监测客户" className="min-h-[276px] rounded-xl border border-border bg-muted/20 p-4">
                    <div className="h-5 w-32 animate-pulse rounded bg-muted" />
                    <div className="mt-4 h-4 w-full animate-pulse rounded bg-muted" />
                    <div className="mt-2 h-4 w-3/4 animate-pulse rounded bg-muted" />
                </div>
            )}

            {clientsError && clients.length === 0 && !clientsLoading && (
                <div role="alert" className="min-h-[276px] rounded-xl border border-amber-500/30 bg-amber-500/5 p-6 text-center">
                    <p className="text-sm font-medium text-foreground">{clientsError}</p>
                    <Button className="mt-4" size="sm" variant="outline" onClick={() => { void fetchClients(); }}>重新读取</Button>
                </div>
            )}

            {clients.length === 0 && !clientsLoading && !clientsError && !loading && (
                <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
                    <Activity className="h-12 w-12 mb-3 opacity-30" />
                    <p className="text-sm font-medium text-foreground">还没有监测中的客户</p>
                    <p className="text-xs mt-2 opacity-70 text-center max-w-md">先给客户做 AI 体检、签约报价,关键词会自动进入监测看效果</p>
                    {/* WJ-23/32 空态明确下一步 */}
                    <div className="flex gap-2 mt-4">
                        <Button size="sm" onClick={() => navigate('/diagnosis/new')}>去做 AI 体检</Button>
                        <Button size="sm" variant="outline" onClick={() => navigate('/pricing')}>生成报价单</Button>
                    </div>
                </div>
            )}

            {clients.length > 0 && activeBrandId && !selectedClientInfo && !loading && (
                <div className="flex flex-col items-center justify-center py-16 text-muted-foreground border border-dashed border-border rounded-xl bg-muted/30">
                    <Activity className="h-12 w-12 mb-3 opacity-30" />
                    <p className="text-sm font-medium text-foreground">当前客户还没有监测数据</p>
                    <p className="text-xs mt-2 opacity-70 text-center max-w-md">
                        这个客户还没有已确认/已付款的监测报价。先生成报价单签约,或手动添加要监测的关键词。
                    </p>
                    {/* WJ-23/32 空态分流:未签约→报价单 / 已签约→加词 */}
                    <div className="flex gap-2 mt-4">
                        <Button size="sm" onClick={() => navigate('/pricing')}>生成报价单</Button>
                        {/* [BUG5 2026-06-05] 添加关键词仅 admin · 非 admin 加词走报价/合同/服务变更流程 */}
                        {isAdmin && <Button size="sm" variant="outline" onClick={() => setShowAddDialog(true)}>添加关键词</Button>}
                    </div>
                </div>
            )}

            {/* 沙盒 step 4 · 快进 10 天横幅 */}
            {inStep4 && (
                <FeatureTooltip
                    featureId="sandbox_step4_intro"
                    stepId="first_monitoring"
                    title="第四步:看效果 · 盯出现率"
                    content={'签约后, 报价单里的关键词会自动进入监测, 不用你手动加。\n\n监测就是去 AI 里搜这些词、看你品牌有没有被推荐到。可以手动点"开始监测"跑一次, 也能在"定时监测"里设成每天自动跑(默认关, 按需自己开)。\n\n注意顶部: 教程帮你快进了 10 天, 下面是持续发文 10 天后的数据形态。真实使用时出现率要几天到几周才会慢慢涨。'}
                    side="bottom"
                    wrapClassName="relative block"
                    nextLabel="知道了, 往下看 →"
                    onNext={() => setTutorialStage('step4-rate')}
                    disabled={tutorialStage !== 'step4-intro'}
                >
                    <div className="flex items-start gap-3 rounded-xl border border-amber-400/50 bg-amber-50 px-4 py-3 dark:bg-amber-950/30">
                        <FastForward className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" />
                        {(tutorialStage === 'step4-recovered' || tutorialStage === 'step4-token-card' || tutorialStage === 'step4-token-share' || tutorialStage === 'step4-finish') ? (
                            <div className="text-sm leading-6">
                                <span className="font-semibold text-amber-700 dark:text-amber-300">补发后又快进了几天(第 18 天)</span>
                                <span className="text-amber-700/80 dark:text-amber-200/80">
                                    {' '}· 看看刚才补发的那个词出现率有没有追上来(演示)。
                                </span>
                            </div>
                        ) : (
                            <div className="text-sm leading-6">
                                <span className="font-semibold text-amber-700 dark:text-amber-300">教程已帮你快进 10 天</span>
                                <span className="text-amber-700/80 dark:text-amber-200/80">
                                    {' '}· 下面是这个客户持续发文 10 天后的真实数据形态(演示)。真实使用时, 出现率要持续发文、几天到几周才会慢慢涨上来。
                                </span>
                            </div>
                        )}
                    </div>
                </FeatureTooltip>
            )}

            {activeBrandId && (
                <IdentityReviewPanel
                    brandId={activeBrandId}
                    readOnly={activeAccessMode === 'demo'}
                    /* [#182] 有任务在跑时才值得继续轮询(另一个理由是"有待审条目")。 */
                    taskActive={showProgressPanel && !streamStalled}
                    // [M-1 ①] 确认落库后回写:重取任务详情刷新进度面板/结果行,
                    // 并刷新关键词统计(检出率随重判结果更新)。零引擎调用,只读本地 API。
                    onDecided={() => {
                        if (progressTaskId) {
                            void hydrateMonitoringTask(progressTaskId, false).catch(() => undefined);
                        }
                        if (selectedClient) {
                            void fetchKeywords(parseInt(selectedClient));
                        }
                    }}
                />
            )}

            {/* [#222 a1b' 2026-09-16] ActionCards 内部**不再有**「是不是服务商」这一维:
                原来它只差一张「数据回退」,Owner 已确认放开(删的是自己账号的监测历史)。
                还留着的只有 isAdmin —— 加词 / 清除数据 / 数据归档属系统参数,不是身份差。
                🔴 上一版注释写「按 isAdmin/isAgent 分级」,改完就成了假话;过期注释不会红,只会误导下一个人。 */}
            {selectedClientInfo && <ActionCards
                scheduleEnabled={scheduleEnabled}
                currentServiceDays={currentServiceDays}
                currentServiceStart={currentServiceStart}
                archivesCount={archives.length}
                selectedClient={selectedClient}
                currentBrandId={activeBrandId ?? undefined}
                isAdmin={isAdmin}
                onAddKeyword={() => setShowAddDialog(true)}
                    onTrend={() => { setShowTrendDialog(true); fetchTrendData(); }}
                    onToken={() => { setShowTokenDialog(true); if (selectedClient) fetchToken(parseInt(selectedClient)); if (sandboxActive && tutorialStage === 'step4-token-card') setTutorialStage('step4-token-share'); }}
                    onPublication={() => { setShowPublicationDialog(true); if (selectedClient) fetchPublications(parseInt(selectedClient)); }}
                    onLogs={() => { setShowLogsDialog(true); fetchOperationLogs(); }}
                    onReportGen={() => { setShowReportGenDialog(true); setReportGenResult(''); }}
                    onRollback={() => { setShowRollbackDialog(true); fetchRollbackTasks(); }}
                    onSchedule={() => { setShowScheduleDialog(true); fetchScheduleStatus(); fetchClientMonitoringConfig(); }}
                    onServiceConfig={() => {
                        setServiceConfigDays(currentServiceDays || 365);
                        setServiceConfigCustom('');
                        setShowServiceConfigDialog(true);
                    }}
                    onClearData={() => setShowClearDialog(true)}
                    onArchives={() => { setShowArchivesDialog(true); fetchArchives(); }}
                />}

            {/* 🔴 [#182 §1.4] 停滞态:120 秒没有任何帧 ⇒ 照实说,并给一个能点的出口。
                不加这一条时,格子会一直 pulse —— 屏幕说"还在跑",而其实没人再说话了。
                重试 = 重新发起同一次监测(后端 generation 机制保证幂等)。 */}
            {selectedClientInfo && showProgressPanel && streamStalled && (
                <div role="status" data-testid="monitoring-stream-stalled"
                    className="mb-3 flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300">
                    <span aria-hidden="true">⏱️</span>
                    <span className="flex-1">
                        等了 2 分钟没有新进展 —— 可能是引擎那边卡住了。这次没有多扣算力,可以重试。
                    </span>
                    <Button size="sm" variant="outline" data-testid="monitoring-stream-retry"
                        onClick={() => { void handleRunMonitoring(); }}>
                        重试这次监测
                    </Button>
                </div>
            )}

            {/* Progress panel */}
            {selectedClientInfo && showProgressPanel && (
                <ProgressPanel
                    loading={loading}
                    progressLogs={progressLogs}
                    progressTotal={progressTotal}
                    progressCompleted={progressCompleted}
                    cells={progressCells}
                    retryingCellId={retryingCellId}
                    onClose={() => {
                        setShowProgressPanel(false);
                        if (activeBrandId) window.localStorage.removeItem(`omnirank_monitoring_active_task_${activeBrandId}`);
                    }}
                    onToggleExpand={(idx) => setProgressLogs(prev => prev.map((l, i) => i === idx ? { ...l, expanded: !l.expanded } : l))}
                    onRetryCell={handleRetryMonitoringCell}
                />
            )}

            {/* Keyword table */}
            {selectedClientInfo && <KeywordTable
                keywords={keywords}
                selectedKeywords={selectedKeywords}
                loading={loading}
                isAdmin={isAdmin}
                selectedClient={selectedClient}
                latestUnsyncedTaskId={latestUnsyncedTaskId}
                syncingTaskId={syncingTaskId}
                serviceStartDate={currentServiceStart}
                serviceEndDate={currentServiceEnd}
                readOnly={activeAccessMode === 'demo'}
                onSelectKeywords={setSelectedKeywords}
                onSyncTrends={handleSyncTrends}
                onOpenWeights={handleOpenWeights}
                onDeleteKeyword={handleDeleteKeyword}
                onArchiveKeywords={handleArchiveKeywords}
                onRefreshKeywords={() => { if (selectedClient) fetchKeywords(parseInt(selectedClient)); }}
            />}

            {/* [§4.2 闭环] 超红海词单独风险区 · 高竞争·需单独报价·不计达标 · 绝不混进上方普通监测/达标词 */}
            {selectedClientInfo && superRedOceanKws.length > 0 && (
                <div className="mt-4 rounded-lg border border-red-500/30 bg-red-500/5 p-4">
                    <div className="flex items-center gap-2 mb-2">
                        <span className="text-sm font-semibold text-red-600 dark:text-red-400">🔴 高竞争词 · 需单独报价</span>
                        <span className="text-xs rounded-full border border-red-400 text-red-500 px-2 py-0.5">{superRedOceanKws.length} 词 · 不计达标</span>
                    </div>
                    <p className="text-xs text-muted-foreground mb-3">
                        这些词搜索竞争极度饱和（9 成以上）。已单独监测展示（下方逐引擎结果），但不纳入普通达标统计、不承诺出现率，需单独沟通方案与报价。
                    </p>
                    {/* [2026-06-06 老板:监测不隐藏] 超红海词逐引擎检测明细补回(提到/未提到均如实·仍不计达标) */}
                    <div className="space-y-2">
                        {superRedOceanKws.map(kw => {
                            const sroPlatformNames: Record<string, string> = { dashscope: '通义千问', deepseek: 'DeepSeek', yuanbao: '元宝', kimi: 'Kimi（历史）', doubao: '豆包' };
                            const sroDetails = kw.detection_details || [];
                            return (
                                <div key={kw.id} className="rounded-md bg-background border border-red-300/50 px-3 py-2">
                                    <div className="text-xs font-medium text-foreground">{kw.keyword}</div>
                                    {sroDetails.length > 0 ? (
                                        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
                                            {sroDetails.map((d, j) => (
                                                <span key={j} className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                                                    {d.is_detected
                                                        ? <CheckCircle2 className="h-3 w-3 text-green-500" />
                                                        : <Circle className="h-3 w-3 text-muted-foreground" />}
                                                    {sroPlatformNames[d.platform] || d.platform}
                                                    <span className={d.is_detected ? 'text-green-600' : 'text-muted-foreground'}>{d.is_detected ? '提到' : (d.pending ? '待监测' : '未提到')}</span>
                                                </span>
                                            ))}
                                        </div>
                                    ) : (
                                        <div className="mt-1 text-[11px] text-muted-foreground">已纳入监测 · 逐引擎明细将在下一轮监测后展示</div>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </div>
            )}

            {/* [CTO-15.23 2026-05-08 监测归档区] 已归档词条列表(默认折叠 · 展开看 + 续费/删除) */}
            {selectedClientInfo && (
                <ArchivedKeywordsList
                    quoteId={selectedClient ? parseInt(selectedClient) : null}
                    brandId={activeBrandId}
                    onChanged={refreshKeywordState}
                />
            )}

            </>)}

            {/* Dialogs load only after their existing action opens them. */}
            <Suspense fallback={null}>
                {showAddDialog && <AddKeywordDialog open onOpenChange={setShowAddDialog} newKeyword={newKeyword} onNewKeywordChange={setNewKeyword} onAdd={handleAddKeyword} />}
                {showTokenDialog && (
                    <TokenDialog open onOpenChange={setShowTokenDialog} tokenInfo={tokenInfo} copied={copied} onCopyToken={copyToken} onGenerateToken={handleGenerateToken}
                        tutorialMode={sandboxActive && tutorialStage === 'step4-token-share'}
                        onTutorialFinish={() => { setShowTokenDialog(false); setTutorialStage('step4-finish'); }}
                    />
                )}
                {showPublicationDialog && <PublicationDialog open onOpenChange={setShowPublicationDialog} publications={publications} newPublication={newPublication} onNewPublicationChange={setNewPublication} onAdd={handleAddPublication} onRefresh={() => selectedClient && fetchPublications(parseInt(selectedClient))} />}
                {showTrendDialog && <TrendChartDialog open onOpenChange={setShowTrendDialog} trendData={trendData} />}
                {showLogsDialog && <OperationLogsDialog open onOpenChange={setShowLogsDialog} operationLogs={operationLogs} />}
                {showReportGenDialog && <ReportGenDialog open onOpenChange={setShowReportGenDialog} reportGenType={reportGenType} onReportGenTypeChange={setReportGenType} reportGenLoading={reportGenLoading} reportGenResult={reportGenResult} currentBrandId={activeBrandId ?? undefined} onGenerate={handleGenerateReport} />}
                {showResultsDialog && <MonitoringResultsDialog open onOpenChange={setShowResultsDialog} results={monitoringResults} />}
                {showRollbackDialog && <RollbackDialog open onOpenChange={setShowRollbackDialog} rollbackTasks={rollbackTasks} selectedRollbackIds={selectedRollbackIds} onSelectedRollbackIdsChange={setSelectedRollbackIds} rollbackLoading={rollbackLoading} syncingTaskId={syncingTaskId} onRollback={handleRollback} onSyncTrends={handleSyncTrends} />}
                {showClearDialog && <ClearDataDialog open onOpenChange={setShowClearDialog} clearPassword={clearPassword} onClearPasswordChange={setClearPassword} clearReason={clearReason} onClearReasonChange={setClearReason} clearLoading={clearLoading} onClear={handleClearData} />}
                {showScheduleDialog && (
                    <ScheduleDialog
                        open
                        onOpenChange={setShowScheduleDialog}
                        scheduleHour={scheduleHour}
                        onScheduleHourChange={setScheduleHour}
                        scheduleMinute={scheduleMinute}
                        onScheduleMinuteChange={setScheduleMinute}
                        scheduleEnabled={scheduleEnabled}
                        onScheduleEnabledChange={setScheduleEnabled}
                        clientMonitoringEnabled={clientMonitoringEnabled}
                        onClientMonitoringEnabledChange={setClientMonitoringEnabled}
                        monitoringIntervalHours={monitoringIntervalHours}
                        onMonitoringIntervalHoursChange={setMonitoringIntervalHours}
                        monitoringStartHour={monitoringStartHour}
                        onMonitoringStartHourChange={setMonitoringStartHour}
                        clientMonitoringConfig={clientMonitoringConfig}
                        scheduleStatus={scheduleStatus}
                        scheduleLoading={scheduleLoading}
                        onSave={handleSaveSchedule}
                        onRunNow={handleRunNow}
                        onStop={handleStopMonitoring}
                    />
                )}
                {showArchivesDialog && <ArchivesDialog open onOpenChange={setShowArchivesDialog} archives={archives} restoreLoading={restoreLoading} onRestore={handleRestore} />}
                {showServiceConfigDialog && <ServiceConfigDialog open onOpenChange={setShowServiceConfigDialog} serviceConfigDays={serviceConfigDays} onServiceConfigDaysChange={setServiceConfigDays} serviceConfigCustom={serviceConfigCustom} onServiceConfigCustomChange={setServiceConfigCustom} serviceConfigLoading={serviceConfigLoading} currentServiceDays={currentServiceDays} currentServiceStart={currentServiceStart} currentServiceEnd={currentServiceEnd} selectedClient={selectedClient} onSave={handleSaveServiceConfig} onGoToServicePeriod={() => navigate('/pricing')} />}
                {showWeightsDialog && <PlatformWeightsDialog open onOpenChange={setShowWeightsDialog} weightsLoading={weightsLoading} weightsSearching={weightsSearching} platformWeights={platformWeights} onPlatformWeightsChange={setPlatformWeights} platformMau={platformMau} weightsSource={weightsSource} weightsUpdatedAt={weightsUpdatedAt} onSearchMau={handleSearchMau} onSave={handleSaveWeights} />}
            </Suspense>

            {/* ===== 沙盒 step 4 · 柔和的"时间快进"过渡(补发后回监测页时播一次) ===== */}
            {showTimeSkip && (
                <div
                    onClick={dismissTimeSkip}
                    className={`fixed inset-0 z-[200] flex cursor-pointer items-center justify-center bg-background/60 backdrop-blur-sm transition-opacity duration-500 ${timeSkipFading ? 'opacity-0' : 'opacity-100'}`}
                >
                    <div className="flex flex-col items-center gap-3 rounded-2xl border border-border bg-card px-12 py-9 shadow-xl animate-in fade-in zoom-in-95 duration-500">
                        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-amber-500/10">
                            <FastForward className="h-6 w-6 text-amber-500 animate-pulse" />
                        </div>
                        <div className="text-xs text-muted-foreground">{timeSkipText.sub}</div>
                        <div className="text-4xl font-bold text-foreground">{timeSkipText.big}</div>
                        <div className="text-xs text-muted-foreground">{timeSkipText.tip}</div>
                        <div className="mt-1 text-[11px] text-muted-foreground/60">点击任意处继续</div>
                    </div>
                </div>
            )}

            {/* ===== 沙盒 step 4 收尾 · 完整闭环总结窗 ===== */}
            <Dialog open={sandboxActive && tutorialStage === 'step4-finish'} onOpenChange={() => { /* 锁住 · 只能点按钮收尾 */ }}>
                <DialogContent className="max-w-xl [&>button]:hidden">
                    <div className="flex flex-col items-center text-center pt-2">
                        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-amber-500/10 animate-in zoom-in-50 duration-500">
                            <Trophy className="h-8 w-8 text-amber-500" />
                        </div>
                        <DialogTitle className="mt-3 text-xl">🎉 恭喜!你已掌握 {monitoringBrandName} 全流程</DialogTitle>
                        <DialogDescription className="text-sm mt-2 leading-6">
                            你刚刚完整走了一圈:发现一个词没达标 → 给它补发文章 → 过几天它也追上来达标了。
                        </DialogDescription>
                    </div>

                    <div className="my-4 rounded-lg border border-border bg-muted/30 p-4">
                        <div className="text-sm font-medium mb-2">你走完的完整流程:</div>
                        <ol className="text-sm text-muted-foreground space-y-1.5 leading-5">
                            <li>1. <strong className="text-foreground">品牌体检</strong> — 看品牌在 AI 里的现状</li>
                            <li>2. <strong className="text-foreground">报价方案</strong> — 选词、出报价、签约</li>
                            <li>3. <strong className="text-foreground">AI 写文章 + 发布</strong> — 生成文章、过审、发到各媒体</li>
                            <li>4. <strong className="text-foreground">盯出现率 + 补救</strong> — 哪个词没达标就补发, 直到追上来</li>
                        </ol>
                        <div className="mt-3 pt-3 border-t border-border/60 text-sm leading-6">
                            <span className="font-semibold text-amber-600 dark:text-amber-400">真实运营就是这个循环:</span>
                            <br />发文 → 盯出现率 → 哪个词慢了/掉了就补发 → 再盯,一个月一个月把词推到达标。
                        </div>
                    </div>

                    <DialogFooter>
                        <Button
                            className="bg-amber-500 hover:bg-amber-600 text-white"
                            onClick={() => {
                                markStep('first_monitoring');
                                setTutorialStage('done');
                                exitSandbox();
                                // 退出沙盒回首页 · 整页刷新进真实态(清干净沙盒内存)
                                window.location.href = '/';
                            }}
                        >
                            <Sparkles className="h-4 w-4 mr-1.5" />
                            开始正式使用
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
          {confirmDialog}
            <NextStepBar page="monitoring" />
        </div>
    );
}
