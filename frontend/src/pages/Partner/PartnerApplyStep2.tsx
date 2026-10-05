/**
 * PartnerApplyStep2 — 代理申请 Step 2：上传身份证
 *
 * - front 必填, back 必填, selfie 可选
 * - 每张上传成功后, 后端返 OCR 结果给用户确认
 * - OCR 识别的姓名/身份证号与 Step1 填写一致时显示"对齐通过"
 */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowRight, AlertTriangle, CheckCircle2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { IdCardUpload } from '@/components/partner/IdCardUpload';
import { readDraft, writeDraft, type PartnerDraft } from './draftStorage';
import { StepIndicator } from './PartnerApplyStep1';

export default function PartnerApplyStep2() {
  const navigate = useNavigate();
  const [draft, setDraft] = useState<PartnerDraft>(() => readDraft());

  // Step1 表单不完整 → 退回
  useEffect(() => {
    if (!draft.real_name || !draft.id_card_no) {
      navigate('/partner/apply');
    }
  }, [draft.real_name, draft.id_card_no, navigate]);

  const frontOk = !!draft.front_upload?.oss_key;
  const backOk = !!draft.back_upload?.oss_key;

  const ocrFront = draft.front_upload?.ocr as
    | { name?: string | null; id_card_no?: string | null }
    | null;

  const nameMatch = ocrFront?.name?.trim() === draft.real_name.trim();
  const idMatch = ocrFront?.id_card_no?.trim()?.toUpperCase() === draft.id_card_no.toUpperCase();

  const alignOk = frontOk && nameMatch && idMatch;
  const alignWarn = frontOk && !alignOk;

  const handleNext = () => {
    if (!frontOk || !backOk) return;
    writeDraft(draft);
    navigate('/partner/apply/agreement');
  };

  return (
    <div className="max-w-2xl mx-auto p-6 space-y-6">
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => navigate('/partner/apply')}>
          <ArrowLeft className="h-4 w-4 mr-1" />
          返回
        </Button>
        <div className="flex-1">
          <h1 className="text-lg font-semibold">服务商申请 · 第 2 步 / 共 3 步</h1>
          <p className="text-xs text-muted-foreground">上传身份证照片，AI 识别后你确认</p>
        </div>
      </div>

      <StepIndicator current={2} />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">拍照规范</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-xs text-muted-foreground">
          <p>· 请在光线充足、无反光的环境下拍摄，边缘完整、文字清晰可辨</p>
          <p>· 照片仅用于审核 + 合同签署 + 反作弊核查，不会用于其他用途</p>
          <p>· 身份证号数据库内 AES-256 加密存储，照片存私有桶 15 分钟临时签名访问</p>
        </CardContent>
      </Card>

      <IdCardUpload
        side="front"
        label="身份证 · 人像面 *"
        hint="含姓名、身份证号、出生日期、住址等"
        initial={draft.front_upload}
        onSuccess={r => {
          const next = { ...draft, front_upload: r };
          setDraft(next);
          writeDraft(next);
        }}
      />

      <IdCardUpload
        side="back"
        label="身份证 · 国徽面 *"
        hint="含签发机关、有效期"
        initial={draft.back_upload}
        onSuccess={r => {
          const next = { ...draft, back_upload: r };
          setDraft(next);
          writeDraft(next);
        }}
      />

      <IdCardUpload
        side="selfie"
        label="手持身份证照片（可选）"
        hint="提高审核通过率；若上传需露脸且身份证清晰"
        initial={draft.selfie_upload}
        onSuccess={r => {
          const next = { ...draft, selfie_upload: r };
          setDraft(next);
          writeDraft(next);
        }}
      />

      {/* 一致性校验提示 */}
      {alignOk && (
        <div className="flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3">
          <CheckCircle2 className="h-4 w-4 text-emerald-400 mt-0.5 shrink-0" />
          <div className="text-xs">
            <div className="font-medium text-foreground">OCR 一致性校验通过</div>
            <div className="text-muted-foreground mt-0.5">
              姓名、身份证号与填写一致，审核时可直接比对。
            </div>
          </div>
        </div>
      )}

      {alignWarn && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
          <AlertTriangle className="h-4 w-4 text-amber-400 mt-0.5 shrink-0" />
          <div className="text-xs">
            <div className="font-medium text-foreground">OCR 与填写不完全一致</div>
            <div className="text-muted-foreground mt-0.5">
              识别到 姓名 = <span className="text-foreground">{ocrFront?.name || '—'}</span>、
              身份证号 = <span className="text-foreground">{ocrFront?.id_card_no || '—'}</span>
              。如识别有误可重新上传更清晰照片；如填写有误请返回上一步修改。提交后不一致将自动转人工审核。
            </div>
          </div>
        </div>
      )}

      <div className="flex justify-between gap-3">
        <Button variant="outline" onClick={() => navigate('/partner/apply')}>
          上一步
        </Button>
        <Button onClick={handleNext} disabled={!frontOk || !backOk}>
          下一步：签协议
          <ArrowRight className="ml-2 h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
