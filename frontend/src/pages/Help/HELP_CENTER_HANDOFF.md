# 帮助中心 · 交接文档

> 创建时间: 2026-05-18 · 最后更新: 2026-05-18 (Phase 1 / 2 / 3 全部交付)
> Phase 1 用户侧 + Phase 2 后端 + Phase 3 admin 全部已实现, 见下方"状态总览"

---

## 1. 状态总览

| 阶段 | 模块 | 状态 |
| --- | --- | --- |
| Phase 1 | 用户侧 `/help` 主页 + 文档 + FAQ | ✅ 已完成 |
| Phase 2 | 后端 3 张表 + 10 个 API | ✅ 已完成 |
| Phase 2.2 | 用户侧 FAQ 切真后端 + 离线降级队列 | ✅ 已完成 |
| Phase 3 | 管理员 `/admin/help-center` 双 tab | ✅ 已完成 |

### 已实现(用户侧)

- `/help` 主页 · 视频教程(4 步学习路径 + 概念科普)
- `/help/docs` + `/help/docs/:slug` · 文档子页(左目录树 + 右 markdown 内容 · 16/18 节点"施工中"占位 · `first-diagnosis` 节点填了示例)
- `/help/faq` · FAQ 子页 · 12 条 seed 问题 · up/down 真投票 · 页面底部反馈走真后端

### 已实现(后端)

- `db/faq_db.py` · 3 张表 + 索引 · 启动幂等建表 + 首次 seed 12 条
- `api/faq_api.py` · 用户侧 3 端点 + 管理员侧 7 端点
- `server.py` 注册 `faq_router` 并调用 `init_faq_tables()`

### 已实现(管理员)

- `/admin/help-center` 双 tab 页面
- Tab 1: FAQ 内容(新建/编辑/删除/差答案高亮)
- Tab 2: 用户反馈收件箱(状态机 + 跨 tab 跳 FAQ 编辑)

### 已知局限(等内容/视频拍完)

- 11 个视频文件还没拍 · 点击 toast "制作中"
- 文档 17/18 节点正文还没写 · 只有 `first-diagnosis` 是示例
- FAQ 12 条 seed 答案都是空字符串(管理员可在 `/admin/help-center` 里写)
- 截图全部灰底占位 · 等运营提供

---

## 2. Phase 2 后端 · 已实现细节

### 2.1 新建 3 张表

```sql
-- FAQ 条目主表
CREATE TABLE faq_items (
  id              bigserial PRIMARY KEY,
  question        text NOT NULL,
  answer_md       text NOT NULL DEFAULT '',
  category        text NOT NULL CHECK (category IN ('billing','operation','data','account')),
  sort_order      int NOT NULL DEFAULT 0,
  is_published    bool NOT NULL DEFAULT true,
  thumbs_up       int NOT NULL DEFAULT 0,  -- 冗余 · 触发器维护
  thumbs_down     int NOT NULL DEFAULT 0,  -- 冗余 · 触发器维护
  feedback_count  int NOT NULL DEFAULT 0,  -- 冗余 · 关联未处理反馈数
  created_at      timestamptz NOT NULL DEFAULT NOW(),
  updated_at      timestamptz NOT NULL DEFAULT NOW(),
  updated_by      int REFERENCES users(id)
);
CREATE INDEX faq_items_category_sort ON faq_items (category, sort_order);
CREATE INDEX faq_items_published ON faq_items (is_published) WHERE is_published = true;

-- 投票记录 · 一人一票, 可切换可撤销
CREATE TABLE faq_votes (
  faq_id      int NOT NULL REFERENCES faq_items(id) ON DELETE CASCADE,
  user_id     int NOT NULL REFERENCES users(id),
  vote        text NOT NULL CHECK (vote IN ('up','down')),
  created_at  timestamptz NOT NULL DEFAULT NOW(),
  PRIMARY KEY (faq_id, user_id)
);
-- 触发器: insert/update/delete 时实时更新 faq_items.thumbs_up/down 冗余字段

-- 用户反馈
CREATE TABLE faq_feedback (
  id            bigserial PRIMARY KEY,
  client_id     text NOT NULL,  -- 前端生成的 uuid · 用来去重
  faq_id        int NULL REFERENCES faq_items(id),  -- 可空 · 通用反馈不关联具体 FAQ
  message       text NOT NULL,
  urgency       text NOT NULL CHECK (urgency IN ('low','mid','high')) DEFAULT 'low',
  contact       text NOT NULL DEFAULT '',
  user_id       int NOT NULL REFERENCES users(id),
  status        text NOT NULL CHECK (status IN ('pending','read','done','closed')) DEFAULT 'pending',
  admin_note    text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT NOW(),
  handled_at    timestamptz NULL,
  handled_by    int NULL REFERENCES users(id),
  UNIQUE (client_id)  -- 防离线重发同一条
);
CREATE INDEX faq_feedback_status_created ON faq_feedback (status, created_at DESC);
CREATE INDEX faq_feedback_faq_id ON faq_feedback (faq_id) WHERE faq_id IS NOT NULL;
```

