/**
 * QuoteProgressStages — 报价计算阶段式进度(纯体验层 · 报价解释层一期 2026-06-13)
 *
 * ⚠️ 纯前端体验层 · 不参与价格计算 · 不写 DB · 不是真实 SSE。
 * 算价接口当前是阻塞请求(可能数秒),用阶段式文案缓解等待焦虑(对齐元指令#4 节奏 3-8s 随机)。
 * 推进到倒数第二阶段「正在做异常审计」后停住,等真实请求返回 → 父组件拿到 pricingData 即整卡卸载。
 * 若后续实测报价接口常 > 8s,再单独开「真实 SSE 报价进度」批次。
 */
import { useEffect, useState } from 'react'
import { Loader2, Check } from 'lucide-react'

const STAGES = [
  '正在读取关键词',
  '正在获取市场数据',
  '正在分析 AI 搜索竞争',
  '正在判断商业价值',
  '正在计算三档报价',
  '正在做异常审计',
] as const

export function QuoteProgressStages({ className = '' }: { className?: string }) {
  const [idx, setIdx] = useState(0)

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>
    const tick = () => {
      setIdx(prev => (prev >= STAGES.length - 1 ? prev : prev + 1)) // 停在最后阶段等真实完成
      timer = setTimeout(tick, 3000 + Math.random() * 4000) // 3-8s 随机
    }
    timer = setTimeout(tick, 3000 + Math.random() * 4000)
    return () => clearTimeout(timer)
  }, [])

  return (
    <div className={`space-y-1.5 ${className}`}>
      {STAGES.map((stage, i) => (
        <div key={stage} className="flex items-center gap-2 text-xs">
          {i < idx ? (
            <Check className="h-3.5 w-3.5 shrink-0 text-green-500" />
          ) : i === idx ? (
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-blue-400" />
          ) : (
            <span className="h-3.5 w-3.5 shrink-0 rounded-full border border-border" />
          )}
          <span className={i <= idx ? 'text-foreground' : 'text-muted-foreground'}>{stage}</span>
        </div>
      ))}
    </div>
  )
}
