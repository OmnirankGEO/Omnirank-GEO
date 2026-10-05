/**
 * M3 配置 SSOT(老板红线 · 不在页面组件里硬编码)
 *
 * C20(2026-04-25)抽离:
 *   - 积分汇率(M3 收入页 原硬编码 RATE=130)
 *   - 监测引擎名清单(DeliveryMonitor 原硬编码 4 引擎名)
 *
 * 后续应从后端 settings / config endpoint 拉真实配置 · 现在先静态 export 集中。
 */

/**
 * 积分汇率:1 元 = 130 积分
 *
 * 来源:CLAUDE.md "积分体系 · 汇率 1 元 = 130 积分(故意非整数 · 阻断心算)"
 * 真实值来自 backend feature_pricing 表(应通过 PricingContext 拉)· 此处仅 fallback。
 */
export const POINTS_PER_CNY = 130;

export function pointsToCny(points: number): string {
  return (points / POINTS_PER_CNY).toFixed(2);
}

/**
 * 4 大监测引擎名(对外展示)
 *
 * 真实运行引擎从后端 /api/settings 或 /api/m3/monitor/engines 拉
 * 现在 backend 暂无 endpoint · 此处硬编码 fallback。
 *
 * 老板红线:监测引擎不能散在页面组件里 · 必须 config / adapter 集中。
 */
export interface MonitorEngineDef {
  /** 内部 key(对齐后端 model provider) */
  key: 'deepseek' | 'kimi' | 'qwen3' | 'doubao';
  /** 对外展示名 */
  name: string;
  /** 简短描述 */
  hint: string;
}

export const MONITOR_ENGINES: MonitorEngineDef[] = [
  { key: 'deepseek', name: 'DeepSeek', hint: '主引擎 · 4 大模型之首' },
  { key: 'kimi', name: 'Kimi (Moonshot)', hint: '长上下文 · k2.5' },
  { key: 'qwen3', name: 'Qwen3', hint: 'qwen3-max · 阿里通义' },
  { key: 'doubao', name: 'Doubao', hint: '字节豆包 · ai_search 模式' },
];

/**
 * 续费窗(T-21 内服务到期触发 stage=9)
 */
export const RENEWAL_WINDOW_DAYS = 21;

/**
 * stalled 风险阈值(报价已确认 N 小时未付款)
 */
export const STALLED_HOURS_THRESHOLD = 24;
