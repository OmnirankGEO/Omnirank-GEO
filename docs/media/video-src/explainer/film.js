/* OmniRank 讲解片时间轴 · 由 render.cjs 逐帧 seek 渲染 */
const $ = (s) => document.querySelector(s);
const tl = gsap.timeline({ paused: true });
const E = 'power3.out';

// ---------- helpers ----------
function scene(id, start, end) {
  tl.set(id, { autoAlpha: 1 }, start);
  tl.fromTo(id, { opacity: 0 }, { opacity: 1, duration: 0.5 }, start);
  tl.to(id, { opacity: 0, duration: 0.5 }, end - 0.5);
  tl.set(id, { autoAlpha: 0 }, end);
}
function cap(html, at, dur) {
  tl.call(() => { $('#capt').innerHTML = html; }, null, at);
  tl.fromTo('#capt', { opacity: 0, y: 14 }, { opacity: 1, y: 0, duration: 0.35, immediateRender: false }, at);
  tl.to('#capt', { opacity: 0, duration: 0.3 }, at + dur - 0.3);
}
function tag(num, title, sub, at, end) {
  tl.call(() => {
    $('#tag .num').textContent = num; $('#tag .t').textContent = title; $('#tag .s').textContent = sub;
  }, null, at);
  tl.fromTo('#tag', { opacity: 0, x: -30 }, { opacity: 1, x: 0, duration: 0.5, ease: E, immediateRender: false }, at);
  tl.to('#tag', { opacity: 0, duration: 0.4 }, end - 0.5);
}
function pop(sel, at, opts = {}) {
  tl.fromTo(sel, { opacity: 0, y: opts.y ?? 30, scale: opts.s ?? 1 }, { opacity: 1, y: 0, scale: 1, duration: opts.d ?? 0.6, ease: E, stagger: opts.st ?? 0, immediateRender: true }, at);
}
function shotIn(sel, at, out) {
  tl.fromTo(sel, { opacity: 0, y: 80, scale: 0.94 }, { opacity: 1, y: 0, scale: 1, duration: 0.8, ease: E, immediateRender: true }, at);
  if (out) tl.to(sel, { opacity: 0, y: -40, duration: 0.5 }, out);
}
function typeText(el, text, at, dur) {
  const o = { n: 0 };
  tl.to(o, { n: text.length, duration: dur, ease: 'none', onUpdate: () => { el.textContent = text.slice(0, Math.round(o.n)); } }, at);
}
function el(html) { const d = document.createElement('div'); d.innerHTML = html.trim(); return d.firstChild; }

// ---------- S1 hook 0-15 ----------
scene('#s1', 0, 15);
pop('#s1hint', 0.4, { y: 0 });
pop('#s1u', 1.2, { s: 0.9 });
typeText($('#s1ut'), '深圳商务用车，哪家比较靠谱？', 1.4, 1.4);
pop('#s1a', 3.4, { s: 0.95 });
pop('#s1a1', 3.8, { y: 10 });
pop('#s1a2', 4.6, { y: 10 });
pop('#s1miss', 6.2, { s: 0.7 });
tl.to('#s1miss', { x: -8, duration: 0.06, repeat: 5, yoyo: true }, 6.9);
pop('#s1big', 8.6);
cap('越来越多的人不再搜索，而是<b>直接问 AI</b>', 0.6, 5.6);
cap('AI 不给十条链接，<b>它直接说出几个名字</b>', 6.4, 4);
cap('OmniRank 要解决的，就是这个问题', 10.6, 4.2);

// ---------- S2 title 15-24 ----------
scene('#s2', 15, 24);
pop('#s2a', 15.3, { s: 0.9, d: 0.9 });
pop('#s2b', 16.3);
pop('#s2c .pill', 17.1, { st: 0.15, y: 16 });
pop('#s2d', 18.4);
tl.fromTo(['#brand'], { opacity: 0 }, { opacity: 1, duration: 0.6 }, 24);

