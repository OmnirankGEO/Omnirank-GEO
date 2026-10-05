/**
 * QuoteMarkupCard — 「默认报价设置」面板体
 *
 * 2026-06-08 定价权根治(Option B):
 *   - 所有登录用户(含被邀请普通用户)签《报价定价免责协议》后,可在此自设【关键词报价系数】+【单篇成本】。
 *   - 未签普通用户:显示免责协议 + 同意按钮(签了就开通·不需要实名);签后开放设置。
 *   - 服务商 / admin:直接可设(走各自既有口径)。平台强制系数(admin override)时只读。
 *
 * [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11] 隐私模式改造:
 *   🔴 本组件不再自己管"显示/隐藏"。原来的实现有三个洞(工单 §2):
 *      ① 显示态存 `localStorage: omnirank_pricing_reveal` → 刷新/重开浏览器仍是公开态;
 *      ② 隐藏时只把数值换成 `••`,「默认报价系数」「毛利」「单篇成本」「平台设定」
 *         这些**字段名**照旧渲染;
 *      ③ 只读支路里的系数值也只遮数字不遮标签。
 *   现在:眼睛与遮罩由 QuotePricingControlBar 统一持有(零持久化 · 默认隐藏),
 *   隐藏态**根本不挂载本组件** —— 于是敏感文字/数字/输入框不在可见 DOM 里,
 *   也不会发出 `/api/auth/quote-markup-preference` 之外的任何预加载。
 *   ⇒ 本文件里**不许**再出现 localStorage 的显示态持久化(会把 ① 改回来)。
 *
 * 🔴 作用域(工单 §4):这里设的是**默认**报价系数,只影响以后新生成的报价。
 *    改当前这一份报价走 QuoteCoefficientEditor(POST /api/quotes/:id/coefficient),两条链不得互相覆盖。
 *
 * 后端 SSOT:
 *   GET  /api/auth/quote-markup-preference  → {effective_ratio, source, can_edit, needs_pricing_disclaimer, ...}
 *   PUT  /api/auth/profile {quote_markup_ratio}     → 存系数(后端按签约/归属判定放行)
 *   GET/PUT /api/agent/pricing/cost-per-article      → 读/存单篇成本(所有登录用户·WHERE id=self)
 *   POST /api/auth/pricing-disclaimer/sign           → 签署免责协议
 *
 * 客户侧红线:绝不暴露内部结算主体 / wholesale / 批发价 / 内部成本明细。
 */

import { useState, useEffect, useCallback } from 'react';
import { authFetch } from '@/lib/api';
import { useIsMounted } from '@/hooks/useIsMounted';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Save, Loader2, Info, Lock, AlertTriangle, ShieldCheck, Coins,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  PRICING_DISCLAIMER_TITLE, PRICING_DISCLAIMER_INTRO,
  PRICING_DISCLAIMER_SECTIONS, PRICING_DISCLAIMER_CONFIRM,
} from '@/lib/pricingDisclaimer';

// [报价中心·2026-06-08] 报价系数永远用本人·绝不继承上级 → 无 'inherited' 来源
type MarkupSource = 'admin_override' | 'self' | 'default';

interface QuoteMarkupPreference {
  effective_ratio: number;
  own_ratio: number | null;
  admin_override: number | null;
  source: MarkupSource;
  can_edit: boolean;
  needs_pricing_disclaimer: boolean;
  margin_pct: number;
}

