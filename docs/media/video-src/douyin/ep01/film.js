/* 第 1 集 · 黑白纪实 + 粗黑体巨字:我们把一整套 GEO 系统开源了 */
const A = '../assets/';
particles(40, '#ffffff');
seriesBadge(1, 0.2, 38.5);

// 背景:黑白纪实照片,硬切 + 缓推
bg(A + 'ep01_crowd.jpg', 0, 3.4, { s0: 1.35, s1: 1.15, alpha: 0.35, fade: 0.2 });
bg(A + 'ep01_street.jpg', 3.4, 10, { s0: 1.25, s1: 1.05, y0: -40, y1: 20, fade: 0.2 });
bg(A + 'ep01_crowd.jpg', 14.2, 26, { s0: 1.0, s1: 1.25, alpha: 0.45, fade: 0.2 });
bg(A + 'ep01_desk.jpg', 26, 31, { s0: 1.2, s1: 1.05, x0: 40, fade: 0.2, alpha: 0.75 });

// S0 钩子 0–3.4
scene('#s0', 0, 3.4, { sfx: false, zoomFrom: 1 });
kinetic('#h0a', 0.1, { st: 0.04, sfx: 'tick', rx: 0, y: 40 });
impact('#h0b', 0.45);
tl.fromTo('#h0c', { autoAlpha: 0, scaleX: 0, transformOrigin: '0% 50%' }, { autoAlpha: 1, scaleX: 1, duration: 0.35, ease: 'expo.out', immediateRender: true }, 1.25);
tl.fromTo('#h0c', { scale: 1.6 }, { scale: 1, duration: 0.4, ease: 'power4.in', immediateRender: false }, 1.25);
sfx('boom', 1.62);
tl.fromTo('#stage-inner', { y: 0 }, { keyframes: [{ y: 18, duration: 0.05 }, { y: -10, duration: 0.06 }, { y: 0, duration: 0.12 }], immediateRender: false }, 1.65);
tl.to('#h0b', { scale: 1.05, duration: 2.4, ease: 'none' }, 0.9);

// S1 街头问 AI 3.4–10
scene('#s1', 3.4, 10);
kinetic('#s1t', 3.5, { st: 0.03, rx: 0, y: 50 });
rise('#ph', 3.7, { y: 400, rx: 0, s: 1 });
rise('#q1', 4.2, { y: 30, rx: 0, s: 0.9, sfx: 'pop' });
typeText('#q1t', '深圳商务用车哪家好？', 4.3, 1.1);
rise('#a1', 5.7, { y: 40, rx: 0, s: 0.95, sfx: 'pop' });
['#n1', '#n2', '#n3'].forEach((s, i) => { tl.fromTo(s, { autoAlpha: 0, x: -40 }, { autoAlpha: 1, x: 0, duration: 0.4, ease: EASE, immediateRender: true }, 6.0 + i * 0.3); sfx('tick', 6.0 + i * 0.3); });
tl.to('#ph', { scale: 0.92, y: -60, duration: 0.6, ease: 'power3.inOut' }, 7.9);
tl.fromTo('#miss', { autoAlpha: 0, scaleY: 0 }, { autoAlpha: 1, scaleY: 1, duration: 0.3, ease: 'power4.out', immediateRender: true }, 8.1);
tl.fromTo('#miss', { letterSpacing: '40px' }, { letterSpacing: '-1px', duration: 0.5, ease: 'expo.out', immediateRender: false }, 8.1);
sfx('boom', 8.1);
tl.fromTo('#stage-inner', { x: 0 }, { keyframes: [{ x: -18, duration: 0.05 }, { x: 14, duration: 0.05 }, { x: -6, duration: 0.05 }, { x: 0, duration: 0.08 }], immediateRender: false }, 8.15);

