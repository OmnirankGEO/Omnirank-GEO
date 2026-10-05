/**
 * Partner 申请流程: 跨步骤表单草稿 (sessionStorage)
 *
 * key = 'partner_apply_draft'
 * 用户关闭浏览器 / 换设备 → 草稿清空, 重新填
 * 用户刷新页面 / 步骤间跳转 → 草稿保留
 */

import type { IdCardUploadResult } from '@/components/partner/IdCardUpload';

const KEY = 'partner_apply_draft';

export interface PartnerDraft {
  real_name: string;
  id_card_no: string;
  promotion_scenes: string[];
  expected_monthly_customers: string;
  remark: string;
  front_upload?: IdCardUploadResult | null;
  back_upload?: IdCardUploadResult | null;
  selfie_upload?: IdCardUploadResult | null;
}

const EMPTY: PartnerDraft = {
  real_name: '',
  id_card_no: '',
  promotion_scenes: [],
  expected_monthly_customers: '',
  remark: '',
  front_upload: null,
  back_upload: null,
  selfie_upload: null,
};

export function readDraft(): PartnerDraft {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return { ...EMPTY };
    const parsed = JSON.parse(raw) as Partial<PartnerDraft>;
    return { ...EMPTY, ...parsed };
  } catch {
    return { ...EMPTY };
  }
}

export function writeDraft(draft: PartnerDraft): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(draft));
  } catch {
    // 忽略 (隐私模式等)
  }
}

export function clearDraft(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}

/** 18 位身份证基础格式校验 (不含校验码, 后端会再校验) */
export function validateIdCardFormat(idno: string): boolean {
  return /^\d{17}[0-9X]$/.test(idno);
}
