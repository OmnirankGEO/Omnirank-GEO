// [2026-05-18 帮助中心 Phase 3 v2] FAQ 编辑独立全屏页
// 路由: /admin/help-center/faq/new · /admin/help-center/faq/:id
// 替代原 Sheet 抽屉版编辑器 · markdown + 预览各占 ~600px · 体验完全不同

import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import ReactMarkdown from '@/components/SafeMarkdown'
import { toast } from 'sonner'
import {
  ArrowLeft,
  Eye,
  FileText,
  Loader2,
  Save,
  Trash2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import {
  createItem,
  deleteItem,
  getAdminItem,
  updateItem,
  type CreateItemInput,
} from './api'
import { CATEGORY_LABEL, type FAQCategory } from './types'

const DEFAULT_INPUT: CreateItemInput = {
  question: '',
  category: 'billing',
  answer_md: '',
  sort_order: 0,
  is_published: true,
}

function goBack(navigate: ReturnType<typeof useNavigate>) {
  // 优先返回历史栈 · 没有历史就回列表 · 防直接打开 url 卡死
  if (window.history.length > 1) navigate(-1)
  else navigate('/admin/help-center')
}

export default function FAQItemEditPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const isNew = !id || id === 'new'
  const itemId = isNew ? null : Number(id)

  const [loading, setLoading] = useState(!isNew)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [input, setInput] = useState<CreateItemInput>(DEFAULT_INPUT)
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  useEffect(() => {
    if (isNew || itemId == null) return
    setLoading(true)
    setLoadError(null)
    getAdminItem(itemId)
      .then((item) => {
        setInput({
          question: item.question,
          category: item.category,
          answer_md: item.answer_md,
          sort_order: item.sort_order,
          is_published: item.is_published,
        })
        setLoading(false)
      })
      .catch((err) => {
        setLoadError(String((err as Error).message))
        setLoading(false)
      })
  }, [itemId, isNew])

  const canSave = input.question.trim().length >= 2 && !saving

  const handleSave = async () => {
    if (!canSave) return
    setSaving(true)
    try {
      if (isNew) {
        await createItem(input)
        toast('已新建 FAQ')
      } else if (itemId != null) {
        await updateItem(itemId, input)
        toast('已保存')
      }
      goBack(navigate)
    } catch (err) {
      toast.error('保存失败', { description: String((err as Error).message) })
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async () => {
    if (itemId == null) return
    setDeleting(true)
    try {
      await deleteItem(itemId)
      toast('已删除')
      navigate('/admin/help-center')
    } catch (err) {
      toast.error('删除失败', { description: String((err as Error).message) })
    } finally {
      setDeleting(false)
      setConfirmDelete(false)
    }
  }

  // ========== 加载态 ==========
  if (loading) {
    return (
      <div className="flex min-h-full items-center justify-center text-muted-foreground">
        <Loader2 className="mr-2 size-4 animate-spin" />
        加载中
      </div>
    )
  }
  if (loadError) {
    return (
      <div className="mx-auto flex min-h-full max-w-md flex-col items-center justify-center p-8 text-center">
        <p className="text-sm text-foreground">{loadError}</p>
        <Button variant="outline" size="sm" className="mt-4" onClick={() => navigate('/admin/help-center')}>
          <ArrowLeft className="size-3.5" />
          返回列表
        </Button>
      </div>
    )
  }

  return (
    <div className="flex min-h-full flex-col bg-background">
      {/* 顶部 sticky · 返回 + 标题 + 操作 */}
      <header className="sticky top-0 z-10 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex w-full max-w-7xl items-center gap-3 px-4 py-3 md:px-8">
          <Button asChild variant="ghost" size="sm" className="-ml-2">
            <button onClick={() => goBack(navigate)}>
              <ArrowLeft className="size-4" />
              返回列表
            </button>
          </Button>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold text-foreground md:text-lg">
              {isNew ? '新建 FAQ' : `编辑 · ${input.question || `FAQ #${itemId}`}`}
            </h1>
          </div>
          <Button variant="ghost" onClick={() => goBack(navigate)} disabled={saving}>
            取消
          </Button>
          <Button onClick={handleSave} disabled={!canSave}>
            <Save className="size-4" />
            {saving ? '保存中...' : isNew ? '新建' : '保存'}
          </Button>
        </div>
      </header>

      {/* 主体 */}
      <div className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 md:px-8 md:py-8">
        {/* 问题 */}
        <div className="mb-5 space-y-1.5">
          <Label htmlFor="faq-question" className="text-sm">
            问题
          </Label>
          <Input
            id="faq-question"
            value={input.question}
            onChange={(e) => setInput({ ...input, question: e.target.value.slice(0, 200) })}
            placeholder="例: 消耗算力后生成失败会退款吗"
            className="h-11 text-base"
            maxLength={200}
            autoFocus={isNew}
          />
        </div>

        {/* 元数据三栏 */}
        <div className="mb-6 grid gap-4 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label className="text-sm">分类</Label>
            <Select
              value={input.category}
              onValueChange={(v) => setInput({ ...input, category: v as FAQCategory })}
            >
              <SelectTrigger className="h-10">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(CATEGORY_LABEL) as FAQCategory[]).map((c) => (
                  <SelectItem key={c} value={c}>
                    {CATEGORY_LABEL[c]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="faq-sort" className="text-sm">
              排序(小的在前)
            </Label>
            <Input
              id="faq-sort"
              type="number"
              value={input.sort_order}
              onChange={(e) => setInput({ ...input, sort_order: Number(e.target.value) || 0 })}
              className="h-10"
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-sm">是否上架</Label>
            <div className="flex h-10 items-center gap-3 rounded-md border bg-card px-3">
              <Switch
                checked={input.is_published}
                onCheckedChange={(v) => setInput({ ...input, is_published: v })}
              />
              <span className="text-sm text-muted-foreground">
                {input.is_published ? '已上架(用户可见)' : '未上架'}
              </span>
            </div>
          </div>
        </div>

        {/* 答案 markdown 编辑器 + 预览 · 50/50 占主屏 */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label className="text-sm">答案 (markdown)</Label>
            <span className="text-xs text-muted-foreground tabular-nums">
              {input.answer_md.length} / 20000
            </span>
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-lg border bg-card">
              <div className="flex items-center gap-1.5 border-b px-3 py-2 text-xs font-medium text-muted-foreground">
                <FileText className="size-3.5" />
                编辑
              </div>
              <Textarea
                value={input.answer_md}
                onChange={(e) => setInput({ ...input, answer_md: e.target.value.slice(0, 20000) })}
                rows={22}
                className="resize-none rounded-none border-0 font-mono text-sm focus-visible:ring-0"
                placeholder={'## 退款规则\n\n系统走幂等保护...\n\n- 多次消耗算力叠加: 后台手动核对\n- 退款到账: 1-3 个工作日'}
              />
            </div>
            <div className="rounded-lg border bg-card">
              <div className="flex items-center gap-1.5 border-b px-3 py-2 text-xs font-medium text-muted-foreground">
                <Eye className="size-3.5" />
                预览
              </div>
              <div className="min-h-[500px] p-4">
                {input.answer_md ? (
                  <div className="prose prose-sm dark:prose-invert max-w-none">
                    <ReactMarkdown>{input.answer_md}</ReactMarkdown>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    左边写 markdown, 这里实时预览效果
                  </p>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* 底部 · 删除区(只编辑模式显示) */}
        {!isNew && (
          <div className="mt-10 rounded-lg border border-destructive/30 bg-destructive/5 p-5">
            <h3 className="text-sm font-semibold text-destructive">危险操作</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              删除后用户立刻看不到, 关联的投票一起删 · 关联反馈保留(faq_id 置空)
            </p>
            <Button
              variant="destructive"
              size="sm"
              className="mt-3"
              onClick={() => setConfirmDelete(true)}
              disabled={deleting}
            >
              <Trash2 className="size-4" />
              删除这条 FAQ
            </Button>
          </div>
        )}
      </div>

      {/* 删除二次确认 */}
      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认删除这条 FAQ?</AlertDialogTitle>
            <AlertDialogDescription>
              「{input.question}」<br />
              删除后无法恢复, 用户立刻看不到这条 FAQ。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              disabled={deleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleting ? '删除中...' : '确认删除'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
