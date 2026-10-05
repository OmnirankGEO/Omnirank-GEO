# EXIT · 可搜索下拉统一组件与第一批接入

> 工单 `docs/AI-CONTEXT/WORKORDER_SEARCHABLE_SELECT_2026-07-30.md`
> 清单 `docs/AI-CONTEXT/SEARCHABLE_SELECT_INVENTORY_2026-07-30.md`（§1 交付物 · Review 已过目并裁决 5 项）
> 基线 = 生产尖 `80132d5f225b6e6e585529536ed90500dbc02c7f`（实测容器 label + `/app/.release_sha` 双证）
> 分支 `feat/searchable-select-2026-07-30` @ worktree `C:\AI-Test\_searchsel_20260730`
> 状态 = **coded**（未部署 · 纯前端 · 无迁移 · 无后端改动 · 无新依赖）

---

## 一、交付内容

**T1 · 两层组件** `frontend/src/components/ui/searchable-select.tsx`

| 层 | 职责 |
|---|---|
| `SearchableRoot` | 数据 + 搜索词 + 过滤 + 关闭清空（唯一实现点）+ `inline` 模式开关 |
| `SearchableSearchBox` / `SearchableItems` | 搜索框（自动聚焦 + 清空按钮 + Esc）/ 过滤列表（含空态）· 可分开摆放 |
| `SearchableListPanel` | 上面两段的组合件（简单场景用） |
| `SearchableSelect` | Root + trigger + 弹层 + 单值语义（新接入点用） |

底座 = `@base-ui/react/combobox`（`package.json:27` 既有依赖 · lockfile 锁 1.3.0 · 已被 6 个 ui 基元使用）。
**没有新增任何依赖**，不违反"不引入新 UI 框架"。

阈值 `SEARCHABLE_MIN_OPTIONS = 9`：≤8 不渲染搜索框，`searchable` 显式传参可双向覆盖。

**T2 · 第一批接入 6 处**（Review 裁决后的名单）

| 位置 | 下拉 | 选项数（生产实测） |
|---|---|---|
| `Employees/MeetingRoom:815` | 选择客户/品牌 | 100（limit=100 · 库内 321） |
| `Writing/WritingCenter:264` | 选择诊断记录 | ≤50（库内 306） |
| `Admin/UserManagement:1650` | 新的直属上游/商业承接方 | 41 |
| `Admin/ResearchMonitor/IndustriesPromptsPanel:1099` | 行业改判 | 19 |
| `Wallet/BankCardsPage:405` | 开户行 | 15（Review 提进第一批） |
| `Monitoring/components/MonitoringSettingsDialog:170` | 首次执行时间 | 24（**阈值例外**，理由见 §四） |

**ClientSwitcherSidebar** 只换"搜索段"（输入框 + 过滤 + 列表），业务段（状态 pills / 测试客户开关 / 全部客户行 / 新建客户 / 尾栏）原地不动。

---

## 二、7 条锁

| 锁 | 判据 | 结果 |
|---|---|---|
| 1 过滤与恢复 | 30 项 → `Item-17` → 1 条；清空 → 30 条 | 🟢 |
| 2 自动聚焦 | **真渲染 `document.activeElement === 搜索框`**（Select 模式 / inline 模式 / Dialog 内 三处各取一次） | 🟢 |
| 3 关闭重置 | 四条关闭路径（trigger 切换 / 选中 / Esc / 外部点击）各一条，重开均 `query=''` + 列表完整 | 🟢 |
| 4 键盘 | ↑↓ 高亮且 `aria-activedescendant` 与高亮项 id 相等；Enter 选中并关闭；Esc 关闭 | 🟢 |
| 5 ClientSwitcher 零回归 | 逐项对照改前基线 **19 项一致**（明细 `LOCK5_clientswitcher_after_vs_before.json`） | 🟢 |
| 6 阈值分流 | 6 项 → 可见输入框 0；9 项 → 1；30 项+`searchable=false` → 0；6 项+`searchable=true` → 1 | 🟢 |
| 7 aria 不退化 | 改前 input 上零 aria；改后 `role=combobox` + `aria-expanded/controls/autocomplete` + `listbox/option` + `aria-activedescendant` | 🟢 改进 |

