#!/usr/bin/env node
/**
 * 判据 · 搜索词预填语义(订正三十① 及其补)。**纯行为臂**:import 真模块跑真输入。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 这一批刻意**不含任何 HTTP**:C 的端点契约还没定形,但语义已经定死。
 *    先把语义钉住,契约到了只接一根线 —— 免得"等契约"变成"语义也没人验"。
 */
let bad = 0;
let notEvaluated = 0;
const skipped = [];
/**
 * 🔴 三类未评估,处置各不相同,不许并档:
 *   · ENV_SKIPS     环境缺东西(库/dist/基线)—— 补环境就能评
 *   · PENDING_SKIPS **依赖方还没交**(C 的只读端点)—— 端点一落地,那个分支自动
 *                    变成真检查,这条 skip 会自己消失;不会烂在这儿
 *   · 其余          锚失效 —— 必红
 */
const ENV_SKIPS = [];
const PENDING_SKIPS = ['G2'];
const ok = (cond, label) => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}`);
    if (!cond) bad += 1;
};

const m = await import(new URL('../src/pages/Diagnosis/launch/searchTermsPrefill.ts', import.meta.url).href)
    .catch(() => null);
if (!m) {
    notEvaluated += 1; skipped.push('A0');
    console.log('  ⚠️ A0 **未评估**:没能 import searchTermsPrefill.ts。');
} else {
    // ── A dirty 语义:回填不许冲掉她打的字 ────────────────────────────
    ok(m.shouldAcceptRefill(false) === true, 'A1 正样本臂:未改动时接受回填(否则下一条不携带信息)');
    ok(m.shouldAcceptRefill(true) === false,
        'A2 🔴 已改动 ⇒ **拒绝回填**。晚到 200ms 的响应会把整框冲掉,'
        + '而屏幕上只表现为「字自己变了」—— 不报错、没得点回去,她只会以为自己手滑');

    // ── B 三态提交决策:「取到空数组」与「没取到」不许并档 ──────────────
    const READY = (t, s) => ({ kind: 'ready', terms: t, source: s });
    const r1 = m.keywordsForSubmit(READY(['a', 'b'], 'last_diagnosis'), false, []);
    ok(r1.send === true && r1.terms.join(',') === 'a,b',
        'B1 🔴 没改 ⇒ 带**屏幕上显示的那一份**(不留空让服务端在提交时刻再派生一次)');
    const r2 = m.keywordsForSubmit(READY(['a'], 'x'), true, ['我改的']);
    ok(r2.send === true && r2.terms.join(',') === '我改的',
        'B2 🔴 改过 ⇒ 以她改后的为准(Owner 原话:按客户改动作为标准)');
    const r3 = m.keywordsForSubmit(READY([], 'none'), false, []);
    const r4 = m.keywordsForSubmit({ kind: 'unavailable' }, false, []);
    ok(r3.send === true && r4.send === false,
        'B3 🔴 **「取到空数组」与「没取到」分开**:前者带空(她看到的就是没有词)、'
        + '后者不带(她什么都没看到,交服务端派生)。两者在"框里是空的"这点上完全同形,而处置相反');
    ok(r3.why !== r4.why, 'B3 配套:两档给出的理由不同(理由相同 ⇒ 上一条可能只是巧合)');
    const r5 = m.keywordsForSubmit({ kind: 'loading' }, false, []);
    ok(r5.send === false, 'B4 还在取 ⇒ 不带(此刻本来也不该能提交,门会拦)');
    const r6 = m.keywordsForSubmit(READY(['a'], 'x'), true, []);
    ok(r6.send === true && r6.terms.length === 0,
        'B5 🔴 她把词**全删光**也算她的选择:带空、不回落到预填值');

    // ── C 换品牌:替换但出声 ─────────────────────────────────────────
    ok(m.brandSwitchNotice('甲客户', '乙客户', false) === null,
        'C1 没改过 ⇒ 不出声(没什么可交代的,别制造噪音)');
    const n = m.brandSwitchNotice('甲客户', '乙客户', true);
    ok(typeof n === 'string' && n.includes('甲客户') && n.includes('乙客户'),
        `C2 🔴 改过 ⇒ 说清**两个品牌名**,不是一句笼统的"已更新"(实得 ${JSON.stringify(n)})`);
    ok(typeof n === 'string' && n.includes('没有被提交过'),
        'C3 🔴 明说她那份**没被提交过** —— 否则她会担心刚才的改动是不是已经花钱跑了');
    const n2 = m.brandSwitchNotice(null, null, true);
    ok(typeof n2 === 'string' && n2.length > 6,
        'C4 品牌名缺失时仍给出可读的一句(不能渲染成「已换成的词」这种断句)');

    // ── D 来源映射(Review 裁定两句)+ 总覆盖 ──────────────────────────
    ok(m.sourceLabel('last_diagnosis', false) === '上次体检的词', 'D1 上次诊断那一级的对客说法');
    ok(m.sourceLabel('brand_name', false) === m.sourceLabel('industry', false),
        'D2 品牌名/行业两级对客合并成同一句(不暴露内部阶梯)');
    ok(m.sourceLabel('last_diagnosis', true) === 'AI 按品牌信息规划',
        'D3 🔴 混合态(上次的词 + AI 追加)说 AI 那句 —— Review 裁的安全侧:'
        + '少说复用的那部分不会误导,说成「上次体检的词」则是**具体但错误**'
        + '(框里有她上次根本没见过的词)');
    ok(m.sourceLabel('none', false) === '', 'D4 确实没有词 ⇒ 不显示标签(与"未知"分开)');
    const unknown = m.sourceLabel('zzq_契约里没有的新档', false);
    ok(unknown.length > 0,
        'D5 🔴 未知档也要给一句话 —— 渲染空白 = 标签整块消失,'
        + '而"标签没出现"与"这一档不该有标签"在屏幕上同形');
    ok(unknown === m.sourceLabel('brand_name', false),
        'D6 🔴 未知档给**笼统但为真**的那句,不是某个具体来源');

    // ── F 空态三分:三种「框里是空的」说不同的话 ────────────────────────
    /**
     * 🔴 三态在屏幕上完全同形(框都是空的),处置却不同:
     *    · unavailable(没取到)⇒ 不带 keywords,服务端派生,「不填也行」为真
     *    · ready+none(取到了但确实没有词)⇒ 服务端也派生不出 ⇒ **提交必 422**,
     *      「不填也行」在这一态是**必然为假**的承诺
     *    · ready+有词 ⇒ 无空态文案
     */
    const fU = m.emptyStateNotice({ kind: 'unavailable' });
    const fN = m.emptyStateNotice({ kind: 'ready', terms: [], source: 'none' });
    const fR = m.emptyStateNotice({ kind: 'ready', terms: ['a'], source: 'brand_name' });
    ok(fU.text !== fN.text && fU.text.length > 0 && fN.text.length > 0,
        `F1 🔴 「没取到」与「确实没有词」说**不同**的话(实得 ${JSON.stringify([fU.text, fN.text])})`);
    ok(fU.mustFill === false && fN.mustFill === true,
        'F2 🔴 只有「确实没有词」那一态要求她必须填 —— 因为 C 的契约写明三源皆空时提交路径 raise 422');
    ok(!fN.text.includes('不填也行'),
        'F3 🔴 那一态**不许**出现「不填也行」—— 它在这一态必然为假,她照做就撞一条像系统故障的 422');
    ok(fR.text === '' && fR.mustFill === false, 'F4 有词时不显示空态文案(别制造噪音)');
    ok(fN.text === '这个客户还没有可用的搜索词,先填 1 个再开始(一个词就够)',
        'F5 🔴 none 文案与 Review 裁定的字面**逐字相同**(它进了 copy 登记,'
        + '改字面必须同笔改这里 —— 不然登记的和跑的是两句话)');
}

// ── E 反臂:本模块不许自己发请求(HTTP 归接线那一笔)────────────────
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const SRC = readFileSync(join(ROOT, 'src/pages/Diagnosis/launch/searchTermsPrefill.ts'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
ok(!/fetch|authFetch|axios|XMLHttpRequest/.test(SRC),
    'E1 🔴 纯语义模块零 HTTP —— 契约未定形时先钉语义,接线是另一笔');
ok(SRC.includes('keywordsForSubmit') && SRC.includes('shouldAcceptRefill'),
    'E1 配套正样本臂:确实读到了模块内容(读空会让上一条恒真)');

// ── G 🔴 跨层同一性:预填与提交出自**同一份阶梯** ────────────────────
/**
 * 🔴 C 把阶梯抽成 `resolve_keywords_with_source`(**唯一实现**),
 *    `resolve_keywords` 降为薄壳。所以锁要**成对**(C 的建议,采纳):
 *    ① 端点里恰 1 处 `_kw, _src = resolve_keywords_with_source(...)` —— 锁**赋值目标同一性**,
 *       不是"有没有这个调用"(有人换个赋值目标照样绿);
 *    ② `resolve_keywords` 仍是薄壳:去掉 docstring 后**只剩一条 return**,且 return 里
 *       含 `resolve_keywords_with_source`。少了 ②,「阶梯只有一份」就是假的 ——
 *       有人在薄壳里加两行逻辑,①照绿。
 *
 * 🔴 锁「同一个函数」**不锁同样的 HTTP 结果**:三源皆空时提交 422 / 预填 none+200
 *    是**故意的呈现差异**,锁 HTTP 结果会把一个正确的设计判红。
 */
const PY2 = `
import ast, json, io
src = io.open("server.py", encoding="utf-8").read()
tree = ast.parse(src)
out = {"endpoint_pairs": 0, "shell_ok": None, "shell_stmts": None, "impl_exists": ("def resolve_keywords_with_source" in io.open("services/diagnosis_keyword_source.py", encoding="utf-8").read())}
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "diagnosis_keyword_suggestion":
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call):
                f = sub.value.func
                nm = getattr(f, "id", None) or getattr(f, "attr", None)
                tgt = sub.targets[0]
                names = [e.id for e in tgt.elts] if isinstance(tgt, ast.Tuple) else []
                if nm == "resolve_keywords_with_source" and len(names) == 2:
                    out["endpoint_pairs"] += 1
