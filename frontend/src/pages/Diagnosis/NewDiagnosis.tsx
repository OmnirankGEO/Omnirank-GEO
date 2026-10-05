import { useState, useEffect, useRef, useCallback, lazy, Suspense } from "react";
import { useSimulatedThinking } from '@/hooks/useSimulatedThinking';
import { DEFENSIVE_GEO_PLATFORM_KEYS } from '@/lib/defensiveGeoEngines';
import { industryCategoryText, useIndustryTaxonomy } from '@/lib/industryTaxonomy';
import { SimulatedThinking } from '@/components/SimulatedThinking';
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useSearchParams } from "react-router-dom";
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import {
    ArrowLeft,
    ChevronDown,
    ChevronRight,
    ClipboardList,
    Eye,
    Globe,
    HelpCircle,
    Info,
    Loader2,
    Quote,
    RefreshCw,
    Search,
    Search as SearchIcon,
    Sparkles,
    Tag,
    Target,
    Upload,
    UserCircle,
    Users,
} from 'lucide-react';
import api, { diagnosisApi, brandsApi, authFetch, formatApiErrorForDisplay, loadOrCreateDiagnosisRequestId, clearDiagnosisRequestId, isDeterministicDiagnosisReject } from "@/lib/api";
import { useClientContext } from '@/context/ClientContext';
import { useWallet } from '@/context/WalletContext';
import { useUnsavedWarning } from '@/hooks/useUnsavedWarning';
import { useAuth } from '@/context/AuthContext';
import DeductDialog from '@/components/DeductDialog';
// v1_3 (CTO-15.1 2026-04-19): 动态价目表(全站静默扣费 · 不前置展示价格 · useFeatureCost 仍用于余额预检)
import { usePricing } from '@/context/PricingContext';
import { BridgeBanner } from '@/components/workbench/BridgeBanner';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import axios from "axios";
import { awaitConfirmedSessionToken } from '@/lib/authoritativeSession';
import { useMarkStepCompleted } from '@/hooks/useMarkStepCompleted';
import { FeatureTooltip } from '@/components/onboarding/FeatureTooltip';
import { isSandboxActive } from '@/sandbox/sandboxState';
import { SANDBOX_DEMO_PRESET } from '@/sandbox/mockData';
import { useTutorialStage, setTutorialStage } from '@/sandbox/tutorialStage';
// [diaglaunch 2026-08-05] 发起页 IA 升级(工单 WO_DIAGNOSIS_LAUNCH_UI_2026-08-05)
import { LaunchStepBar } from './launch/LaunchStepBar';
import { resolveLaunchStep } from './launch/launchSteps';
import { isLaunchBlocked } from './launch/defensiveLaunchGate';
import { wouldEmptySide, sideFromTarget, sideLabel, sideCounts, questionSetSignature,
    planMatchesDraft, questionSetKey } from './launch/planSideGuard';
import type { ModeSide, PlanMode } from './launch/planSideGuard';
// [defgeo 装配 2026-08-21] 防御型 GEO 目标模式与两阶段启动。
// 🔴 [包一阶段① 2026-09-05 订正] 原注释写的是「offensive 时只有 ModeRadioCards 会渲染」——
//    三张卡已被 ModeTabs 取代,那句话现在是反的。页面里**模式选择器只有一个**(ModeTabs);
//    下面这行只从该模块取 `modeFromSearchParams` 与 `CampaignMode` 类型,**不是组件**。
//    (`ModeRadioCards.tsx` 文件本身留到阶段④与自动收敛一并处置;届时这两个导出要先搬家。)
//    提交路径仍逐字走 legacy —— 见 DEFGEO_WIRE_ASSEMBLY_CENSUS §3。
import {
    modeFromSearchParams, type CampaignMode,
} from '@/components/defensiveGeo/ModeRadioCards';
// [包一 · 订正三] 三张模式卡 → 页内三标签。说明文案仍来自 modeOptions(),没有第二份。
import { ModeTabs } from '@/components/defensiveGeo/ModeTabs';
// [包一阶段②] 诊断对象四项:摘要态 / 表单态两态呈现(订正六),四项一个不丢。
import { SubjectSummary } from './launch/SubjectSummary';
import { BUSINESS_SCOPE_OPTIONS, subjectComplete } from './launch/subjectFields';
import { looksLikeTestBrand, matchedTestPatterns } from './launch/testBrandName';
// [阶段③ · 订正十二] 三分支「跑哪些题」单源:题单显示 / 按钮价 / 真跑三处都从它取。
import { aiQuestionsMuted, mutedNotice, effectiveQuestionCount } from './launch/questionRunPlan';
// [阶段⑤ · 订正二十一] 价**只从服务端 POST 取**,前端不算钱。
import { fetchDiagnosisPrice, priceMatchesInput, type DiagnosisPricePreview } from './launch/diagnosisPricePreview';
// [⑤b · 订正二十四] 唯一题源的归一化(与服务端 validate_custom_questions 同解)。
import { collectOwnQuestions, CUSTOM_QUESTION_MAX_CHARS } from './launch/ownQuestions';
import { fetchSuggestedQuestions, toSuggestMode, buildQuestionMeta, growthRunSet } from './launch/suggestQuestionsApi';
import {
    collectSearchTerms, isSearchTermTooLong,
    SEARCH_TERM_MAX_CHARS, SEARCH_TERMS_MAX_COUNT,
} from './launch/searchTerms';
import {
    brandSwitchNotice, emptyStateNotice, keywordsForSubmit, shouldAcceptRefill, sourceLabel,
    type PrefillState,
} from './launch/searchTermsPrefill';
import { fetchSearchTermSuggestion } from './launch/searchTermsApi';
import { sideDefaults } from './launch/planSideGuard';
import {
    QuestionPlanEditor, suggestedQuestions, removalKey,
} from '@/components/defensiveGeo/QuestionPlanEditor';
import { LaunchPanel } from '@/components/defensiveGeo/LaunchPanel';
// [包H] 前端文案 SSOT。机械从后端 copy registry 生成,build 链里有门逐字节比对。
import { DEFGEO_COPY } from '@/lib/defensiveGeoCopy';
import {
    DefGeoError, previewQuestionPlan,
    type DraftQuestion, type QuestionPlanResponse,
} from '@/lib/defensiveGeoApi';

const HistoryList = lazy(() => import('@/pages/History/HistoryList').then(m => ({ default: m.HistoryList })));

const INDUSTRIES = [
    { value: "科技服务", label: "科技/SaaS" },
    { value: "金融保险", label: "金融/保险" },
    { value: "医疗健康", label: "医疗/健康" },
    { value: "教育培训", label: "教育/培训" },
    { value: "消费品", label: "消费品" },
    { value: "制造业", label: "制造业" },
    { value: "营销广告", label: "营销/广告" },
    { value: "电商", label: "电商" },
    { value: "出海服务", label: "出海服务" },
    { value: "其他", label: "其他" },
];

function brandDisplayNamesText(value: unknown): string {
    if (Array.isArray(value)) {
        return value.filter((item): item is string => typeof item === 'string').join('\n');
    }
    if (typeof value !== 'string' || !value.trim()) return '';
    try {
        const parsed = JSON.parse(value);
        if (Array.isArray(parsed)) {
            return parsed.filter((item): item is string => typeof item === 'string').join('\n');
        }
    } catch { /* legacy plain-text value */ }
    return value.split(/[\n,，;；]+/).map(item => item.trim()).filter(Boolean).join('\n');
}

/**
 * [CUR-05] 能力文案由服务端运行计划驱动,不写死引擎名单与耗时。
 * 服务端没给时退到不含具体数字的说法 —— 宁可少说,也不要说一个可能已经不对的数。
 */
export function buildCapabilityHint(plan?: { platform_names?: string[]; eta_minutes?: number } | null): string {
    const names = plan?.platform_names?.filter(Boolean) ?? [];
    const eta = typeof plan?.eta_minutes === 'number' && Number.isFinite(plan.eta_minutes)
        ? `约 ${plan.eta_minutes} 分钟出报告`
        : '完成后通知你';
    const engines = names.length > 0
        ? `AI 会用本次可用的 ${names.length} 个平台(${names.join('/')})同步检测`
        : 'AI 会用本次可用的平台同步检测';
    return `填品牌名 + 关键词,${engines}你品牌在 AI 答案里的可见度,${eta}`;
}

