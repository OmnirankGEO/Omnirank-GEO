// [2026-05-18 帮助中心 Phase 2] FAQ 子页 /help/faq
// 数据从 GET /api/faq/items 拉 · 投票 POST · 反馈 POST(失败落 localStorage)
// 页面 mount 时 drainQueue 重发上次离线的反馈

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import ReactMarkdown from '@/components/SafeMarkdown'
import {
  ArrowLeft,
  ChevronDown,
  HelpCircle,
  Loader2,
  MessageSquarePlus,
  Search,
  ThumbsDown,
  ThumbsUp,
} from 'lucide-react'
import { toast } from 'sonner'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import {
  faqCategories,
  type FAQCategoryId,
} from './faq-data'
import {
  fetchFAQItems,
  postVote,
  type FAQItemAPI,
  type Vote,
} from './faq-api-client'
import { drainQueue } from './faq-feedback'
import { FAQFeedbackDialog } from './FAQFeedbackDialog'

type Filter = FAQCategoryId | 'all'

function categoryLabel(id: FAQCategoryId): string {
  return faqCategories.find((c) => c.id === id)?.title ?? id
}

type FAQRowProps = {
  item: FAQItemAPI
  expanded: boolean
  onToggle: () => void
  onVote: (next: Vote | null) => void
  voteBusy: boolean
}

function FAQRow({ item, expanded, onToggle, onVote, voteBusy }: FAQRowProps) {
  const cast = (target: Vote) => {
    if (voteBusy) return
    // 点已选 = 撤销 · 点未选 = 切换 / 设置
    onVote(item.my_vote === target ? null : target)
  }

  return (
    <li className="rounded-lg border bg-card">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-4 py-3.5 text-left transition-colors hover:bg-muted/30"
        aria-expanded={expanded}
      >
        <ChevronDown
          className={`size-4 shrink-0 text-muted-foreground transition-transform ${
            expanded ? 'rotate-0' : '-rotate-90'
          }`}
        />
        <span className="flex-1 text-sm font-medium text-foreground">{item.question}</span>
        <span className="rounded-full border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground">
          {categoryLabel(item.category)}
        </span>
      </button>
      {expanded && (
        <div className="border-t px-4 py-4">
          {item.answer_md ? (
            <div className="prose prose-sm dark:prose-invert max-w-none">
              <ReactMarkdown>{item.answer_md}</ReactMarkdown>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              这个问题暂未收录, 正在补 · 也可以到页面底部提反馈让我们优先安排。
            </p>
          )}
          <div className="mt-4 flex items-center gap-2 border-t pt-3 text-xs text-muted-foreground">
            <span className="mr-1">这个回答有用吗?</span>
            <button
              type="button"
              onClick={() => cast('up')}
              disabled={voteBusy}
              aria-pressed={item.my_vote === 'up'}
              className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition-colors disabled:opacity-60 ${
                item.my_vote === 'up'
                  ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600'
                  : 'border-border bg-card text-muted-foreground hover:bg-muted'
              }`}
            >
              <ThumbsUp className="size-3" />
              <span>有用 {item.thumbs_up}</span>
            </button>
            <button
              type="button"
              onClick={() => cast('down')}
              disabled={voteBusy}
              aria-pressed={item.my_vote === 'down'}
              className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition-colors disabled:opacity-60 ${
                item.my_vote === 'down'
                  ? 'border-rose-500/40 bg-rose-500/10 text-rose-600'
                  : 'border-border bg-card text-muted-foreground hover:bg-muted'
              }`}
            >
              <ThumbsDown className="size-3" />
              <span>没用 {item.thumbs_down}</span>
            </button>
          </div>
        </div>
      )}
    </li>
  )
}

