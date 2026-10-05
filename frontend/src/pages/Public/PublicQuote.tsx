/**
 * PublicQuote — 公开报价单页面（无需登录）
 * 客户通过 /q/:code 链接访问，看到白标报价单
 */

import { useState, useEffect, useRef } from 'react'
import { useParams, Link } from 'react-router-dom'
import { Loader2, Printer } from 'lucide-react'
import { mountOpened, watchSawPrice, trackCtaClick } from '@/lib/customerEvents'
import { PrivacyNotice } from '@/components/customer/PrivacyNotice'
import { OssAttribution } from '@/components/common/OssAttribution'

interface QuoteService {
  name: string
  quantity: number
  unit_price: number
  subtotal: number
}

interface PublicQuoteData {
  services: QuoteService[]
  total_price: number
  whitelabel: {
    company_name?: string
    logo_url?: string
    slogan?: string
  }
  created_at: string
}

function toFiniteNumber(value: unknown): number | null {
  const num = Number(value)
  return Number.isFinite(num) ? num : null
}

function getServiceQuantity(svc: QuoteService): number {
  return toFiniteNumber(svc.quantity) ?? 1
}

function getServiceUnitPrice(svc: QuoteService): number {
  return toFiniteNumber(svc.unit_price) ?? toFiniteNumber((svc as any).price) ?? 0
}

function getServiceSubtotal(svc: QuoteService): number {
  const explicit = toFiniteNumber(svc.subtotal) ?? toFiniteNumber((svc as any).total)
  const calculated = getServiceQuantity(svc) * getServiceUnitPrice(svc)
  if (explicit !== null && (explicit > 0 || calculated <= 0)) return explicit
  return calculated
}

