/**
 * Layout — 主布局壳
 * 使用 shadcn-dashboard 的 SidebarProvider 响应式骨架
 * 桌面端: 左侧 Sidebar(可折叠) + 右侧 Header + PageContainer
 * 移动端: Sidebar 自动变为 Sheet 抽屉，通过 Header 的汉堡菜单触发
 */

import { lazy, Suspense, useEffect, useState } from 'react';
import { Outlet, useLocation, useSearchParams, Navigate } from 'react-router-dom';
import { useIsMobile } from '@/hooks/use-mobile';
import { SidebarProvider, SidebarInset, useSidebar } from '@/components/ui/sidebar';
import { AppSidebar } from './AppSidebar';
import { cn } from '@/lib/utils';
import { FLOATING_FAB, FLOATING_Z } from '@/lib/floating-stack';
import { Header } from './Header';
// CTO-15.21 v5 (2026-04-28 老板拍板):M3 已废弃 · 测试版本而已 · 取消顶部"新版工作台已就绪"banner
// import { M3BackBanner } from './M3BackBanner';
import { ClientProvider } from '@/context/ClientContext';
// Phase 07 (CTO-15.23 2026-05-04) · 旧客户上下文条 移到 sidebar 顶部 · Layout 不再顶部显示
// import 旧客户上下文条 from './旧客户上下文条';
import { NotificationBanner } from './NotificationBanner';
import { useSwipeGesture } from '@/hooks/useSwipeGesture';
import { useAuth } from '@/context/AuthContext';
import { useOnboarding } from '@/context/OnboardingContext';
import { useBranding } from '@/hooks/useWhitelabel';
import { BrandTitle } from '@/components/brand/BrandDisplay';
import { MessageCircle } from 'lucide-react';
import { PageLoading } from '@/components/ui/page-loading';
import { OssAttribution } from '@/components/common/OssAttribution';
// Stage 1 Task 2 (2026-05-06) · 新代理首次进工作台 · 30 秒看懂 7 步流程的欢迎弹窗
// 2026-05-22 弃用:换成 WelcomeChoiceModal · 新代理直接进沙盒教程,不再让用户在真实路径里挑
// import { OnboardingWelcomeModal } from '@/components/onboarding/OnboardingWelcomeModal';
// 2026-05-22 新代理首次登录二选一(新代理→进沙盒 / 已用过→跳过)
// Stage 1 Task 3 (2026-05-06) · 右下角 7 步任务卡 · welcome_choice 做了选择后持续陪跑
// 内部判断 isAvailable + welcome_choice ∈ {start,later} + 未全部完成
import { SandboxRouteGuard } from '@/sandbox/SandboxRouteGuard';
import { useSandboxState } from '@/sandbox/sandboxState';
import { useScreenshotMode } from '@/sandbox/screenshotMode';
import { DemoActionPreviewDialog } from '@/components/demo/DemoActionPreviewDialog';

// 2026-05-25 小榜重做:挂点切到新 XiaobangDrawer(纯问答 + RAG)
// 旧 旧 AI 助手抽屉 暂保留,给 /agent 全屏页 + C 端聊天页 + 社媒工作台 继续用,
// 等小榜稳定后单独 PR 清理。
const XiaobangDrawer = lazy(() =>
  import('@/components/xiaobang/XiaobangDrawer').then(m => ({ default: m.XiaobangDrawer }))
);
const WelcomeChoiceModal = lazy(() => import('@/components/onboarding/WelcomeChoiceModal'));
const OnboardingChecklist = lazy(() => import('@/components/onboarding/OnboardingChecklist'));
const SandboxBanner = lazy(() => import('@/sandbox/SandboxBanner'));
const SandboxIntroModal = lazy(() => import('@/sandbox/SandboxIntroModal'));
const MobileSidebarCoach = lazy(() => import('@/sandbox/MobileSidebarCoach'));

