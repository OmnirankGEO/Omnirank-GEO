/**
 * SearchableSelect · 可搜索下拉统一组件（两层）
 *
 * 工单：docs/AI-CONTEXT/WORKORDER_SEARCHABLE_SELECT_2026-07-30.md
 * 清单：docs/AI-CONTEXT/SEARCHABLE_SELECT_INVENTORY_2026-07-30.md
 *
 * 为什么不是"把 ClientSwitcherSidebar 抄进 shadcn Select"：
 *   Radix Select 的 SelectContent 会劫持键盘做 typeahead，塞不进真输入框。
 *   底座改用 @base-ui/react/combobox（项目既有依赖 · 锁定 1.3.0 · 已被 6 个 ui 基元使用），
 *   键盘（↑↓/Enter/Esc）与 aria（combobox/listbox/option + aria-activedescendant）由基元提供。
 *
 * 两层：
 *   SearchableRoot      —— 数据 + 搜索词 + 过滤 + 🔴关闭清空（全站唯一一处）
 *   SearchableListPanel —— 搜索框 + 即时过滤列表 + 空态（ClientSwitcherSidebar 与 SearchableSelect 共用）
 *   SearchableSelect    —— SearchableRoot + trigger + 弹层 + 单值语义（新接入点用）
 *
 * 🔴 关闭清空只在 SearchableRoot 里实现一次（open: true→false 的唯一 effect）。
 *    消费方不持有搜索词、也无权清空 —— 新增第 5 条关闭路径也不可能漏。
 */

import * as React from 'react'
import { Combobox } from '@base-ui/react/combobox'
import { Check, ChevronDown, Search, X } from 'lucide-react'
import { cn } from '@/lib/utils'

/** ≤8 不渲染搜索框（工单 §1：一眼能看完，加了是体验倒退）。9 起渲染。 */
export const SEARCHABLE_MIN_OPTIONS = 9

/** 阈值自动判定；显式传 searchable 时以显式为准。 */
export function resolveSearchable(optionCount: number, explicit?: boolean): boolean {
    return explicit === undefined ? optionCount >= SEARCHABLE_MIN_OPTIONS : explicit
}

/** 默认过滤：大小写不敏感 · 命中 label / value / keywords 任一即可。 */
export function defaultMatch(haystacks: Array<string | null | undefined>, query: string): boolean {
    const q = query.trim().toLowerCase()
    if (!q) return true
    return haystacks.some((h) => !!h && h.toLowerCase().includes(q))
}

// ============================================================
// 共用层 1/2 · SearchableRoot
// ============================================================

interface SearchableListContextValue<T> {
    query: string
    setQuery: (q: string) => void
    /** 当前过滤后的条目（消费方拿它渲染列表 / 显示"匹配 N"） */
    filtered: readonly T[]
    /** 过滤前的全量条目 */
    items: readonly T[]
    searchable: boolean
    inputRef: React.RefObject<HTMLInputElement>
    /** 请求关闭（Esc 等）· 共用层统一走这里，消费方不各自写 Esc 处理 */
    requestClose: () => void
}

const SearchableListContext = React.createContext<SearchableListContextValue<any> | null>(null)

export function useSearchableList<T = any>(): SearchableListContextValue<T> {
    const ctx = React.useContext(SearchableListContext)
    if (!ctx) throw new Error('useSearchableList 必须在 <SearchableRoot> 内使用')
    return ctx as SearchableListContextValue<T>
}

