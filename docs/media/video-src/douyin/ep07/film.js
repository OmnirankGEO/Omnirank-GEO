/* 第 7 集 · 综艺分屏风:为什么 AI 不信「行业第一」(创作中心事实核查) */
const D = 24;
seriesBadge(7, 0.2, D);
const shake = (t, a = 14) => tl.fromTo('#stage-inner', { x: 0, y: 0 }, { keyframes: [{ x: -a, y: a * 0.6, duration: 0.04 }, { x: a * 0.8, y: -a * 0.4, duration: 0.04 }, { x: 0, y: 0, duration: 0.06 }], immediateRender: false }, t);
const slam = (sel, t, s = 'boom', from = 2.6) => { tl.fromTo(sel, { autoAlpha: 0, scale: from }, { autoAlpha: 1, scale: 1, duration: 0.26, ease: 'power4.in', immediateRender: true }, t); sfx(s, t + 0.25); shake(t + 0.27); };
tl.fromTo('#bg0', { backgroundPosition: '0 0' }, { backgroundPosition: '0 0', duration: D }, 0);

// S0 0–2.5 第一帧:金牌堆 + 花字
scene('#s0', 0, 2.5, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#md', { autoAlpha: 1, y: 500, rotation: -10 }, { y: 0, rotation: 0, duration: 0.4, ease: 'back.out(1.6)', immediateRender: true }, 0);
tl.fromTo('#h0a', { autoAlpha: 1, x: -300 }, { x: 0, duration: 0.25, ease: 'expo.out', immediateRender: true }, 0);
sfx('whoosh', 0);
slam('#h0b', 0.8);
tl.to('#md', { rotation: 4, duration: 0.15, yoyo: true, repeat: 5, ease: 'sine.inOut' }, 1.2);

// S1 2.5–14 A vs B + 评委
scene('#s1', 2.5, 14, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#cA', { autoAlpha: 1, x: -1100, rotation: -6 }, { x: 0, rotation: -2, duration: 0.4, ease: 'expo.out', immediateRender: true }, 2.6);
sfx('whoosh', 2.6);
slam('#vs', 3.0, 'stamp', 3);
tl.to('#vs', { rotation: 360, duration: 10, ease: 'none' }, 3.3);
tl.fromTo('#cB', { autoAlpha: 1, x: 1100, rotation: 6 }, { x: 0, rotation: 2, duration: 0.4, ease: 'expo.out', immediateRender: true }, 5.6);
sfx('whoosh', 5.6);
tl.fromTo('#jd', { autoAlpha: 0, y: 300, scale: 0.6 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.5, ease: 'back.out(1.8)', immediateRender: true }, 8.6);
sfx('pop', 8.6);
slam('#qm', 9.0, 'pop', 2);
tl.to('#qm', { rotation: 15, duration: 0.2, yoyo: true, repeat: 3, ease: 'sine.inOut' }, 9.3);
tl.to(['#qm'], { autoAlpha: 0, duration: 0.2 }, 10.2);
tl.to('#jd', { y: 900, autoAlpha: 0, duration: 0.5, ease: 'power3.in' }, 10.2);
tl.to('#vs', { autoAlpha: 0, scale: 0.3, duration: 0.3, ease: 'power3.in' }, 11.0);
tl.to('#cA', { filter: 'grayscale(1) brightness(.8)', scale: 0.94, duration: 0.3 }, 10.3);
slam('#xA', 10.3, 'stamp', 3);
tl.to('#cB', { scale: 1.06, boxShadow: '16px 16px 0 #2b0f4a, 0 0 0 16px #ffe14d', duration: 0.3, ease: 'back.out(2)' }, 10.6);
sfx('ding', 10.6);
slam('#okB', 10.8, 'pop', 2);
burst(10.9, 540, 1150, 80, '#ffe14d');
tl.fromTo('#why2', { autoAlpha: 0, x: -400 }, { autoAlpha: 1, x: 0, duration: 0.3, ease: 'expo.out', immediateRender: true }, 11.4);
tl.fromTo('#why1', { autoAlpha: 0, x: -400 }, { autoAlpha: 1, x: 0, duration: 0.3, ease: 'expo.out', immediateRender: true }, 12.3);
sfx('whoosh', 11.4); sfx('whoosh', 12.3);

// S2 14–19.6 创作中心挑刺
scene('#s2', 14, 19.6, { cut: true, sfx: false, zoomTo: 1 });
slam('#s2t', 14.05, 'boom', 2);
tl.fromTo('#doc', { autoAlpha: 1, y: 900 }, { y: 0, duration: 0.45, ease: 'expo.out', immediateRender: true }, 14.4);
sfx('whoosh', 14.4);
mark('#dm', 15.4, 0.4); sfx('tick', 15.4);
tl.fromTo('#ds', { '--k': 0 }, { '--k': 1, duration: 0.3, ease: 'power2.out', immediateRender: true }, 16.4);
sfx('stamp', 16.6);
tl.fromTo('#fix', { autoAlpha: 0, scale: 0.3 }, { autoAlpha: 1, scale: 1, duration: 0.4, ease: 'back.out(2.5)', immediateRender: true }, 17.3);
sfx('ding', 17.3);

// S3 19.6–24 结尾
scene('#s3', 19.6, D, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#e1', { autoAlpha: 1, x: -400 }, { x: 0, duration: 0.25, ease: 'expo.out', immediateRender: true }, 19.6);
slam('#e2', 19.85);
tl.fromTo('#bz', { autoAlpha: 0, y: 300 }, { autoAlpha: 1, y: 0, duration: 0.45, ease: 'back.out(2)', immediateRender: true }, 20.2);
tl.fromTo('#bz', { scaleY: 1 }, { scaleY: 0.85, duration: 0.08, yoyo: true, repeat: 1 }, 20.8);
sfx('ding', 20.85);
tl.fromTo('#e3', { autoAlpha: 0, y: 40 }, { autoAlpha: 1, y: 0, duration: 0.35, ease: EASE, immediateRender: true }, 21.0);

finish(D);
