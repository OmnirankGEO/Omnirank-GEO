#!/usr/bin/env node
/**
 * [工单 2026-08-06 §4] 浏览器支持底线 —— 机器闸的入口 + 它自己的判别力自证。
 *
 * 干三件事:
 *   1. **底线在不在**:package.json 的 `browserslist` 必须存在,且逐条等于 Owner 2026-08-06
 *      拍板的那张表。写宽一点让闸变绿是明令禁止的(工单 §4.5)。
 *   2. **闸真的跑**:调 eslint 的 compat/compat 扫全 src,有一条超标 API 就退 1。
 *   3. **闸有判别力**(`--selftest` · 七条 · 三正样本 + 四反向对照):
 *      a) 正样本(插件层)—— 植入 `Object.hasOwn`(Chrome 92 > 底线 90),必须报红。
 *         🔴 一开始我用的是 `Object.groupBy`,结果自证报「闸坏了」——
 *         实测插件的数据集里根本没有 groupBy 条目。**选正样本必须先证它真能触发**,
 *         否则自证本身就是坏的,得出的还是假结论。
 *      b) 正样本(补盲区层)—— 插件抓不到原型方法和过新的 API,由文本扫描补;
 *         植入 `Object.groupBy` + `.toSorted`,那一层必须报红。
 *      c) 正样本(容器查询盘点闸)—— 植入一个清单外的 `@lg:hidden`,必须报红。
 *      d-f) 三条「必须不命中」:现有代码不被补盲区扫描误伤 / 现有容器查询全在清单内 /
 *         `Report`·`Notification` 的真调用形态一处都没有(它们是被豁免的,豁免要配反向锁)。
 *      g) 反向对照 —— 把 browserslist 临时收紧成 `chrome 60`,闸必须**大面积**报红;
 *         若仍然全绿,说明闸根本没接上 browserslist,整节作废。
 *
 * 用法:
 *   node scripts/verify-browser-baseline.mjs             # 闸(build 链里跑这个)
 *   node scripts/verify-browser-baseline.mjs --selftest  # 判别力自证(七条)
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PKG = path.join(ROOT, 'package.json');

// 🔴 Owner 2026-08-06 拍板的底线。改这张表 = 改 Owner 的决定,必须回去问。
const EXPECTED_BROWSERSLIST = [
  'safari >= 16',
  'ios_saf >= 16',
  'chrome >= 90',
  'and_chr >= 90',
  'edge >= 90',
  'android >= 90',
];

// 被豁免掉的两个 API 的**真调用形态**。豁免只针对同名局部变量的误报,
// 真用上了必须回来重新判定。🔴 别因为底线抬到 16 就以为可以放行:
//    iOS 的 Web Notification 要到 **16.4** 才有,16.0~16.3 仍然没有。
const FORBIDDEN_REAL_USES = [
  { re: /\bnew\s+Notification\s*\(/, why: 'Web Notification API 在 iOS Safari 16.4 之前不支持(底线 16.0 仍不覆盖)' },
  { re: /\bNotification\s*\.\s*requestPermission\s*\(/, why: '同上' },
  { re: /\bnew\s+ReportingObserver\s*\(/, why: 'Reporting API 在 Safari 16 仍不支持' },
];

// 🔴🔴 **闸的已知盲区,以及补它的那一层。别把这段当装饰。**
//
// eslint-plugin-compat 实测能抓什么、抓不到什么(2026-08-06 用探针逐条打出来的,不是推断):
//   ✅ 抓得到:静态命名空间调用 —— structuredClone / Object.hasOwn /
//             Crypto.randomUUID() / navigator.* 之类
//   ❌ 抓不到:① **原型方法**(`xs.at()` / `xs.toSorted()` / `xs.findLast()`)——
//                静态分析定不了 xs 的类型,插件放弃;
//             ② 比它自带数据集**更新**的 API —— `Object.groupBy` / `Promise.withResolvers`
//                在它那里根本没有条目,报都不会报。
//
// 也就是说:光靠插件,工单 §4.4「新代码用超标 API 必须红」只做到了一半。
// 下面这张表用纯文本扫描补上剩下那一半 —— 只收**歧义极小**的形态,
// 故意不收 `.at(` / `.with(`(太多同名业务方法,误报会让闸变成恒红,那和恒绿一样废)。
const BEYOND_BASELINE_PATTERNS = [
  // 🔴🔴 crypto.randomUUID 放在这一层,不是因为插件抓不到它 —— 而是因为插件**时灵时不灵**。
  //   2026-08-06 实测:在 ProviderDowngradeWizard.tsx 里,
  //     · 第 72/78 行的 `${crypto.randomUUID()}`(长行 · 在 headers 对象里)→ **不报**
  //     · 同一个文件、同一个函数里新插一行一模一样形态的调用(第 65 行)→ **报**
  //   同样形态在 DemoGrantPanel.tsx 和各种最小复现里都报。我没能刻画出这个盲区的边界。
  //   **刻画不出边界的工具,不能拿来当唯一判据** —— 否则「闸绿了」只是没被抓到,不是没有。
  //   文本扫描没有这类不确定性,所以这条由它兜底;插件那层重复报也无所谓。
  { re: /\bcrypto\s*\.\s*randomUUID\s*\(/, why: 'crypto.randomUUID 需 Chrome 92(底线是 90)→ 请改用 lib/safeRandomUUID.ts 的 safeRandomUUID()' },
  { re: /\b(?:Object|Map)\s*\.\s*groupBy\s*\(/, why: 'Object/Map.groupBy 需 Chrome 117 / Safari 17.4' },
  { re: /\bPromise\s*\.\s*withResolvers\s*\(/, why: 'Promise.withResolvers 需 Chrome 119 / Safari 17.4' },
  { re: /\bArray\s*\.\s*fromAsync\s*\(/, why: 'Array.fromAsync 需 Chrome 121 / Safari 16.4' },
  { re: /\.toSorted\s*\(/, why: 'Array.prototype.toSorted 需 Chrome 110 / Safari 16' },
  { re: /\.toReversed\s*\(/, why: 'Array.prototype.toReversed 需 Chrome 110 / Safari 16' },
  { re: /\.toSpliced\s*\(/, why: 'Array.prototype.toSpliced 需 Chrome 110 / Safari 16' },
  { re: /\.findLast\s*\(/, why: 'Array.prototype.findLast 需 Chrome 97(超 Chrome 90 底线)/ Safari 15.4(Safari 侧在线内)' },
  { re: /\.findLastIndex\s*\(/, why: 'Array.prototype.findLastIndex 需 Chrome 97(超底线)' },
];

// 🔴🔴 CSS 容器查询(`@container` + `@md:` / `@max-[880px]:` 之类变体)是**最危险的一类**:
// 🔴 2026-08-06 二次拍板后前提已变:Owner 把 Safari/iOS 底线从 15.4 抬到了 16,
//    容器查询(Safari 16.0 起)因此**落在底线之内**,原来的「静默退化」风险已经消失。
//    本闸**故意保留**,但它已从「安全闸」降级为「盘点闸」——
//    留着是因为它逼人确认新变体只做加法,这个习惯本身有价值。
//    🔴 要不要彻底移除,由 Review 定,不许 Deploy 顺手删。
//    (以下原始论证保留作历史:Safari <16 不支持容器查询,规则会被静默忽略。)
//
// 「fallback」在这里的具体含义:**容器查询只许做加法**。
// 也就是说,把所有 `@xxx:` 规则全部删掉之后,剩下的基础样式必须仍是一套完整可用的布局。
// 2026-08-06 逐条核过当时在用的 31 个变体,全部落在
// sticky/top/z/bottom/px/py/gap/grid-cols/max-w/self-start/border/-mx 这些**排版增强**上,
// 没有一个是「只有支持容器查询的浏览器才看得见正文」。
//
// 下面这张表就是当时那份清单。**它是一个盘点闸,不是黑名单**:
// 谁新加一个变体,这里就会红,逼他回来回答一句「把它删掉之后,基础样式还是一套完整布局吗」。
// 🔴 千万别把它当噪声顺手加进来 —— 它红的时候正是该做判断的时候。
const CONTAINER_QUERY_ALLOWLIST = new Set([
  // 🔴 [#153-B 2026-09-08] 原来这 11 条是**前缀式**(`@4xl:grid-cols-` 之类),
  //    语义是「该前缀下**任意** arbitrary value 都预先放行」—— 比精确匹配弱:
  //    新加一个值会**静默通过**,而本闸的命题正是「每个新变体都要被判一次」。
  //    换成**全串枚举**:11 条前缀 → **14 条**全串(+3)。
  //    理由一字未变(它们讲的是同一批类,只是从「前缀」写成「全串」),
  //    所以不需要重新判「删掉它还是完整布局吗」—— 那个判断当初就是对这批类做的。
  //    收益:今后任一**新的** arbitrary value 必然触发一次判断。
  // 🔴 [#153-B 返修 · 合流才出现的红] 这里原本还有一条
  //      @4xl:grid-cols- 加 minmax(0,1fr) 与 minmax(0,0.42fr) 那一条
  //    它的**唯一**使用者 `DouyinImagePost.tsx` 被同一班的 #150 §4 删掉了。
  //    两条分支各自都看不见:#153-B 那边它还有使用者,#150 那边它不在 allowlist 里 ——
  //    只有**合并树**上同时具备「条目在 + 使用者没了」⇒ 死条目臂(build 链内)才响。
  //    抓到它的正是 `ab6eb24e0` 那条非死臂,这就是它存在的理由。
  //
  //    🔴 **本笔只在合流后成立**:在**还有** `DouyinImagePost.tsx` 的树上单独跑本闸,
  //    它会正当地把那个变体报成「新的容器查询变体」。**那不是缺陷,别把条目加回来** ——
  //    加回来就是把合并树上的死条目放回去。判定以**合并树**为准。
  // 🔴 [#203 2026-09-13] 图文详情页三栏重排(设置 / 预览 / 重要内容)换了栏宽比。
  //    判过「删掉它还是完整布局吗」:删掉之后只剩 `grid gap-4` = **单列纵排**,
  //    三栏各自完整、顺序是 设置 → 预览 → 重要内容 —— 正是窄档该有的样子。⇒ 只做加法。
  //    旧那一条(0.86/0.72)同笔删除:它的唯一使用者已经不在了,
  //    留着不会红,却会让下一个人以为它还被用着。
  '@5xl:grid-cols-[minmax(0,0.62fr)_minmax(0,1fr)_minmax(0,0.9fr)]',
  // Step 3: base grid is a complete single column; wider containers add preview/editor columns.
  '@3xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]',
  '@5xl:max-h-[calc(100vh-2rem)]',
  '@min-[880px]:grid-cols-[minmax(0,1fr)_340px]',
  '@min-[880px]:max-w-[1240px]',
  '@min-[1184px]:min-w-[420px]',
  '@min-[1184px]:w-[112px]',
  '@min-[1184px]:w-[224px]',
  '@min-[1184px]:w-[380px]',
  '@min-[1600px]:grid-cols-[minmax(0,1fr)_380px]',
  '@min-[1600px]:max-w-[1440px]',
  '@min-[2200px]:grid-cols-[minmax(0,1fr)_420px]',
  '@min-[2200px]:max-w-[1760px]',
  '@min-[3000px]:max-w-[2000px]',
  // [🔴 #149 2026-09-08] 这 5 条随 `QuestionExampleCard.tsx` 一并退役 ——
  //   它是它们**唯一**的使用者,卡删了条目就成死条目。
  //   是本文件自己的「非死臂」(ab6eb24e0)把它们报出来的:删代码时**顺手留下死条目**
  //   正是那条臂要防的事,而它一声不吭就会变成「已登记因而没人回头核」。
  // 🔴 [#203] 三栏断点从 896 抬到 1024,这四个 sticky 类跟着换前缀。
  //    判过「删掉还是完整布局吗」:删掉之后中栏不再粘顶、跟着页面一起滚 ——
  //    正是窄档该有的样子,三栏内容一个不少。⇒ 只做加法。
  //    旧的 @4xl 那几条同笔删除:全树已无使用者,留着不会红却会被当成还在用。
  '@5xl:self-start', '@5xl:sticky', '@5xl:top-4',
  // 🔴 [#200] 体检页三档说明同屏:宽够就三列并排,窄了纵排。
  //    判过「删掉还是完整布局吗」:删掉之后只剩 `grid gap-2` = 三句**纵排一列**,
  //    每句前面带着自己的标签名,谁对谁一目了然。⇒ 只做加法。
  '@min-[560px]:grid-cols-3',
  // 超宽容器下把右栏钉住并限高滚动;不支持容器查询时整段忽略 = 该栏正常随页面流动
  '@5xl:overflow-y-auto',
  '@md:gap-3',
  '@sm:block',
  // [#125 2026-09-06] 窄档首屏预算:隐藏纯说明文案 + 收紧内距/行距。
  // 只做加法已核:删掉这 7 条 ⇒ 说明文字全部显示、内距回到 p-5/space-y-4,
  // 布局完整,只是首屏更长(= 今天线上的样子)。
  '@max-[880px]:hidden',
  '@max-[880px]:p-4', '@max-[880px]:pb-2', '@max-[880px]:pt-0', '@max-[880px]:py-2',
  '@max-[880px]:space-y-1', '@max-[880px]:space-y-2',
  // [#125 2026-09-06] 390 单列首屏重排:窄档把两列摊平成栅格项再逐块 order。
  //   **只做加法已核**:这七条全在 `@max-[880px]` 一侧,删掉后栅格回到两个项 ——
  //   桌面仍是两列(auto-flow)、窄屏仍是堆叠,即今天的布局,完整,只是没重排。
  //   🔴 刻意**不用** `@min-[880px]:col-start-*` 给桌面显式定列:那样删掉之后
  //   三个栅格项会自动流成「主列跑到右边」,**就不是加法了**,本闸会挡住它。
  '@max-[880px]:contents',
  '@max-[880px]:order-1', '@max-[880px]:order-2', '@max-[880px]:order-3',
  '@max-[880px]:order-4', '@max-[880px]:order-5', '@max-[880px]:order-6',
  // [🔴 #135 2026-09-08] arbitrary **property** 形态,本单之前对本闸**整类隐形**。
  //   只做加法已核:删掉它 ⇒ 主列在宽档不再是查询容器 ⇒ 里面的卡片按**整页宽**排
  //   而不是按主列宽排。布局**完整**,只是不优 —— 不支持容器查询的浏览器拿到的正是它。
  //   (它本身是 #125 的修复:窄档 `contents` 与容器身份不能共存,见 NewDiagnosis 那段注释。)
  '@min-[880px]:[container-type:inline-size]',
  '@min-[880px]:sticky', '@min-[880px]:top-6',
  // [WO_GAPPLAN_RELOCATION_A 2026-08-10] 交付计划任务卡的四列档。
  // 只做加法:删掉这四个变体后基础类仍是 `flex flex-col gap-4` + 三列各自
  // `min-w-0`,即完整的单列堆叠布局(不支持容器查询的浏览器拿到的正是它)。
  // 断点问容器不问视口的理由见 GapTaskCard.tsx 里那段注释。
  '@min-[1184px]:flex-row', '@min-[1184px]:items-start',
]);
// 🔴 [#135 2026-09-08] 两条 alternation,**不是**把 `[` 塞进原字符类。
//
//    原正则 `:` 后要求至少一个 `[A-Za-z0-9:_./-]`,于是:
//      · arbitrary **value**(`@4xl:grid-cols-[minmax(0,1fr)]`)在 `[` 处**截断**,
//        采到 `@4xl:grid-cols-` —— 一直可见,allowlist 里那些前缀条目就是这么来的;
//      · arbitrary **property**(`@min-[880px]:[container-type:inline-size]`)工具部分
//        **以 `[` 开头** ⇒ `+` 一个字符都匹配不到 ⇒ **整条不可见**,整类不在盘点分母里。
//        (是我 #125 加那条类时发现的:它对本闸隐形。)
//
//    🔴 修法只补第二类,**不动第一类的捕获形状**。把 `[` 塞进原字符类会让 14 条
//    arbitrary value 变体从「前缀」变成「全串」,当场作废 14 条**已逐条裁定过
//    「只做加法」**的 allowlist 条目、要求全部重判 —— 那不是修洞,是把做过的判断重做一遍。
// 🔴 [#153-B 2026-09-08] 采样器对 **arbitrary value 也收全串**。
//    #135 时这里是两支:①arbitrary property 整串收;②其余遇 `[` **截断成前缀**,
//    为的是让 allowlist 里 11 条**前缀式**条目继续成立。
//    但前缀条目的语义是「该前缀下**任意**值都预先放行」—— 新加 `@4xl:grid-cols-[任意值]`
//    会**静默通过**,而本闸的命题正是「每个新变体都要被判一次『删掉它还是完整布局吗』」。
//    实测:11 条前缀里 **10 条各自只覆盖 1 个真实全串**,换全串是 13 条 ⇒ 代价 +2 条。
//    🔴 我在 #135 说过「扩正则会作废 14 条已裁定条目」——那句**对当时那个提案成立**
//    (改字符类会同时改掉所有 token 的捕获形状),但**不等于收紧前缀本身贵**;
//    我当时把两件事混在一起量了,这里订正。
const CONTAINER_VARIANT_RE = new RegExp(
    '@[a-z0-9-]*\\[?[0-9]*p?x?\\]?:[A-Za-z0-9:_.%(),/\\[\\]-]+', 'g');

function checkContainerQueries() {
  const seen = new Set();
  for (const f of walkSrc()) {
    if (!/\.tsx$/.test(f)) continue;
    const src = fs.readFileSync(f, 'utf8');
    for (const m of src.matchAll(CONTAINER_VARIANT_RE)) seen.add(m[0]);
  }
  const findings = [];
  // ── 🔴 [#135] 正样本臂:证明取样器认得 **arbitrary property 这一类**,不是恰好认得那一个串。
  //    没有这一条的话,「新变体会红」这个命题对整类**结构性失明**而判据全绿 ——
  //    #125 那条 `[container-type:inline-size]` 就是这样在盘点表外躺了三天。
  //    两条**合成**的、树里不存在的串:都必须被完整捕获(不是截断)。
  {
    const SYNTH = ['@min-[880px]:[contain:layout]', '@2xl:[aspect-ratio:16/9]'];
    for (const t of SYNTH) {
      const got = t.match(CONTAINER_VARIANT_RE);
      if (!got || got[0] !== t) {
        findings.push(`盘点器自证失败:arbitrary property 形态 ${t} 没被完整捕获`
          + `(实得 ${JSON.stringify(got)})。这一类会整类隐形,先修取样器再谈结论。`);
      }
    }
    // 🔴 [#153-B 退役 + 接替] 这里原来是「arbitrary value 必须**仍然截断成前缀**」。
    //    它是 #135 的**过渡期不变式**:那时 allowlist 用前缀条目,截断是它们成立的前提。
    //    本单把前缀改成全串枚举 ⇒ 那条与正确做法**互斥**,留着就是两把方向相反的锁并存。
    //    ⇒ 退役,由这条**正样本臂**接替:arbitrary value 必须被**完整**捕获。
    for (const t of ['@4xl:grid-cols-[minmax(0,1fr)_340px]', '@min-[1184px]:w-[112px]']) {
      const got = t.match(CONTAINER_VARIANT_RE);
      if (!got || got[0] !== t) {
        findings.push(`盘点器自证失败:arbitrary value ${t} 没被**完整**捕获(实得 ${JSON.stringify(got)})。`
          + '全串枚举的 allowlist 依赖完整捕获;退回截断会让每个前缀下的新值静默通过。');
      }
    }
  }

  // 🔴 [#135 返修 2026-09-08] `findings` **必须先声明**。
  //    上一版自证块写在 `const findings = added.map(...)` **之前**,同一函数作用域 ⇒
  //    自证一旦失败,`findings.push` 撞 TDZ 抛 `ReferenceError`,**根本走不到报红**。
  //    顺利路径全绿(gate rc=0),所以三天里没人看得见 —— 「顺利路径看不见」那一类。
  //    🔴 更坏的是我的注毒**读错了信号**:Node 崩溃时会把出错那一行**源码原样打出来**,
  //    而那行里正好含我 grep 的那句话 ⇒ 「报文命中」为真,崩溃被当成判据在报红。
  //    ⇒ 先 `const findings = []`,顺序不再影响正确性。
  for (const v of [...seen].filter(v => !CONTAINER_QUERY_ALLOWLIST.has(v))) {
    findings.push(
      `新的容器查询变体 ${v} —— 底线已抬到 Safari 16,容器查询在线内;本闸只做盘点。` +
      '请确认把它删掉之后基础样式仍是完整布局(即它只做加法),确认后把它加进 ' +
      'scripts/verify-browser-baseline.mjs 的 CONTAINER_QUERY_ALLOWLIST。');
  }
  const added = [...seen].filter(v => !CONTAINER_QUERY_ALLOWLIST.has(v));

  // ── 🔴 非死臂:allowlist 每条都必须在 src 里真的命中 ──────────────
  //
  // 这张表是**盘点闸**,它的力量全部来自「新变体会红」。而一条**死条目**
  // (类早就删了、条目还留着)会安静地把这份力量削掉一角:
  //   · 它不红、不报错,没有任何人有理由回头看它;
  //   · 更糟的是,它已经**被登记过**,于是从「可疑」变成了「既定事实」——
  //     下一个人看到它只会以为「这条早就核过了」。
  // 2026-09-06 实测:pointer-fine 那 9 条的类在 560b9c4f1 已整组退役,
  // 条目却原样留着,而本闸一声不吭。⇒ 补这一臂。
  //
  // 🔴 自证在先:分母读坏了(src 一个都没扫到)会让**每一条**都像死的,
  //    那是仪器坏了不是表脏了 —— 所以先要求 seen 有合理体量,再谈死条目。
  const dead = [...CONTAINER_QUERY_ALLOWLIST].filter(v => !seen.has(v));
  if (seen.size < 20) {
    findings.push(
      `盘点器自证失败:src 里只采到 ${seen.size} 个容器查询变体(预期 ≥20)。` +
      '这多半是取样器坏了,而不是变体真的消失了 —— 先修仪器,别改表。');
  } else if (dead.length > 0) {
    findings.push(
      `CONTAINER_QUERY_ALLOWLIST 有 ${dead.length} 条死条目(类已不在 src 里):` +
      `${dead.join(' / ')} —— 请删掉它们。死条目不会红,也就没人回头核它,` +
      '而它「已被登记」这件事会让下一个人以为它早就核过了。');
  }
  // 反向对照:编造一个不可能出现的变体,它必须**不在** seen 里
  // (若 seen 变成了"什么都有",上面的死条目检查会恒绿)。
  if (seen.has('@zz-[9999px]:never-used-anywhere')) {
    findings.push('盘点器反向对照失败:seen 命中了一个编造的变体 ⇒ 采样面失控,死条目检查不可信。');
  }
  return findings;
}

function readPkg() {
  return JSON.parse(fs.readFileSync(PKG, 'utf8'));
}

function runGate() {
  // --quiet:只要 error。noInlineConfig 产生的 warning 是噪声,不参与判定。
  // 🔴 maxBuffer 必须放大:自证的反向对照会把底线收紧到 chrome 60,eslint 的 JSON 报告
  //    带上 source 字段能到几十 MB。默认 1MB 会 ENOBUFS → e.stdout 被**截断成半截 JSON**,
  //    半截 JSON 仍然以 '[' 开头 → 看着像正常输出 → JSON.parse 崩在这里,
  //    表现成「脚本自己炸了」而不是「闸报了多少条」。这正是自证第一版踩到的坑。
  const OPTS = {
    cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
    shell: process.platform === 'win32', maxBuffer: 256 * 1024 * 1024,
  };
  const parse = (out, fallbackMsg) => {
    try {
      return JSON.parse(out).flatMap(f => f.messages.map(m => ({
        file: path.relative(ROOT, f.filePath).replace(/\\/g, '/'), line: m.line, msg: m.message,
      })));
    } catch {
      console.error('闸的输出解析不了(不是代码违规):\n' + fallbackMsg.slice(0, 500));
      process.exit(2);
    }
  };
  try {
    const out = execFileSync('npx', ['eslint', 'src', '--no-warn-ignored', '--quiet', '-f', 'json'], OPTS);
    return out.trim().startsWith('[') ? parse(out.trim(), out) : [];
  } catch (e) {
    const out = (e.stdout || '').trim();
    if (!out.startsWith('[')) {
      console.error('闸自己跑挂了(不是代码违规):\n' + (e.stderr || e.message));
      process.exit(2);
    }
    return parse(out, e.stderr || e.message);
  }
}

function walkSrc() {
  const files = [];
  const stack = [path.join(ROOT, 'src')];
  while (stack.length) {
    const dir = stack.pop();
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, entry.name);
      if (entry.isDirectory()) stack.push(p);
      else if (/\.(ts|tsx|js|jsx|mjs)$/.test(entry.name)) files.push(p);
    }
  }
  return files;
}

// 🔴 唯一允许出现 `crypto.randomUUID(` 的文件:它就是那个兜底工具本身。
//    豁免只给这一个文件,而且下面 checkWrapperIntegrity() 会反过来断言
//    「这个文件里三层退化一层不少」—— 豁免不是垃圾桶,它配着一条反向锁。
const RANDOM_UUID_WRAPPER = 'src/lib/safeRandomUUID.ts';

/**
 * 把注释清成空格,保留长度与换行(行号必须还能对得上)。
 *
 * 🔴 为什么非做不可:第一版没剥注释,结果闸红在 `authoritativeSession.ts:94` ——
 *    那一行是**我自己写的注释**「原来这里首选 crypto.randomUUID()」。
 *    锁抓到自己写的解释文字,是「判据自坏」里最常见的一种。
 * 🔴 剥的时候必须认字符串:`'https://x'` 里的 `//` 不是注释,
 *    把它当注释会从那里一路吞到行尾 —— 那就从误报变成了漏报,更糟。
 */
