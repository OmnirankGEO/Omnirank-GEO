#!/usr/bin/env node
/**
 * 注毒自证 · #220 a1。
 *
 * 🔴 开跑前后各断言一次 HEAD 与 dirty 清单,逐文件 sha 回位(222 起的规矩)。
 * 🔴 毒不是从判据倒推出来的,是从**抬头承诺的每一样**倒推:
 *    「打开能看见 / 保存后不消失 / 换客户要清掉 / 选几张发几次 / 顺序 / 失败不中断 /
 *      上传中禁用 / 列表只刷一次」—— 一样一发。
 * 别与 build 并行(毒在工作树里)。
 */
import { execFileSync, execSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const abs = (rel) => join(ROOT, rel);
const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');
const say = (m) => console.log(m);
let failures = 0;

function treeState() {
    const head = execSync('git rev-parse HEAD', { cwd: REPO, encoding: 'utf8' }).trim();
    const dirty = execSync('git status --porcelain', { cwd: REPO, encoding: 'utf8' })
        .split('\n').map((s) => s.trim()).filter(Boolean).sort().join('|');
    return { head, dirty };
}
const BEFORE = treeState();
say(`开跑前 HEAD=${BEFORE.head.slice(0, 9)} · dirty ${BEFORE.dirty.split('|').filter(Boolean).length} 项`);

const GATE = 'scripts/verify-knowledge-basics-and-batch-upload.mjs';
function run() {
    try {
        const out = execFileSync(process.execPath, [GATE], { cwd: ROOT, encoding: 'utf8' });
        return { rc: 0, red: [...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]) };
    } catch (e) {
        const out = String(e.stdout || '') + String(e.stderr || '');
        return {
            rc: e.status === undefined ? -1 : e.status,
            red: [...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]),
        };
    }
}

const HALL = 'src/pages/Writing/WritingHall.tsx';
const GAL = 'src/components/brand/BrandImageGallery.tsx';

