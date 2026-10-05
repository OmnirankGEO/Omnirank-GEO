/**
 * 注毒前置:**下毒之后先验语法** —— 一处定义,所有 runner 共用。
 *
 * ## 它防的是什么
 *
 * 几乎所有 runner 都用 `caught = rc === 1` 判「这发毒被抓住了」。
 * 而一发**把文件写成语法错**的毒,同样会让门 rc=1 ——
 * 于是 runner 报「CAUGHT」,而实际上**门什么都没测到**。
 *
 * 🔴 「毒没下成」与「锁咬住了」在 rc 上**完全同形**。
 *    这不是假想:C 2026-09-20 自陈他第一版的 helper 毒就是语法错,
 *    runner 读成 CAUGHT,差点据此下结论。我全部 runner 都有同一个洞。
 *
 * ## 🔴 这个文件自己栽过一次(2026-09-20 第一版)
 *
 * 第一版给 `.ts` 传了 `jsx: ts.JsxEmit.None`(枚举值 0)——
 * 这不是**语法**问题,而是 `transpileModule` 拒收的**选项值**:
 * 它对每个文件都回一条 `TS6046`,于是 `syntaxOk()` **恒 false**,
 * 22 发本来落得好好的毒全被报成「没下成」。
 *
 * 形状 = 本仓 `everything-looks-dead-means-the-scanner-died`:
 * **全同的读数 = 尺子坏了**。而那一发牙证(故意写坏语法)照样「通过」——
 * 一把说「全世界都是语法错」的尺子,牙证是满分的。
 *
 * ⇒ 牙证只证明它**会红**,不证明它**会绿**。两条都要:
 *    · `expectSyntaxFail` 那一发 —— 证明它会红;
 *    · `assertRulerWorks(原文)` —— 证明它对**没动过的文件**会绿(对照臂)。
 *    每个 runner 下毒**之前**先跑对照臂,不过就 rc=3(尺子坏了,不是判据红了)。
 *
 * ## 用法
 *
 * ```js
 * import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';
 * assertRulerWorks(p.file);              // 下毒前:原文必须过
 * writeFileSync(p.file, poisoned);
 * if (!syntaxOk(p.file)) { ...「没下成」,不算 CAUGHT... }
 * ```
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require_ = createRequire(import.meta.url);

/**
 * 只看**语法**诊断。
 *
 * 🔴 判据是「诊断带不带 `d.file`」:语法诊断一定指向源文件里的某个位置;
 *    选项类诊断(TS6046 之类)是**没有 file 的** —— 第一版栽的正是这一类。
 *    类型错不算(那是 `tsc` 的活,而注毒常常刻意制造类型不匹配)。
 *
 * @param {string} file 绝对路径;`.ts` / `.tsx` / `.js` / `.mjs` 都能过
 * @returns {boolean} 语法是否成立
 */
export function syntaxOk(file) {
    return syntaxDiagnostics(file).length === 0;
}

/**
 * 这把尺子**够得着**哪些文件。
 *
 * 🔴 2026-09-20 实测:`mutation_runner_ios_touch_ux` 有一发毒打的是 `src/index.css`,
 *    而这把尺子拿 TypeScript 去 parse 它 ⇒ 一堆 `Expression expected` ⇒
 *    对照臂喊「没下毒的文件就判不过 ⇒ 尺子坏了」。尺子没坏,**是它够不着**。
 *    「够不着」必须与「坏了」分开,否则这条自陈本身又是一次同形混淆。
 *
 * ⚠️ **自陈盲区**:`.css` / `.json` / `.md` 这些不在射程内 ——
 *    对它们下的毒如果写坏了语法,本前置**看不见**,那一发仍可能被读成 CAUGHT。
 *    这不是"已覆盖",是覆盖不到;要补得换一把对应语言的尺子。
 */
const JUDGEABLE = /\.(ts|tsx|js|jsx|mjs|cjs)$/i;
export function canJudge(file) { return JUDGEABLE.test(file); }

/** 语法诊断明细(给报错时打印用 —— 「没下成」总该说清哪一行)。 */
export function syntaxDiagnostics(file) {
    if (!canJudge(file)) return [];
    const ts = require_('typescript');
    const src = readFileSync(file, 'utf8');
    const isTsx = file.endsWith('.tsx');
    const compilerOptions = {
        module: ts.ModuleKind.ESNext,
        target: ts.ScriptTarget.ES2020,
        allowJs: true,
    };
    /* 🔴 只有 tsx 才传 jsx —— 给 .ts 传 `None` 会被当成非法选项值(见抬头)。 */
    if (isTsx) compilerOptions.jsx = ts.JsxEmit.Preserve;
    const out = ts.transpileModule(src, {
        compilerOptions,
        reportDiagnostics: true,
        fileName: isTsx ? 'x.tsx' : 'x.ts',
    });
    return (out.diagnostics || []).filter((d) => d.file);
}

/**
 * 🔴 对照臂:**没动过的文件必须是绿的**。
 *
 * 少了这一条,一把恒 false 的尺子会把每一发毒都报成「没下成」——
 * 而这个读数跟「毒确实没下成」长得一模一样。
 *
 * @throws {Error} 尺子坏了(调用方应当以 rc=3 退出:门没跑成,不是判据红了)
 */
