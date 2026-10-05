/**
 * QuoteCoefficientEditor — 「修改本次报价系数」编辑器
 *
 * [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11]
 * 这段逻辑原本内联在 OnlineQuoteFlow.tsx 的 Step4Review 正文里(一整块 Card),
 * 服务商当面给客户演示报价时,客户直接看到售价系数、调整原因、套餐换算、关键词标准价。
 * 工单 §8 要求把它从「审核与发送」正文移走,只能从页顶控制栏进入 → 抽成本组件。
 *
 * 🔴 后端契约一字未改(工单 §9):
 *   GET  /api/quotes/:quote_id/coefficient-preview?coefficient=X
 *   POST /api/quotes/:quote_id/coefficient  { coefficient, reason, expected_snapshot_hash }
 * expected_snapshot_hash 的 CAS、adjustment reason 的审计、append-only 快照全部保留。
 *
 * 🔴 只改「当前这一份报价」。默认报价系数(QuoteMarkupCard / PUT /api/auth/profile)
 *    是另一条链,只作用于以后新生成的报价,两者不得互相覆盖(工单 §4)。
 *
 * 🔴 隐私合同(工单 §6):本组件**只在眼睛处于显示态时才被挂载**。
 *    父级 QuotePricingControlBar 在隐藏态直接不渲染它 —— 于是预览请求不会发出,
 *    敏感文字/数字/输入框也不会留在可见 DOM 里(不是 opacity / blur / visibility 遮挡)。
 */

import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Check, Loader2 } from 'lucide-react';
import api, { formatApiErrorForDisplay } from '@/lib/api';

export interface QuoteCoefficientPreview {
  old_coefficient: number;
  new_coefficient: number;
  calculation_version: string;
  summaries: Record<'entry' | 'standard' | 'flagship', { total_price: number; total_articles: number }>;
  keywords: Array<{ id: number; keyword: string; entry: number; standard: number; flagship: number }>;
  keyword_count: number;
  snapshot_hash: string;
}

export interface QuoteCoefficientEditorProps {
  /** 当前报价单 id(不可变主键)· 与 selection token 无关 */
  quoteId: number;
  /** pricingData.generated_at — 报价重算后要重新预览 */
  pricingGeneratedAt?: string | null;
  /** 保存成功后回调(刷新会话 + 由父级恢复隐藏态) */
  onSaved: () => void;
  /**
   * 忙态上报 —— 父级据此禁用「确认无误,发送报价给客户」。
   *
   * 🔴 分 kind 上报是**必须的**(返修 R1):
   *   `preview` 可随组件卸载一起作废(预览不改任何东西);
   *   `save` 绝不能跟着卸载作废 —— POST 在途时用户点眼睛隐藏 / 切标签会卸载本组件,
   *   若此时无条件上报 false,发送按钮提前解锁,**旧价格可能被发给客户**。
   *   所以 `save` 的 false 只由 POST 自己的 finally 发出(卸载后照样执行)。
   */
  onBusyChange?: (kind: 'preview' | 'save', busy: boolean) => void;
}

