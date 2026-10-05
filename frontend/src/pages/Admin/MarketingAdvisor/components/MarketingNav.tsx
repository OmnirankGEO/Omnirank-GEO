// 营销军师控制台 · 左侧导航(主题感知 · 语义 token 自动亮暗 · 可折叠)
import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import {
  LayoutDashboard, ClipboardList, Megaphone, SlidersHorizontal,
  Image as ImageIcon, BarChart3, Settings, PanelLeftClose, PanelLeft,
  ArrowLeftToLine,
  type LucideIcon,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import type { SectionKey } from '../types'

interface NavItem { key: SectionKey; label: string; icon: LucideIcon }

const NAV_ITEMS: readonly NavItem[] = [
  { key: 'today', label: '今日军情', icon: LayoutDashboard },
  { key: 'cases', label: '营销方案', icon: ClipboardList },
  { key: 'campaigns', label: '活动与台账', icon: Megaphone },
  { key: 'levers', label: '杠杆面板', icon: SlidersHorizontal },
  { key: 'materials', label: '营销素材', icon: ImageIcon },
  { key: 'effect', label: '效果分析', icon: BarChart3 },
  { key: 'settings', label: '设置', icon: Settings },
]

export function MarketingNav({ active, onChange, collapsed, onToggle }: {
  active: SectionKey
  onChange: (s: SectionKey) => void
  collapsed: boolean
  onToggle: () => void
}) {
  return (
    <nav
      aria-label="营销军师导航"
      className={cn(
        'flex h-full flex-col border-r border-border bg-card transition-[width] duration-200',
        collapsed ? 'w-16' : 'w-60',
      )}
    >
      {/* 品牌标题 */}
      <div className={cn('flex items-center gap-2.5 border-b border-border px-4 py-4', collapsed && 'justify-center px-0')}>
        <span className="relative flex size-2.5 shrink-0">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-brand/60" />
          <span className="relative inline-flex size-2.5 rounded-full bg-brand" />
        </span>
        {!collapsed && (
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-foreground">营销军师</div>
            <div className="truncate text-[11px] text-muted-foreground">经营后台营销大脑</div>
          </div>
        )}
      </div>

      {/* 导航项 */}
      <div className="flex-1 space-y-1 overflow-y-auto p-2">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon
          const isActive = active === item.key
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => onChange(item.key)}
              aria-current={isActive ? 'page' : undefined}
              title={collapsed ? item.label : undefined}
              className={cn(
                'group relative flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition-colors',
                collapsed && 'justify-center px-0',
                isActive
                  ? 'bg-muted font-medium text-foreground'
                  : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground',
              )}
            >
              {isActive && (
                <motion.span
                  layoutId="marketing-nav-accent"
                  className="absolute left-0 top-1/2 h-6 w-1 -translate-y-1/2 rounded-r-full bg-brand"
                  transition={{ duration: 0.22 }}
                />
              )}
              <Icon className={cn('size-4 shrink-0', isActive && 'text-foreground')} />
              {!collapsed && <span className="truncate">{item.label}</span>}
            </button>
          )
        })}
      </div>

      {/* 回主菜单 + 收起菜单 */}
      <div className="space-y-1 border-t border-border p-2">
        <BackToMain collapsed={collapsed} />
        <button
          type="button"
          onClick={onToggle}
          title={collapsed ? '展开菜单' : '收起菜单'}
          className={cn(
            'flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground',
            collapsed && 'justify-center px-0',
          )}
        >
          {collapsed ? <PanelLeft className="size-4 shrink-0" /> : <PanelLeftClose className="size-4 shrink-0" />}
          {!collapsed && <span>收起菜单</span>}
        </button>
      </div>
    </nav>
  )
}

// 营销中心是独立全屏 chrome,必须给一条明确的回家路(老板 2026-07-05 点名)
function BackToMain({ collapsed }: { collapsed: boolean }) {
  const navigate = useNavigate()
  return (
    <button
      type="button"
      onClick={() => navigate('/')}
      title="回到主菜单"
      className={cn(
        'flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground',
        collapsed && 'justify-center px-0',
      )}
    >
      <ArrowLeftToLine className="size-4 shrink-0" />
      {!collapsed && <span>回到主菜单</span>}
    </button>
  )
}
