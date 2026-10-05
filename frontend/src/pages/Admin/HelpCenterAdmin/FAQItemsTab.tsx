// [2026-05-18 帮助中心 Phase 3 v3] FAQ 内容 tab · 分组卡片 + 拖拽排序
// 用 @dnd-kit 做拖拽 · 卡片左侧拖把手 · 拖完批量 POST /reorder
// 卡片去掉"排序 X"显示(由拖拽控制 · 数字本身用户不关心)

import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import {
  AlertCircle,
  ChevronDown,
  GripVertical,
  Loader2,
  MessageSquare,
  Plus,
  ThumbsDown,
  ThumbsUp,
} from 'lucide-react'
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { listAdminItems, reorderItems } from './api'
import { CATEGORY_LABEL, type FAQAdminItem, type FAQCategory } from './types'

// ========== 单条可拖拽卡片 ==========

function SortableFAQCard({ item }: { item: FAQAdminItem }) {
  const navigate = useNavigate()
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: item.id,
  })
  const badAnswer = item.thumbs_down > item.thumbs_up && item.thumbs_down > 5

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.6 : 1,
    zIndex: isDragging ? 10 : 'auto' as const,
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`group relative flex items-stretch gap-2 rounded-lg border bg-card transition-colors ${
        badAnswer ? 'border-l-4 border-l-rose-500 bg-rose-500/5' : ''
      } ${!item.is_published ? 'opacity-70' : ''} ${
        isDragging ? 'shadow-lg' : 'hover:border-foreground/30 hover:bg-muted/30'
      }`}
    >
      {/* 拖把手 · 左侧 · hover/focus 出深色 */}
      <button
        type="button"
        {...attributes}
        {...listeners}
        className="flex shrink-0 cursor-grab items-center px-2 text-muted-foreground/50 hover:text-foreground active:cursor-grabbing"
        aria-label={`拖动 ${item.question}`}
      >
        <GripVertical className="size-4" />
      </button>

      {/* 卡片主体 · 整片可点跳编辑 */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => navigate(`/admin/help-center/faq/${item.id}`)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            navigate(`/admin/help-center/faq/${item.id}`)
          }
        }}
        className="min-w-0 flex-1 cursor-pointer py-4 pr-4"
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-medium text-foreground">{item.question}</h3>
            {!item.answer_md && (
              <p className="mt-1 text-xs text-muted-foreground">暂未填答案 · 点击进入编辑</p>
            )}
          </div>
          {badAnswer && (
            <Badge variant="destructive" className="shrink-0">
              差答案
            </Badge>
          )}
          {!item.is_published && (
            <Badge variant="outline" className="shrink-0 text-xs text-muted-foreground">
              未上架
            </Badge>
          )}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <ThumbsUp className="size-3" />
            <span className="tabular-nums">{item.thumbs_up}</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <ThumbsDown className="size-3" />
            <span className="tabular-nums">{item.thumbs_down}</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <MessageSquare className="size-3" />
            <span className="tabular-nums">{item.feedback_count}</span>
          </span>
        </div>
      </div>
    </div>
  )
}

// ========== 分组节(每组一个 DndContext) ==========

function CategorySection({
  category,
  items,
  collapsed,
  onToggle,
  onReorder,
}: {
  category: FAQCategory
  items: FAQAdminItem[]
  collapsed: boolean
  onToggle: () => void
  onReorder: (newOrderIds: number[]) => void
}) {
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 5 }, // 5px 才触发拖拽 · 防误触
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  )

  if (items.length === 0) return null
  const badCount = items.filter((i) => i.thumbs_down > i.thumbs_up && i.thumbs_down > 5).length

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const oldIndex = items.findIndex((i) => i.id === active.id)
    const newIndex = items.findIndex((i) => i.id === over.id)
    if (oldIndex < 0 || newIndex < 0) return
    const reordered = arrayMove(items, oldIndex, newIndex)
    onReorder(reordered.map((i) => i.id))
  }

  return (
    <section className="space-y-2">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-muted/50"
      >
        <ChevronDown
          className={`size-4 shrink-0 text-muted-foreground transition-transform ${
            collapsed ? '-rotate-90' : ''
          }`}
        />
        <h2 className="text-base font-semibold text-foreground">{CATEGORY_LABEL[category]}</h2>
        <span className="text-xs text-muted-foreground">({items.length})</span>
        {badCount > 0 && (
          <Badge variant="destructive" className="ml-1 text-[10px]">
            {badCount} 差答案
          </Badge>
        )}
      </button>
      {!collapsed && (
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <SortableContext items={items.map((i) => i.id)} strategy={verticalListSortingStrategy}>
            <div className="space-y-2 pl-1">
              {items.map((item) => (
                <SortableFAQCard key={item.id} item={item} />
              ))}
            </div>
          </SortableContext>
        </DndContext>
      )}
    </section>
  )
}

