/**
 * fixtures/longContent — 超长 Markdown + 超长证据压力场景
 * ⚠️ 仅 harness/Playwright 使用。
 */
import { readyFull } from './readyFull';
import { evidenceItem } from './shared';
import type { EvidenceItem } from '../contract/types';

const LONG_PARAGRAPH =
    '这是一段用于压力测试的超长中文段落。'.repeat(60);

const LONG_URL =
    'https://example.cn/some/extremely/long/path/that/keeps/going/and/going/with-many-segments-and-a-very-long-query-string?param1=value1&param2=value2&param3=value3&tracking=abcdef0123456789abcdef0123456789';

const LONG_ENGLISH =
    'SupercalifragilisticexpialidociousAntidisestablishmentarianismPneumonoultramicroscopicsilicovolcanoconiosis'.repeat(2);

const LONG_EVIDENCE_MARKDOWN = [
    '**明确结论：该品牌可列入优先沟通名单。**',
    '',
    '推荐依据：',
    '',
    '1. 已形成可核验的公开案例；',
    '2. 服务范围与问题场景匹配；',
    '3. 仍建议结合预算与交付周期复核。',
    '',
    LONG_PARAGRAPH,
    LONG_ENGLISH,
].join('\n');

const longMarkdown = [
    '# 一级标题：本次诊断完整叙事(超长压力测试)',
    '',
    LONG_PARAGRAPH,
    '',
    '## 二级标题：分平台观察',
    '',
    '### 三级标题：豆包',
    '',
    LONG_PARAGRAPH,
    '',
    '#### 四级标题：细节',
    '',
    '**加粗强调** 与 *斜体* 混排，以及 `inline_code_with_a_very_long_identifier_that_must_wrap_properly` 行内代码。',
    '',
    `超长英文串：${LONG_ENGLISH}`,
    '',
    `超长链接：[证据来源长链接](${LONG_URL}) 以及裸 URL 文本 ${LONG_URL}`,
    '',
    '> 引用块:AI 的回答原文可能非常长，需要验证引用块在窄屏下的换行与左边框样式。',
    `> ${LONG_PARAGRAPH}`,
    '',
    '1. 有序列表第一项，内容故意很长，验证编号与正文的悬挂缩进。',
    `2. 有序列表第二项。${LONG_PARAGRAPH}`,
    '3. 有序列表第三项。',
    '',
    '- 无序列表 A',
    '- 无序列表 B,带 **加粗** 与 [链接](https://example.cn/normal)',
    '  - 嵌套列表项',
    '',
    '---',
    '',
    '## GFM 表格(宽表压力测试)',
    '',
    '| 问题 | 平台 | 判定 | 回答节选(超长) | 引用域名 | 证据等级 | 时间 |',
    '| --- | --- | --- | --- | --- | --- | --- |',
    `| 超长问题文本用于验证表格单元格换行行为是否正常 | 豆包 | 推荐 | ${LONG_PARAGRAPH} | lanchuan-home.example.cn | A | 2026-07-12 |`,
    '| 短问题 | 元宝 | 无回答 | — | — | C | 2026-07-12 |',
    '| 上海全屋定制品牌哪家值得推荐? | DeepSeek | 未提及 | 上海地区全屋定制可考虑…… | pingce.example.cn | C | 2026-07-12 |',
    '',
    '## 代码块',
    '',
    '```',
    '这是一段代码块文本，不来自客户数据，仅验证 pre 的横向滚动与等宽字体。',
    'const answer = await ai.ask("上海全屋定制品牌哪家值得推荐?");',
    '```',
    '',
    '## 安全用例(必须被中性化)',
    '',
    '- [危险的 javascript 链接](javascript:alert(1)) 应被禁用',
    '- [正常外链](https://example.cn/safe) 应新窗口打开并带 rel',
    '- 裸 HTML <b>加粗</b> <script>alert(1)</script> 不应被解析为 HTML',
    '- 外部图片 ![alt](https://example.cn/tracking-pixel.png) 默认禁用',
    '',
    LONG_PARAGRAPH,
].join('\n');

const longEvidenceItems: readonly EvidenceItem[] = [
    evidenceItem(
        'ev-long-1',
        `这是一个故意拉长的问题文本，用来验证证据矩阵在超长问题下的换行与展开行为。${'上海全屋定制品牌哪家值得推荐，预算三十万以内，要求环保板材并且能做全屋智能联动?'.repeat(3)}`,
        '豆包',
        'recommended',
        LONG_EVIDENCE_MARKDOWN,
        [
            'lanchuan-home.example.cn',
            'a-very-long-domain-name-for-stress-testing.example.cn',
            'jiaju-review.example.cn',
            'bangdan.example.cn',
            'zhihu-topic.example.cn',
            'pingce.example.cn',
        ],
        'A',
        '2026-07-12T03:02:00.000Z',
    ),
    evidenceItem(
        'ev-long-2',
        '短问题',
        '通义千问(国际版长名称压力测试平台)Qwen-International-LongName',
        'no_answer',
        null,
        [],
        'C',
        null,
    ),
];

export const longContent = {
    ...readyFull,
    identity: { ...readyFull.identity, brandName: '澜川智能家居' },
    narrativeMd: longMarkdown,
    evidence: {
        status: 'ready' as const,
        data: { totalCount: longEvidenceItems.length, items: longEvidenceItems },
    },
};