// ---------- S3 overview 24-38 ----------
scene('#s3', 24, 38);
const ringLabels = [['1', '诊断报告'], ['2', '报价'], ['3', '创作中心'], ['4', '发布'], ['5', '监测'], ['6', '飞轮系统']];
const cx = 960, cy = 430, R = 290;
const ring = $('#s3ring');
const ns = 'http://www.w3.org/2000/svg';
const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('width', 1920); svg.setAttribute('height', 1080); svg.style.position = 'absolute';
const circ = document.createElementNS(ns, 'circle'); circ.setAttribute('cx', cx); circ.setAttribute('cy', cy); circ.setAttribute('r', R);
circ.setAttribute('fill', 'none'); circ.setAttribute('stroke', '#2fd47a'); circ.setAttribute('stroke-width', 4); circ.setAttribute('stroke-dasharray', 2 * Math.PI * R); circ.setAttribute('stroke-dashoffset', 2 * Math.PI * R);
circ.setAttribute('transform', `rotate(-90 ${cx} ${cy})`); circ.id = 's3circ'; circ.style.opacity = .55;
svg.appendChild(circ); ring.appendChild(svg);
ringLabels.forEach(([n, l], i) => {
  const a = -Math.PI / 2 + i * Math.PI / 3;
  const node = el(`<div class="node" id="s3n${i}"><div class="n">第 ${n} 步</div><div class="l">${l}</div></div>`);
  node.style.left = (cx + R * Math.cos(a) - 105) + 'px'; node.style.top = (cy + R * Math.sin(a) - 105) + 'px';
  ring.appendChild(node);
});
tl.to('#s3circ', { attr: { 'stroke-dashoffset': 0 }, duration: 6, ease: 'none' }, 24.6);
ringLabels.forEach((_, i) => {
  pop(`#s3n${i}`, 24.6 + i, { s: 0.6, y: 0 });
  tl.to(`#s3n${i}`, { borderColor: '#2fd47a', boxShadow: '0 0 40px rgba(47,212,122,.35)', duration: 0.4 }, 24.8 + i);
});
pop('#s3mid', 25);
pop('#s3base', 31.2);
cap('六个环节连成一条线：<b>看清现状 → 定方案 → 写内容 → 发出去 → 看效果</b>', 24.8, 6.4);
cap('飞轮把 AI 的真实回答<b>反过来喂给写作</b>；分销和计价托住整条线', 31.3, 6.5);

