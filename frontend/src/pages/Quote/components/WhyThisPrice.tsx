/**
 * WhyThisPrice — 报价解释层一期「为什么是这个价」展示块(代理端 · OnlineQuoteFlow 展开区)
 *
 * 人话拆解(篇数/价值/成本/系数)+ 风险提示 · 复用 priceRationale 纯构建器。
 * 仅代理端调用(客户端 effective_competition/value_score/guarantee_unavailable 等已脱敏)。
 * className 由父控制 grid 跨列(移动端 col-span-2 · 桌面端整宽)。
 */
import { ListChecks, TrendingUp, Coins, Percent, ShieldCheck, AlertTriangle, Info, CalendarClock } from 'lucide-react'
import {
  buildPriceRationale,
  type RationaleTier,
  type PriceRationaleInput,
} from '../utils/priceRationale'

const KIND_ICON = {
  articles: ListChecks,
  value: TrendingUp,
  cost: Coins,
  markup: Percent,
  trust: ShieldCheck,
  recheck: CalendarClock,
} as const

interface Props {
  kw: PriceRationaleInput
  tier?: RationaleTier
  className?: string
  /** 后端 /api/quotes/:id/media-mix 返回的比例上下文。
   *  🔴 [2026-08-12 Review P1-1] 不传 = 走全局先验,且文案会**如实**降级成
   *     「当前按全行业平均值估算」。绝不允许"没接线却说按行业调整"。 */
  mixContext?: { ratioUsed?: number; douyinShare?: number; ratioSource?: string }
}

export function WhyThisPrice({ kw, tier = 'standard', className = '', mixContext }: Props) {
  /* 🔴 [Owner 2026-08-11 亲裁 · R5] 本块**刻意不接**隐私态。
     R2 曾把它整块挂到页顶那只眼睛后面,因为当时里面有「单篇成本 / 基础成本 = 篇数 × 单篇成本 /
     你账号的报价系数」;R5 把这几处措辞**直接删掉**了(见 priceRationale 的
     RATIONALE_FORBIDDEN_PHRASES),剩下的只是基本的成本构成,客户看见也无所谓 → 常显。
     ⚠️ 想在这里加回 usePricingPrivacy 之前先问:是不是有人又把敏感措辞塞回去了? */
  const { rows, risk } = buildPriceRationale(kw, tier, mixContext ?? {})

  return (
    <div className={`rounded-lg border border-border bg-background/50 p-3 space-y-2.5 ${className}`} data-testid="why-this-price">
      <p className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
        <Info className="h-3.5 w-3.5 shrink-0 text-blue-400" />
        为什么是这个价
      </p>

      <div className="space-y-2">
        {rows.map(row => {
          const Icon = KIND_ICON[row.kind]
          return (
            <div key={row.kind} className="flex gap-2">
              <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <p className="text-xs leading-relaxed text-muted-foreground">
                <span className="font-medium text-foreground">{row.title}：</span>
                {row.body}
              </p>
            </div>
          )
        })}
      </div>

      {risk && (
        <div
          className={`flex gap-2 rounded-md border p-2 ${
            risk.tone === 'warn'
              ? 'border-amber-500/30 bg-amber-500/10'
              : 'border-border bg-muted'
          }`}
        >
          {risk.tone === 'warn' ? (
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          ) : (
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          )}
          <p
            className={`text-xs leading-relaxed ${
              risk.tone === 'warn'
                ? 'text-amber-600 dark:text-amber-300'
                : 'text-muted-foreground'
            }`}
          >
            {risk.body}
          </p>
        </div>
      )}
    </div>
  )
}