### 2.2 用户侧接口(6 个)

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/api/faq/items` | 拿已上架的 FAQ 列表 · 带 `thumbs_up/down` · 用户自己的 `my_vote` 字段 |
| POST | `/api/faq/vote` | body: `{ faq_id, vote }` (vote 传 null 撤销) · 返回最新计数 |
| POST | `/api/faq/feedback` | body: 见下方 schema · 返回 `{ ok, feedback_id }` |
| GET  | `/api/admin/faq/items` | 管理员看全部(含未上架) |
| POST/PATCH/DELETE `/api/admin/faq/items[/id]` | CRUD |
| GET/PATCH `/api/admin/faq/feedback[/id]` | 反馈列表 / 改状态 |

**POST /api/faq/feedback 请求体**:
```json
{
  "client_id": "前端 uuid",
  "faq_id": 123,
  "message": "用户写的内容(≤500字)",
  "urgency": "low | mid | high",
  "contact": "可选 · 用户填的手机/微信",
  "client_created_at": "2026-05-18T03:00:00Z"
}
```
后端从 JWT 取 `user_id`, 前端**不传**。

### 2.3 限流 + 重复检测
- 同 `user_id` 每 10 分钟最多 5 条反馈
- 用 `client_id` 唯一索引去重(用户离线-重发场景)
- 高紧急(`urgency=high`)实时推 Slack/钉钉

### 2.4 前端接入改动(等后端做完)

文件: `src/pages/Help/faq-feedback.ts`

```ts
// 投票 setVote 改造
export async function setVote(faqId, vote) {
  const res = await fetch('/api/faq/vote', { ... })
  if (res.ok) { /* 写本地 mirror */ }
  else { /* 还按本地 fallback */ }
}

// 反馈 appendFeedback 改造
export async function appendFeedback(data) {
  try {
    const res = await fetch('/api/faq/feedback', { ... })
    if (res.ok) return { ...entry, status: 'synced' }
  } catch {}
  // 失败落本地队列, drainQueue 重发
  return persistLocal(entry)
}

// 新增 · App 启动 drain
export async function drainQueue() {
  const queue = loadQueue().filter(f => f.status === 'pending')
  for (const f of queue) {
    await fetch('/api/faq/feedback', { ... })
    // 限速 1 条/秒 + 失败不删
  }
}
```

`drainQueue()` 在 `App.tsx` mount 调一次, 或者 HelpFAQ 页面 mount 调。

文件: `src/pages/Help/faq-data.ts`
- 删 `mockUp/mockDown` 字段
- `faqItems` 从 `GET /api/faq/items` 拉, 改成 React Query / SWR / 自己 useEffect

文件: `src/pages/Help/HelpFAQ.tsx` + `HelpDocs.tsx`
- 把硬编码的 `faqItems` 改成接口数据

---

## 3. Phase 3 管理员侧 · 已实现细节

### 3.1 入口

- 路由: `/admin/help-center` · 用 `requiredModule="settings"` (同审计/订单)
- AppSidebar 加: `{ to: '/admin/help-center', icon: HelpCircle, label: '帮助中心管理', adminOnly: true }`

### 3.2 单页双 Tab 结构

```
/admin/help-center
├─ Tab 1 · FAQ 内容(默认)
│   ├─ 列表: 排序/分类/问题/up/down/反馈数/操作
│   ├─ 筛选: 分类 + 上架状态
│   ├─ + 新建 按钮
│   └─ 行 ✏ → 编辑抽屉(或编辑页)
│
└─ Tab 2 · 用户反馈收件箱
    ├─ 状态 chip: 待处理 ●3 / 已读 / 已处理 / 关闭
    ├─ 紧急度 + 分类筛选
    └─ 单条: 内容 / 关联 FAQ / 用户 / 操作(标记已读 / 改 FAQ / 关闭)
```

### 3.3 反馈状态流转

```
pending(待处理) → read(已读) → done(已处理) → closed(关闭)
                     ↓                      ↓
                  也可直接                  done 后 可重开
                  done                      回 pending
```

四态语义:
- `pending` · 新提交,管理员还没看
- `read` · 看过了,但还没改 FAQ(可能在排期)
- `done` · 改完 FAQ 了
- `closed` · 不会处理(过期 / 重复 / 不合理)

### 3.4 FAQ 编辑器(markdown · 老板选 Option A)

**纯 textarea + 右侧实时预览**(零新依赖, 复用现有 react-markdown)

```tsx
<div className="grid grid-cols-2 gap-4">
  <Textarea value={md} onChange={...} rows={20} />
  <div className="prose prose-sm dark:prose-invert">
    <ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown>
  </div>
