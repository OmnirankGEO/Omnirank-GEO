/**
 * V3.5 W2 · 代理白标定价中心
 *
 * 路由: /agent/pricing
 * 责任: 服务商管理独立的对客零售算力包。
 * 边界: 服务商可编辑算力与零售价，成本必须由后端 canonical calculator 只读计算。
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { agentApi, formatCents } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { useAuth } from '@/context/AuthContext';
import ClientPurchaseGatePanel from '@/components/agent/ClientPurchaseGatePanel';
import { cn } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger } from '@/components/ui/dialog';
import { Tag, Save, RefreshCw, Calculator, Gift, Info, Lock, Package, TrendingUp, Coins, Boxes, Plus, Trash2, Settings2 } from 'lucide-react';
import { lazyToast } from '@/lib/lazyToast';
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { safeRandomUUID } from '@/lib/safeRandomUUID';

interface SKU {
  retail_sku_id: string;
  version: number;
  sku_template_id?: number | null;
  source_template_id?: number | null;
  override_id: number;
  sort_order?: number;
  scene?: string;               // 适合场景
  sku_key: string;
  category: string;
  display_name: string;
  subtitle?: string;
  extra_promo_text?: string;
  points_granted: number;
  wholesale_cents: number;
  retail_cents: number;
  is_active: boolean;
  margin_label?: string;
  margin_warning?: string;
}

// [2026-06-06 1:N 白标包] 售价/利润率校验阈值 · 低利润率主动让利需二次确认
const LOW_MARGIN_PCT = 10;  // 利润率 < 10% 视为偏低 · 弹确认

// [2026-06-06 卖算力库存口径] 主售卖 = 算力包(按算力大小分档)· 场景功能包(scenario_pack)已下架,
// 不再作为主销售分类(后端 migration_v36_sku_credit_inventory_rename 下架 · 前端不再列出)
const CATEGORY_LABELS: Record<string, string> = {
  credit_pack: '算力包',
  addon_pack: '按需加购',
};

// 卡片场景说明(营销口径:大概几条搜索项 + 建议怎么卖 + 适合什么客户 · 仅参考,不限制实际用途)
const SCENE_HINT_BY_KEY: Record<string, string> = {
  credit_basic: '大概够 1 条搜索项试跑一轮 · 建议卖给想先看效果的新客户 · 低门槛试水',
  credit_growth: '大概够 3 条搜索项 · 建议卖给要验证获客方向的小客户 · 启动期首选',
  credit_pro: '大概够 5-6 条搜索项 · 建议卖给本地品牌做首月经营 · 一个月跑出效果',
  credit_team: '大概够 10-15 条搜索项 · 建议卖给多区域 / 成熟客户打包长期经营',
};
const sceneHint = (sku: SKU): string =>
  SCENE_HINT_BY_KEY[sku.sku_key] ||
  (sku.category === 'addon_pack' ? '临时补充算力 · 不够时随时补一点,任意功能都能用' : '');

const packDisplayName = (sku: SKU): string => sku.display_name;

// [2026-05-30 计价人话化] margin_label(英文枚举)→ 人话 + 徽章样式 + 金额色
// 替代裸露的 high/healthy/allowed · 并修"徽章恒红"bug(原 margin_warning 非空恒 truthy)
const MARGIN_LABEL: Record<string, { text: string; variant: 'default' | 'secondary' | 'destructive' | 'outline'; tone: string }> = {
  loss_heavy:     { text: '亏本 · 不可发布', variant: 'destructive', tone: 'text-destructive' },
  loss_medium:    { text: '亏本 · 不可发布', variant: 'destructive', tone: 'text-destructive' },
  loss_light:     { text: '微亏 · 不可发布', variant: 'outline', tone: 'text-orange-500' },
  zero:           { text: '零利润 · 不可发布', variant: 'destructive', tone: 'text-destructive' },
  healthy:        { text: '利润正常', variant: 'secondary', tone: 'text-emerald-600' },
  profit_good:    { text: '利润优秀', variant: 'secondary', tone: 'text-emerald-600' },
  profit_excellent: { text: '利润优秀', variant: 'secondary', tone: 'text-emerald-600' },
  high:           { text: '利润较高', variant: 'secondary', tone: 'text-emerald-600' },
  excess_warning: { text: '利润偏高', variant: 'outline', tone: 'text-orange-500' },
  excess_over:    { text: '利润过高', variant: 'destructive', tone: 'text-orange-500' },
  excess_blocked: { text: '利润过高 · 请确认', variant: 'destructive', tone: 'text-destructive' },
  margin_anomaly_blocked: { text: '利润异常 · 已拦截', variant: 'destructive', tone: 'text-destructive' },
  invalid_factory: { text: '成本异常 · 已拦截', variant: 'destructive', tone: 'text-destructive' },
};
const marginMeta = (label?: string) =>
  (label && MARGIN_LABEL[label]) || { text: '', variant: 'secondary' as const, tone: 'text-foreground' };

interface RetailEconomics {
  points_granted: number;
  retail_cents: number;
  estimated_cost_cents: number;
  estimated_profit_cents: number;
  margin_label: string;
  margin_action: string;
  is_loss: boolean;
  publishable: boolean;
}

function parsePositivePoints(raw: string): number | null {
  const value = raw.trim();
  if (!/^[1-9]\d*$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed <= 9_000_000_000_000_000 ? parsed : null;
}

// Exact decimal-string → integer cents conversion. No float participates in money input.
function parseYuanToCents(raw: string): number | null {
  const value = raw.trim();
  const match = /^(0|[1-9]\d*)(?:\.(\d{1,2}))?$/.exec(value);
  if (!match) return null;
  const yuan = Number(match[1]);
  const fraction = Number((match[2] || '').padEnd(2, '0'));
  if (!Number.isSafeInteger(yuan)) return null;
  const cents = yuan * 100 + fraction;
  return Number.isSafeInteger(cents) && cents > 0 && cents <= 2_000_000_000 ? cents : null;
}

function centsToYuanInput(cents: number): string {
  const yuan = Math.trunc(cents / 100);
  const fraction = String(cents % 100).padStart(2, '0');
  return `${yuan}.${fraction}`;
}

function useRetailEconomics(pointsInput: string, retailInput: string, enabled = true) {
  const [economics, setEconomics] = useState<RetailEconomics | null>(null);
  const [economicsLoading, setEconomicsLoading] = useState(false);
  useEffect(() => {
    const points = parsePositivePoints(pointsInput);
    const retailCents = parseYuanToCents(retailInput);
    if (!enabled || points == null || retailCents == null) {
      setEconomics(null);
      setEconomicsLoading(false);
      return;
    }
    let cancelled = false;
    // Never let a prior input's economics remain actionable during debounce/fetch.
    setEconomics(null);
    setEconomicsLoading(true);
    const timer = window.setTimeout(() => {
      agentApi.previewSKU({ points_granted: points, retail_cents: retailCents })
        .then((response) => { if (!cancelled) setEconomics(response.data); })
        .catch(() => { if (!cancelled) setEconomics(null); })
        .finally(() => { if (!cancelled) setEconomicsLoading(false); });
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [enabled, pointsInput, retailInput]);
  return { economics, economicsLoading };
}

export default function PricingCenter() {
  const { user, authorizationScope } = useAuth();
  const identityKey = user ? authorizationScope : null;
  const activeIdentityRef = useRef<string | null>(identityKey);
  const [items, setItems] = useState<SKU[]>([]);
  const [loading, setLoading] = useState(true);
  const [readErrors, setReadErrors] = useState<Record<string, string>>({});
  const [loadedResources, setLoadedResources] = useState<Record<string, boolean>>({});
  const [ratio, setRatio] = useState('');
  const [applying, setApplying] = useState(false);
  const [costPerArticle, setCostPerArticle] = useState('');  // [M2] 服务商自设每篇成本(空=系统估算)
  const [costSaving, setCostSaving] = useState(false);
  const [markupResult, setMarkupResult] = useState<{
    created_count: number; updated_count: number;
    applied_count: number; skipped_count: number;
    skipped: Array<{ sku: string; reason: string }>;
  } | null>(null);
  const [rebateEnabled, setRebateEnabled] = useState(false);
  const [rebateRate, setRebateRate] = useState('');   // 百分比字符串 如 '10'
  const [rebateMax, setRebateMax] = useState('');      // 单笔上限积分(可选)
  const [rebateSaving, setRebateSaving] = useState(false);
  // [2026-06-06] 本月已售(真实经营数据)· 复用 finance/overview · 无数据降级单包服务收益区间
  const [overview, setOverview] = useState<{ pnl?: { gmv?: number; orders?: number } } | null>(null);
  // [2026-06-06 1:N 白标包] 新增算力包弹窗
  const [createOpen, setCreateOpen] = useState(false);

  useLayoutEffect(() => {
    activeIdentityRef.current = identityKey;
    setItems([]);
    setRatio('');
    setCostPerArticle('');
    setRebateEnabled(false);
    setRebateRate('');
    setRebateMax('');
    setOverview(null);
    setReadErrors({});
    setLoadedResources({});
    setLoading(identityKey !== null);
  }, [identityKey]);

  const markReadError = (resource: string, error: unknown, fallback: string) => {
    setReadErrors((prev) => ({
      ...prev,
      [resource]: formatApiErrorForDisplay(error, fallback, 'agent'),
    }));
  };

  const markReadSuccess = (resource: string) => {
    setReadErrors((prev) => {
      if (!(resource in prev)) return prev;
      const next = { ...prev };
      delete next[resource];
      return next;
    });
    setLoadedResources((prev) => ({ ...prev, [resource]: true }));
  };

  const reload = async (signal?: AbortSignal, forceRefresh = false) => {
    const requestIdentity = identityKey;
    if (requestIdentity === null) return;
    setLoading(items.length === 0);
    try {
      const r = await agentApi.pricingSKUs(signal, forceRefresh);
      if (signal?.aborted || activeIdentityRef.current !== requestIdentity) return;
      // [2026-06-06] 前端兜底:即使后端迁移未跑,也不展示已下架的场景功能包
      setItems((r.items || []).filter((s: SKU) => s.category !== 'scenario_pack'));
      markReadSuccess('skus');
    } catch (e: any) {
      if (signal?.aborted || activeIdentityRef.current !== requestIdentity) return;
      markReadError('skus', e, '算力包读取失败 · 已保留上次成功数据');
      lazyToast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'agent'));
    } finally {
      if (!signal?.aborted && activeIdentityRef.current === requestIdentity) setLoading(false);
    }
  };

  const loadRatio = async (signal?: AbortSignal, forceRefresh = false) => {
    const requestIdentity = identityKey;
    try {
      const r = await agentApi.getMarkupRatio(signal, forceRefresh);
      if (signal?.aborted || activeIdentityRef.current !== requestIdentity) return;
      if (r.ratio != null) setRatio(String(r.ratio));
      markReadSuccess('ratio');
    } catch (e) {
      if (!signal?.aborted && activeIdentityRef.current === requestIdentity) {
        markReadError('ratio', e, '加价系数读取失败');
      }
    }
  };

  const handleApply = async () => {
    const v = parseFloat(ratio);
    if (isNaN(v) || v <= 0) { lazyToast.error('系数须为正数'); return; }
    setApplying(true);
    try {
      const r = await agentApi.applyMarkup(parseFloat(ratio));
      setMarkupResult({
        created_count: r.created_count ?? 0, updated_count: r.updated_count ?? 0,
        applied_count: r.applied_count, skipped_count: r.skipped_count, skipped: r.skipped,
      });
      // 批量加价只更新已有包；没有可更新包时不得显示绿色成功。
      const parts: string[] = [];
      if (r.updated_count) parts.push(`更新 ${r.updated_count} 个`);
      if (r.skipped_count) parts.push(`跳过 ${r.skipped_count} 个`);
      const msg = parts.length ? parts.join(' · ') : '无可定价的算力包';
      if (r.applied_count > 0) {
        lazyToast.success(msg);
      } else {
        lazyToast.error(`未影响任何算力包(${msg})· 请先在下方新增零售算力包`);
      }
      void reload(undefined, true);
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '应用失败 · 请重试', 'agent'));
    } finally {
      setApplying(false);
    }
  };

  // [M2 2026-06-07] 每篇内容成本(服务商自设·空=系统估算)
  const loadCost = async (signal?: AbortSignal, forceRefresh = false) => {
    const requestIdentity = identityKey;
    try {
      const r = await agentApi.getCostPerArticle(signal, forceRefresh);
      if (signal?.aborted || activeIdentityRef.current !== requestIdentity) return;
      if (r.cost_per_article != null) setCostPerArticle(String(r.cost_per_article));
      markReadSuccess('cost');
    } catch (e) {
      if (!signal?.aborted && activeIdentityRef.current === requestIdentity) {
        markReadError('cost', e, '内容成本读取失败');
      }
    }
  };

  const handleSaveCost = async () => {
    const raw = costPerArticle.trim();
    const v = raw === '' ? null : parseFloat(raw);
    if (v !== null && (isNaN(v) || v <= 0)) { lazyToast.error('每篇成本要填正数,或留空用系统估算'); return; }
    setCostSaving(true);
    try {
      await agentApi.setCostPerArticle(v);
      lazyToast.success(v === null ? '已清空 · 改用系统自动估' : `每篇成本已设为 ¥${v}`);
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'agent'));
    } finally {
      setCostSaving(false);
    }
  };

  const loadRebate = async (signal?: AbortSignal, forceRefresh = false) => {
    const requestIdentity = identityKey;
    try {
      const r = await agentApi.getRebateConfig(signal, forceRefresh);
      if (signal?.aborted || activeIdentityRef.current !== requestIdentity) return;
      setRebateEnabled(!!r.enabled);
      setRebateRate(r.rebate_rate ? String(Math.round(r.rebate_rate * 1000) / 10) : '');
      setRebateMax(r.max_rebate_points_per_order != null ? String(r.max_rebate_points_per_order) : '');
      markReadSuccess('rebate');
    } catch (e) {
      if (!signal?.aborted && activeIdentityRef.current === requestIdentity) {
        markReadError('rebate', e, '返利配置读取失败');
      }
    }
  };

  const handleSaveRebate = async () => {
    const pct = parseFloat(rebateRate);
    if (rebateEnabled && (isNaN(pct) || pct < 0 || pct > 100)) { lazyToast.error('返利比例须在 0 ~ 100% 之间'); return; }
    const maxPts = rebateMax.trim() ? parseInt(rebateMax, 10) : null;
    if (maxPts != null && (isNaN(maxPts) || maxPts < 0)) { lazyToast.error('单笔上限须为非负整数'); return; }
    setRebateSaving(true);
    try {
      await agentApi.setRebateConfig({
        enabled: rebateEnabled,
        rebate_rate: rebateEnabled ? (parseFloat(rebateRate) || 0) / 100 : 0,
        max_rebate_points_per_order: maxPts,
      });
      lazyToast.success('返利设置已保存');
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'agent'));
    } finally {
      setRebateSaving(false);
    }
  };

  const loadOverview = async (signal?: AbortSignal, forceRefresh = false) => {
    const requestIdentity = identityKey;
    try {
      const value = await agentApi.financeOverview(signal, forceRefresh);
      if (signal?.aborted || activeIdentityRef.current !== requestIdentity) return;
      setOverview(value);
      markReadSuccess('overview');
    } catch (e) {
      if (!signal?.aborted && activeIdentityRef.current === requestIdentity) {
        markReadError('overview', e, '经营总览读取失败');
      }
    }
  };

  useEffect(() => {
    if (identityKey === null) return;
    const controller = new AbortController();
    void Promise.allSettled([
      reload(controller.signal),
      loadRatio(controller.signal),
      loadCost(controller.signal),
      loadRebate(controller.signal),
      loadOverview(controller.signal),
    ]);
    return () => controller.abort();
  // The authenticated identity is the lifecycle boundary for all five read resources.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identityKey]);

  const grouped = items.reduce<Record<string, SKU[]>>((acc, s) => {
    (acc[s.category] = acc[s.category] || []).push(s);
    return acc;
  }, {});

  // 纯展示派生量(不改数据流 · 仅汇总现有 items / ratio / overview)
  const ratioNum = parseFloat(ratio);
  // 本月已售(真实订单)· 无可靠数据则降级单包服务收益区间(不造假 0)
  const monthlyOrders = overview?.pnl?.orders ?? 0;
  const monthlyGmvYuan = overview?.pnl?.gmv ?? 0;
  const hasSales = monthlyOrders > 0;
  const marginList = items
    .map((x) => (x.retail_cents || 0) - (x.wholesale_cents || 0))
    .filter((m) => m > 0);
  const minMargin = marginList.length ? Math.min(...marginList) : 0;
  const maxMargin = marginList.length ? Math.max(...marginList) : 0;

  return (
    <div className="container mx-auto max-w-7xl space-y-6 py-6 lg:py-8">
      {/* 顶部标题区 */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted">
            <Tag className="h-5 w-5" />
          </div>
          <h1 className="truncate text-xl font-bold sm:text-2xl">客户售价</h1>
          <Badge variant="outline" className="hidden shrink-0 sm:inline-flex">仅经营后台可见</Badge>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button size="sm" onClick={() => setCreateOpen(true)} className="shrink-0">
            <Plus className="mr-1 h-4 w-4" /> 新增算力包
          </Button>
          <Button variant="ghost" size="sm" onClick={() => void reload(undefined, true)} className="shrink-0">
            <RefreshCw className={cn('mr-1 h-4 w-4', loading && 'animate-spin')} /> 刷新
          </Button>
        </div>
      </div>

      {/* [微单 C-6 2026-07-28] 客户线上购买门控 · 唯一入口(自推广中心迁入,置顶):
          给客户定价与决定"能否线上直购"在同一动线。API 与门控判定零改动。 */}
      <ClientPurchaseGatePanel />

      {/* 零售包由服务商直接自定义。 */}
      <CreateSKUDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onDone={() => { setCreateOpen(false); void reload(undefined, true); }}
      />

      {Object.keys(readErrors).length > 0 && (
        <div role="alert" className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-amber-700 dark:text-amber-300">
          部分经营数据读取失败；已成功读取的数据会继续保留，未返回的项目不会按 0 或“未开启”处理。
        </div>
      )}

      {/* 经营汇总条(现有数据派生) */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 sm:gap-4">
        <SummaryTile
          icon={TrendingUp}
          label="算力包加价系数"
          loading={!loadedResources.ratio && !readErrors.ratio}
          accent
          value={!loadedResources.ratio && readErrors.ratio ? '读取失败' : (!isNaN(ratioNum) && ratioNum > 0 ? `${ratioNum.toFixed(2)}x` : '未设置')}
        />
        {hasSales ? (
          <SummaryTile
            icon={Coins}
            label="本月已售"
            loading={loading}
            hint="来自客户购买算力包订单"
            value={formatCents(Math.round(monthlyGmvYuan * 100))}
          />
        ) : (
          <SummaryTile
            icon={Coins}
            label="单包服务收益区间"
            loading={loading}
            hint="按当前加价系数计算"
            value={marginList.length ? `¥${Math.round(minMargin / 100).toLocaleString()} - ¥${Math.round(maxMargin / 100).toLocaleString()}` : '—'}
          />
        )}
        <SummaryTile
          icon={Boxes}
          label="上架算力包"
          loading={!loadedResources.skus && !readErrors.skus}
          hint="服务商自己的零售目录"
          value={!loadedResources.skus && readErrors.skus ? '读取失败' : `${items.filter((x) => x.is_active).length} 个`}
        />
      </div>

      {/* 主体两栏:左 = 卡片列表 · 右 = 定价助手/设置 */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* 左主区 */}
        <div className="min-w-0 space-y-4 lg:col-span-2">
          <PricingNotice />

          <Tabs defaultValue={Object.keys(CATEGORY_LABELS)[0]}>
            <TabsList className="grid w-full grid-cols-2 sm:inline-flex sm:w-auto">
              {Object.entries(CATEGORY_LABELS).map(([k, v]) => (
                <TabsTrigger key={k} value={k} className="gap-1.5">
                  {v}<span className="text-xs text-muted-foreground">{grouped[k]?.length || 0}</span>
                </TabsTrigger>
              ))}
            </TabsList>
            {Object.keys(CATEGORY_LABELS).map((cat) => (
              <TabsContent key={cat} value={cat} className="pt-4">
                {loading && items.length === 0 ? (
                  <div className="grid grid-cols-1 items-start gap-4 sm:grid-cols-2">
                    {[0, 1].map((i) => <SkuSkeleton key={i} />)}
                  </div>
                ) : readErrors.skus && items.length === 0 ? (
                  <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
                    {readErrors.skus} · 这不代表算力包为空。
                  </div>
                ) : grouped[cat]?.length ? (
                  <div className="grid grid-cols-1 items-start gap-4 sm:grid-cols-2">
                    {grouped[cat].map((sku) => (
                      <SKUCard
                        key={sku.retail_sku_id}
                        sku={sku}
                        onSaved={() => void reload(undefined, true)}
                      />
                    ))}
                  </div>
                ) : (
                  <EmptyState />
                )}
              </TabsContent>
            ))}
          </Tabs>
        </div>

        {/* 右辅区:定价助手(全局加价 + 返利设置) */}
        <aside className="space-y-4 self-start lg:col-span-1 lg:sticky lg:top-6">
          <div className="space-y-0.5 px-1">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <Calculator className="h-4 w-4 text-brand" /> 定价助手
            </h2>
            <p className="text-xs text-muted-foreground">全局批量定价与客户返利,统一在此设置</p>
          </div>

          {/* D3 全局加价系数 · 一键定价(仅加价倍数 · 客户毛利率模式已移除) */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Calculator className="h-4 w-4" /> 算力包全局定价
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {!loadedResources.ratio && readErrors.ratio && (
                <p role="alert" className="text-xs text-destructive">{readErrors.ratio} · 当前值未知，已禁止覆盖。</p>
              )}
              {/* [2026-06-06] 全局一键定价 = 加价倍数(进货价 × 系数)· 客户毛利率模式已移除 */}
              <p className="text-xs leading-relaxed text-muted-foreground">
                设一个系数,一键把所有<strong className="text-foreground">算力包</strong>售价定为 <strong className="text-foreground">进货价 × 系数</strong>(如 1.5 = 在进货价上加 50%)· 倍数由你自定,赚多少自己说了算。<br />给客户做<strong className="text-foreground">关键词报价</strong>的系数是另一个,在「报价中心」单独设置,与此互不影响。
              </p>
              <div className="space-y-1.5">
                <Label className="text-xs">加价倍数(进货价 ×)</Label>
                <Input type="number" step="0.1" min="0" value={ratio}
                       onChange={(e) => setRatio(e.target.value)} placeholder="如 1.5 · 自定" />
              </div>

              <Button onClick={handleApply} disabled={applying || (!loadedResources.ratio && !!readErrors.ratio)} className="w-full">
                <Calculator className="mr-1 h-4 w-4" /> {applying ? '应用中…' : '一键应用到所有包'}
              </Button>
              {markupResult && (
                <div className="space-y-1 rounded-lg bg-muted/40 p-2.5 text-sm">
                  {/* 明示更新/跳过；绝不从平台规格自动创建零售包。 */}
                  {markupResult.applied_count > 0 ? (
                    <p>✅ 更新 <strong>{markupResult.updated_count}</strong> 个
                      {markupResult.skipped_count > 0 && <> · ⚠️ 跳过 <strong>{markupResult.skipped_count}</strong> 个</>}
                    </p>
                  ) : (
                    <p className="text-orange-500">⚠️ 未影响任何算力包
                      {markupResult.skipped_count > 0 && <> · 跳过 <strong>{markupResult.skipped_count}</strong> 个</>}
                      · 请先新增零售算力包
                    </p>
                  )}
                  {markupResult.skipped.map((s, i) => (
                    <p key={i} className="text-xs text-orange-500">· {s.sku}：{s.reason}</p>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          {/* [M2 2026-06-07] 每篇内容成本自设 · 王姐口径(投什么档次媒体自己定·防亏本价) */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Calculator className="h-4 w-4" /> 每篇内容成本
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {!loadedResources.cost && readErrors.cost && (
                <p role="alert" className="text-xs text-destructive">{readErrors.cost} · 当前值未知，已禁止覆盖。</p>
              )}
              <p className="text-xs leading-relaxed text-muted-foreground">
                你投什么档次的媒体,每篇成本自己定。<strong className="text-foreground">填了数,报价就按你的真实成本算</strong>,不会报出低于成本的亏本价;留空就用系统按同行自动估。
              </p>
              <div className="space-y-1.5">
                <Label className="text-xs">每篇成本(元 · 留空 = 系统自动估)</Label>
                <Input type="number" step="1" min="0" value={costPerArticle}
                       onChange={(e) => setCostPerArticle(e.target.value)} placeholder="如投央媒填 350 · 留空系统估" />
              </div>
              <Button onClick={handleSaveCost} disabled={costSaving || (!loadedResources.cost && !!readErrors.cost)} variant="outline" className="w-full">
                {costSaving ? '保存中…' : '保存'}
              </Button>
            </CardContent>
          </Card>

          {/* D2-b 客户充值返利 · 从代理库存出 */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Gift className="h-4 w-4" /> 客户充值返利(可选)
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {!loadedResources.rebate && readErrors.rebate && (
                <p role="alert" className="text-xs text-destructive">{readErrors.rebate} · 当前开关未知，未按“关闭”处理。</p>
              )}
              <p className="text-xs leading-relaxed text-muted-foreground">
                给你的客户设充值返利(如"充值送 10%")· 返利算力 <strong className="text-foreground">从你的库存(赠送池)出</strong> · 丰俭由人,关闭或设 0 即不返。库存不足时该笔自动跳过、不影响客户正常充值。
              </p>
              <div className="flex items-center justify-between rounded-lg border border-border px-3 py-2.5">
                <Label className="cursor-pointer">{!loadedResources.rebate && readErrors.rebate ? '状态未知' : (rebateEnabled ? '已开启返利' : '未开启')}</Label>
                <Switch checked={rebateEnabled} disabled={!loadedResources.rebate && !!readErrors.rebate} onCheckedChange={setRebateEnabled} />
              </div>
              {rebateEnabled && (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label className="text-xs">返利比例(%)</Label>
                    <Input type="number" step="1" min="0" max="100" value={rebateRate}
                           onChange={(e) => setRebateRate(e.target.value)} placeholder="如 10" />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs">单笔上限(算力·选填)</Label>
                    <Input type="number" step="1" min="0" value={rebateMax}
                           onChange={(e) => setRebateMax(e.target.value)} placeholder="不填 = 不限" />
                  </div>
                </div>
              )}
              <Button onClick={handleSaveRebate} disabled={rebateSaving || (!loadedResources.rebate && !!readErrors.rebate)} size="sm" className="w-full">
                <Save className="mr-1 h-4 w-4" /> {rebateSaving ? '保存中…' : '保存返利设置'}
              </Button>
            </CardContent>
          </Card>
        </aside>
      </div>
    </div>
  );
}

// 经营汇总小卡(纯展示)
function SummaryTile({ icon: Icon, label, value, loading, accent, hint }: {
  icon: any; label: string; value: string; loading?: boolean; accent?: boolean; hint?: string;
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-card p-3 sm:p-4">
      <div className={cn(
        'flex h-9 w-9 shrink-0 items-center justify-center rounded-lg sm:h-10 sm:w-10',
        accent ? 'bg-brand/10 text-brand' : 'bg-muted text-muted-foreground',
      )}>
        <Icon className="h-4 w-4 sm:h-5 sm:w-5" />
      </div>
      <div className="min-w-0">
        {loading
          ? <div className="h-6 w-12 animate-pulse rounded bg-muted" />
          : <div className="truncate text-lg font-bold leading-none tabular-nums sm:text-xl">{value}</div>}
        <div className="mt-1.5 truncate text-xs text-muted-foreground">{label}</div>
        {hint && <div className="mt-0.5 truncate text-[10px] leading-tight text-muted-foreground/70">{hint}</div>}
      </div>
    </div>
  );
}

// 定价提示条 · [2026-06-06] 强调「卖算力库存」口径(诊断/写作/监测/报告都从这里消耗)
function PricingNotice() {
  return (
    <div className="space-y-2">
      {/* [2026-06-17 营销包装] 「搜索项」显眼说明:让默认套餐看起来像一套完整营销计划 */}
      <div className="rounded-lg border border-brand/30 bg-brand/5 px-3.5 py-3 text-xs leading-relaxed">
        <p className="font-medium text-foreground">什么是「搜索项」?</p>
        <p className="mt-1 text-muted-foreground">
          搜索项不是一个短词, 而是一条真实用户会在 AI 搜索里问的长句, 例如「深圳小户型法式装修公司推荐」「别墅电梯定制多少钱」。一个搜索项通常对应一个真实获客场景。下面每个套餐大概能支持几条搜索项, 见卡片说明 —— 据此判断卖给什么客户、怎么定价。
        </p>
      </div>
      <div className="rounded-lg border border-border bg-muted/30 px-3.5 py-3 text-xs leading-relaxed">
        <p className="font-medium text-foreground">套餐本质是一份算力库存 · 诊断、写作、监测、报告都从这里消耗。</p>
        <p className="mt-1 text-muted-foreground">下方套餐按算力大小分档,用法不限;实际能服务几条搜索项 / 几个客户,取决于关键词难度、文章数量和监测频次,卡片里的条数为大致参考。</p>
      </div>
      <div className="flex flex-col gap-2 rounded-lg border border-border bg-muted/30 px-3.5 py-3 text-xs sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-4">
        <span className="inline-flex items-center gap-1.5 font-medium">
          <Info className="h-3.5 w-3.5 text-brand" /> 定价说明
        </span>
        <span className="inline-flex items-center gap-1.5 text-muted-foreground">
          <Lock className="h-3.5 w-3.5" /> 成本只读 · 算力由你定义
        </span>
        <span className="inline-flex items-center gap-1.5 text-muted-foreground">
          <Package className="h-3.5 w-3.5" /> 可调整算力、包装与客户售价
        </span>
        <span className="text-muted-foreground">你的服务收益 = 客户售价 − 进货价</span>
      </div>
    </div>
  );
}

// 加载骨架
function SkuSkeleton() {
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="h-4 w-2/3 animate-pulse rounded bg-muted" />
      <div className="mt-2 h-3 w-1/3 animate-pulse rounded bg-muted" />
      <div className="mt-5 space-y-2">
        <div className="h-3 w-full animate-pulse rounded bg-muted" />
        <div className="h-3 w-full animate-pulse rounded bg-muted" />
        <div className="h-7 w-1/2 animate-pulse rounded bg-muted" />
      </div>
      <div className="mt-5 h-9 w-full animate-pulse rounded bg-muted" />
    </div>
  );
}

// 空态
function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-14 text-center">
      <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
        <Package className="h-6 w-6 text-muted-foreground/60" />
      </div>
      <p className="text-sm font-medium">该分类下暂无套餐</p>
      <p className="mt-1 text-xs text-muted-foreground">点击“新增算力包”，填写名称、算力和客户售价。</p>
    </div>
  );
}

function SKUCard({ sku, onSaved }: { sku: SKU; onSaved: () => void }) {
  const [open, setOpen] = useState(false);

  // [2026-05-30 计价人话化] 毛利金额/率 + 人话健康度徽章(替代裸英文 high/allowed · 修徽章恒红 bug)
  const meta = marginMeta(sku.margin_label);
  const marginCents = sku.retail_cents - sku.wholesale_cents;
  const pct = sku.wholesale_cents > 0 ? Math.round((marginCents / sku.wholesale_cents) * 100) : 0;
  const hint = sceneHint(sku);
  // canonical 零售包卡:可编辑 / 删除 / 上下架
  return (
    <Card className={cn('flex flex-col transition-colors hover:border-foreground/20', !sku.is_active && 'opacity-60')}>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base leading-snug">{packDisplayName(sku)}</CardTitle>
          {sku.is_active ? (
            <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
              <span className="h-1.5 w-1.5 rounded-full bg-brand" /> 上架
            </span>
          ) : (
            <Badge variant="secondary" className="shrink-0">已下架</Badge>
          )}
        </div>
        {sku.subtitle && <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{sku.subtitle}</p>}
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {/* 适合场景(代理自定 · 优先) 或 默认场景说明 */}
        {(sku.scene || hint) && (
          <p className="rounded-md bg-muted/40 px-2.5 py-1.5 text-xs leading-relaxed text-muted-foreground">
            {sku.scene || hint}
          </p>
        )}
        {/* 只读内核(低优先级 · muted) */}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-muted-foreground">算力库存<span className="ml-1 opacity-70">· 仅服务方可见</span></span>
            <span className="font-mono text-xs tabular-nums text-muted-foreground">{sku.points_granted.toLocaleString()}</span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-muted-foreground">进货价<span className="ml-1 opacity-70">· 仅服务方可见</span></span>
            <span className="font-mono text-xs tabular-nums text-muted-foreground">{formatCents(sku.wholesale_cents)}</span>
          </div>
        </div>

        <div className="border-t border-border" />

        {/* 客户售价(主视觉) */}
        <div className="flex items-end justify-between gap-2">
          <span className="text-xs text-muted-foreground">你的客户售价</span>
          <span className="text-xl font-bold tabular-nums">{formatCents(sku.retail_cents)}</span>
        </div>

        {/* 服务收益 + 健康度徽章 */}
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-muted-foreground">你的服务收益</span>
          <span className={cn('font-mono text-sm font-medium tabular-nums', meta.tone)}>
            {formatCents(marginCents)}
            {sku.wholesale_cents > 0 && <span className="ml-1 text-xs">({pct}%)</span>}
          </span>
        </div>
        {meta.text && <div><Badge variant={meta.variant} className="text-xs">{meta.text}</Badge></div>}

        {/* 操作按钮(底部对齐) */}
        <div className="pt-1">
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button size="sm" variant="outline" className="w-full">
                <Settings2 className="mr-1 h-4 w-4" /> 编辑 / 删除
              </Button>
            </DialogTrigger>
            <EditSKUDialog sku={sku} enabled={open} onDone={() => { setOpen(false); onSaved(); }} />
          </Dialog>
        </div>
      </CardContent>
    </Card>
  );
}

// [2026-06-06 1:N 白标包] 售价校验:< 进货价 禁止保存 · 利润率过低弹确认
// 返回 'block' = 禁止保存(已 toast) · 'confirm-low' = 利润偏低需二次确认 · 'ok' = 通过
function validateRetail(retailCents: number, wholesaleCents: number): 'block' | 'confirm-low' | 'ok' {
  if (isNaN(retailCents) || retailCents <= 0) {
    lazyToast.error('零售价必须大于 0');
    return 'block';
  }
  if (wholesaleCents > 0 && retailCents <= wholesaleCents) {
    lazyToast.error('客户售价必须高于当前进货成本');
    return 'block';
  }
  if (wholesaleCents > 0) {
    const pct = ((retailCents - wholesaleCents) / wholesaleCents) * 100;
    if (pct < LOW_MARGIN_PCT) return 'confirm-low';
  }
  return 'ok';
}

function EditSKUDialog({ sku, enabled, onDone }: { sku: SKU; enabled: boolean; onDone: () => void }) {
  // 初始名严格取 canonical display_name，不改写历史模板型包名称。
  const [displayName, setDisplayName] = useState(packDisplayName(sku));
  const [subtitle, setSubtitle] = useState(sku.subtitle || '');
  const [extraPromo, setExtraPromo] = useState(sku.extra_promo_text || '');
  const [scene, setScene] = useState(sku.scene || '');
  const [points, setPoints] = useState(String(sku.points_granted));
  const [retail, setRetail] = useState(centsToYuanInput(sku.retail_cents));
  const [active, setActive] = useState(sku.is_active);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const { economics, economicsLoading } = useRetailEconomics(points, retail, enabled);

  const submit = async () => {
    const pointsGranted = parsePositivePoints(points);
    const retailCents = parseYuanToCents(retail);
    if (pointsGranted == null) { lazyToast.error('包含算力必须是正整数'); return; }
    if (retailCents == null) { lazyToast.error('客户售价最多保留两位小数'); return; }
    if (!economics || economics.points_granted !== pointsGranted || economics.retail_cents !== retailCents) {
      lazyToast.error('正在核算当前成本，请稍后再试');
      return;
    }
    const v = validateRetail(retailCents, economics.estimated_cost_cents);
    if (v === 'block') return;
    if (v === 'confirm-low' &&
        !(await askConfirm({ title: '这个售价利润很低,请确认是主动让利。' }))) return;
    setBusy(true);
    try {
      await agentApi.saveSKU(sku.override_id, {
        version: sku.version,
        display_name: displayName,
        subtitle,
        extra_promo_text: extraPromo,
        scene,
        points_granted: pointsGranted,
        retail_cents: retailCents,
        is_active: active,
      });
      lazyToast.success('保存成功');
      onDone();
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    const ok = await askConfirm({ title: `确定删除算力包「${packDisplayName(sku)}」?`, description: '删除后客户将无法购买。', danger: true });
    if (!ok) return;
    setDeleting(true);
    try {
      const r = await agentApi.deleteSKU(sku.override_id, sku.version);
      // 一律 tombstone：不复活、不破坏历史订单快照。
      lazyToast.success(r?.action === 'soft_deleted' ? '已删除并保留历史订单追溯' : '已删除');
      onDone();
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '删除失败 · 请重试', 'agent'));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <DialogContent className="sm:max-w-md">
      <DialogHeader>
        <DialogTitle className="text-base">编辑算力包</DialogTitle>
      </DialogHeader>
      <p className="-mt-1 truncate text-sm text-muted-foreground">{packDisplayName(sku)}</p>

      <div className="space-y-4">
        <div className="space-y-1.5"><Label>展示名(对客户)</Label><Input maxLength={120} value={displayName} onChange={(e) => setDisplayName(e.target.value)} /></div>
        <div className="space-y-1.5"><Label>副标题</Label><Input maxLength={240} value={subtitle} onChange={(e) => setSubtitle(e.target.value)} /></div>
        <div className="space-y-1.5"><Label>补充话术 / 卖点</Label><Input maxLength={1000} value={extraPromo} onChange={(e) => setExtraPromo(e.target.value)} /></div>
        <div className="space-y-1.5"><Label>适合场景</Label><Input maxLength={500} value={scene} onChange={(e) => setScene(e.target.value)} placeholder="如 适合新店起步 / 多客户批量运营" /></div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor={`edit-points-${sku.retail_sku_id}`}>包含算力</Label>
            <Input id={`edit-points-${sku.retail_sku_id}`} inputMode="numeric" value={points} onChange={(e) => setPoints(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`edit-price-${sku.retail_sku_id}`}>客户售价(元)</Label>
            <Input id={`edit-price-${sku.retail_sku_id}`} inputMode="decimal" value={retail} onChange={(e) => setRetail(e.target.value)} />
          </div>
        </div>
        <div className="grid grid-cols-3 gap-2 rounded-lg border border-border bg-muted/30 p-3 text-xs" aria-live="polite">
          <div><p className="text-muted-foreground">包含算力</p><p className="mt-1 break-all font-mono">{parsePositivePoints(points)?.toLocaleString() ?? '—'}</p></div>
          <div><p className="text-muted-foreground">当前预计成本</p><p className="mt-1 font-mono">{economicsLoading ? '计算中…' : economics ? formatCents(economics.estimated_cost_cents) : '—'}</p></div>
          <div><p className="text-muted-foreground">预计利润</p><p className={cn('mt-1 font-mono', economics?.is_loss && 'text-destructive')}>{economics ? formatCents(economics.estimated_profit_cents) : '—'}</p></div>
        </div>
        <div className="flex items-center justify-between rounded-lg border border-border px-3 py-2.5">
          <Label className="cursor-pointer">{active ? '上架' : '下架'}</Label>
          <Switch checked={active} onCheckedChange={setActive} />
        </div>
      </div>
      <DialogFooter className="flex-col gap-2 sm:flex-row sm:justify-between">
        <Button onClick={remove} disabled={busy || deleting} variant="ghost"
                className="w-full text-destructive hover:bg-destructive/10 hover:text-destructive sm:w-auto">
          <Trash2 className="mr-1 h-4 w-4" /> {deleting ? '删除中…' : '删除算力包'}
        </Button>
        <Button onClick={submit} disabled={busy || deleting || economicsLoading} className="w-full sm:w-auto">
          <Save className="mr-1 h-4 w-4" /> {busy ? '保存中…' : '保存'}
        </Button>
      </DialogFooter>
      {confirmDialog}
    </DialogContent>
  );
}

// 新增算力包由服务商直接填写，不读取平台进货模板。
function CreateSKUDialog({ open, onOpenChange, onDone }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onDone: () => void;
}) {
  const [displayName, setDisplayName] = useState('');
  const [subtitle, setSubtitle] = useState('');
  const [extraPromo, setExtraPromo] = useState('');
  const [scene, setScene] = useState('');
  const [points, setPoints] = useState('');
  const [retail, setRetail] = useState('');
  const [active, setActive] = useState(true);
  const [busy, setBusy] = useState(false);
  const [clientRequestId, setClientRequestId] = useState(() => safeRandomUUID());

  const [confirmDialog, askConfirm] = useConfirmDialog();
  const { economics, economicsLoading } = useRetailEconomics(points, retail, open);

  // 关闭时重置
  const handleOpenChange = (o: boolean) => {
    if (!o) {
      setDisplayName(''); setSubtitle(''); setExtraPromo('');
      setScene(''); setPoints(''); setRetail(''); setActive(true);
      setClientRequestId(safeRandomUUID());
    }
    onOpenChange(o);
  };

  const submit = async () => {
    if (!displayName.trim()) { lazyToast.error('请填写展示名'); return; }
    const pointsGranted = parsePositivePoints(points);
    const retailCents = parseYuanToCents(retail);
    if (pointsGranted == null) { lazyToast.error('包含算力必须是正整数'); return; }
    if (retailCents == null) { lazyToast.error('客户售价最多保留两位小数'); return; }
    if (!economics || economics.points_granted !== pointsGranted || economics.retail_cents !== retailCents) {
      lazyToast.error('正在核算当前成本，请稍后再试');
      return;
    }
    const v = validateRetail(retailCents, economics.estimated_cost_cents);
    if (v === 'block') return;
    if (v === 'confirm-low' &&
        !(await askConfirm({ title: '这个售价利润很低,请确认是主动让利。' }))) return;
    setBusy(true);
    try {
      await agentApi.createSKU({
        client_request_id: clientRequestId,
        display_name: displayName,
        subtitle,
        extra_promo_text: extraPromo,
        scene,
        points_granted: pointsGranted,
        retail_cents: retailCents,
        is_active: active,
        sort_order: 0,
      });
      lazyToast.success('算力包已创建');
      onDone();
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '创建失败 · 请重试', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="text-base">新增算力包</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5"><Label htmlFor="create-retail-name">算力包名称</Label><Input id="create-retail-name" className="h-11" maxLength={120} value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="例如：新客启动包" /></div>
          <div className="space-y-1.5"><Label htmlFor="create-retail-subtitle">副标题</Label><Input id="create-retail-subtitle" className="h-11" maxLength={240} value={subtitle} onChange={(e) => setSubtitle(e.target.value)} /></div>
          <div className="space-y-1.5"><Label htmlFor="create-retail-pitch">补充话术 / 卖点</Label><Input id="create-retail-pitch" className="h-11" maxLength={1000} value={extraPromo} onChange={(e) => setExtraPromo(e.target.value)} /></div>
          <div className="space-y-1.5"><Label htmlFor="create-retail-scene">适合场景</Label><Input id="create-retail-scene" className="h-11" maxLength={500} value={scene} onChange={(e) => setScene(e.target.value)} placeholder="如 适合新店起步 / 多客户批量运营" /></div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="create-retail-points">包含算力</Label>
              <Input id="create-retail-points" className="h-11" inputMode="numeric" value={points} onChange={(e) => setPoints(e.target.value)} placeholder="例如 12000" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="create-retail-price">客户售价(元)</Label>
              <Input id="create-retail-price" className="h-11" inputMode="decimal" value={retail} onChange={(e) => setRetail(e.target.value)} placeholder="例如 299.00" />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2 rounded-lg border border-border bg-muted/30 p-3 text-xs" aria-live="polite">
            <div><p className="text-muted-foreground">包含算力</p><p className="mt-1 break-all font-mono">{parsePositivePoints(points)?.toLocaleString() ?? '—'}</p></div>
            <div><p className="text-muted-foreground">当前预计成本</p><p className="mt-1 font-mono">{economicsLoading ? '计算中…' : economics ? formatCents(economics.estimated_cost_cents) : '—'}</p></div>
            <div><p className="text-muted-foreground">预计利润</p><p className={cn('mt-1 font-mono', economics?.is_loss && 'text-destructive')}>{economics ? formatCents(economics.estimated_profit_cents) : '—'}</p></div>
          </div>
          <p className="text-xs text-muted-foreground">成本由当前统一计算器估算，下单时会再锁定不可变报价与商业关系快照。</p>
          <div className="flex min-h-11 items-center justify-between rounded-lg border border-border px-3 py-2.5">
            <Label htmlFor="create-retail-active" className="flex min-h-11 flex-1 cursor-pointer items-center">
              {active ? '上架(客户可购买)' : '下架(暂不可购买)'}
            </Label>
            <Switch id="create-retail-active" checked={active} onCheckedChange={setActive} />
          </div>
        </div>

        <DialogFooter>
          <Button onClick={submit} disabled={busy || economicsLoading} className="h-11 w-full sm:w-auto">
            <Plus className="mr-1 h-4 w-4" /> {busy ? '创建中…' : '创建算力包'}
          </Button>
        </DialogFooter>
        {confirmDialog}
      </DialogContent>
    </Dialog>
  );
}
