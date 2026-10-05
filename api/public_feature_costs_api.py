"""官网定价页用的公开功能价目(Review 09-27 · 官网 WO_292 前置)。

GET /api/public/feature-costs —— 免登录(落在 auth 中间件已放行的 /api/public/ 前缀下),只读。
返回在役 GEO 功能的「对外名称 + 每次扣多少算力 + 计价单位」,外加 as_of:
    {"as_of": "2026-09-27T10:00:00+00:00",
     "items": [{"feature_name": "…", "cost_points": 130, "unit": "每篇"}, …]}

口径:
  · cost_points 就是 middleware/billing.py 扣费时读的那个数(db.wallet_db.get_feature_pricing → feature_pricing.cost_points,
    只认 is_active = TRUE)。部分功能在调用时另按用量追加(extra_cost),这里给的是每次的基础扣费。
  · 只列 PUBLIC_GEO_FEATURES 白名单里、当前上架(is_active)且 cost_points > 0 的功能;白名单外的即使上架也不出现,
    社媒功能不上。
  · 不返回 feature_code、cost_compute(语义未核实)、requires_paid_points、任何现金价 / 倍率 / 供应商信息。
  · unit:对外计价单位(白名单常量里逐项写死)。cost_points 是每次的基础扣费,按用量追加的写在 unit 里(「起」「另计」)。
  · Cache-Control: public, max-age=300(官网与 CDN 缓存 5 分钟);进程内另缓存 30 秒,挡住绕过 CDN 的刷量。
    读库失败 ⇒ 503 + 固定文案 + Cache-Control: no-store(错误不进缓存)。
锁:tests/public_feature_costs_2026_09_27
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response

logger = logging.getLogger("GEO-PublicFeatureCosts")

router = APIRouter(prefix="/api/public", tags=["公开价目"])

#: 在役 GEO 功能白名单:(feature_code, 对外名称, 对外计价单位)。官网定价页只列这些,按列表顺序输出。
#: 每一项的出处(左侧栏入口 + 扣费路由)与单位依据(调用时怎么按用量追加)见交付单 C14_313。
#: 对外名称由 a4 定稿、Review 核过:C:/AI-Test/开源/WO_290b_PRICING_PUBLIC_NAMES_2026-09-27.md(sha256 前缀 f3eca5e7c880)。
#: 接口只回对外名称,不回库里的内部 feature_name(库里改名不影响官网)。
#: 🔴 改这张表 = 改官网对外展示的功能清单、名称与计价口径:社媒功能、E3 删除清单里的功能、没有左侧栏在役入口的功能都不许加;
#:    单位只写计量方式,不写倍率、不写现金。
PUBLIC_GEO_FEATURES: tuple[tuple[str, str, str], ...] = (
    ("geo_diagnosis", "GEO 专项诊断", "每次起,自定义题数另计"),                  # 品牌体检;自定义题按题追加
    ("report_regen", "诊断报告重新生成", "每次"),                                # 诊断报告 → 重新生成
    ("report_export", "诊断报告导出(PDF / PPTX)", "每次"),                      # 诊断报告 → 导出
    ("topic_gen", "选题生成", "每个关键词"),                                      # AI 创作中心 → 生成选题,按关键词数计
    ("article_gen", "GEO 文章写作", "每篇"),                                     # AI 创作中心 → 写文章,按篇计
    ("article_rewrite", "GEO 文章补发 / 重写", "每篇"),                          # AI 创作中心 → 补发 / 重写,按篇计
    ("monitor_single", "单次监测", "每词每次"),                                   # 效果监测 → 单次检测,按关键词计
    ("monitoring_keyword_daily", "关键词每日监测", "每词每天"),                   # 效果监测 → 关键词每日监测
    ("geo_research_selfserve", "GEO 单行业自助调研", "每次起,超出 15 题另计"),   # 发布投放 → 单行业自助调研;基础价含 15 题
                                                                                  #   (锁对着 research_selfserve_api.SELFSERVE_BASE_INCLUDED_PROMPTS)
    ("deep_analyze", "品牌行业深度解析", "每次"),                                 # 我的客户 → 品牌深度行业解析
    ("industry_brief_rerun", "行业知识库字段重新生成", "每次"),                    # 我的客户 → 知识库字段重跑
    ("brand_fill", "品牌信息 AI 一键填充", "每次"),                               # 我的客户 → 品牌信息 AI 填充
    ("autofill_brand", "客户资料 AI 补齐空缺字段", "每次最多,按实际补齐的字段结算"),  # 我的客户 → 客户资料 AI 补齐(先扣满额、按补齐字段退差)
)
PUBLIC_GEO_FEATURE_CODES: tuple[str, ...] = tuple(c for c, _n, _u in PUBLIC_GEO_FEATURES)

# 撤回记录:r3 曾不列 geo_research_selfserve,理由已撤(基础价读 cost_points,extra_cost 只装超题加价 · Review 09-27 r4)。

CACHE_CONTROL = "public, max-age=300"
UNAVAILABLE = "价目暂时不可用,请稍后再试"
_MEMO_SECONDS = 30.0
_memo_lock = threading.Lock()
_memo: dict = {"at": 0.0, "body": None}


def _read() -> dict:
    from db.connection import get_connection

    public = {c: (n, u) for c, n, u in PUBLIC_GEO_FEATURES}
    codes = list(public)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        # cost_points = 0 不上:这张表里 0 是「价格在别处」的占位(如自助调研的种子值 0、实价由生产配置),
        # 对外写「0 算力」就是错的
        cur.execute("""SELECT feature_code, cost_points FROM feature_pricing
                        WHERE is_active = TRUE AND cost_points > 0 AND feature_code = ANY(%s)""", (codes,))
        rows = {r["feature_code"]: r for r in cur.fetchall()}
        conn.rollback()
    finally:
        conn.close()
    # feature_name 键填对外名称(官网不用改);库里的内部名不出库
    items = [{"feature_name": public[c][0], "cost_points": int(rows[c]["cost_points"]), "unit": public[c][1]}
             for c in codes if c in rows]
    return {"as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items}


def reset_cache() -> None:
    with _memo_lock:
        _memo["at"], _memo["body"] = 0.0, None


@router.get("/feature-costs", summary="在役 GEO 功能的算力价目(官网定价页)")
def public_feature_costs(response: Response) -> dict:
    now = time.monotonic()
    with _memo_lock:
        if _memo["body"] is not None and now - _memo["at"] < _MEMO_SECONDS:
            body = _memo["body"]
        else:
            body = None
    if body is None:
        try:
            body = _read()
        except Exception as exc:        # noqa: BLE001  对外只给固定文案
            logger.error("[public-feature-costs] 读价目失败:%s: %s", type(exc).__name__, exc)
            # 固定文案、不带异常细节;no-store:别让 CDN / 浏览器把错误缓存 5 分钟
            raise HTTPException(status_code=503, detail=UNAVAILABLE, headers={"Cache-Control": "no-store"}) from None
        with _memo_lock:
            _memo["at"], _memo["body"] = now, body
    response.headers["Cache-Control"] = CACHE_CONTROL
    return body
