/** Production market helpers + actual React SSR (no browser, external calls or file writes). */
import assert from 'node:assert/strict';
import Module from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
const root = fileURLToPath(new URL('../', import.meta.url));
async function load(path) {
  const compiled = await build({ entryPoints: [join(root, path)], absWorkingDir: root, bundle: true,
    write: false, platform: 'node', format: 'cjs', jsx: 'automatic', packages: 'external',
    tsconfig: join(root, 'tsconfig.app.json'), logLevel: 'silent',
    plugins: [{ name: 'offline-reader', setup(b) {
      b.onResolve({ filter: /^@\/lib\/api$/ }, () => ({ path: 'api', namespace: 'offline' }));
      b.onResolve({ filter: /^@\/context\/AuthContext$/ }, () => ({ path: 'auth', namespace: 'offline' }));
      b.onResolve({ filter: /^@\/context\/ClientContext$/ }, () => ({ path: 'client', namespace: 'offline' }));
      b.onLoad({ filter: /.*/, namespace: 'offline' }, args => ({ contents: args.path === 'auth' ? 'export const useAuth=()=>({user:null})' : args.path === 'client' ? 'export const useClientContext=()=>({clients:[]})' : 'export function authFetch(){throw new Error("SSR must not fetch")}' }));
    } }],
  });
  const filename = join(root, 'scripts/.market-render.cjs');
  const mod = new Module(filename); mod.filename = filename; mod.paths = Module._nodeModulePaths(dirname(filename));
  mod._compile(compiled.outputFiles[0].text, filename); return mod.exports;
}
const M = await load('src/pages/Publishing/shortVideoMarket.ts');
const UI = await load('src/pages/Publishing/ShortVideoAccountMarket.tsx');
const Receipt = await load('src/pages/Writing/ImageNotePublishInline.tsx');
const List = await load('src/pages/Publishing/ImageNoteList.tsx');
const Status = await load('src/pages/Publishing/imageNotePublishStatus.ts');
let passed = 0;
function test(name, fn) { fn(); passed++; console.log(`PASS market ${name}`); }
const scope = { platform: '抖音', imageNote: true };
const base = M.emptyMarketFilters();
const account = { id: 71, media_name: '目录真实账号', platform: '抖音', price_points: 260,
  industry: '旅游', location: '上海', fans_num: 5000, avg_likes_num: 333,
  avg_publish_time: '33384', account_auth: '蓝V', authority_media: 1, can_modify: 1, remark: '账号原始备注' };