function stripComments(text) {
  const out = text.split('');
  let i = 0;
  const n = text.length;
  while (i < n) {
    const ch = text[i];
    const nx = text[i + 1];
    if (ch === '/' && nx === '/') {
      while (i < n && text[i] !== '\n') out[i++] = ' ';
      continue;
    }
    if (ch === '/' && nx === '*') {
      while (i < n && !(text[i] === '*' && text[i + 1] === '/')) {
        if (text[i] !== '\n') out[i] = ' ';
        i += 1;
      }
      if (i < n) { out[i] = ' '; out[i + 1] = ' '; i += 2; }
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      const q = ch;
      i += 1;
      while (i < n) {
        if (text[i] === '\\') { i += 2; continue; }
        if (text[i] === q) break;
        if (q !== '`' && text[i] === '\n') break;  // 普通字符串不跨行
        i += 1;
      }
      i += 1;
      continue;
    }
    i += 1;
  }
  return out.join('');
}

function scanText(patterns, files = walkSrc()) {
  const bad = [];
  for (const f of files) {
    if (path.relative(ROOT, f).replace(/\\/g, '/') === RANDOM_UUID_WRAPPER) continue;
    const src = stripComments(fs.readFileSync(f, 'utf8'));
    const lines = src.split('\n');
    for (const { re, why } of patterns) {
      lines.forEach((line, i) => {
        if (re.test(line)) {
          bad.push(`${path.relative(ROOT, f).replace(/\\/g, '/')}:${i + 1}  ${why}`);
        }
      });
    }
  }
  return bad;
}

