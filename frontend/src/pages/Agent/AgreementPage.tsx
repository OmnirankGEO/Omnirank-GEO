/**
 * 服务商经营功能协议签约页
 *
 * 路由: /agent/agreement(支持 ?returnTo=/agent/xxx · 签署后自动回原页)
 * 责任: 展示协议条款 + 签 / 暂不签 + 显示当前状态
 * 未签 → 获客推广 / 客户售价 / 对外品牌 / 收益结算 等页被 AgreementGate 拦截引导回此页
 *
 * [r16/r4 2026-06-02] 经营功能协议改名「服务商经营功能协议」(区别于 partner 申请协议「服务商申请协议」)+ 责任边界定义 + 通篇服务商 → v2.3 · 不覆盖 v2.2 · 全体服务商重签
 */
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { agreementApi } from '@/lib/v35w3Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Scroll, FileSignature, X, CheckCircle, AlertTriangle, Loader2 } from 'lucide-react';
import ReactMarkdown from '@/components/SafeMarkdown';
import { toast } from 'sonner';
import {
  AGENT_OPERATING_AGREEMENT_CONTENT_HASH,
  AGENT_OPERATING_AGREEMENT_VERSION,
} from '@/lib/legalAgreements';

interface Status {
  version: string;
  status: 'unsigned' | 'signed' | 'rejected' | 'expired';
  signed_at?: string | null;
  rejected_at?: string | null;
  rejected_reason?: string | null;
  can_show_dialog: boolean;
}

const AGREEMENT_VERSION = AGENT_OPERATING_AGREEMENT_VERSION;

