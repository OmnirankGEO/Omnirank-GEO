// 全域上榜GEO交付系统操作说明书。
// /help 直接进入角色手册；/help/docs/:slug 保留为兼容深链。

import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link, useParams } from 'react-router-dom'
import ReactMarkdown from '@/components/SafeMarkdown'
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  BookOpenCheck,
  ChevronRight,
  Clock,
  Coins,
  Construction,
  Lightbulb,
  Menu,
  Lock,
  PlayCircle,
  Search,
  X as XIcon,
} from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { VideoCard } from '@/components/help/VideoCard'
import { getVideo } from '@/components/help/videos-data'
import { useAuth } from '@/context/AuthContext'
import { authFetch } from '@/lib/api'
import {
  canViewDoc,
  defaultHelpSlug,
  findDoc,
  getNeighbors,
  getVisibleCategories,
  type DocCategory,
  type DocNode,
  type HelpViewer,
} from './docs-data'

function ScreenshotPlaceholder({ caption }: { caption?: string }) {
  return (
    <span className="my-4 flex aspect-video w-full items-center justify-center rounded-lg border border-dashed bg-muted/40 text-sm text-muted-foreground">
      截图位 · {caption || '待运营提供'}
    </span>
  )
}

/**
 * 文档里的真实截图 · 点击放大灯箱
 * - 缩略图:鼠标 hover 缩小指针变 zoom · 视觉提示可点
 * - 点击 → createPortal 全屏遮罩 · 居中放大显示原图
 * - 关闭路径:点遮罩 / 点关闭按钮 / ESC 键 (三个互冗余 · 任一即可)
 */
function ZoomableImage({ src, alt }: { src?: string; alt?: string }) {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    // 锁住页面滚动 · 防灯箱开时背景跟着滚
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
    }
  }, [open])

  if (!src) return null

  return (
    <>
      <img
        src={src}
        alt={alt}
        loading="lazy"
        className="my-4 rounded-lg border border-border/40 cursor-zoom-in transition hover:opacity-90"
        onClick={() => setOpen(true)}
      />
      {open && createPortal(
        <div
          className="fixed inset-0 z-[200] flex items-center justify-center bg-black/85 backdrop-blur-sm p-6 cursor-zoom-out animate-in fade-in duration-150"
          onClick={() => setOpen(false)}
          role="dialog"
          aria-modal="true"
          aria-label={alt || '放大图片'}
        >
          {/* 关闭按钮 */}
          <button
            type="button"
            className="absolute right-4 top-4 grid h-10 w-10 place-items-center rounded-full bg-white/10 text-white/80 hover:bg-white/20 hover:text-white transition"
            onClick={(e) => { e.stopPropagation(); setOpen(false) }}
            aria-label="关闭"
          >
            <XIcon className="h-5 w-5" />
          </button>
          {/* 大图 · 点图本身不关 · 防误关 */}
          <img
            src={src}
            alt={alt}
            className="max-h-full max-w-full rounded-lg shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          />
          {/* 底部 caption */}
          {alt && (
            <div className="absolute bottom-4 left-1/2 -translate-x-1/2 max-w-[90vw] truncate rounded-md bg-black/60 px-3 py-1.5 text-xs text-white/85">
              {alt}
            </div>
          )}
        </div>,
        document.body,
      )}
    </>
  )
}

// 帮助文档专用引用块标记 · 把 blockquote 转成卡片 / 折叠块
//   > [!TIP] 文字   → 提示卡    > [!WARN] 文字 → 警告卡
//   > [!COST] 文字  → 费用卡    > [!MORE] 标题 + 后续行 → 折叠块
// 不依赖 rehype-raw / 第三方插件 · 纯手写 mdast 遍历 · 改 hName/hProperties 走 remark-rehype
// 只匹配标记前缀 · 不锁定行尾(正文可能有软换行 / 多段)
const CALLOUT_RE = /^\[!(TIP|WARN|COST|MORE)\][ \t]*/

function remarkCallouts() {
  return (tree: any) => {
    const walk = (node: any) => {
      if (!node || !Array.isArray(node.children)) return
      for (const child of node.children) {
        if (child.type === 'blockquote') transformCallout(child)
        walk(child)
      }
    }
    walk(tree)
  }
}

