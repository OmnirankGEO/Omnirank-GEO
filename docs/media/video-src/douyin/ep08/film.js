/* 第 8 集 · 等距小岛:越用越准的飞轮(学规律 → 用回去 → 人确认) */
const D = 23;
seriesBadge(8, 0.2, D);
BG_IMGS.push('../assets/ep08_town.jpg');
// 虚拟镜头:cam(s, x, y) = 把小岛上 (x, y) 这一点放大 s 倍放到画面中心
const cam = (t, s, x, y, d = 0.9, ease = 'power3.inOut') => tl.to('#cam', { scale: s, x: 540 - x * s, y: 900 - y * s, duration: d, ease }, t);
gsap.set('#cam', { scale: 1.25, x: 540 - 540 * 1.25, y: 900 - 900 * 1.25 });
const ui = (sel, t, s = 'pop') => { tl.fromTo(sel, { autoAlpha: 0, y: 60, scale: 0.85 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.45, ease: 'back.out(2)', immediateRender: true }, t); if (s) sfx(s, t); };
const pin = (sel, t) => { tl.fromTo(sel, { autoAlpha: 0, y: -80, scale: 0.6 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.45, ease: 'back.out(2.2)', immediateRender: true }, t); sfx('ding', t); };

// S0 0–2.7 全景缓推,第一帧就有标题
scene('#s0', 0, 2.7, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#cam', { scale: 1.0, x: 0, y: 0 }, { scale: 1.25, x: 540 - 540 * 1.25, y: 900 - 900 * 1.25, duration: 2.7, ease: 'power1.inOut', immediateRender: true }, 0);
tl.fromTo('#h0', { autoAlpha: 1, x: -500 }, { x: 0, duration: 0.3, ease: 'expo.out', immediateRender: true }, 0);
sfx('whoosh', 0);
tl.fromTo('#h0', { scale: 1 }, { scale: 1.06, duration: 0.15, yoyo: true, repeat: 1 }, 0.9);
sfx('boom', 0.9);

// S1 2.7–6.9 镜头到信号塔:每天问 AI,收集引用的文章
scene('#s1', 2.7, 6.9, { cut: true, sfx: false, zoomTo: 1 });
cam(2.7, 2.2, 245, 520);
sfx('whoosh', 2.7);
pin('#pn1', 3.2);
cam(4.6, 2.0, 330, 640, 1.6, 'sine.inOut');
ui('#u1', 4.8, 'tick');

// S2 6.9–10.7 镜头到房子:学规律
scene('#s2', 6.9, 10.7, { cut: true, sfx: false, zoomTo: 1 });
cam(6.9, 2.3, 860, 1060);
sfx('whoosh', 6.9);
pin('#pn2', 7.3);
ui('#u2', 7.9);
['#g1', '#g2', '#g3'].forEach((s, i) => { tl.fromTo(s, { autoAlpha: 0, scale: 0.3 }, { autoAlpha: 1, scale: 1, duration: 0.35, ease: 'back.out(3)', immediateRender: true }, 8.5 + i * 0.4); sfx('pop', 8.5 + i * 0.4); });

// S3 10.7–14.3 镜头到中心飞轮:用回去
scene('#s3', 10.7, 14.3, { cut: true, sfx: false, zoomTo: 1 });
cam(10.7, 2.6, 540, 840);
sfx('rise', 10.7);
pin('#pn3', 11.1);
tl.to('#cam', { rotation: 0, duration: 0.01 }, 11.6);
cam(11.7, 2.9, 540, 840, 2.4, 'sine.inOut');
ui('#u3', 12.0, 'ding');
burst(12.4, 540, 900, 60, '#ff7a45');

// S4 14.3–17.9 人确认
scene('#s4', 14.3, 17.9, { cut: true, sfx: false, zoomTo: 1 });
cam(14.3, 1.6, 540, 1100);
tl.to('#cam', { filter: 'blur(6px) brightness(1.08)', duration: 0.4 }, 14.3);
tl.to('#cam', { filter: 'blur(0px) brightness(1)', duration: 0.4 }, 17.6);
pin('#pn4', 14.5);
ui('#dk', 14.9);
tl.fromTo('#dk', { rotation: 0 }, { keyframes: [{ y: -14, duration: 0.12 }, { y: 6, duration: 0.08 }, { y: 0, duration: 0.1 }], immediateRender: false }, 16.0);
sfx('stamp', 16.1);
tl.fromTo('#stage-inner', { y: 0 }, { keyframes: [{ y: 10, duration: 0.04 }, { y: -5, duration: 0.05 }, { y: 0, duration: 0.07 }], immediateRender: false }, 16.12);
ui('#u4', 16.3, false);

// S5 17.9–23 拉回全景
scene('#s5', 17.9, D, { cut: true, sfx: false, zoomTo: 1 });
cam(17.9, 1.0, 540, 900, 1.2, 'power2.inOut');
tl.to('#cam', { x: 0, y: 0, duration: 1.2, ease: 'power2.inOut' }, 17.9);
sfx('whoosh', 17.9);
ui('#e1', 18.3, 'boom');
ui('#e2', 19.2, 'ding');

finish(D);