const AGREEMENT_TEXT = `# OmniRank 服务商经营功能协议 v2.4

本协议由 OmniRank(以下称"平台")与申请成为服务商的用户(以下称"服务商")共同遵守。服务商点击签署或继续使用服务方经营功能,即视为已阅读、理解并同意本协议全部内容。

## 一、定义
- 平台:指 OmniRank 提供的 GEO 工具与相关服务。
- 服务商:指使用平台工具采购算力库存并向其客户提供咨询、内容、监测、投放或相关服务的独立经营主体。服务商不代表平台作出承诺,不得以平台名义对客户保证效果、价格或交付结果。
- 客户:指由服务商推荐、绑定或服务的最终用户。
- 算力:指在平台内用于调用各项功能的计量单位,具体消耗以功能页面提示为准。
- 算力库存:指服务商以经营、转售或履约为目的采购并记入其库存账户的算力,不属于普通生活消费充值。
- 原订单:指页面和订单记录中列明买方、卖方、金额、算力数量、规则版本和状态的单笔交易。

## 二、合作范围
平台向服务商提供 GEO 工具能力,包括但不限于:客户资料管理、AI 体检、报价、内容写作、效果监测、报告生成、对外品牌与收益结算。服务商基于上述工具,以自身独立经营主体身份,自主开展面向其客户的经营活动。

## 三、服务商开通与使用条件
1. 服务商须按平台要求完成身份认证及相关资料提交后,方可开通服务方经营功能。
2. 服务商应妥善保管账号,对其账号下的全部操作负责。
3. 服务商使用平台功能须遵守法律法规及平台当前规则;平台规则以页面公示的当前版本为准。

## 四、客户关系与承诺边界
1. 除客户通过平台页面购买算力、支付订单等形成的平台交易记录外,服务商与其客户之间的线下服务、报价、承诺、售后及商业约定,由服务商自行建立与履行。平台不成为服务商线下服务合同的一方。
2. 平台保障工具按系统规则正常提供,但不承诺客户一定上榜、一定达标,也不承诺固定周期见效,更不承诺客户的最终成交或经营结果。
3. 服务商向客户作出的额外服务承诺、线下承诺或商业承诺,由服务商自行负责;由此引发的投诉、纠纷或责任,由服务商自行承担。
4. 服务商不是平台的法定代理人、代理商或雇员,不得以平台名义或平台代表身份对外活动,亦不得向客户作出平台未承诺的结果性保证。

## 五、客户售价与收益结算
1. 服务商可在平台允许的范围内,自主设置面向其客户的售价。
2. 服务商的经营成本、客户售价、可得收益及结算方式,均以平台当前页面、订单记录和结算规则为准;平台不在本协议中固定任何具体数值或金额。
3. 收益的产生、冻结、结算与到账时间,以平台当前结算规则和页面提示为准。
4. 每笔订单独立记录和结算。服务商仅能查看与自己直接相关的订单、库存和收益信息,无权查看与本订单无关的供应安排、其他经营主体信息或差价。

## 六、客户退款、库存退货与异常订单
1. 服务商面向普通客户销售算力时,应依法保障客户的退款权利。法定退款、重复扣款、未到账、系统故障或未按约提供服务等情形,不得由服务商任意拒绝。
2. 不属于前款强制退款情形的协商退款,由原订单所列直接服务方依据订单约定、实际履行和客户沟通结果决定;平台保留合规复核、异常风控和争议协同权。
3. 客户退款申请经依法确认后,仅回收原订单尚未消费的充值算力;赠送算力及由原订单产生的奖励按规则撤回。回收的可售算力记回原订单所列直接服务方库存,可继续用于后续独立交易。
4. 原支付渠道由平台统一接入时,服务商同意平台根据有效退款决定从该服务商待结算款、退款准备金或其他依法可扣款项中扣回退款金额,并协助原路退还客户。服务商应确保退款准备金充足;余额不足不得成为拒绝客户法定退款的理由。
5. 服务商为经营或转售目的采购的未使用库存,可按订单页公示的期限申请退货。仅对非平台过错的自愿退货,可扣除可证明且实际发生的支付、清算或订单处理成本,合计不超过该笔实付金额的百分之五。重复扣款、未到账、数量错误、系统故障、平台违约或法律规定不得收费的情形不收取该费用。
6. 库存已经消费、划拨、拆分或用于履约的,不适用普通自愿退货;依法应退款或双方另有书面约定的除外。
7. 任何退款或退货仅调整原订单所列双方的权利义务,不自动撤销其他独立订单。服务商需要处理自己的其他采购订单时,须以该独立订单另行申请,不得要求系统自动连带撤销。
8. 平台有权对疑似作弊、刷单、虚假交易、反复充值退款获取优惠或其他套利行为进行核查,撤回未实际支付对价的奖励并采取必要风控措施,但不得以风控为由规避法定退款义务。

## 七、税务与提现资料
1. 服务商应按平台要求提供真实、准确的税务与提现资料。
2. 提现门槛、税费处理、开票及到账方式,均以平台当前页面和结算规则为准。
3. 因服务商提供资料不实导致的税务或结算问题,由服务商自行承担。

## 八、内容、数据与报告
1. 平台依据多平台 AI 检测的实际结果展示数据,并按系统规则生成报告。
2. 数据反映检测时点的实际情况,可能随时间与外部环境变化。
3. 服务商及其客户应对使用平台生成内容后的对外发布行为负责。

## 九、对外品牌
1. 平台支持服务商在允许范围内配置对外品牌信息(如名称、标识等)。
2. 服务商应保证其配置的品牌信息合法合规,不侵犯第三方权益。
3. 平台有权对违规的对外品牌配置予以暂停或撤下。

## 十、禁止行为
服务商不得从事下列行为:
1. 以平台名义、平台代理人或平台代表身份对外活动,或作出平台未承诺的保证或宣传;
2. 使用违法、传销或误导性话术开展推广;
3. 进行作弊、刷单、虚假交易或套利;
4. 泄露、倒卖客户数据或平台数据;
5. 其他违反法律法规或平台规则的行为。

## 十一、协议变更、暂停与终止
1. 平台可根据业务需要调整本协议及相关规则;涉及价格、退款、库存、结算或责任主体的重大变更将要求服务商重新确认,未经确认不得对历史订单作不利追溯。
2. 服务商违反本协议或平台规则的,平台可视情节暂停或终止其服务方经营功能。
3. 协议终止后,服务商已产生的合规收益按平台当前结算规则处理。

## 十二、争议处理
1. 因本协议引起的争议,双方应先友好协商解决。
2. 协商不成的,按平台当前规则及相关法律法规处理。
3. 本协议的解释与适用以中华人民共和国法律及服务商签署时的协议版本为准。格式条款存在两种以上解释的,依法处理。
`;

const AGREEMENT_HASH = AGENT_OPERATING_AGREEMENT_CONTENT_HASH;

