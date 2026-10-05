/**
 * WO_218-a1 · 「生成标题这次会扣多少」——**只负责显示口径**,一个乘法都没有。
 *
 * 契约原件:`services/topic_gen_charge.py` 的模块 docstring(读 sha,不读消息)。
 * 载点:`GET /api/writing/projects/{quote_id}` 回包里的 `charge`。
 *
 * 🔴 为什么把这几行摘成纯函数而不是写在 `WritingHall.tsx` 的 JSX 里:
 *    这个口径要在**五个入口**上成立(见 `shouldShowCharge` 的注释),
 *    写在 JSX 里就等于写五遍,而五遍里只要有一遍写反,
 *    **那一处各自看都对**,只有客户在那一个入口看到的数是错的。
 *    摘出来之后判据可以直接调它,不必把半个写作大厅搭起来。
 *
 * 🔴 本模块**不做乘法**。契约逐字写着「前端不要自己乘 base × count」——
 *    倍率口径一变两边就分家,而分家时两边各自都对。要显示什么就读什么字段。
 */

/** 后端 `charge_card()` 交下来的那一份。字段含义以契约原件为准。 */
export interface TopicGenCharge {
    feature_code: string;
    base_points: number;
    keyword_count: number;
    /** 本次将扣。 */
    estimated_points: number;
    /** 上限,契约保证 `>= estimated_points`。**显示这个**。 */
    ceiling_points: number;
}

const isInt = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && Number.isInteger(v);

/**
 * 把回包里的 `charge` 收成本模块认的形状。
 *
 * 🔴 **任何一处不合契约都回 `null`(= 不显示价),绝不回一个"尽量凑出来的数"。**
 *    凑出来的数会被客户当成收费依据 —— 而「少显示一个数」是可恢复的,
 *    「显示一个错的数」不是。
 * 🔴 `null` 与 `0` 在这里**必须分开**:`null` = 本次不走计费(不显示),
 *    `0` = 本次免费(显示"免费")。压成一种,少写一个判断就会对客户说错话。
 */
export function parseTopicGenCharge(raw: unknown): TopicGenCharge | null {
    if (!raw || typeof raw !== 'object') return null;
    const c = raw as Record<string, unknown>;
    if (typeof c.feature_code !== 'string' || !c.feature_code) return null;
    if (!isInt(c.base_points) || !isInt(c.keyword_count)) return null;
    if (!isInt(c.estimated_points) || !isInt(c.ceiling_points)) return null;
    if (c.base_points < 0 || c.keyword_count < 1) return null;
    if (c.estimated_points < 0 || c.ceiling_points < 0) return null;
    /*
     * 🔴 契约:「上限不许显示得比实扣少」。真出现这种回包时**不显示**,
     *    而不是退而求其次显示 `estimated_points` —— 后者会把一次契约违规
     *    悄悄糊过去,而这正是客户按之同意扣费的那个数。
     */
    if (c.ceiling_points < c.estimated_points) return null;
    return {
        feature_code: c.feature_code,
        base_points: c.base_points,
        keyword_count: c.keyword_count,
        estimated_points: c.estimated_points,
        ceiling_points: c.ceiling_points,
    };
}

/**
 * 「生成标题」这件事的三个**面孔**。
 *
 * 🔴 [WO_241 丙 · Review 裁定 2026-09-19] 拆的依据是
 *    **这个词的钱有没有付过**,不是端点名字、也不是按钮文案。
 *    前端能看见的对应物是「这个词有没有选题行」:
 *      · 没有行  = 从没进过任何批次 = 从没付过 ⇒ **收费**
 *      · 有行没标题 = 那一批已经付过、这一条没出来 ⇒ **免费补救**
 *        (退费只在「一条题都没出」时触发;部分成功一分不退 ⇒ 再收就是对同一个词收两次)
 *
 * 🔴 **为什么改成具名面孔,而不是把原来那个布尔取反**:
 *    原来的参数叫 `isNewKwOnly`,而 2026-09-19 之后它的**含义整个反了**
 *    (新词面从"免费"变成"收费")。名字没变、极性变了 ⇒ 每个调用点都会
 *    **读起来合理而意思相反**,那是最难发现的一种错。
 *    换成具名面孔,类型不兼容,编译器会把每个调用点逼出来重看一遍。
 */
export type TitleGenFace =
    /** 整张表出题(按钮默认面)—— 收费 */
    | 'batch-all'
    /** 只给没有选题行的新词出题(带 `per_keyword_plan` 子集)—— 收费 */
    | 'new-keywords'
    /** 某批次里缺题的那一条补救(带 `generation_request_id`)—— **免费** */
    | 'recover-missing'
    /**
     * [WO_254 附带] 这个词**一条都不缺题** —— 没有补救可做。
     * 它不是一种"点了会发生什么"的面孔,而是「这颗按钮根本不该出现」的信号:
     * 走补救端点会被后端 403,而那条 403 的话术指向一个页面上不存在的按钮。
     */
    | 'has-title';

