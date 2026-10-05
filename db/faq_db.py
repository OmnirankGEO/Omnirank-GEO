"""
帮助中心 FAQ 数据层 (Phase 2 · 2026-05-18)

3 张表:
  - faq_items     · FAQ 条目主表 · 带 thumbs_up/down 冗余计数(投票变化后应用层 recalc)
  - faq_votes     · 投票记录 · 一人一票 · 可切换可撤销
  - faq_feedback  · 用户反馈 · client_id 唯一防离线重发

启动时由 server.py 调用 init_faq_tables() 幂等建表 + 首次 seed 12 条占位 FAQ。

接手同事注意:
  - 冗余计数 thumbs_up/down 由 _recalc_counts() 应用层维护(没用 trigger · 调试更直观)
  - 反馈状态机: pending → read → done → closed (handled_at/handled_by 在 read/done/closed 时填)
  - 一人一票去重靠 PRIMARY KEY (faq_id, user_id)
"""

import logging
from typing import Any, Optional
import psycopg2

logger = logging.getLogger("FAQ-DB")


def _get_conn():
    from db.connection import get_connection
    return get_connection()


# 帮助中心身份分档:FAQ 可见性。普通用户只看 normal_user+both,代理看 agent+both,管理员看全部。
_VISIBLE_TO_VALUES = ('normal_user', 'agent', 'both')

# 代理专属 seed FAQ(含佣金/提现等代理经营内容)· 首次 seed / 老库补档时收紧到 agent。
# 其余 seed 默认 both(普通用户和代理都能看)。
_AGENT_ONLY_SEED_QUESTIONS = {'在哪里查扣费明细'}


def _visible_to_filter(identity: Optional[str]) -> Optional[tuple[str, ...]]:
    """按身份返回允许的 visible_to 取值;返回 None 表示不过滤(管理员/全量)。"""
    if identity == 'admin':
        return None
    if identity in ('agent', 'l2'):  # 一级/二级代理:FAQ 看 agent + both
        return ('agent', 'both')
    if identity == 'normal_user':
        return ('normal_user', 'both')
    return None


# ==========================================
# 初始化 + Seed
# ==========================================

