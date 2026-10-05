/**
 * 小榜预填条(XO-03 · 规格 §12.3)。
 *
 * §12.3 要求这批字段**在主按钮上方同屏可见**：当前客户、当前对象、小榜推荐和原因、
 * 所有将产生的外部动作、目标渠道/账号、发布后是否公开、payer、准确算力。
 * 少一项都会让用户在不知道后果的情况下点下去。
 *
 * 🔴 组件**不生成任何业务文案** —— 主按钮与取消的文字全部来自服务端
 * (`primary_action.label` / `cancel_action.label`)。理由见 `lib/xiaobangPrefill.ts`。
 */

import { CheckCircle2, Info } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { channelDisplayText, type XiaobangPrefill } from '@/lib/xiaobangPrefill'

type Props = {
  prefill: XiaobangPrefill
  onBackToAssistant?: () => void
}

function reasonText(reason: Record<string, unknown>): string {
  const explanation = reason?.explanation ?? reason?.reason_text ?? reason?.label
  return typeof explanation === 'string' ? explanation : ''
}

export function XiaobangPrefillStrip({ prefill, onBackToAssistant }: Props) {
  const reasons = (prefill.recommendation?.reasons || []).map(reasonText).filter(Boolean)
  const objectItems = prefill.object?.items || []
  const compute = prefill.compute
  const computeText =
    compute?.state === 'quoted' && compute.amount != null
      ? `${compute.amount} ${compute.unit || '算力'}`
      : compute?.note || '按你选的内容实时计算'

  return (
    <Card data-testid="xiaobang-prefill-strip" className="border-primary/40 bg-primary/5">
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary" className="gap-1">
            <CheckCircle2 className="h-3.5 w-3.5" />
            小榜已帮你填好
          </Badge>
          <span className="text-sm font-medium" data-testid="xiaobang-prefill-operation">
            {prefill.display_name}
          </span>
        </div>

        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-muted-foreground">当前客户</dt>
            <dd data-testid="xiaobang-prefill-customer">{prefill.customer?.label || '未指定'}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">当前对象</dt>
            <dd data-testid="xiaobang-prefill-object">
              {prefill.object?.label || '未指定'}
              {objectItems.length > 1 && (
                <span className="ml-1 text-xs text-muted-foreground">({objectItems.length} 项)</span>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">谁来付</dt>
            <dd data-testid="xiaobang-prefill-payer">{prefill.payer?.label || '你的钱包'}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">要用多少算力</dt>
            <dd data-testid="xiaobang-prefill-compute">{computeText}</dd>
          </div>
          {prefill.visibility && (
            <div>
              <dt className="text-muted-foreground">发布后是否公开</dt>
              <dd data-testid="xiaobang-prefill-visibility">{prefill.visibility}</dd>
            </div>
          )}
          {/* [R3-P11 ②] 目标渠道/账号 —— §12.3 要求它在主按钮上方同屏可见。
              🔴 **无条件渲染**:不加 `prefill.channel &&` 守卫。
              渠道缺失恰恰是现在的常态(渠道资格仍在 manifest.pending 里),
              加守卫等于"缺的时候这一栏整个消失",用户看不到"还没选目标渠道"
              这件本该知道的事。文案由 channelDisplayText 三档兜底,不会是空串。 */}
          <div>
            <dt className="text-muted-foreground">目标渠道/账号</dt>
            <dd data-testid="xiaobang-prefill-channel">
              {channelDisplayText(prefill.channel)}
            </dd>
          </div>
        </dl>

        {reasons.length > 0 && (
          <div className="rounded-md bg-background/70 p-3 text-sm" data-testid="xiaobang-prefill-reasons">
            <div className="mb-1 flex items-center gap-1 text-muted-foreground">
              <Info className="h-3.5 w-3.5" />
              小榜为什么这样安排
            </div>
            <ul className="list-disc space-y-1 pl-5">
              {reasons.map((reason, index) => (
                <li key={index}>{reason}</li>
              ))}
            </ul>
          </div>
        )}

        {prefill.external_actions?.length > 0 && (
          <div className="text-sm" data-testid="xiaobang-prefill-external-actions">
            <span className="text-muted-foreground">会产生的对外动作:</span>
            <ul className="mt-1 list-disc space-y-1 pl-5">
              {prefill.external_actions.map((action, index) => (
                <li key={index}>{String(action?.label ?? '')}</li>
              ))}
            </ul>
          </div>
        )}

        {/* 🔴 文案全部来自服务端。取消那句尤其不能前端拼:
            「已开始执行」还显示成「取消 · 不使用算力」= 承诺了做不到的事。 */}
        <div className="flex flex-wrap items-center gap-2 pt-1">
          {/* 🔴 [微单 2026-08-20] `enabled=false` 时**视觉上也要塌下去**。
              服务端已经把「这条执行链还没开放」如实说出来了(enabled + note),
              前端还把它渲染成一行自信的 CTA 文案,等于把死按钮换了个材质:
              看着能点、点了(或以为点了)什么都不会发生。 */}
          <span
            className={prefill.primary_action?.enabled === false
              ? 'text-sm font-medium text-muted-foreground line-through decoration-1'
              : 'text-sm font-medium'}
            data-testid="xiaobang-prefill-primary-label"
            data-enabled={String(prefill.primary_action?.enabled !== false)}
            aria-disabled={prefill.primary_action?.enabled === false || undefined}
          >
            {prefill.primary_action?.label}
          </span>
          {prefill.primary_action?.enabled === false && prefill.primary_action?.note && (
            <span
              className="text-xs text-muted-foreground"
              data-testid="xiaobang-prefill-primary-note"
            >
              {prefill.primary_action.note}
            </span>
          )}
          {prefill.cancel_action?.label && (
            <span className="text-xs text-muted-foreground" data-testid="xiaobang-prefill-cancel-label">
              · {prefill.cancel_action.label}
            </span>
          )}
          {onBackToAssistant && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              data-testid="xiaobang-prefill-back"
              onClick={onBackToAssistant}
            >
              返回小榜
            </Button>
          )}
        </div>

        {prefill.compute?.state === 'expired' && (
          <p className="text-xs text-amber-700" data-testid="xiaobang-prefill-quote-expired">
            {prefill.compute.note || '算力报价已过期,重新核对一下就能继续。'}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
