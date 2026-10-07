/* 第 10 集 · 像素游戏风:30 秒通关一整套 GEO 系统(系列收官) */
const D = 27;
seriesBadge(10, 0.2, D);
BG_IMGS.push('../assets/ep10_map.jpg');
const S = 1.8;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
// 把地图上 (x, y) 放到主角脚下(画面 540, 1150)
const camTo = (t, x, y, d = 0.8, s = S) => tl.to('#cam', { scale: s, x: clamp(540 - x * s, 1080 - 1080 * s, 0), y: clamp(1150 - y * s, 1920 - 1920 * s, 0), duration: d, ease: 'power2.inOut' }, t);
const shake = (t, a = 12) => tl.fromTo('#stage-inner', { x: 0, y: 0 }, { keyframes: [{ x: -a, y: a * 0.6, duration: 0.04 }, { x: a * 0.8, y: -a * 0.4, duration: 0.04 }, { x: 0, y: 0, duration: 0.06 }], immediateRender: false }, t);
// 主角一路小跳着走
tl.fromTo('#hero', { y: 0 }, { y: -18, duration: 0.16, ease: 'steps(1)', yoyo: true, repeat: Math.floor(21 / 0.16), immediateRender: false }, 0);

// S0 0–3 标题 + 地图全景推进
scene('#s0', 0, 3, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#cam', { scale: 1, x: 0, y: 0 }, { scale: S, x: clamp(540 - 520 * S, 1080 - 1080 * S, 0), y: 1920 - 1920 * S, duration: 2.8, ease: 'power2.inOut', immediateRender: true }, 0);
tl.fromTo('#t0', { autoAlpha: 1, y: -120 }, { y: 0, duration: 0.3, ease: 'bounce.out', immediateRender: true }, 0);
sfx('boom', 0.3); shake(0.32);
tl.fromTo('#ps', { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.2, ease: 'steps(1)', yoyo: true, repeat: 9, immediateRender: true }, 0.8);
tl.fromTo('#hero', { autoAlpha: 0, scale: 0.3 }, { autoAlpha: 1, scale: 1, duration: 0.4, ease: 'back.out(2)', immediateRender: true }, 1.2);
sfx('pop', 1.2);

// 7 张关卡卡片,镜头沿路往上
const T = [3.0, 5.6, 8.2, 10.4, 12.8, 15.4, 18.0, 21.0];
const P = [[510, 1560], [513, 1350], [502, 1134], [540, 945], [621, 756], [540, 594], [621, 405]];
for (let i = 1; i <= 7; i++) {
  const t0 = T[i - 1], t1 = T[i], id = `#v${i}`;
  camTo(t0 - 0.1, P[i - 1][0], P[i - 1][1]);
  tl.set(id, { autoAlpha: 1 }, t0);
  tl.fromTo(id, { y: -500, scale: 0.9 }, { y: 0, scale: 1, duration: 0.32, ease: 'back.out(1.6)', immediateRender: false }, t0);
  sfx('whoosh', t0);
  tl.fromTo(`${id} .hp i.on:last-child, ${id} .hp i.on`, { opacity: 0.4 }, { opacity: 1, duration: 0.2, immediateRender: false }, t0 + 0.3);
  tl.set(`#c${i}`, { autoAlpha: 1 }, t1 - 0.75);
  tl.fromTo(`#c${i}`, { scale: 2.6 }, { scale: 1, duration: 0.2, ease: 'power4.in', immediateRender: false }, t1 - 0.75);
  sfx('stamp', t1 - 0.56);
  burst(t1 - 0.55, 540, 1100, 40, '#ffcc00'); sfx('ding', t1 - 0.5);
  tl.to(id, { y: -700, duration: 0.25, ease: 'power3.in' }, t1 - 0.25);
  tl.set(id, { autoAlpha: 0 }, t1);
}

// S9 21–27 城堡 + 宝箱 + 结尾
camTo(20.9, 540, 120, 0.9);
tl.to('#hero', { autoAlpha: 0, duration: 0.3 }, 21.0);
scene('#s9', 21, D, { cut: true, sfx: false, zoomTo: 1 });
tl.fromTo('#e1', { autoAlpha: 1, y: -600 }, { y: 0, duration: 0.4, ease: 'bounce.out', immediateRender: true }, 21.0);
sfx('boom', 21.35); shake(21.37, 16);
tl.fromTo('#chest', { autoAlpha: 0, scale: 0.3, y: 200 }, { autoAlpha: 1, scale: 1, y: 0, duration: 0.5, ease: 'back.out(2)', immediateRender: true }, 21.6);
sfx('pop', 21.6);
burst(22.1, 540, 820, 120, '#ffcc00'); sfx('sparkle', 22.1);
tl.to('#chest', { y: -16, duration: 0.3, yoyo: true, repeat: 7, ease: 'sine.inOut' }, 22.2);
tl.fromTo('#e2', { autoAlpha: 0, y: 200 }, { autoAlpha: 1, y: 0, duration: 0.35, ease: 'back.out(2)', immediateRender: true }, 22.6);
sfx('ding', 22.6);

finish(D);