_INITIAL_SEED: list[tuple[str, str, int, int, int, str]] = [
    # (question, category, sort_order, mock_up, mock_down, answer_md)
    # answer_md 用"讲人话"风格写 · 零英文术语 · 用"你"称呼 · 见 frontend/src/pages/Help/docs-data.ts
    (
        '扣费后生成失败会退款吗',
        'billing', 10, 12, 8,
        """**大部分情况不用你操心 · 系统会自动处理**:

- 写文章这类短任务是**跑成功才扣费** · 失败了根本没扣过你的算力
- 诊断 / 监测这类长任务是**先冻结、跑完再结算** · 失败会自动把冻结的算力原路退回

所以正常情况下 "失败白扣" 不会发生。万一你查流水发现确实被扣了但任务没出结果:

1. 进我的钱包 → 资金流水 · 记下那笔扣费的编号 + 时间
2. 帮助中心顶部点 "给管理员提反馈" · 紧急度选 "卡死了" · 附扣费编号 + 失败截图
3. 管理员核实后补回

详见 [扣费失败](/help/docs/payment-failed) 文档。""",
    ),
    (
        '余额不足时怎么充值',
        'billing', 20, 23, 1,
        """在左边栏 "收益" 那组 · 点 **我的钱包**。顶部有个 **充值** 按钮:

- 输你想充的金额 · 或选个套餐
- 微信 / 支付宝扫码付
- 钱到账是即时的

充进来的是**充值算力** · 可以花。没用完想退也行(原路退回未消耗部分 · 手续费与到账时间以退款页显示为准)。详见 [我的钱包 + 充值](/help/docs/wallet)。""",
    ),
    (
        '在哪里查扣费明细',
        'billing', 30, 5, 0,
        """进我的钱包页 · 拉到底部能看到 **资金流水**。可以按类型筛选:

- 消费 / 充值 / 退款
- 佣金到账 / 提现冻结 / 提现退回 / 提现打款

每条流水都有:时间 / 类型 / 金额 / 关联订单编号。""",
    ),
    (
        '找不到诊断入口在哪',
        'operation', 10, 18, 2,
        """两个入口都能进:

- 在左边栏 "销售" 那组 · 点 **品牌体检** — 走通用诊断流程
- 或者从客户列表点客户名进详情页 · 点 **发起诊断** — 系统会自动带这个客户的信息

新代理推荐第二种:先**加客户**(我的客户 → + 添加客户)· 再从客户详情发起 · 省下重复填字段的功夫。""",
    ),
    (
        '流程卡住了点不动怎么办',
        'operation', 20, 7, 5,
        """按这个顺序排查试试:

1. 先按 **F5 刷新**页面 — 大多数前端卡顿一刷新就好了
2. 看右下角有没有提示 — 网断了 / 后端报错都会有提示
3. 进我的钱包看流水 — 看刚才那笔操作有没有真扣费(没扣费 = 任务根本没启动)
4. 还不行就提反馈 — 把页面 URL + 截图 + 浏览器控制台错误(按 F12 看 Console)都发给管理员

诊断 / 监测 / 写作这种长任务一直没结果的话 · 看 [扣费失败](/help/docs/payment-failed)。""",
    ),
    (
        '怎么联系客服',
        'operation', 30, 34, 0,
        """**帮助中心顶部的 "给管理员提反馈" 按钮**就是客服入口。

- 写清你的问题 + 紧急度
- 提交后管理员在后台收件箱能看到
- 紧急(卡死)类问题优先处理 · 普通问题按反馈顺序回

紧急的财务问题(扣费 / 退款 / 提现)请在反馈里附上扣费编号 + 时间戳 · 帮我们快速定位。""",
    ),
    (
        '诊断报告里那个分数代表啥',
        'data', 10, 9, 11,
        """**GEO 总分** 0-100 · 综合反映你客户在 AI 搜索里的表现。

6 个等级:

- 85-100 **领先** — AI 主动推荐你客户
- 70-84 **成熟** — 常被提到
- 55-69 **成长** — 提到不太稳
- 40-54 **起步** — 偶尔提到
- 20-39 **待提升** — 极少提到
- 0-19 **空白** — AI 还不知道你客户

总分由 5 个维度加权算出:**AI引擎推荐率(30)** + **网页内容资产(25)** + **权威背书(20)** + **结构化内容(15)** + **品牌基础(10)**。详见 [评分维度](/help/docs/scoring)。""",
    ),
    (
        '监测多久跑一次, 数据多久更新',
        'data', 20, 15, 1,
        """监测有两种频率:

- **定时监测**(默认)— 每天自动跑 1 次(24 小时间隔)· 每词每次扣 130 算力
- **立即测一次** — 你手动点立刻跑 · 几分钟出结果 · 也是每词 130 算力
- **监测月包 10 词** — 整体 39000 算力 · 比按次划算(具体以监测页显示为准)

定时频率可以在排名监测页配置(每 N 小时一次)。""",
    ),
    (
        '能把数据导出来吗',
        'data', 30, 3, 0,
        """**现在能导**:

- 诊断报告 PDF / PPTX(报告页右上 · 扣 260 算力)
- 月度监测报告 markdown 草稿(排名监测页 "生成月报" · 默认 draft 状态需审核后发)

**还没做**(常见需求 · 在排期):

- 客户列表批量导成 Excel
- 监测原始数据 CSV
- 文章批量下成 Word
- 财务流水 Excel

短期可以用截图或者复制粘贴单元格替代 · 详见 [数据导出](/help/docs/data-export)。""",
    ),
    (
        '怎么改密码',
        'account', 10, 11, 0,
        """进左边栏 "账号" → **个人设置** · 拉到页面底下有个 **修改密码** 按钮:

- 输当前密码(就是你现在登录用的那个)
- 输新密码(至少 6 位)
- 再输一次新密码(防打错)

改完**不用重新登录** · 系统会自动帮你续上登录状态。

要是当前密码忘了? 去登录页用**短信验证码登录** · 登进系统后再改。""",
    ),
]


