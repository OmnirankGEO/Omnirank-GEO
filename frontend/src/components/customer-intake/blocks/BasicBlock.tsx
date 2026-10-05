/**
 * BasicBlock · 客户基础三字段(name / industry / city) + AI 联网填充 + 已有客户复用
 *
 * Phase 06 · CTO-15.23 · 2026-05-03
 * AI 填充调 /api/brand/auto-fill · 把返回字段下放到上层(industry/city/business/target_users/competitors/keywords)
 *
 * [CTO-15.23 2026-05-17 老板报"快速写作加已有客户复用 · 跟品牌体检逻辑一致"]
 * - 客户/品牌名 Input 改成可搜索下拉 · 输入时实时过滤已有客户
 * - 选中已有客户 → 调 GET /api/my-clients/:id 拿完整 brand+profile · 自动填全字段
 * - lift up brand_id 给上层 Dialog · 提交时走 reuse 跳过同名检测
 */

import { useState, useEffect, useRef, useMemo } from 'react';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Sparkles, Loader2, ChevronDown, X } from 'lucide-react';
import { usePricing } from '@/context/PricingContext';
import { FeatureCostBadge } from '@/components/FeatureCostBadge';
import { authApi } from '@/context/AuthContext';
import { authFetch } from '@/lib/api';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { industryCategoryText, loadIndustryTaxonomy, nameOfFrom } from '@/lib/industryTaxonomy';
import type { CustomerIntakeData, AiFilledFields, ValidationErrors, SellingPoint, CaseStudy } from '../types';

interface ClientOption {
  id: number;
  name: string;
  industry?: string;
}

interface Props {
  data: CustomerIntakeData;
  onChange: (patch: Partial<CustomerIntakeData>) => void;
  errors?: ValidationErrors;
  showAiFill?: boolean;
  /** 选中已有客户时 lift up brand_id · 上层 Dialog 提交时走 reuse */
  onSelectExisting?: (brandId: number | null) => void;
}

const COMMON_INDUSTRIES = [
  '建筑装饰、装修和其他建筑业',
  '室内设计',
  '家政服务',
  '教育培训',
  '美容医美',
  '餐饮',
  '法律咨询',
  '医疗健康',
  '金融服务',
  '电商零售',
];

/** 字符串字段试 JSON.parse · 失败按"逗号/换行分隔"切 */
function parseStringList(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.filter(x => typeof x === 'string' && x.trim()) as string[];
  if (typeof raw !== 'string' || !raw.trim()) return [];
  const trimmed = raw.trim();
  if (trimmed.startsWith('[')) {
    try {
      const arr = JSON.parse(trimmed);
      if (Array.isArray(arr)) return arr.filter(x => typeof x === 'string' && x.trim());
    } catch { /* fallthrough */ }
  }
  return trimmed.split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
}

function parseSellingPoints(raw: unknown): SellingPoint[] {
  if (Array.isArray(raw)) {
    return raw.filter(x => x && typeof x === 'object' && typeof (x as SellingPoint).point === 'string') as SellingPoint[];
  }
  if (typeof raw === 'string' && raw.trim().startsWith('[')) {
    try {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) return parseSellingPoints(arr);
    } catch { /* fallthrough */ }
  }
  return [];
}

function parseSuccessCases(raw: unknown): CaseStudy[] {
  if (Array.isArray(raw)) {
    return raw.filter(x => x && typeof x === 'object' && typeof (x as CaseStudy).client === 'string') as CaseStudy[];
  }
  if (typeof raw === 'string' && raw.trim().startsWith('[')) {
    try {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) return parseSuccessCases(arr);
    } catch { /* fallthrough */ }
  }
  return [];
}