🔴 **锁 6 的判据必须用「可见输入框」不能用「DOM 里有 input」**：base-ui 每个 `Combobox.Root` 都会渲染一个不可见 `<input type="text">`（表单值载体），6 个实例的 harness 里 DOM 恒有 6 个 input 标签 —— 即使一个搜索框都没渲染。判据 = `offsetParent !== null`。这与工单 §4 点的 `ui/progress.tsx` `aria-valuenow` 恒 0 是同一类陷阱。

---

## 三、5 个变异 + Review 追加的结构性验证

| 变异 | 结果 |
|---|---|
| ③ 大小写敏感 | 🔴 RED（harness 10→0；ClientSwitcher 1→0） |
| ④ 去掉阈值 | 🔴 RED（6 项下拉冒出搜索框 0→1） |
| ⑤ 破坏状态筛选 | 🔴 RED（选「未定」应 0 条、实得 201） |
| ① 去掉 autofocus | **分模式**：inline 模式 RED；Select 模式 SURVIVED |
| ② 关闭不重置 | **复合变异才 RED**：单删 effect / 单掐库回调都 SURVIVED，两条同时掐才 RED |
| 追加 · 清空收敛结构性验证 | 共用层两条机制全掐后 ClientSwitcher 仍干净 → 清空由**面板卸载**保证 |

三条要向 Review 明说的事：

1. **① 在 Select 模式转不红**，因为自动聚焦是 base-ui `Popup` 默认 `initialFocus`（聚焦首个可 tab 元素）提供的，我的代码删不掉。该模式下锁 2 是**对库行为的契约校验**（库升级才会红），不是对我代码的校验。inline 模式的自动聚焦确实由我的 effect 承担，变异转红。
2. **② 单点变异 SURVIVED ≠ 锁假绿**：清空由两道独立守卫兜（共用层 effect + 受控 `inputValue` 回写），复合变异才证明锁有牙。
3. **④ 的夹具选择有讲究**：用「跟进」做状态筛选夹具毫无判别力（生产 206/共 243，改前改后都是 201）。必须用「未定 0 条」或「归档 37 条」。

**清空收敛核对**：`ClientSwitcherSidebar.tsx` 内 `setSearch` 调用 **6 → 0**；唯一清空实现点在 `SearchableRoot`（另有一处是用户点清空按钮，属显式操作）。ClientSwitcher 的关闭清空实际有三层保障：**面板卸载（`{open && ...}`）> 共用层 effect > 受控 inputValue 回写**。所以 Review 担心的"将来加第 5 条关闭路径漏清空"在该组件里**结构上不可表达** —— 任何关闭路径都会卸载面板。

🔴 **残留风险已写进代码注释**（Review 要求 · commit `b8ad2e6b`）：第一层依赖"条件渲染 = 卸载"。
若哪天为了做动画把面板改成常驻渲染 + CSS 隐藏（`keepMounted` / `display:none` / `opacity-0` 任何不卸载的写法），
第一层立即失效，清空只剩 ②③ 兜底 —— 那时必须重新跑四条关闭路径验整条清空链，不要假定还成立。
注释就写在 `SearchableRoot` 的清空 effect 上方，改那段代码的人必看到。

---

## 四、改造过程中真渲染抓到的 3 个真 bug（纯 DOM 断言会全部放过）

1. **Dialog 内搜索框拿不到焦点** —— `UserManagement`「调整直属上游」弹窗里搜索框存在但 `activeElement` 是 trigger。根因：`Combobox.Portal` 默认投 `document.body`，焦点离开 Radix Dialog 子树 → dialog focus trap 立刻拽回。修法：Portal `container` 指向最近 `[role=dialog]`。
   **坑中坑**：`container` 传 `null` 会被 FloatingPortal 当成"无容器" → 弹层整个不渲染（非 Dialog 场景直接打不开），必须传 `undefined`。
