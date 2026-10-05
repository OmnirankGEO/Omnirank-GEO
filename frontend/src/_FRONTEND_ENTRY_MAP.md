# Frontend Entry Map · 给所有 AI 看的入口指南

**新 AI 写前端代码前必读这一份** · 5 分钟搞清新 vs 老入口

最后更新:2026-05-14 · Deploy-CTO

---

## ✅ 新入口(正在用 · 你的改动这里生效)

| 板块 | 用户 URL | 代码主目录 | App.tsx 路由 |
|---|---|---|---|
| **代理端主工作台** | `https://omnirank.top/` 或 `/dashboard` | `pages/Dashboard.tsx` + `pages/Brands/*` | `<Route path="/" element={<Layout/>}>` |
| **社媒板块**(C 端 + 工作台) | `https://omnirank.top/s` + `/s/advisor-market` + `/s/experts` + `/s/profile` 等 | **`pages/SocialStudio/**`** | `<Route path="/s/*" element={<SocialStudioProtected/>}>` |
| **GEO 诊断** | `/diagnosis/new` · `/diagnosis/progress/:id` · `/diagnosis/report/:id` | `pages/Diagnosis/**` | 上面 `/` Layout 下 |
| **GEO 报价** | `/pricing` · `/quote` · `/online-quote` | `pages/Pricing/**` · `pages/Quote/**` | |
| **GEO 监测** | `/monitoring` | `pages/Monitoring/**` | |
| **GEO 写作** | `/writing` · `/articles` · `/publish` | `pages/Publishing/**` · `pages/Knowledge/**` | |
| **GEO 顾问团队**(代理用) | `/advisors` · `/advisors/chat/:id` · `/advisors/manage` | `pages/Advisors/*` · `pages/Employees/AdvisorTeam.tsx` | |
| **GEO 客户管理** | `/brands` · `/my-clients` · `/my-clients/:id` | `pages/Brands/**` | |
| **订阅/钱包** | `/pricing-plans` · `/subscription/manage` | `pages/Pricing/SubscriptionPlansPage.tsx` · `pages/Subscription/**` | |
| **公开报告(分享链路)** | `/public/report/:id` | `pages/Public/SharedReport.tsx` | 顶级 |
| **公开报价 token** | `/q/:code` | `pages/Quote/PublicQuote.tsx` | 顶级 |
| **客户营销物料确认** | `/m/:token` | `pages/MaterialConfirm/MaterialConfirmPage.tsx` | 顶级 |
| **客户采访填写** | `/intake/:token` | `pages/Intake/IntakeFillPage.tsx` | 顶级 |
| **登录/注册** | `/login` · `/register` · `/s/login` | `pages/Login/*` | 顶级 |
| **管理后台** | `/admin/orders` · `/admin/users` · `/admin/roles` · `/admin/llm-cost` · `/admin/research-monitor` 等 | `pages/Admin/**` · `pages/OrderManagement/**` | |

---

## ❌ 老入口(已归档 · 不要在此改 bug · 改了用户看不到)

| 老板块 | 老 URL | 老代码目录 | 当前路由状态 | 新入口在哪 |
|---|---|---|---|---|
| **老社媒操盘手** | `/social-ops` (rollback only · 无 sidebar) | `pages/SocialOperator/**` + `components/social/SocialLayout.tsx` | App.tsx 412-444 行保留 Route · 但 0 sidebar 指它 | → 新社媒 `pages/SocialStudio/**` · URL `/s/*` |
| **老社媒 C 端 SEnd** | (无 active 路由 · 全删) | ~~`pages/SEnd/**`~~ + ~~`components/s_end/**`~~ | ✅ **已删除**(2026-05-14 Deploy-CTO A 级清理) | → 新社媒 `pages/SocialStudio/**` · URL `/s/*` |
| **M3 销售工作台 v5** | `/m3` · `/m3/*` | `pages/M3/**` + `components/m3/**` | App.tsx 408-409 全部 `<Navigate to="/"/>` redirect | → 代理主工作台 `pages/Dashboard.tsx` + `pages/Brands/*` |
| **老 C 端**(豆包式分屏) | `/c` · `/c/*` | `pages/CEnd/**` + `components/c_end/**` | App.tsx 376-377 全部 `<Navigate to="/"/>` redirect · ⚠️ `EmbeddedPanel` 还有 lazy ref | → 代理端 `/` 或 社媒 C 端 `/s` |

