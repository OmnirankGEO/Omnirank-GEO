// node render.cjs preview t1 t2 ... | node render.cjs full
const { chromium } = require('playwright');
const { spawn } = require('child_process'); const fs = require('fs');
(async () => {
  const mode = process.argv[2];
  const b = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  p.on('pageerror', e => console.log('PAGEERR', e.message)); p.on('console', m => { if (m.type() === 'error') console.log('CONSOLE', m.text()); });
  fs.mkdirSync(__dirname + '/out', { recursive: true }); fs.mkdirSync(__dirname + '/prev', { recursive: true });
  await p.goto('file://' + __dirname + '/index.html'); await p.evaluate(() => window.ready); await p.waitForTimeout(800);
  const meta = await p.evaluate(() => ({ narr: window.NARR, sfx: window.SFX, dur: window.DURATION }));
  fs.writeFileSync(__dirname + '/out/meta.json', JSON.stringify(meta, null, 1));
  const seq = async (t, cur) => { for (let x = cur; x < t; x += 0.25) await p.evaluate(v => seekTo(v), x); await p.evaluate(v => seekTo(v), t); };
  if (mode === 'preview') {
    let cur = 0; await p.evaluate(() => document.body.classList.add('subs'));
    for (const t of process.argv.slice(3).map(Number)) { await seq(t, cur); cur = t; await p.screenshot({ path: `${__dirname}/prev/p-${String(t).padStart(6, '0')}.png` }); }
  } else {
    const fps = 30, n = Math.ceil(meta.dur * fps);
    const enc = (f) => spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'mjpeg', '-i', '-', '-c:v', 'libx264', '-preset', 'slow', '-crf', '20', '-pix_fmt', 'yuv420p', f], { stdio: ['pipe', 'inherit', 'inherit'] });
    const A = enc(__dirname + '/out/v_nosubs.mp4'), B = enc(__dirname + '/out/v_subs.mp4');
    const w = async (ff, buf) => { if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r)); };
    for (let i = 0; i < n; i++) {
      await p.evaluate(v => { seekTo(v); document.body.classList.remove('subs'); }, i / fps);
      await w(A, await p.screenshot({ type: 'jpeg', quality: 92 }));
      await p.evaluate(() => document.body.classList.add('subs'));
      await w(B, await p.screenshot({ type: 'jpeg', quality: 92 }));
      if (i % 600 === 0) console.log('frame', i, '/', n);
    }
    A.stdin.end(); B.stdin.end(); await Promise.all([new Promise(r => A.on('close', r)), new Promise(r => B.on('close', r))]); console.log('done');
  }
  await b.close();
})();
