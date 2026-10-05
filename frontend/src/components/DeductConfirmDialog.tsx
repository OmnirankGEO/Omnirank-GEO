/**
 * DeductConfirmDialog — 统一扣费前确认(WJ-05/06/22 · 王姐"敢点")
 *
 * 🔴 铁律:costPoints 必须来自**后端权威**(useFeatureCost / 接口返回的报价),
 *    前端只**展示**,绝不前端自己算钱当权威。组件不接收"前端拼出来的金额"。
 *    真实扣费走后端单一入口(freeze_points / 订单)· 本组件只是点前告知。
 *
 * 两种模式:
 *  1. 普通扣费(默认):点前三行「给谁做 · 扣 N 算力 · 做完得到」+「后台运行不重复扣」
 *  2. 真外采下单(externalOrder=true · WJ-22):媒体/外采/客户售价 +「确认后不可撤回」
 *
 * 🔴 价格口径铁律:本通用扣费组件**只显示算力**,绝不显示人民币估算(≈¥)。
 *    admin 如需人民币换算,另做 admin-only 财务组件,不得塞进此共享组件。
 *    例外:externalOrder 模式的「外采成本¥ / 客户售价¥」是代理真实下单确认所需的**真实下单金额**,
 *    不是积分→人民币折算,不属于"固定汇率/约¥/前台换算"口径问题,保留。
 */
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Loader2, Wallet, AlertTriangle } from 'lucide-react';

export interface DeductConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 给谁做(客户/品牌名)· 缺省时不显该行 */
  brandName?: string;
  /** 做什么(AI 体检 / 写文章 / 发布 ...) */
  featureLabel: string;
  /** 扣多少算力 — 🔴 必须后端权威值,前端只展示 */
  costPoints: number;
  /** 做完得到什么(一句话) */
  outcome?: string;
  /** 后台运行安心语 · 默认"后台运行,刷新或离开不重复扣费" · 同步任务可传 '' 关掉 */
  bgNote?: string;
  loading?: boolean;
  confirmText?: string;
  onConfirm: () => void;

  // ===== WJ-22 真外采下单模式 =====
  externalOrder?: boolean;
  externalVendor?: string;   // 媒体
  externalCost?: number;     // 外采成本(¥)
  clientPrice?: number;      // 客户售价(¥)
}

export function DeductConfirmDialog({
  open, onOpenChange, brandName, featureLabel, costPoints,
  outcome, bgNote = '后台运行,刷新或离开不重复消耗算力', loading = false,
  confirmText, onConfirm,
  externalOrder = false, externalVendor, externalCost, clientPrice,
}: DeductConfirmDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!loading) onOpenChange(next); }}>
      <DialogContent className="max-w-[92vw] sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {externalOrder ? <AlertTriangle className="h-5 w-5 text-amber-500" /> : <Wallet className="h-5 w-5 text-primary" />}
            {externalOrder ? '确认真实下单' : '确认这一步'}
          </DialogTitle>
          {externalOrder && (
            <DialogDescription className="text-amber-600">真实下单给媒体 · 确认后不可撤回</DialogDescription>
          )}
        </DialogHeader>

        {externalOrder ? (
          <div className="space-y-2 text-sm">
            {brandName && <div className="flex justify-between"><span className="text-muted-foreground">给谁做</span><span className="font-medium">{brandName}</span></div>}
            {externalVendor && <div className="flex justify-between"><span className="text-muted-foreground">投放媒体</span><span className="font-medium">{externalVendor}</span></div>}
            {externalCost != null && <div className="flex justify-between"><span className="text-muted-foreground">外采成本</span><span className="font-medium">¥{externalCost.toLocaleString()}</span></div>}
            {clientPrice != null && <div className="flex justify-between"><span className="text-muted-foreground">客户售价</span><span className="font-medium text-emerald-600">¥{clientPrice.toLocaleString()}</span></div>}
          </div>
        ) : (
          <div className="space-y-2 text-sm">
            {brandName && <div className="flex justify-between"><span className="text-muted-foreground">给谁做</span><span className="font-medium">{brandName}</span></div>}
            <div className="flex justify-between"><span className="text-muted-foreground">做什么</span><span className="font-medium">{featureLabel}</span></div>
            <div className="flex justify-between"><span className="text-muted-foreground">扣多少</span><span className="font-medium">{costPoints.toLocaleString()} 算力</span></div>
            {outcome && <div className="flex justify-between"><span className="text-muted-foreground">做完得到</span><span className="font-medium text-right max-w-[60%]">{outcome}</span></div>}
            {bgNote && <p className="pt-1 text-xs text-muted-foreground">{bgNote}</p>}
          </div>
        )}

        <DialogFooter className="gap-2 sm:gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={loading}>取消</Button>
          <Button onClick={onConfirm} disabled={loading} className={externalOrder ? 'bg-amber-500 hover:bg-amber-600 text-white' : ''}>
            {loading && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />}
            {confirmText || (externalOrder ? '确认下单' : '确认')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
