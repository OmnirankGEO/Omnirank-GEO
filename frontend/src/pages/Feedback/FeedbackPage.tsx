import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
} from 'react'
import { toast } from 'sonner'
import {
  AlertCircle,
  CheckCircle2,
  Clipboard,
  FileImage,
  ImagePlus,
  LifeBuoy,
  Loader2,
  MessageSquareWarning,
  Send,
  Smartphone,
  UploadCloud,
  X,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import SafeMarkdown from '@/components/SafeMarkdown'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { authFetch } from '@/lib/api'
import { cn } from '@/lib/utils'

type Urgency = 'low' | 'mid' | 'high'
type Step = 'input' | 'answer' | 'submitted' | 'solved'
type ImageState = 'idle' | 'parsing' | 'ready' | 'error'
type XiaobangTriage = {
  answer: string
  confidence: 'high' | 'medium' | 'low' | ''
  handoff: boolean
}

const MAX_IMAGE_BYTES = 10 * 1024 * 1024
const ALLOWED_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp'])

function shouldSendToHuman(triage: XiaobangTriage) {
  const answer = triage.answer.trim()
  if (triage.handoff || triage.confidence === 'low') return true
  if (!answer) return true
  return /没找到|没有足够|无法准确|没有成功回答|建议提交|反馈给工作人员|人工/.test(answer)
}

async function parseSseTriage(response: Response): Promise<XiaobangTriage> {
  if (!response.ok) {
    throw new Error(`小榜暂时无法回答: ${response.status}`)
  }
  if (!response.body) {
    throw new Error('小榜没有返回内容')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let answer = ''
  let confidence: XiaobangTriage['confidence'] = ''
  let handoff = false

  const handleEvent = (block: string) => {
    const eventLine = block.split('\n').find((line) => line.startsWith('event:'))
    const dataLine = block.split('\n').find((line) => line.startsWith('data:'))
    const event = eventLine?.slice(6).trim()
    if (!dataLine) return
    try {
      const payload = JSON.parse(dataLine.slice(5).trim()) as {
        delta?: string
        confidence?: XiaobangTriage['confidence']
        handoff?: boolean
      }
      if (event === 'text') {
        answer += payload.delta || ''
      } else if (event === 'meta') {
        confidence = payload.confidence || confidence
        handoff = Boolean(payload.handoff)
      }
    } catch {
      // 忽略单帧解析失败,后续帧继续读。
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() || ''
    parts.forEach(handleEvent)
  }
  if (buffer.trim()) handleEvent(buffer)
  return { answer: answer.trim(), confidence, handoff }
}

function makeClientId() {
  const rand = Math.random().toString(36).slice(2, 10)
  return `bug_${Date.now()}_${rand}`
}

function urgencyText(value: Urgency) {
  if (value === 'high') return '卡死'
  if (value === 'low') return '不急'
  return '影响业务'
}

export default function FeedbackPage() {
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const pasteZoneRef = useRef<HTMLDivElement | null>(null)

  const [description, setDescription] = useState('')
  const [urgency, setUrgency] = useState<Urgency>('mid')
  const [file, setFile] = useState<File | null>(null)
  const [previewUrl, setPreviewUrl] = useState('')
  const [attachmentText, setAttachmentText] = useState('')
  const [screenshotKey, setScreenshotKey] = useState('')
  const [imageState, setImageState] = useState<ImageState>('idle')
  const [imageError, setImageError] = useState('')
  const [isDragging, setIsDragging] = useState(false)
  const [aiAnswer, setAiAnswer] = useState('')
  const [submitNote, setSubmitNote] = useState('')
  const [step, setStep] = useState<Step>('input')
  const [busy, setBusy] = useState(false)

  const locked = busy || step === 'answer'

  const canSubmit = useMemo(() => {
    return (description.trim().length >= 5 || !!file) && !busy && imageState !== 'parsing'
  }, [busy, description, file, imageState])

  const clearScreenshot = useCallback(() => {
    setPreviewUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev)
      return ''
    })
    setFile(null)
    setAttachmentText('')
    setScreenshotKey('')
    setImageState('idle')
    setImageError('')
    if (fileInputRef.current) fileInputRef.current.value = ''
  }, [])

  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl)
    }
  }, [previewUrl])

  const setScreenshot = useCallback(
    async (selected: File | null) => {
      clearScreenshot()
      if (!selected) return

      if (!selected.type.startsWith('image/') || !ALLOWED_IMAGE_TYPES.has(selected.type)) {
        toast.error('只支持 JPG / PNG / WebP / GIF / BMP 图片')
        return
      }
      if (selected.size > MAX_IMAGE_BYTES) {
        toast.error('截图不能超过 10MB')
        return
      }

      const nextPreviewUrl = URL.createObjectURL(selected)
      setFile(selected)
      setPreviewUrl(nextPreviewUrl)
      setImageState('parsing')
      setImageError('')

      const form = new FormData()
      form.append('file', selected)
      try {
        const res = await authFetch('/api/xiaobang/parse-image', {
          method: 'POST',
          body: form,
        })
        const data = await res.json().catch(() => ({}))
        if (!res.ok || data?.ok === false) {
          throw new Error(data?.detail || data?.message || `图片识别失败: ${res.status}`)
        }
        setAttachmentText(String(data.attachment_text || '').trim())
        setImageState('ready')
        if (!data.attachment_text) {
          setImageError('截图已附上，但图片内容没识别清楚。你可以在问题描述里多写两句。')
        }
      } catch (err) {
        setImageState('error')
        setImageError(String((err as Error).message || '图片没看清，请直接打字描述'))
        toast.error('图片没看清', { description: String((err as Error).message) })
      }
    },
    [clearScreenshot],
  )

  const handlePasteFile = useCallback(
    (items: DataTransferItemList | undefined) => {
      if (locked) return false
      const item = Array.from(items || []).find((it) => it.type.startsWith('image/'))
      const pastedFile = item?.getAsFile()
      if (!pastedFile) return false
      const ext = pastedFile.type.split('/')[1] || 'png'
      const namedFile = new File([pastedFile], `pasted-screenshot.${ext}`, { type: pastedFile.type })
      void setScreenshot(namedFile)
      return true
    },
    [locked, setScreenshot],
  )

  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement | HTMLDivElement>) => {
    if (handlePasteFile(event.clipboardData?.items)) {
      event.preventDefault()
    }
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setIsDragging(false)
    if (locked) return
    const droppedFile = Array.from(event.dataTransfer.files || []).find((item) => item.type.startsWith('image/'))
    if (droppedFile) void setScreenshot(droppedFile)
  }

  const uploadScreenshotForHuman = async () => {
    if (!file) return ''
    if (screenshotKey) return screenshotKey
    const form = new FormData()
    form.append('file', file)
    const res = await authFetch('/api/faq/feedback/screenshot', {
      method: 'POST',
      body: form,
    })
    if (!res.ok) {
      const err = await res.json().catch(() => null)
      throw new Error(err?.detail || `截图上传失败: ${res.status}`)
    }
    const data = (await res.json()) as { screenshot_url?: string }
    const key = data.screenshot_url || ''
    setScreenshotKey(key)
    return key
  }

  const askAi = async () => {
    if (!canSubmit) return
    setBusy(true)
    try {
      const message = description.trim() || '请帮我看一下这张截图，这里应该怎么操作？'
      const res = await authFetch('/api/xiaobang/chat', {
        method: 'POST',
        body: JSON.stringify({
          message,
          current_page: '/feedback',
          attachment_text: attachmentText || undefined,
        }),
      })
      const triage = await parseSseTriage(res)
      const answer = triage.answer || '这块我没有足够把握直接判断，建议提交给工作人员处理。'
      setAiAnswer(answer)
      if (shouldSendToHuman({ ...triage, answer })) {
        await submitHuman(answer, '小榜判断这件事不能直接解决，已自动转给工作人员。')
        return
      }
      setStep('answer')
    } catch (err) {
      const fallback = `小榜这次没有成功回答。错误信息: ${String((err as Error).message)}`
      setAiAnswer(fallback)
      await submitHuman(fallback, '小榜暂时无法判断，已自动转给工作人员。')
    } finally {
      setBusy(false)
    }
  }

  const submitHuman = async (answerOverride?: string, note?: string) => {
    setBusy(true)
    try {
      const key = await uploadScreenshotForHuman()
      const message = description.trim() || '用户上传截图后表示小榜没有解决问题。'
      const answerForHuman = answerOverride ?? aiAnswer
      const res = await authFetch('/api/faq/feedback', {
        method: 'POST',
        body: JSON.stringify({
          client_id: makeClientId(),
          kind: 'bug',
          message,
          urgency,
          screenshot_url: key,
          ai_answer: answerForHuman,
        }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => null)
        throw new Error(err?.detail || `提交失败: ${res.status}`)
      }
      setSubmitNote(note || '')
      setStep('submitted')
    } catch (err) {
      toast.error('提交失败', { description: String((err as Error).message) })
    } finally {
      setBusy(false)
    }
  }

  const resetForm = () => {
    setDescription('')
    setAiAnswer('')
    setSubmitNote('')
    setStep('input')
    clearScreenshot()
  }

  const attachmentLabel =
    imageState === 'parsing'
      ? '正在识别截图'
      : imageState === 'ready'
        ? attachmentText
          ? '截图已识别'
          : '截图已附上'
        : imageState === 'error'
          ? '截图已附上，识别失败'
          : '未附截图'

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 px-4 py-5 md:px-6 md:py-6">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border/60 pb-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-2xl font-semibold tracking-normal text-foreground">问题反馈</h1>
            <Badge variant="outline" className="gap-1 border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300">
              <MessageSquareWarning className="size-3.5" />
              先答后转人工
            </Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            描述你遇到的问题，可以附截图。小榜能解决就直接给答案，判断不了会转给工作人员。
          </p>
        </div>
        <div className="flex items-center gap-2 rounded-lg border border-border/60 bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          <LifeBuoy className="size-4 text-foreground" />
          登录用户均可提交
        </div>
      </header>

      {step === 'submitted' ? (
        <section className="rounded-lg border border-emerald-500/25 bg-emerald-500/5 p-6">
          <CheckCircle2 className="mb-3 size-8 text-emerald-500" />
          <h2 className="text-lg font-medium">已提交给工作人员</h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">
            {submitNote || '这条反馈已经带上你的描述、截图和小榜回答，工作人员会按紧急度处理。'}
          </p>
          <Button className="mt-5" onClick={resetForm}>
            继续反馈
          </Button>
        </section>
      ) : step === 'solved' ? (
        <section className="rounded-lg border border-emerald-500/25 bg-emerald-500/5 p-6">
          <CheckCircle2 className="mb-3 size-8 text-emerald-500" />
          <h2 className="text-lg font-medium">已关闭</h2>
          <p className="mt-2 text-sm text-muted-foreground">这次问题由小榜解决，没有提交人工。</p>
          <Button className="mt-5" onClick={resetForm}>
            反馈新问题
          </Button>
        </section>
      ) : (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
          <main
            ref={pasteZoneRef}
            onPaste={handlePaste}
            onDragOver={(event) => {
              event.preventDefault()
              if (!locked) setIsDragging(true)
            }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={handleDrop}
            className="space-y-4"
          >
            <section className="rounded-lg border border-border/60 bg-card">
              <div className="border-b border-border/60 px-4 py-3">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <MessageSquareWarning className="size-4 text-foreground" />
                  问题描述
                </div>
              </div>
              <div className="space-y-3 p-4">
                <Textarea
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  onPaste={handlePaste}
                  placeholder="比如：这个按钮为什么点不了、这里应该填什么、刚刚操作后页面没有变化"
                  className="min-h-36 resize-y text-sm leading-6"
                  disabled={locked}
                />
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground">紧急度</span>
                    <Select value={urgency} onValueChange={(value) => setUrgency(value as Urgency)} disabled={locked}>
                      <SelectTrigger className="w-36">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="low">不急</SelectItem>
                        <SelectItem value="mid">影响业务</SelectItem>
                        <SelectItem value="high">卡死</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="text-xs text-muted-foreground">
                    至少写 5 个字；只有截图也可以提交给小榜看。
                  </div>
                </div>
              </div>
            </section>

            <section className="rounded-lg border border-border/60 bg-card">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-4 py-3">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <FileImage className="size-4 text-foreground" />
                  截图证据
                </div>
                <Badge variant="outline" className="font-normal">
                  {attachmentLabel}
                </Badge>
              </div>
              <div className="p-4">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/jpeg,image/png,image/webp,image/gif,image/bmp"
                  className="hidden"
                  onChange={(event) => void setScreenshot(event.target.files?.[0] || null)}
                />
                {!previewUrl ? (
                  <button
                    type="button"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={locked}
                    className={cn(
                      'flex w-full flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border bg-muted/20 px-4 py-8 text-center transition-colors',
                      isDragging && 'border-brand bg-brand/5',
                      locked ? 'cursor-not-allowed opacity-60' : 'hover:border-brand/60 hover:bg-brand/5',
                    )}
                  >
                    <div className="flex size-11 items-center justify-center rounded-lg bg-background text-foreground shadow-sm">
                      <ImagePlus className="size-5" />
                    </div>
                    <div>
                      <div className="text-sm font-medium text-foreground">上传或粘贴截图</div>
                      <div className="mt-1 flex flex-wrap justify-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                        <span className="inline-flex items-center gap-1">
                          <Clipboard className="size-3.5" />
                          PC 可 Ctrl+V 粘贴
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <UploadCloud className="size-3.5" />
                          可点击上传 / 拖拽
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <Smartphone className="size-3.5" />
                          手机可从相册选择
                        </span>
                      </div>
                    </div>
                    <div className="text-xs text-muted-foreground">JPG / PNG / WebP / GIF / BMP，单张不超过 10MB</div>
                  </button>
                ) : (
                  <div className="grid gap-3 md:grid-cols-[220px_minmax(0,1fr)]">
                    <div className="overflow-hidden rounded-lg border border-border/60 bg-muted/20">
                      <img src={previewUrl} alt="问题截图" className="h-44 w-full object-contain" />
                    </div>
                    <div className="flex min-w-0 flex-col justify-between gap-3 rounded-lg border border-border/60 bg-muted/20 p-3">
                      <div className="min-w-0">
                        <div className="flex items-center justify-between gap-2">
                          <div className="truncate text-sm font-medium text-foreground">{file?.name || '问题截图'}</div>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon-sm"
                            onClick={clearScreenshot}
                            disabled={locked}
                            title="移除截图"
                          >
                            <X className="size-4" />
                          </Button>
                        </div>
                        <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                          {imageState === 'parsing' ? (
                            <Loader2 className="size-3.5 animate-spin" />
                          ) : imageState === 'error' ? (
                            <AlertCircle className="size-3.5 text-amber-500" />
                          ) : (
                            <CheckCircle2 className="size-3.5 text-emerald-500" />
                          )}
                          <span>{attachmentLabel}</span>
                        </div>
                        {imageError && (
                          <p className="mt-2 text-xs leading-5 text-amber-600 dark:text-amber-300">{imageError}</p>
                        )}
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => fileInputRef.current?.click()}
                          disabled={locked}
                        >
                          <ImagePlus className="mr-1 size-3.5" />
                          换一张
                        </Button>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </section>

            {step === 'input' && (
              <div className="flex flex-wrap items-center gap-3">
                <Button onClick={askAi} disabled={!canSubmit} className="h-9 min-w-40">
                  {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Send className="mr-2 size-4" />}
                  先让小榜看看
                </Button>
                <span className="text-xs text-muted-foreground">
                  小榜低把握或答不出来时，会直接转人工。
                </span>
              </div>
            )}

            {step === 'answer' && (
              <section className="rounded-lg border border-border/60 bg-card">
                <div className="border-b border-border/60 px-4 py-3 text-sm font-medium">小榜回答</div>
                <div className="p-4">
                  <SafeMarkdown className="text-sm leading-6 text-foreground">{aiAnswer}</SafeMarkdown>
                  <div className="mt-4 flex flex-wrap gap-2 border-t border-border/60 pt-4">
                    <Button onClick={() => setStep('solved')} disabled={busy} variant="outline">
                      <CheckCircle2 className="mr-2 size-4" />
                      解决了
                    </Button>
                    <Button onClick={() => void submitHuman()} disabled={busy}>
                      {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <UploadCloud className="mr-2 size-4" />}
                      没解决，提交人工
                    </Button>
                  </div>
                </div>
              </section>
            )}
          </main>

          <aside className="space-y-4">
            <section className="rounded-lg border border-border/60 bg-card p-4">
              <div className="text-sm font-medium text-foreground">处理流程</div>
              <ol className="mt-4 space-y-3 text-sm">
                {[
                  ['1', '先问小榜', '用你的描述和截图定位问题。'],
                  ['2', '能解决就给答案', '你确认有用后关闭，不占用人工。'],
                  ['3', '答不好转人工', '自动带上截图、紧急度和小榜回答。'],
                ].map(([num, title, desc]) => (
                  <li key={num} className="flex gap-3">
                    <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-xs text-foreground">
                      {num}
                    </span>
                    <span>
                      <span className="block font-medium text-foreground">{title}</span>
                      <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">{desc}</span>
                    </span>
                  </li>
                ))}
              </ol>
            </section>

            <section className="rounded-lg border border-border/60 bg-muted/20 p-4 text-sm">
              <div className="font-medium text-foreground">提交给人工时会带上</div>
              <ul className="mt-3 space-y-2 text-muted-foreground">
                <li className="flex items-center justify-between gap-2">
                  <span>问题描述</span>
                  <Badge variant="outline" className="font-normal">{description.trim() ? '已填写' : '待填写'}</Badge>
                </li>
                <li className="flex items-center justify-between gap-2">
                  <span>截图原图</span>
                  <Badge variant="outline" className="font-normal">{file ? '已附上' : '可选'}</Badge>
                </li>
                <li className="flex items-center justify-between gap-2">
                  <span>紧急度</span>
                  <Badge variant="outline" className="font-normal">{urgencyText(urgency)}</Badge>
                </li>
                <li className="flex items-center justify-between gap-2">
                  <span>小榜回答</span>
                  <Badge variant="outline" className="font-normal">{aiAnswer ? '已记录' : '先问后记录'}</Badge>
                </li>
              </ul>
            </section>
          </aside>
        </div>
      )}
    </div>
  )
}
