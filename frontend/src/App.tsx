import { lazy, Suspense, useState, useEffect, Component, type ErrorInfo, type ReactNode } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider, useAuth } from '@/context/AuthContext';
import { UserModeProvider, useUserMode } from '@/context/UserModeContext';
import { WalletProvider, useWallet } from '@/context/WalletContext';
import { PricingProvider } from '@/context/PricingContext';
import { ClientProvider } from '@/context/ClientContext';
import { OnboardingProvider } from '@/context/OnboardingContext';
import { OrganizationProvider } from '@/context/OrganizationContext';
import { ProtectedRoute } from '@/components/auth/ProtectedRoute';
import { AgentLevelGate } from '@/components/AgentLevelGate';
import { Layout } from '@/components/layout/Layout';
// [CTO-15.23 2026-05-05] 旧 C 端布局 import 已删除 · /c/* 路由全清
// [Deploy-CTO 2026-05-14] SEndLayout import 已删除 · /s 旧 SEnd 路由已归并 社媒工作台
import { PageLoading } from '@/components/ui/page-loading';
import { TOASTER_REQUEST_EVENT } from '@/lib/lazyToast';
const LazyToaster = lazy(() =>
    import('@/components/ui/sonner').then((module) => ({ default: module.Toaster })),
);

/** 官网(WO_293)。旧落地页 /landing 落到这里。 */
const OFFICIAL_SITE = 'https://omnirank.cn/';

/** 站外跳转:<Navigate> 只能站内跳。用 location.replace,历史里不留 /landing 这一格,返回键不会弹回来。 */
function ExternalRedirect({ to }: { to: string }) {
    useEffect(() => {
        window.location.replace(to);
    }, [to]);
    return null;
}

function DeferredToasterHost() {
    const [ready, setReady] = useState(() =>
        (window as Window & { __OMNIRANK_TOASTER_REQUESTED__?: boolean }).__OMNIRANK_TOASTER_REQUESTED__ === true,
    );
    useEffect(() => {
        const onRequest = () => setReady(true);
        window.addEventListener(TOASTER_REQUEST_EVENT, onRequest);

        // [F-2 根因] 全仓 186 个文件直接 `import { toast } from 'sonner'`,只有 19 个走
        // `@/lib/lazyToast`。而事件只由 lazyToast 触发 —— 于是一次会话里若没人先调过
        // lazyToast,所有直连 sonner 的 toast 都**没有宿主可渲染**,用户表现为"点了没反应"
        // (MyClientsPage 添加客户失败即是一例)。
        // 这里在首屏空闲后无条件挂载宿主:仍不占关键渲染路径(保留原有的延迟加载意图),
        // 但保证用户有机会点按钮之前宿主一定已经就位。
        const idle = (window as Window & {
            requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
        }).requestIdleCallback;
        let timer: number | undefined;
        if (typeof idle === 'function') idle(() => setReady(true), { timeout: 2000 });
        else timer = window.setTimeout(() => setReady(true), 1200);

        return () => {
            window.removeEventListener(TOASTER_REQUEST_EVENT, onRequest);
            if (timer !== undefined) window.clearTimeout(timer);
        };
    }, []);
    if (!ready) return null;
    return (
        <Suspense fallback={null}>
            <LazyToaster richColors position="top-center" />
        </Suspense>
    );
}

import { isRecoverableSessionError } from '@/lib/authoritativeSession';
import { isChunkLoadError, maybeReloadOnChunkError } from '@/lib/errorReporter';
import { startChunkHeal } from '@/lib/chunkHeal';

/** 全局错误边界：防止渲染错误导致白屏
 * [2026-05-15 Deploy-CTO 调试增强] 显示完整 error.message + stack 头 5 行
 *   原 slice(0, 100) 会截掉 React minified error 关键 args(如 invariant=31 args[]=object with keys ...)
 *   现完整显示 + 可展开 stack · 方便定位真凶 component */
// [2026-07-28] 导出仅为判别测试可挂载(scripts/ui-verify/harness-error-boundary)。
// 运行时入口仍是本文件内部使用,不改变任何行为。
export class GlobalErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null; componentStack: string | null }> {
    state = { error: null as Error | null, componentStack: null as string | null };
    static getDerivedStateFromError(error: Error) {
        // [修复 2026-07-28] chunk 失效要静默自愈,不能甩一页 stack trace 给用户。
        //   React 错误边界**就近捕获**:本边界在 main.tsx 那个之内,Lazy 路由的
        //   "Failed to fetch dynamically imported module" 先被这里接住,
        //   外层已有的静默 reload 于是从来没机会跑 —— 这就是老板看到报错页的原因。
        //   现在这里也判一次(复用 errorReporter 的单一来源,不再造第四份正则)。
        maybeReloadOnChunkError(error?.message || '');
        return { error, componentStack: null };
    }
    componentDidCatch(error: Error, info: ErrorInfo) {
        console.error('[ErrorBoundary]', error, info);
        this.setState({ componentStack: info.componentStack ?? null });
    }
    render() {
        if (this.state.error) {
            // [BUG-3 2026-07-27] 会话确认类错误是【可恢复】的(在途 / 超时 / 网络抖动),
            // 不该给用户看 stack trace。给人话 + 两个可操作出口:重试 / 重新登录。
            // 这是最后一道防线 —— 正常路径上调用点已改成等待,走不到这里。
            // [2026-07-28] 部署后旧 chunk 失效:上面已经排了静默 reload,这里只是
            //   reload 落地前的过渡画面。给人话,不给 stack —— 这不是用户的错,
            //   也不需要用户做任何事。(30s cooldown 内不再自动 reload 时,
            //   下面那个"刷新页面"按钮仍是有效出口。)
            if (isChunkLoadError(this.state.error)) {
                return (
                    <div style={{ padding: 24, textAlign: 'center', color: '#bbb', maxWidth: 480, margin: '64px auto', fontFamily: 'system-ui, sans-serif' }}>
                        <h2 style={{ color: '#fff', marginBottom: 12 }}>正在更新到新版本</h2>
                        <p style={{ fontSize: 14, marginBottom: 20, lineHeight: 1.7 }}>
                            刚刚发布了新版本,页面正在自动刷新。<br />你的数据没有丢失,稍等一下即可。
                        </p>
                        {/* [WO_276] 先刷掉浏览器缓存里的坏 chunk 响应再刷新 —— 缓存了 301 / 404 时,
                            单纯 reload 只会把同一份坏响应再读一遍(这个按钮以前就是这样「按了没用」)。 */}
                        <button onClick={() => { void startChunkHeal(this.state.error?.message || '', { force: true }).then(() => window.location.reload()); }}
                            style={{ padding: '8px 24px', borderRadius: 8, border: '1px solid #333', background: '#222', color: '#fff', cursor: 'pointer' }}>
                            立即刷新
                        </button>
                    </div>
                );
            }
            if (isRecoverableSessionError(this.state.error)) {
                return (
                    <div style={{ padding: 24, textAlign: 'center', color: '#bbb', maxWidth: 480, margin: '64px auto', fontFamily: 'system-ui, sans-serif' }}>
                        <h2 style={{ color: '#fff', marginBottom: 12 }}>网络有点慢，还没连上</h2>
                        <p style={{ fontSize: 14, marginBottom: 20, lineHeight: 1.7 }}>
                            正在确认你的登录状态，暂时没成功。<br />你的数据没有丢失，重试一下通常就好了。
                        </p>
                        <div style={{ display: 'flex', gap: 12, justifyContent: 'center' }}>
                            <button onClick={() => { this.setState({ error: null, componentStack: null }); window.location.reload(); }}
                                style={{ padding: '8px 24px', borderRadius: 8, border: '1px solid #333', background: '#222', color: '#fff', cursor: 'pointer' }}>
                                重试
                            </button>
                            <button onClick={() => { window.location.href = '/login'; }}
                                style={{ padding: '8px 24px', borderRadius: 8, border: '1px solid #333', background: 'transparent', color: '#bbb', cursor: 'pointer' }}>
                                重新登录
                            </button>
                        </div>
                    </div>
                );
            }
            const errMsg = this.state.error.message || String(this.state.error);
            const errStack = (this.state.error.stack || '').split('\n').slice(0, 5).join('\n');
            const compStack = (this.state.componentStack || '').split('\n').slice(0, 5).join('\n');
            return (
                <div style={{ padding: 24, textAlign: 'left', color: '#bbb', maxWidth: 720, margin: '0 auto', fontFamily: 'system-ui, sans-serif' }}>
                    <h2 style={{ color: '#fff', marginBottom: 12, textAlign: 'center' }}>页面出错了</h2>
                    <p style={{ fontSize: 13, marginBottom: 12, wordBreak: 'break-word', whiteSpace: 'pre-wrap', background: '#1a1a1a', padding: 12, borderRadius: 6 }}>
                        <strong style={{ color: '#fbbf24' }}>Error:</strong> {errMsg}
                    </p>
                    {errStack && (
                        <details style={{ marginBottom: 12 }}>
                            <summary style={{ cursor: 'pointer', fontSize: 12 }}>展开 stack trace</summary>
                            <pre style={{ fontSize: 11, background: '#1a1a1a', padding: 12, borderRadius: 6, overflowX: 'auto', whiteSpace: 'pre-wrap' }}>{errStack}</pre>
                        </details>
                    )}
                    {compStack && (
                        <details style={{ marginBottom: 16 }}>
                            <summary style={{ cursor: 'pointer', fontSize: 12 }}>展开 component stack</summary>
                            <pre style={{ fontSize: 11, background: '#1a1a1a', padding: 12, borderRadius: 6, overflowX: 'auto', whiteSpace: 'pre-wrap' }}>{compStack}</pre>
                        </details>
                    )}
                    <div style={{ textAlign: 'center' }}>
                        <button onClick={() => { this.setState({ error: null, componentStack: null }); window.location.reload(); }}
                            style={{ padding: '8px 24px', borderRadius: 8, border: '1px solid #333', background: '#222', color: '#fff', cursor: 'pointer' }}>
                            刷新页面
                        </button>
                    </div>
                </div>
            );
        }
        return this.props.children;
    }
}

