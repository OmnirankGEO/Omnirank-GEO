/** Real pure-function contracts; no browser, database, network or source changes. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const ts = require('typescript');
export async function loadTs(path, transform = value => value) {
  const source = transform(readFileSync(new URL(`../${path}`, import.meta.url), 'utf8'));
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
}
const F = await loadTs('src/pages/Writing/imageNoteFlow.ts');
const P = await loadTs('src/pages/Writing/imageNoteProduction.ts');
const T = await loadTs('src/pages/Writing/imageNoteTopics.ts');
const S = await loadTs('src/pages/Publishing/imageNotePublishScope.ts');
const R = await loadTs('src/pages/Publishing/imageNotePublishStatus.ts');
let passed = 0;
function test(name, fn) { fn(); ++passed; console.log(`PASS ${name}`); }
for (const type of ['imagenote', 'svideo']) for (const step of ['article', 'media']) test(`${type}/${step} no article dependency`, () => {
  assert.deepEqual(F.publicationLayout('proxy', type, step), { articlePane: false, mediaVisible: true, articleCart: false });
});
test('article flow still has article pane and cart', () => assert.deepEqual(F.publicationLayout('proxy', 'soft', 'article'), { articlePane: true, mediaVisible: false, articleCart: true }));
// [WO_273] 原格「self mode still has article pane」肯定式退役:自助发布整档退役,接替者 = 下一格
//   (文章栏只属于代发;退役的 self 与其余非代发模式一律没有文章栏)。
test('only proxy mode has an article pane (self-publish retired, WO_273)', () => {
  for (const mode of ['self', 'history', 'advisor', 'placement', 'manual']) for (const type of ['soft', 'imagenote']) {
    assert.equal(F.publicationLayout(mode, type, 'article').articlePane, false, `${mode}/${type}`);
  }
});
test('deep link restores all three identifiers', () => {
  const url = new URL(F.imageNotePublishHref(73, 17), 'http://localhost');
  assert.equal(url.pathname, '/publish');
  assert.deepEqual(Object.fromEntries(url.searchParams), { media_type: 'svideo', content_type: 'imagenote', geo_post_id: '73', brand_id: '17' });
});
test('legacy image-note and original short-video post links land in same image lane', () => {
  for (const query of ['media_type=imagenote', 'media_type=svideo&geo_post_id=73', 'geo_post_id=73', 'media_type=svideo&content_type=imagenote']) {
    assert.deepEqual(F.publicationEntry(new URLSearchParams(query)), {type:'svideo',content:'imagenote'});
  }
  assert.deepEqual(F.publicationEntry(new URLSearchParams('media_type=svideo&content_type=video')), {type:'svideo',content:'video'});
  assert.deepEqual(F.publicationEntry(new URLSearchParams('media_type=svideo')), {type:'svideo',content:'video'});
  assert.deepEqual(F.publicationEntry(new URLSearchParams('media_type=svideo&content_type=invalid')), {type:'svideo',content:'video'});
  assert.equal(F.publicationEntry(new URLSearchParams('media_type=wemedia')).type, 'wemedia');
});
test('existing work step two retains current object and does not navigate to creation', () => {
  assert.deepEqual(F.existingWorkStep(2), {stage:2});
  assert.deepEqual(F.existingWorkStep(3), {stage:3});
  assert.deepEqual(F.existingWorkStep(1), {href:'/writing/image-note'});
});
test('repeated effect before target context arrives preserves the handoff', () => {
  const first = F.advanceClientHandoff(null,'3::418',3,4);
  assert.equal(first.action,'switch'); assert.equal(first.state.phase,'pending');
  for (const current of [4,4,null,4]) {
    const repeated = F.advanceClientHandoff(first.state,'3::418',3,current);
    assert.equal(repeated.action,'none'); assert.equal(repeated.state.phase,'pending');
  }
});
test('target must actually be observed before later manual switch clears the old link', () => {
  const pending = F.advanceClientHandoff(null,'3::418',3,4).state;
  const observed = F.advanceClientHandoff(pending,'3::418',3,3);
  assert.equal(observed.action,'none'); assert.equal(observed.state.phase,'observed');
  const changed = F.advanceClientHandoff(observed.state,'3::418',3,4);
  assert.equal(changed.action,'clear'); assert.equal(changed.state.phase,'released');
  const repeatedBeforeUrlCommits = F.advanceClientHandoff(changed.state,'3::418',3,4);
  assert.equal(repeatedBeforeUrlCommits.action,'none');
  assert.equal(F.advanceClientHandoff(changed.state,'',null,4).state,null);
});
test('same-client initial link and changed handoff keys retain proper ownership', () => {
  const ready = F.advanceClientHandoff(null,'3::418',3,3);
  assert.equal(ready.state.phase,'observed'); assert.equal(ready.action,'none');
  assert.equal(F.advanceClientHandoff(ready.state,'3::418',3,null).action,'clear');
  const newer = F.advanceClientHandoff(ready.state,'4::4',4,3);
  assert.equal(newer.action,'switch'); assert.equal(newer.state.brandId,4);
  assert.equal(F.advanceClientHandoff(null,'::418',null,4).action,'none');
});
test('production-incomplete work never offers publication or masquerades as old backfill', () => {
  const row = R.publishRow({id:3,status:'generating',publish_status:'',active_revision_id:1,oss_keys:['image']});
  assert.equal(row.bucket, 'incomplete'); assert.equal(R.canPublishRow(row), false);
  assert.match(R.rowBlockedReason(row), /尚未制作完成/);
  assert.equal(R.blockedAllNotice([row]), '');
});
test('submitted and published complete versions do not claim system backfill', () => {
  for (const status of ['publishing','published','self_reported_unverified']) {
    const row = R.publishRow({id:3,status:'ready',publish_status:status,active_revision_id:1,oss_keys:['image']});
    assert.equal(R.rowBlockedReason(row), ''); assert.equal(R.blockedAllNotice([row]), '');
  }
});
test('ready work needs actual delivered assets before being pending', () => {
  for (const keys of [[], ['ok',''], [null], [false]]) {
    const row = R.publishRow({id:3,status:'ready',publish_status:'',active_revision_id:1,oss_keys:keys});
    assert.equal(row.bucket,'incomplete'); assert.equal(R.canPublishRow(row),false);
  }
  const row = R.publishRow({id:3,status:'ready',publish_status:'',active_revision_id:1,oss_keys:['ok']});
  assert.equal(row.bucket,'unpublished'); assert.equal(R.canPublishRow(row),true);
});
const PT = await loadTs('src/pages/Publishing/publicationTime.ts');
test('confirmed seconds are durations with small seconds and long days', () => {
  for (const [input,expected] of [[1,'1秒'],[59,'59秒'],[116,'1分钟56秒'],[33384,'9小时16分钟'],[86400,'1天']]) {
    assert.equal(PT.publicationTime(input,'seconds'),expected);
  }
});
test('unknown numeric duration unit is disclosed; descriptive text stays unchanged', () => {
  assert.match(PT.publicationTime('33384'), /未提供单位/);
  for (const value of ['1-3个工作日','30分钟','当天出稿']) assert.equal(PT.publicationTime(value),value);
  for (const value of [null,'',0,-2,NaN,Infinity,'NaN']) assert.equal(PT.publicationTime(value,'seconds'),'暂无数据');
});
test('handoff expands the first visit', () => assert.equal(F.expandedAfterHandoff('', '3:5', null, 5), 5));
test('background refresh retains another open work', () => assert.equal(F.expandedAfterHandoff('3:5', '3:5', 3, 5), 3));
test('background refresh respects explicitly closed work', () => assert.equal(F.expandedAfterHandoff('3:5', '3:5', null, 5), null));
test('new client or handoff starts its own focus', () => assert.equal(F.expandedAfterHandoff('3:5', '4:6', 3, 6), 6));
for (const bad of [null, '', '3oops', -1, 1.2, true, '0', Number.MAX_SAFE_INTEGER + 1]) test(`invalid post identity ${bad}`, () => assert.equal(F.positiveId(bad), null));
test('wrong client cannot become current post', () => assert.equal(F.scopedPost({ id: 3, brand_id: 9 }, 8), null));
test('authoritative post accepted only in current client', () => assert.equal(F.scopedPost({ id: 3, brand_id: 8 }, 8)?.id, 3));
test('dirty comparison detects title/body/tags individually', () => {
  const copy = { title: '标题', body: '正文', tags: ['标签'] };
  for (const next of [{ ...copy, title: '' }, { ...copy, body: '' }, { ...copy, tags: [] }]) assert.equal(F.sameCopy(copy, next), false);
  assert.equal(F.sameCopy(copy, { ...copy, tags: [...copy.tags] }), true);
});
test('save/readback collision never confirms another editor version', () => {
  assert.equal(F.matchesSavedRevision({ active_revision_id: 9 }, { active_revision_id: 9 }), true);
  assert.equal(F.matchesSavedRevision({ active_revision_id: 9 }, { active_revision_id: 10 }), false);
  assert.equal(F.matchesSavedRevision({}, { active_revision_id: 10 }), false);
  assert.equal(F.matchesSavedRevision({ active_revision_id: null }, { active_revision_id: 10 }), false);
  assert.equal(F.matchesSavedRevision({ active_revision_id: null }, { active_revision_id: null }), true);
});
const rows = [{ id: 77, keyword: '商务酒店', city: '揭阳', styleKey: 'editorial', confirmedKeywordId: 9 }];
const settings = { cardCount: 4, styleKey: 'minimal', aspectRatio: '3:4', industry: '酒店' };
const quote = { total_points: 390, lines: [{ price_fingerprint: 'server-fingerprint' }] };
test('production quote uses actual lines DTO', () => assert.deepEqual(P.productionLines(rows, settings), [{ keyword: '商务酒店', city: '揭阳', card_count: 4 }]));
test('batch carries real creation fields and per-line fingerprint', () => assert.deepEqual(P.productionBatch(3, rows, settings, quote, 'same-request'), {
  request_id: 'same-request', items: [{ keyword: '商务酒店', city: '揭阳', card_count: 4, brand_id: 3, topic_id: 77,
    industry_key: '酒店', style_key: 'minimal', aspect_ratio: '3:4', confirmed_keyword_id: 9, expected_price_fingerprint: 'server-fingerprint' }]
}));
test('same retry produces identical request identity', () => assert.deepEqual(P.productionBatch(3, rows, settings, quote, 'same-request'), P.productionBatch(3, rows, settings, quote, 'same-request')));
for (const bad of [[], Array(21).fill(rows[0])]) test(`invalid batch denominator ${bad.length}`, () => assert.throws(() => P.productionBatch(3, bad, settings, quote, 'r')));
test('missing price fingerprint does not construct payable batch', () => assert.throws(() => P.productionBatch(3, rows, settings, { ...quote, lines: [{}] }, 'r')));
test('server zero price is not confused with missing price', () => {
  assert.equal(T.canStart({ selectedCount: 1, points: 0, busy: false }), true);
  assert.equal(T.canStart({ selectedCount: 1, points: null, busy: false }), false);
});
const input = { chosen: [{ postId: 3, revisionId: 8, mediaId: 99, itemRequestId: 'request-id' }],
  previewItems: [{ item_request_id: 'request-id', publish_price_fingerprint: 'publish-fp' }] };
for (const id of [12, '12']) test(`actual prepared artifact DTO ${typeof id}`, () => {
  const items = S.buildBatchItems({ ...input, artifacts: { 3: { prepared_artifact_id: id, manifest_hash: 'hash', state: 'ready' } } });
  assert.equal(items.length, 1); assert.equal(items[0].prepared_artifact_id, '12');
  assert.deepEqual(Object.keys(items[0]).sort(), [...S.BATCH_ITEM_KEYS].sort());
});
for (const id of [null, false, 0, -1, 1.5, '', 'bad', '12.0', Number.MAX_SAFE_INTEGER + 1]) test(`invalid artifact identity ${id}`, () => assert.equal(S.artifactIdentity(id), ''));
test('current publication row shows actual saved copy', () => {
  const row = R.publishRow({ id: 3, title: '保存后的标题', body_text: '保存后的正文', keyword: '旧关键词' });
  assert.equal(row.title, '保存后的标题'); assert.equal(row.body, '保存后的正文'); assert.equal(row.keyword, '旧关键词');
});
test('accepted/processing are not claims of publication', () => {
  for (const state of ['accepted', 'processing']) {
    assert.match(S.publishCommandLabel(state), /已提交.*等待/);
    assert.doesNotMatch(S.publishCommandLabel(state), /已发布|成功/);
    assert.equal(S.isCommandTerminal(state), false);
  }
});
test('only server-confirmed released failure may choose another account', () => {
  const command = { command_status: 'failed', items: [{ state: 'rejected', media_id: 99, retryable: true }] };
  assert.deepEqual(S.failedCommandAccounts(command), [99]);
  for (const state of ['accepted', 'processing', 'unknown', 'succeeded']) assert.deepEqual(S.failedCommandAccounts({ ...command, command_status: state }), []);
  assert.deepEqual(S.failedCommandAccounts({ ...command, items: [{ ...command.items[0], retryable: false }] }), []);
  assert.deepEqual(S.failedCommandAccounts({ ...command, items: [{ ...command.items[0], state: 'submitted' }] }), []);
  assert.deepEqual(S.failedCommandAccounts({ ...command, items: [] }), []);
});
const API = await loadTs('src/lib/imageNoteApi.ts', source => source.replace(
  "import { authFetch } from '@/lib/api';",
  `const authFetch = async () => ({ok:false,status:409,json:async()=>({detail:{
    code:'PUBLISH_ATTEMPT_EXISTS',message:'查看提交结果',command_id:'pubcmd_existing',retryable:false
  }})});`));
const existingError = await API.submitPublish({request_id:'r',expected_total_price_points:260,items:[]}).catch(error => error);
test('real error DTO preserves existing command for duplicate-submit recovery', () => {
  assert.ok(existingError instanceof API.ImageNoteError);
  assert.equal(existingError.contract.command_id, 'pubcmd_existing');
  assert.equal(existingError.httpStatus, 409);
  assert.equal(existingError.contract.retryable, false);
});
const kw = { confirmed_keyword_id: 7, quote_id: 4, keyword: '同词', required_articles: 1,
  quote_status: 'paid', production_status: 'pending', produced_count: 0 };
test('paid and confirmed same-text inputs keep independent identities', () => {
  const a = T.keywordRow(kw), b = T.keywordRow({ ...kw, confirmed_keyword_id: 11, quote_id: 5, quote_status: 'confirmed' });
  assert.equal(a.originLabel, '已购关键词'); assert.equal(b.originLabel, '已确认关键词');
  assert.equal(a.keyword, b.keyword); assert.notEqual(a.id, b.id);
  assert.equal(T.topicBuckets([a, b]).pending.length, 2);
});
test('no paid title step required and negative UI keys never become topic IDs', () => {
  const row = T.keywordRow(kw);
  const item = P.productionBatch(3, [row], settings, quote, 'one-batch').items[0];
  assert.equal(item.confirmed_keyword_id, 7); assert.equal(item.keyword, '同词');
  assert.equal('topic_id' in item, false);
});
test('editing a pending angle preserves original keyword and actual topic identity', () => {
  const row = T.keywordRow({ ...kw, topic_id: 88, topic_title: '新标题' });
  const item = P.productionBatch(3, [row], settings, quote, 'one-batch').items[0];
  assert.equal(item.confirmed_keyword_id, 7); assert.equal(item.keyword, '同词'); assert.equal(item.topic_id, 88);
});
test('zero allocation never pretends a purchased unfinished deliverable', () => {
  const row = T.keywordRow({ ...kw, required_articles: 0 });
  assert.equal(row.statusLabel, '未安排内容'); assert.equal(row.selectable, false);
});
for (const status of ['making', 'done', 'failed', 'incomplete', 'something_new']) test(`existing ${status} work continues without new production selection`, () => {
  const row = T.keywordRow({ ...kw, production_status: status, post_id: 19 });
  assert.equal(row.postId, 19); assert.equal(row.selectable, false);
});
test('bare draft is incomplete, not an endless running job', () => {
  const row = T.keywordRow({ ...kw, production_status: 'incomplete', post_status: 'draft', post_id: 19 });
  assert.equal(row.statusLabel, '尚未完成'); assert.equal(T.topicBuckets([row]).making.length, 0);
});
test('card selection and unselection uses one immutable bounded transition', () => {
  const empty = new Set(); const selected = T.toggleTopicSelection(empty, -7);
  assert.equal(empty.size, 0); assert.equal(selected.has(-7), true);
  assert.equal(T.toggleTopicSelection(selected, -7).size, 0);
  const full = new Set(Array.from({ length: 20 }, (_, i) => i + 1));
  assert.equal(T.toggleTopicSelection(full, -7).size, 20);
  assert.equal(T.toggleTopicSelection(full, 1).size, 19);
});
test('client/source/page/stale response cannot replace the current worklist', () => {
  assert.equal(T.currentTopicRead('3:keywords:0', '3:keywords:0', 2, 2), true);
  for (const active of ['4:keywords:0', '3:ideas:0', '3:keywords:1']) assert.equal(T.currentTopicRead('3:keywords:0', active, 2, 2), false);
  assert.equal(T.currentTopicRead('3:keywords:0', '3:keywords:0', 1, 2), false);
  assert.equal(T.currentTopicRead('3:keywords:0', '3:keywords:0', 2, 2, true), false);
});
test('Douyin shortcut scopes the server catalog before pagination and search', () => {
  const params = new URLSearchParams(S.accountCatalogQuery(3, ' 酒店 ', '微博', '抖音'));
  assert.deepEqual(Object.fromEntries(params), { can_tuwen: '1', limit: '50', page: '3', search: '酒店', platform: '抖音' });
  assert.equal(new URLSearchParams(S.accountCatalogQuery(1, '', '微博')).get('platform'), '微博');
  assert.equal(new URLSearchParams(S.accountCatalogQuery(1, '', '')).has('platform'), false);
});
test('platform eligibility uses real platform field, never a name containing Douyin', () => {
  const rows = [{ id: 1, platform: '抖音', media_name: '真实账号', can_tuwen: 1, is_active: true },
    { id: 2, platform: '微博', media_name: '抖音官方字样不算平台', can_tuwen: 1, is_active: true },
    { id: 3, platform: '抖音', media_name: '只能视频', can_tuwen: 0, is_active: true }];
  assert.deepEqual(S.accountsForPlatform(rows, '抖音').map(a => a.mediaId), [1]);
  assert.deepEqual(S.accountsForPlatform(rows).map(a => a.mediaId), [1, 2]);
});
test('command receipt restores selected account after refresh', () => {
  assert.equal(S.commandMediaId({ items: [{ media_id: 990001 }] }, 0), 990001);
  assert.equal(S.commandMediaId({ items: [{ media_id: 990002 }] }, 990001), 990002);
  assert.equal(S.commandMediaId(null, 990001), 990001);
});
test('late accepted and existing-command completions remember their original work without updating a new screen', () => {
  for (const commandId of ['accepted-command', 'already-existing-command']) {
    let current = true;
    const saved = new Map(), effects = [];
    const complete = () => F.deliverPublishCompletion({
      commandKey: 'user:3:old-work:revision', commandId,
      remember: (key, id) => saved.set(key, id), isCurrent: () => current,
      onCurrent: () => effects.push('old-onPublished'),
    });
    current = false; // The work/customer was left before this response arrived.
    assert.equal(complete(), false);
    assert.deepEqual([...saved], [['user:3:old-work:revision', commandId]]);
    assert.deepEqual(effects, []);
  }
});
test('current completion remembers receipt before invoking the live publisher callback', () => {
  const calls = [];
  assert.equal(F.deliverPublishCompletion({ commandKey: 'current-work', commandId: 'command',
    remember: (key, id) => calls.push(['remember', key, id]), isCurrent: () => true,
    onCurrent: () => calls.push(['onPublished']),
  }), true);
  assert.deepEqual(calls, [['remember', 'current-work', 'command'], ['onPublished']]);
});
test('storage failure does not block a live receipt or reactivate a departed publisher', () => {
  for (const current of [true, false]) {
    let callbacks = 0;
    const delivered = F.deliverPublishCompletion({ commandKey: 'k', commandId: 'c',
      remember: () => { throw new Error('storage disabled'); }, isCurrent: () => current,
      onCurrent: () => callbacks++,
    });
    assert.equal(delivered, current); assert.equal(callbacks, current ? 1 : 0);
  }
});
test('liveness is checked after the resume hint, not cached before it', () => {
  let current = true, callbacks = 0;
  const saved = [];
  assert.equal(F.deliverPublishCompletion({ commandKey: 'old', commandId: 'c',
    remember: (key, id) => { saved.push([key, id]); current = false; }, isCurrent: () => current,
    onCurrent: () => callbacks++,
  }), false);
  assert.deepEqual(saved, [['old', 'c']]); assert.equal(callbacks, 0);
});
/* ── 2026-09-22 制作中心跳静默刷新(Owner:「前端一直看着它闪」)── */
test('background refresh never swaps the list for the loading placeholder', () => {
  assert.deepEqual(T.topicReadPlan('refresh'), { showLoading: false, silentErrors: true });
  assert.deepEqual(T.topicReadPlan('read'), { showLoading: true, silentErrors: false });
});
test('missed heartbeats stay quiet until the third miss, then say so without hiding the list', () => {
  for (const n of [0, 1, 2, -1, NaN]) assert.equal(T.refreshNotice(n), '');
  assert.match(T.refreshNotice(3), /3 次/);
  assert.match(T.refreshNotice(7), /7 次/);
  assert.match(T.refreshNotice(3), /上次读到的内容/);
  assert.equal(T.REFRESH_STALL_AFTER, 3);
});
console.log(`image-note-flow: ${passed} passed / 0 failed / 0 skipped`);
