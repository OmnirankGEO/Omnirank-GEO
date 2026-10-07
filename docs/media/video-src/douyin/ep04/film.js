/* 第 4 集 · 粘土 3D + 色块:8 个比喻看懂一套 GEO 系统 */
const D = 36;
seriesBadge(4, 0.2, D);

// S0 0–3:8 个图标极速闪过,然后大字
scene('#s0', 0, 3.6, { cut: true, sfx: false, zoomTo: 1 });
for (let i = 1; i <= 8; i++) {
  const t = (i - 1) * 0.11;
  tl.set(`#fl${i}`, { autoAlpha: 1, scale: 1.15, rotation: i % 2 ? -6 : 6 }, t);
  if (i < 8) tl.set(`#fl${i}`, { autoAlpha: 0 }, t + 0.11);
  sfx('tick', t);
}
tl.to('#fl8', { scale: 0.75, y: -40, duration: 0.5, ease: 'back.out(2)' }, 0.9);
tl.fromTo('#h0a', { autoAlpha: 1, x: -900 }, { x: 0, duration: 0.3, ease: 'expo.out', immediateRender: true }, 0);
tl.fromTo('#h0b', { autoAlpha: 0, scale: 2.4 }, { autoAlpha: 1, scale: 1, duration: 0.3, ease: 'power4.in', immediateRender: true }, 0.95);
sfx('boom', 1.25);
tl.fromTo('#stage-inner', { y: 0 }, { keyframes: [{ y: 16, duration: 0.04 }, { y: -8, duration: 0.05 }, { y: 0, duration: 0.07 }], immediateRender: false }, 1.27);
rise('#h0c', 1.5, { y: 40, rx: 0, s: 1, sfx: false });

// 8 张色块卡:圆形擦除进场,图标拍上来,一句话
const T = [3, 6.6, 10.4, 14, 17.6, 21.2, 24.8, 28.4, 32];
for (let i = 1; i <= 8; i++) {
  const id = `#k${i}`, t0 = T[i - 1], t1 = T[i];
  tl.set(id, { autoAlpha: 1 }, t0);
  tl.fromTo(id, { clipPath: 'circle(0% at 50% 50%)' }, { clipPath: 'circle(80% at 50% 50%)', duration: 0.45, ease: 'power3.inOut', immediateRender: false }, t0);
  tl.set(id, { autoAlpha: 0 }, t1 + 0.45);
  tl.set(id, { zIndex: 3 + i }, t0);
  sfx('whoosh', t0);
  tl.fromTo(`${id} .num`, { autoAlpha: 0, y: -60 }, { autoAlpha: 1, y: 0, duration: 0.4, ease: EASE, immediateRender: true }, t0 + 0.15);
  tl.fromTo(`${id} .feat`, { autoAlpha: 0, x: -500 }, { autoAlpha: 1, x: 0, duration: 0.4, ease: 'expo.out', immediateRender: true }, t0 + 0.2);
  tl.fromTo(`${id} .eq`, { autoAlpha: 0, y: 30 }, { autoAlpha: 1, y: 0, duration: 0.4, ease: EASE, immediateRender: true }, t0 + 0.6);
  tl.fromTo(`${id} .ic`, { autoAlpha: 0, scale: 0.2, rotation: -20, y: 200 }, { autoAlpha: 1, scale: 1, rotation: 0, y: 0, duration: 0.6, ease: 'back.out(1.8)', immediateRender: true }, t0 + 0.4);
  sfx('pop', t0 + 0.45);
  tl.to(`${id} .ic`, { y: -18, rotation: i % 2 ? 3 : -3, duration: (t1 - t0 - 1) / 2, ease: 'sine.inOut', yoyo: true, repeat: 1 }, t0 + 1);
  tl.fromTo(`${id} .say`, { autoAlpha: 0, y: 30 }, { autoAlpha: 1, y: 0, duration: 0.4, ease: EASE, immediateRender: true }, t0 + 1.0);
}

// 结尾 32–36
tl.set('#end', { zIndex: 20 }, 32);
scene('#end', 32, D, { cut: true, zoomTo: 1 });
tl.fromTo('#end', { clipPath: 'circle(0% at 50% 50%)' }, { clipPath: 'circle(80% at 50% 50%)', duration: 0.45, ease: 'power3.inOut', immediateRender: false }, 32);
sfx('whoosh', 32);
kinetic('#e1', 32.2, { st: 0.04, rx: 0, y: 50, sfx: false });
kinetic('#e2', 32.4, { st: 0.04, rx: 0, y: 50, sfx: false });
for (let i = 1; i <= 8; i++) slap(`#m${i}`, 32.6 + i * 0.12, { rot: i % 2 ? -6 : 6, sfx: 'pop' });
rise('#e3', 33.8, { y: 40, rx: 0, s: 1, sfx: 'ding' });

finish(D);
