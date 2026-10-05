# 系统知识库格式契约

本文件是系统知识库（Agent-facing System KB）的格式规范，供采集脚本、diff 工具、reindex 解析器对齐。共四个格式：采集产物、页面卡、diff 输出、final 目录。

---

## 1. 采集产物

文件名规则：`agent_kb_crawl_<crawler>.json`，其中 `crawler` 取值为 `claude` 或 `codex`（字符串，标识哪个代理账号完成采集）。

顶层结构：

```json
{
  "meta": {
    "crawler": "<claude|codex>",
    "account_id": "<代理账号 ID，字符串>",
    "seed_version": "<本次采集使用的路由种子版本号，字符串，如 '0.1.0'>",
    "data_snapshot_id": "<本次采集的唯一快照 ID，字符串>",
    "crawled_at": "<ISO 8601 时间戳，字符串>"
  },
  "pages": [ /* <page_card> 数组，每项遵循 page_card.schema.json */ ],
  "inaccessible": [
    { "route": "<路由路径>", "reason": "<403|跳登录|隐藏|其他说明>" }
  ]
}
```

### meta 字段说明

| 字段 | 含义 |
|------|------|
| `crawler` | 执行采集的代理标识，枚举值 `claude` 或 `codex` |
| `account_id` | 采集时登录的账号 ID，便于复现和权限溯源 |
| `seed_version` | 路由种子列表的版本号，与 manifest.json 的 `version` 对应 |
| `data_snapshot_id` | 本次采集的全局唯一 ID，用于 diff 工具对账 |
| `crawled_at` | 采集完成时间，ISO 8601 格式 |

### pages

数组元素为页面卡对象，结构见第 2 节及 `page_card.schema.json`。

### inaccessible

记录代理无法访问的路由（HTTP 403、被重定向到登录页、前端隐藏不渲染）：

| 字段 | 含义 |
|------|------|
| `route` | 无法访问的路由路径 |
| `reason` | 失败原因，建议填 `403` / `跳登录` / `隐藏` 或具体说明 |

---

## 2. 页面卡（Page Card）

格式定义见 `page_card.schema.json`（JSON Schema draft-07）。

每个页面卡是一个 JSON 对象，以下是各字段含义：

| 字段 | 类型 | 是否必填 | 含义 |
|------|------|----------|------|
| `route` | string | 必填 | 页面路由路径，如 `/pricing` |
| `page_name` | string | 必填 | 页面中文名称 |
| `visible_to` | enum `"agent"` / `"normal_user"` / `"both"` | 必填 | 两套知识库身份路由：`agent`=仅代理 KB，`normal_user`=仅普通用户 KB，`both`=两套都收录。依据=AppSidebar 标志位 + L0 实测侧边栏。（早期版本固定 `agent`，已废弃——现行规则见第 4 节与两套 KB 设计。） |
| `kb` | enum `"agent"` / `"normal_user"` / `"both"` | 可选 | 该卡归入哪套 KB，通常与 `visible_to` 一致。 |
| `is_admin_only` | boolean | 可选，默认 false | 是否仅管理员可见；admin 页不进代理/普通用户两套 KB |
| `normal_user_can_know` | boolean | 可选（字段/按钮级） | 在 `both` 共享页里，标某字段/按钮是否可对普通用户透露；`false`=代理专属（佣金/提现/库存/出厂价/利润/白标/客户归属/报价加价系数等）。等价于 final 页里的 `[仅代理]` 标记。 |
| `purpose` | string | 必填 | 页面用途的一句话描述 |
| `fields` | array | 可选 | 页面上的表单字段列表，每项含 name/label/required/placeholder/meaning/validation |
| `buttons` | array | 可选 | 页面上的操作按钮，每项含 label/action/disabled_when |
| `validation_errors` | array | 可选 | 已知的校验报错，每项含 trigger/message/fix |
| `common_questions` | array | 可选 | 用户常问，每项含 question/answer/normal_user_can_know（标准答案问答对） |
| `step_flow` | array | 可选 | 操作步骤列表（字符串数组，按顺序） |
| `empty_state` | string | 可选 | 无数据时的页面状态描述 |
| `loading_state` | string | 可选 | 加载中状态描述 |
| `success_state` | string | 可选 | 操作成功后状态描述 |
| `failure_state` | string | 可选 | 操作失败后状态描述 |
| `related_routes` | array | 可选 | 相关路由路径列表 |
| `screenshots` | array | 可选 | 截图文件名或路径列表 |

