"""价目表版本化 SSOT(§5 / §9.4 / §10.2)

两个逻辑现金价目表(procurement / retail)+ 功能消耗目录(feature_consumption)统一用
pricing_catalog_versions(版本头)+ pricing_catalog_entries(明细)承载。

不变量:
  - 已发布价格不可原地改金额;改价=发新版本;旧版本 archived 仍供历史订单读取(§9.4)。
  - 同一(catalog_type, scope_key)任一时刻只有一个开放 published 版本(DB 排他索引 + 事务锁)。
  - 全部整数分 / 整数 bps;final_price_cents 是"全链只取整一次"的结果。
  - 发布时执行 §11 护栏:低于成本底线 → 阻断;极端高价无审批 → 阻断。

红线:不碰 billing/connection/auth;不写税字段;整数分。
"""

import logging
from typing import Any, Dict, List, Optional

from db.connection import get_db
from config.pricing_config import get_margin_label_thresholds

logger = logging.getLogger("GEO-PricingCatalog")

VALID_TYPES = ("procurement", "retail", "feature_consumption")


# ------------------------------------------------------------------ 整数分工具
def apply_multiplier_cents(base_cents: int, multiplier_bps: int) -> int:
    """final = ceil(base_cents * multiplier_bps / 10000)。全链只在此取整一次(§5.1.8)。
    [审核 P2] 纯整数天花板除法(不经 IEEE-754 float · 大额也精确)· 满足"整数分"铁律。"""
    base_cents = int(base_cents); multiplier_bps = int(multiplier_bps)
    if base_cents < 0 or multiplier_bps < 0:
        raise ValueError("base_cents/multiplier_bps 不得为负")
    return (base_cents * multiplier_bps + 9999) // 10000


# ------------------------------------------------------------------ 读取(SSOT 入口)
def get_published_version(catalog_type: str, scope_key: str, cur=None) -> Optional[Dict[str, Any]]:
    """当前开放 published 版本头。cur 传入则复用事务(下单锁价路径)。"""
    if catalog_type not in VALID_TYPES:
        raise ValueError(f"invalid catalog_type {catalog_type}")
    sql = """
        SELECT * FROM pricing_catalog_versions
        WHERE catalog_type = %s AND scope_key = %s AND status = 'published' AND effective_to IS NULL
        LIMIT 1
    """
    if cur is not None:
        cur.execute(sql, (catalog_type, scope_key))
        row = cur.fetchone()
        return dict(row) if row else None
    with get_db() as conn:
        c = conn.cursor()
        c.execute(sql, (catalog_type, scope_key))
        row = c.fetchone()
        return dict(row) if row else None


def get_published_catalog(catalog_type: str, scope_key: str) -> Optional[Dict[str, Any]]:
    """返回 {version, items:[entry...]}。无已发布版本 → None(调用方须 fail-closed 拒绝下单)。"""
    with get_db() as conn:
        cur = conn.cursor()
        ver = get_published_version(catalog_type, scope_key, cur=cur)
        if not ver:
            return None
        cur.execute(
            "SELECT * FROM pricing_catalog_entries WHERE version_id = %s ORDER BY product_code",
            (ver["id"],),
        )
        items = [dict(r) for r in cur.fetchall()]
        return {"version": ver, "items": items}


def get_published_entry(catalog_type: str, scope_key: str, product_code: str, cur=None) -> Optional[Dict[str, Any]]:
    """单商品已发布明细(报价锁价用)。cur 复用事务。"""
    ver = get_published_version(catalog_type, scope_key, cur=cur)
    if not ver:
        return None

    def _q(c):
        c.execute(
            "SELECT * FROM pricing_catalog_entries WHERE version_id = %s AND product_code = %s",
            (ver["id"], product_code),
        )
        r = c.fetchone()
        return (dict(r), ver) if r else (None, ver)

    if cur is not None:
        return _q(cur)
    with get_db() as conn:
        return _q(conn.cursor())


