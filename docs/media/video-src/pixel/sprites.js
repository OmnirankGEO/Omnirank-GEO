/* 像素精灵:字符网格 → canvas → dataURL。一个字符 = 一个像素。 */
const PAL = {
  k: '#1a1c2c', w: '#f4f4f4', s: '#f2c29b', S: '#d99a6c', h: '#2b2b2b', b: '#3b5dc9', B: '#29366f',
  r: '#e43b44', g: '#38b764', G: '#257179', y: '#ffcd75', o: '#ef7d57', p: '#b13e53', c: '#41a6f6',
  l: '#94b0c2', d: '#566c86', n: '#8b5a2b', m: '#5d3a1a', v: '#7b3fbf', V: '#4b2380', a: '#73eff7',
};
const SPR = {};
SPR.boss = [
  '....kkkkkkkk....', '...khhhhhhhhk...', '..khhhhhhhhhhk..', '..khhhhhhhhhhk..', '..khsssssssshk..',
  '..kskkkssskkksk.', '..kskwkssskwksk.', '..kssssssssssk..', '..kssssksksssk..', '...kssssssssk...',
  '....kwwrrwwk....', '..kbbwwrrwwbbk..', '.kbbbbwrrwbbbbk.', 'kbbbbbbrrbbbbbbk', 'kbkbbbbrrbbbbkbk',
  'kbkbbbbbbbbbbkbk', 'kskbbbbbbbbbbksk', 'kkkBBBBBBBBBBkkk', '...kBBBBBBBBk...', '...kBBBkkBBBk...',
  '...kBBBkkBBBk...', '...kBBBkkBBBk...', '..kmmmk..kmmmk..', '..kkkkk..kkkkk..',
];
SPR.bot = [
  '.......kk.......', '......kyyk......', '.......kk.......', '...kkkkkkkkkk...', '..kggggggggggk..',
  '..kgkkkkkkkkgk..', '..kgkccccccckgk.', '..kgkcwccwcckgk.', '..kgkcccccckgk..', '..kgkcwwwwckgk..',
  '..kgkkkkkkkkgk..', '..kggggggggggk..', '...kkkkkkkkkk...', '....kgGGGGgk....', '...kkgGGGGgkk...',
  '....kk....kk....',
];
const person = (X, hat) => [
  ...(hat ? ['..kkkkkkkk..', '.kmmmmmmmmk.', 'kkkkkkkkkkkk'] : ['...kkkkkk...', '..khhhhhhk..', '..khsssshk..']),
  '..kskssksk..', '..kssssssk..', ...(hat ? ['..kshhhhsk..'] : ['...kssssk...']),
  `..k${X}${X}${X}${X}${X}${X}k..`, `.k${X}${X}${X}${X}${X}${X}${X}${X}k.`, `.k${X}k${X}${X}${X}${X}k${X}k.`,
  `.ks${X}${X}${X}${X}${X}${X}sk.`, `..k${X}${X}${X}${X}${X}${X}k..`, '..kddddddk..', '..kddkkddk..',
  '..kddkkddk..', '..kkk..kkk..',
];
SPR.cust_r = person('r'); SPR.cust_c = person('c'); SPR.cust_o = person('o'); SPR.cust_g = person('g');
SPR.cust_v = person('v'); SPR.cust_y = person('y'); SPR.cust_l = person('l');
SPR.merchant = person('p', true);
SPR.agent = person('G');
SPR.coin = ['..kkkk..', '.kyyyyk.', 'kyyoyyyk', 'kyyoyyyk', 'kyyoyyyk', 'kyyoyyyk', '.kyyyyk.', '..kkkk..'];
SPR.scroll = ['.kkkkkkkk.', 'kwwwwwwwwk', 'kwdddddwwk', 'kwwwwwwwwk', 'kwddddddwk', 'kwwwwwwwwk', 'kwdddwwwwk', '.kkkkkkkk.'];
SPR.scrollgold = ['.kkkkkkkk.', 'kyyyyyyyyk', 'kyoooooyyk', 'kyyyyyyyyk', 'kyooooooyk', 'kyyyyyyyyk', 'kyoooyyyyk', '.kkkkkkkk.'];
SPR.bag = ['...kkkk...', '....kk....', '...kyyk...', '..kyyyyk..', '.kyyoyyyk.', 'kyyoooyyyk', 'kyyyoyyyyk', 'kyyoooyyyk', '.kyyyyyyk.', '..kkkkkk..'];
SPR.heart = ['.kk.kk.', 'krrkrrk', 'krrrrrk', '.krrrk.', '..krk..', '...k...'];
SPR.cloud = ['....wwww........', '..wwwwwwww.ww...', '.wwwwwwwwwwwwww.', 'wwwwwwwwwwwwwwww', '.wwwwwwwwwwwwww.'];
SPR.sweat = ['.c.', 'ccc', 'ccc', '.c.'];
SPR.weed = ['..nnnn..', '.n.mm.n.', 'n.n..n.n', 'nm.nn.mn', 'nm.nn.mn', 'n.n..n.n', '.n.mm.n.', '..nnnn..'];
SPR.hammer = ['kkkkkk..', 'kllllk..', 'kllllkkk', 'kllllknn', 'kkkkkknn', '......nn', '......nn', '......nn'];
SPR.book = ['kkkkkkkkkkkk', 'knnnnnnnnnnk', 'knwwwwwwwwnk', 'knwddddddwnk', 'knwwwwwwwwnk', 'knwddddwwwnk', 'knwwwwwwwwnk', 'knnnnnnnnnnk', 'kkkkkkkkkkkk'];
SPR.crate = ['kkkkkkkkkk', 'knnnnnnnnk', 'knmnnnnmnk', 'knnmnnmnnk', 'knnnmmnnnk', 'knnnmmnnnk', 'knnmnnmnnk', 'knmnnnnmnk', 'knnnnnnnnk', 'kkkkkkkkkk'];
SPR.tag = ['....kkkkkk', '...kyyyyyk', '..kyyyyyyk', '.kyykyyyyk', 'kyyyyyyyyk', '.kyyyyyyyk', '..kyyyyyyk', '...kyyyyyk', '....kkkkkk'];
SPR.star = ['....k....', '...kyk...', 'kkkkyykkk', 'kyyyyyyyk', '.kyyyyyk.', '..kyyyk..', '.kyykyyk.', '.kkk.kkk.'];