function transformCallout(node: any) {
  const firstPara = node.children?.[0]
  if (!firstPara || firstPara.type !== 'paragraph') return
  const firstText = firstPara.children?.[0]
  if (!firstText || firstText.type !== 'text') return
  const m = String(firstText.value).match(CALLOUT_RE)
  if (!m) return
  const type = m[1].toLowerCase()
  const stripped = String(firstText.value).slice(m[0].length)
  node.data = node.data || {}
  if (type === 'more') {
    // [!MORE] 标题独占首段(后面空 > 行换段)· 移除首段当折叠标题 · 余下作折叠内容
    const title = stripped.split('\n')[0].trim() || '更多'
    node.children = node.children.slice(1)
    node.data.hName = 'div'
    node.data.hProperties = { 'data-callout': 'more', 'data-title': title }
  } else {
    // 去掉标记 · 保留同段剩余文字 + 后续子节点作为卡片正文
    firstText.value = stripped
    node.data.hName = 'div'
    node.data.hProperties = { 'data-callout': type }
  }
}

// 2026-05-23 · C 方案 Linear/Notion 风:去底色 + 改左侧 2px 竖条 + 图标降饱和
//   原版 3 色底块视觉太抢戏 · 文档要的是"提示属性",不是"重要程度三档"
//   保留图标语义色但调淡 · 整体几乎单色化
const CALLOUT_STYLES: Record<string, { cls: string; color: string; Icon: typeof Lightbulb }> = {
  tip:  { cls: 'border-l-2 border-blue-500/50',     color: 'text-blue-500/80',     Icon: Lightbulb },
  warn: { cls: 'border-l-2 border-amber-500/60',    color: 'text-amber-600/90',    Icon: AlertTriangle },
  cost: { cls: 'border-l-2 border-emerald-500/55',  color: 'text-emerald-600/90',  Icon: Coins },
}

const markdownComponents = {
  img: ({ src, alt }: { src?: string; alt?: string }) => {
    if (src === 'screenshot-placeholder') {
      return <ScreenshotPlaceholder caption={alt} />
    }
    // 真实截图 · 走可点击放大灯箱
    return <ZoomableImage src={src} alt={alt} />
  },
  // 内联小圆点:行内 code `dot:green/blue/amber/rose/red` → 渲染成跟前端同款的彩色圆点
  code: ({ node: _node, className, children, ...props }: any) => {
    const raw = Array.isArray(children) ? children.join('') : String(children ?? '')
    const m = /^dot:(green|blue|amber|rose|red)$/.exec(raw.trim())
    if (m && !className) {
      const tone: Record<string, string> = {
        green: 'bg-emerald-500',
        blue: 'bg-sky-500',
        amber: 'bg-amber-500',
        rose: 'bg-rose-500',
        red: 'bg-destructive',
      }
      return <span className={`mr-0.5 inline-block size-2.5 rounded-full align-middle ${tone[m[1]]}`} />
    }
    return <code className={className} {...props}>{children}</code>
  },
  // callout 卡片 / 折叠块只会以 <div data-callout> 形式出现(普通 markdown 不产 div)
  div: ({ node: _node, children, ...props }: any) => {
    const callout: string | undefined = props['data-callout']
    if (!callout) return <div {...props}>{children}</div>
    if (callout === 'more') {
      const title = props['data-title'] || '更多'
      // C 方案:去底色 + 弱化边框 · 跟 callout 视觉权重一致
      return (
        <details className="my-6 border-l-2 border-border/60 pl-4 pr-2 open:pb-2">
          <summary className="cursor-pointer select-none py-1 text-sm font-medium text-foreground/80 hover:text-foreground marker:text-muted-foreground/60">
            {title}
          </summary>
          <div className="mt-2 text-foreground/85 [&>:first-child]:mt-0">{children}</div>
        </details>
      )
    }
    const s = CALLOUT_STYLES[callout] ?? CALLOUT_STYLES.tip
    const { Icon } = s
    return (
      <div className={`my-6 flex gap-3 pl-4 pr-2 py-1 text-sm ${s.cls}`}>
        <Icon className={`mt-0.5 size-4 shrink-0 ${s.color}`} />
        <div className="min-w-0 text-foreground/85 [&>p]:my-0 [&>p+p]:mt-2 [&>ul]:my-1.5">{children}</div>
      </div>
    )
  },
}