/**
 * [CTO-15.23 2026-05-21 v12] 撤回 visualViewport hook · SidebarInset 永远 100dvh
 *
 * 老板拍板走 B 方向:v11 viewport meta 锁 scale=1 后 · zoom 系列 BUG 根治 ·
 * visualViewport hook 不再必要 · iOS Safari 原生 scroll-into-view 处理键盘弹起更稳
 *
 * 历史 v1-v11 hook 演化:
 *   v1 (336bb0e9) 永远 vv.height
 *   v2 (9f271b69) scale > 1 → naturalH
 *   v3-v5 (879ecf96/2b3cc573/77bc9d3f) bg-card 兜底
 *   v6 (13c893bc) 回 v2
 *   v11 (42bfa4a4) viewport meta 锁 scale=1 → scale 永远 = 1 · hook 实际只跑 vv.height 分支
 *
 * v12 撤回的理由:
 *   1. v11 后 scale 永远 = 1 · hook 不会跑 zoom 分支
 *   2. vv.height 在键盘弹起时缩到键盘上方 · 但 6.mp4 实证 SidebarInset 缩后 form 局部
 *      渲染 + 下方 SidebarInset bg-card 空白 · 老板报"勉强可用 · 还有问题"
 *   3. 撤 hook 让 SidebarInset 永远 100dvh · iOS Safari 原生 scroll input into view
 *      处理键盘 · 不再有 SidebarInset 缩到键盘上方导致的视觉空白
 *   4. 简化代码 · 减少 visualViewport API 监听器
 *
 * 兜底保留:
 *   - bg-card 在 SidebarInset/body/html 三层(v3-v5)· 防 form 短下方空白显眼
 *   - useSwipeGesture 多指 + 垂直锁定(v7+v8)· 防 swipe 误触发 sidebar overlay
 *   - viewport meta 锁 scale=1(v11)· 根治 zoom 系列 BUG
 */

/**
 * [CTO-15.23 2026-05-20 老板拍板] 撤回 iOS Chrome UA 检测 + 硬编码 padding · 改用纯自适应
 *
 * 之前 c8dfcef3 + 41bcab78 + 77d91d0e 假设 iOS Chrome 不报 chrome shell · 加 UA 检测
 * 硬编码 pt-40/pb-40。但实际 Chrome iOS 91+ 的 100dvh 已经扣除 chrome shell:
 * - 100dvh = WKWebView 视口的可见区域(chrome 显示时 = 小视口 · 已避开)
 * - env(safe-area-inset-*) 兜物理 safe-area(notch / home indicator)
 * = 完全自适应 · 不需要硬编码
 *
 * 老板截图反馈"是不是主动加了遮罩":硬编码 px 跟 bg-background 同暗色 = 视觉像遮罩
 * 撤回 UA 检测 · 信任浏览器自己 viewport API:
 * - iPhone Safari / Chrome / Firefox / Edge / 安卓 Chrome / 桌面 → dvh + safe-area 一套搞定
 * - 旧版 iOS Chrome(< 91) dvh 不支持 · 降级 vh · 内容**可能**被遮 1-2cm(minority 接受)
 * - 内容真被遮的极少数场景 · 由 textarea max-h-200px 限高 + bg-background 自然边缘减弱
 */

/** Enables swipe-to-open/close sidebar on mobile. Must be inside SidebarProvider. */
function SwipeHandler() {
  const { setOpenMobile } = useSidebar();
  useSwipeGesture(
    () => setOpenMobile(true),   // Right swipe from edge → open sidebar
    () => setOpenMobile(false),  // Left swipe → close sidebar
  );
  return null;
}

