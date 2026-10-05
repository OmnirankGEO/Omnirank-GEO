# Density System v1 · 使用指南

> CTO-15.23 · 2026-05-22 · 老板"文字密密麻麻"收口
> 配套 CSS token:`src/styles/density.css`

## 为什么有这个系统

老板报"系统文字密密麻麻" · Codex 实测全站:
- `text-[10px]` ~765 处 · `text-[11px]` ~790 处 · `text-xs` ~3345 处
- Badge / rounded-full ~1473 处
- truncate / line-clamp 仍 ~431 处(已部分修)

**核心病不是单点截断 · 是设计系统层没管住信息层级**:
- 小字号太多 + Badge 太多 + 卡片太多 + 表格/列表没有主次
- 硬编码 Tailwind 不会自动吃 CSS token · 必须通过组件取代

## 三层信息模型(老板拍板)

| 层 | 用途 | token | 字号 | 字重 | 颜色 |
|---|---|---|---|---|---|
| L1 主要 | 用户马上要决策 · 标题/客户名/价格 | `.text-info-primary` | 15px(移动 16) | 600 | foreground |
| L2 次要 | 辅助判断 · 行业/城市/类型 | `.text-info-secondary` | 13px(移动 14) | 500 | foreground/78% |
| L3 辅助 | 证据/规则/技术解释 · 备注/原因 | `.text-info-tertiary` | 12px(移动 13) | 400 | muted-foreground |

**不要用** `text-[10px]` / `text-[11px]` 硬编码 · **改用** 三层 class

## 行密度(comfortable vs compact)

| 场景 | class | 行高 | 间距 |
|---|---|---|---|
| 阅读密集页(Publish/Quote/Brand/Diagnosis/Monitoring) | `.row-comfortable` | 48px(移动 56) | py 12 / px 16 |
| 工作台密集页(M3 sales 今日跟进) | `.row-compact` 或 `--pc-*` 紧凑 | 36px | py 8 / px 12 |

**不要用** `py-1` `py-1.5` 硬编码

## 5 个核心组件(Phase 2.1 加 InfoBadgeRow)

### 0. InfoBadgeRow · 图标 inline 陈列 + 单独 hover/tap 解释

**何时用**:页面顶部 / 表格列头有 N 个语义图标(≤6 个)· 每个图标本身有视觉语义(V / GEO / 🔥)

**何时不用**(用 InfoLegendPopover):
- badge > 7 个
- label 必读才能理解(rules / 计费规则 / 长说明)

**关键能力**:
- 全部 chip 直接 inline 陈列 · 无 button 包裹
- 每 chip 独立 tooltip popover · PC hover (150ms 延迟) / mobile tap
- popover 显 label(标题)+ description(说明)
- click-outside 自动收(mobile tap 模式)
- focus ring + aria-expanded · a11y 友好

**示例**:
```tsx
import { InfoBadgeRow } from '@/components/density';

<InfoBadgeRow
  items={[
    { key: 'v', badge: <span className="...">V</span>, label: '权威媒体',
      description: '权威媒体认证 · 高公信力新闻源' },
    { key: 'geo', badge: <span className="...">GEO</span>, label: '被 AI 引擎引用',
      description: 'GEO 调研观测到该媒体被 AI 搜索引擎引用' },
  ]}
/>
```

**老板 5/22 反馈背景**:
- v0 6 chip 文字平铺 · 视觉过载(老板截图核心问题)
- v1 InfoLegendPopover 包成 "徽章图例(6)" button · 文案生涩 + 用户不知道按钮里啥
- v2 InfoBadgeRow · 图标直摆 + cursor-help + tooltip · 用户看图标就懂 + hover/tap 看说明

### 1. ReadableTable · 表格

**何时用**:列 ≥ 5 的表格

**关键能力**:
- 列分级 `primary / secondary / tertiary`
  - primary 永远显示
  - secondary 默认最多 4 列(`maxSecondaryVisible` 可调)· 超出 + tertiary 一起折叠
  - tertiary 默认隐藏 · 用户点 "显示更多列(N)" 一并展开
- 外层自动 `overflow-x-auto` · 防 minWidth fit-content 撑爆页面
- 行高自动应用 `comfortable` 或 `compact`
- 列宽走 `--table-primary-min / --table-secondary-min / --table-meta-min`
- 字号自动按 tier 走三层 token

**示例**:
```tsx
import { ReadableTable } from '@/components/density';

<ReadableTable
  rows={mediaList}
  rowKey={(m) => m.id}
  columns={[
    { key: 'name', label: '媒体名称', tier: 'primary', minWidth: 200 },
    { key: 'price', label: '价格', tier: 'secondary', minWidth: 80, align: 'right' },
    { key: 'rate', label: '收录率', tier: 'secondary', minWidth: 64 },
    { key: 'avgTime', label: '出稿', tier: 'tertiary', minWidth: 80 },
    { key: 'weight', label: '权重', tier: 'tertiary', minWidth: 64 },
  ]}
  renderCell={(row, col) => row[col.key]}
  onRowClick={(r) => navigate(`/media/${r.id}`)}
/>
```