// [CTO-15.23 2026-05-19] OnboardingGate 已移除 · /welcome 旧版废弃(老板拍板等重新设计)
// 之前逻辑:无 profile 用户 redirect /welcome · 现在所有用户直接进 Dashboard
// 没 brand 的用户在 Dashboard 自然引导(后续重新设计 welcome 时再加新组件)

// ——— Lazy imports: 每个页面独立 chunk ———
const loadLoginPage = () => import('@/pages/Login/LoginPage');
const LoginPage = lazy(loadLoginPage);
const AgreementUpdatePage = lazy(() => import('@/pages/Login/AgreementUpdatePage'));
// /help 直达角色操作说明书；/help/home 是独立视频教程主页。
const HelpDocs = lazy(() => import('@/pages/Help/HelpDocs'));
const HelpCenter = lazy(() => import('@/pages/Help/HelpCenter'));
const HelpFAQ = lazy(() => import('@/pages/Help/HelpFAQ'));
const FeedbackPage = lazy(() => import('@/pages/Feedback/FeedbackPage'));
// [2026-05-18 帮助中心 Phase 3] 管理员双 tab · FAQ CRUD + 反馈收件箱
const HelpCenterAdmin = lazy(() => import('@/pages/Admin/HelpCenterAdmin'));
// [2026-05-18 帮助中心 Phase 3 v2] FAQ 独立编辑页 · 替代原 Sheet 抽屉
const FAQItemEditPage = lazy(() => import('@/pages/Admin/HelpCenterAdmin/FAQItemEditPage'));
// [2026-07-01] AI 运维控制塔
const AiOpsCenter = lazy(() => import('@/pages/Admin/AiOpsCenter'));
// [2026-07-04] 营销军师控制台
const MarketingAdvisor = lazy(() => import('@/pages/Admin/MarketingAdvisor'));
// [CTO-15.23 2026-05-05] /c 端豆包式对话已废弃 · CEnd* lazy import 已删除
// 老板拍板: /c/* 全部 redirect 到旧版 Dashboard
// 源文件 frontend/src/pages/CEnd/** + components/c_end/** 暂保留(7 天观察期)
// [老板 2026-06-03 终态拍板] 老 C 端(pages/CEnd/** + components/c_end/** + components/agent/** 豆包对话 UI)完全废弃
//   → 所有 AI 不用管这个入口(勿改 / 勿清扣费角标 / 勿对标) · 下个清欠债 PR 删源文件
// [开源 E3 · 前端 · 2026-10-01] 社媒工作台 社媒工作台(/s 社媒族)整目录删除;/s/:token 选词报价页在役保留
// [Deploy-CTO 2026-05-14] 老 SEnd 7 个 lazy import 已删 · /s/* 全归 社媒工作台 · 0 Route 引用
const TrialPassReviewPage = lazy(() => import('@/pages/Agent/TrialPassReviewPage'));
// [V3.5 W2 2026-05-26] 代理工作台 4 页 + admin 工厂治理 4 页
const loadAgentInventoryCenter = () => import('@/pages/Agent/InventoryCenter');
const AgentInventoryCenter = lazy(loadAgentInventoryCenter);
// [工单 P0-2] 服务商发展下级服务商的入口页 —— 此前渠道关系只有管理员能建,
// 服务商侧完全没有入口,这正是 2026-08-12 事故的直接卡点。
const loadAgentChannelPartners = () => import('@/pages/Agent/ChannelPartners');
const AgentChannelPartners = lazy(loadAgentChannelPartners);
const loadAgentPricingCenter = () => import('@/pages/Agent/PricingCenter');
const AgentPricingCenter = lazy(loadAgentPricingCenter);
const AgentSettlementCenter = lazy(() => import('@/pages/Agent/SettlementCenter'));
const AgentPromotionCenter = lazy(() => import('@/pages/Agent/PromotionCenter'));
const loadAgreementGate = () => import('@/components/AgreementGate').then(m => ({ default: m.AgreementGate }));
const AgreementGate = lazy(loadAgreementGate);

/**
 * The pricing route used to load serially: auth -> agreement chunk/check -> pricing chunk.
 * Start the page chunk while the read-only agreement check is in flight so the gate remains
 * authoritative without extending the skeleton by another network round trip.
 */