---

## 3. diff 输出

### 3.1 人读版：`agent_kb_crawl_diff.md`

文件由六个小节组成，标题精确如下（下游工具按标题字符串分割，不得变更）：

1. `only_in_claude` — 仅在 claude 采集结果中存在的路由/字段
2. `only_in_codex` — 仅在 codex 采集结果中存在的路由/字段
3. `conflicting_interpretation` — 同一路由的 purpose/功能描述存在实质矛盾
4. `same_route_different_fields` — 同一路由采集到的字段列表不一致
5. `browser_visible_but_static_missing` — 浏览器（代理）采集到但静态代码分析未发现的内容
6. `static_found_but_browser_invisible` — 静态代码分析发现但浏览器采集未见的内容

每个小节内每条记录格式：

```
- route: <路由路径>
  摘要: <差异描述>
  处置建议: <建议操作>
```

### 3.2 机器可读版：`agent_kb_crawl_diff.json`

结构为按上述六个 key 分组的对象，每个 key 对应一个数组：

```json
{
  "only_in_claude": [
    { "route": "<路由>", "summary": "<差异摘要>", "suggestion": "<处置建议>" }
  ],
  "only_in_codex": [ /* 同上 */ ],
  "conflicting_interpretation": [ /* 同上 */ ],
  "same_route_different_fields": [ /* 同上 */ ],
  "browser_visible_but_static_missing": [ /* 同上 */ ],
  "static_found_but_browser_invisible": [ /* 同上 */ ]
}
```

---

## 4. final 目录

### 4.1 文件命名与位置

路径：`knowledge/system_kb/pages/<route-slug>.md`，每个页面一个文件。

### 4.2 route → slug 转换规则

1. 去掉路由开头的第一个 `/`
2. 将其余的 `/` 替换为 `-`
3. 特殊情况：首页 `/` → `home`

示例：

| route | slug |
|-------|------|
| `/pricing` | `pricing` |
| `/agent/inventory` | `agent-inventory` |
| `/admin/users/list` | `admin-users-list` |
| `/` | `home` |

### 4.3 文件结构

每个 `.md` 文件由 YAML frontmatter + 正文构成。

**frontmatter 字段**（合法 YAML，用 `---` 包裹）：

| 字段 | 含义 |
|------|------|
| `route` | 页面路由路径 |
| `page_name` | 页面中文名称 |
| `is_admin_only` | 是否仅管理员可见（boolean） |
| `visible_to` | 两套知识库身份路由：`agent`（仅代理 KB）/ `normal_user`（仅普通用户 KB）/ `both`（两套都进）。缺省 `both`。依据=AppSidebar 标志位 + L0 实测侧边栏。 |

**正文分节**（标题精确，下游解析器按此字符串分割）：

| 标题 | 含义 |
|------|------|
| `## 用途` | 页面功能一句话描述 |
| `## 字段` | 页面表单字段列表 |
| `## 按钮` | 页面操作按钮列表 |
| `## 常见异常` | 已知报错和处置方式 |
| `## 用户常问` | 常见用户问题 + 标准答案（`问题 → 答案`，拆 `sys_qa` chunk，截图问答直答主力） |
| `## 截图问答指引` | 截图出现哪些字段/按钮/状态时，定位到本页哪一步、怎么回答（并入 `sys_page` chunk，可选节） |
| `## 流程` | 操作步骤 |
| `## 状态` | 空态/加载/成功/失败等页面状态（可选节） |

### 4.4 字段行语法（钉死，解析器依赖）

`## 字段` 节下每行格式：

```
- <name>（必填|可选）：<meaning>
```

规则：
- 分隔符使用**全角冒号 `：`**（不是半角冒号 `:`）
- `（必填）` 表示 required=true；无括注或 `（可选）` 表示 required=false
- `<name>` 是括号前的主体字符串，解析器用 `startswith` 在全角冒号前提取

示例：
```
- 客户名称（必填）：报价单上展示的客户名。
- 行业（可选）：影响推荐关键词和默认价格。
```

