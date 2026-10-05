import { ChangeEvent, useEffect, useMemo, useRef, useState } from 'react';
import { FileText, ImageIcon, Trash2, UploadCloud } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { uploadRefundAttachment } from './api';
import type { PendingEvidence, RefundAttachment, RefundWorkOrder } from './types';
import { toast } from 'sonner';

const evidenceOptions = [
  { value: 'chat_record', label: '聊天记录' },
  { value: 'payment_proof', label: '付款凭证' },
  { value: 'customer_request', label: '客户申请' },
  { value: 'agent_confirmation', label: '服务商确认' },
  { value: 'payout_proof', label: '打款凭证' },
  { value: 'other', label: '其他' },
];

function sizeLabel(size?: number) {
  if (!size) return '0 KB';
  if (size > 1024 * 1024) return `${(size / 1024 / 1024).toFixed(2)} MB`;
  return `${Math.max(1, Math.round(size / 1024))} KB`;
}

function evidenceLabel(value: string) {
  return evidenceOptions.find((item) => item.value === value)?.label || '其他';
}

function revokePreviewUrl(item: PendingEvidence) {
  if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
}

export function EvidenceUploader({
  workOrder,
  pendingFiles,
  onPendingChange,
  onWorkOrderChange,
  disabled,
}: {
  workOrder?: RefundWorkOrder | null;
  pendingFiles: PendingEvidence[];
  onPendingChange: (files: PendingEvidence[]) => void;
  onWorkOrderChange?: (workOrder: RefundWorkOrder) => void;
  disabled?: boolean;
}) {
  const [evidenceType, setEvidenceType] = useState('chat_record');
  const [uploading, setUploading] = useState(false);
  const attachments = workOrder?.attachments || [];
  const previousPendingRef = useRef<PendingEvidence[]>(pendingFiles);

  const accept = useMemo(() => 'image/png,image/jpeg,image/webp,application/pdf', []);

  useEffect(() => {
    const nextUrls = new Set(
      pendingFiles
        .map((item) => item.previewUrl)
        .filter((url): url is string => Boolean(url)),
    );
    previousPendingRef.current.forEach((item) => {
      if (item.previewUrl && !nextUrls.has(item.previewUrl)) revokePreviewUrl(item);
    });
    previousPendingRef.current = pendingFiles;
  }, [pendingFiles]);

  useEffect(() => () => {
    previousPendingRef.current.forEach(revokePreviewUrl);
  }, []);

  const handleFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!files.length) return;
    const accepted = files.filter((file) => {
      if (file.size > 10 * 1024 * 1024) {
        toast.error(`${file.name} 超过 10MB`);
        return false;
      }
      const ok = ['image/png', 'image/jpeg', 'image/webp', 'application/pdf'].includes(file.type)
        || /\.(png|jpe?g|webp|pdf)$/i.test(file.name);
      if (!ok) toast.error(`${file.name} 不是支持的凭证格式`);
      return ok;
    });
    if (!accepted.length) return;

    if (workOrder?.id) {
      setUploading(true);
      try {
        let latest: RefundWorkOrder | null = null;
        for (const file of accepted) {
          const res = await uploadRefundAttachment(workOrder.id, file, evidenceType);
          latest = res.work_order;
        }
        if (latest) onWorkOrderChange?.(latest);
        toast.success('凭证已上传');
      } catch (error: any) {
        toast.error(error.message || '上传失败');
      } finally {
        setUploading(false);
      }
      return;
    }

    const next = accepted.map((file) => ({
      id: `${file.name}-${file.lastModified}-${Math.random().toString(36).slice(2)}`,
      file,
      evidence_type: evidenceType,
      previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
    }));
    onPendingChange([...pendingFiles, ...next]);
  };

  const removePending = (id: string) => {
    onPendingChange(pendingFiles.filter((item) => item.id !== id));
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-3 sm:flex-row">
        <Select value={evidenceType} onValueChange={setEvidenceType}>
          <SelectTrigger className="h-10 rounded-md border-white/10 bg-background/60 sm:w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {evidenceOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <label className={cn(
          'flex min-h-[44px] flex-1 cursor-pointer items-center justify-center gap-2 rounded-md border border-dashed border-white/15 bg-background/45 px-4 text-sm text-muted-foreground transition hover:border-emerald-400/50 hover:text-foreground',
          disabled && 'pointer-events-none opacity-50',
        )}>
          <UploadCloud className="h-4 w-4" />
          {uploading ? '上传中' : workOrder?.id ? '上传凭证' : '选择凭证'}
          <input
            type="file"
            multiple
            accept={accept}
            className="hidden"
            disabled={disabled || uploading}
            onChange={handleFiles}
          />
        </label>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {attachments.map((file) => (
          <AttachmentCard key={file.id} attachment={file} />
        ))}
        {pendingFiles.map((item) => (
          <div key={item.id} className="relative rounded-md border border-white/10 bg-background/45 p-3">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="absolute right-2 top-2 h-7 w-7"
              onClick={() => removePending(item.id)}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
            <div className="mb-3 flex h-24 items-center justify-center overflow-hidden rounded-md bg-muted/30">
              {item.file.type.startsWith('image/') ? (
                <img src={item.previewUrl} alt={item.file.name} className="h-full w-full object-cover" />
              ) : (
                <FileText className="h-10 w-10 text-red-300" />
              )}
            </div>
            <p className="truncate text-sm font-medium text-foreground">{item.file.name}</p>
            <p className="mt-1 text-xs text-muted-foreground">{evidenceLabel(item.evidence_type)} · {sizeLabel(item.file.size)} · 待上传</p>
          </div>
        ))}
      </div>

      {!attachments.length && !pendingFiles.length && (
        <div className="rounded-md border border-white/10 bg-background/35 p-4 text-sm text-muted-foreground">
          至少上传 1 个证据后才能提交审核。支持图片和 PDF，单个文件不超过 10MB。
        </div>
      )}
    </div>
  );
}

function AttachmentCard({ attachment }: { attachment: RefundAttachment }) {
  const isImage = attachment.file_type === 'image';
  return (
    <a
      href={attachment.file_url}
      target="_blank"
      rel="noreferrer"
      className="block rounded-md border border-white/10 bg-background/45 p-3 transition hover:border-emerald-400/40"
    >
      <div className="mb-3 flex h-24 items-center justify-center overflow-hidden rounded-md bg-muted/30">
        {isImage ? (
          <img src={attachment.file_url} alt={attachment.file_name} className="h-full w-full object-cover" />
        ) : (
          <FileText className="h-10 w-10 text-red-300" />
        )}
      </div>
      <div className="flex items-start gap-2">
        {isImage ? <ImageIcon className="mt-0.5 h-4 w-4 text-emerald-300" /> : <FileText className="mt-0.5 h-4 w-4 text-red-300" />}
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-foreground">{attachment.file_name}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {evidenceLabel(attachment.evidence_type)} · {sizeLabel(attachment.file_size_bytes)}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            上传人 {attachment.uploaded_by || '-'} · {(attachment.uploaded_at || '').slice(0, 16).replace('T', ' ')}
          </p>
        </div>
      </div>
    </a>
  );
}