const POISONS = [
    {
        /* 🔴 这就是 Owner 报的那件事本身 */
        id: 'M1', file: HALL,
        why: '保存成功后又整体清空(退回缺陷现场)',
        from: '            resetKnowledgeInputsOnly();\n            toast.success("客户资料已保存");',
        to: '            resetKnowledgeDraft();\n            toast.success("客户资料已保存");',
        expectRed: ['K2', 'K3b'],   /* K3 看不见它:清空后打开重拉会把档案那六个值填回来(丙类) */
    },
    {
        id: 'M2', file: HALL,
        why: '打开抽屉不再从服务端取(退回「档案里有、页面上空」)',
        from: '        void refreshKnowledgeAutofill(brandId, selectedProject?.id ?? null);',
        to: '        void loadWritingBrandAssets(brandId);',
        expectRed: ['K1'],
    },
    {
        /* 🔴 反臂那一格:把「保存不清空」做过头,换客户时留着上一家的资料 */
        /* 🔴 这一处在当前导航下是**冗余守卫**:UI 上换客户必须先「返回列表」,
              而 backToList 也清 —— 所以行为判据 K4 看不见它,只有结构锁 K4a 能。
              「毒仍绿」的第四解:目标冗余。留着它 + 留着 M3b(下一发)才说得清。 */
        id: 'M3', file: HALL,
        why: '换客户时不再整体清空(冗余守卫:行为判据看不见,结构锁看得见)',
        from: '        setKnowledgeStatus(null);\n        setKnowledgeLink("");\n        resetKnowledgeDraft();\n        setKnowledgeFiles([]);\n        setDeliverySummary(null);',
        to: '        setKnowledgeStatus(null);\n        setKnowledgeLink("");\n        setKnowledgeFiles([]);\n        setDeliverySummary(null);',
        expectRed: ['K4a'],
    },
    {
        /* 🔴 M3b:打在**真正可达**的那一处 —— 行为判据必须红在这里 */
        id: 'M3b', file: HALL,
        why: '返回列表时不再整体清空(换客户会看见上一家的资料)',
        from: '        setKnowledgeLink("");\n        resetKnowledgeDraft();\n        setKnowledgeFiles([]);\n        setMaterialsDrawerOpen(false);',
        to: '        setKnowledgeLink("");\n        setKnowledgeFiles([]);\n        setMaterialsDrawerOpen(false);',
        expectRed: ['K4a'],
    },
    {
        /*
         * 🔴 M3c:**两处一起**摘掉。
         *    M3 与 M3b 各自存活在 K4 上,原因不是「锁没牙」也不是「够不着」——
         *    是这两处清空**互为冗余**:UI 上换客户必走「返回列表 → 进工作台」,
         *    任一处留着,K4 就看不出另一处没了。
         *    所以行为判据能钉住的命题是「换客户后表是干净的」,
         *    钉不住「哪一处负责把它弄干净」;后者只有结构锁 K4a 能数。
         *    两发单点毒 + 这一发双点毒放在一起,才说得清这道锁到底管什么。
         */
        id: 'M3c', file: HALL,
        why: '两处清空一起摘(换客户真能看见上一家的资料)',
        from: '        resetKnowledgeDraft();\n        setKnowledgeFiles([]);\n        setDeliverySummary(null);',
        to: '        setKnowledgeFiles([]);\n        setDeliverySummary(null);',
        from2: '        resetKnowledgeDraft();\n        setKnowledgeFiles([]);\n        setMaterialsDrawerOpen(false);',
        to2: '        setKnowledgeFiles([]);\n        setMaterialsDrawerOpen(false);',
        expectRed: ['K4', 'K4a'],
    },
    {
        /* 🔴 契约键读不到 ⇒ c2 之后那六个字段全空(而四个真列还在,所以只有 K1 会红) */
        id: 'M14', file: HALL,
        why: '不读契约键 profile.writing_basics(只认老列)',
        from: '        ? profile.writing_basics : {};',
        to: '        ? {} : {};',
        expectRed: ['K1', 'K1e'],
    },
    {
        /* 🔴 把飞轮的失败教训又接回「禁用表达」—— 文本框里会出现一串 JSON */
        id: 'M15', file: HALL,
        why: '禁用表达又回落到 negative_feedback(飞轮字典数组)',
        from: '        forbidden_notes: pick(wb.forbidden_notes, profile?.brand_constraints),',
        to: '        forbidden_notes: pick(wb.forbidden_notes, profile?.brand_constraints, profile?.negative_feedback),',
        expectRed: ['K1d'],
    },
    {
        /* 🔴 两个 JSONB 字段在 c2 前回落到形态不定的老列 */
        id: 'M16', file: HALL,
        why: '案例/口碑回落到 success_cases(形态取决于写入方)',
        from: '        proof_cases: pick(wb.proof_cases),',
        to: '        proof_cases: pick(wb.proof_cases, profile?.success_cases),',
        expectRed: ['K1c', 'K1d'],
    },
    {
        /* 🔴 契约那一层有**自己的** fillIfEmpty,K6 现在真正靠的是这一行;
              只毒老 helper(M4)钉不住它 —— 一个命题两处实现,两处都要有毒。 */
        id: 'M17', file: HALL,
        why: '契约层改成无条件覆盖(用户打的字被档案值吞掉)',
        from: '        if (!afterContract[k].trim() && v) afterContract[k] = v;',
        to: '        if (v) afterContract[k] = v;',
        expectRed: ['K6'],
    },
    {
        /* 🔴 老列排到契约键前面:c2 之后老列不再被写,用户会看到一年前的旧文案 */
        id: 'M18', file: HALL,
        why: '优先级颠倒(老列压过契约键)',
        from: '        business_summary: pick(wb.business_summary, profile?.business_summary),',
        to: '        business_summary: pick(profile?.core_value, wb.business_summary, profile?.business_summary),',
        expectRed: ['K1e'],
    },
    {
        id: 'M4', file: HALL,
        why: '合并改成无条件覆盖(用户打的字被服务端值吞掉)',
        from: '        if (nextBasics[field].trim()) return;',
        to: '        if (false) return;',
        expectRed: ['K6'],
    },
    {
        /*
         * 🔴 [a1' · 端到端逼出来的那条] 把结构化 PUT 挪回「markdown 成功之后」。
         *    真后端上 /api/knowledge/upload 回 500(那台机器向量库没起来),
         *    于是 throw 把后面全跳过 —— 六个字段一个都没落库,
         *    而用户只看到「保存失败,请重试」,重试也永远存不进去。
         *    桩恒 200 时这条看不见:32 条判据全绿。
         */
        id: 'M22', file: HALL,
        why: '结构化 PUT 挪回 markdown 成功之后(知识库一坏,基础资料也存不进)',
        from: '        await putWritingBasics();\n        try {\n            const stamp',
        to: '        try {\n            const stamp',
        from2: '            resetKnowledgeInputsOnly();\n            toast.success("客户资料已保存");',
        to2: '            await putWritingBasics();\n            resetKnowledgeInputsOnly();\n            toast.success("客户资料已保存");',
        expectRed: ['K7'],
    },
    {
        /* 🔴 只传非空 ⇒ 用户清掉某一栏再保存,服务端那栏原样留着,
              下次打开又被重拉回来 —— 在用户看来就是「这一栏删不掉」。 */
        id: 'M23', file: HALL,
        why: '只传非空字段(空串被省略 ⇒ 清不掉)',
        from: '                    proof_cases: knowledgeBasics.proof_cases,',
        to: '                    proof_cases: knowledgeBasics.proof_cases || undefined,',
        expectRed: ['K8', 'K8b'],
    },
    {
        /*
         * 🔴 [a1''' · 复审点名] 去掉「加载成功过」这道守卫。
         *    后果不是少做一件事,是**静默清空真客户档案**:
         *    重拉还在路上时表单是空的,六个空串送出去,
         *    而契约里「显式空串 = 清空」⇒ 档案里已有的四个真列被抹掉,零报错。
         */
        id: 'M25', file: HALL,
        why: '去掉「加载成功过」守卫(空表被当成用户清空 ⇒ 抹掉档案)',
        from: '        if (knowledgeBasicsLoadedKeyRef.current !== key) {',
        to: '        if (false) {',
        expectRed: ['K9', 'K9b'],
    },
    {
        /* 🔴 守卫退化成布尔:换了客户、新客户的重拉还没回来时,
              会因为**上一个客户**加载过而放行 —— 把 A 的空表写进 B 的档案。 */
        id: 'M26', file: HALL,
        why: '守卫只看「加载过没有」,不看是哪一个客户',
        from: "        const key = `${selectedProject?.brand_id ?? ''}:${selectedProject?.id ?? ''}`;",
        to: "        const key = knowledgeBasicsLoadedKeyRef.current || 'x';",
        /* 🔴 K9 在这一发下**本来就绿**:同一客户的剧本里 ref 是空串、key 变成 'x',
              两者仍不等 ⇒ 照样拦住。看得见这一面的只有换客户的 K9c。 */
        expectRed: ['K9c'],
    },
    {
        /* 🔴 「读到了 profile」才算加载成功:my-clients 非 2xx 时拿到的是
              {brand:null, profile:null} —— 一个真值,里面什么都没有。 */
        id: 'M27', file: HALL,
        why: '把「请求回来了」当成「读到了 profile」',
        from: '        if (profileBundle?.profile) {',
        to: '        if (profileBundle || true) {',
        /* 🔴 这一发**预期存活**,归因 = 目标冗余(毒仍绿四解的第四解),不是没牙:
              profile 为空时 setBrandProfile({profile:null}) 让 profileId 也为空,
              更早那条 `if (!profileId) return`(:3627)先把它拦了。
              证明这一面确实被锁住的是下面的 M27b —— 把两道冗余守卫**同时**摘掉。 */
        expectSurvive: true,
        expectRed: [],
    },
    {
        /* 🔴 双改:单发谁都活,合起来才露出这一面 —— 冗余守卫要用配对毒去证。 */
        /* 🔴 [#220 a2] 出声那条:摘掉 ⇒ 用户那头又变回一句「已保存」。
              K9d2(console 痕迹)照样绿 —— 它管的是「谁挡的」,不是「用户知不知道」。 */
        id: 'M29', file: HALL,
        why: '[a2] 拿不到 profile id 时对用户不出声(退回只写 console.warn)',
        from: "            toast.warning('客户档案还没同步好,基础资料这次没有写回档案;资料本身已保存');\n            return;\n        }\n        /*\n         * 🔴 [#220 a1''' · 复审点名]",
        to: "            return;\n        }\n        /*\n         * 🔴 [#220 a1''' · 复审点名]",
        expectRed: ['K10'],
    },
    {
        /* 🔴 [#220 a2] 无条件常驻的提示 = 每次保存都喊狼来了,用户学会无视,
              到真出事那次跟没提示一样。反臂 K10r 就是为这一发存在的。 */
        id: 'M30', file: HALL,
        why: '[a2] 把提示挪成无条件常驻(每次保存都喊)',
        from: '    const putWritingBasics = async () => {\n        const profileId = brandProfile?.profile?.id;',
        to: "    const putWritingBasics = async () => {\n        toast.warning('客户档案还没同步好,基础资料这次没有写回档案;资料本身已保存');\n        const profileId = brandProfile?.profile?.id;",
        expectRed: ['K10r'],
    },
    {
        id: 'M27b', file: HALL,
        why: '双改:把「请求回来了」当加载成功 + 摘掉 !profileId 那道冗余守卫',
        from: '        if (profileBundle?.profile) {',
        to: '        if (profileBundle || true) {',
        from2: '        const profileId = brandProfile?.profile?.id;\n        if (!profileId) {',
        to2: '        const profileId = brandProfile?.profile?.id;\n        if (false) {',
        expectRed: ['K9d', 'K9d2'],
    },
    {
        /* 🔴 「拿不到 profile id 就不发」是对的,但不能变成「永远不发」 */
        id: 'M24', file: HALL,
        why: '结构化 PUT 整个不发(退回 a1 之前的状态)',
        from: '        const profileId = brandProfile?.profile?.id;\n        if (!profileId) {',
        to: '        const profileId = brandProfile?.profile?.id;\n        if (true) {',
        expectRed: ['K7'],
    },
    {
        /* 🔴 「不清」做成「什么都不清」—— 原始资料留在框里,下次保存会重复入库 */
        id: 'M5', file: HALL,
        why: '保存后连这一次的输入也不清',
        from: '            resetKnowledgeInputsOnly();\n            toast.success("客户资料已保存");',
        to: '            toast.success("客户资料已保存");',
        expectRed: ['K2b'],
    },
    {
        /*
         * 🔴 [Review 09-16 点名的两发,连同第三发一起编进来]
         *    「一次最多 20 张」是**裁定进来的承诺**,我实现了却没给它配锁:
         *    复审把 MAX_UPLOAD_BATCH 改成 200、以及干脆不截断,26 条判据照样全绿。
         *    我原来那 20 发毒全是从**我自己的判据**倒推的,所以一发都没打到这一面。
         *    ⇒ 列毒从「抬头承诺的每一样」来,不是从「我已经写了哪些判据」来。
         */
        id: 'M19', file: GAL,
        why: '[复审 P6] 上限偷偷改成 200',
        from: 'const MAX_UPLOAD_BATCH = 20;',
        to: 'const MAX_UPLOAD_BATCH = 200;',
        expectRed: ['I8a', 'I8c'],
    },
    {
        id: 'M20', file: GAL,
        why: '[复审 P7] 干脆不截断(选多少传多少)',
        from: '    const files = picked.slice(0, MAX_UPLOAD_BATCH);',
        to: '    const files = picked;',
        expectRed: ['I8b', 'I8c'],
    },
    {
        /* 🔴 第三发是我自己补的那一面:常量**看着**没动,截断处却写死数字。
              只锁常量值的话这一发照样绿 —— 改常量不会改行为,而判据看常量是对的。 */
        id: 'M21', file: GAL,
        why: '截断处写死字面量(改常量不再改行为)',
        from: '    const files = picked.slice(0, MAX_UPLOAD_BATCH);',
        to: '    const files = picked.slice(0, 200);',
        expectRed: ['I8b', 'I8c'],
    },
    {
        id: 'M6', file: GAL,
        why: '只传第一张(退回缺陷现场)',
        from: '    const files = picked.slice(0, MAX_UPLOAD_BATCH);',
        to: '    const files = picked.slice(0, 1);',
        expectRed: ['I1', 'I1b'],
    },
    {
        id: 'M7', file: GAL,
        why: '改成并发(N 张图同时压后端的视觉识别)',
        from: '    for (let i = 0; i < files.length; i += 1) {\n      setUploadProgress({ done: i, total: files.length });\n      try {\n        const r = await uploadOne(files[i]);',
        to: '    await Promise.all(files.map(async (_f, i) => {\n      setUploadProgress({ done: i, total: files.length });\n      try {\n        const r = await uploadOne(files[i]);',
        to2: '    }));',
        from2: '    }\n\n    setUploadProgress(null);',
        expectRed: ['I2'],
    },
    {
        id: 'M8', file: GAL,
        why: '中间一张失败就中断,剩下的不传了',
        from: '      } catch (err: any) {\n        const d = err.response?.data?.detail || err.response?.data?.error;\n        failed.push(`${files[i].name}${typeof d === \'string\' ? `(${d})` : \'\'}`);\n      }',
        to: '      } catch (err: any) {\n        const d = err.response?.data?.detail || err.response?.data?.error;\n        failed.push(`${files[i].name}${typeof d === \'string\' ? `(${d})` : \'\'}`);\n        break;\n      }',
        expectRed: ['I3'],
    },
    {
        id: 'M9', file: GAL,
        why: '失败提示只说「上传失败」,不说几张、哪一张',
        from: '      toast.error(`失败 ${failed.length} 张:${failed.join(\'、\')}`);',
        to: '      toast.error(\'上传失败\');',
        expectRed: ['I3b'],
    },
    {
        id: 'M10', file: GAL,
        why: '每传一张就刷一次列表(N 次请求 + 后到覆盖先到)',
        from: '        const r = await uploadOne(files[i]);\n        okCount += 1;',
        to: '        const r = await uploadOne(files[i]);\n        fetchAssets();\n        okCount += 1;',
        expectRed: ['I5'],
    },
    {
        /* 🔴 结构毒:只改一处 input,另一处照旧 —— 分母锁要能看见 */
        id: 'M11', file: GAL,
        why: '空态那个 input 的 multiple 被摘掉(只改一处漏一处)',
        from: '          <input\n            type="file" multiple disabled={uploading}\n            accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"\n            onChange={handleUpload} className="hidden"\n          />\n          <p className="text-sm text-muted-foreground">',
        to: '          <input\n            type="file" disabled={uploading}\n            accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"\n            onChange={handleUpload} className="hidden"\n          />\n          <p className="text-sm text-muted-foreground">',
        expectRed: ['I6'],
    },
    {
        id: 'M12', file: GAL,
        why: '上传中不禁用入口(两批能交错)',
        from: '            type="file" multiple disabled={uploading}\n            accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"\n            onChange={handleUpload} className="hidden"\n          />\n          {uploading ? (',
        to: '            type="file" multiple\n            accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"\n            onChange={handleUpload} className="hidden"\n          />\n          {uploading ? (',
        expectRed: ['I7', 'I4'],
    },
    {
        id: 'M13', file: GAL,
        why: '进度不说第几张(退回「上传中...」)',
        from: "  if (!p || p.total <= 1) return '上传中...';\n  return `正在识别第 ${Math.min(p.done + 1, p.total)} / ${p.total} 张`;",
        to: "  return '上传中...';",
        expectRed: ['I4b'],
    },
];

