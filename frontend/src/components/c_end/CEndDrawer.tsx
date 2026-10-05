/**
 * CEndDrawer — C 端侧边抽屉（v3.3 重设计 · v3.6 置顶卡智能化）
 *
 * 结构：
 *   顶部品牌卡片（仅 <85% 或无品牌时置顶 + 呼吸灯提醒补全；≥85% 折叠进"我的"菜单）
 *   对话 → 我的 → 操作工具 → (代理组) → 推广 → 设置
 */

import { useEffect, useState } from 'react';
import {
  MessageSquare, History, FileText, PenLine, Send,
  BarChart3, LineChart, Wallet, Sparkles, CreditCard,
  Gift, Settings, Crown, Briefcase, LogOut, Building2,
  ChevronRight, Rocket,
} from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { useNavigate } from 'react-router-dom';
import { useCEndPanel } from '@/context/CEndPanelContext';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import { usePartnerFlag } from '@/hooks/usePartnerFlag';
import { toast } from 'sonner';
// CTO-15.2 commit 19: 老板拍方案 C - 托管套餐弹 Dialog 不离开 /c/chat
import { ManagedSheet } from './ManagedSheet';
import { MANAGED_ENTRY_ENABLED } from '@/config/managedEntryGate';

interface Props {
  onNavigate?: () => void;
}

// commit 27 (CTO-15.2 2026-04-19): brand 缓存 key 暴露给其他组件
// 用户在 BrandDetailPage / AiFillDialog 等地方改完 brand 后, 调 clearCEndBrandCache()
// 让 CEndDrawer 下次打开时拉最新 brand (而非等 5 分钟 TTL 自然过期)
export const CEND_BRAND_CACHE_KEY = 'omnirank_cend_brand_v1';
export function clearCEndBrandCache() {
  try {
    sessionStorage.removeItem(CEND_BRAND_CACHE_KEY);
  } catch { /* 静默 */ }
}

interface DrawerItem {
  label: string;
  href?: string;
  icon: React.ComponentType<{ className?: string }>;
  onClick?: () => void;
  /** 副标题（小字灰色，写在主标题下方，用于"推广有礼"等场景） */
  hint?: string;
  /** 图标/主标题高亮色（代理入口组用琥珀/金色） */
  accent?: 'default' | 'gold';
}

// ========== 品牌数据结构 ==========

interface BrandInfo {
  id: number;
  name: string;
  industry?: string;
  company_name?: string;
  /** [CTO-15.3 2026-04-20] 后端 utils/brand_completeness.py 算的多维度完善度 0-100
      (基础身份 30 + 核心业务 30 + 深度营销 20 + v3.6 深度分析 20) */
  completeness?: number;
}

// v3.6 阈值：≥85% 视为"已完善"，折叠进"我的"菜单；<85% 置顶呼吸灯提醒
const COMPLETENESS_THRESHOLD = 85;

// 注册时系统占位 brand.name 模板: "XXX的创作空间_数字"(本地 fallback 算法用)
const DEFAULT_NAME_PATTERN = /的创作空间_\d+$/;

function calcScore(brand: BrandInfo | null): number {
  if (!brand) return 0;
  // 优先用后端算好的多维度 completeness(utils/brand_completeness.py)
  if (typeof brand.completeness === 'number') return brand.completeness;
  // fallback: 后端未返 completeness 时的本地简化算法(仅 3 字段)
  const isDefaultName = DEFAULT_NAME_PATTERN.test(brand.name || '');
  const nameScore = brand.name && !isDefaultName ? 30 : 0;
  const industryScore = brand.industry ? 35 : 0;
  const companyScore = brand.company_name ? 35 : 0;
  return nameScore + industryScore + companyScore;
}

// ========== 置顶品牌卡片（仅 <85% 或无品牌时渲染） ==========

