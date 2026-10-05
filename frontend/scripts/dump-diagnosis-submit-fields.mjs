/**
 * dump-diagnosis-submit-fields.mjs —— 把 NewDiagnosis.tsx 的**提交字段集**吐成 JSON
 *
 * 存在的理由(WO-LATENT-TRAPS §3 · 2026-08-17):
 *   `test-diagnosis-launch-ui.mjs` 的 §1 红线是「表单提交字段集 ⊆ server.py 的 DiagnosisRequest
 *   字段集」。它引后端文件 → 在 Docker 的 frontend-builder 阶段(只 `COPY frontend/ ./`)
 *   够不到 server.py → **每次镜像构建都大声 SKIP,判据从没在它该起作用的地方跑过**。
 *
 *   §3 把那条判据挪到后端 pytest(`tests/test_diagnosis_launch_request_model_lock_2026_08_17.py`)。
 *   但字段抽取必须仍走 **TypeScript AST**(原判据的口径就是"不 grep"),
 *   所以抽取器留在 JS 这边,由 pytest 起子进程调它 —— 一个抽取器,两处复用,口径不会漂。
 *
 * 用法(在 frontend/ 下):node scripts/dump-diagnosis-submit-fields.mjs
 * 输出:{"file": "...", "fields": ["brand_name", ...]}
 */
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import ts from 'typescript';

const root = process.cwd();
const PAGE = path.join(root, 'src/pages/Diagnosis/NewDiagnosis.tsx');

function walk(node, fn) {
    fn(node);
    node.forEachChild(child => walk(child, fn));
}

/**
 * 从 AST 收集 `payload.X = ...` 与 `const payload = { X: ... }` 的全部字段名。
 * 🔴 与 test-diagnosis-launch-ui.mjs 里的 collectSubmitFields **逐字同源**;
 *    改这里必须同步改那里,否则两处口径会悄悄分叉。
 */
export function collectSubmitFields(sf) {
    const fields = new Set();
    walk(sf, node => {
        if (ts.isBinaryExpression(node)
            && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
            && ts.isPropertyAccessExpression(node.left)
            && ts.isIdentifier(node.left.expression)
            && node.left.expression.text === 'payload') {
            fields.add(node.left.name.text);
        }
        if (ts.isVariableDeclaration(node)
            && ts.isIdentifier(node.name)
            && node.name.text === 'payload'
            && node.initializer
            && ts.isObjectLiteralExpression(node.initializer)) {
            for (const prop of node.initializer.properties) {
                if (prop.name && (ts.isIdentifier(prop.name) || ts.isStringLiteral(prop.name))) {
                    fields.add(prop.name.text);
                }
            }
        }
    });
    return fields;
}

if (!fs.existsSync(PAGE)) {
    console.error(`缺 ${PAGE} —— 抽不出字段集,判据不可用(不是通过)`);
    process.exit(2);
}
const sf = ts.createSourceFile(PAGE, fs.readFileSync(PAGE, 'utf8'),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
process.stdout.write(JSON.stringify({
    file: path.relative(root, PAGE).replace(/\\/g, '/'),
    fields: [...collectSubmitFields(sf)],
}));