2. **点状态 pill / 测试开关 → 面板整个关闭** —— 面板是 ClientSwitcher 自己的绝对定位 div，不是 base-ui Popup，combobox 的 dismiss 把面板内点击当 `outsidePress`。修法：`Combobox.Root` 传 `inline`（官方语义 = 列表内联渲染不走 popup）。
3. **Esc 关不掉面板** —— 改前 Esc 挂在原生 input 的 `onKeyDown`；换成 `Combobox.Input` 后，inline 模式下 base-ui 不接管 open。修法：Esc→关闭收进共用层（`context.requestClose`），所有消费方统一，不各自写。

---

## 五、构建

```
npm run build → EXIT=0
  build 脚本 = tsc -b && vite build && 9 个项目自带 gate 脚本
  → tsc 零错误（含在 build 里）· 9 个 gate 全过
产物 dist/assets/*.js 合计 8.4M
harness 未进产物：dist 内无 *harness* 文件，无 SearchableSelectHarness 代码
组件已进产物：dist/assets/index-DNPxI3ge.js 含搜索框 placeholder
```

### 体积基线对照（Review 放行条件 · 已补实测）

同一 worktree 同一 `node_modules`，`git checkout --detach 80132d5f` 构建一次（BEFORE），
切回本分支再构建一次（AFTER），两次都 `npm run build` EXIT=0。gzip 用 level 9。

```
ENTRY chunk  gzip : 103.7KB → 142.9KB    delta +39.2KB   ← 判据看这行
ENTRY chunk  raw  : 325.2KB → 439.0KB    delta +113.9KB
全部 JS 合计 gzip : 2358.7KB → 2376.8KB  delta +18.1KB
产物 JS 文件数    : 244 → 243
```

**按 Review 判据：+39.2KB 落在 20~50KB 区间 → 放行，EXIT 记明。**

逐 chunk 明细（gzip 变化 ≥0.5KB 的全部三项）：

```
  +35.9KB  index（entry 与其同组的公共 chunk）   763.1 → 799.0
  -15.8KB  sheet                                 20.8 →   5.0
   -1.8KB  useOpenChangeComplete                  1.8 →   0.0  [消失]
```

**关键解读：entry 的 +39.2KB 里有约 17.6KB 是"搬家"不是"新增"。**
`sheet`（-15.8KB）和 `useOpenChangeComplete`（-1.8KB）这两个 chunk 缩小/消失，
是 base-ui 的公共内核（floating-ui / popup 机制）从懒加载 chunk 被提到了 entry。
真正新增的代码量看**全站合计 +18.1KB gzip**。

**为什么会提到 entry**：`searchable-select.tsx` 同时被
`ClientSwitcherSidebar`（在 Layout 里，首屏就加载）和几个懒加载页面引用 →
Rollup 判定为共享模块，放进 entry/公共 chunk。

**如果 Review 想把这 ~39KB 挪出首屏**（本单未做，等指示）：
把 ClientSwitcher 的面板整段（`{open && <SearchableRoot>…}`）改成 `React.lazy`，
combobox 就只在"第一次点开客户切换器"时才拉。
代价 = 首次点开有一次约 40KB 的取包，可能出现一帧空面板；
而客户切换器是登录后立刻会用的高频控件，这个 flicker 是否可接受要你定。

---

## 六、⚠️ 三项必须让 Review 知道的偏差

