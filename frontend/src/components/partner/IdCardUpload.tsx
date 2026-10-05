/**
 * IdCardUpload — 身份证照片上传组件
 *
 * 流程:
 *   选图 → 预览 → POST /api/partner/apply/upload-id-card (multipart)
 *       → 拿到 oss_key + ocr 结果
 *       → 展示 OCR 给用户确认
 *   用户确认无误后，把 oss_key 回传给父组件 via onSuccess
 */

import { useRef, useState } from 'react';
import { Upload, CheckCircle2, AlertTriangle, Loader2, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { authFetch } from '@/lib/api';

type Side = 'front' | 'back' | 'selfie';

export interface IdCardOcrFront {
  name?: string | null;
  id_card_no?: string | null;
  gender?: string | null;
  nation?: string | null;
  birthday?: string | null;
  address?: string | null;
  confidence?: number | null;
  raw_text?: string | null;
  error?: string | null;
}

export interface IdCardOcrBack {
  issue_authority?: string | null;
  valid_period?: string | null;
  confidence?: number | null;
  raw_text?: string | null;
  error?: string | null;
}

export interface IdCardUploadResult {
  oss_key: string;
  side: Side;
  ocr: IdCardOcrFront | IdCardOcrBack | null;
}

interface Props {
  side: Side;
  label: string;
  hint?: string;
  onSuccess: (result: IdCardUploadResult) => void;
  /** 已上传时传入的初始值（刷新后回显） */
  initial?: IdCardUploadResult | null;
}

const SIDE_LABEL: Record<Side, string> = {
  front: '人像面',
  back: '国徽面',
  selfie: '手持照片',
};

const MAX_BYTES = 10 * 1024 * 1024;

export function IdCardUpload({ side, label, hint, onSuccess, initial }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<IdCardUploadResult | null>(initial ?? null);

  const handlePick = () => inputRef.current?.click();

  const handleFile = async (file: File) => {
    setError(null);

    if (!['image/jpeg', 'image/png'].includes(file.type)) {
      setError('仅支持 JPEG / PNG 格式');
      return;
    }
    if (file.size > MAX_BYTES) {
      setError(`文件过大（>${MAX_BYTES / 1024 / 1024}MB）`);
      return;
    }

    // 本地预览
    const reader = new FileReader();
    reader.onload = e => setPreview((e.target?.result as string) || null);
    reader.readAsDataURL(file);

    setUploading(true);
    try {
      const formData = new FormData();
      formData.append('side', side);
      formData.append('file', file);

      const resp = await authFetch('/api/partner/apply/upload-id-card', {
        method: 'POST',
        body: formData,
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setError((err as { detail?: string })?.detail || '上传失败');
        return;
      }

      const data = (await resp.json()) as {
        success: boolean;
        oss_key: string;
        side: Side;
        ocr: IdCardOcrFront | IdCardOcrBack | null;
      };

      const r: IdCardUploadResult = {
        oss_key: data.oss_key,
        side: data.side,
        ocr: data.ocr,
      };
      setResult(r);
      onSuccess(r);
    } catch (e) {
      setError((e as Error)?.message || '网络错误');
    } finally {
      setUploading(false);
    }
  };

  const handleClear = () => {
    setPreview(null);
    setResult(null);
    setError(null);
    if (inputRef.current) inputRef.current.value = '';
  };

  const ocrFront = side === 'front' ? (result?.ocr as IdCardOcrFront | null) : null;
  const ocrBack = side === 'back' ? (result?.ocr as IdCardOcrBack | null) : null;
  const confidence = (result?.ocr as { confidence?: number | null })?.confidence ?? null;
  const lowConf = confidence !== null && confidence < 0.9;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div>
          <label className="text-sm font-medium">{label}</label>
          {hint && <p className="text-xs text-muted-foreground mt-0.5">{hint}</p>}
        </div>
        {result && (
          <Button variant="ghost" size="sm" onClick={handleClear} className="text-xs">
            <X className="h-3 w-3 mr-1" />
            重新上传
          </Button>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept="image/jpeg,image/png"
        className="hidden"
        onChange={e => {
          const f = e.target.files?.[0];
          if (f) handleFile(f);
        }}
      />

      {!result && !preview && (
        <button
          type="button"
          onClick={handlePick}
          className={cn(
            'w-full h-40 flex flex-col items-center justify-center gap-2',
            'border-2 border-dashed border-border rounded-lg',
            'hover:border-foreground/30 hover:bg-muted/30 transition-colors',
          )}
        >
          <Upload className="h-5 w-5 text-muted-foreground" />
          <span className="text-xs text-muted-foreground">
            点击上传{SIDE_LABEL[side]}（JPEG/PNG，≤10MB）
          </span>
        </button>
      )}

      {preview && (
        <div className="relative">
          <img
            src={preview}
            alt={SIDE_LABEL[side]}
            className="w-full max-h-48 object-contain rounded-lg border border-border bg-muted/30"
          />
          {uploading && (
            <div className="absolute inset-0 flex items-center justify-center bg-background/70 rounded-lg">
              <Loader2 className="h-6 w-6 animate-spin text-foreground" />
              <span className="ml-2 text-sm">识别中…</span>
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-md p-2">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {result && (
        <div
          className={cn(
            'rounded-lg border p-3 space-y-1.5',
            lowConf
              ? 'border-amber-500/30 bg-amber-500/5'
              : 'border-emerald-500/30 bg-emerald-500/5',
          )}
        >
          <div className="flex items-center gap-2">
            <CheckCircle2
              className={cn(
                'h-4 w-4',
                lowConf ? 'text-amber-400' : 'text-emerald-400',
              )}
            />
            <span className="text-xs font-medium">
              上传成功{confidence !== null && ` · 识别置信度 ${(confidence * 100).toFixed(0)}%`}
            </span>
          </div>

          {ocrFront && (
            <div className="text-xs text-muted-foreground space-y-0.5 pl-6">
              <div>姓名：{ocrFront.name || '—'}</div>
              <div>身份证号：{ocrFront.id_card_no || '—'}</div>
              {ocrFront.birthday && <div>生日：{ocrFront.birthday}</div>}
              {ocrFront.address && <div>住址：{ocrFront.address}</div>}
            </div>
          )}
          {ocrBack && (
            <div className="text-xs text-muted-foreground space-y-0.5 pl-6">
              <div>签发机关：{ocrBack.issue_authority || '—'}</div>
              <div>有效期：{ocrBack.valid_period || '—'}</div>
            </div>
          )}
          {lowConf && (
            <div className="text-xs text-amber-400/80 pl-6">
              置信度偏低，请核对识别结果是否正确；如有错漏请重新上传更清晰的照片。
            </div>
          )}
        </div>
      )}
    </div>
  );
}