export default function AgreementPage() {
  const navigate = useEmbeddedNavigate();
  const [searchParams] = useSearchParams();
  // 仅接受单斜杠站内路径(含 query/hash)· 拒绝 //host 协议相对 + /\ 反斜杠变体(防开放重定向)
  const rawReturnTo = searchParams.get('returnTo') || '';
  const returnTo =
    rawReturnTo.startsWith('/') && !rawReturnTo.startsWith('//') && !rawReturnTo.startsWith('/\\')
      ? rawReturnTo
      : '';
  const [status, setStatus] = useState<Status | null>(null);
  const [loading, setLoading] = useState(true);
  const [signing, setSigning] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);

  const reload = async () => {
    setLoading(true);
    try {
      const r = await agreementApi.status();
      setStatus(r as Status);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'agent'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { reload(); }, []);

  const onSign = async () => {
    if (signing) return;
    setSigning(true);
    try {
      await agreementApi.sign({ version: AGREEMENT_VERSION, content_hash: AGREEMENT_HASH });
      toast.success('已签署,可以使用服务方经营功能了。');
      // 带合法 returnTo 时签署成功自动回原页(保留 query/hash);否则刷新本页状态
      if (returnTo) {
        navigate(returnTo);
      } else {
        await reload();
      }
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '签署失败 · 请重试', 'agent'));
    } finally {
      setSigning(false);
    }
  };

  const isSigned = status?.status === 'signed';

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-4xl">
      <div className="flex items-center gap-2">
        <FileSignature className="w-6 h-6" />
        <h1 className="text-2xl font-bold">服务商经营功能协议</h1>
      </div>

      {/* 状态卡 */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle className="text-base">当前协议 · {AGREEMENT_VERSION}</CardTitle>
            {loading ? (
              <p className="text-xs text-muted-foreground mt-1 flex items-center gap-1.5">
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> 正在检查协议状态…
              </p>
            ) : isSigned ? (
              <p className="text-xs text-muted-foreground mt-1">
                你已签署服务商经营功能协议,可以正常使用服务方经营功能。
              </p>
            ) : (
              <p className="text-xs text-muted-foreground mt-1">
                签署后可使用获客推广、客户售价、对外品牌和收益结算。签署前不会产生费用。
              </p>
            )}
          </div>
          {!loading && (
            isSigned ? (
              <Badge className="bg-green-600"><CheckCircle className="w-4 h-4 mr-1" />已签署</Badge>
            ) : status?.status === 'rejected' ? (
              <Badge variant="secondary"><X className="w-4 h-4 mr-1" />已暂不签</Badge>
            ) : status?.status === 'expired' ? (
              <Badge variant="destructive">已过期 · 请重新签署</Badge>
            ) : (
              <Badge variant="outline"><AlertTriangle className="w-4 h-4 mr-1" />未签署</Badge>
            )
          )}
        </CardHeader>
        {!loading && (isSigned || status?.status === 'rejected') && (
          <CardContent>
            {isSigned && (
              <p className="text-sm text-muted-foreground">
                签署版本:{status?.version || AGREEMENT_VERSION}
                {status?.signed_at ? ` · 签署时间:${new Date(status.signed_at).toLocaleString()}` : ''}
              </p>
            )}
            {status?.status === 'rejected' && (
              <p className="text-sm text-muted-foreground">
                暂不签时间:{status.rejected_at ? new Date(status.rejected_at).toLocaleString() : '-'}
                {status.rejected_reason && ` · 原因:${status.rejected_reason}`}
                <br />30 天后可重新签署
              </p>
            )}
          </CardContent>
        )}
      </Card>

      {/* 协议正文 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Scroll className="w-4 h-4" /> 协议正文
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="prose prose-sm dark:prose-invert max-w-none max-h-96 overflow-auto rounded bg-muted/30 p-4 prose-headings:font-bold prose-h1:text-lg prose-h1:mb-2 prose-h2:text-base prose-h2:mt-4 prose-h2:mb-1 prose-p:leading-relaxed prose-li:leading-relaxed prose-ul:my-1 prose-ol:my-1">
            <ReactMarkdown>{AGREEMENT_TEXT}</ReactMarkdown>
          </div>
        </CardContent>
      </Card>

      {/* 操作区 */}
      {!loading && !isSigned && (
        <div className="flex gap-3 justify-end">
          <Dialog open={rejectOpen} onOpenChange={setRejectOpen}>
            <DialogTrigger asChild>
              <Button variant="outline" disabled={signing}>暂不签</Button>
            </DialogTrigger>
            <RejectDialog onDone={() => { setRejectOpen(false); reload(); }} />
          </Dialog>
          <Button onClick={onSign} disabled={signing}>
            {signing ? <><Loader2 className="w-4 h-4 mr-1.5 animate-spin" />签署中…</> : '我已阅读并同意,签署'}
          </Button>
        </div>
      )}
    </div>
  );
}

function RejectDialog({ onDone }: { onDone: () => void }) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <DialogContent>
      <DialogHeader><DialogTitle>暂不签 · 30 天冷静期</DialogTitle></DialogHeader>
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">
          30 天内不再主动弹出签约提醒;如需使用服务方经营功能(获客推广 / 客户售价 / 对外品牌 / 收益结算),仍需先签署协议。历史提现不受影响。
        </p>
        <div>
          <Label>反馈原因(选填)</Label>
          <Input value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
      </div>
      <DialogFooter>
        <Button variant="destructive" onClick={async () => {
          setBusy(true);
          try {
            await agreementApi.reject({ version: AGREEMENT_VERSION, reason });
            toast.success('已记录 · 30 天后可重新签署');
            onDone();
          } catch (e: any) {
            toast.error(formatApiErrorForDisplay(e, '操作失败 · 请重试', 'agent'));
          } finally { setBusy(false); }
        }} disabled={busy}>确认暂不签</Button>
      </DialogFooter>
    </DialogContent>
  );
}
