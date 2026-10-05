/**
 * ToolId · 客户工具屏的 id(纯类型)。
 *
 * 🔴 [WO_260 · 2026-09-23] 从 `components/m3/tools/M3 工具注册表.ts` 抽出来的独立类型文件。
 *    原先 `services/m3/proCapabilities.ts` 为了这一个类型 `import type` 整个旧工具注册表,
 *    静态闭包因此把注册表与八个 m3 工具页一起拖进在役代码 —— E3 删 components/m3/** 后
 *    services/m3 这一侧会编译失败。抽成独立文件,**不带**注册表与工具页。
 *    M3 工具注册表.ts 里的同名联合暂不动(随 E3 删);两边在旧代码里互传时由 tsc 核对一致。
 */
export type ToolId =
  | 'customer_profile_tool'
  | 'quote_management_tool'
  | 'writing_tool'
  | 'publish_tool'
  | 'report_tool'
  | 'article_history_tool'
  | 'monitor_tool'
  | 'managed_tool';
