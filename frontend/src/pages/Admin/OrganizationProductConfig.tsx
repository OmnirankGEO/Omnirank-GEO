import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Loader2, ShieldCheck } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  OrganizationApiError,
  organizationAdminRequest,
  requestId,
} from '@/lib/organizationApi';

interface ProductConfig {
  publication_version: number;
  version_code: string;
  included_seats: number;
  extra_seat_price_cents: 0;
  paid_extra_seats_enabled: false;
  operational: boolean;
  invite_ttl_hours: number;
  verification_ttl_minutes: number;
  verification_max_attempts: number;
  config_hash: string;
  created_at: string;
}

function errorMessage(error: unknown): string {
  if (error instanceof OrganizationApiError) return error.detail.message;
  return error instanceof Error ? error.message : '团队席位策略读取失败';
}

export default function OrganizationProductConfig() {
  const generationRef = useRef(0);
  const requestRef = useRef(requestId('publish-organization-seat-policy'));
  const [config, setConfig] = useState<ProductConfig | null>(null);
  const [includedSeats, setIncludedSeats] = useState('');
  const [inviteHours, setInviteHours] = useState('72');
  const [verificationMinutes, setVerificationMinutes] = useState('10');
  const [verificationAttempts, setVerificationAttempts] = useState('5');
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    const generation = ++generationRef.current;
    setLoading(true);
    setError(null);
    try {
      const next = await organizationAdminRequest<ProductConfig>('');
      if (generation !== generationRef.current) return;
      setConfig(next);
      setIncludedSeats(String(Math.max(next.included_seats, 1)));
      setInviteHours(String(next.invite_ttl_hours));
      setVerificationMinutes(String(next.verification_ttl_minutes));
      setVerificationAttempts(String(next.verification_max_attempts));
    } catch (caught) {
      if (generation === generationRef.current) setError(errorMessage(caught));
    } finally {
      if (generation === generationRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    return () => { generationRef.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const publish = async () => {
    if (!config || saving) return;
    const generation = ++generationRef.current;
    setSaving(true);
    setError(null);
    try {
      const next = await organizationAdminRequest<ProductConfig>('/publish', {
        method: 'POST',
        body: JSON.stringify({
          request_id: requestRef.current,
          expected_version: config.publication_version,
          included_seats: Number(includedSeats),
          invite_ttl_hours: Number(inviteHours),
          verification_ttl_minutes: Number(verificationMinutes),
          verification_max_attempts: Number(verificationAttempts),
          reason: reason.trim(),
        }),
      });
      if (generation !== generationRef.current) return;
      setConfig(next);
      setReason('');
      requestRef.current = requestId('publish-organization-seat-policy');
    } catch (caught) {
      if (generation === generationRef.current) setError(errorMessage(caught));
    } finally {
      if (generation === generationRef.current) setSaving(false);
    }
  };

  return (
    <main className="mx-auto w-full max-w-4xl space-y-5 p-4 md:p-8" data-testid="organization-product-config">
      <div>
        <h1 className="text-2xl font-semibold">团队基础席位策略</h1>
        <p className="mt-1 text-sm text-muted-foreground">这里发布版本化、可审计的免费基础员工席位。额外付费席位未签发，不能在此定价。</p>
      </div>

      {loading ? (
        <Card><CardContent className="grid min-h-44 place-items-center"><Loader2 className="h-6 w-6 animate-spin" /></CardContent></Card>
      ) : config ? (
        <>
          <Card>
            <CardHeader>
              <CardTitle>当前版本：{config.version_code}</CardTitle>
              <CardDescription>{config.operational ? '已发布可用基础席位' : '治理态：用户可创建团队，但尚不能邀请员工'}</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 text-sm sm:grid-cols-2">
              <div>发布代际：{config.publication_version}</div>
              <div>基础员工席位：{config.included_seats}</div>
              <div>额外席位价格：未签发（固定关闭）</div>
              <div className="break-all">配置哈希：{config.config_hash}</div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle>发布新版本</CardTitle><CardDescription>发布会以 CAS 推进版本，并为现有团队更新免费 entitlement；不会写钱包或产生扣费。</CardDescription></CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div><Label htmlFor="included-seats">每个团队的基础员工席位</Label><Input id="included-seats" type="number" min={1} max={10000} value={includedSeats} onChange={event => setIncludedSeats(event.target.value)} /></div>
                <div><Label htmlFor="invite-hours">邀请有效小时数</Label><Input id="invite-hours" type="number" min={1} max={720} value={inviteHours} onChange={event => setInviteHours(event.target.value)} /></div>
                <div><Label htmlFor="verify-minutes">验证码有效分钟数</Label><Input id="verify-minutes" type="number" min={5} max={60} value={verificationMinutes} onChange={event => setVerificationMinutes(event.target.value)} /></div>
                <div><Label htmlFor="verify-attempts">验证码最多尝试次数</Label><Input id="verify-attempts" type="number" min={3} max={10} value={verificationAttempts} onChange={event => setVerificationAttempts(event.target.value)} /></div>
              </div>
              <div><Label htmlFor="publish-reason">发布原因</Label><Input id="publish-reason" maxLength={500} value={reason} onChange={event => setReason(event.target.value)} placeholder="例如：Owner 批准面向全部激活账号开放 3 个基础员工席位" /></div>
              <Button disabled={saving || !reason.trim() || Number(includedSeats) < 1} onClick={() => void publish()}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}发布基础席位策略</Button>
            </CardContent>
          </Card>
        </>
      ) : null}

      {error && <div role="alert" className="flex items-start gap-2 rounded-lg border border-destructive/35 bg-destructive/5 p-4 text-sm text-destructive"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />{error}</div>}
      <p className="flex items-start gap-1.5 text-xs leading-5 text-muted-foreground"><ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />此页面不能打开付费额外席位，也不能修改价格、钱包、推荐、分佣或客户商业归属。</p>
    </main>
  );
}