function LayoutInner() {
  const location = useLocation();
  const { user } = useAuth();
  const { state: onboardingState, isAvailable: onboardingAvailable } = useOnboarding();
  // [V3.6 OEM 2026-05-30] 仅 oem 授权服务商的后台注入其 product_name + favicon 到浏览器标签;
  // D2（Owner 2026-07-22）：判定改读 backoffice 独立授权（fail-closed）· 未授权不动 document.title
  const { brand: oemBrand, backofficeBrandAllowed: oemBackofficeAllowed } = useBranding({ userId: user?.id, surface: 'agent' });
  const { isSandbox } = useSandboxState();
  // 2026-05-23 · 截图模式:视觉上等同非沙盒 · 让小榜 旧 AI 助手抽屉 等"真实工作台"特征出现
  const screenshotMode = useScreenshotMode();
  const [xiaobangRequested, setXiaobangRequested] = useState(false);
  const [auxiliaryUiReady, setAuxiliaryUiReady] = useState(false);
  const sandboxUI = isSandbox && !screenshotMode;
  const welcomeChoiceNeeded = onboardingAvailable
    && onboardingState.welcome_choice === undefined
    && !!user
    && (user.agent_level ?? 0) >= 1
    && !sandboxUI
    && !screenshotMode;
  useEffect(() => {
    if (sandboxUI) {
      setAuxiliaryUiReady(true);
      return;
    }
    // The first-time choice is a real gate, so never hide it behind a fixed 2s
    // timer. Start its lazy chunk immediately after the shell's first paint.
    const frameId = window.requestAnimationFrame(() => setAuxiliaryUiReady(true));
    return () => window.cancelAnimationFrame(frameId);
  }, [sandboxUI]);
  // [2026-05-27 撤回自动跳 · 改 B 方案] 4 个 sidebar stage 交给 MobileSidebarCoach 真实引导
  //   (2 阶段:点汉堡 → 点 sidebar 项)· 这里只保留 isMobile 用于 SidebarInset padding 让位 banner
  const isMobile = useIsMobile();
  const isPortalPage = location.pathname.startsWith('/portal');
  // [WO-PUBCENTER-LAYOUT 2026-08-04] 发布中心是"自己管滚动"的定高页:
  // 页面内部左右两栏各自 overflow-y-auto,外壳再给一层 overflow-y-auto 只会让
  // 页面比可用区矮/高时无声地多出一条滚动或一截底边。
  //
  // 老板报的"底部还有一部分黑色卡片留白"就是后者:PublishCenter 之前自己算
  // `100dvh - 7rem`(112px),而这个壳里 Header 实测只有 h-12(48px),
  // 差的 64px 露出的正是 SidebarInset 的 bg-card —— 生产实测 gap 恒等于 64px。
  // 修法不是把 7rem 改成 3rem(换一个写死的数字,Header 一改照样错),
  // 而是让外壳定高、页面 `h-full` 跟着壳走,谁都不再猜头部高度。
  //
  // 🔴 只对发布中心生效:其余页面继续 `overflow-y-auto`(它们靠外壳滚动)。
  // [WO_260] 原先还有 D 桶 `/agent`(全屏对话页,不在侧栏、随 E3 删):定高与下面两处小榜判断里的
  //    `/agent` 特判一并摘掉 —— 在役页的定高与小榜显示逐位不变。
  const isFullHeightPage = location.pathname === '/publish';
  // Phase 07 (CTO-15.23 2026-05-04) · 客户选择栏移到 sidebar 顶部 · 此处不再渲染
  // 老代码保留注释 · 方便回滚或对照:
  //   const clientBarPages = [
  //     '/writing', '/monitoring', '/pricing',
  //     '/placement', '/history', '/reports', '/insights',
  //     '/brands', '/my-clients',
  //   ];
  //   const showClientBar = clientBarPages.some(p => location.pathname.startsWith(p));

  // CTO-15.23 2026-05-25 · 老板新商业规则:
  //   "通过邀请码注册的用户都可以使用我们的系统 · 只是代理推荐的用户充值后获可提现积分(旧分成制度,已停),
  //    普通用户推荐就是 15% 赠送积分奖励"
  // → L0 普通用户也走正常代理端 Layout (sidebar + 全功能可见)
  // → 删除原 [CTO-13.3 2026-04-20] 的 L0 → CEndSimplifiedLayout 简化壳逻辑
  // → AgentLevelGuard line 319 已放行 L0 走 Dashboard · 这里 LayoutInner 也对齐
  // → 老板报"sidebar 闪一下就自动隐藏" 真因 = wallet 加载完 isL0=true 切到无 sidebar 简化壳
  // → [WO_260] 原「沉浸路径(/social/interview 等 4 条)→ 简化壳」整段删:/social/* 是 App.tsx 顶层重定向路由,
  //   不挂在本壳下,这段从未命中;简化壳 CEndSimplifiedLayout 只有它一个入口,一并删。

  return (
    <SidebarProvider>
      {/* [V3.6 OEM 2026-05-30] 服务商后台浏览器标签/favicon 换肤 · D2：仅 backoffice 独立授权生效 · 渲染 null 纯副作用 */}
      {oemBackofficeAllowed && <BrandTitle brand={oemBrand} suffix="工作台" />}
      <SwipeHandler />
      <AppSidebar />
      <SidebarInset className={cn(
        'h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] bg-card',
        // [CTO-15.23 2026-05-21 v12] height 回 100dvh · 撤回 visualViewport hook
        // [P2-13 fix 2026-05-23] 非 agent 页加 overflow-x-hidden 防 mobile 长文/表格横向越界
        //   不影响子组件自身的 overflow-x-auto(如 PublishCenter 表格 / 1920 宽页面)
        isFullHeightPage ? 'flex flex-col overflow-hidden' : 'overflow-y-auto overflow-x-hidden',
        // [2026-05-27] 移动端沙盒态:banner z-180 在最顶 · 主内容下移 36px 防汉堡菜单被挡
        //   PC 端不动 · banner 高度变化交给 PC 后续单独 PR 处理
        sandboxUI && isMobile && 'pt-[calc(env(safe-area-inset-top)+2.25rem)]'
      )}>
        {/* CTO-15.21 v5 (2026-04-28):M3 已废弃 · 测试版本 · 取消顶部"新版工作台已就绪"banner */}
        {/* <M3BackBanner /> */}
        <Header />
        {/* Phase 07 · 旧客户上下文条 移到 sidebar 顶部 · 这里不再渲染 */}
        <main role="main" data-testid="app-page-main" className={cn(isFullHeightPage ? 'flex-1 min-h-0' : 'flex-1')}>
          {/* Keep the authenticated shell mounted while a lazy route chunk loads. Without
              this local boundary the app-level Suspense remounts the shell and repeats
              logo/provider GETs during every cold navigation. */}
          <Suspense fallback={<PageLoading />}>
            <Outlet />
          </Suspense>
        </main>
        {/* WO_329 开源版署名位:放在 main 之外,固定高度页(/publish,main 溢出隐藏)也不会被裁掉;开关关 ⇒ 不渲染 */}
        <OssAttribution />
      </SidebarInset>
      <NotificationBanner />
      <DemoActionPreviewDialog />
      {/* Stage 1 Batch 4 · 沙盒态下隐藏小榜 AI 助手 · 防真实账号 AI 数据漏出 */}
      {!sandboxUI && !screenshotMode && !xiaobangRequested && (
        /* 🔴 位置改走 FloatingStack 常量(xbvnext 2026-08-18)。原来写死
             `bottom-5 right-5 z-50`,两个真问题:
             ① 20px 的 bottom 会压在**页面级**底部固定操作条上(选词/报价预览/
                采集填写/素材确认四处 `fixed bottom-0`),而 z-50 比它们都高;
                🔴 更正:先前这里写的理由是「压住 MobileTabBar」——**那是错的**,
                那个组件全仓零挂载、根本不渲染,是真浏览器判据把这个错前提照出来的;
             ② 抽屉一挂载,FAB 就换成 AgentFAB(bottom 152),按钮当场从 20 跳到 152。
             两处用同一组常量后,位置一致、也自动进了"FAB 区间不许相交"那条锁。
             floating-stack.ts 顶上本来就写着「不要再写 z-50 这种魔法数字」。 */
        <button
          type="button"
          aria-label="打开小榜 GEO 助手"
          aria-haspopup="dialog"
          title="小榜 · GEO 助手"
          onClick={() => setXiaobangRequested(true)}
          style={{
            right: FLOATING_FAB.XIAOBANG_DEFAULT_RIGHT,
            bottom: `calc(${FLOATING_FAB.XIAOBANG_DEFAULT_BOTTOM}px + env(safe-area-inset-bottom, 0px))`,
            zIndex: FLOATING_Z.FAB,
          }}
          className="fixed flex size-12 items-center justify-center rounded-full border border-border/60 bg-background text-foreground shadow-lg transition-transform hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <MessageCircle className="size-5" />
        </button>
      )}
      {!sandboxUI && (screenshotMode || xiaobangRequested) && (
        <Suspense fallback={null}>
          <XiaobangDrawer initialOpen={xiaobangRequested} />
        </Suspense>
      )}
      {/* Stage 1 Task 2 · 老版"跑诊断/出报价/跑全流程"3 选项欢迎弹窗 ——
         已弃用 (2026-05-22): 改为 WelcomeChoiceModal 二选一(新代理→沙盒 / 老用户→跳过)
         OnboardingWelcomeModal.tsx 文件保留供历史参考。
       <OnboardingWelcomeModal /> */}
      {/* 2026-05-22 · 新代理首次登录二选一弹窗 · 强制选择不让蒙混 */}
      {auxiliaryUiReady && welcomeChoiceNeeded && (
        <Suspense fallback={null}>
          <WelcomeChoiceModal />
        </Suspense>
      )}
      {auxiliaryUiReady && sandboxUI && (
        <Suspense fallback={null}>
          {/* Stage 1 Task 3 · 右下角 7 步任务陪跑卡 */}
          <OnboardingChecklist />
          {/* Stage 1 Batch 3 · 沙盒态顶部横幅 */}
          <SandboxBanner />
          {/* Stage 1 Batch 4 · 沙盒首次进入确认弹窗 + 逃生口 */}
          <SandboxIntroModal />
          {/* [2026-05-27 B 方案] 移动端 sidebar 引导 2 阶段(汉堡 → sidebar 项) */}
          <MobileSidebarCoach />
        </Suspense>
      )}
      {/* Stage 1 Batch 4 · 沙盒态全局路由守卫 · 非白名单 URL 自动踢回 /diagnosis/new */}
      <SandboxRouteGuard />
    </SidebarProvider>
  );
}

