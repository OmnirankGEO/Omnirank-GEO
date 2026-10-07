/* OmniRank 讲解片(第二版):钩子 → GEO 是什么、从哪来 → 我们的四条规矩 → 开源
 * 每一帧只由时间 t 决定;配音版由 tools/dub.py 按旁白时间重新映射。 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const tl = gsap.timeline({ paused: true });
const E = 'power3.out';
window.SFX = [];
const sfx = (name, t) => window.SFX.push({ t: +t.toFixed(3), name });
const el = (h) => { const d = document.createElement('div'); d.innerHTML = h.trim(); return d.firstChild; };

function scene(id, t0, t1) {
  tl.set(id, { autoAlpha: 1 }, t0);
  tl.fromTo(id, { opacity: 0 }, { opacity: 1, duration: 0.35, immediateRender: false }, t0);
  tl.fromTo(id, { scale: 1 }, { scale: 1.035, duration: t1 - t0, ease: 'none', immediateRender: false }, t0);  // 缓推镜头
  tl.to(id, { opacity: 0, duration: 0.35 }, t1 - 0.35);
  tl.set(id, { autoAlpha: 0 }, t1);
  sfx('whoosh', t0);
}
function pop(sel, t, s = 'pop', from = { y: 40, scale: 0.92 }) {
  tl.fromTo(sel, { autoAlpha: 0, ...from }, { autoAlpha: 1, y: 0, x: 0, scale: 1, duration: 0.5, ease: 'back.out(1.6)', immediateRender: true }, t);
  if (s) sfx(s, t);
}
function slideIn(sel, t, dx = -120, s = 'whoosh') {
  tl.fromTo(sel, { autoAlpha: 0, x: dx }, { autoAlpha: 1, x: 0, duration: 0.6, ease: E, immediateRender: true }, t);
  if (s) sfx(s, t);
}
function out(sel, t, d = 0.3) { tl.to(sel, { autoAlpha: 0, duration: d }, t); }
function slam(sel, t) {
  tl.fromTo(sel, { autoAlpha: 0, scale: 1.8 }, { autoAlpha: 1, scale: 1, duration: 0.35, ease: 'power4.in', immediateRender: true }, t);
  tl.fromTo('#flash', { opacity: 0.12 }, { opacity: 0, duration: 0.4, immediateRender: false }, t + 0.35);
  sfx('stamp', t + 0.33);
}
function typeText(sel, text, t, dur) {
  const o = { n: 0 }, e = $(sel);
  tl.to(o, { n: text.length, duration: dur, ease: 'none', onUpdate: () => { e.textContent = text.slice(0, Math.round(o.n)); } }, t);
  for (let i = 0; i < text.length; i += 2) sfx('tick', t + i / text.length * dur);
}
function kenBurns(sel, t, dur) {
  tl.fromTo(sel, { autoAlpha: 0, y: 60, scale: 0.96 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.7, ease: E, immediateRender: true }, t);
  tl.to(`${sel} img`, { scale: 1.12, y: -40, duration: dur, ease: 'none' }, t);
  sfx('whoosh', t);
}
function count(sel, from, to, t, dur, fmt = (v) => Math.round(v)) {
  const o = { v: from };
  tl.to(o, { v: to, duration: dur, ease: 'power2.out', onUpdate: () => { $(sel).textContent = fmt(o.v); } }, t);
  sfx('rise', t);
}

// ================= ACT 1 钩子 0–16 =================
scene('#s1', 0, 16);
pop('#s1q', 0.2, 'pop');
out('#s1q', 2.5);
pop('#s1search', 2.8, 'whoosh', { y: -40, scale: 1 });
typeText('#s1typed', '深圳商务用车哪家好', 3.0, 1.2);
const links = ['深圳商务用车租赁_价格_口碑排行', '深圳豪车配司机 - 专业商务接待', '2026 深圳商务车租赁公司推荐', '埃尔法租车多少钱一天?', '深圳企业用车长租方案', '商务用车哪家好?知乎热议', '深圳租车平台对比 | 测评', '高端商务出行服务 - 官网', '会议接待用车怎么选', '深圳包车一日游'];
links.forEach((t, i) => $('#s1links').appendChild(el(`<div class="link" id="lk${i}"><div class="u">www.example${i + 1}.com</div><div class="t">${t}</div></div>`)));
links.forEach((_, i) => tl.fromTo(`#lk${i}`, { autoAlpha: 0, x: -30 }, { autoAlpha: 1, x: 0, duration: 0.25, immediateRender: true }, 4.3 + i * 0.07));
sfx('rise', 4.3);
pop('#s1ten', 5.0, 'pop', { x: 60, scale: 1 });
links.forEach((_, i) => tl.to(`#lk${i}`, { autoAlpha: 0, y: -20 * (i + 1), scaleY: 0.2, duration: 0.3, ease: 'power2.in' }, 6.2 + (9 - i) * 0.03));
out(['#s1search', '#s1ten'], 6.3);
sfx('whoosh', 6.3);
pop('#s1ai', 6.8, 'ding');
['#s1n1', '#s1n2', '#s1n3'].forEach((s, i) => pop(s, 7.3 + i * 0.3, 'pop', { y: 20, scale: 0.8 }));
pop('#s1three', 8.4, 'pop');
out(['#s1ai', '#s1three'], 9.8);
pop('#s1you', 10.1, 'pop');
slam('#s1stamp', 11.0);
tl.to('#s1you', { filter: 'grayscale(1)', opacity: 0.5, duration: 0.4 }, 11.4);
pop('#s1zero', 12.0, 'pop');

// ================= ACT 2 GEO 16–40 =================
scene('#s2', 16, 40);
slam('#s2seo', 16.4);
pop('#s2seot', 17.1, 'pop');
tl.fromTo('#s2strike', { scaleX: 0, autoAlpha: 1 }, { scaleX: 1, duration: 0.35, ease: 'power2.in', immediateRender: true }, 19.0);
sfx('stamp', 19.3);
out(['#s2seo', '#s2seot', '#s2strike'], 19.6);
slam('#s2geo', 19.8);
pop('#s2geot', 20.6, 'pop');
out(['#s2geo', '#s2geot'], 22.8);
tl.fromTo('#s2paper', { autoAlpha: 0, y: 120, rotationX: 30 }, { autoAlpha: 1, y: 0, rotationX: 0, duration: 0.8, ease: E, immediateRender: true }, 23.1);
sfx('whoosh', 23.1);
out('#s2paper', 28.4);
['#c1', '#c2', '#c3'].forEach((s, i) => slideIn(s, 28.7 + i * 0.45, -160, 'pop'));
pop('#s2bar', 29.6, null);
tl.fromTo('#b1', { scaleY: 0.714 }, { scaleY: 1, duration: 1.2, ease: E, immediateRender: true }, 30.6);
sfx('rise', 30.6);
pop('#b1n', 31.8, 'ding');
out(['#s2chips', '#s2bar'], 34.8);
tl.fromTo('#s2sum > div:first-child', { autoAlpha: 0, y: 40 }, { autoAlpha: 1, y: 0, duration: 0.5, immediateRender: true }, 35.1);
tl.fromTo('#s2sum > div:last-child', { autoAlpha: 0, y: 40 }, { autoAlpha: 1, y: 0, duration: 0.5, immediateRender: true }, 36.4);
tl.set('#s2sum', { autoAlpha: 1 }, 35.0);
gsap.set('#s2sum', { autoAlpha: 0 });
sfx('pop', 35.1); sfx('ding', 36.4);

// ================= ACT 3 四条规矩 40–90 =================
scene('#s3', 40, 90);
pop('#s3h > div:first-child', 40.4, 'whoosh', { y: 0, scale: 1.3 });
pop('#s3h > div:last-child', 41.5, 'ding');
out('#s3h', 44.7);
// 规矩 1
slideIn('#r1', 45.0);
const AIs = ['豪车配司机哪家好', '租埃尔法配司机', '企业长租怎么选', '品牌靠谱吗'];
const res = [['n', 'n', 'm', 'n'], ['n', 'n', 'n', 'n'], ['n', 'm', 'n', 'n'], ['y', 'y', 'm', 'y']];
const lab = { y: ['推荐', '#123d27', '#2fd47a'], m: ['提到', '#3a2e12', '#f5b041'], n: ['未提到', '#3a1717', '#ef5b5b'] };
AIs.forEach((q, i) => {
  const row = el(`<div style="display:flex;gap:12px;align-items:center;margin-bottom:12px"><div style="width:278px;font-size:26px">“${q}”</div></div>`);
  res[i].forEach((r, j) => row.appendChild(el(`<div class="cell" id="g${i}${j}"><span style="opacity:0">${lab[r][0]}</span></div>`)));
  $('#r1rows').appendChild(row);
});
pop('#r1grid', 45.6, null, { x: 80, scale: 1 });
AIs.forEach((_, i) => res[i].forEach((r, j) => {
  const t = 46.2 + i * 0.5 + j * 0.12;
  tl.to(`#g${i}${j}`, { background: lab[r][1], borderColor: lab[r][2], color: lab[r][2], duration: 0.2 }, t);
  tl.to(`#g${i}${j} span`, { opacity: 1, duration: 0.2 }, t);
  if (j === 0) sfx('tick', t);
}));
count('#r1num', 0, 26, 48.6, 1.4);
out('#r1grid', 51.0);
kenBurns('#r1shot', 51.2, 3.3);
out(['#r1', '#r1shot'], 54.3);
// 规矩 2
slideIn('#r2', 54.6);
pop('#r2txt', 55.3, null, { x: 80, scale: 1 });
pop('#r2why', 57.0, 'pop', { y: 10, scale: 1 });
tl.fromTo('#r2hl', { backgroundColor: 'rgba(239,91,91,0)' }, { backgroundColor: 'rgba(239,91,91,.4)', duration: 0.3, immediateRender: false }, 56.8);
tl.to('#r2bad', { opacity: 0.35, textDecoration: 'line-through', duration: 0.2 }, 58.0);
sfx('stamp', 58.0);
pop('#r2good', 58.6, 'ding', { y: 20, scale: 1 });
out(['#r2', '#r2txt'], 62.2);
// 规矩 3
slideIn('#r3', 62.5);
pop('#r3chart', 63.1, null, { x: 80, scale: 1 });
const pts = [11, 13, 12, 18, 23, 22, 30, 36, 34, 42, 48, 52, 57, 55, 61, 63];
const X = (i) => 10 + i * 770 / (pts.length - 1), Y = (v) => 310 - v * 4;
$('#r3line').setAttribute('d', pts.map((v, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' '));
const L = 1100; $('#r3line').style.strokeDasharray = L; $('#r3line').style.strokeDashoffset = L;
const pr = { p: 0 };
tl.to(pr, { p: 1, duration: 3.6, ease: 'power1.inOut', onUpdate: () => {
  $('#r3line').style.strokeDashoffset = L * (1 - pr.p);
  const f = pr.p * (pts.length - 1), i = Math.min(Math.floor(f), pts.length - 2), r = f - i, v = pts[i] + (pts[i + 1] - pts[i]) * r;
  $('#r3dot').setAttribute('cx', X(i) + (X(i + 1) - X(i)) * r); $('#r3dot').setAttribute('cy', Y(v)); $('#r3pct').textContent = Math.round(v) + '%';
} }, 63.4);
sfx('rise', 63.4);
pop('#r3cite', 67.3, 'ding', { y: 30, scale: 1 });
out(['#r3', '#r3chart', '#r3cite'], 69.7);
// 规矩 4
slideIn('#r4', 70.0);
for (let i = 0; i < 10; i++) {
  const a = i / 10 * Math.PI * 2;
  $('#r4ring').appendChild(el(`<div class="a card c" style="left:${230 + 230 * Math.cos(a)}px;top:${255 + 230 * Math.sin(a)}px;width:100px;height:50px;font-size:20px;border-color:${i % 3 ? '#2fd47a' : '#3a4250'}">${i % 3 ? '被引用' : '没用上'}</div>`));
}
pop('#r4ring', 70.6, 'whoosh', { y: 0, scale: 0.6 });
pop('#r4core', 71.0, 'ding');
tl.to('#r4ring', { rotation: 200, duration: 8, ease: 'none' }, 70.6);
slam('#r4stamp', 75.6);
out(['#r4', '#r4ring', '#r4core', '#r4stamp'], 78.7);
// 蒙太奇
['诊断', '报价', '创作', '发布', '监测', '飞轮'].forEach((n, i) => $('#s3flow').appendChild(el(`<div class="chip" id="fl${i}">${n}</div>`)));
['#fl0', '#fl1', '#fl2', '#fl3', '#fl4', '#fl5'].forEach((s, i) => { pop(s, 79.0 + i * 0.22, 'tick', { y: -30, scale: 1 }); tl.to(s, { borderColor: '#2fd47a', color: '#2fd47a', duration: 0.2 }, 79.9 + i * 0.25); });
kenBurns('#mq', 80.4, 8);
kenBurns('#mp', 81.0, 8);
pop('#s3tags', 84.2, 'ding');

// ================= ACT 4 开源 90–100 =================
scene('#s4', 90, 100.5);
slam('#e1', 90.3);
pop('#e2', 91.2, 'ding');
pop('#e3', 92.0, 'pop');
pop('#e4', 92.7, 'pop');

tl.fromTo('#progress', { width: 0 }, { width: 1920, duration: 100.5, ease: 'none' }, 0);
window.DURATION = 100.5;
window.seekTo = (t) => { tl.seek(t, false); };
window.setSub = (text) => { const e = $('#capt'); if (e.textContent !== (text || '')) e.textContent = text || ''; e.style.opacity = text ? 1 : 0; };
window.ready = document.fonts.ready.then(() => true);