function AgentPricingRoute() {
    useEffect(() => {
        void loadAgentPricingCenter();
    }, []);

    return <AgreementGate><AgentPricingCenter /></AgreementGate>;
}
const AdminSettlementsReview = lazy(() => import('@/pages/Admin/SettlementsReview'));
const AdminRefundConsole = lazy(() => import('@/pages/Admin/RefundConsole'));  // [2A-2] V3.5 充值退款工单
const AdminTaxProfiles = lazy(() => import('@/pages/Admin/TaxProfiles'));
const AdminInventoryAudit = lazy(() => import('@/pages/Admin/InventoryAudit'));
// [invrel 2026-08-13 · 工单 §P1-2] 服务商库存与关系治理 —— 后端三端点早已存在但零页面
const AdminAgentInventoryGovernance = lazy(() => import('@/pages/Admin/AgentInventoryGovernance'));
// [2026-06-07] 算力定价中心(合并 算力包管理 PricingMgmt + 定价系数配置 PricingConfig)
// 旧两页源文件保留磁盘待回滚 · 路由已重定向到 pricing-center · 旧 lazy import 已移除(无引用)
const loadAdminPricingCenter = () => import('@/pages/Admin/PricingCenter');
const AdminPricingCenter = lazy(loadAdminPricingCenter);
const AdminChannelTier = lazy(() => import('@/pages/Admin/ChannelTierAdmin'));
// [V3.5 W3+W4 2026-05-26] 客户工作台 + 协议 + admin 治理
const AgreementPage = lazy(() => import('@/pages/Agent/AgreementPage'));
const CustomerCreditWallet = lazy(() => import('@/pages/Customer/CreditWallet'));
const loadCustomerBuyCredit = () => import('@/pages/Customer/BuyCredit');
const CustomerBuyCredit = lazy(loadCustomerBuyCredit);
const AdminBindingDisputes = lazy(() => import('@/pages/Admin/BindingDisputes'));
const AdminInventoryAuditHistory = lazy(() => import('@/pages/Admin/InventoryAuditHistory'));
// [V3.6 白标 2026-05-30] admin 白标授权页
const AdminWhitelabelGrant = lazy(() => import('@/pages/Admin/WhitelabelGrant'));
const TermsPage = lazy(() => import('@/pages/Legal/TermsPage'));
const PrivacyPage = lazy(() => import('@/pages/Legal/PrivacyPage'));
const RegisterPage = lazy(() => import('@/pages/Login/RegisterPage'));
const ChangePasswordPage = lazy(() => import('@/pages/Login/ChangePasswordPage'));
const loadDashboard = () => import('@/pages/Dashboard').then(m => ({ default: m.Dashboard }));
const Dashboard = lazy(loadDashboard);
const NewDiagnosis = lazy(() => import('@/pages/Diagnosis/NewDiagnosis').then(m => ({ default: m.NewDiagnosis })));
const DiagnosisProgress = lazy(() => import('@/pages/Diagnosis/DiagnosisProgress').then(m => ({ default: m.DiagnosisProgress })));
const DiagnosisReport = lazy(() => import('@/pages/Diagnosis/DiagnosisReport').then(m => ({ default: m.DiagnosisReport })));
const PricingCenter = lazy(() => import('@/pages/Quote').then(m => ({ default: m.PricingCenter })));
const WritingCenter = lazy(() => import('@/pages/Writing/WritingCenter').then(m => ({ default: m.WritingCenter })));
const WritingHall = lazy(() => import('@/pages/Writing/WritingHall').then(m => ({ default: m.WritingHall })));
const WritingWorkspace = lazy(() => import('@/pages/Writing/WritingWorkspace').then(m => ({ default: m.WritingWorkspace })));
// [WP4 2026-08-17 · 规格 01 §4] 单篇校对工作台需要一条**真路由** ——
// 没有路由的页面在浏览器测试里根本到不了,判据只能退化成源码字符串扫描,
// 而规格 01 §10 明写「只有 TSX 字符串锁,没有真实浏览器行为测试」= 不可签收。
const ImageNoteDetailRoute = lazy(() => import('@/pages/Writing/ImageNoteDetailRoute').then(m => ({ default: m.ImageNoteDetailRoute })));
const NotFound = lazy(() => import('@/pages/NotFound').then(m => ({ default: m.NotFound })));
const ReferenceLibrary = lazy(() => import('@/pages/Writing/ReferenceLibrary').then(m => ({ default: m.ReferenceLibrary })));
const ImitatedArticleDetail = lazy(() => import('@/pages/Writing/ImitatedArticleDetail').then(m => ({ default: m.ImitatedArticleDetail })));
const WritingProgress = lazy(() => import('@/pages/Writing/WritingProgress').then(m => ({ default: m.WritingProgress })));
const loadPublishCenter = () => import('@/pages/Publishing/PublishCenter').then(m => ({ default: m.PublishCenter }));
const PublishCenter = lazy(loadPublishCenter);
const loadPublishHistory = () => import('@/pages/Publishing/PublishHistory').then(m => ({ default: m.PublishHistory }));
const PublishHistory = lazy(loadPublishHistory);
const HistoryList = lazy(() => import('@/pages/History/HistoryList').then(m => ({ default: m.HistoryList })));
const SettingsPage = lazy(() => import('@/pages/Settings/SettingsPage').then(m => ({ default: m.SettingsPage })));
const BrandList = lazy(() => import('@/pages/Brands').then(m => ({ default: m.BrandList })));
const BrandDetail = lazy(() => import('@/pages/Brands').then(m => ({ default: m.BrandDetail })));
const EmployeeHall = lazy(() => import('@/pages/Employees').then(m => ({ default: m.EmployeeHall })));
const MeetingRoom = lazy(() => import('@/pages/Employees').then(m => ({ default: m.MeetingRoom })));
const MeetingHistory = lazy(() => import('@/pages/Employees').then(m => ({ default: m.MeetingHistory })));
const TaskBoard = lazy(() => import('@/pages/Employees').then(m => ({ default: m.TaskBoard })));
const EmployeeSettings = lazy(() => import('@/pages/Employees').then(m => ({ default: m.EmployeeSettings })));
const Workspace = lazy(() => import('@/pages/Workspace').then(m => ({ default: m.Workspace })));
// [开源 E3 · 前端 · 2026-10-01] 旧社媒操盘手 /social-ops(旧社媒操盘手 + components/social)与顾问对话页整删
// [防御型 GEO 发布 v2 · 2026-08-21] UI-34 / UI-35 / UI-36 三页。
// 后端 façade = /api/defensive-geo/publish/**(api/defensive_publish_api.py)。
// [包E 2026-08-24] 执行侧接线完成(dispatch / reconcile / 激活物化三条 job
// 真的在 register_v32_core_tasks 里推进了),客户入口重新接上。
// [合流 2026-08-24 Review-CTO · 预裁终态] 重开**不再直挂**确认/状态页:
// 两条客户路由统一经包H 的 PublishDeepLink 入口闸(闸开 = 直达确认屏;
// 将来再关闸时渲染 typed 说明 + 出口,不是白页)。直挂 lazy 常量已删 ——
// gate2 判据 R1-① 「通向确认组件的 import() 恒零」锁着这一点。
const DefgeoSettlementReviewQueue = lazy(() => import('@/pages/DefensivePublish/SettlementReviewQueue'));
// [包H · U-5/U-6 2026-08-23] 交付待办聚合页 + 深链落点。
// 🔴 深链落点**不是**把终审 P0-1 关掉的客户入口原样接回来:
//    PublishDeepLink 先问 publishEntryGate,入口关着时渲染 typed 说明
//    (「还没开放 · 没有扣除任何算力」)+ 出口,不渲染确认页。
//    真正挡钱的仍是后端 _CUSTOMER_PUBLISH_ENTRY_OPEN(三个冻钱端点第一行就拒)。
//    补这两条路由的理由:U-6 要求「审批通过 → 深链直达确认页」,
//    落点不存在时她点开通知看到的是白页 —— 白页解释不了任何事。
const DefgeoDeliveryTodo = lazy(() => import('@/pages/DefensivePublish/DeliveryTodoList'));
const DefgeoPublishDecisionLink = lazy(() => import('@/pages/DefensivePublish/PublishDeepLink').then(m => ({ default: m.PublishDecisionDeepLink })));
const DefgeoPublishCommandLink = lazy(() => import('@/pages/DefensivePublish/PublishDeepLink').then(m => ({ default: m.PublishCommandDeepLink })));
const KnowledgeManager = lazy(() => import('@/pages/Knowledge/KnowledgeManager'));
const MyBrandPage = lazy(() => import('@/pages/Brand/MyBrandPage'));
const MyClientsPage = lazy(() => import('@/pages/Brand/MyClientsPage'));
const BrandDetailPage = lazy(() => import('@/pages/Brand/BrandDetailPage'));
const loadMonitoringPage = () => import('@/pages/Monitoring');
const MonitoringPage = lazy(loadMonitoringPage);
const ObservationMethodology = lazy(() => import('@/features/geoObservation').then(m => ({ default: m.MethodologyPage })));
const AdminObservationCenter = lazy(() => import('@/features/geoObservation').then(m => ({ default: m.AdminObservationCenter })));
const ReportManagement = lazy(() => import('@/pages/Reports'));
const PortalLogin = lazy(() => import('@/pages/Portal').then(m => ({ default: m.PortalLogin })));
const PortalDashboard = lazy(() => import('@/pages/Portal').then(m => ({ default: m.PortalDashboard })));
const loadUserManagement = () => import('@/pages/Admin/UserManagement');
const UserManagement = lazy(loadUserManagement);
const AdminUserDetail = lazy(() => import('@/pages/Admin/AdminUserDetail'));
const RoleManagement = lazy(() => import('@/pages/Admin/RoleManagement'));
const ProfilePage = lazy(() => import('@/pages/Account/ProfilePage').then(m => ({ default: m.ProfilePage })));
const TVDashboard = lazy(() => import('@/pages/Home/TVDashboard').then(m => ({ default: m.TVDashboard })));
const NotificationCenter = lazy(() => import('@/pages/Notifications/NotificationCenter').then(m => ({ default: m.NotificationCenter })));
const OrganizationCenter = lazy(() => import('@/pages/Organization/OrganizationCenter'));
const OrganizationInviteAccept = lazy(() => import('@/pages/Organization/OrganizationInviteAccept'));
const OrganizationProductConfig = lazy(() => import('@/pages/Admin/OrganizationProductConfig'));
const AuditLogs = lazy(() => import('@/pages/Admin/AuditLogs'));
const AgentAudit = lazy(() => import('@/pages/Admin/AgentAudit'));
const AgentLoopControl = lazy(() => import('@/pages/Admin/AgentLoopControl'));
const AgentMetrics = lazy(() => import('@/pages/Admin/AgentMetrics'));
const AgentTrace = lazy(() => import('@/pages/Admin/AgentTrace'));
// CTO-15.23 2026-05-09 · LLM 成本监控 dashboard
const LLMCostDashboard = lazy(() => import('@/pages/Admin/LLMCostDashboard'));
const FinanceCenter = lazy(() => import('@/pages/Admin/FinanceCenter'));
const ResearchMonitor = lazy(() => import('@/pages/Admin/ResearchMonitor'));
const GeoPlacementFlywheel = lazy(() => import('@/pages/Admin/GeoPlacementFlywheel'));
const OrderManagementPage = lazy(() => import('@/pages/OrderManagement/OrderManagementPage').then(m => ({ default: m.OrderManagementPage })));
const InsightsCenter = lazy(() => import('@/pages/Insights/InsightsCenter'));
const GeoResearchCenter = lazy(() => import('@/pages/GeoResearch/GeoResearchCenter'));
const PlacementCenter = lazy(() => import('@/pages/Placement/PlacementCenter'));
const SelectionPage = lazy(() => import('@/pages/Selection').then(m => ({ default: m.SelectionPage })));
const MaterialConfirmPage = lazy(() => import('@/pages/MaterialConfirm/MaterialConfirmPage').then(m => ({ default: m.MaterialConfirmPage })));

