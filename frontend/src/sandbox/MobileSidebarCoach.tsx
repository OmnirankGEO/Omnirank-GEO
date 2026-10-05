/**
 * MobileSidebarCoach · 移动端沙盒 4 step sidebar 引导 · 2 阶段
 * [2026-05-27 B 方案 · 老板拍板]
 *
 * 背景:
 *   PC 端 sidebar 引导用户点"品牌体检/报价方案/写文章/监测"4 项 ·
 *   移动端 sidebar 收在汉堡菜单 · sidebar 项 DOM 不挂出来 · 无法直接挖洞高亮
 *
 * 修法 · 2 阶段:
 *   阶段 1 (sidebar 未开):挖洞高亮汉堡按钮 · sheet "第 N 步: 打开菜单"
 *   用户点汉堡 → sidebar 展开
 *   阶段 2 (sidebar 已开):挖洞高亮 sidebar 内对应项 · sheet "去 XX"
 *   用户点 → AppSidebar handleClick 推进 stage + 关 sidebar + navigate
 *
 * 跟 PC 体验对齐:用户感觉是自己走的路 · 不是被强制跳转
 *
 * 挂载点:Layout.tsx 内 · 只 isMobile + sandboxUI + 4 sidebar stage 才渲染
 */
import { MobileCoachMark } from '@/components/onboarding/mobile/MobileCoachMark'
import { useTutorialStage } from '@/sandbox/tutorialStage'
import { useSandboxState } from '@/sandbox/sandboxState'
import { useScreenshotMode } from '@/sandbox/screenshotMode'
import { useIsMobile } from '@/hooks/use-mobile'
import { useSidebar } from '@/components/ui/sidebar'
import { getSandboxTutorialNavItem } from '@/sandbox/tutorialLayoutContract'

export function MobileSidebarCoach() {
    const tutorialStage = useTutorialStage()
    const { isSandbox } = useSandboxState()
    const isMobile = useIsMobile()
    const screenshotMode = useScreenshotMode()
    const { openMobile, setOpenMobile } = useSidebar()

    const config = getSandboxTutorialNavItem(tutorialStage)

    // 不在 4 sidebar stage / 非沙盒 / 桌面 / 截图模式 · 不渲染
    if (!config || !isMobile || !isSandbox || screenshotMode) return null

    // 阶段 1:sidebar 未打开 · 引导用户点汉堡
    if (!openMobile) {
        return (
            <MobileCoachMark
                open
                targetSelector='[data-mobile-coach="hamburger"]'
                title={`第 ${config.stepNumber} 步 · 打开菜单`}
                description={
                    <>
                        点<b>左上角菜单图标</b>打开侧边导航 · 找到「<b>{config.label}</b>」
                        <br /><span className="text-muted-foreground">{config.content}</span>
                    </>
                }
                /* fallback 兜底:汉堡按钮一般稳定挂 DOM · onClose 真做点事防万一 ·
                 *   关 sheet 不退引导 · 用户重新打开汉堡也能重弹(stage 没变) */
                onClose={() => { /* 强引导 · noop · 不让 sheet 真消失 */ }}
                closeLabel={null}
            />
        )
    }

    // 阶段 2:sidebar 已打开 · 引导点 sidebar 内对应项
    return (
        <MobileCoachMark
            open
            targetSelector={`[data-mobile-coach="sidebar-${config.stepId}"]`}
            title={`第 ${config.stepNumber} 步 · 点这里去${config.label}`}
            description={config.content}
            /* 阶段 2 用户可以关 sidebar 回阶段 1 · 提供出口 */
            onClose={() => setOpenMobile(false)}
            closeLabel={null}
        />
    )
}

export default MobileSidebarCoach
