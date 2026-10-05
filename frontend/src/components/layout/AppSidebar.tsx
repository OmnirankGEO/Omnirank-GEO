/**
 * AppSidebar — 工作面 sidebar · IA v2(2026-06-08 信息架构优化)
 *
 * 目标:从"功能说明书"改成"清爽导航"。主流程 1→4(品牌体检/报价方案/AI写文章/发布投放)一眼可见。
 *   - 三身份各自独立结构(普通用户 / 服务商 / 管理员),同一功能命名统一
 *   - 主流程组小圆序号标识(不用星标)· 默认展开 · 当前页所属分组自动展开并高亮
 *   - 分组标题可点击展开/收起(只标题可折叠)· localStorage 记忆 · 点击菜单项不收起分组
 *   - hint 大幅删减:主流程/账号类不写 hint · 仅保留少数易误解的经营/算力/结算类短 hint
 *   - 全站文案统一"算力"(禁"积分/额度/工具额度")
 *
 * 纯 UI/IA:不改路由 / 权限 / API / hooks / 数据结构 / 点击后业务行为。
 *
 * 历史(CTO-15.20 桥接):GEO 助手留全局浮窗;砍 AI搜索优化/短视频 toggle;社媒路由保留但 sidebar 不显示。
 */

import { lazy, Suspense, useEffect, useState } from 'react';
import { NavLink, useNavigate, useLocation } from 'react-router-dom';
import { MANAGED_ENTRY_ENABLED } from '@/config/managedEntryGate';
import {
  Stethoscope, Settings,
  Building2, Calculator, BookOpen,
  Activity, LogOut, Send, Receipt,
  Wallet, Share2,
  Coins,
  Contact, UserCog, Zap,
  ScrollText, User, HelpCircle,
  MessageSquareWarning,
  Database, Banknote, Monitor,
  Sun,  // 今日工作台 icon
  LifeBuoy, // admin 帮助中心管理
  Boxes, Tag, Megaphone, Scale, BarChart3, Trophy, // 经营后台 + 经营总览
  SlidersHorizontal, // 算力定价中心
  Palette, // 对外品牌(白标)
  FileSignature, Gavel, // 合作协议 + 治理
  ChevronDown, // [IA v2] 分组折叠箭头
  RadioTower, // GEO 数据飞轮
  Bot, // AI 运维控制塔
  ImagePlus, // 营销物料(海报/朋友圈文案 · 用户侧)
  Sparkles, // 营销中心(军师 · admin)
  UsersRound, // 服务商内部团队席位
  Newspaper, // [P1-3] 媒体目录分档
} from 'lucide-react';
import { lazyToast } from '@/lib/lazyToast';
import { useAuth } from '@/context/AuthContext';
import { useBranding } from '@/hooks/useWhitelabel';
import { useTheme } from '@/context/ThemeContext';
import { useWallet } from '@/context/WalletContext';
import { useSandboxState } from '@/sandbox/sandboxState';
import { useTutorialStage, setTutorialStage } from '@/sandbox/tutorialStage';
import {
  getSandboxTutorialNavItem,
  SANDBOX_TUTORIAL_NAV_ITEMS,
} from '@/sandbox/tutorialLayoutContract';
import { useScreenshotMode } from '@/sandbox/screenshotMode';
import { useIsMobile } from '@/hooks/use-mobile';
import { ChannelTierBadge } from '@/components/agent/ChannelTierBadge';
import { usePendingUserActionCount } from '@/hooks/usePendingUserActions';
import { useOrganization } from '@/context/OrganizationContext';

const FeatureTooltip = lazy(() =>
  import('@/components/onboarding/FeatureTooltip').then((module) => ({
    default: module.FeatureTooltip,
  })),
);

// Stage 1 Batch 4 (2026-05-18) · 沙盒态允许访问的路径前缀
// 数据隔离已经在接口层做 (sandboxInterceptor 默认拦所有 API · 返空)
// 路由这边只挡 dashboard / 后台管理这类教学外的页面
const SANDBOX_ALLOWED_PREFIXES = [
  '/diagnosis',
  '/pricing',
  '/writing',
  '/publish',
  '/monitoring',
  '/help',
  '/my-clients',
  '/my-brand',
];