function BrandCard({
  brand,
  score,
  onClick,
}: {
  brand: BrandInfo | null;
  score: number;
  onClick: () => void;
}) {
  const hasBrand = !!brand;
  // v3.6: 只有真正需要补全时（<60%）才呼吸灯，避免 60-85 区间"每次开都闪"
  const strongAttention = !hasBrand || score < 60;
  return (
    <button
      onClick={onClick}
      className={cn(
        'rounded-lg border p-3 text-left transition-colors hover:bg-accent/50',
        strongAttention
          ? 'border-amber-500/40 bg-amber-500/5 animate-pulse'
          : 'border-amber-500/20 bg-amber-500/[0.03]',
      )}
      style={{ width: 'calc(100% - 24px)', margin: '12px auto 4px' }}
    >
      <div className="flex items-center gap-2.5">
        <Building2 className={cn('h-5 w-5 shrink-0', hasBrand ? 'text-amber-400' : 'text-amber-400')} />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium truncate">
            {brand?.name || '设置我的品牌'}
          </div>
          <div className="text-[10px] text-muted-foreground mt-0.5">
            {!hasBrand ? '在对话里告诉 AI 你的品牌名' : `营销资料完善度 ${score}%`}
          </div>
        </div>
        <span className="text-[9px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 shrink-0">
          {hasBrand ? '待补齐' : '待设置'}
        </span>
        <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/40 shrink-0" />
      </div>
      {hasBrand && (
        <div className="mt-2 h-1 bg-muted rounded-full overflow-hidden">
          <div
            className={cn(
              'h-full rounded-full transition-all',
              score >= 60 ? 'bg-amber-500' : 'bg-red-500',
            )}
            style={{ width: `${score}%` }}
          />
        </div>
      )}
    </button>
  );
}

// ========== 主组件 ==========

