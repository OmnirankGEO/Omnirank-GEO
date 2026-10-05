import * as React from "react"
import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const buttonVariants = cva(
  "group/button inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-lg border border-transparent bg-clip-padding text-sm font-medium whitespace-nowrap transition-all outline-none select-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 active:not-aria-[haspopup]:translate-y-px disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40 sm:min-h-0 sm:min-w-0 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground [a]:hover:bg-primary/80",
        outline:
          "border-border bg-background hover:bg-muted hover:text-foreground aria-expanded:bg-muted aria-expanded:text-foreground dark:border-input dark:bg-input/30 dark:hover:bg-input/50",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-secondary/80 aria-expanded:bg-secondary aria-expanded:text-secondary-foreground",
        ghost:
          "hover:bg-muted hover:text-foreground aria-expanded:bg-muted aria-expanded:text-foreground dark:hover:bg-muted/50",
        destructive:
          "bg-destructive/10 text-destructive hover:bg-destructive/20 focus-visible:border-destructive/40 focus-visible:ring-destructive/20 dark:bg-destructive/20 dark:hover:bg-destructive/30 dark:focus-visible:ring-destructive/40",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        default:
          "h-8 gap-1.5 px-2.5 has-data-[icon=inline-end]:pr-2 has-data-[icon=inline-start]:pl-2",
        xs: "h-6 gap-1 rounded-[min(var(--radius-md),10px)] px-2 text-xs in-data-[slot=button-group]:rounded-lg has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 [&_svg:not([class*='size-'])]:size-3",
        sm: "h-7 gap-1 rounded-[min(var(--radius-md),12px)] px-2.5 text-[0.8rem] in-data-[slot=button-group]:rounded-lg has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 [&_svg:not([class*='size-'])]:size-3.5",
        lg: "h-9 gap-1.5 px-2.5 has-data-[icon=inline-end]:pr-2 has-data-[icon=inline-start]:pl-2",
        icon: "size-8",
        "icon-xs":
          "size-6 rounded-[min(var(--radius-md),10px)] in-data-[slot=button-group]:rounded-lg [&_svg:not([class*='size-'])]:size-3",
        "icon-sm":
          "size-7 rounded-[min(var(--radius-md),12px)] in-data-[slot=button-group]:rounded-lg",
        "icon-lg": "size-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

type ButtonProps = ButtonPrimitive.Props & VariantProps<typeof buttonVariants> & {
  /** [CTO-15.9 build 修] · 复用 Radix Slot 模式让 children(如 Link)继承 button 样式 */
  asChild?: boolean
}

const ICON_BUTTON_LABELS: Record<string, string> = {
  X: "关闭",
  Menu: "打开菜单",
  Search: "搜索",
  RefreshCw: "刷新",
  RotateCw: "重试",
  Plus: "新增",
  Minus: "减少",
  Edit: "编辑",
  Edit2: "编辑",
  Pencil: "编辑",
  Trash: "删除",
  Trash2: "删除",
  Copy: "复制",
  Download: "下载",
  Upload: "上传",
  Eye: "查看",
  EyeOff: "隐藏",
  Settings: "设置",
  MoreHorizontal: "更多操作",
  MoreVertical: "更多操作",
  ChevronLeft: "上一项",
  ChevronRight: "下一项",
  ChevronUp: "收起",
  ChevronDown: "展开",
  ArrowLeft: "返回",
  ArrowRight: "继续",
}

function inferIconButtonLabel(children: React.ReactNode): string | undefined {
  const nodes = React.Children.toArray(children)
  for (const node of nodes) {
    if (!React.isValidElement(node)) continue
    const type = node.type as { displayName?: string; name?: string }
    const iconName = type.displayName || type.name
    if (iconName && ICON_BUTTON_LABELS[iconName]) return ICON_BUTTON_LABELS[iconName]
  }
  return undefined
}

function hasReadableText(node: React.ReactNode): boolean {
  if (typeof node === "string" || typeof node === "number") return String(node).trim().length > 0
  if (React.isValidElement(node)) {
    return hasReadableText((node.props as { children?: React.ReactNode }).children)
  }
  return React.Children.toArray(node).some(hasReadableText)
}

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: ButtonProps) {
  const suppliedLabel = props["aria-label"]
  const contentHasText = hasReadableText(props.children)
  const accessibleLabel = suppliedLabel
    || (!contentHasText && typeof props.title === "string" ? props.title : undefined)
    || (!contentHasText ? inferIconButtonLabel(props.children) : undefined)
  const accessibleProps = accessibleLabel && !suppliedLabel
    ? { ...props, "aria-label": accessibleLabel }
    : props
  if (asChild) {
    // Slot 把 className + children 合并到 children 元素 · 不渲染 ButtonPrimitive
    return (
      <Slot
        data-slot="button"
        className={cn(buttonVariants({ variant, size, className }))}
        {...(accessibleProps as React.HTMLAttributes<HTMLElement>)}
      />
    )
  }
  return (
    <ButtonPrimitive
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...accessibleProps}
    />
  )
}

export { Button, buttonVariants }