function isSandboxAllowed(path: string): boolean {
  return SANDBOX_ALLOWED_PREFIXES.some((prefix) => path === prefix || path.startsWith(prefix + '/') || path === prefix);
}
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarSeparator,
  useSidebar,
} from '@/components/ui/sidebar';
import { cn } from '@/lib/utils';
import logoDark from '@/assets/logo.png';
import logoLight from '@/assets/logo-light.png';
import logoIconDark from '@/assets/logo-icon-dark.png';
import logoIconLight from '@/assets/logo-icon-light.png';
// Phase 07 (CTO-15.23 2026-05-04) · 全局客户切换器 · sidebar 顶部 · 对标 Linear
import ClientSwitcherSidebar from './ClientSwitcherSidebar';
import { sidebarLabel } from '@/lib/v35Terminology';

interface NavItem {
  to: string;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  hint?: string;
  module?: string;
  /** 主流程小圆序号(1-4)· 存在时用绿色小圆替代 icon */
  stepNumber?: number;
  dataGuide?: string;
}

interface NavSection {
  /** 分组标题(空 = 无标题的置顶单项组,如今日工作台) */
  title: string;
  /** localStorage 折叠状态 key · 无标题组不折叠可省略 */
  key?: string;
  items: NavItem[];
  /** 主流程组:子项显示小圆序号 */
  numbered?: boolean;
  /** 默认是否展开(无显式值时默认展开) */
  defaultOpen?: boolean;
  /** 不可折叠(置顶单项组) */
  fixed?: boolean;
  /** 本组上方画一条分割线(大分组之间) */
  topDivider?: boolean;
}

const GROUPS_LS_KEY = 'omnirank_sidebar_groups_v1';

// ========== 主流程 4 步(三身份共用 · 组名不同) ==========
const FLOW_ITEMS: NavItem[] = [
  { to: '/diagnosis/new', icon: Stethoscope, label: '品牌体检', stepNumber: 1, module: 'diagnosis', dataGuide: 'diagnosis' },
  { to: '/pricing', icon: Calculator, label: '报价方案', stepNumber: 2, module: 'quote', dataGuide: 'pricing' },
  { to: '/writing', icon: BookOpen, label: 'AI 创作中心', stepNumber: 3, module: 'writing', dataGuide: 'writing' },
  { to: '/publish', icon: Send, label: '发布投放', stepNumber: 4, module: 'publish' },
];

const TODAY_SECTION: NavSection = {
  title: '', fixed: true,
  items: [{ to: '/dashboard', icon: Sun, label: '今日工作台', hint: '今天先看这里', dataGuide: 'today-board' }],
};

const REVIEW_SECTION: NavSection = {
  title: '效果复盘', key: 'review', defaultOpen: true,
  items: [{ to: '/monitoring', icon: Activity, label: '效果监测', module: 'monitoring', dataGuide: 'monitoring' }],
};

const SANDBOX_TUTORIAL_ICON_BY_ROUTE: Record<string, React.ComponentType<{ className?: string }>> = {
  '/diagnosis/new': Stethoscope,
  '/pricing': Calculator,
  '/writing': BookOpen,
  '/monitoring': Activity,
};

const SANDBOX_TUTORIAL_SECTIONS: NavSection[] = [{
  title: '',
  key: 'sandbox_tutorial_flow',
  numbered: true,
  fixed: true,
  items: SANDBOX_TUTORIAL_NAV_ITEMS.map(item => ({
    to: item.to,
    icon: SANDBOX_TUTORIAL_ICON_BY_ROUTE[item.to],
    label: item.label,
    stepNumber: item.stepNumber,
  })),
}];

const ACCOUNT_HELP_ITEMS: NavItem[] = [
  { to: '/account/profile', icon: User, label: '个人设置' },
  { to: '/feature-pricing', icon: Coins, label: '算力价格表', hint: '各功能用多少算力' },
  { to: '/help', icon: HelpCircle, label: '帮助中心', dataGuide: 'help' },
  { to: '/feedback', icon: MessageSquareWarning, label: '问题反馈' },
];

