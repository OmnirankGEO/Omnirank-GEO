import { readdir, readFile, stat } from 'node:fs/promises';
import { unevaluated } from './_verify_exit.mjs';
import { resolve } from 'node:path';

const distDir = resolve(process.cwd(), 'dist');
const forbidden = [
  { label: 'write legacy referral storage', pattern: /setItem\(["']omnirank_locked_ref["']/ },
  { label: 'write legacy portal owner storage', pattern: /setItem\(["']portal_owner_user_id["']/ },
  { label: 'legacy unbound-provider copy', pattern: /尚未绑定服务方/ },
  { label: 'provider-controlled-price copy', pattern: /价格和包装由服务方设置/ },
  { label: 'contact-provider copy', pattern: /联系服务方/ },
  { label: 'provider-supplied copy', pattern: /由服务方提供/ },
  { label: 'referral query URL', pattern: /\/invite\?ref=/ },
  { label: 'upstream seller account key', pattern: /seller_account_code/ },
  { label: 'responsible service account key', pattern: /responsible_service_account_code/ },
  { label: 'counterparty account key', pattern: /counterparty_account_code/ },
  { label: 'upstream seller copy', pattern: /直属卖方|公开编号待补齐/ },
];
const providerProcurementForbidden = [
  /platform_base/i,
  /upstream/i,
  /multiplier/i,
  /cost_basis/i,
  /\bB2B\b/i,
  /平台库存转售|倍率|上游|底价|逐级利润/,
];
const cashAnchorCopy = '输入金额就是本次应付金额，系统按当前采购规则换算到账算力。';

async function collectFiles(directory) {
  const files = [];
  for (const entry of await readdir(directory)) {
    const path = resolve(directory, entry);
    const metadata = await stat(path);
    if (metadata.isDirectory()) files.push(...await collectFiles(path));
    else if (/\.(?:html|js|css)$/i.test(entry)) files.push(path);
  }
  return files;
}

// 🔴 [#95] 没有产物 = **未评估**(exit 3)。原来 dist 不存在时 collectFiles 直接抛
//    ENOENT **未捕获异常**崩退(rc=1),与真违规同码,而且报文是一坨 Node 栈。
let files = [];
try {
  files = await collectFiles(distDir);
} catch (e) {
  unevaluated([`扫不了 ${distDir}:${e && e.code ? e.code : e} —— 先 npm run build 生成 dist`],
              '公开页隐私产物闸');
}
if (!files.length) {
  unevaluated([`${distDir} 里没有任何生产资产 —— 先 npm run build`], '公开页隐私产物闸');
}

const violations = [];
for (const file of files) {
  const source = await readFile(file, 'utf8');
  for (const rule of forbidden) {
    if (rule.pattern.test(source)) violations.push(`${file}: ${rule.label}`);
  }
  if (/[/\\]InventoryCenter-[^/\\]+\.js$/i.test(file)) {
    for (const pattern of providerProcurementForbidden) {
      if (pattern.test(source)) violations.push(`${file}: provider procurement internal term ${pattern}`);
    }
    if (!source.includes(cashAnchorCopy)) {
      violations.push(`${file}: missing cash-anchor payable copy`);
    }
  }
}

if (violations.length) {
  throw new Error(`privacy bundle guard found public relationship residue:\n${violations.join('\n')}`);
}

console.log(`privacy bundle guard: ${files.length} production assets verified`);