1. 🔴 **站点 6（`MonitoringSettingsDialog:170` 24 项时间轮盘）改在死代码上 —— 验收时找不到这个功能是正常的，不是包没做完。**
   `grep` 全仓 `MonitoringSettingsDialog` 只命中自身定义，无 index 再导出、无动态 import；线上真用的是 `ScheduleDialog`（`Monitoring/index.tsx:51` lazy import），而 `ScheduleDialog` 里**没有任何小时选择器**。全站唯一的 24 项时间选择器就在这个死组件里。
   **Review 裁定（2026-07-30）= 保持现状**：不复活（等于在 UI 优化包里顺手上一个线上没有的功能，超范围）、不删（删死代码是独立清理动作，混进来扩大回滚面）；改动无害，哪天复活那个 Dialog 搜索能力已经在了。
   → 这也修正我清单 §H 的一处：那一行应标注为死代码，与 `ScheduleDialog` 的死 import 同类。

   **附：Owner 顺带问的"定时监测是不是选不了具体小时" —— 查了，不是缺口，是主动砍的。**
   `ScheduleDialog.tsx:94-95` 原注释写明：*"老 频率 select / 起始小时 select / 间隔 input 全砍 · 改为只读提示卡 · UI 跟实际扣费模型一致"*，
   出处 CTO-15.23 2026-05-09，理由是扣费按 daily 模型算（`kwCount × unitPrice`，频率不影响），
   留着那三个控件会误导代理"可自定义频率"。
   字段本身没废：`api/scheduler.py:813-825` 仍读 `monitoring_start_hour`（默认 8），
   并按 `brand_id` hash 把执行摊到 `[start_hour, start_hour+5]` 窗口错峰（历史上出过"大量客户 start_hour=3 集中触发"的事故）。
   生产实证（`quotes` 表）：**已开启监测的报价 7 条，非默认小时(≠8) 的 0 条** —— 砍掉这个 UI 实际零代价。
   那个死组件正是被 `ScheduleDialog` 替代掉的旧版弹窗，这解释了它为什么成死代码。

2. **锁 2 在 Select 模式无法由"改我的代码"转红**（见 §三-1）。我没有把它算作"变异全转红"，而是标成契约校验。

3. **清单 §V 我写「4 处 setSearch」漏了 Esc 那处，实为 6 处**（4 条关闭路径 + 1 处打开时清空 + 1 处清空按钮）。收敛按 6 处算，锁按 Review 点的 4 条关闭路径配。

---

## 六之二、两条取证口径已入 SSOT

Review 要求，已写进 `docs/AI-CONTEXT/REVIEW_CTO_METHODOLOGY_SSOT.md`：

- **§5.1 前端"真渲染"取证口径** —— "DOM 里有那个标签"≠"那个东西在工作"。
  与 `ui/progress.tsx` `aria-valuenow` 恒 0 并列成表；判据规则：数控件用可见性
  `offsetParent !== null`、判焦点只认 `document.activeElement`、判状态量渲染出来的属性值。
- **§7.1 取样点选错 = 锁什么也不验** —— 与 108/108 单边、138/200 可渲染并列为同族第 3 例；
  规则 = 取与全量差异最大的那一档，并自问"这段逻辑整段删掉，这个数字会变吗"。
- 顺带补了 **§7.2 单点变异 SURVIVED ≠ 锁假绿**（要分清"锁没牙"还是"保障冗余"）
  和 **§7.3 库提供的行为用改自己代码的变异转不红**（那是契约校验不是判别锁）。

---

## 七、未做 / 不在本单

- 第二批 16 个 9~20 选项的下拉（清单 §D）—— 按工单"低优先，可后置"。
- 原生 `<select>` 那 49 个文件 —— §5 明确排除。
- `prod-current` tag 落后一次切流（仍指 `e4c06a14`，实际生产尖 `80132d5f`）—— 部署侧事项，已在清单 §Z 记录。

## 八、取证文件

```
qa/searchable-select-2026-07-30/
  BASELINE_clientswitcher_before.json          改前基线（锁5 对照组）
  LOCKS_component_level.json                   组件级 6 条锁
  LOCK5_clientswitcher_after_vs_before.json     锁5 逐项对照 19 项
  MUTATIONS_and_SITES.json                      5 变异 + 结构性验证 + 6 站点真页面取证
  EXIT_REPORT.md                                本文件
```

取证环境：本地 vite dev（127.0.0.1:5173）+ 生产后端（`https://omnirank.top`），账号 `qa_admin_2026` / `qa_agent_2026`（常设 UI 测试授权）。全程**零写库**：`UserManagement` 只开 draft 未保存、行业改判只输入未选中、绑卡表单只开未提交；唯一状态变更是 QA 账号自己的当前客户上下文（已还原为"全部客户"）与新手引导偏好。
