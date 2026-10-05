/**
 * sidebar.tsx — shadcn sidebar primitives (compatible implementation)
 * Implements group/data-collapsible/data-state pattern used by AppSidebar.
 */

import React, {
  createContext, useContext, useState, useCallback, useEffect,
  type ReactNode, type HTMLAttributes, type ButtonHTMLAttributes,
} from 'react';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/use-mobile';

// ─── Context ──────────────────────────────────────────────────────────────────

type SidebarState = 'expanded' | 'collapsed';

interface SidebarContextType {
  state: SidebarState;
  open: boolean;
  setOpen: (v: boolean) => void;
  openMobile: boolean;
  setOpenMobile: (v: boolean) => void;
  isMobile: boolean;
  toggleSidebar: () => void;
}

const SidebarCtx = createContext<SidebarContextType>({
  state: 'expanded',
  open: true, setOpen: () => {},
  openMobile: false, setOpenMobile: () => {},
  isMobile: false, toggleSidebar: () => {},
});

export function SidebarProvider({
  children,
  defaultOpen = true,
}: {
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [openMobile, setOpenMobile] = useState(false);
  const isMobile = useIsMobile();

  // Close mobile drawer when switching to desktop
  useEffect(() => {
    if (!isMobile) setOpenMobile(false);
  }, [isMobile]);

  const toggleSidebar = useCallback(() => {
    if (isMobile) setOpenMobile(v => !v);
    else setOpen(v => !v);
  }, [isMobile]);

  const state: SidebarState = open ? 'expanded' : 'collapsed';

  return (
    <SidebarCtx.Provider value={{ state, open, setOpen, openMobile, setOpenMobile, isMobile, toggleSidebar }}>
      <div
        className="flex h-[100dvh] w-full overflow-hidden"
        style={{
          '--sidebar-width': '16rem',
          '--sidebar-width-icon': '3.5rem',
        } as React.CSSProperties}
      >
        {children}
      </div>
    </SidebarCtx.Provider>
  );
}

export function useSidebar() { return useContext(SidebarCtx); }

// ─── Sidebar shell ────────────────────────────────────────────────────────────

export function Sidebar({
  children,
  className,
  collapsible = 'offcanvas',
  ...props
}: HTMLAttributes<HTMLDivElement> & { collapsible?: 'offcanvas' | 'icon' | 'none' }) {
  const { state, isMobile, openMobile, setOpenMobile } = useSidebar();
  const isCollapsed = state === 'collapsed';

  // 移动端：完全隐藏，汉堡菜单/手势唤出抽屉 + 遮罩
  if (isMobile) {
    return (
      <>
        {openMobile && (
          <button
            type="button"
            aria-label="关闭主菜单"
            className="fixed inset-0 z-40 bg-black/50"
            onClick={() => setOpenMobile(false)}
          />
        )}
        {openMobile && (
          <aside
            data-state="expanded"
            className={cn(
              'group fixed inset-y-0 left-0 z-50 flex h-[100dvh] max-h-[100dvh] flex-col bg-card border-r border-border overflow-hidden',
              'w-[var(--sidebar-width,16rem)]',
              className,
            )}
            {...props}
          >
            {children}
          </aside>
        )}
      </>
    );
  }

  // 桌面端：图标折叠 / 完全隐藏 / 常驻
  return (
    <aside
      data-state={state}
      data-collapsible={isCollapsed ? collapsible : ''}
      className={cn(
        'group flex flex-col h-full shrink-0 bg-card border-r border-border overflow-hidden',
        'transition-[width] duration-200 ease-linear',
        isCollapsed
          ? collapsible === 'icon'
            ? 'w-[var(--sidebar-width-icon,3.5rem)]'
            : 'w-0 border-r-0'
          : 'w-[var(--sidebar-width,16rem)]',
        className,
      )}
      {...props}
    >
      {children}
    </aside>
  );
}

export function SidebarInset({ children, className, ...props }: HTMLAttributes<HTMLElement>) {
  return (
    <main className={cn('flex flex-col flex-1 min-w-0 overflow-hidden', className)} {...props}>
      {children}
    </main>
  );
}

// ─── Structural sections ──────────────────────────────────────────────────────

export function SidebarHeader({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex flex-col gap-2 p-2', className)} {...props}>{children}</div>;
}

export function SidebarContent({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn('flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto overflow-x-hidden py-1', className)} {...props}>
      {children}
    </div>
  );
}

export function SidebarFooter({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex flex-col gap-2 p-2', className)} {...props}>{children}</div>;
}

// ─── Groups ───────────────────────────────────────────────────────────────────

export function SidebarGroup({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn('relative flex w-full min-w-0 flex-col px-2 py-0.5', className)} {...props}>
      {children}
    </div>
  );
}

export function SidebarGroupLabel({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex h-8 shrink-0 items-center rounded-md px-2 text-xs font-medium text-muted-foreground',
        'group-data-[collapsible=icon]:hidden',
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}

export function SidebarGroupContent({ children, className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('w-full', className)} {...props}>{children}</div>;
}

// ─── Menu ─────────────────────────────────────────────────────────────────────

export function SidebarMenu({ children, className, ...props }: HTMLAttributes<HTMLUListElement>) {
  return <ul className={cn('flex w-full min-w-0 flex-col gap-0.5', className)} {...props}>{children}</ul>;
}

export function SidebarMenuItem({ children, className, ...props }: HTMLAttributes<HTMLLIElement>) {
  return <li className={cn('group/menu-item relative', className)} {...props}>{children}</li>;
}

export function SidebarMenuButton({
  children,
  className,
  asChild,
  isActive,
  tooltip,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  asChild?: boolean;
  isActive?: boolean;
  tooltip?: string;
}) {
  const baseClass = cn(
    'peer/menu-button flex w-full min-h-[44px] items-center gap-2 overflow-hidden rounded-md px-2.5 py-2 text-sm',
    'text-foreground outline-none ring-sidebar-ring transition-colors duration-150',
    'hover:bg-muted focus-visible:ring-2',
    isActive && 'bg-muted font-medium',
    'group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:size-9 group-data-[collapsible=icon]:px-0',
    '[&>span:last-child]:truncate group-data-[collapsible=icon]:[&>span:last-child]:hidden',
    '[&>svg]:size-4 [&>svg]:shrink-0',
    className,
  );

  if (asChild) {
    // Clone the single child (NavLink/a) and merge button styles directly onto it
    const child = children as React.ReactElement<{
      className?: string;
      'aria-label'?: string;
      title?: string;
    }>;
    if (child && React.isValidElement(child)) {
      return React.cloneElement(child, {
        className: cn(baseClass, (child.props as { className?: string }).className),
        'aria-label': child.props['aria-label'] || tooltip,
        title: child.props.title || tooltip,
      } as any);
    }
    return <div className={baseClass}>{children}</div>;
  }

  return (
    <button
      className={baseClass}
      aria-label={props['aria-label'] || tooltip}
      title={props.title || tooltip}
      {...props}
    >
      {children}
    </button>
  );
}

// ─── Decorative ───────────────────────────────────────────────────────────────

export function SidebarRail({ className, ...props }: HTMLAttributes<HTMLButtonElement>) {
  const { toggleSidebar } = useSidebar();
  return (
    <button
      aria-label="切换侧边栏"
      tabIndex={-1}
      onClick={toggleSidebar}
      title="切换侧边栏"
      className={cn(
        'absolute inset-y-0 z-20 hidden w-4 -translate-x-1/2 transition-all ease-linear',
        'after:absolute after:inset-y-0 after:left-1/2 after:w-[2px]',
        'hover:after:bg-sidebar-border group-data-[side=left]:-right-4',
        'sm:flex',
        className,
      )}
      {...(props as any)}
    />
  );
}

export function SidebarSeparator({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('mx-2 my-0.5 h-px bg-border group-data-[collapsible=icon]:mx-0', className)}
      {...props}
    />
  );
}

// ─── SidebarTrigger ─ 顶部栏触发按钮（移动端汉堡菜单 / 桌面端折叠按钮）─────
export function SidebarTrigger({ className, ...props }: HTMLAttributes<HTMLButtonElement>) {
  const { toggleSidebar } = useSidebar();
  return (
    <button
      type="button"
      aria-label="打开或收起主菜单"
      onClick={toggleSidebar}
      className={cn(
        'inline-flex h-11 w-11 items-center justify-center rounded-md',
        'text-foreground/70 hover:bg-accent hover:text-accent-foreground',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        className,
      )}
      {...(props as any)}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="3" y1="6" x2="21" y2="6" />
        <line x1="3" y1="12" x2="21" y2="12" />
        <line x1="3" y1="18" x2="21" y2="18" />
      </svg>
    </button>
  );
}