const TOUCHED = [...new Set(POISONS.map((p) => p.file))];
const SHA0 = Object.fromEntries(TOUCHED.map((f) => [f, sha(abs(f))]));
const SRC0 = Object.fromEntries(TOUCHED.map((f) => [f, readFileSync(abs(f), 'utf8')]));
say('被碰文件开跑前 sha:');
for (const f of TOUCHED) say(`  ${f} ${SHA0[f].slice(0, 12)}`);

/* 🔴 只跑点名的几发(MR_ONLY=M25,M26,...)。
   用得住的前提:**代码零改动、门只加格不减格** —— 存在性断言单调,
   原先红的那些在更大的判据集下不可能变绿。前提不成立就必须整轮重跑。
   点名了不存在的毒 ⇒ 退 3(仪器没跑成),不是静默跳过。 */
const ONLY = (process.env.MR_ONLY || '').split(',').map((s) => s.trim()).filter(Boolean);
const bad = ONLY.filter((id) => !POISONS.some((p) => p.id === id));
if (bad.length) { say(`🔴 MR_ONLY 点名了不存在的毒:${bad.join(',')}`); process.exit(3); }
const RUN = ONLY.length ? POISONS.filter((p) => ONLY.includes(p.id)) : POISONS;
if (ONLY.length) {
    say(`\n🔴 本轮只跑点名的 ${RUN.length} 发:${RUN.map((p) => p.id).join(',')}`
        + `(全表 ${POISONS.length} 发,其余沿用上一轮读数)`);
}

