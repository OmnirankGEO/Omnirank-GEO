/**
 * BrandWizardBanner — M1c A1 · 资料补齐 3 步引导(BrandDetailPage 顶部 banner)
 *
 * CTO-15.9 2026-04-25 · PRD M1c §5 用户旅程:
 *   目标:代理 30 秒生成 AI 草稿 + ≤ 3 分钟代理确认
 *
 * 3 步引导(Dialog 模式):
 *   Step 1 · 确认 3 基础字段(brand_name + industry + city)
 *   Step 2 · "AI 一键补齐" → 调 /api/profiles/ai-fill → 11+ 字段回填(静默扣费 · 页面不前置告知额度)
 *   Step 3 · 核对低置信字段(AI 高亮的 aiFilledFields) → 保存 → 完整度 ≥80 解锁 CTA
 *
 * 挂载:
 *   BrandDetailPage 基本信息页顶部 · 当 completeness < 80 时自动弹醒目 banner
 *   点击"开始向导"进 Dialog
 *   localStorage key `brand_wizard_dismissed_{brandId}` 支持"不再提醒"
 *
 * 不重写 field UI · 仍走 BrandDetailPage 基本信息页表单
 * wizard 本质是"引导 + 一键 AI 补齐" UX 包装
 */
import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Sparkles, ChevronRight, CheckCircle2, Target, Loader2, X } from 'lucide-react';
import { authApi } from '@/context/AuthContext';
import { toast } from 'sonner';
import type { AiFilledFields } from './AiFillDialog';
import { FeatureTooltip } from '@/components/onboarding/FeatureTooltip';
import { isSandboxActive } from '@/sandbox/sandboxState';
import { useOnboarding } from '@/context/OnboardingContext';

interface Props {
  brandId: number;
  /** 当前 form 基础字段 */
  form: {
    name: string;
    industry: string;
    cities: string;
    business: string;
  };
  /** 当前完整度 0-100 */
  completeness: number;
  /** AI 补齐回调 · 父组件消费字段 */
  onAiFilled: (fields: AiFilledFields) => void;
  /** 保存 */
  onSave: () => Promise<void>;
  /** 进诊断页 */
  onStartDiagnosis: () => void;
  /** aiFilledFields Set · 判低置信字段 */
  aiFilledFields: Set<string>;
}

const DISMISS_KEY = (brandId: number) => `brand_wizard_dismissed_${brandId}`;

