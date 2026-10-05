/**
 * 合规推广话术模板 + 禁词检测（v3.2 Phase 3）
 *
 * 代理推广内容时的合规防线：
 *   1. 提供安全话术模板，代理可直接复制
 *   2. 禁词清单：用户自定义文案时前端 warning，后端可拦截
 *
 * 法律依据：
 *   - 《禁止传销条例》第 7 条（严禁"拉人头"话术）
 *   - 《广告法》第 9 条（禁用绝对化用语）
 *   - 《互联网广告管理办法》2023 版
 *   - 《反不正当竞争法》
 */

// ============================================
// 合规话术模板（代理可直接复制使用）
// ============================================

export interface PromoTemplate {
  id: string;
  scene: string;       // 使用场景
  title: string;       // 标题
  text: string;        // 文案（{link} 会被替换为推广链接）
  description?: string;
}

export const COMPLIANT_PROMO_TEMPLATES: PromoTemplate[] = [
  {
    id: 'wechat_moment',
    scene: '朋友圈',
    title: '温和推荐型',
    text: '分享一个好用的 AI 工具，能帮品牌在 AI 搜索引擎里被推荐。\n用我的链接注册送体验算力 → {link}',
    description: '适合个人朋友圈发，不含任何销售压力',
  },
  {
    id: 'wechat_group',
    scene: '微信群',
    title: '同行推荐型',
    text: '最近在用一个 GEO 优化工具，能让品牌在豆包/Kimi/DeepSeek 里被推荐。效果确实不错，分享给同行 → {link}',
    description: '适合行业群、运营群',
  },
  {
    id: 'xiaohongshu',
    scene: '小红书',
    title: '干货分享型',
    text: 'AI 搜索时代怎么让自己的品牌被推荐？我最近发现一个专门做这个的工具，可以免费试试 → {link}\n#AI搜索 #GEO优化 #品牌营销',
    description: '适合小红书笔记',
  },
  {
    id: 'douyin_caption',
    scene: '抖音简介',
    title: '个人简介型',
    text: '🌟 GEO 优化 · AI 搜索推荐\n📊 让品牌在豆包/Kimi 被看见\n👉 点击链接免费体验 {link}',
    description: '适合抖音/小红书主页简介',
  },
  {
    id: 'business_referral',
    scene: '商务对话',
    title: '专业介绍型',
    text: '您好，我是 OmniRank AI 的合作伙伴。我们专做 AI 搜索引擎优化（GEO），帮品牌在 DeepSeek、Kimi、豆包等 AI 搜索中被推荐。可以免费做个品牌体检，看看现状。\n\n如果需要，可以通过我的链接注册体验 → {link}',
    description: '适合 B 端客户沟通',
  },
  {
    id: 'one_liner',
    scene: '一句话',
    title: '极简型',
    text: '推荐个好用的品牌工具 👉 {link}',
    description: '最简单的分享，适合任何场景',
  },
];

// ============================================
// 禁词清单（违反《广告法》《禁止传销条例》）
// ============================================

/** 严禁词汇（前端 warning + 后端拦截） */
export const FORBIDDEN_WORDS = [
  // 涉传销类
  '加盟', '加盟费', '代理费', '入门费', '发展下线', '招代理', '建团队',
  '团队分红', '团队业绩奖', '三级返利', '层级奖',

  // 涉金融诱导类
  '躺赚', '躺赢', '睡后收入', '被动收入', '财务自由', '一夜暴富',
  '暴富神话', '翻倍增长', '日入过万',

  // 涉保证收益类
  '必赚钱', '稳赚不赔', '无风险', '零风险', '100%回报',
  '保证收益', '包赚', '稳赚',

  // 涉广告法绝对化用语
  '最低价', '第一', '唯一', '最好', '顶级', '全国第一',
  '行业龙头', '最优秀', '最强',
];

/** 提示警告（轻度违规，只提示不拦截） */
export const WARNING_WORDS = [
  '火爆', '疯狂', '爆款', '神器',
  '独家', '首创', '开创',
];

// ============================================
// 检测函数
// ============================================

export interface ComplianceCheckResult {
  safe: boolean;
  forbidden: string[];   // 触发严禁词
  warnings: string[];    // 触发警告词
}

/** 检测文案是否有禁词 */
export function checkCompliance(text: string): ComplianceCheckResult {
  const forbidden: string[] = [];
  const warnings: string[] = [];

  for (const word of FORBIDDEN_WORDS) {
    if (text.includes(word)) forbidden.push(word);
  }

  for (const word of WARNING_WORDS) {
    if (text.includes(word)) warnings.push(word);
  }

  return {
    safe: forbidden.length === 0,
    forbidden,
    warnings,
  };
}

/** 尝试自动替换禁词为合规替代（简化版） */
const REPLACEMENTS: Record<string, string> = {
  '加盟': '合作',
  '加盟费': '',
  '代理费': '',
  '发展下线': '推荐朋友',
  '建团队': '建立合作网络',
  '团队分红': '团队辅导奖',
  '躺赚': '长期收益',
  '躺赢': '稳定收益',
  '睡后收入': '长期收益',
  '财务自由': '增加收入',
  '必赚钱': '带来收益',
  '稳赚不赔': '合理收益',
  '无风险': '风险可控',
  '100%': '较高',
  '最好': '优秀',
  '第一': '领先',
  '唯一': '独特',
};

export function suggestReplacement(text: string): string {
  let result = text;
  for (const [bad, good] of Object.entries(REPLACEMENTS)) {
    result = result.split(bad).join(good);
  }
  return result;
}
