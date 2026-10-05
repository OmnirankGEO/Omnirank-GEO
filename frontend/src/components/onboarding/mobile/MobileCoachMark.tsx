/**
 * MobileCoachMark · 移动端 spotlight 替代品 · 全屏遮罩挖洞 + 底部 sheet 解释
 * [2026-05-27 移动端教程基础设施 commit 2]
 *
 * 跟 PC 端 OnboardingSpotlight / FeatureTooltip 的关系:
 *   - PC 是浮动小气泡指 DOM 元素 · 移动端小屏 + 键盘 + sticky + 滚动会打架
 *   - 移动端改 coach mark:固定底部 sheet 解释 + SVG 挖洞高亮目标 · 不浮动
 *   - 沙盒 Step 1-4 引导 / FeatureTooltip 移动端形态 都走这个
 *
 * 硬验收(老板 2026-05-27 拍板):
 *   - iPhone SE (375x667) 下:
 *     1. 目标找不到 → 自动降级纯 bottom sheet · 不出现空遮罩
 *     2. 目标在屏外 → 自动 scrollIntoView · 若仍 < 50% 可见则降级
 *     3. 始终能关闭(底部 sheet "知道了" 按钮永远可点)
 *     4. 始终能继续(有 onNext 时 "下一步" 按钮可点)
 *
 * z-index: FLOATING_Z.MOBILE_COACH_MARK (150) · 跟 dialog 错开
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetFooter } from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { FLOATING_Z } from '@/lib/floating-stack'
import { useSandboxState } from '@/sandbox/sandboxState'
import { isMobileCoachPaused, subscribePause } from '@/sandbox/mobileCoachPauseStore'
import {
    coachRectVisibleFraction,
    getCoachViewport,
    measureCoachTarget,
    resolveCoachTarget,
    sameCoachRect,
    type CoachTargetRect,
} from '../coachTarget'

/** 沙盒 banner 高度 · py-1.5 + text-xs 单行 · 给 sheet side=top 让位 */
const SANDBOX_BANNER_HEIGHT = 36

interface MobileCoachMarkProps {
    /** 是否显示 · 父组件控制 */
    open: boolean
    /** 目标元素 DOM 引用 · 优先级高于 targetSelector · FeatureTooltip 包裹模式用这个 */
    targetEl?: HTMLElement | null
    /** 目标元素 CSS selector · 备选 · 适合 data-attribute 标记的稳定锚点 */
    targetSelector?: string
    /** 卡片标题 */
    title: string
    /** 卡片描述 · 支持 jsx · 长文本会滚动 */
    description: React.ReactNode
    /** 关闭 / 跳过 · sheet 关闭时触发 · 强制 step 场景不渲染按钮也不可下拉关 · 但 fallback 时强制渲染按钮防卡死 */
    onClose: () => void
    /** 下一步 · 信息型 step · 渲染主按钮(右)· 点了走 onNext */
    onNext?: () => void
    /** 目标暂未就绪时重新定位 */
    onRetry?: () => void
    /** "下一步" 按钮文字 · 默认 "下一步" */
    nextLabel?: string
    /** 关闭按钮文字 · null/undefined = 不渲染关闭按钮 + sheet 不可下拉关(强引导模式)
     *  fallback 时(目标找不到)无视此值 · 强制渲染 "知道了" 防卡死 */
    closeLabel?: string | null
    /** 挖洞外扩 padding · 默认 8(让按钮周围留呼吸)
     *  支持 object 各方向独立(跟 PC OnboardingSpotlight 一致)·
     *  比如 dropdown 展开时 bottom 加大让挖洞包住整个下拉列表 */
    padding?: number | { top?: number; right?: number; bottom?: number; left?: number }
    /** 强制走 bottom sheet 模式 · 不挖洞(列表/表格场景预留) */
    forceFallback?: boolean
}

