/**
 * 托管套餐(managed campaign)入口总闸 —— **单点开关**。
 *
 * 🔴 Owner 2026-08-10 裁定:**先隐藏,历史遗留功能,未来再开**。
 *
 * ## 定性:隐藏 ≠ 删除
 *
 * 托管是**保留待未来重启**的能力,不是废弃功能。所以本次:
 *   ✅ 只做**入口下线** —— 侧边栏 / C 端抽屉 / 嵌入面板 / M3 工具位;
 *   ❌ **不删**任何代码、端点、状态机、数据表、迁移;
 *   ❌ **不动** `managed_campaigns` / `managed_actions` 任何存量数据;
 *   ❌ **不清理**已有的 2 笔 consume 交易(user 25 / user 46)。
 *
 * 页面与路由**保留可访问**(直接输 URL 仍能打开),只是不再有任何**可达入口**。
 * 这样做的理由:未来重启时不需要把页面重新写一遍;而且真有人存了书签,
 * 看到的是功能页而不是 404 —— 比"消失"更好解释。
 *
 * ## 恢复怎么做(一处翻转)
 *
 * 把下面这个常量改成 `true`,四个入口同时回来。后端另需把
 * `feature_pricing` 里 `managed_campaign_recharge` / `managed_brand_recharge`
 * 的 `is_active` 改回 `true`(前端闸不是钱的闸)。
 *
 * 完整恢复清单:`docs/AI-CONTEXT/MANAGED_CAMPAIGN_RESTORE_2026-08-10.md`
 *
 * ## ⚠️ 前端隐藏不是安全闸
 *
 * 用户可以直接打 API。真正的闸是 `feature_pricing.is_active = false` +
 * `get_feature_pricing()` 的 `WHERE is_active = TRUE` —— 那一层才拦得住钱。
 */
export const MANAGED_ENTRY_ENABLED = false;