export function CEndDrawer({ onNavigate }: Props) {
  const { logout, user, isLoading: authLoading } = useAuth();
  const isAdmin = user?.is_admin === true;
  const navigate = useNavigate();
  const panel = useCEndPanel();
  const isAgent = (user?.agent_level ?? 0) >= 1;
  const { enabled: partnerFlagEnabled } = usePartnerFlag();

  // v3.6: 品牌数据提升到主组件 — 既驱动"是否置顶卡片"也驱动"我的"组是否带"我的品牌"菜单项
  // commit 27 (CTO-15.2 2026-04-19): 老板反馈"每次开关 Drawer 重新嗅探 brand 1秒延迟"
  // 修法: sessionStorage 缓存 5 分钟 TTL,命中直接读不重新 fetch
  // 参考 usePartnerFlag 同样模式
  const BRAND_CACHE_TTL_MS = 5 * 60 * 1000;  // 5 分钟 TTL
  const readBrandCache = (): BrandInfo | null => {
    try {
      const raw = sessionStorage.getItem(CEND_BRAND_CACHE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (Date.now() - (parsed.cachedAt || 0) > BRAND_CACHE_TTL_MS) return null;
      return parsed.brand || null;
    } catch { return null; }
  };
  const writeBrandCache = (b: BrandInfo | null) => {
    try {
      sessionStorage.setItem(CEND_BRAND_CACHE_KEY, JSON.stringify({ brand: b, cachedAt: Date.now() }));
    } catch { /* 静默 */ }
  };

  const cachedBrand = readBrandCache();
  const [brand, setBrand] = useState<BrandInfo | null>(cachedBrand);
  const [brandLoading, setBrandLoading] = useState<boolean>(!cachedBrand);
  // CTO-15.2 commit 19: 托管套餐 Dialog (方案 C 不跳转 /managed)
  const [managedSheetOpen, setManagedSheetOpen] = useState(false);

  useEffect(() => {
    // 命中 sessionStorage 缓存 → 跳过 fetch (避免每次开关 Drawer 重新嗅探)
    if (cachedBrand) return;
    // [CTO-15.3 2026-04-20] 换用 /api/my-brand(已 JOIN profile + 后端算好多维度 completeness)
    //   原 /api/client-context/list 只返 brand 字段, 算不出跨表深度营销/深度分析维度
    authFetch('/api/my-brand')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        const b = data?.success && data?.brand;
        if (b) {
          const info: BrandInfo = {
            id: b.id,
            name: b.name || '未命名',
            industry: b.industry,
            company_name: b.company_name,
            completeness: typeof b.completeness === 'number' ? b.completeness : undefined,
          };
          setBrand(info);
          writeBrandCache(info);
        } else {
          writeBrandCache(null);  // 写入 null 也缓存,避免反复无意义请求
        }
      })
      .catch(() => {})
      .finally(() => setBrandLoading(false));
  }, []);

  const score = calcScore(brand);
  // v3.6: ≥85% 折叠进菜单不再置顶；<85% 或无品牌（且已加载完）才渲染置顶卡
  const showTopCard = !brandLoading && (!brand || score < COMPLETENESS_THRESHOLD);
  const showMyBrandInMenu = !brandLoading && brand && score >= COMPLETENESS_THRESHOLD;

  const handleMyBrandClick = () => {
    panel?.openInPanel('/my-brand');
    onNavigate?.();
  };

  // 2026-04-18 v3.4: 身份感知文案
  // r10 (2026-05-27): 客户面去工程词 · 积分→额度
  const referralHint = isAgent
    ? '把链接发给客户，成交得服务收益（可提现）'
    : '邀请朋友得奖励算力（可消费）';

  // v1.1 审核制: flag on + L0 时隐藏"升级代理"组 (入口搬到设置菜单深层)
  //              flag on + L1+ 保留"切到工作台"
  //              flag off 保持老样子
  // admin 也能切工作台（admin 身份独立于 agent_level）
  // 数据未加载完成时默认显示（避免首次渲染闪烁不显示）
  const dataReady = !authLoading;
  const showAgentSection = !dataReady || isAgent || isAdmin || !partnerFlagEnabled;

  const sections: { title: string; items: DrawerItem[] }[] = [
    {
      title: '对话',
      items: [
        { label: '新对话', icon: MessageSquare, href: '/c/chat' },
        { label: '历史对话', icon: History, href: '/c/history' },
      ],
    },
    {
      title: '我的',
      items: [
        // v3.6: 品牌已完善（≥85%）时折叠进菜单；否则入口在顶部置顶卡
        ...(showMyBrandInMenu ? [{
          label: '我的品牌',
          icon: Building2,
          hint: brand!.name,
          onClick: handleMyBrandClick,
        }] : []),
        { label: '我的钱包', icon: Wallet, href: '/wallet' },
      ],
    },
    {
      title: '操作工具',
      items: [
        { label: '出方案', icon: FileText, href: '/c/geo-plan' },
        // commit 21: 老板反馈"C 端查看报告的地方不见了" → 加"看报告"快捷入口
        // 跳 /c/history?tab=reports (旧 C 端历史页 新增"诊断报告" Tab)
        { label: '看报告', icon: FileText, href: '/c/history?tab=reports' },
        { label: '写文章', icon: PenLine, href: '/writing' },
        { label: '发布文章', icon: Send, href: '/publish' },
        { label: '看排名', icon: BarChart3, href: '/monitoring' },
        { label: '看数据', icon: LineChart, href: '/' },
        // CTO-15.2 commit 19: 不再 href='/managed'(避免 C 端进代理 Layout 看到代理 Sidebar)
        // [托管入口隐藏 2026-08-10] Owner 裁定隐藏,**能力保留,未来重启**。
        //   ManagedSheet 组件与其 API 一律不删,只是这个按钮不出现。
        //   恢复:MANAGED_ENTRY_ENABLED → true(单点开关)。别当死代码清理。
        ...(MANAGED_ENTRY_ENABLED
          ? [{ label: '托管套餐', icon: Sparkles, onClick: () => setManagedSheetOpen(true) }]
          : []),
      ],
    },
    ...(showAgentSection ? [{
      // 代理权益组 — 按身份分化; flag on + L0 时此组整体隐藏
      title: (isAgent || isAdmin) ? '服务方工作台' : '升级为服务商',
      items: (!isAgent && !isAdmin)
        ? [{
            label: '成为服务商',
            icon: Rocket,
            hint: '自设客户售价赚服务收益 + 白标 + CRM',
            accent: 'gold' as const,
            // [2026-06-07] c-end 板块已废弃 · 此处 /wallet/recharge 入口跟随保留 · 主流量走 /customer/recharge
            // 老 c-end 抽屉如果还有用户访问 · 路由会进入老 RechargePage(已加废弃 banner 引导新页)
            href: '/wallet/recharge',
          }]
        : [{
            label: '切到工作台',
            icon: Crown,
            hint: '进入服务方操作中心，管理客户和报价',
            accent: 'gold' as const,
            // [CTO-15.5 2026-04-21] 老板反馈"切换还是原地没动"
            // 根因: 原代码 res.ok=false 时完全无反应(没 toast 没 throw),用户以为按钮坏了
            // 修: 失败显 toast + 跳转失败兜底重定向 · 对齐 旧 C 端布局.tsx 顶栏 goAgentWorkspace 的完整错误处理
            onClick: async () => {
              try {
                const res = await fetch('/api/c-end/settings/mode', {
                  method: 'PATCH',
                  headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${localStorage.getItem('omnirank_token')}`,
                  },
                  body: JSON.stringify({ preferred_mode: 'agent' }),
                });
                if (res.ok) {
                  const data = await res.json().catch(() => ({}));
                  window.location.href = data.redirect_to || '/';
                } else {
                  const err = await res.json().catch(() => ({}));
                  const detail = err?.detail || err?.message || '切换失败,请稍后重试';
                  toast.error(typeof detail === 'string' ? detail : '切换失败');
                  console.error('[切到工作台] PATCH 失败', res.status, err);
                }
              } catch (e) {
                toast.error('网络错误,请稍后重试');
                console.error('[切到工作台] exception', e);
              }
            },
          }],
    }] : []),
    {
      // v3.6: 推广有礼 + 合作伙伴计划合并为"推广"独立组（老板："推广有礼放在合作伙伴计划上面"）
      title: '推广',
      items: [
        { label: '推广有礼', icon: Gift, href: '/referral', hint: referralHint },
        // commit 27 (CTO-15.2 2026-04-19): 老板反馈"已经是代理还显示合作伙伴计划没清干净"
        // 修法: 加 !isAgent 条件,代理用户不再显示申请入口
        ...(partnerFlagEnabled && !isAgent ? [{
          label: '合作伙伴计划',
          icon: Briefcase,
          hint: '身份证实名申请 · AI+人工审核',
          href: '/partner/about',
        }] : []),
      ],
    },
    {
      title: '设置',
      items: [
        // v3.6 修 bug: /settings 需 admin 权限导致普通用户点击无反应；C 端走 /account/profile
        { label: '个人设置', icon: Settings, href: '/account/profile' },
        { label: '功能消耗', icon: CreditCard, href: '/feature-pricing' },
      ],
    },
  ];

  const handleClick = (item: DrawerItem) => {
    if (item.onClick) {
      item.onClick();
    } else if (item.href) {
      if (item.href.startsWith('/c/') || item.href === '/c') {
        navigate(item.href);
      } else if (panel) {
        panel.openInPanel(item.href);
      } else {
        navigate(item.href);
      }
    }
    onNavigate?.();
  };

  return (
    <div className="flex flex-col h-full overflow-auto">
      {/* 顶部品牌卡片（仅 <85% 或无品牌时置顶提醒补全；≥85% 折叠进"我的"菜单） */}
      {showTopCard && <BrandCard brand={brand} score={score} onClick={handleMyBrandClick} />}

      <nav className="flex-1 py-1">
        {sections.map((section) => (
          <div key={section.title} className="mb-2">
            <div className="px-4 py-1 text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
              {section.title}
            </div>
            {section.items.map((item) => {
              const Icon = item.icon;
              const isGold = item.accent === 'gold';
              return (
                <button
                  key={item.label}
                  onClick={() => handleClick(item)}
                  className={cn(
                    'flex w-full items-start gap-3 px-4 py-2.5 text-sm hover:bg-accent/50 transition-colors text-left',
                    isGold && 'bg-amber-500/5 hover:bg-amber-500/10',
                  )}
                >
                  <Icon className={cn(
                    'h-4 w-4 shrink-0 mt-0.5',
                    isGold ? 'text-amber-400' : 'text-muted-foreground',
                  )} />
                  <div className="flex-1 min-w-0">
                    <div className={cn('truncate', isGold && 'font-medium text-amber-300')}>
                      {item.label}
                    </div>
                    {item.hint && (
                      <div className="text-[11px] text-muted-foreground mt-0.5 leading-tight">
                        {item.hint}
                      </div>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="border-t p-2">
        <button
          onClick={() => { logout(); window.location.href = '/login'; }}
          className="flex w-full items-center gap-3 px-4 py-2.5 text-sm hover:bg-accent/50 transition-colors text-left text-muted-foreground"
        >
          <LogOut className="h-4 w-4 shrink-0" />
          <span>退出登录</span>
        </button>
      </div>

      {/* CTO-15.2 commit 19: 托管套餐 Dialog (老板拍方案 C - 不离开 /c/chat) */}
      <ManagedSheet open={managedSheetOpen} onOpenChange={setManagedSheetOpen} />
    </div>
  );
}