export interface SearchableRootProps<T> {
    items: readonly T[]
    open: boolean
    onOpenChange: (open: boolean) => void
    value?: T | null
    onValueChange?: (value: T | null) => void
    /** 自定义过滤；不传则用 itemToLabel 做大小写不敏感包含匹配 */
    filter?: (item: T, query: string) => boolean
    itemToLabel?: (item: T) => string
    isItemEqualToValue?: (a: T, b: T) => boolean
    /** 未指定时按 items.length 与阈值自动判定 */
    searchable?: boolean
    disabled?: boolean
    /**
     * 列表内联渲染、不走 Popup（ClientSwitcherSidebar 是这种：面板是自家的绝对定位 div）。
     * 🔴 必须传 true，否则 base-ui 的 popup dismiss 会把「面板内的状态筛选 pill / 测试开关」
     *    当成 outsidePress 直接关掉面板 —— 实测点 pill 面板消失，锁5 判红。
     */
    inline?: boolean
    children: React.ReactNode
}

export function SearchableRoot<T>({
    items,
    open,
    onOpenChange,
    value = null,
    onValueChange,
    filter,
    itemToLabel,
    isItemEqualToValue,
    searchable,
    disabled,
    inline,
    children,
}: SearchableRootProps<T>) {
    const [query, setQuery] = React.useState('')
    const inputRef = React.useRef<HTMLInputElement>(null)

    // 🔴🔴 全站唯一的"关闭清空"实现点。
    // 任何关闭路径（trigger 再点 / 选中项 / Esc / 外部点击 / 未来新增的第 N 条）
    // 都只需要把 open 置 false，清空由这一处结构性保证。
    // 不要把 setQuery('') 复制回调用方 —— 那就退回改前"6 处各自清空"的状态。
    //
    // 🔴 清空链共三层保障，从强到弱：
    //   ① 面板卸载 —— 消费方若用 `{open && <SearchableRoot>…}` 条件渲染（ClientSwitcherSidebar
    //      就是），关闭即整个 Root 卸载，query state 随之消失。实测把 ②③ 两层全掐掉，
    //      ClientSwitcher 的"关闭再打开搜索框为空"依然成立 —— 靠的就是这一层。
    //   ② 本 effect（open true→false）
    //   ③ 受控 inputValue 回写（base-ui 关闭时会回调 onInputValueChange('')）
    //
    // ⚠️ ① 依赖"条件渲染 = 卸载"。若哪天为了做动画把面板改成常驻渲染 + CSS 隐藏
    //    （keepMounted / display:none / opacity-0 等任何不卸载的写法），第一层立即失效，
    //    清空就只剩 ②③ 兜底 —— 那时必须重新验证整条清空链（四条关闭路径各跑一遍
    //    「输入 → 关闭 → 重开搜索框为空且列表完整」），不要假定还成立。
    const prevOpenRef = React.useRef(open)
    React.useEffect(() => {
        if (prevOpenRef.current && !open) setQuery('')
        prevOpenRef.current = open
    }, [open])

    const resolvedSearchable = resolveSearchable(items.length, searchable)

    const filtered = React.useMemo(() => {
        if (!query.trim()) return items
        if (filter) return items.filter((item) => filter(item, query))
        return items.filter((item) =>
            defaultMatch([itemToLabel ? itemToLabel(item) : String(item)], query),
        )
    }, [items, query, filter, itemToLabel])

    const requestClose = React.useCallback(() => onOpenChange(false), [onOpenChange])

    const ctx = React.useMemo<SearchableListContextValue<T>>(
        () => ({ query, setQuery, filtered, items, searchable: resolvedSearchable, inputRef, requestClose }),
        [query, filtered, items, resolvedSearchable, requestClose],
    )

    return (
        <SearchableListContext.Provider value={ctx}>
            <Combobox.Root
                items={items as any}
                filteredItems={filtered as any}
                value={value as any}
                onValueChange={(next: any) => onValueChange?.(next ?? null)}
                open={open}
                onOpenChange={(nextOpen) => onOpenChange(nextOpen)}
                inputValue={query}
                onInputValueChange={(next) => setQuery(next)}
                itemToStringLabel={itemToLabel as any}
                isItemEqualToValue={isItemEqualToValue as any}
                disabled={disabled}
                inline={inline}
            >
                {children}
            </Combobox.Root>
        </SearchableListContext.Provider>
    )
}

