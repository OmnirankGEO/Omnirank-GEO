/**
 * 小榜 deep link 预填的**唯一**消费实现(WO-B ① · 规格 §12.2/§12.3)。
 *
 * ## 为什么是一个 hook,而不是每页抄一遍
 *
 * §12.3 点名三个页面要接 intent(PublishCenter / WritingHall / GEO 图文页)。
 * R3-P7 只接了 PublishCenter,那段逻辑写在它的 6000 行文件里。
 * 照抄两份的话,「解析 → 取数 → 落参数 → 降级文案」这同一个谓词就会有三份实现,
 * 而本仓的定律是:**同一谓词写三处,至少有两处没人验**。
 *
 * 所以这里抽成一个 hook,三页共用;PublishCenter 既有的那一组浏览器判据
 * 因此变成**这个 hook 的**判据 —— 抽出来不是为了少写字,是为了让已有的锁
 * 覆盖到新接的两页。
 *
 * ## 每页不一样的只有一件事:表单参数怎么落
 *
 * 服务端冻结的 `form_prefill` 是同一套键(brand_id / quote_id / article_id /
 * geo_post_id,见后端 `_FORM_PREFILL_FIELDS`),但每一页认的 **URL 参数名** 不同
 * (发布中心的多篇文章走 `articles=1,2,3`,写作中心走 `quote_id`)。
 * 所以差异被收成一张 `PrefillParamPlan` 映射表,逻辑本身一份。
 *
 * 🔴 plan 的键必须落在后端**真会写**的那几个字段里。写一个后端从不产出的键
 * 不会报错,只会安静地什么都不填 —— 判据
 * `test_page_param_plans_only_reference_producer_fields` 从后端源码机械枚举分母,
 * 逐个核对(前端这边的正样本在 PW 用例里)。
 */
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { authFetch } from '@/lib/api'
import {
  XIAOBANG_INTENT_PARAM, prefillNumber, prefillNumberList, prefillUrl, readIntentId,
  type XiaobangPrefill,
} from '@/lib/xiaobangPrefill'

export type PrefillParamPlan = {
  /** `form_prefill` 的正整数字段 → 本页要落的 URL 参数名。 */
  numbers?: Record<string, string>
  /** `form_prefill` 的整数数组字段 → 本页要落的 URL 参数名(逗号连接)。 */
  lists?: Record<string, string>
}

export type XiaobangPrefillState = {
  intentId: string | null
  prefill: XiaobangPrefill | null
  error: string | null
}

/** intent 过期/读不出来时的两句降级文案。**不是空白页**(§12.2 明写)。 */
export const PREFILL_EXPIRED_TEXT = '这个安排已经过期了,回小榜重新准备一下。'
export const PREFILL_UNREADABLE_TEXT = '这个安排暂时读不出来,回小榜重新准备一下。'

export function useXiaobangPrefill(plan: PrefillParamPlan): XiaobangPrefillState {
  const [searchParams, setSearchParams] = useSearchParams()
  const intentId = readIntentId(searchParams)
  const [prefill, setPrefill] = useState<XiaobangPrefill | null>(null)
  const [error, setError] = useState<string | null>(null)
  // 同一个 intent 只应用一次:重复应用会在用户手改了表单之后把他改的又冲掉。
  const appliedRef = useRef<string | null>(null)
  // plan 放进 ref:它通常是页面里的字面量,每次 render 都是新对象,
  // 直接进依赖数组会让这个 effect 每帧重跑一次(而它会发请求)。
  const planRef = useRef(plan)
  planRef.current = plan

  useEffect(() => {
    if (!intentId) return
    if (appliedRef.current === intentId) return
    let cancelled = false
    void (async () => {
      try {
        const res = await authFetch(prefillUrl(intentId))
        if (!res.ok) {
          if (!cancelled) setError(PREFILL_EXPIRED_TEXT)
          return
        }
        const data = (await res.json()) as XiaobangPrefill
        if (cancelled) return
        setPrefill(data)
        setError(null)
        appliedRef.current = intentId

        const current = planRef.current
        setSearchParams((prev) => {
          const next = new URLSearchParams(prev)
          next.set(XIAOBANG_INTENT_PARAM, intentId)   // 保留,支持刷新/后退恢复
          for (const [field, param] of Object.entries(current.numbers || {})) {
            const value = prefillNumber(data, field)
            if (value) next.set(param, String(value))
          }
          for (const [field, param] of Object.entries(current.lists || {})) {
            const values = prefillNumberList(data, field)
            if (values.length) next.set(param, values.join(','))
          }
          return next
        }, { replace: true })
      } catch {
        if (!cancelled) setError(PREFILL_UNREADABLE_TEXT)
      }
    })()
    return () => { cancelled = true }
  }, [intentId, setSearchParams])

  return { intentId, prefill, error }
}
