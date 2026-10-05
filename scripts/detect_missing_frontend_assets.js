#!/usr/bin/env node
/**
 * Detect frontend asset imports that will be missing in a clean checkout/build.
 *
 * This intentionally flags assets that exist on the local machine but are
 * untracked or ignored, because a clean worktree used by CI/deploy will not
 * receive them.
 *
 * Usage:
 *   node scripts/detect_missing_frontend_assets.js --json
 *   node scripts/detect_missing_frontend_assets.js --json --no-fail
 */

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const CODE_EXTENSIONS = new Set(['.ts', '.tsx', '.js', '.jsx', '.css', '.scss']);
const ASSET_EXTENSIONS = new Set([
  '.avif',
  '.gif',
  '.ico',
  '.jpeg',
  '.jpg',
  '.pdf',
  '.png',
  '.svg',
  '.webp',
]);

const DEFAULT_SCAN_ROOT = 'frontend/src';

function parseArgs(argv) {
  const args = {
    root: process.cwd(),
    scanRoot: DEFAULT_SCAN_ROOT,
    json: false,
    noFail: false,
    advisorsOnly: false,
    help: false,
  };

  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--json') args.json = true;
    else if (arg === '--no-fail') args.noFail = true;
    else if (arg === '--advisors-only') args.advisorsOnly = true;
    else if (arg === '--root') {
      i += 1;
      args.root = path.resolve(argv[i]);
    } else if (arg === '--scan-root') {
      i += 1;
      args.scanRoot = argv[i];
    } else if (arg === '--help' || arg === '-h') {
      args.help = true;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  return args;
}

function printHelp() {
  console.log(`detect_missing_frontend_assets.js

Options:
  --json              Emit JSON only
  --no-fail           Exit 0 even when missing/untracked assets are found
  --advisors-only     Only report assets under frontend/src/assets/advisors
  --root <path>       Repository root; default current working directory
  --scan-root <path>  Source tree to scan; default frontend/src
`);
}

function runGit(root, args, input) {
  const result = spawnSync('git', args, {
    cwd: root,
    input,
    encoding: 'utf8',
    windowsHide: true,
  });
  return result;
}

function trackedFiles(root) {
  const result = runGit(root, ['ls-files', '-z']);
  if (result.status !== 0) {
    return {
      available: false,
      files: null,
      warning: `git ls-files unavailable: ${(result.stderr || result.stdout || 'no stderr').trim()}`,
    };
  }
  return {
    available: true,
    files: new Set(
      result.stdout
        .split('\0')
        .filter(Boolean)
        .map((file) => normalizeRel(file)),
    ),
    warning: null,
  };
}

function isIgnored(root, relPath, gitAvailable) {
  if (!gitAvailable) return false;
  const result = runGit(root, ['check-ignore', '-q', '--', relPath]);
  return result.status === 0;
}

function normalizeRel(p) {
  return p.replace(/\\/g, '/');
}

function walkFiles(startPath) {
  const files = [];
  if (!fs.existsSync(startPath)) return files;

  const stack = [startPath];
  while (stack.length > 0) {
    const current = stack.pop();
    const entries = fs.readdirSync(current, { withFileTypes: true });
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === 'node_modules' || entry.name === 'dist') continue;
        stack.push(full);
      } else if (entry.isFile() && CODE_EXTENSIONS.has(path.extname(entry.name))) {
        files.push(full);
      }
    }
  }
  return files.sort();
}

function lineStarts(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === '\n') starts.push(i + 1);
  }
  return starts;
}

function locationForIndex(starts, index) {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (starts[mid] <= index) lo = mid + 1;
    else hi = mid - 1;
  }
  const lineIndex = Math.max(0, hi);
  return { line: lineIndex + 1, column: index - starts[lineIndex] + 1 };
}

function stripQuery(spec) {
  return spec.split('?')[0].split('#')[0];
}

function isAssetSpec(spec) {
  const clean = stripQuery(spec);
  return ASSET_EXTENSIONS.has(path.extname(clean).toLowerCase());
}

function extractAssetSpecs(text) {
  const specs = [];
  const patterns = [
    /\bimport\s+(?:type\s+)?(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]/g,
    /\bexport\s+[^'"]*?\s+from\s+['"]([^'"]+)['"]/g,
    /\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
    /\bnew\s+URL\s*\(\s*['"]([^'"]+)['"]\s*,\s*import\.meta\.url\s*\)/g,
    /\burl\s*\(\s*['"]?([^)'"]+)['"]?\s*\)/g,
  ];

  for (const pattern of patterns) {
    pattern.lastIndex = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const spec = match[1];
      if (spec && isAssetSpec(spec)) {
        specs.push({ spec, index: match.index });
      }
    }
  }
  return specs;
}

