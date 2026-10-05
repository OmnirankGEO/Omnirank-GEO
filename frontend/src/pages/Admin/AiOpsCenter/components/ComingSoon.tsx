// 「待接入」占位:该板块暂无真实后端接口。诚实告知,不放任何伪造数据。

import { Construction } from 'lucide-react'

export function ComingSoon({ title, desc }: { title: string; desc?: string }) {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center rounded-xl border border-dashed border-border bg-card/40 p-10 text-center">
      <span className="grid size-12 place-items-center rounded-xl bg-amber-500/15 text-amber-400">
        <Construction className="size-6" />
      </span>
      <h3 className="mt-4 text-base font-semibold text-foreground">{title} · 待接入</h3>
      <p className="mt-2 max-w-md text-xs leading-5 text-muted-foreground">
        {desc || '该板块暂无对应真实接口,先做入口占位。接入后此处会展示真实数据,不做任何伪造。'}
      </p>
      <span className="mt-4 rounded-full border border-border px-3 py-1 text-[11px] text-muted-foreground">
        无 mock 数据 · 等真实 API
      </span>
    </div>
  )
}
