#!/usr/bin/env node
/**
 * 门禁 · iPhone/iPad「点了没反应 / 卡住」修复锁
 * [WO_IOS_TOUCH_UX 2026-08-05]
 *
 * 这些 bug 的共同特征是**桌面 Chrome 上永远不复现** —— 所以人工验不出来,
 * 只能靠机械判据钉住。锁的每一条都配了成对的「必须不命中」,
 * 防止判据退化成恒真(2026-08-02 死函数判据恒绿那个坑)。
 *
 * 跑法:node scripts/test-ios-touch-ux.mjs
 * ⚠️ 本锁**未接进 package.json 的 build 链** —— frontend/package.json 是
 *    diaglaunch-20260805 的活声明,刻意避让。接线交 Deploy 在那个包落地后补。
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const ts = require('typescript');

const SRC = path.resolve(import.meta.dirname, '../src');
const results = [];
const rec = (name, pass, detail) => results.push({ name, pass, detail });

/**
 * 🔴 去注释后再做文本判定。
 * 2026-08-05 变异实测:M4/M10 两个变异**存活**,因为判据抓到的是我自己写的注释 ——
 * openAsyncUrl.ts 的头注释里有字面 `win.opener = null`、hook 注释里有 `vv.offsetTop`,
 * 于是把代码删掉判据照样绿。凡是"某处必须有这行代码"的判据,一律打在去注释后的文本上。
 * (同一个坑记忆里已有:media-board-ux 那次锁抓到自己的注释。)
 */
const stripComments = (s) => s
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
const readCode = (p) => stripComments(fs.readFileSync(p, 'utf8'));

// ============================================================
// 共用:AST 手势链扫描(与交付时用的扫描器同一套判据)
// ============================================================
const DANGER = [
  { re: /^window\.open$/, kind: 'window.open' },
  { re: /^navigator\.clipboard\.writeText$/, kind: 'clipboard.writeText' },
  { re: /^navigator\.clipboard\.write$/, kind: 'clipboard.write' },
  { re: /^document\.execCommand$/, kind: 'execCommand', argMustBe: 'copy' },
  { re: /^copyToClipboard$/, kind: 'copyToClipboard' },
  { re: /^copyAsyncText$/, kind: 'copyAsyncText' },
  { re: /^openAsyncUrl$/, kind: 'openAsyncUrl' },
];

// 🔴 归一化可选链:`clipboard?.writeText` 与 `clipboard.writeText` 在 Safari 手势闸面前
//    是同一回事。不归一化会整片漏检(交付时第一版扫描器就栽在这)。
const calleeText = (n) => {
  try { return n.expression.getText().replace(/\?\./g, '.').replace(/\s+/g, ''); } catch { return ''; }
};
const isFn = (n) => ts.isArrowFunction(n) || ts.isFunctionExpression(n)
  || ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n);

