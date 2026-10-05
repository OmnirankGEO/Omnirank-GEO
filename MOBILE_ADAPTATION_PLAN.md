# OmniRank 移动端适配方案

> 状态跟踪文档 — 每完成一步更新状态
> 最后更新: 2026-03-25 16:10
>
> **进度: Phase 1-4 全部完成, Phase 5 全部完成, 已构建部署**
>
> **Phase 5 完成内容**:
> - QuoteCenter.tsx: 全部9处 grid-cols 修复（客户选择、AI分析、品牌信息、套餐选择、报价摘要、成交信息、服务期、查看报价指标卡片）
> - OnlineQuoteFlow.tsx: 移动端侧边栏改为抽屉式（w-72 sidebar → 滑出drawer + 遮罩层）、4处 grid-cols 响应式、内容区 padding 适配、空状态文案适配
> - Monitoring/index.tsx: 页面 padding 响应式、标题栏堆叠、按钮 flex-wrap、投放管理 grid 响应式、趋势统计卡片字号适配
> - BrandList.tsx: 页头标题栏堆叠、搜索栏响应式、品牌卡片 flex-col 堆叠、操作按钮 flex-wrap、头像/字号缩放
> - DiagnosisReport.tsx: 已检查，310行纯 prose 内容，天然响应式，无需改动

## 背景

系统面向上百上千销售使用，销售整体素质不高，移动端体验必须符合直觉。
当前 GEO 业务页面（非社媒）在 375px 手机屏上表格溢出、布局混乱、不可用。
社媒操盘手页面已完成移动端适配（BottomNav + Tailwind 改造）。

## 架构决策

**一套代码，两种渲染。不做两套前端。**

```
同一个 URL / API / 认证
    │
    ├─ 屏幕 ≥ 1024px → 桌面布局（Sidebar + 表格）
    └─ 屏幕 < 1024px → 移动布局（底部Tab + 卡片视图）
```

## 执行计划

---

### Phase 1: 全局基础设施

#### Task 1.1 — 创建 useIsMobile hook
- **文件**: `frontend/src/hooks/useIsMobile.ts`（新建）
- **作用**: 统一设备检测，替代分散的 `window.innerWidth` 判断
- **实现**: 监听 `(max-width: 1023px)` media query
- **状态**: [x] 已完成

#### Task 1.2 — 创建 MobileTabBar 组件
- **文件**: `frontend/src/components/layout/MobileTabBar.tsx`（新建）
- **作用**: GEO 业务页面的底部导航（类似社媒 BottomNav）
- **Tab 结构**:
  - 首页 🏠 → `/`
  - 客户 🏢 → `/brands`
  - 诊断 🔍 → `/diagnosis/new`
  - 报价 📊 → `/quote`
  - 更多 ☰ → 展开菜单（历史/监测/报告/社媒/设置等）
- **参考**: `components/social/BottomNav.tsx` 的实现模式
- **状态**: [x] 已完成

#### Task 1.3 — Layout.tsx 集成 MobileTabBar
- **文件**: `frontend/src/components/layout/Layout.tsx`（修改，68行）
- **改动**:
  - 引入 MobileTabBar 组件
  - 在 `</main>` 之前、移动端（`lg:hidden`）渲染 MobileTabBar
  - 给 main 内容区加 `pb-20 lg:pb-0`（预留底部导航空间）
  - 非社媒页面才显示（`!isSocialPage` 时）
- **状态**: [x] 已完成

#### Task 1.4 — ClientContextBar 移动端适配
- **文件**: `frontend/src/components/layout/ClientContextBar.tsx`（修改，340行）
- **问题**:
  - Line 106: `flex items-center gap-5 px-6` 水平排列过密
  - Line 118: 下拉触发按钮 `min-w-[200px]` 在 375px 上占一半以上
  - Line 133: 弹出面板 `w-[380px]` 超出手机屏幕
- **改动**:
  - 外层: `flex-col md:flex-row` + `gap-2 md:gap-5` + `px-3 md:px-6`
  - 下拉按钮: 去掉 `min-w-[200px]`，移动端用 `w-full md:min-w-[200px]`
  - 弹出面板: `w-[380px]` → `w-[calc(100vw-24px)] md:w-[380px]`
  - 品牌信息（分数/诊断次数等）: 移动端换到第二行，用 `flex-wrap`
- **状态**: [x] 已完成

---

### Phase 2: 表格页 → 移动端卡片视图（核心改动）

#### Task 2.1 — HistoryList.tsx 移动端卡片
- **文件**: `frontend/src/pages/History/HistoryList.tsx`（修改，314行）
- **当前问题**:
  - Line 164-277: 8列 Table（ID/品牌/行业/评分/等级/版本/时间/操作）
  - Line 226-272: 每行4个操作按钮（复测/查看报告/写作中心/删除）
  - Line 139-161: 搜索+筛选横排溢出