function DocTree({
  filtered,
  currentSlug,
  onNavigate,
}: {
  filtered: DocCategory[]
  currentSlug?: string
  onNavigate?: () => void
}) {
  if (filtered.length === 0) {
    return (
      <p className="px-3 py-6 text-center text-xs text-muted-foreground">
        没找到匹配的文档
      </p>
    )
  }
  return (
    <nav className="space-y-5">
      {filtered.map((category) => (
        <div key={category.id}>
          <div className="mb-2 flex items-center gap-2 px-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            <category.icon className="size-3.5" />
            <span>{category.title}</span>
            <span className="ml-auto text-muted-foreground/70">
              {category.nodes.length}
            </span>
          </div>
          <ul className="space-y-0.5">
            {category.nodes.map((node) => {
              const active = node.slug === currentSlug
              return (
                <li key={node.slug}>
                  <Link
                    to={`/help/docs/${node.slug}`}
                    onClick={onNavigate}
                    className={`block min-h-9 rounded-md px-3 py-2 text-sm leading-5 transition-colors ${
                      active
                        ? 'bg-primary/12 font-medium text-primary ring-1 ring-inset ring-primary/15'
                        : 'text-foreground/78 hover:bg-muted/70 hover:text-foreground'
                    }`}
                  >
                    {node.title}
                  </Link>
                </li>
              )
            })}
          </ul>
        </div>
      ))}
    </nav>
  )
}