def _assert_seed_text_clean() -> None:
    """[R3-P10 ①] `_INITIAL_SEED` 过门 —— **第五条写径不许裸奔**。

    ## 为什么这条也算写径

    ``init_faq_tables()`` 里有两个写 `faq_items` **正文**的分支:
    首部署那次 ``INSERT``,以及老库那次 ``UPDATE ... SET answer_md``
    (只覆盖 ``updated_by IS NULL`` 的行)。两个都把 `_INITIAL_SEED` 的文本
    直接灌进 `faq_items` —— 而 `faq_items` 的已发布行正是 `build_faq_chunks()`
    的取数来源。R3-P9 ② 关了 `create_faq_item` / `update_faq_item` 两个 writer
    与 API 前门,唯独**启动期这条**从来没人拦:seed 里混进一个旧词,
    每次部署都会把它同步进库,然后 `reindex_faq()` 在**别的地方**抛,
    报的错跟"谁把它放进来的"毫无关系。

    ## 位置:在 DDL **之后**、写 seed **之前**

    放在建表之前的话,一个脏 seed 会让 `faq_items` **整张表都建不出来** ——
    那比脏 seed 坏得多(`init_faq_tables()` 在 server.py 里被
    ``try/except Exception`` 包着,抛出去只记 warning,应用照跑,
    于是会得到一个"表不存在"的 FAQ 模块)。
    所以顺序是:建表 / 建索引 → **过门** → 写 seed。

    ## 全量扫完再决定 —— 而且**不自己写这个循环**

    [WO-A ⑤ · 2026-08-20] 上一版在这里手抄了第三份「逐条扫 → 汇总 → 抛」聚合器
    (前两份是 :func:`services.kb_terminology_gate.assert_kb_text_clean` 与
    :func:`~services.kb_terminology_gate.assert_chunk_rows_clean`)。抄出来的那份
    漏了 ``_warn_advisories`` —— 也就是说 seed 里的 ③ 域建议**一条都不会被记下来**,
    而同一份文本走 chunk 写径时是会记的。同一个谓词写三处,必有一处没人验。

    现在改成**委托**给 ``assert_chunk_rows_clean``:把 seed 条目摊成它认识的行形状
    (``source_slug`` / ``source_title`` / ``content``)。行为等价 ——
    ``slug`` 取 ``seed[前 20 字]``,于是问题串仍然是 ``seed[xxx]: <命中>``,
    ``where`` 仍然是 ``faq_db.seed``,异常类型与 ``fixes`` 都不变;
    **多出来的**只有 advisory 日志(那正是要补的)。
    """
    from services.kb_terminology_gate import assert_chunk_rows_clean

    assert_chunk_rows_clean(
        [
            {
                "source_slug": "seed[{0}]".format((entry[0] or "")[:20]),
                "source_title": entry[0],
                "content": entry[5],
            }
            for entry in _INITIAL_SEED
        ],
        where="faq_db.seed",
    )


