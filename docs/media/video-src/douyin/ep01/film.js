/* 第 1 集 · 科技发布会风:我们把一整套 GEO 系统开源了 */
aurora(['rgba(47,212,122,.55)', 'rgba(40,120,255,.45)', 'rgba(130,60,255,.35)', 'rgba(47,212,122,.35)']);
particles(90, '#8ff5bf');
const A = '../assets/';
bg(A + 'ep01_stage.jpg', 0, 3.4, { s0: 1.25, s1: 1.05, y0: 60 });
bg(A + 'ep01_city.jpg', 3.4, 10, { s0: 1.15, s1: 1.0, y0: -80, y1: 40, filter: 'blur(3px) brightness(.8)' });
bg(A + 'ep01_core.jpg', 10, 14.2, { s0: 1.0, s1: 1.15, y0: -170, y1: -190 });
bg(A + 'ep01_city.jpg', 14.2, 31, { s0: 1.3, s1: 1.1, x0: -60, x1: 60, filter: 'blur(10px) brightness(.55)' });
bg(A + 'ep01_stage.jpg', 31, 38.5, { s0: 1.0, s1: 1.15, y1: -40 });
seriesBadge(1, 0.2, 38.5);

// S0 钩子 0–3.4
scene('#s0', 0, 3.4, { sfx: false });
kinetic('#h0a', 0.1, { st: 0.05, sfx: 'tick' });
impact('#h0b', 0.45);
burst(0.92, 540, 700, 90);
rise('#h0c', 1.25, { y: 80, rx: 0, s: 0.7, sfx: 'boom' });
tl.to('#h0b', { scale: 1.06, duration: 2.2, ease: 'sine.inOut' }, 1.0);

// S1 手机问 AI 3.4–10
scene('#s1', 3.4, 10);
rise('#ph', 3.5, { y: 260, rx: 35, s: 0.85 });
tl.fromTo('#ph', { rotationY: -10 }, { rotationY: 8, duration: 6.4, ease: 'sine.inOut', immediateRender: false }, 3.5);
rise('#q1', 4.0, { y: 40, rx: 0, sfx: 'pop' });
typeText('#q1t', '深圳商务用车哪家好？', 4.2, 1.2);
rise('#a1', 5.7, { y: 60, rx: 0, sfx: 'ding' });
['#n1', '#n2', '#n3'].forEach((s, i) => { tl.fromTo(s, { autoAlpha: 0, scale: 0.5 }, { autoAlpha: 1, scale: 1, duration: 0.5, ease: 'back.out(2)', immediateRender: true }, 6.1 + i * 0.25); sfx('pop', 6.1 + i * 0.25); });
tl.to(['#n1', '#n2', '#n3'], { boxShadow: '0 0 30px rgba(47,212,122,.8)', duration: 0.4, stagger: 0.1 }, 7.1);
rise('#miss', 8.1, { y: 60, rx: 0, sfx: 'boom' });
tl.fromTo('#miss', { x: 0 }, { keyframes: [{ x: -16, duration: 0.05 }, { x: 14, duration: 0.05 }, { x: -8, duration: 0.05 }, { x: 0, duration: 0.05 }], immediateRender: false }, 8.6);

// S2 GEO 定义 10–14.2
scene('#s2', 10, 14.2);
impact('#geo', 10.25);
burst(10.7, 540, 520, 80);
kinetic('#eq', 11.2, { st: 0.04 });
rise('#eq2', 12.3, { y: 30, rx: 0, sfx: false });
tl.to('#geo', { scale: 1.05, duration: 3, ease: 'sine.inOut' }, 10.8);

// S3 功能卡 14.2–26
scene('#s3', 14.2, 26);
kinetic('#s3t', 14.3, { st: 0.04 });
[['#f1', 14.8], ['#f2', 18.2], ['#f3', 20.3], ['#f4', 22.4]].forEach(([s, t]) => {
  rise(s, t, { y: 140, rx: 45, s: 0.9 });
  sweep(s, t + 0.6, 0.9);
  tl.fromTo(`${s} .ic`, { scale: 0.6, rotation: -20 }, { scale: 1, rotation: 0, duration: 0.8, ease: 'back.out(2)', immediateRender: false }, t + 0.2);
  sfx('ding', t + 0.5);
});

// S4 开源部署 26–31
scene('#s4', 26, 31);
rise('#term', 26.2, { y: 180, rx: 30 });
typeText('#cmd1', 'git clone github.com/OmnirankGEO/Omnirank-GEO', 26.6, 1.2);
typeText('#cmd2', 'docker compose up -d …', 28.0, 0.8);
tl.fromTo('#ok', { autoAlpha: 0, x: -30 }, { autoAlpha: 1, x: 0, duration: 0.5, ease: EASE, immediateRender: true }, 29.0);
sfx('ding', 29.0);
['#p1', '#p2', '#p3'].forEach((s, i) => { tl.fromTo(s, { autoAlpha: 0, y: 60, scale: 0.6 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.6, ease: 'back.out(2)', immediateRender: true }, 29.5 + i * 0.2); sfx('pop', 29.5 + i * 0.2); });

// S5 结尾 31–38.5
scene('#s5', 31, 38.5, { zoomTo: 1.0 });
impact('#e1', 31.3);
burst(31.75, 540, 420, 100);
kinetic('#e2', 32.0, { st: 0.08 });
rise('#e3', 32.8, { y: 80, rx: 0 });
sweep('#e3 .glass', 33.5, 1.0);
rise('#e4', 34.2, { y: 40, rx: 0, sfx: 'ding' });

finish(38.5);
