// [2026-05-18 帮助中心 Layer 3] 视频缩略图 + 卡片 + 弹窗播放
// [2026-05-26 v3] 缩略图用 <video preload="metadata"> 显示首帧 + 真实时长
//   - 之前砍掉是因为 mp4 moov 在尾 + 本地 462MB · preload=metadata 会拉接近整文件
//   - 现在 mp4 已 faststart(moov 在头)+ 上 OSS · preload=metadata 只拉头几百 KB
//   - 14 卡片 metadata 总加载 ~5-10 MB · 可接受(老板 5/26 反馈"没封面" → 恢复)

import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Clock, Play, X } from 'lucide-react'
import { toast } from 'sonner'
import type { VideoItem } from './videos-data'

function notReady(title: string) {
  toast('视频制作中 · 联系运营', {
    description: `《${title}》还在拍, 上线后会通知`,
  })
}

// 视频播放弹窗 · 用户点了才加载(此时 preload=auto 配合 mp4 faststart 秒起播)
function VideoLightbox({ video, onClose }: { video: VideoItem; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
    }
  }, [onClose])

  return createPortal(
    <div
      className="fixed inset-0 z-[120] flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div className="relative w-full max-w-5xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-2 flex items-center justify-between gap-3">
          <h3 className="text-sm font-medium text-white">{video.title}</h3>
          <button
            type="button"
            onClick={onClose}
            className="flex size-8 shrink-0 items-center justify-center rounded-full bg-white/15 text-white transition-colors hover:bg-white/25"
            aria-label="关闭"
          >
            <X className="size-4" />
          </button>
        </div>
        <video
          src={video.src}
          controls
          autoPlay
          playsInline
          preload="auto"
          className="aspect-video w-full rounded-lg bg-black"
        >
          你的浏览器不支持视频播放
        </video>
      </div>
    </div>,
    document.body,
  )
}

interface ThumbProps {
  video: VideoItem
  /** hero 区用 · 缩略图加大 */
  large?: boolean
  /** 自定义点击行为 · 不传则默认弹 Lightbox */
  onPlay?: () => void
}

export function VideoThumb({ video, large = false, onPlay }: ThumbProps) {
  const [open, setOpen] = useState(false)

  const handleClick = () => {
    if (onPlay) {
      onPlay()
      return
    }
    if (video.src) {
      setOpen(true)
      return
    }
    notReady(video.title)
  }

  return (
    <>
      <button
        type="button"
        onClick={handleClick}
        className="group relative aspect-video w-full overflow-hidden rounded-lg border bg-gradient-to-br from-muted to-muted/40 text-left transition-all hover:border-foreground/30 hover:shadow-md"
        aria-label={`播放 ${video.title}`}
      >
        {/* 有视频:首帧当封面(#t=0.5 跳到 0.5s 取帧 · 避免开头黑屏)
            preload=metadata + faststart 后浏览器只拉头几百 KB · 不会炸带宽 */}
        {video.src && (
          <video
            src={`${video.src}#t=0.5`}
            preload="metadata"
            muted
            playsInline
            tabIndex={-1}
            className="absolute inset-0 size-full object-cover"
          />
        )}
        <div className="absolute inset-0 flex items-center justify-center bg-foreground/5 transition-colors group-hover:bg-foreground/10">
          <span
            className={`flex items-center justify-center rounded-full bg-foreground/85 text-background transition-transform group-hover:scale-110 ${
              large ? 'size-16' : 'size-12'
            }`}
          >
            <Play className={large ? 'size-6 fill-current' : 'size-5 fill-current'} />
          </span>
        </div>
        {/* 右下角时长标签 · 用 videos-data.ts 手填值 */}
        {video.src && video.duration && (
          <span className="absolute bottom-2 right-2 rounded bg-foreground/80 px-1.5 py-0.5 text-xs font-medium text-background">
            {video.duration}
          </span>
        )}
      </button>
      {open && video.src && <VideoLightbox video={video} onClose={() => setOpen(false)} />}
    </>
  )
}

export function VideoCard({ video, onPlay }: { video: VideoItem; onPlay?: () => void }) {
  return (
    <div className="flex flex-col gap-2.5">
      <VideoThumb video={video} onPlay={onPlay} />
      <h3 className="line-clamp-2 text-sm font-medium leading-snug text-foreground">
        {video.title}
      </h3>
      {video.src && video.duration && (
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Clock className="size-3" />
          <span>{video.duration}</span>
        </div>
      )}
    </div>
  )
}
