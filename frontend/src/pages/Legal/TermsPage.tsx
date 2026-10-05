/**
 * TermsPage — 用户服务协议渲染页（v3.2 Phase 3）
 *
 * 用户注册时从勾选处跳转到这里（embedded=true 无 Sidebar）
 * 完整版协议文件在 docs/条款/用户服务协议.md，待律师审核定稿后替换
 */

import { USER_TERMS_VERSION } from '@/lib/legalAgreements';

export default function TermsPage() {
  return (
    <div className="container max-w-3xl py-6 prose prose-sm dark:prose-invert">
      <h1 className="text-xl font-bold mb-4">OmniRank AI 用户服务协议</h1>
      <p className="text-xs text-muted-foreground mb-6">版本: {USER_TERMS_VERSION} · 发布日期: 2026-07-15</p>

      <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 mb-6 text-xs">
        <strong>重要提示：</strong>算力充值属于预付服务安排。请重点阅读充值退款、
        AI 生成内容、责任主体和争议处理条款。依法不得排除的消费者权利不因本协议而被排除。
      </div>

      <Section id="定义">
        <h2>一、定义</h2>
        <ul>
          <li><strong>本服务</strong>：OmniRank AI 平台提供的所有功能</li>
          <li><strong>算力</strong>：平台内用于调用功能的预付型服务计量单位，分为充值算力和赠送算力</li>
          <li><strong>AI 生成内容</strong>：通过本平台 AI 功能产出的文字、图像等内容</li>
        </ul>
      </Section>

      <Section id="账号">
        <h2>二、账号注册与使用</h2>
        <p>您承诺年满 18 周岁、提供真实信息、妥善保管密码。账号仅限本人使用，不得转让。</p>
      </Section>

      <Section id="积分">
        <h2>三、算力充值与退款 ⚠️</h2>
        <p><strong>算力充值与计费：</strong></p>
        <ul>
          <li>订单提交前展示实付金额、充值算力、赠送算力、订单责任服务方及适用规则。</li>
          <li>任务未执行、重复扣款、未到账、系统错误或者未按约提供服务时，平台和订单责任服务方应依法纠正、补足或退款。</li>
          <li>已实际完成并交付的服务对应算力不重复退还；赠送算力和由原订单产生的奖励在退款时按规则撤回，不折算现金。</li>
        </ul>

        <p><strong>七日规则：</strong>对依法适用预付式消费七日无理由退款的订单，用户可自支付预付款之日起七日内提出申请；
          用户订立合同时已经从本经营者或其他经营者处获得过相同商品或服务的，依法不适用该项七日无理由规则。
          依法适用的消费者退款不预扣固定百分之五费用。</p>
        <p><strong>责任与办理：</strong>退款只调整客户与原订单所列直属服务方之间的该笔订单，不自动撤销任何上游或其他独立订单。
          法定退款、重复扣款、未到账、未交付及系统故障不得由服务方任意拒绝；其他协商退款由直属服务方依据订单约定和实际履行审核。
          退款成立后，平台按原支付路径执行退款，冻结并回收未消费付费算力、撤回赠送权益，回收算力记回直属服务方可售库存，
          并按证据状态幂等冲销该订单收益。服务方结算余额不足不影响依法成立的客户退款。</p>
        <p>平台可以核验订单、消费流水、支付回调和异常风险，但不得以风险审核为由无正当理由拖延法定退款。
          退款原则上退回原支付渠道，用户不得要求退至无关账户或通过退款套现。</p>
      </Section>

      <Section id="责任主体">
        <h2>四、订单责任服务方</h2>
        <p>本次交易的服务责任主体和售后信息以订单页面、支付凭证及依法公示的信息为准。
          客户无需了解与本订单无关的上游关系、层级、成本、倍率或差价，也无需向无关主体主张退款。</p>
      </Section>

      <Section id="AI">
        <h2>五、AI 生成内容责任分配 ⚠️</h2>
        <p>平台根据《生成式人工智能服务管理暂行办法》第 10 条，
          在每段 AI 生成内容处明确标注"AI 生成，请自行核实"。</p>
        <p><strong>您是 AI 生成内容的唯一发布者和责任人</strong>。
          因您发布的内容引发的投诉、处罚、诉讼由您全部承担。</p>
      </Section>

      <Section id="推广">
        <h2>六、推广与收益</h2>
        <ul>
          <li>所有用户注册后获得个人推荐码</li>
          <li>推广奖励和服务收益按平台当前规则执行，具体比例与到账方式<strong>以后台展示为准</strong></li>
          <li>推广收益基于<strong>被邀请人实际充值</strong>，经观察期后结算</li>
          <li>服务商须使用平台合规话术，不得使用"加盟/躺赚/稳赚"等违法用语</li>
        </ul>
      </Section>

      <Section id="免责">
        <h2>七、免责声明</h2>
        <ul>
          <li>不可抗力（战争、自然灾害）导致的服务中断</li>
          <li>第三方服务（LLM、支付通道）故障</li>
          <li>AI 生成内容不准确造成的间接损失</li>
          <li>平台<strong>不保证</strong>任何具体的排名、曝光、转化效果</li>
          <li>
            就经系统判定为<strong>超饱和</strong>（搜索竞争对手占比≥90%）之关键词，因其所处市场竞争密度已超出常规可测与可控范围，
            本平台所提供之工具及内容投放服务，<strong>不对该等关键词之 AI 出现率、上榜位次或达标结果作任何明示或默示之保证</strong>；
            用户经明确知悉并确认后仍坚持纳入报价的，由此产生效果不达预期之后果由其自行承担。
          </li>
        </ul>
        <p>赔偿限额：累计不超过您过去 12 个月支付的费用总额。</p>
      </Section>

      <Section id="管辖">
        <h2>八、争议解决</h2>
        <p>协商不成的，由<strong>原告所在地</strong>有管辖权的人民法院审理。</p>
        <p>适用中华人民共和国法律（不含港澳台）。</p>
      </Section>

      <div className="mt-8 pt-4 border-t text-xs text-muted-foreground">
        <p>完整条款文件：<code>docs/条款/用户服务协议.md</code></p>
        <p>客服邮箱: <em>待补充</em></p>
        <p className="mt-2">本协议由律师审核定稿，若有疑问请联系客服。</p>
      </div>
    </div>
  );
}

function Section({ id, children }: { id: string; children: React.ReactNode }) {
  return <section id={id} className="mb-6">{children}</section>;
}
