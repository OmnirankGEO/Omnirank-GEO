# 前端优化清单

> 生成日期: 2026-02-27
> 项目: GEO Diagnosis System Frontend
> 技术栈: React 18 + TypeScript + Vite + Tailwind CSS + Radix UI
> **最后修复日期: 2026-02-28**

---

## 一、功能缺失 / TODO 未实现（优先级：🔴 高）

### ✅ 1.1 会议室 - 继续执行 API 未对接
- **文件:** `src/pages/Employees/MeetingRoom.tsx`
- **状态:** ✅ **审核发现：代码第672行已实现 `authFetch("/api/meetings/continue")` 完整流式SSE对接，清单描述有误**

### ✅ 1.2 客户门户 - 社媒帖子使用硬编码假数据
- **文件:** `src/pages/Portal/PortalDashboard.tsx`
- **状态:** ✅ 已修复 — mock 数据清除为空数组 + TODO 注释

### ✅ 1.3 数据洞察 - brandId 硬编码为 1
- **文件:** `src/pages/Insights/InsightsCenter.tsx`
- **状态:** ✅ 已修复 — 改为从 `ClientContext` 动态获取

### ✅ 1.4 内容工坊 - project_id 硬编码为 1
- **文件:** `src/pages/SocialOperator/ContentWorkshop.tsx`
- **状态:** ✅ 已修复 — 2处改为从 `localStorage.getItem('currentProjectId')` 动态获取

### 1.5 DeepSeek 功能受限提示
- **文件:** `src/pages/Employees/AdvisorTeam.tsx`
- **状态:** ⏳ 未修复（需确认 DeepSeek 是否已支持 Web 搜索）

---

## 二、调试代码残留（优先级：🟡 中）

### ✅ 2.1 console.log 未清理（共 42 处）
- **状态:** ✅ **全部清除** — 42 → 0，覆盖11个文件

---

## 三、TypeScript 类型安全（优先级：🟡 中）

### ✅ 3.1 `any` 类型滥用（核心文件已修复）

**已修复：**
- `lib/api.ts` — 5处 `any` → 明确类型
- `MeetingRoom.tsx` — `StreamEvent.data: any` → `Record<string,unknown>` + EventData 断言（20+处）
- `MeetingRoom.tsx` — `presets: any[]` / `meetingInfo: any` → 明确接口

**未修复（低风险）：** ~95处分布在28个文件的 `.map()` 回调中，为动态JSON遍历，需逐个定义接口

---

## 四、代码风格不一致（优先级：🟢 低）

### 4.1 大量内联样式未使用 Tailwind（100+ 处）
- **状态:** ⏳ 未修复 — 社媒模块特性，工作量大，建议逐步迁移

### ✅ 4.2 会议室硬编码公司信息
- **文件:** `src/pages/Employees/MeetingRoom.tsx`
- **状态:** ✅ 已修复 — 改为环境变量 `VITE_COMPANY_NAME`

---

## 五、上下文/状态管理（优先级：🟢 低）

### ✅ 5.1 SocialContext 空函数桩
- **状态:** ✅ **确认为正常 React Context fallback 模式，无需修改**

### ✅ 5.2 监控页面硬编码有效期
- **文件:** `src/pages/Monitoring/index.tsx`
- **状态:** ✅ 已修复 — 改为环境变量 `VITE_TOKEN_DAYS_VALID`

---

## 六、修复状态总览

| 优先级 | 编号 | 问题 | 状态 |
|--------|------|------|------|
| 🔴 P0 | 1.3 | InsightsCenter brandId 硬编码 | ✅ 已修复 |
| 🔴 P0 | 1.4 | ContentWorkshop project_id 硬编码 | ✅ 已修复 |
| 🔴 P0 | 1.2 | PortalDashboard 使用 mock 数据 | ✅ 已修复 |
| 🔴 P1 | 1.1 | MeetingRoom continue API | ✅ 已对接（清单有误） |
| 🟡 P2 | 2.1 | 42 处 console.log | ✅ 全部清除 |
| 🟡 P2 | 3.1 | any 类型（核心文件） | ✅ 28处修复 |
| 🟡 P3 | 1.5 | DeepSeek 功能提示 | ⏳ 待确认 |
| 🟢 P4 | 4.1 | 100+ 处内联样式 | ⏳ 建议逐步迁移 |
| 🟢 P4 | 4.2 | 公司信息硬编码 | ✅ 环境变量 |
| 🟢 P5 | 5.1 | Context 空函数 | ✅ 正常模式 |
| 🟢 P5 | 5.2 | 监控有效期硬编码 | ✅ 环境变量 |
