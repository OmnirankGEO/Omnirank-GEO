/* 第 6 集 · 漫画分镜风:想做 GEO 服务商?(诊断 → 报价 → 白标 → 交付 → 看板 → 记账) */
const D = 27;
seriesBadge(6, 0.2, D);
const shake = (t, a = 14) => tl.fromTo('#stage-inner', { x: 0, y: 0 }, { keyframes: [{ x: -a, y: a * 0.6, duration: 0.04 }, { x: a * 0.8, y: -a * 0.4, duration: 0.04 }, { x: 0, y: 0, duration: 0.06 }], immediateRender: false }, t);
const pop = (sel, t, rot = 0, s = 'stamp') => { tl.fromTo(sel, { autoAlpha: 0, scale: 2.8, rotation: rot - 20 }, { autoAlpha: 1, scale: 1, rotation: rot, duration: 0.25, ease: 'power4.in', immediateRender: true }, t); sfx(s, t + 0.24); shake(t + 0.26); };
const panel = (sel, t, from = { x: -1100 }) => { tl.fromTo(sel, { autoAlpha: 1, ...from }, { x: 0, y: 0, duration: 0.35, ease: 'expo.out', immediateRender: true }, t); sfx('whoosh', t); };

// S0 0–2.7 第一帧就是放射线 + 人物
scene('#s0', 0, 2.7, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#rays', { rotation: 0 }, { rotation: 40, duration: 2.7, ease: 'none', immediateRender: true }, 0);
tl.fromTo('#boss', { y: 300, scale: 0.9 }, { y: 0, scale: 1, duration: 0.4, ease: 'back.out(1.6)', immediateRender: true }, 0);
slap('#b0', 0.15, { sfx: 'pop' });
pop('#f0', 0.75, 12, 'boom');

// S1 2.7–6.4
scene('#s1', 2.7, 6.4, { cut: true, sfx: false, zoomTo: 1 });
panel('#p1a', 2.7);
slap('#b1', 3.2, { sfx: 'pop' });
panel('#p1b', 4.3, { x: 1100 });
pop('#f1', 4.7, -10, 'ding');
slap('#l1', 5.0, { rot: -2, sfx: false });

// S2 6.4–10.2
scene('#s2', 6.4, 10.2, { cut: true, sfx: false, zoomTo: 1 });
panel('#p2', 6.4, { y: 1400, x: 0 });
pop('#f2', 6.8, 10, 'whoosh');
['#q1', '#q2', '#q3', '#q4'].forEach((s, i) => { tl.fromTo(s, { autoAlpha: 0, x: -60 }, { autoAlpha: 1, x: 0, duration: 0.3, ease: EASE, immediateRender: true }, 7.4 + i * 0.45); sfx(i === 3 ? 'ding' : 'tick', 7.4 + i * 0.45); });

// S3 10.2–13.6 白标
scene('#s3', 10.2, 13.6, { cut: true, sfx: false, zoomTo: 1 });
panel('#p3', 10.2);
tl.to('#lg0', { opacity: 0, duration: 0.15 }, 11.4);
tl.fromTo('#lg1', { opacity: 0, scale: 0.4 }, { opacity: 1, scale: 1, duration: 0.35, ease: 'back.out(2.5)', immediateRender: false }, 11.4);
pop('#f3', 11.4, -8, 'boom');
rise('#n3', 12.0, { y: 10, rx: 0, s: 1, sfx: false });

// S4 13.6–17.2 流水线
scene('#s4', 13.6, 17.2, { cut: true, sfx: false, zoomTo: 1 });
panel('#p4', 13.6, { x: 1100 });
tl.fromTo('#cv', { x: 60 }, { x: -60, duration: 3.6, ease: 'none', immediateRender: true }, 13.6);
[['#k1', 14.2], ['#k2', 14.9], ['#k3', 15.6]].forEach(([s, t]) => slap(s, t, { rot: -3, sfx: 'pop' }));
slap('#l4', 16.2, { rot: 2, sfx: 'ding' });

// S5 17.2–20.6 客户看板
scene('#s5', 17.2, 20.6, { cut: true, sfx: false, zoomTo: 1 });
panel('#p5', 17.2, { y: 1400, x: 0 });
slap('#b5', 17.7, { sfx: 'pop' });
bob('#lap', 17.6, 20.6, 10);

// S6 20.6–27 账本 + 结尾
scene('#s6', 20.6, D, { cut: true, sfx: false, zoomTo: 1 });
panel('#p6', 20.6);
['#l6a', '#l6b', '#l6c'].forEach((s, i) => slap(s, 21.1 + i * 0.4, { rot: [-3, 2, -2][i], sfx: 'tick' }));
pop('#l6d', 22.4, 0, 'stamp');
slap('#b6', 23.4, { rot: -2, sfx: 'ding' });

finish(D);
