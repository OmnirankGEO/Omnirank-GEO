/**
 * 算力定价中心 (admin-only · 2026-06-07)
 *
 * 路由: /admin/pricing-center
 * 合并自:算力包管理(PricingMgmt) + 定价系数配置(PricingConfig)
 *
 * 责任:统一设置服务商拿货规则、客户建议售价、可售算力包(新增/复制/上下架/重算)
 * 资金铁律:仅操作 sku_templates / pricing_config · 绝不回写 recharge_orders(历史订单按下单价锁定)
 */
import { useEffect, useState, useCallback } from 'react';
import { adminApi, formatCents } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import {
  Sheet, SheetContent, SheetHeader, SheetTitle, SheetFooter,
} from '@/components/ui/sheet';
import {
  RefreshCw, Plus, Search, Save, RotateCcw, Copy, Pencil, AlertTriangle, SlidersHorizontal,
} from 'lucide-react';
import { lazyToast } from '@/lib/lazyToast';
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { ChannelTierPanel } from './ChannelTierPanel';
import { InventoryPurchaseCatalogPanel } from './InventoryPurchaseCatalogPanel';
import { PricingPublicationPanel } from './PricingPublicationPanel';

// ============================================================
// 类型
// ============================================================

type SkuType = 'credit_pack' | 'scenario_pack' | 'addon_pack';
type PricingCenterTab = 'default-rules' | 'purchase-catalog' | 'channel-rewards' | 'customer-packages';

interface PricingPackage {
  sku_template_id: number;
  sku_key: string;
  sku_type: string;
  display_name: string;
  subtitle?: string | null;
  capability_pitch?: string | null;
  points_granted: number;
  wholesale_cents: number;
  retail_cents: number;
  margin_cents: number;
  is_active: boolean;
  non_standard: boolean;
}

interface DefaultRule {
  points_per_yuan: number;
  wholesale_discount: number;
  agent_purchase_bonus_rate: number;
  points_per_yuan_after_discount: number;
}

interface Overview {
  active_count: number;
  min_wholesale_cents: number;
  retail_range: [number, number];
  non_standard_count: number;
}

const SKU_TABS: { key: SkuType; label: string }[] = [
  { key: 'credit_pack', label: '常规售卖包' },
  { key: 'scenario_pack', label: '用途推荐包' },
  { key: 'addon_pack', label: '单项补充包' },
];

// 进货价/售价/利润为 0 时显示"面议"(团队定制 / 人工谈价)
const moneyOrNegotiable = (cents: number): string =>
  cents > 0 ? formatCents(cents) : '面议';

const marginDisplay = (cents: number): string =>
  cents > 0 ? formatCents(cents) : (cents === 0 ? '面议' : '—');

// ============================================================
// 主页面
// ============================================================