# ------------------------------------------------------------------ §11 护栏
def _gate_entry(entry: Dict[str, Any], *, is_retail: bool, approved: bool) -> Optional[str]:
    """返回 None=放行;返回字符串=阻断原因。§11.1 强制阻断 + §11.2 极端价需审批。"""
    final = int(entry.get("final_price_cents", 0))
    points = int(entry.get("paid_points", 0))
    if points <= 0:
        return "商品算力必须>0"
    if final <= 0:
        return "价格必须>0"
    if is_retail:
        floor = entry.get("cost_floor_cents")
        if floor is None:
            return "零售条目缺成本底线 cost_floor_cents"
        if final < int(floor):
            return f"最终售价 {final} 低于有效成本底线 {floor}(§11.1 强制阻断)"
        # 极端高价:毛利率 bps 超过 margin_label_thresholds.hard_block_bps → 必须人工审批
        thresholds = get_margin_label_thresholds()
        hard_block_bps = int(thresholds.get("hard_block_bps", 250000))
        if int(floor) > 0:
            margin_bps = int(round((final - int(floor)) * 10000 / int(floor)))
            if margin_bps >= hard_block_bps and not approved:
                return f"毛利率 {margin_bps}bps ≥ 极端阈 {hard_block_bps}bps 且无审批(§11.2 人工审批)"
    return None


# ------------------------------------------------------------------ 版本生命周期
def create_draft_version(
    *,
    catalog_type: str,
    scope_key: str,
    version_code: str,
    entries: List[Dict[str, Any]],
    reason: str = "",
    created_by: Optional[int] = None,
    calc_meta: Optional[Dict[str, Any]] = None,
) -> int:
    """建草稿版本 + 明细。entries 每项: product_code/base_price_cents/multiplier_bps/paid_points
    /bonus_points?/cost_floor_cents?/usage_example_version?。final 由后端算(不信前端)。"""
    import json
    if catalog_type not in VALID_TYPES:
        raise ValueError(f"invalid catalog_type {catalog_type}")
    if not entries:
        raise ValueError("空目录不得建版本")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO pricing_catalog_versions
               (catalog_type, scope_key, version_code, status, reason, created_by, calc_meta_jsonb)
               VALUES (%s,%s,%s,'draft',%s,%s,%s) RETURNING id""",
            (catalog_type, scope_key, version_code, reason, created_by,
             json.dumps(calc_meta) if calc_meta else None),
        )
        version_id = cur.fetchone()["id"]
        for e in entries:
            base = int(e["base_price_cents"])
            mult = int(e.get("multiplier_bps", 10000))
            # [审核 P2] final 一律后端算(base×系数),不信任 caller 传入的 final_price_cents
            #   —— 兑现契约"final 由后端算·不信前端",防前端/误调传入与 base×mult 不符的价。
            final = apply_multiplier_cents(base, mult)
            cur.execute(
                """INSERT INTO pricing_catalog_entries
                   (version_id, product_code, base_price_cents, multiplier_bps, final_price_cents,
                    paid_points, bonus_points, cost_floor_cents, usage_example_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (version_id, e["product_code"], base, mult, final,
                 int(e["paid_points"]), int(e.get("bonus_points", 0)),
                 e.get("cost_floor_cents"), e.get("usage_example_version")),
            )
        return version_id


def validate_version(version_id: int, *, approved: bool = False) -> List[str]:
    """规则校验:返回阻断原因列表(空=可发布)。"""
    problems: List[str] = []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT catalog_type FROM pricing_catalog_versions WHERE id = %s", (version_id,))
        v = cur.fetchone()
        if not v:
            return ["版本不存在"]
        is_retail = v["catalog_type"] == "retail"
        cur.execute("SELECT * FROM pricing_catalog_entries WHERE version_id = %s", (version_id,))
        for e in cur.fetchall():
            reason = _gate_entry(dict(e), is_retail=is_retail, approved=approved)
            if reason:
                problems.append(f"{e['product_code']}: {reason}")
    return problems


