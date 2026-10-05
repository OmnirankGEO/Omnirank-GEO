"""同媒体双供应商等价映射 —— 生成、查询、目录去重。

工单 WORKORDER_MEDIA_PROVIDER_ROUTING_2026-08-02 §2/§3。

**这一层只回答一个问题**:用户选的这条媒体,另一家有没有同一个媒体、更便宜?
它不碰计价、不碰投递、不碰退款 —— 那三条链一行不改。

--------------------------------------------------------------------------
匹配规则(改它等于改风险敞口,先读 §2 红线)
--------------------------------------------------------------------------
一条 mhz 媒体与一条 kyb 媒体判为同一媒体,必须**同时**满足:

1. ``lower(btrim(名字))`` 相等(软文 ``media_name`` / 自媒体 ``toutiao_name``)
2. 归一化域名相等
3. 两边都 ``is_active``
4. 一对多时只取**价格最低**的那条(``DISTINCT ON``)

然后按金额分档决定能不能自动生效:

============  ==========================================  ==================
档            条件                                        处置
============  ==========================================  ==================
auto_low_diff 价差 ≤ ¥200                                  直接进表
auto_fans_ok  价差 > ¥200 且粉丝佐证通过(仅自媒体有此字段)  直接进表
人工          价差 > ¥200 且佐证不通过 / 无从佐证(软文)     **不进表**,出清单
============  ==========================================  ==================

🔴 高价条目佐证不通过的一律不进表。同名不同资源位真实存在
(历史实测:羊城晚报 我们 ¥460 → 对方 ¥60,166)。切错 = 用户花大钱买的头部位置
被换成边角位,那是交付事故,比多花钱严重得多。

--------------------------------------------------------------------------
🔴 域名口径:两侧都从 ``case_link`` 现算**完整 host**,谁都不读 ``source_domain``
--------------------------------------------------------------------------
**Owner 2026-08-02 拍板改用对称完整 host**(工单 §2 原定的是不对称口径)。

两侧的 ``source_domain`` 语义本就不同,生产实测:

* kyb 侧由 ``media_sync.normalize_source_domain`` 写入 = **注册域(eTLD+1)**
  —— ``baijiahao.baidu.com`` 存成 ``baidu.com``,``mp.weixin.qq.com`` 存成 ``qq.com``
  (自媒体表 82,031 条里 **39,910 条**与完整 host 不同)。
* mhz 侧该列**全空**。

工单 §2 原定"kyb 取 source_domain / mhz 取 case_link host" —— 那是**苹果比橘子**。
换成对称完整 host 后实测三项都变好::

    项                          工单原口径        对称完整 host
    软文 / 自媒体 配对          8,482 / 12,138    14,948 / 26,135
    可省合计                    ¥745,520          ¥1,362,181
    两边真实 host 不同的错配    249 条            0

关键在于**它更具体、不是更宽松**:原口径要求"mhz 完整 host == kyb 注册域"才命中,
这会把 mhz 的 ``baidu.com`` 配到 kyb 的 ``baijiahao.baidu.com`` 头上(实测 249 条)。
对称之后只是让 ``mp.weixin.qq.com`` 能对上自己,同时把那 249 条剔掉。

⚠️ **注册域对称**(两边都取 eTLD+1)是另一回事,**不能用** ——
``toutiao.com`` 底下 18,745 个不同账号名,那样等于主动放大撞名风险。

代价:人工核清单 64 → 130 条(软文 106 + 自媒体 24),Owner 已知悉并接受。

两侧共用同一个常量 ``_HOST_FROM_CASE_LINK`` —— **对称是结构性的**,不是两处碰巧写一样
(``test_domain_expr_is_symmetric`` 锁住)。
且**谁都不读 source_domain**,所以 §6 的回填(写注册域)**不可能**回头改变匹配结果
(``test_backfill_cannot_change_matching`` 锁住)。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger("GEO-MediaRouting")

ARTICLE_TABLE = "mhz_media"
WEMEDIA_TABLE = "mhz_wemedia"
SUPPORTED_TABLES = (ARTICLE_TABLE, WEMEDIA_TABLE)

#: 表 -> 名字列。软文和自媒体存名字的列名不同。
NAME_COLUMN = {ARTICLE_TABLE: "media_name", WEMEDIA_TABLE: "toutiao_name"}

#: 金额分档门槛(元)。🔴 按**绝对金额**分档,不按比值 ——
#: ``20→3`` 这种小站比值 0.15 看着吓人,判错只损失 ¥17;真正该警惕的是绝对额大的。
HIGH_VALUE_THRESHOLD_YUAN = 200

#: 粉丝佐证窗口。mhz 侧 ``fans_num`` 存的是**分档值**不是真实粉丝数,kyb 存真实值,
#: 所以两边差 1000 倍是单位差异,归一化后一致 = 佐证通过。
#:
#: ⚠️ 2026-08-02 实测订正工单 §2:工单称 mhz 侧"观察到的值只有 100/1000/5000/10000/10001",
#:    生产实际是 **9 个值**,且占比最大的是 ``1``(17,973 条)、``50``(4,300)、``5``(2,887)、``10``(720)。
#:    所以"差 1000 倍 = 单位差异"这个解释在全表层面并不成立。
#:    但本信号**只用于高价档加分、绝不用于否决**(对不上多为精度差异,不等于名字撞车),
#:    且实测在高价子集上复现了工单的 492 通过 / 20 不通过,故规则本身照工单实现。
FANS_RATIO_THOUSAND = (999.0, 1001.0)
FANS_RATIO_ONE = (0.8, 1.2)

CONFIDENCE_LOW_DIFF = "auto_low_diff"
CONFIDENCE_FANS_OK = "auto_fans_ok"
CONFIDENCE_HUMAN = "human_approved"

#: 域名表达式 —— **两侧共用同一个常量**,对称是结构性的而不是"两处碰巧写一样"。
#: 从 ``case_link`` 现算**完整 host**,去 ``www.`` 前缀 + 转小写。
#:
#: 🔴 绝不读 ``source_domain``(两侧都不读)。理由见模块头。
_HOST_FROM_CASE_LINK = (
    r"lower(regexp_replace("
    r"substring(case_link from '^https?://([^/:?#]+)'), '^www\.', ''))"
)
#: 保留这两个名字是为了让"两侧用的是同一个表达式"在代码里一眼可见 ——
#: 它们**必须**指向同一个对象,`test_domain_expr_is_symmetric` 锁住这一点。
_MHZ_DOMAIN_EXPR = _HOST_FROM_CASE_LINK
_KYB_DOMAIN_EXPR = _HOST_FROM_CASE_LINK


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------
def is_routing_enabled() -> bool:
    """路由总开关。默认**关**(A8 回滚判据:关掉后行为逐位回到现状)。"""
    return _config_flag("media_provider_routing_enabled", default=False)


def get_saving_share_to_user() -> float:
    """λ —— 成本节省中让给用户的比例。

    本期 Owner 定 **λ=0**(售价对用户零变化),只留口子不启用。

    定价成本 P = 成本_kyb + (1−λ) × (成本_mhz − 成本_kyb)
    λ=0 → P = 成本_mhz → 售价与现状逐字相同。

    🔴 将来启用 λ>0 必须配套做 7 天价格锁,否则售价会随两家成本差实时漂动,
       违反仓内铁律「价格稳定感 ≥ 价格精确度」。本期 λ=0 不受此影响
       (售价锚在 mhz 成本上,与现状一致)。
    """
    from db.meijiehezi_db import get_config

    try:
        raw = get_config("routing_saving_share_to_user")
    except Exception as exc:
        logger.warning("[routing] λ 读取失败,按 0 处理: %s", exc)
        return 0.0
    if raw in (None, ""):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("[routing] λ 值非法 %r,按 0 处理", raw)
        return 0.0
    if not 0.0 <= value <= 1.0:
        logger.warning("[routing] λ 越界 %s,按 0 处理", value)
        return 0.0
    return value


def effective_cost_basis(mhz_cost: float, kyb_cost: float,
                         saving_share: float | None = None) -> float:
    """定价成本 P = 成本_kyb + (1−λ)(成本_mhz − 成本_kyb)。

    λ 真参与计算(不是写死忽略)—— A5b 判据要的就是这个。
    λ=0 时恒等于 ``mhz_cost``,所以本期售价逐字不变。
    """
    lam = get_saving_share_to_user() if saving_share is None else float(saving_share)
    kyb = float(kyb_cost)
    return kyb + (1.0 - lam) * (float(mhz_cost) - kyb)


def _config_flag(key: str, *, default: bool) -> bool:
    from db.meijiehezi_db import get_config

    try:
        raw = get_config(key)
    except Exception as exc:
        logger.warning("[routing] 开关 %s 读取失败,按默认 %s: %s", key, default, exc)
        return default
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# 匹配 SQL
# ---------------------------------------------------------------------------
def build_match_sql(table_name: str) -> str:
    """产出该表的候选配对 SQL。

    🔴 mhz 侧域名从 ``case_link`` 现算,**不读 source_domain** —— 见模块头说明。
    """
    if table_name not in SUPPORTED_TABLES:
        raise ValueError(f"不支持的表: {table_name}")
    name_col = NAME_COLUMN[table_name]
    fans_select_m = ", m.fans_num AS mhz_fans" if table_name == WEMEDIA_TABLE else ""
    fans_select_k = ", k.fans_num AS kyb_fans" if table_name == WEMEDIA_TABLE else ""
    fans_col_m = ", fans_num" if table_name == WEMEDIA_TABLE else ""
    fans_col_k = ", fans_num" if table_name == WEMEDIA_TABLE else ""
    return f"""