export function BasicBlock({ data, onChange, errors, showAiFill = true, onSelectExisting }: Props) {
  // [#65] 价目取自 feature_pricing SSOT;为空 = 该 code 还没配 ⇒ 不显示任何数字。
  const { getCost } = usePricing();
  const brandFillCost = getCost('brand_fill');
  const [aiFilling, setAiFilling] = useState(false);

  // 已有客户列表 + 下拉状态
  const [clients, setClients] = useState<ClientOption[]>([]);
  const [dropOpen, setDropOpen] = useState(false);
  const [reusedBrandId, setReusedBrandId] = useState<number | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const dropRef = useRef<HTMLDivElement>(null);

  // mount 拉客户列表
  useEffect(() => {
    authFetch('/api/my-clients?page_size=200')
      .then(r => r.json())
      .then(d => {
        const list = (d?.clients || []) as Array<Record<string, unknown>>;
        setClients(list.map(b => ({
          id: Number(b.id),
          name: String(b.name || b.brand_name || ''),
          industry: typeof b.industry === 'string' ? b.industry : undefined,
        })).filter(b => b.id && b.name));
      })
      .catch(() => { /* 列表拉不到也允许直接输入新客户名 */ });
  }, []);

  // 点外部关闭下拉
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropRef.current && !dropRef.current.contains(e.target as Node)) setDropOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const filteredClients = useMemo(() => {
    const q = (data.name || '').trim().toLowerCase();
    if (!q) return clients.slice(0, 20);
    return clients.filter(c => c.name.toLowerCase().includes(q)).slice(0, 20);
  }, [clients, data.name]);

  // 选中已有客户 → 拉详情 + patch 全字段 + lift up brand_id
  const handleSelectClient = async (client: ClientOption) => {
    setDropOpen(false);
    setReusedBrandId(client.id);
    onSelectExisting?.(client.id);
    setLoadingDetail(true);
    try {
      const res = await authFetch(`/api/my-clients/${client.id}`);
      const json = await res.json();
      if (!json?.success) {
        // fallback:至少把 name + industry 填上
        onChange({ name: client.name, industry: client.industry || '' });
        toast.warning('已选中客户,但资料拉取失败,请手动补全');
        return;
      }
      const brand = (json.brand || {}) as Record<string, unknown>;
      const profile = (json.profile || {}) as Record<string, unknown>;
      const patch: Partial<CustomerIntakeData> = {
        name: String(brand.name || client.name || ''),
        /* [WO_267] `/api/my-clients/{id}` 不附大类中文名,新写入的原值是英文 key ——
           预填进自由文本框前先按字典翻成中文名,认不出就留空,不把 key 填给用户 */
        industry: String(brand.industry || industryCategoryText(
          brand.industry_category as string | null | undefined,
          brand.industry_category_name as string | null | undefined,
          nameOfFrom(await loadIndustryTaxonomy())) || ''),
        city: String(brand.cities || ''),
      };
      if (typeof profile.business === 'string' && profile.business) patch.business = profile.business;
      if (typeof profile.target_users === 'string' && profile.target_users) patch.target_users = profile.target_users;
      if (typeof profile.company_intro === 'string' && profile.company_intro) patch.company_intro = profile.company_intro;
      if (typeof profile.core_value === 'string' && profile.core_value) patch.core_value = profile.core_value;
      const sps = parseSellingPoints(profile.selling_points);
      if (sps.length > 0) patch.selling_points = sps;
      const cases = parseSuccessCases(profile.success_cases);
      if (cases.length > 0) patch.success_cases = cases;
      const competitors = parseStringList(profile.competitors);
      if (competitors.length > 0) patch.competitors = competitors;
      onChange(patch);
      toast.success(`已加载「${patch.name}」资料,可继续补关键词`);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`加载客户资料失败:${msg}`);
    } finally {
      setLoadingDetail(false);
    }
  };

  // 用户手动改 name → 清除"已选中已有客户"状态
  const handleNameChange = (v: string) => {
    onChange({ name: v });
    if (reusedBrandId !== null) {
      setReusedBrandId(null);
      onSelectExisting?.(null);
    }
    setDropOpen(true);
  };

  const handleClearReuse = () => {
    setReusedBrandId(null);
    onSelectExisting?.(null);
    onChange({ name: '' });
  };

  const handleAiFill = async () => {
    if (!data.name?.trim()) {
      toast.warning('请先填客户名,AI 才能联网搜资料');
      return;
    }
    setAiFilling(true);
    try {
      const res = await authApi.post<{ success: boolean; data?: AiFilledFields; error?: string }>(
        '/api/brand/auto-fill',
        { text: data.name },
        { timeout: 90000 },
      );
      if (res.data.success && res.data.data) {
        const filled = res.data.data;
        const patch: Partial<CustomerIntakeData> = {};
        if (!data.industry?.trim() && filled.industry) patch.industry = String(filled.industry);
        if (!data.city?.trim() && filled.city) patch.city = String(filled.city);
        if (!data.business?.trim() && filled.business) patch.business = String(filled.business);
        if (!data.target_users?.trim() && filled.target_users) patch.target_users = String(filled.target_users);
        if (!data.company_intro?.trim() && filled.company_intro) patch.company_intro = String(filled.company_intro);
        if (Array.isArray(filled.competitors) && filled.competitors.length > 0 && (!data.competitors || data.competitors.length === 0)) {
          patch.competitors = filled.competitors;
        }
        if (Array.isArray(filled.keywords) && filled.keywords.length > 0 && (!data.seed_keywords || data.seed_keywords.length === 0)) {
          patch.seed_keywords = filled.keywords.slice(0, 15);
        }
        onChange(patch);
        toast.success('AI 已填充 · 请核对各字段');
      } else {
        toast.error(`AI 填充失败:${res.data.error || '未知错误'}`);
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`AI 填充失败:${msg}`);
    } finally {
      setAiFilling(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-foreground">客户基础</h3>
        {showAiFill && (
          <>
          {/* 🔴 [#65 2026-09-05] 价格必须看得见(2026-06-03 拍板第 ② 段)。取不到价一个字都不显示。 */}
          {brandFillCost != null && (
            <span data-testid="basic-ai-fill-cost" className="self-center mr-2">
              <FeatureCostBadge featureCode="brand_fill" variant="inline" />
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={handleAiFill}
            disabled={aiFilling || !data.name?.trim()}
            className="text-xs"
          >
            {aiFilling ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />
            ) : (
              <Sparkles className="h-3.5 w-3.5 mr-1" />
            )}
            AI 联网填充
          </Button>
          </>
        )}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="sm:col-span-2 space-y-1">
          <Label htmlFor="ci-name" className="text-xs">客户/品牌名 *</Label>
          {/* 2026-05-17 加已有客户搜索下拉 · 输入时实时过滤 · 选中后自动填全字段 */}
          <div className="relative" ref={dropRef}>
            <Input
              id="ci-name"
              value={data.name || ''}
              onChange={(e) => handleNameChange(e.target.value)}
              onFocus={() => setDropOpen(true)}
              placeholder="输入客户名搜索已有 · 或直接输入新客户名"
              className={cn(errors?.name ? 'border-rose-500' : '', 'pr-16')}
              autoComplete="off"
            />
            <div className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center gap-0.5">
              {loadingDetail && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
              {reusedBrandId !== null && !loadingDetail && (
                <button
                  type="button"
                  onClick={handleClearReuse}
                  className="p-1 text-muted-foreground/60 hover:text-foreground"
                  aria-label="取消已选客户"
                  title="取消已选客户"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
              <button
                type="button"
                tabIndex={-1}
                className="p-1 text-muted-foreground/60 hover:text-foreground"
                onClick={() => setDropOpen(v => !v)}
                aria-label="展开已有客户下拉"
              >
                <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', dropOpen && 'rotate-180')} />
              </button>
            </div>
            {dropOpen && (
              <div className="absolute z-50 top-full left-0 right-0 mt-1 max-h-48 overflow-y-auto rounded-lg border border-border bg-popover shadow-lg">
                {filteredClients.length > 0 ? filteredClients.map(c => (
                  <button
                    key={c.id}
                    type="button"
                    className="w-full text-left px-3 py-2 text-sm hover:bg-foreground/5 transition-colors flex items-center justify-between"
                    onClick={() => handleSelectClient(c)}
                  >
                    <span className="truncate">{c.name}</span>
                    {c.industry && <span className="text-[10px] text-muted-foreground/50 shrink-0 ml-2">{c.industry}</span>}
                  </button>
                )) : (
                  <div className="px-3 py-3 text-xs text-muted-foreground/70 text-center">
                    {clients.length === 0 ? '暂无已有客户 · 直接输入新客户名即可' : '无匹配客户 · 继续输入将创建新客户'}
                  </div>
                )}
              </div>
            )}
          </div>
          {errors?.name && <p className="text-xs text-rose-600">{errors.name}</p>}
          {reusedBrandId !== null && !loadingDetail && (
            <p className="text-[11px] text-emerald-600 dark:text-emerald-400">
              已选中已有客户 · 提交时复用档案 · 不会重复创建
            </p>
          )}
        </div>

        <div className="space-y-1">
          <Label htmlFor="ci-industry" className="text-xs">行业 *</Label>
          <Input
            id="ci-industry"
            value={data.industry || ''}
            onChange={(e) => onChange({ industry: e.target.value })}
            placeholder="例:建筑装饰"
            list="ci-industry-options"
            className={errors?.industry ? 'border-rose-500' : ''}
          />
          <datalist id="ci-industry-options">
            {COMMON_INDUSTRIES.map((i) => (
              <option key={i} value={i} />
            ))}
          </datalist>
          {errors?.industry && <p className="text-xs text-rose-600">{errors.industry}</p>}
        </div>

        <div className="space-y-1">
          <Label htmlFor="ci-city" className="text-xs">城市</Label>
          <Input
            id="ci-city"
            value={data.city || ''}
            onChange={(e) => onChange({ city: e.target.value })}
            placeholder="例:云南省曲靖市"
          />
        </div>
      </div>
    </div>
  );
}
