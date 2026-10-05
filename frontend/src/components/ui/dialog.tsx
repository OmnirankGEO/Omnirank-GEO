"use client"

import * as React from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { X } from "lucide-react"

import { cn } from "@/lib/utils"

const Dialog = DialogPrimitive.Root

const DialogTrigger = DialogPrimitive.Trigger

const DialogPortal = DialogPrimitive.Portal

const DialogClose = DialogPrimitive.Close

const DialogOverlay = React.forwardRef<
    React.ElementRef<typeof DialogPrimitive.Overlay>,
    React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
    <DialogPrimitive.Overlay
        ref={ref}
        className={cn(
            // 2026-05-09: 更深的遮罩 + 更柔的 blur · 让 dialog 视觉浮起来更明显
            "fixed inset-0 z-50 bg-black/65 backdrop-blur-md data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
            className
        )}
        {...props}
    />
))
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName

interface DialogContentProps extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
    /** 2026-05-09: 自定义了 close 按钮的 dialog 设 true 防出双 X · 社媒画像飞轮弹窗 / 全屏 chat 等场景 */
    hideCloseButton?: boolean
}

function hasDialogDescription(children: React.ReactNode): boolean {
    const descriptionDisplayName = DialogPrimitive.Description.displayName
    return React.Children.toArray(children).some((child) => {
        if (!React.isValidElement(child)) return false
        const type = child.type as { displayName?: string }
        if (type === DialogPrimitive.Description
            || (descriptionDisplayName && type.displayName === descriptionDisplayName)) return true
        return hasDialogDescription((child.props as { children?: React.ReactNode }).children)
    })
}

const DialogContent = React.forwardRef<
    React.ElementRef<typeof DialogPrimitive.Content>,
    DialogContentProps
>(({ className, children, hideCloseButton = false, ...props }, ref) => {
    const containsDescription = hasDialogDescription(children)
    const hasCustomDescribedBy = Object.prototype.hasOwnProperty.call(props, "aria-describedby")
    const descriptionOptOut = !containsDescription && !hasCustomDescribedBy
        ? { "aria-describedby": undefined }
        : {}
    return <DialogPortal>
        <DialogOverlay />
        <DialogPrimitive.Content
            ref={ref}
            className={cn(
                // 2026-05-09 老板 reported "弹窗样式简陋" · 升级:
                //   - shadow:double-stop(深 + 浅)有物理悬浮感 · 替代 shadow-2xl 单层
                //   - border:opacity 60(原 50)· 更明确分割
                //   - bg:97%(原 95%)· 让背后透出更少防内容浑浊
                //   - rounded-[20px]:介于 2xl 和 3xl 之间 · 现代感
                //   - padding:p-6 sm:p-7 · 更舒展(原 5/6)· 移动端 close 按钮也不挤
                //   - gap-5(原 4)· 标题 / 描述 / 内容 / footer 之间呼吸感更足
                "fixed left-[50%] top-[50%] z-50 grid w-[calc(100%-2rem)] max-w-lg translate-x-[-50%] translate-y-[-50%] gap-5 border border-border/60 bg-card/97 backdrop-blur-2xl p-6 sm:p-7 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.45),0_8px_24px_-4px_rgba(0,0,0,0.22)] rounded-[20px] duration-200 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 data-[state=closed]:slide-out-to-left-1/2 data-[state=closed]:slide-out-to-top-[48%] data-[state=open]:slide-in-from-left-1/2 data-[state=open]:slide-in-from-top-[48%]",
                className
            )}
            {...props}
            {...descriptionOptOut}
        >
            {children}
            {/* close 按钮:48px 触控区 · 圆角 lg · hover 时 muted bg + foreground */}
            {!hideCloseButton && (
                <DialogPrimitive.Close className="absolute right-2 top-2 grid h-12 w-12 place-items-center rounded-lg text-muted-foreground/75 transition hover:bg-muted hover:text-foreground focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none">
                    <X className="h-4 w-4" />
                    <span className="sr-only">关闭弹窗</span>
                </DialogPrimitive.Close>
            )}
        </DialogPrimitive.Content>
    </DialogPortal>
})
DialogContent.displayName = DialogPrimitive.Content.displayName

const DialogHeader = ({
    className,
    ...props
}: React.HTMLAttributes<HTMLDivElement>) => (
    <div
        className={cn(
            // 2026-05-09: pr-8 让标题不和 close 按钮挤;space-y-2 标题/描述间气;text-left 中文场景更稳(原 sm:text-left 移动端居中错位)
            "flex flex-col space-y-2 pr-8 text-left",
            className
        )}
        {...props}
    />
)
DialogHeader.displayName = "DialogHeader"

const DialogFooter = ({
    className,
    ...props
}: React.HTMLAttributes<HTMLDivElement>) => (
    <div
        className={cn(
            // gap-2 替代 sm:space-x-2 · flex-wrap 防按钮多挤换行;mt-1 微调和 content 间距
            "mt-1 flex flex-col-reverse gap-2 sm:flex-row sm:flex-wrap sm:justify-end",
            className
        )}
        {...props}
    />
)
DialogFooter.displayName = "DialogFooter"

const DialogTitle = React.forwardRef<
    React.ElementRef<typeof DialogPrimitive.Title>,
    React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
    <DialogPrimitive.Title
        ref={ref}
        className={cn(
            // 中文 leading-none 容易顶头,改 leading-snug 视觉舒展;tracking 减弱(中文不需要紧字距)
            "text-base sm:text-[17px] font-semibold leading-snug text-foreground",
            className
        )}
        {...props}
    />
))
DialogTitle.displayName = DialogPrimitive.Title.displayName

const DialogDescription = React.forwardRef<
    React.ElementRef<typeof DialogPrimitive.Description>,
    React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
    <DialogPrimitive.Description
        ref={ref}
        // 中文 leading-relaxed(1.625)更顺眼 · text-[13px] 略小做 hint 角色更鲜明
        className={cn("text-[13px] leading-relaxed text-muted-foreground", className)}
        {...props}
    />
))
DialogDescription.displayName = DialogPrimitive.Description.displayName

export {
    Dialog,
    DialogPortal,
    DialogOverlay,
    DialogClose,
    DialogTrigger,
    DialogContent,
    DialogHeader,
    DialogFooter,
    DialogTitle,
    DialogDescription,
}