/** 这一面会不会计费。**只此一处**,别在 JSX 里再判一遍。 */
export function isChargedFace(face: TitleGenFace): boolean {
    return face !== 'recover-missing';
}

/** 判面孔只需要 topics 行的这几列 —— 别把整个 Topic 类型拖进来。 */
export interface TopicRowForFace {
    keyword_id: number;
    generation_request_id?: string | null;
    /**
     * 🔴 [WO_254 附带] 判「这一行缺不缺题」要用它。
     *
     * 声明成可选是为了兼容只喂两列的老夹具,但**真实调用点必须带上** ——
     * 不带的话每一行都读作「缺题」,行为退回改动前(安全方向),
     * 而这个修**静默失效**。判据 F5 钉住真实调用点的行类型确实有这一列。
     */
    optimized_title?: string | null;
}

/** 这一行到底有没有题。空串 / 只有空白 / null 都算没有。 */
const rowHasTitle = (r: TopicRowForFace): boolean =>
    typeof r.optimized_title === 'string' && r.optimized_title.trim() !== '';

/**
 * 这个词走哪一面,以及补救要带的那个标识。
 *
 * 🔴 契约逐字:「有没有选题」按 topics 行的 **`keyword_id`** 判,
 *    **不按标题是否为空** —— 受理凭据行(`optimized_title IS NULL`)
 *    也算这个词**已经进过批次**,它是「缺题的词」走**免费补救**,不是「新词」。
 *    按标题判的话,那些词会被当成新词**再收一次钱** —— 对同一个词收两次。
 *
 * 🔴 这个判断只许有一处:它同时决定**走哪个端点**和**显不显示价**。
 *    两处各写一遍的话,会出现「按钮说免费、请求打收费端点」或者反过来,
 *    而两边各自看都对。
 */
export function faceForKeyword(
    keywordId: number,
    topics: readonly TopicRowForFace[],
): { face: TitleGenFace; generationRequestId: string | null } {
    const rows = (topics || []).filter((t) => t && t.keyword_id === keywordId);
    if (rows.length === 0) return { face: 'new-keywords', generationRequestId: null };
    /*
     * 🔴 [WO_254 附带] **一条都不缺题的词,没有"补救"可做。**
     *
     * 生产实测(项目 380 词 / 2900,topic 6104 completed 有题有文章 1519):
     * 这些词仍被判成 `recover-missing` ⇒ 打补救端点 ⇒ 后端 403
     * `TITLE_RECOVERY_NOT_AUTHORIZED` ⇒ toast 叫用户"用『生成标题』重新生成",
     * 而页面上根本没有同名按钮 —— 一条死路。
     *
     * 🔴 与上面那条契约**不冲突**,是对它的细化:受理凭据行
     * (`optimized_title IS NULL`)**仍然**走免费补救 —— 那正是"缺题的词"。
     * 变的只有「一条都不缺」这一种:它不是补救,是重新生成。
     */
    if (rows.every(rowHasTitle)) return { face: 'has-title', generationRequestId: null };
    const rid = rows.map((t) => t.generation_request_id).find((x) => typeof x === 'string' && x.length > 0);
    return { face: 'recover-missing', generationRequestId: rid ?? null };
}

/**
 * 还没有任何选题行的那些词 —— 「新词面」要发的 `per_keyword_plan` 子集。
 * 🔴 与 `faceForKeyword` 同一个判据(keyword_id 有没有行),不另写一遍。
 */
export function keywordIdsWithoutTopics(
    keywordIds: readonly number[],
    topics: readonly TopicRowForFace[],
): number[] {
    return (keywordIds || []).filter((id) => faceForKeyword(id, topics).face === 'new-keywords');
}

/**
 * 要显示的那句话;**`null` = 这一处不显示价**。
 *
 * 调用方一律 `const t = chargeText(...); return t && <Badge>{t}</Badge>` ——
 * 不许自己再判一遍 `charge === null`,那就又变成五个地方各写一遍了。
 */
export function chargeText(
    charge: TopicGenCharge | null,
    opts: { face?: TitleGenFace } = {},
): string | null {
    /*
     * 免费那一面(补救):不显示价,也**不显示 0** —— 0 会被读成"这次免费"的承诺,
     * 而补救之所以不要钱,是因为**这个词的钱已经付过了**,不是"本次免费"。
     */
    if (!isChargedFace(opts.face ?? 'batch-all')) return null;
    /* 本次不走计费(当前:admin)⇒ 不显示价。**不是显示 0**。 */
    if (!charge) return null;
    /* 这一支才是「本次免费」。 */
    if (charge.ceiling_points === 0) return '本次免费';
    /*
     * 🔴 显示 `ceiling_points`,不是 `estimated_points`,更不是 base × count:
     *    契约保证 `ceiling >= estimated`,而「显示得比实扣少」比不显示更糟 ——
     *    客户是按这个数同意扣费的。
     */
    return `本次将扣 ${charge.ceiling_points.toLocaleString()} 算力`;
}
