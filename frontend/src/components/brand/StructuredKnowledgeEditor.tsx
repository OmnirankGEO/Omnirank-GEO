/**
 * StructuredKnowledgeEditor — 5 维度业务知识嵌套编辑器
 *
 * 老版 pages/Brands/MarketingTab.tsx 有这个能力（~200 行嵌套表单），新版 BrandDetailPage 缺。
 * CTO-13.0 2026-04-19 S2.1 · 抽成独立组件复用挂载到 IP 人设 Tab 底部。
 *
 * 数据结构（对应 client_profiles.structured_knowledge JSONB 列）：
 *   products        产品/服务（name/features[]/scenarios[]/metrics）
 *   painPoints      痛点（scenario/emotions[]/consequences/triggers）
 *   customers       客户（segments[]/needs[]/barriers[]/concerns[]）
 *   differentiation 差异化（competitors[]/advantages[]/usp/killerData[]）
 *   cases[]         案例数组（client/background/solution/results/quote）
 *
 * 使用：
 *   <StructuredKnowledgeEditor
 *     value={structuredKnowledge}
 *     onChange={setStructuredKnowledge}
 *     aiFilled={aiFilledFields.has('structured_knowledge')}
 *   />
 */
import { useState, useEffect, useId } from 'react';
import { useDirtyForm } from '@/hooks/useDirtyForm';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  ChevronDown, Package, AlertTriangle, Users, Award, BookMarked,
  Plus, X, Sparkles,
} from 'lucide-react';
import { cn } from '@/lib/utils';

// ========== 类型 ==========

export interface StructuredKnowledge {
  products?: {
    name?: string;
    features?: string[];
    scenarios?: string[];
    metrics?: string;
  };
  painPoints?: {
    scenario?: string;
    emotions?: string[];
    consequences?: string;
    triggers?: string;
  };
  customers?: {
    segments?: string[];
    needs?: string[];
    barriers?: string[];
    concerns?: string[];
  };
  differentiation?: {
    competitors?: string[];
    advantages?: string[];
    usp?: string;
    killerData?: string[];
  };
  cases?: Array<{
    client?: string;
    background?: string;
    solution?: string;
    results?: string;
    quote?: string;
  }>;
}

interface Props {
  value: StructuredKnowledge | null;
  onChange: (value: StructuredKnowledge) => void;
  aiFilled?: boolean;
  /** 只读模式（C 端用户可能不需要编辑这些）· 默认 false */
  readonly?: boolean;
}

// ========== 小工具：TagList ==========

function TagList({
  values,
  onChange,
  placeholder,
  disabled,
}: {
  values?: string[];
  onChange: (v: string[]) => void;
  placeholder: string;
  disabled?: boolean;
}) {
  const [input, setInput] = useState('');

  // [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16 ①] 客户资料编辑接脏表单守卫。
  //   这里是"打了一半的一条知识点"——还没回车提交,静默刷新一到就没了。
  //   🔴 id 用 useId 生成:本组件没有稳定的 props 标识,而同一页会渲染多个 TagList。
  //   注册表按 id 去重,写死同一个 id 会让后挂载的那个把先挂载的探针顶掉 ——
  //   先挂的那份输入**失去保护**,而表面上看「已经接入了」。
  const __dirtyId = useId();
  useDirtyForm(`brand.knowledge.${__dirtyId}`, () => input.trim().length > 0);
  const arr = Array.isArray(values) ? values : [];
  const add = () => {
    const v = input.trim();
    if (!v || arr.includes(v)) return;
    onChange([...arr, v]);
    setInput('');
  };
  const remove = (idx: number) => onChange(arr.filter((_, i) => i !== idx));

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap gap-1.5">
        {arr.map((tag, i) => (
          <Badge key={`${tag}-${i}`} variant="secondary" className="gap-1 pr-1 text-[11px]">
            <span>{tag}</span>
            {!disabled && (
              <button
                type="button"
                onClick={() => remove(i)}
                className="ml-0.5 hover:text-destructive transition-colors"
              >
                <X className="h-2.5 w-2.5" />
              </button>
            )}
          </Badge>
        ))}
      </div>
      {!disabled && (
        <div className="flex gap-1.5">
          <Input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                e.preventDefault();
                add();
              }
            }}
            placeholder={placeholder}
            className="h-8 text-xs"
          />
          <Button type="button" size="sm" variant="outline" onClick={add} className="h-8 text-xs">
            添加
          </Button>
        </div>
      )}
    </div>
  );
}

// ========== 维度子组件（折叠分组） ==========

function DimensionBlock({
  icon: Icon,
  title,
  defaultOpen,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  defaultOpen: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-lg border bg-card/40">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 p-3 text-left hover:bg-accent/30 transition-colors"
      >
        <Icon className="h-4 w-4 text-muted-foreground shrink-0" />
        <span className="text-sm font-medium flex-1">{title}</span>
        <ChevronDown className={cn('h-4 w-4 text-muted-foreground transition-transform', open ? '' : '-rotate-90')} />
      </button>
      {open && <div className="p-3 pt-0 space-y-2">{children}</div>}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <label className="text-[10px] text-muted-foreground tracking-wide uppercase">{label}</label>
      {children}
    </div>
  );
}