def init_faq_tables():
    """幂等建表 + 首次 seed"""
    conn = _get_conn()
    try:
        cur = conn.cursor()

        # ---------- faq_items ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS faq_items (
                id              SERIAL PRIMARY KEY,
                question        TEXT NOT NULL,
                answer_md       TEXT NOT NULL DEFAULT '',
                category        VARCHAR(20) NOT NULL CHECK (category IN ('billing','operation','data','account')),
                sort_order      INTEGER NOT NULL DEFAULT 0,
                is_published    BOOLEAN NOT NULL DEFAULT TRUE,
                thumbs_up       INTEGER NOT NULL DEFAULT 0,
                thumbs_down     INTEGER NOT NULL DEFAULT 0,
                feedback_count  INTEGER NOT NULL DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW(),
                updated_by      INTEGER
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_faq_items_cat_sort
            ON faq_items (category, sort_order);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_faq_items_published
            ON faq_items (is_published) WHERE is_published = TRUE;
        """)
        # 身份可见性列(迁移安全 · 老库补列)· normal_user / agent / both
        cur.execute("""
            ALTER TABLE faq_items
            ADD COLUMN IF NOT EXISTS visible_to VARCHAR(16) NOT NULL DEFAULT 'both';
        """)
        cur.execute("""
            ALTER TABLE faq_items
            DROP CONSTRAINT IF EXISTS faq_items_visible_to_check;
        """)
        cur.execute("""
            ALTER TABLE faq_items
            ADD CONSTRAINT faq_items_visible_to_check
            CHECK (visible_to IN ('normal_user','agent','both'));
        """)

        # ---------- faq_votes ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS faq_votes (
                faq_id      INTEGER NOT NULL REFERENCES faq_items(id) ON DELETE CASCADE,
                user_id     INTEGER NOT NULL,
                vote        VARCHAR(10) NOT NULL CHECK (vote IN ('up','down')),
                created_at  TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (faq_id, user_id)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_faq_votes_user
            ON faq_votes (user_id);
        """)

        # ---------- faq_feedback ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS faq_feedback (
                id              SERIAL PRIMARY KEY,
                client_id       VARCHAR(64) NOT NULL UNIQUE,
                faq_id          INTEGER REFERENCES faq_items(id) ON DELETE SET NULL,
                message         TEXT NOT NULL,
                urgency         VARCHAR(10) NOT NULL DEFAULT 'low' CHECK (urgency IN ('low','mid','high')),
                contact         TEXT NOT NULL DEFAULT '',
                user_id         INTEGER NOT NULL,
                status          VARCHAR(10) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','read','done','closed')),
                admin_note      TEXT NOT NULL DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW(),
                handled_at      TIMESTAMP,
                handled_by      INTEGER
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_faq_feedback_status
            ON faq_feedback (status, created_at DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_faq_feedback_faq
            ON faq_feedback (faq_id) WHERE faq_id IS NOT NULL;
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD COLUMN IF NOT EXISTS kind VARCHAR(10) NOT NULL DEFAULT 'faq';
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            DROP CONSTRAINT IF EXISTS faq_feedback_kind_check;
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD CONSTRAINT faq_feedback_kind_check CHECK (kind IN ('faq','bug'));
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD COLUMN IF NOT EXISTS screenshot_url TEXT NOT NULL DEFAULT '';
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD COLUMN IF NOT EXISTS ai_answer TEXT NOT NULL DEFAULT '';
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD COLUMN IF NOT EXISTS admin_note TEXT NOT NULL DEFAULT '';
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD COLUMN IF NOT EXISTS handled_at TIMESTAMP;
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD COLUMN IF NOT EXISTS handled_by INTEGER;
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD COLUMN IF NOT EXISTS submitter_identity VARCHAR(20) NOT NULL DEFAULT '';
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            DROP CONSTRAINT IF EXISTS faq_feedback_submitter_identity_check;
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD CONSTRAINT faq_feedback_submitter_identity_check
            CHECK (submitter_identity IN ('', 'normal_user', 'agent', 'l2', 'admin'));
        """)
        cur.execute("""
            ALTER TABLE faq_feedback
            ADD COLUMN IF NOT EXISTS submitter_agent_level INTEGER NOT NULL DEFAULT 0;
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_faq_feedback_kind_status
            ON faq_feedback (kind, status, created_at DESC);
        """)

        # ---------- 首次 seed 或老库补 answer_md ----------
        # 🔴 [R3-P10 ①] 写 seed 之前过术语门。建表/建索引已经做完 ——
        #    脏 seed 只该挡住「把脏文本灌进库」,不该连表都建不出来。
        #
        #    🔴 先 commit 再过门:DDL 与 seed 写在**同一个事务**里,
        #    门抛出去会把 CREATE TABLE 一起回滚 —— 那就退化成「脏 seed ⇒ 表不存在」,
        #    比脏 seed 坏得多(init_faq_tables 在 server.py 被 try/except 包着,
        #    抛出去只记 warning,应用照跑,于是 FAQ 模块拿到一个没有表的库)。
        #    判据:test_a_dirty_seed_still_leaves_the_tables_created —— 它就是这么抓到的。
        conn.commit()
        _assert_seed_text_clean()
        cur.execute("SELECT COUNT(*) AS c FROM faq_items")
        row = cur.fetchone()
        if row and row['c'] == 0:
            # 全新部署 · seed 带答案 + 身份可见性
            for q, cat, sort_o, mock_up, mock_down, answer in _INITIAL_SEED:
                vis = 'agent' if q in _AGENT_ONLY_SEED_QUESTIONS else 'both'
                cur.execute(
                    """INSERT INTO faq_items
                       (question, category, sort_order, thumbs_up, thumbs_down, answer_md, visible_to)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (q, cat, sort_o, mock_up, mock_down, answer, vis),
                )
            logger.info("[faq] seed 初始 FAQ 完成(含答案 + 身份可见性)")
        else:
            # 老部署 · 同步 seed 答案到 DB
            # - updated_by IS NULL:管理员没手动改过 · 同步到当前 seed 版本(允许覆盖)
            # - updated_by 非 NULL:管理员手动改过 · 保留管理员版本不动
            patched = 0
            for q, _cat, _sort, _up, _down, answer in _INITIAL_SEED:
                cur.execute(
                    """UPDATE faq_items
                       SET answer_md = %s, updated_at = NOW()
                       WHERE question = %s
                         AND updated_by IS NULL
                         AND answer_md IS DISTINCT FROM %s""",
                    (answer, q, answer),
                )
                patched += cur.rowcount
            if patched > 0:
                logger.info(f"[faq] 同步了 {patched} 条 FAQ answer_md 到最新 seed 版本")
            # 老库补 seed 的身份可见性:管理员没手动改过(updated_by IS NULL)的代理专属 seed 收紧到 agent
            for q in _AGENT_ONLY_SEED_QUESTIONS:
                cur.execute(
                    """UPDATE faq_items
                       SET visible_to = 'agent', updated_at = NOW()
                       WHERE question = %s AND updated_by IS NULL AND visible_to <> 'agent'""",
                    (q,),
                )

        conn.commit()
        logger.info("[faq] 表初始化完成 (faq_items / faq_votes / faq_feedback)")
    finally:
        conn.close()


