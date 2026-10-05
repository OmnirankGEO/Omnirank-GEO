/**
 * 小榜 deep link 预填(XO-03 · 规格 §12.2/§12.3)。
 *
 * deep link 只带一个不可猜的 `xiaobang_intent=xint_...`（§12.2 明写 URL 里不放
 * 客户名、供应商、算力公式或完整内容），所以页面进来时只有这一个 opaque id。
 * 这个模块负责拿它向服务端换回**已授权的预填数据**，页面再据此填表。
 *
 * 🔴 为什么文案不在前端拼：主按钮二选一、取消三档都由服务端定
 * （`primary_action` / `cancel_action`）。前端拼的话拼错的方向恰好是最贵的那种 ——
 * 「已开始执行」还显示成「取消 · 不使用算力」= 对用户承诺了一件我们做不到的事。
 * 所以这里只做**取数 + 解析**，一个字的业务文案都不生成。
 */

export const XIAOBANG_INTENT_PARAM = 'xiaobang_intent'

/** intent id 形状：不可猜的 `xint_` 前缀（与后端 `new_opaque_id` 一致）。 */
const INTENT_ID_RE = /^xint_[A-Za-z0-9_-]{8,64}$/

export type PrefillAction = {
  next_action_id: string
  label: string
  enabled?: boolean
  note?: string | null
}

export type PrefillObjectItem = { resource_kind: string; label: string }

/**
 * 目标渠道/账号(规格 §12.3 要求它在主按钮上方同屏可见)。
 *
 * 🔴 所有字段都可能缺。服务端在渠道资格没核过时**故意不编账号名** ——
 * 页面必须把「还没核」如实说出来,而不是渲染成空白。
 */
export type XiaobangChannel = {
  /** 调用方选的渠道项。机读键(DLP 白名单里那个全名),不是人话。 */
  channel_option_id?: string | null
  /** 这条动作往哪种载体上发(合同登记的对象类型)。 */
  resource_kind?: string | null
  /** 渠道资格核过没有。没核过时页面必须明说,不能显示成"已确认"。 */
  verified?: boolean | null
  /**
   * [WO-B ③] 核过之后**能不能用**。`verified` 与 `eligible` 是两件事:
   * 压成一位的那一天,「核过了但这个号今天满了」会显示成「还没核」,
   * 用户会一直等它变好,而它今天不会变好。只在真核过时下发。
   */
  eligible?: boolean | null
  /** 核过之后的人话结论(中文;服务端定,前端不拼)。 */
  eligibility_note?: string | null
}

/** 对象类型 → 人话。认不出就落到空串交给下一档兜底,**不硬翻**(硬翻会造出不存在的载体名)。 */
const CHANNEL_KIND_LABELS: Record<string, string> = {
  geo_image_post: '图文',
  article: '文章',
  publication: '发布任务',
}

/**
 * 目标渠道/账号的显示文案。
 *
 * 🔴 四档都必须有字,**一个 undefined 都不许漏到页面上**:
 *  - 有渠道项 + **已核** → 说清是哪一项 + 服务端给的资格结论(能不能用/为什么不能);
 *  - 有渠道项 + 未核 → 说清是哪一项 + "账号资格还没核"；
 *  - 只知道载体类型 → 说载体 + "具体账号在下一步选"；
 *  - 什么都没有 → "还没选目标渠道"。
 * 空串是最坏的一档:页面上会渲染成"有这一栏、但它是空的",
 * 比"还没选"更误导 —— 判据 pinned 了这几档的**实际文字**。
 *
 * 🔴 [WO-B ③] 第四档(已核)是新加的,前三档**逐字不动** ——
 *    ③ 之前 `verified` 恒 false,那三档的 PW 判据打的就是那三种输入。
 *    资格结论的措辞一律来自服务端 `eligibility_note`,前端不翻译 reason_code:
 *    翻一次就会有两份措辞,而它们会漂。
 */
export function channelDisplayText(channel: XiaobangChannel | null | undefined): string {
  const option = typeof channel?.channel_option_id === 'string'
    ? channel.channel_option_id.trim() : ''
  const kindRaw = typeof channel?.resource_kind === 'string' ? channel.resource_kind : ''
  const kind = CHANNEL_KIND_LABELS[kindRaw] || ''
  const note = typeof channel?.eligibility_note === 'string'
    ? channel.eligibility_note.trim() : ''
  // 已核 → 说结论(服务端给的中文);未核 → 明说没核。绝不留空。
  const verifiedSuffix = channel?.verified === true
    ? (note ? ` · ${note}` : '')
    : ' · 账号资格还没核'

  if (option) {
    return `${kind ? kind + ' · ' : ''}${option}${verifiedSuffix}`
  }
  if (kind) {
    return `${kind} · 具体账号在下一步选${verifiedSuffix}`
  }
  return '还没选目标渠道'
}

