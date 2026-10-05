/**
 * Header — 顶部导航栏
 * 包含: SidebarTrigger(移动端汉堡菜单)、面包屑、主题切换
 */

import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { NotificationBell } from './NotificationBell';
import { Separator } from '@/components/ui/separator';
import { SidebarTrigger } from '@/components/ui/sidebar';
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
} from '@/components/ui/breadcrumb';
import { ThemeToggle } from './ThemeToggle';
import { HeaderTutorialButton } from '@/components/onboarding/TutorialVideoButton';
import { useWallet } from '@/context/WalletContext';
import { useAuth } from '@/context/AuthContext';
import { useBranding } from '@/hooks/useWhitelabel';
import { useUserMode, type UserMode } from '@/context/UserModeContext';
import { Wallet, FlaskConical, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Badge } from '@/components/ui/badge';

// [WO_260 · 2026-09-23] 已删 24 条:社媒 /social 一族 16 条 + D 桶 7 条(/advisors /references /geo-research /insights /placement
//   /agent/preview /admin/roles)+ 从来不存在的 /agent/quote 1 条。它们要么随 E3 删域、要么本来就 404;都不是任何在役路由的前缀,在役页标题不变。
const ROUTE_LABELS: Record<string, string> = {
  '/': '首页看板',
  '/dashboard': '今日工作台',
  // 🔴 [#165 F5] 侧栏首项改指 /dashboard 后必须有这一条：标题表查不到会掉到 fallbackBrand（页头显示代理品牌名而不是页名）。
  // 下面那条留着：书签还会打到 /dashboard/today（路由仍在，只是立即重定向）。
  '/dashboard/today': '今日工作台',
  '/diagnosis/new': '品牌体检',
  '/history': '历史记录',
  '/brands': '客户管理',
  '/my-clients': '我的客户',
  '/my-brand': '我的品牌',
  '/pricing': '报价方案',
  '/monitoring': '效果监测',
  '/publish': '发布中心',
  '/portal': '客户门户',
  '/writing': 'AI 创作中心',
  '/wallet': '我的钱包',
  // [2026-06-07] /wallet/recharge 老页已废弃(平台硬编码价 · 不绑服务商系数)· 路由保留兼容老链接 · 标题保留
  '/wallet/recharge': '充值',
  // [2026-06-07] /customer/recharge = 新版 BuyCredit · 已绑客户走邀请服务商系数 SKU · 主流量入口
  '/customer/recharge': '购买算力',
  '/customer/wallet': '我的算力',
  '/feature-pricing': '算力价格表',
  '/referral': '推荐有礼',
  '/account/profile': '个人设置',
  '/help': '帮助中心',
  '/feedback': '问题反馈',
  '/notifications': '通知中心',
  '/agent/whitelabel': '对外品牌',
  '/admin/whitelabel': '对外品牌授权',
  '/agent/profit': '经营总览',
  '/agent/promotion': '推广获客',
  '/agent/inventory': '算力库存',
  '/agent/pricing': '客户售价',
  '/agent/settlement': '提现结算',
  '/agent/agreement': '合作协议',
  '/reports': '报告管理',
  '/settings': '系统设置',
  '/admin/users': '用户管理',
  '/admin/organization-product-config': '团队席位策略',
  '/admin/audit': '审计日志',
};

function getPageTitle(pathname: string, fallbackBrand: string): string {
  // 精确匹配
  if (ROUTE_LABELS[pathname]) return ROUTE_LABELS[pathname];
  // 前缀匹配（如 /diagnosis/progress/123）
  const prefix = Object.keys(ROUTE_LABELS)
    .filter(k => k !== '/')
    .sort((a, b) => b.length - a.length)
    .find(k => pathname.startsWith(k));
  // v3.6 白标(oem)：未匹配路由的兜底标题用代理品牌名（无 oem 则平台默认）
  return prefix ? ROUTE_LABELS[prefix] : fallbackBrand;
}

