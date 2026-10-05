"""监测/写作关键词卫生闸(WO_DELIVERY_FLYWHEEL_CLOSURE §2.4.2)。

起因(2026-08-06 生产实测):晨光富士的付费监测配置里躺着两个错别字词 ——
「东菀载货电梯哪家好」(68 次测试)与「东菀载人电梯哪家好」(32 次测试)。
正确的字是**东莞**。这些词每天都在花 4 引擎的钱跑,测的却是一个不存在的地名。

⚠️ 工单里「0/36 永不中」那句**已被实测证伪**:这两个词分别命中 3 次和 2 次
(AI 能猜出用户想问东莞)。所以理由不是"永远不中",而是:
  ① 花钱测一个错的地名,命中与否都不能作为该地区可见度的证据;
  ② 报告给客户看到错别字,直接损伤专业度。

闸的口径(刻意不做"全量中文拼写检查",那既做不准也拦不住)::

  典型错字 = **行政区名的一字之差**。这类词可枚举、可判定、可给出确定的更正建议。
  判据:词里切出的候选地名 token,若本身不是已知地名,但与某个已知地名**编辑距离为 1**
  且长度一致 → 判为错字,并给出唯一更正。距离 ≥2 或有多个等距候选 → **不判**(宁可漏,
  不可乱改客户的词)。

三条规则的严格度是分开的(遵循 Owner「非必要不警告」与「提示要么帮人解决要么不显示」)::

  - `typo`        **阻断**。因为它带着确定的更正建议 —— 提示能帮人一键解决,才有资格拦。
  - `too_few`     **提示**。词数下限是经验值,拦下来只会挡住正常业务。
  - `red_ocean`   **提示**。红海词不是错,是选择;带上实测命中率让人自己判。

本模块**纯函数 + 只读查询**,不写任何表。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger("GEO-KeywordHygiene")

# 每品牌监测词数量下限(低于此只提示,不阻断)。
MIN_KEYWORDS_PER_BRAND = 3

# 红海判定:同一个词在生产里被测过 N 次以上且命中率低于 R → 提示。
RED_OCEAN_MIN_TESTS = 100
RED_OCEAN_MAX_HIT_RATE = 0.05

# 已知地名表。**故意只收地级市及以上 + 广东省内区县** —— 覆盖当前客户盘,
# 且保持"一字之差"判据的精度:表越大,误判为错字的概率越高。
# 新增客户地区时往这里加,别去放宽编辑距离阈值。
KNOWN_PLACE_NAMES: frozenset[str] = frozenset({
    # 广东地级市
    "广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆", "江门", "茂名",
    "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞", "中山", "潮州", "揭阳", "云浮",
    # 深圳行政区
    "福田", "罗湖", "南山", "宝安", "龙岗", "盐田", "龙华", "坪山", "光明", "大鹏",
    # 广州行政区
    "越秀", "海珠", "天河", "白云", "黄埔", "番禺", "花都", "南沙", "从化", "增城",
    # 东莞主要镇街(客户在东莞投放)
    "松山湖", "虎门", "长安", "厚街", "常平", "塘厦", "凤岗", "寮步", "大朗",
    # 其他常见目标城市
    "北京", "上海", "天津", "重庆", "杭州", "南京", "苏州", "无锡", "宁波", "合肥",
    "成都", "武汉", "西安", "长沙", "郑州", "青岛", "济南", "厦门", "福州", "南昌",
    "昆明", "贵阳", "南宁", "海口", "三亚", "沈阳", "大连", "哈尔滨", "长春", "石家庄",
    "太原", "兰州", "银川", "西宁", "乌鲁木齐", "拉萨", "呼和浩特", "温州", "泉州", "惠阳",
})

# 形近/音近混淆字表 —— 阻断闸的**精度闸门**,不是可选的锦上添花。
#
# 🔴 为什么必须有它(开发时实测出来的坑):
#    光靠"与已知地名编辑距离为 1"会**误伤并阻断真实业务词**:
#      「大型载货电梯哪家好」→「大型」与「大鹏」距离 1 → 被判错字 → 用户被拦
#      「长期维保」→「长期」与「长安」距离 1;「海外案例」→「海外」与「海口」距离 1
#    阻断型闸的误报代价远高于漏报(用户被挡在门外,还被告知一个荒谬的更正),
#    所以判据必须再加一道:**变化的那一个字必须是形近或音近**。
#    「菀→莞」同为艹头、字形几乎一致(真实错字);「型→鹏」毫无关系(误报)。
#
# 覆盖面是**刻意保守且可扩充**的:这里只收中文地名里真实高频的混淆对。
# 漏掉某个错字 → 加一对进来;绝不通过放宽编辑距离来提高召回。
_CONFUSABLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("菀", "莞"),   # 东菀 / 东莞 —— 2026-08-06 生产实测的那个
    ("管", "莞"),
    ("川", "圳"),   # 深川 / 深圳
    ("训", "圳"),
    ("夏", "厦"),   # 夏门 / 厦门
    ("汕", "山"),   # 汕头 / 山头
    ("朱", "珠"),   # 朱海 / 珠海
    ("慧", "惠"),   # 慧州 / 惠州
    ("占", "湛"),   # 占江 / 湛江
    ("隆", "龙"),   # 隆岗 / 龙岗
    ("冈", "岗"),   # 龙冈 / 龙岗
    ("萝", "罗"),   # 萝湖 / 罗湖
    ("盐", "塩"),
    ("芜", "无"),
    ("肇", "兆"),   # 兆庆 / 肇庆
    ("揭", "碣"),   # 碣阳 / 揭阳
    ("潮", "朝"),   # 朝州 / 潮州
    ("番", "翻"),   # 翻禺 / 番禺
    ("禺", "偶"),
    ("屿", "与"),
)
_CONFUSABLE: dict[str, set[str]] = {}
for _a, _b in _CONFUSABLE_PAIRS:
    _CONFUSABLE.setdefault(_a, set()).add(_b)
    _CONFUSABLE.setdefault(_b, set()).add(_a)


def _iter_cjk_candidates(text: str):
    """滑窗切 2/3 字候选。

    🔴 不能用 `re.findall(r'[一-鿿]{2,3}')` —— 那是**非重叠**切分:
       「东菀载货电梯哪家好」会被切成「东菀载」「货电梯」「哪家好」,
       真正的「东菀」一次都不会出现。开发时第一版就是这么写的,测试当场红。
    """
    runs = re.findall(r"[一-鿿]+", str(text or ""))
    for run in runs:
        for size in (2, 3):
            for start in range(0, len(run) - size + 1):
                yield run[start:start + size]


def _edit_distance_one(a: str, b: str) -> bool:
    """等长且恰好一个字符不同。**刻意不做插入/删除** —— 「东莞」vs「东菀」是替换型,
    而放开插入删除会让「深圳」和「深圳市」互判为错字,制造噪声。"""
    if len(a) != len(b) or a == b:
        return False
    diff = 0
    for ch_a, ch_b in zip(a, b):
        if ch_a != ch_b:
            diff += 1
            if diff > 1:
                return False
    return diff == 1


def _confusable_substitution(token: str, place: str) -> bool:
    """两串恰好一字之差,且**那一个字是形近/音近对**。不在表里 → 判否(宁漏不误伤)。"""
    if not _edit_distance_one(token, place):
        return False
    for ch_token, ch_place in zip(token, place):
        if ch_token != ch_place:
            return ch_place in _CONFUSABLE.get(ch_token, set())
    return False


def detect_place_typo(keyword: str, *, extra_known: Iterable[str] = ()) -> dict[str, Any] | None:
    """返回 {'token', 'suggestion'} 或 None。

    判据是**两个条件同时成立**:
      ① token 与某个已知地名恰好一字之差;
      ② 变化的那一个字在形近/音近表 `_CONFUSABLE` 里。
    只满足 ① 不算 —— 见 `_CONFUSABLE_PAIRS` 上方关于「大型/大鹏」误伤的说明。
    多个等距候选 → None(判不准就不判,绝不乱改客户的词)。
    """
    known = set(KNOWN_PLACE_NAMES) | {str(x).strip() for x in extra_known if str(x).strip()}
    for token in _iter_cjk_candidates(keyword):
        if token in known:
            continue
        candidates = sorted({name for name in known if _confusable_substitution(token, name)})
        if len(candidates) == 1:
            return {"token": token, "suggestion": candidates[0]}
    return None


def _brand_known_places(brand: dict[str, Any] | None) -> set[str]:
    """把品牌自己声明的城市也当成已知地名 —— 否则小地名会被误判成错字。"""
    if not brand:
        return set()
    out: set[str] = set()
    for field in ("cities", "city_scope"):
        raw = brand.get(field)
        if not raw:
            continue
        for piece in re.split(r"[,、,;;/\s]+", str(raw)):
            piece = piece.strip().rstrip("省市区县")
            if len(piece) >= 2:
                out.add(piece)
    return out


def lookup_keyword_history(keyword: str) -> dict[str, Any]:
    """该词在生产里被测过多少次、命中率多少(只读;查不到就诚实返回 unknown)。"""
    from db.connection import get_db

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS tests, COALESCE(SUM(is_detected), 0) AS hits
                  FROM monitoring_results
                 WHERE keyword = %s
                """,
                (str(keyword),),
            )
            row = dict(cur.fetchone() or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[keyword-hygiene] 历史查询失败(按 unknown 处理): %s", str(exc)[:200])
        return {"known": False}

    tests = int(row.get("tests") or 0)
    if tests <= 0:
        return {"known": False}
    hits = int(row.get("hits") or 0)
    return {"known": True, "tests": tests, "hits": hits, "hit_rate": round(hits / tests, 4)}


def check_keywords(
    keywords: list[str],
    *,
    brand: dict[str, Any] | None = None,
    existing_count: int = 0,
    with_history: bool = True,
) -> dict[str, Any]:
    """闸本体。返回 {blocking: [...], advisory: [...], checked: n}。

    `blocking` 非空 = 调用方应拒绝写入并把 suggestion 回给用户。
    `advisory`  = 只提示,不拦。
    """
    extra_known = _brand_known_places(brand)
    blocking: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []

    cleaned = [str(k).strip() for k in (keywords or []) if str(k or "").strip()]

    for keyword in cleaned:
        typo = detect_place_typo(keyword, extra_known=extra_known)
        if typo:
            blocking.append({
                "rule": "place_typo",
                "keyword": keyword,
                "token": typo["token"],
                "suggestion": typo["suggestion"],
                "fixed_keyword": keyword.replace(typo["token"], typo["suggestion"]),
                "message": (
                    f"「{typo['token']}」看起来是「{typo['suggestion']}」的错字。"
                    f"建议改成「{keyword.replace(typo['token'], typo['suggestion'])}」。"
                ),
            })
            continue
        if with_history:
            history = lookup_keyword_history(keyword)
            if (history.get("known")
                    and int(history.get("tests") or 0) >= RED_OCEAN_MIN_TESTS
                    and float(history.get("hit_rate") or 0) <= RED_OCEAN_MAX_HIT_RATE):
                advisory.append({
                    "rule": "red_ocean",
                    "keyword": keyword,
                    "tests": history["tests"],
                    "hit_rate": history["hit_rate"],
                    "message": (
                        f"这个词历史上测过 {history['tests']} 次,命中率 "
                        f"{history['hit_rate'] * 100:.1f}% —— 竞争很激烈,"
                        "配它要有打长线的准备,或者换个更具体的问法。"
                    ),
                })

    total_after = int(existing_count or 0) + len(cleaned)
    if total_after < MIN_KEYWORDS_PER_BRAND:
        advisory.append({
            "rule": "too_few_keywords",
            "count": total_after,
            "minimum": MIN_KEYWORDS_PER_BRAND,
            "message": (
                f"这个品牌配好后只有 {total_after} 个监测词(建议至少 {MIN_KEYWORDS_PER_BRAND} 个)。"
                "词太少,一次波动就会让整条趋势线看起来大起大落。"
            ),
        })

    return {"checked": len(cleaned), "blocking": blocking, "advisory": advisory}


def assert_keywords_clean(
    keywords: list[str],
    *,
    brand: dict[str, Any] | None = None,
    existing_count: int = 0,
) -> dict[str, Any]:
    """闸的 HTTP 包装:有 blocking 就抛 400(detail 带确定的更正建议)。

    🔴 只拦 `blocking`(目前唯一一条是行政区错字,且必然带唯一更正)。
       advisory 一律不拦 —— 拦一个"我也不知道怎么办"的提示,就是 Owner 说的
       「此地无银 + 帮不上忙的警告」。
    """
    from fastapi import HTTPException

    report = check_keywords(keywords, brand=brand, existing_count=existing_count)
    if report["blocking"]:
        first = report["blocking"][0]
        raise HTTPException(status_code=400, detail={
            "code": "KEYWORD_HYGIENE_BLOCKED",
            "message": first["message"],
            "issues": report["blocking"],
            "advisory": report["advisory"],
        })
    return report