export default function PricingCenter() {
  const [rule, setRule] = useState<DefaultRule | null>(null);
  const [packages, setPackages] = useState<PricingPackage[]>([]);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(false);
  const [centerTab, setCenterTab] = useState<PricingCenterTab>('default-rules');

  // 默认拿货规则编辑态(百分比/比例字符串绑定)
  const [discountInput, setDiscountInput] = useState('');
  const [bonusInput, setBonusInput] = useState('');
  const [ruleBusy, setRuleBusy] = useState(false);
  const [ruleOverrideMessage, setRuleOverrideMessage] = useState<string | null>(null);
  const [ruleCatalogVersion, setRuleCatalogVersion] = useState('');
  const [ruleConflicted, setRuleConflicted] = useState(false);

  // 算力包筛选 / tab
  const [tab, setTab] = useState<SkuType>('credit_pack');
  const [onlyActive, setOnlyActive] = useState(false);
  const [confirmDialog, askConfirm] = useConfirmDialog();

  // 抽屉编辑
  const [sheetOpen, setSheetOpen] = useState(false);
  const [editing, setEditing] = useState<PricingPackage | null>(null); // null = 新增模式

  const loadCenter = useCallback(async () => {
    setLoading(true);
    try {
      const r = await adminApi.pricingCenter();
      setRule(r.default_rule);
      setRuleCatalogVersion(r.catalog_version);
      setRuleConflicted(false);
      setPackages(r.packages || []);
      setOverview(r.overview);
      setDiscountInput(formatPercent(r.default_rule.wholesale_discount));
      setBonusInput(formatPercent(r.default_rule.agent_purchase_bonus_rate));
      setRuleOverrideMessage(r.environment_override?.active
        ? r.environment_override.message || '环境配置覆盖中'
        : null);
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'admin'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadCenter(); }, [loadCenter]);

  // 保存默认拿货规则(进货折扣 + 进货赠送)
  const saveRule = async () => {
    if (!ruleCatalogVersion) {
      lazyToast.error('价格版本尚未加载，请刷新后重试');
      return;
    }
    const discount = parsePercent(discountInput);
    const bonus = parsePercent(bonusInput);
    if (discount === undefined && bonus === undefined) {
      lazyToast.error('请填写进货折扣或进货赠送');
      return;
    }
    if (discount !== undefined && (discount < 0.39 || discount > 1.0)) {
      lazyToast.error('服务商进货折扣需在 39%-100% 之间');
      return;
    }
    if (bonus !== undefined && (bonus < 0 || bonus > 1.0)) {
      lazyToast.error('进货赠送比例需在 0%-100% 之间');
      return;
    }
    setRuleBusy(true);
    try {
      await adminApi.globalPricingConfigPut({
        expected_catalog_version: ruleCatalogVersion,
        wholesale_discount: discount,
        agent_purchase_bonus_rate: bonus,
      });
      lazyToast.success('默认规则已保存至管理配置 · 发布新目录后供新报价使用');
      await loadCenter();
    } catch (e: any) {
      if (e?.response?.status === 409) {
        setRuleConflicted(true);
        lazyToast.error('默认规则已被其他管理员更新，请刷新后重新确认');
      } else {
        lazyToast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'admin'));
      }
    } finally {
      setRuleBusy(false);
    }
  };

  // 批量按规则重算进货价
  const recalcAll = async () => {
    if (!(await askConfirm({ title: onlyActive ? '将按当前默认规则重算所有【已上架·标准】算力包的服务商进货价。活动包/人工谈价包默认跳过。已下单订单不受影响,是否继续?' : '将按当前默认规则重算所有【标准】算力包的服务商进货价。活动包/人工谈价包默认跳过。已下单订单不受影响,是否继续?' }))) return;
    try {
      const r = await adminApi.pricingRecalcAll(onlyActive, false);
      // [BH-015a] 一行都没动时,「实际变动 0 个」是一句**不解释任何事**的话 ——
      // admin 会读成「已经是最新的了」,而真相通常是「这个模式下永远不会动」。
      if (r.updated_count === 0 && r.no_op_reason) {
        lazyToast.info(r.no_op_reason, { duration: 10000 });
      } else {
        lazyToast.success(`已重算 · 实际变动 ${r.updated_count} 个算力包`);
      }
      // 平台进货规格只影响采购目录；服务商零售价不跟随平台建议价。
      if (r.notice) lazyToast.info(r.notice, { duration: 8000 });
      await loadCenter();
      // [复审返修2] 有特殊包被跳过 → 二次确认是否强制覆盖(覆盖丢失人工定价)
      if (r.skipped_count > 0 && await askConfirm({ title: `有 ${r.skipped_count} 个特殊定价包(活动包/人工谈价)按规则跳过了。`, description: '是否强制按当前默认规则覆盖这些特殊包?(覆盖后将丢失人工定价·已下单订单仍不受影响)', danger: true })) {
        const r2 = await adminApi.pricingRecalcAll(onlyActive, true);
        if (r2.updated_count === 0 && r2.no_op_reason) {
          lazyToast.info(r2.no_op_reason, { duration: 10000 });
        } else {
          lazyToast.success(`特殊包已覆盖 · 本次变动 ${r2.updated_count} 个`);
        }
        await loadCenter();
      }
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '重算失败 · 请重试', 'admin'));
    }
  };

  // 复制算力包
  const copyPackage = async (pkg: PricingPackage) => {
    try {
      await adminApi.pricingCopy(pkg.sku_template_id);
      lazyToast.success('已复制 · 新副本默认未上架');
      await loadCenter();
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '复制失败 · 请重试', 'admin'));
    }
  };

  // 上下架切换
  const togglePackage = async (pkg: PricingPackage, next: boolean) => {
    try {
      await adminApi.pricingToggle(pkg.sku_template_id, next);
      lazyToast.success(next ? '已上架' : '已下架');
      await loadCenter();
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '操作失败 · 请重试', 'admin'));
    }
  };

  const openEdit = (pkg: PricingPackage) => { setEditing(pkg); setSheetOpen(true); };
  const openCreate = () => { setEditing(null); setSheetOpen(true); };

  const packagesForTab = (key: SkuType): PricingPackage[] =>
    packages.filter((p) => p.sku_type === key && (!onlyActive || p.is_active));

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-7xl">
      {confirmDialog}
      {/* 顶部标题 + 操作 */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <SlidersHorizontal className="w-6 h-6" />
          <div>
            <h1 className="text-2xl font-bold">算力定价中心</h1>
            <p className="text-sm text-muted-foreground mt-0.5">
              统一管理默认进货规则、进货价目表、渠道奖励和普通客户算力包。
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={loadCenter} disabled={loading}>
            <RefreshCw className="w-4 h-4 mr-1" /> 刷新
          </Button>
          {centerTab === 'customer-packages' && (
            <Button size="sm" onClick={openCreate}>
              <Plus className="w-4 h-4 mr-1" /> 新增算力包
            </Button>
          )}
        </div>
      </div>

      <PricingPublicationPanel />

      <Tabs value={centerTab} onValueChange={(value) => setCenterTab(value as PricingCenterTab)}>
        <TabsList className="grid h-auto w-full grid-cols-2 gap-1 lg:grid-cols-4" aria-label="定价中心页面">
          <TabsTrigger value="default-rules">默认进货规则</TabsTrigger>
          <TabsTrigger value="purchase-catalog">进货价目表</TabsTrigger>
          <TabsTrigger value="channel-rewards">渠道奖励</TabsTrigger>
          <TabsTrigger value="customer-packages">普通客户算力包</TabsTrigger>
        </TabsList>

      {/* 上半两栏:默认拿货规则 + 单个服务商特殊设置 */}
      <TabsContent value="default-rules" className="grid gap-6 pt-4 md:grid-cols-2">
        {/* 左:默认拿货规则 */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">默认进货规则</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {ruleOverrideMessage && (
              <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300">
                <AlertTriangle className="mr-2 inline h-4 w-4" />{ruleOverrideMessage}。当前只能查看，无法保存。
              </div>
            )}
            {ruleConflicted && (
              <div className="rounded-md border border-orange-500/40 bg-orange-500/10 p-3 text-sm text-orange-700 dark:text-orange-300">
                <AlertTriangle className="mr-2 inline h-4 w-4" />默认规则已被其他管理员更新。本地输入尚未保存，请刷新后重新确认。
              </div>
            )}
            <div className="space-y-1">
              <Label>基础换算</Label>
              <div className="text-sm rounded-md border bg-muted/30 px-3 py-2">
                1 元 = {rule ? rule.points_per_yuan : '—'} 算力
                <span className="ml-2 text-xs text-muted-foreground">(系统固定·本页暂不开放修改)</span>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label>服务商进货折扣</Label>
                <Input
                  value={discountInput}
                  onChange={(e) => setDiscountInput(e.target.value)}
                  placeholder="如 80%"
                />
              </div>
              <div className="space-y-1">
                <Label>进货赠送</Label>
                <Input
                  value={bonusInput}
                  onChange={(e) => setBonusInput(e.target.value)}
                  placeholder="如 10%"
                />
              </div>
            </div>
            <div className="text-sm rounded-md border border-primary/30 bg-primary/5 px-3 py-2">
              服务商 1 元进货 ≈{' '}
              <span className="font-semibold">
                {rule ? rule.points_per_yuan_after_discount : '—'}
              </span>{' '}
              算力
            </div>
            <div className="flex gap-2 flex-wrap">
              <Button onClick={saveRule} disabled={ruleBusy || !rule || !!ruleOverrideMessage || ruleConflicted || !ruleCatalogVersion}>
                <Save className="w-4 h-4 mr-1" /> 保存默认规则
              </Button>
              <Button variant="outline" onClick={loadCenter} disabled={loading}>
                <RotateCcw className="w-4 h-4 mr-1" /> 放弃修改并刷新
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              保存只更新管理配置；发布新目录后才影响新报价。旧报价、待支付订单和历史订单保持原快照。
            </p>
          </CardContent>
        </Card>

        {/* 右:单个服务商特殊设置 */}
        <AgentOverridePanel
          pointsPerYuan={rule?.points_per_yuan ?? null}
          catalogVersion={ruleCatalogVersion}
          onSaved={loadCenter}
        />
      </TabsContent>

      <TabsContent value="purchase-catalog" className="pt-4">
        <InventoryPurchaseCatalogPanel />
      </TabsContent>

      <TabsContent value="channel-rewards" className="pt-4">
        <ChannelTierPanel embedded />
      </TabsContent>

      <TabsContent value="customer-packages" className="space-y-4 pt-4">
        <div>
          <h2 className="text-xl font-semibold">普通客户算力包</h2>
          <p className="text-sm text-muted-foreground">管理面向普通客户的算力包、建议售价和上架状态。</p>
        </div>
        {/* 中间 4 概览卡 */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <OverviewCard
          label="已上架算力包"
          value={overview ? String(overview.active_count) : '—'}
        />
        <OverviewCard
          label="最低进货价"
          value={overview ? moneyOrNegotiable(overview.min_wholesale_cents) : '—'}
        />
        <OverviewCard
          label="建议售价区间"
          value={
            overview && overview.retail_range[1] > 0
              ? `${formatCents(overview.retail_range[0])} ~ ${formatCents(overview.retail_range[1])}`
              : '—'
          }
        />
        <OverviewCard
          label="特殊定价包"
          value={overview ? String(overview.non_standard_count) : '—'}
        />
        </div>

        {/* 下半:算力包设置 */}
        <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <CardTitle className="text-base">算力包设置</CardTitle>
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <Switch checked={onlyActive} onCheckedChange={setOnlyActive} />
                只看已上架
              </label>
              <Button variant="outline" size="sm" onClick={recalcAll}>
                <RotateCcw className="w-4 h-4 mr-1" /> 批量按规则重算
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <Tabs value={tab} onValueChange={(v) => setTab(v as SkuType)}>
            <TabsList>
              {SKU_TABS.map((t) => (
                <TabsTrigger key={t.key} value={t.key}>{t.label}</TabsTrigger>
              ))}
            </TabsList>
            {SKU_TABS.map((t) => (
              <TabsContent key={t.key} value={t.key} className="pt-4">
                <PackageTable
                  packages={packagesForTab(t.key)}
                  onEdit={openEdit}
                  onCopy={copyPackage}
                  onToggle={togglePackage}
                />
              </TabsContent>
            ))}
          </Tabs>
        </CardContent>
        </Card>
      </TabsContent>
      </Tabs>

      {/* 编辑 / 新增抽屉 */}
      <PackageSheet
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        pkg={editing}
        currentTab={tab}
        rule={rule}
        onDone={loadCenter}
      />
    </div>
  );
}