function BalanceChip() {
  const { totalPoints, loading, status, lastUpdatedAt } = useWallet();
  const navigate = useNavigate();

  if (loading && !lastUpdatedAt) return null;
  const isUnknown = status === 'initial' || status === 'error';
  const isStale = status === 'stale';

  return (
    <button
      onClick={() => navigate('/wallet')}
      aria-label={isUnknown ? '余额暂时无法确认，打开钱包重试' : `当前余额 ${totalPoints.toLocaleString()} 算力，打开钱包`}
      title={isUnknown ? '余额暂时无法确认，点击重试' : (isStale ? '余额数据可能已过期，点击查看' : '打开钱包与交易记录')}
      className={cn(
        'flex min-h-11 max-w-[76px] items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium sm:min-h-0 sm:max-w-none sm:gap-1.5 sm:px-2.5',
        'bg-secondary hover:bg-muted border border-border transition-colors cursor-pointer'
      )}
    >
      <Wallet className="h-3.5 w-3.5 text-muted-foreground" />
      <span className="min-w-0 truncate text-foreground">{isUnknown ? '待确认' : totalPoints.toLocaleString()}</span>
      {!isUnknown && <span className="text-muted-foreground hidden sm:inline">{isStale ? '可能过期' : '算力'}</span>}
    </button>
  );
}

// ========== 身份预设 ==========

const IDENTITY_PRESETS = [
  { label: '普通用户', desc: '0算力，刚注册', paidPoints: 0, bonusPoints: 680, agentLevel: 'free' as const, totalRecharged: 0 },
  { label: '普通用户（已充值）', desc: '充值过¥200', paidPoints: 15000, bonusPoints: 3900, agentLevel: 'paid' as const, totalRecharged: 20000 },
  { label: '服务方', desc: '消费≥¥500 或 推荐≥5人付费', paidPoints: 5000, bonusPoints: 1200, agentLevel: 'L1' as const, totalRecharged: 5000 },
  { label: '服务方（大客户）', desc: '消费≥¥2000', paidPoints: 200000, bonusPoints: 50000, agentLevel: 'L2' as const, totalRecharged: 200000 },
  { label: '余额不足', desc: '测试扣费拦截', paidPoints: 0, bonusPoints: 10, agentLevel: 'paid' as const, totalRecharged: 5000 },
  { label: '土豪用户', desc: '大量算力', paidPoints: 500000, bonusPoints: 100000, agentLevel: 'L2' as const, totalRecharged: 500000 },
];

const MODE_SWITCH_OPTIONS: { label: string; value: UserMode }[] = [
  { label: 'GEO模式', value: 'geo' },
  { label: '社媒模式', value: 'social' },
  { label: '全量模式', value: 'full' },
];