</div>
```

可选优化(非必须):
- 工具栏按钮: 加粗/列表/标题/链接(往 textarea 插字符)
- Tab 键缩进
- 截图粘贴上传(走后端 OSS)

### 3.5 跨 Tab 联动

反馈条目里"改 FAQ"按钮 → 切到 Tab 1 + 自动打开对应 FAQ 编辑抽屉:
```tsx
// state: { activeTab, editingFaqId }
const goEditFaq = (faqId: number) => {
  setActiveTab('items')
  setEditingFaqId(faqId)
}
```

---

## 4. 文件清单

### Phase 1 已建/已改

| 文件 | 状态 | 说明 |
|---|---|---|
| `src/components/help/videos-data.ts` | 新建 | 视频清单单一来源 |
| `src/components/help/VideoCard.tsx` | 新建 | 视频缩略图 + 卡片 |
| `src/pages/Help/HelpCenter.tsx` | 重写 | 学院风主页 |
| `src/pages/Help/HelpDocs.tsx` | 新建 | 文档子页 |
| `src/pages/Help/HelpFAQ.tsx` | 新建 | FAQ 子页 |
| `src/pages/Help/docs-data.ts` | 新建 | 文档树 |
| `src/pages/Help/faq-data.ts` | 新建 | FAQ 数据 |
| `src/pages/Help/faq-feedback.ts` | 新建 | 反馈 + 投票本地存储 |
| `src/pages/Help/FAQFeedbackDialog.tsx` | 新建 | 反馈表单 Dialog |
| `src/pages/M3/HelpPage.tsx` | 改 | 一行 re-export 老 bookmark 兼容 |
| `src/App.tsx` | 改 | +3 lazy import +4 route |
| `src/components/m3/copilot/M3RouteIsolation.tsx` | 改 | 删 `/help → /m3/help` 映射 |

### Phase 2/3 待建

| 文件 | 谁建 | 说明 |
|---|---|---|
| 后端 migration `XXXX_add_faq_tables.sql` | 后端 | 3 张表 |
| 后端 router/controller `faq.py` | 后端 | 6 个接口 |
| `src/pages/Admin/HelpCenterAdmin.tsx` | admin 团队 | 双 tab 页面 |
| `src/pages/Admin/components/FAQEditor.tsx` | admin 团队 | markdown 编辑器 |
| `src/pages/Admin/components/FAQFeedbackList.tsx` | admin 团队 | 反馈收件箱 |

---

## 5. 关键决策记录

| 议题 | 决策 | 理由 |
|---|---|---|
| 帮助中心入口 | `/help`(从 `/m3/help` 收回) | Layer 3 设计为单一帮助中心,不该 M3/非M3 分裂 |
| 主页结构 | 方案 A · 学院风(非 3-tab) | 强引导, 跟代理 4 步工作流同构 |
| 文档结构 | 双栏 wiki + markdown · Stripe/Vercel 风 | 图文混排, 配真实截图 + 视频引用 |
| FAQ 反馈方式 | 页面级"提反馈"按钮(非每条反馈) | 简洁, 管理员收件箱集中处理 |
| 投票去重 | 一人一票, 可切换可撤销 | 后端 UNIQUE(faq_id, user_id) |
| markdown 编辑器 | Option A · textarea + 实时预览 | 零新依赖, 复用 react-markdown |
| 反馈状态 | 四态 pending → read → done → closed | 区分"看过没改"vs"改完" |
| 管理员回复用户 | Phase 1 不做 | 等系统消息中心支持 |
| 截图占位 | 灰底虚线框 + "截图位 · 待运营提供" | 给运营留补图缺口 |

---

## 6. 已知技术债 / 后续

- 视频文件未拍 · 拍完替换 `videos-data.ts` 里的 `id` 加 `videoUrl` 字段, VideoThumb 改成真 `<video>` 播放
- 文档大部分节点"施工中" · 内容补完后填 `docs-data.ts` 的 `body` 字段
- FAQ 答案全部"暂未收录" · Phase 2 接通后从后端拿, 现 `faq-data.ts` 整个 hardcode 列表可删
- 反馈 localStorage 没做 cross-tab sync (不同 tab 同时投票可能冲突 · 概率低 · 可接受)
- 反馈 drain 失败重试目前没有指数退避 · Phase 2 加
- "重走新手教程"卡片占位在 `HelpCenter.tsx` 顶部 `<div data-slot="onboarding-restart-placeholder" />` · 沙盒会话 merge 时填
