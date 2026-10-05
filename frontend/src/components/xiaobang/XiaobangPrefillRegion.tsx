/**
 * 小榜预填区(条 + 降级提示)—— 三个业务页面共用的**同一段呈现**(WO-B ①)。
 *
 * §12.3 那批字段必须在主按钮上方同屏可见,§12.2 要求过期时显示「重新准备」
 * 而不是空白页。这两件事在三页上必须**一模一样**:
 * 各页各写一遍的话,某一页少渲染一栏(比如渠道那一栏)是不会有人发现的 ——
 * 那正是 R3-P11 修 channel 时踩过的形态(前端有渲染、后端没 producer,反过来也一样)。
 *
 * 🔴 组件不生成任何业务文案:主按钮 / 取消的字全部来自服务端。
 *    前端拼错的方向恰好是最贵的那种(把「已开始执行」显示成「取消 · 不使用算力」)。
 */
import { XiaobangPrefillStrip } from '@/components/publishing/XiaobangPrefillStrip'
import { awaitsPageConfirmation, type XiaobangPrefill } from '@/lib/xiaobangPrefill'

type Props = {
  prefill: XiaobangPrefill | null
  error: string | null
  onBackToAssistant?: () => void
  className?: string
}

export function XiaobangPrefillRegion({ prefill, error, onBackToAssistant, className }: Props) {
  return (
    <>
      {/* 终态/已执行的 intent 不再显示确认区 —— §12.1:同一 intent 任一时刻
          只能有一个可用确认 CTA,抽屉那边已经有了。 */}
      {prefill && awaitsPageConfirmation(prefill) && (
        <div className={className ?? 'mb-3'}>
          <XiaobangPrefillStrip prefill={prefill} onBackToAssistant={onBackToAssistant} />
        </div>
      )}
      {error && (
        <div
          data-testid="xiaobang-prefill-error"
          className={`${className ?? 'mb-3'} rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800`}
        >
          {error}
        </div>
      )}
    </>
  )
}