// ========== 主组件 ==========

export function StructuredKnowledgeEditor({ value, onChange, aiFilled, readonly }: Props) {
  const v: StructuredKnowledge = value || {};
  const hasAny = !!(
    v.products?.name || (v.products?.features?.length) ||
    v.painPoints?.scenario ||
    (v.customers?.segments?.length) ||
    v.differentiation?.usp ||
    (v.cases?.length)
  );
  const [open, setOpen] = useState(hasAny || !!aiFilled);

  useEffect(() => {
    if (hasAny || aiFilled) setOpen(true);
  }, [hasAny, aiFilled]);

  // 辅助 setter：只改一个维度
  const setProducts = (p: StructuredKnowledge['products']) => onChange({ ...v, products: p });
  const setPainPoints = (p: StructuredKnowledge['painPoints']) => onChange({ ...v, painPoints: p });
  const setCustomers = (c: StructuredKnowledge['customers']) => onChange({ ...v, customers: c });
  const setDifferentiation = (d: StructuredKnowledge['differentiation']) => onChange({ ...v, differentiation: d });
  const setCases = (c: StructuredKnowledge['cases']) => onChange({ ...v, cases: c });

  const addCase = () => {
    setCases([...(v.cases || []), { client: '', background: '', solution: '', results: '', quote: '' }]);
  };
  const removeCase = (idx: number) => {
    setCases((v.cases || []).filter((_, i) => i !== idx));
  };

  return (
    <div className={cn(
      'rounded-lg border border-dashed border-border/60 bg-muted/10 p-3 space-y-2',
      aiFilled && 'ring-1 ring-amber-500/40 bg-amber-500/5',
    )}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 text-left hover:bg-accent/30 rounded px-1 py-0.5 transition-colors"
      >
        <Sparkles className={cn('h-3.5 w-3.5 shrink-0', aiFilled ? 'text-amber-400' : 'text-muted-foreground')} />
        <div className="flex-1 min-w-0">
          <div className={cn('text-xs font-medium flex items-center gap-1', aiFilled && 'text-amber-400')}>
            5 维度业务画像
            {aiFilled && <span className="text-[9px] px-1 py-px rounded bg-amber-500/20 font-normal">✨ AI 填充 · 请检查</span>}
          </div>
          <div className="text-[10px] text-muted-foreground">
            AI 顾问版会深度提取 · 用于写文章、脚本、报价时让 AI 更懂你的产品/客户/差异化
          </div>
        </div>
        <ChevronDown className={cn('h-3.5 w-3.5 text-muted-foreground shrink-0 transition-transform', open ? '' : '-rotate-90')} />
      </button>

      {open && (
        <div className="space-y-2 pt-1">
          {/* 产品/服务 */}
          <DimensionBlock icon={Package} title="产品 / 服务" defaultOpen={!!v.products?.name}>
            <Field label="产品名">
              <Input
                value={v.products?.name || ''}
                onChange={e => setProducts({ ...v.products, name: e.target.value })}
                placeholder="核心产品/服务名称"
                disabled={readonly}
              />
            </Field>
            <Field label="核心特性">
              <TagList
                values={v.products?.features}
                onChange={(arr) => setProducts({ ...v.products, features: arr })}
                placeholder="特性，回车添加"
                disabled={readonly}
              />
            </Field>
            <Field label="使用场景">
              <TagList
                values={v.products?.scenarios}
                onChange={(arr) => setProducts({ ...v.products, scenarios: arr })}
                placeholder="场景，回车添加"
                disabled={readonly}
              />
            </Field>
            <Field label="效果数据">
              <Input
                value={v.products?.metrics || ''}
                onChange={e => setProducts({ ...v.products, metrics: e.target.value })}
                placeholder="如：复购率 42% · ROI 1:5"
                disabled={readonly}
              />
            </Field>
          </DimensionBlock>

          {/* 痛点 */}
          <DimensionBlock icon={AlertTriangle} title="客户痛点" defaultOpen={!!v.painPoints?.scenario}>
            <Field label="典型场景">
              <textarea
                value={v.painPoints?.scenario || ''}
                onChange={e => setPainPoints({ ...v.painPoints, scenario: e.target.value })}
                placeholder="客户遇到什么问题、在什么情境下"
                rows={2}
                className="w-full rounded-lg border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring resize-none"
                disabled={readonly}
              />
            </Field>
            <Field label="情绪">
              <TagList
                values={v.painPoints?.emotions}
                onChange={(arr) => setPainPoints({ ...v.painPoints, emotions: arr })}
                placeholder="如：焦虑 / 迷茫"
                disabled={readonly}
              />
            </Field>
            <Field label="不解决会怎样">
              <Input
                value={v.painPoints?.consequences || ''}
                onChange={e => setPainPoints({ ...v.painPoints, consequences: e.target.value })}
                placeholder="后果"
                disabled={readonly}
              />
            </Field>
            <Field label="购买触发时机">
              <Input
                value={v.painPoints?.triggers || ''}
                onChange={e => setPainPoints({ ...v.painPoints, triggers: e.target.value })}
                placeholder="什么节点用户会开始考虑买"
                disabled={readonly}
              />
            </Field>
          </DimensionBlock>

          {/* 客户 */}
          <DimensionBlock icon={Users} title="目标客户" defaultOpen={!!(v.customers?.segments?.length)}>
            <Field label="客户群体">
              <TagList
                values={v.customers?.segments}
                onChange={(arr) => setCustomers({ ...v.customers, segments: arr })}
                placeholder="如：25-35 岁职场妈妈"
                disabled={readonly}
              />
            </Field>
            <Field label="核心需求">
              <TagList
                values={v.customers?.needs}
                onChange={(arr) => setCustomers({ ...v.customers, needs: arr })}
                placeholder="回车添加"
                disabled={readonly}
              />
            </Field>
            <Field label="决策障碍">
              <TagList
                values={v.customers?.barriers}
                onChange={(arr) => setCustomers({ ...v.customers, barriers: arr })}
                placeholder="如：价格敏感 / 怕踩坑"
                disabled={readonly}
              />
            </Field>
            <Field label="最关心的问题">
              <TagList
                values={v.customers?.concerns}
                onChange={(arr) => setCustomers({ ...v.customers, concerns: arr })}
                placeholder="回车添加"
                disabled={readonly}
              />
            </Field>
          </DimensionBlock>

          {/* 差异化 */}
          <DimensionBlock icon={Award} title="差异化优势" defaultOpen={!!v.differentiation?.usp}>
            <Field label="核心竞品">
              <TagList
                values={v.differentiation?.competitors}
                onChange={(arr) => setDifferentiation({ ...v.differentiation, competitors: arr })}
                placeholder="竞品名，回车添加"
                disabled={readonly}
              />
            </Field>
            <Field label="你的优势">
              <TagList
                values={v.differentiation?.advantages}
                onChange={(arr) => setDifferentiation({ ...v.differentiation, advantages: arr })}
                placeholder="优势点"
                disabled={readonly}
              />
            </Field>
            <Field label="你的独特卖点">
              <Input
                value={v.differentiation?.usp || ''}
                onChange={e => setDifferentiation({ ...v.differentiation, usp: e.target.value })}
                placeholder="一句话说清你和别人的最核心区别"
                disabled={readonly}
              />
            </Field>
            <Field label="关键数据点">
              <TagList
                values={v.differentiation?.killerData}
                onChange={(arr) => setDifferentiation({ ...v.differentiation, killerData: arr })}
                placeholder="如：独家方法论 3 年验证"
                disabled={readonly}
              />
            </Field>
          </DimensionBlock>

          {/* 案例 */}
          <DimensionBlock icon={BookMarked} title={`客户案例（${v.cases?.length || 0} 条）`} defaultOpen={!!(v.cases?.length)}>
            {(v.cases || []).map((c, idx) => (
              <div key={idx} className="relative rounded-lg border bg-background p-2.5 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-medium text-muted-foreground">案例 #{idx + 1}</span>
                  {!readonly && (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => removeCase(idx)}
                      className="h-6 px-2 text-xs text-destructive hover:text-destructive"
                    >
                      <X className="h-3 w-3" /> 删
                    </Button>
                  )}
                </div>
                <Field label="客户名">
                  <Input
                    value={c.client || ''}
                    onChange={e => setCases((v.cases || []).map((x, i) => i === idx ? { ...x, client: e.target.value } : x))}
                    placeholder="客户名称 / 行业"
                    disabled={readonly}
                  />
                </Field>
                <Field label="问题背景">
                  <Input
                    value={c.background || ''}
                    onChange={e => setCases((v.cases || []).map((x, i) => i === idx ? { ...x, background: e.target.value } : x))}
                    placeholder="客户之前遇到什么问题"
                    disabled={readonly}
                  />
                </Field>
                <Field label="解决方案">
                  <Input
                    value={c.solution || ''}
                    onChange={e => setCases((v.cases || []).map((x, i) => i === idx ? { ...x, solution: e.target.value } : x))}
                    placeholder="你怎么帮他解决的"
                    disabled={readonly}
                  />
                </Field>
                <Field label="效果数据">
                  <Input
                    value={c.results || ''}
                    onChange={e => setCases((v.cases || []).map((x, i) => i === idx ? { ...x, results: e.target.value } : x))}
                    placeholder="如：3 个月复购率从 18% 升到 42%"
                    disabled={readonly}
                  />
                </Field>
                <Field label="客户原话">
                  <Input
                    value={c.quote || ''}
                    onChange={e => setCases((v.cases || []).map((x, i) => i === idx ? { ...x, quote: e.target.value } : x))}
                    placeholder="客户评价"
                    disabled={readonly}
                  />
                </Field>
              </div>
            ))}
            {!readonly && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={addCase}
                className="w-full h-8 text-xs"
              >
                <Plus className="h-3 w-3 mr-1" /> 添加案例
              </Button>
            )}
          </DimensionBlock>
        </div>
      )}
    </div>
  );
}

export default StructuredKnowledgeEditor;