export default function QuoteCoefficientEditor({
  quoteId, pricingGeneratedAt, onSaved, onBusyChange,
}: QuoteCoefficientEditorProps) {
  const [quoteCoefficient, setQuoteCoefficient] = useState(1);
  const [coefficientReason, setCoefficientReason] = useState('');
  const [coefficientPreview, setCoefficientPreview] = useState<QuoteCoefficientPreview | null>(null);
  const [coefficientError, setCoefficientError] = useState('');
  const [previewingCoefficient, setPreviewingCoefficient] = useState(false);
  const [savingCoefficient, setSavingCoefficient] = useState(false);
  const coefficientInitializedQuoteId = useRef<number | null>(null);

  // 预览忙态:跟随本组件生命周期(预览可作废),卸载时清掉
  useEffect(() => {
    onBusyChange?.('preview', previewingCoefficient);
  }, [previewingCoefficient, onBusyChange]);
  useEffect(() => () => { onBusyChange?.('preview', false); }, [onBusyChange]);
  // 🔴 保存忙态**刻意不在这里跟随 state / 不在卸载时清** —— 见 onBusyChange 的注释。
  //    它只在 handleSaveQuoteCoefficient 里成对开关,由 POST 的 finally 收尾。

  useEffect(() => {
    if (!quoteId) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      setPreviewingCoefficient(true);
      try {
        const { data } = await api.get<QuoteCoefficientPreview>(
          `/api/quotes/${quoteId}/coefficient-preview`,
          { params: { coefficient: quoteCoefficient } },
        );
        if (cancelled) return;
        setCoefficientPreview(data);
        setCoefficientError('');
        if (coefficientInitializedQuoteId.current !== quoteId) {
          coefficientInitializedQuoteId.current = quoteId;
          setQuoteCoefficient(data.old_coefficient);
        }
      } catch (error: any) {
        if (cancelled) return;
        const detail = error?.response?.data?.detail;
        setCoefficientPreview(null);
        setCoefficientError(
          typeof detail === 'object' ? (detail?.message || '本次报价系数暂不可用') : (detail || error.message),
        );
      } finally {
        if (!cancelled) setPreviewingCoefficient(false);
      }
    }, 220);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [quoteId, pricingGeneratedAt, quoteCoefficient]);

  const handleSaveQuoteCoefficient = async () => {
    if (!coefficientPreview || !coefficientReason.trim()) return;
    setSavingCoefficient(true);
    // 🔴 立刻上报,不经 useEffect:本组件可能在 POST 落地前就被卸载(用户点眼睛隐藏 /
    //    切标签 / 切报价),卸载后 effect 不会再跑,只有这一对直调能保证锁真的扣上。
    onBusyChange?.('save', true);
    try {
      const { data } = await api.post(`/api/quotes/${quoteId}/coefficient`, {
        coefficient: quoteCoefficient,
        reason: coefficientReason.trim(),
        expected_snapshot_hash: coefficientPreview.snapshot_hash,
      });
      toast.success(`本次报价系数已冻结为 ${Number(data.new_coefficient).toFixed(1)} 倍`);
      setCoefficientReason('');
      onSaved();
    } catch (error: any) {
      toast.error(formatApiErrorForDisplay(error, '保存本次报价系数失败'));
    } finally {
      setSavingCoefficient(false);   // 组件已卸载时这行是空操作,不影响下面那行
      onBusyChange?.('save', false); // 无论是否已卸载都必须执行 —— 锁在这里才允许松开
    }
  };

  return (
    <div className="space-y-3" data-testid="quote-coefficient-editor">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium">本次报价系数</span>
        <Badge variant="outline">仅影响报价 #{quoteId}</Badge>
      </div>
      <div className="grid gap-3 sm:grid-cols-[160px_1fr_auto] sm:items-end">
        <label className="space-y-1 text-xs text-muted-foreground">
          <span>售价系数（1.0–5.0）</span>
          <Input
            aria-label="本次报价系数"
            type="number"
            min={1}
            max={5}
            step={0.1}
            value={quoteCoefficient}
            onChange={(event) => setQuoteCoefficient(Math.min(5, Math.max(1, Number(event.target.value) || 1)))}
          />
        </label>
        <label className="space-y-1 text-xs text-muted-foreground">
          <span>调整原因（写入审计）</span>
          <Input
            aria-label="系数调整原因"
            maxLength={300}
            value={coefficientReason}
            onChange={(event) => setCoefficientReason(event.target.value)}
            placeholder="例如：本单服务范围扩大"
          />
        </label>
        <Button
          onClick={handleSaveQuoteCoefficient}
          disabled={!coefficientPreview || !coefficientReason.trim() || savingCoefficient || previewingCoefficient}
          className="gap-2"
        >
          {savingCoefficient ? <Loader2 className="size-4 animate-spin" /> : <Check className="size-4" />}
          保存并冻结版本
        </Button>
      </div>
      {coefficientPreview ? (
        <div className="space-y-2" aria-live="polite">
          <div className="grid grid-cols-3 gap-2">
            {(['entry', 'standard', 'flagship'] as const).map((tier) => (
              <div key={tier} className="rounded-lg border bg-background/70 px-3 py-2">
                <div className="text-xs text-muted-foreground">
                  {tier === 'entry' ? '入门方案' : tier === 'standard' ? '标准方案' : '旗舰方案'}
                </div>
                <div className="font-semibold">¥{coefficientPreview.summaries[tier].total_price.toLocaleString()}</div>
                <div className="text-[11px] text-muted-foreground">
                  {coefficientPreview.summaries[tier].total_articles} 篇 · {coefficientPreview.keyword_count} 个关键词
                </div>
              </div>
            ))}
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 rounded-md bg-muted/40 px-3 py-2 text-[11px] text-muted-foreground">
            {coefficientPreview.keywords.slice(0, 6).map((keyword) => (
              <span key={keyword.id}>{keyword.keyword} · 标准 ¥{keyword.standard.toLocaleString()}</span>
            ))}
            {coefficientPreview.keyword_count > 6 && <span>另 {coefficientPreview.keyword_count - 6} 个关键词</span>}
          </div>
        </div>
      ) : (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-600">
          {previewingCoefficient ? '正在即时预览方案、关键词与总价…' : coefficientError}
        </div>
      )}
      <p className="text-[11px] text-muted-foreground">
        只缩放现有客户售价，不改底层成本；发送客户时会再冻结一份只读快照。
      </p>
    </div>
  );
}
