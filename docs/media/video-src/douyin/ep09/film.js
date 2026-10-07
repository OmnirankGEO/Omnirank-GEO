/* 第 9 集 · 浅色终端风:一句话把系统装好(AGENTS.md 交给 AI 部署) */
const D = 25;
seriesBadge(9, 0.2, D);
const shake = (t, a = 12) => tl.fromTo('#stage-inner', { x: 0, y: 0 }, { keyframes: [{ x: -a, y: a * 0.6, duration: 0.04 }, { x: a * 0.8, y: -a * 0.4, duration: 0.04 }, { x: 0, y: 0, duration: 0.06 }], immediateRender: false }, t);
const win = (sel, t, from = 600) => { tl.fromTo(sel, { autoAlpha: 0, y: from, scale: 0.92 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.45, ease: 'expo.out', immediateRender: true }, t); sfx('whoosh', t); };
const blink = (sel, t0, t1) => tl.fromTo(sel, { opacity: 1 }, { opacity: 0, duration: 0.25, ease: 'steps(1)', yoyo: true, repeat: Math.floor((t1 - t0) / 0.25), immediateRender: false }, t0);

// S0 0–2.5
scene('#s0', 0, 2.5, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#h0a', { autoAlpha: 1, x: -300 }, { x: 0, duration: 0.25, ease: 'expo.out', immediateRender: true }, 0);
tl.fromTo('#h0b', { autoAlpha: 0, x: -300 }, { autoAlpha: 1, x: 0, duration: 0.25, ease: 'expo.out', immediateRender: true }, 0.25);
sfx('whoosh', 0);
tl.fromTo('#h0n', { autoAlpha: 0, scale: 3 }, { autoAlpha: 1, scale: 1, duration: 0.28, ease: 'power4.in', immediateRender: true }, 0.9);
sfx('boom', 1.18); shake(1.2, 18);
tl.fromTo('#h0c', { autoAlpha: 0, x: 200 }, { autoAlpha: 1, x: 0, duration: 0.3, ease: 'expo.out', immediateRender: true }, 1.3);

// S1 2.5–9.6 克隆 + 对 AI 说一句话
scene('#s1', 2.5, 9.6, { cut: true, sfx: false, zoomTo: 1 });
win('#w1', 2.5);
typeText('#c1', 'git clone …/Omnirank-GEO', 2.9, 1.0);
blink('#cu1', 2.6, 9.6);
win('#w2', 4.3);
tl.fromTo('#sy', { autoAlpha: 0, scale: 0.4, y: 40 }, { autoAlpha: 1, scale: 1, y: 0, duration: 0.45, ease: 'back.out(2.2)', immediateRender: true }, 5.0);
sfx('pop', 5.0);
tl.fromTo('#pl', { autoAlpha: 0, x: -300 }, { autoAlpha: 1, x: 0, duration: 0.35, ease: 'expo.out', immediateRender: true }, 6.8);
sfx('ding', 6.8);

// S2 9.6–14.5 逐条打勾
scene('#s2', 9.6, 14.5, { cut: true, sfx: false, zoomTo: 1 });
win('#w3', 9.6);
for (let i = 1; i <= 7; i++) { const t = 10.0 + (i - 1) * 0.6; tl.fromTo(`#l${i}`, { opacity: 0, x: -30 }, { opacity: 1, x: 0, duration: 0.25, ease: EASE, immediateRender: true }, t); sfx(i === 7 ? 'ding' : 'tick', t); }
tl.fromTo('#pb', { scaleX: 0 }, { scaleX: 1, duration: 3.8, ease: 'power1.inOut', immediateRender: true }, 10.0);
tl.fromTo('#done', { autoAlpha: 0, scale: 2.6 }, { autoAlpha: 1, scale: 1, duration: 0.26, ease: 'power4.in', immediateRender: true }, 13.8);
sfx('stamp', 14.05); shake(14.07, 14);

// S3 14.5–18 安全
scene('#s3', 14.5, 18, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#s3t', { autoAlpha: 1, x: -400 }, { x: 0, duration: 0.3, ease: 'expo.out', immediateRender: true }, 14.5);
sfx('whoosh', 14.5);
win('#w4', 14.8);
tl.fromTo('#w4 .term div', { opacity: 0, x: -30 }, { opacity: 1, x: 0, duration: 0.3, stagger: 0.5, ease: EASE, immediateRender: true }, 15.2);
[15.2, 15.7, 16.2].forEach((t) => sfx('tick', t));

// S4 18–20.6 真实界面
scene('#s4', 18, 20.6, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#s4t', { autoAlpha: 1, x: -400 }, { x: 0, duration: 0.3, ease: 'expo.out', immediateRender: true }, 18);
win('#w5', 18.2, 900);
tl.fromTo('#app', { x: 0, y: 0 }, { x: -380, y: -60, duration: 2.2, ease: 'sine.inOut', immediateRender: true }, 18.5);
sfx('ding', 18.7);

// S5 20.6–25
scene('#s5', 20.6, D, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#e1', { autoAlpha: 0, scale: 2.2 }, { autoAlpha: 1, scale: 1, duration: 0.26, ease: 'power4.in', immediateRender: true }, 20.6);
sfx('boom', 20.85); shake(20.87);
win('#e2', 21.0, 300);

finish(D);