export function BrandWizardBanner({
  brandId,
  form,
  completeness,
  onAiFilled,
  onSave,
  onStartDiagnosis,
  aiFilledFields,
}: Props) {
  const { markStepCompleted } = useOnboarding();
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [filling, setFilling] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(DISMISS_KEY(brandId)) === '1';
    } catch {
      return false;
    }
  });

  // 当 completeness 更新 · 自动推进 step
  useEffect(() => {
    if (!open) return;
    if (step === 2 && aiFilledFields.size > 0) {
      setStep(3);
    }
  }, [aiFilledFields, open, step]);

  const markDismissed = () => {
    try { localStorage.setItem(DISMISS_KEY(brandId), '1'); } catch { /* noop */ }
    setDismissed(true);
  };

  // Step 2 · AI 一键补齐
  const handleAiFill = async () => {
    if (filling) return;
    setFilling(true);
    try {
      const res = await authApi.post('/api/profiles/ai-fill', { brand_id: brandId });
      if (res.data?.success && res.data?.data) {
        onAiFilled(res.data.data as AiFilledFields);
        // 静默扣费口径(2026-06-03)· 页面不前置告知额度 · 完成后走铃铛/账单通知
        // 后端 _bill_ctx('autofill_brand') 真扣 · 此处文案只报"补齐完成"不提扣费
        toast.success('AI 已补齐字段，请检查并保存');
        // Stage 1 Batch 3 · 沙盒里直接标 step 2 完成 (brand.completeness state 不会自动刷新)
        if (isSandboxActive()) {
          markStepCompleted('fill_client_profile');
        }
        setStep(3);
      } else {
        toast.error(res.data?.error || 'AI 补齐失败 · 请先上传客户知识库');
      }
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'AI 补齐失败';
      toast.error(String(msg).slice(0, 120));
    } finally {
      setFilling(false);
    }
  };

  // Step 3 · 保存 + 进诊断
  const handleSaveAndDiagnose = async () => {
    if (saving) return;
    setSaving(true);
    try {
      await onSave();
      toast.success('已保存 · 进入诊断');
      setOpen(false);
      setStep(1);
      onStartDiagnosis();
    } catch {
      // onSave 内部已 toast
    } finally {
      setSaving(false);
    }
  };

  // completeness 足够 + dismissed · 不显示 banner
  if (completeness >= 80 || dismissed) return null;

  const step1Ready = Boolean(form.name?.trim() && form.industry?.trim());
  const lowConfidenceFields = Array.from(aiFilledFields);

  return (
    <>
      {/* 顶部 Banner */}
      <div className="rounded-xl border border-amber-500/30 bg-gradient-to-r from-amber-500/10 to-orange-500/10 p-3 mb-4 flex items-center gap-3">
        <div className="h-9 w-9 rounded-lg bg-amber-500/20 flex items-center justify-center shrink-0">
          <Sparkles className="h-4 w-4 text-amber-600" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-xs font-medium text-amber-700 dark:text-amber-300">
            客户资料字段 {completeness}% · 建议补齐到 80% 再诊断
          </div>
          <div className="text-[11px] text-amber-700/80 dark:text-amber-300/80 mt-0.5">
            3 步 30 秒 AI 补齐 · 大幅提升诊断 / 拓词 / 报告质量
          </div>
        </div>
        <Button
          size="sm"
          className="h-8 text-xs shrink-0 bg-amber-500 hover:bg-amber-600 text-white"
          onClick={() => { setStep(1); setOpen(true); }}
        >
          <Sparkles className="h-3.5 w-3.5 mr-1" />开始向导
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-7 w-7 p-0 shrink-0 text-muted-foreground"
          title="不再提醒"
          onClick={markDismissed}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>

      {/* Wizard Dialog */}
      <Dialog open={open} onOpenChange={(v: boolean) => { if (!filling && !saving) setOpen(v); }}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Target className="h-5 w-5 text-amber-500" />
              资料补齐向导 ({step} / 3)
            </DialogTitle>
            {/* 步骤指示条 */}
            <div className="flex items-center gap-2 pt-2">
              {[1, 2, 3].map((s) => (
                <div key={s} className="flex-1 flex items-center gap-2">
                  <div
                    className={
                      step === s
                        ? 'h-6 w-6 rounded-full flex items-center justify-center text-xs bg-amber-500 text-white font-medium'
                        : step > s
                          ? 'h-6 w-6 rounded-full flex items-center justify-center text-xs bg-emerald-500 text-white'
                          : 'h-6 w-6 rounded-full flex items-center justify-center text-xs bg-muted text-muted-foreground'
                    }
                  >
                    {step > s ? <CheckCircle2 className="h-3.5 w-3.5" /> : s}
                  </div>
                  {s < 3 && <div className="flex-1 h-0.5 bg-muted" />}
                </div>
              ))}
            </div>
          </DialogHeader>

          {/* Step 1 · 确认基础字段 */}
          {step === 1 && (
            <div className="space-y-3 py-2">
              <div className="text-xs text-muted-foreground">
                确认 3 个核心字段 · 决定 AI 能补多准(这些在基本信息页可继续编辑)
              </div>
              <div className="space-y-2">
                <div>
                  <label className="text-[11px] text-muted-foreground">品牌 / 业务名</label>
                  <Input value={form.name} readOnly className="bg-muted/30 text-xs mt-1" />
                </div>
                <div>
                  <label className="text-[11px] text-muted-foreground">行业</label>
                  <Input
                    value={form.industry}
                    readOnly
                    className={`bg-muted/30 text-xs mt-1 ${form.industry?.trim() ? '' : 'border-rose-500/60'}`}
                  />
                  {!form.industry?.trim() && (
                    <p className="text-[10px] text-rose-500 mt-0.5">请先在基本信息页填写行业</p>
                  )}
                </div>
                <div>
                  <label className="text-[11px] text-muted-foreground">城市</label>
                  <Input value={form.cities} readOnly className="bg-muted/30 text-xs mt-1" placeholder="如 深圳 · 或 全国" />
                </div>
              </div>
              <div className="p-2 rounded bg-muted/40 text-[11px] text-muted-foreground">
                Tip:关掉向导 → 基本信息页手填这 3 字段 → 重开向导 → 下一步
              </div>
            </div>
          )}

          {/* Step 2 · AI 一键补齐 */}
          {step === 2 && (
            <div className="space-y-3 py-2">
              <div className="text-xs text-muted-foreground">
                AI 读取该客户的知识库(需先上传资料到知识库 tab)· 10-15 秒自动补 11+ 字段
              </div>
              <div className="p-4 rounded-xl border border-amber-500/30 bg-amber-500/5 flex items-center gap-3">
                <Sparkles className="h-5 w-5 text-amber-500 shrink-0" />
                <div className="flex-1 text-xs">
                  <div className="font-medium text-amber-700 dark:text-amber-300">
                    AI 一键补齐
                  </div>
                  <div className="text-amber-700/80 dark:text-amber-300/80 mt-0.5">
                    补全 行业 / 业务 / 目标客户 / 客户痛点 / 竞品 /
                    公司简介 / 核心价值 / 卖点 / 成功案例 / 客户证言 /
                    市场洞察 等字段
                  </div>
                </div>
                <Button
                  onClick={handleAiFill}
                  disabled={filling}
                  className="shrink-0 bg-amber-500 hover:bg-amber-600"
                >
                  {filling ? (
                    <><Loader2 className="h-4 w-4 mr-1 animate-spin" />补齐中...</>
                  ) : (
                    <><Sparkles className="h-4 w-4 mr-1" />一键补齐</>
                  )}
                </Button>
              </div>
              <div className="p-2 rounded bg-muted/40 text-[11px] text-muted-foreground">
                💡 若报"知识库为空" · 先到知识库 tab 上传客户资料(PDF / Word / 公司介绍文本) 再回来
              </div>
            </div>
          )}

          {/* Step 3 · 核对 + 保存 */}
          {step === 3 && (
            <div className="space-y-3 py-2">
              <div className="text-xs text-muted-foreground">
                AI 已补 {lowConfidenceFields.length} 个字段 · 琥珀高亮在基本信息页里可核对 / 编辑
              </div>
              <div className="p-3 rounded bg-emerald-500/10 border border-emerald-500/30 flex items-center gap-2 text-xs">
                <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" />
                <div className="flex-1">
                  <div className="font-medium text-emerald-700 dark:text-emerald-300">
                    当前完整度 {completeness}%
                    {completeness >= 80 ? ' · 已达标' : ` · 目标 80% · 还差 ${80 - completeness}%`}
                  </div>
                  <div className="text-emerald-700/80 dark:text-emerald-300/80 mt-0.5">
                    {lowConfidenceFields.length > 0
                      ? `AI 已补 ${lowConfidenceFields.length} 个字段 · 点"保存并开始诊断"会落库`
                      : '若未补齐字段 · 关闭向导手填更快'}
                  </div>
                </div>
              </div>
              {lowConfidenceFields.length > 0 && (
                <div className="text-[11px] text-muted-foreground">
                  AI 已补字段:{lowConfidenceFields.slice(0, 8).join(' · ')}
                  {lowConfidenceFields.length > 8 && ' · ...'}
                </div>
              )}
              {/* CTO-15.16 round2 Task E · 下一步引导
                  · BatchUpgrade(persist=true)/ai-fill 单步只能补 B/C/E 组 · 拉到 ~75-80
                  · 进诊断前还有 D 组(industry_brief 深度解析 + 代理确认)能 +10
                  · 给代理明确下一步 · 不让他停在 step 3 不知道走哪
              */}
              <div className="p-3 rounded border border-amber-500/30 bg-amber-500/10 text-[11px] space-y-1">
                <div className="font-medium text-amber-700 dark:text-amber-300">下一步建议</div>
                {completeness >= 80 ? (
                  <>
                    <div className="text-amber-700/90 dark:text-amber-300/90">
                      ✓ 已达 80 · 可保存进诊断 · 想再 +10 分进入"领先级"·
                    </div>
                    <div className="text-amber-700/80 dark:text-amber-300/80">
                      → 在「行业洞察」Tab 跑「深度解析」· 完成后点「确认行业简报」·
                      brief 才会注入诊断/拓词/写作 prompt
                    </div>
                  </>
                ) : completeness >= 60 ? (
                  <>
                    <div className="text-amber-700/90 dark:text-amber-300/90">
                      还差 {80 - completeness}% · 推荐顺序:
                    </div>
                    <div className="text-amber-700/80 dark:text-amber-300/80">
                      1️⃣ 「行业洞察」Tab 跑「深度解析」(D 组 +10)
                    </div>
                    <div className="text-amber-700/80 dark:text-amber-300/80">
                      2️⃣ 手填本地竞品 / 权威来源 / 差异化定位(E 组 +6)·
                      基本信息页可见琥珀色字段
                    </div>
                    <div className="text-amber-700/80 dark:text-amber-300/80">
                      3️⃣ 完成后回向导确认行业简报 · brief 才进生产链路
                    </div>
                  </>
                ) : (
                  <>
                    <div className="text-amber-700/90 dark:text-amber-300/90">
                      仍 {completeness}% · 知识库可能太薄 · 推荐:
                    </div>
                    <div className="text-amber-700/80 dark:text-amber-300/80">
                      1️⃣ 「知识库」Tab 上传更多客户文档(PDF / Word / 公司介绍文本 ≥ 200 字)
                    </div>
                    <div className="text-amber-700/80 dark:text-amber-300/80">
                      2️⃣ 重跑「AI 一键补齐」· 资料越厚 confidence 越 strong
                    </div>
                    <div className="text-amber-700/80 dark:text-amber-300/80">
                      3. 仍未到 60 时考虑代理直接手填基本信息页 · 比 AI 推断更准
                    </div>
                  </>
                )}
              </div>
            </div>
          )}

          <DialogFooter>
            {step > 1 && (
              <Button variant="ghost" onClick={() => setStep((step - 1) as 1 | 2)} disabled={filling || saving}>
                上一步
              </Button>
            )}
            {step === 1 && (
              <Button onClick={() => setStep(2)} disabled={!step1Ready}>
                下一步 <ChevronRight className="h-4 w-4 ml-1" />
              </Button>
            )}
            {step === 2 && (
              <Button variant="outline" onClick={() => setStep(3)}>
                跳过(我自己填)
              </Button>
            )}
            {step === 3 && (
              <Button onClick={handleSaveAndDiagnose} disabled={saving}>
                {saving ? (
                  <><Loader2 className="h-4 w-4 mr-1 animate-spin" />保存中...</>
                ) : (
                  <><Target className="h-4 w-4 mr-1" />保存并开始诊断</>
                )}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export default BrandWizardBanner;
