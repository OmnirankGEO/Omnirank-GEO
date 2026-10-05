/**
 * useXiaobangHandoff — 抽屉内「转人工」的**共享**提交逻辑(包 D②③④)。
 *
 * 🔴 这里**没有第二套反馈系统**(工单 §7.4 明令)。
 *    它打的就是 `FeedbackPage` 已经在打的那个端点 `POST /api/faq/feedback`,
 *    字段形状逐字对齐(`client_id` / `kind:'bug'` / `message` / `urgency` /
 *    `screenshot_url` / `ai_answer`),后端合同一个字没动。
 *    抽出来的理由只有一个:抽屉和 Feedback 页要用**同一段**逻辑 ——
 *    复制一份出来,两份会各自漂,而漂的表现是「两个入口提交出来的工单不一样」。
 *
 * 🔴 用户必须**知道会提交什么**:调用方把 `contextSummary` 显示出来,
 *    这里只负责把它拼进 message。不允许悄悄外发(工单 §6 包 D③)。
 */
import { useCallback, useState } from 'react'
import { authFetch } from '@/lib/api'

/** 与 `FeedbackPage.makeClientId` 同形态:幂等边界,后端据此去重。 */
function makeClientId(): string {
  const rand = Math.random().toString(36).slice(2, 10)
  return `bug_${Date.now()}_${rand}`
}

export interface XiaobangHandoffPayload {
  /** 用户原始问题 */
  question: string
  /** 小榜刚才的回答(交给人工看的关键上下文) */
  aiAnswer: string
  /** 当前路由 */
  currentPage: string
  /** 最近有界对话(已经是净化过的 role/content) */
  recentTurns: Array<{ role: string; content: string }>
  /** 截图 key(可空;抽屉里的截图走和 Feedback 页同一个上传端点) */
  screenshotUrl?: string
  urgency?: 'low' | 'mid' | 'high'
}

export interface XiaobangHandoffResult {
  /** 后端返回的工单 id —— 必须显示给用户,不能只弹个 toast(工单 §6 包 D④) */
  ticketId: number
  /** 'new' | 'existing'(同 client_id 重放拿到同一张) */
  status: string
}

/**
 * 后端合同上限 —— `api/faq_api.py`:
 * `FeedbackRequest.message = Field(..., min_length=5, max_length=500)`。
 *
 * 🔴 这是**存量合同**(`FeedbackPage` 也在打同一个端点),不许为了抽屉好拼就去放宽它。
 *    拼出来超一个字 → pydantic 422 → 用户点「确认提交」只看到一句提交失败,
 *    而他刚才聊得越久越必然触发(轮数越多拼得越长)。
 *
 * 🔴 用 `String.length`(UTF-16 码元)当尺子而不是码点数:同一段文本
 *    JS 的码元数 ≥ Python `len()` 的码点数(BMP 外字符 JS 记 2、Python 记 1),
 *    所以「JS 量到 ≤500」必然蕴含「后端量到 ≤500」—— 方向是保守的那一边。
 */
export const HANDOFF_MESSAGE_MAX = 500

/** 单轮对话在 message 里最多占多少字(v1 就是 200,本次不动)。 */
const TURN_CHARS_MAX = 200

/** 丢掉更早几轮后留在原地的记号:人工看见它就知道上面还有,不是对话只有这么点。 */
const ELISION = '…(更早对话已省略)'

/**
 * 按字数截断,且**不从代理对(surrogate pair)中间下刀**。
 *
 * 切出半个代理会被 `JSON.stringify` 原样发出去、pydantic 也收得下,
 * 一路要等到写库 encode UTF-8 才炸 —— 那是个 500,而且现场在数据库层,
 * 离这里十万八千里。所以刀口宁可退一格。
 */
function cutChars(text: string, limit: number): string {
  if (text.length <= limit) return text
  let end = limit
  const lead = text.charCodeAt(end - 1)
  if (lead >= 0xd800 && lead <= 0xdbff) end -= 1
  return text.slice(0, end)
}

function assemble(
  question: string,
  page: string,
  turns: Array<{ role: string; content: string }>,
  elided: boolean,
): string {
  const lines: string[] = []
  lines.push(`问题:${question}`)
  lines.push(`当前页面:${page}`)
  if (turns.length || elided) {
    lines.push('最近对话:')
    if (elided) lines.push(`  ${ELISION}`)
    for (const turn of turns) {
      const who = turn.role === 'assistant' ? '小榜' : '用户'
      lines.push(`  ${who}:${cutChars(turn.content, TURN_CHARS_MAX)}`)
    }
  }
  return lines.join('\n')
}

/**
 * 把要提交的东西拼成人工看得懂的一段话,且**保证不超过后端合同上限**。
 *
 * 顺序固定:问题 → 在哪一页 → 最近几轮 → 小榜答过什么。
 * 人工接手时第一眼要看的就是这个顺序。
 *
 * 装不下时的取舍顺序(工单 B-⑥):**用户当前描述 > 最近的轮 > 更早的轮**。
 * 所以丢的是**最早**那几轮,丢多少丢多少,丢过就在原地留 `ELISION`。
 * 短对话装得下 → 一个字都不动,全量带上。
 */
export function buildHandoffMessage(payload: XiaobangHandoffPayload): string {
  const question = payload.question.trim() || '(用户未补充描述)'
  const page = payload.currentPage || '(未知)'
  const all = payload.recentTurns

  for (let dropped = 0; dropped <= all.length; dropped++) {
    const candidate = assemble(question, page, all.slice(dropped), dropped > 0)
    if (candidate.length <= HANDOFF_MESSAGE_MAX) return candidate
  }

  // 走到这儿 = 一轮都塞不下,问题本身就把预算吃光了。
  // 省略记号留着(得让人工知道上面还有对话),剩下的全给问题。
  const elided = all.length > 0
  const room = HANDOFF_MESSAGE_MAX - assemble('', page, [], elided).length
  const trimmed = assemble(cutChars(question, Math.max(1, room)), page, [], elided)
  // 兜底硬钳:page 本身荒谬地长时上面那个 room 会算成负数。
  // 这一层保证「≤500」在任何输入下都成立 —— 422 一次都不许出现。
  return cutChars(trimmed, HANDOFF_MESSAGE_MAX)
}

export function useXiaobangHandoff() {
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<XiaobangHandoffResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const submit = useCallback(async (payload: XiaobangHandoffPayload) => {
    setSubmitting(true)
    setError(null)
    try {
      const res = await authFetch('/api/faq/feedback', {
        method: 'POST',
        body: JSON.stringify({
          client_id: makeClientId(),
          kind: 'bug',
          message: buildHandoffMessage(payload),
          urgency: payload.urgency || 'mid',
          screenshot_url: payload.screenshotUrl || '',
          ai_answer: payload.aiAnswer || '',
        }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(detail?.detail || `提交失败:${res.status}`)
      }
      const body = await res.json()
      const out: XiaobangHandoffResult = {
        ticketId: Number(body?.id || 0),
        status: String(body?.status || 'new'),
      }
      setResult(out)
      return out
    } catch (err) {
      // 🔴 O1:失败**不清空用户已填内容**,只报错并留着重试入口(工单 §8 S15)。
      const msg = String((err as Error).message || err)
      setError(msg)
      return null
    } finally {
      setSubmitting(false)
    }
  }, [])

  const reset = useCallback(() => {
    setResult(null)
    setError(null)
  }, [])

  return { submit, submitting, result, error, reset }
}