// ============================================================
// 共用层 2/2 · SearchableListPanel
// ============================================================

export interface SearchableListPanelProps<T> {
    /** 每行渲染（保留徽章 / 多行等自定义能力） */
    renderItem: (item: T, state: { selected: boolean; highlighted: boolean }) => React.ReactNode
    itemKey: (item: T) => React.Key
    itemValue?: (item: T) => any
    isSelected?: (item: T) => boolean
    searchPlaceholder?: string
    /** 有搜索词但无匹配时的文案 */
    emptyText?: string
    /** 无搜索词且列表本身为空时的文案 */
    emptyNoDataText?: string
    /** 打开即聚焦搜索框（工单 §0 行为 1）。仅在 searchable 时有效。 */
    autoFocus?: boolean
    className?: string
    listClassName?: string
    itemClassName?: string
}

export function SearchableListPanel<T>({
    renderItem,
    itemKey,
    itemValue,
    isSelected,
    searchPlaceholder = '搜索…',
    emptyText = '没有匹配项',
    emptyNoDataText = '暂无可选项',
    autoFocus = true,
    className,
    listClassName,
    itemClassName,
}: SearchableListPanelProps<T>) {
    // 组合件：搜索框 + 列表。两段各自的实现在下方,ClientSwitcherSidebar 直接用那两段。
    return (
        <div className={cn('flex min-h-0 flex-col', className)}>
            <SearchableSearchBox searchPlaceholder={searchPlaceholder} autoFocus={autoFocus} />
            <SearchableItems
                renderItem={renderItem}
                itemKey={itemKey}
                itemValue={itemValue}
                isSelected={isSelected}
                emptyText={emptyText}
                emptyNoDataText={emptyNoDataText}
                listClassName={listClassName}
                itemClassName={itemClassName}
            />
        </div>
    )
}

/**
 * 搜索框（含自动聚焦 + 清空按钮）· 可与列表分开摆放
 * ClientSwitcherSidebar 需要「搜索框 → 状态筛选 pills → 列表」的顺序，
 * 所以两段必须能拆开各自摆，而不是被面板绑死在一起。
 */
export function SearchableSearchBox({
    searchPlaceholder = '搜索…',
    autoFocus = true,
    className,
    wrapperClassName,
}: {
    searchPlaceholder?: string
    autoFocus?: boolean
    className?: string
    wrapperClassName?: string
}) {
    const { query, setQuery, searchable, inputRef, requestClose } = useSearchableList()

    // 打开即聚焦：面板随 open 挂载，所以挂载即"刚打开"。
    //
    // 🔴 为什么不能只靠一个同步 effect：弹层 Portal 到 Dialog 之外时，
    //    Dialog 的 focus trap 会在我们聚焦之后把焦点拽回 trigger（实测:
    //    UserManagement「调整直属上游」弹窗里,搜索框存在但 activeElement 是 trigger）。
    //    根治靠 Portal container 指向该 Dialog(见 SearchableSelect),这里再补一帧 rAF 兜底。
    React.useEffect(() => {
        if (!autoFocus || !searchable) return
        inputRef.current?.focus()
        const raf = requestAnimationFrame(() => {
            if (document.activeElement !== inputRef.current) inputRef.current?.focus()
        })
        return () => cancelAnimationFrame(raf)
    }, [autoFocus, searchable, inputRef])

    if (!searchable) return null

    return (
        <div className={cn('border-b border-border/60 p-2', wrapperClassName)}>
            <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <Combobox.Input
                    ref={inputRef}
                    placeholder={searchPlaceholder}
                    // 🔴 Esc 关闭统一放在共用层。
                    //    inline 模式下 base-ui 不接管 open,若不在这里处理,
                    //    ClientSwitcher 的 Esc 关闭就静默失效(实测已复现:改前能关,改后关不掉)。
                    onKeyDown={(e) => {
                        if (e.key === 'Escape') requestClose()
                    }}
                    className={cn(
                        'w-full rounded-md border border-border bg-secondary py-2 pl-8 pr-7 text-[13px] outline-none transition-colors focus:border-brand focus:ring-1 focus:ring-brand/30',
                        className,
                    )}
                />
                {query && (
                    <button
                        type="button"
                        onClick={() => {
                            setQuery('')
                            inputRef.current?.focus()
                        }}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                        aria-label="清空"
                    >
                        <X className="h-3 w-3" />
                    </button>
                )}
            </div>
        </div>
    )
}