/**
 * EmbeddedLayout — C 端嵌入模式（v3.2）
 * 当 URL 参数带 ?embedded=true 时启用
 * 隐藏 Sidebar 和 Header，只显示页面内容
 * 由 C 端 旧 C 端布局 的分屏/覆盖容器调起
 */
function EmbeddedLayout() {
  const content = (
    <div className="h-[100dvh] w-full overflow-auto bg-card pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]">
      <div className="p-4 md:p-6">
        <Outlet />
      </div>
      <OssAttribution />
    </div>
  );

  // [WO_260] 原 `/social` 前缀 → 包社媒 Provider 的分支已删(社媒随 E3 删域;/social/* 本就不经本壳)
  return <ClientProvider>{content}</ClientProvider>;
}

/**
 * v1_2 (CTO-14.0 2026-04-19) 权限漏洞修复：L0 删 URL 后缀 → 直达代理端
 *
 * 背景：ProtectedRoute 只管"登录 + 模块权限 + 改密"，不管 agent_level。
 *      L0 用户手动敲 `omnirank.top/` 或 `/writing` 绕开 postLoginRedirect 进代理端。
 *
 * 策略：代理端 Layout 顶层拦截 L0 访问代理端专属路由 → 强制跳 /c/chat。
 *
 * 白名单（L0 也能访问）：
 *   /wallet(.*)          所有登录用户的钱包页
 *   /account(.*)         账户设置
 *   /notifications       通知中心
 *   /my-brand            L0 管理自己的品牌
 *   /partner/*           合作伙伴计划（L0 是申请方必须能访问）
 *   /managed/*           v3.3/v3.4 托管套餐（C 端也能用）
 *   /material-center/*   物料中心（和 /my-brand 同级）
 *   /feature-pricing     定价页（公开）
 *   /change-password     改密
 *   /admin/*             由 ProtectedRoute 的 ADMIN_ONLY_MODULES 拦 admin
 *   /social/*            由 ProtectedRoute 的 requiredModule='social' 拦
 *
 * admin + 代理（l1/l2）+ wallet 加载中 → 全部放行
 */