svc = io.open("services/diagnosis_keyword_source.py", encoding="utf-8").read()
st = ast.parse(svc)
for node in ast.walk(st):
    if isinstance(node, ast.FunctionDef) and node.name == "resolve_keywords":
        body = [b for b in node.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
        out["shell_stmts"] = len(body)
        out["shell_ok"] = (len(body) == 1 and isinstance(body[0], ast.Return)
                           and "resolve_keywords_with_source" in ast.dump(body[0]))
print(json.dumps(out))
`;
const { spawnSync } = await import('node:child_process');
const r2 = spawnSync('python', ['-c', PY2], { cwd: REPO, encoding: 'utf8', timeout: 60000 });
let g = null;
try { g = JSON.parse((r2.stdout || '').trim().split(String.fromCharCode(10)).pop() || ''); } catch { /* below */ }
if (!g) {
    notEvaluated += 1; skipped.push('G0');
    console.log(`  ⚠️ G0 **未评估**:AST 探针没跑成(exit=${r2.status}) ${(r2.stderr || '').slice(-160)}`);
} else if (g.shell_stmts === null && !g.impl_exists) {
    // 🔴 两种"找不到"处置相反,不许并档:
    //    · 唯一实现也不在 ⇒ 阶梯**还没交到本树** ⇒ 未评估
    //    · 唯一实现在、薄壳没了 ⇒ 薄壳**被人删/改名了** ⇒ 这是缺陷,必须红
    notEvaluated += 1; skipped.push('G2');
    console.log('  ⚠️ G2 **未评估**:阶梯尚未在本树(resolve_keywords 与唯一实现都找不到)。');
} else if (g.shell_stmts === null) {
    ok(false, 'G2 🔴 唯一实现在,但薄壳 `resolve_keywords` 不见了 —— '
        + '提交路径那一侧的调用会断,而"找不到"曾被我并进「还没交」那一档');
} else {
    ok(g.endpoint_pairs === 1,
        `G1 🔴 端点里恰 1 处 「_kw, _src = resolve_keywords_with_source(...)」(实得 ${g.endpoint_pairs})`
        + ' —— 锁的是**赋值目标同一性**,不是"有没有这个调用"');
    ok(g.shell_ok === true,
        `G2 🔴 resolve_keywords 仍是**薄壳**:去 docstring 后只剩 1 条 return 且转调唯一实现`
        + `(实得 ${g.shell_stmts} 条语句 / ok=${g.shell_ok})`
        + ' —— 少了这条,「阶梯只有一份」就是假的:有人在壳里加两行逻辑,G1 照绿');
}

// ── H 🔴 前端接线 ───────────────────────────────────────────────────
const stripSrc = (t) => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const PAGE = stripSrc(readFileSync(join(ROOT, 'src/pages/Diagnosis/NewDiagnosis.tsx'), 'utf8'));
ok(/fetchSearchTermSuggestion\(selectedBrandId/.test(PAGE), 'H1 取数挂在品牌上');
ok(/\}, \[selectedBrandId\]\);/.test(PAGE), 'H2 依赖恰是品牌 —— 换客户必重取(词是关于某个品牌的)');
// 🔴 锚要切**调用点**不是 import 行 —— `fetchSearchTermSuggestion` 第一次出现在
//    import 里,按它切窗口会把整段 effect 切在窗外,H3 红得莫名其妙。
const callIdx = PAGE.indexOf('fetchSearchTermSuggestion(selectedBrandId');
const eff = callIdx >= 0 ? PAGE.slice(callIdx, callIdx + 620) : '';
ok(eff.length > 200, `H0 正样本臂:切到了取数 effect(实得 ${eff.length} 字符)`);
ok(/shouldAcceptRefill\(searchTermsDirty\)/.test(eff),
    'H3 🔴 回填**门控在 dirty 上** —— 晚到的响应不许冲掉她刚打的字');
ok(/if \(searchTermsSubmit\.send\) payload\.keywords = searchTermsSubmit\.terms;/.test(PAGE),
    'H4 🔴 提交体走三态决策,页面不再自己判空');
ok(!/if \(searchTerms\.terms\.length > 0\) payload\.keywords/.test(PAGE),
    'H4 反臂:旧的"非空才带"已消失(它会把「取到空数组」当成「没取到」)');
ok(/setSearchTermsDirty\(true\)/.test(PAGE) && /onChange=\{\(e\) => \{ setSearchTermsRaw/.test(PAGE),
    'H5 她一改就置 dirty(且就在受控 onChange 里,不是某个 effect)');
ok(/data-testid="search-terms-source"/.test(PAGE) && /data-testid="search-terms-empty-state"/.test(PAGE)
    && /data-testid="search-terms-brand-switch"/.test(PAGE),
    'H6 来源标签 / 空态说明 / 换品牌提示三块都渲染');

const pend = skipped.filter((t) => PENDING_SKIPS.includes(t));
if (pend.length) console.log(`  ⚠️ SKIP-GUARD 依赖方未交:${JSON.stringify(pend)} —— C 的端点落地后这条会自动变成真检查,**现在别读成通过**。`);
const unexpected = skipped.filter((t) => !ENV_SKIPS.includes(t) && !PENDING_SKIPS.includes(t));
if (unexpected.length) { bad += 1; console.log(`  🔴 SKIP-GUARD 清单外未评估:${JSON.stringify(unexpected)}`); }
if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
if (notEvaluated > 0) { console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`); process.exit(3); }
console.log('\n✅ 全部通过');
process.exit(0);
