/** Real React server rendering of the production step component. No browser or writes. */
import assert from 'node:assert/strict';
import Module, { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFileSync } from 'node:fs';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const root = fileURLToPath(new URL('../', import.meta.url));
const source = join(root, 'src/pages/Writing/ImageNoteFlowSteps.tsx');
const compiled = await build({
    entryPoints: [source], absWorkingDir: root, bundle: true, write: false,
    platform: 'node', format: 'cjs', jsx: 'automatic', packages: 'external',
    tsconfig: join(root, 'tsconfig.app.json'), logLevel: 'silent',
});
const filename = join(root, 'scripts/.image-note-steps-render.cjs');
const loaded = new Module(filename);
loaded.filename = filename;
loaded.paths = Module._nodeModulePaths(dirname(filename));
loaded._compile(compiled.outputFiles[0].text, filename);
const { ImageNoteFlowSteps } = loaded.exports;
assert.equal(typeof ImageNoteFlowSteps, 'function');
let passed = 0;
function test(name, fn) { fn(); passed++; console.log(`PASS ${name}`); }
const render = props => renderToStaticMarkup(createElement(ImageNoteFlowSteps, props));
const buttons = html => [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(m => m[0]);

for (const current of [1, 2, 3]) test(`creation stage ${current} renders exactly three real buttons`, () => {
    const html = render({ current, onBack: () => {} });
    const actual = buttons(html);
    assert.equal(actual.length, 3);
    actual.forEach(button => assert.match(button, /min-h-11/));
    assert.equal((html.match(/aria-current="step"/g) || []).length, 1);
    assert.match(actual[current - 1], /aria-current="step"/);
    ['选题', '制作图文', '检查与修改'].forEach((label, i) => assert.ok(actual[i].includes(label)));
    assert.match(html, /grid-cols-3/);
    assert.doesNotMatch(html, /发布|选择渠道|grid-cols-4/);
});
test('mobile short labels render alongside their full labels', () => {
    const html = render({ current: 2 });
    for (const label of ['选题', '制作', '检查']) assert.ok(html.includes(`sm:hidden">${label}</span>`));
});
test('new creation cannot click forward but earlier steps remain available', () => {
    const actual = buttons(render({ current: 2, onBack: () => {} }));
    assert.doesNotMatch(actual[0], /disabled=/);
    assert.match(actual[1], /disabled=/);
    assert.match(actual[2], /disabled=/);
});
test('existing work can revisit all three views without a fourth destination', () => {
    const actual = buttons(render({ current: 2, visitable: [1, 2, 3], onBack: () => {} }));
    assert.equal(actual.length, 3);
    actual.forEach(button => assert.doesNotMatch(button, /disabled=/));
});

// AST caller check complements actual component rendering: no publishing host.
const require = createRequire(import.meta.url);
const ts = require('typescript');
function stepInstances(file) {
    const ast = ts.createSourceFile(file, readFileSync(join(root, file), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    let count = 0;
    function visit(node) {
        if ((ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node)) && node.tagName.getText(ast) === 'ImageNoteFlowSteps') count++;
        ts.forEachChild(node, visit);
    }
    visit(ast);
    return count;
}
test('the real step component is mounted only in creation, never publishing', () => {
    assert.equal(stepInstances('src/pages/Writing/ImageNoteDetailRoute.tsx'), 1);
    assert.equal(stepInstances('src/pages/Publishing/ImageNoteList.tsx'), 0);
    assert.equal(stepInstances('src/pages/Publishing/PublishCenter.tsx'), 0);
});
console.log(`image-note-steps-render: ${passed} passed / 0 failed / 0 skipped`);