function resolveSpec(root, importer, spec) {
  const clean = stripQuery(spec);
  if (clean.startsWith('@/')) {
    return path.resolve(root, 'frontend/src', clean.slice(2));
  }
  if (clean.startsWith('/src/')) {
    return path.resolve(root, 'frontend', clean.slice(1));
  }
  if (clean.startsWith('/')) {
    return path.resolve(root, 'frontend/public', clean.slice(1));
  }
  if (clean.startsWith('./') || clean.startsWith('../')) {
    return path.resolve(path.dirname(importer), clean);
  }
  return null;
}

function uniqueFindings(findings) {
  const seen = new Set();
  const out = [];
  for (const finding of findings) {
    const key = [
      finding.importer,
      finding.line,
      finding.spec,
      finding.resolved,
      finding.reason,
    ].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(finding);
  }
  return out;
}

function scan(args) {
  const root = path.resolve(args.root);
  const scanRoot = path.resolve(root, args.scanRoot);
  const gitState = trackedFiles(root);
  const tracked = gitState.files;
  const files = walkFiles(scanRoot);
  const findings = [];
  const imports = [];

  for (const file of files) {
    const text = fs.readFileSync(file, 'utf8');
    const starts = lineStarts(text);
    const lines = text.split(/\r?\n/);
    for (const asset of extractAssetSpecs(text)) {
      const resolved = resolveSpec(root, file, asset.spec);
      if (!resolved) continue;

      const rel = normalizeRel(path.relative(root, resolved));
      if (args.advisorsOnly && !rel.startsWith('frontend/src/assets/advisors/')) continue;

      const importerRel = normalizeRel(path.relative(root, file));
      const loc = locationForIndex(starts, asset.index);
      const exists = fs.existsSync(resolved);
      const trackedInGit = tracked ? tracked.has(rel) : null;
      const ignored = exists ? isIgnored(root, rel, gitState.available) : false;
      const entry = {
        importer: importerRel,
        line: loc.line,
        column: loc.column,
        spec: asset.spec,
        resolved: rel,
        exists,
        tracked: trackedInGit,
        ignored,
        excerpt: (lines[loc.line - 1] || '').trim().slice(0, 240),
      };
      imports.push(entry);

      if (!exists) {
        findings.push({ ...entry, severity: 'error', reason: 'missing_on_disk' });
      } else if (gitState.available && !trackedInGit) {
        findings.push({
          ...entry,
          severity: 'error',
          reason: ignored ? 'exists_locally_but_gitignored' : 'exists_locally_but_untracked',
        });
      }
    }
  }

  const unique = uniqueFindings(findings);
  const uniqueAssets = Array.from(
    new Map(
      unique.map((finding) => [
        finding.resolved,
        {
          resolved: finding.resolved,
          exists: finding.exists,
          tracked: finding.tracked,
          ignored: finding.ignored,
          reason: finding.reason,
        },
      ]),
    ).values(),
  ).sort((a, b) => a.resolved.localeCompare(b.resolved));
  return {
    passed: unique.length === 0,
    scan_root: normalizeRel(path.relative(root, scanRoot)),
    git_available: gitState.available,
    warnings: gitState.warning ? [gitState.warning] : [],
    summary: {
      files_scanned: files.length,
      asset_imports: imports.length,
      findings: unique.length,
      unique_assets_with_findings: uniqueAssets.length,
      missing_on_disk: unique.filter((f) => f.reason === 'missing_on_disk').length,
      exists_locally_but_gitignored: unique.filter((f) => f.reason === 'exists_locally_but_gitignored').length,
      exists_locally_but_untracked: unique.filter((f) => f.reason === 'exists_locally_but_untracked').length,
    },
    unique_assets: uniqueAssets,
    findings: unique,
  };
}

function main() {
  const args = parseArgs(process.argv);
  if (args.help) {
    printHelp();
    return 0;
  }

  const report = scan(args);
  if (args.json) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(`Frontend asset detector: ${report.passed ? 'PASS' : 'FAIL'}`);
    console.log(JSON.stringify(report.summary, null, 2));
    for (const finding of report.findings) {
      console.log(`${finding.severity.toUpperCase()} ${finding.resolved} (${finding.reason})`);
      console.log(`  imported by ${finding.importer}:${finding.line}:${finding.column}`);
      console.log(`  ${finding.excerpt}`);
    }
  }

  return report.passed || args.noFail ? 0 : 1;
}

try {
  process.exitCode = main();
} catch (error) {
  console.error(JSON.stringify({ passed: false, error: error.message }, null, 2));
  process.exitCode = 2;
}