- **改动**:
  - 搜索栏: `flex gap-4` → `flex flex-col sm:flex-row gap-3`
  - 引入 useIsMobile
  - 桌面端: 保留现有 Table（不动）
  - 移动端: 渲染卡片列表，每张卡片显示:
    ```
    ┌─────────────────────────────────┐
    │ 品牌名称                   #ID  │
    │ 行业类别                        │
    │ [21/100] [起步] [完整版]        │
    │ 2026-03-20                      │
    │ [查看报告]  [复测]  [🗑️]       │
    └─────────────────────────────────┘
    ```
- **状态**: [x] 已完成

#### Task 2.2 — UserManagement.tsx 移动端卡片
- **文件**: `frontend/src/pages/Admin/UserManagement.tsx`（修改，406行）
- **当前问题**:
  - Line 214-293: 7列 table（用户名/显示名/角色/客户/状态/最后登录/操作）
  - Line 256-287: 每行6个操作按钮
- **改动**:
  - 引入 useIsMobile
  - 桌面端: 保留现有 table
  - 移动端: 卡片列表，每张显示:
    ```
    ┌─────────────────────────────────┐
    │ 👤 显示名           [启用/禁用] │
    │ @username · 角色名             │
    │ 最后登录: 2026-03-20           │
    │ [编辑] [角色] [客户] [重置密码] │
    └─────────────────────────────────┘
    ```
- **状态**: [x] 已完成

#### Task 2.3 — AuditLogs.tsx 移动端卡片
- **文件**: `frontend/src/pages/Admin/AuditLogs.tsx`（修改，266行）
- **当前问题**:
  - Line 137-198: 7列 table（时间/用户/操作/模块/摘要/IP/详情）
- **改动**:
  - 引入 useIsMobile
  - 桌面端: 保留现有 table
  - 移动端: 简化卡片，每条显示:
    ```
    ┌─────────────────────────────────┐
    │ [操作类型]  用户名    03-20 14:30│
    │ 摘要内容...                     │
    └─────────────────────────────────┘
    ```
- **状态**: [x] 已完成

---

### Phase 3: 响应式修补（快速修复）

#### Task 3.1 — Dashboard.tsx 内边距
- **文件**: `frontend/src/pages/Dashboard.tsx`
- **改动**: Line ~103 `p-8` → `p-4 md:p-8`
- **状态**: [x] 已完成

#### Task 3.2 — Monitoring/index.tsx 确认横滑
- **文件**: `frontend/src/pages/Monitoring/index.tsx`（2377行）
- **现状**: Line 1260 已有 `overflow-x-auto`，Line 1261 `min-w-[960px]`
- **改动**: 确认移动端可横滑即可，可能需微调
- **状态**: [x] 已完成

#### Task 3.3 — ClientMaterialsEditor.tsx Grid 响应式
- **文件**: `frontend/src/pages/Diagnosis/ClientMaterialsEditor.tsx`
- **改动**: Line 286 `grid-cols-3` → `grid-cols-1 md:grid-cols-3`
- **状态**: [x] 已完成

#### Task 3.4 — ExportReportModal.tsx Grid 响应式
- **文件**: `frontend/src/pages/Diagnosis/ExportReportModal.tsx`
- **改动**: Line 147 `grid-cols-2` → `grid-cols-1 sm:grid-cols-2`
- **状态**: [x] 已完成

#### Task 3.5 — AdvisorTeam.tsx Grid 响应式
- **文件**: `frontend/src/pages/Employees/AdvisorTeam.tsx`
- **改动**: Line 326 `grid-cols-3` → `grid-cols-1 sm:grid-cols-3`
  （Line 373 已有响应式 `grid-cols-1 md:grid-cols-2 lg:grid-cols-3`，不用改）
- **状态**: [x] 已完成

#### Task 3.6 — BrandList.tsx 移动端微调
- **文件**: `frontend/src/pages/Brands/BrandList.tsx`（628行）
- **现状**: 已是卡片布局，但操作按钮可能在手机端溢出
- **改动**:
  - 卡片内 flex 布局加 `flex-wrap` 或移动端堆叠
  - 操作按钮区域响应式调整
- **状态**: [x] 已完成

---

### Phase 4: 构建 & 部署

#### Task 4.1 — 构建验证
- `cd frontend && npx vite build`
- 确认无编译错误
- **状态**: [x] 已完成

#### Task 4.2 — 部署到容器
- `docker cp dist/. omnirank-ai:/app/frontend/dist/ && docker exec omnirank-ai nginx -s reload`
- **状态**: [x] 已完成

#### Task 4.3 — 更新 CLAUDE.md
- 记录本次移动端适配完成的工作
- **状态**: [x] 已完成

---

### Phase 5: 销售核心页面内容区适配

#### Task 5.1 — QuoteCenter.tsx 全量 grid-cols 修复
- **文件**: `frontend/src/pages/Quote/QuoteCenter.tsx`（修改，2920行）
- **改动**: 9处 grid-cols 全部加响应式前缀
  - 客户选择/诊断报告 grid-cols-2 → 1/2
  - AI分析结果 grid-cols-2 → 1/2
  - 品牌名称/城市 grid-cols-2 → 1/2
  - 套餐选择 grid-cols-3 → 1/3（2处）
  - 报价摘要 grid-cols-4 → 2/4 + 字号缩放
  - 成交信息 grid-cols-2 → 1/2
  - 服务期 grid-cols-2 → 1/2
  - 查看报价指标 grid-cols-4 → 2/4 + 字号/padding缩放
