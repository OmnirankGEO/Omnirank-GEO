/**
 * 防御型 GEO：客户可选的检索平台集（唯一前端来源）。
 *
 * 🔴 这份清单不许在别的 .tsx 里再写一遍
 * ------------------------------------
 * 它必须与后端 `config/ai_engines.py` 的 `DEFENSIVE_GEO_BILLABLE_ENGINES` 逐项相同。
 * 门四实测过不同步的后果：前端按 ['qwen','deepseek','kimi','doubao'] 计价扣费，
 * 管线真跑 ["dashscope","deepseek","doubao","yuanbao"] —— 客户为 kimi 付了钱、
 * 报告里 kimi 一次都没出现；而且 'qwen' 这个键在后端任何注册表里都不存在
 * （canonical 键是 dashscope），所以它连"跑错了"都算不上，是"根本没这个东西"。
 *
 * 同步由判据跨语言钉住：
 *   tests/defensive_geo_2026_08_21/test_rework3_engine_ssot_pg.py
 *   ::test_frontend_selectable_set_equals_the_ssot
 * 从 SSOT 删掉一个引擎，那条判据立刻红。
 *
 * 展示名走后端下发的文案，前端不自造平台中文名
 * （对客户只显示产品名，不显示供应商/模型）。
 */
export const DEFENSIVE_GEO_PLATFORM_KEYS: readonly string[] = [
    'dashscope',
    'deepseek',
    'doubao',
    'kimi',
];