export type XiaobangPrefill = {
  contract_version: string
  intent_id: string
  intent_state: string
  intent_revision: number
  operation_id: string
  display_name: string
  side_effect: string
  target_route: string | null
  customer: { label: string | null }
  object: {
    pending_checks?: string[]
    label: string | null
    items: PrefillObjectItem[]
  }
  recommendation: { reasons: Array<Record<string, unknown>> }
  external_actions: Array<Record<string, unknown>>
  payer: { label: string | null; role?: string | null }
  visibility?: string | null
  channel: XiaobangChannel
  compute: { state: string; amount: number | null; unit: string; note?: string }
  primary_action: PrefillAction
  cancel_action: PrefillAction
  secondary_actions: PrefillAction[]
  form_prefill: Record<string, unknown>
  rebind_hint: { label: string; primary_label: string; watched_fields: string[] }
}

/**
 * 从 query string 里取 intent id。
 *
 * 形状不对就当没有 —— 不把任意字符串拼进 URL 发给后端
 * （那会把「用户手改了 URL」变成一次 404 噪声，也给猜 id 提供了反馈通道）。
 */
export function readIntentId(search: string | URLSearchParams): string | null {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search
  const raw = (params.get(XIAOBANG_INTENT_PARAM) || '').trim()
  return INTENT_ID_RE.test(raw) ? raw : null
}

export function prefillUrl(intentId: string): string {
  return `/api/xiaobang/operations/intents/${encodeURIComponent(intentId)}/prefill`
}

/**
 * [WO-B2 ① 2026-08-20] 深链 **producer** 的两件事,放在 consumer 隔壁。
 *
 * 🔴 为什么写在这个文件里:`XIAOBANG_INTENT_PARAM` 是 producer 与 consumer
 *    **共用的那一个常量**。producer 在别处自己拼一次 `?xiaobang_intent=`,
 *    就是同一个谓词写两处 —— 改参数名的那天,签发方和解析方会各走各的。
 */
export function preparePath(operationId: string): string {
  return `/api/xiaobang/operations/${encodeURIComponent(operationId)}/prepare`
}

/**
 * 把 prepare 返回的 intent 挂到目标路由上。
 *
 * 🔴 §12.2:URL 里**只放**这一个不可猜的 opaque id ——
 *    不放客户名、供应商、算力公式或完整内容。所以这里只接受 route + intentId,
 *    没有第三个参数可以塞业务字段进去。
 * 🔴 route 自带 query 时用 `URLSearchParams` 合并,不做字符串拼接:
 *    `?` 拼成第二个会让整串 query 失效,而那种链接点开是**空白默认表单** ——
 *    正是 §12.3 要消灭的那个形态。
 */
export function buildDeepLink(route: string, intentId: string): string {
  const [path, query = ''] = String(route || '').split('?')
  const params = new URLSearchParams(query)
  params.set(XIAOBANG_INTENT_PARAM, intentId)
  return `${path}?${params.toString()}`
}

/** 从 `form_prefill` 里取一个正整数字段；拿不到就 null（不猜、不填 0）。 */
export function prefillNumber(prefill: XiaobangPrefill | null, field: string): number | null {
  const raw = prefill?.form_prefill?.[field]
  const value = typeof raw === 'string' ? Number(raw) : raw
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

/** 从 `form_prefill` 里取一组正整数（文章多选这类）。 */
export function prefillNumberList(prefill: XiaobangPrefill | null, field: string): number[] {
  const raw = prefill?.form_prefill?.[field]
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => (typeof item === 'string' ? Number(item) : item))
    .filter((item): item is number => typeof item === 'number' && Number.isFinite(item) && item > 0)
}

/**
 * intent 是否还等着用户在页面上确认。
 *
 * 终态/已执行时页面**不该**再显示确认区 —— 那会给出第二个确认 CTA，
 * 而 §12.1 规定同一 intent 任一时刻只能有一个可用确认 CTA。
 */
export function awaitsPageConfirmation(prefill: XiaobangPrefill | null): boolean {
  if (!prefill) return false
  return prefill.intent_state === 'prepared' || prefill.intent_state === 'awaiting_confirmation'
}
