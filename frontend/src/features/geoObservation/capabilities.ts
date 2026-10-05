/**
 * 治理写能力开关（capability adapter）。
 *
 * AI-3 是只读产品 API；审核晋升/撤回、平台启停等**写**接口属 AI-2 治理层，尚未交付。
 * 在 AI-2 最终 OpenAPI 到达前：
 *  - 全部写能力关闭；
 *  - 生产 transport 绝不调用这些不存在的接口；
 *  - 管理员页隐藏写控件、显示只读状态，绝不显示点击即 404 的按钮。
 * AI-2 落地后仅需把对应开关置 true 并在集成层接线，页面语义不变。
 */

export interface ObservationCapabilities {
  /** 样本晋升审核/撤回（AI-2 治理写）。 */
  sampleGovernanceWrite: boolean;
  /** 平台启停策略写（AI-2 policy CAS）。 */
  platformPolicyWrite: boolean;
}

export const OBSERVATION_CAPABILITIES: ObservationCapabilities = {
  sampleGovernanceWrite: false,
  platformPolicyWrite: false,
};
