/**
 * PartnerApplyStep1 — 代理申请 Step 1：填表
 *
 * 字段:
 *   - real_name
 *   - id_card_no (18 位, 末位可为 X)
 *   - promotion_scenes (多选)
 *   - expected_monthly_customers (单选: <10 / 10-50 / 50+)
 *   - remark (可选)
 *
 * 表单草稿保存到 sessionStorage (key=partner_apply_draft), 跨步骤持久化.
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { readDraft, writeDraft, validateIdCardFormat } from './draftStorage';

const SCENES = [
  { value: 'self_media', label: '自有社媒（朋友圈 / 小红书 / 抖音等）' },
  { value: 'agency', label: '广告 / 营销 / 咨询公司' },
  { value: 'channel', label: '行业社群 / 同行渠道' },
  { value: 'line_offline', label: '线下面谈 / 活动' },
  { value: 'other', label: '其他' },
];

const CUSTOMER_RANGES = [
  { value: '<10', label: '10 人以下' },
  { value: '10-50', label: '10 - 50 人' },
  { value: '50+', label: '50 人以上' },
];

export default function PartnerApplyStep1() {
  const navigate = useNavigate();
  const [draft, setDraft] = useState(() => readDraft());
  const [errors, setErrors] = useState<Record<string, string>>({});

  const toggleScene = (v: string) => {
    const scenes = draft.promotion_scenes.includes(v)
      ? draft.promotion_scenes.filter(s => s !== v)
      : [...draft.promotion_scenes, v];
    setDraft({ ...draft, promotion_scenes: scenes });
  };

  const handleNext = () => {
    const errs: Record<string, string> = {};
    if (!draft.real_name.trim() || draft.real_name.length < 2) {
      errs.real_name = '请输入真实姓名';
    }
    const idno = draft.id_card_no.trim().toUpperCase();
    if (!validateIdCardFormat(idno)) {
      errs.id_card_no = '身份证号格式不合法（18 位，末位可为 X）';
    }
    if (draft.promotion_scenes.length === 0) {
      errs.promotion_scenes = '请至少选一个推广场景';
    }
    if (!draft.expected_monthly_customers) {
      errs.expected_monthly_customers = '请选择预期月客户规模';
    }

    if (Object.keys(errs).length > 0) {
      setErrors(errs);
      return;
    }

    writeDraft({ ...draft, real_name: draft.real_name.trim(), id_card_no: idno });
    navigate('/partner/apply/id-card');
  };

  return (
    <div className="max-w-2xl mx-auto p-6 space-y-6">
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => navigate('/partner/about')}>
          <ArrowLeft className="h-4 w-4 mr-1" />
          返回
        </Button>
        <div className="flex-1">
          <h1 className="text-lg font-semibold">服务商申请 · 第 1 步 / 共 3 步</h1>
          <p className="text-xs text-muted-foreground">填写实名信息与推广场景</p>
        </div>
      </div>

      <StepIndicator current={1} />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">实名信息</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <Field error={errors.real_name}>
            <Label>真实姓名 *</Label>
            <Input
              value={draft.real_name}
              onChange={e => setDraft({ ...draft, real_name: e.target.value })}
              placeholder="与身份证一致"
              maxLength={30}
            />
          </Field>

          <Field error={errors.id_card_no}>
            <Label>身份证号 *</Label>
            <Input
              value={draft.id_card_no}
              onChange={e => setDraft({ ...draft, id_card_no: e.target.value.toUpperCase() })}
              placeholder="18 位身份证号（末位可为 X）"
              maxLength={18}
            />
            <p className="text-[10px] text-muted-foreground mt-1">
              仅用于审核 · AES-256 加密存储 · 详见第 13 章隐私政策
            </p>
          </Field>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">推广场景与规模</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <Field error={errors.promotion_scenes}>
            <Label>推广场景 *（可多选）</Label>
            <div className="space-y-2 mt-2">
              {SCENES.map(s => (
                <label
                  key={s.value}
                  className="flex items-center gap-2 p-2 rounded-md border border-border cursor-pointer hover:bg-muted/30"
                >
                  <input
                    type="checkbox"
                    checked={draft.promotion_scenes.includes(s.value)}
                    onChange={() => toggleScene(s.value)}
                    className="accent-foreground"
                  />
                  <span className="text-sm">{s.label}</span>
                </label>
              ))}
            </div>
          </Field>

          <Field error={errors.expected_monthly_customers}>
            <Label>预期月客户规模 *</Label>
            <div className="grid grid-cols-3 gap-2 mt-2">
              {CUSTOMER_RANGES.map(r => (
                <button
                  key={r.value}
                  type="button"
                  onClick={() => setDraft({ ...draft, expected_monthly_customers: r.value })}
                  className={[
                    'py-2 px-3 rounded-md border text-sm transition-colors',
                    draft.expected_monthly_customers === r.value
                      ? 'border-foreground bg-foreground/5 text-foreground'
                      : 'border-border text-muted-foreground hover:text-foreground',
                  ].join(' ')}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </Field>

          <Field>
            <Label>备注（可选）</Label>
            <Textarea
              value={draft.remark}
              onChange={e => setDraft({ ...draft, remark: e.target.value })}
              placeholder="如有推广历史 / 已有客户资源等信息，可在此说明（选填）"
              maxLength={500}
              rows={3}
            />
            <p className="text-[10px] text-muted-foreground mt-1 text-right">
              {draft.remark.length} / 500
            </p>
          </Field>
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={handleNext}>
          下一步：上传身份证
          <ArrowRight className="ml-2 h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function Field({ children, error }: { children: React.ReactNode; error?: string }) {
  return (
    <div className="space-y-1">
      {children}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );
}

export function StepIndicator({ current }: { current: 1 | 2 | 3 }) {
  const steps = ['填表', '上传身份证', '签协议'];
  return (
    <div className="flex items-center gap-2">
      {steps.map((label, i) => {
        const idx = i + 1;
        const active = idx === current;
        const done = idx < current;
        return (
          <div key={label} className="flex items-center gap-2">
            <div
              className={[
                'h-6 w-6 rounded-full flex items-center justify-center text-xs font-medium border',
                active
                  ? 'border-foreground bg-foreground text-background'
                  : done
                  ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400'
                  : 'border-border text-muted-foreground',
              ].join(' ')}
            >
              {idx}
            </div>
            <span
              className={[
                'text-xs',
                active ? 'text-foreground font-medium' : 'text-muted-foreground',
              ].join(' ')}
            >
              {label}
            </span>
            {idx < steps.length && <div className="w-6 h-px bg-border" />}
          </div>
        );
      })}
    </div>
  );
}
