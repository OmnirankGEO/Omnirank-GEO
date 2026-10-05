/**
 * PrivacyPage — 隐私政策渲染页（v3.2 Phase 3）
 *
 * 完整版在 docs/条款/隐私政策.md
 */

import { PRIVACY_VERSION } from '@/lib/legalAgreements';

export default function PrivacyPage() {
  return (
    <div className="container max-w-3xl py-6 prose prose-sm dark:prose-invert">
      <h1 className="text-xl font-bold mb-4">OmniRank AI 隐私政策</h1>
      <p className="text-xs text-muted-foreground mb-6">版本: {PRIVACY_VERSION} · 生效日期: 2026-04-16</p>

      <Section>
        <h2>一、我们收集的信息</h2>
        <ul>
          <li><strong>您主动提供</strong>：手机号、密码、昵称、品牌信息、对话内容</li>
          <li><strong>自动收集</strong>：设备信息、登录 IP、操作日志（用于安全防护）</li>
          <li><strong>第三方授权</strong>：微信 OpenID（支付/登录用）</li>
        </ul>
      </Section>

      <Section>
        <h2>二、信息使用目的</h2>
        <ul>
          <li>提供 AI 服务（生成 GEO 方案、文章等）</li>
          <li>账号管理与安全保障</li>
          <li>支付与结算</li>
          <li>基于匿名化数据改进产品</li>
        </ul>
      </Section>

      <Section>
        <h2>三、第三方共享</h2>
        <p>为提供服务，我们与以下第三方共享必要信息：</p>
        <ul>
          <li><strong>境内 LLM 供应商</strong>（阿里通义、DeepSeek、Kimi、字节豆包）：
            共享您的对话内容（去标识化）</li>
          <li><strong>境外 LLM 供应商</strong>（如 OpenAI）：仅在您明示同意后使用，
            遵守《数据出境安全评估办法》</li>
          <li><strong>支付服务商</strong>（微信支付、支付宝）：订单号和金额</li>
          <li><strong>短信服务商</strong>：您的手机号和验证码</li>
        </ul>
        <p><strong>我们绝不出售您的个人信息</strong>。</p>
      </Section>

      <Section>
        <h2>四、数据保留</h2>
        <ul>
          <li>账号基础信息：账号存续期间 + 注销后 6 个月</li>
          <li>交易记录：5 年（税务要求）</li>
          <li>生成内容：账号存续期间（最长 3 年）</li>
        </ul>
      </Section>

      <Section>
        <h2>五、您的权利（根据《个人信息保护法》）</h2>
        <ul>
          <li><strong>查询权</strong>：随时在账号设置查看您的信息</li>
          <li><strong>更正权</strong>：发现不准确时可修改</li>
          <li><strong>删除权</strong>：可申请删除信息</li>
          <li><strong>撤回授权</strong>：随时撤回之前给予的授权</li>
          <li><strong>账号注销</strong>：提交后 30 天内可撤销，30 天后永久删除</li>
        </ul>
      </Section>

      <Section>
        <h2>六、安全措施</h2>
        <ul>
          <li>全站 HTTPS 加密传输</li>
          <li>密码 bcrypt 单向加密，敏感信息 AES-256 加密</li>
          <li>严格的内部访问控制和日志审计</li>
          <li>数据泄漏应急响应机制（72 小时内通知监管机构）</li>
        </ul>
      </Section>

      <Section>
        <h2>七、未成年人保护</h2>
        <p>本服务仅面向 18 周岁及以上的成年人，不主动收集未成年人信息。</p>
      </Section>

      <div className="mt-8 pt-4 border-t text-xs text-muted-foreground">
        <p>完整文件：<code>docs/条款/隐私政策.md</code></p>
        <p>隐私投诉邮箱: <em>待补充</em> · 工作时间：工作日 9:00-18:00</p>
      </div>
    </div>
  );
}

function Section({ children }: { children: React.ReactNode }) {
  return <section className="mb-6">{children}</section>;
}