// 我的钱包(提现 / 佣金 / 抵扣偏好)· 服务商 + 管理员
// 🔴 [#222 a1b' 2026-09-16] 原注释写「三身份共用」—— 改完就是假话,过期注释不会红只会误导。
//    普通账号的钱包是「算力」组的 /customer/wallet + /customer/recharge(零售面)。
const WALLET_ITEM: NavItem = { to: '/wallet', icon: Wallet, label: '我的钱包', dataGuide: 'wallet' };
// 对外品牌(白标)· 普通用户填品牌即开通(2026-06-06 P1 放开),服务商/管理员经营后台也含
const WHITELABEL_ITEM: NavItem = { to: '/agent/whitelabel', icon: Palette, label: '对外品牌' };
const ORGANIZATION_ITEM: NavItem = { to: '/organization/team', icon: UsersRound, label: '团队与席位' };

// 经营后台(服务商 + 管理员代运营共用)
const BIZ_ITEMS: NavItem[] = [
  { to: '/agent/profit', icon: BarChart3, label: '经营总览' },
  { to: '/agent/inventory', icon: Boxes, label: sidebarLabel('agent_inventory'), hint: '进货 / 划拨' },
  { to: '/agent/pricing', icon: Tag, label: sidebarLabel('agent_pricing'), hint: '卖给客户的价格' },
  { to: '/agent/settlement', icon: Wallet, label: sidebarLabel('agent_settlement'), hint: '收益 / 提现' },
  { to: '/agent/promotion', icon: Megaphone, label: sidebarLabel('agent_promotion') },
  { to: '/marketing-materials', icon: ImagePlus, label: 'GEO 获客内容', hint: '成图 / 文案 / 话术' },
  WHITELABEL_ITEM,
  { to: '/agent/agreement', icon: FileSignature, label: sidebarLabel('agent_agreement') },
];

// ========== 普通用户 ==========
const NORMAL_SECTIONS: NavSection[] = [
  TODAY_SECTION,
  { title: '主流程 · 按顺序做', key: 'flow', numbered: true, defaultOpen: true, items: FLOW_ITEMS },
  REVIEW_SECTION,
  {
    title: '资料', key: 'profile', defaultOpen: true,
    items: [
      { to: '/my-clients', icon: Contact, label: '我的客户' },
      { to: '/my-brand', icon: Building2, label: '我的品牌' },
      WHITELABEL_ITEM,
      { to: '/marketing-materials', icon: ImagePlus, label: 'GEO 获客内容', hint: '成图 / 文案 / 话术' },
    ],
  },
  {
    title: '算力', key: 'credits', defaultOpen: true,
    items: [
      { to: '/customer/wallet', icon: Zap, label: '我的算力' },
      { to: '/customer/recharge', icon: Coins, label: '购买算力' },
    ],
  },
  {
    title: '账号', key: 'account', defaultOpen: true, topDivider: true,
    items: [
      ORGANIZATION_ITEM,
      /* 🔴 [#222 a1b'] 这里原来有 WALLET_ITEM(`/wallet` 服务商钱包面)。
         路由已对普通账号关死,菜单再留着就是一条通往「没有服务商权限」否决页的死链。
         普通账号的钱包在上面「算力」组:`/customer/wallet` + `/customer/recharge`。 */
      { to: '/referral', icon: Share2, label: '推荐有礼' },
      ...ACCOUNT_HELP_ITEMS,
    ],
  },
];

// ========== 服务商(代理) ==========
const AGENT_SECTIONS: NavSection[] = [
  TODAY_SECTION,
  { title: '客户交付 · 按顺序做', key: 'flow', numbered: true, defaultOpen: true, items: FLOW_ITEMS },
  REVIEW_SECTION,
  {
    title: '资料', key: 'profile', defaultOpen: true,
    items: [
      { to: '/my-clients', icon: Contact, label: '我的客户' },
      { to: '/my-brand', icon: Building2, label: '我的品牌' },
    ],
  },
  {
    title: '团队', key: 'organization', defaultOpen: true,
    items: [ORGANIZATION_ITEM],
  },
  {
    title: '经营后台', key: 'biz', defaultOpen: true,
    items: BIZ_ITEMS,
  },
  {
    title: '账号', key: 'account', defaultOpen: false, topDivider: true,
    items: [WALLET_ITEM, ...ACCOUNT_HELP_ITEMS],
  },
];