const SOURCE_BADGE: Record<MarkupSource, { label: string; cls: string }> = {
  self: { label: '你设定的', cls: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' },
  admin_override: { label: '平台设定', cls: 'bg-violet-500/10 text-violet-600 dark:text-violet-400' },
  default: { label: '系统默认', cls: 'bg-secondary text-muted-foreground' },
};

export default function QuoteMarkupCard() {
  const isMounted = useIsMounted();
  const [pref, setPref] = useState<QuoteMarkupPreference | null>(null);
  const [cost, setCost] = useState<number | null>(null);
  const [systemDefaultCost, setSystemDefaultCost] = useState<number>(60);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [costValue, setCostValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [costSaving, setCostSaving] = useState(false);
  const [savedHint, setSavedHint] = useState(false);
  const [signing, setSigning] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      const res = await authFetch('/api/auth/quote-markup-preference');
      if (!isMounted()) return;
      if (!res.ok) { setError(true); return; }
      const d: QuoteMarkupPreference = await res.json();
      if (!isMounted()) return;
      setPref(d);
      setEditValue(String(d.effective_ratio ?? 1));
      // 成本(所有登录用户可读自己的;失败/未设忽略)
      try {
        const cres = await authFetch('/api/agent/pricing/cost-per-article');
        if (cres.ok && isMounted()) {
          const cd = await cres.json();
          setCost(cd.cost_per_article ?? null);
          if (typeof cd.system_default === 'number') setSystemDefaultCost(cd.system_default);
          setCostValue(cd.cost_per_article != null ? String(cd.cost_per_article) : '');
        }
      } catch { /* 成本读取失败忽略 */ }
    } catch {
      if (isMounted()) setError(true);
    } finally {
      if (isMounted()) setLoading(false);
    }
  }, [isMounted]);

  useEffect(() => { load(); }, [load]);

  const handleSave = async () => {
    if (!pref) return;
    setSaving(true);
    try {
      const res = await authFetch('/api/auth/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quote_markup_ratio: editValue }),
      });
      if (!res.ok) {
        if (res.status === 403) toast.error('当前报价规则由平台设定,暂不可修改');
        else { const e = await res.json().catch(() => ({} as any)); toast.error(e?.detail?.message || e?.detail || '保存失败,请稍后重试'); }
        return;
      }
      const d = await res.json();
      if (d.quote_markup_ignored) {
        toast.error(d.message || '请先签署《报价定价免责协议》后再设置');
        await load();
        return;
      }
      if (d.success) {
        setSavedHint(true);
        setTimeout(() => { if (isMounted()) setSavedHint(false); }, 2000);
        await load();
      } else {
        toast.error(d.message || '保存失败,请稍后重试');
      }
    } catch (e) {
      console.error('保存报价系数失败:', e);
      toast.error('保存失败,请检查网络后重试');
    } finally {
      if (isMounted()) setSaving(false);
    }
  };

  const handleSaveCost = async () => {
    setCostSaving(true);
    try {
      const v = costValue.trim() === '' ? null : Number(costValue);
      const res = await authFetch('/api/agent/pricing/cost-per-article', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cost: v }),
      });
      if (!res.ok) {
        if (res.status === 403) toast.error('请先签署《报价定价免责协议》后再设置成本');
        else { const e = await res.json().catch(() => ({} as any)); toast.error(e?.detail || '保存失败,请稍后重试'); }
        return;
      }
      setSavedHint(true);
      setTimeout(() => { if (isMounted()) setSavedHint(false); }, 2000);
      await load();
    } catch (e) {
      console.error('保存单篇成本失败:', e);
      toast.error('保存失败,请检查网络后重试');
    } finally {
      if (isMounted()) setCostSaving(false);
    }
  };

  const handleSign = async () => {
    setSigning(true);
    try {
      const res = await authFetch('/api/auth/pricing-disclaimer/sign', { method: 'POST' });
      if (res.ok) { toast.success('已开通自定义报价'); await load(); }
      else toast.error('签署失败,请稍后重试');
    } catch (e) {
      console.error('签署免责协议失败:', e);
      toast.error('签署失败,请检查网络后重试');
    } finally {
      if (isMounted()) setSigning(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground" data-testid="quote-markup-loading">
        <Loader2 className="size-3.5 animate-spin" /> 加载报价定价设置…
      </div>
    );
  }

  if (error || !pref) {
    return (
      <div className="flex items-center justify-between gap-2 text-sm text-muted-foreground">
        <span className="flex items-center gap-2">
          <Info className="size-3.5" /> 报价定价设置暂时读取失败
        </span>
        <Button size="xs" variant="ghost" onClick={load}>重试</Button>
      </div>
    );
  }

  const ratio = Number.isFinite(pref.effective_ratio) ? pref.effective_ratio : 1;
  const badge = SOURCE_BADGE[pref.source] ?? SOURCE_BADGE.default;

  const editNum = Number(editValue);
  const editRatio = Number.isFinite(editNum) ? Math.min(5, Math.max(1, editNum)) : 1;
  const editMarginPct = editRatio > 1 ? Math.round(((editRatio - 1) / editRatio) * 100) : 0;

  return (
    <div data-testid="quote-markup-panel">
      {/* 当前生效值 —— 只在显示态存在(父级隐藏时整个组件不挂载) */}
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">默认报价系数 · {ratio.toFixed(1)} 倍</span>
        <span className="text-xs text-muted-foreground">· 毛利约 {pref.margin_pct}%</span>
        <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${badge.cls}`}>{badge.label}</span>
        {savedHint && <span className="text-[11px] text-emerald-500">已保存</span>}
      </div>

      {pref.needs_pricing_disclaimer ? (
        /* 未签普通用户:内联免责协议门 */
        <div className="space-y-3">
          <p className="flex items-start gap-1.5 text-xs text-muted-foreground leading-relaxed">
            <ShieldCheck className="size-3.5 shrink-0 mt-0.5 text-sky-500" />
            想自己定价、给客户报价?先读一遍下面的《{PRICING_DISCLAIMER_TITLE}》并同意即可开通——签了就开,不需要实名。
          </p>
          <div className="max-h-56 overflow-y-auto rounded-md border border-border bg-secondary/30 px-3 py-2 text-xs leading-relaxed text-muted-foreground space-y-2">
            <p className="font-semibold text-foreground">{PRICING_DISCLAIMER_TITLE}</p>
            <p>{PRICING_DISCLAIMER_INTRO}</p>
            {PRICING_DISCLAIMER_SECTIONS.map((sec) => (
              <div key={sec.heading} className="space-y-0.5">
                <p className="font-medium text-foreground/90">{sec.heading}</p>
                <ul className="list-disc pl-4 space-y-0.5">
                  {sec.points.map((pt, i) => <li key={i}>{pt}</li>)}
                </ul>
              </div>
            ))}
            <p className="font-medium text-foreground/90 pt-1">{PRICING_DISCLAIMER_CONFIRM}</p>
          </div>
          <Button size="sm" onClick={handleSign} disabled={signing} className="w-full">
            {signing ? <Loader2 className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />}
            我已阅读并同意 · 开通设价
          </Button>
        </div>
      ) : pref.can_edit ? (
        /* 可编辑:系数 + 成本(就近聚合) */
        <div className="space-y-4">
          {/* 关键词报价系数 */}
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <label className="text-sm text-muted-foreground">关键词报价系数</label>
              <Input
                type="number" min={1} max={5} step={0.1}
                value={editValue}
                onChange={e => setEditValue(e.target.value)}
                className="h-8 max-w-[120px] text-sm"
                disabled={saving}
              />
              <span className="text-sm text-muted-foreground">倍</span>
              <span className="text-xs text-muted-foreground">
                这个系数下毛利约
                <span className={`ml-1 font-semibold ${editRatio <= 1 ? 'text-amber-500' : 'text-emerald-500'}`}>
                  {editMarginPct}%
                </span>
              </span>
              <Button size="sm" onClick={handleSave} disabled={saving} className="ml-auto">
                {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
                保存
              </Button>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              给客户生成新<strong className="text-foreground">关键词报价</strong>时的默认售价系数（售价 = 成本 × 系数），仅你可见。只影响以后新生成的关键词报价,不影响算力包售价,也不改已经生成的这一份报价。
            </p>
            {editRatio <= 1 && (
              <p className="flex items-start gap-1.5 text-xs text-amber-600 dark:text-amber-400 leading-relaxed">
                <AlertTriangle className="size-3.5 shrink-0 mt-0.5" />
                1.0 倍 = 按成本价给客户报价,不额外加价;想赚服务利润,请调到 1.1 倍以上。
              </p>
            )}
          </div>

          {/* 单篇成本 */}
          <div className="space-y-2 border-t border-border/60 pt-3">
            <div className="flex flex-wrap items-center gap-2">
              <Coins className="size-3.5 text-muted-foreground shrink-0" />
              <label className="text-sm text-muted-foreground">单篇内容成本</label>
              <Input
                type="number" min={0} step={1}
                value={costValue}
                onChange={e => setCostValue(e.target.value)}
                placeholder={`留空 = 系统自动估(约 ¥${systemDefaultCost}）`}
                className="h-8 max-w-[180px] text-sm"
                disabled={costSaving}
              />
              <span className="text-sm text-muted-foreground">元 / 篇</span>
              <Button size="sm" variant="outline" onClick={handleSaveCost} disabled={costSaving} className="ml-auto">
                {costSaving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
                保存
              </Button>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              按你做不同行业客户的真实成本填（比如医疗、央媒类成本高就填高一点;留空就用系统自动估）。算报价时按这个成本 × 系数。
              {cost != null && <span className="ml-1 text-foreground">当前:¥{cost} / 篇</span>}
            </p>
          </div>
        </div>
      ) : (
        /* 只读 = 平台强制系数(admin_override)· 不展示"签了就能改"误导 */
        <div className="space-y-1.5">
          <div className="flex items-center gap-2 text-sm">
            <Lock className="size-3.5 text-muted-foreground shrink-0" />
            <span className="text-muted-foreground">
              报价系数 <span className="font-semibold text-foreground">{ratio.toFixed(1)} 倍</span>
            </span>
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            平台已为你设定报价规则,当前暂不可自改。
          </p>
        </div>
      )}
    </div>
  );
}
