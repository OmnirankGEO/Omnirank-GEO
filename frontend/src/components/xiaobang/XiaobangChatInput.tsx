/**
 * XiaobangChatInput — 小榜输入框
 *
 * 比旧 旧对话输入框 简化:
 * - 没有 ABCD 弹层 / 没有 confirm callback / 没有外部状态注入
 * - 单行 textarea · Enter 发送 · Shift+Enter 换行
 * - 流式中按钮变 "停止" 可点(调 cancelStreaming)
 */
import {
  useRef,
  useState,
  useCallback,
  useEffect,
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
} from 'react'
import { ArrowUp, ImagePlus, Loader2, StopCircle, X } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { useAuth } from '@/context/AuthContext'

interface XiaobangChatInputProps {
  onSend: (
    text: string,
    options?: { attachmentText?: string; attachmentTitle?: string; attachmentPreviewUrl?: string },
  ) => void
  onStop?: () => void
  disabled?: boolean
  isStreaming?: boolean
  placeholder?: string
  className?: string
}

interface ParsedImageAttachment {
  fileName: string
  previewUrl: string
  attachmentText: string
  parsing: boolean
}

const IMAGE_MAX_BYTES = 10 * 1024 * 1024
const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp']

export function XiaobangChatInput({
  onSend,
  onStop,
  disabled = false,
  isStreaming = false,
  placeholder = '输入你的问题...',
  className,
}: XiaobangChatInputProps) {
  const { token } = useAuth()
  const [value, setValue] = useState('')
  const [attachment, setAttachment] = useState<ParsedImageAttachment | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const transferredPreviewUrlsRef = useRef<Set<string>>(new Set())

  const adjustHeight = useCallback(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 120) + 'px'
  }, [])

  useEffect(() => {
    return () => {
      if (attachment?.previewUrl && !transferredPreviewUrlsRef.current.has(attachment.previewUrl)) {
        URL.revokeObjectURL(attachment.previewUrl)
      }
    }
  }, [attachment?.previewUrl])

  const clearAttachment = useCallback(() => {
    setAttachment((prev) => {
      if (prev?.previewUrl && !transferredPreviewUrlsRef.current.has(prev.previewUrl)) {
        URL.revokeObjectURL(prev.previewUrl)
      }
      return null
    })
    if (fileInputRef.current) fileInputRef.current.value = ''
  }, [])

  const parseImage = useCallback(
    async (file: File) => {
      if (disabled || isStreaming) return
      if (!file.type.startsWith('image/') || (file.type && !IMAGE_TYPES.includes(file.type))) {
        toast.error('只支持 JPG / PNG / WebP / GIF / BMP 截图')
        return
      }
      if (file.size > IMAGE_MAX_BYTES) {
        toast.error('截图过大 · 请压缩到 10MB 以内')
        return
      }

      const previewUrl = URL.createObjectURL(file)
      setAttachment((prev) => {
        if (prev?.previewUrl && !transferredPreviewUrlsRef.current.has(prev.previewUrl)) {
          URL.revokeObjectURL(prev.previewUrl)
        }
        return {
          fileName: file.name || 'screenshot.png',
          previewUrl,
          attachmentText: '',
          parsing: true,
        }
      })

      try {
        const form = new FormData()
        form.append('file', file)
        const resp = await fetch('/api/xiaobang/parse-image', {
          method: 'POST',
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
          body: form,
        })
        const data = await resp.json().catch(() => ({}))
        if (!resp.ok || !data?.ok) {
          throw new Error(data?.detail || data?.message || `HTTP ${resp.status}`)
        }
        const attachmentText = String(data.attachment_text || '').trim()
        if (!attachmentText) {
          throw new Error('图片没看清，请直接打字描述问题')
        }
        setAttachment({
          fileName: String(data.title || file.name || 'screenshot.png'),
          previewUrl,
          attachmentText,
          parsing: false,
        })
      } catch (e) {
        URL.revokeObjectURL(previewUrl)
        setAttachment(null)
        toast.error((e as Error).message || '图片识别失败，请直接打字描述')
      } finally {
        if (fileInputRef.current) fileInputRef.current.value = ''
      }
    },
    [disabled, isStreaming, token],
  )

  const pickFile = () => {
    if (disabled || isStreaming) return
    fileInputRef.current?.click()
  }

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) void parseImage(file)
  }

  const handlePaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const item = Array.from(e.clipboardData.items).find((it) => it.type.startsWith('image/'))
    const file = item?.getAsFile()
    if (file) {
      e.preventDefault()
      void parseImage(file)
    }
  }

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setIsDragging(false)
    const file = Array.from(e.dataTransfer.files).find((f) => f.type.startsWith('image/'))
    if (file) void parseImage(file)
  }

  const handleSubmit = () => {
    const trimmed = value.trim()
    if (disabled || attachment?.parsing) return
    if (!trimmed && !attachment?.attachmentText) return
    const sentPreviewUrl = attachment?.previewUrl
    if (sentPreviewUrl) transferredPreviewUrlsRef.current.add(sentPreviewUrl)
    onSend(trimmed, {
      attachmentText: attachment?.attachmentText,
      attachmentTitle: attachment?.fileName,
      attachmentPreviewUrl: sentPreviewUrl,
    })
    setValue('')
    setAttachment((prev) => (prev?.previewUrl === sentPreviewUrl ? null : prev))
    if (fileInputRef.current) fileInputRef.current.value = ''
    requestAnimationFrame(adjustHeight)
  }

  const handleKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  const canSend = (value.trim().length > 0 || !!attachment?.attachmentText) && !disabled && !attachment?.parsing

  return (
    <div className={cn('border-t border-border/30 bg-background px-3 py-2.5', className)}>
      <input
        ref={fileInputRef}
        type="file"
        accept="image/jpeg,image/png,image/webp,image/gif,image/bmp"
        className="hidden"
        onChange={handleFileChange}
      />
      {/* items-center 让 textarea 跟发送按钮垂直居中(单行时不偏下)
          多行时 textarea 会自适应高度 · 按钮仍居中显示
          py-2 让上下空间均匀 · 文字不贴边 */}
      <div
        onDragOver={(e) => {
          e.preventDefault()
          if (!disabled && !isStreaming) setIsDragging(true)
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
        className={cn(
          'rounded-2xl border border-border/40 bg-muted/30 px-3 py-2 transition-colors',
          isDragging && 'border-brand/60 bg-brand/5',
        )}
      >
        {attachment && (
          <div className="mb-2 flex items-center gap-2 rounded-lg border border-border/40 bg-background/70 px-2 py-1.5">
            <img
              src={attachment.previewUrl}
              alt=""
              className="size-8 shrink-0 rounded border border-border/40 object-cover"
            />
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs text-foreground">{attachment.fileName}</p>
              <p className="text-[11px] text-muted-foreground">
                {attachment.parsing ? '正在识别截图...' : '已识别，可随问题一起发送'}
              </p>
            </div>
            {attachment.parsing ? (
              <Loader2 className="size-4 shrink-0 animate-spin text-muted-foreground" />
            ) : (
              <button
                type="button"
                onClick={clearAttachment}
                title="移除截图"
                className="shrink-0 rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <X className="size-3.5" />
              </button>
            )}
          </div>
        )}
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={pickFile}
            disabled={disabled || isStreaming || !!attachment?.parsing}
            title="上传截图"
            className={cn(
              'flex size-7 shrink-0 items-center justify-center rounded-full transition-colors',
              disabled || isStreaming || attachment?.parsing
                ? 'text-muted-foreground/40 cursor-not-allowed'
                : 'text-muted-foreground hover:bg-muted hover:text-foreground',
            )}
          >
            <ImagePlus className="size-4" />
          </button>
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => {
              setValue(e.target.value)
              adjustHeight()
            }}
            onKeyDown={handleKey}
            onPaste={handlePaste}
            rows={1}
            placeholder={placeholder}
            disabled={disabled && !isStreaming}
            className={cn(
              'block min-h-[20px] max-h-[120px] flex-1 resize-none bg-transparent',
              'py-0 align-middle',
              'text-sm leading-5 text-foreground placeholder:text-muted-foreground/60',
              'focus:outline-none',
            )}
          />
          {isStreaming && onStop ? (
            <button
              type="button"
              onClick={onStop}
              title="停止"
              className={cn(
                'flex size-7 shrink-0 items-center justify-center rounded-full',
                'bg-foreground text-background transition-opacity hover:opacity-80',
              )}
            >
              <StopCircle className="size-4" />
            </button>
          ) : (
            <button
              type="button"
              onClick={handleSubmit}
              disabled={!canSend}
              title="发送"
              className={cn(
                'flex size-7 shrink-0 items-center justify-center rounded-full transition-opacity',
                canSend
                  ? 'bg-brand text-white hover:opacity-90'
                  : 'bg-muted-foreground/20 text-muted-foreground/50 cursor-not-allowed',
              )}
            >
              <ArrowUp className="size-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
