/**
 * 购买算力(双 SSOT 新版 · §8.1 布局)
 *
 * 只渲染后端 /api/pricing/retail/catalog 返回的价格与积分,浏览器绝不算价。
 * flag 关(503 SSOT_DISABLED)→ 渲染"新版未启用"提示,由调用方回退旧版购买页。
 * 不展示任何工程字段(base_price_cents / multiplier / cost / SKU / 卖家实名),
 * 只展示 display_name / 价格 / 算力 / 用法示例。
 */
import { useEffect, useState, useCallback } from 'react';
import { Loader2, Check, Sparkles, ShoppingCart } from 'lucide-react';
import { toast } from 'sonner';
import { cn, formatDateTime } from '@/lib/utils';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import {
  usePricingSSOT,
  type RetailCatalog,
  type RetailCatalogItem,
  type RetailQuote,
  type PricingErrorCode,
} from '@/hooks/usePricingSSOT';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

type LoadState =
  | { phase: 'loading' }
  | { phase: 'ready'; catalog: RetailCatalog }
  | { phase: 'error'; code: PricingErrorCode; message: string };

export default function BuyCreditsSSOT() {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const { getRetailCatalog, createRetailQuote, formatCents, formatPoints } = usePricingSSOT();

  const [state, setState] = useState<LoadState>({ phase: 'loading' });
  const [selected, setSelected] = useState<string | null>(null);
  const [quoting, setQuoting] = useState(false);
  const [quote, setQuote] = useState<RetailQuote | null>(null);
  const [quoteItem, setQuoteItem] = useState<RetailCatalogItem | null>(null);

  const load = useCallback(async () => {
    setState({ phase: 'loading' });
    const res = await getRetailCatalog();
    if (res.ok) {
      setState({ phase: 'ready', catalog: res.data });
    } else {
      setState({ phase: 'error', code: res.code, message: res.message });
    }
  }, [getRetailCatalog]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleBuy = useCallback(
    async (item: RetailCatalogItem) => {
      const notice = state.phase === 'ready' ? state.catalog.digital_goods_notice : undefined;
      if (notice && !(await askConfirm({ title: `购买前请确认`, description: `${notice}\n\n点击“确定”后生成报价。` }))) {
        return;
      }
      setQuoting(true);
      const res = await createRetailQuote(item.product_code, 1, undefined, Boolean(notice));
      setQuoting(false);
      if (res.ok) {
        setQuote(res.data);
        setQuoteItem(item);
      } else if (res.code === 'SSOT_DISABLED') {
        toast.info('新版购买页未启用,请使用旧版购买页');
      } else {
        toast.error(res.message || '生成报价失败,请稍后再试');
      }
    },
    [createRetailQuote, state],
  );

  // ---------- flag 关 / 无发布目录 → 交回旧版 ----------
  if (state.phase === 'error' && state.code === 'SSOT_DISABLED') {
    return (
      <div className="p-6">
        <div className="mx-auto max-w-md rounded-xl border border-border bg-card p-6 text-center">
          <p className="text-base font-medium text-foreground">新版购买页未启用</p>
          <p className="mt-2 text-sm text-muted-foreground">
            请使用现有购买算力页面完成充值。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 p-3 sm:p-4 md:p-6 pb-24 lg:pb-6">
      {/* 标题 */}
      <div className="flex items-center gap-3">
        <ShoppingCart className="size-6 text-foreground" />
        <h1 className="text-2xl font-bold text-foreground">购买算力</h1>
      </div>

      {/* 平台服务说明 */}
      {state.phase === 'ready' && (
        <div className="rounded-xl border border-border bg-muted/40 p-4 space-y-1.5">
          <p className="text-sm text-foreground">
            由 OmniRank 平台提供产品、收款与售后服务
          </p>
          <p className="text-xs text-muted-foreground leading-relaxed">
            下方价格已是最终价格,购买后算力即时到账,可用于诊断、写作、监测等全部 AI 工具。
          </p>
          {state.catalog.digital_goods_notice && (
            <p className="text-xs font-medium text-amber-700 dark:text-amber-300 leading-relaxed">
              数字商品退款提示：{state.catalog.digital_goods_notice}
            </p>
          )}
        </div>
      )}

      {/* 加载态 */}
      {state.phase === 'loading' && (
        <div className="flex items-center justify-center py-24 text-muted-foreground">
          <Loader2 className="size-5 animate-spin" />
          <span className="ml-2 text-sm">正在加载价格…</span>
        </div>
      )}

      {/* 其他错误(无发布目录 / 未知) */}
      {state.phase === 'error'
        && state.code !== 'SSOT_DISABLED' && (
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
                  {item.subtitle && (
                    <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{item.subtitle}</p>
                  )}

                  <div className="my-3">
                    <span className="text-2xl font-bold text-foreground">
                      {formatCents(item.final_price_cents)}
                    </span>
                  </div>

                  {(item.sales_pitch || item.scene) && (
                    <div className="mt-3 space-y-1 text-xs text-muted-foreground">
                      {item.sales_pitch && <p className="leading-relaxed">{item.sales_pitch}</p>}
                      {item.scene && <p className="leading-relaxed">适合：{item.scene}</p>}
                    </div>
                  )}

                  <div className="space-y-1 text-sm">
                    <div className="flex justify-between text-muted-foreground">
                      <span>到账算力</span>
                      <span className="tabular-nums text-foreground">{formatPoints(item.points_granted)}</span>
                    </div>
                    {item.bonus_points > 0 && (
                      <div className="flex justify-between text-muted-foreground">
                        <span>赠送算力</span>
                        <span className="tabular-nums text-amber-500">
                          +{formatPoints(item.bonus_points)}
                        </span>
                      </div>
                    )}
                  </div>

                  {item.usage_examples.length > 0 && (
                    <div className="mt-3 space-y-1 border-t border-border/50 pt-3">
                      {item.usage_examples.map((ex, i) => (
                        <div key={i} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                          <Sparkles className="mt-0.5 size-3 shrink-0 text-emerald-500" />
                          <span className="leading-relaxed">{ex}</span>
                        </div>
                      ))}
                    </div>
                  )}

                  <Button
                    className="mt-4 w-full bg-foreground text-background hover:bg-foreground/90"
                    disabled={quoting}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelected(item.product_code);
                      void handleBuy(item);
                    }}
                  >
                    {quoting && selected === item.product_code ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      '立即购买'
                    )}
                  </Button>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* 报价确认弹窗(展示后端报价 + 有效期,不算价) */}
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
            <DialogTitle>确认购买</DialogTitle>
          </DialogHeader>

          {quote && (
            <div className="space-y-3 py-2">
              {quoteItem && (
                <p className="text-base font-semibold text-foreground">{quoteItem.display_name}</p>
              )}
              <div className="rounded-lg border border-border bg-muted/30 p-4 space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">应付金额</span>
                  <span className="text-lg font-bold text-foreground">{formatCents(quote.final_price_cents)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">到账算力</span>
                  <span className="tabular-nums text-foreground">{formatPoints(quote.points_granted)}</span>
                </div>
                {quote.bonus_points > 0 && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">赠送算力</span>
                    <span className="tabular-nums text-amber-500">+{formatPoints(quote.bonus_points)}</span>
                  </div>
                )}
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
                // 支付动线由后续接入(本页仅展示后端报价与有效期供确认)
                toast.info('报价已生成,请在支付页完成付款');
              }}
            >
              去支付
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {confirmDialog}
    </div>
  );
}
