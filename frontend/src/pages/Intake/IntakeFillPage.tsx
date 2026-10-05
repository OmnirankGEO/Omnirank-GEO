/**
 * IntakeFillPage — 客户公开填写页 (P0-7 重写为分步采访 · 2026-05-04)
 *
 * 路径: /intake/:token (无登录)
 * 设计: 分步采访 · 每步 1-2 题 · 进度条 · 总览页 · 服务端断点续填
 *
 * 红线:
 *   - 不显示内部代理信息 / 利润 / 成本
 *   - AI 草稿要明显标"AI 建议", 客户必须点确认
 *   - 步骤数据每步保存到服务端 (POST /step), 刷新可恢复
 *   - AI 不可用时不阻塞客户填写 (按钮 degraded 提示, 仍可手填 + Next)
 *   - 不暴露 LLM 报错给客户
 */

import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Loader2, Sparkles, Check, AlertCircle, ShieldCheck,
  ChevronLeft, ChevronRight, Edit2, Send,
} from 'lucide-react';
import { useSoftKeyboardOpen } from '@/hooks/useSoftKeyboardOpen';
import {
  publicIntakeApi,
  type PublicIntakeView,
  type PublicAISuggestResponse,
  type IntakeFlowStep,
} from '@/services/intake';
import { useForceLightMode } from '@/pages/Selection/hooks/useForceLightMode';
import { OssAttribution } from '@/components/common/OssAttribution';

// ==================== 主页面 ====================

