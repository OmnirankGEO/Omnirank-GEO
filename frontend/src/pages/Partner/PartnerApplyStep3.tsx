/**
 * PartnerApplyStep3 — 代理申请 Step 3：签协议 + 提交
 *
 * - 拉协议全文（markdown 文本）滚动阅读
 * - 11 项承诺勾选（必须全部勾）
 * - 输入"真实姓名"作为电子签名（必须与 Step1 填写一致）
 * - POST /api/partner/apply 提交
 * - 成功后根据 status 跳转（approved → status 页 ok / manual_review → status 页 pending）
 */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, CheckCircle2, Loader2, AlertTriangle } from 'lucide-react';
import ReactMarkdown from '@/components/SafeMarkdown';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { authFetch } from '@/lib/api';
import { readDraft, writeDraft, clearDraft, type PartnerDraft } from './draftStorage';
import { StepIndicator } from './PartnerApplyStep1';

const COMMITMENTS = [
  '我已年满 18 周岁，具有完全民事行为能力（平台将从身份证号自动核验）',
  '我自愿申请成为 OmniRank AI 服务商，非受任何一方强迫或诱导。我理解服务商不代表平台对外作出承诺，不得以平台名义对客户保证效果、价格或交付结果。',
  '我理解 1 个身份证仅可绑定 1 个服务商账号，不存在冒用他人身份或协助他人冒用的情形',
  '我理解本协议不构成劳动/劳务/雇佣关系，我自担经营风险',
  '我承诺严格遵守合规推广话术，不使用任何违法、传销话术',
  '我承诺不向客户作平台未授权的承诺，因此产生的纠纷由我承担',
  '我理解收益基于真实消费，作弊、刷单、虚假交易套利将面临封号和追责',
  '我理解我与客户之间的任何线下交易与平台无关',
  '我已阅读《用户服务协议》《隐私政策》并同意其内容',
];

// 敏感个人信息单独同意 (《个保法》第 28-29 条强制要求)
const SENSITIVE_CONSENT_TEXT =
  '我单独同意：平台按本协议第十三章及《隐私政策》的约定，收集、加密存储、处理我的身份证号、身份证正反面照片（及可选手持照片）、OCR 识别结果等敏感个人信息，用于服务商身份审核、反套利核查与电子签约证据留存。我理解上述处理对我的权益可能具有重大影响，我已在充分了解必要性后单独、明确作出本同意。';

const AGREEMENT_VERSION = 'v2.3';

