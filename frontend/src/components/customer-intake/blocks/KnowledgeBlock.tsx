/**
 * KnowledgeBlock · 资料文件上传(File[] 暂存内存 · CustomerIntakeDialog 提交时分两步落库)
 *
 * Phase 06 · CTO-15.23 · 2026-05-03
 *
 * 流程:
 *   1. 此块仅持有 File[] · 不立即上传(因为新建客户时还没 brand_id)
 *   2. CustomerIntakeDialog/WritingHall 提交时:先 POST /api/my-clients 拿 brand_id
 *   3. 再 for each file POST /api/knowledge/upload-file(multipart · kb_type=client · kb_id=brand_id)
 *      [2026-06-07 P0-1a 订正] 原写 /api/profile/{id}/knowledge/upload 是错端点(profile 路由复数 /api/profiles · 单数 404 静默丢文件)
 *   4. 用户体验:上传成功/失败均 toast 提示(WritingHall handleQuickWriteSubmit)
 *
 * v1 限制:
 *   - 类型:.pdf .docx .pptx .txt .md(旧格式 .doc/.ppt 后端读不了已不收 · BUG-2 已修:前端去 accept + 后端人话提示)
 *   - 单文件 ≤ 20MB
 *   - 数量 ≤ 10
 */

import { useRef, useCallback } from 'react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Upload, FileText, X } from 'lucide-react';
import { toast } from 'sonner';
import type { CustomerIntakeData } from '../types';

interface Props {
  data: CustomerIntakeData;
  onChange: (patch: Partial<CustomerIntakeData>) => void;
}

const ACCEPT_EXT = '.pdf,.docx,.pptx,.txt,.md';
const MAX_FILE_SIZE = 20 * 1024 * 1024; // 20 MB
const MAX_FILES = 10;

export function KnowledgeBlock({ data, onChange }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const files = data.knowledge_files || [];

  const addFiles = useCallback(
    (incoming: FileList | File[]) => {
      const arr = Array.from(incoming);
      const valid: File[] = [];
      const oversized: string[] = [];
      const dups: string[] = [];
      for (const f of arr) {
        if (f.size > MAX_FILE_SIZE) {
          oversized.push(f.name);
          continue;
        }
        if (files.some((existing) => existing.name === f.name && existing.size === f.size)) {
          dups.push(f.name);
          continue;
        }
        valid.push(f);
      }
      // [2026-06-13 客户报障] 超限单独弹 error(原和"重复"混在一条 toast.warning "跳过" · 太轻易错过 → 客户以为 BUG)
      if (oversized.length) {
        toast.error(`${oversized.join('、')} 超过 20MB，无法上传 · 请压缩或拆分后再传`, { duration: 6000 });
      }
      if (dups.length) {
        toast.warning(`已跳过重复文件：${dups.join('、')}`);
      }
      // [2026-06-13 复查修] 配额检查只对有效文件计数:原先在过滤前用 arr.length,超大/重复文件会误占
      // 10 个名额 → "8 有效 + 3 超大" 被整批拒。改为剔除超大/重复后再按 valid 计数。
      if (files.length + valid.length > MAX_FILES) {
        toast.warning(`最多 ${MAX_FILES} 个文件，超出部分未添加`);
        return;
      }
      if (valid.length) {
        onChange({ knowledge_files: [...files, ...valid] });
      }
    },
    [files, onChange],
  );

  const removeFile = useCallback(
    (idx: number) => {
      onChange({ knowledge_files: files.filter((_, i) => i !== idx) });
    },
    [files, onChange],
  );

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    if (e.dataTransfer.files?.length) {
      addFiles(e.dataTransfer.files);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-foreground">
          资料文件 <span className="text-xs text-muted-foreground font-normal">(可选 · 上传后 AI 写作引用)</span>
        </h3>
        <span className="text-xs text-muted-foreground">{files.length}/{MAX_FILES}</span>
      </div>

      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={handleDrop}
        className="rounded-md border-2 border-dashed border-border p-4 text-center hover:bg-muted/30 transition-colors"
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPT_EXT}
          className="hidden"
          onChange={(e) => {
            if (e.target.files?.length) addFiles(e.target.files);
            if (inputRef.current) inputRef.current.value = '';
          }}
        />
        <Upload className="h-6 w-6 mx-auto mb-2 text-muted-foreground" />
        <p className="text-xs text-muted-foreground mb-2">
          拖拽文件到这里 · 或
        </p>
        <Button
          variant="outline"
          size="sm"
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={files.length >= MAX_FILES}
        >
          点击选择文件
        </Button>
        <p className="text-xs text-muted-foreground mt-2">
          支持 PDF / Word / PPT / TXT · 单个 ≤ 20MB
        </p>
      </div>

      {files.length > 0 && (
        <div className="space-y-1.5">
          <Label className="text-xs">已选文件</Label>
          {files.map((f, idx) => (
            <div
              key={`${f.name}-${idx}`}
              className="flex items-center gap-2 px-2.5 py-1.5 rounded-md bg-muted/50 border border-border"
            >
              <FileText className="h-4 w-4 text-muted-foreground shrink-0" />
              <span className="text-xs flex-1 truncate" title={f.name}>{f.name}</span>
              <span className="text-xs text-muted-foreground shrink-0">
                {(f.size / 1024).toFixed(0)} KB
              </span>
              <Button
                variant="ghost"
                size="icon"
                type="button"
                onClick={() => removeFile(idx)}
                className="h-6 w-6 shrink-0 text-muted-foreground hover:text-destructive"
                aria-label={`移除 ${f.name}`}
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>
          ))}
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        💡 文件提交后将异步向量化(数秒到数分钟) · 即使索引未完成也能开始写作 · 索引好后 AI 会自动引用
      </p>
    </div>
  );
}