export default function PublicQuote() {
  const { code } = useParams<{ code: string }>()
  const [quote, setQuote] = useState<PublicQuoteData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const priceRef = useRef<HTMLDivElement | null>(null)

  // 客户行为埋点 · mount 即写 opened + 启动 dwell 计时器
  useEffect(() => {
    if (!code) return
    const cleanup = mountOpened({
      source: 'public_quote',
      rawToken: code,
      metadata: { share_code: code },
    })
    return () => cleanup()
  }, [code])

  // 客户行为埋点 · 价格段 50% 可见时写 saw_price (报价加载完后挂)
  useEffect(() => {
    if (!code || !quote || !priceRef.current) return
    const stop = watchSawPrice(priceRef.current, {
      source: 'public_quote',
      rawToken: code,
    })
    return () => stop()
  }, [code, quote])

  useEffect(() => {
    if (!code) return
    fetch(`/api/public/quote/${code}`)
      .then(r => r.json())
      .then(data => {
        if (data.status === 'success') {
          setQuote(data.quote)
        } else {
          setError(data.detail || '报价单不存在')
        }
      })
      .catch(() => setError('网络错误'))
      .finally(() => setLoading(false))
  }, [code])

  if (loading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (error || !quote) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <div className="text-center space-y-3">
          <p className="text-lg font-medium text-foreground">报价单不存在或已失效</p>
          <p className="text-sm text-muted-foreground">{error}</p>
          {/* [P2-15 fix 2026-05-23] token-only 客户身份不应跳登录 · 改为联系顾问 */}
          <p className="text-sm text-muted-foreground">请联系发送给您的顾问获取新报价</p>
        </div>
      </div>
    )
  }

  const { whitelabel, services, total_price } = quote

  return (
    <div className="min-h-screen bg-background">
      {/* 顶栏 */}
      <div className="border-b border-border bg-card/50 sticky top-0 z-10 print:hidden">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between">
          <span className="text-sm font-medium text-foreground">{whitelabel.company_name || '报价单'}</span>
          <button
            onClick={() => {
              if (code) {
                trackCtaClick({ source: 'public_quote', rawToken: code }, 'print_pdf')
              }
              window.print()
            }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-muted-foreground border border-border hover:text-foreground hover:bg-muted transition-colors"
          >
            <Printer className="h-3.5 w-3.5" />
            打印 / 保存 PDF
          </button>
        </div>
      </div>

      {/* 报价单内容 */}
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8">
        <div className="bg-card border border-border rounded-xl overflow-hidden print:border-none print:shadow-none">
          {/* 头部 */}
          <div className="px-6 py-8 sm:px-10 sm:py-10 border-b border-border">
            <div className="flex items-start justify-between gap-4">
              <div className="space-y-2">
                {whitelabel.logo_url ? (
                  <img src={whitelabel.logo_url} alt={whitelabel.company_name} className="h-10 w-auto object-contain" />
                ) : (
                  <h2 className="text-xl font-bold text-foreground">{whitelabel.company_name || '公司名称'}</h2>
                )}
                {whitelabel.slogan && <p className="text-sm text-muted-foreground">{whitelabel.slogan}</p>}
              </div>
              <div className="text-right shrink-0">
                <p className="text-sm font-medium text-muted-foreground">报价单</p>
                <p className="text-xs text-muted-foreground mt-1">{new Date(quote.created_at).toLocaleDateString('zh-CN')}</p>
              </div>
            </div>
          </div>

          {/* 服务明细 */}
          <div className="px-6 py-6 sm:px-10">
            <h3 className="text-sm font-semibold text-foreground mb-4">服务明细</h3>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border">
                    <th className="text-left py-3 pr-4 font-medium text-muted-foreground">服务项目</th>
                    <th className="text-center py-3 px-4 font-medium text-muted-foreground">数量</th>
                    <th className="text-right py-3 px-4 font-medium text-muted-foreground">单价 (元)</th>
                    <th className="text-right py-3 pl-4 font-medium text-muted-foreground">小计 (元)</th>
                  </tr>
                </thead>
                <tbody>
                  {services.map((svc, idx) => (
                    <tr key={idx} className="border-b border-border/50 last:border-0">
                      <td className="py-3 pr-4 text-foreground">{svc.name}</td>
                      <td className="py-3 px-4 text-center text-foreground">{getServiceQuantity(svc)}</td>
                      {/* 后端 total_price 仍是总价 SSOT;行小计缺失时只做展示兜底 */}
                      <td className="py-3 px-4 text-right text-foreground">{getServiceUnitPrice(svc).toFixed(2)}</td>
                      <td className="py-3 pl-4 text-right font-medium text-foreground">{getServiceSubtotal(svc).toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 合计 · saw_price IO 监听段 */}
            <div ref={priceRef} className="mt-6 flex justify-end">
              <div className="w-48 border-t-2 border-foreground pt-3">
                <div className="flex justify-between items-center">
                  <span className="text-sm font-medium text-foreground">合计</span>
                  <span className="text-xl font-bold text-foreground">{total_price.toFixed(2)} 元</span>
                </div>
              </div>
            </div>
          </div>

          {/* 服务说明 · 交付内容(客户面人话 · 报价解释层一期 2026-06-13 · 无任何内部成本/系数) */}
          <div className="px-6 py-5 sm:px-10 border-t border-border">
            <h3 className="text-sm font-semibold text-foreground mb-2">服务说明</h3>
            <p className="text-xs leading-relaxed text-muted-foreground">
              本方案是 AI 搜索优化的内容服务:针对上面这些关键词,在主流 AI 搜索 / 问答场景里建设优质内容,
              提升你的品牌被 AI 推荐、引用到的机会。竞争越激烈的词,需要建设的内容量越多。
              具体投放节奏和篇数,以为你服务的顾问最终确认为准。
            </p>
          </div>

          {/* WO_329 开源版署名位:放在报价卡片里,打印出来也在;开关关 ⇒ 不渲染 */}
          <OssAttribution />
        </div>

        {/* 隐私轻提示 (老板拍板文案) */}
        <div className="mt-6 print:hidden">
          <PrivacyNotice />
        </div>
      </div>
    </div>
  )
}
