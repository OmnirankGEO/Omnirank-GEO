/**
 * SearchableSelect 组件级验证台（仅 dev · 不进生产包）
 *
 * 入口 frontend/searchable-select-harness.html，不被 index.html 引用，
 * vite build 只以 index.html 为入口 → 不会打进产物（由 §锁 校验产物无此文件）。
 *
 * 覆盖：锁1(过滤/清空恢复) 锁2(自动聚焦) 锁3(关闭重置) 锁4(键盘) 锁6(阈值分流) 锁7(aria)
 */

import * as React from 'react'
import { createRoot } from 'react-dom/client'
import { SearchableSelect, type SearchableSelectOption } from '@/components/ui/searchable-select'
import '../index.css'

const THIRTY: SearchableSelectOption[] = Array.from({ length: 30 }, (_, i) => ({
    value: `opt-${i + 1}`,
    label: `选项 ${i + 1} · Item-${i + 1}`,
    keywords: i % 3 === 0 ? 'ABC-Keyword' : undefined,
}))

const SIX: SearchableSelectOption[] = Array.from({ length: 6 }, (_, i) => ({
    value: `few-${i + 1}`,
    label: `少量选项 ${i + 1}`,
}))

const NINE: SearchableSelectOption[] = Array.from({ length: 9 }, (_, i) => ({
    value: `nine-${i + 1}`,
    label: `边界选项 ${i + 1}`,
}))

const ENGLISH: SearchableSelectOption[] = [
    { value: 'zhaoshang', label: 'MEIGOU-OVERSEAS Ltd' },
    { value: 'gongshang', label: 'Alpha Beta GmbH' },
    { value: 'jianshe', label: 'ZETA Holdings' },
    ...Array.from({ length: 12 }, (_, i) => ({ value: `en-${i}`, label: `Filler Corp ${i}` })),
]

function Case({
    id,
    title,
    options,
    searchable,
}: {
    id: string
    title: string
    options: SearchableSelectOption[]
    searchable?: boolean
}) {
    const [value, setValue] = React.useState<string | null>(null)
    return (
        <section style={{ marginBottom: 24 }} data-case={id}>
            <h2 style={{ fontSize: 14, marginBottom: 6 }}>
                {title}（{options.length} 项{searchable !== undefined ? ` · searchable=${String(searchable)}` : ''}）
            </h2>
            <div style={{ width: 320 }}>
                <SearchableSelect
                    id={`trigger-${id}`}
                    options={options}
                    value={value}
                    onChange={setValue}
                    placeholder="请选择"
                    searchable={searchable}
                    aria-label={title}
                />
            </div>
            <p data-selected={id} style={{ fontSize: 12, marginTop: 6 }}>
                selected={value ?? '(null)'}
            </p>
        </section>
    )
}

function Harness() {
    return (
        <div style={{ padding: 24, fontFamily: 'system-ui' }}>
            <h1 style={{ fontSize: 18, marginBottom: 16 }}>SearchableSelect harness</h1>
            <Case id="thirty" title="30 选项 · 自动判定应有搜索框" options={THIRTY} />
            <Case id="six" title="6 选项 · 阈值以下不应有搜索框" options={SIX} />
            <Case id="nine" title="9 选项 · 阈值边界应有搜索框" options={NINE} />
            <Case id="forced-off" title="30 选项 · 显式关闭搜索" options={THIRTY} searchable={false} />
            <Case id="forced-on" title="6 选项 · 显式打开搜索" options={SIX} searchable={true} />
            <Case id="english" title="英文品牌名 · 大小写不敏感" options={ENGLISH} />
        </div>
    )
}

createRoot(document.getElementById('root')!).render(<Harness />)