const render = (component, props) => renderToStaticMarkup(createElement(component, props));
test('image scope cannot be changed by a platform filter', () => {
  const q = new URLSearchParams(M.marketQuery({ ...base, platform: '微博', page: 3, search: ' 酒店 ', pointsMin: '6500', pointsMax: '13000' }, scope));
  assert.equal(q.get('platform'), '抖音'); assert.equal(q.get('can_tuwen'), '1');
  assert.equal(q.get('page'), '3'); assert.equal(q.get('limit'), '20'); assert.equal(q.get('search'), '酒店');
  assert.equal(q.get('points_min'), '6500'); assert.equal(q.get('points_max'), '13000');
});
test('video keeps all platforms/capabilities unless selected', () => {
  const q = new URLSearchParams(M.marketQuery(base)); assert.equal(q.has('can_tuwen'), false); assert.equal(q.has('platform'), false);
});
test('missing or malformed catalog data becomes retryable error, never zero or NaN', () => {
  assert.deepEqual(M.readMarketPage({ media: [], total: 0 }), { media: [], total: 0 });
  for (const value of [null, {}, { media: [], total: '5' }, { media: [], total: -1 }, { media: null, total: 5 }, { media: [{ id: 0 }], total: 1 }]) assert.throws(() => M.readMarketPage(value));
});
test('facet request has same scope and combination except industry and pagination', () => {
  const f = { ...base, industry: '旅游', location: '上海', fansMin: '5000', fansMax: '10000', accountAuth: '蓝V', authorityMedia: '0', canModify: '1', sort: 'price_points:desc' };
  const list = new URLSearchParams(M.marketQuery(f, scope)); const facets = new URLSearchParams(M.marketQuery(f, scope, true));
  for (const key of ['industry', 'page', 'limit', 'sort_by', 'sort_dir']) list.delete(key);
  assert.equal(list.toString(), facets.toString()); assert.equal(facets.get('authority_media'), '0');
});
test('filter changes reset page and clear returns actual empty query', () => {
  assert.equal(M.changeMarketFilters({ ...base, page: 4 }, { location: '上海' }).page, 1);
  assert.equal(M.changeMarketFilters(base, { page: 2 }).page, 2);
  assert.equal(M.emptyMarketFilters().search, ''); assert.equal(M.emptyMarketFilters().sort, 'id:asc');
});
test('quick filters atomically synchronize draft bounds and reset only pagination', () => {
  const initial = { filters: { ...base, page: 3, location: '上海' }, draft: { ...base, pointsMin: '1', pointsMax: '2', search: '未提交名称' } };
  const next = M.changeMarketView(initial, { pointsMin: '6501', pointsMax: '13000' });
  assert.equal(next.filters.page, 1); assert.equal(next.filters.location, '上海');
  assert.equal(next.filters.pointsMin, '6501'); assert.equal(next.draft.pointsMin, '6501');
  assert.equal(next.draft.pointsMax, '13000'); assert.equal(next.draft.search, '未提交名称');
  const searched = M.changeMarketView(next, { search: next.draft.search });
  assert.equal(searched.filters.pointsMin, '6501'); assert.equal(searched.filters.pointsMax, '13000');
  assert.deepEqual(M.changeMarketView(searched, { pointsMin: '', pointsMax: '' }).draft.pointsMin, '');
  assert.equal(initial.filters.page, 3); assert.equal(initial.draft.pointsMax, '2');
});
test('preset ranges cover their integer boundaries once, in server points and actual followers', () => {
  for (const presets of [M.MARKET_POINT_PRESETS, M.MARKET_FAN_PRESETS]) {
    let previousMax = 0;
    for (const [index, preset] of presets.entries()) {
      const min = Number(preset.min || 0), max = preset.max ? Number(preset.max) : Infinity;
      if (index > 0) assert.equal(min, previousMax + 1);
      assert.ok(min <= max); previousMax = max;
      assert.equal(presets.filter(p => min >= Number(p.min || 0) && min <= Number(p.max || Infinity)).length, 1);
    }
    assert.equal(previousMax, Infinity);
  }
  assert.deepEqual(M.MARKET_POINT_PRESETS.map(p => [p.min, p.max]), [['', '6500'], ['6501', '13000'], ['13001', '26000'], ['26001', '']]);
  const q = new URLSearchParams(M.marketQuery({ ...base, pointsMin: M.MARKET_POINT_PRESETS[1].min, pointsMax: M.MARKET_POINT_PRESETS[1].max }, scope));
  assert.equal(q.get('points_min'), '6501'); assert.equal(q.get('points_max'), '13000');
});
test('individual active-filter removal clears a range pair without losing other conditions or account choice', () => {
  const filters = { ...base, page: 3, industry: '旅游', pointsMin: '6501', pointsMax: '13000', fansMin: '1001', search: '酒店' };
  const conditions = M.marketActiveFilters(filters, scope);
  const price = conditions.find(c => c.key === 'points');
  assert.deepEqual(price.clear, { pointsMin: '', pointsMax: '' });
  const result = M.changeMarketView({ filters, draft: filters }, price.clear);
  assert.equal(result.filters.page, 1); assert.equal(result.filters.industry, '旅游'); assert.equal(result.filters.fansMin, '1001');
  assert.equal(result.filters.pointsMax, ''); assert.equal(result.draft.pointsMax, '');
  assert.equal(M.marketActiveFilters(base, scope).length, 0);
  assert.deepEqual(M.chooseMarketAccount([], account, false), [account]);
});
test('real category row exposes server categories, counts, current choice and expansion without an input', () => {
  const choices = Array.from({ length: 12 }, (_, i) => ({ value: `行业${i}`, label: `行业${i}`, count: i + 1 }));
  const props = { label: '行业', choices, value: '行业11', onChange() {}, expanded: false, onExpandedChange() {} };
  const html = render(UI.MarketFilterRow, props);
  assert.match(html, /aria-label="行业筛选"/); assert.match(html, /行业0/); assert.match(html, /行业11/);
  assert.doesNotMatch(html, /行业10|<input|<select/); assert.match(html, /aria-pressed="true"/);
  assert.match(html, /展开全部/); assert.match(html, /min-h-11/); assert.match(html, /flex-wrap/);
  assert.match(render(UI.MarketFilterRow, { ...props, expanded: true }), /行业10/);
  const absent = render(UI.MarketFilterRow, { ...props, value: '暂缺的分类', choices: [] });
  assert.match(absent, /暂缺的分类/); assert.doesNotMatch(absent, /暂缺的分类[^<]*0/);
});
test('real category button immediately calls the filter action and selected button cancels itself', () => {
  const changes = [];
  const elements = [UI.MarketFilterRow({ label: '地区', choices: [{ value: '上海', label: '上海' }], value: '上海', onChange: v => changes.push(v), expanded: false, onExpandedChange() {} })];
  while (elements.length) {
    const element = elements.pop();
    if (!element?.props) continue;
    if (element.props['aria-label'] === '地区：上海') element.props.onClick();
    elements.push(...[element.props.children].flat(3));
  }
  assert.deepEqual(changes, ['']);
});
test('range validation accepts points and rejects unclear malformed ranges', () => {
  assert.equal(M.marketRangeError({ ...base, pointsMin: '260', pointsMax: '390' }), '');
  for (const pointsMin of ['12.5', '-1', 'NaN']) assert.ok(M.marketRangeError({ ...base, pointsMin }));
  assert.ok(M.marketRangeError({ ...base, fansMin: '100', fansMax: '20' }));
});
test('single/multi choice and lock exclude false retry selections', () => {
  const b = { ...account, id: 72 };
  assert.deepEqual(M.chooseMarketAccount([account], b, false), [b]);
  assert.deepEqual(M.chooseMarketAccount([account], b, true), [account, b]);
  assert.deepEqual(M.chooseMarketAccount([account], b, false, true), [account]);
  assert.deepEqual(M.chooseMarketAccount([account], b, false, false, [72]), [account]);
  assert.deepEqual(M.chooseMarketAccount([account], account, true), []);
});
test('paging/filtering never rebuilds caller selection from visible rows', () => {
  const selected = M.chooseMarketAccount([], account, false);
  const newPage = M.changeMarketFilters(base, { page: 2, location: '北京' });
  assert.equal(newPage.page, 2); assert.deepEqual(selected, [account]);
  const html = render(UI.ShortVideoAccountMarket, { scope, selected, onChange() {} });
  assert.match(html, /account-market-selection/); assert.match(html, /目录真实账号/); assert.match(html, /260 算力/);
});
test('actual shared component renders common and more filters, not clipped cards', () => {
  const html = render(UI.ShortVideoAccountMarket, { scope, selected: [], onChange() {} });
  for (const label of ['行业', '地区', '价格（算力）', '粉丝数', '账号认证', '官方媒体', '内容修改', '排序', '搜索账号', '清空筛选']) assert.ok(html.includes(label), label);
  assert.match(html, /<details/); assert.doesNotMatch(html, /max-h-\[92px\]|inp-account-chips/);
  assert.doesNotMatch(html, /aria-label="平台筛选"/); assert.match(html, /账号名称（选填）/);
});
test('actual table has details, semantic radio, selected and excluded states', () => {
  const html = render(UI.AccountMarketResults, { rows: [account, { ...account, id: 72 }], selected: [account], excludedIds: [72], onChange() {} });
  assert.equal((html.match(/type="radio"/g) || []).length, 2);
  assert.equal((html.match(/checked=""/g) || []).length, 1); assert.equal((html.match(/disabled=""/g) || []).length, 1);
  for (const value of ['上海', '旅游', '平均点赞', '333', '蓝V', '官方媒体', '可改稿', '账号原始备注', '待确认']) assert.ok(html.includes(value), value);
  assert.doesNotMatch(html, />33384</); assert.match(html, /overflow-auto/); assert.match(html, /max-h-\[28rem\]/); assert.match(html, /sticky top-0/); assert.match(html, /min-h-11/);
});
test('video uses same results with checkboxes and preserved priced selections', () => {
  const html = render(UI.AccountMarketResults, { rows: [account], selected: [account], multiple: true, onChange() {} });
  assert.match(html, /type="checkbox"/); assert.match(html, /checked=""/);
});
test('unknown price and duration do not pretend free or seconds', () => {
  const html = render(UI.AccountMarketResults, { rows: [{ ...account, price_points: null, avg_publish_time: '' }], selected: [], onChange() {} });
  assert.match(html, /待询价/); assert.match(html, /暂无数据/); assert.doesNotMatch(html, />0 算力</);
});
test('locked receipt summary stays visible without current-page account', () => {
  const html = render(UI.ShortVideoAccountMarket, { scope, selected: [account], locked: true, onChange() {} });
  assert.match(html, /本次提交账号/); assert.match(html, /目录真实账号/); assert.doesNotMatch(html, /移除 目录真实账号/);
});
test('receipt name prefers server snapshot and only same-account local hint, never an invented id name', () => {
  assert.equal(M.receiptAccountName({ media_id: 71, media_name: '服务端账号' }, account), '服务端账号');
  assert.equal(M.receiptAccountName({ media_id: 71, media_name: '' }, account), account.media_name);
  assert.equal(M.receiptAccountName({ media_id: 72 }, account), '发布账号资料暂未读取');
  assert.equal(M.receiptAccountName({ media_id: 71 }, null), '发布账号资料暂未读取');
});
test('actual no-cache publication view has status and working history exit, not another pay action', () => {
  const html = render(Receipt.ImageNotePublishInline, { brandId: 3, postId: 241, revisionId: 12, keyword: '酒店', publicationAllowed: false, publicationStatusLabel: '发布中' });
  assert.match(html, /发布中/); assert.match(html, /mode=history&amp;brand_id=3/);
  assert.match(html, /查看发布记录/); assert.match(html, /short-video-account-market/);
  assert.doesNotMatch(html, /data-testid="inp-publish"/);
  assert.ok(html.indexOf('inp-record-fallback') < html.indexOf('short-video-account-market'));
});
test('real content row selects one work without embedding a market or pay action', () => {
  const row = Status.publishRow({ id: 9, status: 'ready', active_revision_id: 4, oss_keys: ['a'], title: '准备好的图文', body_text: '真实正文', publish_status: '' });
  const html = render(List.ImageNoteContentRow, { row, selected: true, onSelect() {} });
  assert.match(html, /aria-pressed="true"/); assert.match(html, /准备好的图文/); assert.match(html, /真实正文/);
  assert.match(html, /\/writing\/image-note\/9/);
  assert.doesNotMatch(html, /short-video-account-market|inp-inline|data-testid="inp-publish"|aria-expanded/);
});
test('real failed and pending rows retain authoritative reasons and no made-up success', () => {
  const failed = Status.publishRow({ id: 9, publish_status: 'failed', publish_failure_reason: '真实拒稿原因' });
  const html = render(List.ImageNoteContentRow, { row: failed, selected: false, onSelect() {} });
  assert.match(html, /真实拒稿原因/); assert.match(html, /aria-pressed="false"/);
  const inflight = render(List.ImageNoteContentRow, { row: Status.publishRow({ id: 9, publish_status: 'self_reported_unverified', published_url: 'https://should-not-appear.test' }), selected: false, onSelect() {} });
  assert.match(inflight, /待核实/); assert.doesNotMatch(inflight, /should-not-appear|已发布/);
});
test('missing publication reason directs users to the selected work receipt, not a retired page', () => {
  const row = Status.publishRow({ id: 9, publish_status: 'failed', publish_failure_reason: '', failure_reason: '制作超时' });
  const html = render(List.ImageNoteContentRow, { row, selected: false, onSelect() {} });
  assert.match(html, /选择这条图文/); assert.match(html, /发布回执和下一步处理/);
  assert.doesNotMatch(html, /供应商|去校对页|制作超时/);
});
test('real two-pane layout keeps account pane separate from content on both mobile steps', () => {
  for (const mobilePane of ['content', 'accounts']) {
    const html = render(List.ImageNotePublishingLayout, { mobilePane, onPaneChange() {}, content: 'CONTENT_SENTINEL', publisher: 'MARKET_SENTINEL' });
    assert.equal((html.match(/pub-imagenote-content-pane/g) || []).length, 1);
    assert.equal((html.match(/pub-imagenote-account-pane/g) || []).length, 1);
    assert.ok(html.indexOf('CONTENT_SENTINEL') < html.indexOf('</section><section'));
    assert.ok(html.indexOf('MARKET_SENTINEL') > html.indexOf('</section><section'));
    assert.match(html, /lg:flex-row/); assert.match(html, /min-w-0/); assert.match(html, /2 选账号与发布/);
  }
});
test('empty customer still renders a browsable market in the right pane', () => {
  const html = render(List.ImageNoteList, { brandId: null });
  assert.match(html, /pub-imagenote-no-client/); assert.match(html, /short-video-account-market/);
  assert.match(html, /先在左侧选择图文/); assert.doesNotMatch(html, /data-testid="inp-publish"/);
});
test('SSR publishers for different works consume the same lifted filter props without a payable selection', () => {
  const viewState = UI.newMarketViewState();
  viewState.filters = { ...viewState.filters, industry: '酒店', location: '上海', page: 3 };
  viewState.draft = { ...viewState.draft, search: '保留搜索', pointsMin: '260' };
  for (const postId of [9, 12]) {
    const html = render(Receipt.ImageNotePublishInline, { brandId: 3, postId, revisionId: 4, keyword: '酒店', viewState, setViewState() {} });
    assert.match(html, /aria-label="行业：酒店" aria-pressed="true"/); assert.match(html, /aria-label="地区：上海" aria-pressed="true"/);
    assert.match(html, /value="保留搜索"/); assert.match(html, /value="260"/);
    assert.match(html, /inp-publish-footer/); assert.match(html, /sticky bottom-0/);
    assert.match(html, /先选一个要发到的账号/); assert.doesNotMatch(html, /发布 · \d+ 算力/);
  }
  assert.equal(viewState.filters.page, 3);
});
test('both markets expose range presets before optional custom fields and search', () => {
  for (const imageNote of [true, false]) {
    const html = render(UI.ShortVideoAccountMarket, { scope: { imageNote }, selected: [], onChange() {} });
    const detailsStart = html.indexOf('<details'), detailsEnd = html.indexOf('</details>');
    const price = html.indexOf('aria-label="价格（算力）最低"');
    assert.ok(detailsStart > 0 && detailsEnd > detailsStart && price > 0);
    assert.ok(price > detailsStart && price < detailsEnd);
    assert.ok(html.indexOf('6,501–13,000') < detailsStart);
    assert.ok(html.indexOf('1,001–5,000') < detailsStart);
    assert.match(html, /更多条件与自定义范围/); assert.match(html, /账号名称（选填）/);
    assert.ok(html.indexOf('inp-account-search') > html.indexOf('aria-label="价格（算力）筛选"'));
    assert.equal((html.match(/aria-label="价格（算力）最低"/g) || []).length, 1);
    assert.match(html, /aria-label="排序"/); assert.match(html, /默认排序/);
  }
});
test('deep-link error notice displays the server-independent explanation and executes retry callback', () => {
  let retries = 0;
  const props = { loading: false, error: '这条图文暂时打不开，请重试或返回编辑页。', onRetry: () => retries++ };
  const html = render(List.ImageNoteFocusNotice, props);
  assert.match(html, /role="alert"/); assert.match(html, /这条图文暂时打不开/); assert.match(html, /重新读取/);
  const pending = [List.ImageNoteFocusNotice(props)];
  while (pending.length) {
    const element = pending.pop();
    if (!element?.props) continue;
    if (typeof element.props.onClick === 'function') element.props.onClick();
    const children = element.props.children;
    pending.push(...(Array.isArray(children) ? children.flat() : [children]));
  }
  assert.equal(retries, 1);
  assert.doesNotMatch(render(List.ImageNoteFocusNotice, { ...props, error: '' }), /role="alert"|重新读取/);
});
console.log(`short-video-market: ${passed} passed`);