// ---------- S4 diagnosis 38-72 ----------
scene('#s4', 38, 72);
tag('1', '诊断报告', '给品牌做一次「AI 体检」', 38.2, 72);
cap('<b>有什么用：</b>查清 AI 现在怎么说你、怎么说同行、你差在哪', 38.4, 5);
pop('#s4in', 38.6);
pop('#s4qs div', 40.2, { st: 0.35, y: 12 });
pop('#s4grid > .muted', 43, { y: 10 });
pop('#s4heads .ghead', 43.3, { st: 0.12, y: 10 });
const qs = ['豪车配司机哪家好', '租埃尔法配司机', '企业长租怎么选', '品牌靠谱吗'];
const res = [['n', 'n', 'm', 'n'], ['n', 'n', 'n', 'n'], ['n', 'm', 'n', 'n'], ['r', 'r', 'm', 'r']];
const lab = { r: ['推荐', '#123d27', '#2fd47a'], m: ['提到', '#3a2e12', '#f5b041'], n: ['未提到', '#3a1717', '#ef5b5b'] };
qs.forEach((q, i) => {
  const row = el(`<div class="flex" style="gap:14px;margin-bottom:12px"><div class="qrow">“${q}”</div></div>`);
  res[i].forEach((r, j) => {
    const c = el(`<div class="gcell" id="s4c${i}${j}"><span style="opacity:0">${lab[r][0]}</span></div>`);
    row.appendChild(c);
  });
  $('#s4rows').appendChild(row);
});
cap('把客户真会问的问题，<b>同时发给多家 AI</b>，开联网搜索还原真实回答', 43.6, 5.2);
qs.forEach((_, i) => res[i].forEach((r, j) => {
  const t = 44.2 + i * 0.7 + j * 0.18;
  tl.to(`#s4c${i}${j}`, { background: lab[r][1], borderColor: lab[r][2], color: lab[r][2], duration: 0.3 }, t);
  tl.to(`#s4c${i}${j} span`, { opacity: 1, duration: 0.3 }, t);
}));
pop('#s4legend', 47.2, { y: 10 });
cap('逐条判断：<b>有没有提到你</b>、是推荐还是顺带一提、同行被推荐了几次', 48.9, 5);
// score
const bars = [['品牌认知 · 搜你的名字能不能答对', '20%', 1.0, '#2fd47a'], ['决策获客 · 问「哪家好」推不推荐你', '40%', 0.0, '#ef5b5b'], ['场景转化 · 问方案、对比时引不引用你', '40%', 0.15, '#f5b041']];
bars.forEach(([l, w, v, c], i) => {
  $('#s4bars').appendChild(el(`<div style="margin-bottom:12px"><div style="font-size:23px;margin-bottom:6px">${l} <span class="muted">权重 ${w}</span></div><div class="barwrap" style="width:640px"><div class="barfill" id="s4b${i}" style="background:${c}"></div></div></div>`));
});
pop('#s4score', 54);
bars.forEach((b, i) => tl.to(`#s4b${i}`, { width: Math.max(b[2] * 100, 1.5) + '%', duration: 1, ease: E }, 54.8 + i * 0.4));
const sc = { v: 0 };
tl.to(sc, { v: 26, duration: 1.6, ease: 'power2.out', onUpdate: () => { $('#s4num').textContent = Math.round(sc.v) + ' 分'; } }, 55.2);
cap('按用户的决策过程分三层，<b>按规则算分</b>，不让大模型凭感觉打', 54.1, 5.6);
tl.to(['#s4in', '#s4grid', '#s4score'], { opacity: 0.12, duration: 0.6 }, 60);
shotIn('#s4shot', 60.2, 65.8);
cap('出报告：每个平台的<b>AI 回答原文</b>做证据，附行动清单，可分享给客户', 60.4, 5.6);
shotIn('#s4shot2', 66, 71.3);
cap('报告直接把分数翻译成生意语言：<b>老客户搜得到，新客户问「哪家好」时被截走</b>', 66.2, 5.4);

