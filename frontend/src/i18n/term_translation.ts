/**
 * 工程术语翻译表 · CTO-15.18 PM 干预 B.6
 *
 * 老板红线(2026-04-28):
 * - 40 岁女销售视角:看到工程术语就劝退
 * - "stage" "token" "draft" "M1c" "SEM" "净化测度" "qwen3-max" "quote" "brand" "portal" 等暴露在 UI
 * - 全站翻译人话(Q12 老板裁决:改前端可见 + 不改后端日志/audit)
 *
 * 用法:
 *   import { translateTerm, T } from '@/i18n/term_translation';
 *   <span>{T.stage}</span>            // "阶段"
 *   <span>{translateTerm('quote')}</span>  // "报价单"
 *
 * 不翻译的场景(代理后台调试 / admin):
 *   - QuoteCenter / 报价单后台(代理算利润要精确)
 *   - admin /admin/* 路径
 *   - 后端 audit_log / 错误码 ENUM
 */

/** 标准术语翻译表(单数 · 多数语境通用) */
export const TERM_DICT: Record<string, string> = {
    // 9 stage 生命周期
    stage: '阶段',
    'stage 1': '询价',
    'stage 2': '诊断',
    'stage 3': '待报价',
    'stage 4': '报价中',
    'stage 5': '写作',
    'stage 6': '投放',
    'stage 7': '监测',
    'stage 8': '报告',
    'stage 9': '续费',

    // 数据对象
    quote: '报价单',
    quotes: '报价单',
    brand: '客户',
    brands: '客户',
    diagnosis: '诊断',
    diagnoses: '诊断',
    profile: '客户资料',
    profiles: '客户资料',

    // token / 链接
    token: '链接',
    portal: '客户门户',
    'portal_token': '客户门户链接',
    'intake_token': '资料补全链接',
    'access_token': '访问令牌',

    // 状态
    draft: '草稿',
    pending_payment: '待付款',
    paid: '已付款',
    confirmed: '已确认',
    archived: '已归档',
    active: '服务中',
    expired: '已到期',
    expiring: '即将到期',
    paused: '已暂停',
    inactive: '未激活',

    // 工程版本术语(M1a/M1b/M1c/M2/M3 等)
    'M1a': 'GEO 主链路',
    'M1b': '报价质量',
    'M1c': '资料飞轮',
    'M2': '报告 v2',
    'M3': '新版工作台',

    // SEO/GEO 术语
    SEM: '搜索引擎营销',
    GEO: 'AI 搜索优化',
    // [2026-05-26 v6] 占有率类术语翻译条目删除 · 残留调用应即时暴露给 reviewer
    // 见 docs/AI-CONTEXT/SOV_ALLOWLIST.md(allowlist 第 2 条)
    '净化测度': '精度评分',

    // LLM 模型名
    'qwen3-max': '通义千问',
    'qwen3': '通义千问',
    'deepseek': 'DeepSeek',
    'kimi': 'Kimi',
    'doubao': '豆包',

    // 流程动词
    autofill: 'AI 自动填写',
    rerun: '重跑',
    revert: '回退',

    // 其他常见术语
    BRD: '客户编号',
    'fee': '费用',
    'fees': '费用',
};

/** 短语级翻译(多 token · 优先级高于单 token) */
export const PHRASE_DICT: Record<string, string> = {
    'unmapped route': '该功能暂未开放',
    'unmapped_route': '该功能暂未开放',
    'permission denied': '权限不足',
    'permission_changed': '权限已变更 · 请重新登录',
    'rate limit': '操作太频繁 · 请稍后再试',
    'rate_limit': '操作太频繁 · 请稍后再试',
    'service unavailable': '服务暂不可用 · 请稍后重试',
    'invalid token': '登录已过期 · 请重新登录',
    'token expired': '登录已过期 · 请重新登录',
    '没有数据': '暂无数据',
    '加载失败': '加载失败 · 请稍后再试',
    '请求失败': '操作失败 · 请稍后再试',
};

/**
 * 翻译单个术语 · 命中返人话 / 不命中返原文
 * 不区分大小写
 */
export function translateTerm(term: string | null | undefined): string {
    if (!term) return '';
    const key = String(term).trim();
    if (!key) return '';
    // 1. PHRASE_DICT 优先(多 token)
    const lc = key.toLowerCase();
    if (PHRASE_DICT[lc]) return PHRASE_DICT[lc];
    // 2. 严格大小写匹配
    if (TERM_DICT[key]) return TERM_DICT[key];
    // 3. 小写匹配
    if (TERM_DICT[lc]) return TERM_DICT[lc];
    // 4. 兜底:返原文(不破坏)
    return key;
}

/**
 * 替换文本中的工程术语 · 用于 UI 文案 / 错误信息 prefix
 *
 * 例:
 *   replaceTermsInText("不动 stage · 直接加进当前 quote")
 *   → "不动 阶段 · 直接加进当前 报价单"
 *
 * 注意:严格大小写敏感 · 避免误伤(如 "正在 GEO 中" 不会改 GEO)
 */
export function replaceTermsInText(text: string | null | undefined): string {
    if (!text) return '';
    let result = String(text);
    // 短语优先
    Object.entries(PHRASE_DICT).forEach(([term, replacement]) => {
        const re = new RegExp(`\\b${term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'gi');
        result = result.replace(re, replacement);
    });
    // 单 token(只匹配独立词 · 不匹配 substring)
    Object.entries(TERM_DICT).forEach(([term, replacement]) => {
        // 跳过太短(2 字以下)的项 · 避免误伤
        if (term.length < 2) return;
        const re = new RegExp(`\\b${term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'g');
        result = result.replace(re, replacement);
    });
    return result;
}

/** 常用术语对象访问(代码补全友好) */
export const T = {
    stage: TERM_DICT['stage'],
    quote: TERM_DICT['quote'],
    brand: TERM_DICT['brand'],
    diagnosis: TERM_DICT['diagnosis'],
    profile: TERM_DICT['profile'],
    token: TERM_DICT['token'],
    portal: TERM_DICT['portal'],
    draft: TERM_DICT['draft'],
    pending_payment: TERM_DICT['pending_payment'],
    paid: TERM_DICT['paid'],
    confirmed: TERM_DICT['confirmed'],
    active: TERM_DICT['active'],
    M1c: TERM_DICT['M1c'],
    SEM: TERM_DICT['SEM'],
} as const;
