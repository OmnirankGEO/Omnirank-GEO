/* 抖音竖屏系列公共动效库(1080×1920 / 60fps)
 * 原则:每一帧只由时间 t 决定;缓动统一用 expo/power 曲线,保证丝滑。 */
window.VIDEO = { w: 1080, h: 1920, fps: 60 };
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const tl = gsap.timeline({ paused: true });
const EASE = 'expo.out';
window.SFX = [];
const sfx = (name, t) => window.SFX.push({ t: +t.toFixed(3), name });
const el = (h) => { const d = document.createElement('div'); d.innerHTML = h.trim(); return d.firstChild; };
let _seed = 17; const rnd = () => ((_seed = (_seed * 16807) % 2147483647) / 2147483647);

/** 场景:淡入 + 整体缓推镜头 + 淡出 */
function scene(id, t0, t1, opts = {}) {
  tl.set(id, { autoAlpha: 1 }, t0);
  tl.fromTo(id, { opacity: 0, scale: opts.zoomFrom ?? 1.06 }, { opacity: 1, scale: 1, duration: 0.7, ease: EASE, immediateRender: false }, t0);
  tl.to(id, { scale: opts.zoomTo ?? 0.97, duration: t1 - t0 - 0.7, ease: 'none' }, t0 + 0.7);
  tl.to(id, { opacity: 0, scale: '-=0.03', duration: 0.4, ease: 'power2.in' }, t1 - 0.4);
  tl.set(id, { autoAlpha: 0 }, t1);
  if (opts.sfx !== false) sfx('whoosh', t0);
}
/** 把文字拆成逐字 span,支持 <em> 高亮 */
function splitChars(sel) {
  const root = $(sel);
  const walk = (node) => {
    [...node.childNodes].forEach((n) => {
      if (n.nodeType === 3) {
        const frag = document.createDocumentFragment();
        [...n.textContent].forEach((ch) => { const s = document.createElement('span'); s.className = 'ch'; s.textContent = ch === ' ' ? ' ' : ch; frag.appendChild(s); });
        n.replaceWith(frag);
      } else if (n.nodeType === 1) walk(n);
    });
  };
  walk(root);
  return [...root.querySelectorAll('.ch')];
}
/** 逐字飞入(3D 翻转 + 上浮) */
function kinetic(sel, t, opts = {}) {
  const chs = splitChars(sel);
  tl.set(sel, { autoAlpha: 1 }, t);
  tl.fromTo(chs, { opacity: 0, y: opts.y ?? 80, rotationX: opts.rx ?? -70, scale: opts.s ?? 0.9, filter: 'blur(8px)' },
    { opacity: 1, y: 0, rotationX: 0, scale: 1, filter: 'blur(0px)', duration: opts.d ?? 0.9, ease: EASE, stagger: opts.st ?? 0.035, immediateRender: true }, t);
  gsap.set(sel, { autoAlpha: 0 });
  if (opts.sfx !== false) sfx(opts.sfx || 'pop', t);
  return chs;
}
/** 冲击大字:从大到小砸下 + 闪光 */
function impact(sel, t) {
  tl.fromTo(sel, { autoAlpha: 0, scale: 2.4, filter: 'blur(20px)' }, { autoAlpha: 1, scale: 1, filter: 'blur(0px)', duration: 0.45, ease: 'power4.in', immediateRender: true }, t);
  tl.fromTo('#flash', { opacity: 0.35 }, { opacity: 0, duration: 0.6, immediateRender: false }, t + 0.45);
  tl.fromTo('#stage-inner', { y: 0 }, { keyframes: [{ y: 14, duration: 0.05 }, { y: -8, duration: 0.06 }, { y: 0, duration: 0.12 }], immediateRender: false }, t + 0.45);
  sfx('boom', t + 0.43);
}
/** 浮入(卡片等) */
function rise(sel, t, opts = {}) {
  tl.fromTo(sel, { autoAlpha: 0, y: opts.y ?? 120, rotationX: opts.rx ?? 25, scale: opts.s ?? 0.92 },
    { autoAlpha: 1, y: 0, rotationX: 0, scale: 1, duration: opts.d ?? 1.0, ease: EASE, immediateRender: true }, t);
  if (opts.sfx !== false) sfx(opts.sfx || 'whoosh', t);
}
function fadeOut(sel, t, d = 0.35) { tl.to(sel, { autoAlpha: 0, y: '-=40', duration: d, ease: 'power2.in' }, t); }
/** 扫光:一条斜向高光划过元素 */
function sweep(sel, t, d = 0.9) {
  const host = $(sel); host.style.position = host.style.position || 'relative'; host.style.overflow = 'hidden';
  const s = el('<i class="sweep"></i>'); host.appendChild(s);
  tl.fromTo(s, { xPercent: -160 }, { xPercent: 260, duration: d, ease: 'power2.inOut', immediateRender: true }, t);
}
function typeText(sel, text, t, dur, tick = true) {
  const o = { n: 0 }, e = $(sel);
  tl.to(o, { n: text.length, duration: dur, ease: 'none', onUpdate: () => { e.textContent = text.slice(0, Math.round(o.n)); } }, t);
  if (tick) for (let i = 0; i < text.length; i += 2) sfx('tick', t + i / text.length * dur);
}
function countUp(sel, from, to, t, dur, fmt = (v) => Math.round(v)) {
  const o = { v: from };
  tl.to(o, { v: to, duration: dur, ease: 'power3.out', onUpdate: () => { $(sel).textContent = fmt(o.v); } }, t);
  sfx('rise', t);
}

