/**
 * customer-intake · 共享客户录入组件 · Phase 06
 *
 * Phase 1(本次) · 仅写作大厅"+ 快速写作"使用 CustomerIntakeDialog
 * Phase 2(后续) · my-clients / m3-add / diagnosis / pricing 4 入口替换为复用此模块
 */

export { CustomerIntakeForm, validateQuickWriteIntake } from './CustomerIntakeForm';
export { CustomerIntakeDialog } from './CustomerIntakeDialog';
export type { SubmitMode } from './CustomerIntakeDialog';
export { BasicBlock } from './blocks/BasicBlock';
export { ProfileBlock } from './blocks/ProfileBlock';
export { KeywordsBlock } from './blocks/KeywordsBlock';
export { KnowledgeBlock } from './blocks/KnowledgeBlock';
export type {
  CustomerIntakeData,
  CustomerIntakeResult,
  AiFilledFields,
  FieldBlock,
  SellingPoint,
  CaseStudy,
  ValidationErrors,
} from './types';
export { DEFAULT_INTAKE_DATA } from './types';
