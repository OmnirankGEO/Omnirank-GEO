/**
 * preprocessMarkdownForChinese — ResearchMonitor 中文抓取正文的 markdown 清洗
 *
 * 原 ArticlesPanel.tsx / CitationsPanel.tsx 两处复制粘贴的 SOH/STX hack，
 * 2026-07-22 sink census F6/F7 收口为共享 lib（语义一字未改，仅去重）。
 *
 * 流程:
 *   step -1: 行首"数字 + 多星 + 内容"整段转 H3 标题 (1****X / 2.1****<6 mm)
 *   step  0: 3+ 连续星号折叠为 ** (****X**** → **X**)
 *   step  1: 配对 **X** 转占位符 (保护) → SOH X STX
 *   step  2: 单星 *X* 同样占位符保护 (避免被 step 3 误删)
 *   step  3: 删所有残留孤立的 ** 和 * (没匹配上的)
 *   step  4: 占位符还原为 **​X​** (零宽空格强制 CommonMark 识别中文边界)
 *   step  5: heading # 后补空格 (#标题 → # 标题)
 *
 * SOH(\x01) / STX(\x02) 是 ASCII 控制字符 · 正文几乎不会出现 · 用作临时占位
 */

const SOH_B = '\x01', STX_B = '\x02';  // bold 占位
const SOH_I = '\x03', STX_I = '\x04';  // italic 占位

export function preprocessMarkdownForChinese(md: string | null | undefined): string {
    if (!md) return '';
    let s = md;
    // step -1: 行首 '数字 + 2+ 星号 + 内容' → ### 标题
    s = s.replace(/^(\d+(?:\.\d+)?)\*{2,}\s*([^\n*]+?)(\s*\*{2,})?\s*$/gm, '### $1 $2');
    // step -0.5: 整行 '**数字 内容**' 包裹的粗体段落 → H3 标题
    s = s.replace(/^\*\*\s*(\d+(?:\.\d+)?)\s*([^\n*]+?)\*\*\s*$/gm, '### $1 $2');
    // step 0: 折叠 3+ 连续星号
    s = s.replace(/\*{3,}/g, '**');
    // step 1: 配对 **X** → 占位符 (保护)
    s = s.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, `${SOH_B}$1${STX_B}`);
    // step 2: 配对 *X* → 占位符 (此时 ** 已转占位 · 单星不会误吃)
    s = s.replace(/\*([^*\n]+?)\*/g, `${SOH_I}$1${STX_I}`);
    // step 3: 删残留孤立 * (一切没成对的 · 视为脏数据)
    s = s.replace(/\*/g, '');
    // step 4: 占位符还原为 emphasis (加零宽空格强制中文边界识别)
    s = s.replace(new RegExp(`${SOH_B}([^${STX_B}]+?)${STX_B}`, 'g'), '**​$1​**');
    s = s.replace(new RegExp(`${SOH_I}([^${STX_I}]+?)${STX_I}`, 'g'), '*​$1​*');
    // step 5: heading # 后补空格
    s = s.replace(/^(#{1,6})([^#\s])/gm, '$1 $2');
    return s;
}
