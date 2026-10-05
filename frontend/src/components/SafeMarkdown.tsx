/**
 * SafeMarkdown — 全站唯一用户可见 Markdown 渲染 SSOT（硬门）
 *
 * 口径（Review-CTO §13.3 技术纠正 + census MARKDOWN_SINK_CENSUS_2026-07-22）：
 *  - react-markdown + remark-gfm（frontend/patches 已移除 autolink-literal
 *    lookbehind，实测全依赖树零 lookbehind，可安全保留）；禁止 rehype-raw
 *  - <details>/<summary> 不开放任意 HTML：结构化预处理把块级 details 解析为
 *    受控组件树（支持嵌套、未闭合自动收口、游离闭合降级为文本、围栏代码块免疫）
 *  - 链接仅 http/https 且无 userinfo；非法链接降级为纯文本 <span>；
 *    合法外链 target="_blank" rel="noopener noreferrer"
 *  - 图片按来源区分：自有图库（同源 + /uploads/article-images/<brand_id>/…，口径见
 *    config/owned_image_asset_policy.json，前后端同一份）正常渲染 <img>；
 *    其它一切（外部域名/data:/blob:/协议相对/内嵌 userinfo）维持文字占位不发请求
 *  - 表格 .safe-markdown-table 横向滚动包裹（320px 可读；打印还原）
 *  - 软换行保留为 <br>（与后端 markdown-it breaks=True 对齐）；代码块免疫
 *  - 打印前自动展开 details，打印后还原
 *  - 单行被压平的历史表格数据不逆向重建：标注 anomaly 后按原文安全展示
 *
 * 数据口径：只承载 Markdown 类型 DTO；raw text（用户输入回显等）不要走这里。
 */
