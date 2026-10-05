/**
 * MobileHelpSheet · HelpHint 移动端形态 · 点问号弹底部 sheet
 * [2026-05-27 移动端教程基础设施 commit 2]
 *
 * 跟 PC HelpHint 关系:
 *   - PC 是 popover(点问号弹气泡 · 跟着按钮浮动)· 移动端易被键盘 / 屏幕边缘截断
 *   - 移动端改 bottom sheet · 不浮动 · 内容可滚 · 关闭明确
 *   - HelpHint 内部按 useIsMobile 分发:PC → 原 popover · 移动 → 本组件
 *   - HelpHint 的 PC 行为不动(老板拍板硬约束)
 *
 * 用法:
 *   <MobileHelpSheet
 *     open={open}
 *     onClose={() => setOpen(false)}
 *     title="出现率是什么意思?"
 *   >
 *     出现率 = 在 AI 里搜这个词时, 你的品牌被 AI 提到/推荐的比例 ...
 *   </MobileHelpSheet>
 */
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { FLOATING_Z } from '@/lib/floating-stack'

interface MobileHelpSheetProps {
    /** 是否打开 · 父组件控制 */
    open: boolean
    /** 关闭 · sheet 内置右上 X + 滑下手势都会触发 */
    onClose: () => void
    /** sheet 标题 · 一般是"XX 是什么意思?" 之类 */
    title: string
    /** 解释内容 · 支持 jsx 富文本(<b>/<br/>/<ul>/...) */
    children: React.ReactNode
}

export function MobileHelpSheet({ open, onClose, title, children }: MobileHelpSheetProps) {
    return (
        <Sheet open={open} onOpenChange={(o) => { if (!o) onClose() }}>
            <SheetContent
                side="bottom"
                /* max-h-[80dvh]:老板拍板上限 · 大部分 HelpHint 内容 1-2 段 · 实际占 ~30dvh
                   overflow-y-auto:超长解释滚动 · 不撑爆屏幕
                   pb-safe:留 iOS safe-area-inset-bottom 防 Home Indicator 挡按钮 */
                className="max-h-[80dvh] overflow-y-auto rounded-t-2xl pb-[max(1rem,env(safe-area-inset-bottom))]"
                style={{ zIndex: FLOATING_Z.POPOVER }}
            >
                <SheetHeader>
                    <SheetTitle>{title}</SheetTitle>
                    {/* 不用 SheetDescription(base-ui 不支持 asChild)· 直接 div · 富文本可塞 */}
                    <div className="text-sm leading-6 text-muted-foreground">
                        {children}
                    </div>
                </SheetHeader>
            </SheetContent>
        </Sheet>
    )
}