// ---------- S5 quote 72-96 ----------
scene('#s5', 72, 96);
tag('2', '报价', '告诉客户：做哪些、做多少、花多少', 72.2, 96);
cap('<b>有什么用：</b>体检之后客户会问「要花多少钱」，系统帮你有依据地报价', 72.4, 5.2);
const fparts = [['需要的篇数', '看同行在这个问题下<br>已有多少内容，倒推'], ['×'], ['单篇成本', '写作 + 发布 + 运营<br>默认 60 元'], ['×'], ['服务商系数', '服务商自己定'], ['×'], ['价值系数', '搜索量、广告竞价<br>越热越值钱'], ['='], ['报价', '每个问题单独算']];
fparts.forEach((p, i) => {
  const node = p.length === 1
    ? el(`<div class="arrow s5fp" style="font-size:54px;font-weight:900">${p[0]}</div>`)
    : el(`<div class="step s5fp" style="width:${i === 8 ? 230 : 300}px;${i === 8 ? 'border-color:#2fd47a' : ''}">${p[0]}<small>${p[1]}</small></div>`);
  node.style.position = 'relative';
  $('#s5f').appendChild(node);
});
pop('.s5fp', 73, { st: 0.32, y: 20 });
cap('价格不是拍脑袋：<b>篇数 × 单篇成本 × 系数</b>，竞争越激烈要发得越多', 77.8, 5.6);
const tiers = [['入门版', '¥3,000', 'AI 出现率目标 50%'], ['标准版', '¥5,000', 'AI 出现率目标 65%'], ['旗舰版', '¥8,000', 'AI 出现率目标 75%']];
tiers.forEach(([n, p, d], i) => {
  $('#s5tiers').appendChild(el(`<div class="card s5t" style="width:460px;padding:26px 30px;text-align:center;${i === 1 ? 'border:2px solid #2fd47a' : ''}"><div class="muted" style="font-size:26px">${n}</div><div style="font-size:60px;font-weight:900;margin:6px 0">${p}</div><div style="font-size:22px" class="muted">${d}</div></div>`));
});
pop('.s5t', 79, { st: 0.25 });
const flow = ['生成选词链接', '客户勾选想做的词', '确认并锁定价格', '收款', '自动建写作任务 + 开启监测'];
flow.forEach((f, i) => {
  if (i) $('#s5flow').appendChild(el(`<div class="arrow s5fl">→</div>`));
  $('#s5flow').appendChild(el(`<div class="pill s5fl" style="font-size:26px;padding:12px 22px;${i === 4 ? 'border-color:#2fd47a;color:#2fd47a' : ''}">${f}</div>`));
});
pop('.s5fl', 83.4, { st: 0.2, y: 12 });
cap('分入门、标准、旗舰三档；客户确认后，<b>报价自动变成写作任务和监测任务</b>', 83.6, 5.4);
tl.to(['#s5f', '#s5tiers', '#s5flow'], { opacity: 0.12, duration: 0.6 }, 89.2);
shotIn('#s5shot', 89.4, 95.3);
cap('真实界面：每个关键词的价格、推荐理由都列在明细里', 89.6, 5.8);

// ---------- S6 writing 96-120 ----------
scene('#s6', 96, 120);
tag('3', '创作中心', '写出 AI 愿意引用的内容', 96.2, 120);
cap('<b>有什么用：</b>围绕用户真会问的问题，批量写出有证据、不吹牛的文章', 96.4, 5.4);
const pipe = [['准备素材', '品牌资料 · 知识库<br>上网找证据'], ['AI 写作', '多种模型可选<br>失败自动换备用'], ['事实核查', '无来源数字<br>绝对化用语'], ['AI 审稿', '可直接发 / 改一下<br>/ 要人看'], ['自动修复', '只改有问题的段落'], ['人工把关', '可编辑、可重写<br>医疗法律金融必签']];
pipe.forEach(([t, s], i) => {
  const x = 70 + i * 300;
  const node = el(`<div class="step s6p" id="s6p${i}" style="left:${x}px;top:250px;width:250px;height:190px;display:flex;flex-direction:column;justify-content:center">${t}<small>${s}</small></div>`);
  $('#s6pipe').appendChild(node);
  if (i < pipe.length - 1) $('#s6pipe').appendChild(el(`<div class="arrow abs s6a" style="left:${x + 258}px;top:315px">→</div>`));
});
pop('.s6p', 97, { st: 0.45 });
pop('.s6a', 97.3, { st: 0.45, y: 0 });
pipe.forEach((_, i) => tl.to(`#s6p${i}`, { borderColor: '#2fd47a', duration: 0.3 }, 102 + i * 0.35));
cap('写之前先备料：没有证据就<b>上网检索</b>，核实过的才能当事实写', 101.9, 5);
pop('#s6demo', 107);
tl.fromTo('#s6hl', { backgroundColor: 'rgba(239,91,91,0)' }, { backgroundColor: 'rgba(239,91,91,.35)', duration: 0.4 }, 108.4);
pop('#s6why', 109, { y: 8 });
tl.to('#s6bad', { opacity: 0.45, textDecoration: 'line-through', duration: 0.3 }, 110.8);
pop('#s6good', 111.2, { y: 8 });
cap('它<b>最怕编造</b>：「行业第一」、没来源的数字、虚构案例都会被挑出来修掉', 107.1, 6);
cap('因为 AI 平台只愿意引用<b>说得清、查得到</b>的内容', 113.3, 6.2);