// ========== 管理员 ==========
// 主体 = 代运营主流程 + 效果 + 客户与资料 + 经营后台 + 6 管理分组 + 账号组。
// 管理员既管平台也替客户代运营,故保留经营后台 + 我的钱包(老板 2026-06-08 拍板不移除)。
const ADMIN_SECTIONS: NavSection[] = [
  TODAY_SECTION,
  { title: '代运营操作台 · 按顺序做', key: 'flow', numbered: true, defaultOpen: true, items: FLOW_ITEMS },
  REVIEW_SECTION,
  {
    title: '客户与资料', key: 'profile', defaultOpen: true,
    items: [
      { to: '/my-clients', icon: Contact, label: '我的客户' },
      { to: '/my-brand', icon: Building2, label: '我的品牌' },
    ],
  },
  {
    title: '团队', key: 'organization', defaultOpen: true,
    items: [ORGANIZATION_ITEM],
  },
  {
    title: '经营后台', key: 'biz', defaultOpen: true,
    items: BIZ_ITEMS,
  },
  {
    title: '管理 · 常用巡检', key: 'adm_patrol', defaultOpen: false, topDivider: true,
    items: [
      { to: '/tv', icon: Monitor, label: '大屏监控' },
      { to: '/admin/inventory-audit', icon: Scale, label: sidebarLabel('admin_fund_audit'), hint: '资金 / 算力核对' },
      // [invrel 2026-08-13] 沿用现役 sidebar 单一入口,不新建导航层级(工单 §P1-2)
      { to: '/admin/agent-inventory', icon: Boxes, label: '服务商库存与关系', hint: '入库 / 划拨 / 关系' },
      // 营销中心(2026-07-05 入口接线):今日军情/审批/活动/杠杆/效果(五闸默认关=只读)
      { to: '/admin/marketing', icon: Sparkles, label: '营销中心', hint: '军师建议 / 活动 / 物料' },
    ],
  },
  {
    title: '管理 · 财务与结算', key: 'adm_finance', defaultOpen: false,
    items: [
      { to: '/admin/finance', icon: Wallet, label: '财务中心' },
      { to: '/admin/orders', icon: Receipt, label: '订单管理' },
      { to: '/admin/withdrawals', icon: Banknote, label: '提现审核' },
      { to: '/admin/settlements', icon: Wallet, label: sidebarLabel('admin_settlements') },
      { to: '/admin/refunds', icon: Banknote, label: '充值退款工单中心' },
      { to: '/admin/diagnosis-fund-exceptions', icon: Banknote, label: '诊断资金异常处置' },
      { to: '/admin/pricing-center', icon: SlidersHorizontal, label: sidebarLabel('admin_pricing_center'), hint: '套餐 / 折扣 / 定价' },
      { to: '/admin/channel-tier', icon: Trophy, label: '渠道等级后台', hint: '等级 / 奖励 / 覆盖' },
      { to: '/admin/tax-profiles', icon: Receipt, label: '代理税务档案' },
    ],
  },
  {
    title: '管理 · 系统与权限', key: 'adm_sys', defaultOpen: false,
    items: [
      { to: '/settings', icon: Settings, label: '系统设置' },
      { to: '/admin/users', icon: UserCog, label: '用户管理' },
      { to: '/admin/organization-product-config', icon: UsersRound, label: '团队席位策略' },
      { to: '/admin/audit', icon: ScrollText, label: '审计日志' },
      { to: '/admin/ai-ops', icon: Bot, label: 'AI 运维控制塔' },
    ],
  },
  {
    title: '管理 · GEO 业务', key: 'adm_geo', defaultOpen: false,
    items: [
      { to: '/admin/research-monitor', icon: BookOpen, label: 'GEO 调研监测' },
      { to: '/admin/geo-placement-flywheel', icon: RadioTower, label: 'GEO 数据飞轮' },
      { to: '/admin/geo-observation-center', icon: BarChart3, label: '观测治理中心' },
      // [托管入口隐藏 2026-08-10] Owner 裁定隐藏,**能力保留,未来重启** ——
      //   页面 /admin/managed 与其代码一律不删,只是不给可达入口。
      //   恢复:把 MANAGED_ENTRY_ENABLED 改成 true(单点开关)。
      //   ⚠️ 别把它当死代码顺手清理掉。
      ...(MANAGED_ENTRY_ENABLED
        ? [{ to: '/admin/managed', icon: Zap, label: 'GEO 托管管理' }]
        : []),
      { to: '/admin/industry-corrections', icon: Database, label: '行业素材矫正' },
      { to: '/admin/media-directory', icon: Newspaper, label: '媒体目录分档' },
    ],
  },
  {
    title: '管理 · 代理治理', key: 'adm_gov', defaultOpen: false,
    items: [
      { to: '/admin/whitelabel', icon: Palette, label: '对外品牌授权' },
      { to: '/admin/binding-disputes', icon: Gavel, label: '客户归属争议' },
    ],
  },
  {
    title: '管理 · 支持', key: 'adm_support', defaultOpen: false,
    items: [
      { to: '/admin/help-center', icon: LifeBuoy, label: '帮助中心管理' },
    ],
  },
  {
    title: '账号', key: 'account', defaultOpen: false, topDivider: true,
    items: [WALLET_ITEM, ...ACCOUNT_HELP_ITEMS],
  },
];

