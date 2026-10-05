/**
 * CustomerIntakeForm · 共享客户录入主组件(纯字段渲染 + 校验 · 不管业务跳转)
 *
 * Phase 06 · CTO-15.23 · 2026-05-03
 *
 * 用法:
 *   <CustomerIntakeForm
 *     blocks={['basic', 'profile', 'keywords', 'knowledge']}
 *     defaultValues={{ name: '...' }}
 *     onChange={(data) => ...}
 *     errors={validationErrors}
 *   />
 *
 * Phase 1 由 CustomerIntakeDialog(写作大厅快速写作)使用
 * Phase 2 由 my-clients/m3-add/diagnosis 4 入口替换为复用此组件
 */

import { useCallback } from 'react';
import { Separator } from '@/components/ui/separator';
import { BasicBlock } from './blocks/BasicBlock';
import { ProfileBlock } from './blocks/ProfileBlock';
import { KeywordsBlock } from './blocks/KeywordsBlock';
import { KnowledgeBlock } from './blocks/KnowledgeBlock';
import type { CustomerIntakeData, FieldBlock, ValidationErrors } from './types';

interface Props {
  /** 显示哪些字段块 · 顺序按数组顺序 */
  blocks: FieldBlock[];
  data: CustomerIntakeData;
  onChange: (data: CustomerIntakeData) => void;
  errors?: ValidationErrors;

  // basic 配置
  showAiFill?: boolean;
  /** 2026-05-17 已有客户复用 · 选中已有客户时 lift up brand_id · 上层 Dialog 提交走 reuse */
  onSelectExisting?: (brandId: number | null) => void;

  // keywords 配置
  minKeywords?: number;
  maxKeywords?: number;
  keywordsRequired?: boolean;

  // profile 配置
  complexFields?: boolean;
}

export function CustomerIntakeForm({
  blocks,
  data,
  onChange,
  errors,
  showAiFill = true,
  onSelectExisting,
  minKeywords = 1,
  maxKeywords = 15,
  keywordsRequired = true,
  complexFields = true,
}: Props) {
  const handlePatch = useCallback(
    (patch: Partial<CustomerIntakeData>) => {
      onChange({ ...data, ...patch });
    },
    [data, onChange],
  );

  return (
    <div className="space-y-5">
      {blocks.map((block, idx) => (
        <div key={block}>
          {idx > 0 && <Separator className="mb-5" />}
          {block === 'basic' && (
            <BasicBlock data={data} onChange={handlePatch} errors={errors} showAiFill={showAiFill} onSelectExisting={onSelectExisting} />
          )}
          {block === 'profile' && (
            <ProfileBlock data={data} onChange={handlePatch} errors={errors} complexFields={complexFields} />
          )}
          {block === 'keywords' && (
            <KeywordsBlock
              data={data}
              onChange={handlePatch}
              errors={errors}
              minKeywords={minKeywords}
              maxKeywords={maxKeywords}
              required={keywordsRequired}
            />
          )}
          {block === 'knowledge' && <KnowledgeBlock data={data} onChange={handlePatch} />}
        </div>
      ))}
    </div>
  );
}

/** 校验工具 · 检查数据是否满足"快速写作"最小要求 */
export function validateQuickWriteIntake(
  data: CustomerIntakeData,
  opts: { keywordsRequired?: boolean; minKeywords?: number } = {},
): ValidationErrors {
  const errors: ValidationErrors = {};
  const { keywordsRequired = true, minKeywords = 1 } = opts;

  if (!data.name?.trim()) {
    errors.name = '请填客户/品牌名';
  }
  if (!data.industry?.trim()) {
    errors.industry = '请填行业';
  }
  if (keywordsRequired) {
    const kws = data.seed_keywords || [];
    if (kws.length < minKeywords) {
      errors.seed_keywords = `至少 ${minKeywords} 个关键词`;
    }
  }
  return errors;
}
