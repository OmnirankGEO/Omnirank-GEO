// 通用渲染器:按时间映射逐帧截图,用 ffmpeg 编码成 MP4。
//   node render.cjs <页面目录> <输出目录> [map.json] [--preview 1,5,30]
// map.json(由 dub.py 生成):
//   { "duration": 视频总长秒, "knots": [[视频秒, 时间轴秒], ...],
//     "subs": [{"v0":秒,"v1":秒,"text":"..."}], "variants": ["subs","nosubs"] }
// 不给 map 时按页面原始时长 1:1 渲染。
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const [pageDir, outDir, mapArg, ...rest] = process.argv.slice(2);
const previewArg = rest.includes('--preview') ? rest[rest.indexOf('--preview') + 1] : (mapArg === '--preview' ? rest[0] : null);
const mapFile = mapArg && mapArg !== '--preview' ? mapArg : null;
const FPS = 30;

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const b = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  p.on('pageerror', (e) => console.log('PAGEERR', e.message));
  await p.goto('file://' + path.resolve(pageDir, 'index.html'));
  await p.evaluate(() => window.ready);
  await p.waitForTimeout(800);
  const meta = await p.evaluate(() => ({ narr: window.NARR || [], sfx: window.SFX || [], dur: window.DURATION }));
  fs.writeFileSync(path.join(outDir, 'meta.json'), JSON.stringify(meta, null, 1));

  const map = mapFile ? JSON.parse(fs.readFileSync(mapFile, 'utf8')) : { duration: meta.dur, knots: [[0, 0], [meta.dur, meta.dur]], subs: null, variants: ['main'] };
  const K = map.knots;
  const toTimeline = (tv) => {
    if (tv <= K[0][0]) return K[0][1];
    for (let i = 1; i < K.length; i++) if (tv <= K[i][0]) {
      const [v0, t0] = K[i - 1], [v1, t1] = K[i];
      return v1 === v0 ? t1 : t0 + (tv - v0) * (t1 - t0) / (v1 - v0);
    }
    return K[K.length - 1][1];
  };
  const subAt = (tv) => { if (!map.subs) return null; const s = map.subs.find((x) => tv >= x.v0 && tv < x.v1); return s ? s.text : ''; };
  if (map.subs) await p.evaluate(() => { window.SUBS_OVERRIDE = true; });
  const frame = async (tv, variant) => {
    const sub = subAt(tv);
    await p.evaluate(([t, s, v]) => {
      seekTo(t);
      if (s !== null && window.setSub) window.setSub(s);
      document.body.classList.toggle('subs', v === 'subs');
    }, [toTimeline(tv), sub, variant]);
  };

  if (previewArg) {
    let cur = 0;
    for (const t of previewArg.split(',').map(Number)) {
      for (let x = cur; x < t; x += 0.25) await frame(x, 'subs');
      await frame(t, 'subs'); cur = t;
      await p.screenshot({ path: path.join(outDir, `prev-${t.toFixed(1).padStart(6, '0')}.png`) });
    }
  } else {
    const variants = map.variants || ['main'];
    const n = Math.ceil(map.duration * FPS);
    const enc = variants.map((v) => spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'mjpeg', '-i', '-',
      '-c:v', 'libx264', '-preset', 'slow', '-crf', '20', '-pix_fmt', 'yuv420p', path.join(outDir, `video_${v}.mp4`)], { stdio: ['pipe', 'inherit', 'inherit'] }));
    const write = async (ff, buf) => { if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once('drain', r)); };
    for (let i = 0; i < n; i++) {
      for (let k = 0; k < variants.length; k++) {
        await frame(i / FPS, variants[k]);
        await write(enc[k], await p.screenshot({ type: 'jpeg', quality: 92 }));
      }
      if (i % 900 === 0) console.log('frame', i, '/', n);
    }
    enc.forEach((e) => e.stdin.end());
    await Promise.all(enc.map((e) => new Promise((r) => e.on('close', r))));
    console.log('done');
  }
  await b.close();
})();
