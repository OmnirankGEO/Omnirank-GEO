/**
 * XiaobangLinkCard — 答案下方的"去 X 页"跳转按钮
 *
 * 跟旧 链接卡 的区别:
 * - 只跳转 · 不执行任何业务操作(不扣费 / 不写库 / 不切状态)
 * - 关闭 drawer 后跳转(否则 drawer 盖住目标页)
 *
 * ## [WO-B2 ① 2026-08-20] 深链 producer
 *
 * 服务端给这张卡带上 `operation_id` 时(= 这个路由背后有一条**可 prepare** 的
 * operation),点击不再是裸 navigate,而是:
 *
 *     POST /api/xiaobang/operations/{operation_id}/prepare
 *       → 拿到 intent_id 与服务端签发的 target_route
 *       → navigate(`${target_route}?xiaobang_intent=xint_...`)
 *
 * 在这之前,全仓**没有任何地方**构造 `?xiaobang_intent=` —— 三个页面的消费方
 * (WO-B ① 接的)只能靠手拼 URL 才到得了。这一格就是那个缺失的 producer。
 *
 * 🔴 **prepare 失败一律降级成裸跳转,不拦路。** 用户点的是"去这一页",
 *    预填是锦上添花;因为预填没准备好就不让他去,是把一个增强做成了闸。
 * 🔴 **目标路由用服务端返回的那个**(`deep_link.target_route`),不是卡片上的
 *    `link.route`:路由由版本化操作地图签发,前端不拼、不猜、不兜底旧路径。
 * 🔴 **不扣费**:prepare 是零算力、零 provider、零业务写(规格 §10)。
 */
import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { authFetch } from '@/lib/api'
import { buildDeepLink, preparePath, readIntentId } from '@/lib/xiaobangPrefill'
import { useClientContext } from '@/context/ClientContext'
import type { XiaobangLink } from '@/hooks/useXiaobangChat'

interface XiaobangLinkCardProps {
  link: XiaobangLink
  onNavigate?: () => void
  className?: string
}

/** 每次点击一个新的 prepare_request_id:同 id 会命中幂等、拿回上一次那个 intent。 */
function newPrepareRequestId(): string {
  const rand = Math.random().toString(36).slice(2, 10)
  return `xbcard-${Date.now().toString(36)}-${rand}`
}

export function XiaobangLinkCard({ link, onNavigate, className }: XiaobangLinkCardProps) {
  const navigate = useNavigate()
  const { currentBrandId } = useClientContext()
  const [minting, setMinting] = useState(false)

  const mintDeepLink = useCallback(async (): Promise<string | null> => {
    if (!link.operation_id) return null
    try {
      const res = await authFetch(preparePath(link.operation_id), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prepare_request_id: newPrepareRequestId(),
          // 🔴 只带**当前客户**。选择项由用户在目标页上继续选 ——
          //    从聊天里猜一个报价/文章/账号塞进去,等于替他做了决定。
          selection: currentBrandId ? { brand_id: currentBrandId } : {},
        }),
      })
      if (!res.ok) return null
      const data = await res.json()
      const intentId = readIntentId(`x=1&xiaobang_intent=${data?.intent_id ?? ''}`)
      const route = data?.deep_link?.target_route
      if (!intentId || typeof route !== 'string' || !route) return null
      return buildDeepLink(route, intentId)
    } catch {
      return null
    }
  }, [link.operation_id, currentBrandId])

  const handleClick = async () => {
    if (minting) return
    let target = link.route
    if (link.operation_id) {
      setMinting(true)
      try {
        target = (await mintDeepLink()) ?? link.route
      } finally {
        setMinting(false)
      }
    }
    onNavigate?.()
    navigate(target)
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={minting}
      data-testid="xiaobang-link-card"
      data-operation-id={link.operation_id || ''}
      className={cn(
        'inline-flex w-full items-center justify-between rounded-lg',
        'bg-brand px-3 py-2 text-xs font-medium text-white',
        'shadow-sm transition-opacity hover:opacity-90 disabled:opacity-60',
        className,
      )}
    >
      <span className="truncate">{link.label}</span>
      {minting
        ? <Loader2 className="size-3.5 shrink-0 animate-spin" />
        : <ArrowRight className="size-3.5 shrink-0" />}
    </button>
  )
}