// ---------- S7 publish 120-144 ----------
scene('#s7', 120, 144);
tag('4', '发布', '把内容放到 AI 会读的地方', 120.2, 144);
cap('<b>有什么用：</b>文章不发出去 AI 读不到——帮你选媒体、下单、追结果', 120.4, 5.4);
const modes = [['不接渠道（默认）', '不能发布，也不扣钱', '#8b95a3'], ['模拟发布', '整条流程走一遍，全是演示数据', '#f5b041'], ['真实发布', '对接第三方渠道，真实投放', '#2fd47a']];
$('#s7modes').appendChild(el(`<div class="muted" style="font-size:22px;margin-bottom:16px">三种模式，一个配置项切换</div>`));
modes.forEach(([t, d, c]) => $('#s7modes').appendChild(el(`<div class="card s7m" style="padding:22px 26px;margin-bottom:16px;border-left:6px solid ${c}"><div style="font-size:30px;font-weight:700">${t}</div><div class="muted" style="font-size:22px;margin-top:6px">${d}</div></div>`)));
pop('.s7m', 121, { st: 0.3 });
const media = [['搜狐号 · 汽车出行频道', 86, true], ['新浪汽车 · 出行综合', 78, true], ['凤凰网 · 商务出行', 64, false], ['某论坛 · 杂谈版', 31, false]];
$('#s7media').appendChild(el(`<div class="muted" style="font-size:22px;margin-bottom:14px">系统推荐媒体 · 最看重「在你的行业里<b class="green">真的被 AI 引用过</b>」</div>`));
media.forEach(([n, s, cited], i) => $('#s7media').appendChild(el(`<div class="flex s7r" style="align-items:center;gap:20px;height:62px;font-size:26px"><div style="width:360px">${n}</div><div class="barwrap" style="width:400px"><div class="barfill" id="s7b${i}" style="background:${s >= 50 ? '#2fd47a' : '#5b6472'}"></div></div><div style="width:70px;text-align:right">${s}</div><div style="width:150px">${cited ? '<span class="chk" style="background:#123d27;color:#2fd47a">被 AI 引用</span>' : s < 50 ? '<span class="muted" style="font-size:20px">不推荐</span>' : ''}</div></div>`)));
pop('#s7media', 126);
pop('.s7r', 126.3, { st: 0.2, y: 10 });
media.forEach((m, i) => tl.to(`#s7b${i}`, { width: m[1] + '%', duration: 0.8, ease: E }, 126.8 + i * 0.2));
cap('推荐依据：<b>历史引用证据</b>、媒体质量、平台权重、价格，有风险的不推荐', 126.2, 5.4);
const states = ['下单（只用付费算力）', '已提交', '发布中', '已发布 ✓'];
states.forEach((s, i) => {
  if (i) $('#s7states').appendChild(el(`<div class="arrow s7s">→</div>`));
  $('#s7states').appendChild(el(`<div class="pill s7s" style="font-size:24px;padding:12px 20px;${i === 3 ? 'border-color:#2fd47a;color:#2fd47a' : ''}">${s}</div>`));
});
pop('.s7s', 131.8, { st: 0.25, y: 10 });
$('#s7refund').innerHTML = '被拒稿 / 失败 / 撤单 → <b class="green">原额退回算力</b>　·　状态不明先核对，不糊涂扣钱';
pop('#s7refund', 133.4, { y: 10 });
cap('后台定时任务去渠道查结果；<b>发不出去自动退钱</b>', 131.8, 4.2);
tl.to(['#s7modes', '#s7media', '#s7states', '#s7refund'], { opacity: 0.12, duration: 0.6 }, 136.4);
shotIn('#s7shot', 136.6, 143.3);
cap('真实界面：选文章、选媒体、加入清单、一键发布', 136.8, 6.4);