// 员工不是独立商业主体：不展示个人钱包、充值、利润、库存、提现、推荐和平台治理。
// 每个业务入口仍由后端逐请求校验 capability、客户分配、审批和双腿预留。
const MEMBER_SECTIONS: NavSection[] = [
  { title: '客户交付 · 按顺序做', key: 'member_flow', numbered: true, defaultOpen: true, items: FLOW_ITEMS },
  REVIEW_SECTION,
  {
    title: '资料', key: 'member_profile', defaultOpen: true,
    items: [{ to: '/my-clients', icon: Contact, label: '分配给我的客户' }],
  },
  {
    title: '账号', key: 'member_account', defaultOpen: true, topDivider: true,
    items: [
      { to: '/account/profile', icon: User, label: '个人设置' },
      { to: '/help', icon: HelpCircle, label: '帮助中心', dataGuide: 'help' },
      { to: '/feedback', icon: MessageSquareWarning, label: '问题反馈' },
    ],
  },
];

export function AppSidebar() {
  const { user, hasModule, logout } = useAuth();
  const { isMember, overview } = useOrganization();
  // v3.6 白标(oem)：代理工作台 sidebar logo/品牌名。surface 自动判 agent；
  // D2（Owner 2026-07-22）：仅 backoffice_brand_allowed 独立授权才换代理 logo；
  // 未授权/admin/加载中 时 hook 的 brand 已解析为平台默认(SSOT)，故直接用 brand.company_name，
  // 不再硬编码平台名字面量(避免白标泄漏扫描误判)。
  const { brand, backofficeBrandAllowed } = useBranding({ userId: user?.id });
  const oemLogo = backofficeBrandAllowed ? brand?.logo_url : undefined;
  const brandAlt = brand?.company_name || '';
  const { resolvedTheme } = useTheme();
  const { channelTier } = useWallet();
  // [§13 出口可达性 2026-07-26] "待你确认"的发布任务数（单次拉取 + 模块级缓存，与
  // 发布中心横幅共用同一份数据，不轮询）。为 0 时不渲染任何东西。
  const pendingActionCount = usePendingUserActionCount();
  const { setOpen, setOpenMobile, state: sidebarState } = useSidebar();
  const navigate = useNavigate();
  const location = useLocation();
  // Stage 1 Batch 4 (2026-05-18) · 沙盒态下非教学路径项灰化 + 拦截点击
  const { isSandbox: isSandboxRaw } = useSandboxState();
  const screenshotMode = useScreenshotMode();
  // 2026-05-23 · 截图模式 = 视觉上当作非沙盒 · sidebar 不灰化 / 不显 spotlight / 不拦点击
  const isSandbox = isSandboxRaw && !screenshotMode;
  // tutorial stage 跨页面 spotlight 接力 · 'sidebar' 阶段在"品牌体检"项上挂引导气泡
  const tutorialStage = useTutorialStage();
  // [2026-05-28] 移动端 sidebar 引导由 MobileSidebarCoach 接管(汉堡→sidebar 两阶段)
  const isMobile = useIsMobile();

  // 教程外壳在桌面端始终展开，不能继承用户上一次折叠侧栏的本地状态。
  useEffect(() => {
    if (isSandbox && !isMobile) setOpen(true);
  }, [isMobile, isSandbox, setOpen]);

  const isAdmin = user?.is_admin === true;
  const isAgent = (user?.agent_level ?? 0) >= 1;

  // [IA v2] 分组折叠状态(用户手动展开/收起记忆)· 当前页所属分组渲染时强制展开(不写 localStorage)
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>(() => {
    try { return JSON.parse(localStorage.getItem(GROUPS_LS_KEY) || '{}'); } catch { return {}; }
  });

  const handleLogout = () => {
    logout();
    navigate('/login', { replace: true });
  };

  const handleNavClick = () => {
    setOpenMobile(false);
  };

  // 身份分流:管理员 > 服务商 > 普通用户(互斥)
  const baseSections: NavSection[] = isSandbox
    ? SANDBOX_TUTORIAL_SECTIONS
    : isMember
      ? MEMBER_SECTIONS
      : isAdmin
        ? ADMIN_SECTIONS
        : isAgent
          ? AGENT_SECTIONS
          : NORMAL_SECTIONS;

  // ========== 权限/沙盒过滤 ==========
  // 身份已在 baseSections 分流 · 此处仅保留 module 级 RBAC(hasModule)· 不改权限语义
  const ADMIN_MODULES = new Set(['users', 'roles', 'audit']);
  const filterItems = (items: NavItem[]) => items.filter((item) => {
    // 教程四步是固定外壳，不按真实账号的成员能力或后台模块裁剪。
    // 右侧真实 handler 的写操作仍由沙盒服务端/拦截器做零副作用限制。
    if (isSandbox) return true;
    if (isMember && item.module) {
      // 与 OperationRegistry 的员工规则一致：required_module → `${module}.` capability。
      // 不在前端再维护一份动作名清单，否则新增只读能力时两边必然漂移。
      const prefix = `${item.module}.`;
      if (!overview?.identity.capabilities.some(capability => capability.startsWith(prefix))) return false;
    }
    if (item.module && ADMIN_MODULES.has(item.module) && !hasModule(item.module)) return false;
    return true;
  });

  const filteredSections = baseSections
    .map((s) => ({ ...s, items: filterItems(s.items) }))
    .filter((s) => s.items.length > 0);

  const isActive = (path: string) => {
    // [WO_260] 原还有 `|| path === '/social'`:侧栏早已没有社媒项,社媒随 E3 删域。
    if (path === '/') return location.pathname === path;
    return location.pathname.startsWith(path);
  };

  // 当前页所属分组(渲染时强制展开 · 高亮)
  const sectionHasActive = (section: NavSection) => section.items.some((it) => isActive(it.to));

  const toggleGroup = (section: NavSection) => {
    if (!section.key) return;
    // [IA v2] 当前页所属分组强制展开(铁律)· 锁定不可手动折叠 → 标题点击直接 no-op,
    // 避免把"过期折叠态"静默写入 localStorage 导致导航离开后入口意外收起(对抗审计 P2)
    if (sectionHasActive(section)) return;
    const cur = openGroups[section.key] ?? section.defaultOpen ?? true;
    const next = { ...openGroups, [section.key]: !cur };
    setOpenGroups(next);
    try { localStorage.setItem(GROUPS_LS_KEY, JSON.stringify(next)); } catch { /* ignore */ }
  };

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center px-2 py-2 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0">
          {/* [2026-06-04 老板订正] 后台操作页保留平台 LOGO(全域上榜)· 仅 OEM(花钱单独部署)换代理自有 logo · 客户面才白标 */}
          {isMobile || sidebarState === 'expanded' ? (
            <img
              src={isSandbox ? (resolvedTheme === 'dark' ? logoLight : logoDark) : (oemLogo || (resolvedTheme === 'dark' ? logoLight : logoDark))}
              alt={isSandbox ? '全域上榜GEO交付系统' : brandAlt}
              className="h-14 w-auto object-contain"
            />
          ) : (
            <img
              src={isSandbox ? (resolvedTheme === 'dark' ? logoIconDark : logoIconLight) : (oemLogo || (resolvedTheme === 'dark' ? logoIconDark : logoIconLight))}
              alt={isSandbox ? '全域上榜GEO交付系统' : brandAlt}
              className="h-5 w-5 object-contain mx-auto"
            />
          )}
        </div>
        {/* Phase 07 (CTO-15.23 2026-05-04) · 全局客户切换器 · 全栈聚合 · 对标 Linear */}
        <div className="px-2 pb-2 group-data-[collapsible=icon]:px-0">
          {isSandbox ? (
            <div
              data-testid="sandbox-canonical-client"
              className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 group-data-[collapsible=icon]:hidden"
            >
              <div className="text-[10px] font-medium text-amber-700 dark:text-amber-300">标准教程客户</div>
              <div className="mt-0.5 truncate text-sm font-semibold text-sidebar-foreground">一路顺风出行服务</div>
              <div className="mt-0.5 text-[10px] text-muted-foreground">固定演示数据 · 零扣费</div>
            </div>
          ) : (
            <ClientSwitcherSidebar />
          )}
        </div>
      </SidebarHeader>

      <SidebarContent className="sidebar-scroll">
        {filteredSections.map((section, idx) => {
          const forceOpen = sectionHasActive(section);
          const storedOpen = section.key ? (openGroups[section.key] ?? section.defaultOpen ?? true) : true;
          const open = section.fixed || forceOpen || storedOpen;
          const collapsible = !section.fixed && !!section.title;

          return (
            <div key={section.key || section.title || `_g${idx}`}>
              {section.topDivider && <SidebarSeparator />}
              <SidebarGroup>
                {/* 分组标题:可点击折叠(只标题可折叠)· 置顶单项组无标题 */}
                {collapsible && (
                  <button
                    type="button"
                    onClick={() => toggleGroup(section)}
                    aria-expanded={open}
                    title={forceOpen ? '当前所在分组 · 保持展开' : undefined}
                    className={cn(
                      'flex h-8 w-full shrink-0 items-center gap-1 rounded-md px-2 text-xs font-medium',
                      'text-muted-foreground/80 hover:text-foreground transition-colors',
                      forceOpen && 'cursor-default',
                      'group-data-[collapsible=icon]:hidden',
                    )}
                  >
                    <span className="flex-1 truncate text-left">{section.title}</span>
                    <ChevronDown
                      className={cn(
                        'size-3.5 shrink-0 transition-transform duration-150',
                        open ? '' : '-rotate-90',
                      )}
                    />
                  </button>
                )}

                {/* 子项容器:grid-rows 0fr↔1fr 折叠动画(150ms)· icon 整栏折叠态强制展开保留图标 */}
                <div
                  className={cn(
                    'grid overflow-hidden transition-[grid-template-rows] duration-150 ease-out',
                    open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]',
                    'group-data-[collapsible=icon]:grid-rows-[1fr]',
                  )}
                >
                  <div className="min-h-0 overflow-hidden">
                    <SidebarGroupContent>
                      <SidebarMenu>
                        {section.items.map((item) => {
                          // 沙盒态下非教学路径灰化 + 拦截 · 防真实账号数据漏出
                          const sandboxBlocked = isSandbox && !isSandboxAllowed(item.to);
                          // 沙盒各 step 的 sidebar 引导目标
                          const tutorialNavItem = isSandbox
                            ? SANDBOX_TUTORIAL_NAV_ITEMS.find(navItem => navItem.to === item.to) ?? null
                            : null;
                          const activeTutorialNavItem = isSandbox
                            ? getSandboxTutorialNavItem(tutorialStage)
                            : null;
                          const sandboxSidebarConfig = activeTutorialNavItem?.to === item.to
                            ? activeTutorialNavItem
                            : null;
                          const handleClick = (e: React.MouseEvent) => {
                            if (sandboxBlocked) {
                              e.preventDefault();
                              lazyToast.info('教程模式下此页不可用', {
                                description: '退出教程后即可访问 · 顶部琥珀横幅有"退出教程"按钮',
                                duration: 3500,
                              });
                              return;
                            }
                            // 沙盒里点了 sidebar 教学目标 → 推进到下一阶段
                            if (sandboxSidebarConfig) {
                              setTutorialStage(sandboxSidebarConfig.nextStage);
                            }
                            handleNavClick();
                          };
                          const navLinkEl = (
                            <NavLink
                              to={item.to}
                              onClick={handleClick}
                              data-guide={item.dataGuide}
                              data-mobile-coach={tutorialNavItem ? `sidebar-${tutorialNavItem.stepId}` : undefined}
                              data-sandbox-coach-anchor={sandboxSidebarConfig?.featureId}
                              className={sandboxBlocked ? 'opacity-40 cursor-not-allowed' : ''}
                              aria-disabled={sandboxBlocked || undefined}
                            >
                              {section.numbered && item.stepNumber ? (
                                <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-sidebar-primary text-[11px] font-semibold leading-none text-sidebar-primary-foreground">
                                  {item.stepNumber}
                                </span>
                              ) : (
                                <item.icon className="size-4" />
                              )}
                              <span className="flex-1 group-data-[collapsible=icon]:hidden">
                                {item.label}
                              </span>
                              {/* [§13 出口可达性 2026-07-26] "待你确认"的发布任务计数。
                                  只挂在 /publish 这一项上；数据来自模块级缓存的单次拉取
                                  (usePendingUserActions)，不轮询 —— 全站每页轮询一个边缘态，
                                  代价远大于收益。 */}
                              {item.to === '/publish' && pendingActionCount > 0 && !sandboxBlocked && (
                                <span
                                  data-testid="sidebar-pending-actions"
                                  title={`${pendingActionCount} 条发布任务待你确认`}
                                  className="ml-auto inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-full bg-amber-500 px-1 text-[10px] font-semibold leading-none text-white group-data-[collapsible=icon]:hidden"
                                >
                                  {pendingActionCount > 99 ? '99+' : pendingActionCount}
                                </span>
                              )}
                              {item.hint && !sandboxBlocked && (
                                <span className="text-[10px] text-muted-foreground/50 ml-auto group-data-[collapsible=icon]:hidden">
                                  {item.hint}
                                </span>
                              )}
                              {sandboxBlocked && (
                                <span className="text-[10px] text-amber-600/70 ml-auto group-data-[collapsible=icon]:hidden">
                                  教程外
                                </span>
                              )}
                            </NavLink>
                          );
                          const menuButtonEl = (
                            <SidebarMenuButton
                              asChild
                              isActive={isActive(item.to)}
                              tooltip={sandboxBlocked ? `${item.label} · 教程模式下不可用` : item.label}
                            >
                              {navLinkEl}
                            </SidebarMenuButton>
                          );
                          return (
                            <SidebarMenuItem key={item.to}>
                              {sandboxSidebarConfig && !isMobile ? (
                                <Suspense fallback={menuButtonEl}>
                                  <FeatureTooltip
                                    featureId={sandboxSidebarConfig.featureId}
                                    stepId={sandboxSidebarConfig.stepId}
                                    title={sandboxSidebarConfig.title}
                                    content={sandboxSidebarConfig.content}
                                    side="right"
                                    wrapClassName="block"
                                    targetSelector={`[data-sandbox-coach-anchor="${sandboxSidebarConfig.featureId}"]`}
                                  >
                                    {menuButtonEl}
                                  </FeatureTooltip>
                                </Suspense>
                              ) : (
                                menuButtonEl
                              )}
                            </SidebarMenuItem>
                          );
                        })}
                      </SidebarMenu>
                    </SidebarGroupContent>
                  </div>
                </div>
              </SidebarGroup>
            </div>
          );
        })}
      </SidebarContent>

      <SidebarFooter>
        <SidebarSeparator />
        {!isSandbox && isAgent && channelTier?.enabled && channelTier.effective_tier && channelTier.effective_tier !== 'none' && (
          <div className="px-2 pt-1 group-data-[collapsible=icon]:hidden">
            <ChannelTierBadge
              compact
              tier={channelTier.effective_tier}
              enabled={channelTier.enabled}
              founder={channelTier.is_founder}
              founderRank={channelTier.founder_rank ?? null}
            />
          </div>
        )}
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              className="text-sidebar-foreground/70 hover:text-destructive"
              tooltip="退出登录"
              aria-label="退出登录"
              title="退出登录"
              onClick={handleLogout}
            >
              <LogOut className="size-4" />
              <span className="flex-1 truncate">
                {isSandbox ? '教程账号' : (user?.display_name || user?.username || '用户')}
              </span>
              <span className="text-[10px] text-muted-foreground/60 shrink-0">退出</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}