const L0_WHITELIST_PREFIXES = [
  '/wallet',
  '/account',
  '/notifications',
  '/my-brand',
  '/partner/',
  '/managed/',
  '/material-center/',
  '/feature-pricing',
  '/change-password',
  '/admin/',
  // v1_3 (CTO-15.1 2026-04-19 老板反馈): 写作大厅 C 端也用
  // C 端 GEO 方案"确认写作" / "查看历史方案" 都跳 /writing?quote_id=X
  // 若不放行 L0 会被 AgentLevelGuard 拦回 /c/chat（老板截图的"跳回新对话框"）
  '/writing',
  // [WO_260] 原 '/social/' 与 '/advisors' 两条已删:社媒与顾问团都是废弃域(E3 删),L0 放行对它们无意义。
  // commit 22 (CTO-15.2 2026-04-19): 老板严重反馈 C 端点诊断报告弹登录页
  // 根因: AgentLevelGuard 没把 /diagnosis/ 放白名单 → L0 被踢回 /c/chat → iframe 嵌套 → 登录页
  // 旧 C 端历史页 诊断报告 Tab 卡片点击 panel.openInPanel('/diagnosis/report/{id}') 必须放行
  // 后端 auth/brand_access.py 已校验用户是否有权访问该报告(L0 看自己的报告 OK)
  '/diagnosis/',
];

