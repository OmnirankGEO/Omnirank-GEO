/**
 * ClientSwitcherSidebar · sidebar 顶部全局客户切换器
 *
 * Phase 07 (CTO-15.23 2026-05-04) · 老板拍板"全部聚合 · 一个地方切"
 *
 * 设计:对标 Linear / Slack workspace switcher
 *   - 位置:AppSidebar 顶部 logo 下方
 *   - Expanded:全宽按钮 [👤 客户名 ▼]
 *   - Collapsed:只显示首字 avatar(60x60)
 *   - 点击展开 popover(right-side)· 搜索 + 状态筛选 + 客户列表 + + 新建客户
 *   - 选客户 → switchClient → 全站联动
 *
 * 与现有 旧客户上下文条(/components/layout/旧客户上下文条.tsx)区别:
 *   - 那个是 horizontal bar at top of page · 此为 vertical 适配 sidebar
 *   - 那个会随 Phase 07 部署被 Layout 移除 · 此组件取代之
 *   - 复用 useClientContext / useTestClientFilter / useBrandsTestMap 同样 hooks
 */

import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, Check, Plus, Users } from 'lucide-react';
import {
  SearchableRoot,
  SearchableSearchBox,
  SearchableItems,
  useSearchableList,
  defaultMatch,
} from '@/components/ui/searchable-select';
import { useClientContext, type BrandSummary } from '@/context/ClientContext';
import { useAuth } from '@/context/AuthContext';
import { useTestClientFilter, TestClientFilterToggle } from '@/components/workbench/TestClientFilterToggle';
import { useBrandsTestMap } from '@/hooks/useBrandsTestMap';
import { useSidebar } from '@/components/ui/sidebar';
import { cn } from '@/lib/utils';
import type { CustomerIntakeData, SubmitMode } from '@/components/customer-intake';

const CustomerIntakeDialog = lazy(() =>
  import('@/components/customer-intake').then((module) => ({ default: module.CustomerIntakeDialog })),
);
import { authFetch } from '@/lib/api';
import { emitBrandUpdated } from '@/lib/brandProfileEvents';
import { lazyToast } from '@/lib/lazyToast';
import { CLIENT_STATUS_OPTIONS, getClientStatusMeta, normalizeClientStatus } from '@/lib/clientStatus';

/**
 * 尾栏「共 N 个客户 · 匹配 M」
 * 搜索词与过滤结果现在归 SearchableRoot 持有，所以这一小段要在 Root 内部读 context。
 * 文案与改前逐字一致（只有搜索词非空且匹配数≠总数时才追加"· 匹配 M"）。
 */
function MatchCountFooter({ total }: { total: number }) {
  const { query, filtered } = useSearchableList();
  return (
    <div className="px-3 py-1.5 text-center text-[10px] text-muted-foreground">
      共 {total} 个客户 {query && filtered.length !== total ? `· 匹配 ${filtered.length}` : ''}
    </div>
  );
}

function getInitial(name: string | undefined | null): string {
  if (!name || !name.trim()) return '·';
  const trimmed = name.trim();
  return trimmed.charAt(0).toUpperCase();
}