function TestModePanel() {
  const { user } = useAuth();
  const { setTestMode, testMode, agentLevel, paidPoints, bonusPoints } = useWallet();
  const { mode, setMode } = useUserMode();
  const [open, setOpen] = useState(false);

  if (!user?.is_admin) return null;

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          'flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium transition-colors cursor-pointer border',
          testMode
            ? 'bg-amber-500/20 text-amber-400 border-amber-500/40'
            : 'bg-secondary text-muted-foreground border-border hover:text-foreground'
        )}
      >
        <FlaskConical className="size-3.5" />
        <span className="hidden sm:inline">{testMode ? '测试中' : '测试'}</span>
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full mt-2 z-50 w-72 rounded-xl border border-border bg-card/95 backdrop-blur-xl shadow-2xl p-4 space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-foreground">余额状态模拟</h3>
              <button
                type="button"
                aria-label="关闭测试面板"
                title="关闭测试面板"
                onClick={() => setOpen(false)}
                className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground sm:min-h-0 sm:min-w-0"
              >
                <X className="size-4" />
              </button>
            </div>

            {testMode && (
              <div className="text-xs text-amber-400 bg-amber-500/10 rounded-lg px-3 py-2">
                认证身份不变: {agentLevel} | 充值:{paidPoints.toLocaleString()} 赠送:{bonusPoints.toLocaleString()}
              </div>
            )}

            <div className="grid grid-cols-2 gap-2">
              {IDENTITY_PRESETS.map(preset => (
                <button
                  key={preset.label}
                  onClick={() => {
                    setTestMode({
                      paidPoints: preset.paidPoints,
                      bonusPoints: preset.bonusPoints,
                      agentLevel: preset.agentLevel,
                      totalRecharged: preset.totalRecharged,
                    });
                  }}
                  className="flex flex-col items-start gap-0.5 p-2.5 rounded-lg border border-border bg-secondary/50 hover:bg-muted hover:border-foreground/20 transition-colors text-left cursor-pointer"
                >
                  <span className="text-xs font-medium text-foreground">{preset.label}</span>
                  <span className="text-[10px] text-muted-foreground">{preset.desc}</span>
                </button>
              ))}
            </div>

            <div className="border-t border-border pt-3 space-y-2">
              <h4 className="text-xs font-medium text-muted-foreground">模式切换</h4>
              <div className="grid grid-cols-3 gap-1.5">
                {MODE_SWITCH_OPTIONS.map(opt => (
                  <button
                    key={opt.value}
                    onClick={() => setMode(opt.value)}
                    className={cn(
                      'text-xs py-2 rounded-lg border transition-colors cursor-pointer',
                      mode === opt.value
                        ? 'border-foreground/30 bg-foreground/10 text-foreground font-medium'
                        : 'border-border text-muted-foreground hover:text-foreground hover:bg-muted'
                    )}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>

            {testMode && (
              <button
                onClick={() => { setTestMode(null); setOpen(false); }}
                className="w-full text-xs py-2 rounded-lg border border-border text-muted-foreground hover:text-foreground hover:bg-muted transition-colors cursor-pointer"
              >
                退出测试模式（恢复真实数据）
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export function Header() {
  const location = useLocation();
  // v3.6 白标(oem)：兜底页面标题用代理品牌名。D2（Owner 2026-07-22）：
  // product_name 仅 backoffice 独立授权才出；未授权时 hook 的 brand 已解析为平台默认(SSOT)，fail-closed。
  const { user } = useAuth();
  const { brand, backofficeBrandAllowed } = useBranding({ userId: user?.id });
  const fallbackBrand = (backofficeBrandAllowed ? brand?.product_name : undefined) || brand?.company_name || '';
  const title = getPageTitle(location.pathname, fallbackBrand);
  const { testMode } = useWallet();

  return (
    <header className={cn(
      "bg-background/80 backdrop-blur-sm sticky top-0 z-20 flex h-12 shrink-0 items-center gap-2 border-b px-4",
      testMode && "border-b-amber-500/30"
    )}>
      {/* data-mobile-coach="hamburger" · MobileSidebarCoach 阶段 1 挖洞锚点 */}
      <SidebarTrigger className="-ml-1" data-mobile-coach="hamburger" />
      <Separator orientation="vertical" className="mr-2 !h-4" />
      <Breadcrumb className="min-w-0 flex-1 sm:flex-initial">
        <BreadcrumbList className="flex-nowrap">
          <BreadcrumbItem className="min-w-0">
            <BreadcrumbPage className="text-sm font-medium truncate">
              {title}
            </BreadcrumbPage>
          </BreadcrumbItem>
        </BreadcrumbList>
      </Breadcrumb>
      {testMode && (
        <Badge variant="outline" className="hidden sm:inline-flex text-[10px] text-amber-400 border-amber-500/30 bg-amber-500/10">
          测试模式
        </Badge>
      )}
      {/* Layer 2 教程视频统一入口 · 全站固定在页面标题旁 · 按路由自动切视频, 无教程的页面自动隐藏 */}
      {/* 「最后一公里」手机壳:教程胶囊在手机端收起(仍可在帮助中心回看),不长期占主导航挤标题 */}
      <div className="hidden sm:contents">
        <HeaderTutorialButton />
      </div>
      {/* 手机端保留通知入口；测试与主题工具在 sm 以上恢复。 */}
      <div className="ml-auto flex items-center gap-1.5 sm:gap-2">
        <div className="hidden sm:contents">
          <TestModePanel />
        </div>
        <NotificationBell />
        <BalanceChip />
        <div className="hidden sm:contents">
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
