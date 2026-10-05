/**
 * 文章方向(user_choice)的**唯一**清单。
 *
 * 🔴 为什么抽出来:改前有**两份**互不相干的清单 ——
 *    `WritingHall.tsx` 的 `USER_CHOICE_OPTIONS`(页顶快捷模式 + 单篇下拉用)
 *    与 `DistributionConfigDialog.tsx` 的 `USER_CHOICE_LABELS`(配比对话框用)。
 *    加一档新方向时,只改一处就会出现「配比里能选、单篇下拉里没有」这种半截状态,
 *    而两边各自都"看起来对"。同一个谓词写两处,必有一处没人验。
 *    #185 正好要加一档,先把这件事收掉。
 *
 * 🔴 `value` 必须与后端 `server.py:USER_CHOICE_VALUES`(Pydantic Literal)**逐字一致**。
 *    前端发一个后端不认的值 = 422,而且是在用户点了「重新生成标题」之后才炸。
 *    判据 `verify-article-directions-contract.mjs` 每次跑都去读那份 Literal 比对。
 *
 * 🔴 本模块**零 import**:判据用 data: URL 直接加载它来真调,带相对 import 会解析不了。
 */

export type ArticleDirection =
    | 'auto'
    | 'evidence_qa'
    | 'multi_brand_comparison'
    | 'implementation_guide'
    | 'trend_policy_risk'
    | 'case_data_roi'
    | 'company_facts'
    | 'defensive_company';

export interface DirectionOption {
    value: ArticleDirection;
    label: string;
    /** 一句人话:用户要判断的是"我该选哪个",不是工程词。 */
    desc: string;
    emoji: string;
    /**
     * 防御型这一档改的是**题的主语**(题 = 公司名 + 问法),不只是写法。
     * 界面上要能把它和别的方向区分开(徽章、说明行),所以单独标出来。
     */
    defensive?: boolean;
}

/**
 * 🔴 `auto` 只在"选一个方向"的地方出现,**不进配比器**
 *    (配比是给每一类分篇数,"系统推荐"不是一类)。
 *    所以清单里带着它,取用的地方各自过滤 —— 而不是再维护第二份清单。
 */
/**
 * 🔴 [#185 · Review 09-13 裁定] **「防御型(公司词)」的编译期开关。**
 *
 *    前端这一档的值是 `defensive_company`,而后端 `server.py:USER_CHOICE_VALUES`
 *    (Pydantic `Literal`)还没有它 —— 用户选了、点「重新生成标题」就是 **422**,
 *    而界面上那一档看起来完全正常(有标签、有说明、能选中)。
 *    这正是 #176 那个形状:入口看着活的,点下去才发现是死的。
 *
 *    0913b 这班只带前端、不带 C 的 c2,所以**关着**。
 *    C 的 c2 上线那班把它翻成 `true`,判据 D2 会自动从"未评估"转绿。
 *
 * 🔴 为什么用开关不用"把代码删了下班再写":删掉再写会丢掉这一轮所有的
 *    判据与注释,而且下一次重写还会再踩一遍同样的坑。开关**本身是可验的**:
 *    D9 钉住"关着时三处入口都不出现这一档",毒:开关不生效 ⇒ 红。
 */
export const DEFENSIVE_DIRECTION_ENABLED = false;

/**
 * @@R@@ **全量**清单(含开关关着的那一档)。判据要能验"那一档的文案对不对",
 *    而对外清单在开关关着时根本没有它 —— 只看对外清单的话,
 *    文案判据会因为"这一档不在"而红,那是把两件事混在一起了:
 *    「能不能选」由开关管,「文案对不对」跟开关无关。
 */
export const ALL_ARTICLE_DIRECTIONS: DirectionOption[] = [
    {
        value: 'auto', label: '系统推荐', emoji: '✨',
        desc: '按证据、问题意图和运行中配比选择文体',
    },
    {
        value: 'evidence_qa', label: '证据型问答', emoji: '❓',
        desc: '直接回答问题,事实逐项可核验',
    },
    {
        value: 'multi_brand_comparison', label: '选购与多品牌比较(含排行榜单)', emoji: '⚖',
        desc: '同字段比较真实候选;可给有依据的排行榜单位次,禁自创评分体系',
    },
    {
        value: 'implementation_guide', label: '方法与实施指南', emoji: '🧭',
        desc: '步骤、检查点、风险与验收标准',
    },
    {
        value: 'trend_policy_risk', label: '趋势、政策与风险分析', emoji: '⚠',
        desc: '变化、证据、影响、风险与行动建议',
    },
    {
        value: 'case_data_roi', label: '案例、数据与 ROI', emoji: '📊',
        desc: '有边界的案例、数据和情景测算',
    },
    {
        value: 'company_facts', label: '企业事实与品牌说明', emoji: '🏢',
        desc: '区分企业自述与独立证据,需人工审核',
    },
    {
        /**
         * [#185] Owner 2026-09-13:「选择防御型 GEO,然后重新生成标题,
         * 就是全是企业相关的标题?」—— 是。
         *
         * 🔴 与 `company_facts` 的区别要写在 desc 里,因为两者**听起来像同一件事**:
         *    `company_facts` 只改**写法**(仍按原关键词出题);
         *    防御型改的是**题的主语**(题 = 公司名 + 问法)。
         *    用户选错这一步,拿到的东西完全不同。
         */
        value: 'defensive_company', label: '防御型(公司词)', emoji: '🛡',
        desc: '标题全部围绕公司名,如「<品牌>怎么样」;正文按「介绍公司」写,需人工审核',
        defensive: true,
    },
];

