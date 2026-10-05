/**
 * CustomerIntake · Phase 06 共享客户录入组件类型
 *
 * 4 入口共用(Phase 06 写作大厅 · Phase 2 my-clients/m3-add/diagnosis/pricing)
 * 字段映射 client_profiles 表(单表根治后 SSOT · 见 06-PLAN.md T7)
 *
 * CTO-15.23 · 2026-05-03
 */

export type FieldBlock = 'basic' | 'profile' | 'keywords' | 'knowledge';

/** 卖点 — JSONB 数组对象(T7 schema 升级后) */
export interface SellingPoint {
  point: string;
  evidence?: string;
}

/** 成功案例 — JSONB 数组对象 */
export interface CaseStudy {
  client: string;
  industry?: string;
  result: string;
  timeline?: string;
}

/** 客户录入完整数据 · 跨入口共享 · 各入口按 fieldBlocks 决定显示哪些块 */
export interface CustomerIntakeData {
  // basic 块
  name: string;
  industry: string;
  city?: string;

  // profile 块
  business?: string;
  target_users?: string;
  company_intro?: string;
  core_value?: string;
  selling_points?: SellingPoint[];
  success_cases?: CaseStudy[];
  competitors?: string[];

  // keywords 块
  seed_keywords?: string[];

  // knowledge 块(File[] 暂存内存 · CustomerIntakeDialog 提交时分两步落库)
  knowledge_files?: File[];
}

/** 录入提交结果 */
export interface CustomerIntakeResult {
  brand_id: number;
  quote_id?: number;
  is_existing?: boolean;
}

/** AI 联网填充返回字段(对齐 /api/brand/auto-fill 响应) */
export interface AiFilledFields {
  brand_name?: string;
  industry?: string;
  city?: string;
  business?: string;
  target_users?: string;
  competitors?: string[];
  keywords?: string[];
  company_intro?: string;
  core_value?: string;
  selling_points?: string | SellingPoint[];
  success_cases?: string | CaseStudy[];
  [key: string]: unknown;
}

export type ValidationErrors = Partial<Record<keyof CustomerIntakeData, string>>;

export const DEFAULT_INTAKE_DATA: CustomerIntakeData = {
  name: '',
  industry: '',
  city: '',
};