def publish_version(
    version_id: int, *, effective_from=None, approved_by: Optional[int] = None
) -> Dict[str, Any]:
    """原子发布:校验 → 归档当前开放 published(effective_to=new.from)→ 本版 published+开放。
    §9.4:旧版本不删,进 archived;§9.4.6 DB 排他索引兜底"任一时刻一个开放版本"。"""
    problems = validate_version(version_id, approved=approved_by is not None)
    if problems:
        raise ValueError("发布校验未通过: " + "; ".join(problems))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pricing_catalog_versions WHERE id = %s FOR UPDATE", (version_id,))
        ver = cur.fetchone()
        if not ver:
            raise ValueError("版本不存在")
        if ver["status"] == "published":
            return dict(ver)  # 幂等
        if ver["status"] == "archived":
            raise ValueError("已归档版本不能直接发布,请回滚为新版本")
        # 锁定同 scope 当前开放 published 并归档
        cur.execute(
            """SELECT id FROM pricing_catalog_versions
               WHERE catalog_type=%s AND scope_key=%s AND status='published' AND effective_to IS NULL
               FOR UPDATE""",
            (ver["catalog_type"], ver["scope_key"]),
        )
        eff_from_sql = "COALESCE(%s, NOW())"
        cur.execute(
            f"""UPDATE pricing_catalog_versions
                SET status='archived', effective_to={eff_from_sql}, archived_at=NOW(), updated_at=NOW()
                WHERE catalog_type=%s AND scope_key=%s AND status='published' AND effective_to IS NULL""",
            (effective_from, ver["catalog_type"], ver["scope_key"]),
        )
        cur.execute(
            f"""UPDATE pricing_catalog_versions
                SET status='published', effective_from={eff_from_sql}, effective_to=NULL,
                    approved_by=%s, approved_at=NOW(), published_at=NOW(), updated_at=NOW()
                WHERE id=%s RETURNING *""",
            (effective_from, approved_by, version_id),
        )
        return dict(cur.fetchone())


def rollback_to(source_version_id: int, *, new_version_code: str, created_by: Optional[int] = None,
                approved_by: Optional[int] = None) -> Dict[str, Any]:
    """回滚=把旧版本内容复制为新版本再发布(§9.4.5 不删历史)。"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pricing_catalog_versions WHERE id=%s", (source_version_id,))
        src = cur.fetchone()
        if not src:
            raise ValueError("源版本不存在")
        cur.execute("SELECT * FROM pricing_catalog_entries WHERE version_id=%s", (source_version_id,))
        entries = [
            {"product_code": r["product_code"], "base_price_cents": r["base_price_cents"],
             "multiplier_bps": r["multiplier_bps"], "final_price_cents": r["final_price_cents"],
             "paid_points": r["paid_points"], "bonus_points": r["bonus_points"],
             "cost_floor_cents": r["cost_floor_cents"], "usage_example_version": r["usage_example_version"]}
            for r in cur.fetchall()
        ]
    new_id = create_draft_version(
        catalog_type=src["catalog_type"], scope_key=src["scope_key"], version_code=new_version_code,
        entries=entries, reason=f"rollback from version {source_version_id}", created_by=created_by,
    )
    return publish_version(new_id, approved_by=approved_by)


def list_versions(catalog_type: str, scope_key: str, limit: int = 50) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, catalog_type, scope_key, version_code, status, effective_from, effective_to,
                      reason, approved_by, created_at
               FROM pricing_catalog_versions WHERE catalog_type=%s AND scope_key=%s
               ORDER BY created_at DESC LIMIT %s""",
            (catalog_type, scope_key, limit),
        )
        return [dict(r) for r in cur.fetchall()]