export function assertRulerWorks(file) {
    /* 够不着的文件不参与对照 —— 它不是"好"也不是"坏",是不在射程内。 */
    if (!canJudge(file)) return true;
    const diags = syntaxDiagnostics(file);
    if (diags.length === 0) return true;
    const ts = require_('typescript');
    const where = diags.map((d) => `${d.code}:${ts.flattenDiagnosticMessageText(d.messageText, ' ')}`).join(' · ');
    throw new Error(
        `🔴 语法尺子对照臂失败:**没下毒**的 ${file} 就判不过 ⇒ 是尺子坏了,不是毒没下成。${where}`,
    );
}

/**
 * 一步到位:**对照臂 → 落盘 → 验语法**。给存量 runner 接线用。
 *
 * 存量那 38 个 runner 的写法五花八门(`fs.writeFileSync` / 裸 `writeFileSync` /
 * `SRC0[p.file]` 映射 / 一次写多份),逐个手改三段代码,改错的概率比缺陷本身还高。
 * 这里把三步收成一个调用,每个 runner 只需要把**那一行落盘**换掉。
 *
 * @param {string} file     被下毒的文件(绝对路径)
 * @param {string} poisoned 下毒后的完整内容
 * @returns {boolean} 语法是否成立 —— `false` = **这一发没下成**,不许算 CAUGHT
 * @throws {Error} 对照臂失败(没动过的文件就判不过)⇒ 尺子坏了,调用方应当 rc=3
 */
export function landPoison(file, poisoned) {
    /* 🔴 对照臂在**落盘之前**:此刻盘上还是原文。落盘之后再验就分不出
          「尺子坏了」和「毒写坏了」—— 那正是这整套东西要防的同形。 */
    assertRulerWorks(file);
    writeFileSync(file, poisoned, 'utf8');
    return syntaxOk(file);
}

/** 一次写多份时的版本。任一份语法不过即判「没下成」,并回那一份的路径。 */
export function landPoisons(entries) {
    for (const [file] of entries) assertRulerWorks(file);
    for (const [file, content] of entries) writeFileSync(file, content, 'utf8');
    const bad = entries.map(([file]) => file).filter((file) => !syntaxOk(file));
    return { ok: bad.length === 0, bad };
}

/**
 * 🔴 **牙证**:证明这道前置在**这个 runner 里**真的会红。
 *
 * 少了它,`syntaxOk()` 平时永远返回 true,**没有任何东西证明它在工作** ——
 * 而一把恒 true 的尺子与「所有毒都下成了」读数完全同形。
 * (对照臂 `assertRulerWorks` 管的是反方向:恒 false。两条臂缺一不可。)
 *
 * 做法:拿这个 runner **自己的目标文件**,故意写坏语法,要求前置认出来;
 * 无论成败都逐字还原。任何一步不对就抛 —— 不返回布尔值,免得调用方忘了看。
 *
 * @param {string} file 该 runner 的任一 judgeable 目标文件(绝对路径)
 * @param {(m: string) => void} [say] 打印函数(有的 runner 自己有 say)
 * @throws {Error} 牙证下不成(找不到锚)/ 牙证失败(前置没拦住)
 */
export function proveGuardHasTeeth(file, say = console.log) {
    if (!canJudge(file)) throw new Error(`牙证选错了文件:${file} 不在这把尺子的射程内`);
    const original = readFileSync(file, 'utf8');
    /*
     * 🔴 **不钉任何锚**:在文件末尾多加一个收口 `}`。
     *    任何模块多一个顶层 `}` 都是语法错,与文件内容、命名、重构全无关 ——
     *    也就没有"锚哪天失效了"这回事(本仓最常见的哑火方式)。
     *
     * 试过两种钉锚的写法,都不成立,留在这里免得有人再试一遍:
     *   · 裸 `export ` —— **先命中注释里那一处**,下毒后语法照样通过;
     *   · 行首 `\nexport ` → `export export …` —— TS **不报语法错**(实测通过),
     *     牙证于是自己失败了(幸好它是抛异常而不是返回布尔值)。
     */
    const broken = `${original}\n} /* 牙证:故意多一个收口 */\n`;
    try {
        writeFileSync(file, broken, 'utf8');
        if (syntaxOk(file)) {
            throw new Error(`🔴 牙证失败:${file} 被故意写坏语法,而前置判它**通过** —— 那道前置是瞎的`);
        }
        say(`  ✅ 牙证:故意写坏语法的一发被语法前置拦下(${file.split(/[\\/]/).pop()})`);
    } finally {
        writeFileSync(file, original, 'utf8');
    }
}

/**
 * 给 runner 的统一措辞 —— 免得每个 runner 自己编一句,
 * 而「没下成」这三个字正是最不该各写各的。
 */
export const NOT_LANDED_SYNTAX =
    '🔴 毒把文件写成了语法错 ⇒ **这一发没下成**,不算 CAUGHT(需重写这发毒)';
