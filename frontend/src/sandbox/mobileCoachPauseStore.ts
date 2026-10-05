/**
 * MobileCoachPauseStore · 业务代码显式暂停/恢复移动端教程遮罩
 * [2026-05-27 老板拍板]
 *
 * 用法:
 *   import { pauseMobileCoach, resumeMobileCoach } from '@/sandbox/mobileCoachPauseStore'
 *
 *   // 业务侧打开一个会挡教程的 modal (如 WritingHall 文章预览):
 *   pauseMobileCoach()
 *   // ... 业务操作
 *
 *   // modal 关闭时恢复:
 *   resumeMobileCoach()
 *
 * 为啥这样设计 · 不用 dialog DOM observer / document click 检测:
 *   - DOM observer 依赖第三方 modal 必须 role="dialog" data-state="open" · 不可靠
 *   - document click 检测依赖测目标 rect 位置 · DOM 变动后位置不准 · 误判多
 *   - 显式 API · 业务方知道自己开了 modal · 直接 pause · 关闭时 resume · 100% 准确
 *
 * 安全机制:
 *   - resume() 把 paused 重置为 false · 永远能从 paused 状态恢复
 *   - 调用方 pause 后忘 resume · 用户可以通过点屏幕等任意路径触发 resume · 防"永久消失"
 */

type Listener = () => void

let paused = false
const listeners = new Set<Listener>()

function emit() {
    listeners.forEach((l) => l())
}

/** 暂停 MobileCoachMark · 业务侧打开 modal 之前调 */
export function pauseMobileCoach() {
    if (paused) return
    paused = true
    emit()
}

/** 恢复 MobileCoachMark · 业务侧关闭 modal 后调 · 多次调安全 */
export function resumeMobileCoach() {
    if (!paused) return
    paused = false
    emit()
}

/** 当前是否 paused · 让 MobileCoachMark 内部 hook 读 */
export function isMobileCoachPaused(): boolean {
    return paused
}

/** 订阅状态变化 · MobileCoachMark useEffect 用 */
export function subscribePause(listener: Listener): () => void {
    listeners.add(listener)
    return () => listeners.delete(listener)
}