export default function PartnerApplyStep3() {
  const navigate = useNavigate();
  const [draft, setDraft] = useState<PartnerDraft>(() => readDraft());
  const [agreementText, setAgreementText] = useState<string>('');
  const [agreementLoading, setAgreementLoading] = useState(true);
  const [agreementFailed, setAgreementFailed] = useState(false);
  const [checked, setChecked] = useState<boolean[]>(() => COMMITMENTS.map(() => false));
  const [consentSensitive, setConsentSensitive] = useState(false);
  const [signedName, setSignedName] = useState('');
  const [smsCode, setSmsCode] = useState('');
  const [smsSending, setSmsSending] = useState(false);
  const [smsCooldown, setSmsCooldown] = useState(0); // 倒计时秒
  const [smsPhoneMask, setSmsPhoneMask] = useState<string | null>(null);
  const [smsError, setSmsError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitResult, setSubmitResult] = useState<{ status: string; message: string } | null>(null);

  // 倒计时 tick
  useEffect(() => {
    if (smsCooldown <= 0) return;
    const t = setTimeout(() => setSmsCooldown(c => c - 1), 1000);
    return () => clearTimeout(t);
  }, [smsCooldown]);

  // 前置校验: 必填字段齐
  useEffect(() => {
    if (!draft.real_name || !draft.id_card_no ||
        !draft.front_upload?.oss_key || !draft.back_upload?.oss_key) {
      navigate('/partner/apply');
    }
  }, [draft, navigate]);

  // 拉协议全文
  // 🔴 [WO_268 §4] 拉不到 / 拉回来是空的 ⇒ fail-closed:明说加载失败,且不许提交。
  //    原来 resp.ok 为假就静默不设正文、canSubmit 也不看协议 ⇒ 用户在空白协议框下
  //    照样能点提交,提交才 404(07-12 起生产镜像里没有协议文件)。
  useEffect(() => {
    (async () => {
      try {
        const resp = await authFetch(`/api/partner/agreement/${AGREEMENT_VERSION}`);
        const data = resp.ok ? await resp.json().catch(() => null) : null;
        const text = typeof data?.text === 'string' ? data.text : '';
        setAgreementText(text);
        setAgreementFailed(!text.trim());
      } catch {
        setAgreementFailed(true);
      } finally {
        setAgreementLoading(false);
      }
    })();
  }, []);

  const agreementReady = !agreementLoading && !agreementFailed;
  const allChecked = checked.every(Boolean);
  const nameMatch = signedName.trim() === draft.real_name.trim();
  const smsCodeValid = /^\d{4,8}$/.test(smsCode.trim());
  const canSubmit =
    agreementReady && allChecked && consentSensitive && nameMatch && smsCodeValid && !submitting;

  const handleSendSms = async () => {
    if (smsCooldown > 0 || smsSending) return;
    setSmsError(null);
    setSmsSending(true);
    try {
      const resp = await authFetch('/api/partner/apply/send-sms', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}), // 默认用账号绑定手机
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setSmsError((data as { detail?: string })?.detail || '发送失败');
        return;
      }
      setSmsPhoneMask((data as { phone_mask?: string }).phone_mask || null);
      setSmsCooldown(60); // 60 秒后可重发
    } catch (e) {
      setSmsError((e as Error)?.message || '网络错误');
    } finally {
      setSmsSending(false);
    }
  };

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitError(null);
    setSubmitting(true);

    try {
      const resp = await authFetch('/api/partner/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          real_name: draft.real_name,
          id_card_no: draft.id_card_no,
          id_card_front_key: draft.front_upload!.oss_key,
          id_card_back_key: draft.back_upload!.oss_key,
          id_card_selfie_key: draft.selfie_upload?.oss_key,
          promotion_scenes: draft.promotion_scenes,
          expected_monthly_customers: draft.expected_monthly_customers,
          remark: draft.remark,
          agreement_version: AGREEMENT_VERSION,
          signed_name: signedName.trim(),
          sms_code: smsCode.trim(),
          consent_sensitive_info: consentSensitive,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        setSubmitError((err as { detail?: string })?.detail || '提交失败');
        return;
      }

      const data = await resp.json() as { status: string; message: string };
      clearDraft();
      writeDraft({ ...draft }); // 确保 session 里 cleared
      setSubmitResult(data);
    } catch (e) {
      setSubmitError((e as Error)?.message || '网络错误');
    } finally {
      setSubmitting(false);
    }
  };

  if (submitResult) {
    const ok = submitResult.status === 'approved';
    return (
      <div className="max-w-xl mx-auto p-6">
        <Card className={ok ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-amber-500/30 bg-amber-500/5'}>
          <CardContent className="py-8 text-center space-y-4">
            {ok ? (
              <>
                <CheckCircle2 className="h-12 w-12 text-emerald-400 mx-auto" />
                <h2 className="text-lg font-semibold">服务商身份已激活</h2>
                <p className="text-sm text-muted-foreground">
                  AI 自动审核通过，您已成为服务商。
                </p>
              </>
            ) : (
              <>
                <Loader2 className="h-12 w-12 text-amber-400 mx-auto animate-spin" />
                <h2 className="text-lg font-semibold">申请已提交</h2>
                <p className="text-sm text-muted-foreground">{submitResult.message}</p>
              </>
            )}
            <div className="flex gap-2 justify-center pt-2">
              <Button onClick={() => navigate('/partner/status')}>查看状态</Button>
              <Button variant="outline" onClick={() => navigate('/')}>返回首页</Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto p-6 space-y-6">
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={() => navigate('/partner/apply/id-card')}>
          <ArrowLeft className="h-4 w-4 mr-1" />
          返回
        </Button>
        <div className="flex-1">
          <h1 className="text-lg font-semibold">服务商申请 · 第 3 步 / 共 3 步</h1>
          <p className="text-xs text-muted-foreground">阅读协议、勾选承诺、电子签名提交</p>
        </div>
      </div>

      <StepIndicator current={3} />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">《服务商申请协议 {AGREEMENT_VERSION}》</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="max-h-80 overflow-auto border border-border rounded-md p-3 bg-muted/10 text-xs prose prose-sm dark:prose-invert max-w-none">
            {agreementLoading ? (
              <div className="text-center text-muted-foreground py-8">
                <Loader2 className="h-4 w-4 animate-spin inline mr-2" />
                加载协议…
              </div>
            ) : agreementFailed ? (
              <div role="alert" data-testid="partner-agreement-failed"
                className="flex items-center justify-center gap-2 py-8 text-red-500">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                协议加载失败，请刷新或联系客服
              </div>
            ) : (
              <ReactMarkdown>{agreementText}</ReactMarkdown>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">一般承诺（整体勾选确认）</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {COMMITMENTS.map((text, i) => (
            <label
              key={i}
              className="flex items-start gap-2 p-2 rounded-md hover:bg-muted/30 cursor-pointer"
            >
              <input
                type="checkbox"
                checked={checked[i]}
                onChange={() => {
                  const next = [...checked];
                  next[i] = !next[i];
                  setChecked(next);
                }}
                className="accent-foreground mt-0.5"
              />
              <span className="text-xs text-foreground">{text}</span>
            </label>
          ))}
        </CardContent>
      </Card>

      {/* 敏感个人信息单独同意（《个保法》28-29 条强制要求，独立区块展示） */}
      <Card className="border-amber-500/30 bg-amber-500/5">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-amber-400" />
            敏感个人信息处理 · 单独同意
          </CardTitle>
        </CardHeader>
        <CardContent>
          <label className="flex items-start gap-2 p-2 rounded-md hover:bg-muted/30 cursor-pointer">
            <input
              type="checkbox"
              checked={consentSensitive}
              onChange={() => setConsentSensitive(v => !v)}
              className="accent-foreground mt-0.5"
            />
            <span className="text-xs text-foreground">{SENSITIVE_CONSENT_TEXT}</span>
          </label>
          <p className="text-[10px] text-muted-foreground mt-2 pl-6">
            依据《个人信息保护法》第 28-29 条，处理身份证号等敏感个人信息须经您单独同意。
            如您拒绝本项同意，平台将无法完成服务商审核，但不影响您的普通用户权益。
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">电子签名（双重验证）</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-xs text-muted-foreground">
            根据《中华人民共和国电子签名法》第 13-14 条可靠电子签名要件，请依次完成以下两步。
            签署信息（姓名 + 短信验证通过记录 + 服务器时间戳 + IP + 设备指纹 + 协议文本哈希）
            将一并作为签约证据永久留存。
          </p>

          {/* 第 1 步：签名 */}
          <div className="space-y-1">
            <label className="text-xs font-medium">第 1 步 · 输入真实姓名作为电子签名</label>
            <Input
              value={signedName}
              onChange={e => setSignedName(e.target.value)}
              placeholder={`请输入：${draft.real_name || '（真实姓名）'}`}
              className="max-w-xs"
            />
            {signedName && !nameMatch && (
              <p className="text-xs text-red-400">签名需与真实姓名完全一致</p>
            )}
          </div>

          {/* 第 2 步：短信验证码 */}
          <div className="space-y-1">
            <label className="text-xs font-medium">
              第 2 步 · 手机短信验证码
              {smsPhoneMask && (
                <span className="text-muted-foreground ml-2">（已发送至 {smsPhoneMask}）</span>
              )}
            </label>
            <div className="flex gap-2 max-w-xs">
              <Input
                value={smsCode}
                onChange={e => setSmsCode(e.target.value.replace(/\D/g, '').slice(0, 8))}
                placeholder="输入短信中的验证码"
                className="flex-1"
                inputMode="numeric"
              />
              <Button
                variant="outline"
                onClick={handleSendSms}
                disabled={smsCooldown > 0 || smsSending}
                className="whitespace-nowrap"
              >
                {smsSending ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : smsCooldown > 0 ? (
                  `${smsCooldown} s`
                ) : smsPhoneMask ? (
                  '重新发送'
                ) : (
                  '发送验证码'
                )}
              </Button>
            </div>
            {smsError && (
              <p className="text-xs text-red-400">{smsError}</p>
            )}
            <p className="text-[10px] text-muted-foreground">
              验证码发送至您账号绑定的手机号，有效期 5 分钟，错误 3 次失效。
            </p>
          </div>
        </CardContent>
      </Card>

      {submitError && (
        <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/5 p-3">
          <AlertTriangle className="h-4 w-4 text-red-400 mt-0.5 shrink-0" />
          <div className="text-xs text-foreground">{submitError}</div>
        </div>
      )}

      <div className="flex justify-between gap-3">
        <Button variant="outline" onClick={() => navigate('/partner/apply/id-card')}>
          上一步
        </Button>
        <Button onClick={handleSubmit} disabled={!canSubmit}>
          {submitting ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
              提交中…
            </>
          ) : (
            '确认签署并提交申请'
          )}
        </Button>
      </div>
    </div>
  );
}