export function MobileCoachMark({
    open,
    targetEl,
    targetSelector,
    title,
    description,
    onClose,
    onNext,
    onRetry,
    nextLabel = '下一步',
    closeLabel = '知道了',
    padding = 8,
    forceFallback = false,
}: MobileCoachMarkProps) {
    const [rect, setRect] = useState<CoachTargetRect | null>(null)
    const [tried, setTried] = useState(false)
    const [locateGeneration, setLocateGeneration] = useState(0)
    // [2026-05-27 fix] 沙盒 banner 在最顶 · sheet side=top 时要下沉留位 · 防 banner 被盖
    const { isSandbox } = useSandboxState()

    // padding 解析提前 · useEffect 依赖用得到
    const padT = typeof padding === 'number' ? padding : (padding?.top ?? 6)
    const padR = typeof padding === 'number' ? padding : (padding?.right ?? 6)
    const padB = typeof padding === 'number' ? padding : (padding?.bottom ?? 6)
    const padL = typeof padding === 'number' ? padding : (padding?.left ?? 6)
    // [2026-05-27 老板拍板] 显式 pause/resume · 业务侧打开 modal 调 pauseMobileCoach() ·
    //   关闭调 resumeMobileCoach() · 100% 准确 · 不依赖 DOM observer 猜
    const [paused, setPaused] = useState<boolean>(isMobileCoachPaused())
    useEffect(() => {
        const unsub = subscribePause(() => setPaused(isMobileCoachPaused()))
        return unsub
    }, [])

    // 打开时先把真实交互目标移入 visualViewport，再进行有限稳定帧测量。
    // 后续由 scroll/resize/observer 驱动，不再永久 60fps 轮询。
    useEffect(() => {
        if (!open || forceFallback) {
            setRect(null)
            setTried(false)
            return
        }

        let cancelled = false
        let raf = 0
        let stableFrames = 0
        let lastRect: CoachTargetRect | null = null
        const resolveCurrentTarget = () => resolveCoachTarget(targetEl, targetSelector)
        const tryMeasure = (): boolean => {
            if (cancelled) return false
            const measured = measureCoachTarget(resolveCurrentTarget())
            const visibleRect = measured && coachRectVisibleFraction(measured) >= 0.35
                ? measured
                : null
            setRect(previous => sameCoachRect(previous, visibleRect) ? previous : visibleRect)
            setTried(true)
            return !!visibleRect
        }

        const el = resolveCurrentTarget()
        if (el) {
            const measured = measureCoachTarget(el)
            if (!measured || coachRectVisibleFraction(measured) < 0.6) {
                el.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' })
            }
        }

        const tick = () => {
            if (cancelled) return
            const measured = measureCoachTarget(resolveCurrentTarget())
            if (sameCoachRect(lastRect, measured)) stableFrames += 1
            else stableFrames = 0
            lastRect = measured
            tryMeasure()
            if (stableFrames < 4) raf = window.requestAnimationFrame(tick)
        }
        raf = window.requestAnimationFrame(tick)

        const remeasure = () => { tryMeasure() }
        window.addEventListener('scroll', remeasure, { passive: true, capture: true })
        window.addEventListener('resize', remeasure)
        window.visualViewport?.addEventListener('resize', remeasure)
        window.visualViewport?.addEventListener('scroll', remeasure)

        let ro: ResizeObserver | null = null
        if (el && typeof ResizeObserver !== 'undefined') {
            ro = new ResizeObserver(remeasure)
            ro.observe(el)
        }
        const mo = new MutationObserver(remeasure)
        mo.observe(document.body, {
            attributes: true,
            childList: true,
            subtree: true,
            attributeFilter: ['class', 'style', 'disabled', 'aria-disabled'],
        })

        return () => {
            cancelled = true
            cancelAnimationFrame(raf)
            window.removeEventListener('scroll', remeasure, { capture: true })
            window.removeEventListener('resize', remeasure)
            window.visualViewport?.removeEventListener('resize', remeasure)
            window.visualViewport?.removeEventListener('scroll', remeasure)
            ro?.disconnect()
            mo.disconnect()
        }
    }, [forceFallback, locateGeneration, open, targetEl, targetSelector])

    if (!open) return null
    // 业务侧调 pauseMobileCoach() 时 hide · 关闭 modal 后调 resumeMobileCoach() 恢复
    if (paused) return null

    // [2026-05-27 fix] rect 为空立刻 fallback(渲染全屏黑罩)· 防"空 UI"
    //   原 isFallback = forceFallback || (tried && !rect) · tried=false 时 rect 仍 null 会两边不渲染
    //   现在:任何 rect=null 都走 fallback · 350ms 后测出 rect 切到挖洞
    const isFallback = forceFallback || !rect
    const showHole = !!rect && !isFallback
    // hardFallback = 真找不到目标(测过仍 null)· 这种才强制 "知道了" 防卡死
    //   过渡态(还没 tried)closeLabel 保留 null(强引导)· 不让用户误点
    const isHardFallback = forceFallback || (tried && !rect)

    // [2026-05-27 fix] sheet 位置自适应:目标在下半屏 → sheet 从顶部下滑 · 不挡目标
    //   反则 sheet 默认底部 · 也不会跟顶部 SandboxBanner 撞
    //   fallback(无 rect)默认底部 · 跟原行为一致
    const sheetSide: 'top' | 'bottom' = (() => {
        if (!rect) return 'bottom'
        const viewport = getCoachViewport()
        const targetCenter = rect.top + rect.height / 2
        return targetCenter > viewport.top + viewport.height / 2 ? 'top' : 'bottom'
    })()

    // [2026-05-27 fix] PC 强引导对齐 · closeLabel === null/undefined → 不渲染关闭按钮 · sheet 不可下拉关
    //   但 fallback 时必须给出口 · 否则用户没挖洞看不到目标 · 卡死
    //   fallback 强制 closeLabel "知道了" + sheet 可关
    const effectiveCloseLabel = isHardFallback ? (closeLabel || '知道了') : closeLabel
    const canCloseViaSheet = isHardFallback || effectiveCloseLabel != null
    const handleSheetOpenChange = (o: boolean) => {
        if (o) return
        if (canCloseViaSheet) onClose()
        // 强引导模式(canCloseViaSheet=false) · 忽略关闭尝试 · sheet 不消失
    }

    // [2026-05-28 真正根因] 父级有 backdrop-filter (eg. sticky bottom 购物车栏的 backdrop-blur)
    //   会创建新的 containing block · 内部 position:fixed 不再按 viewport 定位 ·
    //   而是按这个 containing block 定位 · 挖洞坐标偏移到下方白块位置
    // 修法:整个 MobileCoachMark 通过 createPortal 渲染到 document.body ·
    //   跟 PC OnboardingSpotlight 架构对齐 · fixed 始终按 viewport 定位
    if (typeof document === 'undefined') return null
    return createPortal(
        <>
            {/* 视觉层只负责聚焦，不拦截页面点击。目标轻微位移时也不会吞掉真实按钮。 */}
            {showHole && (() => {
                const holeLeft = Math.floor(rect!.left - padL)
                const holeTop = Math.floor(rect!.top - padT)
                const holeRight = Math.ceil(rect!.left + rect!.width + padR)
                const holeBottom = Math.ceil(rect!.top + rect!.height + padB)
                const holeWidth = holeRight - holeLeft
                const holeHeight = holeBottom - holeTop
                return (
                    <>
                        <div
                            className="fixed pointer-events-none rounded-lg"
                            style={{
                                left: holeLeft,
                                top: holeTop,
                                width: holeWidth,
                                height: holeHeight,
                                boxShadow: '0 0 0 9999px rgba(0, 0, 0, 0.55)',
                                zIndex: FLOATING_Z.MOBILE_COACH_MARK,
                            }}
                            aria-hidden
                        />
                        <div
                            className="fixed pointer-events-none rounded-lg ring-2 ring-amber-500"
                            style={{
                                left: holeLeft,
                                top: holeTop,
                                width: holeWidth,
                                height: holeHeight,
                                zIndex: FLOATING_Z.MOBILE_COACH_MARK + 1,
                            }}
                            aria-hidden
                        />
                    </>
                )
            })()}

            {/* fallback 同样只做视觉提示，避免定位失败把业务页面锁死。 */}
            {isFallback && (
                <div
                    aria-hidden="true"
                    className="fixed inset-0 pointer-events-none"
                    style={{
                        background: 'rgba(0, 0, 0, 0.5)',
                        zIndex: FLOATING_Z.MOBILE_COACH_MARK,
                    }}
                />
            )}

            {/* 底部 sheet · 始终在 · 哪怕挖不出洞也能看解释 + 关闭 + 下一步
                [2026-05-27 fix] hideOverlay · 不要 Sheet 内置 bg-black/10 + backdrop-blur
                    叠加 SVG 50% 黑遮罩会变得鸡毛都看不到 · 让 SVG 独占遮罩控制
                max-h-[60dvh]:留 40% 屏幕给挖洞 · 内容多则滚动
                [2026-05-27 fix] 按钮去重:closeLabel === nextLabel (如都是"知道了") 时 ·
                    只渲染右侧主按钮 · 点了走 onNext 推进 stage · 避免视觉重复 */}
            {/* [2026-05-27 fix] modal={false} · base-ui Dialog 默认 modal=true 会拦外部 pointer-events ·
                即使 hideOverlay 也拦 · 导致挖洞高亮的目标元素点不响应 · 强引导卡死
                modal=false 让目标元素可点 · sheet 关闭仍通过 handleSheetOpenChange 控制(强引导模式拦) */}
            <Sheet open={open} onOpenChange={handleSheetOpenChange} modal={false}>
                <SheetContent
                    side={sheetSide}
                    /* [2026-05-27 老板报 sheet 占屏太多] 整体收紧:
                       max-h 50dvh → 35dvh · 给主屏 65% 操作空间
                       Header / Footer 内边距 p-4 → p-3 · 减 16px 内空间 */
                    className={
                        sheetSide === 'top'
                            ? 'max-h-[35dvh] overflow-y-auto rounded-b-2xl pt-[max(0.25rem,env(safe-area-inset-top))] [&>[data-slot=sheet-header]]:p-3 [&>[data-slot=sheet-footer]]:p-3'
                            : 'max-h-[35dvh] overflow-y-auto rounded-t-2xl [&>[data-slot=sheet-header]]:p-3 [&>[data-slot=sheet-footer]]:p-3'
                    }
                    showCloseButton={false}
                    hideOverlay
                    /* zIndex 低于 SANDBOX_BANNER (180)· 确保 banner 永远在最顶 · 退出沙盒按钮始终可点
                       side=top + 沙盒态 · top 下沉 36px 给 banner 让位 · 不被盖 */
                    style={{
                        zIndex: FLOATING_Z.MOBILE_COACH_MARK + 5,
                        ...(sheetSide === 'top' && isSandbox ? { top: SANDBOX_BANNER_HEIGHT } : {}),
                    }}
                >
                    <SheetHeader>
                        {/* 标题字号缩到 sm · 紧凑 */}
                        <SheetTitle className="text-sm">{title}</SheetTitle>
                        {/* 描述字号 sm → xs · 行距 6 → 5 · 紧凑 */}
                        <div className="text-xs leading-5 text-muted-foreground">{description}</div>
                        {isHardFallback && (
                            <p className="text-[11px] leading-4 text-amber-600 dark:text-amber-400">
                                当前按钮还没有准备好。你可以重新定位，或先跳过这一步继续体验。
                            </p>
                        )}
                    </SheetHeader>
                    <SheetFooter className="flex-row justify-end gap-2 pt-1">
                        {isHardFallback && (
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={() => {
                                    setTried(false)
                                    setLocateGeneration(value => value + 1)
                                    onRetry?.()
                                }}
                                className="min-h-9"
                            >
                                重新定位
                            </Button>
                        )}
                        {/* 关闭按钮:effectiveCloseLabel 存在才渲染 · null 时不渲染(强引导)
                            跟 nextLabel 撞名也不渲染(去重) */}
                        {effectiveCloseLabel != null && (!onNext || effectiveCloseLabel !== nextLabel) && (
                            <Button variant="ghost" size="sm" onClick={onClose} className="min-h-9">
                                {effectiveCloseLabel}
                            </Button>
                        )}
                        {onNext && (
                            <Button size="sm" onClick={onNext} className="min-h-9">
                                {nextLabel}
                            </Button>
                        )}
                        {/* 强引导兜底文案 · 没任何按钮时显示 · 告诉用户去点高亮 */}
                        {effectiveCloseLabel == null && !onNext && (
                            <span className="text-[11px] text-muted-foreground self-center pr-1">
                                请点高亮区域继续
                            </span>
                        )}
                    </SheetFooter>
                </SheetContent>
            </Sheet>
        </>,
        document.body
    )
}