function checkForbiddenRealUses() {
  return scanText(FORBIDDEN_REAL_USES);
}

function checkBeyondBaseline() {
  return scanText(BEYOND_BASELINE_PATTERNS);
}

/** 豁免的反向锁:那个被豁免的文件必须**真的**是三层退化的兜底,不能只剩一层。 */
function checkWrapperIntegrity() {
  const p = path.join(ROOT, RANDOM_UUID_WRAPPER);
  if (!fs.existsSync(p)) return [`${RANDOM_UUID_WRAPPER} 不见了 —— 全站 UUID 兜底没了`];
  const src = fs.readFileSync(p, 'utf8');
  const need = [
    ['crypto.randomUUID', /\brandomUUID\s*===\s*'function'|typeof\s+c\.randomUUID/],
    ['getRandomValues 退化层', /getRandomValues/],
    ['Math.random 兜底层', /Math\.random\(\)/],
  ];
  return need.filter(([, re]) => !re.test(src))
    .map(([name]) => `${RANDOM_UUID_WRAPPER} 缺少「${name}」—— 它是唯一被豁免的文件,退化层不能少`);
}

function checkBaselineDeclared() {
  const pkg = readPkg();
  const got = pkg.browserslist;
  if (!Array.isArray(got)) return ['package.json 缺 browserslist —— 没有底线就没有闸'];
  const a = JSON.stringify(got), b = JSON.stringify(EXPECTED_BROWSERSLIST);
  if (a !== b) {
    return [`browserslist 与 Owner 拍板的底线不一致\n    实际: ${a}\n    应为: ${b}`];
  }
  return [];
}