// ---------- S8 monitoring 144-170 ----------
scene('#s8', 144, 170);
tag('5', '监测', '定期问 AI，看效果有没有变好', 144.2, 170);
cap('<b>有什么用：</b>做了到底有没有用？系统每天替你去问 AI 同样的问题', 144.4, 5.4);
pop('#s8chart', 144.8);
const pts = [11, 12, 15, 14, 20, 24, 23, 31, 36, 34, 42, 47, 51, 49, 55, 60, 58, 63];
const X = (i) => 40 + i * (940 / (pts.length - 1)); const Y = (v) => 410 - v * (350 / 75);
const full = pts.map((v, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
$('#s8line').setAttribute('d', full);
const len = 1400; $('#s8line').style.strokeDasharray = len; $('#s8line').style.strokeDashoffset = len;
const prog = { p: 0 };
tl.to(prog, {
  p: 1, duration: 7, ease: 'none', onUpdate: () => {
    $('#s8line').style.strokeDashoffset = len * (1 - prog.p);
    const f = prog.p * (pts.length - 1); const i = Math.min(Math.floor(f), pts.length - 2); const r = f - i;
    const v = pts[i] + (pts[i + 1] - pts[i]) * r;
    $('#s8dot').setAttribute('cx', X(i) + (X(i + 1) - X(i)) * r); $('#s8dot').setAttribute('cy', Y(v));
    $('#s8pct').textContent = Math.round(v) + '%';
  }
}, 145.6);
const cats = [['被推荐', '含有条件推荐、进入候选名单', '#2fd47a'], ['仅提到', '提到了但没推荐', '#f5b041'], ['未提到', '只说了同行', '#ef5b5b'], ['疑似提到', '名字像但拿不准，人确认后才计入', '#5aa9ff']];
$('#s8cats').appendChild(el(`<div class="muted" style="font-size:22px;margin-bottom:14px">每条 AI 回答归成四类之一</div>`));
cats.forEach(([t, d, c]) => $('#s8cats').appendChild(el(`<div class="card s8c" style="padding:18px 24px;margin-bottom:14px;border-left:6px solid ${c}"><div style="font-size:30px;font-weight:700;color:${c}">${t}</div><div class="muted" style="font-size:21px;margin-top:4px">${d}</div></div>`)));
pop('.s8c', 150, { st: 0.3 });
cap('不偷偷改问法，<b>前后可比</b>；看趋势、看是否达到套餐目标', 149.9, 5.4);
$('#s8attr').innerHTML = '<span class="pill" style="font-size:24px">📝 我们发的文章《深圳企业用车怎么选》</span><span class="arrow">→</span><span class="pill" style="font-size:24px">🤖 DeepSeek 的回答里引用了这篇</span><span class="arrow">→</span><span style="font-size:26px">知道<b class="green">哪篇文章、哪家媒体</b>真的起了作用</span>';
pop('#s8attr', 155.6);
cap('AI 引用的链接正好是我们发的文章？<b>记一笔功劳</b>，归到具体文章和媒体', 155.7, 5.4);
tl.to(['#s8chart', '#s8cats', '#s8attr'], { opacity: 0.12, duration: 0.6 }, 161.3);
shotIn('#s8shot', 161.5, 169.3);
cap('真实界面：出现率、达标情况、需要人确认的品牌名，一屏看完', 161.7, 7.4);

// ---------- S9 flywheel 170-192 ----------
scene('#s9', 170, 192);
tag('6', '飞轮系统', '越用越懂 AI 喜欢什么', 170.2, 192);
cap('<b>有什么用：</b>前五步是「做一次」，飞轮让系统<b>越做越准</b>', 170.4, 5);
const fw = [['收集', '按行业定期问 AI<br>抓下被引用的文章'], ['找规律', '哪类内容被采纳<br>哪类只是被搜到'], ['人工把关', '只出建议草稿'], ['用起来', '写作模板 · 选题<br>选媒体'], ['看效果', '前后对比<br>变差就提示回滚']];
const fcx = 620, fcy = 520, fR = 290;
const svg2 = document.createElementNS(ns, 'svg'); svg2.setAttribute('width', 1920); svg2.setAttribute('height', 1080); svg2.style.position = 'absolute';
const c2 = document.createElementNS(ns, 'circle'); c2.setAttribute('cx', fcx); c2.setAttribute('cy', fcy); c2.setAttribute('r', fR); c2.setAttribute('fill', 'none'); c2.setAttribute('stroke', '#2fd47a'); c2.setAttribute('stroke-width', 3); c2.setAttribute('stroke-dasharray', '14 14'); c2.style.opacity = .5; c2.id = 's9c';
svg2.appendChild(c2); $('#s9ring').appendChild(svg2);
fw.forEach(([t, d], i) => {
  const a = -Math.PI / 2 + i * 2 * Math.PI / 5;
  const n = el(`<div class="step s9n" id="s9n${i}" style="width:260px;left:${fcx + fR * Math.cos(a) - 130}px;top:${fcy + fR * Math.sin(a) - 62}px">${t}<small>${d}</small></div>`);
  $('#s9ring').appendChild(n);
});
$('#s9ring').appendChild(el(`<div class="abs center" style="left:${fcx - 120}px;top:${fcy - 50}px;width:240px;height:100px;font-size:34px;font-weight:900;text-align:center" id="s9mid">AI 的<br>真实回答</div>`));
pop('#s9mid', 170.8, { s: 0.8 });
pop('.s9n', 171.2, { st: 0.5, s: 0.85 });
tl.fromTo('#s9c', { rotation: 0, svgOrigin: `${fcx} ${fcy}` }, { rotation: 120, svgOrigin: `${fcx} ${fcy}`, duration: 21, ease: 'none' }, 170.5);
fw.forEach((_, i) => tl.to(`#s9n${i}`, { borderColor: '#2fd47a', duration: 0.3 }, 176 + i * 0.6));
const w = [['被 AI 采纳进答案', 100, '#2fd47a'], ['被 AI 明确引用', 78, '#5aa9ff'], ['只被搜到', 32, '#f5b041'], ['只被爬过', 12, '#5b6472']];
w.forEach(([t, v, c], i) => $('#s9bars').appendChild(el(`<div style="margin-bottom:14px"><div style="font-size:24px;margin-bottom:6px">${t}</div><div class="barwrap" style="width:520px"><div class="barfill" id="s9b${i}" style="background:${c}"></div></div></div>`)));
pop('#s9w', 176);
w.forEach((x, i) => tl.to(`#s9b${i}`, { width: x[1] + '%', duration: 0.8, ease: E }, 176.4 + i * 0.25));
cap('观察 AI <b>最爱引用哪些网站、哪种写法</b>，再反过来指导写作、选题、选媒体', 175.6, 6.4);
pop('#s9rule', 182.4);
cap('稳字当头：<b>学到的只是建议</b>，人确认才生效；新写法先做对照实验', 182.2, 9.4);

// ---------- S10 distribution + billing 192-218 ----------
scene('#s10', 192, 218);
tag('+', '分销 & 计价', '让服务商用它做生意，每一笔都算得清', 192.2, 218);
$('#s10l').innerHTML = `
  <div class="muted" style="font-size:22px;margin-bottom:16px">🤝 分销系统</div>
  <div class="flex" style="align-items:center;gap:16px">
    <div class="step s10a" style="position:relative;width:220px">平台<small>出厂价</small></div><div class="arrow s10a">→</div>
    <div class="step s10a" style="position:relative;width:250px;border-color:#2fd47a">服务商<small>进货默认 9 折<br>自己定零售价</small></div><div class="arrow s10a">→</div>
    <div class="step s10a" style="position:relative;width:220px">客户<small>线上付 / 线下付</small></div>
  </div>
  <div class="card s10b" style="margin-top:28px;padding:24px 30px;font-size:26px;line-height:1.85">
    <div>💵 赚差价：<b>客户付的钱 − 出厂价</b>，3 天后可提现</div>
    <div>🏷️ 白标：报告、报价页换成服务商自己的品牌</div>
    <div>👥 团队：席位、分配客户、消费上限、审批</div>
    <div>🔗 下级服务商：收益<b>只给直属上级</b>，不搞多级抽成</div>
  </div>`;
$('#s10r').innerHTML = `
  <div class="muted" style="font-size:22px;margin-bottom:16px">💰 计价系统 · 单位「算力」（默认 1 元 = 130 算力）</div>
  <div class="flex" style="gap:16px">
    <div class="card s10c" style="flex:1;padding:20px;border-top:5px solid #2fd47a"><div style="font-size:28px;font-weight:700">付费算力</div><div class="muted" style="font-size:20px;margin-top:6px">充值来的<br>可退款</div></div>
    <div class="card s10c" style="flex:1;padding:20px;border-top:5px solid #f5b041"><div style="font-size:28px;font-weight:700">赠送算力</div><div class="muted" style="font-size:20px;margin-top:6px">活动、返利<br>只能消费</div></div>
    <div class="card s10c" style="flex:1;padding:20px;border-top:5px solid #5aa9ff"><div style="font-size:28px;font-weight:700">佣金算力</div><div class="muted" style="font-size:20px;margin-top:6px">分销收益<br>可提现</div></div>
  </div>
  <div class="muted s10d" style="font-size:22px;margin:30px 0 14px">耗时任务的扣费方式</div>
  <div class="flex s10d" style="align-items:center;gap:14px;font-size:26px">
    <span class="pill" style="font-size:24px">先冻结</span><span class="arrow">→</span>
    <span class="pill" style="font-size:24px;border-color:#2fd47a;color:#2fd47a">成功：按实际扣</span>
    <span class="pill" style="font-size:24px;border-color:#ef5b5b;color:#ef5b5b">失败：解冻退回</span>
  </div>
  <div class="s10d" style="font-size:24px;margin-top:22px" class="muted">每一笔都有流水 · 管理员调账要填原因、留记录</div>`;
pop('.s10a', 192.6, { st: 0.25 });
pop('.s10b', 194.6);
cap('<b>分销：</b>服务商按批发价进货、自己定价卖给客户，还能换上自己的品牌', 192.6, 7.4);
pop('.s10c', 200.2, { st: 0.25 });
pop('.s10d', 202.6, { st: 0.3, y: 12 });
cap('<b>计价：</b>按钮旁就标着要花多少算力；<b>成功才扣，失败自动退</b>', 200.2, 8);
cap('收得清、扣得准、退得回、查得到', 208.4, 9.2);

// ---------- S11 outro 218-230 ----------
scene('#s11', 218, 230.5);
tl.to('#brand', { opacity: 0, duration: 0.4 }, 217.6);
pop('#s11a', 218.4, { y: 12 });
pop('#s11b', 219, { s: 0.9, d: 0.9 });
pop('#s11c .pill', 220, { st: 0.15, y: 12 });
pop('#s11d', 221);
pop('#s11e', 221.8);

// progress bar
tl.fromTo('#progress', { width: 0 }, { width: 1920, duration: 230.5, ease: 'none' }, 0);

window.DURATION = 230.5;
window.seekTo = (t) => { tl.seek(t, false); };
window.ready = document.fonts.ready.then(() => true);
