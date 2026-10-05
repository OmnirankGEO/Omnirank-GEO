/**
 * 我的客户列表页
 *
 * CTO-15.21 v5 · 收口(2026-04-28 老板拍板):
 *   旧版客户卡升级 4 元素 · 用 m3Api.listClients 一次拿够 stage/风险/完整度/is_test
 *   - StagePill(询价/诊断/待报价/报价中/写作/投放/监测/报告/续费)
 *   - CompletenessBadge(0-100 SSOT)
 *   - RiskDot(stalled/renewal/ready/warming/ok)
 *   - "查看客户 →"按钮(进入客户详情 · /my-clients/:brandId 旧版工作面)
 *   - 顶部 toggle "隐藏测试客户(默认 ON)" · is_test=false 真客户优先
 *   - URL ?add=1 自动打开"添加客户"弹窗(给 M3 快捷操作条 联动)
 */
import { useState, useEffect, useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { Plus, Search, Users, ChevronRight, Trash2, Sparkles, AlertTriangle, ArrowRight, Star } from 'lucide-react';
import { authApi, useAuth } from '@/context/AuthContext';
import { useClientContext } from '@/context/ClientContext';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';
import { apiErrorText } from '@/lib/api';   // [F-2] 安全取文案:detail 可能是 §13 合同对象
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import { BatchUpgradeDialog } from './BatchUpgradeDialog';
import { INDUSTRY_CATEGORIES } from '@/lib/industries';
import { StagePill, CompletenessBadge, RiskDot } from '@/components/workbench/parts';
import { m3Api, type SalesClientListItem, type LifecycleStage, type RiskLevel } from '@/services/m3';
import { useOnBrandUpdated } from '@/lib/brandProfileEvents';
import { useMarkStepCompleted } from '@/hooks/useMarkStepCompleted';
import { FeatureTooltip } from '@/components/onboarding/FeatureTooltip';
import { isSandboxActive } from '@/sandbox/sandboxState';
import { SANDBOX_DEMO_PRESET } from '@/sandbox/mockData';
import { useOrganization } from '@/context/OrganizationContext';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import {
  CLIENT_STATUS_OPTIONS,
  type ClientStatus,
  getClientStatusMeta,
  normalizeClientStatus,
} from '@/lib/clientStatus';

interface ClientItem {
  id: number;
  name: string;
  industry?: string;
  cities?: string;
  latest_score?: number;
  diagnosis_count?: number;
  has_profile?: boolean;
  updated_at?: string;
  owner_name?: string;
  owner_account?: string | null;
  owner_user_id?: number | null;
  client_status?: ClientStatus;
  brand_status?: ClientStatus;
  status_label?: string;
  brand_type?: string;
  /** A2 · 客户端计算 · /api/brands/{id}/completeness */
  completeness?: number;
  /** C.3 (CTO-15.20):桥接 4 元素来自 m3Api.listClients */
  stage?: LifecycleStage;
  risk?: RiskLevel;
  is_test?: boolean;
  /** [客户反馈③ 2026-08-09] 星标置顶 · 后端已按它排好序,前端只回显与切换 */
  is_starred?: boolean;
  starred_at?: string | null;
}

const TEST_FILTER_KEY = 'omnirank_hide_test_clients';

export default function MyClientsPage() {
  const navigate = useEmbeddedNavigate();
  // 原生 confirm() 在部分 WebKit 会话里静默返回 false → 归档永远发不出 DELETE(工单 §1)
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const { user } = useAuth();
  const { isMember } = useOrganization();
  const isAdmin = user?.is_admin === true;
  const { switchClient, refreshClients } = useClientContext();
  const markStep = useMarkStepCompleted();
  const [clients, setClients] = useState<ClientItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [showAll, setShowAll] = useState(false);
  const [statusFilter, setStatusFilter] = useState<ClientStatus | 'daily'>('daily');
  const [statusDialog, setStatusDialog] = useState<{ client: ClientItem; nextStatus: ClientStatus } | null>(null);
  const [statusChanging, setStatusChanging] = useState(false);
  const [archiveConfirmText, setArchiveConfirmText] = useState('');
  // [客户反馈③ 2026-08-09] 星标切换在飞标记(防连点两下发两次相反的请求)
  const [starringId, setStarringId] = useState<number | null>(null);

  // C.3 (CTO-15.20):隐藏测试客户 toggle · localStorage 记忆 · 默认 ON
  const [hideTest, setHideTest] = useState<boolean>(() => {
    try {
      const raw = localStorage.getItem(TEST_FILTER_KEY);
      return raw === null ? true : raw === '1';
    } catch {
      return true;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(TEST_FILTER_KEY, hideTest ? '1' : '0');
    } catch {
      /* ignore */
    }
  }, [hideTest]);

  // 添加客户弹窗
  const [showAdd, setShowAdd] = useState(false);
  const [addForm, setAddForm] = useState({ name: '', industry: '', city: '' });
  const [addCustomIndustryMode, setAddCustomIndustryMode] = useState(false);
  const [adding, setAdding] = useState(false);

  // Stage 1 Batch 3 (2026-05-08) · 沙盒态打开弹窗时自动填好预设客户
  // 让用户不用思考填什么, 直接点"添加"就行 · 跟"教程不打断"原则一致
  useEffect(() => {
    if (showAdd && isSandboxActive() && !addForm.name) {
      setAddForm({
        name: SANDBOX_DEMO_PRESET.brandName,
        industry: SANDBOX_DEMO_PRESET.industry,
        city: SANDBOX_DEMO_PRESET.city,
      });
    }
  }, [showAdd, addForm.name]);

  // CTO-15.21 v5 收口:URL ?add=1 自动打开弹窗(M3 快捷操作条"录新客户"联动)
  // 打开后立即清掉 query · 防刷新重复弹
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    if (searchParams.get('add') === '1') {
      setShowAdd(true);
      const next = new URLSearchParams(searchParams);
      next.delete('add');
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  // 搜索防抖：用户停止输入 300ms 后才发请求
  const [debouncedSearch, setDebouncedSearch] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  // A2 · 批量升级 Dialog
  const [showBatchUpgrade, setShowBatchUpgrade] = useState(false);

  const fetchClients = useCallback(async () => {
    setLoading(true);
    try {
      // 老 endpoint 拉基础信息(name / industry / cities / has_profile / updated_at)
      const params: Record<string, unknown> = {};
      // [CTO-15.23 2026-05-20] admin 搜索时强制 show_all · 跟左上角 ClientSwitcher 行为一致
      // 老板报:左上角能搜到"雅栖" · 我的客户搜不到 · 根因 my-clients 默认只返 owner 自己的客户
      // admin 搜索时自动 show_all=true 看全部代理客户(toggle state 不动 · 清搜索框恢复)
      const _isAdminSearching = isAdmin && debouncedSearch.trim().length > 0;
      if (showAll || _isAdminSearching) params.show_all = true;
      if (debouncedSearch.trim()) params.search = debouncedSearch.trim();
      // [WJ-03 2026-05-31] 总拿全部(含测试客户)· 前端 hideTest 过滤;否则后端默认 include_test=False 排掉测试客户 → 建完测试客户列表显 0
      params.include_test = isAdmin;  // [BUG6 2026-06-05] 仅 admin 拿测试客户·非 admin 后端强制排除(防绕过)
      const res = await authApi.get('/api/my-clients', { params });
      const baseList: ClientItem[] = res.data.clients || [];
      setTotal(res.data.total || 0);

      // C.3 (CTO-15.20):用 m3Api.listClients 一次拿 stage / risk / completeness / is_test
      let m3Map: Record<number, SalesClientListItem> = {};
      try {
        const m3List = await m3Api.listClients({ includeTest: isAdmin });
        m3Map = m3List.reduce<Record<number, SalesClientListItem>>((acc, c) => {
          acc[c.id] = c;
          return acc;
        }, {});
      } catch {
        // m3 BFF 失败时降级 · 老逻辑只显示基础信息
      }

      const enriched: ClientItem[] = baseList.map((c) => {
        const m = m3Map[c.id];
        const clientStatus = normalizeClientStatus(c.client_status || c.brand_status || (c as any).status);
        return {
          ...c,
          client_status: clientStatus,
          brand_status: clientStatus,
          status_label: getClientStatusMeta(clientStatus).label,
          stage: m?.stage,
          risk: m?.risk,
          is_test: m?.is_test,
          completeness: m?.completeness?.score ?? c.completeness ?? 0,
        };
      });
      setClients(enriched);
    } catch {
      /* silent */
    }
    setLoading(false);
  }, [showAll, debouncedSearch, isAdmin]);

  useEffect(() => { fetchClients(); }, [fetchClients]);

  // T8 (Phase 06 · CTO-15.23 2026-05-03) · 监听跨入口 brand 更新事件 · 不 F5 自动刷
  // 写作大厅快速写作 / M3 新增客户页(Phase 2)/ ClientMaterialsEditor(Phase 2)等创建/改客户后 emit
  useOnBrandUpdated(() => {
    fetchClients();
  });

  // C.3 (CTO-15.20):测试客户隔离 · hideTest=true 时过滤 is_test=true
  const filtered = useMemo(() => {
    // [BUG6 2026-06-05] 非 admin 始终过滤测试客户(不暴露内部测试概念)· admin 走 hideTest 开关
    let result = !isAdmin || hideTest
      ? clients.filter((c) => c.is_test !== true)
      : clients;
    if (statusFilter === 'daily') {
      result = result.filter((c) => normalizeClientStatus(c.client_status || c.brand_status) !== 'archived');
    } else {
      result = result.filter((c) => normalizeClientStatus(c.client_status || c.brand_status) === statusFilter);
    }
    return result;
  }, [clients, hideTest, isAdmin, statusFilter]);
  const statusCounts = useMemo(() => {
    const base = !isAdmin || hideTest
      ? clients.filter((c) => c.is_test !== true)
      : clients;
    const counts: Record<ClientStatus | 'daily', number> = {
      daily: 0,
      active: 0,
      undecided: 0,
      won: 0,
      archived: 0,
    };
    base.forEach((client) => {
      const normalized = normalizeClientStatus(client.client_status || client.brand_status);
      counts[normalized] += 1;
      if (normalized !== 'archived') counts.daily += 1;
    });
    return counts;
  }, [clients, hideTest, isAdmin]);
  const testHiddenCount = isAdmin && hideTest ? clients.filter((c) => c.is_test === true).length : 0;

  const handleAdd = async () => {
    if (!addForm.name.trim()) { toast.error('请输入客户名称'); return; }
    setAdding(true);
    try {
      const res = await authApi.post('/api/my-clients', addForm);
      if (res.data.success) {
        toast.success('客户添加成功');
        markStep('enroll_client');
        setShowAdd(false);
        setAddForm({ name: '', industry: '', city: '' });
        setAddCustomIndustryMode(false);
        await fetchClients();
        await refreshClients(); // 同步顶部客户选择栏
        // 延迟跳转确保状态已更新
        const brandId = res.data.brand_id;
        setTimeout(() => navigate(`/my-clients/${brandId}`), 100);
      } else {
        // [F-2] success=false 过去**没有任何反馈**(用户体验上就是"点了没用")。
        // 现在必须给可见反馈,并能渲染 §13 合同对象形态的 detail。
        toast.error(apiErrorText(res, '添加失败,请稍后重试'));
      }
    } catch (e: any) {
      // [F-2] detail 可能是 §13 合同对象(如 UPGRADE_REQUIRED 结构化 payload),
      // 直接丢给 toast 渲染不出来 → 统一走安全取文案。
      toast.error(apiErrorText(e, '添加失败'));
    }
    setAdding(false);
  };

  const handleSwitchTo = (client: ClientItem) => {
    switchClient(client.id);
    toast.success(`已切换到 ${client.name}`);
    navigate('/');
  };

  const handleDelete = async (client: ClientItem) => {
    try {
      const { data: preview } = await authApi.get(`/api/my-clients/${client.id}/archive-preview`);
      const counts = preview.associations;
      const ok = await askConfirm({
        title: `归档客户「${preview.object.display_name}」(#${preview.object.brand_id})？`,
        description: `关联 ${counts.profiles} 个档案、${counts.diagnoses} 条诊断、${counts.quotes} 份报价、${counts.articles} 篇文章。公开链接与员工分配会失效；业务数据保留，7 天内可恢复。`,
        confirmLabel: '归档',
        danger: true,
      });
      if (!ok) return;
      await authApi.delete(`/api/my-clients/${client.id}`, { params: { reason: '客户列表用户归档' } });
      toast.success('客户已归档 · 7 天内可从回收站恢复');
      await fetchClients();
      refreshClients();
    } catch (error: any) {
      const detail = error?.response?.data?.detail;
      toast.error(typeof detail === 'object' ? detail.message : (detail || '归档失败'));
    }
  };

  // [客户反馈③ 2026-08-09] 星标切换。
  //   🔴 成功后必须 fetchClients() 重拉:排序是**后端**做的(星标恒最前),
  //     只改本地 is_starred 会出现"星星亮了但位置没动" —— 用户会当成没生效。
  const toggleStar = async (client: ClientItem) => {
    if (starringId !== null) return;
    const next = !(client.is_starred === true);
    setStarringId(client.id);
    try {
      await authApi.patch(`/api/my-clients/${client.id}/star`, { starred: next });
      toast.success(next ? '已置顶' : '已取消置顶');
      await fetchClients();
    } catch {
      toast.error(next ? '置顶失败' : '取消置顶失败');
    } finally {
      setStarringId(null);
    }
  };

  const requestStatusChange = (client: ClientItem, nextStatus: ClientStatus) => {
    const current = normalizeClientStatus(client.client_status || client.brand_status);
    if (current === nextStatus) return;
    setArchiveConfirmText('');
    setStatusDialog({ client, nextStatus });
  };

  const submitStatusChange = async () => {
    if (!statusDialog) return;
    const { client, nextStatus } = statusDialog;
    if (nextStatus === 'archived' && archiveConfirmText.trim() !== client.name) {
      toast.error('请输入完整客户名称后再归档');
      return;
    }
    setStatusChanging(true);
    try {
      const res = await authApi.patch(`/api/brands/${client.id}/status`, { status: nextStatus });
      const statusValue = normalizeClientStatus(res.data?.new_status || nextStatus);
      setClients((prev) => prev.map((item) => (
        item.id === client.id
          ? {
              ...item,
              client_status: statusValue,
              brand_status: statusValue,
              status_label: getClientStatusMeta(statusValue).label,
            }
          : item
      )));
      toast.success(`已设为${getClientStatusMeta(statusValue).label}`);
      setStatusDialog(null);
      setArchiveConfirmText('');
      await refreshClients();
    } catch (e: any) {
      toast.error(e.response?.data?.detail || '状态更新失败');
    } finally {
      setStatusChanging(false);
    }
  };

  return (
    <div className="mx-auto max-w-5xl p-3 sm:p-4 md:px-6 md:py-5">
      {/* 头部 */}
      <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold text-foreground tracking-tight">
            {showAll ? '全部客户' : '我的客户'}
          </h1>
          <p className="text-xs text-muted-foreground mt-0.5 tracking-wide">
            {total} 个客户
            {isAdmin && testHiddenCount > 0 && (
              <span className="ml-2 text-muted-foreground/60">· 已隐藏 {testHiddenCount} 个测试</span>
            )}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          {/* C.3 + E.2 · [BUG6 2026-06-05] 隐藏测试客户 toggle 仅 admin 可见(测试客户=平台内部概念) */}
          {isAdmin && (
          <label className="inline-flex items-center gap-2 text-xs text-muted-foreground cursor-pointer">
            <Switch
              checked={hideTest}
              onCheckedChange={setHideTest}
              aria-label="隐藏测试客户"
            />
            <span>隐藏测试客户</span>
          </label>
          )}
          {isAdmin && (
            <Button size="sm" variant={showAll ? "default" : "outline"} className="h-8 text-xs"
              onClick={() => setShowAll(v => !v)}>
              <Users className="h-3.5 w-3.5 mr-1" />{showAll ? '仅看我的' : '查看全部'}
            </Button>
          )}
          <Button size="sm" className="min-h-[40px] text-xs sm:h-8" onClick={() => setShowAdd(true)}>
            <Plus className="h-3.5 w-3.5 mr-1" />添加客户
          </Button>
        </div>
      </div>

      {/* 搜索 */}
      <div className="relative mb-2">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
        <Input placeholder="搜索客户名称或行业..." value={search} onChange={e => setSearch(e.target.value)} className="pl-8 h-9 text-xs" />
      </div>
      {/* [CTO-15.23 2026-05-20] admin 搜索时自动包含全部代理客户(跟左上角 ClientSwitcher 一致)· UX 提示让用户知情 */}
      {isAdmin && !showAll && debouncedSearch.trim().length > 0 && (
        <div className="mb-3 text-[11px] text-muted-foreground px-2">
          搜索范围 · 全部客户(含其他服务方名下) · 清空搜索框恢复"仅看我的"
        </div>
      )}

      {/* 客户状态筛选: 旧版成交/未确定/归档能力恢复,并让归档不再挤占日常选择列表 */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {[
          { value: 'daily' as const, label: '日常客户', count: statusCounts.daily },
          ...CLIENT_STATUS_OPTIONS.map((item) => ({ value: item.value, label: item.label, count: statusCounts[item.value] })),
        ].map((item) => {
          const active = statusFilter === item.value;
          return (
            <button
              key={item.value}
              type="button"
              onClick={() => setStatusFilter(item.value)}
              className={cn(
                "rounded-full border px-3 py-1 text-[11px] font-medium transition-colors",
                active
                  ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-200"
                  : "border-border/60 bg-card/60 text-muted-foreground hover:text-foreground"
              )}
            >
              {item.label} {item.count}
            </button>
          );
        })}
      </div>

      {/* 🔴 [包三 · 我的客户] 五个筛选看起来是并列的五档,其实不是:
          「日常客户」= 除已归档外的**全部**,与「跟进中/未确定/已成交」是包含关系,
          所以那几个数字加起来不等于第一个 —— 她会以为哪里算错了。
          这里把**当前这一档的含义**直接说出来;文案取自 clientStatus.ts 的
          description(SSOT),不在这儿另写一份,免得两处漂开。 */}
      <p className="mb-4 -mt-2 px-1 text-[11px] text-muted-foreground" data-testid="client-filter-meaning">
        {statusFilter === 'daily'
          ? '「日常客户」= 除已归档外的全部客户;右边几档是它里面的细分,所以数字不是相加关系。'
          : getClientStatusMeta(statusFilter).description}
      </p>

      {/* A2 · 批量升级 banner(CTO-15.9 M1c §Epic 3) */}
      {(() => {
        const lowCompletenessCandidates = clients
          .filter(c => c.completeness !== undefined && c.completeness < 80)
          .map(c => ({
            id: c.id,
            name: c.name,
            industry: c.industry,
            completeness: c.completeness ?? 0,
          }));
        if (lowCompletenessCandidates.length === 0) return null;
        return (
          <div className="mb-4 flex flex-col gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 sm:flex-row sm:items-center">
            <AlertTriangle className="h-4 w-4 text-amber-600 shrink-0" />
            <div className="flex-1 text-xs">
              <div className="font-medium text-amber-700 dark:text-amber-300">
                {lowCompletenessCandidates.length} 个客户资料 &lt; 80 分 · 影响诊断 / 拓词 / 报告质量
              </div>
              <div className="text-amber-700/80 dark:text-amber-300/80 mt-0.5">
                让 AI 一次补齐 · 每个约 30 秒 · 补不上的会自动跳过,不影响其他客户
              </div>
            </div>
            <Button
              size="sm"
              className="min-h-[40px] w-full shrink-0 bg-amber-500 text-xs text-white hover:bg-amber-600 sm:w-auto sm:h-8"
              onClick={() => setShowBatchUpgrade(true)}
            >
              <Sparkles className="h-3.5 w-3.5 mr-1" />批量升级
            </Button>
          </div>
        );
      })()}

      {/* A2 · 批量升级 Dialog */}
      <BatchUpgradeDialog
        open={showBatchUpgrade}
        onOpenChange={setShowBatchUpgrade}
        candidates={clients
          .filter(c => c.completeness !== undefined && c.completeness < 80)
          .map(c => ({
            id: c.id,
            name: c.name,
            industry: c.industry,
            completeness: c.completeness ?? 0,
          }))}
        onAllDone={() => {
          fetchClients();
          refreshClients();
        }}
      />

      {/* 列表 */}
      {loading ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground text-sm">加载中...</div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16">
          <Users className="h-12 w-12 text-muted-foreground/20 mb-3" />
          {/* 🔴 [包三 · 我的客户] 空态分两种,以前只有「无匹配结果」一句、**没有任何可点的东西**:
              她盯着一个空列表,不知道是没这个客户、还是被筛选挡住了。
              这里说清最可能的原因,并把能解开它的那几个动作直接放出来。 */}
          <p className="text-sm text-muted-foreground mb-2">
            {total === 0 ? '还没有客户' : '这里没有符合条件的客户'}
          </p>
          {total === 0 ? (
            <Button size="sm" onClick={() => setShowAdd(true)}>
              <Plus className="h-3.5 w-3.5 mr-1" />添加第一个客户
            </Button>
          ) : (
            <div className="flex flex-col items-center gap-2" data-testid="clients-empty-exits">
              <p className="text-xs text-muted-foreground/80">
                {search.trim()
                  ? '可能是搜索词对不上,也可能这个客户在别的分类里。'
                  : '当前这一档下没有客户,换一档看看。'}
              </p>
              <div className="flex flex-wrap items-center justify-center gap-2">
                {search.trim() && (
                  <Button size="sm" variant="outline" onClick={() => setSearch('')}>清空搜索</Button>
                )}
                {statusFilter !== 'daily' && (
                  <Button size="sm" variant="outline" onClick={() => setStatusFilter('daily')}>看全部日常客户</Button>
                )}
                {statusFilter !== 'archived' && (
                  <Button size="sm" variant="ghost" onClick={() => setStatusFilter('archived')}>到已归档里找</Button>
                )}
                <Button size="sm" onClick={() => setShowAdd(true)}>
                  <Plus className="h-3.5 w-3.5 mr-1" />新建这个客户
                </Button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border/60 bg-card/50">
          {/* 表头(仅桌面 · 与每行用同一 grid 模板,保证列对齐) */}
          <div className="hidden gap-x-3 border-b border-border/40 bg-muted/20 px-4 py-2.5 text-[11px] font-medium text-muted-foreground sm:grid sm:grid-cols-[minmax(0,2fr)_minmax(0,1.4fr)_120px_56px_108px_auto] sm:items-center">
            <span>客户</span>
            <span>行业 / 地区</span>
            <span>阶段 / 状态</span>
            <span className="text-right">GEO</span>
            <span>资料完整度</span>
            <span className="text-right">操作</span>
          </div>
          <div className="space-y-3 p-3 sm:space-y-0 sm:p-0">
            {filtered.map((c, i) => (
              <div key={c.id}
                onClick={() => navigate(`/my-clients/${c.id}`)}
                className={cn(
                  "cursor-pointer rounded-xl border border-border/60 bg-card/50 p-3 transition-colors hover:bg-muted/40",
                  "sm:grid sm:grid-cols-[minmax(0,2fr)_minmax(0,1.4fr)_120px_56px_108px_auto] sm:items-center sm:gap-x-3 sm:rounded-none sm:border-0 sm:bg-transparent sm:px-4 sm:py-3",
                  i > 0 && "sm:border-t sm:border-border/30"
                )}>
                {(() => {
                  const statusMeta = getClientStatusMeta(c.client_status || c.brand_status);
                  const ownerText = c.owner_account || c.owner_name || (c.owner_user_id ? `服务商账号 #${c.owner_user_id}` : '');
                  return (
                    <>
                {/* 列1 · 客户(星标+图标+名称+风险点+测试标) */}
                <div className="flex min-w-0 items-center gap-3">
                  {/* [客户反馈③ 2026-08-09] 星标切换。后端排序里星标恒在最前,
                      所以点完必须重拉列表(fetchClients),否则位置不动 = 用户以为没生效。 */}
                  <button
                    type="button"
                    data-testid={`client-star-${c.id}`}
                    aria-pressed={c.is_starred === true}
                    aria-label={c.is_starred ? '取消置顶' : '置顶该客户'}
                    title={c.is_starred ? '取消置顶' : '置顶该客户'}
                    disabled={starringId === c.id}
                    onClick={(e) => { e.stopPropagation(); void toggleStar(c); }}
                    className={cn(
                      "shrink-0 rounded p-1 transition-colors hover:bg-muted disabled:opacity-50",
                      c.is_starred ? "text-amber-500" : "text-muted-foreground/40 hover:text-amber-500"
                    )}
                  >
                    <Star className={cn("h-4 w-4", c.is_starred && "fill-current")} />
                  </button>
                  <div className="h-10 w-10 rounded-lg bg-gradient-to-br from-sky-600/60 to-cyan-700/60 flex items-center justify-center text-sm font-semibold text-white/90 shrink-0">
                    {c.name[0]}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                      <span className="text-sm font-medium text-foreground break-words leading-snug sm:text-[13px]" title={c.name}>{c.name}</span>
                      {c.risk && <RiskDot risk={c.risk} />}
                      {isAdmin && c.is_test && (
                        <span className="shrink-0 rounded bg-muted px-1 py-0.5 text-[9px] tracking-wider text-muted-foreground">
                          测试
                        </span>
                      )}
                    </div>
                    {/* 移动端把行业/地区显示在名称下方;桌面端有独立列 */}
                    <div className="mt-0.5 text-[11px] text-muted-foreground break-words leading-snug sm:hidden" title={[c.industry, c.cities].filter(Boolean).join(' · ')}>
                      {[c.industry, c.cities].filter(Boolean).join(' · ') || '—'}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <span className={cn("rounded border px-1.5 py-0.5 text-[10px] font-medium", statusMeta.badgeClass)}>
                        {statusMeta.shortLabel}
                      </span>
                      {isAdmin && ownerText && (
                        <span className="text-[10px] text-purple-300/80 break-words leading-snug" title={`归属: ${ownerText}`}>
                          归属: {ownerText}
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                {/* 列2 · 行业/地区(桌面,line-clamp-2 max 2 行) */}
                <div className="hidden min-w-0 sm:block">
                  <div className="text-[12px] text-muted-foreground line-clamp-2 break-words leading-snug" title={c.industry || ''}>{c.industry || '—'}</div>
                  {c.cities && <div className="text-[11px] text-muted-foreground/60 line-clamp-2 break-words leading-snug" title={c.cities}>{c.cities}</div>}
                </div>

                {/* 列3 · 阶段(固定列,各行对齐) */}
                <div className="mt-2 flex flex-col items-start gap-1.5 sm:mt-0">
                  {c.stage !== undefined
                    ? <StagePill stage={c.stage} />
                    : <span className="text-[11px] text-muted-foreground/40">—</span>}
                  <select
                    value={statusMeta.value}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => {
                      e.stopPropagation();
                      requestStatusChange(c, e.target.value as ClientStatus);
                    }}
                    className="h-7 max-w-[112px] rounded-md border border-border/70 bg-background px-2 text-[11px] text-foreground outline-none focus:border-emerald-500/60"
                    title="设置客户状态"
                  >
                    {CLIENT_STATUS_OPTIONS.map((item) => (
                      <option key={item.value} value={item.value}>{item.label}</option>
                    ))}
                  </select>
                </div>

                {/* 列4 · GEO 分(桌面,右对齐) */}
                <div className="hidden text-right text-[12px] tabular-nums text-muted-foreground sm:block">
                  {c.latest_score != null && c.latest_score > 0
                    ? c.latest_score
                    : <span className="text-muted-foreground/40">—</span>}
                </div>

                {/* 列5 · 资料完整度 */}
                <div className="mt-2 flex min-w-0 items-center gap-1.5 sm:mt-0">
                  {typeof c.completeness === 'number' && <CompletenessBadge score={c.completeness} />}
                </div>

                {/* 列6 · 操作 */}
                <div className="mt-3 flex items-center justify-end gap-2 sm:mt-0">
                  {/* CTO-15.21 v5 收口:进入客户详情(旧版工作面 · /my-clients/:brandId) */}
                  <Button
                    size="sm"
                    variant="default"
                    className="min-h-[40px] flex-1 gap-1 px-3 text-xs sm:h-7 sm:min-h-0 sm:flex-none sm:px-2.5 sm:text-[11px]"
                    onClick={(e) => {
                      e.stopPropagation();
                      if (typeof window !== 'undefined') {
                        const w = window as unknown as { __m3Analytics?: { track?: (e: string, p?: unknown) => void } };
                        w.__m3Analytics?.track?.('myclients_open_detail', { brand_id: c.id });
                      }
                      navigate(`/my-clients/${c.id}`);
                    }}
                    aria-label="查看客户详情"
                  >
                    打开客户
                    <ArrowRight className="h-3 w-3" aria-hidden />
                  </Button>
                  <Button size="sm" variant="ghost" className="hidden h-7 px-2 text-[10px] text-muted-foreground sm:inline-flex"
                    onClick={(e) => { e.stopPropagation(); handleSwitchTo(c); }}>
                    切换
                  </Button>
                  {!isMember && (
                    <Button size="sm" variant="ghost" className="hidden h-7 w-7 p-0 text-muted-foreground/40 hover:text-destructive sm:inline-flex"
                      onClick={(e) => { e.stopPropagation(); handleDelete(c); }}>
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  )}
                  <ChevronRight className="hidden h-3.5 w-3.5 text-muted-foreground/40 sm:block" />
                </div>
                    </>
                  );
                })()}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 添加客户弹窗 */}
      <Dialog open={showAdd} onOpenChange={setShowAdd}>
        <DialogContent>
          <DialogHeader><DialogTitle>添加客户</DialogTitle></DialogHeader>
          <div className="flex flex-col gap-4">
            <div className="space-y-1.5">
              <label className="text-[11px] font-medium text-muted-foreground tracking-wide uppercase">客户名称 *</label>
              <Input value={addForm.name} onChange={e => setAddForm(p => ({ ...p, name: e.target.value }))} placeholder="品牌名或公司名" />
            </div>
            <div className="space-y-1.5">
              <label className="text-[11px] font-medium text-muted-foreground tracking-wide uppercase">行业</label>
              {/* [CTO-15.23 2026-05-06 P3 修复] 行业改 select + custom fallback · 防 free-text 失去归类
                  老板 E2E:行业 free-text 影响 L1/L2 兜底匹配。INDUSTRY_CATEGORIES 抽到 lib/industries.ts SSOT */}
              <select
                value={
                  addCustomIndustryMode
                    ? '__custom__'
                    : (INDUSTRY_CATEGORIES.some(c => c.label === addForm.industry) ? addForm.industry : (addForm.industry ? '__custom__' : ''))
                }
                onChange={e => {
                  const v = e.target.value;
                  if (v === '__custom__') {
                    setAddCustomIndustryMode(true);
                    setAddForm(p => ({ ...p, industry: '' }));
                    return;
                  }
                  setAddCustomIndustryMode(false);
                  setAddForm(p => ({ ...p, industry: v }));
                }}
                className="w-full h-9 text-sm border border-border rounded-md px-2 bg-card text-foreground"
              >
                <option value="">请选择行业</option>
                {INDUSTRY_CATEGORIES.map(c => (
                  <option key={c.label} value={c.label}>{c.label}</option>
                ))}
                <option value="__custom__">其他(自定义)</option>
              </select>
              {(addCustomIndustryMode || (addForm.industry !== '' && !INDUSTRY_CATEGORIES.some(c => c.label === addForm.industry))) && (
                <Input value={addForm.industry} onChange={e => setAddForm(p => ({ ...p, industry: e.target.value }))} placeholder="如:美容护肤、餐饮、制造业" className="mt-1" autoFocus />
              )}
            </div>
            <div className="space-y-1.5">
              <label className="text-[11px] font-medium text-muted-foreground tracking-wide uppercase">城市</label>
              <Input value={addForm.city} onChange={e => setAddForm(p => ({ ...p, city: e.target.value }))} placeholder="如：杭州、深圳" />
            </div>
            {/* 🆕 BUG-P1-1 (CTO-15.22 2026-05-03): 提示 AI 填全套在下一页 · 减少 dialog 阶段焦虑 */}
            <div className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-700 dark:text-amber-400">
              <span className="text-base leading-none">💡</span>
              <span>填客户名即可创建 · 下一页可一键 <strong>AI 联网填全套</strong>(行业/城市/产品/痛点/竞品 8 字段)</span>
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setShowAdd(false)}>取消</Button>
            <Button onClick={handleAdd} disabled={adding}>{adding ? '添加中...' : '添加'}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 客户状态确认 · 尤其归档要防止运营误归错账号/客户 */}
      <Dialog open={!!statusDialog} onOpenChange={(open) => {
        if (!open && !statusChanging) {
          setStatusDialog(null);
          setArchiveConfirmText('');
        }
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {statusDialog?.nextStatus === 'archived' ? '确认归档客户' : '确认修改客户状态'}
            </DialogTitle>
          </DialogHeader>
          {statusDialog && (() => {
            const currentMeta = getClientStatusMeta(statusDialog.client.client_status || statusDialog.client.brand_status);
            const nextMeta = getClientStatusMeta(statusDialog.nextStatus);
            const ownerText = statusDialog.client.owner_account || statusDialog.client.owner_name || (statusDialog.client.owner_user_id ? `服务商账号 #${statusDialog.client.owner_user_id}` : '');
            const needsExactName = statusDialog.nextStatus === 'archived';
            return (
              <div className="space-y-4">
                <div className={cn(
                  "rounded-lg border p-3 text-sm",
                  needsExactName ? "border-destructive/30 bg-destructive/10" : "border-border bg-muted/30"
                )}>
                  <div className="font-medium text-foreground">{statusDialog.client.name}</div>
                  {isAdmin && ownerText && (
                    <div className="mt-1 text-xs text-muted-foreground">
                      归属账号: <span className="text-purple-300">{ownerText}</span>
                    </div>
                  )}
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                    <span className={cn("rounded border px-2 py-1", currentMeta.badgeClass)}>{currentMeta.label}</span>
                    <span className="text-muted-foreground">→</span>
                    <span className={cn("rounded border px-2 py-1", nextMeta.badgeClass)}>{nextMeta.label}</span>
                  </div>
                </div>
                {needsExactName ? (
                  <div className="space-y-2">
                    <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
                      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                      <span>
                        归档后, 该客户会从日常客户选择里移走, 但不会删除数据。为避免归错客户, 请输入完整客户名称确认。
                      </span>
                    </div>
                    <Input
                      value={archiveConfirmText}
                      onChange={(e) => setArchiveConfirmText(e.target.value)}
                      placeholder={`输入「${statusDialog.client.name}」确认归档`}
                    />
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    修改后会同步到左上角客户选择器和"我的客户"列表。
                  </p>
                )}
              </div>
            );
          })()}
          <DialogFooter>
            <Button variant="ghost" onClick={() => {
              if (!statusChanging) {
                setStatusDialog(null);
                setArchiveConfirmText('');
              }
            }}>取消</Button>
            <Button
              onClick={submitStatusChange}
              disabled={
                statusChanging ||
                (statusDialog?.nextStatus === 'archived' && archiveConfirmText.trim() !== statusDialog.client.name)
              }
              variant={statusDialog?.nextStatus === 'archived' ? 'destructive' : 'default'}
            >
              {statusChanging ? '保存中...' : statusDialog?.nextStatus === 'archived' ? '确认归档' : '确认修改'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {confirmDialog}
    </div>
  );
}
