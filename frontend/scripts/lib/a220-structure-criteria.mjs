/**
 * #220 结构面判据 · **唯一归属**在这里。
 *
 * 🔴 为什么单独一个模块:这几格只读源文件文本,不需要浏览器,
 *    所以它们能进 `npm run build` 的 && 链(那条链跑在 Dockerfile 的 frontend-builder 里,
 *    那儿没有 chromium —— 起浏览器的门挂进去会退 3,把烤镜像整条挡掉,2026-09-16 实测)。
 *    而行为门必须起浏览器,进不了链。
 *
 * 🔴 为什么是**共享模块**而不是抄一份:同一个谓词落在两个文件里,
 *    早晚一边改一边不改,而两边都是绿的。判据和产品代码一样,一个谓词一个家。
 *
 * 调用方各自提供 `ok(cond, name, detail)`,这样两边的三态/计数各归各。
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export const STRUCTURE_SOURCES = [
    'src/components/brand/BrandImageGallery.tsx',
    'src/pages/Writing/WritingHall.tsx',
];

/** 读不到源文件 = 门没跑成(3),不是判据红(1) —— 由调用方的 cannotRun 处理。 */
export function readStructureSources(ROOT) {
    return {
        GALLERY_SRC: readFileSync(join(ROOT, STRUCTURE_SOURCES[0]), 'utf8'),
        HALL_SRC: readFileSync(join(ROOT, STRUCTURE_SOURCES[1]), 'utf8'),
    };
}

/**
 * 跑结构面 6 格。返回跑了几格,调用方用来对分母。
 * 🔴 格名与原来逐字一致(I6a/I6/I7/I8a/I8b/K4a)——
 *    注毒表按名字断言期望红,改名字等于悄悄换掉判据。
 */
export function runStructureCriteria(ok, { GALLERY_SRC, HALL_SRC }) {
    const before = { n: 0 };
    const count = (...args) => { before.n += 1; return ok(...args); };

    const fileInputs = GALLERY_SRC.match(/<input\s[^>]*type="file"[\s\S]*?\/>/g) || [];
    count(fileInputs.length >= 2, 'I6a 分母自证:文件选择框总数', `${fileInputs.length} 个`);
    count(fileInputs.length > 0 && fileInputs.every((x) => /\bmultiple\b/.test(x)),
        'I6 🔴 **每一个**文件选择框都有 multiple —— 常态按钮和空态整块是两个 input,'
        + '只改一个的话空态那条路仍然只能单传,而空态正是新客户第一次用的那条路',
        `${fileInputs.filter((x) => /\bmultiple\b/.test(x)).length}/${fileInputs.length}`);
    count(fileInputs.length > 0 && fileInputs.every((x) => /disabled=\{uploading\}/.test(x)),
        'I7 🔴 每一个都在上传中禁用 —— 顺序上传期间再选一批会让两批交错、列表刷新竞态',
        `${fileInputs.filter((x) => /disabled=\{uploading\}/.test(x)).length}/${fileInputs.length}`);

    /*
     * 🔴 [a1'' · Review 09-16 点名] 「一次最多 20 张」是**裁定进来的承诺**,
     *    我实现了却没给它配锁 —— 复审把 `MAX_UPLOAD_BATCH = 200` 和「干脆不截断」
     *    两发毒打进来,26 条判据照样全绿。
     *    我那 20 发毒**全是从我自己的判据倒推的**,所以一发都没打到这一面。
     *    列毒要从「抬头承诺的每一样」来,不是从「我已经写了哪些判据」来。
     */
    count(/const MAX_UPLOAD_BATCH = 20;/.test(GALLERY_SRC),
        'I8a 🔴 一批上限**就是 20**(裁定值,不是随手写的数)',
        (GALLERY_SRC.match(/const MAX_UPLOAD_BATCH = \d+;/) || ['(没找到这个常量)'])[0]);
    count(/picked\.slice\(0, MAX_UPLOAD_BATCH\)/.test(GALLERY_SRC),
        'I8b 🔴 截断用的是**那个常量**,不是另写一个字面量 —— '
        + '写死数字的话,改常量不会改行为,而判据看常量是绿的',
        /picked\.slice\(0, \d+\)/.test(GALLERY_SRC) ? '🔴 截断处写的是字面量' : '引用常量');

    /* 🔴 这条是**反臂**:换客户必须仍然整体清空。
          修「保存后不清空」最容易顺手把 resetKnowledgeDraft 整个删掉,
          那样换客户时上一家的资料会留在表里 —— 比原缺陷严重得多。 */
    count(/const resetKnowledgeDraft = \(\) => \{/.test(HALL_SRC)
        && (HALL_SRC.match(/resetKnowledgeDraft\(\);/g) || []).length >= 2,
        'K4a 反臂:整体清空的函数还在,且仍有 ≥2 处调用(换客户 / 退回列表)',
        `${(HALL_SRC.match(/resetKnowledgeDraft\(\);/g) || []).length} 处调用`);

    return before.n;
}

/** 两边共用的分母:这个模块承诺跑满几格。对不上就是有人删了格。 */
export const STRUCTURE_CRITERIA_COUNT = 6;
