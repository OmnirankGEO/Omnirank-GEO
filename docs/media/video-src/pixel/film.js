/* 王老板的 AI 上榜之旅 · 像素 RPG 风讲解片
 * 每一帧只由时间 t 决定(GSAP 时间轴 seek)。120 BPM,关卡切换卡在 2 秒小节线上。 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const tl = gsap.timeline({ paused: true });
const P = 6; // 一个逻辑像素
const snap = (v) => Math.round(parseFloat(v) / P) * P + 'px';
const SNAP = { modifiers: { x: snap, y: snap } };
window.NARR = []; window.SFX = [];
let seed = 7; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);

// ---------- helpers ----------
const put = (sel, html) => { $(sel).innerHTML = html; };
const sfx = (name, t) => window.SFX.push({ t: +t.toFixed(3), name });
function nar(text, t0, t1) {
  window.NARR.push({ t0, t1, text });
  tl.call(() => { if (window.SUBS_OVERRIDE) return; $('#subsT').textContent = text; $('#subs').style.opacity = 1; }, null, t0);
  tl.call(() => { if (window.SUBS_OVERRIDE) return; $('#subs').style.opacity = 0; }, null, t1);
}
const hide = (sel) => gsap.set(sel, { autoAlpha: 0 });
const show = (sel, t) => tl.set(sel, { autoAlpha: 1 }, t);
const gone = (sel, t) => tl.set(sel, { autoAlpha: 0 }, t);
function pop(sel, t, s = 'pop') {   // 像素感:三格放大
  tl.fromTo(sel, { autoAlpha: 0, scale: 0.3 }, { autoAlpha: 1, scale: 1, duration: 0.2, ease: 'steps(3)', immediateRender: false }, t);
  if (s) sfx(s, t);
}
function slam(sel, t) {
  tl.fromTo(sel, { autoAlpha: 0, scale: 2.2 }, { autoAlpha: 1, scale: 1, duration: 0.2, ease: 'steps(4)', immediateRender: false }, t);
  tl.fromTo('#stage', { x: 0 }, { keyframes: [{ x: -18, y: 12 }, { x: 18, y: -6 }, { x: -6, y: 6 }, { x: 0, y: 0 }], duration: 0.24, ease: 'steps(4)', immediateRender: false }, t + 0.18);
  sfx('slam', t + 0.18);
}
function move(sel, vars, t, dur, ease = 'none') { tl.to(sel, { ...vars, duration: dur, ease, ...SNAP }, t); }
function walk(sel, t, dur) {  // 走路:上下颠 6px
  tl.to(sel, { y: '-=12', duration: 0.13, repeat: Math.max(1, Math.floor(dur / 0.13) - 1), yoyo: true, ease: 'steps(1)' }, t);
}
function hop(sel, t, n = 2, hgt = 48) {
  for (let i = 0; i < n; i++) tl.to(sel, { keyframes: [{ y: -hgt, duration: 0.15 }, { y: 0, duration: 0.15 }], ease: 'steps(3)' }, t + i * 0.32);
  sfx('jump', t);
}
function typeText(sel, text, t, dur, blip = true) {
  const o = { n: 0 }; const el = $(sel);
  tl.to(o, { n: text.length, duration: dur, ease: 'none', onUpdate: () => { el.textContent = text.slice(0, Math.floor(o.n)); } }, t);
  tl.call(() => { el.textContent = ''; }, null, Math.max(0, t - 0.001));
  if (blip) for (let i = 0; i < text.length; i += 3) sfx('blip', t + (i / text.length) * dur);
}
function throwArc(sel, t, dx, dy, peak, dur) {
  tl.to(sel, { x: `+=${dx}`, duration: dur, ease: 'none', ...SNAP }, t);
  tl.to(sel, { keyframes: [{ y: -peak, duration: dur * 0.45, ease: 'power2.out' }, { y: dy, duration: dur * 0.55, ease: 'power2.in' }], ...SNAP }, t);
  sfx('throw', t);
}
function banner(text, feat, t) {
  tl.call(() => { $('#banner').innerHTML = `<b>${text}</b>${feat ? `<i>${feat}</i>` : ''}`; }, null, t);
  tl.fromTo('#banner', { y: -120 }, { y: 0, duration: 0.3, ease: 'steps(5)', immediateRender: false }, t);
  sfx('stage', t);
}
// 像素转场:16×9 块随机盖上再掀开,中点切场景
const wipeCells = [];
for (let i = 0; i < 16 * 9; i++) { const c = document.createElement('i'); c.style.left = (i % 16) * 120 + 'px'; c.style.top = Math.floor(i / 16) * 120 + 'px'; $('#wipe').appendChild(c); wipeCells.push(c); }
function cut(from, to, t) {
  const order = wipeCells.slice().sort(() => rnd() - 0.5);
  tl.to(order, { scale: 1, duration: 0.05, stagger: 0.32 / order.length, ease: 'steps(1)' }, t - 0.4);
  if (from) tl.set(from, { autoAlpha: 0 }, t);
  if (to) tl.set(to, { autoAlpha: 1 }, t);
  tl.to(order, { scale: 0, duration: 0.05, stagger: 0.32 / order.length, ease: 'steps(1)' }, t + 0.05);
  sfx('wipe', t - 0.4);
}
function stars(sel, n) {
  let h = '';
  for (let i = 0; i < n; i++) h += `<i class="a st" style="left:${Math.floor(rnd() * 320) * 6}px;top:${Math.floor(rnd() * 180) * 6}px;width:6px;height:6px;background:${rnd() > .7 ? '#ffcd75' : '#94b0c2'}"></i>`;
  put(sel, h);
}

// ---------- 布置精灵 ----------
stars('#stars', 70); stars('#stars2', 70);
put('#cl1', sprite('cloud', 12)); put('#cl2', sprite('cloud', 9));
put('#orbS', sprite('orb', 9)); put('#bossS', sprite('boss', 9)); put('#sweatS', sprite('sweat', 6));
put('#s1boss', sprite('boss', 9)); put('#s1bag', sprite('bag', 6)); put('#s1bot', sprite('bot', 9));
put('#s2m', sprite('merchant', 12)); put('#s2orb', sprite('orb', 9));
put('#s3boss', sprite('boss', 9)); put('#s3scroll', sprite('scroll', 6)); put('#s3orb', sprite('orb', 9));
put('#s3shield', sprite('shield', 12)); put('#s3ham', sprite('hammer', 18)); put('#s3gold', sprite('scrollgold', 12));
put('#s4boss', sprite('boss', 9)); put('#s4sc0', sprite('scroll', 6)); put('#s4weed', sprite('weed', 9)); put('#s4orb', sprite('orb', 6));
put('#s5boss', sprite('boss', 9)); put('#s5bot', sprite('bot', 9)); put('#s5orb', sprite('orb', 6));
put('#s7agent', sprite('agent', 9));
// 人群
const crowdCols = ['cust_r', 'cust_c', 'cust_o', 'cust_g', 'cust_v', 'cust_y'];
put('#crowd', crowdCols.map((c, i) => `<div class="a cw" id="cw${i}" style="left:-120px;bottom:180px">${sprite(c, 9)}</div>`).join(''));
// 金币
put('#coins', Array.from({ length: 16 }, (_, i) => `<div class="a cn" id="cn${i}" style="left:936px;top:520px">${sprite('coin', 6)}</div>`).join(''));
// 第 1 关:四家 AI 卡
const AIs = [['通义', '✘', 'r'], ['DeepSeek', '✘', 'r'], ['豆包', '提到', 'y'], ['元宝', '✘', 'r']];
put('#s1ai', AIs.map(([n, r, c], i) => `<div class="box" id="s1a${i}" style="width:262px;text-align:center"><div class="t36">${n}</div><div class="t24 l" style="margin:6px 0">问「深圳哪家好」</div><div class="t60 ${c}" id="s1ar${i}">${r}</div></div>`).join(''));
// 第 2 关:供料卷轴 + 堆叠
put('#s2feed', Array.from({ length: 8 }, (_, i) => `<div class="a fd" id="fd${i}" style="left:${900 + (i % 4) * 90}px;top:${700 + Math.floor(i / 4) * 72}px">${sprite('scroll', 6)}</div>`).join(''));
const stack = (n) => Array.from({ length: n }, () => `<div style="height:30px;margin-top:6px;background:#f4f4f4;box-shadow:0 0 0 6px #1a1c2c"></div>`).join('');
put('#s2st1', stack(10)); put('#s2st2', ''); put('#s2st3', stack(4));
// 第 4 关:媒体塔
const towers = [['门户网站', 840, 1, 390], ['某博客', 1020, 0, 270], ['行业媒体', 1200, 1, 450], ['个人站', 1380, 0, 240], ['问答平台', 1560, 1, 420], ['某贴吧', 1740, 0, 300]];
put('#s4towers', towers.map(([n, x, hot, h], i) => `<div class="a tw" id="tw${i}" style="left:${x - 75}px;bottom:180px;width:150px;height:${h}px;background:${hot ? '#41a6f6' : '#566c86'};box-shadow:0 0 0 6px #1a1c2c"><div class="t24" style="text-align:center;margin-top:12px;white-space:nowrap">${n}</div><div class="a t36 y twk" id="twk${i}" style="left:-30px;right:-30px;top:-60px;text-align:center;white-space:nowrap">${hot ? 'AI 常去' : ''}</div></div>`).join('')
  + towers.map(([, x], i) => `<div class="a ts" id="ts${i}" style="left:180px;top:600px">${sprite('scroll', 6)}</div>`).join(''));
// 第 5 关:日历 + 柱子
const days = ['一', '二', '三', '四', '五', '六', '日'];
put('#s5cal', days.map((d, i) => `<div class="box" id="d${i}" style="width:120px;text-align:center;padding:6px"><div class="t24 l">周${d}</div><div class="t36" id="dk${i}">·</div></div>`).join(''));
const vals = [11, 15, 14, 22, 27, 31, 36, 42, 47, 51, 55, 60, 63];
put('#s5bars', vals.map((v, i) => `<div id="b${i}" style="width:60px;height:${Math.round(v / 75 * 300 / 6) * 6}px;background:${v >= 63 ? '#38b764' : '#41a6f6'};box-shadow:0 0 0 6px #1a1c2c;transform-origin:bottom"></div>`).join(''));
// 第 6 关:卡片雨 + 环
put('#s6rain', Array.from({ length: 18 }, (_, i) => `<div class="a box rc" id="rc${i}" style="left:${120 + (i % 9) * 190}px;top:-200px;padding:6px 12px"><span class="t24">AI 回答</span> <span class="t24 ${i % 3 ? 'g' : 'r'}">${i % 3 ? '✔引用' : '✘'}</span></div>`).join(''));
put('#s6ring', Array.from({ length: 8 }, (_, i) => { const a = i / 8 * Math.PI * 2; return `<div class="a" style="left:${Math.round((270 + 240 * Math.cos(a)) / 6) * 6}px;top:${Math.round((270 + 240 * Math.sin(a)) / 6) * 6}px">${sprite('scrollgold', 6)}</div>`; }).join(''));
put('#s6ups', [['写作模板', '+1'], ['选题', '+1'], ['选媒体', '+1']].map(([a, b], i) => `<div class="box g" id="up${i}" style="text-align:center"><div class="t48">${a}</div><div class="t72 y">${b}</div></div>`).join(''));
// 第 7 关
const bossCols = ['b', 'r', 'g', 'o', 'v', 'c'];
put('#s7bosses', bossCols.map((c, i) => `<div class="a sb" id="sb${i}" style="left:${120 + i * 150}px;bottom:180px">${sprite(['cust_c', 'cust_r', 'cust_g', 'cust_o', 'cust_v', 'cust_y'][i], 9)}<div class="a t36 y" style="left:18px;top:-48px">?</div></div>`).join(''));
put('#s7tools', [['crate', '批发进货'], ['tag', '自己定价'], ['star', '换上招牌']].map(([s, n], i) => `<div class="box y" id="tool${i}" style="text-align:center;width:300px"><div style="display:flex;justify-content:center;height:96px;align-items:center">${sprite(s, 9)}</div><div class="t48" style="margin-top:12px">${n}</div></div>`).join(''));
const rows = [['客户付款', '+¥3,000', 'g'], ['品牌体检', '−650 算力', 'l'], ['发布失败 · 原路退回', '+390 算力', 'g'], ['服务商利润', '+¥1,200', 'y']];
put('#s7rows', rows.map(([a, b, c], i) => `<div id="row${i}" style="display:flex;justify-content:space-between;border-bottom:6px dotted #566c86;padding:12px 6px"><span class="t48">${a}</span><span class="t48 ${c}">${b}</span></div>`).join(''));

// 初始隐藏(由时间轴逐个打开)
hide(['#h1', '#h2', '#h3', '#h4', '#hchat', '#hq', '#hbrand', '#hbrand2', '#hstory', '#htitle', '#hstart', '.cn', '#orbS', '#orbSay', '#sweatS', '#bossSay', '#custSay',
  '#s1think', '#s1bag', '#s1miss', '#s1money', '#s1dlg', '#s1scan', '#s1res', '#s1tv', '#s1r1', '#s1r2',
  '#s2say', '#s2stamp', '#s2orb', '#s2orbL', '.fd', '#s2cmp', '#s2shop', '#s2tv', '#s2n2', '#s2n3',
  '#s3say', '#s3scroll', '#s3block', '#s3zero', '#s3q', '#s3orbSay', '#s3forge', '#s3shield', '#s3good', '#s3gold', '#s3txt',
  '#s4sc0', '.ts', '.twk', '#s4wallet', '#s5say', '#s5news', '#s5tv', '#s5chart', '#s5cal',
  '#s6lv', '#s6ups', '#s6note', '#s6ring', '#s6core', '#s6xp', '#s7tools', '#s7sign', '#s7book', '#s7agent', '#s7badge',
  '#e0', '#e1', '#e2', '#e3', '#e4', '#e5', '#e6', '#hud', '[id^=s1ar]']);
gsap.set('#subs', { opacity: 0 });
gsap.set('#s1bot', { y: 0 });

// ===================== HOOK 0–12 =====================
show('#hook', 0);
pop('#h1', 0.05, 'blip');
slam('#h2', 0.3);
slam('#h3', 0.95);
for (let i = 0; i < 16; i++) {
  const a = rnd() * Math.PI * 2, d = 300 + rnd() * 500;
  tl.set(`#cn${i}`, { autoAlpha: 1, x: 0, y: 0 }, 1.15);
  tl.to(`#cn${i}`, { x: Math.cos(a) * d, keyframes: [{ y: -Math.abs(Math.sin(a)) * d * 0.7, duration: 0.45, ease: 'power2.out' }, { y: 700, duration: 0.9, ease: 'power2.in' }], duration: 1.35, ...SNAP }, 1.15);
  if (i % 4 === 0) sfx('coin', 1.15 + i * 0.03);
}
pop('#h4', 1.9, 'blip');
nar('我们把一整套 GEO 系统，开源了。', 0.3, 3.0);
gone(['#h1', '#h2', '#h3', '#h4'], 3.2);
pop('#hchat', 3.3, 'select');
typeText('#hans', '星驰、恒远、云途……', 3.9, 1.2);
pop('#hq', 5.6, 'powerup');
nar('它只做一件事：让 AI 在回答问题时，主动推荐你。', 3.3, 7.6);
gone(['#hchat', '#hq'], 8.0);
slam('#hbrand', 8.1);
pop('#hbrand2', 8.8, 'blip');
pop('#hstory', 9.8, 'blip');
pop('#htitle', 10.1, 'stage');
tl.fromTo('#hstart', { autoAlpha: 1 }, { autoAlpha: 0, duration: 0.25, repeat: 3, yoyo: true, ease: 'steps(1)', immediateRender: false }, 10.6);
sfx('select', 10.9);
nar('不信？先看看王老板的遭遇。', 8.0, 11.4);
cut('#hook', '#street', 12);

// ===================== 第 0 关 街道 12–30 =====================
show('#hud', 12);
banner('第 0 关 · 王老板的烦恼', '', 12.1);
hop('#bossS', 12.6, 2, 30);
nar('王老板在深圳做商务用车，车好，司机也靠谱。', 12.3, 16.0);
[0, 1].forEach((i) => { move(`#cw${i}`, { x: 330 - i * 60 }, 12.4 + i * 0.6, 2.2); walk(`#cw${i}`, 12.4 + i * 0.6, 2.2); gone(`#cw${i}`, 14.8 + i * 0.6); sfx('door', 14.8 + i * 0.6); });
nar('可最近，新客户越来越少。', 16.3, 19.6);
const cnt = { v: 12 };
tl.to(cnt, { v: 3, duration: 2.4, ease: 'steps(9)', onUpdate: () => { $('#custN').textContent = Math.round(cnt.v); } }, 16.6);
for (let i = 0; i < 9; i++) sfx('down', 16.6 + i * 0.27);
tl.set('#custN', { color: '#e43b44' }, 17.0);
// 问 AI
tl.set('#cw2', { x: 0, autoAlpha: 1 }, 19.9);
move('#cw2', { x: 840 }, 19.9, 1.6); walk('#cw2', 19.9, 1.6);
tl.set('#custSay', { autoAlpha: 1, x: 0 }, 21.4); tl.set('#custSay', { left: 690 }, 21.4);
typeText('#custSay', 'AI，深圳租埃尔法哪家好？', 21.4, 1.0);
pop('#orbS', 21.0, 'orb');
tl.to('#orbS', { y: -24, duration: 0.5, repeat: 15, yoyo: true, ease: 'steps(2)' }, 21.2);
tl.set('#orbSay', { autoAlpha: 1 }, 22.6);
typeText('#orbSay', '推荐：星驰、恒远、云途', 22.6, 1.2);
nar('因为现在的客户不再搜索，而是直接问 AI。', 19.9, 24.6);
gone('#custSay', 24.8);
[2, 3, 4].forEach((i, k) => {
  if (i !== 2) tl.set(`#cw${i}`, { x: 0, autoAlpha: 1 }, 24.9 + k * 0.3);
  const tx = [1170, 1500, 1820][k];
  move(`#cw${i}`, { x: tx }, 24.9 + k * 0.3, 3); walk(`#cw${i}`, 24.9 + k * 0.3, 3);
});
tl.set('#bossSay', { autoAlpha: 1 }, 26.2); typeText('#bossSay', '？？？我呢？', 26.2, 0.6);
pop('#sweatS', 26.6, 'error');
tl.to('#sweatS', { y: 24, duration: 0.4, repeat: 4, yoyo: true, ease: 'steps(2)' }, 26.7);
nar('而 AI 只报几个名字——没有他。', 24.9, 29.4);
cut('#street', '#s1', 30);

// ===================== 第 1 关 体检 30–58 =====================
banner('第 1 关 · 为什么要先体检？', 'OmniRank · 诊断报告', 30.1);
pop('#s1think', 30.4, 'blip');
nar('王老板第一反应：砸钱投广告。', 30.3, 33.8);
tl.set('#s1bag', { autoAlpha: 1 }, 31.3);
throwArc('#s1bag', 31.4, 1080, 120, 360, 1.5);
gone('#s1bag', 32.95);
pop('#s1miss', 32.95, 'miss');
pop('#s1money', 33.2, 'down');
gone('#s1think', 33.8);
nar('可病没查清就乱开药，钱只会打水漂。', 34.1, 38.6);
tl.to('#s1money', { y: 30, duration: 0.3, repeat: 7, yoyo: true, ease: 'steps(2)' }, 34.2);
gone(['#s1miss'], 36.0);
// 小榜登场
tl.to('#s1bot', { top: 816, duration: 0.5, ease: 'bounce.out', ...SNAP }, 38.9); sfx('land', 39.3);
tl.set('#s1dlg', { autoAlpha: 1 }, 39.5);
typeText('#s1dlgT', '先别砸钱！先给品牌做个 AI 体检，看看 AI 怎么说你。', 39.6, 2.4);
nar('所以第一步，是给品牌做一次 AI 体检。', 38.9, 43.6);
gone(['#s1dlg', '#s1money'], 43.8);
show('#s1scan', 43.9);
AIs.forEach((a, i) => { pop(`#s1a${i}`, 44.0 + i * 0.25, 'blip'); });
AIs.forEach((a, i) => { tl.call(() => { }, null, 0); pop(`#s1ar${i}`, 45.4 + i * 0.4, a[1] === '✘' ? 'error' : 'coin'); });
nar('把客户真会问的问题，拿去挨个问各家 AI。', 43.9, 48.4);
const hp = { v: 0 };
tl.to(hp, { v: 26, duration: 1.2, ease: 'steps(13)', onUpdate: () => { $('#s1hpbar').style.width = (hp.v) + '%'; $('#s1hpn').textContent = Math.round(hp.v) + '/100'; } }, 47.2);
sfx('hurt', 48.4);
tl.fromTo('#s1hp', { autoAlpha: 1 }, { autoAlpha: 0.2, duration: 0.15, repeat: 5, yoyo: true, ease: 'steps(1)', immediateRender: false }, 48.4);
gone('#s1scan', 49.6);
show('#s1res', 49.6);
nar('结果一目了然：老客户搜名字，找得到；', 48.7, 52.6);
pop('#s1r1', 49.8, 'coin');
nar('新客户问「哪家好」，全被同行截走了。', 52.9, 57.4);
pop('#s1r2', 53.0, 'error');
pop('#s1tv', 54.4, 'select');
cut('#s1', '#s2', 58);

// ===================== 第 2 关 报价 58–80 =====================
banner('第 2 关 · 价格凭什么？', 'OmniRank · 报价', 58.1);
tl.fromTo('#s2m', { x: -400 }, { x: 0, duration: 1, ease: 'none', ...SNAP, immediateRender: false }, 58.3); walk('#s2m', 58.3, 1);
tl.set('#s2say', { autoAlpha: 1 }, 59.6); typeText('#s2say', '包月一万，保证第一！', 59.6, 1.0);
nar('那要花多少钱？以前常听到：包月一万，保证第一。', 58.3, 62.8);
slam('#s2stamp', 61.6);
nar('可 AI 推荐谁，只看它读到了什么。', 63.1, 67.0);
gone(['#s2say', '#s2stamp', '#s2m'], 63.0);
pop('#s2orb', 63.2, 'orb'); pop('#s2orbL', 63.4, null);
$$('.fd').forEach((el, i) => {
  tl.set(el, { autoAlpha: 1, x: -760, y: -300 + i * 30 }, 63.6 + i * 0.25);
  move(el, { x: 520 - (i % 4) * 90, y: -500 - Math.floor(i / 4) * 72 + 30 }, 63.6 + i * 0.25, 0.9);
  tl.set(el, { autoAlpha: 0 }, 64.5 + i * 0.25);
  sfx('blip', 64.5 + i * 0.25);
});
gone(['#s2orb', '#s2orbL'], 67.1);
show('#s2cmp', 67.3);
tl.fromTo('#s2st1', { scaleY: 0 }, { scaleY: 1, transformOrigin: 'bottom', duration: 0.8, ease: 'steps(10)', immediateRender: false }, 67.4);
sfx('stack', 67.4);
pop('#s2n2', 68.6, 'error');
tl.fromTo('#s2st3', { scaleY: 0 }, { scaleY: 1, transformOrigin: 'bottom', duration: 0.6, ease: 'steps(4)', immediateRender: false }, 70.0);
pop('#s2n3', 70.6, 'powerup');
nar('同行在这个问题下写了 40 篇，你就得写够数，才挤得进去。', 67.3, 72.6);
gone('#s2cmp', 72.8);
pop('#s2shop', 72.9, 'select');
['#s2i1', '#s2i2', '#s2i3', '#s2i2'].forEach((s, i) => {
  tl.set(['#s2i1', '#s2i2', '#s2i3'], { background: 'transparent', color: '#f4f4f4' }, 73.6 + i * 0.5);
  tl.set(s, { background: '#ffcd75', color: '#1a1c2c' }, 73.6 + i * 0.5); sfx('blip', 73.6 + i * 0.5);
});
sfx('coin', 75.6);
pop('#s2tv', 75.8, null);
nar('所以报价按「要写多少篇 × 每篇成本」算出来，客户才敢付钱。', 72.9, 78.6);
cut('#s2', '#s3', 80);

// ===================== 第 3 关 创作 80–104 =====================
banner('第 3 关 · AI 凭什么信你？', 'OmniRank · 创作中心', 80.1);
pop('#s3shield', 80.2, null);
tl.set('#s3say', { autoAlpha: 1 }, 81.2); typeText('#s3say', '深圳第一！全网最低！', 81.2, 1.0);
nar('内容写好了。王老板张口就是：深圳第一、全网最低。', 80.3, 84.8);
tl.set('#s3scroll', { autoAlpha: 1 }, 83.6);
throwArc('#s3scroll', 83.7, 960, -60, 240, 1.0);
pop('#s3block', 84.7, 'hit');
throwArc('#s3scroll', 84.75, -660, 420, 120, 0.8);
gone(['#s3say', '#s3scroll'], 85.6);
nar('结果，AI 一个字都没引用。', 85.1, 88.4);
pop('#s3zero', 85.6, 'error');
gone('#s3block', 86.4);
nar('因为 AI 不信吹牛，只信有出处、查得到的话。', 88.7, 93.4);
tl.set('#s3orbSay', { autoAlpha: 1 }, 88.9); typeText('#s3orbSay', '出处呢？证据呢？', 88.9, 0.9);
pop('#s3q', 90.0, 'blip');
tl.to('#s3q', { y: -18, duration: 0.25, repeat: 9, yoyo: true, ease: 'steps(1)' }, 90.1);
gone(['#s3orbSay', '#s3q', '#s3zero', '#s3orb', '#s3shield', '#s3boss'], 93.6);
show('#s3forge', 93.7);
['#s3in1', '#s3in2', '#s3in3'].forEach((s, i) => {
  pop(s, 93.8 + i * 0.3, 'blip');
  move(s, { x: 600 - 0, y: 360 - i * 120, autoAlpha: 0 }, 95.0 + i * 0.25, 0.5);
});
gone(['#s3in1', '#s3in2', '#s3in3'], 96.0);
for (let i = 0; i < 3; i++) { tl.to('#s3ham', { rotation: -40, duration: 0.15, ease: 'steps(2)', transformOrigin: '90% 90%' }, 96.0 + i * 0.5); tl.to('#s3ham', { rotation: 0, duration: 0.1, ease: 'steps(1)' }, 96.25 + i * 0.5); sfx('hammer', 96.3 + i * 0.5); }
gone(['#s3ham', '#s3anvil'], 97.6);
nar('所以创作中心先找证据再动笔，写完还要核查、审稿，把吹过头的话改掉。', 93.7, 102.4);
pop('#s3txt', 97.7, 'select');
tl.set('#s3hl', { background: '#e43b44', color: '#f4f4f4' }, 98.8); sfx('error', 98.8);
tl.to('#s3bad', { opacity: 0.35, textDecoration: 'line-through', duration: 0.1, ease: 'steps(1)' }, 99.6); sfx('slash', 99.6);
pop('#s3good', 100.0, 'powerup');
pop('#s3gold', 100.8, 'coin');
tl.to('#s3gold', { y: -18, duration: 0.2, repeat: 7, yoyo: true, ease: 'steps(1)' }, 101.0);
cut('#s3', '#s4', 104);

// ===================== 第 4 关 发布 104–120 =====================
banner('第 4 关 · 发在哪？', 'OmniRank · 发布', 104.1);
tl.set('#s4sc0', { autoAlpha: 1 }, 104.6);
throwArc('#s4sc0', 104.7, 300, 180, 180, 0.8);
gone('#s4sc0', 105.5); sfx('plop', 105.5);
tl.fromTo('#s4weed', { x: 0, rotation: 0 }, { x: -1500, rotation: -720, duration: 3, ease: 'none', ...SNAP, immediateRender: false }, 105.6);
sfx('wind', 105.6);
nar('文章随手发在小论坛？AI 根本不去那儿。', 104.3, 108.6);
nar('发布系统会挑 AI 常去、真引用过的媒体去发。', 108.9, 113.8);
tl.to('#s4orb', { y: 24, duration: 0.4, repeat: 20, yoyo: true, ease: 'steps(2)' }, 104.2);
towers.forEach(([, , hot], i) => { if (hot) { pop(`#twk${i}`, 109.2 + i * 0.3, 'select'); tl.to(`#tw${i}`, { boxShadow: '0 0 0 6px #ffcd75', duration: 0.1, ease: 'steps(1)' }, 109.2 + i * 0.3); } });
towers.forEach(([, x, hot], i) => {
  if (!hot) return;
  tl.set(`#ts${i}`, { autoAlpha: 1, x: 0, y: 0 }, 111.0 + i * 0.35);
  throwArc(`#ts${i}`, 111.0 + i * 0.35, x - 210, -150, 300, 0.8);
  tl.set(`#ts${i}`, { autoAlpha: 0 }, 111.8 + i * 0.35); sfx('coin', 111.8 + i * 0.35);
});
nar('发不出去、被拒稿？钱原路退回。', 114.1, 119.4);
pop('#s4wallet', 114.1, null);
tl.set('#ts3', { autoAlpha: 1, x: 1080, y: -160 }, 114.4);
move('#ts3', { x: 60, y: -360 }, 114.4, 1.0);
tl.set('#ts3', { autoAlpha: 0 }, 115.4);
const w = { v: 0 };
tl.to(w, { v: 390, duration: 0.8, ease: 'steps(8)', onUpdate: () => { $('#s4w').textContent = '+¥' + Math.round(w.v) + ' 退回'; } }, 115.4);
for (let i = 0; i < 4; i++) sfx('coin', 115.4 + i * 0.2);
cut('#s4', '#s5', 120);

// ===================== 第 5 关 监测 120–136 =====================
banner('第 5 关 · 到底有没有用？', 'OmniRank · 监测', 120.1);
tl.set('#s5say', { autoAlpha: 1 }, 120.5); typeText('#s5say', '感觉……好像有用？', 120.5, 0.9);
nar('做了到底有没有用？不能凭感觉。', 120.3, 123.6);
gone('#s5say', 123.7);
show('#s5cal', 123.9);
days.forEach((_, i) => {
  tl.to('#s5bot', { x: i % 2 ? 0 : 960, duration: 0.3, ease: 'steps(4)', ...SNAP }, 124.0 + i * 0.35);
  tl.call(() => { }, null, 0);
  tl.set(`#dk${i}`, { textContent: '✔', color: '#38b764' }, 124.3 + i * 0.35); sfx('tick', 124.3 + i * 0.35);
});
gone('#s5bot', 126.6);
pop('#s5chart', 126.6, 'select');
vals.forEach((v, i) => tl.fromTo(`#b${i}`, { scaleY: 0 }, { scaleY: 1, duration: 0.12, ease: 'steps(3)', immediateRender: false }, 126.8 + i * 0.15));
const pc = { v: 11 };
tl.to(pc, { v: 63, duration: 1.95, ease: 'none', onUpdate: () => { $('#s5pct').textContent = Math.round(pc.v) + '%'; } }, 126.8);
nar('监测系统每天替你问一遍 AI，记下它有没有提到你。', 123.9, 129.0);
gone(['#s5chart', '#s5cal'], 129.2);
pop('#s5news', 129.4, 'powerup');
hop('#s5boss', 130.4, 3, 60);
pop('#s5tv', 131.6, null);
nar('哪篇文章被 AI 引用了，还能追到是哪一篇、哪家媒体。', 129.3, 135.4);
cut('#s5', '#s6', 136);

// ===================== 第 6 关 飞轮 136–154 =====================
banner('第 6 关 · 越打越强', 'OmniRank · 飞轮系统', 136.1);
$$('.rc').forEach((el, i) => { tl.to(el, { y: 560 + (i % 3) * 90, duration: 1.2, ease: 'steps(8)', ...SNAP }, 136.4 + i * 0.18); if (i % 3 === 0) sfx('blip', 137.4 + i * 0.18); });
nar('每一次 AI 的回答，其实都在告诉我们：它喜欢什么。', 136.3, 141.0);
show(['#s6ring', '#s6core', '#s6xp'], 141.3);
$$('.rc').forEach((el, i) => { tl.to(el, { x: 900 - (120 + (i % 9) * 190), y: 560, scale: 0, duration: 0.5, ease: 'steps(5)', ...SNAP }, 141.4 + i * 0.08); });
tl.to('#s6ring', { rotation: 360, duration: 6, ease: 'steps(48)' }, 141.3);
sfx('charge', 141.4);
tl.to('#s6xpbar', { width: '100%', duration: 3.6, ease: 'steps(24)' }, 141.6);
nar('飞轮系统把这些回答收集起来，学出规律，', 141.3, 146.0);
gone(['#s6ring', '#s6core'], 146.3);
slam('#s6lv', 146.3); sfx('levelup', 146.4);
[0, 1, 2].forEach((i) => pop(`#up${i}`, 147.3 + i * 0.5, 'coin'));
show('#s6ups', 147.3);
pop('#s6note', 149.4, null);
nar('再反过来指导下一篇写什么、发在哪——越用越准。', 146.3, 153.4);
cut('#s6', '#s7', 154);

// ===================== 第 7 关 分销 & 计价 154–172 =====================
banner('第 7 关 · 一个人干不完', 'OmniRank · 分销 & 计价', 154.1);
$$('.sb').forEach((el, i) => { tl.fromTo(el, { y: 0 }, { keyframes: [{ y: -30, duration: 0.15 }, { y: 0, duration: 0.15 }], ease: 'steps(2)', immediateRender: false }, 154.4 + i * 0.2); });
sfx('blip', 154.4);
nar('可大多数老板不会自己做，他们会找服务商。', 154.3, 158.6);
pop('#s7agent', 156.6, 'jump'); pop('#s7badge', 156.9, null);
gone(['#s7bosses'], 158.8);
show('#s7tools', 158.9);
[0, 1, 2].forEach((i) => pop(`#tool${i}`, 159.0 + i * 0.6, 'select'));
pop('#s7sign', 161.5, null);
tl.to('#s7sign', { scaleX: 0, duration: 0.15, ease: 'steps(2)' }, 162.6);
tl.set('#s7signT', { textContent: '你的品牌' }, 162.75);
tl.set('#s7sign', { borderColor: '#38b764' }, 162.75);
tl.to('#s7sign', { scaleX: 1, duration: 0.15, ease: 'steps(2)' }, 162.75); sfx('powerup', 162.8);
nar('所以系统给服务商一整套开店工具：批发进货、自己定价、换上自己的招牌。', 158.9, 165.4);
gone(['#s7tools', '#s7sign', '#s7agent', '#s7badge'], 165.6);
pop('#s7book', 165.7, 'select');
rows.forEach((r, i) => { tl.fromTo(`#row${i}`, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.1, ease: 'steps(1)', immediateRender: false }, 166.3 + i * 0.7); sfx('coin', 166.3 + i * 0.7); });
nar('每一笔钱怎么扣、怎么退，都记在账上。', 165.7, 171.4);
cut('#s7', '#street', 172);

// ===================== 结局 172–182 =====================
banner('最终关 · 三个月后', '', 172.1);
tl.set(['#orbSay', '#bossSay', '#sweatS'], { autoAlpha: 0 }, 172);
tl.set('.cw', { autoAlpha: 0, x: 0 }, 172);
tl.set('#custN', { color: '#ffcd75' }, 172);
tl.set('#cw5', { autoAlpha: 1 }, 172.3);
move('#cw5', { x: 840 }, 172.3, 1.4); walk('#cw5', 172.3, 1.4);
tl.set('#custSay', { autoAlpha: 1 }, 173.8); typeText('#custSay', 'AI，深圳租埃尔法哪家好？', 173.8, 1.0);
nar('三个月后，客户再问 AI——', 172.3, 175.6);
tl.set('#orbSay', { autoAlpha: 1 }, 175.9); typeText('#orbSay', '推荐：一路顺风出行服务！', 175.9, 1.2);
sfx('levelup', 177.1);
gone('#custSay', 177.3);
nar('第一个名字，就是王老板。', 175.9, 179.6);
[5, 0, 1, 2, 3, 4].forEach((i, k) => {
  if (i !== 5) tl.set(`#cw${i}`, { autoAlpha: 1, x: 0 }, 177.4 + k * 0.25);
  move(`#cw${i}`, { x: 330 }, 177.4 + k * 0.25, i === 5 ? 0.6 : 1.6); walk(`#cw${i}`, 177.4 + k * 0.25, 1.6);
  tl.set(`#cw${i}`, { autoAlpha: 0 }, 179.0 + k * 0.25); sfx('door', 179.0 + k * 0.25);
});
const c2 = { v: 3 };
tl.to(c2, { v: 28, duration: 1.6, ease: 'steps(25)', onUpdate: () => { $('#custN').textContent = Math.round(c2.v); } }, 179.0);
tl.set('#custN', { color: '#38b764' }, 179.0);
hop('#bossS', 179.2, 4, 60);
cut('#street', '#end', 182);

// ===================== 结尾卡 182–194 =====================
gone('#hud', 182);
pop('#e0', 182.3, 'blip');
slam('#e1', 182.6);
slam('#e2', 183.3);
for (let i = 0; i < 16; i++) {
  const a = rnd() * Math.PI * 2, d = 300 + rnd() * 500;
  tl.set(`#cn${i}`, { autoAlpha: 0 }, 182);
}
pop('#e3', 184.3, 'blip');
pop('#e4', 185.0, 'select');
pop('#e5', 185.8, 'coin');
tl.fromTo('#e5', { autoAlpha: 1 }, { autoAlpha: 0.2, duration: 0.4, repeat: 9, yoyo: true, ease: 'steps(1)', immediateRender: false }, 186.4);
pop('#e6', 186.6, null);
nar('这套让 AI 推荐你的 GEO 系统，现在开源了。', 182.3, 186.8);
nar('去 GitHub 搜 OmniRank，部署一套试试。', 187.1, 191.6);

window.DURATION = 194;
// 配音版:字幕按真实配音时间由渲染脚本逐帧设置
window.setSub = (text) => { $('#subsT').textContent = text || ''; $('#subs').style.opacity = text ? 1 : 0; };
window.seekTo = (t) => { tl.seek(Math.round(t * 15) / 15, false); };  // 15fps 像素顿挫
window.ready = document.fonts.ready.then(() => document.fonts.load('48px "Fusion Pixel 12px Proportional Simplified Chinese"', '王老板开源')).then(() => true);
