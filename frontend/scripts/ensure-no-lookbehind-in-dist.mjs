import { readdir, readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const assetsDir = join(scriptDir, '..', 'dist', 'assets');
const lookbehindPattern = /\(\?<[!=]/;
const offenders = [];

for (const fileName of await readdir(assetsDir)) {
  if (!fileName.endsWith('.js')) continue;
  const filePath = join(assetsDir, fileName);
  const source = await readFile(filePath, 'utf8');
  if (lookbehindPattern.test(source)) {
    offenders.push(fileName);
  }
}

if (offenders.length) {
  console.error(
    `Unsupported JavaScript lookbehind regex found in build assets: ${offenders.join(', ')}`
  );
  process.exit(1);
}

console.log('No JavaScript lookbehind regex found in build assets.');