WITH m AS (
    SELECT id, price, id AS provider_id{fans_col_m},
           lower(btrim({name_col})) AS nm,
           {_MHZ_DOMAIN_EXPR} AS dom
      FROM {table_name}
     WHERE provider = 'mhz'
       AND is_active
       AND price IS NOT NULL
       AND case_link ~ '^https?://'
), k AS (
    SELECT id, price, provider_media_id AS provider_id{fans_col_k},
           lower(btrim({name_col})) AS nm,
           {_KYB_DOMAIN_EXPR} AS dom
      FROM {table_name}
     WHERE provider = 'kyb'
       AND is_active
       AND price IS NOT NULL
       AND provider_media_id IS NOT NULL
       AND case_link ~ '^https?://'
)
SELECT DISTINCT ON (m.id)
       m.id  AS mhz_media_id,  m.provider_id AS mhz_provider_media_id, m.price AS mhz_price,
       k.id  AS kyb_media_id,  k.provider_id AS kyb_provider_media_id, k.price AS kyb_price,
       m.nm  AS matched_name,  m.dom AS matched_domain{fans_select_m}{fans_select_k}
  FROM m
  JOIN k ON k.nm = m.nm AND k.dom = m.dom
 WHERE m.nm <> '' AND COALESCE(m.dom, '') <> ''
 ORDER BY m.id, k.price ASC, k.id ASC