function DocContent({
  node,
  category,
  viewer,
}: {
  node: DocNode
  category: DocCategory
  viewer: HelpViewer
}) {
  const video = node.videoId ? getVideo(node.videoId) : undefined
  const { prev, next } = getNeighbors(node.slug, viewer)

  // 正文按真实身份从后端取 · 前端 bundle 不含正文;普通用户取代理 slug 后端直接 403
  const [docBody, setDocBody] = useState<string | null>(null)
  const [bodyState, setBodyState] = useState<'loading' | 'ok' | 'denied' | 'error'>('loading')
  useEffect(() => {
    let alive = true
    setBodyState('loading')
    setDocBody(null)
    authFetch(`/api/help/docs/${node.slug}`)
      .then(async (r) => {
        if (!alive) return
        if (r.status === 403) { setBodyState('denied'); return }
        if (!r.ok) { setBodyState('error'); return }
        const data = await r.json()
        setDocBody(data.body || '')
        setBodyState('ok')
      })
      .catch(() => { if (alive) setBodyState('error') })
    return () => { alive = false }
  }, [node.slug])

  return (
    <article className="mx-auto min-w-0 max-w-[860px]">
      {/* 面包屑 */}
      <nav className="mb-4 flex items-center gap-1.5 text-xs text-muted-foreground">
        <Link to="/help" className="hover:text-foreground">
          操作说明书
        </Link>
        <ChevronRight className="size-3" />
        <span>{category.title}</span>
        <ChevronRight className="size-3" />
        <span className="text-foreground">{node.title}</span>
      </nav>

      <h1 className="text-[30px] font-semibold tracking-normal text-foreground leading-tight md:text-[34px]">
        {node.title}
      </h1>

      {/* 节点视频 · 如果有 */}
      {video && (
        <div className="mt-6 max-w-md">
          <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
            <Clock className="size-3.5" />
            看视频更快 · {video.duration}
          </p>
          <VideoCard video={video} />
        </div>
      )}

      {/* 正文 · C 方案 Linear/Notion 风:自定义排版替代 prose-sm · 间距打开 / 单色 / 阅读舒适 */}
      {bodyState === 'ok' && docBody ? (
        <div className="
          mt-10 max-w-none text-[16px] leading-[1.85] text-foreground/90
          [&_h2]:mt-14 [&_h2]:mb-5 [&_h2]:border-b [&_h2]:border-border/55 [&_h2]:pb-3 [&_h2]:text-[23px] [&_h2]:font-semibold [&_h2]:tracking-normal [&_h2]:text-foreground [&_h2]:leading-snug
          [&_h3]:mt-10 [&_h3]:mb-3 [&_h3]:text-[18px] [&_h3]:font-semibold [&_h3]:text-foreground [&_h3]:leading-snug
          [&_h4]:mt-8 [&_h4]:mb-2 [&_h4]:text-[16px] [&_h4]:font-semibold [&_h4]:text-foreground
          [&_p]:my-5
          [&_ul]:my-4 [&_ul]:space-y-1.5 [&_ul]:pl-6 [&_ul]:list-disc [&_ul]:marker:text-muted-foreground/50
          [&_ol]:my-4 [&_ol]:space-y-1.5 [&_ol]:pl-6 [&_ol]:list-decimal [&_ol]:marker:text-muted-foreground/60
          [&_li]:leading-[1.72] [&_li]:pl-1
          [&_li>p]:my-1.5
          [&_li>ul]:mt-2 [&_li>ul]:mb-0
          [&_table]:my-8 [&_table]:block [&_table]:w-full [&_table]:max-w-full [&_table]:overflow-x-auto [&_table]:border-collapse [&_table]:text-[14px]
          [&_thead]:border-b [&_thead]:border-border
          [&_th]:px-3 [&_th]:py-2.5 [&_th]:text-left [&_th]:font-medium [&_th]:text-foreground
          [&_td]:min-w-32 [&_td]:border-b [&_td]:border-border/40 [&_td]:px-3 [&_td]:py-2.5 [&_td]:align-top
          [&_tr:last-child_td]:border-b-0
          [&_strong]:font-semibold [&_strong]:text-foreground
          [&_a]:text-foreground [&_a]:underline [&_a]:underline-offset-[3px] [&_a]:decoration-muted-foreground/40 [&_a:hover]:decoration-foreground
          [&_hr]:my-12 [&_hr]:border-border/60
          [&_blockquote]:my-6 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-4 [&_blockquote]:text-foreground/75
          [&_code:not(pre_code)]:rounded [&_code:not(pre_code)]:bg-muted/60 [&_code:not(pre_code)]:px-1.5 [&_code:not(pre_code)]:py-0.5 [&_code:not(pre_code)]:text-[13px] [&_code:not(pre_code)]:text-foreground
        ">
          <ReactMarkdown remarkPlugins={[remarkCallouts]} components={markdownComponents}>
            {docBody}
          </ReactMarkdown>
        </div>
      ) : bodyState === 'loading' ? (
        <div className="mt-10 flex justify-center py-16 text-sm text-muted-foreground">正在加载…</div>
      ) : bodyState === 'denied' ? (
        <div className="mt-8 rounded-xl border border-dashed bg-muted/20 p-10 text-center">
          <Lock className="mx-auto mb-3 size-8 text-muted-foreground" />
          <p className="text-sm font-medium text-foreground">这篇文档当前账号没有权限</p>
          <p className="mt-1 text-xs text-muted-foreground">这篇帮助文档不在你当前账号可见的范围内 · 以页面实际显示为准。</p>
        </div>
      ) : (
        <div className="mt-8 rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <Construction className="mx-auto mb-3 size-8 text-muted-foreground" />
          <p className="text-sm font-medium text-foreground">本篇文档暂时打不开</p>
          <p className="mt-1 text-xs text-muted-foreground">
            请刷新重试；仍打不开时，可在“问题反馈”中提交当前页面和时间。
          </p>
          <Button asChild variant="outline" size="sm" className="mt-4">
            <Link to="/feedback">提交问题反馈</Link>
          </Button>
        </div>
      )}

      {/* 上下篇 */}
      <div className="mt-12 grid gap-3 border-t pt-6 sm:grid-cols-2">
        {prev ? (
          <Link
            to={`/help/docs/${prev.node.slug}`}
            className="group rounded-lg border bg-card p-3 transition-colors hover:border-foreground/30"
          >
            <p className="flex items-center gap-1 text-xs text-muted-foreground">
              <ArrowLeft className="size-3" />
              上一篇
            </p>
            <p className="mt-1 text-sm font-medium text-foreground">{prev.node.title}</p>
          </Link>
        ) : (
          <div />
        )}
        {next ? (
          <Link
            to={`/help/docs/${next.node.slug}`}
            className="group rounded-lg border bg-card p-3 text-right transition-colors hover:border-foreground/30 sm:text-right"
          >
            <p className="flex items-center justify-end gap-1 text-xs text-muted-foreground">
              下一篇
              <ArrowRight className="size-3" />
            </p>
            <p className="mt-1 text-sm font-medium text-foreground">{next.node.title}</p>
          </Link>
        ) : (
          <div />
        )}
      </div>
    </article>
  )
}