function isL0Whitelisted(path: string): boolean {
  // path 精确等于白名单项（去掉尾 /），或以前缀开头
  return L0_WHITELIST_PREFIXES.some(p => {
    const bare = p.replace(/\/$/, '');
    return path === bare || path.startsWith(p);
  });
}

function AgentLevelGuard({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const { user, isLoading: authLoading } = useAuth();

  // 身份只等 auth/me；钱包故障不能阻断布局或改变角色。
  if (authLoading) return <>{children}</>;

  // admin 绕过
  if (user?.is_admin) return <>{children}</>;

  if ((user?.agent_level ?? 0) >= 1) return <>{children}</>;

  // L0 访问共享路径 → 放行
  if (isL0Whitelisted(location.pathname)) return <>{children}</>;

  /*
   * L0 访问服务商端路由 → 放行。
   *   历史:CTO-15.23 废弃 /c/* 豆包式对话,老板拍板「L0 也走旧版 Dashboard」。
   *   之前 redirect 到 /c/chat 会被 App.tsx 的 /c/* → / 兜底反弹,再被本守卫踢回 /c/chat,
   *   形成死循环;iOS Safari 的 history.replaceState 100/10s 上限秒爆(微信内置浏览器 P0)。
   *
   * 🔴 [#222 a1b] 这里原来还有一句话,声称 Dashboard 内部按 agent_level 做过裁剪、
   *    普通账号看到的是删减版视图 —— **那是假的,已删**。
   *    (这里**故意不原样引用**那句原文:判据钉的就是「这句话不许再出现」,
   *     引用一遍会把判据自己打红 —— 我第一版就这么干了。)
   *    2026-09-15 实测 `pages/Dashboard.tsx` 里
   *    `isAgent` / `agent_level` / `agentLevel` **一处都没有**;
   *    `pages/Writing`、`pages/Publishing`、`pages/Pricing`、`pages/Quote` 同样是 0。
   *    那句话在本单普查里**当场误导了两个人**,而错注释永远不会红。
   *
   * 🔴 这个守卫今天**不拦任何人**:上面四条路径全部 `return <>{children}</>`。
   *    名字仍叫 Guard,行为是全放行 —— 要改行为的人别被名字骗了。
   */
  return <>{children}</>;
}

export function Layout() {
  const [searchParams] = useSearchParams();
  const isEmbedded = searchParams.get('embedded') === 'true';
  // [CTO-15.23 2026-05-21 v12] 撤回 useAppViewportHeight 调用 · 见文件顶部 v12 注释

  useEffect(() => {
    if (isEmbedded) return;
    document.documentElement.classList.add('omnirank-shell-lock');
    document.body.classList.add('omnirank-shell-lock');
    return () => {
      document.documentElement.classList.remove('omnirank-shell-lock');
      document.body.classList.remove('omnirank-shell-lock');
    };
  }, [isEmbedded]);

  // v3.2: C 端嵌入模式 — 无 Sidebar 无 Header
  // 不经过 AgentLevelGuard（C 端用户点"查看钱包"等分屏操作必须放行）
  if (isEmbedded) {
    return <EmbeddedLayout />;
  }

  // v1_2: L0 权限守卫（代理端专属路由拦截）
  const content = (
    <AgentLevelGuard>
      <LayoutInner />
    </AgentLevelGuard>
  );
  // [WO_260] 原 `/social` 前缀 → 包社媒 Provider 的分支已删(同上)
  return <ClientProvider>{content}</ClientProvider>;
}