// 公开页面
const SharedReport = lazy(() => import('@/pages/Public/VersionAwareSharedReport'));
const SharedProfile = lazy(() => import('@/pages/Public/SharedProfile'));
const PublicQuote = lazy(() => import('@/pages/Public/PublicQuote'));

// [CTO-15.23 2026-05-19] WelcomePage lazy import 移除(/welcome 路由整体废弃 · 等老板重新设计)

// 积分系统
const WalletPage = lazy(() => import('@/pages/Wallet/WalletPage'));
const BankCardsPage = lazy(() => import('@/pages/Wallet/BankCardsPage'));
// V3.3.1 服务费流水(L2 才显示)
const ServiceFeeHistoryPage = lazy(() => import('@/pages/Wallet/ServiceFeeHistoryPage'));
const PricingPage = lazy(() => import('@/pages/Pricing/PricingPage'));
// Phase 07: Social Studio 订阅 V3.1
const SubscriptionPlansPage = lazy(() => import('@/pages/Pricing/SubscriptionPlansPage'));
const SubscriptionManagementPage = lazy(() => import('@/pages/Subscription/SubscriptionManagementPage'));
const SubscriptionSigningPage = lazy(() => import('@/pages/Subscription/SubscriptionSigningPage'));

// v1.1 审核制: 合作伙伴计划
const PartnerAbout = lazy(() => import('@/pages/Partner/PartnerAbout'));
const PartnerApplyStep1 = lazy(() => import('@/pages/Partner/PartnerApplyStep1'));
const PartnerApplyStep2 = lazy(() => import('@/pages/Partner/PartnerApplyStep2'));
const PartnerApplyStep3 = lazy(() => import('@/pages/Partner/PartnerApplyStep3'));
const PartnerStatus = lazy(() => import('@/pages/Partner/PartnerStatus'));
const AgreementViewer = lazy(() => import('@/pages/Partner/AgreementViewer'));
const PartnerApplicationsReview = lazy(() => import('@/pages/Admin/PartnerApplicationsReview'));
const IndustryCorrectionsAdmin = lazy(() => import('@/pages/Admin/IndustryCorrectionsAdmin'));
const MediaDirectoryAdmin = lazy(() => import('@/pages/Admin/MediaDirectoryAdmin'));
const WithdrawalReviewPage = lazy(() => import('@/pages/Admin/WithdrawalReviewPage'));
const DiagnosisFundExceptions = lazy(() => import('@/pages/Admin/DiagnosisFundExceptions'));  // [返工4 P2] 诊断资金异常处置台
const AuditorRejectionRate = lazy(() => import('@/pages/Admin/AuditorRejectionRate'));

// 推荐/推广
// [推广页统一 2026-07-28] /referral 切到普通用户版 InviteCenter(按新推广中心骨架);
// 旧 ReferralCenter 归档保留(pages/Referral/ReferralCenter.tsx,回滚参考,不再路由)。
const InviteCenter = lazy(() => import('@/pages/Referral/InviteCenter'));
// 2026-05-25 删:QuoteCenter (快速录单) 已下架 · 只保留 /pricing 在线报价单一入口
const WhitelabelSettings = lazy(() => import('@/pages/Agent/WhitelabelSettings').then(m => ({ default: m.WhitelabelSettings })));
const ProfitDashboard = lazy(() => import('@/pages/Agent/ProfitDashboard').then(m => ({ default: m.ProfitDashboard })));
const QuotePreview = lazy(() => import('@/pages/Agent/QuotePreview').then(m => ({ default: m.QuotePreview })));
const LeadsPage = lazy(() => import('@/pages/Agent/LeadsPage'));

// v3.3 / v3.4 GEO 全自动托管
const ManagedListPage = lazy(() => import('@/pages/Managed/ManagedListPage'));
const ManagedDetailPage = lazy(() => import('@/pages/Managed/ManagedDetailPage'));
const BrandDashboardPage = lazy(() => import('@/pages/Managed/BrandDashboardPage'));
const MaterialCenterPage = lazy(() => import('@/pages/MaterialCenter/MaterialCenterPage'));
const ManagedCampaignsAdmin = lazy(() => import('@/pages/Admin/ManagedCampaignsAdmin'));
// 营销物料工厂 · 用户侧独立页(入口接线 2026-07-05:三身份侧栏可达,海报/朋友圈文案扣算力自助)
const MarketingMaterialsPage = lazy(() => import('@/pages/MarketingMaterials'));

// [CTO-15.23 2026-05-05] M3 双端工作台已废弃 · 28 个 M3* lazy import 已删除
// 老板拍板: 用户登录后不再进入 M3 路由 · /m3/* 全部 redirect 到旧版 Dashboard
// 源文件 frontend/src/pages/M3/** + components/m3/** 暂保留(7 天观察期 · B 方案精神)
// 7 天稳定后下一个清欠债 PR 删源文件
// [老板 2026-06-03 终态拍板] 老 M3(pages/M3/** + components/m3/**)完全废弃
//   → 所有 AI 不用管这个入口(勿改 / 勿清扣费角标) · 下个清欠债 PR 删源文件
//
// CTO-E 2026-04-26 · 客户资料补全邀请(C 端公开链接 · 跟 M3 无关 · 保留)
const IntakeFillPage = lazy(() => import('@/pages/Intake/IntakeFillPage'));
const InvitePage = lazy(() => import('@/pages/Invite/InvitePage'));

// Keep route components lazy, but start the chunk for the current URL while
// the authoritative identity read is in flight. This downloads code only;
// ProtectedRoute still owns every permission decision and API remains idle.
if (typeof window !== 'undefined') {
    const path = window.location.pathname;
    const loaders: Array<() => Promise<unknown>> = [];
    if (path === '/') loaders.push(loadDashboard);
    else if (path === '/login') loaders.push(loadLoginPage);
    else if (path === '/customer/recharge') loaders.push(loadCustomerBuyCredit);
    else if (path === '/agent/pricing') loaders.push(loadAgreementGate, loadAgentPricingCenter);
    else if (path === '/agent/inventory') loaders.push(loadAgentInventoryCenter);
    else if (path === '/agent/channel-partners') loaders.push(loadAgentChannelPartners);
    else if (path === '/monitoring') loaders.push(loadMonitoringPage);
    else if (path === '/publish/history') loaders.push(loadPublishHistory);
    else if (path === '/publish') loaders.push(loadPublishCenter);
    else if (path === '/admin/users') loaders.push(loadUserManagement);
    else if (path === '/admin/pricing-center') loaders.push(loadAdminPricingCenter);
    for (const load of loaders) void load().catch(() => undefined);
}

/** /register?ref=XXX → /login?mode=register&ref=XXX（保留所有 query params） */
function RegisterRedirect() {
    const search = window.location.search;
    const params = new URLSearchParams(search);
    params.set('mode', 'register');
    return <Navigate to={`/login?${params.toString()}`} replace />;
}