/** 生图背景层:按时间段交叉淡入,并做缓慢推拉 + 平移(Ken Burns) */
const BG_IMGS = [];
function bg(src, t0, t1, opts = {}) {
  const b = el(`<div class="bg" style="background-image:url('${src}')"></div>`);
  $('#bgs').appendChild(b); BG_IMGS.push(src);
  const f = opts.fade ?? 0.6;
  gsap.set(b, { autoAlpha: 0, filter: opts.filter || 'none' });
  tl.fromTo(b, { autoAlpha: 0 }, { autoAlpha: opts.alpha ?? 1, duration: f, ease: 'power1.inOut', immediateRender: false }, Math.max(0, t0 - f / 2));
  tl.fromTo(b, { scale: opts.s0 ?? 1.12, y: opts.y0 ?? 0, x: opts.x0 ?? 0 }, { scale: opts.s1 ?? 1.0, y: opts.y1 ?? 0, x: opts.x1 ?? 0, duration: t1 - t0 + f, ease: 'none', immediateRender: t0 <= 0 }, Math.max(0, t0 - f / 2));
  tl.to(b, { autoAlpha: 0, duration: f, ease: 'power1.inOut' }, t1 - f / 2);
  return b;
}
/** 极光背景:几团大色块缓慢漂移(按时间计算) */
function aurora(colors) {
  const host = $('#aurora');
  colors.forEach((c, i) => {
    const b = el(`<div class="blob" style="background:radial-gradient(circle at center, ${c} 0%, transparent 65%)"></div>`);
    host.appendChild(b);
    const x0 = rnd() * 700 - 200, y0 = rnd() * 1500;
    gsap.set(b, { x: x0, y: y0 });
    tl.to(b, { x: x0 + (rnd() - 0.5) * 500, y: y0 + (rnd() - 0.5) * 600, duration: 60, ease: 'sine.inOut', yoyo: true, repeat: 3 }, 0);
  });
}
/** 粒子:位置完全由 t 计算,画在 canvas 上 */
const PARTICLES = [];
function particles(n, color) {
  for (let i = 0; i < n; i++) PARTICLES.push({ x: rnd() * 1080, y: rnd() * 1920, r: 1 + rnd() * 2.6, v: 8 + rnd() * 30, ph: rnd() * 6.28, c: color });
}
const BURSTS = [];
function burst(t, x, y, n = 60, color = '#2fd47a') { for (let i = 0; i < n; i++) { const a = rnd() * 6.28, sp = 300 + rnd() * 900; BURSTS.push({ t, x, y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp, r: 2 + rnd() * 4, c: color }); } sfx('sparkle', t); }
function drawCanvas(t) {
  const cv = $('#fx'); const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, 1080, 1920);
  for (const p of PARTICLES) {
    const y = (p.y - p.v * t) % 1920; const yy = y < 0 ? y + 1920 : y;
    const a = 0.25 + 0.25 * Math.sin(p.ph + t * 1.3);
    ctx.globalAlpha = a; ctx.fillStyle = p.c; ctx.beginPath(); ctx.arc(p.x + Math.sin(p.ph + t * 0.6) * 12, yy, p.r, 0, 6.28); ctx.fill();
  }
  for (const b of BURSTS) {
    const dt = t - b.t; if (dt < 0 || dt > 1.4) continue;
    const k = 1 - Math.exp(-dt * 3);
    const x = b.x + b.vx * k * 0.35, y = b.y + b.vy * k * 0.35 + 220 * dt * dt;
    ctx.globalAlpha = Math.max(0, 1 - dt / 1.4); ctx.fillStyle = b.c; ctx.beginPath(); ctx.arc(x, y, b.r, 0, 6.28); ctx.fill();
  }
  ctx.globalAlpha = 1;
  drawGrain(t);
}
/** 胶片颗粒:预生成 4 张噪点图,按帧轮换(只要页面有 #grain) */
let _grain = null;
function drawGrain(t) {
  const cv = $('#grain'); if (!cv) return;
  if (!_grain) {
    _grain = [0, 1, 2, 3].map(() => { const c = document.createElement('canvas'); c.width = c.height = 256; const g = c.getContext('2d'); const d = g.createImageData(256, 256);
      for (let i = 0; i < d.data.length; i += 4) { const v = rnd() * 255; d.data[i] = d.data[i + 1] = d.data[i + 2] = v; d.data[i + 3] = 255; } g.putImageData(d, 0, 0); return c; });
  }
  const ctx = cv.getContext('2d'); const img = _grain[Math.floor(t * 24) % 4];
  ctx.fillStyle = ctx.createPattern(img, 'repeat'); ctx.fillRect(0, 0, 1080, 1920);
}
/** 系列角标:《AI 会推荐你吗?》第 N 集 */
function seriesBadge(ep, t0, t1) {
  rise('#badge', t0, { y: -60, rx: 0, sfx: false });
  tl.to('#badge', { autoAlpha: 0, duration: 0.4 }, t1 - 0.4);
  $('#badge').innerHTML = `<b>AI 会推荐你吗？</b><span>第 ${ep} 集</span>`;
}
function finish(duration) {
  tl.fromTo('#progress', { width: 0 }, { width: 1080, duration, ease: 'none' }, 0);
  window.DURATION = duration;
  window.seekTo = (t) => { tl.seek(t, false); drawCanvas(t); };
  window.setSub = (text) => { const e = $('#capt'); if (e.textContent !== (text || '')) e.textContent = text || ''; e.parentNode.style.opacity = text ? 1 : 0; };
  const imgs = BG_IMGS.map((src) => new Promise((ok) => { const i = new Image(); i.onload = i.onerror = () => (i.decode ? i.decode().catch(() => 0) : 0).then(ok); i.src = src; }));
  window.ready = document.fonts.ready.then(() => Promise.all([...imgs,
    document.fonts.load('900 80px "Noto Sans SC"', '开源推荐'),
  ])).then(() => true);
}