// ============================================================
// 概览卡
// ============================================================

function OverviewCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="py-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="text-xl font-bold mt-1">{value}</div>
      </CardContent>
    </Card>
  );
}

// ============================================================
// 算力包表格
// ============================================================

function PackageTable({
  packages, onEdit, onCopy, onToggle,
}: {
  packages: PricingPackage[];
  onEdit: (p: PricingPackage) => void;
  onCopy: (p: PricingPackage) => void;
  onToggle: (p: PricingPackage, next: boolean) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b bg-muted/50">
            <th className="text-left p-3">算力包</th>
            <th className="text-left p-3">适合场景</th>
            <th className="text-right p-3">包含算力</th>
            <th className="text-right p-3">服务商进货价</th>
            <th className="text-right p-3">建议客户售价</th>
            <th className="text-right p-3">服务商预计利润</th>
            <th className="text-left p-3">上架状态</th>
            <th className="text-right p-3">操作</th>
          </tr>
        </thead>
        <tbody>
          {packages.length === 0 && (
            <tr>
              <td colSpan={8} className="text-center p-6 text-muted-foreground">
                暂无算力包
              </td>
            </tr>
          )}
          {packages.map((p) => (
            <tr key={p.sku_template_id} className="border-b">
              <td className="p-3">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{p.display_name}</span>
                  {p.non_standard && (
                    <Badge variant="secondary" className="text-xs">活动包</Badge>
                  )}
                </div>
                <div className="text-xs text-muted-foreground">内部编号 {p.sku_key}</div>
                {p.subtitle && (
                  <div className="text-xs text-muted-foreground">{p.subtitle}</div>
                )}
              </td>
              <td className="p-3 text-muted-foreground max-w-[220px]">
                {p.capability_pitch || '—'}
              </td>
              <td className="p-3 text-right font-mono">
                {p.points_granted.toLocaleString()}
              </td>
              <td className="p-3 text-right font-mono">
                {moneyOrNegotiable(p.wholesale_cents)}
              </td>
              <td className="p-3 text-right font-mono">
                {moneyOrNegotiable(p.retail_cents)}
              </td>
              <td className="p-3 text-right font-mono">
                {marginDisplay(p.margin_cents)}
              </td>
              <td className="p-3">
                {p.is_active
                  ? <Badge>已上架</Badge>
                  : <Badge variant="secondary">未上架</Badge>}
              </td>
              <td className="p-3">
                <div className="flex gap-1 justify-end">
                  <Button size="sm" variant="outline" onClick={() => onEdit(p)}>
                    <Pencil className="w-4 h-4 mr-1" /> 编辑
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => onCopy(p)}>
                    <Copy className="w-4 h-4 mr-1" /> 复制
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => onToggle(p, !p.is_active)}
                  >
                    {p.is_active ? '下架' : '上架'}
                  </Button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ============================================================
// 编辑 / 新增抽屉
// ============================================================

function PackageSheet({
  open, onOpenChange, pkg, currentTab, rule, onDone,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  pkg: PricingPackage | null;       // null = 新增
  currentTab: SkuType;
  rule: DefaultRule | null;         // 默认规则(算"留空自动算"进货价用)
  onDone: () => Promise<void> | void;
}) {
  const isCreate = pkg === null;

  const [name, setName] = useState('');
  const [subtitle, setSubtitle] = useState('');
  const [pitch, setPitch] = useState('');
  const [points, setPoints] = useState('');
  const [wholesale, setWholesale] = useState('');
  const [retail, setRetail] = useState('');
  const [active, setActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [nonStandard, setNonStandard] = useState(false);

  // 抽屉打开 / 切换目标时同步表单
  useEffect(() => {
    if (!open) return;
    setNonStandard(pkg?.non_standard ?? false);
    setName(pkg?.display_name ?? '');
    setSubtitle(pkg?.subtitle ?? '');
    setPitch(pkg?.capability_pitch ?? '');
    setPoints(pkg ? String(pkg.points_granted) : '');
    setWholesale(pkg ? centsToYuan(pkg.wholesale_cents) : '');
    setRetail(pkg ? centsToYuan(pkg.retail_cents) : '');
    setActive(pkg?.is_active ?? false);
  }, [open, pkg]);

  // 按当前规则重算进货价(仅编辑态可用)
  const recalc = async () => {
    if (!pkg) return;
    setBusy(true);
    try {
      const r = await adminApi.pricingRecalc(pkg.sku_template_id);
      setWholesale(centsToYuan(r.wholesale_cents));
      setNonStandard(false);
      lazyToast.success('已按当前规则重算服务商进货价');
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '重算失败 · 请重试', 'admin'));
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!name.trim()) { lazyToast.error('请填写包名称'); return; }
    const pointsInt = parseInt(points || '0', 10);
    if (Number.isNaN(pointsInt) || pointsInt < 0) { lazyToast.error('包含算力须为非负整数'); return; }
    const retailCents = yuanToCents(retail);
    if (retailCents < 0) { lazyToast.error('建议客户售价须为非负数'); return; }
    const wholesaleRaw = wholesale.trim();
    let wholesaleCents: number | undefined;
    if (wholesaleRaw === '') {
      // [复审返修3] 留空 = 按当前默认规则自动算(create + edit 一致·与输入框文案一致)
      if (rule) {
        const numer = Math.round(rule.wholesale_discount * 10000);
        const denom = rule.points_per_yuan * 100;
        wholesaleCents = Math.ceil((pointsInt * numer) / denom);
      } else {
        wholesaleCents = undefined; // 规则未加载·交后端兜底(create 按规则算)
      }
    } else {
      wholesaleCents = yuanToCents(wholesale);
      if (wholesaleCents < 0) { lazyToast.error('服务商进货价须为非负数'); return; }
    }

    setBusy(true);
    try {
      if (isCreate) {
        const r = await adminApi.pricingCreate({
          sku_type: currentTab,
          display_name: name.trim(),
          subtitle: subtitle.trim(),
          capability_pitch: pitch.trim() || undefined,
          points_granted: pointsInt,
          retail_cents: retailCents,
          wholesale_cents: wholesaleCents,
          is_active: active,
        });
        setNonStandard(r.non_standard);
        lazyToast.success('算力包已新增' + (active ? '' : '(默认未上架)'));
      } else {
        const r = await adminApi.pricingSave(pkg.sku_template_id, {
          display_name: name.trim(),
          subtitle: subtitle.trim(),
          capability_pitch: pitch.trim() || undefined,
          points_granted: pointsInt,
          retail_cents: retailCents,
          wholesale_cents: wholesaleCents,
          is_active: active,
        });
        setNonStandard(r.non_standard);
        lazyToast.success('算力包已保存');
      }
      onOpenChange(false);
      await onDone();
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'admin'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-md flex flex-col">
        <SheetHeader>
          <SheetTitle>{isCreate ? '新增算力包' : '编辑算力包'}</SheetTitle>
        </SheetHeader>

        <div className="flex-1 overflow-y-auto px-4 space-y-4">
          {!isCreate && pkg && (
            <div className="text-xs text-muted-foreground">
              内部编号 {pkg.sku_key}
            </div>
          )}
          <div className="space-y-1">
            <Label>包名称</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如:基础体验包" />
          </div>
          <div className="space-y-1">
            <Label>副标题</Label>
            <Input value={subtitle} onChange={(e) => setSubtitle(e.target.value)} placeholder="可选 · 留空保存为空" />
          </div>
          <div className="space-y-1">
            <Label>适合场景</Label>
            <Input value={pitch} onChange={(e) => setPitch(e.target.value)} placeholder="给服务商看的推荐场景" />
          </div>
          <div className="space-y-1">
            <Label>包含算力</Label>
            <Input type="number" value={points} onChange={(e) => setPoints(e.target.value)} placeholder="如 13000" />
          </div>
          <div className="space-y-1">
            <Label>服务商进货价(元)</Label>
            <Input
              type="number"
              step="0.01"
              value={wholesale}
              onChange={(e) => setWholesale(e.target.value)}
              placeholder="留空则按当前默认规则自动计算"
            />
          </div>
          <div className="space-y-1">
            <Label>建议客户售价(元)</Label>
            <Input type="number" step="0.01" value={retail} onChange={(e) => setRetail(e.target.value)} placeholder="如 200" />
          </div>
          <div className="flex items-center gap-2">
            <Switch checked={active} onCheckedChange={setActive} />
            <Label className="cursor-pointer">上架给服务商使用</Label>
          </div>

          {nonStandard && (
            <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50/40 p-3 text-xs text-amber-700">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span>当前设置不是按默认规则计算,适合活动包或人工谈价。</span>
            </div>
          )}
        </div>

        <SheetFooter>
          <div className="flex gap-2">
            {!isCreate && (
              <Button variant="outline" onClick={recalc} disabled={busy}>
                <RotateCcw className="w-4 h-4 mr-1" /> 按当前规则重算
              </Button>
            )}
            <Button onClick={submit} disabled={busy} className="flex-1">
              <Save className="w-4 h-4 mr-1" /> 保存
            </Button>
          </div>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}

// ============================================================
// 单个服务商特殊设置
// ============================================================

export function AgentOverridePanel({
  pointsPerYuan,
  catalogVersion,
  onSaved,
  initialAgentUserId,
  initialAgentName,
}: {
  pointsPerYuan: number | null;
  catalogVersion: string;
  onSaved: () => Promise<void>;
  // [WP2 §5.4] 从用户详情页内嵌时预填目标服务商 user_id,免 admin 手输/搜索。
  // 缺省(独立定价中心用法)时行为不变:展示搜索框自查。
  initialAgentUserId?: number;
  initialAgentName?: string;
}) {
  const embedded = initialAgentUserId != null;
  const [agentId, setAgentId] = useState('');
  const [loaded, setLoaded] = useState(false);  // 是否已查询到一个服务商
  const [loadedAgentId, setLoadedAgentId] = useState<number | null>(null);
  const [loadedAgentName, setLoadedAgentName] = useState('');  // 选中服务商名(展示确认)
  const [discountInput, setDiscountInput] = useState('');  // 专属进货折扣(友好%/折)
  const [markupInput, setMarkupInput] = useState('');      // 专属客户售价倍数
  const [quoteMarkupInput, setQuoteMarkupInput] = useState('');  // [WP2 §5.4] 专属报价系数(1.0-5.0)
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  const num = (v: string): number | undefined => {
    const s = (v || '').trim();
    if (s === '') return undefined;
    const n = Number(s);
    return Number.isNaN(n) ? undefined : n;
  };

  // 后端 numer/denom → 友好折扣%(与后端 _ratio_to_discount 同口径)
  const ratioToDiscount = (numer: unknown, denom: unknown): string => {
    const n = Number(numer); const d = Number(denom);
    if (!pointsPerYuan || !d || Number.isNaN(n) || Number.isNaN(d)) return '';
    const discount = +((n * pointsPerYuan) / (d * 100)).toFixed(4);
    return formatPercent(discount);
  };

  // 把后端 override 行套进各输入框(load / loadById 共用,含 [WP2 §5.4] 报价系数)。
  const applyOverride = (o: Record<string, unknown>) => {
    setDiscountInput(ratioToDiscount(o.wholesale_numer, o.wholesale_denom));
    setMarkupInput(o.sku_markup_override == null ? '' : String(o.sku_markup_override));
    setQuoteMarkupInput(o.quote_markup_override == null ? '' : String(o.quote_markup_override));
    setNote(o.note == null ? '' : String(o.note));
  };

  const load = async () => {
    const kw = agentId.trim();
    if (!kw) { lazyToast.error('请输入服务商编号 / 手机号 / 用户名'); return; }
    try {
      // [复审返修4] 先按编号/手机号/用户名查服务商(避免 admin 死记 user_id)
      const lk = await adminApi.pricingAgentLookup(kw);
      const agents = (lk.results || []).filter((x) => x.agent_level >= 1);
      if (agents.length === 0) {
        lazyToast.error('未找到对应服务商,请确认编号/手机号/用户名(对方需为服务商)');
        return;
      }
      const target = agents[0];
      const id = target.user_id;
      const r = await adminApi.agentPricingOverrideGet(id);
      applyOverride((r.override as Record<string, unknown>) || {});
      setLoadedAgentId(id);
      setLoadedAgentName(target.display_name || target.username || target.phone || String(id));
      setLoaded(true);
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '查询失败 · 请重试', 'admin'));
    }
  };

  // [WP2 §5.4] 内嵌用户详情页:直接按 user_id 载入专属规则(免搜索)。
  const loadById = async (id: number, name?: string) => {
    try {
      const r = await adminApi.agentPricingOverrideGet(id);
      applyOverride((r.override as Record<string, unknown>) || {});
      setLoadedAgentId(id);
      setLoadedAgentName(name || String(id));
      setLoaded(true);
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '载入专属规则失败 · 请重试', 'admin'));
    }
  };

  useEffect(() => {
    if (initialAgentUserId != null) {
      void loadById(initialAgentUserId, initialAgentName);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialAgentUserId]);

  const save = async () => {
    if (loadedAgentId == null) return;
    if (!catalogVersion) { lazyToast.error('价格版本尚未加载，请刷新后重试'); return; }
    const discount = parsePercent(discountInput);
    if (discount !== undefined && (discount < 0.3 || discount > 1.0)) {
      lazyToast.error('专属进货折扣需在 30%-100% 之间');
      return;
    }
    // [WP2 §5.4] 报价系数(quote_markup)与后端同口径校验 [1.0, 5.0]
    const quoteMarkup = num(quoteMarkupInput);
    if (quoteMarkup !== undefined && (quoteMarkup < 1.0 || quoteMarkup > 5.0)) {
      lazyToast.error('专属报价系数需在 1.0-5.0 之间');
      return;
    }
    // 友好折扣 → numer/denom(与后端 _discount_to_ratio 同口径:denom=每元算力×100,numer=折扣×10000)
    let numer: number | undefined;
    let denom: number | undefined;
    if (discount !== undefined && pointsPerYuan) {
      denom = pointsPerYuan * 100;
      numer = Math.round(discount * 10000);
    }
    setBusy(true);
    try {
      await adminApi.agentPricingOverridePut(loadedAgentId, {
        expected_catalog_version: catalogVersion,
        quote_markup_override: quoteMarkup,  // [WP2 §5.4] 专属报价系数(catalog-versioned 单一写路径)
        sku_markup_override: num(markupInput),
        wholesale_numer: numer,
        wholesale_denom: denom,
        note: note.trim() || undefined,
      });
      lazyToast.success('专属规则已保存');
      await onSaved();
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'admin'));
    } finally { setBusy(false); }
  };

  const clear = async () => {
    if (loadedAgentId == null) return;
    if (!catalogVersion) { lazyToast.error('价格版本尚未加载，请刷新后重试'); return; }
    setBusy(true);
    try {
      await adminApi.agentPricingOverrideDelete(loadedAgentId, catalogVersion);
      lazyToast.success('已清空 · 该服务商回到默认规则');
      await onSaved();
      await load();
    } catch (e) {
      lazyToast.error(formatApiErrorForDisplay(e, '清空失败 · 请重试', 'admin'));
    } finally { setBusy(false); }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">单个服务商特殊设置</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {!embedded && (
          <div className="flex gap-2">
            <Input
              value={agentId}
              onChange={(e) => setAgentId(e.target.value)}
              placeholder="服务商编号 / 手机号 / 用户名"
            />
            <Button variant="outline" onClick={load}>
              <Search className="w-4 h-4 mr-1" /> 查询
            </Button>
          </div>
        )}

        {loaded ? (
          <>
            <div className="text-xs text-muted-foreground rounded-md border bg-muted/30 px-3 py-2">
              已选服务商:<span className="font-medium text-foreground">{loadedAgentName}</span>(编号 {loadedAgentId})
            </div>
            <div className="space-y-1">
              <Label>专属报价系数</Label>
              <Input
                value={quoteMarkupInput}
                onChange={(e) => setQuoteMarkupInput(e.target.value)}
                placeholder="留空=用默认 · 范围 1.0-5.0"
                inputMode="decimal"
                data-testid="agent-quote-markup-input"
              />
              <div className="text-[10px] text-muted-foreground/80">面向该服务商终端客户报价的系数(1.0-5.0)。</div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label>专属进货折扣</Label>
                <Input
                  value={discountInput}
                  onChange={(e) => setDiscountInput(e.target.value)}
                  placeholder="留空=用默认"
                />
              </div>
              <div className="space-y-1">
                <Label>专属客户售价倍数</Label>
                <Input
                  value={markupInput}
                  onChange={(e) => setMarkupInput(e.target.value)}
                  placeholder="留空=用默认"
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label>备注</Label>
              <Input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="可选"
              />
            </div>
            <div className="flex gap-2">
              <Button onClick={save} disabled={busy}>
                <Save className="w-4 h-4 mr-1" /> 保存专属规则
              </Button>
              <Button variant="outline" onClick={clear} disabled={busy}>
                <RotateCcw className="w-4 h-4 mr-1" /> 清空(回到默认)
              </Button>
            </div>
          </>
        ) : embedded ? (
          <div className="text-center py-6 text-sm text-muted-foreground">载入专属规则中…</div>
        ) : (
          <div className="text-center py-8 text-sm text-muted-foreground">
            <div className="font-medium">未选择服务商</div>
            <div className="mt-1">请输入服务商编号 / 手机号 / 用户名,查看或设置专属规则。</div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ============================================================
// 工具函数
// ============================================================

// 小数(0.8) → 百分比字符串("80%")
function formatPercent(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '';
  return `${+(ratio * 100).toFixed(2)}%`;
}

// 百分比/折扣输入("80%" / "80" / "8折" / "0.8") → 小数(0.8)·空返 undefined
function parsePercent(input: string): number | undefined {
  const s = (input || '').trim();
  if (s === '') return undefined;
  const zheMatch = s.match(/^([\d.]+)\s*折$/);
  if (zheMatch) {
    const z = parseFloat(zheMatch[1]);
    if (Number.isNaN(z)) return undefined;
    return +(z / 10).toFixed(4); // "8折" → 0.8
  }
  const numStr = s.replace('%', '').trim();
  const n = parseFloat(numStr);
  if (Number.isNaN(n)) return undefined;
  // 带 % 或 >1 视为百分比;否则视为已是小数
  if (s.includes('%') || n > 1) return +(n / 100).toFixed(4);
  return +n.toFixed(4);
}

function centsToYuan(cents: number): string {
  return (cents / 100).toFixed(2);
}

function yuanToCents(yuan: string): number {
  const n = parseFloat((yuan || '').trim());
  if (Number.isNaN(n)) return 0;
  return Math.round(n * 100);
}