export function ClientSwitcherSidebar() {
  const {
    clients,
    currentBrandId,
    clientContext,
    listLoading,
    isAllClientsMode,
    switchClient,
  } = useClientContext();
  const { user } = useAuth();
  const isAdmin = user?.is_admin === true;

  // sidebar collapsed/expanded state
  const { state: sidebarState, setOpen: setSidebarOpen, isMobile, setOpenMobile } = useSidebar();
  const collapsed = sidebarState === 'collapsed';

  // popover state
  //
  // 🔴 这里不再有 search state。改前它散在 6 处各自 setSearch('')
  //    (:77 外部点击 / :132 选中 / :183 collapsed 打开 / :191 trigger 切换 / :270 Esc / :276 清空按钮)，
  //    现在搜索词归 SearchableRoot 持有，"关闭即清空"由它内部唯一一处 effect 结构性保证：
  //    本文件所有关闭路径只需 setOpen(false)，将来加第 5 条关闭路径也不可能漏清空。
  const [open, setOpen] = useState(false);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // 测试客户隔离
  const [includeTest, setIncludeTest] = useTestClientFilter();
  const { isTest, testCount } = useBrandsTestMap();

  // 点击外部关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  // 打开聚焦搜索 → 已由 SearchableSearchBox 负责（同一套机制,不再各自 focus）

  // 活跃客户(排除已归档)
  const activeClients = useMemo(
    () => clients.filter((c) => normalizeClientStatus(c.brand_status) !== 'archived'),
    [clients],
  );

  // 状态筛选 + 测试客户开关（原逻辑一字不动）· 搜索过滤交给 SearchableRoot,
  // 两者共存关系不变：这里先按状态/测试收窄,再由搜索词在其结果上过滤。
  const statusFiltered = useMemo(() => {
    let result = statusFilter === 'archived'
      ? clients.filter((c) => normalizeClientStatus(c.brand_status) === 'archived')
      : activeClients;
    if (statusFilter && statusFilter !== 'archived') {
      result = result.filter((c) => normalizeClientStatus(c.brand_status) === statusFilter);
    }
    if (!includeTest) {
      result = result.filter((c) => !isTest(c.id));
    }
    return result;
  }, [clients, activeClients, statusFilter, includeTest, isTest]);

  const statusCounts = useMemo(() => {
    const counts = { active: 0, undecided: 0, won: 0, archived: 0 };
    clients.forEach((c) => {
      counts[normalizeClientStatus(c.brand_status)]++;
    });
    return counts;
  }, [clients]);

  const handleSelect = (brandId: number | null, allMode = false) => {
    if (allMode) {
      switchClient(null, true);
    } else {
      switchClient(brandId);
    }
    setOpen(false); // 清空由 SearchableRoot 的关闭 effect 统一处理
  };

  const currentBrand = clientContext?.brand;
  const selectedClient = clients.find((c) => c.id === currentBrandId);

  const displayText = isAllClientsMode
    ? '全部客户'
    : selectedClient
      ? selectedClient.name
      : '选择客户';

  const displayInitial = isAllClientsMode
    ? <Users className="h-4 w-4" />
    : <span className="text-[13px] font-semibold">{getInitial(selectedClient?.name)}</span>;

  const handleCreate = async (data: CustomerIntakeData, _mode: SubmitMode, _existingBrandId?: number) => {
    // 调 /api/my-clients 创建 brand · 然后切到新客户
    const res = await authFetch('/api/my-clients', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: data.name,
        industry: data.industry || '',
        city: data.city || '',
        business: data.business || '',
        seed_keywords: data.seed_keywords || [],
      }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json?.success) {
      throw new Error(json?.detail || json?.error || '创建客户失败');
    }
    const brandId = json.brand_id ?? json.id ?? json.client?.id;
    if (brandId) {
      emitBrandUpdated(brandId, 'sidebar-quick-create');
      switchClient(brandId);
      lazyToast.success(`已创建并切换到「${data.name}」`);
    }
  };

  return (
    <>
      <div ref={containerRef} className="relative">
        {/* 触发按钮 · sidebar collapsed/expanded 自适应 */}
        <button
          onClick={() => {
            // Phase 07.1 · collapsed 时自动展开 sidebar 再开 popover(避免 popover 飞出 sidebar 边界)
            if (collapsed && !isMobile) {
              setSidebarOpen(true);
              setTimeout(() => setOpen(true), 80); // 等 sidebar 展开动画
              return;
            }
            // mobile 下 sidebar 是 sheet · 展开点击直接弹 popover
            if (collapsed && isMobile) {
              setOpenMobile(true);
            }
            setOpen(!open);
          }}
          disabled={listLoading}
          className={cn(
            'flex items-center gap-2 w-full rounded-lg border text-sm transition-colors duration-200',
            'group-data-[collapsible=icon]:size-8 group-data-[collapsible=icon]:p-0 group-data-[collapsible=icon]:justify-center',
            'px-2.5 py-2',
            open
              ? 'border-brand ring-1 ring-brand/30 bg-secondary'
              : 'border-border bg-secondary/50 hover:bg-secondary',
            selectedClient || isAllClientsMode ? 'text-foreground' : 'text-muted-foreground',
          )}
          title={collapsed ? displayText : undefined}
          aria-label="切换客户"
        >
          {/* Avatar · always visible */}
          <span
            className={cn(
              'shrink-0 rounded-md flex items-center justify-center',
              'h-7 w-7',
              isAllClientsMode
                ? 'bg-purple-500/15 text-purple-500'
                : selectedClient
                  ? 'bg-brand/15 text-brand'
                  : 'bg-muted text-muted-foreground',
            )}
          >
            {displayInitial}
          </span>
          {/* 客户名 + chevron · collapsed 时隐藏 */}
          <span className="flex-1 min-w-0 text-left group-data-[collapsible=icon]:hidden">
            <span className="block text-[10px] uppercase tracking-wider text-muted-foreground/70 leading-tight">
              当前客户
            </span>
            <span className={cn('block text-[13px] font-medium break-words leading-snug', !selectedClient && !isAllClientsMode && 'text-muted-foreground')} title={displayText}>
              {displayText}
            </span>
            {selectedClient?.access_mode === 'demo' && (
              <span className="mt-0.5 inline-flex rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700 dark:text-amber-300">
                演示案例
              </span>
            )}
          </span>
          <ChevronDown
            className={cn(
              'h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform duration-200',
              'group-data-[collapsible=icon]:hidden',
              open && 'rotate-180',
            )}
          />
        </button>

        {/* Popover · 自适应 sidebar 状态 · 不超模 */}
        {/* expanded: top-full 下方弹出 · 占满 sidebar 宽度(sidebar 内护栏) */}
        {/* collapsed: left-full 右侧弹出 · 280px(因为 sidebar 太窄装不下搜索/列表) */}
        {open && (
          <div
            className={cn(
              'absolute z-50 bg-background border border-border rounded-lg shadow-xl flex flex-col overflow-hidden',
              collapsed
                ? 'top-0 left-full ml-2 w-[280px]'
                : 'top-full mt-1 left-0 right-0',
              'max-h-[70vh]',
            )}
          >
            {/* 🔴 搜索段换成共用件（本单唯一改动面）· 业务段(状态筛选/测试开关/全部客户/新建客户)原地不动 */}
            <SearchableRoot
              items={statusFiltered}
              open={open}
              onOpenChange={(next) => setOpen(next)}
              value={selectedClient ?? null}
              onValueChange={(c) => { if (c) handleSelect(c.id); }}
              filter={(c, q) => defaultMatch([c.name, c.industry, c.brand_code], q)}
              itemToLabel={(c) => c.name}
              isItemEqualToValue={(a, b) => a?.id === b?.id}
              // 显式 true:改前无论几个客户都有搜索框,靠阈值自动判定会让客户数 ≤8 的
              // 服务商突然没搜索框 = 回归。锁5 要的是"行为与改前一致"。
              searchable
              // 面板是本组件自己的绝对定位 div,不是 base-ui Popup。
              // 不传 inline 会让点状态 pill / 测试开关被当作 outsidePress 关掉面板(实测已复现)。
              inline
            >
              <SearchableSearchBox searchPlaceholder="搜索品牌名 / 行业 / 代码" />

            {/* 状态筛选 */}
            <div className="flex gap-1 px-2 py-1.5 border-b border-border/50 flex-wrap items-center">
              {[
                { key: null, label: '日常', count: activeClients.length },
                ...CLIENT_STATUS_OPTIONS.filter((item) => item.value !== 'archived').map((item) => ({
                  key: item.value,
                  label: item.shortLabel,
                  count: statusCounts[item.value],
                })),
                ...(statusCounts.archived > 0 ? [{ key: 'archived', label: '归档', count: statusCounts.archived }] : []),
              ].map((opt) => (
                <button
                  key={opt.key || 'all'}
                  onClick={() => setStatusFilter(opt.key as string | null)}
                  className={cn(
                    'px-2 py-0.5 rounded-full text-[11px] font-medium transition-colors',
                    statusFilter === opt.key
                      ? 'bg-foreground/10 text-foreground'
                      : 'text-muted-foreground hover:bg-secondary',
                  )}
                >
                  {opt.label} {opt.count}
                </button>
              ))}
              <TestClientFilterToggle
                value={includeTest}
                onChange={setIncludeTest}
                hiddenCount={testCount}
                className="ml-auto"
              />
            </div>

            {/* 客户列表(scroll)*/}
            <div className="flex-1 min-h-0 overflow-y-auto">
              {/* 全部客户(管理员可选)*/}
              {isAdmin && (
                <button
                  onClick={() => handleSelect(null, true)}
                  className={cn(
                    'w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors',
                    isAllClientsMode ? 'bg-purple-500/10' : 'hover:bg-secondary',
                  )}
                >
                  <span className="w-7 h-7 rounded-md bg-purple-500/15 text-purple-500 flex items-center justify-center shrink-0">
                    <Users className="h-3.5 w-3.5" />
                  </span>
                  <span className="flex-1 min-w-0">
                    <span className={cn('block text-[13px]', isAllClientsMode ? 'font-semibold text-purple-600 dark:text-purple-400' : 'text-foreground')}>
                      全部客户(管理员模式)
                    </span>
                    <span className="block text-[11px] text-muted-foreground">
                      {clients.length} 个客户 · 全数据视图
                    </span>
                  </span>
                  {isAllClientsMode && <Check className="h-4 w-4 text-purple-500 shrink-0" />}
                </button>
              )}

              {/* 客户列表 · 行 JSX 与改前逐字一致,只是外层从 <button onClick> 换成
                  Combobox.Item(点击/Enter 都经 onValueChange → handleSelect,顺带白送键盘导航) */}
              <SearchableItems<BrandSummary>
                itemKey={(c) => c.id}
                isSelected={(c) => c.id === currentBrandId && !isAllClientsMode}
                emptyText="没有匹配的客户"
                emptyNoDataText="没有客户"
                listClassName="overflow-visible"
                renderItem={(c, { selected: isActive }) => {
                  const initial = getInitial(c.name);
                  const primaryIndustry = c.industry ? c.industry.split('/')[0].split('、')[0].trim() : '';
                  const statusMeta = getClientStatusMeta(c.brand_status);
                  return (
                    <span
                      className={cn(
                        'w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors',
                        isActive ? 'bg-brand/10' : 'hover:bg-secondary',
                      )}
                    >
                      <span
                        className={cn(
                          'w-7 h-7 rounded-md flex items-center justify-center text-[12px] font-semibold shrink-0',
                          isActive ? 'bg-brand text-white' : 'bg-muted text-muted-foreground',
                        )}
                      >
                        {initial}
                      </span>
                      <span className="flex-1 min-w-0">
                        <span className={cn('block text-[13px] break-words leading-snug', isActive ? 'font-semibold text-foreground' : 'font-medium text-foreground')} title={c.name}>
                          {c.name}
                        </span>
                        {primaryIndustry && (
                          <span className="block text-[11px] text-muted-foreground break-words leading-snug" title={primaryIndustry}>
                            {primaryIndustry}
                          </span>
                        )}
                        <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
                          <span className={cn('rounded border px-1.5 py-0.5', statusMeta.badgeClass)}>
                            {statusMeta.shortLabel}
                          </span>
                          {c.access_mode === 'demo' && (
                            <span className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 font-semibold text-amber-700 dark:text-amber-300">
                              演示案例
                            </span>
                          )}
                          {isAdmin && (c.owner_account || c.owner_name || c.owner_user_id) && (
                            <span className="break-words leading-snug" title={`归属: ${c.owner_account || c.owner_name || `账号#${c.owner_user_id}`}`}>
                              {c.owner_account || c.owner_name || `账号#${c.owner_user_id}`}
                            </span>
                          )}
                        </span>
                      </span>
                      {isActive && <Check className="h-4 w-4 text-brand shrink-0" />}
                    </span>
                  );
                }}
              />
            </div>

            {/* 底部 + 新建客户 */}
            <div className="border-t border-border/50">
              <button
                onClick={() => {
                  setOpen(false);
                  setShowCreateDialog(true);
                }}
                className="w-full flex items-center gap-2 px-3 py-2.5 text-[13px] text-foreground hover:bg-secondary transition-colors"
              >
                <span className="w-7 h-7 rounded-md bg-emerald-500/15 text-emerald-500 flex items-center justify-center shrink-0">
                  <Plus className="h-4 w-4" />
                </span>
                <span className="flex-1 text-left">+ 新建客户</span>
              </button>
              <MatchCountFooter total={clients.length} />
            </div>
            </SearchableRoot>
          </div>
        )}
      </div>

      {/* + 新建客户 Dialog · 仅在用户打开时下载完整资料表单 */}
      {showCreateDialog && (
        <Suspense fallback={null}>
          <CustomerIntakeDialog
            open
            onOpenChange={setShowCreateDialog}
            blocks={['basic']}
            title="新建客户"
            description="填客户基础信息 · 创建后自动切换"
            submitText="创建并切换"
            onSubmit={handleCreate}
            keywordsRequired={false}
            complexFields={false}
            showAiFill={true}
          />
        </Suspense>
      )}
    </>
  );
}

export default ClientSwitcherSidebar;