function scanFile(file) {
  const src = fs.readFileSync(file, 'utf8');
  const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const hits = [];
  const visit = (node) => {
    if (ts.isCallExpression(node)) {
      const txt = calleeText(node);
      for (const d of DANGER) {
        if (!d.re.test(txt)) continue;
        if (d.argMustBe) {
          const a0 = node.arguments[0];
          if (!a0 || !ts.isStringLiteral(a0) || a0.text !== d.argMustBe) continue;
        }
        // 只有「会被手势闸拦」的才需要判;copyAsyncText / openAsyncUrl 是修好后的写法
        const gestureSafeApi = d.kind === 'copyAsyncText' || d.kind === 'openAsyncUrl';
        const reasons = [];
        let n = node.parent, depth = 0;
        const chain = [];
        while (n && depth < 40) { if (isFn(n)) chain.push(n); n = n.parent; depth++; }
        for (const fn of chain) {
          if (fn.body) {
            let found = null;
            const walk = (x) => {
              if (found) return;
              if (x !== fn && isFn(x)) return;
              if (ts.isAwaitExpression(x) && x.getEnd() <= node.getStart()) {
                let p = x.parent, own = null;
                while (p) { if (isFn(p)) { own = p; break; } p = p.parent; }
                if (own === fn) { found = x; return; }
              }
              ts.forEachChild(x, walk);
            };
            walk(fn.body);
            if (found) reasons.push('AWAIT_BEFORE');
          }
          const p = fn.parent;
          if (p && ts.isCallExpression(p) && p.arguments.includes(fn)) {
            const ct = calleeText(p);
            if (/\.(then|catch|finally)$/.test(ct)) reasons.push('IN_THEN');
            else if (/^(setTimeout|setInterval|requestAnimationFrame|queueMicrotask)$/.test(ct)) reasons.push('IN_TIMER');
          }
        }
        hits.push({
          file: path.relative(SRC, sf.fileName).replace(/\\/g, '/'),
          line: sf.getLineAndCharacterOfPosition(node.getStart()).line + 1,
          kind: d.kind,
          broken: !gestureSafeApi && reasons.length > 0,
        });
        break;
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return hits;
}

const walkDir = (dir, out = []) => {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) { if (e.name !== 'node_modules') walkDir(p, out); }
    else if (/\.tsx?$/.test(e.name)) out.push(p);
  }
  return out;
};

const allHits = walkDir(SRC).flatMap(scanFile);
const broken = allHits.filter((h) => h.broken);
const safe = allHits.filter((h) => !h.broken);

// ============================================================
// 锁 1 · 手势链断裂点只允许剩白名单那 3 处
// ============================================================
// 白名单每条都必须写清"为什么这处不修" —— 没有理由的豁免就是后门。
const ALLOWED = [
  {
    file: 'lib/copyUtils.ts', kind: 'execCommand',
    why: 'execCommand 兜底本体。它就是 `await clipboard.writeText` 失败后的降级路径,'
       + '结构上必然在 await 之后 —— 工单 §4 明令不许动兜底顺序去碰运气。'
       + '真兜底靠 ManualCopyDialog(把内容亮出来让用户长按选)。',
  },
  {
    file: 'lib/copyUtils.ts', kind: 'copyToClipboard',
    why: 'copyAsyncText 内部:Safari 不接受 Promise 载荷时回落到「await 后再写」的旧路。'
       + '桌面手势上下文不过期,旧路是好的 —— 属设计内。',
  },
  // [开源 E3 · 前端 · 2026-10-01 · WO_322] 原第三条(社媒工作台主页面的 best-effort 顺手复制)随社媒工作台整删
];
const brokenKeys = broken.map((h) => `${h.file}::${h.kind}`).sort();
const allowedKeys = ALLOWED.map((a) => `${a.file}::${a.kind}`).sort();
const unexpected = brokenKeys.filter((k) => !allowedKeys.includes(k));
const disappeared = allowedKeys.filter((k) => !brokenKeys.includes(k));
rec('锁1 手势链断裂点 == 白名单',
  unexpected.length === 0 && disappeared.length === 0,
  unexpected.length ? `🔴 新增未豁免的断裂点: ${unexpected.join(' , ')}`
    : disappeared.length ? `白名单条目已消失(是好事,但要同步删白名单): ${disappeared.join(' , ')}`
    : `断裂点 ${broken.length} 处,全部在白名单内`);

// 🔴 反向对照:SAFE 桶必须够大。若为 0 或极小,说明扫描器根本没在工作,
//    锁 1 的"0 个意外断裂点"就是恒真的假绿。
// [开源 E3 · 前端 · 2026-10-01] 下限 50 → 35:基线 0cce841f9 SAFE 114 → E3 后 47,少的 67 处逐文件核过
//   全在已删文件里(社媒工作台主页面 12 · M3 方案页 6 · M3 客户工作台 4 …),在役文件一处没变;留约四分之一余量,
//   这格验的是「扫描器没瞎」,不是「调用点不许少」。
rec('锁1-反向 扫描器不是恒空(SAFE 桶 > 35)',
  safe.length > 35,
  `SAFE=${safe.length} / 总调用点=${allHits.length}(SAFE 过小 = 判据失效,锁1 的绿是假的)`);

// 🔴 反向对照:三个被点名修过的页面必须真的出现在扫描结果里。
//    否则可能是路径写错/文件没扫到,导致"没有断裂点"。
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 原三个点名页里社媒充值弹窗与 M3 方案页
//   随宿主整删;换成同一批迁移过的在役页 QuotePreview / Reports(同样要求扫描覆盖得到)。
for (const f of ['pages/Diagnosis/DiagnosisReport.tsx', 'pages/Agent/QuotePreview.tsx', 'pages/Reports/index.tsx']) {
  rec(`锁1-反向 ${f} 在扫描覆盖范围内`,
    allHits.some((h) => h.file === f),
    allHits.some((h) => h.file === f) ? '已覆盖' : '🔴 该文件一个调用点都没扫到 —— 扫描范围有问题');
}

// 🔴 反向对照:可选链写法(`clipboard?.writeText`)也必须被扫到 —— 扫描器先把 `?.` 归一成 `.` 再认,
//    去掉归一化这类调用点会整片漏检。[开源 E3 · 前端 · 2026-10-01 · WO_322] 原来靠白名单里社媒工作台主页面那处(可选链)
//    间接证明,它随社媒工作台删除;改成直接点名一个在役的可选链站点:BrandDetailPage 唯一那处复制。
{
  const f = 'pages/Brand/BrandDetailPage.tsx';
  const optionalSite = /navigator\.clipboard\?\.writeText\(/.test(fs.readFileSync(path.join(SRC, f), 'utf8'));
  const seen = allHits.some((h) => h.file === f);
  rec('锁1-反向 可选链写法的调用点也扫得到(BrandDetailPage)', optionalSite && seen,
    !optionalSite ? '🔴 锚点没了(那处可选链调用被改写)—— 换一个在役可选链站点' : seen ? '已覆盖' : '🔴 可选链调用点漏检 —— 归一化失效');
}

// ============================================================
// 锁 2 · 三处 window.open 病灶必须走 openAsyncUrl 保窗
// ============================================================
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 三处病灶里社媒充值弹窗与 M3 方案页随宿主整删,在役只剩诊断报告这一处
const OPEN_FIXED = [
  'pages/Diagnosis/DiagnosisReport.tsx',
];
for (const f of OPEN_FIXED) {
  const s = fs.readFileSync(path.join(SRC, f), 'utf8');
  rec(`锁2 ${f} 走 openAsyncUrl`, s.includes('openAsyncUrl('), s.includes('openAsyncUrl(') ? 'ok' : '🔴 没用保窗共用件');
}
// 🔴 反向对照:**同步**手势内直开门户链接(扫描器判 SAFE)不该被顺手改掉 ——
//    裸 window.open 本身不是罪,「await 之后的 window.open」才是。
//    这条同时证明锁 2 不是"把所有 window.open 都禁掉"的粗暴规则。
//    [开源 E3 · 前端 · 2026-10-01 · WO_322] 原锚点(M3 方案页的打开门户按钮)随 pages/M3 删;换成同义的在役站点:
//    监测门户令牌弹窗里「打开门户」按钮的 onClick 同步直开 /portal/<token>;并要求扫描器没把它判成断裂点。
{
  const f = 'pages/Monitoring/components/TokenDialog.tsx';
  const s = fs.readFileSync(path.join(SRC, f), 'utf8');
  const hasPortalDirect = /onClick=\{\(\)\s*=>\s*window\.open\(`\/portal\//.test(s);
  const notBroken = !broken.some((h) => h.file === f);
  rec('锁2-反向 同步手势内的裸 window.open 保持不动(TokenDialog 打开门户)',
    hasPortalDirect && notBroken,
    !hasPortalDirect ? '🔴 同步直开被误改了,或判据锚点失效' : !notBroken ? '🔴 扫描器把同步直开判成断裂点了' : 'ok · 证明本锁只管 await 之后那种');
}

// ============================================================
// 锁 3 · openAsyncUrl 实现的两个反直觉点
// ============================================================
{
  // 🔴 用 readCode(去注释):本文件头注释里就有字面 `win.opener = null`,
  //    直接读原文会让"代码被删"这个变异存活(2026-08-05 实测过一次)。
  const s = readCode(path.join(SRC, 'lib/openAsyncUrl.ts'));
  // 必须命中:占位窗口后手动断 opener(等效 noopener)
  rec('锁3 openAsyncUrl 手动断 opener', /win\.opener\s*=\s*null/.test(s),
    /win\.opener\s*=\s*null/.test(s) ? 'ok' : '🔴 缺 opener 断链 = 安全回退');
  // 🔴 必须不命中:占位那次 window.open 不许传 noopener ——
  //    规范规定带 noopener 时返回 null,拿不到窗口引用,保窗直接失效(修了等于没修)。
  const placeholderOpen = s.match(/window\.open\((['"])\1\s*,\s*['"]_blank['"][^)]*\)/);
  rec('锁3-反向 占位 window.open 不带 noopener',
    Boolean(placeholderOpen) && !/noopener/.test(placeholderOpen[0]),
    placeholderOpen ? `实参 = ${placeholderOpen[0]}` : '🔴 找不到占位 open 调用');
  // 🔴 必须不命中:失败时不许回落 location.href —— 那会顶掉用户当前页面
  rec('锁3-反向 被拦时不顶掉当前页(无 location.href 回落)',
    !/location\.href\s*=/.test(s),
    !/location\.href\s*=/.test(s) ? 'ok' : '🔴 出现 location.href 回落,会把用户正在填的页面顶掉');
}

// ============================================================
// 锁 4 · 触屏表单字号下限 16px
// ============================================================
{
  const css = fs.readFileSync(path.join(SRC, 'index.css'), 'utf8');
  const block = css.match(/@media\s*\(hover:\s*none\)\s*and\s*\(pointer:\s*coarse\)\s*\{[\s\S]*?\n\}/);
  rec('锁4 存在触屏 media query 且设 16px',
    Boolean(block) && /font-size:\s*16px/.test(block?.[0] ?? ''),
    block ? 'ok' : '🔴 找不到 (hover:none) and (pointer:coarse) 规则块');
  if (block) {
    const b = block[0];
    // 必须命中:三种表单元素都覆盖
    for (const el of ['input', 'textarea', 'select']) {
      rec(`锁4 覆盖 ${el}`, new RegExp(`(^|[\\s,])${el}[:.\\[\\s,{]`, 'm').test(b), 'ok');
    }
    // 🔴 反向对照 A:必须排除大字号,否则会把金额等故意放大的输入框压成 16px
    rec('锁4-反向 排除 text-lg 及以上(不破坏大字号设计)',
      /:not\(\.text-lg\)/.test(b) && /:not\(\.text-2xl\)/.test(b),
      /:not\(\.text-lg\)/.test(b) ? 'ok' : '🔴 没排除大字号,会把 text-2xl 的输入框压小');
    // 🔴 反向对照 B:必须排除不弹键盘的控件
    rec('锁4-反向 排除 checkbox/radio(不弹软键盘,与缩放无关)',
      /:not\(\[type=['"]checkbox['"]\]\)/.test(b),
      /checkbox/.test(b) ? 'ok' : '🔴 没排除 checkbox');
    // 🔴 反向对照 C:不许用 !important 硬压 —— 本仓靠 unlayered 胜过 @layer(index.css:271 已确立)
    rec('锁4-反向 不用 !important(靠 unlayered 覆盖)',
      !/!important/.test(b),
      !/!important/.test(b) ? 'ok' : '🔴 出现 !important = 粗暴覆盖,会压到不该压的地方');
    // 🔴 反向对照 D:规则必须在 unlayered 区域(不能被包进 @layer,否则压不过 utilities)
    //    2026-08-05 变异实测:第一版判据写的是「@layer 开括号数 <= 行首 } 数」,
    //    结果是**恒真** —— index.css 里几百个普通 CSS 规则的 `}` 也在行首,被算进了"闭"。
    //    改成真正的括号平衡:从文件开头逐字符算嵌套深度,记录进入 @layer 时的深度,
    //    到达目标规则时若仍在某个 @layer 的深度之内 → 说明被包住了。
    const idx = css.indexOf(block[0]);
    const head = stripComments(css.slice(0, idx));
    let depth = 0;
    const layerDepths = [];
    for (let i = 0; i < head.length; i++) {
      const c = head[i];
      if (c === '{') {
        // 看这个 { 属不属于一条 @layer 声明
        const lineStart = head.lastIndexOf('\n', i) + 1;
        if (/@layer[^{};]*$/.test(head.slice(lineStart, i))) layerDepths.push(depth);
        depth++;
      } else if (c === '}') {
        depth--;
        while (layerDepths.length && layerDepths[layerDepths.length - 1] >= depth) layerDepths.pop();
      }
    }
    rec('锁4-反向 规则未被包进 @layer(否则压不过 utilities)',
      layerDepths.length === 0 && depth === 0,
      layerDepths.length ? `🔴 规则处在 @layer 内(层深 ${layerDepths.join(',')})` : `unlayered ✅ (到达该规则时嵌套深度=${depth})`);
    // 🔴 反向对照 E:不能用 [class*="text-"] —— text-center/text-foreground 全会误命中,规则形同虚设
    rec('锁4-反向 不用 [class*="text-"] 这种会被 text-center 误命中的写法',
      !/class\*=/.test(b),
      !/class\*=/.test(b) ? 'ok' : '🔴 [class*="text-"] 会被 text-center / text-foreground 误判');
  }
}

// ============================================================
// 锁 5 · 客户视角两页的软键盘处理
// ============================================================
const KB_PAGES = ['pages/Intake/IntakeFillPage.tsx', 'pages/MaterialConfirm/MaterialConfirmPage.tsx'];
for (const f of KB_PAGES) {
  const s = readCode(path.join(SRC, f));
  rec(`锁5 ${f} 接了 useSoftKeyboardOpen`, s.includes('useSoftKeyboardOpen'),
    s.includes('useSoftKeyboardOpen') ? 'ok' : '🔴 底部固定条会被软键盘盖住');
  // 🔴 2026-08-05 变异实测:第一版判据只验 /keyboardInset/ 出现过 —— 但 `const { inset: keyboardInset }`
  //    这行声明本身就满足它,于是"拿了 hook 却不用"这个变异存活。
  //    改成必须验**真的用在位移上**:translateY(-${keyboardInset}) 或 framer 的 y: ...keyboardInset。
  const reallyUsed = /translateY\(-\$\{keyboardInset\}px\)/.test(s)
    || /y:\s*keyboardInset\s*>\s*0\s*\?\s*-keyboardInset/.test(s);
  rec(`锁5 ${f} 真的用了键盘高度做位移`, reallyUsed,
    reallyUsed ? 'ok' : '🔴 拿了 hook 但没把 keyboardInset 用在位移上(声明它不算用)');
}
// 🔴 反向对照:MaterialConfirm 的底部条是 motion.div —— framer 的 animate 会生成自己的
//    inline transform,把 style.transform 覆盖掉。位移必须走 framer 的 `y`,写在 style 里等于没修。
{
  const s = fs.readFileSync(path.join(SRC, 'pages/MaterialConfirm/MaterialConfirmPage.tsx'), 'utf8');
  const bar = s.match(/mc-bottom-bar[\s\S]{0,900}/)?.[0] ?? '';
  const ctx = s.match(/[\s\S]{0,900}mc-bottom-bar/)?.[0] ?? '';
  const both = ctx + bar;
  rec('锁5-反向 键盘位移走 framer 的 animate y,不是 style.transform',
    /animate=\{\{\s*y:\s*keyboardInset/.test(both) && !/transform:\s*`translateY/.test(both),
    /animate=\{\{\s*y:\s*keyboardInset/.test(both)
      ? 'ok' : '🔴 位移没走 framer 的 y → motion 的 inline transform 会把它覆盖掉,修了等于没修');
}
// 🔴 反向对照:hook 必须扣掉 visualViewport.offsetTop,否则 iOS 上页面被系统上推时会顶过头
{
  // 🔴 去注释:本 hook 的注释里就解释了 `vv.offsetTop` 是什么,
  //    读原文会让"把 offsetTop 从公式里删掉"这个变异存活(2026-08-05 实测过一次)。
  const s = readCode(path.join(SRC, 'hooks/useSoftKeyboardOpen.ts'));
  rec('锁5-反向 键盘高度扣掉 visualViewport.offsetTop',
    /-\s*vv\.offsetTop/.test(s), /-\s*vv\.offsetTop/.test(s) ? 'ok' : '🔴 没扣 offsetTop,iOS 上底部条会顶过头');
  rec('锁5-反向 有最小阈值防把工具栏收放误判成键盘',
    /KEYBOARD_MIN_INSET\s*=\s*\d{2,}/.test(s), 'ok');
}

// ============================================================
// 锁 6 · 复制失败不许只说一句"失败"(feedback_hint_must_help_or_hide)
// ============================================================
// 本单改过的页面里,凡是走 copyAsyncText 的,失败分支必须把内容交出去
// (ManualCopyDialog / setManualCopyText / 把 url 塞进可见文案),不许只 toast 一句失败。
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 原六个面里五个(旧社媒操盘手两页 · 社媒工作台身份面板 · 社媒组件两件)随宿主整删
const MUST_GIVE_BACK = [
  'pages/Referral/InviteCenter.tsx',
];
for (const f of MUST_GIVE_BACK) {
  const s = readCode(path.join(SRC, f));
  // 🔴 2026-08-05 变异实测:第一版判据只验 `setManualCopyText(` 出现过 —— 但
  //    `onClose={() => setManualCopyText(null)}` 本身就满足它,于是"失败只报错"这个变异存活。
  //    必须验**用真内容调用**(参数不是 null),才算真把内容交还给了用户。
  const givesBack = /setManualCopyText\(\s*(?!null\s*\))[A-Za-z_$]/.test(s);
  const hasDialog = /<ManualCopyDialog/.test(s);
  rec(`锁6 ${f} 复制失败把内容交还用户`,
    givesBack && hasDialog,
    !givesBack ? '🔴 只有 setManualCopyText(null) 这种关闭调用,没有把真内容交出去'
      : !hasDialog ? '🔴 没渲染 ManualCopyDialog,内容交了也没地方显示' : 'ok');
}
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 锁6-反向(「本单没碰的社媒工作台主页面本地 copyToClipboard 保持原样」)退役:
//   那是全站唯一一个未纳管的本地复制实现,随社媒工作台整删,没有同类可接替(其余复制都走 lib/copyUtils)。

// ============================================================
const fail = results.filter((r) => !r.pass);
for (const r of results) console.log(`  ${r.pass ? 'PASS' : '🔴FAIL'}  ${r.name}${r.pass ? '' : ' — ' + r.detail}`);
console.log(`\n  ${results.length - fail.length} PASS / ${fail.length} FAIL`);
if (fail.length) { console.error('\n🔴 iOS 触屏 UX 门禁未通过'); process.exit(1); }
console.log('  ✅ iOS 触屏 UX 门禁通过');