function circleSprite(R, fill, edge) {
  const n = R * 2, rows = [];
  for (let y = 0; y < n; y++) {
    let row = '';
    for (let x = 0; x < n; x++) {
      const d = Math.hypot(x + 0.5 - R, y + 0.5 - R);
      row += d > R ? '.' : d > R - 1.2 ? 'k' : d > R - 2.6 ? edge : fill;
    }
    rows.push(row);
  }
  return rows;
}
// AI 之眼:紫色球 + 大眼睛
SPR.orb = (() => {
  const R = 14, g = circleSprite(R, 'v', 'V').map(r => r.split(''));
  const put = (x, y, ch) => { if (g[y] && g[y][x] !== undefined && g[y][x] !== '.') g[y][x] = ch; };
  for (let y = 9; y <= 18; y++) for (let x = 7; x <= 20; x++) { const e = ((x - 13.5) / 7) ** 2 + ((y - 13.5) / 5) ** 2; if (e <= 1) put(x, y, e > 0.75 ? 'k' : 'w'); }
  for (let y = 11; y <= 16; y++) for (let x = 11; x <= 16; x++) if (Math.hypot(x - 13.5, y - 13.5) <= 2.8) put(x, y, Math.hypot(x - 13.5, y - 13.5) < 1.5 ? 'k' : 'a');
  put(12, 12, 'w');
  for (let y = 3; y <= 6; y++) for (let x = 7; x <= 10; x++) if (g[y][x] === 'v') g[y][x] = 'p';
  return g.map(r => r.join(''));
})();
SPR.shield = [
  'kkkkkkkkkkkk', 'kllllllllllk', 'klaaaaaaaalk', 'klaaawwaaalk', 'klaawkkwaalk', 'klaaawwaaalk',
  'klaaaaaaaalk', '.klaaaaaalk.', '.klaaaaaalk.', '..klaaaalk..', '...kllllk...', '....kkkk....',
];

function spriteURL(name) {
  if (spriteURL.cache[name]) return spriteURL.cache[name];
  const g = SPR[name], h = g.length, w = Math.max(...g.map(r => r.length));
  const cv = document.createElement('canvas'); cv.width = w; cv.height = h;
  const ctx = cv.getContext('2d');
  g.forEach((row, y) => [...row].forEach((ch, x) => { if (ch !== '.' && PAL[ch]) { ctx.fillStyle = PAL[ch]; ctx.fillRect(x, y, 1, 1); } }));
  return (spriteURL.cache[name] = { url: cv.toDataURL(), w, h });
}
spriteURL.cache = {};
/** 生成一个像素精灵 <img>;scale = 每个像素放大倍数 */
function sprite(name, scale = 6, cls = '', style = '') {
  const s = spriteURL(name);
  return `<img class="px ${cls}" src="${s.url}" style="width:${s.w * scale}px;height:${s.h * scale}px;${style}">`;
}