### 2. BadgeOverflowGroup · Badge 容器

**何时用**:一行 badge ≥ 3 个

**关键能力**:
- 常驻最多 N(默认 2)彩色
- 超出进 "+N" overflow chip · click 展开 popover 看全
- 整体 chip 颜色 token 统一(blue/green/amber/red/purple/gray)

**示例**:
```tsx
import { BadgeOverflowGroup } from '@/components/density';

<BadgeOverflowGroup
  maxVisible={2}
  badges={[
    { key: 'authority', label: '权威', color: 'blue', tooltip: '权威媒体认证' },
    { key: 'geo', label: 'GEO', color: 'green', tooltip: 'AI 引擎引用过' },
    { key: 'engine6', label: '6 引擎', color: 'amber' },
    { key: 'lowprice', label: '低价警告', color: 'red' },
  ]}
/>
```

### 3. InfoLegendPopover · 图例/规则

**何时用**:页面顶部有"规则说明" "图例" "计费解释" 等元信息

**关键能力**:
- 默认收起成 1 个 button "ℹ️ 图例(N)"
- 点开 popover · 看分组的全部图例
- 支持分组(基础徽章 / 价格警示 / 风险标识)
- click-outside 自动收

**示例**:
```tsx
import { InfoLegendPopover } from '@/components/density';

<InfoLegendPopover
  buttonLabel="图例"
  groups={[
    {
      title: '基础徽章',
      items: [
        { badge: <span className="...">V</span>, label: '权威媒体', description: '权威媒体认证' },
        { badge: <span className="...">GEO</span>, label: 'GEO 引用', description: 'AI 搜索引擎引用过' },
      ],
    },
    {
      title: '价格警示',
      items: [
        { badge: '⚠️', label: '低价', description: '低于成本 · 收录率仅 17%' },
      ],
    },
  ]}
/>
```

### 4. ExpandableMeta · 长 remark / reason / description

**何时用**:表格行 / 列表项 内嵌 `recommendation_reason` `remark` `description` 等长文本

**关键能力**:
- 默认 preview 模式 · 短摘要(默认 30 chars)+ "看详情" link
- iconOnly 模式 · 只显 icon button · 适合表格末列
- 点开 popover · 完整看 + click-outside 关

**示例**:
```tsx
import { ExpandableMeta } from '@/components/density';

// preview mode · 短摘要 + "看详情"
<ExpandableMeta text={kw.recommendation_reason} maxChars={30} />

// iconOnly mode · 表格末列只显 💬 icon · 默认 align=end 防右屏溢出
<ExpandableMeta text={m.remark} mode="iconOnly" iconLabel="备注" />

// 显式 align(在 PC 表格最右侧列里 popover 需要往左展开)
<ExpandableMeta text={m.remark} mode="preview" align="end" />
```

## Badge 预算红线

**每行常驻 ≤ 2 彩色 badge** · 超出走 `BadgeOverflowGroup`

例外:
- `状态 pill`(单个 · 业务必要):OK 不算
- `key 数值 metric`(只 1 个 · 如 "63 分"):OK 不算

## 反例不要做

❌ `<span className="text-[10px] text-muted-foreground">{remark}</span>`
✅ `<ExpandableMeta text={remark} mode="preview" />`

❌ `<div className="grid grid-cols-[1fr_80px_60px_60px_60px_60px_120px_100px]">` (8 列固定 1080px)
✅ `<ReadableTable columns={[...]} />` (自动分级)

❌ `<div className="flex gap-1 flex-wrap"><Badge>...</Badge> x 6</div>`
✅ `<BadgeOverflowGroup badges={[...]} maxVisible={2} />`

❌ `<div className="text-[10px] flex gap-1">图例:V=权威 GEO=引用 ...</div>`
✅ `<InfoLegendPopover groups={[...]} />`

## Phase 路线

- **Phase 1**(本 commit · 已完成):token + 4 组件 + 文档
- **Phase 2**(待开):P0 4 件样板页应用 · PublishCenter / KeywordTable / DiagnosisReport / QuotesListPage
- **Phase 3**(待开):P1 4 件 · BrandDetail / OnlineQuoteFlow / ClientWorkbench / NewDiagnosis
- **Phase 4**(后续):SocialStudio territory 由 CTO-13.0 单独开专项

## 验收方式

每个样板页提交前 · 必须截图实证 4 个宽度:
- 移动 375px / 390px / 430px
- 桌面 1440px

老板可对比 before/after 看效果。
