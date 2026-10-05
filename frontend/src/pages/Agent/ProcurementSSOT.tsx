/**
 * 进货库存(双 SSOT 新版)
 *
 * 服务商向平台进货,购买后进入自己的可售库存。价格由后端
 * /api/pricing/procurement/catalog 提供,浏览器绝不算价。
 *
 * 关键约束:可售库存(paid_inventory_points)与赠送算力(bonus_inventory_points)
 * 必须分两行独立展示,永不相加。
 *
 * flag 关(503 SSOT_DISABLED)→ 提示回退旧版进货页。
 */
import { useEffect, useState, useCallback } from 'react';
import { Loader2, Check, PackagePlus } from 'lucide-react';
import { toast } from 'sonner';
import { cn, formatDateTime } from '@/lib/utils';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import {
  usePricingSSOT,
  type ProcurementCatalog,
  type ProcurementCatalogItem,
  type ProcurementQuote,
  type PricingErrorCode,
} from '@/hooks/usePricingSSOT';

type LoadState =
  | { phase: 'loading' }
  | { phase: 'ready'; catalog: ProcurementCatalog }
  | { phase: 'error'; code: PricingErrorCode; message: string };

export default function ProcurementSSOT() {
  const { getProcurementCatalog, createProcurementQuote, formatCents, formatPoints } = usePricingSSOT();

  const [state, setState] = useState<LoadState>({ phase: 'loading' });
  const [selected, setSelected] = useState<string | null>(null);
  const [quoting, setQuoting] = useState(false);
  const [quote, setQuote] = useState<ProcurementQuote | null>(null);
  const [quoteItem, setQuoteItem] = useState<ProcurementCatalogItem | null>(null);

  const load = useCallback(async () => {
    setState({ phase: 'loading' });
    const res = await getProcurementCatalog();
    if (res.ok) {
      setState({ phase: 'ready', catalog: res.data });
    } else {
      setState({ phase: 'error', code: res.code, message: res.message });
    }
  }, [getProcurementCatalog]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleProcure = useCallback(
    async (item: ProcurementCatalogItem) => {
      setQuoting(true);
      const res = await createProcurementQuote(item.product_code, 1);
      setQuoting(false);
      if (res.ok) {
        setQuote(res.data);
        setQuoteItem(item);
      } else if (res.code === 'SSOT_DISABLED') {
        toast.info('新版进货页未启用,请使用旧版进货页');
      } else {
        toast.error(res.message || '生成进货报价失败,请稍后再试');
      }
    },
    [createProcurementQuote],
  );

  // ---------- flag 关 → 交回旧版 ----------
  if (state.phase === 'error' && state.code === 'SSOT_DISABLED') {
    return (
      <div className="p-6">
        <div className="mx-auto max-w-md rounded-xl border border-border bg-card p-6 text-center">
          <p className="text-base font-medium text-foreground">新版进货页未启用</p>
          <p className="mt-2 text-sm text-muted-foreground">请使用现有进货库存页面完成进货。</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 p-3 sm:p-4 md:p-6 pb-24 lg:pb-6">
      {/* 标题 */}
      <div className="flex items-center gap-3">
        <PackagePlus className="size-6 text-foreground" />
        <h1 className="text-2xl font-bold text-foreground">进货库存</h1>
      </div>

      {/* 平台服务说明 */}
      {state.phase === 'ready' && (
        <div className="rounded-xl border border-border bg-muted/40 p-4 space-y-1.5">
          <p className="text-sm text-foreground">
            由 OmniRank 平台提供进货、收款与售后服务，购买后进入你的可售库存。
          </p>
          <p className="text-xs text-muted-foreground leading-relaxed">
            价格已是最终进货价,进货后可售库存与赠送算力分别入账。
          </p>
        </div>
      )}

      {/* 加载态 */}
      {state.phase === 'loading' && (
        <div className="flex items-center justify-center py-24 text-muted-foreground">
          <Loader2 className="size-5 animate-spin" />
          <span className="ml-2 text-sm">正在加载进货价格…</span>
        </div>
      )}

      {/* 其他错误 */}
      {state.phase === 'error' && state.code !== 'SSOT_DISABLED' && (
        <div className="rounded-xl border border-border bg-card p-6 text-center">
          <p className="text-sm text-muted-foreground">{state.message}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => void load()}>
            重试
          </Button>
        </div>
      )}

      {/* 目录卡片 */}
      {state.phase === 'ready' && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {state.catalog.items.map((item) => {
            const isSelected = selected === item.product_code;
            return (
              <Card
                key={item.product_code}
                className={cn(
                  'relative cursor-pointer transition-all duration-200',
                  isSelected ? 'border-foreground ring-1 ring-foreground/20' : 'hover:border-foreground/40',
                )}
                onClick={() => setSelected(item.product_code)}
              >
                <CardContent className="p-4">
                  {isSelected && (
                    <div className="absolute right-3 top-3 flex size-5 items-center justify-center rounded-full bg-foreground">
                      <Check className="size-3 text-background" />
                    </div>
                  )}

                  <p className="text-base font-semibold text-foreground">{item.display_name}</p>

                  <div className="my-3">
                    <span className="text-2xl font-bold text-foreground">
                      {formatCents(item.cash_price_cents)}
                    </span>
                  </div>

                  {/* 可售库存 与 赠送算力 — 严格分两行,绝不相加 */}
                  <div className="space-y-1 text-sm">
                    <div className="flex justify-between text-muted-foreground">
                      <span>可售库存</span>
                      <span className="tabular-nums text-foreground">
                        {formatPoints(item.paid_inventory_points)}
                      </span>
                    </div>
                    <div className="flex justify-between text-muted-foreground">
                      <span>赠送算力</span>
                      <span className="tabular-nums text-amber-500">
                        {formatPoints(item.bonus_inventory_points)}
                      </span>
                    </div>
                  </div>

                  <Button
                    className="mt-4 w-full bg-foreground text-background hover:bg-foreground/90"
                    disabled={quoting}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelected(item.product_code);
                      void handleProcure(item);
                    }}
                  >
                    {quoting && selected === item.product_code ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      '确认进货'
                    )}
                  </Button>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* 进货报价确认弹窗 */}
      <Dialog
        open={!!quote}
        onOpenChange={(open) => {
          if (!open) {
            setQuote(null);
            setQuoteItem(null);
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>确认进货</DialogTitle>
          </DialogHeader>

          {quote && (
            <div className="space-y-3 py-2">
              {quoteItem && (
                <p className="text-base font-semibold text-foreground">{quoteItem.display_name}</p>
              )}
              <div className="rounded-lg border border-border bg-muted/30 p-4 space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">进货金额</span>
                  <span className="text-lg font-bold text-foreground">{formatCents(quote.cash_price_cents)}</span>
                </div>
                {/* 分两行 · 绝不相加 */}
                <div className="flex justify-between">
                  <span className="text-muted-foreground">可售库存</span>
                  <span className="tabular-nums text-foreground">
                    {formatPoints(quote.paid_inventory_points)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">赠送算力</span>
                  <span className="tabular-nums text-amber-500">
                    {formatPoints(quote.bonus_inventory_points)}
                  </span>
                </div>
                <p className="border-t border-border/50 pt-2 text-xs text-muted-foreground">
                  由 OmniRank 平台提供服务
                </p>
              </div>
              <p className="text-xs text-muted-foreground">
                报价有效期至 {formatDateTime(quote.price_valid_until)} · 报价单号 {quote.quote_id}
              </p>
            </div>
          )}

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setQuote(null);
                setQuoteItem(null);
              }}
            >
              取消
            </Button>
            <Button
              className="bg-foreground text-background hover:bg-foreground/90"
              onClick={() => {
                toast.info('进货报价已生成,请在支付页完成付款');
              }}
            >
              去支付
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