// ---------------------------------------------------------------- selftest
function selftest() {
  let failed = 0;
  const t = (name, ok, detail = '') => {
    console.log(`  ${ok ? '✅' : '🔴'} ${name}${detail ? ' — ' + detail : ''}`);
    if (!ok) failed = 1;
  };

  console.log('=== 判别力自证(十一条 · 三条正样本 + 八条反向/自坏对照)===');

  // (a) 正样本之一:eslint 插件那一层。
  //     🔴 用 `Object.hasOwn`(Chrome 92 > 底线 90)而**不是** `Object.groupBy` ——
  //     实测插件的数据集里根本没有 groupBy 条目,拿它当正样本会得到「闸坏了」的假结论。
  //     选正样本必须先证它真能触发,否则自证本身就是坏的。
  const probe = path.join(ROOT, 'src', '__baseline_probe__.ts');
  fs.writeFileSync(probe, 'export const g = (o: object) => Object.hasOwn(o, "x");\n', 'utf8');
  let probeHits;
  try {
    probeHits = runGate().filter(m => m.file.includes('__baseline_probe__'));
  } finally {
    fs.unlinkSync(probe);
  }
  t('正样本(插件层)Object.hasOwn 被抓到', probeHits.length > 0,
    probeHits.length ? probeHits[0].msg : '闸对故意植入的超标 API 视而不见 → 恒绿,整节作废');

  // (a2) 正样本之二:补盲区的文本扫描那一层。插件抓不到 Object.groupBy / .toSorted,
  //      这一层必须抓到,否则「盲区已补」是句空话。
  const probe2 = path.join(ROOT, 'src', '__baseline_probe2__.ts');
  fs.writeFileSync(probe2,
    'export const a = (xs: number[]) => Object.groupBy(xs, String);\n' +
    'export const b = (xs: number[]) => xs.toSorted();\n', 'utf8');
  let probe2Hits;
  try {
    probe2Hits = checkBeyondBaseline().filter(s => s.includes('__baseline_probe2__'));
  } finally {
    fs.unlinkSync(probe2);
  }
  t('正样本(补盲区层)Object.groupBy + .toSorted 被抓到', probe2Hits.length >= 2,
    probe2Hits.length ? probe2Hits.join(' / ') : '补盲区的扫描没生效 → 插件盲区仍是敞的');

  // (a3) 必须不命中:干净代码不许被补盲区的扫描误伤(恒红和恒绿一样废)
  t('反向:现有代码不被补盲区扫描误伤', checkBeyondBaseline().length === 0,
    checkBeyondBaseline().slice(0, 3).join(' / '));

  // (a4) 容器查询盘点闸:植入一个清单外的变体,必须红;现有代码必须不红。
  const probe3 = path.join(ROOT, 'src', '__baseline_probe3__.tsx');
  fs.writeFileSync(probe3, 'export const A = () => <div className="@lg:hidden" />;' + String.fromCharCode(10), 'utf8');
  let probe3Hits;
  try {
    probe3Hits = checkContainerQueries();
  } finally {
    fs.unlinkSync(probe3);
  }
  t('容器查询盘点闸:清单外的新变体被抓到', probe3Hits.length > 0,
    probe3Hits.length ? probe3Hits[0].slice(0, 60) : '盘点闸没生效 → 新容器查询可以随便加,没人再确认它是否只做加法');
  t('反向:现有容器查询全在清单内', checkContainerQueries().length === 0,
    checkContainerQueries().slice(0, 2).join(' / '));

  // (b) 反向对照:底线收紧成 chrome 60,闸必须大面积红
  const original = fs.readFileSync(PKG, 'utf8');
  let tightened;
  try {
    const pkg = JSON.parse(original);
    pkg.browserslist = ['chrome 60'];
    fs.writeFileSync(PKG, JSON.stringify(pkg, null, 4) + '\n', 'utf8');
    tightened = runGate().length;
  } finally {
    fs.writeFileSync(PKG, original, 'utf8');
  }
  const loose = runGate().length;
  t('反向对照:底线收紧后闸大面积报红', tightened > loose + 10,
    `chrome60=${tightened} 条 vs 现底线=${loose} 条` +
    (tightened > loose + 10 ? '' : ' → 闸没有真读 browserslist'));

  // (c) 豁免反向锁
  t('豁免反向锁:Report/Notification 的真调用形态确实一处都没有',
    checkForbiddenRealUses().length === 0);

  t('豁免反向锁:safeRandomUUID 兜底三层一层不少', checkWrapperIntegrity().length === 0,
    checkWrapperIntegrity().join(' / '));

  // (h) 剥注释这一步自己的判别力:注释里的调用不算,字符串里的 `//` 不许被当注释吞掉。
  const stripped = stripComments(
    "// 旧写法 crypto.randomUUID() 已废弃\n" +
    "const u = 'https://x/y'; const KEEP = crypto.randomUUID();\n" +
    "/* 块注释里也提到 Object.groupBy( */\n");
  t('剥注释:注释里的 crypto.randomUUID 被剥掉', !/\/\/ 旧写法/.test(stripped) && (stripped.match(/randomUUID/g) || []).length === 1);
  t("剥注释:字符串里的 // 不被当注释(否则会把后面的真调用一起吞掉)",
    /KEEP\s*=\s*crypto\.randomUUID/.test(stripped));
  t('剥注释:保长度保换行(行号要对得上)',
    stripped.length === ("// 旧写法 crypto.randomUUID() 已废弃\n" +
      "const u = 'https://x/y'; const KEEP = crypto.randomUUID();\n" +
      "/* 块注释里也提到 Object.groupBy( */\n").length);

  console.log(failed ? '\n🔴 自证不通过' : '\n✅ 自证通过');
  return failed;
}

// ---------------------------------------------------------------- main
if (process.argv.includes('--selftest')) {
  process.exit(selftest());
}

const problems = [...checkBaselineDeclared(), ...checkForbiddenRealUses(), ...checkBeyondBaseline(), ...checkContainerQueries(), ...checkWrapperIntegrity()];
const violations = runGate();
if (problems.length || violations.length) {
  console.error('🔴 浏览器支持底线闸不通过\n');
  for (const p of problems) console.error('  ' + p);
  for (const v of violations) console.error(`  ${v.file}:${v.line}  ${v.msg}`);
  console.error(`\n共 ${problems.length + violations.length} 条。底线见 package.json 的 browserslist(Owner 2026-08-06 拍板)。`);
  console.error('低于底线的浏览器由 src/lib/browserBaseline.ts 给明确升级提示 —— 不许白屏,也不许静默降级。');
  process.exit(1);
}
console.log('✅ 浏览器支持底线闸通过(browserslist 与 Owner 底线一致 · src 无超标 API · 豁免反向锁未响)');
