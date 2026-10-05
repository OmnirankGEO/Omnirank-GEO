/**
 * 前端门禁 · 出现率分母「身份待确认」明示(WO_UNKNOWN_DENOMINATOR_DISCLOSURE 2026-08-07)
 *
 * 🔴 本锁**只读 frontend/ 下的文件** —— 引用后端文件的锁不许进前端 build 链
 *    (本仓烧过两次 19 分钟预烤)。
 *
 * 锁四条 Owner 边界里前端能证的那几条,每条都配反向对照:
 *   ② C 端不给确认入口   ③ C 端人话/禁工程词   ④ N=0 整块不渲染
 * 以及复审点名的映射层(少一行 → 整段平台数据静默降级,线上看不出来)。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, '../src');

const read = (rel) => readFileSync(resolve(SRC, rel), 'utf8');

const PLATFORM = 'features/publicReportPremium/components/sections/PlatformPerformance.tsx';
const TYPES = 'features/publicReportPremium/contract/types.ts';
const MAPDTO = 'features/publicReportPremium/transport/mapDto.ts';
const CELL = 'pages/Diagnosis/components/BrandVerdictCell.tsx';
const CALIB = 'features/publicReportPremium/components/sections/IdentityCalibration.tsx';
const PAGE = 'features/publicReportPremium/components/ReportPage.tsx';

/** 剥块注释与行注释:判据不许撞上自己写的注释(本仓踩过) */
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
/**
 * 只取 JSX 文本节点里的中文串 = 真正渲染给用户看的文案。
 *
 * 🔴 边界必须同时认 `<` `>` 和 `{` `}`:文案里夹着 `{pendingDisclosureTotal}`
 * 这类插值时,文本节点是被大括号**切断**的(`>已统计 {n} 次...`)。
 * 第一版只认尖括号,「已统计」整段取不到 —— 判据当场变红,而实现是对的。
 * (先读实现/先看真实产物,别按"我以为的形状"写正则。)
 */
const renderedText = (s) =>
    [...strip(s).matchAll(/[>}]([^<>{}]*[一-鿿][^<>{}]*)[<{]/g)].map((m) => m[1]).join(' ');

let pass = 0;
const fails = [];
const check = (name, ok) => {
    if (ok) { pass += 1; console.log(`  PASS  ${name}`); }
    else { fails.push(name); console.log(`  FAIL  ${name}`); }
};

console.log('出现率分母明示 · 前端门禁');

const platform = read(PLATFORM);
const types = read(TYPES);
const mapdto = read(MAPDTO);
const cell = read(CELL);

// ── 边界④ N=0 整块不渲染 ───────────────────────────────────────────────
check('锁1 C 端明示块条件渲染(N=0 不出现)',
    platform.includes('pendingDisclosureTotal > 0 &&'));
check('锁1-反向 求和用 ?? 0 但渲染判据是 > 0(0 不会被渲染成「0 次」)',
    /identityPendingSamples \?\? 0/.test(platform) && platform.includes('pendingDisclosureTotal > 0'));
check('锁2 代理端徽章/明示/去确认三处都挂 pendingTotal > 0',
    (cell.match(/pendingTotal > 0 &&/g) || []).length >= 3);

// ── 边界② C 端不给确认入口 ─────────────────────────────────────────────
const CONFIRM_MARKERS = ['brand-cells/decision', 'confirm_yes', 'add_alias', 'onDecide', '去确认'];
check('锁3 C 端(token 面)无任何确认入口',
    CONFIRM_MARKERS.every((m) => !platform.includes(m)));
check('锁3-反向 代理端**有**确认入口(证明判据不是恒真)',
    cell.includes('去确认') && cell.includes('onDecide'));

// ── 边界③ 人话 / 禁工程词 ──────────────────────────────────────────────
const JARGON = ['PENDING_IDENTITY', 'ai_total_tests', 'UNKNOWN', 'invalid_matched_text',
    'identityPendingSamples', 'detection_reason', 'brand_verdict'];
const platformText = renderedText(platform);
const cellText = renderedText(cell);
check('锁4 C 端渲染文案无工程词', JARGON.every((j) => !platformText.includes(j)));
check('锁4 代理端渲染文案无工程词', JARGON.every((j) => !cellText.includes(j)));
check('锁4-反向 扫描口径抓得到工程词(不是恒真)',
    JARGON.some((j) => renderedText('<p>另有 3 格 PENDING_IDENTITY 未计入</p>').includes(j)));

// ── 文案必须说清分母与被扣掉的数 ───────────────────────────────────────
// 🔴 [Owner 2026-08-07 (A)(a)] 文案口径改了,锁跟着改:
//    生产实证这一层待确认格**是进分母的**(status 是中文标签不是 error),
//    所以「未计入本次出现率」在这里是假话 —— 反过来钉住它不许出现。
check('锁5 C 端给出「已统计 N」+「当前按未提到计入」',
    platformText.includes('已统计') && platformText.includes('当前按「未提到」计入'));
check('锁5-反向 不许再说「未计入本次出现率」(在这一层是假话)',
    !platformText.includes('未计入本次出现率'));
check('锁5 代理端同时给出已确认格数与未计入格数',
    cellText.includes('已确认') && cellText.includes('未计入'));

// ── 复审点名:映射层 + 契约 ─────────────────────────────────────────────
check('锁6 映射层校验新字段(少这行整段平台数据静默降级)',
    mapdto.includes('isOptionalCount(value.identityPendingSamples)'));
check('锁6 契约声明为可选可空(0 时后端给 null)',
    types.includes('readonly identityPendingSamples?: number | null;'));

// ── [WO 2026-08-07 · Owner 拍板] 门户按身份出流 · 前端三条 ──────────────
// 与后端「响应体裁剪」锁**成对**:那条卡数据,这几条卡渲染。
const calib = read(CALIB);
const page = read(PAGE);
const calibText = renderedText(calib);
const calibCode = strip(calib);
const pageCode = strip(page);

check('锁7 校准块渲染条件 = 数据在不在(前端不判身份)',
    /state\.report\.calibration \?/.test(pageCode));
check('锁7-反向 前端不做第二处身份判断(服务端裁剪已是唯一口径)',
    !/isOwner|isAdmin|currentUser|owner_user_id|is_admin|user\.(id|roles)/.test(
        calibCode + pageCode));

check('锁8 服务商视角有软提示条与两个确认按钮',
    calib.includes('identity-calibration-banner')
    && calib.includes('identity-calibration-same')
    && calib.includes('identity-calibration-different')
    && calibText.includes('建议校准后再发给客户'));
check('锁8-反向 软引导不是硬门(不阻断分享 / 不弹原生确认框)',
    !/window\.confirm|blockShare|disableShare/.test(calibCode));

check('锁9 确认调现役端点 + 成功后刷新(数字随之更新)',
    calib.includes('calibration.decisionEndpoint')
    && calib.includes('confirm_yes') && calib.includes('confirm_no')
    && /window\.location\.replace/.test(calib));
check('锁9-反向 不自建第二条写路径(端点只从响应里取,不硬编码)',
    !/['"]\/api\/diagnosis\//.test(calibCode));

check('锁10 校准块文案无内部行话',
    JARGON.every((j) => !calibText.includes(j))
    && !calibText.includes('单元格') && !calibText.includes('待确认格'));

console.log(`\n  ${pass} PASS / ${fails.length} FAIL`);
if (fails.length) {
    console.error('分母明示门禁未通过:\n' + fails.map((f) => '  - ' + f).join('\n'));
    process.exit(1);
}
console.log('分母明示门禁通过');