# ==========================================
# 内部工具
# ==========================================

def _recalc_counts(cur, faq_id: int) -> None:
    """投票变化后重算 faq_items 的冗余计数"""
    cur.execute(
        """
        UPDATE faq_items SET
          thumbs_up   = (SELECT COUNT(*) FROM faq_votes WHERE faq_id = %s AND vote = 'up'),
          thumbs_down = (SELECT COUNT(*) FROM faq_votes WHERE faq_id = %s AND vote = 'down')
        WHERE id = %s
        """,
        (faq_id, faq_id, faq_id),
    )


def _recalc_feedback_count(cur, faq_id: int) -> None:
    """反馈状态变化后重算 faq_items 的 feedback_count(只算 pending+read 未结案)"""
    cur.execute(
        """
        UPDATE faq_items SET
          feedback_count = (
            SELECT COUNT(*) FROM faq_feedback
            WHERE faq_id = %s AND kind = 'faq' AND status IN ('pending','read')
          )
        WHERE id = %s
        """,
        (faq_id, faq_id),
    )


# ==========================================
# FAQ 条目 CRUD
# ==========================================

# ══════════════════════════════════════════════════════════════════════════
# [R3-P9 ②] 术语门:进 canonical writer 同事务(行锁)+ 用户面读路径过滤
# ══════════════════════════════════════════════════════════════════════════
#
# R3-P8 的门开在 ``api/faq_api.py`` 的**前门**:先 ``get_faq_item()`` 读一次
# (自己一条连接),再 ``update_faq_item()`` 写一次(**另一条连接**)。
# 读与写之间没有锁,于是有一条真缝:
#
#   PATCH A: {answer_md: 脏}          读到 is_published=False → 放行 → 写脏正文
#   PATCH B: {is_published: True}     读到 answer_md=干净     → 放行 → 标上架
#   ⇒ 两个请求各自都"合规",合起来产出一条**已上架的违规行**。
#
# 所以判定必须挪进 writer,和写在**同一个事务**里,并且先把那一行
# ``SELECT ... FOR UPDATE`` 锁住再合并 —— 两个 PATCH 于是排队,后到的那个
# 看见的是前一个写完的值。前门那道**不撤**:它的职责是让管理员在提交那一刻
# 就拿到「哪个词、改成什么」,而不是提交成功、后台重建静默失败。
# 两道用的是**同一份实现**(``services.kb_terminology_gate``),不各写一套。


def _assert_release_text_clean(question: Optional[str], answer_md: Optional[str],
                               *, is_published: bool, where: str) -> None:
    """要进 release 的文本过术语门。草稿(``is_published=False``)放行。

    草稿不进索引(``build_faq_chunks`` 的 SQL 明写 ``WHERE is_published = TRUE``),
    存一份带旧词的草稿对用户零影响 —— 硬拦草稿只会把门变成"别用草稿"的压力。
    """
    if not is_published:
        return
    from services.kb_terminology_gate import assert_kb_text_clean

    assert_kb_text_clean(
        (question or "") + chr(10) + (answer_md or ""), where=where)


def _row_release_violations(row: dict) -> list:
    """一行 FAQ 的违规清单(用户面读路径用)。"""
    from services.kb_terminology_gate import kb_write_violations

    return kb_write_violations(
        str(row.get("question") or "") + chr(10) + str(row.get("answer_md") or ""))