/**
 * 对外的清单:开关关着时**整档不出现**。
 * 🔴 过滤在**唯一清单**这一层做,不在三个入口各做一次 ——
 *    各做一次必然漏一处(那正是 #185 一开始要收拾的两份手写清单的病)。
 */
export const ARTICLE_DIRECTIONS: DirectionOption[] = ALL_ARTICLE_DIRECTIONS.filter(
    (d) => DEFENSIVE_DIRECTION_ENABLED || !d.defensive);

/** 配比器用:按类分篇数,「系统推荐」不是一类。 */
export function distributableDirections(): DirectionOption[] {
    return ARTICLE_DIRECTIONS.filter((d) => d.value !== 'auto');
}

/** 某个值是不是防御型 —— 徽章与缺事实提示都问它,不许各自写 `=== 'defensive_company'`。 */
export function isDefensiveDirection(value: unknown): boolean {
    const v = typeof value === 'string' ? value : '';
    /* 🔴 判定走**全量**清单,不走对外清单:开关关着时老数据里可能已经有这个值
       (比如另一班先落了后端、或历史数据),那时徽章仍然要认得出它。
       开关管的是"能不能**选**",不是"认不认得"。 */
    return ALL_ARTICLE_DIRECTIONS.some((d) => d.value === v && d.defensive === true);
}

/** 界面文案:防御型那一行的说明。徽章 hover 与配比器共用一句,不写两遍。 */
export const DEFENSIVE_HINT =
    '标题全部围绕公司名(如「<品牌>怎么样」「<品牌>靠谱吗」),正文按「介绍公司」写。';

/**
 * [#185] 防御型的八问。逐字取自 WO_185 §6 原文。
 *
 * 🔴 第六问是「适合谁·不适合谁」,**不是**「适合谁」。
 *    C 09-13 交底时特意点出来:Review 给他的转述里写的是「适合谁」,而 WO 原文带后半句。
 *    两边差半句,徽章文案与后端出的题就对不上 —— 引规则必须贴原文,不能凭转述。
 */
export const DEFENSIVE_QUESTIONS: string[] = [
    '怎么样',
    '靠谱吗·口碑',
    '投诉·售后',
    '与同类的差异(不点名不排名)',
    '资质·案例',
    '适合谁·不适合谁',
    '价格·收费',
    '团队·流程',
];

/**
 * 缺事实时那一行。
 *
 * 🔴 服务端(#185 c5)回的是**结构**不是整句:
 *    `missing_facts: [{ question: "<八问里的问名>", fields: ["<缺的档案字段名>", ...] }]`
 *    C 给结构不给整句的理由值得记:整句一旦少一个占位符,会渲染成用户可见的乱码,
 *    而那种错**代码里不报错**。措辞归前端。
 *
 * 🔴 说**缺什么**,不说"资料不足" —— 后者用户不知道该去补哪一项。
 *    一个都没给就返回空串,**不编**。
 *    老形状(纯字符串数组)也吃得下:契约还没落地时我自己的桩是那个形状,
 *    两种都收比"上线当天才发现形状不对"便宜。
 */
export function defensiveMissingFactsLine(missing: unknown): string {
    const rows = Array.isArray(missing) ? missing : [];
    const parts: string[] = [];
    for (const raw of rows) {
        if (typeof raw === 'string') {
            const t = raw.trim();
            if (t) parts.push(t);
            continue;
        }
        if (!raw || typeof raw !== 'object') continue;
        const o = raw as { question?: unknown; fields?: unknown };
        const q = typeof o.question === 'string' ? o.question.trim() : '';
        const fields = Array.isArray(o.fields)
            ? o.fields.map((f) => (typeof f === 'string' ? f.trim() : '')).filter(Boolean)
            : [];
        if (q && fields.length) parts.push(`${q}(缺${fields.join('、')})`);
        else if (q) parts.push(q);
        else if (fields.length) parts.push(fields.join('、'));
    }
    if (parts.length === 0) return '';
    return `这几问还没有可引用的事实:${parts.join(';')}。补齐后防御篇会更实,现在写出来偏空。`;
}