export default function HelpFAQ() {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [items, setItems] = useState<FAQItemAPI[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [voteBusy, setVoteBusy] = useState<Set<number>>(new Set())
  const [feedbackOpen, setFeedbackOpen] = useState(false)

  // 首次 mount: 拉列表 + drain 离线队列(best-effort)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    fetchFAQItems()
      .then((data) => {
        if (!cancelled) {
          setItems(data)
          setLoading(false)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setLoadError(String(err.message ?? err))
          setLoading(false)
        }
      })
    drainQueue()
      .then((res) => {
        if (res.sent > 0) {
          toast(`已重发 ${res.sent} 条离线反馈`)
        }
      })
      .catch(() => {
        /* drain 失败不打扰用户 */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return items.filter((item) => {
      if (filter !== 'all' && item.category !== filter) return false
      if (q && !item.question.toLowerCase().includes(q)) return false
      return true
    })
  }, [items, query, filter])

  const counts = useMemo(() => {
    const total: Record<Filter, number> = {
      all: items.length,
      billing: 0,
      operation: 0,
      data: 0,
      account: 0,
    }
    for (const item of items) total[item.category] += 1
    return total
  }, [items])

  const toggle = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleVote = async (faqId: number, next: Vote | null) => {
    setVoteBusy((prev) => new Set(prev).add(faqId))
    try {
      const result = await postVote(faqId, next)
      setItems((prev) =>
        prev.map((it) =>
          it.id === faqId
            ? {
                ...it,
                thumbs_up: result.thumbs_up,
                thumbs_down: result.thumbs_down,
                my_vote: result.my_vote,
              }
            : it,
        ),
      )
    } catch {
      // 🔴 [#78 2026-09-05] 原文案是「投票失败 · 网络问题, 稍后再试」——
      //    而真因是服务端 `user["id"]` KeyError 恒 500(确定性失败),
      //    「网络问题」是**我们并不知道**的原因,「稍后再试」则永远没用。
      //    一句**具体但错误**的话比笼统的更糟:笼统让她再看看/来问,
      //    错的具体让她停止追查并走错方向(去查自己的网)。
      //    所以只说**发生了什么**,不编原因。
      toast.error('投票没保存上,再点一次试试;还是不行的话告诉我们')
    } finally {
      setVoteBusy((prev) => {
        const next = new Set(prev)
        next.delete(faqId)
        return next
      })
    }
  }

  return (
    <div className="min-h-full bg-background">
      <div className="mx-auto w-full max-w-4xl px-4 py-6 md:px-8 md:py-8">
        {/* 顶部 · 返回 + 标题 */}
        <div className="mb-6">
          <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
            <Link to="/help">
              <ArrowLeft className="size-3.5" />
              返回帮助中心
            </Link>
          </Button>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground md:text-3xl">
            疑难杂症 FAQ
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            短问短答 · 找不到先搜 · 觉得回答好不好就点"有用 / 没用", 管理员能看到。
          </p>
        </div>

        {/* 搜索 */}
        <div className="relative mb-4">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜问题关键词"
            className="h-11 pl-10"
          />
        </div>

        {/* 分类 chip */}
        <div className="mb-6 flex flex-wrap gap-2">
          {faqCategories.map((cat) => {
            const active = filter === cat.id
            return (
              <button
                key={cat.id}
                type="button"
                onClick={() => setFilter(cat.id)}
                className={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                  active
                    ? 'border-primary bg-primary text-primary-foreground'
                    : 'border-border bg-card text-muted-foreground hover:bg-muted'
                }`}
              >
                {cat.title} <span className="opacity-70">{counts[cat.id]}</span>
              </button>
            )
          })}
        </div>

        {/* 列表 · loading / error / data / empty 4 态 */}
        {loading ? (
          <div className="flex min-h-[200px] items-center justify-center text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />
            加载中
          </div>
        ) : loadError ? (
          <div className="flex min-h-[200px] flex-col items-center justify-center rounded-xl border border-dashed bg-muted/20 p-8 text-center">
            <HelpCircle className="mb-2 size-8 text-destructive" />
            <p className="text-sm text-foreground">加载失败</p>
            <p className="mt-1 text-xs text-muted-foreground">{loadError}</p>
            <Button
              variant="outline"
              size="sm"
              className="mt-4"
              onClick={() => window.location.reload()}
            >
              刷新页面重试
            </Button>
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex min-h-[200px] flex-col items-center justify-center rounded-xl border border-dashed bg-muted/20 p-8 text-center">
            <HelpCircle className="mb-2 size-8 text-muted-foreground" />
            <p className="text-sm text-foreground">没找到匹配的问题</p>
            <p className="mt-1 text-xs text-muted-foreground">
              试试换个关键词, 或者切到"全部"分类
            </p>
          </div>
        ) : (
          <ul className="space-y-2">
            {filtered.map((item) => (
              <FAQRow
                key={item.id}
                item={item}
                expanded={expanded.has(item.id)}
                onToggle={() => toggle(item.id)}
                onVote={(v) => handleVote(item.id, v)}
                voteBusy={voteBusy.has(item.id)}
              />
            ))}
          </ul>
        )}

        {/* 底部 · 页面级反馈入口 */}
        <section className="mt-10 rounded-xl border bg-muted/30 p-6 text-center md:p-8">
          <h2 className="text-base font-semibold text-foreground">所有问题都列在上面了</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            还有别的想问 / 想补条 FAQ / 答案不准, 都可以提反馈, 管理员会跟进。
          </p>
          <Button
            type="button"
            size="lg"
            className="mt-4"
            onClick={() => setFeedbackOpen(true)}
          >
            <MessageSquarePlus className="size-4" />
            给管理员提反馈
          </Button>
        </section>
      </div>

      <FAQFeedbackDialog
        open={feedbackOpen}
        onOpenChange={setFeedbackOpen}
        items={items}
      />
    </div>
  )
}