// ========== Tab 主组件 ==========

const CATEGORY_ORDER: FAQCategory[] = ['billing', 'operation', 'data', 'account']

export function FAQItemsTab() {
  const navigate = useNavigate()
  const [items, setItems] = useState<FAQAdminItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [collapsed, setCollapsed] = useState<Set<FAQCategory>>(new Set())

  const reload = async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await listAdminItems()
      setItems(data)
    } catch (err) {
      setError(String((err as Error).message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    reload()
  }, [])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items
    return items.filter((item) => item.question.toLowerCase().includes(q))
  }, [items, query])

  const grouped = useMemo(() => {
    const groups: Record<FAQCategory, FAQAdminItem[]> = {
      billing: [],
      operation: [],
      data: [],
      account: [],
    }
    for (const item of filtered) groups[item.category].push(item)
    // 每组按 sort_order 升序(后端已排, 这里再 stable 一次防搜索筛后乱)
    for (const cat of Object.keys(groups) as FAQCategory[]) {
      groups[cat].sort((a, b) => a.sort_order - b.sort_order)
    }
    return groups
  }, [filtered])

  const toggleGroup = (cat: FAQCategory) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(cat)) next.delete(cat)
      else next.add(cat)
      return next
    })
  }

  const totalBadCount = useMemo(
    () => items.filter((i) => i.thumbs_down > i.thumbs_up && i.thumbs_down > 5).length,
    [items],
  )

  /**
   * 拖拽完成 · 乐观更新 + 后端 POST
   * 失败时 toast 错误并刷新拉回原状(简化处理 · 不做精细回滚)
   */
  const handleReorder = async (newOrderIds: number[]) => {
    // 乐观更新: 立刻把对应分类的 items 按 newOrderIds 重排, sort_order 也临时调
    setItems((prev) => {
      const idToItem = new Map(prev.map((i) => [i.id, i]))
      const reorderedSet = new Set(newOrderIds)
      const updates = newOrderIds.map((id, idx) => {
        const it = idToItem.get(id)
        return it ? { ...it, sort_order: (idx + 1) * 10 } : null
      })
      const updatedById = new Map(updates.filter((u): u is FAQAdminItem => !!u).map((u) => [u.id, u]))
      return prev.map((it) => (reorderedSet.has(it.id) ? updatedById.get(it.id) ?? it : it))
    })
    try {
      await reorderItems(newOrderIds)
    } catch (err) {
      toast.error('排序保存失败 · 已恢复', { description: String((err as Error).message) })
      await reload()
    }
  }

  return (
    <div className="space-y-5">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => navigate('/admin/help-center/faq/new')} size="sm">
          <Plus className="size-4" />
          新建 FAQ
        </Button>
        <Input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜问题..."
          className="h-9 max-w-xs"
        />
        <div className="ml-auto flex items-center gap-4 text-xs text-muted-foreground">
          <span>共 {filtered.length} / {items.length} 条</span>
          {totalBadCount > 0 && (
            <span className="inline-flex items-center gap-1 text-rose-600">
              <AlertCircle className="size-3.5" />
              {totalBadCount} 条差答案待改
            </span>
          )}
        </div>
      </div>

      {/* 提示 · 拖把手怎么用 */}
      {!loading && !error && items.length > 0 && (
        <p className="px-2 text-xs text-muted-foreground">
          提示: 卡片左边的 <GripVertical className="inline size-3" /> 可以拖动调整同分类内的顺序
        </p>
      )}

      {/* 列表 */}
      {loading ? (
        <div className="flex min-h-[300px] items-center justify-center text-muted-foreground">
          <Loader2 className="mr-2 size-4 animate-spin" />
          加载中
        </div>
      ) : error ? (
        <div className="flex min-h-[300px] flex-col items-center justify-center rounded-lg border border-dashed bg-muted/20 p-8 text-center">
          <AlertCircle className="mb-2 size-6 text-destructive" />
          <p className="text-sm">{error}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => window.location.reload()}>
            刷新页面重试
          </Button>
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex min-h-[300px] flex-col items-center justify-center rounded-lg border border-dashed bg-muted/20 p-8 text-center">
          <p className="text-sm text-foreground">
            {query ? '没匹配的 FAQ' : '还没有 FAQ'}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {query ? '换个关键词试试' : '点上面"新建 FAQ"开始'}
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {CATEGORY_ORDER.map((cat) => (
            <CategorySection
              key={cat}
              category={cat}
              items={grouped[cat]}
              collapsed={collapsed.has(cat)}
              onToggle={() => toggleGroup(cat)}
              onReorder={handleReorder}
            />
          ))}
        </div>
      )}
    </div>
  )
}