/** 过滤后的列表（含空态）· 可与搜索框分开摆放 */
export function SearchableItems<T>({
    renderItem,
    itemKey,
    itemValue,
    isSelected,
    emptyText = '没有匹配项',
    emptyNoDataText = '暂无可选项',
    listClassName,
    itemClassName,
}: Pick<
    SearchableListPanelProps<T>,
    'renderItem' | 'itemKey' | 'itemValue' | 'isSelected' | 'emptyText' | 'emptyNoDataText' | 'listClassName' | 'itemClassName'
>) {
    const { query, filtered, items } = useSearchableList<T>()

    return (
        <Combobox.List className={cn('min-h-0 flex-1 overflow-y-auto', listClassName)}>
            {filtered.length === 0 ? (
                <div className="px-3 py-8 text-center text-[13px] text-muted-foreground">
                    {items.length === 0 ? emptyNoDataText : query.trim() ? emptyText : emptyNoDataText}
                </div>
            ) : (
                filtered.map((item, index) => (
                    <Combobox.Item
                        key={itemKey(item)}
                        index={index}
                        value={itemValue ? itemValue(item) : item}
                        className={cn(
                            'cursor-pointer outline-none data-[highlighted]:bg-secondary',
                            itemClassName,
                        )}
                    >
                        {renderItem(item, {
                            selected: isSelected ? isSelected(item) : false,
                            highlighted: false,
                        })}
                    </Combobox.Item>
                ))
            )}
        </Combobox.List>
    )
}

// ============================================================
// SearchableSelect · 单值下拉（新接入点用）
// ============================================================

export interface SearchableSelectOption {
    value: string
    label: string
    disabled?: boolean
    /** 额外可搜索文本（如行业 / 代码 / 备注），大小写不敏感 */
    keywords?: string
    /** 自定义该行渲染（默认渲染 label） */
    render?: React.ReactNode
}

export interface SearchableSelectProps {
    options: SearchableSelectOption[]
    value: string | null | undefined
    onChange: (value: string) => void
    placeholder?: string
    searchPlaceholder?: string
    emptyText?: string
    /** 未指定 → 按选项数自动判定（≤8 不渲染搜索框） */
    searchable?: boolean
    disabled?: boolean
    id?: string
    /** trigger 的 className，用法与 SelectTrigger 一致 */
    className?: string
    contentClassName?: string
    'aria-label'?: string
}

function SearchableSelectPopup({
    children,
    className,
}: {
    children: React.ReactNode
    className?: string
}) {
    // initialFocus 指向搜索框：Popup 自己的聚焦时机在定位之后，
    // 比"挂载即 focus"更能顶住 Dialog focus trap。
    const { inputRef, searchable } = useSearchableList()
    return (
        <Combobox.Popup initialFocus={searchable ? inputRef : true} className={className}>
            {children}
        </Combobox.Popup>
    )
}

