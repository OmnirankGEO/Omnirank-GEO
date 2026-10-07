/* 第 3 集 · 大促海报风:别人卖你一个轮子,我们把整辆车开源了 */
const D = 28;
seriesBadge(3, 0.2, D);
const shake = (t, a = 16) => tl.fromTo('#stage-inner', { x: 0, y: 0 }, { keyframes: [{ x: -a, y: a * 0.6, duration: 0.04 }, { x: a * 0.8, y: -a * 0.4, duration: 0.04 }, { x: -a * 0.3, y: 0, duration: 0.05 }, { x: 0, duration: 0.06 }], immediateRender: false }, t);

// S0 0–2.5 第一帧就有字
scene('#s0', 0, 2.5, { cut: true, sfx: false });
tl.fromTo('#h0a', { autoAlpha: 1, x: -260 }, { x: 0, duration: 0.25, ease: 'expo.out', immediateRender: true }, 0);
sfx('whoosh', 0);
tl.fromTo('#h0b', { autoAlpha: 0, scale: 2.6 }, { autoAlpha: 1, scale: 1, duration: 0.3, ease: 'power4.in', immediateRender: true }, 0.08);
sfx('boom', 0.36); shake(0.38);
tl.fromTo('#h0c', { autoAlpha: 0, scale: 3, rotation: -25 }, { autoAlpha: 1, scale: 1, rotation: -6, duration: 0.32, ease: 'power4.in', immediateRender: true }, 1.0);
sfx('stamp', 1.3); shake(1.32, 22);
tl.fromTo('#flash', { opacity: 0.5 }, { opacity: 0, duration: 0.3, immediateRender: false }, 1.32);
burst(1.32, 500, 900, 70, '#0d0d0d');
tl.to('#h0c', { scale: 1.05, duration: 1.1, ease: 'none' }, 1.4);

// S1 2.5–9.8 价格牌一个个砸下来
scene('#s1', 2.5, 9.8, { cut: true });
slap('#s1t', 2.55, { sfx: false });
[['#t1', 3.4], ['#t2', 6.2], ['#t3', 7.6]].forEach(([s, t], i) => {
  tl.fromTo(s, { autoAlpha: 0, y: -500, rotation: [-8, 6, -4][i] }, { autoAlpha: 1, y: 0, rotation: [-2, 1.5, -1][i], duration: 0.45, ease: 'bounce.out', immediateRender: true }, t);
  sfx('stamp', t + 0.3);
});
countUp('#t3v', 0, 15, 7.75, 0.8);
shake(7.95, 12);
rise('#s1n', 8.4, { y: 20, rx: 0, s: 1, sfx: false });

// S2 9.8–16.2 只卖零件 → 一个轮子
scene('#s2', 9.8, 16.2, { cut: true });
kinetic('#s2t', 9.85, { st: 0.03, rx: 0, y: 60 });
slap('#x1', 11.6, { rot: -3, sfx: 'stamp' });
slap('#x2', 12.6, { rot: 2, sfx: 'stamp' });
tl.fromTo('#wh', { autoAlpha: 1, x: 900, rotation: 0 }, { x: 0, rotation: -360, duration: 0.9, ease: 'power3.out', immediateRender: true }, 13.8);
sfx('whoosh', 13.8); sfx('boom', 14.6);
tl.to('#wh', { rotation: '-=12', duration: 0.12, yoyo: true, repeat: 3, ease: 'sine.inOut' }, 14.7);

// S3 16.2–24 整辆车 + 8 个模块
scene('#s3', 16.2, 24, { cut: true });
kinetic('#s3t', 16.25, { st: 0.03, rx: 0, y: 60, sfx: false });
tl.fromTo('#car', { autoAlpha: 1, x: -1300, skewX: 18 }, { x: 0, skewX: 0, duration: 0.7, ease: 'expo.out', immediateRender: true }, 16.4);
sfx('whoosh', 16.4); sfx('boom', 16.9); shake(16.95, 10);
for (let i = 1; i <= 8; i++) slap(`#c${i}`, 17.8 + (i - 1) * 0.45, { rot: i % 2 ? -4 : 4, sfx: 'pop' });
rise('#s3l', 21.6, { y: 40, rx: 0, s: 1, sfx: 'ding' });
bob('#car', 17.2, 24, 8);

// S4 24–28 结尾,不留长空白
scene('#s4', 24, D, { cut: true, zoomTo: 1 });
['#e0', '#e1', '#e2'].forEach((s, i) => { tl.fromTo(s, { autoAlpha: 0, x: -600 }, { autoAlpha: 1, x: 0, duration: 0.3, ease: 'expo.out', immediateRender: true }, 24.05 + i * 0.25); sfx('boom', 24.1 + i * 0.25); });
slap('#e3', 25.0, { rot: -2, sfx: 'ding' });
rise('#e4', 25.4, { y: 20, rx: 0, s: 1, sfx: false });

finish(D);