- **状态**: [x] 已完成

#### Task 5.2 — OnlineQuoteFlow.tsx 移动端侧边栏 + grid
- **文件**: `frontend/src/pages/Quote/OnlineQuoteFlow.tsx`（修改，1810行）
- **改动**:
  - 导入 useIsMobile + Menu/X 图标
  - SessionSidebar: w-72 固定侧边栏 → 移动端滑出 drawer + 遮罩层
  - 选择 session 后自动关闭 drawer
  - 移动端空状态: 显示"查看报价记录"按钮替代"选择左侧"文案
  - 顶栏: 移动端加 Menu 按钮，流程说明 truncate
  - 内容区: px-5 → px-3 md:px-5
  - 4处 grid-cols 响应式: 客户选择(2→1/2)、品牌信息(3→1/3)、三档总价(3→1/3)、签约确认(3→1/3)
- **状态**: [x] 已完成

#### Task 5.3 — Monitoring/index.tsx 内容区适配
- **文件**: `frontend/src/pages/Monitoring/index.tsx`（修改，2377行）
- **改动**:
  - 页面外层 p-6 → p-3 md:p-6, space-y-6 → space-y-4 md:space-y-6
  - 标题栏 flex → flex-col sm:flex-row, 字号响应式
  - 操作按钮区 flex-wrap
  - 投放管理 dialog grid-cols-2 → 1/2
  - 趋势统计 3列卡片字号 text-2xl → text-lg md:text-2xl, padding 缩放
- **状态**: [x] 已完成

#### Task 5.4 — BrandList.tsx 移动端适配
- **文件**: `frontend/src/pages/Brands/BrandList.tsx`（修改，628行）
- **改动**:
  - 页头标题栏: flex → flex-col sm:flex-row, 字号响应式, 操作按钮 flex-wrap
  - 搜索栏: flex → flex-col sm:flex-row
  - 品牌卡片: flex → flex-col sm:flex-row, 头像缩放, 名称 truncate
  - 操作按钮区: flex-wrap + gap 缩小
- **状态**: [x] 已完成

#### Task 5.5 — DiagnosisReport.tsx 检查
- **文件**: `frontend/src/pages/Diagnosis/DiagnosisReport.tsx`（310行）
- **现状**: 纯 prose 内容渲染，天然响应式，无需改动
- **状态**: [x] 已完成（无需修改）

#### Task 5.6 — 构建部署
- `cd frontend && npx vite build`（成功）
- `docker cp dist/. omnirank-ai:/app/frontend/dist/ && docker exec omnirank-ai nginx -s reload`
- **状态**: [x] 已完成

---

## 文件清单总览

| # | 文件路径 | 操作 | Phase | 状态 |
|---|---------|------|-------|------|
| 1 | `hooks/useIsMobile.ts` | 新建 | 1.1 | [x] |
| 2 | `components/layout/MobileTabBar.tsx` | 新建 | 1.2 | [x] |
| 3 | `components/layout/Layout.tsx` | 修改 | 1.3 | [x] |
| 4 | `components/layout/ClientContextBar.tsx` | 修改 | 1.4 | [x] |
| 5 | `pages/History/HistoryList.tsx` | 修改 | 2.1 | [x] |
| 6 | `pages/Admin/UserManagement.tsx` | 修改 | 2.2 | [x] |
| 7 | `pages/Admin/AuditLogs.tsx` | 修改 | 2.3 | [x] |
| 8 | `pages/Dashboard.tsx` | 修改 | 3.1 | [x] |
| 9 | `pages/Monitoring/index.tsx` | 修改 | 3.2+5.3 | [x] |
| 10 | `pages/Diagnosis/ClientMaterialsEditor.tsx` | 修改 | 3.3 | [x] |
| 11 | `pages/Diagnosis/ExportReportModal.tsx` | 修改 | 3.4 | [x] |
| 12 | `pages/Employees/AdvisorTeam.tsx` | 修改 | 3.5 | [x] |
| 13 | `pages/Brands/BrandList.tsx` | 修改 | 3.6+5.4 | [x] |
| 14 | `pages/Quote/QuoteCenter.tsx` | 修改 | 5.1 | [x] |
| 15 | `pages/Quote/OnlineQuoteFlow.tsx` | 修改 | 5.2 | [x] |
| 16 | `pages/Diagnosis/DiagnosisReport.tsx` | 检查 | 5.5 | [x] |

## 设计规范

- 断点: `lg:` (1024px) 区分桌面/移动
- 底部导航高度: `h-16` (64px)，内容区预留 `pb-20 lg:pb-0`
- 触控区最小: 44px × 44px
- 卡片间距: `space-y-3`
- 移动端内边距: `p-3 sm:p-4`
- 卡片圆角: `rounded-xl`
- 品牌色/设计 token: 沿用 CLAUDE.md 中定义的规范
