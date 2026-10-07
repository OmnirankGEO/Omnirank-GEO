/* 第 5 集 · 体育比分直播风:AI 到底推荐你没有(监测) */
const D = 24;
seriesBadge(5, 0.2, D);
bg('../assets/ep05_stadium.jpg', 0, D, { s0: 1.3, s1: 1.05, fade: 0.01, filter: 'blur(6px) brightness(.55)' });
const shake = (t, a = 14) => tl.fromTo('#stage-inner', { x: 0, y: 0 }, { keyframes: [{ x: -a, y: a * 0.6, duration: 0.04 }, { x: a * 0.8, y: -a * 0.4, duration: 0.04 }, { x: 0, y: 0, duration: 0.06 }], immediateRender: false }, t);
const barIn = (sel, t, from = -1200) => { tl.fromTo(sel, { autoAlpha: 1, x: from }, { x: 0, duration: 0.35, ease: 'expo.out', immediateRender: true }, t); sfx('whoosh', t); };

// S0 0–2.6 第一帧就有内容
scene('#s0', 0, 2.6, { cut: true, sfx: false });
tl.fromTo('#lv', { autoAlpha: 1 }, { autoAlpha: 0.35, duration: 0.25, yoyo: true, repeat: 9, ease: 'steps(1)' }, 0.1);
tl.fromTo('#h0a', { autoAlpha: 1, x: -300 }, { x: 0, duration: 0.25, ease: 'expo.out', immediateRender: true }, 0);
barIn('#h0b', 0.25);
tl.fromTo('#h0q', { autoAlpha: 0, scale: 3, rotation: 30 }, { autoAlpha: 1, scale: 1, rotation: 8, duration: 0.3, ease: 'power4.in', immediateRender: true }, 0.7);
sfx('boom', 1.0); shake(1.02, 18);
tl.to('#h0q', { rotation: -6, duration: 0.18, yoyo: true, repeat: 7, ease: 'sine.inOut' }, 1.1);

// S1 2.6–4.2 靠猜?
scene('#s1', 2.6, 4.2, { cut: true, sfx: false });
tl.fromTo('#s1a', { autoAlpha: 0, scale: 2 }, { autoAlpha: 1, scale: 1, duration: 0.25, ease: 'power4.in', immediateRender: true }, 2.6);
sfx('boom', 2.85);
tl.fromTo('#s1x', { autoAlpha: 0, scale: 2.5 }, { autoAlpha: 1, scale: 1, duration: 0.25, ease: 'power4.in', immediateRender: true }, 3.2);
sfx('stamp', 3.45); shake(3.47);

// S2 4.2–8.6 4 个 AI 名牌
scene('#s2', 4.2, 8.6, { cut: true, sfx: false });
barIn('#s2t', 4.2);
[['#p1', 5.6], ['#p2', 6.2], ['#p3', 6.8], ['#p4', 7.3]].forEach(([s, t]) => { barIn(s, t, 1200); tl.fromTo(`${s} em`, { autoAlpha: 0, scale: 0.4 }, { autoAlpha: 1, scale: 1, duration: 0.3, ease: 'back.out(2.5)', immediateRender: true }, t + 0.25); sfx('ding', t + 0.25); });

// S3 8.6–13 排名:你从第 4 冲到第 1
scene('#s3', 8.6, 13, { cut: true, sfx: false });
barIn('#s3t', 8.6);
['#r1', '#r2', '#r3', '#r4'].forEach((s, i) => { tl.fromTo(s, { autoAlpha: 0, x: -900 }, { autoAlpha: 1, x: 0, duration: 0.35, ease: 'expo.out', immediateRender: true }, 8.8 + i * 0.12); sfx('tick', 8.8 + i * 0.12); });
tl.to('#r4', { y: -420, duration: 0.7, ease: 'power3.inOut' }, 10.6);
['#r1', '#r2', '#r3'].forEach((s) => tl.to(s, { y: 140, duration: 0.7, ease: 'power3.inOut' }, 10.6));
tl.to('#s3 .r .o', { opacity: 0, y: -30, duration: 0.25 }, 10.9);
tl.fromTo('#s3 .r .v', { opacity: 0, y: 30 }, { opacity: 1, y: 0, duration: 0.25, immediateRender: false }, 10.9);
sfx('rise', 10.6); sfx('boom', 11.3); shake(11.32, 10);
tl.fromTo('#up', { autoAlpha: 0, scale: 0.3 }, { autoAlpha: 1, scale: 1, duration: 0.35, ease: 'back.out(3)', immediateRender: true }, 11.3);
burst(11.35, 500, 480, 70, '#ffd400');
rise('#s3f', 11.6, { y: 10, rx: 0, s: 1, sfx: false });

// S4 13–16.6 追到具体文章
scene('#s4', 13, 16.6, { cut: true, sfx: false });
barIn('#s4t', 13);
rise('#art', 13.3, { y: 160, rx: 0, s: 0.9 });
tl.fromTo('#s4s', { autoAlpha: 0, scale: 2.6 }, { autoAlpha: 1, scale: 1, duration: 0.28, ease: 'power4.in', immediateRender: true }, 14.6);
sfx('stamp', 14.88); shake(14.9);
rise('#s4f', 15.0, { y: 10, rx: 0, s: 1, sfx: false });

// S5 16.6–19.2 下跌提醒
scene('#s5', 16.6, 19.2, { cut: true, sfx: false });
barIn('#s5t', 16.6);
draw('#ln', 16.8, 1.0);
tl.to('#ln', { stroke: '#e10600', duration: 0.15 }, 17.6);
tl.fromTo('#s5s', { autoAlpha: 0, scale: 2.4 }, { autoAlpha: 1, scale: 1, duration: 0.25, ease: 'power4.in', immediateRender: true }, 17.9);
sfx('stamp', 18.15); shake(18.17);
tl.fromTo('#s5s', { x: 0 }, { keyframes: [{ x: -8, duration: 0.05 }, { x: 8, duration: 0.05 }], repeat: 4, immediateRender: false }, 18.3);
rise('#s5f', 18.2, { y: 10, rx: 0, s: 1, sfx: false });

// S6 19.2–24 结尾
scene('#s6', 19.2, D, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#e1', { autoAlpha: 1, x: -400 }, { x: 0, duration: 0.3, ease: 'expo.out', immediateRender: true }, 19.2);
barIn('#e2', 19.6); sfx('boom', 19.9); shake(19.92);
barIn('#e3', 20.6, 1200);
sfx('ding', 20.8);

finish(D);
