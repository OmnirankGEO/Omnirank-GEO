/**
 * ProfileBlock · 完整客户资料(business / target_users / company_intro / core_value /
 *                              selling_points[] / success_cases[] / competitors[])
 *
 * Phase 06 · CTO-15.23 · 2026-05-03
 *
 * 字段分级(D5):
 *   - 简单字段(string)总显示
 *   - 复杂字段(数组)props complexFields 控制 · 默认显示
 *   - 超复杂字段(pricing_tiers / testimonials / credentials)留 ClientMaterialsEditor 负责 · 本块不实装
 */

import { useCallback, useState } from 'react';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Plus, Trash2, X } from 'lucide-react';
import type { CustomerIntakeData, SellingPoint, CaseStudy, ValidationErrors } from '../types';

interface Props {
  data: CustomerIntakeData;
  onChange: (patch: Partial<CustomerIntakeData>) => void;
  errors?: ValidationErrors;
  /** complexFields 控制是否显示 selling_points[] / success_cases[](默认 true) */
  complexFields?: boolean;
}

export function ProfileBlock({ data, onChange, errors, complexFields = true }: Props) {
  // selling_points 操作
  const addSellingPoint = useCallback(() => {
    const next = [...(data.selling_points || []), { point: '', evidence: '' }];
    onChange({ selling_points: next });
  }, [data.selling_points, onChange]);

  const updateSellingPoint = useCallback(
    (idx: number, patch: Partial<SellingPoint>) => {
      const next = [...(data.selling_points || [])];
      next[idx] = { ...next[idx], ...patch };
      onChange({ selling_points: next });
    },
    [data.selling_points, onChange],
  );

  const removeSellingPoint = useCallback(
    (idx: number) => {
      const next = (data.selling_points || []).filter((_, i) => i !== idx);
      onChange({ selling_points: next });
    },
    [data.selling_points, onChange],
  );

  // success_cases 操作
  const addCase = useCallback(() => {
    const next = [...(data.success_cases || []), { client: '', industry: '', result: '', timeline: '' }];
    onChange({ success_cases: next });
  }, [data.success_cases, onChange]);

  const updateCase = useCallback(
    (idx: number, patch: Partial<CaseStudy>) => {
      const next = [...(data.success_cases || [])];
      next[idx] = { ...next[idx], ...patch };
      onChange({ success_cases: next });
    },
    [data.success_cases, onChange],
  );

  const removeCase = useCallback(
    (idx: number) => {
      const next = (data.success_cases || []).filter((_, i) => i !== idx);
      onChange({ success_cases: next });
    },
    [data.success_cases, onChange],
  );

  // competitors 操作
  const addCompetitor = useCallback(
    (name: string) => {
      const trimmed = name.trim();
      if (!trimmed) return;
      const cur = data.competitors || [];
      if (cur.includes(trimmed)) return;
      onChange({ competitors: [...cur, trimmed] });
    },
    [data.competitors, onChange],
  );

  const removeCompetitor = useCallback(
    (name: string) => {
      onChange({ competitors: (data.competitors || []).filter((c) => c !== name) });
    },
    [data.competitors, onChange],
  );

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-foreground">完整资料 <span className="text-xs text-muted-foreground font-normal">(强烈推荐 · 决定 AI 写作质量)</span></h3>

      {/* 简单字段 · string 输入 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="sm:col-span-2 space-y-1">
          <Label htmlFor="ci-business" className="text-xs">核心业务一句话</Label>
          <Input
            id="ci-business"
            value={data.business || ''}
            onChange={(e) => onChange({ business: e.target.value })}
            placeholder="例:专注于室内外装饰装修、安防工程、园林景观工程"
          />
        </div>

        <div className="sm:col-span-2 space-y-1">
          <Label htmlFor="ci-target_users" className="text-xs">目标客户群体</Label>
          <Input
            id="ci-target_users"
            value={data.target_users || ''}
            onChange={(e) => onChange({ target_users: e.target.value })}
            placeholder="例:云南省本地高端家装客户 / 商业装修甲方"
          />
        </div>

        <div className="sm:col-span-2 space-y-1">
          <Label htmlFor="ci-company_intro" className="text-xs">公司介绍</Label>
          <Textarea
            id="ci-company_intro"
            value={data.company_intro || ''}
            onChange={(e) => onChange({ company_intro: e.target.value })}
            placeholder="2-4 句话介绍公司·成立年份/规模/服务区域/经营理念"
            rows={3}
          />
        </div>

        <div className="sm:col-span-2 space-y-1">
          <Label htmlFor="ci-core_value" className="text-xs">核心价值主张</Label>
          <Textarea
            id="ci-core_value"
            value={data.core_value || ''}
            onChange={(e) => onChange({ core_value: e.target.value })}
            placeholder="给客户的核心承诺·1-2 句"
            rows={2}
          />
        </div>
      </div>

      {/* 主要竞品 · Tag 输入 */}
      <div className="space-y-1.5">
        <Label className="text-xs">主要竞品(可选)</Label>
        <CompetitorsInput
          value={data.competitors || []}
          onAdd={addCompetitor}
          onRemove={removeCompetitor}
        />
      </div>

      {/* 复杂字段 · 多条卡片 */}
      {complexFields && (
        <>
          {/* 差异化卖点 */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label className="text-xs">差异化卖点(可选 · 多条)</Label>
              <Button variant="outline" size="sm" type="button" onClick={addSellingPoint} className="h-7 text-xs">
                <Plus className="h-3 w-3 mr-1" />
                添加卖点
              </Button>
            </div>
            {(data.selling_points || []).map((sp, idx) => (
              <div key={idx} className="rounded-md border border-border p-2.5 space-y-1.5 bg-muted/30">
                <div className="flex items-start gap-2">
                  <div className="flex-1 space-y-1.5">
                    <Input
                      value={sp.point}
                      onChange={(e) => updateSellingPoint(idx, { point: e.target.value })}
                      placeholder={`卖点 ${idx + 1}:简短一句话`}
                      className="text-xs"
                    />
                    <Input
                      value={sp.evidence || ''}
                      onChange={(e) => updateSellingPoint(idx, { evidence: e.target.value })}
                      placeholder="支撑证据(数字/案例/资质 · 可选)"
                      className="text-xs"
                    />
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    type="button"
                    onClick={() => removeSellingPoint(idx)}
                    className="h-7 w-7 shrink-0 text-muted-foreground hover:text-destructive"
                    aria-label="删除卖点"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            ))}
          </div>

          {/* 成功案例 */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label className="text-xs">成功案例(可选 · 多条)</Label>
              <Button variant="outline" size="sm" type="button" onClick={addCase} className="h-7 text-xs">
                <Plus className="h-3 w-3 mr-1" />
                添加案例
              </Button>
            </div>
            {(data.success_cases || []).map((cs, idx) => (
              <div key={idx} className="rounded-md border border-border p-2.5 space-y-1.5 bg-muted/30">
                <div className="flex items-start gap-2">
                  <div className="flex-1 grid grid-cols-2 gap-1.5">
                    <Input
                      value={cs.client}
                      onChange={(e) => updateCase(idx, { client: e.target.value })}
                      placeholder="客户名"
                      className="text-xs"
                    />
                    <Input
                      value={cs.industry || ''}
                      onChange={(e) => updateCase(idx, { industry: e.target.value })}
                      placeholder="客户行业(可选)"
                      className="text-xs"
                    />
                    <Input
                      value={cs.result}
                      onChange={(e) => updateCase(idx, { result: e.target.value })}
                      placeholder="成果(数字最佳)"
                      className="text-xs col-span-2"
                    />
                    <Input
                      value={cs.timeline || ''}
                      onChange={(e) => updateCase(idx, { timeline: e.target.value })}
                      placeholder="周期(可选)"
                      className="text-xs col-span-2"
                    />
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    type="button"
                    onClick={() => removeCase(idx)}
                    className="h-7 w-7 shrink-0 text-muted-foreground hover:text-destructive"
                    aria-label="删除案例"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {errors?.business && <p className="text-xs text-rose-600">{errors.business}</p>}
    </div>
  );
}

/** Competitors Tag 输入(单独子组件 · 仅 ProfileBlock 内使用) */
interface CompetitorsInputProps {
  value: string[];
  onAdd: (name: string) => void;
  onRemove: (name: string) => void;
}

function CompetitorsInput({ value, onAdd, onRemove }: CompetitorsInputProps) {
  const [input, setInput] = useState('');

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <Input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              onAdd(input);
              setInput('');
            }
          }}
          placeholder="按回车添加竞品名"
          className="text-xs"
        />
        <Button
          variant="outline"
          size="sm"
          type="button"
          onClick={() => {
            onAdd(input);
            setInput('');
          }}
          disabled={!input.trim()}
        >
          <Plus className="h-4 w-4" />
        </Button>
      </div>
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {value.map((c) => (
            <Badge key={c} variant="outline" className="px-2 py-0.5 gap-1">
              <span className="text-xs">{c}</span>
              <button
                type="button"
                onClick={() => onRemove(c)}
                className="hover:bg-destructive/20 rounded-full p-0.5 -mr-0.5"
                aria-label={`移除 ${c}`}
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}

