"""GEO 抖音图文管线 v1 · 配置 SSOT(仅依赖 os,不 import 三方)

两类配置:
  A. 内容口径常量 —— 全部来自 §6 深挖实证(见 docs/AI-CONTEXT/DOUYIN_ADOPTED_CONTENT_PATTERNS_2026-08.md),
     不是拍脑袋。凡与工单原文有出入的,注释里写清"工单写 X / 实证 Y / 取 Y"。
  B. 抖音侧频控 —— 工单 §4.2 要求"可配置(settings)不硬编码",走环境变量,
     与兄弟模块 services/meijiehezi/config.py 同一套写法。

🔴 本模块【不】重复定义任何已有 SSOT:
  - 发布相关(标题硬上限/article_type/白名单域)一律 import 自 services.meijiehezi.config;
  - 价目走 feature_pricing 表(价目表 SSOT 铁律 1),这里只放 feature_code 字符串。
"""
from __future__ import annotations

import os

# ─────────────────────────────────────────────────────────────
# A. 内容口径(§6 实证校准)
# ─────────────────────────────────────────────────────────────

# 卡片张数:实证中位 4.5,3-9 张占 59%,但【单图占 23%】→ 下限必须放到 1
# 工单 §2 写"3-9 张";实证支持上限 9,但下限 3 会砍掉 23% 的真实形态 → 取 1-9
CARD_COUNT_MIN = 1
CARD_COUNT_MAX = 9
# 🔴 默认 4(Owner 2026-08-03 拍板)。二次实证复核支持这个值:
#    TIKHUB 拉到的被采纳**图文帖** n=56 → 中位 **4 张**(均值 4.7);
#    对照组(仅被检索没被采纳)n=61 中位同样是 4。
#    ≤4 张覆盖 51.8%,≤9 张覆盖 91.1% → 默认 4 / 上限 9。
CARD_COUNT_DEFAULT = 4
# 套餐**含**几张。超出的部分按 feature_pricing 里的每张单价加价。
# 🔴 少于这个数**不减价**(Owner 2026-08-03 明确):基础价买的是"一组",
#    不是"按张零售"。计价一律走 services/geo_douyin/pricing.py 的唯一计算处。
CARD_COUNT_INCLUDED = 4

# 正文字数:工单 §2 写 200-500 字;实证 desc 中位【91 字】,>200 字的仅约两成
# → 取 80-200 为主,上限放到 300(超长反而偏离被采纳形态)
BODY_LEN_MIN = 80
BODY_LEN_TARGET = 160
BODY_LEN_MAX = 300

# hashtag:工单 §2 写"3-5 个";实证众数就是 5(90% 带标签,分布 5 个占 57/110)
# → 收窄为定值 5(仍保留上下限给调用方兜底)
HASHTAG_COUNT_MIN = 3
HASHTAG_COUNT_DEFAULT = 5
HASHTAG_COUNT_MAX = 7  # desc 原文实测最多见到 7 个

# 城市变体矩阵:工单 §2 要求"同内容至少出 5 个城市变体标题"
CITY_VARIANT_DEFAULT = 5
CITY_VARIANT_MAX = 20

# ── 画幅(杂志级连续组图规范 §8.8:3:4 与 9:16 两族并存,客户可自选)──
# 🔴 规范原文:「本 3:4 系列是**新增一族,不替代** 9:16 十母版 —— 两族并存,
#    系统默认规划、客户可自选(Owner 08-02 已拍)。抖音图文流主投 3:4,
#    9:16 保留给竖屏场景。」在此之前 `_IMAGE_SIZE = "3:4"` 是写死的,
#    "可自选"那一层根本不存在。
#
# 🔴 `size` 是**给 provider 的参数**(它只收宽高比 + 分辨率档,不收像素);
#    `spec` 是**写进 prompt 的排版提示**。两者不是一回事,别合并:
#    像素数字对 provider 无效,它只对模型的构图有暗示作用。
# ⚠️ 这里的像素数字是**提示语,不是契约** —— 真实输出像素至今没实测
#    (本机没有生图 key,见交付单 §5.1 挂账)。不要拿它当尺寸保证。
ASPECT_RATIOS = {
    "3:4": {
        "label": "竖版 3:4",
        "hint": "抖音图文默认，信息密度高",
        "size": "3:4",
        "spec": "竖版 3:4 构图，1080×1440 像素，移动端缩略图状态下主标题仍要清晰可读",
    },
    "9:16": {
        "label": "全屏 9:16",
        "hint": "竖屏满屏场景，沿用十母版那一族",
        "size": "9:16",
        "spec": "竖版 9:16 构图，1080×1920 像素，移动端缩略图状态下主标题仍要清晰可读",
    },
}
ASPECT_RATIO_DEFAULT = "3:4"


def normalize_aspect_ratio(value) -> str:
    """把外部传来的画幅收敛到白名单。

    🔴 不认识的值一律落到默认,**不抛错也不透传** ——
       透传等于让用户随便给 provider 传字符串。
    """
    key = str(value or "").strip()
    return key if key in ASPECT_RATIOS else ASPECT_RATIO_DEFAULT

# ─────────────────────────────────────────────────────────────
# B. 抖音侧频控(工单 §4.2 · 可配置不硬编码)
# ─────────────────────────────────────────────────────────────


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def douyin_per_account_daily_limit() -> int:
    """单账号日发上限。防平台判重复铺量。"""
    return _int_env("GEO_DOUYIN_ACCOUNT_DAILY_LIMIT", 3)