export default function IntakeFillPage() {
  // [2026-06-02 客户公开页强制亮色] 同 MaterialConfirm — 防继承代理端 .dark 导致亮色设计(bg-white)崩坏
  useForceLightMode();
  // [WO_IOS_TOUCH_UX 2026-08-05] 软键盘弹出时底部固定条会被键盘盖住 → 让它临时退回文档流。
  //   🔴 必须在所有 early return(loading / loadError / submitted)之前调,否则违反 hooks 规则。
  const { inset: keyboardInset } = useSoftKeyboardOpen();
  const { token } = useParams<{ token: string }>();
  const [view, setView] = useState<PublicIntakeView | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<{ status?: number; message: string } | null>(null);

  // 形态: 'step' (分步采访) | 'summary' (总览页)
  const [phase, setPhase] = useState<'step' | 'summary'>('step');
  const [stepIndex, setStepIndex] = useState(0);

  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [answeredKeys, setAnsweredKeys] = useState<string[]>([]);

  const [aiDrafts, setAiDrafts] = useState<PublicAISuggestResponse['drafts']>({});
  const [aiAccepted, setAiAccepted] = useState<Record<string, boolean>>({});
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);

  const [savingStep, setSavingStep] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState<{ id: number; message: string } | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // ---- 加载 + 服务端草稿恢复 ----
  useEffect(() => {
    if (!token) return;
    setLoading(true);
    publicIntakeApi
      .view(token)
      .then(v => {
        setView(v);
        setLoadError(null);
        // 服务端有保存的草稿 → 恢复进度
        if (v.draft_payload && Object.keys(v.draft_payload).length > 0) {
          const restored: Record<string, string> = {};
          Object.entries(v.draft_payload).forEach(([k, val]) => {
            if (val == null) return;
            if (Array.isArray(val)) {
              restored[k] = val.map(x => String(x)).join('\n');
            } else if (typeof val === 'object') {
              restored[k] = JSON.stringify(val);
            } else {
              restored[k] = String(val);
            }
          });
          setFormValues(restored);
        }
        setAnsweredKeys(v.answered_fields || []);
        // 已知字段 prefilled 也填充
        v.flow_steps.forEach(step => {
          step.fields.forEach(f => {
            if (f.prefilled && f.current_value != null) {
              const val = Array.isArray(f.current_value)
                ? (f.current_value as unknown[]).map(x => String(x)).join('\n')
                : typeof f.current_value === 'object'
                ? JSON.stringify(f.current_value)
                : String(f.current_value);
              setFormValues(prev => ({ ...prev, [f.key]: prev[f.key] || val }));
            }
          });
        });
        // 跳到服务端记录的 step_index (但夹紧到合法范围)
        const validSteps = v.flow_steps.filter(s => !s.skipped).length;
        const target = Math.max(0, Math.min(v.step_index || 0, validSteps - 1));
        setStepIndex(target);
      })
      .catch((err: { status?: number; message?: string }) => {
        setLoadError({
          status: err.status,
          message: err.message || '链接无法打开',
        });
      })
      .finally(() => setLoading(false));
  }, [token]);

  // 计算非跳过的步骤序列
  const activeSteps: IntakeFlowStep[] = useMemo(
    () => (view?.flow_steps || []).filter(s => !s.skipped),
    [view],
  );

  if (loading) {
    return (
      <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] flex items-center justify-center bg-slate-50">
        <Loader2 className="w-6 h-6 animate-spin text-slate-400" />
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] flex items-center justify-center bg-slate-50 px-4">
        <div className="max-w-md text-center">
          <AlertCircle className="w-12 h-12 mx-auto text-rose-500 mb-3" />
          <h1 className="text-lg font-semibold text-slate-900 mb-2">
            {loadError.status === 410 ? '链接已不可用' : '链接无法打开'}
          </h1>
          <p className="text-sm text-slate-600">{loadError.message}</p>
          <p className="text-xs text-slate-400 mt-4">如需继续, 请联系给你发链接的顾问。</p>
        </div>
      </div>
    );
  }

  if (!view) return null;

  if (submitted) {
    return (
      <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] flex items-center justify-center bg-slate-50 px-4">
        <div className="max-w-md text-center bg-white rounded-2xl p-8 shadow-sm">
          <div className="w-14 h-14 mx-auto rounded-full bg-emerald-50 flex items-center justify-center mb-4">
            <Check className="w-7 h-7 text-emerald-600" />
          </div>
          <h1 className="text-lg font-semibold text-slate-900 mb-2">已发给对方审核</h1>
          <p className="text-sm text-slate-600 leading-relaxed">{submitted.message}</p>
          <p className="text-xs text-slate-400 mt-6">提交编号 #{submitted.id}</p>
        </div>
      </div>
    );
  }

  const currentStep = activeSteps[stepIndex];
  const totalSteps = activeSteps.length;
  const progressPct = totalSteps > 0
    ? Math.round(((stepIndex + (phase === 'summary' ? 1 : 0)) / (totalSteps + 1)) * 100)
    : 0;

  const handleChange = (key: string, value: string) => {
    setFormValues(prev => ({ ...prev, [key]: value }));
    if (aiAccepted[key]) {
      setAiAccepted(prev => ({ ...prev, [key]: false }));
    }
  };

  const handleAIHelp = async () => {
    if (!token || aiBusy) return;
    if (view.ai_suggest_remaining <= 0) {
      setAiError(`AI 帮填已达上限, 请直接手动填写。`);
      return;
    }
    setAiBusy(true);
    setAiError(null);
    try {
      const resp = await publicIntakeApi.aiSuggest(token, formValues);
      setAiDrafts(prev => ({ ...prev, ...(resp.drafts || {}) }));
      setView(prev => (prev ? { ...prev, ai_suggest_remaining: resp.ai_suggest_remaining } : prev));
      if (resp.degraded) {
        setAiError('AI 帮填功能暂时不可用, 请手动填写');
      }
    } catch (err) {
      const e = err as Error;
      setAiError(e.message || 'AI 帮填失败, 请手动填写');
    } finally {
      setAiBusy(false);
    }
  };

  const acceptAI = (key: string) => {
    const draft = aiDrafts[key];
    if (!draft || draft.value === null) return;
    handleChange(key, String(draft.value));
    setAiAccepted(prev => ({ ...prev, [key]: true }));
  };

  // ---- 步骤校验: 当前步必填字段是否都填了 ----
  const validateCurrentStep = (): string | null => {
    if (!currentStep) return null;
    for (const f of currentStep.fields) {
      if (f.optional) continue;
      const v = (formValues[f.key] || '').trim();
      if (!v) return `请回答「${f.label}」`;
    }
    return null;
  };

  // ---- 保存当前步到服务端 ----
  const saveCurrentStep = async (): Promise<boolean> => {
    if (!token || !currentStep) return false;
    setSavingStep(true);
    setSubmitError(null);
    try {
      const stepPayload: Record<string, unknown> = {};
      currentStep.fields.forEach(f => {
        const v = (formValues[f.key] || '').trim();
        if (v) stepPayload[f.key] = v;
      });
      const newAnswered = Array.from(new Set([
        ...answeredKeys,
        ...currentStep.fields.map(f => f.key).filter(k => (formValues[k] || '').trim()),
      ]));
      await publicIntakeApi.saveStep(token, {
        step_index: stepIndex + 1, // 下一步 index (服务端记录"已完成到第 N 步")
        payload: stepPayload,
        answered_fields: newAnswered,
      });
      setAnsweredKeys(newAnswered);
      return true;
    } catch (err) {
      // 草稿保存失败不阻塞客户继续填 · 静默降级
      const e = err as Error;
      // 410 / 404 是 token 失效 · 必须提示
      const errStatus = (err as { status?: number }).status;
      if (errStatus === 410 || errStatus === 404) {
        setLoadError({ status: errStatus, message: e.message || '链接已不可用' });
        return false;
      }
      // 其他错误静默 (网络抖动) · 仍允许继续
      return true;
    } finally {
      setSavingStep(false);
    }
  };

  const goNext = async () => {
    const err = validateCurrentStep();
    if (err) {
      setSubmitError(err);
      return;
    }
    setSubmitError(null);
    const ok = await saveCurrentStep();
    if (!ok) return;
    if (stepIndex < totalSteps - 1) {
      setStepIndex(stepIndex + 1);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      // 最后一步 → 总览
      setPhase('summary');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  const goPrev = () => {
    if (phase === 'summary') {
      setPhase('step');
      return;
    }
    if (stepIndex > 0) {
      setStepIndex(stepIndex - 1);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  const goEditStep = (idx: number) => {
    setStepIndex(idx);
    setPhase('step');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  // ---- 最终提交 ----
  const handleFinalSubmit = async () => {
    if (!token || submitting) return;
    setSubmitError(null);
    setSubmitting(true);
    try {
      const aiSuggestedSubset: Record<string, unknown> = {};
      Object.keys(aiAccepted).forEach(k => {
        if (aiAccepted[k] && aiDrafts[k]?.value != null) {
          aiSuggestedSubset[k] = aiDrafts[k].value;
        }
      });
      const payload: Record<string, unknown> = {};
      Object.entries(formValues).forEach(([k, v]) => {
        const trimmed = v?.trim?.();
        if (trimmed) payload[k] = trimmed;
      });

      const resp = await publicIntakeApi.submit(token, {
        payload,
        ai_suggested: Object.keys(aiSuggestedSubset).length ? aiSuggestedSubset : undefined,
        submitted_by_name: formValues.submitted_by_name?.trim() || undefined,
        submitted_by_phone: formValues.submitted_by_phone?.trim() || undefined,
      });
      setSubmitted({ id: resp.submission_id, message: resp.message });
    } catch (err) {
      const e = err as Error;
      setSubmitError(e.message || '提交失败, 请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  // 该步是否有 AI 可建议字段
  const stepAISuggestable = currentStep?.fields.some(
    f => ['business', 'industry', 'service_scope', 'target_users', 'core_value',
          'selling_points', 'company_intro'].includes(f.key),
  );

  return (
    <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] bg-slate-50">
      {/* 顶部进度条 */}
      <header className="bg-white border-b border-slate-200 sticky top-0 z-20">
        <div className="max-w-2xl mx-auto px-4 py-4">
          <div className="flex items-center justify-between mb-2">
            <h1 className="text-sm font-semibold text-slate-900">资料补充</h1>
            <span className="text-xs text-slate-500">
              {phase === 'summary'
                ? '总览'
                : `第 ${stepIndex + 1} / ${totalSteps} 步`}
            </span>
          </div>
          <div className="w-full h-1.5 bg-slate-100 rounded-full overflow-hidden">
            <div
              className="h-full bg-emerald-500 transition-all duration-300"
              style={{ width: `${progressPct}%` }}
            />
          </div>
          {view.expires_at && (
            <p className="text-[11px] text-slate-400 mt-2">
              链接有效期至 {new Date(view.expires_at).toLocaleString()}
            </p>
          )}
        </div>
      </header>

      <main className="max-w-2xl mx-auto px-4 py-5 pb-32">
        {/* ---- 总览页 ---- */}
        {phase === 'summary' ? (
          <SummaryView
            view={view}
            formValues={formValues}
            onEdit={goEditStep}
          />
        ) : currentStep ? (
          <StepView
            step={currentStep}
            formValues={formValues}
            aiDrafts={aiDrafts}
            aiAccepted={aiAccepted}
            onChange={handleChange}
            onAcceptAI={acceptAI}
          />
        ) : (
          <div className="text-center py-10 text-sm text-slate-500">
            没有需要补充的资料 — 你可以直接提交。
          </div>
        )}

        {/* AI 帮整理 (仅在 step 阶段且当前步包含可建议字段时展示) */}
        {phase === 'step' && stepAISuggestable && (
          <div className="mt-5 bg-white rounded-xl border border-slate-200 p-4 flex items-center justify-between gap-3">
            <div className="flex-1">
              <div className="flex items-center gap-2 text-sm font-medium text-slate-900">
                <Sparkles className="w-4 h-4 text-amber-500" />
                AI 帮整理草稿
              </div>
              <p className="text-xs text-slate-500 mt-0.5">
                基于已填的信息生成草稿, 你确认后再继续。剩余 {view.ai_suggest_remaining} 次。
              </p>
            </div>
            <button
              type="button"
              onClick={handleAIHelp}
              disabled={aiBusy || view.ai_suggest_remaining <= 0}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-slate-900 text-white text-sm font-medium hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed min-h-[44px]"
              aria-label="AI 帮整理草稿"
            >
              {aiBusy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              {aiBusy ? '生成中' : '帮我整理'}
            </button>
          </div>
        )}

        {aiError && (
          <div className="mt-4 px-4 py-3 rounded-lg bg-amber-50 border border-amber-200 text-amber-800 text-xs">
            {aiError}
          </div>
        )}

        {submitError && (
          <div className="mt-4 px-4 py-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-sm">
            {submitError}
          </div>
        )}

        <div className="text-xs text-slate-400 leading-relaxed bg-slate-100 rounded-lg p-3 mt-6">
          <ShieldCheck className="w-3.5 h-3.5 inline mr-1 align-text-bottom" />
          你提交的信息只用于生成你的品牌诊断和方案, 顾问审核后才会更新到正式资料中。
        </div>
        {/* WO_329 开源版署名位:main 已留 pb-32,不被底部固定操作栏盖住;开关关 ⇒ 不渲染 */}
        <OssAttribution className="mt-4" />
      </main>

      {/* 底部固定操作栏
          [WO_IOS_TOUCH_UX 2026-08-05] iOS 软键盘弹出时不改 layout viewport,
          这条 fixed bottom-0 会被键盘整条盖住 → 客户点进输入框后就看不见"下一步/提交",
          体感是"卡在这一步动不了"。键盘打开时改为静态跟在内容后(收起键盘立刻回到底部固定)。 */}
      <div
        className="fixed bottom-0 inset-x-0 bg-white border-t border-slate-200 px-4 py-3 z-10"
        style={keyboardInset > 0 ? { transform: `translateY(-${keyboardInset}px)` } : undefined}
      >
        <div className="max-w-2xl mx-auto flex items-center gap-3">
          {(stepIndex > 0 || phase === 'summary') && (
            <button
              type="button"
              onClick={goPrev}
              disabled={savingStep || submitting}
              className="inline-flex items-center justify-center gap-1.5 px-4 py-3 rounded-xl border border-slate-200 bg-white text-slate-700 text-sm font-medium disabled:opacity-50 min-h-[48px]"
            >
              <ChevronLeft className="w-4 h-4" />
              上一步
            </button>
          )}

          {phase === 'step' ? (
            <button
              type="button"
              onClick={goNext}
              disabled={savingStep || submitting}
              className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-3 rounded-xl bg-slate-900 text-white text-sm font-semibold disabled:opacity-50 min-h-[48px]"
            >
              {savingStep ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
              {stepIndex < totalSteps - 1 ? (
                <>
                  下一步
                  <ChevronRight className="w-4 h-4" />
                </>
              ) : (
                <>
                  查看总览
                  <ChevronRight className="w-4 h-4" />
                </>
              )}
            </button>
          ) : (
            <button
              type="button"
              onClick={handleFinalSubmit}
              disabled={submitting}
              className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-3 rounded-xl bg-emerald-600 text-white text-sm font-semibold disabled:opacity-50 min-h-[48px]"
            >
              {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              {submitting ? '提交中' : '提交给顾问审核'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ==================== StepView ====================

interface StepViewProps {
  step: IntakeFlowStep;
  formValues: Record<string, string>;
  aiDrafts: PublicAISuggestResponse['drafts'];
  aiAccepted: Record<string, boolean>;
  onChange: (key: string, value: string) => void;
  onAcceptAI: (key: string) => void;
}

function StepView({ step, formValues, aiDrafts, aiAccepted, onChange, onAcceptAI }: StepViewProps) {
  return (
    <div>
      <div className="mb-5">
        <div className="text-xs text-slate-500 uppercase tracking-wide mb-1">{step.title}</div>
        <h2 className="text-lg font-semibold text-slate-900 leading-snug">
          {step.question}
        </h2>
        {step.helper && (
          <p className="text-xs text-slate-500 mt-2">{step.helper}</p>
        )}
        {step.namespace === 'social' && (
          <span className="inline-block mt-2 px-2 py-0.5 rounded text-[10px] bg-violet-50 text-violet-700 border border-violet-100">
            社媒补充
          </span>
        )}
      </div>

      <div className="space-y-4">
        {step.fields.map(f => {
          const value = formValues[f.key] || '';
          const draft = aiDrafts[f.key];
          const showAIChip = !!draft && draft.value != null && !draft.needs_more_info;
          const accepted = !!aiAccepted[f.key];

          return (
            <div key={f.key} className="bg-white rounded-xl border border-slate-200 p-4">
              <label className="block text-sm font-medium text-slate-900 mb-1">
                {f.label}
                {!f.optional && <span className="text-rose-500 ml-1">*</span>}
                {f.optional && <span className="text-slate-400 ml-1 text-xs">(可选)</span>}
              </label>
              {f.placeholder && (
                <p className="text-xs text-slate-400 mb-2">{f.placeholder}</p>
              )}

              {f.type === 'scope_radio' ? (
                <div className="grid grid-cols-3 gap-2">
                  {[
                    { v: 'local', label: '本地服务' },
                    { v: 'national', label: '全国服务' },
                    { v: 'hybrid', label: '本地 + 全国' },
                  ].map(opt => (
                    <button
                      key={opt.v}
                      type="button"
                      onClick={() => onChange(f.key, opt.v)}
                      className={`px-3 py-2.5 rounded-lg border text-sm transition min-h-[44px] ${
                        value === opt.v
                          ? 'bg-slate-900 text-white border-slate-900'
                          : 'bg-white border-slate-200 text-slate-700'
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              ) : f.type === 'textarea' ? (
                <textarea
                  value={value}
                  onChange={e => onChange(f.key, e.target.value)}
                  rows={3}
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 text-sm focus:border-slate-900 focus:outline-none"
                />
              ) : f.type === 'tag_list' ? (
                <textarea
                  value={value}
                  onChange={e => onChange(f.key, e.target.value)}
                  rows={3}
                  placeholder="一行一个, 或用顿号、逗号分隔"
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 text-sm focus:border-slate-900 focus:outline-none"
                />
              ) : (
                <input
                  type={f.type === 'tel' ? 'tel' : 'text'}
                  value={value}
                  onChange={e => onChange(f.key, e.target.value)}
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 text-sm focus:border-slate-900 focus:outline-none min-h-[44px]"
                />
              )}

              {f.prefilled && !value && (
                <p className="text-[11px] text-emerald-600 mt-1.5">
                  ✓ 我们已经知道这一条, 你可以确认或修改
                </p>
              )}

              {showAIChip && (
                <div className="mt-2.5 px-3 py-2.5 rounded-lg bg-amber-50 border border-amber-200">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 mb-1">
                        <Sparkles className="w-3 h-3 text-amber-600" />
                        <span className="text-[11px] font-medium text-amber-900">
                          AI 建议 ({draft.confidence === 'high' ? '高' : draft.confidence === 'medium' ? '中' : '弱'} 置信)
                        </span>
                      </div>
                      <div className="text-sm text-amber-900 break-words">{String(draft.value)}</div>
                    </div>
                    {accepted ? (
                      <span className="inline-flex items-center gap-1 text-[11px] text-emerald-700 shrink-0">
                        <Check className="w-3 h-3" /> 已采用
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => onAcceptAI(f.key)}
                        className="text-xs px-2.5 py-1 rounded-md bg-amber-600 text-white shrink-0 min-h-[32px]"
                      >
                        采用
                      </button>
                    )}
                  </div>
                </div>
              )}

              {!!draft && draft.needs_more_info && (
                <p className="text-[11px] text-slate-400 mt-2">
                  AI 信息不足, 建议你手动填写。
                </p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ==================== SummaryView ====================

interface SummaryViewProps {
  view: PublicIntakeView;
  formValues: Record<string, string>;
  onEdit: (stepIndex: number) => void;
}

function SummaryView({ view, formValues, onEdit }: SummaryViewProps) {
  const activeSteps = view.flow_steps.filter(s => !s.skipped);
  const skippedSteps = view.flow_steps.filter(s => s.skipped);

  return (
    <div>
      <div className="mb-5">
        <h2 className="text-lg font-semibold text-slate-900 mb-1">检查一下你填的内容</h2>
        <p className="text-xs text-slate-500">点任意一项可以返回修改, 确认无误后点底部"提交给顾问审核"。</p>
      </div>

      <div className="space-y-3">
        {activeSteps.map((step, idx) => (
          <div key={step.id} className="bg-white rounded-xl border border-slate-200 p-4">
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs text-slate-500 uppercase tracking-wide">{step.title}</div>
              <button
                type="button"
                onClick={() => onEdit(idx)}
                className="inline-flex items-center gap-1 text-xs text-slate-600 hover:text-slate-900"
              >
                <Edit2 className="w-3 h-3" />
                修改
              </button>
            </div>
            <div className="space-y-2">
              {step.fields.map(f => {
                const v = (formValues[f.key] || '').trim();
                return (
                  <div key={f.key}>
                    <div className="text-[11px] text-slate-400">{f.label}</div>
                    <div className="text-sm text-slate-800 whitespace-pre-line break-words">
                      {v || <span className="text-slate-300">未填</span>}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {skippedSteps.length > 0 && (
        <div className="mt-4 px-4 py-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs">
          ✓ 我们已经知道你的 {skippedSteps.length} 项基础信息, 自动跳过了相关问题。
        </div>
      )}
    </div>
  );
}