function AccessDenied() {
  return (
    <div className="flex min-h-[400px] flex-col items-center justify-center rounded-xl border border-dashed bg-muted/20 p-10 text-center">
      <Lock className="mb-3 size-10 text-muted-foreground" />
      <h2 className="text-base font-semibold text-foreground">这篇文档当前账号没有权限</h2>
      <p className="mt-1 max-w-sm text-sm text-muted-foreground">
        这篇帮助文档不在你当前账号可见的范围内 · 以页面实际显示为准。
      </p>
      <Button asChild variant="outline" size="sm" className="mt-4">
        <Link to="/help">
          <ArrowLeft className="size-3.5" />
          回文档主页
        </Link>
      </Button>
    </div>
  )
}

export default function HelpDocs() {
  const { slug } = useParams<{ slug?: string }>()
  const [query, setQuery] = useState('')
  const [mobileTreeOpen, setMobileTreeOpen] = useState(false)
  const { user } = useAuth()
  const isAdmin = !!user?.is_admin
  // 身份只来自 /api/auth/me 返回的权威 agent_level。
  const authLevel = Number((user as unknown as Record<string, unknown>)?.agent_level ?? 0)
  const isAgent = authLevel >= 1
  const isL2 = authLevel >= 2

  // 后端权威白名单:进帮助中心时拉一次 · 返回当前身份看不到的 slug
  const [hiddenSlugs, setHiddenSlugs] = useState<Set<string> | null>(null)
  useEffect(() => {
    let alive = true
    authFetch('/api/help/docs/acl')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (alive && Array.isArray(data?.hidden_slugs)) {
          setHiddenSlugs(new Set<string>(data.hidden_slugs as string[]))
        }
      })
      .catch(() => {
        /* 接口失败 · 退回本地 audience 兜底(fail-safe:agent/admin 文档身份不够仍挡) */
      })
    return () => {
      alive = false
    }
  }, [])

  const viewer = useMemo<HelpViewer>(
    () => ({ isAdmin, isAgent, isL2, hiddenSlugs: hiddenSlugs ?? undefined }),
    [isAdmin, isAgent, isL2, hiddenSlugs],
  )
  const effectiveSlug = slug || defaultHelpSlug(viewer)

  // 切换文档时回到顶部 · 让用户能从下一篇开头读
  // Layout 用 SidebarInset(<main>)做 overflow-y-auto 滚动 · window.scrollTo 无效
  // → 兜底:同时滚 window + 所有 main / overflow-y-auto 候选容器
  useEffect(() => {
    requestAnimationFrame(() => {
      window.scrollTo({ top: 0, behavior: 'auto' })
      document.querySelectorAll<HTMLElement>('main, [data-scroll-container]').forEach((el) => {
        if (el.scrollTop > 0) el.scrollTop = 0
      })
    })
  }, [effectiveSlug])

  // 先按身份过滤 · 再按搜索关键词过滤
  const visibleCategories = useMemo(() => getVisibleCategories(viewer), [viewer])
  const filtered = useMemo<DocCategory[]>(() => {
    const q = query.trim().toLowerCase()
    if (!q) return visibleCategories
    return visibleCategories
      .map((category) => ({
        ...category,
        nodes: category.nodes.filter((n) => n.title.toLowerCase().includes(q)),
      }))
      .filter((c) => c.nodes.length > 0)
  }, [query, visibleCategories])

  const current = findDoc(effectiveSlug)
  // 直达拦截:即使知道 slug,身份不够也看不到正文
  const accessDenied = current ? !canViewDoc(current.node, current.category, viewer) : false

  return (
    <div className="min-h-full bg-background">
      <div className="border-b bg-background/95 px-4 py-4 backdrop-blur md:px-8 lg:hidden">
        <div>
          <div className="min-w-0">
            <p className="text-xs text-muted-foreground">全域上榜GEO交付系统</p>
            <h1 className="truncate text-base font-semibold">操作说明书</h1>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <Button asChild variant="outline" size="sm">
              <Link to="/help/home">
                <PlayCircle className="size-4" />
                视频教程
              </Link>
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setMobileTreeOpen((open) => !open)}
              aria-expanded={mobileTreeOpen}
              aria-controls="mobile-help-tree"
            >
              {mobileTreeOpen ? <XIcon className="size-4" /> : <Menu className="size-4" />}
              {mobileTreeOpen ? '收起目录' : '打开目录'}
            </Button>
          </div>
        </div>
        {mobileTreeOpen && (
          <div id="mobile-help-tree" className="mt-4 max-h-[60dvh] overflow-y-auto border-t pt-4">
            <div className="relative mb-4">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索操作说明"
                className="h-10 pl-8 text-sm"
              />
            </div>
            <DocTree
              filtered={filtered}
              currentSlug={effectiveSlug}
              onNavigate={() => setMobileTreeOpen(false)}
            />
          </div>
        )}
      </div>

      <div className="grid lg:grid-cols-[310px_minmax(0,1fr)]">
        {/* 左 · 文档树(粘性) */}
        <aside className="hidden border-r bg-muted/[0.12] px-5 py-7 lg:sticky lg:top-0 lg:block lg:h-[calc(100dvh-3.5rem)] lg:self-start lg:overflow-y-auto">
          <div className="mb-5 flex items-start gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-md bg-primary/10 text-primary">
              <BookOpenCheck className="size-5" />
            </span>
            <div className="min-w-0">
              <p className="text-xs text-muted-foreground">全域上榜GEO交付系统</p>
              <h2 className="text-base font-semibold text-foreground">操作说明书</h2>
              <p className="mt-1 text-xs text-muted-foreground">
                {isAgent || isAdmin ? '服务商工作路径' : '普通用户工作路径'}
              </p>
            </div>
          </div>
          <div className="relative mb-4">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索操作说明"
              className="h-10 bg-background pl-8 text-sm"
            />
          </div>
          <Button
            asChild
            className="mb-5 min-h-11 w-full bg-emerald-400 text-sm font-semibold text-emerald-950 shadow-[0_0_0_1px_rgba(52,211,153,0.35),0_0_22px_rgba(52,211,153,0.28)] transition-[transform,background-color,box-shadow] duration-200 hover:-translate-y-0.5 hover:bg-emerald-300 hover:text-emerald-950 hover:shadow-[0_0_0_1px_rgba(110,231,183,0.5),0_0_28px_rgba(52,211,153,0.38)] focus-visible:ring-2 focus-visible:ring-emerald-300 motion-safe:animate-[pulse-subtle_2.6s_ease-in-out_infinite] motion-reduce:animate-none"
          >
            <Link to="/help/home" data-help-cta="video-home">
              <PlayCircle className="size-4" />
              打开视频教程主页
            </Link>
          </Button>
          <DocTree filtered={filtered} currentSlug={effectiveSlug} />
        </aside>

        <main className="min-w-0 px-5 py-8 md:px-10 md:py-12 xl:px-16">
          <div className="mx-auto max-w-[980px]">
            <div className="mb-6 hidden items-center justify-between gap-4 border-b pb-4 lg:flex">
              <p className="text-sm text-muted-foreground">
                正在阅读{isAgent || isAdmin ? '服务商' : '普通用户'}操作说明书
              </p>
              <Button asChild variant="ghost" size="sm">
                <Link to="/help/home">
                  <PlayCircle className="size-4" />
                  看视频教程
                  <ArrowRight className="size-3.5" />
                </Link>
              </Button>
            </div>
            {current && !accessDenied ? (
              <DocContent node={current.node} category={current.category} viewer={viewer} />
            ) : (
              <AccessDenied />
            )}
          </div>
        </main>
      </div>
    </div>
  )
}