// S2 GEO 白底硬切 10–14.2
scene('#s2', 10, 14.2, { zoomFrom: 1, sfx: false });
tl.fromTo('#flash', { opacity: 1 }, { opacity: 0, duration: 0.25, immediateRender: false }, 10);
tl.fromTo('#geo', { autoAlpha: 0, scale: 3, letterSpacing: '60px' }, { autoAlpha: 1, scale: 1, letterSpacing: '-16px', duration: 0.5, ease: 'power4.in', immediateRender: true }, 10.05);
sfx('boom', 10.5);
tl.to('#geo', { scale: 1.06, duration: 3.5, ease: 'none' }, 10.6);
kinetic('#eq', 11.0, { st: 0.04, rx: 0, y: 50 });
tl.fromTo('#ul', { scaleX: 0 }, { scaleX: 1, duration: 0.5, ease: 'expo.out', immediateRender: true }, 11.6);
sfx('whoosh', 11.6);
rise('#eq2', 12.2, { y: 20, rx: 0, s: 1, sfx: false });

// S3 四件事 14.2–26:每件事整屏巨字,上一件向上推走
scene('#s3', 14.2, 26);
kinetic('#s3t', 14.3, { st: 0.03, rx: 0, y: 40 });
const items = [['#f1', 14.8, 18.2], ['#f2', 18.2, 20.3], ['#f3', 20.3, 22.4], ['#f4', 22.4, 26]];
rise('#ticks', 14.6, { y: 20, rx: 0, s: 1, sfx: false });
items.forEach(([s, t, t1], i) => {
  tl.fromTo(`${s} .no`, { autoAlpha: 0, y: 160 }, { autoAlpha: 1, y: 0, duration: 0.6, ease: EASE, immediateRender: true }, t);
  tl.fromTo(`${s} h3`, { autoAlpha: 0, y: 220, scale: 1.25 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.55, ease: 'expo.out', immediateRender: true }, t + 0.08);
  tl.fromTo(`${s} p`, { autoAlpha: 0, y: 40 }, { autoAlpha: 1, y: 0, duration: 0.6, ease: EASE, immediateRender: true }, t + 0.35);
  tl.fromTo(`#ticks i:nth-child(${i + 1}) b`, { scaleX: 0 }, { scaleX: 1, duration: 0.5, ease: 'power3.out', immediateRender: true }, t);
  sfx('whoosh', t); sfx('boom', t + 0.12);
  tl.fromTo(`${s} h3`, { x: 0 }, { keyframes: [{ x: 10, duration: 0.04 }, { x: -6, duration: 0.04 }, { x: 0, duration: 0.06 }], immediateRender: false }, t + 0.6);
  if (i < 3) tl.to(s, { y: -260, autoAlpha: 0, duration: 0.35, ease: 'power3.in' }, t1 - 0.3);
});

// S4 开源 26–31
scene('#s4', 26, 31);
rise('#term', 26.1, { y: 120, rx: 0, s: 1 });
typeText('#cmd1', 'git clone github.com/OmnirankGEO/Omnirank-GEO', 26.4, 1.0);
typeText('#cmd2', 'docker compose up -d …', 27.5, 0.6);
tl.fromTo('#ok', { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.2, immediateRender: true }, 28.2);
sfx('ding', 28.2);
['#w1', '#w2', '#w3'].forEach((s, i) => {
  const t = 28.6 + i * 0.45;
  tl.fromTo(s, { autoAlpha: 0, scale: 2.2 }, { autoAlpha: 1, scale: 1, duration: 0.3, ease: 'power4.in', immediateRender: true }, t);
  sfx('boom', t + 0.28);
  tl.fromTo('#stage-inner', { y: 0 }, { keyframes: [{ y: 12, duration: 0.04 }, { y: -6, duration: 0.05 }, { y: 0, duration: 0.08 }], immediateRender: false }, t + 0.3);
});

// S5 结尾 31–38.5(不放下集预告)
scene('#s5', 31, 38.5, { zoomTo: 1.0 });
impact('#e1', 31.2);
kinetic('#e2', 31.9, { st: 0.06, rx: 0, y: 50 });
tl.fromTo('#e2l', { scaleX: 0 }, { scaleX: 1, duration: 0.5, ease: 'expo.out', immediateRender: true }, 32.3);
rise('#e3', 32.8, { y: 60, rx: 0, s: 1, sfx: 'ding' });

finish(38.5);