export function SearchableSelect({
    options,
    value,
    onChange,
    placeholder,
    searchPlaceholder = '输入关键字过滤…',
    emptyText = '没有匹配项',
    searchable,
    disabled,
    id,
    className,
    contentClassName,
    'aria-label': ariaLabel,
}: SearchableSelectProps) {
    const [open, setOpen] = React.useState(false)
    const triggerRef = React.useRef<HTMLButtonElement>(null)
    // 弹层投放容器:在 Dialog 内就投进该 Dialog(见下方 Portal 处注释),否则 null=body
    const [portalContainer, setPortalContainer] = React.useState<HTMLElement | null>(null)

    const selected = React.useMemo(
        () => options.find((o) => o.value === value) ?? null,
        [options, value],
    )

    return (
        <SearchableRoot<SearchableSelectOption>
            items={options}
            open={open}
            onOpenChange={(next) => {
                if (next) {
                    setPortalContainer(
                        (triggerRef.current?.closest('[role="dialog"]') as HTMLElement | null) ?? null,
                    )
                }
                setOpen(next)
            }}
            value={selected}
            onValueChange={(next) => {
                if (next) onChange(next.value)
            }}
            filter={(item, query) => defaultMatch([item.label, item.value, item.keywords], query)}
            itemToLabel={(item) => item.label}
            isItemEqualToValue={(a, b) => a?.value === b?.value}
            searchable={searchable}
            disabled={disabled}
        >
            <Combobox.Trigger
                ref={triggerRef}
                id={id}
                aria-label={ariaLabel}
                className={cn(
                    'flex h-10 w-full items-center justify-between rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-hidden focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 [&>span]:line-clamp-1',
                    className,
                )}
            >
                <Combobox.Value>
                    {(v: SearchableSelectOption | null) =>
                        v ? (
                            <span className="truncate">{v.label}</span>
                        ) : (
                            <span className="truncate text-muted-foreground">{placeholder ?? '请选择'}</span>
                        )
                    }
                </Combobox.Value>
                <Combobox.Icon>
                    <ChevronDown className="h-4 w-4 shrink-0 opacity-50" />
                </Combobox.Icon>
            </Combobox.Trigger>

            {/*
              🔴 container 必须指向所在 Dialog,不能用默认的 document.body。
              默认 body portal 会让弹层落在 Dialog 的 DOM 之外,Radix Dialog 的 focus scope
              检测到焦点离开 dialog 子树就立刻拽回 trigger —— 搜索框永远拿不到焦点
              (实测:UserManagement「调整直属上游」弹窗里锁2 真渲染判红,而"input 存在"断言会假绿)。
              Positioner 强制要求 Portal(去掉会抛 "Base UI: <Combobox.Portal> is missing"),
              所以走 container 改投放点;不在 Dialog 里时 portalContainer=null → 仍是 body。
            */}
            {/* container 必须给 undefined 才回落到 body:实测传 null 会让 FloatingPortal
                当成"无容器"→ 弹层整个不渲染(非 Dialog 场景直接打不开) */}
            <Combobox.Portal container={portalContainer ?? undefined}>
                <Combobox.Positioner sideOffset={4} positionMethod="fixed" className="z-50">
                    <SearchableSelectPopup
                        className={cn(
                            'flex max-h-[min(24rem,var(--available-height))] w-[var(--anchor-width)] min-w-32 flex-col overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md',
                            contentClassName,
                        )}
                    >
                        <SearchableListPanel<SearchableSelectOption>
                            itemKey={(o) => o.value}
                            itemValue={(o) => o}
                            isSelected={(o) => o.value === value}
                            searchPlaceholder={searchPlaceholder}
                            emptyText={emptyText}
                            renderItem={(o, { selected: isSel }) => (
                                <div className="flex w-full items-center gap-2 px-3 py-2 text-sm">
                                    <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">
                                        {isSel && <Check className="h-4 w-4" />}
                                    </span>
                                    <span className="min-w-0 flex-1">{o.render ?? o.label}</span>
                                </div>
                            )}
                        />
                    </SearchableSelectPopup>
                </Combobox.Positioner>
            </Combobox.Portal>
        </SearchableRoot>
    )
}

export default SearchableSelect