def douyin_same_content_min_city_gap() -> int:
    """同一条内容投多个账号时,要求城市变体错开的最小数量。

    = 1 表示不强制;>1 表示同内容同城市最多只能投 N-1 ... 具体判定在 publish_adapter。
    """
    return _int_env("GEO_DOUYIN_SAME_CONTENT_MIN_CITY_GAP", 2)


def douyin_batch_max_accounts() -> int:
    """单次提交最多选多少个账号(防一把梭)。"""
    return _int_env("GEO_DOUYIN_BATCH_MAX_ACCOUNTS", 10)


def image_gen_concurrency() -> int:
    """生图全局并发上限。工单 §3.2。

    [2026-08-02 按真实额度重定 3 → 8]
    原来的 3 是保守拍的,**不是供应商限制** —— provider 文档里根本没有并发限制,
    只有 RPM 500(每分钟请求数)。换算:

        改轮询策略前:每张在飞图 ≈ 20 req/min → 500 RPM 只够 ~25 张
        改之后(首轮按 estimated_time 延迟 + 间隔 5s):≈ 5.4 req/min → 可撑 ~93 张

    所以额度侧完全不是瓶颈,3 只用掉 12%。
    🔴 但**额度上限 ≠ 吞吐上限**:实测同一 prompt 快的 49s、慢的 >240s(差 5 倍),
       这种波动更像供应商侧排队 —— 并发提上去单张耗时可能一起涨,总吞吐未必线性。
       所以这次只提到 8 先跑实测,不一步到位到 20/90。要再往上走请拿实测数据说话。
    """
    return _int_env("GEO_DOUYIN_IMAGE_CONCURRENCY", 8)


# ─────────────────────────────────────────────────────────────
# C. 计费 feature_code(真实价目在 feature_pricing 表 · 价目表 SSOT)
# ─────────────────────────────────────────────────────────────
# B 类异步长任务:freeze_points(feature_code) → commit_freeze / release_freeze
FEATURE_CODE_IMAGE_POST = "geo_douyin_image_post"
# 重新生成走独立档位(便宜一档),与写文章的 article_gen / article_rewrite 两档结构对齐
FEATURE_CODE_IMAGE_POST_REGEN = "geo_douyin_image_post_regen"
# 超出套餐张数的**每张**加价。首次制作与重新生成**共用**同一个单价
# (Owner 2026-08-03:「2、需要」= 重新生成也按张数加价)。
FEATURE_CODE_IMAGE_POST_EXTRA_CARD = "geo_douyin_image_post_extra_card"
# 单张重抽。🔴 2026-08-03 由**免费**改为收费(Owner 拍板「3、单张重抽按100算力」)。
#    真实单价照旧在 feature_pricing,这里只有 code。
FEATURE_CODE_IMAGE_POST_REDRAW = "geo_douyin_image_post_redraw"
# AI 一键蒸馏选题。🔴 2026-08-03 由**免费**改为收费(Owner 拍板「AI蒸馏收130算力」)。
#    与上面几个不同,它是 **A 类同步短任务**:charge_on_success(成功才扣),
#    不走冻结 —— 产出同步返回,没有"后台跑着要占位"这回事。
FEATURE_CODE_TOPIC_DISTILL = "geo_douyin_topic_distill"


# ─────────────────────────────────────────────────────────────
# D. 后台任务(异步化)· 时间常量全部来自【生产实测】,不是拍脑袋
# ─────────────────────────────────────────────────────────────
# 🔴 为什么必须异步:生产 nginx 对 /api/geo-douyin/* 走默认 `location /api/`,
#    proxy_read_timeout = 60s。而一组卡实测 ≈137s(封面串行 63.8s + 其余并发 73s)
#    → 同步跑**必然 504**,不是"可能超时"。这条链在异步化之前生产上跑不通。
#
# 实测值(2026-08-02 生产分跳实测):封面 63.8s / 内容卡 65.3s / 收尾卡 72.9s。
# 取 68s 作单张估算基数 —— 只用来算"预计还要多久",不参与任何判定/计费。
CARD_SECONDS_ESTIMATE = 68
# 文案阶段(LLM 写正文 + 卡片要点)实测 ~25s
COPY_SECONDS_ESTIMATE = 25

# 多久没有任何进展就当"可能已中断"。
# 🔴 存在的理由:进程重启会让内存里的后台任务直接消失,而任务行还停在 running。
#    没有这条判定,前端会**永远转圈**。到点后前端停轮询、给重试入口。
#    冻结的积分由平台既有的 services/freeze_sweeper.py(每小时,12h+ 自动 release)
#    兜底回收 —— 本单不自建第二套资金清扫(资金面只标记不自改)。
TASK_STALE_SECONDS = 900

# 单个后台任务的硬上限。到点即取消,走统一失败出口退款,防"永远跑下去"占着冻结。
TASK_TOTAL_TIMEOUT_SECONDS = 1800


def is_pipeline_enabled() -> bool:
    """总闸。未开时 API 返回"即将开放",绝不扣费/生成(与 svideo 开闸同款形态)。"""
    return str(os.getenv("GEO_DOUYIN_PIPELINE_ENABLED", "0")).strip().lower() in (
        "1", "true", "yes", "on",
    )
