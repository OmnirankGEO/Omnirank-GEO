import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';

const root = path.resolve(process.cwd(), 'src');
const pageRoot = path.join(root, 'pages');
const failures = [];

function walk(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolute = path.join(directory, entry.name);
    return entry.isDirectory() ? walk(absolute) : [absolute];
  });
}

function containsJsx(node) {
  let found = false;
  function visit(current) {
    if (ts.isJsxElement(current) || ts.isJsxSelfClosingElement(current) || ts.isJsxFragment(current)) {
      found = true;
      return;
    }
    ts.forEachChild(current, visit);
  }
  visit(node);
  return found;
}

for (const file of walk(pageRoot).filter((value) => /\.[jt]sx$/.test(value))) {
  const relative = path.relative(process.cwd(), file).replaceAll('\\', '/');
  const basename = path.basename(file, path.extname(file));
  if (/^Demo.*(?:Page|View)$/i.test(basename)) {
    failures.push(`${relative}: business pages may not define Demo*Page/View replacements`);
  }
  const sourceText = fs.readFileSync(file, 'utf8');
  const source = ts.createSourceFile(file, sourceText, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  function visit(node) {
    if (
      ts.isReturnStatement(node)
      && node.expression
      && ts.isConditionalExpression(node.expression)
      && /demo/i.test(node.expression.condition.getText(source))
      && containsJsx(node.expression)
    ) {
      const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
      failures.push(`${relative}:${line}: demo conditions may not return a ternary replacement page`);
    }
    if (ts.isIfStatement(node) && /demo/i.test(node.expression.getText(source))) {
      const returns = [];
      function collect(current) {
        if (ts.isReturnStatement(current) && current.expression && containsJsx(current.expression)) returns.push(current);
        ts.forEachChild(current, collect);
      }
      collect(node.thenStatement);
      if (returns.length > 0) {
        const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
        failures.push(`${relative}:${line}: demo conditions may not return a replacement page`);
      }
    }
    if (ts.isJsxOpeningLikeElement(node)) {
      const name = node.tagName.getText(source);
      if (/^Demo.*(?:Page|View)$/.test(name)) {
        const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
        failures.push(`${relative}:${line}: Demo*Page/View route or JSX replacement is forbidden`);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
}

if (failures.length > 0) {
  console.error('Demo WYSIWYG contract failed:\n' + failures.map((value) => `- ${value}`).join('\n'));
  process.exit(1);
}

console.log('Demo WYSIWYG contract passed: no page-level demo replacements.');
