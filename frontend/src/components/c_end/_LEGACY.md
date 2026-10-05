# ⚠️ LEGACY DIRECTORY · DO NOT USE FOR NEW WORK

**老 C 端配套组件 · 已归档 · 不要在此新增/修 bug**

## 当前状态

整个 `components/c_end/**` 是老 C 端(`/c/*`)的配套组件。
`/c` + `/c/*` 已全部 redirect 到 `/`(App.tsx 行 376-377)。

- **删除计划**: D 级清理目标(中风险 · 需先 grep `EmbeddedPanel.tsx` 引用面)

## ⚠️ EmbeddedPanel 不确定性

`EmbeddedPanel.tsx` 还 lazy import `pages/CEnd/CEndGeoPlanPage`。
彻底删本目录前必须先验:**谁还 import EmbeddedPanel?**

如果 0 引用 → 整目录安全删
如果还有 active 组件 import → 保留 EmbeddedPanel · 其他可删

## 文件清单(待 EmbeddedPanel grep 后再定)

```
CEndDrawer.tsx              CEndLayout.tsx            EmbeddedPanel.tsx ← 待验
ManagedSheet.tsx            PanelSkeleton.tsx         QualityWarningDialog.tsx
RunningTasksBubble.tsx      SameBrandRunningDialog.tsx
TrialPassApplyDialog.tsx    TrialPassBanner.tsx
```

## ✅ 新入口

| 用户 | URL | 代码 |
|---|---|---|
| 代理端 | `/` | `pages/Dashboard.tsx` |
| 社媒 C 端 | `/s` | `pages/SocialStudio/**` |

详见 `frontend/src/pages/CEnd/_LEGACY.md`

---

最后更新:2026-05-14 · Deploy-CTO