say('\n=== 0. 基线(失败集必须为空,且 rc 必须是 0 不是 3) ===');
const base = run();
const clean = base.rc === 0 && base.red.length === 0;
if (!clean) failures += 1;
say(`  ${clean ? 'OK  ' : 'FAIL'} rc=${base.rc} 失败集=${base.red.join(',') || '空'}`);
if (!clean) {
    say(base.rc === 3
        ? '\n🔴 基线 rc=3 —— 门自己没跑成,再注毒等于往一个没开机的仪器上下毒。'
        : '\n🔴 基线不干净 —— 红基线会让每发毒都像命中,不往下跑。');
    process.exit(1);
}
const baseSet = new Set(base.red);

/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(abs(RUN[0].file), say);

for (const p of RUN) {
    say(`\n=== ${p.id} ${p.why} ===`);
    let src = SRC0[p.file];
    const n = src.split(p.from).length - 1;
    if (n !== 1) {
        say(`  FAIL 毒没下成:${p.file} 的锚命中 ${n} 次(要恰好 1 次)—— 不是"锁没牙"`);
        failures += 1;
        continue;
    }
    src = src.replace(p.from, p.to);
    if (p.from2) {
        const n2 = src.split(p.from2).length - 1;
        if (n2 !== 1) {
            say(`  FAIL 毒没下成:第二处锚命中 ${n2} 次`);
            failures += 1;
            continue;
        }
        src = src.replace(p.from2, p.to2);
    }
    try { assertRulerWorks(abs(p.file)); } catch (err) { say(`  ${err.message}`); process.exit(3); }
    writeFileSync(abs(p.file), src, 'utf8');
    if (!syntaxOk(abs(p.file))) {
        say(`  FAIL ${NOT_LANDED_SYNTAX}`);
        failures += 1;
        writeFileSync(abs(p.file), SRC0[p.file], 'utf8');
        continue;
    }
    say(`  毒已落地(${p.from2 ? 2 : 1} 处改动)`);
    const r = run();
    const missing = p.expectRed.filter((w) => !r.red.includes(w));
    const fresh = r.red.filter((x) => !baseSet.has(x));
    /* 🔴 rc=3 不算命中:那是门断了,不是锁咬住了 */
    /* 🔴 「预期存活」必须写死在毒表里,不能跑出来再解释:
          否则每一发存活都能事后编一个「冗余」出来。 */
    const good = p.expectSurvive
        ? (r.rc === 0 && fresh.length === 0)
        : (r.rc === 1 && missing.length === 0);
    if (!good) failures += 1;
    if (p.expectSurvive) {
        say(`  ${good ? 'OK  ' : 'FAIL'} rc=${r.rc} 预期**存活**(目标冗余)`
            + `${good ? ' —— 确实没红,证据见配对毒' : ' 🔴 竟然红了 ⇒ 冗余这个归因是错的,回去重判'}`);
    } else {
        say(`  ${good ? 'OK  ' : 'FAIL'} rc=${r.rc}${r.rc === 3 ? '(门自己断了,不算命中)' : ''} `
            + `期望红=[${p.expectRed.join(',')}]${missing.length ? ` 🔴 没红=[${missing.join(',')}]` : ' 全中'}`);
    }
    say(`       新增报红 ${fresh.length} 条:${fresh.join(',') || '(无)'}`);
    writeFileSync(abs(p.file), SRC0[p.file], 'utf8');
    const back = sha(abs(p.file)) === SHA0[p.file];
    if (!back) failures += 1;
    say(`  ${back ? 'OK  ' : 'FAIL'} 还原:逐文件 sha 与下毒前一致`);
}

say('\n=== 收尾 ===');
for (const f of TOUCHED) {
    const now = sha(abs(f));
    const good = now === SHA0[f];
    if (!good) failures += 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${f} ${now.slice(0, 12)}`);
}
const AFTER = treeState();
const headSame = AFTER.head === BEFORE.head;
const dirtySame = AFTER.dirty === BEFORE.dirty;
if (!headSame) failures += 1;
if (!dirtySame) failures += 1;
say(`  ${headSame ? 'OK  ' : 'FAIL'} HEAD 与开跑前一致 ${AFTER.head.slice(0, 9)}`);
say(`  ${dirtySame ? 'OK  ' : 'FAIL'} dirty 清单与开跑前一致`);

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${RUN.length} 发毒,每发都落在写死的那一格(含预期存活的),树状态逐字回位`);
process.exit(0);