// [2026-06-01 WJ P2 · Codex smoke] /invite/:code 路径形式兼容
//   公开邀请链接常被手写/转发成 /invite/CODE(而非 /invite?ref=CODE)→ 之前空白
//   统一 redirect 到 /invite?ref=CODE · InvitePage 仍单一读 ?ref · 逻辑不分叉
function InviteCodeRedirect() {
    const { pathname, search } = window.location;
    const code = decodeURIComponent(pathname.replace(/^\/invite\//, '').replace(/\/+$/, ''));
    const params = new URLSearchParams(search);
    if (code && !params.get('ref')) params.set('ref', code);
    return <Navigate to={`/invite?${params.toString()}`} replace />;
}

/**
 * CTO-15.20 桥接重设计 · C.2:旧版默认路由 / 跳 /m3/sales/today
 *
 * 默认登录后跳 M3 客户工作台 · 双轨明示:
 *   - localStorage.omnirank_m3_optout === '1' → 留在旧版 Dashboard(M3 侧栏 切回旧版按钮设)
 *   - 否则 → /m3/sales/today
 * /dashboard 路由保留作为旧版 stat tile 看板(C.5 · 老代理偶尔看)
 */
// CTO-15.21 v5 (2026-04-28 老板拍板):
//   M3 已废弃 · 测试版本 · 默认 / 跳旧版 Dashboard · 不再 Navigate /m3/sales/today
//   旧版主页 / + sidebar"今日看板" 入口蒸馏 M3 M3 今日销售页 优先级 3 层
function DefaultRouteGate() {
    return <Dashboard />;
}


function App() {
    return (
        <GlobalErrorBoundary>
        <Router>
            <AuthProvider>
            <OrganizationProvider>
            <UserModeProvider>
            <WalletProvider>
            <OnboardingProvider>
            <PricingProvider>
            <DeferredToasterHost />
                <Suspense fallback={<PageLoading />}>
                    <Routes>
                        {/* 电视大屏（仅管理员 + 二次门禁 · 板块D 2026-07-22 收紧，对齐下方 ai-ops 先例） */}
                        <Route path="/tv" element={<ProtectedRoute requiredModule="users"><TVDashboard /></ProtectedRoute>} />

                        {/* [2026-07-01] AI 运维控制塔 · 顶层全屏路由(移出 <Layout> · 无 app chrome · admin-only) */}
                        <Route path="/admin/ai-ops" element={<ProtectedRoute requiredModule="users"><AiOpsCenter /></ProtectedRoute>} />
                        <Route path="/admin/ai-ops/tasks/:taskId" element={<ProtectedRoute requiredModule="users"><AiOpsCenter /></ProtectedRoute>} />
                        <Route path="/admin/ai-ops/reports" element={<ProtectedRoute requiredModule="users"><AiOpsCenter /></ProtectedRoute>} />
                        <Route path="/admin/ai-ops/reports/:date" element={<ProtectedRoute requiredModule="users"><AiOpsCenter /></ProtectedRoute>} />

                        {/* [2026-07-04] 营销军师控制台 · 顶层全屏路由(移出 <Layout> · admin-only) */}
                        <Route path="/admin/marketing" element={<ProtectedRoute requiredModule="users"><MarketingAdvisor /></ProtectedRoute>} />
                        <Route path="/admin/marketing/:section" element={<ProtectedRoute requiredModule="users"><MarketingAdvisor /></ProtectedRoute>} />
                        <Route path="/admin/marketing/cases/:caseId" element={<ProtectedRoute requiredModule="users"><MarketingAdvisor /></ProtectedRoute>} />

                        {/* 公开路由 */}
                        {/* [2026-09-27 · Owner「把社媒踢出去」] 旧落地页下线(挂着作废的套餐价与社媒工具)。
                            WO_293 官网上线后落到官网 omnirank.cn(在应用里跳,不改容器 nginx:同一班 A 也改 nginx.conf)。 */}
                        <Route path="/landing" element={<ExternalRedirect to={OFFICIAL_SITE} />} />
                        <Route path="/login" element={<LoginPage />} />
                        <Route path="/agreement-update" element={<AgreementUpdatePage />} />
                        <Route path="/register" element={<RegisterRedirect />} />
                        <Route path="/change-password" element={
                            <ProtectedRoute><ChangePasswordPage /></ProtectedRoute>
                        } />
                        <Route path="/public/report/:id" element={<SharedReport />} />
                        <Route path="/public/creator/:shareCode" element={<SharedProfile />} />
                        <Route path="/q/:code" element={<PublicQuote />} />
                        {/* v3.2: 法律协议页（公开访问） */}
                        <Route path="/terms" element={<TermsPage />} />
                        <Route path="/privacy" element={<PrivacyPage />} />
                        {/* v1_3 (CTO-15.1 2026-04-19) 代理合作协议独立查看页 —
                            从代理端 Layout 抽出，避免泄漏代理侧边栏给普通用户/准代理（老板截图反馈）*/}
                        <Route path="/partner/agreement/:version" element={
                            <ProtectedRoute><AgreementViewer /></ProtectedRoute>
                        } />
                        {/* [CTO-15.23 2026-05-19] /welcome Route 移除 · 旧版废弃等重新设计 */}
                        <Route path="/m/:token" element={<MaterialConfirmPage />} />
                        {/* CTO-E 2026-04-26 · 客户资料补全公开页(无登录) */}
                        <Route path="/intake/:token" element={<IntakeFillPage />} />
                        {/* [2026-05-30 B方案] 公开邀请落地页 · 扫码二维码落地(未登录可见·主显代理品牌+邀请码+注册引导) */}
                        <Route path="/invite" element={<InvitePage />} />
                        <Route path="/organization/invite" element={<OrganizationInviteAccept />} />
                        {/* [2026-06-01 WJ P2] /invite/:code 路径形式兼容 → redirect /invite?ref=:code(防手写/转发成路径形式空白) */}
                        <Route path="/invite/:code" element={<InviteCodeRedirect />} />
                        <Route path="portal" element={<PortalLogin />} />
                        <Route path="portal/:token" element={<PortalLogin />} />
                        <Route path="portal/dashboard" element={<PortalDashboard />} />

                        {/* [CTO-15.23 2026-05-05] C 端豆包对话已废弃 · 全部 redirect 到 / 旧版 Dashboard
                            · 老用户 bookmark /c/chat /c/history 等不会 404 · 自动回旧版
                            · 源文件 frontend/src/pages/CEnd/** 暂保留 7 天观察期 · 稳定后下个 PR 删
                            · [老板 2026-06-03 终态拍板] 完全废弃 · 所有 AI 不用管此入口 */}
                        <Route path="/c" element={<Navigate to="/" replace />} />
                        <Route path="/c/*" element={<Navigate to="/" replace />} />

                        {/* 2026-05-13 (老板"跳转到 GEO 那边去下单"反馈):
                            订阅相关 3 路由从 GEO <Layout /> 子路由内移出,变成独立路由(无 GEO sidebar)。
                            原嵌套位置 line ~513-515 已删,前后对照: 用户从 /s 顶栏点套餐胶囊 → 跳本路由 →
                            页面体感"留在社媒板块"(无 GEO sidebar 干扰) · SubscriptionPlansPage 内部有"返回小榜"按钮防迷路 */}
                        <Route path="/pricing-plans" element={<ProtectedRoute><SubscriptionPlansPage /></ProtectedRoute>} />
                        <Route path="/subscription/manage" element={<ProtectedRoute><SubscriptionManagementPage /></ProtectedRoute>} />
                        <Route path="/subscription/sign" element={<ProtectedRoute><SubscriptionSigningPage /></ProtectedRoute>} />
                        {/* [开源 E3 · 前端 · 2026-10-01] 社媒 /s 族(钱包 / 登录 / 审批 / 匹配 / 各模块)与 /social、/social-studio 跳转随
                            社媒工作台 整删;只留下面这条在役的客户选词报价链接。单段旧书签(/s/login 等)会落到选词页的「链接无效」空态。 */}
                        <Route path="/s/:token" element={<SelectionPage />} />

                        {/* [CTO-15.23 2026-05-05] M3 双端工作台已废弃 · 全部 redirect 到 / 旧版 Dashboard
                            · 老用户 bookmark /m3/sales/today /m3/customer/X 等不会 404 · 自动回旧版
                            · /m3/intake/review/:submissionId 是代理审核客户资料补全(C 端公开链接相关) · 也回 / 旧版有同等入口
                            · 源文件 frontend/src/pages/M3/** + components/m3/** 暂保留 7 天观察期 · 稳定后下个 PR 删
                            · CEndDrawer/SEndDrawer "切到工作台"按钮 + 任何跳 /m3 入口本 commit 一并删除
                            · [老板 2026-06-03 终态拍板] 完全废弃 · 所有 AI 不用管此入口 */}
                        {/* [2026-05-18 帮助中心 Layer 3] /m3/help 老 bookmark 兼容 · redirect 到新 /help
                            必须放在 /m3/* catchall 前 · 否则会被吃掉 */}
                        <Route path="/m3/help" element={<Navigate to="/help" replace />} />
                        <Route path="/m3" element={<Navigate to="/" replace />} />
                        <Route path="/m3/*" element={<Navigate to="/" replace />} />


                        {/* 受保护路由（代理端完整工作台） */}
                        <Route path="/" element={
                            <ProtectedRoute><Layout /></ProtectedRoute>
                        }>
                            {/* CTO-15.21 v5:M3 废弃 · 默认 / = 旧版 Dashboard · 蒸馏 M3 今日销售页 到 /dashboard/today */}
                            <Route index element={<DefaultRouteGate />} />
                            <Route path="dashboard" element={<Dashboard />} />
                            {/* [CTO-15.23 2026-05-05] /dashboard/today 原嵌 <M3SalesToday /> · M3 已废弃 → redirect /dashboard
                                老 sidebar 入口 bookmark 自动跳旧版 Dashboard · 不影响代理日常使用 */}
                            <Route path="dashboard/today" element={<Navigate to="/dashboard" replace />} />
                            <Route path="diagnosis/new" element={<ProtectedRoute requiredModule="diagnosis"><NewDiagnosis /></ProtectedRoute>} />
                            {/* commit 22 (CTO-15.2 2026-04-19): 去掉 requiredModule="diagnosis" L0 也能看自己诊断报告
                                老板严重反馈: C 端用户从 /c/history?tab=reports 点报告卡 → iframe 弹登录页
                                根因: requiredModule="diagnosis" 拦 L0(没有 module 权限) → Navigate("/") → 嵌套陷死循环 → 弹登录
                                后端 auth/brand_access.py:104 已校验"用户是否有权访问指定报告",前端 module 是冗余二道门
                                代理端用户登录 + 后端 API 校验依然 work,删 module 零副作用 */}
                            <Route path="diagnosis/progress/:id" element={<ProtectedRoute><DiagnosisProgress /></ProtectedRoute>} />
                            <Route path="diagnosis/report/:id" element={<ProtectedRoute><DiagnosisReport /></ProtectedRoute>} />
                            <Route path="pricing" element={<ProtectedRoute requiredModule="quote"><PricingCenter /></ProtectedRoute>} />
                            <Route path="quote" element={<ProtectedRoute requiredModule="quote"><PricingCenter /></ProtectedRoute>} />
                            <Route path="online-quote" element={<ProtectedRoute requiredModule="quote"><PricingCenter /></ProtectedRoute>} />
                            <Route path="monitoring" element={<ProtectedRoute requiredModule="monitoring"><MonitoringPage /></ProtectedRoute>} />
                            <Route path="monitoring/methodology" element={<ProtectedRoute requiredModule="monitoring"><ObservationMethodology /></ProtectedRoute>} />
                            <Route path="insights" element={<ProtectedRoute requiredModule="insights"><InsightsCenter /></ProtectedRoute>} />
                            <Route path="reports" element={<ProtectedRoute requiredModule="reports"><ReportManagement /></ProtectedRoute>} />
                            <Route path="writing" element={<ProtectedRoute requiredModule="writing"><WritingWorkspace /></ProtectedRoute>} />
                            {/* [#204 §4] 无 id = 当前客户那一屏(左栏选最近一条;一条都没有就空态) */}
                            <Route path="writing/image-note" element={<ProtectedRoute requiredModule="writing"><ImageNoteDetailRoute /></ProtectedRoute>} />
                            <Route path="writing/image-note/:postId" element={<ProtectedRoute requiredModule="writing"><ImageNoteDetailRoute /></ProtectedRoute>} />
                            <Route path="references" element={<ProtectedRoute requiredModule="writing"><ReferenceLibrary /></ProtectedRoute>} />
                            <Route path="imitated-articles/:articleId" element={<ProtectedRoute requiredModule="writing"><ImitatedArticleDetail /></ProtectedRoute>} />
                            <Route path="articles" element={<ProtectedRoute requiredModule="writing"><WritingCenter /></ProtectedRoute>} />
                            <Route path="articles/progress" element={<ProtectedRoute requiredModule="writing"><WritingProgress /></ProtectedRoute>} />
                            <Route path="publish" element={<ProtectedRoute requiredModule="writing"><PublishCenter /></ProtectedRoute>} />
                            <Route path="publish/history" element={<ProtectedRoute requiredModule="writing"><PublishHistory /></ProtectedRoute>} />
                            {/* [§13 出口可达性 2026-07-26] 后端两条出口合同的 view_orders 动作
                                target 都写的 '/publishing'，而站内真实路由是 '/publish' ——
                                点了会落到不存在的路由。**必须在前端加别名而不是只改后端**：
                                合同 JSON 在 suspend 时就冻进 mhz_publish_order_items.reject_contract，
                                改后端构造函数不会回填已存在的行（生产已有 5 条真实数据）。
                                后端也应把新合同的 target 改成 '/publish'，但那只管未来。 */}
                            <Route path="publishing" element={<Navigate to="/publish" replace />} />
                            <Route path="publishing/*" element={<Navigate to="/publish" replace />} />
                            <Route path="brands" element={<ProtectedRoute requiredModule="brands"><BrandList /></ProtectedRoute>} />
                            <Route path="brands/:id" element={<ProtectedRoute requiredModule="brands"><BrandDetail /></ProtectedRoute>} />
                            <Route path="my-brand" element={<ProtectedRoute><MyBrandPage /></ProtectedRoute>} />
                            <Route path="my-clients" element={<ProtectedRoute><MyClientsPage /></ProtectedRoute>} />
                            <Route path="my-clients/:id" element={<ProtectedRoute><BrandDetailPage /></ProtectedRoute>} />
                            <Route path="employees" element={<ProtectedRoute requiredModule="ai_agents"><EmployeeHall /></ProtectedRoute>} />
                            <Route path="employees/meeting" element={<ProtectedRoute requiredModule="ai_agents"><MeetingRoom /></ProtectedRoute>} />
                            <Route path="employees/meeting-history" element={<ProtectedRoute requiredModule="ai_agents"><MeetingHistory /></ProtectedRoute>} />
                            <Route path="employees/tasks" element={<ProtectedRoute requiredModule="ai_agents"><TaskBoard /></ProtectedRoute>} />
                            <Route path="employees/settings" element={<ProtectedRoute requiredModule="ai_agents"><EmployeeSettings /></ProtectedRoute>} />
                            {/* [开源 E3 · 前端 · 2026-10-01 · WO_322] 顾问团(/advisors 专家市场 · /advisors/manage 顾问管理)整删:不在侧栏,WO_260 已标废弃域 */}
                            <Route path="history" element={<ProtectedRoute requiredModule="history"><HistoryList /></ProtectedRoute>} />
                            <Route path="workspace" element={<ProtectedRoute requiredModule="ai_agents"><Workspace /></ProtectedRoute>} />
                            <Route path="settings" element={<ProtectedRoute requiredModule="settings"><SettingsPage /></ProtectedRoute>} />
                            <Route path="knowledge" element={<ProtectedRoute requiredModule="writing"><KnowledgeManager /></ProtectedRoute>} />
                            <Route path="geo-research" element={<ProtectedRoute requiredModule="writing"><GeoResearchCenter /></ProtectedRoute>} />
                            <Route path="placement" element={<ProtectedRoute requiredModule="writing"><PlacementCenter /></ProtectedRoute>} />
                            <Route path="admin/orders" element={<ProtectedRoute requiredModule="settings"><OrderManagementPage /></ProtectedRoute>} />
                            <Route path="admin/users" element={<ProtectedRoute requiredModule="users"><UserManagement /></ProtectedRoute>} />
                            <Route path="admin/organization-product-config" element={<ProtectedRoute requiredModule="users"><OrganizationProductConfig /></ProtectedRoute>} />
                            <Route path="admin/users/:id" element={<ProtectedRoute requiredModule="users"><AdminUserDetail /></ProtectedRoute>} />
                            <Route path="admin/roles" element={<ProtectedRoute requiredModule="users"><RoleManagement /></ProtectedRoute>} />
                            <Route path="admin/audit" element={<ProtectedRoute requiredModule="users"><AuditLogs /></ProtectedRoute>} />
                            <Route path="admin/agent-audit" element={<ProtectedRoute requiredModule="users"><AgentAudit /></ProtectedRoute>} />
                            <Route path="admin/agent_audit" element={<ProtectedRoute requiredModule="users"><AgentAudit /></ProtectedRoute>} />
                            <Route path="admin/agent-metrics" element={<ProtectedRoute requiredModule="users"><AgentMetrics /></ProtectedRoute>} />
                            <Route path="admin/agent_metrics" element={<ProtectedRoute requiredModule="users"><AgentMetrics /></ProtectedRoute>} />
                            <Route path="admin/agent-trace/:turnId" element={<ProtectedRoute requiredModule="users"><AgentTrace /></ProtectedRoute>} />
                            <Route path="admin/agent_trace/:turnId" element={<ProtectedRoute requiredModule="users"><AgentTrace /></ProtectedRoute>} />
                            <Route path="admin/agent-loop-control" element={<ProtectedRoute requiredModule="users"><AgentLoopControl /></ProtectedRoute>} />
                            <Route path="admin/agent_loop_control" element={<ProtectedRoute requiredModule="users"><AgentLoopControl /></ProtectedRoute>} />
                            {/* CTO-15.23 2026-05-09 · LLM 成本监控 dashboard */}
                            <Route path="admin/llm-cost" element={<ProtectedRoute requiredModule="users"><LLMCostDashboard /></ProtectedRoute>} />
                            {/* 财务中心 2026-05-30 · 投资人/审计级财务报表 */}
                            <Route path="admin/finance" element={<ProtectedRoute requiredModule="users"><FinanceCenter /></ProtectedRoute>} />
                            <Route path="admin/research-monitor" element={<ProtectedRoute requiredModule="users"><ResearchMonitor /></ProtectedRoute>} />
                            <Route path="admin/geo-placement-flywheel" element={<ProtectedRoute requiredModule="users"><GeoPlacementFlywheel /></ProtectedRoute>} />
                            <Route path="admin/geo-observation-center" element={<ProtectedRoute requiredModule="users"><AdminObservationCenter /></ProtectedRoute>} />
                            {/* [2026-07-01] AI 运维控制塔路由已移出 <Layout> · 作为顶层全屏路由(见 /tv 附近)· 无 app chrome */}
                            <Route path="admin/partner-applications" element={<ProtectedRoute requiredModule="users"><PartnerApplicationsReview /></ProtectedRoute>} />
                            {/* CTO-13.0: admin/partner-review 对齐 HANDOFF/测试文档命名（/admin/partner-applications 旧路径保留兼容） */}
                            <Route path="admin/partner-review" element={<ProtectedRoute requiredModule="users"><PartnerApplicationsReview /></ProtectedRoute>} />
                            {/* CTO-15.2 v3.6: L1/L2 公共素材矫正记录管理 */}
                            <Route path="admin/industry-corrections" element={<ProtectedRoute requiredModule="users"><IndustryCorrectionsAdmin /></ProtectedRoute>} />
                            <Route path="admin/media-directory" element={<ProtectedRoute requiredModule="users"><MediaDirectoryAdmin /></ProtectedRoute>} />
                            <Route path="admin/withdrawals" element={<ProtectedRoute requiredModule="users"><WithdrawalReviewPage /></ProtectedRoute>} />
                            {/* [返工4 P2] 诊断资金异常处置台(补发/退款 + 结算人工处置队列 · 替代手工调 API) */}
                            <Route path="admin/diagnosis-fund-exceptions" element={<ProtectedRoute requiredModule="users"><DiagnosisFundExceptions /></ProtectedRoute>} />
                            {/* [2026-05-18 帮助中心 Phase 3] 管理员双 tab(FAQ 内容 + 反馈收件箱) · is_admin 校验 */}
                            <Route path="admin/help-center" element={<ProtectedRoute requiredModule="settings"><HelpCenterAdmin /></ProtectedRoute>} />
                            {/* [2026-05-18 v2] FAQ 编辑独立全屏页 · /faq/new 新建 · /faq/:id 编辑 */}
                            <Route path="admin/help-center/faq/new" element={<ProtectedRoute requiredModule="settings"><FAQItemEditPage /></ProtectedRoute>} />
                            <Route path="admin/help-center/faq/:id" element={<ProtectedRoute requiredModule="settings"><FAQItemEditPage /></ProtectedRoute>} />
                            {/* CTO-15.9 A.4 (2026-04-25): 行业 auditor 拒绝率监督 · M1b prompt 调优入口 */}
                            <Route path="admin/auditor" element={<ProtectedRoute requiredModule="users"><AuditorRejectionRate /></ProtectedRoute>} />
                            <Route path="account" element={<Navigate to="/account/profile" replace />} />
                            <Route path="account/profile" element={<ProtectedRoute><ProfilePage /></ProtectedRoute>} />
                            <Route path="notifications" element={<ProtectedRoute><NotificationCenter /></ProtectedRoute>} />
                            <Route path="organization/team" element={<ProtectedRoute><OrganizationCenter /></ProtectedRoute>} />
                            {/* /help 直接显示说明书；视频学院独立放在 /help/home。 */}
                            <Route path="help" element={<ProtectedRoute><HelpDocs /></ProtectedRoute>} />
                            <Route path="help/home" element={<ProtectedRoute><HelpCenter /></ProtectedRoute>} />
                            <Route path="help/docs" element={<ProtectedRoute><HelpDocs /></ProtectedRoute>} />
                            <Route path="help/docs/:slug" element={<ProtectedRoute><HelpDocs /></ProtectedRoute>} />
                            <Route path="help/faq" element={<ProtectedRoute><HelpFAQ /></ProtectedRoute>} />
                            <Route path="feedback" element={<ProtectedRoute><FeedbackPage /></ProtectedRoute>} />
                            {/* 积分系统 */}
                            {/* 🔴 [#222 a1b' 2026-09-16] `/wallet` 是**服务商**钱包面(提现 / 佣金 / 抵扣偏好 / 自动监测),
                                按 Owner 原则属经营后台。原来只在菜单里对普通账号藏着,路由是裸的 ——
                                敲 URL 就能进,那是「藏」不是「限」。
                                普通账号的零售钱包面仍在:`/customer/wallet`(余额/明细)+ `/customer/recharge`(购买),
                                两条都不带守卫、页面内也无身份闸(反向对照格 W2/W3 钉住它们不许一起关掉)。

                                🔴 用 `AgentLevelGate + fallback=Navigate` 而**不是** `ProtectedRoute requiresAgent`:
                                后者渲染的是「这是服务商经营后台」否决页,出口只有主菜单/帮助 ——
                                普通账号此前一直能进 `/wallet`,有书签的人会撞进死胡同,而那页**不告诉他自己的钱包在哪**。
                                改道到零售面与下面一行 `wallet/recharge` 的既有做法一致。
                                另:`AgentLevelGate` 只认**本人** agent_level,不认 `operating_for_agent` ——
                                这正合 `ProtectedRoute` 自己的注释:提现/佣金是 owner-only,代作业不构成放行理由。 */}
                            <Route path="wallet" element={
                                <AgentLevelGate requiredLevel={1} fallback={<Navigate to="/customer/wallet" replace />}>
                                    <WalletPage />
                                </AgentLevelGate>
                            } />
                            <Route path="wallet/recharge" element={<Navigate to="/customer/recharge" replace />} />
                            <Route path="wallet/bank-cards" element={<BankCardsPage />} />
                            {/* V3.3.1 服务费流水(L2 才能进 · 后端 RBAC 兜底) */}
                            <Route path="wallet/service-fee-history" element={<ProtectedRoute><ServiceFeeHistoryPage /></ProtectedRoute>} />
                            {/* Phase 07: Social Studio 订阅 V3.1
                                2026-05-13: 3 路由已提到 GEO <Layout /> 外(line ~378 附近),无 sidebar
                                防止用户从 /s 跳过来后被 GEO sidebar 包住误认为"跑到 GEO 板块" */}

                            {/* v3.3 / v3.4 GEO 全自动托管 */}
                            <Route path="managed" element={<ManagedListPage />} />
                            <Route path="managed/:id" element={<ManagedDetailPage />} />
                            <Route path="managed/brand/:brandId" element={<BrandDashboardPage />} />
                            <Route path="material-center/:profileId" element={<MaterialCenterPage />} />
                            <Route path="admin/managed" element={<ProtectedRoute requiredModule="users"><ManagedCampaignsAdmin /></ProtectedRoute>} />
                            {/* [防御型 GEO 发布 v2 · 2026-08-21 → P0-1 关 2026-08-23 → 包E 重开 2026-08-24]
                                · decision/:snapshotId  UI-35 媒体方案确认(恢复走 exact GET,永不重 POST preview)
                                · commands/:commandId   UI-34/36 发布状态(statusVersion 单调守卫)
                                🔴 重开的前置条件是**执行侧真的接上了**,不是"觉得可以了":
                                  claim_outbox / dispatch_once / reconcile_once 各有生产调用点
                                  (services/defensive_geo/publish/publish_worker.py),
                                  调度器 register_v32_core_tasks 里有三条**推进** job
                                  (defgeo_publish_dispatch / defgeo_publish_reconcile /
                                   defgeo_activation_materialize),而不只是只读告警。
                                  后端同步把 `_CUSTOMER_PUBLISH_ENTRY_OPEN` 翻 True,
                                  两侧不能各自漂移(判据把它们绑在一起)。
                                · admin 队列保留:只读、不产生新冻结,走 requiredModule="users",
                                  后端 `_require_admin` 仍逐请求判 is_admin。 */}
                            {/* [合流 2026-08-24 Review-CTO · 预裁终态] 两条客户路由 =
                                ProtectedRoute(writing) 包 PublishDeepLink 家族,唯一挂法:
                                E 版直挂确认屏与 H 版裸 DeepLink 各取一半归并,重复路由已消。 */}
                            <Route path="defensive-geo/publish/decision/:snapshotId" element={<ProtectedRoute requiredModule="writing"><DefgeoPublishDecisionLink /></ProtectedRoute>} />
                            <Route path="defensive-geo/publish/commands/:commandId" element={<ProtectedRoute requiredModule="writing"><DefgeoPublishCommandLink /></ProtectedRoute>} />
                            <Route path="admin/defensive-geo-settlement-review" element={<ProtectedRoute requiredModule="users"><DefgeoSettlementReviewQueue /></ProtectedRoute>} />
                            {/* [包H · U-5] 交付待办聚合页。U-5 逐字:12~50 轮确认没有聚合页 = 验收红。
                                只读:这一页不签发 preview、不冻算力,资金仍逐项。 */}
                            <Route path="defensive-geo/publish/todo" element={<DefgeoDeliveryTodo />} />
                            <Route path="feature-pricing" element={<PricingPage />} />
                            {/* 营销物料工厂(用户侧 · 登录即用 · 端点内 user_id/brand owner 隔离) */}
                            <Route path="marketing-materials" element={<MarketingMaterialsPage />} />
                            {/* v1.1 审核制: 合作伙伴计划（PARTNER_APPLY_ENABLED flag 控制） */}
                            <Route path="partner/about" element={<PartnerAbout />} />
                            <Route path="partner/apply" element={<PartnerApplyStep1 />} />
                            <Route path="partner/apply/id-card" element={<PartnerApplyStep2 />} />
                            <Route path="partner/apply/agreement" element={<PartnerApplyStep3 />} />
                            <Route path="partner/status" element={<PartnerStatus />} />
                            {/* v1_3 (CTO-15.1 2026-04-19): 协议查看路由已抽到顶层公开位置（L249）
                                此处不再渲染，避免代理端 Layout 侧边栏泄漏给普通用户 */}

                            {/* 推荐/推广(普通用户版 · 服务商推广走 /agent/promotion) */}
                            <Route path="referral" element={<InviteCenter />} />
                            {/* 2026-05-25 删:agent/quote 快速录单页已废弃 · 走 /pricing 单一在线报价入口 */}
                            {/* [P1 2026-06-06] 基础白标对所有 operator 放开:不走服务商协议门(AgreementGate)·父级 Layout 已保证登录 */}
                            <Route path="agent/whitelabel" element={<WhitelabelSettings />} />
                            <Route path="agent/profit" element={<ProfitDashboard />} />
                            <Route path="agent/preview" element={<QuotePreview />} />
                            <Route path="agent/trial-pass-review" element={<TrialPassReviewPage />} />
                            <Route path="agent/leads" element={<LeadsPage />} />

                            {/* [V3.5 W2 2026-05-26] 代理工作台 4 页 */}
                            <Route path="agent/inventory" element={<ProtectedRoute requiresAgent ownerIdentityOnly><AgreementGate><AgentInventoryCenter /></AgreementGate></ProtectedRoute>} />
                            <Route path="agent/channel-partners" element={<ProtectedRoute requiresAgent ownerIdentityOnly><AgreementGate><AgentChannelPartners /></AgreementGate></ProtectedRoute>} />
                            <Route path="agent/pricing" element={<ProtectedRoute requiresAgent ownerIdentityOnly><AgentPricingRoute /></ProtectedRoute>} />
                            <Route path="agent/settlement" element={<ProtectedRoute requiresAgent ownerIdentityOnly><AgreementGate><AgentSettlementCenter /></AgreementGate></ProtectedRoute>} />
                            <Route path="agent/promotion" element={<ProtectedRoute requiresAgent ownerIdentityOnly><AgreementGate><AgentPromotionCenter /></AgreementGate></ProtectedRoute>} />

                            {/* [V3.5 W2 2026-05-26] Admin 工厂治理 4 页(仅 admin 可访问 · 后端 _require_admin 兜底) */}
                            <Route path="admin/settlements" element={<ProtectedRoute requiredModule="users"><AdminSettlementsReview /></ProtectedRoute>} />
                            <Route path="admin/refunds" element={<ProtectedRoute requiredModule="users"><AdminRefundConsole /></ProtectedRoute>} />
                            <Route path="admin/tax-profiles" element={<ProtectedRoute requiredModule="users"><AdminTaxProfiles /></ProtectedRoute>} />
                            <Route path="admin/inventory-audit" element={<ProtectedRoute requiredModule="users"><AdminInventoryAudit /></ProtectedRoute>} />
                            <Route path="admin/agent-inventory" element={<ProtectedRoute requiredModule="users"><AdminAgentInventoryGovernance /></ProtectedRoute>} />
                            {/* [2026-06-07] 算力定价中心(合并 算力包管理 + 定价系数配置)· 旧路由重定向不 404 */}
                            <Route path="admin/pricing-center" element={<ProtectedRoute requiredModule="users"><AdminPricingCenter /></ProtectedRoute>} />
                            <Route path="admin/channel-tier" element={<ProtectedRoute requiredModule="users"><AdminChannelTier /></ProtectedRoute>} />
                            <Route path="admin/pricing" element={<Navigate to="/admin/pricing-center" replace />} />
                            <Route path="admin/pricing-config" element={<Navigate to="/admin/pricing-center" replace />} />

                            {/* [V3.5 W3 2026-05-26] 代理协议签约 */}
                            <Route path="agent/agreement" element={<AgreementPage />} />

                            {/* [V3.5 W3 2026-05-26] 客户工具算力 */}
                            <Route path="customer/wallet" element={<CustomerCreditWallet />} />
                            <Route path="customer/recharge" element={<CustomerBuyCredit />} />

                            {/* [V3.5 W4 2026-05-26] Admin 治理:dispute + audit 历史 */}
                            <Route path="admin/binding-disputes" element={<ProtectedRoute requiredModule="users"><AdminBindingDisputes /></ProtectedRoute>} />
                            <Route path="admin/inventory-audit-history" element={<ProtectedRoute requiredModule="users"><AdminInventoryAuditHistory /></ProtectedRoute>} />
                            {/* [V3.6 白标 2026-05-30] admin 白标授权页 */}
                            <Route path="admin/whitelabel" element={<ProtectedRoute requiredModule="users"><AdminWhitelabelGrant /></ProtectedRoute>} />
                            {/* 🔴 [#165 F2] 未知路由兜底。原来全站**没有**这一条 ⇒
                                任何写错的站内链接都是**白屏**（真人点测在生产上撞到 `/quotes`）。
                                必须放在**组内**：登录用户看到带侧栏的「页面不存在」，还能直接点去别处；
                                顶层再挂一条就永远匹配不到（父路由 `path="/"` 对任何 URL 都是前缀），
                                而死路由不如不写。静态段 > 动态段 > splat 是 RR v6 的排序规则，
                                本单用 `matchRoutes` 拿真路径集实测过，不是只读文档。 */}
                            <Route path="*" element={<NotFound />} />
                        </Route>
                    </Routes>
                </Suspense>
            </PricingProvider>
            </OnboardingProvider>
            </WalletProvider>
            </UserModeProvider>
            </OrganizationProvider>
            </AuthProvider>
        </Router>
        </GlobalErrorBoundary>
    )
}

export default App