def list_faq_items(
    user_id: Optional[int] = None,
    category: Optional[str] = None,
    include_unpublished: bool = False,
    identity: Optional[str] = None,
) -> list[dict]:
    """
    列出 FAQ · 默认只返回已上架 · 带当前用户自己的 my_vote

    identity 控制身份可见性过滤(后端真实身份 · 不信前端传参):
      - 'normal_user' → 只看 visible_to ∈ (normal_user, both)
      - 'agent'       → 看 visible_to ∈ (agent, both)
      - 'admin' / None → 不按 visible_to 过滤(看全部)
    管理员查全部传 include_unpublished=True + identity='admin'
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        where = []
        params: list[Any] = []
        if not include_unpublished:
            where.append("is_published = TRUE")
        if category:
            where.append("category = %s")
            params.append(category)
        allowed = _visible_to_filter(identity)
        if allowed is not None:
            placeholders = ", ".join(["%s"] * len(allowed))
            where.append(f"visible_to IN ({placeholders})")
            params.extend(allowed)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        # LEFT JOIN faq_votes 拿当前用户那一票
        sql = f"""
            SELECT
              i.id, i.question, i.answer_md, i.category, i.sort_order,
              i.is_published, i.visible_to, i.thumbs_up, i.thumbs_down, i.feedback_count,
              i.created_at, i.updated_at,
              v.vote AS my_vote
            FROM faq_items i
            LEFT JOIN faq_votes v ON v.faq_id = i.id AND v.user_id = %s
            {where_sql}
            ORDER BY i.category, i.sort_order, i.id
        """
        cur.execute(sql, [user_id or 0, *params])
        rows = list(cur.fetchall())
    finally:
        conn.close()

    # 🔴 [R3-P9 ②] 用户面读路径过滤:**违规的已发布行不得吐出**。
    #    writer 上的门管得住"经过我们的写入路径"的改动;管不住 DBA 直改库、
    #    也管不住历史遗留行。而 FAQ 是用户面文案 + 小榜索引源,
    #    「库里有一条脏的」和「用户看得见一条脏的」是两件事,后者才是伤害。
    #    只在用户面过滤:``include_unpublished=True`` 是管理员视图,
    #    要看得见才改得掉 —— 把脏行对管理员也藏起来,等于让它永远留在库里。
    if include_unpublished:
        return rows
    kept = []
    for row in rows:
        problems = _row_release_violations(row)
        if problems:
            logger.warning(
                "[faq] 已发布行 id=%s 含废弃说法,已从用户面隐藏(请到后台修):%s",
                row.get("id"), problems[:3])
            continue
        kept.append(row)
    return kept


def get_faq_item(faq_id: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM faq_items WHERE id = %s", (faq_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_faq_item(
    question: str,
    category: str,
    answer_md: str = "",
    sort_order: int = 0,
    is_published: bool = True,
    updated_by: Optional[int] = None,
    visible_to: str = 'both',
) -> int:
    if visible_to not in _VISIBLE_TO_VALUES:
        raise ValueError(f"invalid visible_to: {visible_to}")
    # [R3-P9 ②] 新建没有"旧值"可合并,但门要和写在同一条路径上 ——
    # 绕过 API 直接调 writer 的调用方(脚本 / 迁移 / 以后新增的入口)也得过。
    _assert_release_text_clean(question, answer_md,
                               is_published=bool(is_published), where="faq_db.create")
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO faq_items
               (question, answer_md, category, sort_order, is_published, updated_by, visible_to)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (question, answer_md, category, sort_order, is_published, updated_by, visible_to),
        )
        new_id = cur.fetchone()['id']
        conn.commit()
        return new_id
    finally:
        conn.close()


def update_faq_item(
    faq_id: int,
    *,
    question: Optional[str] = None,
    answer_md: Optional[str] = None,
    category: Optional[str] = None,
    sort_order: Optional[int] = None,
    is_published: Optional[bool] = None,
    updated_by: Optional[int] = None,
    visible_to: Optional[str] = None,
) -> bool:
    """部分更新 · 返回是否真的有行被改"""
    if visible_to is not None and visible_to not in _VISIBLE_TO_VALUES:
        raise ValueError(f"invalid visible_to: {visible_to}")
    fields = []
    params: list[Any] = []
    if question is not None:
        fields.append("question = %s")
        params.append(question)
    if answer_md is not None:
        fields.append("answer_md = %s")
        params.append(answer_md)
    if category is not None:
        fields.append("category = %s")
        params.append(category)
    if sort_order is not None:
        fields.append("sort_order = %s")
        params.append(sort_order)
    if is_published is not None:
        fields.append("is_published = %s")
        params.append(is_published)
    if visible_to is not None:
        fields.append("visible_to = %s")
        params.append(visible_to)
    if not fields:
        return False
    fields.append("updated_at = NOW()")
    if updated_by is not None:
        fields.append("updated_by = %s")
        params.append(updated_by)
    params.append(faq_id)

    conn = _get_conn()
    try:
        cur = conn.cursor()
        # 🔴 [R3-P9 ②] 先锁行再合并再判定 —— 全部在**同一个事务**里。
        #    没有这把锁,「改正文」与「点上架」两个并发 PATCH 各自读到对方写之前的
        #    旧值,两边都判"合规",合起来产出一条已上架的违规行。
        cur.execute(
            "SELECT question, answer_md, is_published FROM faq_items "
            "WHERE id = %s FOR UPDATE",
            (faq_id,),
        )
        locked = cur.fetchone()
        if locked is None:
            conn.rollback()
            return False
        merged_q = question if question is not None else locked["question"]
        merged_a = answer_md if answer_md is not None else locked["answer_md"]
        merged_p = is_published if is_published is not None else locked["is_published"]
        try:
            _assert_release_text_clean(merged_q, merged_a,
                                       is_published=bool(merged_p),
                                       where="faq_db.update")
        except Exception:
            conn.rollback()
            raise
        cur.execute(
            f"UPDATE faq_items SET {', '.join(fields)} WHERE id = %s",
            params,
        )
        changed = cur.rowcount > 0
        conn.commit()
        return changed
    finally:
        conn.close()


def delete_faq_item(faq_id: int) -> bool:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM faq_items WHERE id = %s", (faq_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def reorder_items(ids: list[int]) -> int:
    """
    按 ids 顺序重新分配 sort_order(10, 20, 30, ...) · 单事务批量更新
    用于拖拽排序 · 不校验 category 一致性(信任前端传同分类内的 ids)
    返回成功更新的行数
    """
    if not ids:
        return 0
    conn = _get_conn()
    try:
        cur = conn.cursor()
        updated = 0
        for i, fid in enumerate(ids):
            cur.execute(
                "UPDATE faq_items SET sort_order = %s, updated_at = NOW() WHERE id = %s",
                ((i + 1) * 10, fid),
            )
            updated += cur.rowcount
        conn.commit()
        return updated
    finally:
        conn.close()


# ==========================================
# 投票
# ==========================================

def set_vote(faq_id: int, user_id: int, vote: Optional[str]) -> dict:
    """
    投票 / 切换 / 撤销 · vote 传 None 撤销
    返回更新后的 {thumbs_up, thumbs_down, my_vote}
    """
    if vote not in ('up', 'down', None):
        raise ValueError(f"invalid vote: {vote}")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        if vote is None:
            cur.execute(
                "DELETE FROM faq_votes WHERE faq_id = %s AND user_id = %s",
                (faq_id, user_id),
            )
        else:
            cur.execute(
                """INSERT INTO faq_votes (faq_id, user_id, vote)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (faq_id, user_id)
                   DO UPDATE SET vote = EXCLUDED.vote, created_at = NOW()""",
                (faq_id, user_id, vote),
            )
        _recalc_counts(cur, faq_id)

        cur.execute(
            "SELECT thumbs_up, thumbs_down FROM faq_items WHERE id = %s",
            (faq_id,),
        )
        row = cur.fetchone()
        conn.commit()
        return {
            'thumbs_up': row['thumbs_up'] if row else 0,
            'thumbs_down': row['thumbs_down'] if row else 0,
            'my_vote': vote,
        }
    finally:
        conn.close()


# ==========================================
# 反馈
# ==========================================

def create_feedback(
    client_id: str,
    user_id: int,
    message: str,
    urgency: str = 'low',
    contact: str = '',
    faq_id: Optional[int] = None,
    kind: str = 'faq',
    screenshot_url: str = '',
    ai_answer: str = '',
    submitter_identity: str = '',
    submitter_agent_level: int = 0,
) -> dict:
    """
    新建反馈 · client_id 已存在则返回现有(幂等 · 防离线重发)
    返回 {id, status: 'new'|'existing'}
    """
    if urgency not in ('low', 'mid', 'high'):
        raise ValueError(f"invalid urgency: {urgency}")
    if kind not in ('faq', 'bug'):
        raise ValueError(f"invalid feedback kind: {kind}")
    if submitter_identity not in ('', 'normal_user', 'agent', 'l2', 'admin'):
        raise ValueError(f"invalid submitter_identity: {submitter_identity}")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        # ON CONFLICT DO NOTHING RETURNING id · 已存在则 fetchone 是 None
        cur.execute(
            """INSERT INTO faq_feedback
               (client_id, faq_id, message, urgency, contact, user_id, kind, screenshot_url, ai_answer,
                submitter_identity, submitter_agent_level)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (client_id) DO NOTHING
               RETURNING id""",
            (
                client_id, faq_id, message, urgency, contact, user_id, kind, screenshot_url, ai_answer,
                submitter_identity, int(submitter_agent_level or 0),
            ),
        )
        row = cur.fetchone()
        if row:
            # 新建 · 同时刷 faq_items.feedback_count
            new_id = row['id']
            if faq_id is not None and kind == 'faq':
                _recalc_feedback_count(cur, faq_id)
            conn.commit()
            return {'id': new_id, 'status': 'new'}
        else:
            # 已存在
            cur.execute(
                "SELECT id FROM faq_feedback WHERE client_id = %s",
                (client_id,),
            )
            existing = cur.fetchone()
            conn.commit()
            return {'id': existing['id'] if existing else 0, 'status': 'existing'}
    finally:
        conn.close()


def list_feedback_for_user(user_id: int, limit: int = 50) -> list:
    """提交者查**自己**的反馈状态。

    🔴 [#96 2026-09-05] 刻意**不返回 `admin_note`**:那是**内部备注**字段,
    里面是给运营看的话。把它当「回复」发给提交者 = 内部口径外泄,
    而且没人会为此报警 —— 它长得就像一个正常的文本字段。
    「公开回复」需要一列独立的字段,本单**没有加**(见工单回报)。

    🔴 归属过滤放进 WHERE,不是查出来再比:
    查出来再比的代码,漏一个 return 就把别人的反馈发出去了。
    """
    if not user_id:
        return []
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.kind, f.faq_id, f.message, f.urgency,
                       f.status, f.screenshot_url,
                       f.created_at, f.handled_at
                  FROM faq_feedback f
                 WHERE f.user_id = %s
                 ORDER BY f.created_at DESC, f.id DESC
                 LIMIT %s
                """,
                (int(user_id), int(limit)),
            )
            return [dict(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()


def list_feedback(
    status: Optional[str] = None,
    urgency: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """管理员列表反馈 · 带关联 FAQ question + 用户 username"""
    where = []
    params: list[Any] = []
    if status:
        where.append("f.status = %s")
        params.append(status)
    if urgency:
        where.append("f.urgency = %s")
        params.append(urgency)
    if kind:
        where.append("f.kind = %s")
        params.append(kind)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
              f.id, f.client_id, f.faq_id, f.message, f.urgency, f.contact,
              f.kind, f.screenshot_url, f.ai_answer,
              COALESCE(NULLIF(f.submitter_identity, ''),
                CASE
                  WHEN EXISTS (
                    SELECT 1
                    FROM user_roles ur
                    JOIN roles r ON r.id = ur.role_id
                    WHERE ur.user_id = f.user_id AND r.name = 'admin'
                  ) THEN 'admin'
                  WHEN COALESCE(w.agent_level, 0) >= 2 THEN 'l2'
                  WHEN COALESCE(w.agent_level, 0) >= 1 THEN 'agent'
                  ELSE 'normal_user'
                END
              ) AS submitter_identity,
              CASE
                WHEN f.submitter_agent_level > 0 THEN f.submitter_agent_level
                ELSE COALESCE(w.agent_level, 0)
              END AS submitter_agent_level,
              f.user_id, f.status, f.admin_note,
              f.created_at, f.handled_at, f.handled_by,
              i.question AS faq_question,
              u.username AS user_username,
              u.display_name AS user_display_name
            FROM faq_feedback f
            LEFT JOIN faq_items i ON i.id = f.faq_id
            LEFT JOIN users u ON u.id = f.user_id
            LEFT JOIN user_wallets w ON w.user_id = f.user_id
            {where_sql}
            ORDER BY
              CASE f.urgency WHEN 'high' THEN 0 WHEN 'mid' THEN 1 ELSE 2 END,
              f.created_at DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def update_feedback_status(
    feedback_id: int,
    *,
    status: Optional[str] = None,
    admin_note: Optional[str] = None,
    handled_by: Optional[int] = None,
) -> bool:
    if status is not None and status not in ('pending', 'read', 'done', 'closed'):
        raise ValueError(f"invalid status: {status}")

    fields = []
    params: list[Any] = []
    if status is not None:
        fields.append("status = %s")
        params.append(status)
        # done / closed 时填 handled_at
        if status in ('done', 'closed'):
            fields.append("handled_at = NOW()")
            if handled_by is not None:
                fields.append("handled_by = %s")
                params.append(handled_by)
    if admin_note is not None:
        fields.append("admin_note = %s")
        params.append(admin_note)
    if not fields:
        return False
    params.append(feedback_id)

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE faq_feedback SET {', '.join(fields)} WHERE id = %s RETURNING faq_id",
            params,
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return False
        # 关联 FAQ 的 feedback_count 重算
        if row['faq_id']:
            _recalc_feedback_count(cur, row['faq_id'])
        conn.commit()
        return True
    finally:
        conn.close()


def get_feedback_counts(kind: Optional[str] = None, urgency: Optional[str] = None) -> dict:
    """快速拿各状态反馈数量(给 admin 页面 tab badge 用)"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        where = []
        params: list[Any] = []
        if kind:
            where.append("kind = %s")
            params.append(kind)
        if urgency:
            where.append("urgency = %s")
            params.append(urgency)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(
            f"SELECT status, COUNT(*) AS c FROM faq_feedback {where_sql} GROUP BY status",
            params,
        )
        result = {r['status']: r['c'] for r in cur.fetchall()}
        for s in ('pending', 'read', 'done', 'closed'):
            result.setdefault(s, 0)
        return result
    finally:
        conn.close()