每个老目录有 `_LEGACY.md` 详细说明:
- `pages/SocialOperator/_LEGACY.md`
- `pages/M3/_LEGACY.md` + `components/m3/_LEGACY.md`
- `pages/CEnd/_LEGACY.md` + `components/c_end/_LEGACY.md`

---

## 🚧 部分共享组件(谨慎操作)

`frontend/src/components/social/**` 既被新 `SocialStudio` 用 · 也被老 `SocialOperator` 用:

- **新+老共用**(active · 不能删):`VoiceRecorder` · `SocialRechargeDialog` · `SocialStudioBalanceBar` · `ProgressiveProfileCard` · `QuickSetupSheet` · `CorpusUploader` · 等
- **仅老用**(可删):`SocialLayout.tsx`(标了 LEGACY banner)

删 `components/social/*` 任何文件前必须 grep:
```bash
grep -rn "from '@/components/social/<file>'" frontend/src/
```
出现在 `pages/SocialStudio/` / `pages/Advisors/` / `components/wallet/` / `components/subscription/` 等 = 共用 · **不能删**。

---

## 🧭 给 AI 的"我该改哪里"决策树

```
用户报 bug · 在哪个 URL ?
├── /s/* (社媒)
│   ├── 改 frontend/src/pages/SocialStudio/SocialStudioApp.tsx 
│   └── 或 pages/SocialStudio/<sub-module>/*
├── / 或 /dashboard (代理主工作台)
│   ├── 改 frontend/src/pages/Dashboard.tsx
│   └── 或 pages/Brands/* (客户列表) · pages/Diagnosis/* (诊断入口)
├── /diagnosis|monitoring|writing|pricing|advisors|... (GEO 子板块)
│   └── 改 pages/<对应板块>/*
├── /public/report/:id (分享报告)
│   └── 改 pages/Public/SharedReport.tsx
├── /q/:code · /m/:token · /intake/:token (token 链路)
│   └── 改 pages/Quote|MaterialConfirm|Intake/*
├── /s/advisor-market · /s/experts (社媒专家市场)
│   └── 改 pages/SocialStudio/SocialStudioApp.tsx (AdvisorMarketPanel)
├── /advisors (代理端顾问团队 · 跟 social 板块不同)
│   └── 改 pages/Advisors/* · pages/Employees/AdvisorTeam.tsx
└── /m3/* 或 /c/* 或 /social-ops/*
    └── ⚠️ 你看错了 · 这些是老入口 · 已 redirect / 归档 · 你的改动用户看不到
        重新看 URL · 真用户用的是上面新入口
```

---

## 历史归档说明

`docs/` 目录被 .gitignore 排除(本地参考) · 各分支历史可在 git log 找到。

完整入口演进:
- 2026-04 之前:M3 销售工作台 v5(已废弃)+ 老 C 端 `/c/*`(已废弃)
- 2026-04-28:M3 双轨过渡铁律拍板("旧版默认 · M3 顶部提供切换 · 不强推")
- 2026-05-09:Social Studio 上线 `/s/*` · 旧 SocialOperator 仅作 rollback
- 2026-05-13:M3 + CEnd 全 redirect · 7 天观察期开始
- 2026-05-14:**SEnd 死代码删除**(A 级清理)· 这份文档建立 · LEGACY banner 全面铺
- 后续:B 级 SocialOperator / C 级 M3 / D 级 CEnd 按观察期到删

---

🤖 给所有 AI:写前端代码前 · 先 `grep -rn "/s/your-route"` 看是否归到 SocialStudio · 再 grep 老入口看是否 dead · 找准位置再改。