**`[仅代理]` 字段级内容闸（两套知识库）：**
- 在 `visible_to: both` 的共享页里，若某字段属代理专属（佣金/出厂价/利润/客户归属/报价加价系数等），在该字段行任意位置加标记 `[仅代理]`。
- 解析器据此把这一条 `sys_field` chunk 收紧为 `visible_to=agent`，**只进代理 KB，不进普通用户 KB**；标记本身不写入 chunk 正文。
- `[仅代理]` 闸只作用于 **`## 字段` 与 `## 按钮` 两节**（解析器分别产出 `sys_field` / `sys_button` chunk 并按标记收紧到 `agent`）。
- 共享页的 `## 用途 / 流程 / 用户常问 / 常见异常` 是**页级内容**（随页面 `visible_to`，both 页普通用户也可见），**不支持 `[仅代理]` 行内闸**。因此**这些页级分节只写普通用户可安全获知的内容，任何代理专属事实必须落到带 `[仅代理]` 的字段行或按钮行**——不要把 `[仅代理]` 放进用途/流程/用户常问/常见异常节（放了也不生效，反而泄漏）。

### 4.4b 按钮行语法（钉死，解析器依赖）

`## 按钮` 节下每行格式：

```
- <name>：<点击后果 / 何时点击 / 禁用或报错原因>
```

规则：
- 分隔符使用**全角冒号 `：`**；冒号前是按钮名，冒号后是含义（点了会怎样、何时可点、为什么变灰/报错）。
- 行内 `[仅代理]` 标记 → 该按钮的 `sys_button` chunk 收紧为 `visible_to=agent`（共享页里只进代理 KB）。
- 截图问答主力：用户常拿截图问"这个按钮干嘛 / 点了会怎样 / 为什么点不了"，故按钮含义要写全后果与禁用条件。

示例（共享页 `/wallet`，代理专属按钮加 `[仅代理]`）：
```
- 提现[仅代理]：把佣金积分转出到银行卡，未实名或未绑卡时不可点。
- 生成报价：校验客户信息和关键词后生成三档方案。
```

### 4.5 常见异常行语法（钉死，解析器依赖）

`## 常见异常` 节下每行格式：

```
- <trigger> → <fix>
```

规则：
- 分隔符使用**箭头 `→`**（Unicode U+2192）
- `<trigger>` 是触发条件或报错描述，`<fix>` 是处置方式

示例：
```
- 余额不足 → 去钱包充值。
- 关键词为空 → 先添加关键词。
```

### 4.5b 用户常问行语法（钉死，解析器依赖）

`## 用户常问` 节下每行格式：

```
- <问题> → <标准答案>
```

规则：
- 分隔符使用**箭头 `→`**（Unicode U+2192）；问题在前、标准答案在后。
- 每条拆成一个 `sys_qa` chunk（content=`问题 答：答案`），是小榜截图问答**直答主力**：用户问法命中问题即返回标准答案，减少模型跨段拼答。
- 答案必须有依据（代码核验 / 深度卡 / FULL_MANUAL / 已确认 final 内容），不自由发挥；带身份差异要写清代理/普通用户分别能不能看、能不能用。
- 行内 `[仅代理]` 标记 → 该 `sys_qa` chunk 在共享页里收紧为 `visible_to=agent`，只进代理 KB。共享页的页级答案（普通用户可见）不得展开代理专属内容（佣金/提现/库存/出厂价/利润/白标/客户归属/报价系数/代理等级业务含义等），这些一律放 `[仅代理]` 问答行。

示例（共享页 `/wallet`）：
```
- 余额怎么换算人民币？ → 1 元 = 130 积分，顶部「约 ¥X」按此换算。
- 佣金积分怎么提现？ [仅代理] → 需实名+绑卡且满 ¥100，手续费 1%，最多同时 5 笔。
```

### 4.5c 截图问答指引节（`## 截图问答指引`，可选）

`## 截图问答指引` 节下每行 `- ` 列表，写「截图出现哪些字段/按钮/状态 → 定位到本页哪一步 → 怎么回答」三段式。并入 `sys_page` chunk。

- 该节是**页级内容**（随页面 `visible_to`，both 页普通用户也可见），**不支持 `[仅代理]` 行内闸**；共享页只写普通用户可安全获知的定位/答法，代理专属定位提示放进带 `[仅代理]` 的用户常问行。

示例：
```
- 截图出现「账户」卡片四格（积分/等级/推荐/授权）→ 账户卡只读区 → 答：均为只读展示，点积分余额可跳钱包。
```

### 4.6 manifest.json 结构

文件位置：`knowledge/system_kb/manifest.json`

```json
{
  "version": "<语义版本号，字符串>",
  "generated_at": "<ISO 8601 时间戳，字符串>",
  "pages": [
    {
      "route": "<路由路径>",
      "slug": "<route-slug>",
      "file": "<相对于 system_kb/ 的文件路径>"
    }
  ]
}
```