export function NewDiagnosis() {
    const navigate = useEmbeddedNavigate();
    const markStep = useMarkStepCompleted();
    const [searchParams, setSearchParams] = useSearchParams();
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [showAdvanced, setShowAdvanced] = useState(false);
    /**
     * 🔴 [订正二十九 · 乙案] 搜索词。**刻意不放进 `formData`** ——
     * 规格要求「默认空 · 不预填」,而 formData 有四类写入者(品牌档案 / AI 补全 / 事件补填 / 重置)。
     * 放在自己的 state 里,「没有任何东西预填它」就是**结构性**的、不靠纪律维持,
     * 判据也能机械证明:它的 setter 全仓只该有输入框那一处。
     */
    const [searchTermsRaw, setSearchTermsRaw] = useState('');
    /**
     * 🔴 [订正三十①] 预填状态与 dirty。
     * dirty = 她动过这个框。**回填只在 dirty=false 时发生** —— 否则端点响应晚到会把
     * 她刚打的字冲掉,而屏幕上只表现为「字自己变了」:不报错、没得点回去,她只会以为自己手滑。
     */
    const [prefill, setPrefill] = useState<PrefillState>({ kind: 'loading' });
    const [searchTermsDirty, setSearchTermsDirty] = useState(false);
    const [brandSwitchMsg, setBrandSwitchMsg] = useState<string | null>(null);
    const prevBrandRef = useRef<{ id: number | null; name: string }>({ id: null, name: '' });
    const [file, setFile] = useState<File | null>(null);
    const [, setLoadingRetest] = useState(false);
    const [autofilling, setAutofilling] = useState(false);
    const { thinkingSteps: autofillThinking, isThinking: isAutofillThinking } = useSimulatedThinking(autofilling, {
      steps: ['正在联网搜索品牌信息...', '提取行业和关键词...', '分析竞品...', '整理填充数据...'],
      intervalMs: 1500,
    });
    const [autofillStatus, setAutofillStatus] = useState<string>("");  // 当前搜索状态提示
    const [autofillError, setAutofillError] = useState<string | null>(null);
    const [activeTab, setActiveTab] = useState<'new' | 'history'>('new');

    // [CUR-05 2026-08-21] 本次实际可用平台与预计耗时由服务端运行计划下发。
    // WP2 的 start-context 端点(窗A 地界)接上后填这个 state;在它到位之前
    // 保持 null —— buildCapabilityHint(null) 会退到**不含具体数字**的说法,
    // 而不是继续对用户说"4 大引擎、5 分钟"这种可能已经不成立的话。
    const [startContext] = useState<{ platform_names?: string[]; eta_minutes?: number } | null>(null);
    const planCapabilityHint = buildCapabilityHint(startContext);

    // ── [defgeo 装配 2026-08-21] 目标模式与防御题单 ────────────────────
    // 🔴 初值来自 ?goal=,非法/缺省**确定性回 offensive** ⇒ 旧链接/旧书签
    //    全部落在 legacy 分支,提交路径逐字不变(ACT-01)。
    const [campaignMode, setCampaignMode] = useState<CampaignMode>(
        () => modeFromSearchParams(window.location.search));
    /**
     * 🔴 [WO_PLAN_SIDE_MISMATCH §5] 她点过"改回只测会问"吗?
     *
     * 只有一个用途:**打断自动收敛的死循环**。
     * 没有它:混侧 ⇒ 自动切 hybrid ⇒ 她改回 defensive ⇒ 又被切回 hybrid,
     * 而这在屏幕上表现为"按钮点了没反应" —— 与我们正在修的症状一模一样。
     */
    /**
     * 🔴 **是不是我们替她切的**。只用来决定那句话怎么说。
     *
     * 没有它就只能靠"现在是 hybrid + 有会搜的题"来推,而她**自己**选混合时
     * 这个条件同样成立 ⇒ 会对她说一句「已改成混合体检」——
     * **一句具体但错误的话**:她会以为系统动了她的选择,然后去找哪里被改了。
     * 笼统的提示让人继续找,具体但错误的提示让人停止找并走错方向。
     */
    // 建议题与人工题**分开存**:切模式只重算建议那半,人工题原样保留 ——
    // "静默删用户输入"在数据结构层就发生不了(§9.2)。
    const [manualQuestions, setManualQuestions] = useState<DraftQuestion[]>([]);
    const [removedSuggested, setRemovedSuggested] = useState<Set<string>>(new Set());
    const [questionPlan, setQuestionPlan] = useState<QuestionPlanResponse | null>(null);
    const [planError, setPlanError] = useState<DefGeoError | null>(null);
    /** [WO §3.1.5] 刚刚被拦下的那一侧(删到只剩一道时的内联提示),null = 没有。 */
    const [lastSideBlocked, setLastSideBlocked] = useState<ModeSide | null>(null);
    const planEditorRef = useRef<HTMLDivElement | null>(null);
    /**
     * 🔴 [WO §3.2] 最近一次**成功** preview 下发的计价规则。
     *
     * 为什么要缓存:0 题时 `question-plans/preview` 是 422 `plan_empty`,**拿不到规则**;
     * 而 Owner 要看的恰恰是「删光之后那一屏也显示起步价」。实践中系统建议题一进来
     * 就会成功 preview 一次,所以那一屏会有数字。
     * 🔴 品牌 / 模式一变就清 —— 规则可能随之不同,**留着旧的等于显示别的品牌的价**。
     * 🔴 只存不算:前端不拿它做任何乘法,总价仍只来自 run-previews。
     */
    const [pricingRule, setPricingRule] = useState<QuestionPlanResponse['pricingRule'] | null>(null);
    /**
     * 🔴 [WO §3.2 第三态] 上一次**成功** preview 时那份题单的签名。
     * 与当前题集不一致 ⇒ 已算出的价对不上现在的题单了 ⇒ 面板要把它标成过期。
     * **只存签名不存价**:价永远来自服务端,前端不算钱。
     */
    const [pricedSignature, setPricedSignature] = useState<string | null>(null);
    const [planPending, setPlanPending] = useState(false);
    /** 🔴 唯一分流键。offensive ⇒ 一切走 legacy。 */
    const isDefensiveFlow = campaignMode !== 'offensive';


    // 复测模式参数
    const isRetest = searchParams.get('retest') === '1';
    const originalId = searchParams.get('original_id');
    const originalScore = searchParams.get('original_score');
    const prefillBrandName = searchParams.get('brand_name') || '';
    const prefillIndustry = searchParams.get('industry') || '';

    // 复测参数变化时（同页面 URL 跳转）：同步表单 + 切到"发起体检"Tab
    useEffect(() => {
        if (isRetest && prefillBrandName) {
            setFormData(prev => ({
                ...prev,
                brandName: prefillBrandName,
                industry: prefillIndustry || prev.industry,
            }));
            setActiveBrandId(null);
            setActiveTab('new');
        }
    }, [isRetest, originalId]);

    // [CTO-15.23 2026-05-21] 复测继承自定义诊断问题(老板拍板 #7:默认继承可改)
    // 从原诊断记录读 custom_questions(列优先,raw_data_json.input_params 兜底)
    useEffect(() => {
        if (!isRetest || !originalId) return;
        let cancelled = false;
        (async () => {
            try {
                const res = await diagnosisApi.getDetail(parseInt(originalId, 10));
                if (cancelled) return;
                const rec: any = res.data || {};
                let arr: string[] = [];
                // 优先取列
                const raw = rec.custom_questions;
                if (Array.isArray(raw)) {
                    arr = raw.filter((q: any) => typeof q === 'string' && q.trim());
                } else if (typeof raw === 'string' && raw.trim()) {
                    try { const parsed = JSON.parse(raw); if (Array.isArray(parsed)) arr = parsed; } catch { /* skip */ }
                }
                // 兜底从 raw_data_json.input_params 取
                if (!arr.length && rec.raw_data_json) {
                    try {
                        const rd = typeof rec.raw_data_json === 'string' ? JSON.parse(rec.raw_data_json) : rec.raw_data_json;
                        const candidate = rd?.input_params?.custom_questions;
                        if (Array.isArray(candidate)) arr = candidate.filter((q: any) => typeof q === 'string' && q.trim());
                    } catch { /* skip */ }
                }
                if (arr.length) {
                    // 复测继承自定义题 + ai_optimize_custom toggle 状态(input_params 里也带了)
                    let prevOptimize = false;
                    let prevMeta: Array<{ text?: string; origin?: string; side?: string; layer?: string }> = [];
                    try {
                        const rd = typeof rec.raw_data_json === 'string' ? JSON.parse(rec.raw_data_json) : rec.raw_data_json;
                        prevOptimize = !!(rd?.input_params?.ai_optimize_custom);
                        // 🔴 [#149] 复测按 `question_meta` 还原两半。
                        //    老记录没有这一格 ⇒ 全部按 `customer`(与今天逐字节同行为)。
                        //    不还原的话:上次 AI 出的 8 道会以「她自己写的」身份重跑,
                        //    豁免与报告文案都跟着走偏,而屏幕上看不出区别。
                        const m = rd?.input_params?.question_meta;
                        if (Array.isArray(m)) prevMeta = m;
                    } catch { /* skip */ }
                    const metaByText = new Map(prevMeta
                        .filter((x) => typeof x?.text === 'string')
                        .map((x) => [String(x.text).trim(), x]));
                    /**
                     * 🔴 [⑤b · 订正二十四] 复测预填要写进**题单编辑器**,不是那个已删的字段。
                     *
                     * 改前它写 `formData.customQuestions`。本笔把唯一题源换成 `manualQuestions`
                     * 之后,继续写那个字段 = **写进一个没人读的地方** ——
                     * 她点「复测」,上次的题静默不回来,而屏幕上没有任何字说明。
                     * (只写不读的字段最难发现:它不报错、不空指针,只是什么都不做。)
                     */
                    const _side: 'defensive' | 'offensive' = campaignMode === 'offensive' ? 'offensive' : 'defensive';
                    const _rows = arr.map((text: string) => {
                        const meta = metaByText.get(String(text).trim());
                        const side = meta?.side === 'growth' ? 'offensive'
                            : meta?.side === 'defensive' ? 'defensive' : _side;
                        // origin 缺省(老记录)⇒ customer,与改前逐字节同行为。
                        const src: 'user' | 'ai' = meta?.origin === 'ai_suggested' ? 'ai' : 'user';
                        return {
                            text, modeSide: side, ...sideDefaults(side), source: src,
                            ...(meta?.layer ? { layer: String(meta.layer) } : {}),
                        };
                    });
                    // AI 出的那半回到 suggested 区,她自己写的回到 manual 区 —— 两半各归各位。
                    setAiSuggested(_rows.filter((r) => r.source === 'ai'));
                    setManualQuestions(_rows.filter((r) => r.source === 'user'));
                    setFormData(prev => ({ ...prev, aiOptimizeCustom: prevOptimize }));
                }
            } catch { /* 失败不影响主流程,让用户手动重填即可 */ }
        })();
        return () => { cancelled = true; };
    }, [isRetest, originalId]);

    // 🆕 全局品牌上下文
    const { currentBrandId, clientContext } = useClientContext();
    const { nameOf: industryNameOf } = useIndustryTaxonomy();

    // 积分扣费
    // [CTO-15.23 2026-05-21 P0 修双扣] deductPoints 已下线 · 真扣费走后端 freeze_points 单一入口
    // [P5b 2026-06-03] 长任务先占用提示走 notifyFreezeHold(freeze 型:做成才正式扣)
    // [V3.5 v8 P0 2026-06-08 Codex 复审]诊断是工具类 feature · 必须用 toolPointsAvailable
    // 用 totalPoints 会把 V3.5 客户 publish_credit 也算进去 · publish>>tool 时前端"够"后端"402"
    const { toolPointsAvailable, status: walletStatus, notifyFreezeHold } = useWallet();
    const { user } = useAuth();
    const isAdmin = user?.is_admin === true;
    // [P2-2 fix 2026-05-23] L0(普通用户)文案口径:不是"给客户做" · L0 是给自己品牌做
    const isL0 = (user?.agent_level ?? 0) < 1;
    const [showDeductDialog, setShowDeductDialog] = useState(false);
    const [deductLoading, setDeductLoading] = useState(false);
    const [confirmDialog, askConfirm] = useConfirmDialog();

    // Stage 1 Batch 4 (2026-05-18) · 沙盒 step 1 引导链 · 直接读全局 tutorial stage
    // 阶段: 'page-intro' (页面介绍) → 'brand-name' (品牌名预填) → 'autofill' (AI 填写) → 'submit' (开始诊断) → 'done'
    const tutorialStage = useTutorialStage();

    // 沙盒态下落地此页面时自动推进到 'page-intro' (兼容用户绕过 sidebar 直接 URL 访问)
    // 2026-05-22 BUGFIX: 不再从 'modal' 自动推进 · 否则新代理 reload 后落到 /diagnosis/new 时
    // SandboxIntroModal 还没关闭就把 page-intro spotlight 一起弹出 (两层弹窗叠加)
    // 只有用户主动看完 SandboxIntroModal · setTutorialStage('sidebar') 后 · 直接 URL 访问才走这条推进
    useEffect(() => {
        if (!isSandboxActive()) return;
        if (tutorialStage === 'sidebar') {
            setTutorialStage('page-intro');
        }
    }, [tutorialStage]);

    const [formData, setFormData] = useState({
        brandName: prefillBrandName,
        industry: prefillIndustry,
        ownAccounts: "",
        competitors: "",
        additionalInfo: "",
        clientLocation: "",
        businessScope: "regional",   // [P0-4] 业务范围：regional(默认) | national
        diagnosisScope: "geo",
        brandDisplayNames: "",  // 品牌别名
        customQuestions: "",     // [CTO-15.23 2026-05-21] 自定义诊断问题(每行一题 · 单题 100 字)
        aiOptimizeCustom: false, // [CTO-15.23 2026-05-21 老板订正] 主动优化 toggle:false=verbatim 客户填啥跑啥 / true=LLM 双轨补足
    });

    // 品牌下拉列表
    const [brandOptions, setBrandOptions] = useState<{ id: number; name: string; industry?: string; owner_name?: string }[]>([]);
    const [brandDropOpen, setBrandDropOpen] = useState(false);
    const [expandedBrandId, setExpandedBrandId] = useState<number | null>(null);
    const [selectedBrandId, setSelectedBrandId] = useState<number | null>(null);
    /**
     * [包一阶段② · 订正六] 四项齐 ⇒ 压成一行摘要;点「改」回表单。
     * 🔴 摘要**只渲染当前 formData**,不缓存快照 —— 存快照就会出现
     *    「她看到的 ≠ 她提交的」(与 #62 同形),而订正六补那条判据钉的正是这个。
     */
    const [subjectEditing, setSubjectEditing] = useState(false);
    /** [阶段② · 订正十补] 她看过「这名字会被当测试客户」并点了「仍要用」。 */
    const [testNameAck, setTestNameAck] = useState(false);
    const subjectSummaryMode = !subjectEditing && subjectComplete(formData);
    /**
     * 只对**新品牌**判(未匹配到已有客户):已存在的品牌 `is_test` 早就定了,
     * 对它提示「会被打成测试客户」是错的 —— 打标只发生在**新建**那一刻
     * (`detect_is_test_for_new_brand`,三个写入点都在 INSERT 前调)。
     */
    const isNewBrandName = !!formData.brandName.trim() && !selectedBrandId;
    const testNameHit = isNewBrandName && looksLikeTestBrand(formData.brandName);
    const testNameUnacknowledged = testNameHit && !testNameAck;
    useEffect(() => { setTestNameAck(false); }, [formData.brandName]);
    /**
     * 🔴 摘要态下改了品牌名 ⇒ 退回表单态。
     *
     * 不这么做的话:她把名字换成另一个新客户,而行业/城市/范围**还是上一个客户的值**,
     * 摘要行照旧显示着它们。那些值确实会被提交(所以不是"看到的≠提交的"),
     * 但她很可能不会注意到这三项还是旧的 —— 而这三项**直接决定出什么题**
     * (区域生意出「城市+行业」题、全国出行业大词,填错命中率失真)。
     * 退回表单态 = 把这件事摆到她眼前,让她自己确认或改掉。
     */
    const lastSummarizedNameRef = useRef<string>('');
    useEffect(() => {
        if (subjectSummaryMode) { lastSummarizedNameRef.current = formData.brandName; return; }
    }, [subjectSummaryMode, formData.brandName]);
    useEffect(() => {
        if (!subjectSummaryMode) return;
        if (lastSummarizedNameRef.current && lastSummarizedNameRef.current !== formData.brandName) {
            setSubjectEditing(true);
        }
    }, [formData.brandName, subjectSummaryMode]);
    const selectedBrandIdRef = useRef<number | null>(null);
    const brandLoadRef = useRef<{ generation: number; controller: AbortController | null }>({
        generation: 0,
        controller: null,
    });
    const brandInputRef = useRef<HTMLInputElement>(null);
    const brandDropRef = useRef<HTMLDivElement>(null);

    const setActiveBrandId = useCallback((brandId: number | null) => {
        if (selectedBrandIdRef.current !== brandId) {
            brandLoadRef.current.controller?.abort();
            brandLoadRef.current.controller = null;
            brandLoadRef.current.generation += 1;
        }
        selectedBrandIdRef.current = brandId;
        setSelectedBrandId(brandId);
    }, []);

    useEffect(() => () => {
        brandLoadRef.current.controller?.abort();
        brandLoadRef.current.controller = null;
        brandLoadRef.current.generation += 1;
    }, []);

    // [CTO-15.23 2026-05-13 BUG fix] industry 下拉:HTML 原生 <datalist> 在暗色主题/部分浏览器不显示
    // 老板报"品牌名称所属行业都无法调用下拉" · 改用自定义下拉跟 brandName 一致
    const [industryDropOpen, setIndustryDropOpen] = useState(false);
    const industryDropRef = useRef<HTMLDivElement>(null);

    // 管理员专用：全量品牌（含归属用户，用于下拉展示和冲突检测）
    const [allClientBrands, setAllClientBrands] = useState<{ id: number; name: string; industry?: string; owner_name?: string }[]>([]);
    // 同名品牌冲突弹窗
    const [showConflictDialog, setShowConflictDialog] = useState(false);
    const [conflictBrands, setConflictBrands] = useState<{ id: number; name: string; industry?: string; owner_name?: string }[]>([]);
    // [P1-11] 疑似重复品牌（名称归一 + 相似度）。只提示，不静默改归属。
    const [similarBrands, setSimilarBrands] = useState<{ brand_id: number; name: string; industry?: string | null; city?: string | null; match: string; similarity: number }[]>([]);
    const [showSimilarDialog, setShowSimilarDialog] = useState(false);
    const similarAckRef = useRef<string>('');
    const pendingBrandIdRef = useRef<number | null>(null);
    // [P0 2026-09-01] 题单预览成功后要把面板**滚进视线**:窄屏(容器 <880px)单列时
    // 右栏沉到页面最底,CTA 在页底、面板在主列第 2 张卡里 —— 用户点完 CTA,
    // 面板出现在**屏幕上方之外**(实测 390x844:算价按钮 y=-43,视口内=false),
    // 页底零变化,于是连点 14 次。桌面两栏并排看得见,所以只在窄屏发作。
    // 🔴 修法刻意**不**给 CTA 加 sticky。
    //    ⚠️ [#125 裁定乙 2026-09-06 订正] 这里原来写着「`:1977` 的 pointer-fine 限定是故意的」——
    //       **那条限定已经整组删掉了**,行号也早就漂了。现在 CTA 在任何档、任何指针类型下都在流里,
    //       首屏由窄档重排(contents + order)唯一负责。留着旧话会让下一个人去找一个不存在的限定。
    //    (历史:v9 在触屏上钉过底栏,iOS pinch-zoom 时 sticky 不跟内容缩放,整条撤回;
    //     2026-09-06 又因为「读数有两个解释」把仅剩的 fine-pointer 那组也删了。)
    //    方向是「面板到用户那儿去」,不是「按钮钉在屏上」。
    const launchPanelRef = useRef<HTMLDivElement>(null);
    // [WORKERS=4 · P0-1] 诊断请求幂等 ID:提交时生成,成功清空;失败保留 → 重试复用(不重复冻结积分)
    const clientReqIdRef = useRef<string | null>(null);

    useEffect(() => {
        // 当前用户的客户品牌（用于下拉 fallback 和冲突对比基准）
        authFetch('/api/my-clients?page_size=200').then(r => r.json())
            .then(data => {
                const list = data?.clients || [];
                setBrandOptions(list.map((b: any) => ({ id: b.id, name: b.name || b.brand_name, industry: b.industry })));
            })
            .catch(() => {
                brandsApi.list({ limit: 200 }).then(res => {
                    const list = (res.data as any)?.data || res.data || [];
                    setBrandOptions(Array.isArray(list) ? list.map((b: any) => ({ id: b.id, name: b.name || b.brand_name, industry: b.industry })) : []);
                }).catch(() => {});
            });
    }, []);

    // 管理员：加载全量品牌（含归属用户名）用于下拉展示和冲突检测
    useEffect(() => {
        if (!isAdmin) return;
        authFetch('/api/my-clients?show_all=true&page_size=500').then(r => r.json())
            .then(data => {
                const list = data?.clients || [];
                setAllClientBrands(list.map((b: any) => ({
                    id: b.id,
                    name: b.name || b.brand_name,
                    industry: b.industry,
                    owner_name: b.owner_name,
                })));
            })
            .catch(() => {});
    }, [isAdmin]);

    // 点击外部关闭下拉(brand + industry)
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (brandDropRef.current && !brandDropRef.current.contains(e.target as Node)) {
                setBrandDropOpen(false);
                setExpandedBrandId(null);
            }
            if (industryDropRef.current && !industryDropRef.current.contains(e.target as Node)) setIndustryDropOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, []);

    // [CTO-15.23 2026-05-13] industry 自定义下拉的筛选选项(input 包含匹配)
    // [CTO-15.23 2026-05-19 老板报"客户档案能补 · 诊断时显示无匹配行业"]
    // 根因:INDUSTRIES 只 10 个粗分类(科技/金融/医疗/教育/消费品/制造/营销/电商/出海/其他)
    // 客户档案预填值"翡翠饰品"/"建筑装饰"等不在粗分类 → filteredIndustries=[] → 显"无匹配"
    // 修法:当前值非空 + 不在 INDUSTRIES 时 · 动态加入下拉首项作"使用当前自定义行业"
    //       让用户感知 input 是自由文本 + 当前预填值有效 · 不再误以为是 BUG
    const filteredIndustries = INDUSTRIES.filter(ind =>
        !formData.industry || ind.value.includes(formData.industry) || ind.label.includes(formData.industry)
    );
    const currentIndustry = (formData.industry || '').trim();
    const hasExactMatch = INDUSTRIES.some(ind => ind.value === currentIndustry || ind.label === currentIndustry);
    const showCurrentAsOption = currentIndustry && !hasExactMatch;

    // 管理员下拉显示全量品牌（含归属），普通用户只显示自己的
    const displayBrands = isAdmin && allClientBrands.length > 0 ? allClientBrands : brandOptions;
    const filteredBrands = displayBrands.filter(b =>
        !formData.brandName || (b.name || '').toLowerCase().includes((formData.brandName || '').toLowerCase())
    );

    const loadBrandData = useCallback(async (brandId: number) => {
        brandLoadRef.current.controller?.abort();
        const controller = new AbortController();
        const generation = brandLoadRef.current.generation + 1;
        brandLoadRef.current = { generation, controller };
        try {
            // [BUG-3] 在途等待,不抛错(原来会冒到 ErrorBoundary 把整页炸白)
            const authoritativeToken = await awaitConfirmedSessionToken();
            const res = await axios.get(`/api/brands/${brandId}/latest-diagnosis-params`, {
                headers: authoritativeToken ? { Authorization: `Bearer ${authoritativeToken}` } : {},
                signal: controller.signal,
            });
            if (
                controller.signal.aborted
                || generation !== brandLoadRef.current.generation
                || selectedBrandIdRef.current !== brandId
            ) return;
            const d = res.data;
            const confirmedDisplayNames = brandDisplayNamesText(d.brand_display_names);
            // [CTO-15.23 2026-05-07 P0-3 修复] client_location 在 has_profile / 无诊断分支漏写
            // 老板 E2E 复测:创建客户填城市深圳 + 还没诊断 → has_diagnosis=false / has_profile=false
            // → 走 else 分支 · setFormData 只设 industry · clientLocation 仍空
            // 后端 73762a84 已让 endpoint 返 client_location=brand.cities 默认值 · 前端漏接
            // 修法:3 分支统一接 d.client_location(prev fallback 防覆盖用户手输)
            if (d.has_diagnosis) {
                setFormData(prev => ({
                    ...prev,
                    industry: d.industry || prev.industry,
                    ownAccounts: Array.isArray(d.own_accounts) ? d.own_accounts.join('\n') : '',
                    competitors: Array.isArray(d.competitors) ? d.competitors.join('\n') : '',
                    additionalInfo: d.additional_info || '',
                    clientLocation: d.client_location || prev.clientLocation,
                    diagnosisScope: 'geo',
                    brandDisplayNames: confirmedDisplayNames,
                }));
                if ((d.own_accounts?.length > 0) || (d.competitors?.length > 0)) {
                    setShowAdvanced(true);
                }
            } else if (d.has_profile) {
                setFormData(prev => ({
                    ...prev,
                    industry: d.industry || prev.industry,
                    competitors: Array.isArray(d.competitors) ? d.competitors.join('\n') : '',
                    clientLocation: d.client_location || prev.clientLocation,
                    brandDisplayNames: confirmedDisplayNames,
                }));
            } else {
                setFormData(prev => ({
                    ...prev,
                    industry: d.industry || prev.industry,
                    clientLocation: d.client_location || prev.clientLocation,
                    brandDisplayNames: confirmedDisplayNames,
                }));
            }
        } catch (err) {
            if (!axios.isCancel(err) && !controller.signal.aborted) {
                console.warn('品牌资料加载失败', err);
            }
        } finally {
            if (brandLoadRef.current.generation === generation) {
                brandLoadRef.current.controller = null;
            }
        }
    }, []);

    const selectBrand = useCallback((b: { id: number; name: string; industry?: string }) => {
        setActiveBrandId(b.id);
        setFormData(prev => ({ ...prev, brandName: b.name, industry: b.industry || prev.industry }));
        setBrandDropOpen(false);
        setExpandedBrandId(null);
        loadBrandData(b.id);
    }, [loadBrandData, setActiveBrandId]);

    // 未保存提醒
    // 🔴 [#165 F4] 原判据是 `brandName || additionalInfo` **有没有值** ——
    //    而品牌名是**系统预填**的(选客户/沙盒自动填),于是她一进页面、
    //    一个字都没动,离开时就被浏览器原生弹框拦一下,框里还没有任何文字
    //    (beforeunload 不让自定义内容)。她根本不知道自己「没保存」了什么。
    //    改成:只有**她自己动过手**才守卫。判据从「有没有值」换成「是不是她填的」——
    //    同一个词「已填」在本页另有一处也犯这个错(见 F11 的 checklist)。
    const [userEditedForm, setUserEditedForm] = useState(false);
    const hasUnsaved = userEditedForm && !!(formData.brandName || formData.additionalInfo);
    useUnsavedWarning(hasUnsaved);

    // Stage 1 Batch 4 (2026-05-18) · 沙盒态只自动填品牌名, 其他字段留空让用户点 AI 填写
    // 教学引导链 step 1.1 = 用户看到 demo 品牌已填 → 点 AI 填写 → 其他字段一键补齐
    useEffect(() => {
        if (!isSandboxActive()) return;
        if (formData.brandName) return;
        setFormData(prev => ({
            ...prev,
            brandName: SANDBOX_DEMO_PRESET.brandName,
        }));
    }, [formData.brandName]); // eslint-disable-line react-hooks/exhaustive-deps

    // 🆕 BUG-P0-4 修复 (CTO-15.22 2026-05-03):自动 prefill 全局 selected 客户档案
    // [P1-2 fix 2026-05-23 v2] sidebar 切换品牌后表单也要联动更新
    // 老板报:在 /diagnosis/new 用 sidebar 切到另一个品牌 · 表单仍停留在前一个品牌
    // 修法:同时跟踪 brand_id + form snapshot · sidebar 切换时
    //      - 表单仍 = 上次 prefill 的 snapshot → 用户没手编 → 重新 prefill 新品牌
    //      - 表单 ≠ snapshot → 用户已手编 → 不覆盖(防 step 4-7 类用户输入丢失)
    const prefilledBrandIdRef = useRef<number | null>(null);
    const prefilledSnapshotRef = useRef<{
        brandName: string;
        industry: string;
        clientLocation: string;
        businessScope: string;
        brandDisplayNames: string;
    } | null>(null);
    useEffect(() => {
        if (!clientContext || !currentBrandId) return;
        if (prefilledBrandIdRef.current === currentBrandId) return;
        // 首次 prefill:表单非空 → 用户已自己填,不覆盖
        if (prefilledBrandIdRef.current === null) {
            if (formData.brandName || formData.industry || formData.clientLocation) return;
        } else {
            // 后续 sidebar 切换:对比 snapshot · 任何字段被改 → 用户编辑过 → 不覆盖
            const snap = prefilledSnapshotRef.current;
            if (snap) {
                const userEdited = formData.brandName !== snap.brandName
                    || formData.industry !== snap.industry
                    || formData.clientLocation !== snap.clientLocation
                    || formData.businessScope !== snap.businessScope
                    || formData.brandDisplayNames !== snap.brandDisplayNames;
                if (userEdited) return;
            }
        }
        const brand = clientContext.brand;
        const profile = (clientContext.profile || {}) as Record<string, unknown>;
        // [CTO-15.23 2026-05-07 P0-3 修复] city fallback 加 brand.cities
        const city = (profile.city as string)
            || (profile.city_name as string)
            || brand.cities
            || '';
        const newBrandName = brand.name || formData.brandName;
        /* [WO_267] 新写入的大类是英文 key —— 预填进自由文本框前翻成中文名;认不出就不填 */
        const newIndustry = brand.industry
            || industryCategoryText(brand.industry_category, brand.industry_category_name, industryNameOf)
            || formData.industry;
        const newCity = city || formData.clientLocation;
        // [P0-4] 业务范围从客户档案带出（client_profiles.service_scope / city_scope）；
        //   档案里没填就保持默认「区域」—— 多数代理客户是本地生意。
        const storedScope = String(
            (profile.service_scope as string) || (profile.city_scope as string) || '',
        ).toLowerCase();
        const newScope = storedScope === 'national' ? 'national' : 'regional';
        const newDisplayNames = brandDisplayNamesText(
            (brand as Record<string, unknown>).brand_display_names
                ?? profile.brand_display_names,
        );
        setFormData(prev => ({
            ...prev,
            brandName: newBrandName,
            industry: newIndustry,
            clientLocation: newCity,
            businessScope: newScope,
            brandDisplayNames: newDisplayNames,
        }));
        setActiveBrandId(brand.id);
        prefilledBrandIdRef.current = currentBrandId;
        prefilledSnapshotRef.current = {
            brandName: newBrandName,
            industry: newIndustry,
            clientLocation: newCity,
            businessScope: newScope,
            brandDisplayNames: newDisplayNames,
        };
    }, [currentBrandId, clientContext]); // eslint-disable-line react-hooks/exhaustive-deps

    // 品牌列表加载完后,如果已有品牌名(复测/URL预填)则自动匹配 selectedBrandId
    // BUG-P0-5(2026-05-03 CTO-15.22):精确匹配 → 模糊(双向 includes)防代理输入简称撞不上全名
    // [CTO-15.23 2026-05-05] P0 修反向 fuzzy 砍头 bug:
    //   原 input.includes(name) 把"深圳怡然瑜伽"撞到旧 brand 173('瑜伽')→ 自动 select brand_id=173
    //   → 诊断关联到错品牌 · 报告显示"瑜伽"
    //   砍掉反向方向 · 只保留 name.includes(input)(全名包含简称 · 真正解决万嘉澜 case)
    //   "佛山市顺德区万嘉澜".includes("万嘉澜")=TRUE ✓
    //   "深圳怡然瑜伽".includes("瑜伽")=TRUE 但属反向 · 已删
    useEffect(() => {
        if (formData.brandName && !selectedBrandId && brandOptions.length > 0) {
            const input = formData.brandName.trim().toLowerCase();
            if (input.length < 2) return;
            // 1) 精确匹配优先(忽略大小写)
            let matched = brandOptions.find(b => (b.name || '').toLowerCase() === input);
            // 2) 单向模糊匹配:仅"全名包含用户输入"·单一候选才自动 select
            if (!matched) {
                const candidates = brandOptions.filter(b => {
                    const name = (b.name || '').toLowerCase();
                    return name.length >= 2 && name.includes(input);
                });
                if (candidates.length === 1) matched = candidates[0];
            }
            if (matched) setActiveBrandId(matched.id);
        }
    }, [brandOptions, formData.brandName, selectedBrandId]);

    // 复测模式：自动获取原诊断的完整数据
    useEffect(() => {
        if (isRetest && originalId) {
            setLoadingRetest(true);
            diagnosisApi.getRetestData(Number(originalId))
                .then((res) => {
                    const data = res.data as {
                        brand_name?: string;
                        industry?: string;
                        keywords?: string[];
                        own_accounts?: string[];
                        competitors?: string[];
                        additional_info?: string;
                        error?: string;
                    };
                    if (data && !('error' in data)) {
                        const d = data as any;
                        setFormData(prev => ({
                            ...prev,
                            brandName: d.brand_name || prev.brandName,
                            industry: d.industry || prev.industry,
                            // 预填高级选项
                            ownAccounts: Array.isArray(d.own_accounts) ? d.own_accounts.join('\n') : '',
                            competitors: Array.isArray(d.competitors) ? d.competitors.join('\n') : '',
                            additionalInfo: d.additional_info || '',
                            clientLocation: d.client_location || '',
                            brandDisplayNames: brandDisplayNamesText(d.brand_display_names),
                            diagnosisScope: 'geo',
                        }));
                        // 复测时自动关联已有品牌(BUG-P0-5 同样模糊匹配)
                        // [CTO-15.23 2026-05-05] 砍反向 fuzzy 防"深圳怡然瑜伽"撞 brand 173 ('瑜伽')
                        if (d.brand_name) {
                            const input = d.brand_name.trim().toLowerCase();
                            let matched = brandOptions.find(b => (b.name || '').toLowerCase() === input);
                            if (!matched && input.length >= 2) {
                                const candidates = brandOptions.filter(b => {
                                    const name = (b.name || '').toLowerCase();
                                    return name.length >= 2 && name.includes(input);
                                });
                                if (candidates.length === 1) matched = candidates[0];
                            }
                            if (matched) setActiveBrandId(matched.id);
                        }
                        // 如果有高级选项数据，自动展开
                        if ((data.own_accounts && data.own_accounts.length > 0) ||
                            (data.competitors && data.competitors.length > 0)) {
                            setShowAdvanced(true);
                        }
                    }
                })
                .catch((err) => {
                    console.error('获取复测数据失败:', err);
                })
                .finally(() => {
                    setLoadingRetest(false);
                });
        }
    }, [isRetest, originalId]);

    // Agent 填充事件监听
    useEffect(() => {
        const handler = (e: Event) => {
            const fields = (e as CustomEvent).detail;
            if (!fields || typeof fields !== 'object') return;
            setFormData(prev => {
                const updated = { ...prev };
                if (fields.brandName) updated.brandName = fields.brandName;
                if (fields.industry) updated.industry = fields.industry;
                if (fields.competitors) { updated.competitors = fields.competitors; setShowAdvanced(true); }
                if (fields.clientLocation) { updated.clientLocation = fields.clientLocation; setShowAdvanced(true); }
                if (fields.additionalInfo) updated.additionalInfo = fields.additionalInfo;
                return updated;
            });
        };
        window.addEventListener('agent-fill', handler);
        return () => window.removeEventListener('agent-fill', handler);
    }, []);

    const handleAutofill = async () => {
        if (!formData.brandName.trim()) return;
        setAutofilling(true);
        setAutofillError(null);
        setAutofillStatus("");

        const brandName = formData.brandName.trim();

        // 逐步尝试每个 provider，实时更新状态
        const providers = [
            { key: "doubao", label: "正在搜索品牌信息..." },
            { key: "dashscope", label: "正在搜索品牌信息..." },
            { key: "kimi", label: "正在搜索品牌信息..." },
        ];

        // 辅助：填充表单
        const applyData = (data: any) => {
            setFormData(prev => ({
                ...prev,
                industry: data.industry || prev.industry,
                clientLocation: data.clientLocation || prev.clientLocation,
                additionalInfo: data.additionalInfo || prev.additionalInfo,
                competitors: data.competitors?.length ? data.competitors.join('\n') : prev.competitors,
            }));
            if (data.competitors?.length) setShowAdvanced(true);
        };

        for (let i = 0; i < providers.length; i++) {
            const { key, label } = providers[i];
            setAutofillStatus(label);
            try {
                const res = await diagnosisApi.autofill(
                    brandName,
                    key,
                    formData.clientLocation,  // 强约束：用户已填的城市
                    formData.industry,        // 强约束：用户已填的行业
                );
                const data = res.data?.data;
                if (!data) continue;
                const hasContent = data.industry || data.keywords?.length || data.clientLocation || data.additionalInfo || data.competitors?.length;
                if (!hasContent) {
                    console.warn(`[Autofill] ${key} returned empty data, trying next`);
                    continue;
                }
                // 成功
                applyData(data);
                setAutofillStatus("");
                setAutofilling(false);
                // Stage 1 Batch 4 (2026-05-18) · 沙盒里 AI 填写成功后切到 'submit' 阶段引导开始诊断
                if (isSandboxActive()) {
                    toast.success('信息已补全 ✓ · 接下来点"开始品牌体检"', { duration: 4000 });
                    setTutorialStage('submit');
                }
                return;
            } catch (err: any) {
                console.warn(`[Autofill] ${key} failed:`, err.response?.data?.detail || err.message);
                // 继续下一个 provider
            }
        }

        // 全部失败
        setAutofillStatus("");
        setAutofilling(false);
        setAutofillError("所有AI模型均未能搜索到该企业信息，请手动填写");
    };

    // 价格只来自 DB feature_pricing；读取失败时冻结动作，绝不以旧常量替代。
    const { getTrustedCost, loading: pricingLoading, error: pricingError } = usePricing();
    const geoCost = getTrustedCost('geo_diagnosis');
    const diagnosisCostMap: Record<'geo', { code: string; name: string; cost: number | null }> = {
        geo: { code: 'geo_diagnosis', name: 'GEO专项诊断', cost: geoCost },
    };
    const currentDiagnosisCost = diagnosisCostMap.geo;

    // [CTO-15.23 2026-05-21] 自定义诊断问题加价计算(口径必须与 server.py validator 对齐)
    // 真扣费走后端 freeze_points 单一入口 · 前端只算总价用于余额预检 + 明细显示
    // P2 修一致性:后端 validator 会 dedup + 抛 100 字超长 · 前端必须 mirror 同样 logic
    // 否则用户填重复行前端多算钱、超长行被静默过滤都会造成困惑
    /**
     * 🔴 [⑤b · 订正二十四] **唯一题源** —— 她自己出的题只有一个地方:题单编辑器。
     *
     * 改前有两个互不相通的管道:编辑器的 manualQuestions(只走防御链、**不进提交体**)
     * 与高级选项那个 Textarea(走 legacy 提交体)。于是她在**默认线**上出的题
     * 静默不跑也不计价。本笔把编辑器变成唯一入口,那个 Textarea 删。
     *
     * 🔴 下面这一个变量被**三处**读:首次取价 / 409 重取 / 提交体 custom_questions。
     *    必须是**同一个变量**,不是三次「应该等价」的调用 —— 后端拿 custom_questions
     *    去比 pricePreviewId 的哈希,分家的后果要么每次 409,要么按 A 算价按 B 扣钱。
     */
    /**
     * 🔴 [#144 2026-09-07] 「客户可能会这样问」的**候选题**。
     *
     * 后端 `POST /api/diagnosis/suggest-questions`(server.py:4231)2026-09-05 就在,
     * **全仓零调用方** —— 新页只给本地模板,于是 Owner 的原话是
     * 「老版本能蒸馏,新版不会」。这里把它接上。
     *
     * 🔴 只在**题单still空**时预填:她已经写了东西就绝不覆盖。
     * 🔴 每个(品牌 × 模式)只取一次 —— 用 ref 记,不然 effect 会自己喂自己
     *    (预填 ⇒ manualQuestions 变 ⇒ effect 重跑)。
     * 🔴 防守模式**不调**(`toSuggestMode` 返 null):它有品牌名模板,工单要求保留。
     */
    /**
     * 🔴 [#149] 后端出的候选题 —— 它们**就是这次会问的题**,不再是「示例」。
     *
     * 09-07 我把它们降级成示例卡,理由是「预览题与实跑题输入不同源」(#147)。
     * **那个前提已经没了**:#147-B(`13865e440`,0907c)把两侧输入合成同一份。
     * Owner 的原话是「为什么非要在下面添加个示例,直接填充到上面这个框不行吗」。
     * 现在它们进题单的 AI 那一半,和她自己写的题一起构成**这次的考卷**。
     */
    const [aiSuggested, setAiSuggested] = useState<DraftQuestion[]>([]);
    const [suggestPending, setSuggestPending] = useState(false);
    const [suggestNotice, setSuggestNotice] = useState<string | null>(null);
    const suggestTriedRef = useRef<Set<string>>(new Set());
    useEffect(() => {
        const sMode = toSuggestMode(campaignMode);
        if (!sMode) return;                       // 防守模式:保留模板,不调接口
        const bid = selectedBrandId;
        const key = `${bid ?? 'none'}:${sMode}`;
        if (suggestTriedRef.current.has(key)) return;
        suggestTriedRef.current.add(key);
        const ac = new AbortController();
        setSuggestPending(true);
        void (async () => {
            const r = await fetchSuggestedQuestions(authFetch, {
                brandId: bid, mode: sMode,
                businessScope: formData.businessScope, signal: ac.signal,
            });
            if (ac.signal.aborted) return;
            setSuggestPending(false);
            if (!r.ok) { setAiSuggested([]); setSuggestNotice(r.error.message || null); return; }
            setSuggestNotice(null);
            /**
             * 🔴 侧别按**顺序**切,这是一条对后端实现的假设:
             *    `server.py` 全面模式返的是 `g_qs[:4] + d_qs[:4]`,响应体**不带侧别标签**。
             *    顺序一旦变,这里会把增长题标成防守题 —— 屏幕上看起来完全正常。
             *    已报 Review:响应应当带 side 字段。在那之前这里保守处理:
             *    只有**恰好 8 条**才对半切,其余一律按当前模式的默认侧别走,
             *    宁可全归一侧(她可以逐条改),也不按一个不成立的假设去猜。
             */
            const defSide: 'defensive' | 'offensive' =
                campaignMode === 'offensive' ? 'offensive' : 'defensive';
            setAiSuggested(r.data.candidates.map((c) => {
                // 侧别**用后端给的**(已在 suggestQuestionsApi 里映射成前端词表);
                // 后端没给或给了不认识的值才落到当前模式默认侧。
                const side = c.side ?? defSide;
                return {
                    text: c.text, modeSide: side, ...sideDefaults(side),
                    source: 'ai' as const,
                    ...(c.layer ? { layer: c.layer } : {}),
                };
            }));
        })();
        return () => { ac.abort(); };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [campaignMode, selectedBrandId]);
    /** 🔴 品牌/模式一变,上一份候选立刻作废 —— 留着会把**另一个品牌**的题显示成"系统将问"。 */
    useEffect(() => { setAiSuggested([]); setSuggestNotice(null); }, [campaignMode, selectedBrandId]);

    /**
     * 🔴 [#149] **题单整体 = 这次的运行集**(增长线)。
     *
     * 改前 `ownQuestions` 只收 manual,AI 那半单独走 `suggestedWithFiller`;
     * 现在 AI 出的题直接进题单,所以「显示的 = 计价的 = 提交的 = 实跑的」
     * 必须落在**同一个变量**上,否则又回到「屏幕上 N 道、按 M 道收钱」。
     *
     * 🔴 防守/混合线**逐字节不动**:它在 `handleSubmit` 早返回走
     *    `runDefensivePlanPreview`,根本不构造这个提交体;它的建议题是本地模板,
     *    另有一套 `suggestedWithFiller` / 补位逻辑。把它并进来会双计。
     */
    const growthSuggested: DraftQuestion[] = isDefensiveFlow
        ? []
        : aiSuggested.filter((q) => !removedSuggested.has(removalKey(q)));
    /** 送进归一化的那一份 —— 三处消费点(取价 / 计数 / 提交体)都读它派生出的 `ownQuestions`。 */
    const planQuestions: DraftQuestion[] = isDefensiveFlow
        ? manualQuestions
        // 🔴 增长线走 `growthRunSet` —— **编辑器显示的也走它**(见 suggested/manual 两个 prop),
        //    两侧同一个构造器,「屏幕上 N 道、按 M 道收钱」不再是靠纪律避开的。
        : growthRunSet(growthSuggested, manualQuestions);
    const ownQuestions = collectOwnQuestions(planQuestions);
    const customQuestionCount = ownQuestions.unique.length;
    /**
     * [阶段⑤] 服务端价预览。**唯一的价**,同时给出绑住提交的 pricePreviewId。
     *
     * 🔴 题集/开关/scope 任一变 ⇒ 旧价立即作废(pricePreview 置 null)并重取。
     *    不这么做的话,新价回来之前按钮上挂着的是**上一份输入的价** ——
     *    她看到的与她将要被扣的不是一回事(与 #62 同形)。
     * 🔴 取不到价 ⇒ 不显示任何数字(同 #64:宁可少说,不可说错),按钮同时被闸拦住。
     */
    const [pricePreview, setPricePreview] = useState<DiagnosisPricePreview | null>(null);
    const [pricePending, setPricePending] = useState(false);
    const [priceError, setPriceError] = useState<string | null>(null);
    const priceInputKey = JSON.stringify([ownQuestions.unique, !!formData.aiOptimizeCustom]);
    useEffect(() => {
        const ac = new AbortController();
        // 输入过程防抖;开关/scope 那类离散变化也走同一条路径(裁定要求一变必重取)。
        setPricePreview(null);
        setPriceError(null);
        setPricePending(true);
        const t = setTimeout(() => {
            void (async () => {
                const r = await fetchDiagnosisPrice({
                    questions: ownQuestions.unique,
                    aiOptimizeCustom: !!formData.aiOptimizeCustom,
                    scope: 'geo',
                    mode: campaignMode,
                    signal: ac.signal,
                });
                if (ac.signal.aborted) return;
                setPricePending(false);
                if (r.ok) setPricePreview(r.data);
                else if (r.error.message) setPriceError(r.error.message);
            })();
        }, 400);
        return () => { ac.abort(); clearTimeout(t); };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [priceInputKey, campaignMode]);
    /** 价还没回来 / 回来的那份对不上当前题数 ⇒ 不许确认(继承 launch-confirm-blocked-stale 语义)。 */
    const priceNotReady = pricePending || !priceMatchesInput(pricePreview, customQuestionCount);
    /**
     * 🔴 [阶段⑤ · 订正五① / 订正二十一] 前端算价的**镜子已删**。
     *
     * 原来这里有一份浏览器端预估(mirror server.py validator),自陈「仅用于余额预检与
     * freeze hold 展示」。但它是**同一条计价规则的第二份** —— 订正十二一改规则,
     * 两处必漂一处,而**漂开那天不会有任何判据变红**(两个数各自都「看起来对」)。
     *
     * 现在唯一的价来自服务端 POST /api/pricing/diagnosis-preview,它同时给出绑住提交的
     * pricePreviewId ⇒ **显价与扣费出自同一个响应体**,「按钮上的价为唯一价」
     * (订正七④)成为结构性的,而不是靠纪律。
     * 🔴 不用 GET:GET 恒 ai_optimized=False,双轨开时显 650 而 POST 绑 950。
     */
    const totalDiagnosisCost = pricePreview?.points ?? null;

    // [2026-06-16 客户报障] 必填完整性(trim · 与下方"启动检查"清单口径一致):品牌名 + 行业 + 至少 1 个关键词。
    // 防"只填品牌简称就诊断→结果拉垮→以为系统不行"。原启动按钮 disabled 用非 trim 原值有空格洞 + 无兜底校验。
    // [diaglaunch 2026-08-05] 主输入的心智从"检测关键词"改成"核心搜索问题"(工单 §1.3),
    //   提交字段仍是 keywords —— 映射一个字没动。但心智一改,用户会打整句问句进来,
    //   而 server.py `validate_keywords` 对 >50 字 / >20 条是**抛错**(422)不是截断。
    //   所以这里把同一套口径镜像到浏览器端,超了当场说清楚并拦住提交,
    //   🔴 不做静默过滤 —— 偷偷丢行的话用户看到的条数和真正提交的对不上,他永远不知道题被吃了。
    /**
     * 🔴 [⑤b] 这几个量原来挂在 `formData.keywords`(旧的核心搜索问题框)上。
     *    那个框已删、字段恒空 ⇒ 继续挂着的话 `questionsValid` 恒 false、
     *    `isFormComplete` 恒 false、**按钮永久灰着**,而屏幕上说的是「填完就能开始」。
     *    全部改挂唯一题源(题单编辑器)。
     *
     * 🔴 **空题集是合法的**(订正十二:她不出题就跑 AI 出的 8 道)——
     *    所以这里**没有** `>= 1` 这一项。加回去就等于把「不出题」这条主路径堵死。
     */
    const questionCount = ownQuestions.unique.length;
    const questionsValid = ownQuestions.tooLong.length === 0;
    /** 搜索词与题是**两个概念、两套上限**:题 100 字走 custom_questions,词 50 字走 keywords。 */
    const searchTerms = collectSearchTerms(searchTermsRaw);

    /**
     * 🔴 [订正三十①] 预填取数。挂在 activeBrandId 上 —— 词是**关于某个品牌**的。
     *
     * 三条不变量,判据分别钉:
     *  ① 回填只在 `shouldAcceptRefill(dirty)` 为真时发生(晚到的响应不许冲掉她的字);
     *  ② 换品牌一律重取 + dirty 归零 + 出一句说明(替换但出声,订正三十补①);
     *  ③ 失败翻 `unavailable`,**不显示半个结果**。
     */
    useEffect(() => {
        const prev = prevBrandRef.current;
        const switched = prev.id !== null && prev.id !== selectedBrandId;
        if (switched) {
            setBrandSwitchMsg(brandSwitchNotice(prev.name, formData.brandName, searchTermsDirty));
            setSearchTermsDirty(false);
            setSearchTermsRaw('');
        }
        prevBrandRef.current = { id: selectedBrandId, name: formData.brandName };
        if (!selectedBrandId) { setPrefill({ kind: 'unavailable' }); return; }
        const ac = new AbortController();
        setPrefill({ kind: 'loading' });
        fetchSearchTermSuggestion(selectedBrandId, ac.signal).then((st) => {
            setPrefill(st);
            // ① 她没动过才回填。动过就以她的为准(Owner:按客户改动作为标准)。
            if (st.kind === 'ready' && shouldAcceptRefill(searchTermsDirty)) {
                setSearchTermsRaw(st.terms.join(String.fromCharCode(10)));
            }
        });
        return () => ac.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [selectedBrandId]);

    const searchTermsEmptyState = emptyStateNotice(prefill);
    const searchTermsSubmit = keywordsForSubmit(prefill, searchTermsDirty, searchTerms.terms);
    const isFormComplete = !!formData.brandName.trim() && !!formData.industry.trim() && questionsValid;

    // ── [defgeo 装配] 防御题单派生 ────────────────────────────────────
    // 🔴 [订正二十五] `formData.keywords` **这个字段已经不存在了** —— 前端一个字都不发,
    //    服务端从品牌名派生。所以这里不是"写不写回"的问题,是根本没有可写的地方。
    // 🔴 品牌或模式一变,上一次的计价规则就不一定还适用 ⇒ 清掉。
    //    留着旧的比不显示更糟:她会看着**另一个品牌**的起步价做决定。
    useEffect(() => { setPricingRule(null); }, [campaignMode, formData.brandName]);

    /**
     * 🔴 [#144 · C #147 取证订正] 增长模式这里**保持空**。
     *    候选**不能**放进这一半:C 的取证显示 suggest-questions 的候选与正式体检
     *    实际问的题**必然不同**(预览输入更少 + 随机温度 + 两套缓存)。
     *    放进「系统将问」那一栏 = 屏幕上宣称这就是要问的题,而它不是。
     *    候选改走**示例卡**(它本来就有一键加入)—— 加进去之后它才真的会被问。
     */
    /**
     * 🔴 [#149] 增长模式这一半 = 后端出的候选题(不再恒空)。
     *
     * 09-07 这里写着「候选不能放进这一半」,理由是预览题与实跑题输入不同源(#147)。
     * **那条理由在 `13865e440`(#147-B,0907c)之后不再成立** —— 两侧输入已合成同一份。
     * 现在整张题单就是这次的考卷:显示的 = 计价的 = 提交的 = 实跑的。
     * 后端据 `question_meta.origin` 分辨哪几道是 AI 出的(工作流 §2.3 的承重墙:
     * 品牌定向豁免只对 customer 生效,否则 AI 出的品牌题会顶高提及率)。
     */
    const suggestedDefensive = isDefensiveFlow
        ? suggestedQuestions(campaignMode, formData.brandName, { industry: formData.industry, location: formData.clientLocation, businessScope: formData.businessScope }).filter(
            (q) => !removedSuggested.has(removalKey(q)))
        // 🔴 增长线直接复用上面那一份 —— **不在这里再过滤一次**。
        //    同一个「删掉哪些」的谓词写两处,必有一处漏改,而漏改那处会让
        //    屏幕显示的题数与计价用的题数分家(两处各自看起来都对)。
        : growthSuggested;
    /**
     * 🔴 [⑤b 返修] **关键词迁移整条拆掉**(原 keywordsMigrationOn / migratedFromKeywords)。
     *
     * 它是旧关键词框还是题输入位时搭的桥。keywords 一路喂 `batch_collect_all` →
     * 抖音/小红书搜索框,与「题」是两个东西(C 的证据)。订正二十五更进一步:
     * `formData.keywords` **整个字段删掉** —— 前端不持有、不发送,服务端从品牌名派生。
     *
     * 拆掉同时消掉两个可达缺陷:
     *  ① 迁移项在编辑器里可编辑、镜像的是**题**的 100 字上限,却写回**关键词**字段
     *     (`validate_keywords` 上限 50)⇒ 她在一个叫「问题」的框里打 60 字,
     *     收到一条说「关键词」的 422。(C 在接缝上抓到,两边判据都照不到:
     *      我的跨层锁比的是 100≡100 恒绿,他的判据不看被写回的 keywords。)
     *  ② 我自己引入的:`ownQuestionCount` 数「迁移+手动」而 `customQuestionCount` 只数手动
     *     ⇒ hybrid 下有迁移词时,**AI 题被折叠标「不跑」,而价按 0 道题算**。
     *
     * 现在题只有一个来源(manualQuestions),两个计数合一,keywords 不再被编辑器碰。
     */
    const rawDefensiveDraft: DraftQuestion[] = [...suggestedDefensive, ...manualQuestions];
    /**
     * 🔴 [阶段④ · 订正八①] 原来这里有「单侧混进另一侧 ⇒ 就地自动改混合」的收敛
     *    (intrudingSide + effect 里 setCampaignMode('hybrid') + 两颗撤销按钮)。
     *    三标签下模式由用户点选,「系统替你切模式」没有存在理由 —— 整套退役。
     *    侧别不符改为**不制造**:迁移只发生在 hybrid(见上),
     *    她自己加的题在 hybrid 下必须选侧(题单里每题带侧别)。
     *    服务端侧别守卫**保留**(typed 409 仍在),只是前端不再自动收敛。
     */
    /**
     * 🔴 [WO 2026-09-02 §3.1.5] **缺侧当场补位**,而不是让后端报一句通用错。
     *
     * hybrid 下后端要求两侧都有题(`question_plan.py` 的身份/计价安全线,不动它)。
     * 但用户可能什么都没做错就落到单侧 —— Deploy 差分复现指出:系统题与核心搜索问题
     * 全落 defensive 侧,选 hybrid 就必炸。**报错的是系统,做错事的不是用户。**
     *
     * 所以在**送出之前**把缺的那一侧补回一道系统建议题:错误根本不发生(:76 不中断),
     * 比"报错 + 给个修复按钮"更早一层。补位是**派生**的,不写回 state ——
     * 写回会和 `removedSuggested` 打架,而且用户下次删除时又要重新推理一遍。
     */
    const sideFiller: DraftQuestion[] = (() => {
        if (campaignMode !== 'hybrid') return [];
        const counts = sideCounts(rawDefensiveDraft);
        const missing = (['defensive', 'offensive'] as const).filter((s) => counts[s] === 0);
        if (missing.length === 0) return [];
        const pool = suggestedQuestions('hybrid', formData.brandName, { industry: formData.industry, location: formData.clientLocation, businessScope: formData.businessScope });
        return missing
            .map((s) => pool.find((q) => q.modeSide === s))
            .filter((q): q is DraftQuestion => !!q)
            .map((q) => ({ ...q, isFiller: true }));
    })();
    /**
     * 🔴 补位题**必须与渲染同源**。
     *
     * 我第一版把补位只加进 `defensiveDraft`(送出与计价用),而编辑器渲染的是
     * `suggestedDefensive` —— 于是屏幕上看不见那道题,它却进了题数与价格。
     * 那正是「客户端与服务端题数不一致」的雏形:**用户看不见的题不能进计价**,
     * 比报错更糟(她按 8 题算价,系统按 9 题收)。
     * 所以补位并进 `suggested`,列表里看得见、可删可改,题数与价格自然一致。
     */
    const suggestedWithFiller: DraftQuestion[] = [...suggestedDefensive, ...sideFiller];
    /** 编辑器里「她自己的」那半 —— 迁移拆掉后它就是 manualQuestions 本身。 */
    const editableQuestions: DraftQuestion[] = manualQuestions;
    /**
     * [阶段③ · 订正十二] 她出了自己的题 ⇒ **只跑她的**;开了双轨才两边都跑。
     * 🔴 三分支只在 `questionRunPlan` 里判一次 —— 题单怎么显示、按钮多少钱、
     *    最终真跑什么,三处必须同源,否则屏幕上三处各自看起来都对而彼此不同。
     */
    /**
     * 🔴 [⑤b 返修] 与 `customQuestionCount` **合并成同一个数**。
     *
     * 改前两者不同源:这个数「非空即算」,那个数是去重 + 去超长之后的。
     * 于是同一屏上 `aiMuted`(用这个)与价、开关(用那个)可能对不上 ——
     * 极端情况:她写了两道重复的题 ⇒ AI 题被折叠标「不跑」,而价按 1 道算。
     * 「她自己出了几道题」这件事在一个组件里只该有一个答案。
     */
    const ownQuestionCount = ownQuestions.unique.length;
    const alsoRunAi = !!formData.aiOptimizeCustom;
    /**
     * 🔴 [#149] 增长线**不折叠、不静音**:AI 出的题已经在题单里,
     *    「AI 那批要不要跑」这个三分支在增长线上没有对象了。
     *    折叠/静音与双轨开关都只留给防守/混合线。
     */
    const aiMuted = isDefensiveFlow && aiQuestionsMuted(ownQuestionCount, alsoRunAi);
    const effectiveRunCount = isDefensiveFlow
        ? effectiveQuestionCount(suggestedWithFiller.length, ownQuestionCount, alsoRunAi)
        // 增长线:题单整体即运行集。`ownQuestionCount` 已经包含 AI 那半(见 planQuestions),
        // 再把 suggested 加一遍就是双计。
        : ownQuestionCount;
    const defensiveDraft: DraftQuestion[] = [...suggestedWithFiller, ...editableQuestions];

    /**
     * 🔴 [WO §3.2 第三态 · Review 2026-09-04 裁 (i)]
     *
     * 两种情况分开,**不要合成一个"题单变了"**:
     *   · **题数 = 0** ⇒ 0 道题本来就没有合法 plan ⇒ **清 plan**,回 idle 分支显示
     *     「已有 0 道问题 + 缓存的起步价规则」。这是 Owner 的原话,也避免她读到一个
     *     **按 4 道题算出来、与当前题单无关**的置灰旧价 —— 置灰不足以消除数字的暗示力。
     *   · **题数 > 0 且签名变了** ⇒ 价还在但对不上了 ⇒ 标**过期**(置灰 + 一行 + 按钮),
     *     **不自动重算**(打后端会撞限流,也会放大"看到的那版≠确认的那版")。
     */
    const liveQuestionCount = defensiveDraft.filter((q) => (q.text || '').trim()).length;
    const priceIsStale = !!questionPlan && !!pricedSignature && liveQuestionCount > 0
        && questionSetSignature(defensiveDraft) !== pricedSignature;

    useEffect(() => {
        // 题数掉到 0:清 plan(不清 pricingRule —— 缓存的**规则**仍要显示)
        if (isDefensiveFlow && liveQuestionCount === 0 && questionPlan) {
            setQuestionPlan(null);
            setPricedSignature(null);
        }
    }, [isDefensiveFlow, liveQuestionCount, questionPlan]);

    // ── [G2 门二返修 2026-08-22] 启动按钮的可用性判定 ──────────────────
    //
    // 🔴 **不改 `isFormComplete`**:它同时是 `proceedWithDeductAndSubmit` 里的
    //    兜底硬校验(防 Enter 提交 / hydration fallback 绕过 disabled),
    //    而那条路径是 **legacy 专用**。动它 = 动 legacy 行为。
    //    所以这里新增一个**只给按钮用**的判定,legacy 分支原样取 isFormComplete。
    //
    // 防御/混合模式下 legacy 的「核心搜索问题」Textarea 不是必填 ——
    // 她填的是上面的题单编辑器;再要求她把同样的问题在下面再抄一遍,
    // 是把两套输入的债务(CUR-06)又还给用户。
    // 🔴 判定本体**只在** defensiveLaunchGate 里存在一份。
    //    这里一度还留了个等价的 `defensiveReady` —— 同一个谓词写两处,
    //    必有一处没人验(改了 gate 忘了改它,判据照样绿)。已删。
    const defensiveBrandId = pendingBrandIdRef.current ?? selectedBrandId;
    /**
     * 启动按钮被拦住的完整条件。**只此一处定义**,`disabled` 与
     * hydration fallback 的 `onClick` 早退都用它 —— 两处各写一遍迟早会漂,
     * 而漂的后果是"按钮看着能点、点了没反应"(本页 2026-05-23 正踩过这个)。
     *
     * 逻辑本体在无 React 依赖的 `launch/defensiveLaunchGate.ts`,
     * 好让判据能直接 import 真跑(读源码串证明不了分支真的会那样走)。
     */
    const launchBlocked = isLaunchBlocked({
        mode: campaignMode,
        isFormComplete,
        totalDiagnosisCost: totalDiagnosisCost ?? null,
        defensiveQuestionCount: defensiveDraft.filter((q) => q.text.trim().length > 0).length,
        hasBrandId: !!defensiveBrandId,
        // 🔴 [P0 2026-09-01] `planPending` 必须进这一格。
        //    原来它只挡 QuestionPlanEditor,按钮自己只看 `loading` ——
        //    于是题单预览在飞的 9 秒里按钮仍可点,生产实测**连点 14 次**
        //    (幂等去重后只落 2 行,但每点都真发一次 preview)。
        //    她连点不是因为手快,是因为屏幕上没有任何"正在处理"的迹象。
        loading,
        planPending,
        testNameUnacknowledged,
        // [阶段⑤] 价没回来 / 回来的对不上当前题数 ⇒ 不许确认。
        priceNotReady,
        searchTermsInvalid: !searchTerms.valid || (searchTermsEmptyState.mustFill && searchTerms.terms.length === 0),
    });

    /**
     * 「为什么点不了」的人话。🔴 [P0 2026-09-01] 灰按钮 + 零文案是本次 P0 的一半:
     * 实测被拦那一屏比能点那一屏**只多出**品牌下拉那句「无匹配品牌 · 继续输入将创建新品牌」
     * ——屏幕上没有任何一个字解释按钮为什么是灰的,而那句话还**反向承诺**了会建档。
     * 侧栏「启动检查」说「品牌名称 已填」、按钮却灰,两个谓词各说一半,用户只能瞎猜。
     * 顺序 = 拦截判定的顺序,别让她修完一个又冒出下一个。
     */
    const launchBlockedReason = ((): string | null => {
        if (!launchBlocked) return null;
        if (loading) return '正在启动,请稍等…';
        if (planPending) return '正在生成题单,请稍等…';
        if (testNameUnacknowledged) return '这个名字会被当作测试客户 · 先在上面选「仍要用」或换个名字';
        if (priceError) return priceError;
        if (priceNotReady) return '正在按你现在的题单重新算价,算完就能开始';
        // 🔴 [订正二十九] 顺序必须与 gate 的判定顺序一致 —— 不然按钮说的原因
        //    和真正拦住她的那一条不是同一条,她会去改一个改了也没用的地方。
        if (!searchTerms.valid) {
            return searchTerms.overflow
                ? `高级里的搜索词有 ${searchTerms.terms.length} 条,最多 ${SEARCH_TERMS_MAX_COUNT} 条 · 删几条就能开始`
                : `高级里有 ${searchTerms.tooLong.length} 条搜索词超过 ${SEARCH_TERM_MAX_CHARS} 字 · 整句话请填到题单里`;
        }
        if (isDefensiveFlow) {
            return '先加一道客户会问的问题,再开始体检';
        }
        /* 🔴 [#143④ 撤回订正] 上一版我把这句说明当成假承诺删掉了 —— **它是真的**。
           `diagnosis_workflow.py:881`:custom 为空 ⇒ 总是走 system 题(蒸馏 + 硬编码兜底)。
           说明必须在,否则不填题的用户会以为自己漏了一步。
           数字写「最多 8 道」不写「8 道」:`real_questions` 是 `[:8]`,兜底那条只有 5 条,
           写死 8 就成了另一个同类的假承诺。 */
        if (!isFormComplete) return '把品牌名称和所属行业填完就能开始 · 题可以不填,系统会按你的行业自动出题(最多 8 道)';
        if (totalDiagnosisCost == null) {
            return pricingError
                ? '价目暂时读不到,稍后再试一次;没有扣除任何算力。'
                : '正在读取价目,稍等一下就能开始';
        }
        return null;
    })();
    // brandName 已填但行业/关键词还缺 → 高亮「AI 填写」引导一键补全
    // [⑤b] 「没出题」不再是缺项(空 ⇒ 跑 AI 8),所以只看行业。
    const shouldSuggestAutofill = !!formData.brandName.trim() && !autofilling && !formData.industry.trim();


    /**
     * 🔴 [⑤b 返修 · 订正七③] 「使用示例」改成往**题单编辑器**里加题。
     *
     * 改前它写 `formData.keywords`。删掉关键词框之后那个字段没有任何 UI ⇒
     * **她点「使用示例」,屏幕上什么都不会发生** —— 例句进了搜索词、既不显示、
     * 也不计价、也不作为题去问。一个点了没有任何反馈的按钮,比没有这个按钮更糟。
     * (订正七③ 本来就要求把它并进题单区。)
     */
    // 顶部三步进度条的当前步(纯位置指示 · 不拦任何操作)
    const currentLaunchStep = resolveLaunchStep({
        brandName: formData.brandName,
        industry: formData.industry,
        questionCount,
    });

    // [CTO-15.23 2026-05-21 P0 修双扣] 前端不再 deductPoints 预扣 · 统一 freeze_points 单一扣费入口
    // 历史 BUG:前端 /api/wallet/deduct 扣 650 + 后端 /api/diagnosis freeze_points 扣 650+N×100 = 双扣
    // 修法:前端只做总价预算 + 余额预检 · 余额够直接 submit · 余额不够弹充值提示
    const proceedWithDeductAndSubmit = async () => {
        // [2026-06-16 客户报障] 兜底硬校验:必填没填完整直接拦(防 Enter 提交 / hydration fallback 绕过按钮 disabled)
        if (!isFormComplete) {
            // [diaglaunch 2026-08-05] 分清"没填完"和"填了但后端会拒"——后者原来会走到 422,用户只看到一句"启动失败"
            if (ownQuestions.tooLong.length > 0) {
                setError(`有 ${ownQuestions.tooLong.length} 道题超过 ${CUSTOM_QUESTION_MAX_CHARS} 字,请先改短再启动(第一条:「${ownQuestions.tooLong[0].slice(0, 20)}…」)`);
                return;
            }
            // [⑤b] 原来这里有一条「核心搜索问题最多 N 条」的提交期拦截,读的是已删的 keywords 框。
            //      题数上限现在由服务端算价与 validator 负责,前端不再镜像第二份口径。
            // 🔴 [#143④] 同一句假承诺的**第二处** —— 我第一遍只改了灰按钮理由那一处,
            //    是自己的判据 C7 把这处翻出来的。分母是「这句话出现在哪几个地方」,不是「我记得的那处」。
            setError('请先填写完整:品牌名称、所属行业 · 可点「AI 填写」一键补全');
            return;
        }
        if (totalDiagnosisCost == null) {
            setError(pricingError || '动态价目尚未确认，本次没有发起诊断或占用算力。');
            return;
        }
        if (!isAdmin && walletStatus !== 'ready') {
            setError('暂时无法确认当前功能可用算力，请先刷新钱包余额后再开始诊断；本次没有占用或扣除算力。');
            return;
        }
        if (isAdmin || toolPointsAvailable >= totalDiagnosisCost) {
            await executeSubmit();
            return;
        }
        setShowDeductDialog(true);
    };

    /**
     * [P1-11 · 2026-07-26] 疑似重复品牌检测。
     *
     * 生产实证：brand 278「深圳驰鲸科技」与 brand 712「深圳市驰鲸科技有限公司」
     * 是同一家公司，各自独立诊断计费、数据不互通。旧版只在 admin 场景做精确同名
     * 比对，这两个名字永远撞不上。
     *
     * 只在**未从下拉选中已有品牌**时检查（选了就说明用户已明确指向哪一个）；
     * 同一个名字提示过一次就不再拦（similarAckRef），不做无出口的反复阻断。
     */
    const checkSimilarBrands = async (): Promise<boolean> => {
        const typed = formData.brandName.trim();
        if (!typed || selectedBrandId) return true;
        if (similarAckRef.current === typed) return true;
        try {
            const res = await authFetch(`/api/my-clients/check-duplicate?name=${encodeURIComponent(typed)}`);
            if (!res.ok) return true;   // 检测失败不阻断建档（O1：可选检查不堵核心流程）
            const data = await res.json();
            const list = Array.isArray(data?.similar) ? data.similar : [];
            if (list.length === 0) return true;
            setSimilarBrands(list);
            setShowSimilarDialog(true);
            return false;
        } catch {
            return true;
        }
    };

    /**
     * [defgeo 装配] 第一阶段:把当前题单冻结成 question plan。
     *
     * 🔴 只做题单预览,**不算价、不扣费**。算价在 LaunchPanel 的
     *    run-preview,确认在 confirm —— 两阶段分开是 §15.3 的要求。
     * 🔴 409 全家不自己编话:一律渲染服务端 `publicExplanation`
     *    (§15.8 / U-2);`IDEMPOTENCY_CONFLICT` 无 publicExplanation ⇒ isSilent ⇒ 不上屏。
     */
    /**
     * 没选已有品牌时,**就地建档**再继续 —— 兑现品牌下拉里那句
     * 「无匹配品牌 · 继续输入将创建新品牌」。
     *
     * 🔴 [P0 2026-09-01] 走的是**现役**建档端点 `POST /api/my-clients`
     *    (底层 `db.diagnosis_db.get_or_create_brand`:ON CONFLICT 防 TOCTOU、
     *    自动 BRD 编号、is_test 自动判定、先过 validate_brand_name),
     *    与 legacy 诊断链同一条原语 —— **不另写一套建档**。
     *    后端合同一个字节不动:防御 preview 要的 `brandId` 由这一步给出,
     *    `profileRevisionId` 仍按现有写法从 brandId 派生。
     *
     * 两种失败必须**显式上屏**,不许吞(吞了就是把死按钮换成死点击):
     *   · 402 upgrade_required:L0 已有 ≥1 个非-self 品牌,多品牌是服务商特权;
     *   · 4xx 脏名字:`validate_brand_name` 拒(生产上 brands.id=278 那种带换行的名字
     *     会让诊断恒 0 分,所以这道校验不能绕)。
     */
    const ensureBrandForDefensive = async (): Promise<number | null> => {
        const existing = pendingBrandIdRef.current ?? selectedBrandId;
        if (existing) return existing;
        const name = formData.brandName.trim();
        if (!name) {
            setError('先填客户品牌名称,再开始体检');
            return null;
        }
        try {
            const res = await api.post<{ success?: boolean; brand_id?: number }>(
                '/api/my-clients',
                {
                    name,
                    industry: formData.industry.trim(),
                    city: formData.clientLocation.trim(),
                    business: formData.businessScope === 'national' ? '全国' : '',
                });
            const newId = res.data?.brand_id;
            if (!newId) {
                setError('建档没成功 —— 服务端没有返回品牌 ID,请稍后再试或改从下拉里选已有品牌');
                return null;
            }
            pendingBrandIdRef.current = newId;
            setActiveBrandId(newId);
            // 品牌下拉的列表不用在这里刷 —— 本次流程只需要 brandId,
            // 而 brandId 已经进了 pendingBrandIdRef / selectedBrandId。
            return newId;
        } catch (e) {
            const status = (e as { response?: { status?: number } })?.response?.status;
            const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
            // 402:额度闸。它自带引导文案,原样上屏 —— 前端不改写服务端的话。
            const sentence = typeof detail === 'string'
                ? detail
                : (detail && typeof detail === 'object'
                    ? String((detail as { message?: string; msg?: string }).message
                        ?? (detail as { msg?: string }).msg ?? '')
                    : '');
            setError(sentence
                || (status === 402
                    ? '你的账号只能管 1 个客户品牌;要再建请先升级,或从下拉里选已有品牌。'
                    : '建档没成功,请检查品牌名称后重试。'));
            return null;
        }
    };

    const runDefensivePlanPreview = async () => {
        setPlanError(null);
        const usable = defensiveDraft.filter((q) => q.text.trim().length > 0);
        if (usable.length === 0) {
            setError('先加一道客户会问的问题,再开始体检');
            return null;
        }
        setPlanPending(true);
        // 🔴 [P0 2026-09-01] 反馈必须在**按下的那一瞬**发生,不能等接口回来。
        //    生产实测 preview urt 只有 0.065-0.073s,而她 9 秒点了 14 次、间隔 <1s ——
        //    说明不是等不及,是**按下去屏幕上什么都没变**。等回来再反馈等于没反馈。
        //    所以这里先把面板滚进视线(此时它显示"正在生成题单…"),再去发请求。
        requestAnimationFrame(() => {
            launchPanelRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
        });
        const brandId = await ensureBrandForDefensive();
        if (!brandId) {
            setPlanPending(false);
            return null;
        }
        try {
            const plan = await previewQuestionPlan({
                // clientRequestId 稳定 ⇒ 同一份编辑不会造出两个 plan。
                // 🔴 与 legacy 的 loadOrCreateDiagnosisRequestId 是**两套键**,不混用。
                // [#62 2026-09-05] 键按**内容**取,不按条数。只改题文不改条数时
                // 旧键会把改之前那份 plan 幂等取回来 ⇒ 给旧 plan 算价,读数好看钱仍错。
                clientRequestId: `defgeo:${brandId}:${campaignMode}:${questionSetKey(usable)}`,
                brandId,
                profileRevisionId: `brand-${brandId}`,
                mode: campaignMode,
                questions: usable,
            });
            setQuestionPlan(plan);
            if (plan.pricingRule) setPricingRule(plan.pricingRule);
            // 🔴 [#62 2026-09-05] 这里**不再写** pricedSignature。
            //    它原先写在题单重建路径上 ⇒ 点「重新算一下」会把签名刷成当前题集、
            //    priceIsStale 清零、黄条消失,**而价格一次都没重算**。
            //    签名只在**算价成功**时写(LaunchPanel 的 onPriced),见 §①。
            // 成功之后再滚一次并把焦点交给面板的主行动:她可能在这 70ms 里又滑走了。
            // (点击瞬间那一次负责"立刻有反应",这一次负责"结果就在你眼前"。)
            requestAnimationFrame(() => {
                launchPanelRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
                launchPanelRef.current?.querySelector('button')?.focus({ preventScroll: true });
            });
            toast.success('题单已生成 · 往下看这次要花多少');
            return plan;
        } catch (e) {
            const err = e as DefGeoError;
            setQuestionPlan(null);
            if (!err.isSilent) setPlanError(err);
            return null;
        } finally {
            setPlanPending(false);
        }
    };

    /**
     * 🔴 [#62 P0 2026-09-05] 「重新算一下」的本体 —— **有序两步**,不是一步。
     *
     * 修前:这个按钮接的是 `runDefensivePlanPreview`(**题单生成器**),而那条路径里
     * 还写了 `setPricedSignature` ⇒ 点一下,黄条消失、面板显示新题数,**价格一次没重算**,
     * confirm 仍用绑旧题单的 preview。实跑 3 题×4 平台=12 次,她新加的两题 0 次出现,650 照扣。
     *
     * ⇒ **这比没有那个按钮更糟**:没按钮时她一直看着「题单变了」;有了它,系统警告 →
     *    她照做 → 警告消失,而警告的消失是系统能给的最强"已修复"信号。
     *
     * 正确形态是两段链,两段都得现做:
     *   ① draft ↔ plan —— 只有重建题单才更新 planId/revision
     *   ② plan  ↔ price —— run-previews 传的是 { questionPlanId, questionPlanRevision },
     *                      它**给 plan 定价,不给 draft 定价**
     * 所以只把按钮改成打 run-previews 仍然是错的:draft 变了而 plan 没重建时,
     * 它会给**旧 plan** 重新算一次价 —— 请求确实又发了一次(读数好看),钱仍然错。
     *
     * 本函数只负责 ①,并**亲自验一遍产物真的绑当前题单**;② 与"仅成功写签名"在
     * LaunchPanel 里(它才持有 run-previews)。任一步失败 ⇒ 返回 null ⇒ **黄条留着**。
     */
    const repriceFromCurrentDraft = async () => {
        const fresh = await runDefensivePlanPreview();
        if (!fresh) return null;
        // 🔴 不信"调用成功"这件事本身 —— 只信返回体里装的题单。
        //    `clientRequestId` 幂等、后端合并、并发覆盖,任何一种都会让这里拿到旧 plan。
        if (!planMatchesDraft(fresh.questions, defensiveDraft)) return null;
        return fresh;
    };

    /**
     * 🔴 [WO 2026-09-02 §3.1.4] `planError` 的**修复动作**。
     *
     * DEV_PRINCIPLES :83「提示二选一」——要么有明细 + 能点的修复,要么别显示。
     * 现状(Owner 线上撞到的)是有提示、无明细、无动作,还重复一遍,三违。
     *
     * 动作分两层,**都不猜**:
     *  ① 服务端 `nextAction.target` 认得出侧别 ⇒ 把该侧的系统建议题恢复回来
     *     (从 `removedSuggested` 移除,而不是新造题 —— 新造会绕开她删过的意图);
     *  ② 认不出 ⇒ 只做"聚焦题单"这个永远成立的兜底。
     *     猜错一侧会把用户带到错的地方,**比没有按钮更糟**。
     */
    const applyPlanErrorFix = () => {
        const side = sideFromTarget(planError?.envelope?.nextAction?.target);
        if (side) {
            const pool = suggestedQuestions(campaignMode, formData.brandName, { industry: formData.industry, location: formData.clientLocation, businessScope: formData.businessScope })
                .filter((q) => q.modeSide === side);
            setRemovedSuggested((prev) => {
                const next = new Set(prev);
                for (const q of pool) next.delete(removalKey(q));
                return next;
            });
        }
        setLastSideBlocked(null);
        setPlanError(null);
        // 聚焦到题单:侧别认不认得出,这一步都成立。
        const el = planEditorRef.current;
        if (el) {
            el.scrollIntoView({ block: 'center' });
            const first = el.querySelector('textarea');
            if (first instanceof HTMLTextAreaElement) first.focus();
        }
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);

        // ── [defgeo 装配] 唯一分流点 ──────────────────────────────────
        // 🔴 offensive(默认)时**直接落到下面的 legacy 函数体**,一字不改:
        //    不发 defensive 请求、不动 payload、不碰 client_request_id。
        //    防御/混合模式改由第 2 张 Card 里的 LaunchPanel 走两阶段
        //    preview→confirm,所以这里只是"别让表单 submit 触发 legacy 启动"。
        if (isDefensiveFlow) {
            await runDefensivePlanPreview();
            return;
        }

        if (!(await checkSimilarBrands())) return;

        // 管理员冲突检测：手动输入品牌名（未从下拉选中）且全量列表里存在其他用户的同名品牌
        if (isAdmin && !selectedBrandId && formData.brandName.trim() && allClientBrands.length > 0) {
            const myBrandIds = new Set(brandOptions.map(b => b.id));
            const conflicts = allClientBrands.filter(b =>
                b.name === formData.brandName.trim() && !myBrandIds.has(b.id)
            );
            if (conflicts.length > 0) {
                setConflictBrands(conflicts);
                setShowConflictDialog(true);
                return;
            }
        }

        await proceedWithDeductAndSubmit();
    };

    // [CTO-15.23 2026-05-21 P0 修双扣] dialog 内点"确认"不再 deductPoints 预扣
    // 直接 executeSubmit · 后端 freeze_points 是唯一扣费入口(余额仍不够时后端抛 402)
    const handleDeductConfirm = async () => {
        if (totalDiagnosisCost == null) {
            setShowDeductDialog(false);
            setError(pricingError || '动态价目已经失效，请重新读取后再试；本次没有发起诊断。');
            return;
        }
        if (!isAdmin && walletStatus !== 'ready') {
            setShowDeductDialog(false);
            setError('余额数据已经变化，请重新读取余额后再试；本次没有发起诊断。');
            return;
        }
        setDeductLoading(true);
        setShowDeductDialog(false);
        setDeductLoading(false);
        await executeSubmit();
    };

    const executeSubmit = async () => {
        if (totalDiagnosisCost == null) {
            setError(pricingError || '动态价目尚未确认，本次没有发起诊断或占用算力。');
            return;
        }
        setLoading(true);
        setError(null);

        // [P5b] 长任务启动:先占用额度提示(做成才正式扣,失败自动退回)· 管理员免扣不提示
        if (!isAdmin) {
            notifyFreezeHold(totalDiagnosisCost);
        }

        try {
            // 1. 如有文件则先上传
            let fileContent = "";
            if (file) {
                const fileFormData = new FormData();
                fileFormData.append("file", file);
                try {
                    const uploadRes = await axios.post("/api/upload", fileFormData);
                    fileContent = uploadRes.data.content;
                } catch (uploadErr) {
                    console.warn("文件上传失败，继续诊断:", uploadErr);
                }
            }

            // 2. 解析多行输入
            const ownAccounts = formData.ownAccounts
                ? formData.ownAccounts.split('\n').map(a => a.trim()).filter(a => a)
                : undefined;
            const competitors = formData.competitors
                ? formData.competitors.split('\n').map(c => c.trim()).filter(c => c)
                : undefined;

            // 3. 构建请求
            // 🔴 [订正二十九] 空 ⇒ **不带** keywords(服务端从上次已发布诊断/品牌名/行业派生);
            //    非空 ⇒ 原样带上她填的那几条,不加工、不补、不截断。
            const payload: any = {
                brand_name: formData.brandName,
                industry: formData.industry,
                additional_info: formData.additionalInfo || undefined,
                own_accounts: ownAccounts,
                competitors,
                diagnosis_scope: 'geo',
            };
            // 🔴 [订正二十九] 只有她真填了才带;空就整个字段不出现 ⇒ 服务端一处派生。
            //    这里**不做去重、不做补全、不做截断** —— 超限在上面就把提交拦住了,
            //    到这一步的每一条都已经是合法的,原样送。
            // 🔴 [订正三十①]「看到的必须就是跑的」:未改动带**端点返回的那一份**
            //    (不留空让服务端在提交时刻再派生一次 —— 那一刻可能已有新的已发布诊断);
            //    改动过带她的;没取到才不带。三态在 keywordsForSubmit 里,页面不再自己判。
            if (searchTermsSubmit.send) payload.keywords = searchTermsSubmit.terms;

            // [NEW] 品牌别名
            const brandDisplayNames = formData.brandDisplayNames
                ? formData.brandDisplayNames.split('\n').map(n => n.trim()).filter(n => n)
                : undefined;
            if (brandDisplayNames && brandDisplayNames.length > 0) {
                payload.brand_display_names = brandDisplayNames;
            }

            // 添加可选字段
            if (formData.clientLocation) {
                payload.client_location = formData.clientLocation;
            }
            // [P0-4] 业务范围决定选词是否必须带地域限定（区域客户出全国大词必然 0 命中）
            payload.business_scope = formData.businessScope || 'regional';
            if (fileContent) {
                payload.file_content = fileContent;
            }
            // [CTO-15.23 2026-05-21] 自定义诊断问题(老板拍板 + 5/21 订正)
            // P2 一致性:用与 render 同一份 parseCustomQuestions 结果(dedup + 100 字过滤)
            /**
             * 🔴 [#143① 2026-09-07] `price_preview_id` **无条件带上**。
             *
             * 改前它和 `custom_questions` 一起躺在 `if (题数 > 0)` 里,而服务端
             * `server.py:3667` 是**无条件**要它的(那段注释还专门写了为什么:
             * 「custom 为空那一支……计价规则那一轴仍然绑住」)。
             * ⇒ 增长模式 0 道题时必 400「这次提交少了价格确认标识」,
             *   而预览本身全好 —— 报错指向的地方与真正的原因无关,Owner 在真客户上撞到。
             *
             * 🔴 我自己那把锁没抓住它,原因值得写在这里:
             *   `verify-pkg1-server-price.mjs` C1 判的是「这行**存在**」——
             *   它确实存在,只是不可达。**存在 ≠ 可达**。本笔把锚改成
             *   「赋值不在任何题数条件分支内」并配注毒。
             */
            payload.price_preview_id = pricePreview?.pricePreviewId;
            if (ownQuestions.unique.length > 0) {
                payload.custom_questions = ownQuestions.unique;
                /**
                 * 🔴 [#149] 题面元数据 —— 后端据 `origin` 分辨哪几道是 AI 出的。
                 *
                 * 不送的后果**不是报错,是静默走偏**(老前端兼容路径全按 `customer`):
                 *   · 品牌定向豁免会把 AI 出的品牌题也豁免掉 ⇒ 进竞争格局分母,顶高提及率;
                 *   · 报告会把 AI 出的题写成「您填写的问题」。
                 * 两件事在屏幕上都**看起来正常**,所以新前端务必送。
                 *
                 * 🔴 `text` 必须 ⊆ `custom_questions`(否则 422)。这里传的就是上面那一份
                 *    `ownQuestions.unique`,由 `buildQuestionMeta` 逐条取 —— 不第二次归一化。
                 */
                payload.question_meta = buildQuestionMeta(planQuestions, ownQuestions.unique);
                /**
                 * 🔴 [阶段⑤ · #84 §3] 绑住「看到的价 == 提交的题集 == 扣的钱」。
                 *    服务端拿 custom_questions 去比这个 id 的哈希(server.py:3605 一带),
                 *    所以这里的 id 必须是**用同一份 questions** 取回来的那个 ——
                 *    传别的会每次 409,而错误看起来像服务端抽风。
                 *    缺 ⇒ 400 / 对不上 ⇒ 409 / 价目读不到 ⇒ 503,三条都明说没有扣算力。
                 */
                /**
                 * 🔴 [#149] 增长线**恒 false**:AI 出的题已经在题单里,
                 *    「也跑 AI 出的题」这个开关没有对象了(UI 上也不再显示)。
                 *    字段保留是为了不动 API 形状;防守/全面线不走这个提交体。
                 */
                payload.ai_optimize_custom = isDefensiveFlow ? formData.aiOptimizeCustom : false;
            }
            // brand_id：冲突弹窗选择的品牌优先，其次用下拉已选中的
            const effectiveBrandId = pendingBrandIdRef.current ?? selectedBrandId;
            if (pendingBrandIdRef.current !== null) pendingBrandIdRef.current = null;
            if (effectiveBrandId) {
                payload.brand_id = effectiveBrandId;
            }

            // [CTO-15.23 2026-05-05] P0 防御:brand_id 关联 brand 的 name 跟 brand_name 不一致时弹确认
            // 老板原 case:输"深圳怡然瑜伽" 但 brand_id 关联到 brand 173('瑜伽')→ 报告显示'瑜伽'
            if (effectiveBrandId) {
                const matchedBrand = brandOptions.find(b => b.id === effectiveBrandId)
                    || allClientBrands.find(b => b.id === effectiveBrandId);
                if (matchedBrand && matchedBrand.name && matchedBrand.name !== formData.brandName.trim()) {
                    const ok = await askConfirm({ title: `你输入的品牌名是「${formData.brandName.trim()}」，但即将关联到已有品牌「${matchedBrand.name}」`, description: `继续 → 报告里品牌名将显示「${formData.brandName.trim()}」但底层关联到「${matchedBrand.name}」客户档案；取消 → 返回检查(可清掉品牌选择再提交以新建独立品牌档案)。要继续吗?`, confirmLabel: '继续', cancelLabel: '取消' });
                    if (!ok) {
                        setLoading(false);
                        return;
                    }
                }
            }

            // [WORKERS=4 · P0-1 / 返工2 P1-1] 注入幂等 ID(缺则生成 · 存在则复用 → 超时重试同一 ID 不重复冻结)·
            //   sessionStorage 短暂持久化(TTL 10min)→ 提交中刷新页面复用同一 ID 防重复冻结
            const _reqKey = `classic:${effectiveBrandId ?? formData.brandName.trim()}`;
            if (!clientReqIdRef.current) clientReqIdRef.current = loadOrCreateDiagnosisRequestId(_reqKey);
            payload.client_request_id = clientReqIdRef.current;
            const response = await diagnosisApi.start(payload);
            // [返工2 P1-1] 幂等命中旧 run 已终态 → 后端 status='resolved'(非 started)· 不跳进死任务轮询
            if ((response.data as any)?.status && (response.data as any).status !== 'started') {
                clientReqIdRef.current = null;
                clearDiagnosisRequestId(_reqKey);
                setError('该请求此前已结束,请重新发起诊断');
                setLoading(false);
                return;
            }
            clientReqIdRef.current = null;   // 成功 → 下次提交是新意图
            clearDiagnosisRequestId(_reqKey);
            // 沙盒: 第一步"完成庆祝 + 衔接第二步"挪到落地报告页时做(等用户真看到报告)
            // 真实流程仍在启动时标记完成
            if (!isSandboxActive()) {
                markStep('first_diagnosis');
            }

            /* 2026-05-22 BUG 3 修(老板报"用户切出去后没看到生成中状态")
             * diagnosis_records 表完成才 INSERT · 列表没"生成中"占位
             * 用 localStorage 标记活跃 session · BrandDetail DiagnosisTab 渲染时显占位行
             * DiagnosisProgress 完成时清除 · 失败/取消保留 1h 后过期 */
            try {
                /* 新品牌时 effectiveBrandId=null · 后端 INSERT brands 后返回 brand_id
                 * 优先用 response.data.brand_id · fallback effectiveBrandId · 防占位行因 brandId mismatch 不显 */
                const finalBrandId = (response.data as { brand_id?: number })?.brand_id ?? effectiveBrandId ?? null;
                localStorage.setItem('active_diagnosis_session', JSON.stringify({
                    sessionId: response.data.session_id,
                    brandId: finalBrandId,
                    brandName: formData.brandName,
                    industry: formData.industry,
                    startedAt: Date.now(),
                }));
            } catch { /* localStorage 可能 disabled · 静默 */ }

            // 跳转到进度页面
            navigate(`/diagnosis/progress/${response.data.session_id}`);
        } catch (err: any) {
            console.error("诊断启动失败:", err);
            // [返工2 P1-1] 确定性拒绝(余额不足 402 / 参数非法 400)→ 清幂等 ID(重试换新意图);网络/未知 → 保留复用去重
            //   catch 作用域看不到 try 内 const effectiveBrandId · 用其定义式 (pendingBrandIdRef.current ?? selectedBrandId) 重建同 key
            if (isDeterministicDiagnosisReject(err)) {
                clientReqIdRef.current = null;
                clearDiagnosisRequestId(`classic:${(pendingBrandIdRef.current ?? selectedBrandId) ?? formData.brandName.trim()}`);
            }
            /**
             * 🔴 [阶段⑤ · #84 §3] 409 = 题集或价在她确认之后又变了。
             *    服务端已明说「这次没有扣除任何算力」。这里**当场重取一份新价**,
             *    她再确认一次即可 —— 不让她自己去猜要刷新页面。
             *    重取用的是**当前**题集,所以拿回来的 id 与她接下来提交的题集同源。
             */
            if (err?.response?.status === 409) {
                setPricePreview(null);
                setPricePending(true);
                void (async () => {
                    const r = await fetchDiagnosisPrice({
                        questions: ownQuestions.unique,
                        aiOptimizeCustom: !!formData.aiOptimizeCustom,
                        scope: 'geo',
                        mode: campaignMode,
                    });
                    setPricePending(false);
                    if (r.ok) setPricePreview(r.data);
                })();
            }
            setError(formatApiErrorForDisplay(err, "启动诊断失败，请重试"));
        } finally {
            setLoading(false);
        }
    };

    return (
        <>
        {/* [diaglaunch 2026-08-05 · 工单 §2] 容器查询锚点 = 内容区的真实宽度。
          * 🔴 用容器查询不用裸视口断点:sidebar 可收起,「窗口多宽」≠「内容区多宽」。
          *   1280 窗口 + 展开的 sidebar,真正能用的只有 ~1000px;裸 lg:/xl: 会把它判成"宽"。
          *   (geovid 三栏在 935px 容器上永远不出现,就是这么来的。)
          * 🔴 这一层里**不许放 position:fixed 元素**:container-type 隐含 contain:layout,
          *   fixed 后代会相对这个容器定位而不是视口 → 全屏遮罩当场错位。
          *   下面两个手写弹窗因此挪到了这层之外;Radix 那几个走 portal,不受影响。 */}
        <div className="@container w-full">
        <div
            data-testid="diagnosis-launch-shell"
            className={cn(
                "mx-auto w-full space-y-6 p-4 md:p-6",
                // 三档独立 layout(不是等比放大):
                //   窄容器 = 满宽单列 / 标准 = 1240 / 宽 = 1440 / 超宽(4K)= 1760 → 2000
                "@min-[880px]:max-w-[1240px]",
                "@min-[1600px]:max-w-[1440px]",
                "@min-[2200px]:max-w-[1760px]",
                "@min-[3000px]:max-w-[2000px]",
            )}
        >
            {/* CTO-15.20 桥接 banner */}
            <BridgeBanner />
            {/* Tab 切换 */}
            <div className="flex items-center gap-2">
                <button onClick={() => setActiveTab('new')}
                    className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${activeTab === 'new' ? 'bg-foreground text-background' : 'text-muted-foreground hover:text-foreground hover:bg-secondary'}`}>
                    发起体检
                </button>
                <button onClick={() => setActiveTab('history')}
                    className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${activeTab === 'history' ? 'bg-foreground text-background' : 'text-muted-foreground hover:text-foreground hover:bg-secondary'}`}>
                    体检报告
                </button>
            </div>

            {activeTab === 'history' ? (
                <Suspense fallback={<div className="flex justify-center py-12"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>}>
                    <HistoryList />
                </Suspense>
            ) : (
            <>
            <div className="flex items-center gap-4">
                <Button variant="ghost" size="icon" onClick={() => {
                    if (isRetest) {
                        // 复测模式：清掉 retest 参数，回到普通新建
                        setSearchParams({});
                        setFormData({ brandName: '', industry: '', ownAccounts: '', competitors: '', additionalInfo: '', clientLocation: '', businessScope: 'regional', diagnosisScope: 'geo', brandDisplayNames: '', customQuestions: '', aiOptimizeCustom: false });
                        setActiveBrandId(null);
                    } else {
                        navigate(-1);
                    }
                }}>
                    <ArrowLeft className="h-4 w-4" />
                </Button>
                <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center">
                    {isRetest ? <RefreshCw className="h-5 w-5 text-brand" /> : <ClipboardList className="h-5 w-5 text-brand" />}
                </div>
                <div>
                    <h2 className="text-2xl font-bold text-foreground">
                        {isRetest ? '复测品牌体检' : '新建品牌体检'}
                    </h2>
                    <p className="text-muted-foreground @max-[880px]:hidden">
                        {isRetest
                            ? `对「${prefillBrandName}」进行复测，上次得分: ${originalScore}分`
                            : isL0
                                ? '填好品牌信息和客户会问的问题，就能启动体检（可离开页面，完成后通知）'
                                : '填好客户的品牌信息和客户会问的问题，就能启动体检（可离开页面，完成后通知）'
                        }
                    </p>
                </div>
            </div>

            {/* [diaglaunch 2026-08-05 · 工单 §1.1] 顶部三步进度条 · 纯位置指示,不拦任何操作 */}
            <div className="@max-[880px]:hidden">
                <LaunchStepBar current={currentLaunchStep} />
            </div>

            {/* 复测提示框 */}
            {isRetest && (
                <div className="bg-green-500/5 border border-green-500/20 text-green-400 px-4 py-3 rounded-xl flex items-center gap-3">
                    <RefreshCw className="h-5 w-5 text-green-400 shrink-0" />
                    <div className="flex-1">
                        <span className="font-medium">复测模式：</span>
                        完成后可对比「{prefillBrandName}」AI可见度变化趋势
                        <span className="text-sm ml-2 text-green-400/70">(原记录 #{originalId})</span>
                    </div>
                    <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="shrink-0 text-xs border-green-500/30 text-green-400 hover:bg-green-500/10"
                        onClick={() => {
                            setSearchParams({});
                            setFormData({ brandName: '', industry: '', ownAccounts: '', competitors: '', additionalInfo: '', clientLocation: '', businessScope: 'regional', diagnosisScope: 'geo', brandDisplayNames: '', customQuestions: '', aiOptimizeCustom: false });
                            setActiveBrandId(null);
                        }}
                    >
                        退出复测
                    </Button>
                </div>
            )}

            {error && (
                <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-xl">
                    {error}
                </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-5">

            {/* 诊断范围 · 沙盒 stage 'page-intro' spotlight 锚点 */}
            <FeatureTooltip
                featureId="diagnosis_page_intro"
                stepId="first_diagnosis"
                title="这是品牌体检页面"
                // [CUR-05 2026-08-21] 引擎名单与耗时**不写死**:本次实际可用平台
                // 由服务端运行计划下发(§9.2「右栏只显示服务端返回的……实际可用平台、
                // 预计时间」)。写死会在平台增减或降级时对用户说谎。
                content={planCapabilityHint}
                side="bottom"
                wrapClassName="relative block"
                disabled={!isSandboxActive() || tutorialStage !== 'page-intro'}
                nextLabel="知道了"
                onNext={() => setTutorialStage('brand-name')}
            >
                <div className="rounded-xl border border-foreground/20 bg-foreground/5 px-4 py-3 @max-[880px]:py-2">
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
                        <div className="flex min-w-0 items-center gap-3">
                            <div className="@max-[880px]:hidden h-10 w-10 shrink-0 rounded-lg bg-brand/10 flex items-center justify-center">
                                <SearchIcon className="h-5 w-5 text-brand" />
                            </div>
                            <div className="min-w-0">
                                <div className="text-sm font-semibold text-foreground">GEO 诊断</div>
                                <div className="@max-[880px]:hidden mt-0.5 text-xs text-muted-foreground">
                                    检查品牌在 AI 搜索中的可见度、引用来源和关键词机会，预计 5 分钟左右。
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </FeatureTooltip>

            {/* 启动台:左右两栏 · form 包住两栏 · CTA 留右栏底部仍在 form 内
              * [diaglaunch 2026-08-05 · 工单 §2] 断点全部走容器查询:
              *   <880 单列(右栏沉到主列下方)/ ≥880 两栏 340 / ≥1600 两栏 380 / ≥2200 两栏 420 */}
            <div
                data-testid="launch-grid"
                className="grid grid-cols-1 gap-5 @min-[880px]:grid-cols-[minmax(0,1fr)_340px] @min-[1600px]:grid-cols-[minmax(0,1fr)_380px] @min-[2200px]:grid-cols-[minmax(0,1fr)_420px]"
            >
            {/* 主列自己也是一个查询容器:里面的卡片按**主列宽度**排,不按整页宽度排 */}
            {/* 🔴 [#125] 390 单列首屏四块前置(客户 / 行业 / 价格面板 / 按钮)。
                Deploy 线上实测(355×767 fresh load、行业已填、高级收起、0 题):
                CTA top=2127 —— **在首屏下方 1408px**。根因不是主列太长,而是
                `aside`(启动检查 / 本次会得到 / **CTA**)在单列档整列沉到主列之后。

                修法是**排序**,不是 sticky:窄档把两列都摊平成栅格项(`contents`),
                再逐块给 order —— 诊断对象 → CTA → 题单 → 高级 → 两张说明卡。

                🔴 **全部用 `@max-[880px]:`,不用 `@min-`** —— 浏览器底线闸要求
                容器查询"只做加法"(删掉它基础样式仍是完整布局)。若改用 `@min-` 给
                桌面显式定列,删掉那些类之后 3 个栅格项会自动流成"主列跑到右边",
                **那就不是加法了**。所以桌面保持原样(两个栅格项、自动流),
                窄档才摊平重排;删掉本批 class ⇒ 回到今天的布局,完整、只是没重排。

                🔴 仍**禁 sticky/fixed**:v9(5d731493)给这颗按钮加过 sticky bottom,
                iOS Safari pinch-zoom 时它绑 visualViewport 不跟内容缩放,老板报
                「底部有一个遮罩一直在搞事情」,v10 整条撤回。这里不重蹈。

                🔴 `contents` 会让 main-col 在窄档失去自己的 `@container` —— 而**两者同时存在会出事**。

                  🔴 上一版这里断言(还写着「已核」):「窄档下内层 `@max-[880px]` 改由 L1690 那个
                  `@container` 解析,页宽 355 < 880,**真值不变**」。**这句话是错的,已被线上读数证伪。**

                  真相:`container-type:inline-size` + `display:contents` 落在同一个元素上 ⇒ 它不生成盒
                  ⇒ 尺寸不可解析 ⇒ **子树里的容器查询求值为假**,**不会**回落到外层容器。

                  后果:主列那三个 窄档 order 变体(order-1…order-6,带 @max- 前缀) 计算值恒为 0 —— 不报错、不告警、类也确实
                  编译进了产物,只是运行时求假。#125 第一版「重排没生效」就是它,而它活到了线上。
                  右栏三个同形的类一直有效,因为右栏**故意没开** `@container`(见下方右栏注释)。

                  Deploy 2026-09-06 四格实验(E 格复原对照,证明无残留):
                    inline-size+contents → 0 · normal+contents → 1 · inline-size+block → 1 · normal+block → 1
                  ⇒ 改掉任一个都恢复。这里把**容器身份限定到宽档**:窄档 `contents`(要重排、不要容器),
                  宽档才当容器(卡片按主列宽排,不按整页宽排)。两个意图都保住,而且二者**在任何断点
                  都不同时存在** —— 冲突被结构性消除,不是靠纪律避开。
                  🔴 锁:`verify-container-query-conflict.mjs`,任何元素不许同时带 `@container` 与 `contents`。 */}
            <div className="space-y-5 @max-[880px]:contents @min-[880px]:[container-type:inline-size]" data-testid="launch-main-col">

            {/* 诊断对象 · [工单 §1.2] 每行一个字段 · label 清晰 */}
            <Card className="border border-border rounded-xl shadow-none @max-[880px]:order-1">
                <CardHeader className="p-5 pb-3 @max-[880px]:p-4 @max-[880px]:pb-2">
                    <CardTitle className="text-base">诊断对象</CardTitle>
                    <CardDescription className="@max-[880px]:hidden">要给谁做体检 —— 这四项决定 AI 会拿什么题去问</CardDescription>
                </CardHeader>
                <CardContent className="p-5 pt-0 space-y-4 @max-[880px]:p-4 @max-[880px]:pt-0 @max-[880px]:space-y-2">
                        <div className="space-y-4 @max-[880px]:space-y-2">
                            {/* 品牌名称 — 可搜索下拉 + 自由输入 */}
                            <FeatureTooltip
                                featureId="diagnosis_brand_input"
                                stepId="first_diagnosis"
                                title="品牌名已经填好"
                                content="教程模式已经帮你填好示例品牌「一路顺风出行服务」 · 真实使用时, 你填客户的品牌名 (新品牌直接填就行, 系统会自动建客户档案)"
                                side="bottom"
                                wrapClassName="relative block"
                                disabled={!isSandboxActive() || tutorialStage !== 'brand-name'}
                                nextLabel="下一步"
                                onNext={() => setTutorialStage('autofill')}
                            >
                            <div className="space-y-2 @max-[880px]:space-y-1">
                                <div className="flex items-center gap-1.5">
                                    <Label htmlFor="brandName">品牌名称 *</Label>
                                    <div className="relative group">
                                        <HelpCircle className="h-3.5 w-3.5 text-muted-foreground/40 cursor-help" />
                                        <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-3 py-2 bg-popover border border-border rounded-lg shadow-lg text-xs text-muted-foreground w-64 opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50">
                                            输入已有品牌名可自动加载历史数据；输入新品牌名将在诊断完成后自动创建品牌。
                                        </div>
                                    </div>
                                </div>
                                <div className="flex flex-col gap-2 sm:flex-row">
                                    <div className="relative flex-1" ref={brandDropRef}>
                                        <Input
                                            ref={brandInputRef}
                                            id="brandName"
                                            placeholder="输入品牌名搜索或创建新品牌"
                                            value={formData.brandName}
                                            readOnly={isRetest || isSandboxActive()}
                                            onClick={() => {
                                                if (isRetest) {
                                                    toast('复测模式下品牌名已锁定，如需测试其他品牌请先退出复测模式', {
                                                        action: { label: '退出复测', onClick: () => { setSearchParams({}); setFormData({ brandName: '', industry: '', ownAccounts: '', competitors: '', additionalInfo: '', clientLocation: '', businessScope: 'regional', diagnosisScope: 'geo', brandDisplayNames: '', customQuestions: '', aiOptimizeCustom: false }); setActiveBrandId(null); } },
                                                    });
                                                } else if (isSandboxActive()) {
                                                    toast.info('教程模式下品牌名锁定为"一路顺风出行服务"', {
                                                        description: '退出教程后用真实品牌名',
                                                        duration: 2500,
                                                    });
                                                }
                                            }}
                                            onChange={(e) => {
                                                if (isRetest || isSandboxActive()) return;
                                                setUserEditedForm(true);
                                                        setFormData({ ...formData, brandName: e.target.value });
                                                setBrandDropOpen(true);
                                                if (selectedBrandId) setActiveBrandId(null);
                                            }}
                                            onFocus={() => { if (!isRetest && !isSandboxActive()) setBrandDropOpen(true); }}
                                            required
                                            autoComplete="off"
                                            className={cn("pr-8", (isRetest || isSandboxActive()) && "bg-muted cursor-not-allowed")}
                                        />
                                        {/* [CTO-15.23 2026-05-13] ChevronDown 加可点击 toggle · 防"无法调用下拉"
                                            原 pointer-events-none 用户只能 focus 输入框触发 · 不直观 */}
                                        <button
                                            type="button"
                                            tabIndex={-1}
                                            className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted-foreground/60 hover:text-foreground"
                                            onClick={() => { if (!isRetest) setBrandDropOpen(v => !v); }}
                                            aria-label="展开品牌下拉"
                                        >
                                            <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", brandDropOpen && "rotate-180")} />
                                        </button>
                                        {/* [CTO-15.23 2026-05-13 BUG fix] 即使无匹配也显示 popup
                                            原仅在 filteredBrands.length > 0 才显示 → 新用户/无客户时下拉永远不出 · 老板报"无法调用下拉" */}
                                        {brandDropOpen && !isRetest && (
                                            <div className="absolute z-50 top-full left-0 right-0 mt-1 max-h-48 overflow-y-auto rounded-lg border border-border bg-popover shadow-lg">
                                                {filteredBrands.length > 0 ? filteredBrands.slice(0, 20).map(b => (
                                                    <div
                                                        key={b.id}
                                                        data-testid={`brand-option-${b.id}`}
                                                        className="w-full min-w-0 border-b border-border/30 last:border-b-0"
                                                    >
                                                        <div className="flex min-w-0 items-stretch">
                                                            <button
                                                                type="button"
                                                                data-testid="brand-option-select"
                                                                className="flex min-w-0 flex-1 flex-col items-stretch gap-0.5 overflow-hidden px-3 py-2 text-left text-sm transition-colors hover:bg-foreground/5 sm:flex-row sm:items-center sm:gap-2"
                                                                onClick={() => selectBrand(b)}
                                                                aria-label={`选择品牌 ${b.name}`}
                                                            >
                                                                <span
                                                                    data-testid="brand-option-name"
                                                                    className="line-clamp-2 min-w-0 flex-1 break-words whitespace-normal leading-snug sm:block sm:truncate"
                                                                    title={b.name}
                                                                >
                                                                    {b.name}
                                                                </span>
                                                                {(b.owner_name || b.industry) && <span
                                                                    data-testid="brand-option-metadata"
                                                                    className="line-clamp-2 min-w-0 break-words whitespace-normal text-[10px] leading-snug text-muted-foreground/60 sm:block sm:w-[40%] sm:max-w-[10rem] sm:flex-none sm:truncate sm:text-right"
                                                                    title={[
                                                                        b.owner_name ? `归属：${b.owner_name}` : '',
                                                                        b.industry ? `行业：${b.industry}` : '',
                                                                    ].filter(Boolean).join(' · ')}
                                                                >
                                                                    {b.owner_name && <>归属：{b.owner_name}{b.industry ? ' · ' : ''}</>}
                                                                    {b.industry && <>行业：{b.industry}</>}
                                                                </span>}
                                                            </button>
                                                            <button
                                                                type="button"
                                                                className="flex min-h-11 w-11 shrink-0 items-center justify-center text-muted-foreground transition-colors hover:bg-foreground/5 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand"
                                                                aria-label={`查看${b.name}完整信息`}
                                                                aria-expanded={expandedBrandId === b.id}
                                                                onFocus={() => setExpandedBrandId(b.id)}
                                                                onClick={() => setExpandedBrandId(b.id)}
                                                            >
                                                                <Info className="h-4 w-4" aria-hidden="true" />
                                                            </button>
                                                        </div>
                                                        {expandedBrandId === b.id && (
                                                            <div
                                                                data-testid={`brand-option-full-${b.id}`}
                                                                className="mx-3 mb-2 rounded-md border border-border/50 bg-muted/40 px-3 py-2 text-xs leading-5 text-foreground"
                                                            >
                                                                <div className="break-words font-medium">{b.name}</div>
                                                                {b.owner_name && <div className="break-words text-muted-foreground">归属：{b.owner_name}</div>}
                                                                {b.industry && <div className="break-words text-muted-foreground">行业：{b.industry}</div>}
                                                                <button
                                                                    type="button"
                                                                    className="mt-1 min-h-11 rounded px-2 text-brand hover:bg-brand/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                                                                    aria-label={`收起${b.name}完整信息`}
                                                                    onClick={() => setExpandedBrandId(null)}
                                                                >
                                                                    收起完整信息
                                                                </button>
                                                            </div>
                                                        )}
                                                    </div>
                                                )) : (
                                                    <div className="px-3 py-3 text-xs text-muted-foreground/70 text-center">
                                                        {displayBrands.length === 0 ? '暂无已有品牌 · 直接输入新品牌名即可' : '无匹配品牌 · 继续输入将创建新品牌'}
                                                    </div>
                                                )}
                                            </div>
                                        )}
                                    </div>
                                    <FeatureTooltip
                                        featureId="diagnosis_ai_fill_btn"
                                        stepId="first_diagnosis"
                                        title="点这里 AI 一键补全"
                                        content="点击后 AI 会自动补齐行业 / 关键词 / 竞品 (教程模式 · 预设数据 · 2.5 秒模拟)"
                                        side="bottom"
                                        disabled={!isSandboxActive() || tutorialStage !== 'autofill'}
                                    >
                                        <Button
                                            type="button"
                                            variant="outline"
                                            size="sm"
                                            className={cn(
                                                "shrink-0 bg-brand text-white hover:bg-brand/90 transition-all",
                                                // [2026-06-16 客户报障] brandName 已填但行业/关键词还缺 → 高亮引导用户点 AI 一键补全
                                                shouldSuggestAutofill && "ring-2 ring-brand/60 ring-offset-2 ring-offset-background shadow-lg shadow-brand/40 animate-pulse"
                                            )}
                                            disabled={autofilling || !formData.brandName.trim()}
                                            onClick={handleAutofill}
                                        >
                                            {autofilling ? (
                                                <><Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />搜索中</>
                                            ) : (
                                                <><Sparkles className="mr-1 h-3.5 w-3.5" />AI填写</>
                                            )}
                                        </Button>
                                    </FeatureTooltip>
                                </div>
                                {/* 新品牌提示条 */}
                                {formData.brandName.trim() && !selectedBrandId && !brandDropOpen && (
                                    <div className="flex items-center gap-1.5 mt-1.5 px-2 py-1.5 bg-amber-500/10 border border-amber-500/20 rounded-lg">
                                        <Info className="h-3 w-3 text-amber-500 shrink-0" />
                                        <span className="text-[11px] text-amber-600 dark:text-amber-400">
                                            未匹配到已有品牌，诊断完成后将自动创建「{formData.brandName.trim()}」
                                        </span>
                                    </div>
                                )}

                                {/* 🔴 [包一阶段② · 订正十补] 打标就地确认。
                                    后端在**新建那一刻**按名字自动打 `is_test`(`utils/is_test_brand.py`,三个写入点都调它)。
                                    打上之后:不进真客户统计 / KPI / admin 默认隐藏,而**用户全程没有任何提示**
                                    (#67 最小口径只保证她自己列表看得见)。事后解释比事前一句话贵得多。
                                    🔴 复述的是**今天真在跑的那份规则**(8 项),不是文档写的 5 项;
                                       三份规则的分歧见 #99 / WO_A_TEST_BRAND_RULE_DIVERGENCE。 */}
                                {testNameHit && !brandDropOpen && (
                                    <div data-testid="test-brand-confirm"
                                        className="mt-1.5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 space-y-1.5">
                                        <p className="text-[12px] leading-relaxed text-amber-700 dark:text-amber-300">
                                            这个名字含「{matchedTestPatterns(formData.brandName).join('」「')}」,建出来会被当作<b>测试客户</b>
                                            —— 不计入真实客户统计,报表里也看不到它。
                                        </p>
                                        <div className="flex items-center gap-3">
                                            <button type="button" data-testid="test-brand-accept"
                                                className="text-[12px] font-medium text-amber-800 dark:text-amber-200 underline underline-offset-2"
                                                onClick={() => setTestNameAck(true)}>
                                                仍要用这个名字
                                            </button>
                                            <button type="button" data-testid="test-brand-rename"
                                                className="text-[12px] text-muted-foreground underline underline-offset-2"
                                                onClick={() => {
                                                    const el = document.getElementById('brandName') as HTMLInputElement | null;
                                                    el?.focus(); el?.select();
                                                }}>
                                                换个名字
                                            </button>
                                            {testNameAck && (
                                                <span data-testid="test-brand-accepted" className="text-[11px] text-muted-foreground">
                                                    已确认 · 会按测试客户建档
                                                </span>
                                            )}
                                        </div>
                                    </div>
                                )}
                            </div>
                            </FeatureTooltip>
                        {subjectSummaryMode ? (
                            <SubjectSummary
                                industry={formData.industry}
                                clientLocation={formData.clientLocation}
                                businessScope={formData.businessScope}
                                onEdit={() => setSubjectEditing(true)}
                                disabled={loading}
                            />
                        ) : (
                          <>
                            <div className="space-y-2 @max-[880px]:space-y-1">
                                {/* 🔴 [包H · Owner 2026-08-24] 防守/混合模式**不强制**所属行业。
                                    与 G2b 修 keywords Textarea 同一个坑、同一个修法:按钮 disabled
                                    放开了没用,原生 required 会在 submit 事件**之前**拦掉约束校验,
                                    于是 handleSubmit 里那条 runDefensivePlanPreview() 永远走不到 ——
                                    她看到的是"按钮能点、点了没反应"。
                                    复用页面唯一分流键 isDefensiveFlow,不另写 campaignMode 判断。
                                    ⚠️ `isFormComplete` 一个字都不动:它是 legacy 提交路径的兜底硬校验。 */}
                                <Label htmlFor="industry">
                                    所属行业{isDefensiveFlow ? '（选填）' : ' *'}
                                </Label>
                                {/* [CTO-15.23 2026-05-13 BUG fix] 原 HTML <datalist> 在暗色主题/部分浏览器不显示
                                    改用自定义下拉跟 brandName 一致 · 老板报"品牌名称所属行业都无法调用下拉" */}
                                <div className="relative" ref={industryDropRef}>
                                    <Input
                                        id="industry"
                                        placeholder="选择或输入"
                                        value={formData.industry}
                                        onChange={(e) => { setFormData({ ...formData, industry: e.target.value }); setIndustryDropOpen(true); }}
                                        onFocus={() => setIndustryDropOpen(true)}
                                        required={!isDefensiveFlow}
                                        autoComplete="off"
                                        className="pr-8"
                                    />
                                    <button
                                        type="button"
                                        tabIndex={-1}
                                        className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted-foreground/60 hover:text-foreground"
                                        onClick={() => setIndustryDropOpen(v => !v)}
                                        aria-label="展开行业下拉"
                                    >
                                        <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", industryDropOpen && "rotate-180")} />
                                    </button>
                                    {industryDropOpen && (
                                        <div className="absolute z-50 top-full left-0 right-0 mt-1 max-h-60 overflow-y-auto rounded-lg border border-border bg-popover shadow-lg">
                                            {/* [CTO-15.23 2026-05-19] 当前值不在 INDUSTRIES 时动态加首项 · 让用户知道"翡翠饰品"等自定义行业有效 */}
                                            {showCurrentAsOption && (
                                                <button
                                                    type="button"
                                                    className="w-full text-left px-3 py-2 text-sm bg-primary/10 hover:bg-primary/20 transition-colors border-b border-border/50"
                                                    onClick={() => setIndustryDropOpen(false)}
                                                >
                                                    <span className="text-primary font-medium">✓ 使用当前行业 · {currentIndustry}</span>
                                                    <div className="text-[10px] text-muted-foreground mt-0.5">(自定义行业 · 客户档案已填)</div>
                                                </button>
                                            )}
                                            {filteredIndustries.length > 0 ? filteredIndustries.map(ind => (
                                                <button
                                                    key={ind.value}
                                                    type="button"
                                                    className="w-full text-left px-3 py-2 text-sm hover:bg-foreground/5 transition-colors"
                                                    onClick={() => { setFormData({ ...formData, industry: ind.value }); setIndustryDropOpen(false); }}
                                                >
                                                    {ind.label}
                                                </button>
                                            )) : (
                                                !showCurrentAsOption && (
                                                    <div className="px-3 py-3 text-xs text-muted-foreground/70 text-center">
                                                        无匹配行业 · 可直接输入自定义行业
                                                    </div>
                                                )
                                            )}
                                        </div>
                                    )}
                                </div>
                                {/* 🔴 [包H · Owner 2026-08-24 + Z-3.1] 防守/混合模式下行业不强制,
                                    但「AI 尽量填全」—— 所以缺行业时这里给一条**显眼**的入口,
                                    而不是让她自己去猜上面那颗 AI 按钮管不管行业。
                                    文案取 copy registry(前端不自造),按钮复用现役 handleAutofill
                                    (autofill_brand 能力),不另起第二条补齐链路。 */}
                                {isDefensiveFlow && !formData.industry.trim() && (
                                    <div
                                        data-testid="industry-optional-hint"
                                        className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/25 bg-amber-500/10 px-2.5 py-2"
                                    >
                                        <Info className="h-3.5 w-3.5 shrink-0 text-amber-500" aria-hidden />
                                        <span className="text-[11px] leading-relaxed text-amber-700 dark:text-amber-300">
                                            {DEFGEO_COPY.industryOptionalDefensive}
                                        </span>
                                        <Button
                                            type="button"
                                            variant="outline"
                                            size="sm"
                                            data-testid="industry-ai-fill"
                                            className="ml-auto h-7 shrink-0 px-2 text-[11px]"
                                            disabled={autofilling || !formData.brandName.trim()}
                                            onClick={handleAutofill}
                                        >
                                            <Sparkles className="mr-1 h-3 w-3" aria-hidden />
                                            {DEFGEO_COPY.aiFillIndustry}
                                        </Button>
                                    </div>
                                )}
                            </div>
                          </>
                        )}
                        </div>

                        {/* AI自动填写思考过程 */}
                        {isAutofillThinking && (
                            <SimulatedThinking steps={autofillThinking} isVisible={true} className="mt-2" />
                        )}
                        {autofillError && (
                            <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-3 py-2.5 rounded-xl flex items-center justify-between text-sm">
                                <span>{autofillError}</span>
                                <Button type="button" variant="outline" size="sm" className="shrink-0 ml-3" onClick={() => { setAutofillError(null); handleAutofill(); }} disabled={autofilling}>
                                    重试
                                </Button>
                            </div>
                        )}

                        {/* 客户区域 + 业务范围（[P0-4] 区域客户必须出带地域的题）
                          * [diaglaunch 2026-08-05 · 工单 §1.2] 改成每行一个字段
                          * [包一阶段②] 摘要态下整块收起,四项改由 SubjectSummary 呈现。 */}
                        {!subjectSummaryMode && (
                          <>
                        <div className="space-y-4 @max-[880px]:space-y-2">
                            <div className="space-y-1.5">
                                <Label htmlFor="client-location">客户城市</Label>
                                <Input
                                    id="client-location"
                                    placeholder="深圳、杭州、成都..."
                                    value={formData.clientLocation}
                                    onChange={(e) => setFormData({ ...formData, clientLocation: e.target.value })}
                                />
                            </div>
                            <div className="space-y-1.5">
                                <Label htmlFor="business-scope">业务范围</Label>
                                <select
                                    id="business-scope"
                                    className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                                    value={formData.businessScope}
                                    onChange={(e) => setFormData({ ...formData, businessScope: e.target.value })}
                                >
                                    {/* [包一阶段②] 标签走 subjectFields 单源 —— 摘要行用的是同一份短名,
                                        写两份的话改一处另一处不跟,而屏幕上两处各自看起来都对。 */}
                                    {BUSINESS_SCOPE_OPTIONS.map((o) => (
                                        <option key={o.value} value={o.value}>{o.optionLabel}</option>
                                    ))}
                                </select>
                                <p className="text-xs text-muted-foreground @max-[880px]:hidden">
                                    区域生意会出「城市 + 行业」的题；全国生意才会出不带地域的行业大词。填错会让测出来的命中率失真。
                                </p>
                            </div>
                        </div>
                          </>
                        )}

                </CardContent>
            </Card>

            {/* [diaglaunch 2026-08-05 · 工单 §1.3] 「客户会怎么问」——本单的心智升级。
              * 主输入的说法从"检测关键词"改成"核心搜索问题":代理真正该填的是客户会打给 AI 的
              * 那句话,而不是 SEO 词。
              * ⚠️ 这里原来写着「底层提交字段一个字没动:仍然是 payload.keywords,custom_questions
              *    也还在高级选项里」—— ⑤a/⑤b 之后**两句都不成立了**:提交体不带 keywords
              *    (订正二十五:服务端从品牌名派生),高级选项那个自定义题框也已删,
              *    题只从下面的题单编辑器出。留着这句会让下一个人照着去找两个不存在的东西。 */}
            <Card className="border border-border rounded-xl shadow-none @max-[880px]:order-3">
                <CardHeader className="p-5 pb-3">
                    <CardTitle className="text-base">客户会怎么问</CardTitle>
                    <CardDescription>AI 会拿这些问题去问 4 个 AI 平台，看你的品牌会不会被提到</CardDescription>
                </CardHeader>
                <CardContent className="p-5 pt-0 space-y-4">

                        {/* ── 目标模式选择(§9.2 原三卡 → 包一阶段① 三标签)──────────
                          * 🔴 [⑤b] 这里原来写着「offensive 默认时核心搜索问题 Textarea 与
                          *    formData.keywords 一字不动」—— 阶段⑤ 已把那个框和那个字段都删了,
                          *    三个标签下的出题位都是同一个题单编辑器。
                          * 🔴 推荐徽标只认服务端签发规则;start-context(§15.1)未建 ⇒
                          *    recommendation 恒 undefined ⇒ 三个标签都不显示徽标。 */}
                        {/* [包一 · 订正七②]「这次体检想解决什么」标题与三张卡一并由标签取代:
                            标签名 + 下一行说明已经把"这次体检想解决什么"说清楚了,
                            再留一个标题就是同一件事说两遍(Owner:删重复操作)。 */}
                        <div className="space-y-2">
                            <ModeTabs
                                value={campaignMode}
                                /* [阶段④ · 订正八①] 她自己选的,永远算数 —— 现在这句是**结构性**的:
                                   自动收敛已整套退役,不存在"系统替她改回去"的路径。 */
                                onChange={setCampaignMode}
                                recommendation={null}
                                disabled={loading}
                            />
                        </div>

                        {/* [⑤b] 题单编辑器**三个标签都渲染** —— 它现在是唯一的出题入口。
                            改前它挂在 isDefensiveFlow 后,默认线(增长)上根本不出现。 */}
                        {/* 三个标签都渲染,无条件 —— 编辑器现在是唯一出题入口 */}
                        {(
                            <div ref={planEditorRef}
                                className="space-y-3 rounded-lg border border-primary/30 bg-primary/[0.03] p-4">
                                {/* 🔴 [#144] 候选题的状态必须在**题单上方**说清:
                                    正在找 / 没找到(原因)/ 还差品牌。
                                    不说的话,空题单与"接口挂了"在屏幕上完全同形 ——
                                    她只会以为这页就是要自己从零写。 */}
                                {suggestPending && (
                                    <p role="status" data-testid="suggest-questions-pending"
                                        className="text-xs text-muted-foreground">
                                        正在按这个客户找「客户可能会这样问」的候选…
                                    </p>
                                )}
                                {!suggestPending && suggestNotice && (
                                    <p role="status" data-testid="suggest-questions-notice"
                                        className="text-xs text-muted-foreground">
                                        {suggestNotice}
                                    </p>
                                )}
                                <QuestionPlanEditor
                                    aiMuted={aiMuted}
                                    mutedNotice={mutedNotice(ownQuestionCount)}
                                    runningCount={effectiveRunCount}
                                    mode={campaignMode}
                                    brandName={formData.brandName}
                                    suggested={isDefensiveFlow ? suggestedWithFiller : growthSuggested}
                                    manual={editableQuestions}
                                    onManualChange={(next) => {
                                        // 🔴 [⑤b · 订正二十五] 编辑器里的条目**只有一个源** = manualQuestions。
                                        //    原来这里按 `fromKeywords` 把 next 拆两半,一半回写
                                        //    `formData.keywords`。那正是 C 在你我接缝上抓到的那格:
                                        //    编辑器镜像的是**题**的 100 字上限,写回的却是上限 50 的
                                        //    **关键词**字段 ⇒ 她在叫「问题」的框里打字,收到一条说
                                        //    「关键词」的 422。两边判据都照不到 —— 我的跨层锁比的是
                                        //    100 ≡ 100(恒绿),C 的判据只看 custom_questions。
                                        //    现在 keywords 在前端根本不存在,这个拆分也一并没了。
                                        setManualQuestions(next);
                                    }}
                                    onRemoveSuggested={(index) => {
                                        const q = suggestedWithFiller[index];
                                        if (!q) return;
                                        // 🔴 [WO 2026-09-02 §3.1.5] **上游预防**:hybrid 下删到某侧为空,
                                        //    后端 `validate_plan` 会抛「两侧都必须有题」,而它当时只带
                                        //    reason_key=None ⇒ 面板底部弹一句无明细无动作的通用话
                                        //    (Owner 线上就是这么撞上的)。与其事后报错,不如**不让它发生**:
                                        //    该侧最后一道题不删,改为就地内联说明 —— 这才符合 :76「不中断」。
                                        const emptySide = wouldEmptySide(
                                            campaignMode as PlanMode, defensiveDraft, q);
                                        if (emptySide) {
                                            setLastSideBlocked(emptySide);
                                            return;
                                        }
                                        setLastSideBlocked(null);
                                        setRemovedSuggested((prev) =>
                                            new Set(prev).add(removalKey(q)));
                                    }}
                                    disabled={loading || planPending}
                                />
                                {/* 🔴 [⑤b] 双轨开关从折叠的高级面板搬到这里 —— 它管的就是上面那批 AI 题跑不跑。
                                    原位置的问题:「AI 出的题不再跑」这句说明在编辑器里,而改变它的开关
                                    在**默认收起**的高级选项里 —— 解释与控制分处两地,
                                    与订正十二「UI 必须把这条规则做成看得见的」相悖。
                                    只在她出了题时才有意义(没出题时 AI 那批本来就跑)。 */}
                                {/* 🔴 [#149] 增长线**不显示**双轨开关:AI 的题已经在题单里,
                                    「也跑 AI 出的题」没有对象。只留给防守/混合线。 */}
                                {isDefensiveFlow && customQuestionCount > 0 && (
                                    <label className="flex items-start gap-2 cursor-pointer select-none">
                                        <input
                                            type="checkbox"
                                            data-testid="also-run-ai-toggle"
                                            className="mt-0.5 h-4 w-4 rounded border-border bg-card text-foreground focus:ring-foreground"
                                            checked={formData.aiOptimizeCustom}
                                            onChange={(e) => setFormData({ ...formData, aiOptimizeCustom: e.target.checked })}
                                        />
                                        <div className="text-xs text-foreground">
                                            <div className="font-medium">也跑 AI 出的题(双轨)</div>
                                            <div className="text-muted-foreground mt-0.5">
                                                默认不跑。勾选后这次会把 AI 出的题和你自己出的题都跑一遍,
                                                跑的题变多,价也随之变。
                                            </div>
                                        </div>
                                    </label>
                                )}
                                {lastSideBlocked && (
                                    <p role="status" data-testid="plan-side-inline-hint"
                                        className="text-[12px] text-amber-800">
                                        {`混合体检两类问题各要留一道 —— 这是「${sideLabel(lastSideBlocked)}」的最后一道,`
                                            + '删了就没法算价了。想换掉它,可以先加一道同类的再删。'}
                                    </p>
                                )}
                                {/* 🔴 [阶段④ · 订正八①] 原来这里是自动收敛的两行就地说明 + 两颗撤销按钮
                                    (plan-auto-hybrid-undo / plan-single-side-undo)。收敛退役 ⇒ 一并删。
                                    取而代之的是下面这句:防守标签下,她填的「核心搜索问题」属于增长线、
                                    这次不跑 —— 说清楚,而不是把它们悄悄塞进题单再自动改她的模式。 */}
                                {/* [⑤b 返修] 原来这句是给「关键词迁移」配的说明。迁移已拆 ⇒ 说明也没有对象了:
                                    keywords 现在是纯搜索词、不进题单、三个标签下行为一致,没有「这次不跑」这回事。 */}
                                {planError && !planError.isSilent && (
                                    <div role="alert" data-testid="plan-error"
                                        className="rounded-md border border-amber-300 bg-amber-50 p-3 space-y-2">
                                        {/* §15.8:用服务端的用户句,前端不自己把 code 翻成中文。
                                            🔴 [WO §3.1.4] 原来这下面还硬编码了一行「没有扣除任何算力。」,
                                            而 registry 的 `validation_failed` 句尾**已经含这句** ⇒ 同一句连出两遍。
                                            删掉硬编码那行,只留服务端那一句。 */}
                                        <p data-testid="plan-error-sentence"
                                            className="text-[13px] text-amber-900">{planError.userSentence}</p>
                                        {/* 🔴 :83 提示二选一:要么有明细+修复动作,要么不显示。
                                            按钮文案优先用服务端 `nextAction.label`;没有就退到永远成立的兜底
                                            (聚焦题单),**不猜**具体哪一侧 —— 猜错比没有按钮更糟。 */}
                                        <button type="button" data-testid="plan-error-fix"
                                            className="text-[12px] font-medium text-amber-900 underline underline-offset-2"
                                            onClick={applyPlanErrorFix}>
                                            {planError.nextActionLabel ?? '去把问题补好'}
                                        </button>
                                    </div>
                                )}
                                {/* 🔴 [P0 2026-09-01] 即时反馈:preview 成功是**静默**的,
                                    手机上面板还在屏幕外 —— 她眼前什么都没变,于是连点。
                                    这一行 + 下面的 scrollIntoView/focus 一起,才算"推进了"。 */}
                                {/* 🔴 滚动目标必须**从反馈那一行开始**,不是从面板开始。
                                    实测:ref 只挂面板时 `scrollIntoView({block:'start'})`
                                    把面板顶到视口顶,而反馈行在它**上面** ⇒ y=-40,
                                    她滚过去了却读不到"题单已生成"那句话。
                                    这一格是新判据 test-diagnosis-launch-viewport 抓出来的。 */}
                                <div ref={launchPanelRef}>
                                {(planPending || questionPlan) && (
                                    <p data-testid="launch-feedback" role="status"
                                       className="rounded-md bg-primary/10 px-3 py-2 text-[13px] text-foreground">
                                        {planPending
                                            ? '正在生成题单…'
                                            : `题单已生成 · 共 ${questionPlan?.counts.total ?? 0} 道 · 下面这块显示这次要花多少`}
                                    </p>
                                )}
                                {/* revision 只从**真** brandId 派生:原来的 `?? 0` 会拼出合法但
                                    无意义的 `brand-0`(过得了后端 min_length=1),把"没有品牌"
                                    伪装成一个身份。没有 plan 时它不会被用到。 */}
                                <LaunchPanel
                                    draftCount={liveQuestionCount}
                                    pricingRule={pricingRule}
                                    priceIsStale={priceIsStale}
                                    onRepriceRequested={repriceFromCurrentDraft}
                                    onPriced={setPricedSignature}
                                    plan={questionPlan}
                                    profileRevisionId={questionPlan ? `brand-${questionPlan.brandId}` : ''}
                                    platformKeys={[...DEFENSIVE_GEO_PLATFORM_KEYS]}
                                    onConfirmed={(result) => {
                                        // 与 legacy 同一个落地页:确认后去看进度。
                                        // 🔴 [门八第二发现] 这里必须是 progressSessionId,**不是** runId。
                                        //    runId 是 run_token;进度端点/WS 校验的是
                                        //    diagnosis_runs.session_id = "defgeo_" + run_token。
                                        //    拿 runId 拼路径 ⇒ 她付完钱看到「无权访问该诊断进度」。
                                        navigate(`/diagnosis/progress/${result.progressSessionId}`);
                                    }}
                                />
                                </div>
                            </div>
                        )}

                        {/* 🔴 [WO §3.2 2026-09-02] 「核心搜索问题」整块**只在 legacy(单模式)下渲染**。
                            hybrid / defensive 下它已并进上面那个问题列表(每条按 modeSide 打「会问/会搜」标签),
                            用户视角两者本就是同一件事;后端也一直是同一个数组。
                            🔴 **legacy 主动线一个字不动** —— 老链接与老书签全落在这条线上,
                            这也是判据里那条反臂要钉的:hybrid/defensive 不渲染 ≠ legacy 也不渲染。 */}
                        {/* 🔴 [⑤b · 订正七① / 二十四] 旧的「核心搜索问题」Textarea 已删。
                            它与题单是**同一概念两个框**,而且两边互不相通:她在这里填的进 legacy 的
                            `keywords`,在题单里出的进防御链 —— 默认线上后者根本不渲染。
                            现在题只有一个入口(下面的题单编辑器)。
                            🔴 [订正二十五] `keywords` **前端一个字都不发**,由服务端从品牌名派生
                            (C 那笔:去 min_length + 一处派生 + 三源皆空才拒收)。
                            为什么连"顺便带上品牌档案里的关键词"都不留:只要前端还有一个写 keywords
                            的口子,就会有下一个人把「题」写进去 —— 而题在服务端过的是上限 50 的
                            `validate_keywords`,编辑器镜像的却是上限 100 的题 ⇒ 她在一个叫「问题」
                            的框里打字,收到一条说「关键词」的 422(C 在你我接缝上抓到的就是这一格)。
                            两边判据都照不到,因为我的跨层锁比的是 100 ≡ 100(恒绿),
                            C 的判据只看 custom_questions、不看被写回的 keywords。
                            🔴 本笔**绝不能先于 C 那笔落地** —— 否则三源皆空必被拒。
                               判据里有一条行为臂钉这件事(本机无库 ⇒ 未评估,交 C 处跑)。 */}

                        {/* 🔴 [#149 2026-09-08] 示例问题卡**整块删除**。
                            Owner 原话:「为什么非要在下面添加个示例,直接填充到上面这个框不行吗?」
                            AI 出的题现在直接进上面的题单(`aiSuggested` → `suggestedDefensive`),
                            示例卡的职责被题单本身接管。
                            连同 `QuestionExampleCard.tsx` / `searchQuestionExamples.ts` 一并退役,
                            其判据(含 L3「示例卡必须真渲染」与变异 M7/M9/M10)
                            **按 Review #149 裁定退役**,不是改代码时顺手拿掉挡路的判据。 */}

                </CardContent>
            </Card>

            {/* [diaglaunch 2026-08-05 · 工单 §1.6] 高级选项 + 补充材料 收成**两条折叠行**,
              * 同一张卡里上下排,不再各占一张卡把主流程挤下去。 */}
            <Card className="border border-border rounded-xl shadow-none @max-[880px]:order-4" data-testid="collapsed-rows">
                <CardContent className="divide-y divide-border p-0">
                        <div className="p-4">
                            <div
                                data-testid="advanced-row"
                                className="flex items-center gap-2 text-sm text-muted-foreground cursor-pointer hover:text-foreground transition-colors select-none"
                                onClick={() => setShowAdvanced(!showAdvanced)}
                            >
                                <ChevronRight className={`h-4 w-4 shrink-0 transition-transform duration-200 ${showAdvanced ? 'rotate-90' : ''}`} />
                                <span className="font-medium">高级选项</span>
                                {(formData.brandDisplayNames.trim() || formData.ownAccounts.trim() || formData.competitors.trim() || customQuestionCount > 0) ? (
                                    <span className="inline-flex items-center gap-1.5 text-xs text-brand">
                                        <span className="h-1.5 w-1.5 rounded-full bg-brand" />
                                        {/* 🔴 [#165 F11] 原文是「已填」。这四个字段多半是从客户档案
                                            带进来的,她自己一个字没敲过 —— 说「已填」是替她宣称
                                            做过一件没做的事。「有内容」只陈述状态,对带进来的和
                                            她敲的都成立。 */}
                                        有内容
                                    </span>
                                ) : (
                                    <span className="text-xs font-normal text-muted-foreground/60">· 可选,别名 / 自有账号 / 竞品 / 自定义题</span>
                                )}
                            </div>

                            {showAdvanced && (
                                <div className="mt-4 space-y-4 p-4 bg-muted rounded-xl border border-border animate-in slide-in-from-top-2">
                                    {/* 🔴 [订正二十九 · 乙案] 搜索词 —— 折叠 / 可选 / 默认空 / **不预填**。
                                        它**不是题**:题是拿去问 AI 的整句话(100 字,走 custom_questions),
                                        词是喂抖音/小红书搜索框的短词(50 字,走 keywords)。两个概念两个输入位,
                                        所以订正二十五「一概念一输入位」不破。
                                        为什么不预填:预填过的框会让她以为那是她填的,改一个字就变成她的输入;
                                        而复诊的沿用由服务端派生(C4),前端一个字都不带,两边不会打架。 */}
                                    <div className="space-y-1.5">
                                        <Label htmlFor="searchTerms" className="text-foreground flex items-center gap-1.5">
                                            <Search className="h-3.5 w-3.5 text-muted-foreground" />
                                            搜索词(选填)
                                            {sourceLabel(prefill.kind === 'ready' ? prefill.source : null,
                                                prefill.kind === 'ready' ? prefill.aiAugmented : false) && (
                                              <span data-testid="search-terms-source" className="ml-1 rounded-full border border-border px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
                                                {sourceLabel(prefill.kind === 'ready' ? prefill.source : null,
                                                  prefill.kind === 'ready' ? prefill.aiAugmented : false)}
                                              </span>
                                            )}
                                        </Label>
                                        <Textarea
                                            id="searchTerms"
                                            data-testid="search-terms-input"
                                            placeholder="不填也行,AI 会按这个品牌已有的信息来规划"
                                            className="min-h-[60px] bg-card placeholder:text-muted-foreground/30"
                                            value={searchTermsRaw}
                                            onChange={(e) => { setSearchTermsRaw(e.target.value); setSearchTermsDirty(true); }}
                                        />
                                        <p className="text-[12px] text-muted-foreground" data-testid="search-terms-hint">
                                            每行一个,最多 {SEARCH_TERMS_MAX_COUNT} 条,单条 {SEARCH_TERM_MAX_CHARS} 字以内。
                                            这些词用来在抖音/小红书里找素材,不是拿去问 AI 的问题。
                                        </p>
                                        {searchTermsEmptyState.text && (
                                          <p role="status" data-testid="search-terms-empty-state"
                                            className={searchTermsEmptyState.mustFill
                                              ? "text-[12px] text-amber-500" : "text-[12px] text-muted-foreground"}>
                                            {searchTermsEmptyState.text}
                                          </p>
                                        )}
                                        {brandSwitchMsg && (
                                          <p role="status" data-testid="search-terms-brand-switch" className="text-[12px] text-muted-foreground">
                                            {brandSwitchMsg}
                                          </p>
                                        )}
                                        {searchTerms.overflow && (
                                            <p role="alert" data-testid="search-terms-overflow"
                                                className="text-[12px] text-destructive">
                                                现在有 {searchTerms.terms.length} 条,超过 {SEARCH_TERMS_MAX_COUNT} 条了 ——
                                                删掉几条再开始;一条都不会被我们悄悄丢掉。
                                            </p>
                                        )}
                                        {searchTerms.tooLong.length > 0 && (
                                            <div role="alert" data-testid="search-terms-too-long"
                                                className="text-[12px] text-destructive space-y-0.5">
                                                <p>这 {searchTerms.tooLong.length} 条超过 {SEARCH_TERM_MAX_CHARS} 字,
                                                    看起来更像「问题」而不是搜索词 —— 整句话请填到上面的题单里:</p>
                                                {searchTerms.tooLong.map((t, i) => (
                                                    <p key={i} className="truncate">· {t}</p>
                                                ))}
                                            </div>
                                        )}
                                    </div>

                                    {/* 品牌别名 */}
                                    <div className="space-y-1.5">
                                        <Label htmlFor="brandDisplayNames" className="text-foreground flex items-center gap-1.5">
                                            <Tag className="h-3.5 w-3.5 text-muted-foreground" />
                                            品牌别名
                                        </Label>
                                        <Textarea
                                            id="brandDisplayNames"
                                            placeholder="对外品牌名（如与公司名不同），每行一个"
                                            className="min-h-[60px] bg-card placeholder:text-muted-foreground/30"
                                            value={formData.brandDisplayNames}
                                            onChange={(e) => setFormData({ ...formData, brandDisplayNames: e.target.value })}
                                        />
                                    </div>

                                    <div className="space-y-1.5">
                                        <Label htmlFor="ownAccounts" className="text-foreground flex items-center gap-1.5">
                                            <UserCircle className="h-3.5 w-3.5 text-muted-foreground" />
                                            自有账号（排除竞品误判）
                                        </Label>
                                        <Textarea
                                            id="ownAccounts"
                                            placeholder="自有账号昵称，每行一个"
                                            className="min-h-[60px] bg-card placeholder:text-muted-foreground/30"
                                            value={formData.ownAccounts}
                                            onChange={(e) => setFormData({ ...formData, ownAccounts: e.target.value })}
                                        />
                                    </div>

                                    <div className="space-y-1.5">
                                        <Label htmlFor="competitors" className="text-foreground flex items-center gap-1.5">
                                            <Globe className="h-3.5 w-3.5 text-muted-foreground" />
                                            指定竞品
                                        </Label>
                                        <Textarea
                                            id="competitors"
                                            placeholder="竞品账号或品牌名，每行一个"
                                            className="min-h-[60px] bg-card placeholder:text-muted-foreground/30"
                                            value={formData.competitors}
                                            onChange={(e) => setFormData({ ...formData, competitors: e.target.value })}
                                        />
                                    </div>

                                    {/* 🔴 [⑤b · 订正二十四] 「自定义检测问题」Textarea 与双轨开关**整块搬走**。
                                        Textarea:题只有一个入口了 —— 上面的题单编辑器(它才是真正会跑的那份);
                                        留着这个框 = 同一概念两个输入位,而且它与编辑器**互不相通**。
                                        双轨开关:它管的是「AI 那批跑不跑」,而那句说明在编辑器里。
                                        解释在一处、控制在另一处、且控制默认收起 —— 与订正十二
                                        「UI 必须把这条规则做成看得见的」相悖 ⇒ 开关移到题单编辑器旁。 */}
                                </div>
                            )}
                        </div>

                    {/* 第二条折叠行:补充材料(默认收起 · 原生 <details> 无新增 state/事件处理) */}
                    <div className="p-4">
                    <details className="group">
                        <summary data-testid="materials-row" className="flex items-center gap-2 text-sm text-muted-foreground cursor-pointer hover:text-foreground transition-colors select-none list-none [&::-webkit-details-marker]:hidden">
                            <ChevronRight className="h-4 w-4 shrink-0 transition-transform duration-200 group-open:rotate-90" />
                            <span className="font-medium">补充材料</span>
                            {(formData.additionalInfo.trim() || file) ? (
                                <span className="inline-flex items-center gap-1.5 text-xs text-brand">
                                    <span className="h-1.5 w-1.5 rounded-full bg-brand" />
                                    {formData.additionalInfo.trim() ? '已填' : '已选附件'}
                                    {formData.additionalInfo.trim() && file ? ' · 已选附件' : ''}
                                </span>
                            ) : (
                                <span className="text-xs text-muted-foreground/60 font-normal">· 可选,提升诊断精准度</span>
                            )}
                        </summary>
                        <div className="mt-4 space-y-4">
                            <div className="space-y-2">
                                <Label htmlFor="additional">补充信息</Label>
                                <Textarea
                                    id="additional"
                                    placeholder="输入公司简介、核心优势等..."
                                    value={formData.additionalInfo}
                                    onChange={(e) => { setUserEditedForm(true); setFormData({ ...formData, additionalInfo: e.target.value }); }}
                                />
                            </div>

                            {/* 文件上传 */}
                            <div className="space-y-2">
                                <Label>上传附件 (支持 PDF/MD)</Label>
                                <div className="border-2 border-dashed border-border rounded-xl p-6 text-center hover:bg-muted transition-colors cursor-pointer relative">
                                    <input
                                        type="file"
                                        className="absolute inset-0 opacity-0 cursor-pointer"
                                        onChange={(e) => setFile(e.target.files?.[0] || null)}
                                        accept=".md,.txt,.pdf"
                                    />
                                    <div className="flex flex-col items-center gap-2 text-muted-foreground">
                                        <Upload className="w-8 h-8 text-muted-foreground" />
                                        <span className="text-sm font-medium">
                                            {file ? file.name : "点击或拖拽文件到此处"}
                                        </span>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </details>
                    </div>
                </CardContent>
            </Card>

            {/* [CTO-15.23 2026-05-21 v10] 撤回 v9 sticky bottom · 真根因 = v9 自己制造的"底部遮罩"
              * Qwen3.5-Omni-Plus 分析 + iOS Safari 真机 pinch zoom 实证:
              * sticky bottom 元素在 iOS Safari pinch zoom 时绑定 visualViewport · 不跟内容缩放
              * 周围 form 内容放大 1.5x · sticky bar 大小不变 · 视觉上"底部 bar 卡屏幕"
              * = 老板原话"底部有一个遮罩一直在搞事情"
              * = Qwen 报告"固定 bottom bar 拦截 touchmove · 跟手势走"
              * v9 (5d731493) 让按钮 sticky 反而制造了真 BUG · 撤回 · 回 inline 自然 flow
              * form 短下方空白是设计层 UX 问题 · 不通过 sticky 解 · 后续走 min-h 或 form 内容补充方向
              */}
            {/* [2026-06-03 全站静默扣费] 删:原"本次诊断积分明细"inline 价格表 · 不前置展示价格
              * 费用走铃铛/站内信/账单事后通知 · totalDiagnosisCost 仍用于余额预检 + freeze hold
              */}

            </div>{/* /左栏 */}

            {/* 右栏:启动检查 + 本次会得到 + CTA
              * sticky 只在两栏档(容器 ≥880)生效 —— 单列档右栏沉到主列下方,不做 sticky。
              * 🔴 单列档也**不给 CTA 做 sticky 底栏**,理由见下方 CTA 处的长注释(v9 旧坑)。
              * 🔴 右栏**故意不开 @container**:里面的 @max-[880px] 判据问的是"整页内容区宽不宽"
              *   (决定单列还是两栏),不是"右栏自己宽不宽"。右栏在两栏档只有 340-420px,
              *   开了 @container 这条判据就恒真,CTA 在两栏档也会贴底。 */}
            <aside data-testid="launch-aside" className="space-y-4 h-fit @max-[880px]:contents @min-[880px]:sticky @min-[880px]:top-6">
                {/* 启动检查 · 状态派生自 formData · 只展示不堆解释 */}
                <Card className="border border-border rounded-xl shadow-none @max-[880px]:order-5">
                    <CardHeader className="p-5 pb-3">
                        <CardTitle className="text-base">启动检查</CardTitle>
                    </CardHeader>
                    <CardContent data-testid="launch-checklist" className="p-5 pt-0 space-y-2.5">
                        {[
                            { label: '品牌名称', ok: !!formData.brandName.trim(), optional: false },
                            { label: '所属行业', ok: !!formData.industry.trim(), optional: false },
                            // [diaglaunch 2026-08-05] 跟主输入改口径:说法是"核心搜索问题",判据用**有效条数**
                            //   而不是 keywords.trim() —— 后者只要有个空格就算"已填",和启动按钮的口径打架。
                            // 🔴 [#143④ 撤回订正] 曾经改成 `effectiveRunCount >= 1 && ...`,
                            //    那是跟着一条错闸走的。**空题集本来就合法**
                            //    (`diagnosis_workflow.py:881` custom 为空 ⇒ 总是走 system 题),
                            //    写 >=1 会把「不出题」这条**最常见的主路径**说成缺项。
                            { label: '题单', ok: ownQuestions.tooLong.length === 0, optional: false,
                              // 🔴 [#165 F11] 这一格的 ok 判的是「没有超长题」——
                              //    0 道时它当然为真,于是她一个字没填就看到「已填」。
                              //    ok 的判据不动(空题集本来就合法,见 #143④ 撤回订正),
                              //    动的是那个词:状态词只能陈述现在是什么状态,
                              //    不能替用户宣称「你填了」。
                              okLabel: liveQuestionCount === 0 ? '等 AI 出题' : '已就绪' },
                            { label: '客户区域', ok: !!formData.clientLocation.trim(), optional: true },
                        ].map((it) => (
                            <div key={it.label} data-testid="checklist-item" data-ok={it.ok ? '1' : '0'} className="flex items-center justify-between gap-2 text-sm">
                                <span className="min-w-0 truncate text-muted-foreground">
                                    {it.label}
                                    {it.optional && <span className="text-muted-foreground/40"> · 选填</span>}
                                </span>
                                <span className={cn('inline-flex shrink-0 items-center gap-1.5 text-xs', it.ok ? 'text-brand' : 'text-muted-foreground/40')}>
                                    <span className={cn('h-1.5 w-1.5 rounded-full', it.ok ? 'bg-brand' : 'bg-muted-foreground/30')} />
                                    {it.ok ? (('okLabel' in it && it.okLabel) || '已填') : (it.optional ? '可跳过' : '待填')}
                                </span>
                            </div>
                        ))}
                    </CardContent>
                </Card>

                {/* [diaglaunch 2026-08-05 · 工单 §1.5] 本次会得到 · 四条带图标 */}
                <Card className="border border-border rounded-xl shadow-none @max-[880px]:order-6">
                    <CardHeader className="p-5 pb-3">
                        <CardTitle className="text-base">本次会得到</CardTitle>
                    </CardHeader>
                    <CardContent className="p-5 pt-0">
                        <ul data-testid="outcome-list" className="space-y-3 text-sm text-muted-foreground">
                            {[
                                { Icon: Eye, text: 'AI 搜索可见度（通义 / DeepSeek / Kimi / 豆包）' },
                                { Icon: Quote, text: '品牌引用来源与高频内容' },
                                { Icon: Target, text: '关键词机会与上榜空间' },
                                { Icon: Users, text: '竞品在 AI 答案里的表现对比' },
                            ].map(({ Icon, text }) => (
                                <li key={text} className="flex items-start gap-2.5">
                                    <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground/70" aria-hidden="true" />
                                    <span className="min-w-0 leading-snug">{text}</span>
                                </li>
                            ))}
                        </ul>
                        <p className="mt-3 text-xs text-muted-foreground/60">可离开页面，完成后通知</p>
                    </CardContent>
                </Card>

                {/* CTA · 必须留在 <form> 内(submit 双触发依赖 e.currentTarget.form)· [2026-06-03 静默扣费] 不前置展示价格 · totalDiagnosisCost 仍用于余额预检 + freeze hold */}
                <div
                    data-testid="launch-cta"
                    className={cn(
                        "space-y-2",
                        // 🔴 [#125] 窄档紧随诊断对象,排在题单之前
                        "@max-[880px]:order-2",
                        // 🔴 [#125 裁定乙 2026-09-06] 原来这里还有一组
                        //    窄档 + fine 指针下的 sticky / bottom-0 / z-10 三条,与配套的
                        //    ⚠️ 这里**刻意不写出完整类名**:浏览器底线闸扫源码里的容器查询 token,
                        //       把注释里提到的裸类名也算进「新变体」——散文会触发裸串锁(今天第 N 次)。
                        //    负外距 / 上边框 / 卡片底色 / 内距 那组 bar 装饰(窄档 + 鼠标才贴底)。
                        //    **整组删掉**,CTA 在任何档、任何指针类型下都留在流里。
                        //
                        //    删它的理由不是它有害,是**它让读数有两个解释**:
                        //    2026-09-06 生产量到 CTA 达标(bottom≤767),而那台机器报 fine pointer,
                        //    sticky 生效 —— 容器 y=642 h=125 ⇒ bottom 正好 767,**分毫不差**,
                        //    是贴底不是自然流。于是"首屏达标"到底是重排的功劳还是 sticky 的功劳,
                        //    这份读数说不清。「窄窗口 + 鼠标」那一格的价值,低于判据的可解释性。
                        //
                        //    v9(5d731493)那次是因为**真 BUG** 撤的(iOS Safari pinch-zoom 时
                        //    sticky 绑 visualViewport 不跟内容缩放,老板报「底部有个遮罩」);
                        //    这次是因为**可解释性**。两次理由不同,结论一样:CTA 不靠 sticky。
                        //    首屏靠的是 #125 的窄档重排(contents + order),那是唯一机制。
                        // [diaglaunch 2026-08-05 · 工单 §2] 单列档(容器 <880)CTA 贴底,长表单里随时能启动。
                        // 🔴 只对 pointer:fine(鼠标/触控板)开。触屏不开,是拿代价换来的:
                        //    2026-05-21 v9(5d731493)给这个按钮加过一模一样的 sticky bottom,
                        //    iOS Safari pinch-zoom 时 sticky 元素绑 visualViewport、不跟内容缩放,
                        //    老板报「底部有一个遮罩一直在搞事情」,v10 整条撤回(见本文件上方那段长注释)。
                        //    所以粗指针(触屏)一律留在自然流里 —— 工单 §2 那条在触屏上是**明写不做**,
                        //    不是漏做,交付说明里单列了一条偏离。
                    )}
                >
                    <FeatureTooltip
                        featureId="diagnosis_submit_btn"
                        stepId="first_diagnosis"
                        title="信息齐了 · 点这里开始体检"
                        content="AI 会在 5 分钟内出一份完整的 GEO 诊断报告, 看你的品牌在各家 AI 平台里的可见度"
                        side="top"
                        wrapClassName="relative block w-full"
                        disabled={!isSandboxActive() || tutorialStage !== 'submit'}
                    >
                        <Button
                            type="submit"
                            data-testid="launch-submit"
                            className="w-full bg-foreground text-background hover:bg-foreground/90 min-h-[48px] text-base font-semibold"
                            disabled={launchBlocked}
                            title={(!isDefensiveFlow && totalDiagnosisCost == null) ? (pricingError || '正在读取动态价目') : undefined}
                            onClick={(e) => {
                                // [P1-1 fix 2026-05-23 老板授权 + Codex Deploy-CTO SQL 实证]
                                // 老板报"开始 GEO 诊断 首点 0 反应 · 二点启动"
                                // 根因:React 18 hydration 慢 · form onSubmit 还未 attach · onClick 丢
                                // 修法:button 加 manual onClick 作为 fallback · 双绑定保证至少一个触发
                                // disabled 已防双点 · 触发后 loading=true 第二次 click 不重复
                                if (launchBlocked) return;
                                // 若 form 已 mounted · onSubmit 会 fire · 不重复触发
                                // 若 form 未 mounted(hydration race)· 这里手动触发
                                const form = (e.currentTarget as HTMLButtonElement).form;
                                if (form && typeof form.requestSubmit === 'function') {
                                    // 浏览器原生 · requestSubmit 触发 onSubmit handler(防 hydration race)
                                    e.preventDefault();
                                    form.requestSubmit();
                                }
                            }}
                        >
                            {totalDiagnosisCost == null ? (
                                /* 🔴 [阶段⑤ · 订正七④] 价没就绪时按钮**自己**说清在等什么,
                                   而不是显示一个上一份输入的旧价。这就是「正在重新算」瞬态。 */
                                pricePending ? '正在按你的题单算价…' : (priceError || '价目暂时读不到')
                            ) : loading ? (
                                <>
                                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                    正在初始化...
                                </>
                            ) : (
                                <span className="inline-flex items-center gap-2">
                                    开始品牌体检
                                    {/* 🔴 按钮上的价 = 唯一的价,且与绑住提交的 pricePreviewId 同一响应体。
                                        前端不做任何算术,直接渲染服务端给的 points。 */}
                                    <span data-testid="launch-price-points" className="opacity-80">
                                        · {totalDiagnosisCost} 算力
                                    </span>
                                </span>
                            )}
                        </Button>
                    </FeatureTooltip>
                    {/* 🔴 [P0 2026-09-01] 灰按钮**必须**自带一句人话。
                        侧栏「启动检查」说已填、按钮却灰 —— 两个谓词各说一半,
                        而屏幕上零个字解释,用户只能瞎猜(实测:被拦那一屏比能点那一屏
                        只多出品牌下拉里那句反向承诺)。 */}
                    {launchBlockedReason && (
                        <p data-testid="launch-blocked-reason" role="status"
                           className="text-[13px] text-muted-foreground">
                            {launchBlockedReason}
                        </p>
                    )}
                    <Button type="button" variant="ghost" className="w-full text-muted-foreground hover:text-foreground" onClick={() => navigate(-1)} disabled={loading}>
                        取消
                    </Button>
                </div>
            </aside>{/* /右栏 */}
            </div>{/* /grid 启动台 */}

            </form>

            </>
            )}
        </div>{/* /限宽内容容器 */}
        </div>{/* /@container 查询锚点 */}

            {/* 🔴 下面这几个弹窗**必须**留在 @container 之外:container-type 隐含 contain:layout,
              * 里面的 position:fixed 会相对那个容器定位、而不是相对视口 —— 全屏遮罩当场错位。
              * (confirmDialog / DeductDialog 是 Radix,走 portal 本就不受影响,一并放这儿更省心。) */}

            {/* [P1-11] 疑似重复品牌弹窗：使用已有 / 登记为别名 / 仍然新建 */}
            {showSimilarDialog && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
                    <div className="bg-background border border-border rounded-xl p-6 w-full max-w-md shadow-xl space-y-4">
                        <h3 className="text-base font-semibold">疑似已存在这个客户</h3>
                        <p className="text-sm text-muted-foreground">
                            你输入的「{formData.brandName}」和下面这些客户很像。如果是同一家公司，
                            用已有档案能让诊断、报价和监测数据都攒在一起；新建会各自独立计费、数据不互通。
                        </p>
                        <div className="space-y-2 max-h-60 overflow-y-auto">
                            {similarBrands.map(b => (
                                <div key={b.brand_id} className="flex items-center justify-between gap-3 p-3 border border-border rounded-lg">
                                    <div className="min-w-0">
                                        <p className="text-sm font-medium break-words leading-snug" title={b.name}>{b.name}</p>
                                        <p className="text-xs text-muted-foreground">
                                            {[b.industry, b.city].filter(Boolean).join(' · ') || '未填行业/城市'}
                                        </p>
                                    </div>
                                    <Button
                                        size="sm"
                                        variant="outline"
                                        className="shrink-0"
                                        onClick={async () => {
                                            // 使用已有品牌 = 换 brand_id + 用已有品牌名；不改任何归属
                                            pendingBrandIdRef.current = b.brand_id;
                                            setFormData(prev => ({
                                                ...prev,
                                                brandName: b.name,
                                                industry: b.industry || prev.industry,
                                                clientLocation: b.city || prev.clientLocation,
                                            }));
                                            similarAckRef.current = b.name;
                                            setShowSimilarDialog(false);
                                            await proceedWithDeductAndSubmit();
                                        }}
                                    >
                                        用这个客户
                                    </Button>
                                </div>
                            ))}
                        </div>
                        <p className="text-xs text-muted-foreground">
                            如果只是同一家公司的另一个叫法，建议用已有客户档案，然后到客户档案里把这个叫法登记成「品牌常用名」——
                            这样 AI 识别会同时认这两个名字，历史数据也不会分家。
                        </p>
                        <div className="flex gap-2 pt-1">
                            <Button variant="outline" className="flex-1" onClick={() => setShowSimilarDialog(false)}>
                                返回修改
                            </Button>
                            <Button
                                className="flex-1"
                                onClick={async () => {
                                    similarAckRef.current = formData.brandName.trim();
                                    setShowSimilarDialog(false);
                                    await handleSubmit({ preventDefault: () => {} } as React.FormEvent);
                                }}
                            >
                                确认是新客户，继续
                            </Button>
                        </div>
                    </div>
                </div>
            )}

            {/* 同名品牌冲突弹窗 */}
            {showConflictDialog && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
                    <div className="bg-background border border-border rounded-xl p-6 w-full max-w-md shadow-xl space-y-4">
                        <h3 className="text-base font-semibold">发现同名客户</h3>
                        <p className="text-sm text-muted-foreground">
                            「{formData.brandName}」已存在于其他账户下，请选择归属账户，或在您的账户下新建：
                        </p>
                        <div className="space-y-2 max-h-60 overflow-y-auto">
                            {conflictBrands.map(b => (
                                <div key={b.id} className="flex items-center justify-between p-3 border border-border rounded-lg">
                                    <div className="min-w-0 mr-3">
                                        <p className="text-sm font-medium break-words leading-snug" title={b.name}>{b.name}</p>
                                        <p className="text-xs text-muted-foreground">
                                            归属：{b.owner_name || '其他用户'}{b.industry ? ` · ${b.industry}` : ''}
                                        </p>
                                    </div>
                                    <Button
                                        size="sm"
                                        variant="outline"
                                        className="shrink-0"
                                        onClick={async () => {
                                            pendingBrandIdRef.current = b.id;
                                            setFormData(prev => ({ ...prev, brandName: b.name, industry: b.industry || prev.industry }));
                                            setShowConflictDialog(false);
                                            await proceedWithDeductAndSubmit();
                                        }}
                                    >
                                        选此客户
                                    </Button>
                                </div>
                            ))}
                        </div>
                        <div className="flex gap-2 pt-2">
                            <Button
                                variant="outline"
                                className="flex-1"
                                onClick={() => setShowConflictDialog(false)}
                            >
                                取消
                            </Button>
                            <Button
                                className="flex-1 bg-foreground text-background hover:bg-foreground/90"
                                onClick={async () => {
                                    setShowConflictDialog(false);
                                    await proceedWithDeductAndSubmit();
                                }}
                            >
                                在我的账户下新建
                            </Button>
                        </div>
                    </div>
                </div>
            )}

            {confirmDialog}
            {totalDiagnosisCost != null && <DeductDialog
                open={showDeductDialog}
                onOpenChange={setShowDeductDialog}
                featureCode={currentDiagnosisCost.code}
                featureName={currentDiagnosisCost.name}
                cost={totalDiagnosisCost}
                onConfirm={handleDeductConfirm}
                loading={deductLoading}
            />}
        </>
    );
}