import { Fragment, useEffect, useRef, type ComponentPropsWithoutRef, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { PluggableList } from 'unified'

import { cn } from '@/lib/utils'
// [返修单 T4] 自有图库判定与后端同读 config/owned_image_asset_policy.json
import { isOwnedImageUrl } from '@/lib/ownedImagePolicy'

type SafeMarkdownProps = {
  children: unknown
  className?: string
  components?: Components
  remarkPlugins?: PluggableList
}

type MarkdownNode = {
  type: string
  value?: string
  children?: MarkdownNode[]
}

// ---------------------------------------------------------------------------
// 链接安全：仅 http/https、无 userinfo、无控制字符/反斜杠（防 \\@ 混淆）
// ---------------------------------------------------------------------------
function safeHttpUrl(value: string | undefined): string | undefined {
  if (!value || /[\u0000-\u0020\u007f\\]/.test(value)) return undefined
  try {
    const parsed = new URL(value)
    const allowedScheme = parsed.protocol === 'http:' || parsed.protocol === 'https:'
    return allowedScheme && !parsed.username && !parsed.password ? parsed.href : undefined
  } catch {
    return undefined
  }
}

// react-markdown 会给自定义组件注入 `node`（hast 节点），透传到 DOM 会触发
// React unknown-prop 警告；各受保护组件统一剔除后再 spread。
type InjectedNodeProp = { node?: unknown }

function SafeLink({ href, children, node: _node, ...props }: ComponentPropsWithoutRef<'a'> & InjectedNodeProp) {
  const safeHref = safeHttpUrl(href)
  if (!safeHref) {
    // 非法/危险链接：保留可见文本，去掉跳转能力（与后端"去 href 保文本"一致）
    return <span className="safe-markdown-dead-link">{children}</span>
  }
  return (
    <a {...props} href={safeHref} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  )
}

function SafeImage({ src, alt, node: _node }: ComponentPropsWithoutRef<'img'> & InjectedNodeProp) {
  // [返修单 REWORK_T4_SAFEMARKDOWN_IMAGE_2026-07-29]
  //
  // 旧实现：`function SafeImage({ alt, node })` —— **连 src 都没接收**，
  // 无条件返回文字占位。那不是"加载失败"，是根本没打算加载：客户自己上传、
  // 已确认可外发、URL 实测 HTTP 200 的图，在写作大厅一张也画不出来。
  //
  // 挡外部图片的设计是对的（跟踪像素风险真实存在），所以这里**不是拆防线**，
  // 是按来源区分：自有图库（同源 + /uploads/article-images/<brand_id>/…）放行，
  // 其它一切（外部域名 / data: / blob: / 协议相对 / 内嵌 userinfo）维持占位。
  // 判定口径与后端读同一份 config/owned_image_asset_policy.json。
  const url = typeof src === 'string' ? src : undefined
  const safeSrc = safeHttpUrl(url) ?? (url && url.startsWith('/') ? url : undefined)
  if (safeSrc && isOwnedImageUrl(safeSrc)) {
    return (
      <img
        className="safe-markdown-owned-img"
        src={safeSrc}
        alt={alt ?? ''}
        loading="lazy"
        // 自有同源图不需要把 referrer 送出去；也不参与任何跨站请求。
        referrerPolicy="no-referrer"
        data-owned-image="true"
      />
    )
  }
  // 文案区分「外部图不加载」与「没有图」——别让用户再看到一个说不清是什么的方括号。
  return (
    <span className="safe-markdown-img-placeholder" data-owned-image="false">
      [外部图片不加载{alt ? `: ${alt}` : ''}]
    </span>
  )
}

function ScrollTable({ children, node: _node, ...props }: ComponentPropsWithoutRef<'table'> & InjectedNodeProp) {
  return (
    <div className="safe-markdown-table">
      <table {...props}>{children}</table>
    </div>
  )
}

// 受保护映射永远赢过调用方自定义 components（防调用方绕开链接/表格/图片策略）
const protectedComponents: Components = {
  a: SafeLink,
  img: SafeImage,
  table: ScrollTable,
  details: ({ children, node: _node, ...props }) => <details {...props}>{children}</details>,
  summary: ({ children, node: _node, ...props }) => <summary {...props}>{children}</summary>,
}

// ---------------------------------------------------------------------------
// remark 插件：软换行 → <br>（后端 breaks=True 对齐）；代码/行内代码免疫
// ---------------------------------------------------------------------------
function remarkPreserveSoftBreaks() {
  return (tree: MarkdownNode) => {
    const walk = (node: MarkdownNode) => {
      if (!node.children || node.type === 'code' || node.type === 'inlineCode') return
      const children: MarkdownNode[] = []
      node.children.forEach((child) => {
        if (child.type === 'text' && child.value?.includes('\n')) {
          child.value.split('\n').forEach((part, index) => {
            if (index > 0) children.push({ type: 'break' })
            if (part) children.push({ type: 'text', value: part })
          })
          return
        }
        walk(child)
        children.push(child)
      })
      node.children = children
    }
    walk(tree)
  }
}

// ---------------------------------------------------------------------------
// 压平历史数据检测：单行、含表格分隔行特征 → 不逆向重建，标注后原文展示
// ---------------------------------------------------------------------------
function structureAnomaly(text: string): 'flattened_table' | null {
  if (!text.trim() || text.includes('\n')) return null
  if ((text.match(/\|/g) || []).length >= 4 && /\|\s*:?-{3,}:?\s*\|/.test(text)) return 'flattened_table'
  return null
}

// ---------------------------------------------------------------------------
// details 结构化预处理（不开放任意 HTML 的受控映射）
//
// 报告生产端（services/report_writer_v2.py）把证据折叠写成块级：
//   <details>
//   <summary>…</summary>
//   （markdown 正文，可含嵌套 <details>）
//   </details>
// 解析规则：
//  - 标签必须独占一行（允许首尾空白）；同一行内联写法先规范化为独占行
//  - 支持嵌套；EOF 未闭合自动收口；游离 </details>/<summary> 降级为普通文本
//  - 生产端对 AI 原文的闭合标签已写为 <\/…>（带反斜杠），不会被误判为结构
//  - 围栏代码块（``` / ~~~）内的标签字面量免疫，不参与结构解析
//  - 行内 code span（成对反引号，含多反引号）与 4 空格/Tab 缩进代码块同属
//    保护区：其中的 <details>/<summary> 是字面值，按原文渲染为代码，不做
//    规范化与结构解析（"使用 `<details>` 标签"不会被渲成真实折叠块）
//  - <details> 允许受控属性：仅识别 open 语义，其余属性忽略但不静默丢弃整标签
// ---------------------------------------------------------------------------
type Segment =
  | { kind: 'markdown'; text: string }
  | { kind: 'details'; summary: string; open: boolean; children: Segment[] }

const DETAILS_OPEN_RE = /^<details(\s[^>]*)?>$/
const DETAILS_CLOSE_RE = /^<\/details>$/
const SUMMARY_RE = /^<summary>([\s\S]*)<\/summary>$/
const FENCE_RE = /^\s*(```|~~~)/
const INDENTED_CODE_RE = /^(?: {4}|\t)/
// 行内 code span：成对反引号（开闭反引号数一致），内容可含其它数量的反引号
const INLINE_CODE_RE = /(`+)([\s\S]*?)\1/g
const DETAILS_OPEN_ATTRS_RE = /<details(\s[^>]*)?>/g
const DETAILS_OPEN_SEMANTIC_RE = /(?:^|\s)open(?:\s|$)/

function normalizeDetailsLine(line: string): string[] {
  // 把同一行内的 details/summary 标签切到独立行；不匹配生产端转义的 <\/…>
  if (!line.includes('<')) return [line]
  // 先按行内 code span 切分保护区：区内的标签字面值原样保留，不做规范化
  const segments: { text: string; isCode: boolean }[] = []
  let cursor = 0
  for (const match of line.matchAll(INLINE_CODE_RE)) {
    const index = match.index ?? 0
    if (index > cursor) segments.push({ text: line.slice(cursor, index), isCode: false })
    segments.push({ text: match[0], isCode: true })
    cursor = index + match[0].length
  }
  if (cursor < line.length) segments.push({ text: line.slice(cursor), isCode: false })
  const pieces: string[] = []
  for (const segment of segments) {
    const text = segment.isCode
      ? segment.text
      : segment.text
          .replace(DETAILS_OPEN_ATTRS_RE, (tag) => `\n${tag}\n`)
          .replace(/<\/details>/g, '\n</details>\n')
          .replace(/<summary>([^<\n]*)<\/summary>/g, (_m, inner: string) => `\n<summary>${inner}</summary>\n`)
    pieces.push(...text.split('\n').filter((piece) => piece !== ''))
  }
  return pieces
}

function parseDetailsSegments(text: string): Segment[] {
  type Frame = { summary: string | null; open: boolean; children: Segment[]; buffer: string[] }
  const root: Frame = { summary: null, open: false, children: [], buffer: [] }
  const stack: Frame[] = [root]
  let inFence = false

  const flush = (frame: Frame) => {
    const chunk = frame.buffer.join('\n')
    frame.buffer = []
    if (chunk.trim()) frame.children.push({ kind: 'markdown', text: chunk })
  }
  const closeFrame = () => {
    if (stack.length <= 1) return false
    const frame = stack.pop()!
    flush(frame)
    stack[stack.length - 1].children.push({
      kind: 'details',
      summary: frame.summary ?? '',
      open: frame.open,
      children: frame.children,
    })
    return true
  }

  for (const rawLine of text.split('\n')) {
    if (FENCE_RE.test(rawLine)) {
      inFence = !inFence
      stack[stack.length - 1].buffer.push(rawLine)
      continue
    }
    if (inFence) {
      stack[stack.length - 1].buffer.push(rawLine)
      continue
    }
    if (INDENTED_CODE_RE.test(rawLine)) {
      // 4 空格/Tab 缩进代码块：整行原样保留，标签字面值不参与结构解析
      stack[stack.length - 1].buffer.push(rawLine)
      continue
    }
    for (const piece of normalizeDetailsLine(rawLine)) {
      const line = piece.trim()
      const openMatch = DETAILS_OPEN_RE.exec(line)
      if (openMatch) {
        flush(stack[stack.length - 1])
        // 受控解析：仅识别 open 属性语义，其余属性忽略（不静默丢弃整标签）
        const attrs = openMatch[1] ?? ''
        stack.push({ summary: null, open: DETAILS_OPEN_SEMANTIC_RE.test(attrs), children: [], buffer: [] })
        continue
      }
      if (DETAILS_CLOSE_RE.test(line)) {
        if (!closeFrame()) stack[stack.length - 1].buffer.push(piece) // 游离闭合 → 原文本
        continue
      }
      const summaryMatch = SUMMARY_RE.exec(line)
      const top = stack[stack.length - 1]
      if (summaryMatch && stack.length > 1 && top.summary === null && top.children.length === 0) {
        top.summary = summaryMatch[1]
        continue
      }
      top.buffer.push(piece)
    }
  }
  while (stack.length > 1) closeFrame() // EOF 未闭合自动收口
  flush(root)
  return root.children
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------
function MarkdownChunk({
  text,
  components,
  remarkPlugins,
}: {
  text: string
  components?: Components
  remarkPlugins: PluggableList
}) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkPreserveSoftBreaks, ...remarkPlugins]}
      components={{ ...components, ...protectedComponents }}
    >
      {text}
    </ReactMarkdown>
  )
}

