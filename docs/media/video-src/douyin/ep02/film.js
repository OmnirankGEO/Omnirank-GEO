/* 第 2 集 · 贴纸风科普:GEO 到底从哪来 */
const D = 50;
seriesBadge(2, 0.2, D);
const wob = (sel, t0, t1, a = 3) => tl.to(sel, { rotation: `+=${a}`, duration: (t1 - t0) / 4, ease: 'sine.inOut', yoyo: true, repeat: 3 }, t0);

// S0 钩子 0–4
scene('#s0', 0, 4, { sfx: false, zoomFrom: 1 });
slap('#h0a', 0.1, { rot: -2 });
kinetic('#h0b', 0.6, { st: 0.04, rx: 0, y: 60, sfx: false });
mark('#h0m', 1.1);
slap('#h0p', 1.6, { rot: 6, sfx: 'stamp' });
slap('#h0t', 2.3, { rot: -6, sfx: 'stamp' });
bob('#h0p', 2.2, 4, 14);

// S1 搜索 vs AI 4–12
scene('#s1', 4, 12);
kinetic('#s1t', 4.1, { st: 0.05, rx: 0, y: 60 });
mark('#s1m', 4.6);
slap('#s1a', 4.6, { rot: -5 });
slap('#s1at', 5.4, { rot: -3, sfx: 'tick' });
slap('#s1b', 7.6, { rot: 5 });
draw('#s1arr path', 7.4, 0.5);
slap('#s1bt', 8.2, { rot: 4, sfx: 'ding' });
bob('#s1b', 8.2, 12, 12);
rise('#s1q', 9.6, { y: 50, rx: 0, s: 0.9, sfx: 'pop' });
tl.fromTo('#s1q', { scale: 1 }, { scale: 1.06, duration: 0.25, yoyo: true, repeat: 1, ease: 'sine.inOut' }, 10.4);

// S2 论文 12–19
scene('#s2', 12, 19);
slap('#s2p', 12.1, { rot: -4, sfx: 'stamp' });
slap('#s2d', 12.6, { rot: -8 });
slap('#s2u', 13.6, { rot: 3 });
slap('#s2n', 15.6, { rot: -2, sfx: 'ding' });
rise('#s2c', 16.8, { y: 40, rx: 0, s: 0.9, sfx: false });
mark('#s2m', 17.6);
wob('#s2p', 13, 19, 3);

// S3 实验 19–30
scene('#s3', 19, 30);
kinetic('#s3t', 19.2, { st: 0.025, rx: 0, y: 40 });
mark('#s3m', 20.2);
[['#s3a', '#s3at', 23.0], ['#s3b', '#s3bt', 23.8], ['#s3c', '#s3ct', 24.6]].forEach(([a, b, t], i) => { slap(a, t, { rot: [-6, 4, -3][i] }); slap(b, t + 0.2, { rot: [3, -3, 4][i], sfx: 'tick' }); });
slap('#s3w', 26.0, { rot: -6, sfx: 'stamp' });
tl.fromTo('#s3n', { autoAlpha: 0, scale: 0.4 }, { autoAlpha: 1, scale: 1, duration: 0.5, ease: 'back.out(2)', immediateRender: true }, 26.0);
countUp('#s3v', 0, 40, 26.1, 1.4);
tl.fromTo('#s3n', { scale: 1 }, { scale: 1.12, duration: 0.18, yoyo: true, repeat: 1, ease: 'power2.out' }, 27.5);
sfx('ding', 27.5);
rise('#s3s', 27.6, { y: 20, rx: 0, s: 1, sfx: false });
bob('#s3w', 27, 30, 10);

// S4 堆关键词 30–36
scene('#s4', 30, 36);
slap('#s4m', 30.1, { rot: -8 });
tl.fromTo('#s4m', { x: 0 }, { keyframes: [{ x: -10, duration: 0.05 }, { x: 10, duration: 0.05 }], repeat: 6, immediateRender: false }, 30.6);
tl.fromTo('#s4x', { autoAlpha: 0, scale: 3, rotation: -30 }, { autoAlpha: 1, scale: 1, rotation: 0, duration: 0.35, ease: 'power4.in', immediateRender: true }, 31.0);
sfx('boom', 31.33);
slap('#s4t', 31.5, { rot: -3, sfx: 'stamp' });
slap('#s4c1', 33.2, { sfx: 'ding' });
slap('#s4c2', 33.9, { sfx: 'ding' });

// S5 OmniRank 36–43.4
scene('#s5', 36, 43.4);
slap('#s5r', 36.1, { rot: 8 });
kinetic('#s5t', 36.2, { st: 0.04, rx: 0, y: 50 });
mark('#s5m', 36.9);
[['#c1', 39.4], ['#c2', 40.6], ['#c3', 41.6]].forEach(([s, t]) => { tl.fromTo(s, { autoAlpha: 0, x: -80 }, { autoAlpha: 1, x: 0, duration: 0.5, ease: 'back.out(1.8)', immediateRender: true }, t); sfx(s === '#c3' ? 'stamp' : 'ding', t); });
bob('#s5r', 37, 43.4, 14);

// S6 结尾 43.4–50(不放下集预告)
scene('#s6', 43.4, D, { zoomTo: 1.0 });
slap('#e1', 43.5, { rot: -2, sfx: 'stamp' });
kinetic('#e2', 44.0, { st: 0.06, rx: 0, y: 40, sfx: false });
slap('#e3', 44.6, { rot: -3, sfx: 'ding' });
slap('#e4', 45.3, { rot: 4 });

finish(D);