"""


def fans_corroborated(mhz_fans: Any, kyb_fans: Any) -> bool:
    """粉丝佐证:两侧比值 ≈1000(单位差异)或 ≈1(同口径)。

    只用于给高价条目**加分放行**,绝不用于否决 —— "对不上"多为精度差异,
    不等于名字撞车。
    """
    try:
        m = float(mhz_fans or 0)
        k = float(kyb_fans or 0)
    except (TypeError, ValueError):
        return False
    if m <= 0:
        return False
    ratio = k / m
    return (FANS_RATIO_THOUSAND[0] <= ratio <= FANS_RATIO_THOUSAND[1]
            or FANS_RATIO_ONE[0] <= ratio <= FANS_RATIO_ONE[1])


def classify(row: dict[str, Any], table_name: str) -> tuple[str | None, str]:
    """给一条候选配对定档。

    返回 ``(confidence, preferred_provider)``;``confidence is None`` = 转人工,不进表。
    """
    saving = float(row["mhz_price"]) - float(row["kyb_price"])
    # 取便宜的那家。**持平时选 mhz** —— 成本无差别,改道只增加渠道风险,
    # 且与既有 Owner 口径「先用完媒介盒子」一致(见 routing.split_items_by_provider)。
    preferred = "kyb" if saving > 0 else "mhz"

    if saving <= HIGH_VALUE_THRESHOLD_YUAN:
        return CONFIDENCE_LOW_DIFF, preferred
    # 高价档。软文没有 fans_num,无从佐证 → 一律人工。
    if table_name != WEMEDIA_TABLE:
        return None, preferred
    if fans_corroborated(row.get("mhz_fans"), row.get("kyb_fans")):
        return CONFIDENCE_FANS_OK, preferred
    return None, preferred


# ---------------------------------------------------------------------------
# 生成 / 刷新
# ---------------------------------------------------------------------------
def rebuild_table(table_name: str, *, dry_run: bool = True) -> dict[str, Any]:
    """重算某张表的等价映射。

    ``dry_run=True`` 只统计不写库(默认),便于先看数再决定。
    人工清单(转人工的那些)一并返回,随单交付。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(build_match_sql(table_name))
        candidates = [dict(r) for r in cur.fetchall()]

        auto_rows: list[tuple] = []
        human_rows: list[dict[str, Any]] = []
        for row in candidates:
            confidence, preferred = classify(row, table_name)
            saving = float(row["mhz_price"]) - float(row["kyb_price"])
            if confidence is None:
                human_rows.append({**row, "saving_yuan": round(saving, 2),
                                   "reason": "高价且粉丝佐证不通过" if table_name == WEMEDIA_TABLE
                                             else "高价且无粉丝字段可佐证"})
                continue
            auto_rows.append((
                table_name,
                int(row["mhz_media_id"]), int(row["kyb_media_id"]),
                int(row["mhz_provider_media_id"]), int(row["kyb_provider_media_id"]),
                float(row["mhz_price"]), float(row["kyb_price"]), round(saving, 2),
                preferred, confidence,
                str(row.get("matched_name") or "")[:255],
                str(row.get("matched_domain") or "")[:255],
            ))

        stats = {
            "table": table_name,
            "candidates": len(candidates),
            "auto": len(auto_rows),
            "human": len(human_rows),
            "prefer_kyb": sum(1 for r in auto_rows if r[8] == "kyb"),
            "prefer_mhz": sum(1 for r in auto_rows if r[8] == "mhz"),
            "strictly_cheaper_mhz": sum(1 for r in auto_rows if r[7] < 0),
            "tie": sum(1 for r in auto_rows if r[7] == 0),
            "saving_yuan": round(sum(r[7] for r in auto_rows if r[7] > 0), 2),
            "dry_run": dry_run,
        }
        if dry_run:
            return {**stats, "human_list": human_rows}

        # 全量重算:先清掉本表的自动档(人工核过的 human_approved 保留,那是人的决定),
        # 再灌新的。放同一事务里,避免中途失败留下半张表。
        cur.execute(
            "DELETE FROM media_provider_equivalence "
            " WHERE table_name = %s AND confidence <> %s",
            (table_name, CONFIDENCE_HUMAN),
        )
        if auto_rows:
            from psycopg2.extras import execute_values

            execute_values(
                cur,
                """
                INSERT INTO media_provider_equivalence
                    (table_name, mhz_media_id, kyb_media_id,
                     mhz_provider_media_id, kyb_provider_media_id,
                     mhz_price, kyb_price, saving_yuan,
                     preferred_provider, confidence, matched_name, matched_domain)
                VALUES %s
                ON CONFLICT (table_name, mhz_media_id) DO UPDATE SET
                    kyb_media_id          = EXCLUDED.kyb_media_id,
                    kyb_provider_media_id = EXCLUDED.kyb_provider_media_id,
                    mhz_price             = EXCLUDED.mhz_price,
                    kyb_price             = EXCLUDED.kyb_price,
                    saving_yuan           = EXCLUDED.saving_yuan,
                    preferred_provider    = EXCLUDED.preferred_provider,
                    confidence            = EXCLUDED.confidence,
                    updated_at            = NOW()
                """,
                auto_rows,
                page_size=1000,
            )
        conn.commit()
        return {**stats, "human_list": human_rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 路由查询
# ---------------------------------------------------------------------------
def lookup_cheaper_targets(table_name: str,
                           mhz_media_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """查这批 mhz 媒体有没有**更便宜**的对家。

    🔴 只返回严格更便宜的目标。这条不变量是本单的资金安全底线:
       扣费锚在用户选中那条媒体的价上(``_recompute_publish_charge``,发生在路由之前),
       所以改道到更贵的一家会直接吃掉毛利。持平也不改道(无收益,徒增渠道风险)。
    """
    ids = sorted({int(i) for i in mhz_media_ids or () if i is not None})
    if not ids or table_name not in SUPPORTED_TABLES:
        return {}
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT mhz_media_id, kyb_media_id, kyb_provider_media_id,
                   mhz_price, kyb_price, preferred_provider, confidence
              FROM media_provider_equivalence
             WHERE table_name = %s
               AND is_enabled
               AND preferred_provider = 'kyb'
               AND kyb_price < mhz_price
               AND mhz_media_id = ANY(%s)
            """,
            (table_name, ids),
        )
        return {int(r["mhz_media_id"]): dict(r) for r in cur.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 目录去重
# ---------------------------------------------------------------------------
def apply_catalog_dedupe(table_name: str, *, dry_run: bool = True) -> dict[str, Any]:
    """同名同域两家都在架时,隐藏 kyb 那条、保留 mhz 那条(Owner 选 B)。

    ⚠️ Owner 已知悉代价:用户今天就能看到 kyb 那条更便宜的,B 方案等于把便宜选项
       收起来只留贵的 —— 毛利最大,但用户可见价格相对变贵。改 C(降一部分让给用户)
       只需把 λ 调上去。

    **只对已进映射表的配对去重**:转人工的 64 条两条都留着不动 ——
    那些还没定论,隐藏便宜那条等于既不省钱又少给用户一个选择,是纯损失。

    ``hidden_by_dedupe`` 与 ``is_active`` 分开:后者是上游同步字段,动了会被覆盖。
    """
    from db.connection import get_connection

    if table_name not in SUPPORTED_TABLES:
        raise ValueError(f"不支持的表: {table_name}")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT count(*) AS n FROM {table_name}
             WHERE provider = 'kyb' AND is_active AND NOT hidden_by_dedupe
               AND id IN (SELECT kyb_media_id FROM media_provider_equivalence
                           WHERE table_name = %s AND is_enabled)
            """,
            (table_name,),
        )
        to_hide = int(cur.fetchone()["n"])
        if dry_run:
            return {"table": table_name, "to_hide": to_hide, "dry_run": True}

        cur.execute(
            f"""
            UPDATE {table_name} SET hidden_by_dedupe = TRUE
             WHERE provider = 'kyb' AND is_active AND NOT hidden_by_dedupe
               AND id IN (SELECT kyb_media_id FROM media_provider_equivalence
                           WHERE table_name = %s AND is_enabled)
            """,
            (table_name,),
        )
        hidden = cur.rowcount
        conn.commit()
        return {"table": table_name, "hidden": hidden, "dry_run": False}
    finally:
        conn.close()


def clear_catalog_dedupe(table_name: str) -> int:
    """A8 回滚:把去重隐藏全部撤销,目录逐位回到现状。"""
    from db.connection import get_connection

    if table_name not in SUPPORTED_TABLES:
        raise ValueError(f"不支持的表: {table_name}")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE {table_name} SET hidden_by_dedupe = FALSE WHERE hidden_by_dedupe")
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()
