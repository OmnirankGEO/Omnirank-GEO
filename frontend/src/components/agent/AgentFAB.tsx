/**
 * AgentFAB — 右下角浮窗按钮（可拖拽）
 * 点击打开 旧 AI 助手抽屉，长按/拖动可移动位置
 */

import { useRef, useState, useCallback } from 'react'
import { MessageCircle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { FLOATING_Z, FLOATING_FAB, MOBILE_BOTTOM_BAR_HEIGHT, LG_BREAKPOINT } from '@/lib/floating-stack'

interface AgentFABProps {
  onClick: () => void
  className?: string
}

export function AgentFAB({ onClick, className }: AgentFABProps) {
  // 默认位置走 FloatingStack 常量 · 改一处生效(数值跟原 24/100 一致)
  // 显式标 number 类型 · 防 as const 收窄 literal type 让 setPos 拒掉拖拽算出的值
  const [pos, setPos] = useState<{ x: number; y: number }>({
    x: FLOATING_FAB.XIAOBANG_DEFAULT_RIGHT,
    y: FLOATING_FAB.XIAOBANG_DEFAULT_BOTTOM,
  })
  const dragging = useRef(false)
  const moved = useRef(false)
  const startPt = useRef({ px: 0, py: 0, ox: 0, oy: 0 })

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    dragging.current = true
    moved.current = false
    startPt.current = {
      px: e.clientX,
      py: e.clientY,
      ox: pos.x,
      oy: pos.y,
    }
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
  }, [pos])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (!dragging.current) return
    const dx = e.clientX - startPt.current.px
    const dy = e.clientY - startPt.current.py
    // 移动超过 5px 才算拖拽（区分点击）
    if (Math.abs(dx) > 5 || Math.abs(dy) > 5) moved.current = true
    if (!moved.current) return

    const vw = window.innerWidth
    const vh = window.innerHeight
    // x = 距右边距离（right），y = 距底边距离（bottom）
    // 鼠标右移 dx>0 → right 减小；鼠标下移 dy>0 → bottom 减小
    //
    // 🔴 下限不是 8(xbvnext 2026-08-18)。窄屏页面有**页面级**底部固定操作条
    //    (选词 BottomActionBar / 报价预览 / 采集填写 / 素材确认,见
    //    floating-stack.ts 的 MOBILE_BOTTOM_BAR_HEIGHT 注释),旧的 8px 下限
    //    允许用户把 FAB 拖到这些条上盖住主按钮 —— 默认位置让开了、手一拖又
    //    盖回去,等于「不遮挡主按钮」只在初始态成立。钳制到底部条之上 8px。
    //    (先前这里写的是 MobileTabBar,那个组件全仓零挂载,是错的依据。)
    const floor = vw < LG_BREAKPOINT ? MOBILE_BOTTOM_BAR_HEIGHT + 8 : 8
    const newX = Math.max(8, Math.min(vw - 56, startPt.current.ox - dx))
    const newY = Math.max(floor, Math.min(vh - 56, startPt.current.oy - dy))
    setPos({ x: newX, y: newY })
  }, [])

  const onPointerUp = useCallback(() => {
    dragging.current = false
    if (!moved.current) onClick()
  }, [onClick])

  // 🔴 键盘激活(xbvnext 2026-08-18)。
  //    这个按钮此前**只绑了指针事件**:tab 能聚焦上去,回车/空格却什么都不发生 ——
  //    也就是说键盘用户根本打不开小榜。验收门「键盘可达、焦点清晰」原本是不满足的。
  //    刻意用 onKeyDown 而不是改成 onClick:改 onClick 会和 onPointerUp 在鼠标路径上
  //    双触发,或者在"拖到一半松手"时丢掉激活。加一条键盘分支是纯增量,动不了鼠标路径。
  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return
    e.preventDefault()   // 空格默认会滚页
    onClick()
  }, [onClick])

  return (
    <button
      type="button"
      aria-label="打开小榜 GEO 助手"
      aria-haspopup="dialog"
      title="小榜 · GEO 助手(可拖动)"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onKeyDown={onKeyDown}
      style={{
        right: pos.x,
        bottom: `calc(${pos.y}px + env(safe-area-inset-bottom, 0px))`,
        zIndex: FLOATING_Z.FAB,
      }}
      className={cn(
        'fixed touch-none select-none',
        'w-12 h-12 rounded-full',
        'bg-foreground text-background shadow-lg',
        'flex items-center justify-center',
        'hover:scale-105 active:scale-95 transition-transform',
        // 焦点可见:键盘用户要看得见自己停在哪。ring-offset 用背景色,
        // 暗/亮两套主题下都不会糊成一团。
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        className,
      )}
    >
      <MessageCircle className="size-5" />
    </button>
  )
}