function SummaryInline({ text, remarkPlugins }: { text: string; remarkPlugins: PluggableList }) {
  // summary 内容按行内 markdown 渲染（剥掉 <p> 外壳）；链接/图片策略同样生效
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, ...remarkPlugins]}
      components={{ p: ({ children }) => <Fragment>{children}</Fragment>, ...protectedComponents }}
    >
      {text}
    </ReactMarkdown>
  )
}

function Segments({
  segments,
  components,
  remarkPlugins,
}: {
  segments: Segment[]
  components?: Components
  remarkPlugins: PluggableList
}) {
  return (
    <>
      {segments.map((segment, index) =>
        segment.kind === 'markdown' ? (
          <MarkdownChunk
            key={index}
            text={segment.text}
            components={components}
            remarkPlugins={remarkPlugins}
          />
        ) : (
          <details key={index} open={segment.open}>
            <summary>
              {segment.summary ? (
                <SummaryInline text={segment.summary} remarkPlugins={remarkPlugins} />
              ) : (
                '展开详情'
              )}
            </summary>
            <Segments segments={segment.children} components={components} remarkPlugins={remarkPlugins} />
          </details>
        ),
      )}
    </>
  )
}

export function SafeMarkdown({ children, className, components, remarkPlugins = [] }: SafeMarkdownProps) {
  const text = String(children ?? '').replace(/\r\n?/g, '\n')
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // 打印前展开所有 details（屏幕折叠状态不应丢证据）；打印后还原
    const beforePrint = () => {
      rootRef.current?.querySelectorAll<HTMLDetailsElement>('details:not([open])').forEach((details) => {
        details.dataset.safeMarkdownPrintOpened = 'true'
        details.open = true
      })
    }
    const afterPrint = () => {
      rootRef.current
        ?.querySelectorAll<HTMLDetailsElement>('details[data-safe-markdown-print-opened="true"]')
        .forEach((details) => {
          details.open = false
          delete details.dataset.safeMarkdownPrintOpened
        })
    }
    window.addEventListener('beforeprint', beforePrint)
    window.addEventListener('afterprint', afterPrint)
    return () => {
      window.removeEventListener('beforeprint', beforePrint)
      window.removeEventListener('afterprint', afterPrint)
    }
  }, [])

  if (!text.trim()) return null

  const anomaly = structureAnomaly(text)
  if (anomaly) {
    return (
      <div ref={rootRef} className={cn('safe-markdown', className)} data-markdown-anomaly={anomaly}>
        <div className="safe-markdown-anomaly">历史内容的换行结构已缺失，以下按原文安全展示。</div>
        <p className="whitespace-pre-wrap break-words">{text}</p>
      </div>
    )
  }

  const segments = parseDetailsSegments(text)

  return (
    <div ref={rootRef} className={cn('safe-markdown', className)}>
      <Segments segments={segments} components={components} remarkPlugins={remarkPlugins} />
    </div>
  )
}

export type SafeMarkdownChildren = ReactNode
export type SafeMarkdownComponents = Components

export default SafeMarkdown
