/**
 * KeywordsBlock · 关键词 Tag 输入(Phase 06 不带 AI 推词 · 复用 BasicBlock 的 AI 填充返回的 keywords)
 *
 * Phase 06 · CTO-15.23 · 2026-05-03
 */

import { useState, useCallback } from 'react';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Plus, X } from 'lucide-react';
import { toast } from 'sonner';
import type { CustomerIntakeData, ValidationErrors } from '../types';

interface Props {
  data: CustomerIntakeData;
  onChange: (patch: Partial<CustomerIntakeData>) => void;
  errors?: ValidationErrors;
  minKeywords?: number;
  maxKeywords?: number;
  required?: boolean;
}

export function KeywordsBlock({
  data,
  onChange,
  errors,
  minKeywords = 1,
  maxKeywords = 15,
  required = true,
}: Props) {
  const [input, setInput] = useState('');
  const keywords = data.seed_keywords || [];

  const addKeyword = useCallback(
    (kw: string) => {
      const trimmed = kw.trim();
      if (!trimmed) return;
      if (keywords.includes(trimmed)) {
        toast.info(`已有「${trimmed}」`);
        return;
      }
      if (keywords.length >= maxKeywords) {
        toast.warning(`最多 ${maxKeywords} 个关键词`);
        return;
      }
      onChange({ seed_keywords: [...keywords, trimmed] });
      setInput('');
    },
    [keywords, maxKeywords, onChange],
  );

  const removeKeyword = useCallback(
    (kw: string) => {
      onChange({ seed_keywords: keywords.filter((k) => k !== kw) });
    },
    [keywords, onChange],
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-foreground">
          关键词{required ? ' *' : ''}
          <span className="text-xs text-muted-foreground font-normal ml-2">
            ({keywords.length}/{maxKeywords} · 最少 {minKeywords} 个)
          </span>
        </h3>
      </div>

      <div className="flex gap-2">
        <Input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              addKeyword(input);
            }
          }}
          placeholder="按回车添加 · 例:GEO 优化 / 装修设计"
          disabled={keywords.length >= maxKeywords}
        />
        <Button
          variant="outline"
          size="sm"
          onClick={() => addKeyword(input)}
          disabled={!input.trim() || keywords.length >= maxKeywords}
        >
          <Plus className="h-4 w-4" />
        </Button>
      </div>

      {keywords.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {keywords.map((kw) => (
            <Badge key={kw} variant="secondary" className="px-2 py-0.5 gap-1">
              <span>{kw}</span>
              <button
                type="button"
                onClick={() => removeKeyword(kw)}
                className="hover:bg-destructive/20 rounded-full p-0.5 -mr-0.5"
                aria-label={`移除 ${kw}`}
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}

      {errors?.seed_keywords && <p className="text-xs text-rose-600">{errors.seed_keywords}</p>}

      <p className="text-xs text-muted-foreground">
        建议 3-10 个客户会问的词 · 比如行业关键词 + 城市 + 产品名
      </p>
    </div>
  );
}
